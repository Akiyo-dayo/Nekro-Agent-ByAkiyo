"""导入酒馆人设卡时顺带打开高级提示词槽位

卡里有后置指令、导入目标实例上 `PROMPT_ADVANCED_SLOTS_ENABLED` 的生效值是关时，导入检查
告诉前端「可以顺带打开」，并给出确认文案、频道级覆盖单独关着的频道；导入时选「打开并导入」
就在同一次导入里把这个实例的槽位打开（实例级覆盖，走通用配置写入口，写审计）。

钉住的：
- 检查：能不能顺带打开、全局开没开、确认文案与配置表单同一份、频道级覆盖关着的频道列出来；
  已经开着、没有目标实例（全局视图）时不提供；
- 导入：打开并导入（实例层写上、审计有一行）、只导入（不动）、租户越权（404，什么都没写）、
  频道覆盖关着（照样打开实例级，结果里列出那些频道）、卡里没有后置指令或取消了勾选（不动）、
  打开失败（人设照样留着，结果写明没打开）、本来就开着（不重复写）。

配置服务换成内存层（与 `test_tavern_config_audit.py` 同一做法），人设与审计跑真表。
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
from starlette.datastructures import UploadFile
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig, config
from nekro_agent.core.core_utils import ConfigManager
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_config_audit import DBConfigChangeAudit
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.routers import tavern
from nekro_agent.schemas.errors import NotFoundError, OperationFailedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.config_service import ConfigService, UnifiedConfigService
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
OWN = "t1a"
OTHER = "t2a"
CHAT_KEY = "onebot_v11-group_1"
OWN_KEY = f"instance_override_{ADAPTER}__{OWN}"
OTHER_KEY = f"instance_override_{ADAPTER}__{OTHER}"
CHANNEL_KEY = f"channel_config_{CHAT_KEY}"
SLOTS = "PROMPT_ADVANCED_SLOTS_ENABLED"


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


def _card(*, post_history: str = "后置指令正文") -> dict[str, Any]:
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "导入角色",
            "description": "描述",
            "personality": "性格",
            "scenario": "场景",
            "first_mes": "开场",
            "mes_example": "示例",
            "system_prompt": "系统提示",
            "post_history_instructions": post_history,
            "extensions": {},
        },
    }


def _upload(card: dict[str, Any] | None = None) -> UploadFile:
    return UploadFile(file=io.BytesIO(json.dumps(card or _card()).encode("utf-8")), filename="card.json")


class _Harness:
    """配置服务换成内存层；保存照常写到临时目录"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.layers: dict[str, Any] = {}
        monkeypatch.setattr(UnifiedConfigService, "_get_config_instance", staticmethod(self.get))
        monkeypatch.setattr(UnifiedConfigService, "batch_update_config", staticmethod(self.batch_update))
        monkeypatch.setattr(UnifiedConfigService, "save_config", staticmethod(self.save))

    def get(self, config_key: str) -> Any:
        layer = self.layers.get(config_key)
        if layer is None:
            layer = CoreConfig() if config_key == "system" else OverridableConfig()
            layer.set_instance_config_file_path(self.tmp_path / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', config_key)}.yaml")
            self.layers[config_key] = layer
        return layer

    def batch_update(self, config_key: str, configs: dict[str, str]) -> Any:
        return ConfigService.batch_update_config(self.get(config_key), configs)

    def save(self, config_key: str, file_path: Any = None) -> Any:
        return ConfigService.save_config(self.get(config_key), file_path)

    def slots_of(self, config_key: str) -> tuple[bool, bool]:
        layer = self.get(config_key)
        return bool(getattr(layer, f"enable_{SLOTS}")), bool(getattr(layer, SLOTS))


@pytest.fixture
async def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, SLOTS, False)
    # 实例层是否存在按磁盘目录判；这里的实例层都在内存里，直接当「有实例层」（与 test_tavern_slots 同法）
    monkeypatch.setattr(config_resolver, "_has_instance_layer", lambda _adapter_key: True)
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    for instance_key, tenant_id in ((OWN, 1), (OTHER, 2)):
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
        instance_key=OWN,
        channel_id="1",
        chat_key=CHAT_KEY,
        channel_type="group",
        channel_name="群一",
        data="{}",
    )
    harness = _Harness(tmp_path, monkeypatch)
    try:
        yield harness
    finally:
        ConfigManager.unregister_config(CHANNEL_KEY)
        await Tortoise.close_connections()


def _channel_keeps_slots_off() -> None:
    """这个频道用频道级覆盖把槽位单独关着（已加载进内存的那一层）"""
    layer = OverridableConfig()
    setattr(layer, f"enable_{SLOTS}", True)
    setattr(layer, SLOTS, False)
    ConfigManager.register_config(CHANNEL_KEY, layer)


async def _import(user: DBUser, instance_key: str, *, enable: bool, **options: Any) -> Any:
    return await tavern.import_card_endpoint(
        file=_upload(options.pop("card", None)),
        options=json.dumps({"enable_advanced_slots": enable, **options}),
        instance_key=instance_key,
        adapter_key=ADAPTER if instance_key else "",
        _current_user=user,
    )


async def _slot_audit_rows() -> list[DBConfigChangeAudit]:
    return await DBConfigChangeAudit.filter(field=SLOTS).order_by("id")


class TestInspect:
    async def test_offers_to_enable_when_the_instance_has_it_off(self, world: _Harness) -> None:
        report = await tavern.inspect_card_endpoint(
            file=_upload(),
            instance_key=OWN,
            adapter_key=ADAPTER,
            _current_user=_tenant_user(),
        )

        gates = report.target
        assert gates.advanced_slots_enabled is False
        assert gates.can_enable_advanced_slots is True
        assert gates.advanced_slots_global_enabled is False
        assert gates.advanced_slots_off_channels == []
        # 与配置表单打开这一项时弹的确认文案是同一份
        expected = CoreConfig.model_fields[SLOTS].json_schema_extra["enable_confirm"]  # type: ignore[index]
        assert gates.advanced_slots_enable_confirm == expected
        assert "由管理员负责" in gates.advanced_slots_enable_confirm["zh-CN"]

    async def test_lists_channels_that_keep_it_off(self, world: _Harness) -> None:
        _channel_keeps_slots_off()

        report = await tavern.inspect_card_endpoint(
            file=_upload(),
            instance_key=OWN,
            adapter_key=ADAPTER,
            _current_user=_tenant_user(),
        )

        assert [(item.chat_key, item.channel_name) for item in report.target.advanced_slots_off_channels] == [
            (CHAT_KEY, "群一")
        ]

    async def test_no_offer_when_the_instance_already_has_it_on(self, world: _Harness) -> None:
        world.batch_update(OWN_KEY, {f"enable_{SLOTS}": "true", SLOTS: "true"})

        report = await tavern.inspect_card_endpoint(
            file=_upload(),
            instance_key=OWN,
            adapter_key=ADAPTER,
            _current_user=_tenant_user(),
        )

        assert report.target.advanced_slots_enabled is True
        assert report.target.can_enable_advanced_slots is False

    async def test_no_offer_in_the_global_view(self, world: _Harness) -> None:
        """全局视图导入全局人设：要改的是全局开关、影响所有实例，不在导入确认里顺手做"""
        report = await tavern.inspect_card_endpoint(
            file=_upload(),
            instance_key="",
            adapter_key="",
            _current_user=_platform_user(),
        )

        assert report.target.can_enable_advanced_slots is False


class TestImport:
    async def test_enable_and_import_turns_the_instance_on_and_audits(self, world: _Harness) -> None:
        result = await _import(_tenant_user(), OWN, enable=True)

        assert result.advanced_slots == "enabled"
        assert await DBPreset.filter(id=result.preset_id).exists()
        assert world.slots_of(OWN_KEY) == (True, True)
        rows = await _slot_audit_rows()
        assert len(rows) == 1
        assert (rows[0].layer, rows[0].adapter_key, rows[0].instance_key) == ("instance", ADAPTER, OWN)
        assert rows[0].operator == "tenant-admin"

    async def test_import_only_leaves_it_off(self, world: _Harness) -> None:
        result = await _import(_tenant_user(), OWN, enable=False)

        assert result.advanced_slots == "not_requested"
        assert world.slots_of(OWN_KEY) == (False, False)
        assert await _slot_audit_rows() == []

    async def test_tenant_cannot_import_into_another_tenants_instance(self, world: _Harness) -> None:
        before = await DBPreset.all().count()

        with pytest.raises(NotFoundError):
            await _import(_tenant_user(), OTHER, enable=True)

        assert await DBPreset.all().count() == before
        assert world.slots_of(OTHER_KEY) == (False, False)
        assert await _slot_audit_rows() == []

    async def test_channels_that_keep_it_off_are_reported(self, world: _Harness) -> None:
        _channel_keeps_slots_off()

        result = await _import(_tenant_user(), OWN, enable=True)

        assert result.advanced_slots == "enabled"
        assert world.slots_of(OWN_KEY) == (True, True)
        assert [item.chat_key for item in result.advanced_slots_off_channels] == [CHAT_KEY]

    async def test_card_without_post_history_does_not_touch_the_switch(self, world: _Harness) -> None:
        result = await _import(_tenant_user(), OWN, enable=True, card=_card(post_history=""))

        assert result.advanced_slots == "not_needed"
        assert world.slots_of(OWN_KEY) == (False, False)
        assert await _slot_audit_rows() == []

    async def test_unchecked_post_history_does_not_touch_the_switch(self, world: _Harness) -> None:
        result = await _import(_tenant_user(), OWN, enable=True, include_post_history=False)

        assert result.advanced_slots == "not_needed"
        assert world.slots_of(OWN_KEY) == (False, False)

    async def test_failure_to_enable_keeps_the_imported_preset(
        self,
        world: _Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise OperationFailedError(operation="保存配置")

        monkeypatch.setattr(config_router, "batch_update_config", broken)

        result = await _import(_tenant_user(), OWN, enable=True)

        assert result.advanced_slots == "failed"
        assert "保存配置失败" in result.advanced_slots_error
        assert any("人设已导入，但高级提示词槽位没打开" in note for note in result.notes)
        assert await DBPreset.filter(id=result.preset_id).exists()
        assert world.slots_of(OWN_KEY) == (False, False)

    async def test_already_on_is_not_written_again(self, world: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, SLOTS, True)

        result = await _import(_tenant_user(), OWN, enable=True)

        assert result.advanced_slots == "already_enabled"
        assert world.slots_of(OWN_KEY) == (False, False)
        assert await _slot_audit_rows() == []
