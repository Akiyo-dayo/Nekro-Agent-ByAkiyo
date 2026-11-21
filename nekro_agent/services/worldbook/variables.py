"""世界书模板的变量提供者注册表

世界书条目里的模板（`template_lite`）用 `getvar` / `getLocalVar` 读变量。核心自己没有变量，
运行时默认用空的提供者（什么都读不到）；某个功能（例如角色状态插件）要让模板读到它的数据，
就在这里登记一个**工厂**，每轮由核心调用一次，拿到这一轮、这个频道的提供者。

约束：

- **读路径 fail-open**：没有登记、工厂返回 `None`、抛错、自己取消或超时，一律回落到空提供者——这一轮的
  世界书与没有这个功能时逐字相同，不能因为变量拖住或打断一轮回复。
- **工厂一次准备好，`get` 只读内存**：工厂是异步的，查库在它里面做；它返回的提供者的 `get` 是同步的，
  在模板求值里被一条条调用，不能做任何 IO。
- **每轮一个实例**：提供者只活在这一轮的求值里，不进 `purpose` 的分析缓存（那份缓存按条目
  跨轮、跨频道共享）。
- **变量值按外部输入看**：拿到的提供者由核心再包一层（:class:`GuardedVariables`）——字符串里的
  `{{` 用零宽空格断开（模板算出来的文字之后还会做宏展开，变量值里的 `{{user}}` 不能被当成宏）；
  只放行 JSON 形状的值；`get` 里的任何异常都按「读不到」处理。
- **日志不打值**：变量值来自聊天，这里的日志只记登记者名字与原因。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

from nekro_agent.core.logger import get_sub_logger

from . import template_lite

logger = get_sub_logger("worldbook.variables")

# 工厂每轮最多等这么久：它坐在回复链路上，慢了宁可这一轮不给变量。
FACTORY_TIMEOUT_SECONDS = 0.5
# 变量值嵌套的上限：与求值器同一个数，更深的整体按「读不到」处理。
MAX_VALUE_DEPTH = template_lite.MAX_VALUE_DEPTH
# 断开宏的写法：插一个零宽空格。宏展开不认它；不可信组清洗时零宽字符会被去掉，但清洗发生在宏展开
# 之后，去掉以后也不会再被展开。要断开的有三种：
# - 值里相邻的花括号（`{{`、`}}`，连着三个也逐个断开）；
# - 值首尾单个的花括号：两个变量挨着输出时能拼出 `{{`（`'abc{'` 接 `'{user}}'`）；
# - 旧式宏 `<user>` / `<bot>` / `<char>`（宏展开按 Unicode 忽略大小写认它，这里用同样的写法）。
_ZERO_WIDTH = "​"
_ADJACENT_OPEN = re.compile(r"\{(?=\{)")
_ADJACENT_CLOSE = re.compile(r"\}(?=\})")
_LEGACY_MACRO_OPEN = re.compile(r"<(?=(?:user|bot|char)>)", re.IGNORECASE)


def _break_macros(text: str) -> str:
    """让变量值里的文字在之后的宏展开里不被认成宏；没有可疑字符时原样返回"""
    if "{" in text or "}" in text:
        text = _ADJACENT_OPEN.sub("{" + _ZERO_WIDTH, text)
        text = _ADJACENT_CLOSE.sub("}" + _ZERO_WIDTH, text)
        if text[:1] in ("{", "}"):
            text = _ZERO_WIDTH + text
        if text[-1:] in ("{", "}"):
            text = text + _ZERO_WIDTH
    if "<" in text:
        text = _LEGACY_MACRO_OPEN.sub("<" + _ZERO_WIDTH, text)
    return text


@dataclass(frozen=True)
class RoundVariableContext:
    """工厂拿到的这一轮的上下文

    Attributes:
        one_time_code: 本轮令牌（工厂可以按它缓存这一轮读过的数据）。
        channel: 当前频道（`DBChatChannel`）。
        chat_key: 频道标识。
        preset_id: 预览「换一个人设看看」时是所选人设；正式回合为 `None`，由工厂按频道当前人设解析。
        is_private: 是否私聊频道（群聊、未知都算否）。
        trigger_person_uid: 触发这一轮的人的平台账号；判不出时为空串。
        config: 频道生效配置。
        dry_run: 是否预览 / 试发（工厂只能读，不能写）。
    """

    one_time_code: str
    channel: Any
    chat_key: str
    preset_id: Optional[int]
    is_private: bool
    trigger_person_uid: str
    config: Any
    dry_run: bool


VariableSourceFactory = Callable[[RoundVariableContext], Awaitable[Optional[template_lite.VariableProvider]]]

# 按登记顺序；第一个给出提供者的生效。
_sources: dict[str, VariableSourceFactory] = {}


def register_variable_source(owner: str, factory: VariableSourceFactory) -> None:
    """登记一个变量来源；同名再登记就替换"""
    _sources[str(owner)] = factory


def unregister_variable_source(owner: str) -> None:
    """注销；没登记过是空操作"""
    _sources.pop(str(owner), None)


def registered_sources() -> tuple[str, ...]:
    """已登记的来源名，按登记顺序"""
    return tuple(_sources)


def clear_variable_sources() -> None:
    """清空注册表；只给用例用"""
    _sources.clear()


# 超时后被丢下的工厂任务：已经取消，但工厂可能吞掉取消、还在跑。持有强引用直到它真正结束，
# 结束时取走它的异常（不让事件循环报「异常没人取」）。这一轮不等它。
_ABANDONED: set["asyncio.Task[Any]"] = set()


def _forget(task: "asyncio.Task[Any]") -> None:
    _ABANDONED.discard(task)
    if not task.cancelled():
        task.exception()


async def provider_for_round(ctx: RoundVariableContext) -> template_lite.VariableProvider:
    """这一轮的变量提供者；没有可用的来源时是空提供者（与现在逐字相同的那一个）

    工厂放在单独的任务里限时等：它超时（哪怕吞掉了取消还在跑）、出错、自己抛出取消，这一轮都
    回落到下一个来源或空提供者，不被它拖住。只有这一轮本身被取消时才把取消往外抛。
    """
    for owner, factory in list(_sources.items()):
        try:
            task: asyncio.Task[Any] = asyncio.ensure_future(factory(ctx))
        except Exception as error:  # noqa: BLE001 - 工厂连协程都没给出来
            logger.warning(f"变量来源 {owner} 出错，这一轮不给模板变量: {type(error).__name__}")
            continue
        try:
            done, _pending = await asyncio.wait({task}, timeout=FACTORY_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            task.cancel()
            _ABANDONED.add(task)
            task.add_done_callback(_forget)
            logger.warning(f"变量来源 {owner} 超过 {FACTORY_TIMEOUT_SECONDS}s 未返回，这一轮不给模板变量")
            continue
        try:
            provider = task.result()
        except asyncio.CancelledError:
            # 这一轮没有被取消（上面的等待正常返回了）：是工厂自己抛出的取消，按出错处理。
            logger.warning(f"变量来源 {owner} 自己取消了，这一轮不给模板变量")
            continue
        except Exception as error:  # noqa: BLE001 - 读路径 fail-open
            logger.warning(f"变量来源 {owner} 出错，这一轮不给模板变量: {type(error).__name__}")
            continue
        if provider is None:
            continue
        if provider is template_lite.EMPTY_VARIABLES:
            return provider
        return GuardedVariables(provider)
    return template_lite.EMPTY_VARIABLES


def _guard(value: Any, depth: int = 0) -> Any:
    """把一个变量值变成只含 JSON 形状、字符串里的 `{{` 已断开的副本；不合格的整体按读不到"""
    if depth > MAX_VALUE_DEPTH:
        return template_lite.UNDEFINED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _break_macros(value)
    if isinstance(value, Mapping):
        guarded: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                return template_lite.UNDEFINED
            inner = _guard(item, depth + 1)
            if inner is template_lite.UNDEFINED:
                return template_lite.UNDEFINED
            guarded[_guard(key, depth + 1)] = inner
        return guarded
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for item in value:
            inner = _guard(item, depth + 1)
            if inner is template_lite.UNDEFINED:
                return template_lite.UNDEFINED
            items.append(inner)
        return items
    return template_lite.UNDEFINED


class GuardedVariables:
    """核心给每个真实提供者包的一层：断开宏、只放行 JSON 形状、异常按读不到

    同一个路径在一轮里可能被读很多次（`<%= getvar('s') %>` 写在多段里），结果按 `(作用域, 路径)`
    记在这一轮的实例上，不重复复制整棵树。

    `get` 是**同步**调用，在模板求值里一条条地读：提供者的 `get` 只能读内存里已经准备好的数据，
    不能查库、发请求、等锁（要读的东西在工厂里一次准备好）。
    """

    def __init__(self, inner: template_lite.VariableProvider) -> None:
        self._inner = inner
        self._memo: dict[tuple[str, str], Any] = {}

    def get(self, scope: str, path: str) -> Any:
        key = (scope, path)
        if key in self._memo:
            return self._memo[key]
        try:
            raw = self._inner.get(scope, path)
        except Exception:  # noqa: BLE001 - 提供者是插件代码，出错按读不到
            raw = template_lite.UNDEFINED
        value = raw if raw is template_lite.UNDEFINED else _guard(raw)
        self._memo[key] = value
        return value


__all__ = [
    "FACTORY_TIMEOUT_SECONDS",
    "GuardedVariables",
    "RoundVariableContext",
    "VariableSourceFactory",
    "clear_variable_sources",
    "provider_for_round",
    "register_variable_source",
    "registered_sources",
    "unregister_variable_source",
]
