"""人设节的外层框：角色扮演频道只留标题与人设正文

人设正文前的「mainly controls your tone…」与三条 Important boundaries 在角色扮演频道会
削弱「你就是这个角色」。开了高级提示词槽位的酒馆频道去掉这几行，其余频道与以前逐字相同；
「不假装执行过、不编造结果」仍由核心规则 `policy_kernel.j2` 保证，不在这次改动里。

这里用真模板、真 `PromptCompiler` 拼一遍系统提示词，只把查库与外部服务换成替身。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.agent import round_prompt as round_prompt_module
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.agent.templates.base import env as default_env
from nekro_agent.services.agent.templates.compiler import PromptCompiler
from nekro_agent.services.agent.templates.system import PersonaPrompt
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.tavern.slots import AdvancedSlots, SlotText
from nekro_agent.services.worldbook import runtime as wb_runtime
from nekro_agent.services.worldbook import selection

CHAT_KEY = "onebot_v11-group_1"
CODE = "pr0001"

# 改动前人设节的逐字输出（含开头与结尾的换行）。
PERSONA_WITH_FRAME = (
    "\n## Character Profile\n\n"
    "The following character preset mainly controls your tone, emotional expression, interaction style, "
    "and sense of personality.\n\n"
    "Important boundaries:\n"
    "- Character style affects HOW you speak, not WHETHER something is true.\n"
    "- Character style must NEVER override honesty, verification standards, or real-world factual boundaries.\n"
    "- You may respond naturally and vividly in character, but when making real claims about facts, status, "
    "results, files, links, or availability, remain evidence-aligned and careful.\n\n"
    "X\n"
)


class _FakeDbMessage:
    is_system = False

    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        self.ext_data_obj = SimpleNamespace(ref_msg_id="")

    def parse_content_data(self) -> list[Any]:
        return []

    def parse_chat_history_prompt(self, *_args: Any, **_kwargs: Any) -> str:
        return f"[{self.message_id}]"


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
            PREFILL_MODE="off",
        ),
    }


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
        return SimpleNamespace(id=1, name="默认人设", content="你是薄荷。")

    async def get_effective_config(self) -> Any:
        return self._cfg


def _compiler(*, roleplay: bool) -> PromptCompiler:
    return PromptCompiler(
        platform_name="qq",
        bot_platform_id="10001",
        chat_preset="你是薄荷。",
        plugins_prompt="",
        plugins_runtime_prompt="",
        plugin_activation_rules="",
        plugin_call_priority_rules="",
        enable_cot=False,
        chat_key_rules="",
        enable_at=False,
        persona_roleplay=roleplay,
    )


class TestPersonaTemplate:
    def test_default_output_is_byte_identical(self) -> None:
        assert PersonaPrompt(chat_preset="X").render(default_env) == PERSONA_WITH_FRAME

    def test_roleplay_keeps_only_the_profile(self) -> None:
        rendered = PersonaPrompt(chat_preset="X", roleplay=True).render(default_env)

        assert rendered == "## Character Profile\n\nX"
        assert "mainly controls your tone" not in rendered
        assert "Important boundaries" not in rendered
        assert "## Character Profile" in rendered


class TestCompiler:
    def test_default_keeps_the_frame(self) -> None:
        content = str(_compiler(roleplay=False).render_system_message().to_dict()["content"])

        assert "mainly controls your tone" in content
        assert "Important boundaries" in content

    def test_roleplay_drops_the_frame(self) -> None:
        content = str(_compiler(roleplay=True).render_system_message().to_dict()["content"])

        assert "mainly controls your tone" not in content
        assert "Important boundaries" not in content
        assert "## Character Profile" in content
        assert "你是薄荷。" in content


def _fake_message_query(monkeypatch: pytest.MonkeyPatch) -> None:
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

    async def empty_memory(**_kwargs: Any) -> str:
        return ""

    monkeypatch.setattr(history_module.DBChatMessage, "filter", staticmethod(fake_filter))
    monkeypatch.setattr(history_module, "_inject_memory_context", empty_memory)
    monkeypatch.setattr(history_module, "is_domain_scope_enabled", lambda: False)
    monkeypatch.setattr(history_module, "supports_vision", lambda *_a, **_k: False)

    import nekro_agent.services.ops_agent.context_guard as guard_module

    monkeypatch.setattr(guard_module, "note_rendered_history", lambda *_a, **_k: None)


def _install_fakes(monkeypatch: pytest.MonkeyPatch, cfg: _FakeConfig) -> Any:
    async def slots(_channel: Any, effective: Any, _card: Any) -> AdvancedSlots:
        enabled = bool(getattr(effective, "PROMPT_ADVANCED_SLOTS_ENABLED", False))
        empty = SlotText(text="")
        return AdvancedSlots(enabled=enabled, post_history=empty, auxiliary=empty, prefill=empty)

    async def no_card(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def empty_worldbook(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            state="skipped",
            dropped=[],
            groups=selection.RenderGroups(),
            budget=None,
            tokens={},
        )

    async def empty_affection(**_kwargs: Any) -> str:
        return ""

    async def empty_plugins(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(system_prompt="", runtime_prompt="", depth_blocks=[])

    async def no_plugins(**_kwargs: Any) -> list[Any]:
        return []

    async def no_scope(*_args: Any, **_kwargs: Any) -> None:
        return None

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
    channel = _FakeChannel(cfg)
    ctx = SimpleNamespace(adapter=adapter, from_chat_key=CHAT_KEY, db_chat_channel=channel)

    _fake_message_query(monkeypatch)
    monkeypatch.setattr(wb_runtime, "resolve_round", empty_worldbook)
    monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(no_card))
    monkeypatch.setattr(tavern_runtime, "resolve_slots", slots)
    monkeypatch.setattr(round_prompt_module, "render_affection_prompt", empty_affection)
    monkeypatch.setattr(round_prompt_module, "schedule_affection_evaluation", lambda *_a, **_k: None)
    monkeypatch.setattr(round_prompt_module, "render_plugins_prompt", empty_plugins)
    monkeypatch.setattr(round_prompt_module, "resolve_active_plugins", no_plugins)
    monkeypatch.setattr(round_prompt_module, "enforce_channel_model_group_scope", no_scope)
    monkeypatch.setattr(round_prompt_module, "render_ops_prompt_section", lambda _ctx: "")
    return channel, ctx


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    tavern_runtime._rounds.clear()  # noqa: SLF001
    wb_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001
    yield
    tavern_runtime._rounds.clear()  # noqa: SLF001
    wb_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001


async def _system_prompt(monkeypatch: pytest.MonkeyPatch, *, gate: bool, slots: bool) -> str:
    monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", gate)
    cfg = _FakeConfig()
    cfg.PROMPT_ADVANCED_SLOTS_ENABLED = slots
    channel, ctx = _install_fakes(monkeypatch, cfg)

    built = await round_prompt_module.build_round_prompt(
        chat_key=CHAT_KEY,
        db_chat_channel=channel,  # type: ignore[arg-type]
        ctx=ctx,  # type: ignore[arg-type]
        one_time_code=CODE,
        trigger_message=SimpleNamespace(sender_nickname="阿明"),
    )
    return str(built.messages[0].to_dict()["content"])


class TestRoundPromptWiring:
    async def test_slots_on_drops_the_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        content = await _system_prompt(monkeypatch, gate=True, slots=True)

        assert "## Character Profile" in content
        assert "你是薄荷。" in content
        assert "mainly controls your tone" not in content
        assert "Important boundaries" not in content

    async def test_slots_off_keeps_the_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        content = await _system_prompt(monkeypatch, gate=True, slots=False)

        assert "mainly controls your tone" in content
        assert "Important boundaries" in content

    async def test_tavern_gate_off_keeps_the_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        content = await _system_prompt(monkeypatch, gate=False, slots=True)

        assert "mainly controls your tone" in content
        assert "Important boundaries" in content
