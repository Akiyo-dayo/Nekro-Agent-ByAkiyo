"""第三方插件替换框架函数时的兼容契约

市场里的插件会直接替换框架的内部函数，并照上游 NekroAgent 的签名写成固定参数的包装函数。
本分支曾经比上游少一个参数：命令钩子 `_try_handle_command` 在上游是 6 个位置参数，这里是 5 个。
结果是装上这类插件以后，**所有频道**带文字的消息都在 `collect_message` 里抛 `TypeError`，
消息不入库、Bot 不回复，而不只是这个插件自己失灵。

这里钉住三件事：

1. 消息入口按上游的 6 参形状调用命令钩子，照上游写的包装函数能接住；
2. 包装函数的签名接不住时退回框架实现：命令照常执行，只告警一次；
3. 包装函数体内自己抛出的 `TypeError` 原样冒出来，不被当成签名不符吞掉。

外加 `get_chat_info_old` 的旧签名：上游只收 `event`，插件照那个形状调用。
"""

import json
from typing import Any, List, Optional, Tuple

import pytest
from nonebot.adapters.onebot.v11 import GroupIncreaseNoticeEvent
from tortoise import Tortoise

from nekro_agent.adapters.interface import collector
from nekro_agent.adapters.interface.schemas.platform import PlatformChannel, PlatformMessage, PlatformUser
from nekro_agent.adapters.onebot_v11.tools import onebot_util
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.chat_message import ChatType

ADAPTER = "onebot_v11"
CHANNEL_ID = "group_123456"
CHAT_KEY = f"{ADAPTER}-{CHANNEL_ID}"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key="",
        channel_id=CHANNEL_ID,
        channel_type=ChatType.GROUP.value,
        channel_name="",
        chat_key=CHAT_KEY,
        is_active=True,
        observe_mode=False,
        data=json.dumps({}),
    )
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def fresh_warn_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "_warned_command_hooks", set())


class FakeAdapter:
    """命令链路只会碰到的几个成员；`/help` 被识别为命令，由 `execute_command` 记账"""

    key: str = ADAPTER

    def __init__(self) -> None:
        self.executed: List[str] = []

    def build_chat_key(self, channel_id: str, instance_key: str = "") -> str:
        return f"{self.key}-{instance_key}-{channel_id}" if instance_key else f"{self.key}-{channel_id}"

    def detect_command(self, text: str) -> Optional[Tuple[str, str]]:
        return ("help", "") if text.startswith("/help") else None

    async def execute_command(self, **kwargs: Any) -> None:
        self.executed.append(kwargs["command_name"])

    async def try_handle_wait_input(self, **_kwargs: Any) -> bool:
        return False


def make_inbound(text: str = "/help") -> Tuple[PlatformChannel, PlatformUser, PlatformMessage]:
    return (
        PlatformChannel(channel_id=CHANNEL_ID, channel_name="", channel_type=ChatType.GROUP),
        PlatformUser(platform_name="qq", user_id="10001", user_name="tester"),
        PlatformMessage(message_id="1", sender_id="10001", sender_name="tester", content_text=text),
    )


class TestCommandHookShape:
    def test_signature_matches_upstream_positional_shape(self) -> None:
        """参数名与位置逐位对齐上游 2.4.x"""
        import inspect

        params = list(inspect.signature(collector._FRAMEWORK_TRY_HANDLE_COMMAND).parameters)
        assert params == ["adapter", "chat_key", "platform_channel", "platform_user", "platform_message", "content_text"]

    async def test_upstream_style_wrapper_receives_six_args(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """照上游写的 6 参包装函数（与市场插件同一写法）能接住，并且命令照常执行"""
        seen: List[Any] = []
        original = collector._try_handle_command

        async def wrapped(adapter, chat_key, platform_channel, platform_user, platform_message, content_text):
            seen.append((chat_key, platform_channel.channel_id, content_text))
            return await original(adapter, chat_key, platform_channel, platform_user, platform_message, content_text)

        monkeypatch.setattr(collector, "_try_handle_command", wrapped)
        adapter = FakeAdapter()
        await collector.collect_message(adapter, *make_inbound())  # type: ignore[arg-type]

        assert seen == [(CHAT_KEY, CHANNEL_ID, "/help")]
        assert adapter.executed == ["help"]
        assert not collector._warned_command_hooks


class TestPatchGuard:
    async def test_wrapper_with_wrong_signature_falls_back_to_framework(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """签名接不住（这里是旧的 5 参写法）：命令仍由框架实现执行，消息入口不受影响"""
        called: List[int] = []

        async def legacy(adapter, chat_key, platform_user, platform_message, content_text):
            called.append(1)
            return False

        monkeypatch.setattr(collector, "_try_handle_command", legacy)
        adapter = FakeAdapter()
        await collector.collect_message(adapter, *make_inbound())  # type: ignore[arg-type]

        assert adapter.executed == ["help"]
        assert called == [], "签名不符的包装函数不应被调用"
        assert len(collector._warned_command_hooks) == 1

    async def test_warns_once_per_replacer(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        async def legacy(adapter, chat_key, platform_user, platform_message, content_text):
            return False

        monkeypatch.setattr(collector, "_try_handle_command", legacy)
        adapter = FakeAdapter()
        for _ in range(3):
            await collector.collect_message(adapter, *make_inbound())  # type: ignore[arg-type]

        assert adapter.executed == ["help", "help", "help"]
        assert len(collector._warned_command_hooks) == 1

    async def test_varargs_wrapper_is_used(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """`(*args, **kwargs)` 原样转发的包装函数总能接住，应当被调用而不是被绕过"""
        calls: List[int] = []
        original = collector._try_handle_command

        async def passthrough(*args, **kwargs):
            calls.append(len(args))
            return await original(*args, **kwargs)

        monkeypatch.setattr(collector, "_try_handle_command", passthrough)
        adapter = FakeAdapter()
        await collector.collect_message(adapter, *make_inbound())  # type: ignore[arg-type]

        assert calls == [6]
        assert adapter.executed == ["help"]

    async def test_type_error_raised_inside_wrapper_is_not_swallowed(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """签名对得上、函数体内自己抛的 `TypeError` 是插件的报错，原样冒出来"""

        async def buggy(adapter, chat_key, platform_channel, platform_user, platform_message, content_text):
            raise TypeError("plugin bug")

        monkeypatch.setattr(collector, "_try_handle_command", buggy)
        with pytest.raises(TypeError, match="plugin bug"):
            await collector.collect_message(FakeAdapter(), *make_inbound())  # type: ignore[arg-type]
        assert not collector._warned_command_hooks


class TestGetChatInfoLegacySignature:
    @staticmethod
    def _event(self_id: int = 10001) -> GroupIncreaseNoticeEvent:
        return GroupIncreaseNoticeEvent(
            time=1,
            self_id=self_id,
            post_type="notice",
            notice_type="group_increase",
            sub_type="approve",
            group_id=123456,
            operator_id=1,
            user_id=2,
        )

    async def test_get_chat_info_old_without_bot_uses_event_self_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """上游插件写 `get_chat_info_old(event=event)`；省略 `bot` 时按事件的 `self_id` 解析实例"""
        asked: List[str] = []

        async def fake_resolve(self_id: str) -> str:
            asked.append(self_id)
            return "inst1"

        monkeypatch.setattr(onebot_util, "resolve_instance_key", fake_resolve)
        chat_key, chat_type = await onebot_util.get_chat_info_old(event=self._event())

        assert asked == ["10001"]
        assert chat_key == "onebot_v11-inst1-group_123456"
        assert chat_type == ChatType.GROUP

    async def test_get_chat_info_old_with_bot_still_prefers_bot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: List[str] = []

        async def fake_resolve(self_id: str) -> str:
            asked.append(self_id)
            return ""

        class FakeBot:
            self_id = "20002"

        monkeypatch.setattr(onebot_util, "resolve_instance_key", fake_resolve)
        chat_key, _ = await onebot_util.get_chat_info_old(event=self._event(), bot=FakeBot())  # type: ignore[arg-type]

        assert asked == ["20002"]
        assert chat_key == "onebot_v11-group_123456"

    async def test_get_chat_info_without_bot(self) -> None:
        channel_id, chat_type = await onebot_util.get_chat_info(event=self._event())
        assert (channel_id, chat_type) == ("group_123456", ChatType.GROUP)
