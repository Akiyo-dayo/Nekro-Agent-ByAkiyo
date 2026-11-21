"""导入酒馆卡时生成状态模板（插件这一侧，核心口子见 `services/tavern/state_hook`）

- **检查页**：用卡内书规整后的条目列（导入时写进库的那一份），挑出启用的 `[initvar]` 条目，照运行时的规则
  （order 从大到小、先出现的键优先）合并、推导，只回报字段数、自动标了私密的字段路径与隐藏字段数，不落库。
- **导入之后**：插件在导入目标上生效时（全局人设看全局开关），按刚建好的书推导并存模板（来源 `initvar`）；
  这个人设已经有模板的不动。插件没开时只说一句：启用后首次生效时会按卡里的 `[initvar]` 生成。
- 推导用平台层面的私密词表（模板跨租户共用）。报告里的私密路径是卡片作者起的字段名，只在后台导入对话框
  里显示；日志只写类别。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Optional

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.tavern import StateTemplateReport
from nekro_agent.services.plugin.scope import is_plugin_active
from nekro_agent.services.tavern.state_hook import register_state_template_hook
from nekro_agent.services.worldbook.purpose import EntryPurpose, title_purpose

from . import store
from .initvar import InitvarError, derive_template, parse_initvar_text
from .plugin import plugin
from .schema import Template
from .session import derive_from_book, initvar_entries

logger = get_sub_logger("state_table.import")

MAX_PRIVATE_PATHS = 50
INACTIVE_NOTE = "角色状态插件在这里没有启用；启用后首次生效时会按卡里的 [initvar] 生成模板"


def _report(status: str, template: Template, entries: int) -> StateTemplateReport:
    private = ["/".join(item.path) for item in template.fields if item.private]
    hidden = sum(1 for item in template.fields if item.hidden or any(part.startswith("_") for part in item.path))
    return StateTemplateReport(
        status=status,  # type: ignore[arg-type]
        fields=len(template.fields),
        private_paths=private[:MAX_PRIVATE_PATHS],
        hidden=hidden,
        entries=entries,
    )


def _platform_words() -> tuple[str, ...]:
    from .updater import platform_words

    return platform_words()


async def inspect_columns(columns: Sequence[Any], owner_instance_key: str) -> Optional[StateTemplateReport]:
    """检查页：在内存里推一遍（不落库）；解析与推导放进线程，大卡不卡住事件循环"""
    if not is_plugin_active(plugin.key, owner_instance_key):
        return StateTemplateReport(status="inactive", note=INACTIVE_NOTE)
    return await asyncio.to_thread(_inspect, list(columns), _platform_words())


def _inspect(columns: Sequence[Any], private_words: tuple[str, ...]) -> StateTemplateReport:
    picked: list[tuple[int, int, str]] = []
    for index, row in enumerate(columns):
        if not isinstance(row, dict) or not row.get("enabled", True):
            continue
        title, content = str(row.get("comment") or ""), str(row.get("content") or "")
        if title_purpose(title, content) is not EntryPurpose.INITVAR:
            continue
        picked.append((-int(row.get("order") or 0), index, content))
    if not picked:
        return StateTemplateReport(status="no_initvar")
    picked.sort()
    merged: dict[str, Any] = {}
    for _order, _index, content in picked:
        try:
            tree = parse_initvar_text(content)
        except InitvarError:
            continue
        for key, value in tree.items():
            merged.setdefault(key, value)
    if not merged:
        return StateTemplateReport(status="failed", entries=len(picked), note="[initvar] 条目解析不了")
    try:
        template, _initial = derive_template(merged, private_words=private_words)
    except InitvarError:
        return StateTemplateReport(status="failed", entries=len(picked), note="推导不出模板")
    return _report("preview", template, len(picked))


async def persist_template(preset_id: int, owner_instance_key: str) -> Optional[StateTemplateReport]:
    """导入之后：插件生效时按书推导并存模板；已有模板的不动"""
    if not is_plugin_active(plugin.key, owner_instance_key):
        return StateTemplateReport(status="inactive", note=INACTIVE_NOTE)
    existing_row, existing = await store.load_template(preset_id)
    if existing_row is not None:
        report = _report("exists", existing, 0) if existing is not None else StateTemplateReport(status="exists")
        report.note = "这个人设已经有状态模板，没有改动"
        return report
    entries = await initvar_entries(preset_id)
    if not entries:
        return StateTemplateReport(status="no_initvar")
    derived = await derive_from_book(preset_id, private_words=_platform_words())
    if derived is None:
        return StateTemplateReport(status="failed", entries=len(entries), note="[initvar] 条目解析不了或推导不出模板")
    template, initial, source_entry = derived
    saved = await store.save_template(
        preset_id, template, initial, source="initvar", source_entry_id=source_entry, expected_version=0
    )
    if saved is None:
        # 并发的另一处刚建了模板（比如这个人设已经在某个频道生效）：以那一份为准
        return StateTemplateReport(status="exists", note="这个人设已经有状态模板，没有改动")
    logger.info(f"导入卡片时生成了状态模板: preset_id={preset_id}, 字段={len(template.fields)}")
    return _report("generated", template, len(entries))


register_state_template_hook(plugin.key, inspect_columns, persist_template)


__all__ = ["INACTIVE_NOTE", "inspect_columns", "persist_template"]
