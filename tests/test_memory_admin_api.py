"""记忆管理路由的行为用例

直接调用路由函数（与频道开关那条用例同一做法），跑在真表（SQLite 内存库）、本地 Qdrant
与确定性假嵌入上。要钉住三件事：

1. 可见范围只来自身份：平台方的全量、租户的实例与域、筛选参数（频道 / 人 / 分区）都不能
   把范围外的行带出来；
2. 「不存在」与「不是你的」在按 ID 的每个写接口上逐字相同，且失败后库里一行都没变；
3. 真删的连带范围与报告条数对得上。

嵌入与向量库都用本地的：这些用例的价值在顺序与归属，不在网络调用。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_channel_summary_state import DBMemChannelSummaryState
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_mem_reinforcement_log import (
    DBMemReinforcementLog,
    TargetType,
    TriggerSource,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import memory_admin
from nekro_agent.schemas.errors import ConflictError, NotFoundError, ValidationError
from nekro_agent.schemas.memory_admin import (
    BulkParagraphsRequest,
    BulkSelection,
    MemoryItemActionRequest,
    MemoryItemUpdateRequest,
    ParagraphFilterSelection,
    SummaryEditRequest,
    SummaryPauseRequest,
    SummaryRollbackRequest,
)
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.memory import admin_service
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.qdrant_manager import MEMORY_PARAGRAPH_COLLECTION
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
VECTOR = [0.1, 0.2, 0.3, 0.4]
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

CHAT_A1 = "onebot_v11-a1-group_1"
CHAT_A2 = "onebot_v11-a2-group_2"
CHAT_B1 = "onebot_v11-b1-group_9"
CHAT_P1 = "onebot_v11-p1-group_7"


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


@dataclass
class World:
    client: AsyncQdrantClient
    platform: DBUser
    tenant: DBUser
    group: DBInstanceGroup
    instances: dict[str, DBAdapterInstance]
    channels: dict[str, DBChatChannel]
    paragraphs: dict[str, DBMemParagraph]
    episodes: dict[str, DBMemEpisode]
    relations: dict[str, DBMemRelation]
    entities: dict[str, DBMemEntity]


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
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
        yield await _seed_world(client)
    finally:
        await client.close()
        await Tortoise.close_connections()


async def _instance(
    instance_key: str,
    *,
    tenant_id: Optional[int],
    display_name: str,
    bot_account: str,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=display_name,
        tenant_id=tenant_id,
        provider_account_id=bot_account,
    )


async def _channel(
    chat_key: str,
    *,
    instance_key: str,
    channel_id: str,
    name: str,
) -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=channel_id,
        channel_name=name,
        channel_type="group",
        data="{}",
        workspace_id=None,
    )


async def _message(chat_key: str, *, account: str, message_id: str) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=account,
        sender_name=account,
        sender_nickname=account,
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key="",
        message_id=message_id,
        chat_key=chat_key,
        chat_type="group",
        platform_userid=account,
        content_text="消息",
        content_data="{}",
        raw_cq_code="",
        ext_data="",
        send_timestamp=1_700_000,
    )


async def _paragraph(
    *,
    instance_key: str,
    adapter_key: str,
    workspace_id: int,
    preset_id: int,
    origin_chat_key: Optional[str],
    chat_type: str,
    content: str,
    persons: tuple[str, ...] = (),
) -> DBMemParagraph:
    row = await DBMemParagraph.create(
        workspace_id=workspace_id,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        origin_chat_key=origin_chat_key,
        origin_chat_type=chat_type,
        memory_source="na",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content=content,
        summary=content[:16],
        origin_kind=OriginKind.CONSOLIDATION,
        event_time=_T0,
    )
    for account in persons:
        await DBMemParagraphPerson.create(paragraph_id=row.id, account=account)
    return row


async def _episode(
    *,
    instance_key: str,
    adapter_key: str,
    workspace_id: int,
    preset_id: int,
    origin_chat_key: str,
    title: str,
    paragraph_ids: list[int],
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
        paragraph_ids=paragraph_ids,
        participant_entity_ids=[],
        phase_mapping={},
    )


async def _relation(
    *,
    instance_key: str,
    workspace_id: int,
    paragraph_id: int,
    predicate: str,
) -> DBMemRelation:
    return await DBMemRelation.create(
        workspace_id=workspace_id,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=7,
        origin_chat_key=CHAT_A1,
        subject_entity_id=1,
        predicate=predicate,
        object_entity_id=2,
        paragraph_id=paragraph_id,
        memory_source="na",
        cognitive_type="semantic",
    )


async def _entity(*, instance_key: str, name: str) -> DBMemEntity:
    return await DBMemEntity.create(
        workspace_id=0,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=7,
        origin_chat_key=CHAT_A1,
        entity_type=EntityType.PERSON,
        name=name,
        canonical_name=name.lower(),
    )


async def _log(*, target_id: int, target_type: TargetType) -> DBMemReinforcementLog:
    return await DBMemReinforcementLog.create(
        workspace_id=0,
        target_type=target_type,
        target_id=target_id,
        trigger_source=TriggerSource.MANUAL,
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


def _snapshot(row: Any) -> dict[str, Any]:
    data = dict(row.to_dict())
    update_time = getattr(row, "update_time", None)
    data["update_time"] = update_time.isoformat() if update_time else None
    return data


async def _expect_not_found(awaitable: Any, expected: str) -> None:
    with pytest.raises(NotFoundError) as exc:
        await awaitable
    assert str(exc.value) == expected


async def _seed_world(client: AsyncQdrantClient) -> World:
    platform = _platform_user()
    tenant = _tenant_user()

    group = await DBInstanceGroup.create(name="租户一记忆域", tenant_id=1)
    instances = {
        "a1": await _instance("a1", tenant_id=1, display_name="租户一甲", bot_account="bot_a1"),
        "a2": await _instance("a2", tenant_id=1, display_name="租户一乙", bot_account="bot_a2"),
        "b1": await _instance("b1", tenant_id=2, display_name="租户二", bot_account="bot_b1"),
        "p1": await _instance("p1", tenant_id=None, display_name="平台实例", bot_account="bot_p1"),
    }
    await DBInstanceDomainBinding.bind_to_groups(ADAPTER, "a1", memory_group=group)
    await DBInstanceDomainBinding.bind_to_groups(ADAPTER, "a2", memory_group=group)

    channels = {
        "a1": await _channel(CHAT_A1, instance_key="a1", channel_id="group_1", name="租户一群"),
        "a2": await _channel(CHAT_A2, instance_key="a2", channel_id="group_2", name="租户一另一群"),
        "b1": await _channel(CHAT_B1, instance_key="b1", channel_id="group_9", name="租户二群"),
        "p1": await _channel(CHAT_P1, instance_key="p1", channel_id="group_7", name="平台群"),
    }

    await _message(CHAT_A1, account="u1", message_id="m-a1")
    await _message(CHAT_B1, account="ub1", message_id="m-b1")
    # 平台实例 p1 自成一域：别名落在 `i:适配器:实例` 上，它正好能验证按人筛选时用的是
    # 实例行上的适配器，而不是请求里那个可能为空的 adapter_key。
    await DBIdentityAlias.create(
        domain_id=f"i:{ADAPTER}:p1",
        platform_userid="up1",
        person_uid="up151",
    )
    await DBIdentityAlias.create(
        domain_id=f"i:{ADAPTER}:p1",
        platform_userid="up1alias",
        person_uid="up151",
    )

    paragraphs = {
        "a1_main": await _paragraph(
            instance_key="a1",
            adapter_key=ADAPTER,
            workspace_id=0,
            preset_id=7,
            origin_chat_key=CHAT_A1,
            chat_type="group",
            content="租户一的段落甲",
            persons=("u1",),
        ),
        "a1_other": await _paragraph(
            instance_key="a1",
            adapter_key=ADAPTER,
            workspace_id=0,
            preset_id=7,
            origin_chat_key=CHAT_A1,
            chat_type="group",
            content="租户一的另一段",
        ),
        "a2_main": await _paragraph(
            instance_key="a2",
            adapter_key=ADAPTER,
            workspace_id=0,
            preset_id=7,
            origin_chat_key=CHAT_A2,
            chat_type="group",
            content="租户一在乙实例的段落",
        ),
        "b1_main": await _paragraph(
            instance_key="b1",
            adapter_key=ADAPTER,
            workspace_id=0,
            preset_id=9,
            origin_chat_key=CHAT_B1,
            chat_type="group",
            content="租户二的段落",
            persons=("ub1",),
        ),
        "p1_main": await _paragraph(
            instance_key="p1",
            adapter_key=ADAPTER,
            workspace_id=0,
            preset_id=7,
            origin_chat_key=CHAT_P1,
            chat_type="group",
            content="平台实例的段落",
            persons=("up1",),
        ),
        "p1_alias": await _paragraph(
            instance_key="p1",
            adapter_key=ADAPTER,
            workspace_id=0,
            preset_id=7,
            origin_chat_key=CHAT_P1,
            chat_type="group",
            content="平台实例的别名段落",
            persons=("up1alias",),
        ),
        "legacy": await _paragraph(
            instance_key="",
            adapter_key="",
            workspace_id=3,
            preset_id=0,
            origin_chat_key=None,
            chat_type="",
            content="升级前的存量工作区行",
        ),
    }
    for row in paragraphs.values():
        await _add_point(client, row, [])

    episodes = {
        "a1": await _episode(
            instance_key="a1",
            adapter_key=ADAPTER,
            workspace_id=0,
            preset_id=7,
            origin_chat_key=CHAT_A1,
            title="租户一的事件",
            paragraph_ids=[paragraphs["a1_main"].id, paragraphs["a1_other"].id],
        ),
        "b1": await _episode(
            instance_key="b1",
            adapter_key=ADAPTER,
            workspace_id=0,
            preset_id=9,
            origin_chat_key=CHAT_B1,
            title="租户二的事件",
            paragraph_ids=[paragraphs["b1_main"].id],
        ),
    }
    paragraphs["a1_main"].episode_id = episodes["a1"].id
    await paragraphs["a1_main"].save(update_fields=["episode_id"])
    paragraphs["a1_other"].episode_id = episodes["a1"].id
    await paragraphs["a1_other"].save(update_fields=["episode_id"])
    paragraphs["b1_main"].episode_id = episodes["b1"].id
    await paragraphs["b1_main"].save(update_fields=["episode_id"])

    relations = {
        "a1": await _relation(
            instance_key="a1",
            workspace_id=0,
            paragraph_id=paragraphs["a1_main"].id,
            predicate="likes",
        ),
        "b1": await _relation(
            instance_key="b1",
            workspace_id=0,
            paragraph_id=paragraphs["b1_main"].id,
            predicate="knows",
        ),
    }
    entities = {
        "a1": await _entity(instance_key="a1", name="甲"),
        "b1": await _entity(instance_key="b1", name="乙"),
    }
    await _log(target_id=paragraphs["a1_main"].id, target_type=TargetType.PARAGRAPH)
    await _log(target_id=relations["a1"].id, target_type=TargetType.RELATION)

    return World(
        client=client,
        platform=platform,
        tenant=tenant,
        group=group,
        instances=instances,
        channels=channels,
        paragraphs=paragraphs,
        episodes=episodes,
        relations=relations,
        entities=entities,
    )


def _ids(response: Any) -> set[int]:
    return {int(item.id) for item in response.items}


class TestPlatformScope:
    async def test_without_instance_sees_everything(self, world: World) -> None:
        response = await memory_admin.list_paragraphs(
            _current_user=world.platform,
            status="all",
            page_size=100,
        )

        assert _ids(response) == {row.id for row in world.paragraphs.values()}
        legacy = next(item for item in response.items if item.id == world.paragraphs["legacy"].id)
        assert legacy.adapter_key == ""
        assert legacy.instance_key == ""

    async def test_selected_instance_and_domain(self, world: World) -> None:
        own = await memory_admin.list_paragraphs(
            _current_user=world.platform,
            instance_key="a1",
            status="all",
            page_size=100,
        )
        assert _ids(own) == {world.paragraphs[key].id for key in ("a1_main", "a1_other")}

        grouped = await memory_admin.list_paragraphs(
            _current_user=world.platform,
            instance_key="a1",
            domain="group",
            status="all",
            page_size=100,
        )
        assert _ids(grouped) == {world.paragraphs[key].id for key in ("a1_main", "a1_other", "a2_main")}

    async def test_group_filter_without_instance(self, world: World) -> None:
        response = await memory_admin.list_paragraphs(
            _current_user=world.platform,
            group_id=world.group.id,
            status="all",
            page_size=100,
        )
        assert _ids(response) == {world.paragraphs[key].id for key in ("a1_main", "a1_other", "a2_main")}

    async def test_person_filter_expands_alias_through_the_instance_row(self, world: World) -> None:
        # 请求里 adapter_key 留空；别名展开必须用实例行上的适配器，否则域解析不到 p1，
        # 副号那一行就会被漏掉。
        response = await memory_admin.list_paragraphs(
            _current_user=world.platform,
            instance_key="p1",
            person="up1",
            status="all",
            page_size=100,
        )
        assert _ids(response) == {world.paragraphs["p1_main"].id, world.paragraphs["p1_alias"].id}

    async def test_episode_list_is_scoped_too(self, world: World) -> None:
        response = await memory_admin.list_episodes(
            _current_user=world.platform,
            status="all",
            page_size=100,
        )
        assert _ids(response) == {world.episodes["a1"].id, world.episodes["b1"].id}

        narrowed = await memory_admin.list_episodes(
            _current_user=world.platform,
            instance_key="a1",
            status="all",
            page_size=100,
        )
        assert _ids(narrowed) == {world.episodes["a1"].id}


class TestListFilters:
    async def test_partition_preset_keyword_type_time_and_pagination(self, world: World) -> None:
        workspace = await memory_admin.list_paragraphs(
            _current_user=world.platform,
            partition="workspace",
            status="all",
            page_size=100,
        )
        assert _ids(workspace) == {world.paragraphs["legacy"].id}

        page = await memory_admin.list_paragraphs(
            _current_user=world.platform,
            instance_key="p1",
            preset_id=7,
            status="all",
            page=1,
            page_size=1,
        )
        assert page.total == 2
        assert len(page.items) == 1

        keyword = await memory_admin.list_paragraphs(
            _current_user=world.platform,
            instance_key="p1",
            q="别名",
            status="all",
            page_size=100,
        )
        assert _ids(keyword) == {world.paragraphs["p1_alias"].id}

        typed = await memory_admin.list_paragraphs(
            _current_user=world.platform,
            instance_key="p1",
            cognitive_type="semantic",
            knowledge_type="fact",
            time_from="2025-12-31T00:00:00+00:00",
            time_to="2026-01-02T00:00:00+00:00",
            status="all",
            page_size=100,
        )
        assert typed.total == 2

    async def test_unknown_domain_is_a_validation_error(self, world: World) -> None:
        with pytest.raises(ValidationError):
            await memory_admin.list_paragraphs(
                _current_user=world.tenant,
                instance_key="a1",
                domain="everything",
            )


class TestTenantScope:
    async def test_tenant_requires_instance(self, world: World) -> None:
        with pytest.raises(ValidationError):
            await memory_admin.list_paragraphs(_current_user=world.tenant, status="all")

    async def test_tenant_cannot_use_another_tenants_instance(self, world: World) -> None:
        with pytest.raises(NotFoundError):
            await memory_admin.list_paragraphs(
                _current_user=world.tenant,
                instance_key="b1",
                status="all",
            )

    async def test_tenant_sees_only_the_selected_instance(self, world: World) -> None:
        response = await memory_admin.list_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            status="all",
            page_size=100,
        )
        assert _ids(response) == {world.paragraphs[key].id for key in ("a1_main", "a1_other")}

    async def test_tenant_domain_includes_group_members(self, world: World) -> None:
        response = await memory_admin.list_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            domain="group",
            status="all",
            page_size=100,
        )
        assert _ids(response) == {world.paragraphs[key].id for key in ("a1_main", "a1_other", "a2_main")}

    async def test_foreign_chat_key_is_just_an_empty_filter(self, world: World) -> None:
        response = await memory_admin.list_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            chat_key=CHAT_B1,
            status="all",
            page_size=100,
        )
        assert _ids(response) == set()


class TestItemAccess:
    async def test_foreign_and_missing_items_are_indistinguishable(self, world: World) -> None:
        with pytest.raises(NotFoundError) as missing:
            await memory_admin.get_memory_item(
                _current_user=world.tenant,
                memory_type="paragraph",
                item_id=999_999,
            )
        expected = str(missing.value)

        foreign = [
            ("paragraph", world.paragraphs["b1_main"].id),
            ("episode", world.episodes["b1"].id),
            ("relation", world.relations["b1"].id),
            ("entity", world.entities["b1"].id),
            ("paragraph", world.paragraphs["legacy"].id),
        ]
        snapshots = {
            ("paragraph", world.paragraphs["b1_main"].id): _snapshot(world.paragraphs["b1_main"]),
            ("episode", world.episodes["b1"].id): _snapshot(world.episodes["b1"]),
            ("relation", world.relations["b1"].id): _snapshot(world.relations["b1"]),
            ("entity", world.entities["b1"].id): _snapshot(world.entities["b1"]),
            ("paragraph", world.paragraphs["legacy"].id): _snapshot(world.paragraphs["legacy"]),
        }

        for memory_type, item_id in foreign:
            await _expect_not_found(
                memory_admin.get_memory_item(
                    _current_user=world.tenant,
                    memory_type=memory_type,
                    item_id=item_id,
                ),
                expected,
            )
            await _expect_not_found(
                memory_admin.update_memory_item(
                    _current_user=world.tenant,
                    memory_type=memory_type,
                    item_id=item_id,
                    body=MemoryItemUpdateRequest(content="尝试改写"),
                ),
                expected,
            )
            await _expect_not_found(
                memory_admin.act_on_memory_item(
                    _current_user=world.tenant,
                    memory_type=memory_type,
                    item_id=item_id,
                    body=MemoryItemActionRequest(action="delete"),
                ),
                expected,
            )
            await _expect_not_found(
                memory_admin.delete_memory_item(
                    _current_user=world.tenant,
                    memory_type=memory_type,
                    item_id=item_id,
                    hard=False,
                ),
                expected,
            )
            await _expect_not_found(
                memory_admin.delete_memory_item(
                    _current_user=world.tenant,
                    memory_type=memory_type,
                    item_id=item_id,
                    hard=True,
                ),
                expected,
            )

        await _expect_not_found(
            memory_admin.trace_memory_item(
                _current_user=world.tenant,
                item_id=world.paragraphs["b1_main"].id,
            ),
            expected,
        )
        await _expect_not_found(
            memory_admin.trace_memory_item(
                _current_user=world.tenant,
                item_id=world.paragraphs["legacy"].id,
            ),
            expected,
        )

        for (memory_type, item_id), before in snapshots.items():
            row = await admin_service.load_item(memory_type, item_id)
            assert row is not None
            assert _snapshot(row) == before
        assert await _point_count(world.client, world.paragraphs["b1_main"].id) == 1
        assert await _point_count(world.client, world.paragraphs["legacy"].id) == 1

    async def test_relation_and_entity_edit_is_rejected_for_own_rows(self, world: World) -> None:
        with pytest.raises(ValidationError):
            await memory_admin.update_memory_item(
                _current_user=world.tenant,
                memory_type="relation",
                item_id=world.relations["a1"].id,
                body=MemoryItemUpdateRequest(content="不该被接受"),
            )
        with pytest.raises(ValidationError):
            await memory_admin.update_memory_item(
                _current_user=world.tenant,
                memory_type="entity",
                item_id=world.entities["a1"].id,
                body=MemoryItemUpdateRequest(content="不该被接受"),
            )

    async def test_invalid_memory_type_is_a_validation_error(self, world: World) -> None:
        with pytest.raises(ValidationError):
            await memory_admin.get_memory_item(
                _current_user=world.tenant,
                memory_type="bogus",
                item_id=1,
            )

    async def test_bad_time_string_is_a_validation_error(self, world: World) -> None:
        with pytest.raises(ValidationError):
            await memory_admin.list_paragraphs(
                _current_user=world.tenant,
                instance_key="a1",
                time_from="2026-13-99",
            )
        with pytest.raises(ValidationError):
            await memory_admin.list_episodes(
                _current_user=world.tenant,
                instance_key="a1",
                time_to="not-a-time",
            )


class TestOwnItemLifecycle:
    async def test_quarantine_restore_soft_and_hard_delete(self, world: World) -> None:
        paragraph = world.paragraphs["a1_main"]
        other = world.paragraphs["a1_other"]

        await memory_admin.act_on_memory_item(
            _current_user=world.tenant,
            memory_type="paragraph",
            item_id=paragraph.id,
            body=MemoryItemActionRequest(action="quarantine"),
        )
        active = await memory_admin.list_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            status="active",
            page_size=100,
        )
        assert paragraph.id not in _ids(active)
        quarantined = await memory_admin.list_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            status="quarantined",
            page_size=100,
        )
        assert paragraph.id in _ids(quarantined)
        quarantine_list = await memory_admin.list_quarantine(
            _current_user=world.tenant,
            instance_key="a1",
            page_size=100,
        )
        assert paragraph.id in _ids(quarantine_list)

        await memory_admin.act_on_memory_item(
            _current_user=world.tenant,
            memory_type="paragraph",
            item_id=paragraph.id,
            body=MemoryItemActionRequest(action="restore"),
        )
        active = await memory_admin.list_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            status="active",
            page_size=100,
        )
        assert paragraph.id in _ids(active)
        quarantine_list = await memory_admin.list_quarantine(
            _current_user=world.tenant,
            instance_key="a1",
            page_size=100,
        )
        assert paragraph.id not in _ids(quarantine_list)
        assert await _point_count(world.client, paragraph.id) == 1

        soft_report = await memory_admin.delete_memory_item(
            _current_user=world.tenant,
            memory_type="paragraph",
            item_id=paragraph.id,
            hard=False,
        )
        assert soft_report.paragraphs == 1
        assert soft_report.episodes == 0
        assert soft_report.relations == 0
        deleted = await memory_admin.list_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            status="deleted",
            page_size=100,
        )
        assert paragraph.id in _ids(deleted)
        assert await _point_count(world.client, paragraph.id) == 0

        hard_report = await memory_admin.delete_memory_item(
            _current_user=world.tenant,
            memory_type="paragraph",
            item_id=paragraph.id,
            hard=True,
        )
        assert hard_report.paragraphs == 1
        assert hard_report.persons == 1
        assert hard_report.relations == 1
        assert hard_report.episodes == 1
        assert hard_report.reinforcement_logs == 2
        assert hard_report.vectors_failed is False

        with pytest.raises(NotFoundError):
            await memory_admin.get_memory_item(
                _current_user=world.tenant,
                memory_type="paragraph",
                item_id=paragraph.id,
            )
        assert not await DBMemParagraphPerson.filter(paragraph_id=paragraph.id).exists()
        assert not await DBMemRelation.filter(id=world.relations["a1"].id).exists()
        assert not await DBMemEpisode.filter(id=world.episodes["a1"].id).exists()
        assert await _point_count(world.client, paragraph.id) == 0

        refreshed_other = await DBMemParagraph.get(id=other.id)
        assert refreshed_other.episode_id is None
        assert refreshed_other.is_inactive is False

    async def test_episode_hard_delete_only_removes_the_episode(self, world: World) -> None:
        report = await memory_admin.delete_memory_item(
            _current_user=world.tenant,
            memory_type="episode",
            item_id=world.episodes["a1"].id,
            hard=True,
        )
        assert report.episodes == 1
        assert report.paragraphs == 0
        assert await DBMemParagraph.filter(id=world.paragraphs["a1_main"].id).exists()
        refreshed = await DBMemParagraph.get(id=world.paragraphs["a1_main"].id)
        assert refreshed.episode_id is None


class TestBulk:
    async def test_dry_run_excludes_foreign_ids_and_execution_leaves_them_alone(self, world: World) -> None:
        own_id = world.paragraphs["a1_main"].id
        foreign = world.paragraphs["b1_main"]

        preview = await memory_admin.bulk_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            body=BulkParagraphsRequest(
                selection=BulkSelection(ids=[own_id, foreign.id]),
                action="freeze",
                dry_run=True,
            ),
        )
        assert preview.matched == 1
        assert preview.affected == 0

        result = await memory_admin.bulk_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            body=BulkParagraphsRequest(
                selection=BulkSelection(ids=[own_id, foreign.id]),
                action="freeze",
                expected_count=1,
            ),
        )
        assert result.matched == 1
        assert result.affected == 1
        assert (await DBMemParagraph.get(id=own_id)).is_frozen is True
        assert (await DBMemParagraph.get(id=foreign.id)).is_frozen is False

    async def test_expected_count_mismatch_is_a_conflict_without_changes(self, world: World) -> None:
        paragraph = world.paragraphs["a1_main"]
        with pytest.raises(ConflictError):
            await memory_admin.bulk_paragraphs(
                _current_user=world.tenant,
                instance_key="a1",
                body=BulkParagraphsRequest(
                    selection=BulkSelection(ids=[paragraph.id]),
                    action="freeze",
                    expected_count=9,
                ),
            )
        assert (await DBMemParagraph.get(id=paragraph.id)).is_frozen is False

    async def test_filter_bulk_stays_inside_the_tenant_scope(self, world: World) -> None:
        preview = await memory_admin.bulk_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            body=BulkParagraphsRequest(
                selection=BulkSelection(filter=ParagraphFilterSelection(status="active")),
                action="freeze",
                dry_run=True,
            ),
        )
        assert preview.matched == 2

        result = await memory_admin.bulk_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            body=BulkParagraphsRequest(
                selection=BulkSelection(filter=ParagraphFilterSelection(status="active")),
                action="freeze",
                expected_count=preview.matched,
            ),
        )
        assert result.affected == 2
        assert (await DBMemParagraph.get(id=world.paragraphs["b1_main"].id)).is_frozen is False
        assert (await DBMemParagraph.get(id=world.paragraphs["a2_main"].id)).is_frozen is False

        foreign_channel = await memory_admin.bulk_paragraphs(
            _current_user=world.tenant,
            instance_key="a1",
            body=BulkParagraphsRequest(
                selection=BulkSelection(
                    filter=ParagraphFilterSelection(status="active", chat_key=CHAT_B1),
                ),
                action="delete",
                dry_run=True,
            ),
        )
        assert foreign_channel.matched == 0


class TestScopeOptions:
    async def test_platform_sees_every_instance_and_channel(self, world: World) -> None:
        options = await memory_admin.get_scope_options(_current_user=world.platform)
        assert {item.instance_key for item in options.instances} == {"a1", "a2", "b1", "p1"}
        assert {item.chat_key for item in options.channels} == {CHAT_A1, CHAT_A2, CHAT_B1, CHAT_P1}

    async def test_tenant_sees_only_its_instances_and_channels(self, world: World) -> None:
        options = await memory_admin.get_scope_options(
            _current_user=world.tenant,
            instance_key="a1",
        )
        assert {item.instance_key for item in options.instances} == {"a1", "a2"}
        assert {item.chat_key for item in options.channels} == {CHAT_A1}

        grouped = await memory_admin.get_scope_options(
            _current_user=world.tenant,
            instance_key="a1",
            domain="group",
        )
        assert {item.chat_key for item in grouped.channels} == {CHAT_A1, CHAT_A2}


class TestEdit:
    async def test_person_from_another_channel_is_rejected(self, world: World) -> None:
        paragraph = world.paragraphs["a1_main"]
        with pytest.raises(ValidationError):
            await memory_admin.update_memory_item(
                _current_user=world.tenant,
                memory_type="paragraph",
                item_id=paragraph.id,
                body=MemoryItemUpdateRequest(person_accounts=["ub1"]),
            )
        accounts = await DBMemParagraphPerson.filter(paragraph_id=paragraph.id).values_list(
            "account",
            flat=True,
        )
        assert sorted(str(account) for account in accounts) == ["u1"]

    async def test_person_from_the_source_channel_is_accepted(self, world: World) -> None:
        paragraph = world.paragraphs["a1_main"]
        detail = await memory_admin.update_memory_item(
            _current_user=world.tenant,
            memory_type="paragraph",
            item_id=paragraph.id,
            body=MemoryItemUpdateRequest(content="改写后的正文", person_accounts=["u1"]),
        )
        assert detail.id == paragraph.id
        assert detail.status == "active"
        accounts = await DBMemParagraphPerson.filter(paragraph_id=paragraph.id).values_list(
            "account",
            flat=True,
        )
        assert sorted(str(account) for account in accounts) == ["u1"]

async def _summary_version(
    chat_key: str,
    *,
    instance_key: str,
    preset_id: int,
    version: int,
    content: str,
    source: str = "auto",
    source_version: Optional[int] = None,
    edited_by: str = "",
) -> DBMemChannelSummary:
    return await DBMemChannelSummary.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=preset_id,
        version=version,
        content=content,
        last_message_db_id=version,
        source=source,
        source_version=source_version,
        edited_by=edited_by,
    )


async def _prompt_round(
    chat_key: str,
    *,
    instance_key: str,
    outcome: str = "ok",
    memory_items: Optional[list[dict[str, Any]]] = None,
    worldbook_state: str = "disabled",
    worldbook_items: Optional[list[dict[str, Any]]] = None,
    worldbook_chars: int = 0,
) -> DBMemPromptRound:
    return await DBMemPromptRound.create(
        round_id=secrets.token_hex(8),
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=7,
        workspace_id=0,
        outcome=outcome,
        render_count=2,
        memory_state="injected",
        memory_items=memory_items or [],
        memory_block_chars=120,
        cooldown_skipped=1,
        summary_state="injected",
        summary_version=1,
        summary_chars=30,
        affection_shown=True,
        affection_chars=40,
        affection_people=2,
        worldbook_state=worldbook_state,
        worldbook_items=worldbook_items or [],
        worldbook_chars=worldbook_chars,
        started_at=_T0,
        finished_at=_T0,
    )


class TestSummaryAdmin:
    async def test_summary_list_is_grouped_and_scoped(self, world: World) -> None:
        await _summary_version(CHAT_A1, instance_key="a1", preset_id=7, version=1, content="甲一版")
        await _summary_version(CHAT_A1, instance_key="a1", preset_id=7, version=2, content="甲二版")
        await _summary_version(CHAT_B1, instance_key="b1", preset_id=9, version=1, content="乙一版")

        platform = await memory_admin.list_summaries(_current_user=world.platform, page_size=100)
        by_channel = {item.chat_key: item for item in platform.items}
        assert platform.total == 2
        assert by_channel[CHAT_A1].version == 2
        assert by_channel[CHAT_A1].version_count == 2
        assert by_channel[CHAT_A1].content == "甲二版"
        assert by_channel[CHAT_B1].version == 1

        tenant = await memory_admin.list_summaries(
            _current_user=world.tenant,
            instance_key="a1",
            page_size=100,
        )
        assert tenant.total == 1
        assert [item.chat_key for item in tenant.items] == [CHAT_A1]

    async def test_summary_versions_edit_rollback_and_pause(self, world: World) -> None:
        await _summary_version(CHAT_A1, instance_key="a1", preset_id=7, version=1, content="第一版")

        versions = await memory_admin.get_summary_versions(
            _current_user=world.tenant,
            chat_key=CHAT_A1,
            preset_id=7,
        )
        assert versions.paused is False
        assert [(item.version, item.content) for item in versions.versions] == [(1, "第一版")]

        edited = await memory_admin.edit_channel_summary(
            _current_user=world.tenant,
            chat_key=CHAT_A1,
            preset_id=7,
            body=SummaryEditRequest(content="手动第二版", base_version=1),
        )
        assert edited.version == 2
        assert edited.source == "manual"
        assert edited.edited_by == world.tenant.username

        with pytest.raises(ConflictError):
            await memory_admin.edit_channel_summary(
                _current_user=world.tenant,
                chat_key=CHAT_A1,
                preset_id=7,
                body=SummaryEditRequest(content="基于旧版本", base_version=1),
            )

        rolled = await memory_admin.rollback_channel_summary(
            _current_user=world.tenant,
            chat_key=CHAT_A1,
            preset_id=7,
            body=SummaryRollbackRequest(to_version=1),
        )
        assert rolled.version == 3
        assert rolled.source == "rollback"
        assert rolled.source_version == 1
        assert rolled.content == "第一版"

        paused = await memory_admin.pause_channel_summary(
            _current_user=world.tenant,
            chat_key=CHAT_A1,
            preset_id=7,
            body=SummaryPauseRequest(paused=True),
        )
        assert paused.paused is True
        assert paused.paused_by == world.tenant.username
        assert paused.paused_at is not None

        paused_versions = await memory_admin.get_summary_versions(
            _current_user=world.tenant,
            chat_key=CHAT_A1,
            preset_id=7,
        )
        assert paused_versions.paused is True
        assert paused_versions.paused_by == world.tenant.username

        resumed = await memory_admin.pause_channel_summary(
            _current_user=world.tenant,
            chat_key=CHAT_A1,
            preset_id=7,
            body=SummaryPauseRequest(paused=False),
        )
        assert resumed.paused is False
        assert resumed.paused_at is None

    async def test_foreign_and_missing_channels_are_indistinguishable(self, world: World) -> None:
        await _summary_version(CHAT_B1, instance_key="b1", preset_id=9, version=1, content="别人的摘要")

        with pytest.raises(NotFoundError) as missing:
            await memory_admin.get_summary_versions(
                _current_user=world.tenant,
                chat_key="onebot_v11-a1-group_nope",
                preset_id=7,
            )
        with pytest.raises(NotFoundError) as foreign:
            await memory_admin.get_summary_versions(
                _current_user=world.tenant,
                chat_key=CHAT_B1,
                preset_id=9,
            )
        assert str(foreign.value) == str(missing.value)

        for call in (
            memory_admin.edit_channel_summary(
                _current_user=world.tenant,
                chat_key=CHAT_B1,
                preset_id=9,
                body=SummaryEditRequest(content="越权修改", base_version=1),
            ),
            memory_admin.rollback_channel_summary(
                _current_user=world.tenant,
                chat_key=CHAT_B1,
                preset_id=9,
                body=SummaryRollbackRequest(),
            ),
            memory_admin.pause_channel_summary(
                _current_user=world.tenant,
                chat_key=CHAT_B1,
                preset_id=9,
                body=SummaryPauseRequest(paused=True),
            ),
        ):
            await _expect_not_found(call, str(missing.value))

        assert await DBMemChannelSummary.filter(chat_key=CHAT_B1).count() == 1
        assert not await DBMemChannelSummaryState.filter(chat_key=CHAT_B1).exists()

    async def test_a_chat_key_shared_with_a_foreign_channel_row_is_not_visible(self, world: World) -> None:
        # 频道表只对（适配器, 实例, 频道 ID）唯一，`chat_key` 没有唯一约束，而摘要只按
        # `chat_key` 读写。万一别的租户有一行频道与自己的频道共用一个 `chat_key`，按不可见处理，
        # 不能因为恰好先取到自己那一行就把两边的摘要一起交出去。
        await _channel(CHAT_A1, instance_key="b1", channel_id="group_shared", name="撞号的频道")
        await _summary_version(CHAT_A1, instance_key="a1", preset_id=7, version=1, content="自己的摘要")

        with pytest.raises(NotFoundError) as missing:
            await memory_admin.get_summary_versions(
                _current_user=world.tenant,
                chat_key="onebot_v11-a1-group_nope",
                preset_id=7,
            )
        await _expect_not_found(
            memory_admin.get_summary_versions(_current_user=world.tenant, chat_key=CHAT_A1, preset_id=7),
            str(missing.value),
        )
        await _expect_not_found(
            memory_admin.pause_channel_summary(
                _current_user=world.tenant,
                chat_key=CHAT_A1,
                preset_id=7,
                body=SummaryPauseRequest(paused=True),
            ),
            str(missing.value),
        )
        assert not await DBMemChannelSummaryState.filter(chat_key=CHAT_A1).exists()

        platform = await memory_admin.get_summary_versions(_current_user=world.platform, chat_key=CHAT_A1, preset_id=7)
        assert [item.content for item in platform.versions] == ["自己的摘要"]


class TestPromptRoundAdmin:
    async def test_list_and_detail_for_both_identities(self, world: World) -> None:
        own = await _prompt_round(
            CHAT_A1,
            instance_key="a1",
            memory_items=[{"type": "paragraph", "id": world.paragraphs["a1_main"].id, "chars": 10, "renders": 1}],
        )
        await _prompt_round(CHAT_A1, instance_key="a1", outcome="blocked")
        await _prompt_round(CHAT_B1, instance_key="b1")

        platform = await memory_admin.list_prompt_rounds(
            _current_user=world.platform,
            chat_key=CHAT_A1,
            page_size=100,
        )
        assert platform.total == 2
        assert {item.outcome for item in platform.items} == {"ok", "blocked"}
        assert all(item.memory_block_chars == 120 for item in platform.items)
        first = next(item for item in platform.items if item.id == own.id)
        assert first.memory_items_count == 1
        assert first.summary_state == "injected"
        assert first.summary_version == 1
        assert first.affection_shown is True
        assert first.affection_people == 2
        assert first.worldbook_count == 0

        tenant = await memory_admin.list_prompt_rounds(
            _current_user=world.tenant,
            instance_key="a1",
            chat_key=CHAT_A1,
            page_size=100,
        )
        assert tenant.total == 2

        detail = await memory_admin.get_prompt_round(_current_user=world.tenant, round_pk=own.id)
        assert detail.id == own.id
        assert detail.chat_key == CHAT_A1
        assert detail.memory_items[0].type == "paragraph"
        assert detail.memory_items[0].status == "active"
        assert "summary" in detail.model_dump()

    async def test_foreign_and_missing_rows_are_indistinguishable(self, world: World) -> None:
        foreign = await _prompt_round(CHAT_B1, instance_key="b1")

        with pytest.raises(NotFoundError) as missing:
            await memory_admin.get_prompt_round(_current_user=world.tenant, round_pk=999_999)
        with pytest.raises(NotFoundError) as denied:
            await memory_admin.get_prompt_round(_current_user=world.tenant, round_pk=foreign.id)
        assert str(denied.value) == str(missing.value)
        assert await DBMemPromptRound.filter(id=foreign.id).exists()

        with pytest.raises(NotFoundError):
            await memory_admin.list_prompt_rounds(
                _current_user=world.tenant,
                instance_key="a1",
                chat_key=CHAT_B1,
            )

    async def test_chat_key_is_required(self, world: World) -> None:
        with pytest.raises(ValidationError):
            await memory_admin.list_prompt_rounds(_current_user=world.platform)


class TestPromptRoundWorldbook:
    """明细里的世界书一栏：状态、字数、条目补状态与越权裁剪

    这一组钉住的是「明细页看到的条目现在是什么状态」，以及「租户看不到别人书里的条目」。
    它不跑匹配引擎：世界书条目在明细里只是被引用的行，判据全在查询侧。
    """

    async def _book(self, name: str, *, instance_key: str, adapter_key: str = ADAPTER, **overrides: Any) -> Any:
        from nekro_agent.models.db_wb_book import DBWbBook

        overrides.setdefault("scope_kind", "domain")
        return await DBWbBook.create(
            name=name,
            adapter_key=adapter_key,
            instance_key=instance_key,
            **overrides,
        )

    async def _preset(self, name: str, owner_instance_key: str) -> Any:
        from nekro_agent.models.db_preset import DBPreset

        return await DBPreset.create(
            name=name,
            title=name,
            avatar="",
            content="",
            description="",
            tags="",
            author="test",
            owner_instance_key=owner_instance_key,
        )

    async def _entry(self, book: Any, *, uid: int, content: str = "正文", **overrides: Any) -> Any:
        from nekro_agent.models.db_wb_entry import DBWbEntry

        return await DBWbEntry.create(book=book, uid=uid, content=content, **overrides)

    async def test_list_row_carries_state_and_only_counts_injected_entries(self, world: World) -> None:
        injected = await self._entry(await self._book("域书", instance_key="a1"), uid=0)
        await self._entry(await self._book("域书", instance_key="a1"), uid=1)
        row = await _prompt_round(
            CHAT_A1,
            instance_key="a1",
            worldbook_state="injected",
            worldbook_chars=42,
            worldbook_items=[
                {"type": "worldbook", "id": injected.id, "book_id": 1, "chars": 42, "position": "before_history"},
                {"type": "worldbook_dropped", "id": 999, "book_id": 1, "chars": 9, "reason": "global_chars"},
            ],
        )

        listed = await memory_admin.list_prompt_rounds(
            _current_user=world.tenant,
            instance_key="a1",
            chat_key=CHAT_A1,
            page_size=100,
        )
        first = next(item for item in listed.items if item.id == row.id)
        # 被预算跳过的那条不算「注入了几条」，但它让字数统计保持为注入条目的字数。
        assert first.worldbook_count == 1
        assert first.worldbook_state == "injected"
        assert first.worldbook_chars == 42

    async def test_detail_reports_current_status_of_each_entry(self, world: World) -> None:
        book = await self._book("域书", instance_key="a1")
        active = await self._entry(book, uid=0)
        disabled = await self._entry(book, uid=1, enabled=False)
        quarantined = await self._entry(book, uid=2, status="quarantined")
        row = await _prompt_round(
            CHAT_A1,
            instance_key="a1",
            worldbook_state="injected",
            worldbook_items=[
                {"type": "worldbook", "id": active.id, "book_id": book.id, "chars": 2, "position": "before_history"},
                {"type": "worldbook", "id": disabled.id, "book_id": book.id, "chars": 2, "position": "before_history"},
                {
                    "type": "worldbook",
                    "id": quarantined.id,
                    "book_id": book.id,
                    "chars": 2,
                    "position": "before_history",
                },
                {"type": "worldbook", "id": 900_001, "book_id": book.id, "chars": 2, "position": "before_history"},
            ],
        )

        detail = await memory_admin.get_prompt_round(_current_user=world.tenant, round_pk=row.id)
        by_id = {item["id"]: item for item in detail.worldbook_items}
        assert by_id[active.id]["status"] == "active"
        assert by_id[active.id]["book_name"] == "域书"
        assert by_id[disabled.id]["status"] == "disabled"
        assert by_id[quarantined.id]["status"] == "quarantined"
        # 条目行已经没了，`describe_round_items` 给不出归属实例对；租户一侧按不可见处理，
        # 否则「这条曾经属于某本书」会从状态本身泄露出去。平台方看到的是真状态。
        assert by_id[900_001]["status"] == "hidden"

        platform_detail = await memory_admin.get_prompt_round(_current_user=world.platform, round_pk=row.id)
        platform_by_id = {item["id"]: item for item in platform_detail.worldbook_items}
        assert platform_by_id[900_001]["status"] == "hard_deleted"
        assert platform_by_id[900_001]["book_name"] == ""

    async def test_detail_shows_platform_global_preset_book_entries_to_tenants(self, world: World) -> None:
        # 平台方给全局人设建的书：人设对租户的实例可见，明细里照常显示（只读开放），不写 hidden。
        preset = await self._preset("全局人设", "")
        global_book = await self._book(
            "全局人设书",
            instance_key="",
            adapter_key="",
            scope_kind="preset",
            preset_id=preset.id,
        )
        entry = await self._entry(global_book, uid=0, comment="全局标题", content="可以显示")
        row = await _prompt_round(
            CHAT_A1,
            instance_key="a1",
            worldbook_state="injected",
            worldbook_items=[
                {
                    "type": "worldbook",
                    "id": entry.id,
                    "book_id": global_book.id,
                    "chars": 4,
                    "position": "before_history",
                },
            ],
        )

        tenant_detail = await memory_admin.get_prompt_round(_current_user=world.tenant, round_pk=row.id)
        item = tenant_detail.worldbook_items[0]
        assert item["status"] == "active"
        assert item["book_id"] == global_book.id
        assert item["book_name"] == "全局人设书"
        assert item["comment"] == "全局标题"
        assert not any(key.startswith("_") for key in item)

    async def test_detail_hides_entries_from_books_the_tenant_cannot_see(self, world: World) -> None:
        # 归属实例对为空、但绑的人设是别的租户实例私有的书：租户看不到它。
        foreign_preset = await self._preset("别人人设", "b1")
        global_book = await self._book(
            "全局人设书",
            instance_key="",
            adapter_key="",
            scope_kind="preset",
            preset_id=foreign_preset.id,
        )
        hidden_entry = await self._entry(global_book, uid=0, content="不该显示")
        row = await _prompt_round(
            CHAT_A1,
            instance_key="a1",
            worldbook_state="injected",
            worldbook_items=[
                {
                    "type": "worldbook",
                    "id": hidden_entry.id,
                    "book_id": global_book.id,
                    "chars": 4,
                    "position": "before_history",
                },
            ],
        )

        tenant_detail = await memory_admin.get_prompt_round(_current_user=world.tenant, round_pk=row.id)
        item = tenant_detail.worldbook_items[0]
        assert item["status"] == "hidden"
        assert "book_id" not in item
        assert item["book_name"] == ""

        platform_detail = await memory_admin.get_prompt_round(_current_user=world.platform, round_pk=row.id)
        assert platform_detail.worldbook_items[0]["status"] == "active"
        assert platform_detail.worldbook_items[0]["book_name"] == "全局人设书"

    async def test_detail_hides_entries_from_another_tenants_book(self, world: World) -> None:
        # 域里混进一条别的租户实例的书：归属实例对不为空，但租户一看不到它。明细行里多记的
        # 字段（这里是一个标题）也不能带出去，只留白名单里的键。
        other_book = await self._book("租户二的书", instance_key="b1")
        other_entry = await self._entry(other_book, uid=0, comment="租户二的标题")
        row = await _prompt_round(
            CHAT_A1,
            instance_key="a1",
            worldbook_state="injected",
            worldbook_items=[
                {
                    "type": "worldbook",
                    "id": other_entry.id,
                    "book_id": other_book.id,
                    "title": "租户二的标题",
                    "chars": 2,
                    "position": "before_history",
                    "trusted": False,
                    "renders": 1,
                },
            ],
        )

        tenant_detail = await memory_admin.get_prompt_round(_current_user=world.tenant, round_pk=row.id)
        item = tenant_detail.worldbook_items[0]
        assert item == {
            "type": "worldbook",
            "id": other_entry.id,
            "chars": 2,
            "position": "before_history",
            "trusted": False,
            "renders": 1,
            "status": "hidden",
            "book_name": "",
            "comment": "",
        }

        platform_detail = await memory_admin.get_prompt_round(_current_user=world.platform, round_pk=row.id)
        assert platform_detail.worldbook_items[0]["book_name"] == "租户二的书"
        assert platform_detail.worldbook_items[0]["comment"] == "租户二的标题"

    async def test_count_ignores_record_types_it_does_not_know(self, world: World) -> None:
        # 列表那一栏只数注入的条目：明细行里以后多出一种记录，不该被算成「注入了一条」。
        row = await _prompt_round(
            CHAT_A1,
            instance_key="a1",
            worldbook_state="injected",
            worldbook_items=[
                {"type": "worldbook", "id": 1, "book_id": 1, "chars": 2, "position": "before_history"},
                {"type": "worldbook_dropped", "id": 2, "book_id": 1, "chars": 9, "reason": "global_chars"},
                {"type": "something_else", "id": 3, "book_id": 1, "chars": 3},
                "not a dict",
            ],
        )

        listed = await memory_admin.list_prompt_rounds(
            _current_user=world.platform,
            instance_key="a1",
            chat_key=CHAT_A1,
            page_size=100,
        )
        assert next(item for item in listed.items if item.id == row.id).worldbook_count == 1

    async def test_disabled_book_marks_its_entries_disabled(self, world: World) -> None:
        # 书停用等于它下面所有条目都不生效：明细要显示「停用」，管理员据此决定去书上开，
        # 而不是逐条去改条目。
        book = await self._book("停用的书", instance_key="a1", enabled=False)
        entry = await self._entry(book, uid=0)
        row = await _prompt_round(
            CHAT_A1,
            instance_key="a1",
            worldbook_state="injected",
            worldbook_items=[
                {"type": "worldbook", "id": entry.id, "book_id": book.id, "chars": 2, "position": "before_history"},
            ],
        )

        detail = await memory_admin.get_prompt_round(_current_user=world.tenant, round_pk=row.id)

        assert detail.worldbook_items[0]["status"] == "disabled"
        assert detail.worldbook_items[0]["book_name"] == "停用的书"

    async def test_internal_scope_keys_never_reach_the_response(self, world: World) -> None:
        # 内部键只是路由判可见用的中间产物：留在响应里等于把书的归属实例对交给前端，
        # 而那是逐条裁剪时不该带出去的信息。
        book = await self._book("域书", instance_key="a1")
        entry = await self._entry(book, uid=0)
        row = await _prompt_round(
            CHAT_A1,
            instance_key="a1",
            worldbook_state="injected",
            worldbook_items=[
                {"type": "worldbook", "id": entry.id, "book_id": book.id, "chars": 2, "position": "before_history"},
            ],
        )

        for user in (world.platform, world.tenant):
            detail = await memory_admin.get_prompt_round(_current_user=user, round_pk=row.id)
            keys = set(detail.worldbook_items[0])
            assert "_book_adapter_key" not in keys
            assert "_book_instance_key" not in keys
