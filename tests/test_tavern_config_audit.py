"""配置写入口的变更审计

五个写入口（`/set`、`/batch`、`/save`、`/reload`、实例覆盖写入）都要在写成功后比较
「这一层写入前后的原始值」，有变化才写一行审计。这里跑真路由、真表：层与归属、旧值
摘要、`inherit`、被拒写入与审计自身失败的影响面都要钉住。

`/save` 与 `/reload` 不提交取值，变化发生在内存与磁盘之间：`/save` 的对比基准是文件，
`/reload` 的对比基准是内存。两者各有一条用例，避免以后有人把它们统一成「前后都读内存」，
那会让这两条路径永远写不出审计。

审计里只允许出现长度与摘要。`get_sub_logger` 回的是 loguru，`caplog` 抓不到，WARNING
那一条要用真正的 loguru sink 抓（见 `_capture_warnings`）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_config_audit import DBConfigChangeAudit
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.config_service import ConfigService, UnifiedConfigService
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
OWN_INSTANCE = "t1a"
OTHER_INSTANCE = "t2a"
CHAT_KEY = "onebot_v11-group_1"
INSTANCE_CONFIG_KEY = f"instance_override_{ADAPTER}__{OWN_INSTANCE}"
OTHER_INSTANCE_CONFIG_KEY = f"instance_override_{ADAPTER}__{OTHER_INSTANCE}"
ADAPTER_CONFIG_KEY = f"adapter_override_{ADAPTER}"
CHANNEL_CONFIG_KEY = f"channel_config_{CHAT_KEY}"

SECRET_TEXT = "只该以长度与摘要出现的一段文字"


def _platform_user() -> DBUser:
    return DBUser(
        id=1,
        username="platform",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def _tenant_user() -> DBUser:
    return DBUser(
        id=21,
        username="tenant-admin",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        tenant_id=1,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


def _capture_warnings():
    """挂一个真正的 loguru sink；产品代码的 logger 不走 stdlib logging"""
    from loguru import logger

    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="WARNING")
    return captured, sink_id


class _Harness:
    """把配置服务换成内存层，落盘仍是真的 YAML（`/save`、`/reload` 需要磁盘那份）"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.layers: dict[str, CoreConfig | OverridableConfig] = {}

        monkeypatch.setattr(UnifiedConfigService, "_get_config_instance", staticmethod(self.get))
        monkeypatch.setattr(UnifiedConfigService, "set_config_value", staticmethod(self.set_value))
        monkeypatch.setattr(UnifiedConfigService, "batch_update_config", staticmethod(self.batch_update))
        monkeypatch.setattr(UnifiedConfigService, "save_config", staticmethod(self.save))
        monkeypatch.setattr(UnifiedConfigService, "reload_config", staticmethod(self.reload))

    def path_of(self, config_key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", config_key)
        return self.tmp_path / f"{safe}.yaml"

    def get(self, config_key: str):
        layer = self.layers.get(config_key)
        if layer is None:
            layer = CoreConfig() if config_key == "system" else OverridableConfig()
            layer.set_instance_config_file_path(self.path_of(config_key))
            self.layers[config_key] = layer
        return layer

    def set_value(self, config_key: str, item_key: str, value: str):
        return ConfigService.set_config_value(self.get(config_key), item_key, value)

    def batch_update(self, config_key: str, configs: dict[str, str]):
        return ConfigService.batch_update_config(self.get(config_key), configs)

    def save(self, config_key: str, file_path=None):
        return ConfigService.save_config(self.get(config_key), file_path)

    def reload(self, config_key: str, file_path=None):
        layer = self.get(config_key)
        target = file_path or layer.get_config_file_path()
        assert target is not None
        self.layers[config_key] = type(layer).load_from_path(target)
        return True, None

    def prepare_disk(self, config_key: str, values: dict[str, object]) -> None:
        """造一份「磁盘上的这一层」，不经过内存层"""
        layer = OverridableConfig()
        layer.set_instance_config_file_path(self.path_of(config_key))
        for key, value in values.items():
            setattr(layer, key, value)
        layer.dump_config()


@pytest.fixture
async def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    for instance_key, tenant_id in ((OWN_INSTANCE, 1), (OTHER_INSTANCE, 2)):
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )
    await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=OWN_INSTANCE,
        channel_id="1",
        chat_key=CHAT_KEY,
        channel_type="group",
        data="{}",
    )
    harness = _Harness(tmp_path, monkeypatch)
    try:
        yield harness
    finally:
        await Tortoise.close_connections()


async def _rows() -> list[DBConfigChangeAudit]:
    return await DBConfigChangeAudit.all().order_by("id")


class TestEveryWriteEntryAudits:
    async def test_set_system_layer(self, world: _Harness) -> None:
        response = await config_router.set_config_value(
            "system",
            "PROMPT_ADVANCED_SLOTS_ENABLED",
            "true",
            _current_user=_platform_user(),
        )
        assert response.ok is True

        rows = await _rows()
        assert len(rows) == 1
        row = rows[0]
        assert row.layer == "system"
        assert row.field == "PROMPT_ADVANCED_SLOTS_ENABLED"
        assert row.old_value == "false"
        assert row.new_value == "true"
        assert row.operator == "platform"
        assert row.operator_is_platform is True
        assert row.adapter_key == ""
        assert row.instance_key == ""
        assert row.chat_key == ""

    async def test_batch_instance_layer_as_tenant(self, world: _Harness) -> None:
        captured, sink_id = _capture_warnings()
        try:
            response = await config_router.batch_update_config(
                INSTANCE_CONFIG_KEY,
                config_router.BatchUpdateConfig(
                    configs={
                        "enable_PROMPT_POST_HISTORY": "true",
                        "PROMPT_POST_HISTORY": SECRET_TEXT,
                    },
                ),
                _current_user=_tenant_user(),
            )
        finally:
            from loguru import logger

            logger.remove(sink_id)
        assert response.ok is True

        rows = await _rows()
        assert len(rows) == 1
        row = rows[0]
        assert row.layer == "instance"
        assert row.adapter_key == ADAPTER
        assert row.instance_key == OWN_INSTANCE
        assert row.chat_key == ""
        assert row.operator == "tenant-admin"
        assert row.operator_is_platform is False
        assert row.field == "PROMPT_POST_HISTORY"
        assert row.old_value == "inherit"
        assert row.new_value.startswith("len=")
        assert SECRET_TEXT not in row.old_value
        assert SECRET_TEXT not in row.new_value
        # WARNING 里也不能有原文
        assert any("配置变更" in line for line in captured)
        assert all(SECRET_TEXT not in line for line in captured)

    async def test_put_instance_layer(self, world: _Harness) -> None:
        response = await config_router.update_instance_config(
            ADAPTER,
            OWN_INSTANCE,
            config_router.BatchUpdateConfig(
                configs={"enable_PROMPT_PREFILL": "true", "PROMPT_PREFILL": SECRET_TEXT},
            ),
            _current_user=_platform_user(),
        )
        assert response.ok is True

        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].layer == "instance"
        assert rows[0].field == "PROMPT_PREFILL"
        assert rows[0].old_value == "inherit"
        assert rows[0].new_value.startswith("len=")

    async def test_save_channel_layer_compares_disk_to_memory(self, world: _Harness) -> None:
        world.prepare_disk(
            CHANNEL_CONFIG_KEY,
            {"enable_PROMPT_PREFILL": True, "PROMPT_PREFILL": SECRET_TEXT},
        )
        # 内存层是新建的空层（没有覆盖）；`/save` 会用内存覆盖磁盘，这本身就是一次变更
        response = await config_router.save_config_api(CHANNEL_CONFIG_KEY, _current_user=_platform_user())
        assert response.ok is True

        rows = await _rows()
        assert len(rows) == 1
        row = rows[0]
        assert row.layer == "channel"
        assert row.chat_key == CHAT_KEY
        assert row.adapter_key == ADAPTER
        assert row.instance_key == OWN_INSTANCE
        assert row.field == "PROMPT_PREFILL"
        assert row.old_value.startswith("len=")
        assert row.new_value == "inherit"
        assert SECRET_TEXT not in row.old_value

    async def test_reload_adapter_layer_compares_memory_to_disk(self, world: _Harness) -> None:
        world.prepare_disk(
            ADAPTER_CONFIG_KEY,
            {"enable_PROMPT_PREFILL": True, "PROMPT_PREFILL": SECRET_TEXT},
        )
        response = await config_router.reload_config_api(ADAPTER_CONFIG_KEY, _current_user=_platform_user())
        assert response.ok is True

        rows = await _rows()
        assert len(rows) == 1
        row = rows[0]
        assert row.layer == "adapter"
        assert row.adapter_key == ADAPTER
        assert row.instance_key == ""
        assert row.chat_key == ""
        assert row.old_value == "inherit"
        assert row.new_value.startswith("len=")
        assert SECRET_TEXT not in row.new_value


class TestWhatMustNotBeAudited:
    async def test_unchanged_value_writes_nothing(self, world: _Harness) -> None:
        user = _platform_user()
        for _ in range(2):
            await config_router.set_config_value(
                "system",
                "PROMPT_ADVANCED_SLOTS_ENABLED",
                "true",
                _current_user=user,
            )
        assert len(await _rows()) == 1

    async def test_field_without_audit_flag_writes_nothing(self, world: _Harness) -> None:
        await config_router.set_config_value(
            "system",
            "TAVERN_CARD_TEMPLATE",
            "改过的模板",
            _current_user=_platform_user(),
        )
        assert await _rows() == []

    async def test_clearing_an_override_is_recorded_as_inherit(self, world: _Harness) -> None:
        user = _platform_user()
        await config_router.batch_update_config(
            INSTANCE_CONFIG_KEY,
            config_router.BatchUpdateConfig(
                configs={"enable_PROMPT_PREFILL": "true", "PROMPT_PREFILL": SECRET_TEXT},
            ),
            _current_user=user,
        )
        await DBConfigChangeAudit.all().delete()

        await config_router.set_config_value(
            INSTANCE_CONFIG_KEY,
            "enable_PROMPT_PREFILL",
            "false",
            _current_user=user,
        )
        rows = await _rows()
        assert len(rows) == 1
        assert rows[0].old_value.startswith("len=")
        assert rows[0].new_value == "inherit"

    async def test_rejected_write_writes_nothing(self, world: _Harness) -> None:
        with pytest.raises(NotFoundError):
            await config_router.set_config_value(
                OTHER_INSTANCE_CONFIG_KEY,
                "PROMPT_PREFILL",
                SECRET_TEXT,
                _current_user=_tenant_user(),
            )
        assert await _rows() == []

    async def test_audit_write_failure_does_not_fail_the_write(
        self,
        world: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def boom(**_kwargs):
            raise RuntimeError("审计库抖动")

        monkeypatch.setattr(DBConfigChangeAudit, "create", classmethod(boom))
        captured, sink_id = _capture_warnings()
        try:
            response = await config_router.set_config_value(
                "system",
                "PROMPT_ADVANCED_SLOTS_ENABLED",
                "true",
                _current_user=_platform_user(),
            )
        finally:
            from loguru import logger

            logger.remove(sink_id)

        assert response.ok is True
        assert world.get("system").PROMPT_ADVANCED_SLOTS_ENABLED is True
        assert any("审计写入失败" in line for line in captured)
