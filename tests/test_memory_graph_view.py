"""记忆页「图谱」：`GET /memory/graph` 的取数范围、筛选与「人」节点

## 这条测试钉的是什么

- 范围与段落列表同一个起点：选了实例只画这个实例的段落、事件、实体、关系；域记忆（`workspace_id = 0`）
  也画；别的实例的实体就算被本实例的事件引用了也不画。租户看不到别人的实例（404），不带实例 400。
- 筛选：分区（域 / 工作区）、失活默认不画、按人（选了实例时按别名展开）与按时间时关系与事件跟着
  收窄到本次画出来的段落上；`limit` 有下限。
- 「人」节点：本次段落涉及的人一个人一个节点，名字与段落列表同一个口径（含名字从哪来：来源频道 /
  好感记录 / 没查到）；选了实例时一个人的几个账号并成一个节点，同一段里两个账号是同一个人时只连
  一条边；不选实例时按账号各算各的。
- 关系里程碑段落在元数据里带 `is_milestone`；节点与边的 ID 不重复，边的两头都在节点里。

## 环境

内存 SQLite；这个接口不碰向量库。账号、昵称、称呼都是现编的。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import memory_admin
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.memory_admin import MemoryGraphResponse
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.identity_scope import ScopeDomain
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
CHAT_A1 = "onebot_v11-a1-group_1"
CHAT_B1 = "onebot_v11-b1-group_9"
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _platform() -> DBUser:
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


def _tenant() -> DBUser:
    return DBUser(
        id=21,
        username="tenant-admin",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        tenant_id=1,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


@dataclass
class World:
    p: dict[str, DBMemParagraph]
    e: dict[str, DBMemEpisode]
    ent: dict[str, DBMemEntity]
    r: dict[str, DBMemRelation]


def _place(instance_key: str) -> dict[str, object]:
    chat_key = CHAT_A1 if instance_key == "a1" else CHAT_B1
    return {"adapter_key": ADAPTER, "instance_key": instance_key, "preset_id": 7, "origin_chat_key": chat_key}


async def _paragraph(
    *,
    instance_key: str = "a1",
    workspace_id: int = 0,
    persons: tuple[str, ...] = (),
    episode: Optional[DBMemEpisode] = None,
    days: int = 0,
    inactive: bool = False,
    origin_kind: OriginKind = OriginKind.CONSOLIDATION,
) -> DBMemParagraph:
    row = await DBMemParagraph.create(
        workspace_id=workspace_id,
        **_place(instance_key),
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content="一段记忆",
        summary="一段记忆",
        origin_kind=origin_kind,
        event_time=_T0 + timedelta(days=days),
        episode_id=episode.id if episode is not None else None,
        is_inactive=inactive,
        quarantined_at=_T0 if inactive else None,
        quarantine_reason="manual" if inactive else "",
    )
    for account in persons:
        await DBMemParagraphPerson.create(paragraph_id=row.id, account=account)
    return row


async def _episode(instance_key: str, title: str) -> DBMemEpisode:
    return await DBMemEpisode.create(
        workspace_id=0,
        **_place(instance_key),
        origin_chat_type="group",
        title=title,
        narrative_summary=f"{title}的经过",
        time_start=_T0,
        time_end=_T0,
        paragraph_ids=[],
        participant_entity_ids=[],
        phase_mapping={},
    )


async def _entity(instance_key: str, name: str) -> DBMemEntity:
    return await DBMemEntity.create(
        workspace_id=0,
        **_place(instance_key),
        entity_type=EntityType.PERSON,
        name=name,
        canonical_name=name,
    )


async def _relation(
    instance_key: str,
    subject: DBMemEntity,
    obj: DBMemEntity,
    paragraph: Optional[DBMemParagraph],
) -> DBMemRelation:
    return await DBMemRelation.create(
        workspace_id=0,
        **_place(instance_key),
        subject_entity_id=subject.id,
        predicate="喜欢",
        object_entity_id=obj.id,
        paragraph_id=paragraph.id if paragraph is not None else None,
        memory_source="na",
        cognitive_type="semantic",
    )


async def _seed() -> World:
    for key, tenant in (("a1", 1), ("b1", 2)):
        await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=key, display_name=key, tenant_id=tenant)
    for chat_key, instance_key in ((CHAT_A1, "a1"), (CHAT_B1, "b1")):
        await DBChatChannel.create(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=instance_key,
            channel_id=chat_key.rsplit("-", 1)[-1],
            channel_name=chat_key,
            channel_type="group",
            data="{}",
        )
    await DBChatMessage.create(
        sender_id="u1",
        sender_name="明明",
        sender_nickname="明明",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key="",
        message_id="m1",
        chat_key=CHAT_A1,
        chat_type="group",
        platform_userid="u1",
        content_text="消息",
        content_data="{}",
        raw_cq_code="",
        ext_data="",
        send_timestamp=1_700_000,
    )
    key = AffectionKey.of_domain(ScopeDomain.of_instance(ADAPTER, "a1"), person_uid="u1", preset_id=7)
    await DBAffection.create(**key.as_columns(), nickname="阿明", call_name="明哥", score=0, stage="stranger")
    # u4 是 u1 的副号（a1 的域里合并过）
    await DBIdentityAlias.create(
        domain_id=ScopeDomain.of_instance(ADAPTER, "a1").domain_id,
        platform_userid="u4",
        person_uid="u1",
    )

    e1 = await _episode("a1", "周末开黑")
    e2 = await _episode("a1", "搬家")
    eb = await _episode("b1", "别人家的事")
    ent = {
        "xm": await _entity("a1", "小明"),
        "cat": await _entity("a1", "猫"),
        "other": await _entity("b1", "外人"),
    }
    p = {
        # 同一段里两个账号是同一个人
        "p1": await _paragraph(persons=("u1", "u4"), episode=e1),
        "p2": await _paragraph(persons=("u2",), episode=e2),
        # 只挂着副号；比 p3 早建，所以 u1 最新那一段是 p3
        "p6": await _paragraph(persons=("u4",), days=1),
        "p3": await _paragraph(persons=("u1",), days=10, origin_kind=OriginKind.AFFECTION),
        "p4": await _paragraph(workspace_id=5, persons=("u2",)),
        "p5": await _paragraph(persons=("u3",), inactive=True),
        "pb": await _paragraph(instance_key="b1", persons=("u9",), episode=eb),
    }
    e1.paragraph_ids = [p["p1"].id]
    # 别的实例的实体被本实例的事件引用：图上也不能画出来
    e1.participant_entity_ids = [ent["xm"].id, ent["other"].id]
    await e1.save()
    e2.paragraph_ids = [p["p2"].id]
    await e2.save()
    eb.paragraph_ids = [p["pb"].id]
    eb.participant_entity_ids = [ent["other"].id]
    await eb.save()
    r = {
        "r1": await _relation("a1", ent["xm"], ent["cat"], p["p1"]),
        "r2": await _relation("a1", ent["cat"], ent["xm"], p["p2"]),
        "rb": await _relation("b1", ent["other"], ent["other"], p["pb"]),
    }
    return World(p=p, e={"e1": e1, "e2": e2, "eb": eb}, ent=ent, r=r)


@pytest.fixture
async def world() -> AsyncIterator[World]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield await _seed()
    finally:
        await Tortoise.close_connections()


def _ids(graph: MemoryGraphResponse, memory_type: str) -> set[int]:
    return {node.ref_id for node in graph.nodes if node.memory_type == memory_type}


def _persons(graph: MemoryGraphResponse) -> dict[str, dict[str, object]]:
    return {
        node.id.removeprefix("person-"): {"label": node.label, "subtitle": node.subtitle, **node.metadata}
        for node in graph.nodes
        if node.memory_type == "person"
    }


def _edges(graph: MemoryGraphResponse, edge_type: str) -> list[tuple[str, str]]:
    return sorted((edge.source, edge.target) for edge in graph.edges if edge.edge_type == edge_type)


def _assert_well_formed(graph: MemoryGraphResponse) -> None:
    node_ids = [node.id for node in graph.nodes]
    edge_ids = [edge.id for edge in graph.edges]
    assert not [key for key, count in Counter(node_ids).items() if count > 1]
    assert not [key for key, count in Counter(edge_ids).items() if count > 1]
    known = set(node_ids)
    assert all(edge.source in known and edge.target in known for edge in graph.edges)
    assert (graph.node_count, graph.edge_count) == (len(graph.nodes), len(graph.edges))


async def _graph(user: DBUser, **params: object) -> MemoryGraphResponse:
    return await memory_admin.get_memory_graph(**params, _current_user=user)  # type: ignore[arg-type]


class TestScope:
    async def test_an_instance_draws_only_its_own_memory(self, world: World) -> None:
        graph = await _graph(_platform(), instance_key="a1", adapter_key=ADAPTER)

        _assert_well_formed(graph)
        p = world.p
        # 域记忆与工作区记忆都画；失活的默认不画；别的实例的不画
        assert _ids(graph, "paragraph") == {p["p1"].id, p["p2"].id, p["p3"].id, p["p4"].id, p["p6"].id}
        assert _ids(graph, "episode") == {world.e["e1"].id, world.e["e2"].id}
        assert _ids(graph, "relation") == {world.r["r1"].id, world.r["r2"].id}
        # 事件引用了别的实例的实体：不画
        assert _ids(graph, "entity") == {world.ent["xm"].id, world.ent["cat"].id}
        assert ("episode-%d" % world.e["e1"].id, "paragraph-%d" % p["p1"].id) in _edges(graph, "episode_paragraph")
        assert _edges(graph, "relation_paragraph") == sorted(
            [
                ("relation-%d" % world.r["r1"].id, "paragraph-%d" % p["p1"].id),
                ("relation-%d" % world.r["r2"].id, "paragraph-%d" % p["p2"].id),
            ],
        )

    async def test_tenants_see_only_their_own_instances(self, world: World) -> None:
        mine = await _graph(_tenant(), instance_key="a1", adapter_key=ADAPTER)
        assert world.p["pb"].id not in _ids(mine, "paragraph")
        assert "u9" not in _persons(mine)

        with pytest.raises(NotFoundError):
            await _graph(_tenant(), instance_key="b1", adapter_key=ADAPTER)
        with pytest.raises(ValidationError):
            await _graph(_tenant())

    async def test_the_platform_wide_view_counts_accounts(self, world: World) -> None:
        graph = await _graph(_platform())

        _assert_well_formed(graph)
        assert world.p["pb"].id in _ids(graph, "paragraph")
        people = _persons(graph)
        # 不选实例就没有域，副号各算各的
        assert people["u1"]["accounts"] == ["u1"]
        assert people["u4"]["accounts"] == ["u4"]
        # 副号在本群没说过话：名字是好感记录里的昵称（好感按人存），并标明来源
        assert (people["u4"]["label"], people["u4"]["name_source"]) == ("阿明", "affection")
        assert people["u9"]["paragraph_count"] == 1


class TestPersons:
    async def test_one_node_per_person_with_the_list_names(self, world: World) -> None:
        graph = await _graph(_platform(), instance_key="a1", adapter_key=ADAPTER)

        people = _persons(graph)
        assert set(people) == {"u1", "u2"}
        u1 = people["u1"]
        assert u1["accounts"] == ["u1", "u4"]
        # p1 里 u1 与 u4 是同一个人：只算一段、只连一条边
        assert u1["paragraph_count"] == 3
        p = world.p
        assert [edge for edge in _edges(graph, "paragraph_person") if edge[1] == "person-u1"] == sorted(
            [("paragraph-%d" % p[key].id, "person-u1") for key in ("p1", "p3", "p6")],
        )
        # 名字取最新那一段（p3）上的：本群最近的昵称；最早那一段（p1）最后一个是副号 u4，名字会是好感昵称
        assert (u1["label"], u1["subtitle"], u1["display_name"], u1["call_name"]) == ("明明", "明哥", "明明", "明哥")
        assert u1["name_source"] == "chat"
        # 查不到名字的人：节点名退到账号
        assert (people["u2"]["label"], people["u2"]["paragraph_count"], people["u2"]["name_source"]) == ("u2", 2, "")

    async def test_milestones_are_flagged(self, world: World) -> None:
        graph = await _graph(_platform(), instance_key="a1", adapter_key=ADAPTER)

        flags = {node.ref_id: node.metadata["is_milestone"] for node in graph.nodes if node.memory_type == "paragraph"}
        assert flags[world.p["p3"].id] is True
        assert flags[world.p["p1"].id] is False
        p1 = next(node for node in graph.nodes if node.id == "paragraph-%d" % world.p["p1"].id)
        assert (p1.metadata["memory_status"], p1.metadata["origin_chat_key"]) == ("active", CHAT_A1)


class TestFilters:
    async def test_inactive_rows_on_request(self, world: World) -> None:
        graph = await _graph(_platform(), instance_key="a1", adapter_key=ADAPTER, include_inactive=True)

        _assert_well_formed(graph)
        node = next(node for node in graph.nodes if node.id == "paragraph-%d" % world.p["p5"].id)
        assert node.status == "inactive"
        assert "u3" in _persons(graph)

    async def test_partitions(self, world: World) -> None:
        domain = await _graph(_platform(), instance_key="a1", adapter_key=ADAPTER, partition="domain")
        assert world.p["p4"].id not in _ids(domain, "paragraph")
        assert world.p["p1"].id in _ids(domain, "paragraph")

        workspace = await _graph(_platform(), instance_key="a1", adapter_key=ADAPTER, partition="workspace")
        assert _ids(workspace, "paragraph") == {world.p["p4"].id}

    async def test_the_person_filter_narrows_relations_and_events(self, world: World) -> None:
        graph = await _graph(_platform(), instance_key="a1", adapter_key=ADAPTER, person="u1")

        _assert_well_formed(graph)
        p = world.p
        # 选了实例，按别名展开：只挂着副号 u4 的 p6 也算 u1 的
        assert _ids(graph, "paragraph") == {p["p1"].id, p["p3"].id, p["p6"].id}
        # u2 那段上的关系、只装着 u2 那段的事件都不画
        assert _ids(graph, "relation") == {world.r["r1"].id}
        assert _ids(graph, "episode") == {world.e["e1"].id}
        assert set(_persons(graph)) == {"u1"}

    async def test_the_time_filter_narrows_relations_and_events(self, world: World) -> None:
        graph = await _graph(
            _platform(),
            instance_key="a1",
            adapter_key=ADAPTER,
            time_from=(_T0 + timedelta(days=5)).isoformat(),
        )

        _assert_well_formed(graph)
        assert _ids(graph, "paragraph") == {world.p["p3"].id}
        assert _ids(graph, "relation") == set()
        assert _ids(graph, "episode") == set()
        assert _ids(graph, "entity") == set()

        with pytest.raises(ValidationError):
            await _graph(_platform(), time_from="昨天")

    async def test_the_limit_has_a_floor(self, world: World) -> None:
        graph = await _graph(_platform(), instance_key="a1", adapter_key=ADAPTER, limit=1)

        assert len(_ids(graph, "paragraph")) == 5
        assert world.p["p1"].id in _ids(graph, "paragraph")
