"""频道每日额度的状态与判定

## 「已暂停」是算出来的，不是存下来的

暂停 = 超额动作是 `pause` 且当天用量 ≥ 生效额度 + 临时加额。它不落库，因此第二天零点
自动恢复、平台方给了临时加额也立刻恢复，不需要任何定时任务去「解除」，也不存在
「暂停标记忘了清」这类状态不一致。

## 判定在回合开始之前

额度只在一个回合**开始之前**判：已经开始的回合跑完为止，所以实际用量会略超额度
（多一个回合；token 计数还有后台写入的几秒滞后）。这是有意的——在回合中途打断会留下
发了一半的回复，比略超更糟。后台显示里用量可以超过 100%。

## 一个闸门里判两层

`gate` 先判平台方给这个频道的承诺，再判租户自己的回复上限（`AI_CHAT_DAILY_REPLY_LIMIT`，
按回合数、与额度同一个时区零点换天）。两层都放行才回复；任何一层判定出错都按放行处理。
`is_paused` 也走这个闸门，因此定时提醒、插件推送这类不经过入站消息路径的回合同样受自限约束。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily
from nekro_agent.models.db_chat_channel import DBChatChannel

from . import alerts
from .counter import get_daily, quota_day
from .policy import (
    ACTION_PAUSE,
    UNIT_REPLIES,
    UNIT_TOKENS,
    EffectivePolicy,
    alert_percents,
    resolve_policy,
    token_scope_is_reply_only,
)
from .self_limit import SelfLimitStatus, evaluate_self_limit

logger = get_sub_logger("channel_quota")

STATE_DISABLED = "disabled"
STATE_UNLIMITED = "unlimited"
STATE_NORMAL = "normal"
STATE_WARNING = "warning"
STATE_EXCEEDED = "exceeded"
STATE_PAUSED = "paused"


@dataclass(frozen=True)
class QuotaStatus:
    """某个频道在某一天的额度状态

    Attributes:
        enabled: 功能是否启用
        unit: 生效单位
        used: 已用量（单位见 `unit`）
        limit: 生效额度；0 表示不限
        bonus: 平台方当天临时加额
        allowed: 额度 + 临时加额；`limit == 0` 时也记 0（不限）
        percent: 用量占 `allowed` 的百分比；不限时为 0
        state: disabled / unlimited / normal / warning / exceeded / paused
        action: 超额动作（alert / pause），前端据此显示「会怎样」
        policy: 解析出来的生效策略
        daily: 当天的计数行；没有行时为 None（用量按 0 算）
    """

    enabled: bool
    unit: str
    used: int
    limit: int
    bonus: int
    allowed: int
    percent: int
    state: str
    action: str
    policy: EffectivePolicy
    daily: Optional[DBChannelQuotaDaily]


@dataclass(frozen=True)
class GateResult:
    """统一闸门的判定结果

    Attributes:
        blocked: 这一回合是否应该被拦下
        reason: `""` / `platform` / `self_daily` / `self_hourly`
        status: 平台方额度状态；平台判定出错时为 None
        self_status: 自限状态；平台已暂停或自限判定出错时为 None
    """

    blocked: bool
    reason: str
    status: Optional[QuotaStatus]
    self_status: Optional[SelfLimitStatus]


def _used_of(policy: EffectivePolicy, daily: Optional[DBChannelQuotaDaily]) -> int:
    """按单位取已用量

    token 单位下按全局配置决定读哪一列：`reply_only` 只算回复主链路与上下文规划，
    其余场景照样记账、照样显示，但不计入额度。两列都在计数时写好了，
    切换范围只是换一列读，不需要回算历史天。
    """
    if daily is None:
        return 0
    if policy.unit == UNIT_REPLIES:
        return int(daily.replies or 0)
    if token_scope_is_reply_only():
        return int(daily.tokens_reply or 0)
    return int(daily.tokens_all or 0)


def build_status(policy: EffectivePolicy, daily: Optional[DBChannelQuotaDaily]) -> QuotaStatus:
    """由策略与当天计数行算出状态（纯函数，便于单独验证状态机）"""
    used = _used_of(policy, daily)
    limit = int(policy.effective_limit or 0)
    bonus = int(daily.bonus or 0) if daily is not None else 0
    allowed = limit + bonus if limit > 0 else 0

    if not policy.enabled:
        return QuotaStatus(
            enabled=False,
            unit=policy.unit,
            used=used,
            limit=limit,
            bonus=bonus,
            allowed=allowed,
            percent=0,
            state=STATE_DISABLED,
            action=policy.over_limit_action,
            policy=policy,
            daily=daily,
        )

    if allowed <= 0:
        return QuotaStatus(
            enabled=True,
            unit=policy.unit,
            used=used,
            limit=limit,
            bonus=bonus,
            allowed=0,
            percent=0,
            state=STATE_UNLIMITED,
            action=policy.over_limit_action,
            policy=policy,
            daily=daily,
        )

    percent = int(used * 100 // allowed)
    if used >= allowed:
        state = STATE_PAUSED if policy.over_limit_action == ACTION_PAUSE else STATE_EXCEEDED
    else:
        thresholds = alert_percents()
        lowest = thresholds[0] if thresholds else 100
        state = STATE_WARNING if percent >= lowest else STATE_NORMAL

    return QuotaStatus(
        enabled=True,
        unit=policy.unit,
        used=used,
        limit=limit,
        bonus=bonus,
        allowed=allowed,
        percent=percent,
        state=state,
        action=policy.over_limit_action,
        policy=policy,
        daily=daily,
    )


async def evaluate(channel: DBChatChannel, *, day: Optional[str] = None) -> QuotaStatus:
    """算出某个频道在某一天（缺省今天）的额度状态

    功能关着时也把继承结果算出来（后台要显示「如果打开会是多少」），但 `state` 是
    `disabled`、`allowed` 仍是额度 + 加额，调用方只看 `state` 就不会误判。
    """
    key = day or quota_day()
    policy = await resolve_policy(
        channel.adapter_key,
        channel.instance_key,
        channel.chat_key,
        channel.channel_type,
    )
    daily = await get_daily(channel.chat_key, key) if policy.enabled else None
    return build_status(policy, daily)


async def is_paused(chat_key: str) -> bool:
    """这个频道此刻是否应该停止回复

    走统一闸门：平台方额度暂停或租户自限用尽都算暂停。频道行不存在、功能没开、
    额度不限、动作是只告警，都返回 False——调用方拿它当「要不要拦这一回合」的闸门，
    任何不确定的情况都必须放行，否则一次查库抖动就会让所有频道停止回复。
    """
    try:
        channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
        if channel is None:
            return False
        result = await gate(channel)
        return result.blocked
    except Exception as exc:  # noqa: BLE001 - 判定失败一律放行
        logger.warning(f"频道额度暂停判定失败，按未暂停处理: chat_key={chat_key} | {exc}")
        return False


async def background_paused(chat_key: str) -> bool:
    """这个频道的后台调用此刻是否应该停掉

    三个条件**同时**成立才返回 True，每一条都有独立的理由：

    1. 频道此刻是暂停状态（`is_paused` 为真）。后台调用花的是平台方承诺给这个频道的那份
       额度，额度已经用尽还继续烧后台调用，等于把「今天用完了」这句话说给客户听的同时
       又替他把明天的份额提前花掉。
    2. 单位是 token。按回复次数计额度时，后台调用本来就不占额度——它们不产生「回合」，
       停掉它们省不下任何回复次数，只会让记忆与好感停滞。
    3. `CHANNEL_QUOTA_TOKEN_SCOPE` 不是 `reply_only`。只算回复链路时，后台调用的 token
       不计入额度，也就没有「因为额度用尽所以要停」这回事；它们照常跑，照常记账，
       照常出现在后台的场景拆分里。

    频道摘要与记忆沉淀是**同一次模型调用**的产物，它跟着沉淀一起停，不需要单独判断；
    重建（管理员手动发起）也不受这里影响，那是人的明确动作，不是后台自动行为。

    任何异常都返回 False（放行）并记 warning：这是挂在后台任务入口上的闸门，
    一次查库抖动不该让所有频道的记忆与好感一起停摆。
    """
    try:
        channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
        if channel is None:
            return False
        status = await evaluate(channel)
        if status.state != STATE_PAUSED:
            return False
        if status.unit != UNIT_TOKENS:
            return False
        return not token_scope_is_reply_only()
    except Exception as exc:  # noqa: BLE001 - 判定失败一律放行
        logger.warning(f"频道额度后台暂停判定失败，按不停处理: chat_key={chat_key} | {exc}")
        return False


async def check_and_alert(channel: DBChatChannel) -> QuotaStatus:
    """算状态并做一次告警评估

    告警评估只在这里与回合收尾处触发，两处都走同一份判据（`alerts.maybe_enqueue`）。
    """
    status = await evaluate(channel)
    try:
        await alerts.maybe_enqueue(channel, status)
    except Exception as exc:  # noqa: BLE001 - 告警失败不影响回复
        logger.warning(f"频道额度告警评估失败（已忽略）: chat_key={channel.chat_key} | {exc}")
    return status


async def gate(channel: DBChatChannel) -> GateResult:
    """统一闸门：平台方额度先判，再判租户自限

    两段各自兜底：任何异常都按放行处理并记 warning。判定在每条入站消息的路径上，
    不能自己变成「群友说一句话、Bot 报一个错」的故障点。平台方已经暂停时不再算自限，
    `reason` 以平台方为准。
    """
    status: Optional[QuotaStatus] = None
    try:
        status = await check_and_alert(channel)
        if status.state == STATE_PAUSED:
            return GateResult(blocked=True, reason="platform", status=status, self_status=None)
    except Exception as exc:  # noqa: BLE001 - 判定失败一律放行
        logger.warning(f"频道额度平台判定失败，按放行处理: chat_key={channel.chat_key} | {exc}")
        status = None

    self_status: Optional[SelfLimitStatus] = None
    try:
        self_status = await evaluate_self_limit(channel)
        if self_status.exceeded:
            reason = "self_daily" if self_status.exceeded == "daily" else "self_hourly"
            return GateResult(blocked=True, reason=reason, status=status, self_status=self_status)
    except Exception as exc:  # noqa: BLE001 - 判定失败一律放行
        logger.warning(f"频道自限判定失败，按放行处理: chat_key={channel.chat_key} | {exc}")
        self_status = None

    return GateResult(blocked=False, reason="", status=status, self_status=self_status)


__all__ = [
    "STATE_DISABLED",
    "STATE_EXCEEDED",
    "STATE_NORMAL",
    "STATE_PAUSED",
    "STATE_UNLIMITED",
    "STATE_WARNING",
    "GateResult",
    "QuotaStatus",
    "background_paused",
    "build_status",
    "check_and_alert",
    "evaluate",
    "gate",
    "is_paused",
]
