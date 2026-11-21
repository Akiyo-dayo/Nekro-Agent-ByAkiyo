"""现成的两份状态模板：场景（频道状态）与物品栏（个人档案）

不依赖卡里的变量，没导入卡的人设也能套用。套用时与已有模板合并：已经定义过的路径（含同名的顶层字段）
保留原来的，只补缺的；初值同样只补缺的键。现实时间 NA 本来就发，场景里只放故事里的时间。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import tree as state_tree
from .schema import FieldDef, Template


@dataclass(frozen=True)
class Builtin:
    name: str
    title: str
    fields: tuple[FieldDef, ...]
    initial: Mapping[str, Any]


BUILTINS: dict[str, Builtin] = {
    "scene": Builtin(
        name="scene",
        title="场景",
        fields=(
            FieldDef(
                path=("故事时间",),
                type="string",
                description="故事里的时间，比如「第三天傍晚」",
                max_length=40,
                priority=3,
                headline=True,
            ),
            FieldDef(
                path=("所在地点",), type="string", description="现在在哪里", max_length=40, priority=3, headline=True
            ),
            FieldDef(path=("环境",), type="string", description="天气、氛围", max_length=100, priority=3),
        ),
        initial={"故事时间": "", "所在地点": "", "环境": ""},
    ),
    "inventory": Builtin(
        name="inventory",
        title="物品栏",
        fields=(
            FieldDef(
                path=("背包",),
                type="object",
                description="这个人随身带的东西，键是物品名",
                extensible=True,
                template={"数量": 1, "说明": ""},
                person=True,
                priority=2,
            ),
            FieldDef(path=("背包", "*"), type="object", description="一件物品"),
            FieldDef(path=("背包", "*", "数量"), type="number", minimum=0, maximum=999),
            FieldDef(path=("背包", "*", "说明"), type="string", max_length=60),
        ),
        initial={"背包": {}},
    ),
}


@dataclass(frozen=True)
class Merged:
    template: Template
    initial: dict[str, Any]
    added: tuple[str, ...]
    skipped: tuple[str, ...]


def merge(template: Template | None, initial: Mapping[str, Any], kinds: Sequence[str]) -> Merged:
    """把选中的现成模板并进已有模板（没有就新建）；同名的顶层字段跳过，只补缺的

    Raises:
        KeyError: `kinds` 里有不认识的名字。
    """
    base = template or Template()
    fields = list(base.fields)
    defined = {item.path for item in fields}
    top_level = {item.path[0] for item in fields}
    merged_initial = state_tree.copy_tree(dict(initial))
    added: list[str] = []
    skipped: list[str] = []
    for kind in dict.fromkeys(kinds):
        builtin = BUILTINS[kind]
        roots = {item.path[0] for item in builtin.fields}
        if roots & (top_level | set(merged_initial)):
            skipped.append(kind)
            continue
        fields.extend(item for item in builtin.fields if item.path not in defined)
        defined.update(item.path for item in builtin.fields)
        top_level.update(roots)
        for key, value in builtin.initial.items():
            merged_initial.setdefault(key, state_tree.copy_tree(value))
        added.append(kind)
    merged = Template(
        fields=tuple(fields),
        strict_set=base.strict_set,
        private_words=base.private_words,
        theme=base.theme,
    )
    return Merged(template=merged, initial=merged_initial, added=tuple(added), skipped=tuple(skipped))


__all__ = ["BUILTINS", "Builtin", "Merged", "merge"]
