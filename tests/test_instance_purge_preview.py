"""彻底删除实例前的影响面：自有邮箱、用户邮件偏好、收集库表情

确认框要写出「这次会一并删掉什么」：这三样跟着实例一起删（`purge_instance`），而它们是 #79 新加的，
确认框里原有的那句「会话、消息与记忆」不包括它们。接口与删除同一档权限（`Role.Super`），只读。
"""

from __future__ import annotations

import datetime

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_email_user_pref import DBEmailUserPref
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.instances import preview_console_instance_purge
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import emotion_library
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OTHER = "inst2"


def platform_owner() -> DBUser:
    return DBUser(
        username="owner",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def tenant_admin() -> DBUser:
    return DBUser(
        username="acme-admin",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=Role.Admin,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=1,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for key in (INSTANCE, OTHER):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=key,
                display_name=key,
                status="online",
                provider="remote_onebot",
                tenant_id=1,
                metadata_json="",
            )
        yield
    finally:
        await Tortoise.close_connections()


async def add_mailbox(instance_key: str, username: str) -> None:
    await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
        username=username,
        display_name="",
        provider="QQ邮箱",
        settings={},
        password="p",
        attachment_dir="",
    )


async def add_pref(instance_key: str, userid: str) -> None:
    await DBEmailUserPref.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        platform_userid=userid,
        email="someone@example.test",
    )


class TestThePurgePreview:
    async def test_it_counts_only_this_instance(self, db) -> None:
        await add_mailbox(INSTANCE, "a@example.test")
        await add_mailbox(INSTANCE, "b@example.test")
        await add_mailbox(OTHER, "c@example.test")
        await add_pref(INSTANCE, "1001")
        await add_pref(OTHER, "1002")
        collect = await emotion_library.get_or_create_collect_set(ADAPTER, INSTANCE)
        await emotion_library.create_item(
            set_id=collect.id, description="笑", tags=[], file_path="emotions/h/x.png", file_sha256="x" * 64, source_path=""
        )

        preview = await preview_console_instance_purge(INSTANCE, _current_user=platform_owner())

        assert (preview.email_accounts, preview.email_user_prefs, preview.collected_emotions) == (2, 1, 1)

    async def test_an_empty_instance_is_all_zero(self, db) -> None:
        preview = await preview_console_instance_purge(OTHER, _current_user=platform_owner())

        assert (preview.email_accounts, preview.email_user_prefs, preview.collected_emotions) == (0, 0, 0)

    async def test_a_tenant_admin_is_refused(self, db) -> None:
        """与删除同一档：租户管理员连影响面也看不到（删实例本来就不是他的动作）"""
        from fastapi import HTTPException

        from nekro_agent.schemas.errors import PermissionDeniedError

        with pytest.raises((HTTPException, PermissionDeniedError)):
            await preview_console_instance_purge(INSTANCE, _current_user=tenant_admin())
