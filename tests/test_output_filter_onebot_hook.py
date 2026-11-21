"""OneBot V11 出站过滤钩子

纯函数部分直接测 `filter_onebot_payload` 对 `data` 的原地改写；钩子部分用最小替身，
只有最后一条走真实的 `Bot.call_api`，证明插件直接调用发送接口也会被改写。
"""

import copy
from collections import Counter
from typing import Any, Dict, List, Optional

import pytest
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment

from nekro_agent.adapters.onebot_v11.core import output_filter_hook as hook
from nekro_agent.services.output_filter.rules import compile_rules
from nekro_agent.services.output_filter.service import FilterSettings, outbound_filtered


def _settings(*, mode: str = "fragment", replacement: str = "**Filter**", words: str = "bad") -> FilterSettings:
    return FilterSettings(
        mode=mode,  # type: ignore[arg-type]
        replacement=replacement,
        rules=compile_rules(words, ""),
    )


def _text_of(message: Any) -> str:
    return str(message)


def _texts(message: Any) -> List[str]:
    return [segment.data["text"] for segment in message if segment.type == "text"]


class _FakeBot:
    def __init__(self, self_id: str = "10001") -> None:
        self.self_id = self_id


@pytest.fixture
def fake_bot() -> _FakeBot:
    return _FakeBot()


@pytest.fixture
def patch_bot_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "OneBotV11Bot", _FakeBot)


@pytest.fixture(autouse=True)
def _no_hit_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(hook, "record_hits_for_instance", _noop)


class TestMessageShapes:
    def test_plain_string_is_parsed_and_replaced(self) -> None:
        data: Dict[str, Any] = {"message": "bad"}

        hits = hook.filter_onebot_payload("send_group_msg", data, _settings(), "10001")

        assert hits == Counter({"word:bad": 1})
        assert isinstance(data["message"], Message)
        assert _text_of(data["message"]) == "**Filter**"

    def test_auto_escape_string_stays_a_string(self) -> None:
        data: Dict[str, Any] = {"message": "bad", "auto_escape": True}

        hook.filter_onebot_payload("send_group_msg", data, _settings(), "10001")

        assert data["message"] == "**Filter**"
        assert isinstance(data["message"], str)

    def test_message_object_is_replaced(self) -> None:
        data: Dict[str, Any] = {"message": Message("bad")}

        hook.filter_onebot_payload("send_group_msg", data, _settings(), "10001")

        assert isinstance(data["message"], Message)
        assert _text_of(data["message"]) == "**Filter**"

    def test_list_of_dicts_is_converted_to_message(self) -> None:
        data: Dict[str, Any] = {
            "message": [
                {"type": "text", "data": {"text": "bad"}},
                {"type": "image", "data": {"file": "pic.png"}},
            ],
        }

        hook.filter_onebot_payload("send_group_msg", data, _settings(), "10001")

        assert isinstance(data["message"], Message)
        assert [segment.type for segment in data["message"]] == ["text", "image"]
        assert _texts(data["message"]) == ["**Filter**"]

    def test_single_message_segment_is_replaced(self) -> None:
        data: Dict[str, Any] = {"message": MessageSegment.text("bad")}

        hook.filter_onebot_payload("send_group_msg", data, _settings(), "10001")

        assert isinstance(data["message"], Message)
        assert _text_of(data["message"]) == "**Filter**"

    def test_non_text_segments_are_preserved(self) -> None:
        data: Dict[str, Any] = {
            "message": [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": "bad"}},
                {"type": "face", "data": {"id": "1"}},
            ],
        }

        hook.filter_onebot_payload("send_group_msg", data, _settings(), "10001")

        assert [segment.type for segment in data["message"]] == ["at", "text", "face"]
        assert data["message"][0].data == {"qq": "12345"}
        assert data["message"][2].data == {"id": "1"}

    def test_json_card_is_replaced_whole(self) -> None:
        data: Dict[str, Any] = {
            "message": [{"type": "json", "data": {"data": '{"text":"bad"}'}}],
        }

        hook.filter_onebot_payload("send_group_msg", data, _settings(), "10001")

        assert [segment.type for segment in data["message"]] == ["text"]
        assert _texts(data["message"]) == ["**Filter**"]

    def test_ai_record_text_is_filtered(self) -> None:
        data: Dict[str, Any] = {"text": "bad"}

        hook.filter_onebot_payload("send_group_ai_record", data, _settings(), "10001")

        assert data["text"] == "**Filter**"


class TestForwardNodes:
    def test_node_content_string_is_filtered(self) -> None:
        data: Dict[str, Any] = {
            "messages": [{"type": "node", "data": {"name": "n", "uin": "12345", "content": "bad"}}],
        }

        hook.filter_onebot_payload("send_group_forward_msg", data, _settings(), "10001")

        content = data["messages"][0]["data"]["content"]
        assert isinstance(content, Message)
        assert _text_of(content) == "**Filter**"

    def test_node_content_list_is_filtered(self) -> None:
        data: Dict[str, Any] = {
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "name": "n",
                        "uin": "12345",
                        "content": [
                            {"type": "text", "data": {"text": "bad"}},
                            {"type": "image", "data": {"file": "pic.png"}},
                        ],
                    },
                },
            ],
        }

        hook.filter_onebot_payload("send_group_forward_msg", data, _settings(), "10001")

        content = data["messages"][0]["data"]["content"]
        assert isinstance(content, Message)
        assert [segment.type for segment in content] == ["text", "image"]
        assert _texts(content) == ["**Filter**"]

    def test_node_with_only_id_is_not_touched(self) -> None:
        data: Dict[str, Any] = {"messages": [{"type": "node", "data": {"id": "999"}}]}
        original = copy.deepcopy(data)

        hits = hook.filter_onebot_payload("send_group_forward_msg", data, _settings(), "10001")

        assert not hits
        assert data == original

    def test_depth_limit_stops_recursion(self) -> None:
        content: Any = "bad"
        for _ in range(7):
            content = {"type": "node", "data": {"content": content}}
        data: Dict[str, Any] = {"messages": [{"type": "node", "data": {"content": content}}]}

        hook.filter_onebot_payload("send_group_forward_msg", data, _settings(), "10001")

        # 超过深度上限后不再深入，但没有抛异常
        assert isinstance(data["messages"], list)


class TestWholeMode:
    def test_message_becomes_a_single_text_segment(self) -> None:
        data: Dict[str, Any] = {"message": "bad"}

        hook.filter_onebot_payload("send_group_msg", data, _settings(mode="whole"), "10001")

        assert isinstance(data["message"], Message)
        assert [segment.type for segment in data["message"]] == ["text"]
        assert _texts(data["message"]) == ["**Filter**"]

    def test_forward_keeps_first_node_identity(self) -> None:
        data: Dict[str, Any] = {
            "messages": [
                {"type": "node", "data": {"name": "Alice", "uin": "12345", "content": "bad"}},
                {"type": "node", "data": {"name": "Bob", "uin": "54321", "content": "clean"}},
            ],
        }

        hook.filter_onebot_payload("send_group_forward_msg", data, _settings(mode="whole"), "10001")

        assert len(data["messages"]) == 1
        node = data["messages"][0]
        assert node["type"] == "node"
        assert node["data"]["name"] == "Alice"
        assert node["data"]["uin"] == "12345"
        assert _text_of(node["data"]["content"]) == "**Filter**"

    def test_ai_record_becomes_replacement(self) -> None:
        data: Dict[str, Any] = {"text": "bad"}

        hook.filter_onebot_payload("send_group_ai_record", data, _settings(mode="whole"), "10001")

        assert data["text"] == "**Filter**"


class TestHook:
    async def test_filter_off_leaves_data_untouched(self, fake_bot: _FakeBot, patch_bot_class, monkeypatch) -> None:
        async def _settings_none(_chat_key: str) -> Optional[FilterSettings]:
            return None

        monkeypatch.setattr(hook, "lookup_cached_instance_key", lambda _self_id: "inst1")
        monkeypatch.setattr(hook, "settings_for_chat", _settings_none)
        original_message = Message("bad")
        data: Dict[str, Any] = {"group_id": 12345, "message": original_message}
        before = copy.deepcopy(data)

        await hook._on_calling_api(fake_bot, "send_group_msg", data)  # type: ignore[arg-type]

        assert data == before
        assert data["message"] is original_message

    async def test_filter_on_without_hit_leaves_data_untouched(
        self,
        fake_bot: _FakeBot,
        patch_bot_class,
        monkeypatch,
    ) -> None:
        async def _settings_clean(_chat_key: str) -> Optional[FilterSettings]:
            return _settings(words="bad")

        monkeypatch.setattr(hook, "lookup_cached_instance_key", lambda _self_id: "inst1")
        monkeypatch.setattr(hook, "settings_for_chat", _settings_clean)
        original_message = Message("clean")
        data: Dict[str, Any] = {"group_id": 12345, "message": original_message}
        before = copy.deepcopy(data)

        await hook._on_calling_api(fake_bot, "send_group_msg", data)  # type: ignore[arg-type]

        assert data == before
        assert data["message"] is original_message

    async def test_outbound_filtered_context_makes_hook_a_noop(
        self,
        fake_bot: _FakeBot,
        patch_bot_class,
        monkeypatch,
    ) -> None:
        async def _settings_hit(_chat_key: str) -> Optional[FilterSettings]:
            return _settings()

        monkeypatch.setattr(hook, "lookup_cached_instance_key", lambda _self_id: "inst1")
        monkeypatch.setattr(hook, "settings_for_chat", _settings_hit)
        original_message = Message("bad")
        data: Dict[str, Any] = {"group_id": 12345, "message": original_message}

        with outbound_filtered():
            await hook._on_calling_api(fake_bot, "send_group_msg", data)  # type: ignore[arg-type]

        assert data["message"] is original_message

    async def test_non_send_api_is_ignored(self, fake_bot: _FakeBot, patch_bot_class, monkeypatch) -> None:
        monkeypatch.setattr(hook, "settings_for_chat", lambda _chat_key: _settings())
        data: Dict[str, Any] = {"group_id": 12345}

        await hook._on_calling_api(fake_bot, "get_group_list", data)  # type: ignore[arg-type]

        assert data == {"group_id": 12345}

    async def test_settings_failure_falls_back_to_global_config(
        self,
        fake_bot: _FakeBot,
        patch_bot_class,
        monkeypatch,
    ) -> None:
        async def _boom(_chat_key: str) -> Optional[FilterSettings]:
            raise RuntimeError("config down")

        monkeypatch.setattr(hook, "lookup_cached_instance_key", lambda _self_id: "inst1")
        monkeypatch.setattr(hook, "settings_for_chat", _boom)
        monkeypatch.setattr(hook, "settings_from_config", lambda _cfg: _settings())
        data: Dict[str, Any] = {"group_id": 12345, "message": "bad"}

        await hook._on_calling_api(fake_bot, "send_group_msg", data)  # type: ignore[arg-type]

        assert _text_of(data["message"]) == "**Filter**"

    def test_engine_exception_falls_back_to_whole_without_raising(self, monkeypatch) -> None:
        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("engine down")

        monkeypatch.setattr(hook, "filter_text", _boom)
        data: Dict[str, Any] = {"group_id": 12345, "message": "bad"}

        hits = hook.filter_onebot_payload("send_group_msg", data, _settings(), "10001")

        assert hits == Counter({"error:engine": 1})
        assert _text_of(data["message"]) == "**Filter**"

    async def test_instance_override_only_filters_that_account(
        self,
        patch_bot_class,
        monkeypatch,
    ) -> None:
        settings = _settings()
        overrides = {"onebot_v11-inst1-group_1": settings}

        async def _settings_for_chat(chat_key: str) -> Optional[FilterSettings]:
            return overrides.get(chat_key)

        monkeypatch.setattr(hook, "lookup_cached_instance_key", lambda self_id: "inst1" if self_id == "10001" else "inst2")
        monkeypatch.setattr(hook, "settings_for_chat", _settings_for_chat)

        filtered: Dict[str, Any] = {"group_id": 1, "message": "bad"}
        untouched: Dict[str, Any] = {"group_id": 1, "message": "bad"}
        original = Message("bad")
        untouched["message"] = original

        await hook._on_calling_api(_FakeBot("10001"), "send_group_msg", filtered)  # type: ignore[arg-type]
        await hook._on_calling_api(_FakeBot("10002"), "send_group_msg", untouched)  # type: ignore[arg-type]

        assert _text_of(filtered["message"]) == "**Filter**"
        assert untouched["message"] is original


    async def test_instance_without_channel_uses_instance_settings(
        self,
        fake_bot: _FakeBot,
        patch_bot_class,
        monkeypatch,
    ) -> None:
        """解析不出频道（没带 group_id / user_id）时按实例层配置过滤，而不是退回全局"""
        monkeypatch.setattr(hook, "lookup_cached_instance_key", lambda _self_id: "inst1")
        monkeypatch.setattr(hook, "settings_for_instance", lambda _adapter_key, _instance_key: _settings())
        monkeypatch.setattr(hook, "settings_from_config", lambda _cfg: None)
        data: Dict[str, Any] = {"message": "bad"}

        await hook._on_calling_api(fake_bot, "send_msg", data)  # type: ignore[arg-type]

        assert _text_of(data["message"]) == "**Filter**"


class TestPluginDirectCallApi:
    async def test_call_api_is_filtered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: List[Dict[str, Any]] = []

        class _Adapter:
            async def _call_api(self, _bot: Any, api: str, **data: Any) -> Any:
                captured.append({"api": api, **data})
                return {"message_id": "1"}

        async def _settings_hit(_chat_key: str) -> Optional[FilterSettings]:
            return _settings()

        monkeypatch.setattr(hook, "lookup_cached_instance_key", lambda _self_id: "inst1")
        monkeypatch.setattr(hook, "settings_for_chat", _settings_hit)
        hook._reset_installed_for_tests()
        hook.install_output_filter_hook()

        bot = Bot(_Adapter(), "10001")  # type: ignore[arg-type]
        try:
            await bot.call_api("send_group_msg", group_id=12345, message="bad")
        finally:
            # 钩子挂在类上，不拆掉会跟着进程带进后面所有调用 call_api 的用例
            Bot._calling_api_hook.discard(hook._on_calling_api)  # noqa: SLF001
            hook._reset_installed_for_tests()

        assert len(captured) == 1
        assert _text_of(captured[0]["message"]) == "**Filter**"
