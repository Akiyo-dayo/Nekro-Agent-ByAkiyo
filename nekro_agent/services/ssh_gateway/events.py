"""凭据变更通知

凭据行被改动、删除或启停之后，网关要立刻作废相关缓存、断开正在用的连接、关掉上游。
服务层不该知道谁在听，所以这里只维护一组回调，由 `credentials.py` / `suspend.py` 在
**写库成功之后**调用 `notify_credential_changed`。

## 为什么每个回调单独兜住

一个订阅者出错不能挡住后面的订阅者，更不能让一次保存失败：凭据已经落库了，通知只是
"顺手让缓存失效"。所以逐个 try/except，只记日志。

## 为什么同步回调也允许

有的订阅者只需要改一个内存集合，写成普通函数最自然；返回协程的则等它跑完再轮到下一个，
保证"通知发出时缓存已经清了"这个顺序对调用方可预期。
"""

from __future__ import annotations

import inspect
from typing import Awaitable, Callable, List

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("ssh_gateway.events")

CredentialChangedCallback = Callable[[int], Awaitable[None] | None]

_callbacks: List[CredentialChangedCallback] = []


def on_credential_changed(callback: CredentialChangedCallback) -> None:
    """订阅凭据变更；重复订阅同一个函数不会去重（调用方自己保证）"""
    _callbacks.append(callback)


def is_subscribed(callback: CredentialChangedCallback) -> bool:
    """这个回调当前是否在订阅列表里（订阅方用来避免重复登记）"""
    return any(existing is callback for existing in _callbacks)


async def notify_credential_changed(credential_id: int) -> None:
    """逐个通知订阅者；每个都单独兜住

    先复制一份列表再遍历：订阅者在处理过程中增删订阅不会影响这一轮。
    """
    value = int(credential_id)
    for callback in list(_callbacks):
        try:
            result = callback(value)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - 一个订阅者失败不能影响写库与别的订阅者
            logger.warning(
                f"SSH 凭据变更通知失败（已忽略）: credential={value} "
                f"callback={getattr(callback, '__name__', repr(callback))} | {type(exc).__name__}: {exc}",
            )


def reset_credential_callbacks() -> None:
    """清空订阅者（仅供测试）"""
    _callbacks.clear()


__all__ = [
    "CredentialChangedCallback",
    "is_subscribed",
    "notify_credential_changed",
    "on_credential_changed",
    "reset_credential_callbacks",
]
