"""宿主侧读沙盒共享目录里的文件

沙盒共享目录（`{SANDBOX_SHARED_HOST_DIR}/<container_key>`，沙盒里是 `/app/shared`）按频道复用、
0777 挂进沙盒：沙盒里的代码能在里面放指向宿主任意位置的符号链接。`convert_to_host_path` /
`FileSystem.get_file` 拼路径时已经 `resolve()` 判过归属，但判完到真正打开之间沙盒还在运行，
完全可以在这个时间窗里把文件换成链接（检查时是普通文件、读的时候是 `configs/nekro-agent.yaml`）。

所以插件真正读文件时走这里：

- 路径落在某个沙盒共享目录下：从那个 `<container_key>` 目录（NA 建的、沙盒换不掉的那一级）
  出发逐级 `O_NOFOLLOW` 打开，途中或末级是链接一律拒绝；
- 要把路径交给别人（适配器发文件）时，先按同样的规则复制一份到这个频道的上传目录
  （NA 写、沙盒只读），交出去的是副本的路径；
- 不在共享目录下：只有落在上传目录里（NA 写、沙盒只读；NA 自己下载的文件也在这里，两种写法
  都算）才按普通路径打开，别的地方一律拒绝，不再退回会跟随链接的普通 `open()`。

## 「在不在共享目录下」怎么判

共享目录根有两种写法：按字面展开的绝对路径（`abspath`），与解析掉链接之后的真实路径
（`realpath`）。拼共享目录路径的地方各用各的：`FileUtils.get_sandbox_shared_host_path`
对相对的 `DATA_DIR` 会 `resolve()`，别处是字面拼接。NA 的工作目录路径里有链接时两种写法
对不上；原来只认字面写法，对不上就当成「不在共享目录下」、退回会跟随链接的普通 `open()`——
等于这道防线在这种部署上整个失效。

现在两种写法都认（`shared_root_forms`）。仍然对不上、但按真实路径看落在共享目录里的
（经别的别名绕进来的），一律拒绝，不退回普通 `open()`。根目录本身是 NA 建的那几级，
解析它里面的链接是安全的；共享目录**里面**的链接从来不解析。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import BinaryIO, List, Optional, Tuple

from nekro_agent.core import os_env
from nekro_agent.core.logger import logger
from nekro_agent.core.os_env import SANDBOX_SHARED_HOST_DIR
from nekro_agent.services.workspace import safe_fs

# 发给适配器的文件、白板 / 表情读取的上限；与上传目录的常规文件同一量级
DEFAULT_MAX_BYTES = 512 * 1024 * 1024


class UnsafeSharedFileError(ValueError):
    """共享目录里的这个文件（或途中某一级）是符号链接 / 不是普通文件"""


def shared_root_forms(shared_root: Optional[Path] = None) -> List[Path]:
    """共享目录根的写法：字面展开的绝对路径，以及（不同时）解析掉链接后的真实路径

    判「在不在共享目录下」的唯一口径，见模块文档。
    """
    raw = str(shared_root) if shared_root is not None else str(SANDBOX_SHARED_HOST_DIR)
    literal = Path(os.path.abspath(raw))
    resolved = Path(os.path.realpath(raw))
    return [literal] if resolved == literal else [literal, resolved]


def split_shared_path(host_path: Path, *, shared_root: Optional[Path] = None) -> Optional[Tuple[Path, List[str]]]:
    """路径落在某个沙盒共享目录下时返回 `(该沙盒的共享目录, 目录下的相对路径各段)`，否则 None

    共享目录根的两种写法都认（见 `shared_root_forms`）；候选路径只按字面展开，不解析链接。
    """
    candidate = Path(os.path.abspath(str(host_path)))
    for root in shared_root_forms(shared_root):
        try:
            rel = candidate.relative_to(root)
        except ValueError:
            continue
        parts = list(rel.parts)
        if len(parts) < 2:
            return None
        return root / parts[0], parts[1:]
    return None


def _reaches_shared_dir(host_path: Path, *, shared_root: Optional[Path] = None) -> bool:
    """按真实路径看，这个路径是不是落在共享目录里（只用来判「要不要拒绝」，从不拿它去打开）"""
    real_root = Path(os.path.realpath(str(shared_root) if shared_root is not None else str(SANDBOX_SHARED_HOST_DIR)))
    try:
        Path(os.path.realpath(str(host_path))).relative_to(real_root)
    except ValueError:
        return False
    return True


def _refuse_alias(host_path: Path, action: str) -> UnsafeSharedFileError:
    logger.warning(f"拒绝{action}: 路径经别的写法落进了沙盒共享目录，对不上共享目录的规整路径: {host_path}")
    return UnsafeSharedFileError(f"路径落在沙盒共享目录里，但写法对不上，拒绝{action}: {Path(host_path).name}")


def _trusted_root_forms() -> List[Path]:
    """可以按普通路径打开的根目录：上传目录（NA 写、沙盒只读），两种写法都算

    调用时读 `os_env.USER_UPLOAD_DIR`（与 `snapshot_for_handoff` 落副本的目录同一个来源）。
    """
    raw = str(os_env.USER_UPLOAD_DIR)
    literal = Path(os.path.abspath(raw))
    resolved = Path(os.path.realpath(raw))
    return [literal] if resolved == literal else [literal, resolved]


def _under_trusted_root(host_path: Path) -> bool:
    """不在共享目录下的路径，只有落在上传目录里（字面写法）才按普通路径打开

    主沙盒给的路径只会被映射到上传目录或共享目录（`convert_to_host_path`），NA 自己下载的
    文件也落在上传目录；别的地方一律不认，不再退回会跟随链接的普通 `open()`。

    判的是字面写法，所以路径里带 `..` 的一律不认：`abspath` 按字面把 `<上传目录>/a/../x` 规整成
    `<上传目录>/x`，而系统打开时先走 `a`——`a` 若是指向别处的链接，`..` 回到的是链接目标的上级，
    早就不在上传目录里了。
    """
    if ".." in Path(str(host_path)).parts:
        return False
    candidate = Path(os.path.abspath(str(host_path)))
    for root in _trusted_root_forms():
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _refuse_untrusted(host_path: Path, action: str) -> UnsafeSharedFileError:
    logger.warning(f"拒绝{action}: 路径既不在沙盒共享目录下，也不在上传目录下: {host_path}")
    return UnsafeSharedFileError(f"路径不在沙盒共享目录或上传目录下，拒绝{action}: {Path(host_path).name}")


def open_host_file(
    host_path: Path,
    *,
    max_bytes: Optional[int] = DEFAULT_MAX_BYTES,
    shared_root: Optional[Path] = None,
) -> BinaryIO:
    """打开一个宿主侧文件读；在沙盒共享目录下时逐级不跟随链接

    返回的流读到超过 `max_bytes` 时抛 `safe_fs.FileTooLargeError`（打开时已判过一次大小，
    读的过程中再数一次：文件可能在读的时候被追加）。

    Raises:
        FileNotFoundError: 文件不存在
        UnsafeSharedFileError: 共享目录里的路径上有链接、不是普通文件、超过上限，
            或者路径经别的写法落进了共享目录
    """
    split = split_shared_path(host_path, shared_root=shared_root)
    if split is None:
        if _reaches_shared_dir(host_path, shared_root=shared_root):
            raise _refuse_alias(host_path, "读取")
        if not _under_trusted_root(host_path):
            raise _refuse_untrusted(host_path, "读取")
        path = Path(host_path)
        if max_bytes is not None and path.stat().st_size > max_bytes:
            raise UnsafeSharedFileError(f"文件超过 {max_bytes} 字节: {path.name}")
        return safe_fs.bounded_reader(open(path, "rb"), max_bytes, path.name)  # noqa: SIM115 - 交给调用方关闭
    base, parts = split
    try:
        with safe_fs.open_dir(base, parts[:-1]) as directory:
            return directory.open_read(parts[-1], max_bytes=max_bytes)
    except safe_fs.FileTooLargeError as e:
        raise UnsafeSharedFileError(f"文件超过 {max_bytes} 字节: {Path(host_path).name}") from e
    except safe_fs.UnsafePathError as e:
        logger.warning(f"拒绝读取沙盒共享目录里的非普通文件: {host_path}: {e}")
        raise UnsafeSharedFileError(f"文件不是普通文件（可能是符号链接）: {Path(host_path).name}") from e


def read_host_file_bytes(host_path: Path, *, max_bytes: Optional[int] = DEFAULT_MAX_BYTES) -> bytes:
    try:
        with open_host_file(host_path, max_bytes=max_bytes) as handle:
            data = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
    except safe_fs.FileTooLargeError as e:
        raise UnsafeSharedFileError(f"文件超过 {max_bytes} 字节: {Path(host_path).name}") from e
    if max_bytes is not None and len(data) > max_bytes:
        raise UnsafeSharedFileError(f"文件超过 {max_bytes} 字节: {Path(host_path).name}")
    return data


def host_file_is_regular(host_path: Path, *, shared_root: Optional[Path] = None) -> bool:
    """是不是一个普通文件；在沙盒共享目录下时不跟随链接（链接算「不是」）

    经别的写法落进共享目录的路径算「不是」（见模块文档）。
    """
    split = split_shared_path(host_path, shared_root=shared_root)
    if split is None:
        if _reaches_shared_dir(host_path, shared_root=shared_root):
            logger.warning(f"路径经别的写法落进了沙盒共享目录，按不是普通文件处理: {host_path}")
            return False
        if not _under_trusted_root(host_path):
            logger.warning(f"路径既不在沙盒共享目录下，也不在上传目录下，按不是普通文件处理: {host_path}")
            return False
        return Path(host_path).is_file()
    base, parts = split
    try:
        return safe_fs.kind(base, parts) == "file"
    except (OSError, ValueError):
        # 某一级名字不合法（例如带 `\`）：`split_rel` 拒绝它，这里按「不是」回答，不往上抛
        return False


def snapshot_for_handoff(host_path: Path, chat_key: str, *, max_bytes: Optional[int] = DEFAULT_MAX_BYTES) -> Path:
    """要把路径交给别人（适配器、外部服务）时用：共享目录里的文件先复制一份到这个频道的上传目录

    交出去的是副本：之后沙盒再怎么换链接，副本都不受影响。不在共享目录下的路径原样返回；
    经别的写法落进共享目录的路径拒绝（见模块文档）。
    """
    split = split_shared_path(host_path)
    if split is None:
        if _reaches_shared_dir(host_path):
            raise _refuse_alias(host_path, "转交")
        if not _under_trusted_root(host_path):
            raise _refuse_untrusted(host_path, "转交")
        return Path(host_path)
    from nekro_agent.tools.path_convertor import safe_upload_file_name, sanitize_chat_key_for_path

    base, parts = split
    # 每份副本一个独立子目录：文件名保持原样（适配器发文件时对方看到的就是它），
    # 又不会盖掉上传目录里同名的用户文件
    upload_root = Path(os_env.USER_UPLOAD_DIR)
    target_dir = upload_root / sanitize_chat_key_for_path(chat_key) / ".outbound" / uuid.uuid4().hex
    target = target_dir / (safe_upload_file_name(parts[-1]) or "file")
    if not target.resolve().is_relative_to(upload_root.resolve()):
        raise UnsafeSharedFileError("转交副本的落盘目标不在上传目录里")
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        with safe_fs.open_dir(base, parts[:-1]) as directory:
            directory.copy_out(parts[-1], target, max_bytes=max_bytes)
    except safe_fs.UnsafePathError as e:
        logger.warning(f"拒绝转交沙盒共享目录里的非普通文件: {host_path}: {e}")
        raise UnsafeSharedFileError(f"文件不是普通文件（可能是符号链接）: {parts[-1]}") from e
    return target


__all__ = [
    "DEFAULT_MAX_BYTES",
    "UnsafeSharedFileError",
    "host_file_is_regular",
    "open_host_file",
    "read_host_file_bytes",
    "shared_root_forms",
    "snapshot_for_handoff",
    "split_shared_path",
]
