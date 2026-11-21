"""预填的发送、拒绝后的去预填重试与回复拼回

所有上游调用都打桩：这里要钉的是**消息列表的形态**与状态机，不连网。四条边界：
模型组声明关着 / 刚被拒的 TTL 内不发；请求形态被拒（400 / 413 / 415 / 422）时同组去掉
预填重发一次且只插一次；401 / 429 不触发这条修复；成功时把续写接回预填、回显不叠两遍。

`prefill=None` 必须与改造前逐字相同——它现在是默认值，运行时接线之外的调用点都走这条。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nekro_agent.core.config import CoreConfig, ModelConfigGroup
from nekro_agent.services.agent import run_agent
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.model_capability import (
    is_capability_marked,
    note_capability_unsupported,
    reset_capability_marks,
)
from nekro_agent.services.agent.prefill import (
    PREFILL_CAPABILITY,
    PrefillSpec,
    merge_prefill,
    prefill_message,
)

BASE_URL = "https://upstream.example/v1"


class _UpstreamRejected(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"上游返回 HTTP {status_code}")


def _group(model: str, mode: str = "assistant", base_url: str = BASE_URL) -> ModelConfigGroup:
    return ModelConfigGroup(
        CHAT_MODEL=model,
        BASE_URL=base_url,
        API_KEY="sk-test",
        MODEL_TYPE="chat",
        ENABLE_VISION=False,
        PREFILL_MODE=mode,
    )


def _config(*, fallback: str = "fallback", retries: int = 2) -> CoreConfig:
    return CoreConfig(
        MODEL_GROUPS={
            "primary": _group("primary-model"),
            "fallback": _group(fallback),
        },
        USE_MODEL_GROUP="primary",
        FALLBACK_MODEL_GROUP="fallback",
        AI_CHAT_LLM_API_MAX_RETRIES=retries,
        SAVE_PROMPTS_LOG=False,
    )


def _messages() -> list[OpenAIChatMessage]:
    return [OpenAIChatMessage.from_text("user", "你好")]


def _as_dicts(call: dict) -> list[dict]:
    return [message.to_dict() if isinstance(message, OpenAIChatMessage) else message for message in call["messages"]]


def _install(monkeypatch: pytest.MonkeyPatch, outcomes: list[object]):
    """把上游换成按顺序返回 outcomes 的替身；返回它收到的每一次调用"""
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(response_content=outcome)

    monkeypatch.setattr(run_agent, "gen_openai_chat_response", fake)
    return calls


@pytest.fixture(autouse=True)
def _clean_capability_marks():
    reset_capability_marks()
    yield
    reset_capability_marks()


class TestPrefillMessage:
    def test_off_produces_nothing(self) -> None:
        assert prefill_message(_group("m", mode="off"), "开头") is None

    def test_assistant_shape(self) -> None:
        assert prefill_message(_group("m", mode="assistant"), "开头") == {"role": "assistant", "content": "开头"}

    def test_deepseek_adds_prefix(self) -> None:
        assert prefill_message(_group("m", mode="deepseek"), "开头") == {
            "role": "assistant",
            "content": "开头",
            "prefix": True,
        }

    def test_empty_text_produces_nothing(self) -> None:
        assert prefill_message(_group("m"), "") is None

    def test_marked_group_produces_nothing(self) -> None:
        group = _group("m")
        note_capability_unsupported(group, PREFILL_CAPABILITY)
        assert prefill_message(group, "开头") is None


class TestMergePrefill:
    def test_continuation_is_prefixed(self) -> None:
        assert merge_prefill("续写", "开头") == "开头续写"

    def test_echo_is_not_duplicated(self) -> None:
        assert merge_prefill("开头续写", "开头") == "开头续写"

    def test_empty_prefill_keeps_content(self) -> None:
        assert merge_prefill("续写", "") == "续写"


class TestSdkPassthrough:
    async def test_prefix_survives_the_sdk_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`gen_openai_chat_response` 对消息只做「模型对象转 dict」，dict 原样交给 SDK

        `prefix` 不在 `OpenAIChatMessage` 的字段里，不能靠改模型类来带它；这里在 SDK 调用口
        截住实际发出的 kwargs，证明多出来的键没有被过滤掉。
        """
        from nekro_agent.services.agent import openai as openai_module

        seen: dict = {}

        class _Completions:
            async def create(self, **kwargs):
                seen.update(kwargs)
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="续写", reasoning_content=""),
                            finish_reason="stop",
                        )
                    ],
                    usage=None,
                )

        class _FakeClient:
            def __init__(self, **_kwargs) -> None:
                self.chat = SimpleNamespace(completions=_Completions())

        monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeClient)
        monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: object())
        monkeypatch.setattr(openai_module, "record_llm_usage", lambda *_args, **_kwargs: None)

        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "开头", "prefix": True},
        ]
        response = await openai_module.gen_openai_chat_response(
            model="deepseek-chat",
            messages=messages,
            stream_mode=False,
        )

        assert seen["messages"] == messages
        assert seen["messages"][-1]["prefix"] is True
        assert response.response_content == "续写"


class TestSendAgentRequest:
    async def test_off_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _config()
        config.MODEL_GROUPS["primary"] = _group("primary-model", mode="off")
        calls = _install(monkeypatch, ["正文"])
        spec = PrefillSpec("开头")

        response, _group_used, _errors = await run_agent.send_agent_request(
            _messages(),
            config,
            chat_key="onebot_v11-group_1",
            prefill=spec,
        )

        assert _as_dicts(calls[0]) == [{"role": "user", "content": "你好"}]
        assert response.response_content == "正文"
        assert spec.state == "unsupported"

    async def test_assistant_appends_one_assistant_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _install(monkeypatch, ["续写"])
        spec = PrefillSpec("开头")

        response, _group_used, _errors = await run_agent.send_agent_request(
            _messages(),
            _config(),
            chat_key="onebot_v11-group_1",
            prefill=spec,
        )

        assert _as_dicts(calls[0]) == [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "开头"},
        ]
        assert response.response_content == "开头续写"
        assert spec.state == "sent"

    async def test_deepseek_passes_prefix_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _config()
        config.MODEL_GROUPS["primary"] = _group("primary-model", mode="deepseek")
        calls = _install(monkeypatch, ["续写"])

        await run_agent.send_agent_request(
            _messages(),
            config,
            chat_key="onebot_v11-group_1",
            prefill=PrefillSpec("开头"),
        )

        assert _as_dicts(calls[0])[-1] == {"role": "assistant", "content": "开头", "prefix": True}

    async def test_marked_group_is_skipped_with_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _config()
        note_capability_unsupported(config.MODEL_GROUPS["primary"], PREFILL_CAPABILITY)
        calls = _install(monkeypatch, ["正文"])
        spec = PrefillSpec("开头")

        await run_agent.send_agent_request(
            _messages(),
            config,
            chat_key="onebot_v11-group_1",
            prefill=spec,
        )

        assert _as_dicts(calls[0]) == [{"role": "user", "content": "你好"}]
        assert spec.state == "unsupported"

    async def test_content_reject_retries_once_without_prefill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _config()
        calls = _install(monkeypatch, [_UpstreamRejected(400), "续写"])
        spec = PrefillSpec("开头")

        response, group_used, _errors = await run_agent.send_agent_request(
            _messages(),
            config,
            chat_key="onebot_v11-group_1",
            prefill=spec,
        )

        assert len(calls) == 2
        assert calls[0]["model"] == "primary-model"
        assert calls[1]["model"] == "primary-model"
        assert _as_dicts(calls[0])[-1] == {"role": "assistant", "content": "开头"}
        assert _as_dicts(calls[1]) == [{"role": "user", "content": "你好"}]
        assert spec.state == "retried_without"
        # 去掉预填后拿到的是续写本身，不再拼回预填
        assert response.response_content == "续写"
        assert group_used.CHAT_MODEL == "primary-model"
        assert is_capability_marked(config.MODEL_GROUPS["primary"], PREFILL_CAPABILITY) is True

    async def test_repair_is_inserted_only_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _config(retries=3)
        calls = _install(
            monkeypatch,
            [_UpstreamRejected(400), _UpstreamRejected(400), _UpstreamRejected(400), _UpstreamRejected(400)],
        )

        with pytest.raises(run_agent.AllLLMRequestsFailedError):
            await run_agent.send_agent_request(
                _messages(),
                config,
                chat_key="onebot_v11-group_1",
                prefill=PrefillSpec("开头"),
            )

        # 原计划 [主, 主, 备]，只插一次去预填的同组重试 → 四次
        assert len(calls) == 4
        assert [call["model"] for call in calls] == [
            "primary-model",
            "primary-model",
            "primary-model",
            "fallback",
        ]
        assert _as_dicts(calls[0])[-1] == {"role": "assistant", "content": "开头"}
        # 插进来的同组重试与之后的主组重试都不带预填（该组已被标记）；
        # 最后回到原计划的备用组时预填又会出现，那只是原计划，不是第二次修复。
        assert _as_dicts(calls[1]) == [{"role": "user", "content": "你好"}]
        assert _as_dicts(calls[2]) == [{"role": "user", "content": "你好"}]
        assert _as_dicts(calls[3])[-1] == {"role": "assistant", "content": "开头"}

    async def test_image_rejection_is_not_blamed_on_the_prefill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 带着预填与图片的请求因为图片被 400：视觉修复会剥图重试，这时不能把模型组
        # 记成「不支持预填」——真是预填的问题，剥图后的那一次还会再 400。
        config = _config()
        _install(monkeypatch, [_UpstreamRejected(400), "续写"])
        monkeypatch.setattr(
            run_agent,
            "classify_llm_failure",
            lambda _exc, **_kwargs: run_agent.LLMFailureKind.VISION_UNSUPPORTED,
        )
        spec = PrefillSpec("开头")

        await run_agent.send_agent_request(
            _messages(),
            config,
            chat_key="onebot_v11-group_1",
            prefill=spec,
        )

        assert is_capability_marked(config.MODEL_GROUPS["primary"], PREFILL_CAPABILITY) is False
        assert spec.state == "sent"

    @pytest.mark.parametrize("status", [401, 429])
    async def test_other_client_errors_do_not_trigger_the_repair(
        self,
        monkeypatch: pytest.MonkeyPatch,
        status: int,
    ) -> None:
        config = _config()
        calls = _install(monkeypatch, [_UpstreamRejected(status), "续写"])

        await run_agent.send_agent_request(
            _messages(),
            config,
            chat_key="onebot_v11-group_1",
            prefill=PrefillSpec("开头"),
        )

        assert [call["model"] for call in calls] == ["primary-model", "fallback"]
        assert is_capability_marked(config.MODEL_GROUPS["primary"], PREFILL_CAPABILITY) is False

    async def test_echoed_prefill_is_not_duplicated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, ["开头续写"])
        spec = PrefillSpec("开头")

        response, _group_used, _errors = await run_agent.send_agent_request(
            _messages(),
            _config(),
            chat_key="onebot_v11-group_1",
            prefill=spec,
        )

        assert response.response_content == "开头续写"
        assert spec.state == "sent"

    async def test_prefill_none_keeps_the_message_list_identical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _install(monkeypatch, ["正文"])

        await run_agent.send_agent_request(
            _messages(),
            _config(),
            chat_key="onebot_v11-group_1",
        )

        assert _as_dicts(calls[0]) == [{"role": "user", "content": "你好"}]
