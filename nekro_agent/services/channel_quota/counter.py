"""频道每日额度的计数

## 为什么是「有则加、无则建」而不是先读后写

计数发生在**每条入站消息的判定路径**与**每个回合的收尾**上，而这两处都会并发：
两条消息同时到达、两个回合同时收尾。先 `get_or_create` 再 `save` 的写法在并发下会
把同一个 `(chat_key, day)` 写成两行（唯一键会拒掉第二行，于是那一路的计数整条丢掉），
或者互相覆盖（后写的那次把前一次的增量抹掉）。所以用一条原子 `UPDATE ... SET x = x + n`：
影响行数为 0 说明当天还没有行，再 `CREATE`；`CREATE` 撞唯一键说明并发的另一路刚建好，
回头再 `UPDATE` 一次即可。两条路径都不会丢数。

## 任何异常都只记 warning

计数是**观测**，它的失败不该让一条消息处理失败、更不该让一个已经跑完的回合变成异常。
这与用量账本的同一条约束一致（见 `services/usage/ledger.py` 的模块文档）。
"""

from __future__ import annotations

import datetime
from typing import Dict, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tortoise.exceptions import IntegrityError
from tortoise.expressions import F

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily

logger = get_sub_logger("channel_quota")

FALLBACK_TIMEZONE = "Asia/Shanghai"

# 计入「回复主链路」的那些场景。平台方把 token 计入范围切成只算回复链路时，
# 读的是 `tokens_reply` 这一列，所以这里的集合决定了那一列的语义。
REPLY_SCENES: frozenset[str] = frozenset({"chat", "history_plan"})

_timezone_warned = False


def _resolve_timezone() -> ZoneInfo:
    """按配置取日界时区；名字非法时回落默认时区并记一次 warning

    只在第一次出错时打 warning：判定在每条消息上，时区名写错会让这条日志变成刷屏，
    而它想传达的信息只有一条——「你配的那个时区名不认识」。
    """
    global _timezone_warned  # noqa: PLW0603
    name = str(getattr(config, "CHANNEL_QUOTA_TIMEZONE", FALLBACK_TIMEZONE) or "").strip() or FALLBACK_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if not _timezone_warned:
            _timezone_warned = True
            logger.warning(f"频道额度时区名非法: {name!r}，回落 {FALLBACK_TIMEZONE}")
        return ZoneInfo(FALLBACK_TIMEZONE)


def resolve_timezone() -> ZoneInfo:
    """按配置取日界时区（对账与接口共用这一份，判据必须与计数侧完全一致）

    时区名非法时的回落规则只在这里写一次：读日界、算时间窗的两处若各写一遍，
    某天其中一处改了回落值，就会出现「按 A 时区记的账、按 B 时区去查」的错位。
    """
    return _resolve_timezone()


def reset_timezone_warning() -> None:
    """清掉「已经警告过」的标记（**仅供测试**）"""
    global _timezone_warned  # noqa: PLW0603
    _timezone_warned = False


def quota_day(at: Optional[datetime.datetime] = None) -> str:
    """按额度时区算出日期键 `YYYY-MM-DD`

    Args:
        at: 要换算的时刻；不带时区时按 UTC 理解（与库里的时间列一致）。
            缺省取此刻。
    """
    moment = at if at is not None else datetime.datetime.now(datetime.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(_resolve_timezone()).strftime("%Y-%m-%d")


async def _bump(chat_key: str, day: str, **increments: int) -> None:
    """对 `(chat_key, day)` 做一次原子累加；没有行就建

    `increments` 是「列名 → 增量」，增量必须为正。
    """
    updates = {name: F(name) + amount for name, amount in increments.items()}
    affected = await DBChannelQuotaDaily.filter(chat_key=chat_key, day=day).update(**updates)
    if affected:
        return
    try:
        await DBChannelQuotaDaily.create(chat_key=chat_key, day=day, **increments)
    except IntegrityError:
        # 并发的另一路刚把行建好：回头再做一次累加，增量不能丢
        await DBChannelQuotaDaily.filter(chat_key=chat_key, day=day).update(**updates)


async def record_tokens(
    chat_key: str,
    adapter_key: str,
    instance_key: str,
    scene: str,
    tokens: int,
) -> None:
    """把一次调用的 token 记到频道的当天计数上

    `scene` 属于回复主链路时同时累加 `tokens_reply`。两列都记的理由见
    `models/db_channel_quota.py` 的模块文档：平台方可以中途切换计入范围，
    两列都记就不用回算历史天。
    """
    if not chat_key or tokens <= 0:
        return
    try:
        day = quota_day()
        increments: Dict[str, int] = {"tokens_all": int(tokens)}
        if scene in REPLY_SCENES:
            increments["tokens_reply"] = int(tokens)
        await _bump(chat_key, day, **increments)
        # 实例归属按写入时的快照补上：只在该行还没有值时填，避免把历史行改写成
        # 「最后一次写入的实例」——换了会话标识之后旧账不该跟着改归属
        await DBChannelQuotaDaily.filter(chat_key=chat_key, day=day, adapter_key="").update(
            adapter_key=adapter_key or "",
            instance_key=instance_key or "",
        )
    except Exception as exc:  # noqa: BLE001 - 计数失败不能影响主链路
        logger.warning(f"频道额度 token 计数失败（已忽略）: chat_key={chat_key} scene={scene} | {exc}")


async def record_reply(chat_key: str, adapter_key: str, instance_key: str) -> None:
    """给频道的当天计数加一次回复（一个回合算一次，不是一条消息一次）"""
    if not chat_key:
        return
    try:
        day = quota_day()
        await _bump(chat_key, day, replies=1)
        await DBChannelQuotaDaily.filter(chat_key=chat_key, day=day, adapter_key="").update(
            adapter_key=adapter_key or "",
            instance_key=instance_key or "",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"频道额度回复计数失败（已忽略）: chat_key={chat_key} | {exc}")


async def add_bonus(chat_key: str, day: str, amount: int) -> None:
    """给某天的计数加一笔临时额度；没有行就建

    与 `record_tokens` 同一套原子写法，理由相同（并发加额不能互相覆盖）。
    """
    await _bump(chat_key, day, bonus=int(amount))


async def get_daily(chat_key: str, day: str) -> Optional[DBChannelQuotaDaily]:
    """读某天的计数行；**不建行**，没有就是 None"""
    return await DBChannelQuotaDaily.filter(chat_key=chat_key, day=day).order_by("id").first()


async def get_daily_bulk(chat_keys: Iterable[str], day: str) -> Dict[str, DBChannelQuotaDaily]:
    """一次读出一批频道当天的计数行；没有行的频道不出现在结果里"""
    keys = [key for key in chat_keys if key]
    if not keys:
        return {}
    rows = await DBChannelQuotaDaily.filter(chat_key__in=keys, day=day)
    result: Dict[str, DBChannelQuotaDaily] = {}
    for row in rows:
        result.setdefault(row.chat_key, row)
    return result


__all__ = [
    "FALLBACK_TIMEZONE",
    "REPLY_SCENES",
    "add_bonus",
    "get_daily",
    "get_daily_bulk",
    "quota_day",
    "record_reply",
    "record_tokens",
    "reset_timezone_warning",
    "resolve_timezone",
]
