"""召回冷却：同一段记忆注入后 N 轮内不再自动注入。

冷却只在闸开时生效，且只作用于自动注入（`history._inject_memory_context`）；
`memory_tools` 的主动搜索不受影响。同一轮的多次渲染必须注入同一批，所以轮次按
`one_time_code` 计，而不是按渲染次数计。
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.affection.suppression import reset_narrative_suppression
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.memory import instance_scope as instance_scope_module
from nekro_agent.services.memory import recall_cooldown as cooldown_module
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.recall_contract import MemoryRecallPlan, MemoryRecallQuerySpec
from nekro_agent.services.memory.recall_cooldown import (
    filter_cooled,
    mark_injected,
    reset_recall_cooldown,
)
from nekro_agent.services.memory.retriever import RetrievedMemory
from nekro_agent.services.memory.round_recall import reset_round_recalls

CHAT_KEY = "onebot_v11-cooldown-group_1"
PRESET_ID = 7


def _memory(pid: int, *, similarity: float = 0.5, weight: float = 1.0) -> RetrievedMemory:
    return RetrievedMemory(
        target_id=pid,
        source_type="paragraph",
        episode_id=None,
        paragraph_id=pid,
        relation_id=None,
        content=f"记忆内容 {pid}",
        summary=f"摘要 {pid}",
        cognitive_type="episodic",
        knowledge_type="conversation",
        similarity_score=similarity,
        effective_weight=weight,
        event_time=None,
        origin_chat_key=CHAT_KEY,
    )


def _channel(*, cooldown_rounds: int, max_items: int = 3) -> Any:
    class _Channel:
        chat_key = CHAT_KEY
        workspace_id = None
        adapter_key = "onebot_v11"
        instance_key = "cooldown"
        channel_type = "group"

        async def get_effective_config(self) -> SimpleNamespace:
            return SimpleNamespace(
                MEMORY_ENABLE_FOR_CHAT=True,
                MEMORY_CONTEXT_MAX_ITEMS=max_items,
                MEMORY_CONTEXT_MAX_LENGTH=1200,
                MEMORY_RECALL_COOLDOWN_ROUNDS=cooldown_rounds,
            )

    return _Channel()


async def _compile_ignoring_included(
    fake: Any,
    *,
    recall_query: Any,
    memories: List[RetrievedMemory],
    max_length: int,
    included: Any = None,
) -> str:
    """登记替身时吞掉新增的 included 关键字，替身本身仍只关心渲染文字。"""
    del included
    return await fake(recall_query=recall_query, memories=memories, max_length=max_length)


@pytest.fixture(autouse=True)
def clean_process_state() -> Any:
    reset_recall_cooldown()
    reset_round_recalls()
    reset_narrative_suppression()
    try:
        yield
    finally:
        reset_recall_cooldown()
        reset_round_recalls()
        reset_narrative_suppression()


class TestCooldownUnit:
    def test_same_round_is_not_cooled(self) -> None:
        key = (CHAT_KEY, PRESET_ID)
        memory = _memory(1)
        mark_injected(key, "code-0", [memory])

        kept = filter_cooled(key, "code-0", [memory], rounds=3, bypass_similarity=0.8)

        assert kept == [memory]

    def test_next_round_is_cooled_and_expires_after_n(self) -> None:
        key = (CHAT_KEY, PRESET_ID)
        memory = _memory(1)
        mark_injected(key, "code-0", [memory])

        assert filter_cooled(key, "code-1", [memory], rounds=3, bypass_similarity=0.8) == []
        assert filter_cooled(key, "code-2", [memory], rounds=3, bypass_similarity=0.8) == []
        assert filter_cooled(key, "code-3", [memory], rounds=3, bypass_similarity=0.8) == []
        assert filter_cooled(key, "code-4", [memory], rounds=3, bypass_similarity=0.8) == [memory]

    def test_zero_rounds_never_cools(self) -> None:
        key = (CHAT_KEY, PRESET_ID)
        memory = _memory(1)
        mark_injected(key, "code-0", [memory])

        kept = filter_cooled(key, "code-1", [memory], rounds=0, bypass_similarity=0.8)

        assert kept == [memory]

    def test_high_similarity_bypasses_the_cooldown(self) -> None:
        key = (CHAT_KEY, PRESET_ID)
        memory = _memory(1, similarity=0.8)
        mark_injected(key, "code-0", [memory])

        kept = filter_cooled(key, "code-1", [memory], rounds=3, bypass_similarity=0.8)

        assert kept == [memory]

    def test_key_limit_drops_the_oldest_half(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cooldown_module, "MAX_KEYS", 2)
        memory = _memory(1)
        mark_injected(("chat-a", PRESET_ID), "code-0", [memory])
        mark_injected(("chat-b", PRESET_ID), "code-0", [memory])
        mark_injected(("chat-c", PRESET_ID), "code-0", [memory])

        # 丢的是最早的一半：chat-a 的记录没了，所以不再冷却；chat-b / chat-c 还在。
        # 先查还在的两个键，最后再查被淘汰的键：查一个没见过的键本身也会占一个位置。
        assert filter_cooled(("chat-b", PRESET_ID), "code-1", [memory], rounds=3, bypass_similarity=0.8) == []
        assert filter_cooled(("chat-c", PRESET_ID), "code-1", [memory], rounds=3, bypass_similarity=0.8) == []
        assert filter_cooled(("chat-a", PRESET_ID), "code-1", [memory], rounds=3, bypass_similarity=0.8) == [memory]

    def test_memory_limit_drops_the_oldest_half(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cooldown_module, "MAX_MEMORIES_PER_KEY", 2)
        key = (CHAT_KEY, PRESET_ID)
        first, second, third = _memory(1), _memory(2), _memory(3)
        mark_injected(key, "code-0", [first])
        mark_injected(key, "code-1", [second])
        mark_injected(key, "code-2", [third])

        # 最早的一条记录被丢掉，不再冷却；另外两条仍在窗口里。
        assert filter_cooled(key, "code-3", [first], rounds=3, bypass_similarity=0.8) == [first]
        assert filter_cooled(key, "code-3", [second], rounds=3, bypass_similarity=0.8) == []
        assert filter_cooled(key, "code-3", [third], rounds=3, bypass_similarity=0.8) == []


class TestInjectionCooldown:
    @pytest.fixture(autouse=True)
    def gate_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        monkeypatch.setattr(config, "MEMORY_RECALL_COOLDOWN_BYPASS_SIMILARITY", 0.8)
        monkeypatch.setattr(config, "MEMORY_ENABLE_ENHANCED_RETRIEVAL", False)

        async def fake_scope(_channel: Any) -> MemoryScope:
            return MemoryScope(allowed_instance_keys=None, preset_id=PRESET_ID)

        monkeypatch.setattr(instance_scope_module, "resolve_memory_scope", fake_scope)

    def _install_retrieval(
        self,
        monkeypatch: pytest.MonkeyPatch,
        batches: List[List[RetrievedMemory]],
    ) -> dict[str, Any]:
        state: dict[str, Any] = {"limits": [], "compiled": []}
        calls = {"index": 0}

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
            del workspace_id
            state["limits"] = [request.limit for request in requests]
            index = min(calls["index"], len(batches) - 1)
            calls["index"] += 1
            return [batches[index]]

        async def fake_compile(*, recall_query: Any, memories: List[RetrievedMemory], max_length: int) -> str:
            del recall_query, max_length
            state["compiled"].append([memory.target_id for memory in memories])
            if not memories:
                return ""
            return "\n".join(memory.content for memory in memories)

        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)
        monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_retrieve)
        monkeypatch.setattr(
            retriever_module,
            "compile_memories_for_context",
            partial(_compile_ignoring_included, fake_compile),
        )
        return state

    async def test_cooldown_skips_n_rounds_then_returns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        channel = _channel(cooldown_rounds=3)
        state = self._install_retrieval(monkeypatch, [[_memory(1)]])
        codes = ["code-0", "code-1", "code-2", "code-3", "code-4"]

        rendered = [
            await history_module._inject_memory_context(  # noqa: SLF001
                workspace_id=None,
                recent_messages=[],
                channel=channel,
                one_time_code=code,
            )
            for code in codes
        ]

        assert rendered[0] != "" and "记忆内容 1" in rendered[0]
        assert rendered[1] == ""
        assert rendered[2] == ""
        assert rendered[3] == ""
        assert rendered[4] != "" and "记忆内容 1" in rendered[4]
        # 冷却掉的轮次在编排之前就返回了，只有真正注入的两轮会走到编排。
        assert state["compiled"] == [[1], [1]]
        # 冷却开着时候选取两倍，冷却后再截回配置条数。
        assert state["limits"] == [6]

    async def test_same_round_renders_the_same_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        channel = _channel(cooldown_rounds=3)
        self._install_retrieval(monkeypatch, [[_memory(1)]])

        first = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code="code-same",
        )
        second = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code="code-same",
        )

        assert "记忆内容 1" in first
        assert "记忆内容 1" in second

    async def test_cooled_slots_are_refilled_by_later_candidates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        channel = _channel(cooldown_rounds=3, max_items=3)
        first_round = [_memory(pid, weight=float(10 - pid)) for pid in range(1, 7)]
        state = self._install_retrieval(monkeypatch, [first_round])

        first = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code="code-0",
        )
        second = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code="code-1",
        )

        assert first != ""
        # 第一轮取前三条（权重最高的 1/2/3）；第二轮这三条都在冷却里，由 4/5/6 补满。
        assert state["compiled"] == [[1, 2, 3], [4, 5, 6]]
        assert "记忆内容 4" in second and "记忆内容 6" in second
        assert "记忆内容 1" not in second

    async def test_high_similarity_is_injected_during_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        channel = _channel(cooldown_rounds=3, max_items=2)
        batch = [_memory(1, similarity=0.9, weight=2.0), _memory(2, similarity=0.3, weight=1.0)]
        state = self._install_retrieval(monkeypatch, [batch])

        await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code="code-0",
        )
        second = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code="code-1",
        )

        assert state["compiled"][-1] == [1]
        assert "记忆内容 1" in second

    async def test_zero_rounds_disables_the_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        channel = _channel(cooldown_rounds=0)
        state = self._install_retrieval(monkeypatch, [[_memory(1)]])

        for code in ("code-0", "code-1", "code-2"):
            rendered = await history_module._inject_memory_context(  # noqa: SLF001
                workspace_id=None,
                recent_messages=[],
                channel=channel,
                one_time_code=code,
            )
            assert "记忆内容 1" in rendered
        # 关掉冷却时检索条数与改造前相同，不取两倍。
        assert state["limits"] == [3]
        assert state["compiled"] == [[1], [1], [1]]


class TestMemoryToolsUnaffected:
    def test_memory_tools_does_not_use_the_cooldown(self) -> None:
        source = (Path(__file__).resolve().parent.parent / "plugins" / "builtin" / "memory_tools" / "main.py").read_text(
            encoding="utf-8",
        )

        assert "recall_cooldown" not in source
        assert "filter_cooled" not in source


class TestGateOffDoesNotTouchCooldown:
    async def test_gate_off_never_calls_filter_cooled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        monkeypatch.setattr(config, "MEMORY_ENABLE_ENHANCED_RETRIEVAL", False)

        def explode(*_args: Any, **_kwargs: Any) -> List[RetrievedMemory]:
            raise AssertionError("闸关时不应该调用召回冷却")

        monkeypatch.setattr(cooldown_module, "filter_cooled", explode)

        async def fake_plan(_messages: List[Any]) -> MemoryRecallPlan:
            return MemoryRecallPlan(
                queries=[MemoryRecallQuerySpec(query_text="之前聊过什么", focus_text="之前聊过什么")],
            )

        async def fake_retrieve(
            workspace_id: int,
            requests: List[Any],
            **_kwargs: Any,
        ) -> List[List[RetrievedMemory]]:
            del workspace_id
            return [[_memory(1)] for _ in requests]

        async def fake_compile(*, recall_query: Any, memories: List[RetrievedMemory], max_length: int) -> str:
            del recall_query, max_length
            return "\n".join(memory.content for memory in memories)

        async def fake_scope(_channel: Any) -> MemoryScope:
            return MemoryScope.unrestricted()

        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)
        monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_retrieve)
        monkeypatch.setattr(
            retriever_module,
            "compile_memories_for_context",
            partial(_compile_ignoring_included, fake_compile),
        )
        monkeypatch.setattr(instance_scope_module, "resolve_memory_scope", fake_scope)

        rendered = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=9401,
            recent_messages=[],
            channel=_channel(cooldown_rounds=3),
            one_time_code="code-gate-off",
        )

        assert "记忆内容 1" in rendered
