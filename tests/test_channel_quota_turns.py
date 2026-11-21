"""频道每日额度：回合计数

「一次回复」= 一个跑完的回合，而不是发出去的消息条数：一轮回复常常被拆成三条发出去，
按条数算等于对客户收三次费。判据是「这一轮开始之后，这个频道里有没有新的 Bot 消息」。

三条容易写错的地方在这里各钉一条：

1. 上一轮的消息不能被算进这一轮（用消息 ID 而不是时间戳）；
2. 失败的回合也要记（消息已经发出去了），只有被取消的那一轮不记；
3. 计数与告警不能改变原本的异常——外面正拿着它挑回执文案。
"""

from typing import Any, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.channel_quota import policy as policy_module

ADAPTER = "onebot_v11"
INSTANCE = "turn1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_110001"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    policy_module.invalidate_policy_cache()
    try:
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id="group_110001",
            chat_key=CHAT_KEY,
            channel_type="group",
            data="{}",
        )
        yield
    finally:
        policy_module.invalidate_policy_cache()
        await Tortoise.close_connections()


async def add_message(*, sender_id: str = "10001", sender_name: str = "群友", text: str = "在吗") -> DBChatMessage:
    return await DBChatMessage.create(
        message_id="",
        sender_id=sender_id,
        sender_name=sender_name,
        sender_nickname=sender_name,
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        chat_key=CHAT_KEY,
        chat_type="group",
        platform_userid=sender_id,
        content_text=text,
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=1000,
    )


async def replies_today() -> int:
    row = await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY).order_by("id").first()
    return int(row.replies) if row is not None else 0


async def run_round(monkeypatch: pytest.MonkeyPatch, outcome: Optional[BaseException], *, sent_messages: int = 0) -> None:
    """跑一轮 `run_agent`，按 `sent_messages` 在这次回合里推几条 Bot 消息"""

    async def fake_round(**_kwargs: Any) -> None:
        for index in range(sent_messages):
            await add_message(sender_id="-1", sender_name="Bot", text=f"回复 {index}")
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(run_agent_module, "_run_agent_in_instance_scope", fake_round)
    ctx = _Ctx()
    if outcome is None:
        await run_agent_module.run_agent(CHAT_KEY, ctx=ctx)  # type: ignore[arg-type]
        return
    with pytest.raises(type(outcome)) as raised:
        await run_agent_module.run_agent(CHAT_KEY, ctx=ctx)  # type: ignore[arg-type]
    assert raised.value is outcome


class _Ctx:
    """只提供 `run_agent` 取频道所需的那一层"""

    def __init__(self) -> None:
        self.db_chat_channel = None


class TestRoundCounting:
    """有多少条 Bot 消息都只算一次；没有就不算"""

    async def test_zero_bot_messages_records_nothing(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await add_message()

        await run_round(monkeypatch, None, sent_messages=0)

        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_one_bot_message_records_one_reply(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await add_message()

        await run_round(monkeypatch, None, sent_messages=1)

        assert await replies_today() == 1

    async def test_three_bot_messages_still_record_one_reply(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """一轮被拆成三条发出去：按回合计，不是按消息条数计"""
        await add_message()

        await run_round(monkeypatch, None, sent_messages=3)

        assert await replies_today() == 1

    async def test_two_rounds_record_two_replies(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await add_message()

        await run_round(monkeypatch, None, sent_messages=1)
        await run_round(monkeypatch, None, sent_messages=1)

        assert await replies_today() == 2

    async def test_previous_round_messages_are_not_counted_again(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """此前的 Bot 消息不该让一个什么都没发的回合也算一次"""
        await add_message()
        await add_message(sender_id="-1", sender_name="Bot", text="上一轮的回复")

        await run_round(monkeypatch, None, sent_messages=0)

        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_system_messages_are_not_replies(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """`SYSTEM` 是宿主推进去的提示，不是这一轮的回复"""
        await add_message()

        async def fake_round(**_kwargs: Any) -> None:
            await add_message(sender_id="-1", sender_name="SYSTEM", text="额度已用完")

        monkeypatch.setattr(run_agent_module, "_run_agent_in_instance_scope", fake_round)
        await run_agent_module.run_agent(CHAT_KEY, ctx=_Ctx())  # type: ignore[arg-type]

        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_human_messages_are_not_replies(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        await add_message()

        async def fake_round(**_kwargs: Any) -> None:
            await add_message(sender_id="10002", sender_name="另一个人")

        monkeypatch.setattr(run_agent_module, "_run_agent_in_instance_scope", fake_round)
        await run_agent_module.run_agent(CHAT_KEY, ctx=_Ctx())  # type: ignore[arg-type]

        assert await DBChannelQuotaDaily.all().count() == 0


class TestFailedAndCancelledRounds:
    """失败的回合照记，取消的不记"""

    async def test_failed_round_still_counts_the_sent_messages(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """异常发生在发完消息之后：消息已经在群里了，这一轮就该算"""
        await add_message()
        error = RuntimeError("回合中途炸了")

        await run_round(monkeypatch, error, sent_messages=1)

        assert await replies_today() == 1

    async def test_cancelled_round_is_not_counted(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """被取消的一轮发出去了多少无法确定，宁可不记"""
        await add_message()
        error = run_agent_module.asyncio.CancelledError()

        await run_round(monkeypatch, error, sent_messages=2)

        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_original_exception_is_reraised_unchanged(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """外面靠异常类型挑回执文案；计数路径再抛一个就会把它盖掉"""
        await add_message()
        error = run_agent_module.AgentRoundUnfinishedError("没跑完")

        await run_round(monkeypatch, error, sent_messages=1)

        assert await replies_today() == 1

    async def test_counter_failure_does_not_mask_the_round(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """计数整层挂掉时，回合该成功还是成功、该抛什么还是抛什么"""

        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("计数表不可用")

        monkeypatch.setattr(counter, "record_reply", boom)
        await add_message()

        await run_round(monkeypatch, None, sent_messages=1)

        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_marker_failure_skips_counting_this_round(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """取不到开始标记时这一轮不计：按 0 算会把频道里更早的 Bot 消息也当成这一轮发的，
        替客户多记一次回复。额度是平台方给客户的承诺，查询失败时宁可少记"""
        await add_message()
        await add_message(sender_id="-1", sender_name="Bot", text="更早的回复")

        async def boom(_chat_key: str) -> int:
            raise RuntimeError("查不到")

        monkeypatch.setattr(run_agent_module, "_channel_message_marker", boom)

        await run_round(monkeypatch, None, sent_messages=1)

        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_marker_uses_message_ids_not_timestamps(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """同一条秒级时间戳下的上一轮消息不该被算进来

        时间戳全是 1000（`add_message` 里写死的），如果判据是「时间戳不早于开始时刻」，
        下面那条更早的 Bot 消息就会被算进这一轮。
        """
        await add_message()
        await add_message(sender_id="-1", sender_name="Bot", text="上一轮的回复")

        await run_round(monkeypatch, None, sent_messages=0)

        assert await DBChannelQuotaDaily.all().count() == 0


OTHER_CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_110099"


async def add_message_to(chat_key: str, *, sender_id: str) -> DBChatMessage:
    return await DBChatMessage.create(
        message_id="",
        sender_id=sender_id,
        sender_name="Bot" if sender_id == "-1" else "群友",
        sender_nickname="",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        chat_key=chat_key,
        chat_type="group",
        platform_userid=sender_id,
        content_text="x",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=1000,
    )


class TestRoundMarker:
    """回合开始标记取全表最大消息 ID：只读主键末端，结果与按频道取相同"""

    async def test_the_marker_is_the_table_wide_max_id(self, db) -> None:  # noqa: ARG002
        await add_message()
        newest = await add_message_to(OTHER_CHAT_KEY, sender_id="20001")

        assert await run_agent_module._channel_message_marker(CHAT_KEY) == int(newest.id)  # noqa: SLF001

    async def test_an_empty_table_gives_zero(self, db) -> None:  # noqa: ARG002
        assert await run_agent_module._channel_message_marker(CHAT_KEY) == 0  # noqa: SLF001

    async def test_a_bot_message_in_another_channel_does_not_count_here(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_message()

        async def fake_round(**_kwargs: Any) -> None:
            await add_message_to(OTHER_CHAT_KEY, sender_id="-1")

        monkeypatch.setattr(run_agent_module, "_run_agent_in_instance_scope", fake_round)
        await run_agent_module.run_agent(CHAT_KEY, ctx=_Ctx())  # type: ignore[arg-type]

        assert await replies_today() == 0

    async def test_a_bot_message_here_after_older_traffic_elsewhere_counts_once(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_message_to(OTHER_CHAT_KEY, sender_id="20001")
        await add_message()

        async def fake_round(**_kwargs: Any) -> None:
            await add_message_to(OTHER_CHAT_KEY, sender_id="-1")
            await add_message(sender_id="-1", sender_name="Bot", text="回复")

        monkeypatch.setattr(run_agent_module, "_run_agent_in_instance_scope", fake_round)
        await run_agent_module.run_agent(CHAT_KEY, ctx=_Ctx())  # type: ignore[arg-type]

        assert await replies_today() == 1
