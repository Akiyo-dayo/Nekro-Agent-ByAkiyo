"""把世界书正文里「读用户 / 角色名字」的变量换成 NA 认识的宏

中文社区的卡常用 `<%= getLocalVar('数据库用户名称') %>`、`{{getvar::用户名}}` 这类写法在
世界书里放「用户的名字」，值由酒馆的脚本写进去。NA 不执行脚本，这些变量取出来是空串，
句子就缺一块（「XX大人」变成「大人」）。

**只改写「输出变量值」的那两种写法**：EJS 的 `<%= getvar('…') %>` 一类输出标签，与酒馆的
`{{getvar::…}}` / `{{getglobalvar::…}}` 宏。正文里的普通文字一个字都不动，条件里的读法
（`<% if (getvar('用户名') == …) %>`）、多参数调用、其它变量（`好感度`、`stat_data.金钱`）
也原样留着。

**分类按变量名的整段写法认，宁可漏换也不误换。** 先去掉首尾空白、把 ASCII 转小写，再按
`.`、`/`、`[`、`]`、`:` 切成若干段，每段去掉 `_`、`-`、空格（`user_name` 当 `username`）。
只认两种形状：

- 最后一段**正好**是「谁 + 名字类词」：`用户名`、`玩家名称`、`主角昵称`、`username`、
  `player_name`，前面最多再带一个命名空间前缀（`数据库用户名称`、`global_user_name`）；
- 最后一段是**光秃秃的**名字类词、倒数第二段**正好**是「谁」：`主角.名字`、`stat_data.玩家.昵称`。

「谁」和名字类词之间夹了别的字就不换：MVU 卡里 `stat_data.主角.武器.名称`、`玩家公会名称`、
`主角坐骑名字`、`player_rank_name`、`user.pet.nickname` 都是别的东西的名字，开了角色状态后
这些变量是有真值的。「用户名额」「用户签名」「玩家排名」「角色名单」`user_name_count` 同样不换。
认不出就不换——漏换的变量，管理员能在书详情里自己改。

两个正则会用到的重复都封了顶，几十万字的对抗文本也是线性的。
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass

# 变量名分类时只把 ASCII 字母转成小写；中文与其它文字原样比较。
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

# 归一化最后一段时去掉的分隔符：`user_name`、`user-name` 与 `username` 视为同一个词。
_SEGMENT_SEPARATORS = str.maketrans("", "", "_- ")
# 变量名里可能出现的路径分隔符；只用来取最后一段。
_SEGMENT_SPLIT = re.compile(r"[./\[\]:]")

# 整个名字里找「这是谁的名字」的字样。
_USER_HINTS = ("用户", "玩家", "主角", "user", "player")
_CHAR_HINTS = ("角色", "char")

# 最后一段整词相等就算；这些都是「某人的名字」，不含「名单」「名额」这类别的词。
_USER_EXACT = frozenset(
    {
        "用户名",
        "玩家名",
        "主角名",
        "username",
        "playername",
        "用户名字",
        "用户名称",
        "用户姓名",
        "用户昵称",
        "玩家名字",
        "玩家名称",
        "玩家昵称",
        "主角名字",
        "主角名称",
        "主角姓名",
        "主角昵称",
        "usernickname",
        "playernickname",
    },
)
_CHAR_EXACT = frozenset(
    {
        "角色名",
        "charname",
        "charactername",
        "角色名字",
        "角色名称",
        "角色姓名",
        "角色昵称",
    },
)

# 名字类词；「名」单独一个字只在紧跟「谁」时算（`用户名`、`charname`），见 `_HEAD_ENDINGS`。
_NAME_WORDS = ("名字", "名称", "姓名", "昵称", "name", "nickname")
# 紧跟在「谁」后面时也算名字的单字：`用户名`、`玩家名`、`角色名`。
_HEAD_ENDINGS = ("名",) + _NAME_WORDS
# 单段名前面允许的命名空间前缀。中文卡常把变量放在「数据库」底下（`数据库用户名称`）。
_NAMESPACE_PREFIXES = ("", "数据库", "全局", "当前", "global", "current")

# 1. EJS 输出标签：`<%=` / `<%-`，调用里只有一个字符串参数，之后可以有分号。
_EJS_NAME_CALL = re.compile(
    r"<%[=-]\s{0,8}(?:getvar|getLocalVar|getGlobalVar|getChatVar)\s{0,8}\(\s{0,8}"
    r"(['\"])([^'\"\r\n<>%]{1,64})\1\s{0,8}\)\s{0,8};?\s{0,8}-?%>",
)
# 2. 酒馆宏：`{{getvar::名字}}` / `{{getglobalvar::名字}}`，宏名不分大小写。
_MACRO_NAME_CALL = re.compile(
    r"\{\{\s{0,8}(?:getvar|getglobalvar)\s{0,8}::\s{0,8}([^{}\r\n:]{1,64})\s{0,8}\}\}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NameVarReplacement:
    """一段正文里某个变量被换掉的结果

    Attributes:
        var_name: 原变量名（正文里怎么写就怎么留，供导入说明点名）。
        macro: 换成的宏，`{{user}}` 或 `{{char}}`。
        count: 这个变量在这段正文里被换了几处。
    """

    var_name: str
    macro: str
    count: int


def _ascii_lower(value: str) -> str:
    """只把 ASCII 大写字母转小写，其它字符原样返回"""
    return value.translate(_ASCII_LOWER)


def _segments(var_name: str) -> list[str]:
    """把变量名切成段：ASCII 转小写，按 `.`、`/`、`[`、`]`、`:` 切开，每段去掉 `_`、`-` 与空格，丢掉空段"""
    whole = _ascii_lower(var_name.strip())
    compacted = (segment.translate(_SEGMENT_SEPARATORS) for segment in _SEGMENT_SPLIT.split(whole))
    return [segment for segment in compacted if segment]


def _is_head_and_name(segment: str, hints: tuple[str, ...], exact: frozenset[str]) -> bool:
    """这一段正好是「谁 + 名字类词」（可带一个命名空间前缀），或在整词表里"""
    if segment in exact:
        return True
    for prefix in _NAMESPACE_PREFIXES:
        if not segment.startswith(prefix):
            continue
        rest = segment[len(prefix) :]
        if any(rest == hint + ending for hint in hints for ending in _HEAD_ENDINGS):
            return True
    return False


def _owner_matches(segments: list[str], hints: tuple[str, ...], exact: frozenset[str]) -> bool:
    last = segments[-1]
    if _is_head_and_name(last, hints, exact):
        return True
    return len(segments) >= 2 and last in _NAME_WORDS and segments[-2] in hints


def _macro_for(var_name: str) -> str | None:
    """按变量名判断该换成哪个宏；认不出时返回 `None`（原样不动）"""
    segments = _segments(var_name)
    if not segments:
        return None
    if _owner_matches(segments, _USER_HINTS, _USER_EXACT):
        return "{{user}}"
    if _owner_matches(segments, _CHAR_HINTS, _CHAR_EXACT):
        return "{{char}}"
    return None


def _matches(content: str) -> list[tuple[int, int, str]]:
    """两种写法命中的 `(起点, 终点, 变量名)`，按起点排序"""
    found = [(match.start(), match.end(), match.group(2)) for match in _EJS_NAME_CALL.finditer(content)]
    found.extend((match.start(), match.end(), match.group(1)) for match in _MACRO_NAME_CALL.finditer(content))
    found.sort(key=lambda item: item[0])
    return found


def rewrite_name_vars(content: str) -> tuple[str, list[NameVarReplacement]]:
    """把正文里读名字的变量换成宏，返回新正文与逐变量的替换结果

    结果列表按变量在正文里第一次出现的顺序排列；同一个变量出现多处时合并成一个
    :class:`NameVarReplacement`，`count` 是总处数。没有可替换的东西时原样返回正文与空列表。
    """
    # 只要正文里可能有这两种写法才继续扫；`Var` 能覆盖 `getLocalVar (` 这种括号前带空格的写法。
    if "getvar" not in content and "Var" not in content and "{{" not in content:
        return content, []

    parts: list[str] = []
    cursor = 0
    last_end = -1
    replacements: dict[str, NameVarReplacement] = {}
    for start, end, var_name in _matches(content):
        if start < last_end:
            # 两种写法不会真的套在一起；万一有重叠，只认先出现的那一个，避免把正文切乱。
            continue
        macro = _macro_for(var_name)
        if macro is None:
            continue
        parts.append(content[cursor:start])
        parts.append(macro)
        cursor = end
        last_end = end
        old = replacements.get(var_name)
        replacements[var_name] = NameVarReplacement(var_name, macro, 1 if old is None else old.count + 1)

    if not replacements:
        return content, []
    parts.append(content[cursor:])
    return "".join(parts), list(replacements.values())


__all__ = [
    "NameVarReplacement",
    "rewrite_name_vars",
]
