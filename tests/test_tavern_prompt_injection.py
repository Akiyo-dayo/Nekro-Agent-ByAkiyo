"""酒馆运行时在提示词里的落点：辅助提示词、后置指令、深度注入、世界书宏与明细

这里用真模板渲染，只把查库与外部服务换成替身。钉的是**位置与文字**：

- 辅助提示词在人设之后、世界书「人设之后」之前，空着时一个字符都不多；
- 后置指令在整段聊天记录之后，迭代的增量历史里不再出现；
- 角色备注按深度的同一套下标规则插入，同深度时排在世界书块之前；
- 世界书条目里的宏先展开、不可信组再清洗；
- 槽位关着时三段都不出现，明细只有字数、层名与状态。
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.agent import round_prompt as round_prompt_module
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.agent.templates.compiler import PromptCompiler
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.tavern.macros import MacroContext
from nekro_agent.services.tavern.runtime import TavernRound
from nekro_agent.services.tavern.slots import AdvancedSlots, SlotText
from nekro_agent.services.worldbook import render as wb_render
from nekro_agent.services.worldbook import runtime as wb_runtime
from nekro_agent.services.worldbook import selection
from nekro_agent.services.worldbook.types import MatchEntry, Position

CODE = "tp0001"
CHAT_KEY = "onebot_v11-group_1"


class _FakeDbMessage:
    is_system = False

    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        self.ext_data_obj = SimpleNamespace(ref_msg_id="")

    def parse_content_data(self) -> list[Any]:
        return []

    def parse_chat_history_prompt(self, *_args: Any, **_kwargs: Any) -> str:
        return f"[{self.message_id}]"


class _FakeChannel:
    chat_key = CHAT_KEY
    conversation_start_time = dt.datetime(2000, 1, 1)
    workspace_id = None
    adapter_key = "onebot_v11"
    instance_key = "i1"
    channel_name = "测试群"
    channel_type = "group"

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg
        self.adapter = SimpleNamespace(
            chat_key_rules=["group_<id>"],
            config=SimpleNamespace(SESSION_ENABLE_AT=False),
        )

    async def get_preset(self) -> Any:
        return _preset()

    async def get_effective_config(self) -> Any:
        return self._cfg


class _FakeConfig:
    AI_CHAT_CONTEXT_EXPIRE_SECONDS = 0
    AI_CHAT_CONTEXT_MAX_LENGTH = 10
    AI_SYSTEM_NOTIFY_WINDOW_SIZE = 0
    AI_SYSTEM_NOTIFY_LIMIT = 0
    AI_VISION_IMAGE_LIMIT = 10
    AI_VISION_IMAGE_SIZE_LIMIT_KB = 1024 * 1024
    MEMORY_ENABLE_ENHANCED_RETRIEVAL = False
    AI_ALWAYS_INCLUDE_MSG_ID = False
    AI_CONTEXT_LENGTH_PER_SESSION = 100000
    PROMPT_ADVANCED_SLOTS_ENABLED = True
    USE_MODEL_GROUP = "default"
    MODEL_GROUPS = {
        "default": SimpleNamespace(
            CHAT_MODEL="fake-model",
            BASE_URL="https://example.invalid/v1",
            ENABLE_VISION=False,
            ENABLE_COT=False,
            PREFILL_MODE="assistant",
        ),
    }


def _preset() -> Any:
    return SimpleNamespace(id=1, name="默认人设", content="你是{{char}}。")


def _card() -> Any:
    return SimpleNamespace(
        nickname="薄荷",
        macros_enabled=True,
        depth_prompt="深度里的 {{char}}",
        depth_prompt_depth=2,
        post_history="",
        prefill="",
    )


def _hit(entry_id: int, *, content: str, trusted: bool, position: Position, depth: int = 4) -> selection.Hit:
    from nekro_agent.services.worldbook import matcher

    return selection.Hit(
        entry=MatchEntry(
            id=entry_id,
            book_id=1,
            keys=(),
            secondary_keys=(),
            selective_logic=None,
            use_regex=False,
            constant=True,
            content=content,
            order=100,
            position=position,
            depth=depth,
            scan_depth=4,
            case_sensitive=False,
            match_whole_words=False,
            sticky=0,
            cooldown=0,
            delay=0,
            trusted=trusted,
            invalid_keys=(),
        ),
        reason=matcher.HitReason(kind="constant"),
    )


def _round(
    *,
    depth_prompt: str = "",
    depth: int = 0,
    post_history: str = "",
    auxiliary: str = "",
    prefill: str = "",
    slots: AdvancedSlots | None = None,
) -> TavernRound:
    return TavernRound(
        enabled=True,
        card=None,
        macro=MacroContext(
            char_name="薄荷",
            user_name="阿明",
            now=dt.datetime(2026, 1, 2),
            chat_key=CHAT_KEY,
            preset_id=1,
        ),
        persona_content="你是薄荷。",
        depth_prompt=depth_prompt,
        depth=depth,
        auxiliary=auxiliary,
        post_history=post_history,
        prefill=SimpleNamespace(text=prefill, state="pending") if prefill else None,
        slots=slots,
    )


def _slots(*, enabled: bool, post: str = "", auxiliary: str = "", prefill: str = "") -> AdvancedSlots:
    layers = ["system"]
    return AdvancedSlots(
        enabled=enabled,
        post_history=SlotText(text=post, layers=layers if post else []),
        auxiliary=SlotText(text=auxiliary, layers=layers if auxiliary else []),
        prefill=SlotText(text=prefill, layers=layers if prefill else []),
    )


def _fake_message_query(monkeypatch: pytest.MonkeyPatch, message_ids: tuple[str, ...]) -> None:
    messages = [_FakeDbMessage(message_id) for message_id in message_ids]
    descending = list(reversed(messages))

    def fake_filter(*_args: Any, **_kwargs: Any) -> Any:
        class _QS:
            def order_by(self, *_a: Any, **_k: Any) -> "_QS":
                return self

            def limit(self, *_a: Any, **_k: Any) -> "_QS":
                return self

            async def __aiter__(self) -> Any:
                for message in descending:
                    yield message

            def __await__(self) -> Any:
                async def _inner() -> list[_FakeDbMessage]:
                    return descending

                return _inner().__await__()

        return _QS()

    async def empty_memory(**_kwargs: Any) -> str:
        return ""

    monkeypatch.setattr(history_module.DBChatMessage, "filter", staticmethod(fake_filter))
    monkeypatch.setattr(history_module, "_inject_memory_context", empty_memory)
    monkeypatch.setattr(history_module, "is_domain_scope_enabled", lambda: False)
    monkeypatch.setattr(history_module, "supports_vision", lambda *_a, **_k: False)


async def _render_history(
    monkeypatch: pytest.MonkeyPatch,
    *,
    code: str,
    inject_worldbook: bool = True,
    plugin_depth_blocks: list[tuple[int, str]] | None = None,
) -> str:
    _fake_message_query(monkeypatch, ("m1", "m2", "m3"))
    rendered = await history_module.render_history_data(
        chat_key=CHAT_KEY,
        db_chat_channel=_FakeChannel(_FakeConfig()),  # type: ignore[arg-type]
        one_time_code=code,
        config=_FakeConfig(),  # type: ignore[arg-type]
        model_group=_FakeConfig.MODEL_GROUPS["default"],  # type: ignore[arg-type]
        inject_worldbook=inject_worldbook,
        plugin_depth_blocks=plugin_depth_blocks,
    )
    return str(rendered.to_dict()["content"])


def _compiler(*, auxiliary: str = "", worldbook: str = "", chat_preset: str = "你是薄荷。") -> PromptCompiler:
    return PromptCompiler(
        platform_name="qq",
        bot_platform_id="10001",
        chat_preset=chat_preset,
        plugins_prompt="",
        plugins_runtime_prompt="",
        plugin_activation_rules="",
        plugin_call_priority_rules="",
        enable_cot=False,
        chat_key_rules="",
        enable_at=False,
        auxiliary_prompt=auxiliary,
        worldbook_prompt=worldbook,
    )


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    tavern_runtime._rounds.clear()  # noqa: SLF001
    wb_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001
    yield
    tavern_runtime._rounds.clear()  # noqa: SLF001
    wb_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001


class TestAuxiliaryPrompt:
    def test_sits_after_persona_and_before_worldbook(self) -> None:
        content = str(
            _compiler(
                auxiliary="辅助正文",
                worldbook="## World Info\n宿主世界书",
                chat_preset="你是薄荷，说话简洁。",
            )
            .render_system_message()
            .to_dict()["content"],
        )

        assert content.index("你是薄荷") < content.index("## Additional Instructions")
        assert content.index("## Additional Instructions") < content.index("辅助正文")
        assert content.index("辅助正文") < content.index("宿主世界书")

    def test_empty_is_byte_identical(self) -> None:
        baseline = str(_compiler(worldbook="## World Info\n宿主").render_system_message().to_dict()["content"])
        with_empty = str(
            _compiler(auxiliary="", worldbook="## World Info\n宿主").render_system_message().to_dict()["content"],
        )

        assert with_empty == baseline


class TestPostHistory:
    async def test_appended_after_the_whole_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tavern_runtime._rounds[CODE] = _round(post_history="后置正文")  # noqa: SLF001

        content = await _render_history(monkeypatch, code=CODE)

        assert f"\n<{CODE} | message separator>\n<{CODE} | PostHistoryInstructions source:host>" in content
        assert f"后置正文\n<{CODE} | /PostHistoryInstructions>" in content
        assert content.rstrip().endswith(f"<{CODE} | /PostHistoryInstructions>")

    async def test_iteration_render_does_not_repeat_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tavern_runtime._rounds[CODE] = _round(post_history="后置正文")  # noqa: SLF001

        first = await _render_history(monkeypatch, code=CODE)
        again = await _render_history(monkeypatch, code=CODE, inject_worldbook=False)

        assert "PostHistoryInstructions" in first
        assert "PostHistoryInstructions" not in again


class TestPluginDepthBlocks:
    async def test_inserted_before_the_last_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        content = await _render_history(monkeypatch, code=CODE, plugin_depth_blocks=[(1, "插件状态块")])

        assert content.index("[m2]") < content.index("插件状态块") < content.index("[m3]")

    async def test_after_the_character_note_at_the_same_depth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tavern_runtime._rounds[CODE] = _round(depth_prompt="备注正文", depth=1)  # noqa: SLF001

        content = await _render_history(monkeypatch, code=CODE, plugin_depth_blocks=[(1, "插件状态块")])

        assert content.index("备注正文") < content.index("插件状态块") < content.index("[m3]")

    async def test_iteration_render_does_not_repeat_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = await _render_history(monkeypatch, code=CODE, plugin_depth_blocks=[(1, "插件状态块")])
        again = await _render_history(monkeypatch, code=CODE, inject_worldbook=False, plugin_depth_blocks=[(1, "插件状态块")])

        assert "插件状态块" in first
        assert "插件状态块" not in again

    async def test_no_blocks_is_byte_identical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        baseline = await _render_history(monkeypatch, code=CODE)
        with_empty = await _render_history(monkeypatch, code=CODE, plugin_depth_blocks=[])

        assert with_empty == baseline


class TestDepthNote:
    @pytest.mark.parametrize(
        ("depth", "expected_order"),
        [
            (0, ["[m1]", "[m2]", "[m3]", "CharacterNote"]),
            (2, ["[m1]", "CharacterNote", "[m2]", "[m3]"]),
            (99, ["CharacterNote", "[m1]", "[m2]", "[m3]"]),
        ],
    )
    async def test_lands_at_the_right_index(
        self,
        monkeypatch: pytest.MonkeyPatch,
        depth: int,
        expected_order: list[str],
    ) -> None:
        tavern_runtime._rounds[CODE] = _round(depth_prompt="备注正文", depth=depth)  # noqa: SLF001

        content = await _render_history(monkeypatch, code=CODE)
        positions = [content.index(token) for token in expected_order]

        assert positions == sorted(positions)
        assert f"<{CODE} | CharacterNote source:host>\n备注正文\n<{CODE} | /CharacterNote>" in content

    async def test_same_depth_note_precedes_the_worldbook_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tavern_runtime._rounds[CODE] = _round(depth_prompt="备注正文", depth=1)  # noqa: SLF001
        wb_runtime._rounds[CODE] = wb_runtime.RoundWorldbook(  # noqa: SLF001
            state="injected",
            groups=selection.RenderGroups(
                at_depth={1: [_hit(1, content="世界书深度", trusted=True, position=Position.AT_DEPTH, depth=1)]},
            ),
        )

        content = await _render_history(monkeypatch, code=CODE)

        assert content.index("CharacterNote") < content.index("世界书深度")

    async def test_iteration_render_does_not_repeat_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tavern_runtime._rounds[CODE] = _round(depth_prompt="备注正文", depth=1)  # noqa: SLF001

        first = await _render_history(monkeypatch, code=CODE)
        again = await _render_history(monkeypatch, code=CODE, inject_worldbook=False)

        assert "CharacterNote" in first
        assert "CharacterNote" not in again


class TestWorldbookMacros:
    def test_host_entry_macros_are_expanded(self) -> None:
        tavern_runtime._rounds[CODE] = _round()  # noqa: SLF001
        expand = tavern_runtime.expander(CODE)
        hits = [_hit(1, content="{{char}} 与 {{user}}", trusted=True, position=Position.AFTER_PERSONA)]

        block = wb_render.render_block(hits, CODE, in_history=False, expand=expand)

        assert "薄荷 与 阿明" in block
        assert "{{" not in block

    def test_untrusted_entry_is_expanded_before_sanitizing(self) -> None:
        tavern_runtime._rounds[CODE] = _round()  # noqa: SLF001
        expand = tavern_runtime.expander(CODE)
        hits = [_hit(1, content="{{char}}\u200b", trusted=False, position=Position.AFTER_PERSONA)]

        block = wb_render.render_block(hits, CODE, in_history=False, expand=expand)

        assert "薄荷" in block
        assert "\u200b" not in block

    def test_rendered_items_chars_follow_the_expanded_text(self) -> None:
        tavern_runtime._rounds[CODE] = _round()  # noqa: SLF001
        expand = tavern_runtime.expander(CODE)
        hits = [_hit(1, content="{{char}}", trusted=True, position=Position.AFTER_PERSONA)]

        items = wb_render.rendered_items(hits, expand=expand)

        assert items[0][2] == len("薄荷")


class TestGatedSlots:
    async def test_nothing_is_rendered_when_the_gate_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def fake_card(*_args: Any, **_kwargs: Any) -> Any:
            return _card()

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(enabled=False, post="关着的后置", auxiliary="关着的辅助", prefill="关着的预填")

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)

        prompt_rounds.begin(CODE, _FakeChannel(_FakeConfig()))
        tavern = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(_FakeConfig()),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            SimpleNamespace(sender_nickname="阿明"),
        )

        system = str(
            _compiler(auxiliary=tavern.auxiliary, chat_preset=tavern.persona_content)
            .render_system_message()
            .to_dict()["content"],
        )
        _fake_message_query(monkeypatch, ("m1",))
        history = await _render_history(monkeypatch, code=CODE)

        assert "Additional Instructions" not in system
        assert "关着的辅助" not in system
        assert "关着的后置" not in history
        assert "PostHistoryInstructions" not in history
        assert tavern.prefill is None

        items = prompt_rounds._rounds[CODE].tavern_items  # noqa: SLF001
        assert items["gate"] is False
        assert items["post_history"]["state"] == "gated"
        assert items["auxiliary"]["state"] == "gated"
        assert items["prefill"]["state"] == "gated"
        assert items["post_history"]["chars"] == len("关着的后置")
        assert "关着的后置" not in json.dumps(items, ensure_ascii=False)
        assert "关着的辅助" not in json.dumps(items, ensure_ascii=False)


class TestRoundDetail:
    async def test_states_words_and_no_raw_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def fake_card(*_args: Any, **_kwargs: Any) -> Any:
            return _card()

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(
                enabled=True,
                post="后置正文 {{char}}",
                auxiliary="辅助正文 {{user}}",
                prefill="预填正文 {{char}}",
            )

        async def fake_resolve(*_args: Any, **_kwargs: Any) -> wb_runtime.RoundWorldbook:
            return wb_runtime.RoundWorldbook(state="none", groups=selection.RenderGroups())

        async def empty_affection(**_kwargs: Any) -> str:
            return ""

        async def empty_plugins(*_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(system_prompt="", runtime_prompt="", depth_blocks=[])

        async def empty_plugins_resolve(**_kwargs: Any) -> list[Any]:
            return []

        async def no_scope(*_args: Any, **_kwargs: Any) -> None:
            return None

        adapter = SimpleNamespace(
            chat_key_rules=["group_<id>"],
            config=SimpleNamespace(SESSION_ENABLE_AT=False),
            set_dialog_example=_async_value([]),
            get_jinja_env=_async_value(None),
            get_self_info=_async_value(SimpleNamespace(platform_name="qq", user_id="10001")),
            render_runtime_prompt=_async_value(""),
        )
        ctx = SimpleNamespace(adapter=adapter, from_chat_key="")

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)
        monkeypatch.setattr(round_prompt_module, "wb_runtime", SimpleNamespace(resolve_round=fake_resolve))
        monkeypatch.setattr(round_prompt_module, "render_affection_prompt", empty_affection)
        monkeypatch.setattr(round_prompt_module, "schedule_affection_evaluation", lambda *_a, **_k: None)
        monkeypatch.setattr(round_prompt_module, "render_plugins_prompt", empty_plugins)
        monkeypatch.setattr(round_prompt_module, "resolve_active_plugins", empty_plugins_resolve)
        monkeypatch.setattr(round_prompt_module, "enforce_channel_model_group_scope", no_scope)
        _fake_message_query(monkeypatch, ("m1", "m2"))

        prompt_rounds.begin(CODE, _FakeChannel(_FakeConfig()))
        result = await round_prompt_module.build_round_prompt(
            chat_key=CHAT_KEY,
            db_chat_channel=_FakeChannel(_FakeConfig()),  # type: ignore[arg-type]
            ctx=ctx,  # type: ignore[arg-type]
            one_time_code=CODE,
            trigger_message=SimpleNamespace(sender_nickname="阿明"),
        )
        items = prompt_rounds._rounds[CODE].tavern_items  # noqa: SLF001

        assert "## Additional Instructions" in str(result.messages[0].to_dict()["content"])
        assert "PostHistoryInstructions" in str(result.messages[-1].to_dict()["content"])
        assert items["gate"] is True
        assert items["post_history"]["state"] == "sent"
        assert items["auxiliary"]["state"] == "sent"
        assert items["prefill"]["state"] == "pending"
        assert items["prefill"]["chars"] == len("预填正文 薄荷")
        assert items["depth_prompt"]["chars"] == len("深度里的 薄荷")
        assert items["macros"]["expanded"] >= 5
        dumped = json.dumps(items, ensure_ascii=False)
        for raw in ("后置正文", "辅助正文", "预填正文", "深度里的"):
            assert raw not in dumped


class TestMacroCountMatchesInjection:
    async def test_measuring_expansions_are_not_counted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """明细里的宏计数只算真正注入的那几处

        量字数（世界书条目的明细字数）与「槽位打开后会发什么」各自又展开一遍；这两遍
        原来也计数，关着的槽位里的宏会被算进来、世界书条目被算两次。
        """
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def fake_card(*_args: Any, **_kwargs: Any) -> Any:
            return _card()

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            # 关着：这里的两处宏一处都不该计入
            return _slots(enabled=False, post="关着 {{char}} {{user}}")

        groups = selection.RenderGroups(
            after_persona=[_hit(1, content="人设后 {{user}}", trusted=True, position=Position.AFTER_PERSONA)],
            at_depth={1: [_hit(2, content="深度条 {{char}}", trusted=True, position=Position.AT_DEPTH, depth=1)]},
        )

        async def fake_resolve(code: str, *_args: Any, **_kwargs: Any) -> wb_runtime.RoundWorldbook:
            result = wb_runtime.RoundWorldbook(state="injected", groups=groups)
            wb_runtime._rounds[code] = result  # noqa: SLF001
            return result

        async def empty_affection(**_kwargs: Any) -> str:
            return ""

        async def empty_plugins(*_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(system_prompt="", runtime_prompt="", depth_blocks=[])

        async def empty_plugins_resolve(**_kwargs: Any) -> list[Any]:
            return []

        async def no_scope(*_args: Any, **_kwargs: Any) -> None:
            return None

        adapter = SimpleNamespace(
            chat_key_rules=["group_<id>"],
            config=SimpleNamespace(SESSION_ENABLE_AT=False),
            set_dialog_example=_async_value([]),
            get_jinja_env=_async_value(None),
            get_self_info=_async_value(SimpleNamespace(platform_name="qq", user_id="10001")),
            render_runtime_prompt=_async_value(""),
        )
        ctx = SimpleNamespace(adapter=adapter, from_chat_key="")

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)
        monkeypatch.setattr(wb_runtime, "resolve_round", fake_resolve)
        monkeypatch.setattr(round_prompt_module, "render_affection_prompt", empty_affection)
        monkeypatch.setattr(round_prompt_module, "schedule_affection_evaluation", lambda *_a, **_k: None)
        monkeypatch.setattr(round_prompt_module, "render_plugins_prompt", empty_plugins)
        monkeypatch.setattr(round_prompt_module, "resolve_active_plugins", empty_plugins_resolve)
        monkeypatch.setattr(round_prompt_module, "enforce_channel_model_group_scope", no_scope)
        _fake_message_query(monkeypatch, ("m1", "m2", "m3"))

        prompt_rounds.begin(CODE, _FakeChannel(_FakeConfig()))
        result = await round_prompt_module.build_round_prompt(
            chat_key=CHAT_KEY,
            db_chat_channel=_FakeChannel(_FakeConfig()),  # type: ignore[arg-type]
            ctx=ctx,  # type: ignore[arg-type]
            one_time_code=CODE,
            trigger_message=SimpleNamespace(sender_nickname="阿明"),
        )
        items = prompt_rounds._rounds[CODE].tavern_items  # noqa: SLF001
        texts = "".join(str(message.to_dict()["content"]) for message in result.messages)

        # 真正注入的四处：人设正文、深度注入、「人设之后」一条、「深度」一条
        assert "人设后 阿明" in texts
        assert "深度条 薄荷" in texts
        assert "关着" not in texts
        assert items["macros"]["expanded"] == 4
        assert items["macros"]["guarded"] is False


def _async_value(value: Any) -> Any:
    async def _inner(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _inner
