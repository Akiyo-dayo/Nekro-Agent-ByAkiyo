"""频道每日额度：暂停时停掉后台调用

三条口径在这里钉死：

- 按 token 计、计入范围是全部调用时，暂停的频道连记忆沉淀与好感评估一起停；
- 按回复次数计、或计入范围只算回复链路时，后台调用不占额度，也就不停；
- 判定本身出任何问题都放行——一次查库抖动不该让所有频道的记忆与好感一起停摆。

沉淀一侧还要证明「停下」是**什么都没动**（游标不前进、记忆表不写），并且调度器把它当
「跳过」而不是「失败」：待处理消息数保留、不进失败退避，第二天恢复后从断点接着沉淀。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.services.affection import evaluator as evaluator_module
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota import service as service_module
from nekro_agent.services.channel_quota.policy import SCOPE_GLOBAL
from nekro_agent.services.channel_quota.service import background_paused
from nekro_agent.services.memory.consolidator import ConsolidationResult, EpisodicConsolidator
from nekro_agent.services.memory.scheduler import (
    ChannelActivityStats,
    ConsolidationPriority,
    ConsolidationTask,
    MemoryMaintenanceScheduler,
)

ADAPTER = "onebot_v11"
INSTANCE = "bg1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_310001"
WORKSPACE_ID = 7401


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


async def seed_channel(chat_key: str = CHAT_KEY, channel_type: str = "group") -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=chat_key.rsplit("-", 1)[-1],
        chat_key=chat_key,
        channel_type=channel_type,
        data="{}",
    )


async def seed_global(**fields: Any) -> None:
    defaults = {
        "enabled": True,
        "unit": "tokens",
        "group_daily_limit": 100,
        "private_daily_limit": 100,
        "over_limit_action": "pause",
    }
    defaults.update(fields)
    await DBChannelQuotaPolicy.create(scope=SCOPE_GLOBAL, **defaults)
    policy_module.invalidate_policy_cache()


async def seed_paused_day(*, tokens_all: int = 500, replies: int = 0) -> DBChannelQuotaDaily:
    """把「今天」的用量推到额度上限之上，让状态是 paused"""
    return await DBChannelQuotaDaily.create(
        chat_key=CHAT_KEY,
        day=counter.quota_day(),
        replies=replies,
        tokens_all=tokens_all,
        tokens_reply=tokens_all,
    )


class _LogRecorder:
    """抓产品代码真正打出去的日志（`get_sub_logger` 是 loguru，`caplog` 抓不到）"""

    def __init__(self) -> None:
        self.warnings: List[str] = []
        self.infos: List[str] = []
        self.debugs: List[str] = []

    def warning(self, message: object) -> None:
        self.warnings.append(str(message))

    def info(self, message: object) -> None:
        self.infos.append(str(message))

    def debug(self, message: object) -> None:
        self.debugs.append(str(message))


# ---------------------------------------------------------------------------
# 判定本身
# ---------------------------------------------------------------------------


class TestBackgroundPaused:
    async def test_token_unit_paused_and_all_scope_pauses(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await seed_channel()
        await seed_global(unit="tokens", group_daily_limit=100)
        await seed_paused_day()
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "all")

        assert await background_paused(CHAT_KEY) is True

    async def test_reply_only_scope_does_not_pause(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """只算回复链路时后台调用的 token 不计入额度，停掉它们省不下任何额度"""
        await seed_channel()
        await seed_global(unit="tokens", group_daily_limit=100)
        await seed_paused_day()
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "reply_only")

        assert await background_paused(CHAT_KEY) is False

    async def test_replies_unit_does_not_pause(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """按回复次数计时后台调用不占额度，停掉只会让记忆与好感停滞"""
        await seed_channel()
        await seed_global(unit="replies", group_daily_limit=2)
        await seed_paused_day(tokens_all=0, replies=2)
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "all")

        assert await background_paused(CHAT_KEY) is False

    async def test_not_paused_does_not_pause(self, db) -> None:  # noqa: ARG002
        await seed_channel()
        await seed_global(unit="tokens", group_daily_limit=1000)
        await seed_paused_day(tokens_all=10)

        assert await background_paused(CHAT_KEY) is False

    async def test_feature_disabled_does_not_pause(self, db) -> None:  # noqa: ARG002
        """没有全局策略行 —— 功能关着时行为必须一个字节都不变"""
        await seed_channel()
        await seed_paused_day()

        assert await background_paused(CHAT_KEY) is False

    async def test_exception_is_swallowed_and_reported(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorder = _LogRecorder()
        monkeypatch.setattr(service_module, "logger", recorder)

        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("查库炸了")

        monkeypatch.setattr(DBChatChannel, "filter", classmethod(lambda cls, **_kwargs: boom()))

        assert await background_paused(CHAT_KEY) is False
        assert any("后台暂停判定失败" in message for message in recorder.warnings)


# ---------------------------------------------------------------------------
# 记忆沉淀
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self, data: str = "{}") -> None:
        self.data = data
        self.save_calls = 0

    async def save(self, **_kwargs: Any) -> None:
        self.save_calls += 1

    async def get_effective_config(self) -> Any:
        return SimpleNamespace(AI_CHAT_PRESET_SETTING="")

    def memory_state(self) -> dict[str, Any]:
        return json.loads(self.data).get("memory_state", {})


class _FakeMessage:
    def __init__(self, db_id: int) -> None:
        self.id = db_id
        self.send_timestamp = db_id * 10 + 1_000
        self.sender_id = "user-1"
        self.platform_userid = "user-1"
        self.sender_name = "用户"
        self.sender_nickname = "用户"
        self.content_text = "这是一条足够长的测试消息内容"


CURSOR_DATA = json.dumps({"memory_state": {"last_consolidated_message_db_id": 100}})


def _consolidator_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: str = CURSOR_DATA,
) -> tuple[EpisodicConsolidator, _FakeChannel, dict[str, int]]:
    """把取消息、调模型、写记忆全部替换掉，只留 `consolidate` 开头的闸门

    `_extract_memories` 计数用来证明「有没有真的往下走」：被拦时它一次都不该被调到。
    频道 `data` 默认带一个已有游标，否则走到第一次沉淀那条分支就「只初始化游标」返回了，
    看谁都像被拦住。
    """
    channel = _FakeChannel(data=data)
    consolidator = EpisodicConsolidator(WORKSPACE_ID)
    calls = {"extracted": 0}

    async def fake_load_channel(_chat_key: str) -> _FakeChannel:
        return channel

    async def fake_fetch(_chat_key: str, **_kwargs: Any) -> list[_FakeMessage]:
        return [_FakeMessage(101), _FakeMessage(102)]

    async def fake_store(*_args: Any, **_kwargs: Any) -> tuple[None, list[Any], list[Any]]:
        return (None, [], [])

    async def fake_extract(_messages: Any, chat_key: str | None = None) -> Any:
        calls["extracted"] += 1
        return SimpleNamespace(memories=[], channel_summary=None)

    async def fake_persona(_chat_key: str) -> None:
        return None

    monkeypatch.setattr(consolidator, "_load_channel", fake_load_channel)
    monkeypatch.setattr(consolidator, "_fetch_messages", fake_fetch)
    monkeypatch.setattr(consolidator, "_store_memory", fake_store)
    monkeypatch.setattr(consolidator, "_extract_memories", fake_extract)
    monkeypatch.setattr(consolidator, "load_persona_context", fake_persona)
    return consolidator, channel, calls


class TestConsolidationGate:
    async def test_paused_channel_skips_realtime_consolidation(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """游标不动、记忆表不写：第二天恢复后从同一个断点接着沉淀"""
        await seed_channel()
        await seed_global(unit="tokens", group_daily_limit=100)
        await seed_paused_day()
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "all")

        consolidator, channel, calls = _consolidator_harness(monkeypatch)
        channel.data = CURSOR_DATA
        before = await DBMemParagraph.all().count()

        result = await consolidator.consolidate(CHAT_KEY, persist_progress=True)

        assert result.skipped_reason == "channel_quota_paused"
        assert result.messages_processed == 0
        assert result.failed_reason is None
        assert calls["extracted"] == 0
        assert channel.save_calls == 0
        assert channel.memory_state()["last_consolidated_message_db_id"] == 100
        assert await DBMemParagraph.all().count() == before

    async def test_reply_only_scope_does_not_skip(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """计入范围只算回复链路时后台调用不计额度，沉淀照常往下走"""
        await seed_channel()
        await seed_global(unit="tokens", group_daily_limit=100)
        await seed_paused_day()
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "reply_only")

        consolidator, _channel, calls = _consolidator_harness(monkeypatch)

        result = await consolidator.consolidate(CHAT_KEY, persist_progress=True)

        assert result.skipped_reason != "channel_quota_paused"
        assert calls["extracted"] > 0

    async def test_rebuild_is_not_blocked(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """重建是管理员手动发起的明确动作，不该被额度闸门挡住"""
        await seed_channel()
        await seed_global(unit="tokens", group_daily_limit=100)
        await seed_paused_day()
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "all")

        consolidator, _channel, calls = _consolidator_harness(monkeypatch)

        result = await consolidator.consolidate(CHAT_KEY, persist_progress=False)

        assert result.skipped_reason != "channel_quota_paused"
        assert calls["extracted"] > 0

    async def test_feature_disabled_is_not_blocked(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """没有全局策略行：这条闸门一个字节的行为都不该变"""
        await seed_channel()
        await seed_paused_day()

        consolidator, _channel, calls = _consolidator_harness(monkeypatch)

        result = await consolidator.consolidate(CHAT_KEY, persist_progress=True)

        assert result.skipped_reason != "channel_quota_paused"
        assert calls["extracted"] > 0


# ---------------------------------------------------------------------------
# 调度器：跳过 ≠ 失败
# ---------------------------------------------------------------------------


@pytest.fixture
async def scheduler():
    instance = MemoryMaintenanceScheduler()
    yield instance
    for timer, _due_ts in list(instance._retry_timers.values()):  # noqa: SLF001
        timer.cancel()
    instance._retry_timers.clear()  # noqa: SLF001


class TestSchedulerTreatsPauseAsSkip:
    async def test_pending_survives_and_no_failure_backoff(self, scheduler: MemoryMaintenanceScheduler) -> None:
        scheduler._versions[(WORKSPACE_ID, CHAT_KEY)] = 0  # noqa: SLF001
        scheduler._channel_stats[(WORKSPACE_ID, CHAT_KEY)] = ChannelActivityStats(  # noqa: SLF001
            workspace_id=WORKSPACE_ID,
            chat_key=CHAT_KEY,
            pending_messages=7,
        )
        task = ConsolidationTask(
            priority=ConsolidationPriority.NORMAL,
            scheduled_at=0.0,
            workspace_id=WORKSPACE_ID,
            chat_key=CHAT_KEY,
            version=0,
        )

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            return ConsolidationResult(skipped_reason="channel_quota_paused")

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001

        stats = scheduler._channel_stats[(WORKSPACE_ID, CHAT_KEY)]  # noqa: SLF001
        # 待处理消息数保留：跳过只是「这一轮没做」，不是「这一批做完了」
        assert stats.pending_messages == 7
        # 没有进失败退避：下一次有消息就能重新排进来
        assert stats.next_eligible_at is None
        assert stats.consecutive_skips == 0
        assert scheduler._retry_timers == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# 好感度评估
# ---------------------------------------------------------------------------


class _StubChannel:
    def __init__(self, chat_key: str) -> None:
        self.chat_key = chat_key


class TestAffectionGate:
    """暂停期间不评估，而且要**不**更新节流时间戳

    `_last_evaluated_at` 是「上次真的评过」的意思。暂停时也写它，等于把恢复后的第一次
    评估推迟一个 `AFFECTION_UPDATE_MIN_INTERVAL_SECONDS`，白等一整个窗口。
    """

    @pytest.fixture(autouse=True)
    def _reset_throttle(self):
        evaluator_module.reset_evaluation_throttle()
        yield
        evaluator_module.reset_evaluation_throttle()

    async def test_paused_channel_skips_evaluation(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await seed_channel()
        await seed_global(unit="tokens", group_daily_limit=100)
        await seed_paused_day()
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "all")
        calls: list[str] = []

        async def spy(channel: Any, _config: Any) -> int:
            calls.append(channel.chat_key)
            return 0

        monkeypatch.setattr(evaluator_module, "evaluate_channel", spy)

        await evaluator_module._run_evaluation(  # noqa: SLF001
            _StubChannel(CHAT_KEY),  # type: ignore[arg-type]
            config,
        )

        assert calls == []
        assert CHAT_KEY not in evaluator_module._last_evaluated_at  # noqa: SLF001

    async def test_reply_only_scope_still_evaluates(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await seed_channel()
        await seed_global(unit="tokens", group_daily_limit=100)
        await seed_paused_day()
        monkeypatch.setattr(config, "CHANNEL_QUOTA_TOKEN_SCOPE", "reply_only")
        calls: list[str] = []

        async def spy(channel: Any, _config: Any) -> int:
            calls.append(channel.chat_key)
            return 0

        monkeypatch.setattr(evaluator_module, "evaluate_channel", spy)

        await evaluator_module._run_evaluation(  # noqa: SLF001
            _StubChannel(CHAT_KEY),  # type: ignore[arg-type]
            config,
        )

        assert calls == [CHAT_KEY]
        assert CHAT_KEY in evaluator_module._last_evaluated_at  # noqa: SLF001

    async def test_not_paused_still_evaluates(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """没超额度时这条闸门不该拦下任何东西"""
        await seed_channel()
        await seed_global(unit="tokens", group_daily_limit=1000)
        await seed_paused_day(tokens_all=1)
        calls: list[str] = []

        async def spy(channel: Any, _config: Any) -> int:
            calls.append(channel.chat_key)
            return 0

        monkeypatch.setattr(evaluator_module, "evaluate_channel", spy)

        await evaluator_module._run_evaluation(  # noqa: SLF001
            _StubChannel(CHAT_KEY),  # type: ignore[arg-type]
            config,
        )

        assert calls == [CHAT_KEY]
