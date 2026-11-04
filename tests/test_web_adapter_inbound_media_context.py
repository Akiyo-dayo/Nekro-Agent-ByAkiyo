"""网页会话入站媒体的落盘路径

上传的图片/文件必须把**保存后的** `local_path` 写进消息段：只写 `file_name` 的话，
历史渲染要靠 `file_name` 再推一次路径，而沙盒侧 `./uploads/<filename>` 与宿主侧
`USER_UPLOAD_DIR/<chat_key>/<filename>` 只在两者一致时才碰得上。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from starlette.datastructures import Headers, UploadFile

from nekro_agent.schemas.chat_message import ChatMessageSegmentImage, ChatMessageSegmentType


class _FakeConfig:
    MESSAGE_MAX_LENGTH = 8000
    FILE_UPLOAD_MAX_SIZE_MB = 10


class _FakeAdapter:
    config = _FakeConfig()


class _FakeChannel:
    chat_key = "web-session_test"


class _FakeUser:
    id = 1
    username = "admin"
    perm_level = 3


@pytest.mark.asyncio
async def test_web_upload_message_records_saved_local_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from nekro_agent.adapters.web import routers

    captured: dict[str, Any] = {}

    async def fake_get_web_channel(chat_key: str) -> _FakeChannel:
        assert chat_key == _FakeChannel.chat_key
        return _FakeChannel()

    async def fake_collect_web_message(
        adapter: _FakeAdapter,
        channel: _FakeChannel,
        current_user: _FakeUser,
        content: str,
        content_data: list[Any],
    ) -> routers.WebMessageCreateResponse:
        del adapter, current_user
        captured["content"] = content
        captured["content_data"] = content_data
        return routers.WebMessageCreateResponse(ok=True, chat_key=channel.chat_key, message_id="webmsg_test")

    async def fake_save_upload_file(chat_key: str, file: Any, max_size_mb: int) -> tuple[str, str]:
        assert chat_key == _FakeChannel.chat_key
        assert file.filename == "cat.png"
        assert max_size_mb == _FakeConfig.FILE_UPLOAD_MAX_SIZE_MB
        saved_path = tmp_path / "uploads" / chat_key / "cat.png"
        saved_path.parent.mkdir(parents=True)
        saved_path.write_bytes(b"fake-png")
        return str(saved_path), "cat.png"

    monkeypatch.setattr(routers, "_save_upload_file", fake_save_upload_file)
    monkeypatch.setattr(routers, "_get_adapter", lambda: _FakeAdapter())
    monkeypatch.setattr(routers, "_get_web_channel", fake_get_web_channel)
    monkeypatch.setattr(routers, "_collect_web_message", fake_collect_web_message)

    upload = UploadFile(
        file=io.BytesIO(b"fake-png"),
        filename="cat.png",
        headers=Headers({"content-type": "image/png"}),
    )

    response = await routers.create_web_upload_message(
        _FakeChannel.chat_key,
        content="看看这张图",
        file=upload,
        _current_user=_FakeUser(),
    )

    assert response.ok is True
    image_segment = captured["content_data"][1]
    assert isinstance(image_segment, ChatMessageSegmentImage)
    assert image_segment.type == ChatMessageSegmentType.IMAGE.value
    assert image_segment.file_name.endswith("cat.png")
    assert image_segment.local_path
    assert Path(image_segment.local_path).is_file()
    assert Path(image_segment.local_path).parent == tmp_path / "uploads" / _FakeChannel.chat_key


def test_history_image_access_path_falls_back_to_file_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """只有 `file_name` 的旧记录照旧可读：走 `convert_filename_to_access_path` 那条老路"""
    from nekro_agent.services.agent.templates import history

    image_path = tmp_path / "uploads" / _FakeChannel.chat_key / "old.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"fake-png")

    monkeypatch.setattr(
        history,
        "convert_filename_to_access_path",
        lambda file_name, chat_key: tmp_path / "uploads" / chat_key / Path(file_name).name,
    )

    segment = ChatMessageSegmentImage(
        type=ChatMessageSegmentType.IMAGE,
        text="[Image: old.png]",
        file_name="old.png",
    )

    assert history._resolve_image_access_path(segment, _FakeChannel.chat_key) == image_path
