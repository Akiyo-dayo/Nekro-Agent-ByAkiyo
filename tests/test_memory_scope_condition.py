"""记忆可见范围的条件树与新的 `MemoryScope` 形状。

## 这份测试要钉死什么

1. **一个判据、三个渲染器。** 同一批行分别用纯 Python（`evaluate`）、SQLite / 真 PG
   （`scope.apply`）与本地 Qdrant（`scope.qdrant_must`）过滤，得到的 ID 集合必须相同。
   只测其中一条腿会漏掉「某一侧写法不同」的缺陷，而这类缺陷在授权边界上就是泄露。
2. **分区矩阵。** 自成一域、实例组、两个单实例适配器、不同租户、人设维、工作区的本地集、
   私聊与 `__user__` / NULL 行各自可见或不可见。
3. **闸门。** 关着时返回改造前的两维形状（新字段为 None）；打开时返回新形状，并且
   频道缺少 `chat_key` 时拒绝解析，不退化成不过滤。
4. **Qdrant 的语义钉子。** `IsNull` 只匹配显式 null、不匹配缺键；`PresetVisible` 必须
   放行缺 `preset_id` 键的存量点。
5. **随机对拍。** 固定种子生成 300 组（行 + 范围 + 表），三条腿逐组相同；不同就打印
   那一组，便于复现。
"""

from __future__ import annotations

import inspect
import os
import random
import uuid
from dataclasses import dataclass, fields
from typing import Any, Iterable, Sequence
from urllib.parse import urlencode

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_instance_group import DBInstanceDomainBinding, DBInstanceGroup
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import (
    QDRANT_PAYLOAD_SCHEMA_VERSION,
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.identity_scope import resolve_domain_members
from nekro_agent.services.memory.instance_scope import (
    CONSOLE_USER_CHAT_KEY,
    ChannelVisibility,
    MemoryScope,
    resolve_memory_scope,
)
from nekro_agent.services.memory.scope_condition import (
    FALSE,
    TABLE_FIELDS,
    TRUE,
    And,
    In,
    MemTable,
    Not,
    Or,
    PersonsAny,
    PersonsOutside,
    ScopeField,
    and_,
    eq,
    evaluate,
    in_,
    is_null,
    member_pairs,
    not_,
    or_,
    persons_all_in,
    persons_any,
    preset_visible,
    to_qdrant_filter,
    to_tortoise_q,
)
from nekro_agent.services.memory.semantic_writer import persist_cc_task_memory

PG_DSN_ENV = "NA_TEST_PG_DSN"

_MODEL_MODULES = [
    "nekro_agent.models.db_mem_paragraph",
    "nekro_agent.models.db_mem_paragraph_person",
    "nekro_agent.models.db_mem_entity",
    "nekro_agent.models.db_mem_relation",
    "nekro_agent.models.db_mem_episode",
    "nekro_agent.models.db_chat_channel",
    "nekro_agent.models.db_chat_message",
    "nekro_agent.models.db_adapter_instance",
    "nekro_agent.models.db_instance_group",
]

_MODELS: dict[MemTable, type[Any]] = {
    MemTable.PARAGRAPH: DBMemParagraph,
    MemTable.ENTITY: DBMemEntity,
    MemTable.RELATION: DBMemRelation,
    MemTable.EPISODE: DBMemEpisode,
}

# 本文件只给这四张行表布置夹具。`MemTable` 后来加入了摘要类表（它们没有写入侧夹具、
# 也没有工作区分区），遍历时按有模型的表来，避免 KeyError。
_SCOPE_TABLES: tuple[MemTable, ...] = tuple(_MODELS)

_VECTOR = [0.1, 0.2, 0.3, 0.4]

# 测试里的账号与 ID 都用短字符串，不用会撞公开树扫描的 9–11 位数字。
_ADAPTERS = ("onebot_v11", "minecraft", "sse")


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

    schema = f"na_test_scope_{uuid.uuid4().hex[:12]}"
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
        # 证明连的是真 PG，而不是某条静默回退的 SQLite 路径。
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


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认打开域分区闸；闸关的用例自己再关一次。"""
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    monkeypatch.setattr(config, "AI_CHAT_DEFAULT_PRESET_ID", None)


@pytest.fixture
async def arena(db: Any) -> Any:
    client = AsyncQdrantClient(location=":memory:")
    collection = f"scope_condition_{uuid.uuid4().hex}"
    await client.create_collection(
        collection_name=collection,
        vectors_config=qdrant_models.VectorParams(size=len(_VECTOR), distance=qdrant_models.Distance.COSINE),
    )
    try:
        yield MemoryArena(client=client, collection=collection)
    finally:
        await client.close()


@dataclass(frozen=True)
class RowSpec:
    """一行记忆在测试里的完整取值。"""

    id: int
    workspace_id: int
    adapter_key: str
    instance_key: str
    preset_id: int
    origin_chat_key: str | None
    origin_chat_type: str
    person_accounts: tuple[str, ...] = ()

    def as_row(self, table: MemTable) -> dict[str, Any]:
        row = {field.name: getattr(self, field.name) for field in fields(self)}
        if ScopeField.ORIGIN_CHAT_TYPE not in TABLE_FIELDS[table]:
            row.pop(ScopeField.ORIGIN_CHAT_TYPE.value, None)
        return row


class MemoryArena:
    """一套真表 + 一个本地 Qdrant collection，三条腿都从同一批行出发。"""

    def __init__(self, *, client: AsyncQdrantClient, collection: str) -> None:
        self.client = client
        self.collection = collection
        self.rows: dict[MemTable, list[RowSpec]] = {table: [] for table in MemTable}

    async def add(self, table: MemTable, specs: Sequence[RowSpec]) -> None:
        model = _MODELS[table]
        for spec in specs:
            kwargs: dict[str, Any] = {
                "id": spec.id,
                "workspace_id": spec.workspace_id,
                "adapter_key": spec.adapter_key,
                "instance_key": spec.instance_key,
                "preset_id": spec.preset_id,
                "origin_chat_key": spec.origin_chat_key,
            }
            if table is MemTable.PARAGRAPH:
                kwargs.update(
                    origin_chat_type=spec.origin_chat_type,
                    memory_source="na",
                    cognitive_type=CognitiveType.SEMANTIC,
                    knowledge_type=KnowledgeType.FACT,
                    content=f"段落内容 {spec.id}",
                    summary=f"摘要 {spec.id}",
                    origin_kind=OriginKind.CONSOLIDATION,
                )
            elif table is MemTable.ENTITY:
                kwargs.update(
                    entity_type=EntityType.PERSON,
                    name=f"实体 {spec.id}",
                    canonical_name=f"实体{spec.id}",
                )
            elif table is MemTable.RELATION:
                kwargs.update(
                    subject_entity_id=1,
                    predicate="related_to",
                    object_entity_id=2,
                    memory_source="na",
                    cognitive_type=CognitiveType.SEMANTIC.value,
                )
            else:
                kwargs.update(
                    origin_chat_type=spec.origin_chat_type,
                    title=f"事件 {spec.id}",
                    narrative_summary=f"事件叙述 {spec.id}",
                )
            created = await model.create(**kwargs)
            self.rows[table].append(spec)
            if table is MemTable.PARAGRAPH:
                for account in spec.person_accounts:
                    await DBMemParagraphPerson.create(paragraph_id=spec.id, account=account)
                await self.client.upsert(
                    collection_name=self.collection,
                    points=[
                        qdrant_models.PointStruct(
                            id=spec.id,
                            vector=list(_VECTOR),
                            payload=created.to_qdrant_payload(person_accounts=list(spec.person_accounts)),
                        ),
                    ],
                )

    async def assert_three_way(
        self,
        scope: MemoryScope,
        table: MemTable,
        *,
        workspace_id: int,
        expected: set[int] | None = None,
        label: str = "",
    ) -> None:
        rows = self.rows[table]
        condition = scope.condition(table, workspace_id=workspace_id)
        evaluated = {row.id for row in rows if evaluate(condition, row.as_row(table))}

        queryset = scope.apply(_MODELS[table].filter(), table, workspace_id=workspace_id)
        queried = {int(row.id) async for row in queryset}

        context = f"{label} table={table.value} workspace={workspace_id} scope={scope!r}"
        assert evaluated == queried, f"evaluate 与数据库不一致：{context}; {evaluated} != {queried}"

        if table is MemTable.PARAGRAPH:
            must = scope.qdrant_must(workspace_id=workspace_id)
            if must is None:
                qdrant_ids: set[int] = set()
            else:
                points = await self.client.search(
                    collection_name=self.collection,
                    query_vector=list(_VECTOR),
                    query_filter=qdrant_models.Filter(must=must),
                    limit=10000,
                    with_payload=False,
                )
                qdrant_ids = {int(point.id) for point in points}
            assert evaluated == qdrant_ids, (
                f"evaluate 与 Qdrant 不一致：{context}; {evaluated} != {qdrant_ids}"
            )

        if expected is not None:
            assert evaluated == expected, f"可见集合与预期不一致：{context}; {evaluated} != {expected}"


def _new_scope(
    *,
    preset_id: int,
    domain_members: Iterable[tuple[str, str]],
    local_keys: Iterable[str],
    allowed_instance_keys: list[str] | None = None,
    chat_key: str = "chat-main",
    chat_kind: str = "group",
    group_to_group: str = "off",
    group_to_private: str = "off",
    private_to_group: str = "off",
    member_accounts: Iterable[str] = (),
    counterpart_accounts: Iterable[str] = (),
) -> MemoryScope:
    return MemoryScope(
        allowed_instance_keys=allowed_instance_keys,
        preset_id=preset_id,
        domain_members=tuple(domain_members),
        visibility=ChannelVisibility(
            chat_key=chat_key,
            chat_kind=chat_kind,
            local_keys=frozenset(local_keys),
            group_to_group=group_to_group,
            group_to_private=group_to_private,
            private_to_group=private_to_group,
            member_accounts=frozenset(member_accounts),
            counterpart_accounts=frozenset(counterpart_accounts),
        ),
    )


def _spec(
    row_id: int,
    *,
    workspace_id: int,
    adapter_key: str = "onebot_v11",
    instance_key: str = "ob1",
    preset_id: int = 7,
    origin_chat_key: str | None = "gA",
    origin_chat_type: str = "group",
) -> RowSpec:
    return RowSpec(
        id=row_id,
        workspace_id=workspace_id,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        origin_chat_key=origin_chat_key,
        origin_chat_type=origin_chat_type,
    )


class _StubChannel:
    def __init__(
        self,
        instance_key: str,
        *,
        preset_id: int = 7,
        adapter_key: str = "onebot_v11",
        chat_key: str | None = "chat-main",
        channel_type: str | None = "group",
        workspace_id: int | None = None,
    ) -> None:
        self.adapter_key = adapter_key
        self.instance_key = instance_key
        self._preset_id = preset_id
        self.chat_key = chat_key
        self.channel_type = channel_type
        self.workspace_id = workspace_id

    async def get_effective_preset_id(self) -> int:
        return self._preset_id


async def _create_channel(
    *,
    chat_key: str,
    adapter_key: str = "onebot_v11",
    instance_key: str = "ob1",
    channel_type: str = "group",
    workspace_id: int | None = 1,
) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        channel_id=chat_key,
        channel_name=chat_key,
        channel_type=channel_type,
        chat_key=chat_key,
        workspace_id=workspace_id,
        data="{}",
    )


# --------------------------------------------------------------------------------------
# 1. 化简规则、渲染器与参照求值
# --------------------------------------------------------------------------------------


class TestConstructorsAndRendering:
    def test_simplification_rules(self) -> None:
        field = ScopeField.ORIGIN_CHAT_KEY
        assert in_(field, []) is FALSE
        assert member_pairs([]) is FALSE
        assert in_(field, ["a", "a", "b"]) == In(field=field, values=("a", "b"))
        assert and_() is TRUE
        assert and_(TRUE, eq(field, "a")) == eq(field, "a")
        assert and_(FALSE, eq(field, "a")) is FALSE
        assert and_(eq(field, "a")) == eq(field, "a")
        assert and_(eq(field, "a"), eq(field, "b")) == And(items=(eq(field, "a"), eq(field, "b")))
        assert or_() is FALSE
        assert or_(FALSE, eq(field, "a")) == eq(field, "a")
        assert or_(TRUE, eq(field, "a")) is TRUE
        assert or_(eq(field, "a")) == eq(field, "a")
        assert or_(eq(field, "a"), eq(field, "b")) == Or(items=(eq(field, "a"), eq(field, "b")))
        assert not_(TRUE) is FALSE
        assert not_(FALSE) is TRUE
        # 取反收紧到只接受 PersonsOutside：其余节点在 SQL 三值逻辑下与 Python / Qdrant 不等价。
        with pytest.raises(ValueError):
            not_(eq(field, "a"))
        assert persons_any([]) is FALSE
        assert persons_all_in([]) is FALSE
        assert persons_any(["b", "a", "b"]) == PersonsAny(accounts=("a", "b"))
        assert persons_all_in(["b", "a"]) == And(
            items=(PersonsAny(accounts=("a", "b")), Not(item=PersonsOutside(accounts=("a", "b")))),
        )

    def test_renderers_reject_false_and_missing_columns(self) -> None:
        with pytest.raises(ValueError):
            to_tortoise_q(FALSE, MemTable.PARAGRAPH)
        with pytest.raises(ValueError):
            to_qdrant_filter(FALSE)
        with pytest.raises(ValueError):
            to_tortoise_q(eq(ScopeField.ORIGIN_CHAT_TYPE, "group"), MemTable.ENTITY)
        with pytest.raises(ValueError):
            to_tortoise_q(eq(ScopeField.ORIGIN_CHAT_TYPE, "group"), MemTable.RELATION)

    def test_evaluate_each_node(self) -> None:
        row = {
            "workspace_id": 0,
            "adapter_key": "onebot_v11",
            "instance_key": "ob1",
            "preset_id": 7,
            "origin_chat_key": None,
        }
        assert evaluate(TRUE, row) is True
        assert evaluate(FALSE, row) is False
        assert evaluate(eq(ScopeField.ADAPTER_KEY, "onebot_v11"), row) is True
        assert evaluate(eq(ScopeField.INSTANCE_KEY, "ob2"), row) is False
        assert evaluate(in_(ScopeField.INSTANCE_KEY, ["ob1", "ob2"]), row) is True
        assert evaluate(is_null(ScopeField.ORIGIN_CHAT_KEY), row) is True
        assert evaluate(preset_visible(7), row) is True
        assert evaluate(preset_visible(9), row) is False
        assert evaluate(member_pairs([("onebot_v11", "ob1")]), row) is True
        assert evaluate(and_(eq(ScopeField.WORKSPACE_ID, 0), preset_visible(7)), row) is True
        assert evaluate(or_(eq(ScopeField.WORKSPACE_ID, 1), preset_visible(7)), row) is True
        # Not 只包 PersonsOutside；空的人集合让它为假，取反后为真。
        assert evaluate(not_(PersonsOutside(("u151",))), row) is True
        assert evaluate(persons_any(["u151"]), {**row, "person_accounts": ["u151"]}) is True
        assert evaluate(persons_all_in(["u151"]), {**row, "person_accounts": ["u151", "uxw"]}) is False

    def test_qdrant_preset_condition_has_the_three_legacy_should_entries(self) -> None:
        for preset_id, expected_values in ((0, [0]), (7, [7, 0])):
            condition = to_qdrant_filter(preset_visible(preset_id))
            values = [
                item.match.value
                for item in condition.should or []
                if isinstance(item, qdrant_models.FieldCondition) and item.match is not None
            ]
            assert values == expected_values
            assert any(isinstance(item, qdrant_models.IsEmptyCondition) for item in condition.should or [])


# --------------------------------------------------------------------------------------
# 2. 固定数据集上的三方等价
# --------------------------------------------------------------------------------------


def _fixed_specs() -> list[RowSpec]:
    pairs = [
        ("onebot_v11", "ob1"),
        ("onebot_v11", "ob2"),
        ("onebot_v11", "ob3"),
        ("onebot_v11", ""),
        ("minecraft", ""),
        ("sse", ""),
    ]
    chats = ["gA", "gB", "pX", "__user__", None, "gU", "gT"]
    types = ["group", "private", ""]
    specs: list[RowSpec] = []
    row_id = 1
    for workspace_id in (0, 1, 2):
        for adapter_key, instance_key in pairs:
            for preset_id in (0, 7, 9):
                chat = chats[(row_id - 1) % len(chats)]
                origin_chat_type = types[(row_id - 1) % len(types)]
                specs.append(
                    RowSpec(
                        id=row_id,
                        workspace_id=workspace_id,
                        adapter_key=adapter_key,
                        instance_key=instance_key,
                        preset_id=preset_id,
                        origin_chat_key=chat,
                        origin_chat_type=origin_chat_type,
                    ),
                )
                row_id += 1
    return specs


_FIXED_SCOPES: list[MemoryScope] = [
    _new_scope(preset_id=7, domain_members=[("onebot_v11", "ob1")], local_keys=["gA"]),
    _new_scope(
        preset_id=7,
        domain_members=[("onebot_v11", "ob1"), ("onebot_v11", "ob2")],
        local_keys=["gA", "gB"],
    ),
    _new_scope(preset_id=9, domain_members=[("onebot_v11", "ob3")], local_keys=["gT"]),
    _new_scope(preset_id=0, domain_members=[("onebot_v11", "")], local_keys=["gA", "__user__"]),
    _new_scope(preset_id=7, domain_members=[("minecraft", "")], local_keys=["pX"]),
    _new_scope(preset_id=7, domain_members=[("sse", "")], local_keys=["gU", "gT"]),
    _new_scope(
        preset_id=7,
        domain_members=[("onebot_v11", "ob1")],
        local_keys=["gA", "gB", "gU", "__user__"],
        allowed_instance_keys=["ob1", ""],
    ),
]


class TestThreeWayEquivalence:
    async def test_fixed_dataset_for_every_table(self, arena: MemoryArena) -> None:
        specs = _fixed_specs()
        for table in _SCOPE_TABLES:
            await arena.add(table, specs)

        for scope in _FIXED_SCOPES:
            for table in _SCOPE_TABLES:
                for workspace_id in (0, 1, 2):
                    await arena.assert_three_way(
                        scope,
                        table,
                        workspace_id=workspace_id,
                        label="固定数据集",
                    )

    async def test_false_condition_returns_empty_without_querying(self, arena: MemoryArena) -> None:
        await arena.add(MemTable.PARAGRAPH, [_spec(1, workspace_id=0)])
        # 本地集为空 => 域腿的 `in_(ORIGIN_CHAT_KEY, ())` 化简成 FALSE。工作区腿在没有
        # 绑工作区（workspace_id=0）时不存在，整棵树因此是 FALSE。
        scope = _new_scope(
            preset_id=7,
            domain_members=[("onebot_v11", "ob1")],
            local_keys=[],
        )
        assert scope.condition(MemTable.PARAGRAPH, workspace_id=0) is FALSE
        queryset = scope.apply(DBMemParagraph.filter(), MemTable.PARAGRAPH, workspace_id=0)
        assert await queryset.count() == 0
        assert scope.qdrant_must(workspace_id=0) is None

    async def test_random_300_cases(self, arena: MemoryArena) -> None:
        rng = random.Random(20261106)
        for table in _SCOPE_TABLES:
            await arena.add(table, _random_pool(rng, 40))

        for index in range(300):
            table = rng.choice(list(_SCOPE_TABLES))
            scope = _random_scope(rng)
            workspace_id = rng.choice([0, 1, 2])
            await arena.assert_three_way(
                scope,
                table,
                workspace_id=workspace_id,
                label=f"随机第 {index} 组",
            )

    async def test_random_300_cases_with_persons(self, arena: MemoryArena) -> None:
        """另起 300 组：行带随机的人，范围带随机的开关 / 成员集 / 对方账号集。"""
        rng = random.Random(20261107)
        for table in _SCOPE_TABLES:
            await arena.add(table, _random_pool(rng, 40))

        for index in range(300):
            table = rng.choice(list(_SCOPE_TABLES))
            scope = _random_scope(rng)
            workspace_id = rng.choice([0, 1, 2])
            await arena.assert_three_way(
                scope,
                table,
                workspace_id=workspace_id,
                label=f"人这一维随机第 {index} 组",
            )


def _random_pool(rng: random.Random, count: int) -> list[RowSpec]:
    specs: list[RowSpec] = []
    for row_id in range(1, count + 1):
        adapter_key = rng.choice(_ADAPTERS)
        instance_key = rng.choice(["ob1", "ob2", "ob3", ""]) if adapter_key == "onebot_v11" else ""
        specs.append(
            RowSpec(
                id=row_id,
                workspace_id=rng.choice([0, 0, 1, 2]),
                adapter_key=adapter_key,
                instance_key=instance_key,
                preset_id=rng.choice([0, 7, 9]),
                origin_chat_key=rng.choice(
                    ["gA", "gB", "gU", "pX", "gT", "__user__", None],
                ),
                origin_chat_type=rng.choice(["group", "private", ""]),
                person_accounts=tuple(rng.sample(["u151", "u151b", "uxw", "uxh"], rng.randint(0, 3))),
            ),
        )
    return specs


def _random_scope(rng: random.Random) -> MemoryScope:
    universe = [
        ("onebot_v11", "ob1"),
        ("onebot_v11", "ob2"),
        ("onebot_v11", "ob3"),
        ("onebot_v11", ""),
        ("minecraft", ""),
        ("sse", ""),
    ]
    member_count = rng.randint(1, 4)
    domain_members = rng.sample(universe, member_count)
    local_keys = rng.sample(
        ["gA", "gB", "gU", "pX", "gT", "__user__"],
        rng.randint(1, 4),
    )
    allowed_instance_keys: list[str] | None
    if rng.random() < 0.5:
        allowed_instance_keys = None
    else:
        allowed_instance_keys = rng.sample(["ob1", "ob2", "ob3", ""], rng.randint(1, 4))
    account_universe = ["u151", "u151b", "uxw", "uxh"]
    return _new_scope(
        preset_id=rng.choice([0, 7, 9]),
        domain_members=domain_members,
        local_keys=local_keys,
        allowed_instance_keys=allowed_instance_keys,
        chat_key=local_keys[0],
        chat_kind=rng.choice(["group", "private", "other"]),
        group_to_group=rng.choice(["off", "related", "all"]),
        group_to_private=rng.choice(["off", "related", "all"]),
        private_to_group=rng.choice(["off", "related", "all"]),
        member_accounts=rng.sample(account_universe, rng.randint(0, 3)),
        counterpart_accounts=rng.sample(account_universe, rng.randint(0, 3)),
    )


# --------------------------------------------------------------------------------------
# 3. 隔离矩阵（分区部分）
# --------------------------------------------------------------------------------------


class TestPartitionIsolation:
    async def test_self_domains_do_not_see_each_other(self, arena: MemoryArena) -> None:
        ob1 = _spec(1, workspace_id=0, instance_key="ob1", origin_chat_key="g1")
        ob2 = _spec(2, workspace_id=0, instance_key="ob2", origin_chat_key="g2")
        await arena.add(MemTable.PARAGRAPH, [ob1, ob2])
        scope_ob1 = _new_scope(preset_id=7, domain_members=[("onebot_v11", "ob1")], local_keys=["g1"])
        scope_ob2 = _new_scope(preset_id=7, domain_members=[("onebot_v11", "ob2")], local_keys=["g2"])
        await arena.assert_three_way(scope_ob1, MemTable.PARAGRAPH, workspace_id=0, expected={1}, label="自成一域 ob1")
        await arena.assert_three_way(scope_ob2, MemTable.PARAGRAPH, workspace_id=0, expected={2}, label="自成一域 ob2")

    async def test_single_instance_adapters_are_isolated(self, arena: MemoryArena) -> None:
        minecraft = _spec(
            1,
            workspace_id=0,
            adapter_key="minecraft",
            instance_key="",
            origin_chat_key="m1",
        )
        sse = _spec(2, workspace_id=0, adapter_key="sse", instance_key="", origin_chat_key="s1")
        await arena.add(MemTable.PARAGRAPH, [minecraft, sse])
        scope_m = _new_scope(preset_id=7, domain_members=[("minecraft", "")], local_keys=["m1"])
        scope_s = _new_scope(preset_id=7, domain_members=[("sse", "")], local_keys=["s1"])
        await arena.assert_three_way(scope_m, MemTable.PARAGRAPH, workspace_id=0, expected={1}, label="minecraft 单实例")
        await arena.assert_three_way(scope_s, MemTable.PARAGRAPH, workspace_id=0, expected={2}, label="sse 单实例")

    async def test_preset_dimension_keeps_zero_visible(self, arena: MemoryArena) -> None:
        rows = [
            _spec(1, workspace_id=0, preset_id=0, origin_chat_key="gA"),
            _spec(2, workspace_id=0, preset_id=7, origin_chat_key="gA"),
            _spec(3, workspace_id=0, preset_id=9, origin_chat_key="gA"),
        ]
        await arena.add(MemTable.PARAGRAPH, rows)
        scope = _new_scope(preset_id=7, domain_members=[("onebot_v11", "ob1")], local_keys=["gA"])
        await arena.assert_three_way(
            scope,
            MemTable.PARAGRAPH,
            workspace_id=0,
            expected={1, 2},
            label="人设 7 看 0 与 7",
        )

    async def test_legacy_empty_instance_row_is_not_hit_by_the_domain_leg(self, arena: MemoryArena) -> None:
        legacy = _spec(
            1,
            workspace_id=1,
            instance_key="",
            origin_chat_key="gLegacy",
            origin_chat_type="group",
        )
        await arena.add(MemTable.PARAGRAPH, [legacy])
        # 没绑工作区 => workspace_id=0 => 只有域腿；域成员是 ob1，不含空串实例。
        scope = _new_scope(preset_id=7, domain_members=[("onebot_v11", "ob1")], local_keys=["gA"])
        await arena.assert_three_way(
            scope,
            MemTable.PARAGRAPH,
            workspace_id=0,
            expected=set(),
            label="存量空实例行不经域腿命中",
        )

    async def test_group_members_see_each_other_and_removal_is_immediate(self, arena: MemoryArena) -> None:
        gA = await _create_channel(chat_key="gA", instance_key="ob1", workspace_id=1)
        gB = await _create_channel(chat_key="gB", instance_key="ob2", workspace_id=1)
        group = await DBInstanceGroup.create(name="group-1", tenant_id=None)
        await DBInstanceDomainBinding.create(
            adapter_key="onebot_v11",
            instance_key="ob1",
            memory_group_id=group.id,
        )
        await DBInstanceDomainBinding.create(
            adapter_key="onebot_v11",
            instance_key="ob2",
            memory_group_id=group.id,
        )

        from_a = _spec(1, workspace_id=0, instance_key="ob1", origin_chat_key="gA")
        from_b = _spec(2, workspace_id=0, instance_key="ob2", origin_chat_key="gB")
        await arena.add(MemTable.PARAGRAPH, [from_a, from_b])

        scope = await resolve_memory_scope(gA, preset_id=7)
        assert scope.visibility is not None
        assert scope.visibility.local_keys == frozenset({"gA", "gB"})
        await arena.assert_three_way(scope, MemTable.PARAGRAPH, workspace_id=1, expected={1, 2}, label="组内互见")

        await DBInstanceDomainBinding.filter(adapter_key="onebot_v11", instance_key="ob2").delete()
        scope_after = await resolve_memory_scope(gB, preset_id=7)
        assert scope_after.visibility is not None
        assert scope_after.visibility.local_keys == frozenset({"gB"})
        await arena.assert_three_way(
            scope_after,
            MemTable.PARAGRAPH,
            workspace_id=1,
            expected={2},
            label="出组后下一次解析就不可见",
        )

    async def test_different_groups_are_isolated(self, arena: MemoryArena) -> None:
        group_one = await DBInstanceGroup.create(name="tenant-one", tenant_id=None)
        group_two = await DBInstanceGroup.create(name="tenant-two", tenant_id=None)
        await DBInstanceDomainBinding.create(
            adapter_key="onebot_v11",
            instance_key="ob1",
            memory_group_id=group_one.id,
        )
        await DBInstanceDomainBinding.create(
            adapter_key="onebot_v11",
            instance_key="ob3",
            memory_group_id=group_two.id,
        )
        gA = await _create_channel(chat_key="gA", instance_key="ob1", workspace_id=1)
        gT = await _create_channel(chat_key="gT", instance_key="ob3", workspace_id=2)
        await arena.add(
            MemTable.PARAGRAPH,
            [
                _spec(1, workspace_id=0, instance_key="ob1", origin_chat_key="gA"),
                _spec(2, workspace_id=0, instance_key="ob3", origin_chat_key="gT"),
            ],
        )
        scope_a = await resolve_memory_scope(gA, preset_id=7)
        scope_t = await resolve_memory_scope(gT, preset_id=7)
        await arena.assert_three_way(scope_a, MemTable.PARAGRAPH, workspace_id=1, expected={1}, label="租户一")
        await arena.assert_three_way(scope_t, MemTable.PARAGRAPH, workspace_id=2, expected={2}, label="租户二")

    async def test_bound_group_sees_local_set_console_and_null_but_not_private(self, arena: MemoryArena) -> None:
        gA = await _create_channel(chat_key="gA", instance_key="ob1", workspace_id=1)
        await _create_channel(chat_key="gB", instance_key="ob1", workspace_id=1)
        await _create_channel(chat_key="gC", instance_key="ob2", workspace_id=1)
        await _create_channel(chat_key="pX", instance_key="ob1", channel_type="private", workspace_id=1)
        await _create_channel(chat_key="gU", instance_key="ob1", workspace_id=None)
        await _create_channel(chat_key="gT", instance_key="ob1", workspace_id=2)

        await arena.add(
            MemTable.PARAGRAPH,
            [
                _spec(1, workspace_id=1, origin_chat_key="gA"),
                _spec(2, workspace_id=1, origin_chat_key="gB"),
                _spec(3, workspace_id=1, origin_chat_key="gC"),
                _spec(4, workspace_id=1, origin_chat_key="pX", origin_chat_type="private"),
                _spec(5, workspace_id=1, origin_chat_key=CONSOLE_USER_CHAT_KEY),
                _spec(6, workspace_id=1, origin_chat_key=None),
                _spec(7, workspace_id=0, origin_chat_key="gA"),
                _spec(8, workspace_id=0, origin_chat_key="gU"),
            ],
        )

        scope = await resolve_memory_scope(gA, preset_id=7)
        assert scope.visibility is not None
        assert scope.visibility.local_keys == frozenset({"gA", "gB"})
        await arena.assert_three_way(
            scope,
            MemTable.PARAGRAPH,
            workspace_id=1,
            expected={1, 2, 5, 6, 7},
            label="绑工作区的群本地集",
        )

    async def test_private_channel_only_sees_itself_console_and_null(self, arena: MemoryArena) -> None:
        await _create_channel(chat_key="gA", instance_key="ob1", workspace_id=1)
        pX = await _create_channel(chat_key="pX", instance_key="ob1", channel_type="private", workspace_id=1)

        await arena.add(
            MemTable.PARAGRAPH,
            [
                _spec(1, workspace_id=1, origin_chat_key="gA"),
                _spec(2, workspace_id=1, origin_chat_key="pX", origin_chat_type="private"),
                _spec(3, workspace_id=1, origin_chat_key=CONSOLE_USER_CHAT_KEY),
                _spec(4, workspace_id=1, origin_chat_key=None),
                _spec(5, workspace_id=0, origin_chat_key="pX", origin_chat_type="private"),
                _spec(6, workspace_id=0, origin_chat_key="gA"),
            ],
        )

        scope = await resolve_memory_scope(pX, preset_id=7)
        assert scope.visibility is not None
        assert scope.visibility.chat_kind == "private"
        assert scope.visibility.local_keys == frozenset({"pX"})
        await arena.assert_three_way(
            scope,
            MemTable.PARAGRAPH,
            workspace_id=1,
            expected={2, 3, 4, 5},
            label="私聊只看自己、控制台与 NULL",
        )

    async def test_visibility_none_is_the_admin_view_without_a_channel_leg(self, arena: MemoryArena) -> None:
        await arena.add(
            MemTable.PARAGRAPH,
            [
                _spec(1, workspace_id=0, origin_chat_key="gA"),
                _spec(2, workspace_id=0, origin_chat_key="pX", origin_chat_type="private"),
                _spec(3, workspace_id=0, instance_key="ob2", origin_chat_key="gB"),
                _spec(4, workspace_id=0, preset_id=9, origin_chat_key="gA"),
                _spec(5, workspace_id=1, origin_chat_key="gB"),
                _spec(6, workspace_id=1, origin_chat_key=None),
            ],
        )
        scope = MemoryScope(
            allowed_instance_keys=None,
            preset_id=7,
            domain_members=(("onebot_v11", "ob1"),),
            visibility=None,
        )
        assert scope.is_local_origin("gA") is True
        assert scope.is_local_origin(None) is True
        await arena.assert_three_way(
            scope,
            MemTable.PARAGRAPH,
            workspace_id=1,
            expected={1, 2, 5, 6},
            label="管理端不按频道来源过滤",
        )

    def test_is_local_origin_uses_only_the_precomputed_local_set(self) -> None:
        scope = _new_scope(
            preset_id=7,
            domain_members=[("onebot_v11", "ob1")],
            local_keys=["gA"],
        )
        assert scope.is_local_origin("gA") is True
        assert scope.is_local_origin("gB") is False
        assert scope.is_local_origin(None) is False
        assert MemoryScope(allowed_instance_keys=None, preset_id=7).is_local_origin("anything") is True

    async def test_partition_only_matrix_uses_all_channels_in_the_local_set(self, arena: MemoryArena) -> None:
        """只测分区：本地集直接含全部频道，于是不同实例 / 人设的隔离只由域成员与人设决定。"""
        await arena.add(
            MemTable.PARAGRAPH,
            [
                _spec(1, workspace_id=0, instance_key="ob1", origin_chat_key="gA"),
                _spec(2, workspace_id=0, instance_key="ob2", origin_chat_key="gB"),
                _spec(3, workspace_id=0, instance_key="ob3", origin_chat_key="gT"),
                _spec(4, workspace_id=0, adapter_key="minecraft", instance_key="", origin_chat_key="m1"),
            ],
        )
        scope = _new_scope(
            preset_id=7,
            domain_members=[("onebot_v11", "ob1"), ("onebot_v11", "ob2")],
            local_keys=["gA", "gB", "gT", "m1"],
        )
        await arena.assert_three_way(
            scope,
            MemTable.PARAGRAPH,
            workspace_id=0,
            expected={1, 2},
            label="只测分区：同域两个实例互见，ob3 与 minecraft 不可见",
        )


# --------------------------------------------------------------------------------------
# 4. Qdrant 语义钉子
# --------------------------------------------------------------------------------------


@pytest.fixture
async def qdrant_client() -> Any:
    client = AsyncQdrantClient(location=":memory:")
    collection = f"scope_semantics_{uuid.uuid4().hex}"
    await client.create_collection(
        collection_name=collection,
        vectors_config=qdrant_models.VectorParams(size=len(_VECTOR), distance=qdrant_models.Distance.COSINE),
    )
    try:
        yield client, collection
    finally:
        await client.close()


class TestQdrantSemantics:
    async def test_is_null_matches_explicit_null_not_missing_key(self, qdrant_client: Any) -> None:
        client, collection = qdrant_client
        await client.upsert(
            collection_name=collection,
            points=[
                qdrant_models.PointStruct(id=1, vector=list(_VECTOR), payload={"origin_chat_key": None}),
                qdrant_models.PointStruct(id=2, vector=list(_VECTOR), payload={}),
                qdrant_models.PointStruct(id=3, vector=list(_VECTOR), payload={"origin_chat_key": "gA"}),
            ],
        )
        points = await client.search(
            collection_name=collection,
            query_vector=list(_VECTOR),
            query_filter=to_qdrant_filter(is_null(ScopeField.ORIGIN_CHAT_KEY)),
            limit=10000,
        )
        assert {int(point.id) for point in points} == {1}

    async def test_preset_visible_still_matches_the_legacy_missing_key(self, qdrant_client: Any) -> None:
        client, collection = qdrant_client
        await client.upsert(
            collection_name=collection,
            points=[
                qdrant_models.PointStruct(id=1, vector=list(_VECTOR), payload={"preset_id": 0}),
                qdrant_models.PointStruct(id=2, vector=list(_VECTOR), payload={"preset_id": 7}),
                qdrant_models.PointStruct(id=3, vector=list(_VECTOR), payload={"preset_id": 9}),
                qdrant_models.PointStruct(id=4, vector=list(_VECTOR), payload={}),
            ],
        )
        for preset_id, expected in ((7, {1, 2, 4}), (0, {1, 4})):
            points = await client.search(
                collection_name=collection,
                query_vector=list(_VECTOR),
                query_filter=to_qdrant_filter(preset_visible(preset_id)),
                limit=10000,
            )
            assert {int(point.id) for point in points} == expected


# --------------------------------------------------------------------------------------
# 5. 闸门、频道可见性与域成员
# --------------------------------------------------------------------------------------


class TestGateAndResolution:
    async def test_gate_off_returns_the_old_shape_verbatim(
        self,
        db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        channel = _StubChannel("ob1", preset_id=7)
        scope = await resolve_memory_scope(channel, preset_id=7)
        assert scope == MemoryScope(allowed_instance_keys=["ob1", ""], preset_id=7)
        assert scope.domain_members is None
        assert scope.visibility is None
        assert scope.is_legacy_shape is True

    async def test_gate_on_builds_the_new_shape_and_local_keys(self, db: Any) -> None:
        gA = await _create_channel(chat_key="gA", instance_key="ob1", workspace_id=1)
        await _create_channel(chat_key="gB", instance_key="ob1", workspace_id=1)
        pX = await _create_channel(chat_key="pX", instance_key="ob1", channel_type="private", workspace_id=1)
        await _create_channel(chat_key="gC", instance_key="ob2", workspace_id=1)
        await _create_channel(chat_key="gU", instance_key="ob1", workspace_id=None)
        await _create_channel(chat_key="gT", instance_key="ob1", workspace_id=2)

        group_scope = await resolve_memory_scope(gA, preset_id=7)
        assert group_scope.is_legacy_shape is False
        assert group_scope.domain_members == (("onebot_v11", "ob1"),)
        assert group_scope.visibility == ChannelVisibility(
            chat_key="gA",
            chat_kind="group",
            local_keys=frozenset({"gA", "gB"}),
        )

        private_scope = await resolve_memory_scope(pX, preset_id=7)
        assert private_scope.visibility == ChannelVisibility(
            chat_key="pX",
            chat_kind="private",
            local_keys=frozenset({"pX"}),
        )

    async def test_gate_on_rejects_a_channel_without_chat_key(self, db: Any) -> None:
        channel = _StubChannel("ob1", chat_key=None, workspace_id=1)
        with pytest.raises(ValueError):
            await resolve_memory_scope(channel, preset_id=7)

    def test_console_constant_matches_semantic_writer_default(self) -> None:
        default = inspect.signature(persist_cc_task_memory).parameters["source_chat_key"].default
        assert CONSOLE_USER_CHAT_KEY == default

    async def test_domain_members_self_group_and_legacy_configuration(
        self,
        db: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert await resolve_domain_members("onebot_v11", "ob1") == (("onebot_v11", "ob1"),)
        assert await resolve_domain_members("minecraft", "") == (("minecraft", ""),)

        group = await DBInstanceGroup.create(name="domain-members", tenant_id=None)
        await DBInstanceDomainBinding.create(
            adapter_key="onebot_v11",
            instance_key="ob1",
            memory_group_id=group.id,
        )
        await DBInstanceDomainBinding.create(
            adapter_key="onebot_v11",
            instance_key="ob2",
            memory_group_id=group.id,
        )
        assert await resolve_domain_members("onebot_v11", "ob1") == (
            ("onebot_v11", "ob1"),
            ("onebot_v11", "ob2"),
        )

        monkeypatch.setattr(
            config,
            "MEMORY_INSTANCE_SHARING_GROUPS",
            {"legacy": ["ob1", " ob2 ", "", "   "]},
        )
        members = await resolve_domain_members("onebot_v11", "ob1")
        assert members == (("onebot_v11", "ob1"), ("onebot_v11", "ob2"))
        assert ("onebot_v11", "") not in members, "旧配置里的空串不能被当成通配"
        assert members, "域成员永不为空"


# --------------------------------------------------------------------------------------
# 6. 新列与 payload 的形状
# --------------------------------------------------------------------------------------


async def test_new_payload_keys_are_appended_and_explicit(arena: MemoryArena) -> None:
    spec = RowSpec(
        id=1,
        workspace_id=0,
        adapter_key="onebot_v11",
        instance_key="ob1",
        preset_id=7,
        origin_chat_key=None,
        origin_chat_type="group",
    )
    await arena.add(MemTable.PARAGRAPH, [spec])
    paragraph = await DBMemParagraph.get(id=1)
    payload = paragraph.to_qdrant_payload()
    assert list(payload)[-5:] == [
        "adapter_key",
        "origin_chat_key",
        "origin_chat_type",
        "person_accounts",
        "payload_schema",
    ]
    assert payload["origin_chat_key"] is None
    assert payload["person_accounts"] == []
    assert payload["payload_schema"] == QDRANT_PAYLOAD_SCHEMA_VERSION == 2
    assert paragraph.to_dict()["adapter_key"] == "onebot_v11"
    assert paragraph.to_dict()["origin_chat_type"] == "group"


def test_table_fields_cover_the_required_shape() -> None:
    for table in (MemTable.PARAGRAPH, MemTable.EPISODE):
        assert TABLE_FIELDS[table] >= {
            ScopeField.WORKSPACE_ID,
            ScopeField.ADAPTER_KEY,
            ScopeField.INSTANCE_KEY,
            ScopeField.ORIGIN_CHAT_KEY,
            ScopeField.ORIGIN_CHAT_TYPE,
        }
    for table in (MemTable.ENTITY, MemTable.RELATION):
        assert TABLE_FIELDS[table] >= {
            ScopeField.WORKSPACE_ID,
            ScopeField.ADAPTER_KEY,
            ScopeField.INSTANCE_KEY,
            ScopeField.ORIGIN_CHAT_KEY,
        }
        assert ScopeField.ORIGIN_CHAT_TYPE not in TABLE_FIELDS[table]


def test_cond_nodes_are_hashable() -> None:
    tree = and_(
        eq(ScopeField.WORKSPACE_ID, 0),
        member_pairs([("onebot_v11", "ob1")]),
        preset_visible(7),
        or_(
            in_(ScopeField.ORIGIN_CHAT_KEY, ["gA", "gB"]),
            is_null(ScopeField.ORIGIN_CHAT_KEY),
        ),
    )
    assert hash(tree) == hash(tree)
    assert tree == tree
