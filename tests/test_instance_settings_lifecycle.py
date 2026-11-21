"""实例名下设置的生命周期补强：防同名继承的兜底、跨租户迁移、核心配置覆盖、链接与几处小漏口

## 这条测试钉的是什么

- **新建实例前兜底清扫**：四个建实例的入口（总后台新建、认领为新实例、协议端自动登记、适配器
  服务新建）在建行之前都清掉这个标识上残留的插件设置与配置覆盖；静态用例保证以后新加的建行点也走它。
- **彻底删除的窗口**：改库之前删一次、提交之后再兜一次——两次之间被写回的也删得掉。
- **平台方也要目标存在**：对已删除的实例 / 不存在的频道写插件设置一律 404。
- **跨租户的迁移、并入**：源实例的插件设置与配置覆盖默认删、给勾选框；同一归属的照旧跟着走。
- **核心配置的实例层 / 频道层覆盖**：与插件设置同一套口径（删除、划转、换标识、迁移、建前清扫）。
- **链接**：符号链接 / 目录联接只删链接本身；共用的上两级是链接时报错、整次操作不做。
- 复核的几处 P3：迁移部分失败时不误搬、划转遇到跨适配器同名拒绝、UID 白名单、先断开邮箱客户端再算附件、
  删 / 搬之后重同步插件的进程级加载状态。
- 复核后补：跨租户选了删就一路不搬（循环中途写回的也到不了目标方）、划转改归属后再删一遍、
  协议端自动登记没扫成的补扫标记、彻底删除提交后的那一遍不受前面步骤失败影响、前缀撞上别的实例的频道、
  平台方配置接口要求目标存在、新实例标识的形态与不分大小写的重名。

## 环境

内存 SQLite；插件设置、附件、表情文件、核心配置覆盖的落盘目录由全局固件改道到临时目录；
`DATA_DIR` 也改到临时目录（令牌曝光记录写在它下面）。插件键、密钥、地址都现编。
"""

from __future__ import annotations

import ast
import datetime
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml
from tortoise import Tortoise

from nekro_agent.core.core_utils import ConfigManager
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.routers.console import instances as instances_router
from nekro_agent.routers.console import tenants as tenants_router
from nekro_agent.routers.console.instances import (
    preview_console_instance_migration,
    run_console_instance_migration,
)
from nekro_agent.routers.plugins import assert_plugin_scope_accessible
from nekro_agent.schemas.console import (
    InstanceAdoptRequest,
    InstanceCreateRequest,
    InstanceMigrationRequest,
    InstanceProvider,
)
from nekro_agent.schemas.errors import ConflictError, NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantInstanceBindRequest, TenantRole
from nekro_agent.services import chat_key_migration, config_service, instance_settings
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.config_service import UnifiedConfigService
from nekro_agent.services.console import instance_manager
from nekro_agent.services.plugin import scope as scope_store
from nekro_agent.services.plugin.scope import OverrideScope
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OTHER = "inst2"
TARGET = "inst3"
SECRET_PLUGIN = "test_scope.secret_probe"
SWITCH_PLUGIN = "test_scope.switch_probe"
OLD_SECRET = "old-owner-key-" + "x4k"
CONTEXT_FIELD = "AI_CHAT_CONTEXT_MAX_LENGTH"
WEBHOOK_FIELD = "INSTANCE_ALERT_WEBHOOK_URL"
OLD_WEBHOOK = "https://hooks.example.test/old-owner"


def chat_key(instance_key: str, channel_id: str) -> str:
    return f"{ADAPTER}-{instance_key}-{channel_id}"


# ---------------------------------------------------------------------------
# 环境与夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "data"
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def clean_config_manager():
    """用例里加载过的覆盖对象从进程级缓存里摘掉，不留给下一条用例"""
    before = set(ConfigManager.get_all_config_keys())
    yield
    for key in set(ConfigManager.get_all_config_keys()) - before:
        if key.startswith((config_service.INSTANCE_OVERRIDE_KEY_PREFIX, config_service.CHANNEL_OVERRIDE_KEY_PREFIX)):
            ConfigManager.unregister_config(key)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def no_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(_instance_key: str) -> None:
        return None

    monkeypatch.setattr(instance_manager, "purge_instance_vectors", _noop)


@pytest.fixture
def offline_qdrant(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.memory import qdrant_manager as qdrant_module

    async def fake_set(paragraph_ids: List[int], _instance_key: str) -> int:
        return len(paragraph_ids)

    async def fake_payload(paragraph_ids: List[int], _payload: Dict[str, Any]) -> int:
        return len(paragraph_ids)

    monkeypatch.setattr(qdrant_module.memory_qdrant_manager, "set_paragraph_instance_key", fake_set)
    monkeypatch.setattr(qdrant_module.memory_qdrant_manager, "set_paragraph_payload", fake_payload)


@pytest.fixture
def resynced(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """记录每次「删 / 搬之后重同步插件进程级加载状态」涉及的插件"""
    from nekro_agent.services.plugin import manager

    calls: List[List[str]] = []

    async def record(plugin_keys: Any) -> Dict[str, str]:
        calls.append(sorted(plugin_keys))
        return {}

    monkeypatch.setattr(manager, "sync_plugin_load_states", record)
    return calls


def platform_owner() -> DBUser:
    return DBUser(
        username="owner",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


async def make_tenant(name: str) -> DBTenant:
    return await DBTenant.create(name=name, display_name=name)


async def make_instance(
    instance_key: str,
    tenant_id: Optional[int] = None,
    *,
    adapter_key: str = ADAPTER,
    status: str = "online",
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=instance_key,
        status=status,
        provider="remote_onebot",
        tenant_id=tenant_id,
        metadata_json="",
    )


async def make_channel(instance_key: str, channel_id: str) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=channel_id,
        chat_key=chat_key(instance_key, channel_id),
        channel_type="group",
        data="{}",
    )


def seed_plugin_settings(instance_key: str, chat_keys: List[str]) -> None:
    scope_store.set_scope_override(OverrideScope.INSTANCE, instance_key, SECRET_PLUGIN, True)
    for key in chat_keys:
        scope_store.set_scope_override(OverrideScope.SESSION, key, SWITCH_PLUGIN, True)
    scope_store.save_instance_config_overrides(SECRET_PLUGIN, instance_key, {"API_KEY": OLD_SECRET})


def write_yaml(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")


def read_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def instance_override_path(instance_key: str) -> Path:
    return config_service.instance_override_config_path(ADAPTER, instance_key)


def channel_override_path(key: str) -> Path:
    return config_service.channel_override_config_path(key)


def seed_config_overrides(instance_key: str, chat_keys: List[str]) -> None:
    write_yaml(instance_override_path(instance_key), {f"enable_{WEBHOOK_FIELD}": True, WEBHOOK_FIELD: OLD_WEBHOOK})
    for key in chat_keys:
        write_yaml(channel_override_path(key), {f"enable_{CONTEXT_FIELD}": True, CONTEXT_FIELD: 7})


def plugin_settings_gone(instance_key: str, chat_keys: List[str]) -> None:
    assert scope_store.get_scope_overrides(OverrideScope.INSTANCE, instance_key) == {}
    for key in chat_keys:
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, key) == {}
    assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, instance_key) == {}


def config_overrides_gone(instance_key: str, chat_keys: List[str]) -> None:
    assert not instance_override_path(instance_key).exists()
    for key in chat_keys:
        assert not channel_override_path(key).exists()


_SWEEPERS = {"sweep_before_create", "_sweep_settings_before_create", "_sweep_settings_before_register"}
_ROW_BIRTHS = {"create", "bulk_create", "get_or_create", "update_or_create"}


def _chain_root(node: ast.expr) -> str:
    """`A.filter(...).update` 这种调用链最左边的名字"""
    while True:
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        else:
            return node.id if isinstance(node, ast.Name) else ""


def _instance_key_birth_sites(source: str) -> List[Tuple[str, int, bool]]:
    """源码里「让一个实例标识出现在实例表里」的调用点：`[(函数名, 行号, 之前有没有兜底清扫)]`

    四种建行写法（`create` / `bulk_create` / `get_or_create` / `update_or_create`），加上把已有行的
    `instance_key` 改掉的 `.update(instance_key=...)`。
    """
    sites: List[Tuple[str, int, bool]] = []
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        births: List[int] = []
        sweeps: List[int] = []
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Attribute) and _chain_root(target.value) == "DBAdapterInstance":
                renames = target.attr == "update" and any(kw.arg == "instance_key" for kw in node.keywords)
                if (target.attr in _ROW_BIRTHS and isinstance(target.value, ast.Name)) or renames:
                    births.append(node.lineno)
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name in _SWEEPERS:
                sweeps.append(node.lineno)
        sites.extend((func.name, line, any(sweep < line for sweep in sweeps)) for line in sorted(births))
    return sites


def make_dir_link(link: Path, target: Path) -> None:
    """建一个目录链接：Windows 上用目录联接（不需要特权），别处用符号链接"""
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


# ---------------------------------------------------------------------------
# 新建实例前的兜底清扫
# ---------------------------------------------------------------------------


class TestSweepBeforeCreate:
    async def test_console_create_clears_leftovers_of_the_key(self, db) -> None:  # noqa: ARG002
        leftover = chat_key(INSTANCE, "group_1")
        seed_plugin_settings(INSTANCE, [leftover])
        seed_config_overrides(INSTANCE, [leftover])

        await instance_manager.create_instance(
            InstanceCreateRequest(adapter_key=ADAPTER, instance_key=INSTANCE, provider=InstanceProvider.REMOTE_ONEBOT),
        )

        plugin_settings_gone(INSTANCE, [leftover])
        config_overrides_gone(INSTANCE, [leftover])

    async def test_the_adapter_service_create_clears_leftovers(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.adapter_instance_service import adapter_instance_service

        leftover = chat_key(INSTANCE, "group_1")
        seed_plugin_settings(INSTANCE, [leftover])
        seed_config_overrides(INSTANCE, [leftover])

        await adapter_instance_service.create_instance(ADAPTER, INSTANCE, INSTANCE, "remote_onebot")

        plugin_settings_gone(INSTANCE, [leftover])
        config_overrides_gone(INSTANCE, [leftover])

    async def test_onebot_auto_registration_clears_leftovers(self, db) -> None:  # noqa: ARG002
        from nekro_agent.adapters.onebot_v11.core import instance as onebot_instance

        key = onebot_instance._generate_instance_key("123456")  # noqa: SLF001
        leftover = chat_key(key, "group_1")
        seed_plugin_settings(key, [leftover])

        created = await onebot_instance._register_unclaimed_instance("123456")  # noqa: SLF001

        assert created.instance_key == key
        plugin_settings_gone(key, [leftover])

    async def test_auto_registration_still_registers_when_the_sweep_fails(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """入站链路的第一步：清扫失败只记日志，协议端照样登记（不能因为一份残留文件把消息挡掉）"""
        from nekro_agent.adapters.onebot_v11.core import instance as onebot_instance

        async def broken(_adapter_key: str, _instance_key: str) -> Dict[str, int]:
            raise OSError("busy")

        monkeypatch.setattr(instance_settings, "sweep_before_create", broken)

        created = await onebot_instance._register_unclaimed_instance("123456")  # noqa: SLF001

        assert await DBAdapterInstance.filter(id=created.id).exists()

    async def test_console_create_refuses_when_the_sweep_fails(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def broken(_adapter_key: str, _instance_key: str) -> Dict[str, int]:
            raise OSError("busy")

        monkeypatch.setattr(instance_settings, "sweep_before_create", broken)

        with pytest.raises(ValidationError):
            await instance_manager.create_instance(
                InstanceCreateRequest(adapter_key=ADAPTER, instance_key=INSTANCE, provider=InstanceProvider.REMOTE_ONEBOT),
            )
        assert not await DBAdapterInstance.filter(instance_key=INSTANCE).exists()

    async def test_claiming_as_a_new_instance_clears_leftovers_of_the_new_key(
        self,
        db,  # noqa: ARG002
        offline_qdrant,  # noqa: ARG002
    ) -> None:
        source = await make_instance("ob123456", status="unclaimed")
        leftover = chat_key(TARGET, "group_1")
        seed_plugin_settings(TARGET, [leftover])
        seed_config_overrides(TARGET, [leftover])

        await instance_manager.claim_unclaimed_as_new_instance(source, "", TARGET)

        plugin_settings_gone(TARGET, [leftover])
        config_overrides_gone(TARGET, [leftover])

    async def test_instance_level_plugin_settings_of_a_live_namesake_are_kept(self, db) -> None:  # noqa: ARG002
        """同一个实例标识在别的适配器下还有活着的行：插件的实例级设置是那一行的，不动"""
        await make_instance(INSTANCE, adapter_key="other_adapter")
        seed_plugin_settings(INSTANCE, [])
        own_leftover = chat_key(INSTANCE, "group_1")
        scope_store.set_scope_override(OverrideScope.SESSION, own_leftover, SWITCH_PLUGIN, True)

        await instance_settings.sweep_before_create(ADAPTER, INSTANCE)

        assert scope_store.get_scope_overrides(OverrideScope.INSTANCE, INSTANCE) == {SECRET_PLUGIN: True}
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, INSTANCE) == {"API_KEY": OLD_SECRET}
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, own_leftover) == {}

    def test_every_instance_creation_sweeps_first(self) -> None:
        """静态守卫：建实例行（四种写法）或改实例行标识的函数里，那一步之前必须先调兜底清扫"""
        root = Path(__file__).resolve().parents[1] / "nekro_agent"
        offenders: List[str] = []
        sites = 0
        for path in sorted(root.rglob("*.py")):
            for func_name, line, swept in _instance_key_birth_sites(path.read_text(encoding="utf-8")):
                sites += 1
                if not swept:
                    offenders.append(f"{path.relative_to(root)}:{line} {func_name}")
        assert sites >= 4
        assert offenders == []

    @pytest.mark.parametrize(
        "statement",
        [
            "await DBAdapterInstance.create(instance_key=key)",
            "await DBAdapterInstance.bulk_create(rows)",
            "await DBAdapterInstance.get_or_create(adapter_key=a, instance_key=key)",
            "await DBAdapterInstance.update_or_create(adapter_key=a, instance_key=key)",
            "await DBAdapterInstance.filter(id=1).update(instance_key=key)",
        ],
    )
    def test_the_guard_sees_every_way_to_give_a_row_a_key(self, statement: str) -> None:
        unswept = f"async def f(key, a, rows):\n    {statement}\n"
        swept = f"async def f(key, a, rows):\n    await sweep_before_create(a, key)\n    {statement}\n"
        assert _instance_key_birth_sites(unswept) == [("f", 2, False)]
        assert _instance_key_birth_sites(swept) == [("f", 3, True)]

    def test_the_guard_ignores_other_models_and_other_columns(self) -> None:
        source = (
            "async def f(key):\n"
            "    await DBWbBook.filter(id=1).update(instance_key=key)\n"
            "    await DBAdapterInstance.filter(id=1).update(display_name=key)\n"
        )
        assert _instance_key_birth_sites(source) == []


# ---------------------------------------------------------------------------
# 彻底删除：改库前删一次、提交后再兜一次
# ---------------------------------------------------------------------------


class TestPurge:
    async def test_settings_written_back_inside_the_window_are_gone_too(
        self,
        db,  # noqa: ARG002
        no_vectors,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance = await make_instance(INSTANCE)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        seed_plugin_settings(INSTANCE, [own])
        original = instance_manager._disconnect_instance_mailboxes  # noqa: SLF001

        async def write_back_then_disconnect(*args: Any, **kwargs: Any) -> None:
            # 改库之前那次删除之后、事务之前：后台页面正开着，有人又写了一遍
            seed_plugin_settings(INSTANCE, [own])
            seed_config_overrides(INSTANCE, [own])
            await original(*args, **kwargs)

        monkeypatch.setattr(instance_manager, "_disconnect_instance_mailboxes", write_back_then_disconnect)

        await instance_manager.purge_instance(instance)

        plugin_settings_gone(INSTANCE, [own])
        config_overrides_gone(INSTANCE, [own])

    async def test_core_config_overrides_go_and_the_cache_forgets_them(
        self,
        db,  # noqa: ARG002
        no_vectors,  # noqa: ARG002
    ) -> None:
        instance = await make_instance(INSTANCE)
        await make_instance(OTHER)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        orphan = chat_key(INSTANCE, "group_gone")
        neighbour = (await make_channel(OTHER, "group_2")).chat_key
        seed_config_overrides(INSTANCE, [own, orphan])
        seed_config_overrides(OTHER, [neighbour])
        own_key = f"{config_service.CHANNEL_OVERRIDE_KEY_PREFIX}{own}"
        assert getattr(UnifiedConfigService._get_config_instance(own_key), CONTEXT_FIELD) == 7  # noqa: SLF001

        counts = await instance_manager.purge_instance(instance)

        assert counts["config_overrides"] == 3
        config_overrides_gone(INSTANCE, [own, orphan])
        assert channel_override_path(neighbour).exists()
        assert instance_override_path(OTHER).exists()
        assert ConfigManager.get_config(own_key) is None

    async def test_a_reclaimed_name_reads_no_core_overrides(self, db, no_vectors) -> None:  # noqa: ARG002
        instance = await make_instance(INSTANCE)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        seed_config_overrides(INSTANCE, [own])
        instance_key = config_service.instance_override_config_key(ADAPTER, INSTANCE)
        assert getattr(UnifiedConfigService._get_config_instance(instance_key), WEBHOOK_FIELD) == OLD_WEBHOOK  # noqa: SLF001

        await instance_manager.purge_instance(instance)
        await instance_manager.create_instance(
            InstanceCreateRequest(adapter_key=ADAPTER, instance_key=INSTANCE, provider=InstanceProvider.REMOTE_ONEBOT),
        )

        fresh = UnifiedConfigService._get_config_instance(instance_key)  # noqa: SLF001
        assert fresh is not None
        assert getattr(fresh, f"enable_{WEBHOOK_FIELD}") is False
        assert getattr(fresh, WEBHOOK_FIELD) != OLD_WEBHOOK

    async def test_the_preview_counts_core_overrides_with_active_items_only(self, db) -> None:  # noqa: ARG002
        from nekro_agent.routers.console.instances import preview_console_instance_purge

        await make_instance(INSTANCE)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        second = (await make_channel(INSTANCE, "group_2")).chat_key
        seed_config_overrides(INSTANCE, [own])
        # 读侧会落一个空文件：它不算「有覆盖」，但删的时候照样删
        write_yaml(channel_override_path(second), {})

        preview = await preview_console_instance_purge(INSTANCE, _current_user=platform_owner())

        assert preview.config_overrides == 2

    async def test_plugin_load_states_are_resynced(
        self,
        db,  # noqa: ARG002
        no_vectors,  # noqa: ARG002
        resynced: List[List[str]],
    ) -> None:
        instance = await make_instance(INSTANCE)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        seed_plugin_settings(INSTANCE, [own])

        await instance_manager.purge_instance(instance)

        assert [SECRET_PLUGIN, SWITCH_PLUGIN] in resynced


# ---------------------------------------------------------------------------
# 平台方也要目标存在
# ---------------------------------------------------------------------------


class TestPlatformNeedsTheTarget:
    async def test_a_deleted_instance_is_404_for_the_platform_too(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await assert_plugin_scope_accessible(platform_owner(), INSTANCE)

    async def test_a_missing_channel_is_404_for_the_platform_too(self, db) -> None:  # noqa: ARG002
        await make_instance(INSTANCE)

        with pytest.raises(NotFoundError):
            await assert_plugin_scope_accessible(platform_owner(), INSTANCE, chat_key(INSTANCE, "group_404"))

    async def test_existing_targets_pass(self, db) -> None:  # noqa: ARG002
        await make_instance(INSTANCE)
        own = (await make_channel(INSTANCE, "group_1")).chat_key

        await assert_plugin_scope_accessible(platform_owner(), INSTANCE, own)


# ---------------------------------------------------------------------------
# 链接：只删链接本身；共用的上两级是链接时报错
# ---------------------------------------------------------------------------


class TestLinks:
    async def test_a_linked_instance_dir_loses_only_the_link(self, db, no_vectors, tmp_path: Path) -> None:  # noqa: ARG002
        instance = await make_instance(INSTANCE)
        elsewhere = tmp_path / "elsewhere" / "kept"
        write_yaml(elsewhere / "config.yaml", {"API_KEY": OLD_SECRET})
        make_dir_link(scope_store.PLUGIN_DATA_DIR / SECRET_PLUGIN / "instances" / INSTANCE, elsewhere)
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, INSTANCE) == {"API_KEY": OLD_SECRET}

        await instance_manager.purge_instance(instance)

        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, INSTANCE) == {}
        assert (elsewhere / "config.yaml").exists()

    async def test_a_linked_instances_dir_stops_the_purge(self, db, no_vectors, tmp_path: Path) -> None:  # noqa: ARG002
        instance = await make_instance(INSTANCE)
        shared = tmp_path / "elsewhere" / "instances"
        write_yaml(shared / INSTANCE / "config.yaml", {"API_KEY": OLD_SECRET})
        make_dir_link(scope_store.PLUGIN_DATA_DIR / SECRET_PLUGIN / "instances", shared)

        with pytest.raises(ValidationError):
            await instance_manager.purge_instance(instance)

        assert await DBAdapterInstance.filter(id=instance.id).exists()
        assert (shared / INSTANCE / "config.yaml").exists()

    async def test_a_linked_channel_override_dir_stops_the_purge(self, db, no_vectors, tmp_path: Path) -> None:  # noqa: ARG002
        instance = await make_instance(INSTANCE)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        shared = tmp_path / "elsewhere" / "channels"
        make_dir_link(config_service.CHANNEL_CONFIG_DIR / ADAPTER, shared)
        write_yaml(channel_override_path(own), {f"enable_{CONTEXT_FIELD}": True, CONTEXT_FIELD: 7})

        with pytest.raises(ValidationError):
            await instance_manager.purge_instance(instance)

        assert await DBAdapterInstance.filter(id=instance.id).exists()

    async def test_a_linked_attachment_dir_loses_only_the_link(self, db, tmp_path: Path) -> None:  # noqa: ARG002
        row = await DBEmailAccount.create(
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE,
            username="own@example.test",
            display_name="",
            provider="自定义",
            settings={},
            password="secret",
            attachment_dir="",
        )
        row.attachment_dir = mail.new_attachment_dir(row.id)
        await row.save(update_fields=["attachment_dir"])
        elsewhere = tmp_path / "elsewhere" / "files"
        (elsewhere / "1001").mkdir(parents=True)
        (elsewhere / "1001" / "a.pdf").write_bytes(b"pdf")
        make_dir_link(mail.email_attachment_root() / row.attachment_dir, elsewhere)

        mail.run_attachment_cleanup(await mail.plan_attachment_cleanup([row]))

        assert not (mail.email_attachment_root() / row.attachment_dir).exists()
        assert (elsewhere / "1001" / "a.pdf").exists()

    async def test_a_linked_mail_dir_in_an_address_dir_loses_only_the_link(self, db, tmp_path: Path) -> None:  # noqa: ARG002
        row = await DBEmailAccount.create(
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE,
            username="own@example.test",
            display_name="",
            provider="自定义",
            settings={},
            password="secret",
            attachment_dir="own@example.test",
        )
        await DBEmail.create(
            account_username="own@example.test",
            email_uid="1001",
            subject="主题",
            sender="noreply@sender.example",
            recipients='["own@example.test"]',
            body_text="正文",
            has_attachments=True,
            attachment_names='["a.pdf"]',
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE,
        )
        elsewhere = tmp_path / "elsewhere" / "mail"
        (elsewhere / "a.pdf").parent.mkdir(parents=True, exist_ok=True)
        (elsewhere / "a.pdf").write_bytes(b"pdf")
        make_dir_link(mail.email_attachment_root() / "own@example.test" / "1001", elsewhere)

        mail.run_attachment_cleanup(await mail.plan_attachment_cleanup([row]))

        assert not (mail.email_attachment_root() / "own@example.test" / "1001").exists()
        assert (elsewhere / "a.pdf").exists()


# ---------------------------------------------------------------------------
# UID 白名单、先断开再算附件
# ---------------------------------------------------------------------------


class TestAttachmentHardening:
    @pytest.mark.parametrize("uid", ["12a", "１２", "../1", "", "1" * 21])
    def test_only_ascii_digit_uids_make_a_path(self, uid: str) -> None:
        assert mail.attachment_file_path("_i1", uid, "a.pdf") is None

    def test_a_digit_uid_makes_a_path(self) -> None:
        path = mail.attachment_file_path("_i1", "1001", "a.pdf")
        assert path is not None and path.parent.name == "1001"

    async def test_an_address_directory_must_contain_an_at(self, db) -> None:  # noqa: ARG002
        """地址目录那一支只认含 `@` 的目录名：账户地址是坏数据（没有 `@`）时不按它去删"""
        row = await DBEmailAccount.create(
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE,
            username="ops",
            display_name="",
            provider="自定义",
            settings={},
            password="secret",
            attachment_dir="ops",
        )
        await DBEmail.create(
            account_username="ops",
            email_uid="1001",
            subject="主题",
            sender="noreply@sender.example",
            recipients='["ops"]',
            body_text="正文",
            has_attachments=False,
            attachment_names="[]",
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE,
        )
        kept = mail.email_attachment_root() / "ops" / "1001"
        kept.mkdir(parents=True)

        mail.run_attachment_cleanup(await mail.plan_attachment_cleanup([row]))

        assert kept.is_dir()

    @pytest.mark.parametrize("name", ["_i1", "_i007"])
    def test_instance_dir_names(self, name: str) -> None:
        assert mail.is_instance_attachment_dir(name)

    @pytest.mark.parametrize("name", ["_i", "_i１", "_ia", "own@example.test"])
    def test_not_instance_dir_names(self, name: str) -> None:
        assert not mail.is_instance_attachment_dir(name)

    async def test_the_route_disconnects_the_client_before_planning(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.routers import instance_email

        tenant = await make_tenant("acme")
        await make_instance(INSTANCE, tenant.id)
        row = await DBEmailAccount.create(
            owner_adapter_key=ADAPTER,
            owner_instance_key=INSTANCE,
            username="own@example.test",
            display_name="",
            provider="自定义",
            settings={},
            password="secret",
            attachment_dir="",
        )
        order: List[str] = []
        original_plan = mail.plan_attachment_cleanup

        async def removed(*_args: Any, **_kwargs: Any) -> None:
            order.append("disconnect")

        async def planned(rows: Any) -> Any:
            order.append("plan")
            return await original_plan(rows)

        monkeypatch.setattr(mail, "remove_runtime_account", removed)
        monkeypatch.setattr(mail, "plan_attachment_cleanup", planned)
        monkeypatch.setattr(instance_email, "_get_email_adapter", lambda: object())

        await instance_email.delete_instance_email_account(row.id, ADAPTER, INSTANCE, _current_user=platform_owner())

        assert order == ["disconnect", "plan"]


# ---------------------------------------------------------------------------
# 跨租户的迁移、并入
# ---------------------------------------------------------------------------


class TestCrossTenantMigration:
    async def _world(self, *, same_tenant: bool = False) -> List[str]:
        acme = await make_tenant("acme")
        rival = acme if same_tenant else await make_tenant("rival")
        await make_instance(INSTANCE, acme.id)
        await make_instance(TARGET, rival.id)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        seed_plugin_settings(INSTANCE, [own])
        seed_config_overrides(INSTANCE, [own])
        return [own]

    async def test_the_preview_counts_and_warns(self, db) -> None:  # noqa: ARG002
        await self._world()

        preview = await preview_console_instance_migration(
            instance_key=TARGET,
            source_instance_key=INSTANCE,
            _current_user=platform_owner(),
        )

        assert preview.cross_tenant
        assert (preview.plugin_settings, preview.config_overrides) == (2, 2)
        assert any("2 个插件的设置" in warning for warning in preview.warnings)

    async def test_by_default_the_source_settings_are_deleted(self, db, offline_qdrant) -> None:  # noqa: ARG002
        [own] = await self._world()

        result = await run_console_instance_migration(
            instance_key=TARGET,
            body=InstanceMigrationRequest(source_instance_key=INSTANCE, confirm_source_instance_key=INSTANCE),
            _current_user=platform_owner(),
        )

        assert result.counts["plugin_settings_deleted"] == 2
        assert result.counts["config_overrides_deleted"] == 2
        moved = chat_key(TARGET, "group_1")
        plugin_settings_gone(TARGET, [moved])
        plugin_settings_gone(INSTANCE, [own])
        config_overrides_gone(TARGET, [moved])
        config_overrides_gone(INSTANCE, [own])

    async def test_unticking_lets_them_follow(self, db, offline_qdrant) -> None:  # noqa: ARG002
        await self._world()

        await run_console_instance_migration(
            instance_key=TARGET,
            body=InstanceMigrationRequest(
                source_instance_key=INSTANCE,
                confirm_source_instance_key=INSTANCE,
                delete_plugin_settings=False,
                delete_config_overrides=False,
            ),
            _current_user=platform_owner(),
        )

        moved = chat_key(TARGET, "group_1")
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, TARGET) == {"API_KEY": OLD_SECRET}
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, moved) == {SWITCH_PLUGIN: True}
        assert read_yaml(instance_override_path(TARGET))[WEBHOOK_FIELD] == OLD_WEBHOOK
        assert read_yaml(channel_override_path(moved))[CONTEXT_FIELD] == 7

    async def test_the_same_tenant_always_follows(self, db, offline_qdrant) -> None:  # noqa: ARG002
        await self._world(same_tenant=True)

        await run_console_instance_migration(
            instance_key=TARGET,
            body=InstanceMigrationRequest(source_instance_key=INSTANCE, confirm_source_instance_key=INSTANCE),
            _current_user=platform_owner(),
        )

        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, TARGET) == {"API_KEY": OLD_SECRET}
        assert read_yaml(instance_override_path(TARGET))[WEBHOOK_FIELD] == OLD_WEBHOOK

    async def test_adopting_into_another_tenant_deletes_by_default(self, db, offline_qdrant) -> None:  # noqa: ARG002
        acme = await make_tenant("acme")
        source = await make_instance("ob123456", status="unclaimed")
        target = await make_instance(TARGET, acme.id)
        own = (await make_channel("ob123456", "group_1")).chat_key
        seed_plugin_settings("ob123456", [own])
        seed_config_overrides("ob123456", [own])

        await instance_manager.adopt_unclaimed_instance(
            source,
            target,
            delete_plugin_settings=True,
            delete_config_overrides=True,
        )

        moved = chat_key(TARGET, "group_1")
        plugin_settings_gone(TARGET, [moved])
        config_overrides_gone(TARGET, [moved])


# ---------------------------------------------------------------------------
# P3：迁移部分失败、划转遇跨适配器同名、核心配置覆盖跟着走
# ---------------------------------------------------------------------------


class TestMigrationEdges:
    async def test_a_failed_channel_keeps_its_settings_and_the_instance_level_ones(
        self,
        db,  # noqa: ARG002
        offline_qdrant,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await make_instance(INSTANCE)
        await make_instance(TARGET)
        stuck = (await make_channel(INSTANCE, "group_stuck")).chat_key
        orphan = chat_key(INSTANCE, "group_gone")
        seed_plugin_settings(INSTANCE, [stuck, orphan])
        original = chat_key_migration._migrate_one_channel  # noqa: SLF001

        async def fail_stuck(adapter_key: str, item: Any, target_instance_key: str, **kwargs: Any) -> Dict[str, int]:
            if item.source_chat_key == stuck:
                raise RuntimeError("boom")
            return await original(adapter_key, item, target_instance_key, **kwargs)

        monkeypatch.setattr(chat_key_migration, "_migrate_one_channel", fail_stuck)

        outcome = await chat_key_migration.migrate_instance_ownership(ADAPTER, INSTANCE, TARGET)

        assert outcome.failed_channels == ["group_stuck"]
        # 还在源实例名下的频道：开关不被当成孤儿搬走
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, stuck) == {SWITCH_PLUGIN: True}
        # 源实例还有频道在用：实例级设置留在源实例
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, INSTANCE) == {"API_KEY": OLD_SECRET}
        assert scope_store.get_scope_overrides(OverrideScope.INSTANCE, INSTANCE) == {SECRET_PLUGIN: True}
        # 真正的孤儿照样搬
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, chat_key(TARGET, "group_gone")) == {
            SWITCH_PLUGIN: True,
        }

    async def test_core_overrides_follow_a_chat_key_rename_and_the_new_key_wins(self, db) -> None:  # noqa: ARG002
        old = f"{ADAPTER}-group_1"
        new = chat_key(INSTANCE, "group_1")
        write_yaml(
            channel_override_path(old),
            {f"enable_{CONTEXT_FIELD}": True, CONTEXT_FIELD: 7, "enable_AI_DEBOUNCE_WAIT_SECONDS": True,
             "AI_DEBOUNCE_WAIT_SECONDS": 3},
        )
        write_yaml(channel_override_path(new), {f"enable_{CONTEXT_FIELD}": True, CONTEXT_FIELD: 9})

        counts = await chat_key_migration.migrate_chat_key_references(old, new, instance_key=INSTANCE)

        assert counts["config_channel_override"] == 1
        assert not channel_override_path(old).exists()
        merged = read_yaml(channel_override_path(new))
        assert merged[CONTEXT_FIELD] == 9
        assert merged["AI_DEBOUNCE_WAIT_SECONDS"] == 3

    async def test_core_overrides_follow_an_instance_migration(self, db, offline_qdrant) -> None:  # noqa: ARG002
        await make_instance(INSTANCE)
        await make_instance(TARGET)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        orphan = chat_key(INSTANCE, "group_gone")
        seed_config_overrides(INSTANCE, [own, orphan])

        outcome = await chat_key_migration.migrate_instance_ownership(ADAPTER, INSTANCE, TARGET)

        assert read_yaml(instance_override_path(TARGET))[WEBHOOK_FIELD] == OLD_WEBHOOK
        assert read_yaml(channel_override_path(chat_key(TARGET, "group_1")))[CONTEXT_FIELD] == 7
        assert read_yaml(channel_override_path(chat_key(TARGET, "group_gone")))[CONTEXT_FIELD] == 7
        config_overrides_gone(INSTANCE, [own, orphan])
        assert outcome.residual["config_overrides"] == 0


class TestTransferEdges:
    async def test_a_namesake_in_another_adapter_that_stays_refuses_the_transfer(self, db) -> None:  # noqa: ARG002
        acme = await make_tenant("acme")
        rival = await make_tenant("rival")
        await make_instance(INSTANCE, acme.id)
        await make_instance(INSTANCE, rival.id, adapter_key="other_adapter")
        seed_plugin_settings(INSTANCE, [])

        with pytest.raises(ValidationError):
            await tenants_router.bind_tenant_instances(
                tenant_id=acme.id,
                body=TenantInstanceBindRequest(instance_keys=[]),
                _current_user=platform_owner(),
            )

        assert (await DBAdapterInstance.get(adapter_key=ADAPTER, instance_key=INSTANCE)).tenant_id == acme.id
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, INSTANCE) == {"API_KEY": OLD_SECRET}

    async def test_core_overrides_are_deleted_by_default_and_kept_when_unticked(self, db) -> None:  # noqa: ARG002
        acme = await make_tenant("acme")
        await make_instance(INSTANCE, acme.id)
        await make_instance(OTHER, acme.id)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        kept = (await make_channel(OTHER, "group_2")).chat_key
        seed_config_overrides(INSTANCE, [own])
        seed_config_overrides(OTHER, [kept])

        preview = await tenants_router.preview_tenant_instances(
            tenant_id=acme.id,
            body=TenantInstanceBindRequest(instance_keys=[]),
            _current_user=platform_owner(),
        )
        assert sorted(change.config_overrides for change in preview.changes) == [2, 2]

        await tenants_router.bind_tenant_instances(
            tenant_id=acme.id,
            body=TenantInstanceBindRequest(instance_keys=[OTHER]),
            _current_user=platform_owner(),
        )
        config_overrides_gone(INSTANCE, [own])

        await tenants_router.bind_tenant_instances(
            tenant_id=acme.id,
            body=TenantInstanceBindRequest(instance_keys=[], delete_config_overrides=False),
            _current_user=platform_owner(),
        )
        assert read_yaml(instance_override_path(OTHER))[WEBHOOK_FIELD] == OLD_WEBHOOK
        assert channel_override_path(kept).exists()


# ---------------------------------------------------------------------------
# 进程级加载状态重同步
# ---------------------------------------------------------------------------


class TestLoadStateResync:
    async def test_a_plugin_kept_alive_only_by_the_deleted_instance_goes_down(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.core.config import config
        from nekro_agent.services.plugin import manager
        from nekro_agent.services.plugin.router_manager import plugin_router_manager

        class StubPlugin:
            key = SECRET_PLUGIN
            name = "stub"
            is_enabled = True
            disabled = False

            async def enable(self) -> None:
                self.is_enabled = True

            async def disable(self) -> None:
                self.is_enabled = False
                self.disabled = True

        stub = StubPlugin()

        async def no_notify(*_args: Any) -> None:
            return None

        monkeypatch.setattr(manager.plugin_collector, "get_plugin", lambda key: stub if key == SECRET_PLUGIN else None)
        monkeypatch.setattr(plugin_router_manager, "unmount_plugin_router", lambda _key: None)
        monkeypatch.setattr(manager, "_notify_commands_changed_after_plugin_toggle", no_notify)
        monkeypatch.setattr(config, "PLUGIN_ENABLED", [key for key in config.PLUGIN_ENABLED if key != SECRET_PLUGIN])

        failures = await manager.sync_plugin_load_states([SECRET_PLUGIN, "test_scope.not_loaded"])

        assert failures == {}
        assert stub.disabled


# ---------------------------------------------------------------------------
# 复核后补：选了删就一路不搬、登记时没扫成的补扫、平台方配置接口、前缀撞名、标识形态与大小写
# ---------------------------------------------------------------------------


async def make_marked_instance(
    instance_key: str,
    tenant_id: Optional[int] = None,
    *,
    status: str = "unclaimed",
) -> DBAdapterInstance:
    """协议端自动登记时兜底清扫失败、带着补扫标记的一行"""
    row = await make_instance(instance_key, tenant_id, status=status)
    row.metadata_json = instance_settings.sweep_pending_metadata()
    await row.save(update_fields=["metadata_json"])
    return row


class Switch:
    """可以中途关掉的故障：`broken` 为真时兜底清扫抛错"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.broken = True
        original = instance_settings.sweep_before_create

        async def sweep(adapter_key: str, instance_key: str) -> Dict[str, int]:
            if self.broken:
                raise OSError("busy")
            return await original(adapter_key, instance_key)

        monkeypatch.setattr(instance_settings, "sweep_before_create", sweep)


class TestDeleteModeMovesNothing:
    async def _world(self) -> List[str]:
        acme = await make_tenant("acme")
        rival = await make_tenant("rival")
        await make_instance(INSTANCE, acme.id)
        await make_instance(TARGET, rival.id)
        keys = [(await make_channel(INSTANCE, channel_id)).chat_key for channel_id in ("group_1", "group_2")]
        seed_plugin_settings(INSTANCE, keys)
        seed_config_overrides(INSTANCE, keys)
        return keys

    async def test_settings_written_during_the_loop_never_reach_the_target(
        self,
        db,  # noqa: ARG002
        offline_qdrant,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        keys = await self._world()
        # group_2 在目标实例上也有一行：走合并（源行并进目标行）那条分支
        await make_channel(TARGET, "group_2")
        orphan = chat_key(INSTANCE, "group_gone")
        original = chat_key_migration._migrate_one_channel  # noqa: SLF001

        async def source_tenant_writes_then_move(
            adapter_key: str,
            item: Any,
            target_instance_key: str,
            **kwargs: Any,
        ) -> Dict[str, int]:
            # 开头那一遍删过之后、搬这个频道之前：源租户的后台页面还开着，实例级、这个频道、
            # 一个没有频道行的前缀标识上又各写了一遍
            seed_plugin_settings(INSTANCE, [item.source_chat_key, orphan])
            seed_config_overrides(INSTANCE, [item.source_chat_key, orphan])
            return await original(adapter_key, item, target_instance_key, **kwargs)

        monkeypatch.setattr(chat_key_migration, "_migrate_one_channel", source_tenant_writes_then_move)

        outcome = await chat_key_migration.migrate_instance_ownership(
            ADAPTER,
            INSTANCE,
            TARGET,
            delete_plugin_settings=True,
            delete_config_overrides=True,
        )

        assert outcome.failed_channels == []
        assert (outcome.moved_channels, outcome.merged_channels) == (["group_1"], ["group_2"])
        moved = [chat_key(TARGET, channel_id) for channel_id in ("group_1", "group_2", "group_gone")]
        plugin_settings_gone(TARGET, moved)
        config_overrides_gone(TARGET, moved)
        # 源标识上写回的也删了（搬完之后那一遍）
        plugin_settings_gone(INSTANCE, [*keys, orphan])
        config_overrides_gone(INSTANCE, [*keys, orphan])

    async def test_a_namesake_in_another_adapter_refuses_deleting(self, db) -> None:  # noqa: ARG002
        acme = await make_tenant("acme")
        rival = await make_tenant("rival")
        await make_instance(INSTANCE, acme.id)
        await make_instance(INSTANCE, rival.id, adapter_key="other_adapter")
        await make_instance(TARGET, rival.id)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        seed_plugin_settings(INSTANCE, [own])

        with pytest.raises(ValidationError):
            await chat_key_migration.migrate_instance_ownership(ADAPTER, INSTANCE, TARGET, delete_plugin_settings=True)

        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, INSTANCE) == {"API_KEY": OLD_SECRET}
        assert await DBChatChannel.filter(chat_key=own, instance_key=INSTANCE).exists()

    async def test_a_delete_that_fails_is_a_400_and_nothing_moves(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        [own, _] = await self._world()

        async def broken(*_args: Any, **_kwargs: Any) -> Dict[str, int]:
            raise OSError("busy")

        monkeypatch.setattr(instance_settings, "drop_instance_settings", broken)

        with pytest.raises(ValidationError):
            await chat_key_migration.migrate_instance_ownership(
                ADAPTER,
                INSTANCE,
                TARGET,
                delete_plugin_settings=True,
                delete_config_overrides=True,
            )

        assert await DBChatChannel.filter(chat_key=own, instance_key=INSTANCE).exists()

    async def test_the_adopt_message_names_the_deleted_settings(self, db, offline_qdrant) -> None:  # noqa: ARG002
        acme = await make_tenant("acme")
        await make_instance("ob123456", status="unclaimed")
        await make_instance(TARGET, acme.id)
        own = (await make_channel("ob123456", "group_1")).chat_key
        seed_plugin_settings("ob123456", [own])
        seed_config_overrides("ob123456", [own])

        result = await instances_router.adopt_console_instance(
            instance_key=TARGET,
            body=InstanceAdoptRequest(source_instance_key="ob123456", confirm_source_instance_key="ob123456"),
            _current_user=platform_owner(),
        )

        assert "删除了 2 个插件的设置、2 个配置覆盖文件" in result.message

    async def test_a_transfer_deletes_again_after_the_owner_changes(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        acme = await make_tenant("acme")
        await make_instance(INSTANCE, acme.id)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        seed_plugin_settings(INSTANCE, [own])
        seed_config_overrides(INSTANCE, [own])
        original = tenants_router._delete_instance_settings  # noqa: SLF001

        async def delete_then_write_back(*args: Any, **kwargs: Any) -> None:
            await original(*args, **kwargs)
            # 删过之后、改归属之前：前一个主人又写了一遍
            seed_plugin_settings(INSTANCE, [own])
            seed_config_overrides(INSTANCE, [own])

        monkeypatch.setattr(tenants_router, "_delete_instance_settings", delete_then_write_back)

        await tenants_router.bind_tenant_instances(
            tenant_id=acme.id,
            body=TenantInstanceBindRequest(instance_keys=[]),
            _current_user=platform_owner(),
        )

        assert (await DBAdapterInstance.get(adapter_key=ADAPTER, instance_key=INSTANCE)).tenant_id is None
        plugin_settings_gone(INSTANCE, [own])
        config_overrides_gone(INSTANCE, [own])


class TestSweepPendingMark:
    async def test_a_failed_sweep_marks_the_auto_registered_row(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.adapters.onebot_v11.core import instance as onebot_instance

        Switch(monkeypatch)

        created = await onebot_instance._register_unclaimed_instance("123456")  # noqa: SLF001

        assert instance_settings.is_sweep_pending(await DBAdapterInstance.get(id=created.id))

    async def test_a_clean_registration_carries_no_mark(self, db) -> None:  # noqa: ARG002
        from nekro_agent.adapters.onebot_v11.core import instance as onebot_instance

        created = await onebot_instance._register_unclaimed_instance("123456")  # noqa: SLF001

        assert not instance_settings.is_sweep_pending(await DBAdapterInstance.get(id=created.id))

    async def test_claiming_in_place_resweeps_and_clears_the_mark(self, db) -> None:  # noqa: ARG002
        row = await make_marked_instance("ob123456")
        own = chat_key("ob123456", "group_1")
        seed_plugin_settings("ob123456", [own])
        seed_config_overrides("ob123456", [own])

        await instance_manager.claim_instance(row, "mine")

        plugin_settings_gone("ob123456", [own])
        config_overrides_gone("ob123456", [own])
        assert not instance_settings.is_sweep_pending(await DBAdapterInstance.get(id=row.id))

    async def test_claiming_refuses_when_the_resweep_fails(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        row = await make_marked_instance("ob123456")
        Switch(monkeypatch)

        with pytest.raises(ValidationError):
            await instance_manager.claim_instance(row, "mine")

        fresh = await DBAdapterInstance.get(id=row.id)
        assert fresh.status == "unclaimed"
        assert instance_settings.is_sweep_pending(fresh)

    async def test_adopting_a_marked_row_carries_no_leftovers_even_in_one_tenant(
        self,
        db,  # noqa: ARG002
        offline_qdrant,  # noqa: ARG002
    ) -> None:
        source = await make_marked_instance("ob123456")
        target = await make_instance(TARGET)
        own = (await make_channel("ob123456", "group_1")).chat_key
        seed_plugin_settings("ob123456", [own])
        seed_config_overrides("ob123456", [own])

        await instance_manager.adopt_unclaimed_instance(source, target)

        moved = chat_key(TARGET, "group_1")
        plugin_settings_gone(TARGET, [moved])
        config_overrides_gone(TARGET, [moved])

    async def test_adopting_refuses_when_the_resweep_fails(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = await make_marked_instance("ob123456")
        target = await make_instance(TARGET)
        own = (await make_channel("ob123456", "group_1")).chat_key
        Switch(monkeypatch)

        with pytest.raises(ValidationError):
            await instance_manager.adopt_unclaimed_instance(source, target)

        assert await DBChatChannel.filter(chat_key=own, instance_key="ob123456").exists()

    async def test_claiming_as_a_new_instance_leaves_no_mark_on_either_row(
        self,
        db,  # noqa: ARG002
        offline_qdrant,  # noqa: ARG002
    ) -> None:
        source = await make_marked_instance("ob123456")

        outcome = await instance_manager.claim_unclaimed_as_new_instance(source, "", TARGET)

        assert not instance_settings.is_sweep_pending(await DBAdapterInstance.get(id=outcome.target.id))
        assert not instance_settings.is_sweep_pending(await DBAdapterInstance.get(id=source.id))

    async def test_claiming_as_a_new_instance_refuses_before_creating_the_row(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = await make_marked_instance("ob123456")
        Switch(monkeypatch)

        with pytest.raises(ValidationError):
            await instance_manager.claim_unclaimed_as_new_instance(source, "", TARGET)

        assert not await DBAdapterInstance.filter(instance_key=TARGET).exists()

    async def test_a_transfer_resweeps_first_and_refuses_when_it_cannot(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        acme = await make_tenant("acme")
        row = await make_marked_instance(INSTANCE, status="online")
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        seed_plugin_settings(INSTANCE, [own])
        seed_config_overrides(INSTANCE, [own])
        switch = Switch(monkeypatch)
        # 取消勾选删除：没有补扫的话残留会跟着实例划过去
        body = TenantInstanceBindRequest(
            instance_keys=[INSTANCE],
            delete_plugin_settings=False,
            delete_config_overrides=False,
        )

        with pytest.raises(ValidationError):
            await tenants_router.bind_tenant_instances(tenant_id=acme.id, body=body, _current_user=platform_owner())
        assert (await DBAdapterInstance.get(id=row.id)).tenant_id is None

        switch.broken = False
        await tenants_router.bind_tenant_instances(tenant_id=acme.id, body=body, _current_user=platform_owner())

        fresh = await DBAdapterInstance.get(id=row.id)
        assert fresh.tenant_id == acme.id
        assert not instance_settings.is_sweep_pending(fresh)
        plugin_settings_gone(INSTANCE, [own])
        config_overrides_gone(INSTANCE, [own])

    @pytest.mark.parametrize(
        ("metadata_json", "pending"),
        [
            ("", False),
            ('{"other": 1}', False),
            (f'{{"{instance_settings.SWEEP_PENDING_KEY}": true, "other": 1}}', True),
            (f'{{"{instance_settings.SWEEP_PENDING_KEY}": false}}', False),
            (f'not json {instance_settings.SWEEP_PENDING_KEY}', True),
            ("[1, 2]", False),
        ],
    )
    def test_reading_the_mark(self, metadata_json: str, pending: bool) -> None:
        assert instance_settings.is_sweep_pending(DBAdapterInstance(metadata_json=metadata_json)) is pending

    async def test_clearing_the_mark_keeps_the_rest_of_the_metadata(self, db) -> None:  # noqa: ARG002
        row = await make_instance("ob123456", status="unclaimed")
        row.metadata_json = f'{{"{instance_settings.SWEEP_PENDING_KEY}": true, "access_token": "t"}}'
        await row.save(update_fields=["metadata_json"])

        assert await instance_settings.resweep_if_pending(row)

        assert yaml.safe_load((await DBAdapterInstance.get(id=row.id)).metadata_json) == {"access_token": "t"}
        assert not await instance_settings.resweep_if_pending(row)


class TestPurgeAfterCommit:
    async def test_the_last_drop_runs_even_when_an_earlier_step_fails(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance = await make_instance(INSTANCE)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        original = instance_manager._disconnect_instance_mailboxes  # noqa: SLF001

        async def write_back_then_disconnect(*args: Any, **kwargs: Any) -> None:
            seed_plugin_settings(INSTANCE, [own])
            await original(*args, **kwargs)

        async def vectors_down(_instance_key: str) -> None:
            raise RuntimeError("vector store down")

        monkeypatch.setattr(instance_manager, "_disconnect_instance_mailboxes", write_back_then_disconnect)
        monkeypatch.setattr(instance_manager, "purge_instance_vectors", vectors_down)

        with pytest.raises(RuntimeError):
            await instance_manager.purge_instance(instance)

        plugin_settings_gone(INSTANCE, [own])

    async def test_the_sweep_revokes_the_platform_mail_share(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: List[Any] = []

        def record(source_ref: str, target_ref: Optional[str]) -> int:
            calls.append((source_ref, target_ref))
            return 1

        monkeypatch.setattr(mail, "rewrite_platform_share_instances", record)

        counts = await instance_settings.sweep_before_create(ADAPTER, INSTANCE)

        assert calls == [(f"{ADAPTER}:{INSTANCE}", None)]
        assert counts["platform_mail_share"] == 1


class TestForeignPrefixKeys:
    """频道标识由客户端给的适配器上，别的实例的频道可以恰好以 `<适配器>-<这个实例>-` 开头"""

    async def _foreign(self) -> str:
        await make_instance(INSTANCE)
        await make_instance(OTHER)
        foreign = await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=OTHER,
            channel_id="group_9",
            chat_key=chat_key(INSTANCE, "group_9"),
            channel_type="group",
            data="{}",
        )
        scope_store.set_scope_override(OverrideScope.SESSION, foreign.chat_key, SWITCH_PLUGIN, True)
        write_yaml(channel_override_path(foreign.chat_key), {f"enable_{CONTEXT_FIELD}": True, CONTEXT_FIELD: 7})
        return foreign.chat_key

    def _kept(self, foreign: str) -> None:
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, foreign) == {SWITCH_PLUGIN: True}
        assert channel_override_path(foreign).exists()

    async def test_a_drop_keeps_it(self, db) -> None:  # noqa: ARG002
        foreign = await self._foreign()

        await instance_settings.drop_instance_settings(ADAPTER, INSTANCE, [])

        self._kept(foreign)

    async def test_a_sweep_keeps_it(self, db) -> None:  # noqa: ARG002
        foreign = await self._foreign()

        await instance_settings.sweep_before_create(ADAPTER, INSTANCE)

        self._kept(foreign)

    async def test_a_migration_does_not_carry_it(self, db) -> None:  # noqa: ARG002
        foreign = await self._foreign()

        await instance_settings.move_instance_settings(ADAPTER, INSTANCE, TARGET)

        self._kept(foreign)
        moved = chat_key(TARGET, "group_9")
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, moved) == {}
        assert not channel_override_path(moved).exists()

    async def test_the_preview_does_not_count_it(self, db) -> None:  # noqa: ARG002
        await self._foreign()

        counts = await instance_settings.count_instance_settings(ADAPTER, INSTANCE)

        assert (counts.plugin_settings, counts.config_overrides) == (0, 0)


class TestCoreOverrideLinksAndCache:
    async def test_a_linked_instance_dir_without_a_file_loses_the_link(self, db, tmp_path: Path) -> None:  # noqa: ARG002
        elsewhere = tmp_path / "elsewhere"
        link = instance_override_path(INSTANCE).parent
        make_dir_link(link, elsewhere)

        await instance_settings.drop_instance_settings(ADAPTER, INSTANCE, [])

        assert not os.path.lexists(link)
        assert elsewhere.is_dir()

    async def test_a_dangling_instance_link_is_removed(self, db, tmp_path: Path) -> None:  # noqa: ARG002
        elsewhere = tmp_path / "elsewhere"
        link = instance_override_path(INSTANCE).parent
        make_dir_link(link, elsewhere)
        elsewhere.rmdir()

        await instance_settings.drop_instance_settings(ADAPTER, INSTANCE, [])

        assert not os.path.lexists(link)

    async def test_cached_overrides_are_forgotten_even_without_files(self, db) -> None:  # noqa: ARG002
        from nekro_agent.core.overridable_config import OverridableConfig

        await make_instance(INSTANCE)
        await make_instance(OTHER)
        # 调用方点名的会话标识不带实例前缀（单实例年代的旧格式）：只有「传进来的一律注销」这一支能管到它
        passed = f"{ADAPTER}-group_1"
        orphan = chat_key(INSTANCE, "group_gone")
        foreign = (
            await DBChatChannel.create(
                adapter_key=ADAPTER,
                instance_key=OTHER,
                channel_id="group_9",
                chat_key=chat_key(INSTANCE, "group_9"),
                channel_type="group",
                data="{}",
            )
        ).chat_key
        instance_key = config_service.instance_override_config_key(ADAPTER, INSTANCE)
        channel_keys = {key: f"{config_service.CHANNEL_OVERRIDE_KEY_PREFIX}{key}" for key in (passed, orphan, foreign)}
        for config_key in [instance_key, *channel_keys.values()]:
            ConfigManager.register_config(config_key, OverridableConfig())

        await instance_settings.drop_instance_settings(ADAPTER, INSTANCE, [passed])

        assert ConfigManager.get_config(instance_key) is None
        assert ConfigManager.get_config(channel_keys[passed]) is None
        assert ConfigManager.get_config(channel_keys[orphan]) is None
        assert ConfigManager.get_config(channel_keys[foreign]) is not None
        ConfigManager.unregister_config(channel_keys[foreign])


class TestPlatformConfigRoutes:
    async def test_set_and_batch_on_a_missing_instance_are_404(self, db) -> None:  # noqa: ARG002
        key = config_service.instance_override_config_key(ADAPTER, INSTANCE)

        with pytest.raises(NotFoundError):
            await config_router.set_config_value(key, CONTEXT_FIELD, value="7", _current_user=platform_owner())
        with pytest.raises(NotFoundError):
            await config_router.batch_update_config(
                key,
                config_router.BatchUpdateConfig(configs={f"enable_{CONTEXT_FIELD}": "true", CONTEXT_FIELD: "7"}),
                _current_user=platform_owner(),
            )

        assert not instance_override_path(INSTANCE).exists()

    async def test_set_on_a_missing_channel_is_404(self, db) -> None:  # noqa: ARG002
        key = f"{config_service.CHANNEL_OVERRIDE_KEY_PREFIX}{chat_key(INSTANCE, 'group_1')}"

        with pytest.raises(NotFoundError):
            await config_router.set_config_value(key, CONTEXT_FIELD, value="7", _current_user=platform_owner())

        assert not channel_override_path(chat_key(INSTANCE, "group_1")).exists()

    async def test_the_instance_endpoints_check_adapter_and_instance(self, db) -> None:  # noqa: ARG002
        await make_instance(INSTANCE, adapter_key="other_adapter")

        with pytest.raises(NotFoundError):
            await config_router.get_instance_config(adapter_key=ADAPTER, instance_key=INSTANCE, _current_user=platform_owner())
        with pytest.raises(NotFoundError):
            await config_router.update_instance_config(
                adapter_key=ADAPTER,
                instance_key=INSTANCE,
                body=config_router.BatchUpdateConfig(configs={}),
                _current_user=platform_owner(),
            )

        assert not instance_override_path(INSTANCE).exists()

    async def test_an_existing_instance_passes(self, db) -> None:  # noqa: ARG002
        await make_instance(INSTANCE)

        await config_router.update_instance_config(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            body=config_router.BatchUpdateConfig(configs={f"enable_{CONTEXT_FIELD}": "true", CONTEXT_FIELD: "7"}),
            _current_user=platform_owner(),
        )

        assert read_yaml(instance_override_path(INSTANCE))[CONTEXT_FIELD] == 7
        assert await config_router.get_instance_config(adapter_key=ADAPTER, instance_key=INSTANCE, _current_user=platform_owner())


class TestNewInstanceKeys:
    WECHAT = "wechat_ilink_multi"

    @staticmethod
    def _wechat_create_endpoint() -> Any:
        from nekro_agent.adapters.wechat_ilink_multi.routers import create_router

        for route in create_router().routes:
            if getattr(route, "path", "") == "/instances" and "POST" in getattr(route, "methods", set()):
                return route.endpoint  # type: ignore[attr-defined]
        raise AssertionError("找不到新建实例的路由")

    @pytest.mark.parametrize("key", ["bot1 ", "bot1.", "..", "a/b", "a.b", "机器人", "x" * 129])
    async def test_odd_keys_are_refused_by_the_wechat_create(self, db, key: str) -> None:  # noqa: ARG002
        from fastapi import Response

        from nekro_agent.adapters.wechat_ilink_multi.schemas import InstanceCreateRequest as WechatCreateRequest

        endpoint = self._wechat_create_endpoint()

        with pytest.raises(ValidationError):
            await endpoint(
                body=WechatCreateRequest(instance_key=key),
                response=Response(),
                _current_user=platform_owner(),
                _owner=platform_owner(),
            )
        assert not await DBAdapterInstance.filter(adapter_key=self.WECHAT).exists()

    async def test_a_plain_key_is_created(self, db) -> None:  # noqa: ARG002
        from fastapi import Response

        from nekro_agent.adapters.wechat_ilink_multi.schemas import InstanceCreateRequest as WechatCreateRequest

        result = await self._wechat_create_endpoint()(
            body=WechatCreateRequest(instance_key="Bot_1-a"),
            response=Response(),
            _current_user=platform_owner(),
            _owner=platform_owner(),
        )

        assert result.instance_key == "Bot_1-a"
        assert not result.existing

    @pytest.mark.parametrize("key", ["BOT1", "bot1"])
    async def test_another_adapter_s_key_conflicts_in_any_case(self, db, key: str) -> None:  # noqa: ARG002
        from nekro_agent.services.adapter_instance_service import adapter_instance_service

        await make_instance("bot1")

        with pytest.raises(ConflictError):
            await adapter_instance_service.create_instance(self.WECHAT, key, "x", "")

    async def test_a_case_variant_in_the_same_adapter_conflicts(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.adapter_instance_service import adapter_instance_service

        await make_instance("Bot1", adapter_key=self.WECHAT)

        with pytest.raises(ConflictError):
            await adapter_instance_service.create_instance(self.WECHAT, "bot1", "x", "")
        # 精确同名照旧按更新处理
        await adapter_instance_service.create_instance(self.WECHAT, "Bot1", "renamed", "")
        assert (await DBAdapterInstance.get(adapter_key=self.WECHAT, instance_key="Bot1")).display_name == "renamed"

    async def test_the_console_create_is_case_insensitive_too(self, db) -> None:  # noqa: ARG002
        await make_instance("Bot1", adapter_key=self.WECHAT)

        with pytest.raises(ConflictError):
            await instance_manager.create_instance(
                InstanceCreateRequest(adapter_key=ADAPTER, instance_key="bot1", provider=InstanceProvider.REMOTE_ONEBOT),
            )

    async def test_claiming_as_a_new_instance_is_case_insensitive_too(self, db) -> None:  # noqa: ARG002
        source = await make_instance("ob123456", status="unclaimed")
        await make_instance("Bot1", adapter_key=self.WECHAT)

        with pytest.raises(ConflictError):
            await instance_manager.claim_unclaimed_as_new_instance(source, "", "bot1")

    @pytest.mark.parametrize("taken", ["OB123456", "ob123456"])
    async def test_auto_registration_steps_over_another_adapter_s_key(self, db, taken: str) -> None:  # noqa: ARG002
        from nekro_agent.adapters.onebot_v11.core import instance as onebot_instance

        await make_instance(taken, adapter_key=self.WECHAT)

        created = await onebot_instance._register_unclaimed_instance("123456")  # noqa: SLF001

        assert created.instance_key == "ob123456x"
