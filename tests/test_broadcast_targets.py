"""广播目标筛选：实例离线与「最近没人说话」两条判据

节日祝福暴露的问题：一类频道所属实例根本没连上（Agent 在取自身信息那一步就抛），
另一类群里 Bot 早已被移出（Agent 白跑一轮，发送被 QQ 拒绝）。两者都不该进入广播。
"""

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.services.broadcast_targets import (
    BroadcastSkipReason,
    get_broadcast_skip_reason,
    has_inbound_message_since,
    is_channel_instance_offline,
)

CHAT_KEY = "onebot_v11-inst1-group_500001"
# 取一个小整数时间戳：判据只做相对换算，不需要真实当前时间
NOW = 2000.0
DAY = 86400


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _make_channel(*, instance_key: str = "inst1", chat_key: str = CHAT_KEY) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key="onebot_v11",
        instance_key=instance_key,
        channel_id="group_500001",
        chat_key=chat_key,
        channel_type="group",
        data="{}",
    )


async def _make_instance(instance_key: str, status: str) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key="onebot_v11",
        instance_key=instance_key,
        display_name=instance_key,
        status=status,
    )


async def _add_message(chat_key: str, *, sender_id: str, send_timestamp: int) -> None:
    await DBChatMessage.create(
        sender_id=sender_id,
        sender_name="发送者",
        sender_nickname="发送者",
        is_tome=0,
        is_recalled=False,
        adapter_key="onebot_v11",
        instance_key="inst1",
        message_id="",
        chat_key=chat_key,
        chat_type="group",
        platform_userid=sender_id,
        content_text="内容",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=send_timestamp,
    )


@pytest.mark.parametrize("status", ["online", "connecting", "pending"])
async def test_connected_and_transitional_statuses_are_not_offline(db, status: str) -> None:
    channel = await _make_channel()
    await _make_instance("inst1", status)

    assert await is_channel_instance_offline(channel) is False


@pytest.mark.parametrize(
    "status",
    ["offline", "unbound", "login_required", "error", " OFFLINE ", "Login_Required"],
)
async def test_known_broken_statuses_are_offline(db, status: str) -> None:
    channel = await _make_channel()
    await _make_instance("inst1", status)

    assert await is_channel_instance_offline(channel) is True


async def test_missing_instance_row_is_not_offline(db) -> None:
    """实例行不存在属于数据异常，不该抢先判成离线；下游发送会给出更准确的报错"""
    channel = await _make_channel()

    assert await is_channel_instance_offline(channel) is False


async def test_empty_instance_key_is_not_offline(db) -> None:
    channel = await _make_channel(instance_key="")

    assert await is_channel_instance_offline(channel) is False


async def test_only_bot_messages_do_not_count_as_inbound(db) -> None:
    await _make_channel()
    # Bot 自己的回复和系统消息都是 sender_id="-1"，广播自己写进去的也是这一种
    await _add_message(CHAT_KEY, sender_id="-1", send_timestamp=int(NOW) - DAY)

    assert await has_inbound_message_since(CHAT_KEY, int(NOW) - 2 * DAY) is False


async def test_recent_human_message_counts_as_inbound(db) -> None:
    await _make_channel()
    await _add_message(CHAT_KEY, sender_id="20002", send_timestamp=int(NOW) - DAY)

    assert await has_inbound_message_since(CHAT_KEY, int(NOW) - 2 * DAY) is True


async def test_human_message_outside_the_window_does_not_count(db) -> None:
    await _make_channel()
    await _add_message(CHAT_KEY, sender_id="20002", send_timestamp=int(NOW) - 40 * DAY)

    assert await has_inbound_message_since(CHAT_KEY, int(NOW) - 30 * DAY) is False


async def test_inactive_days_zero_does_not_check_liveness(db) -> None:
    """0 表示不按活跃度跳过：再久没人说话也照常返回不跳过"""
    channel = await _make_channel()

    assert await get_broadcast_skip_reason(channel, inactive_days=0, now=NOW) is None


async def test_offline_instance_wins_before_liveness(db) -> None:
    channel = await _make_channel()
    await _make_instance("inst1", "offline")

    assert (
        await get_broadcast_skip_reason(channel, inactive_days=30, now=NOW)
        is BroadcastSkipReason.INSTANCE_OFFLINE
    )


async def test_channel_without_recent_inbound_is_skipped(db) -> None:
    channel = await _make_channel()
    await _make_instance("inst1", "online")
    await _add_message(CHAT_KEY, sender_id="20002", send_timestamp=int(NOW) - 40 * DAY)

    assert (
        await get_broadcast_skip_reason(channel, inactive_days=30, now=NOW)
        is BroadcastSkipReason.NO_RECENT_INBOUND
    )


async def test_recent_inbound_channel_is_not_skipped(db) -> None:
    channel = await _make_channel()
    await _make_instance("inst1", "online")
    await _add_message(CHAT_KEY, sender_id="20002", send_timestamp=int(NOW) - DAY)

    assert await get_broadcast_skip_reason(channel, inactive_days=30, now=NOW) is None
