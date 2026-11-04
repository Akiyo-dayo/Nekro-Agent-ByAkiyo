"""历史图片读取：`local_path` 只认本会话上传目录

`ChatMessageSegmentImage.local_path` 是消息段里的自由字段：入站、插件、命令输出、
导入的旧数据都能往里写。历史渲染按它去读宿主文件，就等于"消息段里写哪个路径就读哪个"
——一份伪造的记录可以把 `/app/configs/nekro-agent.yaml`（平台方全部模型组的 API_KEY）
塞进视觉上下文，再让模型原样复述出来。

所以只接受 resolve 之后仍落在本会话上传目录里的候选；`file_name` 那条老路照旧
（`convert_filename_to_access_path` 本身就限定在上传目录内）。
"""

from pathlib import Path
from typing import Any

import pytest

from nekro_agent.schemas.chat_message import ChatMessageSegmentImage, ChatMessageSegmentType
from nekro_agent.services.agent.templates.history import _resolve_image_access_path
from nekro_agent.tools.path_convertor import sanitize_chat_key_for_path

CHAT_KEY = "web-session_test"


def _image_segment(*, file_name: str = "", local_path: str = "") -> ChatMessageSegmentImage:
    return ChatMessageSegmentImage(
        type=ChatMessageSegmentType.IMAGE,
        text=f"[Image: {file_name or Path(local_path).name}]",
        file_name=file_name,
        local_path=local_path,
    )


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 `USER_UPLOAD_DIR` 指到临时目录，本会话的上传目录一并建好

    两个模块都在导入时把常量绑到了自己的命名空间上（`history` 与 `path_convertor`），
    所以两处都要改。
    """
    from nekro_agent.services.agent.templates import history as history_mod
    from nekro_agent.tools import path_convertor

    uploads = tmp_path / "uploads"
    monkeypatch.setattr(history_mod, "USER_UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(path_convertor, "USER_UPLOAD_DIR", str(uploads))
    chat_dir = uploads / sanitize_chat_key_for_path(CHAT_KEY)
    chat_dir.mkdir(parents=True, exist_ok=True)
    return chat_dir


def test_local_path_outside_session_upload_dir_is_dropped(upload_dir: Path, tmp_path: Path) -> None:
    """会话上传目录之外的真实图片不读：路径存在不是授权"""
    outside = tmp_path / "elsewhere" / "secret.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"secret")

    seg = _image_segment(file_name="secret.png", local_path=str(outside))

    # 会话目录里没有同名文件，file_name 那条候选也落空
    assert _resolve_image_access_path(seg, CHAT_KEY) is None


def test_local_path_outside_is_dropped_even_when_file_name_falls_back_inside(upload_dir: Path, tmp_path: Path) -> None:
    """越界的 `local_path` 被丢弃后，退回按 `file_name` 在本会话目录里找"""
    outside = tmp_path / "elsewhere" / "note.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"outside")
    inside = upload_dir / "note.png"
    inside.write_bytes(b"inside")

    seg = _image_segment(file_name="note.png", local_path=str(outside))

    resolved = _resolve_image_access_path(seg, CHAT_KEY)
    assert resolved is not None
    assert resolved == inside


def test_local_path_inside_session_upload_dir_is_used(upload_dir: Path) -> None:
    inside = upload_dir / "cat.png"
    inside.write_bytes(b"cat")

    seg = _image_segment(file_name="cat.png", local_path=str(inside))

    assert _resolve_image_access_path(seg, CHAT_KEY) == inside


def test_local_path_inside_dir_but_without_file_name_is_used(upload_dir: Path) -> None:
    """新记录可能只有 local_path（file_name 为空）：沙盒路径按文件名兜底"""
    inside = upload_dir / "only-local.png"
    inside.write_bytes(b"png")

    seg = _image_segment(file_name="", local_path=str(inside))

    assert _resolve_image_access_path(seg, CHAT_KEY) == inside


def test_symlink_escaping_upload_dir_is_dropped(upload_dir: Path, tmp_path: Path) -> None:
    """软链指到目录外也要挡：判据是 resolve 之后的位置"""
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    link = upload_dir / "link.png"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前平台不允许创建软链")

    # file_name 留空：只剩 local_path 一条候选，被丢弃后就没有别的来源了
    seg = _image_segment(file_name="", local_path=str(link))

    assert _resolve_image_access_path(seg, CHAT_KEY) is None


def test_legacy_record_with_only_file_name_is_readable(upload_dir: Path) -> None:
    """只有 file_name 的旧记录照旧可读（老 Web Chat 入站图片）"""
    inside = upload_dir / "legacy.png"
    inside.write_bytes(b"legacy")

    seg = _image_segment(file_name="legacy.png", local_path="")

    assert _resolve_image_access_path(seg, CHAT_KEY) == inside


def test_missing_everywhere_returns_none(upload_dir: Path) -> None:
    seg = _image_segment(file_name="nope.png", local_path="")

    assert _resolve_image_access_path(seg, CHAT_KEY) is None


def test_traversal_in_local_path_is_dropped(upload_dir: Path, tmp_path: Path) -> None:
    """`../` 词法穿越同样挡在 resolve 之后"""
    outside = tmp_path / "traverse.png"
    outside.write_bytes(b"traverse")
    sneaky = str(upload_dir / ".." / ".." / "traverse.png")

    seg = _image_segment(file_name="", local_path=sneaky)

    assert _resolve_image_access_path(seg, CHAT_KEY) is None


class _FakeExtData:
    ref_msg_id = ""


class _FakeDbMessage:
    """只够 `render_history_data` 走完流程的假消息行"""

    is_system = False
    message_id = "msg-1"
    ext_data_obj = _FakeExtData()

    def __init__(self, segments: list[Any]):
        self._segments = segments

    def parse_content_data(self) -> list[Any]:
        return self._segments

    def parse_chat_history_prompt(self, *_args: Any, **_kwargs: Any) -> str:
        return "[fake message]"


async def _render_with_segments(
    monkeypatch: pytest.MonkeyPatch,
    segments: list[Any],
    *,
    chat_key: str = CHAT_KEY,
) -> Any:
    """跑一遍 `render_history_data`，只关心它读到了哪张图"""
    from nekro_agent.services.agent import creator as creator_mod
    from nekro_agent.services.agent.templates import history as history_mod

    read_paths: list[str] = []

    def fake_image_content_from_path(path: str) -> dict[str, Any]:
        read_paths.append(path)
        return {"type": "image_url", "image_url": {"url": "data:fake"}}

    class _FakeChannel:
        conversation_start_time = __import__("datetime").datetime(2000, 1, 1)
        workspace_id = None

        async def get_preset(self) -> Any:
            return None

    class _FakeConfig:
        AI_CHAT_CONTEXT_EXPIRE_SECONDS = 0
        AI_CHAT_CONTEXT_MAX_LENGTH = 10
        AI_SYSTEM_NOTIFY_WINDOW_SIZE = 0
        AI_SYSTEM_NOTIFY_LIMIT = 0
        AI_VISION_IMAGE_LIMIT = 10
        AI_VISION_IMAGE_SIZE_LIMIT_KB = 1024 * 1024
        MEMORY_ENABLE_ENHANCED_RETRIEVAL = False
        AI_ALWAYS_INCLUDE_MSG_ID = False
        AI_CONTEXT_LENGTH_PER_SESSION = 100000

    def fake_filter(*_args: Any, **_kwargs: Any) -> Any:
        class _QS:
            def order_by(self, *_a: Any, **_k: Any) -> "_QS":
                return self

            def limit(self, *_a: Any, **_k: Any) -> "_QS":
                return self

            async def __aiter__(self):
                yield _FakeDbMessage(segments)

            def __await__(self):
                async def _inner():
                    return [_FakeDbMessage(segments)]

                return _inner().__await__()

        return _QS()

    async def fake_inject_memory_context(**_kwargs: Any) -> str:
        return ""

    class _FakeGroup:
        CHAT_MODEL = "fake-model"

    monkeypatch.setattr(history_mod, "supports_vision", lambda *_args, **_kwargs: True)

    monkeypatch.setattr(history_mod.DBChatMessage, "filter", staticmethod(fake_filter))
    monkeypatch.setattr(history_mod, "_inject_memory_context", fake_inject_memory_context)
    monkeypatch.setattr(creator_mod.ContentSegment, "image_content_from_path", staticmethod(fake_image_content_from_path))

    await history_mod.render_history_data(
        chat_key=chat_key,
        db_chat_channel=_FakeChannel(),  # type: ignore[arg-type]
        one_time_code="code",
        config=_FakeConfig(),  # type: ignore[arg-type]
        model_group=_FakeGroup(),  # type: ignore[arg-type]
    )
    return read_paths


async def test_render_history_data_skips_image_outside_upload_dir(
    monkeypatch: pytest.MonkeyPatch,
    upload_dir: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside" / "leak.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"leak")

    read_paths = await _render_with_segments(
        monkeypatch,
        [_image_segment(file_name="leak.png", local_path=str(outside))],
    )

    assert read_paths == []


async def test_render_history_data_reads_image_inside_upload_dir(
    monkeypatch: pytest.MonkeyPatch,
    upload_dir: Path,
) -> None:
    inside = upload_dir / "ok.png"
    inside.write_bytes(b"ok")

    read_paths = await _render_with_segments(
        monkeypatch,
        [_image_segment(file_name="ok.png", local_path=str(inside))],
    )

    assert len(read_paths) == 1
    assert Path(read_paths[0]).name == "ok.png"
