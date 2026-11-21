"""管理端服务：列表、状态转换、编辑、真删、批量与筛选项。

这些都跑在真表（SQLite 内存库）与本地 Qdrant 上：状态转换的核心是「数据库与向量两侧的
先后顺序」，只 mock 掉其中一侧就测不到那个顺序。嵌入用确定性假向量，避免任何外网调用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_mem_reinforcement_log import (
    DBMemReinforcementLog,
    TargetType,
    TriggerSource,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import ConflictError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.memory import admin_service
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.admin_service import (
    ParagraphListFilters,
    apply_action,
    build_paragraph_detail,
    bulk_paragraphs,
    edit_paragraph,
    hard_delete_entities,
    hard_delete_paragraphs,
    list_episodes,
    list_paragraphs,
    list_quarantine,
    load_item,
    paragraph_status,
    preview_hard_delete_paragraphs,
    quarantine,
    quarantine_paragraphs,
    resolve_person_filter,
    restore,
    scope_options,
    soft_delete,
    status_q,
    trace_paragraph,
)
from nekro_agent.services.memory.instance_scope import (
    CONSOLE_ONLY_PRESET_ID,
    AdminFilter,
    AdminMemoryScope,
    MemoryScope,
    resolve_admin_scope,
)
from nekro_agent.services.memory.qdrant_manager import MEMORY_PARAGRAPH_COLLECTION
from nekro_agent.services.memory.query_service import search_workspace_memories
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE = "i1"
OTHER_INSTANCE = "i2"
CHAT = "onebot_v11-i1-group_1"
OTHER_CHAT = "onebot_v11-i2-group_2"
VECTOR = [0.1, 0.2, 0.3, 0.4]

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _platform_user() -> DBUser:
    return DBUser(
        id=1,
        username="platform",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def _tenant_user(tenant_id: int = 1) -> DBUser:
    return DBUser(
        id=21,
        username="tenant-admin",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


def _scope(instance_key: str = INSTANCE, adapter_key: str = ADAPTER) -> AdminMemoryScope:
    return AdminMemoryScope.for_pairs([(adapter_key, instance_key)])


@pytest.fixture
async def env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncQdrantClient]:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.0)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_DEFAULT_LIMIT", 10)
    monkeypatch.setattr(config, "OUTPUT_FILTER_ENABLE", False)
    monkeypatch.setattr(config, "OUTPUT_FILTER_BEFORE_MEMORY", False)

    async def fake_embed(_text: str, **_kwargs: object) -> list[float]:
        return list(VECTOR)

    monkeypatch.setattr(embedding_service_module, "embed_text", fake_embed)

    client = AsyncQdrantClient(location=":memory:")

    async def fake_get_client() -> AsyncQdrantClient:
        return client

    monkeypatch.setattr(qdrant_manager_module, "get_qdrant_client", fake_get_client)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await qdrant_manager_module.memory_qdrant_manager.ensure_collection(len(VECTOR))
    try:
        yield client
    finally:
        await client.close()
        await Tortoise.close_connections()


async def _seed_instance(
    *,
    adapter_key: str = ADAPTER,
    instance_key: str = INSTANCE,
    tenant_id: int | None = None,
    bot_account: str = "",
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=f"{adapter_key}-{instance_key}",
        tenant_id=tenant_id,
        provider_account_id=bot_account,
    )


async def _seed_channel(
    *,
    chat_key: str = CHAT,
    adapter_key: str = ADAPTER,
    instance_key: str = INSTANCE,
    channel_id: str = "group_1",
    channel_name: str = "频道一",
    channel_type: str = "group",
) -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=adapter_key,
        instance_key=instance_key,
        channel_id=channel_id,
        channel_name=channel_name,
        channel_type=channel_type,
        data="{}",
        workspace_id=None,
    )


async def _message(
    *,
    chat_key: str = CHAT,
    sender_id: str,
    platform_userid: str,
    nickname: str,
    message_id: str,
) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=sender_id,
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        message_id=message_id,
        chat_key=chat_key,
        chat_type="group",
        platform_userid=platform_userid,
        content_text="消息",
        content_data="{}",
        raw_cq_code="",
        ext_data="",
        send_timestamp=1_700_000,
    )


async def _add_point(client: AsyncQdrantClient, row: DBMemParagraph, accounts: list[str]) -> None:
    await client.upsert(
        collection_name=MEMORY_PARAGRAPH_COLLECTION,
        points=[
            qdrant_models.PointStruct(
                id=row.id,
                vector=list(VECTOR),
                payload=row.to_qdrant_payload(person_accounts=accounts),
            ),
        ],
        wait=True,
    )


async def _point_count(client: AsyncQdrantClient, paragraph_id: int) -> int:
    records = await client.retrieve(
        collection_name=MEMORY_PARAGRAPH_COLLECTION,
        ids=[paragraph_id],
        with_payload=True,
    )
    return len(records)


async def _payload(client: AsyncQdrantClient, paragraph_id: int) -> dict[str, Any]:
    records = await client.retrieve(
        collection_name=MEMORY_PARAGRAPH_COLLECTION,
        ids=[paragraph_id],
        with_payload=True,
    )
    assert records
    return dict(records[0].payload or {})


async def _make_paragraph(
    *,
    workspace_id: int = 0,
    adapter_key: str = ADAPTER,
    instance_key: str = INSTANCE,
    preset_id: int = 7,
    origin_chat_key: str | None = CHAT,
    content: str = "记忆内容",
    summary: str | None = None,
    cognitive_type: CognitiveType = CognitiveType.SEMANTIC,
    knowledge_type: KnowledgeType = KnowledgeType.FACT,
    persons: tuple[str, ...] = (),
    event_time: datetime | None = None,
    is_inactive: bool = False,
    quarantined_at: datetime | None = None,
    quarantine_reason: str = "",
    blocked_round_hits: int = 0,
    is_frozen: bool = False,
    is_protected: bool = False,
    last_manual_action: str | None = None,
    embedding_ref: str | None = None,
) -> DBMemParagraph:
    row = await DBMemParagraph.create(
        workspace_id=workspace_id,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        origin_chat_key=origin_chat_key,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=cognitive_type,
        knowledge_type=knowledge_type,
        content=content,
        summary=summary,
        origin_kind=OriginKind.CONSOLIDATION,
        event_time=event_time,
        is_inactive=is_inactive,
        quarantined_at=quarantined_at,
        quarantine_reason=quarantine_reason,
        blocked_round_hits=blocked_round_hits,
        is_frozen=is_frozen,
        is_protected=is_protected,
        last_manual_action=last_manual_action,
        embedding_ref=embedding_ref,
    )
    for account in persons:
        await DBMemParagraphPerson.create(paragraph_id=row.id, account=account)
    return row


async def _make_episode(
    *,
    workspace_id: int = 0,
    adapter_key: str = ADAPTER,
    instance_key: str = INSTANCE,
    preset_id: int = 7,
    origin_chat_key: str | None = CHAT,
    title: str = "事件",
    is_inactive: bool = False,
    quarantined_at: datetime | None = None,
    quarantine_reason: str = "",
    blocked_round_hits: int = 0,
) -> DBMemEpisode:
    return await DBMemEpisode.create(
        workspace_id=workspace_id,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        origin_chat_key=origin_chat_key,
        origin_chat_type="group",
        title=title,
        narrative_summary=f"{title}的叙事",
        is_inactive=is_inactive,
        quarantined_at=quarantined_at,
        quarantine_reason=quarantine_reason,
        blocked_round_hits=blocked_round_hits,
    )


async def _make_relation(
    *,
    paragraph_id: int | None = None,
    subject_entity_id: int = 1,
    object_entity_id: int = 2,
    predicate: str = "related_to",
    workspace_id: int = 0,
) -> DBMemRelation:
    return await DBMemRelation.create(
        workspace_id=workspace_id,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        preset_id=7,
        origin_chat_key=CHAT,
        subject_entity_id=subject_entity_id,
        predicate=predicate,
        object_entity_id=object_entity_id,
        paragraph_id=paragraph_id,
        memory_source="na",
        cognitive_type="semantic",
    )


async def _make_entity(name: str) -> DBMemEntity:
    return await DBMemEntity.create(
        workspace_id=0,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        preset_id=7,
        origin_chat_key=CHAT,
        entity_type=EntityType.PERSON,
        name=name,
        canonical_name=name.lower(),
    )


async def _log(target_id: int, target_type: TargetType = TargetType.PARAGRAPH) -> DBMemReinforcementLog:
    return await DBMemReinforcementLog.create(
        workspace_id=0,
        target_type=target_type,
        target_id=target_id,
        trigger_source=TriggerSource.MANUAL,
    )


class TestLists:
    async def _seed(self, client: AsyncQdrantClient) -> dict[str, DBMemParagraph]:
        await _seed_instance()
        await _seed_channel()
        rows = {
            "a": await _make_paragraph(
                workspace_id=0,
                preset_id=7,
                content="缓存层方案",
                summary="缓存",
                persons=("u151",),
                event_time=_T0,
                blocked_round_hits=2,
            ),
            "b": await _make_paragraph(
                workspace_id=1,
                preset_id=7,
                content="另一条工作区记忆",
                persons=("u151",),
                cognitive_type=CognitiveType.EPISODIC,
                knowledge_type=KnowledgeType.DECISION,
                event_time=_T0 + timedelta(days=1),
                is_frozen=True,
                is_protected=True,
                blocked_round_hits=1,
            ),
            "c": await _make_paragraph(
                workspace_id=0,
                instance_key=OTHER_INSTANCE,
                origin_chat_key=OTHER_CHAT,
                preset_id=9,
                persons=("uxw",),
                content="范围外实例",
                event_time=_T0 + timedelta(days=2),
                blocked_round_hits=9,
            ),
            "d": await _make_paragraph(
                workspace_id=0,
                preset_id=9,
                origin_chat_key=OTHER_CHAT,
                persons=("uxw",),
                content="已删除",
                event_time=_T0 + timedelta(days=3),
                is_inactive=True,
                last_manual_action="delete",
            ),
            "e": await _make_paragraph(
                workspace_id=0,
                preset_id=0,
                content="隔离",
                event_time=_T0 + timedelta(days=4),
                is_inactive=True,
                quarantined_at=_T0 + timedelta(days=4),
                quarantine_reason="manual",
            ),
            "f": await _make_paragraph(
                workspace_id=0,
                preset_id=0,
                content="已清理",
                event_time=_T0 + timedelta(days=5),
                is_inactive=True,
                last_manual_action="prune",
            ),
            "g": await _make_paragraph(
                workspace_id=0,
                preset_id=7,
                content="含关键词 缓存层",
                event_time=_T0 + timedelta(days=6),
                blocked_round_hits=5,
            ),
            "h": await _make_paragraph(
                workspace_id=0,
                preset_id=7,
                content="别的",
                event_time=_T0 + timedelta(days=7),
                blocked_round_hits=3,
            ),
        }
        for row in rows.values():
            await _add_point(client, row, [])
        return rows

    async def _ids(self, scope: AdminMemoryScope, f: AdminFilter, **kwargs: Any) -> set[int]:
        _, rows = await list_paragraphs(scope, f, **kwargs)
        return {row.id for row in rows}

    async def test_partition_channel_preset_and_person(self, env: AsyncQdrantClient) -> None:
        rows = await self._seed(env)
        scope = _scope()

        assert await self._ids(scope, AdminFilter(), page_size=100) == {
            rows[key].id for key in ("a", "b", "d", "e", "f", "g", "h")
        }
        assert await self._ids(scope, AdminFilter(partition="domain"), page_size=100) == {
            rows[key].id for key in ("a", "d", "e", "f", "g", "h")
        }
        assert await self._ids(scope, AdminFilter(partition="workspace"), page_size=100) == {rows["b"].id}
        assert await self._ids(scope, AdminFilter(chat_key=OTHER_CHAT), page_size=100) == {rows["d"].id}
        assert await self._ids(scope, AdminFilter(preset_id=9), page_size=100) == {rows["d"].id}
        assert await self._ids(scope, AdminFilter(person_accounts=frozenset({"u151"})), page_size=100) == {
            rows["a"].id,
            rows["b"].id,
        }
        assert await self._ids(scope, AdminFilter(person_accounts=frozenset()), page_size=100) == set()

    async def test_status_frozen_protected_and_types(self, env: AsyncQdrantClient) -> None:
        rows = await self._seed(env)
        scope = _scope()

        assert await self._ids(scope, AdminFilter(), status="active", page_size=100) == {
            rows[key].id for key in ("a", "b", "g", "h")
        }
        assert await self._ids(scope, AdminFilter(), status="deleted", page_size=100) == {rows["d"].id}
        assert await self._ids(scope, AdminFilter(), status="pruned", page_size=100) == {rows["f"].id}
        assert await self._ids(scope, AdminFilter(), status="quarantined", page_size=100) == {rows["e"].id}
        assert await self._ids(scope, AdminFilter(), frozen=True, page_size=100) == {rows["b"].id}
        assert await self._ids(scope, AdminFilter(), protected=True, page_size=100) == {rows["b"].id}
        assert await self._ids(scope, AdminFilter(), cognitive_type="episodic", page_size=100) == {rows["b"].id}
        assert await self._ids(scope, AdminFilter(), knowledge_type="decision", page_size=100) == {rows["b"].id}

    async def test_time_keyword_sort_and_pagination(self, env: AsyncQdrantClient) -> None:
        rows = await self._seed(env)
        scope = _scope()

        assert await self._ids(
            scope,
            AdminFilter(),
            time_from=_T0 + timedelta(days=5),
            page_size=100,
        ) == {rows[key].id for key in ("f", "g", "h")}
        assert await self._ids(
            scope,
            AdminFilter(),
            time_to=_T0 + timedelta(days=1),
            page_size=100,
        ) == {rows["a"].id, rows["b"].id}
        assert await self._ids(scope, AdminFilter(), q="缓存层", page_size=100) == {
            rows["a"].id,
            rows["g"].id,
        }
        assert await self._ids(scope, AdminFilter(), q="x" * 100, page_size=100) == set()

        total, ordered = await list_paragraphs(
            scope,
            AdminFilter(),
            sort="blocked_round_hits",
            order="desc",
            page_size=100,
        )
        assert total == 7
        assert [row.id for row in ordered][:3] == [rows["g"].id, rows["h"].id, rows["a"].id]

        total, page_two = await list_paragraphs(scope, AdminFilter(), sort="blocked_round_hits", order="desc", page=2, page_size=2)
        assert total == 7
        assert len(page_two) == 2

        total, clamped = await list_paragraphs(scope, AdminFilter(), page_size=500)
        assert total == 7
        assert len(clamped) <= 100

    async def test_create_time_sort_and_invalid_values(self, env: AsyncQdrantClient) -> None:
        await self._seed(env)
        scope = _scope()
        _, ascending = await list_paragraphs(scope, AdminFilter(), sort="create_time", order="asc", page_size=100)
        assert [row.id for row in ascending] == sorted(row.id for row in ascending)

        with pytest.raises(ValidationError):
            await list_paragraphs(scope, AdminFilter(), sort="bogus")
        with pytest.raises(ValidationError):
            await list_paragraphs(scope, AdminFilter(), order="sideways")

    async def test_tenant_scope_never_leaks_outside_rows(self, env: AsyncQdrantClient) -> None:
        rows = await self._seed(env)
        await DBAdapterInstance.filter(adapter_key=ADAPTER, instance_key=INSTANCE).update(tenant_id=1)
        await _seed_instance(instance_key=OTHER_INSTANCE, tenant_id=2)
        scope = await resolve_admin_scope(_tenant_user(), instance_key=INSTANCE, adapter_key=ADAPTER)
        total, items = await list_paragraphs(scope, AdminFilter(), page_size=100)
        assert rows["c"].id not in {row.id for row in items}
        assert total == 7

    async def test_episodes_and_quarantine(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        episode_a = await _make_episode(
            title="隔离事件",
            is_inactive=True,
            quarantined_at=_T0,
            quarantine_reason="auto_blocked",
            blocked_round_hits=3,
        )
        episode_b = await _make_episode(title="正常事件")
        paragraph_a = await _make_paragraph(
            content="隔离段落",
            is_inactive=True,
            quarantined_at=_T0 + timedelta(hours=1),
            quarantine_reason="manual",
            blocked_round_hits=1,
        )
        await _make_paragraph(content="正常段落")

        assert episode_a.id != episode_b.id
        total, episodes = await list_episodes(_scope(), AdminFilter(), status="all", page_size=100)
        assert total == 2 and {episode.id for episode in episodes} == {episode_a.id, episode_b.id}
        total, episodes = await list_episodes(_scope(), AdminFilter(), status="quarantined", page_size=100)
        assert {episode.id for episode in episodes} == {episode_a.id}
        with pytest.raises(ValidationError):
            await list_episodes(_scope(), AdminFilter(person_accounts=frozenset({"u151"})))

        total, entries = await list_quarantine(_scope(), AdminFilter(), page_size=100)
        assert total == 2
        assert [entry.memory_type for entry in entries] == ["paragraph", "episode"]
        assert entries[0].id == paragraph_a.id
        assert entries[0].title_or_summary == "隔离段落"
        assert entries[0].quarantine_reason == "manual"
        assert entries[1].origin_chat_key == CHAT

    async def test_build_detail_and_trace(self, env: AsyncQdrantClient) -> None:
        await _seed_instance(bot_account="bot1")
        await _seed_channel()
        await _message(sender_id="u151", platform_userid="u151", nickname="甲", message_id="m1")
        await _message(sender_id="-1", platform_userid="bot1", nickname="机器人", message_id="m2")
        episode = await _make_episode(title="所属事件")
        paragraph = await _make_paragraph(persons=("u151",), content="详情正文", summary="详情摘要")
        paragraph.episode_id = episode.id
        paragraph.anchor_msg_id = "m1"
        await paragraph.save(update_fields=["episode_id", "anchor_msg_id"])
        await _log(paragraph.id)
        await _log(paragraph.id)

        detail = await build_paragraph_detail(paragraph)
        assert detail["status"] == "active"
        assert detail["persons"] == [{"account": "u151", "display_name": "甲"}]
        assert detail["origin_channel_name"] == "频道一"
        assert detail["episode"] == {"id": episode.id, "title": "所属事件"}
        assert detail["reinforcement_count"] == 2

        trace = await trace_paragraph(paragraph)
        assert trace.memory_id == paragraph.id
        assert trace.origin_kind == OriginKind.CONSOLIDATION.value

        assert await load_item("paragraph", paragraph.id) is not None
        assert await load_item("episode", episode.id) is not None
        assert await load_item("relation", 999) is None
        with pytest.raises(ValidationError):
            await load_item("bogus", 1)

    def test_status_helpers(self) -> None:
        assert status_q("all") is not None
        assert status_q("active") is not None
        assert status_q("pruned") is not None
        with pytest.raises(ValidationError):
            status_q("bogus")


class TestStatusActions:
    async def test_quarantine_deletes_the_vector(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        paragraph = await _make_paragraph(persons=("u151",))
        await _add_point(env, paragraph, ["u151"])
        assert await _point_count(env, paragraph.id) == 1

        await apply_action("paragraph", paragraph, "quarantine")

        await paragraph.refresh_from_db()
        assert paragraph.is_inactive is True
        assert paragraph.quarantined_at is not None
        assert paragraph.quarantine_reason == "manual"
        assert paragraph.last_manual_action == "quarantine"
        assert paragraph_status(paragraph) == "quarantined"
        assert await _point_count(env, paragraph.id) == 0

    async def test_quarantine_is_idempotent_and_rejects_deleted(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        paragraph = await _make_paragraph(
            is_inactive=True,
            quarantined_at=_T0,
            quarantine_reason="manual",
        )
        await quarantine(paragraph, reason="auto_blocked")
        await paragraph.refresh_from_db()
        assert paragraph.quarantine_reason == "manual"

        deleted = await _make_paragraph(is_inactive=True, last_manual_action="delete")
        with pytest.raises(ValidationError):
            await quarantine(deleted)

    async def test_restore_reembeds_and_resets_counters(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        paragraph = await _make_paragraph(
            content="放回的内容",
            is_inactive=True,
            quarantined_at=_T0,
            quarantine_reason="auto_blocked",
            blocked_round_hits=3,
        )

        await restore(paragraph)

        await paragraph.refresh_from_db()
        assert paragraph.is_inactive is False
        assert paragraph.quarantined_at is None
        assert paragraph.quarantine_reason == ""
        assert paragraph.blocked_round_hits == 0
        assert paragraph.last_manual_action == "restore"
        assert paragraph.embedding_ref == str(paragraph.id)
        assert await _point_count(env, paragraph.id) == 1
        assert (await _payload(env, paragraph.id))["is_inactive"] is False

    async def test_restore_embedding_failure_changes_nothing(
        self,
        env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_instance()
        paragraph = await _make_paragraph(
            is_inactive=True,
            quarantined_at=_T0,
            quarantine_reason="manual",
            blocked_round_hits=2,
        )

        async def failing_embed(_text: str, **_kwargs: object) -> list[float]:
            raise RuntimeError("嵌入服务不可用")

        monkeypatch.setattr(embedding_service_module, "embed_text", failing_embed)

        with pytest.raises(RuntimeError):
            await restore(paragraph)

        await paragraph.refresh_from_db()
        assert paragraph.is_inactive is True
        assert paragraph.quarantined_at is not None
        assert paragraph.quarantine_reason == "manual"
        assert paragraph.blocked_round_hits == 2
        assert await _point_count(env, paragraph.id) == 0

    async def test_soft_delete_clears_quarantine_and_vector(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        paragraph = await _make_paragraph(
            is_inactive=True,
            quarantined_at=_T0,
            quarantine_reason="manual",
        )
        await _add_point(env, paragraph, [])

        await soft_delete("paragraph", paragraph)

        await paragraph.refresh_from_db()
        assert paragraph.is_inactive is True
        assert paragraph.quarantined_at is None
        assert paragraph.quarantine_reason == ""
        assert paragraph.last_manual_action == "delete"
        assert await _point_count(env, paragraph.id) == 0

    async def test_soft_delete_episode_clears_member_paragraphs(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        episode = await _make_episode()
        paragraph = await _make_paragraph()
        paragraph.episode_id = episode.id
        paragraph.episode_phase = "opening"
        await paragraph.save(update_fields=["episode_id", "episode_phase"])

        await soft_delete("episode", episode)

        await episode.refresh_from_db()
        await paragraph.refresh_from_db()
        assert episode.is_inactive is True
        assert paragraph.episode_id is None
        assert paragraph.episode_phase is None

    async def test_action_whitelist(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        paragraph = await _make_paragraph()
        episode = await _make_episode()
        entity = await _make_entity("实体")

        with pytest.raises(ValidationError):
            await apply_action("entity", entity, "freeze")
        with pytest.raises(ValidationError):
            await apply_action("episode", episode, "freeze")
        with pytest.raises(ValidationError):
            await apply_action("paragraph", paragraph, "explode")

        await apply_action("paragraph", paragraph, "freeze")
        await paragraph.refresh_from_db()
        assert paragraph.is_frozen is True
        await apply_action("paragraph", paragraph, "unfreeze")
        await paragraph.refresh_from_db()
        assert paragraph.is_frozen is False

        await apply_action("paragraph", paragraph, "protect")
        await paragraph.refresh_from_db()
        assert paragraph.is_protected is True
        await apply_action("paragraph", paragraph, "unprotect")
        await paragraph.refresh_from_db()
        assert paragraph.is_protected is False

        await apply_action("paragraph", paragraph, "demote")
        await paragraph.refresh_from_db()
        assert paragraph.manual_weight_delta == pytest.approx(-0.2)
        await apply_action("paragraph", paragraph, "reinforce")
        await paragraph.refresh_from_db()
        assert paragraph.last_reinforced_at is not None

        relation = await _make_relation(paragraph_id=paragraph.id)
        for memory_type, target in (
            ("paragraph", paragraph),
            ("episode", episode),
            ("relation", relation),
            ("entity", entity),
        ):
            with pytest.raises(ValidationError):
                await apply_action(memory_type, target, "delete")

    async def test_quarantine_vector_failure_still_commits_database(
        self,
        env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_instance()
        paragraph = await _make_paragraph()
        await _add_point(env, paragraph, [])

        async def failing_delete(_ids: list[int]) -> int:
            raise RuntimeError("向量库不可用")

        monkeypatch.setattr(
            qdrant_manager_module.memory_qdrant_manager,
            "delete_paragraphs",
            failing_delete,
        )

        await quarantine(paragraph)

        await paragraph.refresh_from_db()
        assert paragraph.is_inactive is True
        assert paragraph.quarantined_at is not None

    async def test_bulk_quarantine_updates_only_active_rows(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        active = await _make_paragraph()
        deleted = await _make_paragraph(is_inactive=True, last_manual_action="delete")
        await _add_point(env, active, [])

        changed = await quarantine_paragraphs([active.id, deleted.id, 999], reason="auto_blocked")

        assert changed == [active.id]
        await active.refresh_from_db()
        assert active.quarantine_reason == "auto_blocked"
        assert active.is_inactive is True
        assert await _point_count(env, active.id) == 0


class TestQuarantineIsolation:
    @pytest.mark.parametrize("gate", [True, False])
    async def test_retriever_and_tool_search_miss_the_quarantined_row(
        self,
        env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
        gate: bool,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", gate)
        await _seed_instance()
        await _seed_channel()
        paragraph = await _make_paragraph(content="会被隔离的记忆")
        await _add_point(env, paragraph, [])

        before = await search_workspace_memories(
            0,
            "查询",
            limit=10,
            scope=MemoryScope.unrestricted(),
        )
        assert paragraph.id in {item.paragraph_id for item in before.items}

        from nekro_agent.services.memory.retriever import MemoryRetriever

        retriever = MemoryRetriever(0, scope=MemoryScope.unrestricted())
        before_retrieval = await retriever.retrieve("查询", min_similarity=0.0, record_access=False)
        assert paragraph.id in {memory.paragraph_id for memory in before_retrieval.memories}

        await quarantine(paragraph)

        after = await search_workspace_memories(
            0,
            "查询",
            limit=10,
            scope=MemoryScope.unrestricted(),
        )
        assert paragraph.id not in {item.paragraph_id for item in after.items}
        after_retrieval = await retriever.retrieve("查询", min_similarity=0.0, record_access=False)
        assert paragraph.id not in {memory.paragraph_id for memory in after_retrieval.memories}

    @pytest.mark.parametrize("gate", [True, False])
    async def test_rows_stay_out_of_recall_when_the_vector_delete_fails(
        self,
        env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
        gate: bool,
    ) -> None:
        # 删点失败时点和 payload 里的 is_inactive=false 都还在，向量通道照样命中；
        # 回表必须以库里的状态为准，隔离与软删的内容才不会被注入或被工具搜到。
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", gate)
        await _seed_instance()
        await _seed_channel()
        quarantined = await _make_paragraph(content="隔离时删点失败的记忆")
        deleted = await _make_paragraph(content="软删时删点失败的记忆")
        kept = await _make_paragraph(content="一直正常的记忆")
        for row in (quarantined, deleted, kept):
            await _add_point(env, row, [])

        async def failing_delete_one(_id: int) -> bool:
            raise RuntimeError("向量库不可用")

        async def failing_delete_many(_ids: list[int]) -> int:
            raise RuntimeError("向量库不可用")

        monkeypatch.setattr(qdrant_manager_module.memory_qdrant_manager, "delete_paragraph", failing_delete_one)
        monkeypatch.setattr(qdrant_manager_module.memory_qdrant_manager, "delete_paragraphs", failing_delete_many)

        await quarantine(quarantined)
        await soft_delete("paragraph", deleted)
        assert await _point_count(env, quarantined.id) == 1
        assert await _point_count(env, deleted.id) == 1

        from nekro_agent.services.memory.retriever import MemoryRetriever

        hidden = {quarantined.id, deleted.id}
        retrieval = await MemoryRetriever(0, scope=MemoryScope.unrestricted()).retrieve(
            "查询",
            min_similarity=0.0,
            record_access=False,
        )
        recalled = {memory.paragraph_id for memory in retrieval.memories}
        assert kept.id in recalled
        assert hidden.isdisjoint(recalled)

        tool_result = await search_workspace_memories(0, "查询", limit=10, scope=MemoryScope.unrestricted())
        found = {item.paragraph_id for item in tool_result.items}
        assert kept.id in found
        assert hidden.isdisjoint(found)


class TestEdit:
    async def _seed_conversation(self) -> DBMemParagraph:
        await _seed_instance(bot_account="bot1")
        await _seed_channel()
        await _message(sender_id="u151", platform_userid="u151", nickname="甲", message_id="m1")
        await _message(sender_id="uxw", platform_userid="uxw", nickname="乙", message_id="m2")
        await _message(sender_id="-1", platform_userid="bot1", nickname="机器人", message_id="m3")
        return await _make_paragraph(persons=("u151",), content="编辑前", embedding_ref="1")

    async def test_person_accounts_are_validated_and_synced(self, env: AsyncQdrantClient) -> None:
        paragraph = await self._seed_conversation()
        await _add_point(env, paragraph, ["u151"])

        await edit_paragraph(
            paragraph,
            content=None,
            summary="新摘要",
            person_accounts=["uxw"],
            editor="admin",
        )

        await paragraph.refresh_from_db()
        assert paragraph.summary == "新摘要"
        assert paragraph.last_manual_action == "edit"
        accounts = await DBMemParagraphPerson.filter(paragraph_id=paragraph.id).values_list("account", flat=True)
        assert set(accounts) == {"uxw"}
        assert (await _payload(env, paragraph.id))["person_accounts"] == ["uxw"]

        with pytest.raises(ValidationError):
            await edit_paragraph(paragraph, content=None, summary=None, person_accounts=["nobody"], editor="admin")
        with pytest.raises(ValidationError):
            await edit_paragraph(paragraph, content=None, summary=None, person_accounts=["bot1"], editor="admin")

    async def test_content_edit_reembeds(self, env: AsyncQdrantClient) -> None:
        paragraph = await self._seed_conversation()
        await _add_point(env, paragraph, ["u151"])
        embedded: list[str] = []

        async def recording_embed(text: str, **_kwargs: object) -> list[float]:
            embedded.append(text)
            return list(VECTOR)

        import nekro_agent.services.memory.embedding_service as embedding_module

        original = embedding_module.embed_text
        embedding_module.embed_text = recording_embed
        try:
            await edit_paragraph(
                paragraph,
                content="编辑后的正文",
                summary=None,
                person_accounts=None,
                editor="admin",
            )
        finally:
            embedding_module.embed_text = original

        await paragraph.refresh_from_db()
        assert paragraph.content == "编辑后的正文"
        assert "编辑后的正文" in embedded
        assert await _point_count(env, paragraph.id) == 1
        assert (await _payload(env, paragraph.id))["person_accounts"] == ["u151"]

    async def test_quarantined_edit_does_not_touch_vectors(
        self,
        env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paragraph = await self._seed_conversation()
        paragraph.is_inactive = True
        paragraph.quarantined_at = _T0
        paragraph.quarantine_reason = "manual"
        await paragraph.save(update_fields=["is_inactive", "quarantined_at", "quarantine_reason"])

        async def forbidden(*_args: object, **_kwargs: object) -> bool:
            raise AssertionError("隔离状态下不应写向量")

        monkeypatch.setattr(qdrant_manager_module.memory_qdrant_manager, "upsert_paragraph", forbidden)
        monkeypatch.setattr(qdrant_manager_module.memory_qdrant_manager, "set_paragraph_payload", forbidden)

        await edit_paragraph(
            paragraph,
            content="隔离状态下的编辑",
            summary=None,
            person_accounts=["uxw"],
            editor="admin",
        )
        await paragraph.refresh_from_db()
        assert paragraph.content == "隔离状态下的编辑"


class TestHardDelete:
    async def test_paragraph_cascade(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        episode = await _make_episode(title="包含待删段落")
        paragraph = await _make_paragraph(persons=("u151",), summary="待删")
        other_member = await _make_paragraph(content="同 episode 的其余成员")
        for row in (paragraph, other_member):
            row.episode_id = episode.id
            await row.save(update_fields=["episode_id"])
            await _add_point(env, row, [])
        relation = await _make_relation(paragraph_id=paragraph.id)
        await _log(paragraph.id)
        await _log(relation.id, TargetType.RELATION)

        preview = await preview_hard_delete_paragraphs([paragraph.id])
        assert preview.paragraphs == 1
        assert preview.persons == 1
        assert preview.relations == 1
        assert preview.episodes == 1
        assert preview.reinforcement_logs == 2
        assert await DBMemParagraph.get_or_none(id=paragraph.id) is not None

        report = await hard_delete_paragraphs([paragraph.id])

        assert report.paragraphs == 1
        assert report.persons == 1
        assert report.relations == 1
        assert report.episodes == 1
        assert report.reinforcement_logs == 2
        assert report.vectors_failed is False
        assert await DBMemParagraph.get_or_none(id=paragraph.id) is None
        assert await DBMemRelation.get_or_none(id=relation.id) is None
        assert await DBMemReinforcementLog.filter(target_type=TargetType.RELATION, target_id=relation.id).count() == 0
        assert await DBMemEpisode.get_or_none(id=episode.id) is None
        await other_member.refresh_from_db()
        assert other_member.episode_id is None
        assert await _point_count(env, paragraph.id) == 0
        assert await _point_count(env, other_member.id) == 1

    async def test_entity_cascade_deletes_referencing_relations(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        entity = await _make_entity("甲")
        other = await _make_entity("乙")
        relation = await _make_relation(subject_entity_id=entity.id, object_entity_id=other.id)
        await _log(relation.id, TargetType.RELATION)

        report = await hard_delete_entities([entity.id])

        assert report.entities == 1
        assert report.relations == 1
        assert report.reinforcement_logs == 1
        assert await DBMemEntity.get_or_none(id=entity.id) is None
        assert await DBMemEntity.get_or_none(id=other.id) is not None
        assert await DBMemRelation.get_or_none(id=relation.id) is None

    async def test_episode_and_relation_forms(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        episode = await _make_episode(title="待删事件")
        member = await _make_paragraph()
        member.episode_id = episode.id
        member.episode_phase = "opening"
        await member.save(update_fields=["episode_id", "episode_phase"])

        report = await admin_service.hard_delete_episodes([episode.id])

        assert report.episodes == 1
        assert await DBMemEpisode.get_or_none(id=episode.id) is None
        await member.refresh_from_db()
        assert member.episode_id is None
        assert member.episode_phase is None
        assert await DBMemParagraph.get_or_none(id=member.id) is not None

        relation = await _make_relation(paragraph_id=member.id)
        await _log(relation.id, TargetType.RELATION)
        report = await admin_service.hard_delete_relations([relation.id])

        assert report.relations == 1
        assert report.reinforcement_logs == 1
        assert await DBMemRelation.get_or_none(id=relation.id) is None

    async def test_batching_uses_more_than_one_chunk(
        self,
        env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_instance()
        rows = [await _make_paragraph(content=f"分批 {index}") for index in range(5)]
        monkeypatch.setattr(admin_service, "_BATCH_SIZE", 2)

        preview = await preview_hard_delete_paragraphs([row.id for row in rows])
        assert preview.paragraphs == 5

        report = await hard_delete_paragraphs([row.id for row in rows])

        assert report.paragraphs == 5
        assert await DBMemParagraph.filter(id__in=[row.id for row in rows]).count() == 0

    async def test_vector_failure_is_reported_after_pg_commit(
        self,
        env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_instance()
        paragraph = await _make_paragraph()
        await _add_point(env, paragraph, [])

        async def failing_delete(_ids: list[int]) -> int:
            raise RuntimeError("向量库不可用")

        monkeypatch.setattr(
            qdrant_manager_module.memory_qdrant_manager,
            "delete_paragraphs",
            failing_delete,
        )

        report = await hard_delete_paragraphs([paragraph.id])

        assert report.vectors_failed is True
        assert report.paragraphs == 1
        assert await DBMemParagraph.get_or_none(id=paragraph.id) is None


class TestBulk:
    async def _seed(self, env: AsyncQdrantClient) -> tuple[AdminMemoryScope, DBMemParagraph, DBMemParagraph, DBMemParagraph]:
        await _seed_instance()
        first = await _make_paragraph(content="批量一")
        second = await _make_paragraph(content="批量二")
        outside = await _make_paragraph(instance_key=OTHER_INSTANCE, origin_chat_key=OTHER_CHAT, content="范围外")
        for row in (first, second):
            await _add_point(env, row, [])
        return _scope(), first, second, outside

    async def test_dry_run_and_expected_count(self, env: AsyncQdrantClient) -> None:
        scope, first, second, _ = await self._seed(env)
        filters = ParagraphListFilters(status="active")

        preview = await bulk_paragraphs(
            scope,
            ids=None,
            f=AdminFilter(),
            filters=filters,
            action="delete",
            dry_run=True,
            expected_count=None,
        )
        assert preview.matched == 2
        assert preview.affected == 0
        assert preview.cascade is None
        assert (await DBMemParagraph.get(id=first.id)).is_inactive is False

        with pytest.raises(ConflictError):
            await bulk_paragraphs(
                scope,
                ids=None,
                f=AdminFilter(),
                filters=filters,
                action="delete",
                dry_run=False,
                expected_count=99,
            )
        assert (await DBMemParagraph.get(id=first.id)).is_inactive is False

        result = await bulk_paragraphs(
            scope,
            ids=None,
            f=AdminFilter(),
            filters=filters,
            action="delete",
            dry_run=False,
            expected_count=2,
        )
        assert result.matched == 2
        assert result.affected == 2
        assert (await DBMemParagraph.get(id=first.id)).is_inactive is True
        assert (await DBMemParagraph.get(id=second.id)).is_inactive is True
        assert await _point_count(env, first.id) == 0

    async def test_ids_are_filtered_by_scope_silently(self, env: AsyncQdrantClient) -> None:
        scope, first, _, outside = await self._seed(env)

        result = await bulk_paragraphs(
            scope,
            ids=[first.id, outside.id],
            f=None,
            filters=None,
            action="delete",
            dry_run=False,
            expected_count=1,
        )

        assert result.matched == 1
        assert result.affected == 1
        await outside.refresh_from_db()
        assert outside.is_inactive is False

    async def test_bulk_delete_also_takes_quarantined_rows(self, env: AsyncQdrantClient) -> None:
        # 隔离区页面上的批量删除就是从隔离状态删，与单条软删一致；已删除的行不再被改写。
        scope, first, second, _ = await self._seed(env)
        await quarantine(first)
        already_deleted = await _make_paragraph(is_inactive=True, last_manual_action="prune", content="已清理")

        result = await bulk_paragraphs(
            scope,
            ids=[first.id, second.id, already_deleted.id],
            f=None,
            filters=None,
            action="delete",
            dry_run=False,
            expected_count=3,
        )

        assert result.affected == 2
        first_row = await DBMemParagraph.get(id=first.id)
        assert first_row.is_inactive is True
        assert first_row.quarantined_at is None
        assert first_row.last_manual_action == "delete"
        assert (await DBMemParagraph.get(id=already_deleted.id)).last_manual_action == "prune"

    async def test_restore_limit_and_failed_ids(
        self,
        env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_instance()
        first = await _make_paragraph(is_inactive=True, quarantined_at=_T0, quarantine_reason="manual")
        second = await _make_paragraph(is_inactive=True, quarantined_at=_T0, quarantine_reason="manual")
        normal = await _make_paragraph()
        scope = _scope()

        monkeypatch.setattr(admin_service, "_BULK_RESTORE_LIMIT", 1)
        with pytest.raises(ValidationError):
            await bulk_paragraphs(
                scope,
                ids=[first.id, second.id],
                f=None,
                filters=None,
                action="restore",
                dry_run=False,
                expected_count=2,
            )
        monkeypatch.setattr(admin_service, "_BULK_RESTORE_LIMIT", 200)

        result = await bulk_paragraphs(
            scope,
            ids=[first.id, second.id, normal.id],
            f=None,
            filters=None,
            action="restore",
            dry_run=False,
            expected_count=3,
        )
        assert result.matched == 3
        assert result.affected == 2
        assert result.failed_ids == [normal.id]

    async def test_hard_delete_preview_reports_cascade(self, env: AsyncQdrantClient) -> None:
        scope, first, second, _ = await self._seed(env)
        result = await bulk_paragraphs(
            scope,
            ids=[first.id, second.id],
            f=None,
            filters=None,
            action="hard_delete",
            dry_run=True,
            expected_count=None,
        )
        assert result.matched == 2
        assert result.cascade is not None
        assert result.cascade.paragraphs == 2
        assert await DBMemParagraph.get_or_none(id=first.id) is not None

    async def test_filter_selection_intersects_scope(self, env: AsyncQdrantClient) -> None:
        scope, first, second, outside = await self._seed(env)
        result = await bulk_paragraphs(
            scope,
            ids=None,
            f=AdminFilter(),
            filters=ParagraphListFilters(status="active"),
            action="freeze",
            dry_run=False,
            expected_count=2,
        )
        assert result.matched == 2
        assert result.affected == 2
        await first.refresh_from_db()
        await second.refresh_from_db()
        await outside.refresh_from_db()
        assert first.is_frozen is True
        assert second.is_frozen is True
        assert outside.is_frozen is False


class TestScopeOptions:
    async def test_platform_options(self, env: AsyncQdrantClient) -> None:
        first_instance = await _seed_instance()
        second_instance = await _seed_instance(instance_key=OTHER_INSTANCE, tenant_id=2)
        await _seed_channel()
        await _seed_channel(chat_key=OTHER_CHAT, instance_key=OTHER_INSTANCE, channel_id="group_2", channel_name="频道二")
        await DBPreset.create(
            id=7,
            name="preset-seven",
            title="人设七",
            avatar="",
            content="",
            description="",
            tags="",
            author="tester",
        )
        await _make_paragraph(preset_id=7)
        await _make_paragraph(preset_id=0, content="无人设")
        await _make_paragraph(preset_id=9, instance_key=OTHER_INSTANCE, origin_chat_key=OTHER_CHAT)

        options = await scope_options(_platform_user(), AdminMemoryScope.platform_all())

        assert {item["instance_key"] for item in options["instances"]} == {
            first_instance.instance_key,
            second_instance.instance_key,
        }
        assert {item["chat_key"] for item in options["channels"]} == {CHAT, OTHER_CHAT}
        assert {item["preset_id"] for item in options["presets"]} == {0, 7, 9}
        assert {item["name"] for item in options["presets"]} >= {"无人设档", "人设七"}

    async def test_tenant_options(self, env: AsyncQdrantClient) -> None:
        await _seed_instance(tenant_id=1)
        await _seed_instance(instance_key=OTHER_INSTANCE, tenant_id=2)
        await _seed_channel()
        await _seed_channel(chat_key=OTHER_CHAT, instance_key=OTHER_INSTANCE, channel_id="group_2")
        await _make_paragraph(preset_id=7)
        await _make_paragraph(preset_id=9, instance_key=OTHER_INSTANCE, origin_chat_key=OTHER_CHAT)

        scope = await resolve_admin_scope(_tenant_user(), instance_key=INSTANCE, adapter_key=ADAPTER)
        options = await scope_options(_tenant_user(), scope)

        assert {item["instance_key"] for item in options["instances"]} == {INSTANCE}
        assert {item["chat_key"] for item in options["channels"]} == {CHAT}
        assert {item["preset_id"] for item in options["presets"]} == {7}

    async def test_console_only_bucket_is_named_not_reported_as_a_deleted_preset(
        self,
        env: AsyncQdrantClient,
    ) -> None:
        """负数档要有自己的名字，不能被显示成「已删除的人设 #-1」。

        那句话会让管理员以为数据坏了、去清一件不该清的东西（控制台直发 CC 的结论）。
        判据落在**显示名**上而不是 `preset_id` 上：负数档出现在筛选栏里是预期的，
        出现时叫什么是这条用例的内容。
        """
        await _seed_instance(tenant_id=1)
        await _seed_channel()
        await _make_paragraph(preset_id=7)
        await _make_paragraph(preset_id=CONSOLE_ONLY_PRESET_ID, content="控制台直发的结论")

        options = await scope_options(_platform_user(), AdminMemoryScope.platform_all())
        by_id = {item["preset_id"]: item["name"] for item in options["presets"]}

        assert by_id[CONSOLE_ONLY_PRESET_ID] == "仅控制台（控制台直发 CC 的结论）"
        assert "已删除" not in by_id[CONSOLE_ONLY_PRESET_ID]


class TestPersonFilter:
    async def test_platform_all_uses_only_the_account(self, env: AsyncQdrantClient) -> None:
        assert await resolve_person_filter(
            AdminMemoryScope.platform_all(),
            "uxw",
            adapter_key="",
            instance_key="",
        ) == frozenset({"uxw"})

    async def test_selected_instance_expands_aliases(self, env: AsyncQdrantClient) -> None:
        await _seed_instance()
        await DBIdentityAlias.create(
            domain_id=f"i:{ADAPTER}:{INSTANCE}",
            platform_userid="uxw",
            person_uid="u151",
        )
        resolved = await resolve_person_filter(
            _scope(),
            "uxw",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
        )
        assert resolved == frozenset({"uxw", "u151"})

    async def test_blank_account_means_no_person_filter(self, env: AsyncQdrantClient) -> None:
        assert await resolve_person_filter(
            AdminMemoryScope.platform_all(),
            "   ",
            adapter_key="",
            instance_key="",
        ) == frozenset()
