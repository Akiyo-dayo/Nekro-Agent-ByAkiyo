"""系统提示词的稳定前缀顺序：开关关时逐字节不变，开关开时精确换位

## 这份测试要防的是什么

| 失效形态 | 表现 | 钉在哪 |
|---|---|---|
| 开关关着却改了模板空白 | 所有频道的系统提示词整体漂移一个字节 | `TestSwitchOffIsByteIdentical` |
| 开关打开但块没挪干净 | 世界书/好感块仍在前面，缓存前缀照样断 | `TestSwitchOnMovesBlocks` |
| 块搬走时丢了包裹或末尾换行 | 租户内容失去一次性令牌边界 / 多出空行 | `TestSwitchOnMovesBlocks` / `TestRoundPromptWiring` |
| 两段内容变了却带动前缀 | 开关没有实际效果 | `TestStablePrefix` |
| 运维段排到两块后面 | 宿主的运维规矩成了不可信块标题下的子节 | `TestSwitchOnMovesBlocks` / `TestRoundPromptWiring` |
| 开着但两块都空时与关着差字节 | 打开开关本身就让一批频道的前缀变一次 | `TestSwitchOnMovesBlocks` |

开关关着时的基准是**改动前** `system.j2` 的原文，逐字抄在 :data:`SYSTEM_J2_BEFORE` 里，
用项目同一个 Jinja 环境渲染；不是拿改过的模板自己和自己比。
"""

from __future__ import annotations

import datetime as dt
import itertools
from types import SimpleNamespace
from typing import Any

import pytest

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.services.agent import round_prompt as round_prompt_module
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.agent.templates.base import env as default_env
from nekro_agent.services.agent.templates.compiler import PromptCompiler
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.worldbook import runtime as wb_runtime
from nekro_agent.services.worldbook import selection
from nekro_agent.services.worldbook.matcher import HitReason
from nekro_agent.services.worldbook.types import MatchEntry, Position, SelectiveLogic

CHAT_KEY = "onebot_v11-group_1"
CODE = "sp0001"

# 改动前 `nekro_agent/services/agent/templates/j2/system.j2` 的原文（逐字）。
SYSTEM_J2_BEFORE = (
    '{% macro system_prompt(stable_static, channel_static, runtime_dynamic, affection_prompt, plugins_prompt, '
    'worldbook_prompt="", auxiliary_prompt="", ops_prompt="") %}\n'
    "{{ stable_static }}\n"
    "\n"
    "{{ channel_static }}\n"
    "{% if auxiliary_prompt %}\n"
    "## Additional Instructions\n"
    "{{ auxiliary_prompt }}\n"
    "{% endif %}{% if worldbook_prompt %}\n"
    "{{ worldbook_prompt }}\n"
    "{% endif %}{% if affection_prompt %}\n"
    "{{ affection_prompt }}\n"
    "{% endif %}\n"
    "{{ runtime_dynamic }}\n"
    "\n"
    "### Your Plugins:\n"
    "<plugins>\n"
    "{{ plugins_prompt }}\n"
    "</plugins>\n"
    "{%- if ops_prompt %}\n"
    "{{ ops_prompt }}\n"
    "{%- endif %}\n"
    "{% endmacro %}\n"
)


def _compiler(**overrides: Any) -> PromptCompiler:
    kwargs: dict[str, Any] = {
        "platform_name": "qq",
        "bot_platform_id": "10001",
        "chat_preset": "你是薄荷，说话简洁。",
        "plugins_prompt": "插件段",
        "plugins_runtime_prompt": "",
        "plugin_activation_rules": "",
        "plugin_call_priority_rules": "",
        "enable_cot": False,
        "chat_key_rules": "",
        "enable_at": False,
    }
    kwargs.update(overrides)
    return PromptCompiler(**kwargs)


def _content(compiler: PromptCompiler, **kwargs: Any) -> str:
    return str(compiler.render_system_message(**kwargs).to_dict()["content"])


def _old_content(compiler: PromptCompiler, **fields: Any) -> str:
    """用改动前的模板原文渲染同一组输入"""
    segments = compiler.compile_segments()
    values = {
        "stable_static": segments.stable_static,
        "channel_static": segments.channel_static,
        "runtime_dynamic": segments.runtime_dynamic,
        "affection_prompt": fields.get("affection_prompt", compiler.affection_prompt),
        "plugins_prompt": compiler.plugins_prompt,
        "worldbook_prompt": fields.get("worldbook_prompt", compiler.worldbook_prompt),
        "auxiliary_prompt": fields.get("auxiliary_prompt", compiler.auxiliary_prompt),
        "ops_prompt": fields.get("ops_prompt", compiler.ops_prompt),
    }
    return default_env.from_string(SYSTEM_J2_BEFORE).module.system_prompt(**values)


def _inputs(*, auxiliary: bool, worldbook: bool, affection: bool, ops: bool) -> dict[str, str]:
    return {
        "auxiliary_prompt": "辅助正文" if auxiliary else "",
        # 世界书与好感块的样例文字末尾都带换行，开关打开时应被去掉。
        "worldbook_prompt": "## World Info\n宿主正文\n" if worldbook else "",
        "affection_prompt": "## Affection\n好感正文\n" if affection else "",
        "ops_prompt": "运维正文" if ops else "",
    }


COMBINATIONS = list(itertools.product([False, True], repeat=4))


class TestSwitchOffIsByteIdentical:
    @pytest.mark.parametrize(("auxiliary", "worldbook", "affection", "ops"), COMBINATIONS)
    def test_switch_off_matches_the_old_template(
        self,
        auxiliary: bool,
        worldbook: bool,
        affection: bool,
        ops: bool,
    ) -> None:
        inputs = _inputs(auxiliary=auxiliary, worldbook=worldbook, affection=affection, ops=ops)
        compiler = _compiler(**inputs)
        expected = _old_content(compiler, **inputs)

        assert _content(compiler) == expected
        assert _content(compiler, stable_prefix_order=False) == expected


class TestSwitchOnMovesBlocks:
    @pytest.mark.parametrize(("auxiliary", "worldbook", "affection", "ops"), COMBINATIONS)
    def test_tail_matches_the_exact_expression(
        self,
        auxiliary: bool,
        worldbook: bool,
        affection: bool,
        ops: bool,
    ) -> None:
        inputs = _inputs(auxiliary=auxiliary, worldbook=worldbook, affection=affection, ops=ops)
        content = _content(_compiler(**inputs), stable_prefix_order=True)

        blocks = (inputs["worldbook_prompt"], inputs["affection_prompt"])
        expected_tail = (
            "</plugins>"
            + ("\n" + inputs["ops_prompt"] if inputs["ops_prompt"] else "")
            + "".join("\n\n" + block.rstrip("\n") for block in blocks if block)
            + "\n"
        )
        assert content.endswith(expected_tail)

    @pytest.mark.parametrize(("auxiliary", "ops"), list(itertools.product([False, True], repeat=2)))
    def test_on_with_both_blocks_empty_equals_off(self, auxiliary: bool, ops: bool) -> None:
        inputs = _inputs(auxiliary=auxiliary, worldbook=False, affection=False, ops=ops)
        compiler = _compiler(**inputs)

        assert _content(compiler, stable_prefix_order=True) == _content(compiler, stable_prefix_order=False)

    def test_host_ops_section_stays_above_the_untrusted_blocks(self) -> None:
        """运维说明是宿主写的规矩：不能落到世界书 / 好感块的标题底下，成了不可信数据那一节的子节"""
        content = _content(
            _compiler(
                ops_prompt="### 运维工具\n运维正文",
                worldbook_prompt="## World Info\n宿主正文\n",
                affection_prompt="## Relationship State\n好感正文\n",
            ),
            stable_prefix_order=True,
        )

        assert content.index("</plugins>") < content.index("运维正文") < content.index("## World Info")
        assert content.index("## World Info") < content.index("## Relationship State")
        assert content.endswith("好感正文\n")

    @pytest.mark.parametrize(("auxiliary", "worldbook", "affection", "ops"), COMBINATIONS)
    def test_prefix_matches_the_empty_blocks_render(
        self,
        auxiliary: bool,
        worldbook: bool,
        affection: bool,
        ops: bool,
    ) -> None:
        inputs = _inputs(auxiliary=auxiliary, worldbook=worldbook, affection=affection, ops=ops)
        content = _content(_compiler(**inputs), stable_prefix_order=True)

        empty_blocks = _compiler(
            auxiliary_prompt=inputs["auxiliary_prompt"],
            plugins_prompt="插件段",
            worldbook_prompt="",
            affection_prompt="",
            ops_prompt=inputs["ops_prompt"],
        )
        off_without_blocks = _content(empty_blocks)

        assert content.split("</plugins>", 1)[0] == off_without_blocks.split("</plugins>", 1)[0]


class TestStablePrefix:
    def test_switch_on_keeps_everything_through_plugins_identical(self) -> None:
        first = _content(
            _compiler(worldbook_prompt="## World Info\n第一版世界书\n", affection_prompt="## Affection\n第一版好感\n"),
            stable_prefix_order=True,
        )
        second = _content(
            _compiler(worldbook_prompt="## World Info\n第二版世界书\n", affection_prompt="## Affection\n第二版好感\n"),
            stable_prefix_order=True,
        )

        assert first != second
        assert first[: first.index("</plugins>")] == second[: second.index("</plugins>")]

    def test_switch_off_still_differs_right_after_the_persona(self) -> None:
        first = _content(
            _compiler(worldbook_prompt="## World Info\n第一版世界书\n", affection_prompt=""),
        )
        second = _content(
            _compiler(worldbook_prompt="## World Info\n第二版世界书\n", affection_prompt=""),
        )

        first_diff = next(index for index, (left, right) in enumerate(zip(first, second)) if left != right)
        assert first_diff > first.index("你是薄荷")
        assert first.index("第一版世界书") < first.index("### Your Plugins:")


# ---------------------------------------------------------------------------
# 走 `round_prompt` 一层：包裹、令牌与位置
# ---------------------------------------------------------------------------


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
    PROMPT_ADVANCED_SLOTS_ENABLED = False
    PROMPT_STABLE_PREFIX_ORDER = False
    WORLDBOOK_ENABLED = False
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


def _worldbook_round() -> Any:
    entry = MatchEntry(
        id=1,
        book_id=1,
        keys=(),
        secondary_keys=(),
        selective_logic=SelectiveLogic.AND_ANY,
        use_regex=False,
        constant=True,
        content="世界书正文",
        order=100,
        position=Position.AFTER_PERSONA,
        depth=4,
        scan_depth=4,
        case_sensitive=False,
        match_whole_words=False,
        sticky=0,
        cooldown=0,
        delay=0,
        trusted=False,
        invalid_keys=frozenset(),
    )
    hit = selection.Hit(entry=entry, reason=HitReason(kind="constant"))
    return SimpleNamespace(
        state="selected",
        dropped=[],
        groups=selection.RenderGroups(after_persona=[hit]),
        budget=None,
        tokens={},
    )


async def _wrapped_affection(**_kwargs: Any) -> str:
    return f"## Affection\n<{CODE} | Affection trust:untrusted>\n好感正文\n<{CODE} | /Affection>\n"


def _install_fakes(monkeypatch: pytest.MonkeyPatch, cfg: _FakeConfig, ops_section: str = "") -> Any:
    async def fake_worldbook_round(*_args: Any, **_kwargs: Any) -> Any:
        return _worldbook_round()

    async def no_card(*_args: Any, **_kwargs: Any) -> None:
        return None

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
    monkeypatch.setattr(wb_runtime, "resolve_round", fake_worldbook_round)
    monkeypatch.setattr(tavern_runtime.DBPresetCard, "get_or_none", staticmethod(no_card))
    monkeypatch.setattr(round_prompt_module, "render_affection_prompt", _wrapped_affection)
    monkeypatch.setattr(round_prompt_module, "schedule_affection_evaluation", lambda *_a, **_k: None)
    monkeypatch.setattr(round_prompt_module, "render_plugins_prompt", empty_plugins)
    monkeypatch.setattr(round_prompt_module, "resolve_active_plugins", no_plugins)
    monkeypatch.setattr(round_prompt_module, "enforce_channel_model_group_scope", no_scope)
    monkeypatch.setattr(round_prompt_module, "render_ops_prompt_section", lambda _ctx: ops_section)
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


async def _system_prompt(monkeypatch: pytest.MonkeyPatch, *, stable: bool, ops_section: str = "") -> str:
    monkeypatch.setattr(config, "TAVERN_COMPAT_ENABLED", False)
    cfg = _FakeConfig()
    cfg.PROMPT_STABLE_PREFIX_ORDER = stable
    channel, ctx = _install_fakes(monkeypatch, cfg, ops_section)

    built = await round_prompt_module.build_round_prompt(
        chat_key=CHAT_KEY,
        db_chat_channel=channel,  # type: ignore[arg-type]
        ctx=ctx,  # type: ignore[arg-type]
        one_time_code=CODE,
        trigger_message=SimpleNamespace(sender_nickname="阿明"),
    )
    return str(built.messages[0].to_dict()["content"])


class TestRoundPromptWiring:
    async def test_on_keeps_the_wrapping_and_moves_blocks_after_plugins(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        content = await _system_prompt(monkeypatch, stable=True)

        plugins_end = content.index("</plugins>")
        assert f"<{CODE} | WorldInfo" in content
        assert f"<{CODE} | /WorldInfo>" in content
        assert "世界书正文" in content
        assert content.index("Your qq Id:") < plugins_end < content.index("世界书正文")
        assert f"<{CODE} | Affection trust:untrusted>" in content
        assert content.index("好感正文") > plugins_end

    async def test_on_puts_the_ops_section_before_the_moved_blocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        content = await _system_prompt(monkeypatch, stable=True, ops_section="### 运维工具\n运维正文")

        ops = content.index("运维正文")
        assert content.index("</plugins>") < ops < content.index(f"<{CODE} | WorldInfo")
        assert ops < content.index(f"<{CODE} | Affection trust:untrusted>")
        assert content.rstrip("\n").endswith(f"<{CODE} | /Affection>")

    async def test_off_keeps_the_blocks_right_after_the_persona(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        content = await _system_prompt(monkeypatch, stable=False)

        persona = content.index("你是薄荷")
        runtime_contract = content.index("Your qq Id:")
        plugins = content.index("### Your Plugins:")
        assert persona < content.index("世界书正文") < runtime_contract
        assert persona < content.index("好感正文") < runtime_contract
        assert runtime_contract < plugins


class TestConfigItem:
    def test_default_is_off(self) -> None:
        assert CoreConfig.model_fields["PROMPT_STABLE_PREFIX_ORDER"].default is False

    def test_it_is_overridable_and_audited(self) -> None:
        extra = CoreConfig.model_fields["PROMPT_STABLE_PREFIX_ORDER"].json_schema_extra
        assert isinstance(extra, dict)
        assert extra["overridable"] is True
        assert extra["audit_changes"] is True

    def test_it_is_not_tenant_may_only_lower(self) -> None:
        extra = CoreConfig.model_fields["PROMPT_STABLE_PREFIX_ORDER"].json_schema_extra
        assert isinstance(extra, dict)
        assert not extra.get("tenant_may_only_lower")
