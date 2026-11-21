"""记忆沉淀调度器的「按频道」作用域回归。

## 这条缺陷是什么

沉淀本身是**按频道**做的：`consolidate_workspace(workspace_id, chat_key)` 读的是该频道的消息、
写的是该频道 `data.memory_state.last_consolidated_message_db_id`。但改造前调度器把
统计、版本号、去重**全部只按 `workspace_id`** 记：

- `_versions[workspace_id]`：每调度一次就自增，而 `_get_next_task` 只放行版本号相等的任务。
  于是 B 频道的一次调度会把 A 频道**已经排好**的任务整批作废。
- `_workspace_stats[workspace_id].pending_messages`：A、B 两个频道的消息合并计数，
  沉淀完其中一个就清零，另一个的待沉淀量凭空消失。

`WorkspaceService.bind_channel` 是一对多，一个工作区可以绑多个频道（多实例部署下就是多个实例
各自的频道）。结果是：**除最后一个被调度的频道外，其余频道的记忆根本不会被沉淀**——
不是"晚一点沉淀"，是任务被丢掉、没有任何人重排。

最直接的现场是启动恢复：`_recover_pending_consolidations` 逐个频道调度、日志打印
"已恢复 N 个待处理的沉淀任务"，而实际能跑的只有最后一个，另外 N-1 个在队列里被版本号判死。
日志说恢复了 N 个，数据只落了 1 个。

## 单频道部署逐字不变

一个工作区只绑一个频道时 `(workspace_id, chat_key)` 与 `workspace_id` 一一对应，
去重、跳过、清零的行为与改造前完全一致——`TestSingleChannelUnchanged` 钉住这一点。
"""

from __future__ import annotations

import asyncio
import json
from typing import List, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.services.memory import scheduler as scheduler_module
from nekro_agent.services.memory.consolidator import ConsolidationResult
from nekro_agent.services.memory.scheduler import (
    ConsolidationPriority,
    MemoryMaintenanceScheduler,
)

WORKSPACE_ID = 9301
OTHER_WORKSPACE_ID = 9302

CHAT_A = "onebot_v11-ob9301a-group_1"
CHAT_B = "onebot_v11-ob9301b-group_2"
CHAT_C = "onebot_v11-ob9301c-group_3"


@pytest.fixture(autouse=True)
def memory_system_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """记忆总开关默认关闭，关着的话 `on_new_message` 第一行就 return，测的是空转。"""
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)


@pytest.fixture
def scheduler() -> MemoryMaintenanceScheduler:
    """每条用例一个全新调度器实例，不碰模块级单例。"""
    return MemoryMaintenanceScheduler()


async def drain(scheduler: MemoryMaintenanceScheduler) -> List[str]:
    """把队列里**还有效**的任务全部取出来，返回它们的 chat_key（按出队顺序）。"""
    survived: List[str] = []
    while True:
        task = await scheduler._get_next_task()  # noqa: SLF001
        if task is None:
            return survived
        survived.append(task.chat_key)


async def schedule(
    scheduler: MemoryMaintenanceScheduler,
    workspace_id: int,
    chat_key: str,
    priority: ConsolidationPriority = ConsolidationPriority.NORMAL,
) -> None:
    async with scheduler._lock:  # noqa: SLF001
        await scheduler._schedule_consolidation(workspace_id, chat_key, priority)  # noqa: SLF001


class TestQueueKeepsEveryChannel:
    """队列层：同一工作区的多个频道各自成任务，互不作废。"""

    @pytest.mark.asyncio
    async def test_three_channels_all_survive(self, scheduler: MemoryMaintenanceScheduler) -> None:
        await schedule(scheduler, WORKSPACE_ID, CHAT_A)
        await schedule(scheduler, WORKSPACE_ID, CHAT_B)
        await schedule(scheduler, WORKSPACE_ID, CHAT_C)

        assert len(scheduler.queued_tasks()) == 3
        # 改前这里只剩 [CHAT_C]：后两次调度把版本号推到 3，前两个任务被判为过期
        assert sorted(await drain(scheduler)) == sorted([CHAT_A, CHAT_B, CHAT_C])

    @pytest.mark.asyncio
    async def test_other_workspace_does_not_invalidate(self, scheduler: MemoryMaintenanceScheduler) -> None:
        await schedule(scheduler, WORKSPACE_ID, CHAT_A)
        await schedule(scheduler, OTHER_WORKSPACE_ID, CHAT_B)
        assert sorted(await drain(scheduler)) == sorted([CHAT_A, CHAT_B])


class TestSingleChannelUnchanged:
    """单频道工作区：去重语义与改造前逐字一致。"""

    @pytest.mark.asyncio
    async def test_same_channel_repeated_schedule_collapses_to_one(
        self,
        scheduler: MemoryMaintenanceScheduler,
    ) -> None:
        await schedule(scheduler, WORKSPACE_ID, CHAT_A, ConsolidationPriority.LOW)
        await schedule(scheduler, WORKSPACE_ID, CHAT_A, ConsolidationPriority.HIGH)
        await schedule(scheduler, WORKSPACE_ID, CHAT_A, ConsolidationPriority.URGENT)

        # 同一个频道在队列里只留一项：后两次调度只更新它（优先级取最急的），不再重复入队
        assert len(scheduler.queued_tasks()) == 1
        assert scheduler.queued_tasks()[0].priority == ConsolidationPriority.URGENT
        assert await drain(scheduler) == [CHAT_A]

    @pytest.mark.asyncio
    async def test_running_channel_is_skipped(self, scheduler: MemoryMaintenanceScheduler) -> None:
        scheduler._running_scopes.add((WORKSPACE_ID, CHAT_A))  # noqa: SLF001
        await schedule(scheduler, WORKSPACE_ID, CHAT_A)
        assert scheduler.queued_tasks() == []

    @pytest.mark.asyncio
    async def test_running_channel_does_not_block_sibling(self, scheduler: MemoryMaintenanceScheduler) -> None:
        """A 在跑不该挡住 B 排队——这正是改造前"另一个频道永远排不上"的成因之一。"""
        scheduler._running_scopes.add((WORKSPACE_ID, CHAT_A))  # noqa: SLF001
        await schedule(scheduler, WORKSPACE_ID, CHAT_B)
        assert await drain(scheduler) == [CHAT_B]

    @pytest.mark.asyncio
    async def test_force_consolidation_refuses_only_the_running_channel(
        self,
        scheduler: MemoryMaintenanceScheduler,
    ) -> None:
        scheduler._running_scopes.add((WORKSPACE_ID, CHAT_A))  # noqa: SLF001
        assert await scheduler.force_consolidation(WORKSPACE_ID, CHAT_A) is False
        assert await scheduler.force_consolidation(WORKSPACE_ID, CHAT_B) is True


class TestPendingCounterIsPerChannel:
    """统计层：待沉淀计数与清零都必须按频道。"""

    @pytest.mark.asyncio
    async def test_counters_do_not_merge_across_channels(
        self,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MSG_THRESHOLD", 4)
        for _ in range(2):
            await scheduler.on_new_message(chat_key=CHAT_A, workspace_id=WORKSPACE_ID)
        for _ in range(2):
            await scheduler.on_new_message(chat_key=CHAT_B, workspace_id=WORKSPACE_ID)

        # 合并计数的话这里会是一个 4：A 的三条消息把 B 也一起推过阈值，
        # 而沉淀只做了其中一个频道
        assert scheduler._channel_stats[(WORKSPACE_ID, CHAT_A)].pending_messages == 2  # noqa: SLF001
        assert scheduler._channel_stats[(WORKSPACE_ID, CHAT_B)].pending_messages == 2  # noqa: SLF001
        assert [item.chat_key for item in scheduler.queued_tasks()].count(CHAT_A) == 1
        assert [item.chat_key for item in scheduler.queued_tasks()].count(CHAT_B) == 1

    @pytest.mark.asyncio
    async def test_consolidating_one_channel_does_not_zero_the_other(
        self,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0)
        monkeypatch.setattr(
            scheduler_module,
            "config",
            config,
        )
        handled: List[Tuple[int, str]] = []

        async def handler(workspace_id: int, chat_key: str) -> ConsolidationResult:
            handled.append((workspace_id, chat_key))
            pending = scheduler._channel_stats[(workspace_id, chat_key)].pending_messages  # noqa: SLF001
            return ConsolidationResult(messages_processed=pending)

        scheduler.set_consolidation_handler(handler)  # type: ignore[arg-type]
        for _ in range(3):
            await scheduler.on_new_message(chat_key=CHAT_A, workspace_id=WORKSPACE_ID)
        for _ in range(5):
            await scheduler.on_new_message(chat_key=CHAT_B, workspace_id=WORKSPACE_ID)

        await schedule(scheduler, WORKSPACE_ID, CHAT_A)
        task = await scheduler._get_next_task()  # noqa: SLF001
        assert task is not None
        await scheduler._execute_task(task)  # noqa: SLF001

        assert handled == [(WORKSPACE_ID, task.chat_key)]
        assert scheduler._channel_stats[(WORKSPACE_ID, task.chat_key)].pending_messages == 0  # noqa: SLF001
        other = CHAT_B if task.chat_key == CHAT_A else CHAT_A
        assert scheduler._channel_stats[(WORKSPACE_ID, other)].pending_messages > 0  # noqa: SLF001


class TestWorkspaceStaysSerialized:
    """并发层：多频道排队了，但同一工作区内仍然一次只跑一个。

    `DBMemEntity.find_or_create` 是「先查后建」，同实例的两个频道并发沉淀会把同一个实体
    建成两行。改造前靠"一个工作区只能有一个有效任务"顺带做到了串行，现在必须显式挡住。
    """

    @pytest.mark.asyncio
    async def test_second_channel_defers_instead_of_running_concurrently(
        self,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0)
        handled: List[str] = []

        async def handler(workspace_id: int, chat_key: str) -> ConsolidationResult:
            handled.append(chat_key)
            return ConsolidationResult()

        scheduler.set_consolidation_handler(handler)  # type: ignore[arg-type]
        scheduler._running_workspaces.add(WORKSPACE_ID)  # noqa: SLF001

        await schedule(scheduler, WORKSPACE_ID, CHAT_B)
        task = await scheduler._get_next_task()  # noqa: SLF001
        assert task is not None
        await scheduler._execute_task(task)  # noqa: SLF001

        # 没有真的跑，而是原样重排回队列等对方让出工作区
        assert handled == []
        assert [item.chat_key for item in scheduler.queued_tasks()] == [CHAT_B]

    @pytest.mark.asyncio
    async def test_workspace_released_after_execution(
        self,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS", 0)
        # 「执行期间登记了什么」只能带出来在外面判：`_execute_task` 把处理器里的异常
        # 收进 `except Exception` 记日志，直接在处理器里 assert 会被整条吞掉
        # ——第一版就是这么写的，"不登记 _running_workspaces" 的变异实测存活。
        seen: List[Tuple[set, set]] = []

        async def handler(workspace_id: int, chat_key: str) -> ConsolidationResult:
            seen.append((set(scheduler._running_workspaces), set(scheduler._running_scopes)))  # noqa: SLF001
            return ConsolidationResult()

        scheduler.set_consolidation_handler(handler)  # type: ignore[arg-type]
        await schedule(scheduler, WORKSPACE_ID, CHAT_A)
        task = await scheduler._get_next_task()  # noqa: SLF001
        assert task is not None
        await scheduler._execute_task(task)  # noqa: SLF001

        assert seen == [({WORKSPACE_ID}, {(WORKSPACE_ID, CHAT_A)})]
        assert scheduler._running_workspaces == set()  # noqa: SLF001
        assert scheduler._running_scopes == set()  # noqa: SLF001


class TestStatsSnapshotIsPerChannel:
    """`get_stats()` 是这份状态**唯一**面向人的出口，键也必须按频道。

    它当前没有生产调用方，正因如此更要钉住：按 `workspace_id` 记的话，同一工作区的第二个
    频道会把第一个整条覆盖掉——运维照着它排查"某个频道为什么不沉淀"时，看到的会是
    另一个频道的数字。这条是对抗式复核里唯一存活下来的变异（把键换回 `f"{ws_id}"`，
    12 条用例一条不红）。
    """

    @pytest.mark.asyncio
    async def test_two_channels_of_one_workspace_both_appear(
        self,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MSG_THRESHOLD", 100)
        for _ in range(2):
            await scheduler.on_new_message(chat_key=CHAT_A, workspace_id=WORKSPACE_ID)
        for _ in range(5):
            await scheduler.on_new_message(chat_key=CHAT_B, workspace_id=WORKSPACE_ID)

        stats = scheduler.get_stats()

        # 两个频道各占一行，而不是后者盖掉前者
        assert len(stats) == 2
        assert set(stats) == {f"{WORKSPACE_ID}:{CHAT_A}", f"{WORKSPACE_ID}:{CHAT_B}"}
        assert stats[f"{WORKSPACE_ID}:{CHAT_A}"]["pending_messages"] == 2
        assert stats[f"{WORKSPACE_ID}:{CHAT_B}"]["pending_messages"] == 5
        # 键之外，行内也要能定位到是哪个频道
        assert stats[f"{WORKSPACE_ID}:{CHAT_B}"]["chat_key"] == CHAT_B
        assert stats[f"{WORKSPACE_ID}:{CHAT_B}"]["workspace_id"] == WORKSPACE_ID


@pytest.fixture
async def recovery_db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def seed_channel(chat_key: str, instance_key: str, message_count: int) -> None:
    await DBChatChannel.create(
        chat_key=chat_key,
        adapter_key="onebot_v11",
        instance_key=instance_key,
        channel_id=chat_key.rsplit("-", 1)[-1],
        channel_name=chat_key,
        channel_type="group",
        is_active=True,
        workspace_id=WORKSPACE_ID,
        data=json.dumps({"memory_state": {"last_consolidated_message_db_id": 0}}),
    )
    for idx in range(message_count):
        await DBChatMessage.create(
            message_id=f"{chat_key}-{idx}",
            sender_id=f"{instance_key}-sender",
            sender_name="tester",
            sender_nickname="tester",
            platform_userid=f"{instance_key}-user",
            is_tome=0,
            is_recalled=False,
            adapter_key="onebot_v11",
            instance_key=instance_key,
            chat_key=chat_key,
            chat_type="group",
            content_text="hello",
            content_data="[]",
            raw_cq_code="",
            ext_data="{}",
            send_timestamp=1700000000 + idx,
        )


class TestStartupRecoveryReachesEveryChannel:
    """接线层：真表 + 真恢复入口，不打替身。

    这条是「零件对了但接线没测」的守卫（§2.7）：上面几条都在直接调 `_schedule_consolidation`，
    而真正会在生产上出事的是 `_recover_pending_consolidations` 那个循环。
    """

    @pytest.mark.asyncio
    async def test_every_pending_channel_ends_up_runnable(
        self,
        recovery_db,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MSG_THRESHOLD", 2)
        monkeypatch.setattr(config, "MEMORY_STARTUP_RECOVERY_MAX_TASKS", 0)

        original_sleep = asyncio.sleep

        async def no_wait(delay: float, *args, **kwargs):
            return await original_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", no_wait)

        await DBWorkspace.create(id=WORKSPACE_ID, name="ws-9301", status="active")
        await seed_channel(CHAT_A, "ob9301a", 3)
        await seed_channel(CHAT_B, "ob9301b", 4)
        await seed_channel(CHAT_C, "ob9301c", 5)

        await scheduler._recover_pending_consolidations()  # noqa: SLF001

        # 改前恢复循环调度了 3 个、日志也说"已恢复 3 个"，而队列里只有最后一个还有效
        assert sorted(await drain(scheduler)) == sorted([CHAT_A, CHAT_B, CHAT_C])

    @pytest.mark.asyncio
    async def test_pending_counts_are_recorded_per_channel(
        self,
        recovery_db,
        scheduler: MemoryMaintenanceScheduler,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MSG_THRESHOLD", 2)
        monkeypatch.setattr(config, "MEMORY_STARTUP_RECOVERY_MAX_TASKS", 0)

        original_sleep = asyncio.sleep

        async def no_wait(delay: float, *args, **kwargs):
            return await original_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", no_wait)

        await DBWorkspace.create(id=WORKSPACE_ID, name="ws-9301", status="active")
        await seed_channel(CHAT_A, "ob9301a", 3)
        await seed_channel(CHAT_B, "ob9301b", 7)

        await scheduler._recover_pending_consolidations()  # noqa: SLF001

        assert scheduler._channel_stats[(WORKSPACE_ID, CHAT_A)].pending_messages == 3  # noqa: SLF001
        assert scheduler._channel_stats[(WORKSPACE_ID, CHAT_B)].pending_messages == 7  # noqa: SLF001
