"""技能库的归属、可见性与作用域解析

## 这一层要解决的是什么

技能是**给 AI 看的工作流文本**，平台方很可能往里面写运维流程、内部地址、排障步骤。
改前 `routers/skills.py` 的**全部读端点是 `Role.Admin` 且不带任何过滤**，而租户管理员
正好就是 `Role.Admin`（`TENANT_MAX_PERM_LEVEL`）—— `GET /skills/tree` 拿目录、
`GET /skills/content` 拿正文，两跳就能把平台方整座技能库读走。
反过来写端点全是 `Role.Super`，租户一个字都写不了，
`需求文档 §R8`「可以自己制作并使用 skills」对定制版客户根本不成立。

两件事同一个根因：**技能库只有一份，而且没有归属概念**。

## 三种归属

| 归属 | 目录 | 谁能读 | 谁能写 |
|---|---|---|---|
| `BUILTIN` | `nekro_agent/builtin_skills/`（源码树） | 所有人 | 没有人（随代码发布） |
| `PLATFORM` | `{DATA_DIR}/skills/local`、`{DATA_DIR}/skills/repos` | 平台方；`shared` 的那些对全部租户可读 | 平台方（`Role.Super`） |
| `TENANT` | `{DATA_DIR}/skills/tenants/t{id}` | 该租户自己 | 该租户管理员 |

## 为什么按目录分层，而不是给技能加一个归属列

技能是**目录 + 文件**，不是数据库行。加归属列意味着再建一张「目录 -> 归属」的表，
于是同一件事有了两个事实来源：表说这个目录归 A，文件却躺在 B 的路径下，两者迟早分叉，
而且分叉时没有任何运行时信号（`services/workspace/tenant_scope.py` 顶上那条教训就是这个）。
按目录分层则「路径落在哪个根下」**本身就是**归属，判定与存储是同一件事，
连带把路径穿越也变成一次可判定的检查（`_within`）。

因此本次改动**不需要任何数据迁移**：存量的 `local/` 与 `repos/` 逐字不动，含义仍是
「平台方的库」；`tenants/` 是新长出来的目录，第一次有租户写入时才会被创建。
存量部署（含测试部署）升级后平台方看到的东西与改前完全一致。

## 可见性沿用模型组那套词，不造第二套

`SHARED` / `PRIVATE` 与 `models/db_model_group.ModelGroupVisibility` 同名同值
（`tests/test_skills_tenant_scope.py` 钉住两者不会分叉），语义也照抄：

- **平台方技能默认 `PRIVATE`**。这是本轮的安全默认值 —— 默认 `shared` 等于把原来的洞
  原样留着，只是换了个名字。平台方要共享哪一条，就显式点一次。
- **租户自建技能恒为私有**，与「租户自建模型组恒为私有」同源：它承载的是这个租户自己的
  东西，没有「共享给别的租户」这一说。所以可见性登记表里**只允许出现平台方的条目**，
  租户目录下的任何标记都不作数。
- **内置技能恒为共享**。它们随源码发布、在公开仓库里就能读到，没有任何可泄露的东西；
  而且 `auto-inject` 的默认三条全是内置技能，判成私有会让每个租户的新工作区一条技能都没有。

## 未启用多租户时

`get_tenant_scope()` 对平台方恒为 `None`，本模块的每个判定都在第一步返回平台方分支：
可读根还是 `builtin -> local -> repos`、可写根还是 `local/`、可见性一条都不查。
行为与引入多租户之前逐字一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import (
    BUILTIN_SKILLS_SOURCE_DIR,
    SKILLS_LOCAL_DIR,
    SKILLS_REPOS_DIR,
    SKILLS_TENANTS_DIR,
    OsEnv,
)
from nekro_agent.models.db_user import DBUser
from nekro_agent.services.user.tenant_perm import get_tenant_scope

logger = get_sub_logger("skill.scope")


class SkillOwnership(str, Enum):
    """技能的归属

    与 `schemas/tenant.ModelGroupOwnership` 的 `platform` / `tenant` 同义；
    技能多一档 `builtin`，因为内置技能不在 `DATA_DIR` 里而在源码树里，谁都改不了。
    """

    BUILTIN = "builtin"
    PLATFORM = "platform"
    TENANT = "tenant"


class SkillVisibility(str, Enum):
    """平台方技能对租户的可见性

    取值与 `models/db_model_group.ModelGroupVisibility` 逐字相同，这是有意的：
    同一个产品概念只能有一套词。
    """

    SHARED = "shared"
    PRIVATE = "private"


#: 可见性登记表。只记平台方的条目，键分两族：`local` 按技能目录名，`repos` 按仓库目录名。
#: 与 `auto-inject-skills.json` 放在同一个 `configs/` 目录下，两者是同一类「库级别的配置」。
VISIBILITY_PATH = Path(OsEnv.DATA_DIR) / "configs" / "skill-visibility.json"

#: 可见性登记表里的两族键
LOCAL_KIND = "local"
REPOS_KIND = "repos"


@dataclass(frozen=True)
class SkillRoot:
    """一个技能根目录

    Attributes:
        base: 根目录
        ownership: 落在这个根下的技能归属
        editable: 当前身份能否往这个根里写
        kind: 可见性登记表里的族名；`None` 表示这个根不参与可见性判定（内置与租户自有）
    """

    base: Path
    ownership: SkillOwnership
    editable: bool
    kind: Optional[str] = None


# ============================================================
# 目录
# ============================================================


def tenant_library_dir(tenant_id: int) -> Path:
    """某个租户的技能库目录

    目录名带 `t` 前缀而不是裸数字，与 `workspace_group_key` 的 `t{id}` 同一习惯：
    裸数字目录在人肉排查时与「第几个」难以区分，也不便于将来在同级放别的东西。
    """
    return Path(SKILLS_TENANTS_DIR) / f"t{int(tenant_id)}"


def builtin_dir() -> Path:
    return Path(BUILTIN_SKILLS_SOURCE_DIR)


def platform_local_dir() -> Path:
    return Path(SKILLS_LOCAL_DIR)


def platform_repos_dir() -> Path:
    return Path(SKILLS_REPOS_DIR)


# ============================================================
# 可见性登记表
# ============================================================


def _load_visibility() -> Dict[str, Dict[str, str]]:
    """读可见性登记表；读不出来时按空表处理（也就是「全部私有」）

    读失败不能回落成「全部共享」：那会让一次 JSON 损坏静默变成一次全量泄露。
    """
    try:
        raw = json.loads(VISIBILITY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {LOCAL_KIND: {}, REPOS_KIND: {}}
    except Exception as e:
        logger.warning(f"读取技能可见性登记表失败，按全部私有处理: {e}")
        return {LOCAL_KIND: {}, REPOS_KIND: {}}
    if not isinstance(raw, dict):
        logger.warning("技能可见性登记表结构非法，按全部私有处理")
        return {LOCAL_KIND: {}, REPOS_KIND: {}}
    result: Dict[str, Dict[str, str]] = {LOCAL_KIND: {}, REPOS_KIND: {}}
    for kind in (LOCAL_KIND, REPOS_KIND):
        section = raw.get(kind)
        if isinstance(section, dict):
            result[kind] = {str(k): str(v) for k, v in section.items()}
    return result


def _save_visibility(data: Dict[str, Dict[str, str]]) -> None:
    VISIBILITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    VISIBILITY_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_visibility(kind: str, name: str) -> SkillVisibility:
    """读某个平台方条目的可见性；未登记即 `PRIVATE`"""
    value = _load_visibility().get(kind, {}).get(name)
    return SkillVisibility.SHARED if value == SkillVisibility.SHARED.value else SkillVisibility.PRIVATE


def set_visibility(kind: str, name: str, visibility: SkillVisibility) -> None:
    """登记某个平台方条目的可见性

    只允许 `local` / `repos` 两族 —— 内置恒共享、租户自有恒私有，两者都没有可设的余地，
    放进来只会制造一份不生效却看起来生效的登记。
    """
    if kind not in (LOCAL_KIND, REPOS_KIND):
        raise ValueError(f"未知的技能可见性族: {kind!r}")
    data = _load_visibility()
    data.setdefault(kind, {})[name] = visibility.value
    _save_visibility(data)


def list_visibility(kind: str) -> Dict[str, str]:
    return dict(_load_visibility().get(kind, {}))


def prune_visibility(kind: str, name: str) -> None:
    """条目被删掉时一并清掉它的可见性登记

    留着不会造成越权（技能都不在了），但会让下一个同名技能**继承上一个的共享状态** ——
    删掉 `ops-runbook`（shared）后新建一个同名的私有技能，不清理就等于默认共享。
    """
    data = _load_visibility()
    if data.get(kind, {}).pop(name, None) is not None:
        _save_visibility(data)


# ============================================================
# 作用域
# ============================================================


def skill_scope_of(user: DBUser) -> Optional[int]:
    """当前身份的技能库作用域

    直接复用 `tenant_perm.get_tenant_scope`：归属判定只能有一处实现。
    `None` 表示平台方（未启用多租户时全部账号都落在这里）。
    """
    return get_tenant_scope(user)


def readable_roots(scope: Optional[int]) -> List[SkillRoot]:
    """当前作用域可读的技能根目录，**按解析优先级排序**

    平台方：`builtin -> local -> repos`，与改造前 `_resolve_skill_source` 的顺序逐字一致。

    租户：**自己的库排在最前**。租户建了一个与内置同名的技能，那是它在自己作用域里的
    有意覆盖；把内置排前面会让「我明明改了却不生效」变成一个查不出来的问题。
    列表与解析必须共用这一个顺序，否则列表说是内置、解析取到租户的，就是一次静默的错位。
    """
    if scope is None:
        return [
            SkillRoot(builtin_dir(), SkillOwnership.BUILTIN, editable=False),
            SkillRoot(platform_local_dir(), SkillOwnership.PLATFORM, editable=True, kind=LOCAL_KIND),
            SkillRoot(platform_repos_dir(), SkillOwnership.PLATFORM, editable=False, kind=REPOS_KIND),
        ]
    return [
        SkillRoot(tenant_library_dir(scope), SkillOwnership.TENANT, editable=True),
        SkillRoot(builtin_dir(), SkillOwnership.BUILTIN, editable=False),
        SkillRoot(platform_local_dir(), SkillOwnership.PLATFORM, editable=False, kind=LOCAL_KIND),
        SkillRoot(platform_repos_dir(), SkillOwnership.PLATFORM, editable=False, kind=REPOS_KIND),
    ]


def writable_root(scope: Optional[int]) -> Path:
    """当前作用域的可写库

    平台方写 `local/`，租户写自己那一层。**租户永远写不到平台方的目录**：
    这不是靠调用点记得判一下，而是靠「可写根只有这一个出口」。
    """
    return platform_local_dir() if scope is None else tenant_library_dir(scope)


def is_entry_visible(root: SkillRoot, top_name: str, scope: Optional[int]) -> bool:
    """某个根下的顶层条目对当前作用域是否可见

    顶层条目：`local/` 下是技能目录名，`repos/` 下是仓库目录名。
    仓库按**整个仓库**共享而不是按里面每个技能 —— 订阅仓库这个动作本身就是整份拉进来的，
    拆到技能粒度只会让「我共享了仓库，租户却看不到里面新增的技能」变成常态。
    """
    if scope is None:
        return True
    if root.kind is None:
        return True
    return get_visibility(root.kind, top_name) is SkillVisibility.SHARED


# ============================================================
# 路径解析
# ============================================================


@dataclass(frozen=True)
class ResolvedSkillPath:
    """一次成功的解析结果"""

    path: Path
    root: SkillRoot
    top_name: str

    @property
    def ownership(self) -> SkillOwnership:
        return self.root.ownership

    @property
    def editable(self) -> bool:
        return self.root.editable


def normalize_rel(rel: str) -> Optional[List[str]]:
    """把外部传进来的相对路径规整成路径段；不合法时返回 `None`

    这里挡的是字面量层面的越界（`..`、绝对路径、盘符、反斜杠、NUL）。
    符号链接越界由 `_within` 在 resolve 之后再挡一次 —— 两层都要有：
    字面量检查拦不住软链，resolve 检查拦不住 Windows 上的 `C:` 前缀。
    """
    cleaned = rel.strip().strip("/")
    if not cleaned or "\\" in cleaned or "\x00" in cleaned or ":" in cleaned:
        return None
    segments = [segment for segment in cleaned.split("/") if segment]
    if not segments:
        return None
    for segment in segments:
        if segment in {".", ".."} or segment.startswith("."):
            return None
    return segments


def is_within(base: Path, candidate: Path) -> bool:
    """`candidate` **resolve 之后**是否仍落在 `base` 里

    这是本模块唯一的「越界」判据。公开出来是因为它有第二、第三个用处：
    目录树扫描与目录列举会跟着软链走出去，而它们拿到的是已经解析好的绝对路径，
    没法再走一遍 `_resolve`。判据只能有一处实现，否则「读得到但列不出」
    或者反过来「列得出但读不到」这种自相矛盾的状态迟早出现。
    """
    try:
        return candidate.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


#: 模块内部沿用的短名字
_within = is_within


def contains_symlink(path: Path) -> bool:
    """`path` 自身或它的目录树里是否存在符号链接

    ## 为什么这条判据必须在「写进技能库」之前跑

    `writable_root()` 只回答「写到哪儿去」，它对「从哪儿读」一个字都没说。
    晋升（`promote_dynamic_skill`）的**源目录是容器可写的** —— 沙盒里的 Claude Code
    被一句话指使就能 `ln -s`，而 `shutil.copytree` 默认 `symlinks=False`，也就是
    **解引用**：一条指向 `{DATA_DIR}/skills/local` 的软链，晋升之后在租户库里
    变成一份平台方私有技能的**真实副本**（不是软链，因此 `is_within` 这一层
    在读的时候已经无从判起），随后 `GET /skills/file` 逐字读得到、
    `sync_skills` 还会把它复制进这个租户的每一个工作区。
    指向别的工作区 `.claude/settings.json` 时拿到的则是那个工作区的中继凭据。

    所以「技能库里不许出现软链」是一条**内容准入**规则，与路径解析是两件事。
    扫描不跟着软链往下走（先判 `is_symlink` 再判 `is_dir`），因此不会遇到环。

    Args:
        path: 待检查的目录（不存在时返回 `False`，那是调用方另一条分支的事）

    Returns:
        存在符号链接、或扫描过程中出错时为 `True`（拿不准就拒绝）
    """
    try:
        if path.is_symlink():
            return True
        if not path.is_dir():
            return False
        for entry in path.iterdir():
            if entry.is_symlink():
                return True
            if entry.is_dir() and contains_symlink(entry):
                return True
    except OSError as e:
        logger.warning(f"扫描技能目录的符号链接失败，按「存在」处理: {e}")
        return True
    return False


def resolve_dir(scope: Optional[int], rel: str, *, roots: Optional[Sequence[SkillRoot]] = None) -> Optional[ResolvedSkillPath]:
    """在可读根里按优先级解析一个**目录**；不可见与不存在都返回 `None`

    对外必须表现为 404：可见与不可见给出不同的错误，租户就能靠试错枚举出平台方有哪些技能。
    """
    return _resolve(scope, rel, want_dir=True, roots=roots)


def resolve_file(scope: Optional[int], rel: str, *, roots: Optional[Sequence[SkillRoot]] = None) -> Optional[ResolvedSkillPath]:
    """在可读根里按优先级解析一个**文件**；不可见与不存在都返回 `None`"""
    return _resolve(scope, rel, want_dir=False, roots=roots)


def _resolve(
    scope: Optional[int],
    rel: str,
    *,
    want_dir: bool,
    roots: Optional[Sequence[SkillRoot]] = None,
) -> Optional[ResolvedSkillPath]:
    segments = normalize_rel(rel)
    if segments is None:
        return None
    for root in roots if roots is not None else readable_roots(scope):
        if not is_entry_visible(root, segments[0], scope):
            continue
        candidate = root.base.joinpath(*segments)
        if not _within(root.base, candidate):
            logger.warning(f"技能路径越界，已拒绝: {rel!r}")
            continue
        if candidate.is_dir() if want_dir else candidate.is_file():
            return ResolvedSkillPath(path=candidate, root=root, top_name=segments[0])
    return None


def resolve_writable_dir(scope: Optional[int], rel: str) -> Optional[Path]:
    """把一个相对路径解析到当前作用域的**可写根**下，不要求它已存在

    写入路径只认可写根这一个出口，因此这里不遍历可读根：租户传一个平台方技能的名字，
    落点仍然是它自己库里的同名目录，而不是平台方那份。
    """
    segments = normalize_rel(rel)
    if segments is None:
        return None
    base = writable_root(scope)
    candidate = base.joinpath(*segments)
    base.mkdir(parents=True, exist_ok=True)
    if not _within(base, candidate):
        logger.warning(f"技能写入路径越界，已拒绝: {rel!r}")
        return None
    return candidate


# ============================================================
# 工作区归属 -> 技能作用域
# ============================================================


async def channel_skill_scope(chat_key: str) -> Optional[int]:
    """某个聊天频道所在的技能库作用域

    链路与 `services/workspace/tenant_scope` 用的完全一样：频道 -> 实例 -> 租户。
    查不到频道 / 频道没有实例段 / 实例未划归租户，都是**真的归平台方**
    （未认领实例恒归平台方，见 `console/instances.py` 的认领流程）。

    **这里不吞异常**：查询失败时 `None` 恰好等于"不受限"，吞掉就把一次数据库抖动
    变成一次越权。让它抛出去，调用方（提示词注入 / `load_skill`）自己失败，
    那是安全的方向。

    给主 Agent 侧用：`plugins/builtin/skill_loader` 的技能索引与 `load_skill()`
    是技能正文的**第二个出口**，改前它同样不带作用域，租户的 Bot 一句话就能把
    平台方私有技能的正文念进聊天里。
    """
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance
    from nekro_agent.models.db_chat_channel import DBChatChannel

    channel = await DBChatChannel.get_or_none(chat_key=chat_key)
    if channel is None or not channel.instance_key:
        return None
    instance = await DBAdapterInstance.filter(instance_key=channel.instance_key).first()
    return instance.tenant_id if instance is not None else None


async def workspace_skill_scope(workspace) -> Optional[int]:  # noqa: ANN001 - 避免与 models 形成导入环
    """某个工作区在技能库里的作用域

    归属直接走 `services/workspace/tenant_scope` 那条已有的推导链
    （频道 -> 实例 -> 租户），不另推一套。

    `PLATFORM_GROUP`（没绑频道 / 绑的都是平台方频道）与 `SHARED_GROUP`（归属不唯一）
    都按**平台方**处理。这不是放宽：这两类工作区对任何租户身份都是 404
    （`assert_workspace_accessible`），租户根本无法往里面选技能，唯一能给它们选技能的
    就是平台方自己。
    """
    from nekro_agent.services.workspace.network_guard import resolve_workspace_network_group

    group = await resolve_workspace_network_group(workspace)
    if isinstance(group, str) and group.startswith("t") and group[1:].isdigit():
        return int(group[1:])
    return None
