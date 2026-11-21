"""运维写操作的回执

## 回执为什么由宿主直接发

模型可能把回执改写成「已经处理好了」，而实际上业务函数失败了；也可能在操作后继续
说话，让「发生了什么」淹没在对话里。回执因此不经过模型：宿主在写操作执行后（成功或
失败）按固定格式直接发一条到当前频道，群里所有人都看得到，有问题可以立刻撤回。
它也是「直接执行、不二次确认」这道设计里的可见性。

## 不推 SYSTEM、不调度回合、不计数

这与 `channel_quota/notice.py` 的发送段同形：它是宿主直接经适配器说的一句话，
不是一轮对话。写成 SYSTEM 消息会进下一轮提示词（模型会以为系统刚说过话），
调度回合会把回执再喂给模型，按回合数计的额度也不该被一条回执消耗。

## 发送失败只记 warning

回执发不出去时业务已经执行完了；把异常抛回去只会让调用方以为写操作失败。
返回 `False` 让调用方（工具或命令）自己决定要不要在回复里提一句，但工具侧不依赖它。
"""

from __future__ import annotations

from typing import Iterable, Optional

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendSegment,
    PlatformSendSegmentType,
)
from nekro_agent.adapters.utils import adapter_utils
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.errors import AdapterUnavailableError
from nekro_agent.services.output_filter.service import filter_platform_request, outbound_filtered

from .text import ident
from .writes import WriteOutcome

logger = get_sub_logger("ops_agent.receipt")

MAX_CANDIDATES = 5


def _render_operators(operators: Iterable[str]) -> str:
    """按字典序、顿号连接；空集合写 `-`"""
    items = sorted({str(item).strip() for item in operators if str(item).strip()})
    return "、".join(ident(item) for item in items) if items else "-"


def format_receipt(outcome: WriteOutcome, operators: Iterable[str], record_id: Optional[int] = None) -> str:
    """固定格式的回执正文

    成功：`【运维】已执行：{动作} {目标}。操作人 {人}。`
    失败：`【运维】未执行：{动作} {目标}。原因：{类别}。` + 可选候选行 + `操作人 {人}。`

    有记录号时在句尾补「，记录号 {id}」：群里的人可以拿它去后台审计页对上那一行。
    `record_id` 为 `None` 时与改造前逐字一致。
    """
    people = _render_operators(operators)
    tail = f"操作人 {people}"
    if record_id is not None:
        tail = f"{tail}，记录号 {record_id}"
    tail = f"{tail}。"

    if outcome.ok:
        return f"【运维】已执行：{outcome.action_label} {outcome.target_label}。{tail}"

    reason = f"原因：{outcome.error_category}。"
    if outcome.candidates:
        shown = "、".join(ident(item) for item in outcome.candidates[:MAX_CANDIDATES])
        reason = f"{reason}候选：{shown}。"
    return f"【运维】未执行：{outcome.action_label} {outcome.target_label}。{reason}{tail}"


async def send_receipt(chat_key: str, text: str) -> bool:
    """把回执直接发到频道；任何异常都只记 warning 并返回 False"""
    key = str(chat_key or "").strip()
    body = str(text or "").strip()
    if not key or not body:
        return False
    try:
        adapter = await adapter_utils.get_adapter_for_chat(key)
    except AdapterUnavailableError as exc:
        logger.warning(f"[ops_agent] 回执发送失败，适配器不可用: chat_key={key} | {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - 回执发不出去不能反过来打掉写操作
        logger.warning(f"[ops_agent] 回执发送失败: chat_key={key} | {exc}")
        return False

    try:
        request = PlatformSendRequest(
            chat_key=key,
            segments=[PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content=body)],
        )
        filtered = await filter_platform_request(request)
        with outbound_filtered():
            response = await adapter.forward_message(filtered)
    except Exception as exc:  # noqa: BLE001 - 回执发不出去不能反过来打掉写操作
        logger.warning(f"[ops_agent] 回执发送异常: chat_key={key} | {exc}")
        return False

    if not response.success:
        logger.warning(f"[ops_agent] 回执未送达: chat_key={key} | {response.error_message}")
        return False
    return True


__all__ = ["MAX_CANDIDATES", "format_receipt", "send_receipt"]
