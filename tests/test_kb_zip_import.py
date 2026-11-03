"""知识库 zip 导入的安全用例

`services/kb/zip_import.py` 吃的是**完全不可信**的字节：条目名、条目头里声明的大小、
中央目录本身都可以伪造。这些用例分两档来钉：

- 压缩包级别：路径穿越、文件数、中央目录预算、声明总大小、单文件上限、加密条目
  一律 `ValueError`，且**一条都不落盘**（正常条目也不落盘，不做半截解压）；
- 条目级别：符号链接、macOS 元数据、扩展名不符、单条导入失败只影响那一条。

声明值既然可以伪造，光靠"解压前按声明值预检"是不够的，所以 `_copy_bounded` 另有一组
"按实际写出的字节计数"的直测；伪造条目头那一条则把两个预检都骗过去，看的是落盘时
的实际字节上限。
"""

import io
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi import UploadFile

from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.kb import zip_import
from nekro_agent.services.kb.zip_import import (
    ZipImportEntry,
    _copy_bounded,
    extract_zip_safe,
    import_zip_with_upload,
)

# ---------------------------------------------------------------------------
# 现造压缩包
# ---------------------------------------------------------------------------


def _zip_payload(entries: list[tuple[str, bytes]]) -> bytes:
    """构造内存 zip；条目名先建 `ZipInfo` 再赋值，绕过构造函数对反斜杠的改写"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            archive.writestr(info, data)
    return buffer.getvalue()


def _write_zip(tmp_path: Path, entries: list[tuple[str, bytes]], *, filename: str = "case.zip") -> Path:
    zip_path = tmp_path / filename
    zip_path.write_bytes(_zip_payload(entries))
    return zip_path


def _target_dir(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    return target


def _extract(zip_path: Path, target: Path, *, max_entry_size: int = 4096):
    return extract_zip_safe(zip_path, target, max_entry_size=max_entry_size)


def _upload(payload: bytes, *, filename: str = "x.zip") -> UploadFile:
    return UploadFile(file=io.BytesIO(payload), filename=filename)


def _patch_declared_entry_size(payload: bytes, size: int) -> bytes:
    """把本地文件头与中央目录项里声明的 uncompressed size 都改成 `size`"""
    data = bytearray(payload)
    local_header = data.index(b"PK\x03\x04")
    central_header = data.index(b"PK\x01\x02")
    data[local_header + 22 : local_header + 26] = size.to_bytes(4, "little")
    data[central_header + 24 : central_header + 28] = size.to_bytes(4, "little")
    return bytes(data)


def _patch_flag_bits(payload: bytes, flags: int) -> bytes:
    """同时改本地文件头与中央目录项里的通用标志位"""
    data = bytearray(payload)
    local_header = data.index(b"PK\x03\x04")
    central_header = data.index(b"PK\x01\x02")
    data[local_header + 6 : local_header + 8] = flags.to_bytes(2, "little")
    data[central_header + 8 : central_header + 10] = flags.to_bytes(2, "little")
    return bytes(data)


# ---------------------------------------------------------------------------
# 路径穿越：一个非法条目就整包拒绝
# ---------------------------------------------------------------------------


# (条目名, 拒绝原因里应出现的片段)。原因逐条钉住：拆掉其中一道判定时，
# 那个名字要么被放进来、要么换成别的原因被拒，两种都会红
ILLEGAL_ENTRY_NAMES = [
    ("../evil.md", "越界"),
    ("a/../../evil.md", "越界"),
    ("/abs/evil.md", "绝对路径"),
    ("C:/evil.md", "绝对路径"),
    ("C:evil.md", "绝对路径"),
    ("\\evil.md", "绝对路径"),
    ("a/b:stream.md", "':'"),
    ("bad\x01name.md", "非法条目名"),
]


@pytest.mark.parametrize(("bad_name", "reason"), ILLEGAL_ENTRY_NAMES)
def test_illegal_member_name_rejects_the_whole_archive(tmp_path: Path, bad_name: str, reason: str) -> None:
    zip_path = _write_zip(tmp_path, [("ok.md", b"ok body"), (bad_name, b"evil body")])
    target = _target_dir(tmp_path)

    with pytest.raises(ValueError, match=reason):
        _extract(zip_path, target)

    # 正常条目也不能先落盘：拒绝是整包级别的
    assert list(target.iterdir()) == []
    assert not (tmp_path / "evil.md").exists()


# ---------------------------------------------------------------------------
# 解压炸弹：文件数 / 中央目录 / 声明大小 / 单条上限 / 伪造条目头
# ---------------------------------------------------------------------------


def test_too_many_files_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(zip_import, "_MAX_FILES", 3)
    zip_path = _write_zip(tmp_path, [(f"f{index}.md", b"x") for index in range(4)])
    target = _target_dir(tmp_path)

    with pytest.raises(ValueError, match="文件数"):
        _extract(zip_path, target)

    assert list(target.iterdir()) == []


def test_central_directory_budget_is_checked_before_the_archive_is_opened(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(zip_import, "_MAX_CENTRAL_DIRECTORY_SIZE", 64)
    zip_path = _write_zip(tmp_path, [(f"f{index}.md", b"x") for index in range(5)])
    target = _target_dir(tmp_path)

    def _must_not_open(*_args, **_kwargs):
        raise AssertionError("中央目录预检必须发生在打开压缩包之前")

    # 只换 `ZipFile`：预检自己用的是 `zipfile._EndRecData`，不能一起换掉
    monkeypatch.setattr(zip_import.zipfile, "ZipFile", _must_not_open)

    with pytest.raises(ValueError, match="条目过多"):
        _extract(zip_path, target)

    assert list(target.iterdir()) == []


def test_declared_total_size_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(zip_import, "_MAX_EXTRACT_SIZE", 1000)
    zip_path = _write_zip(tmp_path, [("a.md", b"a" * 600), ("b.md", b"b" * 600)])
    target = _target_dir(tmp_path)

    with pytest.raises(ValueError, match="总大小"):
        _extract(zip_path, target, max_entry_size=10_000)

    assert list(target.iterdir()) == []


def test_declared_entry_size_is_rejected(tmp_path: Path) -> None:
    zip_path = _write_zip(tmp_path, [("big.md", b"x" * 200)])
    target = _target_dir(tmp_path)

    with pytest.raises(ValueError, match="过大"):
        _extract(zip_path, target, max_entry_size=100)

    assert list(target.iterdir()) == []


def test_forged_entry_headers_still_stop_at_the_limit(tmp_path: Path) -> None:
    """声明值改成 10、实际 5000 字节：两道预检都被骗过，但落盘不会超过声明值

    标准库按声明值截断读出，随后 CRC 不符，整包按"无法解压"拒绝。这一条钉的是
    端到端结果（落盘字节受限 + 整包拒绝），`_copy_bounded` 自己的计数由下面两条直测。
    """
    zip_path = _write_zip(tmp_path, [("forged.md", b"x" * 5000)])
    zip_path.write_bytes(_patch_declared_entry_size(zip_path.read_bytes(), 10))
    target = _target_dir(tmp_path)

    with pytest.raises(ValueError, match="无法解压"):
        _extract(zip_path, target, max_entry_size=100)

    assert all(path.stat().st_size <= 10 for path in target.rglob("*") if path.is_file())


def test_copy_bounded_rejects_an_entry_over_its_limit() -> None:
    written = io.BytesIO()

    with pytest.raises(ValueError, match="过大"):
        _copy_bounded(io.BytesIO(b"x" * 300), written, entry_limit=100, total_remaining=1000, name="a.md")

    assert len(written.getvalue()) <= 100


def test_copy_bounded_rejects_the_running_total() -> None:
    written = io.BytesIO()

    with pytest.raises(ValueError, match="总大小"):
        _copy_bounded(io.BytesIO(b"x" * 300), written, entry_limit=1000, total_remaining=100, name="a.md")

    assert len(written.getvalue()) <= 100


# ---------------------------------------------------------------------------
# 加密 / 符号链接 / macOS 元数据 / 正常包 / 目录文件同名
# ---------------------------------------------------------------------------


def test_encrypted_entry_is_rejected(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("secret.md")
        info.flag_bits |= 0x1
        archive.writestr(info, b"secret")

    # CPython 的写入侧会把 flag_bits 清掉，这里直接改两个头里的标志位，
    # 才造得出"读的时候确实标着加密"的压缩包
    zip_path = tmp_path / "encrypted.zip"
    zip_path.write_bytes(_patch_flag_bits(buffer.getvalue(), 0x1))
    target = _target_dir(tmp_path)

    with pytest.raises(ValueError, match="加密"):
        _extract(zip_path, target)

    assert list(target.iterdir()) == []


def test_symlink_entry_is_skipped(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        link = zipfile.ZipInfo("link.md")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b"../escape")
        archive.writestr("ok.md", b"ok body")

    zip_path = tmp_path / "symlink.zip"
    zip_path.write_bytes(buffer.getvalue())
    target = _target_dir(tmp_path)

    entries, skipped = _extract(zip_path, target)

    assert [entry.source_path for entry in entries] == ["ok.md"]
    assert skipped == 1
    assert not (target / "link.md").exists()


def test_macos_metadata_entries_are_skipped(tmp_path: Path) -> None:
    zip_path = _write_zip(
        tmp_path,
        [("__MACOSX/._a.md", b"resource fork"), ("docs/._b.md", b"resource fork"), (".DS_Store", b"finder")],
    )
    target = _target_dir(tmp_path)

    entries, skipped = _extract(zip_path, target)

    assert entries == []
    assert skipped == 3


def test_normal_archive_maps_source_path_and_category(tmp_path: Path) -> None:
    zip_path = _write_zip(
        tmp_path,
        [("docs/guide/a.md", b"a body"), ("b.txt", b"b body"), ("./c.md", b"c body")],
    )
    target = _target_dir(tmp_path)

    entries, skipped = _extract(zip_path, target)

    by_source = {entry.source_path: entry for entry in entries}
    assert set(by_source) == {"docs/guide/a.md", "b.txt", "c.md"}
    assert by_source["docs/guide/a.md"].category == "docs/guide/"
    assert by_source["b.txt"].category == ""
    assert by_source["c.md"].category == ""
    assert by_source["docs/guide/a.md"].path.read_bytes() == b"a body"
    assert by_source["b.txt"].path.read_bytes() == b"b body"
    assert by_source["c.md"].path.read_bytes() == b"c body"
    assert skipped == 0


def test_directory_file_name_conflict_is_rejected(tmp_path: Path) -> None:
    zip_path = _write_zip(tmp_path, [("a", b"file body"), ("a/b.md", b"child body")])
    target = _target_dir(tmp_path)

    with pytest.raises(ValueError, match="冲突"):
        _extract(zip_path, target)

    assert not (target / "a" / "b.md").exists()


# ---------------------------------------------------------------------------
# 上传编排：落盘前校验 → 安全解压 → 扩展名过滤 → 逐条导入
# ---------------------------------------------------------------------------


async def test_upload_filename_must_end_with_zip() -> None:
    created: list[str] = []

    async def _create(entry: ZipImportEntry, _content: bytes):
        created.append(entry.source_path)
        return object(), False

    with pytest.raises(ValidationError):
        await import_zip_with_upload(
            file=_upload(_zip_payload([("a.md", b"a body")]), filename="bundle.tar"),
            allowed_exts={".md"},
            create_from_upload=_create,
            max_entry_size=4096,
        )

    assert created == []


async def test_upload_size_limit_is_enforced(monkeypatch) -> None:
    monkeypatch.setattr(zip_import, "_MAX_UPLOAD_SIZE", 10)
    created: list[str] = []

    async def _create(entry: ZipImportEntry, _content: bytes):
        created.append(entry.source_path)
        return object(), False

    with pytest.raises(ValidationError):
        await import_zip_with_upload(
            file=_upload(_zip_payload([("a.md", b"a body")])),
            allowed_exts={".md"},
            create_from_upload=_create,
            max_entry_size=4096,
        )

    assert created == []


async def test_non_zip_payload_is_rejected() -> None:
    created: list[str] = []

    async def _create(entry: ZipImportEntry, _content: bytes):
        created.append(entry.source_path)
        return object(), False

    with pytest.raises(ValidationError) as exc_info:
        await import_zip_with_upload(
            file=_upload(b"definitely not a zip archive"),
            allowed_exts={".md"},
            create_from_upload=_create,
            max_entry_size=4096,
        )

    assert "不是有效的 zip" in str(exc_info.value)
    assert created == []


async def test_traversal_member_in_upload_is_rejected_before_any_import() -> None:
    created: list[str] = []

    async def _create(entry: ZipImportEntry, _content: bytes):
        created.append(entry.source_path)
        return object(), False

    with pytest.raises(ValidationError):
        await import_zip_with_upload(
            file=_upload(_zip_payload([("ok.md", b"ok body"), ("../evil.md", b"evil body")])),
            allowed_exts={".md"},
            create_from_upload=_create,
            max_entry_size=4096,
        )

    assert created == []


async def test_normal_upload_filters_extensions_and_schedules_rebuilds() -> None:
    created: list[tuple[str, bytes]] = []
    scheduled: list[object] = []

    async def _create(entry: ZipImportEntry, content: bytes):
        created.append((entry.source_path, content))
        return f"object:{entry.source_path}", False

    async def _schedule(obj: object) -> None:
        scheduled.append(obj)

    payload = _zip_payload(
        [
            ("a.md", b"a body"),
            ("b.txt", b"b body"),
            ("c.exe", b"binary body"),
            ("__MACOSX/._a.md", b"metadata"),
        ]
    )
    result = await import_zip_with_upload(
        file=_upload(payload),
        allowed_exts={".md", ".txt"},
        create_from_upload=_create,
        schedule_rebuild=_schedule,
        max_entry_size=4096,
    )

    assert (result.imported, result.reused, result.skipped, result.failed) == (2, 0, 2, 0)
    assert created == [("a.md", b"a body"), ("b.txt", b"b body")]
    assert len(scheduled) == 2


async def test_reused_entry_is_counted_and_not_scheduled() -> None:
    scheduled: list[object] = []

    async def _create(entry: ZipImportEntry, _content: bytes):
        return f"object:{entry.source_path}", entry.source_path == "b.md"

    async def _schedule(obj: object) -> None:
        scheduled.append(obj)

    result = await import_zip_with_upload(
        file=_upload(_zip_payload([("a.md", b"a body"), ("b.md", b"b body")])),
        allowed_exts={".md"},
        create_from_upload=_create,
        schedule_rebuild=_schedule,
        max_entry_size=4096,
    )

    assert (result.imported, result.reused) == (1, 1)
    assert scheduled == ["object:a.md"]


async def test_file_exists_failure_is_isolated_to_that_entry() -> None:
    async def _create(entry: ZipImportEntry, _content: bytes):
        if entry.source_path == "b.md":
            raise FileExistsError(entry.source_path)
        return f"object:{entry.source_path}", False

    result = await import_zip_with_upload(
        file=_upload(_zip_payload([("a.md", b"a body"), ("b.md", b"b body")])),
        allowed_exts={".md"},
        create_from_upload=_create,
        max_entry_size=4096,
    )

    assert result.ok is True
    assert (result.imported, result.failed) == (1, 1)
    errors = {error.source_path: error.reason for error in result.errors}
    assert errors == {"b.md": "目标路径已存在"}


async def test_overlong_category_fails_before_the_entry_is_created() -> None:
    created: list[str] = []

    async def _create(entry: ZipImportEntry, _content: bytes):
        created.append(entry.source_path)
        return object(), False

    long_directory = "d" * 70
    result = await import_zip_with_upload(
        file=_upload(_zip_payload([(f"{long_directory}/a.md", b"a body")])),
        allowed_exts={".md"},
        create_from_upload=_create,
        max_entry_size=4096,
    )

    assert (result.imported, result.failed) == (0, 1)
    assert "目录层级过长" in result.errors[0].reason
    assert created == []


# ---------------------------------------------------------------------------
# 压缩方式 / 损坏的数据 / 元数据不占文件数 / 非 UTF-8 条目名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_compression_other_than_stored_and_deflate_is_rejected(tmp_path: Path, method: int) -> None:
    """bzip2 / LZMA 的解压输出是否有上限取决于解释器补丁版本，一律不放行"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=method) as archive:
        archive.writestr("ok.md", b"ok body")
    zip_path = tmp_path / "method.zip"
    zip_path.write_bytes(buffer.getvalue())
    target = _target_dir(tmp_path)

    with pytest.raises(ValueError, match="压缩方式"):
        _extract(zip_path, target)

    assert list(target.iterdir()) == []


def test_deflate_archive_is_accepted(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("docs/a.md", b"a body " * 100)
    zip_path = tmp_path / "deflate.zip"
    zip_path.write_bytes(buffer.getvalue())

    entries, skipped = _extract(zip_path, _target_dir(tmp_path))

    assert [entry.source_path for entry in entries] == ["docs/a.md"]
    assert entries[0].path.read_bytes() == b"a body " * 100
    assert skipped == 0


async def test_corrupt_deflate_data_is_a_validation_error_not_a_crash() -> None:
    """Deflate 数据损坏时标准库抛的是 zlib.error，不在 BadZipFile 之列"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.md", b"a body " * 100)
    data = bytearray(buffer.getvalue())
    local_header = data.index(b"PK\x03\x04")
    name_length = int.from_bytes(data[local_header + 26 : local_header + 28], "little")
    extra_length = int.from_bytes(data[local_header + 28 : local_header + 30], "little")
    payload_start = local_header + 30 + name_length + extra_length
    data[payload_start : payload_start + 4] = b"\xff\xff\xff\xff"  # 非法的 Deflate 块类型

    async def _create(entry: ZipImportEntry, _content: bytes):
        raise AssertionError("损坏的压缩包不应导入任何条目")

    with pytest.raises(ValidationError) as exc_info:
        await import_zip_with_upload(
            file=_upload(bytes(data)),
            allowed_exts={".md"},
            create_from_upload=_create,
            max_entry_size=4096,
        )

    assert "无法解压" in str(exc_info.value)


def test_skipped_metadata_does_not_count_towards_the_file_limit(tmp_path: Path, monkeypatch) -> None:
    """macOS 打包每个文件都多一个 `._` 条目；它们会被跳过，就不该占文件数上限"""
    monkeypatch.setattr(zip_import, "_MAX_FILES", 2)
    zip_path = _write_zip(
        tmp_path,
        [
            ("a.md", b"a body"),
            ("__MACOSX/._a.md", b"resource fork"),
            ("b.md", b"b body"),
            ("__MACOSX/._b.md", b"resource fork"),
        ],
    )

    entries, skipped = _extract(zip_path, _target_dir(tmp_path))

    assert sorted(entry.source_path for entry in entries) == ["a.md", "b.md"]
    assert skipped == 2


def _zip_with_raw_name(name_bytes: bytes, data: bytes) -> bytes:
    """写出一个条目名是**原始字节**、且不带 UTF-8 标志位的压缩包

    标准库写入时非 ASCII 名字一律按 UTF-8 编码并打上标志位，所以先用等长的 ASCII 占位名写，
    再把两个头里的名字字节替换掉。
    """
    placeholder = b"n" * len(name_bytes)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(placeholder.decode("ascii"), data)
    payload = buffer.getvalue()
    assert payload.count(placeholder) == 2  # 本地文件头一次、中央目录一次
    return payload.replace(placeholder, name_bytes)


@pytest.mark.parametrize("encoding", ["gbk", "utf-8"])
def test_names_without_the_utf8_flag_are_recovered(tmp_path: Path, encoding: str) -> None:
    name = "资料/说明.md"
    zip_path = tmp_path / "legacy.zip"
    zip_path.write_bytes(_zip_with_raw_name(name.encode(encoding), b"body"))

    entries, _skipped = _extract(zip_path, _target_dir(tmp_path))

    assert [(entry.source_path, entry.category) for entry in entries] == [(name, "资料/")]
    assert entries[0].path.read_bytes() == b"body"
