"""删除路径上的链接判定：符号链接与 Windows 目录联接（junction）

删实例、划转这类「按路径删一整棵目录」的操作，碰到链接只能删链接本身：`shutil.rmtree` 对
符号链接直接报错，而对 Windows 目录联接，旧版 Python 会顺着它删进联接指向的目录；
`Path.resolve()` 之后再判也晚了——解析完的路径已经是目标本身。所以判定一律对**没解析过**
的路径做，用 `lstat`，不跟随。

`Path.is_symlink()` 认不出目录联接（Python 3.12 才有 `is_junction()`），这里按
`FILE_ATTRIBUTE_REPARSE_POINT` 一起认。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_link(path: Path) -> bool:
    """路径本身是符号链接或目录联接（不存在时为假）"""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def unlink_link(path: Path) -> None:
    """只删链接本身，不碰它指向的内容

    POSIX 上 `unlink` 就够；Windows 上目录符号链接与目录联接要用 `rmdir`（删的是联接点，
    不要求目标为空，也不会进入目标）。
    """
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)
