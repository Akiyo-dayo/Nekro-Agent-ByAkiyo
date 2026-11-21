"""进程内的「确定发不出去」登记表

## 它解决什么

Agent 回合失败时会给用户发一条可见回执（见 `services/agent/failure_feedback.py`）。
但有一类失败是**确定性**的：实例的连接根本不在、Bot 已经被移出群。此时回执本身也发不出去，
只会再撞一次同样的错误：节日祝福这类广播落到 Bot 已离群的频道上时，每条失败后面还要
再补一条同样被拒的回执。

登记表只记「这一轮刚失败过」这类短时事实，不参与「该不该停用频道」的决策：
实例离线、Bot 离群都可能在下一条入站消息或一次成功发送时消失，过期时间只是兜底。

## 依赖约束

本模块刻意只依赖标准库与日志：适配器的 `BotInstanceOfflineError`、消息服务都要 import 它，
反过来 import 模型或适配器会形成循环。
"""

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Dict, Optional


class UndeliverableReason(StrEnum):
    """确定发不出去的原因"""

    INSTANCE_OFFLINE = "instance_offline"  # 实例的连接不在
    BOT_NOT_IN_GROUP = "bot_not_in_group"  # Bot 已不在这个群里


class ChannelUndeliverableError(Exception):
    """确定性的投递失败：实例离线、Bot 已不在群里……重试和失败回执都不会成功

    适配器自己的异常多继承它，上层（消息服务）不必认识具体适配器就能识别。
    """

    undeliverable_reason: UndeliverableReason = UndeliverableReason.INSTANCE_OFFLINE


# 登记的有效期。它只用来挡住「这一轮刚失败、紧接着的那条回执」，以及短时间内的重复回执；
# 过期是兜底，正常情况下由一次成功发送或一条入站消息提前清掉
DELIVERY_BLOCK_TTL_SECONDS = 3600

# 登记条数上限。键是 chat_key，频道数是不设上限的增长维度（一个实例几十个群、私聊……），
# 所以超过之后先清过期项，再让后来者覆盖旧项
_MAX_BLOCKS = 1024


@dataclass(frozen=True)
class _DeliveryBlock:
    reason: UndeliverableReason
    detail: str
    marked_at: float


class ChannelDeliveryBlocks:
    """进程内登记：哪些频道此刻确定发不出去"""

    def __init__(self) -> None:
        self._blocks: Dict[str, _DeliveryBlock] = {}

    def _prune_expired(self, now: float) -> None:
        for chat_key in [
            chat_key
            for chat_key, block in self._blocks.items()
            if now - block.marked_at >= DELIVERY_BLOCK_TTL_SECONDS
        ]:
            self._blocks.pop(chat_key, None)

    def mark(
        self,
        chat_key: str,
        reason: UndeliverableReason,
        detail: str = "",
        *,
        now: Optional[float] = None,
    ) -> None:
        """登记某个频道当前确定发不出去"""
        current = time.time() if now is None else now
        if len(self._blocks) > _MAX_BLOCKS:
            self._prune_expired(current)
        self._blocks[chat_key] = _DeliveryBlock(reason=reason, detail=detail, marked_at=current)

    def clear(self, chat_key: str) -> None:
        """清掉登记：一次成功发送或一条入站消息都说明它又能发了"""
        self._blocks.pop(chat_key, None)

    def get(self, chat_key: str, *, now: Optional[float] = None) -> Optional[UndeliverableReason]:
        """读取登记；过期的顺手删掉并返回 None"""
        block = self._blocks.get(chat_key)
        if block is None:
            return None
        current = time.time() if now is None else now
        if current - block.marked_at >= DELIVERY_BLOCK_TTL_SECONDS:
            self._blocks.pop(chat_key, None)
            return None
        return block.reason

    def reset(self) -> None:
        """清空全部登记（仅测试与进程内重置用）"""
        self._blocks.clear()


channel_delivery_blocks = ChannelDeliveryBlocks()
