"""酒馆世界书格式的导入与导出

酒馆的世界书文件是一个 JSON 对象，`entries` 下按 `uid` 挂条目，条目字段由
`newWorldInfoEntryDefinition` 定义（四十来个，绝大多数本插件用不上）。本模块只做两件事：
把文件读成「书级附加字段 + 条目原始对象」，以及条目原始对象与 `wb_entry` 列之间的双向映射。

## 为什么整份条目留在 `ext`

酒馆的字段远多于本插件要用的那十几个，而且它还在加新字段。只存映射过的列，导出时就会
把没映射的字段全部丢掉——一份「导入再导出」之后内容变少的文件，是用户最不能接受的
数据损失。因此导入时把整份原始条目存进 `ext`，导出时以它为底，只覆盖本插件真正改过的
字段。

## 往返规则：没改过的字段原样保留

导出时对每个映射字段做一次判断：把「原始条目按导入规则再映射一遍」得到的结果与当前
列值比较，**相同就说明这一列没有被编辑过**，保留原始值；不同才按列值写酒馆形态。

这条通用规则比设计稿里只对 `position` 做例外更彻底，也解决了另外几类会丢信息的情况：
超长内容被截断、关键词被截断、越界数值被夹、非字符串关键词被丢弃、`outlet` 位置被降级。
只要用户没动过这些字段，导出就能逐字还原原文件；一旦改过，就以列值为准。

## 上限：完整导入，超了就整份拒绝

32 MiB / 5 万条 / 32 层 JSON 嵌套，加上 `types.py` 里每条的关键词数、关键词长度、正文与
标题字数：这些都是「挡住一份把进程拖死的畸形文件」的安全上限，不是功能限制，取得足够宽，
真实的大世界书（几 MB、几千条、每条上百个关键词）碰不到。

**导入不截断。** 任何一条超了安全上限，:func:`parse_world_file` 就整份拒绝，原因里写清是
哪一条、哪个字段、多长；一条都不写进库。截掉半条设定比导入失败更糟：管理员往往要到 Bot
说出怪话才发现少了字。映射函数里保留的截断只是读库里旧数据（导出、往返比较）时的兜底，
导入路径走不到那里。

内存与耗时按现编的文件量过（Windows 开发机，Python 3.11）：5 MiB / 3000 条 / 每条 100 个
关键词，解析 0.25 s、映射加深拷贝 0.5 s、Python 侧内存峰值约 37 MiB；20 MiB / 12000 条是
1.1 s、2.3 s、约 150 MiB。按这个斜率，32 MiB 的峰值在 250 MiB 上下，是一次性的。

嵌套用迭代遍历计深度而不是递归：递归版在恶意输入上会先把栈撑爆，那时连「拒绝」都做不到。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional

from .name_vars import rewrite_name_vars
from .safe_regex import validate_regex_key

if TYPE_CHECKING:
    from .budget import WorldbookBudget
from .types import (
    DEPTH_MAX,
    DEPTH_MIN,
    MAX_COMMENT_CHARS,
    MAX_CONTENT_CHARS,
    MAX_KEY_LENGTH,
    MAX_KEYS,
    ORDER_MAX,
    ORDER_MIN,
    SCAN_DEPTH_MAX,
    SCAN_DEPTH_MIN,
    TIMED_MAX,
    Position,
    SelectiveLogic,
)

MAX_FILE_BYTES = 32 * 1024 * 1024
# 导入接口读上传文件时用的同一个上限。给它一个与语义对应的名字，而不是在路由里再写一遍
# 字节数：两处数字一旦分叉，就会出现「界面说能传 X、解析器却按另一个值拒绝」这种只有
# 边界上才复现的差异。
IMPORT_MAX_BYTES = MAX_FILE_BYTES
MAX_ENTRIES = 50_000
MAX_JSON_DEPTH = 32
# 条目编号能存下的范围（`wb_entry.uid` 是 32 位整数列）
UID_MIN = -(2**31)
UID_MAX = 2**31 - 1

# 条目字段的默认值，逐个照抄酒馆 `newWorldInfoEntryDefinition` 的 `default`（2026-02 核对，
# 源文件 `public/scripts/world-info.js` 的 `newWorldInfoEntryDefinition`）。`uid` 不在里面：
# 它是每条自己的编号，不是「新条目模板」的一部分（酒馆的模板由这个定义过滤掉
# `excludeFromTemplate` 的键得来，再加一个 `uid`）。
ST_ENTRY_DEFAULTS: dict[str, Any] = {
    "key": [],
    "keysecondary": [],
    "comment": "",
    "content": "",
    "constant": False,
    "vectorized": False,
    "selective": True,
    "selectiveLogic": 0,
    "addMemo": False,
    "order": 100,
    "position": 0,
    "disable": False,
    "ignoreBudget": False,
    "excludeRecursion": False,
    "preventRecursion": False,
    "matchPersonaDescription": False,
    "matchCharacterDescription": False,
    "matchCharacterPersonality": False,
    "matchCharacterDepthPrompt": False,
    "matchScenario": False,
    "matchCreatorNotes": False,
    "delayUntilRecursion": 0,
    "probability": 100,
    "useProbability": True,
    "depth": 4,
    "outletName": "",
    "group": "",
    "groupOverride": False,
    "groupWeight": 100,
    "scanDepth": None,
    "caseSensitive": None,
    "matchWholeWords": None,
    "useGroupScoring": None,
    "automationId": "",
    "role": 0,
    "sticky": None,
    "cooldown": None,
    "delay": None,
    "characterFilterNames": [],
    "characterFilterTags": [],
    "characterFilterExclude": False,
    "triggers": [],
}

# `selectiveLogic` 的四种取值 ↔ 本插件的列值。数字是酒馆 `world_info_logic` 的值。
_LOGIC_FROM_ST: dict[int, SelectiveLogic] = {
    0: SelectiveLogic.AND_ANY,
    1: SelectiveLogic.NOT_ALL,
    2: SelectiveLogic.NOT_ANY,
    3: SelectiveLogic.AND_ALL,
}
_LOGIC_TO_ST: dict[str, int] = {logic.value: value for value, logic in _LOGIC_FROM_ST.items()}

# `position` 是唯一一个「多个酒馆值折成同一个列值」的字段：0、1 都是人设之后，2、3、5、6
# 都是历史之前，4 是深度注入，7 是 outlet（本插件没有 outlet，降级成历史之前并停用）。
_POSITION_FROM_ST: dict[int, Position] = {
    0: Position.AFTER_PERSONA,
    1: Position.AFTER_PERSONA,
    2: Position.BEFORE_HISTORY,
    3: Position.BEFORE_HISTORY,
    4: Position.AT_DEPTH,
    5: Position.BEFORE_HISTORY,
    6: Position.BEFORE_HISTORY,
    7: Position.BEFORE_HISTORY,
}
_POSITION_TO_ST: dict[str, int] = {
    Position.AFTER_PERSONA.value: 1,
    Position.BEFORE_HISTORY.value: 6,
    Position.AT_DEPTH.value: 4,
}
_OUTLET_POSITION = 7

# 列名 ↔ 酒馆字段名。导出时按这张表逐个判断「改过没有」，因此它必须是**全部**映射字段，
# 漏一个就意味着那个字段永远被原始值覆盖。
_COLUMN_TO_ST_FIELD: dict[str, str] = {
    "comment": "comment",
    "keys": "key",
    "secondary_keys": "keysecondary",
    "selective_logic": "selectiveLogic",
    "constant": "constant",
    "content": "content",
    "order": "order",
    "position": "position",
    "depth": "depth",
    "scan_depth": "scanDepth",
    "case_sensitive": "caseSensitive",
    "match_whole_words": "matchWholeWords",
    "ignore_budget": "ignoreBudget",
    "sticky": "sticky",
    "cooldown": "cooldown",
    "delay": "delay",
    "enabled": "disable",
    "vectorized": "vectorized",
}

# 导入报告的类别。`summarize_notes` 按这些键计数，全部键始终存在（0 也要在），前端才能
# 无条件读某个键。`truncated` 留着只为前端与旧报告的键不变：导入超了安全上限现在是整份
# 拒绝，这个类别恒为 0。`over_budget` 只是提示：正文比每轮注入预算还长，照样完整导入。
# 最后三类来自 `purpose.template_import_notes`，同样只是提示，条目照样完整导入、启用状态
# 不改：`state_only` 仅在开启角色状态时使用、`unsupported_template` 含不支持的模板（两者
# 都不进主提示词）、`suspected_trap` 疑似防盗条目、`template_not_analyzed` 一次导入的模板分析
# 超过上限、这一条导入时没有逐条分析（运行时命中后照常分析）。`budget_check` 是整本书的预算体检
# （`budget_check` 模块：常驻条目合计、能进几条、哪几条永远进不来），`unsupported` 是「不生效清单」
# （`unsupported` 模块：EJS 模板块、NA 不支持的条目功能），都是一本书一条，只是提示。`name_var`
# 是正文里读用户 / 角色名字的变量换成了 `{{user}}` / `{{char}}` 宏，条目照样完整导入。
# `vector_pending` 是「有 n 条标了向量激活，导入后会在后台生成向量」，同样只是提示。
NOTE_KINDS = (
    "skipped",
    "truncated",
    "clamped",
    "invalid_key",
    "position_downgraded",
    "renumbered",
    "bad_value",
    "over_budget",
    "name_var",
    "state_only",
    "unsupported_template",
    "suspected_trap",
    "template_not_analyzed",
    "budget_check",
    "unsupported",
    "vector_pending",
)


@dataclass(frozen=True)
class ImportNote:
    """导入过程中的一条说明

    Attributes:
        entry_key: 哪一条（酒馆的 `uid` 原文；拿不到时为空串）。
        field: 哪个字段（酒馆字段名或本插件的列名）。
        kind: :data:`NOTE_KINDS` 之一。
        detail: 给人看的中文说明。
    """

    entry_key: str
    field: str
    kind: str
    detail: str


class WorldFileRejected(ValueError):
    """世界书文件不可用；消息即中文原因，可直接回给管理员"""


@dataclass(frozen=True)
class ParsedWorld:
    """解析好的世界书文件

    Attributes:
        book_ext: 顶层除 `entries` 之外的全部键，原样保留（`originalData`、卡内书的
            `token_budget` 之类）。
        entries: `(uid 键原文, 条目对象)`，顺序按文件里的键顺序。键原文保留是因为它在
            酒馆里就是字符串化后的 `uid`，追加重编号时要用它判断撞号。
    """

    book_ext: dict[str, Any]
    entries: list[tuple[str, Any]]


def _ensure_depth_within_limit(data: Any) -> None:
    """迭代遍历算 JSON 深度，超过上限就拒绝

    迭代而不是递归：恶意文件可以让递归版先栈溢出，那时异常发生在 `json.loads` 之外的
    地方，表现为 500 而不是「文件不合法」。
    """
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise WorldFileRejected(f"JSON 嵌套超过 {MAX_JSON_DEPTH} 层")
        if isinstance(node, dict):
            for value in node.values():
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                stack.append((value, depth + 1))


def _load_json(data: bytes) -> Any:
    """读 JSON 并检查大小与嵌套深度；所有拒绝都抛 :class:`WorldFileRejected`

    含 BOM 的文件按 `utf-8-sig` 解：酒馆导出的文件在 Windows 上偶尔带 BOM，为它报「不是 UTF-8」没意义。
    """
    if len(data) > MAX_FILE_BYTES:
        raise WorldFileRejected(f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB，整份未导入")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorldFileRejected("文件不是 UTF-8 文本") from exc
    try:
        parsed = json.loads(text)
    except RecursionError as exc:
        # 解析器自己是递归的：几十万层 `[` 在走到深度检查之前就会撑爆它。这不是
        # `ValueError`，不单独接住就会变成 500。
        raise WorldFileRejected(f"JSON 嵌套超过 {MAX_JSON_DEPTH} 层") from exc
    except ValueError as exc:
        raise WorldFileRejected(f"不是合法的 JSON：{exc}") from exc
    _ensure_depth_within_limit(parsed)
    return parsed


def parse_world_file(data: bytes) -> ParsedWorld:
    """把一个世界书文件读成书级附加字段与条目列表

    所有拒绝都抛 :class:`WorldFileRejected`，消息是中文原因。
    """
    parsed = _load_json(data)
    if not isinstance(parsed, dict):
        raise WorldFileRejected("顶层必须是一个对象")
    return _parse_st_world(parsed)


def _parse_st_world(parsed: Mapping[str, Any]) -> ParsedWorld:
    """酒馆世界书格式：`entries` 是按 `uid` 挂条目的对象"""
    raw_entries = parsed.get("entries")
    if not isinstance(raw_entries, dict):
        raise WorldFileRejected("entries 必须是一个对象")
    if len(raw_entries) > MAX_ENTRIES:
        raise WorldFileRejected(f"条目数 {len(raw_entries)} 超过安全上限 {MAX_ENTRIES}，整份未导入")
    for entry_key, raw in raw_entries.items():
        if isinstance(raw, dict):
            check_entry_limits(raw, label=f"第 {entry_key} 号条目")

    return ParsedWorld(book_ext=_file_book_ext(parsed), entries=list(raw_entries.items()))


def _file_book_ext(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """文件顶层除 `entries` 外的键，去掉 `na_` 开头的：那是本插件自己记在书级 `ext` 里的（远程来源、卡内书原文），
    文件里带了也不认——否则一份普通的世界书文件就能冒充「已经导入的远程世界书」。导出时同样不带（`export_world`）。
    """
    return {key: value for key, value in parsed.items() if key != "entries" and not str(key).startswith("na_")}


# ---------------------------------------------------------------------------
# 酒馆助手的世界书格式（卡片脚本下载、用 `createOrReplaceWorldbook` 建书的那种）
# ---------------------------------------------------------------------------

# 书级 `ext` 里记下文件原来是哪种格式
SOURCE_FORMAT_KEY = "na_source_format"
TAVERN_HELPER_FORMAT = "tavern-helper-worldbook"

# 下面几张表与默认值照抄酒馆助手（JS-Slash-Runner）`src/function/worldbook.ts` 的 `fromWorldbookEntry`
# 与 `_default_implicit_keys`：它的条目把关键词、位置、递归、定时效果收进几个对象，存进酒馆时再
# 摊平成酒馆的字段。这里照它的规则摊平，后面就是普通的酒馆条目了。
_TH_LOGIC = {"and_any": 0, "not_all": 1, "not_any": 2, "and_all": 3}
_TH_POSITION = {
    "before_character_definition": 0,
    "after_character_definition": 1,
    "before_example_messages": 5,
    "after_example_messages": 6,
    "before_author_note": 2,
    "after_author_note": 3,
    "at_depth": 4,
    "outlet": 7,
}
_TH_ROLE = {"system": 0, "user": 1, "assistant": 2}
_TH_IMPLICIT_DEFAULTS: dict[str, Any] = {
    "addMemo": True,
    "matchPersonaDescription": False,
    "matchCharacterDescription": False,
    "matchCharacterPersonality": False,
    "matchCharacterDepthPrompt": False,
    "matchScenario": False,
    "matchCreatorNotes": False,
    "group": "",
    "groupOverride": False,
    "groupWeight": 100,
    "caseSensitive": None,
    "matchWholeWords": None,
    "useGroupScoring": None,
    "automationId": "",
    "ignoreBudget": False,
    "outletName": "",
    "triggers": [],
    "characterFilter": {"isExclude": False, "names": [], "tags": []},
}


def _nullish(value: Any, default: Any) -> Any:
    """JS 的 `value ?? default`"""
    return default if value is None else value


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _th_key(value: Any) -> Any:
    """关键词照 lodash 的 `_.toString` 转成字符串（酒馆助手会把数字关键词转成字符串）

    对象与数组不转，原样交给后面的映射按「非字符串关键词」丢弃并记一条说明。
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return value


def _th_keys(value: Any) -> Any:
    """`keys?.map(_.toString) ?? []`；不是数组的原样留着，由后面的映射记 `bad_value`"""
    if value is None:
        return []
    if not isinstance(value, list):
        return value
    return [_th_key(item) for item in value]


def tavern_helper_entry_to_st(entry: Mapping[str, Any], display_index: int) -> dict[str, Any]:
    """把一条酒馆助手条目摊平成酒馆的原始条目（照 `fromWorldbookEntry`）

    认不出的枚举值（未知的位置、次关键词逻辑、角色）原样写进酒馆字段，由 :func:`st_entry_to_columns`
    按「未知的值」记进报告并取默认，不在这里悄悄改掉。`uid` 不是整数时不写，交给导入时重新编号。
    """
    strategy = _object(entry.get("strategy"))
    secondary = _object(strategy.get("keys_secondary"))
    position = _object(entry.get("position"))
    recursion = _object(entry.get("recursion"))
    effect = _object(entry.get("effect"))
    strategy_type = strategy.get("type")
    scan_depth = strategy.get("scan_depth")
    logic = _nullish(secondary.get("logic"), "and_any")
    position_type = _nullish(position.get("type"), "at_depth")
    position_value = _TH_POSITION.get(position_type, position_type) if isinstance(position_type, str) else position_type
    role = _nullish(position.get("role"), "system")

    raw: dict[str, Any] = {}
    uid = entry.get("uid")
    if isinstance(uid, int) and not isinstance(uid, bool):
        raw["uid"] = uid
    raw.update(
        {
            "displayIndex": display_index,
            "comment": _nullish(entry.get("name"), ""),
            "disable": not _nullish(entry.get("enabled"), True),
            # 没写类型时酒馆助手按常驻处理（`type ? type === 'constant' : true`）
            "constant": strategy_type == "constant" if strategy_type else True,
            "selective": strategy_type == "selective",
            "key": _th_keys(strategy.get("keys")),
            "selectiveLogic": _TH_LOGIC.get(logic, logic) if isinstance(logic, str) else logic,
            "keysecondary": _th_keys(secondary.get("keys")),
            "scanDepth": None if scan_depth == "same_as_global" else scan_depth,
            "vectorized": strategy_type == "vectorized",
            "position": position_value,
            "role": _TH_ROLE.get(role, role) if isinstance(role, str) else role,
            "depth": _nullish(position.get("depth"), 4),
            "order": _nullish(position.get("order"), 100),
            "content": _nullish(entry.get("content"), ""),
            "useProbability": True,
            "probability": _nullish(entry.get("probability"), 100),
            "excludeRecursion": _nullish(recursion.get("prevent_incoming"), False),
            "preventRecursion": _nullish(recursion.get("prevent_outgoing"), False),
            "delayUntilRecursion": _nullish(recursion.get("delay_until"), False),
            "sticky": effect.get("sticky"),
            "cooldown": effect.get("cooldown"),
            "delay": effect.get("delay"),
        }
    )
    if entry.get("extra"):
        raw["extra"] = copy.deepcopy(entry["extra"])
    for key, default in _TH_IMPLICIT_DEFAULTS.items():
        value = entry[key] if key in entry else default
        if key == "characterFilter" and isinstance(value, Mapping):
            # lodash 的 merge 是按键合并：条目只写了其中几项时，别的项仍是默认值
            value = {**default, **value}
        raw[key] = copy.deepcopy(value)
    return raw


def _looks_like_tavern_helper(entries: list[Any]) -> bool:
    """条目里有 `strategy` 或 `position` 对象：酒馆助手的格式（酒馆的条目里 `position` 是数字）"""
    return any(
        isinstance(item, Mapping)
        and (isinstance(item.get("strategy"), Mapping) or isinstance(item.get("position"), Mapping))
        for item in entries
    )


def parse_world_payload(data: bytes) -> ParsedWorld:
    """世界书文件：酒馆的世界书格式，或酒馆助手的格式（`entries` 是条目数组，或顶层就是条目数组）

    酒馆助手的条目逐条摊平成酒馆的原始条目（见 :func:`tavern_helper_entry_to_st`），之后的映射、
    安全上限与导入分析与酒馆格式完全相同。书级 `ext` 原样保留文件顶层除 `entries` 外的字段
    （`book_name`、`version` 之类），另记 `na_source_format`。别的形状整份拒绝。
    """
    parsed = _load_json(data)
    if isinstance(parsed, list):
        raw_entries: Any = parsed
        book_ext: dict[str, Any] = {}
    elif isinstance(parsed, dict):
        raw_entries = parsed.get("entries")
        if isinstance(raw_entries, dict):
            return _parse_st_world(parsed)
        if not isinstance(raw_entries, list):
            raise WorldFileRejected("认不出的世界书格式：entries 既不是对象也不是数组")
        book_ext = _file_book_ext(parsed)
    else:
        raise WorldFileRejected("顶层必须是一个对象或数组")
    if not raw_entries:
        raise WorldFileRejected("文件里没有条目")
    if not _looks_like_tavern_helper(raw_entries):
        raise WorldFileRejected("认不出的世界书格式：条目既不是酒馆的格式，也不是酒馆助手的格式")
    if len(raw_entries) > MAX_ENTRIES:
        raise WorldFileRejected(f"条目数 {len(raw_entries)} 超过安全上限 {MAX_ENTRIES}，整份未导入")

    entries: list[tuple[str, Any]] = []
    for index, item in enumerate(raw_entries):
        if not isinstance(item, Mapping):
            # 非对象的条目照酒馆格式的规矩：导入时整条跳过并记一条说明
            entries.append((f"#{index + 1}", item))
            continue
        raw = tavern_helper_entry_to_st(item, index)
        check_entry_limits(raw, label=f"第 {index + 1} 条")
        uid = raw.get("uid")
        entries.append((str(uid) if uid is not None else f"#{index + 1}", raw))
    book_ext[SOURCE_FORMAT_KEY] = TAVERN_HELPER_FORMAT
    return ParsedWorld(book_ext=book_ext, entries=entries)


def check_entry_limits(raw: Mapping[str, Any], *, label: str) -> None:
    """一条原始条目有没有超过安全上限；超了抛 :class:`WorldFileRejected`

    只看会被写进列的那几个字段（标题、正文、主 / 次关键词）。类型不对的值不在这里判：
    映射时按 `bad_value` 记进报告，与上限无关。消息里带 `label`（调用方给的「第几条」）、
    字段与实际长度，管理员拿着它就能在原文件里找到那一条。
    """
    for field, limit, name in (("comment", MAX_COMMENT_CHARS, "标题"), ("content", MAX_CONTENT_CHARS, "正文")):
        value = raw.get(field)
        if isinstance(value, str) and len(value) > limit:
            raise WorldFileRejected(f"{label}的{name}有 {len(value)} 字，超过安全上限 {limit}，整份未导入")
    for field, name in (("key", "主关键词"), ("keysecondary", "次关键词")):
        value = raw.get(field)
        if not isinstance(value, list):
            continue
        strings = [item for item in value if isinstance(item, str)]
        if len(strings) > MAX_KEYS:
            raise WorldFileRejected(f"{label}的{name}有 {len(strings)} 个，超过安全上限 {MAX_KEYS}，整份未导入")
        for item in strings:
            if len(item) > MAX_KEY_LENGTH:
                raise WorldFileRejected(
                    f"{label}有一个{name}长 {len(item)} 字，超过安全上限 {MAX_KEY_LENGTH}，整份未导入",
                )


def budget_notes(
    columns_list: Iterable[tuple[str, Mapping[str, Any]]],
    *,
    budget: Optional["WorldbookBudget"],
) -> list[ImportNote]:
    """正文比每轮世界书预算还长的条目，各记一条 `over_budget` 提示

    只提示、不截断：预算是配置项，管理员调大以后这一条就进得去；在导入时截掉，就再也
    找不回原文了。预算读不到（`None`）就不提示。token 数按这个实例主模型组的分词系数算，
    与运行时同一个数法。
    """
    if budget is None:
        return []
    notes: list[ImportNote] = []
    for entry_key, columns in columns_list:
        content = str(columns.get("content") or "")
        size = budget.count(content)
        if size > budget.budget:
            notes.append(
                ImportNote(
                    entry_key,
                    "content",
                    "over_budget",
                    f"这一条正文约 {size} token（{len(content)} 字），比每轮世界书预算 {budget.budget} token 还长"
                    f"（按模型组 {budget.model_group or '（未设置）'} 算，见 WORLDBOOK_BUDGET_PERCENT / "
                    "WORLDBOOK_BUDGET_CAP_TOKENS），按现在的设置注入不进去",
                ),
            )
    return notes


def _entry_key(raw: Mapping[str, Any]) -> str:
    """报告里用来指认条目：优先酒馆的 `uid`，没有就用空串"""
    uid = raw.get("uid")
    return str(uid) if isinstance(uid, int) and not isinstance(uid, bool) else ""


def _as_int(value: Any, default: int) -> int | None:
    """读整数；布尔与读不出来的值都返回 `None`（由调用方决定用默认值还是记报告）"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _clamp_int(
    value: Any,
    *,
    default: int,
    low: int,
    high: int,
    entry_key: str,
    field: str,
    notes: list[ImportNote],
) -> int:
    """读一个整数并夹进区间；越界记 `clamped`，读不出来记 `bad_value`"""
    number = _as_int(value, default)
    if number is None:
        if value is not None and value != default:
            notes.append(ImportNote(entry_key, field, "bad_value", f"不是整数，按 {default} 处理"))
        return default
    if number < low or number > high:
        clamped = max(low, min(high, number))
        notes.append(ImportNote(entry_key, field, "clamped", f"{number} 超出 {low}~{high}，夹到 {clamped}"))
        return clamped
    return number


def _clamp_optional(
    value: Any,
    *,
    low: int,
    high: int,
    entry_key: str,
    field: str,
    notes: list[ImportNote],
) -> int | None:
    """读一个可空整数：`None` 原样保留，数值夹进区间"""
    if value is None:
        return None
    return _clamp_int(value, default=0, low=low, high=high, entry_key=entry_key, field=field, notes=notes)


def _bool_or_default(value: Any, default: bool) -> bool:
    """读布尔；非布尔按默认值（酒馆文件里的布尔基本可信，这里只是不抛异常）"""
    if isinstance(value, bool):
        return value
    return default


def _bool_with_note(
    value: Any,
    *,
    entry_key: str,
    field: str,
    notes: list[ImportNote],
) -> bool:
    """读一个非空布尔列：读不懂按 False，并记一条 `bad_value`

    判据与 `_bool_or_default` 相同（非布尔一律当假），区别只是把「读不懂」写进导入报告：
    这个开关会让一条宿主条目绕过预算，静默按假处理会让管理员以为文件里写的那个 true 生效了。
    """
    if isinstance(value, bool):
        return value
    if value is not None:
        notes.append(ImportNote(entry_key, field, "bad_value", "不是布尔值，按 False 处理"))
    return False


def _optional_bool(value: Any) -> bool | None:
    """三态布尔：`None` 表示「跟书」"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def _map_keys(
    value: Any,
    *,
    entry_key: str,
    field: str,
    notes: list[ImportNote],
) -> list[str]:
    """把关键词数组映射成字符串数组

    非字符串元素丢弃（记 `skipped`）而不是 `str()` 转换：`str(123)` 会造出一个用户从没
    写过的关键词 `"123"`，而它在原文件里根本不是关键词。条数与单条长度超限时截断并记
    `truncated` 只是兜底：导入路径在 :func:`check_entry_limits` 就整份拒绝了，走到这里的
    超限值只会来自读库里的旧数据（导出时以 `ext` 为底再映射一遍），那时不能抛异常。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        notes.append(ImportNote(entry_key, field, "bad_value", "不是数组，按空处理"))
        return []
    keys: list[str] = []
    for item in value:
        if not isinstance(item, str):
            notes.append(ImportNote(entry_key, field, "skipped", f"丢弃非字符串关键词 {item!r}"))
            continue
        if len(keys) >= MAX_KEYS:
            notes.append(ImportNote(entry_key, field, "truncated", f"超过 {MAX_KEYS} 个关键词，多余的丢弃"))
            break
        if len(item) > MAX_KEY_LENGTH:
            notes.append(
                ImportNote(entry_key, field, "truncated", f"关键词超过 {MAX_KEY_LENGTH} 字，已截断"),
            )
            keys.append(item[:MAX_KEY_LENGTH])
            continue
        keys.append(item)
    return keys


def _map_text(
    value: Any,
    *,
    limit: int,
    entry_key: str,
    field: str,
    notes: list[ImportNote],
) -> str:
    """读一段文本并截断到上限"""
    if not isinstance(value, str):
        if value is not None:
            notes.append(ImportNote(entry_key, field, "bad_value", "不是字符串，按空处理"))
        return ""
    if len(value) > limit:
        notes.append(ImportNote(entry_key, field, "truncated", f"超过 {limit} 字，已截断"))
        return value[:limit]
    return value


def st_entry_to_columns(
    raw: Mapping[str, Any],
    *,
    rewrite_names: bool = True,
) -> tuple[dict[str, Any], list[ImportNote]]:
    """把一条酒馆条目映射成 `wb_entry` 的列

    返回的字典键名与 :class:`~nekro_agent.models.db_wb_entry.DBWbEntry` 的字段同名，但
    不含 `book`、`trusted`、`status`、`ext`：前三个由调用方按权限与导入人决定，`ext` 存
    原始条目。

    `rewrite_names` 为假时不把正文里读名字的变量换成宏（导入对话框的开关）；导出时算基线
    的调用保持默认值，这样「列值」与「把原始条目再映射一遍」的比较仍然一致。

    `uid` 不是整数时给 `None`：追加导入时调用方要重编号，新建时取书内最大值加一。这里
    不擅自造一个编号，否则「原 `uid` 丢了」会变成静默的。
    """
    notes: list[ImportNote] = []
    entry_key = _entry_key(raw)

    raw_uid = raw.get("uid")
    uid = _as_int(raw_uid, 0)
    if uid is None:
        notes.append(ImportNote(entry_key, "uid", "bad_value", "uid 不是整数，交由调用方编号"))
    elif not UID_MIN <= uid <= UID_MAX:
        # `wb_entry.uid` 是 32 位整数列：超出范围的编号写库时会报错，整份导入失败成 500
        notes.append(ImportNote(entry_key, "uid", "bad_value", f"uid {uid} 超出可存的范围，交由调用方编号"))
        uid = None

    logic_value = _as_int(raw.get("selectiveLogic", ST_ENTRY_DEFAULTS["selectiveLogic"]), 0)
    logic = _LOGIC_FROM_ST.get(logic_value) if logic_value is not None else None
    if logic is None:
        notes.append(
            ImportNote(
                entry_key,
                "selectiveLogic",
                "bad_value",
                f"未知的次关键词逻辑 {raw.get('selectiveLogic')!r}，按 and_any 处理",
            ),
        )
        logic = SelectiveLogic.AND_ANY

    raw_position = _as_int(raw.get("position", ST_ENTRY_DEFAULTS["position"]), 0)
    position = _POSITION_FROM_ST.get(raw_position) if raw_position is not None else None
    if position is None:
        notes.append(
            ImportNote(
                entry_key,
                "position",
                "position_downgraded",
                f"未知的注入位置 {raw.get('position')!r}，按历史之前处理",
            ),
        )
        position = Position.BEFORE_HISTORY
    enabled = not _bool_or_default(raw.get("disable", ST_ENTRY_DEFAULTS["disable"]), False)
    if raw_position == _OUTLET_POSITION:
        # outlet 是酒馆的另一种投递通道（按名字取用），本插件没有对应位置；停用而不是
        # 悄悄搬到历史之前，免得一条本该「按需取用」的设定变成常驻内容。
        notes.append(
            ImportNote(entry_key, "position", "position_downgraded", "outlet 不支持，改为历史之前并停用"),
        )
        enabled = False

    content = _map_text(
        raw.get("content", ST_ENTRY_DEFAULTS["content"]),
        limit=MAX_CONTENT_CHARS,
        entry_key=entry_key,
        field="content",
        notes=notes,
    )
    name_replacements = []
    if rewrite_names:
        content, name_replacements = rewrite_name_vars(content)

    columns: dict[str, Any] = {
        "uid": uid,
        "comment": _map_text(
            raw.get("comment", ST_ENTRY_DEFAULTS["comment"]),
            limit=MAX_COMMENT_CHARS,
            entry_key=entry_key,
            field="comment",
            notes=notes,
        ),
        "vectorized": _bool_or_default(raw.get("vectorized", ST_ENTRY_DEFAULTS["vectorized"]), False),
        "keys": _map_keys(raw.get("key", ST_ENTRY_DEFAULTS["key"]), entry_key=entry_key, field="key", notes=notes),
        "secondary_keys": _map_keys(
            raw.get("keysecondary", ST_ENTRY_DEFAULTS["keysecondary"]),
            entry_key=entry_key,
            field="keysecondary",
            notes=notes,
        ),
        "selective_logic": logic.value,
        # 酒馆没有「整条关键词都是正则」这个开关（它靠 `/…/` 形态），因此这一列永远是
        # 假的；用户在本插件里打开它之后，导出也不写回任何酒馆字段。
        "use_regex": False,
        "constant": _bool_or_default(raw.get("constant", ST_ENTRY_DEFAULTS["constant"]), False),
        "content": content,
        "order": _clamp_int(
            raw.get("order", ST_ENTRY_DEFAULTS["order"]),
            default=ST_ENTRY_DEFAULTS["order"],
            low=ORDER_MIN,
            high=ORDER_MAX,
            entry_key=entry_key,
            field="order",
            notes=notes,
        ),
        "position": position.value,
        "depth": _clamp_int(
            raw.get("depth", ST_ENTRY_DEFAULTS["depth"]),
            default=ST_ENTRY_DEFAULTS["depth"],
            low=DEPTH_MIN,
            high=DEPTH_MAX,
            entry_key=entry_key,
            field="depth",
            notes=notes,
        ),
        "scan_depth": _clamp_optional(
            raw.get("scanDepth", ST_ENTRY_DEFAULTS["scanDepth"]),
            low=SCAN_DEPTH_MIN,
            high=SCAN_DEPTH_MAX,
            entry_key=entry_key,
            field="scanDepth",
            notes=notes,
        ),
        "case_sensitive": _optional_bool(raw.get("caseSensitive", ST_ENTRY_DEFAULTS["caseSensitive"])),
        "match_whole_words": _optional_bool(raw.get("matchWholeWords", ST_ENTRY_DEFAULTS["matchWholeWords"])),
        "ignore_budget": _bool_with_note(
            raw.get("ignoreBudget", ST_ENTRY_DEFAULTS["ignoreBudget"]),
            entry_key=entry_key,
            field="ignoreBudget",
            notes=notes,
        ),
        "sticky": _clamp_optional(
            raw.get("sticky", ST_ENTRY_DEFAULTS["sticky"]),
            low=0,
            high=TIMED_MAX,
            entry_key=entry_key,
            field="sticky",
            notes=notes,
        ),
        "cooldown": _clamp_optional(
            raw.get("cooldown", ST_ENTRY_DEFAULTS["cooldown"]),
            low=0,
            high=TIMED_MAX,
            entry_key=entry_key,
            field="cooldown",
            notes=notes,
        ),
        "delay": _clamp_optional(
            raw.get("delay", ST_ENTRY_DEFAULTS["delay"]),
            low=0,
            high=TIMED_MAX,
            entry_key=entry_key,
            field="delay",
            notes=notes,
        ),
        "enabled": enabled,
    }
    if name_replacements:
        detail = "读名字的变量换成了宏：" + "、".join(
            f"「{item.var_name}」→ {item.macro} ×{item.count}" for item in name_replacements
        )
        notes.append(ImportNote(entry_key, "content", "name_var", detail))
    return columns, notes


def _columns_to_st_value(field: str, value: Any) -> Any:
    """把一个列值写成酒馆字段值；字段不认识时原样返回"""
    if field == "keys" or field == "secondary_keys":
        return list(value or [])
    if field == "selective_logic":
        return _LOGIC_TO_ST.get(str(value), 0)
    if field == "position":
        return _POSITION_TO_ST.get(str(value), 6)
    if field == "enabled":
        return not bool(value)
    return value


def columns_to_st_entry(columns: Mapping[str, Any], ext: Mapping[str, Any] | None) -> dict[str, Any]:
    """把一条条目的列与原始条目还原成酒馆条目对象

    规则见模块文档：以 `ext` 去掉 `na_` 键后的内容为底（没有就用 :data:`ST_ENTRY_DEFAULTS`
    的深拷贝），逐个映射字段比较「列值」与「把底再映射一遍得到的值」，相同就保留底里的
    原值，不同才写列值的酒馆形态。`uid` 一律写列值。

    深拷贝是必须的：底里可能有列表（关键词、`triggers`），直接引用会让调用方改到
    `ST_ENTRY_DEFAULTS` 本身，此后每个新条目的默认值都被污染。
    """
    base_source = {key: value for key, value in (ext or {}).items() if not str(key).startswith("na_")}
    base = copy.deepcopy(base_source) if base_source else copy.deepcopy(ST_ENTRY_DEFAULTS)
    baseline, _ = st_entry_to_columns(base)

    result = dict(base)
    for column, st_field in _COLUMN_TO_ST_FIELD.items():
        if columns.get(column) == baseline.get(column):
            continue
        result[st_field] = _columns_to_st_value(column, columns.get(column))
    if columns.get("uid") is not None:
        result["uid"] = columns["uid"]
    return result


def export_world(
    book_ext: Mapping[str, Any] | None,
    entries: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, Any]:
    """把一本书导出成酒馆世界书格式

    `entries` 里每项是（列, 原始条目）。`uid` 用 `str()` 作键：酒馆的 `entries` 是对象，
    键只能是字符串，而它的 `uid` 与键内容相同。
    """
    result: dict[str, Any] = {key: value for key, value in (book_ext or {}).items() if not str(key).startswith("na_")}
    result["entries"] = {str(columns["uid"]): columns_to_st_entry(columns, ext) for columns, ext in entries}
    return result


def validate_entry_regexes(columns: Mapping[str, Any]) -> list[dict[str, str]]:
    """校验一条条目的全部关键词，返回无效的那些（`key` 与中文 `reason`）

    只在 `use_regex` 为真或关键词是 `/…/flags` 形态时才真的校验；普通关键词一律通过。
    导入时调用方把结果写进 `ext["na_invalid_keys"]`，匹配时据此跳过。
    """
    use_regex = bool(columns.get("use_regex"))
    invalid: list[dict[str, str]] = []
    for field in ("keys", "secondary_keys"):
        for key in columns.get(field) or []:
            if not isinstance(key, str):
                continue
            reason = validate_regex_key(key, use_regex=use_regex)
            if reason is not None:
                invalid.append({"key": key, "reason": reason})
    return invalid


def summarize_notes(notes: Iterable[ImportNote]) -> dict[str, int]:
    """按类别计数；结果里每个类别都有键（0 也在），前端可以无条件读"""
    summary = {kind: 0 for kind in NOTE_KINDS}
    for note in notes:
        if note.kind in summary:
            summary[note.kind] += 1
    return summary
