"""触发记录簿的本轮快照、两家共用的记账条件、以及来源只查一次

## 为什么这几格要单独测

- `begin_round` / `current_batch` / `end_round` 是 D6 与 SSH 网关共用的唯一入口，
  取早取晚、清不清快照都会让其中一家拿到空批或上一轮的人。
- 记账条件与来源查询次数决定存量部署会不会为一个没人开的功能多查一次库。
- `_human_trigger` 里 `operator_trusted` 与 `source_trusted` 必须来自**同一次**来源判定，
  但又各自跟着不同的名单走。

环境是内存 SQLite，来源判定全部打桩，不碰真连接、不碰真网络。
"""

from __future__ import annotations

from typing import Any, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.services import message_service as module
from nekro_agent.services.ops_agent.triggers import (
    MAX_TRIGGERS_PER_CHANNEL,
    TRIGGER_KIND_HUMAN,
    TRIGGER_KIND_TIMER,
    RoundTrigger,
    RoundTriggerBook,
    round_trigger_book,
)

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_7"
OWNER = "10001"
OPERATOR = "10002"
OUTSIDER = "20002"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        display_name=INSTANCE,
        status="online",
        enabled=True,
        provider="remote_onebot",
    )
    await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_7",
        chat_key=CHAT_KEY,
        channel_type="group",
        data="{}",
    )
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def clean_book():
    round_trigger_book.clear()
    yield
    round_trigger_book.clear()
    for key in (CHAT_KEY,):
        module.message_service.pending_messages.pop(key, None)
        module.message_service.debounce_timers.pop(key, None)


@pytest.fixture
def switches(monkeypatch: pytest.MonkeyPatch):
    def apply(*, ops: bool, gateway: bool) -> None:
        monkeypatch.setattr(config, "OPS_AGENT_TOOLS_ENABLED", ops)
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", gateway)
        monkeypatch.setattr(config, "OPS_AGENT_OPERATORS", [f"{ADAPTER}:{OPERATOR}"])
        monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", [f"{ADAPTER}:{OWNER}"])
        monkeypatch.setattr(config, "SSH_GATEWAY_USER_ACCOUNTS", [f"{ADAPTER}:{OPERATOR}"])
        monkeypatch.setattr(config, "SSH_GATEWAY_LEASE_MAX_SECONDS", 600)
        monkeypatch.setattr(config, "AI_CHAT_QUOTA_WHITELIST_USERS", [])

    apply(ops=False, gateway=False)
    return apply


def human(sender: str = OWNER, *, trusted: bool = True) -> RoundTrigger:
    return RoundTrigger(
        kind=TRIGGER_KIND_HUMAN,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        sender_id=sender,
        operator_trusted=trusted,
        source_trusted=trusted,
    )


def human_message(sender_id: str = OWNER) -> ChatMessage:
    return ChatMessage(
        message_id="m1",
        sender_id=sender_id,
        sender_name="tester",
        sender_nickname="tester",
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        platform_userid=sender_id,
        is_tome=1,
        is_recalled=False,
        chat_key=CHAT_KEY,
        chat_type=ChatType.GROUP,
        content_text="在吗",
        content_data=[],
        raw_cq_code="",
        ext_data={},
        send_timestamp=0,
    )


async def channel() -> DBChatChannel:
    row = await DBChatChannel.get_or_none(chat_key=CHAT_KEY)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# 本轮快照
# ---------------------------------------------------------------------------


class TestRoundSnapshot:
    def test_begin_round_takes_the_batch_and_stores_it(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human("10001"))
        book.record(CHAT_KEY, human("10002"))

        batch = book.begin_round(CHAT_KEY)
        assert [item.sender_id for item in batch.triggers] == ["10001", "10002"]
        assert batch.incomplete is False
        assert book.current_batch(CHAT_KEY) is batch
        assert book.peek(CHAT_KEY).triggers == [], "本子已经被整批取走"

    def test_current_batch_is_none_before_the_round_begins(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human())
        assert book.current_batch(CHAT_KEY) is None

    def test_triggers_arriving_after_begin_belong_to_the_next_round(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human("10001"))
        current = book.begin_round(CHAT_KEY)
        book.record(CHAT_KEY, human("30003", trusted=False))

        assert [item.sender_id for item in current.triggers] == ["10001"]
        assert [item.sender_id for item in book.current_batch(CHAT_KEY).triggers] == ["10001"], "快照不跟着新触发变"
        assert [item.sender_id for item in book.take(CHAT_KEY).triggers] == ["30003"]

    def test_end_round_clears_the_snapshot(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human())
        book.begin_round(CHAT_KEY)
        book.end_round(CHAT_KEY)

        assert book.current_batch(CHAT_KEY) is None
        assert book.take(CHAT_KEY).triggers == [], "已经取走的条目不会因为清快照回来"

    def test_clear_drops_snapshots_too(self) -> None:
        book = RoundTriggerBook()
        book.record(CHAT_KEY, human())
        book.begin_round(CHAT_KEY)
        book.clear()
        assert book.current_batch(CHAT_KEY) is None

    def test_expiry_and_the_cap_still_mark_the_batch_incomplete(self) -> None:
        book = RoundTriggerBook(max_per_channel=2, ttl_seconds=10)
        start = 1_000.0
        book.record(CHAT_KEY, RoundTrigger(kind=TRIGGER_KIND_HUMAN, sender_id="1", at=start))
        book.record(CHAT_KEY, RoundTrigger(kind=TRIGGER_KIND_HUMAN, sender_id="2", at=start + 30))
        book.record(CHAT_KEY, RoundTrigger(kind=TRIGGER_KIND_HUMAN, sender_id="3", at=start + 30))

        batch = book.begin_round(CHAT_KEY, now=start + 31)
        assert [item.sender_id for item in batch.triggers] == ["2", "3"]
        assert batch.incomplete is True

    def test_the_limits_match_the_documented_values(self) -> None:
        assert MAX_TRIGGERS_PER_CHANNEL == 50


# ---------------------------------------------------------------------------
# 记账条件
# ---------------------------------------------------------------------------


async def record_with_switches(monkeypatch: pytest.MonkeyPatch) -> List[RoundTrigger]:
    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(type(module.message_service), "_debounce_task", noop)
    await module.message_service.schedule_agent_task(
        chat_key=CHAT_KEY,
        trigger=RoundTrigger(kind=TRIGGER_KIND_TIMER),
    )
    return round_trigger_book.peek(CHAT_KEY).triggers


class TestRecordingCondition:
    async def test_both_switches_off_record_nothing(self, db, switches, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        switches(ops=False, gateway=False)
        assert await record_with_switches(monkeypatch) == []

    async def test_gateway_alone_records(self, db, switches, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        switches(ops=False, gateway=True)
        triggers = await record_with_switches(monkeypatch)
        assert [trigger.kind for trigger in triggers] == [TRIGGER_KIND_TIMER]

    async def test_ops_alone_still_records(self, db, switches, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        switches(ops=True, gateway=False)
        triggers = await record_with_switches(monkeypatch)
        assert [trigger.kind for trigger in triggers] == [TRIGGER_KIND_TIMER]


# ---------------------------------------------------------------------------
# 来源只查一次
# ---------------------------------------------------------------------------


class TestHumanTrigger:
    @pytest.fixture
    def source_spy(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        class _Spy:
            def __init__(self) -> None:
                self.calls: List[Any] = []
                self.trusted = True

            async def __call__(self, adapter_key: str, instance_key: str) -> bool:
                self.calls.append((adapter_key, instance_key))
                return self.trusted

        spy = _Spy()
        monkeypatch.setattr(module, "is_trusted_super_user_source", spy)
        return spy

    async def test_gateway_owner_is_checked_once_and_trusted(self, db, switches, source_spy: Any) -> None:  # noqa: ARG002
        switches(ops=False, gateway=True)
        trigger = await module.message_service._human_trigger(human_message(OWNER), await channel())

        assert source_spy.calls == [(ADAPTER, INSTANCE)]
        assert trigger.source_trusted is True
        assert trigger.operator_trusted is False, "网关的所有者名单不改变 D6 的字段"

    async def test_gateway_whitelisted_user_is_checked_once(self, db, switches, source_spy: Any) -> None:  # noqa: ARG002
        switches(ops=False, gateway=True)
        trigger = await module.message_service._human_trigger(human_message(OPERATOR), await channel())

        assert source_spy.calls == [(ADAPTER, INSTANCE)]
        assert trigger.source_trusted is True

    async def test_an_outsider_is_never_checked(self, db, switches, source_spy: Any) -> None:  # noqa: ARG002
        switches(ops=False, gateway=True)
        trigger = await module.message_service._human_trigger(human_message(OUTSIDER), await channel())

        assert source_spy.calls == []
        assert trigger.source_trusted is False
        assert trigger.operator_trusted is False

    async def test_ops_operator_keeps_the_old_semantics(self, db, switches, source_spy: Any) -> None:  # noqa: ARG002
        switches(ops=True, gateway=False)
        trigger = await module.message_service._human_trigger(human_message(OPERATOR), await channel())

        assert source_spy.calls == [(ADAPTER, INSTANCE)]
        assert trigger.operator_trusted is True
        assert trigger.source_trusted is True

    async def test_ops_operator_with_an_untrusted_source(self, db, switches, source_spy: Any) -> None:  # noqa: ARG002
        switches(ops=True, gateway=False)
        source_spy.trusted = False
        trigger = await module.message_service._human_trigger(human_message(OPERATOR), await channel())

        assert source_spy.calls == [(ADAPTER, INSTANCE)]
        assert trigger.operator_trusted is False
        assert trigger.source_trusted is False

    async def test_both_lists_hitting_still_checks_only_once(
        self,
        db,
        switches,
        source_spy: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        switches(ops=True, gateway=True)
        monkeypatch.setattr(config, "OPS_AGENT_OPERATORS", [f"{ADAPTER}:{OWNER}"])
        trigger = await module.message_service._human_trigger(human_message(OWNER), await channel())

        assert len(source_spy.calls) == 1, "同一个发送者同时命中两边，来源只查一次"
        assert trigger.operator_trusted is True
        assert trigger.source_trusted is True

    async def test_both_switches_off_never_checks_the_source(self, db, switches, source_spy: Any) -> None:  # noqa: ARG002
        switches(ops=False, gateway=False)
        trigger = await module.message_service._human_trigger(human_message(OWNER), await channel())

        assert source_spy.calls == []
        assert (trigger.operator_trusted, trigger.source_trusted) == (False, False)
