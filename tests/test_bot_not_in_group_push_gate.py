"""宿主替插件做的推送，跳过已被标成「Bot 不在群」的频道

测试部署上的现场：节日祝福定时器到点后，挨个往启用频道推 `trigger_agent=True` 的系统消息。
出发送错误的频道里，绝大多数早已被定期核对标成了 `bot_membership.status=not_in_group`——
标记打上了，推送却不看它，于是每个这样的频道都要开一轮回合、调一次模型，最后被 QQ 以
「你已被移出该群」拒绝，还跟着一串「迭代无进展」。

这份测试钉四个入口：

1. 判据本身（`DBChatChannel.is_bot_marked_not_in_group` 与按 `chat_key` 的查询版）；
2. 广播目标筛选（节日祝福走它），以及真跑 `_festival_callback` 的接线；
3. `schedule_agent_task` 的闸门：定时提醒、系统推送、外部内容推送、插件自己调度的回合
   都从这里开，人类消息不受影响；
4. 以插件身份推送 / 发送的方法：推送不开回合，发送不发出去。

每一组都带对照：同一个频道标记改回「在群」或没有标记时，行为必须与加这道闸之前一样。
"""

import json
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config as core_config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import (
    CHANNEL_DATA_BOT_MEMBERSHIP_KEY,
    BotMembershipSource,
    BotMembershipStatus,
    DBChatChannel,
)
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.services.broadcast_targets import get_broadcast_skip_reason
from nekro_agent.services.message_service import message_service
from nekro_agent.services.ops_agent.triggers import (
    TRIGGER_KIND_HUMAN,
    TRIGGER_KIND_SYSTEM_PUSH,
    TRIGGER_KIND_TIMER,
    TRIGGER_KIND_UNTRUSTED_PUSH,
    RoundTrigger,
)
from nekro_agent.services.plugin import scope as scope_mod
from nekro_agent.services.plugin.base import NekroPlugin

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
GONE = f"{ADAPTER}-{INSTANCE}-group_700001"  # 定期核对标成了「Bot 不在群」
HERE = f"{ADAPTER}-{INSTANCE}-group_700002"  # 标记是「在群」
PLAIN = f"{ADAPTER}-{INSTANCE}-group_700003"  # 从没核对过，没有标记
DAY = 86400


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def scope_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    """插件作用域覆盖文件改道到临时目录，避免写进开发机的真实数据目录"""
    data_dir = tmp_path / "plugin_data"
    monkeypatch.setattr(scope_mod, "PLUGIN_DATA_DIR", data_dir)
    monkeypatch.setattr(scope_mod, "SCOPE_OVERRIDE_DIR", data_dir / "_overrides")
    scope_mod._scope_override_cache.clear()  # noqa: SLF001
    scope_mod._scoped_config_cache.clear()  # noqa: SLF001
    try:
        yield
    finally:
        scope_mod._scope_override_cache.clear()  # noqa: SLF001
        scope_mod._scoped_config_cache.clear()  # noqa: SLF001


@pytest.fixture
def no_debounce(monkeypatch: pytest.MonkeyPatch):
    """防抖换成空转，断言点放在 `pending_messages` 上（进了它就是要开回合）"""

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return

    monkeypatch.setattr(type(message_service), "_debounce_task", noop)
    for key in (GONE, HERE, PLAIN):
        message_service.pending_messages.pop(key, None)
        message_service.debounce_timers.pop(key, None)
    yield
    for key in (GONE, HERE, PLAIN):
        message_service.pending_messages.pop(key, None)
        message_service.debounce_timers.pop(key, None)


def _membership(status: BotMembershipStatus) -> str:
    return json.dumps(
        {
            CHANNEL_DATA_BOT_MEMBERSHIP_KEY: {
                "status": status.value,
                "checked_at": int(time.time()),
                "source": BotMembershipSource.CHECK.value,
                "account_id": "",
            },
        },
    )


async def _make_channel(chat_key: str, *, data: str = "{}") -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=chat_key.rsplit("-", 1)[-1],
        chat_key=chat_key,
        channel_type=ChatType.GROUP.value,
        data=data,
    )


async def _seed() -> None:
    """一个在线实例，三个频道：标成不在群 / 标成在群 / 没有标记"""
    await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=INSTANCE, display_name=INSTANCE, status="online")
    await _make_channel(GONE, data=_membership(BotMembershipStatus.NOT_IN_GROUP))
    await _make_channel(HERE, data=_membership(BotMembershipStatus.IN_GROUP))
    await _make_channel(PLAIN)


async def _add_human_message(chat_key: str, send_timestamp: int) -> None:
    await DBChatMessage.create(
        sender_id="20002",
        sender_name="群友",
        sender_nickname="群友",
        is_tome=0,
        is_recalled=False,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        message_id="",
        chat_key=chat_key,
        chat_type="group",
        platform_userid="20002",
        content_text="内容",
        content_data="[]",
        raw_cq_code="",
        ext_data="{}",
        send_timestamp=send_timestamp,
    )


def _plugin(module_name: str) -> NekroPlugin:
    return NekroPlugin(
        name="不在群探针",
        module_name=module_name,
        description="测试用插件",
        version="1.0.0",
        author="Probe",
        url="",
    )


def _enable_globally(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setattr(core_config, "PLUGIN_ENABLED", [*core_config.PLUGIN_ENABLED, key])


# ---------------------------------------------------------------------------
# 1. 判据
# ---------------------------------------------------------------------------


class TestMarker:
    async def test_only_not_in_group_counts(self, db) -> None:
        del db
        await _seed()

        gone = await DBChatChannel.get(chat_key=GONE)
        here = await DBChatChannel.get(chat_key=HERE)
        plain = await DBChatChannel.get(chat_key=PLAIN)

        assert gone.is_bot_marked_not_in_group() is True
        assert here.is_bot_marked_not_in_group() is False
        assert plain.is_bot_marked_not_in_group() is False

    async def test_a_broken_marker_is_not_evidence(self, db) -> None:
        """标记写坏了（手工改库、旧版本残留）不能当成「不在群」：那会悄悄停掉一个群的推送"""
        del db
        channel = await _make_channel(GONE, data=json.dumps({CHANNEL_DATA_BOT_MEMBERSHIP_KEY: {"status": "???"}}))

        assert channel.is_bot_marked_not_in_group() is False

    async def test_lookup_by_chat_key(self, db) -> None:
        from nekro_agent.models.db_chat_channel import is_chat_key_marked_bot_not_in_group

        del db
        await _seed()

        assert await is_chat_key_marked_bot_not_in_group(GONE) is True
        assert await is_chat_key_marked_bot_not_in_group(HERE) is False
        assert await is_chat_key_marked_bot_not_in_group(PLAIN) is False
        assert await is_chat_key_marked_bot_not_in_group(f"{ADAPTER}-{INSTANCE}-group_799999") is False
        assert await is_chat_key_marked_bot_not_in_group("") is False

    async def test_lookup_failure_lets_the_push_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """查库出错按「在群」处理：这道判断出故障时，不能连正常频道的推送一起挡掉"""
        from nekro_agent.models.db_chat_channel import is_chat_key_marked_bot_not_in_group

        class _Boom:
            def order_by(self, *_args: Any) -> "_Boom":
                return self

            async def first(self) -> None:
                raise RuntimeError("库抖了一下")

        monkeypatch.setattr(DBChatChannel, "filter", classmethod(lambda cls, **_kwargs: _Boom()))

        assert await is_chat_key_marked_bot_not_in_group(GONE) is False


# ---------------------------------------------------------------------------
# 2. 广播目标筛选与节日祝福
# ---------------------------------------------------------------------------


class TestBroadcastTargets:
    async def test_not_in_group_channel_is_skipped(self, db) -> None:
        del db
        await _seed()
        channel = await DBChatChannel.get(chat_key=GONE)

        reason = await get_broadcast_skip_reason(channel, inactive_days=0)
        assert reason is not None
        assert reason.value == "bot_not_in_group"

    async def test_in_group_and_unchecked_channels_are_not_skipped(self, db) -> None:
        del db
        await _seed()

        for chat_key in (HERE, PLAIN):
            channel = await DBChatChannel.get(chat_key=chat_key)
            assert await get_broadcast_skip_reason(channel, inactive_days=0) is None

    async def test_the_marker_wins_even_when_the_group_was_active(self, db) -> None:
        """群里一天前还有人说话、活跃度判据放行，也照样跳过：标记是核对过的证据，不靠猜"""
        del db
        await _seed()
        await _add_human_message(GONE, int(time.time()) - DAY)
        channel = await DBChatChannel.get(chat_key=GONE)

        reason = await get_broadcast_skip_reason(channel, inactive_days=30)
        assert reason is not None
        assert reason.value == "bot_not_in_group"


@pytest.fixture
def festival(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """真跑 `_festival_callback`：只替换配置、插件推送、邮件入队与 sleep"""
    from plugins.builtin.festival_greeting import handlers
    from plugins.builtin.festival_greeting.plugin import FestivalGreetingConfig

    seen = SimpleNamespace(sent=[], handlers=handlers)

    async def _fake_push(chat_key: str, agent_messages: Any, trigger_agent: bool = False, **kwargs: Any) -> bool:
        del agent_messages, trigger_agent, kwargs
        seen.sent.append(chat_key)
        return True

    async def _fake_sleep(_seconds: float) -> None:
        return None

    async def _fake_enqueue(*_args: Any, **_kwargs: Any) -> None:
        return None

    class _FakeTask:
        def __init__(self, coro: Any) -> None:
            coro.close()

        def add_done_callback(self, callback: Callable[[Any], Any]) -> None:
            callback(self)

        def cancelled(self) -> bool:
            return False

        def exception(self) -> None:
            return None

    config = FestivalGreetingConfig(
        AUTO_GREETING_ENABLED=True,
        BLACKLIST_CHATS=[],
        WHITELIST_CHATS=[],
        SKIP_INACTIVE_DAYS=30,
    )
    monkeypatch.setattr(handlers.plugin, "get_config", lambda _cls: config)
    _enable_globally(monkeypatch, handlers.plugin.key)
    monkeypatch.setattr(handlers.plugin, "push_system_message", _fake_push)
    monkeypatch.setattr(handlers, "enqueue_festival_mail", _fake_enqueue)
    monkeypatch.setattr(handlers, "asyncio", SimpleNamespace(sleep=_fake_sleep, create_task=lambda coro: _FakeTask(coro)))
    return seen


async def test_festival_greeting_skips_not_in_group_channels(db, festival) -> None:
    """现场那条路：三个频道都有人说过话，只有被标成不在群的那个不推"""
    del db
    await _seed()
    now = int(time.time())
    for chat_key in (GONE, HERE, PLAIN):
        await _add_human_message(chat_key, now - DAY)

    await festival.handlers._festival_callback("测试节日", "节日快乐")

    assert festival.sent == [HERE, PLAIN]


# ---------------------------------------------------------------------------
# 3. 调度闸门：宿主推送不开回合，人类消息照旧
# ---------------------------------------------------------------------------


HOST_TRIGGERS = [
    pytest.param(RoundTrigger(kind=TRIGGER_KIND_SYSTEM_PUSH, adapter_key=ADAPTER, instance_key=INSTANCE), id="system_push"),
    pytest.param(
        RoundTrigger(kind=TRIGGER_KIND_UNTRUSTED_PUSH, adapter_key=ADAPTER, instance_key=INSTANCE),
        id="untrusted_push",
    ),
    pytest.param(RoundTrigger(kind=TRIGGER_KIND_TIMER), id="timer"),
    pytest.param(None, id="unspecified"),
]


class TestScheduleGate:
    @pytest.mark.parametrize("trigger", HOST_TRIGGERS)
    async def test_host_triggers_do_not_open_a_round(self, db, no_debounce, trigger: Any) -> None:
        del db, no_debounce
        await _seed()

        await message_service.schedule_agent_task(message=ChatMessage.create_empty(GONE), trigger=trigger)

        assert GONE not in message_service.pending_messages

    @pytest.mark.parametrize("trigger", HOST_TRIGGERS)
    async def test_host_triggers_still_open_rounds_elsewhere(self, db, no_debounce, trigger: Any) -> None:
        """对照：标记是「在群」或没有标记时，调度与加这道闸之前一样"""
        del db, no_debounce
        await _seed()

        for chat_key in (HERE, PLAIN):
            await message_service.schedule_agent_task(message=ChatMessage.create_empty(chat_key), trigger=trigger)
            assert chat_key in message_service.pending_messages

    async def test_human_trigger_is_not_affected(self, db, no_debounce) -> None:
        """人类消息不看这道闸：群里有人说话本身就是 Bot 还在的证据"""
        del db, no_debounce
        await _seed()

        await message_service.schedule_agent_task(
            message=ChatMessage.create_empty(GONE),
            trigger=RoundTrigger(kind=TRIGGER_KIND_HUMAN, sender_id="20002"),
        )

        assert GONE in message_service.pending_messages

    async def test_system_push_is_kept_but_no_round_is_opened(self, db, no_debounce, monkeypatch: pytest.MonkeyPatch) -> None:
        """宿主推送那条路：消息照常落库（记录不丢），只是不开回合"""
        from nekro_agent.schemas.signal import MsgSignal
        from nekro_agent.services.plugin.collector import plugin_collector

        del db, no_debounce
        await _seed()

        async def no_plugins(*_args: Any, **_kwargs: Any) -> MsgSignal:
            return MsgSignal.CONTINUE

        monkeypatch.setattr(plugin_collector, "handle_on_system_message", no_plugins)

        await message_service.push_system_message(chat_key=GONE, agent_messages="定时提醒到了", trigger_agent=True)

        assert await DBChatMessage.filter(chat_key=GONE, sender_name="SYSTEM").count() == 1
        assert GONE not in message_service.pending_messages

    async def test_timer_reminder_keeps_the_mark_and_opens_no_round(
        self,
        db,
        no_debounce,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """测试部署上那条「标成不在群之后还来了入站消息」，其实是每天一次的周期定时提醒

        它走 `push_untrusted_message(trigger_agent=True)`，落的是宿主写的工具结果行
        （`sender_id="-2"`），不是群里有人说话：不能清「Bot 不在群」标记，也不该为它开回合
        （现场那一轮调了两次模型，两次都被 QQ 以「已被移出该群」拒绝）。
        """
        from nekro_agent.schemas.signal import MsgSignal
        from nekro_agent.services.plugin.collector import plugin_collector
        from nekro_agent.tools.untrusted_content import UntrustedToolResult

        del db, no_debounce
        await _seed()

        async def no_plugins(*_args: Any, **_kwargs: Any) -> MsgSignal:
            return MsgSignal.CONTINUE

        monkeypatch.setattr(plugin_collector, "handle_on_system_message", no_plugins)

        await message_service.push_untrusted_message(
            chat_key=GONE,
            result=UntrustedToolResult(kind="reminder", summary="定时提醒到了", body="提醒正文"),
            trigger_agent=True,
        )

        assert list(await DBChatMessage.filter(chat_key=GONE).values_list("sender_id", flat=True)) == ["-2"]
        assert GONE not in message_service.pending_messages
        assert (await DBChatChannel.get(chat_key=GONE)).is_bot_marked_not_in_group() is True


# ---------------------------------------------------------------------------
# 4. 以插件身份推送 / 发送
# ---------------------------------------------------------------------------


class TestPluginPushMethods:
    async def test_push_system_message_is_skipped(self, db, scope_env, monkeypatch: pytest.MonkeyPatch) -> None:
        del db, scope_env
        await _seed()
        plugin = _plugin("not_in_group_push_probe")
        _enable_globally(monkeypatch, plugin.key)

        calls: List[Dict[str, Any]] = []

        async def push(**kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(message_service, "push_system_message", push)

        assert await plugin.push_system_message(GONE, "hi", trigger_agent=True) is False
        assert calls == []
        # 对照：同一个插件往在群的频道推，照常转给下游
        assert await plugin.push_system_message(HERE, "hi", trigger_agent=True) is True
        assert [call["chat_key"] for call in calls] == [HERE]

    async def test_push_untrusted_and_schedule_are_skipped(self, db, scope_env, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.tools.untrusted_content import UntrustedToolResult

        del db, scope_env
        await _seed()
        plugin = _plugin("not_in_group_untrusted_probe")
        _enable_globally(monkeypatch, plugin.key)

        calls: List[str] = []

        async def push_untrusted(**kwargs: Any) -> None:
            calls.append(f"untrusted:{kwargs['chat_key']}")

        async def schedule(**kwargs: Any) -> None:
            calls.append(f"schedule:{kwargs['chat_key']}")

        monkeypatch.setattr(message_service, "push_untrusted_message", push_untrusted)
        monkeypatch.setattr(message_service, "schedule_agent_task", schedule)
        result = UntrustedToolResult(kind="probe", summary="外部内容", body="正文")

        assert await plugin.push_untrusted_message(GONE, result, trigger_agent=True) is False
        assert await plugin.schedule_agent_task(chat_key=GONE) is False
        assert calls == []

        assert await plugin.push_untrusted_message(PLAIN, result, trigger_agent=True) is True
        assert await plugin.schedule_agent_task(chat_key=PLAIN) is True
        assert calls == [f"untrusted:{PLAIN}", f"schedule:{PLAIN}"]

    async def test_send_text_is_not_sent(self, db, scope_env, monkeypatch: pytest.MonkeyPatch) -> None:
        """直接发送不开回合，但同样会被 QQ 拒绝：一样要挡"""
        from nekro_agent.api import message as message_api

        del db, scope_env
        await _seed()
        plugin = _plugin("not_in_group_send_probe")
        _enable_globally(monkeypatch, plugin.key)

        sent: List[str] = []

        async def send_text(chat_key: str, message: str, ctx: AgentCtx, **_kwargs: Any) -> None:
            del message, ctx
            sent.append(chat_key)

        monkeypatch.setattr(message_api, "send_text", send_text)

        assert await plugin.send_text(GONE, "hi", AgentCtx(from_chat_key=GONE, adapter_key=ADAPTER)) is False
        assert sent == []
        assert await plugin.send_text(HERE, "hi", AgentCtx(from_chat_key=HERE, adapter_key=ADAPTER)) is True
        assert sent == [HERE]
