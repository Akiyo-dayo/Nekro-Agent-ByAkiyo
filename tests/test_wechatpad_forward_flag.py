"""WeChatPad：只有文本消息（msg_type 1）算发送者本人写的

引用（appmsg 里的 refermsg）、合并转发的聊天记录等非文本类型，消息段里存的是原始 XML，
里面可能是别人说的话（例如别人的主机和密码）。这些消息要标 `ext_data.forwarded`，聊天登记的
来源证明才不会把它们算成主人自己说的。`message_type` 认不出、退回成 TEXT 的类型同样标上。
文字照样入库、照样给模型看，只多一个标记。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest

from nekro_agent.adapters.wechatpad import realtime_processor as rp

OWNER = "wxid_owner01"
BOT = "wxid_bot01"
HOST = "192.0.2." + "7"
QUOTE_XML = (
    "<msg><appmsg><title>这台怎么连</title><type>57</type>"
    f"<refermsg><content>host {HOST} 密码 not-mine-pw</content></refermsg></appmsg></msg>"
)


class _FakeAdapter:
    async def get_self_info(self) -> Any:
        return SimpleNamespace(user_id=BOT)


def _message(msg_type: int, content: str) -> rp.WeChatMessage:
    return rp.WeChatMessage(
        msg_id=1,
        from_user=OWNER,
        to_user=BOT,
        msg_type=msg_type,
        content=content,
        status=0,
        create_time=1_000_000_000,
        msg_source="",
        push_content="",
        new_msg_id=2,
    )


async def _forward(monkeypatch: pytest.MonkeyPatch, message: rp.WeChatMessage) -> Any:
    captured: List[Any] = []

    async def fake_collect(_adapter: Any, _channel: Any, _user: Any, platform_message: Any) -> None:
        captured.append(platform_message)

    monkeypatch.setattr(rp, "collect_message", fake_collect)
    processor = rp.WeChatRealtimeProcessor(SimpleNamespace(), _FakeAdapter())  # type: ignore[arg-type]
    await processor._forward_to_nekro_agent(message)  # noqa: SLF001
    assert len(captured) == 1
    return captured[0]


async def test_plain_text_is_the_senders_own(monkeypatch: pytest.MonkeyPatch) -> None:
    platform_message = await _forward(monkeypatch, _message(1, f"主机 {HOST} 密码 my-own-pw"))
    assert platform_message.ext_data.forwarded is False


async def test_quote_appmsg_is_not_the_senders_own(monkeypatch: pytest.MonkeyPatch) -> None:
    platform_message = await _forward(monkeypatch, _message(49, QUOTE_XML))
    assert platform_message.ext_data.forwarded is True
    assert platform_message.content_text == QUOTE_XML, "原文照样入库"


async def test_unknown_type_falling_back_to_text_is_not_the_senders_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """名片（42）等不在枚举里的类型会被 `message_type` 退回成 TEXT，但存的仍是 XML"""
    message = _message(42, '<msg username="wxid_other" nickname="别人"/>')
    assert message.message_type is rp.MessageType.TEXT
    platform_message = await _forward(monkeypatch, message)
    assert platform_message.ext_data.forwarded is True
