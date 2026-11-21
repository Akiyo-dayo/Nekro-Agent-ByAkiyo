"""SSH 网关的授权租约

## 为什么跟着任务走，而不跟着回合走

CC 任务是异步的：`delegate_to_cc` 只是把任务提交进容器，真正执行往往在那一轮结束很久
之后。D6 的授权窗口是「回合开始到回合收尾」，按它来的话，任务还没在容器里跑起来，授权
就已经过期了。

所以委托的那一刻按本轮触发快照拍一张租约，把「谁叫醒的这一轮、覆盖哪些行」定格下来；
任务在容器里真正收到第一条正文时转 `running`，任务收尾时转 `ended`。租约的有效期从拍下
那一刻起算（`SSH_GATEWAY_LEASE_MAX_SECONDS`），比一次任务正常需要的时间宽裕得多，又比
「永远有效」短得多。

## 为什么只认 NA 看到「正在跑」的那一份

容器是几个频道共用的，而沙盒同一时刻只跑一个任务。授权只能跟着「此刻在跑的是谁的任务」：
排队中的、已经结束的、根本没拍租约的（别的频道委托的、后台直发的）任务在跑时，这个工作区
上的服务器一律连不上。这是有意为之，不是遗漏。

`running_lease` 因此只在一个工作区**恰好**有一份 `running` 且未过期的租约时返回它；多过
一份说明状态机出了错（不该发生），记 ERROR 并返回 `None`——网关据此一律拒，而不是猜一份。

## NA 重启

租约在进程内存里，重启即清空；重启后才恢复的任务结果没有租约，连不上服务器。这可以接受：
重启本来就打断了正在进行的任务。网关那一轮要断开的连接随进程一起没了，也不需要持久化。

## 任务拿的是哪一份租约

拍租约的人把 `lease_id` 显式交给任务（`get_lease`），任务不按 `task_ref` 去「找最新的
一份」：同一个频道上一次委托留下的租约（已结束的，或者因为启动前被取消而还挂着的），
绝不能被下一次没拍到租约的任务认领。为此拍新租约时，同一 `task_ref` 还没结束的旧租约
一律先结束——即使这一次一行都没覆盖、不建新租约。

## 收尾

监督任务每一拍调 `sweep_leases()`：过期的租约在这里结束（不再等到有人来查才结算，
长时间不开新通道的 shell 也会在到期时被断开），结束超过 10 分钟的租约从表里删掉。

## 审计

每次建、生效、结束各写一行 `lease` 审计。租约的状态变化发生在同步路径上（委托、流的第一
条数据、收尾），而审计写库是异步的，所以这里用一个后台任务写；写失败只记日志，不影响任务。
"""

from __future__ import annotations

import asyncio
import inspect
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway.authz import Coverage

logger = get_sub_logger("ssh_gateway.lease")

LEASE_STATE_QUEUED = "queued"
LEASE_STATE_RUNNING = "running"
LEASE_STATE_ENDED = "ended"

LEASE_ORIGIN_CHAT = "chat"
LEASE_ORIGIN_CONSOLE = "console"

# 结束超过这么久的租约从表里删掉；留一会儿只是为了排查时还查得到
ENDED_RETENTION_SECONDS = 600.0

# 审计与过期判定共用同一口单调时钟；测试注入它来造「已经过期」的租约
_clock: Callable[[], float] = time.monotonic


@dataclass
class SshLease:
    """一份跟着 CC 任务走的授权

    Attributes:
        lease_id: 短随机串，审计里把同一份租约的几行串起来
        workspace_id: 这份授权属于哪个工作区
        task_ref: `cc_delegate:<chat_key>` 或后台直发的 `__user__`
        origin: `chat`（聊天委托）或 `console`（后台直发）
        chat_key: 聊天委托的会话标识；后台直发为空
        adapter_key / instance_key: 拍下时频道的实例对，复核平台方账号时按它匹配
        actor: 后台直发的用户名；聊天来源为空
        actor_user_id: 后台直发的账号 id；复核按 id 重查（用户名不唯一）
        triggered_by: 叫醒这一轮的账号，去重保序
        credential_ids: 拍下那一刻被覆盖的行，任务中途新登记的行不会加进来
        duty_ids: 其中按值守判据覆盖的行（复核时不比所有者名单，改判仍是值守）；
            后台直发恒为空
        state: `queued` / `running` / `ended`
        created_at: 拍下时刻（单调时钟）
        started_at: 转 `running` 的时刻；没生效过为空
        expires_at: 最长有效期，`created_at + SSH_GATEWAY_LEASE_MAX_SECONDS`
        end_reason: 结束原因，审计与排查用
        created_wall: 拍下时刻的墙上时间（事后摘要里给人看）
        task_text / final_result: 只在带值守覆盖的租约上存，事后摘要用；
            都来自模型与容器，按不可信内容处理
        connected_ids: 网关按这份租约放行过连接的行（`authority.authorize_connection` 放行时记）。
            事后摘要以它判断「值守行真的连过」，不依赖审计行有没有落库
    """

    lease_id: str
    workspace_id: int
    task_ref: str
    origin: str
    credential_ids: FrozenSet[int]
    chat_key: str = ""
    adapter_key: str = ""
    instance_key: str = ""
    actor: str = ""
    actor_user_id: int = 0
    triggered_by: Tuple[str, ...] = ()
    duty_ids: FrozenSet[int] = frozenset()
    state: str = LEASE_STATE_QUEUED
    created_at: float = field(default_factory=lambda: _clock())
    started_at: Optional[float] = None
    expires_at: float = 0.0
    end_reason: str = ""
    ended_at: Optional[float] = None
    created_wall: float = field(default_factory=time.time)
    task_text: str = ""
    final_result: str = ""
    connected_ids: Set[int] = field(default_factory=set)


# 存在租约上的任务正文与 CC 结果最多留这么长；摘要只取开头几百字，多留一点给截断提示
NOTE_MAX_CHARS = 2000

_end_callbacks: List[Callable[[SshLease], object]] = []
_leases: Dict[str, SshLease] = {}
# 后台审计任务持有强引用，避免被回收；结束即从集合里去掉
_audit_tasks: Set["asyncio.Task[None]"] = set()
_callback_tasks: Set["asyncio.Task[None]"] = set()


def lease_expired(lease: SshLease, now: Optional[float] = None) -> bool:
    """租约是否已过最长有效期"""
    current = _clock() if now is None else now
    return current >= float(lease.expires_at)


def on_lease_end(callback: Callable[[SshLease], object]) -> None:
    """登记租约结束时的回调；网关用它断开连接，值守用它发事后摘要

    每个回调单独包一层：一个回调抛错不能挡住后面的回调，也不能让 `end_lease` 失败。
    回调可以返回协程，返回的协程会被排进事件循环。
    """
    _end_callbacks.append(callback)


def create_lease(
    *,
    workspace_id: int,
    task_ref: str,
    origin: str,
    coverage: Coverage,
    chat_key: str = "",
    adapter_key: str = "",
    instance_key: str = "",
    actor: str = "",
    actor_user_id: int = 0,
) -> Optional[SshLease]:
    """拍一张租约；一行都没覆盖时不建，返回 `None`

    同一个 `task_ref` 已经有未结束的租约时，先把旧的那份结束掉：同一频道的下一个任务
    已经开始了，旧任务的授权不能顺手续给新任务。这一步在「一行都没覆盖」的判断之前
    做：这一次没拍到租约，也不能让上一份留着。
    """
    for old in list(_leases.values()):
        if (
            int(old.workspace_id) == int(workspace_id)
            and old.task_ref == task_ref
            and old.state != LEASE_STATE_ENDED
        ):
            end_lease(old.lease_id, "superseded")

    if not coverage.credential_ids:
        return None

    now = _clock()
    credential_ids = frozenset(int(item) for item in coverage.credential_ids)
    # 值守只给聊天来源；子集之外的 id 一概不认（覆盖结果被拼错时宁可按严格复核）
    duty_ids = (
        frozenset(int(item) for item in getattr(coverage, "duty_ids", frozenset())) & credential_ids
        if str(origin) == "chat"
        else frozenset()
    )
    lease = SshLease(
        lease_id=secrets.token_hex(8),
        workspace_id=int(workspace_id),
        task_ref=str(task_ref),
        origin=str(origin),
        credential_ids=credential_ids,
        duty_ids=duty_ids,
        chat_key=str(chat_key),
        adapter_key=str(adapter_key),
        instance_key=str(instance_key),
        actor=str(actor),
        actor_user_id=int(actor_user_id or 0),
        triggered_by=tuple(str(item) for item in coverage.triggered_by),
        created_at=now,
        expires_at=now + max(0, int(config.SSH_GATEWAY_LEASE_MAX_SECONDS)),
    )
    _leases[lease.lease_id] = lease
    # 按值守授权的行在拍下那一行审计里点名，平台方事后能一眼看出「这次不是所有者本人叫醒的」
    _write_audit(lease, f"create duty={','.join(str(item) for item in sorted(duty_ids))}" if duty_ids else "create")
    return lease


def mark_running(lease_id: str) -> None:
    """任务在容器里真正跑起来：租约转 `running`

    只有从 `queued` 转过来才算「生效」；已经结束的租约不能再被晚到的流事件复活。
    """
    lease = _leases.get(str(lease_id))
    if lease is None or lease.state != LEASE_STATE_QUEUED:
        return
    # 排队超过最长有效期才轮到的任务不再授权：有效期从拍下那一刻算，排队本身也占用窗口。
    if lease_expired(lease):
        end_lease(lease.lease_id, "expired")
        return
    lease.state = LEASE_STATE_RUNNING
    lease.started_at = _clock()
    _write_audit(lease, "start")


def end_lease(lease_id: str, reason: str) -> None:
    """结束一份租约，并逐个通知订阅者

    幂等：已经结束的租约再结束一次不会重复写审计、也不会重复回调。
    """
    lease = _leases.get(str(lease_id))
    if lease is None or lease.state == LEASE_STATE_ENDED:
        return
    lease.state = LEASE_STATE_ENDED
    lease.end_reason = str(reason or "ended")
    lease.ended_at = _clock()
    _write_audit(lease, f"end:{lease.end_reason}")

    for callback in list(_end_callbacks):
        _invoke_callback(callback, lease)


def note_task_text(lease_id: str, text: str) -> None:
    """记下委托给 CC 的任务正文；只记带值守覆盖、还没结束的租约"""
    lease = _leases.get(str(lease_id or ""))
    if lease is None or not lease.duty_ids or lease.state == LEASE_STATE_ENDED:
        return
    lease.task_text = str(text or "")[:NOTE_MAX_CHARS]


def note_final_result(lease_id: str, text: str) -> None:
    """记下 CC 最后一条结果；要在 `end_lease` 之前调，摘要在结束回调里读它"""
    lease = _leases.get(str(lease_id or ""))
    if lease is None or not lease.duty_ids or lease.state == LEASE_STATE_ENDED:
        return
    lease.final_result = str(text or "")[:NOTE_MAX_CHARS]


def note_connected(lease_id: str, credential_id: int) -> None:
    """网关按这份租约放行了一条到 `credential_id` 的连接；行数不超过租约覆盖的行"""
    lease = _leases.get(str(lease_id or ""))
    if lease is None or lease.state == LEASE_STATE_ENDED:
        return
    if int(credential_id) in lease.credential_ids:
        lease.connected_ids.add(int(credential_id))


def get_lease(lease_id: str) -> Optional[SshLease]:
    """按 id 取租约（拍租约的人把 id 交给任务，任务按 id 取）"""
    if not lease_id:
        return None
    return _leases.get(str(lease_id))


def sweep_leases() -> None:
    """监督任务每一拍调一次：结束过期的租约，删掉结束已久的租约"""
    now = _clock()
    for lease in list(_leases.values()):
        if lease.state != LEASE_STATE_ENDED and lease_expired(lease, now):
            end_lease(lease.lease_id, "expired")
    for lease_id, lease in list(_leases.items()):
        if lease.state == LEASE_STATE_ENDED and lease.ended_at is not None:
            if now - float(lease.ended_at) >= ENDED_RETENTION_SECONDS:
                _leases.pop(lease_id, None)


def find_lease(workspace_id: int, task_ref: str, state: Optional[str] = None) -> Optional[SshLease]:
    """按工作区与任务标识找租约；同一 `task_ref` 有多份时取最新的一份"""
    for lease in reversed(list(_leases.values())):
        if int(lease.workspace_id) != int(workspace_id):
            continue
        if lease.task_ref != task_ref:
            continue
        if state is not None and lease.state != state:
            continue
        return lease
    return None


def running_lease(workspace_id: int) -> Optional[SshLease]:
    """这个工作区此刻唯一一份生效中的租约

    过期的顺手结束掉。多过一份是状态机错误：记 ERROR 并返回 `None`，让网关一律拒。
    """
    running: List[SshLease] = []
    for lease in list(_leases.values()):
        if int(lease.workspace_id) != int(workspace_id):
            continue
        if lease.state == LEASE_STATE_ENDED:
            continue
        if lease_expired(lease):
            # 排队中的也一并收掉：它虽然不授权，但留在表里会被 `find_lease` 找到，
            # 晚到的任务一旦开始就会误以为自己还有效。
            end_lease(lease.lease_id, "expired")
            continue
        if lease.state != LEASE_STATE_RUNNING:
            continue
        running.append(lease)

    if len(running) > 1:
        logger.error(
            f"[ssh_gateway] 工作区 {workspace_id} 同时有 {len(running)} 份生效中的 SSH 租约，"
            "状态机异常；网关按无授权处理",
        )
        return None
    return running[0] if running else None


def running_lease_count() -> int:
    """当前生效中且未过期的租约数（后台状态展示用，不触发过期收尾）"""
    return sum(
        1
        for lease in list(_leases.values())
        if lease.state == LEASE_STATE_RUNNING and not lease_expired(lease)
    )


def reset_leases() -> None:
    """清空租约与订阅者（进程内状态，供测试与关停使用）"""
    _leases.clear()
    _end_callbacks.clear()


def _invoke_callback(callback: Callable[[SshLease], object], lease: SshLease) -> None:
    """调用一个结束回调；同步异常当场吞掉，返回的协程排进事件循环"""
    try:
        result = callback(lease)
    except Exception as exc:  # noqa: BLE001 - 一个订阅者出错不能拖垮租约结束
        logger.warning(f"[ssh_gateway] 租约结束回调失败（已忽略）: lease={lease.lease_id} | {exc}")
        return
    if inspect.isawaitable(result):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"[ssh_gateway] 租约结束回调返回协程，但当前没有事件循环: lease={lease.lease_id}")
            return
        task = loop.create_task(_await_callback(result))
        _callback_tasks.add(task)
        task.add_done_callback(_callback_tasks.discard)


async def _await_callback(result: object) -> None:
    try:
        await result  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ssh_gateway] 租约结束回调失败（已忽略）: {exc}")


def _write_audit(lease: SshLease, detail: str) -> None:
    """写一行租约审计；没有事件循环时跳过，写库失败只记日志"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug(f"[ssh_gateway] 没有事件循环，跳过租约审计: lease={lease.lease_id} detail={detail}")
        return
    task = loop.create_task(_create_audit_row(lease, detail))
    _audit_tasks.add(task)
    task.add_done_callback(_audit_tasks.discard)


async def _create_audit_row(lease: SshLease, detail: str) -> None:
    try:
        await DBWsSshAudit.create(
            workspace_id=int(lease.workspace_id),
            credential_id=None,
            owner_kind="",
            owner_account="",
            adapter_key="",
            instance_key="",
            event=C.EVENT_LEASE,
            allowed=True,
            reason="",
            task_ref=lease.task_ref,
            lease_id=lease.lease_id,
            conn_id="",
            triggered_by=list(lease.triggered_by),
            channel_kind="",
            detail=detail,
            input_head="",
            exit_status=None,
            bytes_up=None,
            bytes_down=None,
            duration_ms=None,
            host_key_fp="",
            actor=lease.actor,
        )
    except Exception as exc:  # noqa: BLE001 - 审计写失败不影响任务
        logger.warning(f"[ssh_gateway] 写租约审计失败（已忽略）: lease={lease.lease_id} detail={detail} | {exc}")


def leases_for_workspace(workspace_id: int) -> Tuple[SshLease, ...]:
    """这个工作区当前的租约（只给排查与后台状态用）"""
    return tuple(lease for lease in _leases.values() if int(lease.workspace_id) == int(workspace_id))


__all__ = [
    "LEASE_ORIGIN_CHAT",
    "LEASE_ORIGIN_CONSOLE",
    "LEASE_STATE_ENDED",
    "LEASE_STATE_QUEUED",
    "LEASE_STATE_RUNNING",
    "SshLease",
    "create_lease",
    "end_lease",
    "find_lease",
    "get_lease",
    "lease_expired",
    "leases_for_workspace",
    "mark_running",
    "note_connected",
    "note_final_result",
    "note_task_text",
    "on_lease_end",
    "reset_leases",
    "running_lease",
    "running_lease_count",
    "sweep_leases",
]
