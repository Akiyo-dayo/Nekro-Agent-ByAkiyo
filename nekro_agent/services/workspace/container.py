import asyncio
import contextlib
import os
import random
import re
import secrets
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple

import aiodocker
import httpx
from tortoise import timezone

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv, deployment_scope
from nekro_agent.models.db_workspace import DBWorkspace, patch_workspace_metadata
from nekro_agent.services.ssh_gateway import keys as ssh_gateway_keys
from nekro_agent.services.ssh_gateway import registry as ssh_gateway_registry
from nekro_agent.services.ssh_gateway.container_files import (
    fix_permissions_in_container,
    remove_nk_ssh_files,
    write_nk_ssh_files,
)
from nekro_agent.services.ssh_gateway.image_support import image_supports_gateway
from nekro_agent.services.ssh_gateway.registry import ContainerPeer
from nekro_agent.services.workspace import safe_fs
from nekro_agent.services.workspace.cc_credential import METADATA_TOKEN_KEY
from nekro_agent.services.workspace.manager import WorkspaceService
from nekro_agent.services.workspace.network_guard import (
    extract_network_ipv4,
    prepare_workspace_network,
    resolve_container_relay_addresses,
    resolve_workspace_network_group,
)

logger = get_sub_logger("workspace_container")


class ImageNotFoundError(Exception):
    """本地不存在指定的沙盒镜像，需要先拉取。"""

    def __init__(self, image: str) -> None:
        self.image = image
        super().__init__(f"本地不存在镜像 {image!r}，请先拉取")

CONTAINER_WORKSPACE_PATH = "/workspace"  # 容器内工作区挂载路径

# 工作区行上 `sandbox_version` 列的默认值是 "latest"：没单独设过版本的工作区都带着它。
# 它的意思是「跟着全局默认」，而不是「用 latest 这个标签」——默认镜像（自建的 cc-sandbox）
# 从不发 latest，照字面用会让所有存量工作区去拉一个不存在的标签。
_FOLLOW_GLOBAL_SANDBOX_VERSIONS = frozenset({"", "latest"})


def effective_sandbox_image(workspace: DBWorkspace) -> Tuple[str, str]:
    """这个工作区实际使用的沙盒镜像名与标签

    - 工作区单独设了镜像名：用它，标签照它自己写的（空则用全局标签），`latest` 也照字面用；
    - 没设镜像名（跟着全局镜像）：标签是空串或列默认值 `latest` 时也跟着全局标签，
      只有平台方单独钉过的具体标签才用它自己的。

    建容器、检查 / 拉取镜像、读接口展示、租户回传的等价判断都走这一个函数，不各写一份。
    """
    if workspace.sandbox_image:
        return workspace.sandbox_image, workspace.sandbox_version or config.CC_SANDBOX_IMAGE_TAG
    tag = workspace.sandbox_version
    if tag in _FOLLOW_GLOBAL_SANDBOX_VERSIONS:
        tag = config.CC_SANDBOX_IMAGE_TAG
    return config.CC_SANDBOX_IMAGE, tag

_resolved_nekro_network: str = ""  # 模块级缓存，避免每次创建容器都重复探测
_resolve_network_lock: asyncio.Lock = asyncio.Lock()  # 并发保护，确保只探测一次
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")  # Docker 容器 ID 格式：64 位十六进制


async def resolve_nekro_network(docker: aiodocker.Docker) -> str:
    """解析 NA 所在的 nekro_network 网络名。

    通过 Docker API inspect 当前 NA 容器自身，取其所连接的以 'nekro_network'
    结尾的网络名。结果缓存于模块变量，同一进程内只探测一次；并发调用由 asyncio.Lock 保护。
    """
    global _resolved_nekro_network

    # 快速路径：已缓存则直接返回（无需持锁）
    if _resolved_nekro_network:
        return _resolved_nekro_network

    async with _resolve_network_lock:
        # 持锁后再次检查，避免多个等待者重复探测
        if _resolved_nekro_network:
            return _resolved_nekro_network

        # 通过 Docker API inspect 自身容器，取实际所属的 nekro_network
        try:
            container_id = Path("/etc/hostname").read_text().strip()
            if not _CONTAINER_ID_RE.match(container_id):
                logger.warning(
                    f"[网络自动探测] /etc/hostname 内容 {container_id!r} 不是有效容器 ID"
                    "（非 64 位十六进制），跳过 Docker inspect"
                )
            else:
                self_container = await docker.containers.get(container_id)
                info = await self_container.show()
                networks: dict = info.get("NetworkSettings", {}).get("Networks", {})
                for network_name in networks:
                    if network_name.endswith("nekro_network"):
                        _resolved_nekro_network = network_name
                        logger.info(f"[网络自动探测] 已发现 nekro_network: {network_name!r}")
                        return _resolved_nekro_network
        except Exception as e:
            logger.warning(
                f"[网络自动探测] Docker API 探测失败，回退至默认网络名: {e}",
                exc_info=True,
            )

        # 最终兜底：无前缀的默认名
        _resolved_nekro_network = "nekro_network"
        logger.warning("[网络自动探测] 未找到匹配网络，使用默认值: nekro_network")
        return _resolved_nekro_network


def _get_host_timezone() -> str:
    """获取宿主机时区标识，用于注入容器。优先读 TZ 环境变量，再读 /etc/timezone，最终 fallback UTC。"""
    tz = os.environ.get("TZ", "").strip()
    if tz:
        return tz
    try:
        tz = Path("/etc/timezone").read_text().strip()
        if tz:
            return tz
    except Exception:
        pass
    return "UTC"


def _generate_container_name() -> str:
    """生成随机容器名 nekro-cc-{hex8}"""
    return f"nekro-cc-{secrets.token_hex(4)}"


async def _container_exists(docker: aiodocker.Docker, name: str) -> bool:
    """检查容器是否存在"""
    try:
        c = await docker.containers.get(name)
        return c is not None
    except Exception:
        return False


async def _find_free_port() -> int:
    """在高位端口段随机选取一个未被 DB 使用的端口"""
    start = config.CC_SANDBOX_PORT_RANGE_START
    end = config.CC_SANDBOX_PORT_RANGE_END
    used_ports_raw = await DBWorkspace.filter(host_port__isnull=False).values_list("host_port", flat=True)
    used_from_db: Set[int] = {int(p) for p in used_ports_raw if p is not None}

    for _ in range(100):
        port = random.randint(start, end)
        if port not in used_from_db:
            return port
    raise RuntimeError("无法在端口段内找到空闲端口")


# ── bind 源：工作区目录与 `.claude_home` ─────────────────────────────────────
#
# 两个 bind 源都在容器可写的地方：`/workspace` 整个挂进容器，镜像的入口脚本在它对 appuser
# 不可写时会 `chown -R appuser` 整个工作区，于是容器能把 `.claude_home` 改名、换成指向宿主
# 任意目录的符号链接。Docker 挂 bind 源时会顺着链接走（测试部署上实测：源是指向宿主别处的
# 链接时，挂进容器的是链接的目标），下一次启动就会把那个宿主目录以读写方式挂进容器。
#
# 所以启动（新建、重启）一律按「先停、再修、再核对、再启动」走：
# 1. 停掉这个工作区**所有**在跑的容器：不只是库里记的那一个，还有按标签
#    （`nekro.workspace.id`）或挂载（挂了这个工作区目录）认出来的别的容器——并发启动留下的、
#    升级前「健康检查超时 → 再启动」留下的孤儿、重建时没删掉的旧容器。新建时一律删掉，重启时
#    除了要重启的那一个都删掉；删完还有在跑的就拒绝启动、记 ERROR。别的容器（不是这个工作区的）
#    以读写方式挂着工作区目录或它的上级的，也拒绝启动（`_refuse_foreign_writers`，NA 自己与
#    NapCat 放行）。之后到 Docker 挂载之间，没有任何别的能写这个工作区的容器在跑；
# 2. 修：`init_workspace_dir` 把被换成链接或别的东西的 `.claude_home` 删掉、重建空目录
#    （发现被换过记 ERROR）；
# 3. 核对：两个 bind 源都按字面路径（不 `resolve()`）、不跟随链接地看一遍，不是普通目录就
#    拒绝启动、记 ERROR；重启时还要核对容器里**已经记着**的挂载（`HostConfig.Binds` 与
#    `Mounts`）没有预期之外的来源或目标（`_mount_mismatch`）——早先的版本按 `resolve()` 过的
#    路径建过容器，被利用过的话宿主路径就固化在那里，重启照挂不误；
# 4. 启动。
#
# 新建、重启、重建、停止、删除按工作区串行：进程内一把 `asyncio.Lock`，库里一个可比较替换的
# 过渡状态 `transitioning`（删除是 `deleting`）。**只支持单进程部署**：多个 worker 各有各的锁，
# 库里的比较替换挡得住同时改状态，挡不住另一个进程直接动 Docker（运维文档 §3.5 写明）。
#
# 仍然管不到的：有人在宿主上手动操作容器（容器的 `RestartPolicy` 是 `no`，Docker 自己不会
# 重启它）；NapCat 容器以读写方式挂着整个 `DATA_DIR`（包括工作区），这里按「NapCat 视为可信」
# 处理，收窄它的挂载放在后续版本；把 `.claude_home` 搬出 `/workspace`（容器里挂载点改名会得到
# EBUSY，这一类竞争整个消失）也放在后续版本。


class UnsafeBindSourceError(RuntimeError):
    """工作区目录或 `.claude_home` 不是普通目录（是符号链接或别的类型），拒绝把它挂进容器"""


def _workspace_bind_path(workspace_id: int) -> Path:
    """工作区目录的绝对路径：只按字面展开，不 `resolve()`

    `resolve()` 会顺着链接走到目标，正是这个漏洞要的那一步；工作区目录的上级都是 NA 自己的
    数据目录，按字面交给 Docker 即可。
    """
    return Path(os.path.abspath(str(WorkspaceService.get_workspace_dir(workspace_id))))


async def _stop_if_running(docker: aiodocker.Docker, container_name: str | None) -> None:
    """停掉这个工作区还在跑的容器；容器不在、已经停了都不算错，停不掉就抛

    调用方据「停不掉」放弃启动：旧容器还在跑的话，它能在核对之后、挂载之前把 `.claude_home`
    换成链接。
    """
    if not container_name:
        return
    try:
        container = await docker.containers.get(container_name)
    except aiodocker.exceptions.DockerError as e:
        if e.status == 404:
            return
        raise
    info = await container.show()
    if not (info.get("State") or {}).get("Running"):
        return
    await container.stop(t=10)


def _note_replaced_claude_home(workspace_id: int) -> None:
    """容器停着的时候看一眼 `.claude_home`：被换成了链接或别的东西就记 ERROR

    只记录，不修：修在随后的 `init_workspace_dir` 里做（删掉链接本身、重建空目录，
    从不跟着链接走）。这是一条安全事件，运维要知道这个工作区的容器试过越界。
    """
    kind = safe_fs.kind(_workspace_bind_path(workspace_id), (".claude_home",))
    if kind not in (None, "dir"):
        logger.error(
            f"工作区 {workspace_id} 的 .claude_home 被换成了 {kind}（容器可能想把宿主目录挂进来），"
            "接下来删掉它本身、重建空目录；原来的会话与技能如果被挪走了，在工作区目录里找",
        )


def _checked_bind_sources(workspace_id: int) -> Tuple[str, str]:
    """启动前最后修一遍、核对两个 bind 源，返回 `(工作区目录, .claude_home)` 的字面路径

    只能在这个工作区没有容器在跑的时候调。先把 `.claude_home` 修成普通目录（是链接就删掉
    链接本身重建，不跟着走；`init_workspace_dir` 修过一遍，这里兜住它之后的那几步），再核对：
    任何一个不是普通目录（是链接、文件、不存在）就记 ERROR、抛 `UnsafeBindSourceError`，
    绝不替它 `resolve()`。
    """
    ws_path = _workspace_bind_path(workspace_id)
    safe_fs.ensure_dir(ws_path, (".claude_home",), replace_links=True)
    try:
        ws_stat = os.lstat(ws_path)
    except FileNotFoundError:
        ws_stat = None
    if ws_stat is None or not stat.S_ISDIR(ws_stat.st_mode):
        logger.error(f"工作区 {workspace_id} 的目录不是普通目录（是链接、文件或不存在），拒绝启动容器: {ws_path}")
        raise UnsafeBindSourceError(f"工作区 {workspace_id} 的目录不是普通目录，拒绝启动容器")
    kind = safe_fs.kind(ws_path, (".claude_home",))
    if kind != "dir":
        logger.error(f"工作区 {workspace_id} 的 .claude_home 不是普通目录（{kind or '不存在'}），拒绝启动容器")
        raise UnsafeBindSourceError(f"工作区 {workspace_id} 的 .claude_home 不是普通目录，拒绝启动容器")
    return str(ws_path), str(ws_path / ".claude_home")


# ── 同一工作区的所有容器 ─────────────────────────────────────────────────────

WORKSPACE_LABEL = "nekro.workspace.id"
CLAUDE_HOME_MOUNT = "/home/appuser/.claude"
LOCALTIME_MOUNT = "/etc/localtime"
TRANSITIONING = "transitioning"
DELETING = "deleting"
# 过渡状态多久没动就算卡住（进程在启停中途退出）：建容器、写配置的那一段远用不了这么久，
# 健康检查是在写出 active 之后才等的
STALE_TRANSITION_SECONDS = 600
# NA 启动时 inspect 失败（不是 404）的重试间隔（秒，逐次加长）
_INSPECT_RETRY_DELAY = 0.5
# 以读写方式挂着工作区上级目录、却允许它在跑的容器：NapCat（控制台建的带实例标签，compose 起的
# 按服务名）；NA 自己按容器 ID / 名字认（见 `_refuse_foreign_writers`）
NAPCAT_INSTANCE_LABEL = "nekro.napcat.instance"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"

_workspace_locks: Dict[int, asyncio.Lock] = {}


class WorkspaceBusyError(RuntimeError):
    """工作区正在启停，或当前状态不允许这个操作"""


class WorkspaceContainerConflictError(RuntimeError):
    """同一工作区还有别的容器在跑、停不掉也删不掉，拒绝启动"""


def _workspace_lock(workspace_id: int) -> asyncio.Lock:
    lock = _workspace_locks.get(workspace_id)
    if lock is None:
        lock = asyncio.Lock()
        _workspace_locks[workspace_id] = lock
    return lock


@contextlib.asynccontextmanager
async def _transition(
    workspace: DBWorkspace,
    allowed_from: Tuple[str, ...],
    action: str,
    *,
    wait: bool = False,
    take_stale: bool = False,
) -> AsyncIterator[None]:
    """把一次启停串行化：进程内的锁 + 库里可比较替换的过渡状态

    - 锁：同一工作区同时只有一次启停在做。`wait=False`（启动、重启、重建）时锁被占着就直接
      拒绝（「正在启停，请稍后」），不排队——排队的第二次启动只会把刚起来的容器再换掉一遍；
      `wait=True`（停止）排在前一个后面：停止是收紧的方向，撤销凭据时必须停得下来。
    - 过渡状态：拿到锁之后按 `allowed_from` 把库里的状态比较替换成 `transitioning`；换不成
      （当前状态不允许）就抛 `WorkspaceBusyError`。换成之后按库里的那一行刷新 `workspace`：
      调用方手里的对象可能是排队前读的，容器名已经被前一个操作改过。
    - 收尾：操作自己写最终状态；没写、写库失败、或者中途出了异常（包括停了旧容器之后才失败），
      一律写 `failed` 并记 `last_error`。判「写没写出来」看的是**库里**是否还是 `transitioning`
      （比较替换，不看内存里的对象：内存里改成了 active、落库那一下失败的，库里仍是过渡状态）。
    - 卡住的过渡状态：`take_stale=True`（停止、删除）时，库里是 `transitioning` 且超过
      `STALE_TRANSITION_SECONDS` 没动过，按已失效接管（进程在启停中途退出、又没等到启动恢复）；
      没超过就照样拒绝——那可能是另一个进程正在做（锁只在一个进程里有效，见本模块「bind 源」一节）。

    只支持单进程部署：多个 worker 各有各的锁，库里的比较替换挡得住同时写状态，挡不住另一个
    进程不经这里直接动 Docker。
    """
    lock = _workspace_lock(workspace.id)
    if not wait and lock.locked():
        raise WorkspaceBusyError(f"工作区 {workspace.id} 正在启停，请稍后再试")
    async with lock:
        now = timezone.now()
        changed = await DBWorkspace.filter(id=workspace.id, status__in=list(allowed_from)).update(
            status=TRANSITIONING,
            update_time=now,
        )
        if not changed and take_stale:
            cutoff = now - timedelta(seconds=STALE_TRANSITION_SECONDS)
            changed = await DBWorkspace.filter(id=workspace.id, status=TRANSITIONING, update_time__lt=cutoff).update(
                status=TRANSITIONING,
                update_time=now,
            )
            if changed:
                logger.warning(f"工作区 {workspace.id} 卡在 transitioning 超过 {STALE_TRANSITION_SECONDS} 秒，按已失效接管去{action}")
        if not changed:
            current = await DBWorkspace.get_or_none(id=workspace.id)
            state = current.status if current is not None else "不存在"
            raise WorkspaceBusyError(f"工作区 {workspace.id} 当前状态是 {state}，不能{action}")
        await workspace.refresh_from_db()
        try:
            yield
        except BaseException as exc:
            await _settle_failed(workspace, f"{action}失败: {type(exc).__name__}: {exc}")
            raise
        await _settle_failed(workspace, f"{action}没有写出最终状态")


async def _settle_failed(workspace: DBWorkspace, message: str) -> None:
    """库里还是 `transitioning` 就改成 failed（比较替换：已经落库的最终状态不动）"""
    message = message[:1000]
    try:
        changed = await DBWorkspace.filter(id=workspace.id, status=TRANSITIONING).update(
            status="failed",
            last_error=message,
            update_time=timezone.now(),
        )
    except Exception as exc:  # noqa: BLE001 - 原来的异常更要紧；残留的由停止 / 启动恢复接管
        logger.error(f"工作区 {workspace.id} 收尾时写 failed 也失败了（{message}）: {exc}")
        return
    if changed:
        workspace.status = "failed"
        workspace.last_error = message  # type: ignore[assignment]


def _workspace_source_paths(workspace_id: int) -> Set[str]:
    """这个工作区的 bind 源在容器的挂载里可能出现的写法：字面路径、解析后的路径，各自加 `.claude_home`

    早先的版本按 `resolve()` 过的路径建过容器，两种都要认。
    """
    literal = _workspace_bind_path(workspace_id)
    paths: Set[str] = set()
    for base in {literal, Path(os.path.realpath(str(literal)))}:
        paths.add(str(base))
        paths.add(str(base / ".claude_home"))
    return paths


def _summary(container: Any) -> Dict[str, Any]:
    """`containers.list()` 给出的那份摘要（aiodocker 把它放在 `_container` 上）"""
    data = getattr(container, "_container", None)
    return data if isinstance(data, dict) else {}


def _summary_names(summary: Dict[str, Any]) -> Set[str]:
    return {str(name).lstrip("/") for name in (summary.get("Names") or [])}


def _labelled_workspace_id(summary: Dict[str, Any]) -> Optional[int]:
    """按标签认出的工作区 ID；只认本部署的（同一台宿主上可能还有别的部署，ID 会重复）"""
    labels = summary.get("Labels") or {}
    if str(labels.get("nekro.owner.scope") or "") != deployment_scope():
        return None
    raw = str(labels.get(WORKSPACE_LABEL) or "")
    return int(raw) if raw.isdigit() else None


def _belongs_to_workspace(summary: Dict[str, Any], workspace_id: int, sources: Set[str]) -> bool:
    if _labelled_workspace_id(summary) == workspace_id:
        return True
    return any(str(mount.get("Source") or "") in sources for mount in (summary.get("Mounts") or []))


async def _workspace_containers(docker: aiodocker.Docker, workspace_id: int) -> List[Any]:
    """这个工作区的所有容器（在跑的、停着的）：带它的标签，或者挂了它的目录"""
    sources = _workspace_source_paths(workspace_id)
    containers = await docker.containers.list(all=True)
    return [c for c in containers if _belongs_to_workspace(_summary(c), workspace_id, sources)]


async def _evict_workspace_containers(
    docker: aiodocker.Docker,
    workspace_id: int,
    *,
    keep_name: Optional[str] = None,
) -> None:
    """停掉并删掉这个工作区除 `keep_name` 之外的所有容器；删完还有在跑的就拒绝启动

    `keep_name` 是重启时要重启的那一个（它由调用方单独停）。新建时不留任何一个。
    """
    for container in await _workspace_containers(docker, workspace_id):
        summary = _summary(container)
        names = _summary_names(summary)
        if keep_name and keep_name in names:
            continue
        label = ", ".join(sorted(names)) or str(summary.get("Id") or "")[:12]
        logger.warning(f"工作区 {workspace_id} 还有别的容器 {label}（不是这次要启动的那个），停掉并删除")
        try:
            await container.delete(force=True)
        except aiodocker.exceptions.DockerError as e:
            if e.status != 404:
                logger.error(f"删除工作区 {workspace_id} 的多余容器 {label} 失败: {e}")
    leftovers = []
    for container in await _workspace_containers(docker, workspace_id):
        summary = _summary(container)
        if keep_name and keep_name in _summary_names(summary):
            continue
        if str(summary.get("State") or "").lower() == "running":
            leftovers.append(", ".join(sorted(_summary_names(summary))) or str(summary.get("Id") or "")[:12])
    if leftovers:
        logger.error(f"工作区 {workspace_id} 还有别的容器在跑、删不掉，拒绝启动: {leftovers}")
        raise WorkspaceContainerConflictError(f"工作区 {workspace_id} 还有别的容器在跑、删不掉，拒绝启动")


def _bind_list(ws_host_dir: str, claude_home_host_dir: str) -> List[str]:
    """CC 容器的 bind 挂载：建容器用它，重启 / 启动恢复时核对容器里记着的挂载也用它"""
    binds = [
        f"{ws_host_dir}:{CONTAINER_WORKSPACE_PATH}:rw",
        f"{claude_home_host_dir}:{CLAUDE_HOME_MOUNT}:rw",
    ]
    if Path(LOCALTIME_MOUNT).exists():
        binds.append(f"{LOCALTIME_MOUNT}:{LOCALTIME_MOUNT}:ro")
    return binds


def _parse_bind(entry: str) -> Optional[Tuple[str, str, bool]]:
    """`源:目标[:选项]` → `(源, 目标, 可写)`；认不出返回 None

    源可能带盘符冒号（Windows），所以从右边切：最后一段不以 `/` 开头的才是选项。
    """
    parts = entry.rsplit(":", 2)
    if len(parts) == 3 and not parts[2].startswith("/"):
        source, destination, options = parts
        writable = "ro" not in options.split(",")
    else:
        source, _, destination = entry.rpartition(":")
        writable = True
    if not source or not destination.startswith("/"):
        return None
    return source, destination, writable


def _same_directory(first: Path, second: Path) -> bool:
    """两个路径是不是同一个目录：字面 / 解析后的写法相同，或者 `(st_dev, st_ino)` 相同"""
    forms_a = {os.path.normpath(os.path.abspath(str(first))), os.path.normpath(os.path.realpath(str(first)))}
    forms_b = {os.path.normpath(os.path.abspath(str(second))), os.path.normpath(os.path.realpath(str(second)))}
    if forms_a & forms_b:
        return True
    try:
        st_a, st_b = os.stat(first), os.stat(second)
    except OSError:
        return False
    return stat.S_ISDIR(st_a.st_mode) and (st_a.st_dev, st_a.st_ino) == (st_b.st_dev, st_b.st_ino)


def _is_workspace_source(source: str, workspace_id: int, tail: Tuple[str, ...]) -> bool:
    """`source` 是不是 `<工作区根>/<id>` 再接 `tail`（不论工作区根是哪种写法）

    只在**工作区根**这一级按目录身份（`st_dev` / `st_ino`）比：工作区根及其上级是 NA 与部署者的
    目录，换写法（数据目录经链接进入、docker-guard 改写成解析后的路径）都是同一个目录。工作区
    里面的那几级（`<id>`、`.claude_home`）必须逐字相同、不许有 `..`——那里容器写得到，经它里面
    的链接绕回来的写法，下一次启动 Docker 会顺着那个链接重新走一遍。
    """
    candidate = Path(source)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return False
    need = (str(workspace_id), *tail)
    parts = candidate.parts
    if len(parts) <= len(need) or tuple(parts[-len(need):]) != need:
        return False
    root = Path(*parts[: len(parts) - len(need)])
    expected_root = WorkspaceService.get_workspace_dir(workspace_id).parent
    # 工作区根那一段的写法里，任何一级都不能落在工作区根**下面**（某个工作区里）：
    # `<根>/2/inner/1/.claude_home`，`inner` 是 2 号容器放的指回工作区根的链接，按目录身份比
    # 能对上，可下一次启动 Docker 会顺着 `inner` 走，而 2 号容器随时能把它改指别处
    for prefix in (root, *root.parents):
        if prefix != prefix.parent and _same_directory(prefix.parent, expected_root):
            return False
    return _same_directory(root, expected_root)


def _mount_mismatch(info: Dict[str, Any], workspace_id: int) -> Optional[str]:
    """容器里记着的挂载有没有预期之外的东西；有就返回说明，没有返回 None

    判据：`Mounts`（Docker 实际挂上的，包括经 `HostConfig.Mounts` 声明、不出现在 `Binds` 里的）
    与 `HostConfig.Binds` 里的每一条 bind，目标只能是 `/workspace`、`~/.claude`、`/etc/localtime`
    三者之一，来源必须是这个工作区目录 / 它的 `.claude_home` / 宿主的 `/etc/localtime`（只读）。
    缺哪一条不算不一致：早期版本建的容器本来就没有 `/etc/localtime`，NA 自己有没有这个文件
    也与宿主无关。Docker 自己管的卷（`volume` / `tmpfs`）没有宿主路径，不比。
    """
    actual: List[Tuple[str, str, bool]] = []
    for mount in info.get("Mounts") or []:
        if str(mount.get("Type") or "bind") != "bind":
            continue
        actual.append((str(mount.get("Source") or ""), str(mount.get("Destination") or ""), bool(mount.get("RW"))))
    for entry in (info.get("HostConfig") or {}).get("Binds") or []:
        parsed = _parse_bind(str(entry))
        if parsed is None:
            return f"认不出的 bind 写法: {entry}"
        actual.append(parsed)
    for source, destination, writable in actual:
        if destination == CONTAINER_WORKSPACE_PATH:
            ok = _is_workspace_source(source, workspace_id, ())
        elif destination == CLAUDE_HOME_MOUNT:
            ok = _is_workspace_source(source, workspace_id, (".claude_home",))
        elif destination == LOCALTIME_MOUNT:
            ok = source == LOCALTIME_MOUNT and not writable
        else:
            return f"预期之外的挂载: {source} → {destination}"
        if not ok:
            return f"{destination} 的来源不是预期的: {source}"
    return None


def _missing_hardening(info: Dict[str, Any]) -> List[str]:
    """升级前建的容器缺了哪几项加固（`CapDrop: MKNOD`、`no-new-privileges`）"""
    host_config = info.get("HostConfig") or {}
    missing = []
    if "MKNOD" not in {str(cap).upper().removeprefix("CAP_") for cap in host_config.get("CapDrop") or []}:
        missing.append("CapDrop MKNOD")
    if not any(str(opt).split(":", 1)[0].split("=", 1)[0] == "no-new-privileges" for opt in host_config.get("SecurityOpt") or []):
        missing.append("no-new-privileges")
    return missing


def _self_container_markers() -> Set[str]:
    """NA 自己所在容器的标识（`HOSTNAME`、`/etc/hostname`）；不在容器里时为空"""
    if not OsEnv.RUN_IN_DOCKER:
        return set()
    markers = {os.environ.get("HOSTNAME", "").strip()}
    with contextlib.suppress(OSError):
        markers.add(Path("/etc/hostname").read_text(encoding="utf-8").strip())
    return {marker for marker in markers if marker}


def _is_self(summary: Dict[str, Any], markers: Set[str]) -> bool:
    container_id = str(summary.get("Id") or "")
    if _summary_names(summary) & markers:
        return True
    return any(len(marker) >= 12 and container_id.startswith(marker) for marker in markers)


def _is_trusted_napcat(summary: Dict[str, Any]) -> bool:
    labels = summary.get("Labels") or {}
    return NAPCAT_INSTANCE_LABEL in labels or str(labels.get(COMPOSE_SERVICE_LABEL) or "") == "napcat"


def _ancestor_identities(path: Path) -> Tuple[Set[str], Set[Tuple[int, int]]]:
    """`path` 本身与它的每一级上级：字面 / 解析后的写法，以及 `(st_dev, st_ino)`"""
    names: Set[str] = set()
    inodes: Set[Tuple[int, int]] = set()
    for form in {os.path.abspath(str(path)), os.path.realpath(str(path))}:
        current = Path(form)
        for level in (current, *current.parents):
            names.add(os.path.normpath(str(level)))
            with contextlib.suppress(OSError):
                st = os.stat(level)
                inodes.add((st.st_dev, st.st_ino))
    return names, inodes


async def _refuse_foreign_writers(docker: aiodocker.Docker, workspace_id: int) -> None:
    """在跑的别的容器以读写方式挂着这个工作区目录或它的上级：拒绝启动、记 ERROR

    这样的容器能在核对之后、新容器挂载之前把 `.claude_home` 换掉。按来源的写法与目录身份
    （`st_dev` / `st_ino`）认；只读挂载不算。放行 NA 自己（数据目录本来就挂在它身上）与
    NapCat（视为可信，#257）。同一工作区的 CC 容器在这之前已经清场。
    """
    names, inodes = _ancestor_identities(_workspace_bind_path(workspace_id))
    markers = _self_container_markers()
    offenders: List[str] = []
    for container in await docker.containers.list():
        summary = _summary(container)
        if str(summary.get("State") or "running").lower() != "running":
            continue
        if _is_self(summary, markers) or _is_trusted_napcat(summary):
            continue
        for mount in summary.get("Mounts") or []:
            if str(mount.get("Type") or "bind") != "bind" or not mount.get("RW"):
                continue
            source = str(mount.get("Source") or "")
            if not source:
                continue
            hit = os.path.normpath(source) in names
            if not hit:
                with contextlib.suppress(OSError):
                    st = os.stat(source)
                    hit = (st.st_dev, st.st_ino) in inodes
            if hit:
                label = ", ".join(sorted(_summary_names(summary))) or str(summary.get("Id") or "")[:12]
                offenders.append(f"{label}（{source}）")
                break
    if offenders:
        logger.error(f"工作区 {workspace_id}：有别的容器以读写方式挂着工作区目录或它的上级，拒绝启动: {offenders}")
        raise WorkspaceContainerConflictError(
            f"工作区 {workspace_id}：有别的容器以读写方式挂着工作区目录或它的上级，拒绝启动",
        )


def _workspace_id_of(summary: Dict[str, Any]) -> Optional[int]:
    """从容器摘要认出它属于哪个工作区：先看（本部署的）标签，再看挂在 `/workspace` 的是不是本部署
    工作区根下的目录（升级前建的容器没有工作区标签）"""
    labelled = _labelled_workspace_id(summary)
    if labelled is not None:
        return labelled
    roots = {
        Path(os.path.abspath(str(WorkspaceService.get_workspace_dir(0).parent))),
        Path(os.path.realpath(str(WorkspaceService.get_workspace_dir(0).parent))),
    }
    for mount in summary.get("Mounts") or []:
        if str(mount.get("Destination") or "") != CONTAINER_WORKSPACE_PATH:
            continue
        source = Path(str(mount.get("Source") or ""))
        if source.parent in roots and source.name.isdigit():
            return int(source.name)
    return None


async def _remove_orphan_containers(docker: aiodocker.Docker) -> int:
    """NA 启动时：删掉不是任何工作区当前容器的 CC 容器（孤儿），返回删了几个

    认法与 `_workspace_containers` 相同（标签或挂载）。工作区已经不存在、或者库里记的不是
    这个容器，就是孤儿：它和新容器在同一张网桥上，留着它就是一个没人看着的执行环境。
    """
    removed = 0
    for container in await docker.containers.list(all=True):
        summary = _summary(container)
        workspace_id = _workspace_id_of(summary)
        if workspace_id is None:
            continue
        names = _summary_names(summary)
        # 拿着这个工作区的锁再判：启动恢复晚于 NA 就绪几秒才跑，这期间可能有一次启动正在建容器，
        # 新容器带着标签、还没记进库。锁里库与容器是一致的（建好没记上的会被当场删掉）
        async with _workspace_lock(workspace_id):
            workspace = await DBWorkspace.get_or_none(id=workspace_id)
            if workspace is not None and workspace.container_name and workspace.container_name in names:
                continue
            label = ", ".join(sorted(names)) or str(summary.get("Id") or "")[:12]
            reason = "已不存在" if workspace is None else "记的不是它"
            logger.warning(f"删除孤儿 CC 容器 {label}（工作区 {workspace_id} {reason}）")
            try:
                await container.delete(force=True)
                removed += 1
            except aiodocker.exceptions.DockerError as e:
                if e.status != 404:
                    logger.error(f"删除孤儿 CC 容器 {label} 失败: {e}")
    return removed


class SandboxContainerManager:
    @staticmethod
    async def _rotate_internal_api_token(workspace: DBWorkspace) -> str:
        """每建一个新容器就换一把沙盒内部 API 的鉴权 token（`INTERNAL_API_TOKEN`）

        这把 token 在容器的环境变量里，容器里的进程都读得到。原来整个工作区一直用同一把：
        没删掉的旧容器（孤儿）和新容器在同一张网桥上，拿着它就能调新容器的内部 API。
        重启沿用同一个容器，不换。
        """
        token = secrets.token_urlsafe(32)
        # 按键写：别处同时整份写回 metadata 也改不回旧值（见 `GUARDED_METADATA_KEYS`）
        await patch_workspace_metadata(workspace, set_keys={"sandbox_api_token": token})
        return token

    @staticmethod
    async def _require_image(workspace: DBWorkspace) -> str:
        """镜像不在本地就在动任何东西之前拒绝（不改状态、不删旧容器）"""
        image_name, image_tag = effective_sandbox_image(workspace)
        image = f"{image_name}:{image_tag}"
        if not await SandboxContainerManager.check_image_exists(image):
            raise ImageNotFoundError(image)
        return image

    @staticmethod
    async def _resolve_preset(workspace: DBWorkspace):
        """从工作区 metadata 读取 CC 模型预设，fallback 到默认预设"""
        from nekro_agent.core.cc_model_presets import cc_presets_store

        preset_id = (workspace.metadata or {}).get("cc_model_preset_id")
        if preset_id:
            preset = cc_presets_store.get_by_id(int(preset_id))
            if preset:
                return preset
        return cc_presets_store.get_default()

    @staticmethod
    async def check_image_exists(image: str) -> bool:
        """检查本地是否已有指定镜像。"""
        docker = aiodocker.Docker()
        try:
            await docker.images.get(image)
            return True
        except aiodocker.exceptions.DockerError:
            return False
        finally:
            await docker.close()

    @staticmethod
    async def create_and_start(workspace: DBWorkspace) -> DBWorkspace:
        """初始化目录 + 创建并启动容器 + 等待健康检查

        只能从 stopped / failed 启动；同一工作区的启停串行（见本模块「bind 源」一节）。
        """
        # 提前检查镜像是否存在，给出友好错误而非 500
        await SandboxContainerManager._require_image(workspace)
        async with _transition(workspace, ("stopped", "failed"), "启动"):
            return await SandboxContainerManager._create_and_start_locked(workspace)

    @staticmethod
    async def _create_and_start_locked(workspace: DBWorkspace) -> DBWorkspace:
        """`create_and_start` / `rebuild` 的本体：调用方已经拿着锁、状态已经是 transitioning"""
        image = await SandboxContainerManager._require_image(workspace)

        cc_preset = await SandboxContainerManager._resolve_preset(workspace)
        api_token = await SandboxContainerManager._rotate_internal_api_token(workspace)

        docker = aiodocker.Docker()
        try:
            # 先清场：这个工作区所有的容器（库里记的那个、并发启动或升级前留下的孤儿、重建时没删掉的）
            # 一律停掉删掉。哪个还在跑，它就能在下面核对 bind 源之后、新容器挂载之前把
            # `.claude_home` 换成链接。见本模块「bind 源」一节。删完还有在跑的就整个放弃启动
            await _evict_workspace_containers(docker, workspace.id)
            # 旧容器都删掉了：库里不再留它们的名字（后面失败的话，库里不会挂着一个已经不存在的容器）
            if workspace.container_name or workspace.container_id or workspace.host_port:
                workspace.container_name = None  # type: ignore[assignment]
                workspace.container_id = None  # type: ignore[assignment]
                workspace.host_port = None  # type: ignore[assignment]
                await workspace.save(update_fields=["container_name", "container_id", "host_port", "update_time"])
            _note_replaced_claude_home(workspace.id)

            # Docker 网络配置：默认把工作区放到专用网络上并在宿主侧切断内网访问。
            # 隔离未生效时（best_effort/off）退回改造前的形态——直接挂在 NA 所在的业务网络上。
            #
            # 网络按归属分组：同一网桥内的容器互访是二层交换、不进 iptables，跨租户隔离只能靠
            # 「不同租户不同网络」来做（详见 network_guard 模块 docstring）。分组在**创建容器时**
            # 定下，之后改频道绑定不会让运行中的容器换网，要重建容器才会重新分组。
            #
            # 必须排在写配置文件之前：容器里的 `ANTHROPIC_BASE_URL` 指向 NA 在这张网络上的地址，
            # 网络定下来才知道该写哪个地址。
            network_plan = await prepare_workspace_network(
                docker,
                await resolve_workspace_network_group(workspace),
            )

            # 初始化工作区目录。容器是全新拉起的，中继令牌在这里轮换：
            # 上一代容器若曾把令牌泄漏出去，有效期到此为止。
            await WorkspaceService.init_workspace_dir(
                workspace,
                cc_preset=cc_preset,
                rotate_credential=True,
                controller_address=network_plan.controller_address,
                host_address=network_plan.host_address,
            )

            # SSH 网关：总开关开着且隔离生效时，给这个容器发一把只开网关的钥匙。
            # 钥匙与容器一起轮换：公钥按键写进 metadata，私钥写进
            # `.claude_home/nk-ssh/id_gateway`。总开关关着（或隔离没生效）时，
            # 删掉上次留下的文件与钥匙，容器里不会残留一份指向不存在的网关的配置。
            gateway_enabled = bool(config.SSH_GATEWAY_ENABLED)
            gateway_private_key: str | None = None
            gateway_public_key = ""
            if gateway_enabled and network_plan.gateway_listen_address:
                try:
                    private_text, public_text = ssh_gateway_keys.generate_client_keypair()
                    gateway_private_key = private_text
                    gateway_public_key = ssh_gateway_keys.public_key_line(public_text)
                except Exception as exc:  # noqa: BLE001 - 网关钥匙生成失败不该拦住建容器
                    logger.warning(
                        f"生成工作区 {workspace.id} 的 SSH 网关钥匙失败（这个工作区暂时用不了 SSH）: "
                        f"{type(exc).__name__}: {exc}",
                    )
                if gateway_public_key:
                    await patch_workspace_metadata(workspace, set_keys={"ssh_gateway_client_key": gateway_public_key})
            else:
                try:
                    await remove_nk_ssh_files(workspace.id)
                except Exception as exc:  # noqa: BLE001 - 删不掉最多是旧文件留着
                    logger.warning(f"清理工作区 {workspace.id} 的 nk-ssh 目录失败（已忽略）: {exc}")
                await patch_workspace_metadata(workspace, drop_keys=["ssh_gateway_client_key"])

            if gateway_private_key is not None:
                try:
                    await write_nk_ssh_files(
                        workspace,
                        listen_address=network_plan.gateway_listen_address,
                        private_key=gateway_private_key,
                    )
                except Exception as exc:  # noqa: BLE001 - 写文件失败只让 SSH 不可用
                    logger.warning(
                        f"写工作区 {workspace.id} 的 SSH 网关文件失败（这个工作区暂时用不了 SSH）: "
                        f"{type(exc).__name__}: {exc}",
                    )

            # 生成唯一容器名（查重）
            container_name = _generate_container_name()
            for _ in range(10):
                if not await _container_exists(docker, container_name):
                    break
                container_name = _generate_container_name()
            else:
                raise RuntimeError("无法生成唯一容器名")

            host_port = await _find_free_port()
            # 两个 bind 源都按字面路径、不跟随链接地核对一遍，不是普通目录就拒绝启动（不 resolve）。
            # 修复已经在上面的 `init_workspace_dir` 里做过。这时这个工作区没有容器在跑——前提是
            # 上面清场删掉了**所有**按标签或挂载认出来的容器，而且这次启停拿着这个工作区的锁、
            # 别的启动进不来；以读写方式挂着工作区上级目录的别的容器在下一行拒绝；放行的只有
            # NA 自己与 NapCat（视为可信，见本模块「bind 源」一节）
            await _refuse_foreign_writers(docker, workspace.id)
            try:
                ws_host_dir, claude_home_host_dir = _checked_bind_sources(workspace.id)
            except UnsafeBindSourceError as e:
                workspace.status = "failed"
                workspace.last_error = str(e)  # type: ignore[assignment]
                await workspace.save(update_fields=["status", "last_error", "update_time"])
                raise

            host_tz = _get_host_timezone()
            binds = _bind_list(ws_host_dir, claude_home_host_dir)

            container_config: dict = {
                "Image": image,
                # 归属标签：让 docker-guard 硬化部署能认出这是本部署的容器，从而放行对它的
                # exec（MCP stdio 校验、CC 终端）/logs/inspect 等按 ID 指向的操作。缺了它，
                # 代理重启后无法从快表认出这个长命容器，会把 NA 对自己工作区的 exec 也判成越权。
                # 值取 deployment_scope()（与沙盒网络名/iptables 标记同一套派生，见 core/os_env.py），
                # 便于将来按部署收敛跨部署归属；guard 当前只校验 `nekro.` 前缀。
                # `nekro.workspace.id`：启动前清场、NA 启动时清孤儿都按它认「这个工作区的容器」
                # （升级前建的没有它，按挂载认）
                "Labels": {"nekro.owner.scope": deployment_scope(), WORKSPACE_LABEL: str(workspace.id)},
                "HostConfig": {
                    "Binds": binds,
                    # 容器里不该再有任何提权的路子：镜像里没有 sudo，入口脚本以 root 起步、
                    # `runuser` 降到 appuser（不靠 setuid）；Debian 底自带的 setuid / setgid 程序
                    # （ak3 实测 13 个：su、passwd、mount、umount、chsh、chfn、newgrp、gpasswd、chage、
                    # expiry、unix_chkpwd、ssh-keysign、ssh-agent）由 `no-new-privileges` 让这些位失效。去掉 MKNOD
                    # 让容器里建不了设备节点（宿主侧按名字打开工作区里的文件时，不会撞上一个指向
                    # 设备的节点）。升级前建的容器没有这两项，重启时会提示重建（见 `_restart_locked`）
                    "CapDrop": ["MKNOD"],
                    "SecurityOpt": ["no-new-privileges"],
                    "PortBindings": {
                        f"{config.CC_SANDBOX_INTERNAL_PORT}/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": str(host_port)}
                        ]
                    },
                    # 隔离没生效时（含 NA 跑在宿主上的开发部署）容器挂在默认网络上，
                    # 回连 NA 的中继端点只能经宿主网关，这个别名是它的落点；
                    # 隔离生效时中继地址用的是 NA 在专用网桥上的 IP，这条别名用不到，
                    # 而且发往宿主的包会被守卫的 INPUT 默认拒绝挡掉。
                    "ExtraHosts": ["host.docker.internal:host-gateway"],
                    "RestartPolicy": {"Name": "no"},
                },
                "Env": [
                    f"WORKSPACE_ROOT={CONTAINER_WORKSPACE_PATH}",
                    f"SETTINGS_PATH={CONTAINER_WORKSPACE_PATH}/settings.json",
                    f"RUNTIME_POLICY={workspace.runtime_policy}",
                    "SKIP_PERMISSIONS=true",
                    f"INTERNAL_API_TOKEN={api_token}",
                    f"PORT={config.CC_SANDBOX_INTERNAL_PORT}",
                    "HOST=0.0.0.0",
                    f"TZ={host_tz}",
                ],
                "ExposedPorts": {f"{config.CC_SANDBOX_INTERNAL_PORT}/tcp": {}},
            }

            if network_plan.network_mode:
                container_config["HostConfig"]["NetworkMode"] = network_plan.network_mode

            container = await docker.containers.create_or_replace(
                name=container_name,
                config=container_config,
            )
            try:
                await container.start()

                # 通过 inspect 获取 container_id
                info = await container.show()
                container_id: str = info["Id"][:12]

                # 更新 DB
                workspace.container_name = container_name
                workspace.container_id = container_id
                workspace.host_port = host_port
                workspace.status = "active"
                workspace.last_error = None  # type: ignore[assignment]
                update_fields = [
                    "container_name",
                    "container_id",
                    "host_port",
                    "status",
                    "last_error",
                    "update_time",
                ]
                await workspace.save(
                    update_fields=update_fields
                )
            except BaseException:
                # 建好了却没记进库：库里不认识它，停止 / 重建都碰不到它，它就成了网桥上一个没人
                # 看着的执行环境。当场删掉；删不掉的话它带着工作区标签，下次启动前清场或 NA 启动时
                # 清孤儿会再删一次
                logger.error(f"工作区 {workspace.id} 的新容器 {container_name} 已建好但没能记进库，删掉它")
                try:
                    await container.delete(force=True)
                except Exception as exc:  # noqa: BLE001 - 原来的异常更要紧
                    logger.error(f"删除没记进库的容器 {container_name} 失败（下次启动前或 NA 启动时再删）: {exc}")
                raise

            logger.info(f"容器已启动: {container_name} (port={host_port})")
        finally:
            await docker.close()

        # 等待健康检查（在 docker.close() 后，避免泄漏连接）
        timeout = config.CC_SANDBOX_STARTUP_TIMEOUT
        healthy = await SandboxContainerManager._wait_healthy(workspace.api_endpoint, timeout)
        if not healthy:
            workspace.status = "failed"
            workspace.last_error = f"容器启动超时（{timeout}s）"  # type: ignore[assignment]
            await workspace.save(update_fields=["status", "last_error", "update_time"])
            raise RuntimeError(f"cc-sandbox 容器健康检查超时: {workspace.container_name}")

        # 健康检查通过之后才登记：容器能连上，网关才值得把「这是哪个工作区」的事实记下来。
        # 任何一步失败都只记 WARNING：工作区本身是好的，只是 SSH 用不了。
        if bool(config.SSH_GATEWAY_ENABLED):
            try:
                if not gateway_public_key or not network_plan.gateway_listen_address:
                    raise RuntimeError("网关钥匙或监听地址缺失")
                await fix_permissions_in_container(workspace.container_name or "")
                peer_address = extract_network_ipv4(info, network_plan.network_mode)
                if not peer_address:
                    raise RuntimeError(f"容器在 {network_plan.network_mode!r} 上没有 IPv4 地址")
                from nekro_agent.services.workspace.client import CCSandboxClient

                image_version = await CCSandboxClient(workspace).get_cc_version()
                ssh_gateway_registry.register_peer(
                    ContainerPeer(
                        workspace_id=int(workspace.id),
                        peer_address=peer_address,
                        listen_address=network_plan.gateway_listen_address,
                        client_public_key=gateway_public_key,
                        isolation_ok=network_plan.isolated,
                        image_ok=image_supports_gateway(image_version),
                    ),
                )
                ssh_gateway_registry.add_listen_address(network_plan.gateway_listen_address)
            except Exception as exc:  # noqa: BLE001 - 登记失败只让这个工作区用不了 SSH
                logger.warning(
                    f"登记工作区 {workspace.id} 的 SSH 网关容器失败（这个工作区暂时用不了 SSH）: "
                    f"{type(exc).__name__}: {exc}",
                )

        return workspace

    @staticmethod
    async def stop(workspace: DBWorkspace) -> None:
        """停止容器

        正在启停时排在它后面，不拒绝：停止是收紧的方向（撤销平台方凭据时靠它把 Key 从进程环境里
        拿掉），必须停得下来。
        """
        async with _transition(workspace, ("active", "stopped", "failed"), "停止", wait=True, take_stale=True):
            await SandboxContainerManager._stop_locked(workspace)

    @staticmethod
    async def _stop_locked(workspace: DBWorkspace) -> None:
        # 容器停了，网关就不该再按旧地址认它（地址可能被复用给别的工作区）
        ssh_gateway_registry.unregister_workspace(workspace.id)
        docker = None
        try:
            docker = aiodocker.Docker()
            if workspace.container_name:
                try:
                    container = await docker.containers.get(workspace.container_name)
                    await container.stop(t=10)
                except Exception as e:
                    logger.warning(f"停止容器失败（忽略）: {workspace.container_name}: {e}")
            # 同一工作区别的还在跑的容器（并发启动、升级前留下的孤儿）一起停掉：它们与库里记的
            # 那个一样能写这个工作区，「停止」之后不该还有一个在跑
            for other in await _workspace_containers(docker, workspace.id):
                summary = _summary(other)
                if workspace.container_name and workspace.container_name in _summary_names(summary):
                    continue
                if str(summary.get("State") or "").lower() != "running":
                    continue
                label = ", ".join(sorted(_summary_names(summary))) or str(summary.get("Id") or "")[:12]
                logger.warning(f"工作区 {workspace.id} 还有别的容器 {label} 在跑，一并停掉")
                try:
                    await other.stop(t=10)
                except Exception as e:  # noqa: BLE001 - 停不掉的下次启动前清场再删
                    logger.error(f"停止工作区 {workspace.id} 的多余容器 {label} 失败: {e}")
        except Exception as e:  # noqa: BLE001 - Docker 不可用时照旧只把状态写成 stopped
            logger.warning(f"停止工作区 {workspace.id} 的容器时 Docker 出错（忽略）: {e}")
        finally:
            if docker is not None:
                with contextlib.suppress(Exception):
                    await docker.close()

        # 容器停了就没有合法的中继调用方了，令牌当场作废：中继端点挂在 NA 的主端口上，
        # 不随容器一起消失，留着它等于「工作区停着、额度还能被烧」。
        # 唯一的启动入口 `sandbox/start` 走 create_and_start，会重新签发一把。
        # 网关专用钥匙同理：下一次启动会重新生成，旧的留着只会让"这个容器还能连网关"的
        # 事实多活一段时间。按键删：别处同时整份写回 metadata 也写不回来（见 `GUARDED_METADATA_KEYS`）
        await patch_workspace_metadata(workspace, drop_keys=[METADATA_TOKEN_KEY, "ssh_gateway_client_key"])

        workspace.status = "stopped"
        await workspace.save(update_fields=["status", "update_time"])

    @staticmethod
    async def remove(workspace: DBWorkspace) -> None:
        """删除容器（工作区被删除时用）

        与 `stop()` 的差别是它不回写数据库：调用方紧接着就要把工作区那一行删掉。
        只 `stop` 是不够的 —— 工作区行一没，容器名/端口/磁盘就再没有任何入口能碰到，
        只能到宿主机上 `docker rm` 手工收拾（与资源绑定的悬空行同一种形态）。

        Docker 不可用或容器早已不在都只记录告警：删除工作区不该因为容器层的状态而失败。
        **连 `aiodocker.Docker()` 的构造都在 try 里** —— 宿主上没有 Docker 时它自己就抛，
        而这条路径改前只在 `status == "active"` 时才碰 Docker，把它挪出 try 等于
        给没有 Docker 的部署新增一条"工作区删不掉"的 500。

        排在正在做的启停后面，在锁里把状态比较替换成 `deleting`（启动 / 重启 / 重建的起点都不含它）：
        这边删完、调用方删行之前，别的请求起不来一个新容器。正在别处启停（库里是没过期的
        `transitioning`）就抛 `WorkspaceBusyError`。从没建过容器（没有容器名）的工作区不碰 Docker，
        库里没记的同工作区容器由 NA 启动时的孤儿清理收走（工作区已不存在）。
        """
        # 容器（如果还在）接下来会被删掉；登记表里的旧地址必须立刻失效
        ssh_gateway_registry.unregister_workspace(workspace.id)
        async with _workspace_lock(workspace.id):
            now = timezone.now()
            changed = await DBWorkspace.filter(
                id=workspace.id,
                status__in=["active", "stopped", "failed", DELETING],
            ).update(status=DELETING, update_time=now)
            if not changed:
                cutoff = now - timedelta(seconds=STALE_TRANSITION_SECONDS)
                changed = await DBWorkspace.filter(
                    id=workspace.id,
                    status=TRANSITIONING,
                    update_time__lt=cutoff,
                ).update(status=DELETING, update_time=now)
            if not changed and await DBWorkspace.exists(id=workspace.id):
                raise WorkspaceBusyError(f"工作区 {workspace.id} 正在启停，请稍后再删除")
            workspace.status = DELETING
            if not workspace.container_name:
                return
            docker = None
            try:
                docker = aiodocker.Docker()
                if workspace.container_name:
                    try:
                        container = await docker.containers.get(workspace.container_name)
                        with contextlib.suppress(Exception):
                            await container.stop(t=10)
                        await container.delete(force=True)
                        logger.info(f"已删除工作区容器: {workspace.container_name}")
                    except Exception as e:
                        logger.warning(f"删除工作区容器失败（忽略）: {workspace.container_name}: {e}")
                # 同一工作区别的容器（并发启动、升级前留下的孤儿）一起删掉
                for other in await _workspace_containers(docker, workspace.id):
                    label = ", ".join(sorted(_summary_names(_summary(other))))
                    try:
                        await other.delete(force=True)
                        logger.info(f"已删除工作区 {workspace.id} 的多余容器: {label}")
                    except Exception as e:  # noqa: BLE001 - 删不掉的 NA 启动时按孤儿再删
                        logger.warning(f"删除工作区 {workspace.id} 的多余容器失败（忽略）: {label}: {e}")
            except Exception as e:
                logger.warning(f"删除工作区 {workspace.id} 的容器失败（忽略）: {e}")
            finally:
                if docker is not None:
                    with contextlib.suppress(Exception):
                        await docker.close()

    @staticmethod
    async def restart(workspace: DBWorkspace) -> None:
        """重启容器（进程级恢复），重启前同步配置文件

        不用 Docker 的 `restart`：它在旧容器还活着的时候就得把配置修好，修完到 Docker 重新
        挂载之间容器能把 `.claude_home` 换成链接。这里拆成「停 → 修 → 核对 → 启动」，
        见本模块「bind 源」一节。
        """
        if not workspace.container_name:
            raise RuntimeError("容器未运行")
        async with _transition(workspace, ("active", "failed"), "重启"):
            await SandboxContainerManager._restart_locked(workspace)

    @staticmethod
    async def _restart_locked(workspace: DBWorkspace) -> None:
        if not workspace.container_name:
            raise RuntimeError("容器未运行")

        # 重启前重新写入配置文件，确保数据库与磁盘一致。
        # 中继令牌一并轮换：重启会杀掉容器里所有在跑的 `claude` 进程，本来就没有在途请求可打断。
        cc_preset = await SandboxContainerManager._resolve_preset(workspace)

        docker = aiodocker.Docker()
        try:
            container = await docker.containers.get(workspace.container_name)
            # 重启沿用容器创建时记下的挂载，这次核对过的 bind 源管不到它：早先的版本按 `resolve()`
            # 建过容器，那时 `.claude_home` 若已被换成链接，宿主别处的目录就固化在这份挂载里，
            # 重启照挂不误。对不上就不重启，停掉它、要求重建
            info = await container.show()
            mismatch = _mount_mismatch(info, workspace.id)
            if mismatch:
                logger.error(f"工作区 {workspace.id} 的容器 {workspace.container_name} 挂载与预期不一致，拒绝重启: {mismatch}")
                ssh_gateway_registry.unregister_workspace(workspace.id)
                await _stop_if_running(docker, workspace.container_name)
                workspace.status = "failed"
                workspace.last_error = "容器的挂载与预期不一致，请重建容器"  # type: ignore[assignment]
                await workspace.save(update_fields=["status", "last_error", "update_time"])
                raise UnsafeBindSourceError(f"工作区 {workspace.id} 的容器挂载与预期不一致，请重建容器")
            # 升级前建的容器没有 `CapDrop: MKNOD` / `no-new-privileges`，重启补不上（只能重建）。
            # 不拒绝（否则换版后所有工作区都得先重建），提示一句
            missing = _missing_hardening(info)
            if missing:
                logger.warning(
                    f"工作区 {workspace.id} 的容器 {workspace.container_name} 是升级前建的，缺少 {', '.join(missing)}；"
                    "重启补不上，建议重建容器",
                )

            # 中继地址按容器**实际所在**的网络重新反查一次，而不是沿用 metadata 里那份。
            # 存着的地址会过期：NA 容器被重建后在这张网桥上的 IP 可能变，改造前建的容器
            # 更是压根没有这份地址。重启是运维遇到「CC 连不上模型」时的第一反应，
            # 这条路径修不回来的话就没有别的路径能修了（只剩重建容器）。
            # 反查要在停容器之前做：停了的容器在网络上没有地址。
            controller_address, host_address = await resolve_container_relay_addresses(
                docker,
                workspace.container_name,
            )

            # 容器停下来之前先忘掉它的网关登记：停着的这段时间里旧地址可能被别的容器拿到
            ssh_gateway_registry.unregister_workspace(workspace.id)
            await _stop_if_running(docker, workspace.container_name)
            # 同一工作区别的容器（并发启动、升级前留下的孤儿）停掉删掉，只留要重启的这一个；
            # 删完还有在跑的就放弃重启（异常由外层记成 failed）
            await _evict_workspace_containers(docker, workspace.id, keep_name=workspace.container_name)
            _note_replaced_claude_home(workspace.id)

            await WorkspaceService.init_workspace_dir(
                workspace,
                cc_preset=cc_preset,
                rotate_credential=True,
                controller_address=controller_address,
                host_address=host_address,
            )

            # 以读写方式挂着工作区上级目录的别的容器（NA 自己与 NapCat 除外）：拒绝重启
            await _refuse_foreign_writers(docker, workspace.id)
            try:
                _checked_bind_sources(workspace.id)
            except UnsafeBindSourceError as e:
                # 容器保持停着：管理员在宿主上看过之后再重建
                workspace.status = "failed"
                workspace.last_error = str(e)  # type: ignore[assignment]
                await workspace.save(update_fields=["status", "last_error", "update_time"])
                raise
            await container.start()
            workspace.status = "active"
            workspace.last_error = None  # type: ignore[assignment]
            await workspace.save(update_fields=["status", "last_error", "update_time"])
        finally:
            await docker.close()

        # 等待健康
        timeout = config.CC_SANDBOX_STARTUP_TIMEOUT
        healthy = await SandboxContainerManager._wait_healthy(workspace.api_endpoint, timeout)
        if not healthy:
            workspace.status = "failed"
            workspace.last_error = "容器重启后健康检查超时"  # type: ignore[assignment]
            await workspace.save(update_fields=["status", "last_error", "update_time"])
            return

        # 重启后容器在网桥上的地址可能变了：按实际地址重新登记 SSH 网关（钥匙沿用 metadata 里那把，
        # 重启不轮换它）。先注销旧登记，免得旧地址被别的容器拿到后还被认作本工作区；登记不上只让 SSH 暂时用不了。
        if bool(config.SSH_GATEWAY_ENABLED):
            ssh_gateway_registry.unregister_workspace(workspace.id)
            docker = aiodocker.Docker()
            try:
                info = await (await docker.containers.get(workspace.container_name)).show()
                await fix_permissions_in_container(workspace.container_name)
                await SandboxContainerManager._register_recovered_peer(docker, workspace, info)
            except Exception as exc:  # noqa: BLE001 - 重启已经成功，登记失败只影响 SSH
                logger.warning(
                    f"工作区 {workspace.id} 重启后重新登记 SSH 网关失败（这个工作区暂时用不了 SSH）: "
                    f"{type(exc).__name__}: {exc}",
                )
            finally:
                await docker.close()

    @staticmethod
    async def rebuild(workspace: DBWorkspace) -> DBWorkspace:
        """彻底重建容器（不依赖 cc-sandbox API，纯 Docker 操作）

        镜像不在本地时什么都不动（旧容器留着）；旧容器删不掉就放弃重建、保留库里的容器名
        （记成 failed），不把一个还在的容器从库里抹掉。
        """
        await SandboxContainerManager._require_image(workspace)
        async with _transition(workspace, ("active", "stopped", "failed"), "重建"):
            return await SandboxContainerManager._rebuild_locked(workspace)

    @staticmethod
    async def _rebuild_locked(workspace: DBWorkspace) -> DBWorkspace:
        # 旧容器马上要被删掉：先忘掉它的登记，别让旧地址在新容器起来前继续被认出来
        ssh_gateway_registry.unregister_workspace(workspace.id)
        docker = aiodocker.Docker()
        try:
            if workspace.container_name:
                try:
                    container = await docker.containers.get(workspace.container_name)
                except aiodocker.exceptions.DockerError as e:
                    if e.status != 404:
                        raise
                    container = None
                if container is not None:
                    with contextlib.suppress(Exception):
                        await container.stop(t=10)
                    try:
                        await container.delete(force=True)
                    except aiodocker.exceptions.DockerError as e:
                        if e.status != 404:
                            logger.error(f"删除旧容器 {workspace.container_name} 失败，放弃重建: {e}")
                            raise RuntimeError(f"删除旧容器失败，放弃重建: {e}") from e
                    logger.info(f"已删除旧容器: {workspace.container_name}")
        finally:
            await docker.close()

        # 清空容器信息（状态保持 transitioning，由下面的启动写最终状态）
        workspace.container_name = None  # type: ignore[assignment]
        workspace.container_id = None  # type: ignore[assignment]
        workspace.host_port = None  # type: ignore[assignment]
        await workspace.save(update_fields=["container_name", "container_id", "host_port", "update_time"])

        return await SandboxContainerManager._create_and_start_locked(workspace)

    @staticmethod
    async def get_logs(workspace: DBWorkspace, tail: int = 100) -> str:
        """获取容器日志"""
        if not workspace.container_name:
            return ""
        docker = aiodocker.Docker()
        try:
            container = await docker.containers.get(workspace.container_name)
            logs = await container.log(stdout=True, stderr=True, tail=tail)
            return "".join(logs)
        except Exception as e:
            logger.warning(f"获取容器日志失败: {e}")
            return f"[获取日志失败: {e}]"
        finally:
            await docker.close()

    @staticmethod
    async def _wait_healthy(endpoint: str, timeout: int = 60) -> bool:
        """轮询 GET /health 直到返回 healthy 或超时"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(f"{endpoint}/health")
                    if resp.status_code == 200 and resp.json().get("status") == "healthy":
                        return True
            except Exception:
                pass
            await asyncio.sleep(2)
        return False

    @staticmethod
    async def _register_recovered_peer(docker: aiodocker.Docker, workspace: DBWorkspace, info: dict) -> None:
        """NA 重启后按仍在跑的容器补登记网关容器

        地址按容器实际所在的网络反查（重启不重建容器，网络在创建时就写死了）。
        没有网关钥匙的容器是网关开启前建的：不在运行中补发，留一条 WARNING 让人知道
        要重建容器。任何失败都只记 WARNING——启动恢复的主职责是修正工作区状态。
        """
        public_key = str((workspace.metadata or {}).get("ssh_gateway_client_key") or "").strip()
        if not public_key:
            logger.warning(
                f"工作区 {workspace.id} 的容器是在网关开启前建的，重建后才能用 SSH（本次不补登记）",
            )
            return
        try:
            network_names = list((info.get("NetworkSettings") or {}).get("Networks") or {})
            if not network_names:
                logger.warning(f"工作区 {workspace.id} 的容器没有网络信息，暂不补登记 SSH 网关")
                return
            network_name = str(network_names[0])
            peer_address = extract_network_ipv4(info, network_name)
            if not peer_address:
                logger.warning(f"工作区 {workspace.id} 的容器在 {network_name} 上没有 IPv4 地址，暂不补登记 SSH 网关")
                return
            controller_address, host_address = await resolve_container_relay_addresses(docker, workspace.container_name or "")
            listen_address = controller_address if OsEnv.RUN_IN_DOCKER else host_address
            if not listen_address:
                logger.warning(f"读不出工作区 {workspace.id} 的 SSH 网关监听地址，暂不补登记")
                return
            from nekro_agent.services.workspace.client import CCSandboxClient

            image_version = await CCSandboxClient(workspace).get_cc_version()
            ssh_gateway_registry.register_peer(
                ContainerPeer(
                    workspace_id=int(workspace.id),
                    peer_address=peer_address,
                    listen_address=listen_address,
                    client_public_key=public_key,
                    isolation_ok=True,
                    image_ok=image_supports_gateway(image_version),
                ),
            )
            ssh_gateway_registry.add_listen_address(listen_address)
        except Exception as exc:  # noqa: BLE001 - 补登记失败只让这个工作区的 SSH 暂时不可用
            logger.warning(
                f"补登记工作区 {workspace.id} 的 SSH 网关容器失败（这个工作区暂时用不了 SSH）: "
                f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    async def recover_on_startup() -> None:
        """NA 启动时把库里的工作区状态与 Docker 对齐，并收拾上一次运行留下的容器

        1. 删掉孤儿 CC 容器：工作区已经不存在，或者库里记的不是它（并发启动、升级前「健康检查
           超时 → 再启动」留下的）。认法见 `_remove_orphan_containers`；
        2. 逐个工作区核对库里记的那个容器（每个工作区拿着它的锁做，不与刚进来的启停撞上）：
           - 挂载与预期不一致（见 `_mount_mismatch`）：记 ERROR、停掉、标 failed，要求重建；
           - 库里是 active 或残留的 transitioning（NA 在启停中途退出）：容器在跑就是 active，
             不在或没在跑就降级 stopped；
           - 库里是 stopped / failed 但容器在跑：没人看着它，停掉。
        """
        docker = aiodocker.Docker()
        try:
            try:
                removed = await _remove_orphan_containers(docker)
                if removed:
                    logger.warning(f"NA 启动时删掉了 {removed} 个孤儿 CC 容器")
            except Exception as exc:  # noqa: BLE001 - 清孤儿失败不拦住状态对齐
                logger.error(f"NA 启动时清理孤儿 CC 容器失败: {type(exc).__name__}: {exc}")

            workspaces = await DBWorkspace.filter(status__in=["active", TRANSITIONING, "stopped", "failed"]).all()
            for workspace in workspaces:
                async with _workspace_lock(workspace.id):
                    try:
                        await workspace.refresh_from_db()
                    except Exception:  # noqa: BLE001 - 行已经被删掉
                        continue
                    await SandboxContainerManager._recover_one(docker, workspace)
        finally:
            await docker.close()

    @staticmethod
    async def _recover_one(docker: aiodocker.Docker, workspace: DBWorkspace) -> None:
        expected_running = workspace.status in ("active", TRANSITIONING)
        if not workspace.container_name:
            if expected_running:
                workspace.status = "stopped"
                await workspace.save(update_fields=["status", "update_time"])
            return
        # 只有 404 才是「不存在」。别的错误（Docker 一时没响应、代理超时）重试几次；还不行就
        # 按名字杀掉它、标 failed——当成不存在的话，一个核对不了挂载的容器会接着跑下去
        info: Optional[Dict[str, Any]] = None
        last_error: Optional[BaseException] = None
        for attempt in range(3):
            try:
                container = await docker.containers.get(workspace.container_name)
                info = await container.show()
                break
            except aiodocker.exceptions.DockerError as exc:
                if exc.status == 404:
                    if expected_running:
                        workspace.status = "stopped"
                        await workspace.save(update_fields=["status", "update_time"])
                        logger.info(f"workspace {workspace.id} 容器不存在，降级为 stopped")
                    return
                last_error = exc
            except Exception as exc:  # noqa: BLE001 - 网络层的错误一样重试
                last_error = exc
            await asyncio.sleep(_INSPECT_RETRY_DELAY * (attempt + 1))
        if info is None:
            logger.error(
                f"NA 启动时检查工作区 {workspace.id} 的容器 {workspace.container_name} 失败，按名字停掉它、标 failed: {last_error}",
            )
            with contextlib.suppress(Exception):
                await docker.containers.container(workspace.container_name).kill()
            workspace.status = "failed"
            workspace.last_error = f"NA 启动时检查容器失败: {last_error}"[:1000]  # type: ignore[assignment]
            await workspace.save(update_fields=["status", "last_error", "update_time"])
            return

        running = bool((info.get("State") or {}).get("Running"))
        mismatch = _mount_mismatch(info, workspace.id)
        if mismatch:
            logger.error(
                f"工作区 {workspace.id} 的容器 {workspace.container_name} 挂载与预期不一致，停掉并标 failed，需要重建: {mismatch}",
            )
            if running:
                try:
                    await container.stop(t=10)
                except Exception as exc:  # noqa: BLE001 - 停不掉也要把状态标出来
                    logger.error(f"停止挂载不一致的容器 {workspace.container_name} 失败: {exc}")
            workspace.status = "failed"
            workspace.last_error = "容器的挂载与预期不一致，请重建容器"  # type: ignore[assignment]
            await workspace.save(update_fields=["status", "last_error", "update_time"])
            return

        if expected_running:
            if running:
                logger.debug(f"workspace {workspace.id} 容器运行中: {workspace.container_name}")
                if workspace.status != "active":
                    workspace.status = "active"
                    await workspace.save(update_fields=["status", "update_time"])
                if bool(config.SSH_GATEWAY_ENABLED):
                    await SandboxContainerManager._register_recovered_peer(docker, workspace, info)
            else:
                workspace.status = "stopped"
                await workspace.save(update_fields=["status", "update_time"])
                logger.info(f"workspace {workspace.id} 容器已停止，降级为 stopped")
            return

        if running:
            logger.warning(f"工作区 {workspace.id} 在库里是 {workspace.status}，容器 {workspace.container_name} 却在跑，停掉它")
            try:
                await container.stop(t=10)
            except Exception as exc:  # noqa: BLE001 - 停不掉只记一笔
                logger.error(f"停止容器 {workspace.container_name} 失败: {exc}")
