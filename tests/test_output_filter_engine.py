"""输出过滤引擎的纯函数用例

这里只测「给规则 + 文字，返回什么」：归一化、重叠合并、正则安全、窗口扫描与慢正则停用。
发送链路与 OneBot 钩子各自另有用例。
"""

from collections import Counter

import pytest

from nekro_agent.services.output_filter import engine
from nekro_agent.services.output_filter.engine import (
    TextFilterResult,
    disabled_regexes,
    filter_text,
)
from nekro_agent.services.output_filter.rules import compile_rules


@pytest.fixture(autouse=True)
def _fresh_engine_state():
    engine._reset_runtime_state_for_tests()
    yield
    engine._reset_runtime_state_for_tests()


def _words(text: str):
    return compile_rules(text, "")


def _regex(text: str):
    return compile_rules("", text)


class TestWordMatch:
    def test_fragment_replacement(self) -> None:
        result = filter_text("你好坏词呀", _words("坏词"), "**Filter**")

        assert result.text == "你好**Filter**呀"
        assert result.hits == Counter({"word:坏词": 1})

    def test_multiple_occurrences_are_all_replaced_and_counted(self) -> None:
        result = filter_text("bad and bad", _words("bad"), "X")

        assert result.text == "X and X"
        assert result.hits["word:bad"] == 2

    def test_overlapping_rules_merge_into_one_replacement(self) -> None:
        result = filter_text("badx", _words("bad\nadx"), "X")

        assert result.text == "X", "重叠的两段必须合并成一次替换"
        assert sum(result.hits.values()) == 2

    def test_adjacent_rules_are_not_merged(self) -> None:
        result = filter_text("abcd", _words("ab\ncd"), "X")

        assert result.text == "XX", "首尾相接是两处命中，不能合并"

    def test_no_hit_returns_the_same_string_object(self) -> None:
        text = "完全没有命中"

        result = filter_text(text, _words("别的词"), "X")

        assert result.text is text


class TestNormalization:
    @pytest.mark.parametrize("text", ["ＢＡＤ", "Bad", "bad"])
    def test_ascii_word_matches_fullwidth_and_case_variants(self, text: str) -> None:
        result = filter_text(text, _words("bad"), "X")

        assert result.text == "X"

    def test_fullwidth_word_matches_ascii_text(self) -> None:
        result = filter_text("bad", _words("ＢＡＤ"), "X")

        assert result.text == "X"

    @pytest.mark.parametrize("text", ["b a d", "b\u200bad", "b\u2060a\ufeffd", "b\u3000ad"])
    def test_inserted_whitespace_and_zero_width_chars_still_match(self, text: str) -> None:
        result = filter_text(text, _words("bad"), "X")

        assert result.text == "X"

    def test_replacement_range_covers_inserted_invisible_characters(self) -> None:
        result = filter_text("x b\u200bad y", _words("bad"), "**Filter**")

        assert result.text == "x **Filter** y", "中间的零宽字符要跟着命中片段一起换掉"


class TestRegex:
    def test_regex_matches(self) -> None:
        result = filter_text("baaad", _regex("ba+d"), "X")

        assert result.text == "X"
        assert result.hits["regex:ba+d"] == 1

    def test_regex_is_case_insensitive_even_when_written_uppercase(self) -> None:
        result = filter_text("baaad", _regex("BA+D"), "X")

        assert result.text == "X"

    def test_empty_matches_are_ignored(self) -> None:
        result = filter_text("abc", _regex("x*"), "X")

        assert result.text is not None
        assert result.text == "abc"
        assert not result.hit


class TestAtMarkupProtection:
    def test_markup_is_preserved_while_body_is_filtered(self) -> None:
        text = "[@id:123;nickname:bad@] 正文里的 bad"

        result = filter_text(text, _words("bad"), "X", protect_at_markup=True)

        assert result.text == "[@id:123;nickname:bad@] 正文里的 X"

    def test_markup_is_not_protected_by_default(self) -> None:
        text = "[@id:123;nickname:bad@]"

        result = filter_text(text, _words("bad"), "X")

        assert "bad" not in result.text


class TestRuleParsing:
    def test_comments_and_blank_lines_are_ignored(self) -> None:
        rules = compile_rules("# 注释\n\nbad\n  \n# 另一条注释\nworse", "")

        assert [source for _word, rule in rules.words for source in [rule.source]] == ["bad", "worse"]

    def test_json_array_import(self) -> None:
        rules = compile_rules('["bad", "worse", "bad"]', "")

        assert [rule.source for _word, rule in rules.words] == ["bad", "worse"]

    def test_too_long_word_is_rejected_with_reason(self) -> None:
        rules = compile_rules("啊" * 101, "")

        assert not rules.words
        assert rules.rejected == (("啊" * 101, "过长"),)

    def test_excess_words_are_rejected(self) -> None:
        rules = compile_rules("\n".join(f"w{index}" for index in range(5001)), "")

        assert len(rules.words) == 5000
        assert len(rules.rejected) == 1
        assert rules.rejected[0][1] == "超过条数上限"

    def test_regex_too_long_is_rejected(self) -> None:
        rules = compile_rules("", "a" * 201)

        assert not rules.regexes
        assert rules.rejected == (("a" * 201, "过长"),)


class TestRegexSafety:
    @pytest.mark.parametrize(
        ("pattern", "reason"),
        [
            ("(a+)+", "重复里不能再套重复"),
            ("(a|aa)*", "重复里不能用 |"),
            # `|` 藏在一个 `?` 里，同样会指数回溯
            ("(?:a(?:a|aa)?)*", "重复里不能用 |"),
            (r"(\w+)\1", "不支持反向引用"),
            (".*a.*b", "最多一个不定长重复"),
            ("a{0,100}b{0,100}c{0,100}", "过于复杂"),
        ],
    )
    def test_dangerous_patterns_are_rejected(self, pattern: str, reason: str) -> None:
        rules = compile_rules("", pattern)

        assert not rules.regexes
        assert rules.rejected == ((pattern, reason),)

    @pytest.mark.parametrize("pattern", ["a.{0,10}b", "fo+bar", "^abc$"])
    def test_reasonable_patterns_are_accepted(self, pattern: str) -> None:
        rules = compile_rules("", pattern)

        assert [compiled.pattern for compiled, _rule in rules.regexes] == [pattern]
        assert not rules.rejected

    def test_syntax_error_is_rejected(self) -> None:
        rules = compile_rules("", "(")

        assert not rules.regexes
        assert rules.rejected[0][1].startswith("语法错误：")


class TestSlowRegex:
    def test_regex_is_disabled_after_three_slow_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(engine, "SLOW_SECONDS", 0.0)
        rules = _regex("ba+d")

        for _ in range(3):
            filter_text("baaad", rules, "X")

        assert "ba+d" in disabled_regexes()
        assert disabled_regexes()["ba+d"] == "运行过慢，已停用；修改或重启后恢复"

        result = filter_text("baaad", rules, "X")
        assert result.text == "baaad", "停用之后不再参与匹配"


class TestLongTextWindows:
    def test_hits_at_start_overlap_and_end_are_replaced_once(self) -> None:
        text = "x" * 5000
        text = text[:0] + "baaad" + text[5:]
        text = text[:1900] + "baaad" + text[1905:]
        text = text[:4995] + "baaad"

        result = filter_text(text, _regex("ba+d"), "X")

        assert result.text.count("X") == 3
        assert result.hits["regex:ba+d"] == 3, "窗口重叠区不能重复计数"

    def test_hit_straddling_the_window_boundary_is_found(self) -> None:
        """命中正好跨过第一个窗口的末尾：没有重叠的话两个窗口各看到半截，都匹配不上"""
        text = "x" * 1998 + "baaad" + "x" * 3000

        result = filter_text(text, _regex("ba+d"), "X")

        assert result.hits["regex:ba+d"] == 1
        assert "baaad" not in result.text


def test_result_type_is_exported() -> None:
    """`TextFilterResult` 是发送链路的返回类型，不能被悄悄改名"""
    result = filter_text("a", _words("a"), "X")

    assert isinstance(result, TextFilterResult)
