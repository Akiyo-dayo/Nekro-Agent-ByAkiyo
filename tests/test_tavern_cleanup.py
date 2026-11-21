"""清理规则：导入时默认停用、查找正则解析、替换语义与限时

卡片自带的正则不能直接信：先按固定顺序判默认停用原因，再由管理员决定是否启用；
执行时用 `regex` 的超时在长文本上掐断，预算用完的规则跳过。这里的超时用例故意
绕过静态检查直接构造编译好的规则，验证的是执行期兜底而不是检查器。
"""

from __future__ import annotations

import random
import time
from typing import Any

import pytest
import regex

from nekro_agent.services.tavern.cleanup import (
    DEPTH_LIMITED,
    DISABLED_IN_CARD,
    FIND_MACROS,
    HTML_OUTPUT,
    INVALID_REGEX,
    NOT_AI_OUTPUT,
    OUTPUT_DROP,
    OUTPUT_REPLACE,
    OUTPUT_TEXT,
    PROMPT_ONLY,
    UNSAFE_REGEX,
    CompiledRule,
    apply_compiled,
    compile_rules,
    has_html_output,
    parse_find,
    rule_from_st_script,
    validate_rule,
)


def _script(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scriptName": "规则",
        "findRegex": r"/<status>[\s\S]*?<\/status>/g",
        "replaceString": "",
        "trimStrings": [],
        "placement": [2],
    }
    base.update(overrides)
    return base


def _rule(find: str, replace: str = "", trim: list[str] | None = None, *, enabled: bool = True) -> dict[str, Any]:
    return {
        "name": "规则",
        "find": find,
        "replace": replace,
        "trim": trim or [],
        "enabled": enabled,
        "disabled_reason": "",
        "original": None,
    }


def _apply(text: str, rules: list[dict[str, Any]], **kwargs: Any) -> Any:
    return apply_compiled(text, compile_rules(rules), expand=lambda value: value, **kwargs)


class TestDisabledReasons:
    def test_not_ai_output(self) -> None:
        assert rule_from_st_script(_script(placement=[1]))["disabled_reason"] == NOT_AI_OUTPUT
        assert rule_from_st_script(_script(placement="2"))["disabled_reason"] == NOT_AI_OUTPUT

    def test_disabled_in_card(self) -> None:
        assert rule_from_st_script(_script(disabled=True))["disabled_reason"] == DISABLED_IN_CARD

    def test_prompt_only(self) -> None:
        assert rule_from_st_script(_script(promptOnly=True))["disabled_reason"] == PROMPT_ONLY

    def test_find_macros(self) -> None:
        assert rule_from_st_script(_script(substituteRegex=1))["disabled_reason"] == FIND_MACROS
        assert rule_from_st_script(_script(substituteRegex="1"))["disabled_reason"] == FIND_MACROS

    def test_boolean_substitute_regex_is_not_a_number(self) -> None:
        assert rule_from_st_script(_script(substituteRegex=True))["enabled"] is True

    def test_depth_limited(self) -> None:
        assert rule_from_st_script(_script(minDepth=2))["disabled_reason"] == DEPTH_LIMITED

    def test_invalid_regex(self) -> None:
        assert rule_from_st_script(_script(findRegex=""))["disabled_reason"] == INVALID_REGEX

    def test_unsafe_regex(self) -> None:
        assert rule_from_st_script(_script(findRegex="/(a+)+/"))["disabled_reason"] == UNSAFE_REGEX

    def test_html_output_is_enabled_as_text_only(self) -> None:
        # 输出 HTML 的规则不再停用：启用并只留正文（QQ 显示不了网页）
        rule = rule_from_st_script(_script(replaceString="<b>状态</b>$1"))

        assert rule["enabled"] is True
        assert rule["disabled_reason"] == ""
        assert rule["output_mode"] == OUTPUT_TEXT

    def test_html_output_with_an_image_is_dropped(self) -> None:
        rule = rule_from_st_script(_script(replaceString='<IMG src="https://img.example.test/$1.png">'))

        assert rule["enabled"] is True
        assert rule["output_mode"] == OUTPUT_DROP

    def test_html_output_that_could_not_be_saved_stays_disabled(self) -> None:
        # 启用以后过不了保存校验（去除串超过 20 个）的，照旧停用，免得规则表存不回去
        rule = rule_from_st_script(_script(replaceString="<b>状态</b>", trimStrings=["x"] * 21))

        assert rule["enabled"] is False
        assert rule["disabled_reason"] == HTML_OUTPUT
        # 处理方式照样给默认值：管理员改好去除串再打开时，不会先按 4000 字的「按替换文字」上限报错
        assert rule["output_mode"] == OUTPUT_TEXT

    def test_plain_rule_keeps_replace_mode(self) -> None:
        assert rule_from_st_script(_script())["output_mode"] == OUTPUT_REPLACE

    def test_default_enabled_status_bar_rule(self) -> None:
        rule = rule_from_st_script(_script())

        assert rule["enabled"] is True
        assert rule["disabled_reason"] == ""
        assert rule["find"] == r"/<status>[\s\S]*?<\/status>/g"

    def test_first_matching_reason_wins(self) -> None:
        assert rule_from_st_script(_script(placement=[1], disabled=True))["disabled_reason"] == NOT_AI_OUTPUT
        assert rule_from_st_script(_script(disabled=True, promptOnly=True))["disabled_reason"] == DISABLED_IN_CARD

    def test_markdown_only_does_not_disable(self) -> None:
        assert rule_from_st_script(_script(markdownOnly=True))["enabled"] is True

    def test_markdown_and_prompt_only_together_is_a_normal_rule(self) -> None:
        # 酒馆里两个都开就两处都作用（engine.js:348-354）；NA 按普通规则作用于发出去的消息。
        rule = rule_from_st_script(_script(markdownOnly=True, promptOnly=True))

        assert rule["enabled"] is True
        assert rule["disabled_reason"] == ""

    def test_both_flags_still_go_through_the_later_checks(self) -> None:
        # 不因为两个都开就跳过其余判据：卡里关着、限深度、正则不安全的照样停用。
        both = {"markdownOnly": True, "promptOnly": True}
        assert rule_from_st_script(_script(disabled=True, **both))["disabled_reason"] == DISABLED_IN_CARD
        assert rule_from_st_script(_script(minDepth=2, **both))["disabled_reason"] == DEPTH_LIMITED
        assert rule_from_st_script(_script(findRegex="/(a+)+/", **both))["disabled_reason"] == UNSAFE_REGEX

    def test_original_is_a_deep_copy(self) -> None:
        script = _script(extra={"nested": [1]})
        rule = rule_from_st_script(script)
        script["extra"]["nested"].append(2)

        assert rule["original"] == _script(extra={"nested": [1]})


class TestParseFind:
    def test_global_flag(self) -> None:
        parsed = parse_find("/a/g")

        assert parsed.pattern == "a"
        assert parsed.global_ is True
        assert parsed.error == ""

    def test_without_global_flag(self) -> None:
        assert parse_find("/a/").global_ is False
        assert parse_find("a").global_ is False

    def test_flags_are_mapped(self) -> None:
        parsed = parse_find("/a/ism")

        assert parsed.flags & regex.IGNORECASE
        assert parsed.flags & regex.DOTALL
        assert parsed.flags & regex.MULTILINE

    def test_ignored_flags(self) -> None:
        assert parse_find("/a/yu").error == ""

    def test_js_named_group_is_rewritten(self) -> None:
        parsed = parse_find("/(?<name>a)/")

        assert parsed.pattern == "(?P<name>a)"
        assert parsed.error == ""

    def test_unknown_flag(self) -> None:
        parsed = parse_find("/a/x")

        assert parsed.error_kind == INVALID_REGEX
        assert "旗标" in parsed.error

    def test_empty_pattern(self) -> None:
        parsed = parse_find("")

        assert parsed.error_kind == INVALID_REGEX

    def test_syntax_error_is_invalid_not_unsafe(self) -> None:
        parsed = parse_find("(")

        assert parsed.error_kind == INVALID_REGEX
        assert "语法" in parsed.error

    @pytest.mark.parametrize(
        "find",
        [
            r"/(a)\1/",
            r"/(a+)+/",
            r"/(a|aa)*/",
            r"/.*a.*b/",
            "/" + "a" * 201 + "/",
        ],
        ids=["backreference", "nested-repeat", "branch-in-repeat", "two-unbounded", "too-long"],
    )
    def test_unsafe_patterns(self, find: str) -> None:
        assert parse_find(find).error_kind == UNSAFE_REGEX

    def test_unsafe_message_is_chinese_reason(self) -> None:
        assert parse_find("/(a+)+/").error == "重复里不能再套重复"


class TestApply:
    def test_global_replaces_all(self) -> None:
        assert _apply("aaa", [_rule("/a/g", "x")]).text == "xxx"

    def test_non_global_replaces_first_only(self) -> None:
        assert _apply("aaa", [_rule("/a/", "x")]).text == "xaa"

    def test_match_placeholder(self) -> None:
        assert _apply("abc", [_rule("/(a)(b)c/g", "[{{MATCH}}]")]).text == "[abc]"

    def test_numbered_group(self) -> None:
        assert _apply("ab", [_rule("/(a)(b)/g", "$2$1")]).text == "ba"

    def test_named_group(self) -> None:
        assert _apply("ab", [_rule("/(?<first>a)(?<second>b)/g", "$<second>$<first>")]).text == "ba"

    def test_missing_group_becomes_empty(self) -> None:
        assert _apply("a", [_rule("/(a)/g", "[$1][$2]")]).text == "[a][]"

    def test_trim_strings_apply_to_each_group_value(self) -> None:
        assert _apply("aaa-bbb", [_rule("/(a{1,3})-(b{1,3})/g", "[$1|$2]", ["a"])]).text == "[|bbb]"

    def test_expand_receives_the_final_replacement_text(self) -> None:
        seen: list[str] = []

        def expand(value: str) -> str:
            seen.append(value)
            return "[" + value + "]"

        result = apply_compiled("x", compile_rules([_rule("/x/g", "X")]), expand=expand)

        assert result.text == "[X]"
        assert seen == ["X"]

    def test_applied_counts_only_rules_that_replaced(self) -> None:
        result = _apply("ab", [_rule("/a/g", "x"), _rule("/z/g", "y")])

        assert result.text == "xb"
        assert result.applied == 1


class TestTimeouts:
    def test_slow_rule_times_out_and_text_is_unchanged(self) -> None:
        slow = CompiledRule(
            index=0,
            pattern=regex.compile(r"(.{50,}).*\1"),
            global_=True,
            replace="",
            trim=(),
        )
        text = "".join(random.Random(1).choice("abcdefghij") for _ in range(20000))

        started = time.monotonic()
        result = apply_compiled(text, [slow], expand=lambda value: value, per_call_seconds=0.05)
        elapsed = time.monotonic() - started

        assert result.timed_out == [0]
        assert result.skipped_by_budget == []
        assert result.text == text
        assert elapsed < 1.0

    def test_budget_exhaustion_skips_the_rest(self) -> None:
        slow_pattern = regex.compile(r"(.{50,}).*\1")
        text = "".join(random.Random(1).choice("abcdefghij") for _ in range(20000))
        rules = [
            CompiledRule(0, slow_pattern, True, "", ()),
            CompiledRule(1, slow_pattern, True, "", ()),
            CompiledRule(2, regex.compile("x"), True, "", ()),
        ]

        result = apply_compiled(
            text,
            rules,
            expand=lambda value: value,
            per_call_seconds=0.05,
            budget_seconds=0.01,
        )

        assert result.timed_out == [0]
        assert result.skipped_by_budget == [1, 2]
        assert result.text == text


class TestHtmlOutput:
    @pytest.mark.parametrize("replace", ["<b>x</b>", "<br>", "a</div>", "<!-- 注释 -->"])
    def test_html_is_detected(self, replace: str) -> None:
        assert has_html_output(replace) is True

    @pytest.mark.parametrize("replace", ["{{match}}", "$1", "$<name>", "a < b", "1 <2", "纯文本"])
    def test_non_html_is_not_detected(self, replace: str) -> None:
        assert has_html_output(replace) is False


class TestValidateRule:
    def test_valid_rule(self) -> None:
        assert validate_rule(_rule("/a/g", "b")) is None

    def test_empty_find_is_refused(self) -> None:
        assert validate_rule(_rule("", "b")) is not None

    def test_unsafe_find_is_refused(self) -> None:
        assert validate_rule(_rule("/(a+)+/", "b")) is not None

    def test_long_replace_is_refused(self) -> None:
        assert validate_rule(_rule("/a/", "x" * 4001)) is not None

    def test_too_many_trim_strings_are_refused(self) -> None:
        assert validate_rule(_rule("/a/", "b", ["x"] * 21)) is not None

    def test_non_list_trim_is_refused(self) -> None:
        rule = _rule("/a/", "b")
        rule["trim"] = "x"

        assert validate_rule(rule) is not None

    def test_html_replace_is_allowed_on_manual_save(self) -> None:
        assert validate_rule(_rule("/a/", "<b>x</b>")) is None


class TestCompile:
    def test_only_enabled_valid_rules_are_compiled_with_original_indexes(self) -> None:
        rules = [
            _rule("", ""),
            _rule("/a/g", "x"),
            _rule("/b/g", "y", enabled=False),
            _rule("/(a+)+/", "z"),
            _rule("/c/", "w"),
        ]

        compiled = compile_rules(rules)

        assert [rule.index for rule in compiled] == [1, 4]
