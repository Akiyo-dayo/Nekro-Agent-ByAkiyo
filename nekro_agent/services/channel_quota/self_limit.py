"""租户自限：每个频道每天最多回复多少个回合，以及每小时的均摊

这是「在平台方给的额度以内，自己再调低一档」的那层限制，来自可覆盖配置
`AI_CHAT_DAILY_REPLY_LIMIT`，语义是**回复回合数**而不是 Bot 消息条数：一轮回复常常
拆成几条消息发出去，按条数算等于把一次回复收几次费。

日界与频道额度共用 `CHANNEL_QUOTA_TIMEZONE`；当天用了多少直接读计数表
`DBChannelQuotaDaily.replies`，不再回头数聊天记录。临时加额仍来自
`quota_service`（内存、当天有效），它与额度按同一个日期键换天。

每小时的均摊也是按回合：读 `counter.hour_replies(chat_key)`，即本进程记的当前整点小时
回合数（与每日上限同一个加数点）。**计数只在进程内**，重启后这一小时从 0 起，所以重启后
的头一个小时均摊会偏松；每日上限读库，不受影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nekro_agent.services.quota_service import quota_service

from . import counter

if TYPE_CHECKING:
    from nekro_agent.models.db_chat_channel import DBChatChannel


@dataclass(frozen=True)
class SelfLimitStatus:
    """某个频道当天自限的判定结果

    Attributes:
        limit: 生效的 `AI_CHAT_DAILY_REPLY_LIMIT`；0（或负数）表示不限
        boost: 当天临时加额（`quota_service.get_boost`）
        allowed: `limit + boost`；`limit <= 0` 时为 0（不限）
        used: 当天已完成的回复回合数，即 `DBChannelQuotaDaily.replies`；没有行按 0
        hourly_enabled: 是否启用了每小时均摊限制
        hourly_limit: 每小时上限；没启用或 `allowed <= 0` 时为 0
        hourly_used: 当前整点小时的回合数（进程内计数，重启后从 0 起）
        exceeded: `""` / `"daily"` / `"hourly"`
    """

    limit: int
    boost: int
    allowed: int
    used: int
    hourly_enabled: bool
    hourly_limit: int
    hourly_used: int
    exceeded: str


async def evaluate_self_limit(channel: "DBChatChannel") -> SelfLimitStatus:
    """算出某个频道当天是否撞到自限

    `limit <= 0` 是「不限」：直接返回，不读计数表、不读当前小时的计数。判定在每条入站
    消息的路径上，为了一次「本来就不拦」的判定去查库没有意义。
    """
    effective_config = await channel.get_effective_config()
    limit = int(effective_config.AI_CHAT_DAILY_REPLY_LIMIT or 0)
    hourly_enabled = bool(effective_config.AI_CHAT_ENABLE_HOURLY_LIMIT)
    # 临时加额是进程内数据，读它不查库；`limit <= 0` 的快速返回也把它带上
    boost = int(quota_service.get_boost(channel.chat_key))

    if limit <= 0:
        return SelfLimitStatus(
            limit=limit,
            boost=boost,
            allowed=0,
            used=0,
            hourly_enabled=hourly_enabled,
            hourly_limit=0,
            hourly_used=0,
            exceeded="",
        )

    allowed = limit + boost

    daily = await counter.get_daily(channel.chat_key, counter.quota_day())
    used = int(daily.replies or 0) if daily is not None else 0

    hourly_limit = quota_service.calculate_hourly_quota(allowed) if hourly_enabled else 0
    hourly_used = 0
    if hourly_enabled and allowed > 0:
        # 进程内计数，不查库：一轮回复拆成几条消息都只算一次
        hourly_used = counter.hour_replies(channel.chat_key)

    if used >= allowed:
        exceeded = "daily"
    elif hourly_enabled and hourly_used >= hourly_limit:
        exceeded = "hourly"
    else:
        exceeded = ""

    return SelfLimitStatus(
        limit=limit,
        boost=boost,
        allowed=allowed,
        used=used,
        hourly_enabled=hourly_enabled,
        hourly_limit=hourly_limit,
        hourly_used=hourly_used,
        exceeded=exceeded,
    )


__all__ = ["SelfLimitStatus", "evaluate_self_limit"]
