"""记忆沉淀调度器在多频道同时活跃时的公平性与不停摆

## 现场

全局打开记忆之后，十几个群同时有人说话，其中几个群积压几百条。调度器整机停摆了四十多分钟：
没有任何频道开始沉淀，安静的频道积压几十条也一直轮不到。原因有三处，各钉一条：

1. **同一频道重复入队**：每来一条消息就往堆里再推一个新版本的任务，作废的旧版本要等弹出时才清，
   队列长度随消息量线性增长（`TestOneEntryPerChannel`）。
2. **延迟按队列长度算**：新任务的计划时间是「队列长度 × 错峰间隔」之后，队列一长，新任务就排到
   几十分钟以后（`TestDelayDoesNotGrowWithQueue`）。
3. **主循环抱着队头 sleep**：弹出一个任务后原地睡到它的计划时间，这期间到期的其他任务全都动不了
   （`TestLoopDoesNotHoldTheHead`）。

再加上队列按（优先级，时间）排序，积压多的群每来一条消息就推一个 URGENT，HIGH / LOW 永远轮不到
（`TestAgingPreventsStarvation`）。`TestBusyGroupsDoNotStallQuietChannel` 把这几样放在一起复现现场。

时间都按比例缩小：错峰间隔 0.05 秒、老化步长 0.2 秒，用例在几秒内跑完。
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, List, Tuple

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.memory import scheduler as scheduler_module
from nekro_agent.services.memory.consolidator import ConsolidationResult
from nekro_agent.services.memory.scheduler import (
    ConsolidationPriority,
    ConsolidationTask,
    MemoryMaintenanceScheduler,
)

STAGGER = 0.05
AGING = 0.2

QUIET_CHAT = "web-session_quiet0001"
BUSY: List[Tuple[str, str, str]] = [
    (f"onebot_v11-inst{index % 3}-group_{index}", "onebot_v11", f"inst{index % 3}") for index in range(13)
]


@pytest.fixture(autouse=True)
def busy_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """闸开、记忆开、阈值调小；错峰与老化按比例缩短"""
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MSG_THRESHOLD", 2)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_TIME_THRESHOLD_HOURS", 6)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_SIZE", 50)
    monkeypatch.setattr(config, "MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS", STAGGER)
    monkeypatch.setattr(config, "MEMORY_SCHEDULER_MAX_CONCURRENT", 3)
    # 修复前的模块里没有这两个常量；`raising=False` 让用例在修复前照样跑到断言上，失败在行为上
    monkeypatch.setattr(scheduler_module, "AGING_STEP_SECONDS", AGING, raising=False)
    monkeypatch.setattr(scheduler_module, "LOOP_IDLE_CHECK_SECONDS", 0.05, raising=False)

    # 这一组只测排队，不建库：跑完一轮后「按库里游标校正积压」那一步直接回落到内存计数。
    # 不桩掉的话，同一进程里前面的用例关掉的 sqlite 连接会让那次查询等到超时。
    async def no_db_backlog(_self: MemoryMaintenanceScheduler, _chat_key: str) -> None:
        return None

    monkeypatch.setattr(MemoryMaintenanceScheduler, "_db_backlog", no_db_backlog, raising=False)


def queued(scheduler: MemoryMaintenanceScheduler) -> List[ConsolidationTask]:
    """队列里现有的条目（修复前是堆）"""
    return scheduler.queued_tasks()


async def wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class _Loop:
    """在用例里跑真实主循环，结束时收干净"""

    def __init__(self, scheduler: MemoryMaintenanceScheduler) -> None:
        self.scheduler = scheduler
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> "_Loop":
        self.scheduler._running = True  # noqa: SLF001
        self.task = asyncio.create_task(self.scheduler._run_loop())  # noqa: SLF001
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.scheduler._running = False  # noqa: SLF001
        self.scheduler._wakeup.set()  # noqa: SLF001
        if self.task is not None:
            self.task.cancel()
        for exec_task in list(self.scheduler._execution_tasks):  # noqa: SLF001
            exec_task.cancel()
        for timer, _due in list(self.scheduler._retry_timers.values()):  # noqa: SLF001
            timer.cancel()
        await asyncio.sleep(0)


async def schedule(
    scheduler: MemoryMaintenanceScheduler,
    chat_key: str,
    priority: ConsolidationPriority,
    *,
    extra_delay: float = 0.0,
) -> None:
    async with scheduler._lock:  # noqa: SLF001
        await scheduler._schedule_consolidation(0, chat_key, priority, extra_delay_seconds=extra_delay)  # noqa: SLF001


class TestOneEntryPerChannel:
    @pytest.mark.asyncio
    async def test_two_hundred_messages_leave_one_entry(self) -> None:
        scheduler = MemoryMaintenanceScheduler()
        chat_key, adapter_key, instance_key = BUSY[0]
        for _ in range(200):
            await scheduler.on_new_message(chat_key, 0, adapter_key=adapter_key, instance_key=instance_key)

        entries = queued(scheduler)
        assert len(entries) == 1, "同一频道只留一个条目，新消息只更新它"
        assert entries[0].chat_key == chat_key

    @pytest.mark.asyncio
    async def test_update_keeps_the_most_urgent_priority_and_earliest_due(self) -> None:
        scheduler = MemoryMaintenanceScheduler()
        await schedule(scheduler, QUIET_CHAT, ConsolidationPriority.HIGH, extra_delay=30)
        await schedule(scheduler, QUIET_CHAT, ConsolidationPriority.LOW)

        [entry] = queued(scheduler)
        assert entry.priority == ConsolidationPriority.HIGH, "后来的低优先级不能把已有的高优先级降下去"
        assert entry.scheduled_at <= time.time() + 0.5, "最早可执行时间取两者里早的那个"

    @pytest.mark.asyncio
    async def test_update_does_not_reset_how_long_it_has_waited(self) -> None:
        scheduler = MemoryMaintenanceScheduler()
        await schedule(scheduler, QUIET_CHAT, ConsolidationPriority.LOW)
        [entry] = queued(scheduler)
        entry.enqueued_at = time.time() - 10 * AGING

        await schedule(scheduler, QUIET_CHAT, ConsolidationPriority.LOW)

        [entry] = queued(scheduler)
        assert entry.enqueued_at < time.time() - 5 * AGING, "零星来消息的频道每来一条就从头算等待，永远老化不了"


class TestDelayDoesNotGrowWithQueue:
    @pytest.mark.asyncio
    async def test_hundredth_channel_is_due_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS", 5)
        scheduler = MemoryMaintenanceScheduler()
        for index in range(100):
            await schedule(scheduler, f"onebot_v11-inst1-group_{index}", ConsolidationPriority.NORMAL)

        latest_due = max(entry.scheduled_at for entry in queued(scheduler))
        assert latest_due <= time.time() + 0.5, "修复前第 100 个频道要等 99 × 5 秒"


class TestLoopDoesNotHoldTheHead:
    @pytest.mark.asyncio
    async def test_due_entry_runs_while_an_urgent_one_waits_for_its_time(self) -> None:
        scheduler = MemoryMaintenanceScheduler()
        ran: List[str] = []

        async def handler(_workspace_id: int, chat_key: str) -> ConsolidationResult:
            ran.append(chat_key)
            return ConsolidationResult()

        scheduler.set_consolidation_handler(handler)  # type: ignore[arg-type]
        # 直接调 `_schedule_consolidation` 不经过 `on_new_message`，实例映射要自己登记，
        # 否则闸开时 `_execute_task` 会去库里查频道
        scheduler._chat_instance_map["onebot_v11-inst1-group_far"] = ("onebot_v11", "inst1")  # noqa: SLF001
        scheduler._chat_instance_map[QUIET_CHAT] = ("web", "")  # noqa: SLF001
        async with _Loop(scheduler):
            # URGENT 但半分钟后才到期：修复前主循环先弹出它、原地睡半分钟
            await schedule(scheduler, "onebot_v11-inst1-group_far", ConsolidationPriority.URGENT, extra_delay=30)
            await asyncio.sleep(0.1)
            await schedule(scheduler, QUIET_CHAT, ConsolidationPriority.LOW)

            assert await wait_until(lambda: QUIET_CHAT in ran, timeout=1.5)
            assert "onebot_v11-inst1-group_far" not in ran, "没到期的不能提前跑"


class TestAgingPreventsStarvation:
    @pytest.mark.asyncio
    async def test_urgent_still_goes_first_between_fresh_entries(self) -> None:
        scheduler = MemoryMaintenanceScheduler()
        await schedule(scheduler, "onebot_v11-inst1-group_low", ConsolidationPriority.LOW)
        await schedule(scheduler, "onebot_v11-inst1-group_urgent", ConsolidationPriority.URGENT)

        first = await scheduler._get_next_task()  # noqa: SLF001
        assert first is not None
        assert first.chat_key == "onebot_v11-inst1-group_urgent"

    @pytest.mark.asyncio
    async def test_a_long_waiting_low_entry_beats_a_fresh_urgent_one(self) -> None:
        scheduler = MemoryMaintenanceScheduler()
        await schedule(scheduler, "onebot_v11-inst1-group_low", ConsolidationPriority.LOW)
        await schedule(scheduler, "onebot_v11-inst1-group_urgent", ConsolidationPriority.URGENT)
        for entry in queued(scheduler):
            if entry.chat_key == "onebot_v11-inst1-group_low":
                entry.enqueued_at = time.time() - 10 * AGING  # 等了好几个老化步长

        first = await scheduler._get_next_task()  # noqa: SLF001
        assert first is not None
        assert first.chat_key == "onebot_v11-inst1-group_low", "等得够久的低优先级必须轮到，否则会被饿死"


class TestBusyGroupsDoNotStallQuietChannel:
    @pytest.mark.asyncio
    async def test_quiet_channel_is_consolidated_while_busy_groups_keep_flooding(self) -> None:
        scheduler = MemoryMaintenanceScheduler()
        ran: List[str] = []

        async def handler(_workspace_id: int, chat_key: str) -> ConsolidationResult:
            ran.append(chat_key)
            await asyncio.sleep(0.02)
            # 每轮只消化一条：积压多的群始终保持 URGENT
            return ConsolidationResult(messages_processed=1)

        scheduler.set_consolidation_handler(handler)  # type: ignore[arg-type]
        stop = asyncio.Event()

        async def feed_busy_groups() -> None:
            while not stop.is_set():
                for chat_key, adapter_key, instance_key in BUSY:
                    await scheduler.on_new_message(chat_key, 0, adapter_key=adapter_key, instance_key=instance_key)
                await asyncio.sleep(0.01)

        async with _Loop(scheduler):
            feeder = asyncio.create_task(feed_busy_groups())
            try:
                await asyncio.sleep(0.5)
                for _ in range(3):
                    await scheduler.on_new_message(QUIET_CHAT, 0, adapter_key="web", instance_key="")

                assert await wait_until(lambda: QUIET_CHAT in ran, timeout=4), (
                    "十几个群持续刷屏时，安静频道的沉淀必须在有限时间内轮到"
                )
                assert len(queued(scheduler)) <= len(BUSY) + 1, "队列长度不随消息量增长"
            finally:
                stop.set()
                feeder.cancel()


class TestBlockedEntriesWaitForRelease:
    @pytest.mark.asyncio
    async def test_same_instance_channel_runs_right_after_the_running_one_finishes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 自检间隔调长：这里要靠「一轮跑完就叫醒主循环」，而不是靠定时自检碰巧醒过来
        monkeypatch.setattr(scheduler_module, "LOOP_IDLE_CHECK_SECONDS", 5.0, raising=False)
        scheduler = MemoryMaintenanceScheduler()
        release = asyncio.Event()
        ran: List[Tuple[str, float]] = []
        first_chat, adapter_key, instance_key = BUSY[0]
        second_chat = "onebot_v11-inst0-group_second"

        async def handler(_workspace_id: int, chat_key: str) -> ConsolidationResult:
            ran.append((chat_key, time.monotonic()))
            if chat_key == first_chat:
                await release.wait()
            return ConsolidationResult()

        scheduler.set_consolidation_handler(handler)  # type: ignore[arg-type]
        async with _Loop(scheduler):
            await scheduler.on_new_message(first_chat, 0, adapter_key=adapter_key, instance_key=instance_key)
            assert await wait_until(lambda: any(chat == first_chat for chat, _ in ran), timeout=1)
            await scheduler.on_new_message(second_chat, 0, adapter_key=adapter_key, instance_key=instance_key)
            await asyncio.sleep(0.3)
            assert [chat for chat, _ in ran] == [first_chat], "同实例在跑时不能并发"

            released_at = time.monotonic()
            release.set()
            assert await wait_until(lambda: any(chat == second_chat for chat, _ in ran), timeout=1.5)
            second_started = next(at for chat, at in ran if chat == second_chat)
            assert second_started - released_at < 1.0, "前一个跑完就该轮到，不等新消息、不等固定退避"


class TestDispatchPacing:
    @pytest.mark.asyncio
    async def test_dispatches_are_spaced_by_the_stagger_gap(self) -> None:
        scheduler = MemoryMaintenanceScheduler()
        started: List[float] = []

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            started.append(time.monotonic())
            return ConsolidationResult()

        scheduler.set_consolidation_handler(handler)  # type: ignore[arg-type]
        async with _Loop(scheduler):
            for index in range(4):
                await scheduler.on_new_message(
                    f"onebot_v11-inst{index}-group_pace",
                    0,
                    adapter_key="onebot_v11",
                    instance_key=f"inst{index}",
                )
            assert await wait_until(lambda: len(started) == 4, timeout=2)

        gaps = [later - earlier for earlier, later in zip(started, started[1:])]
        assert min(gaps) >= STAGGER * 0.8, gaps

    @pytest.mark.asyncio
    async def test_entries_stay_queued_while_concurrency_is_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """并发满了就不往外派：派出去的项只会在信号量上干等，留在队列里还能继续合并、老化"""
        monkeypatch.setattr(config, "MEMORY_SCHEDULER_MAX_CONCURRENT", 1)
        scheduler = MemoryMaintenanceScheduler()
        release = asyncio.Event()
        started: List[str] = []

        async def handler(_workspace_id: int, chat_key: str) -> ConsolidationResult:
            started.append(chat_key)
            await release.wait()
            return ConsolidationResult()

        scheduler.set_consolidation_handler(handler)  # type: ignore[arg-type]
        async with _Loop(scheduler):
            await scheduler.on_new_message("onebot_v11-inst1-group_a", 0, adapter_key="onebot_v11", instance_key="inst1")
            assert await wait_until(lambda: started == ["onebot_v11-inst1-group_a"], timeout=1)
            await scheduler.on_new_message("onebot_v11-inst2-group_b", 0, adapter_key="onebot_v11", instance_key="inst2")
            await asyncio.sleep(0.3)

            assert [entry.chat_key for entry in queued(scheduler)] == ["onebot_v11-inst2-group_b"]
            release.set()
            assert await wait_until(lambda: "onebot_v11-inst2-group_b" in started, timeout=1.5)


class TestQueueReport:
    @pytest.mark.asyncio
    async def test_reports_queue_length_and_oldest_wait_at_most_once_per_interval(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(scheduler_module, "QUEUE_REPORT_INTERVAL_SECONDS", 0.2, raising=False)
        lines: List[str] = []

        class _Recorder:
            def info(self, message: str, *_args: object, **_kwargs: object) -> None:
                lines.append(message)

            def __getattr__(self, _name: str) -> Callable[..., None]:
                return lambda *_a, **_k: None

        monkeypatch.setattr(scheduler_module, "logger", _Recorder())
        scheduler = MemoryMaintenanceScheduler()
        release = asyncio.Event()
        first_chat, adapter_key, instance_key = BUSY[0]

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            await release.wait()
            return ConsolidationResult()

        scheduler.set_consolidation_handler(handler)  # type: ignore[arg-type]
        async with _Loop(scheduler):
            await scheduler.on_new_message(first_chat, 0, adapter_key=adapter_key, instance_key=instance_key)
            await asyncio.sleep(0.1)
            # 同实例的第二个频道被占着，一直排在队里
            await scheduler.on_new_message(
                "onebot_v11-inst0-group_waiting",
                0,
                adapter_key=adapter_key,
                instance_key=instance_key,
            )
            await asyncio.sleep(0.75)
            release.set()

        reports = [line for line in lines if line.startswith("记忆沉淀队列")]
        assert 2 <= len(reports) <= 5, reports
        assert "排队 1 个频道" in reports[-1]
        assert "最久的已等" in reports[-1]
        assert "onebot_v11-inst0-group_waiting" in reports[-1]
