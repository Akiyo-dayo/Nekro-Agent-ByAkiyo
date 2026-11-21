"""酒馆卡里给网页看的东西在 QQ 里怎么处理

- 清理规则的三种处理方式：按替换文字 / 只留正文 / 整段删掉，以及保存校验按方式取上限；
- 只是给 HTML 规则用的标记的开场白：判据与挑开场白时跳过；
- 发送前把展示用的 HTML 标签转成纯文字；
- 导出时 NA 里对 HTML 规则的处理不改写酒馆脚本的启用状态；
- 网页代码四类、不支持的条目功能计数、「不生效清单」；
- 预算体检（直接调 `select_hits`）与触发测试的「前面占预算的条目」。

样例文字都是这里现编的。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from nekro_agent.routers.worldbook import _ahead_of
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.tavern import cleanup, exporter, greeting, importer
from nekro_agent.services.tavern.html_text import display_html_to_text, has_display_html
from nekro_agent.services.worldbook import budget_check, selection, unsupported
from nekro_agent.services.worldbook.matcher import HitReason
from nekro_agent.services.worldbook.types import book_to_match, entry_to_match

WATCH_FIND = r"/<watch>([\s\S]*?)<\/watch>/g"
PIC_FIND = r"/<pic>([^<]{1,80})<\/pic>/g"

# 「线性」类用例按本线程 CPU 时间算：全量跑、机器忙时墙钟会被拉长，CPU 时间不会；
# 真退化成二次方时这些输入要跑几分钟，照样超限。
_cpu_seconds = time.thread_time


def _rule(find: str, replace: str, mode: str = cleanup.OUTPUT_REPLACE, **extra: Any) -> dict[str, Any]:
    rule = {
        "name": "规则",
        "find": find,
        "replace": replace,
        "trim": [],
        "enabled": True,
        "disabled_reason": "",
        "original": None,
        "output_mode": mode,
    }
    rule.update(extra)
    return rule


def _apply(text: str, rules: list[dict[str, Any]]) -> str:
    return cleanup.apply_compiled(text, cleanup.compile_rules(rules), expand=lambda value: value).text


class TestOutputModes:
    def test_text_only_keeps_referenced_groups_once(self) -> None:
        rule = _rule(WATCH_FIND, '<div class="a">注视中$1</div><span title="$1">$1</span>', cleanup.OUTPUT_TEXT)

        assert _apply("甲<watch> 钟声 </watch>乙<watch>风</watch>", [rule]) == "甲钟声乙风"

    def test_text_only_joins_several_groups_and_skips_empty_ones(self) -> None:
        find = r"/<card>(?<who>[^|<]{1,6})\|([^|<]{0,6})\|([^<]{1,8})<\/card>/"
        rule = _rule(find, "<b>$<who></b><i>$3</i><u>$2</u>{{match}}", cleanup.OUTPUT_TEXT, trim=["#"])

        result = _apply("<card>阿#青||星期三</card>", [rule])

        # 分组按替换文字里出现的顺序取：名字、第 3 组、第 2 组（空，跳过）；同时引用了具体分组，`{{match}}`
        # 就不要（整个匹配里本来就含着它们）；去除串照常作用
        assert result.split("\n") == ["阿青", "星期三"]

    def test_text_only_does_not_expand_macros(self) -> None:
        rules = cleanup.compile_rules([_rule(WATCH_FIND, "<b>{{user}}$1</b>", cleanup.OUTPUT_TEXT)])

        result = cleanup.apply_compiled(
            "<watch>你好 {{user}}</watch>", rules, expand=lambda value: value.replace("{{user}}", "阿青")
        )

        assert result.text == "你好 {{user}}"

    def test_text_only_with_match_and_groups_is_not_doubled(self) -> None:
        rule = _rule(WATCH_FIND, "<b>{{match}}</b><i>$1</i>", cleanup.OUTPUT_TEXT)

        assert _apply("<watch>钟声</watch>", [rule]) == "钟声"

    def test_groups_are_computed_once_at_compile_time(self) -> None:
        named = r"/<w>(?<who>[^<]{1,9})<\/w>/g"
        compiled = cleanup.compile_rules(
            [
                _rule(WATCH_FIND, "<b>$1</b>{{match}}$<x>$9$" + "9" * 5000, cleanup.OUTPUT_TEXT),
                _rule(named, "<b>$<who></b>$<nope>$1$2", cleanup.OUTPUT_TEXT),
            ],
        )

        # 只留正则里真有的分组：没有的命名分组、超出分组数的编号、位数离谱的编号都不要
        assert compiled[0].groups == (1,)
        assert compiled[1].groups == ("who", 1)

    def test_same_value_in_different_groups_is_kept(self) -> None:
        rule = _rule(r"/<hp>(\d{1,3})\/(\d{1,3})<\/hp>/", "HP $1 / $2 <b>$1</b>", cleanup.OUTPUT_TEXT)

        assert _apply("<hp>10/10</hp>", [rule]) == "10\n10"

    def test_thousands_of_group_references_stay_cheap(self) -> None:
        replace = "<div>" + "".join(f"${index}" for index in range(12_000))
        rule = _rule("/a/g", replace, cleanup.OUTPUT_TEXT)
        assert cleanup.validate_rule(rule) is None

        started = _cpu_seconds()
        compiled = cleanup.compile_rules([rule] * 50)
        result = cleanup.apply_compiled("a" * 1000, compiled[:1], expand=lambda value: value)
        cleanup.placeholder_rule("a" * 60, cleanup.placeholder_rules([rule] * 50))
        assert _cpu_seconds() - started < 2
        # 正则没有分组，只剩整个匹配（$0）
        assert compiled[0].groups == (0,)
        assert result.text == "a" * 1000

    def test_unclosed_named_group_references_are_linear(self) -> None:
        replace = "<div>" + "$<" * 31_990
        started = _cpu_seconds()
        assert cleanup.has_html_output(replace)
        rule = _rule(WATCH_FIND, replace, cleanup.OUTPUT_TEXT)
        assert cleanup.validate_rule(rule) is None
        compiled = cleanup.compile_rules([rule])
        for _ in range(20):
            cleanup.apply_compiled("没有命中的消息", compiled, expand=lambda value: value)
        cleanup.html_kind_of(rule)
        cleanup.placeholder_rule("[开场]", cleanup.placeholder_rules([rule] * 5))
        assert _cpu_seconds() - started < 2

    def test_text_only_without_groups_removes_the_match(self) -> None:
        assert _apply("前<watch>x</watch>后", [_rule(WATCH_FIND, "<hr>", cleanup.OUTPUT_TEXT)]) == "前后"

    def test_drop_removes_the_whole_match(self) -> None:
        rule = _rule(PIC_FIND, '<img src="https://img.example.test/$1.png">', cleanup.OUTPUT_DROP)

        assert _apply("前<pic>角色/日常</pic>后", [rule]) == "前后"

    def test_unknown_or_missing_mode_reads_as_replace(self) -> None:
        legacy = _rule(WATCH_FIND, "[$1]")
        del legacy["output_mode"]
        odd = _rule(WATCH_FIND, "[$1]", "whatever")

        assert _apply("<watch>a</watch>", [legacy]) == "[a]"
        assert _apply("<watch>a</watch>", [odd]) == "[a]"

    def test_validation_limits_follow_the_mode(self) -> None:
        long_html = "<div>" + "字" * (cleanup.MAX_REPLACE_CHARS + 10) + "</div>"

        assert cleanup.validate_rule(_rule(WATCH_FIND, long_html, cleanup.OUTPUT_REPLACE)) is not None
        assert cleanup.validate_rule(_rule(WATCH_FIND, long_html, cleanup.OUTPUT_TEXT)) is None
        assert cleanup.validate_rule(_rule(WATCH_FIND, long_html, cleanup.OUTPUT_DROP)) is None
        too_long = "<div>" + "字" * (cleanup.MAX_DISABLED_RULE_TEXT_CHARS + 1) + "</div>"
        assert cleanup.validate_rule(_rule(WATCH_FIND, too_long, cleanup.OUTPUT_TEXT)) is not None

    def test_bad_mode_is_rejected_on_save(self) -> None:
        bad = _rule(WATCH_FIND, "x", "render")

        assert "处理方式" in (cleanup.validate_rule(bad) or "")
        assert "处理方式" in (cleanup.validate_disabled_rule_shape({**bad, "enabled": False}) or "")

    def test_html_kind_of(self) -> None:
        assert cleanup.html_kind_of(_rule(WATCH_FIND, "【$1】")) == ""
        assert cleanup.html_kind_of(_rule(WATCH_FIND, "<b>$1</b>")) == unsupported.CODE_DISPLAY
        assert cleanup.html_kind_of(_rule(PIC_FIND, '<img src="https://a.test/$1">')) == unsupported.CODE_EXTERNAL


class TestPlaceholderGreeting:
    HTML_RULE = _rule(r"/\[开场界面\]/", "<html><body><script>start()</script></body></html>", cleanup.OUTPUT_TEXT)

    @staticmethod
    def _check(text: str, rules: list[dict[str, Any]]) -> str | None:
        return cleanup.placeholder_rule(text, cleanup.placeholder_rules(rules))

    def test_marker_fully_matched_by_an_html_rule(self) -> None:
        assert self._check("  [开场界面]\n", [self.HTML_RULE]) == "规则"

    def test_disabled_html_rule_still_counts(self) -> None:
        legacy = {**self.HTML_RULE, "enabled": False, "disabled_reason": cleanup.HTML_OUTPUT, "name": ""}

        assert self._check("[开场界面]", [legacy]) == "第 1 条"

    def test_not_a_marker(self) -> None:
        plain = _rule(r"/\[开场界面\]/", "很长的一段开场白")

        # 规则不输出 HTML、只吃掉一部分、开场白太长：都不算标记
        assert self._check("[开场界面]", [plain]) is None
        assert self._check("[开场界面] 你好", [self.HTML_RULE]) is None
        assert self._check("[开场界面]" + "啊" * 80, [self.HTML_RULE]) is None

    def test_rules_not_applied_on_display_do_not_count(self) -> None:
        # 酒馆显示开场白时不套用：只作用于用户输入、卡里关着、只改提示词
        for reason in (cleanup.NOT_AI_OUTPUT, cleanup.DISABLED_IN_CARD, cleanup.PROMPT_ONLY):
            rule = {**self.HTML_RULE, "enabled": False, "disabled_reason": reason}
            assert self._check("[开场界面]", [rule]) is None, reason

    def test_whole_message_beautifier_is_not_a_marker(self) -> None:
        # 整段美化规则能吃掉任何开场白，但正文就在 $1 里：不是标记，短开场白照发
        beautify = _rule(r"/^([\s\S]*)$/", '<div class="msg">$1</div>', cleanup.OUTPUT_TEXT)

        assert self._check("你好呀，欢迎回来。", [beautify]) is None
        assert self._check("[开场界面]", [beautify, self.HTML_RULE]) == "规则"

    def test_marker_check_shares_a_deadline(self) -> None:
        prepared = cleanup.placeholder_rules([self.HTML_RULE])
        ticks = iter([0.0, 10.0])

        assert cleanup.placeholder_rule("[开场界面]", prepared, deadline=5.0, clock=lambda: next(ticks)) == "规则"
        assert cleanup.placeholder_rule("[开场界面]", prepared, deadline=5.0, clock=lambda: 10.0) is None

    def test_pick_greeting_skips_markers(self) -> None:
        channel = SimpleNamespace(chat_type=ChatType.GROUP)
        card = SimpleNamespace(
            greetings=["[开场界面]", "真正的开场白"],
            group_greetings=["[开场界面]"],
            greeting_pick="first",
            cleanup_rules=[self.HTML_RULE],
        )

        # 群聊专用的只有标记：回落到普通开场白，并跳过其中的标记
        assert greeting.pick_greeting(channel, card) == "真正的开场白"  # type: ignore[arg-type]
        card.greetings = ["[开场界面]"]
        assert greeting.pick_greeting(channel, card) is None  # type: ignore[arg-type]

class TestDisplayHtmlToText:
    def test_inline_and_block_tags(self) -> None:
        text = '<div><font color="red">红字</font>与<b>粗体</b></div><p>第二段<br/>换行</p><details><summary>折叠</summary>内容</details>'

        assert display_html_to_text(text) == "红字与粗体\n\n第二段\n换行\n\n折叠\n内容"

    def test_style_script_comment_and_entities(self) -> None:
        text = "<style>.a{color:red}</style>前&lt;后&nbsp;尾<!-- 注释 --><script>alert(1)</script><span>字</span>"

        assert display_html_to_text(text) == "前<后 尾字"

    def test_unknown_tags_and_plain_text_are_left_alone(self) -> None:
        text = "<pic>角色/日常</pic><options>1. 走</options>"
        plain = "a < b，<Update> 不是展示标签"

        assert not has_display_html(plain)
        assert display_html_to_text(plain) is plain
        assert display_html_to_text(f"{text}<br>") == text

    def test_unclosed_blocks_are_kept(self) -> None:
        for text in ("正文<style>.x{", "正文<!-- 没有结尾", "看看 <script> 标签是干嘛的，没有闭合"):
            assert display_html_to_text(text) == text

    def test_ordinary_text_with_angle_brackets_is_kept(self) -> None:
        for text in (
            "if a<b and c>d then",
            "x<a && y>b",
            "1<b>2",
            "表情 <a:wave:123> 与 <:smile:456>",
            "for(i=0;i<n;i++){ if (a<b) c>0 }",
            "vector<string> v; Map<K, V> m;",
            "表情 (>_<) <(_ _)> <3 <_<",
            "代码 `<div>` 示例和 `a<b>c`",
        ):
            assert display_html_to_text(text) == text, text

    def test_only_six_entities_with_semicolons(self) -> None:
        text = "AT&T 的 &copy 与 &para=1、&copy; &lt;b&gt; &amp;amp; &#39;引号&#39;<br>下一行"

        assert display_html_to_text(text) == "AT&T 的 &copy 与 &para=1、&copy; <b> &amp; '引号'\n下一行"

    def test_unpaired_tags_stay_but_void_tags_go(self) -> None:
        assert display_html_to_text("开<b>粗<br>体") == "开<b>粗\n体"
        assert display_html_to_text("<span>a<span>b</span>") == "<span>ab"

    def test_attributes_follow_html_syntax(self) -> None:
        assert display_html_to_text("<details open><summary>标题</summary>内容</details>") == "标题\n内容"
        assert display_html_to_text("<font color=red size='3'>字</font>") == "字"
        assert display_html_to_text("<b and c>字</b>") == "<b and c>字</b>"

    def test_non_ascii_lookalikes_never_match(self) -> None:
        # U+017F ſ 与 U+212A 开尔文符号在 Unicode 忽略大小写下会等于 s / k；按 ASCII 匹配就不会
        for text in (
            "<\u017fcript>x</script> 正文",
            "<\u017ftyle>x</style>",
            "<\u212abd>x</kbd>",
            "<\u017fpan>x</span>",
        ):
            assert display_html_to_text(text) == text

    def test_pathological_input_is_linear(self) -> None:
        started = _cpu_seconds()
        display_html_to_text("<style x" * 100_000)
        display_html_to_text("<!--" + "<style>" * 50_000)
        display_html_to_text("<font " + "a" * 200_000)
        display_html_to_text("<b>" * 100_000)
        display_html_to_text("<b>x</b>" + " " * 200_000 + "x")
        display_html_to_text("<b>x</b>" + "\t" * 200_000 + "x")
        display_html_to_text(("<script>a" + "<!--b") * 30_000)
        display_html_to_text("<style>" * 30_000 + "<!--" * 30_000)
        display_html_to_text("<div>" * 40_000 + "x" + "</div>" * 40_000)
        display_html_to_text("<b " + 'a="1" ' * 50_000 + ">")
        assert _cpu_seconds() - started < 3


class TestExportKeepsHtmlRulesAsInCard:
    SCRIPT = {
        "scriptName": "状态栏",
        "findRegex": WATCH_FIND,
        "replaceString": "<div>$1</div>",
        "trimStrings": [],
        "placement": [2],
        "disabled": False,
        "markdownOnly": True,
    }

    def test_text_only_or_disabled_html_rule_exports_unchanged(self) -> None:
        imported = cleanup.rule_from_st_script(self.SCRIPT)
        assert imported["output_mode"] == cleanup.OUTPUT_TEXT

        for rule in (
            imported,
            {**imported, "output_mode": cleanup.OUTPUT_DROP},
            {**imported, "enabled": False, "disabled_reason": cleanup.HTML_OUTPUT},
            {**imported, "enabled": False, "disabled_reason": ""},
        ):
            script = exporter._regex_script_from_rule(rule)  # noqa: SLF001
            assert script["disabled"] is False

    def test_html_rule_disabled_in_card_keeps_a_default_mode(self) -> None:
        script = {**self.SCRIPT, "disabled": True}
        imported = cleanup.rule_from_st_script(script)

        assert imported["disabled_reason"] == cleanup.DISABLED_IN_CARD
        assert imported["output_mode"] == cleanup.OUTPUT_TEXT
        turned_on = {**imported, "enabled": True, "disabled_reason": ""}
        assert exporter._regex_script_from_rule(turned_on)["disabled"] is True  # noqa: SLF001

    def test_plain_rule_disable_is_still_exported(self) -> None:
        plain = {**self.SCRIPT, "replaceString": "【$1】"}
        rule = {**cleanup.rule_from_st_script(plain), "enabled": False}

        assert exporter._regex_script_from_rule(rule)["disabled"] is True  # noqa: SLF001


class TestCodeKinds:
    def test_four_kinds(self) -> None:
        assert unsupported.classify_code("<div style='color:red'>$1</div>") == unsupported.CODE_DISPLAY
        assert (
            unsupported.classify_code('<div style="background:url(https://a.test/x.png)">') == unsupported.CODE_EXTERNAL
        )
        assert unsupported.classify_code("<style>@import 'x.css';</style>") == unsupported.CODE_EXTERNAL
        assert unsupported.classify_code('<button onclick="go()">走</button>') == unsupported.CODE_INTERACTIVE
        assert unsupported.classify_code("<script>let a = 1</script>") == unsupported.CODE_INTERACTIVE
        assert unsupported.classify_code('<script type="module">import {a} from "https://cdn.test/a.js"</script>') == (
            unsupported.CODE_REMOTE
        )
        assert unsupported.classify_code('<script src="//cdn.test/a.js"></script>') == unsupported.CODE_REMOTE

    def test_scripts_are_at_least_interactive(self) -> None:
        assert unsupported.classify_code("console.log(1)", script=True) == unsupported.CODE_INTERACTIVE
        loader = "const s = document.createElement('script'); s.textContent = await (await fetch(u)).text();"
        assert unsupported.classify_code(loader, script=True) == unsupported.CODE_REMOTE
        assert (
            unsupported.classify_code("await replaceWorldbook(name, entries)", script=True) == unsupported.CODE_REMOTE
        )

    def test_classification_is_linear(self) -> None:
        started = _cpu_seconds()
        unsupported.classify_code("import " + " " * 200_000)
        unsupported.classify_code("<script " + "a" * 200_000)
        unsupported.classify_code(" on" * 100_000)
        assert _cpu_seconds() - started < 3

    def test_html_rules_note(self) -> None:
        rules = [
            cleanup.rule_from_st_script({"findRegex": WATCH_FIND, "replaceString": "<b>$1</b>", "placement": [2]}),
            cleanup.rule_from_st_script(
                {"findRegex": PIC_FIND, "replaceString": '<img src="https://a.test/$1">', "placement": [2]}
            ),
            cleanup.rule_from_st_script({"findRegex": WATCH_FIND, "replaceString": "【$1】", "placement": [2]}),
        ]

        note = importer._html_rules_note(rules)  # noqa: SLF001

        assert note is not None and note.kind == "html_rules"
        assert "2 条输出 HTML 的清理规则（纯展示 1 条、依赖外部资源 1 条）" in note.detail
        assert "1 条「只留正文」、1 条带图片的「整段删掉」" in note.detail
        assert importer._html_rules_note(rules[2:]) is None  # noqa: SLF001


class TestUnsupportedFeatures:
    def test_world_file_entries(self) -> None:
        entries = [
            {"content": "@@activate\n正文"},
            {"content": "a", "group": "天气", "useProbability": True, "probability": 30},
            {"content": "b", "useProbability": False, "probability": 10, "delayUntilRecursion": 2},
            {"content": "c", "characterFilter": {"isExclude": False, "names": ["甲"], "tags": []}, "vectorized": True},
            {"content": "d", "triggers": ["continue"], "matchPersonaDescription": True, "automationId": "x"},
            {"content": "e", "group": "  ", "characterFilterNames": [], "triggers": []},
        ]

        found = {use.feature.key: use.count for use in unsupported.feature_uses(entries)}

        assert found == {
            "decorator": 1,
            "group": 1,
            "probability": 1,
            "recursion": 1,
            "character_filter": 1,
            # `vectorized` 本轮起是支持的功能（语义触发），不再进「不支持」清单。
            "triggers": 1,
            "extra_sources": 1,
            "automation": 1,
        }

    def test_card_book_entries_read_extensions(self) -> None:
        entries = [
            {"content": "a", "extensions": {"group": "天气", "probability": 50, "delay_until_recursion": True}},
            {"content": "b", "extensions": {"character_filter": {"names": [], "tags": ["标签"]}}},
            {"content": "c", "extensions": "坏数据"},
        ]

        keys = [use.feature.key for use in unsupported.feature_uses(entries)]

        assert keys == ["recursion", "group", "probability", "character_filter"]

    def test_unsupported_items_for_a_world_file(self) -> None:
        entries = [{"content": "<% if (getvar('x')) { %>甲<% } %>", "group": "g"}, {"content": "无模板"}]

        items = unsupported.unsupported_items(entries=entries)

        assert [item.field for item in items] == ["ejs", "entries"]
        assert "2 个 EJS 模板块" in items[0].detail and "在 1 条条目里" in items[0].detail
        assert "包含组 / 组评分 1 条（同组条目不互斥，命中的都会注入）" in items[1].detail
        assert unsupported.unsupported_items(entries=[{"content": "普通"}]) == []


def _entry(entry_id: int, tokens: int, *, order: int = 100, constant: bool = True, **extra: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": entry_id,
        "book_id": 7,
        "uid": entry_id,
        "comment": f"条目{entry_id}",
        "content": "字" * tokens,
        "constant": constant,
        "enabled": True,
        "status": "active",
        "order": order,
        "keys": [],
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


BOOK = SimpleNamespace(id=7, budget_tokens=0, scan_depth=4, case_sensitive=False, match_whole_words=False)


class TestBudgetCheck:
    def test_constants_that_never_fit(self) -> None:
        entries = [
            _entry(1, 50, order=300),
            _entry(2, 60, order=200),
            _entry(3, 200, order=100),
            _entry(4, 30, order=50),
            _entry(5, 999, constant=False),
            _entry(6, 10, order=999, comment="[mvu_update]规则"),
        ]

        check = budget_check.check_constants(
            entries,
            BOOK,
            budget=120,
            max_entries=32,
            count=len,
            route=lambda entry: "state_only" if "[mvu_update]" in entry.comment else None,
        )

        assert (check.constant_count, check.constant_tokens) == (4, 340)
        assert (check.fit_count, check.fit_tokens) == (2, 110)
        assert check.routed_out == 1
        assert [(item.entry_id, item.reason, item.alone_over) for item in check.dropped] == [
            (3, "global_tokens", True),
            (4, "global_tokens", False),
        ]
        summary = check.summary()
        assert "启用的常驻条目 4 条，合计约 340 token" in summary
        assert "只有常驻条目命中时能进 2 条" in summary
        assert "单条就超预算、永远进不来的 1 条：「条目3」约 200 token" in summary
        assert "只有常驻条目命中时进不来的 1 条" in summary and "「条目4」约 30 token" in summary
        assert "永远进不来的 2 条" not in summary

    def test_book_budget_and_entry_cap(self) -> None:
        book = SimpleNamespace(**{**vars(BOOK), "budget_tokens": 40})
        entries = [_entry(1, 30, order=300), _entry(2, 30, order=200), _entry(3, 5, order=100), _entry(4, 5, order=50)]

        check = budget_check.check_constants(entries, book, budget=1000, max_entries=2, count=len)

        # 2 号放不进这本书的 40 token；3 号放得下；入选满 2 条以后 4 号按条数上限跳过
        assert [(item.entry_id, item.reason) for item in check.dropped] == [(2, "book_tokens"), (4, "max_entries")]
        assert "这本书最多 40 token" in check.summary()

    def test_all_fit_and_empty(self) -> None:
        fits = budget_check.check_constants([_entry(1, 5)], BOOK, budget=100, max_entries=32, count=len)
        empty = budget_check.check_constants([], BOOK, budget=100, max_entries=32, count=len)

        assert fits.summary().endswith("全部能进")
        assert empty.summary() == "没有启用的常驻条目"

    def test_import_side_uses_columns_and_routed_keys(self) -> None:
        budget = SimpleNamespace(budget=100, cap_tokens=0, count=len)
        columns = [
            ("10", {"uid": 10, "comment": "甲", "content": "字" * 80, "constant": True, "enabled": True, "order": 100}),
            ("11", {"uid": 11, "comment": "乙", "content": "字" * 80, "constant": True, "enabled": True, "order": 90}),
            ("12", {"uid": 12, "comment": "丙", "content": "字" * 80, "constant": True, "enabled": True, "order": 80}),
        ]

        check = budget_check.check_import(columns, budget=budget, max_entries=32, routed_keys=["12"])  # type: ignore[arg-type]

        assert check is not None
        assert (check.constant_count, check.fit_count, check.routed_out) == (2, 1, 1)
        assert [item.uid for item in check.dropped] == [11]
        assert budget_check.check_import(columns, budget=None, max_entries=32) is None


def test_world_file_import_reports_budget_check_and_unsupported(monkeypatch: Any) -> None:
    from nekro_agent.services.worldbook import service as wb_service
    from nekro_agent.services.worldbook import st_format

    budget = SimpleNamespace(budget=100, cap_tokens=0, count=len, model_group="测试组")
    monkeypatch.setattr(wb_service, "import_budget", lambda *_owner: budget)
    parsed = st_format.ParsedWorld(
        book_ext={},
        entries=[
            ("1", {"uid": 1, "comment": "甲", "content": "字" * 80, "constant": True, "order": 100}),
            ("2", {"uid": 2, "comment": "乙", "content": "字" * 80, "constant": True, "order": 90, "group": "天气"}),
            ("3", {"uid": 3, "comment": "丙", "content": "普通", "key": ["丙"]}),
        ],
    )

    prepared = wb_service._prepare_import(parsed, ("", ""), 0)  # noqa: SLF001

    by_kind = {note.kind: note for note in prepared.tail_notes}
    assert "这本书启用的常驻条目 2 条" in by_kind["budget_check"].detail
    assert "「乙」约 80 token" in by_kind["budget_check"].detail
    assert "包含组 / 组评分 1 条" in by_kind["unsupported"].detail
    summary = st_format.summarize_notes(prepared.tail_notes)
    assert summary["budget_check"] == 1 and summary["unsupported"] == 1


async def test_budget_check_runs_one_per_tenant(monkeypatch: Any) -> None:
    from nekro_agent.services.worldbook import service as wb_service

    book = SimpleNamespace(id=1, adapter_key="a", instance_key="i", budget_tokens=0)
    monkeypatch.setattr(wb_service, "import_budget", lambda *_owner: None)

    # 同一个租户已经有一个在算：直接回「忙」，不排队
    monkeypatch.setattr(wb_service, "_budget_check_running", {"tenant:7"})
    busy = await wb_service.book_budget_check(book, tenant_key="tenant:7")  # type: ignore[arg-type]
    assert busy.busy and busy.check is None

    # 别的租户照常算；算完（哪怕预算读不到）就把槽位还回来
    other = await wb_service.book_budget_check(book, tenant_key="platform")  # type: ignore[arg-type]
    assert not other.busy and other.budget is None
    assert wb_service._budget_check_running == {"tenant:7"}  # noqa: SLF001


def test_trigger_test_lists_entries_ahead() -> None:
    match_book = book_to_match(BOOK)
    other_book = book_to_match(SimpleNamespace(**{**vars(BOOK), "id": 8}))

    def hit(entry_id: int, order: int, book: Any = match_book) -> selection.Hit:
        entry = _entry(entry_id, 1, order=order, book_id=book.id)
        return selection.Hit(entry=entry_to_match(entry, book), reason=HitReason(kind="constant"))

    first, second, other = hit(1, 300), hit(2, 200), hit(3, 250, other_book)
    dropped = hit(4, 100)
    selected = (first, other, second)

    assert _ahead_of(dropped, "global_tokens", selected) == [1, 3, 2]
    assert _ahead_of(dropped, "book_tokens", selected) == [1, 2]
    assert _ahead_of(dropped, "max_entries", selected) == []
    assert _ahead_of(hit(5, 400), "global_tokens", selected) == []
