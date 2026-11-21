"""酒馆宏展开

宏只是文字替换，不执行任何东西。卡片正文、开场白、深度注入与清理规则的替换文字
都会在运行时过一遍这里；未知宏原样保留，变量宏与条件宏不会被执行（卡里的状态系
统因此不会工作，导入报告会提醒）。

由内向外最多展开八遍：先替换内层，外层的 `{{…}}` 下一遍才成为完整宏。每一遍结束
都检查长度，超过原文的两倍再加四千字就停在上一遍——卡片可以构造出“展开后仍然
含宏”的文字，没有这道闸，八遍足以把一条消息撑到发不出去。
"""

from __future__ import annotations

import hashlib
import random
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from nekro_agent.services.output_filter.normalize import is_invisible_char

# 一遍里只匹配「内容不含 {{ 与 }}」的宏；内层先被替换，外层留到下一遍。
_MACRO = re.compile(r"\{\{((?:(?!\{\{|\}\}).)*)\}\}", re.DOTALL)
# 旧式占位，酒馆的老卡与部分提示词里还在用。
_LEGACY = re.compile(r"<(user|bot|char)>", re.IGNORECASE)
# 掷骰：`NdM` 或只写面数 `M`。
_ROLL_DICE = re.compile(r"^(\d+)[dD](\d+)$")
_ROLL_PLAIN = re.compile(r"^(\d+)$")

_WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MAX_PASSES = 8
_MAX_USER_NAME_CHARS = 24


@dataclass
class MacroContext:
    """一次宏展开需要的上下文

    `char_name` 由调用方决定用昵称还是人设名；`user_name` 应已经过
    :func:`sanitize_user_name`，这里不再清洗。`rng` 只给 `random` 与 `roll`
    用，`pick` 按种子自己构造随机数，保证同一位置稳定。
    """

    char_name: str
    user_name: str
    now: datetime
    lang: str = "zh-CN"
    chat_key: str = ""
    preset_id: int = 0
    text_kind: str = ""
    original: str = ""
    rng: random.Random | None = None


@dataclass(frozen=True)
class MacroResult:
    """展开结果

    Attributes:
        text: 展开后的文字。
        expanded: 本次调用一共替换了多少处（宏与旧式占位都算）。
        guarded: 是否因为膨胀超过上限而停在上一遍。
    """

    text: str
    expanded: int
    guarded: bool


def sanitize_user_name(raw: str | None) -> str:
    """清洗 `{{user}}` 的昵称

    群友能改自己的昵称，而人设正文是指令级的：换行、标签与零宽字符都能把一句
    昵称变成另一段指令。这里先 NFKC，再丢掉控制与不可见字符，把任何空白压成一个
    空格，去掉花括号、尖括号与反引号，最后截到 24 字；清洗后为空用「用户」。
    """
    if not raw:
        return "用户"
    normalized = unicodedata.normalize("NFKC", raw)
    kept: list[str] = []
    for char in normalized:
        # 空白单独处理：换行本身是控制字符，但这里要的是压成空格而不是删掉。
        if char.isspace():
            kept.append(" ")
            continue
        if unicodedata.category(char) in ("Cc", "Cf") or is_invisible_char(char):
            continue
        kept.append(char)
    text = re.sub(r" +", " ", "".join(kept)).strip()
    for char in "{}<>`":
        text = text.replace(char, "")
    text = text.strip()[:_MAX_USER_NAME_CHARS]
    return text or "用户"


def _weekday(ctx: MacroContext) -> str:
    """按系统语言给星期名"""
    index = ctx.now.weekday()
    if ctx.lang.lower().startswith("en"):
        return _WEEKDAYS_EN[index]
    return _WEEKDAYS_ZH[index]


def _rng(ctx: MacroContext) -> random.Random:
    """`random` 与 `roll` 共用的随机源；没有注入时用模块级随机"""
    return ctx.rng if ctx.rng is not None else random


def _roll(args: list[str], rng: random.Random) -> str | None:
    """掷骰；格式不对或越界返回 `None`，由调用方原样保留"""
    if not args:
        return None
    raw = args[0].strip()
    dice = _ROLL_DICE.match(raw)
    if dice is not None:
        count, faces = int(dice.group(1)), int(dice.group(2))
    else:
        plain = _ROLL_PLAIN.match(raw)
        if plain is None:
            return None
        count, faces = 1, int(plain.group(1))
    if count < 1 or count > 100 or faces < 1 or faces > 1000000:
        return None
    return str(sum(rng.randint(1, faces) for _ in range(count)))


def _pick(args: list[str], ctx: MacroContext, index: int) -> str | None:
    """按频道、人设、文字类别与宏序号稳定挑选一个参数"""
    if not args:
        return None
    seed_text = "\0".join([ctx.chat_key, str(ctx.preset_id), ctx.text_kind, str(index)])
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest(), "big")
    return random.Random(seed).choice(args)


def _parse_macro(content: str) -> tuple[str, list[str]]:
    """拆出宏名与参数

    `::` 是酒馆常用分隔；没有 `::` 但有单个 `:` 时按 V3 / 旧式写法处理，参数用
    逗号切（`{{random:a,b}}`）。参数本身不 strip，作者写的空格要留着。
    """
    if content.lstrip().startswith("//"):
        return "", []
    if "::" in content:
        parts = content.split("::")
        return parts[0].strip().lower(), parts[1:]
    if ":" in content:
        name, rest = content.split(":", 1)
        return name.strip().lower(), rest.split(",")
    return content.strip().lower(), []


# 原样保留的哨兵：宏不识别、参数不够或越界时返回它，这一处不算替换。
_UNCHANGED = object()


def _expand_one(content: str, ctx: MacroContext, pick_index: list[int]) -> object:
    """展开一个宏；不识别时返回 :data:`_UNCHANGED`"""
    name, args = _parse_macro(content)
    if not name:
        # `{{// …}}` 与空内容：注释替换成空串，空内容原样保留。
        return "" if content.lstrip().startswith("//") else _UNCHANGED
    if name == "char":
        return ctx.char_name
    if name == "user":
        return ctx.user_name
    if name == "time":
        return ctx.now.strftime("%H:%M")
    if name == "date":
        return ctx.now.strftime("%Y-%m-%d")
    if name == "weekday":
        return _weekday(ctx)
    if name == "random":
        return _rng(ctx).choice(args) if args else _UNCHANGED
    if name == "pick":
        picked = _pick(args, ctx, pick_index[0])
        if picked is None:
            return _UNCHANGED
        pick_index[0] += 1
        return picked
    if name == "roll":
        rolled = _roll(args, _rng(ctx))
        return rolled if rolled is not None else _UNCHANGED
    if name == "newline":
        return "\n"
    if name == "original":
        return ctx.original
    if name == "comment":
        return ""
    return _UNCHANGED


def _replace_legacy(text: str, ctx: MacroContext) -> tuple[str, int]:
    """在第一遍之前替换旧式 `<USER>` / `<BOT>` / `<CHAR>`"""
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return ctx.user_name if match.group(1).lower() == "user" else ctx.char_name

    return _LEGACY.sub(replace, text), count


def _expand_pass(text: str, ctx: MacroContext, pick_index: list[int]) -> tuple[str, int]:
    """跑一遍由内向外替换，返回新文字与替换处数"""
    replaced = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal replaced
        expanded = _expand_one(match.group(1), ctx, pick_index)
        if expanded is _UNCHANGED:
            return match.group(0)
        replaced += 1
        return str(expanded)

    return _MACRO.sub(replace, text), replaced


def expand_macros(text: str, ctx: MacroContext) -> MacroResult:
    """展开文字里的宏；最多八遍，超长时停在上一遍"""
    current, expanded = _replace_legacy(text, ctx)
    limit = 2 * len(text) + 4000
    guarded = False
    pick_index = [0]

    for _ in range(_MAX_PASSES):
        previous, previous_expanded = current, expanded
        current, replaced = _expand_pass(current, ctx, pick_index)
        expanded += replaced
        if len(current) > limit:
            # 这一遍的结果不要：停在上一遍，宏序号也回到上一遍的进度。
            current = previous
            expanded = previous_expanded
            guarded = True
            break
        if replaced == 0:
            break

    return MacroResult(text=current, expanded=expanded, guarded=guarded)
