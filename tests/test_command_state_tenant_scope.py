"""命令状态接口的租户作用域

`/commands` 的 `list` / `set-state` / `reset-state` / `batch-set-state` 都收一个 `chat_key`，
而它们此前只校验 `Role.Admin`、不校验这个频道归谁。租户管理员本身就是 `Role.Admin`
（`TENANT_MAX_PERM_LEVEL`），于是随手换个 `chat_key` 就能关掉别的租户频道里的命令 —— 跨租户拒绝服务。

这里钉死三条：

1. **越权是 404 不是 403**：403 会告诉租户"这个频道确实存在"，把别人的频道变成可枚举的；
2. **系统级写入（`chat_key` 为空）对租户是 403**：它对所有租户同时生效，属于平台方权限边界，
   与"资源存在性"无关，因此这里反过来不能用 404；
3. **平台方与未启用多租户的部署行为不变**：`get_tenant_scope` 恒为 None，一个分支都不进。

命令输出 SSE 流（`/{chat_key}/output/stream`）同属这一族，一起钉在这里：它同样只有
`Role.Admin`，订阅的是别人频道里跑出来的命令输出。它的特别之处是**校验必须发生在建流之前**，
`EventSourceResponse` 一旦返回状态码就已经是 200，生成器里再抛异常只表现为一条建立后立刻断开的连接。

提权路径（`execute` / `webui-execute` / `set-permission` / `reset-permission`）已经是 `Role.Super`，
不在本文件范围内。
"""

import datetime
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import Request
from sse_starlette.sse import EventSourceResponse
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.commands import (
    BatchSetCommandStateRequest,
    ResetCommandStateRequest,
    SetCommandStateRequest,
    batch_set_command_state,
    list_commands,
    reset_command_state,
    set_command_state,
    stream_command_output,
)
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
OTHER_TENANT_ID = 2

OWN_CHAT_KEY = "onebot_v11-inst1-group_10001"
OWN_SECOND_CHAT_KEY = "onebot_v11-inst2-group_10002"
FOREIGN_CHAT_KEY = "onebot_v11-rival1-group_20001"
UNOWNED_CHAT_KEY = "onebot_v11-ob1000000003-group_30001"
MISSING_CHAT_KEY = "onebot_v11-inst1-group_99999"

COMMAND_NAME = "help"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await seed_instances_and_channels()
        yield
    finally:
        await Tortoise.close_connections()


async def seed_instances_and_channels() -> None:
    """两个租户各一批实例与频道，外加一个未归属实例

    形态取自测试环境：`acme_ltd` 名下 `inst1`/`inst2`，另有 `ob1000000003` 未归属任何租户。
    """
    layout: List[Tuple[str, Optional[int], List[str]]] = [
        ("inst1", ACME_TENANT_ID, [OWN_CHAT_KEY]),
        ("inst2", ACME_TENANT_ID, [OWN_SECOND_CHAT_KEY]),
        ("rival1", OTHER_TENANT_ID, [FOREIGN_CHAT_KEY]),
        ("ob1000000003", None, [UNOWNED_CHAT_KEY]),
    ]
    for instance_key, tenant_id, chat_keys in layout:
        await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )
        for chat_key in chat_keys:
            await DBChatChannel.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
                chat_key=chat_key,
                channel_type="group",
                data="{}",
            )


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份

    作用域判定只读 `tenant_id` / `tenant_role` / `perm_level`，落库与否不影响判定。
    """
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def single_tenant_admin() -> DBUser:
    """未启用多租户的部署里的普通管理员：无租户归属、角色为平台方"""
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


@pytest.fixture
def recorded_writes(monkeypatch) -> List[Dict[str, Any]]:
    """把命令状态的落盘换成记录，用来分辨"被拒绝"和"拒绝前已经写进去了"

    真实实现会往 `COMMAND_CHANNEL_STATE_DIR` 写文件，测试不需要也不应该产生这个副作用。
    """
    from nekro_agent.services.command import manager as manager_module

    calls: List[Dict[str, Any]] = []

    async def fake_set_command_enabled(command_name: str, enabled: bool, chat_key: Optional[str] = None) -> None:
        calls.append({"op": "set", "command_name": command_name, "enabled": enabled, "chat_key": chat_key})

    async def fake_reset_command_state(command_name: str, chat_key: Optional[str] = None) -> None:
        calls.append({"op": "reset", "command_name": command_name, "chat_key": chat_key})

    async def fake_get_all_command_states(chat_key: Optional[str] = None) -> List[dict]:
        calls.append({"op": "list", "chat_key": chat_key})
        return [{"name": COMMAND_NAME, "chat_key": chat_key}]

    monkeypatch.setattr(manager_module.command_manager, "set_command_enabled", fake_set_command_enabled)
    monkeypatch.setattr(manager_module.command_manager, "reset_command_state", fake_reset_command_state)
    monkeypatch.setattr(manager_module.command_manager, "get_all_command_states", fake_get_all_command_states)
    return calls


def make_request() -> Request:
    """一个最小可用的 ASGI 请求

    命令输出流在建流之前就要完成校验，此时请求对象还没被读过，
    因此一个只带必需字段的 scope 足以覆盖被测路径。
    """
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


DENIED_CHAT_KEYS = [
    pytest.param(FOREIGN_CHAT_KEY, id="other-tenant-channel"),
    pytest.param(UNOWNED_CHAT_KEY, id="unassigned-instance-channel"),
    pytest.param(MISSING_CHAT_KEY, id="nonexistent-channel"),
]


class TestTenantCannotTouchForeignChannels:
    """越权一律 404，且必须在任何写入之前拒绝"""

    @pytest.mark.parametrize("chat_key", DENIED_CHAT_KEYS)
    async def test_list_rejects(self, db, recorded_writes, chat_key: str) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await list_commands(chat_key=chat_key, _current_user=acme_admin())
        assert recorded_writes == []

    @pytest.mark.parametrize("chat_key", DENIED_CHAT_KEYS)
    async def test_set_state_rejects(self, db, recorded_writes, chat_key: str) -> None:  # noqa: ARG002
        req = SetCommandStateRequest(command_name=COMMAND_NAME, enabled=False, chat_key=chat_key)
        with pytest.raises(NotFoundError):
            await set_command_state(req=req, _current_user=acme_admin())
        assert recorded_writes == []

    @pytest.mark.parametrize("chat_key", DENIED_CHAT_KEYS)
    async def test_reset_state_rejects(self, db, recorded_writes, chat_key: str) -> None:  # noqa: ARG002
        req = ResetCommandStateRequest(command_name=COMMAND_NAME, chat_key=chat_key)
        with pytest.raises(NotFoundError):
            await reset_command_state(req=req, _current_user=acme_admin())
        assert recorded_writes == []

    @pytest.mark.parametrize("chat_key", DENIED_CHAT_KEYS)
    async def test_batch_rejects(self, db, recorded_writes, chat_key: str) -> None:  # noqa: ARG002
        req = BatchSetCommandStateRequest(
            commands=[SetCommandStateRequest(command_name=COMMAND_NAME, enabled=False, chat_key=chat_key)],
        )
        with pytest.raises(NotFoundError):
            await batch_set_command_state(req=req, _current_user=acme_admin())
        assert recorded_writes == []

    async def test_batch_is_all_or_nothing(self, db, recorded_writes) -> None:  # noqa: ARG002
        """混一条越权进来，自己的那条也不能落地

        逐条边校验边写的话，越权条目之前的改动已经生效了，拒绝就成了半个成功。
        """
        req = BatchSetCommandStateRequest(
            commands=[
                SetCommandStateRequest(command_name=COMMAND_NAME, enabled=False, chat_key=OWN_CHAT_KEY),
                SetCommandStateRequest(command_name=COMMAND_NAME, enabled=False, chat_key=FOREIGN_CHAT_KEY),
            ],
        )
        with pytest.raises(NotFoundError):
            await batch_set_command_state(req=req, _current_user=acme_admin())
        assert recorded_writes == []

    @pytest.mark.parametrize("chat_key", DENIED_CHAT_KEYS)
    async def test_output_stream_rejects(self, db, chat_key: str) -> None:  # noqa: ARG002
        """越权订阅命令输出流同样 404，且不能建起流来"""
        with pytest.raises(NotFoundError):
            await stream_command_output(chat_key=chat_key, request=make_request(), _current_user=acme_admin())

    async def test_denial_is_not_permission_error(self, db, recorded_writes) -> None:  # noqa: ARG002
        """存在的别人家频道与压根不存在的频道，对外必须是同一个错误

        任何把它们区分开的实现（403 vs 404）都让租户能枚举平台方的频道。
        """
        assert NotFoundError.http_status == 404

        errors: List[type] = []
        for chat_key in (FOREIGN_CHAT_KEY, MISSING_CHAT_KEY):
            with pytest.raises(NotFoundError) as exc_info:
                await list_commands(chat_key=chat_key, _current_user=acme_admin())
            errors.append(type(exc_info.value))
        assert errors[0] is errors[1]
        assert recorded_writes == []


class TestTenantCanManageOwnChannels:
    """自己名下的频道必须照常可读可写，否则这条修复就把功能改没了"""

    @pytest.mark.parametrize("chat_key", [OWN_CHAT_KEY, OWN_SECOND_CHAT_KEY])
    async def test_list_own_channel(self, db, recorded_writes, chat_key: str) -> None:  # noqa: ARG002
        result = await list_commands(chat_key=chat_key, _current_user=acme_admin())
        assert result == [{"name": COMMAND_NAME, "chat_key": chat_key}]

    async def test_set_and_reset_own_channel(self, db, recorded_writes) -> None:  # noqa: ARG002
        user = acme_admin()
        await set_command_state(
            req=SetCommandStateRequest(command_name=COMMAND_NAME, enabled=False, chat_key=OWN_CHAT_KEY),
            _current_user=user,
        )
        await reset_command_state(
            req=ResetCommandStateRequest(command_name=COMMAND_NAME, chat_key=OWN_CHAT_KEY),
            _current_user=user,
        )
        assert recorded_writes == [
            {"op": "set", "command_name": COMMAND_NAME, "enabled": False, "chat_key": OWN_CHAT_KEY},
            {"op": "reset", "command_name": COMMAND_NAME, "chat_key": OWN_CHAT_KEY},
        ]

    @pytest.mark.parametrize("chat_key", [OWN_CHAT_KEY, OWN_SECOND_CHAT_KEY])
    async def test_output_stream_on_own_channel(self, db, chat_key: str) -> None:  # noqa: ARG002
        response = await stream_command_output(chat_key=chat_key, request=make_request(), _current_user=acme_admin())
        assert isinstance(response, EventSourceResponse)

    async def test_batch_over_own_channels(self, db, recorded_writes) -> None:  # noqa: ARG002
        req = BatchSetCommandStateRequest(
            commands=[
                SetCommandStateRequest(command_name=COMMAND_NAME, enabled=False, chat_key=OWN_CHAT_KEY),
                SetCommandStateRequest(command_name=COMMAND_NAME, enabled=True, chat_key=OWN_SECOND_CHAT_KEY),
            ],
        )
        await batch_set_command_state(req=req, _current_user=acme_admin())
        assert [call["chat_key"] for call in recorded_writes] == [OWN_CHAT_KEY, OWN_SECOND_CHAT_KEY]


class TestSystemLevelScopeIsPlatformOnly:
    """不带 chat_key 写的是系统级状态，它对所有租户同时生效"""

    async def test_tenant_write_without_chat_key_is_forbidden(self, db, recorded_writes) -> None:  # noqa: ARG002
        assert PermissionDeniedError.http_status == 403

        with pytest.raises(PermissionDeniedError):
            await set_command_state(
                req=SetCommandStateRequest(command_name=COMMAND_NAME, enabled=False, chat_key=None),
                _current_user=acme_admin(),
            )
        with pytest.raises(PermissionDeniedError):
            await reset_command_state(
                req=ResetCommandStateRequest(command_name=COMMAND_NAME, chat_key=None),
                _current_user=acme_admin(),
            )
        with pytest.raises(PermissionDeniedError):
            await batch_set_command_state(
                req=BatchSetCommandStateRequest(
                    commands=[SetCommandStateRequest(command_name=COMMAND_NAME, enabled=False, chat_key=None)],
                ),
                _current_user=acme_admin(),
            )
        assert recorded_writes == []

    async def test_tenant_may_still_read_system_level_list(self, db, recorded_writes) -> None:  # noqa: ARG002
        """系统级列表只有命令元数据与全局开关，不含任何租户数据，读不该被挡

        租户后台要先渲染命令列表才能选频道，挡住它等于把功能删掉。
        """
        result = await list_commands(chat_key=None, _current_user=acme_admin())
        assert result == [{"name": COMMAND_NAME, "chat_key": None}]


class TestPlatformSideUnchanged:
    """平台方与未启用多租户的部署一个分支都不该进"""

    @pytest.mark.parametrize("identity", [platform_owner, single_tenant_admin], ids=["platform-owner", "single-tenant"])
    @pytest.mark.parametrize("chat_key", [OWN_CHAT_KEY, FOREIGN_CHAT_KEY, UNOWNED_CHAT_KEY, MISSING_CHAT_KEY, None])
    async def test_every_chat_key_is_allowed(self, db, recorded_writes, identity, chat_key: Optional[str]) -> None:  # noqa: ARG002
        user = identity()
        await list_commands(chat_key=chat_key, _current_user=user)
        await set_command_state(
            req=SetCommandStateRequest(command_name=COMMAND_NAME, enabled=False, chat_key=chat_key),
            _current_user=user,
        )
        await reset_command_state(
            req=ResetCommandStateRequest(command_name=COMMAND_NAME, chat_key=chat_key),
            _current_user=user,
        )
        await batch_set_command_state(
            req=BatchSetCommandStateRequest(
                commands=[SetCommandStateRequest(command_name=COMMAND_NAME, enabled=True, chat_key=chat_key)],
            ),
            _current_user=user,
        )
        assert [call["op"] for call in recorded_writes] == ["list", "set", "reset", "set"]
        assert {call["chat_key"] for call in recorded_writes} == {chat_key}

    @pytest.mark.parametrize("identity", [platform_owner, single_tenant_admin], ids=["platform-owner", "single-tenant"])
    @pytest.mark.parametrize("chat_key", [OWN_CHAT_KEY, FOREIGN_CHAT_KEY, UNOWNED_CHAT_KEY, MISSING_CHAT_KEY])
    async def test_output_stream_is_unrestricted(self, db, identity, chat_key: str) -> None:  # noqa: ARG002
        response = await stream_command_output(chat_key=chat_key, request=make_request(), _current_user=identity())
        assert isinstance(response, EventSourceResponse)
