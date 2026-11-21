"""心声插件的生命周期、事件总线接入与降级行为

这里保护的是几件在真实运行中出过问题、且靠观察聊天窗口无法稳定复现的事：

1. **无频道上下文的事件必须安全跳过**。心声挂在主对话回调上，一旦解引用到 None，
   刷屏的是每一条进来的消息，而不是某一次心声。
2. **心声只在布防的轮次里出现在模型的工具箱里**。这是"不因为加了心声让主 Agent 回复
   质量下降"的唯一结构性保证——没布防时提示词里根本没有这个方法。
3. **全进程只占一个事件总线订阅位**。总线的订阅位是全局稀缺资源（`_MAX_SUBSCRIBERS`），
   还和 WebUI 的实时推送共用；按频道各订阅一次会在几十个活跃群时把后台推送挤掉。
   这条断言就是钉死"订阅数与频道数无关"。
4. **订阅被总线移除之后要能自愈**。总线对慢消费者的保护是静默移除订阅者，
   被移除方收不到任何通知；没有自愈，心声会在某次积压之后永久失效。
5. **发送失败不能反噬主链路**。心声是回复之后的附加物，它坏掉时用户应该只是少看到
   一张卡片，而不是看到一个报错、或者下一轮又立刻重试。
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.services import system_broadcast
from plugins.builtin import inner_voice as voice

CHAT_KEY = "onebot_v11-10001-group_12345678"
OTHER_CHAT_KEY = "onebot_v11-10001-group_87654321"


class StubCtx:
    """只实现心声用到的那几个口子的上下文替身

    真实 `AgentCtx` 的发送方法要经 message_service 打到适配器上，
    这里要断言的是"调用了什么、按什么顺序"，不是消息真的发出去了。
    """

    def __init__(self, chat_key: str = CHAT_KEY, *, fail_on: str = "") -> None:
        self.from_chat_key = chat_key
        self.channel_type = ChatType.GROUP.value
        self.sent_images: List[str] = []
        self.sent_texts: List[str] = []
        self._fail_on = fail_on
        self.fs = self

    async def mixed_forward_file(self, file: Any, file_name: str = "") -> str:
        if self._fail_on == "forward":
            raise RuntimeError("forward failed")
        return f"/sandbox/{file_name or 'card.png'}"

    async def send_image(self, file_path: str, *, record: bool = True) -> None:
        if self._fail_on == "send":
            raise RuntimeError("send failed")
        self.sent_images.append(file_path)

    async def send_text(self, content: str, *, record: bool = True) -> None:
        self.sent_texts.append(content)


def make_message(chat_type: Any) -> ChatMessage:
    """构造一条最小消息

    直接传原始 `chat_type`：`ChatMessage.Config.use_enum_values` 会把枚举拍成字符串，
    这正是插件必须容忍的那种双形态。
    """
    message = ChatMessage.create_empty(CHAT_KEY)
    message.chat_type = chat_type
    return message


def finish_payload(chat_key: str, started_at: int, *, active: bool = False, event_type: str = "agent_active") -> str:
    """构造一条总线上的回合结束事件（与 `AgentActiveEvent.model_dump_json` 同形）"""
    return json.dumps(
        {
            "type": event_type,
            "chat_key": chat_key,
            "active": active,
            "channel_name": "测试群",
            "chat_type": "group",
            "started_at": started_at,
            "max_duration_ms": 300_000,
        },
    )


async def _never_ending_consumer() -> None:
    """替身消费循环

    `listen_forever` 是个跑到进程结束为止的循环，留一个真的在测试里跑起来，
    pytest 会卡在关闭事件循环那一步。这里只替掉"循环"本身，
    订阅位的申请与归还（`ensure_listener` / `release_subscription`）仍然走真实实现，
    循环体的两块逻辑（`handle_raw_event` 与 `heartbeat`）也各自有直接的用例。
    """
    return


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    """每个用例前后都把插件与总线的全局状态清干净

    插件持的是进程级单例（一个消费者、一个订阅位），用例之间不隔离的话，
    "订阅位只有一个"这类断言会随执行顺序时好时坏。
    """
    monkeypatch.setattr(voice, "listen_forever", _never_ending_consumer)

    # 心声的布防与发送已经接上按频道判据；本文件测的是生命周期与降级行为，不是作用域，
    # 这里把判据与插件发送方法替身成 StubCtx 上的记录口，保持原用例的观察面不变。
    async def _effective(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _send_image(_chat_key: str, image_path: str, ctx: Any, **_kwargs: Any) -> bool:
        await ctx.send_image(image_path)
        return True

    async def _send_text(_chat_key: str, message: str, ctx: Any, **_kwargs: Any) -> bool:
        await ctx.send_text(message)
        return True

    monkeypatch.setattr(voice, "plugin_effective_in_chat", _effective)
    monkeypatch.setattr(voice.plugin, "send_image", _send_image)
    monkeypatch.setattr(voice.plugin, "send_text", _send_text)
    _teardown()
    yield
    _teardown()


def _teardown() -> None:
    task, voice._listener_task = voice._listener_task, None
    if task is not None:
        task.cancel()
    voice.release_subscription()
    for background in list(voice._background_tasks):
        background.cancel()
    voice._background_tasks.clear()
    voice.reset_state()
    system_broadcast._subscribers.clear()


@pytest.fixture
def _armed_group(monkeypatch: pytest.MonkeyPatch):
    """把插件配置调到"必定布防"，再走一遍真实的消息回调"""
    monkeypatch.setattr(voice.config, "ENABLE_INNER_VOICE", True)
    monkeypatch.setattr(voice.config, "TRIGGER_PROBABILITY", 1.0)
    monkeypatch.setattr(voice.config, "MIN_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(voice.random, "random", lambda: 0.0)


class TestChatTypeResolution:
    """群聊/私聊判定"""

    @pytest.mark.parametrize(
        ("chat_type", "channel_type", "expected"),
        [
            (ChatType.GROUP, None, True),
            (ChatType.PRIVATE, None, False),
            # use_enum_values 之后实例上存的是裸字符串，必须同样认得
            ("group", None, True),
            ("private", None, False),
            # 事件本身认不出时，退到上下文的频道类型
            (ChatType.UNKNOWN, "group", True),
            ("unknown", "private", False),
            (None, "group", True),
            # 两边都认不出 —— 这就是"没有频道上下文"的那一类事件
            (ChatType.UNKNOWN, None, None),
            (None, None, None),
            ("", "", None),
        ],
    )
    def test_resolution_table(self, chat_type: Any, channel_type: Optional[str], expected: Optional[bool]) -> None:
        assert voice.resolve_is_group(chat_type, channel_type) is expected


class TestMessageCallbackSafety:
    """主链路回调的健壮性"""

    async def test_event_without_channel_context_is_skipped(self, _armed_group) -> None:
        # 频道类型认不出的事件：既不布防，也不允许抛出去让主链路刷屏
        ctx = AgentCtx(from_chat_key=CHAT_KEY, channel_type=None)
        assert await voice.on_user_message(ctx, make_message(ChatType.UNKNOWN)) is None
        assert voice.is_armed(CHAT_KEY) is False

    async def test_event_without_chat_key_is_skipped(self, _armed_group) -> None:
        ctx = AgentCtx(from_chat_key=None, channel_type=ChatType.GROUP.value)
        assert await voice.on_user_message(ctx, make_message(ChatType.GROUP)) is None
        assert voice._armed == {}

    async def test_group_message_arms_the_session(self, _armed_group) -> None:
        ctx = AgentCtx(from_chat_key=CHAT_KEY, channel_type=ChatType.GROUP.value)
        await voice.on_user_message(ctx, make_message(ChatType.GROUP))
        assert voice.is_armed(CHAT_KEY) is True

    async def test_disabled_plugin_never_arms(self, _armed_group, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(voice.config, "ENABLE_INNER_VOICE", False)
        ctx = AgentCtx(from_chat_key=CHAT_KEY, channel_type=ChatType.GROUP.value)
        await voice.on_user_message(ctx, make_message(ChatType.GROUP))
        assert voice.is_armed(CHAT_KEY) is False
        # 功能关着的时候连订阅位都不该占
        assert system_broadcast._subscribers == []


class TestMethodDisclosure:
    """工具箱披露：没布防的轮次里模型看不到这个方法"""

    async def test_not_disclosed_without_arming(self) -> None:
        ctx = AgentCtx(from_chat_key=CHAT_KEY)
        assert await voice.collect_available_methods(ctx) == []

    async def test_disclosed_once_armed(self) -> None:
        voice._armed[CHAT_KEY] = int(voice.time.time() * 1000)
        ctx = AgentCtx(from_chat_key=CHAT_KEY)
        assert await voice.collect_available_methods(ctx) == [voice.inner_voice]

    async def test_disclosure_is_per_session(self) -> None:
        voice._armed[CHAT_KEY] = int(voice.time.time() * 1000)
        other = AgentCtx(from_chat_key=OTHER_CHAT_KEY)
        assert await voice.collect_available_methods(other) == []

    async def test_stale_arming_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 布防之后 Agent 一直没被触发：不能过几个小时才在毫不相干的对话里冒出来
        now = 10_000.0
        voice._armed[CHAT_KEY] = int(now * 1000)
        monkeypatch.setattr(voice.time, "time", lambda: now + voice.ARM_TTL_SECONDS + 1)
        ctx = AgentCtx(from_chat_key=CHAT_KEY)
        assert await voice.collect_available_methods(ctx) == []
        assert CHAT_KEY not in voice._armed


class TestSingleSubscriptionSlot:
    """全进程只占一个订阅位

    这是这次改造的核心不变量：订阅位是全局的（`_MAX_SUBSCRIBERS = 50`）、
    而且和 WebUI 的 SSE 连接共用。按频道各订阅一次的话，几十个活跃群就会把
    用户的后台实时推送挤掉（超限时 `subscribe_system_events()` 返回 None）。
    """

    async def test_many_armed_channels_share_one_subscription(self, _armed_group) -> None:
        for index in range(60):
            chat_key = f"onebot_v11-10001-group_{index:08d}"
            ctx = AgentCtx(from_chat_key=chat_key, channel_type=ChatType.GROUP.value)
            await voice.on_user_message(ctx, make_message(ChatType.GROUP))

        assert len(voice._armed) == 60
        assert len(system_broadcast._subscribers) == 1

    async def test_webui_subscription_survives_alongside(self, _armed_group) -> None:
        # 先来一个 WebUI 的 SSE 连接，再让心声接入：两者必须能共存，且总数为 2
        webui_queue = system_broadcast.subscribe_system_events()
        assert webui_queue is not None

        for index in range(40):
            chat_key = f"onebot_v11-10001-group_{index:08d}"
            ctx = AgentCtx(from_chat_key=chat_key, channel_type=ChatType.GROUP.value)
            await voice.on_user_message(ctx, make_message(ChatType.GROUP))

        assert len(system_broadcast._subscribers) == 2
        assert any(q is webui_queue for q in system_broadcast._subscribers)

    async def test_ensure_listener_is_idempotent(self) -> None:
        assert voice.ensure_listener() is True
        first_queue = voice._listener_queue
        for _ in range(10):
            assert voice.ensure_listener() is True
        assert voice._listener_queue is first_queue
        assert len(system_broadcast._subscribers) == 1


class TestEventDispatch:
    """按 chat_key 分发：一个订阅要服务所有频道"""

    @pytest.mark.parametrize(
        ("event", "armed_at_ms", "expected"),
        [
            # 正常的回合结束事件
            ({"type": "agent_active", "active": False, "chat_key": CHAT_KEY, "started_at": 2000}, 1000, True),
            # 开始事件不是结束
            ({"type": "agent_active", "active": True, "chat_key": CHAT_KEY, "started_at": 2000}, 1000, False),
            # 别的事件类型（例如运行阶段）即便 active=False 也不算
            ({"type": "agent_runtime_status", "active": False, "chat_key": CHAT_KEY, "started_at": 2000}, 1000, False),
            # 别的频道
            ({"type": "agent_active", "active": False, "chat_key": OTHER_CHAT_KEY, "started_at": 2000}, 1000, False),
            # 布防之前就已经在跑的上一轮：必须放过去继续等
            ({"type": "agent_active", "active": False, "chat_key": CHAT_KEY, "started_at": 500}, 1000, False),
            # 同一毫秒开始的算本轮
            ({"type": "agent_active", "active": False, "chat_key": CHAT_KEY, "started_at": 1000}, 1000, True),
            # started_at 缺失/为 0 的事件当作上一轮
            ({"type": "agent_active", "active": False, "chat_key": CHAT_KEY}, 1000, False),
        ],
    )
    def test_round_finished_table(self, event: Dict[str, Any], armed_at_ms: int, expected: bool) -> None:
        assert voice.is_round_finished_event(event, CHAT_KEY, armed_at_ms) is expected

    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            "[1, 2, 3]",
            '"just a string"',
            # 订阅时总线先推的那条快照事件，顶层没有 chat_key
            '{"type": "snapshot", "data": {}}',
        ],
    )
    def test_unrelated_payloads_are_ignored(self, raw: str) -> None:
        voice._armed[CHAT_KEY] = 1000
        assert voice.handle_raw_event(raw) is None
        assert CHAT_KEY in voice._armed

    async def test_real_broadcast_payload_is_recognised(self) -> None:
        """把宿主真正广播的那条事件原样喂回去

        上面的表用的是手写 payload，认的是字段名。这条用真实的 `AgentActiveEvent`
        走一遍 `publish_system_event` -> 订阅队列 -> `handle_raw_event`，
        宿主改了事件模型（字段改名、`started_at` 单位变了）时才会在这里先炸。
        """
        assert voice.ensure_listener() is True
        queue = voice._listener_queue
        assert queue is not None
        queue.get_nowait()  # 订阅时总线先推的那条快照

        voice._armed[CHAT_KEY] = 1000
        await system_broadcast.publish_system_event(
            system_broadcast.AgentActiveEvent(chat_key=CHAT_KEY, active=False, started_at=2000),
        )

        assert voice.handle_raw_event(queue.get_nowait()) == CHAT_KEY

    async def test_only_the_finished_channel_settles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", False)
        voice.ensure_listener()
        voice._armed[CHAT_KEY] = 1000
        voice._armed[OTHER_CHAT_KEY] = 1000
        mine = StubCtx(CHAT_KEY)
        theirs = StubCtx(OTHER_CHAT_KEY)
        await voice.inner_voice(mine, "我的心声")
        await voice.inner_voice(theirs, "别人的心声")

        assert voice.handle_raw_event(finish_payload(CHAT_KEY, started_at=2000)) == CHAT_KEY
        await _drain_background()

        assert mine.sent_texts == ["我的心声"]
        assert theirs.sent_texts == []
        assert OTHER_CHAT_KEY in voice._pending
        assert CHAT_KEY not in voice._armed
        assert OTHER_CHAT_KEY in voice._armed

    async def test_settlement_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 同一轮里沙盒重试、或结束事件被重复广播，都不该发出第二张卡片
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", False)
        voice.ensure_listener()
        voice._armed[CHAT_KEY] = 1000
        ctx = StubCtx()
        await voice.inner_voice(ctx, "再说一次也没用。")
        await voice.inner_voice(ctx, "以最后一次为准。")
        voice.handle_raw_event(finish_payload(CHAT_KEY, started_at=2000))
        voice.handle_raw_event(finish_payload(CHAT_KEY, started_at=2000))
        await _drain_background()

        assert ctx.sent_texts == ["以最后一次为准。"]

    async def test_settlement_without_content_sends_nothing_but_disarms(self) -> None:
        # 模型选择不说话：布防要解除，冷却却不应该被消耗掉
        voice._armed[CHAT_KEY] = 1000
        await voice.settle_round(CHAT_KEY)
        await _drain_background()

        assert CHAT_KEY not in voice._armed
        assert CHAT_KEY not in voice._last_sent_at

    async def test_settlement_sends_card_when_rendering_is_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 固定住渲染链，否则"发的是卡片还是降级文本"取决于跑测试这台机器有没有中文字体
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", True)
        monkeypatch.setattr(voice, "fonts_available", lambda: True)
        monkeypatch.setattr(voice, "render_inner_voice_card", lambda _body: b"png")

        voice.ensure_listener()
        voice._armed[CHAT_KEY] = 1000
        ctx = StubCtx()
        await voice.inner_voice(ctx, "原来他还记得。")
        voice.handle_raw_event(finish_payload(CHAT_KEY, started_at=2000))
        await _drain_background()

        assert len(ctx.sent_images) == 1
        assert ctx.sent_texts == []
        assert voice._pending == {}
        assert CHAT_KEY in voice._last_sent_at


class TestSubscriptionSelfHealing:
    """订阅被总线移除之后要能自己接回来

    `publish_system_event` 在订阅者队列积压超过 `_MAX_QUEUE_SIZE` 时会把订阅者
    直接从 `_subscribers` 里删掉，被删方收不到任何通知。没有自愈，
    心声会在某一次积压之后对整个进程永久失效。
    """

    async def test_alive_probe_follows_the_bus(self) -> None:
        assert voice.ensure_listener() is True
        queue = voice._listener_queue
        assert queue is not None
        assert voice.subscription_alive(queue) is True

        system_broadcast._subscribers.remove(queue)
        assert voice.subscription_alive(queue) is False

    async def test_heartbeat_resubscribes_after_removal(self) -> None:
        assert voice.ensure_listener() is True
        dropped = voice._listener_queue
        assert dropped is not None

        # 模拟慢消费者保护：总线把我们踢了
        system_broadcast._subscribers.remove(dropped)
        voice.heartbeat()

        assert voice._listener_queue is not None
        assert voice._listener_queue is not dropped
        # 自愈之后仍然只有一个订阅位，不能越修越多
        assert len(system_broadcast._subscribers) == 1

    async def test_heartbeat_keeps_a_healthy_subscription(self) -> None:
        assert voice.ensure_listener() is True
        queue = voice._listener_queue
        voice.heartbeat()
        assert voice._listener_queue is queue
        assert len(system_broadcast._subscribers) == 1

    async def test_probe_failure_does_not_thrash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 上游把 `_subscribers` 改名/换实现时，探测读不到就当订阅还活着，
        # 宁可少一次自愈也不要无限重订阅、反过来把订阅位耗光
        assert voice.ensure_listener() is True
        queue = voice._listener_queue
        monkeypatch.setattr(voice, "_bus", _BusWithoutInternals())
        voice.heartbeat()
        assert voice._listener_queue is queue

    async def test_expired_arming_does_not_block_the_next_round(self, _armed_group, monkeypatch: pytest.MonkeyPatch) -> None:
        # 结束事件丢了、心跳又一直没轮到：过期的布防不能把这个会话永久卡在"在途"
        now = 10_000.0
        voice._armed[CHAT_KEY] = int((now - voice.ARM_TTL_SECONDS - 1) * 1000)
        monkeypatch.setattr(voice.time, "time", lambda: now)

        ctx = AgentCtx(from_chat_key=CHAT_KEY, channel_type=ChatType.GROUP.value)
        await voice.on_user_message(ctx, make_message(ChatType.GROUP))

        assert voice._armed[CHAT_KEY] == int(now * 1000)

    def test_sweep_drops_rounds_whose_finish_event_was_lost(self) -> None:
        # 结束事件在积压里丢了：布防不清掉的话，这个频道的 in_flight 永远为真，
        # 心声对它永久失效
        now_ms = 10_000_000
        stale_ms = now_ms - int(voice.ARM_TTL_SECONDS * 1000) - 1
        voice._armed[CHAT_KEY] = stale_ms
        voice._armed[OTHER_CHAT_KEY] = now_ms
        voice._pending[CHAT_KEY] = ("丢了的心声", StubCtx())  # type: ignore[assignment]

        assert voice.sweep_expired(now_ms) == [CHAT_KEY]
        assert CHAT_KEY not in voice._armed
        assert CHAT_KEY not in voice._pending
        assert OTHER_CHAT_KEY in voice._armed


class TestNoSubscriptionSlotFallback:
    """拿不到订阅位时的降级"""

    async def test_full_bus_falls_back_to_grace_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", False)
        monkeypatch.setattr(voice, "FALLBACK_GRACE_SECONDS", 0)
        monkeypatch.setattr(system_broadcast, "subscribe_system_events", lambda: None)

        assert voice.ensure_listener() is False
        assert voice._listener_queue is None
        # 订阅位满是持续状态，重试不该每次都刷一条 WARNING
        assert voice._slot_unavailable is True

        voice._armed[CHAT_KEY] = 1000
        ctx = StubCtx()
        result = await voice.inner_voice(ctx, "订阅位满了也得发出去")
        await _drain_background()

        assert "shortly" in result
        assert ctx.sent_texts == ["订阅位满了也得发出去"]
        assert voice._pending == {}
        assert CHAT_KEY not in voice._armed

    async def test_missing_event_bus_falls_back_to_grace_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 宿主根本没有 system_broadcast（或它换了接口）：能力探测失败就降级，不是崩
        monkeypatch.setattr(voice, "_bus", None)
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", False)
        monkeypatch.setattr(voice, "FALLBACK_GRACE_SECONDS", 0)

        assert voice.ensure_listener() is False
        assert voice.acquire_subscription() is None
        assert voice.subscription_alive(asyncio.Queue()) is True

        voice._armed[CHAT_KEY] = 1000
        ctx = StubCtx()
        await voice.inner_voice(ctx, "上游模式")
        await _drain_background()

        assert ctx.sent_texts == ["上游模式"]
        assert voice._pending == {}


class TestSandboxMethodBuffering:
    """沙盒方法只登记，不发送"""

    async def test_call_buffers_instead_of_sending(self) -> None:
        voice.ensure_listener()
        voice._armed[CHAT_KEY] = 1000
        ctx = StubCtx()
        result = await voice.inner_voice(ctx, "今天的风好像有点吵。")
        assert voice._pending[CHAT_KEY][0] == "今天的风好像有点吵。"
        assert "after your reply" in result
        assert ctx.sent_images == []
        assert ctx.sent_texts == []

    @pytest.mark.parametrize("content", ["", "   \n\t "])
    async def test_blank_content_is_not_buffered(self, content: str) -> None:
        ctx = StubCtx()
        result = await voice.inner_voice(ctx, content)
        assert "skipped" in result
        assert voice._pending == {}

    async def test_call_without_chat_key_is_refused(self) -> None:
        ctx = StubCtx(chat_key="")
        result = await voice.inner_voice(ctx, "有内容但没有会话")
        assert "skipped" in result
        assert voice._pending == {}


class TestFailureIsolation:
    """失败隔离：心声坏掉时主链路毫发无伤"""

    async def test_render_failure_does_not_escape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", True)
        monkeypatch.setattr(voice, "fonts_available", lambda: True)
        monkeypatch.setattr(voice, "render_inner_voice_card", _raise_render_error)

        voice.ensure_listener()
        voice._armed[CHAT_KEY] = 1000
        ctx = StubCtx()
        await voice.inner_voice(ctx, "这一句注定发不出去。")
        voice.handle_raw_event(finish_payload(CHAT_KEY, started_at=2000))
        await _drain_background()

        assert ctx.sent_images == []
        # 缓存已经清空、冷却已经记上：失败不会在下一轮里变成重试风暴
        assert voice._pending == {}
        assert CHAT_KEY in voice._last_sent_at

    async def test_send_failure_does_not_escape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", True)
        monkeypatch.setattr(voice, "fonts_available", lambda: True)
        monkeypatch.setattr(voice, "render_inner_voice_card", lambda _body: b"png")

        ctx = StubCtx(fail_on="send")
        await voice.render_and_send(ctx, "发送环节炸了")  # type: ignore[arg-type]
        assert ctx.sent_images == []

    async def test_one_broken_channel_does_not_block_the_next(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 单订阅意味着所有频道共用一个消费者：一个频道的结算炸了，
        # 后面频道的结算必须照常。这是原先"收尾回调逐插件 try/except"那条保证的等价物。
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", False)
        voice.ensure_listener()
        voice._armed[CHAT_KEY] = 1000
        voice._armed[OTHER_CHAT_KEY] = 1000
        broken = StubCtx(CHAT_KEY, fail_on="send")
        broken.send_text = _raise_send_error  # type: ignore[method-assign]
        healthy = StubCtx(OTHER_CHAT_KEY)
        await voice.inner_voice(broken, "炸掉的那一条")
        await voice.inner_voice(healthy, "照常发出去")

        voice.handle_raw_event(finish_payload(CHAT_KEY, started_at=2000))
        voice.handle_raw_event(finish_payload(OTHER_CHAT_KEY, started_at=2000))
        await _drain_background()

        assert healthy.sent_texts == ["照常发出去"]
        assert voice._pending == {}


class TestUpstreamDegradation:
    """原版上游兼容性：缺能力时降级而不是崩"""

    async def test_missing_image_card_falls_back_to_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", False)
        ctx = StubCtx()
        await voice.render_and_send(ctx, "没有渲染器也要说得出口")  # type: ignore[arg-type]
        assert ctx.sent_texts == ["没有渲染器也要说得出口"]
        assert ctx.sent_images == []

    async def test_missing_fonts_falls_back_to_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", True)
        monkeypatch.setattr(voice, "fonts_available", lambda: False)
        ctx = StubCtx()
        await voice.render_and_send(ctx, "缺字体时豆腐块还不如文字")  # type: ignore[arg-type]
        assert ctx.sent_texts == ["缺字体时豆腐块还不如文字"]

    def test_no_core_patch_is_required(self) -> None:
        # 心声改走事件总线之后，宿主侧不该再有任何心声专用的挂载点/回调
        from nekro_agent.services.plugin.base import NekroPlugin
        from nekro_agent.services.plugin.collector import PluginCollector

        assert not hasattr(NekroPlugin, "mount_on_agent_finish")
        assert not hasattr(PluginCollector, "handle_on_agent_finish")

    def test_the_bus_contract_this_plugin_relies_on(self) -> None:
        # 插件依赖的是宿主内部模块，接口一旦变了要在这里先炸，而不是在生产上静默失效
        assert voice.HAS_EVENT_BUS is True
        assert callable(system_broadcast.subscribe_system_events)
        assert callable(system_broadcast.unsubscribe_system_events)
        assert system_broadcast.AgentActiveEvent(chat_key=CHAT_KEY, active=False).type == "agent_active"


class TestAgentCtxPrefill:
    """`AgentCtx` 必须真的带上频道实例

    pydantic v2 里下划线开头的字段是私有属性、不进 `__init__`，
    把它当普通字段传进构造函数会被静默丢弃。这条断言就是钉死那次静默丢弃：
    它一旦复发，全体插件的 `ctx.db_chat_channel` 会同时变回 None。
    """

    def test_private_channel_field_survives_construction(self) -> None:
        channel = _FakeChannel()
        ctx = AgentCtx.create_by_db_chat_channel(db_chat_channel=channel)  # type: ignore[arg-type]

        assert ctx.db_chat_channel is channel
        assert ctx.from_chat_key == CHAT_KEY
        assert ctx.channel_type == ChatType.GROUP.value


class _BusWithoutInternals:
    """只留公开接口、没有 `_subscribers` 的事件总线替身

    对应"上游把订阅表改名/换实现"这一种未来：能力探测读不到内部结构时，
    插件必须当订阅还活着，而不是无限重订阅。
    """

    def subscribe_system_events(self) -> None:
        raise AssertionError("探测失败时不该重新订阅")

    def unsubscribe_system_events(self, queue: Any) -> None:
        return None


class _FakeChannel:
    """只提供 `create_by_db_chat_channel` 读到的那几个字段"""

    chat_key = CHAT_KEY
    channel_id = "10001-group_12345678"
    channel_name = "测试群"
    channel_type = ChatType.GROUP.value
    adapter_key = "onebot_v11"


def _raise_render_error(_body: str) -> bytes:
    raise RuntimeError("字体链损坏")


async def _raise_send_error(_content: str, *, record: bool = True) -> None:
    raise RuntimeError("适配器掉线")


async def _drain_background() -> None:
    """等插件丢进后台的发送任务跑完"""
    for task in list(voice._background_tasks):
        if task is voice._listener_task:
            continue
        try:
            await task
        except Exception:  # noqa: BLE001, S110 - 后台任务本就该自行吞掉异常，这里只是等它结束
            pass
