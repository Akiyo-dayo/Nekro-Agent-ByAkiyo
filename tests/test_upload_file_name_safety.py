"""落盘到上传目录的文件名不能把文件带出上传目录

文件名常常来自外部：OpenILink / QQ 机器人上报的附件名就是发件人自己起的。四个落盘函数
（`download_file`、`download_file_from_bytes`、`download_file_from_base64`、`copy_to_upload_dir`）
原来把它原样拼进 `USER_UPLOAD_DIR/<会话>/`，`../../x` 或一个绝对路径就能写到上传目录之外。
第 6 批移植的 OpenILink 入站媒体把这条路径直接交给了任何给机器人发消息的人（独立复核用探针复现）。
"""

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nekro_agent.tools import common_util
from nekro_agent.tools.common_util import (
    UnsafeUploadPathError,
    copy_to_upload_dir,
    download_file,
    download_file_from_base64,
    download_file_from_bytes,
    safe_upload_file_name,
)

CHAT_KEY = "wechat_openilink-private_abc"

HOSTILE_NAMES = [
    "../../../../escaped.txt",
    "..\\..\\..\\escaped.txt",
    "/etc/escaped.txt",
    "C:\\Windows\\escaped.txt",
    "C:escaped.txt",
    "a/../../escaped.txt",
]


@pytest.fixture
def upload_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "uploads"
    monkeypatch.setattr(common_util, "USER_UPLOAD_DIR", str(root))
    return root


def _assert_inside(path: str, upload_dir: Path) -> None:
    resolved = Path(path).resolve()
    assert resolved.is_relative_to((upload_dir / CHAT_KEY).resolve()), path
    assert resolved.exists()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("photo.png", "photo.png"),
        ("../../../../escaped.txt", "escaped.txt"),
        ("..\\..\\escaped.txt", "escaped.txt"),
        ("/etc/passwd", "passwd"),
        ("C:\\Windows\\win.ini", "win.ini"),
        ("C:escaped.txt", "Cescaped.txt"),
        ("..", ""),
        (".", ""),
        ("dir/", ""),
        ("a\x00b\x1fc.txt", "abc.txt"),
        ("file.txt:stream", "file.txtstream"),
        ("", ""),
        (".hidden", ".hidden"),
    ],
)
def test_safe_upload_file_name(raw: str, expected: str) -> None:
    assert safe_upload_file_name(raw) == expected


def test_safe_upload_file_name_limits_length() -> None:
    assert len(safe_upload_file_name("x" * 1000 + ".txt")) == 200


def test_safe_upload_file_name_limits_bytes_not_characters() -> None:
    """中文名按字符截到 200 会有 600 字节，超过文件系统 255 字节的上限；按字节截且不截断半个字"""
    name = safe_upload_file_name("图" * 300 + ".png")

    assert len(name.encode("utf-8")) <= 200
    assert name == "图" * (200 // 3)


async def test_long_chinese_name_can_be_written(upload_dir: Path) -> None:
    path, _file_name = await download_file_from_bytes(b"payload", file_name="长" * 150 + ".txt", from_chat_key=CHAT_KEY)

    _assert_inside(path, upload_dir)


@pytest.mark.parametrize("name", HOSTILE_NAMES)
async def test_bytes_stay_inside_the_session_directory(name: str, upload_dir: Path) -> None:
    path, file_name = await download_file_from_bytes(b"payload", file_name=name, from_chat_key=CHAT_KEY)

    _assert_inside(path, upload_dir)
    assert "/" not in file_name and "\\" not in file_name


@pytest.mark.parametrize("name", HOSTILE_NAMES)
async def test_base64_stays_inside_the_session_directory(name: str, upload_dir: Path) -> None:
    data = base64.b64encode(b"payload").decode()

    path, _file_name = await download_file_from_base64(data, file_name=name, from_chat_key=CHAT_KEY)

    _assert_inside(path, upload_dir)


@pytest.mark.parametrize("name", HOSTILE_NAMES)
async def test_copy_stays_inside_the_session_directory(name: str, upload_dir: Path, tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")

    path, _file_name = await copy_to_upload_dir(str(source), file_name=name, from_chat_key=CHAT_KEY)

    _assert_inside(path, upload_dir)


@pytest.mark.parametrize("name", HOSTILE_NAMES)
async def test_download_stays_inside_the_session_directory(
    name: str,
    upload_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def allow(_url: str) -> None:
        return None

    async def fetch(_client: Any, _url: str, *_args: Any, **_kwargs: Any) -> bytes:
        return b"payload"

    monkeypatch.setattr(common_util, "assert_download_url_safe", allow)
    monkeypatch.setattr(common_util, "_fetch_following_redirects", fetch)

    path, _file_name = await download_file("https://example.com/a.bin", file_name=name, from_chat_key=CHAT_KEY)

    _assert_inside(path, upload_dir)


async def test_empty_name_after_cleaning_falls_back_to_content_hash(upload_dir: Path) -> None:
    path, file_name = await download_file_from_bytes(b"payload", file_name="..", use_suffix=".bin", from_chat_key=CHAT_KEY)

    assert file_name.endswith(".bin") and len(file_name) == 32 + 4
    _assert_inside(path, upload_dir)


async def test_symlinked_session_directory_is_refused(upload_dir: Path, tmp_path: Path) -> None:
    """文件名之外的兜底：会话目录本身指向上传目录之外"""
    outside = tmp_path / "outside"
    outside.mkdir()
    upload_dir.mkdir(parents=True)
    try:
        (upload_dir / CHAT_KEY).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("本机不允许创建符号链接")

    with pytest.raises(UnsafeUploadPathError):
        await download_file_from_bytes(b"payload", file_name="x.txt", from_chat_key=CHAT_KEY)
    assert list(outside.iterdir()) == []


async def test_openilink_inbound_file_name_cannot_escape(upload_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """独立复核的探针：发件人把图片名写成 `../../../../pwn.png`"""
    from nekro_agent.adapters.wechat_openilink.config import WeChatOpenILinkConfig
    from nekro_agent.adapters.wechat_openilink.message_processor import OpenILinkMessageProcessor

    async def allow(_url: str) -> None:
        return None

    async def fetch(_client: Any, _url: str, *_args: Any, **_kwargs: Any) -> bytes:
        return b"\x89PNG\r\n\x1a\n"

    monkeypatch.setattr(common_util, "assert_download_url_safe", allow)
    monkeypatch.setattr(common_util, "_fetch_following_redirects", fetch)

    processor = OpenILinkMessageProcessor(
        config=WeChatOpenILinkConfig(),
        adapter_key="wechat_openilink",
        build_chat_key=lambda _channel_id: CHAT_KEY,
    )
    incoming = SimpleNamespace(
        user_id="wxid_attacker",
        type="image",
        timestamp=None,
        raw={
            "from_user_id": "wxid_attacker",
            "message_id": "m1",
            "create_time_ms": 1700000000000,
            "item_list": [{"type": 2, "image_item": {"url": "https://example.com/a.png", "file_name": "../../../../pwn.png"}}],
        },
    )

    parsed = await processor.parse(incoming)

    assert parsed is not None
    local_paths = [getattr(segment, "local_path", "") for segment in parsed.message.content_data if getattr(segment, "local_path", "")]
    assert local_paths
    for local_path in local_paths:
        _assert_inside(local_path, upload_dir)
    assert not (upload_dir.parent.parent / "pwn.png").exists()
