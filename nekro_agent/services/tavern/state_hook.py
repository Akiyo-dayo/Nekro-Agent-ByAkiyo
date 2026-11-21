"""导入酒馆卡时生成角色状态模板的口子

角色状态是可选插件，核心不认识它的模板格式。插件在这里登记两个函数：

- `inspect(book_entries, owner_instance_key)`：检查页（导入之前）用卡内书的条目推一遍，只回报计数与私密
  字段路径，不落库；
- `persist(preset_id, owner_instance_key)`：导入事务提交之后调，插件在导入目标上生效时按刚建好的书推导并存
  模板（这个人设已经有模板的不动）。

没登记时两处都返回 None，导入与没有这个口子时逐字相同。钩子出错、超时都不影响导入：报告里写 `failed`，
原因只写类别，日志不带卡片内容。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Optional

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.tavern import StateTemplateReport

logger = get_sub_logger("tavern.state_hook")

HOOK_TIMEOUT_SECONDS = 10.0

StateInspect = Callable[[Sequence[Any], str], Awaitable[Optional[StateTemplateReport]]]
StatePersist = Callable[[int, str], Awaitable[Optional[StateTemplateReport]]]

_hooks: dict[str, tuple[StateInspect, StatePersist]] = {}


def register_state_template_hook(owner: str, inspect: StateInspect, persist: StatePersist) -> None:
    """登记；同名再登记就替换"""
    _hooks[str(owner)] = (inspect, persist)


def unregister_state_template_hook(owner: str) -> None:
    _hooks.pop(str(owner), None)


def clear_state_template_hooks() -> None:
    """清空注册表；只给用例用"""
    _hooks.clear()


async def _call(owner: str, awaitable: Awaitable[Optional[StateTemplateReport]]) -> Optional[StateTemplateReport]:
    try:
        return await asyncio.wait_for(awaitable, timeout=HOOK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(f"状态模板钩子 {owner} 超时，这一次不生成")
        return StateTemplateReport(status="failed", note="超时")
    except Exception as error:  # noqa: BLE001 - 钩子是插件代码，不能让导入失败
        logger.warning(f"状态模板钩子 {owner} 出错: {type(error).__name__}")
        return StateTemplateReport(status="failed", note="出错")


async def inspect(book_entries: Any, owner_instance_key: str) -> Optional[StateTemplateReport]:
    """检查页的预览；没有登记、卡里没有书时返回 None"""
    if not _hooks or not isinstance(book_entries, list):
        return None
    owner, (inspect_hook, _persist) = next(iter(_hooks.items()))
    return await _call(owner, inspect_hook(book_entries, owner_instance_key))


async def persist(preset_id: int, owner_instance_key: str) -> Optional[StateTemplateReport]:
    """导入之后生成模板；没有登记时返回 None"""
    if not _hooks:
        return None
    owner, (_inspect, persist_hook) = next(iter(_hooks.items()))
    return await _call(owner, persist_hook(preset_id, owner_instance_key))


__all__ = [
    "HOOK_TIMEOUT_SECONDS",
    "StateInspect",
    "StatePersist",
    "clear_state_template_hooks",
    "inspect",
    "persist",
    "register_state_template_hook",
    "unregister_state_template_hook",
]
