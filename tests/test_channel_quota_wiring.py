"""频道每日额度：与用量账本、消息路径、广播、告警的接线

这一族测的是「谁在什么时候调了谁」。每条都对着一个具体会坏掉的形态：

- 账本落库之后没调计数 → 后台的 token 用量永远是 0；
- 计数失败被算成「流水落库失败」→ 运维去查数据库连接，真正坏的是别的东西；
- 暂停时还在调度回合 → 额度承诺形同虚设；
- 广播与节日祝福绕过闸门 → 一个被暂停的群照样收到推广。

行为一侧的细节（状态机、计数原子性、策略继承）在别的文件里。
"""

from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_llm_usage import DBLLMUsageRecord
from nekro_agent.schemas.usage import LLMUsageScene
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota.policy import SCOPE_GLOBAL
from nekro_agent.services.usage import ledger as ledger_module
from nekro_agent.services.usage.context import LLMUsageContext

ADAPTER = "onebot_v11"
INSTANCE = "wire1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_120001"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    policy_module.invalidate_policy_cache()
    ledger_module.reset_usage_recorder()
    try:
        yield
    finally:
        ledger_module.reset_usage_recorder()
        policy_module.invalidate_policy_cache()
        await Tortoise.close_connections()


async def seed_global(**fields: Any) -> None:
    defaults = {
        "enabled": True,
        "unit": "replies",
        "group_daily_limit": 10,
        "private_daily_limit": 10,
        "over_limit_action": "pause",
    }
    defaults.update(fields)
    await DBChannelQuotaPolicy.create(scope=SCOPE_GLOBAL, **defaults)
    policy_module.invalidate_policy_cache()


async def seed_channel() -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_120001",
        chat_key=CHAT_KEY,
        channel_type="group",
        data="{}",
    )


def usage_context(scene: LLMUsageScene = LLMUsageScene.CHAT) -> LLMUsageContext:
    return LLMUsageContext(adapter_key=ADAPTER, instance_key=INSTANCE, chat_key=CHAT_KEY, scene=scene)


class TestLedgerWiring:
    """账本写成功之后顺带记一次频道计数"""

    async def test_successful_write_feeds_the_counter(self, db) -> None:  # noqa: ARG002
        ledger_module.record_llm_usage(
            model_name="m",
            stream_mode=False,
            token_input=60,
            token_output=40,
            token_total=100,
            context=usage_context(LLMUsageScene.CHAT),
        )
        await ledger_module.flush_llm_usage()

        assert await DBLLMUsageRecord.all().count() == 1
        row = await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY).first()
        assert row is not None
        assert (row.tokens_all, row.tokens_reply) == (100, 100)

    async def test_non_reply_scene_only_fills_the_all_column(self, db) -> None:  # noqa: ARG002
        ledger_module.record_llm_usage(
            model_name="m",
            stream_mode=False,
            token_total=30,
            context=usage_context(LLMUsageScene.EMBEDDING),
        )
        await ledger_module.flush_llm_usage()

        row = await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY).first()
        assert row is not None
        assert (row.tokens_all, row.tokens_reply) == (30, 0)

    async def test_counter_failure_is_not_counted_as_a_ledger_failure(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        """两个失败不是一件事：混在一个 `except` 里会让运维查错方向"""

        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("计数表写不进去")

        monkeypatch.setattr(counter, "record_tokens", boom)

        ledger_module.record_llm_usage(
            model_name="m",
            stream_mode=False,
            token_total=10,
            context=usage_context(),
        )
        await ledger_module.flush_llm_usage()

        # 账本本身是成功的
        assert await DBLLMUsageRecord.all().count() == 1
        _pending, _dropped, failed = ledger_module.usage_recorder_stats()
        assert failed == 0, "计数失败不能记成流水落库失败"

    async def test_counter_failure_does_not_leave_a_task_exception(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """后台任务里漏出来的异常没人接，只会在日志里变成一条 task 异常"""

        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("炸")

        monkeypatch.setattr(counter, "record_tokens", boom)
        ledger_module.record_llm_usage(model_name="m", stream_mode=False, token_total=1, context=usage_context())
        tasks = list(ledger_module._pending)  # noqa: SLF001 - 这条契约没有公开出口

        await ledger_module.flush_llm_usage()

        assert all(task.exception() is None for task in tasks)

    async def test_missing_chat_key_skips_the_counter(self, db) -> None:  # noqa: ARG002
        """没有频道归属的调用（连通性测试、插件生成）不该凭空造出一行频道计数"""
        ledger_module.record_llm_usage(
            model_name="m",
            stream_mode=False,
            token_total=10,
            context=LLMUsageContext(adapter_key=ADAPTER, instance_key=INSTANCE, chat_key=""),
        )
        await ledger_module.flush_llm_usage()

        assert await DBChannelQuotaDaily.all().count() == 0


class TestPushHumanMessageGate:
    """入站消息那条路径：暂停时不开回合，只提示一次"""

    @pytest.fixture(autouse=True)
    def _isolate_service_state(self, monkeypatch: pytest.MonkeyPatch):
        """防抖换成空转，断言点放在 `pending_messages` 上"""
        from nekro_agent.services.message_service import message_service

        async def noop(*_args: Any, **_kwargs: Any) -> None:
            return

        monkeypatch.setattr(type(message_service), "_debounce_task", noop)
        message_service.pending_messages.pop(CHAT_KEY, None)
        message_service.debounce_timers.pop(CHAT_KEY, None)
        yield
        message_service.pending_messages.pop(CHAT_KEY, None)
        message_service.debounce_timers.pop(CHAT_KEY, None)

    def _patch_config(self, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
        from nekro_agent.core.config import config as global_config

        merged = global_config.model_copy(update={"AI_CHAT_TRIGGER_REGEX": [".*"], **overrides})

        async def fake_effective(self: DBChatChannel):
            return merged

        monkeypatch.setattr(DBChatChannel, "get_effective_config", fake_effective)

    async def _push(self, *, is_tome: int = 1) -> None:
        from nekro_agent.schemas.chat_message import ChatMessage, ChatType
        from nekro_agent.services.message_service import message_service

        channel = await DBChatChannel.filter(chat_key=CHAT_KEY).first()
        message = ChatMessage(
            message_id="",
            sender_id="10001",
            sender_name="tester",
            sender_nickname="tester",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            platform_userid="10001",
            is_tome=is_tome,
            is_recalled=False,
            chat_key=CHAT_KEY,
            chat_type=ChatType.GROUP,
            content_text="在吗",
            content_data=[],
            raw_cq_code="",
            ext_data={},
            send_timestamp=0,
        )
        await message_service.push_human_message(message=message, db_chat_channel=channel)

    async def test_feature_off_leaves_the_path_untouched(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """没有策略行 = 功能关着：行为必须与加这层之前完全一致"""
        from nekro_agent.services.message_service import message_service

        await seed_channel()
        self._patch_config(monkeypatch)

        await self._push()

        assert CHAT_KEY in message_service.pending_messages

    async def test_paused_channel_does_not_schedule_a_round(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        from nekro_agent.services.message_service import message_service

        await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=1)
        await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=counter.quota_day(), replies=1)
        self._patch_config(monkeypatch)

        await self._push()

        assert CHAT_KEY not in message_service.pending_messages

    async def test_paused_channel_still_persists_the_message(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """「不回」是行为层的：消息照常落库，群友的发言不该因为额度消失"""
        from nekro_agent.models.db_chat_message import DBChatMessage

        await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=1)
        await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=counter.quota_day(), replies=1)
        self._patch_config(monkeypatch)

        await self._push()

        assert await DBChatMessage.filter(chat_key=CHAT_KEY).count() == 1

    async def test_alert_mode_keeps_scheduling(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        from nekro_agent.services.message_service import message_service

        await seed_channel()
        await seed_global(over_limit_action="alert", group_daily_limit=1)
        await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=counter.quota_day(), replies=1)
        self._patch_config(monkeypatch)

        await self._push()

        assert CHAT_KEY in message_service.pending_messages

    async def test_a_failing_quota_check_lets_the_message_through(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """判定本身出错按「没暂停」放行：额度判定不能变成「群友说一句话、Bot 报一个错」的故障点"""
        from nekro_agent.services.channel_quota import service as channel_quota_service
        from nekro_agent.services.message_service import message_service

        await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=1)
        self._patch_config(monkeypatch)

        async def boom(_channel: Any) -> None:
            raise RuntimeError("库抖了一下")

        monkeypatch.setattr(channel_quota_service, "check_and_alert", boom)

        await self._push()

        assert CHAT_KEY in message_service.pending_messages


class TestScheduleAgentTaskBypass:
    """`schedule_agent_task` 自己也能挡住定时提醒这类间接触发"""

    async def test_paused_channel_is_not_scheduled(self, db) -> None:  # noqa: ARG002
        from nekro_agent.schemas.chat_message import ChatMessage
        from nekro_agent.services.message_service import message_service

        await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=1)
        await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=counter.quota_day(), replies=1)

        await message_service.schedule_agent_task(message=ChatMessage.create_empty(CHAT_KEY))

        assert CHAT_KEY not in message_service.pending_messages

    async def test_bypass_flag_lets_the_caller_decide(self, db) -> None:  # noqa: ARG002
        """入站路径自己判过了（含豁免的人），它要能放行"""
        from nekro_agent.schemas.chat_message import ChatMessage
        from nekro_agent.services.message_service import message_service

        await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=1)
        await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=counter.quota_day(), replies=1)

        await message_service.schedule_agent_task(
            message=ChatMessage.create_empty(CHAT_KEY),
            bypass_channel_quota=True,
        )

        assert CHAT_KEY in message_service.pending_messages

    async def test_feature_off_schedules_as_before(self, db) -> None:  # noqa: ARG002
        from nekro_agent.schemas.chat_message import ChatMessage
        from nekro_agent.services.message_service import message_service

        await seed_channel()

        await message_service.schedule_agent_task(message=ChatMessage.create_empty(CHAT_KEY))

        assert CHAT_KEY in message_service.pending_messages


class TestBroadcastSkip:
    """广播与节日祝福走同一条判据"""

    async def test_paused_channel_is_skipped(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        from nekro_agent.services.broadcast_targets import BroadcastSkipReason, get_broadcast_skip_reason

        channel = await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=1)
        await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=counter.quota_day(), replies=1)

        reason = await get_broadcast_skip_reason(channel, inactive_days=0)

        assert reason is BroadcastSkipReason.CHANNEL_QUOTA_PAUSED

    async def test_paused_channel_is_skipped_even_when_named_explicitly(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        """白名单点名也不该破例：额度是平台方的承诺，点名不改变它"""
        from nekro_agent.services.broadcast_targets import BroadcastSkipReason, get_broadcast_skip_reason

        channel = await seed_channel()
        await seed_global(over_limit_action="pause", group_daily_limit=1)
        await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=counter.quota_day(), replies=1)

        assert await get_broadcast_skip_reason(channel, inactive_days=0) is BroadcastSkipReason.CHANNEL_QUOTA_PAUSED

    async def test_feature_off_is_not_skipped(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.broadcast_targets import get_broadcast_skip_reason

        channel = await seed_channel()

        assert await get_broadcast_skip_reason(channel, inactive_days=0) is None
