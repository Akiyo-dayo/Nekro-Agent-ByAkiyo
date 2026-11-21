"""数据目录里的「本部署专用、不轮换」密钥文件

有些服务端密钥只用来给**存进库里、或交给模型记住**的东西算摘要（例如邮箱账户的不透明引用
`platform:xxxx`）。它们和 `JWT_SECRET_KEY` 的要求正好相反：

- `JWT_SECRET_KEY` 要求可以随时轮换，没配时每进程随机（重启 = 全员重登，安全的一侧）；
- 这类密钥一换，库里已经存着的摘要就全对不上了——排着队的节日邮件会被判成「发件账户已不可用」，
  模型上一轮记下的引用也认不出来。

所以它们单独放一把：第一次用到时在数据目录里生成（`O_EXCL` 建文件、POSIX 下 0600），以后一直沿用。
做法与 `tools/credential_cipher.py` 的密钥文件相同，但**不复用那一把**：那一把在「从文件挪到环境变量」时
会换主密钥（有历史密钥兜着解密），拿它派生摘要同样会在那次操作后整体变掉。

文件丢了（恢复备份时漏了它）的后果只是这些摘要变了一次，不涉及任何数据解不开。
"""

from __future__ import annotations

import os
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Optional, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

logger = get_sub_logger("local_secret")


def local_secret_path(file_name: str) -> Path:
    """密钥文件的绝对路径（数据目录在调用时读，不缓存）"""
    return Path(OsEnv.DATA_DIR).expanduser() / file_name


_MISSING = "missing"
_EMPTY = "empty"
_UNREADABLE = "unreadable"
_OK = "ok"


def _read(path: Path) -> Tuple[Optional[str], str]:
    """读密钥文件，返回 `(内容, 状态)`；状态是 `ok` / `missing` / `empty` / `unreadable`"""
    try:
        if not path.is_file():
            return None, _MISSING
        material = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.error(f"密钥文件读取失败（{path}）：{type(e).__name__}")
        return None, _UNREADABLE
    return (material, _OK) if material else (None, _EMPTY)


def _write_private(path: Path, material: str) -> None:
    """新建一个只有属主可读写的文件并写完、刷到盘上（文件已存在时抛 FileExistsError）"""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(material)
        f.flush()
        os.fsync(f.fileno())


def load_or_create_local_secret(file_name: str, *, purpose: str) -> Optional[str]:
    """读出数据目录里的密钥文件；没有就生成一把

    **先写临时文件、写完再用 `os.link` 放到位**：「不存在才放」是原子的，而且放到位的那一刻内容已经
    写完、刷到盘上。直接 `O_EXCL` 建正式文件再写的话，别的进程可能在「建好了、还没写进去」的空当读到
    一个空文件，整个进程生命周期都退回到 JWT 派生；在那一刻崩溃还会留下一个空文件，之后每次启动都退回。
    两个进程同时生成时 `link` 只有一个成功，另一个回头读它那把。

    读到空文件按损坏处理（老版本的写法崩溃后会留下它）：记 ERROR、删掉、重新生成。读不了（权限不对）时
    不去覆盖它；读不了也建不了时返回 None，由调用方决定退路，**不抛异常**。文件系统不支持硬链接时退回
    `O_EXCL` 直接建正式文件（与旧做法相同）。

    Args:
        file_name: 数据目录下的文件名
        purpose: 这把密钥是干什么用的（只进日志）
    """
    path = local_secret_path(file_name)
    existing, state = _read(path)
    if existing:
        return existing
    if state == _UNREADABLE:
        return None
    if state == _EMPTY:
        logger.error(f"密钥文件是空的（{path}，{purpose}）：多半是上一次写入中途崩溃留下的，删掉后重新生成")
        with suppress(OSError):
            path.unlink()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error(f"密钥文件的目录创建失败（{path.parent}，{purpose}）：{type(e).__name__}")
        return None

    material = secrets.token_urlsafe(48)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        _write_private(temp, material)
        try:
            os.link(temp, path)
        except FileExistsError:
            # 另一个进程刚放好了它那把：用它的
            return _read(path)[0]
        except OSError:
            # 文件系统不支持硬链接：退回直接建正式文件（另一个进程先建好时同样读它那把）
            try:
                _write_private(path, material)
            except FileExistsError:
                return _read(path)[0]
    except OSError as e:
        logger.error(f"密钥文件创建失败（{path}，{purpose}）：{type(e).__name__}")
        return None
    finally:
        with suppress(OSError):
            temp.unlink()
    logger.info(f"已在数据目录生成一把密钥文件（{path}，{purpose}）；备份数据目录时请一并备份")
    return material


__all__ = ["load_or_create_local_secret", "local_secret_path"]
