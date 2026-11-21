"""世界书模板的安全子集：只解析、不执行

样本都是照着酒馆「提示词模板」扩展常见写法现写的小片段，不取自任何真实卡片。要钉住的事：

1. 认的写法按 JS 语义算对（`undefined` / `null`、宽松相等、短路、比较、真假）；
2. 标签与空白处理与 ejs 3 一致（`<%_ _%>`、`-%>`、`<%%`、注释）；
3. 子集之外的写法一律判不支持，包括探测运行环境的标识符与超深嵌套（不能把递归栈撑爆）；
4. JS 里会抛 `TypeError` 的情况抛 `TemplateRuntimeError`。
"""

from __future__ import annotations

import math
import time

import pytest

from nekro_agent.services.worldbook import template_lite
from nekro_agent.services.worldbook.template_lite import (
    EMPTY_VARIABLES,
    MAX_EXPR_DEPTH,
    MAX_NESTING,
    MAX_REASON_CHARS,
    MAX_TEMPLATE_CHARS,
    MAX_VALUE_DEPTH,
    MappingVariables,
    TemplateRuntimeError,
    TemplateUnsupported,
    compile_template,
    has_template,
    render,
)


def run(source: str, cache: dict | None = None, local: dict | None = None) -> str:
    variables = MappingVariables(cache=cache, local=local) if (cache or local) else EMPTY_VARIABLES
    return render(compile_template(source), variables)


class TestTags:
    def test_plain_text_is_not_a_template(self) -> None:
        assert not has_template("普通设定，没有模板")
        assert run("普通设定，没有模板") == "普通设定，没有模板"

    def test_if_else_picks_the_branch_by_variable(self) -> None:
        source = "开头<% if (getvar('stat.阶段') >= 2) { %>后期<% } else { %>前期<% } %>结尾"
        assert run(source) == "开头前期结尾"
        assert run(source, cache={"stat": {"阶段": 3}}) == "开头后期结尾"

    def test_else_if_chain(self) -> None:
        source = (
            "<% if (getvar('x') === 1) { %>一<% } else if (getvar('x') === 2) { %>二"
            "<% } else { %>其它<% } %>"
        )
        assert run(source, cache={"x": 1}) == "一"
        assert run(source, cache={"x": 2}) == "二"
        assert run(source) == "其它"

    def test_output_tags_print_plain_text_and_nothing_for_missing(self) -> None:
        source = "名字：<%= getvar('角色.名字') %>；身份：<%- getLocalVar('身份') %>。"
        assert run(source) == "名字：；身份：。"
        assert run(source, cache={"角色": {"名字": "<b>阿青</b>"}}, local={"身份": "旅人"}) == "名字：<b>阿青</b>；身份：旅人。"

    def test_output_numbers_and_booleans_like_js(self) -> None:
        source = "<%= getvar('a') %>|<%= getvar('b') %>|<%= getvar('c') %>|<%= getvar('d') %>"
        assert run(source, cache={"a": 3.0, "b": 2.5, "c": True, "d": None}) == "3|2.5|true|"

    def test_comments_and_literal_open_tag(self) -> None:
        assert run("前<%# 这是注释，不输出 %>后") == "前后"
        assert run("字面 <%% 照抄 %>") == "字面 <% 照抄 %>"

    def test_whitespace_slurping_and_newline_trimming(self) -> None:
        source = "第一行\n    <%_ if (true) { _%>\n中间\n    <%_ } _%>\n最后"
        assert run(source) == "第一行\n中间\n最后"
        assert run("甲<% if (true) { -%>\n乙<% } -%>\n丙") == "甲乙丙"

    def test_comment_lines_inside_code(self) -> None:
        source = "<%_ // 说明：NSFW 时才出现\nif (getvar('x') === true) { _%>\n内容\n<%_ } _%>\n"
        assert run(source) == ""
        assert run(source, cache={"x": True}) == "内容\n"
        assert run("<% /* 块注释 */ if (true) { %>有<% } %>") == "有"

    def test_unicode_property_names(self) -> None:
        source = "<% if (getvar('主角档案').模式 === '自定义') { %>自设<% } %>"
        assert run(source, cache={"主角档案": {"模式": "自定义"}}) == "自设"


class TestSemantics:
    def test_string_and_lowercase(self) -> None:
        source = "<% if (String(getvar('f')).toLowerCase() === 'true') { %>是<% } else { %>否<% } %>"
        assert run(source) == "否"  # String(undefined) === "undefined"
        assert run(source, cache={"f": True}) == "是"
        assert run(source, cache={"f": "TRUE"}) == "是"

    def test_includes_on_strings_and_arrays(self) -> None:
        source = "<% if (getvar('地点').includes('冰原')) { %>冷<% } else { %>暖<% } %>"
        assert run(source, cache={"地点": "罗伊冰原"}) == "冷"
        assert run(source, cache={"地点": "拉海洛"}) == "暖"
        assert run(source, cache={"地点": ["冰原", "城市"]}) == "冷"

    def test_short_circuit_protects_property_access(self) -> None:
        source = "<% if (getvar('地点') && getvar('地点').includes('冰原')) { %>冷<% } %>"
        assert run(source) == ""

    def test_or_returns_the_operand(self) -> None:
        assert run("<%= getvar('a') || '默认' %>") == "默认"
        assert run("<%= getvar('a') || '默认' %>", cache={"a": "值"}) == "值"
        assert run("<%= getvar('a') && '有' %>") == ""

    def test_null_and_undefined(self) -> None:
        source = "<% if (getvar('x') === null || getvar('x') === undefined) { %>未初始化<% } %>"
        assert run(source) == "未初始化"
        assert run(source, cache={"x": None}) == "未初始化"
        assert run(source, cache={"x": 0}) == ""
        assert run("<% if (getvar('x') == null) { %>空<% } %>") == "空"

    def test_loose_equality(self) -> None:
        assert run("<% if ('1' == 1 && 0 == false && '' == 0) { %>真<% } %>") == "真"
        assert run("<% if ('1' === 1) { %>真<% } else { %>假<% } %>") == "假"
        assert run("<% if (null == 0) { %>真<% } else { %>假<% } %>") == "假"

    def test_comparisons_with_undefined_are_false(self) -> None:
        source = "<% if (getvar('权重') >= 3) { %>大<% } %><% if (getvar('权重') < 3) { %>小<% } %>"
        assert run(source) == ""
        assert run(source, cache={"权重": 5}) == "大"
        assert run(source, cache={"权重": "2"}) == "小"

    def test_string_comparison_is_lexicographic(self) -> None:
        assert run("<% if ('b' > 'a') { %>真<% } %>") == "真"
        assert run("<% if ('10' < '9') { %>真<% } %>") == "真"

    def test_truthiness(self) -> None:
        source = "<% if (getvar('v')) { %>真<% } else { %>假<% } %>"
        for falsy in (0, "", None, False):
            assert run(source, cache={"v": falsy}) == "假"
        for truthy_value in (1, "0", [], {}, True):
            assert run(source, cache={"v": truthy_value}) == "真"
        assert run("<% if (!!getvar('v')) { %>真<% } else { %>假<% } %>") == "假"

    def test_property_and_length(self) -> None:
        assert run("<%= getvar('a').b.c %>", cache={"a": {"b": {"c": "深"}}}) == "深"
        assert run("<%= getvar('a').missing %>", cache={"a": {}}) == ""
        assert run("<%= getvar('s').length %>", cache={"s": "四个字符"}) == "4"


class TestRuntimeErrors:
    def test_property_of_undefined(self) -> None:
        with pytest.raises(TemplateRuntimeError):
            run("<%= getvar('a').b %>")

    def test_method_on_undefined(self) -> None:
        with pytest.raises(TemplateRuntimeError):
            run("<% if (getvar('地点').includes('冰原')) { %>冷<% } %>")

    def test_lowercase_on_non_string(self) -> None:
        with pytest.raises(TemplateRuntimeError):
            run("<%= getvar('n').toLowerCase() %>", cache={"n": 3})


class TestUnsupported:
    @pytest.mark.parametrize(
        "source",
        [
            "<% var x = 1; %>",
            "<% const x = getvar('a'); %>",
            "<% let x = 1 %>",
            "<% x = 1 %>",
            "<% for (const k of names) { %>x<% } %>",
            "<% while (true) { %>x<% } %>",
            "<% try { %>x<% } catch (e) { %>y<% } %>",
            "<% function f() { return 1 } %>",
            "<%= (() => 1)() %>",
            "<%= `模板字符串` %>",
            "<% if (localStorage.getItem('k')) { %>诱饵<% } %>",
            "<% if (window.__X) { %>诱饵<% } %>",
            "<%= getwi('书', '条目') %>",
            "<%= execute('/echo') %>",
            "<%= getvar(name) %>",
            "<%= getvar('a', {defaults: 1}) %>",
            "<%= getvar('a').startsWith('x') %>",
            "<%= getvar('a')['b'] %>",
            "<%= 1 + 2 %>",
            "<%= a ? b : c %>",
            "<% if (true) %>无花括号",
            "<% if (true) { %>没闭合",
            "多出来的<% } %>",
            "<% if (true) { 没闭合的标签",
            "<% if (true) { <% } %>",
            "<%= %>",
            "<%= 'a' 'b' %>",
            "<%= 'a\n' %>",
        ],
    )
    def test_rejected(self, source: str) -> None:
        with pytest.raises(TemplateUnsupported):
            compile_template(source)

    def test_deep_nesting_is_rejected_without_recursion_error(self) -> None:
        deep = "<%= " + "(" * 5000 + "1" + ")" * 5000 + " %>"
        with pytest.raises(TemplateUnsupported):
            compile_template(deep)
        nots = "<%= " + "!" * 5000 + "true %>"
        with pytest.raises(TemplateUnsupported):
            compile_template(nots)
        ifs = "<% if (true) { %>" * (MAX_NESTING + 5) + "x" + "<% } %>" * (MAX_NESTING + 5)
        with pytest.raises(TemplateUnsupported):
            compile_template(ifs)

    def test_nesting_within_the_limit_is_fine(self) -> None:
        ifs = "<% if (true) { %>" * 10 + "x" + "<% } %>" * 10
        assert run(ifs) == "x"


def _probe_expr(level: int, budget_start: int) -> str:
    """括号套括号，每层后面接一串取属性、比较与相等的连写

    每条链单独看都不长，但树会一层层叠深；第一版只按「链有多长」计数，计数器不过 40，
    树却有两千多层深，求值时撞 Python 的递归上限。
    """
    if level <= 0:
        return "1"
    inner = _probe_expr(level - 1, budget_start + 1)
    links = 40 - budget_start
    if links <= 0:
        return "1"
    return "(" + inner + ")" + ".a" * links + " < 1" * links + " == 1" * links


class TestDepthLimits:
    @pytest.mark.parametrize(
        "source",
        [
            "<%= " + _probe_expr(40, 1) + " %>",
            "<% if (" + _probe_expr(40, 2) + ") { %>x<% } %>",
            "<% if (!(" + _probe_expr(40, 3) + ")) { %>x<% } %>",
            "<% if (" + _probe_expr(40, 2) + ") { %>" + "y" * 10 + "<% } %>",
        ],
        ids=["deep-output", "deep-if-cond", "deep-if-false", "deep-if-const"],
    )
    def test_review_probes_are_rejected_without_recursion_error(self, source: str) -> None:
        with pytest.raises(TemplateUnsupported):
            compile_template(source)
        # 防盗扫描走的是宽松扫描，也不能撞递归上限
        template_lite.scan_template(source)

    def test_every_level_of_the_probe_is_either_fine_or_rejected(self) -> None:
        for level in range(1, 41):
            source = "<%= " + _probe_expr(level, 1) + " %>"
            try:
                template = compile_template(source)
            except TemplateUnsupported:
                continue
            try:
                render(template)
            except TemplateRuntimeError:
                pass

    def test_chains_count_toward_the_tree_depth(self) -> None:
        with pytest.raises(TemplateUnsupported):
            compile_template("<%= getvar('a')" + ".b" * (MAX_EXPR_DEPTH + 5) + " %>")
        with pytest.raises(TemplateUnsupported):
            compile_template("<%= " + " === ".join(["1"] * (MAX_EXPR_DEPTH + 5)) + " %>")
        nested: dict = {"b": "底"}
        for _ in range(9):
            nested = {"b": nested}
        assert run("<%= getvar('a')" + ".b" * 10 + " %>", cache={"a": nested}) == "底"

    def test_else_if_chains_do_not_count_as_nesting(self) -> None:
        def chain(count: int, last: str = "b") -> str:
            return (
                "<% if (false) { %>a"
                + "".join("<% } else if (false) { %>b" for _ in range(count))
                + f"<% }} else {{ %>{last}<% }} %>"
            )

        # `else if` 在同一层，链再长也不递归；渲染、字数统计都是迭代的
        assert run(chain(MAX_NESTING + 5)) == "b"
        long_chain = chain(2000, last="尾")
        assert len(long_chain) <= MAX_TEMPLATE_CHARS
        assert run(long_chain) == "尾"
        scan = template_lite.scan_template(long_chain, compile_template(long_chain))
        assert scan.dead_branch_chars == 2001
        # 真正的嵌套（if 里套 if）仍按 40 层封顶
        nested = "<% if (true) { %>" * (MAX_NESTING + 5) + "x" + "<% } %>" * (MAX_NESTING + 5)
        with pytest.raises(TemplateUnsupported):
            compile_template(nested)


class TestSizeLimits:
    def test_long_templates_are_unsupported(self) -> None:
        with pytest.raises(TemplateUnsupported, match="超过"):
            compile_template("<%= 1 %>" + "字" * MAX_TEMPLATE_CHARS)

    def test_reasons_are_short(self) -> None:
        with pytest.raises(TemplateUnsupported) as caught:
            compile_template("<% '" + "A" * 50_000 + "' %>")
        assert len(str(caught.value)) <= MAX_REASON_CHARS
        with pytest.raises(TemplateUnsupported) as caught:
            compile_template("<% " + "某个很长很长的标识符" * 500 + " %>")
        assert len(str(caught.value)) <= MAX_REASON_CHARS

    def test_output_is_capped(self) -> None:
        big = MappingVariables(cache={"v": "x" * 10_000})
        template = compile_template("<%= getvar('v') %>" * 10)
        with pytest.raises(TemplateRuntimeError, match="超过"):
            render(template, big)
        assert len(render(compile_template("<%= getvar('v') %>" * 6), big)) == 60_000

    def test_deeply_nested_values_are_capped(self) -> None:
        value: list = []
        for _ in range(MAX_VALUE_DEPTH + 5):
            value = [value]
        with pytest.raises(TemplateRuntimeError):
            render(compile_template("<%= getvar('v') %>"), MappingVariables(cache={"v": value}))

    def test_mapping_paths_only_take_ascii_digits_as_list_indexes(self) -> None:
        variables = MappingVariables(cache={"l": [1, 2, 3]})
        assert render(compile_template("<%= getvar('l.1') %>"), variables) == "2"
        assert render(compile_template("<%= getvar('l.²') %>"), variables) == ""


class TestMissingVariableBait:
    def test_existence_check_bait_is_counted(self) -> None:
        source = "<% if (!getvar('stat_data')) { %>" + "B" * 2000 + "<% } %>ok"
        assert template_lite.missing_variable_branch_chars(compile_template(source)) == 2000
        source = "<% if (getvar('x') === undefined || getvar('y') == null) { %>" + "B" * 1500 + "<% } %>"
        assert template_lite.missing_variable_branch_chars(compile_template(source)) == 1500
        source = "<% if (getvar('stat_data')) { %>正常<% } else { %>" + "B" * 1200 + "<% } %>"
        assert template_lite.missing_variable_branch_chars(compile_template(source)) == 1200

    def test_value_comparisons_are_not_bait(self) -> None:
        # 比的是值：有变量的客户端里同样可能走到 else，不能算诱饵
        source = "<% if (getvar('剧情权重') >= 3) { %>后期<% } else { %>" + "前期设定" * 600 + "<% } %>"
        assert template_lite.missing_variable_branch_chars(compile_template(source)) == 0

    def test_branches_that_are_not_taken_do_not_count(self) -> None:
        source = "<% if (getvar('stat_data')) { %>" + "B" * 2000 + "<% } %>"
        assert template_lite.missing_variable_branch_chars(compile_template(source)) == 0


class TestScale:
    def test_large_template_compiles_and_renders_quickly(self) -> None:
        paragraph = "这是一段很长的设定文字。" * 40
        block = f"<% if (getvar('阶段') >= 1) {{ %>{paragraph}<% }} else {{ %>{paragraph}<% }} %>\n"
        source = block * 60  # 约 6 万字、120 个分支，贴着上限
        assert len(source) <= MAX_TEMPLATE_CHARS
        started = time.monotonic()
        template = compile_template(source)
        output = render(template)
        assert time.monotonic() - started < 3
        assert len(output) == len(paragraph) * 60 + 60

    def test_long_logical_chains_do_not_hit_the_recursion_limit(self) -> None:
        # `&&` / `||` 拍平成一个节点：几千个连写也只有一层深。
        assert run("<%= " + " && ".join(["true"] * 5000) + " %>") == "true"
        assert run("<%= " + " || ".join(["false"] * 4999 + ["'末尾'"]) + " %>") == "末尾"

    @pytest.mark.parametrize(
        "source",
        [
            "<%= 1 %>" + " " * 300_000 + "x",  # `<%_` 前的去空白
            "<% " + "/* " * 100_000,  # 大量未闭合的块注释
            "<% " + "`\\" * 150_000,  # 大量反引号与转义
            "<%" * 150_000,  # 大量未闭合的标签
        ],
        ids=["spaces-before-slurp", "unclosed-block-comments", "backticks", "unclosed-tags"],
    )
    def test_pathological_inputs_stay_linear(self, source: str) -> None:
        started = time.monotonic()
        try:
            template = compile_template(source)
            render(template)
        except TemplateUnsupported:
            pass
        template_lite.scan_template(source)
        assert time.monotonic() - started < 3

    def test_output_never_contains_code(self) -> None:
        source = "甲<% if (getvar('x')) { %>乙<% } %>丙<%= getvar('y') %>丁<%# 注释 %>"
        output = run(source)
        assert "<%" not in output and "%>" not in output and "getvar" not in output


class TestScan:
    def test_env_probe_counts_conditional_text(self) -> None:
        filler = "这是一段与正文无关的说明文字。" * 100
        source = f"<% if (localStorage.getItem('piupiu_x') || window.__x) {{ %>{filler}<% }} %>"
        scan = template_lite.scan_template(source)
        assert scan.env_identifiers == frozenset({"localStorage", "window"})
        assert scan.conditional_chars == len(filler)

    def test_dead_branch_chars(self) -> None:
        filler = "藏起来的文字" * 300
        source = f"正常的开头<% if (false) {{ %>{filler}<% }} %>"
        scan = template_lite.scan_template(source, compile_template(source))
        assert scan.dead_branch_chars == len(filler)
        assert scan.env_identifiers == frozenset()

    def test_variable_conditions_are_not_dead_branches(self) -> None:
        source = "<% if (getvar('x') === 1) { %>" + "字" * 2000 + "<% } %>"
        assert template_lite.scan_template(source, compile_template(source)).dead_branch_chars == 0


class TestBaitVariants:
    """复验找出的几种「变量钥匙」诱饵写法（启发式，枚举的是常见写法）"""

    BAIT = "B" * 2000

    @pytest.mark.parametrize(
        "condition",
        [
            "!getvar('s')",
            "getvar('s') == null",
            "String(getvar('s')) == 'undefined'",
            "String(getvar('s')) === 'undefined'",
            "getvar('s') == null || getvar('s') == ''",
            "!getvar('s') && true",
            "true && !getvar('s')",
            "!getvar('s') || false",
            "!getvar('s') && !0",
        ],
    )
    def test_text_bait(self, condition: str) -> None:
        source = f"<% if ({condition}) {{ %>{self.BAIT}<% }} %>ok"
        assert template_lite.missing_variable_branch_chars(compile_template(source)) == 2000

    def test_bait_written_as_output_literal(self) -> None:
        for tag in ("<%=", "<%-"):
            source = f"<% if (!getvar('s')) {{ %>{tag} '{self.BAIT}' %><% }} %>ok"
            assert template_lite.missing_variable_branch_chars(compile_template(source)) == 2000
        source = f"<% if (!getvar('s')) {{ %><%= '{'B' * 1000}' || '{'B' * 1000}' %><% }} %>"
        assert template_lite.missing_variable_branch_chars(compile_template(source)) == 2000

    def test_value_checks_are_still_not_bait(self) -> None:
        for condition in ("getvar('s') !== 'ready'", "!(getvar('s') > 0)", "getvar('s') == 'x' && true"):
            source = f"<% if ({condition}) {{ %>{self.BAIT}<% }} %>"
            assert template_lite.missing_variable_branch_chars(compile_template(source)) == 0

    def test_absorbing_literals_make_the_condition_constant(self) -> None:
        # `|| true` 在有变量的客户端里同样成立，不是诱饵；`&& false` 永远不成立
        for condition in ("!getvar('s') || true", "getvar('s') || 'x'", "!getvar('s') && false", "0 && !getvar('s')"):
            source = f"<% if ({condition}) {{ %>{self.BAIT}<% }} %>"
            assert template_lite.missing_variable_branch_chars(compile_template(source)) == 0


class TestStringsAndIndexes:
    def test_surrogate_pairs_are_joined(self) -> None:
        assert run("<%= '\\ud83d\\ude00' %>") == "\U0001F600"
        assert run("<%= '\\ud83d\\ude00' %>").encode("utf-8")

    def test_lone_surrogates_are_unsupported(self) -> None:
        for source in ("<%= '\\ud800' %>", "<%= '\\ude00x' %>", "<%= 'a\\ud83d' %>"):
            with pytest.raises(TemplateUnsupported, match="代理"):
                compile_template(source)

    def test_long_numeric_path_parts_are_not_indexes(self) -> None:
        variables = MappingVariables(cache={"l": [1, 2]})
        assert render(compile_template("<%= getvar('l." + "1" * 5000 + "') %>"), variables) == ""
        assert render(compile_template("<%= getvar('l.0000000001') %>"), variables) == ""
        assert render(compile_template("<%= getvar('l.000000001') %>"), variables) == "2"


class TestRenderComparison:
    """诱饵按几次渲染对比判：变量读不到时才会注入的字面文字"""

    BAIT = "B" * 2000

    @pytest.mark.parametrize(
        "condition",
        [
            "getvar('s') === getvar('__never__')",
            "!getvar('s') == true",
            "(getvar('s') || 'n') == 'n'",
            "'undefined'.includes(String(getvar('s')))",
            "String(getvar('s')).toLowerCase() == 'undefined'",
            "!(getvar('s') && getvar('s').a.b)",
            "!getvar('stat_data.a.b')",
        ],
    )
    def test_existence_checks_written_any_way(self, condition: str) -> None:
        source = f"<% if ({condition}) {{ %>{self.BAIT}<% }} %>ok"
        assert template_lite.compare_renders(compile_template(source)).bait_chars == 2000

    def test_top_level_output_with_a_fallback_literal(self) -> None:
        source = f"<%= getvar('s') || '{self.BAIT}' %>ok"
        comparison = template_lite.compare_renders(compile_template(source))
        assert comparison.bait_chars == 2000
        assert comparison.empty_text == self.BAIT + "ok"

    def test_bait_balanced_by_an_equally_long_else_branch(self) -> None:
        # 总长一样也判得出：按节点比，不按总长比
        source = f"<% if (!getvar('s')) {{ %>{self.BAIT}<% }} else {{ %>{'C' * 2000}<% }} %>"
        assert template_lite.compare_renders(compile_template(source)).bait_chars == 2000

    def test_property_chains_on_present_variables_do_not_error(self) -> None:
        source = f"<% if (getvar('s') && getvar('s').a.b) {{ %>x<% }} else {{ %>{self.BAIT}<% }} %>"
        assert template_lite.compare_renders(compile_template(source)).bait_chars == 2000

    @pytest.mark.parametrize(
        "condition",
        ["getvar('s') !== 'ready'", "!(getvar('s') > 0)", "getvar('w') >= 3", "!getvar('s') || true", "true"],
    )
    def test_conditions_that_do_not_depend_on_presence_are_not_bait(self, condition: str) -> None:
        source = f"<% if ({condition}) {{ %>{self.BAIT}<% }} %>"
        assert template_lite.compare_renders(compile_template(source)).bait_chars == 0

    def test_variable_values_are_not_counted_as_text(self) -> None:
        source = "<%= getvar('a') %>" * 500 + "<%= getvar('b') && getvar('b').c.d %>"
        comparison = template_lite.compare_renders(compile_template(source))
        assert comparison.bait_chars == 0 and comparison.empty_text == ""

    def test_includes_on_a_present_variable_is_deterministic(self) -> None:
        # 哨兵是随机串：`includes` 在哨兵上按「没有这一段」算，结果不随哨兵变
        source = (
            f"<% if (getvar('list') && getvar('list').includes('a')) {{ %>x<% }} else {{ %>{self.BAIT}<% }} %>"
        )
        results = {template_lite.compare_renders(compile_template(source)).bait_chars for _ in range(20)}
        assert results == {0}

    def test_empty_render_error(self) -> None:
        comparison = template_lite.compare_renders(compile_template("<%= getvar('a').b %>"))
        assert comparison.empty_text is None and comparison.bait_chars == 0


class TestDeepChainsAreSafeToHashAndPrint:
    def test_hash_eq_and_repr_do_not_recurse(self) -> None:
        # 一个标签里 4000 节的 `else if` 链：语法树有 4000 层，默认的 `__hash__` / `__repr__` 五百层就会撞上限
        source = "<%if(true){}" + "else if(true){}" * 4000 + "%>end"
        assert len(source) <= MAX_TEMPLATE_CHARS
        template = compile_template(source)
        assert hash(template) == hash(template)
        assert template == template and template != compile_template(source)
        assert "node_count" in repr(template)
        node = template.nodes[0]
        assert hash(node) and node == node and len(repr(node)) < 200


class TestRenderComparisonHardening:
    """渲染对比的几个绕法：同一节点二选一、故意让哨兵出错、认出哨兵"""

    BAIT = "B" * 2000

    def test_same_output_node_choosing_between_bait_and_equal_length_text(self) -> None:
        source = f"<%= (!getvar('s') && '{self.BAIT}') || '{'C' * 2000}' %>ok"
        assert template_lite.compare_renders(compile_template(source)).bait_chars == 2000

    def test_forcing_the_present_render_to_fail_is_treated_as_bait(self) -> None:
        source = (
            "<% if (getvar('s') && getvar('s').length.a.b) { %><% } %>"
            f"<% if (getvar('s') === getvar('__never__')) {{ %>{self.BAIT}<% }} %>ok"
        )
        comparison = template_lite.compare_renders(compile_template(source))
        assert comparison.empty_text == self.BAIT + "ok"
        assert comparison.bait_chars == len(comparison.empty_text)

    def test_a_failing_present_render_with_little_text_stays_small(self) -> None:
        source = "<% if (getvar('s') && getvar('s').length.a.b) { %><% } %>短短一句"
        assert template_lite.compare_renders(compile_template(source)).bait_chars == len("短短一句")

    @pytest.mark.parametrize(
        "condition",
        [
            "!getvar('s') || getvar('s').length == 7",
            "!getvar('s') || getvar('s').a !== getvar('s.a')",
            "!getvar('s') || getvar('s') != getvar('s').toLowerCase()",
            "!getvar('s') || String(getvar('s')).includes('x')",
        ],
    )
    def test_conditions_that_try_to_recognise_the_placeholder(self, condition: str) -> None:
        source = f"<% if ({condition}) {{ %>{self.BAIT}<% }} %>ok"
        results = {template_lite.compare_renders(compile_template(source)).bait_chars for _ in range(30)}
        assert results == {2000}

    def test_placeholders_are_random_lowercase_alphanumerics(self) -> None:
        lengths = set()
        for _ in range(200):
            variables = template_lite._SentinelVariables()
            value = variables.get("cache", "s")
            assert value.isascii() and value.isalnum() and value == value.lower()
            assert template_lite._SENTINEL_MIN_LENGTH <= len(value) <= template_lite._SENTINEL_MAX_LENGTH
            assert variables.get("cache", "s") is value
            assert value.prop("a") is variables.get("cache", "s.a")
            assert variables.get("cache", "t") != value
            lengths.add(len(value))
        assert len(lengths) > 3

    def test_placeholders_compare_like_strings(self) -> None:
        value = template_lite._SentinelVariables().get("cache", "s")
        assert template_lite._loose_equal(value, str(value))
        assert template_lite._strict_equal(value, str(value))


class TestPlaceholderSets:
    """四组哨兵：短而靠前、长而靠后、按种子随机的小写与混合；`includes` 的恒真写法"""

    BAIT = "B" * 2000

    @pytest.mark.parametrize(
        "source",
        [
            f"<% if (!getvar('s') || !getvar('s').includes('')) {{ %>{BAIT}<% }} %>ok",
            f"<% if (!getvar('s') || !getvar('s').includes(getvar('s'))) {{ %>{BAIT}<% }} %>ok",
            f"<% if (!getvar('s') || !String(getvar('s')).includes('')) {{ %>{BAIT}<% }} %>ok",
            f"<% if (!getvar('s') || !getvar('s').toLowerCase().includes('')) {{ %>{BAIT}<% }} %>ok",
            f"<% if (!getvar('s') || getvar('s').a.includes('') == false) {{ %>{BAIT}<% }} %>ok",
            f"<%= (getvar('s') && getvar('s').includes('') && 'ok') || '{BAIT}' %>",
        ],
    )
    def test_includes_tautologies_are_caught(self, source: str) -> None:
        assert template_lite.compare_renders(compile_template(source), seed="x").bait_chars >= 2000

    @pytest.mark.parametrize(
        "condition",
        [
            "!getvar('s') || getvar('s').length < 16",
            "!getvar('s') || getvar('s').length > 20",
            "!getvar('s') || getvar('s') < 'n'",
            "!getvar('s') || String(getvar('s')) > 'm'",
            "!getvar('s') || getvar('s.a') < getvar('s.b')",
            "!(getvar('s') < 'z')",
        ],
    )
    def test_length_and_order_checks_are_caught_with_any_seed(self, condition: str) -> None:
        template = compile_template(f"<% if ({condition}) {{ %>{self.BAIT}<% }} %>ok")
        results = {template_lite.compare_renders(template, seed=str(seed)).bait_chars for seed in range(50)}
        assert results == {2000}

    def test_same_seed_same_verdict(self) -> None:
        # 只在随机那组身上可能不同的写法：长度恰好等于某个数
        template = compile_template(f"<% if (!getvar('s') || getvar('s').length != 13) {{ %>{self.BAIT}<% }} %>ok")
        verdicts = {template_lite.compare_renders(template, seed="同一条正文").bait_chars for _ in range(20)}
        assert len(verdicts) == 1

    def test_benign_length_branch_is_treated_as_bait(self) -> None:
        # 有意的取舍：变量短时（或读不到时）显示一大段默认文字，与诱饵分不开，按诱饵处理
        source = "<% if (getvar('s') && getvar('s').length > 15) { %>短<% } else { %>" + "n" * 1500 + "<% } %>"
        assert template_lite.compare_renders(compile_template(source), seed="x").bait_chars == 1500

    def test_fixed_sets_sort_and_measure_as_described(self) -> None:
        short = template_lite._SentinelVariables(template_lite._short_low)
        long = template_lite._SentinelVariables(template_lite._long_high)
        assert len(short.get("cache", "s")) == template_lite._SENTINEL_MIN_LENGTH
        assert len(long.get("cache", "s")) == template_lite._SENTINEL_MAX_LENGTH
        assert short.get("cache", "s") < "b" and long.get("cache", "s") > "y"
        names = {short.get("cache", f"v{index}") for index in range(1000)}
        assert len(names) == 1000
        # 两组固定的哨兵里，序号的先后一样
        assert short.get("cache", "v1") < short.get("cache", "v2")
        assert long.get("cache", "v1") < long.get("cache", "v2")
        # 序号超过四位字母也照样各不相同
        assert template_lite._index_letters(26**4) == "baaaa"
        assert template_lite._index_letters(26**4 - 1) == "zzzz"


BACKSLASH = "\\"


def _bait_with_seeds(condition: str, seeds: int = 50) -> set[int]:
    template = compile_template(f"<% if ({condition}) {{ %>{'B' * 2000}<% }} %>ok")
    return {template_lite.compare_renders(template, seed=str(seed)).bait_chars for seed in range(seeds)}


class TestPartialVariables:
    """「只有一部分变量」的几次渲染：根变量没设、只有根变量；两个变量比大小不误判"""

    BAIT = "B" * 2000

    @pytest.mark.parametrize(
        "condition",
        [
            "!getvar('a') || getvar('never_set')",
            "!getvar('a') || getvar('never_set.x')",
            "!getvar('a') || getvar('never_set').x",
            "!getvar('a') || getvar('never_set').length",
            "!getvar('s') || getvar('s').zz_unset",
            "!getvar('s') || getvar('s.zz_unset')",
            "!getvar('s') || getvar('s').zz.yy",
            "!getLocalVar('s') || getvar('s')",
        ],
    )
    def test_a_variable_that_is_never_set_is_caught(self, condition: str) -> None:
        assert _bait_with_seeds(condition, seeds=5) == {2000}

    def test_many_roots_are_split_by_bits(self) -> None:
        # 超过 8 个根变量时按序号的二进制位分组：两个变量配合的写法放在一堆别的变量后面照样判得出
        decoys = " || ".join(f"getvar('d{index}') === 'q'" for index in range(20))
        assert _bait_with_seeds(f"{decoys} || !getvar('a') || getvar('never_set')", seeds=5) == {2000}
        assert _bait_with_seeds(f"!getvar('a') || getvar('never_set') || {decoys}", seeds=5) == {2000}

    def test_code_that_fails_only_when_a_variable_is_missing_does_not_hide_bait(self) -> None:
        # 「只有一部分变量」的世界里对 undefined 取属性得到 undefined，不能靠一句故意出错的代码把它废掉
        source = (
            "<% if (getvar('c')) { %><%= getvar('b').x.y %><% } %>"
            f"<% if (!getvar('a') || getvar('b')) {{ %>{self.BAIT}<% }} %>ok"
        )
        assert template_lite.compare_renders(compile_template(source), seed="x").bait_chars == 2000

    @pytest.mark.parametrize(
        "source",
        [
            "<% if (getvar('hp') < getvar('maxhp')) { %>受伤<% } else { %>" + "满" * 1500 + "<% } %>",
            "<% if (getvar('hp') > getvar('maxhp')) { %>溢出<% } else { %>" + "满" * 1500 + "<% } %>",
            "<% if (getvar('hp') <= getvar('maxhp')) { %>" + "满" * 1500 + "<% } else { %>溢出<% } %>",
            "<% if (getvar('hp') >= getvar('maxhp')) { %>" + "满" * 1500 + "<% } else { %>受伤<% } %>",
            "<% if (getvar('stat.hp') < getvar('stat.maxhp')) { %>受伤<% } else { %>" + "满" * 1500 + "<% } %>",
        ],
    )
    def test_comparing_two_variables_is_not_bait(self, source: str) -> None:
        template = compile_template(source)
        assert {template_lite.compare_renders(template, seed=str(seed)).bait_chars for seed in range(30)} == {0}

    @pytest.mark.parametrize(
        "source",
        [
            # 一个变量守着、读另一个变量的属性：只有其中一个没设时 JS 会抛错，这里不算诱饵
            "<% if (getvar('flag')) { %><%= getvar('stat').hp.cur %><% } %>" + "正文" * 600,
            "<%= getvar('stat_data.角色.好感度') %>" + "正文" * 600,
            "<% if (getvar('stat_data')) { %><%= getvar('stat_data').角色.好感度 %><% } %>" + "正文" * 600,
            "<% if (getvar('a') && getvar('b')) { %>都有<% } %>" + "正文" * 600,
            "<%= getvar('name') %>的设定：" + "正文" * 600,
        ],
    )
    def test_ordinary_templates_with_long_static_text_are_not_bait(self, source: str) -> None:
        assert template_lite.compare_renders(compile_template(source), seed="x").bait_chars == 0

    def test_partial_renders_only_run_when_they_can_change_the_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []
        real = template_lite._partial_worlds

        def counting(base, template):  # noqa: ANN001, ANN202
            for world in real(base, template):
                calls.append(1)
                yield world

        monkeypatch.setattr(template_lite, "_partial_worlds", counting)
        # 「读不到」那次输出的字不到门槛：不必再渲染
        short = "<% if (!getvar('a') || getvar('never_set')) { %>短<% } %>"
        assert template_lite.compare_renders(compile_template(short), seed="x").bait_chars == 0
        assert calls == []
        # 四组哨兵已经判出来了：不必再渲染
        caught = f"<% if (!getvar('a')) {{ %>{self.BAIT}<% }} %>"
        assert template_lite.compare_renders(compile_template(caught), seed="x").bait_chars == 2000
        assert calls == []
        # 判出来就停：第一次（只有根变量）就判出来了
        prop = f"<% if (!getvar('a') || getvar('a').zz) {{ %>{self.BAIT}<% }} %>"
        assert template_lite.compare_renders(compile_template(prop), seed="x").bait_chars == 2000
        assert calls == [1]


class TestPlaceholderShapes:
    """按长度、字典序、大小写认哨兵的写法（混合那组的形状与固定的两组不同）"""

    @pytest.mark.parametrize(
        "condition",
        [
            "!getvar('s') || getvar('s') < '{'",
            "!getvar('s') || getvar('s') < '一'",
            "!getvar('s') || getvar('s').length >= 8",
            "!getvar('s') || getvar('s').length > 3",
            "!getvar('s') || getvar('s').length == getvar('w').length",
            "!getvar('s') || getvar('s').toLowerCase() === getvar('s')",
            "!getvar('s') || getvar('s') == String(getvar('s')).toLowerCase()",
        ],
    )
    def test_shape_checks_are_caught_with_any_seed(self, condition: str) -> None:
        assert _bait_with_seeds(condition) == {2000}

    def test_mixed_placeholders_have_the_described_shape(self) -> None:
        for seed in range(20):
            variables = template_lite._SentinelVariables(template_lite._mixed_maker(str(seed)))
            values = [variables.get("cache", f"v{index}") for index in range(60)]
            assert len(set(values)) == 60
            assert sorted(len(value) for value in values[:24]) == list(range(1, 25))
            for value in values:
                assert value[0] > "{" and value != value.lower()
                assert all(char <= chr(0xFFFF) for char in value)
                assert 1 <= len(value) <= template_lite._SENTINEL_MAX_LENGTH
                assert value.lowered() == value.lower() and value.lowered().path == value.path

    def test_placeholders_never_look_like_numbers(self) -> None:
        # 像数的哨兵会让 `getvar(x) > 0` 这种按值比的条件随抽到的哨兵变
        for seed in range(300):
            for maker in (template_lite._seeded_maker(str(seed)), template_lite._mixed_maker(str(seed))):
                variables = template_lite._SentinelVariables(maker)
                for index in range(5):
                    assert math.isnan(template_lite._to_number(variables.get("cache", f"v{index}")))
        template = compile_template("<% if (getvar('好感') > 0) { %>x<% } else { %>" + "n" * 1500 + "<% } %>")
        assert {template_lite.compare_renders(template, seed=str(seed)).bait_chars for seed in range(300)} == {0}

    def test_same_seed_gives_the_same_mixed_set(self) -> None:
        first = template_lite._mixed_maker("同一条正文")
        second = template_lite._mixed_maker("同一条正文")
        assert [first(index) for index in range(30)] == [second(index) for index in range(30)]

    def test_two_placeholders_never_compare_as_ordered(self) -> None:
        variables = template_lite._SentinelVariables(template_lite._short_low)
        a, b = variables.get("cache", "a"), variables.get("cache", "b")
        for op in ("<", "<=", ">", ">="):
            assert template_lite._compare(op, a, b) is False
        assert template_lite._compare("<=", a, a) is True
        assert template_lite._compare("<", a, "zzz") is True


class TestComparisonCost:
    """「只有一部分变量」的渲染次数、编译前判断会不会读变量（记账用）"""

    TEXT = "正文" * 600

    @pytest.mark.parametrize(
        ("roots", "expected"),
        [(1, 1), (2, 3), (8, 9), (9, 9), (16, 9), (17, 11), (2000, 23)],
    )
    def test_partial_render_counts(self, roots: int, expected: int) -> None:
        # 根变量不超过 8 个：只有根变量一次加每个各没设一次（只有一个时「它没设」就是全读不到）；更多：按位分组
        # 正文放在读变量、但几次都成立的分支里：受变量影响，又没判出来，「只有一部分变量」那几次都要做
        condition = " || ".join(f"getvar('r{index}') === 'q'" for index in range(roots))
        template = compile_template(f"<% if ({condition} || true) {{ %>{self.TEXT}<% }} %>")
        assert len(template.roots) == roots
        assert template_lite.compare_renders(template, seed="x").partial_renders == expected

    def test_no_partial_renders_when_the_verdict_is_already_known(self) -> None:
        few = compile_template("<% if (getvar('a') && getvar('b')) { %>甲<% } %>短短一句")
        assert template_lite.compare_renders(few, seed="x").partial_renders == 0
        bait = compile_template(f"<% if (!getvar('a')) {{ %>{self.TEXT}<% }} %>")
        assert template_lite.compare_renders(bait, seed="x").partial_renders == 0
        plain = compile_template(f"<% if (true) {{ %>{self.TEXT}<% }} %>")
        assert template_lite.compare_renders(plain, seed="x").partial_renders == 0

    def test_no_partial_renders_when_the_long_text_does_not_depend_on_variables(self) -> None:
        # 一千多字都是顶层正文或不读变量的分支：哪个世界里都一样，拿掉变量也判不出什么
        for source in (
            f"<% if (getvar('a') && getvar('b')) {{ %>甲<% }} %>{self.TEXT}",
            f"<%= getvar('a') %><% if (1 == 1) {{ %>{self.TEXT}<% }} %>",
        ):
            assert template_lite.compare_renders(compile_template(source), seed="x").partial_renders == 0
        dependent = compile_template(f"<% if (getvar('a') || true) {{ %>{self.TEXT}<% }} %>")
        assert template_lite.compare_renders(dependent, seed="x").partial_renders == 1

    @pytest.mark.parametrize(
        "source",
        [
            "<%= getvar('a.b') %><%= getLocalVar('a') %>",
            "<% if (getvar /* 注释 */ ('a')) { %>甲<% } %>",
            "<% if (getvar %><% ('a')) { %>甲<% } %>",
            f"<%= getvar('a{BACKSLASH}u002eb') %>",
        ],
    )
    def test_reads_variables_before_compiling(self, source: str) -> None:
        assert compile_template(source).roots
        assert template_lite.reads_variables(source)

    def test_roots_are_collected_once_each(self) -> None:
        source = (
            "<%= getvar('stat_data.角色.好感度') %>" * 20
            + "<% if (getvar('flag') && getLocalVar('flag')) { %>甲<% } %>"
        )
        assert compile_template(source).roots == (
            ("cache", "stat_data"),
            ("cache", "flag"),
            ("local", "flag"),
        )
        assert not template_lite.reads_variables("<% if (true) { %>甲<% } %>")


class TestKnownGaps:
    """还绕得过去的写法（`compare_renders` 的文档串里列着）：判出来了这些用例会失败，提醒去改那份说明"""

    @pytest.mark.xfail(strict=True, reason="两个从来不设的变量：每次只让一个根变量没设")
    def test_two_variables_that_are_never_set(self) -> None:
        assert _bait_with_seeds("!getvar('a') || getvar('n1') || getvar('n2')", seeds=5) == {2000}

    @pytest.mark.xfail(strict=True, reason="两个变量比大小按数值比，取反以后几次都成立")
    def test_negated_comparison_of_two_variables(self) -> None:
        assert _bait_with_seeds("!getvar('a') || !(getvar('a') < getvar('b'))", seeds=5) == {2000}

    @pytest.mark.xfail(strict=True, reason="按值比较取反：哨兵不是数，几次都成立")
    def test_negated_numeric_comparison(self) -> None:
        assert _bait_with_seeds("!getvar('s') || !(getvar('s') < 50)", seeds=5) == {2000}

    @pytest.mark.xfail(strict=True, reason="长度超出所有哨兵的范围")
    def test_length_above_every_placeholder(self) -> None:
        assert _bait_with_seeds("!getvar('s') || !(getvar('s').length > 24)", seeds=5) == {2000}

    @pytest.mark.xfail(strict=True, reason="按类型认：哨兵都是字符串")
    def test_type_based_check(self) -> None:
        assert _bait_with_seeds("!getvar('s') || String(getvar('s')) != '[object Object]'", seeds=5) == {2000}

    @pytest.mark.xfail(strict=True, reason="路径超过 8 条、三条路径配合时按位分组不一定分得开")
    def test_three_variables_with_many_roots(self) -> None:
        # 序号 a=0、b=1、c=3：没有哪一位上 a 与 c 相同而 b 不同
        decoys = " || ".join(f"getvar('d{index}') === 'q'" for index in range(5))
        condition = f"!getvar('a') || getvar('b') || getvar('x') === 'q' || !getvar('c') || {decoys}"
        assert len(compile_template(f"<% if ({condition}) {{ %>x<% }} %>").roots) == 9
        assert _bait_with_seeds(condition, seeds=5) == {2000}

    @pytest.mark.xfail(strict=True, reason="几个条件或起来分别对上每组哨兵的形状")
    def test_shape_disjunction(self) -> None:
        low, high = _js_char(0x0002), _js_char(0xFFFF)
        condition = (
            f"!getvar('k') || getvar('k') < '{low}' || getvar('k') >= '{high}'"
            " || (getvar('k') >= 'a' && getvar('k') < 'g') || getvar('k').toLowerCase() != getvar('k')"
        )
        assert _bait_with_seeds(condition, seeds=5) == {2000}

    @pytest.mark.xfail(strict=True, reason="两个变量相等（按值比较）：哨兵各不相同")
    def test_two_fields_that_must_be_equal(self) -> None:
        assert _bait_with_seeds("!getvar('s.a') || getvar('s.a') != getvar('s.b')", seeds=5) == {2000}

    @pytest.mark.xfail(strict=True, reason="路径深过 MAX_UNIT_DEPTH 层：更深的字段跟着祖先走，分不开")
    def test_fields_deeper_than_the_unit_depth(self) -> None:
        assert _bait_with_seeds("!getvar('s.a.b.c.d.x') || getvar('s.a.b.c.d.never')", seeds=5) == {2000}

    @pytest.mark.xfail(strict=True, reason="节点超过 8 个、第 2 层都只有一个孩子时，第 2 层不单独拿掉")
    def test_only_children_on_the_second_level(self) -> None:
        decoys = "".join(f"<%= getvar('r{index}') %>" for index in range(8))
        condition = "!getvar('s') || !getvar('s').length || getvar('s.x')"
        source = decoys + f"<% if ({condition}) {{ %>{'B' * 2000}<% }} %>ok"
        template = compile_template(source)
        assert len(template_lite._unit_tree(template)[1]) > template_lite.MAX_SINGLE_UNSET_RENDERS
        assert template_lite.compare_renders(template, seed="x").bait_chars == 2000

    @pytest.mark.xfail(strict=True, reason="两个哨兵比大小一律不成立，同一个变量与它转小写的结果也算两个")
    def test_variable_against_its_lower_case(self) -> None:
        assert _bait_with_seeds("!getvar('v') || !(getvar('v') < getvar('v').toLowerCase())", seeds=5) == {2000}


# JS 的真值（node 24 实测，值写死在这里，跑用例不需要 node）
JS_NUMBER = [
    ("0x10", "16"),
    ("0X1f", "31"),
    ("0o17", "15"),
    ("0b11", "3"),
    ("-0x10", "NaN"),
    ("+0x10", "NaN"),
    ("0x", "NaN"),
    ("0b2", "NaN"),
    ("0o8", "NaN"),
    (" 12 ", "12"),
    ("\ufeff1", "1"),
    ("\xa01", "1"),
    ("\u20001", "1"),
    ("\u3000 5", "5"),
    ("\u202f8", "8"),
    ("\u20281\u2029", "1"),
    ("\x1c5", "NaN"),
    ("\x855", "NaN"),
    ("\u180e5", "NaN"),
    ("1_0", "NaN"),
    ("\uff11", "NaN"),
    ("\u0661", "NaN"),
    ("", "0"),
    ("  ", "0"),
    (".5", "0.5"),
    ("5.", "5"),
    ("-.5", "-0.5"),
    ("1E+3", "1000"),
    ("Infinity", "Infinity"),
    ("-Infinity", "-Infinity"),
    ("infinity", "NaN"),
    ("NaN", "NaN"),
    ("1e", "NaN"),
    ("12abc", "NaN"),
    ("0.0000001", "1e-7"),
    ("00", "0"),
    ("010", "10"),
    ("-0", "-0"),
    ("1e400", "Infinity"),
    ("\t\n\x0b\x0c\r 3 \r\n", "3"),
    ("3 4", "NaN"),
    ("--1", "NaN"),
    (".", "NaN"),
    ("1..2", "NaN"),
]
JS_STRING = [
    (1e-06, "0.000001"),
    (1e-07, "1e-7"),
    (1.5e-07, "1.5e-7"),
    (1e-07, "1e-7"),
    (1e-06, "0.000001"),
    (1.234e-06, "0.000001234"),
    (123456789012345680000, "123456789012345680000"),
    (1e21, "1e+21"),
    (1.5e21, "1.5e+21"),
    (100000000000000000000, "100000000000000000000"),
    (0.30000000000000004, "0.30000000000000004"),
    (100, "100"),
    (1e300, "1e+300"),
    (-1e-07, "-1e-7"),
    (5e-324, "5e-324"),
    (9007199254740992, "9007199254740992"),
    (123.456, "123.456"),
    (0.3333333333333333, "0.3333333333333333"),
    (0, "0"),
    (10000000000000000, "10000000000000000"),
    (12345678901234567000, "12345678901234567000"),
    (0.5, "0.5"),
    (-123.5, "-123.5"),
    (1.7976931348623157e308, "1.7976931348623157e+308"),
    (4.35, "4.35"),
    (0.000123, "0.000123"),
    (1e-05, "0.00001"),
    (999999999999999900000, "999999999999999900000"),
]
JS_LENGTH = [
    ("\U0001f600", 2),
    ("a\U0001f600b", 4),
    ("\u4e00\u4e01", 2),
    ("", 0),
    ("\U00020bb7\U00020bb7", 4),
]
JS_LESS = [
    ("\U0001f600", "\uff21", True),
    ("\U0001f600", "\uffff", True),
    ("\U0001f600", "\ud7ff", False),
    ("a", "b", True),
    ("\ue000", "\U0001f600", False),
    ("\U00020bb7", "\U0001f600", False),
    ("abc", "ab", False),
]


class TestJsSemantics:
    """求值器与 JS 一致：`Number()`、`String()`、`.length`、字符串比大小；常量条件不能把诱饵带进来"""

    @pytest.mark.parametrize(("text", "expected"), JS_NUMBER)
    def test_string_to_number(self, text: str, expected: str) -> None:
        value = template_lite._to_number(text)
        if expected == "-0":
            assert value == 0 and math.copysign(1, value) < 0
        else:
            assert template_lite._number_to_string(value) == expected

    @pytest.mark.parametrize(("number", "expected"), JS_STRING)
    def test_number_to_string(self, number: float, expected: str) -> None:
        assert template_lite._number_to_string(number) == expected
        # 模板里写数字字面量也一样（子集里没有负号，负数只比函数本身）
        if number >= 0:
            assert run(f"<%= {number!r} %>") == expected

    @pytest.mark.parametrize(("text", "expected"), JS_LENGTH)
    def test_length_counts_utf16_code_units(self, text: str, expected: int) -> None:
        assert template_lite._js_length(text) == expected
        assert run("<%= getvar('s').length %>", cache={"s": text}) == str(expected)

    @pytest.mark.parametrize(("left", "right", "expected"), JS_LESS)
    def test_strings_compare_by_utf16_code_units(self, left: str, right: str, expected: bool) -> None:
        assert template_lite._compare("<", left, right) is expected

    @pytest.mark.parametrize(
        "condition",
        [
            "'\\uD83D\\uDE00'.length < 2",
            "'\\uD83D\\uDE00' > '\\uFF21'",
            "!('0x10' == 16)",
            "'1_0' == 10",
            "'\\uFF11' == 1",
            "!('\\uFEFF1' == 1)",
            "String(0.000001) == '1e-6'",
            "!('0b11' == 3)",
            "'\\u0661' == 1",
        ],
    )
    def test_constant_conditions_that_are_false_in_js(self, condition: str) -> None:
        assert run(f"<% if ({condition}) {{ %>诱饵<% }} %>ok") == "ok"

    @pytest.mark.parametrize(
        "source",
        [
            "<%= 010 %>",
            "<%= 09 %>",
            "<%= 00 %>",
            "<%= 1_0 %>",
            "<%= 0x10 %>",
            "<%= \u0661 %>",
            "<% if (" + chr(0x1C) + "true) { %>x<% } %>",
        ],
    )
    def test_numbers_and_whitespace_outside_js_are_unsupported(self, source: str) -> None:
        with pytest.raises(TemplateUnsupported):
            compile_template(source)

    def test_numeric_literals_that_js_accepts(self) -> None:
        assert run("<%= 0 %>|<%= 0.5 %>|<%= .5 %>|<%= 1e3 %>|<%= 0e5 %>") == "0|0.5|0.5|1000|0"

    def test_line_comments_end_at_any_js_line_terminator(self) -> None:
        # JS 的 `//` 注释到 \r、U+2028、U+2029 为止，后面的代码照常执行
        for terminator in ("\r", chr(0x2028), chr(0x2029)):
            assert run(f"<% // 注释{terminator}if (true) {{ %>有<% }} %>") == "有"

    def test_big_integers_in_variables_do_not_overflow(self) -> None:
        assert run("<%= getvar('n') %>|<%= getvar('n') > 1 %>", cache={"n": 10**400}) == "Infinity|true"


_LONG = "长" * 1200


class TestJsFailClosed:
    """与 JS 对不上、又不好照搬的写法判不支持或按出错算：转义、原型上的名字、不带括号的方法引用"""

    @pytest.mark.parametrize(
        "escape",
        ["x41", "101", "1", "7", "8", "9", "00", "01", "\n", "\r", chr(0x2028), chr(0x2029)],
        ids=["hex", "octal", "one", "seven", "eight", "nine", "zero-zero", "zero-one", "lf", "cr", "ls", "ps"],
    )
    def test_escapes_that_differ_from_js_are_unsupported(self, escape: str) -> None:
        with pytest.raises(TemplateUnsupported):
            compile_template("<%= 'a" + BACKSLASH + escape + "' %>")

    @pytest.mark.parametrize(
        ("escape", "code"),
        [("0", 0), ("n", 10), ("t", 9), ("'", 39), ('"', 34), (BACKSLASH, 92), ("u0041", 65), ("q", 113)],
    )
    def test_escapes_that_match_js_still_work(self, escape: str, code: int) -> None:
        assert run("<%= 'a" + BACKSLASH + escape + "' === 'a" + _js_char(code) + "' %>") == "true"

    @pytest.mark.parametrize(
        "source",
        [
            "<% if ('" + BACKSLASH + "x41' === 'A') { %>短<% } else { %>" + _LONG + "<% } %>",
            "<% if ('" + BACKSLASH + "101' === 'A') { %>短<% } else { %>" + _LONG + "<% } %>",
            "<% if ('x'.constructor) { %>短<% } else { %>" + _LONG + "<% } %>",
            "<% if (!'x'.toLowerCase) { %>" + _LONG + "<% } %>",
            "<% if (!'x'.includes) { %>" + _LONG + "<% } %>",
            "<% if (!1..constructor) { %>" + _LONG + "<% } %>",
            "<% if (!true.constructor) { %>" + _LONG + "<% } %>",
        ],
    )
    def test_constant_conditions_true_in_js_are_unsupported(self, source: str) -> None:
        with pytest.raises(TemplateUnsupported):
            compile_template(source)

    @pytest.mark.parametrize(
        "source",
        [
            "<%= getvar('m').constructor %>",
            "<%= getvar('m').__proto__ %>",
            "<%= getvar('m').hasOwnProperty %>",
            "<%= getvar('m').toString %>",
            "<%= getvar('m').valueOf %>",
            "<%= getvar('m').a.isPrototypeOf %>",
            "<%= getvar('s').includes %>",
            "<%= getvar('s').toLowerCase %>",
            "<%= getvar('m.toString') %>",
            "<%= getvar('constructor') %>",
            "<%= getLocalVar('a.__proto__.b') %>",
        ],
    )
    def test_prototype_names_and_method_references_are_unsupported(self, source: str) -> None:
        with pytest.raises(TemplateUnsupported):
            compile_template(source)

    @pytest.mark.parametrize(
        ("source", "cache"),
        [
            ("<%= 'x'.trim %>", None),
            ("<% if ('x'.at) { %>短<% } else { %>" + _LONG + "<% } %>", None),
            ("<%= (1).toFixed %>", None),
            ("<%= true.x %>", None),
            ("<%= getvar('s').at %>", {"s": "abc"}),
            ("<%= getvar('n').x %>", {"n": 3}),
            ("<%= getvar('l').map %>", {"l": [1, 2]}),
            ("<%= String(getvar('m')).never %>", {"m": {"a": 1}}),
        ],
    )
    def test_other_properties_of_primitives_and_arrays_raise(self, source: str, cache: dict | None) -> None:
        with pytest.raises(TemplateRuntimeError):
            run(source, cache=cache)

    def test_length_and_mapping_fields_still_work(self) -> None:
        cache = {"s": "abc", "l": [1, 2], "m": {"a": "甲", "length": 7}, "n": 3}
        source = (
            "<%= getvar('s').length %>|<%= getvar('l').length %>|<%= getvar('m').a %>|<%= getvar('m').length %>"
            "|<%= getvar('m').zz === undefined %>|<%= getvar('n').length === undefined %>|<%= 'ab'.length %>"
        )
        assert run(source, cache=cache) == "3|2|甲|7|true|true|2"


def _js_char(code: int) -> str:
    """模板里写一个字符的 JS 转义（`'\\uXXXX'`），用例里不直接写那个字"""
    return BACKSLASH + "u" + format(code, "04x")


class TestPathPartialWorlds:
    """「只有一部分变量」按完整路径拿掉：同一个根变量下的字段配合也判得出"""

    @pytest.mark.parametrize(
        "condition",
        [
            "!getvar('stat_data.a') || getvar('stat_data.never')",
            "!getvar('s.a.b') || getvar('s.a.c')",
            "!getvar('s.a') || getvar('s').never",
            "!getvar('s') || !getvar('s').a || getvar('s').never",
            "!getvar('s.a') || getvar('s.a.never')",
            "!getvar('s.a') || getvar('s').toLowerCase().never",
        ],
    )
    def test_same_root_fields_are_caught(self, condition: str) -> None:
        assert _bait_with_seeds(condition, seeds=5) == {2000}

    @pytest.mark.parametrize(
        "condition",
        [
            "!getvar('stat_data.a') || (getvar('stat_data.a') || 0).never",
            "!getvar('stat_data.a') || (getvar('stat_data') || 0).never",
            "!getvar('s.a') || String(getvar('s.a') || '').never",
            "!getvar('s.a') || (getvar('s.a') && getvar('s.a')).never",
        ],
    )
    def test_fields_through_logical_operators_are_caught(self, condition: str) -> None:
        assert _bait_with_seeds(condition, seeds=40) == {2000}

    def test_logical_paths_are_capped(self) -> None:
        operands = " || ".join(f"getvar('v{index}')" for index in range(3000))
        template = compile_template(f"<%= ({operands})" + ".a" * 50 + " %>")
        paths = template_lite._variable_paths(template)
        roots = [("cache", f"v{index}") for index in range(3000)]
        cap = template_lite.MAX_LOGICAL_PATHS
        # 每个并集最多带出 MAX_LOGICAL_PATHS 条，接在后面的取属性到 MAX_UNIT_DEPTH 段为止
        deeper = [
            ("cache", f"v{index}" + ".a" * extra)
            for extra in range(1, template_lite.MAX_UNIT_DEPTH)
            for index in range(cap)
        ]
        assert paths == roots + deeper

    def test_same_root_fields_in_an_output_tag(self) -> None:
        source = "<%= (!getvar('stat_data.hp') || getvar('stat_data.zz')) && '" + "B" * 1500 + "' %>ok"
        assert template_lite.compare_renders(compile_template(source), seed="x").bait_chars == 1500

    def test_many_roots_with_same_root_fields(self) -> None:
        decoys = "".join(f"<%= getvar('r{index}') %>" for index in range(12))
        source = decoys + f"<% if (!getvar('m.a') || getvar('m.never')) {{ %>{'B' * 2000}<% }} %>ok"
        assert template_lite.compare_renders(compile_template(source), seed="x").bait_chars == 2000

    def test_unit_tree_closes_paths_under_prefixes(self) -> None:
        template = compile_template(
            "<%= getvar('s.a.b') %><%= getvar('s').c %><%= getLocalVar('s') %><%= getvar('t.u.v.w.x.y') %>"
        )
        units, info = template_lite._unit_tree(template)
        assert units == {
            ("cache", "s"): 0,
            ("cache", "s.a"): 1,
            ("cache", "s.a.b"): 2,
            ("cache", "s.c"): 3,
            ("local", "s"): 4,
            ("cache", "t"): 5,
            ("cache", "t.u"): 6,
            ("cache", "t.u.v"): 7,
            ("cache", "t.u.v.w"): 8,
        }
        # (层数, 同一个父节点下的序号)；两种作用域的根变量算同一组兄弟；路径最深看 4 层
        assert info == [(1, 0), (2, 0), (3, 0), (2, 1), (1, 1), (1, 2), (2, 0), (3, 0), (4, 0)]

    def test_variable_paths_follow_property_access(self) -> None:
        template = compile_template(
            "<%= String(getvar('s')).a %><%= getvar('s').toLowerCase().b %><%= getvar('s').length %>"
            "<%= (getvar('p') || getvar('q')).c %><%= getvar('s').includes(getvar('k').d) %>"
        )
        assert template_lite._variable_paths(template) == [
            ("cache", "s"),
            ("cache", "s.a"),
            ("cache", "s.b"),
            ("cache", "p"),
            ("cache", "q"),
            ("cache", "p.c"),
            ("cache", "q.c"),
            ("cache", "k"),
            ("cache", "k.d"),
        ]

    def test_partial_renders_are_capped(self) -> None:
        # 又宽又深的路径树：三层各一百个兄弟（每层 7 位、拿掉 14 次），拿掉的次数封顶
        fields = "".join(
            f"<%= getvar('{path}') %>"
            for index in range(100)
            for path in (f"s.f{index}", f"s.f0.g{index}", f"s.f0.g0.h{index}")
        )
        source = fields + "<% if (getvar('s') || true) { %>" + "正" * 1200 + "<% } %>"
        comparison = template_lite.compare_renders(compile_template(source), seed="x")
        assert comparison.bait_chars == 0
        assert comparison.partial_renders == template_lite.MAX_PARTIAL_RENDERS

    @pytest.mark.parametrize(
        "condition",
        [
            "getvar('战斗中') && !getvar('战斗结束')",
            "getvar('角色') && !getvar('角色.死亡')",
            "getvar('s.战斗中') && !getvar('s.战斗结束')",
        ],
    )
    def test_one_present_one_missing_is_treated_as_bait(self, condition: str) -> None:
        # 有意的取舍：「一个有、另一个没有」才不显示的一大段，与诱饵分不开，按诱饵处理
        source = f"<% if ({condition}) {{ %>短<% }} else {{ %>{'正' * 1500}<% }} %>"
        assert template_lite.compare_renders(compile_template(source), seed="x").bait_chars == 1500


class TestPlaceholderExtremes:
    """两组固定的哨兵贴着 UTF-16 码元的两头；混合那组的开头从几段区间里取"""

    @pytest.mark.parametrize(
        ("op", "code"),
        [("<", 0xFF00), ("<", 0x9000), ("<", 0x7B), (">", 0x5A), (">=", 0x0002), ("<=", 0xFFFE), ("<", 0xAC00)],
    )
    def test_one_lexicographic_threshold_is_caught_with_any_key(self, op: str, code: int) -> None:
        assert _bait_with_seeds(f"!getvar('s') || getvar('s') {op} '{_js_char(code)}'") == {2000}

    def test_fixed_sets_sit_at_both_ends(self) -> None:
        short = template_lite._SentinelVariables(template_lite._short_low).get("cache", "s")
        long = template_lite._SentinelVariables(template_lite._long_high).get("cache", "s")
        assert short.startswith(chr(1) * 4) and long.startswith(chr(0xFFFF) * 20)
        assert math.isnan(template_lite._to_number(short)) and math.isnan(template_lite._to_number(long))

    def test_mixed_heads_come_from_every_range(self) -> None:
        seen = set()
        for seed in range(30):
            variables = template_lite._SentinelVariables(template_lite._mixed_maker(str(seed)))
            for index in range(40):
                value = variables.get("cache", f"v{index}")
                if len(value) > 1:
                    head = ord(value[0])
                    seen.update(
                        number
                        for number, (low, high) in enumerate(template_lite._MIXED_HEAD_RANGES)
                        if low <= head <= high
                    )
        assert seen == set(range(len(template_lite._MIXED_HEAD_RANGES)))


class TestPersistentSentinelKey:
    """随机那两组哨兵由调用方给的密钥定：重启前后（进程密钥变了）结论不变"""

    def test_given_key_is_independent_of_the_process_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = b"k" * 32
        before = [template_lite._mixed_maker("正文", key)(index) for index in range(5)]
        before_lower = template_lite._seeded_maker("正文", key)(0)
        process_before = template_lite._mixed_maker("正文")(0)
        monkeypatch.setattr(template_lite, "_SENTINEL_SEED_KEY", b"x" * 32)
        assert [template_lite._mixed_maker("正文", key)(index) for index in range(5)] == before
        assert template_lite._seeded_maker("正文", key)(0) == before_lower
        assert template_lite._mixed_maker("正文")(0) != process_before

    def test_verdict_depends_on_the_key_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 只在随机那两组身上才可能不同的写法：给了同一把密钥，换了进程密钥结论也一样
        template = compile_template("<% if (!getvar('s') || getvar('s').length != 13) { %>" + "B" * 2000 + "<% } %>ok")
        verdicts = set()
        for process_key in (b"a" * 32, b"b" * 32, b"c" * 32):
            monkeypatch.setattr(template_lite, "_SENTINEL_SEED_KEY", process_key)
            verdicts.add(template_lite.compare_renders(template, seed="正文", key=b"k" * 32).bait_chars)
        assert len(verdicts) == 1
