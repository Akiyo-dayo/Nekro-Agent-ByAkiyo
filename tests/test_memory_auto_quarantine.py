"""被拦轮次的自动隔离：计数、阈值、保护与召回出口。

自动隔离只处理被拦轮次里真正注入过的段落与 episode；成功轮次不计数，关系与实体
只在明细里显示。隔离复用失活字段并删掉向量，下一轮召回自然看不到它。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import AsyncIterator, Sequence

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.affection.suppression import reset_narrative_suppression
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.recall_cooldown import reset_recall_cooldown
from nekro_agent.services.memory.retriever import retrieve_memories
from nekro_agent.services.memory.round_recall import reset_round_recalls

ADAPTER = "onebot_v11"
INSTANCE = "autoq"
CHAT_KEY = "onebot_v11-autoq-group_1"
VECTOR = [0.1, 0.2, 0.3, 0.4]


class _Channel:
    def __init__(self, *, chat_key: str = CHAT_KEY) -> None:
        self.chat_key = chat_key
        self.adapter_key = ADAPTER
        self.instance_key = INSTANCE
        self.workspace_id = 0

    async def get_effective_config(self) -> SimpleNamespace:
        return SimpleNamespace(
            MEMORY_ENABLE_FOR_CHAT=True,
            MEMORY_CONTEXT_MAX_ITEMS=8,
            MEMORY_CONTEXT_MAX_LENGTH=1200,
            MEMORY_RECALL_COOLDOWN_ROUNDS=0,
            MEMORY_CHANNEL_SUMMARY_ENABLED=True,
            MEMORY_CHANNEL_SUMMARY_MAX_CHARS=400,
        )

    async def get_effective_preset_id(self) -> int:
        return 0


@pytest.fixture
async def autoq_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncQdrantClient]:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_PROMPT_ROUNDS_KEEP", 20)
    monkeypatch.setattr(config, "MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS", 3)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_DEFAULT_LIMIT", 5)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.0)

    async def fake_embed(_text: str, **_kwargs: object) -> list[float]:
        return VECTOR

    async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [VECTOR for _ in texts]

    monkeypatch.setattr(embedding_service_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_batch", fake_embed_batch)
    monkeypatch.setattr(qdrant_manager_module, "embed_batch", fake_embed_batch)
    monkeypatch.setattr(retriever_module, "embed_text", fake_embed)

    client = AsyncQdrantClient(location=":memory:")

    async def fake_get_client() -> AsyncQdrantClient:
        return client

    monkeypatch.setattr(qdrant_manager_module, "get_qdrant_client", fake_get_client)
    await qdrant_manager_module.memory_qdrant_manager.ensure_collection(4)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    prompt_rounds.reset_prompt_rounds()
    reset_round_recalls()
    reset_recall_cooldown()
    reset_narrative_suppression()
    try:
        yield client
    finally:
        await prompt_rounds.drain()
        prompt_rounds.reset_prompt_rounds()
        reset_round_recalls()
        reset_recall_cooldown()
        reset_narrative_suppression()
        await client.close()
        await Tortoise.close_connections()


async def _create_paragraph(*, is_protected: bool = False, content: str = "可能触发拦截的段落") -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=0,
        instance_key=INSTANCE,
        preset_id=0,
        adapter_key=ADAPTER,
        origin_chat_key=CHAT_KEY,
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.CONVERSATION,
        origin_kind=OriginKind.MESSAGE,
        content=content,
        summary="摘要",
        is_protected=is_protected,
    )


async def _seed_vector(client: AsyncQdrantClient, paragraph_id: int) -> None:
    await client.upsert(
        collection_name=qdrant_manager_module.MEMORY_PARAGRAPH_COLLECTION,
        points=[
            qdrant_models.PointStruct(
                id=paragraph_id,
                vector=VECTOR,
                payload={
                    "workspace_id": 0,
                    "instance_key": INSTANCE,
                    "adapter_key": ADAPTER,
                    "preset_id": 0,
                    "is_inactive": False,
                    "cognitive_type": "episodic",
                    "knowledge_type": "conversation",
                    "origin_chat_key": CHAT_KEY,
                    "person_accounts": [],
                },
            ),
        ],
    )


async def _run_round(
    code: str,
    items: Sequence[tuple[str, int, int]],
    *,
    outcome: str = "blocked",
) -> None:
    prompt_rounds.begin(code, _Channel())
    prompt_rounds.note_memory(code, state="injected", items=items, block_chars=10)
    prompt_rounds.finish(code, outcome)
    await prompt_rounds.drain()


class TestAutoQuarantine:
    async def test_third_blocked_round_quarantines_paragraph(self, autoq_env: AsyncQdrantClient) -> None:
        paragraph = await _create_paragraph()
        await _seed_vector(autoq_env, paragraph.id)
        items = [("paragraph", paragraph.id, 10)]

        await _run_round("block-1", items)
        first = await DBMemParagraph.get(id=paragraph.id)
        assert first.blocked_round_hits == 1
        assert first.is_inactive is False
        assert first.quarantined_at is None

        await _run_round("block-2", items)
        second = await DBMemParagraph.get(id=paragraph.id)
        assert second.blocked_round_hits == 2
        assert second.is_inactive is False

        await _run_round("block-3", items)
        third = await DBMemParagraph.get(id=paragraph.id)
        assert third.blocked_round_hits == 3
        assert third.is_inactive is True
        assert third.quarantined_at is not None
        assert third.quarantine_reason == "auto_blocked"
        points = await autoq_env.retrieve(
            collection_name=qdrant_manager_module.MEMORY_PARAGRAPH_COLLECTION,
            ids=[paragraph.id],
            with_payload=False,
        )
        assert points == []

    async def test_count_and_quarantine_commit_together(
        self,
        autoq_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 第三次被拦时转隔离那一步失败：计数也必须一起回滚，不能停在阈值上而记忆照常注入。
        from nekro_agent.services.memory import admin_service

        paragraph = await _create_paragraph()
        await _seed_vector(autoq_env, paragraph.id)
        items = [("paragraph", paragraph.id, 10)]
        await _run_round("tx-1", items)
        await _run_round("tx-2", items)

        original = admin_service.mark_paragraphs_quarantined

        async def broken_mark(*_args: object, **_kwargs: object) -> list[int]:
            raise RuntimeError("转隔离失败")

        monkeypatch.setattr(admin_service, "mark_paragraphs_quarantined", broken_mark)
        await _run_round("tx-3-broken", items)

        rolled_back = await DBMemParagraph.get(id=paragraph.id)
        assert rolled_back.blocked_round_hits == 2
        assert rolled_back.is_inactive is False

        monkeypatch.setattr(admin_service, "mark_paragraphs_quarantined", original)
        await _run_round("tx-3", items)

        current = await DBMemParagraph.get(id=paragraph.id)
        assert current.blocked_round_hits == 3
        assert current.is_inactive is True
        assert current.quarantine_reason == "auto_blocked"

    async def test_success_round_does_not_increment(self, autoq_env: AsyncQdrantClient) -> None:
        paragraph = await _create_paragraph()
        items = [("paragraph", paragraph.id, 10)]

        await _run_round("block-once", items)
        await _run_round("ok-once", items, outcome="ok")

        current = await DBMemParagraph.get(id=paragraph.id)
        assert current.blocked_round_hits == 1
        assert current.is_inactive is False

    async def test_works_with_itemization_off_and_when_the_row_write_fails(
        self,
        autoq_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 明细保留轮数与自动隔离是两个独立的开关：明细关掉（或明细写库出错）时，
        # 安全兜底仍然要生效。
        monkeypatch.setattr(config, "MEMORY_PROMPT_ROUNDS_KEEP", 0)
        paragraph = await _create_paragraph()
        items = [("paragraph", paragraph.id, 10)]
        for index in range(2):
            await _run_round(f"off-{index}", items)

        monkeypatch.setattr(config, "MEMORY_PROMPT_ROUNDS_KEEP", 20)

        async def broken_write(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("写明细失败")

        monkeypatch.setattr(prompt_rounds, "_write_round", broken_write)
        await _run_round("broken-write", items)

        current = await DBMemParagraph.get(id=paragraph.id)
        assert current.blocked_round_hits == 3
        assert current.is_inactive is True
        assert current.quarantine_reason == "auto_blocked"
        assert await DBMemPromptRound.all().count() == 0

    async def test_zero_threshold_counts_without_quarantine(
        self,
        autoq_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS", 0)
        paragraph = await _create_paragraph()
        items = [("paragraph", paragraph.id, 10)]

        for index in range(5):
            await _run_round(f"zero-{index}", items)

        current = await DBMemParagraph.get(id=paragraph.id)
        assert current.blocked_round_hits == 5
        assert current.is_inactive is False
        assert current.quarantined_at is None

    async def test_protected_paragraph_is_still_quarantined(
        self,
        autoq_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS", 1)
        paragraph = await _create_paragraph(is_protected=True)

        await _run_round("protected-1", [("paragraph", paragraph.id, 10)])

        current = await DBMemParagraph.get(id=paragraph.id)
        assert current.is_inactive is True
        assert current.quarantine_reason == "auto_blocked"

    async def test_episode_is_quarantined(
        self,
        autoq_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS", 2)
        episode = await DBMemEpisode.create(
            workspace_id=0,
            instance_key=INSTANCE,
            preset_id=0,
            adapter_key=ADAPTER,
            origin_chat_key=CHAT_KEY,
            title="被拦事件",
            narrative_summary="事件摘要",
        )
        items = [("episode", episode.id, 10)]

        await _run_round("episode-1", items)
        first = await DBMemEpisode.get(id=episode.id)
        assert first.blocked_round_hits == 1
        assert first.is_inactive is False

        await _run_round("episode-2", items)
        second = await DBMemEpisode.get(id=episode.id)
        assert second.blocked_round_hits == 2
        assert second.is_inactive is True
        assert second.quarantine_reason == "auto_blocked"

    async def test_relation_and_entity_are_not_counted(self, autoq_env: AsyncQdrantClient) -> None:
        subject = await DBMemEntity.create(
            workspace_id=0,
            instance_key=INSTANCE,
            preset_id=0,
            adapter_key=ADAPTER,
            entity_type="person",
            name="甲",
            canonical_name="甲",
        )
        obj = await DBMemEntity.create(
            workspace_id=0,
            instance_key=INSTANCE,
            preset_id=0,
            adapter_key=ADAPTER,
            entity_type="concept",
            name="乙",
            canonical_name="乙",
        )
        relation = await DBMemRelation.create(
            workspace_id=0,
            instance_key=INSTANCE,
            preset_id=0,
            adapter_key=ADAPTER,
            subject_entity_id=subject.id,
            predicate="喜欢",
            object_entity_id=obj.id,
            memory_source="na",
            cognitive_type="episodic",
        )
        items = [("relation", relation.id, 5), ("entity", subject.id, 5)]

        for index in range(5):
            await _run_round(f"graph-{index}", items)

        current_relation = await DBMemRelation.get(id=relation.id)
        current_entity = await DBMemEntity.get(id=subject.id)
        assert current_relation.is_inactive is False
        assert current_entity.is_inactive is False

    async def test_quarantined_paragraph_is_not_recalled(
        self,
        autoq_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paragraph = await _create_paragraph(content="会被召回的内容")
        await _seed_vector(autoq_env, paragraph.id)

        before = await retrieve_memories(0, "测试查询", limit=5, scope=MemoryScope.unrestricted())
        assert [memory.target_id for memory in before] == [paragraph.id]

        monkeypatch.setattr(config, "MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS", 1)
        await _run_round("recall-block", [("paragraph", paragraph.id, 10)])

        after = await retrieve_memories(0, "测试查询", limit=5, scope=MemoryScope.unrestricted())
        assert after == []
