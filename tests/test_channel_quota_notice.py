"""频道每日额度：暂停提示

一句话的三条约束：

1. 当天只在第一次被叫到时说一次——两条消息几乎同时到达时只能有一路说出来；
2. 说出去的话要过租户自己的输出过滤（租户能写提示语，就必须能被自己的词表拦下）；
3. 发不出去不重试、不回滚——否则一个发不出去的频道会被反复尝试到刷屏。

「不跑 Agent、不计用量、不推 SYSTEM 消息」在 `test_channel_quota_wiring.py` 里一并覆盖。
"""

import asyncio
import dataclasses
from typing import Any, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.interface.schemas.platform import (
    PlatformSendRequest,
    PlatformSendResponse,
    PlatformSendSegment,
    PlatformSendSegmentType,
)
from nekro_agent.core.config import config as global_config
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.errors import AdapterUnavailableError
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.channel_quota import notice as notice_module
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota.policy import SCOPE_GLOBAL
from nekro_agent.services.channel_quota.service import evaluate

ADAPTER = "onebot_v11"
INSTANCE = "notice1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_130001"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    policy_module.invalidate_policy_cache()
    try:
        yield
    finally:
        policy_module.invalidate_policy_cache()
        await Tortoise.close_connections()


async def seed(**global_fields: Any) -> DBChatChannel:
    defaults = {
        "enabled": True,
        "unit": "replies",
        "group_daily_limit": 1,
        "private_daily_limit": 1,
        "over_limit_action": "pause",
        "pause_message": None,
    }
    defaults.update(global_fields)
    await DBChannelQuotaPolicy.create(scope=SCOPE_GLOBAL, **defaults)
    policy_module.invalidate_policy_cache()
    channel = await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_130001",
        chat_key=CHAT_KEY,
        channel_type="group",
        data="{}",
    )
    await DBChannelQuotaDaily.create(chat_key=CHAT_KEY, day=counter.quota_day(), replies=1)
    return channel


class _RecordingAdapter:
    """记下每一次真正发出去的请求"""

    def __init__(self, *, success: bool = True) -> None:
        self.requests: list[PlatformSendRequest] = []
        self.success = success

    async def forward_message(self, request: PlatformSendRequest) -> PlatformSendResponse:
        self.requests.append(request)
        if not self.success:
            return PlatformSendResponse(success=False, error_message="协议端拒绝")
        return PlatformSendResponse(success=True)

    @property
    def texts(self) -> list[str]:
        return [segment.content for request in self.requests for segment in request.segments]


@pytest.fixture()
def adapter(monkeypatch: pytest.MonkeyPatch) -> _RecordingAdapter:
    recorder = _RecordingAdapter()

    async def fake_get_adapter_for_chat(_chat_key: str):
        return recorder

    monkeypatch.setattr(notice_module.adapter_utils, "get_adapter_for_chat", fake_get_adapter_for_chat)
    return recorder


class TestSendOncePerDay:
    """当天只发一次：靠一次条件更新决定谁发"""

    async def test_first_call_sends_and_marks_the_row(self, db, adapter: _RecordingAdapter) -> None:  # noqa: ARG002
        channel = await seed(pause_message="今天的额度用完了")
        status = await evaluate(channel)

        assert await notice_module.send_pause_notice_once(channel, status) is True
        assert adapter.texts == ["今天的额度用完了"]

        row = await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY).first()
        assert row is not None
        assert row.pause_notice_sent_at is not None

    async def test_second_call_is_silent(self, db, adapter: _RecordingAdapter) -> None:  # noqa: ARG002
        channel = await seed(pause_message="今天的额度用完了")

        assert await notice_module.send_pause_notice_once(channel, await evaluate(channel)) is True
        assert await notice_module.send_pause_notice_once(channel, await evaluate(channel)) is False

        # 第二次读到的状态里 `pause_notice_sent_at` 已经有值，这里再确认只发了一次
        assert len(adapter.requests) == 1

    async def test_concurrent_calls_send_exactly_one_notice(self, db, adapter: _RecordingAdapter) -> None:  # noqa: ARG002
        """两条消息几乎同时到达：条件更新的影响行数只会有一路是 1"""
        channel = await seed(pause_message="今天的额度用完了")
        status = await evaluate(channel)

        results = await asyncio.gather(
            *[notice_module.send_pause_notice_once(channel, status) for _ in range(5)],
        )

        assert sum(results) == 1
        assert len(adapter.requests) == 1

    async def test_missing_daily_row_sends_nothing(self, db, adapter: _RecordingAdapter) -> None:  # noqa: ARG002
        """没有计数行就没有写「已经发过」的地方，也就没有只发一次的依据"""
        channel = await seed(pause_message="今天的额度用完了")
        await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY).delete()
        status = await evaluate(channel)

        assert await notice_module.send_pause_notice_once(channel, status) is False
        assert adapter.requests == []


class TestPauseMessageSource:
    """提示语从生效策略里取，三层优先级已经在策略测试里钉住"""

    async def test_default_message_is_used_when_nobody_set_one(self, db, adapter: _RecordingAdapter) -> None:  # noqa: ARG002
        channel = await seed()
        await notice_module.send_pause_notice_once(channel, await evaluate(channel))

        assert adapter.texts == [policy_module.DEFAULT_PAUSE_MESSAGE]

    async def test_tenant_message_is_used(self, db, adapter: _RecordingAdapter) -> None:  # noqa: ARG002
        """租户能改那句话；同一行上的平台方字段不受影响"""
        channel = await seed(pause_message="平台方的提示")
        await DBChannelQuotaPolicy.create(
            scope="instance",
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            tenant_pause_message="我们群今天先歇了",
        )
        policy_module.invalidate_policy_cache()

        await notice_module.send_pause_notice_once(channel, await evaluate(channel))

        assert adapter.texts == ["我们群今天先歇了"]


class TestOutputFilter:
    """提示语必须过租户自己的输出过滤，否则它就是一条绕过词表的直投通道"""

    async def test_blocked_word_is_replaced_before_sending(
        self,
        db,
        adapter: _RecordingAdapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        channel = await seed(pause_message="今天的额度用完了，请找管理员")
        from nekro_agent.services.output_filter import service as filter_service

        filtered_config = global_config.model_copy(
            update={
                "OUTPUT_FILTER_ENABLE": True,
                "OUTPUT_FILTER_WORDS": "管理员",
                "OUTPUT_FILTER_REGEX": "",
                "OUTPUT_FILTER_MODE": "fragment",
                "OUTPUT_FILTER_REPLACEMENT": "**Filter**",
            },
        )

        async def fake_settings(chat_key: str):  # noqa: ARG001
            return filter_service.settings_from_config(filtered_config)

        # 过滤链内部的 `settings_for_chat` 就是这一条；不另外造一层假实现
        monkeypatch.setattr(filter_service, "settings_for_chat", fake_settings)

        await notice_module.send_pause_notice_once(channel, await evaluate(channel))

        assert len(adapter.texts) == 1
        assert "管理员" not in adapter.texts[0]
        assert "**Filter**" in adapter.texts[0]

    async def test_request_carries_the_enabled_filter_marker(
        self,
        db,
        adapter: _RecordingAdapter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        """出站已经过滤过时置标记，供协议端钩子跳过第 1 层（否则会被过滤两次）"""
        channel = await seed(pause_message="提示")
        seen: list[bool] = []
        original = adapter.forward_message

        async def spy(request: PlatformSendRequest) -> PlatformSendResponse:
            from nekro_agent.services.output_filter.service import is_outbound_filtered

            seen.append(is_outbound_filtered())
            return await original(request)

        monkeypatch.setattr(adapter, "forward_message", spy)

        await notice_module.send_pause_notice_once(channel, await evaluate(channel))

        assert seen == [True]

    async def test_filter_is_not_bypassed_even_without_rules(self, db, adapter: _RecordingAdapter) -> None:  # noqa: ARG002
        """没配规则时原样发出；这条断言的是「走了过滤」而不是「过滤起了作用」"""
        channel = await seed(pause_message="提示")

        await notice_module.send_pause_notice_once(channel, await evaluate(channel))

        assert adapter.texts == ["提示"]


class TestFailureHandling:
    """发不出去不重试、不回滚"""

    async def test_adapter_unavailable_returns_false_without_raising(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        channel = await seed(pause_message="提示")

        async def boom(_chat_key: str):
            raise AdapterUnavailableError(adapter_key=ADAPTER, reason="没有在线实例")

        monkeypatch.setattr(notice_module.adapter_utils, "get_adapter_for_chat", boom)

        assert await notice_module.send_pause_notice_once(channel, await evaluate(channel)) is False

    async def test_failed_send_is_not_rolled_back(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """回滚会让这个频道在恢复前每次被叫到都再试一次，而那一点成功率都没提高"""
        channel = await seed(pause_message="提示")
        recorder = _RecordingAdapter(success=False)

        async def fake_get_adapter_for_chat(_chat_key: str):
            return recorder

        monkeypatch.setattr(notice_module.adapter_utils, "get_adapter_for_chat", fake_get_adapter_for_chat)

        assert await notice_module.send_pause_notice_once(channel, await evaluate(channel)) is False

        row = await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY).first()
        assert row is not None
        assert row.pause_notice_sent_at is not None, "发送失败之后不能把标记清掉"

    async def test_send_exception_is_swallowed(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await seed(pause_message="提示")

        class _Exploding:
            async def forward_message(self, _request: PlatformSendRequest) -> Optional[PlatformSendResponse]:
                raise RuntimeError("协议端崩了")

        async def fake_get_adapter_for_chat(_chat_key: str):
            return _Exploding()

        monkeypatch.setattr(notice_module.adapter_utils, "get_adapter_for_chat", fake_get_adapter_for_chat)

        assert await notice_module.send_pause_notice_once(channel, await evaluate(channel)) is False

    async def test_empty_message_sends_nothing(self, db, adapter: _RecordingAdapter) -> None:  # noqa: ARG002
        """提示语被清成空白时宁可不说，也不要在群里发一条空消息"""
        channel = await seed()
        status = await evaluate(channel)
        blank = dataclasses.replace(status.policy, pause_message="   ")
        blank_status = dataclasses.replace(status, policy=blank)

        assert await notice_module.send_pause_notice_once(channel, blank_status) is False
        assert adapter.requests == []
        row = await DBChannelQuotaDaily.filter(chat_key=CHAT_KEY).first()
        assert row is not None
        assert row.pause_notice_sent_at is None, "空消息不该用掉当天唯一的那次提示机会"


class TestMessageShape:
    """发出去的就是一句纯文本，不带引用、不带附件"""

    async def test_single_text_segment_without_reference(self, db, adapter: _RecordingAdapter) -> None:  # noqa: ARG002
        channel = await seed(pause_message="提示")

        await notice_module.send_pause_notice_once(channel, await evaluate(channel))

        request = adapter.requests[0]
        assert request.chat_key == CHAT_KEY
        assert request.ref_msg_id is None
        assert len(request.segments) == 1
        assert request.segments[0].type == PlatformSendSegmentType.TEXT

    async def test_segment_is_a_plain_text_segment(self) -> None:
        """与命令输出那条路径同一个形状：`PlatformSendSegment(type=TEXT, content=...)`"""
        segment = PlatformSendSegment(type=PlatformSendSegmentType.TEXT, content="x")
        assert segment.content == "x"
        assert segment.file_path is None
