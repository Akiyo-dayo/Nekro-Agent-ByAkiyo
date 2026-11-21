"""发送路径上的输出清理：只对带 `ctx` 的 AI 回复生效，且在输出过滤之前

这里把清理接到 `send_agent_message` 后必须钉住三件事：

- 发送段与记进聊天记录的那份是同一份清理后的文字；
- 后台手动发、命令输出这类不带 `ctx` 的发送逐字不变；
- 清理在输出过滤之前，过滤命中与不命中都按清理后的文字算。

另外跑真表确认取规则的闸、扩展行与启用状态，以及超时停用、`forget_preset` 与缓存。
"""

from __future__ import annotations

import datetime as dt
import random
import time
from collections import Counter
from types import SimpleNamespace
from typing import Any, AsyncIterator, List, Optional

import pytest
import regex
from tortoise import Tortoise

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendResponse,
)
from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.schemas.agent_message import AgentMessageSegment, AgentMessageSegmentType
from nekro_agent.services.chat import universal_chat_service as ucs_mod
from nekro_agent.services.chat.universal_chat_service import universal_chat_service
from nekro_agent.services.output_filter import service
from nekro_agent.services.output_filter.rules import compile_rules as compile_filter_rules
from nekro_agent.services.output_filter.service import FilterSettings, is_outbound_filtered
from nekro_agent.services.tavern import cleanup_runtime
from nekro_agent.services.tavern.cleanup import CompiledRule, compile_rules
from nekro_agent.services.tavern.cleanup_runtime import RuleSet

CHAT_KEY = "onebot_v11-inst1-group_20"
ADAPTER = "onebot_v11"


@pytest.fixture
async def db() -> AsyncIterator[None]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def _clean_runtime_state() -> AsyncIterator[None]:
    cleanup_runtime._compiled_cache.clear()  # noqa: SLF001
    cleanup_runtime._timeouts.clear()  # noqa: SLF001
    cleanup_runtime._disabled.clear()  # noqa: SLF001
    yield
    cleanup_runtime._compiled_cache.clear()  # noqa: SLF001
    cleanup_runtime._timeouts.clear()  # noqa: SLF001
    cleanup_runtime._disabled.clear()  # noqa: SLF001


class _Recorder:
    def __init__(self) -> None:
        self.calls: List[dict[str, Any]] = []

    async def push_bot_message(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})


class _CaptureAdapter:
    def __init__(self) -> None:
        self.requests: List[PlatformSendRequest] = []
        self.filtered_flags: List[bool] = []

    async def forward_message(self, request: PlatformSendRequest) -> PlatformSendResponse:
        self.requests.append(request)
        self.filtered_flags.append(is_outbound_filtered())
        return PlatformSendResponse(success=True, message_id="out1", recorded=False)


@pytest.fixture(autouse=True)
def _active_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(_chat_key: str) -> None:
        return None

    monkeypatch.setattr(ucs_mod.UniversalChatService, "_assert_instance_active", staticmethod(_ok))


@pytest.fixture(autouse=True)
def _recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    from nekro_agent.services import message_service as message_service_mod

    recorder = _Recorder()
    monkeypatch.setattr(message_service_mod, "message_service", recorder)
    return recorder


@pytest.fixture(autouse=True)
def _no_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(_chat_key: str, _hits: Counter[str]) -> None:
        return None

    monkeypatch.setattr(service, "record_hits_for_chat", _noop)


@pytest.fixture
def use_settings(monkeypatch: pytest.MonkeyPatch):
    def install(settings: Optional[FilterSettings]) -> None:
        async def _fake(_chat_key: str) -> Optional[FilterSettings]:
            return settings

        monkeypatch.setattr(service, "settings_for_chat", _fake)

    return install


def _settings(*, words: str = "bad", replacement: str = "**Filter**") -> FilterSettings:
    return FilterSettings(
        mode="fragment",
        replacement=replacement,
        rules=compile_filter_rules(words, ""),
    )


def _rule(find: str, replace: str = "", *, enabled: bool = True, name: str = "规则") -> dict[str, Any]:
    return {
        "name": name,
        "find": find,
        "replace": replace,
        "trim": [],
        "enabled": enabled,
        "disabled_reason": "",
        "original": None,
    }


def _rule_set(
    rules: list[dict[str, Any]],
    *,
    preset_id: int = 1,
    preset_name: str = "薄荷",
    nickname: str = "",
) -> RuleSet:
    compiled = compile_rules(rules)
    names = {rule.index: str(rules[rule.index].get("name", "")) for rule in compiled}
    return RuleSet(
        preset_id=preset_id,
        compiled=tuple(compiled),
        preset_name=preset_name,
        nickname=nickname,
        rule_names=names,
    )


def _install_rules(monkeypatch: pytest.MonkeyPatch, rules: Optional[RuleSet]) -> None:
    async def _rules(_chat_key: str) -> Optional[RuleSet]:
        return rules

    monkeypatch.setattr(ucs_mod, "rules_for_chat", _rules)


async def _send(
    adapter: _CaptureAdapter,
    content: str,
    *,
    ctx: Any = None,
    record: bool = True,
) -> None:
    await universal_chat_service.send_agent_message(
        chat_key=CHAT_KEY,
        messages=content,
        adapter=adapter,  # type: ignore[arg-type]
        ctx=ctx,
        record=record,
    )


async def _preset(name: str = "薄荷") -> DBPreset:
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content="正文",
        description="",
        tags="",
        author="测试",
    )


async def _channel(*, preset_id: Optional[int] = None, chat_key: str = CHAT_KEY) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key="inst1",
        channel_id=chat_key.split("-")[-1],
        chat_key=chat_key,
        channel_type="group",
        channel_name="测试",
        data="{}",
        preset_id=preset_id,
        is_active=True,
        observe_mode=False,
    )


async def _card(preset: DBPreset, rules: list[dict[str, Any]], **overrides: Any) -> DBPresetCard:
    return await DBPresetCard.create(preset=preset, cleanup_rules=rules, **overrides)


async def _user_message(chat_key: str, nickname: str) -> None:
    await DBChatMessage.create(
        sender_id="20001",
        sender_name=nickname,
        sender_nickname=nickname,
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key="inst1",
        message_id="user-m1",
        chat_key=chat_key,
        chat_type="group",
        platform_userid="20001",
        content_text="在吗",
        content_data="[]",
        raw_cq_code="",
        ext_data="",
        send_timestamp=1,
    )


class TestSendPath:
    async def test_ctx_send_cleans_segment_and_recorded_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recorder: _Recorder,
    ) -> None:
        _install_rules(monkeypatch, _rule_set([_rule("/bad/g", "good")]))
        adapter = _CaptureAdapter()

        await _send(adapter, "前后 bad 结尾", ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests[0].segments[0].content == "前后 good 结尾"
        assert _recorder.calls[0]["args"][1][0].content == "前后 good 结尾"

    async def test_without_ctx_text_is_verbatim(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recorder: _Recorder,
    ) -> None:
        _install_rules(monkeypatch, _rule_set([_rule("/bad/g", "good")]))
        adapter = _CaptureAdapter()

        await _send(adapter, "后台手动发 bad", ctx=None)

        assert adapter.requests[0].segments[0].content == "后台手动发 bad"
        assert _recorder.calls[0]["args"][1][0].content == "后台手动发 bad"

    async def test_operation_message_path_is_verbatim(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
    ) -> None:
        use_settings(None)
        _install_rules(monkeypatch, _rule_set([_rule("/bad/g", "good")]))
        adapter = _CaptureAdapter()

        async def _adapter_for_chat(_chat_key: str) -> Any:
            return adapter

        async def _effective_config(_chat_key: str) -> Any:
            return SimpleNamespace(AI_COMMAND_OUTPUT_PREFIX="")

        monkeypatch.setattr(ucs_mod.adapter_utils, "get_adapter_for_chat", _adapter_for_chat)
        from nekro_agent.services.config_resolver import config_resolver

        monkeypatch.setattr(config_resolver, "get_effective_config", _effective_config)

        await universal_chat_service.send_operation_message(CHAT_KEY, "命令输出 bad")

        assert adapter.requests[0].segments[0].content == "命令输出 bad"
        assert _recorder.calls == []

    async def test_announcement_path_is_verbatim(
        self,
        db: None,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
    ) -> None:
        use_settings(None)
        _install_rules(monkeypatch, _rule_set([_rule("/bad/g", "good")]))
        preset = await _preset()
        await _channel(preset_id=preset.id)
        adapter = _CaptureAdapter()
        adapter.supports_webui_send = True  # type: ignore[attr-defined]

        from nekro_agent.services.announcement import service as announcement_service

        async def _effective_config(_chat_key: str) -> Any:
            return SimpleNamespace(AI_COMMAND_OUTPUT_PREFIX="")

        monkeypatch.setattr(announcement_service, "get_adapter", lambda _key: adapter)
        monkeypatch.setattr(announcement_service.config_resolver, "get_effective_config", _effective_config)

        ok, reason = await announcement_service.announcement_service._deliver(  # noqa: SLF001
            SimpleNamespace(chat_key=CHAT_KEY),
            "公告 bad",
        )

        assert ok is True and reason == ""
        assert adapter.requests[0].segments[0].content == "公告 bad"

    async def test_cleanup_runs_before_output_filter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
    ) -> None:
        use_settings(_settings(words="bad"))
        _install_rules(monkeypatch, _rule_set([_rule("/ok/g", "bad")]))
        adapter = _CaptureAdapter()

        await _send(adapter, "ok", ctx=SimpleNamespace(adapter=adapter))

        # 清理先把 ok 变成 bad，过滤才命中；顺序反了就不会出现 **Filter**。
        assert adapter.requests[0].segments[0].content == "**Filter**"
        assert _recorder.calls[0]["args"][1][0].content == "**Filter**"

    async def test_cleanup_can_make_output_filter_no_longer_match(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
    ) -> None:
        use_settings(_settings(words="bad"))
        _install_rules(monkeypatch, _rule_set([_rule("/bad/g", "ok")]))
        adapter = _CaptureAdapter()

        await _send(adapter, "bad", ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests[0].segments[0].content == "ok"

    async def test_empty_text_segment_is_removed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recorder: _Recorder,
    ) -> None:
        _install_rules(monkeypatch, _rule_set([_rule("/删掉/g", "")]))
        adapter = _CaptureAdapter()
        messages = [
            AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content="删掉"),
            AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content="保留"),
        ]

        await universal_chat_service.send_agent_message(
            chat_key=CHAT_KEY,
            messages=messages,
            adapter=adapter,  # type: ignore[arg-type]
            ctx=SimpleNamespace(adapter=adapter),
            record=True,
        )

        assert [segment.content for segment in adapter.requests[0].segments] == ["保留"]
        assert [message.content for message in _recorder.calls[0]["args"][1]] == ["保留"]

    async def test_all_segments_empty_uses_empty_message_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recorder: _Recorder,
    ) -> None:
        _install_rules(monkeypatch, _rule_set([_rule("/.+/g", "")]))
        adapter = _CaptureAdapter()

        await _send(adapter, "全部删掉", ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests == []
        assert _recorder.calls == []

    async def test_no_rules_keeps_text_byte_for_byte(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recorder: _Recorder,
    ) -> None:
        _install_rules(monkeypatch, None)
        adapter = _CaptureAdapter()

        await _send(adapter, "原样 bad 文字", ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests[0].segments[0].content == "原样 bad 文字"
        assert _recorder.calls[0]["args"][1][0].content == "原样 bad 文字"


class TestRuleLookup:
    async def test_gate_off_returns_none_without_touching_the_card(
        self,
        db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 先把「闸开着就一定有规则」的局面布好：频道、人设、一条启用的规则都在。否则闸开着
        # 也会因为找不到频道返回 None，这条用例就测不出闸。
        preset = await _preset()
        await _channel(preset_id=preset.id)
        await _card(preset, [_rule("/a/g", "b")])
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        assert await cleanup_runtime.rules_for_chat(CHAT_KEY) is not None
        cleanup_runtime._compiled_cache.clear()  # noqa: SLF001

        calls: list[Any] = []

        async def _spy_card(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            return None

        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", False)
        monkeypatch.setattr(cleanup_runtime.DBPresetCard, "get_or_none", staticmethod(_spy_card))

        assert await cleanup_runtime.rules_for_chat(CHAT_KEY) is None
        assert calls == []

    async def test_no_extension_row_returns_none(self, db: None) -> None:
        preset = await _preset()
        await _channel(preset_id=preset.id)

        assert await cleanup_runtime.rules_for_chat(CHAT_KEY) is None

    async def test_switching_preset_takes_effect_on_the_next_send(
        self,
        db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """换人设后的第一条（往往就是新人设的开场白）立刻按新人设的规则清理

        原来按频道缓存人设 ID 十秒，切换后这段时间里仍用旧人设的规则。
        """
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        old = await _preset("旧人设")
        new = await _preset("新人设")
        await _card(old, [_rule("/甲/g", "旧", name="旧规则")])
        await _card(new, [_rule("/甲/g", "新", name="新规则")])
        channel = await _channel(preset_id=old.id)

        before = await cleanup_runtime.rules_for_chat(CHAT_KEY)
        assert before is not None and before.preset_id == old.id

        await channel.set_preset(new.id)
        after = await cleanup_runtime.rules_for_chat(CHAT_KEY)

        assert after is not None
        assert after.preset_id == new.id
        assert after.preset_name == "新人设"

    async def test_no_enabled_rule_returns_none(self, db: None) -> None:
        preset = await _preset()
        await _channel(preset_id=preset.id)
        await _card(preset, [_rule("/a/g", "b", enabled=False)])

        assert await cleanup_runtime.rules_for_chat(CHAT_KEY) is None

    async def test_gate_off_send_path_is_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        _recorder: _Recorder,
    ) -> None:
        calls: list[Any] = []

        async def _card(*args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            return None

        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", False)
        monkeypatch.setattr(cleanup_runtime.DBPresetCard, "get_or_none", staticmethod(_card))
        adapter = _CaptureAdapter()

        await _send(adapter, "闸关 bad 原样", ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests[0].segments[0].content == "闸关 bad 原样"
        assert _recorder.calls[0]["args"][1][0].content == "闸关 bad 原样"
        assert calls == []

    async def test_rule_exception_returns_none(
        self,
        db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("库挂了")

        monkeypatch.setattr(cleanup_runtime.DBChatChannel, "filter", staticmethod(_boom))

        assert await cleanup_runtime.rules_for_chat(CHAT_KEY) is None


class TestTimeouts:
    async def test_three_timeouts_disable_and_forget_preset_restores(self) -> None:
        slow = CompiledRule(
            index=0,
            pattern=regex.compile(r"(.{50,}).*\1"),
            global_=True,
            replace="",
            trim=(),
        )
        text = "".join(random.Random(1).choice("abcdefghij") for _ in range(20000))
        rules = RuleSet(
            preset_id=9,
            compiled=(slow,),
            preset_name="薄荷",
            nickname="",
            rule_names={0: "慢规则"},
        )

        for _ in range(3):
            result = await cleanup_runtime.apply_cleanup(CHAT_KEY, text, rules)
            assert result == text

        assert cleanup_runtime.disabled_cleanup_rules() == {(9, 0): "慢规则"}

        cleanup_runtime.forget_preset(9)
        assert cleanup_runtime.disabled_cleanup_rules() == {}

        # 登记清掉以后不会立刻又被停用：重新从第一次超时开始累计。
        await cleanup_runtime.apply_cleanup(CHAT_KEY, text, rules)
        assert cleanup_runtime.disabled_cleanup_rules() == {}

    async def test_timeout_stays_within_the_budget(self) -> None:
        slow = CompiledRule(
            index=0,
            pattern=regex.compile(r"(.{50,}).*\1"),
            global_=True,
            replace="",
            trim=(),
        )
        text = "".join(random.Random(2).choice("abcdefghij") for _ in range(20000))
        rules = RuleSet(9, (slow,), "薄荷", "", {0: "慢规则"})

        started = time.monotonic()
        result = await cleanup_runtime.apply_cleanup(CHAT_KEY, text, rules)
        elapsed = time.monotonic() - started

        assert result == text
        assert elapsed < 1.0


class TestMacrosAndCache:
    async def test_nickname_and_user_macros_are_expanded(self, db: None) -> None:
        preset = await _preset("本名")
        await _channel(preset_id=preset.id)
        await _card(preset, [_rule("/X/g", "{{char}} 对 {{user}}")], nickname="小薄")
        await _user_message(CHAT_KEY, "阿明")

        rules = await cleanup_runtime.rules_for_chat(CHAT_KEY)

        assert rules is not None
        assert await cleanup_runtime.apply_cleanup(CHAT_KEY, "X", rules) == "小薄 对 阿明"

    async def test_legacy_placeholders_are_expanded(self, db: None) -> None:
        preset = await _preset("薄荷")
        await _channel(preset_id=preset.id)
        await _card(preset, [_rule("/X/g", "<BOT> 对 <USER>")])
        await _user_message(CHAT_KEY, "阿明")

        rules = await cleanup_runtime.rules_for_chat(CHAT_KEY)

        assert rules is not None
        assert await cleanup_runtime.apply_cleanup(CHAT_KEY, "X", rules) == "薄荷 对 阿明"

    async def test_user_lookup_happens_once_per_message(self, db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        preset = await _preset("薄荷")
        await _channel(preset_id=preset.id)
        await _card(preset, [_rule("/X/g", "{{user}}{{user}}")])
        await _user_message(CHAT_KEY, "阿明")

        rules = await cleanup_runtime.rules_for_chat(CHAT_KEY)
        assert rules is not None

        queries = 0
        original = DBChatMessage.filter

        def _counted(*args: Any, **kwargs: Any) -> Any:
            nonlocal queries
            queries += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(cleanup_runtime.DBChatMessage, "filter", staticmethod(_counted))

        assert await cleanup_runtime.apply_cleanup(CHAT_KEY, "X", rules) == "阿明阿明"
        assert queries == 1

    async def test_changed_update_time_uses_new_rules_immediately(self, db: None) -> None:
        preset = await _preset()
        await _channel(preset_id=preset.id)
        card = await _card(preset, [_rule("/A/g", "B")])

        rules = await cleanup_runtime.rules_for_chat(CHAT_KEY)
        assert rules is not None
        assert await cleanup_runtime.apply_cleanup(CHAT_KEY, "A", rules) == "B"

        changed_at = card.update_time + dt.timedelta(seconds=5)
        await DBPresetCard.filter(id=card.id).update(
            cleanup_rules=[_rule("/A/g", "C")],
            update_time=changed_at,
        )

        refreshed = await cleanup_runtime.rules_for_chat(CHAT_KEY)
        assert refreshed is not None
        assert await cleanup_runtime.apply_cleanup(CHAT_KEY, "A", refreshed) == "C"

    async def test_apply_exception_returns_original_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        rules = _rule_set([_rule("/bad/g", "good")])

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("清理引擎挂了")

        monkeypatch.setattr(cleanup_runtime, "apply_compiled", _boom)

        assert await cleanup_runtime.apply_cleanup(CHAT_KEY, "原样 bad", rules) == "原样 bad"
        assert "原样 bad" not in caplog.text
