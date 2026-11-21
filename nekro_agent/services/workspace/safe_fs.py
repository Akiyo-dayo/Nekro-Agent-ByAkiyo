"""宿主侧读写「容器可写目录」的安全函数

## 为什么要有它

CC 工作区目录整个以读写方式挂进容器（`/workspace`），挂成 `~/.claude` 的 CC home 还是 0777；沙盒的
共享目录同样挂进沙盒容器。容器里的代码能在这些目录里能写的地方放符号链接，或者把子目录
换成符号链接。NA 宿主侧（Docker 部署里通常是 root）一旦按路径去读、写、删、复制、改权限，
就会跟着链接走到宿主的任意位置：读平台方 Key 所在的配置、覆盖任意文件、删任意目录。

所以凡是宿主侧碰容器可写目录的地方，都走这里：

- 从一个**信得过的根目录**出发（NA 自己建的、容器动不了的那一级，例如工作区根目录、
  沙盒共享目录本身），往下每一级都用 `O_NOFOLLOW` 按目录句柄打开，途中任何一级是
  符号链接就拒绝（或按调用方要求删掉链接重建目录）；
- 写文件：在目录句柄上 `O_CREAT|O_EXCL|O_NOFOLLOW` 建临时文件，写完 `rename` 过去——
  `rename` 替换的是目录项本身，目标是链接也只是把链接换掉；
- 读文件：`O_NOFOLLOW|O_NONBLOCK` 打开后 `fstat` 确认是普通文件（不是 FIFO、设备），
  并且有长度上限；流式读的时候每读一块再数一次（文件可能在读的过程中被追加，
  稀疏文件撑大也不花容器什么成本），超了抛 `FileTooLargeError`；
- 删目录：不靠递归，把子目录里的条目逐个改名上提一层再 `rmdir`（嵌套多深都删得掉，只占两把
  句柄），遇到链接只删链接本身，不进去；读与复制目录树（`copy_tree_out`、`iter_files`）有深度
  上限（`MAX_TREE_DEPTH`），超了抛 `TreeTooDeepError`，不会撞上 Python 的递归上限；
- 判类型在真正打开之前（Linux）：先 `O_PATH|O_NOFOLLOW` 拿到条目本身、`fstat`，确认是普通
  文件才经 `/proc/self/fd/N` 重新打开，FIFO、设备文件从不被真正打开；
- 改权限：先 `O_NOFOLLOW` 打开再 `fchmod`，不按路径 `chmod`；
- 改属主：按目录句柄 `fchownat(..., AT_SYMLINK_NOFOLLOW)`，是链接只改链接本身；整棵改时链接不进去。

CC home 里的 `settings.json`、`nk-ssh` 等由 CC 镜像相关代码写入的文件，也用这里的函数。

## 不支持目录句柄的平台

Windows 开发机上 `os.open` 不支持 `dir_fd`。退路是逐级 `lstat` 检查链接再按路径操作：
有竞态，但只用在开发与测试环境，生产部署（Linux）一定走目录句柄那条路。
"""

from __future__ import annotations

import contextlib
import errno
import io
import os
import stat
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator, List, Optional, Sequence, Tuple, Union

RelPath = Union[str, Sequence[str]]

DIR_FD_SUPPORTED = (
    os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

# 「先判类型、再真正打开」：`O_PATH` 只拿到条目本身（是链接就是链接本身），不触发设备驱动的
# open，也不会卡在 FIFO 上；`fstat` 确认是普通文件之后，再经 `/proc/self/fd/N` 以只读重新打开
# 同一个 inode。两样都有（Linux）才走这条路，否则退回「`O_NONBLOCK` 打开再判类型」。
_O_PATH = getattr(os, "O_PATH", 0)
PATH_FD_SUPPORTED = bool(_O_PATH) and os.path.isdir("/proc/self/fd")

# `remove_tree` 处理的条目总数上限：容器一边删一边往里塞时，不至于一直转下去
MAX_REMOVE_ENTRIES = 1_000_000

# 读文件的默认上限：这里读的都是配置、技能、说明一类的小文件
DEFAULT_MAX_READ_BYTES = 8 * 1024 * 1024

# 递归走目录树（删、复制出来、列文件、找链接）的最大深度。真实的工作区目录树（依赖目录、
# 虚拟环境）几十层封顶；更深的多半是容器故意造的。不设上限的话，递归会撞上 Python 的
# 递归上限抛 RecursionError，而且每一层都占着一个目录句柄
MAX_TREE_DEPTH = 128

# 改属主（`DirHandle.chown`、`chown_tree`）只在 POSIX 上有；Windows 开发机上调用方什么都不做
CHOWN_SUPPORTED = os.name == "posix" and hasattr(os, "chown")

# `chown_tree` 处理的条目总数上限：它用在 NA 自己写进去的东西上（从技能库整棵复制进来的技能
# 目录），几百个条目封顶；超过的多半是容器往里塞的
MAX_CHOWN_ENTRIES = 100_000


class UnsafePathError(OSError):
    """路径上有符号链接或不是预期类型的条目，按拒绝处理"""


class FileTooLargeError(UnsafePathError):
    """文件超过调用方给的大小上限（包括打开时没超、读的过程中才长大的文件）

    是 `UnsafePathError` 的子类：原来按「不安全」接的调用方照样接得住，想单独提示
    「太大」的调用方可以先接它。
    """


class TreeTooDeepError(UnsafePathError):
    """目录树嵌套超过深度上限（见 `MAX_TREE_DEPTH`）"""


def split_rel(rel: RelPath) -> List[str]:
    """把相对路径拆成目录项名；拒绝空项、`.`、`..`、绝对路径与 NUL"""
    if isinstance(rel, str):
        text = rel.replace("\\", "/")
        if text.startswith("/"):
            raise UnsafePathError(f"不接受绝对路径: {rel!r}")
        parts = [part for part in PurePosixPath(text).parts if part not in ("", ".")]
    else:
        parts = [str(part) for part in rel]
    for part in parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part or "\x00" in part:
            raise UnsafePathError(f"路径里有不允许的一级: {part!r}")
    return parts


def _is_link_error(exc: OSError) -> bool:
    return exc.errno in (errno.ELOOP, errno.ENOTDIR, getattr(errno, "EMLINK", -1))


def _pump(source: BinaryIO, sink: BinaryIO, max_bytes: Optional[int], name: str) -> int:
    written = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            return written
        written += len(chunk)
        if max_bytes is not None and written > max_bytes:
            raise FileTooLargeError(f"{name} 超过 {max_bytes} 字节")
        sink.write(chunk)


class _BoundedReader(io.RawIOBase):
    """读到的总字节数超过上限就抛 `FileTooLargeError` 的读流

    `open_read` 打开时按 `fstat` 判过一次大小，但容器在我们读的过程中还能往文件里追加，
    打开时 1 KB、读的时候几十 GB（稀疏文件尤其便宜）。调用方往往 `read()` 一口气读完，
    或者流式复制到磁盘，只靠打开时那一次判断兜不住内存和磁盘，所以读的每一步都再数一次。
    """

    def __init__(self, raw: BinaryIO, limit: int, name: str) -> None:
        super().__init__()
        self._raw = raw
        self._limit = limit
        self._name = name
        self._consumed = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        count = self._raw.readinto(buffer)  # type: ignore[attr-defined]
        if count:
            self._consumed += count
            if self._consumed > self._limit:
                raise FileTooLargeError(f"{self._name} 超过 {self._limit} 字节")
        return count or 0

    def fileno(self) -> int:
        return self._raw.fileno()

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


def bounded_reader(handle: BinaryIO, max_bytes: Optional[int], name: str) -> BinaryIO:
    """给读流套上「读超了就报错」；`max_bytes` 为 None 时原样返回"""
    if max_bytes is None:
        return handle
    return _BoundedReader(handle, max_bytes, name)  # type: ignore[return-value]


def _inherit_owner(fd: int, previous: Optional[os.stat_result]) -> None:
    """新文件沿用被替换的那个文件的属主；只在以 root 运行时做（别的身份改不了属主）"""
    if previous is None or not hasattr(os, "fchown") or not hasattr(os, "geteuid"):
        return
    if os.geteuid() != 0:
        return
    if (previous.st_uid, previous.st_gid) == (0, 0):
        return
    os.fchown(fd, previous.st_uid, previous.st_gid)


class DirHandle:
    """一个按目录句柄打开的目录；所有操作都相对它，不跟随符号链接

    用 `open_dir` 取得，用完 `close()`（或 `with`）。不支持目录句柄的平台上内部持有路径。
    """

    def __init__(self, fd: Optional[int], path: Path) -> None:
        self._fd = fd
        self.path = path

    # ---- 生命周期 ----

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def __enter__(self) -> "DirHandle":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- 查询 ----

    def kind(self, name: str) -> Optional[str]:
        """`file` / `dir` / `link` / `other`；不存在返回 None。不跟随链接"""
        split_rel([name])
        try:
            if self._fd is not None:
                st = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            else:
                st = os.lstat(self.path / name)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return "link"
        if stat.S_ISDIR(st.st_mode):
            return "dir"
        if stat.S_ISREG(st.st_mode):
            return "file"
        return "other"

    def entries(self) -> List[Tuple[str, str]]:
        """目录里的条目与种类（不跟随链接）"""
        result: List[Tuple[str, str]] = []
        if self._fd is not None:
            with os.scandir(self._fd) as iterator:
                for entry in iterator:
                    result.append((entry.name, self._entry_kind(entry)))
        else:
            for child in self.path.iterdir():
                result.append((child.name, self.kind(child.name) or "other"))
        return sorted(result)

    def iter_entries(self) -> Iterator[Tuple[str, str]]:
        """逐个给出目录里的条目与种类（不跟随链接）；不排序、不先整份读进来

        给删除与回收用：容器能往目录里塞几百万个条目，`entries()` 那样先整份读进内存再排序，
        数到上限之前就已经花掉了时间和内存。
        """
        if self._fd is not None:
            with os.scandir(self._fd) as iterator:
                for entry in iterator:
                    yield entry.name, self._entry_kind(entry)
        else:
            for child in self.path.iterdir():
                yield child.name, self.kind(child.name) or "other"

    @staticmethod
    def _entry_kind(entry: os.DirEntry) -> str:
        if entry.is_symlink():
            return "link"
        if entry.is_dir(follow_symlinks=False):
            return "dir"
        if entry.is_file(follow_symlinks=False):
            return "file"
        return "other"

    # ---- 子目录 ----

    def subdir(self, name: str, *, create: bool = False, mode: int = 0o755, replace_links: bool = False) -> "DirHandle":
        """按名字打开子目录；`create` 时不存在就建，`replace_links` 时它是链接或文件就删掉重建"""
        split_rel([name])
        if self._fd is None:
            target = self.path / name
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                if not replace_links:
                    raise UnsafePathError(f"{name} 不是普通目录")
                target.unlink()
            if create:
                target.mkdir(mode=mode, exist_ok=True)
            if not target.is_dir():
                raise FileNotFoundError(str(target))
            return DirHandle(None, target)
        if create:
            try:
                os.mkdir(name, mode, dir_fd=self._fd)
            except FileExistsError:
                pass
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        try:
            fd = os.open(name, flags, dir_fd=self._fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            if not _is_link_error(exc) or not replace_links:
                raise UnsafePathError(f"{name} 不是普通目录") from exc
            os.unlink(name, dir_fd=self._fd)
            os.mkdir(name, mode, dir_fd=self._fd)
            fd = os.open(name, flags, dir_fd=self._fd)
        return DirHandle(fd, self.path / name)

    def mkdir(self, name: str, mode: int = 0o755) -> None:
        """建子目录；已存在且是普通目录不算错，是链接或文件就拒绝"""
        self.subdir(name, create=True, mode=mode).close()

    # ---- 文件 ----

    def lstat(self, name: str) -> Optional[os.stat_result]:
        """不跟随链接地取一个条目的元数据；不存在返回 None"""
        split_rel([name])
        try:
            if self._fd is not None:
                return os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            return os.lstat(self.path / name)
        except FileNotFoundError:
            return None

    def entries_with_stat(self) -> List[Tuple[str, str, os.stat_result]]:
        """目录里的条目、种类与 `lstat` 结果（不跟随链接），用于列目录给界面看"""
        result: List[Tuple[str, str, os.stat_result]] = []
        for name, entry_kind in self.entries():
            st = self.lstat(name)
            if st is not None:
                result.append((name, entry_kind, st))
        return result

    def write_bytes(self, name: str, data: bytes, *, mode: Optional[int] = None) -> None:
        """写临时文件再 `rename`：读的一方要么看到旧文件、要么看到新文件；目标是链接也只换掉链接

        `mode` 不给时沿用原文件的权限位（没有原文件就是 0644）。原文件存在时新文件沿用它的
        属主（以 root 运行时）：这样与「按路径原地覆盖」的效果一致——容器原来能改的文件，
        宿主侧重写之后它照样能改，不会因为换成了临时文件 + rename 就变成 root 所有。
        """
        self.write_stream(name, io.BytesIO(data), mode=mode)

    def write_stream(
        self,
        name: str,
        source: BinaryIO,
        *,
        mode: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> int:
        """把 `source` 流式写成 `name`（临时文件 + rename，规则同 `write_bytes`），返回字节数

        目标是目录时抛 `UnsafePathError`（rename 盖不了目录，各平台报的错不一样，这里统一）。
        """
        split_rel([name])
        temporary = f".{name}.nk-tmp"
        previous = self.lstat(name)
        if previous is not None and stat.S_ISDIR(previous.st_mode):
            raise UnsafePathError(f"{name} 是目录，不能当文件写")
        if previous is not None and not stat.S_ISREG(previous.st_mode):
            previous = None
        effective_mode = mode if mode is not None else (stat.S_IMODE(previous.st_mode) if previous else 0o644)
        if self._fd is None:
            target = self.path / name
            tmp_path = self.path / temporary
            if tmp_path.is_symlink() or tmp_path.exists():
                tmp_path.unlink()
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, effective_mode)
            try:
                with os.fdopen(fd, "wb") as handle:
                    written = _pump(source, handle, max_bytes, name)
                os.replace(tmp_path, target)
            except BaseException:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                raise
            return written
        try:
            os.unlink(temporary, dir_fd=self._fd)
        except FileNotFoundError:
            pass
        except (IsADirectoryError, PermissionError):
            # Linux 上 unlink 目录报 EISDIR，有的平台报 EPERM：都当成容器放进来的目录
            self.remove_tree(temporary)
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
            effective_mode,
            dir_fd=self._fd,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                written = _pump(source, handle, max_bytes, name)
                handle.flush()
                os.fchmod(handle.fileno(), effective_mode)
                _inherit_owner(handle.fileno(), previous)
            os.replace(temporary, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=self._fd)
            except OSError:
                pass
            raise
        return written

    def write_text(self, name: str, text: str, *, mode: Optional[int] = None, encoding: str = "utf-8") -> None:
        self.write_bytes(name, text.encode(encoding), mode=mode)

    def create_exclusive(self, name: str, data: bytes, *, mode: int = 0o644) -> None:
        """新建一个文件并写入；已有同名条目（含链接）就抛 `FileExistsError`，不覆盖"""
        split_rel([name])
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY
        if self._fd is not None:
            fd = os.open(name, flags, mode, dir_fd=self._fd)
        else:
            fd = os.open(self.path / name, flags, mode)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except BaseException:
            self.unlink(name)
            raise

    def rename_to(self, name: str, target_dir: "DirHandle", new_name: str, *, overwrite: bool = False) -> None:
        """把本目录的一个条目改名到 `target_dir` 下；动的是目录项本身，是链接也只挪链接"""
        split_rel([name])
        split_rel([new_name])
        if not overwrite and target_dir.kind(new_name) is not None:
            raise FileExistsError(new_name)
        if self._fd is not None and target_dir._fd is not None:  # noqa: SLF001 - 同一个类
            os.rename(name, new_name, src_dir_fd=self._fd, dst_dir_fd=target_dir._fd)  # noqa: SLF001
            return
        os.replace(self.path / name, target_dir.path / new_name)

    def open_read(self, name: str, *, max_bytes: Optional[int] = None) -> BinaryIO:
        """以 `O_NOFOLLOW` 打开一个普通文件读；是链接 / 目录 / FIFO / 设备就拒绝，不存在抛 `FileNotFoundError`

        返回的文件对象由调用方关闭。用于大文件的流式读取与复制（不一次读进内存）。

        给了 `max_bytes` 时，打开时按大小判一次，返回的流在读的过程中再数一次：文件在读的时候
        被追加到超过上限，读到超过的那一刻抛 `FileTooLargeError`（不会一路读到内存或磁盘撑满）。
        """
        split_rel([name])
        if self._fd is None:
            target = self.path / name
            if target.is_symlink():
                raise UnsafePathError(f"{name} 是符号链接")
            if not target.exists():
                raise FileNotFoundError(str(target))
            if not target.is_file():
                raise UnsafePathError(f"{name} 不是普通文件")
            if max_bytes is not None and target.stat().st_size > max_bytes:
                raise FileTooLargeError(f"{name} 超过 {max_bytes} 字节")
            return bounded_reader(open(target, "rb"), max_bytes, name)  # noqa: SIM115 - 交给调用方关闭
        fd, st = self._open_regular_fd(name)
        try:
            if max_bytes is not None and st.st_size > max_bytes:
                raise FileTooLargeError(f"{name} 超过 {max_bytes} 字节")
        except BaseException:
            os.close(fd)
            raise
        return bounded_reader(os.fdopen(fd, "rb"), max_bytes, name)

    def _open_regular_fd(self, name: str) -> Tuple[int, os.stat_result]:
        """以只读打开一个普通文件，返回 `(fd, fstat 结果)`；是链接 / 目录 / FIFO / 设备就拒绝

        支持 `O_PATH` 时（见 `PATH_FD_SUPPORTED`）对不是普通文件的条目从不做真正的打开：先
        `O_PATH|O_NOFOLLOW` 拿到条目本身，`fstat` 确认是普通文件，再经 `/proc/self/fd/N` 以只读
        重新打开，并核对重新打开的是同一个 inode。不支持时退回 `O_NONBLOCK|O_NOFOLLOW` 直接
        打开再判类型（FIFO 不会卡住，但设备文件的 open 已经发生了）。

        真正的打开一律带 `O_NONBLOCK`、打开之后再改回阻塞：文件的属主（容器里的 appuser）能对
        自己的文件拿写租约（`fcntl(F_SETLEASE, F_WRLCK)`），阻塞的 open 会在 `break_lease` 上等到
        租约交出或超时（默认 45 秒），而且对方可以反复来；调用方多半在事件循环里。非阻塞的 open
        碰上租约立刻返回 `EWOULDBLOCK`，这里转成 `UnsafePathError`（「被别的进程占着」）。

        Raises:
            FileNotFoundError: 不存在
            UnsafePathError: 是链接、不是普通文件、被租约占着，或在打开过程中被换掉
        """
        assert self._fd is not None
        if PATH_FD_SUPPORTED:
            path_fd = os.open(name, _O_PATH | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=self._fd)
            try:
                first = os.fstat(path_fd)
                if stat.S_ISLNK(first.st_mode):
                    raise UnsafePathError(f"{name} 是符号链接")
                if not stat.S_ISREG(first.st_mode):
                    raise UnsafePathError(f"{name} 不是普通文件")
                try:
                    fd = os.open(f"/proc/self/fd/{path_fd}", os.O_RDONLY | _O_CLOEXEC | _O_NONBLOCK)
                except BlockingIOError as exc:
                    raise UnsafePathError(f"{name} 被别的进程占着（文件租约），不等它") from exc
            finally:
                os.close(path_fd)
            try:
                st = os.fstat(fd)
                if (st.st_dev, st.st_ino) != (first.st_dev, first.st_ino) or not stat.S_ISREG(st.st_mode):
                    raise UnsafePathError(f"{name} 在打开的过程中被换掉了")
                os.set_blocking(fd, True)
            except BaseException:
                os.close(fd)
                raise
            return fd, st
        try:
            fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=self._fd)
        except FileNotFoundError:
            raise
        except BlockingIOError as exc:
            raise UnsafePathError(f"{name} 被别的进程占着（文件租约），不等它") from exc
        except OSError as exc:
            if _is_link_error(exc):
                raise UnsafePathError(f"{name} 是符号链接") from exc
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePathError(f"{name} 不是普通文件")
            # 普通文件读起来不会阻塞，把非阻塞去掉，免得个别文件系统上读到 EAGAIN
            os.set_blocking(fd, True)
        except BaseException:
            os.close(fd)
            raise
        return fd, st

    def copy_out(self, name: str, destination: Path, *, max_bytes: Optional[int] = None) -> int:
        """把容器可写目录里的一个普通文件复制到宿主侧**信得过**的 `destination`，返回字节数

        读的一端不跟随链接；写的一端是调用方给的 NA 私有路径（先写临时文件再 rename）。
        """
        destination = Path(destination)
        temporary = destination.with_name(f".{destination.name}.nk-tmp")
        copied = 0
        with self.open_read(name, max_bytes=max_bytes) as source:
            try:
                with open(temporary, "wb") as sink:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if max_bytes is not None and copied > max_bytes:
                            raise FileTooLargeError(f"{name} 超过 {max_bytes} 字节")
                        sink.write(chunk)
                os.replace(temporary, destination)
            except BaseException:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
        return copied

    def copy_tree_out(
        self,
        name: str,
        destination: Path,
        *,
        max_files: int = 2000,
        max_bytes: int = 64 * 1024 * 1024,
        max_depth: int = MAX_TREE_DEPTH,
    ) -> None:
        """把容器可写目录里的一棵目录树复制到宿主侧**信得过**的 `destination`

        链接与特殊文件一律跳过（不把宿主别处的内容带出来）；文件数、总字节数与嵌套深度都有上限，
        超了分别抛 `UnsafePathError`、`FileTooLargeError`、`TreeTooDeepError`。
        `destination` 必须还不存在。
        """
        destination = Path(destination)
        destination.mkdir(parents=False, exist_ok=False)
        budget = {"files": max_files, "bytes": max_bytes}

        def _walk(directory: DirHandle, target: Path, remaining_depth: int) -> None:
            for child_name, child_kind in directory.entries():
                if child_kind == "file":
                    budget["files"] -= 1
                    if budget["files"] < 0:
                        raise UnsafePathError(f"文件数超过 {max_files}")
                    copied = directory.copy_out(child_name, target / child_name, max_bytes=budget["bytes"])
                    budget["bytes"] -= copied
                elif child_kind == "dir":
                    if remaining_depth <= 0:
                        raise TreeTooDeepError(f"{name} 下的目录嵌套超过 {max_depth} 层")
                    (target / child_name).mkdir()
                    with directory.subdir(child_name) as child:
                        _walk(child, target / child_name, remaining_depth - 1)

        with self.subdir(name) as root:
            _walk(root, destination, max_depth)

    def read_bytes(self, name: str, *, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> Optional[bytes]:
        """读普通文件；不存在返回 None，是链接 / 目录 / FIFO / 设备就拒绝，超长就拒绝"""
        split_rel([name])
        if self._fd is None:
            target = self.path / name
            if target.is_symlink():
                raise UnsafePathError(f"{name} 是符号链接")
            if not target.exists():
                return None
            if not target.is_file():
                raise UnsafePathError(f"{name} 不是普通文件")
            if target.stat().st_size > max_bytes:
                raise FileTooLargeError(f"{name} 超过 {max_bytes} 字节")
            with bounded_reader(open(target, "rb"), max_bytes, name) as handle:
                return handle.read()
        try:
            fd, st = self._open_regular_fd(name)
        except FileNotFoundError:
            return None
        try:
            if st.st_size > max_bytes:
                raise FileTooLargeError(f"{name} 超过 {max_bytes} 字节")
            chunks: List[bytes] = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise FileTooLargeError(f"{name} 超过 {max_bytes} 字节")
            return data
        finally:
            os.close(fd)

    def read_text(
        self,
        name: str,
        *,
        max_bytes: int = DEFAULT_MAX_READ_BYTES,
        encoding: str = "utf-8",
        errors: str = "strict",
    ) -> Optional[str]:
        data = self.read_bytes(name, max_bytes=max_bytes)
        return None if data is None else data.decode(encoding, errors)

    def chmod(self, name: str, mode: int) -> None:
        """不跟随链接地改权限：只改普通文件与目录，是链接 / FIFO / 设备就拒绝

        支持 `O_PATH` 时先拿条目本身、`fstat` 判类型，再经 `/proc/self/fd/N` 改（不对设备文件做
        真正的 open）；不支持时先 `O_NOFOLLOW|O_NONBLOCK` 打开再 `fchmod`。
        """
        split_rel([name])
        if self._fd is None:
            target = self.path / name
            if target.is_symlink():
                raise UnsafePathError(f"{name} 是符号链接")
            os.chmod(target, mode)
            return
        if PATH_FD_SUPPORTED:
            path_fd = os.open(name, _O_PATH | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=self._fd)
            try:
                st = os.fstat(path_fd)
                if stat.S_ISLNK(st.st_mode):
                    raise UnsafePathError(f"{name} 是符号链接")
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                    raise UnsafePathError(f"{name} 不是普通文件或目录")
                os.chmod(f"/proc/self/fd/{path_fd}", mode)
            finally:
                os.close(path_fd)
            return
        try:
            fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=self._fd)
        except OSError as exc:
            if _is_link_error(exc):
                raise UnsafePathError(f"{name} 是符号链接") from exc
            raise
        try:
            os.fchmod(fd, mode)
        finally:
            os.close(fd)

    def chown(self, name: str, uid: int, gid: int) -> None:
        """不跟随链接地改一个条目的属主：是链接只改链接本身（`AT_SYMLINK_NOFOLLOW`），不碰链接那头

        只在 POSIX 上可用（`CHOWN_SUPPORTED`）。条目不在了抛 `FileNotFoundError`。
        """
        split_rel([name])
        if self._fd is not None:
            os.chown(name, uid, gid, dir_fd=self._fd, follow_symlinks=False)
            return
        os.lchown(self.path / name, uid, gid)

    def chown_tree(self, name: str, uid: int, gid: int, *, max_entries: Optional[int] = None) -> int:
        """改一个条目的属主；是目录就连同里面所有条目一起。返回改了几个

        链接一律不改、不进去（它不是 NA 写的）；子目录逐级 `O_NOFOLLOW` 打开，看过之后被换成链接的
        跳过。嵌套超过 `MAX_TREE_DEPTH` 抛 `TreeTooDeepError`，条目超过 `max_entries`（默认
        `MAX_CHOWN_ENTRIES`）抛 `UnsafePathError`；已经改过的不恢复。
        """
        kind = self.kind(name)
        if kind is None or kind == "link":
            return 0
        limit = MAX_CHOWN_ENTRIES if max_entries is None else max_entries
        self.chown(name, uid, gid)
        count = 1
        if kind != "dir":
            return count

        def walk(directory: "DirHandle", depth: int) -> None:
            nonlocal count
            if depth > MAX_TREE_DEPTH:
                raise TreeTooDeepError(f"{name} 嵌套超过 {MAX_TREE_DEPTH} 层")
            for child, child_kind in directory.iter_entries():
                if child_kind == "link":
                    continue
                count += 1
                if count > limit:
                    raise UnsafePathError(f"{name} 里的条目超过 {limit} 个，属主没有改完")
                with contextlib.suppress(FileNotFoundError):
                    directory.chown(child, uid, gid)
                if child_kind != "dir":
                    continue
                try:
                    sub = directory.subdir(child)
                except (FileNotFoundError, UnsafePathError):
                    continue  # 列完之后被删掉或换成了链接
                with sub:
                    walk(sub, depth + 1)

        with self.subdir(name) as top:
            walk(top, 1)
        return count

    def unlink(self, name: str) -> bool:
        """删一个非目录条目（链接只删链接本身）；不存在返回 False，是目录抛 `UnsafePathError`

        是目录时各平台报的错不一样（Linux 的 `IsADirectoryError`、别处的 `PermissionError`），
        统一成 `UnsafePathError`，调用方只接一种。
        """
        split_rel([name])
        if self.kind(name) == "dir":
            raise UnsafePathError(f"{name} 是目录，不按文件删")
        try:
            if self._fd is not None:
                os.unlink(name, dir_fd=self._fd)
            else:
                (self.path / name).unlink()
        except FileNotFoundError:
            return False
        return True

    def rmdir(self, name: str) -> bool:
        """删一个**空**子目录；是链接、不是目录或不空都不删，返回 False"""
        if self.kind(name) != "dir":
            return False
        try:
            if self._fd is not None:
                os.rmdir(name, dir_fd=self._fd)
            else:
                (self.path / name).rmdir()
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOTDIR):
                return False
            raise
        return True

    def remove_tree(self, name: str, *, max_entries: Optional[int] = None) -> bool:
        """删除一个条目（目录就整棵删）；遇到链接只删链接本身，绝不进到链接指向的地方

        不靠递归，嵌套多深都删得掉：把要删的目录里的子目录逐个「压平」——打开子目录，把它里面
        的条目一个个改名到上一层（`renameat`，基于目录句柄，动的是目录项本身、不跟随末级；新名字
        一律是唯一的 `.nk-rm-<uuid>`，绝不覆盖已有条目），子目录空了就 `rmdir`，直到这一层空了。
        任何时候只占两把目录句柄。

        处理的条目总数超过 `max_entries`（默认 `MAX_REMOVE_ENTRIES`）抛 `UnsafePathError`：容器
        一边删一边往里塞时不至于一直转下去。目录是边列边数的（`iter_entries`，不先整份读进来、
        不排序），到上限当场停。已经删掉的部分不恢复；在启动路径上要用 `remove_tree_or_discard`，
        不让它卡住启动。
        """
        kind = self.kind(name)
        if kind is None:
            return False
        if kind != "dir":
            return self.unlink(name)
        if max_entries is None:
            max_entries = MAX_REMOVE_ENTRIES
        budget = max_entries
        with self.subdir(name) as top:
            while True:
                seen_any = False
                for child_name, child_kind in top.iter_entries():
                    seen_any = True
                    budget -= 1
                    if budget < 0:
                        raise UnsafePathError(f"{name} 里的条目超过 {max_entries} 个，没有删完")
                    if child_kind != "dir":
                        with contextlib.suppress(UnsafePathError, FileNotFoundError):
                            # 列完之后被换成了目录 / 已经没了：下一轮按它那时的样子处理
                            top.unlink(child_name)
                        continue
                    try:
                        child = top.subdir(child_name)
                    except (FileNotFoundError, UnsafePathError):
                        # 列完之后被删掉、或被换成了链接 / 文件：下一轮按它那时的样子处理
                        continue
                    with child:
                        for grand_name, _grand_kind in child.iter_entries():
                            budget -= 1
                            if budget < 0:
                                raise UnsafePathError(f"{name} 里的条目超过 {max_entries} 个，没有删完")
                            child.rename_to(grand_name, top, f".nk-rm-{uuid.uuid4().hex}")
                    top.rmdir(child_name)  # 期间又被塞了东西就删不掉，下一轮再来
                if not seen_any:
                    break
        if self._fd is not None:
            os.rmdir(name, dir_fd=self._fd)
        else:
            (self.path / name).rmdir()
        return True

    def remove_tree_or_discard(self, name: str, trash_dir: "DirHandle") -> str:
        """删除一个条目；删不完（条目太多）就把剩下的整体改名进 `trash_dir`，返回怎么处理的

        给启动路径用（同步技能、初始化工作区）：容器在自己的目录里造的垃圾删不完，不该让整个
        启动失败。改名到 `trash_dir` 下的 `.nk-trash-<uuid>`（基于目录句柄，不跟随末级），
        下一次初始化时再清（`purge_trash`）。`trash_dir` 必须是 NA 自己的、容器写不到的目录
        （工作区之外）：回收目录里的东西一律是 NA 放进去的，清的时候不用分辨哪些是容器起的名。

        Returns:
            `"removed"` 删干净了；`"absent"` 本来就不在；`"discarded"` 挪进了回收位置
        """
        try:
            return "removed" if self.remove_tree(name) else "absent"
        except UnsafePathError:
            if self.kind(name) is None:
                return "removed"
            self.rename_to(name, trash_dir, f".nk-trash-{uuid.uuid4().hex}")
            return "discarded"

    def purge_trash(self, *, max_entries: Optional[int] = None) -> int:
        """清空这个回收目录（`remove_tree_or_discard` 的 `trash_dir`）；返回清掉了几项

        只在 NA 自己的回收目录上调：这里的条目都是 NA 改名进来的 `.nk-trash-<uuid>`，别的名字
        一概不碰。每一项最多处理 `max_entries` 个条目（默认 `MAX_REMOVE_ENTRIES`）；有一项清不完
        就停，剩下的留着下次再清；从不抛异常卡住调用方。
        """
        purged = 0
        try:
            names = [entry_name for entry_name, _kind in self.iter_entries() if entry_name.startswith(".nk-trash-")]
        except OSError:
            return 0
        for entry_name in names:
            try:
                if self.remove_tree(entry_name, max_entries=max_entries):
                    purged += 1
            except OSError:
                break
        return purged

    def copy_file_in(
        self,
        source: Path,
        name: str,
        *,
        mode: Optional[int] = None,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        """把一个**宿主侧信得过**的文件复制进来（目标按 `write_bytes` 写）

        `mode` 不给时沿用源文件的权限位（去掉 setuid/setgid/sticky）：技能里的脚本要保留可执行位。
        """
        source_stat = source.stat()
        if source_stat.st_size > max_bytes:
            raise UnsafePathError(f"{source.name} 超过 {max_bytes} 字节")
        effective_mode = mode if mode is not None else (stat.S_IMODE(source_stat.st_mode) & 0o777)
        self.write_bytes(name, source.read_bytes(), mode=effective_mode)

    def copy_tree_in(
        self,
        source: Path,
        name: str,
        *,
        dir_mode: int = 0o755,
        file_mode: Optional[int] = None,
    ) -> None:
        """把宿主侧信得过的目录树复制进来；源里的符号链接一律跳过（不把宿主别处的内容带进容器）"""
        with self.subdir(name, create=True, mode=dir_mode, replace_links=True) as target:
            for child in sorted(source.iterdir()):
                if child.is_symlink():
                    continue
                if child.is_dir():
                    target.copy_tree_in(child, child.name, dir_mode=dir_mode, file_mode=file_mode)
                elif child.is_file():
                    target.copy_file_in(child, child.name, mode=file_mode)


def open_dir(
    base: Path,
    rel: RelPath = (),
    *,
    create: bool = False,
    mode: int = 0o755,
    replace_links: bool = False,
) -> DirHandle:
    """从信得过的根目录 `base` 出发，逐级不跟随链接地打开 `rel`

    `base` 本身按普通路径打开：它必须是 NA 自己建的、容器动不了的那一级。
    `create` 时途中不存在的目录会建出来；`replace_links` 时途中被换成链接或文件的一级
    会被删掉重建（只在那一级是 NA 自己的路径时用）。
    """
    parts = split_rel(rel)
    if not DIR_FD_SUPPORTED:
        current = Path(base)
        if create:
            current.mkdir(parents=True, exist_ok=True)
        handle = DirHandle(None, current)
        for part in parts:
            handle = handle.subdir(part, create=create, mode=mode, replace_links=replace_links)
        return handle
    if create:
        Path(base).mkdir(parents=True, exist_ok=True)
    fd = os.open(str(base), os.O_RDONLY | _O_DIRECTORY)
    handle = DirHandle(fd, Path(base))
    try:
        for part in parts:
            child = handle.subdir(part, create=create, mode=mode, replace_links=replace_links)
            handle.close()
            handle = child
    except BaseException:
        handle.close()
        raise
    return handle


def _split_parent(rel: RelPath) -> Tuple[List[str], str]:
    parts = split_rel(rel)
    if not parts:
        raise UnsafePathError("需要一个文件名")
    return parts[:-1], parts[-1]


def write_text(
    base: Path,
    rel: RelPath,
    text: str,
    *,
    mode: Optional[int] = None,
    dir_mode: int = 0o755,
    create_dirs: bool = True,
    replace_links: bool = False,
) -> None:
    """在 `base` 下安全地写一个文本文件（父目录逐级不跟随链接，写入是临时文件 + rename）

    `mode` 不给时沿用原文件的权限位与属主，见 `DirHandle.write_bytes`。
    """
    parents, name = _split_parent(rel)
    with open_dir(base, parents, create=create_dirs, mode=dir_mode, replace_links=replace_links) as directory:
        directory.write_text(name, text, mode=mode)


def write_bytes(
    base: Path,
    rel: RelPath,
    data: bytes,
    *,
    mode: Optional[int] = None,
    dir_mode: int = 0o755,
    create_dirs: bool = True,
    replace_links: bool = False,
) -> None:
    parents, name = _split_parent(rel)
    with open_dir(base, parents, create=create_dirs, mode=dir_mode, replace_links=replace_links) as directory:
        directory.write_bytes(name, data, mode=mode)


def read_text(
    base: Path,
    rel: RelPath,
    *,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> Optional[str]:
    """安全地读一个文本文件；路径不存在返回 None，途中有链接抛 `UnsafePathError`"""
    parents, name = _split_parent(rel)
    try:
        directory = open_dir(base, parents)
    except FileNotFoundError:
        return None
    with directory:
        return directory.read_text(name, max_bytes=max_bytes, encoding=encoding, errors=errors)


def read_bytes(base: Path, rel: RelPath, *, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> Optional[bytes]:
    """安全地读一个文件；路径不存在返回 None，途中有链接抛 `UnsafePathError`"""
    parents, name = _split_parent(rel)
    try:
        directory = open_dir(base, parents)
    except FileNotFoundError:
        return None
    with directory:
        return directory.read_bytes(name, max_bytes=max_bytes)


def chmod(base: Path, rel: RelPath, mode: int) -> None:
    """不跟随链接地改一个条目的权限（目录也可以）；途中或末级是链接抛 `UnsafePathError`"""
    parents, name = _split_parent(rel)
    with open_dir(base, parents) as directory:
        directory.chmod(name, mode)


def remove(base: Path, rel: RelPath) -> bool:
    """安全地删除一个文件或目录树；不存在返回 False"""
    parents, name = _split_parent(rel)
    try:
        directory = open_dir(base, parents)
    except FileNotFoundError:
        return False
    with directory:
        return directory.remove_tree(name)


def remove_file(base: Path, rel: RelPath, *, allow_chmod: bool = False) -> bool:
    """从信得过的根目录 `base` 出发，逐级不跟随链接地删一个非目录条目

    给「先列出来、过一会儿再删」的清理用：列完之后容器能把中间任何一级目录换成指向宿主别处的
    链接，按路径 `unlink` 会顺着它删掉宿主文件。这里途中任何一级是链接（或不是目录）就抛
    `UnsafePathError`，最后一级是链接只删链接本身。

    Returns:
        删掉了返回 True；途中某一级或它本身已经不在了返回 False

    Raises:
        UnsafePathError: 途中有链接，或者它是个目录
        PermissionError: 删不掉（`allow_chmod` 时会先在句柄上放开写权限再试一次）
    """
    parents, name = _split_parent(rel)
    try:
        directory = open_dir(base, parents)
    except FileNotFoundError:
        return False
    with directory:
        entry_kind = directory.kind(name)
        if entry_kind is None:
            return False
        if entry_kind == "dir":
            raise UnsafePathError(f"{name} 是目录，不按文件删")
        try:
            return directory.unlink(name)
        except PermissionError:
            # 只读文件（Windows 上的只读属性）：`chmod` 在句柄上 `O_NOFOLLOW` 打开再改，
            # 不会改到链接指向的宿主文件
            if not allow_chmod or entry_kind != "file":
                raise
            directory.chmod(name, stat.S_IWRITE | stat.S_IREAD)
            return directory.unlink(name)


def _remove_empty_dirs(directory: DirHandle, remaining_depth: int) -> int:
    removed = 0
    for name, entry_kind in directory.entries():
        if entry_kind != "dir":
            continue
        if remaining_depth > 0:
            try:
                with directory.subdir(name) as child:
                    removed += _remove_empty_dirs(child, remaining_depth - 1)
            except (FileNotFoundError, UnsafePathError):
                # 列目录之后被换成了链接或删掉了：不进去，也不删
                continue
        try:
            if directory.rmdir(name):
                removed += 1
        except OSError:
            continue
    return removed


def remove_empty_dirs(base: Path, *, max_depth: int = MAX_TREE_DEPTH) -> int:
    """删掉 `base` 下面所有空的子目录（不含 `base` 自己），从最深处往上删；返回删掉了几个

    用目录句柄往下走：链接不进去也不删（`rmdir` 只认真目录）；按路径 `rmdir` 的话，中间某一级
    被换成链接就会删到宿主别处的空目录。嵌套超过 `max_depth` 的那一层不再往下走，只试着删它
    自己（不空就删不掉，不报错）。
    """
    with open_dir(base) as root:
        return _remove_empty_dirs(root, max_depth)


def ensure_dir(base: Path, rel: RelPath, *, mode: int = 0o755, replace_links: bool = False) -> None:
    open_dir(base, rel, create=True, mode=mode, replace_links=replace_links).close()


def kind(base: Path, rel: RelPath) -> Optional[str]:
    """不跟随链接地看一个条目是什么；途中某一级不存在返回 None"""
    parents, name = _split_parent(rel)
    try:
        directory = open_dir(base, parents)
    except (FileNotFoundError, UnsafePathError):
        return None
    with directory:
        return directory.kind(name)


def iter_files(
    directory: DirHandle,
    prefix: str = "",
    *,
    max_depth: int = MAX_TREE_DEPTH,
) -> Iterator[Tuple[str, DirHandle, str]]:
    """递归列出普通文件 `(相对路径, 所在目录, 文件名)`；链接与特殊文件跳过

    `max_depth` 是还允许再进几层子目录，嵌套更深抛 `TreeTooDeepError`。
    """
    for name, entry_kind in directory.entries():
        rel = f"{prefix}{name}"
        if entry_kind == "file":
            yield rel, directory, name
        elif entry_kind == "dir":
            if max_depth <= 0:
                raise TreeTooDeepError(f"{rel} 的目录嵌套超过上限")
            with directory.subdir(name) as child:
                yield from iter_files(child, prefix=f"{rel}/", max_depth=max_depth - 1)


def _walk_regular(
    directory: DirHandle,
    path: Path,
    remaining_depth: int,
    include_links: bool,
) -> Iterator[Tuple[Path, os.stat_result]]:
    try:
        entries = directory.entries()
    except (FileNotFoundError, NotADirectoryError):
        # 不存在（没有目录句柄的平台上 `open_dir` 不预先打开），或者列之前被删掉了
        return
    for name, entry_kind in entries:
        if entry_kind == "file" or (include_links and entry_kind == "link"):
            st = directory.lstat(name)
            if st is not None:
                yield path / name, st
        elif entry_kind == "dir" and remaining_depth > 0:
            try:
                child = directory.subdir(name)
            except (FileNotFoundError, UnsafePathError):
                continue
            with child:
                yield from _walk_regular(child, path / name, remaining_depth - 1, include_links)


def walk_regular_files(
    base: Path,
    *,
    max_depth: int = MAX_TREE_DEPTH,
    include_links: bool = False,
) -> Iterator[Tuple[Path, os.stat_result]]:
    """从信得过的根目录 `base` 往下列普通文件 `(路径, lstat 结果)`

    给统计大小、按时间挑要清理的文件用：不跟随链接（也不去量链接那头），特殊文件跳过；
    `include_links` 时链接本身也列出来（`lstat` 是链接自己的，清理时只删链接本身）。
    嵌套超过 `max_depth` 的那几层不再往下走（宁可少列，也不让一棵故意造得很深的树撞上递归
    上限）。路径按 `base` 字面拼出来；根目录不存在或不是普通目录时什么都不列。
    """
    try:
        root = open_dir(base)
    except (FileNotFoundError, NotADirectoryError, UnsafePathError):
        return
    with root:
        yield from _walk_regular(root, Path(base), max_depth, include_links)


__all__ = [
    "DEFAULT_MAX_READ_BYTES",
    "DIR_FD_SUPPORTED",
    "MAX_REMOVE_ENTRIES",
    "MAX_TREE_DEPTH",
    "PATH_FD_SUPPORTED",
    "DirHandle",
    "FileTooLargeError",
    "bounded_reader",
    "TreeTooDeepError",
    "UnsafePathError",
    "chmod",
    "ensure_dir",
    "iter_files",
    "kind",
    "open_dir",
    "read_bytes",
    "read_text",
    "remove",
    "remove_empty_dirs",
    "remove_file",
    "split_rel",
    "walk_regular_files",
    "write_bytes",
    "write_text",
]
