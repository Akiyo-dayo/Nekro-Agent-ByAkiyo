"""机器人公告异步发送服务

服务端持有完整任务生命周期：提交后立刻返回任务 ID，实际投递在后台任务中进行，
前端只负责订阅进度，因此关闭页面不会中断群发。

速率控制按适配器实例独立计算：不同账号的风控额度互相独立，
全局串行只会白白拖慢整体耗时，因此同一实例内串行 + 随机延迟，不同实例之间并发。
"""

import asyncio
import json
import random
from typing import Callable, Dict, List, Set, Tuple

from tortoise.timezone import now as db_now

from nekro_agent.adapters import get_adapter
from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_announcement import (
    AnnouncementScopeType,
    AnnouncementStatus,
    DBAnnouncement,
    DBAnnouncementDelivery,
)
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.agent_message import AgentMessageSegment, AgentMessageSegmentType
from nekro_agent.schemas.errors import AdapterUnavailableError
from nekro_agent.services.config_resolver import config_resolver

from .schemas import (
    AnnouncementDeliveryEvent,
    AnnouncementDeliveryItem,
    AnnouncementDetail,
    AnnouncementEvent,
    AnnouncementInstanceOption,
    AnnouncementInstanceRef,
    AnnouncementPreview,
    AnnouncementScope,
    AnnouncementSnapshotEvent,
    AnnouncementStatusEvent,
    AnnouncementSummary,
    AnnouncementTarget,
)

logger = get_sub_logger("announcement")

# 熔断需要的最小样本量：只发两三个群时一次偶发失败就跳闸会让正常公告频繁中断，
# 因此失败率判定必须建立在足够样本之上，阈值本身仍由配置控制。
FAILURE_BREAK_MIN_SAMPLES = 5

RESTART_INTERRUPTED_REASON = "服务重启导致任务中断，未发送的会话不会自动补发"


class AnnouncementSubscription:
    """公告进度订阅句柄"""

    def __init__(self, queue: "asyncio.Queue[AnnouncementEvent]", cleanup: Callable[[], None]) -> None:
        self._queue = queue
        self._cleanup = cleanup

    async def get(self, timeout: float) -> AnnouncementEvent:
        """获取下一个事件，超时抛出 asyncio.TimeoutError"""
        return await asyncio.wait_for(self._queue.get(), timeout=timeout)

    def close(self) -> None:
        self._cleanup()


class _TaskState:
    """单个公告任务的运行期状态"""

    def __init__(self, announcement_id: int, total: int) -> None:
        self.announcement_id = announcement_id
        self.total = total
        self.success_count = 0
        self.failure_count = 0
        self.lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.cancelled = False
        self.break_reason = ""

    @property
    def completed(self) -> int:
        return self.success_count + self.failure_count


class AnnouncementService:
    """公告任务管理器（提交、执行、取消、进度广播）"""

    def __init__(self) -> None:
        self._states: Dict[int, _TaskState] = {}
        self._tasks: Dict[int, "asyncio.Task[None]"] = {}
        self._subscribers: Dict[int, Set["asyncio.Queue[AnnouncementEvent]"]] = {}

    # ------------------------------------------------------------------
    # 范围解析
    # ------------------------------------------------------------------

    async def _load_active_groups(self) -> Dict[Tuple[str, str], List[DBChatChannel]]:
        """按 (adapter_key, instance_key) 归组全部活跃会话

        活跃会话指未被停用的频道（含旁观模式），与频道列表页的"停用"筛选保持一致。
        已停用/软删的实例整组排除；没有实例登记行的频道（单实例适配器与历史数据）保留，
        否则这类部署选不出任何目标。
        """
        disabled_instances = {
            (instance.adapter_key, instance.instance_key)
            for instance in await DBAdapterInstance.all()
            if not instance.enabled or instance.status == "deleted"
        }

        groups: Dict[Tuple[str, str], List[DBChatChannel]] = {}
        for channel in await DBChatChannel.filter(is_active=True).order_by("chat_key"):
            group_key = (channel.adapter_key, channel.instance_key)
            if group_key in disabled_instances:
                continue
            groups.setdefault(group_key, []).append(channel)
        return groups

    async def list_instance_options(self) -> List[AnnouncementInstanceOption]:
        """列出可作为发送范围的实例及其活跃会话数"""
        groups = await self._load_active_groups()
        instances = {
            (instance.adapter_key, instance.instance_key): instance for instance in await DBAdapterInstance.all()
        }

        options: List[AnnouncementInstanceOption] = []
        for (adapter_key, instance_key), channels in groups.items():
            instance = instances.get((adapter_key, instance_key))
            display_name = instance.display_name if instance and instance.display_name else ""
            if not display_name:
                display_name = f"{adapter_key}:{instance_key}" if instance_key else adapter_key
            options.append(
                AnnouncementInstanceOption(
                    adapter_key=adapter_key,
                    instance_key=instance_key,
                    display_name=display_name,
                    channel_count=len(channels),
                ),
            )
        options.sort(key=lambda option: (option.adapter_key, option.instance_key))
        return options

    async def resolve_targets(self, scope: AnnouncementScope) -> List[AnnouncementTarget]:
        """把发送范围展开为具体的投递目标列表"""
        if scope.scope_type == AnnouncementScopeType.CHANNELS:
            chat_keys = list(dict.fromkeys(chat_key.strip() for chat_key in scope.chat_keys if chat_key.strip()))
            channels = {channel.chat_key: channel for channel in await DBChatChannel.filter(chat_key__in=chat_keys)}
            return [self._to_target(channels[chat_key]) for chat_key in chat_keys if chat_key in channels]

        groups = await self._load_active_groups()
        if scope.scope_type == AnnouncementScopeType.INSTANCES:
            selected = {(ref.adapter_key, ref.instance_key) for ref in scope.instances}
            groups = {group_key: channels for group_key, channels in groups.items() if group_key in selected}

        targets: List[AnnouncementTarget] = []
        for group_key in sorted(groups):
            targets.extend(self._to_target(channel) for channel in groups[group_key])
        return targets

    async def build_preview(self, scope: AnnouncementScope) -> AnnouncementPreview:
        """构建提交前的影响范围预览（将发送到 N 个实例的 M 个会话）"""
        targets = await self.resolve_targets(scope)
        known_instances = {
            (option.adapter_key, option.instance_key): option for option in await self.list_instance_options()
        }

        counts: Dict[Tuple[str, str], int] = {}
        for target in targets:
            group_key = (target.adapter_key, target.instance_key)
            counts[group_key] = counts.get(group_key, 0) + 1

        breakdown: List[AnnouncementInstanceOption] = []
        for group_key in sorted(counts):
            adapter_key, instance_key = group_key
            known = known_instances.get(group_key)
            display_name = known.display_name if known else (f"{adapter_key}:{instance_key}" if instance_key else adapter_key)
            breakdown.append(
                AnnouncementInstanceOption(
                    adapter_key=adapter_key,
                    instance_key=instance_key,
                    display_name=display_name,
                    channel_count=counts[group_key],
                ),
            )

        max_channels = max(config.ANNOUNCEMENT_MAX_CHANNELS_PER_TASK, 1)
        return AnnouncementPreview(
            channel_count=len(targets),
            instance_count=len(breakdown),
            max_channels=max_channels,
            exceeds_limit=len(targets) > max_channels,
            instances=breakdown,
        )

    # ------------------------------------------------------------------
    # 任务提交与取消
    # ------------------------------------------------------------------

    async def submit(self, content: str, scope: AnnouncementScope, created_by: str) -> DBAnnouncement:
        """创建公告任务并在后台开始投递

        Returns:
            已落库的公告任务行，`total` 为解析后的目标会话数
        """
        targets = await self.resolve_targets(scope)
        announcement = await DBAnnouncement.create(
            content=content,
            scope_type=scope.scope_type.value,
            scope_value=self._dump_scope_value(scope),
            status=AnnouncementStatus.PENDING.value,
            total=len(targets),
            created_by=created_by,
        )

        state = _TaskState(announcement.id, len(targets))
        self._states[announcement.id] = state
        self._tasks[announcement.id] = asyncio.create_task(self._run(announcement.id, content, targets, state))
        logger.info(f"公告任务已提交: id={announcement.id}, 目标会话={len(targets)}, 提交人={created_by}")
        return announcement

    async def request_cancel(self, announcement_id: int) -> bool:
        """请求取消进行中的任务

        已经发出的消息不会回滚，只是不再继续投递剩余会话。

        Returns:
            True 表示已通知运行中的任务停止；False 表示该任务当前不在运行
        """
        state = self._states.get(announcement_id)
        if state is None or state.stop_event.is_set():
            return False
        state.cancelled = True
        state.stop_event.set()
        logger.info(f"公告任务收到取消请求: id={announcement_id}")
        return True

    async def recover_stale_tasks(self) -> int:
        """把上次运行遗留的非终态任务标记为失败

        后台任务只存在于进程内存中，进程重启后 pending/running 的任务不可能再被推进，
        不处理就会永远显示"进行中"。

        Returns:
            被标记为失败的任务数量
        """
        stale = await DBAnnouncement.filter(
            status__in=[AnnouncementStatus.PENDING.value, AnnouncementStatus.RUNNING.value],
        )
        for announcement in stale:
            announcement.status = AnnouncementStatus.FAILED.value
            announcement.failure_reason = RESTART_INTERRUPTED_REASON
            announcement.finish_time = db_now()
            await announcement.save(update_fields=["status", "failure_reason", "finish_time"])
            logger.warning(f"公告任务因服务重启被标记为失败: id={announcement.id}")
        return len(stale)

    # ------------------------------------------------------------------
    # 进度订阅
    # ------------------------------------------------------------------

    def subscribe(self, announcement_id: int) -> AnnouncementSubscription:
        """订阅指定公告任务的进度事件"""
        queue: "asyncio.Queue[AnnouncementEvent]" = asyncio.Queue()
        self._subscribers.setdefault(announcement_id, set()).add(queue)

        def cleanup() -> None:
            queues = self._subscribers.get(announcement_id)
            if queues is None:
                return
            queues.discard(queue)
            if not queues:
                self._subscribers.pop(announcement_id, None)

        return AnnouncementSubscription(queue, cleanup)

    def is_running(self, announcement_id: int) -> bool:
        return announcement_id in self._states

    def _publish(self, announcement_id: int, event: AnnouncementEvent) -> None:
        for queue in self._subscribers.get(announcement_id, set()):
            queue.put_nowait(event)

    # ------------------------------------------------------------------
    # 任务执行
    # ------------------------------------------------------------------

    async def _run(
        self,
        announcement_id: int,
        content: str,
        targets: List[AnnouncementTarget],
        state: _TaskState,
    ) -> None:
        """后台执行整个公告任务"""
        try:
            await self._update_status(announcement_id, AnnouncementStatus.RUNNING)

            groups: Dict[Tuple[str, str], List[AnnouncementTarget]] = {}
            for target in targets:
                groups.setdefault((target.adapter_key, target.instance_key), []).append(target)

            # 每个实例一个 worker：实例内串行保证风控节奏，实例之间并发压缩总耗时
            await asyncio.gather(
                *(self._run_instance_worker(content, group_targets, state) for group_targets in groups.values()),
            )

            final_status, failure_reason = self._resolve_final_status(state)
            await self._update_status(announcement_id, final_status, failure_reason=failure_reason)
        except asyncio.CancelledError:
            await self._update_status(
                announcement_id,
                AnnouncementStatus.CANCELLED,
                failure_reason="任务被中止",
            )
            raise
        except Exception as e:
            logger.error(f"公告任务执行异常: id={announcement_id}, error={e}")
            await self._update_status(announcement_id, AnnouncementStatus.FAILED, failure_reason=str(e))
        finally:
            self._states.pop(announcement_id, None)
            self._tasks.pop(announcement_id, None)

    async def _run_instance_worker(
        self,
        content: str,
        targets: List[AnnouncementTarget],
        state: _TaskState,
    ) -> None:
        """单个适配器实例内的串行投递"""
        min_interval = max(config.ANNOUNCEMENT_MIN_INTERVAL_SECONDS, 0.0)
        max_interval = max(config.ANNOUNCEMENT_MAX_INTERVAL_SECONDS, min_interval)

        for index, target in enumerate(targets):
            if state.stop_event.is_set():
                return
            if index > 0:
                try:
                    await asyncio.wait_for(state.stop_event.wait(), timeout=random.uniform(min_interval, max_interval))
                except asyncio.TimeoutError:
                    pass
                else:
                    return

            ok, error = await self._deliver(target, content)
            await DBAnnouncementDelivery.create(
                announcement_id=state.announcement_id,
                chat_key=target.chat_key,
                channel_name=target.channel_name,
                adapter_key=target.adapter_key,
                instance_key=target.instance_key,
                ok=ok,
                error=error,
            )

            async with state.lock:
                if ok:
                    state.success_count += 1
                else:
                    state.failure_count += 1
                await DBAnnouncement.filter(id=state.announcement_id).update(
                    success_count=state.success_count,
                    failure_count=state.failure_count,
                )
                self._publish(
                    state.announcement_id,
                    AnnouncementDeliveryEvent(
                        announcement_id=state.announcement_id,
                        chat_key=target.chat_key,
                        channel_name=target.channel_name,
                        instance_key=target.instance_key,
                        ok=ok,
                        error=error,
                        completed=state.completed,
                        total=state.total,
                        success_count=state.success_count,
                        failure_count=state.failure_count,
                    ),
                )
                self._check_failure_breaker(state)

    def _check_failure_breaker(self, state: _TaskState) -> None:
        """失败率超阈值时熔断整个任务（调用方需持有 state.lock）"""
        if state.stop_event.is_set() or state.completed < FAILURE_BREAK_MIN_SAMPLES:
            return
        ratio = max(min(config.ANNOUNCEMENT_FAILURE_BREAK_RATIO, 1.0), 0.0)
        failure_ratio = state.failure_count / state.completed
        if failure_ratio <= ratio:
            return
        state.break_reason = (
            f"失败率 {failure_ratio:.0%} 超过熔断阈值 {ratio:.0%}（已投递 {state.completed} 个会话），任务已中止"
        )
        state.stop_event.set()
        logger.warning(f"公告任务触发熔断: id={state.announcement_id}, {state.break_reason}")

    def _resolve_final_status(self, state: _TaskState) -> Tuple[AnnouncementStatus, str]:
        if state.cancelled:
            return AnnouncementStatus.CANCELLED, "任务被手动取消，已发送的消息不会撤回"
        if state.break_reason:
            return AnnouncementStatus.FAILED, state.break_reason
        if state.total > 0 and state.success_count == 0:
            return AnnouncementStatus.FAILED, "全部会话投递失败"
        return AnnouncementStatus.COMPLETED, ""

    async def _deliver(self, target: AnnouncementTarget, content: str) -> Tuple[bool, str]:
        """向单个会话投递公告

        公告以命令输出前缀发送且不记录聊天记录，因此不会进入 AI 上下文。
        """
        from nekro_agent.services.chat.universal_chat_service import universal_chat_service

        channel = await DBChatChannel.filter(chat_key=target.chat_key).first()
        if not channel:
            return False, "聊天频道不存在"

        try:
            adapter = get_adapter(channel.adapter_key)
        except AdapterUnavailableError as e:
            return False, str(e)
        if not adapter.supports_webui_send:
            return False, "当前适配器不支持从 WebUI 发送消息"

        effective_config = await config_resolver.get_effective_config(channel.chat_key)
        text = f"{effective_config.AI_COMMAND_OUTPUT_PREFIX}{content}"

        try:
            await universal_chat_service.send_agent_message(
                chat_key=channel.chat_key,
                messages=[AgentMessageSegment(type=AgentMessageSegmentType.TEXT, content=text)],
                adapter=adapter,
                record=False,
            )
        except Exception as e:
            logger.error(f"公告投递失败: chat_key={target.chat_key}, error={e}")
            return False, str(e)
        return True, ""

    async def _update_status(
        self,
        announcement_id: int,
        status: AnnouncementStatus,
        failure_reason: str = "",
    ) -> None:
        announcement = await DBAnnouncement.get_or_none(id=announcement_id)
        if announcement is None:
            return
        announcement.status = status.value
        announcement.failure_reason = failure_reason
        if status != AnnouncementStatus.RUNNING:
            announcement.finish_time = db_now()
        await announcement.save(update_fields=["status", "failure_reason", "finish_time"])

        self._publish(
            announcement_id,
            AnnouncementStatusEvent(
                announcement_id=announcement_id,
                status=status,
                total=announcement.total,
                success_count=announcement.success_count,
                failure_count=announcement.failure_count,
                failure_reason=failure_reason,
            ),
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def build_snapshot_event(self, announcement: DBAnnouncement) -> AnnouncementSnapshotEvent:
        return AnnouncementSnapshotEvent(announcement=self.to_summary(announcement))

    def to_summary(self, announcement: DBAnnouncement) -> AnnouncementSummary:
        return AnnouncementSummary(
            id=announcement.id,
            content=announcement.content,
            scope_type=AnnouncementScopeType(announcement.scope_type),
            scope_detail=self._load_scope_detail(announcement),
            status=AnnouncementStatus(announcement.status),
            total=announcement.total,
            success_count=announcement.success_count,
            failure_count=announcement.failure_count,
            failure_reason=announcement.failure_reason,
            created_by=announcement.created_by,
            create_time=announcement.create_time.strftime("%Y-%m-%d %H:%M:%S"),
            finish_time=announcement.finish_time.strftime("%Y-%m-%d %H:%M:%S") if announcement.finish_time else None,
        )

    async def build_detail(self, announcement: DBAnnouncement) -> AnnouncementDetail:
        deliveries = await DBAnnouncementDelivery.filter(announcement_id=announcement.id).order_by("id")
        summary = self.to_summary(announcement)
        return AnnouncementDetail(
            **summary.model_dump(),
            deliveries=[
                AnnouncementDeliveryItem(
                    chat_key=delivery.chat_key,
                    channel_name=delivery.channel_name,
                    adapter_key=delivery.adapter_key,
                    instance_key=delivery.instance_key,
                    ok=delivery.ok,
                    error=delivery.error,
                    send_time=delivery.send_time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                for delivery in deliveries
            ],
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _to_target(channel: DBChatChannel) -> AnnouncementTarget:
        return AnnouncementTarget(
            chat_key=channel.chat_key,
            channel_name=channel.get_custom_channel_name() or channel.channel_name or channel.chat_key,
            adapter_key=channel.adapter_key,
            instance_key=channel.instance_key,
        )

    @staticmethod
    def _dump_scope_value(scope: AnnouncementScope) -> str:
        if scope.scope_type == AnnouncementScopeType.CHANNELS:
            return json.dumps(scope.chat_keys, ensure_ascii=False)
        if scope.scope_type == AnnouncementScopeType.INSTANCES:
            return json.dumps([ref.model_dump() for ref in scope.instances], ensure_ascii=False)
        return "[]"

    @staticmethod
    def load_scope(announcement: DBAnnouncement) -> AnnouncementScope:
        """把留档的 `scope_value` 还原成结构化的发送范围（`_dump_scope_value` 的逆运算）

        判归属要的是结构化的实例/频道标识，而不是给人看的字符串，所以还原逻辑收在这里，
        由展示用的 `_load_scope_detail` 与调用方共用一份 —— 存储格式的知识只留在本服务内。
        坏行按"这一项不存在"跳过：一条脏数据不该让整个历史列表 500。
        """
        try:
            raw = json.loads(announcement.scope_value or "[]")
        except json.JSONDecodeError:
            raw = []
        if not isinstance(raw, list):
            raw = []

        chat_keys: List[str] = []
        instances: List[AnnouncementInstanceRef] = []
        for item in raw:
            if isinstance(item, str):
                chat_keys.append(item)
            elif isinstance(item, dict):
                try:
                    instances.append(AnnouncementInstanceRef.model_validate(item))
                except Exception:
                    logger.warning(f"公告 {announcement.id} 的范围取值含无法解析的实例项: {item!r}")
        return AnnouncementScope(
            scope_type=AnnouncementScopeType(announcement.scope_type),
            instances=instances,
            chat_keys=chat_keys,
        )

    @staticmethod
    def _load_scope_detail(announcement: DBAnnouncement) -> List[str]:
        """把留档的范围取值还原成可读标识，便于历史列表直接展示"""
        scope = AnnouncementService.load_scope(announcement)
        return list(scope.chat_keys) + [
            f"{ref.adapter_key}:{ref.instance_key}" if ref.instance_key else ref.adapter_key for ref in scope.instances
        ]


announcement_service = AnnouncementService()


__all__ = [
    "AnnouncementService",
    "AnnouncementSubscription",
    "announcement_service",
]
