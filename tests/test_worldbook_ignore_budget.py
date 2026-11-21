"""世界书「忽略预算」与「扫描时带上说话人名字」的纯函数判据

「忽略预算」（酒馆的 `ignoreBudget`）：宿主条目命中后不受全局与书预算拦，仍受条数上限，
token 照样计入已用量；租户条目上的标记不起作用；这类条目合计有兜底上限，超出的按普通条目判。

「扫描时带上说话人名字」（酒馆的 `include_names`）：书级开关，抄到条目上；扫描文本写成
「名字: 正文」，没有名字的消息只有正文。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from nekro_agent.services.worldbook import budget as wb_budget
from nekro_agent.services.worldbook import budget_check
from nekro_agent.services.worldbook.matcher import HitReason, entry_hit, scan_text
from nekro_agent.services.worldbook.safe_regex import RegexRunner
from nekro_agent.services.worldbook.selection import Hit, select_hits
from nekro_agent.services.worldbook.types import (
    MatchEntry,
    Position,
    ScopeKind,
    SelectiveLogic,
    WindowMessage,
    book_to_match,
    entry_to_match,
)


def _entry(entry_id: int, *, size: int, trusted: bool = True, ignore: bool = False, order: int = 100, book_id: int = 1) -> MatchEntry:
    """正文长度就是它的 token 数（用例里 `count` 按长度数）"""
    return MatchEntry(
        id=entry_id,
        book_id=book_id,
        keys=("龙",),
        secondary_keys=(),
        selective_logic=SelectiveLogic.AND_ANY,
        use_regex=False,
        constant=True,
        content="字" * size,
        order=order,
        position=Position.BEFORE_HISTORY,
        depth=4,
        scan_depth=4,
        case_sensitive=False,
        match_whole_words=False,
        sticky=0,
        cooldown=0,
        delay=0,
        trusted=trusted,
        invalid_keys=frozenset(),
        ignore_budget=ignore,
    )


def _select(entries: list[MatchEntry], **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "max_tokens": 10,
        "max_entries": 32,
        "book_budgets": {},
        "count": lambda entry: len(entry.content),
        "ignore_budget_cap": 1000,
    }
    params.update(kwargs)
    return select_hits([Hit(entry=entry, reason=HitReason(kind="constant")) for entry in entries], **params)


def _ids(hits: Any) -> list[int]:
    return [hit.entry.id for hit in hits]


def _dropped(selection: Any) -> dict[int, str]:
    return {hit.entry.id: reason for hit, reason in selection.dropped}


class TestIgnoreBudget:
    def test_a_trusted_entry_passes_the_global_budget(self) -> None:
        chosen = _select([_entry(1, size=50, ignore=True)], max_tokens=10)

        assert _ids(chosen.selected) == [1]
        assert chosen.ignored_budget == frozenset({1})
        assert chosen.total_tokens == 50, "豁免的条目照样计入已用量"

    def test_it_uses_up_room_for_later_ordinary_entries(self) -> None:
        """与酒馆一致：豁免条目的 token 挤占后面普通条目的余量"""
        chosen = _select(
            [_entry(1, size=8, ignore=True, order=200), _entry(2, size=5, order=100)],
            max_tokens=10,
        )

        assert _ids(chosen.selected) == [1]
        assert _dropped(chosen) == {2: "global_tokens"}

    def test_a_tenant_entry_is_not_exempt(self) -> None:
        chosen = _select([_entry(1, size=50, trusted=False, ignore=True)], max_tokens=10)

        assert _ids(chosen.selected) == []
        assert _dropped(chosen) == {1: "global_tokens"}
        assert chosen.ignored_budget == frozenset()

    def test_the_book_budget_does_not_stop_it(self) -> None:
        chosen = _select(
            [_entry(1, size=6, ignore=True, order=200), _entry(2, size=3, order=100)],
            max_tokens=100,
            book_budgets={1: 5},
        )

        assert _ids(chosen.selected) == [1]
        assert _dropped(chosen) == {2: "book_tokens"}, "豁免条目也计入这本书的已用量"

    def test_the_entry_limit_still_applies(self) -> None:
        chosen = _select(
            [_entry(1, size=1, order=300), _entry(2, size=1, ignore=True, order=200)],
            max_tokens=100,
            max_entries=1,
        )

        assert _ids(chosen.selected) == [1]
        assert _dropped(chosen) == {2: "max_entries"}

    def test_over_the_cap_the_rest_are_judged_as_ordinary(self) -> None:
        """合计超过兜底上限的那几条不再豁免：放得下照常进，放不下记 `global_tokens`"""
        chosen = _select(
            [
                _entry(1, size=30, ignore=True, order=300),
                _entry(2, size=30, ignore=True, order=200),
                _entry(3, size=5, ignore=True, order=100),
            ],
            max_tokens=40,
            ignore_budget_cap=50,
        )

        # 1 豁免（合计 30）；2 再豁免就到 60，超过 50，按普通条目判：已用 30 + 30 > 40，跳过；
        # 3 再豁免合计 35，没超，照样豁免
        assert _ids(chosen.selected) == [1, 3]
        assert _dropped(chosen) == {2: "global_tokens"}
        assert chosen.ignored_budget == frozenset({1, 3})

    def test_over_the_cap_but_fitting_goes_in_as_ordinary(self) -> None:
        chosen = _select([_entry(1, size=8, ignore=True)], max_tokens=10, ignore_budget_cap=5)

        assert _ids(chosen.selected) == [1]
        assert chosen.ignored_budget == frozenset(), "这条是按普通条目放进来的"

    def test_a_zero_cap_exempts_nothing(self) -> None:
        chosen = _select([_entry(1, size=50, ignore=True)], max_tokens=10, ignore_budget_cap=0)

        assert _ids(chosen.selected) == []

    def test_without_the_flag_nothing_changes(self) -> None:
        entries = [_entry(1, size=6, order=200), _entry(2, size=6, order=100), _entry(3, size=3, order=50)]

        chosen = _select(entries, max_tokens=10)

        assert _ids(chosen.selected) == [1, 3]
        assert _dropped(chosen) == {2: "global_tokens"}
        assert chosen.ignored_budget == frozenset()


class TestTheCap:
    @staticmethod
    def _budget(cap_tokens: int) -> wb_budget.WorldbookBudget:
        return wb_budget.WorldbookBudget(
            model_group="default",
            context_length=32768,
            context_length_set=False,
            output_reserve=wb_budget.OUTPUT_RESERVE_TOKENS,
            percent=25,
            cap_tokens=cap_tokens,
            base=32768 - wb_budget.OUTPUT_RESERVE_TOKENS,
            budget=7168,
            factor=1.0,
            factor_source="name",
            factor_family="",
            estimator="tiktoken",
        )

    def test_the_cap_follows_the_token_cap(self) -> None:
        assert wb_budget.ignore_budget_cap(self._budget(4800)) == 4800

    def test_no_token_cap_means_the_hard_cap(self) -> None:
        assert wb_budget.ignore_budget_cap(self._budget(0)) == wb_budget.HARD_CAP_TOKENS


class TestBudgetCheck:
    """书详情与导入报告的预算体检与运行时同一个判据：宿主条目上的「忽略预算」算进得来"""

    @staticmethod
    def _row(*, trusted: bool, ignore: bool) -> SimpleNamespace:
        return SimpleNamespace(
            id=1,
            book_id=1,
            uid=1,
            comment="常驻",
            content="字" * 50,
            constant=True,
            enabled=True,
            status="active",
            order=100,
            position=Position.BEFORE_HISTORY.value,
            depth=4,
            trusted=trusted,
            ignore_budget=ignore,
        )

    @staticmethod
    def _check(row: SimpleNamespace, exempt_cap: int) -> budget_check.BudgetCheck:
        return budget_check.check_constants(
            [row],
            SimpleNamespace(id=1, budget_tokens=0),
            budget=10,
            max_entries=32,
            count=len,
            exempt_cap=exempt_cap,
        )

    def test_a_trusted_exempt_constant_fits(self) -> None:
        check = self._check(self._row(trusted=True, ignore=True), exempt_cap=1000)

        assert (check.fit_count, check.dropped) == (1, ())

    def test_a_tenant_flag_or_a_zero_cap_does_not_exempt(self) -> None:
        for row, cap in (
            (self._row(trusted=False, ignore=True), 1000),
            (self._row(trusted=True, ignore=True), 0),
            (self._row(trusted=True, ignore=False), 1000),
        ):
            check = self._check(row, exempt_cap=cap)

            assert check.fit_count == 0
            assert [item.reason for item in check.dropped] == ["global_tokens"]

    def test_the_import_check_follows_whether_the_entries_will_be_trusted(self) -> None:
        columns = [
            ("1", {"uid": 1, "comment": "常驻", "content": "字" * 50, "constant": True, "enabled": True, "order": 100, "ignore_budget": True})
        ]
        budget = replace(TestTheCap._budget(0), budget=1)

        trusted = budget_check.check_import(columns, budget=budget, max_entries=32, trusted=True)
        untrusted = budget_check.check_import(columns, budget=budget, max_entries=32)

        assert trusted is not None and untrusted is not None
        assert (trusted.fit_count, untrusted.fit_count) == (1, 0)


class TestScanNames:
    @staticmethod
    def _window() -> list[WindowMessage]:
        return [
            WindowMessage(id=1, sender_id="u1", platform_userid="u1", content_text="大家好", speaker="龙骑士"),
            WindowMessage(id=2, sender_id="-1", platform_userid="", content_text="你好呀", speaker="星野"),
            WindowMessage(id=3, sender_id="u2", platform_userid="u2", content_text="没有昵称的人"),
        ]

    def test_names_are_prefixed_when_asked(self) -> None:
        assert scan_text(self._window(), 3, include_names=True) == "龙骑士: 大家好\n星野: 你好呀\n没有昵称的人"

    def test_names_are_left_out_by_default(self) -> None:
        assert scan_text(self._window(), 3) == "大家好\n你好呀\n没有昵称的人"

    def test_the_book_flag_reaches_the_entry(self) -> None:
        book = book_to_match(SimpleNamespace(id=7, scope_kind=ScopeKind.DOMAIN.value, scan_names=True))
        row = SimpleNamespace(id=1, book_id=7, keys=["星野"], content="正文", trusted=True, ignore_budget=True)

        entry = entry_to_match(row, book)

        assert entry.scan_names is True
        assert entry.ignore_budget is True

    def test_an_old_row_without_the_columns_reads_as_off(self) -> None:
        book = book_to_match(SimpleNamespace(id=7, scope_kind=ScopeKind.DOMAIN.value))
        entry = entry_to_match(SimpleNamespace(id=1, book_id=7, keys=["星野"], content="正文"), book)

        assert (entry.scan_names, entry.ignore_budget) == (False, False)

    def test_a_keyword_on_the_persona_name_hits_only_with_names(self) -> None:
        entry = replace(_entry(1, size=1), constant=False, keys=("星野",))
        runner = RegexRunner()

        assert entry_hit(entry, self._window(), runner) is None
        assert entry_hit(replace(entry, scan_names=True), self._window(), runner) == HitReason(kind="key", key="星野")
