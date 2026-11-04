"""Web Adapter 的发送路径

`forward_message` 是这个适配器唯一的"协议端"：它自己把回复写进会话历史，并返回
`recorded=True` 让通用发送层跳过重复记录。这里钉住三件事：文本与媒体都进了历史、
远端媒体先落盘、`recorded=True` 时通用层不再写第二遍。
"""

from pathlib import Path
from typing import Any, List

import pytest

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendSegment,
    PlatformSendSegmentType,
)
from nekro_agent.adapters.web.adapter import WebAdapter
from nekro_agent.schemas.agent_message import AgentMessageSegmentType


class _FakeMessageService:
    def __init__(self) -> None:
        self.calls: List[dict[str, Any]] = []

    async def push_bot_message(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append({"args": args, "kwargs": kwargs})


@pytest.mark.asyncio
async def test_web_adapter_forward_message_records_text_and_media(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from nekro_agent.services import message_service as message_service_mod

    image_path = tmp_path / "help.png"
    image_path.write_bytes(b"fake-png")
    recorder = _FakeMessageService()
    monkeypatch.setattr(message_service_mod, "message_service", recorder)

    adapter = WebAdapter()
    response = await adapter.forward_message(
        PlatformSendRequest(
            chat_key="web-session_test",
            segments=[
                PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="帮助结果"),
                PlatformSendSegment(type=PlatformSendSegmentType.IMAGE, file_path=str(image_path)),
            ],
            ref_msg_id="ref_1",
        ),
    )

    assert response.success is True
    assert response.recorded is True
    assert response.message_id and response.message_id.startswith("webout_")
    assert len(recorder.calls) == 1

    call = recorder.calls[0]
    assert call["args"][0] == "web-session_test"
    assert call["args"][2] is response
    assert call["kwargs"] == {"ref_msg_id": "ref_1", "normalize_at_markup": False}

    messages = call["args"][1]
    assert [message.type for message in messages] == [AgentMessageSegmentType.TEXT, AgentMessageSegmentType.IMAGE]
    assert messages[0].content == "帮助结果"
    assert messages[1].content == str(image_path)


@pytest.mark.asyncio
async def test_web_adapter_forward_message_downloads_remote_media(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from nekro_agent.adapters.web import adapter as adapter_mod
    from nekro_agent.services import message_service as message_service_mod

    downloaded_path = tmp_path / "remote.png"
    downloaded_path.write_bytes(b"remote-png")
    recorder = _FakeMessageService()
    monkeypatch.setattr(message_service_mod, "message_service", recorder)

    async def fake_download_file(url: str, *, from_chat_key: str, **kwargs: Any) -> tuple[str, str]:
        del kwargs
        assert url == "https://example.com/remote.png"
        assert from_chat_key == "web-session_test"
        return str(downloaded_path), downloaded_path.name

    monkeypatch.setattr(adapter_mod, "download_file", fake_download_file)

    response = await WebAdapter().forward_message(
        PlatformSendRequest(
            chat_key="web-session_test",
            segments=[
                PlatformSendSegment(
                    type=PlatformSendSegmentType.IMAGE,
                    file_path="https://example.com/remote.png",
                ),
            ],
        ),
    )

    assert response.success is True
    assert response.recorded is True
    messages = recorder.calls[0]["args"][1]
    assert [message.type for message in messages] == [AgentMessageSegmentType.IMAGE]
    assert messages[0].content == str(downloaded_path)


@pytest.mark.asyncio
async def test_web_adapter_forward_message_skips_missing_local_media(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """本地文件不存在就跳过该段；只剩文本时照旧发送"""
    from nekro_agent.services import message_service as message_service_mod

    recorder = _FakeMessageService()
    monkeypatch.setattr(message_service_mod, "message_service", recorder)

    response = await WebAdapter().forward_message(
        PlatformSendRequest(
            chat_key="web-session_test",
            segments=[
                PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="只有文本"),
                PlatformSendSegment(type=PlatformSendSegmentType.IMAGE, file_path=str(tmp_path / "gone.png")),
            ],
        ),
    )

    assert response.success is True
    messages = recorder.calls[0]["args"][1]
    assert [message.type for message in messages] == [AgentMessageSegmentType.TEXT]


@pytest.mark.asyncio
async def test_web_adapter_forward_message_rejects_empty_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services import message_service as message_service_mod

    recorder = _FakeMessageService()
    monkeypatch.setattr(message_service_mod, "message_service", recorder)

    response = await WebAdapter().forward_message(PlatformSendRequest(chat_key="web-session_test", segments=[]))

    assert response.success is False
    assert recorder.calls == []


def test_web_adapter_declares_command_input_recording() -> None:
    """命令原文入库依赖这个属性；网页适配器必须声明为真"""
    assert WebAdapter().record_command_input is True
