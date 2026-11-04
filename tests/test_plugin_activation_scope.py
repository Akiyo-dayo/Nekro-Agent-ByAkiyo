"""`activate_plugin` / `extend_plugin_activation` 只认当前会话看得见的插件

修复前：激活前查插件只看进程级 `is_enabled`，不走 `scope.resolve_active_plugins()`。在当前实例或会话里被关掉的
可休眠插件，模型按名字调一次 `activate_plugin` 就能拿到它的完整插件块——方法说明、按当前会话渲染的注入上下文。
不支持当前适配器的插件同理。

现在看得见的集合与本轮提示词、沙盒方法白名单同一个来源（会话 > 实例 > 全局，再按适配器过滤），
集合外的插件一律按「插件不存在」回答，激活状态也不写。
"""

from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.plugin import isolation
from nekro_agent.services.plugin import scope as scope_mod
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.prompt_activation import PluginActivationState
from nekro_agent.services.plugin.schema import SandboxMethodType
from nekro_agent.services.plugin.scope import OverrideScope, set_scope_override
from plugins.builtin import plugin_activation

ONEBOT = "onebot_v11"
SLEEPY = "scope_probe_sleepy"
AWAKE = "scope_probe_awake"
TG_ONLY = "scope_probe_tg_only"
INJECTED = "INJECTED-CONTEXT-OF-THE-PROBE"
METHOD_DOC = "PROBE-METHOD-DOC"
MISSING = "is missing or disabled"


def chat_key_of(instance_key: str) -> str:
    return f"{ONEBOT}-{instance_key}-group_1"


def _make_plugin(module_name: str, *, sleepable: bool, support_adapter: Optional[List[str]] = None) -> NekroPlugin:
    plugin = NekroPlugin(
        name=f"探针 {module_name}",
        module_name=module_name,
        description="仅用于激活作用域测试",
        version="0.1.0",
        author="Probe",
        url="",
        support_adapter=support_adapter,
        allow_sleep=True if sleepable else None,
        sleep_brief="探针摘要" if sleepable else "",
    )

    @plugin.mount_prompt_inject_method("probe_inject")
    async def _inject(_ctx: AgentCtx) -> str:
        return INJECTED

    @plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="探针方法", description="探针")
    async def probe_method(_ctx: AgentCtx) -> str:
        """PROBE-METHOD-DOC"""
        return "ok"

    return plugin


@pytest.fixture
def probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dict[str, NekroPlugin]]:
    # 作用域覆盖是按层级聚合的单文件，整体改道到临时目录，否则会写进开发机的真实数据目录
    data_dir = tmp_path / "plugin_data"
    monkeypatch.setattr(scope_mod, "PLUGIN_DATA_DIR", data_dir)
    monkeypatch.setattr(scope_mod, "SCOPE_OVERRIDE_DIR", data_dir / "_overrides")
    scope_mod._scope_override_cache.clear()  # noqa: SLF001
    scope_mod._scoped_config_cache.clear()  # noqa: SLF001
    monkeypatch.setattr(config, "PLUGIN_ACTIVATION_STRATEGIES", {})
    monkeypatch.setattr(config, "PLUGIN_CALL_PRIORITIES", {})

    plugins = {
        SLEEPY: _make_plugin(SLEEPY, sleepable=True),
        AWAKE: _make_plugin(AWAKE, sleepable=False),
        TG_ONLY: _make_plugin(TG_ONLY, sleepable=True, support_adapter=["telegram"]),
    }
    monkeypatch.setattr(config, "PLUGIN_ENABLED", [*config.PLUGIN_ENABLED, *(plugin.key for plugin in plugins.values())])
    for plugin in plugins.values():
        plugin_collector.loaded_plugins[plugin.key] = plugin
    try:
        yield plugins
    finally:
        for plugin in plugins.values():
            plugin_collector.loaded_plugins.pop(plugin.key, None)
            isolation.release_quarantine(plugin.key, reason="测试结束")
        scope_mod._scope_override_cache.clear()  # noqa: SLF001
        scope_mod._scoped_config_cache.clear()  # noqa: SLF001


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, str, str]]:
    """激活状态打桩成只记账：(动作, chat_key, module_name)"""
    calls: List[Tuple[str, str, str]] = []

    async def _state(_chat_key: str) -> PluginActivationState:
        return PluginActivationState()

    async def _activate(chat_key: str, module_name: str, rounds: Optional[int] = None) -> int:
        calls.append(("activate", chat_key, module_name))
        return rounds or 3

    async def _extend(chat_key: str, module_name: str, rounds: Optional[int] = None) -> int:
        calls.append(("extend", chat_key, module_name))
        return rounds or 3

    monkeypatch.setattr(plugin_activation, "get_activation_state", _state)
    monkeypatch.setattr(plugin_activation, "activate_module_for_chat", _activate)
    monkeypatch.setattr(plugin_activation, "extend_module_for_chat", _extend)
    return calls


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for tenant_id, instance_key in ((1, "inst1"), (2, "inst2")):
            await DBTenant.create(id=tenant_id, name=f"tenant{tenant_id}", display_name=f"tenant{tenant_id}")
            await DBAdapterInstance.create(
                adapter_key=ONEBOT,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                provider_account_id=f"2000{tenant_id}",
                tenant_id=tenant_id,
            )
            await DBChatChannel.create(
                adapter_key=ONEBOT,
                instance_key=instance_key,
                channel_id="group_1",
                chat_key=chat_key_of(instance_key),
                channel_type="group",
                data="{}",
            )
        yield
    finally:
        await Tortoise.close_connections()


async def ctx_for(instance_key: str, *, with_channel: bool = True) -> AgentCtx:
    """`with_channel` = 沙盒回调那条路（`AgentCtx.create_by_chat_key` 带着频道行）；否则只有 chat_key，要回库里查"""
    chat_key = chat_key_of(instance_key)
    if with_channel:
        return await AgentCtx.create_by_chat_key(chat_key=chat_key)
    return AgentCtx(from_chat_key=chat_key, adapter_key=ONEBOT)


async def activate(instance_key: str, module_name: str = SLEEPY, *, with_channel: bool = True) -> str:
    return await plugin_activation.activate_plugin(await ctx_for(instance_key, with_channel=with_channel), module_name)


async def extend(instance_key: str, module_name: str = SLEEPY) -> str:
    return await plugin_activation.extend_plugin_activation(await ctx_for(instance_key), module_name)


def assert_refused_without_leaking(result: str) -> None:
    assert MISSING in result
    assert INJECTED not in result
    assert METHOD_DOC not in result


class TestScopeClosedPluginIsInvisible:
    async def test_closed_in_this_session(self, db, probes, recorded) -> None:
        set_scope_override(OverrideScope.SESSION, chat_key_of("inst1"), probes[SLEEPY].key, False)

        assert_refused_without_leaking(await activate("inst1"))
        assert recorded == []

    async def test_closed_in_this_instance(self, db, probes, recorded) -> None:
        set_scope_override(OverrideScope.INSTANCE, "inst1", probes[SLEEPY].key, False)

        assert_refused_without_leaking(await activate("inst1"))
        assert recorded == []

    async def test_instance_is_found_from_the_chat_key_when_the_context_has_no_channel(self, db, probes, recorded) -> None:
        set_scope_override(OverrideScope.INSTANCE, "inst1", probes[SLEEPY].key, False)

        assert_refused_without_leaking(await activate("inst1", with_channel=False))
        assert recorded == []

    async def test_extend_is_refused_as_well(self, db, probes, recorded) -> None:
        set_scope_override(OverrideScope.SESSION, chat_key_of("inst1"), probes[SLEEPY].key, False)

        assert MISSING in await extend("inst1")
        assert recorded == []

    @pytest.mark.parametrize("method", ["activate_plugin", "extend_plugin_activation"])
    async def test_closed_always_visible_plugin_is_reported_missing(self, db, probes, recorded, method: str) -> None:
        """常驻插件被关掉时也不能回「它一直可见、直接用」"""
        set_scope_override(OverrideScope.INSTANCE, "inst1", probes[AWAKE].key, False)

        result = await getattr(plugin_activation, method)(await ctx_for("inst1"), AWAKE)
        assert MISSING in result
        assert "always visible" not in result

    async def test_plugin_for_another_adapter_is_invisible(self, db, probes, recorded) -> None:
        assert_refused_without_leaking(await activate("inst1", TG_ONLY))
        assert recorded == []

    async def test_plugin_the_platform_has_not_enabled_is_invisible(
        self, db, probes, recorded, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """全局没开、本实例 / 本会话也没单独打开的插件：进程里活着（别的实例在用）也激活不了"""
        monkeypatch.setattr(config, "PLUGIN_ENABLED", [key for key in config.PLUGIN_ENABLED if key != probes[SLEEPY].key])
        set_scope_override(OverrideScope.INSTANCE, "inst2", probes[SLEEPY].key, True)

        assert_refused_without_leaking(await activate("inst1"))
        assert recorded == []


class TestTenantBoundary:
    """作用域只按调用方自己的会话解析：inst1 属于租户 A、inst2 属于租户 B"""

    async def test_another_tenants_instance_enabling_a_plugin_does_not_open_it_here(
        self, db, probes, recorded, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "PLUGIN_ENABLED", [key for key in config.PLUGIN_ENABLED if key != probes[SLEEPY].key])
        set_scope_override(OverrideScope.INSTANCE, "inst2", probes[SLEEPY].key, True)

        assert_refused_without_leaking(await activate("inst1"))
        # 租户 B 自己照常能用；激活状态只写进各自的会话
        assert INJECTED in await activate("inst2")
        assert recorded == [("activate", chat_key_of("inst2"), SLEEPY)]

    async def test_another_tenants_session_closing_a_plugin_does_not_close_it_here(self, db, probes, recorded) -> None:
        set_scope_override(OverrideScope.SESSION, chat_key_of("inst2"), probes[SLEEPY].key, False)

        assert INJECTED in await activate("inst1")
        assert MISSING in await activate("inst2")
        assert recorded == [("activate", chat_key_of("inst1"), SLEEPY)]

    @pytest.mark.parametrize("method", ["activate_plugin", "extend_plugin_activation"])
    def test_the_model_cannot_name_another_chat(self, method: str) -> None:
        """会话只来自上下文：沙盒回调按本轮签发的调用许可（容器令牌 + 绑定的会话）构造 ctx，方法本身不收会话参数"""
        import inspect

        assert list(inspect.signature(getattr(plugin_activation, method)).parameters) == ["_ctx", "module_name", "rounds"]


class TestControls:
    async def test_visible_plugin_is_activated_with_its_full_block(self, db, probes, recorded) -> None:
        result = await activate("inst1")

        assert "is now activated" in result
        assert INJECTED in result and METHOD_DOC in result
        assert recorded == [("activate", chat_key_of("inst1"), SLEEPY)]

    async def test_another_instance_is_unaffected(self, db, probes, recorded) -> None:
        set_scope_override(OverrideScope.INSTANCE, "inst1", probes[SLEEPY].key, False)

        assert INJECTED in await activate("inst2")
        assert recorded == [("activate", chat_key_of("inst2"), SLEEPY)]

    async def test_enabled_only_in_this_session_is_visible(self, db, probes, recorded, monkeypatch: pytest.MonkeyPatch) -> None:
        """全局关着、会话里单独打开：照样能激活（看的是解析后的集合，不只是全局开关）"""
        monkeypatch.setattr(config, "PLUGIN_ENABLED", [key for key in config.PLUGIN_ENABLED if key != probes[SLEEPY].key])
        set_scope_override(OverrideScope.SESSION, chat_key_of("inst1"), probes[SLEEPY].key, True)

        assert INJECTED in await activate("inst1")
        assert MISSING in await activate("inst2")

    async def test_extend_on_a_visible_plugin(self, db, probes, recorded) -> None:
        assert "visibility extended" in await extend("inst1")
        assert recorded == [("extend", chat_key_of("inst1"), SLEEPY)]

    async def test_visible_always_visible_plugin_says_so(self, db, probes, recorded) -> None:
        assert "always visible" in await activate("inst1", AWAKE)
        assert recorded == []
