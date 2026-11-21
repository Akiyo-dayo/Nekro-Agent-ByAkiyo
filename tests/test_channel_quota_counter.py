"""频道每日额度：按天计数

这一族钉的是「计数不丢、不重复、不因为并发互相覆盖」以及「时区日界」：

- 有则加、无则建：两条消息同时到达时只该有一行，两笔增量都要在；
- 读不建行：判定在每条消息的路径上，为了读一个 0 而写库会把只读路径变成写路径；
- token 两列同时记：平台方切换计入范围时只换一列读，不回算历史天；
- 时区不是 UTC：北京时间 23:59 与 00:01 分属两天。
"""

import asyncio
import datetime
from typing import List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.channel_quota.counter import (
    FALLBACK_TIMEZONE,
    add_bonus,
    get_daily,
    get_daily_bulk,
    quota_day,
    record_reply,
    record_tokens,
)

ADAPTER = "onebot_v11"
INSTANCE = "count1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_800001"
OTHER_CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_800002"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    counter.reset_timezone_warning()
    try:
        yield
    finally:
        counter.reset_timezone_warning()
        await Tortoise.close_connections()


class _LogRecorder:
    """抓产品代码真正打出去的日志

    **不能用 `caplog`**：`get_sub_logger()` 回的是 loguru 的 logger，它不进 stdlib logging，
    `caplog.records` 恒为空——对着抓不到的东西断言等于没断言。
    """

    def __init__(self) -> None:
        self.warnings: List[str] = []

    def warning(self, message: object) -> None:
        self.warnings.append(str(message))

    def info(self, message: object) -> None:
        return None

    def debug(self, message: object) -> None:
        return None


@pytest.fixture()
def log_recorder(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(counter, "logger", recorder)
    return recorder


class TestQuotaDay:
    """日界由配置的时区决定，而不是 UTC"""

    def test_beijing_late_night_and_just_after_midnight_are_different_days(self) -> None:
        """23:59 与 00:01 在北京时间分属两天，在 UTC 里却是同一天的前半段"""
        late = datetime.datetime(2026, 5, 1, 23, 59, tzinfo=datetime.timezone.utc)
        # 北京 23:59 == UTC 15:59；北京次日 00:01 == UTC 16:01
        beijing_late = datetime.datetime(2026, 5, 1, 15, 59, tzinfo=datetime.timezone.utc)
        beijing_early = datetime.datetime(2026, 5, 1, 16, 1, tzinfo=datetime.timezone.utc)

        assert quota_day(beijing_late) == "2026-05-01"
        assert quota_day(beijing_early) == "2026-05-02"
        # 同一个时刻在 UTC 下是同一天，说明这条断言测的确实是时区而不是日期算术
        assert late.strftime("%Y-%m-%d") == beijing_early.strftime("%Y-%m-%d")

    def test_naive_timestamps_are_read_as_utc(self) -> None:
        """库里的时间列不带时区，按 UTC 理解才与写入侧一致"""
        naive = datetime.datetime(2026, 5, 1, 16, 1)
        assert quota_day(naive) == "2026-05-02"

    def test_configured_timezone_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TIMEZONE", "UTC")
        moment = datetime.datetime(2026, 5, 1, 16, 1, tzinfo=datetime.timezone.utc)

        assert quota_day(moment) == "2026-05-01"

    def test_invalid_timezone_falls_back_and_warns_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        log_recorder: _LogRecorder,
    ) -> None:
        """名字写错时回落默认时区；只在第一次打 warning，否则每条消息刷一行日志"""
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TIMEZONE", "Not/AZone")
        counter.reset_timezone_warning()
        moment = datetime.datetime(2026, 5, 1, 16, 1, tzinfo=datetime.timezone.utc)

        first = quota_day(moment)
        second = quota_day(moment)

        assert first == second
        assert quota_day(datetime.datetime(2026, 5, 1, 15, 0, tzinfo=datetime.timezone.utc)) == "2026-05-01"
        assert sum("时区名非法" in message for message in log_recorder.warnings) == 1

    def test_fallback_timezone_is_a_real_zone(self) -> None:
        assert quota_day(datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)) == "2026-05-01"
        assert FALLBACK_TIMEZONE == "Asia/Shanghai"


class TestTokenCounting:
    """token 计数：两列都记，回复场景才进 `tokens_reply`"""

    async def test_accumulates_within_the_same_day(self, db) -> None:  # noqa: ARG002
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "chat", 100)
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "chat", 50)

        row = await get_daily(CHAT_KEY, quota_day())
        assert row is not None
        assert row.tokens_all == 150
        assert row.tokens_reply == 150

    @pytest.mark.parametrize("scene", ["chat", "history_plan"])
    async def test_reply_scenes_fill_the_reply_column(self, db, scene: str) -> None:  # noqa: ARG002
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, scene, 40)

        row = await get_daily(CHAT_KEY, quota_day())
        assert row is not None
        assert (row.tokens_all, row.tokens_reply) == (40, 40)

    @pytest.mark.parametrize(
        "scene",
        ["embedding", "affection_eval", "memory_consolidate", "vision_describe", "plugin", "command", "unattributed"],
    )
    async def test_other_scenes_only_fill_the_all_column(self, db, scene: str) -> None:  # noqa: ARG002
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, scene, 40)

        row = await get_daily(CHAT_KEY, quota_day())
        assert row is not None
        assert (row.tokens_all, row.tokens_reply) == (40, 0)

    async def test_mixed_scenes_keep_the_two_columns_separate(self, db) -> None:  # noqa: ARG002
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "chat", 100)
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "embedding", 25)

        row = await get_daily(CHAT_KEY, quota_day())
        assert row is not None
        assert (row.tokens_all, row.tokens_reply) == (125, 100)

    async def test_zero_or_negative_tokens_write_nothing(self, db) -> None:  # noqa: ARG002
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "chat", 0)
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "chat", -5)

        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_empty_chat_key_writes_nothing(self, db) -> None:  # noqa: ARG002
        await record_tokens("", ADAPTER, INSTANCE, "chat", 100)

        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_instance_ownership_is_filled_on_first_write_only(self, db) -> None:  # noqa: ARG002
        """先写到哪个实例就记哪个；后来的写入不会改写已有归属"""
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "chat", 10)
        await record_tokens(CHAT_KEY, ADAPTER, "another", "chat", 10)

        row = await get_daily(CHAT_KEY, quota_day())
        assert row is not None
        assert row.instance_key == INSTANCE

    async def test_concurrent_first_writes_do_not_lose_tokens(self, db) -> None:  # noqa: ARG002
        """两条调用同时落到一个还没有行的 `(chat_key, day)` 上

        「先读再写」的写法在这里会丢一笔：两边都读到空、都去 create，
        第二笔撞唯一键之后整条丢掉。
        """
        await asyncio.gather(
            *[record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "chat", 10) for _ in range(8)],
        )

        rows = await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY, day=quota_day())
        assert len(list(rows)) == 1
        assert rows[0].tokens_all == 80
        assert rows[0].tokens_reply == 80


class TestReplyCounting:
    """回复次数：一个回合一次"""

    async def test_accumulates(self, db) -> None:  # noqa: ARG002
        await record_reply(CHAT_KEY, ADAPTER, INSTANCE)
        await record_reply(CHAT_KEY, ADAPTER, INSTANCE)

        row = await get_daily(CHAT_KEY, quota_day())
        assert row is not None
        assert row.replies == 2

    async def test_concurrent_first_writes_do_not_lose_replies(self, db) -> None:  # noqa: ARG002
        await asyncio.gather(*[record_reply(CHAT_KEY, ADAPTER, INSTANCE) for _ in range(8)])

        rows = list(await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY, day=quota_day()))
        assert len(rows) == 1
        assert rows[0].replies == 8

    async def test_empty_chat_key_writes_nothing(self, db) -> None:  # noqa: ARG002
        await record_reply("", ADAPTER, INSTANCE)

        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_replies_and_tokens_share_the_same_row(self, db) -> None:  # noqa: ARG002
        await record_reply(CHAT_KEY, ADAPTER, INSTANCE)
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "chat", 77)

        rows = list(await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY))
        assert len(rows) == 1
        assert (rows[0].replies, rows[0].tokens_all) == (1, 77)


class TestReads:
    """读：不建行，缺行按 0 算"""

    async def test_get_daily_does_not_create_a_row(self, db) -> None:  # noqa: ARG002
        assert await get_daily(CHAT_KEY, "2026-05-01") is None
        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_get_daily_bulk_skips_missing_channels(self, db) -> None:  # noqa: ARG002
        await record_reply(CHAT_KEY, ADAPTER, INSTANCE)

        rows = await get_daily_bulk([CHAT_KEY, OTHER_CHAT_KEY], quota_day())

        assert set(rows) == {CHAT_KEY}

    async def test_get_daily_bulk_with_no_keys_issues_no_query(self, db) -> None:  # noqa: ARG002
        assert await get_daily_bulk([], quota_day()) == {}
        assert await get_daily_bulk(["", ""], quota_day()) == {}

    async def test_reads_are_scoped_to_the_day(self, db) -> None:  # noqa: ARG002
        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "chat", 10)

        assert await get_daily(CHAT_KEY, "1999-01-01") is None


class TestBonus:
    """临时加额：原子加，没有行就建"""

    async def test_add_bonus_creates_and_accumulates(self, db) -> None:  # noqa: ARG002
        await add_bonus(CHAT_KEY, "2026-05-01", 100)
        await add_bonus(CHAT_KEY, "2026-05-01", 50)

        row = await get_daily(CHAT_KEY, "2026-05-01")
        assert row is not None
        assert row.bonus == 150
        # 加额不该顺带把回复数与 token 也建出来
        assert (row.replies, row.tokens_all) == (0, 0)

    async def test_concurrent_bonus_does_not_overwrite(self, db) -> None:  # noqa: ARG002
        await asyncio.gather(*[add_bonus(CHAT_KEY, "2026-05-01", 10) for _ in range(5)])

        row = await get_daily(CHAT_KEY, "2026-05-01")
        assert row is not None
        assert row.bonus == 50


class TestFailuresAreSwallowed:
    """计数是观测：任何异常都只记 warning，绝不往上抛"""

    async def test_token_failure_is_logged_not_raised(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        log_recorder: _LogRecorder,
    ) -> None:  # noqa: ARG002
        async def boom(*_args, **_kwargs):
            raise RuntimeError("写不进去")

        monkeypatch.setattr(DBChannelQuotaDaily, "filter", classmethod(lambda cls, **_kwargs: boom()))

        await record_tokens(CHAT_KEY, ADAPTER, INSTANCE, "chat", 10)

        assert any("token 计数失败" in message for message in log_recorder.warnings)

    async def test_reply_failure_is_logged_not_raised(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        log_recorder: _LogRecorder,
    ) -> None:  # noqa: ARG002
        async def boom(*_args, **_kwargs):
            raise RuntimeError("写不进去")

        monkeypatch.setattr(DBChannelQuotaDaily, "filter", classmethod(lambda cls, **_kwargs: boom()))

        await record_reply(CHAT_KEY, ADAPTER, INSTANCE)

        assert any("回复计数失败" in message for message in log_recorder.warnings)
