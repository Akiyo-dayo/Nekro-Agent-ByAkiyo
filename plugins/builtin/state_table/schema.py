"""状态模板：状态树里有哪些字段、什么类型、谁能改、哪些是私密的

模板存在 `state_template.field_defs` 里（一个人设一份），形状是 `{"fields": [字段, …], "strict_set": bool}`。
字段的路径是一串键（相对 `stat_data` 的根）；可扩展映射下的「任意键」写成 `*`，例如
`("角色", "*", "心情")` 匹配 `角色.阿青.心情`。

几条约定（不是 MVU 的语义，是 NA 的默认规则，管理员可以在模板上改）：

- 路径上任何一段以 `_` 开头的字段默认隐藏且只读：不进状态块、更新调用不能改，模板里的条目照样能读。
- 路径或字段名命中私密词表的默认私密：只在私聊里注入与更新。
- `person` 只能标在顶层：那一棵子树按人存（个人档案），不放在频道状态里。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Optional

WILDCARD = "*"
TYPES = ("string", "number", "boolean", "object", "array")
AFFECTION_LINKS = ("score", "stage")
# 状态图主题：与 `services/imagecard/state_card.py` 的 `STATE_CARD_THEMES` 同一组名字
THEMES = ("default", "night", "sakura", "forest", "ink")

DEFAULT_MAX_LENGTH = 200
HARD_MAX_LENGTH = 2000
DEFAULT_MAX_ITEMS = 20
HARD_MAX_ITEMS = 100
MAX_FIELDS = 500
MAX_PATH_DEPTH = 16
MAX_KEY_CHARS = 64
MAX_DESCRIPTION_CHARS = 200
# 一份模板最多记住这么多条路径的查找结果（一棵 64 KB 的状态树远到不了）
MAX_MEMO = 50_000
# 截断顺序：数小的组先截。顶层数组（事件、伏笔一类）默认 1，顶层对象 2，顶层单值 3。
DEFAULT_PRIORITY = 2


class TemplateError(ValueError):
    """模板不合法；消息里只有路径与原因，不带值"""


@dataclass(frozen=True)
class FieldDef:
    """一个字段的定义"""

    path: tuple[str, ...]
    type: str
    description: str = ""
    pair: bool = False
    readonly: bool = False
    private: bool = False
    hidden: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    max_length: int = DEFAULT_MAX_LENGTH
    max_items: int = DEFAULT_MAX_ITEMS
    extensible: bool = False
    template: Any = None
    required: bool = False
    priority: int = DEFAULT_PRIORITY
    person: bool = False
    affection: Optional[str] = None
    # 状态图的页头字段（时间、地点一类）；没标时顶层名字里带「时间、日期、地点、位置、天气」的单值自动算
    headline: bool = False

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": list(self.path), "type": self.type}
        defaults = FieldDef(path=self.path, type=self.type)
        for name in (
            "description",
            "pair",
            "readonly",
            "private",
            "hidden",
            "minimum",
            "maximum",
            "max_length",
            "max_items",
            "extensible",
            "template",
            "required",
            "priority",
            "person",
            "affection",
            "headline",
        ):
            value = getattr(self, name)
            if value != getattr(defaults, name):
                data[name] = value
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "FieldDef":
        if not isinstance(data, Mapping):
            raise TemplateError("字段定义必须是对象")
        path = _path_of(data.get("path"))
        kind = data.get("type")
        if kind not in TYPES:
            raise TemplateError(f"{_show(path)}：类型必须是 {'/'.join(TYPES)} 之一")
        affection = data.get("affection")
        if affection is not None and affection not in AFFECTION_LINKS:
            raise TemplateError(f"{_show(path)}：affection 只能是 score 或 stage")
        person = bool(data.get("person", False))
        if person and len(path) != 1:
            raise TemplateError(f"{_show(path)}：person 只能标在顶层字段上")
        return cls(
            path=path,
            type=str(kind),
            description=str(data.get("description", "") or "")[:MAX_DESCRIPTION_CHARS],
            pair=bool(data.get("pair", False)),
            readonly=bool(data.get("readonly", False)),
            private=bool(data.get("private", False)),
            hidden=bool(data.get("hidden", False)),
            minimum=_number_or_none(data.get("minimum")),
            maximum=_number_or_none(data.get("maximum")),
            max_length=_clamp_int(data.get("max_length"), DEFAULT_MAX_LENGTH, 1, HARD_MAX_LENGTH),
            max_items=_clamp_int(data.get("max_items"), DEFAULT_MAX_ITEMS, 1, HARD_MAX_ITEMS),
            extensible=bool(data.get("extensible", False)),
            template=data.get("template"),
            required=bool(data.get("required", False)),
            priority=_clamp_int(data.get("priority"), DEFAULT_PRIORITY, 0, 9),
            person=person,
            affection=affection,
            headline=bool(data.get("headline", False)) and len(path) == 1,
        )


@dataclass(frozen=True)
class Template:
    """一份状态模板：字段定义的集合"""

    fields: tuple[FieldDef, ...] = ()
    strict_set: bool = False
    # 推导模板时用的私密词表（平台层面的）：运行时照样按它对具体路径再判一次，配置后来改了也不放宽。
    private_words: tuple[str, ...] = ()
    # 状态图的主题（几个固定的名字之一，见 `THEMES`）；空串是默认主题。不收任意颜色。
    theme: str = ""
    _index: dict[tuple[str, ...], FieldDef] = field(default_factory=dict, compare=False, repr=False)
    # 前缀树：{段: [这一层的定义, 子树]}，通配段的键就是 `*`。渲染、去私密、校验每个节点都要查好几次，
    # 线性扫全部字段在几百个字段的模板上会占满一轮；模板不可变，查过的路径直接记下。
    _trie: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    _memo: dict[tuple[str, ...], Optional[FieldDef]] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.fields) > MAX_FIELDS:
            raise TemplateError(f"字段超过 {MAX_FIELDS} 个")
        index: dict[tuple[str, ...], FieldDef] = {}
        trie: dict[str, Any] = {}
        for item in self.fields:
            if item.path in index:
                raise TemplateError(f"{_show(item.path)}：重复定义")
            index[item.path] = item
            level = trie
            for depth, part in enumerate(item.path):
                entry = level.setdefault(part, [None, {}])
                if depth == len(item.path) - 1:
                    entry[0] = item
                level = entry[1]
        object.__setattr__(self, "_index", index)
        object.__setattr__(self, "_trie", trie)
        object.__setattr__(self, "_memo", {})

    # -- 查询 ---------------------------------------------------------------

    def match(self, path: Iterable[str]) -> Optional[FieldDef]:
        """这条具体路径对应的字段定义；精确匹配优先，其次通配段最少的那一条（一样少时靠前的段精确的优先）"""
        concrete = tuple(path)
        try:
            return self._memo[concrete]
        except KeyError:
            pass
        found = self._index.get(concrete)
        if found is None and concrete:
            found = self._match_wild(concrete)
        if len(self._memo) >= MAX_MEMO:
            self._memo.clear()
        self._memo[concrete] = found
        return found

    def _match_wild(self, concrete: tuple[str, ...]) -> Optional[FieldDef]:
        best: Optional[FieldDef] = None
        best_wild = len(concrete) + 1
        # (这一层, 已经走了几段, 用了几个通配)；通配先压栈、精确后压栈，精确的先弹出
        stack: list[tuple[dict[str, Any], int, int]] = [(self._trie, 0, 0)]
        while stack:
            level, depth, wild = stack.pop()
            if wild >= best_wild:
                continue
            part = concrete[depth]
            last = depth == len(concrete) - 1
            for key, extra in ((WILDCARD, 1), (part, 0)):
                if extra and part == WILDCARD:
                    continue  # 具体路径里的段本身就是 `*`：只按精确算一次
                entry = level.get(key)
                if entry is None:
                    continue
                if not last:
                    stack.append((entry[1], depth + 1, wild + extra))
                elif entry[0] is not None and wild + extra < best_wild:
                    best, best_wild = entry[0], wild + extra
        return best

    def chain(self, path: Iterable[str]) -> list[Optional[FieldDef]]:
        """从根到这条路径，每一层的字段定义（没有定义的那一层是 `None`）"""
        concrete = tuple(path)
        return [self.match(concrete[: depth + 1]) for depth in range(len(concrete))]

    def inherited(self, path: Iterable[str], flag: str) -> bool:
        """路径上任何一层标了 `flag`（`private` / `hidden` / `readonly`）就算标了"""
        return any(item is not None and getattr(item, flag) for item in self.chain(path))

    def is_private(self, path: Iterable[str], extra_words: Iterable[str] = ()) -> bool:
        """模板标了私密，或者路径里任何一段含私密词（模板自己的词表加 `extra_words`）——失败就关

        模板只认它定义过的路径：可扩展映射后来加的键、开场白比书里多出来的键、列表元素里的键都可能
        没有定义，所以运行时按词表对具体路径再判一次（像 `_` 开头一律隐藏那样）。
        """
        concrete = tuple(str(part) for part in path)
        if self.inherited(concrete, "private"):
            return True
        words = [word.lower() for word in (*self.private_words, *extra_words) if word]
        return any(word in part.lower() for part in concrete for word in words)

    def is_hidden(self, path: Iterable[str]) -> bool:
        return self.inherited(path, "hidden") or any(str(part).startswith("_") for part in path)

    def is_pair_at(self, path: Iterable[str], value: Any) -> bool:
        """这个位置的值按 MVU 二元组 `[值, "说明"]` 处理吗：模板有定义时只看 `pair`，没定义时看形状"""
        if not (isinstance(value, list) and len(value) == 2 and isinstance(value[1], str)):
            return False
        item = self.match(path)
        return item.pair if item is not None else True

    def person_roots(self) -> tuple[str, ...]:
        return tuple(item.path[0] for item in self.fields if item.person)

    def affection_paths(self) -> tuple[FieldDef, ...]:
        return tuple(item for item in self.fields if item.affection is not None)

    def top_level(self) -> tuple[FieldDef, ...]:
        return tuple(item for item in self.fields if len(item.path) == 1)

    # -- 序列化 -------------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "fields": [item.to_json() for item in self.fields],
            "strict_set": self.strict_set,
            "private_words": list(self.private_words),
        }
        if self.theme:
            data["theme"] = self.theme
        return data

    @classmethod
    def from_json(cls, data: Any) -> "Template":
        if not data:
            return cls()
        if not isinstance(data, Mapping) or not isinstance(data.get("fields", []), list):
            raise TemplateError("模板必须是 {fields: [...]} 的形状")
        raw_words = data.get("private_words", [])
        words = (
            tuple(str(word)[:MAX_KEY_CHARS] for word in raw_words if isinstance(word, str) and word.strip())[:100]
            if isinstance(raw_words, list)
            else ()
        )
        theme = data.get("theme", "")
        return cls(
            fields=tuple(FieldDef.from_json(item) for item in data.get("fields", [])),
            strict_set=bool(data.get("strict_set", False)),
            private_words=words,
            theme=theme if isinstance(theme, str) and theme in THEMES else "",
        )

    def with_fields(self, fields: Iterable[FieldDef]) -> "Template":
        return replace(self, fields=tuple(fields))


def _path_of(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise TemplateError("字段路径必须是非空的键列表")
    if len(raw) > MAX_PATH_DEPTH:
        raise TemplateError(f"字段路径超过 {MAX_PATH_DEPTH} 层")
    path: list[str] = []
    for part in raw:
        if not isinstance(part, str) or not part or len(part) > MAX_KEY_CHARS:
            raise TemplateError(f"字段路径里有不合法的键（必须是 1 ~ {MAX_KEY_CHARS} 字的字符串）")
        path.append(part)
    return tuple(path)


def _number_or_none(raw: Any) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _clamp_int(raw: Any, default: int, low: int, high: int) -> int:
    if raw is None or isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _show(path: Iterable[str]) -> str:
    return "/" + "/".join(path)


def show_path(path: Iterable[str]) -> str:
    """给日志与记录用的路径写法（JSON Pointer 的样子，不转义）"""
    return _show(path)


__all__ = [
    "AFFECTION_LINKS",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_LENGTH",
    "HARD_MAX_ITEMS",
    "HARD_MAX_LENGTH",
    "MAX_FIELDS",
    "TYPES",
    "WILDCARD",
    "FieldDef",
    "Template",
    "TemplateError",
    "show_path",
]
