"""规则解析与正则安全检查

规则来自管理员填写的多行文本。解析结果按原文缓存：每次发送都重新解析一遍是白费工，
而规则文本本身很少变动，`lru_cache` 的键就是这两段原文。

正则能拖慢发送，因此编译之后还要过一遍语法树检查：反向引用、重复套重复、
重复里带 `|`、多个不定长重复、乘积过大的组合都会被拒绝。被拒的规则不会参与匹配，
拒绝原因会随「命中统计」接口返回给后台，管理员能看到自己写的哪一条没生效。

世界书的正则关键词复用同一套检查（`check_regex_safety` 与 `RegexRejected` 是公开的），
判据只有一份：这里漏掉一条，就多一条能把消息发送卡死的正则。
"""

import functools
import json
import re
from dataclasses import dataclass
from typing import Literal

try:  # Python 3.11 / 3.12 都有私有解析器；`re._parser` 不存在时退回旧的 `sre_parse`
    from re import _constants as sre_constants
    from re import _parser as sre_parse
except ImportError:  # pragma: no cover - 仅为极老解释器留的退路
    import sre_constants  # type: ignore[no-redef]
    import sre_parse  # type: ignore[no-redef]

MAX_WORDS = 5000
MAX_REGEXES = 200
MAX_WORD_LENGTH = 100
MAX_REGEX_LENGTH = 200
COMPLEXITY_BUDGET = 20000
# 不定长重复的复杂度权重；一个 `.*` 就足以让回溯爆炸，给它一个远高于普通重复的权重
UNBOUNDED_FACTOR = 2000
# 上界超过它的重复按「不定长」对待
UNBOUNDED_THRESHOLD = 100

_REPEAT_OPS = frozenset(
    op
    for op in (
        getattr(sre_constants, "MAX_REPEAT", None),
        getattr(sre_constants, "MIN_REPEAT", None),
        getattr(sre_constants, "POSSESSIVE_REPEAT", None),
    )
    if op is not None
)
_GROUPREF_OPS = frozenset(
    op
    for op in (
        getattr(sre_constants, "GROUPREF", None),
        getattr(sre_constants, "GROUPREF_EXISTS", None),
    )
    if op is not None
)
_ATOMIC_GROUP = getattr(sre_constants, "ATOMIC_GROUP", None)
_MAXREPEAT = sre_constants.MAXREPEAT


@dataclass(frozen=True)
class Rule:
    kind: Literal["word", "regex"]
    source: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.source}"


@dataclass(frozen=True)
class CompiledRules:
    words: tuple[tuple[str, Rule], ...]
    regexes: tuple[tuple[re.Pattern[str], Rule], ...]
    rejected: tuple[tuple[str, str], ...]

    @property
    def empty(self) -> bool:
        return not self.words and not self.regexes


class RegexRejected(ValueError):
    """某条正则不满足安全条件；消息即拒绝原因

    继承 `ValueError` 而不是 `Exception`：调用方（世界书的关键词校验与导入）把它当成
    「输入不合法」处理，与其它校验失败走同一条 400 分支，不需要为它单独写一个
    `except`。消息是中文的，可以直接回给管理员。
    """


# 旧名保留：模块内部的调用点与既有用例都还写着它，改名只影响想复用安全检查的模块。
_RuleRejected = RegexRejected


def _split_lines(text: str) -> list[str]:
    """逐行去掉空行与 `#` 注释行，保持原顺序"""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def _dedupe(items: list[str]) -> list[str]:
    """按原文去重并保持首次出现的顺序"""
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _parse_word_sources(words_text: str) -> list[str]:
    """词表既支持一行一个，也支持整段粘贴一个 JSON 字符串数组"""
    stripped = words_text.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return _dedupe([item.strip() for item in parsed if item.strip()])

    return _dedupe(_split_lines(words_text))


def _contains_branch(subpattern: object) -> bool:
    """子树里是否出现 `BRANCH`（`|`）

    重复节点也要穿过去：`(?:a(?:a|aa)?)*` 里的 `|` 藏在一个 `?`（上界为 1 的重复）里，
    只看容器节点就漏掉了，而它和 `(a|aa)*` 一样会指数回溯。
    """
    data = getattr(subpattern, "data", None)
    if not data:
        return False
    for op, av in data:
        if op == sre_constants.BRANCH:
            return True
        if op in _REPEAT_OPS:
            child = av[2]  # type: ignore[index]
        else:
            child = _container_child(op, av)
        if child is not None and _contains_branch(child):
            return True
    return False


def _container_child(op: object, av: object) -> object | None:
    """取出需要继续遍历的子树；不是容器节点时返回 None"""
    if op == sre_constants.SUBPATTERN:
        return av[3]  # type: ignore[index]
    if op in (sre_constants.ASSERT, sre_constants.ASSERT_NOT):
        return av[1]  # type: ignore[index]
    if op == sre_constants.BRANCH:
        return None  # 分支由调用方逐个分支遍历
    if _ATOMIC_GROUP is not None and op == _ATOMIC_GROUP:
        return av
    return None


def _walk_safety(subpattern: object, *, inside_repeat: bool, repeats: list[int]) -> None:
    data = getattr(subpattern, "data", None)
    if not data:
        return
    for op, av in data:
        if op in _GROUPREF_OPS:
            raise RegexRejected("不支持反向引用")

        if op in _REPEAT_OPS:
            _min, maximum, child = av  # type: ignore[misc]
            if maximum > 1:
                if inside_repeat:
                    raise RegexRejected("重复里不能再套重复")
                if _contains_branch(child):
                    raise RegexRejected("重复里不能用 |")
                repeats.append(maximum)
                _walk_safety(child, inside_repeat=True, repeats=repeats)
            else:
                # `{0,1}` 在语法树里也是重复节点，但它不具备「重复」的放大能力
                _walk_safety(child, inside_repeat=inside_repeat, repeats=repeats)
            continue

        if op == sre_constants.BRANCH:
            for branch in av[1]:  # type: ignore[index]
                _walk_safety(branch, inside_repeat=inside_repeat, repeats=repeats)
            continue

        child = _container_child(op, av)
        if child is not None:
            _walk_safety(child, inside_repeat=inside_repeat, repeats=repeats)


def check_regex_safety(pattern: str) -> None:
    """按语法树检查正则的复杂度，不满足时抛 `RegexRejected`

    公开成不带下划线的名字，是因为世界书的关键词也要按同一套判据校验。旧名
    `_check_regex_safety` 保留为别名，输出过滤的调用点与既有用例不需要跟着改名。
    """
    repeats: list[int] = []
    try:
        parsed = sre_parse.parse(pattern)
    except Exception as exc:  # noqa: BLE001 - 解析失败本身也是一种拒绝理由
        raise RegexRejected(f"语法错误：{exc}") from exc

    _walk_safety(parsed, inside_repeat=False, repeats=repeats)

    unbounded = 0
    factor = 1
    for maximum in repeats:
        if maximum >= _MAXREPEAT or maximum > UNBOUNDED_THRESHOLD:
            unbounded += 1
            factor *= UNBOUNDED_FACTOR
        else:
            factor *= maximum + 1
    if unbounded > 1:
        raise RegexRejected("最多一个不定长重复")
    if factor > COMPLEXITY_BUDGET:
        raise RegexRejected("过于复杂")


_check_regex_safety = check_regex_safety


def _compile_words(words_text: str) -> tuple[tuple[tuple[str, Rule], ...], list[tuple[str, str]]]:
    from .normalize import normalize_rule_text

    words: list[tuple[str, Rule]] = []
    rejected: list[tuple[str, str]] = []
    for source in _parse_word_sources(words_text):
        normalized = normalize_rule_text(source)
        if not normalized:
            continue
        if len(normalized) > MAX_WORD_LENGTH:
            rejected.append((source, "过长"))
            continue
        if len(words) >= MAX_WORDS:
            rejected.append((source, "超过条数上限"))
            continue
        words.append((normalized, Rule(kind="word", source=source)))
    return tuple(words), rejected


def _compile_regexes(regex_text: str) -> tuple[tuple[tuple[re.Pattern[str], Rule], ...], list[tuple[str, str]]]:
    regexes: list[tuple[re.Pattern[str], Rule]] = []
    rejected: list[tuple[str, str]] = []
    for source in _dedupe(_split_lines(regex_text)):
        if len(source) > MAX_REGEX_LENGTH:
            rejected.append((source, "过长"))
            continue
        if len(regexes) >= MAX_REGEXES:
            rejected.append((source, "超过条数上限"))
            continue
        try:
            pattern = re.compile(source, re.IGNORECASE)
        except re.error as exc:
            rejected.append((source, f"语法错误：{exc}"))
            continue
        try:
            _check_regex_safety(source)
        except _RuleRejected as exc:
            rejected.append((source, str(exc)))
            continue
        regexes.append((pattern, Rule(kind="regex", source=source)))
    return tuple(regexes), rejected


@functools.lru_cache(maxsize=128)
def compile_rules(words_text: str, regex_text: str) -> CompiledRules:
    """把两段管理员填写的规则文本编译成可直接匹配的规则集"""
    words, word_rejected = _compile_words(words_text)
    regexes, regex_rejected = _compile_regexes(regex_text)
    return CompiledRules(
        words=words,
        regexes=regexes,
        rejected=tuple(word_rejected + regex_rejected),
    )
