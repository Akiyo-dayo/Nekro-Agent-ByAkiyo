"""沙盒方法清单必须与提示词用同一套作用域判据

保护的契约（对应一个真实故障）：写进容器 `api_caller.py` 的方法代理清单，同时就是那一轮
RPC 调用许可的白名单（`services/sandbox/ext_caller.py` -> `credential_guard.issue_sandbox_call_grant`
-> `routers/rpc.py` 的 `rpc_request.method not in grant.method_names`）。它此前只按
**进程级** `plugin.is_enabled` 过滤，而提示词按 `会话 > 实例 > 全局` 解析，两者不是一回事：

- 全局开启、在某个实例上关掉的插件，提示词里没有它，容器里却仍有代理，调用真的执行成功；
- 全局关闭、只在 inst1 上开启的插件，它的方法会出现在 **inst2** 容器的代理清单里 ——
  一个租户为自己实例打开的能力，成了所有实例都能调的能力。

两条都在 测试部署的真实链路上复现过（模型提示词里没有 `dice`，沙盒里
`dice_roll(...)` 照样把检定结果发进了群）。因此这里断言的是「清单 == 作用域解析结果」，
而不是某个实现细节。
"""

from pathlib import Path
from typing import List, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.plugin import scope as scope_mod
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.schema import SandboxMethodType

INST1_CHAT_KEY = "onebot_v11-inst1-group_10001"
INST1_OTHER_CHAT_KEY = "onebot_v11-inst1-group_10002"
INST2_CHAT_KEY = "onebot_v11-inst2-group_20001"

CHANNELS: List[Tuple[str, str]] = [
    ("inst1", INST1_CHAT_KEY),
    ("inst1", INST1_OTHER_CHAT_KEY),
    ("inst2", INST2_CHAT_KEY),
]

PROBE_METHOD = "scope_probe_method"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, chat_key in CHANNELS:
            await DBChatChannel.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
                chat_key=chat_key,
                channel_type="group",
                data="{}",
            )
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def probe(tmp_path: Path):
    """一个带沙盒方法的探针插件；作用域覆盖整体改道到 tmp_path

    覆盖是按层级聚合的单文件，写进真实 DATA_DIR 会污染开发机上真实部署的插件状态。
    """
    original_dirs = (scope_mod.PLUGIN_DATA_DIR, scope_mod.SCOPE_OVERRIDE_DIR)
    scope_mod.PLUGIN_DATA_DIR = tmp_path / "plugin_data"
    scope_mod.SCOPE_OVERRIDE_DIR = tmp_path / "plugin_data" / "_overrides"
    scope_mod._scope_override_cache.clear()  # noqa: SLF001

    plugin = NekroPlugin(
        name="作用域探针",
        module_name="sandbox_scope_probe",
        description="仅用于沙盒方法作用域测试",
        version="0.1.0",
        author="Probe",
        url="",
    )

    @plugin.mount_sandbox_method(SandboxMethodType.TOOL, "探针方法")
    async def scope_probe_method(_ctx: AgentCtx) -> str:
        """探针方法，用于断言它是否被下发进容器"""
        return "ok"

    plugin._is_enabled = True  # noqa: SLF001
    plugin_collector.loaded_plugins[plugin.key] = plugin
    original_enabled = list(config.PLUGIN_ENABLED)
    try:
        yield plugin
    finally:
        config.PLUGIN_ENABLED = original_enabled
        plugin_collector.loaded_plugins.pop(plugin.key, None)
        scope_mod.PLUGIN_DATA_DIR, scope_mod.SCOPE_OVERRIDE_DIR = original_dirs
        scope_mod._scope_override_cache.clear()  # noqa: SLF001


def set_global(plugin: NekroPlugin, enabled: bool) -> None:
    keys = [key for key in config.PLUGIN_ENABLED if key != plugin.key]
    config.PLUGIN_ENABLED = [*keys, plugin.key] if enabled else keys


async def exposed_method_names(chat_key: str) -> List[str]:
    """走 ext_caller 真正调用的那条路径拿到下发清单"""
    ctx = await AgentCtx.create_by_chat_key(chat_key=chat_key)
    return [method.func.__name__ for method in await plugin_collector.get_all_sandbox_methods(ctx)]


class TestSandboxMethodScopeResolution:
    """下发清单必须逐层跟随 会话 > 实例 > 全局"""

    @pytest.mark.parametrize(
        ("global_enabled", "instance_overrides", "session_overrides", "chat_key", "expected"),
        [
            # 无任何覆盖：跟随全局
            (True, {}, {}, INST1_CHAT_KEY, True),
            (False, {}, {}, INST1_CHAT_KEY, False),
            # 实例级关闭：该实例的容器里不该再有代理（此前是提示词没有、沙盒仍有）
            (True, {"inst2": False}, {}, INST2_CHAT_KEY, False),
            (True, {"inst2": False}, {}, INST1_CHAT_KEY, True),
            # 会话级再打开：只影响那一个会话，同实例的另一个会话仍然关着
            (True, {"inst1": False}, {INST1_CHAT_KEY: True}, INST1_CHAT_KEY, True),
            (True, {"inst1": False}, {INST1_CHAT_KEY: True}, INST1_OTHER_CHAT_KEY, False),
            # 全局关闭、只有 inst1 打开：inst2 的容器不该拿到别人开的能力
            (False, {"inst1": True}, {}, INST1_CHAT_KEY, True),
            (False, {"inst1": True}, {}, INST2_CHAT_KEY, False),
        ],
    )
    async def test_exposed_methods_follow_scope(
        self,
        db,
        probe: NekroPlugin,
        global_enabled: bool,
        instance_overrides: dict,
        session_overrides: dict,
        chat_key: str,
        expected: bool,
    ) -> None:
        set_global(probe, global_enabled)
        for instance_key, value in instance_overrides.items():
            scope_mod.set_scope_override(scope_mod.OverrideScope.INSTANCE, instance_key, probe.key, value)
        for session_key, value in session_overrides.items():
            scope_mod.set_scope_override(scope_mod.OverrideScope.SESSION, session_key, probe.key, value)

        names = await exposed_method_names(chat_key)
        assert (PROBE_METHOD in names) is expected

    async def test_scope_result_matches_prompt_side_resolution(self, db, probe: NekroPlugin) -> None:
        """同一份作用域下，沙盒清单与提示词用的插件集合必须给出同一个答案

        这条是整组用例的锚：两侧一旦再次用上不同判据，这里就会红。
        """
        set_global(probe, True)
        scope_mod.set_scope_override(scope_mod.OverrideScope.INSTANCE, "inst2", probe.key, False)

        for instance_key, chat_key in (("inst1", INST1_CHAT_KEY), ("inst2", INST2_CHAT_KEY)):
            prompt_side = {
                plugin.key
                for plugin in await scope_mod.resolve_active_plugins(instance_key=instance_key, chat_key=chat_key)
            }
            sandbox_side = PROBE_METHOD in await exposed_method_names(chat_key)
            assert (probe.key in prompt_side) is sandbox_side


class TestSandboxMethodAdapterSupport:
    """`support_adapter` 与提示词侧同口径：当前适配器用不了的方法不下发"""

    @pytest.mark.parametrize(
        ("support_adapter", "expected"),
        [
            ([], True),
            (["onebot_v11"], True),
            (["minecraft"], False),
        ],
    )
    async def test_unsupported_adapter_is_not_exposed(
        self,
        db,
        probe: NekroPlugin,
        support_adapter: List[str],
        expected: bool,
    ) -> None:
        set_global(probe, True)
        probe.support_adapter = support_adapter
        assert (PROBE_METHOD in await exposed_method_names(INST1_CHAT_KEY)) is expected


class TestSandboxMethodWithoutContext:
    """没有会话上下文时（`/debug` 命令直接跑代码）保持改造前行为"""

    async def test_no_ctx_falls_back_to_process_level(self, db, probe: NekroPlugin) -> None:
        set_global(probe, True)
        scope_mod.set_scope_override(scope_mod.OverrideScope.INSTANCE, "inst1", probe.key, False)

        names = [method.func.__name__ for method in await plugin_collector.get_all_sandbox_methods(None)]
        assert PROBE_METHOD in names


class TestCollectMethodsHookStillApplies:
    """插件自己的 `collect_methods` 钩子必须继续对沙盒清单生效

    它是「按上下文隐藏方法」的既有机制（心声插件就靠它只在布防轮次露出 `inner_voice`），
    作用域收窄不能把它绕过去。
    """

    async def test_hook_can_hide_method_in_one_channel(self, db, probe: NekroPlugin) -> None:
        set_global(probe, True)

        @probe.mount_collect_methods()
        async def collect(ctx: AgentCtx) -> List[object]:
            return [] if ctx.from_chat_key == INST1_OTHER_CHAT_KEY else list(probe.sandbox_methods)

        assert PROBE_METHOD in await exposed_method_names(INST1_CHAT_KEY)
        assert PROBE_METHOD not in await exposed_method_names(INST1_OTHER_CHAT_KEY)


class TestLocalOnlyMethodFollowsScope:
    """容器内本地实现的方法也要跟随作用域

    `dynamic_importer` 的函数体直接写在 `ext_caller_code.py` 模板里，不经 RPC、也不进
    调用白名单，因此不受下发清单约束。审计实测：该插件在所有作用域下都关闭，容器里仍然可用 ——
    「关掉这个插件」在模型侧毫无效果，与其它插件的语义不一致。

    生成的代码在本轮不可见时把名字从命名空间里摘掉，于是调用它得到 `NameError`，
    与其它「本轮不可见」的方法表现一致，模型不必学第二种失败形态。
    """

    async def test_revoked_when_out_of_scope(self, db: None, probe: NekroPlugin) -> None:
        from nekro_agent.services.sandbox import ext_caller

        code = await ext_caller.get_api_caller_code(
            container_key="probe-container",
            from_chat_key=INST1_CHAT_KEY,
            ctx=await AgentCtx.create_by_chat_key(chat_key=INST1_CHAT_KEY),
        )
        # 探针插件没有本地方法，因此 dynamic_importer 必然不在解析结果里
        assert 'globals().pop("dynamic_importer", None)' in code

    async def test_kept_when_in_scope(self, db: None, probe: NekroPlugin) -> None:
        """解析结果里有该本地方法时不得摘除，否则等于把正常能力砍掉"""
        from nekro_agent.services.sandbox import ext_caller

        @probe.mount_sandbox_method(SandboxMethodType.TOOL, "本地方法替身")
        async def dynamic_importer(_ctx: AgentCtx) -> str:
            """占位：让解析结果里出现同名本地方法"""
            return "ok"

        set_global(probe, enabled=True)
        code = await ext_caller.get_api_caller_code(
            container_key="probe-container",
            from_chat_key=INST1_CHAT_KEY,
            ctx=await AgentCtx.create_by_chat_key(chat_key=INST1_CHAT_KEY),
        )
        assert 'globals().pop("dynamic_importer", None)' not in code
        # 本地方法不生成 RPC 代理，摘除与否都不该让它进白名单
        assert "def dynamic_importer(*args, **kwargs)" not in code
