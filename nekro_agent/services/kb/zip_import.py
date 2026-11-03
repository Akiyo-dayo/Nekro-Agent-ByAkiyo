"""知识库 Zip 批量导入：安全解压 + 按目录结构映射导入元数据 + 导入编排。

限制（上传大小 / 解压总大小 / 文件数）由集中配置 KB_ZIP_* 控制，配置在模块加载时读取，修改后需重启服务生效；
单个文件的上限由调用方传入，与单文件上传同一个上限。

压缩包里的一切都不可信，所以：

- 条目名：绝对路径、盘符、`..` 段、`:`、控制字符一律**整包**拒绝（不做"去掉开头的 / 再导入"这种修补）；
  落盘前再做一次 resolve 后的包含判定作为第二层；
- 条目头里的大小：先按声明值预检，落盘时再按**实际写出的字节**计数，单条或累计超限立即中止——声明值可以伪造；
- 中央目录：`ZipFile` 打开时会把整张目录解析进内存，一个上传包塞满空条目能吃掉近 1GB，
  所以打开前先看目录结束记录里声明的中央目录大小；
- 压缩方式只放行存储与 Deflate：bzip2 / LZMA 在部分解释器补丁版本上解压输出没有上限
  （几百 KB 的包就能把进程撑爆），LZMA 还会按属性字段预分配字典；
- 上传内容分块落盘、边写边计数，不整体读进内存；解压与逐条读文件都在线程里做，不卡事件循环；
  同时进行的导入数有上限；
- 符号链接条目、macOS 打包附带的 `__MACOSX/` 与 `._*` 元数据文件跳过，计入 skipped，也不占文件数上限；
- 没有 UTF-8 标志位的条目名按 UTF-8、GBK 依次重新解码（中文环境的压缩工具常写 GBK），都不成才沿用 cp437。
"""

import asyncio
import re
import stat
import zipfile
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO

from fastapi import UploadFile
from tortoise.exceptions import IntegrityError

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.kb import KBZipImportError, KBZipImportResponse

logger = get_sub_logger("kb.zip_import")

# 限制常量（模块加载时从配置读取，修改后需重启生效）
_MAX_UPLOAD_SIZE = max(1, int(config.KB_ZIP_MAX_UPLOAD_SIZE_MB)) * 1024 * 1024
_MAX_EXTRACT_SIZE = max(1, int(config.KB_ZIP_MAX_EXTRACT_SIZE_MB)) * 1024 * 1024
_MAX_FILES = max(1, int(config.KB_ZIP_MAX_FILES))
# 中央目录预算：每个文件条目留 16KB（定长头 46 字节 + 文件名 / 扩展字段 / 注释，外加它上面的目录条目）。
# 正常压缩包远低于这个量；超过它说明目录里塞满了与文件数上限无关的东西
_MAX_CENTRAL_DIRECTORY_SIZE = _MAX_FILES * 16 * 1024

_COPY_CHUNK_SIZE = 1024 * 1024  # 1 MiB：上传落盘与解压落盘的分块大小

# 与模型字段长度一致（`DBKBDocument` / `DBKBAsset` 的 category 64、source_path 512）。
# SQLite 不强制 CharField 长度，Postgres 会直接拒绝，所以在这里先挡，给出能看懂的原因
_CATEGORY_MAX_LENGTH = 64
_SOURCE_PATH_MAX_LENGTH = 512

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_ZIP_FLAG_ENCRYPTED = 0x1
_ZIP_FLAG_UTF8 = 0x800
_SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}

# 同时进行的 zip 导入上限：每个导入要占一份上传暂存、一份解压目录和线程池里的一个线程
_IMPORT_SLOTS = asyncio.Semaphore(2)


@dataclass
class ZipImportEntry:
    """解压出的单个可导入文件条目"""

    path: Path  # 磁盘绝对路径
    source_path: str  # 相对解压根目录的完整路径（正斜杠），作为知识库 source_path
    category: str  # 目录部分（带尾部斜杠），作为知识库分类；无目录时为空


def _derive_entry(rel_path: str, abs_path: Path) -> ZipImportEntry:
    parts = rel_path.split("/")
    directory_path = "/".join(parts[:-1])
    return ZipImportEntry(
        path=abs_path,
        source_path=rel_path,
        category=f"{directory_path}/" if directory_path else "",
    )


def _normalize_member_path(name: str) -> str | None:
    """校验并规范化条目名；非法条目整包拒绝（抛 ValueError），空路径返回 None。

    拒绝而不是修补：`/etc/x.md` 被"去掉开头的 /"之后照常导入，等于默许了一个
    本意就是越界的压缩包。
    """
    if not name:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ValueError(f"zip 包包含非法条目名: {name!r}")
    unified = name.replace("\\", "/")
    if unified.startswith("/") or _DRIVE_PREFIX.match(unified):
        raise ValueError(f"zip 包包含绝对路径条目: {name}")
    parts = [part for part in unified.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"zip 包包含越界路径条目: {name}")
    # `:` 在 Windows 上是盘符与 NTFS 备用数据流的分隔符
    if any(":" in part for part in parts):
        raise ValueError(f"zip 包条目名包含非法字符 ':': {name}")
    if not parts:
        return None
    return "/".join(parts)


def _member_name(member: zipfile.ZipInfo) -> str:
    """条目的原始名字（未经标准库改写反斜杠、截断 NUL 的那一份）

    没设 UTF-8 标志位时标准库按 cp437 解码；取回原始字节后依次按 UTF-8、GBK 严格解码，
    都不成再沿用 cp437 的结果。
    """
    name = member.orig_filename
    if member.flag_bits & _ZIP_FLAG_UTF8:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return name


def _safe_dest_path(target_root: Path, rel_path: str, raw_name: str) -> Path:
    """第二层：resolve 之后必须仍在 target_root 内（防 zip-slip 路径穿越）。"""
    dest = (target_root / rel_path).resolve()
    if not dest.is_relative_to(target_root):
        raise ValueError(f"zip 包包含越界路径条目: {raw_name}")
    return dest


def _is_symlink(member: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(member.external_attr >> 16)


def _is_os_metadata(rel_path: str) -> bool:
    """macOS 打包附带的资源分支与目录缓存，不是用户文件"""
    parts = rel_path.split("/")
    return parts[0] == "__MACOSX" or parts[-1].startswith("._") or parts[-1] == ".DS_Store"


def _check_central_directory_size(fp: BinaryIO) -> None:
    """打开前先看目录结束记录里声明的中央目录大小

    用的是 `ZipFile` 自己定位中央目录的同一个函数，所以这里看到的大小就是它随后要解析的大小，
    两边不会被构造出分歧。它是模块私有函数；哪天标准库拿掉了，就退回"解析完再按条目数判定"。
    """
    find_end_record = getattr(zipfile, "_EndRecData", None)
    size_index = getattr(zipfile, "_ECD_SIZE", None)
    if find_end_record is None or size_index is None:
        return
    end_record = find_end_record(fp)
    fp.seek(0)
    if not end_record:
        raise zipfile.BadZipFile("File is not a zip file")
    if int(end_record[size_index]) > _MAX_CENTRAL_DIRECTORY_SIZE:
        raise ValueError(f"zip 包条目过多（最多 {_MAX_FILES} 个文件）")


def _validate_declared_sizes(members: list[zipfile.ZipInfo], max_entry_size: int) -> None:
    """按条目头声明的大小预检；只是快速失败，真正的判据在落盘时按实际字节计数。"""
    if len(members) > _MAX_FILES:
        raise ValueError(f"zip 包文件数超出上限（最多 {_MAX_FILES} 个）")
    total_size = 0
    for member in members:
        if member.file_size > max_entry_size:
            raise ValueError(f"zip 包内文件过大: {member.filename}（单个文件最大 {max_entry_size // 1024 // 1024} MB）")
        total_size += member.file_size
        if total_size > _MAX_EXTRACT_SIZE:
            raise ValueError(f"zip 包解压后总大小超出上限（最大 {_MAX_EXTRACT_SIZE // 1024 // 1024} MB）")


def _copy_bounded(src: BinaryIO, out: BinaryIO, *, entry_limit: int, total_remaining: int, name: str) -> int:
    """按实际写出的字节计数复制，单条或累计超限立即中止"""
    written = 0
    while chunk := src.read(_COPY_CHUNK_SIZE):
        written += len(chunk)
        if written > entry_limit:
            raise ValueError(f"zip 包内文件过大: {name}（单个文件最大 {entry_limit // 1024 // 1024} MB）")
        if written > total_remaining:
            raise ValueError(f"zip 包解压后总大小超出上限（最大 {_MAX_EXTRACT_SIZE // 1024 // 1024} MB）")
        out.write(chunk)
    return written


def extract_zip_safe(zip_path: Path, target_dir: Path, *, max_entry_size: int) -> tuple[list[ZipImportEntry], int]:
    """安全解压 zip 到 target_dir，返回（可导入文件条目列表，被跳过的条目数）。

    条目映射（source_path/category）在解压阶段直接从条目名推导，避免二次文件系统遍历。
    同步函数，调用方放进线程执行。
    """
    target_root = target_dir.resolve()
    with zip_path.open("rb") as fp:
        _check_central_directory_size(fp)
        with zipfile.ZipFile(fp) as zf:
            members = [member for member in zf.infolist() if not member.is_dir()]
            # 加密条目（标志位 0x1）无法无密码解压，解压前整体拒绝并给出明确提示，
            # 避免 zf.open 抛出的 RuntimeError 冒泡成 500
            if any(member.flag_bits & _ZIP_FLAG_ENCRYPTED for member in members):
                raise ValueError("zip 包包含加密文件，暂不支持加密压缩包")

            # 条目名全部先过一遍再落盘：一个非法条目就整包拒绝，不留半截解压结果
            planned: list[tuple[zipfile.ZipInfo, str]] = []
            skipped = 0
            for member in members:
                raw_name = _member_name(member)
                rel_path = _normalize_member_path(raw_name)
                if not rel_path:
                    continue
                if _is_symlink(member) or _is_os_metadata(rel_path):
                    skipped += 1
                    continue
                planned.append((member, rel_path))

            # 只对真正要落盘的条目计数与预检：macOS 打包每个文件多一个 `._` 条目，
            # 把它们算进去会让几百个文件的包被误判超限（中央目录预算已经兜住了条目总量）
            for member, rel_path in planned:
                if member.compress_type not in _SUPPORTED_COMPRESSION:
                    raise ValueError(f"zip 包使用了不支持的压缩方式: {rel_path}（只支持存储与 Deflate）")
            _validate_declared_sizes([member for member, _rel_path in planned], max_entry_size)

            # 按条目名去重（后出现的覆盖先出现的，与解压落盘行为一致）
            entries_by_path: dict[str, ZipImportEntry] = {}
            total_written = 0
            for member, rel_path in planned:
                dest = _safe_dest_path(target_root, rel_path, rel_path)
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, dest.open("wb") as out:
                        total_written += _copy_bounded(
                            src,
                            out,
                            entry_limit=max_entry_size,
                            total_remaining=_MAX_EXTRACT_SIZE - total_written,
                            name=rel_path,
                        )
                except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, EOFError) as e:
                    # 加密预检之外的解压失败（条目损坏、Deflate 数据坏、CRC 不符等）
                    raise ValueError(f"zip 包条目无法解压: {rel_path}") from e
                except OSError as e:
                    # 目录 / 文件同名冲突（先有 a/b.txt 再出现 a，或反之）、系统不接受的文件名等
                    raise ValueError(f"zip 包条目路径冲突或非法: {rel_path}") from e
                entries_by_path[rel_path] = _derive_entry(rel_path, dest)
    return list(entries_by_path.values()), skipped


CreateFromUploadFn = Callable[[ZipImportEntry, bytes], Awaitable[tuple[object, bool]]]
ScheduleRebuildFn = Callable[[object], Awaitable[None]]


def _friendly_entry_error(error: Exception) -> str:
    """将条目级异常清洗为用户可读消息；完整细节由调用方记录日志。"""
    if isinstance(error, FileExistsError):
        return "目标路径已存在"
    if isinstance(error, IntegrityError):
        return "目标路径或内容与现有条目冲突"
    if isinstance(error, ValidationError):
        # 保留 ValidationError 的具体原因（reason），避免丢失用户可读信息
        reason = error.params.get("reason", "") if hasattr(error, "params") else ""
        return reason or str(error) or "文件内容无效"
    if isinstance(error, ValueError):
        return str(error) or "文件内容无效"
    return "导入失败（详见服务端日志）"


def _entry_metadata_error(entry: ZipImportEntry) -> str | None:
    if len(entry.category) > _CATEGORY_MAX_LENGTH:
        return f"目录层级过长：分类最多 {_CATEGORY_MAX_LENGTH} 个字符"
    if len(entry.source_path) > _SOURCE_PATH_MAX_LENGTH:
        return f"路径过长：最多 {_SOURCE_PATH_MAX_LENGTH} 个字符"
    return None


async def _save_zip_upload(file: UploadFile, target: Path) -> None:
    """分块把上传内容写到磁盘（边写边累计大小，超限立即拒绝），不整体读进内存。"""
    file_name = file.filename or ""
    if not file_name.lower().endswith(".zip"):
        raise ValidationError(reason="仅支持上传 zip 压缩包")
    total = 0
    with target.open("wb") as out:
        while chunk := await file.read(_COPY_CHUNK_SIZE):
            total += len(chunk)
            if total > _MAX_UPLOAD_SIZE:
                raise ValidationError(reason=f"文件大小超出限制（最大 {_MAX_UPLOAD_SIZE // 1024 // 1024} MB）")
            out.write(chunk)


async def _import_entries(
    entries: list[ZipImportEntry],
    allowed_exts: set[str],
    create_from_upload: CreateFromUploadFn,
    schedule_rebuild: ScheduleRebuildFn | None,
    result: KBZipImportResponse,
) -> None:
    """按扩展名过滤并逐条导入，单条失败不中断整批。"""
    allowed_exts_lower = {ext.lower() for ext in allowed_exts}
    filtered = [entry for entry in entries if Path(entry.source_path).suffix.lower() in allowed_exts_lower]
    result.skipped += len(entries) - len(filtered)

    for entry in filtered:
        metadata_error = _entry_metadata_error(entry)
        if metadata_error is not None:
            result.failed += 1
            result.errors.append(KBZipImportError(source_path=entry.source_path, reason=metadata_error))
            continue
        try:
            content = await asyncio.to_thread(entry.path.read_bytes)
            obj, reused_existing = await create_from_upload(entry, content)
            if not reused_existing and schedule_rebuild is not None:
                await schedule_rebuild(obj)
            if reused_existing:
                result.reused += 1
            else:
                result.imported += 1
        except Exception as e:
            logger.warning(f"zip 导入条目失败: {entry.source_path}, error: {e}", exc_info=True)
            result.failed += 1
            result.errors.append(KBZipImportError(source_path=entry.source_path, reason=_friendly_entry_error(e)))


async def import_zip_with_upload(
    file: UploadFile,
    allowed_exts: set[str],
    create_from_upload: CreateFromUploadFn,
    schedule_rebuild: ScheduleRebuildFn | None = None,
    *,
    max_entry_size: int,
) -> KBZipImportResponse:
    """zip 上传导入编排：落盘校验 → 安全解压 → 扩展名过滤 → 逐条导入并统计。

    create_from_upload 接收 (entry, content_bytes) 返回 (obj, reused_existing)；
    reused 时不触发重建索引。单条导入失败不中断整批，完整异常细节记录日志，
    客户端仅收到清洗后的消息。压缩包级别的问题（非法条目、超限、损坏）整包拒绝，一条都不导入。
    """
    result = KBZipImportResponse(
        ok=False,
        imported=0,
        reused=0,
        skipped=0,
        failed=0,
        errors=[],
    )
    async with _IMPORT_SLOTS:
        with TemporaryDirectory(prefix="kb-zip-") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            zip_path = tmp_dir / "upload.zip"
            extract_dir = tmp_dir / "extracted"
            extract_dir.mkdir()
            await _save_zip_upload(file, zip_path)
            try:
                entries, skipped = await asyncio.to_thread(
                    extract_zip_safe, zip_path, extract_dir, max_entry_size=max_entry_size
                )
            except (zipfile.BadZipFile, zipfile.LargeZipFile) as e:
                raise ValidationError(reason="上传的文件不是有效的 zip 压缩包或已损坏") from e
            except ValueError as e:
                raise ValidationError(reason=str(e) or "zip 包无法解析") from e
            result.skipped = skipped
            await _import_entries(
                entries=entries,
                allowed_exts=allowed_exts,
                create_from_upload=create_from_upload,
                schedule_rebuild=schedule_rebuild,
                result=result,
            )
    result.ok = result.imported + result.reused > 0 or result.failed == 0
    return result
