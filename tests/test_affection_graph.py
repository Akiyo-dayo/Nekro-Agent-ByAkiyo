"""好感关系图：`GET /affection/graph` 的取数范围、节点与边

## 钉什么

- 范围与好感列表同一套：租户只看自己实例那个域里的好感，别人的实例 404，不带实例 400；平台方
  不带实例看全部。搜索与阶段筛选与列表同义。
- 图的形状：人设在中间，人连到人设（一份好感一条边，分数与阶段在边上），里程碑挂在人身上，
  人连到最近出现的频道。**人与人之间没有边**，每条边都恰好有一头是人。
- 合并后休眠的副号行不画、只计数；副号名下写过的里程碑挂到主账号那个人身上（与详情页同一判据）。
- 里程碑的正文：段落已经跟着实例挪到别的租户的，只留一个节点、不给正文。
- 频道名：只给这个身份看得见的频道；看不见的只显示会话标识。
- 「人」节点带着各人设下的那几份好感（分数、阶段、称呼、最近出现时间），名字与称呼取最近更新的那份。
- 每份好感最多画几条里程碑；`limit` 有下限；`/graph` 排在 `/{record_id}` 前面。

## 数据

内存 SQLite 上的真表；租户、实例、账号、昵称、文案都是现编的。
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from tortoise import Tortoise
from tortoise.timezone import now as db_now

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_identity import DBIdentityAlias
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import affection as affection_router
from nekro_agent.routers.affection import AffectionGraphResponse, get_affection_graph, get_affection_summary
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.affection.graph_view import GraphRow, build_affection_graph
from nekro_agent.services.affection.key import AffectionKey
from nekro_agent.services.affection.stage import AffectionEventSource, AffectionStage, stage_of
from nekro_agent.services.affection.store import invalidate_cache
from nekro_agent.services.identity_scope import PRESET_NONE, ScopeDomain
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE_OWN = "inst-graph-alpha"
INSTANCE_FOREIGN = "inst-graph-beta"
TENANT_OWN = 31
TENANT_FOREIGN = 47
CHAT_OWN = f"{ADAPTER}-{INSTANCE_OWN}-group_alpha"
CHAT_FOREIGN = f"{ADAPTER}-{INSTANCE_FOREIGN}-group_beta"
PRESET_CAT = 7
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
DOMAIN_OWN = ScopeDomain.of_instance(ADAPTER, INSTANCE_OWN)
DOMAIN_FOREIGN = ScopeDomain.of_instance(ADAPTER, INSTANCE_FOREIGN)


def make_user(tenant_id: Optional[int]) -> DBUser:
    role = TenantRole.TENANT_ADMIN if tenant_id is not None else TenantRole.PLATFORM_OWNER
    return DBUser(
        username=f"operator-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin if tenant_id is not None else Role.Super,
        login_time=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        tenant_role=role.value,
    )


def key_of(domain: ScopeDomain, person: str, preset_id: int) -> AffectionKey:
    return AffectionKey.of_domain(domain, person_uid=person, preset_id=preset_id)


async def make_record(
    domain: ScopeDomain,
    person: str,
    preset_id: int,
    *,
    score: int,
    nickname: str,
    call_name: str = "",
    chat_key: str = "",
    days: int = 0,
) -> DBAffection:
    return await DBAffection.create(
        **key_of(domain, person, preset_id).as_columns(),
        nickname=nickname,
        score=score,
        stage=stage_of(score).value,
        state_text=f"{nickname}的印象",
        call_name=call_name,
        interaction_count=3,
        last_chat_key=chat_key,
        last_interact_at=T0 + timedelta(days=days),
    )


async def make_milestone(
    domain: ScopeDomain,
    account: str,
    preset_id: int,
    *,
    paragraph_instance: str,
    content: str,
) -> DBAffectionEvent:
    paragraph = await DBMemParagraph.create(
        workspace_id=0,
        adapter_key=ADAPTER,
        instance_key=paragraph_instance,
        preset_id=preset_id,
        origin_chat_key=CHAT_OWN,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.EMOTION,
        content=content,
        summary=content,
        event_time=db_now(),
        origin_kind=OriginKind.AFFECTION,
    )
    return await DBAffectionEvent.create(
        **key_of(domain, account, preset_id).as_columns(),
        source_platform_userid=account,
        chat_key=CHAT_OWN,
        delta=8,
        score_after=60,
        source=AffectionEventSource.LLM.value,
        reason="关系近了一步",
        memory_paragraph_id=paragraph.id,
    )


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch):
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    monkeypatch.setattr(config, "AI_CHAT_DEFAULT_PRESET_ID", None)
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    invalidate_cache()
    try:
        for tenant_id, name in ((TENANT_OWN, "alpha"), (TENANT_FOREIGN, "beta")):
            await DBTenant.create(id=tenant_id, name=name, display_name=name)
        for instance_key, tenant_id in ((INSTANCE_OWN, TENANT_OWN), (INSTANCE_FOREIGN, TENANT_FOREIGN)):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider=ADAPTER,
                tenant_id=tenant_id,
                metadata_json="",
            )
        for chat_key, instance_key, name in (
            (CHAT_OWN, INSTANCE_OWN, "周末开黑群"),
            (CHAT_FOREIGN, INSTANCE_FOREIGN, "别人家的群"),
        ):
            await DBChatChannel.create(
                chat_key=chat_key,
                adapter_key=ADAPTER,
                instance_key=instance_key,
                channel_id=chat_key.rsplit("-", 1)[-1],
                channel_name=name,
                channel_type="group",
                preset_id=PRESET_CAT,
                data="{}",
            )
        await DBPreset.create(
            id=PRESET_CAT,
            name="cat",
            title="猫娘",
            avatar="",
            content="设定",
            description="",
            tags="",
            author="",
        )
        # u1 在两个人设下各有一份；后建的那份（猫娘）是最近更新的
        u1_none = await make_record(DOMAIN_OWN, "u1", PRESET_NONE, score=5, nickname="小明", chat_key=CHAT_OWN)
        u1_cat = await make_record(
            DOMAIN_OWN, "u1", PRESET_CAT, score=120, nickname="小明", call_name="明哥", chat_key=CHAT_OWN, days=2
        )
        # u2 最近出现在别的租户的群里（脏数据）：租户视角下不给那个频道的名字
        u2_cat = await make_record(
            DOMAIN_OWN, "u2", PRESET_CAT, score=-20, nickname="阿花", chat_key=CHAT_FOREIGN, days=1
        )
        # u4 是 u1 合并进来的副号：它自己那一行是休眠数据
        await DBIdentityAlias.create(domain_id=DOMAIN_OWN.domain_id, platform_userid="u4", person_uid="u1")
        await make_record(DOMAIN_OWN, "u4", PRESET_CAT, score=50, nickname="小明的副号", chat_key=CHAT_OWN)
        # 别的租户的实例里的一份
        u9_cat = await make_record(DOMAIN_FOREIGN, "u9", PRESET_CAT, score=80, nickname="外人", chat_key=CHAT_FOREIGN)

        own = await make_milestone(
            DOMAIN_OWN, "u1", PRESET_CAT, paragraph_instance=INSTANCE_OWN, content="一起熬夜打完副本"
        )
        # 副号名下写的里程碑：按人取并集，挂到 u1 身上
        via_alias = await make_milestone(
            DOMAIN_OWN, "u4", PRESET_CAT, paragraph_instance=INSTANCE_OWN, content="副号那天也在"
        )
        # 段落在别的租户的实例里（实例被单独划走）：只留节点，不给正文
        moved = await make_milestone(
            DOMAIN_OWN, "u1", PRESET_NONE, paragraph_instance=INSTANCE_FOREIGN, content="不该看到的正文"
        )
        yield {
            "u1_none": u1_none,
            "u1_cat": u1_cat,
            "u2_cat": u2_cat,
            "u9_cat": u9_cat,
            "own": own,
            "via_alias": via_alias,
            "moved": moved,
        }
    finally:
        await Tortoise.close_connections()


async def graph_of(user: DBUser, **params: Any) -> AffectionGraphResponse:
    args: dict[str, Any] = {"search": "", "stage": None, "limit": 100, "instance_key": ""}
    args.update(params)
    return await get_affection_graph(**args, _current_user=user)


def nodes_of(graph: AffectionGraphResponse, kind: str) -> dict[str, Any]:
    return {node.id: node for node in graph.nodes if node.memory_type == kind}


def edges_of(graph: AffectionGraphResponse, kind: str) -> list[tuple[str, str]]:
    return sorted((edge.source, edge.target) for edge in graph.edges if edge.edge_type == kind)


def person_id(domain: ScopeDomain, account: str) -> str:
    return f"person-{domain.domain_id}-{account}"


def preset_id_of(domain: ScopeDomain, preset_id: int) -> str:
    return f"preset-{domain.domain_id}-{preset_id}"


def assert_star(graph: AffectionGraphResponse) -> None:
    """每条边恰好一头是人；边的两头都在节点里；节点与边的 ID 不重复"""
    kinds = {node.id: node.memory_type for node in graph.nodes}
    assert len(kinds) == len(graph.nodes)
    assert len({edge.id for edge in graph.edges}) == len(graph.edges)
    for edge in graph.edges:
        assert edge.source in kinds and edge.target in kinds
        assert [kinds[edge.source], kinds[edge.target]].count("person") == 1
    assert (graph.node_count, graph.edge_count) == (len(graph.nodes), len(graph.edges))


class TestScope:
    async def test_a_tenant_sees_its_own_domain(self, world: dict[str, Any]) -> None:
        graph = await graph_of(make_user(TENANT_OWN), instance_key=INSTANCE_OWN)

        assert_star(graph)
        assert set(nodes_of(graph, "person")) == {person_id(DOMAIN_OWN, "u1"), person_id(DOMAIN_OWN, "u2")}
        assert set(nodes_of(graph, "preset")) == {
            preset_id_of(DOMAIN_OWN, PRESET_CAT),
            preset_id_of(DOMAIN_OWN, PRESET_NONE),
        }
        # 合并后休眠的副号行不画，只计数
        assert (graph.total, graph.drawn, graph.skipped_merged) == (4, 3, 1)
        assert nodes_of(graph, "preset")[preset_id_of(DOMAIN_OWN, PRESET_CAT)].label == "猫娘"

    async def test_other_instances_are_not_found(self, world: dict[str, Any]) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await graph_of(make_user(TENANT_OWN), instance_key=INSTANCE_FOREIGN)
        with pytest.raises(ValidationError):
            await graph_of(make_user(TENANT_OWN))

    async def test_the_platform_sees_every_domain(self, world: dict[str, Any]) -> None:  # noqa: ARG002
        graph = await graph_of(make_user(None))

        assert_star(graph)
        people = nodes_of(graph, "person")
        assert person_id(DOMAIN_FOREIGN, "u9") in people
        assert preset_id_of(DOMAIN_FOREIGN, PRESET_CAT) in nodes_of(graph, "preset")
        # 平台方看得见所有频道的名字
        assert nodes_of(graph, "channel")[f"channel-{CHAT_FOREIGN}"].label == "别人家的群"


class TestShape:
    async def test_people_point_at_presets_with_score_and_stage(self, world: dict[str, Any]) -> None:
        graph = await graph_of(make_user(TENANT_OWN), instance_key=INSTANCE_OWN)

        u1, u2 = person_id(DOMAIN_OWN, "u1"), person_id(DOMAIN_OWN, "u2")
        assert edges_of(graph, "affection") == sorted(
            [
                (u1, preset_id_of(DOMAIN_OWN, PRESET_CAT)),
                (u1, preset_id_of(DOMAIN_OWN, PRESET_NONE)),
                (u2, preset_id_of(DOMAIN_OWN, PRESET_CAT)),
            ],
        )
        edge = next(edge for edge in graph.edges if edge.id == f"affection-{world['u1_cat'].id}")
        assert (edge.weight, edge.metadata["stage"], edge.metadata["record_id"]) == (
            120.0,
            AffectionStage.TRUSTED.value,
            world["u1_cat"].id,
        )
        assert edge.strength > next(e for e in graph.edges if e.id == f"affection-{world['u2_cat'].id}").strength

    async def test_a_person_carries_every_share_of_affection(self, world: dict[str, Any]) -> None:
        graph = await graph_of(make_user(TENANT_OWN), instance_key=INSTANCE_OWN)

        u1 = nodes_of(graph, "person")[person_id(DOMAIN_OWN, "u1")]
        # 名字与称呼取最近更新的那份（猫娘）
        assert (u1.label, u1.subtitle) == ("小明", "明哥")
        records = u1.metadata["records"]
        assert [(item["record_id"], item["score"], item["call_name"]) for item in records] == [
            (world["u1_cat"].id, 120, "明哥"),
            (world["u1_none"].id, 5, ""),
        ]
        assert records[0]["preset_name"] == "猫娘"
        # 最近一次出现：两份里最晚的那次
        assert u1.metadata["last_interact_at"] == (T0 + timedelta(days=2)).isoformat()
        assert u1.metadata["last_chat_key"] == CHAT_OWN
        assert u1.metadata["milestone_count"] == 3

    async def test_milestones_hang_on_the_person(self, world: dict[str, Any]) -> None:
        graph = await graph_of(make_user(TENANT_OWN), instance_key=INSTANCE_OWN)

        u1 = person_id(DOMAIN_OWN, "u1")
        milestones = nodes_of(graph, "milestone")
        ids = {f"milestone-{world[key].id}" for key in ("own", "via_alias", "moved")}
        assert set(milestones) == ids
        assert all(target == u1 for source, target in edges_of(graph, "milestone"))
        assert milestones[f"milestone-{world['own'].id}"].label == "一起熬夜打完副本"
        # 段落跟着实例挪到别的租户了：节点还在，正文不给
        moved = milestones[f"milestone-{world['moved'].id}"]
        assert (moved.status, moved.label, moved.metadata["content"]) == ("moved", "", "")

    async def test_channels_only_named_when_visible(self, world: dict[str, Any]) -> None:  # noqa: ARG002
        graph = await graph_of(make_user(TENANT_OWN), instance_key=INSTANCE_OWN)

        channels = nodes_of(graph, "channel")
        assert channels[f"channel-{CHAT_OWN}"].label == "周末开黑群"
        # 别的租户的群：只给会话标识，不给名字
        foreign = channels[f"channel-{CHAT_FOREIGN}"]
        assert (foreign.label, foreign.metadata["channel_name"]) == (CHAT_FOREIGN, "")
        assert edges_of(graph, "last_seen") == sorted(
            [
                (person_id(DOMAIN_OWN, "u1"), f"channel-{CHAT_OWN}"),
                (person_id(DOMAIN_OWN, "u2"), f"channel-{CHAT_FOREIGN}"),
            ],
        )


class TestFilters:
    async def test_search_and_stage(self, world: dict[str, Any]) -> None:  # noqa: ARG002
        searched = await graph_of(make_user(TENANT_OWN), instance_key=INSTANCE_OWN, search="阿花")
        assert set(nodes_of(searched, "person")) == {person_id(DOMAIN_OWN, "u2")}

        trusted = await graph_of(make_user(TENANT_OWN), instance_key=INSTANCE_OWN, stage=AffectionStage.TRUSTED)
        assert edges_of(trusted, "affection") == [
            (person_id(DOMAIN_OWN, "u1"), preset_id_of(DOMAIN_OWN, PRESET_CAT)),
        ]

    async def test_the_limit_has_a_floor(self, world: dict[str, Any]) -> None:  # noqa: ARG002
        graph = await graph_of(make_user(TENANT_OWN), instance_key=INSTANCE_OWN, limit=1)
        assert graph.drawn == 3


class TestBuilder:
    def test_milestones_per_share_are_capped(self) -> None:
        record = DBAffection(
            id=1,
            **key_of(DOMAIN_OWN, "u1", PRESET_CAT).as_columns(),
            nickname="小明",
            score=10,
            stage=stage_of(10).value,
            last_chat_key="",
            update_time=T0,
        )
        items = [
            {
                "paragraph_id": index + 10,
                "event_id": index + 1,
                "content": f"第{index}条",
                "summary": "",
                "status": "active",
            }
            for index in range(7)
        ]
        row = GraphRow(
            record=record,
            domain=DOMAIN_OWN,
            domain_label="本实例",
            preset_name="猫娘",
            in_use_channels=1,
            milestones=items,
        )

        graph = build_affection_graph([row], channel_names={}, milestones_per_record=5)

        assert sum(1 for node in graph.nodes if node["memory_type"] == "milestone") == 5
        # 没有最近出现的频道：不画频道节点
        assert not [node for node in graph.nodes if node["memory_type"] == "channel"]


def test_graph_route_comes_before_the_record_route() -> None:
    paths = [getattr(route, "path", "") for route in affection_router.router.routes]
    assert paths.index("/affection/graph") < paths.index("/affection/{record_id}")


class TestSummarySearch:
    """`GET /affection/summary` 的搜索：四个数都只统计筛出来的行

    概览与列表 / 关系图共用 `_apply_list_filters`（昵称或账号里含关键词）；空串不加条件，
    与不传逐字节相同。这条链路容易出现的错误是「总数筛了、最高分没筛」，所以 `top_score`
    单独断言：筛出来的最高分必须低于未筛时的那个。
    """

    async def test_search_narrows_total_stages_and_top_score(self, world: dict[str, Any]) -> None:  # noqa: ARG002
        user = make_user(TENANT_OWN)
        unfiltered = await get_affection_summary(search="", instance_key=INSTANCE_OWN, _current_user=user)
        summary = await get_affection_summary(search="阿花", instance_key=INSTANCE_OWN, _current_user=user)

        # 只有 u2 那一份命中「阿花」；它的分数是负的，而概览的最高分有 0 这个下限
        assert summary.total == 1
        assert summary.top_score == 0
        assert unfiltered.top_score == 120
        counts = {item.value: item.count for item in summary.stage_counts}
        matched_stage = stage_of(-20).value
        assert sum(counts.values()) == 1
        assert counts[matched_stage] == 1
        assert all(count == 0 for value, count in counts.items() if value != matched_stage)
        # 阶段清单本身与未筛时一致（只是计数变了）
        assert [item.value for item in summary.stage_counts] == [item.value for item in unfiltered.stage_counts]

    async def test_a_keyword_matching_several_people_keeps_their_max(self, world: dict[str, Any]) -> None:  # noqa: ARG002
        summary = await get_affection_summary(
            search="小明",
            instance_key=INSTANCE_OWN,
            _current_user=make_user(TENANT_OWN),
        )

        # 主号两份 + 副号那份（昵称「小明的副号」）；最高分来自猫娘那份
        assert summary.total == 3
        assert summary.top_score == 120
        assert sum(item.count for item in summary.stage_counts) == 3

    async def test_empty_search_is_the_same_as_not_passing_it(self, world: dict[str, Any]) -> None:  # noqa: ARG002
        # 不传时 FastAPI 注入的默认值就是 ""；直接调用这里拿到的是 `Query` 对象，
        # 所以按签名核对默认值，再钉住空串下的结果（不加任何条件）。
        params = inspect.signature(get_affection_summary).parameters
        assert params["search"].default.default == ""

        summary = await get_affection_summary(
            search="",
            instance_key=INSTANCE_OWN,
            _current_user=make_user(TENANT_OWN),
        )

        assert summary.total == 4
        assert summary.top_score == 120
        assert sum(item.count for item in summary.stage_counts) == 4
