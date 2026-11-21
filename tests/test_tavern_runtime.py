"""酒馆运行时的解析：闸、宏上下文、三个槽位、缓存与 fail-open

这些用例只跑 `resolve_tavern_round`，不拉起数据库：卡片行、最近一条消息与槽位解析都用
替身，因为这里要钉的是**判据与形状**，不是 SQL。

最关键的一条是闸关：`TAVERN_COMPAT_ENABLED` 关掉时必须一次表都不查。回滚闸的意义就是
「运行时不读卡片数据」，如果关掉以后还在查 `preset_card`，那这条闸只是把结果藏起来，
没有把读路径关掉。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.tavern.macros import MacroContext
from nekro_agent.services.tavern.slots import AdvancedSlots, SlotText

CODE = "tc0001"
CHAT_KEY = "onebot_v11-group_1"


class _FakeChannel:
    chat_key = CHAT_KEY
    adapter_key = "onebot_v11"
    instance_key = "i1"
    workspace_id = None

    async def get_preset(self) -> Any:
        return None

    async def get_effective_config(self) -> Any:
        return None


class _FakeConfig:
    PROMPT_ADVANCED_SLOTS_ENABLED = False


def _preset(*, preset_id: int | None = 1, name: str = "默认人设", content: str = "你是{{char}}，{{user}} 在跟你说话。") -> Any:
    if preset_id is None:
        return SimpleNamespace(name=name, content=content)
    return SimpleNamespace(id=preset_id, name=name, content=content)


def _card(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "nickname": "",
        "macros_enabled": True,
        "depth_prompt": "",
        "depth_prompt_depth": 4,
        "post_history": "",
        "prefill": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _slots(
    *,
    enabled: bool,
    post_history: str = "",
    auxiliary: str = "",
    prefill: str = "",
    layers: list[str] | None = None,
) -> AdvancedSlots:
    layer_names = layers or ["system"]
    return AdvancedSlots(
        enabled=enabled,
        post_history=SlotText(text=post_history, layers=layer_names if post_history else []),
        auxiliary=SlotText(text=auxiliary, layers=layer_names if auxiliary else []),
        prefill=SlotText(text=prefill, layers=layer_names if prefill else []),
    )


def _trigger(nickname: str) -> Any:
    return SimpleNamespace(sender_id="20001", sender_nickname=nickname)


@pytest.fixture(autouse=True)
def _clean_rounds() -> Any:
    tavern_runtime._rounds.clear()  # noqa: SLF001
    yield
    tavern_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001


class TestGateOff:
    async def test_gate_off_queries_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", False)
        card_queries: list[Any] = []
        message_queries: list[Any] = []
        slot_calls: list[Any] = []

        async def fake_card(*_args: Any, **_kwargs: Any) -> Any:
            card_queries.append(_args)
            return _card()

        def fake_filter(*_args: Any, **_kwargs: Any) -> Any:
            message_queries.append(_args)
            raise AssertionError("闸关时不该查消息")

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            slot_calls.append(_args)
            return _slots(enabled=True)

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
        monkeypatch.setattr(tavern_runtime.DBChatMessage, "filter", staticmethod(fake_filter))
        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)

        result = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("阿明"),
        )

        assert card_queries == []
        assert message_queries == []
        assert slot_calls == []
        assert result.enabled is False
        assert result.persona_content == "你是{{char}}，{{user}} 在跟你说话。"
        assert result.card is None and result.macro is None and result.slots is None
        assert result.depth_prompt == "" and result.depth == 0
        assert result.auxiliary == "" and result.post_history == "" and result.prefill is None


class TestPersona:
    async def test_no_card_keeps_persona_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def fake_card(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(enabled=False)

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)

        result = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("阿明"),
        )

        assert result.enabled is True
        assert result.card is None
        assert result.persona_content == "你是{{char}}，{{user}} 在跟你说话。"

    async def test_macros_enabled_expands_and_disabled_keeps_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(enabled=False)

        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)
        preset = _preset(name="默认人设")

        for enabled, expected in ((True, "你是薄荷，阿明 在跟你说话。"), (False, "你是{{char}}，{{user}} 在跟你说话。")):
            card = _card(nickname="薄荷", macros_enabled=enabled)

            async def fake_card(*_args: Any, _card=card, **_kwargs: Any) -> Any:
                return _card

            monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
            result = await tavern_runtime.resolve_tavern_round(
                CODE,
                _FakeChannel(),  # type: ignore[arg-type]
                _FakeConfig(),
                preset,
                _trigger("阿明"),
            )
            assert result.persona_content == expected
            tavern_runtime.discard_tavern_round(CODE)

    async def test_char_prefers_card_nickname(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def fake_card(*_args: Any, **_kwargs: Any) -> Any:
            return _card(nickname="昵称版", macros_enabled=True)

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(enabled=False)

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)

        result = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(name="人设名"),
            _trigger("阿明"),
        )

        assert result.persona_content.startswith("你是昵称版")


class TestUserName:
    async def test_trigger_nickname_wins_without_querying_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        queries: list[Any] = []

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(enabled=False)

        async def no_card(*_args: Any, **_kwargs: Any) -> None:
            return None

        def fake_filter(*_args: Any, **_kwargs: Any) -> Any:
            queries.append(_args)
            raise AssertionError("有触发人昵称时不该查消息")

        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)
        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(no_card))
        monkeypatch.setattr(tavern_runtime.DBChatMessage, "filter", staticmethod(fake_filter))

        result = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("阿明"),
        )

        assert queries == []
        assert result.macro is not None and result.macro.user_name == "阿明"

    async def test_falls_back_to_latest_non_bot_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        captured: dict[str, Any] = {}

        class _Query:
            def exclude(self, **kwargs: Any) -> "_Query":
                captured["exclude"] = kwargs
                return self

            def order_by(self, *args: Any) -> "_Query":
                captured["order_by"] = args
                return self

            async def first(self) -> Any:
                return SimpleNamespace(sender_nickname="最近的人")

        def fake_filter(**kwargs: Any) -> _Query:
            captured["filter"] = kwargs
            return _Query()

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(enabled=False)

        async def no_card(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)
        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(no_card))
        monkeypatch.setattr(tavern_runtime.DBChatMessage, "filter", staticmethod(fake_filter))

        result = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            None,
        )

        assert captured["filter"] == {"chat_key": CHAT_KEY}
        assert captured["exclude"] == {"sender_id": "-1"}
        assert result.macro is not None and result.macro.user_name == "最近的人"

    async def test_no_messages_uses_the_default_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        class _Query:
            def exclude(self, **_kwargs: Any) -> "_Query":
                return self

            def order_by(self, *_args: Any) -> "_Query":
                return self

            async def first(self) -> None:
                return None

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(enabled=False)

        async def no_card(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)
        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(no_card))
        monkeypatch.setattr(tavern_runtime.DBChatMessage, "filter", staticmethod(lambda **_kwargs: _Query()))

        result = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            None,
        )

        assert result.macro is not None and result.macro.user_name == "用户"

    async def test_user_name_is_sanitized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(enabled=False)

        async def no_card(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)
        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(no_card))

        result = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("<b>\u200b恶意\n名字</b>" + "长" * 40),
        )

        assert result.macro is not None
        user_name = result.macro.user_name
        assert len(user_name) <= 24
        assert "\n" not in user_name and "\u200b" not in user_name
        assert "<" not in user_name and ">" not in user_name


class TestSlotsAndDepth:
    async def test_slots_enabled_expands_all_three_and_builds_prefill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def fake_card(*_args: Any, **_kwargs: Any) -> Any:
            return _card(nickname="薄荷", depth_prompt="深度里的 {{char}}", depth_prompt_depth=2)

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(
                enabled=True,
                post_history="后置 {{char}}",
                auxiliary="辅助 {{user}}",
                prefill="预填 {{char}}",
                layers=["system", "channel"],
            )

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)

        result = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("阿明"),
        )

        assert result.auxiliary == "辅助 阿明"
        assert result.post_history == "后置 薄荷"
        assert result.prefill is not None and result.prefill.text == "预填 薄荷"
        assert result.depth_prompt == "深度里的 薄荷"
        assert result.depth == 2
        assert result.slots is not None and result.slots.enabled is True

    async def test_slots_disabled_leaves_all_three_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def fake_card(*_args: Any, **_kwargs: Any) -> Any:
            return _card(depth_prompt="", post_history="人设层后置", prefill="人设层预填")

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(
                enabled=False,
                post_history="关着的后置",
                auxiliary="关着的辅助",
                prefill="关着的预填",
            )

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)

        prompt_rounds.begin(CODE, _FakeChannel())
        result = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("阿明"),
        )
        draft = prompt_rounds._rounds[CODE]  # noqa: SLF001
        items = draft.tavern_items

        assert result.auxiliary == "" and result.post_history == "" and result.prefill is None
        assert items["gate"] is False
        assert items["post_history"]["state"] == "gated"
        assert items["post_history"]["chars"] == len("关着的后置")
        assert items["auxiliary"]["state"] == "gated"
        assert items["prefill"]["state"] == "gated"
        assert items["depth_prompt"] == {"chars": 0}

    async def test_enabled_but_empty_slots_are_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def fake_card(*_args: Any, **_kwargs: Any) -> None:
            return None

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(enabled=True)

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)

        prompt_rounds.begin(CODE, _FakeChannel())
        await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("阿明"),
        )
        items = prompt_rounds._rounds[CODE].tavern_items  # noqa: SLF001

        assert items["gate"] is True
        assert items["post_history"]["state"] == "empty"
        assert items["auxiliary"]["state"] == "empty"
        assert items["prefill"]["state"] == "empty"
        assert items["prefill"]["chars"] == 0


class TestFailOpen:
    async def test_card_query_failure_falls_back_to_gate_off_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)

        async def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("库抖动")

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(broken))

        result = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("阿明"),
        )

        assert result.enabled is False
        assert result.persona_content == "你是{{char}}，{{user}} 在跟你说话。"
        assert result.auxiliary == "" and result.post_history == "" and result.prefill is None


class TestCacheAndExpander:
    async def test_round_is_cached_and_discard_forces_a_reparse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        calls: list[Any] = []

        async def fake_card(*_args: Any, **_kwargs: Any) -> None:
            calls.append(_args)
            return None

        async def fake_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
            return _slots(enabled=False)

        monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(fake_card))
        monkeypatch.setattr(tavern_runtime, "resolve_slots", fake_slots)

        first = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("阿明"),
        )
        second = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("阿明"),
        )

        assert first is second
        assert len(calls) == 1
        assert tavern_runtime.cached_tavern_round(CODE) is first

        tavern_runtime.discard_tavern_round(CODE)
        assert tavern_runtime.cached_tavern_round(CODE) is None

        third = await tavern_runtime.resolve_tavern_round(
            CODE,
            _FakeChannel(),  # type: ignore[arg-type]
            _FakeConfig(),
            _preset(),
            _trigger("阿明"),
        )
        assert third is not first
        assert len(calls) == 2

    async def test_expander_uses_the_round_macro_context_and_is_identity_without_cache(self) -> None:
        identity = tavern_runtime.expander("missing")
        assert identity("{{char}} {{user}}") == "{{char}} {{user}}"

        tavern_runtime._rounds[CODE] = tavern_runtime.TavernRound(  # noqa: SLF001
            enabled=True,
            card=None,
            macro=MacroContext(char_name="薄荷", user_name="阿明", now=__import__("datetime").datetime(2026, 1, 2)),
            persona_content="",
            depth_prompt="",
            depth=0,
            auxiliary="",
            post_history="",
            prefill=None,
            slots=None,
        )
        expand = tavern_runtime.expander(CODE)

        assert expand("{{char}} 对 {{user}} 说") == "薄荷 对 阿明 说"

    async def test_cache_drops_the_oldest_half_when_full(self) -> None:
        template = tavern_runtime.TavernRound(
            enabled=False,
            card=None,
            macro=None,
            persona_content="",
            depth_prompt="",
            depth=0,
            auxiliary="",
            post_history="",
            prefill=None,
            slots=None,
        )
        for index in range(tavern_runtime.MAX_ROUNDS):
            tavern_runtime._rounds[f"old-{index}"] = template  # noqa: SLF001

        tavern_runtime._store_round("newest", template)  # noqa: SLF001

        assert len(tavern_runtime._rounds) == tavern_runtime.MAX_ROUNDS // 2 + 1  # noqa: SLF001
        assert "old-0" not in tavern_runtime._rounds  # noqa: SLF001
        assert "old-2047" not in tavern_runtime._rounds  # noqa: SLF001
        assert "old-2048" in tavern_runtime._rounds  # noqa: SLF001
        assert "newest" in tavern_runtime._rounds  # noqa: SLF001


class TestTavernDetail:
    async def test_macros_accumulate_and_states_overwrite_without_raw_text(self) -> None:
        prompt_rounds.begin(CODE, _FakeChannel())
        prompt_rounds.note_tavern(CODE, macros={"expanded": 2, "guarded": False}, gate=True)
        prompt_rounds.note_tavern(CODE, macros={"expanded": 3, "guarded": True}, auxiliary_state="sent")
        prompt_rounds.note_tavern(CODE, auxiliary_state="retried_without")
        items = prompt_rounds._rounds[CODE].tavern_items  # noqa: SLF001

        assert items["gate"] is True
        assert items["macros"] == {"expanded": 5, "guarded": True}
        assert items["auxiliary"] == {"state": "retried_without"}


def _round_with_macro() -> tavern_runtime.TavernRound:
    return tavern_runtime.TavernRound(
        enabled=True,
        card=None,
        macro=MacroContext(char_name="薄荷", user_name="阿明", now=__import__("datetime").datetime(2026, 1, 2)),
        persona_content="",
        depth_prompt="",
        depth=0,
        auxiliary="",
        post_history="",
        prefill=None,
        slots=None,
    )


class TestMeasuringExpander:
    """只为量字数再展开一次时不记账：不累加宏统计、不记长度保护的 WARNING"""

    async def test_recording_expander_counts_and_measuring_one_does_not(self) -> None:
        prompt_rounds.begin(CODE, _FakeChannel())
        tavern_runtime._rounds[CODE] = _round_with_macro()  # noqa: SLF001

        assert tavern_runtime.expander(CODE)("{{char}} 和 {{user}}") == "薄荷 和 阿明"
        assert tavern_runtime.expander(CODE, record=False)("{{char}} 和 {{user}}") == "薄荷 和 阿明"

        macros = prompt_rounds._rounds[CODE].tavern_items["macros"]  # noqa: SLF001
        assert macros["expanded"] == 2

    async def test_guard_is_logged_once_and_only_by_the_recording_expander(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(tavern_runtime.logger, "warning", lambda message, *_a, **_k: warnings.append(message))
        # 每一遍都翻好几倍：`{{char}}` 展开成一段又含十个 `{{char}}` 的文字，很快越过长度保护
        explosive = "{{char}}"
        round_ = _round_with_macro()
        round_.macro = MacroContext(
            char_name="{{char}}" * 10,
            user_name="阿明",
            now=__import__("datetime").datetime(2026, 1, 2),
        )
        tavern_runtime._rounds[CODE] = round_  # noqa: SLF001

        tavern_runtime.expander(CODE, record=False)(explosive)
        assert warnings == []

        tavern_runtime.expander(CODE)(explosive)
        assert len(warnings) == 1
        assert "长度保护" in warnings[0]
        assert "kind=worldbook" in warnings[0]
        assert "{{char}}" not in warnings[0]
