"""宿主校验器：模型给出的 JSON Patch 只有过了这里才落进状态

这是状态的安全边界：更新调用的模型看得到群聊原文，群友说什么它都可能照做，所以「能改什么、
改成什么样」只由这里决定，不由模型决定。

- **路径**：JSON Pointer，以 `stat_data` 为根（`/当前时间`）。群聊里个人档案写在
  `/在场的人/<编号>/<个人范围的字段>/…`（编号由调用方给出，不暴露平台账号）；私聊里直接写模板原来的
  路径，对应对方。
- **权限**（每一层、每一个子字段都判）：隐藏（`_` 开头或模板标了）、只读、映射到好感（好感系统开着时）的
  一律不能写；私密的（模板标了，或者路径里任何一段含私密词——模板没定义的键也照样判）只在私聊里能写。
- **整体写对象**：按模板逐个子字段递归校验（权限、类型、范围），模板里没有的键拒掉（可扩展映射下的新键
  除外，按新键模板补齐），原来有的必填键新值里也得有。**整体替换或删除**一个现有值里有受保护子孙（只读、
  隐藏、好感映射、群聊里的私密）的对象或列表一律拒，二元组包着的对象也一样——否则改父对象就能绕过子字段
  的全部权限。列表元素只拦真有受保护值的那个元素。
- **操作**：`replace` / `add` / `remove` / `delta`（只认数；目标是二元组时加到第 0 项；结果必须是有限的数）。
- **值**：数按最小、最大值截断；字符串先去控制字符、零宽字符与标签，再过记忆前过滤，超过长度上限的拒；
  新键名限长度与字符，也过记忆前过滤；数组条数有上限；整棵树序列化后不超过 64 KB；一次最多改 `max_ops` 处。
- **二元组** `[值, "说明"]`：只认模板标了 `pair` 的（没定义的看形状）。标了的字段给二元组或裸值都收，
  存成二元组，说明用现有的（没有就用模板的），模型给的说明不收。路径穿过二元组包着的对象时落到第 0 项
  （`/档案/名字` 写进 `[{名字…}, "说明"]`）；二元组包着的列表只能整体写。
- **记录**：应用了的与被拒的都只记路径、操作与原因，不记值。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from nekro_agent.tools.untrusted_content import sanitize_inline_text

from . import tree as state_tree
from .schema import WILDCARD, FieldDef, Template

PRESENT_KEY = "在场的人"
OPS = ("replace", "add", "remove", "delta")
MAX_NEW_KEY_CHARS = 32
MAX_VALUE_NODES = 200
MAX_VALUE_DEPTH = 8
_TAG = re.compile(r"</?[A-Za-z][^<>]{0,64}>")


def _shown(value: Any, limit: int) -> str:
    """被拒记录里的路径与操作名：压成单行、去控制字符与孤立代理字符（否则这一行写不进库）"""
    return sanitize_inline_text(str(value or ""), limit)


# 新键名不收的字符：控制字符、尖括号与花括号、零宽与方向控制符、孤立代理字符（编码成 UTF-8 会失败，写不进库）。
# 文字值走 sanitize_inline_text，那里已经去掉了这些。
_BAD_KEY = re.compile("[\x00-\x1f\x7f<>{}\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ud800-\udfff]")


@dataclass(frozen=True)
class Policy:
    """一次校验的上下文

    Attributes:
        is_private_chat: 私聊频道；私密路径只在这时可写。
        affection_enabled: 好感系统开着；映射到好感的路径只读。
        max_ops: 一次最多应用几处。
        aliases: 群聊里 `在场的人` 下的编号 → 人的标识。
        counterpart: 私聊里对方的标识；个人范围的路径写给他。
        memory_filter_hit: 文本命中记忆前过滤时返回真（命中就拒）。
        private_words: 运行时额外的私密词（实例层的词表，只加严）。
        person_writes: 能不能写个人档案；为假时个人范围的路径（含 `在场的人/…`）一律拒（群聊里的兼容解析：
            卡片规则写的是单主角路径，判不出说的是谁）。
    """

    is_private_chat: bool
    affection_enabled: bool
    max_ops: int = 20
    aliases: Mapping[str, str] = field(default_factory=dict)
    counterpart: str = ""
    memory_filter_hit: Optional[Callable[[str], bool]] = None
    private_words: tuple[str, ...] = ()
    person_writes: bool = True


@dataclass
class Outcome:
    """校验并应用之后的结果（树都是新的副本，调用方再决定提交不提交）"""

    channel: dict[str, Any]
    people: dict[str, dict[str, Any]]
    applied: list[dict[str, str]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    @property
    def changed_people(self) -> set[str]:
        return {item["target"] for item in self.applied if item.get("target", "channel") != "channel"}


class _Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Checker:
    def __init__(self, template: Template, policy: Policy) -> None:
        self.template = template
        self.policy = policy

    # -- 权限 ---------------------------------------------------------------

    def private(self, path: tuple[str, ...]) -> bool:
        return self.template.is_private(path, self.policy.private_words)

    def permission(self, path: tuple[str, ...]) -> None:
        if self.template.is_hidden(path):
            raise _Reject("隐藏字段不能改")
        if self.template.inherited(path, "readonly"):
            raise _Reject("只读字段不能改")
        if self.policy.affection_enabled and any(
            item is not None and item.affection for item in self.template.chain(path)
        ):
            raise _Reject("好感由好感系统管，这里只读")
        if self.private(path) and not self.policy.is_private_chat:
            raise _Reject("私密字段只在私聊里改")

    def protected(self, path: tuple[str, ...]) -> bool:
        """这一处现有的值能不能被连带改掉：隐藏、只读、好感映射（好感系统开着时）、群聊里的私密"""
        if self.template.is_hidden(path) or self.template.inherited(path, "readonly"):
            return True
        if self.policy.affection_enabled and any(
            item is not None and item.affection for item in self.template.chain(path)
        ):
            return True
        return not self.policy.is_private_chat and self.private(path)

    def protected_under(self, path: tuple[str, ...], current: Any) -> bool:
        """现有的值下面有没有受保护的子孙（只看真有值的地方：模板里定义了、值里没有的不算）

        对象的键、列表的元素、二元组包着的值都往里走。必填不在这里判：整体写对象时由 :meth:`value`
        要求新值里留着原来有的必填键；删掉整个上级或整个列表元素不算删了必填键。
        """
        stack: list[tuple[Any, tuple[str, ...]]] = [(current, path)]
        while stack:
            node, node_path = stack.pop()
            if isinstance(node, dict):
                children = [(child, (*node_path, str(key))) for key, child in node.items()]
            elif isinstance(node, list):
                if self.template.is_pair_at(node_path, node):
                    stack.append((node[0], node_path))
                    continue
                children = [(child, (*node_path, str(index))) for index, child in enumerate(node)]
            else:
                continue
            for child, child_path in children:
                if self.protected(child_path):
                    return True
                stack.append((child, child_path))
        return False

    # -- 值 -----------------------------------------------------------------

    def text(self, value: str, limit: int) -> str:
        cleaned = sanitize_inline_text(value, limit + 1)
        cleaned = _TAG.sub("", cleaned).strip()
        if len(cleaned) > limit:
            raise _Reject(f"超过 {limit} 字")
        if self.policy.memory_filter_hit is not None and cleaned and self.policy.memory_filter_hit(cleaned):
            raise _Reject("命中记忆前过滤")
        return cleaned

    def key(self, key: str) -> None:
        if not key or len(key) > MAX_NEW_KEY_CHARS or _BAD_KEY.search(key) or key.startswith(("_", "$")):
            raise _Reject(f"新键名不合法（1 ~ {MAX_NEW_KEY_CHARS} 字，不能含换行、尖括号、花括号，不能以 _ 或 $ 开头）")
        if self.policy.memory_filter_hit is not None and self.policy.memory_filter_hit(key):
            raise _Reject("新键名命中记忆前过滤")

    def value(
        self,
        value: Any,
        path: tuple[str, ...],
        item: Optional[FieldDef],
        *,
        budget: list[int],
        current: Any = None,
        keep_required: bool = True,
        depth: int = 0,
    ) -> Any:
        """按模板递归校验一个值（每个子字段都判权限），返回清洗后的副本

        模板标了 `pair` 的字段两种写法都收：给二元组只校验第 0 项，给裸值就包回二元组；说明一律用现有的
        （`current` 是二元组时），没有就用模板的，模型给的说明不收。`keep_required` 为真时，`current`
        里有的必填键新值里也得有（整体写对象不能顺手把必填键删掉）。
        """
        budget[0] -= 1
        if budget[0] < 0 or depth > MAX_VALUE_DEPTH:
            raise _Reject("值太大或嵌套太深")
        if item is not None and item.pair:
            note = current[1] if _pair_shape(current, item) else item.description
            inner = value[0] if _pair_shape(value, item) else value
            inner_current = current[0] if _pair_shape(current, item) else current
            plain = self._plain(inner, path, item, budget, inner_current, keep_required, depth)
            return [plain, note]
        return self._plain(value, path, item, budget, current, keep_required, depth)

    def _plain(
        self,
        value: Any,
        path: tuple[str, ...],
        item: Optional[FieldDef],
        budget: list[int],
        current: Any,
        keep_required: bool,
        depth: int,
    ) -> Any:
        if item is not None and item.type in ("number", "boolean", "string"):
            return self.scalar(value, item)
        if isinstance(value, dict):
            if item is not None and item.type != "object":
                raise _Reject("应为对象")
            existing = current if isinstance(current, dict) else {}
            result: dict[str, Any] = {}
            for raw_key, child in value.items():
                child_key = str(raw_key)
                child_path = (*path, child_key)
                child_def = self.template.match(child_path)
                if child_def is None and item is not None and not item.extensible:
                    raise _Reject("对象里有模板没有的字段")
                if child_def is None or item is None:
                    self.key(child_key)
                self.permission(child_path)
                result[child_key] = self.value(
                    child,
                    child_path,
                    child_def,
                    budget=budget,
                    current=existing.get(child_key),
                    keep_required=keep_required,
                    depth=depth + 1,
                )
            if keep_required:
                for key in existing:
                    required = self.template.match((*path, str(key)))
                    if required is not None and required.required and key not in result:
                        raise _Reject("必填字段不能省掉")
            return result
        if isinstance(value, list):
            if item is not None and item.type not in ("array",):
                raise _Reject("应为列表")
            limit = item.max_items if item is not None else 20
            if len(value) > limit:
                raise _Reject(f"超过 {limit} 条")
            existing_items = current if isinstance(current, list) else []
            elements: list[Any] = []
            for index, child in enumerate(value):
                child_path = (*path, str(index))
                if self.private(child_path) and not self.policy.is_private_chat:
                    raise _Reject("私密字段只在私聊里改")
                elements.append(
                    self.value(
                        child,
                        child_path,
                        self.template.match(child_path),
                        budget=budget,
                        current=existing_items[index] if index < len(existing_items) else None,
                        keep_required=False,
                        depth=depth + 1,
                    )
                )
            return elements
        if item is not None and item.type in ("object", "array"):
            raise _Reject("应为对象" if item.type == "object" else "应为列表")
        return self.free_scalar(value)

    def scalar(self, value: Any, item: FieldDef) -> Any:
        if item.type == "number":
            return _tidy_number(_number(value), item)
        if item.type == "boolean":
            if not isinstance(value, bool):
                raise _Reject("应为真假值")
            return value
        if not isinstance(value, str):
            raise _Reject("应为文字")
        return self.text(value, item.max_length)

    def free_scalar(self, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return _tidy_number(_number(value), None)
        if isinstance(value, str):
            return self.text(value, 200)
        raise _Reject("值里有不认识的类型")


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Reject("应为数字")
    try:
        number = float(value)
    except OverflowError as exc:
        raise _Reject("数字太大") from exc
    if not math.isfinite(number):
        raise _Reject("应为有限的数字")
    return number


def _tidy_number(value: float, item: Optional[FieldDef]) -> int | float:
    if item is not None and item.minimum is not None:
        value = max(item.minimum, value)
    if item is not None and item.maximum is not None:
        value = min(item.maximum, value)
    value = float(f"{value:.12g}")
    if not math.isfinite(value):
        raise _Reject("结果不是有限的数字")
    return int(value) if value.is_integer() and abs(value) < 2**53 else value


def _pair_shape(value: Any, item: FieldDef) -> bool:
    """`value` 是不是这个字段的二元组写法 `[值, "说明"]`（列表字段要求第 0 项本身是列表）"""
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[1], str)
        and (item.type != "array" or isinstance(value[0], list))
    )


def _with_fill(cleaned: Any, fill: Any, item: FieldDef) -> Any:
    """可扩展映射的新键：模型给的值盖在新键模板上（模板里有、模型没给的照模板补上）"""
    if item.type != "object":
        return cleaned
    inner = cleaned[0] if item.pair else cleaned
    base = fill[0] if _pair_shape(fill, item) else fill
    merged = state_tree.copy_tree(base) if isinstance(base, dict) else {}
    merged.update(inner)
    return [merged, cleaned[1]] if item.pair else merged


@dataclass
class _Target:
    name: str  # "channel" 或人的标识
    tree: dict[str, Any]
    path: tuple[str, ...]  # 模板路径（模型写的路径）
    shown: str
    tree_path: tuple[str, ...] = ()  # 在目标树里的实际路径：穿过二元组包着的对象时多一段 "0"


def _tree_path(template: Template, tree: Any, path: tuple[str, ...]) -> tuple[str, ...]:
    """模板路径 → 树里的实际路径：途经的、模板标了二元组的对象 `[{…}, "说明"]` 落到它的第 0 项

    这样 `/档案/名字` 就能直接写进二元组包着的对象里（只有包着对象时才这样；包着列表的二元组仍然只能
    整体写，说明 `[1]` 也写不到）。
    """
    node: Any = tree
    out: list[str] = []
    for depth, part in enumerate(path):
        if depth and isinstance(node, list) and template.is_pair_at(path[:depth], node) and isinstance(node[0], dict):
            out.append("0")
            node = node[0]
        out.append(part)
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            node = None
    return tuple(out)


def _route(path: tuple[str, ...], raw: str, template: Template, policy: Policy, outcome: Outcome) -> _Target:
    roots = set(template.person_roots())
    if not policy.person_writes and (path[0] == PRESENT_KEY or path[0] in roots):
        raise _Reject("这里不能改个人档案")
    if path[0] == PRESENT_KEY:
        if policy.is_private_chat:
            raise _Reject("私聊里个人档案直接写在原来的路径上")
        if len(path) < 3 or path[1] not in policy.aliases:
            raise _Reject("在场的人里没有这个编号")
        uid = policy.aliases[path[1]]
        inner = path[2:]
        if inner[0] not in roots:
            raise _Reject("在场的人下只能写个人范围的字段")
        return _Target(uid, outcome.people.setdefault(uid, {}), inner, raw)
    if path[0] in roots:
        if not policy.is_private_chat or not policy.counterpart:
            raise _Reject("群聊里个人档案要写在 /在场的人/<编号>/ 下")
        uid = policy.counterpart
        return _Target(uid, outcome.people.setdefault(uid, {}), path, raw)
    return _Target("channel", outcome.channel, path, raw)


def _ensure_parents(checker: _Checker, target: _Target) -> None:
    """路径上缺的、父级是可扩展映射的那几层，按模板补齐（途经二元组包着的对象时落到第 0 项）"""
    node: Any = target.tree
    for depth, part in enumerate(target.path[:-1]):
        if (
            depth
            and isinstance(node, list)
            and checker.template.is_pair_at(target.path[:depth], node)
            and isinstance(node[0], dict)
        ):
            node = node[0]
        if isinstance(node, dict) and part not in node:
            parent_def = checker.template.match(target.path[:depth]) if depth else None
            if parent_def is None or not parent_def.extensible:
                raise _Reject("路径不存在，且上一层不是可扩展的映射")
            checker.key(part)
            fill = parent_def.template
            wrapped = (
                isinstance(fill, list) and len(fill) == 2 and isinstance(fill[0], dict) and isinstance(fill[1], str)
            )
            node[part] = state_tree.copy_tree(fill) if isinstance(fill, dict) or wrapped else {}
        node = node[part] if isinstance(node, dict) else None
        if not isinstance(node, (dict, list)):
            raise _Reject("路径穿过了不是对象的值")


def _apply_one(raw_op: Any, checker: _Checker, outcome: Outcome) -> dict[str, str]:
    template, policy = checker.template, checker.policy
    if not isinstance(raw_op, Mapping):
        raise _Reject("每一处修改必须是对象")
    op = raw_op.get("op")
    if op not in OPS:
        raise _Reject("只认 replace / add / remove / delta")
    try:
        path = state_tree.parse_pointer(raw_op.get("path"))
    except state_tree.PointerError as exc:
        raise _Reject(str(exc)) from exc
    target = _route(path, str(raw_op.get("path")), template, policy, outcome)
    if not target.path:
        raise _Reject("不能改整棵树")
    target.tree_path = _tree_path(template, target.tree, target.path)
    done = {"op": op, "path": target.shown, "target": target.name}

    parent_found, parent = state_tree.parent_of(target.tree, target.tree_path)
    parent_path = target.path[:-1]
    parent_def = template.match(parent_path) if parent_path else None
    if parent_found and isinstance(parent, list):
        return _apply_array_element(op, raw_op, checker, target, parent, parent_def, done)

    exists, current = state_tree.get(target.tree, target.tree_path)
    item = template.match(target.path)
    # 可扩展映射下树里还没有的键（模板里可能经通配匹配到了）：按新键处理，从映射的新键模板补齐。
    new_key = op in ("replace", "add") and not exists and parent_def is not None and parent_def.extensible
    if item is None and new_key:
        item = template.match((*parent_path, WILDCARD))
    if item is None:
        raise _Reject("模板里没有这个字段")
    checker.permission(target.path)

    if op == "remove":
        if item.required:
            raise _Reject("必填字段不能删")
        if checker.protected_under(target.path, current):
            raise _Reject("下面有受保护的字段，不能整个删掉")
        if not state_tree.remove(target.tree, target.tree_path):
            raise _Reject("要删的位置不存在")
        return done

    if "value" not in raw_op:
        raise _Reject("缺少 value")
    value = raw_op["value"]

    if op == "delta":
        if not exists:
            raise _Reject("要累加的位置不存在")
        pair = item.pair and _pair_shape(current, item)
        base = current[0] if pair else current
        result = _tidy_number(_number(base) + _number(value), item)
        state_tree.set_existing(target.tree, target.tree_path, [result, current[1]] if pair else result)
        return done

    budget = [MAX_VALUE_NODES]
    if new_key:
        checker.key(target.path[-1])
        fill = parent_def.template if parent_def is not None else None
        cleaned = checker.value(value, target.path, item, budget=budget, current=fill, keep_required=False)
        value = _with_fill(cleaned, fill, item)
    else:
        # 二元组包着的对象也一样：保护判断会走进第 0 项（整体替换是绕过子字段保护最直接的写法）
        if exists and isinstance(current, (dict, list)) and checker.protected_under(target.path, current):
            raise _Reject("下面有受保护的字段，不能整体替换")
        value = checker.value(value, target.path, item, budget=budget, current=current if exists else None)

    if not exists:
        _ensure_parents(checker, target)
        target.tree_path = _tree_path(template, target.tree, target.path)  # 补出来的上级可能是二元组
    if op == "replace" and exists:
        state_tree.set_existing(target.tree, target.tree_path, value)
    elif not state_tree.add(target.tree, target.tree_path, value):
        raise _Reject("写不进去（上一层不存在或不是对象）")
    return done


def _apply_array_element(
    op: str,
    raw_op: Mapping[str, Any],
    checker: _Checker,
    target: _Target,
    array: list[Any],
    array_def: Optional[FieldDef],
    done: dict[str, str],
) -> dict[str, str]:
    array_path = target.path[:-1]
    if array_def is None or array_def.type != "array":
        raise _Reject("模板里没有这个列表")
    found_container, container = state_tree.get(target.tree, target.tree_path[:-1])
    if found_container and checker.template.is_pair_at(array_path, container):
        raise _Reject("二元组包着的列表请整体替换")
    checker.permission(array_path)
    key = target.path[-1]
    element_path = target.path
    exists, current = state_tree.get(target.tree, target.tree_path)
    # 只拦真有受保护的值的那个元素：模板里定义了私密子路径（事件/*/秘密），干净的元素照样能删能换
    if op in ("remove", "replace") and exists:
        if checker.protected(element_path) or checker.protected_under(element_path, current):
            raise _Reject("这个元素里有受保护的字段")
    if op == "remove":
        if not state_tree.remove(target.tree, target.tree_path):
            raise _Reject("要删的位置不存在")
        return done
    if op == "delta":
        raise _Reject("数组元素不支持 delta")
    if "value" not in raw_op:
        raise _Reject("缺少 value")
    shape_path = (*array_path, "0")
    element = checker.value(raw_op["value"], shape_path, checker.template.match(shape_path), budget=[MAX_VALUE_NODES])
    if op == "add":
        if len(array) >= array_def.max_items:
            raise _Reject(f"超过 {array_def.max_items} 条")
        if not state_tree.add(target.tree, target.tree_path, element):
            raise _Reject("下标不对")
    elif key == "-" or not state_tree.set_existing(target.tree, target.tree_path, element):
        raise _Reject("要改的位置不存在")
    return done


def apply_patch(
    template: Template,
    channel: dict[str, Any],
    people: Mapping[str, dict[str, Any]],
    patch: Any,
    policy: Policy,
) -> Outcome:
    """在副本上逐处校验并应用；被拒的那一处不影响别的处"""
    checker = _Checker(template, policy)
    outcome = Outcome(
        channel=state_tree.copy_tree(channel),
        people={uid: state_tree.copy_tree(tree) for uid, tree in people.items()},
    )
    if not isinstance(patch, list):
        outcome.rejected.append({"op": "", "path": "", "reason": "patch 必须是列表"})
        return outcome
    for index, raw_op in enumerate(patch):
        shown_path = _shown(raw_op.get("path", ""), 200) if isinstance(raw_op, Mapping) else ""
        shown_op = _shown(raw_op.get("op", ""), 16) if isinstance(raw_op, Mapping) else ""
        if index >= policy.max_ops:
            outcome.rejected.append({"op": shown_op, "path": shown_path, "reason": f"超过一次最多 {policy.max_ops} 处"})
            continue
        snapshot_channel = state_tree.copy_tree(outcome.channel)
        snapshot_people = {uid: state_tree.copy_tree(tree) for uid, tree in outcome.people.items()}
        try:
            applied = _apply_one(raw_op, checker, outcome)
            if state_tree.tree_bytes(outcome.channel) > state_tree.MAX_TREE_BYTES or any(
                state_tree.tree_bytes(tree) > state_tree.MAX_TREE_BYTES for tree in outcome.people.values()
            ):
                raise _Reject("状态太大了")
        except _Reject as reject:
            outcome.channel = snapshot_channel
            outcome.people = snapshot_people
            outcome.rejected.append({"op": shown_op, "path": shown_path, "reason": reject.reason})
            continue
        except Exception as error:  # noqa: BLE001 - 一处的意外（比如值里有序列化不了的字符）只算这一处被拒，不打断整轮
            outcome.channel = snapshot_channel
            outcome.people = snapshot_people
            outcome.rejected.append(
                {"op": shown_op, "path": shown_path, "reason": f"这一处出错（{type(error).__name__}）"}
            )
            continue
        outcome.applied.append(applied)
    return outcome


__all__ = ["OPS", "PRESENT_KEY", "Outcome", "Policy", "apply_patch"]
