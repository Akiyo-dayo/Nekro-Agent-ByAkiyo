"""沉淀时的参与者代号、提示词、persons 解析与「涉及的人」落库。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.consolidator import EpisodicConsolidator, PersonaContext
from nekro_agent.services.memory.persons import load_person_accounts
from nekro_agent.services.memory.qdrant_manager import MEMORY_PARAGRAPH_COLLECTION

ADAPTER = "onebot_v11"
INSTANCE = "inst"
BOT_ACCOUNT = "ubot"


@pytest.fixture
async def memory_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_CONTENT_LENGTH", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(config, "OUTPUT_FILTER_ENABLE", False)
    monkeypatch.setattr(config, "OUTPUT_FILTER_BEFORE_MEMORY", False)

    async def fake_embed(_text: str, **_kwargs: object) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]

    async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

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
    await qdrant_manager_module.memory_qdrant_manager.ensure_collection(4)
    try:
        yield client
    finally:
        await client.close()
        await Tortoise.close_connections()


async def _channel(chat_key: str, chat_type: str, workspace_id: int | None = None) -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type=chat_type,
        is_active=True,
        workspace_id=workspace_id,
        data="{}",
    )


async def _message(
    chat_key: str,
    *,
    index: int,
    sender_id: str,
    account: str,
    nickname: str,
    chat_type: str,
    content: str = "一条消息",
) -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=sender_id,
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


def _response(persons: Any) -> str:
    return json.dumps(
        {
            "memories": [
                {
                    "content": "小满和阿青约好周末去看展",
                    "summary": "小满与阿青约看展",
                    "knowledge_type": "decision",
                    "persons": persons,
                    "entities": ["小满", "阿青"],
                    "relations": [],
                },
            ],
        },
        ensure_ascii=False,
    )


def _install_llm(monkeypatch: pytest.MonkeyPatch, response: str, captured: list[str]) -> None:
    async def fake_call_llm(
        _self: EpisodicConsolidator,
        prompt: str,
        *,
        use_fallback_model: bool = False,
    ) -> str:
        del use_fallback_model
        captured.append(prompt)
        return response

    monkeypatch.setattr(EpisodicConsolidator, "_call_llm", fake_call_llm)


async def _payload_of(client: AsyncQdrantClient, paragraph_id: int) -> dict[str, Any]:
    records = await client.retrieve(
        collection_name=MEMORY_PARAGRAPH_COLLECTION,
        ids=[paragraph_id],
        with_payload=True,
    )
    assert len(records) == 1
    return dict(records[0].payload or {})


class TestGroupBatch:
    async def test_codes_in_transcript_prompt_and_persons_written(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-inst-group_1"
        await _channel(chat_key, "group")
        await _message(chat_key, index=1, sender_id="-1", account=BOT_ACCOUNT, nickname="助手", chat_type="group")
        await _message(chat_key, index=2, sender_id="u151", account="u151", nickname="小满", chat_type="group")
        await _message(chat_key, index=3, sender_id="uxw", account="uxw", nickname="阿青", chat_type="group")

        async def fake_load_persona(_self: EpisodicConsolidator, _chat_key: str) -> PersonaContext:
            _self.persona_context = PersonaContext(
                bot_sender_id="-1",
                persona_name="助手",
                persona_brief="AI 助手",
            )
            return _self.persona_context

        monkeypatch.setattr(EpisodicConsolidator, "load_persona_context", fake_load_persona)

        prompts: list[str] = []
        _install_llm(monkeypatch, _response(["P1", "P9", "u151", "p2", "P1", "P2"]), prompts)

        result = await EpisodicConsolidator(0).consolidate(
            chat_key,
            start_after_db_id=0,
            persist_progress=False,
        )

        assert result.paragraphs_created == 1
        prompt = prompts[0]
        assert "〔P1〕" in prompt
        assert "〔P2〕" in prompt
        # Bot 行现在带〔Bot〕标记：闸开时模型靠这个标记而不是昵称区分 Bot 与同名账号
        assert "助手〔Bot〕: 一条消息" in prompt
        assert "助手〔P" not in prompt
        assert '"persons": ["P1", "P3"]' in prompt
        assert "persons：这条记忆涉及的人" in prompt

        paragraphs = await DBMemParagraph.all()
        assert len(paragraphs) == 1
        written = await load_person_accounts([paragraphs[0].id])
        assert written[paragraphs[0].id] == ["u151", "uxw"]
        assert (await _payload_of(memory_env, paragraphs[0].id))["person_accounts"] == ["u151", "uxw"]

    async def test_more_than_ten_codes_are_truncated(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-inst-group_2"
        await _channel(chat_key, "group")
        for index in range(1, 13):
            await _message(
                chat_key,
                index=index,
                sender_id=f"u{index}",
                account=f"u{index}",
                nickname=f"用户{index}",
                chat_type="group",
            )
        prompts: list[str] = []
        _install_llm(monkeypatch, _response([f"P{index}" for index in range(1, 13)]), prompts)

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        paragraphs = await DBMemParagraph.all()
        written = (await load_person_accounts([paragraphs[0].id]))[paragraphs[0].id]
        assert len(written) == 10
        assert set(written) == {f"u{index}" for index in range(1, 11)}
        payload = await _payload_of(memory_env, paragraphs[0].id)
        assert payload["person_accounts"] == sorted(written)

    async def test_non_list_persons_is_empty(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-inst-group_3"
        await _channel(chat_key, "group")
        await _message(chat_key, index=1, sender_id="u151", account="u151", nickname="小满", chat_type="group")
        prompts: list[str] = []
        _install_llm(monkeypatch, _response("P1"), prompts)

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        paragraphs = await DBMemParagraph.all()
        assert await DBMemParagraphPerson.all().count() == 0
        assert (await _payload_of(memory_env, paragraphs[0].id))["person_accounts"] == []


class TestPrivateBatch:
    async def test_counterpart_is_written_and_model_persons_ignored(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-inst-private_1"
        await _channel(chat_key, "private")
        await _message(chat_key, index=1, sender_id="uxw", account="uxw", nickname="阿青", chat_type="private")
        await _message(chat_key, index=2, sender_id=BOT_ACCOUNT, account=BOT_ACCOUNT, nickname="阿青", chat_type="private")
        prompts: list[str] = []
        _install_llm(monkeypatch, _response(["P1", "P2"]), prompts)

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert '"persons"' not in prompts[0]
        paragraphs = await DBMemParagraph.all()
        written = await load_person_accounts([paragraphs[0].id])
        assert written[paragraphs[0].id] == ["uxw"]
        assert (await _payload_of(memory_env, paragraphs[0].id))["person_accounts"] == ["uxw"]

    async def test_counterpart_unknown_writes_nobody(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-inst-private_2"
        await _channel(chat_key, "private")
        prompts: list[str] = []
        _install_llm(monkeypatch, _response(["P1"]), prompts)
        consolidator = EpisodicConsolidator(0)
        await consolidator._load_channel(chat_key)  # noqa: SLF001

        paragraph, _, _ = await consolidator._store_memory(  # noqa: SLF001
            {
                "content": "私聊里没有可确定的对象",
                "summary": "没有对象",
                "knowledge_type": "fact",
                "persons": ["P1"],
                "entities": [],
                "relations": [],
            },
            chat_key,
            [],
        )

        assert paragraph is not None
        assert await DBMemParagraphPerson.all().count() == 0
        assert (await _payload_of(memory_env, paragraph.id))["person_accounts"] == []


class TestGateOff:
    async def test_transcript_and_prompt_are_byte_identical(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        messages = [
            DBChatMessage(
                sender_id="-1",
                sender_nickname="助手",
                sender_name="助手",
                platform_userid="ubot",
                content_text="我会记下来",
                send_timestamp=1_700_000,
            ),
            DBChatMessage(
                sender_id="u151",
                sender_nickname="小满",
                sender_name="小满",
                platform_userid="u151",
                content_text="周末去看展",
                send_timestamp=1_700_060,
            ),
            DBChatMessage(
                sender_id="uxw",
                sender_nickname="阿青",
                sender_name="阿青",
                platform_userid="uxw",
                content_text="好呀",
                send_timestamp=1_700_120,
            ),
        ]
        consolidator = EpisodicConsolidator(1)
        consolidator.origin_chat_type = "group"
        consolidator.persona_context = PersonaContext(
            bot_sender_id="-1",
            persona_name="助手",
            persona_brief="AI 助手",
        )

        assert consolidator._build_conversation_text(messages) == _frozen_conversation_text(messages)  # noqa: SLF001

        prompts: list[str] = []
        _install_llm(monkeypatch, json.dumps({"memories": []}, ensure_ascii=False), prompts)
        parsed = await consolidator._extract_memories(messages)  # noqa: SLF001
        assert parsed.memories == []
        assert prompts[0] == _frozen_prompt(_frozen_conversation_text(messages))

    async def test_store_writes_no_person_rows(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        chat_key = "onebot_v11-inst-group_off"
        await _channel(chat_key, "group", workspace_id=1)
        consolidator = EpisodicConsolidator(1)
        await consolidator._load_channel(chat_key)  # noqa: SLF001
        message = DBChatMessage(
            sender_id="u151",
            sender_nickname="小满",
            sender_name="小满",
            platform_userid="u151",
            content_text="内容",
            send_timestamp=1_700_000,
        )

        paragraph, _, _ = await consolidator._store_memory(  # noqa: SLF001
            {
                "content": "闸关时的一条记忆",
                "summary": "闸关",
                "knowledge_type": "fact",
                "persons": ["P1"],
                "entities": [],
                "relations": [],
            },
            chat_key,
            [message],
        )

        assert paragraph is not None
        assert await DBMemParagraphPerson.all().count() == 0
        assert (await _payload_of(memory_env, paragraph.id))["person_accounts"] == []


class TestWorkspaceRow:
    async def test_workspace_row_writes_no_person_rows(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-inst-group_ws"
        await _channel(chat_key, "group", workspace_id=1)
        consolidator = EpisodicConsolidator(1)
        await consolidator._load_channel(chat_key)  # noqa: SLF001
        message = DBChatMessage(
            sender_id="u151",
            sender_nickname="小满",
            sender_name="小满",
            platform_userid="u151",
            content_text="内容",
            send_timestamp=1_700_000,
        )

        paragraph, _, _ = await consolidator._store_memory(  # noqa: SLF001
            {
                "content": "工作区行的一条记忆",
                "summary": "工作区行",
                "knowledge_type": "fact",
                "persons": ["P1"],
                "entities": [],
                "relations": [],
            },
            chat_key,
            [message],
        )

        assert paragraph is not None
        assert paragraph.workspace_id == 1
        assert await DBMemParagraphPerson.all().count() == 0
        assert (await _payload_of(memory_env, paragraph.id))["person_accounts"] == []


def _frozen_conversation_text(messages: list[DBChatMessage]) -> str:
    """改动前的对话文本拼法，冻结在这里做逐字对照"""
    lines = []
    for msg in messages:
        time_str = datetime.fromtimestamp(msg.send_timestamp).strftime("%H:%M")
        sender = msg.sender_nickname or msg.sender_name or f"User_{msg.sender_id}"
        if msg.sender_id == "-1":
            sender = "助手"
        content = msg.content_text[:500]
        lines.append(f"[{time_str}] {sender}: {content}")
    return "\n".join(lines)


def _frozen_prompt(conversation_text: str) -> str:
    """改动前的提示词拼法，冻结在这里做逐字对照"""
    persona_hint = "\n注意：对话中 sender_id 为 -1 的消息是 '助手' （AI 助手）发送的，请在记忆中使用该名称而非 'AI' 或 '机器人'。"
    return f"""请分析以下对话，提取值得记住的重要信息。

要求：
1. 使用第三人称视角描述
2. 只提取有价值的信息（事实、偏好、重要讨论、决策等）
3. 过滤掉闲聊和无意义内容
4. 每条记忆应该简洁但完整{persona_hint}

对话内容：
{conversation_text}

请以 JSON 格式返回，格式如下：
```json
{{
  "memories": [
    {{
      "content": "第三人称的记忆描述，必须保留关键对象、行为、结论，不要写成过短标签",
      "summary": "可读性摘要（建议 20-40 字，必须能独立看懂）",
      "knowledge_type": "conversation|preference|fact|experience|decision|emotion",
      "entities": ["涉及的人名或概念"],
      "relations": [
        {{
          "subject": "实体A",
          "predicate": "喜欢|使用|创建|提到|讨论|属于",
          "object": "实体B"
        }}
      ]
    }}
  ]
}}
```

额外要求：
1. 不要输出“某人提到某事”“讨论了某话题”这种空泛句，必须带上具体内容。
2. 优先保留可复用的信息：能力、偏好、决策、结论、承诺、计划、问题定位、技术事实。
3. summary 不能只是项目名或动词短语，单独展示时必须有参考价值。
4. 如果多条信息属于同一事件，优先合并成一条更完整的记忆，而不是切成多个碎片。

如果没有值得记住的内容，返回空数组：{{"memories": []}}"""
