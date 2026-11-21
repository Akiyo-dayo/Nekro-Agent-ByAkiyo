"""记忆维护调度器

事件驱动的自适应调度：监听消息事件，动态决定何时触发记忆沉淀。

## 队列的几条硬约束

全局打开记忆、十几个群同时活跃时，旧实现整机停摆过四十多分钟。下面每一条都对应当时的一个成因：

- **每个频道在队列里最多一项。** 新消息只抬高这一项的优先级、把最早可执行时间往前挪、更新版本号，
  不再重复入队。旧实现每条消息都往堆里推一个新版本，作废的旧版本要等弹出时才清，队列随消息量线性增长。
- **延迟不按队列长度算。** 旧实现的计划时间是「队列长度 × 错峰间隔」之后，队列一长就排到几十分钟以后。
  `MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS` 现在是两次派发之间的最短间隔。
- **主循环不抱着队头睡。** 每次醒来只从「已到期、且没被同实例 / 同工作区 / 同频道占着」的项里挑一个派出去；
  没得挑就等到最早到期或被唤醒（新入队、某一轮跑完都会唤醒）。旧实现弹出一个任务后原地睡到它的计划时间，
  这期间到期的其他任务全都动不了。
- **老化防饿死。** 每在队列里多等 `AGING_STEP_SECONDS`，有效优先级升一级；同级按入队先后。任何一项最多等
  三个步长就与 URGENT 同级，再按先来后到排，等待时间有上界。只按优先级排时，积压多的群每来一条消息就是一个
  URGENT，HIGH / LOW 永远轮不到。选老化而不是按优先级轮转：上界说得清，也不用额外维护轮转状态。
- **看得见。** 队列非空或有沉淀在跑时，每 `QUEUE_REPORT_INTERVAL_SECONDS` 最多打一行 INFO：排队数、已到期数、
  被占着的数、执行中数、最老一项等了多久。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Awaitable, Callable

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.memory.feature_flags import (
    is_domain_scope_enabled,
    is_memory_enabled_for_channel,
    is_memory_system_enabled,
)
from nekro_agent.services.memory.maintenance import prune_all_workspaces
from nekro_agent.services.memory.relation_retention import (
    extend_stable_relation_half_life,
    restore_pruned_stable_relations,
)
from nekro_agent.services.memory.retention import extend_stable_half_life, restore_pruned_stable
from nekro_agent.services.memory.text_terms import warm_up_tokenizer_async

if TYPE_CHECKING:  # pragma: no cover - 避免与沉淀器成环
    from nekro_agent.services.memory.consolidator import ConsolidationResult

logger = get_sub_logger("memory.scheduler")

# 启动恢复按主键分页扫描频道。一次取全表在几百个频道时也要把整表读进内存，
# 而且扫描期间事件循环被 ORM 实例化占住；200 是这里与清理链路共用的页大小。
RECOVERY_CHANNEL_PAGE_SIZE = 200
# 频道正在做域行重建时，实时沉淀往后挪这么久再排。与工作区重建那条分支的写法一致：
# 退避不能直接放进队列（见 `_arm_retry_timer` 的 docstring）。
DOMAIN_REBUILD_RETRY_DELAY_SECONDS = 30.0
# 老化步长：在队列里每多等这么久，有效优先级升一级。LOW 最多等三个步长（6 分钟）就与 URGENT 同级。
# 比最小沉淀间隔（默认 300 秒）短，是为了让积压的低优先级频道在下一轮高峰之前就能轮到。
AGING_STEP_SECONDS = 120.0
# 队列观测日志的最短间隔：只在队列非空或有沉淀在跑时打，每分钟最多一行。
QUEUE_REPORT_INTERVAL_SECONDS = 60.0

# 自动清理两轮之间至少隔这么久（秒），配置的小时数再小也不低于它。
PRUNE_MIN_INTERVAL_SECONDS = 3600.0
# 主循环在「有事但没有可派发的项」时最长等多久再自检一次：兜住按时打观测日志，也兜住漏掉的唤醒。
# 队列空、也没有沉淀在跑时不设上限，只等新入队唤醒。
LOOP_IDLE_CHECK_SECONDS = 30.0
# 启动后等多久再扫一遍积压，让数据库和其他服务先就绪
RECOVERY_START_DELAY_SECONDS = 3.0
# 一轮沉淀结束后查库里积压的超时。这一步只是校正，查不动就回落到内存计数，
# 不能让它挂住 `_execute_task`（那时手里还占着并发名额和同实例的串行标记）。
DB_BACKLOG_TIMEOUT_SECONDS = 5.0


def _memory_cursor(raw_data: str | None) -> int | None:
    """频道 `data.memory_state` 里的沉淀游标；没有游标（还没真正开始沉淀）或读不了返回 None"""
    import json

    try:
        data = json.loads(raw_data or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    memory_state = data.get("memory_state")
    if not isinstance(memory_state, dict) or "last_consolidated_message_db_id" not in memory_state:
        return None
    raw = memory_state.get("last_consolidated_message_db_id")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


class ConsolidationPriority(int, Enum):
    """沉淀优先级"""

    LOW = 3  # 低优先级：消息量少，活跃度低
    NORMAL = 2  # 普通优先级
    HIGH = 1  # 高优先级：消息量大或长时间未沉淀
    URGENT = 0  # 紧急：达到强制阈值


# 调度粒度。沉淀本身是**按频道**做的（`consolidate_workspace(workspace_id, chat_key)` 读的是
# 该频道的消息、写的是该频道的 `memory_state.last_consolidated_message_db_id`），所以任务身份
# 也必须是 (工作区, 频道)。改造前统计、版本号、去重全部只按 `workspace_id`，而
# `WorkspaceService.bind_channel` 是一对多 —— 一个工作区绑多个频道（多实例下就是多个实例）时，
# 后调度的那个频道会把先前排好的其他频道任务整批作废（见 `_get_next_task` 的版本校验），
# 于是除了最后一个频道，其余频道的记忆**根本不会被沉淀**。
ConsolidationScope = tuple[int, str]


@dataclass
class ChannelActivityStats:
    """频道活动统计

    统计口径必须与调度粒度一致：按工作区累计的话，A 频道的消息会把 B 频道推过阈值，
    而沉淀只做了其中一个频道，随后 `pending_messages` 被清零 —— 另一个频道的待沉淀量
    就这样凭空消失了。
    """

    workspace_id: int
    chat_key: str
    pending_messages: int = 0  # 待处理消息数
    last_message_at: datetime | None = None  # 最后消息时间
    last_consolidation_at: datetime | None = None  # 最后沉淀时间
    consecutive_skips: int = 0  # 连续跳过次数
    next_eligible_at: datetime | None = None  # 失败退避的下一可执行时间


@dataclass(order=True)
class ConsolidationTask:
    """沉淀任务，也是队列里的那一项（每个调度键最多一项）"""

    priority: ConsolidationPriority
    scheduled_at: float = field(compare=True)  # 最早可执行时间戳
    workspace_id: int = field(compare=False)
    chat_key: str = field(compare=False)
    version: int = field(compare=False, default=0)  # 每次调度自增，便于排查「这一项被更新过几次」
    # 首次入队的时间。后续更新**不**刷新它：老化按「等了多久」算，刷新就等于永远是新来的。
    enqueued_at: float = field(compare=False, default_factory=time.time)


class MemoryMaintenanceScheduler:
    """记忆维护调度器

    设计目标：
    - 事件驱动：消息到达时更新统计，动态调整优先级
    - 低 CPU：等到最早到期或被唤醒，不做轮询（有事可等时最多每 `LOOP_IDLE_CHECK_SECONDS` 自检一次）
    - 自适应：根据工作区活跃度调整沉淀频率
    - 并发控制：限制同时执行的沉淀任务数
    """

    def __init__(self) -> None:
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._prune_task: asyncio.Task | None = None
        self._recovery_task: asyncio.Task | None = None
        self._retention_task: asyncio.Task | None = None
        self._tokenizer_warmup_task: asyncio.Task | None = None
        self._execution_tasks: set[asyncio.Task] = set()
        self._wakeup = asyncio.Event()
        self._lock = asyncio.Lock()

        # 频道统计（键 = (workspace_id, chat_key)）
        self._channel_stats: dict[ConsolidationScope, ChannelActivityStats] = {}
        # chat_key -> workspace_id 映射缓存
        self._chat_workspace_map: dict[str, int] = {}
        # chat_key -> (adapter_key, instance_key)：闸开后串行粒度是实例，不再是工作区
        self._chat_instance_map: dict[str, tuple[str, str]] = {}

        # 队列：每个调度键最多一项（见模块文档）。频道数在几百量级，挑下一项时整表扫一遍即可。
        self._queue: dict[ConsolidationScope, ConsolidationTask] = {}
        self._versions: dict[ConsolidationScope, int] = {}  # (工作区, 频道) -> 当前版本
        # 上一次派发的时间：两次派发之间至少隔 `MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS`
        self._last_dispatch_at = 0.0
        self._last_queue_report_at = 0.0

        # 并发控制
        self._running_scopes: set[ConsolidationScope] = set()  # 正在执行的 (工作区, 频道)
        # 同一工作区内仍然串行：`DBMemEntity.find_or_create` 是「先查后建」，同实例的两个频道
        # 并发沉淀时会把同一个实体建成两行。跨工作区的并发上限由信号量控制，这里只挡工作区内部。
        self._running_workspaces: set[int] = set()
        # 闸开后的串行边界：同一 (适配器, 实例) 的沉淀串行，跨工作区也要串
        self._running_instances: set[tuple[str, str]] = set()
        # 按频道互斥，与分区无关：运行中切换回滚闸后，同一频道可能先后以 (工作区, 频道)
        # 与 (0, 频道) 两种调度键执行，上面几个集合都按各自的键算，挡不住两轮并发。
        self._running_chat_keys: set[str] = set()
        self._max_concurrent = max(1, int(config.MEMORY_SCHEDULER_MAX_CONCURRENT))
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        # 失败退避的重试定时器（键 = 调度键，值 = (定时任务, 到期时间戳)）。
        # 退避可以长达数小时，到点前不进队列：队列里的项会被新入队把最早可执行时间往前挪，
        # 退避就形同虚设。定时器到点后按普通流程入队。
        self._retry_timers: dict[ConsolidationScope, tuple[asyncio.Task, float]] = {}

        # 沉淀处理器（由外部注入）
        self._consolidation_handler: Callable[[int, str], Awaitable["ConsolidationResult"]] | None = None

    async def start(self) -> None:
        """启动调度器"""
        if self._running:
            return
        if not is_memory_system_enabled():
            logger.info("记忆系统总开关关闭，跳过记忆调度器启动")
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._run_loop())
        # 清理循环总是起：开关每一轮现读，关着就跳过这一轮，改了开关不用重启就生效。
        self._prune_task = asyncio.create_task(self._run_prune_loop())
        # 启动后扫一遍积压。完整的启动恢复默认关闭（达到阈值的频道全部补排，避免启动后大批量补跑
        # 拖垮接口响应）；关着时也要兜底：积压超过一批的频道至少排上一项。内存里的待沉淀计数
        # 重启后从 0 算起，不兜底的话这些积压只能等后续新消息一批一批带，换一次版就要消十几个小时。
        # 排上之后由「批次取满就续排」接着消化，节奏受错峰间隔与并发上限约束。
        if config.MEMORY_STARTUP_RECOVERY_ENABLED:
            self._recovery_task = asyncio.create_task(self._recover_pending_consolidations())
        else:
            self._recovery_task = asyncio.create_task(
                self._recover_pending_consolidations(
                    min_pending=int(config.MEMORY_CONSOLIDATION_BATCH_SIZE) + 1,
                    max_tasks=0,
                ),
            )
        # 补齐存量稳定类型的半衰期并捞回被清理的（见 `retention`；域行与工作区行都算）。清理循环
        # 每轮也会先补一次，这里只是让召回排序不必等到第一次清理（默认 6 小时后）才用上新的衰减速度。
        self._retention_task = asyncio.create_task(self._extend_stable_half_life())
        # 预热切词词典。放线程里 + **不 await**：加载是约 0.85s 的纯 CPU 过程，压在事件循环上
        # 等于单进程里的几十个实例一起停摆，而放进启动链路 await 又会让每次重启都多等这一下。
        # 加载完成之前切词自动回落到启发式档（见 `text_terms` 的模块文档）。
        self._tokenizer_warmup_task = asyncio.create_task(warm_up_tokenizer_async())
        logger.info("记忆维护调度器已启动")

    async def stop(self) -> None:
        """停止调度器"""
        self._running = False
        self._wakeup.set()
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
        if self._prune_task and not self._prune_task.done():
            self._prune_task.cancel()
        if self._recovery_task and not self._recovery_task.done():
            self._recovery_task.cancel()
        if self._retention_task and not self._retention_task.done():
            self._retention_task.cancel()
        if self._tokenizer_warmup_task and not self._tokenizer_warmup_task.done():
            self._tokenizer_warmup_task.cancel()
        for task in list(self._execution_tasks):
            if not task.done():
                task.cancel()
        for timer, _due_ts in list(self._retry_timers.values()):
            if not timer.done():
                timer.cancel()
        self._retry_timers.clear()
        async with self._lock:
            self._queue.clear()
            self._versions.clear()
            self._channel_stats.clear()
            self._chat_instance_map.clear()
            self._running_instances.clear()
            self._running_chat_keys.clear()
        self._execution_tasks.clear()
        logger.info("记忆维护调度器已停止")

    def set_consolidation_handler(
        self,
        handler: Callable[[int, str], Awaitable["ConsolidationResult"]],
    ) -> None:
        """设置沉淀处理器

        Args:
            handler: 接收 (workspace_id, chat_key) 并返回 `ConsolidationResult` 的协程函数。
                返回值是调度器区分「已推进 / 跳过 / 失败」的唯一依据，不能吞成 None。
        """
        self._consolidation_handler = handler

    async def on_new_message(
        self,
        chat_key: str,
        workspace_id: int,
        content_length: int = 0,
        *,
        adapter_key: str = "",
        instance_key: str = "",
    ) -> None:
        """新消息事件处理

        Args:
            chat_key: 聊天频道标识
            workspace_id: 工作区 ID（闸开时强制归零，分区恒为域分区）
            content_length: 消息内容长度
            adapter_key: 来源适配器；闸开时用于实例串行与缓存
            instance_key: 来源实例；闸开时用于实例串行与缓存
        """
        if not is_memory_system_enabled():
            return

        if is_domain_scope_enabled():
            # 分区 0 是域行；调度键也用 0，避免同一个未绑工作区频道在不同入口算出不同分区。
            workspace_id = 0
            instance_scope = (adapter_key, instance_key)
        else:
            instance_scope = None

        async with self._lock:
            # 更新映射缓存
            self._chat_workspace_map[chat_key] = workspace_id
            if instance_scope is not None:
                self._chat_instance_map[chat_key] = instance_scope

            # 更新统计
            stats = self._channel_stats.setdefault(
                (workspace_id, chat_key),
                ChannelActivityStats(workspace_id=workspace_id, chat_key=chat_key),
            )
            stats.pending_messages += 1
            stats.last_message_at = datetime.now()

            # 检查是否需要调度沉淀
            priority = self._compute_priority(stats)
            if priority is not None:
                await self._schedule_consolidation(workspace_id, chat_key, priority)

    def _compute_priority(self, stats: ChannelActivityStats) -> ConsolidationPriority | None:
        """计算沉淀优先级

        Returns:
            优先级，或 None 表示不需要沉淀
        """
        now = datetime.now()

        # 失败退避期内不排新任务；到点后清掉标记，让原来的阈值判据继续工作。
        if stats.next_eligible_at is not None:
            if now < stats.next_eligible_at:
                return None
            stats.next_eligible_at = None

        # 检查最小间隔
        if stats.last_consolidation_at:
            since_last = (now - stats.last_consolidation_at).total_seconds()
            if since_last < config.MEMORY_CONSOLIDATION_MIN_INTERVAL_SECONDS:
                return None

        # 计算优先级
        msg_count = stats.pending_messages
        hours_since_last = (
            (now - stats.last_consolidation_at).total_seconds() / 3600
            if stats.last_consolidation_at
            else float("inf")
        )

        # 紧急：超过消息阈值的 2 倍
        if msg_count >= config.MEMORY_CONSOLIDATION_MSG_THRESHOLD * 2:
            return ConsolidationPriority.URGENT

        # 高优先级：达到消息阈值或时间阈值的 2 倍
        if (
            msg_count >= config.MEMORY_CONSOLIDATION_MSG_THRESHOLD
            or hours_since_last >= config.MEMORY_CONSOLIDATION_TIME_THRESHOLD_HOURS * 2
        ):
            return ConsolidationPriority.HIGH

        # 普通优先级：达到时间阈值
        if hours_since_last >= config.MEMORY_CONSOLIDATION_TIME_THRESHOLD_HOURS:
            return ConsolidationPriority.NORMAL

        # 低优先级：有一定消息量
        if msg_count >= config.MEMORY_CONSOLIDATION_MSG_THRESHOLD // 2:
            return ConsolidationPriority.LOW

        return None

    async def _schedule_consolidation(
        self,
        workspace_id: int,
        chat_key: str,
        priority: ConsolidationPriority,
        extra_delay_seconds: float = 0.0,
    ) -> None:
        """把一个频道排进队列；已在队列里就只更新那一项（调用方须持有 `self._lock`）

        更新规则：优先级取更急的那个，最早可执行时间取更早的那个，版本号自增，入队时间不变。
        调度键是 (工作区, 频道)：按工作区记会让同一工作区的不同频道互相覆盖。

        Args:
            extra_delay_seconds: 最早可执行时间往后推多少秒。只用于「同实例 / 同工作区 / 同频道正被
                占着，过一会儿再来」这类延后，**不**随队列长度变化。
        """
        scope: ConsolidationScope = (workspace_id, chat_key)
        if is_domain_scope_enabled():
            # 闸开后一切任务都落在域分区 0；调用方拿到的旧工作区 ID 不能再带进队列，
            # 否则同一频道会同时存在 0 与非 0 两个调度键。
            workspace_id = 0
            scope = (0, chat_key)
        # 该频道正在执行时跳过（本次触发会由下一条消息重新带起来）
        if scope in self._running_scopes:
            return

        version = self._versions.get(scope, 0) + 1
        self._versions[scope] = version

        now = time.time()
        due_at = now + max(0.0, extra_delay_seconds)
        entry = self._queue.get(scope)
        if entry is None:
            self._queue[scope] = ConsolidationTask(
                priority=priority,
                scheduled_at=due_at,
                workspace_id=workspace_id,
                chat_key=chat_key,
                version=version,
                enqueued_at=now,
            )
        else:
            # 数值小的更急；`ConsolidationPriority` 是 int 枚举，`min` 返回的仍是枚举成员
            entry.priority = min(entry.priority, priority)
            entry.scheduled_at = min(entry.scheduled_at, due_at)
            entry.version = version
        self._wakeup.set()

        logger.debug(
            f"调度沉淀任务: workspace={workspace_id}, chat_key={chat_key}, priority={priority.name}, "
            f"delay={due_at - now:.1f}s, queued={len(self._queue)}",
        )

    def queued_tasks(self) -> list[ConsolidationTask]:
        """队列里现有的各项（快照，不保证顺序）"""
        return list(self._queue.values())

    async def _run_loop(self) -> None:
        """主调度循环

        每次醒来只做一件事：派发一项、或者等。等的时候不抱着任何一项——等到最早到期、被唤醒
        （新入队、某一轮跑完）或自检超时为止，醒来后重新挑。
        """
        while self._running:
            try:
                # 先清再看：清之后发生的入队会重新置位，下面的等待会立刻返回，不会漏掉唤醒
                self._wakeup.clear()
                now = time.time()
                self._maybe_report_queue(now)

                if self._queue and len(self._execution_tasks) < self._max_concurrent:
                    gap = max(0.0, float(config.MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS))
                    gap_left = self._last_dispatch_at + gap - now
                    if gap_left > 0:
                        # 错峰：只是推迟下一次派发，手里不拿任何一项，队列照常被更新
                        await asyncio.sleep(min(gap_left, LOOP_IDLE_CHECK_SECONDS))
                        continue
                    task = await self._get_next_task(now, skip_blocked=True)
                    if task is not None:
                        self._last_dispatch_at = time.time()
                        # 不能直接 await：整个调度循环会被单个长任务串行阻塞，并发信号量形同虚设
                        exec_task = asyncio.create_task(self._execute_task(task))
                        self._execution_tasks.add(exec_task)
                        exec_task.add_done_callback(self._on_execution_done)
                        continue

                timeout = self._next_wait_seconds(now)
                if timeout is None:
                    await self._wakeup.wait()
                else:
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"调度循环异常: {e}")
                await asyncio.sleep(1)

    def _on_execution_done(self, exec_task: asyncio.Task) -> None:
        """一轮跑完：腾出并发名额，也可能放开了同实例 / 同工作区排着的项，叫醒主循环重新挑"""
        self._execution_tasks.discard(exec_task)
        self._wakeup.set()

    def _next_wait_seconds(self, now: float) -> float | None:
        """主循环这一次最多等多久；None = 无事可做，只等新入队唤醒"""
        if not self._queue and not self._execution_tasks:
            return None
        wait = float(LOOP_IDLE_CHECK_SECONDS)
        future = [entry.scheduled_at - now for entry in self._queue.values() if entry.scheduled_at > now]
        if future:
            wait = min(wait, min(future))
        return max(0.0, wait)

    def _effective_priority(self, task: ConsolidationTask, now: float) -> int:
        """老化后的优先级：每等 `AGING_STEP_SECONDS` 升一级，最高到 URGENT"""
        step = float(AGING_STEP_SECONDS)
        if step <= 0:
            return int(task.priority)
        promoted = int(max(0.0, now - task.enqueued_at) // step)
        return max(int(ConsolidationPriority.URGENT), int(task.priority) - promoted)

    def _is_blocked(self, task: ConsolidationTask) -> bool:
        """这一项现在派出去也只会被 `_execute_task` 延后：同频道 / 同实例（闸开）/ 同工作区（闸关）正被占着"""
        if task.chat_key in self._running_chat_keys:
            return True
        if is_domain_scope_enabled():
            instance_scope = self._chat_instance_map.get(task.chat_key)
            return instance_scope is not None and instance_scope in self._running_instances
        return task.workspace_id in self._running_workspaces

    async def _db_backlog(self, chat_key: str) -> int | None:
        """库里这个频道游标之后还有多少条消息

        Returns:
            条数；频道不存在、还没有游标，查询失败或超时时返回 None（调用方回落到内存计数）。
        """
        from nekro_agent.models.db_chat_channel import DBChatChannel
        from nekro_agent.models.db_chat_message import DBChatMessage

        async def query() -> int | None:
            channel = await DBChatChannel.get_or_none(chat_key=chat_key)
            if channel is None:
                return None
            cursor = _memory_cursor(channel.data)
            if cursor is None:
                return None
            return await DBChatMessage.filter(chat_key=chat_key, id__gt=cursor).count()

        try:
            return await asyncio.wait_for(query(), timeout=DB_BACKLOG_TIMEOUT_SECONDS)
        except Exception as e:
            logger.debug(f"读取频道积压失败，回落到内存计数: chat_key={chat_key}, error={e}")
            return None

    def _maybe_report_queue(self, now: float) -> None:
        """队列观测：队列非空或有沉淀在跑时，每 `QUEUE_REPORT_INTERVAL_SECONDS` 最多一行 INFO"""
        if now - self._last_queue_report_at < float(QUEUE_REPORT_INTERVAL_SECONDS):
            return
        if not self._queue and not self._running_scopes:
            return
        self._last_queue_report_at = now
        running = len(self._running_scopes)
        if not self._queue:
            logger.info(f"记忆沉淀队列: 排队 0 个频道，执行中 {running} 个")
            return
        due = [entry for entry in self._queue.values() if entry.scheduled_at <= now]
        blocked = sum(1 for entry in due if self._is_blocked(entry))
        oldest = min(self._queue.values(), key=lambda entry: entry.enqueued_at)
        logger.info(
            f"记忆沉淀队列: 排队 {len(self._queue)} 个频道（已到期 {len(due)}，其中被同实例 / 同工作区占着 {blocked}），"
            f"执行中 {running} 个；最久的已等 {now - oldest.enqueued_at:.0f} 秒: "
            f"chat_key={oldest.chat_key}, priority={oldest.priority.name}",
        )

    async def _extend_stable_half_life(self) -> None:
        """补齐存量半衰期，再捞回按旧半衰期被清理掉的稳定类型段落与关系

        启动时跑一次，之后清理循环每一轮跑一次（清理开关关着也跑：先核对捞回数、再开清理是
        升级后的正常顺序）。两种捞回都是一次性的，做完一遍以后这里只剩两条不命中的 UPDATE。
        段落先捞、关系后捞：跟着段落一起被清的关系，要等段落回来才在范围里。
        失败只记日志，下一轮再来。取消要往上抛：`stop()` 落在这里时，调用方不能接着跑一整轮清理。
        """
        try:
            await extend_stable_half_life()
            await extend_stable_relation_half_life()
            await restore_pruned_stable()
            await restore_pruned_stable_relations()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"补齐存量域记忆半衰期或捞回被清理的段落失败（下一轮再试）: error={e}")

    async def _run_prune_loop(self) -> None:
        """后台定期清理低价值记忆

        `MEMORY_PRUNE_ENABLED` 与间隔每一轮现读：关掉开关，下一轮起就不再清理；再打开也不用重启。
        """
        while self._running:
            try:
                interval_seconds = max(PRUNE_MIN_INTERVAL_SECONDS, config.MEMORY_PRUNE_INTERVAL_HOURS * 3600)
                await asyncio.sleep(interval_seconds)
                if not self._running:
                    return
                await self._extend_stable_half_life()
                if not self._running:
                    return
                if not config.MEMORY_PRUNE_ENABLED:
                    logger.debug("自动记忆清理已关闭，本轮跳过")
                    continue
                results = await prune_all_workspaces()
                total_paragraphs = sum(item.paragraphs_pruned for item in results.values())
                total_relations = sum(item.relations_pruned for item in results.values())
                total_entities = sum(item.entities_pruned for item in results.values())
                logger.info(
                    f"自动记忆清理完成: workspaces={len(results)}, "
                    f"paragraphs={total_paragraphs}, relations={total_relations}, entities={total_entities}",
                )
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.exception(f"自动记忆清理循环异常: {e}")
                await asyncio.sleep(60)

    async def _get_next_task(
        self,
        now: float | None = None,
        *,
        skip_blocked: bool = False,
    ) -> ConsolidationTask | None:
        """从队列里取出下一项：只看已到期的，按（老化后的优先级，入队先后）挑最前面的那个

        Args:
            now: 判断到期与老化用的时刻；默认取当前时间。
            skip_blocked: 主循环传 True，跳过派出去也只会被延后的项（见 `_is_blocked`），
                让别的实例 / 工作区的频道先跑；同一实例的那一轮跑完会唤醒主循环再来挑。

        Returns:
            取出的那一项；没有可取的返回 None。
        """
        async with self._lock:
            current = time.time() if now is None else now
            best: ConsolidationTask | None = None
            best_key: tuple[int, float, float] | None = None
            for entry in self._queue.values():
                if entry.scheduled_at > current:
                    continue
                if skip_blocked and self._is_blocked(entry):
                    continue
                key = (self._effective_priority(entry, current), entry.enqueued_at, entry.scheduled_at)
                if best_key is None or key < best_key:
                    best, best_key = entry, key
            if best is not None:
                del self._queue[(best.workspace_id, best.chat_key)]
            return best

    async def _execute_task(self, task: ConsolidationTask) -> None:
        """执行沉淀任务"""
        gate_open = is_domain_scope_enabled()
        workspace_id = 0 if gate_open else task.workspace_id
        scope: ConsolidationScope = (workspace_id, task.chat_key)

        if self._consolidation_handler is None:
            logger.warning("沉淀处理器未设置，跳过任务")
            return

        if not gate_open and task.workspace_id == 0:
            # 闸开期间排进来的域分区任务，执行前闸已经关了：此时分区 0 不该再被写入
            # （闸关的检索不看域行，写进去的记忆闸关期间根本召回不到，而且绕过了闸开时才做的
            # 「涉及的人」）。直接丢掉；这个频道下一条新消息会按闸关的规则重新登记。
            logger.info(f"记忆域分区已关闭，丢弃排队中的域分区沉淀任务: chat_key={task.chat_key}")
            return

        if gate_open:
            from nekro_agent.services.memory.rebuild import is_domain_channel_rebuild_running

            if is_domain_channel_rebuild_running(task.chat_key):
                # 重建正在删 / 写这个频道的域行，此时再跑实时沉淀就是两边对着写。
                # 只挂一个 30 秒后的重排，`pending` 原样留着，等重建结束再消化。
                logger.info(f"频道域行重建进行中，延后实时沉淀: chat_key={task.chat_key}")
                self._arm_retry_timer(
                    workspace_id,
                    task.chat_key,
                    task.priority,
                    DOMAIN_REBUILD_RETRY_DELAY_SECONDS,
                )
                return

        try:
            async with self._semaphore:
                if not gate_open:
                    from nekro_agent.services.memory.rebuild import is_workspace_memory_rebuild_running

                    if is_workspace_memory_rebuild_running(workspace_id):
                        logger.info(f"工作区记忆重建进行中，延后实时沉淀: workspace={workspace_id}")
                        async with self._lock:
                            await self._schedule_consolidation(
                                workspace_id,
                                task.chat_key,
                                task.priority,
                                extra_delay_seconds=max(2.0, config.MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS),
                            )
                        return

                # 闸关时同一工作区内串行；闸开后串行粒度改成 (适配器, 实例)，
                # 跨工作区共用一个实例的频道也要串，否则实体「先查后建」仍会撞。
                instance_scope: tuple[str, str] | None = None
                if gate_open:
                    instance_scope = self._chat_instance_map.get(task.chat_key)
                    if instance_scope is None:
                        from nekro_agent.models.db_chat_channel import DBChatChannel

                        channel = await DBChatChannel.get_or_none(chat_key=task.chat_key)
                        if channel is not None:
                            instance_scope = (channel.adapter_key, channel.instance_key)
                        else:
                            logger.warning(
                                f"沉淀任务缺少频道记录，无法按实例串行: workspace={workspace_id}, "
                                f"chat_key={task.chat_key}",
                            )
                            instance_scope = (f"workspace:{workspace_id}", task.chat_key)
                    if instance_scope in self._running_instances:
                        logger.debug(
                            f"同实例已有沉淀在执行，延后本频道: instance={instance_scope}, chat_key={task.chat_key}",
                        )
                        async with self._lock:
                            await self._schedule_consolidation(
                                workspace_id,
                                task.chat_key,
                                task.priority,
                                extra_delay_seconds=max(2.0, config.MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS),
                            )
                        return
                elif workspace_id in self._running_workspaces:
                    logger.debug(
                        f"同工作区已有沉淀在执行，延后本频道: workspace={workspace_id}, chat_key={task.chat_key}",
                    )
                    async with self._lock:
                        await self._schedule_consolidation(
                            workspace_id,
                            task.chat_key,
                            task.priority,
                            extra_delay_seconds=max(2.0, config.MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS),
                        )
                    return

                if task.chat_key in self._running_chat_keys:
                    # 同一频道已有一轮在跑（多半是闸切换前后的另一种调度键）：两轮会并发读同一批
                    # 消息、各推各的游标，往后挪。
                    async with self._lock:
                        await self._schedule_consolidation(
                            workspace_id,
                            task.chat_key,
                            task.priority,
                            extra_delay_seconds=max(2.0, config.MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS),
                        )
                    return
                self._running_scopes.add(scope)
                self._running_chat_keys.add(task.chat_key)
                if gate_open and instance_scope is not None:
                    self._running_instances.add(instance_scope)
                self._running_workspaces.add(workspace_id)
                retry_delay: float | None = None
                continuation_priority: ConsolidationPriority | None = None
                try:
                    logger.info(f"开始执行沉淀: workspace={workspace_id}, chat_key={task.chat_key}")
                    result = await self._consolidation_handler(workspace_id, task.chat_key)

                    # 结果表：失败不清 pending、不记 last_consolidation_at；成功与跳过按实际处理量扣减。
                    if result.failed_reason:
                        retry_delay = await self._mark_failure_backoff(scope, result.retry_after)
                        if result.failed_reason == "backoff":
                            # 沉淀器还在上一次失败的退避期里，这一轮根本没有执行：不是新的失败，
                            # 记成失败会让人以为这个频道又坏了一次。
                            logger.info(
                                f"沉淀处于退避期，跳过，到点重排: workspace={workspace_id}, "
                                f"chat_key={task.chat_key}, retry_after={result.retry_after}, "
                                f"delay={retry_delay:.1f}s",
                            )
                        else:
                            logger.warning(
                                f"沉淀失败，退避后重试: workspace={workspace_id}, chat_key={task.chat_key}, "
                                f"reason={result.failed_reason}, delay={retry_delay:.1f}s",
                            )
                    else:
                        # 真正推进了游标的一轮，用库里游标之后的实际条数校正待沉淀数：内存计数重启后
                        # 从 0 算起，只靠它判断「后面还有没有」会低估积压，续排就不触发。
                        db_backlog: int | None = None
                        if not result.cursor_initialized and not result.skipped_reason:
                            db_backlog = await self._db_backlog(task.chat_key)
                        async with self._lock:
                            stats = self._channel_stats.get(scope)
                            if stats is None and db_backlog:
                                stats = self._channel_stats.setdefault(
                                    scope,
                                    ChannelActivityStats(workspace_id=workspace_id, chat_key=task.chat_key),
                                )
                            if stats is not None:
                                if result.cursor_initialized:
                                    stats.pending_messages = 0
                                elif result.skipped_reason == "memory_disabled":
                                    # 开关已关的频道不该继续累计待沉淀消息；留着会让重新打开时
                                    # 一次性沉淀一批关闭期间的消息。
                                    stats.pending_messages = 0
                                elif db_backlog is not None:
                                    stats.pending_messages = db_backlog
                                else:
                                    # 走到这里也包括「频道额度暂停」这类跳过：这一批一条都没做，
                                    # `messages_processed` 是 0，待处理数原样保留，恢复后从断点继续。
                                    # 读不到库里的积压时也按内存计数扣减（与改造前相同）。
                                    stats.pending_messages = max(
                                        0,
                                        stats.pending_messages - max(0, result.messages_processed),
                                    )
                                stats.last_consolidation_at = datetime.now()
                                if not result.skipped_reason:
                                    stats.consecutive_skips = 0
                                stats.next_eligible_at = None
                                if (
                                    not result.cursor_initialized
                                    and result.messages_processed == config.MEMORY_CONSOLIDATION_BATCH_SIZE
                                    and stats.pending_messages > 0
                                ):
                                    # 这一批取满了、后面还有积压（读得到库时以库为准）：立刻按普通优先级
                                    # 再排一次，不等新消息来带。否则静默的积压要等下一次有人说话才开始消化。
                                    continuation_priority = ConsolidationPriority.NORMAL

                        if result.skipped_reason:
                            logger.info(
                                f"沉淀跳过: workspace={workspace_id}, chat_key={task.chat_key}, "
                                f"reason={result.skipped_reason}",
                            )
                        elif result.cursor_initialized:
                            logger.info(
                                f"沉淀只初始化了游标: workspace={workspace_id}, chat_key={task.chat_key}",
                            )
                        else:
                            logger.info(f"沉淀完成: workspace={workspace_id}, chat_key={task.chat_key}")
                            cooldown = max(0.0, float(config.MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS))
                            if cooldown > 0:
                                await asyncio.sleep(cooldown)

                except Exception as e:
                    logger.exception(f"沉淀任务失败: workspace={workspace_id}, error={e}")
                    retry_delay = await self._mark_failure_backoff(scope, None)
                finally:
                    self._running_scopes.discard(scope)
                    self._running_chat_keys.discard(task.chat_key)
                    if instance_scope is not None:
                        self._running_instances.discard(instance_scope)
                    self._running_workspaces.discard(workspace_id)
                    # 排队必须放在丢弃 `_running_scopes` 之后：否则 `_schedule_consolidation`
                    # 会以为本任务还在跑，直接跳过重排。
                    if retry_delay is not None:
                        self._arm_retry_timer(workspace_id, task.chat_key, task.priority, retry_delay)
                    elif continuation_priority is not None:
                        async with self._lock:
                            await self._schedule_consolidation(
                                workspace_id,
                                task.chat_key,
                                continuation_priority,
                            )
        except Exception as e:
            logger.exception(f"沉淀任务调度执行异常: workspace={workspace_id}, error={e}")

    def _arm_retry_timer(
        self,
        workspace_id: int,
        chat_key: str,
        priority: ConsolidationPriority,
        delay_seconds: float,
    ) -> None:
        """失败退避到点后再把任务排进队列（同一调度键只保留最新的一个定时器）

        不直接把一个「N 秒后才到期」的项放进队列：队列里的项会被新入队把最早可执行时间往前挪
        （见 `_schedule_consolidation`），退避（沉淀器的退避最长 6 小时）就形同虚设。定时器到点前
        不进队列，到点后按普通流程排队。
        """
        scope: ConsolidationScope = (workspace_id, chat_key)
        previous = self._retry_timers.pop(scope, None)
        if previous is not None and not previous[0].done():
            previous[0].cancel()

        delay = max(0.0, delay_seconds)

        async def _fire() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            current = self._retry_timers.get(scope)
            if current is not None and current[0] is asyncio.current_task():
                self._retry_timers.pop(scope, None)
            async with self._lock:
                await self._schedule_consolidation(workspace_id, chat_key, priority)

        timer = asyncio.create_task(_fire())
        self._retry_timers[scope] = (timer, time.time() + delay)

    def pending_retry_due(self, workspace_id: int, chat_key: str) -> float | None:
        """某个调度键的退避重试预计在什么时候触发（时间戳）；没有待触发的重试返回 None"""
        entry = self._retry_timers.get((workspace_id, chat_key))
        if entry is None or entry[0].done():
            return None
        return entry[1]

    async def _mark_failure_backoff(self, scope: ConsolidationScope, retry_after: str | None) -> float:
        """记录一次失败并算出退避秒数；重排由调用方在释放运行标记后执行

        `retry_after` 是沉淀器写进结果的 ISO 时间；解析不了就只按最小间隔退避。
        退避截止前 `_compute_priority` 一律返回 None，新消息不会把任务提前排进来。
        """
        delay = max(0.0, float(config.MEMORY_CONSOLIDATION_MIN_INTERVAL_SECONDS))
        if retry_after:
            try:
                parsed = datetime.fromisoformat(retry_after)
            except ValueError:
                parsed = None
            if parsed is not None:
                # 沉淀器写的是带时区的 ISO 时间时，先换回本地时钟再和 `datetime.now()` 比；
                # 直接 `replace(tzinfo=None)` 会把 UTC 时间当成当地时间，退避被缩短一个时区差。
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                delay = max(delay, (parsed - datetime.now()).total_seconds())

        async with self._lock:
            stats = self._channel_stats.get(scope)
            if stats is not None:
                stats.consecutive_skips += 1
                stats.next_eligible_at = datetime.now() + timedelta(seconds=delay)
        return delay

    async def force_consolidation(self, workspace_id: int, chat_key: str) -> bool:
        """强制触发沉淀

        Args:
            workspace_id: 工作区 ID
            chat_key: 聊天频道标识

        Returns:
            是否成功调度
        """
        async with self._lock:
            if is_domain_scope_enabled():
                workspace_id = 0
            if (workspace_id, chat_key) in self._running_scopes:
                return False
            await self._schedule_consolidation(
                workspace_id,
                chat_key,
                ConsolidationPriority.URGENT,
            )
            return True

    async def _recover_pending_consolidations(
        self,
        *,
        min_pending: int | None = None,
        max_tasks: int | None = None,
    ) -> None:
        """重启后按库里的积压把频道排进队列

        扫描启用了记忆的频道（闸开）或活跃工作区绑定的频道（闸关），游标之后的消息数达到门槛的
        排一项，并用这个实际条数校正内存里的待沉淀数。

        Args:
            min_pending: 积压达到多少才排；默认 `MEMORY_CONSOLIDATION_MSG_THRESHOLD`（完整的启动恢复）。
                启动恢复关闭时传「批大小 + 1」，只兜住积压超过一批的频道。
            max_tasks: 最多排几项，0 不限；默认 `MEMORY_STARTUP_RECOVERY_MAX_TASKS`。
        """
        threshold = int(config.MEMORY_CONSOLIDATION_MSG_THRESHOLD) if min_pending is None else int(min_pending)
        limit = int(config.MEMORY_STARTUP_RECOVERY_MAX_TASKS) if max_tasks is None else int(max_tasks)
        try:
            import json

            from nekro_agent.models.db_chat_channel import DBChatChannel
            from nekro_agent.models.db_chat_message import DBChatMessage
            from nekro_agent.models.db_workspace import DBWorkspace

            # 等待一段时间，确保数据库和其他服务就绪
            await asyncio.sleep(RECOVERY_START_DELAY_SECONDS)

            workspaces = await DBWorkspace.filter(status="active").all()
            recovered_count = 0
            max_tasks = max(0, limit)

            if is_domain_scope_enabled():
                # 闸开后频道记忆不再要求绑工作区，恢复必须按频道自己的开关扫全表；
                # 再按 workspace_id 扫活跃工作区会把分区 0 的频道全部漏掉。
                # 按主键分页：频道数上千时一次取全表会把整表读进内存，且在扫描期间占住事件循环。
                cursor = 0
                while True:
                    channels = (
                        await DBChatChannel.filter(is_active=True, id__gt=cursor)
                        .order_by("id")
                        .limit(RECOVERY_CHANNEL_PAGE_SIZE)
                    )
                    if not channels:
                        break
                    cursor = channels[-1].id
                    for channel in channels:
                        try:
                            if not await is_memory_enabled_for_channel(channel):
                                continue
                            # 闸开后调度键恒为分区 0；绑了工作区的频道也不能把非 0 分区
                            # 带进统计与任务队列，否则执行时取不到同一份 pending。
                            workspace_id = 0
                            last_consolidated_db_id = 0
                            has_cursor = False
                            try:
                                data = json.loads(channel.data or "{}")
                                if isinstance(data, dict):
                                    memory_state = data.get("memory_state", {})
                                    if isinstance(memory_state, dict):
                                        raw_db_id = memory_state.get("last_consolidated_message_db_id", 0)
                                        has_cursor = "last_consolidated_message_db_id" in memory_state
                                        if isinstance(raw_db_id, int) and raw_db_id >= 0:
                                            last_consolidated_db_id = raw_db_id
                            except json.JSONDecodeError:
                                pass
                            if not has_cursor:
                                # 开启之前的消息不回溯；没有游标说明这个频道还没真正开始沉淀。
                                continue

                            pending_count = await DBChatMessage.filter(
                                chat_key=channel.chat_key,
                                id__gt=last_consolidated_db_id,
                            ).count()
                            if pending_count >= threshold:
                                async with self._lock:
                                    self._chat_workspace_map[channel.chat_key] = workspace_id
                                    self._chat_instance_map[channel.chat_key] = (
                                        channel.adapter_key,
                                        channel.instance_key,
                                    )
                                    stats = self._channel_stats.setdefault(
                                        (workspace_id, channel.chat_key),
                                        ChannelActivityStats(workspace_id=workspace_id, chat_key=channel.chat_key),
                                    )
                                    stats.pending_messages = pending_count
                                    await self._schedule_consolidation(
                                        workspace_id,
                                        channel.chat_key,
                                        ConsolidationPriority.NORMAL,
                                    )
                                recovered_count += 1
                                logger.debug(
                                    f"恢复待处理沉淀: workspace={workspace_id}, "
                                    f"chat_key={channel.chat_key}, pending={pending_count}",
                                )
                                if max_tasks and recovered_count >= max_tasks:
                                    logger.info(
                                        f"启动恢复已达到任务上限: recovered={recovered_count}, max={max_tasks}",
                                    )
                                    logger.info(f"已按积压排上 {recovered_count} 个频道的沉淀（门槛 {threshold} 条）")
                                    return
                        except Exception as e:
                            logger.warning(f"恢复频道沉淀任务失败: chat_key={channel.chat_key}, error={e}")

                if recovered_count > 0:
                    logger.info(f"已按积压排上 {recovered_count} 个频道的沉淀（门槛 {threshold} 条）")
                return

            for ws in workspaces:
                try:
                    # 获取该工作区的所有频道
                    channels = await DBChatChannel.filter(workspace_id=ws.id).all()

                    for channel in channels:
                        # 读取记忆状态
                        last_consolidated_db_id = 0
                        try:
                            data = json.loads(channel.data or "{}")
                            if isinstance(data, dict):
                                memory_state = data.get("memory_state", {})
                                if isinstance(memory_state, dict):
                                    raw_db_id = memory_state.get("last_consolidated_message_db_id", 0)
                                    if isinstance(raw_db_id, int) and raw_db_id >= 0:
                                        last_consolidated_db_id = raw_db_id
                        except json.JSONDecodeError:
                            pass

                        # 统计待处理消息数
                        pending_count = await DBChatMessage.filter(
                            chat_key=channel.chat_key,
                            id__gt=last_consolidated_db_id,
                        ).count()

                        # 如果达到阈值，调度沉淀任务
                        if pending_count >= threshold:
                            async with self._lock:
                                self._chat_workspace_map[channel.chat_key] = ws.id
                                stats = self._channel_stats.setdefault(
                                    (ws.id, channel.chat_key),
                                    ChannelActivityStats(workspace_id=ws.id, chat_key=channel.chat_key),
                                )
                                stats.pending_messages = pending_count
                                await self._schedule_consolidation(
                                    ws.id,
                                    channel.chat_key,
                                    ConsolidationPriority.NORMAL,
                                )
                            recovered_count += 1
                            logger.debug(
                                f"恢复待处理沉淀: workspace={ws.id}, "
                                f"chat_key={channel.chat_key}, pending={pending_count}",
                            )
                            if max_tasks and recovered_count >= max_tasks:
                                logger.info(
                                    f"启动恢复已达到任务上限: recovered={recovered_count}, max={max_tasks}",
                                )
                                logger.info(f"已按积压排上 {recovered_count} 个频道的沉淀（门槛 {threshold} 条）")
                                return

                except Exception as e:
                    logger.warning(f"恢复工作区沉淀任务失败: workspace={ws.id}, error={e}")

            if recovered_count > 0:
                logger.info(f"已按积压排上 {recovered_count} 个频道的沉淀（门槛 {threshold} 条）")

        except Exception as e:
            logger.warning(f"恢复待处理沉淀任务失败: {e}")

    def get_stats(self) -> dict[str, dict]:
        """获取所有频道的沉淀统计信息（键为 `{workspace_id}:{chat_key}`）"""
        return {
            f"{ws_id}:{chat_key}": {
                "workspace_id": ws_id,
                "chat_key": chat_key,
                "pending_messages": stats.pending_messages,
                "last_message_at": stats.last_message_at.isoformat() if stats.last_message_at else None,
                "last_consolidation_at": (stats.last_consolidation_at.isoformat() if stats.last_consolidation_at else None),
                "consecutive_skips": stats.consecutive_skips,
                "next_eligible_at": stats.next_eligible_at.isoformat() if stats.next_eligible_at else None,
            }
            for (ws_id, chat_key), stats in self._channel_stats.items()
        }


# 全局单例
memory_scheduler = MemoryMaintenanceScheduler()
