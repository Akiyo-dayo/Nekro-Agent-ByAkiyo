"""新建一个只有属主能访问的文件

本来写在 `services/model_group_service.py` 里（备份手填的模型组 Key 用）。配置加载修正非法值前
备份原文件也要用它，而那发生在 `core` 导入阶段，不能去导入 `model_group_service`（它依赖数据库
模型、会提前拉起一大串模块），所以挪到这个只依赖标准库的模块，两处共用一份实现。
`model_group_service` 仍以原来的名字导入它们，既有调用与测试里的替换照旧生效。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Union


def is_windows() -> bool:
    return sys.platform == "win32"


def restrict_to_current_user_windows(path: Path) -> bool:
    """Windows：去掉继承、只授予当前用户完全控制（`icacls`），成功返回 True

    `os.open(..., 0o600)` 在 Windows 上只影响只读位，文件实际继承目录的 ACL（通常 `Users` 组可读）。
    失败（没有 `icacls`、取不到用户名、命令返回非 0）不抛：调用方据返回值在日志里如实说明，不再声称已受保护。
    """
    username = os.environ.get("USERNAME", "")
    if not username:
        return False
    domain = os.environ.get("USERDOMAIN", "")
    principal = f"{domain}\\{username}" if domain else username
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:F"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def write_private_file(path: Path, content: Union[str, bytes]) -> bool:
    """**新建**并写入一份仅属主可访问的文件，返回「访问权限是否确实收紧了」

    `O_EXCL`：不覆盖已有文件。POSIX 上权限在创建那一刻就是 0600，不存在先 644 再 chmod 的窗口，返回 True；
    Windows 上 0600 不是 ACL，写完后用 `icacls` 显式收紧，返回收紧是否成功（False = 文件继承了目录权限）。
    `content` 是 `bytes` 时原样写入（逐字节复制一个文件用），是 `str` 时按 UTF-8 文本写。
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    if isinstance(content, bytes):
        with os.fdopen(fd, "wb") as binary:
            binary.write(content)
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    if is_windows():
        return restrict_to_current_user_windows(path)
    return True


def describe_private_file_protection(owner_only: bool) -> str:
    """日志里说备份文件「保护到了什么程度」的那半句：随平台与实际结果走，不写死 0600"""
    if not is_windows():
        return "权限 0600，仅属主可读写"
    if owner_only:
        return "已用 icacls 把访问权限收紧为仅当前用户"
    return "**Windows 上未能收紧访问权限**，文件继承了数据目录的权限，同机的其他账户可能读到它"
