import asyncio
import json
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import BUILTIN_SKILLS_SOURCE_DIR, SKILLS_LOCAL_DIR, SKILLS_REPOS_DIR, WORKSPACE_ROOT_DIR
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.schemas.workspace import ChannelAnnotation as ChannelAnnotationData
from nekro_agent.services.workspace import safe_fs
from nekro_agent.services.workspace.cc_credential import (
    DEFAULT_TIMEOUT_MS,
    WorkspaceCredential,
    build_claude_settings_payload,
    build_settings_payload,
    claude_settings_carries_platform_secret,
    resolve_workspace_credential,
)

if TYPE_CHECKING:
    from nekro_agent.core.cc_model_presets import CCModelPresetItem

logger = get_sub_logger("workspace_manager")

# 挂进容器当 `~/.claude/` 的宿主目录（CC 会话历史、技能、用户级设置、`.claude.json` 都在这里），
# 两种布局（见 `ClaudeHome`）：
# - 新：`{工作区根}/.nk-homes/<id>/claude`，在工作区目录之外，只以挂载点 `~/.claude` 出现在容器里；
# - 旧：`{工作区}/.claude_home`，同时也在 `/workspace` 挂载里，容器能对它改名、换成链接。
# 旧布局在建容器时迁到新位置（`WorkspaceService.migrate_claude_home`）
CLAUDE_HOME_DIRNAME = ".claude_home"
HOMES_DIRNAME = ".nk-homes"
CLAUDE_HOME_SUBDIR = "claude"
# CC 格式的设置文件名。NA 写的是**用户级**那一份：home 里的 `settings.json`，
# 容器里 `~/.claude/settings.json`——CC 每次启动都读，不受工作目录影响
CLAUDE_SETTINGS_FILENAME = "settings.json"
# 容器里跑 CC 的用户：cc-sandbox 镜像里的 `appuser`，uid / gid 都是 1000。镜像入口脚本 `chown` 工作区
# 用的是它，nk-ssh 收权限脚本里私钥的属主也是这个数；NA 没有对应的配置项，跟镜像保持一致。
# NA 在 CC home 里建的目录、写的文件都交给它（`WorkspaceService.give_to_container`）
CONTAINER_USER_UID = 1000
CONTAINER_USER_GID = 1000
# 早先 NA 把这份设置写在 `{ws}/.claude/settings.json`（容器里 `/workspace/.claude/`），
# 而 CC 的工作目录是 `/workspace/default`，项目级设置只从工作目录下的 `.claude/` 读，所以那份
# 文件从来没生效过；里面却可能留着以前写出去的凭据。写设置与启动复核都会把它删掉
LEGACY_CLAUDE_SETTINGS_PARTS: Tuple[str, str] = (".claude", "settings.json")


@dataclass(frozen=True)
class ClaudeHome:
    """一个工作区的 CC home 在宿主上的位置：`safe_fs` 的可信根 `base` 加上它下面的 `parts`

    新布局 `base` 是工作区根、`parts` 是 `(.nk-homes, <id>, claude)`：`.nk-homes` 与 `<id>` 是 NA
    自己的目录（0700，不挂进任何容器），只有最后一级 `claude` 挂进容器，在容器里对它改名会得到
    EBUSY。旧布局 `base` 是工作区目录、`parts` 是 `(.claude_home,)`，整个都在容器写得到的地方。
    两种布局下 home 里面的条目都是容器写得到的，读写一律走 `safe_fs`。
    """

    base: Path
    parts: Tuple[str, ...]
    legacy: bool
    # 哪个工作区的：旧布局的 home 不见了（刚迁走）时按它重新定位
    workspace_id: int

    @property
    def path(self) -> Path:
        """字面路径（不 `resolve()`）"""
        return self.base.joinpath(*self.parts)

    def sub(self, *names: str) -> Tuple[str, ...]:
        """home 里某个条目相对 `base` 的路径分量"""
        return (*self.parts, *names)


class StaleHomeError(RuntimeError):
    """新建工作区时同号残留的 CC home 清不掉，创建已撤销"""


# 每个工作区一把线程锁：动 CC home 位置的操作（迁移、把旧 home 挪出工作区）与往 home 里写东西
# （用户级设置、技能、nk-ssh）互斥。迁移在线程里跑，写的一方有的在事件循环里（热更新、撤销凭据、
# 动态技能），各自算好 home 之后才动手：没有这把锁，迁移可能正好落在「算好旧位置」与「写进去」之间。
# 持有期间只做几个文件系统调用，不 await；可重入（迁移里会再调挪旧 home 的那一步）
_home_locks: Dict[int, threading.RLock] = {}
_home_locks_guard = threading.Lock()


def home_lock(workspace_id: int) -> threading.RLock:
    with _home_locks_guard:
        lock = _home_locks.get(int(workspace_id))
        if lock is None:
            lock = threading.RLock()
            _home_locks[int(workspace_id)] = lock
        return lock


# NA 改不了属主（不是 root，也不是 uid 1000）时只提醒一次，免得每写一个文件记一条
_chown_refusal_reported = False


# 每 workspace 一把 asyncio.Lock，避免 MCP read-modify-write 并发覆盖
_mcp_locks: Dict[int, asyncio.Lock] = {}


def _get_mcp_lock(workspace_id: int) -> asyncio.Lock:
    lock = _mcp_locks.get(workspace_id)
    if lock is None:
        lock = asyncio.Lock()
        _mcp_locks[workspace_id] = lock
    return lock


def _write_workspace_json(ws_dir: Path, rel: str, payload: Dict[str, Any]) -> None:
    """把 JSON 整份写进工作区里容器可写的位置，不跟随容器放的符号链接（见 `safe_fs`）

    先写临时文件再改名：容器里的 CC 随时可能在读，不能让它读到写了一半的 JSON。
    权限给 0644：容器里的 appuser 与 NA 进程未必是同一个 uid，要保证它读得到。
    上级目录由调用方先建好（`create_dirs=False`），这里不顺手建。
    """
    safe_fs.write_text(ws_dir, rel, json.dumps(payload, indent=2, ensure_ascii=False), mode=0o644, create_dirs=False)


def _global_library_by_name() -> Dict[str, Dict[str, Any]]:
    """读全局 MCP 库为 {name: raw_dict}"""
    from nekro_agent.core.auto_inject_mcp import get_auto_inject_mcp_servers

    library: Dict[str, Dict[str, Any]] = {}
    for raw in get_auto_inject_mcp_servers():
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not name:
            continue
        library[name] = raw
    return library


def _raw_to_disk_entry(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把全局库的一条 raw 转成 .mcp.json 写盘格式（去 NA-internal 字段）

    仅 validation.status == 'validated' 的条目能进入 .mcp.json。

    字段名跟着 Claude Code 的 schema 走：transport 类型叫 `type`，不叫 `transport`；`id` 是
    NA 内部标识，写进 `.mcp.json` 只会让 CC 报未知字段。
    """
    validation = raw.get("validation") or {}
    if (validation.get("status") if isinstance(validation, dict) else None) != "validated":
        return None
    # 跳过 NA 内部字段
    skip = {"id", "name", "auto_inject", "enabled", "validation"}
    entry: Dict[str, Any] = {}
    for k, v in raw.items():
        if k in skip:
            continue
        entry[k] = v
    return entry


def _resolve_library_entry(name: str, library: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按名字在全局库里找条目，认不出名字时按「是不是内置网页聊天条目」兜底

    兜底是为了改名：用户在库里把内置条目改成别的名字后，工作区 `mcp_servers_enabled` 里
    记的仍是旧名；此时只要那个旧名指向的确实是这条内置条目，就退回库里唯一的那一条。
    """
    raw = library.get(name)
    if raw is not None:
        return raw

    from nekro_agent.services.mcp.web_chat_auth import is_web_chat_mcp_entry

    if not is_web_chat_mcp_entry({"name": name}):
        return None
    return next((entry for entry in library.values() if is_web_chat_mcp_entry(entry)), None)


def mcp_entry_has_credentials(raw: Mapping[str, Any]) -> bool:
    """这条全局 MCP 库条目是不是「带凭据」的

    判据刻意只看**有没有** `env` / `headers`，不看里面装的是什么：库条目只有平台方能改，
    而任何一项都可能是令牌、口令或外部服务的鉴权头；逐项猜测哪些「像凭据」必然会漏。
    代价是空字典与纯配置项也会被算进来——写成非空才算，`{"FOO": ""}` 这一种仍然算。
    网页聊天那一条不按这个判据（它有自己的资格判据，见 `is_web_chat_mcp_entry`）。
    """
    for key in ("env", "headers"):
        value = raw.get(key)
        if isinstance(value, Mapping) and value:
            return True
    return False


def _build_disk_mcp_config(
    workspace_metadata: Dict[str, Any],
    library: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    web_chat_workspace_id: Optional[int] = None,
    allow_platform_secrets: bool,
) -> Dict[str, Any]:
    """从全局 MCP 库 + workspace.metadata.mcp_servers_enabled 构建 .mcp.json 写盘内容。

    向后兼容：当 mcp_servers_enabled 不存在时，回退读取老的 metadata.mcp_config.mcpServers
    （把其 key 视作 enabled name 列表，从全局库查配置）。

    网页聊天条目的令牌**在这里现场派生**：库里不存令牌，写盘时按 `web_chat_workspace_id`
    签一张。为 None（工作区此刻不合格）时直接跳过这一条——不仅调用时会被拒，也没有理由把
    一张平台方令牌摆进以后可能交给租户的文件里。`headers` 整体覆盖：库里或手工条目里带的
    任何头都不保留。

    `allow_platform_secrets=False`（工作区不能拿平台方凭据）时，`env` / `headers` 非空的条目
    整条跳过（判据见 :func:`mcp_entry_has_credentials`）：那些值会写进租户读得到的
    `.mcp.json`。不带凭据的条目照写。
    """
    if library is None:
        library = _global_library_by_name()

    from nekro_agent.services.mcp.web_chat_auth import (
        build_workspace_web_chat_mcp_headers,
        is_web_chat_mcp_entry,
    )

    enabled_names: List[str] = list(workspace_metadata.get("mcp_servers_enabled", []) or [])
    if not enabled_names:
        # 回退：老数据中 mcp_config.mcpServers 的 key
        legacy_servers: Dict[str, Any] = (workspace_metadata.get("mcp_config") or {}).get("mcpServers", {}) or {}
        enabled_names = list(legacy_servers.keys())

    dst_servers: Dict[str, Any] = {}
    for name in enabled_names:
        raw = _resolve_library_entry(name, library)
        if raw is None:
            continue
        entry = _raw_to_disk_entry(raw)
        if entry is None:
            continue
        if is_web_chat_mcp_entry(raw):
            if web_chat_workspace_id is None:
                logger.debug(f"跳过网页聊天 MCP 条目 {name}：工作区不含该条目或此刻不合格")
                continue
            entry["headers"] = build_workspace_web_chat_mcp_headers(web_chat_workspace_id)
        elif not allow_platform_secrets and mcp_entry_has_credentials(raw):
            logger.debug(f"跳过带凭据的 MCP 条目 {name}：工作区不能拿平台方凭据")
            continue
        dst_servers[name] = entry
    return {"mcpServers": dst_servers}


async def _render_disk_mcp_json(workspace: DBWorkspace) -> Tuple[str, bool]:
    """渲染这个工作区的 `.mcp.json` 内容

    网页聊天条目的令牌只发给合格的工作区：启用清单里确实有这一条时才去判资格，没有就不必
    付出一次归属推导与记录重建的代价。

    带凭据的条目同样只发给能拿平台方凭据的工作区（同一条判据，见
    `services/workspace/credential_policy.py`）。

    Returns:
        （文件内容，内容里有没有带凭据的全局库条目）。后者按渲染出来的条目算，网页聊天
        那一条不算（它的令牌只对本工作区有效，有自己的清理路径）。
    """
    from nekro_agent.services.mcp.web_chat_auth import is_web_chat_mcp_entry
    from nekro_agent.services.workspace.credential_policy import platform_credentials_allowed

    library = _global_library_by_name()
    metadata = workspace.metadata or {}
    enabled_names: List[str] = list(metadata.get("mcp_servers_enabled", []) or [])
    if not enabled_names:
        legacy_servers: Dict[str, Any] = (metadata.get("mcp_config") or {}).get("mcpServers", {}) or {}
        enabled_names = list(legacy_servers.keys())

    has_web_chat_entry = any(
        (raw := _resolve_library_entry(name, library)) is not None and is_web_chat_mcp_entry(raw)
        for name in enabled_names
    )

    web_chat_workspace_id: Optional[int] = None
    if has_web_chat_entry:
        from nekro_agent.services.mcp.web_chat_workspace_taint import is_workspace_web_chat_token_eligible

        if await is_workspace_web_chat_token_eligible(workspace):
            web_chat_workspace_id = workspace.id

    allowed = await platform_credentials_allowed(workspace)

    config = _build_disk_mcp_config(
        metadata,
        library,
        web_chat_workspace_id=web_chat_workspace_id,
        allow_platform_secrets=allowed,
    )
    carries_secret = any(
        (raw := _resolve_library_entry(name, library)) is not None
        and not is_web_chat_mcp_entry(raw)
        and mcp_entry_has_credentials(raw)
        for name in config["mcpServers"]
    )
    return json.dumps(config, indent=2, ensure_ascii=False), carries_secret


async def _write_disk_mcp_json(workspace: DBWorkspace) -> None:
    """写出 `default/.mcp.json`；写出了带凭据的条目就记「下发过平台方凭据」

    记在写盘之后：归属变化时撤销那一步靠这个标记决定要不要停容器——MCP 服务进程是带着
    这些 `env` 拉起来的，只重写文件挡不住已经在跑的那一份。

    工作区目录整个挂进容器，`default/` 与文件本身都可能被换成符号链接：走 `safe_fs`，
    逐级不跟随，写临时文件再 rename。
    """
    content, carries_secret = await _render_disk_mcp_json(workspace)
    write_workspace_text(workspace.id, ("default", ".mcp.json"), content)
    if carries_secret:
        from nekro_agent.services.workspace.credential_policy import mark_platform_secret_delivered

        await mark_platform_secret_delivered(workspace)


def write_workspace_text(workspace_id: int, rel: Tuple[str, ...], text: str, *, mode: Optional[int] = None) -> None:
    """往工作区目录里写一个 NA 自己的文件（CLAUDE.md、.mcp.json、settings 一类）

    工作区根目录是 NA 建的、容器换不掉的那一级，从它出发逐级 `O_NOFOLLOW`；途中被容器换成
    链接或文件的目录是 NA 自己的路径，删掉链接本身重建。`mode` 不给时沿用原文件的权限与属主。
    """
    safe_fs.write_text(
        WorkspaceService.get_workspace_dir(workspace_id),
        rel,
        text,
        mode=mode,
        replace_links=True,
    )


class WorkspaceService:
    @staticmethod
    def get_workspace_dir(workspace_id: int) -> Path:
        return Path(WORKSPACE_ROOT_DIR) / str(workspace_id)

    # ── CC home（容器里的 ~/.claude）────────────────────────────────────────
    #
    # 新布局在工作区根下的 `.nk-homes/<id>/claude`：与回收目录 `.nk-trash/<id>` 一样在工作区
    # 目录之外、容器写不到它的上级，而且与工作区目录在同一个文件系统上，迁移是一次 `renameat`。

    @staticmethod
    def new_claude_home(workspace_id: int) -> ClaudeHome:
        # 工作区根按 `get_workspace_dir` 的上级算（与回收目录 `_open_trash_dir` 同一口径）
        return ClaudeHome(
            base=WorkspaceService.get_workspace_dir(workspace_id).parent,
            parts=(HOMES_DIRNAME, str(int(workspace_id)), CLAUDE_HOME_SUBDIR),
            legacy=False,
            workspace_id=int(workspace_id),
        )

    @staticmethod
    def legacy_claude_home(workspace_id: int) -> ClaudeHome:
        return ClaudeHome(
            base=WorkspaceService.get_workspace_dir(workspace_id),
            parts=(CLAUDE_HOME_DIRNAME,),
            legacy=True,
            workspace_id=int(workspace_id),
        )

    @staticmethod
    def claude_home(workspace_id: int) -> ClaudeHome:
        """这个工作区现在的 home。宿主侧读写 home、算挂载源、核对挂载的地方一律经它定位

        - 新位置已经是目录：用新的；
        - 还没迁、工作区目录里有 `.claude_home`：用旧的——**不论它是不是普通目录**。升级时还在跑
          的容器挂的是旧路径，重启沿用这份挂载，迁移只在建容器时做（`migrate_claude_home`）。旧的
          被容器换成了链接时也得按旧布局来：写的一方不跟随链接（写设置时删掉链接重建，写 nk-ssh
          时不写），重启前的修复与核对也落在旧路径上——换成「新位置」的话，旧路径上的链接没人修，
          重启时 Docker 会顺着它把宿主别处挂进容器；
        - 都没有（新工作区，或者旧的被容器整个删掉了）：新的。
        """
        new = WorkspaceService.new_claude_home(workspace_id)
        if safe_fs.kind(new.base, new.parts) == "dir":
            return new
        legacy = WorkspaceService.legacy_claude_home(workspace_id)
        if safe_fs.kind(legacy.base, legacy.parts) is not None:
            return legacy
        return new

    @staticmethod
    def migrate_claude_home(workspace_id: int) -> ClaudeHome:
        """把旧布局的 `.claude_home` 挪到工作区之外，返回迁移之后用的 home

        只能在这个工作区没有任何容器在跑、拿着工作区锁的时候调（建容器清场之后）：这时没有谁能
        动工作区目录，旧 `.claude_home` 的判断与改名之间没有竞争。NA 自己在别处往 home 里写的
        （热更新、撤销凭据、动态技能）靠 `home_lock` 排开。
        - 新位置已经是目录：已经迁过。旧的若还在（退回旧版本又升级回来、没跑回滚脚本），整个挪出
          工作区（`retire_legacy_home`）；
        - 旧的是普通目录：基于目录句柄 `renameat` 整个挪过去（同一文件系统，原子，没有大小上限，
          会话历史、技能、`.claude.json`、nk-ssh 钥匙原样带走）；改名失败（例如工作区目录单独挂在
          另一个文件系统上）就照旧按旧布局启动，记 ERROR，不让启动失败；
        - 旧的是链接或别的东西：不挪（那头不是 NA 的东西），新位置建空目录，记 ERROR；
        - 都没有：建新位置。
        """
        with home_lock(workspace_id):
            return WorkspaceService._migrate_claude_home_locked(workspace_id)

    @staticmethod
    def _migrate_claude_home_locked(workspace_id: int) -> ClaudeHome:
        new = WorkspaceService.new_claude_home(workspace_id)
        legacy = WorkspaceService.legacy_claude_home(workspace_id)
        new_kind = safe_fs.kind(new.base, new.parts)
        legacy_kind = safe_fs.kind(legacy.base, legacy.parts)
        if new_kind == "dir":
            if legacy_kind is not None:
                WorkspaceService.retire_legacy_home(workspace_id)
            WorkspaceService.ensure_claude_home(new)
            return new
        if new_kind is not None:
            # `.nk-homes/<id>/claude` 是 NA 自己的路径，不是目录只能是残留：删掉条目本身
            with safe_fs.open_dir(new.base, new.parts[:-1]) as parent:
                parent.remove_tree(new.parts[-1])
        if legacy_kind == "dir":
            for depth in range(1, len(new.parts)):
                safe_fs.ensure_dir(new.base, new.parts[:depth], mode=0o700, replace_links=True)
                safe_fs.chmod(new.base, new.parts[:depth], 0o700)
            try:
                with safe_fs.open_dir(legacy.base, legacy.parts[:-1]) as source, safe_fs.open_dir(
                    new.base,
                    new.parts[:-1],
                ) as target:
                    source.rename_to(legacy.parts[-1], target, new.parts[-1])
            except OSError as e:
                logger.error(
                    f"工作区 {workspace_id} 的 {CLAUDE_HOME_DIRNAME} 没能挪到工作区之外（{type(e).__name__}: {e}），"
                    f"这次照旧按旧位置启动。手动迁移：停掉这个工作区的容器后把 {legacy.path} 挪到 {new.path}",
                )
                return legacy
            if safe_fs.kind(new.base, new.parts) != "dir":
                # 不该发生（容器停着，挪的是核对过的目录）：挪过来的不是目录就删掉，建空的
                logger.error(f"工作区 {workspace_id} 挪到新位置的 CC home 不是目录，已删掉并新建空目录")
                with safe_fs.open_dir(new.base, new.parts[:-1]) as parent:
                    parent.remove_tree(new.parts[-1])
            WorkspaceService.ensure_claude_home(new)
            logger.info(f"工作区 {workspace_id} 的 CC home 已迁到工作区目录之外: {new.path}")
            return new
        if legacy_kind is not None:
            logger.error(
                f"工作区 {workspace_id} 的 {CLAUDE_HOME_DIRNAME} 不是普通目录（{legacy_kind}，容器可能想把宿主目录挂进来），"
                "不迁移、不碰它，在工作区之外新建空的 CC home；原来的会话与技能如果被挪走了，在工作区目录里找",
            )
        WorkspaceService.ensure_claude_home(new)
        return new

    @staticmethod
    def retire_legacy_home(workspace_id: int) -> Optional[Path]:
        """新位置已经在用、工作区目录里还有一份旧 `.claude_home`：整个挪出工作区，返回挪到了哪里

        这份旧 home 不再挂进容器当 `~/.claude`，NA 也不再往里写（定位一律落到新位置），可它还在
        `/workspace` 里，容器读得到。退回旧版本又升级回来（没跑回滚脚本）时就是这样：旧版本往里写过
        `settings.json`（`direct` 模式下带平台 Key）和 `nk-ssh/id_gateway`，之后撤销凭据只重写新
        home，这一份没人管。所以挪到 `.nk-homes/<id>/legacy-<时间>`（0700，不挂进任何容器），日志写出
        路径，确认没用了由管理员删；删工作区时随 `.nk-homes/<id>` 一起删。

        只在没有容器把旧 home 挂成 `~/.claude` 的时候调：建容器（迁移里）、重启（停下来之后）、NA
        启动复核（核对挂载之后）。
        - 新位置不是目录（还是旧布局）、旧的不在：什么都不做；
        - 旧的是链接或文件：不挪（不是 NA 写的：NA 写 home 不跟随链接），记 WARNING；
        - 改名失败：退一步删掉里面 NA 写的那几样（`settings.json`、`nk-ssh`），记 ERROR。
        """
        with home_lock(workspace_id):
            new = WorkspaceService.new_claude_home(workspace_id)
            legacy = WorkspaceService.legacy_claude_home(workspace_id)
            if safe_fs.kind(new.base, new.parts) != "dir":
                return None
            legacy_kind = safe_fs.kind(legacy.base, legacy.parts)
            if legacy_kind is None:
                return None
            if legacy_kind != "dir":
                logger.warning(
                    f"工作区 {workspace_id} 已经在用工作区之外的 CC home，工作区目录里的 {CLAUDE_HOME_DIRNAME} "
                    f"是 {legacy_kind}（不是 NA 写的），不碰它",
                )
                return None
            holder = new.parts[:-1]
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            try:
                for depth in range(1, len(holder) + 1):
                    safe_fs.ensure_dir(new.base, holder[:depth], mode=0o700, replace_links=True)
                    safe_fs.chmod(new.base, holder[:depth], 0o700)
                with safe_fs.open_dir(legacy.base) as source, safe_fs.open_dir(new.base, holder) as target:
                    name = f"legacy-{stamp}"
                    suffix = 1
                    while target.kind(name) is not None:
                        suffix += 1
                        name = f"legacy-{stamp}-{suffix}"
                    source.rename_to(CLAUDE_HOME_DIRNAME, target, name)
            except OSError as e:
                scrubbed = WorkspaceService._scrub_legacy_home(legacy)
                logger.error(
                    f"工作区 {workspace_id} 工作区目录里旧的 {CLAUDE_HOME_DIRNAME} 没能挪出工作区（{type(e).__name__}: {e}），"
                    f"删掉了里面 NA 写过的 {scrubbed or '（没有）'}；剩下的还在 {legacy.path}，请停掉容器后手动处理",
                )
                return None
            try:
                safe_fs.chmod(new.base, (*holder, name), 0o700)
            except OSError as e:
                logger.warning(f"收紧挪出来的旧 CC home 的权限失败（它在 0700 的目录里，宿主上别的用户照样进不去）: {e}")
            moved = new.base.joinpath(*holder, name)
            logger.warning(
                f"工作区 {workspace_id} 已经在用工作区之外的 CC home，工作区目录里还有一份旧的 {CLAUDE_HOME_DIRNAME}"
                f"（退回旧版本又升级回来时会有，里面可能有旧版本写下的凭据），已挪出工作区: {moved}；确认没用了可以删掉",
            )
            return moved

    @staticmethod
    def _scrub_legacy_home(legacy: ClaudeHome) -> List[str]:
        """旧 home 挪不走时的退路：删掉里面 NA 写的 `settings.json` 与 `nk-ssh`，返回删了哪些"""
        from nekro_agent.services.ssh_gateway.container_files import NK_SSH_DIRNAME

        removed: List[str] = []
        try:
            with safe_fs.open_dir(legacy.base, legacy.parts) as home:
                if home.kind(CLAUDE_SETTINGS_FILENAME) not in (None, "dir") and home.unlink(CLAUDE_SETTINGS_FILENAME):
                    removed.append(CLAUDE_SETTINGS_FILENAME)
                if home.kind(NK_SSH_DIRNAME) is not None and home.remove_tree(NK_SSH_DIRNAME):
                    removed.append(NK_SSH_DIRNAME)
        except (FileNotFoundError, safe_fs.UnsafePathError) as e:
            logger.error(f"清理工作区 {legacy.workspace_id} 旧 {CLAUDE_HOME_DIRNAME} 里 NA 写过的文件也失败了: {e}")
        return removed

    @staticmethod
    def discard_stale_home(workspace_id: int) -> List[str]:
        """新建工作区时调：同号的 home 若已经存在（数据库重置、导入导出之后 ID 被复用），清掉

        那是上一个用这个编号的工作区的 CC 会话历史、`.claude.json`、网关私钥，不能被新工作区接手。
        工作区之外的 `.nk-homes/<id>` 与工作区目录里旧布局的 `.claude_home` 都清；删不完（条目太多）
        的整体挪进回收目录，下次初始化再清。返回清掉了哪些（给日志）。
        """
        removed = WorkspaceService._remove_home_entries(workspace_id)
        if removed:
            logger.warning(f"新建的工作区 {workspace_id} 的编号以前用过，清掉了上一个工作区留下的 CC home: {removed}")
        return removed

    @staticmethod
    def _remove_home_entries(workspace_id: int) -> List[str]:
        """删掉这个编号的两种 home：工作区之外的 `.nk-homes/<id>`、工作区目录里旧布局的 `.claude_home`

        逐级不跟随链接，旧 `.claude_home` 是链接只删链接本身；删不完（条目太多）的整体挪进回收
        目录。返回删了（或挪走了）哪些。
        """
        removed: List[str] = []
        root = WorkspaceService.get_workspace_dir(workspace_id).parent
        legacy = WorkspaceService.legacy_claude_home(workspace_id)
        targets = (
            (root, (HOMES_DIRNAME,), str(int(workspace_id))),
            (legacy.base, (), CLAUDE_HOME_DIRNAME),
        )
        with home_lock(workspace_id):
            for base, parent_parts, name in targets:
                if safe_fs.kind(base, (*parent_parts, name)) is None:
                    continue
                with safe_fs.open_dir(base, parent_parts) as parent, WorkspaceService._open_trash_dir(
                    workspace_id,
                    create=True,
                ) as trash:
                    parent.remove_tree_or_discard(name, trash)
                removed.append(str(base.joinpath(*parent_parts, name)))
        return removed

    @staticmethod
    async def claim_fresh_home(workspace: DBWorkspace) -> None:
        """新建的工作区记录一落库就调：清掉同号残留的 CC home；清不掉就删掉这条记录、抛 `StaleHomeError`

        宁可让这次创建失败、让管理员看见报错，也不让新工作区接手上一个同号工作区的会话历史与网关
        私钥。编号在落库之前不知道，所以清扫只能排在 `create` 之后、别的初始化之前。
        """
        try:
            await asyncio.to_thread(WorkspaceService.discard_stale_home, workspace.id)
        except Exception as e:
            logger.error(f"新建工作区 {workspace.id} 时清不掉同号残留的 CC home，撤销创建: {type(e).__name__}: {e}")
            try:
                await workspace.delete()
            except Exception as drop_error:  # noqa: BLE001 - 原来的异常更要紧，这条记录留给管理员删
                logger.error(f"撤销创建工作区 {workspace.id} 也失败了，请手动删除它: {drop_error}")
            raise StaleHomeError(
                f"编号 {workspace.id} 上一个工作区留下的 CC home 清不掉，工作区没有创建，请重试: {type(e).__name__}",
            ) from e

    @staticmethod
    def remove_workspace_home(workspace_id: int) -> List[str]:
        """删工作区时调（库里删除已经提交之后）：删掉它的 CC home（两种布局都删）

        工作区目录本身照旧不删（那是用户的工作产物，见删除接口的判定表），但旧布局的
        `.claude_home` 虽然在工作区目录里也一起删：home 里是 CC 的会话历史、`.claude.json`、网关
        私钥，留着只会在编号被复用时被下一个工作区接手。删不完的挪进回收目录再清一遍，回收目录
        清空了也删掉；清不完的留到下次（同号工作区下次初始化时）。返回删了哪些。
        """
        removed = WorkspaceService._remove_home_entries(workspace_id)
        try:
            with WorkspaceService._open_trash_dir(workspace_id, create=False) as trash:
                trash.purge_trash()
            with safe_fs.open_dir(WorkspaceService.get_workspace_dir(workspace_id).parent, (".nk-trash",)) as trash_root:
                trash_root.rmdir(str(int(workspace_id)))  # 清空了才删得掉
        except FileNotFoundError:
            pass
        if removed:
            logger.info(f"工作区 {workspace_id} 的 CC home 已删除: {removed}")
        return removed

    # ── CLAUDE.md 模板 ───────────────────────────────────────────────────────
    _CLAUDE_MD_TEMPLATE = """\
# NekroAgent CC Workspace — 身份与协作规范

## 你是谁

你是 **NekroAgent**（NA）系统中的执行层子 Agent，运行在独立的 cc-sandbox 容器中。

**代理执行模型**（必须理解）：
```
[人类用户] ←IM聊天→ [NA 主 Agent] ←任务委托→ [你]
     ↑                                        ↑
  只能看到 NA 的消息               你的工作区用户完全无法访问
```
- 真正的最终用户在 IM 端，你应当默认其**无法访问**你的 `/workspace/` 目录、代码、文件或命令输出
- NA 是你的直接通信方，负责将你的成果转化为用户可获取的形式
- 任务"完成"的标准不是"代码改完了"，而是**用户能够获取/使用你的成果**

**你的职责**：执行编程、文件处理、命令执行等任务，并确保成果可交付。

**你是工作区的第一手知识持有者**：NA 无法主动读取你的工作区。当 NA 指令与实际状态矛盾时，基于现实调整并说明原因。

**你不应该**：修改 `settings.json`、`.mcp.json` 等配置文件；访问 `/workspace/` 外的资源；盲目执行包含错误假设的指令。

## 成果交付（核心）

**用户无法访问你的工作区。** 每次任务完成前必须确保成果可达：

| 任务类型 | 交付方式 |
|----------|----------|
| 外部仓库代码修改 | 创建 PR 或推送分支，**响应中返回链接** |
| 生成文件/报告 | 放入 `/workspace/default/shared/`，NA 会获取并发送给用户 |
| 分析/调研 | 响应中详细说明；内容较长时同时写入 `shared/` |
| 无远程权限的代码修改 | 生成 patch/压缩包到 `shared/`，或说明 clone 方式 |

**错误示范** ❌：`我已修改了 src/utils.py`（用户无法访问）
**正确示范** ✅：`已修改并创建 PR: https://github.com/xxx/repo/pull/42`

## 工作环境

### 目录路径

| 目录 | 路径 | 说明 |
|------|------|------|
| 工作目录（cwd） | `/workspace/default/` | Claude Code 默认工作目录 |
| 共享目录 | `/workspace/default/shared/` | 与 NA 交换文件（NA 实时感知目录内容，将产出物放在这里） |
| 技能目录 | `~/.claude/skills/` | Claude Code 可发现的 workspace 本地技能目录 |
| 运行策略 | {runtime_policy} | 控制工具可用范围（agent/relaxed/strict） |

### 预装 CLI 工具

| 工具 | 路径 | 说明 |
|------|------|------|
| `git` | `/usr/bin/git` | 版本控制 |
| `gh` | `/usr/bin/gh` | GitHub CLI — PR/Issue/Repo/Actions，需设置 `GH_TOKEN` |
| `node` v20 | `/usr/bin/node` | Node.js 20 运行时 |
| `npm` | `/usr/bin/npm` | Node 包管理器 |
| `agent-browser` | `/usr/local/bin/agent-browser` | 浏览器自动化（Playwright/Chromium 已预装） |
| `python3` | `/usr/bin/python3` | Python 3.13 |
| `uv` | `/usr/local/bin/uv` | Python 包管理器（推荐替代 pip） |
| `curl` / `wget` | `/usr/bin/curl` `/usr/bin/wget` | HTTP 客户端 |
| `jq` | `/usr/bin/jq` | JSON 处理器（与 `gh api` 配合极佳） |
| `ssh` | `/usr/bin/ssh` | SSH 客户端（支持 Git over SSH） |
| `unzip` / `zip` | `/usr/bin/unzip` `/usr/bin/zip` | 压缩/解压 |
| `bash` | `/bin/bash` | Shell |

### 关键工具说明

**agent-browser（浏览器自动化）**：
- Playwright Chromium 已预装，路径由 `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers` 配置
- **直接使用，无需安装浏览器**：`agent-browser open https://example.com`

**gh（GitHub CLI）**：
- 已安装，但每次使用前须设置认证：`export GH_TOKEN="<your_github_pat>"`
- 持久化方案：`echo 'export GH_TOKEN=your_pat' >> ~/.bashrc`

**Python 开发**：
- 工作区内用 `uv add <包名>` 安装依赖（持久化在工作区目录）
- 运行脚本：`python3 script.py` 或 `uv run script.py`

**Node.js 开发**：
- 直接运行 `node script.js` 或 `npx <包名>`
- 安装包：`npm install <包名>` 或 `npm install -g <包名>`（全局）

**Skills（Claude Code 技能）**：
- Claude Code 从 `~/.claude/skills/<skill-name>/SKILL.md` 发现技能
- 如需创建 workspace 本地技能，直接在 `~/.claude/skills/<skill-name>/` 下写入技能文件
- 不要手动编造平台级来源元数据；若平台需要，它会在后续扫描、晋升或发布时自动补齐

## 记忆系统

统一记忆与经验沉淀由 NekroAgent 在工作区外部集中管理，你只需要：

1. 在任务结果中清晰写出结论、改动位置、验证结果和风险
2. 遇到可复用的技术经验、排障结论、关键约束时，明确写在回复正文中
3. 不要额外创建、更新或依赖任何本地记忆文件

## 与 NA 的协作协议

1. **任务接收**：你收到的消息已经过 NA 预处理和上下文注入
2. **结果汇报**：任务完成后，在响应末尾附加简短执行摘要（文件路径、关键结论、注意事项）
3. **异常上报**：遇到无法自行解决的问题时，明确说明原因，禁止静默失败
4. **澄清反馈**：当任务描述模糊、包含不存在的文件/函数/路径、或与工作区实际状态明显矛盾时，**不要猜测执行**，而是在响应中列出具体的澄清问题（NA 会转达给人类用户）。格式示例：
   ```
   [需要澄清]
   - 任务中提到的 `./src/auth.py` 在工作区中不存在，请确认正确路径
   - "优化性能"的具体目标是？（减少内存？降低延迟？）
   ```
5. **自主调整**：当 NA 给出的实现思路与工作区实际情况不符时，你可以基于现实调整方案，并在响应中说明"原计划 X，实际按 Y 执行，原因：Z"
6. **记忆沉淀约定**：每次任务完成后，确保回复中包含足够清晰的结论、改动和注意事项，供上层统一记忆系统提炼
7. **任务来源**：每条任务消息头部可能包含 `[任务来源频道: <chat_key>]` 标记，标识该任务来自哪个 NA 会话频道。在多频道共用同一工作区场景下，可利用此信息在记忆文件中区分不同频道的任务背景。
8. **态度认真**：对于来自 NA 的任何指示，必须进行足够深入的分析、思考与研究，时刻思考**我是否真的高质量地完成了任务?**，严禁敷衍了事或形式完成！

## Skills 使用规范（必读）

CC 通过 Skills 与任务领域的最佳实践保持一致。**每次任务开始前，必须先判断是否需要调用相关 skill**。

### Skill 调用规则

当任务涉及你的可用 skill 时，**必须主动加载对应 skill**，并在执行过程中严格遵循其指导

### 重要原则

- **不要凭直觉执行**：当你判断任务涉及上表领域时，必须先加载 skill，即使之前做过类似任务
- **skill 内容即是规范**：skill 中的规则是经过验证的最佳实践，**必须遵循**，不要跳过或自行其是

{resource_section}
{shared_section}
{extra_section}"""

    @staticmethod
    def get_memory_root(workspace_id: int) -> "Path":
        return WorkspaceService.get_workspace_dir(workspace_id) / "default" / "memory"

    @staticmethod
    def get_kb_root(workspace_id: int) -> "Path":
        return WorkspaceService.get_workspace_dir(workspace_id) / "default" / "kb"

    @staticmethod
    def get_kb_files_root(workspace_id: int) -> "Path":
        return WorkspaceService.get_kb_root(workspace_id) / "files"

    @staticmethod
    def get_kb_normalized_root(workspace_id: int) -> "Path":
        return WorkspaceService.get_kb_root(workspace_id) / ".normalized"

    @staticmethod
    def get_kb_source_workspace_path(rel_path: str) -> str:
        return f"/workspace/default/kb/files/{rel_path.strip('/')}"

    @staticmethod
    def get_kb_normalized_workspace_path(rel_path: str) -> str:
        return f"/workspace/default/kb/.normalized/{rel_path.strip('/')}"

    @staticmethod
    def ensure_kb_dirs(workspace_id: int) -> None:
        # `default/kb` 在容器可写的工作区里：逐级不跟随，途中被换成链接的一级删掉链接重建
        ws_dir = WorkspaceService.get_workspace_dir(workspace_id)
        safe_fs.ensure_dir(ws_dir, ("default", "kb", "files"), replace_links=True)
        safe_fs.ensure_dir(ws_dir, ("default", "kb", ".normalized"), replace_links=True)

    @staticmethod
    def resolve_kb_source_path(workspace_id: int, rel_path: str) -> "Path":
        files_root = WorkspaceService.get_kb_files_root(workspace_id)
        try:
            target = (files_root / rel_path).resolve()
            target.relative_to(files_root.resolve())
        except ValueError as e:
            raise ValueError(f"非法 KB 源文件路径: {rel_path}") from e
        return target

    @staticmethod
    def resolve_kb_normalized_path(workspace_id: int, rel_path: str) -> "Path":
        normalized_root = WorkspaceService.get_kb_normalized_root(workspace_id)
        try:
            target = (normalized_root / rel_path).resolve()
            target.relative_to(normalized_root.resolve())
        except ValueError as e:
            raise ValueError(f"非法 KB 规范化全文路径: {rel_path}") from e
        return target

    @staticmethod
    def _generate_env_vars_section(env_vars: "List[Dict[str, Any]]") -> str:
        """从 env_vars 列表生成 CLAUDE.md 的环境变量章节（只展示 key 和 description，不暴露 value）。"""
        if not env_vars:
            return "## 可用环境变量\n\n（当前工作区未配置任何自定义环境变量）"
        lines = [
            "## 可用环境变量",
            "",
            "以下环境变量已由 NekroAgent 注入到当前工作区，可在命令执行中直接使用（变量值已自动注入，无需手动设置）：",
            "",
            "| 变量名 | 说明 |",
            "|--------|------|",
        ]
        for item in env_vars:
            key = item.get("key", "").strip()
            if not key:
                continue
            desc = item.get("description", "").strip() or "（无说明）"
            lines.append(f"| `{key}` | {desc} |")
        return "\n".join(lines)
    @staticmethod
    def _generate_claude_md_content(workspace: "DBWorkspace") -> str:
        """生成工作区 CLAUDE.md 的完整内容。"""
        resource_section = str(workspace.metadata.get("resource_prompt_cache") or "").strip()
        raw_extra = (workspace.metadata.get("claude_md_extra") or "").strip()
        prompt_layers = workspace.metadata.get("prompt_layers", {})
        shared_rules = ""
        if isinstance(prompt_layers, dict):
            shared_layer = prompt_layers.get("shared_manual_rules")
            if isinstance(shared_layer, dict):
                shared_rules = str(shared_layer.get("content") or "").strip()
        shared_section = f"## 共享固定事实\n\n{shared_rules}" if shared_rules else ""
        extra_section = f"## 自定义附加指令\n\n{raw_extra}" if raw_extra else ""
        content = (
            WorkspaceService._CLAUDE_MD_TEMPLATE
            .replace("{runtime_policy}", workspace.runtime_policy or "agent")
            .replace("{resource_section}", resource_section)
            .replace("{shared_section}", shared_section)
            .replace("{extra_section}", extra_section)
        )
        # 总开关关着时**不读缓存、不查库**：输出必须与改造前逐字一致。缓存由异步的
        # 建容器 / 凭据变更路径刷新（`ssh_gateway/container_files.py`），这里只做拼接。
        from nekro_agent.core.config import config

        if config.SSH_GATEWAY_ENABLED:
            from nekro_agent.services.ssh_gateway.container_files import get_ssh_servers_section

            ssh_section = get_ssh_servers_section(workspace.id)
            if ssh_section:
                content = f"{content}\n\n{ssh_section}"
        return content

    @staticmethod
    def update_claude_md(workspace: "DBWorkspace") -> None:
        """（重新）生成并写入 CLAUDE.md，不触发完整 init_workspace_dir。
        适用于 metadata 变更（runtime_policy 等）后的即时刷新。"""
        ws_dir = WorkspaceService.get_workspace_dir(workspace.id)
        if not os.path.lexists(ws_dir / "default"):
            return  # 目录尚未初始化，跳过
        write_workspace_text(workspace.id, ("default", "CLAUDE.md"), WorkspaceService._generate_claude_md_content(workspace))
        logger.debug(f"刷新 CLAUDE.md: 工作区 {workspace.id}")

    @staticmethod
    def _resolve_model_and_timeout(cc_preset: Optional["CCModelPresetItem"]) -> "tuple[str, int]":
        """从预设取出写进 settings.json 的模型名与超时

        `model_type == "preset"` 时刻意留空模型名：那一档由用户级 CC 设置
        （CC home 里的 `settings.json`）的 `model` 字段决定；写进来就会变成 `ANTHROPIC_MODEL`
        环境变量，优先级高于设置文件，反而把预设档位覆盖掉。
        """
        model = ""
        if cc_preset and cc_preset.model_type == "manual":
            model = cc_preset.anthropic_model
        timeout_ms = DEFAULT_TIMEOUT_MS
        if cc_preset and cc_preset.api_timeout_ms:
            try:
                timeout_ms = int(cc_preset.api_timeout_ms)
            except (TypeError, ValueError):
                logger.warning(f"CC 模型预设 {cc_preset.id} 的 api_timeout_ms 非法: {cc_preset.api_timeout_ms!r}")
        return model, timeout_ms

    @staticmethod
    def _write_settings_files(
        ws_dir: Path,
        cc_preset: Optional["CCModelPresetItem"],
        credential: WorkspaceCredential,
        label: str,
        *,
        allow_platform_secrets: bool,
        home: ClaudeHome,
    ) -> bool:
        """把 cc-sandbox 的 settings.json 与 CC 的用户级设置落盘

        两处此前各自拼一份同形状的字典，凭据换成中继令牌之后再复制一遍就等于给自己留一个
        「改了一处忘了另一处、真 Key 从另一份文件漏出去」的坑，所以收在这里。

        - `{ws}/settings.json`：cc-sandbox 自己的格式，它每次拉起 `claude` 前只从这里取
          地址、令牌、手动档模型名、超时四项，翻成环境变量；
        - home 里的 `settings.json`（容器里 `~/.claude/settings.json`）：CC 自己的
          格式，preset 档位的 `model`、`extra_env`、手动档的小模型与别名映射都只在这里。
          `home` 由调用方按工作区算好（`WorkspaceService.claude_home`）。算好之后旧布局的 home
          被迁走了（迁移在线程里跑）的话，按迁移之后的位置写，不在工作区目录里把旧的建回来
          （见 `ensure_claude_home`）；迁移与这里的写入由 `home_lock` 排开。
          写在用户级而不是项目目录（`/workspace/default/.claude/`），是因为 CC 在工作区里做
          git 操作时会把项目目录下的文件连同中继令牌一起提交出去。

        **整份覆盖，不合并**：这份文件按「NA 独占」处理。撤销平台方凭据必须保证
        上一次写出去的凭据一个不剩；合并就得记住「哪些键是 NA 写的」，预设里删掉的
        `extra_env` 项才不会留在文件里，记漏一次就是凭据残留。`-p` 模式下 CC 不往这份文件写，
        CC 自己的状态在 `~/.claude.json`；要自定义的项写项目级设置，优先级本来就更高。

        每次写都顺手删掉早先的旧位置 `{ws}/.claude/settings.json`。

        两份文件都在容器可写的目录里，写入、删除一律走 `safe_fs`，不跟随容器放的符号链接。
        路径被容器动过（上级换成了链接、文件换成了目录）时这一份不写，记 WARNING：那是容器
        自己的目录，坏掉的只是它自己。

        `allow_platform_secrets`：这个工作区能不能拿平台方凭据，两个调用方各自按
        `credential_policy.platform_credentials_allowed` 算好再传进来。为假时用户级设置里
        键名像凭据的 `extra_env` 项被去掉（见 `build_claude_settings_payload`）。

        Returns:
            用户级设置里有没有写出 `extra_env` 带来的平台方凭据。这里是同步函数，
            「下发过」标记由两个调用方据此落库。
        """
        model, timeout_ms = WorkspaceService._resolve_model_and_timeout(cc_preset)

        try:
            _write_workspace_json(ws_dir, "settings.json", build_settings_payload(credential, model, timeout_ms))
            logger.debug(f"{label} settings.json: {ws_dir / 'settings.json'}")
        except (safe_fs.UnsafePathError, IsADirectoryError) as e:
            logger.warning(f"{label} 工作区 {ws_dir.name} 的 settings.json 被换成了目录，没有写入: {e}")

        WorkspaceService.remove_legacy_claude_settings(ws_dir)

        claude_payload: Optional[Dict[str, Any]] = None
        if cc_preset:
            claude_payload = build_claude_settings_payload(
                cc_preset,
                credential,
                allow_platform_secrets=allow_platform_secrets,
            )
        try:
            with home_lock(home.workspace_id):
                home = WorkspaceService.ensure_claude_home(home)
                if claude_payload is None:
                    # 没有预设就没有 NA 要下发的内容；留着上一份等于留着上一份里的凭据
                    with safe_fs.open_dir(home.base, home.parts) as claude_home:
                        if claude_home.kind(CLAUDE_SETTINGS_FILENAME) not in (None, "dir"):
                            claude_home.unlink(CLAUDE_SETTINGS_FILENAME)
                    return False
                _write_workspace_json(home.base, "/".join(home.sub(CLAUDE_SETTINGS_FILENAME)), claude_payload)
                with safe_fs.open_dir(home.base, home.parts) as claude_home:
                    WorkspaceService.give_to_container(claude_home, CLAUDE_SETTINGS_FILENAME)
        except (safe_fs.UnsafePathError, IsADirectoryError, FileNotFoundError) as e:
            # 旧布局下刚修好的 `.claude_home` 又被换掉或删掉（容器还在跑），或者 settings.json 被换成了目录
            logger.warning(f"{label} 工作区 {ws_dir.name} 的 CC home 路径被容器改动过，CC 用户级设置没有写入: {e}")
            return False
        logger.debug(f"{label} CC 用户级设置: {home.path / CLAUDE_SETTINGS_FILENAME}")
        return claude_settings_carries_platform_secret(claude_payload)

    @staticmethod
    def ensure_claude_home(home: ClaudeHome) -> ClaudeHome:
        """建好挂进容器当 `~/.claude/` 的目录，并让容器里的 appuser 可写；返回实际用的那个 home

        bind mount 跨越用户边界时宿主上的属主可能对不上，所以 home 本身照旧放开成 0777。
        调用方之后一律用返回值：传进来的旧布局 home 可能已经不是这个工作区现在的 home 了。

        - 旧布局：**只修，不从无到有建**。`.claude_home` 在容器写得到的工作区目录里，容器能把它
          换成符号链接——按路径 `chmod` 会把链接指向的宿主目录改成 0777，建容器时按路径算挂载源
          还会把链接指向的目录挂进容器。所以不跟随链接地看、先打开再 `fchmod`；它是链接或文件时
          删掉条目本身重建成目录。新位置已经是目录（调用方算好 home 之后迁移跑完了）、或者旧的已经
          不在了（刚被迁走，或者被容器删掉了）时改用新位置：在工作区目录里把旧的建回来的话，NA
          接着写进去的 `settings.json`（可能带平台 Key）、nk-ssh 私钥就留在 `/workspace` 里，新布局
          下再没人管它。
        - 新布局：`.nk-homes/<id>` 是 NA 自己的目录，收成 0700（不挂进任何容器，宿主上别的用户
          也读不到会话历史）；只有最后一级 `claude` 是 0777，并交给容器用户（`give_to_container`：
          它不在 `/workspace` 里，镜像入口脚本的 `chown` 管不到）。迁进来的老 home 里面的东西不动。

        与迁移并发时由调用方拿着 `home_lock`（迁移本身也拿着它）。
        """
        if home.legacy:
            new = WorkspaceService.new_claude_home(home.workspace_id)
            previous = None if safe_fs.kind(new.base, new.parts) == "dir" else safe_fs.kind(home.base, home.parts)
            if previous is None:
                return WorkspaceService.ensure_claude_home(new)
            if previous == "dir":
                try:
                    safe_fs.chmod(home.base, home.parts, 0o777)
                    return home
                except FileNotFoundError:
                    return WorkspaceService.ensure_claude_home(new)
                except safe_fs.UnsafePathError:
                    previous = "link"  # 看过之后又被换成了链接：照下面修
            safe_fs.ensure_dir(home.base, home.parts, replace_links=True)
            safe_fs.chmod(home.base, home.parts, 0o777)
            WorkspaceService._give_home_to_container(home)
            logger.warning(f"{home.base} 下的 {CLAUDE_HOME_DIRNAME} 不是普通目录（{previous}，可能是容器换的），已删掉重建")
            return home
        for depth in range(1, len(home.parts)):
            safe_fs.ensure_dir(home.base, home.parts[:depth], mode=0o700, replace_links=True)
            safe_fs.chmod(home.base, home.parts[:depth], 0o700)
        safe_fs.ensure_dir(home.base, home.parts, replace_links=True)
        safe_fs.chmod(home.base, home.parts, 0o777)
        WorkspaceService._give_home_to_container(home)
        return home

    @staticmethod
    def _give_home_to_container(home: ClaudeHome) -> None:
        """home 目录本身交给容器用户（只这一级；里面的东西各由写它的地方交）"""
        try:
            with safe_fs.open_dir(home.base, home.parts[:-1]) as parent:
                WorkspaceService.give_to_container(parent, home.parts[-1])
        except (FileNotFoundError, safe_fs.UnsafePathError) as e:
            logger.warning(f"工作区 {home.workspace_id} 的 CC home 没能交给容器用户: {e}")

    @staticmethod
    def give_to_container(directory: safe_fs.DirHandle, name: str, *, tree: bool = False) -> None:
        """把 NA 在 CC home 里建的、写的条目交给容器里的用户（uid / gid 1000），不跟随链接

        新布局的 home 不在 `/workspace` 里，镜像入口脚本只 `chown` 工作区，碰不到 `~/.claude`。NA
        （Docker 部署里是 root）建出来的 `skills/`、写出来的 `settings.json` 就都属 root，容器里的
        appuser 在 root 的 0755 `skills/` 下建不了 `skills/dynamic`，新建的工作区因此启动不起来。所以 NA
        每建一个目录、写一个文件，都在写完之后交给容器用户；旧布局一样处理（那里本来靠入口脚本
        `chown -R`，只在工作区对 appuser 不可写时才做）。权限位不动：home 本身 0777，私钥照旧 0600。

        - `tree`：连同里面的条目一起（技能目录是整棵复制进来的）；链接不改、不进去；
        - 不支持改属主的平台（Windows）什么都不做；
        - NA 不是 root、改不了属主时记一次 WARNING，之后不再提醒（容器里的 appuser 可能写不进这些
          目录）；条目被容器删掉或换掉、条目太多，记一条 WARNING，不影响调用方。
        """
        global _chown_refusal_reported
        if not safe_fs.CHOWN_SUPPORTED:
            return
        try:
            if tree:
                directory.chown_tree(name, CONTAINER_USER_UID, CONTAINER_USER_GID)
            else:
                directory.chown(name, CONTAINER_USER_UID, CONTAINER_USER_GID)
        except PermissionError as e:
            if not _chown_refusal_reported:
                _chown_refusal_reported = True
                logger.warning(
                    f"NA 改不了 CC home 里条目的属主（{e}）：NA 要以 root 运行（或者就是 uid {CONTAINER_USER_UID}），"
                    f"否则容器里的 appuser 可能写不进 NA 建的目录（例如 skills/）。这条只提醒一次",
                )
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"把 {directory.path / name} 交给容器用户失败: {type(e).__name__}: {e}")

    @staticmethod
    def remove_legacy_claude_settings(ws_dir: Path) -> bool:
        """删掉早先写在 `{ws}/.claude/settings.json` 的那份 CC 设置

        CC 从来不读它，但里面可能还留着以前写出去的凭据（`direct` 模式下的真实 Key、
        像凭据的 `extra_env`）。目录删空了顺手删掉，里面还有别的东西就留着。

        `.claude` 在容器可写的工作区目录里，删除不跟随符号链接：它被换成了链接时什么都不删
        （链接那头不是 NA 的东西），只记一条 WARNING。

        Returns:
            这次确实删掉了一个条目时为 True
        """
        legacy_dirname, legacy_filename = LEGACY_CLAUDE_SETTINGS_PARTS
        try:
            with safe_fs.open_dir(ws_dir, legacy_dirname) as legacy_dir:
                if legacy_dir.kind(legacy_filename) in (None, "dir"):
                    return False
                removed = legacy_dir.unlink(legacy_filename)
        except FileNotFoundError:
            return False  # 工作区或 .claude 不存在
        except safe_fs.UnsafePathError as e:
            logger.warning(f"工作区 {ws_dir.name} 的 .claude 不是普通目录（可能被容器换成了符号链接），没有清旧设置: {e}")
            return False
        if removed:
            with safe_fs.open_dir(ws_dir) as root:
                root.rmdir(legacy_dirname)  # 只删空的真目录
        return removed

    @staticmethod
    async def update_workspace_settings(
        workspace: "DBWorkspace",
        cc_preset: Optional["CCModelPresetItem"] = None,
    ) -> None:
        """更新工作区 settings.json 和 CC 用户级设置，使模型预设变更立即生效无需重建容器。

        CC sandbox 的 ClaudeRuntime 每次 spawn 子进程时动态读取 settings.json，
        因此写入新文件后下一条消息即可使用新的模型配置。

        **不轮换中继令牌**：这条路径不重启容器，轮换会让容器里正在跑的那一轮 LLM 调用当场 401。

        先重读这一行：调用方手里的可能是早先查出来的对象（预设同步一次查出所有工作区再逐个写）。
        工作区已经删了或正在删的不写——删除会在库里提交之后删掉它的 CC home，这时再写就会把
        `.nk-homes/<id>/claude/settings.json` 建回来，成了没人管的孤儿（工作区目录本身删除时保留，
        挡不住这一步）。
        """
        current = await DBWorkspace.get_or_none(id=workspace.id)
        if current is None or current.status == "deleting":
            return
        ws_dir = WorkspaceService.get_workspace_dir(workspace.id)
        if not ws_dir.is_dir():
            return  # 工作区目录尚未初始化，容器首次启动时 init_workspace_dir 会完整写入

        from nekro_agent.services.workspace.credential_policy import (
            mark_platform_secret_delivered,
            platform_credentials_allowed,
        )

        credential = await resolve_workspace_credential(workspace, cc_preset)
        if WorkspaceService._write_settings_files(
            ws_dir,
            cc_preset,
            credential,
            "热更新",
            allow_platform_secrets=await platform_credentials_allowed(workspace),
            home=WorkspaceService.claude_home(workspace.id),
        ):
            await mark_platform_secret_delivered(workspace)

    @staticmethod
    def get_effective_cc_preset_id(workspace: "DBWorkspace") -> Optional[int]:
        """获取工作区当前生效的 CC 模型预设 ID（未显式绑定时回退默认预设）。"""
        from nekro_agent.core.cc_model_presets import cc_presets_store

        raw_preset_id = (workspace.metadata or {}).get("cc_model_preset_id")
        if raw_preset_id is not None:
            try:
                return int(raw_preset_id)
            except (TypeError, ValueError):
                logger.warning(f"工作区 {workspace.id} 的 cc_model_preset_id 非法: {raw_preset_id!r}")
                return None

        default = cc_presets_store.get_default()
        return default.id if default else None

    @staticmethod
    async def sync_workspace_settings_for_preset(
        preset_id: int,
        cc_preset: "CCModelPresetItem",
    ) -> int:
        """将预设变更反向同步到所有引用该预设的工作区磁盘配置。"""
        workspaces = await DBWorkspace.all()
        synced_count = 0

        for workspace in workspaces:
            if WorkspaceService.get_effective_cc_preset_id(workspace) != preset_id:
                continue
            try:
                await WorkspaceService.update_workspace_settings(workspace, cc_preset)
                synced_count += 1
            except Exception as e:
                logger.warning(
                    f"同步工作区 CC 模型预设失败: workspace_id={workspace.id}, preset_id={preset_id}, error={e}"
                )

        logger.info(f"CC 模型预设 {preset_id} 已同步到 {synced_count} 个工作区")
        return synced_count

    @staticmethod
    def _parse_frontmatter(raw: str) -> "tuple[Dict[str, Any], str]":
        """解析 YAML frontmatter，返回 (meta_dict, body)。不依赖 pyyaml，使用行解析。"""
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                fm_text = parts[1].strip()
                body = parts[2].strip()
                meta: Dict[str, Any] = {}
                for line in fm_text.splitlines():
                    line = line.strip()
                    if ":" in line and not line.startswith("#"):
                        key, _, value = line.partition(":")
                        value = value.strip().strip('"').strip("'")
                        if value.startswith("[") and value.endswith("]"):
                            items = value[1:-1].split(",")
                            meta[key.strip()] = [i.strip().strip('"').strip("'") for i in items if i.strip()]
                        elif value.lower() == "true":
                            meta[key.strip()] = True
                        elif value.lower() == "false":
                            meta[key.strip()] = False
                        else:
                            meta[key.strip()] = value
                return meta, body
        return {}, raw

    # 记忆目录 `default/memory` 在容器可写的工作区里：下面几个函数都从工作区根逐级不跟随链接地打开，
    # 读到链接一律当作不存在（或拒绝写），不会顺着链接读出、写入或删掉宿主别处的文件。
    _MEMORY_REL = ("default", "memory")
    _MEMORY_MAX_READ_BYTES = 1024 * 1024

    @staticmethod
    def _memory_meta(rel: str, stem: str, raw: str) -> "Dict[str, Any]":
        m, _ = WorkspaceService._parse_frontmatter(raw)
        tags = m.get("tags", [])
        return {
            "path": rel,
            "title": m.get("title", stem),
            "category": m.get("category", ""),
            "tags": tags if isinstance(tags, list) else [],
            "shared": bool(m.get("shared", False)),
            "updated": str(m.get("updated", "")),
        }

    @staticmethod
    def list_memory_tree(workspace_id: int) -> "List[Dict[str, Any]]":
        """返回记忆目录树（仅包含 .md 文件和目录）。"""
        try:
            memory_dir = safe_fs.open_dir(
                WorkspaceService.get_workspace_dir(workspace_id),
                WorkspaceService._MEMORY_REL,
            )
        except (FileNotFoundError, safe_fs.UnsafePathError):
            return []

        # 记忆目录在容器可写的工作区里：嵌套超过 `MAX_TREE_DEPTH` 的那一层不再往下列
        # （列成空目录），不让一棵故意造得很深的树撞上递归上限
        def _build(directory: safe_fs.DirHandle, prefix: str, remaining_depth: int) -> "List[Dict[str, Any]]":
            nodes: List[Dict[str, Any]] = []
            entries = sorted(directory.entries(), key=lambda item: (item[1] == "file", item[0]))
            for name, kind in entries:
                rel = f"{prefix}{name}"
                if kind == "dir":
                    children: List[Dict[str, Any]] = []
                    if remaining_depth > 0:
                        try:
                            with directory.subdir(name) as child:
                                children = _build(child, f"{rel}/", remaining_depth - 1)
                        except (FileNotFoundError, safe_fs.UnsafePathError):
                            continue
                    nodes.append({"name": name, "type": "dir", "path": rel, "meta": None, "children": children})
                elif kind == "file" and name.endswith(".md"):
                    try:
                        raw = directory.read_text(
                            name,
                            max_bytes=WorkspaceService._MEMORY_MAX_READ_BYTES,
                            errors="replace",
                        ) or ""
                    except OSError:
                        raw = ""
                    nodes.append({
                        "name": name,
                        "type": "file",
                        "path": rel,
                        "meta": WorkspaceService._memory_meta(rel, name[:-3], raw),
                        "children": None,
                    })
            return nodes

        with memory_dir:
            return _build(memory_dir, "", safe_fs.MAX_TREE_DEPTH)

    @staticmethod
    def read_memory_file(workspace_id: int, rel_path: str) -> "Optional[Dict[str, Any]]":
        """读取记忆文件，返回 {path, raw, content, meta}，不存在则返回 None。"""
        try:
            parts = safe_fs.split_rel(rel_path)
            if not parts:
                return None
            raw = safe_fs.read_text(
                WorkspaceService.get_workspace_dir(workspace_id),
                (*WorkspaceService._MEMORY_REL, *parts),
                max_bytes=WorkspaceService._MEMORY_MAX_READ_BYTES,
                errors="replace",
            )
        except (safe_fs.UnsafePathError, IsADirectoryError, NotADirectoryError):
            return None
        if raw is None:
            return None
        _m, body = WorkspaceService._parse_frontmatter(raw)
        stem = parts[-1][:-3] if parts[-1].endswith(".md") else parts[-1]
        return {
            "path": rel_path,
            "raw": raw,
            "content": body,
            "meta": WorkspaceService._memory_meta(rel_path, stem, raw),
        }

    @staticmethod
    def write_memory_file(workspace_id: int, rel_path: str, raw: str) -> None:
        """创建或更新记忆文件，自动创建父目录。路径穿越或途中有链接时抛出 ValueError。"""
        try:
            parts = safe_fs.split_rel(rel_path)
            if not parts:
                raise safe_fs.UnsafePathError("空路径")
            safe_fs.write_text(
                WorkspaceService.get_workspace_dir(workspace_id),
                (*WorkspaceService._MEMORY_REL, *parts),
                raw,
            )
        except safe_fs.UnsafePathError as e:
            raise ValueError(f"非法路径: {rel_path}") from e

    @staticmethod
    def delete_memory_file(workspace_id: int, rel_path: str) -> bool:
        """删除记忆文件，返回是否成功。"""
        try:
            parts = safe_fs.split_rel(rel_path)
            if not parts:
                return False
            with safe_fs.open_dir(
                WorkspaceService.get_workspace_dir(workspace_id),
                (*WorkspaceService._MEMORY_REL, *parts[:-1]),
            ) as directory:
                if directory.kind(parts[-1]) != "file":
                    return False
                return directory.unlink(parts[-1])
        except (FileNotFoundError, safe_fs.UnsafePathError):
            return False

    @staticmethod
    def reset_memory(workspace_id: int) -> int:
        """清空工作区记忆目录下的所有 .md 文件（保留目录结构）。返回删除的文件数量。"""
        try:
            memory_dir = safe_fs.open_dir(
                WorkspaceService.get_workspace_dir(workspace_id),
                WorkspaceService._MEMORY_REL,
            )
        except (FileNotFoundError, safe_fs.UnsafePathError):
            return 0

        # 嵌套超过 `MAX_TREE_DEPTH` 的那一层不再往下走（只试着删它自己，不空就不删）
        def _clear(directory: safe_fs.DirHandle, remaining_depth: int) -> int:
            removed = 0
            for name, kind in directory.entries():
                if kind == "file" and name.endswith(".md"):
                    if directory.unlink(name):
                        removed += 1
                elif kind == "dir":
                    if remaining_depth > 0:
                        try:
                            with directory.subdir(name) as child:
                                removed += _clear(child, remaining_depth - 1)
                        except (FileNotFoundError, safe_fs.UnsafePathError):
                            continue
                    directory.rmdir(name)  # 只删空目录；不空或已被换成链接都不删
            return removed

        with memory_dir:
            return _clear(memory_dir, safe_fs.MAX_TREE_DEPTH)

    @staticmethod
    def read_na_context(workspace_id: int) -> "tuple[str, str]":
        """读取 _na_context.md 正文与更新时间，截取前 1800 字符（约 600 中文字）。

        结果会进每轮 NA 提示词与后台页面：文件或途中某一级被容器换成链接时当作没有，
        不会把链接指向的宿主文件读出来。

        Returns:
            (body, updated): 正文内容（最多1800字符）和 frontmatter 中的 updated 字段（可能为空字符串）
        """
        try:
            raw = safe_fs.read_text(
                WorkspaceService.get_workspace_dir(workspace_id),
                (*WorkspaceService._MEMORY_REL, "_na_context.md"),
                max_bytes=WorkspaceService._MEMORY_MAX_READ_BYTES,
                errors="replace",
            )
        except (safe_fs.UnsafePathError, IsADirectoryError, NotADirectoryError) as e:
            logger.warning(f"工作区 {workspace_id} 的 _na_context.md 不是普通文件，按没有处理: {e}")
            return "", ""
        if raw is None:
            return "", ""
        meta, body = WorkspaceService._parse_frontmatter(raw)
        return body[:1800], str(meta.get("updated", ""))

    @staticmethod
    def write_na_context(
        workspace_id: int,
        body: str,
        *,
        updated_by: str = "manual",
        title: str = "协作现状摘要",
    ) -> None:
        """写入 _na_context.md，自动生成最小 frontmatter。"""
        updated = datetime.now(timezone.utc).isoformat()
        frontmatter = "\n".join([
            "---",
            f'title: "{title}"',
            'category: "coordination"',
            f'updated: "{updated}"',
            f'updated_by: "{updated_by}"',
            "---",
            "",
        ])
        write_workspace_text(
            workspace_id,
            (*WorkspaceService._MEMORY_REL, "_na_context.md"),
            frontmatter + body.strip() + "\n",
        )

    @staticmethod
    def get_capability_summary(workspace: "DBWorkspace") -> str:
        """生成工作区 CC 能力摘要：选中 Skills 和 MCP 服务。

        ⚠️ **接它之前先给它一个作用域。** 目前全库 0 个调用方，所以它不构成活的泄露面；
        但下面那句 `list_all_skills()` 是**不带作用域**的，而 `metadata["skills"]` 是
        租户可写的任意字符串（`PUT /workspaces/{id}/skills`）。一旦有人把这个摘要接进
        提示词或后台响应，租户填一个平台方私有技能的名字就能换回它的 `description`，
        并借此逐个试出平台方库里有哪些技能。
        正确写法是 `list_all_skills(await workspace_skill_scope(workspace))`（函数需改成 async）。
        """
        lines: List[str] = []

        selected_skills: List[str] = workspace.metadata.get("skills", [])
        if selected_skills:
            all_skills_map: Dict[str, Any] = {
                s["name"]: s for s in WorkspaceService.list_all_skills()
            }
            skill_lines: List[str] = []
            for name in selected_skills:
                info = all_skills_map.get(name, {})
                desc = info.get("description", "")
                skill_lines.append(f"  - {name}: {desc}" if desc else f"  - {name}")
            lines.append("已部署 Skills:\n" + "\n".join(skill_lines))

        # 动态 Skills（CC 创建）
        dynamic_skills = WorkspaceService.list_dynamic_skills(workspace.id)
        if dynamic_skills:
            dynamic_lines = [f"  - {s['name']}: {s['description']}" if s["description"] else f"  - {s['name']}" for s in dynamic_skills]
            lines.append("动态 Skills（CC 创建）:\n" + "\n".join(dynamic_lines))

        # MCP 服务
        mcp_config = workspace.metadata.get("mcp_config", {})
        mcp_servers = mcp_config.get("mcpServers", {})
        if mcp_servers:
            lines.append(f"MCP 服务: {', '.join(mcp_servers.keys())}")

        resource_summary = str(workspace.metadata.get("resource_summary_cache") or "").strip()
        if resource_summary:
            lines.append(f"工作区资源:\n{resource_summary}")

        return "\n\n".join(lines) if lines else "（无扩展技能、MCP 服务或工作区资源）"

    @staticmethod
    async def init_workspace_dir(
        workspace: DBWorkspace,
        cc_preset: Optional["CCModelPresetItem"] = None,
        *,
        rotate_credential: bool = False,
        controller_address: str = "",
        host_address: str = "",
    ) -> None:
        """初始化 workspace 目录，写入 settings.json / .mcp.json / CC home 里的 settings.json 与 skills/（幂等）

        Args:
            workspace: 工作区记录
            cc_preset: 当前生效的 CC 模型预设
            rotate_credential: 是否重新签发中继令牌。只有会让容器重新拉起进程的路径
                （创建 / 重启）才置 True——见 `cc_credential.resolve_workspace_credential`
            controller_address: NA 在该工作区网络上的地址，用于推导容器视角的中继地址
            host_address: 该工作区网络的网关地址，NA 跑在宿主上时容器经它回连中继
        """
        ws_dir = WorkspaceService.get_workspace_dir(workspace.id)

        # 这里的文件系统操作一律放进线程：调用方（启动 / 重启）拿着工作区的锁，事件循环不能
        # 跟着卡住（容器能往目录里塞很多东西，删起来要一阵子）
        def _prepare() -> None:
            ws_dir.mkdir(parents=True, exist_ok=True)
            # 上次同步技能时删不完、挪进回收目录的东西：再清一次。回收目录在工作区之外、只有 NA
            # 往里放东西（见 `_discard_tree`）；还删不完就留着下次再来，不让初始化失败
            try:
                with WorkspaceService._open_trash_dir(workspace.id, create=False) as trash:
                    trash.purge_trash()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning(f"清理工作区 {workspace.id} 的回收目录失败（已忽略）: {e}")

        await asyncio.to_thread(_prepare)

        # 写进容器的凭据：默认是**只对本工作区有效的中继令牌**，真实上游 Key 不进容器
        credential = await resolve_workspace_credential(
            workspace,
            cc_preset,
            rotate=rotate_credential,
            controller_address=controller_address,
            host_address=host_address,
        )
        from nekro_agent.services.workspace.credential_policy import (
            mark_platform_secret_delivered,
            platform_credentials_allowed,
        )

        allow_platform_secrets = await platform_credentials_allowed(workspace)
        home = await asyncio.to_thread(WorkspaceService.claude_home, workspace.id)
        if await asyncio.to_thread(
            WorkspaceService._write_settings_files,
            ws_dir,
            cc_preset,
            credential,
            "写入",
            allow_platform_secrets=allow_platform_secrets,
            home=home,
        ):
            await mark_platform_secret_delivered(workspace)

        # 同步 skills 目录
        await WorkspaceService.sync_skills(workspace)

        def _ensure_dirs() -> None:
            # 创建 shared 目录（CC↔NA 文件共享目录，NA 实时感知）。
            # 建目录与 chmod 都逐级不跟随链接：容器能把 `default`、`shared` 换成指向宿主别处的链接，
            # 按路径 chmod 0777 会把链接指向的宿主目录改成人人可写。
            safe_fs.ensure_dir(ws_dir, ("default", "shared"), replace_links=True)
            # 确保容器内的 appuser 有写入权限（bind mount 跨越用户边界时宿主机 owner 可能不匹配）
            safe_fs.chmod(ws_dir, ("default", "shared"), 0o777)

            # 创建工作区知识库目录，供知识库源文件和规范化全文使用
            WorkspaceService.ensure_kb_dirs(workspace.id)

            # CC home（挂载到容器内 ~/.claude/，持久化 Claude Code 会话历史）。它还会被当作
            # bind 源挂进下一个容器：旧布局下是链接就删掉链接重建，绝不顺着它挂宿主别处
            WorkspaceService.ensure_claude_home(home)

        await asyncio.to_thread(_ensure_dirs)

        # 写入 .mcp.json 到 CC 工作目录（/workspace/default/.mcp.json）
        # 注意：与 CLAUDE.md 同理，必须放在 /workspace/default/ 下，CC 只在项目根目录查找
        await _write_disk_mcp_json(workspace)
        logger.debug(f"写入 .mcp.json: 工作区 {workspace.id}")

        # 写入 CLAUDE.md 到 CC 工作目录（/workspace/default/CLAUDE.md）
        # 注意：不能放在 /workspace/CLAUDE.md，否则当 /workspace/default/ 成为 git 根目录时
        # Claude Code 会停止向上遍历，导致 CLAUDE.md 被忽略
        from nekro_agent.core.config import config as app_config

        if app_config.SSH_GATEWAY_ENABLED:
            # `_generate_claude_md_content` 是同步的，凭据行得在这里先查好放进进程内缓存。
            # 闸关时不走这一段：一次库都不多查。
            try:
                from nekro_agent.services.ssh_gateway.container_files import refresh_ssh_servers_section

                await refresh_ssh_servers_section(workspace.id)
            except Exception as e:  # noqa: BLE001 - SSH 小节刷新失败不该拦住工作区初始化
                logger.warning(f"刷新工作区 {workspace.id} 的 SSH 服务器小节失败（已忽略）: {type(e).__name__}: {e}")
        claude_md = WorkspaceService._generate_claude_md_content(workspace)
        await asyncio.to_thread(write_workspace_text, workspace.id, ("default", "CLAUDE.md"), claude_md)
        logger.debug(f"写入 CLAUDE.md: 工作区 {workspace.id}")

    @staticmethod
    def scan_shared_dir(workspace_id: int, max_files: int = 10) -> list[dict[str, str]]:
        """扫描 CC 工作区共享目录，返回最近更新的文件列表。

        Args:
            workspace_id: 工作区 ID
            max_files: 最多返回的文件数量

        Returns:
            按修改时间倒序排列的文件信息列表，每项包含 name, size_human, mtime_str, rel_path。
        """
        # 共享目录是容器可写的：逐级不跟随链接地打开，只列普通文件，元数据取 `lstat`，
        # 链接一律跳过——否则链接指向的宿主文件名、大小、时间会进提示词
        try:
            shared_dir = safe_fs.open_dir(Path(WORKSPACE_ROOT_DIR) / str(workspace_id), ("default", "shared"))
        except (FileNotFoundError, safe_fs.UnsafePathError):
            return []

        files: list[tuple[float, int, str, str]] = []
        with shared_dir:
            for name, kind, st in shared_dir.entries_with_stat():
                if kind == "file":
                    files.append((st.st_mtime, st.st_size, name, name))
                elif kind == "dir":
                    # 浅层递归：只扫描 1 级子目录
                    try:
                        with shared_dir.subdir(name) as sub_dir:
                            for sub_name, sub_kind, sub_st in sub_dir.entries_with_stat():
                                if sub_kind == "file":
                                    files.append((sub_st.st_mtime, sub_st.st_size, sub_name, f"{name}/{sub_name}"))
                    except (FileNotFoundError, safe_fs.UnsafePathError):
                        continue

        # 按修改时间倒序
        files.sort(key=lambda x: x[0], reverse=True)

        result: list[dict[str, str]] = []
        for mtime, size, file_name, rel_path in files[:max_files]:
            if size < 1024:
                size_human = f"{size}B"
            elif size < 1024 * 1024:
                size_human = f"{size / 1024:.1f}KB"
            else:
                size_human = f"{size / (1024 * 1024):.1f}MB"

            mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc).astimezone()
            mtime_str = mtime_dt.strftime("%m-%d %H:%M")

            result.append({
                "name": file_name,
                "size_human": size_human,
                "mtime_str": mtime_str,
                "rel_path": rel_path,
            })
        return result

    @staticmethod
    def _read_skill_meta(skill_dir: Path) -> Optional[Dict[str, str]]:
        """从 SKILL.md frontmatter 读取 skill 元信息，不存在时返回 None。"""
        # 只用于技能库（宿主侧）的目录；工作区里的 skill 走 `_read_skill_md_in` + `_parse_skill_meta`
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            return None
        try:
            return WorkspaceService._parse_skill_meta(skill_md.read_text(encoding="utf-8"), skill_dir.name)
        except Exception as e:
            logger.warning(f"读取 SKILL.md 失败: {skill_dir.name}: {e}")
            return None

    @staticmethod
    def _parse_skill_meta(content: str, default_name: str) -> Dict[str, str]:
        """从 SKILL.md 正文的 frontmatter 取 name 与 description"""
        meta: Dict[str, str] = {}
        in_frontmatter = False
        for i, line in enumerate(content.splitlines()):
            if i == 0 and line.strip() == "---":
                in_frontmatter = True
                continue
            if in_frontmatter and line.strip() == "---":
                break
            if in_frontmatter and ":" in line and not line.startswith("#"):
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip().strip('"').strip("'")
        return {
            "name": meta.get("name", default_name),
            "description": meta.get("description", ""),
        }

    @staticmethod
    def _resolve_skill_source(skill_id: str, tenant_id: Optional[int] = None) -> "tuple[Optional[Path], str]":
        """根据 skill_id 解析源目录和类别。

        Args:
            skill_id: 技能标识（`skill` 或 `repo/skill`）
            tenant_id: 解析所处的技能库作用域；`None` 为平台方（也是未启用多租户时的唯一分支）

        Returns:
            (source_dir, category): source_dir 为 None 表示未找到或对该作用域不可见;
            category 为 "builtin"/"user"/"repo"

        作用域不可见与不存在**返回同一个结果**：这条链的下游是「复制技能目录进工作区」，
        区分两者不但没用，还会让日志把平台方私有技能的名字漏给租户的运维。
        """
        from nekro_agent.services import skill_scope

        # 标识形状决定去哪一族根里找：`repo/skill` 只认仓库根，扁平标识只认非仓库根。
        # 不加这一条的话 `my-skill/scripts` 会命中 `local/my-skill/` 下的子目录，
        # 把技能内部的一个子目录当成一个技能同步进工作区。
        nested = "/" in skill_id
        roots = [
            root
            for root in skill_scope.readable_roots(tenant_id)
            if (root.kind == skill_scope.REPOS_KIND) == nested
        ]
        resolved = skill_scope.resolve_dir(tenant_id, skill_id, roots=roots)
        if resolved is None:
            # 类别只用于日志与 `.skill-origin.json`，取不到目录时按标识形状给一个
            return None, "repo" if "/" in skill_id else "user"
        if resolved.ownership is skill_scope.SkillOwnership.BUILTIN:
            return resolved.path, "builtin"
        return resolved.path, "repo" if resolved.root.kind == skill_scope.REPOS_KIND else "user"

    @staticmethod
    async def sync_skills(workspace: DBWorkspace) -> None:
        """将 metadata['skills'] 中选中的技能同步到 CC home 的 skills/（容器里 ~/.claude/skills/）。

        Claude Code only recognizes flat skill directories under ~/.claude/skills/<skill-name>/SKILL.md.
        Workspace-local dynamic skills are also stored in this same directory. A skill without
        .skill-origin.json is treated as a workspace-local dynamic skill.

        ## 这里是技能泄露的最后一道闸

        `metadata["skills"]` 是一串**任意字符串**：`PUT /workspaces/{id}/skills` 是
        `Role.Admin`，租户管理员填什么都收得下。改前这条链把标识直接拿去全局库里找目录，
        于是租户填一个平台方私有技能的名字，它的正文就被复制进租户的工作区，
        由容器里的 Claude Code 逐字读走 —— 比读接口那条路更直接，且完全不经过路由层。

        所以同步这一步必须自己按**工作区的归属**重新解析一次，不能相信选中列表。
        解析不到（含"对该作用域不可见"）就跳过，并且**连带把已经落在目录里的那一份删掉** ——
        工作区可能是先由平台方建好、选好技能，之后才绑定到租户频道上的。
        """
        from nekro_agent.services.skill_scope import workspace_skill_scope

        scope = await workspace_skill_scope(workspace)

        # CC home 是 0777、整个挂进容器：`skills` 都可能被换成链接（旧布局下 `.claude_home` 本身也会），
        # 指到技能库或宿主别处。逐级不跟随地打开，删用 `remove_tree`（链接只删链接本身）。
        # 这是启动路径：删不完的挪进工作区之外的回收目录，不让启动失败（见 `_discard_tree`）。
        # 整段文件系统操作放进线程，调用方拿着工作区的锁，事件循环不跟着卡住
        def _sync() -> None:
            skills_home = WorkspaceService._open_workspace_skills(workspace.id, create=True)
            assert skills_home is not None
            with skills_home, WorkspaceService._open_trash_dir(workspace.id, create=True) as trash:
                WorkspaceService._sync_skills_in(workspace, scope, skills_home, trash=trash)

        await asyncio.to_thread(_sync)

    @staticmethod
    def _open_trash_dir(workspace_id: int, *, create: bool) -> safe_fs.DirHandle:
        """这个工作区的回收目录：`{工作区根}/.nk-trash/<id>`，在工作区之外、容器写不到

        同步技能时删不完的树整体改名进这里，下次初始化再清（`purge_trash`）。它不在
        `/workspace` 挂载里，所以里面的条目只可能是 NA 放进去的——清的时候不必提防容器
        在里面起名、塞东西。改名是同一文件系统里的 `renameat`（工作区目录就在工作区根下）。

        Raises:
            FileNotFoundError: `create=False` 且还没有回收目录
        """
        root = WorkspaceService.get_workspace_dir(workspace_id).parent
        rel = (".nk-trash", str(workspace_id))
        if create:
            safe_fs.ensure_dir(root, rel, mode=0o700)
        return safe_fs.open_dir(root, rel)

    @staticmethod
    def _sync_skills_in(
        workspace: DBWorkspace,
        scope: Optional[int],
        skills_home: safe_fs.DirHandle,
        *,
        trash: Optional[safe_fs.DirHandle] = None,
    ) -> None:
        # 旧结构已废弃：~/.claude/skills/{builtin,user,dynamic}/
        for legacy_dir_name in ("builtin", "user", "dynamic"):
            if skills_home.kind(legacy_dir_name) == "dir":
                WorkspaceService._discard_tree(skills_home, legacy_dir_name, trash)
                logger.debug(f"清理旧版 skills 分层目录: {legacy_dir_name}")

        Path(SKILLS_LOCAL_DIR).mkdir(parents=True, exist_ok=True)
        Path(SKILLS_REPOS_DIR).mkdir(parents=True, exist_ok=True)

        selected_skills: List[str] = workspace.metadata.get("skills", [])
        # 先按工作区归属解析一遍，**解析结果**（而不是选中列表）才是这个工作区真正该有的技能。
        resolved_sources: List["tuple[str, Path, str]"] = []
        for skill_id in selected_skills:
            src, cat = WorkspaceService._resolve_skill_source(skill_id, scope)
            if src is None or not src.is_dir():
                logger.warning(f"skill 不存在或对本工作区的作用域不可见，已跳过: {skill_id}")
                continue
            resolved_sources.append((skill_id, src, cat))
        selected_names = {skill_id.split("/")[-1] for skill_id, _, _ in resolved_sources}

        # 清理未选中的“受管理 skill”；未标记技能视为 workspace-local dynamic skill，保留。
        # 判据用的是**解析后**的名字集合：一条选中但已不可见的技能落在这里会被删掉，
        # 这正是"工作区先建后绑租户"那条时序的收口。
        for name, kind in skills_home.entries():
            if kind != "dir" or name.startswith("."):
                continue
            origin = WorkspaceService._skill_origin_in(skills_home, name)
            if not origin:
                continue
            if name not in selected_names:
                WorkspaceService._discard_tree(skills_home, name, trash)
                logger.debug(f"清理未选中的受管理 skill: {name}")

        # 复制/更新选中的 skill
        for skill_id, src, cat in resolved_sources:
            target_name = skill_id.split("/")[-1] if "/" in skill_id else skill_id
            if WorkspaceService._sync_skill_into(
                skills_home, src, target_name, category=cat, skill_id=skill_id, trash=trash,
            ):
                logger.debug(f"同步 skill: {skill_id} → {target_name}")

    @staticmethod
    async def list_mcp_servers(workspace: DBWorkspace) -> list[Any]:
        """返回工作区已加入的 MCP 服务器列表

        - 数据源：全局 MCP 库（data/configs/auto-inject-mcp.json）
        - 仅返回 workspace.metadata.mcp_servers_enabled 中引用的条目（即"已加入"集合）
        - 老数据迁移：metadata.mcp_config.mcpServers 中的本地条目会被自动推入全局
          库并加入启用集
        """
        return await WorkspaceService._list_mcp_servers_filtered(workspace, only_enabled=True)

    @staticmethod
    async def list_available_mcp_servers(workspace: DBWorkspace) -> list[Any]:
        """返回工作区可加入但尚未加入的 MCP 服务器（供"添加 MCP 服务"对话框使用）"""
        return await WorkspaceService._list_mcp_servers_filtered(workspace, only_enabled=False)

    @staticmethod
    async def _list_mcp_servers_filtered(workspace: DBWorkspace, only_enabled: bool) -> list[Any]:
        from nekro_agent.services.mcp.schemas import McpServerConfig, McpServerType, McpValidationState

        await WorkspaceService._migrate_legacy_mcp_if_needed(workspace)

        library = _global_library_by_name()
        enabled_set = set(((workspace.metadata or {}).get("mcp_servers_enabled") or []))

        result: list[McpServerConfig] = []
        for name, raw in library.items():
            is_enabled = name in enabled_set
            if only_enabled and not is_enabled:
                continue
            if (not only_enabled) and is_enabled:
                continue
            transport = raw.get("type") or raw.get("transport")
            if transport == "sse":
                server_type = McpServerType.sse
            elif transport == "http":
                server_type = McpServerType.http
            else:
                server_type = McpServerType.stdio
            try:
                validation_raw = raw.get("validation")
                validation = (
                    McpValidationState.model_validate(validation_raw)
                    if isinstance(validation_raw, dict)
                    else McpValidationState(status="unvalidated")
                )
                auto_inject = bool(raw.get("auto_inject", raw.get("enabled", False)))
                result.append(
                    McpServerConfig(
                        id=raw.get("id"),
                        name=name,
                        type=server_type,
                        auto_inject=auto_inject,
                        enabled=is_enabled,
                        command=raw.get("command"),
                        args=raw.get("args", []),
                        env=raw.get("env", {}),
                        url=raw.get("url"),
                        headers=raw.get("headers", {}),
                        validation=validation,
                    ),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"忽略无效的全局 MCP 配置 {name}: {e}")
        return result

    @staticmethod
    async def _migrate_legacy_mcp_if_needed(workspace: DBWorkspace) -> None:
        """把工作区 metadata.mcp_config.mcpServers 里残留的本地条目搬到全局库

        幂等。同名冲突一律保留全局库现有条目（不覆盖），仅记录警告。迁移完成后
        清空 mcp_config 老字段，写入 mcp_servers_enabled 新字段。
        """
        from nekro_agent.core.auto_inject_mcp import (
            get_auto_inject_mcp_servers,
            set_auto_inject_mcp_servers,
        )

        legacy = ((workspace.metadata or {}).get("mcp_config") or {}).get("mcpServers") or {}
        if not legacy:
            return

        async with _get_mcp_lock(workspace.id):
            metadata = dict(workspace.metadata or {})
            legacy = (metadata.get("mcp_config") or {}).get("mcpServers") or {}
            if not legacy:
                return

            library_list = get_auto_inject_mcp_servers()
            library_names = {(e or {}).get("name") for e in library_list if isinstance(e, dict)}

            enabled_names: List[str] = list(metadata.get("mcp_servers_enabled") or [])
            mutated_library = False
            for name, cfg in legacy.items():
                if not isinstance(cfg, dict):
                    continue
                if name in library_names:
                    logger.warning(
                        f"工作区 {workspace.id} 老 MCP 条目 {name} 与全局库同名，跳过推送，工作区将使用全局库配置",
                    )
                else:
                    transport = cfg.get("transport") or ("stdio" if cfg.get("command") else "http")
                    entry: Dict[str, Any] = {
                        "name": name,
                        "type": transport,
                        "auto_inject": False,
                    }
                    for key in ("command", "args", "env", "url", "headers", "validation"):
                        if key in cfg:
                            entry[key] = cfg[key]
                    library_list.append(entry)
                    library_names.add(name)
                    mutated_library = True
                if name not in enabled_names:
                    enabled_names.append(name)

            metadata["mcp_servers_enabled"] = enabled_names
            mcp_config = dict(metadata.get("mcp_config") or {})
            mcp_config["mcpServers"] = {}
            metadata["mcp_config"] = mcp_config

            if mutated_library:
                set_auto_inject_mcp_servers(library_list)

            workspace.metadata = metadata
            await workspace.save(update_fields=["metadata", "update_time"])
            await WorkspaceService._write_mcp_disk_locked(workspace)
            logger.info(
                f"工作区 {workspace.id} 已迁移 {len(legacy)} 个本地 MCP 条目到全局库，"
                f"启用清单: {enabled_names}",
            )

    @staticmethod
    async def set_mcp_server_enabled(workspace: DBWorkspace, server_name: str, enabled: bool) -> None:
        """在当前工作区启用/停用某个全局 MCP 服务器"""
        async with _get_mcp_lock(workspace.id):
            metadata = dict(workspace.metadata or {})
            enabled_names: List[str] = list(metadata.get("mcp_servers_enabled") or [])
            if enabled and server_name not in enabled_names:
                enabled_names.append(server_name)
            elif not enabled and server_name in enabled_names:
                enabled_names = [n for n in enabled_names if n != server_name]
            else:
                return
            metadata["mcp_servers_enabled"] = enabled_names
            workspace.metadata = metadata
            await workspace.save(update_fields=["metadata", "update_time"])
            await WorkspaceService._write_mcp_disk_locked(workspace)

    @staticmethod
    async def _write_mcp_disk_locked(workspace: DBWorkspace) -> None:
        """在持锁状态下重写 .mcp.json（逐级不跟随链接，见 `_write_disk_mcp_json`）"""
        await _write_disk_mcp_json(workspace)

    @staticmethod
    async def sync_mcp_to_disk(workspace: DBWorkspace) -> None:
        """显式触发 .mcp.json 重写（路由 /mcp/sync 调用）"""
        async with _get_mcp_lock(workspace.id):
            await WorkspaceService._write_mcp_disk_locked(workspace)

    @staticmethod
    async def apply_auto_inject(workspace: DBWorkspace) -> List[str]:
        """新建工作区时根据全局库 auto_inject 标志填充 enabled 列表，返回新追加的 name 列表"""
        from nekro_agent.core.auto_inject_mcp import get_auto_inject_mcp_servers

        added: List[str] = []
        async with _get_mcp_lock(workspace.id):
            metadata = dict(workspace.metadata or {})
            enabled_names: List[str] = list(metadata.get("mcp_servers_enabled") or [])
            for raw in get_auto_inject_mcp_servers():
                if not isinstance(raw, dict):
                    continue
                name = raw.get("name")
                if not name:
                    continue
                # 兼容旧字段 enabled
                if not bool(raw.get("auto_inject", raw.get("enabled", False))):
                    continue
                if name in enabled_names:
                    continue
                enabled_names.append(name)
                added.append(name)

            if added:
                metadata["mcp_servers_enabled"] = enabled_names
                workspace.metadata = metadata
                await workspace.save(update_fields=["metadata", "update_time"])
                await WorkspaceService._write_mcp_disk_locked(workspace)
        return added

    @staticmethod
    async def mark_global_server_validation(
        server_name: str,
        validation_state: Dict[str, Any],
    ) -> None:
        """更新全局库中某个 server 的 validation 状态，并刷新所有引用它的工作区 .mcp.json"""
        from nekro_agent.core.auto_inject_mcp import update_auto_inject_validation

        update_auto_inject_validation(server_name, validation_state)
        await WorkspaceService.refresh_global_mcp_server_references(server_name)

    @staticmethod
    async def refresh_global_mcp_server_references(server_name: str) -> None:
        """重写所有引用了指定全局 MCP server 的工作区 `.mcp.json`

        「引用」按 `metadata.mcp_servers_enabled` 里的名字匹配。单个工作区失败只记警告：
        一个坏工作区不该让整批刷新（例如启动时的令牌轮换）半途而废。
        """
        affected = await DBWorkspace.filter(metadata__contains={"mcp_servers_enabled": [server_name]}).all()
        for ws in affected:
            try:
                async with _get_mcp_lock(ws.id):
                    await WorkspaceService._write_mcp_disk_locked(ws)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"刷新工作区 {ws.id} 的 .mcp.json 失败: {e}")

    @staticmethod
    async def bind_channel(workspace: DBWorkspace, chat_key: str) -> None:
        """将频道绑定到工作区

        频道若归租户实例，工作区在这一刻就变成了非平台方归属：先给它打上「曾经不归平台方」
        的标记再改库。标记写不进去就拒绝这次绑定——否则归属已经变了、令牌却还在，事后补不回。
        """
        from nekro_agent.services.mcp.web_chat_workspace_taint import (
            instance_owner_tenant,
            taint_workspace_if_non_platform,
            taint_workspaces,
        )

        channel = await DBChatChannel.filter(chat_key=chat_key).first()
        if channel is None:
            from nekro_agent.schemas.errors import NotFoundError

            raise NotFoundError(resource=f"频道 {chat_key}")
        tenant_id = await instance_owner_tenant(channel.adapter_key, channel.instance_key or "")
        if tenant_id is not None:
            taint_workspaces([workspace.id], reason=f"绑定租户频道 {chat_key}", refuse_on_failure=True)
        channel.workspace_id = workspace.id
        await channel.save(update_fields=["workspace_id", "update_time"])
        # 改库后按整个工作区再推导一次：查实例归属与改库之间，这个实例可能刚被划给租户
        # （划转那一侧的置位只看得到改库前已经绑上的频道），两边都会错过这一次
        await taint_workspace_if_non_platform(workspace, reason=f"绑定频道 {chat_key} 后复核")

    @staticmethod
    async def unbind_channel(workspace: DBWorkspace, chat_key: str) -> None:
        """解除频道的工作区绑定，并同步清理工作区侧的频道注解。"""
        channel = await DBChatChannel.filter(chat_key=chat_key).first()
        if channel is None:
            from nekro_agent.schemas.errors import NotFoundError

            raise NotFoundError(resource=f"频道 {chat_key}")
        channel.workspace_id = None  # type: ignore[assignment]
        await channel.save(update_fields=["workspace_id", "update_time"])
        # 同步清理工作区侧注解
        await WorkspaceService.remove_channel_annotation(workspace, chat_key)

    @staticmethod
    async def get_bound_channels(workspace_id: int) -> List[DBChatChannel]:
        """获取绑定到工作区的所有频道"""
        return await DBChatChannel.filter(workspace_id=workspace_id).all()

    # ── 频道注解管理（存储于 metadata.channel_annotations） ───────────────────

    @staticmethod
    def get_channel_annotations(workspace: DBWorkspace) -> Dict[str, "ChannelAnnotationData"]:
        """获取工作区所有频道注解，返回以 chat_key 为键的字典。"""
        raw: Dict[str, Any] = workspace.metadata.get("channel_annotations", {})
        result: Dict[str, ChannelAnnotationData] = {}
        for chat_key, data in raw.items():
            if isinstance(data, dict):
                result[chat_key] = ChannelAnnotationData(
                    description=str(data.get("description", "")),
                    is_primary=bool(data.get("is_primary", False)),
                )
        return result

    @staticmethod
    def get_primary_channel_chat_key(workspace: DBWorkspace, bound_chat_keys: List[str]) -> Optional[str]:
        """推断主频道 chat_key。
        - 只有一个绑定频道时，直接返回它（无需显式设置）
        - 多个频道时，从 annotations 中找 is_primary=True 的
        """
        if not bound_chat_keys:
            return None
        if len(bound_chat_keys) == 1:
            return bound_chat_keys[0]
        annotations = WorkspaceService.get_channel_annotations(workspace)
        for chat_key in bound_chat_keys:
            ann = annotations.get(chat_key)
            if ann and ann.is_primary:
                return chat_key
        return None

    @staticmethod
    async def update_channel_annotation(
        workspace: DBWorkspace,
        chat_key: str,
        description: str,
        is_primary: bool,
    ) -> None:
        """更新单个频道的注解。若 is_primary=True，自动清除其他频道的主频道标记。"""
        metadata = dict(workspace.metadata)
        annotations: Dict[str, Any] = dict(metadata.get("channel_annotations", {}))

        if is_primary:
            # 清除其他频道的 is_primary
            for key in annotations:
                if key != chat_key and annotations[key].get("is_primary"):
                    annotations[key] = dict(annotations[key])
                    annotations[key]["is_primary"] = False

        annotations[chat_key] = {
            "description": description,
            "is_primary": is_primary,
        }

        metadata["channel_annotations"] = annotations
        workspace.metadata = metadata
        await workspace.save(update_fields=["metadata", "update_time"])

    @staticmethod
    async def remove_channel_annotation(workspace: DBWorkspace, chat_key: str) -> None:
        """删除频道注解（解绑时调用）。若被删除的是主频道且还有其他频道，自动将剩余第一个设为主频道。"""
        metadata = dict(workspace.metadata)
        annotations: Dict[str, Any] = dict(metadata.get("channel_annotations", {}))

        was_primary = annotations.get(chat_key, {}).get("is_primary", False)
        annotations.pop(chat_key, None)

        # 若删除的是主频道，且还有其他注解，将第一个设为主频道
        if was_primary and annotations:
            first_key = next(iter(annotations))
            annotations[first_key] = dict(annotations[first_key])
            annotations[first_key]["is_primary"] = True

        metadata["channel_annotations"] = annotations
        workspace.metadata = metadata
        await workspace.save(update_fields=["metadata", "update_time"])

    @staticmethod
    def get_workspace_skills_dir(workspace_id: int) -> Path:
        return WorkspaceService.claude_home(workspace_id).path / "skills"

    @staticmethod
    def _valid_skill_dir_name(dir_name: str) -> bool:
        try:
            return len(safe_fs.split_rel([dir_name])) == 1 and not dir_name.startswith(".")
        except safe_fs.UnsafePathError:
            return False

    @staticmethod
    def is_dynamic_skill(workspace_id: int, dir_name: str) -> bool:
        """`dir_name` 是不是这个工作区的动态 skill（真目录、不是链接、有 SKILL.md、没被技能库接管）

        目录本身是软链时一律不认：容器里一句 `ln -s {DATA_DIR}/skills/local/ops-runbook mine`
        不能让这条判据点头。
        """
        if not WorkspaceService._valid_skill_dir_name(dir_name):
            return False
        skills = WorkspaceService._open_workspace_skills(workspace_id)
        if skills is None:
            return False
        with skills:
            return WorkspaceService._is_dynamic_skill_in(skills, dir_name)

    @staticmethod
    def list_dynamic_skills(workspace_id: int) -> List[Dict[str, str]]:
        """列出工作区内所有未固化的 workspace-local dynamic skills。"""
        skills_root = WorkspaceService._open_workspace_skills(workspace_id)
        if skills_root is None:
            return []
        skills: List[Dict[str, str]] = []
        with skills_root:
            for name, _kind in skills_root.entries():
                if not WorkspaceService._is_dynamic_skill_in(skills_root, name):
                    continue
                content = WorkspaceService._read_skill_md_in(skills_root, name)
                meta = WorkspaceService._parse_skill_meta(content, name) if content is not None else None
                skills.append(
                    {
                        "dir_name": name,
                        "name": meta["name"] if meta else name,
                        "description": meta["description"] if meta else "",
                    }
                )
        return skills

    @staticmethod
    def read_dynamic_skill(workspace_id: int, dir_name: str) -> Optional[str]:
        """读取 workspace-local dynamic skill 的 SKILL.md 内容，不存在时返回 None。"""
        if not WorkspaceService._valid_skill_dir_name(dir_name):
            return None
        skills = WorkspaceService._open_workspace_skills(workspace_id)
        if skills is None:
            return None
        with skills:
            if not WorkspaceService._is_dynamic_skill_in(skills, dir_name):
                return None
            return WorkspaceService._read_skill_md_in(skills, dir_name)

    @staticmethod
    def write_dynamic_skill(workspace_id: int, dir_name: str, content: str) -> None:
        """创建或更新 workspace-local dynamic skill（写入 SKILL.md）。"""
        if not WorkspaceService._valid_skill_dir_name(dir_name):
            raise ValueError(f"技能目录名不合法: {dir_name}")
        skills = WorkspaceService._open_workspace_skills(workspace_id, create=True)
        assert skills is not None
        with skills:
            existing = skills.kind(dir_name)
            if existing == "dir" and not WorkspaceService._is_dynamic_skill_in(skills, dir_name):
                raise ValueError(f"技能 '{dir_name}' 已被受管理 skill 占用")
            if existing not in (None, "dir"):
                raise ValueError(f"技能 '{dir_name}' 不是普通目录（可能被换成了链接），请在沙盒里删掉后重试")
            with skills.subdir(dir_name, create=True) as skill_dir:
                skill_dir.write_text("SKILL.md", content)
            WorkspaceService.give_to_container(skills, dir_name, tree=True)

    @staticmethod
    def list_dynamic_skill_tree(workspace_id: int, dir_name: str, *, max_depth: int = 5) -> Optional[List[Dict[str, Any]]]:
        """列出动态 skill 目录里的文件与子目录；不是动态 skill 返回 None

        隐藏条目（`.git` 等）与**所有**链接都跳过：这个目录容器可写，链接指向哪儿都不该被走进去，
        连链接的名字也不列（列出来就等于告诉对方链接那头有什么）。
        """
        if not WorkspaceService._valid_skill_dir_name(dir_name):
            return None
        skills = WorkspaceService._open_workspace_skills(workspace_id)
        if skills is None:
            return None

        def _walk(directory: safe_fs.DirHandle, prefix: str, depth: int) -> List[Dict[str, Any]]:
            if depth <= 0:
                return []
            result: List[Dict[str, Any]] = []
            items = sorted(directory.entries_with_stat(), key=lambda item: (item[1] != "dir", item[0].lower()))
            for name, kind, st in items:
                if name.startswith("."):
                    continue
                rel = f"{prefix}{name}"
                if kind == "dir":
                    result.append({"name": name, "rel_path": rel, "type": "dir", "size": None})
                    try:
                        with directory.subdir(name) as child:
                            result.extend(_walk(child, f"{rel}/", depth - 1))
                    except (FileNotFoundError, safe_fs.UnsafePathError):
                        continue
                elif kind == "file":
                    result.append({"name": name, "rel_path": rel, "type": "file", "size": int(st.st_size)})
            return result

        with skills:
            if not WorkspaceService._is_dynamic_skill_in(skills, dir_name):
                return None
            try:
                with skills.subdir(dir_name) as skill_dir:
                    return _walk(skill_dir, "", max_depth)
            except (FileNotFoundError, safe_fs.UnsafePathError):
                return None

    @staticmethod
    def read_dynamic_skill_file(workspace_id: int, dir_name: str, rel_path: str) -> Optional[str]:
        """读动态 skill 目录里的一个文件；不是动态 skill、路径不合法、途中有链接、不是普通文件都返回 None"""
        if not WorkspaceService._valid_skill_dir_name(dir_name):
            return None
        try:
            parts = safe_fs.split_rel(rel_path)
        except safe_fs.UnsafePathError:
            return None
        if not parts:
            return None
        skills = WorkspaceService._open_workspace_skills(workspace_id)
        if skills is None:
            return None
        with skills:
            if not WorkspaceService._is_dynamic_skill_in(skills, dir_name):
                return None
            try:
                with skills.subdir(dir_name) as skill_dir:
                    current = skill_dir
                    opened: List[safe_fs.DirHandle] = []
                    try:
                        for part in parts[:-1]:
                            current = current.subdir(part)
                            opened.append(current)
                        return current.read_text(
                            parts[-1],
                            max_bytes=WorkspaceService._SKILL_FILE_MAX_BYTES,
                            errors="replace",
                        )
                    finally:
                        for handle in reversed(opened):
                            handle.close()
            except (FileNotFoundError, safe_fs.UnsafePathError):
                return None

    @staticmethod
    def write_dynamic_skill_file(workspace_id: int, dir_name: str, rel_path: str, content: str) -> None:
        """写动态 skill 目录里的一个文件（缺的父目录建出来）

        Raises:
            LookupError: 不是动态 skill
            ValueError: 路径不合法，或途中某一级是链接 / 文件
        """
        if not WorkspaceService._valid_skill_dir_name(dir_name):
            raise LookupError(dir_name)
        try:
            parts = safe_fs.split_rel(rel_path)
        except safe_fs.UnsafePathError as e:
            raise ValueError("路径不合法") from e
        if not parts:
            raise ValueError("路径不合法")
        skills = WorkspaceService._open_workspace_skills(workspace_id)
        if skills is None:
            raise LookupError(dir_name)
        with skills:
            if not WorkspaceService._is_dynamic_skill_in(skills, dir_name):
                raise LookupError(dir_name)
            opened: List[safe_fs.DirHandle] = []
            try:
                current = skills.subdir(dir_name)
                opened.append(current)
                for part in parts[:-1]:
                    current = current.subdir(part, create=True)
                    opened.append(current)
                if current.kind(parts[-1]) not in (None, "file"):
                    raise ValueError("路径不合法")
                current.write_text(parts[-1], content)
            except safe_fs.UnsafePathError as e:
                raise ValueError("路径不合法") from e
            finally:
                for handle in reversed(opened):
                    handle.close()
            WorkspaceService.give_to_container(skills, dir_name, tree=True)

    @staticmethod
    def delete_dynamic_skill(workspace_id: int, dir_name: str) -> bool:
        """删除 workspace-local dynamic skill，返回是否成功。"""
        if not WorkspaceService._valid_skill_dir_name(dir_name):
            return False
        skills = WorkspaceService._open_workspace_skills(workspace_id)
        if skills is None:
            return False
        with skills:
            if not WorkspaceService._is_dynamic_skill_in(skills, dir_name):
                return False
            return skills.remove_tree(dir_name)

    @staticmethod
    def promote_dynamic_skill(
        workspace_id: int,
        dir_name: str,
        *,
        force: bool = False,
        tenant_id: Optional[int] = None,
    ) -> str:
        """将动态 skill 晋升为该作用域的技能库（平台方进 `local/`，租户进自己那一层）。

        Args:
            force: 为 True 时覆盖已有同名技能，否则已存在时抛出 ConflictError。
            tenant_id: 晋升进哪个作用域的库；`None` 为平台方，也是未启用多租户时的唯一分支。

        Returns:
            目录名

        Raises:
            ValueError: 源不存在或一致性校验失败
            ConflictError: 目标已存在且 force=False

        落点走 `skill_scope.resolve_writable_dir()` 这一个出口，因此租户的晋升**结构上**
        不可能写到平台方目录里，不依赖调用点记得判一次。

        ⚠️ 落点判定只管「写到哪儿去」，**不管「从哪儿读」**：源目录是容器可写的。
        所以源一侧逐级不跟随链接地打开（旧布局的 `.claude_home`、`skills`、技能目录本身被换成链接都不认），
        内含链接的直接拒绝晋升；真正复制时用 `copy_tree_out`，判完之后才冒出来的链接也只会被跳过，
        不会把链接指向的宿主文件带进技能库。
        """
        if not WorkspaceService._valid_skill_dir_name(dir_name):
            raise ValueError(f"动态 skill 不存在: {dir_name}")
        skills = WorkspaceService._open_workspace_skills(workspace_id)
        if skills is None:
            raise ValueError(f"动态 skill 不存在: {dir_name}")
        with skills:
            WorkspaceService._promote_from(skills, workspace_id, dir_name, force=force, tenant_id=tenant_id)
        return dir_name

    @staticmethod
    def _promote_from(
        skills: safe_fs.DirHandle,
        workspace_id: int,
        dir_name: str,
        *,
        force: bool,
        tenant_id: Optional[int],
    ) -> None:
        from nekro_agent.services.skill_scope import resolve_writable_dir

        if not WorkspaceService._is_dynamic_skill_in(skills, dir_name):
            raise ValueError(f"动态 skill 不存在: {dir_name}")
        with skills.subdir(dir_name) as src_dir:
            if WorkspaceService._tree_has_link(src_dir):
                raise ValueError(
                    f"动态 skill '{dir_name}' 内含符号链接（或目录嵌套超过 {safe_fs.MAX_TREE_DEPTH} 层），"
                    f"无法晋升：技能库里的每个文件都必须是这个 skill 自己的内容。"
                    f"请在沙盒里把链接换成真实文件、把目录层级收浅后重试",
                )

        # 一致性校验：frontmatter name 必须与 dir_name 一致
        content = WorkspaceService._read_skill_md_in(skills, dir_name)
        meta = WorkspaceService._parse_skill_meta(content, dir_name) if content is not None else None
        if meta:
            fm_name = meta.get("name", "")
            if fm_name and fm_name != dir_name:
                raise ValueError(
                    f"SKILL.md 中的 name '{fm_name}' 与目录名 '{dir_name}' 不一致，请修正后重试"
                )

        # 落点也走规整器，而不是 `writable_root() / dir_name`：`dir_name` 是 URL 路径段，
        # 由 `resolve_writable_dir` 统一判字符集与越界，判据不在这里再写一遍
        dst = resolve_writable_dir(tenant_id, dir_name)
        if dst is None:
            raise ValueError(f"skill 目录名不合法: {dir_name}")

        if dst.exists() and not force:
            from nekro_agent.schemas.errors import ConflictError
            raise ConflictError(resource=f"技能 '{dir_name}' 已存在于技能库中")

        # 先复制到同级的临时目录，成功了再换上去：中途失败不会把库里原有的同名技能删掉一半
        dst.parent.mkdir(parents=True, exist_ok=True)
        staging = dst.with_name(f".{dst.name}.nk-promote")
        if staging.is_symlink() or staging.is_file():
            staging.unlink()
        elif staging.exists():
            shutil.rmtree(staging)
        try:
            skills.copy_tree_out(dir_name, staging)
            # 写入 .skill-origin.json（源目录里带过来的那份一并被覆盖）
            WorkspaceService._write_skill_origin(
                staging,
                origin="promoted",
                promoted_from_workspace=workspace_id,
                source_skill_id=dir_name,
            )
            if dst.exists():
                shutil.rmtree(dst)
            staging.rename(dst)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def _tree_has_link(directory: safe_fs.DirHandle, *, max_depth: int = safe_fs.MAX_TREE_DEPTH) -> bool:
        """目录树里有没有符号链接（不跟随任何链接地递归）

        嵌套超过 `max_depth` 层也按「有」处理：调用方据此拒绝晋升，不往更深处走
        （否则一棵故意造得很深的目录会让递归撞上 Python 的递归上限）。
        """
        for name, kind in directory.entries():
            if kind == "link":
                return True
            if kind == "dir":
                if max_depth <= 0:
                    return True
                with directory.subdir(name) as child:
                    if WorkspaceService._tree_has_link(child, max_depth=max_depth - 1):
                        return True
        return False

    @staticmethod
    def list_builtin_skills() -> List[Dict[str, str]]:
        """列出所有内置 skill（来自源码 builtin_skills 目录）"""
        src = Path(BUILTIN_SKILLS_SOURCE_DIR)
        if not src.exists():
            return []
        skills: List[Dict[str, str]] = []
        for skill_dir in sorted(src.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            meta = WorkspaceService._read_skill_meta(skill_dir)
            if meta is None:
                continue
            skills.append({"name": meta["name"], "description": meta["description"], "dir_name": skill_dir.name})
        return skills

    @staticmethod
    def list_global_skills(tenant_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """列出该作用域可见的独立技能库（不含内置、不含订阅仓库）

        平台方看到的就是 `local/`，与改造前逐字一致；
        租户看到的是「自己那一层」+「平台方标为 shared 的那些」。
        """
        from nekro_agent.services import skill_scope

        skills: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for root in skill_scope.readable_roots(tenant_id):
            if root.ownership is skill_scope.SkillOwnership.BUILTIN or root.kind == skill_scope.REPOS_KIND:
                continue
            root.base.mkdir(parents=True, exist_ok=True)
            for skill_dir in sorted(root.base.iterdir()):
                if not skill_dir.is_dir() or skill_dir.name.startswith(".") or skill_dir.name in seen:
                    continue
                if not skill_scope.is_entry_visible(root, skill_dir.name, tenant_id):
                    continue
                meta = WorkspaceService._read_skill_meta(skill_dir)
                if meta is None:
                    continue
                seen.add(skill_dir.name)
                skills.append({
                    "name": meta["name"],
                    "description": meta["description"],
                    "ownership": root.ownership.value,
                    "editable": root.editable,
                })
        return skills

    @staticmethod
    def _scan_repo_skills(root: "Any", tenant_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """递归扫描一个仓库根下所有含 SKILL.md 的目录，返回统一格式列表。

        标识格式：repo_name/skill_dir_name。按**整个仓库**过滤可见性，理由见
        `skill_scope.is_entry_visible`。
        """
        from nekro_agent.services import skill_scope

        results: List[Dict[str, Any]] = []
        repos_dir: Path = root.base
        if not repos_dir.exists():
            return results
        for repo_dir in sorted(repos_dir.iterdir()):
            if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                continue
            repo_name = repo_dir.name
            if not skill_scope.is_entry_visible(root, repo_name, tenant_id):
                continue
            for skill_dir in sorted(repo_dir.iterdir()):
                if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                    continue
                meta = WorkspaceService._read_skill_meta(skill_dir)
                if meta is None:
                    continue
                results.append({
                    "name": f"{repo_name}/{skill_dir.name}",
                    "display_name": meta["name"],
                    "description": meta["description"],
                    "source": "repo",
                    "repo_name": repo_name,
                    "ownership": root.ownership.value,
                    "editable": False,
                })
        return results

    @staticmethod
    def list_all_skills(tenant_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """列出该作用域可用的技能候选，每项含 `source` / `ownership` 字段

        扫描顺序 = `skill_scope.readable_roots()` 的解析顺序，两者必须是同一个列表：
        列表按 A 顺序去重、解析按 B 顺序命中，就会出现「界面上写着内置、同步进去的是别人那份」。
        """
        from nekro_agent.services import skill_scope

        seen: set[str] = set()
        result: List[Dict[str, Any]] = []

        for root in skill_scope.readable_roots(tenant_id):
            if root.ownership is not skill_scope.SkillOwnership.BUILTIN:
                root.base.mkdir(parents=True, exist_ok=True)
            if root.kind == skill_scope.REPOS_KIND:
                result.extend(WorkspaceService._scan_repo_skills(root, tenant_id))
                continue
            if not root.base.exists():
                continue
            for skill_dir in sorted(root.base.iterdir()):
                if not skill_dir.is_dir() or skill_dir.name.startswith(".") or skill_dir.name in seen:
                    continue
                if not skill_scope.is_entry_visible(root, skill_dir.name, tenant_id):
                    continue
                meta = WorkspaceService._read_skill_meta(skill_dir)
                if meta is None:
                    continue
                result.append({
                    "name": skill_dir.name,
                    "display_name": meta["name"],
                    "description": meta["description"],
                    "source": "builtin" if root.ownership is skill_scope.SkillOwnership.BUILTIN else "user",
                    "ownership": root.ownership.value,
                    "editable": root.editable,
                })
                seen.add(skill_dir.name)

        return result

    @staticmethod
    async def sync_single_skill(workspace: "DBWorkspace", skill_id: str) -> bool:
        """从全局 skill 库重新同步单个 skill 到工作区（不修改选中列表）。

        支持 builtin/local/repo 三种来源，skill_id 可含 / (仓库技能)。

        与 `sync_skills` 同理：源目录按**工作区自己的归属**解析，不可见即当作不存在。
        """
        from nekro_agent.services.skill_scope import workspace_skill_scope

        selected_skills: List[str] = workspace.metadata.get("skills", [])
        if skill_id not in selected_skills:
            return False
        src, cat = WorkspaceService._resolve_skill_source(skill_id, await workspace_skill_scope(workspace))
        if src is None or not src.is_dir():
            logger.warning(f"全局 skill 不存在或对本工作区的作用域不可见，无法同步: {skill_id}")
            return False
        target_name = skill_id.split("/")[-1] if "/" in skill_id else skill_id
        skills = WorkspaceService._open_workspace_skills(workspace.id, create=True)
        assert skills is not None
        with skills:
            if not WorkspaceService._sync_skill_into(skills, src, target_name, category=cat, skill_id=skill_id):
                return False
        logger.debug(f"重新同步 skill: {skill_id} → {target_name}")
        return True

    @staticmethod
    def _skill_origin_payload(
        *,
        origin: str,
        source_skill_id: Optional[str] = None,
        promoted_from_workspace: Optional[int] = None,
        repo_url: Optional[str] = None,
        community_id: Optional[str] = None,
        community_slug: Optional[str] = None,
    ) -> str:
        data = {
            "origin": origin,
            "source_skill_id": source_skill_id,
            "promoted_from_workspace": promoted_from_workspace,
            "repo_url": repo_url,
            "community_id": community_id,
            "community_slug": community_slug,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    @staticmethod
    def _write_skill_origin(
        skill_dir: Path,
        *,
        origin: str,
        source_skill_id: Optional[str] = None,
        promoted_from_workspace: Optional[int] = None,
        repo_url: Optional[str] = None,
        community_id: Optional[str] = None,
        community_slug: Optional[str] = None,
    ) -> None:
        """在**技能库**（宿主侧、容器碰不到）的 skill 目录下写入 .skill-origin.json 来源追溯文件。

        工作区里的 skill 目录在容器可写的 CC home 下，不走这里，见 `_sync_skill_into`。
        """
        origin_path = skill_dir / ".skill-origin.json"
        origin_path.write_text(
            WorkspaceService._skill_origin_payload(
                origin=origin,
                source_skill_id=source_skill_id,
                promoted_from_workspace=promoted_from_workspace,
                repo_url=repo_url,
                community_id=community_id,
                community_slug=community_slug,
            ),
            encoding="utf-8",
        )

    # ── 工作区里的 skill 目录（CC home 下的 `skills`，容器可写） ─────────────────
    # 下面这组函数都从 home 所在的根（`WorkspaceService.claude_home`）出发逐级不跟随链接地打开，
    # home、`skills`、技能目录、技能目录里的文件，任何一级是链接都不跟：读当作不存在，删只删链接
    # 本身，写之前删掉链接重建。

    _SKILL_FILE_MAX_BYTES = 1024 * 1024

    @staticmethod
    def _open_workspace_skills(workspace_id: int, *, create: bool = False) -> Optional[safe_fs.DirHandle]:
        """打开 home 下的 `skills`；`create` 时途中缺的建出来、被换成链接的删掉重建

        `create` 时定位、修 home、打开 `skills` 拿着 `home_lock` 一口气做完（迁移在线程里跑）；home
        本身只由 `ensure_claude_home` 修，这里不顺手建（旧布局的 home 不见了就落到新位置）。拿到的
        是目录句柄，之后 home 再被迁走，写入也跟着句柄落在挪过去的目录里。
        """
        if create:
            with home_lock(workspace_id):
                home = WorkspaceService.ensure_claude_home(WorkspaceService.claude_home(workspace_id))
                with safe_fs.open_dir(home.base, home.parts) as claude_home:
                    skills = claude_home.subdir("skills", create=True, replace_links=True)
                    # 容器里的 CC 要在 `skills/` 下建自己的目录（`skills/dynamic`），它得属容器用户
                    WorkspaceService.give_to_container(claude_home, "skills")
                    return skills
        home = WorkspaceService.claude_home(workspace_id)
        try:
            return safe_fs.open_dir(home.base, home.sub("skills"))
        except (FileNotFoundError, safe_fs.UnsafePathError):
            return None

    @staticmethod
    def _skill_origin_in(skills: safe_fs.DirHandle, name: str) -> Optional[Dict[str, Any]]:
        """读工作区里一个 skill 目录的 `.skill-origin.json`；目录或文件是链接、不存在、解析不了都返回 None"""
        if skills.kind(name) != "dir":
            return None
        try:
            with skills.subdir(name) as skill_dir:
                text = skill_dir.read_text(".skill-origin.json", max_bytes=64 * 1024, errors="replace")
        except (FileNotFoundError, safe_fs.UnsafePathError):
            return None
        if text is None:
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _read_skill_md_in(skills: safe_fs.DirHandle, name: str) -> Optional[str]:
        if skills.kind(name) != "dir":
            return None
        try:
            with skills.subdir(name) as skill_dir:
                return skill_dir.read_text(
                    "SKILL.md",
                    max_bytes=WorkspaceService._SKILL_FILE_MAX_BYTES,
                    errors="replace",
                )
        except (FileNotFoundError, safe_fs.UnsafePathError):
            return None

    @staticmethod
    def _is_dynamic_skill_in(skills: safe_fs.DirHandle, name: str) -> bool:
        """工作区本地的动态 skill：真目录（不是链接）、里面有普通文件 SKILL.md、没有来源标记或标记为 dynamic"""
        if name.startswith(".") or skills.kind(name) != "dir":
            return False
        try:
            with skills.subdir(name) as skill_dir:
                if skill_dir.kind("SKILL.md") != "file":
                    return False
        except (FileNotFoundError, safe_fs.UnsafePathError):
            return False
        origin = WorkspaceService._skill_origin_in(skills, name)
        return not origin or origin.get("origin") == "dynamic"

    @staticmethod
    def _discard_tree(directory: safe_fs.DirHandle, name: str, trash: Optional[safe_fs.DirHandle]) -> None:
        """删一个条目；给了 `trash`（启动路径）时删不完就整体挪进回收目录（`_open_trash_dir`）

        CC home 里的东西是容器自己的：它造一棵上百万条目的目录，删不完也不该让工作区
        启动失败。挪走之后这个名字就空出来了，下一次 `init_workspace_dir` 再清回收目录。
        """
        if trash is None:
            directory.remove_tree(name)
            return
        if directory.remove_tree_or_discard(name, trash) == "discarded":
            logger.warning(f"{directory.path / name} 里的条目太多，没删完，已整体挪进回收目录 {trash.path}，下次初始化再清")

    @staticmethod
    def _sync_skill_into(
        skills: safe_fs.DirHandle,
        src: Path,
        target_name: str,
        *,
        category: str,
        skill_id: str,
        trash: Optional[safe_fs.DirHandle] = None,
    ) -> bool:
        """把技能库里的一个 skill 复制进工作区；同名的是动态 skill（无来源标记）时跳过，返回是否同步了

        旧目录用 `remove_tree` 删（链接只删链接本身），新目录用 `copy_tree_in` 写（源里的链接跳过）。
        `trash` 见 `_discard_tree`。
        """
        existing = skills.kind(target_name)
        if existing == "dir":
            if not WorkspaceService._skill_origin_in(skills, target_name):
                logger.warning(f"跳过同步 skill（与 workspace-local dynamic skill 同名冲突）: {skill_id} -> {target_name}")
                return False
            WorkspaceService._discard_tree(skills, target_name, trash)
        elif existing is not None:
            # 容器放的链接或文件占了这个名字：它不是谁的技能，删掉链接本身再同步
            WorkspaceService._discard_tree(skills, target_name, trash)
        skills.copy_tree_in(src, target_name)
        with skills.subdir(target_name) as skill_dir:
            skill_dir.write_text(
                ".skill-origin.json",
                WorkspaceService._skill_origin_payload(origin=category, source_skill_id=skill_id),
            )
        WorkspaceService.give_to_container(skills, target_name, tree=True)
        return True

    @staticmethod
    def read_skill_origin(skill_dir: Path) -> Optional[Dict[str, Any]]:
        """读取 .skill-origin.json，不存在返回 None。"""
        origin_path = skill_dir / ".skill-origin.json"
        if not origin_path.exists():
            return None
        try:
            return json.loads(origin_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    async def sync_skill_to_all_workspaces(skill_id: str, tenant_id: Optional[int] = None) -> int:
        """将指定技能同步到该作用域内引用它的工作区。返回同步成功的工作区数量。

        Args:
            skill_id: 技能标识
            tenant_id: 发起方所处的作用域；`None` 为平台方（未启用多租户时的唯一分支）

        改前这里遍历 `DBWorkspace.all()` **不带任何归属过滤**，也就是说一次
        `POST /skills/sync-to-workspaces` 会往别的租户的工作区目录里写文件。
        现在租户只推得动归属为自己的工作区，平台方仍然是全量（它本来就管着全部工作区）。

        逐个工作区的归属推导会各查两次库，但这条链只在人手点击时触发，
        不在消息主路径上；换成批量推导要把 `sync_single_skill` 的签名一并改掉，
        收益不抵改动面。
        """
        from nekro_agent.services.skill_scope import workspace_skill_scope

        workspaces = await DBWorkspace.all()
        count = 0
        for ws in workspaces:
            selected: List[str] = ws.metadata.get("skills", [])
            if skill_id not in selected:
                continue
            if tenant_id is not None and await workspace_skill_scope(ws) != tenant_id:
                continue
            ok = await WorkspaceService.sync_single_skill(ws, skill_id)
            if ok:
                count += 1
        return count
