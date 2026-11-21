"""宏展开：表里每个宏、由内向外、稳定挑选、越界原样与长度保护

宏是纯文字替换，不执行任何东西；这里只钉输出，不碰数据库与网络。`{{pick}}` 的
稳定性依赖调用方给的频道、人设与文字类别，用例不写死某个随机结果，而是比较同一
上下文重复调用与换频道后的集合。
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime
from types import SimpleNamespace

from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.services.tavern.macros import MacroContext, expand_macros, sanitize_user_name


def _ctx(**overrides: object) -> MacroContext:
    values: dict[str, object] = {
        "char_name": "Bot",
        "user_name": "User",
        "now": datetime(2026, 1, 5, 7, 8, 9),
        "lang": "zh-CN",
        "chat_key": "group_1",
        "preset_id": 7,
        "text_kind": "body",
        "original": "ORIGINAL",
        "rng": random.Random(1),
    }
    values.update(overrides)
    return MacroContext(**values)  # type: ignore[arg-type]


def _expand(text: str, ctx: MacroContext | None = None) -> str:
    return expand_macros(text, ctx or _ctx()).text


class TestSimpleMacros:
    def test_char_user_time_date_weekday(self) -> None:
        text = _expand("{{char}}|{{user}}|{{time}}|{{date}}|{{weekday}}")

        assert text == "Bot|User|07:08|2026-01-05|星期一"

    def test_names_are_case_insensitive(self) -> None:
        assert _expand("{{CHAR}} {{User}}") == "Bot User"

    def test_weekday_english(self) -> None:
        assert _expand("{{weekday}}", _ctx(lang="en-US")) == "Monday"

    def test_newline(self) -> None:
        assert _expand("a{{newline}}b") == "a\nb"

    def test_original(self) -> None:
        assert _expand("{{original}}") == "ORIGINAL"

    def test_legacy_placeholders(self) -> None:
        assert _expand("<USER>|<BOT>|<CHAR>") == "User|Bot|Bot"
        assert _expand("<user>|<char>") == "User|Bot"


class TestRandomAndPick:
    def test_random_from_arguments(self) -> None:
        for _ in range(20):
            assert _expand("{{random::a::b::c}}") in {"a", "b", "c"}

    def test_random_single_colon_comma_form(self) -> None:
        assert _expand("{{random:a,b}}") in {"a", "b"}

    def test_random_without_arguments_is_left_alone(self) -> None:
        assert _expand("{{random}}") == "{{random}}"

    def test_pick_is_stable_for_the_same_context(self) -> None:
        ctx = _ctx()
        results = {_expand("{{pick::a::b::c::d::e::f}}", ctx) for _ in range(5)}

        assert len(results) == 1
        assert results.pop() in {"a", "b", "c", "d", "e", "f"}

    def test_pick_changes_with_chat_key(self) -> None:
        values = {
            _expand("{{pick::a::b::c::d::e::f}}", _ctx(chat_key=f"group_{index}"))
            for index in range(16)
        }

        assert len(values) > 1

    def test_pick_without_arguments_is_left_alone(self) -> None:
        assert _expand("{{pick}}") == "{{pick}}"


class TestRoll:
    def test_ndm_form(self) -> None:
        result = _expand("{{roll::1d20}}")

        assert re.fullmatch(r"\d+", result)
        assert 1 <= int(result) <= 20

    def test_plain_faces_form(self) -> None:
        result = _expand("{{roll::20}}")

        assert 1 <= int(result) <= 20
        assert _expand("{{roll:20}}").isdigit()

    def test_out_of_range_is_left_alone(self) -> None:
        assert _expand("{{roll::101d6}}") == "{{roll::101d6}}"
        assert _expand("{{roll::1d0}}") == "{{roll::1d0}}"
        assert _expand("{{roll::1d1000001}}") == "{{roll::1d1000001}}"

    def test_bad_format_is_left_alone(self) -> None:
        assert _expand("{{roll::d20}}") == "{{roll::d20}}"
        assert _expand("{{roll}}") == "{{roll}}"


class TestCommentsAndUnknown:
    def test_comment_forms_are_removed(self) -> None:
        assert _expand("a{{// 注释}}b") == "ab"
        assert _expand("a{{comment: x}}b") == "ab"
        assert _expand("a{{comment::x}}b") == "ab"

    def test_unknown_macros_are_left_alone(self) -> None:
        assert _expand("{{setvar::a::1}}") == "{{setvar::a::1}}"
        assert _expand("{{if x}}") == "{{if x}}"
        assert _expand("{{unknown::a}}") == "{{unknown::a}}"

    def test_count_is_zero_when_nothing_is_expanded(self) -> None:
        result = expand_macros("{{setvar::a::1}}", _ctx())

        assert result.expanded == 0
        assert result.guarded is False


class TestNesting:
    def test_inner_macro_expands_first(self) -> None:
        result = expand_macros("{{random::{{char}}::b}}", _ctx())

        assert result.text in {"Bot", "b"}
        assert result.expanded == 2

    def test_multiple_passes_for_deep_nesting(self) -> None:
        result = expand_macros("{{random::{{random::{{char}}::x}}::y}}", _ctx())

        assert result.text in {"Bot", "x", "y"}
        assert result.expanded == 3


class TestLengthGuard:
    def test_exploding_expansion_stops_at_the_previous_pass(self) -> None:
        text = "{{original}}"
        ctx = _ctx(original="x" * (2 * len(text) + 4001))

        result = expand_macros(text, ctx)

        assert result.guarded is True
        assert result.text == text


class TestGroupTrimAndExamples:
    def test_group_is_the_character_name(self) -> None:
        assert _expand("{{group}}") == "Bot"
        assert _expand("{{GROUP}}", _ctx(char_name="薄荷")) == "薄荷"

    def test_trim_removes_adjacent_newlines(self) -> None:
        assert _expand("甲\n\n{{trim}}\n乙") == "甲乙"

    def test_trim_keeps_spaces(self) -> None:
        assert _expand("甲 {{trim}} 乙") == "甲  乙"

    def test_trim_is_case_insensitive(self) -> None:
        assert _expand("甲\n{{TRIM}}\n乙") == "甲乙"

    def test_trim_handles_crlf_and_keeps_lone_carriage_return(self) -> None:
        assert _expand("甲\r\n\r\n{{trim}}\r\n乙") == "甲乙"
        assert _expand("甲\r{{trim}}乙") == "甲\r乙"

    def test_adjacent_trims_swallow_the_newlines_between_them(self) -> None:
        assert _expand("甲\n{{trim}}\n\n{{ trim }}\n乙") == "甲乙"

    def test_trim_counts_as_expanded(self) -> None:
        result = expand_macros("甲\n{{trim}}\n乙\n{{trim}}", _ctx())

        assert result.text == "甲乙"
        assert result.expanded == 2

    def test_trim_is_linear_on_a_long_run_of_newlines(self) -> None:
        # 把两侧换行写进正则的写法在这里要十几秒；现在应当瞬间完成。
        text = "\n" * 40_000 + "甲" + "\n" * 40_000 + "{{trim}}" + "\n" * 40_000 + "乙"
        started = time.perf_counter()

        result = _expand(text)

        assert result == "\n" * 40_000 + "甲乙"
        assert time.perf_counter() - started < 2

    def test_mes_examples_raw_returns_the_examples(self) -> None:
        assert _expand("{{mesExamplesRaw}}", _ctx(examples_raw="{{char}}: 你好")) == "Bot: 你好"

    def test_mes_examples_raw_is_empty_without_examples(self) -> None:
        assert _expand("{{mesExamplesRaw}}") == ""


class TestExamplesRawLookup:
    def test_none_card_is_empty(self) -> None:
        assert tavern_runtime._examples_raw(None) == ""  # noqa: SLF001

    def test_source_fields_must_be_a_dict(self) -> None:
        card = SimpleNamespace(source_fields=["mes_example"])

        assert tavern_runtime._examples_raw(card) == ""  # noqa: SLF001

    def test_card_without_source_fields_is_empty(self) -> None:
        assert tavern_runtime._examples_raw(SimpleNamespace()) == ""  # noqa: SLF001

    def test_mes_example_must_be_a_string(self) -> None:
        card = SimpleNamespace(source_fields={"mes_example": 1})

        assert tavern_runtime._examples_raw(card) == ""  # noqa: SLF001

    def test_string_is_returned_verbatim(self) -> None:
        card = SimpleNamespace(source_fields={"mes_example": "{{char}}: 你好"})

        assert tavern_runtime._examples_raw(card) == "{{char}}: 你好"  # noqa: SLF001


class TestSanitizeUserName:
    def test_newline_becomes_space(self) -> None:
        assert sanitize_user_name("a\nb") == "a b"

    def test_tags_and_braces_are_removed(self) -> None:
        assert sanitize_user_name("<b>张三</b>") == "b张三/b"
        assert sanitize_user_name("a{{b}}c") == "abc"

    def test_zero_width_characters_are_removed(self) -> None:
        assert sanitize_user_name("a\u200bb\u200bc") == "abc"

    def test_over_long_name_is_cut_to_twenty_four_characters(self) -> None:
        assert len(sanitize_user_name("长" * 30)) == 24

    def test_empty_and_whitespace_fall_back_to_user(self) -> None:
        assert sanitize_user_name(None) == "用户"
        assert sanitize_user_name("   \n\t ") == "用户"
        assert sanitize_user_name("") == "用户"
