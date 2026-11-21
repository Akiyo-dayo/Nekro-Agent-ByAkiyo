"""世界书条目的用途分类、模板求值与「疑似防盗」判断（纯函数）

样本照着 MVU 一类卡片的结构现写：标题带 `[mvu_update]` / `[initvar]` / `[mvu_plot]` 的条目、
把整份变量打成 YAML 的状态渲染条目、探测运行环境的陷阱条目、`if (false)` 里藏大段文字的
条目。不取自任何真实卡片。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nekro_agent.services.worldbook import purpose
from nekro_agent.services.worldbook.purpose import (
    REASON_STATE_ONLY,
    REASON_TEMPLATE_EMPTY,
    REASON_TEMPLATE_ERROR,
    REASON_TEMPLATE_UNSUPPORTED,
    EntryPurpose,
    analyze_entry,
    render_content,
)
from nekro_agent.services.worldbook.st_format import NOTE_KINDS, summarize_notes

FILLER = "这是一段与设定无关的说明文字，用来凑够长度。" * 60  # 约 1300 字

STATE_RENDER = (
    "<% const show = (key) => { const value = getMessageVar(key); "
    "return applyVarYamlAnnotate(key); }; %>\n<status>\n时间: <%= show('stat_data.时间') %>\n"
    "地点: <%= show('stat_data.地点') %>\n</status>"
)
ENV_TRAP = (
    "<% if (Object.keys(localStorage).some(k => k.startsWith('piupiu_')) || window.__probe) { %>"
    f"{FILLER}<% }} %>"
)
DEAD_BRANCH_TRAP = f"<% if (false) {{ %>{FILLER}<% }} %>"


@pytest.fixture(autouse=True)
def _clean_cache():
    purpose.clear_cache()
    yield
    purpose.clear_cache()


class TestPurpose:
    @pytest.mark.parametrize(
        ("comment", "content", "expected"),
        [
            ("普通设定", "纯文字", EntryPurpose.NORMAL),
            ("角色", "<% if (getvar('x')) { %>甲<% } %>", EntryPurpose.NORMAL),
            ("[mvu_plot]扮演注意事项", "纯文字", EntryPurpose.MVU_PLOT),
            ("[MVU_PLOT]大写也认", "纯文字", EntryPurpose.MVU_PLOT),
            ("[mvu_update]变量更新规则", "每轮末尾输出 <UpdateVariable>…", EntryPurpose.MVU_UPDATE),
            ("[initvar]变量初始化", '{"stat_data": {}}', EntryPurpose.INITVAR),
            ("[InitVar]旧写法", '{"x": 1}', EntryPurpose.INITVAR),
            ("[InitialVariables]模板扩展的写法", '{"x": 1}', EntryPurpose.INITVAR),
            ("没有标记", '@@initial_variables\n{"x": 1}', EntryPurpose.INITVAR),
            ("状态", STATE_RENDER, EntryPurpose.STATE_RENDER),
            ("[mvu_plot]插图", "<% for (const n of names) { %>x<% } %>", EntryPurpose.UNSUPPORTED_TEMPLATE),
            ("陷阱", ENV_TRAP, EntryPurpose.UNSUPPORTED_TEMPLATE),
            ("[mvu_update]就算模板写坏了也按标题", "<% var x = 1 %>", EntryPurpose.MVU_UPDATE),
        ],
    )
    def test_purpose(self, comment: str, content: str, expected: EntryPurpose) -> None:
        assert analyze_entry(comment, content).purpose is expected

    def test_state_render_needs_mostly_code(self) -> None:
        # 只是在大段文字里提了一句函数名：不是状态渲染（这里模板本身也认不出，按不支持算）。
        content = FILLER + "<%= applyVarYamlAnnotate('stat_data') %>" + FILLER
        assert analyze_entry("设定", content).purpose is EntryPurpose.UNSUPPORTED_TEMPLATE

    def test_state_macro_counts_as_code(self) -> None:
        content = "{{format_message_variable::stat_data}}"
        assert analyze_entry("状态", content).purpose is EntryPurpose.STATE_RENDER

    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            (EntryPurpose.NORMAL, None),
            (EntryPurpose.MVU_PLOT, None),
            (EntryPurpose.MVU_UPDATE, REASON_STATE_ONLY),
            (EntryPurpose.INITVAR, REASON_STATE_ONLY),
            (EntryPurpose.STATE_RENDER, REASON_STATE_ONLY),
            (EntryPurpose.UNSUPPORTED_TEMPLATE, REASON_TEMPLATE_UNSUPPORTED),
        ],
    )
    def test_routing(self, value: EntryPurpose, reason: str | None) -> None:
        assert purpose.EntryAnalysis(purpose=value).routed_out_reason == reason

    def test_unsupported_reason_is_kept(self) -> None:
        analysis = analyze_entry("x", "<% let a = 1 %>")
        assert analysis.unsupported_reason
        assert analysis.template is None


class TestTrap:
    def test_env_probe_trap(self) -> None:
        analysis = analyze_entry("拉古那海事法典摘编", ENV_TRAP)
        assert analysis.suspected_trap
        assert "localStorage" in analysis.trap_reason and "window" in analysis.trap_reason
        assert analysis.routed_out_reason == REASON_TEMPLATE_UNSUPPORTED

    def test_dead_branch_trap_is_flagged_and_renders_to_nothing(self) -> None:
        analysis = analyze_entry("排序", DEAD_BRANCH_TRAP)
        assert analysis.suspected_trap
        assert "永远不成立" in analysis.trap_reason
        assert analysis.purpose is EntryPurpose.NORMAL
        assert render_content(DEAD_BRANCH_TRAP, analysis) == purpose.Rendered("", REASON_TEMPLATE_EMPTY)

    def test_dead_branch_trap_with_visible_text_keeps_only_the_visible_text(self) -> None:
        content = f"正常设定。<% if (false) {{ %>{FILLER}<% }} %>"
        analysis = analyze_entry("排序", content)
        assert analysis.suspected_trap
        assert render_content(content, analysis).content == "正常设定。"

    def test_short_conditional_text_is_not_a_trap(self) -> None:
        assert not analyze_entry("x", "<% if (false) { %>几句话<% } %>").suspected_trap
        short_env = "<% if (window.x) { %>一小段<% } %>"
        assert not analyze_entry("x", short_env).suspected_trap

    def test_variable_gated_long_text_is_not_a_trap(self) -> None:
        content = f"<% if (getvar('stat_data.剧情权重') >= 3) {{ %>{FILLER}<% }} %>"
        assert not analyze_entry("角色", content).suspected_trap


class TestLinearTime:
    @pytest.mark.parametrize(
        "content",
        [
            "getMessageVar(" + "<%" * 150_000,  # 状态渲染判断里数代码量
            "getMessageVar(" + "{{get_message_variable::" * 20_000,  # 宏没有闭合
        ],
        ids=["unclosed-tags", "unclosed-macros"],
    )
    def test_state_render_check_is_linear(self, content: str) -> None:
        import time

        started = time.monotonic()
        analyze_entry("x", content)
        assert time.monotonic() - started < 3


class TestRender:
    def test_no_template_returns_the_same_text(self) -> None:
        analysis = analyze_entry("普通", "纯文字")
        assert render_content("纯文字", analysis) == purpose.Rendered("纯文字")

    def test_template_error(self) -> None:
        content = "<% if (getvar('地点').includes('冰原')) { %>冷<% } %>其它"
        analysis = analyze_entry("x", content)
        assert render_content(content, analysis) == purpose.Rendered("", REASON_TEMPLATE_ERROR)

    def test_rendered_text(self) -> None:
        content = "<% if (getvar('x') === undefined) { %>初次见面<% } else { %>老朋友<% } %>"
        assert render_content(content, analyze_entry("x", content)).content == "初次见面"

    @pytest.mark.parametrize(
        "condition",
        ["'" + chr(92) + "x41' === 'A'", "'x'.constructor", "!!'x'.toLowerCase"],
    )
    def test_constants_true_in_js_do_not_inject_bait(self, condition: str) -> None:
        # JS 里这几个条件为真、只输出「短」；与 JS 对不上的写法整条分流（运行时按分流原因不注入），
        # 不能把那一大段带进来
        content = f"<% if ({condition}) {{ %>短<% }} else {{ %>{'长' * 1200}<% }} %>"
        assert analyze_entry("x", content).routed_out_reason == REASON_TEMPLATE_UNSUPPORTED

    def test_other_properties_of_primitives_do_not_inject_bait(self) -> None:
        content = "<% if ('x'.at) { %>短<% } else { %>" + "长" * 1200 + "<% } %>"
        assert render_content(content, analyze_entry("x", content)) == purpose.Rendered("", REASON_TEMPLATE_ERROR)


class TestCache:
    def test_cached_by_id_and_update_time(self) -> None:
        row = SimpleNamespace(id=7, update_time="t1", comment="[mvu_update]规则", content="x")
        first = purpose.analysis_for(row)
        assert purpose.analysis_for(row) is first
        row.comment = "普通"
        # 更新时间没变：仍是缓存里的结果（真实的行改正文时更新时间一定会变）。
        assert purpose.analysis_for(row).purpose is EntryPurpose.MVU_UPDATE
        row.update_time = "t2"
        assert purpose.analysis_for(row).purpose is EntryPurpose.NORMAL

    def test_cache_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(purpose, "MAX_CACHE_ENTRIES", 10)
        for index in range(25):
            purpose.analysis_for(SimpleNamespace(id=index, update_time=0, comment="", content=""))
        assert len(purpose._cache) <= 10  # noqa: SLF001


class TestImportNotes:
    def test_kinds(self) -> None:
        columns = [
            ("1", {"comment": "[mvu_update]规则", "content": "x"}),
            ("2", {"comment": "[initvar]初值", "content": "{}"}),
            ("3", {"comment": "状态", "content": STATE_RENDER}),
            ("4", {"comment": "插图", "content": "<% for (;;) { %>x<% } %>"}),
            ("5", {"comment": "陷阱", "content": ENV_TRAP}),
            ("6", {"comment": "排序", "content": DEAD_BRANCH_TRAP}),
            ("7", {"comment": "普通", "content": "<% if (getvar('x')) { %>y<% } %>"}),
        ]
        notes = purpose.template_import_notes(columns)
        by_key: dict[str, set[str]] = {}
        for note in notes:
            by_key.setdefault(note.entry_key, set()).add(note.kind)
        assert by_key == {
            "1": {"state_only"},
            "2": {"state_only"},
            "3": {"state_only"},
            "4": {"unsupported_template"},
            "5": {"unsupported_template", "suspected_trap"},
            "6": {"suspected_trap"},
        }
        counts = summarize_notes(notes)
        assert set(NOTE_KINDS) <= set(counts)
        assert counts["state_only"] == 3 and counts["unsupported_template"] == 2 and counts["suspected_trap"] == 2


BAIT = "<% if (!getvar('stat_data')) { %>" + "诱饵文字" * 400 + "<% } %>正常设定"


class TestMissingVariableBait:
    def test_bait_is_flagged_and_routed_out(self) -> None:
        analysis = analyze_entry("排序", BAIT)
        assert analysis.suspected_trap
        assert "变量读不到" in analysis.trap_reason
        assert analysis.routed_out_reason == purpose.REASON_TEMPLATE_TRAP
        # 运行时不跑防盗扫描，但诱饵判定照样生效
        quick = analyze_entry("排序", BAIT, with_trap=False)
        assert not quick.suspected_trap
        assert quick.routed_out_reason == purpose.REASON_TEMPLATE_TRAP

    def test_import_note_says_it_is_not_injected(self) -> None:
        notes = purpose.template_import_notes([("1", {"comment": "排序", "content": BAIT})])
        assert [(note.kind, "不注入" in note.detail) for note in notes] == [("suspected_trap", True)]


class TestNeverRaises:
    def test_analysis_errors_only_affect_the_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(_source: str) -> None:
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(purpose.template_lite, "compile_template", explode)
        analysis = analyze_entry("x", "<%= 1 %>")
        assert analysis.purpose is EntryPurpose.UNSUPPORTED_TEMPLATE
        assert analysis.routed_out_reason == REASON_TEMPLATE_UNSUPPORTED

    def test_render_errors_only_affect_the_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        analysis = analyze_entry("x", "<%= 1 %>")

        def explode(*_args: object, **_kwargs: object) -> str:
            raise RecursionError("maximum recursion depth exceeded")

        # 有变量时现算：出错只让这一条记 `template_error`
        monkeypatch.setattr(purpose.template_lite, "render", explode)
        variables = purpose.template_lite.MappingVariables(cache={"a": 1})
        assert render_content("<%= 1 %>", analysis, variables) == purpose.Rendered("", REASON_TEMPLATE_ERROR)
        # 空变量的结果在分析时就算好了；分析时的对比渲染出意外，按「含不支持的模板」分流
        monkeypatch.setattr(purpose.template_lite, "compare_renders", explode)
        assert analyze_entry("x", "<%= 2 %>").routed_out_reason == REASON_TEMPLATE_UNSUPPORTED

    def test_empty_and_failing_renders_are_known_routes(self) -> None:
        assert analyze_entry("x", "<% if (getvar('a')) { %>有<% } %>").routed_out_reason == purpose.REASON_TEMPLATE_EMPTY
        assert analyze_entry("x", "<%= getvar('a').b %>").routed_out_reason == REASON_TEMPLATE_ERROR
        assert analyze_entry("x", "<% if (true) { %>有<% } %>").rendered == "有"

    def test_long_unsupported_reasons_are_short(self) -> None:
        analysis = analyze_entry("x", "<% '" + "A" * 50_000 + "' %>")
        assert len(analysis.unsupported_reason) <= purpose.template_lite.MAX_REASON_CHARS


class TestQuickRoute:
    @pytest.mark.parametrize(
        ("comment", "content", "routed"),
        [
            ("[mvu_update]规则", "x", True),
            ("[InitVar]初值", "{}", True),
            ("没有标记", "  \n@@initial_variables\n{}", True),
            ("[mvu_plot]剧情", "x", False),
            ("普通", "<% var x = 1 %>", False),  # 正文要等命中之后才分析
        ],
    )
    def test_quick_route_only_reads_the_title(self, comment: str, content: str, routed: bool) -> None:
        assert (purpose.quick_route(comment, content) is not None) is routed

    @pytest.mark.parametrize(
        "content",
        [
            "每轮末尾输出 <UpdateVariable> 块",
            "更新时调用 _.set('a', 1, 2)",
        ],
    )
    def test_plot_title_with_update_format_routes_as_state_only(self, content: str) -> None:
        assert purpose.quick_route("[mvu_plot]变量格式说明", content) == REASON_STATE_ONLY

    def test_plot_title_with_plain_body_is_not_routed(self) -> None:
        assert purpose.quick_route("[mvu_plot]剧情", "普通正文") is None

    def test_update_format_without_plot_title_keeps_old_behavior(self) -> None:
        # 改动前这里就是照常注入；标题不带 `[mvu_plot]` 时不扫正文，结论不变。
        assert purpose.quick_route("普通标题", "每轮输出 <UpdateVariable> 块") is None
        assert analyze_entry("普通标题", "每轮输出 <UpdateVariable> 块").purpose is EntryPurpose.NORMAL

    def test_update_format_scan_is_fast_on_adversarial_text(self) -> None:
        content = "<UpdateVariable" * 5_000 + "_.set(" * 5_000
        assert len(content) >= 100_000

        started = time.thread_time()
        assert purpose.title_purpose("[mvu_plot]变量格式说明", content) is EntryPurpose.MVU_UPDATE
        elapsed = time.thread_time() - started

        assert elapsed < 1.0


class TestPrepareContent:
    @staticmethod
    def _row(entry_id: int, content: str, comment: str = "") -> SimpleNamespace:
        return SimpleNamespace(id=entry_id, update_time="t", comment=comment, content=content)

    def test_plain_text_is_returned_as_is_without_budget(self) -> None:
        budget = purpose.RoundBudget(chars_left=0)
        content = "纯文字"
        assert purpose.prepare_content(self._row(1, content), content, budget).content is content

    def test_budget_runs_out(self) -> None:
        content = "<% if (true) { %>" + "字" * 100 + "<% } %>"
        # 每次分析（编译加对比渲染）至少按 512 字记账；注入用的是分析时算好的文字，不再另扣
        budget = purpose.RoundBudget(chars_left=purpose.MIN_ANALYSIS_CHARGE + 10)
        first = purpose.prepare_content(self._row(1, content), content, budget)
        assert first.reason is None and first.content == "字" * 100
        second = purpose.prepare_content(self._row(2, content), content, budget)
        assert second.reason == purpose.REASON_TEMPLATE_BUDGET

    def test_cached_analysis_costs_no_characters(self) -> None:
        content = "<% if (true) { %>有<% } %>"
        row = self._row(3, content)
        purpose.analysis_for(row)
        budget = purpose.RoundBudget(chars_left=0, analyses_left=1)
        assert purpose.prepare_content(row, content, budget).content == "有"
        assert (budget.chars_left, budget.analyses_left) == (0, 0)

    def test_routed_reasons(self) -> None:
        budget = purpose.RoundBudget()
        assert purpose.prepare_content(self._row(4, STATE_RENDER), STATE_RENDER, budget).reason == REASON_STATE_ONLY
        assert purpose.prepare_content(self._row(5, BAIT), BAIT, budget).reason == purpose.REASON_TEMPLATE_TRAP
        bad = "<% var x = 1 %>"
        assert purpose.prepare_content(self._row(6, bad), bad, budget).reason == REASON_TEMPLATE_UNSUPPORTED


class TestLruCache:
    def test_hits_move_to_the_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(purpose, "MAX_CACHE_ENTRIES", 2)
        rows = [SimpleNamespace(id=index, update_time=0, comment="", content="x") for index in range(3)]
        purpose.analysis_for(rows[0])
        purpose.analysis_for(rows[1])
        purpose.analysis_for(rows[0])  # 0 变成最近使用
        purpose.analysis_for(rows[2])  # 挤掉的是 1
        assert purpose.cached_analysis(rows[0]) is not None
        assert purpose.cached_analysis(rows[1]) is None

    def test_byte_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        content = "<% if (true) { %>" + "字" * 5000 + "<% } %>"
        monkeypatch.setattr(purpose, "MAX_CACHE_BYTES", 30_000)
        for index in range(10):
            purpose.analysis_for(SimpleNamespace(id=index, update_time=0, comment="", content=content))
        entries, used = purpose.cache_stats()
        assert used <= 30_000
        assert 0 < entries < 10


class TestPerAnalysisCharge:
    """每次分析的最低记账与每轮条数上限：几万条只有几个字的模板条目不能形同不封顶"""

    @staticmethod
    def _row(entry_id: int, content: str, comment: str = "") -> SimpleNamespace:
        return SimpleNamespace(id=entry_id, update_time="t", comment=comment, content=content)

    def test_every_analysis_is_charged_at_least_512(self) -> None:
        budget = purpose.RoundBudget(chars_left=purpose.MIN_ANALYSIS_CHARGE * 3)
        tiny = "<%x%>"
        reasons = [purpose.prepare_content(self._row(index, tiny), tiny, budget).reason for index in range(5)]
        assert reasons == [REASON_TEMPLATE_UNSUPPORTED] * 3 + [purpose.REASON_TEMPLATE_BUDGET] * 2

    def test_partial_renders_are_charged_after_the_analysis(self) -> None:
        content = "<% if (getvar('a') && getvar('b')) { %>都有<% } %><% if (getvar('a') || true) { %>" + "甲" * 1200
        content += "<% } %>"
        charge = purpose._analysis_charge(content)
        budget = purpose.RoundBudget(chars_left=charge * 2)
        first = purpose.prepare_content(self._row(1, content), content, budget)
        assert first.reason is None
        # 补记了「只有一部分变量」的三次，剩下的不够再分析一条
        assert budget.chars_left == charge - len(content) * 3 // 4
        second = purpose.prepare_content(self._row(2, content), content, budget)
        assert second.reason == purpose.REASON_TEMPLATE_BUDGET

    def test_templated_hits_are_capped_per_round(self) -> None:
        budget = purpose.RoundBudget(analyses_left=2)
        content = "<% if (true) { %>有<% } %>"
        results = [purpose.prepare_content(self._row(10 + index, content), content, budget) for index in range(4)]
        assert [result.reason for result in results] == [None, None, purpose.REASON_TEMPLATE_BUDGET, purpose.REASON_TEMPLATE_BUDGET]
        # 没有模板的条目不占名额
        assert purpose.prepare_content(self._row(99, "纯文字"), "纯文字", budget).content == "纯文字"


class TestSummaryCache:
    @staticmethod
    def _row(entry_id: int, content: str, comment: str = "") -> SimpleNamespace:
        return SimpleNamespace(id=entry_id, update_time="t", comment=comment, content=content)

    def test_listing_does_not_touch_the_ast_cache(self) -> None:
        rows = [self._row(index, "<% if (getvar('x')) { %>甲<% } %>") for index in range(3)]
        summaries = purpose.summaries_for_listing(rows)
        assert all(summary is not None and summary.suspected_trap is False for summary in summaries.values())
        assert purpose.cache_stats()[0] == 0

    def test_listing_budget_marks_the_rest_pending(self) -> None:
        dense = "<%=1%>" * 2000
        rows = [self._row(index, dense) for index in range(5)]
        # 每条按编译 / 扫描（一份正文）加对比渲染记账：不读变量的模板只渲染一次（四分之三份正文）
        assert purpose._listing_cost(dense) == len(dense) + len(dense) * 6 // 8
        summaries = purpose.summaries_for_listing(rows, budget_chars=purpose._listing_cost(dense) * 2)
        assert [summaries[index] is None for index in range(5)] == [False, False, True, True, True]
        # 下一次请求接着算
        summaries = purpose.summaries_for_listing(rows, budget_chars=purpose._listing_cost(dense) * 2)
        assert [summaries[index] is None for index in range(5)] == [False, False, False, False, True]

    # 两个根变量、一千多字的正文：诱饵判断要做「只有一部分变量」的三次渲染
    PARTIAL = "<% if (getvar('a') && getvar('b')) { %>都有<% } %><% if (getvar('a') || true) { %>" + "甲" * 1200 + "<% } %>"

    def test_render_charge_counts_the_full_renders_up_front(self) -> None:
        # 读变量的模板：变量全读不到一次加四组哨兵，每次四分之三份正文
        assert purpose._listing_cost(self.PARTIAL) == len(self.PARTIAL) + len(self.PARTIAL) * 5 * 3 // 4
        assert purpose._analysis_charge(self.PARTIAL) == purpose._listing_cost(self.PARTIAL)
        assert analyze_entry("", self.PARTIAL).partial_renders == 3

    def test_listing_charges_partial_renders_afterwards(self) -> None:
        rows = [self._row(index, self.PARTIAL) for index in range(3)]
        # 两条的预先记账刚好够；第一条做完补记了「只有一部分变量」的三次，第二条就不够了
        summaries = purpose.summaries_for_listing(rows, budget_chars=purpose._listing_cost(self.PARTIAL) * 2)
        assert [summaries[index] is None for index in range(3)] == [False, True, True]

    def test_import_notes_charge_partial_renders_afterwards(self) -> None:
        columns = [(str(index), {"comment": "", "content": self.PARTIAL}) for index in range(3)]
        notes = purpose.template_import_notes(columns, budget_chars=purpose._listing_cost(self.PARTIAL) * 2)
        assert [note.entry_key for note in notes if note.kind == purpose.NOTE_NOT_ANALYZED] == ["1", "2"]

    def test_first_entry_is_always_analysed(self) -> None:
        big = "<% " + "a.b(c); " * 20_000 + " %>"
        summaries = purpose.summaries_for_listing([self._row(1, big)], budget_chars=1)
        assert summaries[1] is not None and summaries[1].purpose is EntryPurpose.UNSUPPORTED_TEMPLATE

    def test_plain_entries_are_never_pending(self) -> None:
        rows = [self._row(index, "纯文字") for index in range(5)]
        summaries = purpose.summaries_for_listing(rows, budget_chars=0)
        assert all(summary is not None for summary in summaries.values())

    def test_runtime_analysis_keeps_the_trap_verdict(self) -> None:
        row = self._row(7, DEAD_BRANCH_TRAP)
        assert purpose.summary_for(row).suspected_trap is True
        purpose.analysis_for(row)  # 运行时不跑防盗扫描
        assert purpose.cached_summary(row).suspected_trap is True

    def test_known_route_only_reads_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = self._row(8, "<% var x = 1 %>")
        assert purpose.known_route(row) is None

        def forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("known_route 不该分析")

        purpose.summary_for(row)
        monkeypatch.setattr(purpose, "analyze_entry", forbidden)
        assert purpose.known_route(row) == REASON_TEMPLATE_UNSUPPORTED


class TestAnalysisPools:
    @staticmethod
    def _row(entry_id: int, content: str) -> SimpleNamespace:
        return SimpleNamespace(id=entry_id, update_time="t", comment="", content=content)

    async def test_busy_listing_returns_pending_without_queueing(self) -> None:
        rows = [self._row(index, "<% if (true) { %>有<% } %>") for index in range(3)]
        assert purpose._listing_slot.acquire(blocking=False)
        try:
            summaries = await purpose.listing_summaries(rows)
        finally:
            purpose._listing_slot.release()
        assert all(summary is None for summary in summaries.values())
        summaries = await purpose.listing_summaries(rows)
        assert all(summary is not None for summary in summaries.values())
        # 都在缓存里了：不再进执行器，也就不看它忙不忙
        assert purpose._listing_slot.acquire(blocking=False)
        try:
            assert all(summary is not None for summary in (await purpose.listing_summaries(rows)).values())
        finally:
            purpose._listing_slot.release()

    async def test_slot_is_released_by_the_worker(self) -> None:
        rows = [self._row(10, "<%= 1 %>")]
        await purpose.listing_summaries(rows)
        assert purpose._listing_slot.acquire(blocking=False)
        purpose._listing_slot.release()

    def test_entries_being_analysed_elsewhere_are_pending(self) -> None:
        row = self._row(20, "<%= 1 %>")
        purpose._inflight.add(purpose._cache_key(row))
        try:
            assert purpose.summaries_for_listing([row]) == {20: None}
        finally:
            purpose._inflight.discard(purpose._cache_key(row))
        assert purpose.summaries_for_listing([row])[20] is not None
        assert not purpose._inflight

    async def test_imports_of_different_tenants_run_at_most_two_at_a_time(self) -> None:
        import asyncio
        import threading
        import time

        active = 0
        peak = 0
        guard = threading.Lock()

        def work(value: int) -> int:
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.05)
                return value
            finally:
                with guard:
                    active -= 1

        # 不同租户：导入执行器两个线程，多出来的排队
        keys = [purpose.tenant_slot_key(None)] + [purpose.tenant_slot_key(index) for index in range(1, 4)]
        results = await asyncio.gather(
            *(purpose.run_import_analysis(work, index, tenant_key=key) for index, key in enumerate(keys)),
        )
        assert results == [0, 1, 2, 3] and peak == 2

    async def test_same_tenant_gets_409_instead_of_queueing(self) -> None:
        import asyncio
        import threading

        from nekro_agent.schemas.errors import WorldbookAnalysisBusyError

        release = threading.Event()
        first = asyncio.ensure_future(purpose.run_import_analysis(release.wait, 5, tenant_key="tenant:7"))
        await asyncio.sleep(0.05)
        with pytest.raises(WorldbookAnalysisBusyError):
            await purpose.run_import_analysis(lambda: None, tenant_key="tenant:7")
        # 卡片检查有自己的执行器与名额，不受导入影响
        assert await purpose.run_inspect_analysis(lambda: "ok", tenant_key="tenant:7") == "ok"
        release.set()
        assert await first is True
        # 跑完名额就还回来了
        assert await purpose.run_import_analysis(lambda: "again", tenant_key="tenant:7") == "again"

    async def test_single_summary(self) -> None:
        summary = await purpose.single_summary(self._row(30, BAIT))
        assert summary.suspected_trap is True and summary.routed_reason == purpose.REASON_TEMPLATE_TRAP

    async def test_single_summary_is_pending_while_busy(self) -> None:
        assert purpose._listing_slot.acquire(blocking=False)
        try:
            assert await purpose.single_summary(self._row(31, "<%= 1 %>")) is None
        finally:
            purpose._listing_slot.release()

    async def test_cancelled_request_does_not_leak_the_slot(self) -> None:
        import asyncio
        import threading

        release = threading.Event()
        # 先占住执行器（模拟一个正在跑的任务），再提交一页：槽位被它拿着，任务在执行器里排队
        blocker = purpose._listing_executor.submit(release.wait, 5)
        page = [self._row(40 + index, "<%= 1 %>") for index in range(3)]
        task = asyncio.ensure_future(purpose.listing_summaries(page))
        await asyncio.sleep(0.05)
        assert purpose._listing_slot.locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        blocker.result(timeout=5)
        await asyncio.sleep(0.05)
        # 排着的任务被取消了：槽位跟着还回来，之后的请求照常分析
        assert not purpose._listing_slot.locked()
        summaries = await purpose.listing_summaries(page)
        assert all(summary is not None for summary in summaries.values())


class TestPlainEntriesAreCheap:
    def test_state_render_scan_is_skipped_without_templates_or_macros(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def forbidden(_content: str) -> bool:
            raise AssertionError("没有模板也没有变量宏的正文不该扫状态渲染")

        monkeypatch.setattr(purpose, "_is_state_render", forbidden)
        content = "getMessageVar(" + "{{}}" * 1000
        assert analyze_entry("x", content).purpose is EntryPurpose.NORMAL


class TestEmptyRendersAreKnownRoutes:
    def test_known_after_the_first_analysis(self) -> None:
        row = SimpleNamespace(id=40, update_time="t", comment="", content="<% if (getvar('a')) { %>有<% } %>")
        assert purpose.known_route(row) is None
        budget = purpose.RoundBudget()
        assert purpose.prepare_content(row, row.content, budget).reason == purpose.REASON_TEMPLATE_EMPTY
        assert purpose.known_route(row) == purpose.REASON_TEMPLATE_EMPTY


class TestStaticRenderSwitch:
    """空变量渲染结果的缓存只在变量提供者为空、开关开着时生效"""

    EMPTY = "<% if (getvar('a')) { %>有<% } %>"

    def test_switch_off_means_no_render_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        analysis = analyze_entry("x", self.EMPTY)
        assert analysis.routed_out_reason == purpose.REASON_TEMPLATE_EMPTY
        monkeypatch.setattr(purpose, "STATIC_RENDER_CACHE", False)
        assert analysis.routed_out_reason is None
        assert render_content(self.EMPTY, analysis) == purpose.Rendered("", purpose.REASON_TEMPLATE_EMPTY)

    def test_known_route_ignores_cached_render_routes_when_switched_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = SimpleNamespace(id=900, update_time="t", comment="", content=self.EMPTY)
        purpose.summary_for(row)
        assert purpose.known_route(row) == purpose.REASON_TEMPLATE_EMPTY
        monkeypatch.setattr(purpose, "STATIC_RENDER_CACHE", False)
        assert purpose.known_route(row) is None

    def test_a_real_provider_is_rendered_fresh(self) -> None:
        row = SimpleNamespace(id=901, update_time="t", comment="", content=self.EMPTY)
        purpose.analysis_for(row)
        variables = purpose.template_lite.MappingVariables(cache={"a": 1})
        assert purpose.static_render_ok(variables) is False
        rendered = purpose.prepare_content(row, row.content, purpose.RoundBudget(), variables)
        assert rendered == purpose.Rendered("有")


class TestImportAnalysisCap:
    def test_entries_past_the_cap_are_marked_not_analysed(self) -> None:
        dense = "<%=1%>" * 2000
        columns = [(str(index), {"comment": "", "content": dense}) for index in range(4)]
        columns.append(("t", {"comment": "[mvu_update]规则", "content": dense}))
        columns.append(("p", {"comment": "", "content": "纯文字"}))
        notes = purpose.template_import_notes(columns, budget_chars=purpose._listing_cost(dense) * 2)
        kinds = [(note.entry_key, note.kind) for note in notes]
        assert ("2", purpose.NOTE_NOT_ANALYZED) in kinds and ("3", purpose.NOTE_NOT_ANALYZED) in kinds
        assert ("0", purpose.NOTE_NOT_ANALYZED) not in kinds and ("1", purpose.NOTE_NOT_ANALYZED) not in kinds
        # 超出上限的条目，按标题能定的用途照样报
        assert ("t", "state_only") in kinds
        assert not any(key == "p" for key, _kind in kinds)
        assert purpose.NOTE_NOT_ANALYZED in NOTE_KINDS


class TestDeterministicVerdicts:
    def test_import_listing_and_runtime_agree(self) -> None:
        content = "<% if (!getvar('s') || getvar('s').length != 13) { %>" + "B" * 2000 + "<% } %>ok"
        verdicts = {analyze_entry("", content).routed_out_reason for _ in range(10)}
        row = SimpleNamespace(id=950, update_time="t", comment="", content=content)
        verdicts.add(purpose.summary_for(row).routed_reason)
        purpose.clear_cache()
        verdicts.add(purpose.analysis_for(row).routed_out_reason)
        assert len(verdicts) == 1


class TestPersistentSentinelKey:
    """诱饵判断里随机那两组哨兵的密钥：从凭据加密的主密钥按用途派生，整个进程只取一次"""

    @pytest.fixture
    def cipher(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.tools import credential_cipher

        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        credential_cipher.reset_credential_cipher_cache()
        yield credential_cipher, OsEnv
        credential_cipher.reset_credential_cipher_cache()

    def test_no_key_and_no_key_file_is_created(self, cipher, tmp_path: Path) -> None:  # noqa: ANN001
        credential_cipher, _env = cipher
        assert credential_cipher.derive_purpose_key("worldbook-template-sentinels") is None
        # 不会为了这把派生密钥去生成凭据密钥文件
        assert not (tmp_path / "data" / credential_cipher.KEY_FILE_NAME).exists()

    def test_derived_from_the_env_key(self, cipher, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
        credential_cipher, env = cipher
        monkeypatch.setattr(env, "CREDENTIAL_SECRET_KEY", "自己编的测试密钥材料-7Q4b")
        credential_cipher.reset_credential_cipher_cache()
        first = credential_cipher.derive_purpose_key("worldbook-template-sentinels")
        assert first is not None and len(first) == 32
        assert credential_cipher.derive_purpose_key("worldbook-template-sentinels") == first
        assert credential_cipher.derive_purpose_key("别的用途") != first
        # 派生出来的不是加密用的那把
        assert first != credential_cipher._key_ring_cached().primary

    def test_derived_from_an_existing_key_file(self, cipher, tmp_path: Path) -> None:  # noqa: ANN001
        credential_cipher, _env = cipher
        key_file = tmp_path / "data" / credential_cipher.KEY_FILE_NAME
        key_file.parent.mkdir(parents=True)
        key_file.write_text("自己编的文件密钥材料", encoding="utf-8")
        derived = credential_cipher.derive_purpose_key("worldbook-template-sentinels")
        assert derived is not None
        credential_cipher.reset_credential_cipher_cache()
        assert credential_cipher.derive_purpose_key("worldbook-template-sentinels") == derived

    def test_analysis_resolves_the_key_once_and_passes_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(purpose, "_sentinel_key_resolved", False)
        monkeypatch.setattr(purpose, "_sentinel_key_value", None)
        calls: list[str] = []

        def fake(name: str) -> bytes:
            calls.append(name)
            return b"k" * 32

        monkeypatch.setattr("nekro_agent.tools.credential_cipher.derive_purpose_key", fake)
        seen: list[object] = []
        real = purpose.template_lite.compare_renders

        def spy(template, **kwargs):  # noqa: ANN001, ANN202
            seen.append(kwargs.get("key"))
            return real(template, **kwargs)

        monkeypatch.setattr(purpose.template_lite, "compare_renders", spy)
        analyze_entry("", "<% if (getvar('a')) { %>甲<% } %>")
        analyze_entry("", "<% if (getvar('b')) { %>乙<% } %>")
        assert seen == [b"k" * 32, b"k" * 32]
        assert calls == ["worldbook-template-sentinels"]

    def test_falls_back_to_the_process_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(purpose, "_sentinel_key_resolved", False)
        monkeypatch.setattr(purpose, "_sentinel_key_value", b"stale")

        def broken(_name: str) -> bytes:
            raise RuntimeError("凭据加密不可用")

        monkeypatch.setattr("nekro_agent.tools.credential_cipher.derive_purpose_key", broken)
        assert purpose._sentinel_key() is None
        # 退回每个进程随机的那把，判断照常
        assert analyze_entry("", "<% if (!getvar('a')) { %>" + FILLER + "<% } %>").routed_out_reason == "template_trap"
