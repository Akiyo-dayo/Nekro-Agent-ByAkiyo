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
from nekro_agent.services.worldbook import unsupported as wb_unsupported
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

# 启用的规则命中以后怎么替换（`output_mode`，缺省按 `replace`）：
#
# - `replace` 按替换文字，酒馆的原语义；
# - `text` 只留正文：只输出替换文字里引用到的分组（`$1`、`$<名字>`、`{{match}}`）的内容，按出现顺序、
#   同一个分组只取一次、空的跳过，用换行连起来；替换文字里的 HTML 与固定文字都不要；
# - `drop` 整段删掉：命中的部分换成空串。
#
# 卡片里输出 HTML 的规则（状态栏、美化块、插图卡）在 QQ 里显示不了，导入时默认「只留正文」，替换文字里
# 带 `<img` 的（图卡：分组里是图片路径，留下来也没有意义）默认「整段删掉」。管理员可以逐条改成停用或按
# 替换文字。这两种方式不输出替换文字，所以替换文字的长度按停用规则的上限查，不按 4000 字。
OUTPUT_REPLACE = "replace"
OUTPUT_TEXT = "text"
OUTPUT_DROP = "drop"
OUTPUT_MODES = (OUTPUT_REPLACE, OUTPUT_TEXT, OUTPUT_DROP)
_IMAGE_TAG = re.compile(r"<\s{0,8}img\b", re.IGNORECASE)

# `/pattern/flags` 形态：`(.+)` 贪婪到最后一个 `/`，模式里自带 `/` 也能用。
_SLASH_FORM = re.compile(r"^/(.+)/([A-Za-z]*)$")
# 替换文字里的分组引用。
# 命名分组的名字封顶 64 字、不许出现 `<` `>`：不封顶的 `<([^>]+)>` 在大量没有 `>` 的 `$<` 上，每个起点都扫到
# 末尾，是二次方的；而替换文字来自卡片，「只留正文」的规则替换文字能有六万多字。
_GROUP_REF = re.compile(r"\$(?:(\d+)|<([^<>]{1,64})>)")
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
# 「开场白只是一个标记」的判据：去掉首尾空白后最多这么多字，规则匹配单次限时。
MAX_PLACEHOLDER_CHARS = 64
PLACEHOLDER_TIMEOUT = 0.05
# 一次挑开场白（或一次导入检查）判所有开场白合计最多花这么久；到点后剩下的按不是标记算
PLACEHOLDER_BUDGET_SECONDS = 0.2


class CleanupRule(TypedDict):
    """一条存进 `preset_card.cleanup_rules` 的规则

    `original` 是酒馆脚本的整份深拷贝，导出时按它还原；`enabled` 与
    `disabled_reason` 决定导入后默认是否生效，管理员改了再启用时仍要过安全检查。
    `output_mode` 见 :data:`OUTPUT_MODES`；旧数据没有这个键，按 `replace` 读。
    """

    name: str
    find: str
    replace: str
    trim: list[str]
    enabled: bool
    disabled_reason: str
    original: dict[str, Any] | None
    output_mode: str


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
    mode: str = OUTPUT_REPLACE
    # 「只留正文」要输出的分组，编译时算一次（替换文字可能有六万多字，不能每条消息都重扫）；
    # `None` 表示没预先算过（只有直接构造的规则才会这样），执行时现算
    groups: tuple[str | int, ...] | None = None


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
    cleaned = re.sub(r"\$<[^<>]{1,64}>", "", cleaned)
    return _HTML_TAG.search(cleaned) is not None


def default_html_output_mode(replace: str) -> str:
    """输出 HTML 的规则导入时默认的处理方式：带图片的整段删掉，其余只留正文"""
    return OUTPUT_DROP if _IMAGE_TAG.search(replace) else OUTPUT_TEXT


def html_kind_of(rule: Mapping[str, Any]) -> str:
    """替换文字带 HTML 时属于哪一类（纯展示 / 依赖外部资源 / 交互程序 / 远端代码），不带 HTML 为空串"""
    replace = rule.get("replace")
    if not isinstance(replace, str) or not has_html_output(replace):
        return ""
    return wb_unsupported.classify_code(replace)


def output_mode_of(rule: Mapping[str, Any]) -> str:
    """规则的处理方式；缺键或不认识的值按 `replace`（旧数据都是这样存的）"""
    mode = rule.get("output_mode")
    return mode if isinstance(mode, str) and mode in OUTPUT_MODES else OUTPUT_REPLACE


def rule_from_st_script(script: Mapping[str, Any]) -> CleanupRule:
    """把一条酒馆正则脚本转成 NA 的清理规则

    按固定顺序判第一条停用原因：只要有一条命中，后面就不再判——顺序决定了报告里
    显示的原因，先判「不是 AI 输出」比先判「正则不安全」更能说明这条脚本本来就不
    该跑。

    输出 HTML 的规则不再停用，而是启用并按 :func:`default_html_output_mode` 只留正文或整段删掉；
    只有这样启用以后过不了 :func:`validate_rule`（比如去除串太多）的，才照旧以 `html_output` 停用，
    免得规则表里留一条启用着却存不回去的规则。处理方式对**所有**替换带 HTML 的规则都给默认值，
    不管它因为什么停用：管理员以后打开它（卡里本来关着的、只改提示词的……）时，不该先按 4000 字的
    「按替换文字」上限报错。
    """
    name = script.get("scriptName")
    find = script.get("findRegex")
    replace = script.get("replaceString")
    trim_raw = script.get("trimStrings")
    trim = [item for item in trim_raw if isinstance(item, str)] if isinstance(trim_raw, list) else []
    replace_text = replace if isinstance(replace, str) else ""
    html = has_html_output(replace_text)

    reason = ""
    output_mode = default_html_output_mode(replace_text) if html else OUTPUT_REPLACE
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
        elif html:
            candidate = {"find": find, "replace": replace_text, "trim": trim, "output_mode": output_mode}
            if validate_rule(candidate) is not None:
                reason = HTML_OUTPUT

    return CleanupRule(
        name=name if isinstance(name, str) else "",
        find=find if isinstance(find, str) else "",
        replace=replace if isinstance(replace, str) else "",
        trim=trim,
        enabled=not reason,
        disabled_reason=reason,
        original=copy.deepcopy(dict(script)),
        output_mode=output_mode,
    )


def validate_rule(rule: Mapping[str, Any]) -> str | None:
    """手动保存规则时的校验；通过返回 `None`，否则返回中文原因

    这里**不**因 HTML 输出拒绝：那只决定导入时的默认处理方式，管理员明确保存
    的规则按他填的算。「只留正文」「整段删掉」不输出替换文字，替换文字按停用规则的
    上限查（导入的状态栏一类常有上万字）。
    """
    mode_error = _output_mode_error(rule)
    if mode_error is not None:
        return mode_error
    find = rule.get("find")
    if not isinstance(find, str):
        return "查找正则必须是字符串"
    parsed = parse_find(find)
    if parsed.error:
        return parsed.error

    replace = rule.get("replace", "")
    if not isinstance(replace, str):
        return "替换文字必须是字符串"
    replace_limit = MAX_REPLACE_CHARS if output_mode_of(rule) == OUTPUT_REPLACE else MAX_DISABLED_RULE_TEXT_CHARS
    if len(replace) > replace_limit:
        return f"替换文字最多 {replace_limit} 字"

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
    mode_error = _output_mode_error(rule)
    if mode_error is not None:
        return mode_error
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


def _output_mode_error(rule: Mapping[str, Any]) -> str | None:
    """`output_mode` 可以不写；写了就必须是三种之一"""
    if "output_mode" not in rule or rule.get("output_mode") is None:
        return None
    if rule.get("output_mode") not in OUTPUT_MODES:
        return "处理方式只能是 replace（按替换文字）、text（只留正文）或 drop（整段删掉）"
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
        replace_text = replace if isinstance(replace, str) else ""
        trim_raw = rule.get("trim")
        trim = tuple(item for item in trim_raw if isinstance(item, str)) if isinstance(trim_raw, list) else ()
        mode = output_mode_of(rule)
        compiled.append(
            CompiledRule(
                index=index,
                pattern=pattern,
                global_=parsed.global_,
                replace=replace_text,
                trim=trim,
                mode=mode,
                groups=tuple(_referenced_groups(replace_text, pattern)) if mode == OUTPUT_TEXT else (),
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


def _referenced_groups(replace: str, pattern: regex.Pattern[str] | None = None) -> list[str | int]:
    """替换文字里引用到的分组，按出现顺序、去重；`{{match}}` 记成 0

    给了 `pattern` 时只留它里面真有的分组（编号不超过分组数、名字在命名分组里）：替换文字可以写上
    一万个 `$n`，留下来的个数却被正则本身的长度封顶，每次命中逐个取分组的代价也就有了上限。去重用
    字典（插入有序），不在列表里查，几万个引用也是线性的；位数离谱的编号（超过 9 位）直接不要，
    `int()` 不会碰到位数上限。

    同时引用了 `{{match}}` 与具体分组时去掉 `{{match}}`：整个匹配里本来就含着那些分组，两个都留，
    正文会出现两遍。只有 `{{match}}` 时照留。
    """
    template = _MATCH_PLACEHOLDER.sub("$0", replace)
    seen: dict[str | int, None] = {}
    for reference in _GROUP_REF.finditer(template):
        digits = reference.group(1)
        if digits is not None:
            if len(digits) > 9:
                continue
            key: str | int = int(digits)
            if pattern is not None and key > pattern.groups:
                continue
        else:
            key = reference.group(2)
            if pattern is not None and key not in pattern.groupindex:
                continue
        seen.setdefault(key, None)
    keys = list(seen)
    if len(keys) > 1 and 0 in seen:
        keys.remove(0)
    return keys


def _build_text_only(groups: Sequence[str | int], trim: tuple[str, ...], match: regex.Match[str]) -> str:
    """「只留正文」：引用到的分组各取一次，空的跳过，用换行连起来

    不同分组的值相同也都留着（`HP $1 / $2` 遇到 `10/10` 要两个 10）；同一个分组引用几次只取一次。
    不展开宏：留下来的是模型写的正文，替换文字（宏所在的地方）一个字都不输出。
    """
    values = (_trim_value(_group_value(match, group), trim).strip() for group in groups)
    return "\n".join(value for value in values if value)


@dataclass(frozen=True)
class PlaceholderRule:
    """判「开场白只是标记」用的一条规则，预先编译好；见 :func:`placeholder_rules`"""

    name: str
    pattern: regex.Pattern[str]
    global_: bool
    groups: tuple[str | int, ...]
    trim: tuple[str, ...]


# 这几种停用原因的规则在酒馆显示开场白时本来就不套用：不作用于 AI 输出、卡里关着、只改提示词。
_NOT_APPLIED_ON_DISPLAY = frozenset({NOT_AI_OUTPUT, DISABLED_IN_CARD, PROMPT_ONLY})


def placeholder_rules(
    rules: Sequence[Mapping[str, Any]],
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[PlaceholderRule]:
    """挑出可能把开场白换成网页的规则并编译好；每张卡只做一次，再对每条开场白用

    要求：替换文字带 HTML、酒馆显示开场白时会套用它（停用原因不是上面那三种）、查找正则能用。
    启没启用、NA 里现在怎么处理都不看：管理员把替换文字改成普通文字之后，它就不再参与判断。
    给了 `deadline` 时预处理也算在同一个时限里：到点就不再往下准备（之后的判断也会按到点处理，
    开场白照常发）。
    """
    prepared: list[PlaceholderRule] = []
    for index, rule in enumerate(rules):
        if deadline is not None and clock() >= deadline:
            break
        if not isinstance(rule, Mapping) or rule.get("disabled_reason") in _NOT_APPLIED_ON_DISPLAY:
            continue
        replace = rule.get("replace")
        find = rule.get("find")
        if not isinstance(replace, str) or not isinstance(find, str) or not has_html_output(replace):
            continue
        parsed = parse_find(find)
        if parsed.error:
            continue
        try:
            pattern = regex.compile(parsed.pattern, parsed.flags | regex.V0)
        except Exception:  # noqa: BLE001 - 编译不过就不参与判断
            continue
        trim_raw = rule.get("trim")
        name = rule.get("name")
        prepared.append(
            PlaceholderRule(
                name=name if isinstance(name, str) and name.strip() else f"第 {index + 1} 条",
                pattern=pattern,
                global_=parsed.global_,
                groups=tuple(_referenced_groups(replace, pattern)),
                trim=tuple(item for item in trim_raw if isinstance(item, str)) if isinstance(trim_raw, list) else (),
            ),
        )
    return prepared


def placeholder_rule(
    text: str,
    rules: Sequence[PlaceholderRule],
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """一条开场白是不是只是给某条 HTML 规则用的标记；是的话返回那条规则的名字

    有的卡把开场白写成一个短标记（`[开场]` 这类），靠一条只改显示的正则在酒馆里把它整个换成网页界面。
    QQ 里显示不了网页，这条开场白在 NA 里发出去就只剩一个标记。判据：去掉首尾空白后不超过
    :data:`MAX_PLACEHOLDER_CHARS` 字；某条规则（:func:`placeholder_rules` 挑出来的）把它整个吃掉；并且按
    「只留正文」它什么都不剩（引用的分组里没有正文）。最后一条挡住「整段美化」：`/^([\\s\\S]*)$/` →
    `<div …>$1</div>` 也能吃掉任何开场白，但正文就在 `$1` 里，那不是标记。

    每条规则单次匹配限时；给了 `deadline`（`clock` 的读数）时，到点后剩下的规则不再试、按不是标记算。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_PLACEHOLDER_CHARS:
        return None
    for rule in rules:
        if deadline is not None and clock() >= deadline:
            return None
        outputs: list[str] = []

        def keep_text(match: regex.Match[str], rule: PlaceholderRule = rule) -> str:
            outputs.append(_build_text_only(rule.groups, rule.trim, match))
            return ""

        try:
            remainder = rule.pattern.sub(
                keep_text,
                stripped,
                count=0 if rule.global_ else 1,
                timeout=PLACEHOLDER_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 - 超时就当它不是标记
            continue
        if outputs and not remainder.strip() and not any(output.strip() for output in outputs):
            return rule.name
    return None


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
        groups: Sequence[str | int] = ()
        if rule.mode == OUTPUT_TEXT:
            groups = rule.groups if rule.groups is not None else _referenced_groups(rule.replace, rule.pattern)

        def replace_match(match: regex.Match[str]) -> str:
            nonlocal replaced
            replaced += 1
            if rule.mode == OUTPUT_DROP:
                return ""
            if rule.mode == OUTPUT_TEXT:
                return _build_text_only(groups, rule.trim, match)
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
