"""发送路径上把剥掉的变量更新块交给接收方

要钉住的事：

1. 取块与剥离同一套配对：取到的正好是剥离去掉的更新块（外层套内层只取外层；`initvar`、
   `status_current_variable` 不取；卡片人设没闭合的尾块取到末尾；超长的整块不取；块数有上限）；
   对抗输入下仍是线性的；
2. 没有接收方登记时，发送路径逐字不变，连取块的函数都不调；
3. 只对酒馆兼容开着、人设来自酒馆卡、带 `ctx` 的发送取；被输出过滤改过的那次发送不交；
   剥完只剩变量块时照样交；
4. 接收方出错、超时、自己取消，发送照常；取块出错，发送照常；
5. 四项新配置的默认值、覆盖与范围。
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

from nekro_agent.adapters.interface.schemas.platform import PlatformSendRequest, PlatformSendResponse
from nekro_agent.core.config import STATE_COMPAT_PARSE_DEFAULT, CoreConfig, config
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.chat import universal_chat_service as ucs_mod
from nekro_agent.services.chat.universal_chat_service import universal_chat_service
from nekro_agent.services.output_filter import service
from nekro_agent.services.output_filter.rules import compile_rules as compile_filter_rules
from nekro_agent.services.output_filter.service import FilterSettings
from nekro_agent.services.tavern import cleanup_runtime, variable_blocks
from nekro_agent.services.tavern.cleanup_runtime import (
    MAX_EXTRACTED_BLOCK_CHARS,
    MAX_EXTRACTED_BLOCKS,
    extract_variable_blocks,
    strip_variable_markup,
)

CHAT_KEY = "onebot_v11-inst1-group_20"
PATCH = '[{"op": "replace", "path": "/时间", "value": "傍晚"}]'
INNER = f"<Analysis>time advanced</Analysis>\n<JSONPatch>{PATCH}</JSONPatch>"
VARIABLE_BLOCK = f"<UpdateVariable>\n{INNER}\n</UpdateVariable>"
# 发送路径先去掉思考与分析区域再取块：块里的 <Analysis> 一起去掉（兼容解析本来也不看它）
SENT_INNER = f"<JSONPatch>{PATCH}</JSONPatch>"


# ---------------------------------------------------------------------------
# 取块
# ---------------------------------------------------------------------------


class TestExtract:
    def test_outer_block_only(self) -> None:
        assert extract_variable_blocks(f"她点点头。\n{VARIABLE_BLOCK}") == [INNER]

    def test_standalone_patch_block(self) -> None:
        assert extract_variable_blocks(f"好。<JSONPatch>{PATCH}</JSONPatch>") == [PATCH]

    def test_two_blocks_in_order(self) -> None:
        text = "<JSONPatch>[1]</JSONPatch> 中间 <VariableUpdate>_.set('a', 2);</VariableUpdate>"

        assert extract_variable_blocks(text) == ["[1]", "_.set('a', 2);"]

    @pytest.mark.parametrize(
        "text",
        [
            "<initvar>a: 1</initvar>",
            "<status_current_variable>{}</status_current_variable>",
            f"<initvar><JSONPatch>{PATCH}</JSONPatch></initvar>",
            "<StatusPlaceHolderImpl/>",
            "没有任何标签",
            "<JSONPatch/>",
        ],
    )
    def test_non_update_blocks_are_not_taken(self, text: str) -> None:
        assert extract_variable_blocks(text, card_mode=True) == []

    def test_unclosed_tail_only_in_card_mode(self) -> None:
        text = "正文。\n<UpdateVariable>\n_.set('a', 1);"

        assert extract_variable_blocks(text) == []
        assert extract_variable_blocks(text, card_mode=True) == ["_.set('a', 1);"]

    def test_pairs_inside_the_cut_are_not_taken_twice(self) -> None:
        text = "x <UpdateVariable>A <JSONPatch>[1]</JSONPatch>"

        assert extract_variable_blocks(text, card_mode=True) == ["A <JSONPatch>[1]</JSONPatch>"]

    def test_cut_by_a_non_update_tag_takes_nothing_after_it(self) -> None:
        text = "x <initvar>没闭合 <JSONPatch>[1]</JSONPatch>"

        assert extract_variable_blocks(text, card_mode=True) == []

    @pytest.mark.parametrize(
        "text",
        [
            f"开头\n{VARIABLE_BLOCK}\n结尾",
            "<JSONPatch>[1]</JSONPatch>a<JSON_Patch>[2]</json_patch>",
            "x <UpdateVariable>A <JSONPatch>[1]</JSONPatch>",
            f"<UpdateVariable>{INNER}</UpdateVariable></UpdateVariable>落单的结束标签",
        ],
    )
    def test_taken_bodies_are_exactly_what_strip_removes(self, text: str) -> None:
        stripped = strip_variable_markup(text, card_mode=True)
        bodies = extract_variable_blocks(text, card_mode=True)

        assert bodies
        for body in bodies:
            assert body in text
            assert body not in stripped

    def test_oversized_block_is_dropped_whole(self) -> None:
        big = "x" * (MAX_EXTRACTED_BLOCK_CHARS + 1)
        text = f"<JSONPatch>{big}</JSONPatch><JSONPatch>[2]</JSONPatch>"

        assert extract_variable_blocks(text) == ["[2]"]

    def test_block_count_is_capped(self) -> None:
        text = "".join(f"<JSONPatch>[{index}]</JSONPatch>" for index in range(MAX_EXTRACTED_BLOCKS + 3))

        assert extract_variable_blocks(text) == [f"[{index}]" for index in range(MAX_EXTRACTED_BLOCKS)]

    @pytest.mark.parametrize(
        "make",
        [
            lambda: "<UpdateVariable>" * 60_000,
            lambda: "</JSONPatch>" * 60_000,
            lambda: "<UpdateVariable " * 60_000,
            lambda: "<JSONPatch>[" * 40_000 + "</JSONPatch>" * 40_000,
            lambda: "<initvar>" + "<JSONPatch>x" * 60_000,
        ],
        ids=["openers", "closers", "unterminated", "deep-nesting", "cut-then-pairs"],
    )
    def test_linear_on_adversarial_text(self, make: Any) -> None:
        text = make()
        # 量本进程的 CPU 时间而不是墙上时间：机器忙时墙上时间会被别的进程拖长。
        # 退化成平方级时这里是几分钟的量级，门限放宽到 10 秒照样拦得住。
        started = time.process_time()
        extract_variable_blocks(text, card_mode=True)
        assert time.process_time() - started < 10.0


class TestMaskReasoning:
    @pytest.mark.parametrize(
        ("text", "kept"),
        [
            ("前<think>想</think>后", "前后"),
            ("前<Analysis x='1'>析</analysis>后", "前后"),
            ("<think>外<analysis>内</analysis>还在外</think>后", "后"),
            ("<think>没配上的<analysis>一起去掉</think>后", "后"),
            ("前</think>落单的结束标签不算", "前</think>落单的结束标签不算"),
            ("前<reasoning>没闭合的去到末尾", "前"),
        ],
    )
    def test_regions(self, text: str, kept: str) -> None:
        assert cleanup_runtime.mask_reasoning_regions(text) == kept

    @pytest.mark.parametrize(
        "make",
        [
            lambda: "<think>" * 9_000 + "</analysis>" * 6_000,
            lambda: "<think>" * 4_500 + "</think>" * 4_500,
            lambda: "<think><analysis>" * 4_000 + "</think>" * 4_000,
            lambda: "<thinking " * 12_000,
        ],
        ids=["opens-then-unmatched-closes", "nested-pairs", "interleaved", "unterminated"],
    )
    def test_linear_on_64kb(self, make: Any) -> None:
        text = make()
        assert len(text) >= 64_000
        cleanup_runtime.mask_reasoning_regions("<think>预热</think>")
        started = time.perf_counter()
        cleanup_runtime.mask_reasoning_regions(text)
        assert time.perf_counter() - started < 0.05


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sinks() -> Any:
    variable_blocks.clear_variable_block_sinks()
    yield
    variable_blocks.clear_variable_block_sinks()


def _payload(*blocks: str) -> variable_blocks.VariableBlocks:
    return variable_blocks.VariableBlocks(
        one_time_code="code1", from_chat_key=CHAT_KEY, chat_key=CHAT_KEY, blocks=tuple(blocks)
    )


class TestDeliver:
    async def test_every_sink_gets_the_blocks(self) -> None:
        seen: list[tuple[str, tuple[str, ...]]] = []

        async def first(payload: variable_blocks.VariableBlocks) -> None:
            seen.append(("first", payload.blocks))

        async def second(payload: variable_blocks.VariableBlocks) -> None:
            seen.append(("second", payload.blocks))

        variable_blocks.register_variable_block_sink("a", first)
        variable_blocks.register_variable_block_sink("b", second)

        await variable_blocks.deliver(_payload("[1]"))

        assert seen == [("first", ("[1]",)), ("second", ("[1]",))]

    async def test_failing_slow_and_self_cancelling_sinks_do_not_raise(self) -> None:
        seen: list[str] = []

        async def boom(_payload: variable_blocks.VariableBlocks) -> None:
            raise RuntimeError("坏了")

        async def slow(_payload: variable_blocks.VariableBlocks) -> None:
            await asyncio.sleep(30)

        async def cancels(_payload: variable_blocks.VariableBlocks) -> None:
            raise asyncio.CancelledError

        async def last(_payload: variable_blocks.VariableBlocks) -> None:
            seen.append("last")

        for owner, sink in (("boom", boom), ("slow", slow), ("cancels", cancels), ("last", last)):
            variable_blocks.register_variable_block_sink(owner, sink)

        started = time.perf_counter()
        await variable_blocks.deliver(_payload("[1]"))

        assert seen == ["last"]
        assert time.perf_counter() - started < variable_blocks.SINK_TIMEOUT_SECONDS + 1.0

    async def test_nothing_to_deliver_calls_nobody(self) -> None:
        async def forbidden(_payload: variable_blocks.VariableBlocks) -> None:
            raise AssertionError("没有块不该调")

        variable_blocks.register_variable_block_sink("x", forbidden)

        await variable_blocks.deliver(_payload())

    async def test_an_inactive_sink_counts_as_unregistered(self) -> None:
        seen: list[str] = []
        switch = {"on": False}

        async def sink(_payload: variable_blocks.VariableBlocks) -> None:
            seen.append("called")

        def broken() -> bool:
            raise RuntimeError("坏了")

        variable_blocks.register_variable_block_sink("state", sink, active=lambda: switch["on"])
        variable_blocks.register_variable_block_sink("broken", sink, active=broken)

        assert not variable_blocks.has_sinks()  # 插件关着（或判不出来）时发送路径不取块
        await variable_blocks.deliver(_payload("[1]"))
        assert seen == []

        switch["on"] = True
        assert variable_blocks.has_sinks()
        await variable_blocks.deliver(_payload("[1]"))
        assert seen == ["called"]

    def test_the_state_plugin_registers_its_sink_behind_its_switch(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "plugins/builtin/state_table/inline.py").read_text(
            encoding="utf-8"
        )

        assert (
            "register_variable_block_sink(plugin.key, on_variable_blocks, active=lambda: plugin.is_enabled)" in source
        )

    def test_register_replace_unregister(self) -> None:
        async def sink(_payload: variable_blocks.VariableBlocks) -> None:
            return None

        assert not variable_blocks.has_sinks()
        variable_blocks.register_variable_block_sink("x", sink)
        variable_blocks.register_variable_block_sink("x", sink)
        assert variable_blocks.has_sinks()
        variable_blocks.unregister_variable_block_sink("x")
        variable_blocks.unregister_variable_block_sink("x")
        assert not variable_blocks.has_sinks()


# ---------------------------------------------------------------------------
# 发送路径
# ---------------------------------------------------------------------------


class _CaptureAdapter:
    def __init__(self) -> None:
        self.requests: List[PlatformSendRequest] = []

    async def forward_message(self, request: PlatformSendRequest) -> PlatformSendResponse:
        self.requests.append(request)
        return PlatformSendResponse(success=True, message_id="out1", recorded=False)


class _Recorder:
    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def push_bot_message(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest.fixture(autouse=True)
def _send_path(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    from nekro_agent.services import message_service as message_service_mod

    async def _ok(_chat_key: str) -> None:
        return None

    async def _no_rules(_chat_key: str) -> None:
        return None

    async def _no_filter(_chat_key: str) -> Optional[FilterSettings]:
        return None

    async def _noop(_chat_key: str, _hits: Counter[str]) -> None:
        return None

    recorder = _Recorder()
    monkeypatch.setattr(ucs_mod.UniversalChatService, "_assert_instance_active", staticmethod(_ok))
    monkeypatch.setattr(ucs_mod, "rules_for_chat", _no_rules)
    monkeypatch.setattr(service, "settings_for_chat", _no_filter)
    monkeypatch.setattr(service, "record_hits_for_chat", _noop)
    monkeypatch.setattr(message_service_mod, "message_service", recorder)
    monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
    cleanup_runtime._compiled_cache.clear()  # noqa: SLF001
    return recorder


def _card(monkeypatch: pytest.MonkeyPatch, is_card: bool) -> list[str]:
    lookups: list[str] = []

    async def _is_card(chat_key: str) -> bool:
        lookups.append(chat_key)
        return is_card

    monkeypatch.setattr(ucs_mod, "is_card_preset_chat", _is_card)
    return lookups


def _ctx(adapter: _CaptureAdapter, *, chat_type: ChatType = ChatType.GROUP, channel_key: str = CHAT_KEY) -> Any:
    channel = SimpleNamespace(chat_key=channel_key, instance_key="inst1", chat_type=chat_type)
    return SimpleNamespace(
        adapter=adapter, one_time_code="code1", from_chat_key=CHAT_KEY, chat_key=CHAT_KEY, db_chat_channel=channel
    )


def _sink() -> list[variable_blocks.VariableBlocks]:
    seen: list[variable_blocks.VariableBlocks] = []

    async def sink(payload: variable_blocks.VariableBlocks) -> None:
        seen.append(payload)

    variable_blocks.register_variable_block_sink("state", sink)
    return seen


async def _send(adapter: _CaptureAdapter, text: str, *, ctx: Any) -> None:
    await universal_chat_service.send_agent_message(
        chat_key=CHAT_KEY,
        messages=text,
        adapter=adapter,  # type: ignore[arg-type]
        ctx=ctx,
        record=True,
    )


class TestSendPath:
    async def test_without_a_sink_nothing_is_taken_and_text_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, _send_path: _Recorder
    ) -> None:
        calls: list[str] = []

        def _spy(text: str, **_kwargs: Any) -> list[str]:
            calls.append(text)
            return []

        monkeypatch.setattr(ucs_mod, "extract_variable_blocks", _spy)
        lookups = _card(monkeypatch, True)
        adapter = _CaptureAdapter()
        text = f"好的，这就出发。\n{VARIABLE_BLOCK}"

        await _send(adapter, text, ctx=_ctx(adapter))

        assert adapter.requests[0].segments[0].content == strip_variable_markup(text) == "好的，这就出发。"
        assert calls == []  # 没有接收方：连取块的函数都不调
        assert lookups == []  # 成对的块剥完就没剩标签，连人设都不查

    async def test_blocks_go_to_the_sink_for_card_presets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _card(monkeypatch, True)
        seen = _sink()
        adapter = _CaptureAdapter()

        await _send(adapter, f"好的。\n{VARIABLE_BLOCK}", ctx=_ctx(adapter))

        assert adapter.requests[0].segments[0].content == "好的。"
        assert [
            (item.one_time_code, item.from_chat_key, item.chat_key, item.blocks, item.instance_key, item.is_private)
            for item in seen
        ] == [("code1", CHAT_KEY, CHAT_KEY, (SENT_INNER,), "inst1", False)]

    async def test_private_chats_are_marked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _card(monkeypatch, True)
        seen = _sink()
        adapter = _CaptureAdapter()

        await _send(adapter, f"好的。\n{VARIABLE_BLOCK}", ctx=_ctx(adapter, chat_type=ChatType.PRIVATE))

        assert [(item.instance_key, item.is_private) for item in seen] == [("inst1", True)]

    async def test_channel_is_looked_up_when_ctx_holds_another(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _card(monkeypatch, True)
        seen = _sink()
        looked_up: list[str] = []
        found: dict[str, Any] = {
            "row": SimpleNamespace(chat_key=CHAT_KEY, instance_key="inst9", chat_type=ChatType.GROUP)
        }

        class _Query:
            def __init__(self, chat_key: str) -> None:
                looked_up.append(chat_key)

            def order_by(self, *_fields: str) -> _Query:
                return self

            async def first(self) -> Any:
                return found["row"]

        monkeypatch.setattr(ucs_mod.DBChatChannel, "filter", lambda chat_key: _Query(chat_key))
        adapter = _CaptureAdapter()

        await _send(adapter, f"好的。\n{VARIABLE_BLOCK}", ctx=_ctx(adapter, channel_key="another"))
        found["row"] = None
        await _send(adapter, f"好的。\n{VARIABLE_BLOCK}", ctx=_ctx(adapter, channel_key="another"))

        assert looked_up == [CHAT_KEY, CHAT_KEY]
        assert [item.instance_key for item in seen] == ["inst9"]  # 查不到频道就不交

    async def test_blocks_inside_thinking_are_not_taken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _card(monkeypatch, True)
        seen = _sink()
        adapter = _CaptureAdapter()
        text = (
            "<think>想一想<UpdateVariable>_.set('a', 1);</UpdateVariable></think>"
            "好的。<UpdateVariable>_.set('b', 2);</UpdateVariable>"
        )

        await _send(adapter, text, ctx=_ctx(adapter))

        assert [item.blocks for item in seen] == [("_.set('b', 2);",)]

    async def test_unclosed_tail_is_taken_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _card(monkeypatch, True)
        seen = _sink()
        adapter = _CaptureAdapter()

        await _send(adapter, "回复。\n<UpdateVariable>\n_.set('a', 1);", ctx=_ctx(adapter))

        assert adapter.requests[0].segments[0].content == "回复。"
        assert [item.blocks for item in seen] == [("_.set('a', 1);",)]

    async def test_not_taken_for_other_presets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _card(monkeypatch, False)
        seen = _sink()
        adapter = _CaptureAdapter()

        await _send(adapter, f"好的。\n{VARIABLE_BLOCK}", ctx=_ctx(adapter))

        assert adapter.requests[0].segments[0].content == "好的。"
        assert seen == []

    async def test_not_taken_when_compat_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", False)
        _card(monkeypatch, True)
        seen = _sink()
        adapter = _CaptureAdapter()
        text = f"好的。\n{VARIABLE_BLOCK}"

        await _send(adapter, text, ctx=_ctx(adapter))

        assert adapter.requests[0].segments[0].content == text
        assert seen == []

    async def test_not_taken_without_a_round_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def _spy(text: str, **_kwargs: Any) -> list[str]:
            calls.append(text)
            return []

        monkeypatch.setattr(ucs_mod, "extract_variable_blocks", _spy)
        _card(monkeypatch, True)
        seen = _sink()
        adapter = _CaptureAdapter()
        ctx = _ctx(adapter)
        ctx.one_time_code = ""

        await _send(adapter, f"好的。\n{VARIABLE_BLOCK}", ctx=ctx)

        assert adapter.requests[0].segments[0].content == "好的。"  # 照样剥
        assert calls == [] and seen == []

    async def test_not_taken_without_ctx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _card(monkeypatch, True)
        seen = _sink()
        adapter = _CaptureAdapter()

        await _send(adapter, f"好的。\n{VARIABLE_BLOCK}", ctx=None)

        assert seen == []

    async def test_not_delivered_when_the_output_filter_changed_the_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _settings(_chat_key: str) -> FilterSettings:
            return FilterSettings(mode="fragment", replacement="**", rules=compile_filter_rules("出发", ""))

        monkeypatch.setattr(service, "settings_for_chat", _settings)
        _card(monkeypatch, True)
        seen = _sink()
        adapter = _CaptureAdapter()

        await _send(adapter, f"这就出发。\n{VARIABLE_BLOCK}", ctx=_ctx(adapter))

        assert "出发" not in adapter.requests[0].segments[0].content
        assert seen == []

    async def test_block_only_reply_is_still_delivered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _card(monkeypatch, True)
        seen = _sink()
        adapter = _CaptureAdapter()

        await _send(adapter, VARIABLE_BLOCK, ctx=_ctx(adapter))

        assert adapter.requests == []
        assert [item.blocks for item in seen] == [(SENT_INNER,)]

    async def test_sink_failure_and_slowness_do_not_break_sending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _card(monkeypatch, True)

        async def boom(_payload: variable_blocks.VariableBlocks) -> None:
            raise RuntimeError("坏了")

        async def slow(_payload: variable_blocks.VariableBlocks) -> None:
            await asyncio.sleep(30)

        variable_blocks.register_variable_block_sink("boom", boom)
        variable_blocks.register_variable_block_sink("slow", slow)
        adapter = _CaptureAdapter()

        started = time.perf_counter()
        await _send(adapter, f"好的。\n{VARIABLE_BLOCK}", ctx=_ctx(adapter))

        assert adapter.requests[0].segments[0].content == "好的。"
        assert time.perf_counter() - started < variable_blocks.SINK_TIMEOUT_SECONDS + 1.5

    async def test_extractor_failure_does_not_break_sending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args: Any, **_kwargs: Any) -> list[str]:
            raise RuntimeError("坏了")

        monkeypatch.setattr(ucs_mod, "extract_variable_blocks", _boom)
        _card(monkeypatch, True)
        seen = _sink()
        adapter = _CaptureAdapter()

        await _send(adapter, f"好的。\n{VARIABLE_BLOCK}", ctx=_ctx(adapter))

        assert adapter.requests[0].segments[0].content == "好的。"
        assert seen == []


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


class TestConfigItems:
    @staticmethod
    def _extra(name: str) -> dict:
        extra = CoreConfig.model_fields[name].json_schema_extra
        assert isinstance(extra, dict)
        return extra

    def test_compat_default_comes_from_one_constant(self) -> None:
        assert STATE_COMPAT_PARSE_DEFAULT == "private"
        assert CoreConfig.model_fields["STATE_COMPAT_PARSE"].default == STATE_COMPAT_PARSE_DEFAULT
        assert CoreConfig().STATE_COMPAT_PARSE == STATE_COMPAT_PARSE_DEFAULT

    @pytest.mark.parametrize("name", ["STATE_COMPAT_PARSE", "STATE_IMAGE_SEND_MODE", "STATE_IMAGE_EVERY_N"])
    def test_tenant_settable_overridable_items(self, name: str) -> None:
        extra = self._extra(name)

        assert extra.get("overridable") is True
        assert not extra.get("tenant_may_only_lower")
        assert not extra.get("platform_only")

    def test_minimum_interval_is_global_only(self) -> None:
        assert not self._extra("STATE_IMAGE_MIN_N").get("overridable")
        assert CoreConfig.model_fields["STATE_IMAGE_MIN_N"].default == 1

    def test_defaults_and_ranges(self) -> None:
        defaults = CoreConfig()
        assert (defaults.STATE_IMAGE_SEND_MODE, defaults.STATE_IMAGE_EVERY_N) == ("command", 10)
        for name in ("STATE_IMAGE_EVERY_N", "STATE_IMAGE_MIN_N"):
            CoreConfig.model_validate({name: 1})
            CoreConfig.model_validate({name: 1000})
            with pytest.raises(ValueError):
                CoreConfig.model_validate({name: 0})
        for name, bad in (("STATE_COMPAT_PARSE", "group"), ("STATE_IMAGE_SEND_MODE", "always")):
            with pytest.raises(ValueError):
                CoreConfig.model_validate({name: bad})
