"""沉淀调度器的结果表：成功、跳过、只初始化游标、失败、异常各走哪条路。

改造前处理器什么都不返回，调度器只能把「跑完」当成「处理完」：失败也清 pending、
也记 last_consolidation_at，于是坏掉的一批消息被永久跳过。改造后处理器返回
`ConsolidationResult`，调度器按结果分别处理：

| 结果 | pending | last_consolidation_at | 重排 |
| --- | --- | --- | --- |
| 成功 | 减 messages_processed | 记 | 批次取满且仍有积压时排一次 |
| 跳过 | 减 messages_processed（通常 0） | 记 | 不排 |
| 只初始化游标 | 清零 | 记 | 不排 |
| 失败 / 异常 | 不动 | 不动 | 退避后排 |

退避期内 `_compute_priority` 必须返回 None，否则新消息会把失败任务立刻拉回来，
退避等于没做。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import AsyncIterator, List

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.memory.consolidator import ConsolidationResult
from nekro_agent.services.memory.scheduler import (
    ChannelActivityStats,
    ConsolidationPriority,
    ConsolidationTask,
    MemoryMaintenanceScheduler,
)

WS = 9301
CHAT = "onebot_v11-sched-group_1"


@pytest.fixture
async def scheduler() -> AsyncIterator[MemoryMaintenanceScheduler]:
    instance = MemoryMaintenanceScheduler()
    yield instance
    # 退避定时器是真实的 asyncio 任务，用例结束时要收掉，免得在事件循环关闭后才被回收。
    for timer, _due_ts in list(instance._retry_timers.values()):  # noqa: SLF001
        timer.cancel()
    instance._retry_timers.clear()  # noqa: SLF001


@pytest.fixture(autouse=True)
def fast_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_SIZE", 5)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_INTERVAL_SECONDS", 30)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MSG_THRESHOLD", 3)
    monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_TIME_THRESHOLD_HOURS", 6)


def install_task(scheduler: MemoryMaintenanceScheduler, pending: int) -> ConsolidationTask:
    scheduler._versions[(WS, CHAT)] = 0  # noqa: SLF001
    scheduler._channel_stats[(WS, CHAT)] = ChannelActivityStats(  # noqa: SLF001
        workspace_id=WS,
        chat_key=CHAT,
        pending_messages=pending,
    )
    return ConsolidationTask(
        priority=ConsolidationPriority.NORMAL,
        scheduled_at=time.time(),
        workspace_id=WS,
        chat_key=CHAT,
        version=0,
    )


def heap_tasks(scheduler: MemoryMaintenanceScheduler) -> List[ConsolidationTask]:
    return scheduler.queued_tasks()


class TestSuccess:
    async def test_success_subtracts_processed_and_sets_last_run(self, scheduler: MemoryMaintenanceScheduler) -> None:
        task = install_task(scheduler, pending=12)

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            return ConsolidationResult(messages_processed=5, paragraphs_created=1)

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001

        stats = scheduler._channel_stats[(WS, CHAT)]  # noqa: SLF001
        assert stats.pending_messages == 7
        assert stats.last_consolidation_at is not None
        assert stats.consecutive_skips == 0
        # 批次取满、仍有积压：立刻再排一次普通优先级。
        assert len(heap_tasks(scheduler)) == 1
        assert heap_tasks(scheduler)[0].priority == ConsolidationPriority.NORMAL

    async def test_success_without_backlog_does_not_reschedule(self, scheduler: MemoryMaintenanceScheduler) -> None:
        task = install_task(scheduler, pending=3)

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            return ConsolidationResult(messages_processed=5)

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001

        assert scheduler._channel_stats[(WS, CHAT)].pending_messages == 0  # noqa: SLF001
        assert heap_tasks(scheduler) == []


class TestSkipAndCursor:
    async def test_skip_keeps_pending_but_records_last_run(self, scheduler: MemoryMaintenanceScheduler) -> None:
        task = install_task(scheduler, pending=4)

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            return ConsolidationResult(messages_processed=0, skipped_reason="no_new_messages")

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001

        stats = scheduler._channel_stats[(WS, CHAT)]  # noqa: SLF001
        assert stats.pending_messages == 4
        assert stats.last_consolidation_at is not None
        assert heap_tasks(scheduler) == []

    async def test_memory_disabled_skip_clears_pending(self, scheduler: MemoryMaintenanceScheduler) -> None:
        task = install_task(scheduler, pending=4)

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            return ConsolidationResult(messages_processed=0, skipped_reason="memory_disabled")

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001

        stats = scheduler._channel_stats[(WS, CHAT)]  # noqa: SLF001
        assert stats.pending_messages == 0
        assert stats.last_consolidation_at is not None

    async def test_cursor_initialization_zeroes_pending(self, scheduler: MemoryMaintenanceScheduler) -> None:
        task = install_task(scheduler, pending=4)

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            return ConsolidationResult(cursor_initialized=True)

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001

        stats = scheduler._channel_stats[(WS, CHAT)]  # noqa: SLF001
        assert stats.pending_messages == 0
        assert stats.last_consolidation_at is not None


class TestFailure:
    async def test_failure_backs_off_without_touching_pending(
        self,
        scheduler: MemoryMaintenanceScheduler,
    ) -> None:
        task = install_task(scheduler, pending=4)
        retry_at = datetime.now() + timedelta(seconds=90)

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            return ConsolidationResult(failed_reason="server_error", retry_after=retry_at.isoformat())

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001

        stats = scheduler._channel_stats[(WS, CHAT)]  # noqa: SLF001
        assert stats.pending_messages == 4
        assert stats.last_consolidation_at is None
        assert stats.consecutive_skips == 1
        assert stats.next_eligible_at is not None
        assert stats.next_eligible_at >= datetime.now() + timedelta(seconds=80)

        # 重试交给定时器，不进堆：堆里一个计划时间很远的任务会让主循环原地 sleep、卡住别的频道。
        assert heap_tasks(scheduler) == []
        due = scheduler.pending_retry_due(WS, CHAT)
        assert due is not None
        assert due >= time.time() + 80

    async def test_exception_is_treated_like_failure(self, scheduler: MemoryMaintenanceScheduler) -> None:
        task = install_task(scheduler, pending=4)

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            raise RuntimeError("boom")

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001

        stats = scheduler._channel_stats[(WS, CHAT)]  # noqa: SLF001
        assert stats.pending_messages == 4
        assert stats.last_consolidation_at is None
        assert stats.consecutive_skips == 1
        assert stats.next_eligible_at is not None
        assert heap_tasks(scheduler) == []
        assert scheduler.pending_retry_due(WS, CHAT) is not None

    async def test_retry_timer_requeues_when_due(
        self,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MIN_INTERVAL_SECONDS", 0)
        monkeypatch.setattr(config, "MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS", 0)
        task = install_task(scheduler, pending=4)

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            return ConsolidationResult(failed_reason="server_error")

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001
        assert heap_tasks(scheduler) == []

        for _ in range(50):
            if heap_tasks(scheduler):
                break
            await asyncio.sleep(0.02)
        tasks = heap_tasks(scheduler)
        assert len(tasks) == 1
        assert tasks[0].chat_key == CHAT
        assert tasks[0].priority == ConsolidationPriority.NORMAL
        assert scheduler.pending_retry_due(WS, CHAT) is None

    async def test_long_backoff_does_not_block_other_channels(
        self,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """一个频道退避一小时，另一个频道的沉淀照常执行（回归：退避任务曾卡住整个主循环）"""
        monkeypatch.setattr(config, "MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS", 0)
        other_chat = "onebot_v11-sched-group_2"
        failing_done = asyncio.Event()
        other_ran = asyncio.Event()
        retry_at = datetime.now() + timedelta(hours=1)

        async def handler(_workspace_id: int, chat_key: str) -> ConsolidationResult:
            if chat_key == CHAT:
                failing_done.set()
                return ConsolidationResult(failed_reason="server_error", retry_after=retry_at.isoformat())
            other_ran.set()
            return ConsolidationResult(messages_processed=1)

        scheduler.set_consolidation_handler(handler)
        scheduler._running = True  # noqa: SLF001
        loop_task = asyncio.create_task(scheduler._run_loop())  # noqa: SLF001
        try:
            async with scheduler._lock:  # noqa: SLF001
                await scheduler._schedule_consolidation(WS, CHAT, ConsolidationPriority.URGENT)  # noqa: SLF001
            await asyncio.wait_for(failing_done.wait(), timeout=5)
            # 等失败那一轮收尾（定时器已挂上）再排另一个频道
            for _ in range(50):
                if scheduler.pending_retry_due(WS, CHAT) is not None:
                    break
                await asyncio.sleep(0.02)
            assert scheduler.pending_retry_due(WS, CHAT) is not None
            async with scheduler._lock:  # noqa: SLF001
                await scheduler._schedule_consolidation(WS + 1, other_chat, ConsolidationPriority.LOW)  # noqa: SLF001
            await asyncio.wait_for(other_ran.wait(), timeout=5)
        finally:
            scheduler._running = False  # noqa: SLF001
            scheduler._wakeup.set()  # noqa: SLF001
            loop_task.cancel()
            for exec_task in list(scheduler._execution_tasks):  # noqa: SLF001
                exec_task.cancel()

    async def test_backoff_window_blocks_new_messages_from_rescheduling(
        self,
        scheduler: MemoryMaintenanceScheduler,
    ) -> None:
        task = install_task(scheduler, pending=4)

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            return ConsolidationResult(failed_reason="server_error", retry_after="not-a-time")

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001

        stats = scheduler._channel_stats[(WS, CHAT)]  # noqa: SLF001
        assert stats.next_eligible_at is not None
        assert scheduler._compute_priority(stats) is None  # noqa: SLF001

        stats.next_eligible_at = datetime.now() - timedelta(seconds=1)
        assert scheduler._compute_priority(stats) is not None  # noqa: SLF001
        assert stats.next_eligible_at is None

    async def test_aware_retry_after_is_not_shortened_by_timezone_stripping(
        self,
        scheduler: MemoryMaintenanceScheduler,
    ) -> None:
        task = install_task(scheduler, pending=4)
        retry_at = datetime.now().astimezone() + timedelta(seconds=120)

        async def handler(_workspace_id: int, _chat_key: str) -> ConsolidationResult:
            return ConsolidationResult(failed_reason="server_error", retry_after=retry_at.isoformat())

        scheduler.set_consolidation_handler(handler)
        await scheduler._execute_task(task)  # noqa: SLF001

        stats = scheduler._channel_stats[(WS, CHAT)]  # noqa: SLF001
        assert stats.next_eligible_at is not None
        assert stats.next_eligible_at >= datetime.now() + timedelta(seconds=100)


class TestSerialization:
    async def test_gate_open_runs_different_instances_concurrently(
        self,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        started: List[str] = []
        release = asyncio.Event()

        async def handler(_workspace_id: int, chat_key: str) -> ConsolidationResult:
            started.append(chat_key)
            await release.wait()
            return ConsolidationResult(messages_processed=0)

        scheduler.set_consolidation_handler(handler)
        tasks: List[ConsolidationTask] = []
        for index, chat_key in enumerate(("onebot_v11-a-group_1", "onebot_v11-b-group_2"), start=1):
            scheduler._versions[(0, chat_key)] = 0  # noqa: SLF001
            scheduler._chat_instance_map[chat_key] = ("onebot_v11", f"inst-{index}")  # noqa: SLF001
            tasks.append(
                ConsolidationTask(
                    priority=ConsolidationPriority.NORMAL,
                    scheduled_at=time.time(),
                    workspace_id=0,
                    chat_key=chat_key,
                    version=0,
                ),
            )

        running = [asyncio.create_task(scheduler._execute_task(task)) for task in tasks]  # noqa: SLF001
        for _ in range(100):
            if len(started) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(started) == 2
        release.set()
        await asyncio.gather(*running)

    async def test_gate_open_serializes_same_instance(
        self,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        started: List[str] = []
        release = asyncio.Event()
        first_chat = "onebot_v11-c-group_1"
        second_chat = "onebot_v11-d-group_2"

        async def handler(_workspace_id: int, chat_key: str) -> ConsolidationResult:
            started.append(chat_key)
            await release.wait()
            return ConsolidationResult(messages_processed=0)

        scheduler.set_consolidation_handler(handler)
        scheduler._chat_instance_map[first_chat] = ("onebot_v11", "same")  # noqa: SLF001
        scheduler._chat_instance_map[second_chat] = ("onebot_v11", "same")  # noqa: SLF001
        scheduler._versions[(0, first_chat)] = 0  # noqa: SLF001
        scheduler._versions[(0, second_chat)] = 0  # noqa: SLF001

        first = asyncio.create_task(  # noqa: SLF001
            scheduler._execute_task(  # noqa: SLF001
                ConsolidationTask(
                    priority=ConsolidationPriority.NORMAL,
                    scheduled_at=time.time(),
                    workspace_id=0,
                    chat_key=first_chat,
                    version=0,
                ),
            ),
        )
        for _ in range(100):
            if started:
                break
            await asyncio.sleep(0.01)
        assert started == [first_chat]

        await scheduler._execute_task(  # noqa: SLF001
            ConsolidationTask(
                priority=ConsolidationPriority.NORMAL,
                scheduled_at=time.time(),
                workspace_id=0,
                chat_key=second_chat,
                version=0,
            ),
        )

        assert started == [first_chat]
        assert len(heap_tasks(scheduler)) == 1
        release.set()
        await first

    async def test_gate_off_still_serializes_same_workspace(
        self,
        scheduler: MemoryMaintenanceScheduler,
    ) -> None:
        started: List[str] = []
        release = asyncio.Event()
        first_chat = "onebot_v11-e-group_1"
        second_chat = "onebot_v11-f-group_2"

        async def handler(_workspace_id: int, chat_key: str) -> ConsolidationResult:
            started.append(chat_key)
            await release.wait()
            return ConsolidationResult(messages_processed=0)

        scheduler.set_consolidation_handler(handler)
        scheduler._versions[(WS, first_chat)] = 0  # noqa: SLF001
        scheduler._versions[(WS, second_chat)] = 0  # noqa: SLF001

        first = asyncio.create_task(  # noqa: SLF001
            scheduler._execute_task(  # noqa: SLF001
                ConsolidationTask(
                    priority=ConsolidationPriority.NORMAL,
                    scheduled_at=time.time(),
                    workspace_id=WS,
                    chat_key=first_chat,
                    version=0,
                ),
            ),
        )
        for _ in range(100):
            if started:
                break
            await asyncio.sleep(0.01)
        assert started == [first_chat]

        await scheduler._execute_task(  # noqa: SLF001
            ConsolidationTask(
                priority=ConsolidationPriority.NORMAL,
                scheduled_at=time.time(),
                workspace_id=WS,
                chat_key=second_chat,
                version=0,
            ),
        )

        assert started == [first_chat]
        assert len(heap_tasks(scheduler)) == 1
        release.set()
        await first


class TestGateFlip:
    """运行中把可回滚闸开 / 关：排队的任务不能写进错误分区，新旧两种调度键按频道互斥。"""

    async def test_gate_closed_drops_queued_domain_task(self, scheduler: MemoryMaintenanceScheduler) -> None:
        # 闸开时排进来的 (0, 频道) 任务，执行时闸已经关了：丢掉，不调处理器、不重排。
        called: List[int] = []

        async def handler(workspace_id: int, _chat_key: str) -> ConsolidationResult:
            called.append(workspace_id)
            return ConsolidationResult(messages_processed=1)

        scheduler.set_consolidation_handler(handler)
        scheduler._versions[(0, CHAT)] = 0  # noqa: SLF001

        await scheduler._execute_task(  # noqa: SLF001
            ConsolidationTask(
                priority=ConsolidationPriority.NORMAL,
                scheduled_at=time.time(),
                workspace_id=0,
                chat_key=CHAT,
                version=0,
            ),
        )

        assert called == []
        assert heap_tasks(scheduler) == []
        assert scheduler._retry_timers == {}  # noqa: SLF001

    async def test_same_channel_never_runs_under_both_keys(
        self,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 闸关时 (工作区, 频道) 正在跑，闸被打开，同一频道的 (0, 频道) 任务到点：
        # 两者的调度键、串行键都不同，只有按频道互斥能挡住它们并发读同一批消息。
        started: List[int] = []
        release = asyncio.Event()

        async def handler(workspace_id: int, _chat_key: str) -> ConsolidationResult:
            started.append(workspace_id)
            await release.wait()
            return ConsolidationResult(messages_processed=0)

        scheduler.set_consolidation_handler(handler)
        scheduler._versions[(WS, CHAT)] = 0  # noqa: SLF001
        first = asyncio.create_task(  # noqa: SLF001
            scheduler._execute_task(  # noqa: SLF001
                ConsolidationTask(
                    priority=ConsolidationPriority.NORMAL,
                    scheduled_at=time.time(),
                    workspace_id=WS,
                    chat_key=CHAT,
                    version=0,
                ),
            ),
        )
        for _ in range(100):
            if started:
                break
            await asyncio.sleep(0.01)
        assert started == [WS]

        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        scheduler._chat_instance_map[CHAT] = ("onebot_v11", "flip")  # noqa: SLF001
        scheduler._versions[(0, CHAT)] = 0  # noqa: SLF001
        # 限时：互斥失效时第二轮会进处理器、卡在同一个事件上，用例应该失败而不是挂住。
        await asyncio.wait_for(
            scheduler._execute_task(  # noqa: SLF001
                ConsolidationTask(
                    priority=ConsolidationPriority.NORMAL,
                    scheduled_at=time.time(),
                    workspace_id=0,
                    chat_key=CHAT,
                    version=0,
                ),
            ),
            timeout=5,
        )

        assert started == [WS]
        queued = heap_tasks(scheduler)
        assert [(task.workspace_id, task.chat_key) for task in queued] == [(0, CHAT)]
        release.set()
        await first
        assert scheduler._running_chat_keys == set()  # noqa: SLF001

    async def test_consolidator_refuses_domain_write_when_gate_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 调度器检查闸之后、处理器开始之前闸被关掉：沉淀器自己再挡一次，频道和游标都不碰。
        from nekro_agent.services.memory.consolidator import EpisodicConsolidator

        async def refuse_load(_self: EpisodicConsolidator, _chat_key: str) -> None:
            raise AssertionError("闸关时不应该读频道、动游标")

        monkeypatch.setattr(EpisodicConsolidator, "_load_channel", refuse_load)

        result = await EpisodicConsolidator(0).consolidate(CHAT)

        assert result.skipped_reason == "domain_scope_disabled"
        assert result.messages_processed == 0
        assert result.failed_reason is None
