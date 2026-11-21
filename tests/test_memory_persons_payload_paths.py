"""所有写段落 payload 的路径都要带上表里的「涉及的人」，删除路径要清掉关联行。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qdrant_models
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.routers.workspaces import MemoryEditBody, edit_memory
from nekro_agent.services.console import instance_manager
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.persons import replace_paragraph_persons
from nekro_agent.services.memory.qdrant_manager import MEMORY_PARAGRAPH_COLLECTION
from nekro_agent.services.user.role import Role


@pytest.fixture
async def memory_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)

    async def fake_embed(_text: str, **_kwargs: object) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]

    async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr(embedding_service_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_batch", fake_embed_batch)
    monkeypatch.setattr(qdrant_manager_module, "embed_batch", fake_embed_batch)
    monkeypatch.setattr(qdrant_manager_module, "get_memory_embedding_dimension", lambda: 4)

    client = AsyncQdrantClient(location=":memory:")

    async def fake_get_client() -> AsyncQdrantClient:
        return client

    monkeypatch.setattr(qdrant_manager_module, "get_qdrant_client", fake_get_client)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await qdrant_manager_module.memory_qdrant_manager.ensure_collection(4)
    try:
        yield client
    finally:
        await client.close()
        await Tortoise.close_connections()


async def _paragraph(
    *,
    workspace_id: int = 0,
    instance_key: str = "inst",
    content: str = "一条有人的记忆",
    embedding_ref: str | None = None,
) -> DBMemParagraph:
    paragraph = await DBMemParagraph.create(
        workspace_id=workspace_id,
        instance_key=instance_key,
        adapter_key="onebot_v11",
        origin_chat_key="onebot_v11-inst-group_1",
        origin_chat_type="group",
        memory_source="na",
        cognitive_type="episodic",
        knowledge_type="fact",
        content=content,
        summary=content,
        origin_kind="consolidation",
        embedding_ref=embedding_ref,
    )
    await replace_paragraph_persons(paragraph.id, ["uxw", "u151"])
    return paragraph


async def _payload_of(client: AsyncQdrantClient, paragraph_id: int) -> dict[str, Any]:
    records = await client.retrieve(
        collection_name=MEMORY_PARAGRAPH_COLLECTION,
        ids=[paragraph_id],
        with_payload=True,
    )
    assert len(records) == 1
    return dict(records[0].payload or {})


class TestBackfill:
    async def test_backfill_writes_the_real_person_accounts(
        self,
        memory_env: AsyncQdrantClient,
    ) -> None:
        paragraph = await _paragraph()
        await memory_env.upsert(
            collection_name=MEMORY_PARAGRAPH_COLLECTION,
            points=[
                qdrant_models.PointStruct(
                    id=paragraph.id,
                    vector=[0.1, 0.2, 0.3, 0.4],
                    payload={"workspace_id": 0},
                ),
            ],
            wait=True,
        )

        updated = await qdrant_manager_module.memory_qdrant_manager.backfill_paragraph_payloads()

        assert updated == 1
        assert (await _payload_of(memory_env, paragraph.id))["person_accounts"] == ["u151", "uxw"]


class TestRebuild:
    async def test_rebuild_writes_the_real_person_accounts(
        self,
        memory_env: AsyncQdrantClient,
    ) -> None:
        paragraph = await _paragraph()

        result = await qdrant_manager_module.memory_qdrant_manager.rebuild_collection()

        assert result["success"] == 1
        assert (await _payload_of(memory_env, paragraph.id))["person_accounts"] == ["u151", "uxw"]


class TestWorkspaceEditReembed:
    async def test_editing_content_keeps_the_person_accounts(
        self,
        memory_env: AsyncQdrantClient,
    ) -> None:
        workspace = await DBWorkspace.create(name="记忆编辑工作区", description="测试")
        paragraph = await _paragraph(workspace_id=workspace.id, embedding_ref="1")
        user = DBUser(
            username="平台管理员",
            password="",
            adapter_key="webui",
            platform_userid="0",
            perm_level=Role.Super,
            login_time=datetime.now(timezone.utc),
            tenant_id=None,
            tenant_role="platform_owner",
        )

        await edit_memory(
            workspace_id=workspace.id,
            memory_type="paragraph",
            memory_id=paragraph.id,
            body=MemoryEditBody(content="编辑后的内容"),
            _current_user=user,
        )

        await paragraph.refresh_from_db()
        assert paragraph.content == "编辑后的内容"
        assert (await _payload_of(memory_env, paragraph.id))["person_accounts"] == ["u151", "uxw"]


class TestPurgeInstance:
    async def test_purge_removes_person_rows(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance = await DBAdapterInstance.create(adapter_key="onebot_v11", instance_key="inst")
        paragraph = await _paragraph(instance_key="inst")
        assert await DBMemParagraphPerson.filter(paragraph_id=paragraph.id).count() == 2

        async def noop(_instance_key: str) -> None:
            return None

        async def noop_container(_instance_key: str, *, had_builtin_container: bool) -> None:
            del had_builtin_container
            return None

        monkeypatch.setattr(instance_manager, "purge_instance_vectors", noop)
        monkeypatch.setattr(instance_manager, "_reclaim_purged_instance_container", noop_container)

        counts = await instance_manager.purge_instance(instance)

        assert counts["mem_paragraph_person"] == 2
        assert await DBMemParagraphPerson.filter(paragraph_id=paragraph.id).count() == 0
        assert await DBMemParagraph.filter(id=paragraph.id).count() == 0
