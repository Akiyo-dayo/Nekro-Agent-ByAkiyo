"""世界书的正则关键词：解析、校验、带时限与熔断的执行

关键词里的正则来自管理员或导入的酒馆文件，两处都不可信。一条 `(a+)+$` 就能让一次
提示词渲染卡到消息发不出去，而提示词渲染在对话主链路上——比输出过滤更靠前。因此这里
做三件事，缺一不可：

1. **解析**：只认两种形态（`/pattern/flags` 与「整条就是正则」），别的都当普通关键词；
2. **保存时校验**：语法、长度、以及复用输出过滤的复杂度检查，不合格的不许存进正则
   位置（手动保存 400，导入记进 `na_invalid_keys` 并跳过）；
3. **执行时兜底**：分窗扫、单次计时、慢够次数就熔断、每轮还有总耗时上限。校验只能
   挡住已知的坏形状，真正的灾难性回溯取决于输入文本，只能靠执行时限兜。

熔断状态是**进程级**且与输出过滤的停用表完全独立：两边的键完全不是一回事（这边是
「条目 ID + 关键词」，那边是正则原文），混在一起会让一条坏世界书正则把一条无辜的
输出过滤规则一并停掉，而管理员在输出过滤页上根本看不到原因。
"""

from __future__ import annotations

import functools
import re
import time
from dataclasses import dataclass
from typing import Callable

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.output_filter.engine import (
    REGEX_OVERLAP,
    REGEX_WINDOW,
    SLOW_LIMIT,
    SLOW_SECONDS,
)
from nekro_agent.services.output_filter.rules import MAX_REGEX_LENGTH, RegexRejected, check_regex_safety

logger = get_sub_logger("worldbook.regex")

# `/pattern/flags` 形态：`(.+)` 贪婪到最后一个 `/`，这样模式里自己带 `/` 也能用。
_SLASH_FORM = re.compile(r"^/(.+)/([A-Za-z]*)$")

_FLAG_BITS = {
    "i": re.IGNORECASE,
    "s": re.DOTALL,
    "m": re.MULTILINE,
}
# 酒馆里合法但对 Python 没有对应语义的旗标：忽略而不是判无效。
_IGNORED_FLAGS = frozenset({"g", "y", "u"})

# JS 命名组 `(?<name>…)` 改写成 Python 的 `(?P<name>…)`。必须避开后顾断言 `(?<=` 与
# `(?<!`：那两个也是 `(?<` 开头，一起改写会把合法的断言变成语法错误。
_JS_NAMED_GROUP = re.compile(r"\(\?<([A-Za-z_][A-Za-z0-9_]*)>")


@dataclass(frozen=True)
class RegexSpec:
    """一条解析好的正则关键词

    Attributes:
        pattern: 已把 JS 命名组改写成 Python 形态的模式。
        flags: 由旗标得来的 `re` 位；`use_regex` 形态没有旗标，这里是 0，大小写由
            条目的 `case_sensitive` 在 :meth:`RegexRunner.search` 里补。
        error: 非空表示这条关键词无效（原因），执行时一律不命中。
    """

    pattern: str
    flags: int = 0
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.error


def _rewrite_named_groups(pattern: str) -> str:
    """把 JS 的 `(?<name>` 改成 Python 的 `(?P<name>`，不动后顾断言"""
    return _JS_NAMED_GROUP.sub(r"(?P<\1>", pattern)


def parse_regex_key(key: str, *, use_regex: bool) -> RegexSpec | None:
    """判断一个关键词是不是正则，是就解析成 :class:`RegexSpec`

    返回 `None` 表示「这不是正则关键词」，按普通关键词处理。返回带 `error` 的
    :class:`RegexSpec` 表示「看起来是正则，但无效」——这两种必须分开：前者要退化成
    子串匹配（酒馆里 `/` 开头的普通词也会命中），后者必须不命中，否则一条坏正则会
    每轮都抛异常。
    """
    if use_regex:
        # `use_regex` 为真时没有旗标语法：整条关键词就是模式，`i` 之类由条目的大小写
        # 决定。这与酒馆一致（它的 regex 条目就是把关键词直接当正则用）。
        if not key:
            # 空模式会匹配一切，等于把一条没写完的条目变成常驻条目。
            return RegexSpec(pattern=key, error="正则为空")
        return RegexSpec(pattern=_rewrite_named_groups(key))

    matched = _SLASH_FORM.match(key)
    if matched is None:
        return None

    pattern, raw_flags = matched.group(1), matched.group(2)
    flags = 0
    for flag in raw_flags:
        if flag in _FLAG_BITS:
            flags |= _FLAG_BITS[flag]
        elif flag in _IGNORED_FLAGS:
            continue
        else:
            return RegexSpec(pattern=pattern, error=f"不支持的旗标 {flag!r}")

    if not pattern:
        return RegexSpec(pattern=pattern, error="正则为空")

    return RegexSpec(pattern=_rewrite_named_groups(pattern), flags=flags)


def validate_regex_key(key: str, *, use_regex: bool) -> str | None:
    """校验一条正则关键词；不是正则或合法时返回 `None`，否则返回中文原因

    长度上限与输出过滤一致（:data:`MAX_REGEX_LENGTH`）：两边都是「每轮要在整段文本上
    跑一遍」的正则，长度上限没有理由不同。复杂度检查直接调输出过滤的
    :func:`check_regex_safety`，判据只有一份。
    """
    spec = parse_regex_key(key, use_regex=use_regex)
    if spec is None:
        return None
    if not spec.valid:
        return spec.error
    if len(spec.pattern) > MAX_REGEX_LENGTH:
        return f"正则过长（最多 {MAX_REGEX_LENGTH} 字）"
    try:
        check_regex_safety(spec.pattern)
    except RegexRejected as exc:
        return str(exc)
    try:
        re.compile(spec.pattern, spec.flags)
    except re.error as exc:
        return f"语法错误：{exc}"
    return None


# 编译缓存：同一轮里同一条正则会被多个条目、多次渲染反复用到，编译一次就够。键带上
# flags，因为同一个模式在 `case_sensitive` 不同时是两个不同的正则对象。
@functools.lru_cache(maxsize=1024)
def _compile(pattern: str, flags: int) -> re.Pattern[str] | None:
    # 执行前再过一遍长度与复杂度检查：保存与导入时虽然查过，但手工改库、检查规则后来
    # 收紧、以及 `/…/` 形态的普通关键词都可能绕过那一道。Python 的正则一旦开跑就停不
    # 下来，时限只能在两次调用之间生效，挡住坏形状只能靠这里。结果随编译一起缓存。
    if len(pattern) > MAX_REGEX_LENGTH:
        return None
    try:
        check_regex_safety(pattern)
    except RegexRejected:
        return None
    try:
        return re.compile(pattern, flags)
    except re.error:
        # 编译不过的正则（导入时校验漏掉的、旗标组合不兼容的）不该在每轮都抛异常，
        # 缓存一个 `None` 让后续调用直接按「不命中」处理。
        return None


# 进程级熔断状态。`_slow_counts` 记「慢了几次」，`_disabled` 记「已停用」及原因。
_slow_counts: dict[tuple[int, str], int] = {}
_disabled: dict[tuple[int, str], str] = {}

DISABLED_REASON = "运行过慢，已停用；修改该条目或重启后恢复"


def disabled_regex_keys() -> dict[tuple[int, str], str]:
    """当前被熔断的（条目 ID, 关键词）与原因，拷贝一份给管理页"""
    return dict(_disabled)


def forget_entry(entry_id: int) -> None:
    """条目被编辑后清掉它的慢计数与停用记录

    编辑往往就是在修那条慢正则，留着停用状态会让管理员改完还看不到效果，以为改动
    没生效。条目删除时也调它，防止键越积越多。
    """
    for key in [key for key in _slow_counts if key[0] == entry_id]:
        _slow_counts.pop(key, None)
    for key in [key for key in _disabled if key[0] == entry_id]:
        _disabled.pop(key, None)


def _reset_runtime_state_for_tests() -> None:
    """清空进程级状态；只给用例用"""
    _slow_counts.clear()
    _disabled.clear()
    _compile.cache_clear()


def _windows(text: str) -> list[str]:
    """按输出过滤同样的窗口与重叠切开长文本

    重叠是必须的：一个命中跨在窗口边界上时，没有重叠就两边都看不见。
    """
    if len(text) <= REGEX_WINDOW:
        return [text]
    windows: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + REGEX_WINDOW, len(text))
        windows.append(text[start:end])
        if end >= len(text):
            break
        start += REGEX_WINDOW - REGEX_OVERLAP
    return windows


class RegexRunner:
    """一轮渲染用的正则执行器

    每轮新建一个实例，因为「本轮已花多少时间」「本轮是否已经警告过」都是轮次范围的状态，
    跨轮复用会让一个频道的慢正则拖累另一个频道。

    Attributes:
        budget_seconds: 本轮正则总耗时上限。超了以后剩余的正则关键词一律按未命中处理：
            宁可这一轮少注入一条世界书，也不能让提示词渲染卡住整条消息。
    """

    def __init__(self, *, budget_seconds: float = 0.05, clock: Callable[[], float] = time.perf_counter) -> None:
        self.budget_seconds = budget_seconds
        self._clock = clock
        self._spent = 0.0
        self._budget_warned = False

    @property
    def spent_seconds(self) -> float:
        """本轮已消耗的正则耗时，给明细与用例看"""
        return self._spent

    def search(
        self,
        entry_id: int,
        key: str,
        spec: RegexSpec,
        text: str,
        *,
        case_sensitive: bool = True,
    ) -> bool:
        """在原始扫描文本上跑一条正则，返回是否命中

        Args:
            case_sensitive: 只有 `use_regex` 形态用得上（`/…/` 形态自带旗标，与酒馆一致
                只看自己的旗标）。为假时补 `IGNORECASE`。
        """
        if not spec.valid:
            return False
        cache_key = (entry_id, key)
        if cache_key in _disabled:
            return False

        flags = spec.flags
        if not case_sensitive and not flags & re.IGNORECASE:
            flags |= re.IGNORECASE

        compiled = _compile(spec.pattern, flags)
        if compiled is None:
            return False

        for window in _windows(text):
            if self._spent >= self.budget_seconds:
                self._warn_budget_once()
                return False
            started_at = self._clock()
            matched = compiled.search(window) is not None
            elapsed = self._clock() - started_at
            self._spent += elapsed
            if elapsed > SLOW_SECONDS:
                self._note_slow(cache_key, key, elapsed)
                if cache_key in _disabled:
                    # 刚到阈值时这一次的结果仍然作数（它已经算出来了），但不再继续扫后面的
                    # 窗口：停用是为了止损，接着跑完剩下的窗口正好违反这个目的。
                    return matched
            if matched:
                return True
        return False

    def _warn_budget_once(self) -> None:
        """本轮第一次因总耗时超限而放弃时记一条 WARNING

        每轮最多一条：一个频道里可能挂着几十条正则，逐条记会把日志刷满，反而让真正
        有用的告警沉下去。
        """
        if self._budget_warned:
            return
        self._budget_warned = True
        logger.warning(
            f"世界书正则本轮总耗时超过 {self.budget_seconds:.3f}s，剩余关键词按未命中处理",
        )

    def _note_slow(self, cache_key: tuple[int, str], key: str, elapsed: float) -> None:
        """记一次慢；满 `SLOW_LIMIT` 次就熔断这条（条目, 关键词）"""
        count = _slow_counts.get(cache_key, 0) + 1
        _slow_counts[cache_key] = count
        logger.warning(
            f"世界书正则运行过慢: 条目 {cache_key[0]} 关键词 {key[:50]!r} 本次耗时 {elapsed:.3f}s（第 {count} 次）",
        )
        if count >= SLOW_LIMIT and cache_key not in _disabled:
            _disabled[cache_key] = DISABLED_REASON
            logger.warning(
                f"世界书正则已停用: 条目 {cache_key[0]} 关键词 {key[:50]!r}（{DISABLED_REASON}）",
            )
