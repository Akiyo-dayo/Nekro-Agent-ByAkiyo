"""闸开时自动注入的排序与冷却旁路

验收时段落召回对题率低：排序按「衰减后权重 × 相似度」，几小时前的闲聊压过几天前真正相关的偏好，
相似度很低的尾巴也一并注入。这里钉住：
- 相似度优先，绝对下限 0.55 与相对截断（阈值来自实际部署的回放评测）；
- 查询仍是最近几句拼起来，寒暄不跳过（回放显示收窄查询、跳过寒暄都会丢掉对题的记忆）；
- 问句的冷却旁路低一档；
- 提示词明细记下每条的相似度；闸关路径不受影响。
"""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace
from typing import Any, List

import pytest

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.memory import instance_scope as instance_scope_module
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.recall_cooldown import reset_recall_cooldown
from nekro_agent.services.memory.recall_focus import (
    DOMAIN_MIN_SIMILARITY,
    QUESTION_BYPASS_SIMILARITY,
    cooldown_bypass_similarity,
    is_question,
    rank_for_injection,
)
from nekro_agent.services.memory.round_recall import reset_round_recalls
from tests.test_memory_recall_cooldown import _channel, _compile_ignoring_included, _memory

T0 = 1_700_000_000


def _msg(account: str, text: str, *, at: int) -> DBChatMessage:
    return DBChatMessage(
        sender_id=account,
        platform_userid=account,
        sender_nickname=account,
        content_text=text,
        send_timestamp=at,
        message_id=f"m{at}",
        ext_data="{}",
    )


class TestQuestion:
    @pytest.mark.parametrize(
        ("text", "question"),
        [
            ("我生日几号？", True),
            ("她喜欢啥", True),
            ("[@id:7000001;nickname:晴音@] 在吗", True),
            ("今天抽卡又歪了，气死", False),
            ("晴音！", False),
            ("等了几天才到货", False),
            ("来了几个人一起开黑", False),
            ("你今年几岁了", True),
            ("明天几点集合", True),
        ],
    )
    def test_is_question(self, text: str, question: bool) -> None:
        assert is_question(text) is question


class TestRanking:
    def test_relevant_old_memory_beats_recent_chatter(self) -> None:
        relevant_but_old = _memory(1, similarity=0.82, weight=0.05)
        recent_chatter = _memory(2, similarity=0.71, weight=1.8)
        assert [m.target_id for m in rank_for_injection([recent_chatter, relevant_but_old])] == [1, 2]

    def test_floor_and_relative_band(self) -> None:
        assert DOMAIN_MIN_SIMILARITY == 0.55
        ranked = rank_for_injection(
            [_memory(1, similarity=0.9), _memory(2, similarity=0.7), _memory(3, similarity=0.54)],
        )
        assert [m.target_id for m in ranked] == [1], "低于下限的不要；比最好那条低太多的尾巴也不要"

    def test_low_similarity_round_injects_nothing(self) -> None:
        assert rank_for_injection([_memory(1, similarity=0.53), _memory(2, similarity=0.51)]) == []

    def test_weight_breaks_near_ties(self) -> None:
        ranked = rank_for_injection([_memory(1, similarity=0.801, weight=0.1), _memory(2, similarity=0.80, weight=1.0)])
        assert [m.target_id for m in ranked] == [2, 1]

    def test_tie_means_within_the_tie_of_the_group_top(self) -> None:
        # 0.611 与 0.589 相差 0.022：不在 0.02 以内，按相似度排，不看权重
        ranked = rank_for_injection([_memory(1, similarity=0.589, weight=9.0), _memory(2, similarity=0.611, weight=0.1)])
        assert [m.target_id for m in ranked] == [2, 1]
        # 0.600 与 0.619 相差 0.019：同一组，权重大的在前（不论落在哪个四舍五入的档）
        ranked = rank_for_injection([_memory(1, similarity=0.619, weight=0.1), _memory(2, similarity=0.600, weight=9.0)])
        assert [m.target_id for m in ranked] == [2, 1]

    def test_question_lowers_the_cooldown_bypass(self) -> None:
        assert cooldown_bypass_similarity(0.8, question=True) == QUESTION_BYPASS_SIMILARITY
        assert cooldown_bypass_similarity(0.8, question=False) == 0.8
        assert cooldown_bypass_similarity(0.6, question=True) == 0.6, "不高于配置值"
        assert cooldown_bypass_similarity(1.0, question=True) == 1.0, "管理员设 1.0 关掉旁路时，问句也不放宽"


class TestInjection:
    @pytest.fixture(autouse=True)
    def gate_on(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        reset_recall_cooldown()
        reset_round_recalls()
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        monkeypatch.setattr(config, "MEMORY_ENABLE_ENHANCED_RETRIEVAL", False)
        monkeypatch.setattr(config, "MEMORY_RECALL_COOLDOWN_BYPASS_SIMILARITY", 0.8)

        async def fake_scope(_channel: Any) -> MemoryScope:
            return MemoryScope(allowed_instance_keys=None, preset_id=7)

        monkeypatch.setattr(instance_scope_module, "resolve_memory_scope", fake_scope)
        yield
        reset_recall_cooldown()
        reset_round_recalls()

    def _install(self, monkeypatch: pytest.MonkeyPatch, memories: List[Any]) -> dict[str, Any]:
        state: dict[str, Any] = {"queries": [], "compiled": []}

        async def fake_retrieve(workspace_id: int, requests: List[Any], **_kwargs: Any) -> List[List[Any]]:
            del workspace_id
            state["queries"].extend(request.query for request in requests)
            return [list(memories) for _ in requests]

        async def fake_compile(*, recall_query: Any, memories: List[Any], max_length: int) -> str:
            del recall_query, max_length
            state["compiled"].append([memory.target_id for memory in memories])
            return "\n".join(memory.content for memory in memories)

        monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_retrieve)
        monkeypatch.setattr(retriever_module, "compile_memories_for_context", partial(_compile_ignoring_included, fake_compile))
        return state

    async def test_greeting_still_searches_with_the_recent_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = self._install(monkeypatch, [_memory(1, similarity=0.9)])
        text = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[_msg("u1", "今天抽卡又歪了", at=T0), _msg("u2", "晴音！", at=T0 + 3)],
            channel=_channel(cooldown_rounds=0),
            one_time_code="focus-greet",
        )
        assert "记忆内容 1" in text
        assert len(state["queries"]) == 1 and "今天抽卡又歪了" in state["queries"][0], "前几句的上下文照样进查询"

    async def test_below_floor_is_not_injected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        state = self._install(monkeypatch, [_memory(1, similarity=0.9), _memory(2, similarity=0.52)])
        await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[_msg("u1", "我上次说的那家店叫什么来着？", at=T0)],
            channel=_channel(cooldown_rounds=0),
            one_time_code="focus-floor",
        )
        assert state["compiled"] == [[1]]

    async def test_gate_off_keeps_weight_ranking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        state = self._install(monkeypatch, [_memory(1, similarity=0.9, weight=0.1), _memory(2, similarity=0.52, weight=1.0)])
        await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=7,
            recent_messages=[_msg("u1", "我上次说的那家店叫什么来着？", at=T0)],
            channel=_channel(cooldown_rounds=0),
            one_time_code="focus-gate-off",
        )
        assert state["compiled"] == [[2, 1]], "闸关仍按权重排、不设下限"

    async def test_scores_are_recorded_in_the_round(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, [_memory(1, similarity=0.9), _memory(2, similarity=0.85)])

        async def compile_with_included(
            *,
            recall_query: Any,
            memories: List[Any],
            max_length: int,
            included: Any = None,
        ) -> str:
            del recall_query, max_length
            lines = [memory.content for memory in memories]
            if included is not None:
                included.extend((memory.source_type, memory.target_id, len(line)) for memory, line in zip(memories, lines))
            return "\n".join(lines)

        monkeypatch.setattr(retriever_module, "compile_memories_for_context", compile_with_included)
        channel = _channel(cooldown_rounds=0)
        channel.chat_key = "onebot_v11-focus-group_1"
        prompt_rounds.begin("focus-score", channel)
        try:
            await history_module._inject_memory_context(  # noqa: SLF001
                workspace_id=None,
                recent_messages=[_msg("u1", "我上次说的那家店叫什么来着？", at=T0)],
                channel=channel,
                one_time_code="focus-score",
            )
            draft = prompt_rounds._rounds["focus-score"]  # noqa: SLF001
        finally:
            prompt_rounds._rounds.pop("focus-score", None)  # noqa: SLF001
        assert {(item["id"], item.get("score")) for item in draft.memory_items_json()} == {(1, 0.9), (2, 0.85)}


def test_scores_absent_without_them() -> None:
    channel = SimpleNamespace(chat_key="c", adapter_key="a", instance_key="i")
    prompt_rounds.begin("no-score", channel)  # type: ignore[arg-type]
    try:
        prompt_rounds.note_memory("no-score", state="injected", items=[("paragraph", 3, 10)])
        items = prompt_rounds._rounds["no-score"].memory_items_json()  # noqa: SLF001
    finally:
        prompt_rounds._rounds.pop("no-score", None)  # noqa: SLF001
    assert items == [{"type": "paragraph", "id": 3, "chars": 10, "renders": 1}], "旧形状不多出键"
