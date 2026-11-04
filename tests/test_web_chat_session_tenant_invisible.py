"""网页会话对租户天然不可见

网页适配器是单实例的：`DBChatChannel.instance_key` 为空。租户的可见性按
`(adapter_key, instance_key)` 成对判定（`accessible_instance_refs`），空 instance_key
不落进任何租户的实例集合，所以租户的频道列表、详情、实时流都看不到网页会话。

这不是"顺手过滤掉了"，而是"这条频道行本来就不属于任何租户"。这里把它钉住：
即使网页会话与租户自己的会话同时存在，租户也只看到自己那一行。
"""

import json
from datetime import datetime, timezone
from typing import Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.chat_channel import assert_channel_accessible, visible_channels
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.role import Role

TENANT_ID = 7
WEB_CHAT_KEY = "web-session_abc"
TENANT_CHAT_KEY = "wechat_ilink_multi-tenant_inst-private_tg1"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def tenant_admin() -> DBUser:
    return _user(tenant_id=TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return _user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


@pytest.fixture
async def channels(db) -> None:  # noqa: ARG001
    await DBAdapterInstance.create(
        adapter_key="wechat_ilink_multi",
        instance_key="tenant_inst",
        name="租户实例",
        tenant_id=TENANT_ID,
    )
    # 网页会话：单实例，instance_key 为空
    await DBChatChannel.create(
        adapter_key="web",
        instance_key="",
        channel_id="session_abc",
        channel_name="网页测试会话",
        chat_key=WEB_CHAT_KEY,
        chat_type=ChatType.PRIVATE,
        is_active=True,
        observe_mode=False,
        data=json.dumps({}),
    )
    # 租户自己的会话
    await DBChatChannel.create(
        adapter_key="wechat_ilink_multi",
        instance_key="tenant_inst",
        channel_id="private_tg1",
        channel_name="租户会话",
        chat_key=TENANT_CHAT_KEY,
        chat_type=ChatType.PRIVATE,
        is_active=True,
        observe_mode=False,
        data=json.dumps({}),
    )


async def test_tenant_channel_list_has_no_web_session(channels) -> None:  # noqa: ARG001
    keys = {channel.chat_key async for channel in await visible_channels(tenant_admin())}

    assert keys == {TENANT_CHAT_KEY}
    assert WEB_CHAT_KEY not in keys


async def test_platform_owner_sees_the_web_session(channels) -> None:  # noqa: ARG001
    keys = {channel.chat_key async for channel in await visible_channels(platform_owner())}

    assert WEB_CHAT_KEY in keys


async def test_tenant_detail_of_web_session_is_404(channels) -> None:  # noqa: ARG001
    with pytest.raises(NotFoundError):
        await assert_channel_accessible(tenant_admin(), WEB_CHAT_KEY)


async def test_platform_owner_detail_of_web_session_passes(channels) -> None:  # noqa: ARG001
    await assert_channel_accessible(platform_owner(), WEB_CHAT_KEY)
