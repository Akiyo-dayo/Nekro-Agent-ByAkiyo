"""记忆强化挪到主对话成功返回之后。

注入链路只把「这一轮真正注入了哪些记忆」记进 `round_recall`，不写库；`run_agent` 正常
收尾时一次性补记强化，被拦 / 超时 / 无进展的轮次直接丢弃。这样一轮主对话迭代几次也只算
一次，被拦下的轮次不强化，被拦之后的下一轮也不注入记忆块。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_reinforcement_log import DBMemReinforcementLog
from nekro_agent.services.affection.suppression import (
    lift_narrative_suppression,
    reset_narrative_suppression,
    suppress_narrative,
)
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.model_capability import LLMFailureKind
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.memory import instance_scope as instance_scope_module
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.recall_contract import MemoryRecallPlan, MemoryRecallQuerySpec
from nekro_agent.services.memory.retriever import (
    BatchRecallRequest,
    RetrievedMemory,
    retrieve_memories_batch,
)
from nekro_agent.services.memory.round_recall import (
    commit_round_recall,
    discard_round_recall,
    reset_round_recalls,
    stage_round_recall,
)

WORKSPACE_ID = 4302
MEMORY_MODELS = [
    "nekro_agent.models.db_mem_paragraph",
    "nekro_agent.models.db_mem_entity",
    "nekro_agent.models.db_mem_relation",
    "nekro_agent.models.db_mem_episode",
    "nekro_agent.models.db_mem_reinforcement_log",
    "nekro_agent.models.db_instance_group",
]

# 查询文本对应一个可识别的查询向量，向量通道替身按它命中段落。
_QUERY_MARKER = {"测试查询": 1.0}


class _StubChannel:
    """`_inject_memory_context` 只需要频道提供域与人设这两样。"""

    def __init__(self, instance_key: str, *, adapter_key: str = "onebot_v11", preset_id: int = 0) -> None:
        self.adapter_key = adapter_key
        self.instance_key = instance_key
        self._preset_id = preset_id

    async def get_effective_preset_id(self) -> int:
        return self._preset_id


class _ChatKeyChannel(_StubChannel):
    """带 `chat_key` 的替身，用来验证记忆块也读抑制标记。"""

    def __init__(self, chat_key: str, instance_key: str = "") -> None:
        super().__init__(instance_key)
        self.chat_key = chat_key


class _ScriptedQdrant:
    """按段落 ID 返回固定相似度的向量通道替身。"""

    def __init__(self, scores: dict[int, float]) -> None:
        self.scores = scores

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        threshold = float(kwargs.get("score_threshold", 0.0))
        return [
            {"id": paragraph_id, "score": score, "payload": {}}
            for paragraph_id, score in sorted(self.scores.items(), key=lambda item: -item[1])
            if score >= threshold
        ]


def _install_memory_fakes(
    monkeypatch: pytest.MonkeyPatch,
    scores: dict[int, float],
) -> None:
    async def fake_embed(text: str) -> list[float]:
        return [_QUERY_MARKER.get(text, 0.0), 0.0, 0.0, 0.0]

    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.5)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_REINFORCE_BOOST", 0.3)
    monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
    monkeypatch.setattr(retriever_module, "memory_qdrant_manager", _ScriptedQdrant(scores))


async def _create_paragraph(content: str) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=WORKSPACE_ID,
        instance_key="",
        memory_source="cc",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content=content,
        summary=content,
        half_life_seconds=30 * 24 * 3600,
        origin_kind=OriginKind.TASK,
        origin_chat_key="__test__",
    )


def _retrieved(target_id: int, content: str) -> RetrievedMemory:
    return RetrievedMemory(
        target_id=target_id,
        source_type="paragraph",
        episode_id=None,
        paragraph_id=target_id,
        relation_id=None,
        content=content,
        summary="",
        cognitive_type=CognitiveType.SEMANTIC.value,
        knowledge_type=KnowledgeType.FACT.value,
        similarity_score=0.9,
        effective_weight=1.0,
        event_time=None,
        origin_chat_key="__test__",
    )


def _fake_plan() -> Any:
    async def plan(_messages: Any) -> MemoryRecallPlan:
        return MemoryRecallPlan(queries=[MemoryRecallQuerySpec(query_text="测试查询")])

    return plan


@pytest.fixture
async def memory_db():
    """真表跑强化写库：要断言的正是「库里被写了几次」。"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": MEMORY_MODELS})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def _clean_round_state():
    reset_round_recalls()
    reset_narrative_suppression()
    yield
    reset_round_recalls()
    reset_narrative_suppression()


class TestStageDoesNotWrite:
    @pytest.mark.asyncio
    async def test_two_injections_stage_but_do_not_reinforce(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = await _create_paragraph("甲的记忆内容")
        second = await _create_paragraph("乙的记忆内容")
        _install_memory_fakes(monkeypatch, {first.id: 0.9, second.id: 0.85})
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", _fake_plan())

        context = ""
        for _ in range(2):
            context = await history_module._inject_memory_context(
                workspace_id=WORKSPACE_ID,
                recent_messages=[],
                channel=_StubChannel(""),
                one_time_code="round-one",
            )

        assert "甲的记忆内容" in context
        assert "乙的记忆内容" in context
        refreshed_first = await DBMemParagraph.get(id=first.id)
        refreshed_second = await DBMemParagraph.get(id=second.id)
        assert refreshed_first.base_weight == 1.0
        assert refreshed_second.base_weight == 1.0
        assert await DBMemReinforcementLog.all().count() == 0

    @pytest.mark.asyncio
    async def test_commit_reinforces_each_paragraph_once(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = await _create_paragraph("甲的记忆内容")
        second = await _create_paragraph("乙的记忆内容")
        _install_memory_fakes(monkeypatch, {first.id: 0.9, second.id: 0.85})
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", _fake_plan())

        for _ in range(2):
            await history_module._inject_memory_context(
                workspace_id=WORKSPACE_ID,
                recent_messages=[],
                channel=_StubChannel(""),
                one_time_code="round-one",
            )

        committed = await commit_round_recall("round-one")

        assert committed == 2
        assert (await DBMemParagraph.get(id=first.id)).base_weight == pytest.approx(1.3)
        assert (await DBMemParagraph.get(id=second.id)).base_weight == pytest.approx(1.3)
        assert await DBMemReinforcementLog.filter(target_id=first.id).count() == 1
        assert await DBMemReinforcementLog.filter(target_id=second.id).count() == 1

        assert await commit_round_recall("round-one") == 0
        assert (await DBMemParagraph.get(id=first.id)).base_weight == pytest.approx(1.3)
        assert await DBMemReinforcementLog.filter(target_id=first.id).count() == 1

    @pytest.mark.asyncio
    async def test_discard_drops_the_staged_round(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paragraph = await _create_paragraph("被丢弃的记忆内容")
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9})
        stage_round_recall(
            "round-two",
            workspace_id=WORKSPACE_ID,
            scope=MemoryScope.unrestricted(),
            query="测试查询",
            memories=[_retrieved(paragraph.id, "被丢弃的记忆内容")],
        )

        discard_round_recall("round-two")

        assert await commit_round_recall("round-two") == 0
        assert (await DBMemParagraph.get(id=paragraph.id)).base_weight == 1.0
        assert await DBMemReinforcementLog.all().count() == 0


class TestBatchEntryPoint:
    @pytest.mark.asyncio
    async def test_record_access_false_skips_writing_and_default_still_writes(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paragraph = await _create_paragraph("批量入口的记忆内容")
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9})
        requests = [BatchRecallRequest(query="测试查询", limit=5)]

        without_write = await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=requests,
            scope=MemoryScope.unrestricted(),
            record_access=False,
        )

        assert [memory.target_id for memory in without_write[0]] == [paragraph.id]
        assert (await DBMemParagraph.get(id=paragraph.id)).base_weight == 1.0
        assert await DBMemReinforcementLog.all().count() == 0

        await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=requests,
            scope=MemoryScope.unrestricted(),
        )

        assert (await DBMemParagraph.get(id=paragraph.id)).base_weight == pytest.approx(1.3)
        assert await DBMemReinforcementLog.filter(target_id=paragraph.id).count() == 1


class TestRunAgentWiresRoundRecall:
    CHAT_KEY = "onebot_v11-ob10002-group_32"

    async def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        outcome: BaseException | None,
    ) -> tuple[list[str], str]:
        committed: list[str] = []
        seen: dict[str, str] = {}

        async def fake_round(**kwargs: Any) -> None:
            code = kwargs["one_time_code"]
            seen["code"] = code
            stage_round_recall(
                code,
                workspace_id=WORKSPACE_ID,
                scope=MemoryScope.unrestricted(),
                query="测试查询",
                memories=[_retrieved(1, "本轮注入的候选")],
            )
            if outcome is not None:
                raise outcome

        async def fake_commit(one_time_code: str) -> int:
            committed.append(one_time_code)
            return 1

        monkeypatch.setattr(run_agent_module, "_run_agent_in_instance_scope", fake_round)
        monkeypatch.setattr(run_agent_module, "commit_round_recall", fake_commit)
        ctx = SimpleNamespace(db_chat_channel=SimpleNamespace(chat_key=self.CHAT_KEY, instance_key="ob10002"))

        if outcome is None:
            await run_agent_module.run_agent(self.CHAT_KEY, ctx=ctx)  # type: ignore[arg-type]
        else:
            with pytest.raises(type(outcome)) as raised:
                await run_agent_module.run_agent(self.CHAT_KEY, ctx=ctx)  # type: ignore[arg-type]
            assert raised.value is outcome
        return committed, seen["code"]

    @pytest.mark.asyncio
    async def test_normal_round_commits_once_with_the_round_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        committed, code = await self._run(monkeypatch, None)

        assert committed == [code]
        # 补记是假的，记录被收尾的 `finally` 丢掉，真函数这里拿不到东西
        assert await commit_round_recall(code) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            run_agent_module.AllLLMRequestsFailedError(kinds=[LLMFailureKind.CONTENT_BLOCKED]),
            run_agent_module.AgentProviderRefusalError(marker="m"),
            run_agent_module.AllLLMRequestsFailedError(kinds=[LLMFailureKind.EMPTY_RESPONSE, LLMFailureKind.OTHER]),
            run_agent_module.AgentRoundUnfinishedError("x"),
        ],
        ids=["content-blocked", "provider-refusal", "not-blocked-llm-failure", "unfinished"],
    )
    async def test_failed_round_never_commits_and_discards_the_record(
        self,
        monkeypatch: pytest.MonkeyPatch,
        error: BaseException,
    ) -> None:
        committed, code = await self._run(monkeypatch, error)

        assert committed == []
        assert await commit_round_recall(code) == 0


class TestSuppressedRoundSkipsMemoryBlock:
    CHAT_KEY = "onebot_v11-ob10003-group_33"

    async def _call(
        self,
        monkeypatch: pytest.MonkeyPatch,
        channel: Any,
        calls: dict[str, int],
    ) -> str:
        async def fake_plan(_messages: Any) -> MemoryRecallPlan:
            calls["plan"] += 1
            return MemoryRecallPlan(queries=[MemoryRecallQuerySpec(query_text="测试查询")])

        async def fake_batch(
            workspace_id: int,
            requests: Any,
            *,
            scope: Any = None,
            record_access: bool = True,
        ) -> list[list[Any]]:
            calls["batch"] += 1
            return [[] for _ in requests]

        async def fake_scope(_channel: Any) -> MemoryScope:
            return MemoryScope.unrestricted()

        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", 1800)
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)
        monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_batch)
        monkeypatch.setattr(instance_scope_module, "resolve_memory_scope", fake_scope)

        return await history_module._inject_memory_context(
            workspace_id=WORKSPACE_ID,
            recent_messages=[],
            channel=channel,
            one_time_code="round-three",
        )

    @pytest.mark.asyncio
    async def test_suppressed_channel_skips_plan_and_retrieval(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = {"plan": 0, "batch": 0}
        suppress_narrative(self.CHAT_KEY)

        result = await self._call(monkeypatch, _ChatKeyChannel(self.CHAT_KEY), calls)

        assert result == ""
        assert calls == {"plan": 0, "batch": 0}

        lift_narrative_suppression(self.CHAT_KEY)
        await self._call(monkeypatch, _ChatKeyChannel(self.CHAT_KEY), calls)

        assert calls == {"plan": 1, "batch": 1}

    @pytest.mark.asyncio
    async def test_channel_without_chat_key_fails_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = {"plan": 0, "batch": 0}
        suppress_narrative(self.CHAT_KEY)

        await self._call(monkeypatch, _StubChannel(""), calls)

        assert calls == {"plan": 1, "batch": 1}
