"""域分区的写入侧：分区 0 的行必须带全归属列。

闸开时一个频道可以没有工作区，记忆落到 `workspace_id = 0`。域行的隔离全靠
`(adapter_key, origin_chat_key)`：缺一个就会变成所有频道共享的无主记忆，所以写入侧
宁可拒绝这一条，也不能写一条错行。

本文件同时钉住两件事：

- 工作区 0 的段落 / 实体 / 关系都写 `adapter_key` 与 `origin_chat_key`，段落还写
  `origin_chat_type`；同一实例的两个频道出现同名实体时是两行，不能合并。
- 闸开时 `consolidate` 进门先看频道自己的聊天记忆开关，关着就整批跳过。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory.consolidator import EpisodicConsolidator

CHAT_A = "onebot_v11-writer-a-group_1"
CHAT_B = "onebot_v11-writer-b-group_2"
INSTANCE = "writer-a"

ITEM: dict[str, Any] = {
    "content": "张三和李四在项目里合作，结论是先把缓存层拆出去。",
    "summary": "张三与李四合作拆分缓存层",
    "knowledge_type": "decision",
    "entities": ["张三", "李四"],
    "relations": [{"subject": "张三", "predicate": "合作", "object": "李四"}],
}


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    # 本文件只测归属列；输出过滤在别的文件里单独测，这里关掉避免互相干扰。
    monkeypatch.setattr(config, "OUTPUT_FILTER_ENABLE", False)
    monkeypatch.setattr(config, "OUTPUT_FILTER_BEFORE_MEMORY", False)


@pytest.fixture(autouse=True)
def no_vector_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_embed(_text: str, **_kwargs: object) -> List[float]:
        return [0.1, 0.2, 0.3]

    async def fake_upsert(**_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(consolidator_module, "embed_text", fake_embed)
    monkeypatch.setattr(consolidator_module.memory_qdrant_manager, "upsert_paragraph", fake_upsert)


async def seed_channel(
    chat_key: str,
    *,
    adapter_key: str = "onebot_v11",
    instance_key: str = INSTANCE,
    chat_type: str = "group",
) -> None:
    await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=adapter_key,
        instance_key=instance_key,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type=chat_type,
        is_active=True,
        workspace_id=None,
        data="{}",
    )


async def store(chat_key: str) -> tuple[DBMemParagraph | None, list[Any], list[Any]]:
    consolidator = EpisodicConsolidator(0)
    await consolidator._load_channel(chat_key)  # noqa: SLF001
    return await consolidator._store_memory(ITEM, chat_key, [])  # noqa: SLF001


class TestDomainRowColumns:
    async def test_paragraph_and_graph_carry_the_full_origin(self, memory_db: None) -> None:
        await seed_channel(CHAT_A)

        paragraph, entities, relations = await store(CHAT_A)

        assert paragraph is not None
        assert paragraph.workspace_id == 0
        assert paragraph.adapter_key == "onebot_v11"
        assert paragraph.origin_chat_key == CHAT_A
        assert paragraph.origin_chat_type == "group"
        assert paragraph.instance_key == INSTANCE
        assert {entity.adapter_key for entity, _ in entities} == {"onebot_v11"}
        assert {entity.origin_chat_key for entity, _ in entities} == {CHAT_A}
        assert {relation.adapter_key for relation, _ in relations} == {"onebot_v11"}
        assert {relation.origin_chat_key for relation, _ in relations} == {CHAT_A}

    @pytest.mark.parametrize(("chat_type", "expected"), [("private", "private"), ("thread", "")])
    async def test_origin_chat_type_maps_only_group_and_private(
        self,
        memory_db: None,
        chat_type: str,
        expected: str,
    ) -> None:
        await seed_channel(CHAT_A, chat_type=chat_type)

        paragraph, _, _ = await store(CHAT_A)

        assert paragraph is not None
        assert paragraph.origin_chat_type == expected


class TestDomainDedup:
    async def test_same_names_in_two_channels_are_two_rows(self, memory_db: None) -> None:
        await seed_channel(CHAT_A)
        await seed_channel(CHAT_B)

        first_paragraph, first_entities, first_relations = await store(CHAT_A)
        second_paragraph, second_entities, second_relations = await store(CHAT_B)

        assert first_paragraph is not None and second_paragraph is not None
        assert first_paragraph.id != second_paragraph.id
        assert await DBMemEntity.all().count() == 4
        assert await DBMemRelation.all().count() == 2
        assert {entity.origin_chat_key for entity, _ in first_entities} == {CHAT_A}
        assert {entity.origin_chat_key for entity, _ in second_entities} == {CHAT_B}
        assert {relation.origin_chat_key for relation, _ in first_relations} == {CHAT_A}
        assert {relation.origin_chat_key for relation, _ in second_relations} == {CHAT_B}


class TestDomainInvariant:
    async def test_domain_row_without_adapter_is_not_written(self, memory_db: None) -> None:
        await seed_channel(CHAT_A, adapter_key="")

        with pytest.raises(ValueError):
            await store(CHAT_A)

        assert await DBMemParagraph.all().count() == 0
        assert await DBMemEntity.all().count() == 0
        assert await DBMemRelation.all().count() == 0


class TestConsolidateChannelSwitch:
    async def test_consolidate_skips_when_channel_switch_is_off(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _OffChannel:
            chat_key = CHAT_A
            workspace_id = None
            adapter_key = "onebot_v11"
            instance_key = INSTANCE
            chat_type = "group"

            async def get_effective_config(self) -> SimpleNamespace:
                return SimpleNamespace(MEMORY_ENABLE_FOR_CHAT=False)

        async def fake_load_channel(_self: EpisodicConsolidator, _chat_key: str) -> _OffChannel:
            return _OffChannel()

        monkeypatch.setattr(EpisodicConsolidator, "_load_channel", fake_load_channel)

        result = await EpisodicConsolidator(0).consolidate(CHAT_A)

        assert result.skipped_reason == "memory_disabled"
        assert result.messages_processed == 0
        assert await DBMemParagraph.all().count() == 0


class TestEmbeddingRequester:
    async def test_store_uses_channel_requester(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_channel(CHAT_A)
        captured: list[Any] = []

        async def fake_embed(_text: str, **kwargs: Any) -> List[float]:
            captured.append(kwargs.get("requester"))
            return [0.1, 0.2, 0.3]

        monkeypatch.setattr(consolidator_module, "embed_text", fake_embed)

        await store(CHAT_A)

        assert captured == [("onebot_v11", INSTANCE)]
