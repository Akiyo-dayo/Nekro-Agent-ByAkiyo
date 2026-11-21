"""运维调用的审计记录

## 事实在表里，日志只是排查线索

每次调用都在**执行之前**写一行 `pending`，执行完再补结果：进程中途崩掉也留得下
「尝试过」的痕迹。结构化日志仍然保留（运维排查时还是先看日志），但后台审计页、
`ops_audit_recent` 与 `/na_ops audit` 读的都是这张表。

## 记什么、不记什么

- 操作者取**授权上**的运维人员集合（宿主算出来的），不取模型传的任何东西；
- 参数先过 `tools/redaction.py: redact_text` 再逐值截断：参数里可能有聊天键、
  频道名，也可能被模型塞进凭据与路径；
- 异常只记**类名**：原文里可能有上游返回、宿主路径甚至凭据，而这张表的读者比写者多；
- 正文与工具返回内容一律不记：审计是「谁动了什么」的留痕，不是外部内容的第二份副本。

## 清理

写入成功后顺手删掉 `created_at` 早于「现在 − 365 天」的行，进程内每 24 小时最多
一次。不加后台循环：功能从没开过的部署表是空的，不需要清。
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from tortoise.timezone import now as db_now

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_ops_agent_audit import DBOpsAgentAudit
from nekro_agent.tools.redaction import redact_text

from .round_grant import OpsRoundGrant
from .text import ident

logger = get_sub_logger("ops_agent.audit")

# 参数最多记 10 个键，多出的丢掉并记一个 "…": "+N"
ARGS_MAX_KEYS = 10
# 每个值 redact_text 后截到 200 字
ARGS_MAX_VALUE_CHARS = 200
# 键也限长：键由模型自由填写，不截断就能用一次调用撑爆一行
ARGS_MAX_KEY_CHARS = 64
# 审计保留天数与进程内清理间隔
KEEP_DAYS = 365
PURGE_INTERVAL_SECONDS = 86400.0

# 日志里的参数回显保留原来的上限：它是给运维排查看的一行，不是事实来源
MAX_ARGS_CHARS = 500
# 「最近的运维记录」一次最多列多少行：`ops_audit_recent` 与 `/na_ops audit` 同一个口径
MAX_RECENT_LINES = 20
DEFAULT_RECENT_LINES = 10


@dataclass
class OpsEventSlot:
    """一轮 RPC 调用里可写的审计槽

    执行层（写工具）把结果填进来，`rpc_exec` 随后用它关闭审计行。没有记录号时
    槽里是 `None`，写工具据此不带记录号、也不写槽。
    """

    event_id: Optional[int] = None
    outcome: str = ""
    reason: str = ""
    summary: str = ""


current_ops_event: ContextVar[Optional[OpsEventSlot]] = ContextVar("current_ops_event", default=None)

# 清理用的单调时钟与写库用的墙钟；测试可注入
_clock: Callable[[], float] = time.monotonic
_wall_clock: Callable[[], datetime] = db_now
_last_purge_at: Optional[float] = None


def reset_audit_state() -> None:
    """清掉「上次清理时刻」（进程内状态，供测试与关停使用）"""
    global _last_purge_at
    _last_purge_at = None


def _clip(value: str, limit: int) -> str:
    return value[:limit]


def _safe_text(value: object, limit: int) -> str:
    """对外文本 -> 脱敏并截断到列宽"""
    return _clip(redact_text(str(value or "")), limit)


def _sanitize_args(args: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """参数 -> 可入库的字典：逐值脱敏截断、键截断、最多 10 个键"""
    if not args:
        return {}
    items = list(args.items())
    safe: dict[str, str] = {}
    for key, value in items[:ARGS_MAX_KEYS]:
        safe[_clip(str(key), ARGS_MAX_KEY_CHARS)] = _clip(redact_text(str(value)), ARGS_MAX_VALUE_CHARS)
    if len(items) > ARGS_MAX_KEYS:
        safe["…"] = f"+{len(items) - ARGS_MAX_KEYS}"
    return safe


def _render_args(args: Optional[Mapping[str, Any]]) -> str:
    """日志里的参数 -> 一行脱敏文本"""
    if not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        text = redact_text(str(value))
        parts.append(f"{key}={text}")
    joined = ", ".join(parts)
    if len(joined) > MAX_ARGS_CHARS:
        joined = joined[:MAX_ARGS_CHARS] + "…"
    return joined


def _log_event(
    *,
    outcome: str,
    tool: str,
    source: str,
    round_id: str,
    chat_key: str,
    adapter_key: str,
    instance_key: str,
    operators: str,
    reason: str,
    args: Optional[Mapping[str, Any]],
    summary: str,
) -> None:
    logger.info(
        "[ops_audit] "
        f"outcome={outcome} tool={tool} source={source or '-'} round={round_id or '-'} "
        f"chat_key={chat_key or '-'} "
        f"instance={f'{adapter_key}/{instance_key}' if adapter_key or instance_key else '-'} "
        f"operators={operators or '-'} reason={redact_text(reason) if reason else '-'} "
        f"args=[{_render_args(args)}] summary={redact_text(summary) if summary else '-'}",
    )


def _identity(
    grant: Optional[OpsRoundGrant],
    operator: str,
    adapter_key: str,
    instance_key: str,
    chat_key: str,
) -> Tuple[str, str, str, str, str]:
    """地点与操作人：有授权就取授权上的，没有就取参数"""
    if grant is not None:
        return (
            _clip(",".join(sorted(grant.operators)), 256),
            _clip(grant.round_id, 64),
            _clip(grant.adapter_key, 64),
            _clip(grant.instance_key, 128),
            _clip(grant.chat_key, 256),
        )
    return (
        _clip(str(operator or ""), 256),
        "",
        _clip(str(adapter_key or ""), 64),
        _clip(str(instance_key or ""), 128),
        _clip(str(chat_key or ""), 256),
    )


async def _purge_if_due() -> None:
    """距上次清理超过一天就删掉过期行；进程里第一次写入也算超过"""
    global _last_purge_at
    moment = _clock()
    if _last_purge_at is not None and moment - _last_purge_at < PURGE_INTERVAL_SECONDS:
        return
    # 先记时刻再删：删除失败时不必让下一次写操作再撞一遍同一个失败
    _last_purge_at = moment
    cutoff = _wall_clock() - timedelta(days=KEEP_DAYS)
    try:
        await DBOpsAgentAudit.filter(created_at__lt=cutoff).delete()
    except Exception as exc:  # noqa: BLE001 - 清理失败不能反过来影响业务调用
        logger.warning(f"[ops_audit] 清理过期记录失败: {type(exc).__name__}")


async def open_ops_event(
    *,
    grant: Optional[OpsRoundGrant],
    tool: str,
    args: Optional[Mapping[str, Any]] = None,
    source: str,
    operator: str = "",
    adapter_key: str = "",
    instance_key: str = "",
    chat_key: str = "",
) -> Optional[int]:
    """执行前先写一行 `pending`，返回记录号

    写库失败只记 warning 并返回 `None`：调用方据此决定要不要继续（写操作不继续，
    读操作照常）。异常原文不进日志，只带类名。
    """
    operators, round_id, source_adapter, source_instance, source_chat = _identity(
        grant,
        operator,
        adapter_key,
        instance_key,
        chat_key,
    )
    _log_event(
        outcome="pending",
        tool=tool,
        source=source,
        round_id=round_id,
        chat_key=source_chat,
        adapter_key=source_adapter,
        instance_key=source_instance,
        operators=operators,
        reason="",
        args=args,
        summary="",
    )
    try:
        row = await DBOpsAgentAudit.create(
            source=_clip(str(source or "agent"), 16),
            operator=operators,
            adapter_key=source_adapter,
            instance_key=source_instance,
            chat_key=source_chat,
            round_id=round_id,
            tool=_clip(str(tool or ""), 64),
            args_json=_sanitize_args(args),
            outcome="pending",
        )
    except Exception as exc:  # noqa: BLE001 - 审计写不进去不能把调用方一起打掉
        logger.warning(f"[ops_audit] 写审计失败: tool={tool} source={source} | {type(exc).__name__}")
        return None
    event_id = int(row.id)
    await _purge_if_due()
    return event_id


async def close_ops_event(
    event_id: Optional[int],
    *,
    outcome: str,
    reason: str = "",
    summary: str = "",
    error_type: str = "",
) -> None:
    """按记录号关闭一行；失败只记 warning"""
    if event_id is None:
        return
    safe_reason = _safe_text(reason, 64)
    safe_summary = _safe_text(summary, 512)
    safe_error = _clip(str(error_type or ""), 128)
    try:
        await DBOpsAgentAudit.filter(id=event_id).update(
            outcome=_clip(str(outcome or ""), 16),
            refuse_reason=safe_reason,
            result_summary=safe_summary,
            error_type=safe_error,
            finished_at=_wall_clock(),
        )
    except Exception as exc:  # noqa: BLE001 - 关闭失败不能反过来打掉已经执行完的业务
        logger.warning(f"[ops_audit] 关闭审计失败: id={event_id} | {type(exc).__name__}")
        return

    # 日志保持原来的字段：能查到这一次是哪个工具、哪个地点、谁触发的
    try:
        row = await DBOpsAgentAudit.get_or_none(id=event_id)
    except Exception:  # noqa: BLE001 - 读回日志字段失败不影响关闭本身
        row = None
    if row is None:
        logger.info(f"[ops_audit] outcome={outcome} id={event_id} reason={safe_reason or '-'}")
        return
    _log_event(
        outcome=row.outcome,
        tool=row.tool,
        source=row.source,
        round_id=row.round_id,
        chat_key=row.chat_key,
        adapter_key=row.adapter_key,
        instance_key=row.instance_key,
        operators=row.operator,
        reason=row.refuse_reason,
        args=row.args_json,
        summary=row.result_summary,
    )


async def record_ops_event(
    *,
    grant: Optional[OpsRoundGrant],
    tool: str,
    args: Optional[Mapping[str, Any]] = None,
    outcome: str,
    reason: str = "",
    summary: str = "",
    operator: str = "",
    adapter_key: str = "",
    instance_key: str = "",
    chat_key: str = "",
    source: str = "agent",
    error_type: str = "",
) -> Optional[int]:
    """一次写完整一行（用于拒绝等不需要执行的事件），返回记录号

    写库失败只记 warning 并返回 `None`，不外抛。
    """
    operators, round_id, source_adapter, source_instance, source_chat = _identity(
        grant,
        operator,
        adapter_key,
        instance_key,
        chat_key,
    )
    safe_reason = _safe_text(reason, 64)
    safe_summary = _safe_text(summary, 512)
    _log_event(
        outcome=outcome,
        tool=tool,
        source=source,
        round_id=round_id,
        chat_key=source_chat,
        adapter_key=source_adapter,
        instance_key=source_instance,
        operators=operators,
        reason=safe_reason,
        args=args,
        summary=safe_summary,
    )
    try:
        row = await DBOpsAgentAudit.create(
            source=_clip(str(source or "agent"), 16),
            operator=operators,
            adapter_key=source_adapter,
            instance_key=source_instance,
            chat_key=source_chat,
            round_id=round_id,
            tool=_clip(str(tool or ""), 64),
            args_json=_sanitize_args(args),
            outcome=_clip(str(outcome or ""), 16),
            refuse_reason=safe_reason,
            result_summary=safe_summary,
            error_type=_clip(str(error_type or ""), 128),
            # 拒绝这类事件写入即完成；pending 才需要等关闭
            finished_at=_wall_clock() if outcome != "pending" else None,
        )
    except Exception as exc:  # noqa: BLE001 - 审计写不进去不能把调用方一起打掉
        logger.warning(f"[ops_audit] 写审计失败: tool={tool} source={source} | {type(exc).__name__}")
        return None
    event_id = int(row.id)
    await _purge_if_due()
    return event_id


async def list_ops_events(
    *,
    limit: int,
    offset: int = 0,
    outcome: str = "",
    tool: str = "",
    source: str = "",
    operator: str = "",
) -> tuple[int, list[DBOpsAgentAudit]]:
    """按记录号倒序分页读取；返回 (总数, 本页)

    `operator` 是包含匹配：一行的操作人可能是逗号连接的多人。
    """
    query = DBOpsAgentAudit.all()
    if outcome:
        query = query.filter(outcome=outcome)
    if tool:
        query = query.filter(tool=tool)
    if source:
        query = query.filter(source=source)
    if operator:
        query = query.filter(operator__icontains=operator)

    total = await query.count()
    rows = await query.order_by("-id").offset(max(0, int(offset))).limit(max(0, int(limit)))
    return total, list(rows)


def format_ops_event_line(row: DBOpsAgentAudit) -> str:
    """一行审计记录

    操作人与工具名过 `ident`（外部可写的文本不能破坏行边界）；`result_summary`
    原样拼接——它是宿主写的回执句，名字已经带引号定界。
    """
    moment = row.created_at.strftime("%m-%d %H:%M") if row.created_at else "--"
    reason = f"（{row.refuse_reason}）" if row.refuse_reason else ""
    return (
        f"#{row.id} {moment} {row.source} {ident(row.tool)} {row.outcome}{reason} "
        f"操作人 {ident(row.operator)} {row.result_summary}"
    )


def format_ops_event_lines(rows: Sequence[DBOpsAgentAudit]) -> str:
    """多行审计文本；空表给一句固定文案"""
    if not rows:
        return "最近没有运维操作记录。"
    return "\n".join(format_ops_event_line(row) for row in rows)


__all__ = [
    "ARGS_MAX_KEYS",
    "ARGS_MAX_VALUE_CHARS",
    "DEFAULT_RECENT_LINES",
    "KEEP_DAYS",
    "MAX_ARGS_CHARS",
    "MAX_RECENT_LINES",
    "PURGE_INTERVAL_SECONDS",
    "OpsEventSlot",
    "close_ops_event",
    "current_ops_event",
    "format_ops_event_line",
    "format_ops_event_lines",
    "list_ops_events",
    "open_ops_event",
    "record_ops_event",
    "reset_audit_state",
]
