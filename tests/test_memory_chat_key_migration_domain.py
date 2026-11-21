"""认领频道时的记忆迁移：段落、实体、关系、向量 payload 一起换。

`chat_key` 迁移以前只改「聊天维度」的表，新增的域归属列被漏下：段落还指着旧频道，
实体 / 关系的 `origin_chat_key` 还是旧值，Qdrant payload 更是原样不动。结果是认领完成后
新频道看不到旧记忆，而旧频道名下的域行又已经不该存在。

迁移只换来源键，不碰 `origin_chat_type`：那是频道类型，认领不改它。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services import chat_key_migration
from nekro_agent.services.chat_key_migration import migrate_chat_key_references

OLD_CHAT = "onebot_v11-old-group_1"
NEW_CHAT = "onebot_v11-new-group_1"


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def seed() -> tuple[DBMemParagraph, DBMemEntity, DBMemEntity, DBMemRelation]:
    paragraph = await DBMemParagraph.create(
        workspace_id=0,
        instance_key="",
        adapter_key="onebot_v11",
        origin_chat_key=OLD_CHAT,
        origin_chat_type="group",
        memory_source="na",
        cognitive_type="episodic",
        knowledge_type="fact",
        content="旧频道的一段记忆",
        summary="旧记忆",
        origin_kind="consolidation",
    )
    subject = await DBMemEntity.create(
        workspace_id=0,
        adapter_key="onebot_v11",
        origin_chat_key=OLD_CHAT,
        entity_type="person",
        name="张三",
        canonical_name="张三",
    )
    obj = await DBMemEntity.create(
        workspace_id=0,
        adapter_key="onebot_v11",
        origin_chat_key=OLD_CHAT,
        entity_type="person",
        name="李四",
        canonical_name="李四",
    )
    relation = await DBMemRelation.create(
        workspace_id=0,
        adapter_key="onebot_v11",
        origin_chat_key=OLD_CHAT,
        subject_entity_id=subject.id,
        predicate="合作",
        object_entity_id=obj.id,
        paragraph_id=paragraph.id,
        memory_source="na",
        cognitive_type="episodic",
    )
    return paragraph, subject, obj, relation


class TestChatKeyMigrationDomain:
    async def test_all_domain_origin_keys_are_migrated(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paragraph, subject, obj, relation = await seed()
        payloads: List[Dict[str, Any]] = []

        async def fake_set_payload(_ids: List[int], payload: Dict[str, Any]) -> int:
            payloads.append(payload)
            return 1

        monkeypatch.setattr(chat_key_migration.memory_qdrant_manager, "set_paragraph_payload", fake_set_payload)

        counts = await migrate_chat_key_references(OLD_CHAT, NEW_CHAT, instance_key="inst-new")

        paragraph = await DBMemParagraph.get(id=paragraph.id)
        subject = await DBMemEntity.get(id=subject.id)
        obj = await DBMemEntity.get(id=obj.id)
        relation = await DBMemRelation.get(id=relation.id)

        assert paragraph.origin_chat_key == NEW_CHAT
        assert paragraph.instance_key == "inst-new"
        assert paragraph.origin_chat_type == "group"
        assert subject.origin_chat_key == NEW_CHAT
        assert obj.origin_chat_key == NEW_CHAT
        assert relation.origin_chat_key == NEW_CHAT
        assert counts["mem_paragraph"] == 1
        assert counts["mem_entity_origin"] == 2
        assert counts["mem_relation_origin"] == 1
        assert counts["qdrant_paragraph_payload"] == 1
        assert payloads == [
            {"instance_key": "inst-new", "origin_chat_key": NEW_CHAT, "origin_chat_type": "group"},
        ]

    async def test_payload_sync_runs_even_without_instance_key(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed()
        payloads: List[Dict[str, Any]] = []

        async def fake_set_payload(_ids: List[int], payload: Dict[str, Any]) -> int:
            payloads.append(payload)
            return 1

        monkeypatch.setattr(chat_key_migration.memory_qdrant_manager, "set_paragraph_payload", fake_set_payload)

        await migrate_chat_key_references(OLD_CHAT, NEW_CHAT)

        assert payloads
        assert payloads[0]["origin_chat_key"] == NEW_CHAT
        assert payloads[0]["instance_key"] == ""
