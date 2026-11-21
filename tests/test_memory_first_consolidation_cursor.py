"""频道第一次沉淀时把游标起点设成当时的最大消息 id。

旧行为是读不到游标就按 0 算，于是一个有几个月历史的群开启记忆后，先被沉淀的是最早
那批旧消息，当下的对话长期进不了记忆。这里钉住：首次沉淀只初始化游标并从最新消息
之后开始，不回溯开启之前的历史；要回溯走重建（`persist_progress=False`）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory.consolidator import EpisodicConsolidator

WORKSPACE_ID = 4404
CHAT_KEY = "__test__:memory:first-consolidation-cursor"


class _FakeMessage:
    def __init__(self, db_id: int, content: str = "这是一条足够长的测试消息内容") -> None:
        self.id = db_id
        self.send_timestamp = 1_700_000_000 + db_id
        self.sender_id = "user-1"
        # 闸开时沉淀会按这一批消息收集账号称呼，假消息也得带上账号列
        self.platform_userid = "user-1"
        self.sender_name = "用户"
        self.sender_nickname = "用户"
        self.content_text = content


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



async def _not_paused(_chat_key: str) -> bool:
    return False


def _make_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: str = "{}",
    latest: int = 10000,
    batch: list[_FakeMessage] | None = None,
) -> tuple[EpisodicConsolidator, _FakeChannel, dict[str, int]]:
    """把取消息、调模型、写记忆、查最大消息 id 全部替换掉。"""
    channel = _FakeChannel(data=data)
    consolidator = EpisodicConsolidator(WORKSPACE_ID)
    messages = [_FakeMessage(10001), _FakeMessage(10002)] if batch is None else batch
    calls = {"llm": 0, "fetch": 0, "latest": 0}

    async def fake_load_channel(_chat_key: str) -> _FakeChannel:
        return channel

    async def fake_fetch(_chat_key: str, **_kwargs: Any) -> list[_FakeMessage]:
        calls["fetch"] += 1
        return messages

    async def fake_store(*_args: Any, **_kwargs: Any) -> tuple[None, list[Any], list[Any]]:
        return (None, [], [])

    async def fake_llm(_prompt: str, *, use_fallback_model: bool = False) -> str:
        calls["llm"] += 1
        return '{"memories": []}'

    async def fake_latest(_chat_key: str) -> int:
        calls["latest"] += 1
        return latest

    monkeypatch.setattr(consolidator, "_load_channel", fake_load_channel)
    # 频道额度的「后台暂停」判断要查库：本文件不建库，之前的用例留下的数据库配置会让它
    # 现开一个 sqlite 连接、没人关。额度不在这里测，按「没暂停」固定住
    monkeypatch.setattr(consolidator_module, "background_paused", _not_paused)
    monkeypatch.setattr(consolidator, "_fetch_messages", fake_fetch)
    monkeypatch.setattr(consolidator, "_store_memory", fake_store)
    monkeypatch.setattr(consolidator, "_call_llm", fake_llm)
    monkeypatch.setattr(consolidator, "_latest_message_db_id", fake_latest)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0.0)
    return consolidator, channel, calls


class TestFirstConsolidationInitializesCursor:
    @pytest.mark.asyncio
    async def test_first_round_only_initializes_then_next_round_consumes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        consolidator, channel, calls = _make_harness(monkeypatch)

        first = await consolidator.consolidate(CHAT_KEY)
        state = channel.memory_state()

        assert first.cursor_initialized is True
        assert first.messages_processed == 0
        assert calls["fetch"] == 0
        assert calls["llm"] == 0
        assert state["last_consolidated_message_db_id"] == 10000
        assert state["consolidation_start_message_db_id"] == 10000
        assert "consolidation_started_at" in state
        assert channel.save_calls == 1

        second = await consolidator.consolidate(CHAT_KEY)

        assert second.cursor_initialized is False
        assert calls["fetch"] == 1
        assert calls["llm"] == 1
        assert channel.memory_state()["last_consolidated_message_db_id"] == 10002

    @pytest.mark.asyncio
    async def test_existing_cursor_never_queries_latest_message_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = json.dumps({"memory_state": {"last_consolidated_message_db_id": 500}})
        consolidator, channel, calls = _make_harness(monkeypatch, data=data)

        async def forbidden_latest(_chat_key: str) -> int:
            raise AssertionError("已有游标时不应再查最大消息 id")

        monkeypatch.setattr(consolidator, "_latest_message_db_id", forbidden_latest)

        result = await consolidator.consolidate(CHAT_KEY)

        assert result.errors is None
        assert calls["fetch"] == 1
        assert channel.memory_state()["last_consolidated_message_db_id"] == 10002

    @pytest.mark.asyncio
    async def test_failure_counters_without_cursor_still_initialize(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = json.dumps(
            {"memory_state": {"consecutive_failures": 1, "last_failure_reason": "auth"}},
        )
        consolidator, channel, calls = _make_harness(monkeypatch, data=data)

        result = await consolidator.consolidate(CHAT_KEY)
        state = channel.memory_state()

        assert result.cursor_initialized is True
        assert calls["fetch"] == 0
        assert state["last_consolidated_message_db_id"] == 10000
        assert state["consolidation_start_message_db_id"] == 10000
        assert state["consecutive_failures"] == 1
        assert state["last_failure_reason"] == "auth"


class TestCursorInitializationSkipped:
    @pytest.mark.asyncio
    async def test_persist_progress_false_does_not_touch_channel_data(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        initial = "{}"
        consolidator, channel, calls = _make_harness(monkeypatch, data=initial)

        result = await consolidator.consolidate(CHAT_KEY, persist_progress=False)

        assert result.cursor_initialized is False
        assert channel.data == initial
        assert channel.save_calls == 0
        assert calls["fetch"] == 1
        assert calls["latest"] == 0

    @pytest.mark.asyncio
    async def test_explicit_start_after_db_id_is_not_initialized(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        consolidator, channel, calls = _make_harness(monkeypatch)

        async def forbidden_latest(_chat_key: str) -> int:
            raise AssertionError("显式传了起点时不应查最大消息 id")

        monkeypatch.setattr(consolidator, "_latest_message_db_id", forbidden_latest)

        result = await consolidator.consolidate(
            CHAT_KEY,
            start_after_db_id=0,
            persist_progress=True,
        )

        assert result.errors is None
        assert result.cursor_initialized is False
        assert calls["fetch"] == 1
        assert "consolidation_start_message_db_id" not in channel.memory_state()


class TestCursorStartValue:
    @pytest.mark.asyncio
    async def test_empty_channel_writes_zero_and_does_not_reinitialize(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        consolidator, channel, calls = _make_harness(monkeypatch, latest=0, batch=[])

        first = await consolidator.consolidate(CHAT_KEY)
        state = channel.memory_state()

        assert first.cursor_initialized is True
        assert "last_consolidated_message_db_id" in state
        assert state["last_consolidated_message_db_id"] == 0
        assert state["consolidation_start_message_db_id"] == 0

        await consolidator.consolidate(CHAT_KEY)
        assert calls["latest"] == 1

    @pytest.mark.asyncio
    async def test_max_message_db_id_caps_the_start_cursor(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        consolidator, channel, calls = _make_harness(monkeypatch, latest=10000)

        result = await consolidator.consolidate(CHAT_KEY, max_message_db_id=9000)
        state = channel.memory_state()

        assert result.cursor_initialized is True
        assert calls["fetch"] == 0
        assert state["last_consolidated_message_db_id"] == 9000
        assert state["consolidation_start_message_db_id"] == 9000
