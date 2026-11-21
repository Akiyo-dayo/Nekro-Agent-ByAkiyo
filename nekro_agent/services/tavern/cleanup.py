"""卡片自带的正则清理规则：解析、默认停用与限时执行

卡片里的 `extensions.regex_scripts` 是网上来的正则，每条出站消息都要跑。标准库
`re` 只能在一次匹配结束后计时，遇到灾难性回溯就停不下来；这里用第三方 `regex`
包，它的 `sub` / `search` 带 `timeout`，能在执行中途抛 `TimeoutError`。它原本只是
`tiktoken` 的间接依赖，现在在 `pyproject.toml` 里单列成直接依赖，免得上游哪天换掉它。

静态检查复用世界书与输出过滤那一套（反向引用、重复套重复、重复里用 `|`、多个
不定长重复、超长），挡不住的多项式回溯靠每次调用的超时兜底：单条最多
`per_call_seconds`，一条消息所有规则合计不超过 `budget_seconds`，用完剩下的规则
直接跳过。被掐断的那条这一次不替换，原文照发。

替换语义照酒馆 `runRegexScript`，用函数做替换而不是 Python 的替换模板：`{{match}}`
是整个匹配，`$1` / `$<name>` 是分组，缺的分组换成空串，每个代入的值先去
`trimStrings`，最后整段交给运行时展开宏。
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence, TypedDict

import regex

from nekro_agent.services.output_filter.rules import MAX_REGEX_LENGTH, RegexRejected, check_regex_safety
from nekro_agent.services.worldbook.safe_regex import _rewrite_named_groups

# 停用原因；存进规则里给扩展页显示，也是导入报告的分类。
NOT_AI_OUTPUT = "not_ai_output"
DISABLED_IN_CARD = "disabled_in_card"
PROMPT_ONLY = "prompt_only"
FIND_MACROS = "find_macros"
DEPTH_LIMITED = "depth_limited"
INVALID_REGEX = "invalid_regex"
UNSAFE_REGEX = "unsafe_regex"
HTML_OUTPUT = "html_output"
# 管理员在导入选项里手动关掉的规则。与卡片自带的停用原因分开，管理页才能区分
# 「这张卡本来就不启用它」与「我导入时没要它」。
MANUAL = "manual"

# `/pattern/flags` 形态：`(.+)` 贪婪到最后一个 `/`，模式里自带 `/` 也能用。
_SLASH_FORM = re.compile(r"^/(.+)/([A-Za-z]*)$")
# 替换文字里的分组引用。
_GROUP_REF = re.compile(r"\$(?:(\d+)|<([^>]+)>)")
# 酒馆用 `{{match}}` 表示整个匹配，大小写不敏感。
_MATCH_PLACEHOLDER = re.compile(r"\{\{match\}\}", re.IGNORECASE)
# 去掉分组引用与 `{{match}}` 之后，看还有没有 HTML 标签。
_HTML_TAG = re.compile(r"<[A-Za-z/!]")

_FLAG_BITS = {
    "i": regex.IGNORECASE,
    "s": regex.DOTALL,
    "m": regex.MULTILINE,
}
# 酒馆里合法但对 Python 没有对应语义的旗标：忽略。
_IGNORED_FLAGS = frozenset({"y", "u"})

# 手动保存时替换文字与 trim 的条数上限。
MAX_REPLACE_CHARS = 4000
MAX_TRIM_STRINGS = 20
# 停用规则只防存储被撑爆：导入时脚本的查找、替换、去除串都原样保存、不截断（状态栏
# 一类的替换文字常有上万字），所以这里不能用上面那组给「要运行的规则」定的上限，否则
# 刚导入的停用规则一回传就存不进去。单个字符串与卡片文字字段同一个上限，去除串条数
# 给一个远大于真实卡片的数。
MAX_DISABLED_RULE_TEXT_CHARS = 64000
MAX_DISABLED_TRIM_STRINGS = 1000


class CleanupRule(TypedDict):
    """一条存进 `preset_card.cleanup_rules` 的规则

    `original` 是酒馆脚本的整份深拷贝，导出时按它还原；`enabled` 与
    `disabled_reason` 决定导入后默认是否生效，管理员改了再启用时仍要过安全检查。
    """

    name: str
    find: str
    replace: str
    trim: list[str]
    enabled: bool
    disabled_reason: str
    original: dict[str, Any] | None


@dataclass(frozen=True)
class ParsedFind:
    """解析后的查找正则

    `error` 非空表示不可用；`error_kind` 是 `invalid_regex` 或 `unsafe_regex`，
    导入时直接作为停用原因。
    """

    pattern: str
    flags: int
    global_: bool
    error: str
    error_kind: str


@dataclass(frozen=True)
class CompiledRule:
    """编译好的一条规则；`index` 是它在原列表里的序号"""

    index: int
    pattern: regex.Pattern[str]
    global_: bool
    replace: str
    trim: tuple[str, ...]


@dataclass(frozen=True)
class CleanupResult:
    """一次清理的结果

    Attributes:
        text: 清理后的文字。
        applied: 实际发生了替换的规则条数。
        timed_out: 因超时这一次没替换的规则序号。
        skipped_by_budget: 因整条消息预算用完而没跑的规则序号。
    """

    text: str
    applied: int
    timed_out: list[int]
    skipped_by_budget: list[int]


def parse_find(find: str) -> ParsedFind:
    """解析 `/pattern/flags` 或整条模式，并做长度、复杂度与编译检查

    `y` / `u` 没有对应语义，忽略；`g` 决定替换全部还是第一处。其它字母判无效，
    与酒馆相近：拼错的旗标不能悄悄变成“不区分大小写”之类的另一种语义。
    """
    matched = _SLASH_FORM.match(find)
    if matched is not None:
        pattern, flag_text = matched.group(1), matched.group(2)
    else:
        pattern, flag_text = find, ""

    flags = 0
    global_ = False
    for flag in flag_text:
        if flag in _FLAG_BITS:
            flags |= _FLAG_BITS[flag]
        elif flag == "g":
            global_ = True
        elif flag in _IGNORED_FLAGS:
            continue
        else:
            return ParsedFind(pattern, flags, global_, f"不支持的旗标 {flag!r}", INVALID_REGEX)

    if not pattern:
        return ParsedFind(pattern, flags, global_, "正则为空", INVALID_REGEX)

    pattern = _rewrite_named_groups(pattern)
    if len(pattern) > MAX_REGEX_LENGTH:
        return ParsedFind(pattern, flags, global_, f"正则过长（最多 {MAX_REGEX_LENGTH} 字）", UNSAFE_REGEX)

    # 先试编译，语法错误归到 `invalid_regex`；安全检查基于标准库 `re` 的语法树，
    # 它自己也会把解析失败报成拒绝，但那属于语法问题，不该显示成“不安全”。
    try:
        regex.compile(pattern, flags | regex.V0)
    except Exception:  # noqa: BLE001 - 编译失败就是不可用，不把底层文本带出去
        return ParsedFind(pattern, flags, global_, "正则语法错误", INVALID_REGEX)
    try:
        check_regex_safety(pattern)
    except RegexRejected as exc:
        return ParsedFind(pattern, flags, global_, str(exc), UNSAFE_REGEX)

    return ParsedFind(pattern, flags, global_, "", "")


def _to_int(value: Any) -> int:
    """把酒馆脚本里的数字字段转成整数；转不出来按 0

    布尔不算数字：`substituteRegex: true` 是坏数据，不能因为 Python 里 `True == 1`
    就把它当成“查找里含宏”。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _is_positive_number(value: Any) -> bool:
    """`minDepth` 是否是大于 0 的数字（布尔不算）"""
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float)) and value > 0


def has_html_output(replace: str) -> bool:
    """替换文字去掉分组引用后是否还含 HTML 标签

    酒馆靠 HTML 渲染状态栏，QQ 渲染不了；这类脚本在导入时默认停用，不直接拒绝，
    管理员可以自己改掉标签再启用。
    """
    cleaned = _MATCH_PLACEHOLDER.sub("", replace)
    cleaned = re.sub(r"\$\d+", "", cleaned)
    cleaned = re.sub(r"\$<[^>]+>", "", cleaned)
    return _HTML_TAG.search(cleaned) is not None


def rule_from_st_script(script: Mapping[str, Any]) -> CleanupRule:
    """把一条酒馆正则脚本转成 NA 的清理规则

    按固定顺序判第一条停用原因：只要有一条命中，后面就不再判——顺序决定了报告里
    显示的原因，先判「不是 AI 输出」比先判「正则不安全」更能说明这条脚本本来就不
    该跑。
    """
    name = script.get("scriptName")
    find = script.get("findRegex")
    replace = script.get("replaceString")
    trim_raw = script.get("trimStrings")
    trim = [item for item in trim_raw if isinstance(item, str)] if isinstance(trim_raw, list) else []

    reason = ""
    parsed: ParsedFind | None = None
    placement = script.get("placement")
    if not isinstance(placement, list) or 2 not in placement:
        reason = NOT_AI_OUTPUT
    elif bool(script.get("disabled")):
        reason = DISABLED_IN_CARD
    elif bool(script.get("promptOnly")) and not bool(script.get("markdownOnly")):
        # 只有「只改提示词」时才停用。酒馆的判据是：`markdownOnly` 作用于显示，`promptOnly` 作用于
        # 发给模型的提示词，两个都开就两处都作用（SillyTavern 1.19.0
        # `public/scripts/extensions/regex/engine.js:348-354`）。NA 没有单独的显示层，发出去的就是
        # 显示的，所以两个都开的脚本（例如删套话的「杀八股」）按普通规则作用于发出去的消息。
        reason = PROMPT_ONLY
    elif _to_int(script.get("substituteRegex")) != 0:
        reason = FIND_MACROS
    elif _is_positive_number(script.get("minDepth")):
        reason = DEPTH_LIMITED
    else:
        parsed = parse_find(find if isinstance(find, str) else "")
        if parsed.error:
            reason = parsed.error_kind
        elif has_html_output(replace if isinstance(replace, str) else ""):
            reason = HTML_OUTPUT

    return CleanupRule(
        name=name if isinstance(name, str) else "",
        find=find if isinstance(find, str) else "",
        replace=replace if isinstance(replace, str) else "",
        trim=trim,
        enabled=not reason,
        disabled_reason=reason,
        original=copy.deepcopy(dict(script)),
    )


def validate_rule(rule: Mapping[str, Any]) -> str | None:
    """手动保存规则时的校验；通过返回 `None`，否则返回中文原因

    这里**不**因 HTML 输出拒绝：那只在导入时作为默认停用的理由，管理员明确保存
    的规则按他填的算。
    """
    find = rule.get("find")
    if not isinstance(find, str):
        return "查找正则必须是字符串"
    parsed = parse_find(find)
    if parsed.error:
        return parsed.error

    replace = rule.get("replace", "")
    if not isinstance(replace, str):
        return "替换文字必须是字符串"
    if len(replace) > MAX_REPLACE_CHARS:
        return f"替换文字最多 {MAX_REPLACE_CHARS} 字"

    trim = rule.get("trim", [])
    if not isinstance(trim, list):
        return "trimStrings 必须是数组"
    if any(not isinstance(item, str) for item in trim):
        return "trimStrings 必须都是字符串"
    if len(trim) > MAX_TRIM_STRINGS:
        return f"trimStrings 最多 {MAX_TRIM_STRINGS} 个"
    return None


def validate_disabled_rule_shape(rule: Mapping[str, Any]) -> str | None:
    """停用规则只查形状，不解析正则

    停用的规则不参与运行（`compile_rules` 只编译启用的），所以坏正则留着也不会跑；
    而导入时因 `invalid_regex` / `unsafe_regex` 停用的规则本来就过不了
    `validate_rule`，前端把整张表传回来保存时会连带把表锁死。

    上限也不能沿用启用规则的那组（正则 200 字、替换 4000 字、去除串 20 个）：导入时
    脚本原样保存、不截断，「正则过长」停用的规则查找串本来就超过 200 字，`html_output`
    停用的规则替换文字常有上万字。这里只挡住会撑坏存储的离谱值，见
    `MAX_DISABLED_RULE_TEXT_CHARS` 与 `MAX_DISABLED_TRIM_STRINGS`。管理员把这样的规则
    打开时，照样要过 `validate_rule`。
    """
    find = rule.get("find")
    if not isinstance(find, str):
        return "查找正则必须是字符串"
    if len(find) > MAX_DISABLED_RULE_TEXT_CHARS:
        return f"查找串最多 {MAX_DISABLED_RULE_TEXT_CHARS} 字"

    replace = rule.get("replace", "")
    if not isinstance(replace, str):
        return "替换文字必须是字符串"
    if len(replace) > MAX_DISABLED_RULE_TEXT_CHARS:
        return f"替换文字最多 {MAX_DISABLED_RULE_TEXT_CHARS} 字"

    trim = rule.get("trim", [])
    if not isinstance(trim, list):
        return "trimStrings 必须是数组"
    if any(not isinstance(item, str) for item in trim):
        return "trimStrings 必须都是字符串"
    if len(trim) > MAX_DISABLED_TRIM_STRINGS:
        return f"trimStrings 最多 {MAX_DISABLED_TRIM_STRINGS} 个"
    if any(len(item) > MAX_DISABLED_RULE_TEXT_CHARS for item in trim):
        return f"每个 trimStrings 最多 {MAX_DISABLED_RULE_TEXT_CHARS} 字"
    return None


def compile_rules(rules: Sequence[Mapping[str, Any]]) -> list[CompiledRule]:
    """只编译默认启用且查找正则合法的规则；停用与坏规则保留在原列表里"""
    compiled: list[CompiledRule] = []
    for index, rule in enumerate(rules):
        if not rule.get("enabled"):
            continue
        find = rule.get("find")
        if not isinstance(find, str):
            continue
        parsed = parse_find(find)
        if parsed.error:
            continue
        try:
            pattern = regex.compile(parsed.pattern, parsed.flags | regex.V0)
        except Exception:  # noqa: BLE001 - 编译不过的规则不参与执行
            continue
        replace = rule.get("replace")
        trim_raw = rule.get("trim")
        trim = tuple(item for item in trim_raw if isinstance(item, str)) if isinstance(trim_raw, list) else ()
        compiled.append(
            CompiledRule(
                index=index,
                pattern=pattern,
                global_=parsed.global_,
                replace=replace if isinstance(replace, str) else "",
                trim=trim,
            ),
        )
    return compiled


def _trim_value(value: str, trim: tuple[str, ...]) -> str:
    """按顺序去掉代入值里的 trim 串"""
    for item in trim:
        if item:
            value = value.replace(item, "")
    return value


def _group_value(match: regex.Match[str], reference: str | int) -> str:
    """取一个分组的值；没参与匹配、为空或不存在时都算空串"""
    try:
        value = match.group(int(reference) if isinstance(reference, str) and reference.isdigit() else reference)
    except (IndexError, KeyError):
        return ""
    return value or ""


def _build_replacement(
    replace: str,
    trim: tuple[str, ...],
    expand: Callable[[str], str],
    match: regex.Match[str],
) -> str:
    """按酒馆的语义拼出一处替换文字"""
    template = _MATCH_PLACEHOLDER.sub("$0", replace)

    def substitute_group(reference: regex.Match[str]) -> str:
        if reference.group(1) is not None:
            value = _group_value(match, reference.group(1))
        else:
            value = _group_value(match, reference.group(2))
        return _trim_value(value, trim)

    substituted = _GROUP_REF.sub(substitute_group, template)
    return expand(substituted)


def apply_compiled(
    text: str,
    compiled: Sequence[CompiledRule],
    *,
    expand: Callable[[str], str],
    per_call_seconds: float = 0.05,
    budget_seconds: float = 0.1,
    clock: Callable[[], float] = time.monotonic,
) -> CleanupResult:
    """按顺序执行清理规则，每条调用带超时，整条消息共用一份预算

    超时的那一条这一次不替换（文字保持它之前的样子），剩下的继续跑；预算用完则
    剩余规则全部跳过。`expand` 只作用于替换文字，不碰原文。
    """
    started_at = clock()
    current = text
    applied = 0
    # 超时的规则按它拿到的整段时限记账：`regex` 抛超时时可能略早于时限，若只按
    # 墙上时间算，预算会剩下几毫秒，后面那条坏规则又被放进来再超时一次。
    charged = 0.0
    timed_out: list[int] = []
    skipped_by_budget: list[int] = []

    for rule in compiled:
        spent = max(clock() - started_at, charged)
        remaining = budget_seconds - spent
        if remaining <= 0:
            skipped_by_budget.append(rule.index)
            continue
        timeout = min(per_call_seconds, remaining)
        if timeout <= 0:
            skipped_by_budget.append(rule.index)
            continue

        replaced = 0

        def replace_match(match: regex.Match[str]) -> str:
            nonlocal replaced
            replaced += 1
            return _build_replacement(rule.replace, rule.trim, expand, match)

        try:
            new_text = rule.pattern.sub(
                replace_match,
                current,
                count=0 if rule.global_ else 1,
                timeout=timeout,
            )
        except TimeoutError:
            timed_out.append(rule.index)
            charged = spent + timeout
            continue
        if replaced:
            applied += 1
            current = new_text

    return CleanupResult(
        text=current,
        applied=applied,
        timed_out=timed_out,
        skipped_by_budget=skipped_by_budget,
    )
