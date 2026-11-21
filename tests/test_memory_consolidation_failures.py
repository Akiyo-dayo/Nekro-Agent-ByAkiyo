"""沉淀失败必须按错误类型分流。

改造前 `_extract_memories` 把鉴权失败、限流、超时、5xx 与内容拦截一律吞成空列表，
`consolidate` 见到空列表就推进游标。后果是模型组认证不通时整段时间的对话再也不进记忆，
而进度看起来一切正常。这里钉住三件事：

1. 不可能成功的批次（内容被拦、供应商拒答、解析重试用尽）跳过并推进进度；
2. 临时故障（鉴权、限流、超时、5xx）不推进进度，连续失败到第 3 次开始退避；
3. `persist_progress=False` 时任何结果都不写频道 data。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from nekro_agent.core.config import config
from nekro_agent.services.agent.openai import LLMContentBlockedError
from nekro_agent.services.memory import consolidator as consolidator_module
from nekro_agent.services.memory.consolidator import EpisodicConsolidator

WORKSPACE_ID = 4303
CHAT_KEY = "__test__:memory:consolidation"

# 供应商拒绝页的逐字形态，取自 `provider_refusal.py` 里已登记的签名
GEMINI_REFUSAL_TEXT = (
    "The prompt could not be submitted. The prompt contains sensitive words that violate "
    "Google's Generative AI Prohibited Use policy. Try rephrasing the prompt. "
    "If you think this was an error, send feedback. "
    "https://policies.google.com/terms/generative-ai/use-policy"
)


class _FakeMessage:
    def __init__(self, db_id: int, content: str = "这是一条足够长的测试消息内容") -> None:
        self.id = db_id
        self.send_timestamp = 1_700_000_000 + db_id
        self.sender_id = "user-1"
        # 闸开时沉淀会按这一批消息收集账号称呼，假消息也得带上账号列
        self.platform_userid = "user-1"
        self.sender_name = "用户"
        self.sender_nickname = "用户"
        self.content_text = content


class _FakeChannel:
    def __init__(self, data: str = "{}") -> None:
        self.data = data
        self.save_calls = 0

    async def save(self, **_kwargs: Any) -> None:
        self.save_calls += 1

    async def get_effective_config(self) -> Any:
        return SimpleNamespace(AI_CHAT_PRESET_SETTING="")

    def memory_state(self) -> dict[str, Any]:
        return json.loads(self.data).get("memory_state", {})


def _auth_error() -> openai.AuthenticationError:
    return openai.AuthenticationError(
        "invalid api key",
        response=httpx.Response(401, request=httpx.Request("POST", "http://x")),
        body=None,
    )



async def _not_paused(_chat_key: str) -> bool:
    return False


def _make_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm: Any,
    data: str = json.dumps({"memory_state": {"last_consolidated_message_db_id": 100}}),
) -> tuple[EpisodicConsolidator, _FakeChannel, list[_FakeMessage], dict[str, int]]:
    """把取消息、调模型、写记忆全部替换掉，只留 `consolidate` 的失败处理逻辑。"""
    channel = _FakeChannel(data=data)
    consolidator = EpisodicConsolidator(WORKSPACE_ID)
    batch = [_FakeMessage(101), _FakeMessage(102)]
    calls = {"llm": 0}

    async def fake_load_channel(_chat_key: str) -> _FakeChannel:
        return channel

    async def fake_fetch(_chat_key: str, **_kwargs: Any) -> list[_FakeMessage]:
        return batch

    async def fake_store(*_args: Any, **_kwargs: Any) -> tuple[None, list[Any], list[Any]]:
        return (None, [], [])

    async def fake_llm(prompt: str, *, use_fallback_model: bool = False) -> str:
        calls["llm"] += 1
        return await llm(prompt, use_fallback_model=use_fallback_model)

    monkeypatch.setattr(consolidator, "_load_channel", fake_load_channel)
    # 频道额度的「后台暂停」判断要查库：本文件不建库，之前的用例留下的数据库配置会让它
    # 现开一个 sqlite 连接、没人关。额度不在这里测，按「没暂停」固定住
    monkeypatch.setattr(consolidator_module, "background_paused", _not_paused)
    monkeypatch.setattr(consolidator, "_fetch_messages", fake_fetch)
    monkeypatch.setattr(consolidator, "_store_memory", fake_store)
    monkeypatch.setattr(consolidator, "_call_llm", fake_llm)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0.0)
    return consolidator, channel, batch, calls


class TestTemporaryFailures:
    @pytest.mark.asyncio
    async def test_auth_failure_keeps_progress_and_backs_off_after_three(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def llm(_prompt: str, *, use_fallback_model: bool = False) -> str:
            raise _auth_error()

        consolidator, channel, batch, calls = _make_harness(monkeypatch, llm=llm)

        first = await consolidator.consolidate(CHAT_KEY)
        state = channel.memory_state()
        assert first.failed_reason == "auth"
        assert first.errors is not None
        assert "invalid api key" in first.errors[0]
        # 进度字段保持提炼前的赋值，但持久化的游标仍停在初始值
        assert first.messages_processed == len(batch)
        assert first.last_processed_message_db_id == batch[-1].id
        assert state.get("last_consolidated_message_db_id", 0) == 100
        assert state["consecutive_failures"] == 1
        assert "consolidation_retry_after" not in state

        await consolidator.consolidate(CHAT_KEY)
        third = await consolidator.consolidate(CHAT_KEY)
        state = channel.memory_state()
        assert state["consecutive_failures"] == 3
        assert state["last_failure_reason"] == "auth"
        assert state["consolidation_retry_after"]
        assert third.retry_after == state["consolidation_retry_after"]
        assert calls["llm"] == 3

        async def forbidden_fetch(*_args: Any, **_kwargs: Any) -> list[_FakeMessage]:
            raise AssertionError("退避期内不应取消息")

        monkeypatch.setattr(consolidator, "_fetch_messages", forbidden_fetch)

        cooled = await consolidator.consolidate(CHAT_KEY)
        assert cooled.failed_reason == "backoff"
        assert cooled.retry_after == state["consolidation_retry_after"]
        assert cooled.messages_processed == 0
        assert calls["llm"] == 3

    @pytest.mark.asyncio
    async def test_success_after_two_failures_clears_backoff_and_keeps_history(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        remaining = {"failures": 2}

        async def llm(_prompt: str, *, use_fallback_model: bool = False) -> str:
            if remaining["failures"] > 0:
                remaining["failures"] -= 1
                raise _auth_error()
            return '{"memories": []}'

        consolidator, channel, _batch, _calls = _make_harness(
            monkeypatch,
            llm=llm,
            data=json.dumps(
                {
                    "memory_state": {
                        "skipped_batches": 4,
                        "skipped_messages": 9,
                        "last_consolidated_message_db_id": 100,
                    },
                },
            ),
        )

        await consolidator.consolidate(CHAT_KEY)
        await consolidator.consolidate(CHAT_KEY)
        assert channel.memory_state()["consecutive_failures"] == 2

        result = await consolidator.consolidate(CHAT_KEY)
        state = channel.memory_state()
        assert result.failed_reason is None
        assert state["consecutive_failures"] == 0
        assert "consolidation_retry_after" not in state
        # 历史跳过计数保留，不被成功路径清零
        assert state["skipped_batches"] == 4
        assert state["skipped_messages"] == 9


class TestSkippedBatches:
    @pytest.mark.asyncio
    async def test_content_blocked_advances_progress_and_counts_skip(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def llm(_prompt: str, *, use_fallback_model: bool = False) -> str:
            raise LLMContentBlockedError("prohibited_content", stream_mode=False)

        consolidator, channel, batch, _calls = _make_harness(monkeypatch, llm=llm)

        result = await consolidator.consolidate(CHAT_KEY)
        state = channel.memory_state()
        assert result.skipped_reason == "content_blocked"
        assert result.failed_reason is None
        assert state["last_consolidated_message_db_id"] == batch[-1].id
        assert state["skipped_batches"] == 1
        assert state["skipped_messages"] == len(batch)
        assert state["last_skip_reason"] == "content_blocked"
        assert "last_skip_at" in state

    @pytest.mark.asyncio
    async def test_provider_refusal_skips_without_parse_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        parse_calls = 0

        async def llm(_prompt: str, *, use_fallback_model: bool = False) -> str:
            return GEMINI_REFUSAL_TEXT

        consolidator, channel, batch, calls = _make_harness(monkeypatch, llm=llm)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 2)

        async def forbidden_parse(_response: str) -> list[dict[str, Any]]:
            nonlocal parse_calls
            parse_calls += 1
            raise AssertionError("供应商拒答不应进入解析重试")

        monkeypatch.setattr(consolidator, "_parse_memory_response", forbidden_parse)

        result = await consolidator.consolidate(CHAT_KEY)
        assert result.skipped_reason == "provider_refusal"
        assert parse_calls == 0
        assert calls["llm"] == 1
        assert channel.memory_state()["last_consolidated_message_db_id"] == batch[-1].id

    @pytest.mark.asyncio
    async def test_parse_retries_exhausted_skips_and_advances(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def llm(_prompt: str, *, use_fallback_model: bool = False) -> str:
            return "这不是 JSON"

        consolidator, channel, batch, _calls = _make_harness(monkeypatch, llm=llm)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_PARSE_MAX_RETRIES", 0)

        result = await consolidator.consolidate(CHAT_KEY)
        state = channel.memory_state()
        assert result.skipped_reason == "parse_failed"
        assert state["last_consolidated_message_db_id"] == batch[-1].id
        assert state["last_skip_reason"] == "parse_failed"


class TestPersistProgressDisabled:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["auth", "blocked", "success"])
    async def test_no_channel_data_is_written(
        self,
        monkeypatch: pytest.MonkeyPatch,
        outcome: str,
    ) -> None:
        async def llm(_prompt: str, *, use_fallback_model: bool = False) -> str:
            if outcome == "auth":
                raise _auth_error()
            if outcome == "blocked":
                raise LLMContentBlockedError("safety", stream_mode=False)
            return '{"memories": []}'

        initial = json.dumps({"memory_state": {"skipped_batches": 2}})
        consolidator, channel, batch, _calls = _make_harness(monkeypatch, llm=llm, data=initial)

        result = await consolidator.consolidate(CHAT_KEY, persist_progress=False)

        assert channel.data == initial
        assert channel.save_calls == 0
        assert result.messages_processed == len(batch)
