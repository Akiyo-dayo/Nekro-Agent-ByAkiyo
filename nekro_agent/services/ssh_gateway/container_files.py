"""NA 写进工作区容器的 SSH 网关文件

容器里的 `ssh` 只看得到别名：目标主机地址、端口和真正的凭据全部留在 NA。这里落三样东西：

- `config`：每个可用别名一段 `Host`，把连接指向 NA 在网桥上的地址与网关端口；
- `known_hosts`：网关自己的主机公钥（主机名固定 `nk-gateway`）；
- `id_gateway`：这个工作区专用的网关客户端私钥（只开网关，开不了任何服务器）。

## 为什么先写临时文件再替换

容器里的 `ssh` 随时可能读这几个文件。直接覆盖时，读到一半的配置会让那一次连接失败，
而用户在容器里看到的只是「连接被重置」一类没有线索的报错。写同目录临时文件再
`os.replace`，读的一方要么看到旧文件、要么看到新文件。

## 为什么权限要进容器里改

NA 可能跑在宿主上、且 uid 既不是 0 也不是 1000。它直接写出来的文件属主就是它自己，
容器里以 `appuser`（uid 1000）运行的 ssh 会判 `config` 为 Bad owner、私钥则根本读不了。
容器里的 root 能改绑定挂载上的属主，所以写完统一经 `docker exec -u 0` 收权限：
目录 `root:root 0755`、两个配置文件 `root:root 0644`、私钥 `1000:1000 0600`。
权限收失败只记 WARNING：文件内容已经写好了，这个工作区的 SSH 暂时不可用，
但不该把工作区的其它功能一起拖下水。

## 不跟随容器能改的符号链接

CC home（容器里的 `~/.claude`，宿主上在哪由 `WorkspaceService.claude_home` 定）是 0777：
容器里的 appuser 能把 `nk-ssh` 换成符号链接，或者在 `nk-ssh` 里放
`id_gateway -> /etc/shadow` 这样的链接；还没迁移的旧布局下 home 本身（`{工作区}/.claude_home`）
也在读写挂载的工作区目录里，能被整个换掉。所以：

- 宿主侧从 home 所在的根逐级用 `O_NOFOLLOW` 打开 home、`nk-ssh`，在目录句柄上写临时文件再
  `rename`；途中任何一级是符号链接就不写（`nk-ssh` 本身是链接或普通文件时先删掉它再
  建目录——那是 NA 自己的路径）。
- 容器里以 root 收权限时先 `cd -P` 进目录、核对物理路径没被换，再把目录本身收成
  `root:root 0755`。从这一刻起 appuser 不能再改目录里的条目，之后才逐个检查三个文件：
  符号链接、非普通文件、硬链接数不为 1 的一律删掉（删的是链接本身），然后再改属主权限。
  这样 `chown` / `chmod` 不会顺着任何容器能控制的路径走到别处。

## 为什么 `render_ssh_config` 不写目标地址

容器里有完整 shell，写进容器的任何东西都能被 CC 打印出来。目标地址与端口虽然不算凭据，
但把它们集中写进容器等于把平台方资产清单递给容器里的任意代码；而 ssh 只需要别名。

## CLAUDE.md 的缓存

`CLAUDE.md` 的生成是同步函数，而凭据行要查库。这里顺手缓存一份「可用的服务器」小节，
由异步写文件/凭据变更的路径刷新；同步生成函数只读缓存，总开关关着时连读都不读。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import aiodocker

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import keys
from nekro_agent.services.workspace import safe_fs
from nekro_agent.services.workspace.manager import WorkspaceService, home_lock

logger = get_sub_logger("ssh_gateway.container_files")

NK_SSH_DIRNAME = "nk-ssh"
CONTAINER_CLAUDE_HOME = "/home/appuser/.claude"
CONTAINER_NK_SSH_DIR = f"{CONTAINER_CLAUDE_HOME}/{NK_SSH_DIRNAME}"
CONTAINER_CONFIG_PATH = f"{CONTAINER_NK_SSH_DIR}/config"
CONTAINER_KNOWN_HOSTS_PATH = f"{CONTAINER_NK_SSH_DIR}/known_hosts"
CONTAINER_PRIVATE_KEY_PATH = f"{CONTAINER_NK_SSH_DIR}/id_gateway"

# ssh 自己会检查的几种状态：停用的行不写进配置，但「登不上」「挂起」仍保留，
# 让用户仍能看到这个别名存在（连接时由网关给出确切原因）
_CONFIG_STATUSES = frozenset({C.STATUS_ACTIVE, C.STATUS_AUTH_FAILED, C.STATUS_SUSPENDED})

_CONFIG_HEADER = (
    "# 由 NekroAgent 生成，改了会被覆盖。\n"
    "# 这里只放别名与网关地址；目标服务器的主机、端口与凭据都不在容器里。\n"
)

# 每次执行一个短命令；超时只影响这一次权限收尾，不影响工作区启动
_EXEC_TIMEOUT_SECONDS = 15.0

# workspace_id -> 已缓存的 CLAUDE.md 小节（空串表示「没有可用行」）
_ssh_servers_section_cache: Dict[int, str] = {}


def nk_ssh_host_dir(workspace_id: int) -> Path:
    """宿主上这个工作区 CC home 里的 `nk-ssh` 目录（容器里看到的是 `~/.claude/nk-ssh`）"""
    return WorkspaceService.claude_home(int(workspace_id)).path / NK_SSH_DIRNAME


def _row_status(row: object) -> str:
    return str(getattr(row, "status", "") or "")


def _sorted_rows(rows: Iterable[object]) -> List[object]:
    """按别名排序：同一份数据每次生成逐字相同，配置文件不会因为查询顺序而变化"""
    return sorted(rows, key=lambda row: str(getattr(row, "alias", "") or ""))


def render_ssh_config(rows: Iterable[object], *, listen_address: str, port: int) -> str:
    """生成容器里的 ssh 配置

    每个 `active` / `auth_failed` / `suspended` 的行一段；停用的不写。一行都没有时只留
    文件头注释而不删文件：镜像里的 `Include` 指向这个路径，文件忽有忽无会让排障的人
    分不清「没配」和「配置丢了」。
    """
    usable = [row for row in _sorted_rows(rows) if _row_status(row) in _CONFIG_STATUSES]
    lines = [_CONFIG_HEADER.rstrip("\n")]
    for row in usable:
        alias = str(getattr(row, "alias", "") or "").strip()
        if not alias:
            continue
        lines.extend(
            [
                "",
                f"Host {alias}",
                f"    HostName {listen_address}",
                f"    Port {int(port)}",
                f"    User {alias}",
                f"    IdentityFile {CONTAINER_PRIVATE_KEY_PATH}",
                "    IdentitiesOnly yes",
                "    IdentityAgent none",
                f"    UserKnownHostsFile {CONTAINER_KNOWN_HOSTS_PATH}",
                "    HostKeyAlias nk-gateway",
                "    StrictHostKeyChecking yes",
                "    ForwardAgent no",
                "    ForwardX11 no",
                "    PreferredAuthentications publickey",
                "    ServerAliveInterval 30",
            ],
        )
    return "\n".join(lines) + "\n"


def render_known_hosts(gateway_public_key: str) -> str:
    """网关主机公钥写成一行 `nk-gateway <公钥行>`；取不到公钥时写空文件"""
    value = str(gateway_public_key or "").strip()
    if not value:
        return ""
    return f"nk-gateway {value}\n"


def render_ssh_servers_prompt_section(rows: Iterable[object]) -> str:
    """生成 `CLAUDE.md` 的「可用的服务器（SSH）」小节；没有可用行时返回空串

    只写别名、登录账号与归属。主机地址、端口、备注一概不写：容器里的 CC（以及任何能
    让它执行命令的人）都能读到 `CLAUDE.md`。
    """
    usable = [row for row in _sorted_rows(rows) if _row_status(row) != C.STATUS_DISABLED]
    if not usable:
        return ""
    lines = [
        "## 可用的服务器（SSH）",
        "",
        "| 别名 | 登录账号 | 归属 |",
        "|------|----------|------|",
    ]
    for row in usable:
        alias = str(getattr(row, "alias", "") or "").strip()
        username = str(getattr(row, "username", "") or "").strip()
        owner = "平台方" if str(getattr(row, "owner_kind", "") or "") == C.OWNER_PLATFORM else "用户"
        if alias:
            lines.append(f"| `{alias}` | `{username}` | {owner} |")
    lines.extend(
        [
            "",
            "用法：",
            '- 非交互执行命令用 `ssh -T <别名> \'命令\'`；需要终端（例如 `htop`）才用 `ssh -t <别名>`。',
            "- `scp` / `rsync` / `git` 把远端路径写成 `<别名>:路径`。",
            "- 不要写 `user@` 或 `-l`：登录账号由登记的凭据决定。",
            "- 不支持 agent 转发（`-A`）与 X11 转发（`-X`）。",
            "- 凭据不在容器里，不用去环境变量或文件里找。",
            "- 只有被授权的任务能连上；被拒时把网关横幅里的原因原样告诉 Agent，不要尝试绕过。",
            "- 服务器上的每条命令都会被审计。",
        ],
    )
    return "\n".join(lines)


def get_ssh_servers_section(workspace_id: int) -> str:
    """读缓存的「可用的服务器」小节；没有时返回空串（不查库）"""
    return _ssh_servers_section_cache.get(int(workspace_id), "")


def reset_ssh_servers_section_cache() -> None:
    """清空缓存（仅供测试与关停）"""
    _ssh_servers_section_cache.clear()


async def _load_rows(workspace_id: int) -> List[object]:
    """查这个工作区的全部凭据行；按别名排序由渲染函数负责"""
    from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential

    return list(await DBWsSshCredential.filter(workspace_id=int(workspace_id)).order_by("alias"))


async def refresh_ssh_servers_section(workspace_id: int) -> str:
    """重新查库并刷新 `CLAUDE.md` 小节缓存；返回新的小节文本

    只在总开关开着时由异步路径调用。同步的 `CLAUDE.md` 生成函数不会走到这里。
    """
    rows = await _load_rows(workspace_id)
    section = render_ssh_servers_prompt_section(rows)
    _ssh_servers_section_cache[int(workspace_id)] = section
    return section


class UnsafeNkSshPathError(OSError):
    """CC home / `nk-ssh` 路径上有容器放的符号链接，不往里写"""


_NK_SSH_FILES = ("config", "known_hosts", "id_gateway")
# 与 `safe_fs` 同一个判据：有没有目录句柄那条路（测试按它区分两种平台）
_DIR_FD_SUPPORTED = safe_fs.DIR_FD_SUPPORTED


def _write_nk_ssh_files_on_host(workspace_id: int, files: Dict[str, str]) -> None:
    """把几份文件写进 `nk-ssh`；路径被容器换成链接时抛 `UnsafeNkSshPathError`

    走 `safe_fs`（与写 CC 用户级 `settings.json` 是同一套函数）：从 home 所在的根逐级
    `O_NOFOLLOW`，`nk-ssh` 是 NA 自己的路径，被换成链接或文件时删掉链接本身重建；
    文件写临时文件再 rename。home 在哪由 `WorkspaceService.claude_home` 定；旧布局的
    `.claude_home` 本身是链接时不替它做主（它还挂着 CC 的会话），这一次不写。
    """
    # 定位与写入拿着 `home_lock` 一口气做完：建容器的迁移在线程里跑，网关开关、补发钥匙这些路径
    # 不拿工作区锁
    with home_lock(int(workspace_id)):
        home = WorkspaceService.claude_home(int(workspace_id))
        try:
            if not home.legacy:
                # 与建容器时同一种建法（上级 0700，home 本身 0777，容器里的 appuser 要写）
                home = WorkspaceService.ensure_claude_home(home)
            with safe_fs.open_dir(home.base, home.parts) as claude_home:
                with claude_home.subdir(NK_SSH_DIRNAME, create=True, replace_links=True) as nk_ssh:
                    for name, text in files.items():
                        nk_ssh.write_text(name, text, mode=0o600 if name == "id_gateway" else 0o644)
                    if "id_gateway" in files:
                        # 与容器里的收权限脚本同一口径：目录与两个配置文件留给 root（容器里的 appuser
                        # 换不掉目录里的条目，ssh 也认 root 的配置），私钥交给 appuser、照旧 0600——
                        # 不然 ssh 读不了它，要等容器起来以后那一步才补上
                        WorkspaceService.give_to_container(nk_ssh, "id_gateway")
        except safe_fs.UnsafePathError as exc:
            raise UnsafeNkSshPathError(str(exc)) from exc


def _fix_permissions_script() -> str:
    """容器里以 root 执行的收权限脚本

    不把路径拼进命令行参数，而是写死在这个脚本里：调用方只传容器名，命令内容固定，
    不会被文件名或别名注入。顺序见模块说明「不跟随容器能改的符号链接」：先进目录、核对
    物理路径、把目录收成 root 所有，之后才碰里面的文件。删掉了可疑条目时以 4 退出，
    调用方记一条 WARNING，下一次写文件会补上。
    """
    return (
        "set -e; "
        f"d={CONTAINER_NK_SSH_DIR}; "
        'if [ ! -e "$d" ] && [ ! -L "$d" ]; then mkdir "$d"; fi; '
        'cd -P "$d"; '
        '[ "$(pwd -P)" = "$d" ] || exit 3; '
        "chown root:root .; "
        "chmod 0755 .; "
        "bad=; "
        "for f in config known_hosts id_gateway; do "
        'if [ -L "$f" ]; then rm -f -- "$f"; bad=1; continue; fi; '
        '[ -e "$f" ] || continue; '
        'if [ ! -f "$f" ]; then rm -rf -- "$f"; bad=1; continue; fi; '
        'if [ "$(stat -c %h -- "$f")" != 1 ]; then rm -f -- "$f"; bad=1; fi; '
        "done; "
        "if [ -f config ]; then chown root:root config; chmod 0644 config; fi; "
        "if [ -f known_hosts ]; then chown root:root known_hosts; chmod 0644 known_hosts; fi; "
        "if [ -f id_gateway ]; then chown 1000:1000 id_gateway; chmod 0600 id_gateway; fi; "
        '[ -z "$bad" ] || exit 4'
    )


def _open_docker() -> aiodocker.Docker:
    """单独一层，方便测试替换；生产路径就是普通的 Docker 客户端"""
    return aiodocker.Docker()


async def fix_permissions_in_container(container_name: str) -> None:
    """经 `docker exec -u 0` 把 `nk-ssh` 的属主与权限收成 ssh 能接受的样子

    失败只记 WARNING（带容器名，不带任何文件内容），并说明这个工作区的 SSH 暂时不可用。
    它不该让容器创建、凭据保存或任务委托失败。
    """
    name = str(container_name or "").strip()
    if not name:
        # 容器还没建好（创建流程在写文件时就会走到这里）：没有可改的对象，静默跳过
        logger.debug("容器名尚未确定，跳过 nk-ssh 权限收尾")
        return

    docker = None
    try:
        docker = _open_docker()
        container = await docker.containers.get(name)
        exec_inst = await container.exec(
            ["sh", "-c", _fix_permissions_script()],
            user="0",
            stdout=True,
            stderr=True,
        )
        async with asyncio.timeout(_EXEC_TIMEOUT_SECONDS):
            async with exec_inst.start(detach=False) as stream:
                # 必须把输出读干净：不读的话守护进程的写缓冲可能把 exec 卡在最后一段
                while await stream.read_out() is not None:
                    pass
        info = await exec_inst.inspect()
        exit_code = int(info.get("ExitCode", 0) or 0)
        if exit_code != 0:
            raise RuntimeError(f"exec 退出码 {exit_code}")
    except Exception as exc:  # noqa: BLE001 - 权限收尾失败不能影响工作区或凭据操作
        logger.warning(
            f"收 nk-ssh 权限失败（容器 {name}），这个工作区的 SSH 暂时不可用: {type(exc).__name__}: {exc}",
        )
    finally:
        if docker is not None:
            try:
                await docker.close()
            except Exception:  # noqa: BLE001 - 关客户端失败只影响这一条收尾路径
                pass


async def write_nk_ssh_files(
    workspace: object,
    *,
    listen_address: str,
    private_key: Optional[str],
) -> None:
    """在宿主侧写 `config` / `known_hosts`，给了私钥时再写 `id_gateway`

    Args:
        workspace: 工作区记录（需要 `id` 与 `container_name`）
        listen_address: 容器连网关的地址（`WorkspaceNetworkPlan.gateway_listen_address`）
        private_key: 新生成的 OpenSSH 私钥文本；`None` 表示只重写配置、不动私钥
            （凭据变更时属于这一种：钥匙要和容器一起轮换，不该在运行中换）

    写完就收一次权限：宿主侧重写会把属主变回 NA 自己。收权限失败只记 WARNING。
    """
    workspace_id = int(getattr(workspace, "id"))
    container_name = str(getattr(workspace, "container_name", "") or "")
    rows = await _load_rows(workspace_id)
    _ssh_servers_section_cache[workspace_id] = render_ssh_servers_prompt_section(rows)

    port = 0
    try:
        from nekro_agent.core.config import config

        port = int(config.SSH_GATEWAY_PORT)
    except Exception:  # noqa: BLE001 - 配置非法时端口写 0，连接会失败但文件本身仍然完整
        port = 0

    files: Dict[str, str] = {
        "config": render_ssh_config(rows, listen_address=listen_address, port=port),
        "known_hosts": render_known_hosts(keys.gateway_host_public_key()),
    }
    if private_key is not None:
        files["id_gateway"] = str(private_key)
    try:
        _write_nk_ssh_files_on_host(workspace_id, files)
    except UnsafeNkSshPathError as exc:
        # 路径被容器换过：这一次不写，也不去容器里收权限（收权限脚本自己也会拒）
        logger.warning(f"工作区 {workspace_id} 的 nk-ssh 路径上有符号链接，没有写入 SSH 网关文件: {exc}")
        return

    await fix_permissions_in_container(container_name)


def _remove_nk_ssh_files_on_host(workspace_id: int) -> None:
    with home_lock(int(workspace_id)):
        _remove_nk_ssh_files_locked(int(workspace_id))


def _remove_nk_ssh_files_locked(workspace_id: int) -> None:
    home = WorkspaceService.claude_home(workspace_id)
    try:
        claude_home = safe_fs.open_dir(home.base, home.parts)
    except FileNotFoundError:
        return
    with claude_home:
        kind = claude_home.kind(NK_SSH_DIRNAME)
        if kind is None:
            return
        if kind != "dir":
            # 容器把 nk-ssh 换成了链接或文件：删掉它本身（链接只删链接），不跟过去
            claude_home.unlink(NK_SSH_DIRNAME)
            return
        with claude_home.subdir(NK_SSH_DIRNAME) as nk_ssh:
            for name in _NK_SSH_FILES:
                for candidate in (name, name + ".tmp", f".{name}.nk-tmp"):
                    # 只删自己写的文件与链接；容器放进来的同名目录不递归去删，留给人处理
                    if nk_ssh.kind(candidate) not in (None, "dir"):
                        nk_ssh.unlink(candidate)
        # 里面还有别的东西（容器放的）：只删我们自己的文件，目录留着
        claude_home.rmdir(NK_SSH_DIRNAME)


async def remove_nk_ssh_files(workspace_id: int) -> None:
    """删掉 `nk-ssh` 里的网关文件与目录（总开关关着时，下一次建容器用）

    只删自己写的三个文件，不递归：路径上任何一级是容器放的符号链接都不跟随。
    """
    _ssh_servers_section_cache.pop(int(workspace_id), None)
    try:
        _remove_nk_ssh_files_on_host(workspace_id)
    except Exception as exc:  # noqa: BLE001 - 删不掉最多是旧文件留着，不该拦住启动
        logger.warning(f"删除工作区 {workspace_id} 的 nk-ssh 目录失败（已忽略）: {type(exc).__name__}: {exc}")


__all__ = [
    "NK_SSH_DIRNAME",
    "fix_permissions_in_container",
    "get_ssh_servers_section",
    "nk_ssh_host_dir",
    "refresh_ssh_servers_section",
    "remove_nk_ssh_files",
    "render_known_hosts",
    "render_ssh_config",
    "render_ssh_servers_prompt_section",
    "reset_ssh_servers_section_cache",
    "write_nk_ssh_files",
]
