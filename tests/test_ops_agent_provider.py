"""核心方法提供者：方法清单、提示词片段、沙盒许可白名单

## 为什么这一层要单独钉

运维方法不是插件方法，却必须走插件方法之后的**每一条**路径：代理生成、
`get_sandbox_method_type`、RPC 分派、以及那一轮沙盒许可的 `method_names`。任何一处漏接，
模型要么看不到工具（功能没了），要么看得到却调不动（更糟：它会把"运维工具不可用"当成事实
转述给运维人员）。因此这里断言的是"同一个输入下三处给出同一个答案"。

## 开关关着时必须逐字一致

`render_prompt_section` 返回空串、`methods_for` 返回空表、模板渲染结果与没有这段时
**逐字节**相同。存量部署（开关默认关）的每一条提示词都不能因此多一个字或多一个换行。
"""

from typing import Any, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.agent.templates.compiler import PromptCompiler
from nekro_agent.services.ops_agent import round_grant as grant_module
from nekro_agent.services.ops_agent.provider import (
    METHOD_PREFIX,
    OPS_METHODS,
    get_method,
    is_ops_method,
    is_write_method,
    methods_for,
    render_prompt_section,
)
from nekro_agent.services.ops_agent.round_grant import open_round, reset_round_grants
from nekro_agent.services.ops_agent.tools import OPS_TOOL_FUNCS, UNAVAILABLE
from nekro_agent.services.ops_agent.triggers import TRIGGER_KIND_HUMAN, RoundTrigger, round_trigger_book
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.utils import get_sandbox_method_type
from nekro_agent.services.sandbox.runner import build_container_key

ADAPTER = "onebot_v11"
INSTANCE = "opsinst"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_10001"
OPERATOR = "10001"
CONTAINER = build_container_key(CHAT_KEY)


def human(sender_id: str = OPERATOR) -> RoundTrigger:
    return RoundTrigger(kind=TRIGGER_KIND_HUMAN, adapter_key=ADAPTER, instance_key=INSTANCE, sender_id=sender_id, operator_trusted=True)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def clean_state():
    reset_round_grants()
    yield
    reset_round_grants()


@pytest.fixture
def ops_on(monkeypatch: pytest.MonkeyPatch):
    def apply(**overrides: Any) -> None:
        settings: dict[str, Any] = {
            "OPS_AGENT_TOOLS_ENABLED": True,
            "OPS_AGENT_OPERATORS": [OPERATOR],
            "OPS_AGENT_GROUPS": [CHAT_KEY],
            "OPS_AGENT_PRIVATE_ENABLED": True,
        }
        settings.update(overrides)
        for name, value in settings.items():
            monkeypatch.setattr(config, name, value)

    apply()
    return apply


async def granted_channel() -> DBChatChannel:
    """平台方实例 + 一个通过了判定的回合：授权已签发在 `CONTAINER` 上"""
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        display_name=INSTANCE,
        status="online",
        enabled=True,
        provider="remote_onebot",
    )
    channel = await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_10001",
        chat_key=CHAT_KEY,
        channel_type="group",
        data="{}",
    )
    round_trigger_book.record(CHAT_KEY, human())
    assert await open_round(channel) is not None
    return channel


def ctx_of(chat_key: str = CHAT_KEY) -> AgentCtx:
    return AgentCtx(from_chat_key=chat_key, container_key=CONTAINER)


class TestMethodTable:
    def test_every_tool_is_in_the_table(self) -> None:
        assert set(OPS_METHODS) == set(OPS_TOOL_FUNCS)

    @pytest.mark.parametrize("name", sorted(OPS_TOOL_FUNCS))
    def test_the_method_type_is_set_on_the_function(self, name: str) -> None:
        """`rpc_exec` 对每个方法都要读它；漏了就是一被调用就 AttributeError"""
        from nekro_agent.services.plugin.schema import SandboxMethodType

        method, _is_write = OPS_METHODS[name]
        # 必须是 AGENT：返回值要回到模型眼前，TOOL 的返回值只进沙盒代码
        assert get_sandbox_method_type(method.func) is SandboxMethodType.AGENT
        assert method.method_type is SandboxMethodType.AGENT

    @pytest.mark.parametrize("name", sorted(OPS_TOOL_FUNCS))
    def test_each_method_carries_a_description(self, name: str) -> None:
        method, _is_write = OPS_METHODS[name]
        assert method.description and method.description != name

    def test_the_prefix_is_used_for_every_name(self) -> None:
        assert METHOD_PREFIX == "ops_"
        assert all(is_ops_method(name) for name in OPS_METHODS)

    def test_get_method_only_answers_for_ops_names(self) -> None:
        assert get_method("ops_overview") is OPS_TOOL_FUNCS["ops_overview"]
        assert get_method("dice_roll") is None
        assert get_method("ops_nonexistent") is None

    def test_the_write_methods_are_exactly_the_declared_ones(self) -> None:
        from nekro_agent.services.ops_agent.tools import OPS_WRITE_TOOLS

        assert {name for name in OPS_METHODS if is_write_method(name)} == set(OPS_WRITE_TOOLS)
        assert len(OPS_WRITE_TOOLS) == 6
        assert not is_write_method("dice_roll")
        assert not is_write_method("ops_overview")


class TestMethodsFor:
    async def test_no_context_means_no_methods(self) -> None:
        """`/exec` 调试路径就是 `ctx=None`：那里没有会话、没有授权，一个都不能给"""
        assert await methods_for(None) == []

    async def test_no_context_means_no_methods_even_with_a_live_grant(self, db, ops_on) -> None:  # noqa: ARG002
        """对照组：即使进程里恰好有一份授权，`ctx is None` 也必须给出空表"""
        await granted_channel()
        assert await methods_for(None) == []

    async def test_a_granted_round_gets_every_method(self, db, ops_on) -> None:  # noqa: ARG002
        await granted_channel()
        methods = await methods_for(ctx_of())
        assert {method.func.__name__ for method in methods} == set(OPS_TOOL_FUNCS)

    async def test_a_round_without_a_grant_gets_nothing(self, db, ops_on) -> None:  # noqa: ARG002
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            provider="remote_onebot",
        )
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id="group_10001",
            chat_key=CHAT_KEY,
            channel_type="group",
            data="{}",
        )
        assert await methods_for(ctx_of()) == []

    async def test_another_channel_does_not_see_the_grant(self, db, ops_on) -> None:  # noqa: ARG002
        await granted_channel()
        other = AgentCtx(from_chat_key=f"{ADAPTER}-{INSTANCE}-group_20002", container_key="sandbox_other")
        assert await methods_for(other) == []

    async def test_a_context_without_a_chat_key_gets_nothing(self, db, ops_on) -> None:  # noqa: ARG002
        await granted_channel()
        assert await methods_for(AgentCtx(container_key=CONTAINER)) == []

    async def test_a_closed_round_gets_nothing(self, db, ops_on) -> None:  # noqa: ARG002
        await granted_channel()
        grant_module.close_round(CONTAINER)
        assert await methods_for(ctx_of()) == []

    async def test_a_grant_signed_before_the_switch_went_off_is_still_listed(self, db, ops_on) -> None:  # noqa: ARG002
        """开关在回合中途被关掉时，方法清单与提示词仍按已签发的授权走

        理由是"哪里判什么"要分得清：`methods_for` 只看授权（授权在回合开始时按宿主事实签发），
        "开关此刻是否还开着"由执行前的复核判——那里会拒绝，模型看到的是明确的拒绝理由，
        而不是一个"工具突然消失了"的谜。开关**从一开始**关着时根本没有授权，清单自然是空的
        （见 `TestSandboxGrantWhitelist.test_switch_off_lists_none_of_them`）。
        """
        await granted_channel()
        ops_on(OPS_AGENT_TOOLS_ENABLED=False)
        assert await methods_for(ctx_of()) != []
        assert await grant_module.authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (False, "disabled")


class TestPromptSection:
    async def test_no_context_renders_nothing(self) -> None:
        assert render_prompt_section(None) == ""

    async def test_no_grant_renders_nothing(self, db, ops_on) -> None:  # noqa: ARG002
        assert render_prompt_section(ctx_of()) == ""

    async def test_a_granted_round_gets_the_section(self, db, ops_on) -> None:  # noqa: ARG002
        await granted_channel()
        section = render_prompt_section(ctx_of())

        assert section.startswith("### 运维工具")
        for name in OPS_TOOL_FUNCS:
            assert name in section, name
        for rule in ("只按运维人员这一轮说的话行动", "ops_find", "不需要再确认一次"):
            assert rule in section, rule

    async def test_every_tool_line_carries_its_signature_and_docstring(self, db, ops_on) -> None:  # noqa: ARG002
        await granted_channel()
        section = render_prompt_section(ctx_of())
        lines = [line for line in section.splitlines() if line.startswith("* ")]
        assert len(lines) == len(OPS_TOOL_FUNCS)
        for line in lines:
            assert "[AGENT METHOD - STOP AFTER CALL]" in line, line
            assert "ctx" not in line, line
        # 参数说明要进提示词，否则模型不知道 adapter_key 什么时候必填
        assert "* ops_instance_status(instance_key: str, adapter_key: str = '')" in section
        assert "同名实例存在于多个适配器下时必填" in section
        assert "* ops_recent_errors(limit: int = 20)" in section

    async def test_the_section_is_stable_across_calls(self, db, ops_on) -> None:  # noqa: ARG002
        await granted_channel()
        assert render_prompt_section(ctx_of()) == render_prompt_section(ctx_of())


# 一组「改造前就有」的输入：用它们证明运维块关着时一个字节都不多
COMPILER_INPUTS: dict[str, Any] = {
    "platform_name": "QQ",
    "bot_platform_id": "10001",
    "chat_preset": "你是一个测试人设。",
    "plugins_prompt": "* probe_plugin：探针",
    "plugins_runtime_prompt": "运行期插件说明",
    "plugin_activation_rules": "激活规则",
    "plugin_call_priority_rules": "优先级规则",
    "enable_cot": True,
    "chat_key_rules": "会话标识规则",
    "enable_at": False,
}


def rendered(**extra: Any) -> str:
    message = PromptCompiler(**{**COMPILER_INPUTS, **extra}).render_system_message()
    parts = message.content if isinstance(message.content, list) else [message.content]
    return "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in parts)


class TestTemplateIsByteIdentical:
    """模板里那段 `{%- if ops_prompt %}` 不能改变空串时的渲染结果

    存量部署的每一条系统提示词都走这条分支：多一个换行就是一次全量提示词漂移。
    """

    def test_the_empty_string_adds_not_a_single_byte(self) -> None:
        assert rendered() == rendered(ops_prompt="")

    def test_a_filled_section_is_appended_after_the_plugins_block(self) -> None:
        baseline = rendered()
        with_section = rendered(ops_prompt="### 运维工具\n* ops_overview：总览")
        assert with_section.startswith(baseline)
        assert with_section[len(baseline) :] == "### 运维工具\n* ops_overview：总览\n"

    def test_the_affection_block_still_precedes_the_runtime_contract(self) -> None:
        """对照组：另一个可选块的位置没被这次改动挪动"""
        text = rendered(affection_prompt="好感度块")
        assert text.index("好感度块") < text.index("会话标识规则")

    def test_the_plugins_block_is_still_present(self, ops_on) -> None:  # noqa: ARG002
        text = rendered(ops_prompt="### 运维工具")
        assert "<plugins>" in text and "</plugins>" in text


class TestCollectorWiring:
    async def test_no_context_gets_no_ops_methods(self, db, ops_on) -> None:  # noqa: ARG002
        """调试路径 `get_all_sandbox_methods(None)` 返回进程里启用的全部插件方法，运维方法绝不能混进去"""
        await granted_channel()
        methods = await plugin_collector.get_all_sandbox_methods(None)
        assert not [method for method in methods if method.func.__name__.startswith(METHOD_PREFIX)]

    async def test_a_granted_round_gets_them_appended(self, db, ops_on) -> None:  # noqa: ARG002
        await granted_channel()
        methods = await plugin_collector.get_all_sandbox_methods(ctx_of())
        assert set(OPS_TOOL_FUNCS) <= {method.func.__name__ for method in methods}

    async def test_a_round_without_a_grant_gets_none(self, db, ops_on) -> None:  # noqa: ARG002
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            provider="remote_onebot",
        )
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id="group_10001",
            chat_key=CHAT_KEY,
            channel_type="group",
            data="{}",
        )
        methods = await plugin_collector.get_all_sandbox_methods(ctx_of())
        assert not [method for method in methods if method.func.__name__.startswith(METHOD_PREFIX)]


class TestSandboxGrantWhitelist:
    """写进容器的代理清单同时就是那一轮 RPC 的白名单：两边必须同源"""

    @staticmethod
    def issued_method_names() -> frozenset[str]:
        from nekro_agent.services.sandbox.credential_guard import _grants  # noqa: PLC0415

        grant = _grants.get(CONTAINER)
        assert grant is not None, "许可没有签发，下面的断言无从谈起"
        return grant.method_names

    async def test_the_granted_round_lists_the_ops_methods(self, db, ops_on) -> None:  # noqa: ARG002
        from nekro_agent.services.sandbox import ext_caller
        from nekro_agent.services.sandbox.credential_guard import revoke_sandbox_call_grant

        await granted_channel()
        try:
            code = await ext_caller.get_api_caller_code(container_key=CONTAINER, from_chat_key=CHAT_KEY, ctx=ctx_of())
            names = self.issued_method_names()
            assert set(OPS_TOOL_FUNCS) <= set(names)
            # 代理确实写进了容器代码：模型得有办法调到它们
            for name in OPS_TOOL_FUNCS:
                assert f"def {name}(" in code, name
        finally:
            revoke_sandbox_call_grant(CONTAINER)

    async def test_a_round_without_a_grant_lists_none_of_them(self, db, ops_on) -> None:  # noqa: ARG002
        from nekro_agent.services.sandbox import ext_caller
        from nekro_agent.services.sandbox.credential_guard import revoke_sandbox_call_grant

        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            provider="remote_onebot",
        )
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id="group_10001",
            chat_key=CHAT_KEY,
            channel_type="group",
            data="{}",
        )
        try:
            code = await ext_caller.get_api_caller_code(container_key=CONTAINER, from_chat_key=CHAT_KEY, ctx=ctx_of())
            names = self.issued_method_names()
            assert not (set(OPS_TOOL_FUNCS) & set(names))
            for name in OPS_TOOL_FUNCS:
                assert f"def {name}(" not in code, name
        finally:
            revoke_sandbox_call_grant(CONTAINER)

    async def test_switch_off_lists_none_of_them(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """开关关着时清单与改造前逐字一致：一个 `ops_` 前缀都不出现"""
        from nekro_agent.services.sandbox import ext_caller
        from nekro_agent.services.sandbox.credential_guard import revoke_sandbox_call_grant

        monkeypatch.setattr(config, "OPS_AGENT_TOOLS_ENABLED", False)
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            provider="remote_onebot",
        )
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id="group_10001",
            chat_key=CHAT_KEY,
            channel_type="group",
            data="{}",
        )
        try:
            await ext_caller.get_api_caller_code(container_key=CONTAINER, from_chat_key=CHAT_KEY, ctx=ctx_of())
            assert not [name for name in self.issued_method_names() if name.startswith(METHOD_PREFIX)]
        finally:
            revoke_sandbox_call_grant(CONTAINER)


class TestNoPluginCollision:
    def test_no_loaded_plugin_method_starts_with_the_prefix(self) -> None:
        """同名会被抢先分派：插件里出现一个 `ops_` 方法就等于跳过了执行前的复核"""
        offenders: List[str] = []
        for plugin in plugin_collector.loaded_plugins.values():
            for method in plugin.sandbox_methods:
                if method.func.__name__.startswith(METHOD_PREFIX):
                    offenders.append(f"{plugin.key}:{method.func.__name__}")
        assert not offenders, offenders

    def test_the_builtin_plugin_sources_do_not_use_the_prefix(self) -> None:
        """已加载插件取决于运行环境；源码级再扫一遍，把没加载的那些也挡住"""
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "plugins"
        offenders: List[str] = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                name = ""
                if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    name = node.name
                elif isinstance(node, ast.Call):
                    keywords = {kw.arg: kw.value for kw in node.keywords}
                    literal = keywords.get("name")
                    if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                        name = literal.value
                if name.startswith(METHOD_PREFIX):
                    offenders.append(f"{path.relative_to(root).as_posix()}:{name}")
        assert not offenders, offenders


class TestToolEntryGuard:
    """工具函数被别处（调试、脚本）直接调用时也要先取授权"""

    # 需要位置参数的工具补一个占位值：授权复查排在这些参数的使用之前，值不会被读到
    REQUIRED: dict[str, dict[str, Any]] = {
        "ops_find": {"keyword": "inst1"},
        "ops_instance_status": {"instance_key": "inst1"},
        "ops_channel_quota": {"chat_key": CHAT_KEY},
        "ops_set_instance_enabled": {"adapter_key": ADAPTER, "instance_key": "inst1", "enabled": True},
        "ops_restart_protocol": {"adapter_key": ADAPTER, "instance_key": "inst1"},
        "ops_set_tenant_status": {"tenant_id": 1, "status": "active"},
        "ops_set_channel_active": {"chat_key": CHAT_KEY, "active": True},
        "ops_quota_bonus": {"chat_key": CHAT_KEY, "amount": 1},
    }

    @pytest.mark.parametrize("name", sorted(OPS_TOOL_FUNCS))
    async def test_an_ungranted_context_gets_the_fixed_notice(self, name: str) -> None:
        func = OPS_TOOL_FUNCS[name]
        result = await func(AgentCtx(from_chat_key=CHAT_KEY, container_key="sandbox_no_grant"), **self.REQUIRED.get(name, {}))
        assert result == UNAVAILABLE

    async def test_a_context_without_a_container_key_still_gets_the_notice(self) -> None:
        result = await OPS_TOOL_FUNCS["ops_overview"](AgentCtx(from_chat_key=CHAT_KEY))
        assert result == UNAVAILABLE
