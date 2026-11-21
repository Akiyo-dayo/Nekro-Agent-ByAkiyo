"""频道暂停提示：当天第一次被叫到时回一句

## 为什么只发一次，且靠一次条件更新决定谁发

暂停期间群里 @ 一次就回一句「额度用完了」，等于把额度提示变成刷屏器。设计是当天
**第一次被叫到**时提示一次，之后静默。两条消息几乎同时到达时，两边都会判成「还没发过」，
所以判据不能是「先读 `pause_notice_sent_at` 再比」，而是
`UPDATE ... SET pause_notice_sent_at = now WHERE id = ? AND pause_notice_sent_at IS NULL`，
**影响行数为 1 的那一路**才发。

## 发送失败不重试、不回滚

发送失败（实例掉线、协议端拒绝）之后如果回滚 `pause_notice_sent_at`，这个频道在恢复之前
每次被叫到都会再试一次，日志与协议端调用都会被刷满，而成功的概率一点没变。
宁可少提示一次，也不在一个发不出去的频道里反复尝试。

## 提示语必须过输出过滤

这句话以 Bot 的口吻在群里说，租户可以自己写（`tenant_pause_message`）。租户能写的内容
就必须能被他自己的输出过滤规则拦下——否则「自定义提示语」就是一个绕过词表的直投通道。
所以走 `filter_platform_request` + `outbound_filtered()`，与命令输出那条路径同形。

## 不跑 Agent、不计用量、不推 SYSTEM 消息

这是宿主直接经适配器说的一句话：它不是一轮对话，不该产生 token，也不该在聊天记录里
多出一条 SYSTEM 消息（那会进提示词，下一轮模型会以为系统刚说过话）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tortoise.timezone import now as db_now

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendSegment,
    PlatformSendSegmentType,
)
from nekro_agent.adapters.utils import adapter_utils
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily
from nekro_agent.schemas.errors import AdapterUnavailableError
from nekro_agent.services.output_filter.service import filter_platform_request, outbound_filtered

if TYPE_CHECKING:
    from nekro_agent.models.db_chat_channel import DBChatChannel

    from .service import QuotaStatus

logger = get_sub_logger("channel_quota")


async def send_pause_notice_once(channel: "DBChatChannel", status: "QuotaStatus") -> bool:
    """当天第一次被叫到时发一次暂停提示；发出去了返回 True

    Args:
        channel: 目标频道
        status: 当前状态（提示语从 `status.policy.pause_message` 取）
    """
    daily: DBChannelQuotaDaily | None = status.daily
    if daily is None:
        # 没有计数行就没有可写的 `pause_notice_sent_at`，也就没有「只发一次」的依据。
        # 这种情况只在额度恰好卡在边界时出现（用量 0 且额度 0 已被判成不限），
        # 不去补建行：读路径不建行是这张表的约定。
        return False

    message = (status.policy.pause_message or "").strip()
    if not message:
        # 提示语为空就当没提示。**先判再标记**：标记是「今天已经说过了」的唯一凭据，
        # 为一条空消息把它用掉，等于让这个频道当天再也等不到提示。
        return False

    affected = await DBChannelQuotaDaily.filter(id=daily.id, pause_notice_sent_at__isnull=True).update(
        pause_notice_sent_at=db_now(),
    )
    if affected != 1:
        return False

    try:
        adapter = await adapter_utils.get_adapter_for_chat(channel.chat_key)
    except AdapterUnavailableError as exc:
        # 不重试、不回滚：见模块文档
        logger.warning(f"[channel_quota] 暂停提示发送失败，适配器不可用: chat_key={channel.chat_key} | {exc}")
        return False

    request = PlatformSendRequest(
        chat_key=channel.chat_key,
        segments=[PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content=message)],
    )
    try:
        filtered = await filter_platform_request(request)
        with outbound_filtered():
            response = await adapter.forward_message(filtered)
    except Exception as exc:  # noqa: BLE001 - 提示发不出去不能反过来打掉消息处理
        logger.warning(f"[channel_quota] 暂停提示发送异常: chat_key={channel.chat_key} | {exc}")
        return False

    if not response.success:
        logger.warning(
            f"[channel_quota] 暂停提示未送达: chat_key={channel.chat_key} | {response.error_message}",
        )
        return False
    return True


__all__ = ["send_pause_notice_once"]
