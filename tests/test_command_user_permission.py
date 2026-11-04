"""用户级命令权限（照搬上游 2.4.x 的用例，按本分支形态改写四处）

改写的地方：

1. OneBot 查群角色按本条消息的会话标识取连接（`get_bot_by_chat_key`），上游是无参 `get_bot()`；
2. 本分支的 `collect_message` 前面多了实例闸门、用户登记走 `register_platform_user`，打桩点随之不同；
3. 命令状态文件一律改道到临时目录，`SUPER_USERS` 清空：用例不读写开发机上的真实数据；
4. 群角色识别在本分支要该频道开了开关（默认关），这里统一打开，照上游的语义测识别本身；
   开关关着时的行为在 `test_command_user_permission_tenant_scope.py`。
"""

from typing import Any

import pytest

from nekro_agent.adapters.interface.collector import (
    _persist_registered_user_command_permission,
    _try_handle_command,
    collect_message,
)
from nekro_agent.adapters.interface.schemas.platform import PlatformChannel, PlatformMessage, PlatformUser
from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.schemas.i18n import i18n_text
from nekro_agent.services.command.base import BaseCommand, CommandMetadata, CommandPermission
from nekro_agent.services.command.built_in.user import GrantAdvancedCommand, RevokeAdvancedCommand
from nekro_agent.services.command.schemas import CommandExecutionContext, CommandResponse


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

    async def detection_on(chat_key: str) -> bool:
        del chat_key
        return True

    monkeypatch.setattr(manager_mod, "is_group_admin_detection_enabled", detection_on)
    manager_mod.command_manager.invalidate_cache()
    yield
    manager_mod.command_manager.invalidate_cache()


class _PermissionCommand(BaseCommand):
    def __init__(self, permission: CommandPermission):
        self._permission = permission

    @property
    def metadata(self) -> CommandMetadata:
        return CommandMetadata(
            name=f"test_permission_{self._permission.value}",
            description="test permission",
            i18n_description=i18n_text(zh_CN="测试权限", en_US="Test permission"),
            permission=self._permission,
        )

    async def execute(self, context: CommandExecutionContext, **kwargs: Any) -> CommandResponse:
        del context, kwargs
        raise NotImplementedError


class _FakeAdapter:
    key = "fake"
    record_command_input = False

    def __init__(
        self,
        permission: CommandPermission,
        *,
        command_detected: bool = True,
        wait_consumed: bool = True,
    ):
        self.permission = permission
        self.command_detected = command_detected
        self.wait_consumed = wait_consumed
        self.executed_kwargs: dict[str, Any] | None = None
        self.wait_kwargs: dict[str, Any] | None = None
        self.granted: tuple[str, CommandPermission | str, str, str | None] | None = None
        self.permission_lookup_count = 0

    async def get_user_command_permission(
        self,
        platform_user: PlatformUser,
        platform_channel: PlatformChannel,
        platform_message: PlatformMessage,
    ) -> CommandPermission:
        del platform_user, platform_channel, platform_message
        self.permission_lookup_count += 1
        return self.permission

    def detect_command(self, content_text: str) -> tuple[str, str] | None:
        del content_text
        if not self.command_detected:
            return None
        return "test", "raw"

    async def execute_command(self, **kwargs: Any) -> None:
        self.executed_kwargs = kwargs

    async def try_handle_wait_input(self, **kwargs: Any) -> bool:
        self.wait_kwargs = kwargs
        return self.wait_consumed

    async def set_user_command_permission(
        self,
        platform_userid: str,
        permission: CommandPermission | str,
        *,
        source: str = "manual",
        channel_id: str | None = None,
    ) -> CommandPermission:
        self.granted = (platform_userid, permission, source, channel_id)
        return permission if isinstance(permission, CommandPermission) else CommandPermission(permission)


class _PermissionGrantAdapter:
    def __init__(self):
        self.granted: tuple[str, CommandPermission | str] | None = None
        self.reset_userid: str | None = None

    async def set_user_command_permission(
        self,
        platform_userid: str,
        permission: CommandPermission | str,
    ) -> CommandPermission:
        self.granted = (platform_userid, permission)
        return permission if isinstance(permission, CommandPermission) else CommandPermission(permission)

    async def reset_user_command_permission(self, platform_userid: str) -> None:
        self.reset_userid = platform_userid


class _OneBotRoleBot:
    def __init__(self, role: str):
        self.role = role
        self.calls: list[dict[str, Any]] = []

    async def get_group_member_info(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"role": self.role}


def _patch_onebot_bot(monkeypatch: pytest.MonkeyPatch, bot: _OneBotRoleBot) -> list[str]:
    """按会话标识取连接的打桩；返回被请求过的会话标识，供断言「取的是哪个账号」"""
    from nekro_agent.adapters.onebot_v11 import adapter as onebot_adapter_mod

    requested: list[str] = []

    async def fake_get_bot_by_chat_key(chat_key: str) -> _OneBotRoleBot:
        requested.append(chat_key)
        return bot

    monkeypatch.setattr(onebot_adapter_mod, "get_bot_by_chat_key", fake_get_bot_by_chat_key)
    return requested


def _context(*, is_super_user: bool, is_advanced_user: bool) -> CommandExecutionContext:
    return CommandExecutionContext(
        user_id="platform-user",
        chat_key="fake-channel",
        username="tester",
        adapter_key="fake",
        is_super_user=is_super_user,
        is_advanced_user=is_advanced_user,
    )


@pytest.mark.asyncio
async def test_super_user_can_execute_advanced_permission() -> None:
    command = _PermissionCommand(CommandPermission.ADVANCED)

    ok, err = await command.check_permission(_context(is_super_user=True, is_advanced_user=False))

    assert ok is True
    assert err is None


@pytest.mark.asyncio
async def test_advanced_user_cannot_execute_super_user_permission() -> None:
    command = _PermissionCommand(CommandPermission.SUPER_USER)

    ok, err = await command.check_permission(_context(is_super_user=False, is_advanced_user=True))

    assert ok is False
    assert err is not None


@pytest.mark.asyncio
async def test_command_manager_persists_user_permission() -> None:
    from nekro_agent.services.command import manager as manager_mod

    manager = manager_mod.CommandManager()

    assert manager.get_user_permission("fake", "u1") == CommandPermission.USER

    await manager.set_user_permission("fake", "u1", CommandPermission.ADVANCED)
    assert manager.get_user_permission("fake", "u1") == CommandPermission.ADVANCED

    await manager.set_user_permission(
        "fake",
        "u1",
        CommandPermission.ADVANCED,
        source="platform",
        channel_id="group_1",
    )
    record = manager.get_user_permission_record("fake", "u1")
    assert record.permission == CommandPermission.ADVANCED
    assert record.source == "platform"
    assert record.channel_id == "group_1"

    await manager.reset_user_permission("fake", "u1")
    assert manager.get_user_permission("fake", "u1") == CommandPermission.USER


@pytest.mark.asyncio
async def test_grant_advanced_command_uses_adapter_permission_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.adapters.utils import adapter_utils

    adapter = _PermissionGrantAdapter()
    monkeypatch.setattr(adapter_utils, "get_adapter", lambda adapter_key: adapter)
    command = GrantAdvancedCommand()

    response = await command.execute(_context(is_super_user=True, is_advanced_user=True), "u1", "fake")

    assert response.data == {
        "adapter_key": "fake",
        "platform_userid": "u1",
        "permission": CommandPermission.ADVANCED.value,
    }
    assert adapter.granted == ("u1", CommandPermission.ADVANCED)


@pytest.mark.asyncio
async def test_revoke_advanced_command_uses_adapter_permission_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.adapters.utils import adapter_utils

    adapter = _PermissionGrantAdapter()
    monkeypatch.setattr(adapter_utils, "get_adapter", lambda adapter_key: adapter)
    command = RevokeAdvancedCommand()

    response = await command.execute(_context(is_super_user=True, is_advanced_user=True), "u1", "fake")

    assert response.data == {
        "adapter_key": "fake",
        "platform_userid": "u1",
        "permission": CommandPermission.USER.value,
    }
    assert adapter.reset_userid == "u1"


@pytest.mark.asyncio
async def test_collector_maps_adapter_advanced_permission_to_command_context() -> None:
    adapter = _FakeAdapter(CommandPermission.ADVANCED)
    channel = PlatformChannel(channel_id="channel", channel_name="Channel", channel_type=ChatType.GROUP)
    user = PlatformUser(platform_name="fake", user_id="advanced-user", user_name="Advanced User")
    message = PlatformMessage(
        message_id="msg",
        sender_id="advanced-user",
        sender_name="Advanced User",
        content_text="/test raw",
    )

    consumed = await _try_handle_command(adapter, "fake-channel", channel, user, message, message.content_text)

    assert consumed is True
    assert adapter.executed_kwargs is not None
    assert adapter.executed_kwargs["is_super_user"] is False
    assert adapter.executed_kwargs["is_advanced_user"] is True


@pytest.mark.asyncio
async def test_collector_reuses_adapter_permission_for_wait_input() -> None:
    from nekro_agent.services.command.wait_manager import wait_manager

    adapter = _FakeAdapter(CommandPermission.ADVANCED, command_detected=False)
    channel = PlatformChannel(channel_id="channel", channel_name="Channel", channel_type=ChatType.GROUP)
    user = PlatformUser(platform_name="fake", user_id="advanced-user", user_name="Advanced User")
    message = PlatformMessage(
        message_id="msg",
        sender_id="advanced-user",
        sender_name="Advanced User",
        content_text="confirm",
    )

    await wait_manager.create_session("fake-channel", "advanced-user", "callback")
    try:
        consumed = await _try_handle_command(adapter, "fake-channel", channel, user, message, message.content_text)
    finally:
        wait_manager.cancel("fake-channel", "advanced-user")

    assert consumed is True
    assert adapter.wait_kwargs is not None
    assert adapter.wait_kwargs["is_super_user"] is False
    assert adapter.wait_kwargs["is_advanced_user"] is True


@pytest.mark.asyncio
async def test_collect_message_records_command_input_without_agent_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    """`record_command_input` 为真的适配器：命令原文先入库，再交给命令系统，不触发 Agent

    本分支的 `collect_message` 前面还有实例闸门（`is_inbound_dispatch_allowed`），一并打桩放行。
    """
    from nekro_agent.adapters.interface import collector as collector_mod

    class _FakeDbChatChannel:
        chat_key = "fake-channel"
        instance_key = ""
        is_active = True
        channel_name = "Channel"
        channel_status = "active"
        workspace_id = "workspace"

    async def fake_is_inbound_dispatch_allowed(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return True

    async def fake_get_or_create(**kwargs: Any) -> _FakeDbChatChannel:
        del kwargs
        return _FakeDbChatChannel()

    recorded_messages: list[dict[str, Any]] = []

    async def fake_record_human_message(*args: Any, **kwargs: Any) -> None:
        recorded_messages.append({"args": args, "kwargs": kwargs})

    async def fake_push_human_message(**kwargs: Any) -> None:
        raise AssertionError("command input must not enter normal human-message trigger flow")

    monkeypatch.setattr(collector_mod, "is_inbound_dispatch_allowed", fake_is_inbound_dispatch_allowed)
    monkeypatch.setattr(collector_mod.DBChatChannel, "get_or_create", fake_get_or_create)
    monkeypatch.setattr(collector_mod.message_service, "record_human_message", fake_record_human_message)
    monkeypatch.setattr(collector_mod.message_service, "push_human_message", fake_push_human_message)

    adapter = _FakeAdapter(CommandPermission.ADVANCED, command_detected=True)
    adapter.record_command_input = True
    channel = PlatformChannel(channel_id="channel", channel_name="Channel", channel_type=ChatType.PRIVATE)
    user = PlatformUser(platform_name="fake", user_id="advanced-user", user_name="Advanced User")
    message = PlatformMessage(
        message_id="msg-command",
        sender_id="advanced-user",
        sender_name="Advanced User",
        content_text="/test raw",
        is_tome=True,
    )

    await collect_message(adapter, channel, user, message)

    assert len(recorded_messages) == 1
    recorded_message = recorded_messages[0]["args"][0]
    assert recorded_message.message_id == "msg-command"
    assert recorded_message.content_text == "/test raw"
    assert recorded_message.chat_key == "fake-channel"
    assert recorded_message.instance_key == ""
    assert recorded_messages[0]["kwargs"]["db_chat_channel"].chat_key == "fake-channel"
    assert adapter.executed_kwargs is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_factory", ["record_command_input_false", "no_attribute"], ids=["attr_false", "no_attr"])
async def test_collect_message_does_not_record_command_input_when_adapter_does_not_ask(
    monkeypatch: pytest.MonkeyPatch,
    adapter_factory: str,
) -> None:
    """`record_command_input` 为假、或适配器根本没有这个属性时，一律不调 `record_human_message`"""
    from nekro_agent.adapters.interface import collector as collector_mod

    class _FakeDbChatChannel:
        chat_key = "fake-channel"
        instance_key = ""
        is_active = True
        channel_name = "Channel"
        channel_status = "active"
        workspace_id = "workspace"

    async def fake_is_inbound_dispatch_allowed(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return True

    async def fake_get_or_create(**kwargs: Any) -> _FakeDbChatChannel:
        del kwargs
        return _FakeDbChatChannel()

    recorded_messages: list[dict[str, Any]] = []

    async def fake_record_human_message(*args: Any, **kwargs: Any) -> None:
        recorded_messages.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(collector_mod, "is_inbound_dispatch_allowed", fake_is_inbound_dispatch_allowed)
    monkeypatch.setattr(collector_mod.DBChatChannel, "get_or_create", fake_get_or_create)
    monkeypatch.setattr(collector_mod.message_service, "record_human_message", fake_record_human_message)

    adapter = _FakeAdapter(CommandPermission.ADVANCED, command_detected=True)
    if adapter_factory == "no_attribute":
        # 复制一份类字典再构造（去掉 `record_command_input`），避免删掉共享类上的属性污染后续用例
        no_attr_attrs = {key: value for key, value in _FakeAdapter.__dict__.items() if key != "record_command_input"}
        no_attr_cls = type("_NoRecordCommandInputAdapter", (), no_attr_attrs)
        adapter = no_attr_cls(CommandPermission.ADVANCED, command_detected=True)
        assert not hasattr(adapter, "record_command_input")
    else:
        adapter.record_command_input = False

    channel = PlatformChannel(channel_id="channel", channel_name="Channel", channel_type=ChatType.PRIVATE)
    user = PlatformUser(platform_name="fake", user_id="advanced-user", user_name="Advanced User")
    message = PlatformMessage(
        message_id="msg-command",
        sender_id="advanced-user",
        sender_name="Advanced User",
        content_text="/test raw",
        is_tome=True,
    )

    await collect_message(adapter, channel, user, message)

    assert recorded_messages == []
    assert adapter.executed_kwargs is not None


@pytest.mark.asyncio
async def test_record_human_message_skips_persist_when_instance_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """实例已停用时不落库：与 `push_human_message` 同一道闸门"""
    from nekro_agent.services import message_service as message_service_mod
    from nekro_agent.services.message_service import message_service

    class _FakeDbChatChannel:
        adapter_key = "fake"
        instance_key = ""
        chat_key = "fake-channel"
        channel_name = "Channel"
        channel_status = "active"
        is_active = True
        workspace_id = None

    persisted: list[Any] = []

    async def fake_is_instance_active(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return False

    async def fake_persist(*args: Any, **kwargs: Any) -> None:
        persisted.append((args, kwargs))

    monkeypatch.setattr(message_service_mod.DBAdapterInstance, "is_instance_active", fake_is_instance_active)
    monkeypatch.setattr(message_service, "_persist_human_message", fake_persist)

    message = ChatMessage.create_empty("fake-channel")
    await message_service.record_human_message(message, db_chat_channel=_FakeDbChatChannel())  # type: ignore[arg-type]

    assert persisted == []


@pytest.mark.asyncio
async def test_record_human_message_persists_when_instance_active(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services import message_service as message_service_mod
    from nekro_agent.services.message_service import message_service

    class _FakeDbChatChannel:
        adapter_key = "fake"
        instance_key = ""
        chat_key = "fake-channel"
        channel_name = "Channel"
        channel_status = "active"
        is_active = True
        workspace_id = None

    persisted: list[Any] = []

    async def fake_is_instance_active(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return True

    async def fake_persist(*args: Any, **kwargs: Any) -> None:
        persisted.append((args, kwargs))

    monkeypatch.setattr(message_service_mod.DBAdapterInstance, "is_instance_active", fake_is_instance_active)
    monkeypatch.setattr(message_service, "_persist_human_message", fake_persist)

    channel = _FakeDbChatChannel()
    message = ChatMessage.create_empty("fake-channel")
    await message_service.record_human_message(message, db_chat_channel=channel)  # type: ignore[arg-type]

    assert persisted == [((message, channel), {})]


@pytest.mark.asyncio
async def test_collector_skips_permission_lookup_for_plain_message_without_wait() -> None:
    adapter = _FakeAdapter(CommandPermission.ADVANCED, command_detected=False, wait_consumed=False)
    channel = PlatformChannel(channel_id="channel", channel_name="Channel", channel_type=ChatType.GROUP)
    user = PlatformUser(platform_name="fake", user_id="advanced-user", user_name="Advanced User")
    message = PlatformMessage(
        message_id="msg",
        sender_id="advanced-user",
        sender_name="Advanced User",
        content_text="plain message",
    )

    consumed = await _try_handle_command(adapter, "fake-channel", channel, user, message, message.content_text)

    assert consumed is False
    assert adapter.permission_lookup_count == 0
    assert adapter.wait_kwargs is None


@pytest.mark.asyncio
async def test_registered_user_adapter_permission_is_persisted_as_platform_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nekro_agent.adapters.interface import collector as collector_mod
    from nekro_agent.services.command.manager import USER_PERMISSION_SOURCE_PLATFORM

    class _FakeDbChatChannel:
        chat_key = "fake-channel"
        instance_key = ""
        is_active = True
        channel_name = "Channel"
        channel_status = "active"
        workspace_id = "workspace"

    class _FakeDbUser:
        is_active = True
        is_prevent_trigger = False
        ban_until = None

    async def fake_is_instance_active(*args: Any, **kwargs: Any) -> bool:
        del args, kwargs
        return True

    async def fake_get_or_create(**kwargs: Any) -> _FakeDbChatChannel:
        del kwargs
        return _FakeDbChatChannel()

    async def fake_register_platform_user(**kwargs: Any) -> tuple[_FakeDbUser, bool]:
        del kwargs
        return _FakeDbUser(), True

    pushed_messages: list[Any] = []

    async def fake_push_human_message(**kwargs: Any) -> None:
        pushed_messages.append(kwargs)

    monkeypatch.setattr(collector_mod.DBAdapterInstance, "is_instance_active", fake_is_instance_active)
    monkeypatch.setattr(collector_mod.DBChatChannel, "get_or_create", fake_get_or_create)
    monkeypatch.setattr(collector_mod, "register_platform_user", fake_register_platform_user)
    monkeypatch.setattr(collector_mod.message_service, "push_human_message", fake_push_human_message)

    adapter = _FakeAdapter(CommandPermission.ADVANCED, command_detected=False, wait_consumed=False)
    channel = PlatformChannel(channel_id="channel", channel_name="Channel", channel_type=ChatType.GROUP)
    user = PlatformUser(platform_name="fake", user_id="advanced-user", user_name="Advanced User")
    message = PlatformMessage(
        message_id="msg",
        sender_id="advanced-user",
        sender_name="Advanced User",
        content_text="hello",
    )

    await collect_message(adapter, channel, user, message)  # type: ignore[arg-type]

    assert adapter.granted == ("advanced-user", CommandPermission.ADVANCED, USER_PERMISSION_SOURCE_PLATFORM, "channel")
    assert pushed_messages


@pytest.mark.asyncio
async def test_platform_permission_registration_clamps_super_user_to_advanced() -> None:
    from nekro_agent.services.command.manager import USER_PERMISSION_SOURCE_PLATFORM

    adapter = _FakeAdapter(CommandPermission.SUPER_USER, command_detected=False)
    channel = PlatformChannel(channel_id="channel", channel_name="Channel", channel_type=ChatType.GROUP)
    user = PlatformUser(platform_name="fake", user_id="platform-owner", user_name="Platform Owner")
    message = PlatformMessage(
        message_id="msg",
        sender_id="platform-owner",
        sender_name="Platform Owner",
        content_text="hello",
    )

    await _persist_registered_user_command_permission(adapter, channel, user, message)  # type: ignore[arg-type]

    assert adapter.granted == ("platform-owner", CommandPermission.ADVANCED, USER_PERMISSION_SOURCE_PLATFORM, "channel")


def test_user_manager_super_users_override_persisted_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.core.config import config
    from nekro_agent.routers import user_manager

    monkeypatch.setattr(config, "SUPER_USERS", ["10086"])
    monkeypatch.setattr(
        user_manager.command_manager,
        "get_user_permission",
        lambda adapter_key, platform_userid: CommandPermission.ADVANCED,
    )

    permission = user_manager._resolve_effective_command_permission("onebot_v11", "10086")

    assert permission == CommandPermission.SUPER_USER


@pytest.mark.asyncio
async def test_onebot_group_admin_gets_advanced_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.command import manager as manager_mod
    from nekro_agent.services.command.manager import UserPermissionRecord

    bot = _OneBotRoleBot("admin")
    requested = _patch_onebot_bot(monkeypatch, bot)
    monkeypatch.setattr(
        manager_mod.command_manager,
        "get_user_permission_record",
        lambda adapter_key, platform_userid: UserPermissionRecord(CommandPermission.USER),
    )
    adapter = OnebotV11Adapter.__new__(OnebotV11Adapter)

    permission = await adapter.get_user_command_permission(
        PlatformUser(platform_name="qq", user_id="10001", user_name="Admin"),
        PlatformChannel(channel_id="group_20002", channel_name="Group", channel_type=ChatType.GROUP),
        PlatformMessage(message_id="msg", sender_id="10001", sender_name="Admin"),
    )

    assert permission == CommandPermission.ADVANCED
    assert bot.calls == [{"group_id": 20002, "user_id": 10001, "no_cache": False}]
    assert requested == ["onebot_v11-group_20002"]


@pytest.mark.asyncio
async def test_onebot_group_owner_gets_advanced_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.command import manager as manager_mod
    from nekro_agent.services.command.manager import UserPermissionRecord

    _patch_onebot_bot(monkeypatch, _OneBotRoleBot("owner"))
    monkeypatch.setattr(
        manager_mod.command_manager,
        "get_user_permission_record",
        lambda adapter_key, platform_userid: UserPermissionRecord(CommandPermission.USER),
    )
    adapter = OnebotV11Adapter.__new__(OnebotV11Adapter)

    permission = await adapter.get_user_command_permission(
        PlatformUser(platform_name="qq", user_id="10001", user_name="Owner"),
        PlatformChannel(channel_id="group_20002", channel_name="Group", channel_type=ChatType.GROUP),
        PlatformMessage(message_id="msg", sender_id="10001", sender_name="Owner"),
    )

    assert permission == CommandPermission.ADVANCED


@pytest.mark.asyncio
async def test_onebot_group_member_keeps_user_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.command import manager as manager_mod
    from nekro_agent.services.command.manager import UserPermissionRecord

    _patch_onebot_bot(monkeypatch, _OneBotRoleBot("member"))
    monkeypatch.setattr(
        manager_mod.command_manager,
        "get_user_permission_record",
        lambda adapter_key, platform_userid: UserPermissionRecord(CommandPermission.USER),
    )
    adapter = OnebotV11Adapter.__new__(OnebotV11Adapter)

    permission = await adapter.get_user_command_permission(
        PlatformUser(platform_name="qq", user_id="10001", user_name="Member"),
        PlatformChannel(channel_id="group_20002", channel_name="Group", channel_type=ChatType.GROUP),
        PlatformMessage(message_id="msg", sender_id="10001", sender_name="Member"),
    )

    assert permission == CommandPermission.USER


@pytest.mark.asyncio
async def test_onebot_private_chat_does_not_query_group_role(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.command import manager as manager_mod
    from nekro_agent.services.command.manager import UserPermissionRecord

    bot = _OneBotRoleBot("owner")
    _patch_onebot_bot(monkeypatch, bot)
    monkeypatch.setattr(
        manager_mod.command_manager,
        "get_user_permission_record",
        lambda adapter_key, platform_userid: UserPermissionRecord(CommandPermission.USER),
    )
    adapter = OnebotV11Adapter.__new__(OnebotV11Adapter)

    permission = await adapter.get_user_command_permission(
        PlatformUser(platform_name="qq", user_id="10001", user_name="Private User"),
        PlatformChannel(channel_id="private_10001", channel_name="Private", channel_type=ChatType.PRIVATE),
        PlatformMessage(message_id="msg", sender_id="10001", sender_name="Private User"),
    )

    assert permission == CommandPermission.USER
    assert bot.calls == []


@pytest.mark.asyncio
async def test_onebot_manual_advanced_permission_works_in_private_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.command import manager as manager_mod
    from nekro_agent.services.command.manager import UserPermissionRecord

    bot = _OneBotRoleBot("member")
    _patch_onebot_bot(monkeypatch, bot)
    monkeypatch.setattr(
        manager_mod.command_manager,
        "get_user_permission_record",
        lambda adapter_key, platform_userid: UserPermissionRecord(CommandPermission.ADVANCED, source="manual"),
    )
    adapter = OnebotV11Adapter.__new__(OnebotV11Adapter)

    permission = await adapter.get_user_command_permission(
        PlatformUser(platform_name="qq", user_id="10001", user_name="Private User"),
        PlatformChannel(channel_id="private_10001", channel_name="Private", channel_type=ChatType.PRIVATE),
        PlatformMessage(message_id="msg", sender_id="10001", sender_name="Private User"),
    )

    assert permission == CommandPermission.ADVANCED
    assert bot.calls == []


@pytest.mark.asyncio
async def test_onebot_platform_permission_is_cleared_when_group_role_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nekro_agent.services.command import manager as manager_mod
    from nekro_agent.services.command.manager import UserPermissionRecord

    reset_calls: list[dict[str, Any]] = []

    async def fake_reset_user_permission(*args: Any, **kwargs: Any) -> None:
        reset_calls.append({"args": args, "kwargs": kwargs})

    _patch_onebot_bot(monkeypatch, _OneBotRoleBot("member"))
    monkeypatch.setattr(
        manager_mod.command_manager,
        "get_user_permission_record",
        lambda adapter_key, platform_userid: UserPermissionRecord(
            CommandPermission.ADVANCED,
            source="platform",
            channel_id="group_20002",
        ),
    )
    monkeypatch.setattr(manager_mod.command_manager, "reset_user_permission", fake_reset_user_permission)
    adapter = OnebotV11Adapter.__new__(OnebotV11Adapter)

    permission = await adapter.get_user_command_permission(
        PlatformUser(platform_name="qq", user_id="10001", user_name="Member"),
        PlatformChannel(channel_id="group_20002", channel_name="Group", channel_type=ChatType.GROUP),
        PlatformMessage(message_id="msg", sender_id="10001", sender_name="Member"),
    )

    assert permission == CommandPermission.USER
    assert reset_calls == [{
        "args": ("onebot_v11", "10001"),
        "kwargs": {"source": "platform", "channel_id": "group_20002"},
    }]


@pytest.mark.asyncio
async def test_onebot_platform_permission_does_not_cross_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.command import manager as manager_mod
    from nekro_agent.services.command.manager import UserPermissionRecord

    bot = _OneBotRoleBot("member")
    _patch_onebot_bot(monkeypatch, bot)
    monkeypatch.setattr(
        manager_mod.command_manager,
        "get_user_permission_record",
        lambda adapter_key, platform_userid: UserPermissionRecord(
            CommandPermission.ADVANCED,
            source="platform",
            channel_id="group_20003",
        ),
    )
    adapter = OnebotV11Adapter.__new__(OnebotV11Adapter)

    permission = await adapter.get_user_command_permission(
        PlatformUser(platform_name="qq", user_id="10001", user_name="Owner"),
        PlatformChannel(channel_id="group_20002", channel_name="Group", channel_type=ChatType.GROUP),
        PlatformMessage(message_id="msg", sender_id="10001", sender_name="Owner"),
    )

    assert permission == CommandPermission.USER
    assert bot.calls == [{"group_id": 20002, "user_id": 10001, "no_cache": False}]
