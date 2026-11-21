"""Bot 自己离群：频道自动停用；Bot 回来或群里有人说话：自动恢复

三层覆盖：

1. 模型层：自动停用标记的写入 / 恢复 / 与手动操作的分界；
2. 通知与消息匹配器：把真实的 `group_decrease` / `group_increase` / 群消息事件喂给处理函数；
3. 后台详情与发送失败判据：运营能在后台看到原因，适配器认得出「已被移出该群」。
"""

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest
from nonebot.adapters.onebot.v11 import GroupDecreaseNoticeEvent, GroupIncreaseNoticeEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11.tools.bot_membership import (
    is_bot_not_in_group_failure,
    restore_after_bot_returned,
)
from nekro_agent.models.db_chat_channel import (
    CHANNEL_DATA_AUTO_DISABLE_KEY,
    ChannelAutoDisableReason,
    ChannelStatus,
    DBChatChannel,
)
from nekro_agent.routers.chat_channel import _build_chat_channel_detail
from nekro_agent.services.channel_delivery import UndeliverableReason, channel_delivery_blocks

BOT_SELF_ID = 10001
OPERATOR_ID = 20002
GROUP_ID = 500001
OTHER_GROUP_ID = 500002
CHAT_KEY = f"onebot_v11-inst1-group_{GROUP_ID}"
OTHER_CHAT_KEY = f"onebot_v11-inst1-group_{OTHER_GROUP_ID}"


@pytest.fixture(autouse=True)
def _clean_blocks():
    channel_delivery_blocks.reset()
    yield
    channel_delivery_blocks.reset()


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _make_channel(
    *,
    chat_key: str = CHAT_KEY,
    is_active: bool = True,
    observe_mode: bool = False,
    data: str = "{}",
) -> DBChatChannel:
    channel_id = chat_key.rsplit("-", 1)[-1]
    return await DBChatChannel.create(
        adapter_key="onebot_v11",
        instance_key="inst1",
        channel_id=channel_id,
        chat_key=chat_key,
        channel_type="group",
        is_active=is_active,
        observe_mode=observe_mode,
        data=data,
    )


async def _reload(channel: DBChatChannel) -> DBChatChannel:
    return await DBChatChannel.get(id=channel.id)


def _data_of(channel: DBChatChannel) -> dict[str, Any]:
    return json.loads(channel.data)


# ---------------------------------------------------------------------------
# 1. 模型层
# ---------------------------------------------------------------------------


async def test_active_channel_is_auto_disabled_and_keeps_other_data(db) -> None:
    channel = await _make_channel(data=json.dumps({"custom_channel_name": "测试群"}))

    changed = await channel.auto_disable(
        ChannelAutoDisableReason.BOT_LEFT_GROUP,
        "被移出群（操作者 20002）",
    )

    assert changed is True
    assert channel.is_active is False
    assert channel.observe_mode is False
    info = channel.get_auto_disable_info()
    assert info is not None
    assert info.reason is ChannelAutoDisableReason.BOT_LEFT_GROUP
    assert info.previous_status is ChannelStatus.ACTIVE
    assert "custom_channel_name" in _data_of(channel)

    reloaded = await _reload(channel)
    assert reloaded.is_active is False
    assert reloaded.get_auto_disable_info() is not None


async def test_observe_channel_restores_to_observe(db) -> None:
    channel = await _make_channel(observe_mode=True)
    assert channel.channel_status is ChannelStatus.OBSERVE

    assert await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP) is True
    info = channel.get_auto_disable_info()
    assert info is not None
    assert info.previous_status is ChannelStatus.OBSERVE

    assert await channel.restore_auto_disabled(ChannelAutoDisableReason.BOT_LEFT_GROUP) is True
    assert channel.is_active is True
    assert channel.observe_mode is True
    assert channel.channel_status is ChannelStatus.OBSERVE


async def test_manually_disabled_channel_is_never_marked_or_restored(db) -> None:
    channel = await _make_channel(is_active=False)

    assert await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP) is False
    assert CHANNEL_DATA_AUTO_DISABLE_KEY not in _data_of(channel)
    assert await channel.restore_auto_disabled(ChannelAutoDisableReason.BOT_LEFT_GROUP) is False
    assert channel.is_active is False


async def test_manual_disable_after_auto_disable_clears_the_marker(db) -> None:
    channel = await _make_channel()
    await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP)

    await channel.set_channel_status("disabled")

    assert CHANNEL_DATA_AUTO_DISABLE_KEY not in _data_of(channel)
    assert await channel.restore_auto_disabled(ChannelAutoDisableReason.BOT_LEFT_GROUP) is False
    assert channel.is_active is False


async def test_manual_enable_after_auto_disable_clears_the_marker(db) -> None:
    channel = await _make_channel()
    await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP)

    await channel.set_channel_status("active")

    assert channel.is_active is True
    assert CHANNEL_DATA_AUTO_DISABLE_KEY not in _data_of(channel)


async def test_set_active_also_clears_the_marker(db) -> None:
    channel = await _make_channel()
    await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP)

    await channel.set_active(True)

    assert CHANNEL_DATA_AUTO_DISABLE_KEY not in _data_of(channel)


async def test_other_data_survives_disable_restore_and_marker_clear(db) -> None:
    channel = await _make_channel(data=json.dumps({"custom_channel_name": "测试群"}))

    await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP)
    await channel.restore_auto_disabled(ChannelAutoDisableReason.BOT_LEFT_GROUP)
    await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP)
    await channel.set_channel_status("active")

    assert _data_of(channel)["custom_channel_name"] == "测试群"


async def test_bad_json_does_not_raise_when_reading_the_marker(db) -> None:
    channel = await _make_channel(data="{不是合法 JSON")

    assert channel.get_auto_disable_info() is None


# ---------------------------------------------------------------------------
# 2. 通知匹配器
# ---------------------------------------------------------------------------


def _notice_handler():
    from nekro_agent.adapters.onebot_v11.matchers import notice as notice_module

    handlers = notice_module.notice_matcher.handlers
    assert len(handlers) == 1
    return notice_module, handlers[0].call


@pytest.fixture
def notice_environment(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from nekro_agent.adapters.onebot_v11.matchers import notice as notice_module
    from nekro_agent.adapters.onebot_v11.tools import bot_membership, onebot_util

    async def _resolve_instance_key(_self_id: Any) -> str:
        return "inst1"

    monkeypatch.setattr(notice_module, "resolve_instance_key", _resolve_instance_key)
    monkeypatch.setattr(onebot_util, "resolve_instance_key", _resolve_instance_key)
    monkeypatch.setattr(notice_module, "set_current_instance", lambda *a, **k: None)

    async def _dispatch_allowed(_adapter_key: str, _instance_key: str) -> bool:
        return True

    monkeypatch.setattr(notice_module, "is_inbound_dispatch_allowed", _dispatch_allowed)

    seen = SimpleNamespace(published=[], pushed=[], handled=[])

    async def _publish(**kwargs: Any) -> None:
        seen.published.append(kwargs)

    monkeypatch.setattr(bot_membership.channel_broadcaster, "publish_update", _publish)

    async def _push(**kwargs: Any) -> None:
        seen.pushed.append(kwargs)

    monkeypatch.setattr(notice_module.message_service, "push_system_message", _push)

    async def _handle(event: Any, _bot: Any, _channel: Any) -> None:
        seen.handled.append(event)
        return None

    monkeypatch.setattr(notice_module.notice_manager, "handle", _handle)

    class _FakeAdapter:
        @staticmethod
        def cast(_target: Any) -> None:
            return None

    monkeypatch.setattr(DBChatChannel, "adapter", property(lambda _self: _FakeAdapter()))
    return seen


def _decrease_event(*, sub_type: str, user_id: int) -> GroupDecreaseNoticeEvent:
    return GroupDecreaseNoticeEvent(
        time=0,
        self_id=BOT_SELF_ID,
        post_type="notice",
        notice_type="group_decrease",
        sub_type=sub_type,
        user_id=user_id,
        group_id=GROUP_ID,
        operator_id=OPERATOR_ID,
    )


def _increase_event(*, user_id: int, group_id: int = GROUP_ID) -> GroupIncreaseNoticeEvent:
    return GroupIncreaseNoticeEvent(
        time=0,
        self_id=BOT_SELF_ID,
        post_type="notice",
        notice_type="group_increase",
        sub_type="invite",
        user_id=user_id,
        group_id=group_id,
        operator_id=OPERATOR_ID,
    )


@pytest.mark.parametrize("sub_type", ["kick_me", "leave"])
async def test_bot_self_leave_auto_disables_without_pushing(
    db,
    notice_environment: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    sub_type: str,
) -> None:
    channel = await _make_channel()
    _notice_module, handler = _notice_handler()

    await handler(_=None, event=_decrease_event(sub_type=sub_type, user_id=BOT_SELF_ID), bot=SimpleNamespace(self_id=BOT_SELF_ID))

    reloaded = await _reload(channel)
    assert reloaded.is_active is False
    info = reloaded.get_auto_disable_info()
    assert info is not None
    assert info.reason is ChannelAutoDisableReason.BOT_LEFT_GROUP
    # 群成员退出的系统消息不该推：群里已经发不出任何东西；也不该再走到原来的处理器分发
    assert notice_environment.pushed == []
    assert notice_environment.handled == []
    assert len(notice_environment.published) == 1
    assert notice_environment.published[0]["chat_key"] == CHAT_KEY
    assert notice_environment.published[0]["event_type"] == "updated"


async def test_normal_member_leave_does_not_touch_the_channel(
    db,
    notice_environment: SimpleNamespace,
) -> None:
    channel = await _make_channel()
    _notice_module, handler = _notice_handler()

    await handler(
        _=None,
        event=_decrease_event(sub_type="leave", user_id=OPERATOR_ID),
        bot=SimpleNamespace(self_id=BOT_SELF_ID),
    )

    reloaded = await _reload(channel)
    assert reloaded.is_active is True
    assert reloaded.get_auto_disable_info() is None
    # 普通成员退群照旧走原来的处理器分发
    assert len(notice_environment.handled) == 1
    assert notice_environment.pushed == []


async def test_already_disabled_channel_is_not_marked_on_kick(
    db,
    notice_environment: SimpleNamespace,
) -> None:
    channel = await _make_channel(is_active=False)
    _notice_module, handler = _notice_handler()

    await handler(
        _=None,
        event=_decrease_event(sub_type="kick_me", user_id=BOT_SELF_ID),
        bot=SimpleNamespace(self_id=BOT_SELF_ID),
    )

    reloaded = await _reload(channel)
    assert reloaded.is_active is False
    assert reloaded.get_auto_disable_info() is None
    assert notice_environment.published == []


async def test_bot_rejoin_restores_auto_disabled_channel(
    db,
    notice_environment: SimpleNamespace,
) -> None:
    channel = await _make_channel()
    await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP)
    _notice_module, handler = _notice_handler()

    await handler(
        _=None,
        event=_increase_event(user_id=BOT_SELF_ID),
        bot=SimpleNamespace(self_id=BOT_SELF_ID),
    )

    reloaded = await _reload(channel)
    assert reloaded.is_active is True
    assert reloaded.get_auto_disable_info() is None
    assert len(notice_environment.published) == 1
    # 不 return：入群通知照旧往下走
    assert len(notice_environment.handled) == 1


async def test_bot_rejoin_does_not_restore_manually_disabled_channel(
    db,
    notice_environment: SimpleNamespace,
) -> None:
    channel = await _make_channel(chat_key=OTHER_CHAT_KEY, is_active=False)
    _notice_module, handler = _notice_handler()

    await handler(
        _=None,
        event=_increase_event(user_id=BOT_SELF_ID, group_id=OTHER_GROUP_ID),
        bot=SimpleNamespace(self_id=BOT_SELF_ID),
    )

    reloaded = await _reload(channel)
    assert reloaded.is_active is False
    assert reloaded.get_auto_disable_info() is None


# ---------------------------------------------------------------------------
# 3. restore_after_bot_returned / 后台详情 / 发送失败判据
# ---------------------------------------------------------------------------


async def test_restore_after_bot_returned_clears_delivery_block(db) -> None:
    channel = await _make_channel()
    await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP)
    channel_delivery_blocks.mark(CHAT_KEY, UndeliverableReason.BOT_NOT_IN_GROUP)

    assert await restore_after_bot_returned(channel, "群里有新消息") is True
    assert channel.is_active is True
    assert channel_delivery_blocks.get(CHAT_KEY) is None


async def test_restore_after_bot_returned_does_not_write_for_active_channel(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = await _make_channel(is_active=True)
    saves: List[dict[str, Any]] = []

    async def _fake_save(self: DBChatChannel, **kwargs: Any) -> None:
        saves.append(kwargs)

    monkeypatch.setattr(DBChatChannel, "save", _fake_save)

    assert await restore_after_bot_returned(channel, "群里有新消息") is False
    assert saves == []


async def test_restore_after_bot_returned_does_not_restore_manual_disable(db) -> None:
    channel = await _make_channel(is_active=False)

    assert await restore_after_bot_returned(channel, "群里有新消息") is False
    assert channel.is_active is False


def test_restore_after_bot_returned_is_wired_between_get_channel_and_collect() -> None:
    """AST 契约：恢复必须发生在 collect_message 之前

    `collect_message` 会重新取频道行；停用的频道在它里面连消息都不落库，
    所以恢复只能插在它之前，否则「群里有人说话」永远恢复不了。
    """
    message_module = __import__(
        "nekro_agent.adapters.onebot_v11.matchers.message",
        fromlist=["message"],
    )
    path = Path(inspect.getsourcefile(message_module) or "")
    tree = ast.parse(path.read_text(encoding="utf-8"))

    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_"
        and "on_message" in ast.dump(ast.Module(body=node.decorator_list, type_ignores=[]))
    ]
    assert len(handlers) == 1, "没找到消息匹配器的处理函数"

    # 处理本体在 `_process_message` 里，匹配器只负责把它包进「协议端掉线」的兜底：
    # 先确认匹配器真的委托给了它，再在本体里判顺序
    delegated = {
        node.func.id
        for node in ast.walk(handlers[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_process_message" in delegated, "消息匹配器没有委托给处理本体"
    bodies = [
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process_message"
    ]
    assert len(bodies) == 1, "没找到消息匹配器的处理本体"

    lines: dict[str, int] = {}
    for node in ast.walk(bodies[0]):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        else:
            continue
        if name in {"get_db_chat_channel", "restore_after_bot_returned", "collect_message"}:
            lines.setdefault(name, node.lineno)

    assert "restore_after_bot_returned" in lines
    assert "get_db_chat_channel" in lines
    assert "collect_message" in lines
    assert lines["get_db_chat_channel"] < lines["restore_after_bot_returned"] < lines["collect_message"]


async def test_detail_exposes_auto_disable_only_for_disabled_channels(db) -> None:
    channel = await _make_channel()
    await channel.auto_disable(ChannelAutoDisableReason.BOT_LEFT_GROUP, "被移出群（操作者 20002）")

    detail = await _build_chat_channel_detail(channel)

    assert detail.auto_disable is not None
    assert detail.auto_disable.reason == ChannelAutoDisableReason.BOT_LEFT_GROUP.value
    assert detail.auto_disable.detail == "被移出群（操作者 20002）"
    assert detail.auto_disable.disabled_at > 0


async def test_detail_has_no_auto_disable_for_active_or_manual_disabled(db) -> None:
    active = await _make_channel()
    manual = await _make_channel(chat_key=OTHER_CHAT_KEY, is_active=False)

    assert (await _build_chat_channel_detail(active)).auto_disable is None
    assert (await _build_chat_channel_detail(manual)).auto_disable is None


async def test_detail_hides_a_stale_marker_on_an_active_channel(db) -> None:
    """启用中的频道 data 里残留了标记（手工改库之类）：后台不该显示「已自动停用」"""
    stale = {
        CHANNEL_DATA_AUTO_DISABLE_KEY: {
            "reason": ChannelAutoDisableReason.BOT_LEFT_GROUP.value,
            "detail": "",
            "disabled_at": 1,
            "previous_status": ChannelStatus.ACTIVE.value,
        },
    }
    channel = await _make_channel(data=json.dumps(stale))

    assert channel.get_auto_disable_info() is not None
    assert (await _build_chat_channel_detail(channel)).auto_disable is None


@pytest.mark.parametrize(
    ("wording", "expected"),
    [
        (
            "send group message rejected: result=110 err=发送失败，你已被移出该群，请重新加群。",
            True,
        ),
        ("send group message rejected: result=120 err=发送失败", False),
        ("OIDB error 1013 on 0x8fc_3:", False),
        ("verify identify fail", False),
    ],
)
def test_is_bot_not_in_group_failure(wording: str, expected: bool) -> None:
    error = ActionFailed(status="failed", retcode=100, data=None, message="", wording=wording)

    assert is_bot_not_in_group_failure(error) is expected


def test_self_leave_judgement_matches_bot_only() -> None:
    from nekro_agent.adapters.onebot_v11.tools.bot_membership import is_bot_self_join, is_bot_self_leave

    kick = {
        "notice_type": "group_decrease",
        "sub_type": "kick_me",
        "user_id": OPERATOR_ID,
    }
    leave = {
        "notice_type": "group_decrease",
        "sub_type": "leave",
        "user_id": BOT_SELF_ID,
    }
    member_leave = {
        "notice_type": "group_decrease",
        "sub_type": "leave",
        "user_id": OPERATOR_ID,
    }

    assert is_bot_self_leave(kick, str(BOT_SELF_ID)) is True
    assert is_bot_self_leave(leave, str(BOT_SELF_ID)) is True
    assert is_bot_self_leave(member_leave, str(BOT_SELF_ID)) is False
    assert is_bot_self_join({"notice_type": "group_increase", "user_id": BOT_SELF_ID}, str(BOT_SELF_ID)) is True
    assert is_bot_self_join({"notice_type": "group_increase", "user_id": OPERATOR_ID}, str(BOT_SELF_ID)) is False
