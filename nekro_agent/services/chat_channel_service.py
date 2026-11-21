"""频道启停

总后台的 `/chat-channel/{chat_key}/active` 与运维通道共用这一处。启停不只是改一列：
还要把新状态广播给正在看列表的客户端，否则后台页面会停在旧状态直到手动刷新。
两处各写一遍的话，广播这一半迟早只留在其中一条路径上。

批量停用（Bot 不在群）目前仍在自己循环里调用同样的两步；它按批处理、统计成功数，
形状与单个启停不同，这里不强行合并。
"""

from __future__ import annotations

from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.services.channel_broadcaster import channel_broadcaster


async def set_channel_active(channel: DBChatChannel, active: bool) -> DBChatChannel:
    """启用 / 停用一个频道，并广播更新"""
    await channel.set_channel_status("active" if active else "disabled")
    await channel_broadcaster.publish_update(
        event_type="updated",
        chat_key=channel.chat_key,
        channel_name=channel.channel_name,
        custom_channel_name=channel.get_custom_channel_name(),
        is_active=channel.is_active,
        status=channel.channel_status,
    )
    return channel


__all__ = ["set_channel_active"]
