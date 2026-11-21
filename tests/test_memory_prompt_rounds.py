"""每轮提示词明细：采集、落库、保留、状态与接入点。

明细只存引用与字数，不存提示词正文；因此这里既校验采集后的字段，也校验被引用条目
在之后被隔离、软删、真删时详情能给出当前状态。所有用例都确认采集不改变提示词文字。
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, AsyncIterator, Sequence

import pytest
from qdrant_client import AsyncQdrantClient
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.affection import prompt as affection_prompt_module
from nekro_agent.services.affection.stage import AffectionStage
from nekro_agent.services.affection.store import AffectionSnapshot
from nekro_agent.services.affection.suppression import reset_narrative_suppression
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.model_capability import LLMFailureKind
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import instance_scope as instance_scope_module
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory.instance_scope import AdminMemoryScope, MemoryScope
from nekro_agent.services.memory.recall_contract import MemoryRecallPlan, MemoryRecallQuerySpec
from nekro_agent.services.memory.recall_cooldown import reset_recall_cooldown
from nekro_agent.services.memory.retriever import MemoryRecallQuery, RetrievedMemory
from nekro_agent.services.memory.round_recall import reset_round_recalls

ADAPTER = "onebot_v11"
INSTANCE = "prompt"
CHAT_KEY = "onebot_v11-prompt-group_1"


class _Channel:
    """只提供采集与注入需要的频道字段，避免用例依赖频道配置合并。"""

    def __init__(
        self,
        *,
        chat_key: str = CHAT_KEY,
        adapter_key: str = ADAPTER,
        instance_key: str = INSTANCE,
        workspace_id: int | None = 0,
        preset_id: int = 0,
        channel_type: str = "group",
        max_items: int = 8,
        max_length: int = 1200,
        cooldown_rounds: int = 0,
        summary_enabled: bool = True,
    ) -> None:
        self.chat_key = chat_key
        self.adapter_key = adapter_key
        self.instance_key = instance_key
        self.workspace_id = workspace_id
        self.preset_id = preset_id
        self.channel_type = channel_type
        self._max_items = max_items
        self._max_length = max_length
        self._cooldown_rounds = cooldown_rounds
        self._summary_enabled = summary_enabled

    async def get_effective_config(self) -> SimpleNamespace:
        return SimpleNamespace(
            MEMORY_ENABLE_FOR_CHAT=True,
            MEMORY_CONTEXT_MAX_ITEMS=self._max_items,
            MEMORY_CONTEXT_MAX_LENGTH=self._max_length,
            MEMORY_RECALL_COOLDOWN_ROUNDS=self._cooldown_rounds,
            MEMORY_CHANNEL_SUMMARY_ENABLED=self._summary_enabled,
            MEMORY_CHANNEL_SUMMARY_MAX_CHARS=400,
        )

    async def get_effective_preset_id(self) -> int:
        return self.preset_id

    @property
    def chat_type(self) -> SimpleNamespace:
        return SimpleNamespace(value=self.channel_type)


def _memory(pid: int, *, content: str | None = None, similarity: float = 0.9) -> RetrievedMemory:
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
        similarity_score=similarity,
        effective_weight=1.0,
        event_time=None,
        origin_chat_key=CHAT_KEY,
    )


@pytest.fixture
async def prompt_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncQdrantClient]:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_MAX_CHARS", 400)
    monkeypatch.setattr(config, "MEMORY_CONTEXT_MAX_ITEMS", 8)
    monkeypatch.setattr(config, "MEMORY_CONTEXT_MAX_LENGTH", 1200)
    monkeypatch.setattr(config, "MEMORY_RECALL_COOLDOWN_ROUNDS", 0)
    monkeypatch.setattr(config, "MEMORY_RECALL_COOLDOWN_BYPASS_SIMILARITY", 0.8)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_DEFAULT_LIMIT", 5)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.0)
    monkeypatch.setattr(config, "MEMORY_PROMPT_ROUNDS_KEEP", 20)
    monkeypatch.setattr(config, "MEMORY_PROMPT_ROUNDS_MAX_AGE_DAYS", 30)
    monkeypatch.setattr(config, "MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS", 3)
    monkeypatch.setattr(config, "AFFECTION_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "AFFECTION_ENABLE_PROMPT_INJECTION", True)
    monkeypatch.setattr(config, "AFFECTION_PROMPT_MAX_USERS", 5)
    monkeypatch.setattr(config, "AFFECTION_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", 3600)

    async def fake_embed(_text: str, **_kwargs: object) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]

    async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

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


def _install_memory_retrieval(
    monkeypatch: pytest.MonkeyPatch,
    memories: list[RetrievedMemory],
    *,
    preset_id: int = 7,
) -> None:
    async def fake_plan(_messages: Sequence[Any]) -> MemoryRecallPlan:
        return MemoryRecallPlan(queries=[MemoryRecallQuerySpec(query_text="最近聊了什么", focus_text="")])

    async def fake_retrieve(*_args: Any, **_kwargs: Any) -> list[list[RetrievedMemory]]:
        return [memories]

    async def fake_scope(_channel: Any) -> MemoryScope:
        return MemoryScope(allowed_instance_keys=None, preset_id=preset_id)

    monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)
    monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_retrieve)
    monkeypatch.setattr(instance_scope_module, "resolve_memory_scope", fake_scope)


async def _create_round(
    *,
    chat_key: str = CHAT_KEY,
    adapter_key: str = ADAPTER,
    instance_key: str = INSTANCE,
    finished_at: datetime | None = None,
    summary_id: int | None = None,
    summary_version: int | None = None,
    memory_items: list[dict[str, Any]] | None = None,
) -> DBMemPromptRound:
    now = datetime.now(timezone.utc)
    return await DBMemPromptRound.create(
        round_id=secrets.token_hex(16),
        chat_key=chat_key,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=1,
        workspace_id=0,
        outcome="ok",
        render_count=1,
        memory_state="empty",
        memory_items=memory_items or [],
        memory_block_chars=0,
        cooldown_skipped=0,
        summary_state="none",
        summary_id=summary_id,
        summary_version=summary_version,
        summary_chars=0,
        affection_shown=False,
        affection_chars=0,
        affection_people=0,
        worldbook_items=[],
        started_at=now,
        finished_at=finished_at or now,
    )


async def _create_paragraph(
    *,
    content: str = "段落正文",
    summary: str | None = "段落摘要",
    is_inactive: bool = False,
    quarantined_at: datetime | None = None,
    last_manual_action: str | None = None,
) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=0,
        instance_key=INSTANCE,
        preset_id=1,
        adapter_key=ADAPTER,
        origin_chat_key=CHAT_KEY,
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.CONVERSATION,
        origin_kind=OriginKind.MESSAGE,
        content=content,
        summary=summary,
        is_inactive=is_inactive,
        quarantined_at=quarantined_at,
        last_manual_action=last_manual_action,
    )


class TestCollector:
    async def test_two_renders_merge_items(self, prompt_env: AsyncQdrantClient) -> None:
        code = "merge-code"
        channel = _Channel()
        prompt_rounds.begin(code, channel)
        prompt_rounds.note_memory(
            code,
            state="injected",
            workspace_id=3,
            preset_id=7,
            items=[("paragraph", 1, 10), ("episode", 2, 20)],
            block_chars=100,
        )
        prompt_rounds.note_memory(
            code,
            state="injected",
            workspace_id=3,
            preset_id=7,
            items=[("paragraph", 1, 12), ("paragraph", 3, 5)],
            block_chars=120,
            cooldown_skipped=2,
        )
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get()
        assert row.render_count == 2
        assert row.memory_state == "injected"
        assert row.memory_block_chars == 120
        assert row.cooldown_skipped == 2
        assert row.workspace_id == 3
        assert row.preset_id == 7
        items = {(item["type"], item["id"]): item for item in row.memory_items}
        assert set(items) == {("paragraph", 1), ("paragraph", 3), ("episode", 2)}
        assert items[("paragraph", 1)]["renders"] == 2
        assert items[("paragraph", 1)]["chars"] == 12
        assert items[("episode", 2)]["renders"] == 1

    @pytest.mark.parametrize("state", ["injected", "empty", "disabled", "suppressed", "error"])
    async def test_memory_states_persist(self, prompt_env: AsyncQdrantClient, state: str) -> None:
        code = f"memory-{state}"
        prompt_rounds.begin(code, _Channel())
        prompt_rounds.note_memory(code, state=state)
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get()
        assert row.memory_state == state

    @pytest.mark.parametrize("state", ["injected", "none", "disabled", "suppressed"])
    async def test_summary_states_persist(self, prompt_env: AsyncQdrantClient, state: str) -> None:
        code = f"summary-{state}"
        prompt_rounds.begin(code, _Channel())
        prompt_rounds.note_memory(code, state="empty")
        prompt_rounds.note_summary(
            code,
            state=state,
            summary_id=11 if state == "injected" else None,
            summary_version=2 if state == "injected" else None,
            chars=33 if state == "injected" else 0,
        )
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get()
        assert row.summary_state == state
        if state == "injected":
            assert row.summary_id == 11
            assert row.summary_version == 2
            assert row.summary_chars == 33
        else:
            assert row.summary_id is None
            assert row.summary_version is None

    async def test_injected_lengths_match_rendered_text(
        self,
        prompt_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset_id = 7
        channel = _Channel(preset_id=preset_id)
        summary = await DBMemChannelSummary.create(
            chat_key=CHAT_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=preset_id,
            version=3,
            content="频道摘要正文",
            last_message_db_id=10,
        )
        _install_memory_retrieval(monkeypatch, [_memory(1), _memory(2)], preset_id=preset_id)

        code = "length-code"
        prompt_rounds.begin(code, channel)
        summary_text = await history_module._inject_channel_summary(channel, code)  # noqa: SLF001
        memory_text = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code=code,
        )
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get()
        assert summary_text
        assert memory_text
        assert row.summary_state == "injected"
        assert row.summary_id == summary.id
        assert row.summary_version == 3
        assert row.summary_chars == len(summary_text)
        assert row.memory_state == "injected"
        assert row.memory_block_chars == len(memory_text)
        assert row.memory_items
        assert sum(int(item["chars"]) for item in row.memory_items) <= row.memory_block_chars

    async def test_included_only_contains_rendered_items(
        self,
        prompt_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        query = MemoryRecallQuery(query_text="最近聊了什么", focus_text="", focus_points=[], context_texts=[])
        memories = [_memory(1), _memory(2), _memory(3)]
        full = await retriever_module.compile_memories_for_context(query, memories, max_length=5000)
        lines = full.split("\n")
        assert len(lines) >= 4
        budget = len(lines[0]) + len(lines[1]) + 1 + len(lines[2]) + 1

        included: list[tuple[str, int, int]] = []
        truncated = await retriever_module.compile_memories_for_context(
            query,
            memories,
            max_length=budget,
            included=included,
        )
        assert truncated == "\n".join(lines[:3])
        assert len(included) == 1
        assert included[0][0] == "paragraph"
        assert included[0][2] == len(lines[2])

        plain = await retriever_module.compile_memories_for_context(query, memories, max_length=5000)
        with_included = await retriever_module.compile_memories_for_context(
            query,
            memories,
            max_length=5000,
            included=[],
        )
        assert plain == with_included

    async def test_truncated_item_is_not_persisted(
        self,
        prompt_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset_id = 7
        channel = _Channel(preset_id=preset_id)
        memories = [_memory(1), _memory(2), _memory(3)]
        _install_memory_retrieval(monkeypatch, memories, preset_id=preset_id)
        query = MemoryRecallQuery(query_text="最近聊了什么", focus_text="", focus_points=[], context_texts=[])
        full = await retriever_module.compile_memories_for_context(query, memories, max_length=5000)
        lines = full.split("\n")
        budget = len(lines[0]) + len(lines[1]) + 1 + len(lines[2]) + 1

        code = "truncate-code"
        prompt_rounds.begin(code, channel)
        memory_text = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code=code,
            max_length=budget,
        )
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get()
        assert memory_text
        assert len(row.memory_items) == 1
        assert row.memory_items[0]["chars"] == len(lines[2])
        assert row.memory_block_chars == len(memory_text)
        assert sum(int(item["chars"]) for item in row.memory_items) <= row.memory_block_chars

    async def test_budget_can_exclude_the_first_item(
        self,
        prompt_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset_id = 7
        channel = _Channel(preset_id=preset_id)
        memories = [_memory(1)]
        _install_memory_retrieval(monkeypatch, memories, preset_id=preset_id)
        query = MemoryRecallQuery(query_text="最近聊了什么", focus_text="", focus_points=[], context_texts=[])
        full = await retriever_module.compile_memories_for_context(query, memories, max_length=5000)
        lines = full.split("\n")
        budget = len(lines[0]) + len(lines[1]) + 1

        code = "truncate-first"
        prompt_rounds.begin(code, channel)
        memory_text = await history_module._inject_memory_context(  # noqa: SLF001
            workspace_id=None,
            recent_messages=[],
            channel=channel,
            one_time_code=code,
            max_length=budget,
        )
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get()
        assert memory_text
        assert row.memory_state == "injected"
        assert row.memory_items == []

    async def test_affection_block_is_recorded(
        self,
        prompt_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_participants(*_args: Any, **_kwargs: Any) -> list[tuple[str, str]]:
            return [("ua", "甲"), ("ub", "乙")]

        async def fake_scopes(_channel: Any, userids: Sequence[str]) -> dict[str, SimpleNamespace]:
            return {
                userid: SimpleNamespace(
                    affection_domain=SimpleNamespace(adapter_key="", instance_key="", group_id=0),
                    affection_person_uid=userid,
                    preset_id=1,
                )
                for userid in userids
            }

        async def fake_snapshots(keys: Sequence[Any], _ttl: int) -> dict[Any, AffectionSnapshot]:
            return {
                key: AffectionSnapshot(
                    key=key,
                    exists=True,
                    score=20,
                    stage=AffectionStage.FRIENDLY,
                    nickname=key.person_uid,
                    state_text="有点开心",
                    last_event_text="刚聊过天",
                    interaction_count=3,
                )
                for key in keys
            }

        monkeypatch.setattr(affection_prompt_module, "_recent_participants", fake_participants)
        monkeypatch.setattr(affection_prompt_module, "resolve_identity_scopes", fake_scopes)
        monkeypatch.setattr(affection_prompt_module, "load_snapshots", fake_snapshots)

        code = "affection-code"
        channel = _Channel()
        prompt_rounds.begin(code, channel)
        text = await affection_prompt_module.render_affection_prompt(
            db_chat_channel=channel,
            one_time_code=code,
            config=config,
        )
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get()
        assert text
        assert row.affection_shown is True
        assert row.affection_chars == len(text)
        assert row.affection_people == 2

    async def test_affection_empty_still_records(
        self,
        prompt_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_participants(*_args: Any, **_kwargs: Any) -> list[tuple[str, str]]:
            return []

        monkeypatch.setattr(affection_prompt_module, "_recent_participants", fake_participants)

        code = "affection-empty"
        channel = _Channel()
        prompt_rounds.begin(code, channel)
        text = await affection_prompt_module.render_affection_prompt(
            db_chat_channel=channel,
            one_time_code=code,
            config=config,
        )
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        row = await DBMemPromptRound.get()
        assert text == ""
        assert row.render_count == 0
        assert row.affection_shown is False
        assert row.affection_chars == 0
        assert row.affection_people == 0


class TestRetention:
    async def test_keep_only_latest_rows_per_channel(
        self,
        prompt_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_PROMPT_ROUNDS_KEEP", 3)
        channel_a = _Channel(chat_key="chat-a")
        channel_b = _Channel(chat_key="chat-b")
        for index in range(5):
            code = f"a-{index}"
            prompt_rounds.begin(code, channel_a)
            prompt_rounds.note_memory(code, state="empty")
            prompt_rounds.finish(code, "ok")
            await prompt_rounds.drain()
        code = "b-0"
        prompt_rounds.begin(code, channel_b)
        prompt_rounds.note_memory(code, state="empty")
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        rows_a = await DBMemPromptRound.filter(chat_key="chat-a").order_by("id")
        rows_b = await DBMemPromptRound.filter(chat_key="chat-b")
        latest = await DBMemPromptRound.filter(chat_key="chat-a").order_by("-id").first()
        assert len(rows_a) == 3
        assert len(rows_b) == 1
        assert latest is not None
        assert rows_a[-1].id == latest.id

    async def test_zero_keep_writes_nothing(
        self,
        prompt_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_PROMPT_ROUNDS_KEEP", 0)
        code = "zero-keep"
        prompt_rounds.begin(code, _Channel())
        prompt_rounds.note_memory(code, state="injected", items=[("paragraph", 1, 3)])
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        assert await DBMemPromptRound.all().count() == 0

    async def test_prune_removes_only_old_rows(self, prompt_env: AsyncQdrantClient) -> None:
        now = datetime.now(timezone.utc)
        old = await _create_round(chat_key="old-chat", finished_at=now - timedelta(days=31))
        fresh = await _create_round(chat_key="fresh-chat", finished_at=now - timedelta(days=1))

        deleted = await prompt_rounds.prune_prompt_rounds(30)

        assert deleted == 1
        assert await DBMemPromptRound.get_or_none(id=old.id) is None
        assert await DBMemPromptRound.get_or_none(id=fresh.id) is not None


class TestPersistence:
    async def test_write_failure_does_not_raise(
        self,
        prompt_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def broken_create(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("database is down")

        monkeypatch.setattr(DBMemPromptRound, "create", broken_create)

        code = "broken-code"
        prompt_rounds.begin(code, _Channel())
        prompt_rounds.note_memory(code, state="empty")
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

    async def test_no_column_equals_one_time_code(self, prompt_env: AsyncQdrantClient) -> None:
        code = "secret-token-value"
        prompt_rounds.begin(code, _Channel())
        prompt_rounds.note_memory(code, state="empty")
        prompt_rounds.finish(code, "ok")
        await prompt_rounds.drain()

        assert "one_time_code" not in DBMemPromptRound._meta.fields_map
        row = await DBMemPromptRound.get()
        for field_name in DBMemPromptRound._meta.fields_map:
            assert str(getattr(row, field_name)) != code


class TestRunAgentOutcome:
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("ok", "ok"),
            ("all_failed_blocked", "blocked"),
            ("provider_refusal", "blocked"),
            ("other_failed", "failed"),
            ("all_failed_other", "failed"),
            ("cancelled", "cancelled"),
        ],
    )
    async def test_outcomes(
        self,
        prompt_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        expected: str,
    ) -> None:
        async def fake_inner(**kwargs: Any) -> None:
            prompt_rounds.note_memory(str(kwargs["one_time_code"]), state="empty")
            if mode == "ok":
                return
            if mode == "all_failed_blocked":
                raise run_agent_module.AllLLMRequestsFailedError(
                    "blocked",
                    kinds=[LLMFailureKind.CONTENT_BLOCKED],
                )
            if mode == "provider_refusal":
                raise run_agent_module.AgentProviderRefusalError("refused")
            if mode == "all_failed_other":
                raise run_agent_module.AllLLMRequestsFailedError("failed", kinds=[LLMFailureKind.OTHER])
            if mode == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("boom")

        monkeypatch.setattr(run_agent_module, "_run_agent_in_instance_scope", fake_inner)
        channel = _Channel()
        code = f"agent-{mode}"
        kwargs = {
            "chat_key": CHAT_KEY,
            "chat_message": None,
            "ctx": SimpleNamespace(),
            "db_chat_channel": channel,
            "one_time_code": code,
        }
        if mode == "ok":
            await run_agent_module._run_agent_with_affection_suppression(**kwargs)  # noqa: SLF001
        elif mode == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await run_agent_module._run_agent_with_affection_suppression(**kwargs)  # noqa: SLF001
        elif mode == "all_failed_blocked":
            with pytest.raises(run_agent_module.AllLLMRequestsFailedError):
                await run_agent_module._run_agent_with_affection_suppression(**kwargs)  # noqa: SLF001
        elif mode == "provider_refusal":
            with pytest.raises(run_agent_module.AgentProviderRefusalError):
                await run_agent_module._run_agent_with_affection_suppression(**kwargs)  # noqa: SLF001
        elif mode == "all_failed_other":
            with pytest.raises(run_agent_module.AllLLMRequestsFailedError):
                await run_agent_module._run_agent_with_affection_suppression(**kwargs)  # noqa: SLF001
        else:
            with pytest.raises(RuntimeError):
                await run_agent_module._run_agent_with_affection_suppression(**kwargs)  # noqa: SLF001

        await prompt_rounds.drain()
        row = await DBMemPromptRound.get()
        assert row.outcome == expected


class TestQueries:
    async def test_list_rounds_filters_by_admin_scope(self, prompt_env: AsyncQdrantClient) -> None:
        own = await _create_round(chat_key="chat-a", adapter_key=ADAPTER, instance_key="inst-a")
        await _create_round(chat_key="chat-b", adapter_key=ADAPTER, instance_key="inst-b")

        scope = AdminMemoryScope.for_pairs([(ADAPTER, "inst-a")])
        total, rows = await prompt_rounds.list_rounds(scope, chat_key="chat-a", page=1, page_size=10)
        assert total == 1
        assert [row.id for row in rows] == [own.id]

        outside_total, outside_rows = await prompt_rounds.list_rounds(scope, chat_key="chat-b", page=1, page_size=10)
        assert outside_total == 0
        assert outside_rows == []

    async def test_round_detail_reports_current_item_status(self, prompt_env: AsyncQdrantClient) -> None:
        active = await _create_paragraph()
        deleted = await _create_paragraph(
            content="已软删",
            is_inactive=True,
            quarantined_at=None,
            last_manual_action="delete",
        )
        now = datetime.now(timezone.utc)
        quarantined_episode = await DBMemEpisode.create(
            workspace_id=0,
            instance_key=INSTANCE,
            preset_id=1,
            adapter_key=ADAPTER,
            origin_chat_key=CHAT_KEY,
            title="被隔离事件",
            narrative_summary="摘要",
            is_inactive=True,
            quarantined_at=now,
            quarantine_reason="manual",
        )
        subject = await DBMemEntity.create(
            workspace_id=0,
            instance_key=INSTANCE,
            preset_id=1,
            adapter_key=ADAPTER,
            entity_type="person",
            name="甲",
            canonical_name="甲",
        )
        obj = await DBMemEntity.create(
            workspace_id=0,
            instance_key=INSTANCE,
            preset_id=1,
            adapter_key=ADAPTER,
            entity_type="concept",
            name="乙",
            canonical_name="乙",
        )
        relation = await DBMemRelation.create(
            workspace_id=0,
            instance_key=INSTANCE,
            preset_id=1,
            adapter_key=ADAPTER,
            subject_entity_id=subject.id,
            predicate="喜欢",
            object_entity_id=obj.id,
            memory_source="na",
            cognitive_type="episodic",
        )
        summary_v1 = await DBMemChannelSummary.create(
            chat_key=CHAT_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=1,
            version=1,
            content="旧版",
            last_message_db_id=1,
        )
        summary_v2 = await DBMemChannelSummary.create(
            chat_key=CHAT_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=1,
            version=2,
            content="新版",
            last_message_db_id=2,
        )
        row = await _create_round(
            summary_id=summary_v2.id,
            summary_version=summary_v2.version,
            memory_items=[
                {"type": "paragraph", "id": active.id, "chars": 4, "renders": 1},
                {"type": "episode", "id": quarantined_episode.id, "chars": 5, "renders": 1},
                {"type": "paragraph", "id": deleted.id, "chars": 3, "renders": 1},
                {"type": "relation", "id": relation.id, "chars": 6, "renders": 1},
                {"type": "paragraph", "id": 9001, "chars": 0, "renders": 1},
            ],
        )

        detail = await prompt_rounds.round_detail(row)
        items = {(item["type"], item["id"]): item for item in detail["memory_items"]}
        assert items[("paragraph", active.id)]["status"] == "active"
        assert items[("paragraph", active.id)]["preview"] == "段落摘要"
        assert items[("episode", quarantined_episode.id)]["status"] == "quarantined"
        assert items[("episode", quarantined_episode.id)]["preview"] == "被隔离事件"
        assert items[("paragraph", deleted.id)]["status"] == "deleted"
        assert items[("relation", relation.id)]["status"] == "active"
        assert items[("relation", relation.id)]["preview"] == "甲 喜欢 乙"
        assert items[("paragraph", 9001)]["status"] == "hard_deleted"
        assert detail["summary"]["id"] == summary_v2.id
        assert detail["summary"]["version"] == 2
        assert detail["summary"]["is_latest"] is True
        assert detail["summary"]["state"] == "current"

        row.summary_id = summary_v1.id
        row.summary_version = summary_v1.version
        old_detail = await prompt_rounds.round_detail(row)
        assert old_detail["summary"]["is_latest"] is False
        assert old_detail["summary"]["state"] == "outdated"

        row.summary_id = 99999
        row.summary_version = 99
        missing_detail = await prompt_rounds.round_detail(row)
        assert missing_detail["summary"]["state"] == "deleted"
        assert missing_detail["summary"]["is_latest"] is False

    async def test_round_detail_carries_the_worldbook_columns(self, prompt_env: AsyncQdrantClient) -> None:
        """世界书的状态与字数原样带出（不在这里补条目状态：那要按身份判，属于路由层）"""
        row = await _create_round()
        row.worldbook_state = "injected"
        row.worldbook_chars = 87
        row.worldbook_items = [
            {"type": "worldbook", "id": 5, "book_id": 2, "chars": 87, "position": "before_history", "trusted": False},
        ]
        await row.save()

        detail = await prompt_rounds.round_detail(row)

        assert detail["worldbook_state"] == "injected"
        assert detail["worldbook_chars"] == 87
        assert [item["id"] for item in detail["worldbook_items"]] == [5]
