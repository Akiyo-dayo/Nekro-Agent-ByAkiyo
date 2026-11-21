"""发送路径上剥掉的变量更新块的接收方注册表

MVU 一类的酒馆卡让模型在回复里写变量更新块（`<UpdateVariable>` / `<JSONPatch>`），发送路径会把它们
剥掉（`cleanup_runtime.strip_variable_markup`）。某个功能（角色状态插件的「兼容解析」）要用这些块，
就在这里登记一个接收方：发送路径把这次发送里取到的块的**原文**交给它，核心自己不解析、不应用。

约束：

- **只在登记了接收方时取**：没登记时发送路径与没有这个注册表时逐字相同（不扫、不取、不多查一次库）。
- **不影响发送**：接收方出错、自己取消或超时，这次发送照常；它坐在发送链路上，所以限时。
- **原文只交给接收方**：块里是模型写的东西，这里的日志只记登记者名字与原因，不打原文、不进明细。
- **交的时机**：这次发送真的发出去了（或者剥完只剩变量块、没有可发的文字），并且没有被输出过滤改过；
  被过滤改过的那次发送里的块不交。要不要用、什么时候用（比如等这一轮结局为 ok）由接收方决定。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("tavern.variable_blocks")

# 接收方每次最多等这么久：它在发送链路上，应当只是把原文放进缓冲。
SINK_TIMEOUT_SECONDS = 0.5


@dataclass(frozen=True)
class VariableBlocks:
    """一次发送里取到的更新块

    Attributes:
        one_time_code: 发出这条消息的那一轮的令牌。
        from_chat_key: 那一轮所在的频道。
        chat_key: 这次发往的频道（模型可能往别的频道发；接收方自己决定认不认）。
        blocks: 块的正文，按出现顺序；每块与块数都有上限（见 `cleanup_runtime`）。
        instance_key: 发往的频道所属的实例（接收方据此判断自己在这里生效没有）。
        is_private: 发往的频道是不是私聊。
    """

    one_time_code: str
    from_chat_key: str
    chat_key: str
    blocks: tuple[str, ...]
    instance_key: str = ""
    is_private: bool = False


VariableBlockSink = Callable[[VariableBlocks], Awaitable[None]]
ActiveCheck = Callable[[], bool]

_sinks: dict[str, tuple[VariableBlockSink, Optional[ActiveCheck]]] = {}


def register_variable_block_sink(owner: str, sink: VariableBlockSink, *, active: Optional[ActiveCheck] = None) -> None:
    """登记一个接收方；同名再登记就替换

    `active` 给了的话，它返回假时这个接收方就当没登记（比如插件在进程里整个关着）：发送路径不为它取块。
    """
    _sinks[str(owner)] = (sink, active)


def unregister_variable_block_sink(owner: str) -> None:
    """注销；没登记过是空操作"""
    _sinks.pop(str(owner), None)


def _active_sinks() -> list[tuple[str, VariableBlockSink]]:
    found: list[tuple[str, VariableBlockSink]] = []
    for owner, (sink, active) in list(_sinks.items()):
        try:
            if active is None or active():
                found.append((owner, sink))
        except Exception as error:  # noqa: BLE001 - 判不出来就当没开
            logger.warning(f"变量块接收方 {owner} 判断是否生效时出错: {type(error).__name__}")
    return found


def has_sinks() -> bool:
    """有没有生效的接收方：没有时发送路径不取块，与没有这个注册表时逐字相同"""
    return bool(_active_sinks())


def clear_variable_block_sinks() -> None:
    """清空注册表；只给用例用"""
    _sinks.clear()


# 超时后被丢下的接收方任务：与变量来源注册表同一个写法，持有到它真正结束，取走它的异常。
_ABANDONED: set["asyncio.Task[Any]"] = set()


def _forget(task: "asyncio.Task[Any]") -> None:
    _ABANDONED.discard(task)
    if not task.cancelled():
        task.exception()


async def deliver(payload: VariableBlocks) -> None:
    """把块交给每个接收方；任何失败只记一条日志（不带原文），不往外抛（这次发送本身被取消时除外）"""
    if not payload.blocks:
        return
    for owner, sink in _active_sinks():
        try:
            task: asyncio.Task[Any] = asyncio.ensure_future(sink(payload))
        except Exception as error:  # noqa: BLE001 - 接收方连协程都没给出来
            logger.warning(f"变量块接收方 {owner} 出错: {type(error).__name__}")
            continue
        try:
            done, _pending = await asyncio.wait({task}, timeout=SINK_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            task.cancel()
            _ABANDONED.add(task)
            task.add_done_callback(_forget)
            logger.warning(f"变量块接收方 {owner} 超过 {SINK_TIMEOUT_SECONDS}s 未返回，这次不等它")
            continue
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning(f"变量块接收方 {owner} 自己取消了")
        except Exception as error:  # noqa: BLE001 - 接收方是插件代码
            logger.warning(f"变量块接收方 {owner} 出错: {type(error).__name__}")


__all__ = [
    "SINK_TIMEOUT_SECONDS",
    "VariableBlockSink",
    "VariableBlocks",
    "clear_variable_block_sinks",
    "deliver",
    "has_sinks",
    "register_variable_block_sink",
    "unregister_variable_block_sink",
]
