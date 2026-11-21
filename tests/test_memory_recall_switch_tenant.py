"""租户管理员可以自由调整三个跨频道召回开关。

三个开关是普通可覆盖配置，不带「只能关」标记：租户在自己的实例上可以调高也可以调低，
写入后由四层解析读回生效值。对别人的实例仍然 404。
"""

from __future__ import annotations

from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.routers.config import BatchUpdateConfig
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import config_resolver as config_resolver_module
from nekro_agent.services.config_resolver import ConfigLayer
from nekro_agent.services.config_service import UnifiedConfigService
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
OWN_INSTANCE = "i1"
FOREIGN_INSTANCE = "i9"
OWN_INSTANCE_KEY = f"instance_override_{ADAPTER}__{OWN_INSTANCE}"
FOREIGN_INSTANCE_KEY = f"instance_override_{ADAPTER}__{FOREIGN_INSTANCE}"

RECALL_FIELDS = (
    "MEMORY_RECALL_GROUP_TO_GROUP",
    "MEMORY_RECALL_GROUP_TO_PRIVATE",
    "MEMORY_RECALL_PRIVATE_TO_GROUP",
)


def _tenant_admin() -> DBUser:
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


@pytest.fixture
async def route_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=OWN_INSTANCE,
        display_name="own",
        tenant_id=1,
    )
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=FOREIGN_INSTANCE,
        display_name="foreign",
        tenant_id=2,
    )

    layer = OverridableConfig()

    monkeypatch.setattr(
        UnifiedConfigService,
        "_get_config_instance",
        staticmethod(lambda config_key: layer if config_key == OWN_INSTANCE_KEY else None),
    )

    def fake_save(_config_key: str, _file_path: Any = None) -> tuple[bool, None]:
        return True, None

    monkeypatch.setattr(UnifiedConfigService, "save_config", staticmethod(fake_save))

    async def fake_inherited(_config_key: str) -> dict[str, Any]:
        return {}

    # 权限判据只关心「写入前后该作用域的生效值」；实例层之上没有别的覆盖。
    monkeypatch.setattr(config_resolver_module.config_resolver, "inherited_values", fake_inherited)
    # 生效值读回走真实的合并逻辑，只把「实例层从哪来」换成内存里的这一份。
    monkeypatch.setattr(
        config_resolver_module.config_resolver,
        "_instance_layers",
        lambda _adapter_key, _instance_key: [(ConfigLayer.INSTANCE, layer)],
    )

    try:
        yield layer
    finally:
        await Tortoise.close_connections()


def _effective() -> CoreConfig:
    return config_resolver_module.config_resolver.get_instance_effective_config(ADAPTER, OWN_INSTANCE)


async def _batch(state: str) -> None:
    body = BatchUpdateConfig(
        configs={
            **{f"enable_{field}": "true" for field in RECALL_FIELDS},
            **{field: state for field in RECALL_FIELDS},
        },
    )
    response = await config_router.batch_update_config(
        OWN_INSTANCE_KEY,
        body,
        _current_user=_tenant_admin(),
    )
    assert response.ok is True


class TestRecallSwitchTenant:
    async def test_tenant_can_raise_to_all_then_lower_to_related(self, route_env: OverridableConfig) -> None:
        await _batch("all")
        effective = _effective()
        assert [getattr(effective, field) for field in RECALL_FIELDS] == ["all", "all", "all"]
        assert layer_overrides(route_env) == "all"

        # 再调回相关：三个 `/set` 路径逐个写入，值同样生效。
        for field in RECALL_FIELDS:
            await config_router.set_config_value(
                OWN_INSTANCE_KEY,
                field,
                "related",
                _current_user=_tenant_admin(),
            )
        effective = _effective()
        assert [getattr(effective, field) for field in RECALL_FIELDS] == ["related", "related", "related"]
        assert layer_overrides(route_env) == "related"

    async def test_foreign_instance_is_not_found(self, route_env: OverridableConfig) -> None:
        body = BatchUpdateConfig(configs={"MEMORY_RECALL_GROUP_TO_GROUP": "all"})
        with pytest.raises(NotFoundError):
            await config_router.batch_update_config(
                FOREIGN_INSTANCE_KEY,
                body,
                _current_user=_tenant_admin(),
            )


def layer_overrides(layer: OverridableConfig) -> str:
    values = {getattr(layer, field) for field in RECALL_FIELDS}
    assert len(values) == 1, "三个开关在本用例里被一起写成同一个值"
    return str(values.pop())
