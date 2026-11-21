"""书详情的条目搜索：标题、正文、主 / 次关键词都在数据库里匹配

要点：

1. 关键词逐个数组元素比，不把整列 JSON 当文本比——不会撞上 JSON 转义，也不会跨两个关键词误配；
2. 搜索词里的 `%`、`_`、`\\` 按字面匹配；
3. 关键词列不是数组（脏数据）时不匹配、不报错；
4. 排序、分页、总数都在数据库里算。

SQLite 侧常跑；PostgreSQL 侧设 `NA_TEST_PG_DSN`（如 `postgres://postgres@127.0.0.1:55499/natest`）开门，
未设则 skip，CI 的 backend-tests 工作流会设它。
"""

from __future__ import annotations

import os
import uuid
from typing import Any, AsyncIterator
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise
from tortoise.backends.base.executor import EXECUTOR_CACHE

from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.services.worldbook import service as wb_service

PG_DSN_ENV = "NA_TEST_PG_DSN"


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request: pytest.FixtureRequest) -> AsyncIterator[str]:
    if request.param == "sqlite":
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
        await Tortoise.generate_schemas()
        try:
            yield "sqlite"
        finally:
            await Tortoise.close_connections()
        return
    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(f"需要真实 PostgreSQL：设 {PG_DSN_ENV}。SQLite 侧用例常跑；CI 的 backend-tests 工作流会设它。")
    import asyncpg

    schema = f"na_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    EXECUTOR_CACHE.clear()
    await Tortoise.init(db_url=f"{dsn}{sep}{urlencode({'schema': schema})}", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield "postgres"
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _book() -> DBWbBook:
    return await DBWbBook.create(
        name="搜索书",
        description="",
        scope_kind="domain",
        adapter_key="onebot_v11",
        instance_key="t1a",
        preset_id=0,
        chat_key="",
        person_account="",
        enabled=True,
        scan_depth=4,
        budget_tokens=0,
    )


async def _entry(book: DBWbBook, uid: int, **fields: Any) -> DBWbEntry:
    values: dict[str, Any] = {
        "comment": "",
        "keys": [],
        "secondary_keys": [],
        "content": "正文",
        "order": 100,
    }
    values.update(fields)
    return await DBWbEntry.create(
        book=book,
        uid=uid,
        selective_logic="and_any",
        use_regex=False,
        constant=False,
        position="before_history",
        depth=4,
        enabled=True,
        trusted=False,
        status="active",
        ext={},
        **values,
    )


async def _search(book: DBWbBook, query: str, *, page: int = 1, page_size: int = 50) -> tuple[list[int], int]:
    rows, total = await wb_service.list_entries_page(book, page=page, page_size=page_size, query=query)
    return [row.id for row in rows], total


class TestEntrySearch:
    async def test_title_body_and_each_key(self, db: str) -> None:
        book = await _book()
        by_title = await _entry(book, 1, comment="龙的传说", order=50)
        by_body = await _entry(book, 2, content="山里住着一条龙", order=90)
        by_key = await _entry(book, 3, keys=["火龙"], order=70)
        by_secondary = await _entry(book, 4, secondary_keys=["小龙女"], order=70)
        await _entry(book, 5, comment="无关", keys=["凤"], secondary_keys=["鸟"])

        ids, total = await _search(book, "龙")
        assert ids == [by_body.id, by_key.id, by_secondary.id, by_title.id]
        assert total == 4

    async def test_like_wildcards_are_literal(self, db: str) -> None:
        book = await _book()
        percent = await _entry(book, 1, keys=["100%"])
        await _entry(book, 2, keys=["1000"])
        underscore = await _entry(book, 3, keys=["a_b"])
        await _entry(book, 4, keys=["axb"])
        backslash = await _entry(book, 5, secondary_keys=["c\\d"])
        await _entry(book, 6, secondary_keys=["cd"])

        assert (await _search(book, "100%"))[0] == [percent.id]
        assert (await _search(book, "a_b"))[0] == [underscore.id]
        assert (await _search(book, "c\\d"))[0] == [backslash.id]

    async def test_ascii_is_case_insensitive(self, db: str) -> None:
        book = await _book()
        upper = await _entry(book, 1, keys=["Dragon"])
        title = await _entry(book, 2, comment="DRAGON 档案")
        assert set((await _search(book, "dragon"))[0]) == {upper.id, title.id}

    async def test_keys_do_not_match_across_elements_or_json_quoting(self, db: str) -> None:
        book = await _book()
        await _entry(book, 1, keys=["ab", "cd"])
        quoted = await _entry(book, 2, keys=['他说"你好"'])
        # 把整列 JSON 当文本比的话，`b", "c` 能跨两个关键词配上；`"你好"` 在 JSON 里是转义过的
        assert (await _search(book, 'b", "c'))[0] == []
        assert (await _search(book, '"你好"'))[0] == [quoted.id]

    async def test_non_array_key_columns_do_not_break_the_search(self, db: str) -> None:
        book = await _book()
        good = await _entry(book, 1, keys=["龙"])
        odd = await _entry(book, 2, keys=["占位"])
        conn = Tortoise.get_connection("default")
        if db == "postgres":
            await conn.execute_query('UPDATE "wb_entry" SET "keys" = \'{"a": "龙"}\'::jsonb WHERE "id" = $1', [odd.id])
        else:
            await conn.execute_query('UPDATE "wb_entry" SET "keys" = \'{"a": "龙"}\' WHERE "id" = ?', [odd.id])
            broken = await _entry(book, 3, keys=["占位"])
            await conn.execute_query('UPDATE "wb_entry" SET "keys" = \'not json\' WHERE "id" = ?', [broken.id])
        assert (await _search(book, "龙"))[0] == [good.id]

    async def test_paging_and_total_come_from_the_database(self, db: str) -> None:
        book = await _book()
        rows = [await _entry(book, index, keys=[f"词{index}"], order=100 - index) for index in range(7)]
        await _entry(book, 99, keys=["别的"])

        ids, total = await _search(book, "词", page=3, page_size=3)
        assert (ids, total) == ([rows[6].id], 7)
        ids, total = await _search(book, "词", page=2, page_size=3)
        assert (ids, total) == ([row.id for row in rows[3:6]], 7)
        ids, total = await _search(book, "词", page=9, page_size=3)
        assert (ids, total) == ([], 7)
        assert await _search(book, "没有这个词") == ([], 0)

    async def test_other_books_are_not_searched(self, db: str) -> None:
        book = await _book()
        other = await _book()
        mine = await _entry(book, 1, keys=["龙"])
        await _entry(other, 1, keys=["龙"])
        assert await _search(book, "龙") == ([mine.id], 1)
