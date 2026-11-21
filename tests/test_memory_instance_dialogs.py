"""入组与划转对话框的后端行为。

三组判据：

1. 实例记忆条数只数**域行**，摘要按「频道 + 人设」计份数，好感只数自成一域那一份；
2. 划转预览与保存共用同一份「改变归属」判据，预览不写库，原样重放为空；
3. 「一并清空」只真删这次改变归属的实例的域行、摘要、明细与自成一域好感，
   工作区行、组域好感与其他实例一条不动。

跑在真表（SQLite 内存库）与本地 Qdrant 上：这些用例的价值在归属与顺序，不在网络调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
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
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console import identity as identity_router
from nekro_agent.routers.console import tenants as tenants_router
from nekro_agent.schemas.errors import PermissionDeniedError
from nekro_agent.schemas.tenant import TenantInstanceBindRequest, TenantRole, TenantStatus
from nekro_agent.services.memory import admin_service
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.qdrant_manager import MEMORY_PARAGRAPH_COLLECTION
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.tenant_perm import require_platform_owner

ADAPTER = "test_adapter"
ALT_ADAPTER = "alt_adapter"
OTHER_ADAPTER = "other_adapter"
VECTOR = [0.1, 0.2, 0.3, 0.4]
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

TENANT_A_ID = 1
TENANT_B_ID = 2
WORKSPACE_ID = 42

CHAT_A1 = "test_adapter-a1-group_1"
CHAT_A2 = "test_adapter-a2-group_2"
CHAT_GROUP = "test_adapter-a1-group_3"
CHAT_J1 = "test_adapter-j1-group_4"
CHAT_J2 = "test_adapter-j1-group_5"
CHAT_P1 = "test_adapter-p1-group_6"


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


def _tenant_admin(tenant_id: int) -> DBUser:
    return DBUser(
        id=2,
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
    tenant_admin: DBUser
    tenant: DBTenant
    other_tenant: DBTenant
    group: DBInstanceGroup
    join_group: DBInstanceGroup
    instances: dict[str, DBAdapterInstance]
    paragraphs: dict[str, DBMemParagraph]
    episodes: dict[str, DBMemEpisode]
    relations: dict[str, DBMemRelation]
    entities: dict[str, DBMemEntity]


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    monkeypatch.setattr(config, "OUTPUT_FILTER_ENABLE", False)
    monkeypatch.setattr(config, "OUTPUT_FILTER_BEFORE_MEMORY", False)

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
    adapter_key: str = ADAPTER,
    tenant_id: Optional[int],
    display_name: str,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=display_name,
        tenant_id=tenant_id,
    )


async def _paragraph(
    *,
    instance_key: str,
    workspace_id: int,
    content: str,
    adapter_key: str = ADAPTER,
    preset_id: int = 7,
    origin_chat_key: Optional[str] = None,
    is_inactive: bool = False,
    quarantined_at: Optional[datetime] = None,
    last_manual_action: str = "",
) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=workspace_id,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        origin_chat_key=origin_chat_key,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content=content,
        summary=content[:16],
        origin_kind=OriginKind.CONSOLIDATION,
        event_time=_T0,
        is_inactive=is_inactive,
        quarantined_at=quarantined_at,
        quarantine_reason="manual" if quarantined_at is not None else "",
        last_manual_action=last_manual_action,
    )


async def _person(paragraph: DBMemParagraph, account: str) -> DBMemParagraphPerson:
    return await DBMemParagraphPerson.create(paragraph_id=paragraph.id, account=account)


async def _episode(
    *,
    instance_key: str,
    workspace_id: int,
    title: str,
    paragraph_ids: list[int],
) -> DBMemEpisode:
    return await DBMemEpisode.create(
        workspace_id=workspace_id,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=7,
        origin_chat_key=CHAT_A1,
        origin_chat_type="group",
        title=title,
        narrative_summary=f"{title}的叙事",
        paragraph_ids=paragraph_ids,
        participant_entity_ids=[],
        phase_mapping={},
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


async def _relation(
    *,
    instance_key: str,
    workspace_id: int,
    paragraph_id: int,
    subject_entity_id: int,
    object_entity_id: int,
    predicate: str,
) -> DBMemRelation:
    return await DBMemRelation.create(
        workspace_id=workspace_id,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=7,
        origin_chat_key=CHAT_A1,
        subject_entity_id=subject_entity_id,
        predicate=predicate,
        object_entity_id=object_entity_id,
        paragraph_id=paragraph_id,
        memory_source="na",
        cognitive_type="semantic",
    )


async def _log(*, target_id: int, target_type: TargetType) -> DBMemReinforcementLog:
    return await DBMemReinforcementLog.create(
        workspace_id=0,
        target_type=target_type,
        target_id=target_id,
        trigger_source=TriggerSource.MANUAL,
    )


async def _summary(
    chat_key: str,
    *,
    instance_key: str,
    preset_id: int = 7,
    version: int = 1,
) -> DBMemChannelSummary:
    return await DBMemChannelSummary.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=preset_id,
        version=version,
        content=f"摘要 {chat_key} 第 {version} 版",
    )


async def _summary_state(chat_key: str, *, instance_key: str, preset_id: int = 7) -> DBMemChannelSummaryState:
    return await DBMemChannelSummaryState.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=preset_id,
        paused=True,
        paused_by="ops",
        paused_at=_T0,
    )


async def _prompt_round(round_id: str, *, instance_key: str, chat_key: str = CHAT_A1) -> DBMemPromptRound:
    return await DBMemPromptRound.create(
        round_id=round_id,
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        preset_id=7,
        started_at=_T0,
        finished_at=_T0,
    )


async def _affection(
    *,
    instance_key: str,
    platform_userid: str,
    domain_group_id: int = 0,
    adapter_key: str = ADAPTER,
    preset_id: int = 7,
    score: int = 10,
) -> DBAffection:
    return await DBAffection.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        domain_group_id=domain_group_id,
        platform_userid=platform_userid,
        preset_id=preset_id,
        score=score,
        stage="stranger",
    )


async def _affection_event(
    *,
    instance_key: str,
    platform_userid: str,
    domain_group_id: int = 0,
    adapter_key: str = ADAPTER,
    preset_id: int = 7,
    delta: int = 1,
    deleted: bool = False,
) -> DBAffectionEvent:
    return await DBAffectionEvent.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        domain_group_id=domain_group_id,
        platform_userid=platform_userid,
        preset_id=preset_id,
        delta=delta,
        score_after=delta,
        source="manual",
        deleted_at=_T0 if deleted else None,
        deleted_by="ops" if deleted else "",
    )


async def _add_point(client: AsyncQdrantClient, row: DBMemParagraph) -> None:
    await client.upsert(
        collection_name=MEMORY_PARAGRAPH_COLLECTION,
        points=[qdrant_models.PointStruct(id=row.id, vector=list(VECTOR), payload={})],
        wait=True,
    )


async def _point_count(client: AsyncQdrantClient, paragraph_id: int) -> int:
    records = await client.retrieve(
        collection_name=MEMORY_PARAGRAPH_COLLECTION,
        ids=[paragraph_id],
        with_payload=False,
    )
    return len(records)


async def _seed_world(client: AsyncQdrantClient) -> World:
    platform = _platform_user()
    tenant = await DBTenant.create(
        id=TENANT_A_ID,
        name="alpha",
        display_name="租户甲",
        status=TenantStatus.ACTIVE.value,
    )
    other_tenant = await DBTenant.create(
        id=TENANT_B_ID,
        name="beta",
        display_name="租户乙",
        status=TenantStatus.ACTIVE.value,
    )
    tenant_admin = _tenant_admin(tenant.id)
    group = await DBInstanceGroup.create(name="甲的实例组", tenant_id=tenant.id)
    join_group = await DBInstanceGroup.create(name="甲的入组目标", tenant_id=tenant.id)

    instances = {
        "a1": await _instance("a1", tenant_id=tenant.id, display_name="甲一"),
        "a2": await _instance("a2", tenant_id=tenant.id, display_name="甲二"),
        "j1": await _instance("j1", tenant_id=tenant.id, display_name="甲三"),
        "p1": await _instance("p1", tenant_id=None, display_name="平台实例"),
        "b1": await _instance("b1", tenant_id=other_tenant.id, display_name="乙一"),
        "dupe_a": await _instance(
            "dupe",
            adapter_key=ALT_ADAPTER,
            tenant_id=None,
            display_name="同名实例甲",
        ),
        "dupe_b": await _instance(
            "dupe",
            adapter_key=OTHER_ADAPTER,
            tenant_id=None,
            display_name="同名实例乙",
        ),
    }
    await DBInstanceDomainBinding.bind_to_groups(ADAPTER, "a1", memory_group=group)
    await DBInstanceDomainBinding.bind_to_groups(ADAPTER, "a2", memory_group=group)

    paragraphs: dict[str, DBMemParagraph] = {}
    episodes: dict[str, DBMemEpisode] = {}
    relations: dict[str, DBMemRelation] = {}
    entities: dict[str, DBMemEntity] = {}

    # 甲一：一份完整的域行 + 一份隔离中的域行 + 一份已删除的域行 + 工作区行。
    entities["a1_subject"] = await _entity(instance_key="a1", name="甲主体")
    entities["a1_object"] = await _entity(instance_key="a1", name="甲客体")
    paragraphs["a1_domain"] = await _paragraph(
        instance_key="a1",
        workspace_id=0,
        content="甲一留在域里的段落",
        origin_chat_key=CHAT_A1,
    )
    await _person(paragraphs["a1_domain"], "u1")
    episodes["a1_domain"] = await _episode(
        instance_key="a1",
        workspace_id=0,
        title="甲一的域 episode",
        paragraph_ids=[paragraphs["a1_domain"].id],
    )
    paragraphs["a1_domain"].episode_id = episodes["a1_domain"].id
    await paragraphs["a1_domain"].save(update_fields=["episode_id"])
    relations["a1_domain"] = await _relation(
        instance_key="a1",
        workspace_id=0,
        paragraph_id=paragraphs["a1_domain"].id,
        subject_entity_id=entities["a1_subject"].id,
        object_entity_id=entities["a1_object"].id,
        predicate="likes",
    )
    await _log(target_id=paragraphs["a1_domain"].id, target_type=TargetType.PARAGRAPH)
    await _log(target_id=relations["a1_domain"].id, target_type=TargetType.RELATION)

    paragraphs["a1_quarantined"] = await _paragraph(
        instance_key="a1",
        workspace_id=0,
        content="甲一隔离中的段落",
        is_inactive=True,
        quarantined_at=_T0,
    )
    paragraphs["a1_deleted"] = await _paragraph(
        instance_key="a1",
        workspace_id=0,
        content="甲一已软删的段落",
        is_inactive=True,
        last_manual_action="delete",
    )
    paragraphs["a1_workspace"] = await _paragraph(
        instance_key="a1",
        workspace_id=WORKSPACE_ID,
        content="甲一的工作区段落",
    )
    await _person(paragraphs["a1_workspace"], "u1ws")
    episodes["a1_workspace"] = await _episode(
        instance_key="a1",
        workspace_id=WORKSPACE_ID,
        title="甲一的工作区 episode",
        paragraph_ids=[paragraphs["a1_workspace"].id],
    )
    paragraphs["a1_workspace"].episode_id = episodes["a1_workspace"].id
    await paragraphs["a1_workspace"].save(update_fields=["episode_id"])

    await _summary(CHAT_A1, instance_key="a1", preset_id=7, version=1)
    await _summary(CHAT_A1, instance_key="a1", preset_id=7, version=2)
    await _summary(CHAT_A2, instance_key="a1", preset_id=0, version=1)
    await _summary_state(CHAT_A1, instance_key="a1")
    await _prompt_round("pr-a1-1", instance_key="a1")
    await _prompt_round("pr-a1-2", instance_key="a1", chat_key=CHAT_A2)

    await _affection(instance_key="a1", platform_userid="person-1", preset_id=7, score=50)
    await _affection(instance_key="a1", platform_userid="person-2", preset_id=0, score=10)
    await _affection(
        instance_key="",
        adapter_key="",
        domain_group_id=group.id,
        platform_userid="group-person",
        preset_id=7,
        score=70,
    )
    await _affection_event(instance_key="a1", platform_userid="person-1", delta=5)
    await _affection_event(instance_key="a1", platform_userid="person-2", preset_id=0, delta=2)
    await _affection_event(instance_key="a1", platform_userid="person-1", delta=1, deleted=True)
    await _affection_event(
        instance_key="",
        adapter_key="",
        domain_group_id=group.id,
        platform_userid="group-person",
        delta=3,
    )

    # 甲三：只用于入组预览，数量与甲一不同，避免两个用例互相掩盖。
    for index in range(3):
        paragraphs[f"j1_{index}"] = await _paragraph(
            instance_key="j1",
            workspace_id=0,
            content=f"甲三的域段落 {index}",
            origin_chat_key=CHAT_A1,
        )
    paragraphs["j1_quarantined"] = await _paragraph(
        instance_key="j1",
        workspace_id=0,
        content="甲三隔离中的段落",
        is_inactive=True,
        quarantined_at=_T0,
    )
    for index in range(2):
        await _paragraph(
            instance_key="j1",
            workspace_id=WORKSPACE_ID,
            content=f"甲三的工作区段落 {index}",
        )
    await _summary(CHAT_J1, instance_key="j1", preset_id=7, version=1)
    await _summary(CHAT_J2, instance_key="j1", preset_id=0, version=1)
    await _prompt_round("pr-j1-1", instance_key="j1", chat_key=CHAT_J1)
    await _prompt_round("pr-j1-2", instance_key="j1", chat_key=CHAT_J2)
    await _affection(instance_key="j1", platform_userid="person-j1")
    await _affection_event(instance_key="j1", platform_userid="person-j1")

    # 平台实例：这次保存会划入甲，它的域行与自成一域好感也要一并清掉。
    paragraphs["p1_domain"] = await _paragraph(
        instance_key="p1",
        workspace_id=0,
        content="平台实例划入前的域段落",
        origin_chat_key=CHAT_A1,
    )
    paragraphs["p1_workspace"] = await _paragraph(
        instance_key="p1",
        workspace_id=WORKSPACE_ID,
        content="平台实例的工作区段落",
    )
    await _summary(CHAT_P1, instance_key="p1", preset_id=7, version=1)
    await _prompt_round("pr-p1-1", instance_key="p1", chat_key=CHAT_P1)
    await _affection(instance_key="p1", platform_userid="person-p1", score=30)
    await _affection_event(instance_key="p1", platform_userid="person-p1", delta=4)

    # 留在租户里的实例与其他租户的数据：清空时一条都不该动。
    paragraphs["a2_domain"] = await _paragraph(
        instance_key="a2",
        workspace_id=0,
        content="甲二留在域里的段落",
        origin_chat_key=CHAT_A2,
    )
    await _summary(CHAT_A2, instance_key="a2", preset_id=7, version=1)
    await _prompt_round("pr-a2-1", instance_key="a2", chat_key=CHAT_A2)
    await _affection(instance_key="a2", platform_userid="person-a2")
    paragraphs["b1_domain"] = await _paragraph(
        instance_key="b1",
        workspace_id=0,
        content="乙一的段落",
        origin_chat_key=CHAT_GROUP,
    )
    await _affection(instance_key="b1", platform_userid="person-b1")
    paragraphs["dupe_a"] = await _paragraph(
        instance_key="dupe",
        adapter_key=ALT_ADAPTER,
        workspace_id=0,
        content="同名实例甲的段落",
    )

    await _add_point(client, paragraphs["a1_domain"])
    await _add_point(client, paragraphs["a1_workspace"])
    await _add_point(client, paragraphs["p1_domain"])
    await _add_point(client, paragraphs["p1_workspace"])

    return World(
        client=client,
        platform=platform,
        tenant_admin=tenant_admin,
        tenant=tenant,
        other_tenant=other_tenant,
        group=group,
        join_group=join_group,
        instances=instances,
        paragraphs=paragraphs,
        episodes=episodes,
        relations=relations,
        entities=entities,
    )


async def _snapshot() -> dict[str, list[dict[str, Any]]]:
    """预览前后要逐字相同的库快照。"""
    return {
        "instances": await DBAdapterInstance.all().order_by("id").values(),
        "bindings": await DBInstanceDomainBinding.all().order_by("id").values(),
        "paragraphs": await DBMemParagraph.all().order_by("id").values(),
        "episodes": await DBMemEpisode.all().order_by("id").values(),
        "relations": await DBMemRelation.all().order_by("id").values(),
        "entities": await DBMemEntity.all().order_by("id").values(),
        "summaries": await DBMemChannelSummary.all().order_by("id").values(),
        "summary_states": await DBMemChannelSummaryState.all().order_by("id").values(),
        "prompt_rounds": await DBMemPromptRound.all().order_by("id").values(),
        "affection": await DBAffection.all().order_by("id").values(),
        "affection_events": await DBAffectionEvent.all().order_by("id").values(),
    }


class TestCountInstanceMemory:
    async def test_counts_domain_rows_and_pairs_with_the_database(self, world: World) -> None:
        counts = await admin_service.count_instance_memory(ADAPTER, "a1")

        assert counts.paragraphs == 1
        assert counts.quarantined == 1
        assert counts.summaries == 2
        assert counts.prompt_rounds == 2
        assert counts.affection_rows == 2
        assert counts.affection_events == 3

        assert counts.paragraphs == await DBMemParagraph.filter(
            workspace_id=0,
            adapter_key=ADAPTER,
            instance_key="a1",
            is_inactive=False,
        ).count()
        assert counts.quarantined == await DBMemParagraph.filter(
            workspace_id=0,
            adapter_key=ADAPTER,
            instance_key="a1",
            is_inactive=True,
            quarantined_at__isnull=False,
        ).count()
        # 摘要表里这个实例有三行（两个频道、其中一个是两版），按份数只应是二。
        assert await DBMemChannelSummary.filter(adapter_key=ADAPTER, instance_key="a1").count() == 3
        assert counts.summaries == len(
            {
                (row.chat_key, row.preset_id)
                for row in await DBMemChannelSummary.filter(adapter_key=ADAPTER, instance_key="a1")
            },
        )
        assert counts.prompt_rounds == await DBMemPromptRound.filter(
            adapter_key=ADAPTER,
            instance_key="a1",
        ).count()
        # 好感只数自成一域那一份；组域那行写的是空实例对，不属于这个实例。
        assert counts.affection_rows == await DBAffection.filter(
            adapter_key=ADAPTER,
            instance_key="a1",
            domain_group_id=0,
        ).count()
        assert counts.affection_events == await DBAffectionEvent.filter(
            adapter_key=ADAPTER,
            instance_key="a1",
            domain_group_id=0,
        ).count()
        # 工作区行确实在库里，只是不计入「跟着实例走」的那几个数。
        assert await DBMemParagraph.filter(
            workspace_id=WORKSPACE_ID,
            instance_key="a1",
        ).count() == 1


class TestDomainJoinPreviewCarriesMemoryCounts:
    async def test_route_returns_the_same_three_numbers_as_the_database(self, world: World) -> None:
        counts = await admin_service.count_instance_memory(ADAPTER, "j1")
        response = await identity_router.get_domain_join_preview(
            instance_key="j1",
            group_id=world.join_group.id,
            _current_user=world.platform,
        )

        assert response.memory_paragraphs == counts.paragraphs == 3
        assert response.memory_quarantined == counts.quarantined == 1
        assert response.memory_summaries == counts.summaries == 2
        # 工作区行与另一实例的数据都不能混进来。
        assert await DBMemParagraph.filter(instance_key="j1", workspace_id__gt=0).count() == 2


class TestTransferPreview:
    async def test_lists_only_changed_rows_and_writes_nothing(self, world: World) -> None:
        body = TenantInstanceBindRequest(instance_keys=["a2", "j1", "p1", "dupe"])
        before = await _snapshot()

        preview = await tenants_router.preview_tenant_instances(
            tenant_id=world.tenant.id,
            body=body,
            _current_user=world.platform,
        )

        after = await _snapshot()
        assert before == after

        assert [(change.instance_key, change.adapter_key) for change in preview.changes] == [
            ("a1", ADAPTER),
            ("dupe", ALT_ADAPTER),
            ("dupe", OTHER_ADAPTER),
            ("p1", ADAPTER),
        ]
        released = preview.changes[0]
        assert released.display_name == "甲一"
        assert released.from_tenant_id == world.tenant.id
        assert released.from_tenant_name == "租户甲"
        assert released.to_tenant_id is None
        assert released.to_tenant_name == ""
        assert released.in_group is True
        assert released.memory_paragraphs == 1
        assert released.memory_quarantined == 1
        assert released.memory_summaries == 2
        assert released.memory_prompt_rounds == 2
        assert released.memory_affection_rows == 2
        assert released.memory_affection_events == 3

        assigned = preview.changes[-1]
        assert assigned.instance_key == "p1"
        assert assigned.from_tenant_id is None
        assert assigned.from_tenant_name == ""
        assert assigned.to_tenant_id == world.tenant.id
        assert assigned.to_tenant_name == "租户甲"
        assert assigned.in_group is False
        assert assigned.memory_paragraphs == 1
        assert assigned.memory_summaries == 1
        assert assigned.memory_prompt_rounds == 1
        assert assigned.memory_affection_rows == 1
        assert assigned.memory_affection_events == 1

        # 同名实例按行各列一条，各自的数据也分开数。
        dupe_changes = [change for change in preview.changes if change.instance_key == "dupe"]
        assert [change.memory_paragraphs for change in dupe_changes] == [1, 0]

    async def test_replaying_the_same_set_is_empty(self, world: World) -> None:
        body = TenantInstanceBindRequest(instance_keys=["a1", "a2", "j1"])
        preview = await tenants_router.preview_tenant_instances(
            tenant_id=world.tenant.id,
            body=body,
            _current_user=world.platform,
        )
        assert preview.changes == []

    async def test_preview_uses_the_same_validation_as_save(self, world: World) -> None:
        # 已归属别的租户：预览与保存同样拒绝，不因为「只是预览」而放行。
        from nekro_agent.schemas.errors import ValidationError

        with pytest.raises(ValidationError):
            await tenants_router.preview_tenant_instances(
                tenant_id=world.tenant.id,
                body=TenantInstanceBindRequest(instance_keys=["b1"]),
                _current_user=world.platform,
            )
        # 不存在的租户同样是 404。
        from nekro_agent.schemas.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await tenants_router.preview_tenant_instances(
                tenant_id=999,
                body=TenantInstanceBindRequest(instance_keys=[]),
                _current_user=world.platform,
            )

    async def test_tenant_identity_is_refused_by_the_platform_owner_dependency(self, world: World) -> None:
        with pytest.raises(PermissionDeniedError):
            await require_platform_owner()(current_user=world.tenant_admin)


class TestClearOnTransfer:
    async def test_true_purges_changed_rows_and_leaves_everything_else(self, world: World) -> None:
        body = TenantInstanceBindRequest(
            instance_keys=["a2", "j1", "p1"],
            clear_memory_and_affection=True,
        )
        await tenants_router.bind_tenant_instances(
            tenant_id=world.tenant.id,
            body=body,
            _current_user=world.platform,
        )

        # 归属已经改了，清空只落在这次改变归属的两行上。
        assert (await DBAdapterInstance.get(id=world.instances["a1"].id)).tenant_id is None
        assert (await DBAdapterInstance.get(id=world.instances["p1"].id)).tenant_id == world.tenant.id

        # 甲一的域行段落（正常、隔离、已软删）与连带数据全部消失。
        assert not await DBMemParagraph.filter(id=world.paragraphs["a1_domain"].id).exists()
        assert not await DBMemParagraph.filter(id=world.paragraphs["a1_quarantined"].id).exists()
        assert not await DBMemParagraph.filter(id=world.paragraphs["a1_deleted"].id).exists()
        assert not await DBMemParagraphPerson.filter(paragraph_id=world.paragraphs["a1_domain"].id).exists()
        assert not await DBMemRelation.filter(id=world.relations["a1_domain"].id).exists()
        assert not await DBMemEpisode.filter(id=world.episodes["a1_domain"].id).exists()
        assert not await DBMemEntity.filter(id=world.entities["a1_subject"].id).exists()
        assert not await DBMemEntity.filter(id=world.entities["a1_object"].id).exists()
        assert not await DBMemReinforcementLog.filter(target_id=world.paragraphs["a1_domain"].id).exists()
        assert not await DBMemReinforcementLog.filter(target_id=world.relations["a1_domain"].id).exists()
        assert await _point_count(world.client, world.paragraphs["a1_domain"].id) == 0

        # 摘要、暂停行与提示词明细。
        assert not await DBMemChannelSummary.filter(
            adapter_key=ADAPTER,
            instance_key="a1",
        ).exists()
        assert not await DBMemChannelSummaryState.filter(
            adapter_key=ADAPTER,
            instance_key="a1",
        ).exists()
        assert not await DBMemPromptRound.filter(adapter_key=ADAPTER, instance_key="a1").exists()

        # 自成一域的好感与事件（含软删过的事件）整行删；组域那一份不动。
        assert not await DBAffection.filter(
            adapter_key=ADAPTER,
            instance_key="a1",
            domain_group_id=0,
        ).exists()
        assert not await DBAffectionEvent.filter(
            adapter_key=ADAPTER,
            instance_key="a1",
            domain_group_id=0,
        ).exists()
        assert await DBAffection.filter(domain_group_id=world.group.id, platform_userid="group-person").exists()
        assert await DBAffectionEvent.filter(
            domain_group_id=world.group.id,
            platform_userid="group-person",
        ).exists()

        # 工作区行一条不少，它的 episode、涉及的人与向量都还在。
        workspace_row = await DBMemParagraph.get(id=world.paragraphs["a1_workspace"].id)
        assert workspace_row.workspace_id == WORKSPACE_ID
        assert workspace_row.is_inactive is False
        assert workspace_row.episode_id == world.episodes["a1_workspace"].id
        assert await DBMemParagraphPerson.filter(paragraph_id=workspace_row.id).exists()
        assert await DBMemEpisode.filter(id=world.episodes["a1_workspace"].id).exists()
        assert await _point_count(world.client, workspace_row.id) == 1

        # 划入的实例也清理了；它的工作区行同样不动。
        assert not await DBMemParagraph.filter(id=world.paragraphs["p1_domain"].id).exists()
        assert not await DBMemChannelSummary.filter(adapter_key=ADAPTER, instance_key="p1").exists()
        assert not await DBMemPromptRound.filter(adapter_key=ADAPTER, instance_key="p1").exists()
        assert not await DBAffection.filter(adapter_key=ADAPTER, instance_key="p1").exists()
        assert await DBMemParagraph.filter(id=world.paragraphs["p1_workspace"].id).exists()
        assert await _point_count(world.client, world.paragraphs["p1_domain"].id) == 0
        assert await _point_count(world.client, world.paragraphs["p1_workspace"].id) == 1

        # 没改变归属的实例与其他租户的数据一条不少。
        assert await DBMemParagraph.filter(id=world.paragraphs["a2_domain"].id).exists()
        assert await DBMemChannelSummary.filter(adapter_key=ADAPTER, instance_key="a2").exists()
        assert await DBMemPromptRound.filter(adapter_key=ADAPTER, instance_key="a2").exists()
        assert await DBAffection.filter(adapter_key=ADAPTER, instance_key="a2").exists()
        assert await DBMemParagraph.filter(id=world.paragraphs["b1_domain"].id).exists()
        assert await DBAffection.filter(adapter_key=ADAPTER, instance_key="b1").exists()
        assert await DBMemParagraph.filter(id=world.paragraphs["dupe_a"].id).exists()

    async def test_a_failed_clear_changes_no_owner_and_the_retry_finishes_it(
        self,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 清空中途失败：归属一个都不改；原样再保存一次，仍按「改变归属」清完再改归属。
        # 先改归属再清的话，重试时这些实例已经归了新租户，清空会被整段跳过。
        from nekro_agent.services.memory import admin_service

        original = admin_service.purge_instance_domain_memory
        calls: list[str] = []

        async def failing_second(adapter_key: str, instance_key: str) -> dict[str, int]:
            calls.append(instance_key)
            if len(calls) == 2:
                raise RuntimeError("清空中途失败")
            return await original(adapter_key, instance_key)

        monkeypatch.setattr(admin_service, "purge_instance_domain_memory", failing_second)
        body = TenantInstanceBindRequest(instance_keys=["a2", "j1", "p1"], clear_memory_and_affection=True)
        with pytest.raises(RuntimeError):
            await tenants_router.bind_tenant_instances(
                tenant_id=world.tenant.id,
                body=body,
                _current_user=world.platform,
            )

        assert len(calls) == 2
        assert (await DBAdapterInstance.get(id=world.instances["a1"].id)).tenant_id == world.tenant.id
        assert (await DBAdapterInstance.get(id=world.instances["p1"].id)).tenant_id is None

        monkeypatch.setattr(admin_service, "purge_instance_domain_memory", original)
        preview = await tenants_router.preview_tenant_instances(
            tenant_id=world.tenant.id,
            body=body,
            _current_user=world.platform,
        )
        assert {change.instance_key for change in preview.changes} == {"a1", "p1"}

        await tenants_router.bind_tenant_instances(
            tenant_id=world.tenant.id,
            body=body,
            _current_user=world.platform,
        )

        assert (await DBAdapterInstance.get(id=world.instances["a1"].id)).tenant_id is None
        assert (await DBAdapterInstance.get(id=world.instances["p1"].id)).tenant_id == world.tenant.id
        assert not await DBMemParagraph.filter(id=world.paragraphs["a1_domain"].id).exists()
        assert not await DBMemParagraph.filter(id=world.paragraphs["p1_domain"].id).exists()
        assert not await DBMemChannelSummary.filter(adapter_key=ADAPTER, instance_key="p1").exists()
        assert not await DBMemPromptRound.filter(adapter_key=ADAPTER, instance_key="p1").exists()
        assert not await DBAffection.filter(adapter_key=ADAPTER, instance_key="p1").exists()
        assert await DBMemParagraph.filter(id=world.paragraphs["a2_domain"].id).exists()

    async def test_false_deletes_nothing(self, world: World) -> None:
        body = TenantInstanceBindRequest(instance_keys=["a2", "j1", "p1"])
        assert body.clear_memory_and_affection is False

        await tenants_router.bind_tenant_instances(
            tenant_id=world.tenant.id,
            body=body,
            _current_user=world.platform,
        )

        assert (await DBAdapterInstance.get(id=world.instances["a1"].id)).tenant_id is None
        assert (await DBAdapterInstance.get(id=world.instances["p1"].id)).tenant_id == world.tenant.id
        assert await DBMemParagraph.filter(id=world.paragraphs["a1_domain"].id).exists()
        assert await DBMemParagraph.filter(id=world.paragraphs["p1_domain"].id).exists()
        assert await DBMemChannelSummary.filter(adapter_key=ADAPTER, instance_key="a1").exists()
        assert await DBMemPromptRound.filter(adapter_key=ADAPTER, instance_key="a1").exists()
        assert await DBAffection.filter(adapter_key=ADAPTER, instance_key="a1", domain_group_id=0).exists()
        assert await _point_count(world.client, world.paragraphs["a1_domain"].id) == 1
