"""按条件批量清「涉及的人」关联行：用子查询，不把段落 ID 全取回来

删除实例 / 清空工作区时要先清关联行。若先取出全部段落 ID 再 `IN (...)`，段落一多就超过
数据库驱动一条语句的参数上限（asyncpg 是 32767 个），删除整个失败。这里钉住两件事：

1. `persons_of_paragraphs` 编译出来的是子查询，不是一串字面量 ID；
2. 在 SQLite 与真 PostgreSQL 上，它只删「满足条件的段落」的关联行，计数也一致。
"""

from __future__ import annotations

import os
import uuid
from typing import AsyncIterator
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE

from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.services.memory.persons import persons_of_paragraphs

PG_DSN_ENV = "NA_TEST_PG_DSN"
_MODEL_MODULES = [
    "nekro_agent.models.db_mem_paragraph",
    "nekro_agent.models.db_mem_paragraph_person",
]


@pytest.fixture(params=["sqlite", "pg"], ids=["sqlite", "pg"])
async def db(request: pytest.FixtureRequest) -> AsyncIterator[str]:
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

    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(f"需要真实 PostgreSQL：设 {PG_DSN_ENV} 后运行；SQLite 腿始终运行。")
    import asyncpg

    schema = f"na_test_persons_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    EXECUTOR_CACHE.clear()
    await Tortoise.init(db_url=f"{dsn}{sep}{urlencode({'schema': schema})}", modules={"models": _MODEL_MODULES})
    await Tortoise.generate_schemas()
    try:
        rows = await connections.get("default").execute_query_dict(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'mem_paragraph_person'",
        )
        assert schema in {str(row["table_schema"]) for row in rows}, "没有在真 PG 的目标 schema 里建出表"
        yield "pg"
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _paragraph(*, workspace_id: int, instance_key: str, persons: list[str]) -> DBMemParagraph:
    paragraph = await DBMemParagraph.create(
        workspace_id=workspace_id,
        instance_key=instance_key,
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.FACT,
        content="测试段落",
        origin_kind=OriginKind.CONSOLIDATION,
    )
    for account in persons:
        await DBMemParagraphPerson.create(paragraph_id=paragraph.id, account=account)
    return paragraph


async def test_compiles_to_a_subquery_not_an_id_list(db: str) -> None:
    sql = persons_of_paragraphs(instance_key="ob1").sql().upper()
    assert "SELECT" in sql.split("FROM", 1)[1], f"应当是子查询: {sql}"
    assert "MEM_PARAGRAPH" in sql


async def test_deletes_only_rows_of_matching_paragraphs(db: str) -> None:
    keep = await _paragraph(workspace_id=0, instance_key="ob2", persons=["u151", "uxw"])
    drop_a = await _paragraph(workspace_id=0, instance_key="ob1", persons=["u151"])
    drop_b = await _paragraph(workspace_id=1, instance_key="ob1", persons=["uxh", "uxw"])

    assert await persons_of_paragraphs(instance_key="ob1").count() == 3
    await persons_of_paragraphs(instance_key="ob1").delete()

    remaining = await DBMemParagraphPerson.all().values_list("paragraph_id", "account")
    assert sorted(remaining) == [(keep.id, "u151"), (keep.id, "uxw")]
    assert await DBMemParagraphPerson.filter(paragraph_id__in=[drop_a.id, drop_b.id]).count() == 0

    await persons_of_paragraphs(workspace_id=0).delete()
    assert await DBMemParagraphPerson.all().count() == 0


async def test_matches_nothing_when_no_paragraph_matches(db: str) -> None:
    await _paragraph(workspace_id=0, instance_key="ob2", persons=["u151"])
    assert await persons_of_paragraphs(instance_key="nobody").count() == 0
    await persons_of_paragraphs(instance_key="nobody").delete()
    assert await DBMemParagraphPerson.all().count() == 1
