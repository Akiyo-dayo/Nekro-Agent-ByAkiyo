"""频道每日额度：告警

四条约束：

1. 每个阈值一天只告警一次，靠一次条件更新决定谁入队（不是先读再比）；
2. 同一个目的地的告警按 10 分钟合并，避免零点前后把运维群刷成瀑布；
3. 平台方收全部告警（正文带租户名），租户只收自己实例那一份（`scoped_only=True`）；
4. 正文里**没有任何聊天内容**——这条告警会经由邮件与 Webhook 离开本进程。
"""

from typing import Any, List, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.services.channel_quota import alerts as alerts_module
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota.policy import SCOPE_GLOBAL
from nekro_agent.services.channel_quota.service import evaluate

ADAPTER = "onebot_v11"
TENANT_INSTANCE = "alert-tenant"
PLATFORM_INSTANCE = "alert-platform"
TENANT_CHAT_KEY = f"{ADAPTER}-{TENANT_INSTANCE}-group_140001"
PLATFORM_CHAT_KEY = f"{ADAPTER}-{PLATFORM_INSTANCE}-group_140002"
TENANT_ID = 41


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    policy_module.invalidate_policy_cache()
    alerts_module.alert_buffer.reset()
    try:
        yield
    finally:
        alerts_module.alert_buffer.reset()
        policy_module.invalidate_policy_cache()
        await Tortoise.close_connections()


async def seed_global(**fields: Any) -> None:
    defaults = {
        "enabled": True,
        "unit": "replies",
        "group_daily_limit": 100,
        "private_daily_limit": 100,
        "over_limit_action": "alert",
    }
    defaults.update(fields)
    await DBChannelQuotaPolicy.create(scope=SCOPE_GLOBAL, **defaults)
    policy_module.invalidate_policy_cache()


async def seed_channel(
    chat_key: str = TENANT_CHAT_KEY,
    instance_key: str = TENANT_INSTANCE,
    *,
    channel_name: str = "测试群",
) -> DBChatChannel:
    await DBAdapterInstance.get_or_create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        defaults={"display_name": instance_key, "status": "online"},
    )
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=chat_key.rsplit("-", 1)[-1],
        chat_key=chat_key,
        channel_type="group",
        channel_name=channel_name,
        data="{}",
    )


async def seed_tenant() -> None:
    await DBTenant.create(id=TENANT_ID, name="acme", display_name="Acme 客户")
    await DBAdapterInstance.filter(adapter_key=ADAPTER, instance_key=TENANT_INSTANCE).update(tenant_id=TENANT_ID)


async def set_usage(chat_key: str, replies: int) -> None:
    await DBChannelQuotaDaily.update_or_create(
        chat_key=chat_key,
        day=counter.quota_day(),
        defaults={"replies": replies},
    )


class _NotificationRecorder:
    """替掉 `dispatch_notification`，记下每一次投递"""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, str, dict]] = []

    async def __call__(self, title: str, body: str, level: str, **kwargs: Any) -> dict:
        self.calls.append((title, body, level, kwargs))
        return {}

    @property
    def platform_calls(self) -> List[Tuple[str, str, str, dict]]:
        return [call for call in self.calls if not call[3].get("scoped_only")]

    @property
    def tenant_calls(self) -> List[Tuple[str, str, str, dict]]:
        return [call for call in self.calls if call[3].get("scoped_only")]


@pytest.fixture()
def notifications(monkeypatch: pytest.MonkeyPatch) -> _NotificationRecorder:
    """替掉真正的投递出口

    告警投递走的是**懒导入**（`_send` 里才 import），所以补丁要打在
    `services.notification.channels` 那个模块上，而不是告警模块自己身上——
    打在后者上会得到一条恒绿的断言。
    """
    from nekro_agent.services.notification import channels as notification_channels

    recorder = _NotificationRecorder()

    async def fake_dispatch(title: str, body: str, level: str, **kwargs: Any) -> dict:
        return await recorder(title, body, level, **kwargs)

    monkeypatch.setattr(notification_channels, "dispatch_notification", fake_dispatch)
    return recorder


class TestThresholdDedupe:
    """同一个阈值只入队一次"""

    async def test_crossing_eighty_enqueues_once(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 85)

        assert await alerts_module.maybe_enqueue(channel, await evaluate(channel)) is True
        assert await alerts_module.maybe_enqueue(channel, await evaluate(channel)) is False
        assert alerts_module.alert_buffer.pending_count() == 1

    async def test_under_the_first_threshold_enqueues_nothing(self, db) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 10)

        assert await alerts_module.maybe_enqueue(channel, await evaluate(channel)) is False

    async def test_crossing_both_thresholds_enqueues_twice(self, db) -> None:  # noqa: ARG002
        """80% 与 100% 是两条告警；合并发送时会把它们折成一条（见下面的合并测试）"""
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 85)
        assert await alerts_module.maybe_enqueue(channel, await evaluate(channel)) is True

        await set_usage(TENANT_CHAT_KEY, 100)
        assert await alerts_module.maybe_enqueue(channel, await evaluate(channel)) is True
        assert alerts_module.alert_buffer.pending_count() == 2

    async def test_lower_threshold_after_a_higher_one_does_not_fire(self, db) -> None:  # noqa: ARG002
        """已经跨过 100% 之后，回到 85% 不该再补一条 80% 的告警"""
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)
        assert await alerts_module.maybe_enqueue(channel, await evaluate(channel)) is True

        await set_usage(TENANT_CHAT_KEY, 85)
        assert await alerts_module.maybe_enqueue(channel, await evaluate(channel)) is False

    async def test_alerted_level_is_persisted(self, db) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))

        row = await DBChannelQuotaDaily.filter(chat_key=TENANT_CHAT_KEY).first()
        assert row is not None
        assert row.alerted_level == 100

    async def test_concurrent_evaluations_enqueue_exactly_once(self, db) -> None:  # noqa: ARG002
        """两条消息同时跨过同一个阈值：条件更新的影响行数只会有一路是 1"""
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 85)
        status = await evaluate(channel)

        results = await asyncio_gather_maybe_enqueue(channel, status)

        assert sum(results) == 1
        assert alerts_module.alert_buffer.pending_count() == 1

    async def test_disabled_policy_never_enqueues(self, db) -> None:  # noqa: ARG002
        channel = await seed_channel()

        assert await alerts_module.maybe_enqueue(channel, await evaluate(channel)) is False

    async def test_unlimited_policy_never_enqueues(self, db) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global(group_daily_limit=0, private_daily_limit=0)

        assert await alerts_module.maybe_enqueue(channel, await evaluate(channel)) is False


async def asyncio_gather_maybe_enqueue(channel: DBChatChannel, status: Any) -> List[bool]:
    import asyncio

    return list(
        await asyncio.gather(*[alerts_module.maybe_enqueue(channel, status) for _ in range(5)]),
    )


class TestDestinations:
    """平台方收全部；实例归属某个租户时，租户再单独收一份"""

    async def test_platform_channel_only_reaches_the_platform(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel(PLATFORM_CHAT_KEY, PLATFORM_INSTANCE)
        await seed_global()
        await set_usage(PLATFORM_CHAT_KEY, 100)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        await alerts_module.flush_alerts_now()

        assert len(notifications.platform_calls) == 1
        assert notifications.tenant_calls == []

    async def test_tenant_channel_reaches_both(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_tenant()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        await alerts_module.flush_alerts_now()

        assert len(notifications.platform_calls) == 1
        assert len(notifications.tenant_calls) == 1

    async def test_tenant_copy_is_scoped_to_its_instance(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_tenant()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        await alerts_module.flush_alerts_now()

        _title, _body, _level, kwargs = notifications.tenant_calls[0]
        assert kwargs["scoped_only"] is True
        assert kwargs["adapter_key"] == ADAPTER
        assert kwargs["instance_key"] == TENANT_INSTANCE

    async def test_platform_copy_names_the_tenant(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_tenant()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        await alerts_module.flush_alerts_now()

        _title, body, _level, _kwargs = notifications.platform_calls[0]
        assert "Acme 客户" in body

    async def test_tenant_copy_does_not_name_the_tenant(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        """那是他自己的频道，写租户名没有信息量"""
        channel = await seed_channel()
        await seed_tenant()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        await alerts_module.flush_alerts_now()

        _title, body, _level, _kwargs = notifications.tenant_calls[0]
        assert "Acme 客户" not in body


class TestMerging:
    """同一个目的地的告警按 10 分钟合并"""

    async def test_alerts_are_merged_into_one_message(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_tenant()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 85)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        await set_usage(TENANT_CHAT_KEY, 100)
        await alerts_module.maybe_enqueue(channel, await evaluate(channel))

        sent = await alerts_module.flush_alerts_now()

        assert sent == 2, "两个目的地各一条（平台方一份、租户一份）"
        # 每个目的地只发了一次，正文里只有一行（同一频道跨两个阈值只留最后那条状态）
        title, body, _level, _kwargs = notifications.platform_calls[0]
        assert title == "频道额度告警：1 个频道"
        assert len(body.splitlines()) == 1
        assert "已告警" in body

    async def test_second_flush_within_the_window_sends_nothing(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 85)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        assert await alerts_module.flush_alerts_now() == 1

        await set_usage(TENANT_CHAT_KEY, 100)
        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        # 未到 10 分钟：积压攒着，不发
        assert await alerts_module.flush_alerts_now() == 0
        assert len(notifications.calls) == 1

    async def test_after_the_window_the_backlog_is_flushed(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 85)
        start = alerts_module._now()  # noqa: SLF001

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        assert await alerts_module.alert_buffer.flush_due(now=start) == 1

        await set_usage(TENANT_CHAT_KEY, 100)
        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        later = start + alerts_module.FLUSH_INTERVAL_SECONDS + 1
        assert await alerts_module.alert_buffer.flush_due(now=later) == 1
        assert len(notifications.calls) == 2

    async def test_force_flush_ignores_the_window(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        """关停时剩下的积压要发一次就结束，不能等 10 分钟"""
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 85)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        assert await alerts_module.alert_buffer.flush_due(force=True) == 1
        assert len(notifications.calls) == 1


class TestAlertBody:
    """正文里只有运维需要的字段，没有任何聊天内容"""

    async def test_body_has_no_chat_content(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        await alerts_module.flush_alerts_now()

        _title, body, _level, _kwargs = notifications.platform_calls[0]
        assert "测试群" in body
        assert TENANT_CHAT_KEY in body
        assert ADAPTER in body
        for leaked in ("在吗", "content_text", "消息内容"):
            assert leaked not in body

    async def test_body_states_the_unit_and_the_ratio(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        await alerts_module.flush_alerts_now()

        _title, body, _level, _kwargs = notifications.platform_calls[0]
        assert "回复次数" in body
        assert "100/100" in body

    async def test_paused_state_is_rendered_as_paused(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        channel = await seed_channel()
        await seed_global(over_limit_action="pause")
        await set_usage(TENANT_CHAT_KEY, 100)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))
        await alerts_module.flush_alerts_now()

        _title, body, _level, _kwargs = notifications.platform_calls[0]
        assert "已暂停" in body

    async def test_title_counts_the_channels_in_the_batch(self, db, notifications: _NotificationRecorder) -> None:  # noqa: ARG002
        await seed_global()
        first = await seed_channel()
        second = await seed_channel(f"{ADAPTER}-{TENANT_INSTANCE}-group_140003", TENANT_INSTANCE, channel_name="另一个群")
        await set_usage(TENANT_CHAT_KEY, 100)
        await set_usage(second.chat_key, 100)

        await alerts_module.maybe_enqueue(first, await evaluate(first))
        await alerts_module.maybe_enqueue(second, await evaluate(second))
        await alerts_module.flush_alerts_now()

        title, body, _level, _kwargs = notifications.platform_calls[0]
        assert title == "频道额度告警：2 个频道"
        assert len(body.splitlines()) == 2


class TestFailuresAreSwallowed:
    """投递失败只记 warning：下一条告警还会再来"""

    async def test_delivery_failure_does_not_raise(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        from nekro_agent.services.notification import channels as notification_channels

        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)

        async def boom(*_args: Any, **_kwargs: Any) -> dict:
            raise RuntimeError("邮件服务器连不上")

        monkeypatch.setattr(notification_channels, "dispatch_notification", boom)

        await alerts_module.maybe_enqueue(channel, await evaluate(channel))

        assert await alerts_module.flush_alerts_now() == 1
        assert alerts_module.alert_buffer.pending_count() == 0, "失败的那批已经出队，不会无限重试"

    async def test_tenant_lookup_failure_does_not_raise(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """查租户归属失败时整条告警评估被吞掉：消息路径不能因为告警查库失败而中断"""
        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("库挂了")

        monkeypatch.setattr(alerts_module, "_tenant_of", boom)

        assert await alerts_module.maybe_enqueue(channel, await evaluate(channel)) is False
        assert alerts_module.alert_buffer.pending_count() == 0

    async def test_alert_queue_failure_is_contained_by_check_and_alert(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """`check_and_alert` 是消息路径上的调用点，它必须把告警的失败咽掉"""
        from nekro_agent.services.channel_quota import service as service_module

        channel = await seed_channel()
        await seed_global()
        await set_usage(TENANT_CHAT_KEY, 100)

        async def boom(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("告警炸了")

        monkeypatch.setattr(alerts_module, "maybe_enqueue", boom)

        status = await service_module.check_and_alert(channel)

        assert status.state == "exceeded"


class TestBuffer:
    """缓冲本身的行为"""

    def test_enqueue_targets_the_requested_destinations(self) -> None:
        buffer = alerts_module.ChannelQuotaAlertBuffer()
        alert = alerts_module.ChannelQuotaAlert(
            chat_key="c",
            channel_name="n",
            adapter_key="a",
            instance_key="i",
            tenant_name="",
            unit="replies",
            used=1,
            limit=1,
            percent=100,
            paused=False,
        )

        buffer.enqueue(alert, [("platform", "", ""), ("tenant", "a", "i")])

        assert buffer.pending_count(("platform", "", "")) == 1
        assert buffer.pending_count(("tenant", "a", "i")) == 1
        assert buffer.pending_count() == 2

    def test_reset_clears_both_the_backlog_and_the_send_times(self) -> None:
        buffer = alerts_module.ChannelQuotaAlertBuffer()
        alert = alerts_module.ChannelQuotaAlert(
            chat_key="c",
            channel_name="n",
            adapter_key="a",
            instance_key="i",
            tenant_name="",
            unit="replies",
            used=1,
            limit=1,
            percent=100,
            paused=False,
        )
        buffer.enqueue(alert)
        buffer._last_sent[("platform", "", "")] = 1.0  # noqa: SLF001

        buffer.reset()

        assert buffer.pending_count() == 0
        assert buffer._last_sent == {}  # noqa: SLF001
