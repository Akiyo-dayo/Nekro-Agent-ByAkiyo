"""统一额度闸门：平台方额度与租户自限

判定收进 `channel_quota.gate` 之后，这里钉住五件事：

1. 自限按**回合数**算，读的是计数表 `DBChannelQuotaDaily.replies`，不是 Bot 消息条数；
2. 每小时均摊同样按回合数：读进程内的 `counter.hour_replies`，与当天计数同一个加数点；
3. 换天与频道额度共用 `CHANNEL_QUOTA_TIMEZONE`，临时加额也按同一个日期键；
4. 平台方暂停优先于自限；
5. 任何一层判定出错都放行，`is_paused` 也认自限。
"""

import datetime
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendResponse,
)
from nekro_agent.core.config import config
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.channel_quota import notice as notice_module
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota import service as service_module
from nekro_agent.services.channel_quota.policy import SCOPE_GLOBAL
from nekro_agent.services.channel_quota.self_limit import SelfLimitStatus, evaluate_self_limit
from nekro_agent.services.channel_quota.service import gate, is_paused
from nekro_agent.services.quota_service import quota_service

ADAPTER = "onebot_v11"
INSTANCE = "self1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_140001"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    policy_module.invalidate_policy_cache()
    counter.reset_timezone_warning()
    counter.reset_hour_replies()
    quota_service.clear_boost(CHAT_KEY)
    try:
        yield
    finally:
        quota_service.clear_boost(CHAT_KEY)
        counter.reset_hour_replies()
        policy_module.invalidate_policy_cache()
        await Tortoise.close_connections()


async def seed_channel() -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_140001",
        chat_key=CHAT_KEY,
        channel_type="group",
        data="{}",
    )


async def seed_daily(replies: int, *, day: str | None = None) -> DBChannelQuotaDaily:
    return await DBChannelQuotaDaily.create(
        chat_key=CHAT_KEY,
        day=day or counter.quota_day(),
        replies=replies,
    )


async def seed_platform(**fields: Any) -> None:
    defaults = {
        "enabled": True,
        "unit": "replies",
        "group_daily_limit": 1,
        "private_daily_limit": 1,
        "over_limit_action": "pause",
        "pause_message": "额度用完了",
    }
    defaults.update(fields)
    await DBChannelQuotaPolicy.create(scope=SCOPE_GLOBAL, **defaults)
    policy_module.invalidate_policy_cache()


def patch_effective_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    limit: int,
    hourly: bool = False,
) -> None:
    merged = config.model_copy(
        update={
            "AI_CHAT_DAILY_REPLY_LIMIT": limit,
            "AI_CHAT_ENABLE_HOURLY_LIMIT": hourly,
        },
    )

    async def fake_effective(_self: DBChatChannel):
        return merged

    monkeypatch.setattr(DBChatChannel, "get_effective_config", fake_effective)


async def add_hour_reply() -> None:
    """把一个回合计进当前整点小时：走与运行时同一个加数点（`record_reply`）"""
    await counter.record_reply(CHAT_KEY, ADAPTER, INSTANCE)


class TestSelfLimitGate:
    """自限的边界：到额度拦，差一个放行"""

    async def test_replies_at_the_limit_is_blocked(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        patch_effective_config(monkeypatch, limit=3)
        await seed_daily(replies=3)

        result = await gate(channel)

        assert result.blocked is True
        assert result.reason == "self_daily"
        assert result.self_status is not None
        assert (result.self_status.used, result.self_status.allowed) == (3, 3)

    async def test_replies_below_the_limit_passes(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        patch_effective_config(monkeypatch, limit=3)
        await seed_daily(replies=2)

        result = await gate(channel)

        assert result.blocked is False
        assert result.reason == ""

    async def test_used_counts_rounds_not_bot_messages(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """一轮拆成三条消息也只算一个回合：记一次回合，再落三条 Bot 消息行"""
        channel = await seed_channel()
        patch_effective_config(monkeypatch, limit=10)
        await add_hour_reply()
        for index in range(3):
            await DBChatMessage.create(
                message_id=f"bot-{index}",
                sender_id=-1,
                sender_name="Bot",
                sender_nickname="Bot",
                adapter_key=ADAPTER,
                instance_key=INSTANCE,
                platform_userid="0",
                is_tome=0,
                is_recalled=False,
                chat_key=CHAT_KEY,
                chat_type="group",
                content_text="回复",
                content_data="[]",
                raw_cq_code="",
                ext_data="{}",
                send_timestamp=1_800_000_000,
            )

        status = await evaluate_self_limit(channel)

        assert status.used == 1
        assert status.allowed == 10
        assert status.exceeded == ""

    async def test_day_boundary_follows_the_quota_timezone(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """北京时间 00:30（UTC 前一天 16:30）：昨天的计数行不算进今天"""
        real_quota_day = counter.quota_day
        moment = datetime.datetime(2026, 5, 1, 16, 30, tzinfo=datetime.timezone.utc)
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TIMEZONE", "Asia/Shanghai")
        counter.reset_timezone_warning()
        assert real_quota_day(moment) == "2026-05-02"

        channel = await seed_channel()
        patch_effective_config(monkeypatch, limit=3)
        await seed_daily(replies=3, day="2026-05-01")
        monkeypatch.setattr(counter, "quota_day", lambda at=None: real_quota_day(moment))

        status = await evaluate_self_limit(channel)
        result = await gate(channel)

        assert status.used == 0
        assert status.exceeded == ""
        assert result.blocked is False

    async def test_boost_extends_the_allowance_and_shares_the_day_key(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        channel = await seed_channel()
        patch_effective_config(monkeypatch, limit=3)
        await seed_daily(replies=3)
        assert (await gate(channel)).blocked is True

        quota_service.add_boost(CHAT_KEY, 2)
        status = await evaluate_self_limit(channel)

        assert status.allowed == 5
        assert status.exceeded == ""
        assert (await gate(channel)).blocked is False
        assert quota_service._daily_boosts[CHAT_KEY]["date"] == counter.quota_day()  # noqa: SLF001

    async def test_platform_pause_wins_over_self_limit(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_platform(over_limit_action="pause", group_daily_limit=1)
        patch_effective_config(monkeypatch, limit=3)
        await seed_daily(replies=3)

        result = await gate(channel)

        assert result.blocked is True
        assert result.reason == "platform"
        assert result.status is not None
        assert result.self_status is None

    async def test_is_paused_recognizes_the_self_limit(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """`schedule_agent_task` 走 `is_paused`，因此定时提醒也会被自限拦住"""
        await seed_channel()
        patch_effective_config(monkeypatch, limit=3)
        await seed_daily(replies=3)

        assert await is_paused(CHAT_KEY) is True

    async def test_self_limit_failure_is_treated_as_allowed(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()

        async def boom(_channel: DBChatChannel) -> SelfLimitStatus:
            raise RuntimeError("计数表读不到")

        monkeypatch.setattr(service_module, "evaluate_self_limit", boom)

        result = await gate(channel)

        assert result.blocked is False
        assert result.reason == ""
        assert result.self_status is None


class TestHourlyRounds:
    """每小时均摊按回合数：进程内计数，与当天计数同一个加数点"""

    async def test_one_round_counts_once_regardless_of_messages(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        patch_effective_config(monkeypatch, limit=240, hourly=True)

        await add_hour_reply()

        status = await evaluate_self_limit(channel)

        assert counter.hour_replies(CHAT_KEY) == 1
        assert status.hourly_used == 1
        assert status.hourly_limit == 10
        assert status.exceeded == ""

    async def test_the_hourly_quota_blocks_on_the_round_count(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        # 每日 240 → 每小时 10；不写任何 Bot 消息行，只记回合
        patch_effective_config(monkeypatch, limit=240, hourly=True)
        for _ in range(10):
            await add_hour_reply()

        result = await gate(channel)

        assert result.blocked is True
        assert result.reason == "self_hourly"
        assert result.self_status is not None
        assert (result.self_status.hourly_used, result.self_status.hourly_limit) == (10, 10)

    async def test_hourly_count_resets_across_the_hour_boundary(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        patch_effective_config(monkeypatch, limit=240, hourly=True)
        hour = 3_600 * 500
        monkeypatch.setattr(counter, "_hour_start", lambda at=None: hour)
        for _ in range(10):
            await add_hour_reply()
        assert counter.hour_replies(CHAT_KEY) == 10

        monkeypatch.setattr(counter, "_hour_start", lambda at=None: hour + 3600)
        status = await evaluate_self_limit(channel)
        assert status.hourly_used == 0
        assert (await gate(channel)).blocked is False

        await add_hour_reply()
        assert counter.hour_replies(CHAT_KEY) == 1

    async def test_an_old_hour_never_reads_as_the_current_count(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        patch_effective_config(monkeypatch, limit=240, hourly=True)
        hour = 3_600 * 600
        monkeypatch.setattr(counter, "_hour_start", lambda at=None: hour)
        await add_hour_reply()

        monkeypatch.setattr(counter, "_hour_start", lambda at=None: hour + 7200)
        status = await evaluate_self_limit(channel)

        assert status.hourly_used == 0
        assert counter.hour_replies(CHAT_KEY) == 0

    async def test_overflowing_the_table_drops_other_hours(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """表超过 4096 项时清掉不属于当前小时的项，当前小时的留着"""
        current = 3_600 * 700
        monkeypatch.setattr(counter, "_hour_start", lambda at=None: current)
        monkeypatch.setattr(counter, "MAX_HOUR_REPLY_KEYS", 4)
        for index in range(3):
            counter._hour_replies[CHAT_KEY + f"-old{index}"] = (current - 3600, index + 1)  # noqa: SLF001

        for index in range(2):
            await counter.record_reply(f"{CHAT_KEY}-new{index}", ADAPTER, INSTANCE)

        hours = {key: value[0] for key, value in counter._hour_replies.items()}  # noqa: SLF001
        assert set(hours.values()) == {current}
        assert set(hours) == {f"{CHAT_KEY}-new0", f"{CHAT_KEY}-new1"}

    async def test_reset_clears_the_counts(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await add_hour_reply()

        counter.reset_hour_replies()

        assert counter.hour_replies(CHAT_KEY) == 0


class _RecordingAdapter:
    def __init__(self) -> None:
        self.requests: list[PlatformSendRequest] = []

    async def forward_message(self, request: PlatformSendRequest) -> PlatformSendResponse:
        self.requests.append(request)
        return PlatformSendResponse(success=True, recorded=True)


class TestSelfLimitNotice:
    """一个频道一天只提示一次，与平台方提示共用同一个标记"""

    async def test_two_calls_same_day_send_once(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_daily(replies=3)
        recorder = _RecordingAdapter()

        async def fake_get_adapter_for_chat(_chat_key: str):
            return recorder

        monkeypatch.setattr(notice_module.adapter_utils, "get_adapter_for_chat", fake_get_adapter_for_chat)
        status = SelfLimitStatus(
            limit=3,
            boost=0,
            allowed=3,
            used=3,
            hourly_enabled=False,
            hourly_limit=0,
            hourly_used=0,
            exceeded="daily",
        )

        first = await notice_module.send_self_limit_notice_once(channel, status, "self_daily")
        second = await notice_module.send_self_limit_notice_once(channel, status, "self_daily")

        assert first is True
        assert second is False
        assert len(recorder.requests) == 1
        assert recorder.requests[0].segments[0].content == (
            "今日回复配额已用完 (3/3)，请明天再试或联系管理员使用 /quota_boost 临时提升配额"
        )
        row = await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY).first()
        assert row is not None
        assert row.pause_notice_sent_at is not None
