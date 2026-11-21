"""`build_round_prompt(dry_run=True)`：预览 / 试发该挡住哪些副作用

预览的承诺是「不调模型、不留痕」。这里把有副作用的那几步逐个打桩，确认一条都没有跑到；
同时确认预览该有的东西都在：换人设、换模型组、追加一句、元数据、以及世界书与酒馆缓存
在结束后都被弹掉。

`prompt_rounds` 的那一条不用单独挡：预览的令牌从不 `begin`，所有 `note_*` 对没有草稿的
令牌本来就是空操作，这里直接断言跑完之后没有留下草稿。
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
from nekro_agent.services.agent.templates.plugin import render_plugins_prompt
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.tavern.slots import AdvancedSlots, SlotText
from nekro_agent.services.worldbook import runtime as wb_runtime
from nekro_agent.services.worldbook import selection
from nekro_agent.services.worldbook.types import MatchEntry, Position

CODE = "td0001"
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
        return SimpleNamespace(id=1, name="基础人设", content="基础正文")

    async def get_effective_config(self) -> Any:
        return self._cfg


_MODEL_GROUP = SimpleNamespace(
    CHAT_MODEL="base-model",
    BASE_URL="https://example.invalid/v1",
    ENABLE_VISION=False,
    ENABLE_COT=False,
    PREFILL_MODE="off",
)


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
    MODEL_GROUPS = {"default": _MODEL_GROUP}


def _hit(entry_id: int, *,
         content: str,
         trusted: bool,
         position: Position,
         depth: int = 4) -> selection.Hit:
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


def _round_worldbook() -> wb_runtime.RoundWorldbook:
    return wb_runtime.RoundWorldbook(
        state="injected",
        groups=selection.RenderGroups(
            after_persona=[_hit(1, content="人设后正文", trusted=True, position=Position.AFTER_PERSONA)],
            before_history=[_hit(2, content="记录前正文", trusted=True, position=Position.BEFORE_HISTORY)],
            at_depth={1: [_hit(3, content="深度世界书", trusted=True, position=Position.AT_DEPTH, depth=1)]},
        ),
    )


def _slots() -> AdvancedSlots:
    layers = ["system"]
    return AdvancedSlots(
        enabled=True,
        post_history=SlotText(text="后置正文 {{char}}", layers=layers),
        auxiliary=SlotText(text="辅助正文 {{user}}", layers=layers),
        prefill=SlotText(text="预填正文 {{char}}", layers=layers),
    )


def _card() -> Any:
    return SimpleNamespace(
        nickname="薄荷",
        macros_enabled=True,
        depth_prompt="深度 {{char}}",
        depth_prompt_depth=2,
        post_history="",
        prefill="",
    )


def _install_fakes(monkeypatch: pytest.MonkeyPatch, calls: dict[str, int], *, memory_calls: dict[str, int]) -> Any:
    messages = [_FakeDbMessage("m1"), _FakeDbMessage("m2")]

    def fake_filter(*_args: Any, **_kwargs: Any) -> Any:
        class _QS:
            def order_by(self, *_a: Any, **_k: Any) -> "_QS":
                return self

            def limit(self, *_a: Any, **_k: Any) -> "_QS":
                return self

            async def __aiter__(self) -> Any:
                for message in messages:
                    yield message

            def __await__(self) -> Any:
                async def _inner() -> list[_FakeDbMessage]:
                    return messages

                return _inner().__await__()

        return _QS()

    async def fake_resolve(code: str, *_args: Any, **_kwargs: Any) -> wb_runtime.RoundWorldbook:
        result = _round_worldbook()
        wb_runtime._rounds[code] = result  # noqa: SLF001
        return result

    async def card(*_args: Any, **_kwargs: Any) -> Any:
        return _card()

    async def slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
        return _slots()

    async def affection(**_kwargs: Any) -> str:
        return "## Affection\n好感正文"

    async def plugins(*_args: Any, **kwargs: Any) -> Any:
        calls["plugins"] += 1
        calls["call_inject"] = kwargs.get("call_inject")
        return SimpleNamespace(system_prompt="插件静态", runtime_prompt="")

    async def active(**_kwargs: Any) -> list[Any]:
        return []

    async def no_scope(*_args: Any, **_kwargs: Any) -> None:
        return None

    def schedule(*_args: Any, **_kwargs: Any) -> None:
        calls["schedule"] += 1

    async def should_not_run_memory(**_kwargs: Any) -> str:
        memory_calls["called"] += 1
        return "不应出现"

    async def empty_dialog() -> list[Any]:
        return []

    async def none_jinja() -> None:
        return None

    async def self_info(_chat_key: str) -> Any:
        return SimpleNamespace(platform_name="qq", user_id="10001")

    async def runtime_prompt() -> str:
        return ""

    adapter = SimpleNamespace(
        set_dialog_example=empty_dialog,
        get_jinja_env=none_jinja,
        get_self_info=self_info,
        render_runtime_prompt=runtime_prompt,
    )
    ctx = SimpleNamespace(adapter=adapter, from_chat_key=CHAT_KEY)

    monkeypatch.setattr(history_module.DBChatMessage, "filter", staticmethod(fake_filter))
    monkeypatch.setattr(history_module, "_inject_memory_context", should_not_run_memory)
    monkeypatch.setattr(history_module, "is_domain_scope_enabled", lambda: False)
    # 只替换求值入口，保留真正的 `finish_round`：预览收尾要靠它弹缓存。
    monkeypatch.setattr(wb_runtime, "resolve_round", fake_resolve)
    monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(card))
    monkeypatch.setattr(tavern_runtime, "resolve_slots", slots)
    monkeypatch.setattr(round_prompt_module, "render_affection_prompt", affection)
    monkeypatch.setattr(round_prompt_module, "schedule_affection_evaluation", schedule)
    monkeypatch.setattr(round_prompt_module, "render_plugins_prompt", plugins)
    monkeypatch.setattr(round_prompt_module, "resolve_active_plugins", active)
    monkeypatch.setattr(round_prompt_module, "enforce_channel_model_group_scope", no_scope)
    return ctx


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    tavern_runtime._rounds.clear()  # noqa: SLF001
    wb_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001
    yield
    tavern_runtime._rounds.clear()  # noqa: SLF001
    wb_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001


class TestDryRunSkipsSideEffects:
    async def test_every_side_effect_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        calls = {"plugins": 0, "schedule": 0, "call_inject": None}
        memory_calls = {"called": 0}
        guard_calls: list[Any] = []
        ctx = _install_fakes(monkeypatch, calls, memory_calls=memory_calls)

        import nekro_agent.services.ops_agent.context_guard as guard_module

        monkeypatch.setattr(guard_module, "note_rendered_history", lambda *_a, **_k: guard_calls.append(1))

        result = await round_prompt_module.build_round_prompt(
            chat_key=CHAT_KEY,
            db_chat_channel=_FakeChannel(_FakeConfig()),  # type: ignore[arg-type]
            ctx=ctx,  # type: ignore[arg-type]
            one_time_code=CODE,
            trigger_message=SimpleNamespace(sender_nickname="阿明"),
            dry_run=True,
        )

        assert calls["schedule"] == 0
        assert calls["plugins"] == 1
        assert calls["call_inject"] is False
        assert memory_calls["called"] == 0
        assert guard_calls == []
        assert prompt_rounds._rounds == {}  # noqa: SLF001
        assert wb_runtime.cached_round(CODE) is None
        assert tavern_runtime.cached_tavern_round(CODE) is None
        assert result.meta


class TestDryRunOverridesAndMeta:
    async def test_overrides_extras_and_meta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", True)
        calls = {"plugins": 0, "schedule": 0, "call_inject": None}
        ctx = _install_fakes(monkeypatch, calls, memory_calls={"called": 0})
        override_preset = SimpleNamespace(id=9, name="预览人设", content="预览正文")
        override_group = SimpleNamespace(
            CHAT_MODEL="preview-model",
            BASE_URL="https://preview.invalid/v1",
            ENABLE_VISION=True,
            ENABLE_COT=False,
            PREFILL_MODE="assistant",
        )

        result = await round_prompt_module.build_round_prompt(
            chat_key=CHAT_KEY,
            db_chat_channel=_FakeChannel(_FakeConfig()),  # type: ignore[arg-type]
            ctx=ctx,  # type: ignore[arg-type]
            one_time_code=CODE,
            trigger_message=SimpleNamespace(sender_nickname="阿明"),
            dry_run=True,
            preset_override=override_preset,  # type: ignore[arg-type]
            model_group_override=override_group,  # type: ignore[arg-type]
            extra_history_lines=["[试发那一句]"],
        )

        assert result.preset is override_preset
        assert result.used_model_group is override_group
        system = str(result.messages[0].to_dict()["content"])
        history = str(result.messages[-1].to_dict()["content"])
        assert "预览正文" in system and "基础正文" not in system
        assert "## Additional Instructions" in system and "辅助正文 阿明" in system
        assert "人设后正文" in system
        assert "记录前正文" in history
        assert "PostHistoryInstructions" in history
        assert "深度 薄荷" in history
        assert history.index("[m2]") < history.index("[试发那一句]")
        assert "[记忆召回：预览不执行；这个频道开着记忆时，实际每轮在这里注入召回结果]" in history

        meta = result.meta
        assert meta["persona_chars"] == len("预览正文")
        assert meta["slots"]["enabled"] is True
        assert meta["slots"]["auxiliary"]["text"] == "辅助正文 阿明"
        assert meta["slots"]["prefill"]["text"] == "预填正文 薄荷"
        assert meta["depth_prompt"] == {"text": "深度 薄荷", "depth": 2}
        assert [item["entry_id"] for item in meta["worldbook"]["after_persona"]] == [1]
        assert [item["entry_id"] for item in meta["worldbook"]["before_history"]] == [2]
        assert meta["worldbook"]["at_depth"][0]["depth"] == 1
        assert meta["prefill"]["text"] == "预填正文 薄荷"
        assert meta["prefill"]["will_send"] is True
        assert meta["prefill"]["reason"] == "ready"

        # 元数据只放引用与字数，不落任何一段原文。
        dumped = json.dumps(meta, ensure_ascii=False)
        assert "人设后正文" not in dumped
        assert "记录前正文" not in dumped
        assert "深度世界书" not in dumped


class TestPluginDryRun:
    async def test_inject_method_is_not_called_and_a_placeholder_is_shown(self) -> None:
        called: list[int] = []

        class _Plugin:
            name = "动态插件"
            module_name = "dynamic_plugin"
            support_adapter: list[str] = []
            description = "测试用"
            sleep_brief = ""
            brief_resolver = None
            prompt_inject_method = object()

            async def render_sandbox_methods_prompt(self, _ctx: Any) -> str:
                return ""

            async def render_inject_prompt(self, _ctx: Any) -> str:
                called.append(1)
                return "不应出现"

        rendered = await render_plugins_prompt(
            [_Plugin()],  # type: ignore[list-item]
            SimpleNamespace(adapter_key="onebot_v11", chat_key=CHAT_KEY),  # type: ignore[arg-type]
            activation_enabled=False,
            call_inject=False,
        )

        assert called == []
        assert rendered.runtime_prompt == "[插件动态注入：预览不执行（动态插件）]"

    async def test_no_inject_plugins_means_no_placeholder(self) -> None:
        class _Plugin:
            name = "静态插件"
            module_name = "static_plugin"
            support_adapter: list[str] = []
            description = "测试用"
            sleep_brief = ""
            brief_resolver = None
            prompt_inject_method = None

            async def render_sandbox_methods_prompt(self, _ctx: Any) -> str:
                return ""

            async def render_inject_prompt(self, _ctx: Any) -> str:
                raise AssertionError("没有注入方法的插件不该被调用")

        rendered = await render_plugins_prompt(
            [_Plugin()],  # type: ignore[list-item]
            SimpleNamespace(adapter_key="onebot_v11", chat_key=CHAT_KEY),  # type: ignore[arg-type]
            activation_enabled=False,
            call_inject=False,
        )

        assert rendered.runtime_prompt == ""
