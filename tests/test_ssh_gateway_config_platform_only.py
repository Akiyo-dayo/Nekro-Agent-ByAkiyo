"""SSH 网关的系统配置只给平台方读写

所有者名单（`SSH_GATEWAY_OWNER_ACCOUNTS`）决定谁的话能让 Bot 动平台方服务器，用户白名单
（`SSH_GATEWAY_USER_ACCOUNTS`）决定谁能在聊天里登记服务器——与 `SUPER_USERS` 同一类「冒充所需的
情报」。此前这一组不在平台方专属名单里：租户写系统配置本来就是 403，但读得到。现在按前缀
`SSH_GATEWAY_` 整组登记，以后新加的网关配置自动跟上。

前端设置页的分类标签是从接口返回的配置项里现推出来的（`pages/settings/system.tsx`），后端把
这一组滤掉以后，租户在「CC 工作区」标签里就看不到它们，不需要另改前端。
"""

from __future__ import annotations

import datetime
from typing import AsyncIterator, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.schemas.errors import ConfigNotFoundError, PermissionDeniedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.role import Role

TENANT_ID = 7
SSH_KEYS = sorted(name for name in CoreConfig.model_fields if name.startswith("SSH_GATEWAY_"))


@pytest.fixture
async def db() -> AsyncIterator[None]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBTenant.create(id=TENANT_ID, name="acme", display_name="acme")
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
    """拦住真正的写盘：只记下写了什么，不碰开发机上的配置文件"""
    calls: List[tuple] = []

    def record_set(config_key: str, item_key: str, value: str) -> tuple:
        calls.append(("set", config_key, item_key, value))
        return True, ""

    def record_batch(config_key: str, configs: dict) -> tuple:
        calls.append(("batch", config_key, dict(configs)))
        return True, ""

    monkeypatch.setattr(config_router.UnifiedConfigService, "set_config_value", record_set)
    monkeypatch.setattr(config_router.UnifiedConfigService, "batch_update_config", record_batch)
    return calls


def _user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    user = DBUser(
        username=f"user-{tenant_role.value}-{perm_level}",
        password="",
        adapter_key="",
        platform_userid="",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )
    user.id = 100 + int(perm_level)
    return user


def tenant_admin() -> DBUser:
    return _user(tenant_id=TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_admin() -> DBUser:
    """平台方一侧、但不是超级管理员的账号"""
    return _user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


def test_every_ssh_gateway_key_is_platform_only() -> None:
    """按前缀整组登记：今天的 16 项都在，以后新加的也自动在；别的作用域里的同名项不算"""
    assert len(SSH_KEYS) >= 16
    assert "SSH_GATEWAY_OWNER_ACCOUNTS" in SSH_KEYS
    assert "SSH_GATEWAY_USER_ACCOUNTS" in SSH_KEYS
    for name in SSH_KEYS:
        assert config_router._is_platform_only_config("system", name), name  # noqa: SLF001
    assert config_router._is_platform_only_config("plugin_basic", "SSH_GATEWAY_ENABLED") is False  # noqa: SLF001


def _category_of(name: str) -> object:
    extra = CoreConfig.model_fields[name].json_schema_extra
    return extra.get("i18n_category") if isinstance(extra, dict) else None


async def test_tenant_does_not_see_the_group(db: None) -> None:
    """租户的系统配置列表里一项都没有（别的系统项还在）；单项读按「不存在」处理

    网关这一组所在的分类（「CC 工作区」）里没有别的项，所以设置页按返回的配置项现推分类时，
    租户那边整个标签都不会出现。
    """
    items = await config_router.get_config_list(config_key="system", _current_user=tenant_admin())
    keys = {item.key for item in items}
    assert not any(key.startswith("SSH_GATEWAY_") for key in keys)
    assert "COMMAND_ENABLED" in keys

    ssh_category = _category_of("SSH_GATEWAY_ENABLED")
    assert ssh_category, "前提：网关配置带分类"
    assert not [key for key in keys if key in CoreConfig.model_fields and _category_of(key) == ssh_category]

    for name in ("SSH_GATEWAY_OWNER_ACCOUNTS", "SSH_GATEWAY_USER_ACCOUNTS", "SSH_GATEWAY_ENABLED"):
        with pytest.raises(ConfigNotFoundError):
            await config_router.get_config_item(config_key="system", item_key=name, _current_user=tenant_admin())


async def test_tenant_cannot_write_the_group(db: None, writes: List[tuple]) -> None:
    """三条写路径都是 403，判据报的是「平台方权限边界」，一次都没落到写盘"""
    with pytest.raises(PermissionDeniedError) as excinfo:
        await config_router._assert_tenant_may_write(  # noqa: SLF001
            tenant_admin(),
            "system",
            {"SSH_GATEWAY_OWNER_ACCOUNTS": '["qq:10001"]'},
        )
    assert "平台方权限边界" in str(excinfo.value.detail)

    with pytest.raises(PermissionDeniedError):
        await config_router.set_config_value(
            config_key="system",
            item_key="SSH_GATEWAY_ENABLED",
            value="true",
            _current_user=tenant_admin(),
        )
    with pytest.raises(PermissionDeniedError):
        await config_router.batch_update_config(
            config_key="system",
            body=config_router.BatchUpdateConfig(configs={"SSH_GATEWAY_USER_ACCOUNTS": '["qq:10002"]'}),
            _current_user=tenant_admin(),
        )
    assert writes == []


async def test_platform_admin_still_reads_and_writes_the_group(db: None, writes: List[tuple]) -> None:
    items = await config_router.get_config_list(config_key="system", _current_user=platform_admin())
    keys = {item.key for item in items}
    assert set(SSH_KEYS) <= keys

    item = await config_router.get_config_item(
        config_key="system",
        item_key="SSH_GATEWAY_ENABLED",
        _current_user=platform_admin(),
    )
    assert item.value is False

    await config_router.set_config_value(
        config_key="system",
        item_key="SSH_GATEWAY_MAX_CHANNELS_PER_WORKSPACE",
        value="8",
        _current_user=platform_admin(),
    )
    assert writes == [("set", "system", "SSH_GATEWAY_MAX_CHANNELS_PER_WORKSPACE", "8")]
