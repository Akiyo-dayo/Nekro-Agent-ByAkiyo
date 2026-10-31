"""记忆维护调度器

事件驱动 + 优先级队列的自适应调度系统。
监听消息事件，动态决定何时触发记忆沉淀。
"""

from __future__ import annotations

import asyncio
import heapq
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.memory.feature_flags import is_memory_system_enabled
from nekro_agent.services.memory.maintenance import prune_all_workspaces
from nekro_agent.services.memory.text_terms import warm_up_tokenizer_async

logger = get_sub_logger("memory.scheduler")


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


@dataclass(order=True)
class ConsolidationTask:
    """沉淀任务"""

    priority: ConsolidationPriority
    scheduled_at: float = field(compare=True)  # 计划执行时间戳
    workspace_id: int = field(compare=False)
    chat_key: str = field(compare=False)
    version: int = field(compare=False, default=0)  # 用于任务更新


class MemoryMaintenanceScheduler:
    """记忆维护调度器

    设计目标：
    - 事件驱动：消息到达时更新统计，动态调整优先级
    - 低 CPU：使用堆 + sleep，不做轮询
    - 自适应：根据工作区活跃度调整沉淀频率
    - 并发控制：限制同时执行的沉淀任务数
    """

    def __init__(self) -> None:
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._prune_task: asyncio.Task | None = None
        self._tokenizer_warmup_task: asyncio.Task | None = None
        self._execution_tasks: set[asyncio.Task] = set()
        self._wakeup = asyncio.Event()
        self._lock = asyncio.Lock()

        # 频道统计（键 = (workspace_id, chat_key)）
        self._channel_stats: dict[ConsolidationScope, ChannelActivityStats] = {}
        # chat_key -> workspace_id 映射缓存
        self._chat_workspace_map: dict[str, int] = {}

        # 优先级队列
        self._heap: list[ConsolidationTask] = []
        self._versions: dict[ConsolidationScope, int] = {}  # (工作区, 频道) -> 当前版本

        # 并发控制
        self._running_scopes: set[ConsolidationScope] = set()  # 正在执行的 (工作区, 频道)
        # 同一工作区内仍然串行：`DBMemEntity.find_or_create` 是「先查后建」，同实例的两个频道
        # 并发沉淀时会把同一个实体建成两行。跨工作区的并发上限由信号量控制，这里只挡工作区内部。
        self._running_workspaces: set[int] = set()
        self._semaphore = asyncio.Semaphore(config.MEMORY_SCHEDULER_MAX_CONCURRENT)

        # 沉淀处理器（由外部注入）
        self._consolidation_handler: Callable[[int, str], asyncio.Task] | None = None

    async def start(self) -> None:
        """启动调度器"""
        if self._running:
            return
        if not is_memory_system_enabled():
            logger.info("记忆系统总开关关闭，跳过记忆调度器启动")
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._run_loop())
        if config.MEMORY_PRUNE_ENABLED:
            self._prune_task = asyncio.create_task(self._run_prune_loop())
        # 异步恢复待处理的沉淀任务。默认关闭，避免启动后历史大批量补跑拖垮接口响应。
        if config.MEMORY_STARTUP_RECOVERY_ENABLED:
            asyncio.create_task(self._recover_pending_consolidations())
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
        if self._tokenizer_warmup_task and not self._tokenizer_warmup_task.done():
            self._tokenizer_warmup_task.cancel()
        for task in list(self._execution_tasks):
            if not task.done():
                task.cancel()
        async with self._lock:
            self._heap.clear()
            self._versions.clear()
            self._channel_stats.clear()
        self._execution_tasks.clear()
        logger.info("记忆维护调度器已停止")

    def set_consolidation_handler(
        self,
        handler: Callable[[int, str], asyncio.Task],
    ) -> None:
        """设置沉淀处理器

        Args:
            handler: 接收 (workspace_id, chat_key) 返回 Task 的函数
        """
        self._consolidation_handler = handler

    async def on_new_message(
        self,
        chat_key: str,
        workspace_id: int,
        content_length: int = 0,
    ) -> None:
        """新消息事件处理

        Args:
            chat_key: 聊天频道标识
            workspace_id: 工作区 ID
            content_length: 消息内容长度
        """
        if not is_memory_system_enabled():
            return

        async with self._lock:
            # 更新映射缓存
            self._chat_workspace_map[chat_key] = workspace_id

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
        """调度沉淀任务

        版本号按 (工作区, 频道) 记：它的作用是让同一个频道的旧任务作废、只跑最新的那个，
        按工作区记会把**别的频道**排好的任务一起作废掉。
        """
        scope: ConsolidationScope = (workspace_id, chat_key)
        # 该频道正在执行时跳过（本次触发会由下一条消息重新带起来）
        if scope in self._running_scopes:
            return

        # 更新版本号
        version = self._versions.get(scope, 0) + 1
        self._versions[scope] = version

        # 计算调度时间（错峰）
        delay = len(self._heap) * config.MEMORY_SCHEDULER_STAGGER_DELAY_SECONDS + max(0.0, extra_delay_seconds)
        scheduled_at = time.time() + delay

        task = ConsolidationTask(
            priority=priority,
            scheduled_at=scheduled_at,
            workspace_id=workspace_id,
            chat_key=chat_key,
            version=version,
        )
        heapq.heappush(self._heap, task)
        self._wakeup.set()

        logger.debug(
            f"调度沉淀任务: workspace={workspace_id}, priority={priority.name}, delay={delay}s",
        )

    async def _run_loop(self) -> None:
        """主调度循环"""
        while self._running:
            try:
                # 获取下一个任务
                task = await self._get_next_task()
                if task is None:
                    # 等待新任务
                    self._wakeup.clear()
                    await self._wakeup.wait()
                    continue

                # 等待到计划时间
                now = time.time()
                if task.scheduled_at > now:
                    await asyncio.sleep(task.scheduled_at - now)

                # 执行任务。这里不能直接 await，否则整个调度循环会被单个长任务串行阻塞，
                # 导致并发信号量形同虚设。
                exec_task = asyncio.create_task(self._execute_task(task))
                self._execution_tasks.add(exec_task)
                exec_task.add_done_callback(self._execution_tasks.discard)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"调度循环异常: {e}")
                await asyncio.sleep(1)

    async def _run_prune_loop(self) -> None:
        """后台定期清理低价值记忆。"""
        interval_seconds = max(3600, config.MEMORY_PRUNE_INTERVAL_HOURS * 3600)
        while self._running:
            try:
                await asyncio.sleep(interval_seconds)
                if not self._running:
                    return
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

    async def _get_next_task(self) -> ConsolidationTask | None:
        """获取下一个有效任务"""
        async with self._lock:
            while self._heap:
                task = heapq.heappop(self._heap)
                # 检查版本是否有效
                if self._versions.get((task.workspace_id, task.chat_key), 0) == task.version:
                    return task
            return None

    async def _execute_task(self, task: ConsolidationTask) -> None:
        """执行沉淀任务"""
        workspace_id = task.workspace_id
        scope: ConsolidationScope = (workspace_id, task.chat_key)

        if self._consolidation_handler is None:
            logger.warning("沉淀处理器未设置，跳过任务")
            return

        try:
            async with self._semaphore:
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

                # 同一工作区内串行：多频道工作区里两个频道的任务现在可以同时排在队列里，
                # 但沉淀会碰同一批实体行，撞上就重排自己（对方跑完就会放行，不会互相等待）。
                if workspace_id in self._running_workspaces:
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

                self._running_scopes.add(scope)
                self._running_workspaces.add(workspace_id)
                try:
                    logger.info(f"开始执行沉淀: workspace={workspace_id}, chat_key={task.chat_key}")
                    await self._consolidation_handler(workspace_id, task.chat_key)

                    # 更新统计
                    async with self._lock:
                        if scope in self._channel_stats:
                            stats = self._channel_stats[scope]
                            stats.pending_messages = 0
                            stats.last_consolidation_at = datetime.now()
                            stats.consecutive_skips = 0

                    logger.info(f"沉淀完成: workspace={workspace_id}, chat_key={task.chat_key}")
                    cooldown = max(0.0, float(config.MEMORY_CONSOLIDATION_BATCH_COOLDOWN_SECONDS))
                    if cooldown > 0:
                        await asyncio.sleep(cooldown)

                except Exception as e:
                    logger.exception(f"沉淀任务失败: workspace={workspace_id}, error={e}")
                    # 增加跳过计数
                    async with self._lock:
                        if scope in self._channel_stats:
                            self._channel_stats[scope].consecutive_skips += 1
                finally:
                    self._running_scopes.discard(scope)
                    self._running_workspaces.discard(workspace_id)
        except Exception as e:
            logger.exception(f"沉淀任务调度执行异常: workspace={workspace_id}, error={e}")

    async def force_consolidation(self, workspace_id: int, chat_key: str) -> bool:
        """强制触发沉淀

        Args:
            workspace_id: 工作区 ID
            chat_key: 聊天频道标识

        Returns:
            是否成功调度
        """
        async with self._lock:
            if (workspace_id, chat_key) in self._running_scopes:
                return False
            await self._schedule_consolidation(
                workspace_id,
                chat_key,
                ConsolidationPriority.URGENT,
            )
            return True

    async def _recover_pending_consolidations(self) -> None:
        """重启后恢复待处理的沉淀任务

        扫描所有活跃工作区，检查是否有达到阈值的待沉淀消息。
        """
        try:
            import json

            from nekro_agent.models.db_chat_channel import DBChatChannel
            from nekro_agent.models.db_chat_message import DBChatMessage
            from nekro_agent.models.db_workspace import DBWorkspace

            # 等待一段时间，确保数据库和其他服务就绪
            await asyncio.sleep(3)

            workspaces = await DBWorkspace.filter(status="active").all()
            recovered_count = 0
            max_tasks = max(0, int(config.MEMORY_STARTUP_RECOVERY_MAX_TASKS))

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
                        if pending_count >= config.MEMORY_CONSOLIDATION_MSG_THRESHOLD:
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
                                logger.info(f"已恢复 {recovered_count} 个待处理的沉淀任务")
                                return

                except Exception as e:
                    logger.warning(f"恢复工作区沉淀任务失败: workspace={ws.id}, error={e}")

            if recovered_count > 0:
                logger.info(f"已恢复 {recovered_count} 个待处理的沉淀任务")

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
            }
            for (ws_id, chat_key), stats in self._channel_stats.items()
        }


# 全局单例
memory_scheduler = MemoryMaintenanceScheduler()
