"""存量向量 payload 回填：只补新键、幂等、可中断，缺 PG 行的点跳过。

闸打开后检索会读 `adapter_key` / `origin_chat_key` / `origin_chat_type` /
`person_accounts` / `payload_schema`。存量向量点没有这些键；按域过滤时它们会被当成
「来源不明」而排除在召回之外 —— 这是安全的一侧，但不能一直不补。

回填以 `payload_schema != 2` 为工作集，每页写完立刻把 schema 写成 2，所以：

- 重复执行第二次返回 0；
- 中途失败后重跑只处理没写完的点；
- PG 里已删除的段落对应的点跳过并告警，不阻塞其余点。
"""

from __future__ import annotations

from typing import Any, List

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from tortoise import Tortoise

from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.services.memory import qdrant_manager as qdrant_module


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
async def qdrant(monkeypatch: pytest.MonkeyPatch) -> AsyncQdrantClient:
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        collection_name=qdrant_module.memory_qdrant_manager.collection_name,
        vectors_config=qdrant_models.VectorParams(size=3, distance=qdrant_models.Distance.COSINE),
    )

    async def fake_client() -> AsyncQdrantClient:
        return client

    monkeypatch.setattr(qdrant_module, "get_qdrant_client", fake_client)
    monkeypatch.setattr(qdrant_module, "PAYLOAD_BACKFILL_BATCH_SIZE", 1)
    try:
        yield client
    finally:
        await client.close()


async def seed_paragraph(index: int, chat_key: str) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=0,
        instance_key=f"inst-{index}",
        adapter_key="onebot_v11",
        origin_chat_key=chat_key,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type="episodic",
        knowledge_type="fact",
        content=f"段落 {index}",
        summary=f"摘要 {index}",
        origin_kind="consolidation",
    )


async def upsert_old_point(client: AsyncQdrantClient, point_id: int) -> None:
    await client.upsert(
        collection_name=qdrant_module.memory_qdrant_manager.collection_name,
        points=[
            qdrant_models.PointStruct(
                id=point_id,
                vector=[0.1, 0.2, 0.3],
                payload={"workspace_id": 0, "content": f"旧点 {point_id}"},
            ),
        ],
        wait=True,
    )


async def payload_of(client: AsyncQdrantClient, point_id: int) -> dict[str, Any]:
    points = await client.retrieve(
        collection_name=qdrant_module.memory_qdrant_manager.collection_name,
        ids=[point_id],
        with_payload=True,
    )
    assert len(points) == 1
    return dict(points[0].payload or {})


class TestBackfill:
    async def test_fills_all_new_keys_and_is_idempotent(
        self,
        memory_db: None,
        qdrant: AsyncQdrantClient,
    ) -> None:
        first = await seed_paragraph(1, "onebot_v11-bf-group_1")
        second = await seed_paragraph(2, "onebot_v11-bf-group_2")
        await upsert_old_point(qdrant, first.id)
        await upsert_old_point(qdrant, second.id)

        updated = await qdrant_module.memory_qdrant_manager.backfill_paragraph_payloads()

        assert updated == 2
        first_payload = await payload_of(qdrant, first.id)
        assert first_payload["adapter_key"] == "onebot_v11"
        assert first_payload["origin_chat_key"] == "onebot_v11-bf-group_1"
        assert first_payload["origin_chat_type"] == "group"
        assert first_payload["person_accounts"] == []
        assert first_payload["payload_schema"] == qdrant_module.QDRANT_PAYLOAD_SCHEMA_VERSION
        # 既有 payload 键不能被整份替换掉。
        assert first_payload["workspace_id"] == 0

        assert await qdrant_module.memory_qdrant_manager.backfill_paragraph_payloads() == 0

    async def test_interrupted_run_resumes_with_remaining_points(
        self,
        memory_db: None,
        qdrant: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rows = [await seed_paragraph(i, f"onebot_v11-bf-group_{i}") for i in (1, 2, 3)]
        for row in rows:
            await upsert_old_point(qdrant, row.id)

        manager = qdrant_module.memory_qdrant_manager
        original = manager.set_paragraph_payload
        calls = 0

        async def flaky(paragraph_ids: List[int], payload: dict[str, Any]) -> int:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("interrupted")
            return await original(paragraph_ids, payload)

        monkeypatch.setattr(manager, "set_paragraph_payload", flaky)

        with pytest.raises(RuntimeError):
            await manager.backfill_paragraph_payloads()

        assert calls == 2
        # 第一页已落盘；剩下两页未处理。
        assert (await payload_of(qdrant, rows[0].id))["payload_schema"] == 2
        assert (await payload_of(qdrant, rows[1].id)).get("payload_schema") != 2
        assert (await payload_of(qdrant, rows[2].id)).get("payload_schema") != 2

        monkeypatch.setattr(manager, "set_paragraph_payload", original)
        assert await manager.backfill_paragraph_payloads() == 2

    async def test_missing_pg_row_is_skipped(
        self,
        memory_db: None,
        qdrant: AsyncQdrantClient,
    ) -> None:
        await upsert_old_point(qdrant, 999)

        assert await qdrant_module.memory_qdrant_manager.backfill_paragraph_payloads() == 0
        assert "payload_schema" not in await payload_of(qdrant, 999)
