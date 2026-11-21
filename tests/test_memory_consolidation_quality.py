"""闸开路径的沉淀质量：Bot 台词、近重复、账号号码。

真机试看里人工看过全部段落，三类问题最刺眼：把 Bot 自己的角色扮演台词与自述当成事实记下来、
同一件事记了两遍、正文里写着账号号码。三类问题都只在闸开时处理，闸关路径逐字不变。

嵌入故意按文本区分：同一段文本给同一个向量，不同文本给正交向量。恒定向量会把「近重复」
测试变成「所有段落都重复」，反而验证不了阈值这一层。
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Callable

import pytest
from qdrant_client import AsyncQdrantClient
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.channel_summary import get_latest_summary
from nekro_agent.services.memory.consolidator import BOT_SAID_PREFIX, EpisodicConsolidator, PersonaContext
from nekro_agent.services.memory.persons import load_person_accounts

ADAPTER = "onebot_v11"
INSTANCE = "quality"
BOT_ACCOUNT = "7000001"
XIAOMAN = "1234567"
PASSERBY = "7654321"
YOUTH = "2345678"
NUMERIC_NICK = "3456789"

# 嵌入维度：8 个正交方向够用，用例里的文本种类远少于这个数
EMBED_DIMENSION = 8


@pytest.fixture
async def memory_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncQdrantClient]:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_ENABLED", False)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_CONTENT_LENGTH", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_DEDUP_SIMILARITY", 0.92)
    monkeypatch.setattr(config, "OUTPUT_FILTER_ENABLE", False)
    monkeypatch.setattr(config, "OUTPUT_FILTER_BEFORE_MEMORY", False)

    vectors: dict[str, list[float]] = {}

    def vector_for(text: str) -> list[float]:
        if text not in vectors:
            index = len(vectors) % EMBED_DIMENSION
            vector = [0.0] * EMBED_DIMENSION
            vector[index] = 1.0
            vectors[text] = vector
        return vectors[text]

    async def fake_embed(text: str, **_kwargs: object) -> list[float]:
        return vector_for(text)

    async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [vector_for(text) for text in texts]

    monkeypatch.setattr(consolidator_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_text", fake_embed)
    monkeypatch.setattr(embedding_service_module, "embed_batch", fake_embed_batch)
    monkeypatch.setattr(qdrant_manager_module, "embed_batch", fake_embed_batch)

    client = AsyncQdrantClient(location=":memory:")

    async def fake_get_client() -> AsyncQdrantClient:
        return client

    monkeypatch.setattr(qdrant_manager_module, "get_qdrant_client", fake_get_client)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        provider_account_id=BOT_ACCOUNT,
    )
    await qdrant_manager_module.memory_qdrant_manager.ensure_collection(EMBED_DIMENSION)
    try:
        yield client
    finally:
        await client.close()
        await Tortoise.close_connections()


async def _preset(name: str) -> DBPreset:
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content=name,
        description=name,
        tags="",
        author="test",
    )


async def _channel(
    chat_key: str,
    *,
    chat_type: str = "group",
    preset_id: int = 0,
    workspace_id: int | None = None,
) -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type=chat_type,
        is_active=True,
        workspace_id=workspace_id,
        preset_id=preset_id or None,
        data="{}",
    )


async def _message(
    chat_key: str,
    *,
    index: int,
    account: str,
    nickname: str,
    chat_type: str = "group",
    content: str = "一条消息",
    sender_id: str | None = None,
) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=sender_id or account,
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        message_id=f"m{index}",
        chat_key=chat_key,
        chat_type=chat_type,
        platform_userid=account,
        content_text=content,
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=1_700_000 + index * 60,
    )


def _install_persona(monkeypatch: pytest.MonkeyPatch, name: str = "助手") -> None:
    async def fake_load_persona(_self: EpisodicConsolidator, _chat_key: str) -> PersonaContext:
        _self.persona_context = PersonaContext(
            bot_sender_id="-1",
            persona_name=name,
            persona_brief="AI 助手",
        )
        assert _self.persona_context is not None
        return _self.persona_context

    monkeypatch.setattr(EpisodicConsolidator, "load_persona_context", fake_load_persona)


def _memories(*items: dict[str, Any]) -> str:
    return json.dumps({"memories": list(items)}, ensure_ascii=False)


def _install_llm(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[str], str],
) -> list[str]:
    prompts: list[str] = []

    async def fake_call(_self: EpisodicConsolidator, prompt: str, *, use_fallback_model: bool = False) -> str:
        del use_fallback_model
        prompts.append(prompt)
        return handler(prompt)

    monkeypatch.setattr(EpisodicConsolidator, "_call_llm", fake_call)
    return prompts


async def _run(
    chat_key: str,
    monkeypatch: pytest.MonkeyPatch,
    *items: dict[str, Any],
    channel_summary: str | None = None,
) -> Any:
    payload: dict[str, Any] = {"memories": list(items)}
    if channel_summary is not None:
        payload["channel_summary"] = channel_summary
    _install_llm(monkeypatch, lambda _p: json.dumps(payload, ensure_ascii=False))
    return await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)


def _item(content: str, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "content": content,
        "summary": content[:40],
        "knowledge_type": "fact",
        "entities": [],
        "relations": [],
    }
    item.update(extra)
    return item


class TestGateOpenConversationText:
    async def test_bot_lines_codes_and_accounts_are_readable(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-quality-group_1"
        await _channel(chat_key)
        _install_persona(monkeypatch)
        await _message(
            chat_key,
            index=1,
            account=BOT_ACCOUNT,
            nickname="助手",
            sender_id="-1",
            content="我会把配方记下来",
        )
        await _message(
            chat_key,
            index=2,
            account=XIAOMAN,
            nickname="小满",
            content=f"[@id:{XIAOMAN};nickname:小满@] 说周末见",
        )
        # 昵称为空：旧的兜底写法会把账号号码写进对话文本
        await _message(chat_key, index=3, account=YOUTH, nickname="", content="我也去")
        await _message(
            chat_key,
            index=4,
            account=YOUTH,
            nickname="",
            content=(
                f"顺便[@id:{PASSERBY};nickname:路人@]也来，别加 {XIAOMAN} 这个号，"
                f"[@id:{BOT_ACCOUNT};nickname:助手@] 你说呢"
            ),
        )
        # 昵称就是号码本身：原样写出来同样等于把账号写进对话文本
        await _message(chat_key, index=5, account=NUMERIC_NICK, nickname=NUMERIC_NICK, content="算我一个")

        consolidator = EpisodicConsolidator(0)
        await consolidator._load_channel(chat_key)  # noqa: SLF001
        consolidator.persona_context = PersonaContext(bot_sender_id="-1", persona_name="助手", persona_brief="AI 助手")
        messages = list(await DBChatMessage.filter(chat_key=chat_key).order_by("id"))
        await consolidator._prepare_batch_participants(messages)  # noqa: SLF001
        text = consolidator._build_conversation_text(messages)  # noqa: SLF001

        assert "助手〔Bot〕: 我会把配方记下来" in text
        assert "小满〔P1〕: @小满〔P1〕 说周末见" in text
        assert "群友〔P2〕: 我也去" in text
        assert "@路人" in text
        assert "路人〔" not in text
        assert "@助手〔Bot〕" in text
        assert "某群友〔P3〕: 算我一个" in text
        assert "User_" not in text
        for account in (XIAOMAN, PASSERBY, YOUTH, BOT_ACCOUNT, NUMERIC_NICK):
            assert account not in text

        prompts = _install_llm(monkeypatch, lambda _p: _memories())
        await consolidator._extract_memories(messages)  # noqa: SLF001
        prompt = prompts[0]

        # 整份提示词里不该出现任何一个测试账号
        for account in (XIAOMAN, PASSERBY, YOUTH, BOT_ACCOUNT, NUMERIC_NICK):
            assert account not in prompt


class TestGateOpenPrompt:
    async def test_group_prompt_has_bot_and_account_rules(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-quality-group_2"
        await _channel(chat_key)
        _install_persona(monkeypatch)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末去看展吗")
        prompts = _install_llm(monkeypatch, lambda _p: _memories())

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        prompt = prompts[0]
        assert '"bot_said": false' in prompt
        assert "Bot 自己说的话（带〔Bot〕标记的行" in prompt
        assert "bot_said 设为 true" in prompt
        assert "里都不写任何账号号码" in prompt
        assert "P1 这类代号只写在 persons 里" in prompt
        assert "带〔Bot〕标记的行是 Bot「助手」" in prompt

    @pytest.mark.parametrize(("chat_type", "summary_on"), [("group", True), ("group", False), ("private", True), ("private", False)])
    async def test_extra_requirements_are_numbered_consecutively(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
        chat_type: str,
        summary_on: bool,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_ENABLED", summary_on)
        chat_key = f"onebot_v11-quality-{chat_type}_num"
        await _channel(chat_key, chat_type=chat_type)
        _install_persona(monkeypatch)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末去看展吗", chat_type=chat_type)
        prompts = _install_llm(monkeypatch, lambda _p: _memories())

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        prompt = prompts[0]
        extra = prompt.split("额外要求：", 1)[1]
        # 一条要求占一行：行首是编号、编号后是句点
        numbers = [int(match.group(1)) for match in re.finditer(r"^(\d+)\. ", extra, re.MULTILINE)]
        assert numbers == list(range(1, len(numbers) + 1))
        expected = 4 + 2 + 1 + (1 if chat_type == "group" else 0) + (1 if summary_on else 0)
        assert len(numbers) == expected

        has_persons = "persons：这条记忆涉及的人" in prompt
        assert has_persons is (chat_type == "group")
        assert ("P1 这类代号只写在 persons 里" in prompt) is (chat_type == "group")
        assert ('"persons"' in prompt) is (chat_type == "group")
        assert ("channel_summary：" in prompt) is summary_on
        assert ('"channel_summary"' in prompt) is summary_on


class TestGateOffStillFrozen:
    async def test_transcript_keeps_raw_markup_and_no_bot_marker(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        marker = f"[@id:{XIAOMAN};nickname:小满@]"
        messages = [
            DBChatMessage(
                sender_id="-1",
                sender_nickname="助手",
                sender_name="助手",
                platform_userid=BOT_ACCOUNT,
                content_text="我会记下来",
                send_timestamp=1_700_000,
            ),
            DBChatMessage(
                sender_id=XIAOMAN,
                sender_nickname="小满",
                sender_name="小满",
                platform_userid=XIAOMAN,
                content_text=f"{marker} 周末去看展",
                send_timestamp=1_700_060,
            ),
        ]
        consolidator = EpisodicConsolidator(1)
        consolidator.origin_chat_type = "group"
        consolidator.persona_context = PersonaContext(bot_sender_id="-1", persona_name="助手", persona_brief="AI 助手")
        prompts = _install_llm(monkeypatch, lambda _p: _memories())

        text = consolidator._build_conversation_text(messages)  # noqa: SLF001
        await consolidator._extract_memories(messages)  # noqa: SLF001

        assert marker in text
        assert "〔Bot〕" not in text
        assert "助手: 我会记下来" in text
        assert '"bot_said"' not in prompts[0]
        assert "Bot 自己说的话" not in prompts[0]


class TestStorageScrub:
    async def test_accounts_codes_and_markers_are_replaced_before_storage(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-quality-group_3"
        await _channel(chat_key)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        await _message(chat_key, index=2, account=YOUTH, nickname="阿青", content="好呀")
        prompts = _install_llm(
            monkeypatch,
            lambda _p: _memories(
                {
                    "content": f"小满（{XIAOMAN}）和 P2 约好周末去看展，〔P1〕先到",
                    "summary": f"{XIAOMAN} 与 P2 约看展",
                    "knowledge_type": "decision",
                    "persons": ["P1", "P2"],
                    "entities": [XIAOMAN, "看展"],
                    "relations": [{"subject": XIAOMAN, "predicate": "约", "object": "P2"}],
                },
            ),
        )

        result = await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert prompts  # 确认真的调过模型
        assert result.paragraphs_created == 1
        paragraph = await DBMemParagraph.first()
        assert paragraph is not None
        assert XIAOMAN not in paragraph.content
        assert XIAOMAN not in paragraph.summary
        assert "P2" not in paragraph.content
        assert "〔P1〕" not in paragraph.content
        # 标记整个去掉，而不是只把里面的代号换成名字（那样会留下「〔小满〕」）
        assert "〔" not in paragraph.content
        assert "小满" in paragraph.content
        assert "阿青" in paragraph.content

        entity_names = set(await DBMemEntity.all().values_list("name", flat=True))
        assert XIAOMAN not in entity_names
        relation_ends: list[str] = []
        for relation in await DBMemRelation.all():
            subject = await DBMemEntity.get(id=relation.subject_entity_id)
            obj = await DBMemEntity.get(id=relation.object_entity_id)
            relation_ends.extend([subject.name, obj.name])
        assert XIAOMAN not in relation_ends
        assert "P2" not in relation_ends
        assert "阿青" in relation_ends

        written = await load_person_accounts([paragraph.id])
        assert written[paragraph.id] == sorted([XIAOMAN, YOUTH])


class TestBotSaidPrefix:
    @pytest.mark.parametrize(
        ("flag", "expected"),
        [(True, True), (False, False), (None, False), ("true", False)],
    )
    async def test_prefix_only_for_literal_true(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
        flag: Any,
        expected: bool,
    ) -> None:
        chat_key = f"onebot_v11-quality-group_bot_{flag}"
        await _channel(chat_key)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        item: dict[str, Any] = {
            "content": "助手答应周末一起去看展",
            "summary": "助手答应了看展",
            "knowledge_type": "decision",
            "entities": [],
            "relations": [],
        }
        if flag is not None:
            item["bot_said"] = flag

        await _run(chat_key, monkeypatch, item)

        paragraph = await DBMemParagraph.get(origin_chat_key=chat_key)
        if expected:
            assert paragraph.content.startswith(BOT_SAID_PREFIX)
            assert paragraph.summary.startswith(BOT_SAID_PREFIX)
        else:
            assert not paragraph.content.startswith(BOT_SAID_PREFIX)
            assert not paragraph.summary.startswith(BOT_SAID_PREFIX)


class TestNearDuplicate:
    async def test_identical_content_in_next_batch_is_skipped(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-quality-group_4"
        await _channel(chat_key)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        await _run(chat_key, monkeypatch, _item("小满和阿青约好周末去看展"))
        # 第二批换消息区间，锚点不同，只有向量相似度能认出来
        await _message(chat_key, index=2, account=XIAOMAN, nickname="小满", content="再确认一次")
        result = await _run(chat_key, monkeypatch, _item("小满和阿青约好周末去看展"))

        assert result.paragraphs_created == 0
        assert await DBMemParagraph.all().count() == 1

    async def test_different_content_is_written(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-quality-group_5"
        await _channel(chat_key)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        await _run(chat_key, monkeypatch, _item("小满和阿青约好周末去看展"))
        await _message(chat_key, index=2, account=XIAOMAN, nickname="小满", content="再聊点别的")
        result = await _run(chat_key, monkeypatch, _item("小满换了新键盘"))

        assert result.paragraphs_created == 1
        assert await DBMemParagraph.all().count() == 2

    async def test_same_content_twice_in_one_batch_writes_one(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 两条措辞不同、意思相同：内容前缀不同，躲过锚点 + 前缀的崩溃恢复去重，
        # 只有向量相似度能认出来。这里让两种说法拿到同一个向量。
        async def same_vector(_text: str, **_kwargs: object) -> list[float]:
            return [1.0] + [0.0] * (EMBED_DIMENSION - 1)

        monkeypatch.setattr(consolidator_module, "embed_text", same_vector)
        chat_key = "onebot_v11-quality-group_6"
        await _channel(chat_key)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        result = await _run(
            chat_key,
            monkeypatch,
            _item("小满和阿青约好周末去看展"),
            _item("阿青和小满说好了周末一起看展"),
        )

        assert result.paragraphs_created == 1
        assert await DBMemParagraph.all().count() == 1

    async def test_other_preset_or_channel_is_written(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset_one = await _preset("人设一")
        preset_two = await _preset("人设二")
        chat_a = "onebot_v11-quality-group_7"
        chat_b = "onebot_v11-quality-group_8"
        await _channel(chat_a, preset_id=preset_one.id)
        await _channel(chat_b, preset_id=preset_two.id)
        await _message(chat_a, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        await _message(chat_b, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        await _run(chat_a, monkeypatch, _item("小满和阿青约好周末去看展"))

        other_channel = await _run(chat_b, monkeypatch, _item("小满和阿青约好周末去看展"))

        channel = await DBChatChannel.get(chat_key=chat_a)
        channel.preset_id = preset_two.id
        await channel.save(update_fields=["preset_id"])
        await _message(chat_a, index=2, account=XIAOMAN, nickname="小满", content="换个说法")
        other_preset = await _run(chat_a, monkeypatch, _item("小满和阿青约好周末去看展"))

        assert other_channel.paragraphs_created == 1
        assert other_preset.paragraphs_created == 1
        assert await DBMemParagraph.all().count() == 3

    async def test_inactive_paragraph_is_not_a_duplicate(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-quality-group_9"
        await _channel(chat_key)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        await _run(chat_key, monkeypatch, _item("小满和阿青约好周末去看展"))
        first = await DBMemParagraph.first()
        assert first is not None
        first.is_inactive = True
        await first.save(update_fields=["is_inactive"])
        await _message(chat_key, index=2, account=XIAOMAN, nickname="小满", content="再确认一次")

        result = await _run(chat_key, monkeypatch, _item("小满和阿青约好周末去看展"))

        assert result.paragraphs_created == 1
        assert await DBMemParagraph.all().count() == 2

    async def test_zero_threshold_disables_dedup(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_DEDUP_SIMILARITY", 0.0)
        chat_key = "onebot_v11-quality-group_10"
        await _channel(chat_key)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        await _run(chat_key, monkeypatch, _item("小满和阿青约好周末去看展"))
        await _message(chat_key, index=2, account=XIAOMAN, nickname="小满", content="再确认一次")

        result = await _run(chat_key, monkeypatch, _item("小满和阿青约好周末去看展"))

        assert result.paragraphs_created == 1
        assert await DBMemParagraph.all().count() == 2

    async def test_qdrant_failure_keeps_the_memory(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("qdrant 挂了")

        monkeypatch.setattr(
            qdrant_manager_module.memory_qdrant_manager,
            "most_similar_paragraph",
            boom,
        )
        chat_key = "onebot_v11-quality-group_11"
        await _channel(chat_key)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")

        result = await _run(chat_key, monkeypatch, _item("小满和阿青约好周末去看展"))

        assert result.paragraphs_created == 1
        assert await DBMemParagraph.all().count() == 1


class TestGateOffUntouched:
    async def test_accounts_and_bot_said_are_stored_as_is(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        chat_key = "onebot_v11-quality-group_off"
        # 闸关的沉淀只写工作区行，频道必须绑工作区
        await _channel(chat_key, preset_id=(await _preset("闸关人设")).id, workspace_id=1)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        item = _item(f"小满（{XIAOMAN}）约好周末去看展", bot_said=True)

        first = await _run(chat_key, monkeypatch, item)
        await _message(chat_key, index=2, account=XIAOMAN, nickname="小满", content="再确认一次")
        second = await _run(chat_key, monkeypatch, item)

        rows = await DBMemParagraph.all()
        assert len(rows) == 2
        assert first.paragraphs_created == 1
        assert second.paragraphs_created == 1
        for row in rows:
            assert XIAOMAN in row.content
            assert not row.content.startswith(BOT_SAID_PREFIX)
            assert not row.summary.startswith(BOT_SAID_PREFIX)
        assert await DBMemParagraphPerson.all().count() == 0


class TestChannelSummaryScrub:
    async def test_summary_accounts_are_replaced(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_ENABLED", True)
        chat_key = "onebot_v11-quality-group_12"
        await _channel(chat_key)
        await _message(chat_key, index=1, account=XIAOMAN, nickname="小满", content="周末见")
        await _message(chat_key, index=2, account=YOUTH, nickname="阿青", content="我也去")

        await _run(
            chat_key,
            monkeypatch,
            _item("小满约阿青周末看展"),
            channel_summary=f"最近 {XIAOMAN} 在约大家周末去看展，{YOUTH} 也答应了",
        )

        latest = await get_latest_summary(chat_key, 0)
        assert latest is not None
        assert XIAOMAN not in latest.content
        assert YOUTH not in latest.content
        assert "小满" in latest.content
