"""广播类推送的目标筛选

## 为什么要在推送前筛

「向一批频道推一条系统消息并触发 Agent」的广播（节日祝福是第一个）会在几分钟到几小时内
挨个处理几百个频道。不加筛选时，代价不是多花几条消息，而是：

- **实例离线的频道**一启动就失败。Agent 在 `get_self_info` 那一步就抛
  `BotInstanceOfflineError`，这条广播对这些频道从头到尾没有任何成功可能，只是白跑一轮。
- **长期没人说话的群**多半是 Bot 已经被移出的群。Agent 仍会正常跑完整个回合、调模型、
  产出回复，最后在发送时被 QQ 拒绝（`你已被移出该群`），每条还要再补一条失败回执，
  又被拒一次。
- **定期核对已经标成「Bot 不在群」的频道**是同一个下场，而且不必靠活跃度去猜：核对拿协议端
  的群列表比对过，标记就是证据。这一条两种名单模式都适用——点名的频道 Bot 不在群里也发不出去。

## 为什么按「入站消息」判活，而不是频道的 `update_time`

`update_time` 会被**广播自己**刷新：推送一条系统消息就会写库、更新频道行。用它判活的话，
一个死群只要被广播碰过一次，就会在下一轮看起来「刚刚活跃」，永远筛不掉。所以判据只能是
「这个频道里最近有没有真实的人说过话」——Bot 自己的回复（`sender_id="-1"`）与系统消息
同样是广播写进去的，也不能算。
"""

import time
from enum import StrEnum
from typing import Optional

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.console import InstanceStatus
from nekro_agent.services.channel_quota.service import is_paused

# 已知连不上的实例状态（取值见 `nekro_agent/schemas/console.py:InstanceStatus`）。
# 不含 `connecting`：那是正在连，可能马上就上来；也不含 `disabled` / `archived`：那两种由
# `DBAdapterInstance.is_instance_active` 在推送入口统一挡掉，这里不重复判
OFFLINE_INSTANCE_STATUSES: frozenset[str] = frozenset(
    {
        InstanceStatus.OFFLINE.value,
        InstanceStatus.UNBOUND.value,
        InstanceStatus.LOGIN_REQUIRED.value,
        InstanceStatus.ERROR.value,
    },
)


class BroadcastSkipReason(StrEnum):
    """这个频道不该收到本次广播的原因"""

    INSTANCE_OFFLINE = "instance_offline"
    BOT_NOT_IN_GROUP = "bot_not_in_group"
    NO_RECENT_INBOUND = "no_recent_inbound"
    CHANNEL_QUOTA_PAUSED = "channel_quota_paused"


async def is_channel_instance_offline(channel: DBChatChannel) -> bool:
    """频道所属实例是否处于「确定连不上」的状态

    实例标识为空（历史数据）、实例行不存在时一律返回 False：那属于数据异常，
    由更下游的发送环节给出准确报错，这里抢先把频道判成离线只会掩盖真正的原因。
    状态比较做去空白与小写，容忍手工写库留下的 ` Offline ` 这类脏值。
    """
    if not channel.instance_key:
        return False
    instance = (
        await DBAdapterInstance.filter(
            adapter_key=channel.adapter_key,
            instance_key=channel.instance_key,
        )
        .order_by("id")
        .first()
    )
    if instance is None:
        return False
    return instance.status.strip().lower() in OFFLINE_INSTANCE_STATUSES


async def has_inbound_message_since(chat_key: str, since_timestamp: int) -> bool:
    """频道里在 `since_timestamp` 之后有没有人来过消息

    `sender_id="-1"` 是 Bot 自己的回复和系统消息（节日祝福本身就是系统消息），
    都不算「群里有人说话」。
    """
    return await (
        DBChatMessage.filter(chat_key=chat_key, send_timestamp__gte=since_timestamp)
        .exclude(sender_id="-1")
        .exists()
    )


async def get_broadcast_skip_reason(
    channel: DBChatChannel,
    *,
    inactive_days: int,
    now: Optional[float] = None,
) -> Optional[BroadcastSkipReason]:
    """判断某个频道是否应跳过本次广播；`None` 表示照常推送

    Args:
        channel: 推送前重新取到的频道行（不是广播开始时缓存的那份）
        inactive_days: 超过这么多天没有入站消息就跳过；0 表示不按这一条跳过
        now: 当前时间戳，便于测试注入；缺省取 `time.time()`
    """
    if await is_channel_instance_offline(channel):
        return BroadcastSkipReason.INSTANCE_OFFLINE

    # 只读频道行上已有的标记，不查库也不问协议端；标记的含义见 `DBChatChannel.is_bot_marked_not_in_group`
    if channel.is_bot_marked_not_in_group():
        return BroadcastSkipReason.BOT_NOT_IN_GROUP

    # 频道额度暂停：广播会触发一轮 Agent，而暂停中的频道不开回合。放在活跃度判断之前，
    # 两种名单模式都适用——白名单点名的频道也该跳过，额度是平台方的承诺，点名不改变它。
    if await is_paused(channel.chat_key):
        return BroadcastSkipReason.CHANNEL_QUOTA_PAUSED

    if inactive_days > 0:
        current = time.time() if now is None else now
        since_timestamp = int(current - inactive_days * 86400)
        if not await has_inbound_message_since(channel.chat_key, since_timestamp):
            return BroadcastSkipReason.NO_RECENT_INBOUND

    return None
