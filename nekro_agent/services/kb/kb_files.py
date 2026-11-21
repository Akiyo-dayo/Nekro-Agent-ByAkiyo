"""工作区知识库文件（`default/kb/files`、`default/kb/.normalized`）的安全读写

知识库目录在 CC 工作区里，整个工作区以读写方式挂进容器：容器里的代码能把 `kb`、`files`、
某个分类目录，乃至某个源文件换成指向宿主任意位置的符号链接。原来按路径 `resolve()` 再判
「还在不在 `files` 下面」的写法挡不住两种情况：

- 祖先目录（`kb/files` 本身）被换成链接时，基准目录跟着一起解析过去，判据形同虚设；
- 判据与真正打开之间有时间窗，判完再换链接照样跟过去。

所以这里一律从工作区根目录（NA 建的、容器换不掉的那一级）出发，逐级 `O_NOFOLLOW` 打开
（见 `services/workspace/safe_fs.py`）：

- 读：途中或末级是链接、不是普通文件，一律当作不存在（并记一条 WARNING）；
- 写：途中被换成链接或文件的目录是 NA 自己的路径，删掉链接本身重建；文件写临时文件再 rename；
- 删 / 改名：动的是目录项本身，是链接也只动链接。

`rel` 是知识库里记的相对路径（`DBKBDocument.source_path` / `normalized_text_path`），
含 `..`、绝对路径、空段时抛 `ValueError`（与原来 `resolve_kb_*_path` 的失败类型一致）。
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, List, Optional, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.workspace import safe_fs
from nekro_agent.services.workspace.manager import WorkspaceService

logger = get_sub_logger("kb_files")

SOURCE: Tuple[str, ...] = ("default", "kb", "files")
NORMALIZED: Tuple[str, ...] = ("default", "kb", ".normalized")

# 读一个知识库文件（读进内存、复制出来、流式下载）的上限。后台上传单个文件最大 50 MB
# （`routers/workspace_kb.py` 的 `KB_MAX_UPLOAD_SIZE`），这里留一点余量。文件在容器可写的
# 目录里：容器能把它撑成几十 GB 的稀疏文件，或者在我们读的时候一直往里追加，不设上限的话
# 一次读取就能吃光内存、一次复制就能写满磁盘。超过上限按「读不了」处理并记 WARNING
MAX_FILE_BYTES = 64 * 1024 * 1024


def _unreadable(workspace_id: int, rel: str, error: safe_fs.UnsafePathError, action: str) -> None:
    """把读不了的原因记清楚：太大与「是链接 / 不是普通文件」分开说"""
    if isinstance(error, safe_fs.FileTooLargeError):
        logger.warning(f"工作区 {workspace_id} 的知识库文件超过 {MAX_FILE_BYTES} 字节上限，{action}: {rel}: {error}")
    else:
        logger.warning(f"工作区 {workspace_id} 的知识库文件不是普通文件，{action}: {rel}: {error}")


def _locate(workspace_id: int, area: Tuple[str, ...], rel: str) -> Tuple[Path, List[str], str]:
    try:
        parts = safe_fs.split_rel(str(rel or ""))
    except safe_fs.UnsafePathError as e:
        raise ValueError(f"非法知识库路径: {rel}") from e
    if not parts:
        raise ValueError(f"非法知识库路径: {rel}")
    return WorkspaceService.get_workspace_dir(int(workspace_id)), [*area, *parts[:-1]], parts[-1]


def _open_parent(workspace_id: int, area: Tuple[str, ...], rel: str) -> Optional[Tuple[safe_fs.DirHandle, str]]:
    """只读地打开文件所在目录；目录不存在或途中有链接返回 None"""
    base, parents, name = _locate(workspace_id, area, rel)
    try:
        return safe_fs.open_dir(base, parents), name
    except FileNotFoundError:
        return None
    except safe_fs.UnsafePathError as e:
        logger.warning(f"工作区 {workspace_id} 知识库路径上有符号链接，按不存在处理: {rel}: {e}")
        return None


def exists(workspace_id: int, area: Tuple[str, ...], rel: str) -> bool:
    """是不是一个普通文件（链接、目录都不算）"""
    opened = _open_parent(workspace_id, area, rel)
    if opened is None:
        return False
    directory, name = opened
    with directory:
        return directory.kind(name) == "file"


def occupied(workspace_id: int, area: Tuple[str, ...], rel: str) -> bool:
    """这个路径上有没有任何条目（文件、目录、链接都算）；途中目录不存在或是链接时算没有"""
    opened = _open_parent(workspace_id, area, rel)
    if opened is None:
        return False
    directory, name = opened
    with directory:
        return directory.kind(name) is not None


def size(workspace_id: int, area: Tuple[str, ...], rel: str) -> Optional[int]:
    opened = _open_parent(workspace_id, area, rel)
    if opened is None:
        return None
    directory, name = opened
    with directory:
        st = directory.lstat(name)
        if st is None or directory.kind(name) != "file":
            return None
        return int(st.st_size)


def read_bytes(workspace_id: int, area: Tuple[str, ...], rel: str, *, max_bytes: int = MAX_FILE_BYTES) -> Optional[bytes]:
    """读一个文件；不存在、是链接、不是普通文件、超过 `max_bytes` 都返回 None"""
    opened = _open_parent(workspace_id, area, rel)
    if opened is None:
        return None
    directory, name = opened
    with directory:
        try:
            with directory.open_read(name, max_bytes=max_bytes) as handle:
                return handle.read()
        except FileNotFoundError:
            return None
        except safe_fs.UnsafePathError as e:
            _unreadable(workspace_id, rel, e, "按不存在处理")
            return None


def read_text(workspace_id: int, area: Tuple[str, ...], rel: str) -> str:
    data = read_bytes(workspace_id, area, rel)
    return "" if data is None else data.decode("utf-8", errors="replace")


def open_read(workspace_id: int, area: Tuple[str, ...], rel: str) -> BinaryIO:
    """以不跟随链接的方式打开一个文件读；不存在或不安全抛 `FileNotFoundError`（调用方负责关闭）

    返回的流读到超过 `MAX_FILE_BYTES` 时抛 `safe_fs.FileTooLargeError`（文件在读的过程中被追加）。
    """
    opened = _open_parent(workspace_id, area, rel)
    if opened is None:
        raise FileNotFoundError(rel)
    directory, name = opened
    with directory:
        try:
            return directory.open_read(name, max_bytes=MAX_FILE_BYTES)
        except safe_fs.UnsafePathError as e:
            _unreadable(workspace_id, rel, e, "按不存在处理")
            raise FileNotFoundError(rel) from e


def copy_to(workspace_id: int, area: Tuple[str, ...], rel: str, destination: Path) -> bool:
    """把一个知识库文件复制到宿主侧 NA 自己的路径（读一端不跟随链接）；不存在或不安全返回 False"""
    opened = _open_parent(workspace_id, area, rel)
    if opened is None:
        return False
    directory, name = opened
    with directory:
        try:
            directory.copy_out(name, destination, max_bytes=MAX_FILE_BYTES)
        except FileNotFoundError:
            return False
        except safe_fs.UnsafePathError as e:
            _unreadable(workspace_id, rel, e, "没有复制")
            return False
    return True


def write_bytes(workspace_id: int, area: Tuple[str, ...], rel: str, data: bytes) -> None:
    """覆盖写一个文件（临时文件 + rename）；缺的父目录建出来，被换成链接的目录删掉链接重建"""
    base, parents, name = _locate(workspace_id, area, rel)
    with safe_fs.open_dir(base, parents, create=True, replace_links=True) as directory:
        directory.write_bytes(name, data)


def write_text(workspace_id: int, area: Tuple[str, ...], rel: str, text: str) -> None:
    write_bytes(workspace_id, area, rel, text.encode("utf-8"))


def create_exclusive(workspace_id: int, area: Tuple[str, ...], rel: str, data: bytes) -> None:
    """新建一个文件；已有同名条目（含容器放的链接）抛 `FileExistsError(rel)`，不覆盖"""
    base, parents, name = _locate(workspace_id, area, rel)
    with safe_fs.open_dir(base, parents, create=True, replace_links=True) as directory:
        try:
            directory.create_exclusive(name, data)
        except FileExistsError as e:
            raise FileExistsError(rel) from e


def unlink(workspace_id: int, area: Tuple[str, ...], rel: str) -> bool:
    """删一个文件（是链接只删链接本身，是目录不动）；删了返回 True"""
    opened = _open_parent(workspace_id, area, rel)
    if opened is None:
        return False
    directory, name = opened
    with directory:
        if directory.kind(name) in ("file", "link", "other"):
            return directory.unlink(name)
        return False


def rename(workspace_id: int, area: Tuple[str, ...], old_rel: str, new_rel: str) -> bool:
    """把 `old_rel` 改名为 `new_rel`；旧文件不在（或不是普通文件）返回 False，新路径已有条目抛 `FileExistsError(new_rel)`"""
    base, new_parents, new_name = _locate(workspace_id, area, new_rel)
    opened = _open_parent(workspace_id, area, old_rel)
    if opened is None:
        return False
    old_dir, old_name = opened
    with old_dir:
        if old_dir.kind(old_name) != "file":
            return False
        with safe_fs.open_dir(base, new_parents, create=True, replace_links=True) as new_dir:
            if new_dir.kind(new_name) is not None:
                raise FileExistsError(new_rel)
            old_dir.rename_to(old_name, new_dir, new_name)
    return True


__all__ = [
    "NORMALIZED",
    "SOURCE",
    "copy_to",
    "create_exclusive",
    "exists",
    "open_read",
    "read_bytes",
    "read_text",
    "rename",
    "size",
    "unlink",
    "write_bytes",
    "write_text",
]
