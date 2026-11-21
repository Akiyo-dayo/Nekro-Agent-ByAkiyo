"""协议端在处理入站事件的半路掉线：记一行 WARNING，不再冒成 ERROR + traceback

测试部署上的现场：协议端掉线那一刻（日志里先是几条 `notice.bot_status.offline`），还在处理中的
群消息走到 `get_user_name` 判「这个人是不是 Bot 自己」，要按会话去连接表里找连接，而连接表里
已经没有这个账号了，于是抛 `BotInstanceOfflineError`。消息匹配器没接，nonebot 把它打成
`Running Matcher(...) failed` 的 ERROR。

这类异常不是代码错误，也没有可补救的动作（连回复都发不出去），匹配器接住它、记一行 WARNING
就够了。这里真的驱动 `register_matcher` 注册的两个处理函数（群消息、群文件上传通知），
入站链路上真实的 `get_user_name` 不替换，只把它要问的适配器换成「连接已经没了」。

对照组钉两件事：连接还在时消息照常交给收集器；别的异常照旧往外抛，不被这道兜底吞掉。
"""

import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, GroupUploadNoticeEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import File, Sender

from nekro_agent.adapters.interface.schemas.platform import PlatformChannel
from nekro_agent.adapters.onebot_v11.core.bot import BotInstanceOfflineError
from nekro_agent.adapters.onebot_v11.matchers import message as matcher_module
from nekro_agent.schemas.chat_message import ChatMessageSegment, ChatMessageSegmentType

ADAPTER = "onebot_v11"
INSTANCE = "dsp"
BOT_ID = "3000000001"
USER_ID = "2000000003"
GROUP_ID = 500000001
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_{GROUP_ID}"


class _Adapter:
    key = ADAPTER
    config = SimpleNamespace(SESSION_ENABLE_AT=True)

    def __init__(self) -> None:
        self.endpoint_gone = True

    async def get_self_info(self, _chat_key: str = "") -> Any:
        # 与真实适配器相同的形状：按会话去连接表里找连接，连接没了就抛
        if self.endpoint_gone:
            raise BotInstanceOfflineError(INSTANCE)
        return SimpleNamespace(user_id=BOT_ID)


class _Logger:
    """匹配器模块里的 logger 替身：只记 warning，别的级别吞掉"""

    def __init__(self) -> None:
        self.warnings: List[str] = []

    def warning(self, message: str, *_args: Any, **_kwargs: Any) -> None:
        self.warnings.append(message)

    def __getattr__(self, _name: str) -> Callable[..., None]:
        return lambda *_a, **_k: None


@pytest.fixture
def inbound(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """装上真实的两个匹配器，入站链路上查库、转换、收集都换成可控替身"""
    adapter = _Adapter()

    async def get_preset() -> Any:
        return SimpleNamespace(name="人设名")

    channel = SimpleNamespace(chat_key=CHAT_KEY, channel_name="测试群", adapter=adapter, get_preset=get_preset)
    collected: List[Any] = []
    log = _Logger()

    async def resolve(_self_id: str) -> str:
        return INSTANCE

    async def allowed(_adapter_key: str, _instance_key: str) -> bool:
        return True

    async def get_db_chat_channel(_self: PlatformChannel, _adapter: Any) -> Any:
        return channel

    async def restore(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def convert(*_args: Any, **_kwargs: Any) -> Any:
        return [ChatMessageSegment(type=ChatMessageSegmentType.TEXT, text="你好")], False, "777"

    async def collect(_adapter: Any, _channel: Any, _user: Any, message: Any) -> None:
        collected.append(message)

    async def no_reply(**_kwargs: Any) -> None:
        return None

    async def get_user(**_kwargs: Any) -> Any:
        return SimpleNamespace(id=1, username="群友", is_active=True)

    monkeypatch.setattr(matcher_module, "resolve_instance_key", resolve)
    monkeypatch.setattr(matcher_module, "set_current_instance", lambda *_a, **_k: None)
    monkeypatch.setattr(matcher_module, "is_inbound_dispatch_allowed", allowed)
    monkeypatch.setattr(PlatformChannel, "get_db_chat_channel", get_db_chat_channel)
    monkeypatch.setattr(matcher_module, "restore_after_bot_returned", restore)
    monkeypatch.setattr(matcher_module, "convert_chat_message", convert)
    monkeypatch.setattr(matcher_module, "collect_message", collect)
    monkeypatch.setattr(matcher_module, "_quoted_video_segment", lambda *_a, **_k: no_reply())
    monkeypatch.setattr(matcher_module.DBUser, "get_or_none", get_user)
    monkeypatch.setattr(matcher_module, "logger", log)

    handlers: List[Callable[..., Any]] = []

    class _Registrar:
        def handle(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
                handlers.append(func)
                return func

            return decorate

    monkeypatch.setattr(matcher_module, "on_message", lambda **_kwargs: _Registrar())
    monkeypatch.setattr(matcher_module, "on_notice", lambda **_kwargs: _Registrar())
    matcher_module.register_matcher(adapter)  # type: ignore[arg-type]
    assert len(handlers) == 2, "消息、群文件上传两个匹配器都该注册上"

    return SimpleNamespace(
        adapter=adapter,
        on_message=handlers[0],
        on_upload=handlers[1],
        collected=collected,
        log=log,
        monkeypatch=monkeypatch,
    )


def _message_event() -> GroupMessageEvent:
    message = Message([MessageSegment.text("你好")])
    return GroupMessageEvent(
        time=int(time.time()),
        self_id=int(BOT_ID),
        post_type="message",
        sub_type="normal",
        user_id=int(USER_ID),
        message_type="group",
        message_id=777,
        message=message,
        original_message=message,
        raw_message="你好",
        font=0,
        sender=Sender(user_id=int(USER_ID), nickname="群友", card=""),
        to_me=False,
        group_id=GROUP_ID,
    )


def _upload_event() -> GroupUploadNoticeEvent:
    return GroupUploadNoticeEvent(
        time=int(time.time()),
        self_id=int(BOT_ID),
        post_type="notice",
        notice_type="group_upload",
        group_id=GROUP_ID,
        user_id=int(USER_ID),
        file=File(id="f1", name="a.txt", size=1, busid=102),
    )


def _bot() -> Any:
    async def get_group_member_info(**_kwargs: Any) -> Dict[str, str]:
        return {"card": "群名片", "nickname": "群友"}

    return SimpleNamespace(self_id=BOT_ID, get_group_member_info=get_group_member_info)


async def test_group_message_is_dropped_with_a_warning(inbound) -> None:
    """现场那条路：`get_user_name` → 适配器按会话找连接 → 连接已经没了"""
    result = await inbound.on_message(None, _message_event(), _bot())

    assert result is None
    assert inbound.collected == []
    assert len(inbound.log.warnings) == 1
    assert "协议端连接已不可用" in inbound.log.warnings[0]


async def test_upload_notice_is_dropped_with_a_warning(inbound) -> None:
    result = await inbound.on_upload(None, _upload_event(), _bot())

    assert result is None
    assert inbound.collected == []
    assert len(inbound.log.warnings) == 1
    assert "协议端连接已不可用" in inbound.log.warnings[0]


async def test_message_is_collected_while_the_endpoint_is_up(inbound) -> None:
    """对照：连接还在时，同一条消息照常交给收集器、不打 WARNING"""
    inbound.adapter.endpoint_gone = False

    await inbound.on_message(None, _message_event(), _bot())

    assert len(inbound.collected) == 1
    assert inbound.collected[0].sender_nickname
    assert inbound.log.warnings == []


async def test_other_errors_still_propagate(inbound) -> None:
    """兜底只接「连接没了」这一种：别的异常是真问题，照旧冒出去让 nonebot 记 ERROR"""
    inbound.adapter.endpoint_gone = False

    async def broken(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("转换器坏了")

    inbound.monkeypatch.setattr(matcher_module, "convert_chat_message", broken)

    with pytest.raises(RuntimeError, match="转换器坏了"):
        await inbound.on_message(None, _message_event(), _bot())
    assert inbound.log.warnings == []
