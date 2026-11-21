"""Bot 自身在群里的增减：自动停用与自动恢复

## 为什么要单独判「Bot 自己」

`group_decrease` / `group_increase` 通知里，普通成员进出与 Bot 自己被踢 / 被拉回来是同一类事件。
普通成员退群只该给 Agent 一条提醒；Bot 自己离群则意味着**这个群已经发不出任何东西**，
再让 Agent 跑一轮只会在发送时被 QQ 拒绝。反过来，Bot 重新入群或群里有人说话时，
系统自动停用的频道应当自己恢复，不需要运维记得回来手动点一下。

## 与手动停用的边界

是否自动恢复只看频道 `data` 里的自动停用标记（见 `DBChatChannel.auto_disable`）。
后台 / 聊天命令的启停会清掉这个标记，因此人的明确操作优先，系统不会把手动停用的频道改回来。

## 「Bot 不在群」标记也在这里清

主动核对（`tools/bot_membership_check.py`）打上的 `bot_membership=not_in_group` 只是一个待人工
处理的提示，不是停用状态。这个群后来又来了入站消息、或收到 Bot 自己入群的通知，都说明
「Bot 现在确实在群里」，因此入站处理的最前面会顺手把标记清掉；它不改 `is_active`，
所以与自动恢复是两条互不影响的路径。
"""

import re
from typing import Any, Mapping

from nonebot.adapters.onebot.v11.exception import ActionFailed

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import (
    BotMembershipSource,
    ChannelAutoDisableReason,
    DBChatChannel,
)
from nekro_agent.services.channel_broadcaster import channel_broadcaster
from nekro_agent.services.channel_delivery import UndeliverableReason, channel_delivery_blocks

logger = get_sub_logger("adapter.onebot_v11")

# 发送被拒、原因是「Bot 已不在群里」的判据。只认实测见过的文案（SnowLuma 回的 wording），
# 不按 retcode 判：retcode=100 是笼统的「失败」，被禁言、发图失败也是它
_NOT_IN_GROUP_PATTERN = re.compile(r"result=110\b|已被移出该群")


def is_bot_self_leave(event_dict: Mapping[str, Any], self_id: str) -> bool:
    """这条群成员减少通知是不是 Bot 自己离开（被踢或主动退群）"""
    if event_dict.get("notice_type") != "group_decrease":
        return False
    return event_dict.get("sub_type") == "kick_me" or str(event_dict.get("user_id")) == str(self_id)


def is_bot_self_join(event_dict: Mapping[str, Any], self_id: str) -> bool:
    """这条群成员增加通知是不是 Bot 自己重新入群"""
    if event_dict.get("notice_type") != "group_increase":
        return False
    return str(event_dict.get("user_id")) == str(self_id)


def is_bot_not_in_group_failure(error: ActionFailed) -> bool:
    """发送被拒的原因是不是「Bot 已不在这个群里」"""
    text = " ".join(str(value) for value in error.info.values())
    return _NOT_IN_GROUP_PATTERN.search(text) is not None


def _build_left_group_detail(event_dict: Mapping[str, Any]) -> str:
    """离群通知的可读说明，写进自动停用标记与后台"""
    sub_type = str(event_dict.get("sub_type") or "")
    if sub_type == "kick_me":
        return f"被移出群（操作者 {event_dict.get('operator_id', '')}）"
    if sub_type == "leave":
        return "主动退群"
    return f"群成员减少通知 sub_type={sub_type}"


async def _publish_channel_update(db_chat_channel: DBChatChannel) -> None:
    """把频道状态变化推给后台（参数与频道管理路由里的写法一致）"""
    await channel_broadcaster.publish_update(
        event_type="updated",
        chat_key=db_chat_channel.chat_key,
        channel_name=db_chat_channel.channel_name,
        custom_channel_name=db_chat_channel.get_custom_channel_name(),
        is_active=db_chat_channel.is_active,
        status=db_chat_channel.channel_status.value,
    )


async def handle_bot_left_group(db_chat_channel: DBChatChannel, event_dict: Mapping[str, Any]) -> None:
    """Bot 自己离群：登记频道发不出去，并自动停用

    停用之前先登记投递失败，让这一轮里可能已经在排队的失败回执也被挡住。
    """
    detail = _build_left_group_detail(event_dict)
    channel_delivery_blocks.mark(
        db_chat_channel.chat_key,
        UndeliverableReason.BOT_NOT_IN_GROUP,
        detail,
    )

    changed = await db_chat_channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP, detail)
    if changed:
        logger.warning(
            f"Bot 已离开频道 {db_chat_channel.chat_key}（{detail}），频道已自动停用；"
            "重新入群或群里再有消息时会自动恢复",
        )
        await _publish_channel_update(db_chat_channel)
    else:
        logger.info(f"Bot 已离开频道 {db_chat_channel.chat_key}（{detail}），频道本来就是停用状态，保持不变")


async def restore_after_bot_returned(db_chat_channel: DBChatChannel, trigger: str) -> bool:
    """收到这个频道的入站事件：实例在线、Bot 也在群里，清登记并尝试恢复自动停用

    Returns:
        是否真的恢复了频道
    """
    channel_delivery_blocks.clear(db_chat_channel.chat_key)

    if await db_chat_channel.clear_bot_not_in_group(BotMembershipSource.INBOUND):
        logger.info(f"频道 {db_chat_channel.chat_key} 的「Bot 不在群」标记已清除（触发：{trigger}）")

    if db_chat_channel.is_active:
        # 快路径：启用中的频道不需要解析 data
        return False

    restored = await db_chat_channel.restore_auto_disabled(ChannelAutoDisableReason.BOT_LEFT_GROUP)
    if restored:
        logger.info(f"频道 {db_chat_channel.chat_key} 已自动恢复（触发：{trigger}）")
        await _publish_channel_update(db_chat_channel)
    return restored
