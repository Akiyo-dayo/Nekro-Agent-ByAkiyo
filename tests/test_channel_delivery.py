"""确定发不出去的频道不再发失败回执

一份登记表 + 两处接线：

- 适配器在发送时把「实例离线」「Bot 已不在群里」登记下来；
- 消息服务发现频道被登记后，不再补发那条注定同样失败的失败回执，
  并且不占掉回执的冷却窗口。

回执那几条真跑 `_run_chat_agent_task`，替身只装在 DB / 适配器 / 广播 / `run_agent` 这一圈边界上。
"""

from types import SimpleNamespace
from typing import Any, List

import pytest
from nonebot.adapters.onebot.v11.exception import ActionFailed

# 复用既有的运行期替身，不另造一套
from test_agent_mutation_survivors import _MessageServiceHarness

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendSegment,
    PlatformSendSegmentType,
)
from nekro_agent.adapters.onebot_v11 import adapter as adapter_module
from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter
from nekro_agent.adapters.onebot_v11.core.bot import BotInstanceOfflineError
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.agent.failure_feedback import NOTICE_ROUND_STALLED, failure_notice_throttle
from nekro_agent.services.agent.run_agent import AgentRoundStalledError
from nekro_agent.services.channel_delivery import (
    DELIVERY_BLOCK_TTL_SECONDS,
    ChannelDeliveryBlocks,
    UndeliverableReason,
    channel_delivery_blocks,
)

CHAT_KEY = "onebot_v11-inst1-group_500001"


@pytest.fixture(autouse=True)
def _clean_registries():
    channel_delivery_blocks.reset()
    failure_notice_throttle.reset()
    yield
    channel_delivery_blocks.reset()
    failure_notice_throttle.reset()


class _FakeBot:
    """发送时按预设抛异常或返回成功"""

    self_id = "10001"

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: List[dict] = []

    async def send_group_msg(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"message_id": 1}


def _request() -> PlatformSendRequest:
    return PlatformSendRequest(
        chat_key=CHAT_KEY,
        segments=[PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="你好")],
    )


class _FakeChannel:
    chat_type = ChatType.GROUP
    channel_id = "group_500001"
    chat_key = CHAT_KEY
    adapter = SimpleNamespace(config=SimpleNamespace(SESSION_ENABLE_AT=True))


async def _install_adapter_doubles(monkeypatch: pytest.MonkeyPatch, *, bot: _FakeBot | None, error: BaseException | None) -> None:
    if error is None:
        assert bot is not None

        async def _get_bot(_chat_key: str) -> _FakeBot:
            return bot

        monkeypatch.setattr(adapter_module, "get_bot_by_chat_key", _get_bot)
    else:

        async def _raise(_chat_key: str) -> _FakeBot:
            raise error

        monkeypatch.setattr(adapter_module, "get_bot_by_chat_key", _raise)

    async def _get_channel(**_kwargs: Any) -> _FakeChannel:
        return _FakeChannel()

    monkeypatch.setattr(DBChatChannel, "get_channel", _get_channel)


def _action_failed(wording: str) -> ActionFailed:
    return ActionFailed(status="failed", retcode=100, data=None, message="", wording=wording)


# ---------------------------------------------------------------------------
# 登记表本身
# ---------------------------------------------------------------------------


def test_mark_get_and_clear() -> None:
    blocks = ChannelDeliveryBlocks()

    blocks.mark(CHAT_KEY, UndeliverableReason.BOT_NOT_IN_GROUP, "已被移出该群", now=100.0)

    assert blocks.get(CHAT_KEY, now=100.0) is UndeliverableReason.BOT_NOT_IN_GROUP
    blocks.clear(CHAT_KEY)
    assert blocks.get(CHAT_KEY, now=100.0) is None


def test_expired_mark_is_removed() -> None:
    blocks = ChannelDeliveryBlocks()
    blocks.mark(CHAT_KEY, UndeliverableReason.INSTANCE_OFFLINE, now=100.0)

    assert blocks.get(CHAT_KEY, now=100.0 + DELIVERY_BLOCK_TTL_SECONDS) is None
    assert CHAT_KEY not in blocks._blocks  # noqa: SLF001 - 这里要钉的正是「顺手删掉」


def test_mark_prunes_expired_entries_when_over_the_cap() -> None:
    blocks = ChannelDeliveryBlocks()
    for index in range(1025):
        blocks.mark(f"chat-{index}", UndeliverableReason.INSTANCE_OFFLINE, now=0.0)

    blocks.mark(CHAT_KEY, UndeliverableReason.BOT_NOT_IN_GROUP, now=DELIVERY_BLOCK_TTL_SECONDS + 1.0)

    assert len(blocks._blocks) == 1  # noqa: SLF001
    assert blocks.get(CHAT_KEY, now=DELIVERY_BLOCK_TTL_SECONDS + 1.0) is UndeliverableReason.BOT_NOT_IN_GROUP


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------


def test_bot_instance_offline_error_is_an_undeliverable_error() -> None:
    from nekro_agent.services.channel_delivery import ChannelUndeliverableError

    error = BotInstanceOfflineError("inst1")

    assert isinstance(error, ChannelUndeliverableError)
    assert isinstance(error, RuntimeError)
    assert error.undeliverable_reason is UndeliverableReason.INSTANCE_OFFLINE


# ---------------------------------------------------------------------------
# 适配器 forward_message
# ---------------------------------------------------------------------------


async def test_not_in_group_failure_is_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _FakeBot(
        _action_failed(
            "send group message rejected: result=110 err=发送失败，你已被移出该群，请重新加群。",
        ),
    )
    await _install_adapter_doubles(monkeypatch, bot=bot, error=None)

    with pytest.raises(ActionFailed):
        await OnebotV11Adapter().forward_message(_request())

    assert channel_delivery_blocks.get(CHAT_KEY) is UndeliverableReason.BOT_NOT_IN_GROUP


async def test_other_action_failure_is_not_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _FakeBot(_action_failed("send group message rejected: result=120 err=发送失败"))
    await _install_adapter_doubles(monkeypatch, bot=bot, error=None)

    with pytest.raises(ActionFailed):
        await OnebotV11Adapter().forward_message(_request())

    assert channel_delivery_blocks.get(CHAT_KEY) is None


async def test_offline_instance_is_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    await _install_adapter_doubles(monkeypatch, bot=None, error=BotInstanceOfflineError("inst1"))

    with pytest.raises(BotInstanceOfflineError):
        await OnebotV11Adapter().forward_message(_request())

    assert channel_delivery_blocks.get(CHAT_KEY) is UndeliverableReason.INSTANCE_OFFLINE


async def test_success_clears_an_existing_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _FakeBot()
    await _install_adapter_doubles(monkeypatch, bot=bot, error=None)
    channel_delivery_blocks.mark(CHAT_KEY, UndeliverableReason.BOT_NOT_IN_GROUP)

    await OnebotV11Adapter().forward_message(_request())

    assert channel_delivery_blocks.get(CHAT_KEY) is None


# ---------------------------------------------------------------------------
# 失败回执：真跑 _run_chat_agent_task
# ---------------------------------------------------------------------------


async def test_offline_instance_failure_sends_no_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _MessageServiceHarness()
    harness.install(monkeypatch, raises=BotInstanceOfflineError("inst1"))

    await harness.run()

    assert harness.sent == []
    # Agent 在发送之前就失败了，登记由异常分支补上
    assert channel_delivery_blocks.get(harness.chat_key) is UndeliverableReason.INSTANCE_OFFLINE


async def test_registered_bot_not_in_group_suppresses_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _MessageServiceHarness()
    harness.install(
        monkeypatch,
        raises=AgentRoundStalledError("无进展", signature="ERROR|boom", repeats=2),
    )
    channel_delivery_blocks.mark(harness.chat_key, UndeliverableReason.BOT_NOT_IN_GROUP)

    await harness.run()

    assert harness.sent == []


async def test_same_failure_without_a_block_still_sends_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """对照组：没有登记时照常发一条，证明上一条压下去的是登记而不是别的什么"""
    harness = _MessageServiceHarness()
    harness.install(
        monkeypatch,
        raises=AgentRoundStalledError("无进展", signature="ERROR|boom", repeats=2),
    )

    await harness.run()

    assert harness.sent == [(harness.chat_key, NOTICE_ROUND_STALLED)]


async def test_suppressed_notice_does_not_consume_the_cooldown_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """被登记挡下的那次不该占冷却窗口：清掉登记后的第一次真实失败必须照常发出"""
    harness = _MessageServiceHarness(cooldown_seconds=300.0)
    harness.install(
        monkeypatch,
        raises=AgentRoundStalledError("无进展", signature="ERROR|boom", repeats=2),
    )
    channel_delivery_blocks.mark(harness.chat_key, UndeliverableReason.BOT_NOT_IN_GROUP)

    await harness.run()
    channel_delivery_blocks.clear(harness.chat_key)
    await harness.run()

    assert harness.sent == [(harness.chat_key, NOTICE_ROUND_STALLED)]
