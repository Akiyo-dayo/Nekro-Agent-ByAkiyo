"""发送路径上的输出清理：把卡片规则接到通用发送服务

## 为什么单独一层

`cleanup.py` 只负责「编译一条规则、按预算执行」，不知道频道、人设与宏上下文；发送服务
也不知道卡片表。这一层把两边接起来：按频道找到生效人设的扩展行，编译并缓存规则，再在
发送前把文字交给 `apply_compiled`。放在这里而不是发送服务里，是为了让发送服务只多一个
调用点，测试也能单独钉住查库、缓存与停用登记。

## 缓存

每条出站消息都查「频道 → 人设 → 扩展行」两次库代价不小，而频道生效人设与规则在几秒内
几乎不会变。这里缓存两层：

- `chat_key → (preset_id, 过期时刻)` 十秒，省掉频道与人设的两次查询；
- `(preset_id, card.update_time)` → 编译结果，规则真的改过（`update_time` 变了）就自然
  失效，不必等十秒。

两个缓存都有上限，满了丢最早一半，避免长期运行后按频道数无限增长。

## 停用太慢的规则

`regex` 的超时能掐断单次匹配，但一条每次都要超时的规则会让每条消息都白白花掉预算。
同一条（人设, 规则序号）累计超时三次就在本进程停用，并把停用登记暴露给扩展页；
管理员保存规则时 `forget_preset` 清掉登记，给这条规则一次重试的机会。
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Optional

from nekro_agent.core.config import config as global_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset_card import DBPresetCard

from .cleanup import OUTPUT_REPLACE, CompiledRule, apply_compiled, compile_rules
from .macros import MacroContext, expand_macros, sanitize_user_name

logger = get_sub_logger("tavern.cleanup_runtime")

# 编译缓存的上限。频道生效人设**不**缓存：换人设的入口很多（频道绑定、实例默认人设、
# 系统默认人设、删人设后的回退），挂失效钩子迟早漏一个，而换人设后的第一条消息恰恰是
# 开场白——用旧人设的规则去清理它最容易出错。每次发送按频道现查生效人设，规则本身按
# （人设, 扩展行更新时间）缓存编译结果。
MAX_CACHE_ENTRIES = 1024
# 一条规则累计超时这么多次就在本进程停用。
MAX_TIMEOUTS = 3

# 替换文字里出现这些记号才需要建宏上下文；建上下文要查一次最近发言人的昵称，
# 绝大多数规则（状态栏清理之类）用不到，不该为它们多查一次库。
_MACRO_MARK = re.compile(r"<(?:USER|BOT|CHAR)>", re.IGNORECASE)


@dataclass(frozen=True)
class RuleSet:
    """一个频道当前生效人设的清理规则

    `rule_names` 的键是规则在原列表里的序号，用来在停用日志里写明是哪一条；
    日志只写名字与序号，不带正文。
    """

    preset_id: int
    compiled: tuple[CompiledRule, ...]
    preset_name: str
    nickname: str
    rule_names: dict[int, str]


# (人设 ID, 扩展行更新时间) → 编译结果。
_compiled_cache: dict[tuple[int, Any], RuleSet] = {}
# (人设 ID, 规则序号) → 累计超时次数 / 停用时的规则名。
_timeouts: dict[tuple[int, int], int] = {}
_disabled: dict[tuple[int, int], str] = {}


def _drop_oldest_half(cache: dict[Any, Any]) -> None:
    """缓存满了丢最早写入的一半；dict 的插入顺序就是最早到最晚"""
    for key in list(cache)[: max(1, len(cache) // 2)]:
        cache.pop(key, None)


def _store(cache: dict[Any, Any], key: Any, value: Any) -> None:
    """写缓存并维持上限"""
    if len(cache) >= MAX_CACHE_ENTRIES:
        _drop_oldest_half(cache)
    cache[key] = value


def _needs_expand(replace_text: str) -> bool:
    """替换文字里有没有宏或旧式占位"""
    return "{{" in replace_text or _MACRO_MARK.search(replace_text) is not None


async def _latest_user_name(chat_key: str) -> str:
    """取这个频道最近一条非 Bot 消息的昵称，清洗后作为 `{{user}}`"""
    latest = (
        await DBChatMessage.filter(chat_key=chat_key)
        .exclude(sender_id="-1")
        .order_by("-id")
        .first()
    )
    nickname = str(latest.sender_nickname or "").strip() if latest is not None else ""
    return sanitize_user_name(nickname or None)


async def _resolve_preset(chat_key: str) -> Optional[tuple[int, str]]:
    """查频道的生效人设；频道不存在时返回 `None`，内置人设按 ID 0 返回"""
    channel = await DBChatChannel.filter(chat_key=chat_key).order_by("id").first()
    if channel is None:
        return None
    preset = await channel.get_preset()
    preset_id = getattr(preset, "id", None)
    name = str(getattr(preset, "name", "") or "")
    if not isinstance(preset_id, int) or preset_id <= 0:
        return 0, name
    return preset_id, name


def _active_rules(rules: RuleSet) -> Optional[RuleSet]:
    """去掉本进程已停用的规则；一条都不剩时按没有规则处理"""
    if not _disabled:
        return rules
    active = tuple(rule for rule in rules.compiled if (rules.preset_id, rule.index) not in _disabled)
    if not active:
        return None
    if len(active) == len(rules.compiled):
        return rules
    return replace(rules, compiled=active)


async def rules_for_chat(chat_key: str) -> Optional[RuleSet]:
    """取这个频道当前生效人设的清理规则；不需要清理时返回 `None`

    闸关、频道不存在、没扩展行、没启用规则都返回 `None`，调用方据此一个字都不改。
    任何异常都吞掉并记 WARNING：清理是附加步骤，不能拦住发送。
    """
    # 回滚闸是全局项、不可覆盖；关着时一次表都不查。
    if not bool(global_config.TAVERN_COMPAT_ENABLED):
        return None

    try:
        resolved = await _resolve_preset(chat_key)
        if resolved is None:
            return None
        preset_id, preset_name = resolved

        # 内置默认人设没有行 ID，也就不可能有扩展行。
        if preset_id <= 0:
            return None

        card = await DBPresetCard.get_or_none(preset_id=preset_id)
        if card is None:
            return None
        raw_rules = card.cleanup_rules if isinstance(card.cleanup_rules, list) else []
        if not raw_rules:
            return None

        cache_key = (preset_id, card.update_time)
        base = _compiled_cache.get(cache_key)
        if base is None:
            compiled = compile_rules(raw_rules)
            if not compiled:
                return None
            names: dict[int, str] = {}
            for index in (rule.index for rule in compiled):
                raw = raw_rules[index] if 0 <= index < len(raw_rules) else {}
                name = raw.get("name") if isinstance(raw, dict) else ""
                names[index] = name if isinstance(name, str) else ""
            nickname = str(card.nickname or "") if card.nickname else ""
            base = RuleSet(
                preset_id=preset_id,
                compiled=tuple(compiled),
                preset_name=preset_name,
                nickname=nickname,
                rule_names=names,
            )
            _store(_compiled_cache, cache_key, base)

        return _active_rules(base)
    except Exception as error:  # noqa: BLE001 - 清理取规则失败不能让发送挂掉
        logger.warning(f"读取酒馆清理规则失败，本次不清理: chat_key={chat_key}, error={error!r}")
        return None


def _account_timeouts(rules: RuleSet, timed_out: list[int]) -> None:
    """累计超时次数，到三次就在本进程停用；日志不带正文"""
    for index in timed_out:
        key = (rules.preset_id, index)
        count = _timeouts.get(key, 0) + 1
        _timeouts[key] = count
        name = rules.rule_names.get(index, "")
        if count < MAX_TIMEOUTS or key in _disabled:
            logger.debug(f"清理规则超时: preset_id={rules.preset_id}, rule_index={index}, name={name}, count={count}")
            continue
        _disabled[key] = name
        logger.warning(
            f"清理规则累计超时 {count} 次，已在本进程停用: preset_id={rules.preset_id}, rule_index={index}, name={name}",
        )


async def apply_cleanup(chat_key: str, text: str, rules: RuleSet) -> str:
    """对一条出站文字执行清理；任何异常都返回原文

    只有替换文字里出现宏记号时才建宏上下文：`{{char}}` 用昵称或人设名，
    `{{user}}` 取频道最近一条非 Bot 消息的昵称（每条消息最多查一次）。
    """
    try:
        expand = _identity
        # 「只留正文」「整段删掉」不输出替换文字，替换文字里的宏用不上，不为它们查昵称。
        if any(rule.mode == OUTPUT_REPLACE and _needs_expand(rule.replace) for rule in rules.compiled):
            macro = MacroContext(
                char_name=rules.nickname or rules.preset_name,
                user_name=await _latest_user_name(chat_key),
                now=datetime.now(),
                lang=str(global_config.SYSTEM_LANG),
                chat_key=chat_key,
                preset_id=rules.preset_id,
                text_kind="cleanup",
            )
            expand = lambda value: expand_macros(value, macro).text  # noqa: E731 - 只在这里用一次

        result = apply_compiled(
            text,
            rules.compiled,
            expand=expand,
            per_call_seconds=0.05,
            budget_seconds=0.1,
        )
        _account_timeouts(rules, result.timed_out)
        for index in result.skipped_by_budget:
            logger.debug(
                f"清理预算用完，跳过规则: preset_id={rules.preset_id}, rule_index={index}, "
                f"name={rules.rule_names.get(index, '')}",
            )
        return result.text
    except Exception as error:  # noqa: BLE001 - 发送不能因为清理挂掉
        logger.warning(
            f"酒馆输出清理失败，原文照发: chat_key={chat_key}, preset_id={rules.preset_id}, error={error!r}",
        )
        return text


def _identity(text: str) -> str:
    """没有宏上下文时的展开函数：原样返回"""
    return text


# ---------------------------------------------------------------------------
# 内置剥离：变量标记
# ---------------------------------------------------------------------------
#
# MVU 一类的卡让模型在回复末尾写变量更新块（`<UpdateVariable>…</UpdateVariable>`，里面是
# `<Analysis>` 与 `<JSONPatch>`），酒馆靠一条「只改显示」的正则把它从界面上藏掉、靠 MVU 自己
# 把它从提示词里删掉。NA 导入卡片时那条正则因为输出 HTML 或只改提示词被停用，变量命令也没人
# 解析——模型一旦写出这种块，就会原样发到群里。所以发送路径上内置一步剥离，不依赖卡片规则：
#
# - 成对的块：`UpdateVariable` / `VariableUpdate` / `JSONPatch` / `status_current_variable` / `initvar`，
#   标签名不分大小写、不看下划线（`JSONPatch` 与 `</json_patch>` 能配上），同名嵌套按层数配对，
#   连同内容整块去掉（开场白里的 `<initvar>` 是给角色状态的初值，在发送前另行取走，见 `greeting`）；
# - 落单的结束标签去掉；
# - 自闭合的占位标签 `<StatusPlaceHolderImpl/>` 去掉。
#
# 下面两种只对**从酒馆卡导入的人设**做（`card_mode`）：别的人设的群里，普通对话也可能写出同名
# 的 JSX（`<JsonPatch op={x} />`），不该为此截掉半条消息：
#
# - 只有开始标签、没有结束标签（模型写到一半被截断）的，从开始标签删到末尾；
# - 以 `/>` 结尾的自闭合变量标签去掉（非卡片人设里原样保留）。
#
# 顺序是：内置剥离 → 卡片清理规则 → 展示标签转纯文本（`html_text`，只对酒馆卡人设）。剥离在前，
# 卡片规则看到的就是去掉了变量块的正文，不会因为变量块里的文字误伤；也不会让「只留正文」的美化规则
# 先把 `<UpdateVariable>` 换成里面的 JSON 正文，让剥离再也找不到标记。裸写的 `_.set(...)` 命令、通用的 `<Update>`、
# 名字里夹了零宽字符或全角尖括号的变体都不剥：在普通对话里误伤的风险比漏掉的风险大。
_VARIABLE_BLOCK_NAMES = r"UpdateVariable|VariableUpdate|JSON_?Patch|status_current_variable|initvar"
# 开始标签里属性最多 1000 字。空白与属性都封顶，`[^>]*`、`.*?` 这类不封顶的写法在大量未闭合的
# 标签上会对每个起点扫到末尾（二次方），而出站文字是模型写的、群里的人能左右。属性里也不许
# 出现 `<`：几十万个没有 `>` 的开始标签连在一起时，每个候选扫到下一个 `<` 就停。
MAX_TAG_ATTR_CHARS = 1000
# 标签名按 ASCII 忽略大小写（`(?a:…)` 只管名字这一段）：Unicode 的忽略大小写会让 `ſ`（U+017F）配上 `s`、
# 开尔文符号（U+212A）配上 `k`，`<JſONPatch>` 就被当成变量标签（成对的整块剥掉，卡片人设里落单的开始标签
# 还会把回复从那里截到末尾）；酒馆的正则（JS 不带 `u` 的 `/i`）不这样折叠。
# 名字外面的 `\s`、`\b` 仍按 Unicode。
_TAG_HEAD = re.compile(rf"<\s{{0,8}}(/?)\s{{0,8}}((?a:{_VARIABLE_BLOCK_NAMES}))\b", re.IGNORECASE)
_TAG_TAIL = re.compile(rf"[^<>]{{0,{MAX_TAG_ATTR_CHARS}}}>")
_STATUS_PLACEHOLDER = re.compile(r"<\s{0,8}(?a:StatusPlaceHolderImpl)\s{0,8}/?\s{0,8}>", re.IGNORECASE)
_MARKUP_HINT = re.compile(rf"<\s{{0,8}}/?\s{{0,8}}(?a:{_VARIABLE_BLOCK_NAMES}|StatusPlaceHolderImpl)", re.IGNORECASE)


def _tag_name(raw: str) -> str:
    """配对用的规范名：小写、去下划线"""
    return raw.lower().replace("_", "")


@dataclass(frozen=True)
class _Tag:
    start: int
    end: int
    closing: bool
    name: str
    self_closing: bool


def _scan_tags(text: str) -> list[_Tag]:
    """找出全部变量标签；单遍、线性

    开始标签收不了尾（属性里有 `<`，或者属性超过上限）时仍记成一个开始标签，块从标签头算起：
    不这样做的话，它的结束标签会被当成落单的删掉，正文连同半个开始标签反而漏出去。
    收不了尾的结束标签不认。
    """
    tags: list[_Tag] = []
    for head in _TAG_HEAD.finditer(text):
        closing = head.group(1) == "/"
        name = _tag_name(head.group(2))
        tail = _TAG_TAIL.match(text, head.end())
        if tail is None:
            if not closing:
                tags.append(_Tag(head.start(), head.end(), False, name, False))
            continue
        self_closing = not closing and tail.group(0)[:-1].rstrip().endswith("/")
        tags.append(_Tag(head.start(), tail.end(), closing, name, self_closing))
    return tags


def builtin_strip_enabled() -> bool:
    """内置剥离跟着酒馆兼容的回滚闸：闸关时出站文字一个字都不改"""
    return bool(global_config.TAVERN_COMPAT_ENABLED)


def strip_variable_markup(text: str, *, card_mode: bool = False) -> str:
    """去掉出站文字里的变量更新块与状态占位标签；没有这些标签时原样返回（同一个对象）

    结束标签按规范名、用每个名字一个栈与开始标签配对（同名嵌套自然配上），配上的整块去掉，
    几块重叠的合在一起去掉；没有开始标签可配的结束标签单独去掉。没配上的开始标签：普通模式下
    当普通文字留着、后面照常剥；`card_mode` 时从第一个（不在已去掉的块里的）起删到末尾。
    自闭合变量标签只在 `card_mode` 时去掉。整段线性。
    """
    if "<" not in text or _MARKUP_HINT.search(text) is None:
        return text
    stacks: dict[str, list[int]] = {}
    intervals: list[tuple[int, int]] = []
    for tag in _scan_tags(text):
        if tag.self_closing:
            if card_mode:
                intervals.append((tag.start, tag.end))
            continue
        if tag.closing:
            stack = stacks.get(tag.name)
            intervals.append((stack.pop() if stack else tag.start, tag.end))
            continue
        stacks.setdefault(tag.name, []).append(tag.start)

    merged = _merge(intervals)
    if card_mode:
        leftovers = sorted(start for stack in stacks.values() for start in stack)
        for start in leftovers:
            if not _covered(merged, start):
                merged = _merge([*merged, (start, len(text))])
                break

    kept: list[str] = []
    position = 0
    for start, end in merged:
        kept.append(text[position:start])
        position = end
    kept.append(text[position:])
    stripped = _STATUS_PLACEHOLDER.sub("", "".join(kept))
    if stripped == text:
        return text
    # 块通常单独占几行：去掉之后留下的多余空行收一收，末尾的空白去掉。
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.rstrip()


# 模型的思考与分析区域（MVU 0.196 遮掉的那几种）：取变量块之前先在整条消息上遮掉，包在这里面的块不算数。
# 标签名按 ASCII 忽略大小写，属性封顶，单遍。
_REASONING_TAG = re.compile(
    r"<\s{0,8}(/?)\s{0,8}((?a:think|thinking|reasoning|analysis|analyze))\b[^<>]{0,200}>", re.IGNORECASE
)


def mask_reasoning_regions(text: str) -> str:
    """去掉思考与分析区域：成对的整段去掉（结束标签关到最近一个同名的开始标签，中间没配上的一起算进这一段），
    没闭合的从最早那个开始标签去到末尾。线性：每个开始标签最多进栈、出栈各一次。只给取块用，不改发出去的文字。
    """
    if "<" not in text:
        return text
    spans: list[tuple[int, int]] = []
    stack: list[tuple[str, int]] = []
    by_name: dict[str, list[int]] = {}
    for tag in _REASONING_TAG.finditer(text):
        name = tag.group(2).lower()
        if not tag.group(1):
            by_name.setdefault(name, []).append(len(stack))
            stack.append((name, tag.start()))
            continue
        positions = by_name.get(name)
        if not positions:
            continue
        index = positions[-1]
        spans.append((stack[index][1], tag.end()))
        while len(stack) > index:
            popped_name, _start = stack.pop()
            by_name[popped_name].pop()
    if stack:
        spans.append((min(start for _name, start in stack), len(text)))
    if not spans:
        return text
    kept: list[str] = []
    position = 0
    for start, end in sorted(spans):
        if start > position:
            kept.append(text[position:start])
        position = max(position, end)
    kept.append(text[position:])
    return "".join(kept)


# 交给角色状态「兼容解析」的块：只取更新类的三种（`initvar` 是开场白的初值，`status_current_variable`
# 是给界面显示的整份变量，都不是更新）。单块超过上限的整块不取（截一半的命令会被读成别的意思），
# 一段文字最多取这么多块。
_UPDATE_BLOCK_NAMES = frozenset({"updatevariable", "variableupdate", "jsonpatch"})
MAX_EXTRACTED_BLOCK_CHARS = 16 * 1024
MAX_EXTRACTED_BLOCKS = 8


def extract_variable_blocks(text: str, *, card_mode: bool = False) -> list[str]:
    """`strip_variable_markup` 会去掉的更新块的正文（按出现顺序），给角色状态的兼容解析用

    配对规则与剥离逐条相同，取到的正好是剥离去掉的那些块：

    - 成对的块取开始标签与结束标签之间的正文；外层套内层（`<UpdateVariable>` 里的 `<JSONPatch>`）只取
      最外层那一块，内层留在它的正文里；
    - `card_mode` 时剥离会从第一个没配上的开始标签删到末尾：那个标签是更新类的，就取它后面到末尾的正文，
      落在这一段里的成对块不再单独取。

    只取不改，不解析正文。整段线性（标签扫描与剥离共用一遍单遍扫描）。
    """
    if "<" not in text or _MARKUP_HINT.search(text) is None:
        return []
    stacks: dict[str, list[_Tag]] = {}
    intervals: list[tuple[int, int]] = []
    pairs: list[tuple[int, int, int, bool]] = []  # (块起点, 正文起点, 正文终点, 是不是更新类)
    for tag in _scan_tags(text):
        if tag.self_closing:
            continue
        if tag.closing:
            stack = stacks.get(tag.name)
            opener = stack.pop() if stack else None
            intervals.append((opener.start if opener else tag.start, tag.end))
            if opener is not None:
                pairs.append((opener.start, opener.end, tag.start, tag.name in _UPDATE_BLOCK_NAMES))
            continue
        stacks.setdefault(tag.name, []).append(tag)

    cut: Optional[_Tag] = None
    if card_mode:
        merged = _merge(intervals)
        leftovers = sorted((tag for stack in stacks.values() for tag in stack), key=lambda tag: tag.start)
        for tag in leftovers:
            if not _covered(merged, tag.start):
                cut = tag
                break

    # 只取最外层的块：套在别的块（包括不是更新类的，比如 `<initvar>`）里面的不单独取。
    spans: list[tuple[int, int]] = []
    outer_end = -1
    for start, body_start, body_end, is_update in sorted(pairs, key=lambda item: (item[0], -item[2])):
        if start < outer_end or (cut is not None and start >= cut.start):
            continue
        outer_end = body_end
        if is_update:
            spans.append((body_start, body_end))
    if cut is not None and cut.name in _UPDATE_BLOCK_NAMES:
        spans.append((cut.end, len(text)))

    blocks: list[str] = []
    for body_start, body_end in spans:
        if body_end - body_start > MAX_EXTRACTED_BLOCK_CHARS:
            continue
        body = text[body_start:body_end].strip()
        if body:
            blocks.append(body)
        if len(blocks) >= MAX_EXTRACTED_BLOCKS:
            break
    return blocks


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covered(merged: list[tuple[int, int]], position: int) -> bool:
    index = bisect.bisect_right(merged, (position, float("inf"))) - 1
    return index >= 0 and merged[index][0] <= position < merged[index][1]


def needs_card_mode(stripped: str) -> bool:
    """普通剥离之后还剩开始或自闭合的变量标签：要不要再按卡片人设剥一遍"""
    if "<" not in stripped:
        return False
    return any(head.group(1) != "/" for head in _TAG_HEAD.finditer(stripped))


async def is_card_preset_chat(chat_key: str) -> bool:
    """这个频道的生效人设是不是从酒馆卡导入的（扩展行上记着卡片版本）

    只看「有没有扩展行」不够：管理员在任何人设的扩展页里存过一次（比如只加了一段预填），
    就会建出一行 `card_spec` 为空的扩展行。查不到或出错按不是处理。
    """
    try:
        resolved = await _resolve_preset(chat_key)
        if resolved is None or resolved[0] <= 0:
            return False
        return await DBPresetCard.filter(preset_id=resolved[0]).exclude(card_spec="").exists()
    except Exception as error:  # noqa: BLE001 - 判不出来就按保守的普通剥离走
        logger.warning(f"判断频道人设是否来自酒馆卡失败，按普通剥离处理: chat_key={chat_key}, error={error!r}")
        return False


def disabled_cleanup_rules() -> dict[tuple[int, int], str]:
    """本进程已停用的清理规则 `(人设 ID, 规则序号) → 规则名`，给扩展页显示用"""
    return dict(_disabled)


def forget_preset(preset_id: int) -> None:
    """清掉某个人设在本进程的停用与超时登记

    管理员在扩展页保存规则时调用：改过的规则应该重新获得完整的超时额度，不能因为
    改之前的旧规则太慢就一直停用。
    """
    for key in [key for key in _disabled if key[0] == preset_id]:
        _disabled.pop(key, None)
    for key in [key for key in _timeouts if key[0] == preset_id]:
        _timeouts.pop(key, None)


__all__ = [
    "MAX_CACHE_ENTRIES",
    "MAX_EXTRACTED_BLOCKS",
    "MAX_EXTRACTED_BLOCK_CHARS",
    "RuleSet",
    "apply_cleanup",
    "MAX_TAG_ATTR_CHARS",
    "builtin_strip_enabled",
    "disabled_cleanup_rules",
    "extract_variable_blocks",
    "forget_preset",
    "is_card_preset_chat",
    "mask_reasoning_regions",
    "needs_card_mode",
    "rules_for_chat",
    "strip_variable_markup",
]
