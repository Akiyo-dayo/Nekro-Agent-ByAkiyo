"""消息段里的文件名 = 实际落盘名 = 沙盒路径指向的文件

入站附件的文件名由发件人自己起。四个落盘函数会先清洗文件名再写盘，消息段却原来优先存调用方给的
原名，于是渲染提示词时按 `seg.file_name` 拼出的沙盒路径指向一个不存在的文件。这里把三者钉在一起：
`seg.file_name` 必须是磁盘上真实的名字，`convert_filename_to_sandbox_upload_path(seg.file_name)`
转回宿主机后必须就是 `seg.local_path`，提示词里也必须出现同一个名字。
"""

import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_message import convert_raw_msg_data_json_to_msg_prompt
from nekro_agent.schemas.chat_message import ChatMessageSegmentFile, ChatMessageSegmentImage
from nekro_agent.tools import common_util
from nekro_agent.tools.common_util import safe_upload_file_name
from nekro_agent.tools.path_convertor import (
    convert_filename_to_sandbox_upload_path,
    convert_to_host_path,
    sanitize_chat_key_for_path,
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

# (原名, 期望的落盘名)
NAME_CASES = [
    ("report.", "report"),
    ("report. ", "report"),
    ("report...", "report"),
    ("report .  ", "report"),
    ("...", ""),
    (". . .", ""),
    ("   ", ""),
    ("NUL", ""),
    ("nul", ""),
    ("NUL.txt", ""),
    ("Nul.tar.gz", ""),
    ("NUL ", ""),
    ("NUL .txt", ""),
    ("aux.", ""),
    ("CON", ""),
    ("con.txt", ""),
    ("PRN", ""),
    ("AUX", ""),
    ("COM1", ""),
    ("com9.log", ""),
    ("LPT1", ""),
    ("LPT9.txt", ""),
    ("COM¹", ""),
    ("LPT³.txt", ""),
    ("CONIN$", ""),
    ("conout$.txt", ""),
    ("COM0", "COM0"),
    ("COM0.txt", "COM0.txt"),
    ("COM10", "COM10"),
    ("CONSOLE.txt", "CONSOLE.txt"),
    ("nullable.txt", "nullable.txt"),
    ("cons.txt", "cons.txt"),
    (".hidden", ".hidden"),
    ("photo.png", "photo.png"),
    ("a?b.txt", "ab.txt"),
    ('x*:y"z|w<>.png', "xyzw.png"),
    ("a\x00b\x1fc.txt", "abc.txt"),
    ("x" * 199 + ". " + "y" * 50, "x" * 199),
]

CONSISTENCY_NAMES = [
    "a?b.txt",
    "report.",
    "report. ",
    "NUL",
    "nul.txt",
    "con.txt",
    "COM1",
    'x*:y"z|w<>.png',
    "a\x00b\x1fc.txt",
    "长" * 150 + ".txt",
    "x" * 300 + ".bin",
    "..",
    "photo.png",
]

CREATE_METHODS = ["bytes", "base64", "form_local_path", "url"]


@pytest.fixture
def upload_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "uploads"
    monkeypatch.setattr(common_util, "USER_UPLOAD_DIR", str(root))
    return root


@pytest.mark.parametrize(("raw", "expected"), NAME_CASES)
def test_safe_upload_file_name(raw: str, expected: str) -> None:
    assert safe_upload_file_name(raw) == expected


@pytest.mark.parametrize("raw", [case[0] for case in NAME_CASES] + HOSTILE_NAMES)
def test_safe_upload_file_name_is_idempotent(raw: str) -> None:
    """`download_file` 重试时会把已清洗的名字再传一次，清洗必须稳定"""
    once = safe_upload_file_name(raw)

    assert safe_upload_file_name(once) == once


@pytest.mark.parametrize("name", CONSISTENCY_NAMES)
@pytest.mark.parametrize("create_method", CREATE_METHODS)
@pytest.mark.parametrize(
    "segment_cls",
    [
        pytest.param(ChatMessageSegmentFile, id="file"),
        pytest.param(ChatMessageSegmentImage, id="image"),
    ],
)
async def test_segment_name_matches_written_file(
    segment_cls: type[ChatMessageSegmentFile],
    create_method: str,
    name: str,
    upload_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"payload"

    # Windows 没启用长路径（LongPathsEnabled=0）时普通写盘仍受 MAX_PATH 260 限制：200 字节的名字
    # 在 pytest 的临时目录下总长会超限，落盘直接失败；Linux 容器里没有这个限制，用例照常跑
    expected_path = upload_dir / sanitize_chat_key_for_path(CHAT_KEY) / safe_upload_file_name(name)
    if sys.platform == "win32" and len(str(expected_path)) >= 260:
        pytest.skip("Windows 未启用长路径，该文件名在临时目录下超过 MAX_PATH")

    if create_method == "bytes":
        seg = await segment_cls.create_from_bytes(payload, CHAT_KEY, file_name=name, use_suffix=".bin")
    elif create_method == "base64":
        data = base64.b64encode(payload).decode()
        seg = await segment_cls.create_from_base64(data, CHAT_KEY, file_name=name, use_suffix=".bin")
    elif create_method == "form_local_path":
        source = tmp_path / "source.bin"
        source.write_bytes(payload)
        seg = await segment_cls.create_form_local_path(str(source), CHAT_KEY, file_name=name, use_suffix=".bin")
    else:

        async def allow(_url: str) -> None:
            return None

        async def fetch(_client: Any, _url: str, *_args: Any, **_kwargs: Any) -> bytes:
            return payload

        monkeypatch.setattr(common_util, "assert_download_url_safe", allow)
        monkeypatch.setattr(common_util, "_fetch_following_redirects", fetch)
        seg = await segment_cls.create_from_url("https://example.com/a.bin", CHAT_KEY, file_name=name, use_suffix=".bin")

    # 1. 真的落盘了，内容就是传入的内容
    assert seg.local_path is not None
    written = Path(seg.local_path)
    assert written.is_file()
    assert written.read_bytes() == payload

    # 2. 会话目录里恰好一个文件，就是消息段里的名字（写进设备、目录里没文件会在这里露馅）
    session_dir = upload_dir / sanitize_chat_key_for_path(CHAT_KEY)
    assert [path.name for path in session_dir.iterdir()] == [seg.file_name]

    # 3. 消息段存的是磁盘上的名字，且是一个干净的单段文件名
    assert seg.file_name == written.name
    assert seg.file_name
    assert "/" not in seg.file_name and "\\" not in seg.file_name

    # 4. 展示文本里的名字与文件名一致
    kind = seg.get_segment_type().value.capitalize()
    assert seg.text == f"[{kind}: {seg.file_name}]"

    # 5. 沙盒路径转回宿主机后就是落盘文件
    sandbox_path = convert_filename_to_sandbox_upload_path(seg.file_name)
    assert convert_to_host_path(sandbox_path, CHAT_KEY, uploads_dir=upload_dir).resolve() == written.resolve()

    # 6. 提示词里给模型的路径也指向同一个文件
    prompt = convert_raw_msg_data_json_to_msg_prompt(
        json.dumps([seg.model_dump(mode="json")], ensure_ascii=False),
        "tok",
        config,
        travel_mode=True,
    )
    # 沙盒里的路径是正斜杠；Windows 上 Path 渲染成反斜杠，断言前统一归一化
    assert f"{kind}:/app/uploads/{seg.file_name}" in prompt.replace("\\", "/")


async def test_question_mark_name_uses_the_written_name(upload_dir: Path) -> None:
    """原报告场景：`a?b.txt` 落盘为 `ab.txt`，消息段必须也存 `ab.txt`"""
    seg = await ChatMessageSegmentFile.create_from_bytes(b"payload", CHAT_KEY, file_name="a?b.txt", use_suffix=".bin")

    assert seg.file_name == "ab.txt"


@pytest.mark.parametrize("name", ["NUL", "nul.txt"])
async def test_reserved_device_name_falls_back_to_content_hash(name: str, upload_dir: Path) -> None:
    """Windows 把 `NUL` 当空设备、写不进文件；按空名处理后退回内容哈希命名"""
    seg = await ChatMessageSegmentFile.create_from_bytes(b"payload", CHAT_KEY, file_name=name, use_suffix=".bin")

    assert seg.file_name == f"{hashlib.md5(b'payload').hexdigest()}.bin"
