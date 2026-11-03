"""查不到群成员信息时，名字退回事件自带的昵称，消息不能因此丢掉

真实现场（美国测试部署，2026-11-02）：协议端那条反向 WS 半死不活的时候，每条群消息进来，
`get_user_name` 都要先调 `get_group_member_info` 拿最新群名片，等满超时抛
`NetworkError: WebSocket call api get_group_member_info timeout`。异常一路冒到 nonebot，
变成 `Running Matcher(...) failed`，**这条消息整条丢了**——12 小时里 6 次。
而消息事件本来就带着发送者的群名片和昵称。对方已退群时（通知事件里常见）协议端回的是 ActionFailed，同样会丢。

所以只兜「查群成员信息失败」这一类适配器异常：消息事件退回发送者自己的群名片 / 昵称；
查的不是发送者（被 @ 的人、通知的目标），或事件根本不带 sender（通知事件），就退回 QQ 号；
接口成功但没给名字时同样退回。别的异常照旧冒出去，不吞。
"""

from types import SimpleNamespace
from typing import Any, Optional

import pytest
from nonebot.adapters.onebot.v11 import GroupIncreaseNoticeEvent, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable, NetworkError

from nekro_agent.adapters.onebot_v11.tools import onebot_util

BOT_QQ = "3000000001"
SENDER_QQ = 3000000002
OTHER_QQ = 3000000003
GROUP_ID = 100000001


class _FakeBot:
    """get_group_member_info 要么抛给定的异常，要么返回给定的成员信息"""

    def __init__(self, error: Optional[BaseException] = None, info: Optional[dict] = None) -> None:
        self.error = error
        self.info = info or {}
        self.calls: list = []

    async def get_group_member_info(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.info


def _channel() -> Any:
    async def get_self_info(_chat_key: str) -> Any:
        return SimpleNamespace(user_id=BOT_QQ)

    async def get_preset() -> Any:
        return SimpleNamespace(name="人设名")

    return SimpleNamespace(
        chat_key=f"onebot_v11-ob{BOT_QQ}-group_{GROUP_ID}",
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
        group_id=GROUP_ID,
        anonymous=None,
    )


MEMBER_INFO_ERRORS = [
    pytest.param(NetworkError("WebSocket call api get_group_member_info timeout"), id="timeout"),
    pytest.param(ActionFailed(status="failed", retcode=100, msg="不是群成员"), id="left-group"),
    pytest.param(ApiNotAvailable(), id="api-not-available"),
]


@pytest.mark.parametrize("error", MEMBER_INFO_ERRORS)
async def test_sender_name_falls_back_to_event_card(error: BaseException) -> None:
    bot = _FakeBot(error=error)
    event = _group_event(Message("你好"))

    name = await onebot_util.get_user_name(event=event, bot=bot, user_id=str(SENDER_QQ), db_chat_channel=_channel())  # type: ignore[arg-type]

    assert name == "群名片"
    assert len(bot.calls) == 1  # 真的去查过，失败后才退回


async def test_sender_without_card_falls_back_to_nickname() -> None:
    event = _group_event(Message("你好"), card="")

    name = await onebot_util.get_user_name(
        event=event,
        bot=_FakeBot(error=NetworkError("timeout")),  # type: ignore[arg-type]
        user_id=SENDER_QQ,
        db_chat_channel=_channel(),  # type: ignore[arg-type]
    )

    assert name == "昵称"


async def test_someone_else_falls_back_to_qq_number_not_sender_name() -> None:
    """查的是别人（被 @ 的人、通知目标）时，不能把发送者的名字安到他头上"""
    event = _group_event(Message("你好"))

    name = await onebot_util.get_user_name(
        event=event,
        bot=_FakeBot(error=NetworkError("timeout")),  # type: ignore[arg-type]
        user_id=OTHER_QQ,
        db_chat_channel=_channel(),  # type: ignore[arg-type]
    )

    assert name == str(OTHER_QQ)


async def test_member_info_still_wins_when_available() -> None:
    bot = _FakeBot(info={"card": "最新群名片", "nickname": "最新昵称"})
    event = _group_event(Message("你好"))

    name = await onebot_util.get_user_name(event=event, bot=bot, user_id=SENDER_QQ, db_chat_channel=_channel())  # type: ignore[arg-type]

    assert name == "最新群名片"


async def test_empty_member_info_falls_back_instead_of_raising() -> None:
    """接口成功但没给名字（协议端回了空对象）：原先 raise ValueError，同样整条丢"""
    event = _group_event(Message("你好"))

    name = await onebot_util.get_user_name(
        event=event,
        bot=_FakeBot(info={}),  # type: ignore[arg-type]
        user_id=SENDER_QQ,
        db_chat_channel=_channel(),  # type: ignore[arg-type]
    )

    assert name == "群名片"


async def test_notice_event_without_sender_falls_back_to_qq_number() -> None:
    """通知事件没有 sender 字段：兜底不能去碰 event.sender，只能退回 QQ 号"""
    event = GroupIncreaseNoticeEvent(
        time=0,
        self_id=int(BOT_QQ),
        post_type="notice",
        notice_type="group_increase",
        sub_type="approve",
        user_id=OTHER_QQ,
        group_id=GROUP_ID,
        operator_id=SENDER_QQ,
    )

    name = await onebot_util.get_user_name(
        event=event,
        bot=_FakeBot(error=ActionFailed(status="failed", retcode=100)),  # type: ignore[arg-type]
        user_id=str(OTHER_QQ),
        db_chat_channel=_channel(),  # type: ignore[arg-type]
    )

    assert name == str(OTHER_QQ)


async def test_unrelated_errors_are_not_swallowed() -> None:
    event = _group_event(Message("你好"))

    with pytest.raises(RuntimeError):
        await onebot_util.get_user_name(
            event=event,
            bot=_FakeBot(error=RuntimeError("bug")),  # type: ignore[arg-type]
            user_id=SENDER_QQ,
            db_chat_channel=_channel(),  # type: ignore[arg-type]
        )


async def test_at_segment_keeps_the_message_when_lookup_times_out() -> None:
    """gen_chat_text 对每个 @ 都查一次名字：查不到时这条消息照样生成，@ 的人用 QQ 号"""
    event = _group_event(Message([MessageSegment.text("看这个 "), MessageSegment.at(OTHER_QQ)]))

    text, _ = await onebot_util.gen_chat_text(
        event=event,
        bot=_FakeBot(error=NetworkError("timeout")),  # type: ignore[arg-type]
        db_chat_channel=_channel(),  # type: ignore[arg-type]
    )

    assert text == f"看这个 [@id:{OTHER_QQ};nickname:{OTHER_QQ}@]"


@pytest.mark.parametrize("error", MEMBER_INFO_ERRORS)
async def test_group_card_name_falls_back_to_qq_number(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    bot = _FakeBot(error=error)

    async def _get_bot(_chat_key: str) -> Any:
        return bot

    monkeypatch.setattr(onebot_util, "get_bot_by_chat_key", _get_bot)

    name = await onebot_util.get_user_group_card_name(
        group_id=GROUP_ID,
        user_id=str(OTHER_QQ),
        db_chat_channel=_channel(),  # type: ignore[arg-type]
    )

    assert name == str(OTHER_QQ)
    assert len(bot.calls) == 1


async def test_group_card_name_uses_member_info_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _FakeBot(info={"card": "", "nickname": "最新昵称"})

    async def _get_bot(_chat_key: str) -> Any:
        return bot

    monkeypatch.setattr(onebot_util, "get_bot_by_chat_key", _get_bot)

    name = await onebot_util.get_user_group_card_name(
        group_id=GROUP_ID,
        user_id=str(OTHER_QQ),
        db_chat_channel=_channel(),  # type: ignore[arg-type]
    )

    assert name == "最新昵称"
    assert bot.calls == [{"group_id": GROUP_ID, "user_id": OTHER_QQ, "no_cache": False}]


async def test_group_card_name_unrelated_errors_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _get_bot(_chat_key: str) -> Any:
        return _FakeBot(error=RuntimeError("bug"))

    monkeypatch.setattr(onebot_util, "get_bot_by_chat_key", _get_bot)

    with pytest.raises(RuntimeError):
        await onebot_util.get_user_group_card_name(
            group_id=GROUP_ID,
            user_id=str(OTHER_QQ),
            db_chat_channel=_channel(),  # type: ignore[arg-type]
        )
