"""单进程多实例下的插件故障隔离

这里保护的是"一个坏插件不能拖垮整个进程"这条约束。它在单实例部署下看不出差别，
在一个进程跑几十个实例时却是可用性的底线：插件在本进程内只加载一份、被所有实例共用，
所以插件里的一次事故不是"某个实例出问题"，而是所有实例一起出问题。

1. **异常不外泄**：一个插件抛异常，排在它后面的插件与主链路照常。
   改造前 `handle_on_user_message` 是裸 `await`，一次抛出会把整轮消息处理带崩。
2. **卡住的回调必须超时**。这是单进程下最危险的一种——比抛异常严重得多，
   因为 `await` 一个永不完成的协程会把这条链路无限挂起，而它占的是共用的事件循环。
3. **连续失败自动隔离，且隔离可恢复、只作用于目标插件**。
4. **离线渲染的调度契约**：Pillow 作画必须离开事件循环。实测一张好感度榜单要 500ms，
   在事件循环里跑等于让本进程内所有实例停摆半秒。
"""

import asyncio
import threading
from typing import Any, List, Optional

import pytest

from nekro_agent.core.config import config
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.schemas.signal import MsgSignal
from nekro_agent.services.plugin import isolation
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import plugin_collector

CHAT_KEY = "onebot_v11-10001-group_12345678"


def _allow_every_plugin_push(
    monkeypatch: pytest.MonkeyPatch,
    plugin: Any = None,
    *,
    send_image: Any = None,
    send_text: Any = None,
) -> None:
    """放开插件推送方法的按频道判据（并可选地把推送转给记录函数）

    本文件判的是渲染调度，不是作用域。判据本身由 `tests/test_plugin_background_scope.py`
    覆盖；这里替换掉，避免测试替身上下文被拖进真实数据库查询。给的替身都要返回 `True`，
    否则调用方会按「频道不存在」处理。
    """
    from nekro_agent.services.plugin import base as plugin_base

    async def _effective(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(plugin_base, "plugin_effective_in_chat", _effective)
    if plugin is None:
        return

    if send_image is not None:

        async def _send_image(*args: Any, **kwargs: Any) -> bool:
            await send_image(*args, **kwargs)
            return True

        monkeypatch.setattr(plugin, "send_image", _send_image)

    if send_text is not None:

        async def _send_text(*args: Any, **kwargs: Any) -> bool:
            await send_text(*args, **kwargs)
            return True

        monkeypatch.setattr(plugin, "send_text", _send_text)


def make_plugin(module_name: str) -> NekroPlugin:
    """构造一个不落盘的最小插件实例

    `NekroPlugin.__init__` 只算路径不建目录，也不读配置文件，因此可以直接用真身而不是替身：
    护栏读的是 `plugin.key` / `plugin.name` / `plugin.logger`，替身反而会让这些字段失真。
    """
    return NekroPlugin(
        name=f"测试插件-{module_name}",
        module_name=module_name,
        description="故障隔离测试用",
        version="0.0.1",
        author="tester",
        url="",
    )


@pytest.fixture(autouse=True)
def clean_isolation_state(monkeypatch: pytest.MonkeyPatch):
    """每个用例都从干净的隔离状态开始，并用确定的阈值取代用户配置"""
    isolation.reset_all()
    monkeypatch.setattr(config, "PLUGIN_CALL_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(config, "PLUGIN_ISOLATION_FAILURE_THRESHOLD", 3)
    monkeypatch.setattr(config, "PLUGIN_ISOLATION_COOLDOWN_SECONDS", 600.0)
    monkeypatch.setattr(config, "PLUGIN_BLOCKING_WARN_SECONDS", 0.0)
    yield
    isolation.reset_all()


class TestCallGuard:
    """单次插件调用的护栏行为"""

    async def test_exception_falls_back_to_default(self):
        plugin = make_plugin("boom")

        async def boom(_ctx: Any) -> str:
            raise RuntimeError("插件炸了")

        assert await isolation.call_plugin(plugin, "prompt_inject", boom, None, default="") == ""

    async def test_stuck_callback_times_out_and_is_cancelled(self):
        """卡住不返回的回调必须被超时切断，而不是无限挂起调用方

        故意让回调等一个永远不会被 set 的 Event：唯一的出路就是超时。
        不比较耗时、不靠 sleep 竞速，因此结果是确定的。
        """
        plugin = make_plugin("stuck")
        entered = asyncio.Event()
        never = asyncio.Event()
        cancelled = False

        async def stuck(_ctx: Any) -> str:
            nonlocal cancelled
            entered.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise
            return "unreachable"

        result = await isolation.call_plugin(plugin, "prompt_inject", stuck, None, default="", timeout=0.05)

        assert result == ""
        assert entered.is_set(), "回调应当真的被调用过"
        assert cancelled, "超时后应当取消卡住的回调，而不是放任它继续占着资源"

    async def test_zero_timeout_disables_the_deadline(self):
        """超时设为 0 表示不限时：正常返回的回调不受影响"""
        plugin = make_plugin("slow_but_fine")

        async def slow(_ctx: Any) -> str:
            await asyncio.sleep(0)
            return "done"

        assert await isolation.call_plugin(plugin, "prompt_inject", slow, None, default="", timeout=0) == "done"

    async def test_successful_call_returns_plugin_value(self):
        plugin = make_plugin("healthy")

        async def ok(value: str) -> str:
            return value.upper()

        assert await isolation.call_plugin(plugin, "prompt_inject", ok, "hi", default="") == "HI"


class TestQuarantineAccounting:
    """失败计数与自动隔离的阈值边界"""

    @pytest.mark.parametrize(
        ("threshold", "failures", "expect_quarantined"),
        [
            (3, 2, False),  # 差一次，不隔离
            (3, 3, True),  # 正好到阈值
            (3, 4, True),  # 超过阈值
            (1, 1, True),  # 阈值下界
            (0, 9, False),  # 阈值为 0 表示只告警不隔离
        ],
    )
    def test_threshold_boundary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        threshold: int,
        failures: int,
        expect_quarantined: bool,
    ):
        monkeypatch.setattr(config, "PLUGIN_ISOLATION_FAILURE_THRESHOLD", threshold)
        for _ in range(failures):
            isolation.record_failure("tester.counted", "on_user_message", isolation.FAILURE_REASON_ERROR, "boom", now=100.0)

        assert (isolation.get_quarantine("tester.counted", now=100.0) is not None) is expect_quarantined

    def test_success_resets_the_consecutive_counter(self):
        """偶发失败不该在几天后攒够数量把一个正常插件隔离掉"""
        key = "tester.flaky"
        for _ in range(2):
            isolation.record_failure(key, "on_user_message", isolation.FAILURE_REASON_ERROR, "boom", now=100.0)
        isolation.record_success(key)
        isolation.record_failure(key, "on_user_message", isolation.FAILURE_REASON_ERROR, "boom", now=100.0)

        assert isolation.get_quarantine(key, now=100.0) is None

    def test_quarantine_records_the_failing_stage(self):
        entry = None
        for _ in range(3):
            entry = isolation.record_failure(
                "tester.recorded",
                "prompt_inject",
                isolation.FAILURE_REASON_TIMEOUT,
                "超过 30.0s 未返回",
                now=1000.0,
            )

        assert entry is not None
        assert (entry.stage, entry.reason, entry.failures) == ("prompt_inject", isolation.FAILURE_REASON_TIMEOUT, 3)
        assert (entry.since, entry.until) == (1000.0, 1600.0)

    @pytest.mark.parametrize(
        ("now", "still_quarantined"),
        [
            (1000.0, True),  # 刚被隔离
            (1599.9, True),  # 冷却期内
            (1600.0, False),  # 冷却期满，就地解除
            (9999.0, False),
        ],
    )
    def test_cooldown_expiry_restores_the_plugin(self, now: float, still_quarantined: bool):
        for _ in range(3):
            isolation.record_failure("tester.cooldown", "on_user_message", isolation.FAILURE_REASON_ERROR, "boom", now=1000.0)

        assert (isolation.get_quarantine("tester.cooldown", now=now) is not None) is still_quarantined

    def test_manual_release_clears_state_immediately(self):
        """用户修好插件重载后必须能立刻恢复，不用等满冷却期"""
        key = "tester.released"
        for _ in range(3):
            isolation.record_failure(key, "on_user_message", isolation.FAILURE_REASON_ERROR, "boom", now=1000.0)

        assert isolation.release_quarantine(key, reason="插件已重新加载") is True
        assert isolation.get_quarantine(key, now=1000.0) is None
        # 计数也一并清零：解除之后再失败一次不该立刻又被隔离
        isolation.record_failure(key, "on_user_message", isolation.FAILURE_REASON_ERROR, "boom", now=1000.0)
        assert isolation.get_quarantine(key, now=1000.0) is None

    def test_release_reports_whether_it_was_quarantined(self):
        assert isolation.release_quarantine("tester.never_failed", reason="插件已重新加载") is False


class TestQuarantineIsScopedToOnePlugin:
    """隔离只作用于出问题的那一个插件"""

    async def test_quarantined_plugin_is_skipped_without_being_called(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(config, "PLUGIN_ISOLATION_FAILURE_THRESHOLD", 2)
        plugin = make_plugin("repeat_offender")
        calls = 0

        async def boom(_ctx: Any) -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError("插件炸了")

        for _ in range(2):
            await isolation.call_plugin(plugin, "prompt_inject", boom, None, default="")
        assert calls == 2

        # 第三轮起，坏插件不再被调用——这正是"不要每轮都拖一次"的点
        assert await isolation.call_plugin(plugin, "prompt_inject", boom, None, default="") == ""
        assert calls == 2

    async def test_other_plugins_keep_working(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(config, "PLUGIN_ISOLATION_FAILURE_THRESHOLD", 1)
        bad = make_plugin("bad_one")
        good = make_plugin("good_one")

        async def boom(_ctx: Any) -> str:
            raise RuntimeError("插件炸了")

        async def fine(_ctx: Any) -> str:
            return "healthy"

        await isolation.call_plugin(bad, "prompt_inject", boom, None, default="")

        assert isolation.get_quarantine(bad.key) is not None
        assert isolation.get_quarantine(good.key) is None
        assert await isolation.call_plugin(good, "prompt_inject", fine, None, default="") == "healthy"


class TestMessageDispatchIsolation:
    """消息分发点：一个插件坏掉，其余插件与主链路照常"""

    @pytest.fixture
    def loaded(self, monkeypatch: pytest.MonkeyPatch):
        """把测试插件塞进收集器，用完还原

        `loaded_plugins` 是进程级全局字典，不还原会污染后续用例。
        """
        registry: dict = {}
        monkeypatch.setattr(plugin_collector, "loaded_plugins", registry)

        # 本文件测的是故障隔离，不测作用域：把「插件在频道生效」的判据替身成恒真，
        # 否则这些没有频道行、也不在全局启用列表里的测试插件会被新钩子直接跳过。
        # 判据是 `collector` 模块级名字（收集器实例上没有），所以要打在模块对象上。
        from nekro_agent.services.plugin import collector as collector_module

        async def _effective(*_args: Any, **_kwargs: Any) -> bool:
            return True

        async def _instance(_chat_key: str) -> str:
            return ""

        monkeypatch.setattr(collector_module, "plugin_effective_with_instance", _effective)
        monkeypatch.setattr(collector_module, "resolve_chat_plugin_instance", _instance)
        return registry

    async def test_raising_plugin_does_not_stop_the_others(self, loaded: dict):
        seen: List[str] = []

        bad = make_plugin("dispatch_bad")
        good = make_plugin("dispatch_good")

        async def boom(_ctx: Any, _msg: ChatMessage) -> Optional[MsgSignal]:
            seen.append("bad")
            raise RuntimeError("插件炸了")

        async def fine(_ctx: Any, _msg: ChatMessage) -> Optional[MsgSignal]:
            seen.append("good")
            return MsgSignal.CONTINUE

        bad.on_user_message_method = boom
        good.on_user_message_method = fine
        loaded[bad.key] = bad
        loaded[good.key] = good

        signal = await plugin_collector.handle_on_user_message(None, ChatMessage.create_empty(CHAT_KEY))  # pyright: ignore[reportArgumentType]

        assert seen == ["bad", "good"], "坏插件抛异常不能让排在它后面的插件收不到消息"
        assert signal == MsgSignal.CONTINUE

    async def test_stuck_plugin_does_not_hang_the_chain(self, loaded: dict, monkeypatch: pytest.MonkeyPatch):
        """卡住的插件被超时切断后，同一轮里的其他插件仍然会被调用"""
        monkeypatch.setattr(config, "PLUGIN_CALL_TIMEOUT_SECONDS", 0.05)
        never = asyncio.Event()
        seen: List[str] = []

        stuck = make_plugin("dispatch_stuck")
        good = make_plugin("dispatch_after_stuck")

        async def hang(_ctx: Any, _msg: ChatMessage) -> Optional[MsgSignal]:
            seen.append("stuck")
            await never.wait()
            return MsgSignal.BLOCK_ALL

        async def fine(_ctx: Any, _msg: ChatMessage) -> Optional[MsgSignal]:
            seen.append("good")
            return MsgSignal.CONTINUE

        stuck.on_user_message_method = hang
        good.on_user_message_method = fine
        loaded[stuck.key] = stuck
        loaded[good.key] = good

        signal = await plugin_collector.handle_on_user_message(None, ChatMessage.create_empty(CHAT_KEY))  # pyright: ignore[reportArgumentType]

        assert seen == ["stuck", "good"]
        # 卡住的插件回落到 CONTINUE：它对本轮没有发言权，但也不会拦下消息
        assert signal == MsgSignal.CONTINUE

    async def test_quarantined_plugin_cannot_block_messages(self, loaded: dict, monkeypatch: pytest.MonkeyPatch):
        """被隔离的插件不再有拦截权，否则一个坏插件能把所有实例的消息全部拦下"""
        monkeypatch.setattr(config, "PLUGIN_ISOLATION_FAILURE_THRESHOLD", 1)
        blocker = make_plugin("dispatch_blocker")
        calls = 0

        async def boom_then_block(_ctx: Any, _msg: ChatMessage) -> Optional[MsgSignal]:
            nonlocal calls
            calls += 1
            raise RuntimeError("插件炸了")

        blocker.on_user_message_method = boom_then_block
        loaded[blocker.key] = blocker

        message = ChatMessage.create_empty(CHAT_KEY)
        assert await plugin_collector.handle_on_user_message(None, message) == MsgSignal.CONTINUE  # pyright: ignore[reportArgumentType]
        assert await plugin_collector.handle_on_user_message(None, message) == MsgSignal.CONTINUE  # pyright: ignore[reportArgumentType]
        assert calls == 1, "隔离后不该再调用它"


class TestPromptInjectIsolation:
    """提示注入直接坐在主对话链路上，坏掉时只能少一段上下文，不能少一次回复"""

    async def test_failing_inject_degrades_to_empty_section(self):
        from nekro_agent.services.plugin.schema import PromptInjectMethod

        plugin = make_plugin("inject_bad")

        async def boom(_ctx: Any) -> str:
            raise RuntimeError("插件炸了")

        plugin.prompt_inject_method = PromptInjectMethod("bad", "", boom)

        assert await plugin.render_inject_prompt(None) == ""  # pyright: ignore[reportArgumentType]

    async def test_failing_collect_methods_hides_methods_instead_of_exposing_all(self):
        """收集函数坏掉时回落到"一个都不给"

        这个钩子的用途正是按上下文隐藏方法，失败时放开等于把本该隐藏的能力交给模型。
        """
        plugin = make_plugin("collect_bad")

        async def boom(_ctx: Any) -> list:
            raise RuntimeError("插件炸了")

        plugin.sandbox_methods = ["should_not_leak"]  # pyright: ignore[reportAttributeAccessIssue]
        plugin._collect_methods_func = boom  # noqa: SLF001  # pyright: ignore[reportAttributeAccessIssue]

        assert await plugin.collect_available_methods(None) == []  # pyright: ignore[reportArgumentType]


class TestOfflineRenderScheduling:
    """离线渲染的调度契约：Pillow 作画必须离开事件循环线程

    实测（本机 Python 3.11 + Pillow 12）：好感度榜单单次渲染 506ms，同步跑时事件循环
    最大停顿 780ms；改走线程后最大停顿降到 19ms（等于空载基线）。心声卡片对应 54ms → 17ms。
    这条用例钉的是"渲染不在事件循环线程上发生"，不比较耗时。
    """

    async def test_inner_voice_card_is_rendered_off_the_loop_thread(self, monkeypatch: pytest.MonkeyPatch):
        from plugins.builtin import inner_voice as voice

        render_thread: List[int] = []

        def fake_render(body: str) -> bytes:
            render_thread.append(threading.get_ident())
            return b"png"

        class StubCtx:
            from_chat_key = CHAT_KEY

            def __init__(self) -> None:
                self.fs = self
                self.sent: List[str] = []

            async def mixed_forward_file(self, file: Any, file_name: str = "") -> str:
                return f"/sandbox/{file_name}"

            async def send_image(self, file_path: str) -> None:
                self.sent.append(file_path)

        monkeypatch.setattr(voice, "HAS_IMAGE_CARD", True)
        monkeypatch.setattr(voice, "fonts_available", lambda: True)
        monkeypatch.setattr(voice, "render_inner_voice_card", fake_render)

        ctx = StubCtx()

        async def record_send_image(_chat_key: str, file_path: str, _ctx: Any, **_kw: Any) -> None:
            ctx.sent.append(file_path)

        _allow_every_plugin_push(monkeypatch, voice.plugin, send_image=record_send_image)

        await voice.render_and_send(ctx, "测试心声")  # pyright: ignore[reportArgumentType]

        assert len(ctx.sent) == 1, "渲染结果仍要正常发出去"
        assert render_thread == [render_thread[0]]
        assert render_thread[0] != threading.get_ident(), "渲染不能发生在事件循环线程上"

    async def test_affection_rank_card_is_rendered_off_the_loop_thread(self, monkeypatch: pytest.MonkeyPatch):
        from plugins.builtin import affection_rank as rank

        render_thread: List[int] = []

        def fake_render(view: Any, *, subtitle: str) -> bytes:
            render_thread.append(threading.get_ident())
            return b"png"

        class StubChannel:
            chat_type = rank.ChatType.GROUP

            @staticmethod
            async def get_channel(chat_key: str) -> "StubChannel":
                return StubChannel()

        class StubCtx:
            def __init__(self) -> None:
                self.fs = self
                self.ms = self
                self.sent: List[str] = []
                # 沙盒方法只对发起会话自己出榜，替身也得有归属，否则连归属校验都过不去
                self.from_chat_key = CHAT_KEY

            async def mixed_forward_file(self, file: Any, file_name: str = "") -> str:
                return f"/sandbox/{file_name}"

            async def send_image(self, chat_key: str, file_path: str, _ctx: Any) -> None:
                self.sent.append(file_path)

        from nekro_agent.services.imagecard.affection_rank import RankEntry

        view = rank.build_rank_view([RankEntry(person_uid="10001", nickname="阿花", score=120)], total_active=3)

        async def fake_build_view(*_args: Any, **_kwargs: Any):
            return view, "群 123456 · 近30天活跃 3 人"

        monkeypatch.setattr(rank, "DBChatChannel", StubChannel)
        monkeypatch.setattr(rank, "fonts_available", lambda: True)
        monkeypatch.setattr(rank, "render_rank_card", fake_render)
        monkeypatch.setattr(rank, "_build_view", fake_build_view)

        ctx = StubCtx()

        async def record_send_image(_chat_key: str, file_path: str, _ctx: Any, **_kw: Any) -> None:
            ctx.sent.append(file_path)

        _allow_every_plugin_push(monkeypatch, rank.plugin, send_image=record_send_image)

        await rank.send_affection_rank(ctx, CHAT_KEY)  # pyright: ignore[reportArgumentType]

        assert len(ctx.sent) == 1
        assert render_thread[0] != threading.get_ident(), "渲染不能发生在事件循环线程上"
