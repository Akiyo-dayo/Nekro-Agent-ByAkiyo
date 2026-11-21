"""节日祝福：推送前按实例在线状态与频道活跃度筛掉死频道

真实现场是内置节日祝福把一条 `trigger_agent=True` 的系统消息推给了库里全部启用频道，
其中一大半是离线实例、Bot 早已被移出的群。每一条都要跑一轮 Agent、被 QQ 拒绝一次，
再补发一条同样发不出去的回执。这份测试真跑 `_festival_callback`，只把配置、推送与
sleep 替换成可控的替身。
"""

import time
from types import SimpleNamespace
from typing import Any, Callable, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config as core_config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from plugins.builtin.festival_greeting import handlers
from plugins.builtin.festival_greeting.plugin import FestivalGreetingConfig

DAY = 86400

A = "onebot_v11-inst1-group_500001"
B = "onebot_v11-inst2-group_500002"
C = "onebot_v11-inst1-group_500003"
D = "onebot_v11-inst1-group_500004"
E = "onebot_v11-inst1-group_500005"
F = "onebot_v11-inst1-group_500006"
G = "onebot_v11-group_500007"
MISSING = "onebot_v11-inst1-group_599999"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _make_instance(instance_key: str, status: str) -> None:
    await DBAdapterInstance.create(
        adapter_key="onebot_v11",
        instance_key=instance_key,
        display_name=instance_key,
        status=status,
    )


async def _make_channel(
    chat_key: str,
    *,
    instance_key: str,
    is_active: bool = True,
) -> DBChatChannel:
    channel_id = chat_key.rsplit("-", 1)[-1]
    return await DBChatChannel.create(
        adapter_key="onebot_v11",
        instance_key=instance_key,
        channel_id=channel_id,
        chat_key=chat_key,
        channel_type="group",
        is_active=is_active,
        data="{}",
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


@pytest.fixture
async def fleet(db) -> SimpleNamespace:
    """在线 / 离线实例各一个，七个频道覆盖「活跃 / 长期沉默 / 只有系统消息 / 黑名单 / 停用 / 无实例」"""
    now = int(time.time())
    await _make_instance("inst1", "online")
    await _make_instance("inst2", "offline")

    await _make_channel(A, instance_key="inst1")
    await _make_channel(B, instance_key="inst2")
    await _make_channel(C, instance_key="inst1")
    await _make_channel(D, instance_key="inst1")
    await _make_channel(E, instance_key="inst1")
    await _make_channel(F, instance_key="inst1", is_active=False)
    await _make_channel(G, instance_key="")

    await _add_message(A, sender_id="20002", send_timestamp=now - DAY)
    await _add_message(B, sender_id="20002", send_timestamp=now - DAY)
    await _add_message(C, sender_id="20002", send_timestamp=now - 40 * DAY)
    # D 里只有 Bot / 系统消息：广播自己写进去的也是这一种
    await _add_message(D, sender_id="-1", send_timestamp=now - DAY)
    await _add_message(E, sender_id="20002", send_timestamp=now - DAY)
    await _add_message(F, sender_id="20002", send_timestamp=now - DAY)
    await _add_message(G, sender_id="20002", send_timestamp=now - DAY)

    return SimpleNamespace(now=now)


@pytest.fixture
def broadcast(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """记录真正推送的 chat_key；`on_push` 可在推送过程中改库（模拟几小时里状态变化）"""
    seen = SimpleNamespace(sent=[], sleeps=0, on_push=None)
    configholder: List[FestivalGreetingConfig] = []

    async def _fake_push(chat_key: str, agent_messages: Any, trigger_agent: bool = False, **kwargs: Any) -> bool:
        del agent_messages, trigger_agent, kwargs
        seen.sent.append(chat_key)
        callback: Callable[[str], Any] | None = seen.on_push
        if callback is not None:
            result = callback(chat_key)
            if result is not None:
                await result
        return True

    async def _fake_sleep(_seconds: float) -> None:
        seen.sleeps += 1

    async def _fake_enqueue(*_args: Any, **_kwargs: Any) -> None:
        """节日邮件入队是另一条路：本文件只测群推送，不让它跑起来"""
        return None

    class _FakeTask:
        """`asyncio.create_task` 的替身：登记回调时立刻回调，任务集合不会积压"""

        def __init__(self, coro: Any) -> None:
            coro.close()

        def add_done_callback(self, callback: Callable[[Any], Any]) -> None:
            callback(self)

        def cancelled(self) -> bool:
            return False

        def exception(self) -> None:
            return None

    def _fake_create_task(coro: Any) -> _FakeTask:
        return _FakeTask(coro)

    def _install(config: FestivalGreetingConfig) -> None:
        configholder.append(config)

        def _get_config(_config_cls: Any) -> FestivalGreetingConfig:
            return configholder[-1]

        monkeypatch.setattr(handlers.plugin, "get_config", _get_config)
        # 推送已经改走插件方法；这里直接替身插件方法，同时确保插件在全局启用，
        # 否则 `_festival_callback` 会在全平台广播入口就整次跳过。
        monkeypatch.setattr(
            core_config,
            "PLUGIN_ENABLED",
            [*core_config.PLUGIN_ENABLED, handlers.plugin.key],
        )
        monkeypatch.setattr(handlers.plugin, "push_system_message", _fake_push)
        monkeypatch.setattr(handlers, "enqueue_festival_mail", _fake_enqueue)
        # 只替换 handlers 模块里的 asyncio 名字，不动全局 asyncio.sleep / create_task
        monkeypatch.setattr(
            handlers,
            "asyncio",
            SimpleNamespace(sleep=_fake_sleep, create_task=_fake_create_task),
        )

    seen.install = _install
    return seen


def _config(**overrides: Any) -> FestivalGreetingConfig:
    base = {
        "AUTO_GREETING_ENABLED": True,
        "BLACKLIST_CHATS": [E],
        "WHITELIST_CHATS": [],
        "SKIP_INACTIVE_DAYS": 30,
    }
    base.update(overrides)
    return FestivalGreetingConfig(**base)


async def test_blacklist_mode_skips_offline_and_silent_channels(fleet, broadcast) -> None:
    del fleet
    broadcast.install(_config())

    await handlers._festival_callback("立冬", "立冬快乐")

    # B 的实例离线；C 近 40 天没人说话；D 只有系统消息；E 在黑名单；F 已停用
    assert broadcast.sent == [A, G]


async def test_zero_inactive_days_only_skips_offline_instances(fleet, broadcast) -> None:
    del fleet
    broadcast.install(_config(SKIP_INACTIVE_DAYS=0))

    await handlers._festival_callback("立冬", "立冬快乐")

    assert broadcast.sent == [A, C, D, G]


async def test_whitelist_mode_only_checks_instance_offline(fleet, broadcast) -> None:
    del fleet
    broadcast.install(
        _config(
            AUTO_GREETING_ENABLED=False,
            WHITELIST_CHATS=[B, C, MISSING],
        ),
    )

    await handlers._festival_callback("立冬", "立冬快乐")

    # B 的实例离线，MISSING 不存在；C 虽然沉默 40 天，但白名单是运营点名的，照推
    assert broadcast.sent == [C]


async def test_channel_disabled_during_the_run_is_not_pushed(fleet, broadcast) -> None:
    """广播会持续几个小时：开头取到的启用状态不能一直沿用"""
    del fleet

    async def _deactivate_next(chat_key: str) -> None:
        if chat_key == A:
            await DBChatChannel.filter(chat_key=G).update(is_active=False)

    broadcast.on_push = _deactivate_next
    broadcast.install(_config())

    await handlers._festival_callback("立冬", "立冬快乐")

    assert broadcast.sent == [A]
    assert broadcast.sleeps == 1
