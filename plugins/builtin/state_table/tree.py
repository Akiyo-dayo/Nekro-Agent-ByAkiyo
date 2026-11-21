"""状态树的读写与 JSON Pointer

状态树是普通的 JSON 形状（字典、列表、字符串、数、布尔、`None`），保留 MVU 的样子：`[值, "说明"]`
这种二元组原样存着，模板里标了 `pair` 的字段只改第 0 项。路径都相对 `stat_data` 的根。
"""

from __future__ import annotations

import json
from typing import Any

MAX_POINTER_DEPTH = 16
MAX_SEGMENT_CHARS = 64
# 整棵树序列化后的上限（字节）：再大就拒绝提交。
MAX_TREE_BYTES = 64 * 1024
MAX_INDEX_DIGITS = 4


class PointerError(ValueError):
    """路径写法不对；消息里不带值"""


def parse_pointer(pointer: Any) -> tuple[str, ...]:
    """JSON Pointer（`/当前时间`、`/角色/阿青/心情`）→ 路径；`~1` 解成 `/`、`~0` 解成 `~`"""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise PointerError("路径必须以 / 开头")
    parts = pointer[1:].split("/")
    if len(parts) > MAX_POINTER_DEPTH:
        raise PointerError(f"路径超过 {MAX_POINTER_DEPTH} 层")
    path: list[str] = []
    for raw in parts:
        part = raw.replace("~1", "/").replace("~0", "~")
        if not part or len(part) > MAX_SEGMENT_CHARS:
            raise PointerError(f"路径里有空的或超过 {MAX_SEGMENT_CHARS} 字的段")
        path.append(part)
    return tuple(path)


def is_pair(value: Any) -> bool:
    """MVU 的 `[值, "说明"]`"""
    return isinstance(value, list) and len(value) == 2 and isinstance(value[1], str)


def _index_of(part: str, size: int, *, allow_end: bool = False) -> int | None:
    if allow_end and part == "-":
        return size
    if not (part.isascii() and part.isdigit()) or len(part) > MAX_INDEX_DIGITS:
        return None
    index = int(part)
    limit = size + 1 if allow_end else size
    return index if index < limit else None


def get(tree: Any, path: tuple[str, ...]) -> tuple[bool, Any]:
    """`(找到了没有, 值)`"""
    node = tree
    for part in path:
        if isinstance(node, dict):
            if part not in node:
                return False, None
            node = node[part]
        elif isinstance(node, list):
            index = _index_of(part, len(node))
            if index is None:
                return False, None
            node = node[index]
        else:
            return False, None
    return True, node


def parent_of(tree: Any, path: tuple[str, ...]) -> tuple[bool, Any]:
    if not path:
        return False, None
    return get(tree, path[:-1])


def set_existing(tree: Any, path: tuple[str, ...], value: Any) -> bool:
    """改一个已经存在的位置；不存在返回 False"""
    found, parent = parent_of(tree, path)
    if not found:
        return False
    key = path[-1]
    if isinstance(parent, dict):
        if key not in parent:
            return False
        parent[key] = value
        return True
    if isinstance(parent, list):
        index = _index_of(key, len(parent))
        if index is None:
            return False
        parent[index] = value
        return True
    return False


def add(tree: Any, path: tuple[str, ...], value: Any) -> bool:
    """映射里加键（已有就覆盖），数组里按下标插入或用 `-` 追加"""
    found, parent = parent_of(tree, path)
    if not found:
        return False
    key = path[-1]
    if isinstance(parent, dict):
        parent[key] = value
        return True
    if isinstance(parent, list):
        index = _index_of(key, len(parent), allow_end=True)
        if index is None:
            return False
        parent.insert(index, value)
        return True
    return False


def remove(tree: Any, path: tuple[str, ...]) -> bool:
    found, parent = parent_of(tree, path)
    if not found:
        return False
    key = path[-1]
    if isinstance(parent, dict):
        if key not in parent:
            return False
        del parent[key]
        return True
    if isinstance(parent, list):
        index = _index_of(key, len(parent))
        if index is None:
            return False
        del parent[index]
        return True
    return False


def copy_tree(tree: Any) -> Any:
    """深拷贝（经 JSON 来回一次：顺带确认只有 JSON 形状的值）"""
    return json.loads(json.dumps(tree, ensure_ascii=False))


def tree_bytes(tree: Any) -> int:
    return len(json.dumps(tree, ensure_ascii=False).encode("utf-8"))


__all__ = [
    "MAX_TREE_BYTES",
    "PointerError",
    "add",
    "copy_tree",
    "get",
    "is_pair",
    "parse_pointer",
    "remove",
    "set_existing",
    "tree_bytes",
]
