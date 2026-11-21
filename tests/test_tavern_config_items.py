"""酒馆兼容与高级提示词槽位的配置项契约

七项配置的默认值、分类、可覆盖与审计 / 确认标记都要钉住：这些标记决定前端会不会
显示覆盖开关、后端写入口会不会写审计。三个槽位出厂必须是空串——NA 不内置任何
破限文字，默认值或说明里出现示例文字，等于把一个提示词模板塞给了所有部署。

`PROMPT_ADVANCED_SLOTS_ENABLED` 特别不能标 `tenant_may_only_lower`：高级槽位不是一次
平台方才能批的能力升级，租户管理员要在自己的实例 / 频道上能打开；但仍不能写别人的实例。
"""

from __future__ import annotations

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.config_service import ConfigService, UnifiedConfigService
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
OWN_INSTANCE = "t1a"
OTHER_INSTANCE = "t2a"

TAVERN_CATEGORY = {"zh-CN": "酒馆兼容", "en-US": "Tavern Compatibility"}
SLOT_CATEGORY = {"zh-CN": "高级提示词槽位", "en-US": "Advanced Prompt Slots"}


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


def _extra(name: str) -> dict:
    extra = CoreConfig.model_fields[name].json_schema_extra
    assert isinstance(extra, dict)
    return extra


class _MemoryLayer:
    """一个不落盘的覆盖层，按真实 `ConfigService` 的取值转换写入"""

    def __init__(self) -> None:
        self.layer = OverridableConfig()

    def get(self, _config_key: str) -> OverridableConfig:
        return self.layer


@pytest.fixture
async def layer(monkeypatch: pytest.MonkeyPatch):
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

    memory = _MemoryLayer()
    monkeypatch.setattr(UnifiedConfigService, "_get_config_instance", staticmethod(memory.get))

    def _set(config_key: str, item_key: str, value: str):
        return ConfigService.set_config_value(memory.get(config_key), item_key, value)

    def _batch(config_key: str, configs: dict[str, str]):
        return ConfigService.batch_update_config(memory.get(config_key), configs)

    def _save(_config_key: str, _file_path=None):
        return True, None

    monkeypatch.setattr(UnifiedConfigService, "set_config_value", staticmethod(_set))
    monkeypatch.setattr(UnifiedConfigService, "batch_update_config", staticmethod(_batch))
    monkeypatch.setattr(UnifiedConfigService, "save_config", staticmethod(_save))
    try:
        yield memory.layer
    finally:
        await Tortoise.close_connections()


def _instance_key(instance_key: str) -> str:
    return f"instance_override_{ADAPTER}__{instance_key}"


class TestDefaultsAndFlags:
    def test_tavern_compatibility_defaults(self) -> None:
        assert CoreConfig.model_fields["TAVERN_COMPAT_ENABLED"].default is True
        assert CoreConfig.model_fields["PERSONA_GREETING_ENABLED"].default is False
        assert CoreConfig.model_fields["TAVERN_TRIAL_DAILY_LIMIT"].default == 100

        assert _extra("TAVERN_COMPAT_ENABLED")["i18n_category"] == TAVERN_CATEGORY
        assert _extra("PERSONA_GREETING_ENABLED")["i18n_category"] == TAVERN_CATEGORY
        assert _extra("TAVERN_TRIAL_DAILY_LIMIT")["i18n_category"] == TAVERN_CATEGORY

        # 回滚闸与试发上限是平台方全局项；开场白已废弃但保留覆盖，旧的覆盖文件照常读得进来
        assert not _extra("TAVERN_COMPAT_ENABLED").get("overridable")
        assert not _extra("TAVERN_TRIAL_DAILY_LIMIT").get("overridable")
        assert _extra("PERSONA_GREETING_ENABLED")["is_hidden"] is True
        assert _extra("PERSONA_GREETING_ENABLED")["overridable"] is True
        assert not _extra("PERSONA_GREETING_ENABLED").get("tenant_may_only_lower")

        assert CoreConfig(TAVERN_TRIAL_DAILY_LIMIT=0).TAVERN_TRIAL_DAILY_LIMIT == 0
        with pytest.raises(ValueError):
            CoreConfig(TAVERN_TRIAL_DAILY_LIMIT=-1)
        with pytest.raises(ValueError):
            CoreConfig(TAVERN_TRIAL_DAILY_LIMIT=100001)

    def test_advanced_slots_defaults(self) -> None:
        assert CoreConfig.model_fields["PROMPT_ADVANCED_SLOTS_ENABLED"].default is False
        for name in ("PROMPT_POST_HISTORY", "PROMPT_AUXILIARY", "PROMPT_PREFILL"):
            assert CoreConfig.model_fields[name].default == ""
            extra = _extra(name)
            assert extra["i18n_category"] == SLOT_CATEGORY
            assert extra["overridable"] is True
            assert extra["is_textarea"] is True
            assert extra["audit_changes"] is True
            assert extra["enable_confirm"] is None
            assert extra["i18n_title"]["zh-CN"]
            assert extra["i18n_title"]["en-US"]
            assert extra["i18n_description"]["zh-CN"]
            assert extra["i18n_description"]["en-US"]

        enabled = _extra("PROMPT_ADVANCED_SLOTS_ENABLED")
        assert enabled["i18n_category"] == SLOT_CATEGORY
        assert enabled["overridable"] is True
        assert enabled["audit_changes"] is True
        assert enabled["enable_confirm"]["zh-CN"]
        assert enabled["enable_confirm"]["en-US"]
        # 租户能在自己的实例覆盖层打开它，所以不能带「只能调低 / 只能由平台方开」
        assert not enabled.get("tenant_may_only_lower")

    def test_both_categories_have_bilingual_titles(self) -> None:
        for name in (
            "TAVERN_COMPAT_ENABLED",
            "PERSONA_GREETING_ENABLED",
            "TAVERN_TRIAL_DAILY_LIMIT",
            "PROMPT_ADVANCED_SLOTS_ENABLED",
            "PROMPT_POST_HISTORY",
            "PROMPT_AUXILIARY",
            "PROMPT_PREFILL",
        ):
            extra = _extra(name)
            assert extra["i18n_title"]["zh-CN"]
            assert extra["i18n_title"]["en-US"]
            assert extra["i18n_description"]["zh-CN"]
            assert extra["i18n_description"]["en-US"]

    def test_override_model_contains_only_the_overridable_items(self) -> None:
        """`不可覆盖` 不能只写在说明里：覆盖模型里根本不该生成对应字段"""
        fields = OverridableConfig.model_fields
        assert "enable_PERSONA_GREETING_ENABLED" in fields
        assert "enable_PROMPT_ADVANCED_SLOTS_ENABLED" in fields
        assert "enable_PROMPT_POST_HISTORY" in fields
        assert "enable_PROMPT_AUXILIARY" in fields
        assert "enable_PROMPT_PREFILL" in fields
        assert "enable_TAVERN_COMPAT_ENABLED" not in fields
        assert "enable_TAVERN_TRIAL_DAILY_LIMIT" not in fields

    def test_extra_field_new_flags_default_off(self) -> None:
        """没设过的字段导出的 schema 里两个新标记必须是默认值，不能影响老配置项"""
        from nekro_agent.core.core_utils import ExtraField

        plain = ExtraField(title="普通项").model_dump()
        assert plain["audit_changes"] is False
        assert plain["enable_confirm"] is None


class TestTenantCanOpenOwnInstance:
    async def test_own_instance_override_can_be_opened(self, layer: OverridableConfig) -> None:
        response = await config_router.batch_update_config(
            _instance_key(OWN_INSTANCE),
            config_router.BatchUpdateConfig(
                configs={
                    "enable_PROMPT_ADVANCED_SLOTS_ENABLED": "true",
                    "PROMPT_ADVANCED_SLOTS_ENABLED": "true",
                },
            ),
            _current_user=_tenant_user(),
        )
        assert response.ok is True
        assert layer.enable_PROMPT_ADVANCED_SLOTS_ENABLED is True
        assert layer.PROMPT_ADVANCED_SLOTS_ENABLED is True

    async def test_someone_elses_instance_is_404(self, layer: OverridableConfig) -> None:
        with pytest.raises(NotFoundError):
            await config_router.batch_update_config(
                _instance_key(OTHER_INSTANCE),
                config_router.BatchUpdateConfig(
                    configs={
                        "enable_PROMPT_ADVANCED_SLOTS_ENABLED": "true",
                        "PROMPT_ADVANCED_SLOTS_ENABLED": "true",
                    },
                ),
                _current_user=_tenant_user(),
            )
        assert layer.enable_PROMPT_ADVANCED_SLOTS_ENABLED is False

    async def test_platform_may_open_a_slot_on_an_instance(self, layer: OverridableConfig) -> None:
        response = await config_router.update_instance_config(
            ADAPTER,
            OWN_INSTANCE,
            config_router.BatchUpdateConfig(
                configs={
                    "enable_PROMPT_PREFILL": "true",
                    "PROMPT_PREFILL": "开头",
                },
            ),
            _current_user=_platform_user(),
        )
        assert response.ok is True
        assert layer.enable_PROMPT_PREFILL is True
        assert layer.PROMPT_PREFILL == "开头"
