"""沉淀主模型组整轮失败后改用备用模型组。

主组连接失败属于临时故障：改造前备用组只在「解析重试的最后一次」才会用，主组连不上时
同一组重试用尽就把整批记失败并退避，配置好的备用组一次都不会被调用。这里钉住新的分流：

- 主组整轮失败、备用组不同名：换成备用组把同一份提示词完整走一轮；
- 备用组这一轮成功就按成功处理（段落、游标、pending）；
- 备用组也失败：按备用组的失败原因记失败；
- 两个组同名：不重复试；
- 内容被拦：不换组，按跳过处理。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable, Iterator

import httpx
import openai
import pytest
from loguru import logger
from qdrant_client import AsyncQdrantClient
from tortoise import Tortoise

from nekro_agent.core.config import ModelConfigGroup, config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.services.agent.openai import LLMContentBlockedError
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.consolidator import EpisodicConsolidator
from nekro_agent.services.memory.scheduler import (
    ChannelActivityStats,
    ConsolidationPriority,
    ConsolidationTask,
    MemoryMaintenanceScheduler,
)

ADAPTER = "onebot_v11"
INSTANCE = "fbinst"
BOT_ACCOUNT = "ubot"
PRIMARY_GROUP = "mem-primary"
FALLBACK_GROUP = "mem-fallback"


@contextmanager
def _recording_usage_scope(context: Any) -> Iterator[Any]:
    _USAGE_CONTEXTS.append(context)
    yield context


_USAGE_CONTEXTS: list[Any] = []


@pytest.fixture
async def memory_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
    monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_CONTENT_LENGTH", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0.0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "MEMORY_LLM_MAX_RETRIES", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MODEL_GROUP", PRIMARY_GROUP)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_FALLBACK_MODEL_GROUP", FALLBACK_GROUP)
    monkeypatch.setattr(config, "USE_MODEL_GROUP", PRIMARY_GROUP)
    monkeypatch.setattr(
        config,
        "MODEL_GROUPS",
        {
            PRIMARY_GROUP: ModelConfigGroup(GROUP_NAME=PRIMARY_GROUP, CHAT_MODEL="primary-model"),
            FALLBACK_GROUP: ModelConfigGroup(GROUP_NAME=FALLBACK_GROUP, CHAT_MODEL="fallback-model"),
        },
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
    _USAGE_CONTEXTS.clear()
    try:
        yield client
    finally:
        await client.close()
        await Tortoise.close_connections()


async def _channel(chat_key: str, chat_type: str = "group", workspace_id: int | None = 1) -> DBChatChannel:
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


async def _message(chat_key: str, *, index: int, account: str, content: str = "一条消息") -> DBChatMessage:
    return await DBChatMessage.create(
        sender_id=account,
        sender_name=f"用户{index}",
        sender_nickname=f"用户{index}",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        message_id=f"m{index}",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=account,
        content_text=content,
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=1_700_000 + index * 60,
    )


def _memory_response() -> str:
    return json.dumps(
        {
            "memories": [
                {
                    "content": "用户甲约好周末看展",
                    "summary": "用户甲约看展",
                    "knowledge_type": "decision",
                    "entities": ["用户甲"],
                    "relations": [],
                },
            ],
        },
        ensure_ascii=False,
    )


def _install_provider(monkeypatch: pytest.MonkeyPatch, handler: Callable[[str], Any]) -> list[str]:
    """把上游换成按模型名分流的假实现，`_call_llm` 的真实重试与用量包裹仍然走一遍。"""
    calls: list[str] = []

    async def fake_gen(**kwargs: Any) -> Any:
        model = str(kwargs["model"])
        calls.append(model)
        return handler(model)

    monkeypatch.setattr("nekro_agent.services.agent.openai.gen_openai_chat_response", fake_gen)
    monkeypatch.setattr("nekro_agent.services.usage.usage_scope", _recording_usage_scope)
    return calls


async def _seed(chat_key: str, *, gate_open: bool) -> DBChatChannel:
    channel = await _channel(chat_key, workspace_id=1)
    await _message(chat_key, index=1, account="ua")
    await _message(chat_key, index=2, account="ub")
    return channel


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "http://x"))


def _auth_error() -> openai.AuthenticationError:
    return openai.AuthenticationError(
        "invalid api key",
        response=httpx.Response(401, request=httpx.Request("POST", "http://x")),
        body=None,
    )


class TestFallbackGroup:
    @pytest.mark.parametrize("gate_open", [False, True])
    async def test_primary_connection_failure_is_retried_on_fallback_group(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
        gate_open: bool,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", gate_open)
        chat_key = "onebot_v11-fbinst-fallback_1"
        channel = await _seed(chat_key, gate_open=gate_open)

        def handler(model: str) -> Any:
            if model == "primary-model":
                raise _connection_error()
            return SimpleNamespace(response_content=_memory_response())

        calls = _install_provider(monkeypatch, handler)

        result = await EpisodicConsolidator(0 if gate_open else 1).consolidate(
            chat_key,
            start_after_db_id=0,
            persist_progress=True,
        )

        assert calls == ["primary-model", "fallback-model"]
        assert result.failed_reason is None
        assert result.paragraphs_created == 1
        assert await DBMemParagraph.all().count() == 1
        refreshed = await DBChatChannel.get(id=channel.id)
        assert json.loads(refreshed.data)["memory_state"]["last_consolidated_message_db_id"] == 2
        assert _USAGE_CONTEXTS[-1].model_group_name == FALLBACK_GROUP

    @pytest.mark.parametrize("gate_open", [False, True])
    async def test_scheduler_subtracts_pending_without_backoff(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
        gate_open: bool,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", gate_open)
        chat_key = "onebot_v11-fbinst-fallback_2"
        await _seed(chat_key, gate_open=gate_open)

        def handler(model: str) -> Any:
            if model == "primary-model":
                raise _connection_error()
            return SimpleNamespace(response_content=_memory_response())

        _install_provider(monkeypatch, handler)

        scheduler = MemoryMaintenanceScheduler()

        async def consolidation_handler(workspace_id: int, channel_key: str) -> Any:
            return await EpisodicConsolidator(workspace_id).consolidate(channel_key)

        scope = (0 if gate_open else 1, chat_key)
        scheduler._versions[scope] = 0  # noqa: SLF001
        scheduler._channel_stats[scope] = ChannelActivityStats(  # noqa: SLF001
            workspace_id=scope[0],
            chat_key=chat_key,
            pending_messages=2,
        )
        scheduler.set_consolidation_handler(consolidation_handler)
        task = ConsolidationTask(
            priority=ConsolidationPriority.NORMAL,
            scheduled_at=1.0,
            workspace_id=1,
            chat_key=chat_key,
            version=0,
        )

        await scheduler._execute_task(task)  # noqa: SLF001

        assert scheduler._channel_stats[scope].pending_messages == 0  # noqa: SLF001
        assert scheduler.pending_retry_due(*scope) is None
        assert scheduler._retry_timers == {}  # noqa: SLF001

    async def test_same_group_name_tries_only_once(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_FALLBACK_MODEL_GROUP", PRIMARY_GROUP)
        chat_key = "onebot_v11-fbinst-fallback_3"
        await _seed(chat_key, gate_open=False)

        def handler(_model: str) -> Any:
            raise _connection_error()

        calls = _install_provider(monkeypatch, handler)

        result = await EpisodicConsolidator(1).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert calls == ["primary-model"]
        assert result.failed_reason == "timeout"
        assert result.paragraphs_created == 0

    async def test_both_groups_fail_reports_fallback_reason(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-fbinst-fallback_4"
        await _seed(chat_key, gate_open=False)

        def handler(model: str) -> Any:
            if model == "primary-model":
                raise _auth_error()
            raise _connection_error()

        calls = _install_provider(monkeypatch, handler)

        result = await EpisodicConsolidator(1).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert calls == ["primary-model", "fallback-model"]
        assert result.failed_reason == "timeout"
        assert await DBMemParagraph.all().count() == 0

    async def test_content_blocked_does_not_switch_group(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-fbinst-fallback_5"
        await _seed(chat_key, gate_open=False)

        def handler(_model: str) -> Any:
            raise LLMContentBlockedError("safety", stream_mode=False)

        calls = _install_provider(monkeypatch, handler)

        result = await EpisodicConsolidator(1).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert calls == ["primary-model"]
        assert result.skipped_reason == "content_blocked"
        assert result.failed_reason is None

    async def test_parse_retries_exhausted_does_not_start_another_group_round(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-fbinst-fallback_7"
        await _seed(chat_key, gate_open=False)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 1)
        fallback_flags: list[bool] = []

        async def fake_call(
            _self: EpisodicConsolidator,
            _prompt: str,
            *,
            use_fallback_model: bool = False,
        ) -> str:
            fallback_flags.append(use_fallback_model)
            return "这不是 JSON"

        monkeypatch.setattr(EpisodicConsolidator, "_call_llm", fake_call)

        result = await EpisodicConsolidator(1).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert result.skipped_reason == "parse_failed"
        assert len(fallback_flags) == 2
        assert result.failed_reason is None

    async def test_parse_final_fallback_failure_does_not_retry_fallback_again(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-fbinst-fallback_8"
        await _seed(chat_key, gate_open=False)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 1)
        flags: list[bool] = []

        async def fake_call(
            _self: EpisodicConsolidator,
            _prompt: str,
            *,
            use_fallback_model: bool = False,
        ) -> str:
            flags.append(use_fallback_model)
            if len(flags) == 1:
                return "这不是 JSON"
            raise _connection_error()

        monkeypatch.setattr(EpisodicConsolidator, "_call_llm", fake_call)

        result = await EpisodicConsolidator(1).consolidate(chat_key, start_after_db_id=0, persist_progress=False)

        assert flags == [False, True]
        assert result.failed_reason == "timeout"
        assert result.skipped_reason is None

    async def test_logs_do_not_contain_percent_placeholders(
        self,
        memory_env: AsyncQdrantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat_key = "onebot_v11-fbinst-fallback_6"
        await _seed(chat_key, gate_open=False)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 1)

        responses = ["这不是 JSON {", _memory_response()]

        async def fake_call(_self: EpisodicConsolidator, _prompt: str, *, use_fallback_model: bool = False) -> str:
            del use_fallback_model
            return responses.pop(0)

        monkeypatch.setattr(EpisodicConsolidator, "_call_llm", fake_call)

        captured: list[str] = []
        sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="DEBUG")
        try:
            await EpisodicConsolidator(1).consolidate(chat_key, start_after_db_id=0, persist_progress=False)
        finally:
            logger.remove(sink_id)

        assert captured, "没有抓到日志，占位符断言没有意义"
        assert not any("%s" in message for message in captured)
