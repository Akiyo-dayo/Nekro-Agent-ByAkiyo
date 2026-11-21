"""世界书的两张表与提示词明细新增的两列

这份用例只钉「库表本身能不能用」：两个后端上真的建出表、`(book, uid)` 的唯一约束真的
拦得住、删书真的连带删条目、`order` 这个列名在两个后端上都不是保留字、明细表两个新列
的默认值符合预期。业务判据（范围、命中、预算）在别的文件里，混在一起会让这里的失败
原因变得不可读。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE
from tortoise.exceptions import IntegrityError

from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry

PG_DSN_ENV = "NA_TEST_PG_DSN"

# 整包登记：外键指向 `models.DBWbBook`，只登记单模块时 Tortoise 解析不到关系。
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

    schema = f"na_test_wb_models_{uuid.uuid4().hex[:12]}"
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
        # 证明查的是目标 schema，而不是某条静默回退的 SQLite 路径。
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


async def _make_book(name: str = "测试书", **extra: Any) -> DBWbBook:
    """建一本域内书；调用方只需要关心自己那几个字段"""
    return await DBWbBook.create(
        name=name,
        scope_kind=extra.pop("scope_kind", "domain"),
        adapter_key=extra.pop("adapter_key", "onebot_v11"),
        instance_key=extra.pop("instance_key", "i1"),
        **extra,
    )


async def _make_entry(book: DBWbBook, uid: int, **extra: Any) -> DBWbEntry:
    return await DBWbEntry.create(
        book=book,
        uid=uid,
        content=extra.pop("content", f"正文 {uid}"),
        **extra,
    )


class TestTablesExist:
    async def test_both_tables_are_usable(self, db: Any) -> None:
        """两张表都建出来了：各写一行再读回来"""
        book = await _make_book()
        entry = await _make_entry(book, 1)
        assert book.id > 0
        assert entry.book_id == book.id
        assert (await DBWbBook.all().count()) == 1
        assert (await DBWbEntry.all().count()) == 1

    async def test_json_columns_default_to_empty_collections(self, db: Any) -> None:
        """`keys` / `secondary_keys` / `ext` 的默认值是空数组与空对象，不是 NULL

        默认值写成 NULL 的话，读出来是 `None` 而不是 `[]`，每一处「遍历关键词」的地方
        就都得多判一次空，漏一处就是迭代 `None` 崩掉。
        """
        book = await _make_book()
        entry = await DBWbEntry.create(book=book, uid=1, content="x")
        assert entry.keys == []
        assert entry.secondary_keys == []
        assert entry.ext == {}
        assert book.ext == {}

    async def test_nullable_overrides_stay_null(self, db: Any) -> None:
        """条目的三态覆盖与定时效果默认是 NULL，表示「跟书」/「不用」"""
        book = await _make_book()
        entry = await DBWbEntry.create(book=book, uid=1, content="x")
        assert entry.scan_depth is None
        assert entry.case_sensitive is None
        assert entry.match_whole_words is None
        assert entry.sticky is None
        assert entry.cooldown is None
        assert entry.delay is None
        assert entry.quarantined_at is None


class TestModelRegistration:
    def test_models_are_exported_from_the_models_package(self) -> None:
        """登记进 `nekro_agent.models` 才算真的接上：只建文件不放 `__init__` 时，
        任何 `Tortoise.init(modules=["nekro_agent.models"])` 都看不到这张表。"""
        import nekro_agent.models as models

        assert models.DBWbBook is DBWbBook
        assert models.DBWbEntry is DBWbEntry


class TestConstraints:
    async def test_unique_book_and_uid(self, db: Any) -> None:
        """同一本书里 `uid` 唯一：第二次写入被数据库拒绝"""
        book = await _make_book()
        await _make_entry(book, 3)
        with pytest.raises(IntegrityError):
            await _make_entry(book, 3)

    async def test_same_uid_in_different_books_is_allowed(self, db: Any) -> None:
        """不同书里的 `uid` 必然重复，唯一约束不能只落在 `uid` 上"""
        first = await _make_book("书一")
        second = await _make_book("书二")
        await _make_entry(first, 1)
        await _make_entry(second, 1)
        assert (await DBWbEntry.all().count()) == 2

    async def test_deleting_book_deletes_entries(self, db: Any) -> None:
        """删书连带删条目：真删，不留看不见的副本"""
        book = await _make_book()
        await _make_entry(book, 1)
        await _make_entry(book, 2)
        await book.delete()
        assert (await DBWbEntry.all().count()) == 0


class TestOrderColumn:
    async def test_order_by_order_works_on_both_backends(self, db: Any) -> None:
        """`order` 是酒馆的字段名，两个后端都不能当保留字处理"""
        book = await _make_book()
        await _make_entry(book, 1, order=300)
        await _make_entry(book, 2, order=100)
        await _make_entry(book, 3, order=200)
        orders = [entry.order for entry in await DBWbEntry.filter(book=book).order_by("order")]
        assert orders == [100, 200, 300]

    async def test_order_defaults_to_hundred(self, db: Any) -> None:
        book = await _make_book()
        entry = await DBWbEntry.create(book=book, uid=1, content="x")
        assert entry.order == 100


class TestPromptRoundColumns:
    async def test_worldbook_columns_have_expected_defaults(self, db: Any) -> None:
        """明细表两个新列：状态默认 `disabled`、字数默认 0"""
        now = datetime(2026, 2, 1, 12, 0, 0)
        row = await DBMemPromptRound.create(
            round_id=uuid.uuid4().hex[:16],
            chat_key="g1",
            started_at=now,
            finished_at=now + timedelta(seconds=3),
        )
        assert row.worldbook_state == "disabled"
        assert row.worldbook_chars == 0
        assert row.worldbook_items == []

    async def test_worldbook_columns_round_trip(self, db: Any) -> None:
        """写进去的状态、引用与字数能原样读回来（JSON 列不是字符串）"""
        now = datetime(2026, 2, 1, 12, 0, 0)
        row = await DBMemPromptRound.create(
            round_id=uuid.uuid4().hex[:16],
            chat_key="g1",
            worldbook_state="injected",
            worldbook_items=[{"entry_id": 7, "chars": 12}],
            worldbook_chars=12,
            started_at=now,
            finished_at=now + timedelta(seconds=1),
        )
        loaded = await DBMemPromptRound.get(id=row.id)
        assert loaded.worldbook_state == "injected"
        assert loaded.worldbook_chars == 12
        assert loaded.worldbook_items == [{"entry_id": 7, "chars": 12}]
