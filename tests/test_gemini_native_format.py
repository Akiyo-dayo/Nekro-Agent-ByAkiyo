"""Gemini 原生接口格式与模型组 `API_FORMAT` 字段

覆盖四层：

- 纯函数：地址、请求头、消息转换、请求体合并、响应与 SSE 解析；
- 两个聊天入口：Gemini 分支的成功 / 被拦 / 报错 / 出站 / 记账，以及 `api_format` 不是
  gemini 时 OpenAI 路径逐字不变；
- 拉模型列表与字段持久化；
- 静态扫描：本仓库每一处聊天调用都显式传了 `api_format`。

全部确定性：上游用假客户端，不发真实网络请求；Key 是本文件自己编的假值。
"""

from __future__ import annotations

import ast
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest

from nekro_agent.core import logger
from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.models.db_model_group import DBModelGroup
from nekro_agent.routers import config as config_router
from nekro_agent.routers.console import model_groups as console_model_groups
from nekro_agent.services.agent import gemini_native
from nekro_agent.services.agent import openai as openai_module
from nekro_agent.services.agent.gemini_native import (
    GEMINI_DEFAULT_API_ROOT,
    GeminiAPIError,
    gemini_api_root,
    gemini_block_reason,
    gemini_contents,
    gemini_finish_reason,
    gemini_headers,
    gemini_model_endpoint,
    gemini_request_body,
    gemini_text_and_thought,
    gemini_usage,
    list_gemini_models,
)
from nekro_agent.services.agent.model_capability import (
    LLMFailureKind,
    classify_llm_failure,
    reset_capability_marks,
)
from nekro_agent.services.agent.openai import (
    EmptyLLMResponseError,
    LLMContentBlockedError,
    LLMMalformedToolCallError,
    OpenAIStreamChunk,
    gen_openai_chat_response,
    gen_openai_chat_stream,
)
from nekro_agent.services.model_group_test import (
    ModelGroupTestOutcome,
    ModelTestErrorCode,
    normalized_message,
)
from nekro_agent.services.model_test import build_openai_model_test_params
from nekro_agent.tools.redaction import redact_credentials

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "test-gemini-key-do-not-leak"
FAKE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MODEL = "gemini-2.5-flash"
REQUEST = httpx.Request("POST", "https://gemini.example.test/v1beta/models/x:generateContent")


# ---------------------------------------------------------------------------
# 假客户端与夹具
# ---------------------------------------------------------------------------


class _FakeStreamContext:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def __aenter__(self) -> httpx.Response:
        return self.response

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _FakeHttpClient:
    """只实现聊天与拉列表用到的几个方法；所有请求都被记下来供断言"""

    def __init__(
        self,
        *,
        response: Optional[httpx.Response] = None,
        get_responses: Optional[List[httpx.Response]] = None,
    ) -> None:
        self.response = response
        self.get_responses = list(get_responses or [])
        self.calls: List[Dict[str, Any]] = []
        self.closed = False

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        assert self.response is not None
        return self.response

    def stream(self, method: str, url: str, **kwargs: Any) -> _FakeStreamContext:
        self.calls.append({"method": method, "url": url, **kwargs})
        assert self.response is not None
        return _FakeStreamContext(self.response)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.get_responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "_FakeHttpClient":
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        await self.aclose()
        return False


@pytest.fixture(autouse=True)
def _clean_capability_marks() -> Any:
    reset_capability_marks()
    yield
    reset_capability_marks()


@pytest.fixture()
def usage_records(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    monkeypatch.setattr(openai_module, "record_llm_usage", lambda **kwargs: records.append(kwargs))
    return records


@pytest.fixture()
def log_sink(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    captured: List[str] = []
    handler_id = logger.add(captured.append, level="DEBUG", format="{message}", colorize=False)
    try:
        yield captured
    finally:
        logger.remove(handler_id)


def _install_client(monkeypatch: pytest.MonkeyPatch, client: _FakeHttpClient) -> None:
    monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: client)
    monkeypatch.setattr(openai_module, "_create_http_client", lambda **_kwargs: client)


def _response(payload: Any = None, *, status: int = 200, content: Optional[bytes] = None) -> httpx.Response:
    if payload is not None:
        return httpx.Response(status, json=payload, request=REQUEST)
    return httpx.Response(status, content=content or b"", request=REQUEST)


def _sse(*events: Any) -> bytes:
    lines: List[str] = []
    for event in events:
        if isinstance(event, str):
            lines.append(f"data: {event}")
        else:
            lines.append(f"data: {json.dumps(event)}")
    return ("\n\n".join(lines) + "\n\n").encode()


# ---------------------------------------------------------------------------
# 1. 地址
# ---------------------------------------------------------------------------


class TestApiRoot:
    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            ("", GEMINI_DEFAULT_API_ROOT),
            (None, GEMINI_DEFAULT_API_ROOT),
            ("   ", GEMINI_DEFAULT_API_ROOT),
            ("https://generativelanguage.googleapis.com/v1beta", "https://generativelanguage.googleapis.com/v1beta"),
            ("https://generativelanguage.googleapis.com/v1beta/", "https://generativelanguage.googleapis.com/v1beta"),
            ("https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/v1beta"),
            ("https://generativelanguage.googleapis.com/", "https://generativelanguage.googleapis.com/v1beta"),
            ("https://generativelanguage.googleapis.com/v1beta/models", "https://generativelanguage.googleapis.com/v1beta"),
            ("https://generativelanguage.googleapis.com/v1", "https://generativelanguage.googleapis.com/v1"),
            ("https://generativelanguage.googleapis.com/v1alpha", "https://generativelanguage.googleapis.com/v1alpha"),
            ("https://generativelanguage.googleapis.com/v1beta2", "https://generativelanguage.googleapis.com/v1beta2"),
            ("https://proxy.example.test/gemini", "https://proxy.example.test/gemini/v1beta"),
        ],
    )
    def test_roots(self, base_url: Optional[str], expected: str) -> None:
        assert gemini_api_root(base_url) == expected

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://generativelanguage.googleapis.com/v1beta?key=test-gemini-key-do-not-leak",
            "https://generativelanguage.googleapis.com/v1beta#frag",
            "https://generativelanguage.googleapis.com/v1beta?",
            "ftp://generativelanguage.googleapis.com/v1beta",
            "generativelanguage.googleapis.com/v1beta",
        ],
    )
    def test_bad_roots(self, base_url: str) -> None:
        with pytest.raises(ValueError):
            gemini_api_root(base_url)

    def test_endpoint_accepts_models_prefix(self) -> None:
        url = gemini_model_endpoint("https://proxy.example.test", f"models/{MODEL}", stream=False)
        assert url == f"https://proxy.example.test/v1beta/models/{MODEL}:generateContent"

    def test_endpoint_escapes_everything_that_could_change_the_path_or_query(self) -> None:
        model = "gemini/2.5?x#y:z"
        url = gemini_model_endpoint("https://proxy.example.test/v1beta", model, stream=False)
        assert "?" not in url
        assert "#" not in url
        assert ":" not in url.rsplit("/", 1)[-1].split(":", 1)[0]
        assert url.endswith(":generateContent")
        quoted = url.split("/models/", 1)[1].removesuffix(":generateContent")
        assert "%2F" in quoted
        assert "%3F" in quoted
        assert "%23" in quoted
        assert "%3A" in quoted

    def test_stream_endpoint_has_alt_sse(self) -> None:
        url = gemini_model_endpoint(FAKE_BASE_URL, MODEL, stream=True)
        assert url.endswith(f"/models/{MODEL}:streamGenerateContent?alt=sse")

    def test_generated_urls_never_carry_the_key(self) -> None:
        for base_url in (FAKE_BASE_URL, "https://proxy.example.test/gemini", ""):
            for stream in (False, True):
                url = gemini_model_endpoint(base_url, MODEL, stream=stream)
                assert FAKE_KEY not in url
                assert "key=" not in url
                assert "x-goog-api-key" not in url

    def test_empty_model_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            gemini_model_endpoint(FAKE_BASE_URL, "models/", stream=False)


class TestHeaders:
    def test_key_goes_to_goog_header_only(self) -> None:
        headers = gemini_headers(FAKE_KEY)
        assert headers["x-goog-api-key"] == FAKE_KEY
        assert headers["Content-Type"] == "application/json"
        assert "Authorization" not in headers

    def test_empty_key_adds_no_header(self) -> None:
        for value in ("", "   ", None):
            headers = gemini_headers(value)
            assert "x-goog-api-key" not in headers
            assert "Authorization" not in headers

    def test_a_key_that_cannot_go_into_a_header_is_refused_without_its_value(self) -> None:
        """几个 Key 换行粘进一格时，错误里不能带 Key：交给 HTTP 库的话异常文本里是转义后的整串"""
        for value in (f"{FAKE_KEY}\n{FAKE_KEY}2", f"{FAKE_KEY} {FAKE_KEY}2", f"{FAKE_KEY}\x00"):
            with pytest.raises(ValueError) as caught:
                gemini_headers(value)
            assert FAKE_KEY not in str(caught.value)

    def test_the_google_key_shape_is_redacted_even_after_an_escaped_line_break(self) -> None:
        first = "AIza" + "T" * 35
        second = "AIza" + "U" * 35
        text = f"Illegal header value b'{first}\\n{second}'"

        redacted = redact_credentials(text)

        assert first not in redacted
        assert second not in redacted


# ---------------------------------------------------------------------------
# 2. 消息转换
# ---------------------------------------------------------------------------


class TestContents:
    def test_leading_system_becomes_system_instruction(self) -> None:
        system, contents = gemini_contents(
            [
                {"role": "system", "content": "你是助手。"},
                {"role": "system", "content": "保持简短。"},
                {"role": "user", "content": "你好"},
            ],
        )
        assert system == {"parts": [{"text": "你是助手。\n\n保持简短。"}]}
        assert contents == [{"role": "user", "parts": [{"text": "你好"}]}]

    def test_later_system_is_sent_as_user(self) -> None:
        system, contents = gemini_contents(
            [
                {"role": "user", "content": "第一条"},
                {"role": "system", "content": "后来这段系统消息"},
            ],
        )
        assert system is None
        assert contents == [{"role": "user", "parts": [{"text": "第一条\n\n后来这段系统消息"}]}]

    def test_assistant_becomes_model(self) -> None:
        _system, contents = gemini_contents(
            [
                {"role": "user", "content": "问"},
                {"role": "assistant", "content": "答"},
            ],
        )
        assert [item["role"] for item in contents] == ["user", "model"]

    def test_adjacent_same_role_is_merged_with_blank_line(self) -> None:
        _system, contents = gemini_contents(
            [
                {"role": "user", "content": "第一段"},
                {"role": "user", "content": "第二段"},
            ],
        )
        assert contents == [{"role": "user", "parts": [{"text": "第一段\n\n第二段"}]}]

    def test_adjacent_same_role_keeps_part_order_around_images(self) -> None:
        _system, contents = gemini_contents(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBBB"}},
                        {"type": "text", "text": "说点什么"},
                    ],
                },
            ],
        )
        parts = contents[0]["parts"]
        assert parts[0] == {"text": "看图"}
        assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": "AAAA"}}
        assert parts[2] == {"inlineData": {"mimeType": "image/jpeg", "data": "BBBB"}}
        assert parts[3] == {"text": "说点什么"}

    def test_data_url_image_becomes_inline_data(self) -> None:
        _system, contents = gemini_contents(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "描述这张图"},
                        {"type": "image_url", "image_url": {"url": "data:image/webp;base64,ZZZZ"}},
                    ],
                },
            ],
        )
        assert {"inlineData": {"mimeType": "image/webp", "data": "ZZZZ"}} in contents[0]["parts"]

    def test_http_image_is_skipped_but_text_label_stays(self) -> None:
        label = "<tok123 | Image:https://example.test/a.png>"
        _system, contents = gemini_contents(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": label},
                        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                    ],
                },
            ],
        )
        assert contents == [{"role": "user", "parts": [{"text": label}]}]

    def test_untrusted_wrapper_text_is_byte_exact(self) -> None:
        raw = "<k3y9z2 | WorldInfo trust:untrusted>\n条目\n<k3y9z2 | /WorldInfo>"
        _system, contents = gemini_contents([{"role": "user", "content": raw}])
        assert contents[0]["parts"] == [{"text": raw}]

    def test_prefill_extra_keys_are_not_sent(self) -> None:
        _system, contents = gemini_contents(
            [
                {"role": "user", "content": "写下去"},
                {"role": "assistant", "content": "这是回答开头", "prefix": True, "name": "x"},
            ],
        )
        dumped = json.dumps(contents, ensure_ascii=False)
        assert "prefix" not in dumped
        assert '"name"' not in dumped
        assert "这是回答开头" in dumped

    def test_system_only_becomes_a_single_user_message(self) -> None:
        system, contents = gemini_contents([{"role": "system", "content": "只有系统提示"}])
        assert system is None
        assert contents == [{"role": "user", "parts": [{"text": "只有系统提示"}]}]

    def test_empty_parts_drop_the_message(self) -> None:
        _system, contents = gemini_contents(
            [
                {"role": "user", "content": ""},
                {"role": "assistant", "content": []},
                {"role": "user", "content": "留下"},
            ],
        )
        assert contents == [{"role": "user", "parts": [{"text": "留下"}]}]


# ---------------------------------------------------------------------------
# 3. 请求体与额外请求体
# ---------------------------------------------------------------------------


class TestRequestBody:
    def test_sampling_parameters_are_mapped(self) -> None:
        body = gemini_request_body(
            [{"role": "user", "content": "hi"}],
            temperature=0.3,
            top_p=0.8,
            top_k=12,
            max_tokens=99,
            stop_words=["END"],
            presence_penalty=0.1,
            frequency_penalty=0.2,
            extra_body=None,
        )
        assert body["generationConfig"] == {
            "temperature": 0.3,
            "topP": 0.8,
            "topK": 12,
            "maxOutputTokens": 99,
            "stopSequences": ["END"],
            "presencePenalty": 0.1,
            "frequencyPenalty": 0.2,
        }

    def test_none_parameters_are_not_sent(self) -> None:
        body = gemini_request_body(
            [{"role": "user", "content": "hi"}],
            temperature=None,
            top_p=None,
            top_k=None,
            max_tokens=None,
            stop_words=None,
            presence_penalty=None,
            frequency_penalty=None,
            extra_body=None,
        )
        assert "generationConfig" not in body

    def test_extra_generation_config_deep_merges(self) -> None:
        body = gemini_request_body(
            [{"role": "user", "content": "hi"}],
            temperature=0.5,
            top_p=None,
            top_k=None,
            max_tokens=None,
            stop_words=None,
            presence_penalty=None,
            frequency_penalty=None,
            extra_body={
                "generationConfig": {
                    "thinkingConfig": {"thinkingBudget": 512},
                    "temperature": 0.1,
                },
            },
        )
        config = body["generationConfig"]
        assert config["thinkingConfig"] == {"thinkingBudget": 512}
        assert config["temperature"] == 0.1

    def test_safety_settings_replace_as_a_whole(self) -> None:
        safety = [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"}]
        body = gemini_request_body(
            [{"role": "user", "content": "hi"}],
            temperature=None,
            top_p=None,
            top_k=None,
            max_tokens=None,
            stop_words=None,
            presence_penalty=None,
            frequency_penalty=None,
            extra_body={"safetySettings": safety},
        )
        assert body["safetySettings"] == safety
        assert body["safetySettings"] is not safety

    def test_response_format_maps_to_response_mime_type(self) -> None:
        for response_format in ({"type": "json_object"}, {"type": "json_schema", "json_schema": {}}):
            body = gemini_request_body(
                [{"role": "user", "content": "hi"}],
                temperature=None,
                top_p=None,
                top_k=None,
                max_tokens=None,
                stop_words=None,
                presence_penalty=None,
                frequency_penalty=None,
                extra_body={"response_format": response_format},
            )
            assert body["generationConfig"]["responseMimeType"] == "application/json"

    def test_existing_response_mime_type_is_not_overwritten(self) -> None:
        body = gemini_request_body(
            [{"role": "user", "content": "hi"}],
            temperature=None,
            top_p=None,
            top_k=None,
            max_tokens=None,
            stop_words=None,
            presence_penalty=None,
            frequency_penalty=None,
            extra_body={
                "generationConfig": {"responseMimeType": "text/plain"},
                "response_format": {"type": "json_object"},
            },
        )
        assert body["generationConfig"]["responseMimeType"] == "text/plain"

    def test_extra_top_k_maps_only_when_top_k_is_absent(self) -> None:
        without_explicit = gemini_request_body(
            [{"role": "user", "content": "hi"}],
            temperature=None,
            top_p=None,
            top_k=None,
            max_tokens=None,
            stop_words=None,
            presence_penalty=None,
            frequency_penalty=None,
            extra_body={"top_k": 7},
        )
        assert without_explicit["generationConfig"]["topK"] == 7

        with_explicit = gemini_request_body(
            [{"role": "user", "content": "hi"}],
            temperature=None,
            top_p=None,
            top_k=3,
            max_tokens=None,
            stop_words=None,
            presence_penalty=None,
            frequency_penalty=None,
            extra_body={"top_k": 7},
        )
        assert with_explicit["generationConfig"]["topK"] == 3

    def test_cached_content_is_passed_through(self) -> None:
        body = gemini_request_body(
            [{"role": "user", "content": "hi"}],
            temperature=None,
            top_p=None,
            top_k=None,
            max_tokens=None,
            stop_words=None,
            presence_penalty=None,
            frequency_penalty=None,
            extra_body={"cachedContent": "cachedContents/abc123"},
        )
        assert body["cachedContent"] == "cachedContents/abc123"

    def test_dropped_keys_do_not_change_address_headers_or_contents(
        self,
        log_sink: List[str],
    ) -> None:
        extra = {
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 256}},
            "safetySettings": [{"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"}],
            "response_format": {"type": "json_object"},
            "contents": [{"role": "user", "parts": [{"text": "hacked"}]}],
            "systemInstruction": {"parts": [{"text": "hacked"}]},
            "key": FAKE_KEY,
            "api_key": FAKE_KEY,
            "x-goog-api-key": FAKE_KEY,
            "url": "https://evil.example.test",
            "base_url": "https://evil.example.test",
            "headers": {"x-goog-api-key": FAKE_KEY},
            "tools": [{"functionDeclarations": []}],
        }
        original = copy.deepcopy(extra)
        body = gemini_request_body(
            [{"role": "user", "content": "真实内容"}],
            temperature=None,
            top_p=None,
            top_k=None,
            max_tokens=None,
            stop_words=None,
            presence_penalty=None,
            frequency_penalty=None,
            extra_body=extra,
        )

        assert extra == original, "传入的额外请求体不能被修改"
        assert body["contents"] == [{"role": "user", "parts": [{"text": "真实内容"}]}]
        assert "systemInstruction" not in body
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 256}
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        dumped = json.dumps(body, ensure_ascii=False)
        assert FAKE_KEY not in dumped
        assert "hacked" not in dumped
        assert "evil.example.test" not in dumped

        text = "".join(log_sink)
        for key in ("contents", "systemInstruction", "api_key", "x-goog-api-key", "base_url", "headers", "tools"):
            assert key in text
        assert FAKE_KEY not in text
        assert "evil.example.test" not in text


# ---------------------------------------------------------------------------
# 4. 响应解析
# ---------------------------------------------------------------------------


class TestResponseParsing:
    def test_parts_split_by_thought_flag(self) -> None:
        payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "正文一"},
                            {"text": "思考一", "thought": True},
                            {"text": "正文二"},
                            {"thoughtSignature": "sig"},
                            {"functionCall": {"name": "x"}},
                        ],
                    },
                },
            ],
        }
        assert gemini_text_and_thought(payload) == ("正文一正文二", "思考一")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("STOP", "stop"), ("MAX_TOKENS", "length"), ("SAFETY", "safety"), ("PROHIBITED_CONTENT", "prohibited_content")],
    )
    def test_finish_reason_normalization(self, raw: str, expected: str) -> None:
        assert gemini_finish_reason({"candidates": [{"finishReason": raw}]}) == expected

    def test_block_reason_normalization(self) -> None:
        assert gemini_block_reason({"promptFeedback": {"blockReason": "SAFETY"}}) == "safety"
        assert gemini_block_reason({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}) == "prohibited_content"
        assert gemini_block_reason({"promptFeedback": {"blockReason": "BLOCK_REASON_UNSPECIFIED"}}) == ""
        assert gemini_block_reason({}) == ""

    def test_usage_maps_thoughts_and_cached(self) -> None:
        usage = gemini_usage(
            {
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 20,
                    "thoughtsTokenCount": 5,
                    "totalTokenCount": 33,
                    "cachedContentTokenCount": 4,
                },
            },
        )
        assert usage == (10, 4, 25, 33)

    def test_usage_falls_back_to_input_plus_output(self) -> None:
        usage = gemini_usage({"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3}})
        assert usage.token_total == 13
        assert usage.token_output == 3

    @pytest.mark.parametrize("bad_value", ["10", True, -1, 1.5, None])
    def test_usage_rejects_non_integer_or_negative(self, bad_value: Any) -> None:
        usage = gemini_usage({"usageMetadata": {"promptTokenCount": bad_value}})
        assert usage.token_input is None


# ---------------------------------------------------------------------------
# 5. 正常响应与记账
# ---------------------------------------------------------------------------


def _success_payload() -> Dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "答案正文"},
                        {"text": "思考过程", "thought": True},
                    ],
                },
                "finishReason": "STOP",
            },
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 5,
            "totalTokenCount": 33,
            "cachedContentTokenCount": 4,
        },
    }


class TestNonStreamSuccess:
    async def test_body_thought_usage_and_ledger(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        client = _FakeHttpClient(response=_response(_success_payload()))
        _install_client(monkeypatch, client)

        response = await gen_openai_chat_response(
            model=MODEL,
            messages=[{"role": "user", "content": "hi"}],
            api_key=FAKE_KEY,
            base_url=FAKE_BASE_URL,
            api_format="gemini",
            tenant_egress=True,
        )

        assert response.response_content == "答案正文"
        assert response.thought_chain == "思考过程"
        assert (response.token_input, response.token_output, response.token_consumption) == (10, 25, 33)
        assert len(usage_records) == 1
        record = usage_records[0]
        assert record["status"].value == "success"
        assert record["token_input"] == 10
        assert record["token_input_cached"] == 4
        assert record["token_output"] == 25
        assert record["token_total"] == 33
        assert record["finish_reason"] == "stop"

    async def test_max_tokens_normalized(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        payload = _success_payload()
        payload["candidates"][0]["finishReason"] = "MAX_TOKENS"
        _install_client(monkeypatch, _FakeHttpClient(response=_response(payload)))

        response = await gen_openai_chat_response(
            model=MODEL,
            messages=[{"role": "user", "content": "hi"}],
            api_key=FAKE_KEY,
            base_url=FAKE_BASE_URL,
            api_format="gemini",
            tenant_egress=True,
        )
        assert response.response_content == "答案正文"
        assert usage_records[0]["finish_reason"] == "length"

    async def test_reasoning_only_output_is_salvaged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        payload = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "思考里其实有答案", "thought": True}]},
                    "finishReason": "STOP",
                },
            ],
        }
        _install_client(monkeypatch, _FakeHttpClient(response=_response(payload)))

        response = await gen_openai_chat_response(
            model=MODEL,
            messages=[],
            api_key=FAKE_KEY,
            base_url=FAKE_BASE_URL,
            api_format="gemini",
            tenant_egress=True,
        )
        assert response.response_content == "思考里其实有答案"

    async def test_truly_empty_response_raises_empty_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        _install_client(monkeypatch, _FakeHttpClient(response=_response({"candidates": []})))
        with pytest.raises(EmptyLLMResponseError):
            await gen_openai_chat_response(
                model=MODEL,
                messages=[],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                tenant_egress=True,
            )
        assert usage_records[0]["status"].value == "failed"

    async def test_malformed_function_call_raises_malformed_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        payload = {
            "candidates": [
                {"content": {"parts": []}, "finishReason": "MALFORMED_FUNCTION_CALL"},
            ],
        }
        _install_client(monkeypatch, _FakeHttpClient(response=_response(payload)))
        with pytest.raises(LLMMalformedToolCallError):
            await gen_openai_chat_response(
                model=MODEL,
                messages=[],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                tenant_egress=True,
            )


# ---------------------------------------------------------------------------
# 6. 流式
# ---------------------------------------------------------------------------


class TestStreamSuccess:
    @staticmethod
    def _events() -> List[Dict[str, Any]]:
        return [
            {"candidates": [{"content": {"parts": [{"text": "he"}]}}]},
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "llo"}, {"text": "想", "thought": True}]},
                    },
                ],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2, "totalTokenCount": 9, "cachedContentTokenCount": 2},
            },
            {
                "candidates": [{"content": {"parts": [{"text": "!"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"candidatesTokenCount": 4},
            },
            "[DONE]",
        ]

    async def test_stream_chunks_callback_and_usage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        client = _FakeHttpClient(response=_response(content=_sse(*self._events())))
        _install_client(monkeypatch, client)
        chunks: List[str] = []
        callbacks: List[OpenAIStreamChunk] = []

        async def on_chunk(chunk: OpenAIStreamChunk) -> bool:
            callbacks.append(chunk)
            chunks.append(chunk.chunk_text)
            return False

        response = await gen_openai_chat_response(
            model=MODEL,
            messages=[{"role": "user", "content": "hi"}],
            api_key=FAKE_KEY,
            base_url=FAKE_BASE_URL,
            api_format="gemini",
            stream_mode=True,
            chunk_callback=on_chunk,
            tenant_egress=True,
        )

        assert response.response_content == "hello!"
        assert response.thought_chain == "想"
        assert chunks == ["he", "llo", "!"]
        assert callbacks[0].token_input == 0
        record = usage_records[0]
        assert record["token_input"] == 7
        assert record["token_input_cached"] == 2, "后面的事件没带缓存命中时必须保留前面的值"
        assert record["token_output"] == 4
        assert record["token_total"] == 11

    async def test_chunk_callback_can_stop_early(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        _install_client(monkeypatch, _FakeHttpClient(response=_response(content=_sse(*self._events()))))
        chunks: List[str] = []

        async def on_chunk(chunk: OpenAIStreamChunk) -> bool:
            chunks.append(chunk.chunk_text)
            return True

        response = await gen_openai_chat_response(
            model=MODEL,
            messages=[{"role": "user", "content": "hi"}],
            api_key=FAKE_KEY,
            base_url=FAKE_BASE_URL,
            api_format="gemini",
            stream_mode=True,
            chunk_callback=on_chunk,
            tenant_egress=True,
        )
        assert chunks == ["he"]
        assert response.response_content == "he"

    async def test_simple_stream_entry_yields_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        _install_client(monkeypatch, _FakeHttpClient(response=_response(content=_sse(*self._events()))))

        chunks = [
            chunk
            async for chunk in gen_openai_chat_stream(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                tenant_egress=True,
            )
        ]
        assert chunks == ["he", "llo", "!"]
        assert len(usage_records) == 1
        record = usage_records[0]
        assert record["status"].value == "success"
        assert record["token_input"] == 7
        assert record["token_input_cached"] == 2
        assert record["token_output"] == 4

    async def test_simple_stream_ignores_bad_sse_lines(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        payload = _sse(
            {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
            "not-json",
            {"candidates": [{"content": {"parts": [{"text": "!"}]}, "finishReason": "STOP"}]},
        )
        _install_client(monkeypatch, _FakeHttpClient(response=_response(content=payload)))
        chunks = [
            chunk
            async for chunk in gen_openai_chat_stream(
                model=MODEL,
                messages=[],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                tenant_egress=True,
            )
        ]
        assert chunks == ["ok", "!"]


# ---------------------------------------------------------------------------
# 7. 被拦
# ---------------------------------------------------------------------------


class TestBlocked:
    async def test_prompt_block_reason_non_stream(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        payload = {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
        _install_client(monkeypatch, _FakeHttpClient(response=_response(payload)))

        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                tenant_egress=True,
            )
        assert excinfo.value.finish_reason == "safety"
        assert classify_llm_failure(excinfo.value, request_has_image=False) is LLMFailureKind.CONTENT_BLOCKED
        assert usage_records[0]["status"].value == "failed"
        assert usage_records[0]["finish_reason"] == "safety"

    async def test_prompt_block_reason_stream(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        payload = _sse({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
        _install_client(monkeypatch, _FakeHttpClient(response=_response(content=payload)))

        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                stream_mode=True,
                tenant_egress=True,
            )
        assert excinfo.value.finish_reason == "prohibited_content"
        assert classify_llm_failure(excinfo.value, request_has_image=False) is LLMFailureKind.CONTENT_BLOCKED
        assert usage_records[0]["finish_reason"] == "prohibited_content"

    async def test_safety_finish_reason_with_empty_text_non_stream(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        payload = {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}
        _install_client(monkeypatch, _FakeHttpClient(response=_response(payload)))

        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(
                model=MODEL,
                messages=[],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                tenant_egress=True,
            )
        assert excinfo.value.finish_reason == "safety"
        assert usage_records[0]["finish_reason"] == "safety"

    async def test_prohibited_content_finish_reason_stream(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        payload = _sse({"candidates": [{"content": {"parts": []}, "finishReason": "PROHIBITED_CONTENT"}]})
        _install_client(monkeypatch, _FakeHttpClient(response=_response(content=payload)))

        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_stream(
                model=MODEL,
                messages=[],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                tenant_egress=True,
            ).__aiter__().__anext__()
        assert excinfo.value.finish_reason == "prohibited_content"


# ---------------------------------------------------------------------------
# 8. 错误
# ---------------------------------------------------------------------------


class TestErrors:
    @pytest.mark.parametrize("status", [400, 403, 429])
    async def test_http_errors_carry_status_and_no_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
        status: int,
    ) -> None:
        payload = {
            "error": {
                "code": status,
                "message": f"upstream said no ({FAKE_KEY})",
                "status": "INVALID_ARGUMENT" if status == 400 else "PERMISSION_DENIED",
            },
        }
        _install_client(monkeypatch, _FakeHttpClient(response=_response(payload, status=status)))

        with pytest.raises(GeminiAPIError) as excinfo:
            await gen_openai_chat_response(
                model=MODEL,
                messages=[],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                tenant_egress=True,
            )
        error = excinfo.value
        assert error.status_code == status
        assert "Gemini API error" in str(error)
        assert str(status) in str(error)
        assert "generativelanguage.googleapis.com" not in str(error)
        assert "models/" not in str(error)
        assert usage_records[0]["status"].value == "failed"

    async def test_plain_text_error_body_is_used_when_json_is_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        _install_client(monkeypatch, _FakeHttpClient(response=_response(content=b"<html>bad gateway</html>", status=502)))

        with pytest.raises(GeminiAPIError) as excinfo:
            await gen_openai_chat_response(
                model=MODEL,
                messages=[],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                tenant_egress=True,
            )
        assert excinfo.value.status_code == 502
        assert "bad gateway" in str(excinfo.value)

    async def test_key_is_redacted_from_logs_and_error_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
        log_sink: List[str],
        tmp_path: Path,
    ) -> None:
        payload = {
            "error": {
                "code": 400,
                "message": f"Incorrect API key provided: {FAKE_KEY}",
                "status": "INVALID_ARGUMENT",
            },
        }
        _install_client(monkeypatch, _FakeHttpClient(response=_response(payload, status=400)))
        error_path = tmp_path / "gemini_error.json"

        with pytest.raises(GeminiAPIError):
            await gen_openai_chat_response(
                model=MODEL,
                messages=[{"role": "user", "content": "hi"}],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                error_log_path=error_path,
                tenant_egress=True,
            )

        assert FAKE_KEY not in "".join(log_sink)
        assert FAKE_KEY not in error_path.read_text(encoding="utf-8")
        assert "Incorrect API key provided" in error_path.read_text(encoding="utf-8")
        dumped_records = json.dumps(
            [{key: (value.value if hasattr(value, "value") else value) for key, value in record.items()} for record in usage_records],
            ensure_ascii=False,
            default=str,
        )
        assert FAKE_KEY not in dumped_records

    async def test_sse_error_event_raises_gemini_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        payload = _sse(
            {
                "error": {
                    "code": 429,
                    "message": f"quota exceeded for {FAKE_KEY}",
                    "status": "RESOURCE_EXHAUSTED",
                },
            },
        )
        _install_client(monkeypatch, _FakeHttpClient(response=_response(content=payload)))

        with pytest.raises(GeminiAPIError) as excinfo:
            await gen_openai_chat_stream(
                model=MODEL,
                messages=[],
                api_key=FAKE_KEY,
                base_url=FAKE_BASE_URL,
                api_format="gemini",
                tenant_egress=True,
            ).__aiter__().__anext__()
        assert excinfo.value.status_code == 429
        assert "RESOURCE_EXHAUSTED" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 9. 出站与客户端生命周期
# ---------------------------------------------------------------------------


class TestEgress:
    @pytest.mark.parametrize("egress", [True, False])
    async def test_tenant_egress_is_passed_through(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
        egress: bool,
    ) -> None:
        seen: List[Dict[str, Any]] = []
        client = _FakeHttpClient(response=_response(_success_payload()))
        monkeypatch.setattr(openai_module, "_get_http_client", lambda **kwargs: seen.append(kwargs) or client)

        await gen_openai_chat_response(
            model=MODEL,
            messages=[],
            api_key=FAKE_KEY,
            base_url=FAKE_BASE_URL,
            api_format="gemini",
            tenant_egress=egress,
        )
        assert seen[0]["tenant_egress"] is egress

    async def test_one_off_client_is_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        client = _FakeHttpClient(response=_response(_success_payload()))
        monkeypatch.setattr(openai_module, "_create_http_client", lambda **_kwargs: client)

        await gen_openai_chat_response(
            model=MODEL,
            messages=[],
            api_key=FAKE_KEY,
            base_url=FAKE_BASE_URL,
            api_format="gemini",
            pooled_client=False,
            tenant_egress=True,
        )
        assert client.closed is True


# ---------------------------------------------------------------------------
# 10. 默认路径不变
# ---------------------------------------------------------------------------


class TestOpenAIPathIsUntouched:
    async def test_unknown_api_format_uses_the_same_openai_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        usage_records: List[Dict[str, Any]],
    ) -> None:
        calls: List[Dict[str, Any]] = []

        class _Completions:
            async def create(self, **kwargs: Any) -> Any:
                calls.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok", reasoning_content=""))],
                    usage=SimpleNamespace(total_tokens=3, prompt_tokens=2, completion_tokens=1),
                )

        class _FakeAsyncOpenAI:
            def __init__(self, **_kwargs: Any) -> None:
                self.chat = SimpleNamespace(completions=_Completions())

        def _gemini_must_not_run(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("OpenAI 路径不得调用 Gemini 适配器")

        monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeAsyncOpenAI)
        monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: object())
        monkeypatch.setattr(gemini_native, "gemini_model_endpoint", _gemini_must_not_run)
        monkeypatch.setattr(gemini_native, "gemini_request_body", _gemini_must_not_run)
        monkeypatch.setattr(gemini_native, "gemini_headers", _gemini_must_not_run)

        for api_format in (None, "openai", "OpenAI ", "banana"):
            kwargs: Dict[str, Any] = {} if api_format is None else {"api_format": api_format}
            response = await gen_openai_chat_response(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
                temperature=0.2,
                top_p=0.9,
                top_k=5,
                max_tokens=64,
                extra_body={"response_format": {"type": "json_object"}},
                **kwargs,
            )
            assert response.response_content == "ok"

        assert len(calls) == 4
        for call in calls[1:]:
            assert call == calls[0]


# ---------------------------------------------------------------------------
# 11. 拉模型列表
# ---------------------------------------------------------------------------


class TestListModels:
    async def test_pagination_filter_and_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = _response(
            {
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "supportedGenerationMethods": ["generateContent", "countTokens"],
                    },
                    {
                        "name": "models/text-embedding-004",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ],
                "nextPageToken": "page-two",
            },
        )
        second = _response(
            {
                "models": [
                    {"name": "models/gemini-2.5-flash"},
                    {"name": "models/gemini-2.5-pro"},
                ],
            },
        )
        client = _FakeHttpClient(get_responses=[first, second])
        seen: List[Dict[str, Any]] = []
        monkeypatch.setattr(openai_module, "_create_http_client", lambda **kwargs: seen.append(kwargs) or client)

        models = await list_gemini_models(
            base_url=FAKE_BASE_URL,
            api_key=FAKE_KEY,
            tenant_egress=True,
        )

        assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]
        assert seen[0]["tenant_egress"] is True
        assert client.calls[0]["url"] == f"{FAKE_BASE_URL}/models"
        assert client.calls[0]["params"] == {"pageSize": 1000}
        assert client.calls[1]["params"] == {"pageSize": 1000, "pageToken": "page-two"}
        assert all(call["headers"]["x-goog-api-key"] == FAKE_KEY for call in client.calls)
        assert all("Authorization" not in call["headers"] for call in client.calls)
        assert client.closed is True

    async def test_non_2xx_raises_gemini_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeHttpClient(
            get_responses=[_response({"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}, status=403)],
        )
        monkeypatch.setattr(openai_module, "_create_http_client", lambda **_kwargs: client)

        with pytest.raises(GeminiAPIError) as excinfo:
            await list_gemini_models(base_url=FAKE_BASE_URL, api_key=FAKE_KEY, tenant_egress=False)
        assert excinfo.value.status_code == 403
        assert "denied" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 12. 字段、接口与前端默认值
# ---------------------------------------------------------------------------


class TestFieldAndWiring:
    def test_config_default_is_openai(self) -> None:
        assert ModelConfigGroup().API_FORMAT == "openai"

    def test_db_column_default_is_openai(self) -> None:
        field = DBModelGroup._meta.fields_map["api_format"]  # noqa: SLF001
        assert field.default == "openai"

    def test_to_config_group_falls_back_for_unknown_value(self) -> None:
        row = DBModelGroup(name="x", api_format="something-else")
        assert row.to_config_group().API_FORMAT == "openai"

    def test_apply_config_group_writes_api_format(self) -> None:
        row = DBModelGroup(name="x")
        row.apply_config_group(ModelConfigGroup(API_FORMAT="gemini"))
        assert row.api_format == "gemini"
        assert row.to_config_group().API_FORMAT == "gemini"

    def test_console_payload_writes_api_format(self) -> None:
        payload = console_model_groups.ModelGroupPayload(api_format="gemini")
        merged = console_model_groups._merge_payload(ModelConfigGroup(), payload)  # noqa: SLF001
        assert merged.API_FORMAT == "gemini"

    def test_model_test_params_carry_api_format(self) -> None:
        group = ModelConfigGroup(CHAT_MODEL="m", BASE_URL=FAKE_BASE_URL, API_KEY=FAKE_KEY, API_FORMAT="gemini")
        params = build_openai_model_test_params(group)
        assert params["api_format"] == "gemini"

    def test_frontend_default_matches_backend(self) -> None:
        source = (
            REPO_ROOT / "frontend" / "src" / "components" / "model-group" / "ModelGroupFormDialog.tsx"
        ).read_text(encoding="utf-8")
        match = re.search(r"API_FORMAT:\s*'(?P<value>openai|gemini)'", source)
        assert match, "前端表单里找不到 API_FORMAT 默认值"
        assert match.group("value") == ModelConfigGroup().API_FORMAT

    async def test_inline_test_passes_api_format_to_upstream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: List[ModelConfigGroup] = []

        class _NoSavedGroup:
            async def get_or_none(self, **_kwargs: Any) -> None:
                return None

        async def fake_prepare(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def fake_run(group: ModelConfigGroup, **_kwargs: Any) -> Any:
            captured.append(group)
            return ModelGroupTestOutcome(
                ok=True,
                error_code=ModelTestErrorCode.NONE,
                latency_ms=1,
                message=normalized_message(ModelTestErrorCode.NONE),
                model_name=group.CHAT_MODEL,
                response_text="",
            )

        monkeypatch.setattr(config_router, "DBModelGroup", _NoSavedGroup())
        monkeypatch.setattr(config_router, "prepare_model_group_test", fake_prepare)
        monkeypatch.setattr(config_router, "run_model_group_test", fake_run)

        user = SimpleNamespace(
            id=1,
            username="platform",
            tenant_id=None,
            tenant_role="platform_owner",
            perm_level=6,
        )
        body = config_router.ModelGroupInlineTestRequest(
            group_name="inline",
            chat_model=MODEL,
            base_url=FAKE_BASE_URL,
            api_key=FAKE_KEY,
            model_type="chat",
            api_format="gemini",
        )
        await config_router.test_model_group_inline(body, _current_user=user)

        assert len(captured) == 1
        assert captured[0].API_FORMAT == "gemini"

    @pytest.mark.parametrize(("api_format", "tenant_id", "expected_callee"), [
        (None, None, "openai"),
        ("openai", None, "openai"),
        ("gemini", None, "gemini"),
        ("gemini", 7, "gemini"),
    ])
    async def test_fetch_models_dispatches_by_api_format(
        self,
        monkeypatch: pytest.MonkeyPatch,
        api_format: Optional[str],
        tenant_id: Optional[int],
        expected_callee: str,
    ) -> None:
        calls: List[Dict[str, Any]] = []

        async def fake_gemini(**kwargs: Any) -> List[str]:
            calls.append({"callee": "gemini", **kwargs})
            return ["gemini-2.5-flash"]

        async def fake_openai(**kwargs: Any) -> List[str]:
            calls.append({"callee": "openai", **kwargs})
            return ["gpt-4o"]

        async def fake_assert(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(gemini_native, "list_gemini_models", fake_gemini)
        monkeypatch.setattr(openai_module, "list_openai_models", fake_openai)
        monkeypatch.setattr(config_router, "assert_outbound_urls_allowed", fake_assert)

        user = SimpleNamespace(
            id=2,
            username="u",
            tenant_id=tenant_id,
            tenant_role="platform_owner" if tenant_id is None else "tenant_admin",
            perm_level=6 if tenant_id is None else 4,
        )
        body = config_router.FetchModelsRequest(
            base_url=FAKE_BASE_URL,
            api_key=FAKE_KEY,
            **({} if api_format is None else {"api_format": api_format}),
        )
        response = await config_router.fetch_model_groups_models(body, _current_user=user)

        assert response.models == (["gemini-2.5-flash"] if expected_callee == "gemini" else ["gpt-4o"])
        assert calls[0]["callee"] == expected_callee
        assert calls[0]["tenant_egress"] is (tenant_id is not None)


# ---------------------------------------------------------------------------
# 13. 静态扫描：每个聊天调用都显式声明接口格式
# ---------------------------------------------------------------------------

_CHAT_CALLS = {"gen_openai_chat_response", "gen_openai_chat_stream"}
_PARAM_BUILDERS = {"build_openai_model_test_params", "_build_chat_params", "_build_model_params"}


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _files() -> List[Path]:
    paths: List[Path] = []
    for base in ("nekro_agent", "plugins"):
        paths.extend((REPO_ROOT / base).rglob("*.py"))
    return paths


def test_every_chat_call_site_passes_api_format() -> None:
    missing: List[str] = []
    for path in _files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        # `**params` 展开的参数字典由这些构造器造，它们自己必须带 `api_format`
        builder_names = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and isinstance(node.value, ast.Call)
            and _callee(node.value) in _PARAM_BUILDERS
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _callee(node) not in _CHAT_CALLS:
                continue
            if any(keyword.arg == "api_format" for keyword in node.keywords):
                continue
            spread_ok = False
            for keyword in node.keywords:
                if keyword.arg is not None:
                    continue
                if isinstance(keyword.value, ast.Call) and _callee(keyword.value) in _PARAM_BUILDERS:
                    spread_ok = True
                elif isinstance(keyword.value, ast.Name) and keyword.value.id in builder_names:
                    spread_ok = True
            if not spread_ok:
                missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert missing == [], f"这些聊天调用没传 api_format：{missing}"


def test_param_builders_include_api_format() -> None:
    missing: List[str] = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in _PARAM_BUILDERS:
                continue
            has_key = any(
                isinstance(returned, ast.Return)
                and isinstance(returned.value, ast.Dict)
                and any(isinstance(key, ast.Constant) and key.value == "api_format" for key in returned.value.keys)
                for returned in ast.walk(node)
            )
            if not has_key:
                missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert missing == [], f"这些参数构造器没带 api_format：{missing}"
