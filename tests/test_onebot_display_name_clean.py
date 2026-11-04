"""OneBot 入口清理协议端上报名字里的控制字符

QQ 的「彩色 / 特效群名片」在部分协议端那侧会被当成原始 protobuf 字节上报：``card`` 里夹着
0x0e、0x12、0x0a、0x09、0x07、0x1a、0x10、0x11 这类控制字符，结尾固定一个 NUL。另一个协议端
会把里面的文字片段拼成一段正常文字。把前者的 U+0000–U+001F 与 U+007F 全部去掉，就得到后者。

样本是人工构造的假名字，结构与真实上报一致。断言分三层：纯函数、回退链、端到端（真实匹配器入库）。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Sender
from tortoise import Tortoise

from nekro_agent.adapters.interface.schemas.platform import PlatformChannel
from nekro_agent.adapters.onebot_v11.matchers import message as matcher_module
from nekro_agent.adapters.onebot_v11.tools import convertor, onebot_util
from nekro_agent.adapters.onebot_v11.tools.display_name import clean_display_name
from nekro_agent.core.config import config as global_config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.signal import MsgSignal
from nekro_agent.services.message_service import message_service
from nekro_agent.services.plugin import scope as plugin_scope
from nekro_agent.services.plugin.collector import plugin_collector

# ============================================================
# 人工构造的样本：结构与真实上报一致，名字是假的
# ============================================================

SAMPLE_1 = "\x0e\x12\x0c" + "测试名甲" + "\x0a\x09\x0a\x07" + "$ǿĀ" + "\x1a\x10\x0a\x09\x0a\x07" + "$ǿĀ" + "\x1a\x11\x10\x00"
EXPECTED_1 = "测试名甲$ǿĀ$ǿĀ"
SAMPLE_2 = "\x12\x09" + "测试乙" + "\x0a\x09\x0a\x07" + "$ÿĀ" + "\x11\x22\x10\x00"
EXPECTED_2 = '测试乙$ÿĀ"'

ADAPTER = "onebot_v11"
INSTANCE = "dsp"
BOT_ID = "2000000009"
USER_ID = "2000000001"
GROUP_ID = 1001


def _has_c0(text: str) -> bool:
    """有没有 U+0000–U+001F 或 U+007F

    换行（0x0A）是合并转发正文里正常的分行符，这里按「除换行外的控制字符」判名字：
    名字本身不允许出现任何控制字符，正文的行结构不算。
    """
    return any((ord(ch) <= 0x1F and ch != "\n") or ord(ch) == 0x7F for ch in text)


# ============================================================
# 一、纯函数
# ============================================================


class TestCleanDisplayName:
    def test_sample_1(self) -> None:
        assert clean_display_name(SAMPLE_1) == EXPECTED_1

    def test_sample_2_keeps_non_control_punctuation(self) -> None:
        """0x22 是引号、不是控制字符：必须原样留着"""
        assert clean_display_name(SAMPLE_2) == EXPECTED_2
        assert '"' in EXPECTED_2

    def test_none_returns_empty_string(self) -> None:
        assert clean_display_name(None) == ""

    def test_normal_name_is_untouched(self) -> None:
        assert clean_display_name("普通名字 Abc 123") == "普通名字 Abc 123"

    def test_only_control_chars_returns_empty_string(self) -> None:
        assert clean_display_name("\x00\x0e\x12\x1f\x7f") == ""

    def test_a_plain_name_keeps_its_spaces(self) -> None:
        """不带控制字符的名字是协议端正常上报的，一个字不动，首尾空格也原样留着"""
        assert clean_display_name(" 甲 乙 ") == " 甲 乙 "

    @pytest.mark.parametrize("blank", ["　", " ", " ", "　　"])
    def test_a_name_made_only_of_blanks_is_kept(self, blank: str) -> None:
        """故意设成全角空格 / 不换行空格的「隐形名」：NapCat 原样上报，清理后必须还是它，不能变成空串"""
        assert clean_display_name(blank) == blank

    def test_a_raw_card_is_trimmed_after_its_control_chars_go(self) -> None:
        """带控制字符的是原始字节拼出来的名片：去掉控制字符后，落到首尾的空白一起去掉"""
        assert clean_display_name("  名字\t\n") == "名字"
        assert clean_display_name("\x0e 测试 \x00") == "测试"

    @pytest.mark.parametrize("residue", ["\x0a \x12", "\x0e　\x00", "\x12\x09   \x00"])
    def test_a_raw_card_with_only_blanks_left_is_empty(self, residue: str) -> None:
        """原始字节去掉控制字符后只剩空白：那是残渣、不是名字，得到空串让回退链接住"""
        assert clean_display_name(residue) == ""

    def test_special_characters_are_kept(self) -> None:
        for text in ("$", '"', "ǿ", "Ā", "ÿ", "甲$ǿĀ"):
            assert clean_display_name(text) == text


# ============================================================
# 二、回退链
# ============================================================


BOT_QQ = "3000000001"
SENDER_QQ = 3000000002
OTHER_QQ = 3000000003
GROUP_ID_FALLBACK = 100000001


class _FakeBot:
    def __init__(self, info: Optional[dict] = None) -> None:
        self.info = info if info is not None else {}

    async def get_group_member_info(self, **_kwargs: Any) -> dict:
        return self.info


def _channel() -> Any:
    async def get_self_info(_chat_key: str) -> Any:
        return SimpleNamespace(user_id=BOT_QQ)

    async def get_preset() -> Any:
        return SimpleNamespace(name="人设名")

    return SimpleNamespace(
        chat_key=f"onebot_v11-ob{BOT_QQ}-group_{GROUP_ID_FALLBACK}",
        adapter=SimpleNamespace(get_self_info=get_self_info),
        get_preset=get_preset,
    )


def _group_event(message: Message, card: str = "群名片", nickname: str = "昵称") -> GroupMessageEvent:
    return GroupMessageEvent(
        time=0,
        self_id=int(BOT_QQ),
        post_type="message",
        sub_type="normal",
        user_id=SENDER_QQ,
        message_type="group",
        message_id=1,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender=Sender(user_id=SENDER_QQ, nickname=nickname, card=card),
        to_me=False,
        group_id=GROUP_ID_FALLBACK,
        anonymous=None,
    )


class TestFallbackChain:
    async def test_card_of_only_control_chars_falls_back_to_nickname(self) -> None:
        """群名片清理后是空串，必须落到昵称，而不是拿空名字去建用户"""
        bot = _FakeBot(info={"card": "\x0e\x12\x10\x00", "nickname": "真实昵称"})

        name = await onebot_util.get_user_name(
            event=_group_event(Message("你好")),
            bot=bot,  # type: ignore[arg-type]
            user_id=SENDER_QQ,
            db_chat_channel=_channel(),  # type: ignore[arg-type]
        )

        assert name == "真实昵称"

    async def test_event_card_falls_back_to_event_nickname(self) -> None:
        """查成员信息失败那条分支：事件自带的群名片清理后为空，落到事件昵称"""
        event = _group_event(Message("你好"), card="\x0e\x00", nickname="事件昵称")
        bot = _FakeBot()

        async def boom(**_kwargs: Any) -> dict:
            from nonebot.adapters.onebot.v11.exception import NetworkError

            raise NetworkError("timeout")

        bot.get_group_member_info = boom  # type: ignore[method-assign]

        name = await onebot_util.get_user_name(
            event=event,
            bot=bot,  # type: ignore[arg-type]
            user_id=SENDER_QQ,
            db_chat_channel=_channel(),  # type: ignore[arg-type]
        )

        assert name == "事件昵称"

    async def test_group_card_name_cleans_sample_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bot = _FakeBot(info={"card": SAMPLE_1, "nickname": "最新昵称"})

        async def _get_bot(_chat_key: str) -> Any:
            return bot

        monkeypatch.setattr(onebot_util, "get_bot_by_chat_key", _get_bot)

        name = await onebot_util.get_user_group_card_name(
            group_id=GROUP_ID_FALLBACK,
            user_id=str(OTHER_QQ),
            db_chat_channel=_channel(),  # type: ignore[arg-type]
        )

        assert name == EXPECTED_1
        assert not _has_c0(name)


# ============================================================
# 三、合并转发 / 回复
# ============================================================


class _ForwardBot:
    def __init__(self, messages: List[dict]) -> None:
        self.messages = messages

    async def call_api(self, api: str, **_kwargs: Any) -> Any:
        assert api == "get_forward_msg"
        return {"messages": self.messages}


async def test_inline_forward_node_name_is_cleaned() -> None:
    nodes = [
        {
            "type": "node",
            "data": {
                "sender": {"user_id": int(USER_ID), "nickname": SAMPLE_1, "card": ""},
                "content": [{"type": "text", "data": {"text": "转发正文"}}],
            },
        },
    ]

    text, content = await convertor._parse_forward_nodes(
        nodes=nodes,
        bot=_ForwardBot([]),  # type: ignore[arg-type]
        chat_key="onebot_v11-dsp-group_1001",
    )

    assert f"{EXPECTED_1}: 转发正文" in text
    assert content[0]["sender"] == EXPECTED_1
    assert not _has_c0(text)
    assert not _has_c0(str(content))


async def test_api_forward_node_name_is_cleaned() -> None:
    """走 `get_forward_msg` 取回的那一路（同一段昵称处理，另一个入口）"""
    bot = _ForwardBot(
        [
            {
                "sender": {"user_id": int(USER_ID), "nickname": "", "card": SAMPLE_1},
                "content": [{"type": "text", "data": {"text": "转发正文"}}],
            },
        ],
    )

    text, content = await convertor._parse_forward_message(
        bot=bot,  # type: ignore[arg-type]
        forward_id="forward-1",
        chat_key="onebot_v11-dsp-group_1001",
    )

    assert f"{EXPECTED_1}: 转发正文" in text
    assert content[0]["sender"] == EXPECTED_1
    assert not _has_c0(text)
    assert not _has_c0(str(content))


# ============================================================
# 四、端到端：真实匹配器 -> collect_message -> 数据库
# ============================================================

@pytest.fixture
async def db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


class _Adapter:
    """匹配器与收集器会碰到的那几个成员"""

    key = ADAPTER
    config = SimpleNamespace(SESSION_ENABLE_AT=True)

    def build_chat_key(self, channel_id: str, instance_key: str = "") -> str:
        return f"{self.key}-{instance_key}-{channel_id}" if instance_key else f"{self.key}-{channel_id}"

    def detect_command(self, _text: str) -> Optional[Tuple[str, str]]:
        return None

    async def try_handle_wait_input(self, **_kwargs: Any) -> bool:
        return False

    async def get_self_info(self, _chat_key: Optional[str] = None) -> Any:
        return SimpleNamespace(user_id=BOT_ID)

    async def get_channel_info(self, channel_id: str) -> PlatformChannel:
        return PlatformChannel(channel_id=channel_id, channel_name="测试群", channel_type=ChatType.GROUP)


class _Bot:
    self_id = BOT_ID

    def __init__(self, card: str, nickname: str) -> None:
        self.card = card
        self.nickname = nickname

    async def get_group_member_info(self, **_kwargs: Any) -> Dict[str, str]:
        return {"card": self.card, "nickname": self.nickname}

    async def call_api(self, api: str, **_data: Any) -> Any:
        raise AssertionError(f"这条链路不该调协议端: {api}")


@pytest.fixture
async def chat_key(db: None) -> str:  # noqa: F811
    key = f"{ADAPTER}-{INSTANCE}-group_{GROUP_ID}"
    await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=f"group_{GROUP_ID}",
        chat_key=key,
        channel_type="group",
        channel_name="测试群",
        data="{}",
    )
    return key


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Callable[..., Any]:
    """把真实的消息匹配器装到一个替身适配器上，返回「喂一个事件」的函数"""
    merged = global_config.model_copy(update={"AI_CHAT_TRIGGER_REGEX": []})

    async def effective(self: DBChatChannel) -> Any:
        return merged

    monkeypatch.setattr(DBChatChannel, "get_effective_config", effective)

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return

    async def no_plugins(*_args: Any, **_kwargs: Any) -> Any:
        return MsgSignal.CONTINUE

    monkeypatch.setattr(type(message_service), "_debounce_task", noop)
    message_service.pending_messages.clear()
    message_service.debounce_timers.clear()
    monkeypatch.setattr(plugin_collector, "handle_on_user_message", no_plugins)
    monkeypatch.setattr(plugin_scope, "SCOPE_OVERRIDE_DIR", tmp_path / "overrides")
    monkeypatch.setattr(plugin_scope, "_scope_override_cache", {})

    adapter = _Adapter()
    monkeypatch.setattr(DBChatChannel, "adapter", property(lambda _self: adapter))

    async def resolve(_self_id: str) -> str:
        return INSTANCE

    monkeypatch.setattr(matcher_module, "resolve_instance_key", resolve)
    monkeypatch.setattr(matcher_module, "set_current_instance", lambda *_a, **_k: None)

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
    on_message = handlers[0]

    async def feed(event: Any, bot: Any) -> None:
        await on_message(None, event, bot)

    yield feed
    message_service.pending_messages.clear()
    message_service.debounce_timers.clear()


def _message_event(card: str, nickname: str, message_id: int = 777) -> GroupMessageEvent:
    message = Message([MessageSegment.text("你好")])
    return GroupMessageEvent(
        time=int(time.time()),
        self_id=int(BOT_ID),
        post_type="message",
        sub_type="normal",
        user_id=int(USER_ID),
        message_type="group",
        message_id=message_id,
        message=message,
        original_message=message,
        raw_message="你好",
        font=0,
        sender=Sender(user_id=int(USER_ID), nickname=nickname, card=card),
        to_me=True,
        group_id=GROUP_ID,
    )


class TestEndToEnd:
    async def test_group_message_card_with_control_chars_is_cleaned(
        self,
        chat_key: str,
        pipeline: Callable[..., Any],
    ) -> None:
        """一条群消息的 `sender.card = SAMPLE_1`：入库后的昵称是清理过的文本，且不含任何 C0 控制字符"""
        await pipeline(_message_event(card=SAMPLE_1, nickname="事件昵称"), _Bot(card=SAMPLE_1, nickname="事件昵称"))

        rows = list(await DBChatMessage.filter(chat_key=chat_key))
        assert len(rows) == 1, "这条消息必须真的落库"
        row = rows[0]
        assert row.sender_nickname == EXPECTED_1
        assert not _has_c0(row.sender_nickname)
        # `sender_name` 取的是事件自带的昵称（不是群名片），同样经过清理
        assert not _has_c0(row.sender_name or "")

    async def test_group_message_falls_back_when_card_is_only_control_chars(
        self,
        chat_key: str,
        pipeline: Callable[..., Any],
    ) -> None:
        """群名片全是控制字符时，事件昵称接住；库里不该出现空名字"""
        await pipeline(_message_event(card="\x0e\x12\x00", nickname="事件昵称"), _Bot(card="\x0e\x12\x00", nickname="事件昵称"))

        rows = list(await DBChatMessage.filter(chat_key=chat_key))
        assert len(rows) == 1
        assert rows[0].sender_nickname == "事件昵称"
        assert rows[0].sender_name == "事件昵称"

    async def test_group_message_still_stored_when_nickname_and_card_are_only_control_chars(
        self,
        chat_key: str,
        pipeline: Callable[..., Any],
    ) -> None:
        """昵称与名片都清理成空串：消息照常入库，发送者名字退回 QQ 号，而不是被断言整条丢掉"""
        junk = "\x0e\x12\x00"
        await pipeline(_message_event(card=junk, nickname=junk), _Bot(card=junk, nickname=junk))

        rows = list(await DBChatMessage.filter(chat_key=chat_key))
        assert len(rows) == 1, "这条消息必须真的落库"
        assert rows[0].sender_name == USER_ID
        assert not _has_c0(rows[0].sender_nickname or "")

    async def test_blank_invisible_name_is_kept_not_replaced_by_the_qq_number(
        self,
        chat_key: str,
        pipeline: Callable[..., Any],
    ) -> None:
        """昵称与名片都是一个全角空格：入库的名字就是这个全角空格（与 NapCat / 0.4.2 一致），不是 QQ 号"""
        blank = "　"
        await pipeline(_message_event(card=blank, nickname=blank), _Bot(card=blank, nickname=blank))

        rows = list(await DBChatMessage.filter(chat_key=chat_key))
        assert len(rows) == 1, "这条消息必须真的落库"
        assert rows[0].sender_name == blank
        assert rows[0].sender_nickname == blank

    async def test_raw_card_leaving_only_blanks_falls_back_to_the_nickname(
        self,
        chat_key: str,
        pipeline: Callable[..., Any],
    ) -> None:
        """彩色名片的原始字节去掉控制字符后只剩一个空格：落到昵称，不留一个空白名字"""
        residue = "\x0a \x12\x00"
        await pipeline(_message_event(card=residue, nickname="事件昵称"), _Bot(card=residue, nickname="事件昵称"))

        rows = list(await DBChatMessage.filter(chat_key=chat_key))
        assert len(rows) == 1, "这条消息必须真的落库"
        assert rows[0].sender_nickname == "事件昵称"
        assert rows[0].sender_name == "事件昵称"

    async def test_reply_sender_name_is_cleaned(
        self,
        chat_key: str,
        pipeline: Callable[..., Any],
    ) -> None:
        """回复（引用）场景：被引用的那条消息原样带回来的 `sender` 与回复者自己的名片一样脏

        `nonebot` 的 `event.reply` 就是协议端 `get_msg` 的结果。OneBot 这条链路只取被引用消息的编号
        （不渲染它的昵称），所以名字的清理落在回复者自己这一行；这里把两处都设成 `SAMPLE_1`，
        断言落库的这一行里不留任何控制字符，引用编号照常保留。
        """
        reply_message = Message([MessageSegment.text("被引用的正文")])
        reply_sender = Sender(user_id=int(USER_ID), nickname=SAMPLE_1, card="")
        quoted = Message(
            [
                MessageSegment("reply", {"id": "9001", "text": "被引用的正文"}),
                MessageSegment("text", {"text": "我的回复"}),
            ],
        )
        event = GroupMessageEvent(
            time=int(time.time()),
            self_id=int(BOT_ID),
            post_type="message",
            sub_type="normal",
            user_id=int(USER_ID),
            message_type="group",
            message_id=778,
            message=quoted,
            original_message=quoted,
            raw_message="我的回复",
            font=0,
            sender=Sender(user_id=int(USER_ID), nickname="事件昵称", card=SAMPLE_1),
            to_me=True,
            group_id=GROUP_ID,
        )
        event.reply = SimpleNamespace(message=reply_message, sender=reply_sender)  # type: ignore[attr-defined]

        await pipeline(event, _Bot(card=SAMPLE_1, nickname="事件昵称"))

        rows = list(await DBChatMessage.filter(chat_key=chat_key))
        assert len(rows) == 1
        row = rows[0]
        assert row.ext_data_obj.ref_msg_id == "9001"
        assert row.sender_nickname == EXPECTED_1
        for field in (row.sender_name, row.sender_nickname, row.content_text, row.content_data):
            assert not _has_c0(field or ""), f"落库字段里还有控制字符: {field!r}"


class TestChannelName:
    """私聊频道名取陌生人昵称、群频道名取群名：两处同样来自协议端"""

    async def test_private_channel_name_uses_cleaned_stranger_nickname(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.adapters.onebot_v11 import adapter as adapter_module

        class _ChannelBot:
            self_id = BOT_ID

            async def get_stranger_info(self, **_kwargs: Any) -> Dict[str, str]:
                return {"nickname": SAMPLE_1}

            async def get_group_info(self, **_kwargs: Any) -> Dict[str, str]:
                raise AssertionError("私聊不该查群信息")

        monkeypatch.setattr(adapter_module, "get_bot", lambda: _ChannelBot())
        monkeypatch.setattr(adapter_module, "get_current_instance", lambda: None)

        channel = await adapter_module.OnebotV11Adapter().get_channel_info(f"private_{USER_ID}")

        assert channel.channel_name == EXPECTED_1
        assert not _has_c0(channel.channel_name)

    async def test_group_channel_name_is_cleaned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.adapters.onebot_v11 import adapter as adapter_module

        class _ChannelBot:
            self_id = BOT_ID

            async def get_stranger_info(self, **_kwargs: Any) -> Dict[str, str]:
                raise AssertionError("群聊不该查陌生人信息")

            async def get_group_info(self, **_kwargs: Any) -> Dict[str, str]:
                return {"group_name": SAMPLE_2}

        monkeypatch.setattr(adapter_module, "get_bot", lambda: _ChannelBot())
        monkeypatch.setattr(adapter_module, "get_current_instance", lambda: None)

        channel = await adapter_module.OnebotV11Adapter().get_channel_info(f"group_{GROUP_ID}")

        assert channel.channel_name == EXPECTED_2
        assert not _has_c0(channel.channel_name)
