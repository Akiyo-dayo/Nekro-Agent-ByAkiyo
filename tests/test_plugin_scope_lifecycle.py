"""插件按实例 / 按频道设置的生命周期：彻底删除、划转、换会话标识、实例迁移

## 这条测试钉的是什么

插件有三样东西按实例或频道存：实例级启停（`instances.yaml`，键是实例标识）、频道级启停
（`sessions.yaml`，键是会话标识）、实例级配置目录（`plugin_data/<插件>/instances/<实例>/`，
插件的 API Key 之类就在这里）。它们都不在数据库里，以前四条生命周期路径一样都没管：

- 彻底删除实例：三样一律删，删在改库**之前**（删不掉就整次删除失败、实例原样留着）；
- 划转给别的租户：默认删、预览报受影响的插件数，取消勾选才跟过去；原样重放不删；
- 换会话标识：频道级启停跟到新标识上，新标识下已有的条目保留；
- 实例迁移：实例级启停与配置目录改到目标实例，频道级启停随频道走，没有频道行的按前缀改。

以及最要紧的那条：**同名的新实例重新认领后读不到旧配置**（实例标识是可以被重新认领的 slug）。

## 环境

内存 SQLite；插件设置的落盘目录由全局固件改道到临时目录（`conftest._isolated_plugin_scope_files`）；
`DATA_DIR` 也改到临时目录（令牌曝光记录、附件目录等都写在它下面）。插件键与密钥都现编。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml
from tortoise import Tortoise

from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console import tenants as tenants_router
from nekro_agent.routers.console.instances import preview_console_instance_purge
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.tenant import TenantInstanceBindRequest, TenantRole
from nekro_agent.services import chat_key_migration
from nekro_agent.services.console import instance_manager
from nekro_agent.services.plugin import scope as scope_store
from nekro_agent.services.plugin import scope_lifecycle
from nekro_agent.services.plugin.scope import OverrideScope
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OTHER = "inst2"
TARGET = "inst3"
SECRET_PLUGIN = "test_scope.secret_probe"
SWITCH_PLUGIN = "test_scope.switch_probe"
OLD_SECRET = "old-owner-key-" + "q7f"
TARGET_SECRET = "target-key-" + "m2c"


class SecretProbeConfig(ConfigBase):
    API_KEY: str = "global-key"
    ENDPOINT: str = "global-endpoint"


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
    """向量库不参与本契约"""

    async def _noop(_instance_key: str) -> None:
        return None

    monkeypatch.setattr(instance_manager, "purge_instance_vectors", _noop)


@pytest.fixture
def offline_qdrant(monkeypatch: pytest.MonkeyPatch) -> None:
    """实例迁移会同步段落向量；这里按「全部同步成功」处理"""
    from nekro_agent.services.memory import qdrant_manager as qdrant_module

    async def fake_set(paragraph_ids: List[int], _instance_key: str) -> int:
        return len(paragraph_ids)

    async def fake_payload(paragraph_ids: List[int], _payload: Dict[str, Any]) -> int:
        return len(paragraph_ids)

    monkeypatch.setattr(qdrant_module.memory_qdrant_manager, "set_paragraph_instance_key", fake_set)
    monkeypatch.setattr(qdrant_module.memory_qdrant_manager, "set_paragraph_payload", fake_payload)


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


async def make_instance(instance_key: str, tenant_id: Optional[int] = None) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=instance_key,
        status="online",
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


def seed_settings(instance_key: str, chat_keys: List[str], secret: str = OLD_SECRET) -> None:
    """实例级启停 + 每个频道上的启停 + 实例级配置目录（里面带一个插件自己放的文件）"""
    scope_store.set_scope_override(OverrideScope.INSTANCE, instance_key, SECRET_PLUGIN, True)
    for key in chat_keys:
        scope_store.set_scope_override(OverrideScope.SESSION, key, SWITCH_PLUGIN, True)
    scope_store.save_instance_config_overrides(SECRET_PLUGIN, instance_key, {"API_KEY": secret})


def instance_dir(instance_key: str, plugin_key: str = SECRET_PLUGIN) -> Path:
    return scope_store.PLUGIN_DATA_DIR / plugin_key / "instances" / instance_key


def reads_as_fresh(instance_key: str, chat_keys: List[str]) -> None:
    """一个刚认领这个标识的实例看到的应该是空白：没有启停覆盖、没有实例级配置、合并结果就是全局"""
    assert scope_store.get_scope_overrides(OverrideScope.INSTANCE, instance_key) == {}
    for key in chat_keys:
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, key) == {}
        assert not scope_store.is_plugin_active(SWITCH_PLUGIN, instance_key, key)
    assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, instance_key) == {}
    global_config = SecretProbeConfig()
    resolved = scope_store.resolve_scoped_config(SECRET_PLUGIN, SecretProbeConfig, global_config, instance_key)
    assert resolved is global_config
    assert not instance_dir(instance_key).exists()


# ---------------------------------------------------------------------------
# 彻底删除实例
# ---------------------------------------------------------------------------


class TestPurgeInstance:
    async def test_all_three_kinds_of_settings_go_and_the_neighbour_keeps_its_own(self, db, no_vectors) -> None:  # noqa: ARG002
        instance = await make_instance(INSTANCE)
        await make_instance(OTHER)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        # 频道行早已不在、开关还留着的那一种：按前缀认出来
        orphan = chat_key(INSTANCE, "group_gone")
        neighbour = (await make_channel(OTHER, "group_2")).chat_key
        seed_settings(INSTANCE, [own, orphan])
        seed_settings(OTHER, [neighbour], secret=TARGET_SECRET)
        (instance_dir(INSTANCE) / "cache.bin").write_bytes(b"x")

        counts = await instance_manager.purge_instance(instance)

        assert counts["plugin_settings"] == 2
        reads_as_fresh(INSTANCE, [own, orphan])
        assert scope_store.get_scope_overrides(OverrideScope.INSTANCE, OTHER) == {SECRET_PLUGIN: True}
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, neighbour) == {SWITCH_PLUGIN: True}
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, OTHER) == {"API_KEY": TARGET_SECRET}

    async def test_a_reclaimed_name_reads_nothing_of_the_old_owner(self, db, no_vectors) -> None:  # noqa: ARG002
        old_tenant = await make_tenant("old-owner")
        new_tenant = await make_tenant("new-owner")
        instance = await make_instance(INSTANCE, old_tenant.id)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        seed_settings(INSTANCE, [own])
        # 先让合并结果进缓存：删完之后缓存也必须失效，否则同一进程里新实例照样读到旧密钥
        merged = scope_store.resolve_scoped_config(SECRET_PLUGIN, SecretProbeConfig, SecretProbeConfig(), INSTANCE)
        assert merged.API_KEY == OLD_SECRET  # type: ignore[attr-defined]

        await instance_manager.purge_instance(instance)
        await make_instance(INSTANCE, new_tenant.id)
        await make_channel(INSTANCE, "group_1")

        reads_as_fresh(INSTANCE, [own])

    async def test_the_preview_counts_plugins_not_entries(self, db) -> None:  # noqa: ARG002
        await make_instance(INSTANCE)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        second = (await make_channel(INSTANCE, "group_2")).chat_key
        seed_settings(INSTANCE, [own, second])

        preview = await preview_console_instance_purge(INSTANCE, _current_user=platform_owner())

        assert preview.plugin_settings == 2

    async def test_settings_that_cannot_be_deleted_stop_the_purge(
        self,
        db,  # noqa: ARG002
        no_vectors,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance = await make_instance(INSTANCE)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        seed_settings(INSTANCE, [own])

        def refuse(_instance_key: str) -> List[str]:
            raise OSError("busy")

        monkeypatch.setattr(scope_store, "drop_instance_config_dirs", refuse)

        with pytest.raises(ValidationError):
            await instance_manager.purge_instance(instance)

        assert await DBAdapterInstance.filter(id=instance.id).exists()
        assert await DBChatChannel.filter(chat_key=own).exists()
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, INSTANCE) == {"API_KEY": OLD_SECRET}
        # 配置目录先删、删不动就停：两份启停文件还是原样
        assert scope_store.get_scope_overrides(OverrideScope.INSTANCE, INSTANCE) == {SECRET_PLUGIN: True}
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, own) == {SWITCH_PLUGIN: True}


# ---------------------------------------------------------------------------
# 划转
# ---------------------------------------------------------------------------


class TestTenantTransfer:
    async def _world(self) -> Any:
        tenant_a = await make_tenant("tenant-a")
        tenant_b = await make_tenant("tenant-b")
        await make_instance(INSTANCE, tenant_a.id)
        await make_instance(OTHER, tenant_a.id)
        own = (await make_channel(INSTANCE, "group_1")).chat_key
        neighbour = (await make_channel(OTHER, "group_2")).chat_key
        seed_settings(INSTANCE, [own])
        seed_settings(OTHER, [neighbour], secret=TARGET_SECRET)
        return tenant_a, tenant_b, own, neighbour

    async def _bind(self, tenant: DBTenant, keys: List[str], **flags: bool) -> None:
        await tenants_router.bind_tenant_instances(
            tenant_id=tenant.id,
            body=TenantInstanceBindRequest(instance_keys=keys, **flags),
            _current_user=platform_owner(),
        )

    async def test_the_preview_reports_how_many_plugins_are_affected(self, db) -> None:  # noqa: ARG002
        _tenant_a, tenant_b, _own, _neighbour = await self._world()
        await make_instance(TARGET)

        preview = await tenants_router.preview_tenant_instances(
            tenant_id=tenant_b.id,
            body=TenantInstanceBindRequest(instance_keys=[TARGET]),
            _current_user=platform_owner(),
        )
        assert [change.plugin_settings for change in preview.changes] == [0]

        seed_settings(TARGET, [])
        preview = await tenants_router.preview_tenant_instances(
            tenant_id=tenant_b.id,
            body=TenantInstanceBindRequest(instance_keys=[TARGET]),
            _current_user=platform_owner(),
        )
        assert [change.plugin_settings for change in preview.changes] == [1]

    async def test_by_default_the_settings_are_deleted_and_the_rest_stay(self, db) -> None:  # noqa: ARG002
        tenant_a, tenant_b, own, neighbour = await self._world()

        # 收回 inst1（inst2 还留在 A 名下）：收回也是归属变更，默认删
        await self._bind(tenant_a, [OTHER])
        reads_as_fresh(INSTANCE, [own])
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, OTHER) == {"API_KEY": TARGET_SECRET}
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, neighbour) == {SWITCH_PLUGIN: True}

        # 再划给 B：B 拿到的是一个空白的 inst1
        await self._bind(tenant_b, [INSTANCE])
        assert (await DBAdapterInstance.get(instance_key=INSTANCE)).tenant_id == tenant_b.id
        reads_as_fresh(INSTANCE, [own])

    async def test_unticking_lets_the_settings_follow_the_instance(self, db) -> None:  # noqa: ARG002
        tenant_a, tenant_b, own, _neighbour = await self._world()

        await self._bind(tenant_a, [OTHER], delete_plugin_settings=False)
        await self._bind(tenant_b, [INSTANCE], delete_plugin_settings=False)

        assert (await DBAdapterInstance.get(instance_key=INSTANCE)).tenant_id == tenant_b.id
        assert scope_store.get_scope_overrides(OverrideScope.INSTANCE, INSTANCE) == {SECRET_PLUGIN: True}
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, own) == {SWITCH_PLUGIN: True}
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, INSTANCE) == {"API_KEY": OLD_SECRET}

    async def test_a_replay_without_ownership_change_deletes_nothing(self, db) -> None:  # noqa: ARG002
        tenant_a, _tenant_b, own, _neighbour = await self._world()

        await self._bind(tenant_a, [INSTANCE, OTHER])

        assert scope_store.get_scope_overrides(OverrideScope.SESSION, own) == {SWITCH_PLUGIN: True}
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, INSTANCE) == {"API_KEY": OLD_SECRET}

    async def test_a_failed_deletion_leaves_the_ownership_unchanged(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tenant_a, _tenant_b, _own, _neighbour = await self._world()

        def refuse(_instance_key: str) -> List[str]:
            raise OSError("busy")

        monkeypatch.setattr(scope_store, "drop_instance_config_dirs", refuse)

        with pytest.raises(OSError):
            await self._bind(tenant_a, [OTHER])

        assert (await DBAdapterInstance.get(instance_key=INSTANCE)).tenant_id == tenant_a.id
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, INSTANCE) == {"API_KEY": OLD_SECRET}


# ---------------------------------------------------------------------------
# 换会话标识
# ---------------------------------------------------------------------------


class TestChatKeyRename:
    async def test_channel_switches_follow_the_new_key(self, db) -> None:  # noqa: ARG002
        old = f"{ADAPTER}-group_1"
        new = chat_key(INSTANCE, "group_1")
        scope_store.set_scope_override(OverrideScope.SESSION, old, SWITCH_PLUGIN, True)
        assert scope_store.is_plugin_active(SWITCH_PLUGIN, INSTANCE, old)

        counts = await chat_key_migration.migrate_chat_key_references(old, new, instance_key=INSTANCE)

        assert counts["plugin_session_toggle"] == 1
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, old) == {}
        assert scope_store.is_plugin_active(SWITCH_PLUGIN, INSTANCE, new)

    async def test_entries_already_on_the_new_key_win(self, db) -> None:  # noqa: ARG002
        old = f"{ADAPTER}-group_1"
        new = chat_key(INSTANCE, "group_1")
        scope_store.set_scope_override(OverrideScope.SESSION, old, SWITCH_PLUGIN, True)
        scope_store.set_scope_override(OverrideScope.SESSION, old, SECRET_PLUGIN, True)
        scope_store.set_scope_override(OverrideScope.SESSION, new, SWITCH_PLUGIN, False)

        counts = await chat_key_migration.migrate_chat_key_references(old, new, instance_key=INSTANCE)

        assert counts["plugin_session_toggle"] == 1
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, new) == {
            SWITCH_PLUGIN: False,
            SECRET_PLUGIN: True,
        }
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, old) == {}

    async def test_the_file_is_what_changes_not_only_the_cache(self, db) -> None:  # noqa: ARG002
        old = f"{ADAPTER}-group_1"
        new = chat_key(INSTANCE, "group_1")
        scope_store.set_scope_override(OverrideScope.SESSION, old, SWITCH_PLUGIN, True)

        await chat_key_migration.migrate_chat_key_references(old, new, instance_key=INSTANCE)

        on_disk = yaml.safe_load((scope_store.SCOPE_OVERRIDE_DIR / "sessions.yaml").read_text(encoding="utf-8"))
        assert on_disk == {new: {SWITCH_PLUGIN: True}}


# ---------------------------------------------------------------------------
# 实例迁移
# ---------------------------------------------------------------------------


class TestInstanceMigration:
    async def test_everything_moves_to_the_target_and_the_source_name_is_blank(
        self,
        db,  # noqa: ARG002
        offline_qdrant,  # noqa: ARG002
    ) -> None:
        await make_instance(INSTANCE)
        await make_instance(TARGET)
        moved = await make_channel(INSTANCE, "group_1")
        orphan = chat_key(INSTANCE, "group_gone")
        seed_settings(INSTANCE, [moved.chat_key, orphan])

        plan = await chat_key_migration.plan_instance_migration(ADAPTER, INSTANCE, TARGET)
        outcome = await chat_key_migration.migrate_instance_ownership(ADAPTER, INSTANCE, TARGET)

        # 预览报的是源实例名下有设置的插件（实例级 + 它的频道上的）；执行结果里 `plugin_settings` 是扫尾时
        # 搬过去的实例级那部分，频道级的随逐个频道搬（计在 `plugin_session_toggle` 里）
        assert plan.plugin_settings == 2
        assert "plugin_settings" not in plan.counts
        assert outcome.counts["plugin_settings"] == 1
        assert outcome.counts["plugin_session_toggle"] == 1
        assert outcome.residual["plugin_settings"] == 0
        assert scope_store.get_scope_overrides(OverrideScope.INSTANCE, TARGET) == {SECRET_PLUGIN: True}
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, chat_key(TARGET, "group_1")) == {
            SWITCH_PLUGIN: True,
        }
        assert scope_store.get_scope_overrides(OverrideScope.SESSION, chat_key(TARGET, "group_gone")) == {
            SWITCH_PLUGIN: True,
        }
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, TARGET) == {"API_KEY": OLD_SECRET}
        reads_as_fresh(INSTANCE, [moved.chat_key, orphan])

    async def test_a_target_that_already_has_settings_keeps_them(
        self,
        db,  # noqa: ARG002
        offline_qdrant,  # noqa: ARG002
    ) -> None:
        await make_instance(INSTANCE)
        await make_instance(TARGET)
        scope_store.set_scope_override(OverrideScope.INSTANCE, INSTANCE, SECRET_PLUGIN, True)
        scope_store.set_scope_override(OverrideScope.INSTANCE, TARGET, SECRET_PLUGIN, False)
        scope_store.save_instance_config_overrides(
            SECRET_PLUGIN,
            INSTANCE,
            {"API_KEY": OLD_SECRET, "ENDPOINT": "source-endpoint"},
        )
        scope_store.save_instance_config_overrides(SECRET_PLUGIN, TARGET, {"API_KEY": TARGET_SECRET})
        (instance_dir(INSTANCE) / "notes.txt").write_text("from source", encoding="utf-8")

        await chat_key_migration.migrate_instance_ownership(ADAPTER, INSTANCE, TARGET)

        assert scope_store.get_scope_overrides(OverrideScope.INSTANCE, TARGET) == {SECRET_PLUGIN: False}
        assert scope_store.load_instance_config_overrides(SECRET_PLUGIN, TARGET) == {
            "API_KEY": TARGET_SECRET,
            "ENDPOINT": "source-endpoint",
        }
        assert (instance_dir(TARGET) / "notes.txt").read_text(encoding="utf-8") == "from source"
        reads_as_fresh(INSTANCE, [])


# ---------------------------------------------------------------------------
# 底层：只认合法标识、不碰内部目录
# ---------------------------------------------------------------------------


class TestStoreHelpers:
    def test_internal_dirs_and_bad_keys_are_never_listed(self) -> None:
        (scope_store.PLUGIN_DATA_DIR / "_overrides" / "instances" / INSTANCE).mkdir(parents=True)
        scope_store.save_instance_config_overrides(SECRET_PLUGIN, INSTANCE, {"API_KEY": OLD_SECRET})

        assert [plugin for plugin, _ in scope_store.instance_config_dirs(INSTANCE)] == [SECRET_PLUGIN]
        assert scope_store.instance_config_dirs("../" + INSTANCE) == []
        assert scope_store.instance_config_dirs("") == []

    def test_an_empty_instance_key_never_touches_instance_level_settings(self) -> None:
        scope_store.set_scope_override(OverrideScope.INSTANCE, INSTANCE, SECRET_PLUGIN, True)

        counts = scope_lifecycle.drop_instance_plugin_settings(ADAPTER, "", [])

        assert counts == {"plugin_instance_toggles": 0, "plugin_session_toggles": 0, "plugin_instance_configs": 0}
        assert scope_store.get_scope_overrides(OverrideScope.INSTANCE, INSTANCE) == {SECRET_PLUGIN: True}
