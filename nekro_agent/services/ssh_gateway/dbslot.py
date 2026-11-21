"""网关自己的数据库并发闸

网关与 Bot 共用 NA 的数据库连接池。授权复核、凭据读取、审计批量写入这些操作都不该
把整池占满——Bot 的消息处理、记忆、额度记账都要用同一批连接。所以网关的每一条查库
路径都先拿这张信号量的一个名额，最多只占两个连接。

审计队列与授权模块共用同一个对象（而不是各自 `Semaphore(2)`），因为"最多两个"说的是
整个网关对连接池的占用，不是每个模块两个。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

# 进程级上限：2 个并发库操作。惰性创建是为了让测试在各自的事件循环里重建它。
_semaphore: asyncio.Semaphore = asyncio.Semaphore(2)


@asynccontextmanager
async def db_slot() -> AsyncIterator[None]:
    """占用一个网关库操作名额

    用法：`async with db_slot(): ...`。名额只在数据库调用期间持有，不要把它包住
    网络等待或解密这类慢操作。
    """
    async with _semaphore:
        yield


def reset_db_slots() -> None:
    """重建信号量（仅供测试）

    `asyncio.Semaphore` 在发生等待时会绑定当前事件循环；pytest 每个用例一个新循环，
    不重建会让第二个用例在需要排队时撞上"绑定到另一个循环"的错误。
    """
    global _semaphore
    _semaphore = asyncio.Semaphore(2)


__all__ = ["db_slot", "reset_db_slots"]
