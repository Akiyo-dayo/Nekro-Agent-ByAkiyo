"""频道每日额度：状态机与判定

状态是**算出来的**，不落库：所以「第二天恢复」「加额立刻恢复」都不需要任何解除动作。
这一族钉住状态之间的边界，以及 `is_paused` 的 fail-open（任何不确定都放行）。
"""

import datetime

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota import service as service_module
from nekro_agent.services.channel_quota.policy import SCOPE_GLOBAL, EffectivePolicy
from nekro_agent.services.channel_quota.service import (
    STATE_DISABLED,
    STATE_EXCEEDED,
    STATE_NORMAL,
    STATE_PAUSED,
    STATE_UNLIMITED,
    STATE_WARNING,
    build_status,
    evaluate,
    is_paused,
)

ADAPTER = "onebot_v11"
INSTANCE = "svc1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_900001"


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
        await Tortoise.close_connections()


def make_policy(
    *,
    enabled: bool = True,
    unit: str = "replies",
    limit: int = 10,
    action: str = "alert",
    is_group: bool = True,
) -> EffectivePolicy:
    return EffectivePolicy(
        enabled=enabled,
        unit=unit,
        is_group=is_group,
        platform_limit=limit,
        tenant_limit=None,
        effective_limit=limit,
        over_limit_action=action,
        pause_message="额度用完了",
    )


async def make_daily(**fields) -> DBChannelQuotaDaily:
    defaults = {"chat_key": CHAT_KEY, "day": counter.quota_day(), "replies": 0}
    defaults.update(fields)
    return await DBChannelQuotaDaily.create(**defaults)


async def seed_channel(chat_key: str = CHAT_KEY, channel_type: str = "group") -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_900001",
        chat_key=chat_key,
        channel_type=channel_type,
        data="{}",
    )


async def seed_global(**fields) -> None:
    defaults = {
        "enabled": True,
        "unit": "replies",
        "group_daily_limit": 10,
        "private_daily_limit": 5,
        "over_limit_action": "pause",
    }
    defaults.update(fields)
    await DBChannelQuotaPolicy.create(scope=SCOPE_GLOBAL, **defaults)
    policy_module.invalidate_policy_cache()


class TestStateMachine:
    """`build_status` 是纯函数：状态边界在这里钉死，接口与判定都从它来"""

    def test_disabled_wins_over_everything(self) -> None:
        status = build_status(make_policy(enabled=False, limit=10), None)

        assert status.state == STATE_DISABLED
        assert status.percent == 0

    def test_zero_limit_is_unlimited_not_exhausted(self) -> None:
        """额度 0 是「不限」，不是「一个都不许」——反过来的话打开功能就全停了"""
        status = build_status(make_policy(limit=0), None)

        assert status.state == STATE_UNLIMITED
        assert status.allowed == 0
        assert status.percent == 0

    def test_under_the_lowest_threshold_is_normal(self) -> None:
        status = build_status(make_policy(limit=100), _daily_with(replies=10))

        assert status.state == STATE_NORMAL
        assert status.percent == 10

    def test_at_the_lowest_threshold_is_warning(self) -> None:
        status = build_status(make_policy(limit=100), _daily_with(replies=80))

        assert status.state == STATE_WARNING
        assert status.percent == 80

    def test_at_the_limit_alert_mode_is_exceeded(self) -> None:
        status = build_status(make_policy(limit=100, action="alert"), _daily_with(replies=100))

        assert status.state == STATE_EXCEEDED

    def test_at_the_limit_pause_mode_is_paused(self) -> None:
        status = build_status(make_policy(limit=100, action="pause"), _daily_with(replies=100))

        assert status.state == STATE_PAUSED

    def test_over_the_limit_stays_in_the_same_state(self) -> None:
        """一个回合跑完才记账，所以用量会略超额度：超额不能变成另一个状态"""
        status = build_status(make_policy(limit=100, action="pause"), _daily_with(replies=130))

        assert status.state == STATE_PAUSED
        assert status.percent == 130

    def test_bonus_extends_the_allowance(self) -> None:
        """加额之后 130 落在 150 以内：从「暂停」退回去，且不再算超额"""
        status = build_status(make_policy(limit=100, action="pause"), _daily_with(replies=130, bonus=50))

        assert status.allowed == 150
        assert status.state == STATE_WARNING
        assert status.percent == 86

    def test_missing_daily_row_means_zero_usage(self) -> None:
        status = build_status(make_policy(limit=10), None)

        assert status.used == 0
        assert status.state == STATE_NORMAL

    def test_token_unit_reads_the_all_column_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "all")

        status = build_status(
            make_policy(unit="tokens", limit=1000),
            _daily_with(tokens_all=600, tokens_reply=100),
        )

        assert status.used == 600
        assert status.percent == 60

    def test_reply_only_scope_reads_the_reply_column(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "reply_only")

        status = build_status(
            make_policy(unit="tokens", limit=1000),
            _daily_with(tokens_all=600, tokens_reply=100),
        )

        assert status.used == 100
        assert status.percent == 10


def _daily_with(**fields) -> DBChannelQuotaDaily:
    """造一个未落库的计数行：`build_status` 只读字段"""
    defaults = {"chat_key": CHAT_KEY, "day": "2026-05-01", "replies": 0, "tokens_all": 0, "tokens_reply": 0, "bonus": 0}
    defaults.update(fields)
    return DBChannelQuotaDaily(**defaults)


class TestEvaluate:
    """`evaluate` 把策略与计数拼起来；功能关着时不读计数表"""

    async def test_disabled_policy_does_not_read_the_counter(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """功能关着时不该因为判定去查一次计数表：那是每条消息的路径"""
        channel = await seed_channel()
        calls: list[str] = []

        async def spy(chat_key: str, day: str):
            calls.append(chat_key)
            return None

        monkeypatch.setattr(service_module, "get_daily", spy)

        status = await service_module.evaluate(channel)

        assert status.state == STATE_DISABLED
        assert calls == []

    async def test_enabled_policy_reads_the_counter(self, db) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global(group_daily_limit=10)
        await make_daily(replies=4)

        status = await evaluate(channel)

        assert status.used == 4
        assert status.state == STATE_NORMAL

    async def test_group_channel_uses_the_group_limit(self, db) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global(group_daily_limit=10, private_daily_limit=3)

        assert (await evaluate(channel)).allowed == 10

    async def test_private_channel_uses_the_private_limit(self, db) -> None:  # noqa: ARG002
        channel = await seed_channel(chat_key=f"{ADAPTER}-{INSTANCE}-private_900001", channel_type="private")
        await seed_global(group_daily_limit=10, private_daily_limit=3)

        assert (await evaluate(channel)).allowed == 3


class TestIsPausedGate:
    """`is_paused`：任何不确定都必须放行，否则一次查库抖动会让所有频道停止回复"""

    async def test_missing_channel_is_not_paused(self, db) -> None:  # noqa: ARG002
        assert await is_paused("no-such-chat") is False

    async def test_no_policy_is_not_paused(self, db) -> None:  # noqa: ARG002
        await seed_channel()

        assert await is_paused(CHAT_KEY) is False

    async def test_alert_action_is_never_paused(self, db) -> None:  # noqa: ARG002
        await seed_channel()
        await seed_global(over_limit_action="alert", group_daily_limit=10)
        await make_daily(replies=99)

        assert await is_paused(CHAT_KEY) is False

    async def test_pause_action_over_the_limit_is_paused(self, db) -> None:  # noqa: ARG002
        await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=10)
        await make_daily(replies=10)

        assert await is_paused(CHAT_KEY) is True

    async def test_bonus_lifts_the_pause_immediately(self, db) -> None:  # noqa: ARG002
        """暂停是算出来的：加额之后一个字都没改，判定自己就恢复了"""
        await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=10)
        await make_daily(replies=10)
        assert await is_paused(CHAT_KEY) is True

        await counter.add_bonus(CHAT_KEY, counter.quota_day(), 5)

        assert await is_paused(CHAT_KEY) is False

    async def test_next_day_is_not_paused(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """换天靠的是日期键，不是任何定时任务：昨天用尽，今天自然恢复"""
        await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=10)
        await make_daily(replies=10, day="2026-05-01")
        monkeypatch.setattr(counter, "quota_day", lambda at=None: "2026-05-02")

        assert await is_paused(CHAT_KEY) is False

    async def test_evaluation_failure_is_treated_as_not_paused(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await seed_channel()

        async def boom(*_args, **_kwargs):
            raise RuntimeError("读策略炸了")

        monkeypatch.setattr(service_module, "evaluate", boom)

        assert await is_paused(CHAT_KEY) is False

    async def test_unlimited_quota_is_never_paused(self, db) -> None:  # noqa: ARG002
        await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=0)
        await make_daily(replies=9999)

        assert await is_paused(CHAT_KEY) is False


class TestThresholdConfiguration:
    """告警阈值来自配置；非法值忽略，不影响判定"""

    def test_bad_threshold_values_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "CHANNEL_QUOTA_ALERT_PERCENTS", [80, "oops", None, 0, -3, 80, 100])

        assert policy_module.alert_percents() == [80, 100]

    def test_state_uses_the_lowest_threshold_as_the_warning_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "CHANNEL_QUOTA_ALERT_PERCENTS", [50, 90])

        status = build_status(make_policy(limit=100), _daily_with(replies=60))

        assert status.state == STATE_WARNING

    def test_empty_threshold_list_falls_back_to_the_limit_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "CHANNEL_QUOTA_ALERT_PERCENTS", [])

        assert build_status(make_policy(limit=100), _daily_with(replies=60)).state == STATE_NORMAL
        assert build_status(make_policy(limit=100), _daily_with(replies=100)).state == STATE_EXCEEDED


class TestDayKeyTimezone:
    """判定与写入必须用同一个日界，否则跨零点前后两侧对不上"""

    async def test_day_key_matches_the_counter(self, db) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global(group_daily_limit=10)
        moment = datetime.datetime(2026, 5, 1, 15, 59, tzinfo=datetime.timezone.utc)
        await make_daily(replies=7, day=counter.quota_day(moment))

        status = await evaluate(channel, day=counter.quota_day(moment))

        assert status.used == 7
