"""记忆前过滤：只作用于「这之后写进去的记忆」，且不能顺手借出站统计的账。

`OUTPUT_FILTER_BEFORE_MEMORY` 之前只有说明文字、没有实现；这次把它接到沉淀写入前：

- 段落正文与摘要按替换语义过滤（片段替换，不按 `whole` 把整段丢掉）；
- 实体 / 关系的名字、关系描述命中时整条丢弃（它们没有「替换后仍然成立」的语义）；
- 出站过滤的命中统计（`record_hits_for_chat`）不参与，记忆过滤不是一次发送；
- 编译 / 引擎异常时 fail-closed，整段换成替换词。

已经写好的记忆不回改：过滤只发生在写入前，读路径（注入、检索）不过滤。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory.channel_summary import get_latest_summary
from nekro_agent.services.memory.consolidator import EpisodicConsolidator
from nekro_agent.services.output_filter import service as output_filter_service

CHAT = "onebot_v11-filter-group_1"
FILTERED_CFG: Any = SimpleNamespace(
    OUTPUT_FILTER_ENABLE=True,
    OUTPUT_FILTER_BEFORE_MEMORY=True,
    OUTPUT_FILTER_WORDS="机密",
    OUTPUT_FILTER_REGEX="",
    OUTPUT_FILTER_MODE="whole",
    OUTPUT_FILTER_REPLACEMENT="**Filter**",
    MEMORY_ENABLE_FOR_CHAT=True,
    MEMORY_CHANNEL_SUMMARY_ENABLED=True,
    MEMORY_CHANNEL_SUMMARY_MAX_CHARS=400,
)


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def no_outbound_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("记忆前过滤不得写作出站命中统计")

    monkeypatch.setattr(output_filter_service, "record_hits_for_chat", forbidden)


class TestPureFilter:
    def test_fragment_is_replaced_but_rest_kept(self) -> None:
        text = "这是一段机密内容，其余照常。"

        filtered = output_filter_service.filter_text_for_memory(FILTERED_CFG, text)

        assert filtered == "这是一段**Filter**内容，其余照常。"
        assert output_filter_service.memory_filter_hit(FILTERED_CFG, text) is True

    def test_whole_mode_does_not_drop_the_paragraph(self) -> None:
        # `OUTPUT_FILTER_MODE=whole` 是出站语义；记忆写入必须保留未命中的部分。
        filtered = output_filter_service.filter_text_for_memory(
            FILTERED_CFG,
            "开头 机密 结尾",
        )

        assert filtered == "开头 **Filter** 结尾"

    def test_clean_text_is_unchanged(self) -> None:
        assert output_filter_service.filter_text_for_memory(FILTERED_CFG, "今天天气不错") == "今天天气不错"
        assert output_filter_service.memory_filter_hit(FILTERED_CFG, "今天天气不错") is False

    def test_before_memory_switch_off_returns_verbatim(self) -> None:
        cfg: Any = SimpleNamespace(**{**vars(FILTERED_CFG), "OUTPUT_FILTER_BEFORE_MEMORY": False})

        assert output_filter_service.filter_text_for_memory(cfg, "机密原文") == "机密原文"
        assert output_filter_service.memory_filter_hit(cfg, "机密原文") is False

    def test_engine_failure_is_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_cfg: Any) -> Any:
            raise RuntimeError("bad rules")

        monkeypatch.setattr(output_filter_service, "_memory_filter_settings", boom)

        assert output_filter_service.filter_text_for_memory(FILTERED_CFG, "原文") == "**Filter**"
        assert output_filter_service.memory_filter_hit(FILTERED_CFG, "原文") is True


async def seed_channel() -> None:
    await DBChatChannel.create(
        chat_key=CHAT,
        adapter_key="onebot_v11",
        instance_key="filter",
        channel_id="1",
        channel_name="filter",
        channel_type="group",
        is_active=True,
        workspace_id=0,
        data="{}",
    )


class TestConsolidatorWrite:
    async def test_paragraph_summary_and_graph_all_filtered(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_channel()

        async def fake_embed(_text: str, **_kwargs: Any) -> List[float]:
            return [0.1, 0.2, 0.3]

        async def fake_upsert(**_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(consolidator_module, "embed_text", fake_embed)
        monkeypatch.setattr(consolidator_module.memory_qdrant_manager, "upsert_paragraph", fake_upsert)
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)

        consolidator = EpisodicConsolidator(0)
        await consolidator._load_channel(CHAT)  # noqa: SLF001
        consolidator.channel_config = FILTERED_CFG

        paragraph, entities, relations = await consolidator._store_memory(  # noqa: SLF001
            {
                "content": "张三把机密文件交给了李四。",
                "summary": "机密文件转交",
                "knowledge_type": "fact",
                "entities": ["机密甲", "李四"],
                "relations": [{"subject": "机密甲", "predicate": "转交", "object": "李四"}],
            },
            CHAT,
            [],
        )

        assert paragraph is not None
        assert paragraph.content == "张三把**Filter**文件交给了李四。"
        assert paragraph.summary == "**Filter**文件转交"
        # 命中过滤的实体不能落库；未命中的「李四」仍保留。
        assert [entity.name for entity, _ in entities] == ["李四"]
        assert relations == []
        assert await DBMemEntity.all().count() == 1
        assert await DBMemRelation.all().count() == 0

    async def test_switch_off_writes_verbatim(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_channel()

        async def fake_embed(_text: str, **_kwargs: Any) -> List[float]:
            return [0.1, 0.2, 0.3]

        async def fake_upsert(**_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(consolidator_module, "embed_text", fake_embed)
        monkeypatch.setattr(consolidator_module.memory_qdrant_manager, "upsert_paragraph", fake_upsert)

        consolidator = EpisodicConsolidator(0)
        await consolidator._load_channel(CHAT)  # noqa: SLF001
        consolidator.channel_config = SimpleNamespace(
            **{**vars(FILTERED_CFG), "OUTPUT_FILTER_BEFORE_MEMORY": False},
        )

        paragraph, _, _ = await consolidator._store_memory(  # noqa: SLF001
            {
                "content": "这句含机密但开关关着。",
                "summary": "机密",
                "knowledge_type": "fact",
                "entities": [],
                "relations": [],
            },
            CHAT,
            [],
        )

        assert paragraph is not None
        assert paragraph.content == "这句含机密但开关关着。"
        assert paragraph.summary == "机密"


class TestChannelSummaryWrite:
    """摘要与段落同一套过滤规则：命中片段替换，两个开关任一关着就逐字保留。"""

    async def _run(self, monkeypatch: pytest.MonkeyPatch, cfg: Any) -> Any:
        await seed_channel()
        await DBChatMessage.create(
            sender_id="uf",
            sender_name="过滤用户",
            sender_nickname="过滤用户",
            is_tome=0,
            is_recalled=False,
            adapter_key="onebot_v11",
            instance_key="filter",
            message_id="mf1",
            chat_key=CHAT,
            chat_type="group",
            platform_userid="uf",
            content_text="一条足够长的测试消息",
            content_data="[]",
            raw_cq_code="",
            ext_data="{}",
            send_timestamp=1_700_000,
        )
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_CONTENT_LENGTH", 0)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 0)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MODEL_GROUP", "filter-primary")
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_FALLBACK_MODEL_GROUP", "filter-primary")
        monkeypatch.setattr(config, "USE_MODEL_GROUP", "filter-primary")

        async def fake_effective_config(_self: DBChatChannel) -> Any:
            return cfg

        async def fake_call(_self: EpisodicConsolidator, _prompt: str, *, use_fallback_model: bool = False) -> str:
            del use_fallback_model
            return json.dumps(
                {
                    "memories": [],
                    "channel_summary": "频道摘要里含机密内容",
                },
                ensure_ascii=False,
            )

        monkeypatch.setattr(DBChatChannel, "get_effective_config", fake_effective_config)
        monkeypatch.setattr(EpisodicConsolidator, "_call_llm", fake_call)

        await EpisodicConsolidator(0).consolidate(CHAT, start_after_db_id=0, persist_progress=False)
        return await get_latest_summary(CHAT, 0)

    async def test_summary_is_filtered_when_both_switches_on(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        latest = await self._run(monkeypatch, FILTERED_CFG)

        assert latest is not None
        assert latest.content == "频道摘要里含**Filter**内容"
        assert await DBMemChannelSummary.all().count() == 1

    @pytest.mark.parametrize(
        "override",
        [{"OUTPUT_FILTER_ENABLE": False}, {"OUTPUT_FILTER_BEFORE_MEMORY": False}],
        ids=["enable-off", "before-memory-off"],
    )
    async def test_summary_is_verbatim_when_either_switch_off(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
        override: dict[str, Any],
    ) -> None:
        cfg: Any = SimpleNamespace(**{**vars(FILTERED_CFG), **override})

        latest = await self._run(monkeypatch, cfg)

        assert latest is not None
        assert latest.content == "频道摘要里含机密内容"
