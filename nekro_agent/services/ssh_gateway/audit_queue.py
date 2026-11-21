"""网关审计的有界写入队列

## 为什么不在转发路径上直接写库

每个连接、每个通道、每条 `exec` 都要留一行审计。直接 `await DBWsSshAudit.create(...)`
会把数据库延迟带进 SSH 转发路径：库慢的时候，CC 的命令也跟着慢；库挂的时候，转发
直接失败。所以调用方只把行放进内存队列就返回，后台任务每 0.5 秒或攒够一批写一次。

## 队列满了为什么不阻塞、也不丢

设计口径是"宁可慢，不丢记录"，但"慢"不能慢在调用方：授权是同步判定之后才决定放不放
行，阻塞在那里等于让一个库问题拖住整个网关。所以队列封顶 1000 行，`enqueue` 返回
`False`，由调用方据此拒绝新的连接 / 通道（理由 `limit_exceeded`）。已经在队列里的行
一行不丢。

## 写库失败为什么留在队头重试

逐行写、写成一行就从队头移除一行：失败时没写成的行留在原地，下一轮重试，已经写进
库的行不会因为重试被再插一遍。退避按指数增长、最长 30 秒，日志限流，避免库长时间
不可用时把日志刷满。这样数据库恢复后积压的审计会自己补上。

## 入队时就把行洗干净

审计里有容器送来的文本（命令原文、输入前若干字节、别名、子系统名）。PG 的文本列存不了
NUL，定长列超长也会报错；这样一行永远写不进去，会把队列一直卡在队头，之后所有新连接
都因为队列满被拒。所以入队时把 NUL 与孤立的代理字符转义、按列宽截断、数值收进列的
取值范围，保证队列里不存在「永远写不进去」的行。

## 关闭

`stop()` 尽量把剩下的写完，最多等 `timeout`；超时就取消后台任务、数清还剩几行并记
ERROR。取消时正在写的那一批还没从队列移除，所以计数是准的。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.services.ssh_gateway.dbslot import db_slot

logger = get_sub_logger("ssh_gateway.audit_queue")

MAX_ROWS = 1000
BATCH_SIZE = 100
FLUSH_INTERVAL_SECONDS = 0.5
DETAIL_LIMIT = 4000
MAX_BACKOFF_SECONDS = 30.0
FAILURE_LOG_INTERVAL_SECONDS = 300.0

# 单调时钟；测试注入它来验证退避，不必真等
_clock = time.monotonic

_queue: Deque[Dict[str, Any]] = deque()
_started = False
_stopping = False
_task: Optional["asyncio.Task[None]"] = None
_wakeup = asyncio.Event()
_idle = asyncio.Event()
_idle.set()
_write_lock = asyncio.Lock()
_writing = False
_failure_streak = 0
_retry_at = 0.0
_last_failure_log_at = 0.0


def _input_limit() -> int:
    try:
        return max(0, int(config.SSH_GATEWAY_AUDIT_INPUT_BYTES))
    except (TypeError, ValueError):
        return 0


def _truncate_bytes(value: str, limit: int) -> str:
    """按 UTF-8 字节截断；截断点落在半个字符上时把残字节转义，保证仍是合法文本"""
    if limit <= 0:
        return ""
    data = value.encode("utf-8", "backslashreplace")
    if len(data) <= limit:
        return data.decode("utf-8")
    return data[:limit].decode("utf-8", "backslashreplace")


# 定长列的宽度（与 `DBWsSshAudit` 一致）
_CHAR_LIMITS: Dict[str, int] = {
    "owner_kind": 16,
    "owner_account": 128,
    "adapter_key": 64,
    "instance_key": 64,
    "event": 24,
    "reason": 48,
    "task_ref": 256,
    "lease_id": 32,
    "conn_id": 32,
    "channel_kind": 24,
    "host_key_fp": 80,
    "actor": 128,
}
_INT32_MAX = 2**31 - 1
_INT32_FIELDS = ("workspace_id", "credential_id", "exit_status", "duration_ms")
_INT64_MAX = 2**63 - 1
_INT64_FIELDS = ("bytes_up", "bytes_down")


def _clean_text(value: Any) -> str:
    """NUL 转义成可见的 `\\x00`，孤立的代理字符按 `backslashreplace` 转义"""
    text = str(value if value is not None else "")
    if "\x00" in text:
        text = text.replace("\x00", "\\x00")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return text


def _clamp_int(value: Any, upper: int) -> Any:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(-upper - 1, min(upper, number))


def _sanitize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    for name, limit in _CHAR_LIMITS.items():
        if name in row:
            row[name] = _clean_text(row.get(name))[:limit]
    if "detail" in row:
        row["detail"] = _clean_text(row.get("detail"))[:DETAIL_LIMIT]
    if "input_head" in row:
        row["input_head"] = _truncate_bytes(_clean_text(row.get("input_head")), _input_limit())
    if "triggered_by" in row:
        row["triggered_by"] = [_clean_text(item)[:128] for item in list(row.get("triggered_by") or [])]
    for name in _INT32_FIELDS:
        if name in row:
            row[name] = _clamp_int(row[name], _INT32_MAX)
    for name in _INT64_FIELDS:
        if name in row:
            row[name] = _clamp_int(row[name], _INT64_MAX)
    if row.get("workspace_id") is None:
        row["workspace_id"] = 0
    row["allowed"] = bool(row.get("allowed"))
    return row


def enqueue(**fields: Any) -> bool:
    """把一行审计放进队列；队列满返回 `False`

    字段就是 `DBWsSshAudit` 的列。入队时洗一遍（见模块说明），不做字段名校验：调用方
    都是网关内部，写错字段名应当在测试里立刻炸出来，而不是被静默丢掉。
    """
    if len(_queue) >= MAX_ROWS:
        return False
    row: Dict[str, Any] = dict(fields)
    row.setdefault("workspace_id", 0)
    row.setdefault("event", "")
    row.setdefault("allowed", False)
    _queue.append(_sanitize_row(row))
    _idle.clear()
    # 攒够一批就立刻叫醒后台任务；不够就让 0.5 秒的定时器来收，避免一条一行地写
    if len(_queue) >= BATCH_SIZE:
        _wakeup.set()
    return True


def is_saturated() -> bool:
    """队列是否已到上限；调用方据此拒绝新的连接 / 通道"""
    return len(_queue) >= MAX_ROWS


def queued_rows() -> int:
    """当前还没写进库的行数（排查与测试用）"""
    return len(_queue)


async def _write_pending(*, until_empty: bool = False) -> bool:
    """写队列头部；成功返回 `True`，写库失败返回 `False`

    只有整批成功才从队头移除：失败时这一批留在原地，下一轮重试。`until_empty=True`
    时一直写到队列空（关停用），否则写到一个不满的批次就停，把机会让给事件循环。
    """
    global _writing, _failure_streak, _retry_at
    async with _write_lock:
        _writing = True
        try:
            while _queue:
                batch_size = min(BATCH_SIZE, len(_queue))
                written = 0
                try:
                    async with db_slot():
                        while written < batch_size and _queue:
                            await DBWsSshAudit.create(**_queue[0])
                            # 写成一行就移除一行：失败重试时不会把已经写进库的行再插一遍
                            _queue.popleft()
                            written += 1
                except Exception as exc:  # noqa: BLE001 - 写失败只影响队列，不影响网关
                    # 队头这一行自己写不进去（洗过之后仍违反约束、某个值超出列的范围……）时，
                    # 原样重试永远不会成功，整个队列就卡死、所有新连接被拒。区分两种失败：
                    # 一看就是这一行的问题（约束、取值）直接丢掉这一行；说不准的（PG 的数据
                    # 错误与断线在 Tortoise 里都是 OperationalError）连续失败几次后，试写下一行，
                    # 下一行写得进去就说明库是好的、坏的是队头那一行
                    if _queue and (_is_row_error(exc) or await _head_is_poison()):
                        _drop_head(exc)
                        continue
                    _record_failure(exc)
                    return False
                _failure_streak = 0
                _retry_at = 0.0
                if not until_empty and batch_size < BATCH_SIZE:
                    break
            return True
        finally:
            _writing = False
            if not _queue:
                _idle.set()


# 队头同一行连续失败这么多次之后，试写下一行来判断是不是这一行自己的问题
POISON_PROBE_AFTER_FAILURES = 3
_head_ref: Optional[Dict[str, Any]] = None
_head_failures = 0


def _is_row_error(exc: BaseException) -> bool:
    """这一行自己的问题（换个时间重试也不会好）：约束冲突、取值或类型不对"""
    from tortoise.exceptions import FieldError, IntegrityError, ValidationError

    return isinstance(exc, (IntegrityError, ValidationError, FieldError, ValueError, TypeError, OverflowError))


async def _head_is_poison() -> bool:
    """队头连续失败够多次时，试写下一行；下一行写进去了，说明坏的是队头这一行"""
    global _head_ref, _head_failures
    head = _queue[0]
    if head is _head_ref:
        _head_failures += 1
    else:
        _head_ref = head
        _head_failures = 1
    if _head_failures < POISON_PROBE_AFTER_FAILURES or len(_queue) < 2:
        return False
    try:
        async with db_slot():
            await DBWsSshAudit.create(**_queue[1])
    except Exception:  # noqa: BLE001 - 下一行也写不进去：多半是库本身的问题，照常退避重试
        return False
    del _queue[1]
    return True


def _drop_head(exc: BaseException) -> None:
    """丢掉写不进库的队头一行；日志只记异常类型与这一行的事件名，不记任何值"""
    global _head_ref, _head_failures
    row = _queue.popleft()
    _head_ref = None
    _head_failures = 0
    logger.warning(
        f"SSH 审计有一行写不进库，已丢弃（事件 {str(row.get('event', ''))[:24]}）: {type(exc).__name__}",
    )


def _record_failure(exc: BaseException) -> None:
    """指数退避 + 日志限流；异常本身可能带 SQL，不把行内容写进日志"""
    global _failure_streak, _retry_at, _last_failure_log_at
    _failure_streak += 1
    delay = min(float(2 ** max(0, _failure_streak - 1)), MAX_BACKOFF_SECONDS)
    _retry_at = _clock() + delay
    now = _clock()
    if _failure_streak == 1 or now - _last_failure_log_at >= FAILURE_LOG_INTERVAL_SECONDS:
        _last_failure_log_at = now
        logger.warning(
            f"SSH 审计写库失败，{delay:.0f} 秒后重试（队列还有 {len(_queue)} 行）: "
            f"{type(exc).__name__}: {exc}",
        )


async def start() -> None:
    """起后台批量写入任务；重复调用无副作用"""
    global _started, _task, _stopping
    if _started:
        return
    _started = True
    _stopping = False
    _task = asyncio.create_task(_run())


async def _run() -> None:
    while not _stopping:
        try:
            await asyncio.wait_for(_wakeup.wait(), timeout=FLUSH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        _wakeup.clear()
        if _stopping:
            break
        if _clock() < _retry_at:
            continue
        await _write_pending()
    # 关停时尽量把剩下的写完；写失败就留给 `stop` 计数并记 ERROR
    if _queue:
        await _write_pending(until_empty=True)


async def flush() -> None:
    """等队列写空（测试用）

    后台任务没起时直接在当前协程里写：测试常常只入队、不 start。
    """
    if not _started:
        await _write_pending(until_empty=True)
        return
    if not _queue:
        return
    _idle.clear()
    _wakeup.set()
    try:
        await asyncio.wait_for(_idle.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning(f"等待 SSH 审计队列写空超时，仍有 {len(_queue)} 行")


async def stop(timeout: float = 5.0) -> None:
    """停止后台任务，尽量写完；超时放弃并记 ERROR（说丢了几行）"""
    global _started, _stopping, _task
    if not _started:
        # 从未启动：也把已经入队的写掉，保持 stop 的语义一致
        try:
            await asyncio.wait_for(_write_pending(until_empty=True), timeout)
        except asyncio.TimeoutError:
            _discard_remaining("关停超时")
        if _queue:
            # 写库一直失败而不是超时：同样要报出丢了几行
            _discard_remaining("未能写进库")
        _idle.set()
        return

    _stopping = True
    _wakeup.set()
    task = _task
    _task = None
    dropped: Optional[int] = None
    try:
        if task is not None:
            await asyncio.wait_for(task, timeout)
    except asyncio.TimeoutError:
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 取消后的收尾不关心
                pass
        dropped = _discard_remaining("关停超时")
    except Exception as exc:  # noqa: BLE001 - 后台任务异常不该让关机失败
        logger.error(f"SSH 审计后台任务异常退出: {type(exc).__name__}: {exc}")
    finally:
        _started = False
        _stopping = False
        if dropped is None:
            _discard_remaining("后台任务已停止但仍有未写行")
        _idle.set()


def _discard_remaining(why: str) -> int:
    """清空队列并报出丢了几行；返回丢弃行数"""
    count = len(_queue)
    if count:
        _queue.clear()
        logger.error(f"SSH 审计队列{why}，丢弃 {count} 行")
    return count


def reset_audit_queue() -> None:
    """清空队列与运行状态（仅供测试）

    事件与锁重新创建，避免绑定到上一个用例的事件循环。
    """
    global _started, _stopping, _task, _wakeup, _idle, _write_lock
    global _writing, _failure_streak, _retry_at, _last_failure_log_at, _head_ref, _head_failures
    _queue.clear()
    _head_ref = None
    _head_failures = 0
    _started = False
    _stopping = False
    _task = None
    _wakeup = asyncio.Event()
    _idle = asyncio.Event()
    _idle.set()
    _write_lock = asyncio.Lock()
    _writing = False
    _failure_streak = 0
    _retry_at = 0.0
    _last_failure_log_at = 0.0


__all__ = [
    "BATCH_SIZE",
    "DETAIL_LIMIT",
    "MAX_ROWS",
    "enqueue",
    "flush",
    "is_saturated",
    "queued_rows",
    "reset_audit_queue",
    "start",
    "stop",
]
