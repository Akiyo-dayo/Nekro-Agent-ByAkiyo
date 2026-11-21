"""频道每日额度的策略解析

## 逐字段继承，而不是整行覆盖

三层（全局 / 实例 / 频道）里每一行只写自己想改的那几列，其余留 NULL 表示「跟上一层」。
整行覆盖的写法会让平台方在实例层只想改一个额度时，把全局的开关、单位、超额动作
一并复制过来，此后全局再改就传不下去。逐字段继承的代价是解析时要做三次合并，
好处是每一层的意图都能独立表达。

## 整表缓存在进程里

这张表的行数等于「1 + 实例数 + 设过策略的频道数」，是几十到几百的量级，而解析发生在
**每条入站消息**的判定路径上。每次判定查三行库没有必要，所以首次用到时整表读进进程。
任何经本模块写入策略的操作之后立即失效重读，另设 60 秒 TTL 兜底：多实例部署下别的进程
改了策略，本进程最多晚一分钟看到，这个延迟对「每日额度」这种量级是可接受的。

## 租户值只在同一个单位下算数

租户能填的只是「在平台方给的额度以内再调低」，填的是一个数字，而单位是平台方的。
平台方把单位从回复数换成 token 之后，租户那个数字既不是回复数也不是 token 数，
只能作废（视为未设），而不是猜一个换算关系——猜错的方向可能是把额度放大十倍。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_channel_quota import DBChannelQuotaPolicy

logger = get_sub_logger("channel_quota")

DEFAULT_PAUSE_MESSAGE = "今天的额度用完了，明天见"

UNIT_REPLIES = "replies"
UNIT_TOKENS = "tokens"
UNIT_VALUES: Tuple[str, ...] = (UNIT_REPLIES, UNIT_TOKENS)

ACTION_ALERT = "alert"
ACTION_PAUSE = "pause"
ACTION_VALUES: Tuple[str, ...] = (ACTION_ALERT, ACTION_PAUSE)

SCOPE_GLOBAL = "global"
SCOPE_INSTANCE = "instance"
SCOPE_CHANNEL = "channel"

# 缓存寿命。见模块文档：多实例部署下别的进程改了策略，本进程最多晚这么久看到。
_CACHE_TTL_SECONDS = 60.0

# 整表快照。`_cache_loaded_at` 为 None 表示还没读过。
_rows: Optional[List[DBChannelQuotaPolicy]] = None
_cache_loaded_at: Optional[float] = None


@dataclass(frozen=True)
class EffectivePolicy:
    """某个频道此刻生效的额度策略

    Attributes:
        enabled: 功能是否启用（全局关着时一律 False，其余字段只是把继承结果算出来）
        unit: 生效单位
        is_group: 是否按群额度（只有 `channel_type == "group"` 才是）
        platform_limit: 平台方给的额度；0 表示不限
        tenant_limit: 租户调低的值；None 表示未设
        effective_limit: 两层合并后的额度；0 表示不限
        over_limit_action: 超额动作
        pause_message: 暂停后第一次被叫到时回的提示
    """

    enabled: bool
    unit: str
    is_group: bool
    platform_limit: int
    tenant_limit: Optional[int]
    effective_limit: int
    over_limit_action: str
    pause_message: str


def invalidate_policy_cache() -> None:
    """丢掉进程内的策略快照，下一次解析重新读表

    任何经本模块写入策略的操作之后都要调它。平台方与租户两组接口共用这一个出口，
    免得出现「平台方改完生效、租户改完要等一分钟」这种一半新一半旧的中间态。
    """
    global _rows, _cache_loaded_at  # noqa: PLW0603
    _rows = None
    _cache_loaded_at = None


async def _load_rows() -> List[DBChannelQuotaPolicy]:
    """读出整张策略表（带进程内缓存与 TTL）"""
    global _rows, _cache_loaded_at  # noqa: PLW0603
    now = time.monotonic()
    if _rows is not None and _cache_loaded_at is not None and now - _cache_loaded_at < _CACHE_TTL_SECONDS:
        return _rows
    try:
        _rows = list(await DBChannelQuotaPolicy.all())
        _cache_loaded_at = now
    except Exception as exc:  # noqa: BLE001 - 读不到策略时按「没有策略」处理，不能把消息路径打掉
        logger.warning(f"读取频道额度策略失败，按未配置处理: {exc}")
        return []
    return _rows


def _row_key(row: DBChannelQuotaPolicy) -> Tuple[str, str, str, str]:
    return (row.scope, row.adapter_key, row.instance_key, row.chat_key)


def _index(rows: List[DBChannelQuotaPolicy]) -> Dict[Tuple[str, str, str, str], DBChannelQuotaPolicy]:
    """把行编成「作用域四元组 → 行」的字典

    唯一键在数据库上保证了一个作用域只有一行；万一库里缺了这条约束而出现重复，
    取 id 最小的那一行，与写接口 `_policy_row` 的 `order_by("id").first()` 读到的是同一行，
    免得「后台显示的」和「判定用的」不是同一份策略。
    """
    indexed: Dict[Tuple[str, str, str, str], DBChannelQuotaPolicy] = {}
    for row in sorted(rows, key=lambda item: item.id or 0):
        indexed.setdefault(_row_key(row), row)
    return indexed


def _first_set(*values: Optional[object]) -> Optional[object]:
    """逐字段继承：从左到右取第一个非 None"""
    for value in values:
        if value is not None:
            return value
    return None


def _layer_values(rows: Iterable[Optional[DBChannelQuotaPolicy]], name: str) -> List[Optional[object]]:
    """取若干层同一列的值（缺层的跳过），从下往上排好交给 `_first_set`"""
    return [getattr(row, name) for row in rows if row is not None]


def _channel_limit_field(is_group: bool) -> str:
    return "group_daily_limit" if is_group else "private_daily_limit"


def _tenant_limit_field(is_group: bool) -> str:
    return "tenant_group_daily_limit" if is_group else "tenant_private_daily_limit"


def _compose(
    global_row: Optional[DBChannelQuotaPolicy],
    instance_row: Optional[DBChannelQuotaPolicy],
    channel_row: Optional[DBChannelQuotaPolicy],
    *,
    is_group: bool,
) -> EffectivePolicy:
    """把三层行合并成一份生效策略（纯函数，便于单独验证继承规则）"""
    rows = (channel_row, instance_row, global_row)
    limit_field = _channel_limit_field(is_group)

    # 开关是唯一一个「只能关不能开」的字段：平台方的全局那一行是总闸，没有它（或它关着）
    # 就等于这个功能不存在——存量部署升级上来不该因为某一行历史数据突然开始拦消息。
    # 具体的层能把总闸关得更小（这个实例先别用），但翻不开它。
    master_enabled = bool(global_row.enabled) if global_row is not None else False
    scoped_enabled = _first_set(*_layer_values((channel_row, instance_row), "enabled"))
    enabled = master_enabled and (bool(scoped_enabled) if scoped_enabled is not None else True)
    unit = str(_first_set(*_layer_values(rows, "unit")) or UNIT_REPLIES)
    action = str(_first_set(*_layer_values(rows, "over_limit_action")) or ACTION_ALERT)
    platform_limit = int(_first_set(*_layer_values(rows, limit_field)) or 0)

    # 租户值只看频道与实例两层，且必须与当前生效单位一致才算数
    tenant_field = _tenant_limit_field(is_group)
    tenant_limit: Optional[int] = None
    for row in (channel_row, instance_row):
        if row is None:
            continue
        value = getattr(row, tenant_field)
        if value is None:
            continue
        if (row.tenant_limit_unit or "") != unit:
            # 单位被平台方改过，旧数字没有意义：视为未设，而不是猜一个换算
            continue
        tenant_limit = int(value)
        break

    if platform_limit <= 0:
        # 平台方不限：租户自己设的上限才是唯一的约束；没设就是不限
        effective_limit = tenant_limit if tenant_limit is not None else 0
    elif tenant_limit is None:
        effective_limit = platform_limit
    else:
        effective_limit = min(platform_limit, tenant_limit)

    pause_message = ""
    for row in (channel_row, instance_row):
        if row is not None and row.tenant_pause_message:
            pause_message = row.tenant_pause_message.strip()
            break
    if not pause_message:
        platform_message = _first_set(*_layer_values(rows, "pause_message"))
        pause_message = str(platform_message).strip() if platform_message else ""
    if not pause_message:
        pause_message = DEFAULT_PAUSE_MESSAGE

    return EffectivePolicy(
        enabled=enabled,
        unit=unit,
        is_group=is_group,
        platform_limit=platform_limit,
        tenant_limit=tenant_limit,
        effective_limit=effective_limit,
        over_limit_action=action,
        pause_message=pause_message,
    )


async def resolve_policy(
    adapter_key: str,
    instance_key: str,
    chat_key: str,
    channel_type: Optional[str],
) -> EffectivePolicy:
    """解析某个频道此刻生效的额度策略

    Args:
        adapter_key: 适配器标识
        instance_key: 实例标识
        chat_key: 会话标识
        channel_type: 频道类型；只有 `group` 用群额度，其余（私聊、网页聊天、邮件等
            一对一形态）用私聊额度

    Returns:
        EffectivePolicy: 逐字段合并后的结果。全局行不存在时用默认值（功能关闭、
        单位回复数、两种额度 0、只告警、默认提示语）。
    """
    rows = await _load_rows()
    indexed = _index(rows)
    global_row = indexed.get((SCOPE_GLOBAL, "", "", ""))
    instance_row = indexed.get((SCOPE_INSTANCE, adapter_key, instance_key, "")) if instance_key else None
    channel_row = indexed.get((SCOPE_CHANNEL, adapter_key, instance_key, chat_key)) if chat_key else None
    return _compose(global_row, instance_row, channel_row, is_group=(channel_type or "") == "group")


def token_scope_is_reply_only() -> bool:
    """token 是否只计回复主链路（读全局配置，非法值按 `all` 处理）"""
    return str(getattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "all") or "all").strip().lower() == "reply_only"


def alert_percents() -> List[int]:
    """告警阈值（百分比），去重后升序；非法值忽略"""
    values: List[int] = []
    for raw in getattr(config, "CHANNEL_QUOTA_ALERT_PERCENTS", None) or []:
        try:
            percent = int(raw)
        except (TypeError, ValueError):
            continue
        if percent > 0 and percent not in values:
            values.append(percent)
    return sorted(values)


__all__ = [
    "ACTION_ALERT",
    "ACTION_PAUSE",
    "ACTION_VALUES",
    "DEFAULT_PAUSE_MESSAGE",
    "SCOPE_CHANNEL",
    "SCOPE_GLOBAL",
    "SCOPE_INSTANCE",
    "UNIT_REPLIES",
    "UNIT_TOKENS",
    "UNIT_VALUES",
    "EffectivePolicy",
    "alert_percents",
    "invalidate_policy_cache",
    "resolve_policy",
    "token_scope_is_reply_only",
]
