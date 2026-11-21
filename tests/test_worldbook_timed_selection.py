"""定时效果与预算截断（纯函数部分）

设计稿的定时效果按「消息条数」计，而窗口最多 50 条、效果最大 200 条，两者的数不出来。
改成由调用方给出「锚点之后几条」与「频道共几条」之后，判据就成了纯函数，边界可以逐条钉：

- `sticky = n`：第 n 条仍是 sticky，第 n + 1 条进 cooldown；
- `cooldown = n`：`sticky + n` 之前都在 cooldown，之后 free；
- `delay = n`：不足 n 条时不命中；
- sticky 期间关键词再命中不刷新锚点（否则一条被反复提到的条目永远 sticky）。

预算那一半同样只测纯函数：入选顺序、三种跳过原因、不截断单条、渲染分组的组内顺序与
按深度分组。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from nekro_agent.services.worldbook.matcher import HitReason
from nekro_agent.services.worldbook.selection import Hit, group_for_render, select_hits
from nekro_agent.services.worldbook.timed_state import (
    TimedGate,
    TimedStateStore,
    apply_timed,
    delay_satisfied,
    entry_gate,
    timed_gate,
    timed_state_store,
)
from nekro_agent.services.worldbook.types import MatchEntry, Position


def make_entry(**extra: object) -> MatchEntry:
    defaults: dict[str, object] = {
        "id": 10,
        "book_id": 1,
        "keys": ("龙",),
        "secondary_keys": (),
        "selective_logic": "and_any",
        "use_regex": False,
        "constant": False,
        "content": "正文",
        "order": 100,
        "position": Position.BEFORE_HISTORY,
        "depth": 4,
        "scan_depth": 4,
        "case_sensitive": False,
        "match_whole_words": False,
        "sticky": 0,
        "cooldown": 0,
        "delay": 0,
        "trusted": False,
        "invalid_keys": frozenset(),
    }
    defaults.update(extra)
    return MatchEntry(**defaults)  # type: ignore[arg-type]


def hit(entry_id: int, **extra: object) -> Hit:
    return Hit(entry=make_entry(id=entry_id, **extra), reason=HitReason(kind="key", key="龙"))


class TestTimedGate:
    def test_no_anchor_is_free(self) -> None:
        assert timed_gate(anchor_message_id=None, msgs_since_anchor=5, sticky=3, cooldown=3) is TimedGate.FREE

    def test_unknown_count_is_free(self) -> None:
        """调用方数不出条数时按 free：这是「从没生效过」的正常状态，不是异常"""
        assert timed_gate(anchor_message_id=7, msgs_since_anchor=None, sticky=3, cooldown=3) is TimedGate.FREE

    def test_boundaries(self) -> None:
        """sticky 第 n 条仍是 sticky；第 n+1 条进 cooldown；cooldown 最后一刻仍是 cooldown"""
        cases = [
            (0, TimedGate.STICKY),
            (1, TimedGate.STICKY),
            (3, TimedGate.STICKY),
            (4, TimedGate.COOLDOWN),
            (6, TimedGate.COOLDOWN),
            (7, TimedGate.FREE),
            (99, TimedGate.FREE),
        ]
        for count, expected in cases:
            actual = timed_gate(anchor_message_id=1, msgs_since_anchor=count, sticky=3, cooldown=3)
            assert actual is expected, f"过了 {count} 条时应为 {expected}，实际 {actual}"

    def test_without_sticky_goes_straight_to_cooldown(self) -> None:
        assert timed_gate(anchor_message_id=1, msgs_since_anchor=1, sticky=0, cooldown=2) is TimedGate.COOLDOWN
        assert timed_gate(anchor_message_id=1, msgs_since_anchor=3, sticky=0, cooldown=2) is TimedGate.FREE

    def test_without_timed_effects_is_always_free(self) -> None:
        for count in (0, 1, 10):
            assert timed_gate(anchor_message_id=1, msgs_since_anchor=count, sticky=0, cooldown=0) is TimedGate.FREE


class TestDelay:
    def test_below_delay_is_not_satisfied(self) -> None:
        assert delay_satisfied(5, 4) is False
        assert delay_satisfied(5, 5) is True
        assert delay_satisfied(5, 6) is True

    def test_zero_delay_is_always_satisfied(self) -> None:
        assert delay_satisfied(0, 0) is True
        assert delay_satisfied(-1, 0) is True


class TestApplyTimed:
    def test_delay_blocks_everything(self) -> None:
        entry = make_entry(delay=10)
        result, anchor = apply_timed(entry, HitReason(kind="key"), TimedGate.FREE, channel_count=3)
        assert result is None
        assert anchor is False

    def test_constant_entry_also_obeys_delay(self) -> None:
        entry = make_entry(constant=True, delay=10)
        result, anchor = apply_timed(entry, HitReason(kind="constant"), TimedGate.FREE, channel_count=3)
        assert result is None
        assert anchor is False

    def test_sticky_gate_keeps_non_constant_entry(self) -> None:
        entry = make_entry(sticky=3)
        result, anchor = apply_timed(entry, None, TimedGate.STICKY, channel_count=99)
        assert result == HitReason(kind="sticky")
        assert anchor is False

    def test_sticky_gate_does_not_refresh_anchor(self) -> None:
        """sticky 期间关键词再命中不刷新锚点：否则被反复提到的条目永远 sticky"""
        entry = make_entry(sticky=3, cooldown=2)
        result, anchor = apply_timed(entry, HitReason(kind="key"), TimedGate.STICKY, channel_count=99)
        assert result is not None
        assert anchor is False

    def test_sticky_gate_ignored_for_constant_entry(self) -> None:
        """常驻条目本来就每轮都在，sticky 对它是多余的"""
        entry = make_entry(constant=True, sticky=5)
        result, _ = apply_timed(entry, HitReason(kind="constant"), TimedGate.STICKY, channel_count=99)
        assert result == HitReason(kind="constant")

    def test_constant_entry_in_sticky_gate_does_not_refresh_anchor(self) -> None:
        """常驻条目每轮都命中；若 sticky 档也给它设新锚点，它就永远进不了 cooldown"""
        entry = make_entry(constant=True, sticky=3, cooldown=2)
        result, anchor = apply_timed(entry, HitReason(kind="constant"), TimedGate.STICKY, channel_count=99)
        assert result == HitReason(kind="constant")
        assert anchor is False

    def test_constant_entry_with_cooldown_cycles(self) -> None:
        """常驻 + cooldown：注入一次、冷却 n 条、再注入，一轮一轮地走下来"""
        entry = make_entry(constant=True, sticky=3, cooldown=2)
        constant = HitReason(kind="constant")
        timeline: list[bool] = []
        anchor_id: int | None = None
        for message_id in range(1, 9):
            since = None if anchor_id is None else message_id - anchor_id
            gate = entry_gate(entry, anchor_message_id=anchor_id, msgs_since_anchor=since)
            result, wants_anchor = apply_timed(entry, constant, gate, channel_count=99)
            timeline.append(result is not None)
            if wants_anchor:
                anchor_id = message_id
        assert timeline == [True, False, False, True, False, False, True, False]

    def test_cooldown_gate_blocks(self) -> None:
        entry = make_entry(cooldown=3)
        result, anchor = apply_timed(entry, HitReason(kind="key"), TimedGate.COOLDOWN, channel_count=99)
        assert result is None
        assert anchor is False

    def test_free_gate_passes_original_hit(self) -> None:
        entry = make_entry()
        original = HitReason(kind="key", key="龙")
        result, anchor = apply_timed(entry, original, TimedGate.FREE, channel_count=99)
        assert result is original
        assert anchor is False

    def test_free_gate_without_hit(self) -> None:
        entry = make_entry()
        result, anchor = apply_timed(entry, None, TimedGate.FREE, channel_count=99)
        assert result is None
        assert anchor is False

    @pytest.mark.parametrize(
        ("extra", "expected"),
        [({"sticky": 3}, True), ({"cooldown": 3}, True), ({}, False)],
    )
    def test_anchor_needed_only_when_effect_configured(self, extra: dict[str, object], expected: bool) -> None:
        entry = make_entry(**extra)
        _, anchor = apply_timed(entry, HitReason(kind="key"), TimedGate.FREE, channel_count=99)
        assert anchor is expected


class TestEntryGate:
    def test_constant_entry_ignores_sticky(self) -> None:
        """常驻条目的 sticky 按 0 算：锚点之后直接进 cooldown，不被 sticky 往后推"""
        entry = make_entry(constant=True, sticky=3, cooldown=2)
        assert entry_gate(entry, anchor_message_id=1, msgs_since_anchor=1) is TimedGate.COOLDOWN
        assert entry_gate(entry, anchor_message_id=1, msgs_since_anchor=2) is TimedGate.COOLDOWN
        assert entry_gate(entry, anchor_message_id=1, msgs_since_anchor=3) is TimedGate.FREE

    def test_keyword_entry_keeps_sticky(self) -> None:
        entry = make_entry(sticky=3, cooldown=2)
        assert entry_gate(entry, anchor_message_id=1, msgs_since_anchor=3) is TimedGate.STICKY
        assert entry_gate(entry, anchor_message_id=1, msgs_since_anchor=4) is TimedGate.COOLDOWN

    def test_no_anchor_is_free(self) -> None:
        entry = make_entry(sticky=3, cooldown=2)
        assert entry_gate(entry, anchor_message_id=None, msgs_since_anchor=None) is TimedGate.FREE


class TestTimedStateStore:
    def test_refreshed_anchor_counts_as_newest(self) -> None:
        """刷新过的锚点要按「最新」算，腾地方时不能被当成最旧的那批丢掉"""
        store = TimedStateStore(max_keys=4)
        for index in range(4):
            store.stage("r", "g1", index, index)
        store.commit("r")
        store.stage("r2", "g1", 0, 50)
        store.commit("r2")
        assert len(store.anchor_keys()) == 4, "刷新已有的锚点不增加条数，不该触发腾地方"
        store.stage("r3", "g1", 99, 99)
        store.commit("r3")
        # 刷新后的顺序是 1、2、3、0；腾地方丢最旧的一半（1、2），留下 3、0，再写 99。
        assert store.anchor_keys() == [("g1", 3), ("g1", 0), ("g1", 99)]
        assert store.get("g1", 0) == 50

    def test_stage_then_commit(self) -> None:
        store = TimedStateStore()
        store.stage("r1", "g1", 7, 100)
        assert store.get("g1", 7) is None, "暂存期间不该可见"
        store.commit("r1")
        assert store.get("g1", 7) == 100

    def test_discard_drops_the_staged_value(self) -> None:
        """被拦、失败、取消的轮次不开始 sticky、也不进 cooldown"""
        store = TimedStateStore()
        store.stage("r1", "g1", 7, 100)
        store.discard("r1")
        store.commit("r1")
        assert store.get("g1", 7) is None

    def test_last_stage_in_a_round_wins(self) -> None:
        """一轮里渲染多次提示词，每次都会算出一个候选锚点，以最后一次为准"""
        store = TimedStateStore()
        store.stage("r1", "g1", 7, 100)
        store.stage("r1", "g1", 7, 204)
        store.commit("r1")
        assert store.get("g1", 7) == 204

    def test_rounds_are_independent(self) -> None:
        store = TimedStateStore()
        store.stage("r1", "g1", 7, 100)
        store.stage("r2", "g1", 8, 200)
        store.commit("r1")
        assert store.get("g1", 7) == 100
        assert store.get("g1", 8) is None
        store.commit("r2")
        assert store.get("g1", 8) == 200

    def test_commit_overwrites_previous_anchor(self) -> None:
        store = TimedStateStore()
        store.stage("r1", "g1", 7, 100)
        store.commit("r1")
        store.stage("r2", "g1", 7, 300)
        store.commit("r2")
        assert store.get("g1", 7) == 300

    def test_key_limit_drops_oldest_half(self) -> None:
        store = TimedStateStore(max_keys=4)
        for index in range(4):
            store.stage("r", "g1", index, index)
        store.commit("r")
        assert len(store.anchor_keys()) == 4

        store.stage("r2", "g1", 99, 99)
        store.commit("r2")
        # 上限 4：先丢最早的一半（2 个），再写入新的那条。
        assert len(store.anchor_keys()) == 3
        assert store.get("g1", 99) == 99
        assert store.get("g1", 0) is None

    def test_round_limit_drops_oldest_half(self) -> None:
        """暂存区同样有上限：进来的轮次码只增不减，不留上限就是内存泄漏"""
        store = TimedStateStore(max_rounds=4)
        for index in range(4):
            store.stage(f"r{index}", "g1", index, index)
        store.stage("r9", "g1", 9, 9)
        store.commit("r0")
        # 最早的一半（r0、r1）已被丢掉，因此 r0 的提交什么都没写进去。
        assert store.get("g1", 0) is None
        store.commit("r9")
        assert store.get("g1", 9) == 9

    def test_clear(self) -> None:
        store = TimedStateStore()
        store.stage("r1", "g1", 7, 100)
        store.commit("r1")
        store.clear()
        assert store.get("g1", 7) is None

    def test_module_singleton_exists(self) -> None:
        """运行时收尾时用的就是这一个实例"""
        assert isinstance(timed_state_store, TimedStateStore)


def _by_len(entry: MatchEntry) -> int:
    """纯函数用例的数法：一字一 token，便于手算（真实数法见 `test_worldbook_tokens.py`）"""
    return len(entry.content)


class TestSelectHits:
    def test_selected_order_is_order_desc_then_id_asc(self) -> None:
        hits = [hit(1, order=100), hit(2, order=300), hit(3, order=100)]
        selection = select_hits(hits, max_tokens=10_000, max_entries=10, book_budgets={}, count=_by_len)
        assert [item.entry.id for item in selection.selected] == [2, 1, 3]

    def test_global_char_budget_skips_whole_entry(self) -> None:
        """放不下就整条跳过，不截断单条：切一半的设定比不注入更糟"""
        hits = [hit(1, content="a" * 6), hit(2, content="b" * 6)]
        selection = select_hits(hits, max_tokens=10, max_entries=10, book_budgets={}, count=_by_len)
        assert [item.entry.id for item in selection.selected] == [1]
        assert [(item.entry.id, reason) for item, reason in selection.dropped] == [(2, "global_tokens")]
        assert selection.total_tokens == 6

    def test_book_budget_is_per_book(self) -> None:
        hits = [
            hit(1, content="a" * 6, book_id=1),
            hit(2, content="b" * 6, book_id=2),
            hit(3, content="c" * 6, book_id=1),
        ]
        selection = select_hits(hits, max_tokens=1000, max_entries=10, book_budgets={1: 10}, count=_by_len)
        assert [item.entry.id for item in selection.selected] == [1, 2]
        assert [(item.entry.id, reason) for item, reason in selection.dropped] == [(3, "book_tokens")]

    def test_zero_book_budget_means_unlimited(self) -> None:
        hits = [hit(1, content="a" * 600, book_id=1)]
        selection = select_hits(hits, max_tokens=1000, max_entries=10, book_budgets={1: 0}, count=_by_len)
        assert len(selection.selected) == 1

    def test_max_entries_skips_the_rest(self) -> None:
        hits = [hit(index, order=100 - index) for index in range(1, 6)]
        selection = select_hits(hits, max_tokens=10_000, max_entries=2, book_budgets={}, count=_by_len)
        assert [item.entry.id for item in selection.selected] == [1, 2]
        assert {reason for _, reason in selection.dropped} == {"max_entries"}
        assert len(selection.dropped) == 3

    def test_max_entries_keeps_a_known_route_alongside(self) -> None:
        """条数上限之后的条目不分析；缓存里已知要分流的，两个原因一起记"""
        hits = [hit(index, order=100 - index) for index in range(1, 5)]
        known = {3: "template_unsupported"}
        selection = select_hits(
            hits,
            max_tokens=10_000,
            max_entries=2,
            book_budgets={},
            count=_by_len,
            known_route=lambda item: known.get(item.entry.id),
        )
        assert [(item.entry.id, reason) for item, reason in selection.dropped] == [
            (3, "max_entries|template_unsupported"),
            (4, "max_entries"),
        ]

    def test_empty_hits(self) -> None:
        selection = select_hits([], max_tokens=100, max_entries=10, book_budgets={}, count=_by_len)
        assert selection.selected == ()
        assert selection.dropped == ()
        assert selection.total_tokens == 0

    def test_total_tokens_counts_selected_only(self) -> None:
        hits = [hit(1, content="abc"), hit(2, content="de")]
        selection = select_hits(hits, max_tokens=100, max_entries=10, book_budgets={}, count=_by_len)
        assert selection.total_tokens == 5

    def test_tokens_are_counted_on_the_prepared_text_and_routed_out_entries_take_no_budget(self) -> None:
        """模板条目按 `prepare` 换过的正文数；被分流的条目不数、不占预算，也不占条数"""
        counted: list[str] = []

        def count(entry: MatchEntry) -> int:
            counted.append(entry.content)
            return len(entry.content)

        def prepare(item: Hit) -> Hit | str:
            if item.entry.id == 1:
                return replace(item, entry=replace(item.entry, content="短"))
            if item.entry.id == 2:
                return "state_only"
            return item

        hits = [
            hit(1, content="模板原文" * 50, order=300),
            hit(2, content="分流出去的长正文" * 50, order=200),
            hit(3, content="普通", order=100),
        ]
        selection = select_hits(hits, max_tokens=3, max_entries=2, book_budgets={}, count=count, prepare=prepare)

        assert [item.entry.id for item in selection.selected] == [1, 3]
        assert selection.selected[0].entry.content == "短"
        assert counted == ["短", "普通"], "原文与被分流的正文都不数"
        assert dict(selection.tokens) == {1: 1, 3: 2}
        assert selection.total_tokens == 3
        assert [(item.entry.id, reason) for item, reason in selection.routed_out] == [(2, "state_only")]
        assert selection.dropped == ()


class TestGroupForRender:
    def test_groups_by_position_and_orders_ascending(self) -> None:
        hits = [
            hit(1, position=Position.BEFORE_HISTORY, order=300),
            hit(2, position=Position.BEFORE_HISTORY, order=100),
            hit(3, position=Position.AFTER_PERSONA, order=200),
            hit(4, position=Position.AT_DEPTH, order=100, depth=2),
            hit(5, position=Position.AT_DEPTH, order=50, depth=2),
        ]
        groups = group_for_render(hits)
        assert [item.entry.id for item in groups.before_history] == [2, 1]
        assert [item.entry.id for item in groups.after_persona] == [3]
        assert [item.entry.id for item in groups.at_depth[2]] == [5, 4]

    def test_same_order_uses_id_ascending(self) -> None:
        hits = [hit(9, order=100), hit(3, order=100)]
        groups = group_for_render(hits)
        assert [item.entry.id for item in groups.before_history] == [3, 9]

    def test_depths_are_separate_keys(self) -> None:
        """不同深度不能混成一个键：它们要插在聊天记录的不同位置"""
        hits = [hit(1, position=Position.AT_DEPTH, depth=0), hit(2, position=Position.AT_DEPTH, depth=3)]
        groups = group_for_render(hits)
        assert set(groups.at_depth) == {0, 3}

    def test_ordered_depths_is_descending(self) -> None:
        """深度大表示插得早（离末尾远），渲染按这个顺序拼"""
        hits = [hit(1, position=Position.AT_DEPTH, depth=1), hit(2, position=Position.AT_DEPTH, depth=5)]
        groups = group_for_render(hits)
        assert groups.ordered_depths() == [5, 1]

    def test_empty_is_reported(self) -> None:
        assert group_for_render([]).empty is True
        assert group_for_render([hit(1)]).empty is False

    def test_replace_on_entry_keeps_grouping(self) -> None:
        """用例里用 `replace` 派生条目（引擎各处也这么做），分组不受影响"""
        entry = replace(make_entry(position=Position.AT_DEPTH), depth=7)
        groups = group_for_render([Hit(entry=entry, reason=HitReason(kind="constant"))])
        assert list(groups.at_depth) == [7]
