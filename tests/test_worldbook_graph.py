"""世界书图谱：`GET /worldbook/graph` 与单条读接口

## 钉什么

- 结构：节点 ID 不重复、每条边两端都在节点里、计数与实际一致；书、条目与人设 / 频道 /
  人三种绑定对象都有；域书不建绑定节点；同一人设被两本书绑定只建一个节点。
- 作用域：租户只看到自己实例的书，外加平台方给全局人设建、人设对它可见的只读书；别的
  实例的书与条目看不到；带别人实例的 `book_id` 是 404；平台方不带实例看全部。
- 注入统计：最近提示词明细里的 `worldbook` / `worldbook_dropped` / `worldbook_lookup`
  分别数成 injected / dropped / lookups；别的实例的明细不计入；`rounds_scanned` 对。
- 截断与排序：条目多于 `limit` 时取注入最多的、常驻优先；被截掉的条目没有悬空的边。
- `include_inactive`、`q`、`book_id` 三个筛选。
- 关键词引用：纯文本关键词、大小写不敏感、自己不引用自己、正则与单字关键词不出边；
  限时改成 0 时接口照常返回并标 `references_truncated`。
- `GET /worldbook/entries/{id}`：可读返回、别人实例 404、只读开放的平台方书能读。

## 数据

内存 SQLite 上的真表；租户、实例、人设、账号、书名与条目文案都是现编的。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.routers import worldbook as worldbook_router
from nekro_agent.routers.worldbook import get_entry, get_worldbook_graph
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.worldbook import EntryItem, WorldbookGraphResponse
from nekro_agent.services.user.role import Role
from nekro_agent.services.worldbook import graph_view as graph_service

ADAPTER = "onebot_v11"
INSTANCE_OWN = "inst-atlas-alpha"
INSTANCE_FOREIGN = "inst-atlas-beta"
TENANT_OWN = 61
TENANT_FOREIGN = 72
CHAT_OWN = f"{ADAPTER}-{INSTANCE_OWN}-group_alpha"
CHAT_FOREIGN = f"{ADAPTER}-{INSTANCE_FOREIGN}-group_beta"
PRESET_SHARED = 17
ACCOUNT_OWN = "account-alpha"
T0 = datetime(2026, 2, 1, tzinfo=timezone.utc)


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


async def make_book(
    name: str,
    *,
    scope_kind: str = "domain",
    adapter_key: str = ADAPTER,
    instance_key: str = INSTANCE_OWN,
    preset_id: int = 0,
    chat_key: str = "",
    person_account: str = "",
    enabled: bool = True,
) -> DBWbBook:
    return await DBWbBook.create(
        name=name,
        description="",
        scope_kind=scope_kind,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        chat_key=chat_key,
        person_account=person_account,
        enabled=enabled,
    )


async def make_entry(
    book: DBWbBook,
    *,
    uid: int,
    comment: str = "",
    keys: Optional[list[str]] = None,
    content: str = "",
    constant: bool = False,
    use_regex: bool = False,
    enabled: bool = True,
    status: str = "active",
    order: int = 100,
) -> DBWbEntry:
    return await DBWbEntry.create(
        book_id=book.id,
        uid=uid,
        comment=comment,
        keys=keys or [],
        secondary_keys=[],
        selective_logic="and_any",
        use_regex=use_regex,
        constant=constant,
        content=content,
        order=order,
        position="before_history",
        depth=4,
        enabled=enabled,
        status=status,
        trusted=False,
    )


def make_round(
    round_id: str,
    items: list[dict[str, Any]],
    *,
    instance_key: str = INSTANCE_OWN,
    chat_key: str = CHAT_OWN,
) -> DBMemPromptRound:
    return DBMemPromptRound(
        round_id=round_id,
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        worldbook_items=items,
        started_at=T0,
        finished_at=T0,
    )


async def graph_of(user: DBUser, **params: Any) -> WorldbookGraphResponse:
    args: dict[str, Any] = {
        "instance_key": "",
        "adapter_key": "",
        "book_id": None,
        "q": "",
        "include_inactive": False,
        "limit": 240,
    }
    args.update(params)
    return await get_worldbook_graph(**args, _current_user=user)


def nodes_of(graph: WorldbookGraphResponse, kind: str) -> dict[str, Any]:
    return {node.id: node for node in graph.nodes if node.memory_type == kind}


def edges_of(graph: WorldbookGraphResponse, kind: str) -> list[tuple[str, str]]:
    return sorted((edge.source, edge.target) for edge in graph.edges if edge.edge_type == kind)


def assert_graph_consistent(graph: WorldbookGraphResponse) -> None:
    assert graph.node_count == len(graph.nodes)
    assert graph.edge_count == len(graph.edges)
    node_ids = [node.id for node in graph.nodes]
    assert len(node_ids) == len(set(node_ids))
    known = set(node_ids)
    for edge in graph.edges:
        assert edge.source in known, edge.id
        assert edge.target in known, edge.id


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch):
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
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
            (CHAT_OWN, INSTANCE_OWN, "本实例频道"),
            (CHAT_FOREIGN, INSTANCE_FOREIGN, "别的实例频道"),
        ):
            await DBChatChannel.create(
                chat_key=chat_key,
                adapter_key=ADAPTER,
                instance_key=instance_key,
                channel_id=chat_key.rsplit("-", 1)[-1],
                channel_name=name,
                channel_type="group",
                data="{}",
            )
        await DBPreset.create(
            id=PRESET_SHARED,
            owner_instance_key="",
            name="shared",
            title="共享人设",
            avatar="",
            content="设定",
            description="",
            tags="",
            author="",
        )
        await DBPreset.create(
            id=PRESET_SHARED + 6,
            owner_instance_key=INSTANCE_OWN,
            name="local",
            title="本实例人设",
            avatar="",
            content="设定",
            description="",
            tags="",
            author="",
        )

        local_book = await make_book("本地设定")
        preset_book_a = await make_book(
            "人设设定一",
            scope_kind="preset",
            preset_id=PRESET_SHARED,
        )
        preset_book_b = await make_book(
            "人设设定二",
            scope_kind="preset",
            preset_id=PRESET_SHARED,
        )
        channel_book = await make_book("频道设定", scope_kind="channel", chat_key=CHAT_OWN)
        person_book = await make_book("个人设定", scope_kind="person", person_account=ACCOUNT_OWN)
        platform_book = await make_book(
            "平台共享设定",
            scope_kind="preset",
            adapter_key="",
            instance_key="",
            preset_id=PRESET_SHARED,
        )
        foreign_book = await make_book(
            "别的实例设定",
            adapter_key=ADAPTER,
            instance_key=INSTANCE_FOREIGN,
        )

        alpha = await make_entry(
            local_book,
            uid=0,
            comment="阿尔法",
            keys=["星尘"],
            content="阿尔法的正文",
            order=300,
        )
        beta = await make_entry(
            local_book,
            uid=1,
            comment="贝塔",
            keys=["月影"],
            content="这里提到了星尘。",
            order=200,
        )
        gamma = await make_entry(
            local_book,
            uid=2,
            comment="伽马",
            keys=["星尘", "月影"],
            content="伽马自己的正文",
            order=100,
        )
        constant = await make_entry(
            local_book,
            uid=3,
            comment="常驻条目",
            keys=[],
            content="常驻正文",
            constant=True,
            order=400,
        )
        disabled = await make_entry(
            local_book,
            uid=4,
            comment="停用条目",
            keys=["停用"],
            content="停用正文",
            enabled=False,
        )
        quarantined = await make_entry(
            local_book,
            uid=5,
            comment="隔离条目",
            keys=["隔离"],
            content="隔离正文",
            status="quarantined",
        )
        regex_entry = await make_entry(
            local_book,
            uid=6,
            comment="正则条目",
            keys=["星.*"],
            content="正则正文",
            use_regex=True,
        )
        short_key = await make_entry(
            local_book,
            uid=7,
            comment="单字条目",
            keys=["星"],
            content="单字正文",
        )
        self_ref = await make_entry(
            local_book,
            uid=8,
            comment="自引用",
            keys=["自身"],
            content="自身重复自身",
        )
        starved = await make_entry(
            local_book,
            uid=9,
            comment="饥饿条目",
            keys=[],
            content="饥饿正文",
            order=500,
        )
        await make_entry(
            preset_book_a,
            uid=0,
            comment="人设条目",
            keys=["人设"],
            content="人设正文",
        )
        platform_entry = await make_entry(
            platform_book,
            uid=0,
            comment="平台条目",
            keys=["平台"],
            content="平台正文",
        )
        foreign_entry = await make_entry(
            foreign_book,
            uid=0,
            comment="外部条目",
            keys=["外部"],
            content="外部正文",
        )

        await make_round(
            "prompt-own-a",
            [
                {"type": "worldbook", "id": alpha.id, "book_id": local_book.id},
                {"type": "worldbook_dropped", "id": alpha.id, "book_id": local_book.id, "reason": "global_tokens"},
                {"type": "worldbook_lookup", "id": alpha.id, "book_id": local_book.id},
                {"type": "worldbook", "id": beta.id, "book_id": local_book.id},
            ],
        ).save()
        await make_round(
            "prompt-own-b",
            [
                {"type": "worldbook", "id": constant.id, "book_id": local_book.id},
                {"type": "worldbook_dropped", "id": quarantined.id, "book_id": local_book.id, "reason": "book_tokens"},
                {"type": "worldbook_dropped", "id": starved.id, "book_id": local_book.id, "reason": "global_tokens"},
            ],
        ).save()
        await make_round(
            "prompt-foreign",
            [
                {"type": "worldbook", "id": alpha.id, "book_id": local_book.id},
                {"type": "worldbook", "id": foreign_entry.id, "book_id": foreign_book.id},
            ],
            instance_key=INSTANCE_FOREIGN,
            chat_key=CHAT_FOREIGN,
        ).save()

        yield {
            "local_book": local_book,
            "preset_book_a": preset_book_a,
            "preset_book_b": preset_book_b,
            "channel_book": channel_book,
            "person_book": person_book,
            "platform_book": platform_book,
            "foreign_book": foreign_book,
            "alpha": alpha,
            "beta": beta,
            "gamma": gamma,
            "constant": constant,
            "disabled": disabled,
            "quarantined": quarantined,
            "regex_entry": regex_entry,
            "short_key": short_key,
            "self_ref": self_ref,
            "starved": starved,
            "platform_entry": platform_entry,
            "foreign_entry": foreign_entry,
        }
    finally:
        await Tortoise.close_connections()


async def test_structure_and_binding_nodes(world: dict[str, Any]) -> None:
    user = make_user(TENANT_OWN)
    graph = await graph_of(user, instance_key=INSTANCE_OWN)

    assert_graph_consistent(graph)
    assert nodes_of(graph, "book")
    assert nodes_of(graph, "entry")
    assert nodes_of(graph, "preset")
    assert nodes_of(graph, "channel")
    assert nodes_of(graph, "person")

    # 域级的书没有绑定对象
    domain_book_id = f"book-{world['local_book'].id}"
    assert not [
        edge for edge in graph.edges if edge.edge_type == "book_binding" and edge.source == domain_book_id
    ]

    # 同一人设被两本书绑定，只建一个节点，book_ids 列出这些书
    preset_nodes = [node for node in graph.nodes if node.id == f"preset-{PRESET_SHARED}"]
    assert len(preset_nodes) == 1
    assert set(preset_nodes[0].metadata["book_ids"]) >= {
        world["preset_book_a"].id,
        world["preset_book_b"].id,
    }

    # 每条 book_entry 两端都在节点里；条目节点数等于 entries_drawn
    entry_ids = {node.id for node in graph.nodes if node.memory_type == "entry"}
    for edge in graph.edges:
        if edge.edge_type == "book_entry":
            assert edge.source.startswith("book-")
            assert edge.target in entry_ids
    assert len(entry_ids) == graph.entries_drawn


async def test_scope_tenant_and_platform(world: dict[str, Any]) -> None:
    tenant = make_user(TENANT_OWN)
    graph = await graph_of(tenant, instance_key=INSTANCE_OWN)
    book_ids = {node.ref_id for node in graph.nodes if node.memory_type == "book"}
    assert world["local_book"].id in book_ids
    assert world["platform_book"].id in book_ids
    assert world["foreign_book"].id not in book_ids
    entry_ids = {node.ref_id for node in graph.nodes if node.memory_type == "entry"}
    assert world["foreign_entry"].id not in entry_ids

    with pytest.raises(NotFoundError):
        await graph_of(tenant, instance_key=INSTANCE_OWN, book_id=world["foreign_book"].id)
    with pytest.raises(ValidationError):
        await graph_of(tenant)

    platform = make_user(None)
    all_graph = await graph_of(platform)
    all_book_ids = {node.ref_id for node in all_graph.nodes if node.memory_type == "book"}
    assert world["foreign_book"].id in all_book_ids
    assert world["platform_book"].id in all_book_ids
    # 平台方不带实例：明细行也不按实例过滤
    assert all_graph.rounds_scanned == 3


async def test_injection_stats(world: dict[str, Any]) -> None:
    user = make_user(TENANT_OWN)
    graph = await graph_of(user, instance_key=INSTANCE_OWN)
    entries = {node.ref_id: node for node in graph.nodes if node.memory_type == "entry"}

    alpha = entries[world["alpha"].id]
    assert alpha.metadata["injected"] == 1
    assert alpha.metadata["dropped"] == 1
    assert alpha.metadata["lookups"] == 1
    assert alpha.metadata["starved"] is False

    beta = entries[world["beta"].id]
    assert beta.metadata["injected"] == 1
    assert beta.metadata["dropped"] == 0

    assert graph.rounds_scanned == 2
    assert entries[world["constant"].id].metadata["injected"] == 1
    assert entries[world["starved"].id].metadata["starved"] is True
    assert entries[world["starved"].id].metadata["injected"] == 0
    assert graph.entries_total == 10


async def test_limit_ordering_and_no_dangling_edges(world: dict[str, Any]) -> None:
    extra_book = await make_book("大量条目")
    extra_entries = []
    for index in range(60):
        extra_entries.append(
            await make_entry(
                extra_book,
                uid=index,
                comment=f"条目 {index:02d}",
                keys=[],
                content=f"正文 {index}",
                order=index,
            ),
        )
    # 让最后一条成为注入最多的：排序应按注入次数优先，而不是 order
    hot = extra_entries[-1]
    await make_round(
        "prompt-hot",
        [{"type": "worldbook", "id": hot.id, "book_id": extra_book.id}],
    ).save()

    user = make_user(TENANT_OWN)
    graph = await graph_of(user, instance_key=INSTANCE_OWN, book_id=extra_book.id, limit=50)
    assert graph.entries_total == 60
    assert graph.entries_drawn == 50
    assert graph.entries_total > graph.entries_drawn
    entry_nodes = {node.ref_id: node for node in graph.nodes if node.memory_type == "entry"}
    assert hot.id in entry_nodes
    assert len(entry_nodes) == 50
    assert_graph_consistent(graph)


async def test_include_inactive(world: dict[str, Any]) -> None:
    user = make_user(TENANT_OWN)
    graph = await graph_of(user, instance_key=INSTANCE_OWN)
    ids = {node.ref_id for node in graph.nodes if node.memory_type == "entry"}
    assert world["disabled"].id not in ids
    assert world["quarantined"].id not in ids

    graph2 = await graph_of(user, instance_key=INSTANCE_OWN, include_inactive=True)
    entry_nodes = {node.ref_id: node for node in graph2.nodes if node.memory_type == "entry"}
    assert entry_nodes[world["disabled"].id].status == "inactive"
    assert entry_nodes[world["quarantined"].id].status == "quarantined"


async def test_q_and_book_filter(world: dict[str, Any]) -> None:
    user = make_user(TENANT_OWN)
    by_comment = await graph_of(user, instance_key=INSTANCE_OWN, q="阿尔法")
    assert {node.ref_id for node in by_comment.nodes if node.memory_type == "entry"} == {world["alpha"].id}

    by_key = await graph_of(user, instance_key=INSTANCE_OWN, q="月影")
    assert {node.ref_id for node in by_key.nodes if node.memory_type == "entry"} == {
        world["beta"].id,
        world["gamma"].id,
    }

    by_content = await graph_of(user, instance_key=INSTANCE_OWN, q="常驻正文")
    assert {node.ref_id for node in by_content.nodes if node.memory_type == "entry"} == {world["constant"].id}

    one_book = await graph_of(user, instance_key=INSTANCE_OWN, book_id=world["local_book"].id)
    book_ids = {node.ref_id for node in one_book.nodes if node.memory_type == "book"}
    assert book_ids == {world["local_book"].id}


async def test_keyword_references(world: dict[str, Any]) -> None:
    user = make_user(TENANT_OWN)
    graph = await graph_of(user, instance_key=INSTANCE_OWN)
    references = [edge for edge in graph.edges if edge.edge_type == "key_reference"]
    # 贝塔的正文里有「星尘」，指向阿尔法
    assert (f"entry-{world['beta'].id}", f"entry-{world['alpha'].id}") in {
        (edge.source, edge.target) for edge in references
    }
    assert all(edge.metadata["derived"] is True for edge in references)
    # 正则条目、单字关键词、自引用都不出边
    assert not [edge for edge in references if edge.source == f"entry-{world['regex_entry'].id}"]
    assert not [edge for edge in references if edge.source == f"entry-{world['short_key'].id}"]
    assert not [edge for edge in references if edge.source == f"entry-{world['self_ref'].id}"]
    # 同一对只留一条
    pairs = [(edge.source, edge.target) for edge in references]
    assert len(pairs) == len(set(pairs))


async def test_keyword_references_are_case_insensitive_and_timeout(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_user(TENANT_OWN)
    book = await make_book("大小写")
    source = await make_entry(book, uid=0, comment="来源", keys=["Solar"], content="正文")
    target = await make_entry(book, uid=1, comment="目标", keys=["其它"], content="这里写了 SOLAR")

    graph = await graph_of(user, instance_key=INSTANCE_OWN, book_id=book.id)
    assert (f"entry-{target.id}", f"entry-{source.id}") in {
        (edge.source, edge.target) for edge in graph.edges if edge.edge_type == "key_reference"
    }

    monkeypatch.setattr(graph_service, "REFERENCE_TIMEOUT_SECONDS", 0)
    timed_out = await graph_of(user, instance_key=INSTANCE_OWN, book_id=book.id)
    assert timed_out.references_truncated is True
    assert timed_out.node_count > 0


async def test_get_entry_endpoint_scope(world: dict[str, Any]) -> None:
    tenant = make_user(TENANT_OWN)
    item = await get_entry(world["alpha"].id, _current_user=tenant)
    assert isinstance(item, EntryItem)
    assert item.id == world["alpha"].id
    assert item.book_id == world["local_book"].id

    with pytest.raises(NotFoundError):
        await get_entry(world["foreign_entry"].id, _current_user=tenant)

    # 平台方给全局人设建、对本实例可见的书：租户能只读打开
    platform_item = await get_entry(world["platform_entry"].id, _current_user=tenant)
    assert platform_item.id == world["platform_entry"].id

    with pytest.raises(NotFoundError):
        await get_entry(999999, _current_user=tenant)


def test_graph_route_before_book_route() -> None:
    paths = [getattr(route, "path", "") for route in worldbook_router.router.routes]
    assert paths.index("/worldbook/graph") < paths.index("/worldbook/books/{book_id}")
    assert paths.index("/worldbook/entries/{entry_id}") < paths.index("/worldbook/entries/{entry_id}/action")
