"""用户级命令权限在多租户下的边界

上游 2.4.x 的用户级命令权限是单租户设计：一个平台用户一条全局记录。本分支在它上面叠了两件事，
这里逐条钉住：

1. **叠加**：超级用户只认 `SUPER_USERS`，两个标志与引入用户级权限之前逐字一致；用户级权限最多给到
   `ADVANCED`，而 `ADVANCED` 能做的事收在当前频道；
2. **按频道授权**：租户管理员只能给自己名下实例的频道授权，授权只在那一个 `chat_key` 上生效；
   别人的频道与不存在的频道一律 404、什么都不写；租户只读成员 403。

同一个 QQ 群里同时有两个租户的机器人时，两边频道的 `channel_id` 相同、`chat_key` 不同——
种子数据刻意这样摆，「按 channel_id 而不是 chat_key 判归属」的实现在这里会红。
"""

import datetime
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException
from pydantic import ValidationError as PydanticValidationError
from tortoise import Tortoise

from nekro_agent.adapters.interface.base import BaseAdapter
from nekro_agent.adapters.interface.schemas.platform import PlatformChannel, PlatformMessage, PlatformUser
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import ChannelStatus, DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.schemas import (
    CommandExecutionContext,
    CommandRequest,
    CommandResponseStatus,
)
from nekro_agent.services.user.role import Role

ONEBOT = "onebot_v11"
ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2
GROUP = "group_5000"
ACME_CHAT = f"{ONEBOT}-acme1-{GROUP}"
RIVAL_CHAT = f"{ONEBOT}-rival1-{GROUP}"
MISSING_CHAT = f"{ONEBOT}-acme1-group_404"

# (platform_userid, 昵称)：只在 ACME 的频道里说过话
ACME_SPEAKERS = [("10001", "甲"), ("10002", "乙"), ("40004", "丁"), ("50005", "戊"), ("60006", "己")]


@pytest.fixture(autouse=True)
def isolated_command_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from nekro_agent.core.config import config
    from nekro_agent.services.command import manager as manager_mod

    monkeypatch.setattr(manager_mod, "COMMAND_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(manager_mod, "COMMAND_CHANNEL_STATE_DIR", str(tmp_path / "channels"))
    monkeypatch.setattr(manager_mod, "COMMAND_CHANNEL_PERMISSION_DIR", str(tmp_path / "channel_permissions"))
    monkeypatch.setattr(manager_mod, "COMMAND_SYSTEM_STATE_FILE", str(tmp_path / "system.json"))
    monkeypatch.setattr(manager_mod, "COMMAND_SYSTEM_PERMISSION_FILE", str(tmp_path / "system_permissions.json"))
    monkeypatch.setattr(manager_mod, "COMMAND_USER_PERMISSION_FILE", str(tmp_path / "user_permissions.json"))
    monkeypatch.setattr(
        manager_mod,
        "COMMAND_CHANNEL_USER_PERMISSION_FILE",
        str(tmp_path / "channel_user_permissions.json"),
    )
    monkeypatch.setattr(config, "SUPER_USERS", [])
    monkeypatch.setattr(config, "ENABLE_ADVANCED_COMMAND", False)

    # 群角色识别开关按四层配置解析，解析时会按需建出频道 / 实例 / 适配器层的覆盖文件：一并改道到临时目录
    from nekro_agent.core.core_utils import ConfigManager
    from nekro_agent.core.os_env import OsEnv
    from nekro_agent.services import config_service
    from nekro_agent.services.config_resolver import config_resolver

    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config_service, "CHANNEL_CONFIG_DIR", tmp_path / "data" / "configs" / "channels")
    config_keys = [config_service.instance_override_config_key(ONEBOT, name) for name in ("acme1", "rival1")]
    config_keys += [f"adapter_override_{ONEBOT}"] + [f"channel_config_{key}" for key in (ACME_CHAT, RIVAL_CHAT, MISSING_CHAT)]
    for key in config_keys:
        ConfigManager.unregister_config(key)
    config_resolver.bump_version()
    manager_mod.command_manager.invalidate_cache()
    yield tmp_path
    for key in config_keys:
        ConfigManager.unregister_config(key)
    config_resolver.bump_version()
    manager_mod.command_manager.invalidate_cache()


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for tenant_id, name in ((ACME_TENANT_ID, "acme"), (RIVAL_TENANT_ID, "rival")):
            await DBTenant.create(id=tenant_id, name=name, display_name=name)
        for instance_key, tenant_id, chat_key in (
            ("acme1", ACME_TENANT_ID, ACME_CHAT),
            ("rival1", RIVAL_TENANT_ID, RIVAL_CHAT),
        ):
            await DBAdapterInstance.create(
                adapter_key=ONEBOT,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider=ONEBOT,
                tenant_id=tenant_id,
                metadata_json="",
            )
            await DBChatChannel.create(
                adapter_key=ONEBOT,
                instance_key=instance_key,
                channel_id=GROUP,
                chat_key=chat_key,
                channel_type="group",
                data="{}",
            )
        for index, (userid, nickname) in enumerate(ACME_SPEAKERS):
            await DBChatMessage.create(
                sender_id=userid,
                sender_name=nickname,
                sender_nickname=nickname,
                is_tome=0,
                is_recalled=False,
                adapter_key=ONEBOT,
                instance_key="acme1",
                message_id=f"acme-msg-{index}",
                chat_key=ACME_CHAT,
                chat_type=ChatType.GROUP,
                platform_userid=userid,
                content_text="hi",
                content_data="[]",
                raw_cq_code="",
                ext_data="",
                send_timestamp=0,
            )
        yield
    finally:
        await Tortoise.close_connections()


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="",
        platform_userid="",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def acme_member() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)


def rival_admin() -> DBUser:
    return make_user(tenant_id=RIVAL_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


async def put_grant(who: DBUser, chat_key: str, userid: str, permission: str = "advanced") -> Any:
    from nekro_agent.routers import chat_channel as chat_module

    return await chat_module.set_chat_channel_user_command_permission(
        chat_key=chat_key,
        platform_userid=userid,
        body=chat_module.SetChannelUserCommandPermissionRequest(permission=permission),  # type: ignore[arg-type]
        _current_user=who,
    )


def channel_grant(chat_key: str, userid: str) -> CommandPermission:
    from nekro_agent.services.command.manager import command_manager

    return command_manager.get_channel_user_permission(chat_key, ONEBOT, userid)


def channel_file(state_dir) -> Any:
    return state_dir / "channel_user_permissions.json"


class _FakeAdapter:
    key = ONEBOT

    def __init__(self, permission: CommandPermission = CommandPermission.USER, *, raises: bool = False):
        self.permission = permission
        self.raises = raises
        self.lookups = 0
        self.executed: List[Dict[str, Any]] = []
        self.granted: Optional[tuple] = None

    async def get_user_command_permission(self, platform_user, platform_channel, platform_message):
        del platform_user, platform_channel, platform_message
        self.lookups += 1
        if self.raises:
            raise RuntimeError("协议端不可用")
        return self.permission

    async def set_user_command_permission(self, platform_userid, permission, *, source="manual", channel_id=None):
        self.granted = (platform_userid, permission, source, channel_id)
        return permission

    def detect_command(self, text: str):
        return ("reset", "") if text.startswith("/reset") else None

    async def execute_command(self, **kwargs: Any) -> None:
        self.executed.append(kwargs)

    async def try_handle_wait_input(self, **_kwargs: Any) -> bool:
        return False


class _RecordingAdapter:
    """授予 / 撤销命令用的适配器替身：只记下调用"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def set_user_command_permission(self, platform_userid, permission, *, source="manual", channel_id=None):
        del source, channel_id
        self.calls.append(("set", platform_userid, permission))
        return permission

    async def reset_user_command_permission(self, platform_userid):
        self.calls.append(("reset", platform_userid))


def inbound(userid: str = "10001", chat_type: ChatType = ChatType.GROUP):
    return (
        PlatformChannel(channel_id=GROUP, channel_name="", channel_type=chat_type),
        PlatformUser(platform_name="qq", user_id=userid, user_name="tester"),
        PlatformMessage(message_id="1", sender_id=userid, sender_name="tester", content_text="/reset"),
    )


async def flags(adapter: _FakeAdapter, chat_key: str, userid: str = "10001") -> tuple:
    from nekro_agent.adapters.interface.collector import _resolve_user_command_flags

    channel, user, message = inbound(userid)
    return await _resolve_user_command_flags(adapter, channel, user, message, chat_key=chat_key)  # type: ignore[arg-type]


def advanced_context(chat_key: str = ACME_CHAT) -> CommandExecutionContext:
    return CommandExecutionContext(
        user_id="10001",
        chat_key=chat_key,
        username="频道管理员",
        adapter_key=ONEBOT,
        is_super_user=False,
        is_advanced_user=True,
    )


def super_context(adapter_key: str = ONEBOT) -> CommandExecutionContext:
    return CommandExecutionContext(
        user_id="99999",
        chat_key=ACME_CHAT,
        username="平台方",
        adapter_key=adapter_key,
        is_super_user=True,
        is_advanced_user=True,
    )


class TestChannelGrantRoute:
    """`PUT /chat-channel/{chat_key}/users/{platform_userid}/command-permission`"""

    async def test_tenant_admin_grant_lands_on_its_own_chat_key_only(self, db) -> None:
        from nekro_agent.services.command.manager import command_manager

        await put_grant(acme_admin(), ACME_CHAT, "10001")

        assert channel_grant(ACME_CHAT, "10001") == CommandPermission.ADVANCED
        # 同一个群、同一个人，在另一个租户的机器人那里不是频道管理员
        assert channel_grant(RIVAL_CHAT, "10001") == CommandPermission.USER
        # 也没有写进上游那份全局授权
        assert command_manager.get_user_permission(ONEBOT, "10001") == CommandPermission.USER

    async def test_cross_tenant_grant_is_not_found_and_writes_nothing(self, db, isolated_command_state) -> None:
        with pytest.raises(NotFoundError):
            await put_grant(acme_admin(), RIVAL_CHAT, "10001")

        assert channel_grant(RIVAL_CHAT, "10001") == CommandPermission.USER
        assert not channel_file(isolated_command_state).exists()

    async def test_cross_tenant_and_missing_channel_are_indistinguishable(self, db) -> None:
        with pytest.raises(NotFoundError) as foreign:
            await put_grant(acme_admin(), RIVAL_CHAT, "10001")
        with pytest.raises(NotFoundError) as missing:
            await put_grant(acme_admin(), MISSING_CHAT, "10001")

        assert str(foreign.value) == str(missing.value)

    async def test_tenant_cannot_revoke_another_tenants_grant(self, db) -> None:
        await put_grant(rival_admin(), RIVAL_CHAT, "10001")

        with pytest.raises(NotFoundError):
            await put_grant(acme_admin(), RIVAL_CHAT, "10001", permission="user")

        assert channel_grant(RIVAL_CHAT, "10001") == CommandPermission.ADVANCED

    async def test_read_only_member_is_forbidden(self, db, isolated_command_state) -> None:
        with pytest.raises(HTTPException) as denied:
            await put_grant(acme_member(), ACME_CHAT, "10001")

        assert denied.value.status_code == 403
        assert not channel_file(isolated_command_state).exists()

    async def test_platform_owner_can_grant_in_any_channel(self, db) -> None:
        await put_grant(platform_owner(), RIVAL_CHAT, "10001")

        assert channel_grant(RIVAL_CHAT, "10001") == CommandPermission.ADVANCED
        assert channel_grant(ACME_CHAT, "10001") == CommandPermission.USER

    async def test_platform_owner_still_gets_404_for_a_missing_channel(self, db, isolated_command_state) -> None:
        with pytest.raises(NotFoundError):
            await put_grant(platform_owner(), MISSING_CHAT, "10001")

        assert not channel_file(isolated_command_state).exists()

    async def test_revoke_only_touches_that_channel(self, db) -> None:
        await put_grant(platform_owner(), ACME_CHAT, "10001")
        await put_grant(platform_owner(), RIVAL_CHAT, "10001")

        await put_grant(acme_admin(), ACME_CHAT, "10001", permission="user")

        assert channel_grant(ACME_CHAT, "10001") == CommandPermission.USER
        assert channel_grant(RIVAL_CHAT, "10001") == CommandPermission.ADVANCED

    @pytest.mark.parametrize("permission", ["super_user", "public", "ADVANCED", ""])
    def test_request_model_only_accepts_advanced_or_user(self, permission: str) -> None:
        from nekro_agent.routers.chat_channel import SetChannelUserCommandPermissionRequest

        with pytest.raises(PydanticValidationError):
            SetChannelUserCommandPermissionRequest(permission=permission)  # type: ignore[arg-type]

    async def test_manager_refuses_super_user_even_without_the_request_model(self, db) -> None:
        from nekro_agent.services.command.manager import command_manager

        with pytest.raises(ValidationError):
            await command_manager.set_channel_user_permission(ACME_CHAT, ONEBOT, "10001", CommandPermission.SUPER_USER)
        assert channel_grant(ACME_CHAT, "10001") == CommandPermission.USER

    async def test_blank_user_id_is_rejected(self, db, isolated_command_state) -> None:
        with pytest.raises(ValidationError):
            await put_grant(acme_admin(), ACME_CHAT, "   ")
        assert not channel_file(isolated_command_state).exists()


class TestChannelUsersListing:
    """`GET /chat-channel/{chat_key}/users` 多出来的档位与来源"""

    async def _listing(self, who: DBUser, chat_key: str) -> Dict[str, Any]:
        from nekro_agent.routers import chat_channel as chat_module

        response = await chat_module.get_chat_channel_users(chat_key=chat_key, _current_user=who)
        return {item.platform_userid: item for item in response.items}

    async def _seed_every_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config
        from nekro_agent.services.command.manager import command_manager

        monkeypatch.setattr(config, "SUPER_USERS", ["10002"])
        await put_grant(acme_admin(), ACME_CHAT, "10001")
        await put_grant(acme_admin(), ACME_CHAT, "10002")  # 超级用户名下也挂一条本频道授权
        await put_grant(acme_admin(), ACME_CHAT, "30003")  # 从没在这里说过话
        await command_manager.set_user_permission(ONEBOT, "40004", CommandPermission.ADVANCED)
        await command_manager.set_user_permission(
            ONEBOT, "50005", CommandPermission.ADVANCED, source="platform", channel_id=GROUP,
        )
        await command_manager.set_user_permission(
            ONEBOT, "60006", CommandPermission.ADVANCED, source="platform", channel_id="group_9999",
        )

    @staticmethod
    def _seen(items: Dict[str, Any]) -> Dict[str, tuple]:
        return {
            uid: (item.command_permission, item.command_permission_source, item.channel_granted)
            for uid, item in items.items()
        }

    async def test_platform_owner_sees_every_source(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        await self._seed_every_source(monkeypatch)

        items = await self._listing(platform_owner(), ACME_CHAT)

        assert self._seen(items) == {
            "10001": ("advanced", "channel", True),
            # 名单里的号在租户的频道里不是超级用户（租户名下的实例不是可信来源），只剩本频道授权那一档；
            # 在平台方频道里显示成超级用户的用例在 test_super_users_identity_scope.py
            "10002": ("advanced", "channel", True),
            "30003": ("advanced", "channel", True),
            "40004": ("advanced", "global", False),
            # 群角色识别默认关：平台来源的快照不算数
            "50005": ("user", None, False),
            "60006": ("user", None, False),
        }
        assert items["30003"].nickname == ""

    async def test_tenant_does_not_see_the_platforms_internal_split(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """平台方名单与全局授权都是平台方的数据：租户只看到「平台方设置」，档位照实给"""
        await self._seed_every_source(monkeypatch)

        items = await self._listing(acme_admin(), ACME_CHAT)

        assert self._seen(items)["10002"] == ("advanced", "channel", True)
        assert self._seen(items)["40004"] == ("advanced", "platform", False)
        assert {item.command_permission_source for item in items.values()} <= {"platform", "channel", None}

    async def test_group_role_snapshot_counts_only_when_detection_is_on(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.core.config import config

        await self._seed_every_source(monkeypatch)
        monkeypatch.setattr(config, "COMMAND_GROUP_ADMIN_AS_CHANNEL_ADMIN", True)

        seen = self._seen(await self._listing(acme_admin(), ACME_CHAT))

        assert seen["50005"] == ("advanced", "group_role", False)
        # 别的群里的快照照样不算
        assert seen["60006"] == ("user", None, False)

    async def test_another_channels_grant_does_not_show_here(self, db) -> None:
        await put_grant(rival_admin(), RIVAL_CHAT, "10001")

        items = await self._listing(acme_admin(), ACME_CHAT)

        assert items["10001"].command_permission == "user"
        assert items["10001"].command_permission_source is None

    async def test_listing_another_tenants_channel_is_not_found(self, db) -> None:
        with pytest.raises(NotFoundError):
            await self._listing(acme_admin(), RIVAL_CHAT)


class TestLayering:
    """`collector._resolve_user_command_flags`：两层叠加，谁都不能替对方放行"""

    @pytest.mark.parametrize("enable_advanced", [False, True])
    async def test_super_users_flags_are_unchanged(self, monkeypatch: pytest.MonkeyPatch, enable_advanced: bool) -> None:
        """超级用户的两个标志取值不变；「谁算超级用户」（名单匹配 ∧ 来源可信）由 test_super_users_identity_scope.py 负责，
        这里把来源判定打桩成可信"""
        from nekro_agent.core.config import config
        from nekro_agent.services.user import super_users as super_users_mod

        async def trusted(*_args: Any) -> bool:
            return True

        monkeypatch.setattr(super_users_mod, "is_trusted_super_user_source", trusted)
        monkeypatch.setattr(config, "SUPER_USERS", ["10001"])
        monkeypatch.setattr(config, "ENABLE_ADVANCED_COMMAND", enable_advanced)
        adapter = _FakeAdapter(CommandPermission.USER)

        assert await flags(adapter, ACME_CHAT) == (True, enable_advanced)
        assert adapter.lookups == 0

    async def test_adapter_super_user_is_clamped_to_advanced(self) -> None:
        assert await flags(_FakeAdapter(CommandPermission.SUPER_USER), ACME_CHAT) == (False, True)

    async def test_stored_super_user_via_base_default_is_clamped(self) -> None:
        from nekro_agent.adapters.interface.collector import _resolve_user_command_flags
        from nekro_agent.services.command.manager import command_manager

        await command_manager.set_user_permission(ONEBOT, "10001", CommandPermission.SUPER_USER)

        class _DefaultAdapter(_FakeAdapter):
            get_user_command_permission = BaseAdapter.get_user_command_permission

        channel, user, message = inbound()
        assert await _resolve_user_command_flags(
            _DefaultAdapter(), channel, user, message, chat_key=ACME_CHAT,  # type: ignore[arg-type]
        ) == (False, True)

    async def test_upstream_four_argument_call_shape_still_works(self) -> None:
        """照上游函数体替换钩子的插件按 4 参调用；没给会话标识时按频道现算，本频道授权照样生效"""
        from nekro_agent.adapters.interface.collector import _resolve_user_command_flags
        from nekro_agent.services.command.manager import command_manager

        class _ChatKeyAdapter(_FakeAdapter):
            def build_chat_key(self, channel_id: str, instance_key: str = "") -> str:
                return f"{ONEBOT}-{instance_key}-{channel_id}"

        await command_manager.set_channel_user_permission(ACME_CHAT, ONEBOT, "10001", CommandPermission.ADVANCED)
        channel = PlatformChannel(channel_id=GROUP, channel_name="", channel_type=ChatType.GROUP, instance_key="acme1")
        _, user, message = inbound()

        assert await _resolve_user_command_flags(_ChatKeyAdapter(), channel, user, message) == (False, True)  # type: ignore[arg-type]

    async def test_unreadable_channel_grant_file_does_not_break_the_command_entry(self, isolated_command_state) -> None:
        """授权文件混进非 UTF-8 字节：命令照常走到执行、按普通用户处理，坏文件挪开保留"""
        from nekro_agent.adapters.interface.collector import _try_handle_command

        channel_file(isolated_command_state).write_bytes(b'{"\xb2\xe2": 1}')
        adapter = _FakeAdapter(CommandPermission.USER)
        channel, user, message = inbound()

        await _try_handle_command(adapter, ACME_CHAT, channel, user, message, "/reset")  # type: ignore[arg-type]

        assert [(call["is_super_user"], call["is_advanced_user"]) for call in adapter.executed] == [(False, False)]
        assert not channel_file(isolated_command_state).exists()
        assert list(isolated_command_state.glob("channel_user_permissions.json.unreadable-*"))

    async def test_unreadable_global_file_does_not_break_the_user_manager_display(self, isolated_command_state) -> None:
        from nekro_agent.routers import user_manager

        (isolated_command_state / "user_permissions.json").write_bytes(b'{"\xb2\xe2": 1}')

        assert user_manager._resolve_effective_command_permission(ONEBOT, "10001") == CommandPermission.USER

    async def test_channel_grant_counts_only_on_its_own_chat_key(self, db) -> None:
        await put_grant(acme_admin(), ACME_CHAT, "10001")

        in_own = _FakeAdapter(CommandPermission.USER)
        assert await flags(in_own, ACME_CHAT) == (False, True)
        assert in_own.lookups == 0, "本频道授权命中时不该再问协议端"

        in_rival = _FakeAdapter(CommandPermission.USER)
        assert await flags(in_rival, RIVAL_CHAT) == (False, False)
        assert in_rival.lookups == 1

    async def test_command_context_end_to_end(self, db) -> None:
        """从消息入口走一遍：同一个人同一句 `/reset`，两个租户的频道里拿到的标志不同"""
        from nekro_agent.adapters.interface.collector import _try_handle_command

        await put_grant(acme_admin(), ACME_CHAT, "10001")
        adapter = _FakeAdapter(CommandPermission.USER)
        channel, user, message = inbound()

        await _try_handle_command(adapter, ACME_CHAT, channel, user, message, "/reset")  # type: ignore[arg-type]
        await _try_handle_command(adapter, RIVAL_CHAT, channel, user, message, "/reset")  # type: ignore[arg-type]

        assert [(call["chat_key"], call["is_super_user"], call["is_advanced_user"]) for call in adapter.executed] == [
            (ACME_CHAT, False, True),
            (RIVAL_CHAT, False, False),
        ]

    async def test_adapter_error_falls_back_to_user(self) -> None:
        assert await flags(_FakeAdapter(raises=True), ACME_CHAT) == (False, False)

    async def test_global_manual_grant_applies_through_the_base_default(self) -> None:
        from nekro_agent.services.command.manager import command_manager

        await command_manager.set_user_permission(ONEBOT, "10001", CommandPermission.ADVANCED)
        channel, user, message = inbound()

        permission = await BaseAdapter.get_user_command_permission(
            SimpleNamespace(key=ONEBOT),  # type: ignore[arg-type]
            user,
            channel,
            message,
        )
        assert permission == CommandPermission.ADVANCED

    async def test_advanced_user_cannot_bulk_switch_other_tenants_channels(self, db) -> None:
        """频道管理员的 `na_off *` 被拒，而且两个租户的频道一个都没被关"""
        from nekro_agent.services.command.built_in.switch import NaOffCommand

        for channel in await DBChatChannel.all():
            await channel.set_channel_status(ChannelStatus.ACTIVE)

        response = await NaOffCommand().execute(advanced_context(), "*")

        assert response.status == CommandResponseStatus.ERROR
        states = await DBChatChannel.all().values_list("chat_key", "is_active")
        assert sorted(states) == [(ACME_CHAT, True), (RIVAL_CHAT, True)]

    async def test_advanced_user_cannot_target_another_tenants_channel(self, db) -> None:
        from nekro_agent.services.command.built_in.switch import NaOffCommand

        rival = await DBChatChannel.get(chat_key=RIVAL_CHAT)
        await rival.set_channel_status(ChannelStatus.ACTIVE)

        response = await NaOffCommand().execute(advanced_context(), RIVAL_CHAT)

        assert response.status == CommandResponseStatus.ERROR
        assert (await DBChatChannel.get(chat_key=RIVAL_CHAT)).is_active is True

    async def test_advanced_user_switches_its_own_channel(self, db) -> None:
        """对照：不带目标时操作的就是当前频道，频道管理员本来就该做得到"""
        from nekro_agent.services.command.built_in.switch import NaOffCommand

        for channel in await DBChatChannel.all():
            await channel.set_channel_status(ChannelStatus.ACTIVE)

        response = await NaOffCommand().execute(advanced_context(), "")

        assert response.status == CommandResponseStatus.SUCCESS
        assert (await DBChatChannel.get(chat_key=ACME_CHAT)).is_active is False
        assert (await DBChatChannel.get(chat_key=RIVAL_CHAT)).is_active is True

    def test_enable_advanced_command_default_stays_off(self) -> None:
        from nekro_agent.core.config import CoreConfig

        assert CoreConfig.model_fields["ENABLE_ADVANCED_COMMAND"].default is False


class TestGrantCommandsRequireSuperUser:
    """`grant_advanced` / `revoke_advanced` 写的是全局授权，频道管理员碰不到"""

    @staticmethod
    def _real_adapter(monkeypatch: pytest.MonkeyPatch) -> "_RecordingAdapter":
        """让 `get_adapter` 找得到适配器：否则拒绝可能只是因为适配器没加载，测不到那道身份判断"""
        from nekro_agent.adapters.utils import adapter_utils

        fake = _RecordingAdapter()
        monkeypatch.setattr(adapter_utils, "get_adapter", lambda adapter_key: fake)
        return fake

    async def test_advanced_user_grant_is_refused_and_writes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.command.built_in.user import GrantAdvancedCommand

        fake = self._real_adapter(monkeypatch)

        response = await GrantAdvancedCommand().execute(advanced_context(), "20002", ONEBOT)

        assert response.status == CommandResponseStatus.ERROR
        assert "仅限超级用户" in response.message
        assert fake.calls == []

    async def test_advanced_user_revoke_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.command.built_in.user import RevokeAdvancedCommand

        fake = self._real_adapter(monkeypatch)

        response = await RevokeAdvancedCommand().execute(advanced_context(), "20002", ONEBOT)

        assert response.status == CommandResponseStatus.ERROR
        assert "仅限超级用户" in response.message
        assert fake.calls == []

    async def test_super_user_grant_and_revoke_go_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照：同样找得到适配器时，超级用户两条都能执行"""
        from nekro_agent.services.command.built_in.user import GrantAdvancedCommand, RevokeAdvancedCommand

        fake = self._real_adapter(monkeypatch)

        granted = await GrantAdvancedCommand().execute(super_context(), "20002", ONEBOT)
        revoked = await RevokeAdvancedCommand().execute(super_context(), "20002", ONEBOT)

        assert (granted.status, revoked.status) == (CommandResponseStatus.SUCCESS, CommandResponseStatus.SUCCESS)
        assert fake.calls == [("set", "20002", CommandPermission.ADVANCED), ("reset", "20002")]

    @pytest.mark.parametrize("command_name", ["grant_advanced", "revoke_advanced"])
    async def test_permission_override_cannot_lower_either(self, monkeypatch: pytest.MonkeyPatch, command_name: str) -> None:
        from nekro_agent.services.command.built_in.user import GrantAdvancedCommand, RevokeAdvancedCommand
        from nekro_agent.services.command.manager import command_manager

        fake = self._real_adapter(monkeypatch)
        monkeypatch.setattr(
            command_manager,
            "get_command_permission",
            lambda command_name, default_permission, chat_key=None: CommandPermission.ADVANCED,
        )
        command = GrantAdvancedCommand() if command_name == "grant_advanced" else RevokeAdvancedCommand()
        request = CommandRequest(context=advanced_context(), command_name=command_name, raw_args=f"20002 {ONEBOT}")

        responses = [response async for response in command.handle(request)]

        assert [response.status for response in responses] == [CommandResponseStatus.ERROR]
        assert fake.calls == []

    async def test_permission_override_cannot_lower_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """平台方把它覆盖成 ADVANCED：权限检查放行了，函数体仍然拒绝，存储不变"""
        from nekro_agent.adapters.utils import adapter_utils
        from nekro_agent.services.command.built_in.user import GrantAdvancedCommand
        from nekro_agent.services.command.manager import command_manager

        fake = _FakeAdapter()
        monkeypatch.setattr(adapter_utils, "get_adapter", lambda adapter_key: fake)
        monkeypatch.setattr(
            command_manager,
            "get_command_permission",
            lambda command_name, default_permission, chat_key=None: CommandPermission.ADVANCED,
        )

        async def run(context: CommandExecutionContext) -> List[Any]:
            request = CommandRequest(context=context, command_name="grant_advanced", raw_args=f"20002 {ONEBOT}")
            return [response async for response in GrantAdvancedCommand().handle(request)]

        refused = await run(advanced_context())
        assert [response.status for response in refused] == [CommandResponseStatus.ERROR]
        assert fake.granted is None

        # 对照：同一条覆盖下超级用户照常授权，说明上面的拒绝来自函数体内的那道判断
        allowed = await run(super_context())
        assert [response.status for response in allowed] == [CommandResponseStatus.SUCCESS]
        assert fake.granted is not None and fake.granted[:2] == ("20002", CommandPermission.ADVANCED)

    async def test_webui_context_must_name_a_real_adapter(self) -> None:
        from nekro_agent.services.command.built_in.user import GrantAdvancedCommand
        from nekro_agent.services.command.manager import command_manager

        response = await GrantAdvancedCommand().execute(super_context(adapter_key="webui"), "20002")

        assert response.status == CommandResponseStatus.ERROR
        assert command_manager.get_user_permission("webui", "20002") == CommandPermission.USER


class TestRegistrationPersistence:
    """新用户注册时登记平台来源权限：不覆盖手动授权，出错不拖垮消息入口"""

    async def _persist(self, adapter: _FakeAdapter, userid: str = "10001") -> None:
        from nekro_agent.adapters.interface.collector import _persist_registered_user_command_permission

        channel, user, message = inbound(userid)
        await _persist_registered_user_command_permission(adapter, channel, user, message)  # type: ignore[arg-type]

    async def test_manual_grant_is_not_overwritten(self) -> None:
        from nekro_agent.services.command.manager import USER_PERMISSION_SOURCE_MANUAL, command_manager

        await command_manager.set_user_permission(ONEBOT, "10001", CommandPermission.ADVANCED)
        adapter = _FakeAdapter(CommandPermission.ADVANCED)

        await self._persist(adapter)

        assert adapter.granted is None
        record = command_manager.get_user_permission_record(ONEBOT, "10001")
        assert record.source == USER_PERMISSION_SOURCE_MANUAL

    async def test_super_users_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """超级用户（名单匹配 ∧ 来源可信）不登记；来源判定在这里打桩成可信，它自己的用例在 test_super_users_identity_scope.py"""
        from nekro_agent.core.config import config
        from nekro_agent.services.user import super_users as super_users_mod

        async def trusted(*_args: Any) -> bool:
            return True

        monkeypatch.setattr(super_users_mod, "is_trusted_super_user_source", trusted)
        monkeypatch.setattr(config, "SUPER_USERS", ["10001"])
        adapter = _FakeAdapter(CommandPermission.ADVANCED)

        await self._persist(adapter)

        assert adapter.lookups == 0
        assert adapter.granted is None

    async def test_errors_never_escape(self) -> None:
        class _BrokenStore(_FakeAdapter):
            async def set_user_command_permission(self, *args: Any, **kwargs: Any):
                raise OSError("磁盘满")

        await self._persist(_BrokenStore(CommandPermission.ADVANCED))
        await self._persist(_FakeAdapter(raises=True))

    async def test_a_manual_grant_landing_during_the_lookup_is_not_overwritten(self) -> None:
        """查群角色要等协议端；这段时间里平台方刚好手动授了权，写之前要再看一眼"""
        from nekro_agent.services.command.manager import command_manager

        class _GrantDuringLookup(_FakeAdapter):
            async def get_user_command_permission(self, platform_user, platform_channel, platform_message):
                await command_manager.set_user_permission(ONEBOT, platform_user.user_id, CommandPermission.ADVANCED)
                return CommandPermission.ADVANCED

        adapter = _GrantDuringLookup()
        await self._persist(adapter)

        assert adapter.granted is None

    async def test_only_a_freshly_created_user_is_probed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.adapters.interface import collector as collector_mod

        class _FakeChannelRow:
            chat_key = ACME_CHAT
            instance_key = "acme1"
            is_active = True

        class _FakeUserRow:
            is_active = True
            ban_until = None

        async def always(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return True

        async def channel_row(**kwargs: Any) -> _FakeChannelRow:
            del kwargs
            return _FakeChannelRow()

        async def existing_user(**kwargs: Any) -> tuple:
            del kwargs
            return _FakeUserRow(), False

        async def swallow(**kwargs: Any) -> None:
            del kwargs

        monkeypatch.setattr(collector_mod.DBAdapterInstance, "is_instance_active", always)
        monkeypatch.setattr(collector_mod.DBChatChannel, "get_or_create", channel_row)
        monkeypatch.setattr(collector_mod, "register_platform_user", existing_user)
        monkeypatch.setattr(collector_mod.message_service, "push_human_message", swallow)

        adapter = _FakeAdapter(CommandPermission.ADVANCED)
        channel, user, message = inbound()
        message.content_text = "普通聊天"
        await collector_mod.collect_message(adapter, channel, user, message)  # type: ignore[arg-type]

        assert adapter.lookups == 0
        assert adapter.granted is None


class TestStorageFormat:
    """落盘格式按字面断言：round-trip 用例对格式一个字都覆盖不到"""

    async def test_global_file_is_exactly_the_upstream_shape(self, isolated_command_state) -> None:
        from nekro_agent.services.command.manager import command_manager

        await command_manager.set_user_permission(ONEBOT, "10001", CommandPermission.ADVANCED)
        await command_manager.set_user_permission(
            ONEBOT, "10002", CommandPermission.ADVANCED, source="platform", channel_id=GROUP,
        )

        on_disk = json.loads((isolated_command_state / "user_permissions.json").read_text(encoding="utf-8"))
        assert on_disk == {
            "onebot_v11:10001": {"permission": "advanced", "source": "manual", "channel_id": None},
            "onebot_v11:10002": {"permission": "advanced", "source": "platform", "channel_id": GROUP},
        }

    async def test_channel_file_buckets_by_full_chat_key(self, isolated_command_state) -> None:
        from nekro_agent.services.command.manager import command_manager

        await command_manager.set_channel_user_permission(ACME_CHAT, ONEBOT, "10001", CommandPermission.ADVANCED)

        on_disk = json.loads(channel_file(isolated_command_state).read_text(encoding="utf-8"))
        assert on_disk == {
            ACME_CHAT: {"onebot_v11:10001": {"permission": "advanced", "source": "manual", "channel_id": ACME_CHAT}},
        }
        assert not list(isolated_command_state.glob("*.tmp"))

    async def test_last_revoke_removes_the_file(self, isolated_command_state) -> None:
        from nekro_agent.services.command.manager import command_manager

        await command_manager.set_channel_user_permission(ACME_CHAT, ONEBOT, "10001", CommandPermission.ADVANCED)
        await command_manager.set_channel_user_permission(ACME_CHAT, ONEBOT, "10001", CommandPermission.USER)

        assert not channel_file(isolated_command_state).exists()

    async def test_chat_keys_that_sanitize_alike_stay_apart(self) -> None:
        """`a:b` 与 `a_b` 做文件名清洗后相同；按完整 chat_key 分桶不能让它们串"""
        from nekro_agent.services.command.manager import CommandManager

        manager = CommandManager()
        await manager.set_channel_user_permission("wx-u:1", "wx", "10001", CommandPermission.ADVANCED)

        assert manager.get_channel_user_permission("wx-u:1", "wx", "10001") == CommandPermission.ADVANCED
        assert manager.get_channel_user_permission("wx-u_1", "wx", "10001") == CommandPermission.USER

    async def test_a_fresh_manager_reads_back_what_was_written(self) -> None:
        from nekro_agent.services.command.manager import CommandManager

        writer = CommandManager()
        await writer.set_channel_user_permission(ACME_CHAT, ONEBOT, "10001", CommandPermission.ADVANCED)
        await writer.set_user_permission(ONEBOT, "10002", CommandPermission.ADVANCED)

        reader = CommandManager()
        assert reader.get_channel_user_permission(ACME_CHAT, ONEBOT, "10001") == CommandPermission.ADVANCED
        assert reader.get_user_permission(ONEBOT, "10002") == CommandPermission.ADVANCED

    def test_corrupt_files_read_as_empty(self, isolated_command_state) -> None:
        from nekro_agent.services.command.manager import CommandManager

        (isolated_command_state / "user_permissions.json").write_text("{坏", encoding="utf-8")
        channel_file(isolated_command_state).write_text("[1, 2", encoding="utf-8")

        manager = CommandManager()
        assert manager.get_user_permission(ONEBOT, "10001") == CommandPermission.USER
        assert manager.get_channel_user_permission(ACME_CHAT, ONEBOT, "10001") == CommandPermission.USER
        # 读不出来的文件挪开保留，下一次写入不会把它覆盖成只剩新的那一条
        assert len(list(isolated_command_state.glob("user_permissions.json.unreadable-*"))) == 1
        assert len(list(isolated_command_state.glob("channel_user_permissions.json.unreadable-*"))) == 1

    @pytest.mark.parametrize("file_name", ["user_permissions.json", "channel_user_permissions.json"])
    async def test_a_transient_read_error_leaves_the_file_alone_and_refuses_writes(
        self, isolated_command_state, monkeypatch: pytest.MonkeyPatch, file_name: str,
    ) -> None:
        """读不到 ≠ 读坏了：不挪文件、不缓存空状态，读按空，写拒绝（否则会用一条覆盖掉完好的整份）"""
        import pathlib

        from nekro_agent.services.command.manager import CommandManager

        writer = CommandManager()
        await writer.set_user_permission(ONEBOT, "10001", CommandPermission.ADVANCED)
        await writer.set_channel_user_permission(ACME_CHAT, ONEBOT, "10001", CommandPermission.ADVANCED)
        target = isolated_command_state / file_name
        original = target.read_bytes()

        real_read_bytes = pathlib.Path.read_bytes

        def flaky(self: pathlib.Path) -> bytes:
            if self.name == file_name:
                raise PermissionError("还没就绪")
            return real_read_bytes(self)

        monkeypatch.setattr(pathlib.Path, "read_bytes", flaky)
        manager = CommandManager()

        global_unreadable = file_name == "user_permissions.json"
        assert manager.get_user_permission(ONEBOT, "10001") == (
            CommandPermission.USER if global_unreadable else CommandPermission.ADVANCED
        )
        assert manager.get_channel_user_permission(ACME_CHAT, ONEBOT, "10001") == (
            CommandPermission.ADVANCED if global_unreadable else CommandPermission.USER
        )
        with pytest.raises(OSError):
            if global_unreadable:
                await manager.set_user_permission(ONEBOT, "10002", CommandPermission.ADVANCED)
            else:
                await manager.set_channel_user_permission(ACME_CHAT, ONEBOT, "10002", CommandPermission.ADVANCED)

        monkeypatch.setattr(pathlib.Path, "read_bytes", real_read_bytes)
        assert target.read_bytes() == original
        assert not list(isolated_command_state.glob("*.unreadable-*"))
        # 读得到之后不需要重启：没有缓存过空状态
        assert manager.get_user_permission(ONEBOT, "10001") == CommandPermission.ADVANCED
        assert manager.get_channel_user_permission(ACME_CHAT, ONEBOT, "10001") == CommandPermission.ADVANCED

    async def test_the_set_aside_copy_survives_the_next_write(self, isolated_command_state) -> None:
        from nekro_agent.services.command.manager import CommandManager

        (isolated_command_state / "user_permissions.json").write_text('{"onebot_v11:10001": ', encoding="utf-8")

        manager = CommandManager()
        await manager.set_user_permission(ONEBOT, "10002", CommandPermission.ADVANCED)

        backups = list(isolated_command_state.glob("user_permissions.json.unreadable-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == '{"onebot_v11:10001": '

    def test_hand_edited_channel_entries_above_advanced_are_ignored(self, isolated_command_state) -> None:
        from nekro_agent.services.command.manager import CommandManager

        channel_file(isolated_command_state).write_text(
            json.dumps({
                ACME_CHAT: {
                    "onebot_v11:10001": {"permission": "super_user", "source": "manual", "channel_id": ACME_CHAT},
                    "onebot_v11:10002": {"permission": "advanced", "source": "manual", "channel_id": ACME_CHAT},
                },
            }),
            encoding="utf-8",
        )

        manager = CommandManager()
        assert manager.get_channel_user_permission(ACME_CHAT, ONEBOT, "10001") == CommandPermission.USER
        assert manager.get_channel_user_permission(ACME_CHAT, ONEBOT, "10002") == CommandPermission.ADVANCED


class TestOneBotAsksTheReceivingInstance:
    async def test_group_role_is_queried_through_the_instance_that_received_the_message(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.adapters.onebot_v11 import adapter as onebot_adapter_mod
        from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

        requested: List[str] = []

        class _Bot:
            async def get_group_member_info(self, **_kwargs: Any) -> Dict[str, str]:
                return {"role": "admin"}

        async def fake_get_bot_by_chat_key(chat_key: str) -> _Bot:
            requested.append(chat_key)
            return _Bot()

        def no_contextual_bot(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("不该走无参 get_bot()：多账号部署里它可能是别的实例")

        async def detection_on(chat_key: str) -> bool:
            del chat_key
            return True

        from nekro_agent.services.command import manager as manager_mod

        monkeypatch.setattr(onebot_adapter_mod, "get_bot_by_chat_key", fake_get_bot_by_chat_key)
        monkeypatch.setattr(onebot_adapter_mod, "get_bot", no_contextual_bot)
        monkeypatch.setattr(manager_mod, "is_group_admin_detection_enabled", detection_on)

        adapter = OnebotV11Adapter.__new__(OnebotV11Adapter)
        permission = await adapter.get_user_command_permission(
            PlatformUser(platform_name="qq", user_id="10001", user_name="Admin"),
            PlatformChannel(channel_id=GROUP, channel_name="", channel_type=ChatType.GROUP, instance_key="rival1"),
            PlatformMessage(message_id="1", sender_id="10001", sender_name="Admin"),
        )

        assert permission == CommandPermission.ADVANCED
        assert requested == [RIVAL_CHAT]


def _recording_bot(monkeypatch: pytest.MonkeyPatch, role: str = "admin") -> List[str]:
    from nekro_agent.adapters.onebot_v11 import adapter as onebot_adapter_mod

    requested: List[str] = []

    class _Bot:
        async def get_group_member_info(self, **_kwargs: Any) -> Dict[str, str]:
            return {"role": role}

    async def fake_get_bot_by_chat_key(chat_key: str) -> _Bot:
        requested.append(chat_key)
        return _Bot()

    monkeypatch.setattr(onebot_adapter_mod, "get_bot_by_chat_key", fake_get_bot_by_chat_key)
    return requested


async def onebot_permission(instance_key: str = "acme1") -> CommandPermission:
    from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

    adapter = OnebotV11Adapter.__new__(OnebotV11Adapter)
    return await adapter.get_user_command_permission(
        PlatformUser(platform_name="qq", user_id="10001", user_name="群管理"),
        PlatformChannel(channel_id=GROUP, channel_name="", channel_type=ChatType.GROUP, instance_key=instance_key),
        PlatformMessage(message_id="1", sender_id="10001", sender_name="群管理"),
    )


class TestGroupRoleDetectionSwitch:
    """群主 / 群管理员自动成为频道管理员：默认关，可按实例 / 频道打开"""

    def test_field_is_overridable_and_off_by_default(self) -> None:
        from nekro_agent.core.config import CoreConfig

        field = CoreConfig.model_fields["COMMAND_GROUP_ADMIN_AS_CHANNEL_ADMIN"]
        assert field.default is False
        assert isinstance(field.json_schema_extra, dict)
        assert field.json_schema_extra["overridable"] is True

    async def test_off_means_group_admins_are_plain_users_and_nobody_is_asked(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        requested = _recording_bot(monkeypatch)

        assert await onebot_permission() == CommandPermission.USER
        assert requested == []

    async def test_on_restores_the_upstream_detection(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config

        monkeypatch.setattr(config, "COMMAND_GROUP_ADMIN_AS_CHANNEL_ADMIN", True)
        requested = _recording_bot(monkeypatch)

        assert await onebot_permission() == CommandPermission.ADVANCED
        assert requested == [ACME_CHAT]

    async def test_manual_grant_does_not_need_the_switch(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.command.manager import command_manager

        await command_manager.set_user_permission(ONEBOT, "10001", CommandPermission.ADVANCED)
        requested = _recording_bot(monkeypatch, role="member")

        assert await onebot_permission() == CommandPermission.ADVANCED
        assert requested == []

    async def test_a_tenant_switching_it_on_affects_only_its_own_instance(
        self, db, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """走真实的配置写入口：租户 A 在自己实例的覆盖层打开，只有 A 的频道生效；B 的实例层、频道层、
        共用的适配器层，A 都写不进去

        覆盖配置文件的落盘路径按调用时的 `OsEnv.DATA_DIR` 计算，这里改道到临时目录，并断言文件
        确实落在那里——否则这条用例会改写开发机上的真实覆盖配置。
        """
        import nekro_agent.adapters  # noqa: F401  适配器注册表
        from nekro_agent.core.core_utils import ConfigManager
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.routers import config as config_router
        from nekro_agent.schemas.errors import PermissionDeniedError
        from nekro_agent.services.command.manager import is_group_admin_detection_enabled
        from nekro_agent.services.config_resolver import config_resolver
        from nekro_agent.services.config_service import instance_override_config_key, instance_override_config_path

        data_dir = tmp_path / "data"
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        keys = [instance_override_config_key(ONEBOT, name) for name in ("acme1", "rival1")]
        keys += [f"adapter_override_{ONEBOT}", f"channel_config_{ACME_CHAT}", f"channel_config_{RIVAL_CHAT}"]
        for key in keys:
            ConfigManager.unregister_config(key)
        config_resolver.bump_version()
        admin = acme_admin()
        admin.id = 101
        body = config_router.BatchUpdateConfig(
            configs={"enable_COMMAND_GROUP_ADMIN_AS_CHANNEL_ADMIN": "true", "COMMAND_GROUP_ADMIN_AS_CHANNEL_ADMIN": "true"},
        )
        try:
            await config_router.update_instance_config(
                adapter_key=ONEBOT, instance_key="acme1", body=body, _current_user=admin,
            )
            config_resolver.bump_version()

            assert instance_override_config_path(ONEBOT, "acme1").resolve().is_relative_to(data_dir.resolve())
            assert instance_override_config_path(ONEBOT, "acme1").exists()
            assert await is_group_admin_detection_enabled(ACME_CHAT) is True
            assert await is_group_admin_detection_enabled(RIVAL_CHAT) is False

            with pytest.raises(NotFoundError):
                await config_router.update_instance_config(
                    adapter_key=ONEBOT, instance_key="rival1", body=body, _current_user=admin,
                )
            with pytest.raises(NotFoundError):
                await config_router.batch_update_config(
                    config_key=f"channel_config_{RIVAL_CHAT}", body=body, _current_user=admin,
                )
            with pytest.raises(PermissionDeniedError):
                await config_router.batch_update_config(
                    config_key=f"adapter_override_{ONEBOT}", body=body, _current_user=admin,
                )
            config_resolver.bump_version()
            assert await is_group_admin_detection_enabled(RIVAL_CHAT) is False
        finally:
            for key in keys:
                ConfigManager.unregister_config(key)
            config_resolver.bump_version()

    async def test_resolution_failure_counts_as_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.command.manager import is_group_admin_detection_enabled
        from nekro_agent.services.config_resolver import config_resolver

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("配置层读不出来")

        monkeypatch.setattr(config_resolver, "get_effective_config", broken)

        assert await is_group_admin_detection_enabled(ACME_CHAT) is False

    async def test_platform_snapshot_is_ignored_by_the_base_default(self) -> None:
        """平台来源的记录只是快照：照上游写法「是群管理给 ADVANCED，否则 super()」的适配器不能靠它放行"""
        from nekro_agent.services.command.manager import command_manager

        await command_manager.set_user_permission(
            ONEBOT, "10001", CommandPermission.ADVANCED, source="platform", channel_id=GROUP,
        )
        channel, user, message = inbound()

        permission = await BaseAdapter.get_user_command_permission(
            SimpleNamespace(key=ONEBOT),  # type: ignore[arg-type]
            user,
            channel,
            message,
        )
        assert permission == CommandPermission.USER


CHANNEL_COMMANDS = ["reset", "stop", "na_on", "na_off", "na_observe"]


def _channel_command(name: str) -> Any:
    from nekro_agent.services.command.built_in.chat import ResetCommand, StopCommand
    from nekro_agent.services.command.built_in.switch import NaObserveCommand, NaOffCommand, NaOnCommand

    return {
        "reset": ResetCommand,
        "stop": StopCommand,
        "na_on": NaOnCommand,
        "na_off": NaOffCommand,
        "na_observe": NaObserveCommand,
    }[name]()


class TestEveryChannelCommandStaysInItsChannel:
    """五条降到 ADVANCED 的命令，就是频道管理员能碰到的全部：每一条都不能伸出当前频道"""

    @staticmethod
    async def _snapshot() -> List[tuple]:
        rows = await DBChatChannel.all().order_by("chat_key")
        return [(row.chat_key, row.is_active, row.observe_mode, row.conversation_start_time) for row in rows]

    @pytest.fixture
    def cancelled(self, monkeypatch: pytest.MonkeyPatch) -> List[str]:
        from nekro_agent.services.message_service import message_service

        calls: List[str] = []

        async def fake_cancel(chat_key: str) -> bool:
            calls.append(chat_key)
            return True

        monkeypatch.setattr(message_service, "cancel_agent_task", fake_cancel)
        return calls

    @pytest.mark.parametrize("name", CHANNEL_COMMANDS)
    @pytest.mark.parametrize("target", ["*", "group_*", "private_*", RIVAL_CHAT])
    async def test_advanced_user_cannot_reach_beyond_the_current_channel(
        self, db, cancelled: List[str], name: str, target: str,
    ) -> None:
        before = await self._snapshot()

        response = await _channel_command(name).execute(advanced_context(), target)

        assert response.status == CommandResponseStatus.ERROR
        assert await self._snapshot() == before
        assert cancelled == []

    @pytest.mark.parametrize("name", CHANNEL_COMMANDS)
    async def test_the_current_channel_is_still_reachable(self, db, cancelled: List[str], name: str) -> None:
        """对照：不带目标时操作的是当前频道，另一个租户的频道原样不动"""
        rival_before = [row for row in await self._snapshot() if row[0] == RIVAL_CHAT]

        response = await _channel_command(name).execute(advanced_context(), "")

        assert response.status == CommandResponseStatus.SUCCESS
        assert [row for row in await self._snapshot() if row[0] == RIVAL_CHAT] == rival_before
        assert cancelled == ([ACME_CHAT] if name == "stop" else [])


class TestLegacyAdvancedOverrideMigration:
    """升级前写下的 `advanced` 覆盖按当时的实际效果（只有超级用户）改写，只做一次"""

    async def test_existing_advanced_overrides_become_super_user(self, isolated_command_state) -> None:
        from nekro_agent.services.command.manager import CommandManager

        (isolated_command_state / "system_permissions.json").write_text(
            json.dumps({"quota_set": "advanced", "na_help": "public"}), encoding="utf-8",
        )
        channel_dir = isolated_command_state / "channel_permissions"
        channel_dir.mkdir(exist_ok=True)
        (channel_dir / "onebot_v11-acme1-group_5000.json").write_text(
            json.dumps({"plugin_ctl": "advanced", "reset": "user"}), encoding="utf-8",
        )

        migrated = CommandManager().migrate_legacy_advanced_overrides()

        assert sorted(migrated) == [
            "channel_permissions/onebot_v11-acme1-group_5000.json:plugin_ctl",
            "system:quota_set",
        ]
        assert json.loads((isolated_command_state / "system_permissions.json").read_text(encoding="utf-8")) == {
            "quota_set": "super_user",
            "na_help": "public",
        }
        assert json.loads((channel_dir / "onebot_v11-acme1-group_5000.json").read_text(encoding="utf-8")) == {
            "plugin_ctl": "super_user",
            "reset": "user",
        }

    async def test_runs_only_once(self, isolated_command_state) -> None:
        """标记之后平台方新写的 advanced 是新含义，不能再被改掉"""
        from nekro_agent.services.command.manager import CommandManager

        CommandManager().migrate_legacy_advanced_overrides()
        (isolated_command_state / "system_permissions.json").write_text(
            json.dumps({"quota_set": "advanced"}), encoding="utf-8",
        )

        assert CommandManager().migrate_legacy_advanced_overrides() == []
        assert json.loads((isolated_command_state / "system_permissions.json").read_text(encoding="utf-8")) == {
            "quota_set": "advanced",
        }

    async def test_a_fresh_install_just_leaves_the_marker(self, isolated_command_state) -> None:
        from nekro_agent.services.command.manager import CommandManager

        assert CommandManager().migrate_legacy_advanced_overrides() == []
        assert (isolated_command_state / "permission_semantics.json").exists()

    async def test_an_unreadable_channel_file_is_skipped_not_fatal(self, isolated_command_state) -> None:
        from nekro_agent.services.command.manager import CommandManager

        channel_dir = isolated_command_state / "channel_permissions"
        channel_dir.mkdir(exist_ok=True)
        (channel_dir / "bad.json").write_bytes(b"\xff\xfe")
        (channel_dir / "good.json").write_text(json.dumps({"na_on": "advanced"}), encoding="utf-8")

        assert CommandManager().migrate_legacy_advanced_overrides() == ["channel_permissions/good.json:na_on"]

    async def test_an_unreadable_file_is_retried_after_it_is_fixed(self, isolated_command_state) -> None:
        """读不出来的文件不改、留在标记的 pending 里；人工修好之后下次启动照样迁移，其余文件不再碰"""
        from nekro_agent.services.command.manager import CommandManager

        system_file = isolated_command_state / "system_permissions.json"
        system_file.write_bytes(b'{"quota_set": "advanced"')  # 截断
        channel_dir = isolated_command_state / "channel_permissions"
        channel_dir.mkdir(exist_ok=True)
        good = channel_dir / "good.json"
        good.write_text(json.dumps({"na_on": "advanced"}), encoding="utf-8")

        assert CommandManager().migrate_legacy_advanced_overrides() == ["channel_permissions/good.json:na_on"]
        marker = json.loads((isolated_command_state / "permission_semantics.json").read_text(encoding="utf-8"))
        assert marker["pending"] == ["system"]

        # 迁过之后平台方按新含义写的 advanced，以及人工修好的系统级文件
        good.write_text(json.dumps({"na_on": "advanced"}), encoding="utf-8")
        system_file.write_text(json.dumps({"quota_set": "advanced"}), encoding="utf-8")

        assert CommandManager().migrate_legacy_advanced_overrides() == ["system:quota_set"]
        assert json.loads(system_file.read_text(encoding="utf-8")) == {"quota_set": "super_user"}
        assert json.loads(good.read_text(encoding="utf-8")) == {"na_on": "advanced"}
        marker = json.loads((isolated_command_state / "permission_semantics.json").read_text(encoding="utf-8"))
        assert marker["pending"] == []
        assert sorted(marker["migrated"]) == ["channel_permissions/good.json:na_on", "system:quota_set"]

        assert CommandManager().migrate_legacy_advanced_overrides() == []

    def test_an_unreadable_marker_counts_as_done(self, isolated_command_state) -> None:
        """宁可少迁，也不去改写可能已是新含义的覆盖"""
        from nekro_agent.services.command.manager import CommandManager

        (isolated_command_state / "permission_semantics.json").write_bytes(b"\xff")
        (isolated_command_state / "system_permissions.json").write_text(
            json.dumps({"quota_set": "advanced"}), encoding="utf-8",
        )

        assert CommandManager().migrate_legacy_advanced_overrides() == []
        assert json.loads((isolated_command_state / "system_permissions.json").read_text(encoding="utf-8")) == {
            "quota_set": "advanced",
        }

    def test_a_marker_write_failure_is_logged_not_raised(self, isolated_command_state, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.command import manager as manager_mod

        real_write = manager_mod._write_json_atomic

        def refuse_marker(path, data):
            if path.name == manager_mod.PERMISSION_SEMANTICS_MARKER_NAME:
                raise OSError("磁盘满")
            real_write(path, data)

        monkeypatch.setattr(manager_mod, "_write_json_atomic", refuse_marker)
        (isolated_command_state / "system_permissions.json").write_text(
            json.dumps({"quota_set": "advanced"}), encoding="utf-8",
        )

        assert manager_mod.CommandManager().migrate_legacy_advanced_overrides() == ["system:quota_set"]
        assert not (isolated_command_state / "permission_semantics.json").exists()

    def test_startup_calls_it_right_after_registering_built_in_commands(self) -> None:
        import inspect

        import nekro_agent

        source = inspect.getsource(nekro_agent)
        register_at = source.index("register_built_in_commands()")
        migrate_at = source.index("migrate_legacy_advanced_overrides()")
        assert register_at < migrate_at < source.index("await init_plugins()")


class TestChannelGrantLimit:
    async def test_a_channel_holds_at_most_the_limit(self) -> None:
        from nekro_agent.services.command.manager import CHANNEL_USER_PERMISSION_LIMIT, command_manager

        for index in range(CHANNEL_USER_PERMISSION_LIMIT):
            await command_manager.set_channel_user_permission(ACME_CHAT, ONEBOT, f"u{index}", CommandPermission.ADVANCED)

        with pytest.raises(ValidationError):
            await command_manager.set_channel_user_permission(ACME_CHAT, ONEBOT, "one-too-many", CommandPermission.ADVANCED)

        # 已有的人重复授予、撤销、以及别的频道都不受影响
        await command_manager.set_channel_user_permission(ACME_CHAT, ONEBOT, "u0", CommandPermission.ADVANCED)
        await command_manager.set_channel_user_permission(ACME_CHAT, ONEBOT, "u1", CommandPermission.USER)
        await command_manager.set_channel_user_permission(RIVAL_CHAT, ONEBOT, "one-too-many", CommandPermission.ADVANCED)
        assert len(command_manager.list_channel_user_permissions(ACME_CHAT, ONEBOT)) == CHANNEL_USER_PERMISSION_LIMIT - 1


class TestAdapterInterfaceShape:
    """三个适配器方法的形状与上游逐字一致：照上游写的子类覆写不能因参数个数报错"""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("get_user_command_permission", ["self", "platform_user", "platform_channel", "platform_message"]),
            ("set_user_command_permission", ["self", "platform_userid", "permission", "source", "channel_id"]),
            ("reset_user_command_permission", ["self", "platform_userid"]),
        ],
    )
    def test_base_signature(self, name: str, expected: List[str]) -> None:
        import inspect

        params = inspect.signature(getattr(BaseAdapter, name)).parameters
        assert list(params) == expected
        if name == "set_user_command_permission":
            assert params["source"].kind is inspect.Parameter.KEYWORD_ONLY
            assert params["source"].default == "manual"
            assert params["channel_id"].kind is inspect.Parameter.KEYWORD_ONLY
            assert params["channel_id"].default is None

    def test_onebot_override_keeps_the_shape(self) -> None:
        import inspect

        from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

        assert list(inspect.signature(OnebotV11Adapter.get_user_command_permission).parameters) == list(
            inspect.signature(BaseAdapter.get_user_command_permission).parameters,
        )


class TestUserManagerDisplay:
    def test_console_accounts_have_no_platform_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import config
        from nekro_agent.routers import user_manager

        monkeypatch.setattr(config, "SUPER_USERS", [""])

        assert user_manager._resolve_effective_command_permission("", "") == CommandPermission.USER

    async def test_stored_super_user_is_shown_as_advanced(self) -> None:
        from nekro_agent.routers import user_manager
        from nekro_agent.services.command.manager import command_manager

        await command_manager.set_user_permission(ONEBOT, "10001", CommandPermission.SUPER_USER)

        assert user_manager._resolve_effective_command_permission(ONEBOT, "10001") == CommandPermission.ADVANCED
