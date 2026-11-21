"""插件与适配器用的落盘接口也要先清洗文件名

`get_upload_file_path`（`FileUtils` 对插件开放）与 SSE 的 `bytes_to_file` / `base64_to_file`
原来把调用方给的 `file_name` 原样拼进路径，`../../x`、绝对路径或带 NUL 的名字传进来就能写到
指定目录之外或写盘报错。这里钉住这三处接口清洗文件名、落盘前确认仍在目录内。
"""

import base64
import re
from pathlib import Path

import pytest

from nekro_agent.adapters.sse.tools.common import base64_to_file, bytes_to_file
from nekro_agent.tools import common_util, path_convertor
from nekro_agent.tools.common_util import UnsafeUploadPathError
from nekro_agent.tools.file_utils import FileUtils

CHAT_KEY = "wechat_openilink-private_abc"

# 照 test_upload_file_name_safety 的敌意名字，再补上带 NUL 与两种分隔符混用的
HOSTILE_NAMES = [
    "../../../../escaped.txt",
    "..\\..\\..\\escaped.txt",
    "/etc/escaped.txt",
    "C:\\Windows\\escaped.txt",
    "C:escaped.txt",
    "a/../../escaped.txt",
    "a\x00b.txt",
    "x\x00/../../y.txt",
    "a\\..\\..\\escaped.txt",
]


@pytest.fixture
def upload_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "uploads"
    monkeypatch.setattr(path_convertor, "USER_UPLOAD_DIR", str(root))
    return root


def _assert_inside_session_dir(path: str | Path, upload_dir: Path) -> Path:
    resolved = Path(path).resolve()
    session_dir = (upload_dir / CHAT_KEY).resolve()
    assert resolved.is_relative_to(session_dir), path
    assert resolved.parent == session_dir
    return resolved


def _assert_single_file_name(file_name: str) -> None:
    assert "/" not in file_name
    assert "\\" not in file_name
    assert "\x00" not in file_name


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_get_upload_file_path_stays_inside_session_dir(name: str, upload_dir: Path) -> None:
    path = path_convertor.get_upload_file_path(CHAT_KEY, name)

    resolved = _assert_inside_session_dir(path, upload_dir)
    _assert_single_file_name(Path(path).name)
    _assert_single_file_name(resolved.name)
    assert (upload_dir / CHAT_KEY).is_dir()


@pytest.mark.parametrize("name", ["..", ".", "dir/"])
def test_get_upload_file_path_falls_back_to_seed_name(name: str, upload_dir: Path) -> None:
    first = Path(path_convertor.get_upload_file_path(CHAT_KEY, name, use_suffix=".bin", seed="seed-alpha"))
    second = Path(path_convertor.get_upload_file_path(CHAT_KEY, name, use_suffix=".bin", seed="seed-alpha"))

    assert first.name == second.name
    assert re.fullmatch(r"[0-9a-f]{32}\.bin", first.name)
    _assert_inside_session_dir(first, upload_dir)


def test_get_upload_file_path_uses_uuid_when_seed_empty(upload_dir: Path) -> None:
    first = Path(path_convertor.get_upload_file_path(CHAT_KEY, "", use_suffix=".bin"))
    second = Path(path_convertor.get_upload_file_path(CHAT_KEY, "", use_suffix=".bin"))

    assert re.fullmatch(r"[0-9a-f]{32}\.bin", first.name)
    assert re.fullmatch(r"[0-9a-f]{32}\.bin", second.name)
    assert first.name != second.name


def test_symlinked_session_directory_is_refused(upload_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    upload_dir.mkdir(parents=True)
    try:
        (upload_dir / CHAT_KEY).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("本机不允许创建符号链接")

    with pytest.raises(UnsafeUploadPathError):
        path_convertor.get_upload_file_path(CHAT_KEY, "x.txt")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", ["../../../../escaped.txt", "..\\..\\..\\escaped.txt"])
def test_file_utils_get_upload_file_path_stays_inside_session_dir(name: str, upload_dir: Path) -> None:
    path = FileUtils.get_upload_file_path(CHAT_KEY, name)

    resolved = _assert_inside_session_dir(path, upload_dir)
    _assert_single_file_name(resolved.name)


@pytest.mark.parametrize("name", HOSTILE_NAMES)
async def test_bytes_to_file_without_save_dir_stays_inside_upload_dir(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "uploads"
    monkeypatch.setattr("nekro_agent.core.os_env.USER_UPLOAD_DIR", str(root))

    path, file_name = await bytes_to_file(b"payload", file_name=name, mime_type="text/plain")

    resolved = Path(path).resolve()
    assert resolved.is_relative_to(root.resolve())
    assert resolved.exists()
    assert resolved.read_bytes() == b"payload"
    _assert_single_file_name(file_name)


@pytest.mark.parametrize("name", HOSTILE_NAMES)
async def test_bytes_to_file_with_save_dir_stays_inside_save_dir(name: str, tmp_path: Path) -> None:
    box = tmp_path / "box"

    path, file_name = await bytes_to_file(b"payload", file_name=name, mime_type="text/plain", save_dir=box)

    resolved = Path(path).resolve()
    assert resolved.is_relative_to(box.resolve())
    assert resolved.exists()
    assert resolved.read_bytes() == b"payload"
    _assert_single_file_name(file_name)


async def test_bytes_to_file_empty_name_falls_back_to_md5_with_suffix(tmp_path: Path) -> None:
    box = tmp_path / "box"

    path, file_name = await bytes_to_file(b"payload", file_name="..", mime_type="text/plain", save_dir=box)

    assert re.fullmatch(r"[0-9a-f]{32}\.txt", file_name)
    assert Path(path).name == file_name
    assert Path(path).read_bytes() == b"payload"


async def test_base64_to_file_with_hostile_name_stays_inside_save_dir(tmp_path: Path) -> None:
    box = tmp_path / "b64"
    data = base64.b64encode(b"payload").decode()

    path, file_name = await base64_to_file(data, file_name="../../escaped.txt", save_dir=box)

    resolved = Path(path).resolve()
    assert resolved.is_relative_to(box.resolve())
    assert resolved.name == "escaped.txt"
    assert resolved.read_bytes() == b"payload"
    _assert_single_file_name(file_name)


def test_old_common_util_names_point_to_the_moved_objects() -> None:
    assert common_util.safe_upload_file_name is path_convertor.safe_upload_file_name
    assert common_util.UnsafeUploadPathError is path_convertor.UnsafeUploadPathError
