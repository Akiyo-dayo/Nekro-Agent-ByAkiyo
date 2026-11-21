"""频道聊天记忆开关：租户只能关、不能开，以及开关的判定入口。

`MEMORY_ENABLE_FOR_CHAT` 是覆盖层字段（适配器 / 实例 / 频道都能写），默认关闭。
租户管理员打开它，等于把「不沉淀、不注入」改成「沉淀、注入」，这是一次能力升级；
平台方不允许租户自助升级，于是写入侧必须比较**写入前后的生效值**，而不是只看提交值。

覆盖开关关着时提交值根本不参与解析。所以「打开覆盖并提交 True」与「关掉覆盖、而上层
此刻是 True」两种写法都能把生效值从 False 变成 True，必须一起挡。上层此刻是 True 时新写
一条「打开且为 True」的覆盖也挡：它把这一层钉在 True，平台方之后关掉上层也关不掉。上层的
真实取值向 `config_resolver.inherited_values()` 要，权限判据不重写一遍四层解析。

写路径逐条覆盖：`/set`、`/batch`、`PUT /config/instance/...`；不带取值的 `/save`（内存写盘）
与 `/reload`（磁盘读回内存）按搬运前后这一层的生效值比较。

本文件同时钉住 `is_memory_enabled_for_channel` 的两个分支：闸关时保持旧规则（绑了工作区
才有记忆），闸开时只看频道自己的开关。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

import plugins.builtin.memory_tools.main as memory_tools_module
from nekro_agent.core.config import CoreConfig
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.schemas.errors import PermissionDeniedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import config_resolver as config_resolver_module
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.config_service import UnifiedConfigService
from nekro_agent.services.memory import feature_flags
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.user.role import Role

INSTANCE_KEY = "instance_override_onebot_v11__writer"
CHANNEL_KEY = "channel_config_onebot_v11-writer-group_1"


def tenant_admin() -> DBUser:
    user = DBUser(
        id=21,
        username="tenant-admin",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        tenant_id=1,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )
    return user


def platform_owner() -> DBUser:
    user = DBUser(
        id=1,
        username="platform",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )
    return user


class ConfigHarness:
    """给真实路由装一份内存里的覆盖层配置

    路由的价值在于它调了 `_assert_tenant_may_write`；配置服务是不是真的落盘与这条判据无关，
    所以这里只保留 `get_config_item` / `set_config_value` 的读写语义，把磁盘换掉。
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, layer: OverridableConfig, inherited: Optional[bool]) -> None:
        self.layer = layer
        self.inherited = inherited
        self.saved: list[str] = []

        monkeypatch.setattr(
            UnifiedConfigService,
            "_get_config_instance",
            staticmethod(lambda config_key: layer if config_key in (INSTANCE_KEY, CHANNEL_KEY) else None),
        )
        monkeypatch.setattr(UnifiedConfigService, "save_config", staticmethod(self._save))

        async def fake_accessible(_user: DBUser, _config_key: str) -> None:
            return None

        async def fake_inherited(_config_key: str) -> Dict[str, Any]:
            return {"MEMORY_ENABLE_FOR_CHAT": self.inherited}

        monkeypatch.setattr(config_router, "_assert_config_key_accessible", fake_accessible)
        monkeypatch.setattr(config_resolver_module.config_resolver, "inherited_values", fake_inherited)

    def _save(self, config_key: str, _file_path: Any = None) -> tuple[bool, None]:
        self.saved.append(config_key)
        return True, None


@pytest.fixture
def layer() -> OverridableConfig:
    return OverridableConfig()


class TestOnlyLower:
    async def test_turning_on_via_set_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, inherited=False)
        layer.enable_MEMORY_ENABLE_FOR_CHAT = True
        layer.MEMORY_ENABLE_FOR_CHAT = False

        with pytest.raises(PermissionDeniedError):
            await config_router.set_config_value(
                INSTANCE_KEY,
                "MEMORY_ENABLE_FOR_CHAT",
                "true",
                _current_user=tenant_admin(),
            )

    async def test_turning_on_via_batch_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, inherited=False)

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                CHANNEL_KEY,
                config_router.BatchUpdateConfig(
                    configs={"enable_MEMORY_ENABLE_FOR_CHAT": "true", "MEMORY_ENABLE_FOR_CHAT": "true"},
                ),
                _current_user=tenant_admin(),
            )

    async def test_disabling_override_above_true_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 关掉覆盖层，上层此刻是 True —— 生效值从 False 跳到 True，同样是「打开」。
        ConfigHarness(monkeypatch, layer, inherited=True)
        layer.enable_MEMORY_ENABLE_FOR_CHAT = True
        layer.MEMORY_ENABLE_FOR_CHAT = False

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                CHANNEL_KEY,
                config_router.BatchUpdateConfig(configs={"enable_MEMORY_ENABLE_FOR_CHAT": "false"}),
                _current_user=tenant_admin(),
            )

    async def test_turning_off_is_allowed(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, inherited=True)
        layer.enable_MEMORY_ENABLE_FOR_CHAT = True
        layer.MEMORY_ENABLE_FOR_CHAT = True

        await config_router.batch_update_config(
            CHANNEL_KEY,
            config_router.BatchUpdateConfig(configs={"MEMORY_ENABLE_FOR_CHAT": "false"}),
            _current_user=tenant_admin(),
        )

        assert layer.MEMORY_ENABLE_FOR_CHAT is False
        assert layer.enable_MEMORY_ENABLE_FOR_CHAT is True

    async def test_unchanged_page_is_allowed(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, inherited=False)

        await config_router.batch_update_config(
            INSTANCE_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_MEMORY_ENABLE_FOR_CHAT": "false", "MEMORY_ENABLE_FOR_CHAT": "false"},
            ),
            _current_user=tenant_admin(),
        )

    async def test_platform_owner_may_turn_on(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ConfigHarness(monkeypatch, layer, inherited=False)

        await config_router.batch_update_config(
            INSTANCE_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_MEMORY_ENABLE_FOR_CHAT": "true", "MEMORY_ENABLE_FOR_CHAT": "true"},
            ),
            _current_user=platform_owner(),
        )

        assert layer.enable_MEMORY_ENABLE_FOR_CHAT is True
        assert layer.MEMORY_ENABLE_FOR_CHAT is True


class TestOtherWritePaths:
    """实例配置页走 `PUT /config/instance/...`（写入即落盘），同一条判据；另有「钉住打开」一条。"""

    @staticmethod
    def allow_instance(monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_instance_accessible(_user: DBUser, _instance_key: str, *, adapter_key: str) -> None:
            return None

        monkeypatch.setattr(config_router, "assert_instance_accessible", fake_instance_accessible)

    async def test_turning_on_via_instance_put_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        harness = ConfigHarness(monkeypatch, layer, inherited=False)
        self.allow_instance(monkeypatch)

        with pytest.raises(PermissionDeniedError):
            await config_router.update_instance_config(
                "onebot_v11",
                "writer",
                config_router.BatchUpdateConfig(
                    configs={"enable_MEMORY_ENABLE_FOR_CHAT": "true", "MEMORY_ENABLE_FOR_CHAT": "true"},
                ),
                _current_user=tenant_admin(),
            )

        assert layer.enable_MEMORY_ENABLE_FOR_CHAT is False
        assert harness.saved == []

    async def test_turning_off_via_instance_put_is_allowed(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        harness = ConfigHarness(monkeypatch, layer, inherited=True)
        self.allow_instance(monkeypatch)

        await config_router.update_instance_config(
            "onebot_v11",
            "writer",
            config_router.BatchUpdateConfig(
                configs={"enable_MEMORY_ENABLE_FOR_CHAT": "true", "MEMORY_ENABLE_FOR_CHAT": "false"},
            ),
            _current_user=tenant_admin(),
        )

        assert layer.enable_MEMORY_ENABLE_FOR_CHAT is True
        assert layer.MEMORY_ENABLE_FOR_CHAT is False
        assert harness.saved == [INSTANCE_KEY]

    async def test_pinning_an_on_override_while_parent_is_on_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 上层此刻是开的，写一条「打开且为 True」的覆盖不改变现在的生效值；但平台方之后把上层
        # 关掉，这一层仍然开着，等于租户自己开了。
        ConfigHarness(monkeypatch, layer, inherited=True)

        with pytest.raises(PermissionDeniedError):
            await config_router.batch_update_config(
                CHANNEL_KEY,
                config_router.BatchUpdateConfig(
                    configs={"enable_MEMORY_ENABLE_FOR_CHAT": "true", "MEMORY_ENABLE_FOR_CHAT": "true"},
                ),
                _current_user=tenant_admin(),
            )

        assert layer.enable_MEMORY_ENABLE_FOR_CHAT is False

    async def test_page_save_of_an_existing_on_override_is_allowed(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 平台方写下的「打开」覆盖，租户整页保存原样带回：不是新写，放行。
        ConfigHarness(monkeypatch, layer, inherited=False)
        layer.enable_MEMORY_ENABLE_FOR_CHAT = True
        layer.MEMORY_ENABLE_FOR_CHAT = True

        await config_router.batch_update_config(
            CHANNEL_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_MEMORY_ENABLE_FOR_CHAT": "true", "MEMORY_ENABLE_FOR_CHAT": "true"},
            ),
            _current_user=tenant_admin(),
        )

        assert layer.MEMORY_ENABLE_FOR_CHAT is True


class TestSaveAndReload:
    """`/save`、`/reload` 不带取值，在内存与磁盘之间整层搬运；按搬运前后这一层的生效值判。"""

    @staticmethod
    def write_disk_copy(path: Path, *, toggle: bool, value: bool) -> None:
        disk = OverridableConfig()
        disk.set_instance_config_file_path(path)
        disk.enable_MEMORY_ENABLE_FOR_CHAT = toggle
        disk.MEMORY_ENABLE_FOR_CHAT = value
        disk.dump_config()

    async def test_save_of_memory_matching_disk_is_allowed(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 正常流程：先 `/batch`（已判）再 `/save`，内存与磁盘一致或更低，放行，值不动。
        harness = ConfigHarness(monkeypatch, layer, inherited=False)
        path = tmp_path / "channel.yaml"
        self.write_disk_copy(path, toggle=True, value=True)
        layer.set_instance_config_file_path(path)
        layer.enable_MEMORY_ENABLE_FOR_CHAT = True
        layer.MEMORY_ENABLE_FOR_CHAT = False

        await config_router.save_config_api(CHANNEL_KEY, _current_user=tenant_admin())

        assert layer.enable_MEMORY_ENABLE_FOR_CHAT is True
        assert layer.MEMORY_ENABLE_FOR_CHAT is False
        assert harness.saved == [CHANNEL_KEY]

    async def test_save_that_would_raise_after_restart_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 内存里有一条平台方没落盘的「打开」，磁盘上没有：租户落盘会让重启后的生效值变高。
        harness = ConfigHarness(monkeypatch, layer, inherited=False)
        layer.set_instance_config_file_path(tmp_path / "missing.yaml")
        layer.enable_MEMORY_ENABLE_FOR_CHAT = True
        layer.MEMORY_ENABLE_FOR_CHAT = True

        with pytest.raises(PermissionDeniedError):
            await config_router.save_config_api(CHANNEL_KEY, _current_user=tenant_admin())

        assert harness.saved == []

    async def test_platform_owner_save_is_not_checked(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        harness = ConfigHarness(monkeypatch, layer, inherited=False)
        layer.set_instance_config_file_path(tmp_path / "missing.yaml")
        layer.enable_MEMORY_ENABLE_FOR_CHAT = True
        layer.MEMORY_ENABLE_FOR_CHAT = True

        await config_router.save_config_api(CHANNEL_KEY, _current_user=platform_owner())

        assert harness.saved == [CHANNEL_KEY]

    async def test_reload_that_would_raise_is_rejected(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # 平台方在内存里关掉了、还没落盘；租户把磁盘上那份「开」读回来就是自己打开。
        ConfigHarness(monkeypatch, layer, inherited=False)
        reloaded: list[str] = []
        monkeypatch.setattr(
            UnifiedConfigService,
            "reload_config",
            staticmethod(lambda config_key, _file_path=None: (reloaded.append(config_key), (True, None))[1]),
        )
        path = tmp_path / "channel.yaml"
        self.write_disk_copy(path, toggle=True, value=True)
        layer.set_instance_config_file_path(path)
        layer.enable_MEMORY_ENABLE_FOR_CHAT = True
        layer.MEMORY_ENABLE_FOR_CHAT = False

        with pytest.raises(PermissionDeniedError):
            await config_router.reload_config_api(CHANNEL_KEY, _current_user=tenant_admin())

        assert reloaded == []

    async def test_reload_that_lowers_is_allowed(
        self,
        layer: OverridableConfig,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        ConfigHarness(monkeypatch, layer, inherited=False)
        reloaded: list[str] = []
        monkeypatch.setattr(
            UnifiedConfigService,
            "reload_config",
            staticmethod(lambda config_key, _file_path=None: (reloaded.append(config_key), (True, None))[1]),
        )
        path = tmp_path / "channel.yaml"
        self.write_disk_copy(path, toggle=True, value=False)
        layer.set_instance_config_file_path(path)
        layer.enable_MEMORY_ENABLE_FOR_CHAT = True
        layer.MEMORY_ENABLE_FOR_CHAT = True

        await config_router.reload_config_api(CHANNEL_KEY, _current_user=tenant_admin())

        assert reloaded == [CHANNEL_KEY]


class TestMarkerIsWellFormed:
    def test_marker_only_sits_on_boolean_or_integer_fields(self) -> None:
        """标记只允许落在 bool 或 int 字段上：导入期按这两种类型各有一支判据"""
        marked = [
            name
            for name, field in CoreConfig.model_fields.items()
            if isinstance(field.json_schema_extra, dict) and field.json_schema_extra.get("tenant_may_only_lower")
        ]

        assert "MEMORY_ENABLE_FOR_CHAT" in marked
        for name in marked:
            assert CoreConfig.model_fields[name].annotation in (bool, int), name


class TestChannelSwitch:
    async def test_gate_off_keeps_workspace_rule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feature_flags, "is_memory_system_enabled", lambda: True)
        monkeypatch.setattr(feature_flags, "is_domain_scope_enabled", lambda: False)

        class _Channel:
            workspace_id: Optional[int] = None

            async def get_effective_config(self) -> SimpleNamespace:
                raise AssertionError("闸关时不应读取频道配置")

        assert await feature_flags.is_memory_enabled_for_channel(_Channel()) is False

        _Channel.workspace_id = 7
        assert await feature_flags.is_memory_enabled_for_channel(_Channel()) is True

    async def test_gate_on_reads_channel_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feature_flags, "is_memory_system_enabled", lambda: True)
        monkeypatch.setattr(feature_flags, "is_domain_scope_enabled", lambda: True)

        class _Channel:
            workspace_id = None

            def __init__(self, enabled: bool) -> None:
                self.enabled = enabled

            async def get_effective_config(self) -> SimpleNamespace:
                return SimpleNamespace(MEMORY_ENABLE_FOR_CHAT=self.enabled)

        assert await feature_flags.is_memory_enabled_for_channel(_Channel(True)) is True
        assert await feature_flags.is_memory_enabled_for_channel(_Channel(False)) is False

    async def test_gate_on_read_failure_is_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feature_flags, "is_memory_system_enabled", lambda: True)
        monkeypatch.setattr(feature_flags, "is_domain_scope_enabled", lambda: True)

        class _Channel:
            workspace_id = None

            async def get_effective_config(self) -> SimpleNamespace:
                raise RuntimeError("bad override file")

        assert await feature_flags.is_memory_enabled_for_channel(_Channel()) is False

    async def test_system_switch_off_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(feature_flags, "is_memory_system_enabled", lambda: False)
        monkeypatch.setattr(feature_flags, "is_domain_scope_enabled", lambda: True)

        class _Channel:
            workspace_id = 7

            async def get_effective_config(self) -> SimpleNamespace:
                return SimpleNamespace(MEMORY_ENABLE_FOR_CHAT=True)

        assert await feature_flags.is_memory_enabled_for_channel(_Channel()) is False


class _ChatMemoryChannel:
    workspace_id = None
    adapter_key = "onebot_v11"
    instance_key = "inst"
    chat_key = ""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    async def get_effective_config(self) -> SimpleNamespace:
        return SimpleNamespace(
            MEMORY_ENABLE_FOR_CHAT=self.enabled,
            MEMORY_CONTEXT_MAX_ITEMS=3,
            MEMORY_CONTEXT_MAX_LENGTH=120,
        )


class TestReadSideGateOn:
    @staticmethod
    async def _enabled(_channel: Any) -> bool:
        return True

    @staticmethod
    async def _disabled(_channel: Any) -> bool:
        return False

    async def test_injection_accepts_channel_without_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        planned: list[object] = []

        async def fake_scope(_channel: Any) -> MemoryScope:
            return MemoryScope.unrestricted()

        async def fake_plan(_messages: Any) -> SimpleNamespace:
            planned.append(object())
            return SimpleNamespace(queries=[])

        monkeypatch.setattr(history_module, "is_domain_scope_enabled", lambda: True)
        monkeypatch.setattr(history_module, "is_memory_enabled_for_channel", self._enabled)
        monkeypatch.setattr("nekro_agent.services.memory.instance_scope.resolve_memory_scope", fake_scope)
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)

        result = await history_module._inject_memory_context(  # noqa: SLF001
            None,
            [],
            _ChatMemoryChannel(True),
            "",
        )

        assert result == ""
        assert len(planned) == 1

    async def test_injection_skips_when_channel_switch_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        planned: list[object] = []

        async def fake_plan(_messages: Any) -> SimpleNamespace:
            planned.append(object())
            return SimpleNamespace(queries=[])

        monkeypatch.setattr(history_module, "is_domain_scope_enabled", lambda: True)
        monkeypatch.setattr(history_module, "is_memory_enabled_for_channel", self._disabled)
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)

        result = await history_module._inject_memory_context(  # noqa: SLF001
            None,
            [],
            _ChatMemoryChannel(False),
            "",
        )

        assert result == ""
        assert planned == []

    async def test_memory_tools_accept_channel_without_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_scope(_channel: Any) -> MemoryScope:
            return MemoryScope.unrestricted()

        class _Ctx:
            db_chat_channel = _ChatMemoryChannel(True)
            from_chat_key = ""

        monkeypatch.setattr(memory_tools_module, "is_domain_scope_enabled", lambda: True)
        monkeypatch.setattr(memory_tools_module, "is_memory_enabled_for_channel", self._enabled)
        monkeypatch.setattr(memory_tools_module, "resolve_memory_scope", fake_scope)

        partition, scope = await memory_tools_module._require_bound_memory_scope(_Ctx())  # noqa: SLF001

        assert partition == 0
        assert scope == MemoryScope.unrestricted()

    async def test_memory_tools_reject_channel_switch_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Ctx:
            db_chat_channel = _ChatMemoryChannel(False)
            from_chat_key = ""

        monkeypatch.setattr(memory_tools_module, "is_domain_scope_enabled", lambda: True)
        monkeypatch.setattr(memory_tools_module, "is_memory_enabled_for_channel", self._disabled)

        with pytest.raises(ValueError):
            await memory_tools_module._require_bound_memory_scope(_Ctx())  # noqa: SLF001
