"""`WEBUI_ADMIN_AS_COMMAND_SUPERUSER` 的判据

上游只看网页用户 ID 的 `admin_` 前缀，于是任何能登录 WebUI 的 `Role.Admin`（本分支里
租户管理员正好就是它）在网页聊天中都能执行超级用户命令。这里把条件收紧到账号本身：
存在、启用、`is_platform_owner`、`Role.Super`，四条缺一不可。
"""

from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.web.adapter import WebAdapter
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.role import Role

TENANT_ID = 1


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _make_user(
    *,
    user_id: int,
    tenant_id: Optional[int],
    tenant_role: TenantRole,
    perm_level: Role,
    banned: bool = False,
) -> DBUser:
    """`is_active` 是"未封禁"的派生属性，封禁靠 `ban_until`"""
    return await DBUser.create(
        id=user_id,
        username=f"user-{user_id}",
        password="",
        adapter_key="webui",
        platform_userid=str(user_id),
        perm_level=perm_level,
        login_time=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
        ban_until=datetime(2999, 1, 1, tzinfo=timezone.utc) if banned else None,
    )


async def test_platform_super_admin_is_promoted(db) -> None:  # noqa: ARG001
    await _make_user(
        user_id=1,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER,
        perm_level=Role.Super,
    )

    assert await WebAdapter()._is_platform_super_web_user("admin_1") is True


async def test_platform_admin_without_super_role_is_not_promoted(db) -> None:  # noqa: ARG001
    """平台方的 `Role.Admin` 账号不够：超级用户命令能跨频道批量操作"""
    await _make_user(
        user_id=2,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER,
        perm_level=Role.Admin,
    )

    assert await WebAdapter()._is_platform_super_web_user("admin_2") is False


async def test_tenant_admin_is_not_promoted(db) -> None:  # noqa: ARG001
    """租户管理员在旧体系里就是 `Role.Admin`；只看前缀的话他能在这里拿到超级用户"""
    await _make_user(
        user_id=3,
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        perm_level=Role.Admin,
    )

    assert await WebAdapter()._is_platform_super_web_user("admin_3") is False


async def test_tenant_row_with_super_level_is_not_promoted(db) -> None:  # noqa: ARG001
    """脏数据兜底：租户账号的等级被手改成 `Role.Super` 也不算——判据是平台方身份，不只是等级"""
    await _make_user(
        user_id=6,
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        perm_level=Role.Super,
    )

    assert await WebAdapter()._is_platform_super_web_user("admin_6") is False


async def test_non_ascii_digits_are_not_promoted(db) -> None:  # noqa: ARG001
    assert await WebAdapter()._is_platform_super_web_user("admin_²") is False


async def test_tenant_user_is_not_promoted(db) -> None:  # noqa: ARG001
    await _make_user(
        user_id=4,
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_USER,
        perm_level=Role.User,
    )

    assert await WebAdapter()._is_platform_super_web_user("admin_4") is False


async def test_banned_platform_super_admin_is_not_promoted(db) -> None:  # noqa: ARG001
    await _make_user(
        user_id=5,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER,
        perm_level=Role.Super,
        banned=True,
    )

    assert await WebAdapter()._is_platform_super_web_user("admin_5") is False


async def test_missing_account_is_not_promoted(db) -> None:  # noqa: ARG001
    assert await WebAdapter()._is_platform_super_web_user("admin_999") is False


@pytest.mark.parametrize("user_id", ["admin_", "admin_abc", "admin_1x", "someone_1", "", "web_agent"])
async def test_malformed_user_id_is_not_promoted(db, user_id: str) -> None:  # noqa: ARG001
    assert await WebAdapter()._is_platform_super_web_user(user_id) is False


async def test_config_switch_off_keeps_superuser_flags_unchanged(monkeypatch: pytest.MonkeyPatch, db) -> None:  # noqa: ARG001
    """开关默认关：即使账号合格，命令上下文里的两个标志也不动"""
    from nekro_agent.adapters.interface.base import BaseAdapter

    await _make_user(
        user_id=6,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER,
        perm_level=Role.Super,
    )

    adapter = WebAdapter()
    monkeypatch.setattr(type(adapter.config), "WEBUI_ADMIN_AS_COMMAND_SUPERUSER", False, raising=False)
    adapter.config.WEBUI_ADMIN_AS_COMMAND_SUPERUSER = False

    seen: dict[str, Any] = {}

    async def fake_super_execute(self, **kwargs: Any) -> list:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(BaseAdapter, "execute_command", fake_super_execute)

    await adapter.execute_command(
        chat_key="web-session_x",
        user_id="admin_6",
        username="web_user_6",
        command_name="test",
        raw_args="",
        is_super_user=False,
        is_advanced_user=False,
    )

    assert seen["is_super_user"] is False
    assert seen["is_advanced_user"] is False


async def test_config_switch_on_promotes_platform_super_admin(monkeypatch: pytest.MonkeyPatch, db) -> None:  # noqa: ARG001
    from nekro_agent.adapters.interface.base import BaseAdapter

    await _make_user(
        user_id=7,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER,
        perm_level=Role.Super,
    )

    adapter = WebAdapter()
    adapter.config.WEBUI_ADMIN_AS_COMMAND_SUPERUSER = True

    seen: dict[str, Any] = {}

    async def fake_super_execute(self, **kwargs: Any) -> list:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(BaseAdapter, "execute_command", fake_super_execute)

    await adapter.execute_command(
        chat_key="web-session_x",
        user_id="admin_7",
        username="web_user_7",
        command_name="test",
        raw_args="",
        is_super_user=False,
        is_advanced_user=False,
    )

    assert seen["is_super_user"] is True
    assert seen["is_advanced_user"] is True


async def test_config_switch_on_does_not_promote_tenant_admin(monkeypatch: pytest.MonkeyPatch, db) -> None:  # noqa: ARG001
    from nekro_agent.adapters.interface.base import BaseAdapter

    await _make_user(
        user_id=8,
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        perm_level=Role.Admin,
    )

    adapter = WebAdapter()
    adapter.config.WEBUI_ADMIN_AS_COMMAND_SUPERUSER = True

    seen: dict[str, Any] = {}

    async def fake_super_execute(self, **kwargs: Any) -> list:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(BaseAdapter, "execute_command", fake_super_execute)

    await adapter.execute_command(
        chat_key="web-session_x",
        user_id="admin_8",
        username="web_user_8",
        command_name="test",
        raw_args="",
        is_super_user=False,
        is_advanced_user=False,
    )

    assert seen["is_super_user"] is False
    assert seen["is_advanced_user"] is False
