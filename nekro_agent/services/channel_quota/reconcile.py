"""频道每日额度的对账：按账本重算计数、清理过期行、零点后自动对昨天

## 计数表是缓存，账本才是事实

按天的计数行是账本的可重算缓存（见 `models/db_channel_quota.py` 的模块文档）。累加走的是
后台任务，进程在队列里还有流水时被杀、数据库瞬时不可用，都会让某一天少记一点，
而少记的那部分会直接变成「平台方承诺的额度被多用了几天」。对账就是拿账本重新盖一遍。

**回复次数不重算**：账本里一行的粒度是「一次上游请求」，没有「回合」这一维，
无法从流水反推出「这一天这几个频道各有几个回合成功发出了消息」。所以回复次数只能以
计数表为准，重算只覆盖两列 token。

## 重算今天有一个很小的竞态

聚合在 `T1` 做完、写入在 `T2` 完成，`T1` 到 `T2` 之间落库的流水会被这次写入覆盖掉——
它们不在聚合结果里，而写入是「直接设成聚合值」而不是加增量。窗口是毫秒级，但它是真的。
因此平台方手动重算用于**对账**（人看着数字、要求的是「此刻账本里到底是多少」），
零点后的自动对账只算**昨天**：昨天不会再有新流水进来，那个窗口天然不存在。
如果哪天需要「一边跑一边重算今天」，得改成「聚合 + 只补差额」的写法。

## 保留 90 天

`day` 是 `YYYY-MM-DD` 定宽零填充字符串，字典序与日期序一致，所以「早于某天」可以直接
用字符串比较，不需要在 SQL 里做日期换算（换算还得依赖数据库自己的时区设置）。
更早的天数随时可以按账本现算，留着只会让这张表无限增长。
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from tortoise.exceptions import IntegrityError
from tortoise.functions import Sum

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily
from nekro_agent.models.db_llm_usage import DBLLMUsageRecord

from . import counter

logger = get_sub_logger("channel_quota")

# 保留天数：更早的计数行从账本现算
DEFAULT_KEEP_DAYS = 90
# 自动对账循环的醒来间隔（秒）。每十分钟看一眼「昨天对过没有」，对过就什么都不做
RECONCILE_TICK_SECONDS = 600.0


def normalize_day(day: str) -> str:
    """把日期键规范成定宽的 `YYYY-MM-DD`；格式不对抛 `ValueError`

    定宽零填充是 `purge_old_daily` 拿字符串比大小的前提：`2026-5-1` 与 `2026-05-01`
    字面上不是同一天，混进表里会让「早于某天」的比较算错。另外 `fromisoformat` 也吃
    `20260501` 这类写法，不统一过一遍，同一个日子会以两种键各存一行。
    """
    try:
        parsed = datetime.date.fromisoformat(str(day).strip())
    except ValueError as exc:
        raise ValueError(f"日期键必须是 YYYY-MM-DD: {day!r}") from exc
    return parsed.isoformat()


def quota_timezone() -> ZoneInfo:
    """额度时区；名字非法时回落默认值（判据与计数侧同一份实现）"""
    return counter.resolve_timezone()


def day_window(day: str) -> Tuple[datetime.datetime, datetime.datetime]:
    """把额度时区下的日期键换算成 UTC 的 `[start, end)`

    时间窗必须落在 UTC 上：账本的 `create_time` 是 UTC，拿一个本地时区的边界去比会
    整体偏一个时区差，那正好是「日界附近几条流水算错天」的形态。
    """
    tz = quota_timezone()
    start_local = datetime.datetime.fromisoformat(normalize_day(day)).replace(tzinfo=tz)
    end_local = start_local + datetime.timedelta(days=1)
    return start_local.astimezone(datetime.timezone.utc), end_local.astimezone(datetime.timezone.utc)


async def _token_sums(
    day: str,
    chat_key: Optional[str],
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """按频道聚合这一天账本里的 token：全部场景一份、回复链路一份

    没有 `chat_key` 的流水不属于任何频道，两边都不计（分组前直接排除，免得把空键
    当成一个频道建出计数行）。
    """
    start_time, end_time = day_window(day)

    def base_query():
        query = DBLLMUsageRecord.filter(create_time__gte=start_time, create_time__lt=end_time)
        if chat_key:
            return query.filter(chat_key=chat_key)
        return query.exclude(chat_key="")

    totals_all: Dict[str, int] = {}
    for row in await (
        base_query().annotate(agg_token_total=Sum("token_total")).group_by("chat_key").values("chat_key", "agg_token_total")
    ):
        key = str(row.get("chat_key") or "")
        if key:
            totals_all[key] = int(row.get("agg_token_total") or 0)

    totals_reply: Dict[str, int] = {}
    for row in await (
        base_query()
        .filter(scene__in=list(counter.REPLY_SCENES))
        .annotate(agg_token_total=Sum("token_total"))
        .group_by("chat_key")
        .values("chat_key", "agg_token_total")
    ):
        key = str(row.get("chat_key") or "")
        if key:
            totals_reply[key] = int(row.get("agg_token_total") or 0)

    return totals_all, totals_reply


async def _set_tokens(chat_key: str, day: str, tokens_all: int, tokens_reply: int, *, create_missing: bool) -> bool:
    """把两列 token **直接设成**给定值；返回是否真的动了行

    只写这两列：`replies` 是账本算不出来的（见模块文档），`bonus` 是平台方手动给的，
    `alerted_level` 与 `pause_notice_sent_at` 记的是「今天已经做过什么」，重算用量
    不该把这几件事一起抹掉——那会让当天已经发过的告警再发一遍、暂停提示再发一次。
    """
    fields = {"tokens_all": int(tokens_all), "tokens_reply": int(tokens_reply)}
    affected = await DBChannelQuotaDaily.filter(chat_key=chat_key, day=day).update(**fields)
    if affected:
        return True
    if not create_missing:
        return False

    try:
        await DBChannelQuotaDaily.create(chat_key=chat_key, day=day, **fields)
    except IntegrityError:
        # 并发的另一路刚把行建好：回头再设一次，值以这次聚合为准
        await DBChannelQuotaDaily.filter(chat_key=chat_key, day=day).update(**fields)
    return True


async def recount_tokens(day: str, chat_key: Optional[str] = None) -> int:
    """按账本重算某一天两列 token，返回处理过的频道数

    Args:
        day: 额度时区下的日期键 `YYYY-MM-DD`
        chat_key: 只重算这一个频道；为空时重算这一天所有有流水的频道，以及这一天已有计数行的频道

    回复次数不重算（理由见模块文档）。重算**今天**时与实时累加有一个很小的竞态：
    聚合之后、写入之前落进来的流水会被覆盖掉，所以平台方手动重算用于对账，
    零点后的自动对账只算昨天。

    传了 `chat_key` 而这一天没有任何流水时，把两列设成 0——那正是对账要修的情形
    （计数行里有值、账本里没有对应的流水）。行不存在就不建：不为一个「确认是 0」的
    频道凭空造一行。
    """
    # 一次规范化，后面查询、更新、建行用的都是同一个键：`2026-5-1` 与 `2026-05-01`
    # 必须以同一行结算，否则重算会绕过既有行另建一行，两行各说一个数。
    day = normalize_day(day)
    totals_all, totals_reply = await _token_sums(day, chat_key)

    if chat_key:
        if chat_key in totals_all or chat_key in totals_reply:
            changed = await _set_tokens(
                chat_key,
                day,
                totals_all.get(chat_key, 0),
                totals_reply.get(chat_key, 0),
                create_missing=True,
            )
            return 1 if changed else 0
        changed = await _set_tokens(chat_key, day, 0, 0, create_missing=False)
        return 1 if changed else 0

    processed = 0
    for key in sorted(totals_all):
        changed = await _set_tokens(
            key,
            day,
            totals_all.get(key, 0),
            totals_reply.get(key, 0),
            create_missing=True,
        )
        if changed:
            processed += 1

    # 当天已有计数行、账本里却一条流水都没有的频道也要按账本清成 0，与单频道重算同一个口径；
    # 只改两列 token，回复次数、加量、告警档位与暂停提示时刻都不动，也不新建行
    existing = await DBChannelQuotaDaily.filter(day=day).values_list("chat_key", flat=True)
    for key in sorted({str(item) for item in existing} - set(totals_all) - set(totals_reply)):
        if await _set_tokens(key, day, 0, 0, create_missing=False):
            processed += 1
    return processed


async def purge_old_daily(keep_days: int = DEFAULT_KEEP_DAYS) -> int:
    """删掉早于「今天 - keep_days」的计数行，返回删除行数

    `day` 是定宽零填充的 `YYYY-MM-DD`，字典序与日期序一致，所以这里直接做字符串比较：
    交给数据库按日期解析反而要依赖它自己的时区与格式设置，同一个部署换一个库就可能
    删错一天。今天与保留期边界那一天都留着。
    """
    today = counter.quota_day()
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=max(0, int(keep_days)))).isoformat()
    deleted = await DBChannelQuotaDaily.filter(day__lt=cutoff).delete()
    if deleted:
        logger.info(f"频道额度计数表已清理过期行: cutoff={cutoff}, removed={deleted}")
    return int(deleted)


class ChannelQuotaReconciler:
    """零点后自动对账的后台循环

    进程内记一个「上次对过哪一天」：重启之后是空的，所以启动后第一次醒来会补对昨天
    （重算是幂等的，多对一次无害）；对过之后同一天再醒就什么都不做。失败不记日期，
    下一次醒来重试——对账是修复动作，失败一次不该等到明天。
    """

    def __init__(self) -> None:
        self._task: Optional["asyncio.Task[None]"] = None
        self._running = False
        self._last_reconciled_day: Optional[str] = None

    @property
    def last_reconciled_day(self) -> Optional[str]:
        return self._last_reconciled_day

    def reset(self) -> None:
        """忘掉「对过哪一天」（**仅供测试**）"""
        self._last_reconciled_day = None

    async def run_once(self) -> bool:
        """对一次昨天；真的做了返回 True

        **不看开关**：计数在功能关着时也照记（后台要能显示用量，平台方打开功能前也要能先看
        各群平时用多少），对账与 90 天清理也就必须照做——否则一个从没打开过这个功能的部署，
        计数表会每天每个频道多一行、只增不删。对账与清理只动这张计数表，不碰任何消息路径，
        功能关着时「消息照常回复」这一条不受影响。
        """
        today = counter.quota_day()
        yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
        if self._last_reconciled_day == yesterday:
            return False

        try:
            channels = await recount_tokens(yesterday)
            removed = await purge_old_daily()
        except Exception as exc:  # noqa: BLE001 - 对账失败不能打掉后台循环
            logger.warning(f"频道额度自动对账失败（下次再试）: day={yesterday} | {exc}")
            return False

        self._last_reconciled_day = yesterday
        logger.info(f"频道额度自动对账完成: day={yesterday}, channels={channels}, purged={removed}")
        return True

    # ==== 后台循环 ====

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        # 先对一次再睡：重启后要补对昨天，不能等到第一个十分钟过去
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"频道额度对账循环出错（已忽略）: {exc}")
            await asyncio.sleep(RECONCILE_TICK_SECONDS)


reconciler = ChannelQuotaReconciler()


async def run_once() -> bool:
    """对一次昨天（用例直接调这个，生产侧由后台循环调）"""
    return await reconciler.run_once()


__all__ = [
    "DEFAULT_KEEP_DAYS",
    "RECONCILE_TICK_SECONDS",
    "ChannelQuotaReconciler",
    "day_window",
    "normalize_day",
    "purge_old_daily",
    "quota_timezone",
    "reconciler",
    "recount_tokens",
    "run_once",
]
