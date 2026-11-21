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
import re
import string
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


VARIABLE_BLOCK = (
    "<UpdateVariable>\n<Analysis>time advanced; mood up</Analysis>\n"
    '<JSONPatch>[{"op": "replace", "path": "/时间", "value": "傍晚"}]</JSONPatch>\n</UpdateVariable>'
)


class TestStripVariableMarkup:
    def test_paired_block_and_placeholder(self) -> None:
        text = f"她点了点头。\n\n{VARIABLE_BLOCK}\n\n<StatusPlaceHolderImpl/>"
        assert cleanup_runtime.strip_variable_markup(text) == "她点了点头。"

    def test_case_and_variants(self) -> None:
        text = "正文<updatevariable>x</UPDATEVARIABLE><JsonPatch>[]</JsonPatch><status_current_variable>y</status_current_variable>"
        assert cleanup_runtime.strip_variable_markup(text) == "正文"
        assert cleanup_runtime.strip_variable_markup("甲<VariableUpdate>_.set('a', 1)</VariableUpdate>乙") == "甲乙"

    def test_unclosed_block_is_cut_to_the_end_only_for_card_presets(self) -> None:
        text = "回复到这里。\n<UpdateVariable>\n<Analysis>写到一半被截断"
        assert cleanup_runtime.strip_variable_markup(text, card_mode=True) == "回复到这里。"
        assert cleanup_runtime.strip_variable_markup(text) is text

    def test_nested_blocks_close_on_the_outer_tag(self) -> None:
        text = "前<UpdateVariable><JSONPatch>[]</JSONPatch>尾巴</UpdateVariable>后"
        assert cleanup_runtime.strip_variable_markup(text) == "前后"
        same = "前<UpdateVariable>x<UpdateVariable>y</UpdateVariable>还在块里</UpdateVariable>后"
        assert cleanup_runtime.strip_variable_markup(same) == "前后"

    def test_closing_tags_pair_by_normalized_name(self) -> None:
        assert cleanup_runtime.strip_variable_markup("前<JSONPatch>x</json_patch>后") == "前后"
        assert cleanup_runtime.strip_variable_markup("前<Json_Patch>x</JSONPATCH>后") == "前后"

    def test_stray_closing_tags_are_removed(self) -> None:
        assert cleanup_runtime.strip_variable_markup("正文</UpdateVariable>结尾") == "正文结尾"

    def test_self_closing_tags(self) -> None:
        jsx = "use <JsonPatch op={x} /> in React, then call save()"
        # 非卡片人设：普通对话里的 JSX 原样保留，后半条也不会被截掉
        assert cleanup_runtime.strip_variable_markup(jsx) is jsx
        assert cleanup_runtime.strip_variable_markup(jsx, card_mode=True) == "use  in React, then call save()"
        assert cleanup_runtime.strip_variable_markup("前<UpdateVariable/>后", card_mode=True) == "前后"

    def test_attribute_length_boundary(self) -> None:
        limit = cleanup_runtime.MAX_TAG_ATTR_CHARS
        fits = f"前<UpdateVariable {'a' * (limit - 1)}>秘密</UpdateVariable>后"
        assert cleanup_runtime.strip_variable_markup(fits) == "前后"
        # 属性超过上限的开始标签收不了尾，仍按开始标签算：整块连同正文去掉，不会漏出半个标签
        too_long = f"前<UpdateVariable {'a' * limit}>秘密</UpdateVariable>后"
        assert cleanup_runtime.strip_variable_markup(too_long) == "前后"

    def test_pathological_text_stays_linear(self) -> None:
        for text in ("<UpdateVariable" * 60_000, "<UpdateVariable>" * 60_000, "<JSONPatch>x</JSONPatch>" * 40_000):
            for card_mode in (False, True):
                # 量本进程的 CPU 时间而不是墙上时间：整包并行、机器忙时墙上时间会被别的进程拖长。
                # 退化成平方级时这里是几分钟的量级，门限放宽到 10 秒照样拦得住。
                started = time.process_time()
                cleanup_runtime.strip_variable_markup("开头" + text, card_mode=card_mode)
                assert time.process_time() - started < 10
        unclosed = "开头" + "<UpdateVariable>" * 1000
        assert cleanup_runtime.strip_variable_markup(unclosed, card_mode=True) == "开头"
        assert cleanup_runtime.strip_variable_markup(unclosed) is unclosed

    def test_unrelated_text_is_the_same_object(self) -> None:
        for text in ("普通回复", "a < b 而且 <b>加粗</b>", "<Update> 这个通用标签不剥"):
            assert cleanup_runtime.strip_variable_markup(text) is text

    def test_unicode_case_folding_lookalikes_are_not_tag_names(self) -> None:
        # Python 的 Unicode 忽略大小写会让 U+017F 配上 s、开尔文符号 U+212A 配上 k（还有 İ、ı 配 i）；
        # 酒馆那边的正则（JS 不带 u 的 /i）不认这些。标签名只按 ASCII 忽略大小写。
        any_letter = re.compile("[a-z]", re.IGNORECASE)
        lookalikes = {
            char: next(letter for letter in string.ascii_lowercase if re.fullmatch(letter, char, re.IGNORECASE))
            for char in map(chr, range(0x80, 0x10000))
            if any_letter.fullmatch(char)
        }
        assert lookalikes[chr(0x17F)] == "s" and lookalikes[chr(0x212A)] == "k"
        names = ("UpdateVariable", "VariableUpdate", "JSONPatch", "JSON_Patch", "status_current_variable")
        cases = 0
        for char, letter in lookalikes.items():
            for name in (*names, "StatusPlaceHolderImpl"):
                at = name.lower().find(letter)
                if at < 0:
                    continue
                fake = name[:at] + char + name[at + 1 :]
                text = f"前<{fake}/>后" if name == "StatusPlaceHolderImpl" else f"前<{fake}>秘密</{fake}>后"
                for card_mode in (False, True):
                    assert cleanup_runtime.strip_variable_markup(text, card_mode=card_mode) is text, (char, name)
                cases += 1
        assert cases >= 10
        # 名字里的 s 换成 U+017F 的开始标签不再算变量标签：卡片人设里也不会从它起截到末尾
        # （落单的结束标签照旧去掉）
        long_s = chr(0x17F)
        assert cleanup_runtime.strip_variable_markup(f"前<J{long_s}ONPatch>x</JSONPatch>后", card_mode=True) == (
            f"前<J{long_s}ONPatch>x后"
        )


class TestBuiltinStripOnSend:
    async def test_strips_without_any_card_rules(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
    ) -> None:
        use_settings(None)
        _install_rules(monkeypatch, None)
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        adapter = _CaptureAdapter()

        await _send(adapter, f"好的，这就出发。\n{VARIABLE_BLOCK}", ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests[0].segments[0].content == "好的，这就出发。"
        assert _recorder.calls[0]["args"][1][0].content == "好的，这就出发。"

    async def test_strip_runs_before_card_rules(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
    ) -> None:
        use_settings(None)
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        # 卡片规则把「Analysis」换掉：剥离在前，规则根本看不到变量块，结果里不该残留标签。
        _install_rules(monkeypatch, _rule_set([_rule("/Analysis/g", "分析")]))
        adapter = _CaptureAdapter()

        await _send(adapter, f"正文 Analysis\n{VARIABLE_BLOCK}", ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests[0].segments[0].content == "正文 分析"

    @pytest.mark.parametrize(("card_preset", "expected"), [(False, None), (True, "回复到这里。")])
    async def test_unclosed_block_is_cut_only_for_card_presets(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
        card_preset: bool,
        expected: Optional[str],
    ) -> None:
        use_settings(None)
        _install_rules(monkeypatch, None)
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        lookups: list[str] = []

        async def _is_card(chat_key: str) -> bool:
            lookups.append(chat_key)
            return card_preset

        monkeypatch.setattr(ucs_mod, "is_card_preset_chat", _is_card)
        adapter = _CaptureAdapter()
        text = "回复到这里。\n<UpdateVariable>\n<Analysis>写到一半被截断"

        await _send(adapter, text, ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests[0].segments[0].content == (expected if expected is not None else text)
        assert lookups == [CHAT_KEY]

    async def test_card_lookup_is_skipped_when_nothing_is_left(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
    ) -> None:
        use_settings(None)
        _install_rules(monkeypatch, None)
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def _forbidden(_chat_key: str) -> bool:
            raise AssertionError("成对的块剥完就没剩标签了，不该查人设")

        monkeypatch.setattr(ucs_mod, "is_card_preset_chat", _forbidden)
        adapter = _CaptureAdapter()

        await _send(adapter, f"好的。\n{VARIABLE_BLOCK}", ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests[0].segments[0].content == "好的。"

    async def test_rollback_switch_off_keeps_text_verbatim(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
    ) -> None:
        use_settings(None)
        _install_rules(monkeypatch, None)
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", False)
        adapter = _CaptureAdapter()
        text = f"正文\n{VARIABLE_BLOCK}"

        await _send(adapter, text, ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests[0].segments[0].content == text

    @pytest.mark.parametrize("card_preset", [False, True])
    async def test_display_tags_become_text_only_for_card_presets(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
        card_preset: bool,
    ) -> None:
        use_settings(None)
        _install_rules(monkeypatch, None)
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        lookups: list[str] = []

        async def _is_card(chat_key: str) -> bool:
            lookups.append(chat_key)
            return card_preset

        monkeypatch.setattr(ucs_mod, "is_card_preset_chat", _is_card)
        adapter = _CaptureAdapter()
        text = '<font color="#f66">红字</font>开头<br>第二行'

        await _send(adapter, text, ctx=SimpleNamespace(adapter=adapter))

        expected = "红字开头\n第二行" if card_preset else text
        assert adapter.requests[0].segments[0].content == expected
        assert _recorder.calls[0]["args"][1][0].content == expected
        assert lookups == [CHAT_KEY]

    async def test_text_only_rule_runs_before_display_tags(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
    ) -> None:
        use_settings(None)
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def _is_card(_chat_key: str) -> bool:
            return True

        monkeypatch.setattr(ucs_mod, "is_card_preset_chat", _is_card)
        rule = {**_rule(r"/<watch>([\s\S]*?)<\/watch>/g", '<div class="bubble">注视中……$1</div>'), "output_mode": "text"}
        _install_rules(monkeypatch, _rule_set([rule]))
        adapter = _CaptureAdapter()

        await _send(adapter, "前文<watch><b>远处的钟响了</b></watch>", ctx=SimpleNamespace(adapter=adapter))

        # 规则只留分组里的正文（替换文字里的「注视中……」不要），分组里的 <b> 再由转纯文本去掉
        assert adapter.requests[0].segments[0].content == "前文远处的钟响了"

    async def test_plain_text_never_looks_up_the_preset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
    ) -> None:
        use_settings(None)
        _install_rules(monkeypatch, None)
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def _forbidden(_chat_key: str) -> bool:
            raise AssertionError("没有标签就不该查人设")

        monkeypatch.setattr(ucs_mod, "is_card_preset_chat", _forbidden)
        adapter = _CaptureAdapter()

        await _send(adapter, "a < b，普通回复", ctx=SimpleNamespace(adapter=adapter))

        assert adapter.requests[0].segments[0].content == "a < b，普通回复"

    async def test_without_ctx_is_verbatim(
        self,
        monkeypatch: pytest.MonkeyPatch,
        use_settings,
        _recorder: _Recorder,
    ) -> None:
        use_settings(None)
        _install_rules(monkeypatch, None)
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        adapter = _CaptureAdapter()
        text = f"后台手动发\n{VARIABLE_BLOCK}"

        await _send(adapter, text, ctx=None)

        assert adapter.requests[0].segments[0].content == text


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


class TestStripLeftovers:
    """剥离不留半个标签，一个没闭合的开始标签也不挡后面的块"""

    def test_opening_tag_with_lt_in_attributes(self) -> None:
        text = "hi <UpdateVariable a='<'>secret</UpdateVariable> bye"
        assert cleanup_runtime.strip_variable_markup(text) == "hi  bye"

    def test_unclosed_tag_does_not_stop_later_blocks(self) -> None:
        text = "a <JSONPatch> b <UpdateVariable>secret</UpdateVariable> c"
        assert cleanup_runtime.strip_variable_markup(text) == "a <JSONPatch> b  c"
        assert cleanup_runtime.strip_variable_markup(text, card_mode=True) == "a"

    def test_unclosed_tag_inside_a_removed_block_does_not_truncate(self) -> None:
        text = "前<UpdateVariable><JSONPatch>没闭合</UpdateVariable>后面的正文"
        assert cleanup_runtime.strip_variable_markup(text, card_mode=True) == "前后面的正文"

    def test_overlapping_blocks(self) -> None:
        text = "hi <UpdateVariable><JSONPatch>x</UpdateVariable>tail</JSONPatch> bye"
        assert cleanup_runtime.strip_variable_markup(text) == "hi  bye"


class TestCardPresetLookup:
    async def test_card_spec_is_required(self, db: None) -> None:
        native = await _preset("原生")
        await _channel(preset_id=native.id)
        await _card(native, [], prefill="只加了一段预填")
        assert await cleanup_runtime.is_card_preset_chat(CHAT_KEY) is False

    async def test_imported_card(self, db: None) -> None:
        imported = await _preset("导入的卡")
        await _channel(preset_id=imported.id)
        await _card(imported, [], card_spec="v3")
        assert await cleanup_runtime.is_card_preset_chat(CHAT_KEY) is True
