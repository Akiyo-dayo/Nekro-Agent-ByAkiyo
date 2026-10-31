"""内置 NapCat 容器与数据目录的孤儿盘点与回收（平台方专属）

## 这个模块要解决的事故

`console/instance_manager.py:purge_instance()` 会级联清掉十二张表与向量库，
**但它从不碰容器**；`container_manager.delete_container()` 又有意保留
`{DATA_DIR}/napcat_data/{instance_key}/`（「重建容器后无需重新扫码」）。两条合起来：
删掉一个内置 NapCat 实例之后，容器带着 `restart: unless-stopped` 永远活下去，
数据目录也永远留着，而**库里已经没有任何一行指得到它们**。

测试部署上的实证（2026-08-19）：`nekro_napcat_inst1` / `inst2` 建于 07-31，库里同名的
`inst1` / `inst2` 两行 `create_time` 是 08-13 —— 建容器的那两行早就没了。两个容器各自
卡在无限刷登录二维码的循环里，合计吃掉 1.2 GB 内存、5.7 GB 磁盘，零产出。

`purge_instance()` 那一侧已经补上「删实例顺手删容器」，堵的是**新的**孤儿。本模块负责
**已经漏出去的那一批**：它们没有 DB 行，于是 `container_manager` 里每一个以
`DBAdapterInstance` 为入口的函数都够不到它们，WebUI 上一个出口都没有。

## 判据：谁是孤儿

以宿主机上的**事实**为准（容器归属标签 + 数据目录），再拿库里的行去反查：

| 库里的行 | 判定 |
|---|---|
| 没有任何行 | 孤儿，可回收（`ORPHANED`） |
| 有行，且 `provider == builtin_napcat` | **不是**孤儿，拒绝（`IN_USE`） |
| 有行，但 provider 已改成别的形态 | 孤儿，可回收，理由单独标出（`PROVIDER_CHANGED`） |

第三档是测试部署上那两个的真实形态：实例还在，协议端已经搬去外部机器，容器于是变成一个
没人连、也永远连不上的空转进程。**必须与第一档区分开**——运维要能看出这不是
「实例被删了」而是「实例改了接入方式」，两者的下一步不同（后者可能只是忘了收尾，
也可能是打算迁回来）。

## 为什么判定要在执行前重跑

与孤儿 iptables 规则那一套同源（`routers/console/network_rules.py`）：预览与确认之间
完全可能有人把该实例重新建成内置 NapCat 形态。**预览时的结论一律不作数。**

## 安全边界

- 容器侧只认带 `nekro.napcat.instance` 标签、且归属校验通过的容器，直接复用
  `container_manager._assert_owned`（含过渡期双识别）。**不在这里另写一套判据**——
  本项目已经五次栽在「同一个判据的第二处实现」上。
- 目录侧只扫 `{DATA_DIR}/napcat_data/` 的**直接子目录**，名字必须通过
  `validate_instance_key()`；`RESERVED_DATA_DIR_NAMES` 里那两个是单实例年代的布局
  （`napcat_data/QQ` 与 `napcat_data/napcat`，见 `core/os_env.py` 的
  `NAPCAT_TEMPFILE_DIR` / `NAPCAT_ONEBOT_ADAPTER_DIR`），**永远不是实例目录**。
  漏掉这一条会把还在用的单实例部署的登录态当孤儿删掉。
- 删数据目录前再确认一次它 `resolve()` 之后仍落在 `napcat_data/` 之下，
  符号链接指到外面时拒绝。
"""

import asyncio
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import aiodocker
from aiodocker.exceptions import DockerError

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.schemas.console import InstanceProvider
from nekro_agent.services.console.container_manager import (
    INSTANCE_KEY_PATTERN,
    LABEL_INSTANCE,
    _assert_owned,  # noqa: PLC2701 — 归属判据只此一份，见模块 docstring「安全边界」
    build_container_name,
    get_owner_tag,
)

logger = get_sub_logger("console.napcat_orphans")


RESERVED_DATA_DIR_NAMES: frozenset[str] = frozenset({"napcat", "QQ"})
"""`napcat_data/` 下**不是**实例目录的名字。

单实例年代的布局把 QQ 登录态与 OneBot 适配器配置直接放在 `napcat_data/QQ` 与
`napcat_data/napcat`（`core/os_env.py:NAPCAT_TEMPFILE_DIR` / `NAPCAT_ONEBOT_ADAPTER_DIR`）。
`QQ` 带大写、过不了 `INSTANCE_KEY_PATTERN`，但 **`napcat` 是全小写的，会过**——
少了这个集合，一台还在用单实例布局的部署会把自己的登录态判成孤儿。
"""


class OrphanVerdict(str, Enum):
    """一个 instance_key 在宿主机上的残留物的判定"""

    ORPHANED = "orphaned"  # 库里没有任何一行，可回收
    PROVIDER_CHANGED = "provider_changed"  # 行还在，但已不是内置 NapCat 形态，可回收
    IN_USE = "in_use"  # 行还在且仍是内置 NapCat 形态，拒绝回收


RECLAIMABLE_VERDICTS: frozenset[OrphanVerdict] = frozenset(
    {OrphanVerdict.ORPHANED, OrphanVerdict.PROVIDER_CHANGED},
)


@dataclass(frozen=True)
class NapcatOrphanCandidate:
    """宿主机上一份 NapCat 残留物的完整盘点结果

    Attributes:
        instance_key: 实例标识（来自容器标签或数据目录名）
        verdict: 判定，见 `OrphanVerdict`
        reason: 判定的人类可读理由，拒绝时是「为什么不能删」
        container_name: 容器名；没有容器时为空串
        container_state: Docker 报的容器状态；没有容器时为空串
        container_restart_policy: 重启策略——孤儿之所以能永远活着就是因为它
        data_dir: 数据目录绝对路径；目录不存在时为 None
        data_dir_bytes: 数据目录占用字节数；目录不存在时为 0
    """

    instance_key: str
    verdict: OrphanVerdict
    reason: str
    container_name: str
    container_state: str
    container_restart_policy: str
    data_dir: Optional[str]
    data_dir_bytes: int

    @property
    def reclaimable(self) -> bool:
        return self.verdict in RECLAIMABLE_VERDICTS


@dataclass(frozen=True)
class NapcatOrphanPlan:
    """一次孤儿盘点的完整结果

    Attributes:
        scope: 本部署当前的归属作用域，与容器标签同源
        docker_available: 有没有读到容器清单
        docker_unavailable_reason: 读不到时的原因（目录侧的结论仍然有效）
        candidates: 全部候选，含拒绝项——运维要看得见「有东西但不让删」
        reclaimable_bytes: 可回收项的数据目录合计字节数
    """

    scope: str
    docker_available: bool
    docker_unavailable_reason: str
    candidates: Tuple[NapcatOrphanCandidate, ...]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(c.data_dir_bytes for c in self.candidates if c.reclaimable)


@dataclass(frozen=True)
class NapcatOrphanOutcome:
    """一次回收的执行结果

    `container_removed` / `data_dir_removed` 分开报：删掉容器但数据目录删失败是
    **正常且可接受**的中间态（内存回来了，磁盘没有），运维需要知道还差哪一半。
    """

    instance_key: str
    verdict: OrphanVerdict
    container_removed: bool
    data_dir_removed: bool
    freed_bytes: int
    failures: Tuple[str, ...]


class NapcatOrphanRefused(RuntimeError):
    """拒绝回收某个 instance_key

    携带**具体理由**而不是一句「不允许」：拦住之后运维要能知道下一步做什么。
    """

    def __init__(self, instance_key: str, reasons: Sequence[str]) -> None:
        self.instance_key = instance_key
        self.reasons: Tuple[str, ...] = tuple(reasons)
        super().__init__(f"拒绝回收 NapCat 残留 {instance_key}: " + "；".join(self.reasons))


# ---------------------------------------------------------------------------
# 盘点
# ---------------------------------------------------------------------------


def napcat_data_root() -> Path:
    """`napcat_data/` 根目录。

    调用时取而不在 import 期定死：测试要能 monkeypatch `OsEnv.DATA_DIR`
    （§2.9：fake 里写死由环境变量派生的值，换台机器就在测另一件事）。
    """
    return Path(OsEnv.DATA_DIR) / "napcat_data"


def _directory_size(directory: Path) -> int:
    """目录占用字节数；读不到的条目跳过而不是让整次盘点失败"""
    total = 0
    for root, _dirs, files in os.walk(directory, onerror=lambda _e: None):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _scan_data_dirs() -> Dict[str, Path]:
    """列出 `napcat_data/` 下所有**看起来像实例目录**的直接子目录"""
    root = napcat_data_root()
    found: Dict[str, Path] = {}
    if not root.is_dir():
        return found
    try:
        entries = list(root.iterdir())
    except OSError as e:
        logger.warning(f"读取 NapCat 数据目录失败: {e}")
        return found
    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name
        if name in RESERVED_DATA_DIR_NAMES:
            # 单实例年代的布局，不是实例目录。见 RESERVED_DATA_DIR_NAMES 的说明。
            continue
        if not INSTANCE_KEY_PATTERN.match(name):
            continue
        found[name] = entry
    return found


async def _scan_containers() -> Tuple[Dict[str, Dict[str, Any]], bool, str]:
    """列出本部署所有带 NapCat 归属标签、且归属校验通过的容器

    返回 `(instance_key -> inspect 结果, 是否读到了容器清单, 读不到时的原因)`。
    Docker 不可用不应让整次盘点失败：目录侧的结论仍然有效，而且**目录才是磁盘大头**。
    """
    owned: Dict[str, Dict[str, Any]] = {}
    try:
        docker = aiodocker.Docker()
    except Exception as e:  # noqa: BLE001 — 连不上守护进程的形态很多，一律降级
        return owned, False, f"无法连接 Docker 守护进程: {e}"

    try:
        try:
            containers = await docker.containers.list(all=True)
        except (DockerError, OSError) as e:
            return owned, False, f"读取容器清单失败: {e}"

        for container in containers:
            try:
                info = await container.show()
            except (DockerError, OSError):
                continue
            labels = (info.get("Config") or {}).get("Labels") or {}
            instance_key = str(labels.get(LABEL_INSTANCE) or "")
            if not instance_key or not INSTANCE_KEY_PATTERN.match(instance_key):
                continue
            try:
                # 归属判定只此一份：容器名模板 + 双标签识别 + 过渡期挂载点确认。
                _assert_owned(info, instance_key, build_container_name(instance_key))
            except Exception:  # noqa: BLE001 — 不属于本部署，静默跳过（别人的容器不该出现在这里）
                continue
            owned[instance_key] = info
    finally:
        await docker.close()

    return owned, True, ""


async def _live_builtin_keys(keys: Set[str]) -> Dict[str, str]:
    """反查这些 instance_key 在库里的形态

    返回 `instance_key -> provider`（库里存在的那些）。**不按 `adapter_key` 过滤**：
    只要有任意一行认领了这个 key 且仍是内置形态，就不能删——fail-closed。
    """
    if not keys:
        return {}
    rows = await DBAdapterInstance.filter(instance_key__in=list(keys)).values_list(
        "instance_key",
        "provider",
    )
    live: Dict[str, str] = {}
    for instance_key, provider in rows:
        normalized = str(provider or "").strip().lower()
        # 同一个 key 有多行时，内置形态优先——只要有一行还在用它，就判 IN_USE
        if live.get(instance_key) == InstanceProvider.BUILTIN_NAPCAT.value:
            continue
        live[instance_key] = normalized
    return live


def _judge(instance_key: str, provider: Optional[str]) -> Tuple[OrphanVerdict, str]:
    """把「库里的行」翻译成判定与理由。判据表见模块 docstring。"""
    if provider is None:
        return (
            OrphanVerdict.ORPHANED,
            "库里已经没有任何实例认领这个标识，容器与数据目录都不会再被用到",
        )
    if provider == InstanceProvider.BUILTIN_NAPCAT.value:
        return (
            OrphanVerdict.IN_USE,
            "该实例仍是内置 NapCat 形态，容器与登录态正在使用中",
        )
    return (
        OrphanVerdict.PROVIDER_CHANGED,
        f"该实例已改为 '{provider}' 形态接入，这个容器不再承载它的连接（登录态删除后无法恢复，"
        "若打算迁回内置形态请先不要回收）",
    )


async def plan_napcat_orphan_cleanup() -> NapcatOrphanPlan:
    """盘点宿主机上所有 NapCat 残留物。**只读，不做任何修改。**"""
    containers, docker_available, docker_reason = await _scan_containers()
    data_dirs = await asyncio.to_thread(_scan_data_dirs)

    keys = set(containers) | set(data_dirs)
    live = await _live_builtin_keys(keys)

    candidates: List[NapcatOrphanCandidate] = []
    for instance_key in sorted(keys):
        verdict, reason = _judge(instance_key, live.get(instance_key))
        info = containers.get(instance_key)
        data_dir = data_dirs.get(instance_key)
        size = await asyncio.to_thread(_directory_size, data_dir) if data_dir else 0
        state = (info or {}).get("State") or {}
        host_config = (info or {}).get("HostConfig") or {}
        candidates.append(
            NapcatOrphanCandidate(
                instance_key=instance_key,
                verdict=verdict,
                reason=reason,
                container_name=str((info or {}).get("Name", "")).lstrip("/"),
                container_state=str(state.get("Status") or ""),
                container_restart_policy=str((host_config.get("RestartPolicy") or {}).get("Name") or ""),
                data_dir=str(data_dir) if data_dir else None,
                data_dir_bytes=size,
            ),
        )

    return NapcatOrphanPlan(
        scope=get_owner_tag(),
        docker_available=docker_available,
        docker_unavailable_reason=docker_reason,
        candidates=tuple(candidates),
    )


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _assert_data_dir_within_root(instance_key: str, data_dir: Path) -> None:
    """删目录前的最后一道：`resolve()` 之后必须仍在 `napcat_data/` 之下

    `validate_instance_key()` 已经挡掉了 `../`，这一条挡的是**符号链接**：
    `napcat_data/inst9 -> /` 这种指到外面的目录，路径拼接怎么看都是合法的。
    """
    root = napcat_data_root().resolve()
    resolved = data_dir.resolve()
    if resolved == root or root not in resolved.parents:
        raise NapcatOrphanRefused(
            instance_key,
            [f"数据目录 {resolved} 不在 {root} 之下（可能是符号链接），拒绝删除"],
        )


async def execute_napcat_orphan_cleanup(
    instance_key: str,
    *,
    remove_data_dir: bool,
) -> NapcatOrphanOutcome:
    """回收一个孤儿的容器与（可选）数据目录

    **判定在这里重跑一遍，预览时的结论不作数**——预览与确认之间完全可能有人把该实例
    重新建成内置 NapCat 形态。理由与孤儿 iptables 规则那一套相同。

    Raises:
        NapcatOrphanRefused: 该 key 不是孤儿，或数据目录逃出了 `napcat_data/`。
    """
    plan = await plan_napcat_orphan_cleanup()
    candidate = next((c for c in plan.candidates if c.instance_key == instance_key), None)
    if candidate is None:
        raise NapcatOrphanRefused(instance_key, ["宿主机上找不到这个标识的容器或数据目录"])
    if not candidate.reclaimable:
        raise NapcatOrphanRefused(instance_key, [candidate.reason])

    failures: List[str] = []
    container_removed = False
    data_dir_removed = False
    freed = 0

    if candidate.container_name:
        try:
            docker = aiodocker.Docker()
        except Exception as e:  # noqa: BLE001
            failures.append(f"无法连接 Docker 守护进程: {e}")
        else:
            try:
                container = await docker.containers.get(candidate.container_name)
                # 再确认一次归属：拿句柄与删除之间同样有窗口
                _assert_owned(await container.show(), instance_key, candidate.container_name)
                await container.delete(force=True)
                container_removed = True
                logger.warning(
                    f"已回收孤儿 NapCat 容器 {candidate.container_name}"
                    f"（判定 {candidate.verdict.value}: {candidate.reason}）",
                )
            except Exception as e:  # noqa: BLE001
                failures.append(f"删除容器 {candidate.container_name} 失败: {e}")
            finally:
                await docker.close()

    if remove_data_dir and candidate.data_dir:
        data_dir = Path(candidate.data_dir)
        _assert_data_dir_within_root(instance_key, data_dir)
        try:
            await asyncio.to_thread(shutil.rmtree, data_dir)
            data_dir_removed = True
            freed = candidate.data_dir_bytes
            logger.warning(f"已删除孤儿 NapCat 数据目录 {data_dir}（{candidate.data_dir_bytes} 字节）")
        except OSError as e:
            failures.append(f"删除数据目录 {data_dir} 失败: {e}")

    return NapcatOrphanOutcome(
        instance_key=instance_key,
        verdict=candidate.verdict,
        container_removed=container_removed,
        data_dir_removed=data_dir_removed,
        freed_bytes=freed,
        failures=tuple(failures),
    )
