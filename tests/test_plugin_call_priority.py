"""插件调用优先级

档位（`auto` / `high` / `medium` / `low`）全进程一份，渲染成提示词里插件块的 `call_priority` 属性，
再由运行时契约里的一段规则告诉模型怎么用。本模块钉住几条没有运行时信号的契约：

1. 落盘格式：`auto` 不落盘；写下去的取值必须能被配置字段的声明类型读回来，否则下次启动配置就加载失败；
2. 保存失败时内存回滚，接口报错，提示词不能已经按新档位渲染；
3. 披露视图的三种状态（休眠 / 激活 / 常驻）、旧版渲染、`activate_plugin` 返回的整块，都带上档位；
4. 规则段真的进了 `run_agent` 组装的系统提示词（调用点不在 = 零件全对、功能为零）；
5. 改档位只有平台方能做：租户管理员在插件列表里看得到档位，改它被 403，配置不动。
"""

import ast
import datetime
import time
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Tuple

import pytest
import yaml
from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.plugins import (
    PluginCallPriorityRequest,
    get_plugin_detail_handler,
    get_plugins,
    update_plugin_call_priority_handler,
)
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.agent.templates.compiler import PromptCompiler
from nekro_agent.services.agent.templates.plugin import (
    PluginPromptRenderUnit,
    render_plugin_prompt_unit,
    render_plugins_prompt,
    render_plugins_prompt_legacy,
)
from nekro_agent.services.config_service import ConfigService
from nekro_agent.services.plugin import call_priority as call_priority_mod
from nekro_agent.services.plugin import isolation
from nekro_agent.services.plugin import prompt_activation as pa
from nekro_agent.services.plugin import scope as scope_mod
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.call_priority import (
    build_plugin_call_priority_rules,
    get_plugin_call_priority,
    set_plugin_call_priority,
)
from nekro_agent.services.plugin.collector import FailedPluginInfo, plugin_collector
from nekro_agent.services.plugin.manager import get_all_ext_meta_data, get_plugin_detail
from nekro_agent.services.plugin.schema import SandboxMethodType
from nekro_agent.services.user.role import Role

_REAL_SAVE_CONFIG = ConfigService.save_config
RUN_AGENT_PATH = Path(__file__).resolve().parents[1] / "nekro_agent" / "services" / "agent" / "run_agent.py"


@pytest.fixture(autouse=True)
def isolated_priorities(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[object, ...]]:
    """每条用例从空字典起步，落盘打桩成只记账

    不打桩的话 `set_plugin_call_priority` 会把整份全局配置写回开发机上真实的配置文件。
    """
    saved: List[Tuple[object, ...]] = []

    def _record(*args: object) -> Tuple[bool, Optional[str]]:
        saved.append(args)
        return True, None

    monkeypatch.setattr(config, "PLUGIN_CALL_PRIORITIES", {})
    monkeypatch.setattr(ConfigService, "save_config", _record)
    return saved


def _make_plugin(module_name: str, *, allow_sleep: Optional[bool] = None, sleep_brief: str = "") -> NekroPlugin:
    return NekroPlugin(
        name=f"探针 {module_name}",
        module_name=module_name,
        description="仅用于调用优先级测试",
        version="0.1.0",
        author="Probe",
        url="",
        allow_sleep=allow_sleep,
        sleep_brief=sleep_brief,
    )


def _fake_ctx():
    return SimpleNamespace(chat_key="onebot_v11-inst1-group_1", adapter_key="onebot_v11")


def _make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def tenant_admin() -> DBUser:
    return _make_user(tenant_id=1, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return _make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


@pytest.fixture
def probe():
    plugin = _make_plugin("call_priority_probe")
    plugin_collector.loaded_plugins[plugin.key] = plugin
    try:
        yield plugin
    finally:
        plugin_collector.loaded_plugins.pop(plugin.key, None)


class TestStoreSemantics:
    def test_priorities_have_no_instance_or_channel_override(self) -> None:
        """只有全局一份：不进覆盖模型，租户就没法在自己的实例 / 频道覆盖里写它"""
        from nekro_agent.core.overridable_config import OverridableConfig

        override_fields = set(OverridableConfig.model_fields)
        # 反向锚点：覆盖模型本身不是空的，否则下面两条断言恒真
        assert any(name.startswith("enable_") for name in override_fields)
        assert "PLUGIN_CALL_PRIORITIES" not in override_fields
        assert "enable_PLUGIN_CALL_PRIORITIES" not in override_fields

    def test_defaults_to_auto(self) -> None:
        assert get_plugin_call_priority("example") == "auto"

    def test_unknown_stored_value_falls_back_to_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """绕过校验写进内存的怪值不能一路渲染进提示词

        配置文件里的非法值在加载时就会被字段校验拒掉（进程起不来），走不到这里；
        这条回退兜的是运行中被直接赋值、没经过校验的那份字典。
        """
        monkeypatch.setattr(config, "PLUGIN_CALL_PRIORITIES", {"example": "urgent"})
        assert get_plugin_call_priority("example") == "auto"

    def test_set_persists_override(self, isolated_priorities: List[Tuple[object, ...]]) -> None:
        result = set_plugin_call_priority("example", "high")

        assert result == (True, None)
        assert config.PLUGIN_CALL_PRIORITIES == {"example": "high"}
        assert len(isolated_priorities) == 1

    def test_auto_removes_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "PLUGIN_CALL_PRIORITIES", {"example": "low", "other": "high"})

        set_plugin_call_priority("example", "auto")

        assert config.PLUGIN_CALL_PRIORITIES == {"other": "high"}

    def test_rolls_back_when_persistence_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = {"example": "low"}
        monkeypatch.setattr(config, "PLUGIN_CALL_PRIORITIES", original)
        monkeypatch.setattr(ConfigService, "save_config", lambda *_args: (False, "disk full"))

        result = set_plugin_call_priority("example", "high")

        assert result == (False, "disk full")
        assert config.PLUGIN_CALL_PRIORITIES is original
        assert get_plugin_call_priority("example") == "low"


class TestOnDiskFormat:
    """真落一次盘再读回来：只看内存里的 round-trip，等于落盘格式一个字都没被测到"""

    def _persisted(self, path: Path) -> dict:
        return yaml.safe_load(path.read_text(encoding="utf-8"))["PLUGIN_CALL_PRIORITIES"]

    def test_written_values_load_back_under_the_declared_type(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "config.yaml"
        written: List[Path] = []

        def _save_into_tmp(config_obj, file_path=None):  # noqa: ANN001, ANN202
            # 先断言再落盘：实现哪天改走别的路径，这里在写之前就红，而不是写完开发机的配置才红
            assert file_path == target, f"落盘路径不是测试目录：{file_path!r}"
            written.append(file_path)
            return _REAL_SAVE_CONFIG(config_obj, file_path)

        monkeypatch.setattr(ConfigService, "save_config", _save_into_tmp)
        monkeypatch.setattr(call_priority_mod, "CONFIG_PATH", target)
        # 兜底：即便绕过 `ConfigService` 直接 `dump_config()`，默认路径也指向测试目录
        monkeypatch.setattr(CoreConfig, "_config_file_path_class", target)
        monkeypatch.setattr(config, "_config_file_path", target)
        field_type = TypeAdapter(CoreConfig.model_fields["PLUGIN_CALL_PRIORITIES"].annotation)

        assert set_plugin_call_priority("probe_a", "high") == (True, None)
        assert set_plugin_call_priority("probe_b", "low") == (True, None)
        assert self._persisted(target) == {"probe_a": "high", "probe_b": "low"}

        # `auto` 必须是「删掉这一项」而不是写一个 auto：声明类型里没有 auto，写下去下次启动就读不回来
        assert set_plugin_call_priority("probe_a", "auto") == (True, None)
        persisted = self._persisted(target)
        assert persisted == {"probe_b": "low"}
        assert field_type.validate_python(persisted) == {"probe_b": "low"}
        assert written == [target, target, target]


class TestPromptRendering:
    async def test_plugin_block_exposes_call_priority(self) -> None:
        prompt = await render_plugin_prompt_unit(
            PluginPromptRenderUnit(
                plugin_name="Example",
                module_name="example",
                state="always_awake",
                call_priority="high",
            ),
        )
        assert 'call_priority="high"' in prompt

    async def test_unit_defaults_to_auto(self) -> None:
        prompt = await render_plugin_prompt_unit(
            PluginPromptRenderUnit(plugin_name="Example", module_name="example", state="always_awake"),
        )
        assert 'call_priority="auto"' in prompt

    async def test_every_disclosure_state_carries_the_configured_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeping = _make_plugin("probe_sleeping", allow_sleep=True, sleep_brief="休眠时的能力摘要")
        active = _make_plugin("probe_active", allow_sleep=True, sleep_brief="激活中的能力摘要")
        awake = _make_plugin("probe_awake")
        monkeypatch.setattr(
            config,
            "PLUGIN_CALL_PRIORITIES",
            {"probe_sleeping": "high", "probe_active": "low", "probe_awake": "medium"},
        )

        async def _state(_chat_key):  # noqa: ANN001, ANN202
            return pa.PluginActivationState(module_rounds={"probe_active": 2})

        async def _noop_save(_chat_key, _state):  # noqa: ANN001, ANN202
            return None

        async def _methods(_self, _ctx):  # noqa: ANN001, ANN202
            return "* do_something\n说明"

        monkeypatch.setattr(pa, "get_activation_state", _state)
        monkeypatch.setattr(pa, "save_activation_state", _noop_save)
        monkeypatch.setattr(NekroPlugin, "render_sandbox_methods_prompt", _methods)

        units = await pa.build_prompt_disclosure_view([sleeping, active, awake], _fake_ctx())
        by_module = {unit.module_name: unit for unit in units}

        assert {name: unit.state for name, unit in by_module.items()} == {
            "probe_sleeping": "sleeping",
            "probe_active": "active",
            "probe_awake": "always_awake",
        }
        assert {name: unit.call_priority for name, unit in by_module.items()} == {
            "probe_sleeping": "high",
            "probe_active": "low",
            "probe_awake": "medium",
        }
        for unit in units:
            assert f'call_priority="{unit.call_priority}"' in await render_plugin_prompt_unit(unit)

    async def test_legacy_rendering_carries_the_configured_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plugin = _make_plugin("probe_legacy")
        monkeypatch.setattr(config, "PLUGIN_CALL_PRIORITIES", {"probe_legacy": "low"})

        rendered = await render_plugins_prompt_legacy([plugin], _fake_ctx())  # pyright: ignore[reportArgumentType]

        assert 'module_name="probe_legacy" call_priority="low"' in rendered.system_prompt

    async def test_activated_block_carries_the_configured_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`activate_plugin` 返回给模型的整块走的是另一条渲染路径"""
        plugin = _make_plugin("probe_activated", allow_sleep=True, sleep_brief="摘要")
        monkeypatch.setattr(config, "PLUGIN_CALL_PRIORITIES", {"probe_activated": "medium"})

        block = await pa.render_plugin_prompt_for_agent(plugin, _fake_ctx(), rounds_left=2)  # pyright: ignore[reportArgumentType]

        assert 'call_priority="medium"' in block


class TestPriorityNeverChangesVisibility:
    """档位只挂在本轮实际渲染出来的块上，不决定谁被渲染

    被当前实例 / 会话关掉的插件、自动隔离期内不交出方法的插件，提示词里没有它的块，
    档位也就不出现；配置里那一项原样保留，回到可见状态后档位照旧生效。
    """

    @pytest.fixture
    def scoped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # 作用域覆盖是按层级聚合的单文件，必须整体改道到临时目录，否则会写进开发机的真实数据目录
        data_dir = tmp_path / "plugin_data"
        monkeypatch.setattr(scope_mod, "PLUGIN_DATA_DIR", data_dir)
        monkeypatch.setattr(scope_mod, "SCOPE_OVERRIDE_DIR", data_dir / "_overrides")
        scope_mod._scope_override_cache.clear()  # noqa: SLF001
        scope_mod._scoped_config_cache.clear()  # noqa: SLF001

        async def _state(_chat_key):  # noqa: ANN001, ANN202
            return pa.PluginActivationState(module_rounds={})

        async def _noop_save(_chat_key, _state):  # noqa: ANN001, ANN202
            return None

        monkeypatch.setattr(pa, "get_activation_state", _state)
        monkeypatch.setattr(pa, "save_activation_state", _noop_save)

        plugins = {}
        for module_name in ("prio_visible", "prio_closed", "prio_quarantined"):
            plugin = _make_plugin(module_name)

            @plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="探针方法", description="探针")
            async def probe_method(_ctx) -> str:  # noqa: ANN001
                """探针方法"""
                return "ok"

            plugins[module_name] = plugin

        quarantined = plugins["prio_quarantined"]

        # 写了 collect_methods 的插件在隔离期内拿不到方法，常驻块整块不渲染
        @quarantined.mount_collect_methods()
        async def _collect(_ctx):  # noqa: ANN001, ANN202
            return list(quarantined.sandbox_methods)

        monkeypatch.setattr(config, "PLUGIN_ENABLED", [plugin.key for plugin in plugins.values()])
        monkeypatch.setattr(
            config,
            "PLUGIN_CALL_PRIORITIES",
            {"prio_visible": "high", "prio_closed": "high", "prio_quarantined": "high"},
        )
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

    async def _render_for(self, instance_key: str) -> str:
        active = await scope_mod.resolve_active_plugins(instance_key=instance_key)
        return (await render_plugins_prompt(active, _fake_ctx())).system_prompt  # pyright: ignore[reportArgumentType]

    async def test_scope_closed_plugin_is_not_rendered_but_keeps_its_priority(self, scoped) -> None:
        scope_mod.set_scope_override(scope_mod.OverrideScope.INSTANCE, "inst1", scoped["prio_closed"].key, False)

        closed_view = await self._render_for("inst1")
        assert 'module_name="prio_visible" call_priority="high"' in closed_view
        assert "prio_closed" not in closed_view
        assert config.PLUGIN_CALL_PRIORITIES["prio_closed"] == "high"

        # 别的实例没关它：同一份全局档位照常生效
        assert 'module_name="prio_closed" call_priority="high"' in await self._render_for("inst2")

    async def test_quarantined_plugin_is_not_rendered_but_keeps_its_priority(self, scoped) -> None:
        quarantined = scoped["prio_quarantined"]
        assert 'module_name="prio_quarantined" call_priority="high"' in await self._render_for("inst1")

        now = time.time()
        isolation._quarantines[quarantined.key] = isolation.PluginQuarantine(  # noqa: SLF001
            plugin_key=quarantined.key,
            stage="collect_methods",
            reason="error",
            detail="探针",
            failures=3,
            since=now,
            until=now + 600,
        )
        quarantined_view = await self._render_for("inst1")
        assert "prio_quarantined" not in quarantined_view
        assert 'module_name="prio_visible" call_priority="high"' in quarantined_view
        assert config.PLUGIN_CALL_PRIORITIES["prio_quarantined"] == "high"

        isolation.release_quarantine(quarantined.key, reason="测试解除")
        assert 'module_name="prio_quarantined" call_priority="high"' in await self._render_for("inst1")


class TestRuntimeContractRules:
    def test_rules_define_fallback_and_auto_semantics(self) -> None:
        rules = build_plugin_call_priority_rules()

        assert "high, then medium, then low" in rules
        assert "try the next applicable lower-priority plugin" in rules
        assert "activate it before selecting a lower-priority equivalent plugin" in rules
        assert "call_priority=auto" in rules

    def _system_message(self, rules: str) -> str:
        compiler = PromptCompiler(
            platform_name="QQ",
            bot_platform_id="10001",
            chat_preset="你是一只猫。",
            plugins_prompt="",
            plugins_runtime_prompt="",
            plugin_activation_rules="",
            plugin_call_priority_rules=rules,
            enable_cot=False,
            chat_key_rules="- rule",
            enable_at=False,
        )
        return str(compiler.render_system_message().to_dict()["content"])

    def test_rules_land_in_the_system_message(self) -> None:
        content = self._system_message(build_plugin_call_priority_rules())
        assert "#### Plugin Call Priority Rules:" in content
        assert "high, then medium, then low" in content

    def test_empty_rules_render_no_section(self) -> None:
        assert "Plugin Call Priority Rules" not in self._system_message("")

    def test_run_agent_passes_the_rules_to_the_compiler(self) -> None:
        """调用点本身：`run_agent` 组装 `PromptCompiler` 时必须把规则段传进去

        按 AST 判，不按源码字符串判：函数名同时出现在 import 行里，字符串判据恒真。
        """
        tree = ast.parse(RUN_AGENT_PATH.read_text(encoding="utf-8"))
        compiler_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "PromptCompiler"
        ]
        assert len(compiler_calls) == 1
        passed = {keyword.arg: keyword.value for keyword in compiler_calls[0].keywords}
        value = passed.get("plugin_call_priority_rules")
        assert isinstance(value, ast.Call)
        assert isinstance(value.func, ast.Name)
        assert value.func.id == "build_plugin_call_priority_rules"


class TestPluginMeta:
    async def test_loaded_plugin_reports_configured_priority(self, probe: NekroPlugin, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "PLUGIN_CALL_PRIORITIES", {probe.module_name: "high"})

        entry = next(item for item in await get_all_ext_meta_data() if item["id"] == probe.key)
        detail = await get_plugin_detail(probe.key)

        assert entry["callPriority"] == {"configured": "high"}
        assert detail is not None
        assert detail["callPriority"] == {"configured": "high"}

    async def test_failed_plugin_has_no_priority(self) -> None:
        info = FailedPluginInfo(
            module_name="broken_priority_probe",
            file_path="broken_priority_probe.py",
            error_message="SyntaxError: invalid syntax",
            error_type="SyntaxError",
            is_builtin=True,
            stack_trace="",
        )
        plugin_collector.failed_plugins["builtin_plugins.broken_priority_probe"] = info
        try:
            entry = next(item for item in await get_all_ext_meta_data() if item["id"] == "broken_priority_probe")
            detail = await get_plugin_detail("broken_priority_probe")
        finally:
            plugin_collector.failed_plugins.pop("builtin_plugins.broken_priority_probe", None)

        assert entry["callPriority"] is None
        assert detail is not None
        assert detail["callPriority"] is None


class TestUpdateEndpoint:
    async def test_platform_owner_can_change_the_priority(
        self,
        probe: NekroPlugin,
        isolated_priorities: List[Tuple[object, ...]],
    ) -> None:
        response = await update_plugin_call_priority_handler(
            plugin_id=probe.key,
            body=PluginCallPriorityRequest(priority="high"),
            _current_user=platform_owner(),
        )

        assert response.ok is True
        assert config.PLUGIN_CALL_PRIORITIES == {probe.module_name: "high"}
        assert len(isolated_priorities) == 1

    async def test_tenant_admin_sees_the_priority_but_cannot_change_it(
        self,
        probe: NekroPlugin,
        monkeypatch: pytest.MonkeyPatch,
        isolated_priorities: List[Tuple[object, ...]],
    ) -> None:
        monkeypatch.setattr(config, "PLUGIN_CALL_PRIORITIES", {probe.module_name: "low"})

        listed = next(item for item in await get_plugins(_current_user=tenant_admin()) if item["id"] == probe.key)
        detail = await get_plugin_detail_handler(plugin_id=probe.key, _current_user=tenant_admin())
        assert listed["callPriority"] == {"configured": "low"}
        assert detail["callPriority"] == {"configured": "low"}

        with pytest.raises(HTTPException) as exc_info:
            await update_plugin_call_priority_handler(
                plugin_id=probe.key,
                body=PluginCallPriorityRequest(priority="high"),
                _current_user=tenant_admin(),
            )

        assert exc_info.value.status_code == 403
        assert config.PLUGIN_CALL_PRIORITIES == {probe.module_name: "low"}
        assert isolated_priorities == []

    def test_unknown_priority_is_rejected_by_the_request_model(self) -> None:
        with pytest.raises(PydanticValidationError):
            PluginCallPriorityRequest(priority="urgent")  # pyright: ignore[reportArgumentType]

    async def test_unknown_plugin_is_a_validation_error(self, isolated_priorities: List[Tuple[object, ...]]) -> None:
        with pytest.raises(ValidationError):
            await update_plugin_call_priority_handler(
                plugin_id="Nobody.no_such_plugin",
                body=PluginCallPriorityRequest(priority="high"),
                _current_user=platform_owner(),
            )
        assert config.PLUGIN_CALL_PRIORITIES == {}
        assert isolated_priorities == []

    async def test_persistence_failure_surfaces_and_leaves_config_untouched(
        self,
        probe: NekroPlugin,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ConfigService, "save_config", lambda *_args: (False, "disk full"))

        with pytest.raises(ValidationError) as exc_info:
            await update_plugin_call_priority_handler(
                plugin_id=probe.key,
                body=PluginCallPriorityRequest(priority="high"),
                _current_user=platform_owner(),
            )

        assert exc_info.value.params["reason"] == "disk full"
        assert config.PLUGIN_CALL_PRIORITIES == {}
