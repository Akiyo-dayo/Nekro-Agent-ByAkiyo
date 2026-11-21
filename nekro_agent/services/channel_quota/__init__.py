"""频道每日额度

平台方给每个群、每个私聊（按人）承诺的每日用量；租户可以用
`AI_CHAT_DAILY_REPLY_LIMIT` 在平台方额度以内再调低（自限，按回复回合数）。

- `policy`：三层（全局 / 实例 / 频道）逐字段继承，进程内缓存整表
- `counter`：按天的计数缓存，账本是它的可重算来源
- `service`：状态与统一闸门（平台额度先判，再判自限；暂停是算出来的，不落库）
- `self_limit`：租户自限的判定（回合数 + 仅保留的每小时均摊）
- `alerts`：跨阈值告警，按目的地合并后发送
- `notice`：暂停/自限期间当天第一次被叫到时回一句提示

功能默认关闭：全局策略行不存在或 `enabled` 不为真时，平台额度判定都返回「没暂停」，
计数照记但不拦任何回合、不发任何告警；自限仍按 `AI_CHAT_DAILY_REPLY_LIMIT` 独立生效。
"""

from .alerts import (
    ChannelQuotaAlert,
    ChannelQuotaAlertBuffer,
    alert_buffer,
    flush_alerts_now,
    maybe_enqueue,
)
from .counter import (
    REPLY_SCENES,
    add_bonus,
    get_daily,
    get_daily_bulk,
    quota_day,
    record_reply,
    record_tokens,
)
from .notice import send_pause_notice_once, send_self_limit_notice_once
from .policy import (
    ACTION_ALERT,
    ACTION_PAUSE,
    DEFAULT_PAUSE_MESSAGE,
    SCOPE_CHANNEL,
    SCOPE_GLOBAL,
    SCOPE_INSTANCE,
    UNIT_REPLIES,
    UNIT_TOKENS,
    EffectivePolicy,
    alert_percents,
    invalidate_policy_cache,
    resolve_policy,
    token_scope_is_reply_only,
)
from .self_limit import SelfLimitStatus, evaluate_self_limit
from .service import (
    STATE_DISABLED,
    STATE_EXCEEDED,
    STATE_NORMAL,
    STATE_PAUSED,
    STATE_UNLIMITED,
    STATE_WARNING,
    GateResult,
    QuotaStatus,
    background_paused,
    build_status,
    check_and_alert,
    evaluate,
    gate,
    is_paused,
)

__all__ = [
    "ACTION_ALERT",
    "ACTION_PAUSE",
    "DEFAULT_PAUSE_MESSAGE",
    "REPLY_SCENES",
    "SCOPE_CHANNEL",
    "SCOPE_GLOBAL",
    "SCOPE_INSTANCE",
    "STATE_DISABLED",
    "STATE_EXCEEDED",
    "STATE_NORMAL",
    "STATE_PAUSED",
    "STATE_UNLIMITED",
    "STATE_WARNING",
    "UNIT_REPLIES",
    "UNIT_TOKENS",
    "ChannelQuotaAlert",
    "ChannelQuotaAlertBuffer",
    "EffectivePolicy",
    "GateResult",
    "QuotaStatus",
    "SelfLimitStatus",
    "add_bonus",
    "alert_buffer",
    "alert_percents",
    "background_paused",
    "build_status",
    "check_and_alert",
    "evaluate",
    "evaluate_self_limit",
    "flush_alerts_now",
    "gate",
    "get_daily",
    "get_daily_bulk",
    "invalidate_policy_cache",
    "is_paused",
    "maybe_enqueue",
    "quota_day",
    "record_reply",
    "record_tokens",
    "resolve_policy",
    "send_pause_notice_once",
    "send_self_limit_notice_once",
    "token_scope_is_reply_only",
]
