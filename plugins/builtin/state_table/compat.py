"""兼容解析：把模型回复里的变量更新块（MVU 的写法）读成 JSON Patch——只解析，不执行

语义照 MVU 0.196 的源码（`update_variables.ts` 与 `structured_update.ts`）核过，只认其中参数全是字面量的子集：

- **JSON Patch**：块里 `<JSONPatch>…</JSONPatch>` 的正文，或者块正文本身，是一个 JSON 数组，每项
  `{op, path, value}`；路径是以状态根为起点的 JSON Pointer。`insert` 当 `add`；`move` 一类不认。
- **旧式命令** `_.名字(参数);`：名字后面紧跟 `(`，右括号后面紧跟 `;`（MVU 也是这样认的，不合的不算命令）。
  - `_.set(路径, …, 新值)`：至少两个参数，新值永远取最后一个（中间的旧值不比对）→ `replace`；
    目标是数、新值是能读成数的文字时按数写（MVU 的 `Number(...)`）；
  - `_.add(路径, 数)`：正好两个参数 → `delta`（日期加毫秒不支持）；
  - `_.insert` / `_.assign`：`(路径, 值)` 时，目标是列表就追加，是对象就把值（得是对象）深合并进去，
    不存在就按值新建；`(路径, 键或下标, 值)` → `add` 到那个键或下标；
  - `_.remove` / `_.unset` / `_.delete`：`(路径)` → `remove`；`(路径, 第二个)` 时，目标是列表：数按下标删、
    别的按值删第一个相等的；目标是对象：数按键的顺序删第几个、文字按键名删。
  两参数的 `insert` / `remove` 要看目标的实际类型，所以先读成占位操作，提交前用 :func:`resolve` 对着当前的
  状态换成真正的 patch（对不上的记原因）。
- **参数只收字面量**：双引号字符串按 JSON 读；单引号字符串按 YAML 单引号读（`''` 是一个单引号，反斜杠就是
  反斜杠）；数（有限的）；`true` / `false` / `null`；方括号、花括号按 JSON5 读（单引号、不加引号的键、末尾
  多一个逗号都收）。出现变量、表达式、函数调用、模板字符串、`undefined`、孤立代理字符时，这一条记「不支持……」，
  不应用；读不成的那一条跳到它自己的右括号（与分号）之后再接着扫，字符串里写的 `_.set(...)` 不会被当成命令。
- **路径**：先去掉首尾的空白与反斜杠，整条被一对引号包着的去掉这对引号；点号分段（整段被引号包着的去掉引号），
  `[0]` 是下标，`['键']` 与方括号里别的写法是键；转成 JSON Pointer。和 MVU 一样以状态的根为起点，不去掉开头的
  `stat_data`。
- **遮掉的区域**：`think` / `thinking` / `reasoning` / `analysis` / `analyze` 先遮掉不看（与发送路径取块用的是同一个
  函数 `cleanup_runtime.mask_reasoning_regions`）；块里的 `<JSONPatch>` 按 JSON Patch 读过以后不再当命令扫。

和 MVU 不一样的地方：只看发送路径剥下来的更新块（`UpdateVariable` / `VariableUpdate` / `JSONPatch`），块外裸写的
命令与通用的 `<Update>` 不看；不做宏替换；MVU 的 zod 扩展能在命令解析后改写命令，这里没有。

解析器是手写的单遍扫描，没有会回溯的正则去吃整块：括号嵌套、参数个数、条数都有上限；引号不闭合时这一条作废、
扫描到末尾为止。结果只有 patch 与被拒的记录（操作名、路径、原因），记录里不带值，路径与操作名压成单行。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from nekro_agent.services.tavern.cleanup_runtime import (
    extract_variable_blocks,
    mask_reasoning_regions,
    strip_variable_markup,
)
from nekro_agent.tools.untrusted_content import sanitize_inline_text

MAX_OPS = 64
MAX_REJECTED = 40
MAX_NESTING = 16
MAX_ARGS = 8
MAX_PATH_CHARS = 200
MAX_NAME_CHARS = 32
MAX_ATOM_CHARS = 64
MAX_MERGE_OPS = 64

_PATCH_OPS = {"replace": "replace", "add": "add", "insert": "add", "remove": "remove", "delta": "delta"}
# 命令名照 MVU 的抽取正则：区分大小写，别名先换成同一个
_COMMANDS = {
    "set": "set",
    "add": "add",
    "insert": "insert",
    "assign": "insert",
    "remove": "remove",
    "unset": "remove",
    "delete": "remove",
}
# 占位操作：两参数的 insert / remove 要看目标的实际类型，提交前由 `resolve` 换掉
INSERT_INTO = "_insert"
REMOVE_ITEM = "_remove_item"
_COERCE = "_coerce_number"
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def safe_text(value: Any, limit: int) -> str:
    """记录里的操作名与路径：压成单行、去控制字符与孤立代理字符（否则写进库会失败）"""
    return sanitize_inline_text(str(value or ""), limit)


def has_surrogate(value: Any) -> bool:
    """值（含对象的键、列表的元素）里有没有孤立代理字符；迭代，不怕深嵌套"""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if _SURROGATE.search(node):
                return True
        elif isinstance(node, dict):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


@dataclass
class Parsed:
    """解析结果：能交给校验器的 patch（可能含占位操作），以及在这一步就拒掉的记录（不带值）"""

    ops: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    def reject(self, op: str, path: str, reason: str) -> None:
        if len(self.rejected) < MAX_REJECTED:
            self.rejected.append({"op": safe_text(op, 16), "path": safe_text(path, MAX_PATH_CHARS), "reason": reason})


class _Unsupported(Exception):
    def __init__(self, reason: str, path: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.path = path


def parse_blocks(blocks: Sequence[str]) -> Parsed:
    """把这一轮收到的几块原文读成 patch（按出现顺序）"""
    result = Parsed()
    for block in blocks:
        if len(result.ops) >= MAX_OPS:
            result.reject("", "", f"一轮最多读 {MAX_OPS} 处，后面的不读")
            break
        _parse_block(block, result)
    return result


# 思考与分析区域的遮法与发送路径取块用的是同一个（线性）
mask_thinking = mask_reasoning_regions


def _parse_block(block: str, result: Parsed) -> None:
    text = mask_thinking(block)
    inner = extract_variable_blocks(text)
    for body in inner:
        _parse_json_patch(body, result)
    rest = strip_variable_markup(text) if inner else text
    stripped = rest.strip()
    if stripped.startswith(("[", "{")) and not inner:
        _parse_json_patch(stripped, result)
        return
    _scan_commands(rest, result)


# ---------------------------------------------------------------------------
# JSON Patch
# ---------------------------------------------------------------------------


def _parse_json_patch(body: str, result: Parsed) -> None:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        result.reject("", "", "JSON Patch 解析不了")
        return
    if isinstance(data, dict) and isinstance(data.get("patch"), list):
        data = data["patch"]
    if not isinstance(data, list):
        result.reject("", "", "JSON Patch 必须是列表")
        return
    for item in data:
        if len(result.ops) >= MAX_OPS:
            result.reject("", "", f"一轮最多读 {MAX_OPS} 处，后面的不读")
            return
        if not isinstance(item, dict):
            result.reject("", "", "每一处修改必须是对象")
            continue
        raw_op = str(item.get("op", ""))
        path = item.get("path")
        op = _PATCH_OPS.get(raw_op)
        shown = path if isinstance(path, str) else ""
        if op is None:
            result.reject(raw_op, shown, "不支持的操作")
            continue
        if not isinstance(path, str) or not path.startswith("/"):
            result.reject(raw_op, shown, "路径要是以 / 开头的 JSON Pointer")
            continue
        if path == "/":
            result.reject(raw_op, shown, "不能改整棵树")
            continue
        if has_surrogate(path) or has_surrogate(item.get("value")):
            result.reject(raw_op, shown, "含孤立代理字符")
            continue
        converted: dict[str, Any] = {"op": op, "path": path}
        if "value" in item:
            converted["value"] = item["value"]
        result.ops.append(converted)


# ---------------------------------------------------------------------------
# 旧式命令
# ---------------------------------------------------------------------------


def _scan_commands(text: str, result: Parsed) -> None:
    position = 0
    length = len(text)
    while len(result.ops) < MAX_OPS:
        start = text.find("_.", position)
        if start < 0:
            return
        if start > 0 and (text[start - 1].isalnum() or text[start - 1] in "_$."):
            position = start + 2
            continue
        name_end = start + 2
        while name_end < length and name_end - start - 2 < MAX_NAME_CHARS and _is_name_char(text[name_end]):
            name_end += 1
        name = text[start + 2 : name_end]
        # 名字得是那几个之一、后面紧跟左括号（`_.set (`、`_.Set(` 都不算命令，照 MVU）
        if name not in _COMMANDS or name_end >= length or text[name_end] != "(":
            position = max(name_end, start + 2)
            continue
        lexer = _Lexer(text, name_end + 1)
        try:
            args = lexer.arguments()
            if lexer.position >= length or text[lexer.position] != ";":
                raise _Unsupported("右括号后面要紧跟分号（按 MVU 的写法这不算命令）", _shown_path(args))
            if has_surrogate(args):
                raise _Unsupported("含孤立代理字符", _shown_path(args))
            result.ops.append(_command_to_op(_COMMANDS[name], args))
            position = lexer.position + 1
        except _Unsupported as error:
            result.reject(f"_.{name}", error.path, error.reason)
            # 这一条读不成字面量：跳到它自己的右括号（与紧跟的分号）之后再接着扫，照 MVU 切参数的方式认引号与括号，
            # 不从出错的地方接着扫——否则字符串字面量里写的 `_.set(...)` 会被当成真命令。
            position = _command_end(text, name_end + 1)
    if text.find("_.", position) >= 0:
        result.reject("", "", f"一轮最多读 {MAX_OPS} 处，后面的不读")


def _command_end(text: str, start: int) -> int:
    """从左括号之后扫到与它配对的右括号之后（紧跟的分号一起算）；认三种引号（前一个字符是反斜杠的不算）与三种括号。
    配不上时到末尾。线性。"""
    depth = 1
    quote = ""
    index = start
    length = len(text)
    while index < length:
        char = text[index]
        if quote:
            if char == quote and text[index - 1] != "\\":
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                index += 1
                return index + 1 if index < length and text[index] == ";" else index
        index += 1
    return length


def _shown_path(args: list[Any]) -> str:
    return str(args[0])[:MAX_PATH_CHARS] if args and isinstance(args[0], str) else ""


def _command_to_op(name: str, args: list[Any]) -> dict[str, Any]:
    if not args:
        raise _Unsupported("缺少路径")
    shown = _shown_path(args)
    try:
        pointer = path_to_pointer(args[0])
        if name == "set":
            if len(args) < 2:
                raise _Unsupported("参数个数不对")
            return {"op": "replace", "path": pointer, "value": args[-1], _COERCE: True}
        if name == "add":
            if len(args) != 2:
                raise _Unsupported("参数个数不对")
            if isinstance(args[1], bool) or not isinstance(args[1], (int, float)):
                raise _Unsupported("只认给数字加减")
            return {"op": "delta", "path": pointer, "value": args[1]}
        if name == "insert":
            if len(args) == 2:
                return {"op": INSERT_INTO, "path": pointer, "value": args[1]}
            if len(args) == 3:
                return {"op": "add", "path": f"{pointer}/{_key_segment(args[1])}", "value": args[2]}
            raise _Unsupported("参数个数不对")
        if len(args) == 1:
            return {"op": "remove", "path": pointer}
        if len(args) == 2:
            return {"op": REMOVE_ITEM, "path": pointer, "item": args[1]}
        raise _Unsupported("参数个数不对")
    except _Unsupported as error:
        raise _Unsupported(error.reason, shown) from error


def _key_segment(value: Any) -> str:
    if value == "-":
        return "-"
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _Unsupported("键或下标要是文字或整数")
    if isinstance(value, int) and value < 0:
        raise _Unsupported("下标不能是负数")
    text = str(value)
    if not text:
        raise _Unsupported("键不能为空")
    return _escape(text)


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


_TRIM = " \t\r\n\\"


def _unquote(segment: str) -> str:
    if len(segment) >= 2 and segment[0] == segment[-1] and segment[0] in "'\"`":
        return segment[1:-1]
    return segment


def path_to_pointer(raw: Any) -> str:
    """MVU 的路径写法（`a.b`、`a[0]`、`a['键'].b`、`a."键"`）→ JSON Pointer（以状态的根为起点）

    先去掉首尾的空白与反斜杠；整条路径被同一种引号包着（而且只有这一对）时去掉这对引号。不按字符逐个剥引号：
    `a."键"` 末尾的引号属于最后一段，剥掉就配不上了。
    """
    if not isinstance(raw, str):
        raise _Unsupported("路径要是文字")
    text = raw.strip(_TRIM)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"`" and text.count(text[0]) == 2:
        text = text[1:-1].strip(_TRIM)
    if not text or len(text) > MAX_PATH_CHARS:
        raise _Unsupported("路径为空或太长", raw[:MAX_PATH_CHARS])
    segments: list[str] = []
    current: list[str] = []
    after_bracket = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == ".":
            if current:
                segments.append(_unquote("".join(current)))
                current = []
            elif not after_bracket:
                raise _Unsupported("路径里有空的一段", text)
            after_bracket = False
            index += 1
        elif char == "[":
            if current:
                segments.append(_unquote("".join(current)))
                current = []
            index, segment = _bracket(text, index + 1)
            segments.append(segment)
            after_bracket = True
        else:
            if after_bracket:
                raise _Unsupported("路径写法不对", text)
            current.append(char)
            index += 1
    if current:
        segments.append(_unquote("".join(current)))
    if not segments or any(not segment for segment in segments):
        raise _Unsupported("路径为空或有空的一段", text)
    return "/" + "/".join(_escape(segment) for segment in segments)


def _bracket(text: str, index: int) -> tuple[int, str]:
    """`[` 之后到 `]`：带引号的是键，别的（数字或任意文字）原样当这一段；返回 (`]` 之后的位置, 这一段)"""
    if index < len(text) and text[index] in "'\"":
        quote = text[index]
        end = text.find(quote, index + 1)
        if end < 0 or end + 1 >= len(text) or text[end + 1] != "]":
            raise _Unsupported("路径里的方括号写法不对", text)
        return end + 2, text[index + 1 : end]
    end = text.find("]", index)
    if end <= index:
        raise _Unsupported("路径里的方括号写法不对", text)
    return end + 1, text[index:end]


# ---------------------------------------------------------------------------
# 提交前对着当前状态换掉占位操作
# ---------------------------------------------------------------------------

Lookup = Callable[[str], tuple[bool, Any]]


def resolve(ops: Sequence[dict[str, Any]], lookup: Lookup) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """把两参数的 insert / remove 与 set 的数字转换对着当前状态落成真正的 patch

    `lookup(pointer)` 返回 `(在不在, 当前的值)`（二元组已经取了第 0 项）。按顺序处理，前面的修改不会影响后面
    这一条看到的「当前值」（与校验器一样，都对着提交前的状态）。返回 (patch, 被拒的记录)。
    """
    out: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []

    def reject(op: dict[str, Any], reason: str) -> None:
        rejected.append(
            {"op": safe_text(op.get("op"), 16), "path": safe_text(op.get("path"), MAX_PATH_CHARS), "reason": reason}
        )

    for op in ops:
        kind = op.get("op")
        path = str(op.get("path", ""))
        if kind == INSERT_INTO:
            found, target = lookup(path)
            value = op.get("value")
            if found and isinstance(target, list):
                out.append({"op": "add", "path": f"{path}/-", "value": value})
            elif found and isinstance(target, dict):
                if not isinstance(value, dict):
                    reject(op, "往对象里合并要给对象")
                else:
                    merged = _merge_ops(path, target, value)
                    if len(merged) > MAX_MERGE_OPS:
                        reject(op, "要合并的东西太多")
                    else:
                        out.extend(merged)
            elif found:
                reject(op, "目标不是列表或对象")
            elif isinstance(value, dict):
                out.append({"op": "add", "path": path, "value": value})
            elif isinstance(value, list):
                out.append({"op": "add", "path": path, "value": [value]})
            else:
                reject(op, "目标不存在，值又不是对象或列表")
        elif kind == REMOVE_ITEM:
            found, target = lookup(path)
            segment = _item_segment(target, op.get("item")) if found else None
            if not found:
                reject(op, "要删的位置不存在")
            elif segment is None:
                reject(op, "没有找到要删的那一项")
            else:
                out.append({"op": "remove", "path": f"{path}/{segment}"})
        elif op.get(_COERCE):
            converted = {key: value for key, value in op.items() if key != _COERCE}
            found, target = lookup(path)
            value = converted.get("value")
            if found and _is_number(target) and isinstance(value, str):
                number = _as_number(value)
                if number is None:
                    reject(op, "目标是数字，新值读不成数字")
                    continue
                converted["value"] = number
            out.append(converted)
        else:
            out.append(dict(op))
    return out, rejected


def _merge_ops(path: str, target: dict[str, Any], value: dict[str, Any]) -> list[dict[str, Any]]:
    """深合并（`_.merge`）：两边都是对象的往里走，别的整个写上"""
    ops: list[dict[str, Any]] = []
    for key, child in value.items():
        child_path = f"{path}/{_escape(str(key))}"
        current = target.get(key)
        if isinstance(child, dict) and isinstance(current, dict):
            ops.extend(_merge_ops(child_path, current, child))
        else:
            ops.append({"op": "add", "path": child_path, "value": child})
        if len(ops) > MAX_MERGE_OPS:
            break
    return ops


def _item_segment(target: Any, item: Any) -> Optional[str]:
    """两参数 remove 的第二个参数 → 要删的那一段（列表是下标，对象是键）；找不到时 None"""
    if isinstance(item, str) and len(item) >= 2 and item[0] == item[-1] and item[0] in "'\"":
        item = item[1:-1]
    if isinstance(target, list):
        if isinstance(item, int) and not isinstance(item, bool):
            return str(item) if 0 <= item < len(target) else None
        for index, element in enumerate(target):
            if element == item:
                return str(index)
        return None
    if isinstance(target, dict):
        if isinstance(item, int) and not isinstance(item, bool):
            keys = list(target)
            return _escape(str(keys[item])) if 0 <= item < len(keys) else None
        key = str(item)
        return _escape(key) if key in target else None
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_number(text: str) -> Optional[int | float]:
    try:
        number = float(text.strip())
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() and abs(number) < 2**53 else number


# ---------------------------------------------------------------------------
# 只认字面量的参数读取器
# ---------------------------------------------------------------------------


def _is_name_char(char: str) -> bool:
    return char.isalnum() or char in "_$"


def _skip_spaces(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0", "v": "\v"}


class _Lexer:
    """只认字面量；每个字符最多看一次，出错时 `position` 停在出错的地方"""

    def __init__(self, text: str, position: int) -> None:
        self.text = text
        self.position = position

    def _peek(self) -> str:
        self.position = _skip_spaces(self.text, self.position)
        return self.text[self.position] if self.position < len(self.text) else ""

    def arguments(self) -> list[Any]:
        args: list[Any] = []
        if self._peek() == ")":
            self.position += 1
            return args
        while True:
            if len(args) >= MAX_ARGS:
                raise _Unsupported("参数太多")
            args.append(self.value(0, top=True))
            char = self._peek()
            if char == ",":
                self.position += 1
                continue
            if char == ")":
                self.position += 1
                return args
            if not char:
                raise _Unsupported("括号没闭合")
            raise _Unsupported("不支持的格式（只收字面量）")

    def value(self, depth: int, *, top: bool = False) -> Any:
        if depth > MAX_NESTING:
            raise _Unsupported("嵌套太深")
        char = self._peek()
        if char == "'":
            return self.yaml_single() if top else self.string(char)
        if char == '"':
            return self.string(char)
        if char == "[":
            return self.array(depth + 1)
        if char == "{":
            return self.mapping(depth + 1)
        if char == "-" or char.isdigit():
            return self.number()
        if char == "`":
            raise _Unsupported("不支持模板字符串")
        if char and _is_name_char(char):
            word = self.word()
            if word == "true":
                return True
            if word == "false":
                return False
            if word == "null":
                return None
            if word == "undefined":
                raise _Unsupported("不支持 undefined")
            raise _Unsupported("不支持变量或表达式")
        if not char:
            raise _Unsupported("括号没闭合")
        raise _Unsupported("不支持的格式（只收字面量）")

    def word(self) -> str:
        start = self.position
        while self.position < len(self.text) and _is_name_char(self.text[self.position]):
            self.position += 1
            if self.position - start > MAX_ATOM_CHARS:
                raise _Unsupported("不支持变量或表达式")
        return self.text[start : self.position]

    def yaml_single(self) -> str:
        """顶层的单引号字符串按 YAML 单引号读：`''` 是一个单引号，反斜杠原样；`\\'` 不收尾（MVU 切参数时这样认）"""
        text = self.text
        index = self.position + 1
        out: list[str] = []
        while index < len(text):
            char = text[index]
            if char == "'":
                if index + 1 < len(text) and text[index + 1] == "'":
                    out.append("'")
                    index += 2
                    continue
                if text[index - 1] != "\\" or index == self.position + 1:
                    self.position = index + 1
                    return "".join(out)
            out.append(char)
            index += 1
        self.position = len(text)
        raise _Unsupported("引号没闭合")

    def string(self, quote: str) -> str:
        """双引号（以及数组、对象里的单引号）字符串：JSON / JSON5 的转义"""
        text = self.text
        index = self.position + 1
        out: list[str] = []
        while index < len(text):
            char = text[index]
            if char == quote:
                self.position = index + 1
                return "".join(out)
            if char == "\\":
                if index + 1 >= len(text):
                    break
                escaped = text[index + 1]
                if escaped == "u" and index + 6 <= len(text):
                    digits = text[index + 2 : index + 6]
                    if all(d in "0123456789abcdefABCDEF" for d in digits):
                        out.append(chr(int(digits, 16)))
                        index += 6
                        continue
                out.append(_ESCAPES.get(escaped, escaped))
                index += 2
                continue
            out.append(char)
            index += 1
        self.position = len(text)
        raise _Unsupported("引号没闭合")

    def number(self) -> int | float:
        text = self.text
        start = self.position
        index = start
        if index < len(text) and text[index] == "-":
            index += 1
        digits_start = index
        while index < len(text) and text[index].isdigit():
            index += 1
        if index == digits_start:
            self.position = index
            raise _Unsupported("不支持变量或表达式")
        is_float = False
        if index < len(text) and text[index] == ".":
            index += 1
            fraction_start = index
            while index < len(text) and text[index].isdigit():
                index += 1
            if index == fraction_start:
                self.position = index
                raise _Unsupported("数的写法不对")
            is_float = True
        if index < len(text) and text[index] in "eE":
            index += 1
            if index < len(text) and text[index] in "+-":
                index += 1
            exponent_start = index
            while index < len(text) and text[index].isdigit():
                index += 1
            if index == exponent_start:
                self.position = index
                raise _Unsupported("数的写法不对")
            is_float = True
        self.position = index
        literal = text[start:index]
        if len(literal) > MAX_ATOM_CHARS:
            raise _Unsupported("数太长")
        if index < len(text) and (_is_name_char(text[index]) or text[index] == "."):
            raise _Unsupported("不支持变量或表达式")
        if not is_float:
            return int(literal)
        number = float(literal)
        if not math.isfinite(number):
            raise _Unsupported("数太大")
        return number

    def array(self, depth: int) -> list[Any]:
        self.position += 1
        items: list[Any] = []
        if self._peek() == "]":
            self.position += 1
            return items
        while True:
            items.append(self.value(depth))
            char = self._peek()
            if char == ",":
                self.position += 1
                if self._peek() == "]":
                    self.position += 1
                    return items
                continue
            if char == "]":
                self.position += 1
                return items
            if not char:
                raise _Unsupported("括号没闭合")
            raise _Unsupported("不支持的格式（只收字面量）")

    def mapping(self, depth: int) -> dict[str, Any]:
        self.position += 1
        items: dict[str, Any] = {}
        if self._peek() == "}":
            self.position += 1
            return items
        while True:
            char = self._peek()
            if char in ("'", '"'):
                key = self.string(char)
            elif char and _is_name_char(char):
                key = self.word()
            elif not char:
                raise _Unsupported("括号没闭合")
            else:
                raise _Unsupported("对象的键写法不对")
            if self._peek() != ":":
                raise _Unsupported("对象的键后面要有冒号")
            self.position += 1
            items[key] = self.value(depth)
            char = self._peek()
            if char == ",":
                self.position += 1
                if self._peek() == "}":
                    self.position += 1
                    return items
                continue
            if char == "}":
                self.position += 1
                return items
            if not char:
                raise _Unsupported("括号没闭合")
            raise _Unsupported("不支持的格式（只收字面量）")


__all__ = [
    "INSERT_INTO",
    "MAX_OPS",
    "REMOVE_ITEM",
    "Lookup",
    "Parsed",
    "mask_thinking",
    "parse_blocks",
    "path_to_pointer",
    "resolve",
]
