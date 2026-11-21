"""闸开时工作区重建对绑定频道做域行重建。

旧域行（段落、涉及的人、向量、关系、实体、episode）在第一次处理频道时删掉，然后以
分区 0 回放消息写出新域行；工作区行的重置与 CC 语义日志重放保持原样。续跑时频道状态
已经是 RUNNING，不再重复删除。

另外覆盖 ops 单频道重建的估算与执行，以及重建期间调度器对该频道挂 30 秒重排。
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, List

import pytest
from qdrant_client import AsyncQdrantClient
from tortoise import Tortoise

from nekro_agent.core.config import ModelConfigGroup, config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_mem_reinforcement_log import DBMemReinforcementLog, TargetType, TriggerSource
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_workspace_comm_log import DBWorkspaceCommLog
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory import rebuild as rebuild_module
from nekro_agent.services.memory import rebuild_state_store as store
from nekro_agent.services.memory.consolidator import EpisodicConsolidator
from nekro_agent.services.memory.persons import build_paragraph_payload
from nekro_agent.services.memory.rebuild import (
    _rebuild_channels,
    estimate_domain_channel_rebuild,
    rebuild_domain_channel,
    rebuild_workspace_memories,
)
from nekro_agent.services.memory.rebuild_state_store import (
    MemoryRebuildChannelsFile,
    MemoryRebuildChannelState,
    RebuildChannelStatus,
    RebuildJobStatus,
    create_job_state,
    read_job_state,
    write_job_channels,
)
from nekro_agent.services.memory.scheduler import (
    ChannelActivityStats,
    ConsolidationPriority,
    ConsolidationTask,
    MemoryMaintenanceScheduler,
)

ADAPTER = "onebot_v11"
INSTANCE = "domain-rebuild"
BOT_ACCOUNT = "ubot"
CHAT_A = "onebot_v11-domain-rebuild-group_1"
CHAT_B = "onebot_v11-domain-rebuild-group_2"


@pytest.fixture
async def memory_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_SIZE", 2)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_CONTENT_LENGTH", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MODEL_GROUP", "mem-primary")
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_FALLBACK_MODEL_GROUP", "mem-primary")
    monkeypatch.setattr(config, "USE_MODEL_GROUP", "mem-primary")
    monkeypatch.setattr(config, "MEMORY_REBUILD_LOOKBACK_DAYS", 7)
    monkeypatch.setattr(config, "MEMORY_SEMANTIC_MIN_RESULT_LENGTH", 10)
    monkeypatch.setattr(config, "MEMORY_RECALL_COOLDOWN_ROUNDS", 0)
    monkeypatch.setattr(config, "OUTPUT_FILTER_ENABLE", False)
    monkeypatch.setattr(config, "OUTPUT_FILTER_BEFORE_MEMORY", False)
    # 假向量恒定，这里不测去重：不关掉的话重建写出的多段会被当成近重复
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_DEDUP_SIMILARITY", 0.0)
    monkeypatch.setattr(
        config,
        "MODEL_GROUPS",
        {"mem-primary": ModelConfigGroup(GROUP_NAME="mem-primary", CHAT_MODEL="mem-model")},
    )

    async def fake_embed(_text: str, **_kwargs: object) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]

    async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr(consolidator_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_batch", fake_embed_batch)
    monkeypatch.setattr(qdrant_manager_module, "embed_batch", fake_embed_batch)
    from nekro_agent.services.memory import semantic_writer as semantic_writer_module

    monkeypatch.setattr(semantic_writer_module, "embed_text", fake_embed)

    client = AsyncQdrantClient(location=":memory:")

    async def fake_get_client() -> AsyncQdrantClient:
        return client

    monkeypatch.setattr(qdrant_manager_module, "get_qdrant_client", fake_get_client)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        provider_account_id=BOT_ACCOUNT,
    )
    await qdrant_manager_module.memory_qdrant_manager.ensure_collection(4)
    try:
        yield client
    finally:
        await client.close()
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def isolated_rebuild_dirs(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "REBUILD_JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(store, "REBUILD_WORKSPACE_DIR", tmp_path / "workspaces")


def _response() -> str:
    return json.dumps(
        {
            "memories": [
                {
                    "content": "小满和阿青约好周末去看展，先把票订下来。",
                    "summary": "小满与阿青约看展",
                    "knowledge_type": "decision",
                    "persons": [],
                    "entities": ["小满", "阿青"],
                    "relations": [{"subject": "小满", "predicate": "约会", "object": "阿青"}],
                },
            ],
        },
        ensure_ascii=False,
    )


def _install_llm(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    prompts: List[str] = []

    async def fake_call(_self: EpisodicConsolidator, prompt: str, *, use_fallback_model: bool = False) -> str:
        del use_fallback_model
        prompts.append(prompt)
        return _response()

    monkeypatch.setattr(EpisodicConsolidator, "_call_llm", fake_call)
    return prompts


async def _create_channel(
    chat_key: str,
    *,
    workspace_id: int | None,
    chat_type: str = "group",
) -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type=chat_type,
        is_active=True,
        workspace_id=workspace_id,
        data="{}",
    )


async def _create_messages(chat_key: str, count: int, *, chat_type: str = "group") -> List[DBChatMessage]:
    messages: List[DBChatMessage] = []
    now = int(time.time())
    for index in range(1, count + 1):
        messages.append(
            await DBChatMessage.create(
                sender_id=f"u{index}",
                sender_name=f"用户{index}",
                sender_nickname=f"用户{index}",
                is_tome=0,
                is_recalled=False,
                adapter_key=ADAPTER,
                instance_key=INSTANCE,
                message_id=f"m{index}",
                chat_key=chat_key,
                chat_type=chat_type,
                platform_userid=f"u{index}",
                content_text=f"第 {index} 条消息，聊的是周末看展的安排。",
                content_data="[]",
                raw_cq_code="",
                ext_data="{}",
                send_timestamp=now - 100 + index,
            ),
        )
    return messages


async def _seed_old_domain_rows(
    client: AsyncQdrantClient,
    chat_key: str,
) -> dict[str, int]:
    paragraph = await DBMemParagraph.create(
        workspace_id=0,
        instance_key=INSTANCE,
        preset_id=0,
        adapter_key=ADAPTER,
        origin_chat_key=chat_key,
        origin_chat_type="group",
        memory_source="na",
        origin_kind=OriginKind.CONSOLIDATION,
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.CONVERSATION,
        content="旧的域行记忆",
        summary="旧",
    )
    person = await DBMemParagraphPerson.create(paragraph_id=paragraph.id, account="uold")
    await qdrant_manager_module.memory_qdrant_manager.upsert_paragraph(
        paragraph_id=paragraph.id,
        embedding=[0.1, 0.2, 0.3, 0.4],
        payload=await build_paragraph_payload(paragraph),
    )
    subject = await DBMemEntity.create(
        workspace_id=0,
        instance_key=INSTANCE,
        preset_id=0,
        adapter_key=ADAPTER,
        origin_chat_key=chat_key,
        entity_type=EntityType.PERSON,
        name="旧人甲",
        canonical_name="旧人甲",
    )
    obj = await DBMemEntity.create(
        workspace_id=0,
        instance_key=INSTANCE,
        preset_id=0,
        adapter_key=ADAPTER,
        origin_chat_key=chat_key,
        entity_type=EntityType.PERSON,
        name="旧人乙",
        canonical_name="旧人乙",
    )
    relation = await DBMemRelation.create(
        workspace_id=0,
        instance_key=INSTANCE,
        preset_id=0,
        adapter_key=ADAPTER,
        origin_chat_key=chat_key,
        subject_entity_id=subject.id,
        predicate="认识",
        object_entity_id=obj.id,
        paragraph_id=paragraph.id,
        memory_source="na",
        cognitive_type="episodic",
    )
    episode = await DBMemEpisode.create(
        workspace_id=0,
        instance_key=INSTANCE,
        preset_id=0,
        adapter_key=ADAPTER,
        origin_chat_key=chat_key,
        origin_chat_type="group",
        title="旧 episode",
        narrative_summary="旧 episode 摘要",
        paragraph_ids=[paragraph.id],
    )
    log = await DBMemReinforcementLog.log_reinforcement(
        workspace_id=0,
        target_type=TargetType.PARAGRAPH,
        target_id=paragraph.id,
        trigger_source=TriggerSource.NA_RETRIEVAL,
    )
    return {
        "paragraph": paragraph.id,
        "person": person.id,
        "subject": subject.id,
        "object": obj.id,
        "relation": relation.id,
        "episode": episode.id,
        "log": log.id,
    }


async def _point_ids(client: AsyncQdrantClient, ids: List[int]) -> List[int]:
    points = await client.retrieve(
        collection_name=qdrant_manager_module.memory_qdrant_manager.collection_name,
        ids=ids,
    )
    return [int(point.id) for point in points]


class TestWorkspaceRebuildDomainPhase:
    async def test_old_domain_rows_are_replaced_and_workspace_rows_still_replayed(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = await DBWorkspace.create(name="domain-rebuild-workspace")
        await _create_channel(CHAT_A, workspace_id=workspace.id)
        await _create_messages(CHAT_A, 2)
        old_ids = await _seed_old_domain_rows(memory_env, CHAT_A)
        old_workspace_paragraph = await DBMemParagraph.create(
            workspace_id=workspace.id,
            instance_key=INSTANCE,
            preset_id=0,
            memory_source="na",
            origin_kind=OriginKind.CONSOLIDATION,
            cognitive_type=CognitiveType.EPISODIC,
            knowledge_type=KnowledgeType.CONVERSATION,
            content="旧的工作区行",
        )
        await DBWorkspaceCommLog.create(
            workspace_id=workspace.id,
            direction="USER_TO_CC",
            source_chat_key=CHAT_A,
            content="请总结这周的安排，结论要具体到时间与地点。",
            task_id="task-domain",
        )
        await DBWorkspaceCommLog.create(
            workspace_id=workspace.id,
            direction="CC_TO_NA",
            source_chat_key=CHAT_A,
            content="这周安排是周六下午两点在美术馆门口集合，先看展再吃饭。",
            task_id="task-domain",
        )
        prompts = _install_llm(monkeypatch)

        state = create_job_state(workspace.id, requested_by="test", request_id=None, cutoff=None)
        write_job_channels(state.job_id, MemoryRebuildChannelsFile())
        result = await rebuild_workspace_memories(state.job_id)

        final = read_job_state(state.job_id)
        assert final is not None
        assert final.status == RebuildJobStatus.COMPLETED
        assert result.channels_failed == 0
        assert result.failed_channels == []
        assert result.messages_processed == 2
        assert prompts

        # 旧域行的每一类都删掉了。
        assert not await DBMemParagraph.filter(id=old_ids["paragraph"]).exists()
        assert not await DBMemParagraphPerson.filter(id=old_ids["person"]).exists()
        assert not await DBMemParagraphPerson.filter(paragraph_id=old_ids["paragraph"]).exists()
        assert not await DBMemRelation.filter(id=old_ids["relation"]).exists()
        assert not await DBMemEntity.filter(id__in=[old_ids["subject"], old_ids["object"]]).exists()
        assert not await DBMemEpisode.filter(id=old_ids["episode"]).exists()
        assert not await DBMemReinforcementLog.filter(id=old_ids["log"]).exists()
        assert await _point_ids(memory_env, [old_ids["paragraph"]]) == []

        # 新域行写了回来，归属是分区 0 + 这个频道。
        new_paragraphs = await DBMemParagraph.filter(workspace_id=0, origin_chat_key=CHAT_A).all()
        assert len(new_paragraphs) == 1
        assert new_paragraphs[0].id != old_ids["paragraph"]
        assert new_paragraphs[0].origin_chat_type == "group"
        assert await _point_ids(memory_env, [new_paragraphs[0].id]) == [new_paragraphs[0].id]

        # 工作区行先被重置、再由 CC 语义日志重放写回；旧的那条不在。
        assert not await DBMemParagraph.filter(id=old_workspace_paragraph.id).exists()
        workspace_paragraphs = await DBMemParagraph.filter(workspace_id=workspace.id).all()
        assert len(workspace_paragraphs) == 1
        assert workspace_paragraphs[0].memory_source == "cc"
        assert result.semantic_tasks_replayed == 1

        events = store.get_job_events_path(state.job_id).read_text(encoding="utf-8")
        assert "channel_domain_reset" in events
        assert "channel_phase_skipped" not in events
        assert "channel_completed" in events

    async def test_resume_from_running_does_not_delete_again(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        workspace = await DBWorkspace.create(name="domain-resume-workspace")
        await _create_channel(CHAT_A, workspace_id=workspace.id)
        messages = await _create_messages(CHAT_A, 3)
        partial = await DBMemParagraph.create(
            workspace_id=0,
            instance_key=INSTANCE,
            preset_id=0,
            adapter_key=ADAPTER,
            origin_chat_key=CHAT_A,
            origin_chat_type="group",
            memory_source="na",
            origin_kind=OriginKind.CONSOLIDATION,
            cognitive_type=CognitiveType.EPISODIC,
            knowledge_type=KnowledgeType.CONVERSATION,
            content="续跑前已经写出的新域行",
        )

        async def refuse_reset(_chat_key: str) -> int:
            raise AssertionError("续跑时不应该再次删除域行")

        calls: List[tuple[int, str, int]] = []

        async def fake_consolidate(
            workspace_id_for_rows: int,
            chat_key: str,
            *,
            max_message_db_id: int | None = None,
            start_after_db_id: int | None = None,
            persist_progress: bool = True,
        ) -> Any:
            del persist_progress
            calls.append((workspace_id_for_rows, chat_key, start_after_db_id or 0))
            assert max_message_db_id == messages[-1].id
            return SimpleNamespace(
                failed_reason=None,
                errors=None,
                messages_processed=2,
                paragraphs_created=1,
                last_processed_message_db_id=messages[-1].id,
            )

        monkeypatch.setattr(rebuild_module, "reset_domain_channel_memory", refuse_reset)
        monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake_consolidate)

        channels = MemoryRebuildChannelsFile(
            channels={
                CHAT_A: MemoryRebuildChannelState(
                    status=RebuildChannelStatus.RUNNING,
                    message_count_total=3,
                    message_count_processed=1,
                    cursor_start_db_id=0,
                    cursor_current_db_id=messages[0].id,
                    cursor_upper_bound_db_id=messages[-1].id,
                ),
            },
        )
        state = create_job_state(workspace.id, requested_by="test", request_id=None, cutoff=None)
        write_job_channels(state.job_id, channels)

        processed, created = await _rebuild_channels(state, channels)

        assert (processed, created) == (2, 1)
        assert calls == [(0, CHAT_A, messages[0].id)]
        assert channels.channels[CHAT_A].status == RebuildChannelStatus.COMPLETED
        assert await DBMemParagraph.filter(id=partial.id).exists()


class TestDomainChannelRebuild:
    async def test_rebuilds_an_unbound_channel(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _create_channel(CHAT_B, workspace_id=None, chat_type="private")
        await _create_messages(CHAT_B, 3, chat_type="private")
        old_ids = await _seed_old_domain_rows(memory_env, CHAT_B)
        _install_llm(monkeypatch)

        result = await rebuild_domain_channel(CHAT_B)

        assert result.channels_completed == 1
        assert result.channels_failed == 0
        assert result.paragraphs_deleted == 1
        assert result.messages_processed == 3
        assert result.episodic_paragraphs_created == 2
        assert not await DBMemParagraph.filter(id=old_ids["paragraph"]).exists()
        rows = await DBMemParagraph.filter(workspace_id=0, origin_chat_key=CHAT_B).all()
        assert len(rows) == 2
        assert all(row.origin_chat_type == "private" for row in rows)
        channel = await DBChatChannel.get(chat_key=CHAT_B)
        assert channel.workspace_id is None

    async def test_estimate_counts_messages_and_calls(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del memory_env
        await _create_channel(CHAT_B, workspace_id=None)
        await _create_messages(CHAT_B, 5)

        estimate = await estimate_domain_channel_rebuild(CHAT_B)

        assert estimate.message_count == 5
        assert estimate.estimated_calls == 3
        assert estimate.cutoff is not None

    async def test_gate_off_rejects_single_channel_rebuild(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del memory_env
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        await _create_channel(CHAT_B, workspace_id=None)

        with pytest.raises(ValidationError, match="记忆域分区未开启"):
            await rebuild_domain_channel(CHAT_B)


class TestSchedulerPausesDuringDomainRebuild:
    async def test_execute_task_arms_a_30_second_retry(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del memory_env
        await _create_channel(CHAT_B, workspace_id=None)
        await _create_messages(CHAT_B, 2)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_reset(_chat_key: str) -> int:
            started.set()
            await release.wait()
            return 0

        async def fake_consolidate(
            workspace_id_for_rows: int,
            chat_key: str,
            *,
            max_message_db_id: int | None = None,
            start_after_db_id: int | None = None,
            persist_progress: bool = True,
        ) -> Any:
            del workspace_id_for_rows, chat_key, max_message_db_id, start_after_db_id, persist_progress
            return SimpleNamespace(
                failed_reason=None,
                errors=None,
                messages_processed=2,
                paragraphs_created=1,
                last_processed_message_db_id=1,
            )

        monkeypatch.setattr(rebuild_module, "reset_domain_channel_memory", slow_reset)
        monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake_consolidate)

        rebuild_task = asyncio.create_task(rebuild_domain_channel(CHAT_B))
        await started.wait()
        try:
            assert rebuild_module.is_domain_channel_rebuild_running(CHAT_B)

            scheduler = MemoryMaintenanceScheduler()
            handler_calls: List[tuple[int, str]] = []

            async def handler(workspace_id: int, chat_key: str) -> Any:
                handler_calls.append((workspace_id, chat_key))
                return SimpleNamespace(failed_reason=None, errors=None, messages_processed=0)

            scheduler.set_consolidation_handler(handler)
            scheduler._channel_stats[(0, CHAT_B)] = ChannelActivityStats(  # noqa: SLF001
                workspace_id=0,
                chat_key=CHAT_B,
                pending_messages=7,
            )
            before = time.time()

            await scheduler._execute_task(  # noqa: SLF001
                ConsolidationTask(
                    priority=ConsolidationPriority.NORMAL,
                    scheduled_at=0.0,
                    workspace_id=0,
                    chat_key=CHAT_B,
                ),
            )

            assert handler_calls == []
            assert scheduler._channel_stats[(0, CHAT_B)].pending_messages == 7  # noqa: SLF001
            due = scheduler.pending_retry_due(0, CHAT_B)
            assert due is not None
            assert 25.0 <= due - before <= 35.0
            await scheduler.stop()
        finally:
            release.set()
            await rebuild_task

        assert not rebuild_module.is_domain_channel_rebuild_running(CHAT_B)
