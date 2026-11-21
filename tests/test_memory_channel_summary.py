"""频道滚动摘要：写入、保留版本、注入与开关。

摘要与记忆来自同一次沉淀调用：模型多返回一个 `channel_summary` 字段，宿主只收非空字符串，
缺字段或空值保留上一版。摘要只按 `chat_key + 人设` 归属，只在本频道注入，且与记忆块共用
同一道「上一轮被拦」抑制判据。
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import openai
import pytest
from qdrant_client import AsyncQdrantClient
from tortoise import Tortoise

from nekro_agent.core.config import ModelConfigGroup, config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_channel_summary_state import DBMemChannelSummaryState
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.services.affection.suppression import (
    lift_narrative_suppression,
    reset_narrative_suppression,
    suppress_narrative,
)
from nekro_agent.services.agent.openai import LLMContentBlockedError
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.channel_summary import (
    SUMMARY_KEEP_VERSIONS,
    get_latest_summary,
    is_paused,
    save_summary,
)
from nekro_agent.services.memory.consolidator import EpisodicConsolidator

ADAPTER = "onebot_v11"
INSTANCE = "suminst"
BOT_ACCOUNT = "ubot"


@pytest.fixture
async def memory_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_MAX_CHARS", 400)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_CONTENT_LENGTH", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MODEL_GROUP", "mem-primary")
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_FALLBACK_MODEL_GROUP", "mem-primary")
    monkeypatch.setattr(config, "USE_MODEL_GROUP", "mem-primary")
    monkeypatch.setattr(
        config,
        "MODEL_GROUPS",
        {"mem-primary": ModelConfigGroup(GROUP_NAME="mem-primary", CHAT_MODEL="mem-model")},
    )
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
    reset_narrative_suppression()
    try:
        yield client
    finally:
        reset_narrative_suppression()
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


async def _channel(chat_key: str, *, chat_type: str = "group", preset_id: int = 0) -> DBChatChannel:
    return await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type=chat_type,
        is_active=True,
        workspace_id=None,
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
        content_text=f"消息 {index}",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=1_700_000 + index * 60,
    )


def _response(*, channel_summary: Any = None, include_summary: bool = True, memories: list[dict[str, Any]] | None = None) -> str:
    payload: dict[str, Any] = {
        "memories": memories
        if memories is not None
        else [
            {
                "content": "小满和阿青约好周末去看展",
                "summary": "小满与阿青约看展",
                "knowledge_type": "decision",
                "persons": ["P1"],
                "entities": ["小满", "阿青"],
                "relations": [],
            },
        ],
    }
    if include_summary:
        payload["channel_summary"] = channel_summary
    return json.dumps(payload, ensure_ascii=False)


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


async def _seed_batch(chat_key: str, *, chat_type: str = "group", preset_id: int = 0) -> DBChatChannel:
    channel = await _channel(chat_key, chat_type=chat_type, preset_id=preset_id)
    await _message(chat_key, index=1, account="ua", nickname="小满", chat_type=chat_type, sender_id="-1" if chat_type == "group" else "ua")
    await _message(chat_key, index=2, account="ub", nickname="阿青", chat_type=chat_type)
    return channel


class TestSummaryWrite:
    async def test_one_call_writes_paragraph_person_and_summary(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-suminst-group_1"
        await _seed_batch(chat_key)
        prompts = _install_llm(monkeypatch, lambda _p: _response(channel_summary="最近在约周末看展，阿青和小满约好一起去。"))

        result = await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert len(prompts) == 1
        assert result.paragraphs_created == 1
        assert await DBMemParagraph.all().count() == 1
        assert await DBMemParagraphPerson.all().count() == 1
        latest = await get_latest_summary(chat_key, 0)
        assert latest is not None
        assert latest.content == "最近在约周末看展，阿青和小满约好一起去。"
        assert latest.last_message_db_id == 2
        assert latest.adapter_key == ADAPTER
        assert latest.instance_key == INSTANCE

    async def test_summary_only_appears_in_its_own_channel_prompt(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_RECALL_GROUP_TO_GROUP", "all")
        monkeypatch.setattr(config, "MEMORY_RECALL_GROUP_TO_PRIVATE", "all")
        monkeypatch.setattr(config, "MEMORY_RECALL_PRIVATE_TO_GROUP", "all")
        channel_a = "onebot_v11-suminst-group_2"
        channel_b = "onebot_v11-suminst-group_3"
        await _channel(channel_a)
        await save_summary(
            chat_key=channel_a,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="频道A的独有暗号",
            last_message_db_id=1,
        )
        await _seed_batch(channel_b)
        prompts = _install_llm(monkeypatch, lambda _p: _response(channel_summary="频道B自己的近况"))

        await EpisodicConsolidator(0).consolidate(channel_b, start_after_db_id=0, persist_progress=False)

        assert "频道A的独有暗号" not in prompts[0]
        assert "频道B自己的近况" not in prompts[0]

    async def test_separate_presets_keep_separate_summaries(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        preset_one = await _preset("人设一")
        preset_two = await _preset("人设二")
        chat_key = "onebot_v11-suminst-group_4"
        channel = await _seed_batch(chat_key, preset_id=preset_one.id)
        _install_llm(monkeypatch, lambda _p: _response(channel_summary="人设一的摘要"))

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)
        channel.preset_id = preset_two.id
        await channel.save(update_fields=["preset_id"])
        _install_llm(monkeypatch, lambda _p: _response(channel_summary="人设二的摘要"))
        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        first = await get_latest_summary(chat_key, preset_one.id)
        second = await get_latest_summary(chat_key, preset_two.id)
        assert first is not None and first.content == "人设一的摘要"
        assert second is not None and second.content == "人设二的摘要"

    async def test_invalid_or_missing_summary_keeps_previous(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-suminst-group_5"
        await _seed_batch(chat_key)
        await save_summary(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="上一版摘要",
            last_message_db_id=1,
        )
        responses = [
            _response(include_summary=False),
            _response(channel_summary=""),
            _response(channel_summary="   "),
            _response(channel_summary=123),
            _response(channel_summary=["不是字符串"]),
        ]

        for response in responses:
            _install_llm(monkeypatch, lambda _p, value=response: value)
            await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        latest = await get_latest_summary(chat_key, 0)
        assert latest is not None
        assert latest.content == "上一版摘要"
        assert await DBMemChannelSummary.all().count() == 1

    async def test_long_summary_is_truncated_to_limit(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CHANNEL_SUMMARY_MAX_CHARS", 120)
        chat_key = "onebot_v11-suminst-group_6"
        await _seed_batch(chat_key)
        _install_llm(monkeypatch, lambda _p: _response(channel_summary="长" * 200))

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        latest = await get_latest_summary(chat_key, 0)
        assert latest is not None
        assert len(latest.content) == 121
        assert latest.content.endswith("…")

    async def test_blocked_or_failed_batch_keeps_summary(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-suminst-group_7"
        await _seed_batch(chat_key)
        await save_summary(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="不该被改动的摘要",
            last_message_db_id=1,
        )

        def blocked(_prompt: str) -> str:
            raise LLMContentBlockedError("safety", stream_mode=False)

        _install_llm(monkeypatch, blocked)
        skipped = await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)
        assert skipped.skipped_reason == "content_blocked"

        def failed(_prompt: str) -> str:
            raise openai.APIConnectionError(request=httpx.Request("POST", "http://x"))

        _install_llm(monkeypatch, failed)
        failed_result = await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)
        assert failed_result.failed_reason == "timeout"

        latest = await get_latest_summary(chat_key, 0)
        assert latest is not None and latest.content == "不该被改动的摘要"

    async def test_disabled_summary_switch_neither_reads_writes_nor_injects(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-suminst-group_8"
        channel = await _seed_batch(chat_key)
        await save_summary(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="开关关着时不该出现的摘要",
            last_message_db_id=1,
        )

        # 全局开关保持打开，只把「本频道生效值」关掉：读配置的路径必须走频道生效值，
        # 不能绕过它直接看全局开关。
        async def disabled_effective_config(_self: DBChatChannel) -> Any:
            return SimpleNamespace(
                MEMORY_ENABLE_FOR_CHAT=True,
                MEMORY_CHANNEL_SUMMARY_ENABLED=False,
                MEMORY_CHANNEL_SUMMARY_MAX_CHARS=400,
            )

        monkeypatch.setattr(DBChatChannel, "get_effective_config", disabled_effective_config)
        prompts = _install_llm(monkeypatch, lambda _p: _response(channel_summary="新摘要不该写入"))

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert "开关关着时不该出现的摘要" not in prompts[0]
        assert "PreviousChannelSummary" not in prompts[0]
        assert '"channel_summary"' not in prompts[0]
        assert await DBMemChannelSummary.all().count() == 1
        assert await history_module._inject_channel_summary(channel, "code-sum") == ""  # noqa: SLF001


class _LogRecorder:
    """顶替沉淀器的 logger，按级别记下每一行"""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def __getattr__(self, level: str) -> Callable[..., None]:
        def record(message: object, *_args: object, **_kwargs: object) -> None:
            self.lines.append((level, str(message)))

        return record

    def info_lines(self, prefix: str) -> list[str]:
        return [message for level, message in self.lines if level == "info" and message.startswith(prefix)]


class TestBatchOutcomeLog:
    """每批一行 INFO：分清「模型返回了空列表」与「返回了条目但缺字段被丢掉」，并带上摘要结果"""

    async def test_empty_list_and_dropped_items_are_distinguishable(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorder = _LogRecorder()
        monkeypatch.setattr(consolidator_module, "logger", recorder)

        empty_key = "onebot_v11-suminst-group_31"
        await _seed_batch(empty_key)
        _install_llm(monkeypatch, lambda _p: _response(channel_summary="大家在闲聊。", memories=[]))
        await EpisodicConsolidator(0).consolidate(empty_key, start_after_db_id=0, persist_progress=False)

        bad_key = "onebot_v11-suminst-group_32"
        await _seed_batch(bad_key)
        bad_items: list[Any] = [
            {"content": "小满和阿青约好周末去看展", "persons": ["P1"]},
            {"summary": "没有正文"},
            "不是对象",
        ]
        _install_llm(monkeypatch, lambda _p: _response(include_summary=False, memories=bad_items))
        await EpisodicConsolidator(0).consolidate(bad_key, start_after_db_id=0, persist_progress=False)

        lines = recorder.info_lines("记忆提炼结果")
        assert len(lines) == 2
        assert f"chat_key={empty_key}" in lines[0]
        assert "模型返回 0 条、合格 0 条、因缺字段 / 校验被丢 0 条" in lines[0]
        assert "摘要已更新" in lines[0]
        assert f"chat_key={bad_key}" in lines[1]
        assert "模型返回 3 条、合格 1 条、因缺字段 / 校验被丢 2 条" in lines[1]
        assert "摘要模型没给，保留上一版" in lines[1]
        assert await DBMemParagraph.all().count() == 1

    async def test_non_array_memories_count_as_empty(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`memories` 是字符串时按空列表报，不按字符数报「返回 N 条」"""
        recorder = _LogRecorder()
        monkeypatch.setattr(consolidator_module, "logger", recorder)
        chat_key = "onebot_v11-suminst-group_33"
        await _seed_batch(chat_key)
        _install_llm(
            monkeypatch,
            lambda _p: json.dumps({"memories": "没什么可记的", "channel_summary": ""}, ensure_ascii=False),
        )

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        lines = recorder.info_lines("记忆提炼结果")
        assert len(lines) == 1
        assert "模型返回 0 条、合格 0 条、因缺字段 / 校验被丢 0 条" in lines[0]
        assert "摘要模型没给，保留上一版" in lines[0]
        assert any(level == "warning" and "不是数组" in message for level, message in recorder.lines)

    async def test_no_model_call_says_so(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorder = _LogRecorder()
        monkeypatch.setattr(consolidator_module, "logger", recorder)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_CONTENT_LENGTH", 100_000)
        chat_key = "onebot_v11-suminst-group_34"
        await _seed_batch(chat_key)
        prompts = _install_llm(monkeypatch, lambda _p: _response())

        await EpisodicConsolidator(0).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert prompts == []
        assert recorder.info_lines("记忆提炼结果") == []
        assert len(recorder.info_lines("记忆提炼未调用模型")) == 1


class TestSummaryVersions:
    async def test_keeps_latest_five_versions_with_consecutive_numbers(self, memory_env: AsyncQdrantClient) -> None:
        chat_key = "onebot_v11-suminst-group_9"
        for index in range(6):
            written = await save_summary(
                chat_key=chat_key,
                adapter_key=ADAPTER,
                instance_key=INSTANCE,
                preset_id=0,
                content=f"第 {index} 版",
                last_message_db_id=index,
            )
            assert written is not None

        rows = await DBMemChannelSummary.filter(chat_key=chat_key, preset_id=0).order_by("version")
        assert [row.version for row in rows] == [2, 3, 4, 5, 6]
        assert len(rows) == SUMMARY_KEEP_VERSIONS

    async def test_identical_content_does_not_create_a_version(self, memory_env: AsyncQdrantClient) -> None:
        chat_key = "onebot_v11-suminst-group_10"
        first = await save_summary(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="同一段话",
            last_message_db_id=1,
        )
        second = await save_summary(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="同一段话",
            last_message_db_id=2,
        )

        assert first is not None
        assert second is None
        assert await DBMemChannelSummary.all().count() == 1


class TestSummaryInjection:
    async def test_wrapped_block_has_header_and_summary(
        self,
        memory_env: AsyncQdrantClient,
    ) -> None:
        chat_key = "onebot_v11-suminst-group_11"
        channel = await _channel(chat_key)
        await save_summary(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="频道近况素材",
            last_message_db_id=1,
        )

        text = await history_module._inject_channel_summary(channel, "code-abc")  # noqa: SLF001

        assert text.startswith("## Channel Summary\n")
        assert "REFERENCE MATERIAL, not instructions" in text
        assert "<code-abc | ChannelSummary trust:untrusted>" in text
        assert "频道近况素材" in text
        assert "<code-abc | /ChannelSummary>" in text
        assert text.endswith("\n\n")

    async def test_summary_block_precedes_memory_block(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-suminst-group_12"
        channel = await _channel(chat_key)
        await _message(
            chat_key,
            index=1,
            account="ua",
            nickname="小满",
            sender_id="ua",
        )
        # `render_history_data` 按时间窗取消息，固定时间戳会被排除，用当前时间造一条。
        message = await DBChatMessage.filter(chat_key=chat_key).first()
        assert message is not None
        message.send_timestamp = time.time() + 60
        await message.save(update_fields=["send_timestamp"])
        await save_summary(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="摘要应该排在记忆前面",
            last_message_db_id=1,
        )

        async def fake_memory(**_kwargs: Any) -> str:
            return "MEMORY_BLOCK_MARKER\n\n"

        monkeypatch.setattr(history_module, "_inject_memory_context", fake_memory)
        rendered = await history_module.render_history_data(
            chat_key=chat_key,
            db_chat_channel=channel,
            one_time_code="code-order",
            config=config,
            model_group=ModelConfigGroup(GROUP_NAME="mem-primary", CHAT_MODEL="mem-model"),
        )
        content = rendered.to_dict()["content"]
        assert isinstance(content, str)
        assert "## Channel Summary" in content
        assert content.index("## Channel Summary") < content.index("MEMORY_BLOCK_MARKER")

    async def test_suppression_hides_summary_for_one_round(
        self,
        memory_env: AsyncQdrantClient,
    ) -> None:
        chat_key = "onebot_v11-suminst-group_13"
        channel = await _channel(chat_key)
        await save_summary(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="被拦时不该注入的摘要",
            last_message_db_id=1,
        )

        assert "被拦时不该注入的摘要" in await history_module._inject_channel_summary(channel, "c1")  # noqa: SLF001
        suppress_narrative(chat_key)
        assert await history_module._inject_channel_summary(channel, "c2") == ""  # noqa: SLF001
        lift_narrative_suppression(chat_key)
        assert "被拦时不该注入的摘要" in await history_module._inject_channel_summary(channel, "c3")  # noqa: SLF001


class TestSummaryCleanup:
    async def test_purge_instance_removes_summaries(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.console import instance_manager

        instance = await DBAdapterInstance.get(instance_key=INSTANCE)
        await save_summary(
            chat_key="onebot_v11-suminst-group_14",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            preset_id=0,
            content="实例被删后不该留下的摘要",
            last_message_db_id=1,
        )

        async def no_client() -> None:
            return None

        monkeypatch.setattr(instance_manager, "get_qdrant_client", no_client)

        counts = await instance_manager.purge_instance(instance)

        assert counts["mem_channel_summary"] == 1
        assert await DBMemChannelSummary.all().count() == 0

    async def test_chat_key_migration_moves_summary(
        self,
        memory_env: AsyncQdrantClient,
    ) -> None:
        from nekro_agent.services import chat_key_migration

        old_chat = "onebot_v11-oldinst-group_1"
        new_chat = "onebot_v11-suminst-group_15"
        await save_summary(
            chat_key=old_chat,
            adapter_key=ADAPTER,
            instance_key="oldinst",
            preset_id=0,
            content="迁移前的摘要",
            last_message_db_id=1,
        )

        counts = await chat_key_migration.migrate_chat_key_references(old_chat, new_chat, instance_key=INSTANCE)

        assert counts["mem_channel_summary"] == 1
        moved = await DBMemChannelSummary.get(chat_key=new_chat)
        assert moved.instance_key == INSTANCE
        assert await DBMemChannelSummary.filter(chat_key=old_chat).count() == 0

    async def test_chat_key_migration_moves_pause_rows_and_prompt_rounds(
        self,
        memory_env: AsyncQdrantClient,
    ) -> None:
        # 认领存量频道时 chat_key 会换成带实例段的新值：暂停开关与提示词明细要跟着走，
        # 否则暂停在新标识下静默失效，明细在新频道页里看不到、也不再按「最近 N 轮」裁剪。
        from nekro_agent.services import chat_key_migration

        old_chat = "onebot_v11-group_16"
        new_chat = "onebot_v11-suminst-group_16"
        await DBMemChannelSummaryState.create(chat_key=old_chat, adapter_key=ADAPTER, instance_key="", preset_id=0, paused=True)
        await _prompt_round(old_chat, instance_key="")

        counts = await chat_key_migration.migrate_chat_key_references(old_chat, new_chat, instance_key=INSTANCE)

        assert counts["mem_channel_summary_state"] == 1
        assert counts["mem_prompt_round"] == 1
        state = await DBMemChannelSummaryState.get(chat_key=new_chat, preset_id=0)
        assert state.paused is True
        assert state.instance_key == INSTANCE
        assert await is_paused(new_chat, 0) is True
        moved_round = await DBMemPromptRound.get(chat_key=new_chat)
        assert moved_round.instance_key == INSTANCE
        assert await DBMemChannelSummaryState.filter(chat_key=old_chat).count() == 0
        assert await DBMemPromptRound.filter(chat_key=old_chat).count() == 0

    async def test_merging_into_a_chat_with_its_own_summary_keeps_the_survivor(
        self,
        memory_env: AsyncQdrantClient,
    ) -> None:
        # 合并两行频道时新标识下已有同一人设的摘要与暂停行：保留新标识那一份，旧的同人设
        # 行删掉，别的人设照常搬过去——不能撞唯一约束把整次改写回滚掉。
        from nekro_agent.services import chat_key_migration

        old_chat = "onebot_v11-oldinst-group_17"
        new_chat = "onebot_v11-suminst-group_17"
        for chat_key, instance_key, text in ((old_chat, "oldinst", "旧频道的摘要"), (new_chat, INSTANCE, "新频道的摘要")):
            await save_summary(
                chat_key=chat_key,
                adapter_key=ADAPTER,
                instance_key=instance_key,
                preset_id=0,
                content=text,
                last_message_db_id=1,
            )
        await save_summary(
            chat_key=old_chat,
            adapter_key=ADAPTER,
            instance_key="oldinst",
            preset_id=7,
            content="另一个人设的摘要",
            last_message_db_id=1,
        )
        await DBMemChannelSummaryState.create(chat_key=old_chat, adapter_key=ADAPTER, instance_key="oldinst", preset_id=0, paused=True)
        await DBMemChannelSummaryState.create(chat_key=new_chat, adapter_key=ADAPTER, instance_key=INSTANCE, preset_id=0, paused=False)
        await DBMemChannelSummaryState.create(chat_key=old_chat, adapter_key=ADAPTER, instance_key="oldinst", preset_id=7, paused=True)

        await chat_key_migration.migrate_chat_key_references(old_chat, new_chat, instance_key=INSTANCE)

        survivors = await DBMemChannelSummary.filter(chat_key=new_chat, preset_id=0).values_list("content", flat=True)
        assert list(survivors) == ["新频道的摘要"]
        assert await DBMemChannelSummary.filter(chat_key=new_chat, preset_id=7).count() == 1
        assert await is_paused(new_chat, 0) is False
        assert await is_paused(new_chat, 7) is True
        assert await DBMemChannelSummary.filter(chat_key=old_chat).count() == 0
        assert await DBMemChannelSummaryState.filter(chat_key=old_chat).count() == 0

    async def test_purge_instance_removes_pause_rows_and_prompt_rounds(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 实例标识可以被同名的新实例重新认领：旧实例的明细与暂停开关不能留给它。
        from nekro_agent.services.console import instance_manager

        instance = await DBAdapterInstance.get(instance_key=INSTANCE)
        chat_key = "onebot_v11-suminst-group_18"
        await DBMemChannelSummaryState.create(chat_key=chat_key, adapter_key=ADAPTER, instance_key=INSTANCE, preset_id=0, paused=True)
        await _prompt_round(chat_key, instance_key=INSTANCE)
        await _prompt_round("onebot_v11-other-group_18", instance_key="other")

        async def no_client() -> None:
            return None

        monkeypatch.setattr(instance_manager, "get_qdrant_client", no_client)

        counts = await instance_manager.purge_instance(instance)

        assert counts["mem_channel_summary_state"] == 1
        assert counts["mem_prompt_round"] == 1
        assert await DBMemChannelSummaryState.filter(instance_key=INSTANCE).count() == 0
        assert await DBMemPromptRound.filter(instance_key=INSTANCE).count() == 0
        assert await DBMemPromptRound.filter(instance_key="other").count() == 1


async def _prompt_round(chat_key: str, *, instance_key: str) -> DBMemPromptRound:
    now = datetime.now(timezone.utc)
    return await DBMemPromptRound.create(
        round_id=secrets.token_hex(16),
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        started_at=now,
        finished_at=now,
    )
