"""Telegram 转发消息的标记

## 这条测试钉的是什么

Telegram 适配器收到转发来的消息时，要在 `ext_data.forwarded` 上标真：正文照样入库、照样
给模型看，但来源证明据此知道那不是发送者本人写的。新版 python-telegram-bot 用
`forward_origin`，旧版用 `forward_date` / `forward_from` / `forward_from_chat` /
`forward_sender_name`；普通消息为假。

这里用最小的假对象走一遍 `_handle_message`，只截获交给收集器的 `PlatformMessage`，
不连 Telegram、不写数据库。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from nekro_agent.adapters.interface.schemas.platform import PlatformMessage
from nekro_agent.adapters.telegram.message_processor import MessageProcessor

_FORWARD_FIELDS = (
    "forward_origin",
    "forward_date",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
)


class _FakeAdapter:
    key = "telegram"


class _FakeUser:
    id = 8001
    first_name = "Tester"
    last_name = ""
    username = "tester"


class _FakeChat:
    id = 7001
    type = "private"
    title = None
    first_name = "Tester"
    username = "tester"


class _FakeMessage:
    """只带 `_handle_message` 会读到的字段；转发的几种写法由调用方设置"""

    message_id = 501
    text = "这台机器怎么连"
    caption = None
    photo = None
    document = None
    video = None
    audio = None
    voice = None
    sticker = None
    video_note = None
    entities = None
    from_user = _FakeUser()
    chat = _FakeChat()

    def __init__(self) -> None:
        for name in _FORWARD_FIELDS:
            setattr(self, name, None)


def _capture_collected(monkeypatch: pytest.MonkeyPatch) -> list[PlatformMessage]:
    captured: list[PlatformMessage] = []

    async def _fake_collect(
        adapter: Any,
        platform_channel: Any,
        platform_user: Any,
        platform_message: PlatformMessage,
    ) -> None:
        captured.append(platform_message)

    monkeypatch.setattr(
        "nekro_agent.adapters.interface.collector.collect_message",
        _fake_collect,
    )
    return captured


async def _process(message: _FakeMessage, monkeypatch: pytest.MonkeyPatch) -> PlatformMessage:
    captured = _capture_collected(monkeypatch)
    await MessageProcessor(adapter=_FakeAdapter())._handle_message(message)  # type: ignore[arg-type]
    assert captured, "消息应交给收集器"
    return captured[0]


@pytest.mark.parametrize("field", _FORWARD_FIELDS)
async def test_forwarded_message_sets_flag(
    field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _FakeMessage()
    setattr(message, field, object())

    platform_message = await _process(message, monkeypatch)

    assert platform_message.ext_data is not None
    assert platform_message.ext_data.forwarded is True
    assert platform_message.content_text == "这台机器怎么连", "文字照样入库、照样给模型看"


async def test_legacy_forward_without_forward_origin_sets_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _FakeMessage()
    del message.forward_origin
    message.forward_date = datetime(2024, 1, 2, tzinfo=timezone.utc)

    platform_message = await _process(message, monkeypatch)

    assert platform_message.ext_data is not None
    assert platform_message.ext_data.forwarded is True


async def test_normal_message_leaves_flag_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform_message = await _process(_FakeMessage(), monkeypatch)

    assert platform_message.ext_data is not None
    assert platform_message.ext_data.forwarded is False
