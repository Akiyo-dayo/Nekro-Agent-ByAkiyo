"""管理端可见范围：新叶子 / 新字段的三渲染器对拍，以及身份到范围的换算。

管理端的范围与召回共用同一棵条件树，因此这里同样要求纯 Python、数据库、Qdrant 三方在
同一批固定数据与同一批随机数据上给出一致的 ID 集合。只测数据库一侧会漏掉「Qdrant 的
`Range(gt=)` 与 SQL 的 `> 0` 不同」这类缺陷，而它恰好决定工作区行会不会出现在域视图里。
"""

from __future__ import annotations

import os
import random
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.memory.instance_scope import (
    AdminFilter,
    AdminMemoryScope,
    admin_row_visible,
    resolve_admin_scope,
)
from nekro_agent.services.memory.scope_condition import (
    FALSE,
    TABLE_FIELDS,
    Gt,
    MemTable,
    ScopeField,
    evaluate,
    gt,
    to_qdrant_filter,
    to_tortoise_q,
)
from nekro_agent.services.user.role import Role

PG_DSN_ENV = "NA_TEST_PG_DSN"
_MODEL_MODULES = ["nekro_agent.models"]
_VECTOR = [0.1, 0.2, 0.3, 0.4]

_ADAPTERS = ("onebot_v11", "minecraft")
_INSTANCES = ("i1", "i2")
_CHATS = ("gA", "gB")
_PRESETS = (0, 7, 9)


def _pg_dsn_or_skip() -> str:
    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(f"需要真实 PostgreSQL：设 {PG_DSN_ENV} 后运行；SQLite 腿始终运行。")
    return dsn


@pytest.fixture(params=["sqlite", "pg"], ids=["sqlite", "pg"])
async def db(request: pytest.FixtureRequest) -> Any:
    """每个后端一套真表；PG 在随机 schema 里建、结束 DROP SCHEMA CASCADE。"""
    if request.param == "sqlite":
        EXECUTOR_CACHE.clear()
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": _MODEL_MODULES})
        await Tortoise.generate_schemas()
        try:
            yield None
        finally:
            await Tortoise.close_connections()
            EXECUTOR_CACHE.clear()
        return

    dsn = _pg_dsn_or_skip()
    import asyncpg

    schema = f"na_test_admin_scope_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    EXECUTOR_CACHE.clear()
    await Tortoise.init(
        db_url=f"{dsn}{sep}{urlencode({'schema': schema})}",
        modules={"models": _MODEL_MODULES},
    )
    await Tortoise.generate_schemas()
    try:
        rows = await connections.get("default").execute_query_dict(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'mem_paragraph'",
        )
        assert schema in {str(row["table_schema"]) for row in rows}, "没有在真 PG 的目标 schema 里建出表"
        yield schema
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


@dataclass(frozen=True)
class ParagraphSpec:
    id: int
    workspace_id: int
    adapter_key: str
    instance_key: str
    preset_id: int
    origin_chat_key: str | None
    person_accounts: tuple[str, ...] = ()
    content: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "adapter_key": self.adapter_key,
            "instance_key": self.instance_key,
            "preset_id": self.preset_id,
            "origin_chat_key": self.origin_chat_key,
            "person_accounts": list(self.person_accounts),
        }


@dataclass(frozen=True)
class EpisodeSpec:
    id: int
    workspace_id: int
    adapter_key: str
    instance_key: str
    preset_id: int
    origin_chat_key: str | None

    def as_row(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "adapter_key": self.adapter_key,
            "instance_key": self.instance_key,
            "preset_id": self.preset_id,
            "origin_chat_key": self.origin_chat_key,
        }


@dataclass(frozen=True)
class SummarySpec:
    id: int
    adapter_key: str
    instance_key: str
    preset_id: int
    chat_key: str

    def as_row(self) -> dict[str, Any]:
        return {
            "adapter_key": self.adapter_key,
            "instance_key": self.instance_key,
            "preset_id": self.preset_id,
            "chat_key": self.chat_key,
        }


_MODELS: dict[MemTable, type[Any]] = {
    MemTable.PARAGRAPH: DBMemParagraph,
    MemTable.EPISODE: DBMemEpisode,
    MemTable.SUMMARY: DBMemChannelSummary,
}

_ROWS: dict[MemTable, list[Any]] = {
    MemTable.PARAGRAPH: [],
    MemTable.EPISODE: [],
    MemTable.SUMMARY: [],
}


async def _create_paragraph(spec: ParagraphSpec) -> None:
    row = await DBMemParagraph.create(
        id=spec.id,
        workspace_id=spec.workspace_id,
        adapter_key=spec.adapter_key,
        instance_key=spec.instance_key,
        preset_id=spec.preset_id,
        origin_chat_key=spec.origin_chat_key,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content=f"段落内容 {spec.id}",
        summary=f"摘要 {spec.id}",
        origin_kind=OriginKind.CONSOLIDATION,
    )
    for account in spec.person_accounts:
        await DBMemParagraphPerson.create(paragraph_id=row.id, account=account)
    _ROWS[MemTable.PARAGRAPH].append(spec)


async def _create_episode(spec: EpisodeSpec) -> None:
    await DBMemEpisode.create(
        id=spec.id,
        workspace_id=spec.workspace_id,
        adapter_key=spec.adapter_key,
        instance_key=spec.instance_key,
        preset_id=spec.preset_id,
        origin_chat_key=spec.origin_chat_key,
        origin_chat_type="group",
        title=f"事件 {spec.id}",
        narrative_summary=f"事件叙述 {spec.id}",
    )
    _ROWS[MemTable.EPISODE].append(spec)


async def _create_summary(spec: SummarySpec) -> None:
    await DBMemChannelSummary.create(
        id=spec.id,
        adapter_key=spec.adapter_key,
        instance_key=spec.instance_key,
        preset_id=spec.preset_id,
        chat_key=spec.chat_key,
        version=spec.id,
        content=f"摘要正文 {spec.id}",
    )
    _ROWS[MemTable.SUMMARY].append(spec)


async def _create(table: MemTable, spec: Any) -> None:
    if table is MemTable.PARAGRAPH:
        await _create_paragraph(spec)
    elif table is MemTable.EPISODE:
        await _create_episode(spec)
    else:
        await _create_summary(spec)


def _rows_of(table: MemTable) -> list[Any]:
    return _ROWS[table]


def _payload_of(spec: ParagraphSpec) -> dict[str, Any]:
    return {
        "workspace_id": spec.workspace_id,
        "adapter_key": spec.adapter_key,
        "instance_key": spec.instance_key,
        "preset_id": spec.preset_id,
        "origin_chat_key": spec.origin_chat_key,
        "person_accounts": sorted(spec.person_accounts),
    }


@pytest.fixture
async def qdrant() -> Any:
    client = AsyncQdrantClient(location=":memory:")
    collection = f"admin_scope_{uuid.uuid4().hex}"
    await client.create_collection(
        collection_name=collection,
        vectors_config=qdrant_models.VectorParams(size=len(_VECTOR), distance=qdrant_models.Distance.COSINE),
    )
    try:
        yield client, collection
    finally:
        await client.close()


@pytest.fixture(autouse=True)
def _reset_rows() -> Any:
    for table in _ROWS:
        _ROWS[table].clear()
    yield
    for table in _ROWS:
        _ROWS[table].clear()


async def _db_ids(scope: AdminMemoryScope, table: MemTable, f: AdminFilter) -> set[int]:
    queryset = scope.apply(_MODELS[table].all(), table, f)
    return {int(row.id) async for row in queryset}


async def _qdrant_ids(
    client: AsyncQdrantClient,
    collection: str,
    condition: Any,
) -> set[int]:
    if condition is FALSE:
        return set()
    points = await client.search(
        collection_name=collection,
        query_vector=list(_VECTOR),
        query_filter=to_qdrant_filter(condition),
        limit=10000,
        with_payload=False,
    )
    return {int(point.id) for point in points}


class TestGtAndNewFields:
    def test_table_fields_include_the_admin_columns(self) -> None:
        for table in (MemTable.PARAGRAPH, MemTable.EPISODE, MemTable.ENTITY, MemTable.RELATION):
            assert ScopeField.PRESET_ID in TABLE_FIELDS[table]
        for table in (MemTable.SUMMARY, MemTable.SUMMARY_STATE):
            assert TABLE_FIELDS[table] == {
                ScopeField.ADAPTER_KEY,
                ScopeField.INSTANCE_KEY,
                ScopeField.CHAT_KEY,
                ScopeField.PRESET_ID,
            }
        # 摘要类表没有工作区分区与来源频道这两列，渲染它们必须是调用方的 bug。
        for table in (MemTable.SUMMARY, MemTable.SUMMARY_STATE):
            assert ScopeField.WORKSPACE_ID not in TABLE_FIELDS[table]
            assert ScopeField.ORIGIN_CHAT_KEY not in TABLE_FIELDS[table]

    def test_gt_renders_to_the_three_shapes(self) -> None:
        node = gt(ScopeField.WORKSPACE_ID, 0)
        assert isinstance(node, Gt)
        assert to_tortoise_q(node, MemTable.PARAGRAPH)
        rendered = to_qdrant_filter(node)
        assert rendered.must[0].range is not None
        assert rendered.must[0].range.gt == 0
        assert evaluate(node, {"workspace_id": 1}) is True
        assert evaluate(node, {"workspace_id": 0}) is False
        assert evaluate(node, {"workspace_id": None}) is False
        assert evaluate(node, {}) is False

    async def test_fixed_dataset_three_way(self, db: Any, qdrant: Any) -> None:
        client, collection = qdrant
        specs = [
            ParagraphSpec(1, 0, "onebot_v11", "i1", 7, "gA", ("u151",)),
            ParagraphSpec(2, 1, "onebot_v11", "i1", 7, "gA", ("u151",)),
            ParagraphSpec(3, 2, "minecraft", "i2", 9, "gB", ("uxw",)),
            ParagraphSpec(4, 0, "minecraft", "i2", 0, None, ()),
            ParagraphSpec(5, 1, "onebot_v11", "i2", 9, "gB", ("u151", "uxw")),
        ]
        for spec in specs:
            await _create_paragraph(spec)
            await client.upsert(
                collection_name=collection,
                points=[
                    qdrant_models.PointStruct(id=spec.id, vector=list(_VECTOR), payload=_payload_of(spec)),
                ],
            )

        cases = [
            AdminMemoryScope.platform_all(),
            AdminMemoryScope.for_pairs([("onebot_v11", "i1")]),
            AdminMemoryScope.for_pairs([("onebot_v11", "i1"), ("minecraft", "i2")]),
            AdminMemoryScope.for_pairs([]),
        ]
        filters = [
            AdminFilter(),
            AdminFilter(partition="domain"),
            AdminFilter(partition="workspace"),
            AdminFilter(chat_key="gA"),
            AdminFilter(preset_id=7),
            AdminFilter(person_accounts=frozenset({"u151"})),
            AdminFilter(person_accounts=frozenset()),
            AdminFilter(partition="workspace", preset_id=9, chat_key="gB"),
        ]
        for scope in cases:
            for f in filters:
                condition = scope.condition(MemTable.PARAGRAPH, f)
                evaluated = {spec.id for spec in specs if evaluate(condition, spec.as_row())}
                queried = await _db_ids(scope, MemTable.PARAGRAPH, f)
                assert evaluated == queried, f"数据库不一致: scope={scope!r}, filter={f!r}"
                qdrant_ids = await _qdrant_ids(client, collection, condition)
                assert evaluated == qdrant_ids, f"Qdrant 不一致: scope={scope!r}, filter={f!r}"

    async def test_fixed_dataset_episode_and_summary(self, db: Any) -> None:
        episodes = [
            EpisodeSpec(1, 0, "onebot_v11", "i1", 7, "gA"),
            EpisodeSpec(2, 1, "minecraft", "i2", 9, "gB"),
            EpisodeSpec(3, 0, "minecraft", "i2", 0, None),
        ]
        summaries = [
            SummarySpec(1, "onebot_v11", "i1", 7, "gA"),
            SummarySpec(2, "minecraft", "i2", 9, "gB"),
            SummarySpec(3, "minecraft", "i2", 0, "gA"),
        ]
        for episode in episodes:
            await _create_episode(episode)
        for summary in summaries:
            await _create_summary(summary)

        summary_scope = AdminMemoryScope.for_pairs([("onebot_v11", "i1"), ("minecraft", "i2")])
        summary_cases = [
            AdminFilter(),
            AdminFilter(chat_key="gA"),
            AdminFilter(preset_id=0),
            AdminFilter(chat_key="gB", preset_id=9),
        ]
        for f in summary_cases:
            condition = summary_scope.condition(MemTable.SUMMARY, f)
            evaluated = {spec.id for spec in summaries if evaluate(condition, spec.as_row())}
            queried = await _db_ids(summary_scope, MemTable.SUMMARY, f)
            assert evaluated == queried, f"摘要表不一致: filter={f!r}"

        episode_scope = AdminMemoryScope.platform_all()
        for f in (AdminFilter(), AdminFilter(partition="domain"), AdminFilter(chat_key="gA"), AdminFilter(preset_id=9)):
            condition = episode_scope.condition(MemTable.EPISODE, f)
            evaluated = {spec.id for spec in episodes if evaluate(condition, spec.as_row())}
            queried = await _db_ids(episode_scope, MemTable.EPISODE, f)
            assert evaluated == queried, f"episode 不一致: filter={f!r}"

    async def test_random_200_cases(self, db: Any, qdrant: Any) -> None:
        client, collection = qdrant
        rng = random.Random(20261108)

        paragraphs: list[ParagraphSpec] = []
        episodes: list[EpisodeSpec] = []
        summaries: list[SummarySpec] = []
        for row_id in range(1, 41):
            adapter_key = rng.choice(_ADAPTERS)
            instance_key = rng.choice(_INSTANCES)
            paragraphs.append(
                ParagraphSpec(
                    row_id,
                    rng.choice([0, 0, 1, 2]),
                    adapter_key,
                    instance_key,
                    rng.choice(_PRESETS),
                    rng.choice([*_CHATS, None]),
                    tuple(rng.sample(["u151", "uxw", "uzz"], rng.randint(0, 2))),
                ),
            )
            episodes.append(
                EpisodeSpec(
                    row_id,
                    rng.choice([0, 0, 1, 2]),
                    adapter_key,
                    instance_key,
                    rng.choice(_PRESETS),
                    rng.choice([*_CHATS, None]),
                ),
            )
            summaries.append(
                SummarySpec(row_id, adapter_key, instance_key, rng.choice(_PRESETS), rng.choice(_CHATS)),
            )

        for spec in paragraphs:
            await _create_paragraph(spec)
            await client.upsert(
                collection_name=collection,
                points=[
                    qdrant_models.PointStruct(id=spec.id, vector=list(_VECTOR), payload=_payload_of(spec)),
                ],
            )
        for spec in episodes:
            await _create_episode(spec)
        for spec in summaries:
            await _create_summary(spec)

        pair_pool = [("onebot_v11", "i1"), ("onebot_v11", "i2"), ("minecraft", "i1"), ("minecraft", "i2")]
        for index in range(200):
            table = rng.choice([MemTable.PARAGRAPH, MemTable.EPISODE, MemTable.SUMMARY])
            if rng.random() < 0.2:
                scope = AdminMemoryScope.platform_all()
            else:
                pairs = rng.sample(pair_pool, rng.randint(0, len(pair_pool)))
                scope = AdminMemoryScope.for_pairs(pairs)
            partition = "all" if table is MemTable.SUMMARY else rng.choice(["all", "domain", "workspace"])
            person = None
            if table is MemTable.PARAGRAPH and rng.random() < 0.3:
                person = frozenset(rng.sample(["u151", "uxw", "uzz"], rng.randint(0, 2)))
            f = AdminFilter(
                partition=partition,
                chat_key=rng.choice([None, *_CHATS]),
                preset_id=rng.choice([None, *_PRESETS]),
                person_accounts=person,
            )
            condition = scope.condition(table, f)
            evaluated = {spec.id for spec in _rows_of(table) if evaluate(condition, spec.as_row())}
            queried = await _db_ids(scope, table, f)
            context = f"随机第 {index} 组: table={table.value}, scope={scope!r}, filter={f!r}"
            assert evaluated == queried, f"数据库不一致：{context}; {evaluated} != {queried}"
            if table is MemTable.PARAGRAPH:
                qdrant_ids = await _qdrant_ids(client, collection, condition)
                assert evaluated == qdrant_ids, f"Qdrant 不一致：{context}; {evaluated} != {qdrant_ids}"


class TestAdminMemoryScopeCondition:
    async def test_condition_order_and_combination(self, db: Any) -> None:
        specs = [
            ParagraphSpec(1, 0, "onebot_v11", "i1", 7, "gA", ("u151",)),
            ParagraphSpec(2, 1, "onebot_v11", "i1", 7, "gA", ("u151",)),
            ParagraphSpec(3, 2, "minecraft", "i2", 9, "gB", ("uxw",)),
            ParagraphSpec(4, 1, "onebot_v11", "i2", 9, "gB", ("uxw",)),
        ]
        for spec in specs:
            await _create_paragraph(spec)

        scope = AdminMemoryScope.for_pairs([("onebot_v11", "i1"), ("minecraft", "i2")])
        assert await _db_ids(scope, MemTable.PARAGRAPH, AdminFilter(partition="domain")) == {1}
        assert await _db_ids(scope, MemTable.PARAGRAPH, AdminFilter(partition="workspace")) == {2, 3}
        assert await _db_ids(scope, MemTable.PARAGRAPH, AdminFilter(chat_key="gA")) == {1, 2}
        assert await _db_ids(scope, MemTable.PARAGRAPH, AdminFilter(preset_id=9)) == {3}
        assert await _db_ids(scope, MemTable.PARAGRAPH, AdminFilter(person_accounts=frozenset({"uxw"}))) == {3}
        assert (
            await _db_ids(
                scope,
                MemTable.PARAGRAPH,
                AdminFilter(partition="workspace", chat_key="gB", preset_id=9, person_accounts=frozenset({"uxw"})),
            )
            == {3}
        )

    async def test_empty_pairs_are_always_empty(self, db: Any) -> None:
        await _create_paragraph(ParagraphSpec(1, 1, "onebot_v11", "i1", 7, "gA"))
        scope = AdminMemoryScope.for_pairs([])
        assert scope.pairs == ()
        assert await _db_ids(scope, MemTable.PARAGRAPH, AdminFilter()) == set()
        assert scope.contains_pair("onebot_v11", "i1") is False
        assert scope == AdminMemoryScope.for_pairs([])

    async def test_platform_all_has_no_condition(self, db: Any) -> None:
        await _create_paragraph(ParagraphSpec(1, 1, "onebot_v11", "i1", 7, "gA"))
        await _create_paragraph(ParagraphSpec(2, 0, "", "", 0, None))
        scope = AdminMemoryScope.platform_all()
        assert scope.contains_pair("anything", "") is True
        assert await _db_ids(scope, MemTable.PARAGRAPH, AdminFilter()) == {1, 2}

    async def test_summary_table_rejects_partition_and_person(self) -> None:
        scope = AdminMemoryScope.platform_all()
        with pytest.raises(ValueError):
            scope.condition(MemTable.SUMMARY, AdminFilter(partition="domain"))
        with pytest.raises(ValueError):
            scope.condition(MemTable.SUMMARY, AdminFilter(partition="workspace"))
        with pytest.raises(ValueError):
            scope.condition(MemTable.SUMMARY, AdminFilter(person_accounts=frozenset({"u151"})))
        with pytest.raises(ValueError):
            scope.condition(MemTable.SUMMARY_STATE, AdminFilter(partition="domain"))

    def test_unknown_partition_raises(self) -> None:
        scope = AdminMemoryScope.platform_all()
        with pytest.raises(ValueError):
            scope.condition(MemTable.PARAGRAPH, AdminFilter(partition="bogus"))


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
class _IdentityWorld:
    group: DBInstanceGroup
    own_instances: list[DBAdapterInstance] = field(default_factory=list)
    other_instance: DBAdapterInstance | None = None


async def _seed_identity_world() -> _IdentityWorld:
    group = await DBInstanceGroup.create(name="同租户的域", tenant_id=1)
    own = [
        await DBAdapterInstance.create(adapter_key="onebot_v11", instance_key="own1", tenant_id=1),
        await DBAdapterInstance.create(adapter_key="onebot_v11", instance_key="own2", tenant_id=1),
    ]
    other = await DBAdapterInstance.create(adapter_key="onebot_v11", instance_key="other1", tenant_id=2)
    for instance in [*own, other]:
        await DBInstanceDomainBinding.create(
            adapter_key=instance.adapter_key,
            instance_key=instance.instance_key,
            memory_group_id=group.id,
        )
    return _IdentityWorld(group=group, own_instances=own, other_instance=other)


class TestResolveAdminScope:
    async def test_platform_all_and_instance(self, db: Any) -> None:
        world = await _seed_identity_world()
        user = _platform_user()

        assert (await resolve_admin_scope(user)).pairs is None
        selected = await resolve_admin_scope(
            user,
            instance_key=world.own_instances[0].instance_key,
            adapter_key=world.own_instances[0].adapter_key,
        )
        assert selected.pairs == (("onebot_v11", "own1"),)

        by_domain = await resolve_admin_scope(
            user,
            instance_key=world.own_instances[0].instance_key,
            adapter_key=world.own_instances[0].adapter_key,
            domain="group",
        )
        assert by_domain.pairs == (
            ("onebot_v11", "other1"),
            ("onebot_v11", "own1"),
            ("onebot_v11", "own2"),
        )

        by_group = await resolve_admin_scope(user, group_id=world.group.id)
        assert by_group.pairs == by_domain.pairs

    async def test_tenant_requires_an_instance(self, db: Any) -> None:
        await _seed_identity_world()
        with pytest.raises(ValidationError):
            await resolve_admin_scope(_tenant_user())

    async def test_tenant_cannot_see_another_tenants_instance(self, db: Any) -> None:
        world = await _seed_identity_world()
        assert world.other_instance is not None
        with pytest.raises(NotFoundError):
            await resolve_admin_scope(
                _tenant_user(),
                instance_key=world.other_instance.instance_key,
                adapter_key=world.other_instance.adapter_key,
            )

    async def test_tenant_scope_is_its_own_instance(self, db: Any) -> None:
        world = await _seed_identity_world()
        scope = await resolve_admin_scope(
            _tenant_user(),
            instance_key=world.own_instances[0].instance_key,
            adapter_key=world.own_instances[0].adapter_key,
        )
        assert scope.pairs == (("onebot_v11", "own1"),)

    async def test_tenant_domain_intersects_accessible_refs(self, db: Any) -> None:
        world = await _seed_identity_world()
        scope = await resolve_admin_scope(
            _tenant_user(),
            instance_key=world.own_instances[0].instance_key,
            adapter_key=world.own_instances[0].adapter_key,
            domain="group",
        )
        # 组里混进了另一个租户的实例（脏数据），交集必须把它剔掉。
        assert scope.pairs == (("onebot_v11", "own1"), ("onebot_v11", "own2"))

    async def test_tenant_rejects_group_id(self, db: Any) -> None:
        world = await _seed_identity_world()
        with pytest.raises(ValidationError):
            await resolve_admin_scope(
                _tenant_user(),
                instance_key=world.own_instances[0].instance_key,
                adapter_key=world.own_instances[0].adapter_key,
                group_id=world.group.id,
            )


class TestAdminRowVisible:
    async def test_platform_always_visible(self, db: Any) -> None:
        assert await admin_row_visible(_platform_user(), "", "") is True
        assert await admin_row_visible(_platform_user(), "onebot_v11", "whatever") is True

    async def test_tenant_own_row_visible(self, db: Any) -> None:
        world = await _seed_identity_world()
        assert await admin_row_visible(_tenant_user(), "onebot_v11", world.own_instances[0].instance_key) is True

    async def test_tenant_other_row_invisible(self, db: Any) -> None:
        world = await _seed_identity_world()
        assert world.other_instance is not None
        assert await admin_row_visible(_tenant_user(), "onebot_v11", world.other_instance.instance_key) is False

    async def test_tenant_legacy_empty_adapter_invisible(self, db: Any) -> None:
        await _seed_identity_world()
        assert await admin_row_visible(_tenant_user(), "", "own1") is False
        assert await admin_row_visible(_tenant_user(), "onebot_v11", "") is False
