"""世界书的范围条件

三件事要钉住：

1. **四条腿各自正确**：域内书、域外书、全局人设书、别人的频道书、别人的 person 书，
   在固定数据上逐条比对「纯 Python 求值」与「Tortoise 查询」的 ID 集合。
2. **退化输入不放行**：没有频道标识、人设 ID 非正、在场账号为空集，都折成恒假而不是
   「不过滤」——授权边界上退化成一类未知状态就是泄露的形状。
3. **管理端的筛选**：`AdminMemoryScope.condition(MemTable.WB_BOOK, …)` 只认自己那几维，
   越界的分区筛选抛错；记忆表上用只登记给书表的两列也抛错。
"""

from __future__ import annotations

import os
import random
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE

from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.services.memory.instance_scope import (
    AdminFilter,
    AdminMemoryScope,
    apply_worldbook_condition,
    worldbook_condition,
)
from nekro_agent.services.memory.scope_condition import (
    FALSE,
    TABLE_FIELDS,
    MemTable,
    ScopeField,
    eq,
    evaluate,
    to_tortoise_q,
)

PG_DSN_ENV = "NA_TEST_PG_DSN"
_MODEL_MODULES = ["nekro_agent.models"]


def _pg_dsn_or_skip() -> str:
    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(f"需要真实 PostgreSQL：设 {PG_DSN_ENV} 后运行；SQLite 腿始终运行。")
    return dsn


@pytest.fixture(params=["sqlite", "pg"], ids=["sqlite", "pg"])
async def db(request: pytest.FixtureRequest) -> Any:
    """每个后端一套真表；PG 在随机 schema 里建、结束 DROP SCHEMA CASCADE"""
    if request.param == "sqlite":
        EXECUTOR_CACHE.clear()
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": _MODEL_MODULES})
        await Tortoise.generate_schemas()
        try:
            yield "sqlite"
        finally:
            await Tortoise.close_connections()
            EXECUTOR_CACHE.clear()
        return

    dsn = _pg_dsn_or_skip()
    import asyncpg

    schema = f"na_test_wb_scope_{uuid.uuid4().hex[:12]}"
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
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'wb_book'",
        )
        assert schema in {str(row["table_schema"]) for row in rows}, "没有在真 PG 的目标 schema 里建出表"
        yield schema
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


@dataclass(frozen=True)
class BookSpec:
    """一本书在用例里的完整取值"""

    id: int
    scope_kind: str
    adapter_key: str = "onebot_v11"
    instance_key: str = "i1"
    preset_id: int = 0
    chat_key: str = ""
    person_account: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "scope_kind": self.scope_kind,
            "adapter_key": self.adapter_key,
            "instance_key": self.instance_key,
            "preset_id": self.preset_id,
            "chat_key": self.chat_key,
            "person_account": self.person_account,
        }

    async def create(self) -> DBWbBook:
        return await DBWbBook.create(
            id=self.id,
            name=f"书 {self.id}",
            scope_kind=self.scope_kind,
            adapter_key=self.adapter_key,
            instance_key=self.instance_key,
            preset_id=self.preset_id,
            chat_key=self.chat_key,
            person_account=self.person_account,
        )


async def _ids_via_tortoise(cond: Any) -> set[int]:
    """按条件树在库里选出书的 ID 集合"""
    queryset = apply_worldbook_condition(DBWbBook.all(), cond)
    return {book.id for book in await queryset}


def _ids_via_python(cond: Any, specs: list[BookSpec]) -> set[int]:
    """按同一棵树在纯 Python 参照语义下选出书的 ID 集合"""
    return {spec.id for spec in specs if evaluate(cond, spec.as_row())}


async def _populate(specs: list[BookSpec]) -> None:
    for spec in specs:
        await spec.create()


class TestFixedData:
    """固定数据：四种作用域各一对域内 / 域外，外加一本全局人设书"""

    SPECS = [
        # 域内：本实例的书
        BookSpec(1, "domain", adapter_key="onebot_v11", instance_key="i1"),
        BookSpec(2, "channel", adapter_key="onebot_v11", instance_key="i1", chat_key="gA"),
        BookSpec(3, "preset", adapter_key="onebot_v11", instance_key="i1", preset_id=7),
        BookSpec(4, "person", adapter_key="onebot_v11", instance_key="i1", person_account="u1"),
        # 域外：别的实例的书（同一适配器、不同实例；以及不同适配器）
        BookSpec(5, "domain", adapter_key="onebot_v11", instance_key="i2"),
        BookSpec(6, "channel", adapter_key="onebot_v11", instance_key="i2", chat_key="gA"),
        BookSpec(7, "preset", adapter_key="onebot_v11", instance_key="i2", preset_id=7),
        BookSpec(8, "person", adapter_key="onebot_v11", instance_key="i2", person_account="u1"),
        BookSpec(9, "domain", adapter_key="minecraft", instance_key="i1"),
        # 平台方的全局人设书：归属实例对为空，只有人设腿放行它
        BookSpec(10, "preset", adapter_key="", instance_key="", preset_id=7),
        # 归属实例对为空的其它类型：一条腿都不该放行
        BookSpec(11, "domain", adapter_key="", instance_key=""),
        BookSpec(12, "channel", adapter_key="", instance_key="", chat_key="gA"),
        # 别的频道、别的人、别的人设
        BookSpec(13, "channel", adapter_key="onebot_v11", instance_key="i1", chat_key="gB"),
        BookSpec(14, "person", adapter_key="onebot_v11", instance_key="i1", person_account="u2"),
        BookSpec(15, "preset", adapter_key="onebot_v11", instance_key="i1", preset_id=9),
    ]

    async def test_all_four_legs(self, db: Any) -> None:
        """域内的四条腿都进来，域外的一条都不进来"""
        await _populate(self.SPECS)
        cond = worldbook_condition(
            [("onebot_v11", "i1")],
            chat_key="gA",
            preset_id=7,
            present_accounts=["u1"],
        )
        assert await _ids_via_tortoise(cond) == {1, 2, 3, 4, 10}
        assert _ids_via_python(cond, self.SPECS) == {1, 2, 3, 4, 10}

    async def test_empty_chat_key_drops_the_channel_leg(self, db: Any) -> None:
        """没有频道标识时 `channel` 腿整体消失，其余三条腿照常"""
        await _populate(self.SPECS)
        cond = worldbook_condition(
            [("onebot_v11", "i1")],
            chat_key="",
            preset_id=7,
            present_accounts=["u1"],
        )
        assert await _ids_via_tortoise(cond) == {1, 3, 4, 10}

    async def test_degenerate_bindings_never_match(self, db: Any) -> None:
        """坏数据：绑定对象为空的频道 / 人设 / 个人书，遇上同样为空的输入也不能被放行

        退化输入折成 FALSE，不是「拿空串去等值匹配」：后者会让一本 `chat_key` 为空的
        频道书（手工改库或旧数据）在每个拿不到频道标识的调用里生效。
        """
        specs = [
            BookSpec(21, "channel", chat_key=""),
            BookSpec(22, "preset", preset_id=0),
            BookSpec(23, "person", person_account=""),
        ]
        await _populate(specs)
        cond = worldbook_condition(
            [("onebot_v11", "i1")],
            chat_key="",
            preset_id=0,
            present_accounts=["", "  "],
        )
        assert await _ids_via_tortoise(cond) == set()
        assert _ids_via_python(cond, specs) == set()

    async def test_zero_preset_drops_the_preset_leg(self, db: Any) -> None:
        """人设 ID 非正时 `preset` 腿整体消失：0 是「无人设档」，不是「任何人设」"""
        await _populate(self.SPECS)
        cond = worldbook_condition(
            [("onebot_v11", "i1")],
            chat_key="gA",
            preset_id=0,
            present_accounts=["u1"],
        )
        assert await _ids_via_tortoise(cond) == {1, 2, 4}

    async def test_no_present_accounts_drops_the_person_leg(self, db: Any) -> None:
        """在场账号为空时 `person` 腿整体消失，而不是「谁都不匹配但书还在」"""
        await _populate(self.SPECS)
        cond = worldbook_condition(
            [("onebot_v11", "i1")],
            chat_key="gA",
            preset_id=7,
            present_accounts=[],
        )
        assert await _ids_via_tortoise(cond) == {1, 2, 3, 10}

    async def test_empty_domain_only_allows_the_global_preset_book(self, db: Any) -> None:
        """域为空时三条腿恒假，只剩「平台方全局人设书」这一条口子

        这条口子是刻意的：那本书的归属实例对本来就是空的，它不属于任何域。除它以外
        一条都不该进来——把空域当成「不过滤」正是最危险的退化。
        """
        await _populate(self.SPECS)
        cond = worldbook_condition([], chat_key="gA", preset_id=7, present_accounts=["u1"])
        assert await _ids_via_tortoise(cond) == {10}
        assert _ids_via_python(cond, self.SPECS) == {10}

    async def test_accounts_are_stripped_and_deduped(self, db: Any) -> None:
        """账号去空白、去重、排序：同一组输入必须渲染出同一棵树"""
        left = worldbook_condition(
            [("onebot_v11", "i1")],
            chat_key="gA",
            preset_id=7,
            present_accounts=[" u1 ", "u1", "", "  "],
        )
        right = worldbook_condition(
            [("onebot_v11", "i1")],
            chat_key="gA",
            preset_id=7,
            present_accounts=["u1"],
        )
        assert left == right


class TestRandomOracle:
    """随机对拍：同一棵树在 Tortoise 与纯 Python 参照语义下给出同一批 ID"""

    def _specs(self, rng: random.Random, count: int) -> list[BookSpec]:
        kinds = ["domain", "preset", "channel", "person"]
        pairs = [("onebot_v11", "i1"), ("onebot_v11", "i2"), ("minecraft", "i1"), ("", "")]
        chats = ["", "gA", "gB"]
        persons = ["", "u1", "u2"]
        presets = [0, 7, 9]
        return [
            BookSpec(
                id=index + 1,
                scope_kind=rng.choice(kinds),
                adapter_key=(pair := rng.choice(pairs))[0],
                instance_key=pair[1],
                preset_id=rng.choice(presets),
                chat_key=rng.choice(chats),
                person_account=rng.choice(persons),
            )
            for index in range(count)
        ]

    async def test_random_cases_match(self, db: Any) -> None:
        rng = random.Random(20260201)
        specs = self._specs(rng, 60)
        await _populate(specs)

        pairs = [[("onebot_v11", "i1")], [("onebot_v11", "i2")], [("onebot_v11", "i1"), ("minecraft", "i1")], []]
        chats = ["", "gA", "gB"]
        persons = [[], ["u1"], ["u2"], ["u1", "u2"]]
        presets = [0, 7, 9]

        for _ in range(300):
            cond = worldbook_condition(
                rng.choice(pairs),
                chat_key=rng.choice(chats),
                preset_id=rng.choice(presets),
                present_accounts=rng.choice(persons),
            )
            expected = _ids_via_python(cond, specs)
            if cond is FALSE:
                # 恒假由调用方短路，渲染器本身会抛错（见条件树模块文档）。
                assert expected == set()
                continue
            assert await _ids_via_tortoise(cond) == expected


class TestAdminFilter:
    """管理端：书表只认自己的几维，越界的一律抛错"""

    async def test_scope_fields_are_registered(self) -> None:
        """书表的字段集合：只登记给它的两列在，工作区列与来源频道列不在"""
        fields = TABLE_FIELDS[MemTable.WB_BOOK]
        assert ScopeField.SCOPE_KIND in fields
        assert ScopeField.PERSON_ACCOUNT in fields
        assert ScopeField.WORKSPACE_ID not in fields
        assert ScopeField.ORIGIN_CHAT_KEY not in fields

    async def test_chat_preset_and_person_filters(self, db: Any) -> None:
        specs = TestFixedData.SPECS
        await _populate(specs)
        scope = AdminMemoryScope.for_pairs([("onebot_v11", "i1")])

        by_chat = scope.condition(MemTable.WB_BOOK, AdminFilter(chat_key="gA"))
        assert await _ids_via_tortoise(by_chat) == {2}

        by_preset = scope.condition(MemTable.WB_BOOK, AdminFilter(preset_id=7))
        assert await _ids_via_tortoise(by_preset) == {3}

        by_person = scope.condition(MemTable.WB_BOOK, AdminFilter(person_accounts=frozenset({"u1"})))
        assert await _ids_via_tortoise(by_person) == {4}

    async def test_empty_person_filter_matches_nothing(self, db: Any) -> None:
        """空的人员筛选是恒假，不是「不按人筛」：`None` 才是不筛"""
        await _populate(TestFixedData.SPECS)
        scope = AdminMemoryScope.for_pairs([("onebot_v11", "i1")])
        cond = scope.condition(MemTable.WB_BOOK, AdminFilter(person_accounts=frozenset()))
        assert cond is FALSE

    async def test_partition_filter_is_rejected(self, db: Any) -> None:
        """书表没有工作区分区：按 domain / workspace 筛选抛错，而不是静默忽略"""
        scope = AdminMemoryScope.for_pairs([("onebot_v11", "i1")])
        with pytest.raises(ValueError, match="世界书书表没有工作区分区"):
            scope.condition(MemTable.WB_BOOK, AdminFilter(partition="domain"))
        with pytest.raises(ValueError, match="世界书书表没有工作区分区"):
            scope.condition(MemTable.WB_BOOK, AdminFilter(partition="workspace"))

    async def test_platform_view_has_no_pair_filter(self, db: Any) -> None:
        """平台方全量：不加实例条件，四本书都能看到"""
        await _populate(TestFixedData.SPECS)
        cond = AdminMemoryScope.platform_all().condition(MemTable.WB_BOOK)
        ids = await _ids_via_tortoise(cond)
        assert {1, 5, 9}.issubset(ids)

    async def test_memory_tables_reject_the_new_fields(self) -> None:
        """只登记给书表的两列在记忆表上渲染时抛错：列根本不存在"""
        with pytest.raises(ValueError, match="不在 summary 表的可见范围列里"):
            to_tortoise_q(eq(ScopeField.SCOPE_KIND, "domain"), MemTable.SUMMARY)
        with pytest.raises(ValueError, match="不在 paragraph 表的可见范围列里"):
            to_tortoise_q(eq(ScopeField.PERSON_ACCOUNT, "u1"), MemTable.PARAGRAPH)

    async def test_book_table_rejects_memory_only_fields(self) -> None:
        """反向也要挡住：书表上没有工作区列与来源频道列"""
        with pytest.raises(ValueError, match="不在 wb_book 表的可见范围列里"):
            to_tortoise_q(eq(ScopeField.WORKSPACE_ID, 0), MemTable.WB_BOOK)

    async def test_person_dimension_still_rejected_on_memory_tables(self) -> None:
        """「涉及的人」那一维仍只允许段落表，书表的扩展没有放宽它"""
        scope = AdminMemoryScope.for_pairs([("onebot_v11", "i1")])
        with pytest.raises(ValueError, match="涉及的人这一维只允许出现在段落表上"):
            scope.condition(MemTable.SUMMARY, AdminFilter(person_accounts=frozenset({"u1"})))
