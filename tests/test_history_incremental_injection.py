"""迭代时的增量渲染不再重复注入频道摘要与记忆召回。

同一轮里沙盒没有正常收尾时会再渲染一次增量历史。摘要只属于第一次渲染；记忆召回只在
这次增量里真的有新的人类消息时才跑，且只补本轮还没注入过的条目。第一次渲染与非增量
渲染不排除任何记忆，同一轮内的冷却照旧不生效。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, List

import pytest

# 复用兄弟用例里已经搭好的可注入环境，不再抄一份替身。
# `tests/` 下没有 `__init__.py`，pytest 的 prepend 导入模式会把该目录放进 sys.path。
from test_agent_round_control import (
    DRAW_PLUGIN_FAILURE,
    SEND_TEXT_FAILURE,
    STICKER_SIGNATURE_FAILURE,
    _error_result,
    _Harness,
    _Parsed,
    _SandboxResult,
)
from tortoise import Tortoise

from nekro_agent.core.config import ModelConfigGroup, config
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.schemas.chat_message import SELF_SENT_SENDER_ID, TOOL_RESULT_SENDER_ID, TOOL_RESULT_SENDER_NAME
from nekro_agent.services.affection.suppression import reset_narrative_suppression
from nekro_agent.services.agent.run_agent import AgentRoundUnfinishedError
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.memory import instance_scope as instance_scope_module
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory import round_recall as round_recall_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.recall_contract import MemoryRecallPlan, MemoryRecallQuerySpec
from nekro_agent.services.memory.recall_cooldown import reset_recall_cooldown
from nekro_agent.services.memory.retriever import RetrievedMemory
from nekro_agent.services.memory.round_recall import (
    commit_round_recall,
    discard_round_recall,
    injected_keys,
    note_injected,
    reset_round_recalls,
)

CHAT_KEY = "onebot_v11-incr-group_31"
PRESET_ID = 7
MODEL_GROUP = ModelConfigGroup(GROUP_NAME="incr-primary", CHAT_MODEL="incr-model")


def _memory(pid: int, *, content: str | None = None) -> RetrievedMemory:
    return RetrievedMemory(
        target_id=pid,
        source_type="paragraph",
        episode_id=None,
        paragraph_id=pid,
        relation_id=None,
        content=content or f"记忆内容 {pid}",
        summary=f"摘要 {pid}",
        cognitive_type="episodic",
        knowledge_type="conversation",
        similarity_score=0.6,
        effective_weight=float(10 - pid),
        event_time=None,
        origin_chat_key=CHAT_KEY,
    )


def _channel(base: int) -> Any:
    class _Channel:
        chat_key = CHAT_KEY
        workspace_id = None
        adapter_key = "onebot_v11"
        instance_key = "incr"
        channel_type = "group"
        conversation_start_time = datetime.fromtimestamp(base)

        async def get_effective_config(self) -> SimpleNamespace:
            return SimpleNamespace(
                MEMORY_ENABLE_FOR_CHAT=True,
                MEMORY_CONTEXT_MAX_ITEMS=3,
                MEMORY_CONTEXT_MAX_LENGTH=1200,
                MEMORY_RECALL_COOLDOWN_ROUNDS=0,
            )

    return _Channel()


async def _message(
    index: int,
    *,
    sender_id: str,
    sender_name: str,
    send_timestamp: int,
) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=sender_id,
        sender_name=sender_name,
        sender_nickname=sender_name,
        is_tome=0,
        is_recalled=False,
        adapter_key="onebot_v11",
        instance_key="incr",
        message_id=f"m{index}",
        chat_key=CHAT_KEY,
        chat_type="group",
        platform_userid=sender_id,
        content_text=f"消息 {index}",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=send_timestamp,
    )


def _text(message: Any) -> str:
    content = message.to_dict()["content"]
    if isinstance(content, str):
        return content
    return "".join(segment.get("text", "") for segment in content or [] if isinstance(segment, dict))


@pytest.fixture(autouse=True)
def clean_round_state() -> Any:
    reset_round_recalls()
    reset_recall_cooldown()
    reset_narrative_suppression()
    try:
        yield
    finally:
        reset_round_recalls()
        reset_recall_cooldown()
        reset_narrative_suppression()


@pytest.fixture
async def history_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_ENHANCED_RETRIEVAL", False)
    monkeypatch.setattr(config, "MEMORY_RECALL_COOLDOWN_BYPASS_SIMILARITY", 0.8)
    monkeypatch.setattr(config, "MEMORY_CONTEXT_MAX_ITEMS", 3)
    monkeypatch.setattr(config, "MEMORY_CONTEXT_MAX_LENGTH", 1200)
    monkeypatch.setattr(config, "MEMORY_RECALL_COOLDOWN_ROUNDS", 0)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _install_recall(
    monkeypatch: pytest.MonkeyPatch,
    batches: List[List[RetrievedMemory]],
) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": 0, "compiled": []}

    async def fake_plan(_messages: List[Any]) -> MemoryRecallPlan:
        return MemoryRecallPlan(
            queries=[
                MemoryRecallQuerySpec(
                    query_text="他们之前聊过什么",
                    focus_text="他们之前聊过什么",
                    context_texts=["他们之前聊过什么"],
                ),
            ],
        )

    async def fake_retrieve(
        workspace_id: int,
        requests: List[Any],
        **_kwargs: Any,
    ) -> List[List[RetrievedMemory]]:
        del workspace_id, requests
        index = min(state["calls"], len(batches) - 1)
        state["calls"] += 1
        return [batches[index]]

    async def fake_compile(
        *,
        recall_query: Any,
        memories: List[RetrievedMemory],
        max_length: int,
        included: List[tuple[str, int, int]] | None = None,
    ) -> str:
        del recall_query, max_length
        state["compiled"].append([memory.target_id for memory in memories])
        if not memories:
            return ""
        lines = ["[相关记忆]"]
        for memory in memories:
            line = f"- {memory.content}"
            lines.append(line)
            if included is not None:
                included.append((memory.source_type, memory.target_id, len(line)))
        return "\n".join(lines)

    async def fake_scope(_channel: Any) -> MemoryScope:
        return MemoryScope(allowed_instance_keys=None, preset_id=PRESET_ID)

    monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)
    monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_retrieve)
    monkeypatch.setattr(retriever_module, "compile_memories_for_context", fake_compile)
    monkeypatch.setattr(instance_scope_module, "resolve_memory_scope", fake_scope)
    return state


def _spy_memory_context(monkeypatch: pytest.MonkeyPatch) -> List[dict[str, Any]]:
    calls: List[dict[str, Any]] = []
    original = history_module._inject_memory_context

    async def spy(*args: Any, **kwargs: Any) -> str:
        calls.append(dict(kwargs))
        return await original(*args, **kwargs)

    monkeypatch.setattr(history_module, "_inject_memory_context", spy)
    return calls


async def _empty_summary(_channel: Any, _one_time_code: str) -> str:
    return ""


async def _render(
    channel: Any,
    *,
    code: str,
    record_sta_timestamp: float,
    incremental: bool = False,
) -> str:
    return _text(
        await history_module.render_history_data(
            chat_key=CHAT_KEY,
            db_chat_channel=channel,
            one_time_code=code,
            config=config,
            model_group=MODEL_GROUP,
            record_sta_timestamp=record_sta_timestamp,
            inject_worldbook=not incremental,
            incremental=incremental,
        ),
    )


class TestIncrementalHistoryInjection:
    async def test_incremental_render_does_not_repeat_the_channel_summary(
        self,
        history_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        summary_calls: List[str] = []

        async def fake_summary(_channel: Any, one_time_code: str) -> str:
            summary_calls.append(one_time_code)
            return "## Channel Summary\n频道近况素材\n\n"

        monkeypatch.setattr(history_module, "_inject_channel_summary", fake_summary)
        _install_recall(monkeypatch, [[_memory(1)]])
        base = int(time.time())
        channel = _channel(base)
        await _message(1, sender_id="ua", sender_name="小满", send_timestamp=base + 30)
        await _message(2, sender_id="ub", sender_name="阿青", send_timestamp=base + 60)

        first = await _render(channel, code="code-incr", record_sta_timestamp=0)
        again = await _render(channel, code="code-incr", record_sta_timestamp=base + 45, incremental=True)

        assert "## Channel Summary" in first
        assert "频道近况素材" in first
        assert "## Channel Summary" not in again
        assert "频道近况素材" not in again
        assert summary_calls == ["code-incr"]

    async def test_incremental_without_a_human_message_skips_recall(
        self,
        history_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(history_module, "_inject_channel_summary", _empty_summary)
        memory_calls: List[dict[str, Any]] = []

        async def fake_memory(**_kwargs: Any) -> str:
            memory_calls.append(dict(_kwargs))
            return "## Recalled Memories\n不该出现的记忆块\n\n"

        monkeypatch.setattr(history_module, "_inject_memory_context", fake_memory)
        base = int(time.time())
        channel = _channel(base)
        await _message(1, sender_id="-1", sender_name="BOT", send_timestamp=base + 30)
        await _message(2, sender_id="-1", sender_name="SYSTEM", send_timestamp=base + 60)

        rendered = await _render(channel, code="code-bot", record_sta_timestamp=0, incremental=True)

        assert memory_calls == []
        assert "## Recalled Memories" not in rendered
        assert "[相关记忆]" not in rendered

    async def test_tool_results_and_self_sent_messages_are_not_human_either(
        self,
        history_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """这一轮沙盒方法的返回值落成工具结果行（`-2`），同账号其他程序发的是 `-3`：都不算有人说话"""
        monkeypatch.setattr(history_module, "_inject_channel_summary", _empty_summary)
        memory_calls: List[dict[str, Any]] = []

        async def fake_memory(**kwargs: Any) -> str:
            memory_calls.append(dict(kwargs))
            return "## Recalled Memories\n不该出现的记忆块\n\n"

        monkeypatch.setattr(history_module, "_inject_memory_context", fake_memory)
        base = int(time.time())
        channel = _channel(base)
        await _message(1, sender_id=TOOL_RESULT_SENDER_ID, sender_name=TOOL_RESULT_SENDER_NAME, send_timestamp=base + 30)
        await _message(2, sender_id=SELF_SENT_SENDER_ID, sender_name="同账号", send_timestamp=base + 60)

        rendered = await _render(channel, code="code-tool", record_sta_timestamp=0, incremental=True)
        assert memory_calls == []
        assert "## Recalled Memories" not in rendered

        await _message(3, sender_id="uc", sender_name="小满", send_timestamp=base + 90)
        await _render(channel, code="code-tool", record_sta_timestamp=0, incremental=True)
        assert len(memory_calls) == 1, "有人说话之后照常召回"

    async def test_incremental_injects_only_memories_not_yet_injected(
        self,
        history_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(history_module, "_inject_channel_summary", _empty_summary)
        _install_recall(monkeypatch, [[_memory(1)], [_memory(1), _memory(2)]])
        memory_calls = _spy_memory_context(monkeypatch)
        base = int(time.time())
        channel = _channel(base)
        await _message(1, sender_id="ua", sender_name="小满", send_timestamp=base + 30)
        await _message(2, sender_id="ub", sender_name="阿青", send_timestamp=base + 60)

        first = await _render(channel, code="code-new", record_sta_timestamp=0)
        again = await _render(channel, code="code-new", record_sta_timestamp=base + 45, incremental=True)

        assert "记忆内容 1" in first
        assert "记忆内容 1" not in again
        assert "记忆内容 2" in again
        assert memory_calls[0].get("exclude_keys") is None
        assert memory_calls[-1]["exclude_keys"] == frozenset({("paragraph", 1)})

    async def test_incremental_with_only_injected_memories_has_no_block(
        self,
        history_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(history_module, "_inject_channel_summary", _empty_summary)
        _install_recall(monkeypatch, [[_memory(1)], [_memory(1)]])
        base = int(time.time())
        channel = _channel(base)
        await _message(1, sender_id="ua", sender_name="小满", send_timestamp=base + 30)
        await _message(2, sender_id="ub", sender_name="阿青", send_timestamp=base + 60)

        await _render(channel, code="code-same", record_sta_timestamp=0)
        again = await _render(channel, code="code-same", record_sta_timestamp=base + 45, incremental=True)

        assert "## Recalled Memories" not in again
        assert "[相关记忆]" not in again

    @pytest.mark.parametrize("second_batch", ["all_injected", "nothing_found"])
    async def test_empty_incremental_does_not_overwrite_the_round_detail(
        self,
        history_db: None,
        monkeypatch: pytest.MonkeyPatch,
        second_batch: str,
    ) -> None:
        """增量这次没有可补的：明细里第一次渲染记下的「已注入」与字数不能被盖成空"""
        monkeypatch.setattr(history_module, "_inject_channel_summary", _empty_summary)
        batches = [[_memory(1)], [_memory(1)] if second_batch == "all_injected" else []]
        _install_recall(monkeypatch, batches)
        base = int(time.time())
        channel = _channel(base)
        await _message(1, sender_id="ua", sender_name="小满", send_timestamp=base + 30)
        await _message(2, sender_id="ub", sender_name="阿青", send_timestamp=base + 60)
        prompt_rounds.reset_prompt_rounds()
        prompt_rounds.begin("code-detail", channel)
        try:
            await _render(channel, code="code-detail", record_sta_timestamp=0)
            draft = prompt_rounds._rounds["code-detail"]  # noqa: SLF001
            first_chars = draft.memory_block_chars
            assert draft.memory_state == "injected"
            assert first_chars > 0

            await _render(channel, code="code-detail", record_sta_timestamp=base + 45, incremental=True)

            assert draft.memory_state == "injected"
            assert draft.memory_block_chars == first_chars
            assert set(draft.memory_items) == {("paragraph", 1)}
        finally:
            prompt_rounds.reset_prompt_rounds()

    async def test_non_incremental_renders_keep_the_same_batch(
        self,
        history_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(history_module, "_inject_channel_summary", _empty_summary)
        _install_recall(monkeypatch, [[_memory(1)]])
        memory_calls = _spy_memory_context(monkeypatch)
        base = int(time.time())
        channel = _channel(base)
        await _message(1, sender_id="ua", sender_name="小满", send_timestamp=base + 30)

        first = await _render(channel, code="code-twice", record_sta_timestamp=0)
        second = await _render(channel, code="code-twice", record_sta_timestamp=0)

        assert "记忆内容 1" in first
        assert "记忆内容 1" in second
        assert all(call.get("exclude_keys") is None for call in memory_calls)


class TestRoundRecallInjectedKeys:
    def test_note_and_read_round_trip(self) -> None:
        note_injected("code-a", [("paragraph", 1), ("relation", 2)])

        assert injected_keys("code-a") == frozenset({("paragraph", 1), ("relation", 2)})
        assert injected_keys("code-missing") == frozenset()

    def test_empty_token_is_not_recorded(self) -> None:
        note_injected("", [("paragraph", 1)])

        assert injected_keys("") == frozenset()

    def test_discard_clears_the_set(self) -> None:
        note_injected("code-b", [("paragraph", 1)])

        discard_round_recall("code-b")

        assert injected_keys("code-b") == frozenset()

    async def test_commit_clears_the_set(self) -> None:
        note_injected("code-c", [("paragraph", 1)])

        await commit_round_recall("code-c")

        assert injected_keys("code-c") == frozenset()

    def test_oldest_keys_are_dropped_over_the_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(round_recall_module, "_MAX_INJECTED_KEYS", 2)
        note_injected("code-d", [("paragraph", 1)])
        note_injected("code-d", [("paragraph", 2)])
        note_injected("code-d", [("paragraph", 3)])

        assert injected_keys("code-d") == frozenset({("paragraph", 2), ("paragraph", 3)})


class TestRunAgentIncrementalWiring:
    def test_iteration_history_renders_are_incremental(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(
            max_retry=3,
            parses=[_Parsed(), _Parsed(), _Parsed()],
            sandbox_results=[
                _error_result(SEND_TEXT_FAILURE),
                _error_result(DRAW_PLUGIN_FAILURE),
                _SandboxResult(output=f"TypeError: {STICKER_SIGNATURE_FAILURE}", stop_type=ExecStopType.ERROR),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.history_render_kwargs, "迭代没有发生，这条用例没测到东西"
        assert all(kwargs.get("incremental") is True for kwargs in harness.history_render_kwargs)
        assert all(kwargs.get("inject_worldbook") is False for kwargs in harness.history_render_kwargs)
