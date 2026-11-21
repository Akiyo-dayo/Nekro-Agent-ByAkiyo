"""不带群号的通知事件，不该炸成 `AttributeError` 的 ERROR + traceback

测试部署上的现场：协议端掉线时先报了几条 `notice.bot_status.offline`，紧接着通知匹配器
连报 `AttributeError: 'NoticeEvent' object has no attribute 'group_id'`。

原因是通知一律按群解析频道（`_resolve_channel_body` 直接读 `event.group_id`），而协议端
自己的状态通知、好友消息撤回这些根本**没有**群号这个字段。匹配器在解析频道那一步只接
`ValueError`（为「群号为 0」那类事件准备的），`AttributeError` 接不住，被 nonebot 翻成整条
`Running Matcher(...) failed`。

修法两层，这里各钉一条：

1. 匹配器在进入「按群解析频道」之前先分流不带群号的通知，安静返回、不查库；
2. `_resolve_channel_body` 遇到不带群号的事件抛 `ValueError`（调用方本来就按它接），
   第三方插件照上游签名调 `get_chat_info_old` 时也不会拿到 `AttributeError`。
"""

from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import FriendRecallNoticeEvent, NoticeEvent

from nekro_agent.adapters.onebot_v11.tools import onebot_util


def _bot_status_offline() -> NoticeEvent:
    """协议端掉线时上报的状态通知：解析出来是通用的 `NoticeEvent`，身上没有 `group_id`"""
    return NoticeEvent(
        time=0,
        self_id=3000000001,
        post_type="notice",
        notice_type="bot_status",
        sub_type="offline",
        user_id=3000000001,
    )


def _friend_recall() -> FriendRecallNoticeEvent:
    """好友消息撤回：同样没有群号"""
    return FriendRecallNoticeEvent(
        time=0,
        self_id=3000000001,
        post_type="notice",
        notice_type="friend_recall",
        user_id=2000000003,
        message_id=5,
    )


GROUPLESS_EVENTS = [
    pytest.param(_bot_status_offline, id="bot_status.offline"),
    pytest.param(_friend_recall, id="friend_recall"),
]


@pytest.mark.parametrize("make_event", GROUPLESS_EVENTS)
def test_the_fixtures_really_have_no_group_id(make_event) -> None:
    """前提：这两种事件身上确实没有 `group_id`（不是 0，是根本没有这个字段）"""
    assert not hasattr(make_event(), "group_id")


# ---------------------------------------------------------------------------
# 1. 解析函数：抛调用方接得住的 ValueError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make_event", GROUPLESS_EVENTS)
def test_resolve_channel_body_raises_value_error(make_event) -> None:
    with pytest.raises(ValueError, match="不带群号"):
        onebot_util._resolve_channel_body(make_event())  # noqa: SLF001


@pytest.mark.parametrize("make_event", GROUPLESS_EVENTS)
async def test_get_chat_info_old_raises_value_error(make_event) -> None:
    """第三方插件照上游签名只传 `event`：拿到的也该是 ValueError"""
    with pytest.raises(ValueError, match="不带群号"):
        await onebot_util.get_chat_info_old(event=make_event())


# ---------------------------------------------------------------------------
# 2. 调用点：真的驱动通知匹配器
# ---------------------------------------------------------------------------


def _notice_handler():
    from nekro_agent.adapters.onebot_v11.matchers import notice as notice_module

    handlers = notice_module.notice_matcher.handlers
    assert len(handlers) == 1, "通知匹配器上应当只有一个处理函数"
    return notice_module, handlers[0].call


@pytest.mark.parametrize("make_event", GROUPLESS_EVENTS)
async def test_matcher_skips_groupless_notices_quietly(make_event, monkeypatch: pytest.MonkeyPatch) -> None:
    notice_module, handler = _notice_handler()

    async def _resolve_instance_key(_self_id):
        return "ob3000000001"

    async def _dispatch_allowed(_adapter_key: str, _instance_key: str) -> bool:
        return True

    looked_up: list = []

    async def _get_channel(chat_key: str):
        looked_up.append(chat_key)
        raise AssertionError("不该为不带群号的通知去查库")

    handled: list = []

    async def _handle(*args, **kwargs):
        handled.append((args, kwargs))

    monkeypatch.setattr(notice_module, "resolve_instance_key", _resolve_instance_key)
    monkeypatch.setattr(notice_module, "set_current_instance", lambda *a, **k: None)
    monkeypatch.setattr(notice_module, "is_inbound_dispatch_allowed", _dispatch_allowed)
    monkeypatch.setattr(notice_module.DBChatChannel, "get_channel", classmethod(lambda cls, chat_key: _get_channel(chat_key)))
    monkeypatch.setattr(notice_module.notice_manager, "handle", _handle)

    result = await handler(_=None, event=make_event(), bot=SimpleNamespace(self_id="3000000001"))

    assert result is None
    assert looked_up == []
    assert handled == []
