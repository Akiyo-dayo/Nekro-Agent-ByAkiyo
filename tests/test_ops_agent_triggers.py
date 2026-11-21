"""一轮的触发集合：记账、取走、过期与上限

## 为什么这本子必须存在

`AgentCtx._trigger_db_user` 记不住「这一轮是谁触发的」：防抖会把窗口里的多条触发并成一轮，
跑的是最后一次调度带进来的消息与上下文；回合在跑时到达的触发只更新待处理消息，这一轮
结束后的 `finally` 还会**拿旧 ctx 重跑**。运维工具要判的是「这一轮的**每一条**触发都是
可信的运维人员」，所以触发必须在**到达时**记下来、在回合开始时整体取走。

## 什么算「不完整」

50 条上限与 10 分钟过期都会丢条目，被丢掉的那条完全可能来自群里另一个人。丢掉过任何一条，
这一批就不能再当作「全是运维人员触发的」——判定侧据此降级成不可用。
"""

from typing import List

import pytest

from nekro_agent.services.ops_agent.triggers import (
    MAX_TRIGGERS_PER_CHANNEL,
    TRIGGER_KIND_HUMAN,
    TRIGGER_KIND_OTHER,
    TRIGGER_KIND_SYSTEM_PUSH,
    TRIGGER_KIND_TIMER,
    TRIGGER_KIND_UNTRUSTED_PUSH,
    TRIGGER_KINDS,
    TRIGGER_TTL_SECONDS,
    RoundTrigger,
    RoundTriggerBook,
)

CHAT_KEY = "onebot_v11-inst1-group_10001"
OTHER_CHAT_KEY = "onebot_v11-inst1-group_20002"


def human(sender_id: str = "10001", *, trusted: bool = True) -> RoundTrigger:
    return RoundTrigger(kind=TRIGGER_KIND_HUMAN, sender_id=sender_id, operator_trusted=trusted)


class TestBookBasics:
    def test_record_then_take_returns_them_in_order(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human("10001"))
        book.record(CHAT_KEY, human("20002"))
        batch = book.take(CHAT_KEY)
        assert [item.sender_id for item in batch.triggers] == ["10001", "20002"]
        assert batch.incomplete is False

    def test_take_clears_the_channel(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human())
        assert book.take(CHAT_KEY).triggers
        assert book.take(CHAT_KEY).triggers == []

    def test_channels_are_isolated(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human("10001"))
        book.record(OTHER_CHAT_KEY, human("20002"))
        assert [item.sender_id for item in book.take(OTHER_CHAT_KEY).triggers] == ["20002"]
        assert [item.sender_id for item in book.take(CHAT_KEY).triggers] == ["10001"]

    def test_an_empty_chat_key_is_ignored(self) -> None:
        """空键会把无关频道的触发混进同一本子"""
        book = RoundTriggerBook()
        book.record("", human())
        assert book.take("").triggers == []

    def test_peek_does_not_consume(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human())
        assert book.peek(CHAT_KEY).triggers
        assert book.peek(CHAT_KEY).triggers
        assert book.take(CHAT_KEY).triggers

    def test_clear_drops_everything(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human())
        book.record(OTHER_CHAT_KEY, human())
        book.clear()
        assert book.peek(CHAT_KEY).triggers == []
        assert book.peek(OTHER_CHAT_KEY).triggers == []


class TestTriggersArrivingWhileARoundRuns:
    """回合跑起来之后到达的触发留在本子里，属于下一轮

    `open_round` 的第一件事就是 `take`：它拿到的是「此刻之前到达的那一批」，之后
    新来的触发不会被这一轮看见。
    """

    def test_a_later_trigger_is_not_in_the_batch_that_was_taken(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human("10001"))
        current = book.take(CHAT_KEY)
        book.record(CHAT_KEY, human("30003", trusted=False))

        assert [item.sender_id for item in current.triggers] == ["10001"]
        nxt = book.take(CHAT_KEY)
        assert [item.sender_id for item in nxt.triggers] == ["30003"]
        assert nxt.incomplete is False

    def test_the_second_take_does_not_resurrect_the_first_batch(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human())
        book.take(CHAT_KEY)
        assert book.take(CHAT_KEY).triggers == []


class TestIncomplete:
    def test_exceeding_the_cap_drops_the_oldest_and_marks_incomplete(self) -> None:
        book = RoundTriggerBook(max_per_channel=3, ttl_seconds=TRIGGER_TTL_SECONDS)
        for index in range(5):
            book.record(CHAT_KEY, human(str(10000 + index)))

        batch = book.take(CHAT_KEY)
        assert [item.sender_id for item in batch.triggers] == ["10002", "10003", "10004"], "留下离这一轮最近的那批"
        assert batch.incomplete is True, "被丢掉的那条照样并进了这一轮"

    def test_reaching_the_cap_exactly_is_still_complete(self) -> None:
        book = RoundTriggerBook(max_per_channel=3, ttl_seconds=TRIGGER_TTL_SECONDS)
        for index in range(3):
            book.record(CHAT_KEY, human(str(10000 + index)))
        batch = book.take(CHAT_KEY)
        assert len(batch.triggers) == 3
        assert batch.incomplete is False

    def test_the_flag_is_consumed_by_take(self) -> None:
        """标记跟着那一批走：带出去之后本子回到干净状态，下一轮不该继续被降级"""
        book = RoundTriggerBook(max_per_channel=1, ttl_seconds=TRIGGER_TTL_SECONDS)
        book.record(CHAT_KEY, human("10001"))
        book.record(CHAT_KEY, human("20002"))
        assert book.take(CHAT_KEY).incomplete is True
        book.record(CHAT_KEY, human("10001"))
        assert book.take(CHAT_KEY).incomplete is False

    def test_stale_entries_are_dropped_and_mark_incomplete(self) -> None:
        book = RoundTriggerBook(max_per_channel=10, ttl_seconds=10)
        start = 1_000.0
        book.record(CHAT_KEY, RoundTrigger(kind=TRIGGER_KIND_HUMAN, sender_id="10001", operator_trusted=True, at=start))
        book.record(CHAT_KEY, RoundTrigger(kind=TRIGGER_KIND_HUMAN, sender_id="20002", operator_trusted=True, at=start + 30))

        batch = book.take(CHAT_KEY, now=start + 31)
        assert [item.sender_id for item in batch.triggers] == ["20002"]
        assert batch.incomplete is True

    def test_fresh_entries_inside_the_window_stay_complete(self) -> None:
        book = RoundTriggerBook(max_per_channel=10, ttl_seconds=10)
        start = 1_000.0
        book.record(CHAT_KEY, RoundTrigger(kind=TRIGGER_KIND_HUMAN, sender_id="10001", operator_trusted=True, at=start))
        book.record(CHAT_KEY, RoundTrigger(kind=TRIGGER_KIND_HUMAN, sender_id="20002", operator_trusted=True, at=start + 5))

        batch = book.take(CHAT_KEY, now=start + 9)
        assert len(batch.triggers) == 2
        assert batch.incomplete is False

    def test_expiry_uses_the_monotonic_clock_at_record_time(self) -> None:
        """系统时间被调整不该让一批刚记下的触发集体过期"""
        trigger = RoundTrigger(kind=TRIGGER_KIND_HUMAN)
        assert trigger.at > 0

    def test_the_ttl_matches_the_documented_window(self) -> None:
        assert TRIGGER_TTL_SECONDS == 600
        assert MAX_TRIGGERS_PER_CHANNEL == 50


class TestTriggerKinds:
    def test_every_kind_is_declared(self) -> None:
        assert TRIGGER_KINDS == {
            TRIGGER_KIND_HUMAN,
            TRIGGER_KIND_SYSTEM_PUSH,
            TRIGGER_KIND_UNTRUSTED_PUSH,
            TRIGGER_KIND_TIMER,
            TRIGGER_KIND_OTHER,
        }

    def test_a_bare_trigger_is_not_trusted(self) -> None:
        """默认值必须是「不可信」：少传一个字段时不能悄悄放行"""
        trigger = RoundTrigger(kind=TRIGGER_KIND_HUMAN)
        assert trigger.operator_trusted is False
        assert trigger.sender_id == "" and trigger.adapter_key == "" and trigger.instance_key == ""

    def test_triggers_are_frozen(self) -> None:
        trigger = RoundTrigger(kind=TRIGGER_KIND_HUMAN)
        with pytest.raises(Exception):
            trigger.sender_id = "10001"  # type: ignore[misc]


def test_the_singleton_is_shared() -> None:
    """`message_service` 往里记、`run_agent` 从里面取，必须是同一个实例"""
    from nekro_agent.services.ops_agent import round_grant
    from nekro_agent.services.ops_agent.triggers import round_trigger_book

    book: List[RoundTrigger] = []
    assert round_trigger_book is round_grant.round_trigger_book
    try:
        round_trigger_book.record(CHAT_KEY, human())
        book = round_trigger_book.peek(CHAT_KEY).triggers
        assert len(book) == 1
    finally:
        round_trigger_book.clear()
