"""初值：从世界书的 `[initvar]` 条目或开场白的 `<initvar>` 块解析，并推导出一份模板

## 解析（正文按恶意输入看）

- 先限长度（2 万字），再剥掉 `<initvar>` 包裹或代码块。**不展开宏**：模板存原文（全局人设的模板各频道
  共用，不能带上某一个频道的宏结果），建频道状态时由 :func:`expand_values` 按那个频道展开字符串值
  （MVU 是先替换宏再解析，所以键名里的宏这里不展开，是已知差异）。
- 解析器抛的任何异常（不合法的日期、超长的整数……）都按「不合法」处理，统一抛 :class:`InitvarError`。
- 先试 JSON，不行再试 YAML。YAML 用安全加载器，并且**拒绝锚点与别名**：`safe_load` 照样会展开
  别名，十几行就能炸出几 GB（billion laughs）。解析后再限节点数与嵌套深度（迭代地数，不递归）。
- 解析器本身是递归的，几千层括号会在深度检查之前撑爆它：`RecursionError` 一律按「不合法」处理。
- 日期之类 YAML 特有的值转成字符串，结果只含 JSON 形状的值。

## 推导模板（照 MVU 0.196 的写法）

- `$meta`（可写在任何对象上）：`extensible` / `recursiveExtensible` → 可扩展映射；`template` → 新键的
  初值模板；`required` → 不许删（`true` 表示这个对象的键都不许删，列表表示列出的那几个）。根上的
  `strictSet` → 二元组也按普通数组写。推导完从树里删掉，与 MVU 的 `generateSchema` 一致。
- 数组里的 `{"$arrayMeta": true, "$meta": {...}}` 元素同理，读完删掉。
- `[值, "说明"]` 二元组记成 `pair` 字段，说明当字段说明。
- 路径上有 `_` 开头的段：默认隐藏、只读（NA 的约定）。路径命中私密词表：默认私密；推导用的词表记进
  模板，运行时对具体路径再按它判一次（模板没定义到的键也照样认）。
- 可扩展映射推一份通配定义（`角色.*.心情`）：对象孩子取全部孩子的键的并集；新键按第一个孩子补齐。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, Optional

import yaml

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.tavern.greeting import extract_initvar_block

from . import tree as state_tree
from .schema import (
    DEFAULT_MAX_ITEMS,
    DEFAULT_MAX_LENGTH,
    HARD_MAX_LENGTH,
    MAX_FIELDS,
    MAX_KEY_CHARS,
    MAX_PATH_DEPTH,
    WILDCARD,
    FieldDef,
    Template,
    TemplateError,
)

logger = get_sub_logger("state_table.initvar")

MAX_INITVAR_CHARS = 20_000
MAX_NODES = 5_000
MAX_DEPTH = 32
MAX_SAFE_INT = 2**53

_FENCE = re.compile(r"^\s{0,16}```[A-Za-z]{0,16}[ \t]{0,16}\n(.*)\n\s{0,16}```\s{0,16}$", re.DOTALL)


class InitvarError(ValueError):
    """初值不合法；消息里只有原因，不带正文"""


class _NoAliasLoader(yaml.SafeLoader):
    """安全加载器，外加：碰到锚点或别名就拒绝"""

    def compose_node(self, parent: Any, index: Any) -> Any:  # type: ignore[override]
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
            raise InitvarError("初值里不能用 YAML 的锚点与别名")
        return super().compose_node(parent, index)


def _check_yaml_nodes(root: Any) -> None:
    stack: list[tuple[Any, int]] = [(root, 1)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES:
            raise InitvarError(f"初值超过 {MAX_NODES} 个节点")
        if depth > MAX_DEPTH:
            raise InitvarError(f"初值嵌套超过 {MAX_DEPTH} 层")
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                stack.append((key, depth + 1))
                stack.append((value, depth + 1))
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                stack.append((value, depth + 1))


def _check_json_shape(data: Any) -> None:
    stack: list[tuple[Any, int]] = [(data, 1)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_NODES:
            raise InitvarError(f"初值超过 {MAX_NODES} 个节点")
        if depth > MAX_DEPTH:
            raise InitvarError(f"初值嵌套超过 {MAX_DEPTH} 层")
        if isinstance(node, dict):
            stack.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            stack.extend((value, depth + 1) for value in node)


def _to_json_value(value: Any) -> Any:
    """YAML 读出来的值 → 只含 JSON 形状；深度已经查过（不超过 MAX_DEPTH），递归是安全的"""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) <= MAX_SAFE_INT:
            return value
        try:
            return float(value)
        except OverflowError as exc:
            raise InitvarError("初值里的数字太大") from exc
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    return str(value)


def _load_yaml(body: str) -> Any:
    loader = _NoAliasLoader(body)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _check_yaml_nodes(node)
        return loader.construct_document(node)
    finally:
        loader.dispose()


def parse_initvar_text(text: str) -> dict[str, Any]:
    """解析一段初值正文（原文，不展开宏），返回状态树（字典）；不合法一律抛 :class:`InitvarError`"""
    if not isinstance(text, str):
        raise InitvarError("初值必须是文本")
    if len(text) > MAX_INITVAR_CHARS:
        raise InitvarError(f"初值超过 {MAX_INITVAR_CHARS} 字")
    body = text.strip()
    block = extract_initvar_block(body)
    if block is not None:
        body = block
    fenced = _FENCE.match(body)
    if fenced is not None:
        body = fenced.group(1)
    if not body.strip():
        raise InitvarError("初值是空的")
    try:
        data = _parse_body(body)
        _check_json_shape(data)
        data = _to_json_value(data)
    except InitvarError:
        raise
    except RecursionError as exc:
        raise InitvarError(f"初值嵌套超过 {MAX_DEPTH} 层") from exc
    except Exception as exc:  # noqa: BLE001 - 不合法的日期、超长的整数之类：解析器抛什么都按「不合法」处理
        raise InitvarError("初值既不是合法的 JSON，也不是合法的 YAML") from exc
    if not isinstance(data, dict):
        raise InitvarError("初值必须是一个对象（键值对）")
    return data


def _parse_body(body: str) -> Any:
    if body.lstrip().startswith(("{", "[")):
        try:
            return json.loads(body)
        except RecursionError:
            raise
        except ValueError:
            pass
    return _load_yaml(body)


def strip_meta(tree: Any) -> Any:
    """原地去掉 `$meta` 键与 `$arrayMeta` 元素（开场白的初值不推导模板，也要去掉）"""
    stack: list[Any] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            node.pop("$meta", None)
            stack.extend(node.values())
        elif isinstance(node, list):
            node[:] = [item for item in node if not (isinstance(item, dict) and item.get("$arrayMeta") is True)]
            stack.extend(node)
    return tree


def expand_values(tree: dict[str, Any], expand: Optional[Callable[[str], str]]) -> dict[str, Any]:
    """按这个频道展开初值里字符串值的宏（键名不展开），返回新树；展开后太大就用原文

    模板存的是未展开的原文（全局人设的模板各频道共用）；建频道状态时才按那个频道展开。
    """
    result = state_tree.copy_tree(tree)
    if expand is None:
        return result
    stack: list[Any] = [result]
    try:
        while stack:
            node = stack.pop()
            items = (
                list(node.items())
                if isinstance(node, dict)
                else list(enumerate(node))
                if isinstance(node, list)
                else []
            )
            for key, value in items:
                if isinstance(value, str) and ("{{" in value or "<" in value):
                    node[key] = expand(value)[:HARD_MAX_LENGTH]
                elif isinstance(value, (dict, list)):
                    stack.append(value)
    except Exception as error:  # noqa: BLE001 - 展开失败就用原文
        logger.warning(f"初值展开宏失败，用原文: error={type(error).__name__}")
        return state_tree.copy_tree(tree)
    if state_tree.tree_bytes(result) > state_tree.MAX_TREE_BYTES:
        return state_tree.copy_tree(tree)
    return result


# ---------------------------------------------------------------------------
# 推导模板
# ---------------------------------------------------------------------------


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "string"


def _is_private(path: Sequence[str], words: Sequence[str]) -> bool:
    lowered = [part.lower() for part in path]
    return any(word and word.lower() in part for word in words for part in lowered)


def _required_keys(meta: dict[str, Any], keys: Sequence[str]) -> set[str]:
    required = meta.get("required")
    if required is True:
        return set(keys)
    if isinstance(required, list):
        return {str(item) for item in required}
    return set()


def _strip_array_meta(items: list[Any]) -> tuple[list[Any], dict[str, Any]]:
    meta: dict[str, Any] = {}
    kept: list[Any] = []
    for item in items:
        if isinstance(item, dict) and item.get("$arrayMeta") is True:
            if isinstance(item.get("$meta"), dict):
                meta.update(item["$meta"])
            continue
        kept.append(item)
    return kept, meta


def derive_template(tree: dict[str, Any], *, private_words: Sequence[str] = ()) -> tuple[Template, dict[str, Any]]:
    """从初值树推导模板；返回 `(模板, 去掉了 $meta 的树)`"""
    cleaned = state_tree.copy_tree(tree)
    dropped = _prune_unstorable(cleaned)
    if dropped:
        logger.warning(
            f"[initvar] 初值里有 {dropped} 个键模板存不下（空键、超过 {MAX_KEY_CHARS} 字或超过 {MAX_PATH_DEPTH} 层），推导时去掉了"
        )
    root_meta = cleaned.pop("$meta", None)
    strict_set = bool(isinstance(root_meta, dict) and root_meta.get("strictSet"))

    fields: dict[tuple[str, ...], FieldDef] = {}
    # (对象节点, 模板路径, 是否在可扩展的祖先下面递归可扩展, 这一层必填的键)
    stack: list[tuple[dict[str, Any], tuple[str, ...], bool, set[str]]] = [
        (cleaned, (), False, _required_keys(root_meta, list(cleaned)) if isinstance(root_meta, dict) else set()),
    ]
    while stack:
        node, template_path, recursive, required = stack.pop()
        for key in list(node):
            value = node[key]
            path = (*template_path, key)
            meta: dict[str, Any] = {}
            if isinstance(value, dict) and isinstance(value.get("$meta"), dict):
                meta = value.pop("$meta")
            elif isinstance(value, dict):
                value.pop("$meta", None)
            if isinstance(value, list):
                value, array_meta = _strip_array_meta(value)
                node[key] = value
                meta = {**array_meta, **meta}
            pair = state_tree.is_pair(value) and not strict_set
            inner = value[0] if pair else value
            # 二元组包着的对象 / 列表也可能带 `$meta` / `$arrayMeta`（不读走就会被当成字段）
            if pair and isinstance(inner, dict):
                inner_meta = inner.pop("$meta", None)
                if isinstance(inner_meta, dict):
                    meta = {**meta, **inner_meta}
            elif pair and isinstance(inner, list):
                inner, inner_array_meta = _strip_array_meta(inner)
                value[0] = inner
                meta = {**meta, **inner_array_meta}
            kind = _type_of(inner)
            extensible = bool(
                meta.get("extensible") or meta.get("recursiveExtensible") or (recursive and kind == "object")
            )
            hidden = any(part.startswith("_") for part in path)
            item = FieldDef(
                path=path,
                type=kind,
                description=value[1][:200] if pair else "",
                pair=pair,
                readonly=hidden,
                private=_is_private(path, private_words),
                hidden=hidden,
                max_length=DEFAULT_MAX_LENGTH,
                max_items=DEFAULT_MAX_ITEMS,
                extensible=extensible and kind == "object",
                template=meta.get("template"),
                required=key in required,
                priority=(1 if kind == "array" else 2 if kind == "object" else 3) if not template_path else 2,
            )
            if path not in fields:
                fields[path] = item
            if len(fields) > MAX_FIELDS:
                raise_too_many()
            if kind != "object":
                continue
            child_recursive = recursive or bool(meta.get("recursiveExtensible"))
            child_required = _required_keys(meta, list(inner))
            if item.extensible and inner:
                # 可扩展映射：孩子推一份通配定义，已有的孩子都走通配。对象孩子取全部孩子的键的并集
                # （只看第一个，后面孩子才有的键就没有定义）；新键按第一个孩子补齐。
                first_key = next(iter(inner))
                first = inner[first_key]
                wildcard_path = (*path, WILDCARD)
                template_value = item.template if item.template is not None else state_tree.copy_tree(first)
                fields[path] = replace(item, template=template_value)
                pair_child = state_tree.is_pair(first) and not strict_set
                children = [_unwrap(child, strict_set) for child in inner.values()]
                for child in children:
                    if isinstance(child, dict):
                        child.pop("$meta", None)
                first_inner = first[0] if pair_child else first
                if isinstance(first_inner, dict):
                    shape = _union_shape([child for child in children if isinstance(child, dict)])
                    fields.setdefault(
                        wildcard_path,
                        FieldDef(
                            path=wildcard_path,
                            type="object",
                            pair=pair_child,
                            description=first[1][:200] if pair_child else "",
                            private=_is_private(wildcard_path, private_words) or item.private,
                            hidden=item.hidden,
                            readonly=item.hidden,
                        ),
                    )
                    stack.append((shape, wildcard_path, child_recursive, set()))
                else:
                    fields.setdefault(
                        wildcard_path,
                        FieldDef(
                            path=wildcard_path,
                            type=_type_of(first[0] if pair_child else first),
                            pair=pair_child,
                            private=item.private,
                            hidden=item.hidden,
                            readonly=item.hidden,
                        ),
                    )
            else:
                stack.append((inner, path, child_recursive, child_required))

    ordered = sorted(fields.values(), key=lambda item: (len(item.path), item.path))
    words = tuple(dict.fromkeys(word.strip() for word in private_words if word and word.strip()))
    strip_meta(cleaned)
    template = Template(fields=tuple(ordered), strict_set=strict_set, private_words=words)
    try:
        # 存下的模板必须读得回来：读不回来的话每一轮都会重新推导、更新永远找不到模板，而且没有任何提示
        Template.from_json(template.to_json())
    except TemplateError as error:
        raise InitvarError(f"推导出的模板存不下: {error}") from error
    return template, cleaned


def _unwrap(value: Any, strict_set: bool) -> Any:
    return value[0] if state_tree.is_pair(value) and not strict_set else value


def _prune_unstorable(tree: dict[str, Any]) -> int:
    """原地去掉模板存不下的键（空键、超过 64 字、超过 16 层），返回去掉了几个

    模板的字段路径有同一套上限（`Template.from_json` 会拒），推导时不先去掉，存下的模板就读不回来。
    """
    dropped = 0
    stack: list[tuple[Any, int]] = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            for key in list(node):
                if not key or len(key) > MAX_KEY_CHARS or depth + 1 > MAX_PATH_DEPTH:
                    del node[key]
                    dropped += 1
                    continue
                stack.append((node[key], depth + 1))
        elif isinstance(node, list):
            if state_tree.is_pair(node):
                stack.append((node[0], depth))  # 二元组不多占一段模板路径
            else:
                stack.extend((child, depth + 1) for child in node)
    return dropped


def _union_shape(children: list[dict[str, Any]]) -> dict[str, Any]:
    """几个对象孩子的键的并集（同名的对象再往下并；值取先出现的）；只用来推导定义，不改原树"""
    merged: dict[str, Any] = {}
    for child in children:
        for key, value in child.items():
            if key == "$meta":
                continue
            if key not in merged:
                merged[key] = state_tree.copy_tree(value)
                if isinstance(merged[key], dict):
                    merged[key].pop("$meta", None)
            elif isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = _union_shape([merged[key], value])
    return merged


def raise_too_many() -> None:
    raise InitvarError(f"初值推导出的字段超过 {MAX_FIELDS} 个")


__all__ = [
    "MAX_DEPTH",
    "MAX_INITVAR_CHARS",
    "MAX_NODES",
    "InitvarError",
    "derive_template",
    "parse_initvar_text",
]
