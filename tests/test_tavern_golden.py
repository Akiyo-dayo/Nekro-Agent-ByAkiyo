"""闸前 / 闸后逐字相同的黄金对照

对照组不是另一份实现，而是改造前 `run_agent` 里那串调用**原样搬过来**：`PromptCompiler`
用同样的参数、世界书用同一个轮次、历史用同一个入口。只要酒馆闸关着，或者闸开着但人设
没有扩展行、世界书条目不含宏，新路径拼出来的三条消息就应当与它逐字一致。

最后一个用例跑完整的 `run_agent`，确认搬到 `round_prompt` 之后实际送进
`send_agent_request` 的仍是同一份消息。
"""

from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace
from typing import Any

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.agent import round_prompt as round_prompt_module
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.agent.templates.compiler import PromptCompiler
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.plugin.call_priority import build_plugin_call_priority_rules
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.tavern.slots import AdvancedSlots, SlotText
from nekro_agent.services.worldbook import render as wb_render
from nekro_agent.services.worldbook import runtime as wb_runtime
from nekro_agent.services.worldbook import selection
from nekro_agent.services.worldbook.types import MatchEntry, Position

CHAT_KEY = "onebot_v11-group_1"
# `os.urandom(8).hex()` 的固定结果：16 位十六进制，真实一轮与对照组共用同一个令牌。
CODE = "abababababababab"
# 历史消息里渲染的「当前时间」精确到秒、农历按日期，对照组与新路径各渲染一次。不钉住的话，
# 两次渲染之间跨过整秒就逐字不同（机器忙、或闸开那一条多走一段解析时更容易碰上）。
FROZEN_NOW = dt.datetime(2026, 1, 2, 3, 4, 5)


class _FrozenTime:
    """history 模块用的 `time`：`localtime()` 固定在 `FROZEN_NOW`，其余照真的"""

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    @staticmethod
    def localtime(*_args: Any) -> time.struct_time:
        return FROZEN_NOW.timetuple()


class _FrozenDatetime(dt.datetime):
    @classmethod
    def now(cls, tz: Any = None) -> dt.datetime:  # type: ignore[override]
        return FROZEN_NOW


class _FrozenDatetimeModule:
    """history 模块用的 `datetime`：`datetime.now()` 固定在 `FROZEN_NOW`，其余照真的"""

    datetime = _FrozenDatetime

    def __getattr__(self, name: str) -> Any:
        return getattr(dt, name)


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
        return SimpleNamespace(id=1, name="默认人设", content="你是薄荷。")

    async def get_effective_config(self) -> Any:
        return self._cfg


_MODEL_GROUP = SimpleNamespace(
    CHAT_MODEL="fake-model",
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
    AI_SCRIPT_MAX_RETRY_TIMES = 2
    AI_CHAT_LLM_API_MAX_RETRIES = 3
    PROMPT_ADVANCED_SLOTS_ENABLED = False
    USE_MODEL_GROUP = "default"
    MODEL_GROUPS = {"default": _MODEL_GROUP}


def _hit(entry_id: int, *, content: str, position: Position, depth: int = 4) -> selection.Hit:
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
            trusted=True,
            invalid_keys=(),
        ),
        reason=matcher.HitReason(kind="constant"),
    )


def _worldbook_round() -> wb_runtime.RoundWorldbook:
    # 世界书条目里**没有**宏：这时闸开着也不该有任何字符变化。
    return wb_runtime.RoundWorldbook(
        state="injected",
        groups=selection.RenderGroups(
            after_persona=[_hit(1, content="人设后世界书", position=Position.AFTER_PERSONA)],
            before_history=[_hit(2, content="记录前世界书", position=Position.BEFORE_HISTORY)],
            at_depth={1: [_hit(3, content="深度世界书", position=Position.AT_DEPTH, depth=1)]},
        ),
    )


def _fake_message_query(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [_FakeDbMessage("m1"), _FakeDbMessage("m2"), _FakeDbMessage("m3")]

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
    monkeypatch.setattr(history_module, "time", _FrozenTime())
    monkeypatch.setattr(history_module, "datetime", _FrozenDatetimeModule())
    monkeypatch.setattr(history_module, "_inject_memory_context", empty_memory)
    monkeypatch.setattr(history_module, "is_domain_scope_enabled", lambda: False)
    monkeypatch.setattr(history_module, "supports_vision", lambda *_a, **_k: False)

    import nekro_agent.services.ops_agent.context_guard as guard_module

    monkeypatch.setattr(guard_module, "note_rendered_history", lambda *_a, **_k: None)


def _install_fakes(monkeypatch: pytest.MonkeyPatch) -> Any:
    async def fake_resolve(code: str, *_args: Any, **_kwargs: Any) -> wb_runtime.RoundWorldbook:
        result = _worldbook_round()
        wb_runtime._rounds[code] = result  # noqa: SLF001
        return result

    async def no_card(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def disabled_slots(*_args: Any, **_kwargs: Any) -> AdvancedSlots:
        # 四个字段都要给：只给 `enabled` 会抛 TypeError，被酒馆运行时的兜底吞掉、整轮退成闸关，
        # 闸开那一条就悄悄变成了在测闸关（下面的用例另外断言了这一轮确实是闸开形状）
        empty = SlotText(text="")
        return AdvancedSlots(enabled=False, post_history=empty, auxiliary=empty, prefill=empty)

    async def empty_affection(**_kwargs: Any) -> str:
        return ""

    async def empty_plugins(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(system_prompt="", runtime_prompt="")

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
    channel = _FakeChannel(_FakeConfig())
    ctx = SimpleNamespace(adapter=adapter, from_chat_key=CHAT_KEY, db_chat_channel=channel)

    monkeypatch.setattr(wb_runtime, "resolve_round", fake_resolve)
    monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(no_card))
    monkeypatch.setattr(tavern_runtime, "resolve_slots", disabled_slots)
    monkeypatch.setattr(round_prompt_module, "render_affection_prompt", empty_affection)
    monkeypatch.setattr(round_prompt_module, "schedule_affection_evaluation", lambda *_a, **_k: None)
    monkeypatch.setattr(round_prompt_module, "render_plugins_prompt", empty_plugins)
    monkeypatch.setattr(round_prompt_module, "resolve_active_plugins", no_plugins)
    monkeypatch.setattr(round_prompt_module, "enforce_channel_model_group_scope", no_scope)
    _fake_message_query(monkeypatch)
    return channel, ctx


async def _baseline() -> list[dict[str, Any]]:
    """改造前的调用顺序：没有酒馆、没有辅助提示词、没有 dry-run 与追加行"""
    cfg = _FakeConfig()
    channel = _FakeChannel(cfg)
    preset = await channel.get_preset()
    worldbook_round = await wb_runtime.resolve_round(CODE, channel, cfg)  # type: ignore[arg-type]
    worldbook_prompt = wb_render.render_block(
        worldbook_round.groups.after_persona,
        CODE,
        in_history=False,
    )
    compiler = PromptCompiler(
        affection_prompt="",
        worldbook_prompt=worldbook_prompt,
        ops_prompt="",
        platform_name="qq",
        bot_platform_id="10001",
        chat_preset=preset.content,
        plugins_prompt="",
        plugins_runtime_prompt="",
        plugin_activation_rules="",
        plugin_call_priority_rules=build_plugin_call_priority_rules(),
        enable_cot=False,
        chat_key_rules="\n".join(f"- {rule}" for rule in channel.adapter.chat_key_rules),
        enable_at=False,
    )
    messages = [compiler.render_system_message()]
    messages.extend(compiler.render_practice_messages([], None))
    messages.append(
        await compiler.render_history_message(
            chat_key=CHAT_KEY,
            db_chat_channel=channel,  # type: ignore[arg-type]
            one_time_code=CODE,
            config=cfg,  # type: ignore[arg-type]
            model_group=_MODEL_GROUP,  # type: ignore[arg-type]
        ),
    )
    return [message.to_dict() for message in messages]


def _cleanup() -> None:
    wb_runtime.finish_round(CODE, "cancelled")
    tavern_runtime.discard_tavern_round(CODE)


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    tavern_runtime._rounds.clear()  # noqa: SLF001
    wb_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001
    yield
    tavern_runtime._rounds.clear()  # noqa: SLF001
    wb_runtime._rounds.clear()  # noqa: SLF001
    prompt_rounds._rounds.clear()  # noqa: SLF001


@pytest.mark.parametrize("gate", [False, True], ids=["gate-off", "gate-on-no-expansion"])
async def test_new_path_is_byte_identical_to_the_old_sequence(
    monkeypatch: pytest.MonkeyPatch,
    gate: bool,
) -> None:
    monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", gate)
    channel, ctx = _install_fakes(monkeypatch)

    baseline = await _baseline()
    _cleanup()

    built = await round_prompt_module.build_round_prompt(
        chat_key=CHAT_KEY,
        db_chat_channel=channel,  # type: ignore[arg-type]
        ctx=ctx,  # type: ignore[arg-type]
        one_time_code=CODE,
        trigger_message=SimpleNamespace(sender_nickname="阿明"),
    )
    built_messages = [message.to_dict() for message in built.messages]

    # 闸开那一条必须真的走了闸开的解析，而不是解析出错后退回闸关的形状
    tavern_round = tavern_runtime.cached_tavern_round(CODE)
    assert tavern_round is not None
    assert tavern_round.enabled is gate

    assert built_messages == baseline
    # 基准也是用现在的模板渲染的，模板本身多出来的东西它看不出来；所以单独钉死：辅助
    # 提示词为空时，系统提示词里不能出现它的标题，人设之后紧接着就是世界书那一段。
    assert "## Additional Instructions" not in str(built_messages[0]["content"])
    # 世界书三段都真的在，不然这个对照是在比两段空串。
    assert "人设后世界书" in str(built_messages[0]["content"])
    assert "记录前世界书" in str(built_messages[-1]["content"])
    assert "深度世界书" in str(built_messages[-1]["content"])


async def test_run_agent_sends_the_same_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", False)
    channel, ctx = _install_fakes(monkeypatch)
    baseline = await _baseline()
    _cleanup()

    captured: list[list[dict[str, Any]]] = []

    async def fake_send(**kwargs: Any) -> Any:
        captured.append([message.to_dict() for message in kwargs["messages"]])
        raise run_agent_module.AllLLMRequestsFailedError("停止在这里")

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(run_agent_module, "send_agent_request", fake_send)
    monkeypatch.setattr(run_agent_module, "_channel_message_marker", noop)
    monkeypatch.setattr(run_agent_module, "_record_round_reply", noop)
    monkeypatch.setattr(prompt_rounds, "begin", lambda *_a, **_k: None)
    monkeypatch.setattr(prompt_rounds, "finish", lambda *_a, **_k: None)
    # `run_agent` 用 `os.urandom(8).hex()` 生成令牌；固定成和对照组一样的 16 位。
    monkeypatch.setattr(run_agent_module.os, "urandom", lambda _n: b"\xab" * 8)

    import nekro_agent.services.system_broadcast as broadcast_module

    async def no_event(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(broadcast_module, "publish_system_event", no_event)

    with pytest.raises(run_agent_module.AllLLMRequestsFailedError):
        await run_agent_module.run_agent(
            chat_key=CHAT_KEY,
            chat_message=SimpleNamespace(sender_nickname="阿明"),  # type: ignore[arg-type]
            ctx=ctx,  # type: ignore[arg-type]
        )

    assert len(captured) == 1
    assert captured[0] == baseline
