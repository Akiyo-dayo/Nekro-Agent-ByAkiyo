"""过滤引擎：在归一化文字上找命中，再把命中区间换回原文

一条文字可能同时命中词和正则，替换要按「原文区间」做，且重叠的命中合并成一次替换。
没有任何命中时必须原样返回**同一个字符串对象**：发送链路上有大量「内容没变」的判断，
凭空重建一个内容相同的串会让它们全部失效。
"""

import re
import time
from collections import Counter
from dataclasses import dataclass

from nekro_agent.core.logger import get_sub_logger

from .normalize import normalize_with_map
from .rules import CompiledRules

logger = get_sub_logger("output_filter")

REGEX_WINDOW = 2000
REGEX_OVERLAP = 200
SLOW_SECONDS = 0.1
SLOW_LIMIT = 3

DISABLED_REASON = "运行过慢，已停用；修改或重启后恢复"

# `[@id:123;nickname:xx@]` 形态的 @ 标记：整段不参与匹配，否则词表/正则会把它拆坏
_AT_MARKUP = re.compile(r"\[@[^\[\]\n]{0,128}?@\]")

# 运行期状态：某个正则在本进程里慢了几次、哪些已被停用
_slow_counts: dict[str, int] = {}
_disabled_regexes: dict[str, str] = {}


def disabled_regexes() -> dict[str, str]:
    """运行中因过慢被停用的正则：原文 -> 原因"""
    return dict(_disabled_regexes)


def _reset_runtime_state_for_tests() -> None:
    """清空运行期状态；只给用例用"""
    _slow_counts.clear()
    _disabled_regexes.clear()


def _note_slow(source: str, elapsed: float) -> None:
    count = _slow_counts.get(source, 0) + 1
    _slow_counts[source] = count
    logger.warning(f"输出过滤正则运行过慢: {source!r} 本次耗时 {elapsed:.3f}s（第 {count} 次）")
    if count >= SLOW_LIMIT and source not in _disabled_regexes:
        _disabled_regexes[source] = DISABLED_REASON
        logger.warning(f"输出过滤正则已停用: {source!r}（{DISABLED_REASON}）")


@dataclass
class TextFilterResult:
    text: str
    hits: Counter[str]

    @property
    def hit(self) -> bool:
        return bool(self.hits)


def _regex_spans(pattern: re.Pattern[str], source: str, text: str) -> list[tuple[int, int]]:
    """在归一化文字上跑一条正则，返回整段坐标下的命中区间

    长文本按窗口切开扫，窗口之间有重叠；重叠区里重复找到的区间按 (start, end) 去重，
    因此同一个命中只会计一次，且跨越窗口边界的命中不会漏。
    """
    if len(text) <= REGEX_WINDOW:
        windows = [(0, text)]
    else:
        windows = []
        start = 0
        while start < len(text):
            end = min(start + REGEX_WINDOW, len(text))
            windows.append((start, text[start:end]))
            if end >= len(text):
                break
            start += REGEX_WINDOW - REGEX_OVERLAP

    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for offset, window in windows:
        if source in _disabled_regexes:
            break
        started_at = time.perf_counter()
        matches = list(pattern.finditer(window))
        elapsed = time.perf_counter() - started_at
        if elapsed > SLOW_SECONDS:
            _note_slow(source, elapsed)
        for match in matches:
            if match.start() == match.end():
                continue
            span = (offset + match.start(), offset + match.end())
            if span in seen:
                continue
            seen.add(span)
            spans.append(span)
    return spans


def _collect_spans(text: str, rules: CompiledRules) -> tuple[str, list[int], list[tuple[int, int]], Counter[str]]:
    """返回 (归一化串, 下标映射, 归一化坐标下的命中区间, 命中计数)"""
    normalized, indexes = normalize_with_map(text)
    spans: list[tuple[int, int]] = []
    hits: Counter[str] = Counter()

    for word, rule in rules.words:
        start = normalized.find(word)
        while start >= 0:
            spans.append((start, start + len(word)))
            hits[rule.key] += 1
            # 不重叠地继续找：同一段文字只算一次
            start = normalized.find(word, start + len(word))

    for pattern, rule in rules.regexes:
        if rule.source in _disabled_regexes:
            continue
        matched = _regex_spans(pattern, rule.source, normalized)
        if matched:
            spans.extend(matched)
            hits[rule.key] += len(matched)

    return normalized, indexes, spans, hits


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """排序后合并真正重叠的区间；首尾相接（start == 上一个 end）不合并"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _replace_spans(text: str, spans: list[tuple[int, int]], replacement: str) -> str:
    """从右往左替换，避免前面的替换改变后面区间的下标"""
    result = text
    for start, end in reversed(spans):
        result = result[:start] + replacement + result[end:]
    return result


def _filter_plain(text: str, rules: CompiledRules, replacement: str) -> TextFilterResult:
    normalized, indexes, spans, hits = _collect_spans(text, rules)
    if not spans:
        return TextFilterResult(text=text, hits=hits)

    original_spans = [(indexes[start], indexes[end - 1] + 1) for start, end in spans]
    merged = _merge_spans(original_spans)
    return TextFilterResult(text=_replace_spans(text, merged, replacement), hits=hits)


def filter_text(
    text: str,
    rules: CompiledRules,
    replacement: str,
    *,
    protect_at_markup: bool = False,
) -> TextFilterResult:
    """对一段出站文字执行词表与正则替换

    Args:
        text: 原文
        rules: 已编译的规则集
        replacement: 命中片段的替换文字
        protect_at_markup: 是否把 `[@id:...;nickname:...@]` 形态的 @ 标记整段保护起来
    """
    if rules.empty:
        return TextFilterResult(text=text, hits=Counter())

    if not protect_at_markup:
        return _filter_plain(text, rules, replacement)

    parts: list[str] = []
    hits: Counter[str] = Counter()
    cursor = 0
    for markup in _AT_MARKUP.finditer(text):
        plain = text[cursor : markup.start()]
        if plain:
            result = _filter_plain(plain, rules, replacement)
            parts.append(result.text)
            hits.update(result.hits)
        parts.append(markup.group(0))
        cursor = markup.end()
    tail = text[cursor:]
    if tail:
        result = _filter_plain(tail, rules, replacement)
        parts.append(result.text)
        hits.update(result.hits)

    filtered = "".join(parts)
    if filtered == text:
        # 内容没变也要还回原对象：调用方用 `is` 判断"这条消息动没动过"
        return TextFilterResult(text=text, hits=hits)
    return TextFilterResult(text=filtered, hits=hits)
