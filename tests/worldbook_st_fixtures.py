"""酒馆世界书格式用例共用的造数与往返工具

放在这里而不是某个用例文件里，是因为两份用例都要用同一套「覆盖全部字段的条目」：
格式用例拿它验逐字段往返，接口用例拿它验导入接口写进库的列是否与解析结果一致。各写
一份的话，接口用例那份会慢慢少几个字段，而它恰恰是用来证明「导进来的东西没丢」的。

模块名不带 `test_` 前缀：它不是用例，import 时不该被 pytest 收集。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from nekro_agent.services.worldbook.st_format import (
    ST_ENTRY_DEFAULTS,
    export_world,
    parse_world_file,
    st_entry_to_columns,
)


def full_entry(uid: int, **overrides: Any) -> dict[str, Any]:
    """按 `ST_ENTRY_DEFAULTS` 造一条覆盖全部字段的条目

    默认值之上再塞几个「插件用不上」的字段与非默认取值，让往返必须真的保留它们。
    """
    entry = copy.deepcopy(ST_ENTRY_DEFAULTS)
    entry["uid"] = uid
    entry.update(
        {
            "key": ["龙", "凤凰"],
            "keysecondary": ["火"],
            "comment": "标题",
            "content": "正文内容",
            "constant": False,
            "selectiveLogic": 1,
            "order": 42,
            "position": 2,
            "depth": 6,
            "scanDepth": 12,
            "caseSensitive": True,
            "matchWholeWords": False,
            "sticky": 3,
            "cooldown": 4,
            "delay": 5,
            "disable": False,
        },
    )
    entry.update(overrides)
    return entry


def world_of(entries: list[dict[str, Any]], **book_extra: Any) -> dict[str, Any]:
    """把条目列表包成一份世界书文件"""
    book: dict[str, Any] = {
        "entries": {str(entry["uid"]): entry for entry in entries},
    }
    book.update(book_extra)
    return book


def dump(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def round_trip(entry: dict[str, Any]) -> dict[str, Any]:
    """一条条目走完整条链路：解析文件 → 映射成列 → 还原 → 导出"""
    parsed = parse_world_file(dump(world_of([entry])))
    _, raw = parsed.entries[0]
    columns, _ = st_entry_to_columns(raw)
    exported = export_world(parsed.book_ext, [(columns, raw)])
    return exported["entries"][str(columns["uid"])]


__all__ = ["dump", "full_entry", "round_trip", "world_of"]
