"""频道每日额度：按账本重算、保留期与零点后自动对账

重算钉三件事：

- **只重算 token**：账本里没有「回合」这一维，回复次数只能以计数表为准；
- **只动两列**：`replies` / `bonus` / `alerted_level` / `pause_notice_sent_at` 记的是
  「这一天发生过什么」，重算用量不该把它们抹掉（否则当天已经发过的告警会再发一遍）；
- **日界按额度时区**：北京时间 23:59 与 00:01 的两条流水必须落进两天。

自动对账钉的是「昨天只算一次、失败不算数」：成功才记 `last_reconciled_day`，
失败的那一天下次醒来还要再试——对账是修复动作，不该等到明天。
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
from typing import List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_llm_usage import DBLLMUsageRecord
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.channel_quota import (
    recount_all_channel_quota,
    recount_channel_quota,
)
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota import reconcile as reconcile_module
from nekro_agent.services.channel_quota.policy import SCOPE_GLOBAL
from nekro_agent.services.channel_quota.reconcile import (
    ChannelQuotaReconciler,
    day_window,
    purge_old_daily,
    recount_tokens,
)
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE = "rec1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_410001"
OTHER_CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_410002"
# 固定的一天，避免「今天」在跨零点时被算到另一天
DAILY_DAY = "2026-05-01"


def platform_owner() -> DBUser:
    return DBUser(
        id=1,
        username="platform-owner",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def tenant_admin() -> DBUser:
    return DBUser(
        id=2,
        username="tenant-admin",
        password="",
        adapter_key="webui",
        platform_userid="1",
        perm_level=Role.Admin,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=1,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    policy_module.invalidate_policy_cache()
    counter.reset_timezone_warning()
    try:
        yield
    finally:
        policy_module.invalidate_policy_cache()
        counter.reset_timezone_warning()
        await Tortoise.close_connections()


class _LogRecorder:
    """抓产品代码真正打出去的日志（`get_sub_logger` 是 loguru，`caplog` 抓不到）"""

    def __init__(self) -> None:
        self.warnings: List[str] = []
        self.infos: List[str] = []

    def warning(self, message: object) -> None:
        self.warnings.append(str(message))

    def info(self, message: object) -> None:
        self.infos.append(str(message))

    def debug(self, message: object) -> None:
        return None


async def seed_channel(chat_key: str) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=chat_key.rsplit("-", 1)[-1],
        chat_key=chat_key,
        channel_type="group",
        data="{}",
    )


async def seed_global() -> None:
    await DBChannelQuotaPolicy.create(
        scope=SCOPE_GLOBAL,
        enabled=True,
        unit="tokens",
        group_daily_limit=1000,
        private_daily_limit=1000,
        over_limit_action="pause",
    )
    policy_module.invalidate_policy_cache()


async def add_usage(
    *,
    day: str,
    hour_utc: int,
    minute_utc: int = 0,
    chat_key: str,
    scene: str,
    token_total: int,
) -> DBLLMUsageRecord:
    """在额度时区下的 `day` 那一天、UTC 的某个整点造一条流水

    时间列按 UTC 理解（库里的时间列不带时区），所以这里直接写带时区的 UTC 时刻。
    """
    start, _ = day_window(day)
    return await DBLLMUsageRecord.create(
        chat_key=chat_key,
        scene=scene,
        token_total=token_total,
        token_input=token_total,
        token_output=0,
        create_time=start + datetime.timedelta(hours=hour_utc, minutes=minute_utc),
    )


async def daily_row(chat_key: str, day: str) -> DBChannelQuotaDaily | None:
    return await DBChannelQuotaDaily.filter(chat_key=chat_key, day=day).order_by("id").first()


# ---------------------------------------------------------------------------
# 时间窗
# ---------------------------------------------------------------------------


class TestDayWindow:
    def test_window_is_in_utc_and_covers_exactly_one_day(self) -> None:
        start, end = day_window("2026-05-01")

        assert start.tzinfo is not None
        assert start.utcoffset() == datetime.timedelta(0)
        assert end - start == datetime.timedelta(days=1)

    def test_beijing_midnight_maps_to_utc_1600(self) -> None:
        """北京时间零点 == 前一天 UTC 16:00，这一条错了整张表都会差一天"""
        start, _end = day_window("2026-05-01")

        assert start == datetime.datetime(2026, 4, 30, 16, 0, tzinfo=datetime.timezone.utc)

    def test_invalid_timezone_falls_back_the_same_way_as_the_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TIMEZONE", "Not/AZone")
        counter.reset_timezone_warning()

        assert day_window("2026-05-01") == (
            datetime.datetime(2026, 4, 30, 16, 0, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 5, 1, 16, 0, tzinfo=datetime.timezone.utc),
        )


# ---------------------------------------------------------------------------
# 重算
# ---------------------------------------------------------------------------


SEED_DAY = "2026-05-01"
NEXT_DAY = "2026-05-02"


async def seed_two_days() -> None:
    """两天各几行，覆盖全部场景与一条没有会话标识的流水"""
    await seed_channel(CHAT_KEY)
    await seed_channel(OTHER_CHAT_KEY)
    for day, base in ((SEED_DAY, 100), (NEXT_DAY, 200)):
        # 北京时间 08:00 之后都在 UTC 的同一格里，不会跨到前一天
        await add_usage(day=day, hour_utc=2, chat_key=CHAT_KEY, scene="chat", token_total=base)
        await add_usage(day=day, hour_utc=2, chat_key=CHAT_KEY, scene="history_plan", token_total=base + 1)
        await add_usage(day=day, hour_utc=2, chat_key=CHAT_KEY, scene="affection_eval", token_total=base + 2)
        await add_usage(day=day, hour_utc=2, chat_key=CHAT_KEY, scene="memory_consolidate", token_total=base + 3)
        await add_usage(day=day, hour_utc=2, chat_key=CHAT_KEY, scene="embedding", token_total=base + 4)
        await add_usage(day=day, hour_utc=2, chat_key=OTHER_CHAT_KEY, scene="chat", token_total=base + 5)
        # 没有会话标识的流水不属于任何频道：分组前就该被排除，不能建出一行空键的计数
        await add_usage(day=day, hour_utc=2, chat_key="", scene="command", token_total=base + 6)


class TestRecountTokens:
    async def test_recounts_only_the_requested_day(self, db) -> None:  # noqa: ARG002
        await seed_two_days()

        processed = await recount_tokens(SEED_DAY)

        assert processed == 2
        row = await daily_row(CHAT_KEY, SEED_DAY)
        assert row is not None
        # 100 + 101 + 102 + 103 + 104
        assert int(row.tokens_all) == 510
        # 只有 chat 与 history_plan 进 tokens_reply
        assert int(row.tokens_reply) == 201
        # 另一天没有流水被重算，行也不存在
        assert await daily_row(CHAT_KEY, NEXT_DAY) is None

    async def test_a_full_recount_zeroes_rows_without_ledger_entries(self, db) -> None:  # noqa: ARG002
        await seed_two_days()
        stale_key = f"{ADAPTER}-{INSTANCE}-group_410099"
        await DBChannelQuotaDaily.create(chat_key=stale_key, day=SEED_DAY, tokens_all=999, tokens_reply=500, replies=3, bonus=7)
        await DBChannelQuotaDaily.create(chat_key=stale_key, day=NEXT_DAY, tokens_all=11, tokens_reply=5)

        processed = await recount_tokens(SEED_DAY)

        assert processed == 3
        row = await daily_row(stale_key, SEED_DAY)
        assert row is not None
        assert (int(row.tokens_all), int(row.tokens_reply)) == (0, 0)
        # 回复次数与加量不是账本算得出来的，不动
        assert (int(row.replies), int(row.bonus)) == (3, 7)
        # 别的天不动
        other_day = await daily_row(stale_key, NEXT_DAY)
        assert other_day is not None
        assert int(other_day.tokens_all) == 11

    async def test_empty_chat_key_rows_are_not_a_channel(self, db) -> None:  # noqa: ARG002
        await seed_two_days()

        await recount_tokens(SEED_DAY)

        assert await DBChannelQuotaDaily.filter(chat_key="").count() == 0

    async def test_other_columns_are_untouched(self, db) -> None:  # noqa: ARG002
        await seed_two_days()
        existing = await DBChannelQuotaDaily.create(
            chat_key=CHAT_KEY,
            day=SEED_DAY,
            replies=7,
            bonus=42,
            alerted_level=80,
            pause_notice_sent_at=datetime.datetime.now(datetime.timezone.utc),
        )

        await recount_tokens(SEED_DAY)

        row = await daily_row(CHAT_KEY, SEED_DAY)
        assert row is not None
        assert int(row.id) == int(existing.id)
        assert int(row.replies) == 7
        assert int(row.bonus) == 42
        assert int(row.alerted_level) == 80
        assert row.pause_notice_sent_at is not None
        assert int(row.tokens_all) == 510

    async def test_missing_counter_row_is_created(self, db) -> None:  # noqa: ARG002
        await seed_two_days()

        await recount_tokens(NEXT_DAY)

        row = await daily_row(OTHER_CHAT_KEY, NEXT_DAY)
        assert row is not None
        assert int(row.tokens_all) == 205
        assert int(row.tokens_reply) == 205

    async def test_single_channel_without_usage_is_zeroed(self, db) -> None:  # noqa: ARG002
        """对账要修的正是「计数行有值、账本里没有对应流水」"""
        await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=SEED_DAY, tokens_all=999, tokens_reply=999)

        processed = await recount_tokens(SEED_DAY, CHAT_KEY)

        assert processed == 1
        row = await daily_row(CHAT_KEY, SEED_DAY)
        assert row is not None
        assert (int(row.tokens_all), int(row.tokens_reply)) == (0, 0)

    async def test_single_channel_without_row_is_not_created(self, db) -> None:  # noqa: ARG002
        await seed_channel(CHAT_KEY)

        processed = await recount_tokens(SEED_DAY, CHAT_KEY)

        assert processed == 0
        assert await daily_row(CHAT_KEY, SEED_DAY) is None

    async def test_single_channel_only_touches_that_channel(self, db) -> None:  # noqa: ARG002
        await seed_two_days()
        await DBChannelQuotaDaily.create(chat_key=OTHER_CHAT_KEY, day=SEED_DAY, tokens_all=1)

        await recount_tokens(SEED_DAY, CHAT_KEY)

        other = await daily_row(OTHER_CHAT_KEY, SEED_DAY)
        assert other is not None
        assert int(other.tokens_all) == 1

    async def test_timezone_boundary_splits_two_days(self, db) -> None:  # noqa: ARG002
        """北京时间 23:59 与次日 00:01 分属两天；UTC 下它们只差两分钟"""
        await seed_channel(CHAT_KEY)
        await add_usage(day=SEED_DAY, hour_utc=15, minute_utc=59, chat_key=CHAT_KEY, scene="chat", token_total=10)
        await add_usage(day=NEXT_DAY, hour_utc=16, minute_utc=1, chat_key=CHAT_KEY, scene="chat", token_total=20)

        await recount_tokens(SEED_DAY)
        await recount_tokens(NEXT_DAY)

        first = await daily_row(CHAT_KEY, SEED_DAY)
        second = await daily_row(CHAT_KEY, NEXT_DAY)
        assert first is not None and second is not None
        assert int(first.tokens_all) == 10
        assert int(second.tokens_all) == 20


# ---------------------------------------------------------------------------
# 保留期
# ---------------------------------------------------------------------------


class TestPurgeOldDaily:
    async def test_91_days_ago_goes_and_90_days_ago_stays(self, db) -> None:  # noqa: ARG002
        await seed_channel(CHAT_KEY)
        today = datetime.date.fromisoformat(counter.quota_day())
        old = (today - datetime.timedelta(days=91)).isoformat()
        edge = (today - datetime.timedelta(days=90)).isoformat()
        today_key = today.isoformat()
        for day in (old, edge, today_key):
            await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=day)

        removed = await purge_old_daily()

        assert removed == 1
        remaining = {row.day for row in await DBChannelQuotaDaily.all()}
        assert remaining == {edge, today_key}

    async def test_keep_days_is_adjustable(self, db) -> None:  # noqa: ARG002
        await seed_channel(CHAT_KEY)
        today = datetime.date.fromisoformat(counter.quota_day())
        for offset in (1, 3):
            await DBChannelQuotaDaily.create(
                chat_key=CHAT_KEY,
                day=(today - datetime.timedelta(days=offset)).isoformat(),
            )

        assert await purge_old_daily(keep_days=2) == 1


# ---------------------------------------------------------------------------
# 自动对账
# ---------------------------------------------------------------------------


class TestRunOnce:
    async def test_first_run_handles_yesterday_and_purges(self, db) -> None:  # noqa: ARG002
        await seed_global()
        await seed_channel(CHAT_KEY)
        today = datetime.date.fromisoformat(counter.quota_day())
        yesterday = (today - datetime.timedelta(days=1)).isoformat()
        stale = (today - datetime.timedelta(days=200)).isoformat()
        await add_usage(day=yesterday, hour_utc=2, chat_key=CHAT_KEY, scene="chat", token_total=30)
        await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=stale)

        reconciler = ChannelQuotaReconciler()
        assert await reconciler.run_once() is True

        row = await daily_row(CHAT_KEY, yesterday)
        assert row is not None
        assert int(row.tokens_all) == 30
        assert reconciler.last_reconciled_day == yesterday
        assert await DBChannelQuotaDaily.filter(day=stale).count() == 0

    async def test_same_day_does_not_repeat(self, db) -> None:  # noqa: ARG002
        await seed_global()
        await seed_channel(CHAT_KEY)
        reconciler = ChannelQuotaReconciler()

        assert await reconciler.run_once() is True
        assert await reconciler.run_once() is False

    async def test_failure_is_reported_and_retried_next_time(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_global()
        recorder = _LogRecorder()
        monkeypatch.setattr(reconcile_module, "logger", recorder)
        original = reconcile_module.recount_tokens

        async def boom(_day: str, _chat_key: str | None = None) -> int:
            raise RuntimeError("账本读不出来")

        monkeypatch.setattr(reconcile_module, "recount_tokens", boom)

        reconciler = ChannelQuotaReconciler()

        assert await reconciler.run_once() is False
        assert reconciler.last_reconciled_day is None
        assert any("自动对账失败" in message for message in recorder.warnings)

        # 修好之后不用等到明天：失败没记日期，下一次醒来就会重试
        monkeypatch.setattr(reconcile_module, "recount_tokens", original)

        assert await reconciler.run_once() is True
        assert reconciler.last_reconciled_day is not None

    async def test_disabled_feature_still_reconciles_and_purges(self, db) -> None:  # noqa: ARG002
        """没有全局策略行时照样对账、照样清理

        计数在功能关着时也照记，对账与清理不跟着照做的话，一个从没打开过这个功能的部署里
        计数表会每天每个频道多一行、只增不删。
        """
        await seed_channel(CHAT_KEY)
        today = datetime.date.fromisoformat(counter.quota_day())
        yesterday = (today - datetime.timedelta(days=1)).isoformat()
        stale = (today - datetime.timedelta(days=200)).isoformat()
        await add_usage(day=yesterday, hour_utc=2, chat_key=CHAT_KEY, scene="chat", token_total=12)
        await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=stale)

        reconciler = ChannelQuotaReconciler()

        assert await reconciler.run_once() is True
        row = await daily_row(CHAT_KEY, yesterday)
        assert row is not None and int(row.tokens_all) == 12
        assert await DBChannelQuotaDaily.filter(day=stale).count() == 0
        assert reconciler.last_reconciled_day == yesterday


class TestLoopStartStop:
    async def test_loop_reconciles_before_the_first_sleep(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """重启后要补对昨天，不能等第一个十分钟过去才算第一次"""
        await seed_global()
        calls: list[str] = []

        async def fake_run_once() -> bool:
            calls.append("tick")
            return True

        monkeypatch.setattr(reconcile_module.reconciler, "run_once", fake_run_once)

        await reconcile_module.reconciler.start()
        try:
            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            await reconcile_module.reconciler.stop()

        assert calls, "启动后应该先对一次再睡"
        assert reconcile_module.reconciler._task is None  # noqa: SLF001

    async def test_loop_keeps_going_after_a_failure(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """单次异常只记警告：循环一次都不许死"""
        await seed_global()
        recorder = _LogRecorder()
        monkeypatch.setattr(reconcile_module, "logger", recorder)
        calls: list[int] = []

        async def fake_run_once() -> bool:
            calls.append(1)
            raise RuntimeError("这一轮炸了")

        monkeypatch.setattr(reconcile_module.reconciler, "run_once", fake_run_once)
        monkeypatch.setattr(reconcile_module, "RECONCILE_TICK_SECONDS", 0.0)

        await reconcile_module.reconciler.start()
        try:
            for _ in range(200):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.01)
        finally:
            await reconcile_module.reconciler.stop()

        assert len(calls) >= 2, "异常之后必须继续下一轮"
        assert any("对账循环出错" in message for message in recorder.warnings)


# ---------------------------------------------------------------------------
# 接口：两条重算都只给平台方
# ---------------------------------------------------------------------------


class TestRecountEndpoints:
    async def test_platform_owner_recounts_one_channel(self, db) -> None:  # noqa: ARG002
        await seed_global()
        await seed_channel(CHAT_KEY)
        await add_usage(day=DAILY_DAY, hour_utc=2, chat_key=CHAT_KEY, scene="chat", token_total=70)
        await add_usage(day=DAILY_DAY, hour_utc=2, chat_key=CHAT_KEY, scene="embedding", token_total=30)

        item = await recount_channel_quota(chat_key=CHAT_KEY, day=DAILY_DAY, _current_user=platform_owner())

        assert item.chat_key == CHAT_KEY
        assert item.unit == "tokens"
        assert item.used == 100
        row = await daily_row(CHAT_KEY, DAILY_DAY)
        assert row is not None
        assert (int(row.tokens_all), int(row.tokens_reply)) == (100, 70)

    async def test_unknown_channel_is_404(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await recount_channel_quota(chat_key=CHAT_KEY, day=DAILY_DAY, _current_user=platform_owner())

    async def test_platform_owner_recounts_all_channels(self, db) -> None:  # noqa: ARG002
        await seed_global()
        await seed_channel(CHAT_KEY)
        await seed_channel(OTHER_CHAT_KEY)
        await add_usage(day=DAILY_DAY, hour_utc=2, chat_key=CHAT_KEY, scene="chat", token_total=10)
        await add_usage(day=DAILY_DAY, hour_utc=2, chat_key=OTHER_CHAT_KEY, scene="chat", token_total=20)

        response = await recount_all_channel_quota(day=DAILY_DAY, _current_user=platform_owner())

        assert response.day == DAILY_DAY
        assert response.channels == 2
        first = await daily_row(CHAT_KEY, DAILY_DAY)
        second = await daily_row(OTHER_CHAT_KEY, DAILY_DAY)
        assert first is not None and second is not None
        assert (int(first.tokens_all), int(second.tokens_all)) == (10, 20)

    async def test_tenant_admin_is_rejected(self) -> None:
        """两条重算接口都挂在 `require_platform_owner()` 上

        直接调端点函数不经过依赖注入，所以这里显式跑一遍那个依赖——断言的是**那道依赖
        真的拒绝租户**，而不是「函数体里恰好没写校验」。
        """
        from nekro_agent.services.user.tenant_perm import require_platform_owner

        dependency = require_platform_owner()
        with pytest.raises(PermissionDeniedError):
            await dependency(current_user=tenant_admin())

    async def test_both_endpoints_declare_the_platform_dependency(self) -> None:
        from nekro_agent.routers.console import channel_quota as module

        for endpoint in (module.recount_channel_quota, module.recount_all_channel_quota):
            source = inspect.getsource(inspect.unwrap(endpoint))
            assert "require_platform_owner()" in source, endpoint.__name__

    async def test_bad_day_is_a_client_error_not_a_server_error(self, db) -> None:  # noqa: ARG002
        """日期键写错是调用方的问题：冒成 500 会让人以为对账本身坏了

        非定宽的写法也拒绝：`purge_old_daily` 靠定宽字符串比大小，放进一个 `2026-5-1`
        就等于给这张表留了一个排不进保留期的键。
        """
        await seed_channel(CHAT_KEY)

        with pytest.raises(ValidationError):
            await recount_channel_quota(chat_key=CHAT_KEY, day="昨天", _current_user=platform_owner())
        with pytest.raises(ValidationError):
            await recount_all_channel_quota(day="2026-13-01", _current_user=platform_owner())
        with pytest.raises(ValidationError):
            await recount_all_channel_quota(day="2026-5-1", _current_user=platform_owner())

    async def test_padded_day_is_echoed_back(self, db) -> None:  # noqa: ARG002
        """回执里的 `day` 必须是后端认的那一天：前端拿它去提示「重算了哪一天」"""
        await seed_global()

        response = await recount_all_channel_quota(day=" 2026-05-01 ", _current_user=platform_owner())

        assert response.day == DAILY_DAY
