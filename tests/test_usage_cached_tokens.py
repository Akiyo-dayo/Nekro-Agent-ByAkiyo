"""上游缓存命中的记录：取值、归一化、三处聊天调用与统计出参

## 这份测试要防的是什么

| 失效形态 | 表现 | 钉在哪 |
|---|---|---|
| 服务商换了缓存字段的位置 | 命中数恒为 0，缓存看起来从没生效过 | `TestCachedInputTokens` |
| 怪字段形状把记账带崩 | 一次正常回复以一条与本任务无关的异常收场 | `TestCachedInputTokens` |
| 命中数超过输入数 | 页面上出现大于 100% 的命中率 | `TestLedgerNormalization` |
| 三个聊天记账点漏传 | 只有流式（或只有非流式）有命中数 | `TestChatCallSites` |
| 统计漏了这一列 | 数据库有数、页面上恒为 0 | `TestAggregateShape` |
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails

from nekro_agent.services.agent import openai as openai_module
from nekro_agent.services.agent.openai import (
    _cached_input_tokens,
    gen_openai_chat_response,
    gen_openai_chat_stream,
)
from nekro_agent.services.usage import ledger
from nekro_agent.services.usage.query import _AGGREGATE_SOURCES, _normalize_row

# ---------------------------------------------------------------------------
# 上游替身：只替换 openai SDK 客户端本身
# ---------------------------------------------------------------------------


class _FakeDelta:
    def __init__(self, content: Any = None) -> None:
        self.content = content
        self.reasoning_content = None


class _FakeChoice:
    def __init__(self, *, delta: Any = None, message: Any = None, finish_reason: Any = None) -> None:
        self.delta = delta
        self.message = message
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, *, content: Any = None, usage: Any = None, no_choices: bool = False) -> None:
        self.choices = [] if no_choices else [_FakeChoice(delta=_FakeDelta(content))]
        self.usage = usage


class _FakeCompletion:
    def __init__(self, *, content: Any, usage: Any = None) -> None:
        message = SimpleNamespace(content=content, reasoning_content=None)
        self.choices = [_FakeChoice(message=message, finish_reason="stop")]
        self.usage = usage


class _FakeStream:
    """迭代器只能有一个：被测代码先取首块，再用 `async for` 消费剩下的"""

    def __init__(self, chunks: List[Any]) -> None:
        self._chunks = list(chunks)
        self._iter = None

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    def __aiter__(self):
        if self._iter is None:

            async def _gen():
                for chunk in self._chunks:
                    yield chunk

            self._iter = _gen()
        return self._iter


def _install_upstream(monkeypatch: pytest.MonkeyPatch, behaviour) -> None:
    class _Completions:
        async def create(self, **kwargs):
            result = behaviour(kwargs)
            if isinstance(result, Exception):
                raise result
            if kwargs.get("stream"):
                return _FakeStream(result)
            return result

    class _FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeClient)
    monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: object())


def _no_cached_usage(*, prompt: int = 20, completion: int = 10, total: int = 30) -> SimpleNamespace:
    """上游完全不回缓存字段的 usage"""
    return SimpleNamespace(total_tokens=total, prompt_tokens=prompt, completion_tokens=completion)


def _cached_usage(cached: int, *, prompt: int = 60, completion: int = 10, total: int = 70) -> CompletionUsage:
    return CompletionUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        prompt_tokens_details=PromptTokensDetails(cached_tokens=cached),
    )


@pytest.fixture()
def records(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """截住聊天调用里的记账参数；本文件只关心传下去的值"""
    captured: List[Dict[str, Any]] = []

    def _capture(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(openai_module, "record_llm_usage", _capture)
    return captured


# ---------------------------------------------------------------------------
# 1. 取值函数
# ---------------------------------------------------------------------------


class TestCachedInputTokens:
    def test_openai_prompt_tokens_details(self) -> None:
        usage = CompletionUsage(
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            prompt_tokens_details=PromptTokensDetails(cached_tokens=42),
        )

        assert _cached_input_tokens(usage) == 42

    def test_deepseek_extra_field_on_a_pydantic_object(self) -> None:
        usage = CompletionUsage.model_validate(
            {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_cache_hit_tokens": 37,
            },
        )

        assert _cached_input_tokens(usage) == 37

    def test_anthropic_compatible_cache_read_field(self) -> None:
        assert _cached_input_tokens({"prompt_tokens": 20, "cache_read_input_tokens": 17}) == 17

    def test_moonshot_top_level_cached_tokens(self) -> None:
        assert _cached_input_tokens({"prompt_tokens": 20, "cached_tokens": 9}) == 9

    def test_dict_with_prompt_tokens_details(self) -> None:
        usage = {"prompt_tokens": 20, "prompt_tokens_details": {"cached_tokens": 5}}

        assert _cached_input_tokens(usage) == 5

    def test_object_with_model_extra(self) -> None:
        usage = SimpleNamespace(total_tokens=5, prompt_tokens=4, completion_tokens=1, model_extra={"prompt_cache_hit_tokens": 3})

        assert _cached_input_tokens(usage) == 3

    def test_the_largest_valid_source_wins(self) -> None:
        usage = {
            "prompt_tokens_details": {"cached_tokens": 7},
            "prompt_cache_hit_tokens": 9,
        }

        assert _cached_input_tokens(usage) == 9

    def test_relay_zero_details_do_not_hide_the_real_hit(self) -> None:
        """中转站常见：标准位置回 0，真实命中数在 DeepSeek 的字段里"""
        usage = CompletionUsage.model_validate(
            {
                "prompt_tokens": 800,
                "completion_tokens": 10,
                "total_tokens": 810,
                "prompt_tokens_details": {"cached_tokens": 0},
                "prompt_cache_hit_tokens": 500,
            },
        )

        assert _cached_input_tokens(usage) == 500

    @pytest.mark.parametrize("name", ["cached_content_token_count", "cachedContentTokenCount"])
    def test_gemini_native_field(self, name: str) -> None:
        assert _cached_input_tokens({"prompt_tokens": 40, name: 24}) == 24

    def test_explicit_zero_is_a_reading_not_a_missing_field(self) -> None:
        assert _cached_input_tokens({"prompt_tokens_details": {"cached_tokens": 0}}) == 0

    def test_none_details_fall_through_to_the_next_source(self) -> None:
        usage = {"prompt_tokens_details": None, "prompt_cache_hit_tokens": 11}

        assert _cached_input_tokens(usage) == 11

    def test_integer_valued_float_is_accepted(self) -> None:
        assert _cached_input_tokens({"cached_tokens": 7.0}) == 7

    @pytest.mark.parametrize(
        "value",
        [True, False, -1, "7", float("nan"), 1.5, None, [7], {"v": 7}],
    )
    def test_invalid_values_are_not_counted(self, value: Any) -> None:
        assert _cached_input_tokens({"cached_tokens": value}) is None

    def test_invalid_value_keeps_looking_at_the_next_source(self) -> None:
        usage = {"prompt_tokens_details": {"cached_tokens": True}, "prompt_cache_hit_tokens": 6}

        assert _cached_input_tokens(usage) == 6

    def test_none_and_missing_fields_return_none(self) -> None:
        assert _cached_input_tokens(None) is None
        assert _cached_input_tokens({}) is None
        assert _cached_input_tokens(SimpleNamespace(total_tokens=1, prompt_tokens=1, completion_tokens=0)) is None

    def test_usage_that_explodes_on_attribute_access_returns_none(self) -> None:
        class _Exploding:
            def __getattr__(self, _name: str) -> Any:
                raise RuntimeError("上游回了个怪东西")

        assert _cached_input_tokens(_Exploding()) is None


# ---------------------------------------------------------------------------
# 2. 账本归一化
# ---------------------------------------------------------------------------


@pytest.fixture()
def drafts(monkeypatch: pytest.MonkeyPatch) -> List[ledger.LLMUsageDraft]:
    captured: List[ledger.LLMUsageDraft] = []

    async def _capture(draft: ledger.LLMUsageDraft) -> None:
        captured.append(draft)

    monkeypatch.setattr(ledger, "_write_record", _capture)
    ledger.reset_usage_recorder()
    yield captured
    ledger.reset_usage_recorder()


async def _record_and_read(drafts: List[ledger.LLMUsageDraft], **kwargs: Any) -> ledger.LLMUsageDraft:
    ledger.record_llm_usage(model_name="m", stream_mode=False, **kwargs)
    await asyncio.sleep(0)
    assert drafts, "记账没有产生草稿"
    return drafts[-1]


class TestLedgerNormalization:
    async def test_cached_is_clamped_to_input(self, drafts: List[ledger.LLMUsageDraft]) -> None:
        draft = await _record_and_read(drafts, token_input=10, token_input_cached=99)

        assert draft.token_input_cached == 10

    async def test_negative_cached_becomes_zero(self, drafts: List[ledger.LLMUsageDraft]) -> None:
        draft = await _record_and_read(drafts, token_input=10, token_input_cached=-3)

        assert draft.token_input_cached == 0

    async def test_cached_defaults_to_zero(self, drafts: List[ledger.LLMUsageDraft]) -> None:
        draft = await _record_and_read(drafts, token_input=10)

        assert draft.token_input_cached == 0

    async def test_zero_input_forces_zero_cached(self, drafts: List[ledger.LLMUsageDraft]) -> None:
        draft = await _record_and_read(drafts, token_input=0, token_input_cached=5)

        assert draft.token_input_cached == 0


# ---------------------------------------------------------------------------
# 3. 三处聊天调用
# ---------------------------------------------------------------------------


class TestChatCallSites:
    async def test_non_stream_with_cached_tokens(self, monkeypatch: pytest.MonkeyPatch, records: List[Dict[str, Any]]) -> None:
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="hi", usage=_cached_usage(12)))

        await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        success = [call for call in records if call["status"].value == "success"]
        assert success and success[-1]["token_input_cached"] == 12

    async def test_non_stream_without_the_field_is_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        records: List[Dict[str, Any]],
    ) -> None:
        _install_upstream(monkeypatch, lambda _kw: _FakeCompletion(content="hi", usage=_no_cached_usage()))

        await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

        assert records[-1]["token_input_cached"] == 0

    async def test_stream_with_cached_tokens(self, monkeypatch: pytest.MonkeyPatch, records: List[Dict[str, Any]]) -> None:
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content="he"),
                _FakeChunk(content="llo"),
                _FakeChunk(no_choices=True, usage=_cached_usage(21, prompt=80, completion=20, total=100)),
            ],
        )

        response = await gen_openai_chat_response(model="m", messages=[], stream_mode=True)

        assert response.response_content == "hello"
        assert records[-1]["token_input_cached"] == 21

    async def test_stream_without_the_field_is_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        records: List[Dict[str, Any]],
    ) -> None:
        _install_upstream(
            monkeypatch,
            lambda _kw: [_FakeChunk(content="hi"), _FakeChunk(no_choices=True, usage=_no_cached_usage())],
        )

        await gen_openai_chat_response(model="m", messages=[], stream_mode=True)

        assert records[-1]["token_input_cached"] == 0

    async def test_stream_later_usage_without_the_field_keeps_the_reading(
        self,
        monkeypatch: pytest.MonkeyPatch,
        records: List[Dict[str, Any]],
    ) -> None:
        """有的上游在命中数之后又发一个只带 token 总数的块：不能把已经读到的命中数清零"""
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content="hi"),
                _FakeChunk(no_choices=True, usage=_cached_usage(21, prompt=80, completion=20, total=100)),
                _FakeChunk(no_choices=True, usage=_no_cached_usage(prompt=80, completion=20, total=100)),
            ],
        )

        await gen_openai_chat_response(model="m", messages=[], stream_mode=True)

        assert records[-1]["token_input_cached"] == 21

    async def test_simple_stream_later_usage_without_the_field_keeps_the_reading(
        self,
        monkeypatch: pytest.MonkeyPatch,
        records: List[Dict[str, Any]],
    ) -> None:
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content="print(1)"),
                _FakeChunk(no_choices=True, usage=_cached_usage(31, prompt=90, completion=5, total=95)),
                _FakeChunk(no_choices=True, usage=_no_cached_usage(prompt=90, completion=5, total=95)),
            ],
        )

        _chunks = [chunk async for chunk in gen_openai_chat_stream(model="m", messages=[])]

        assert records[-1]["token_input_cached"] == 31

    async def test_simple_stream_with_cached_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
        records: List[Dict[str, Any]],
    ) -> None:
        _install_upstream(
            monkeypatch,
            lambda _kw: [
                _FakeChunk(content="print(1)"),
                _FakeChunk(no_choices=True, usage=_cached_usage(31, prompt=90, completion=5, total=95)),
            ],
        )

        chunks = [chunk async for chunk in gen_openai_chat_stream(model="m", messages=[])]

        assert chunks == ["print(1)"]
        assert records[-1]["token_input_cached"] == 31

    async def test_simple_stream_without_the_field_is_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        records: List[Dict[str, Any]],
    ) -> None:
        _install_upstream(
            monkeypatch,
            lambda _kw: [_FakeChunk(content="print(1)"), _FakeChunk(no_choices=True, usage=_no_cached_usage())],
        )

        _chunks = [chunk async for chunk in gen_openai_chat_stream(model="m", messages=[])]

        assert records[-1]["token_input_cached"] == 0


# ---------------------------------------------------------------------------
# 4. 统计出参
# ---------------------------------------------------------------------------


class TestAggregateShape:
    def test_aggregate_sources_include_the_new_column(self) -> None:
        assert _AGGREGATE_SOURCES["agg_token_input_cached"] == "token_input_cached"

    def test_normalize_row_emits_the_column(self) -> None:
        row = _normalize_row(
            {
                "agg_records": 2,
                "agg_token_input": 100,
                "agg_token_input_cached": 40,
                "agg_token_output": 10,
                "agg_token_total": 110,
                "agg_cost": 1.5,
            },
        )

        assert row["token_input_cached"] == 40

    def test_normalize_row_fills_none_with_zero(self) -> None:
        row = _normalize_row(
            {
                "agg_records": 0,
                "agg_token_input": None,
                "agg_token_input_cached": None,
                "agg_token_output": None,
                "agg_token_total": None,
                "agg_cost": None,
            },
        )

        assert row["token_input_cached"] == 0
