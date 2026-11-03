"""实例健康状态监测与掉线告警

## 为什么需要主动探活

NoneBot driver 的 `on_bot_connect` / `on_bot_disconnect` 只在 WebSocket 真正建立/断开
时触发，且只对走 NoneBot 的适配器（OneBot V11）生效。现实中最难受的故障不是"断开"，
而是**假连接**：socket 还在、心跳还在，但协议端进程已经卡死或掉登录，消息发不出去。
这类故障不会产生任何断连事件，只能靠周期性地向协议端要一次自身信息来暴露。

因此健康状态由两路信号共同驱动：

```
连接事件 (on_bot_connect)   ──┐
                              ├──> 健康状态机 ──> DBAdapterInstance.status
主动探活 (probe_instance)    ──┘                  + 审计事件 + SSE 广播 + 告警
```

## 探活必须按实例定位连接

探活走 `BaseAdapter.probe_instance(instance_key)` 而不是 `get_self_info()`，因为它跑在
后台任务里、没有入站链路留下的实例上下文。曾经这里调的是 `get_self_info(chat_key=None)`，
在多账号适配器上两个方向都是错的，且都已实测：多个账号在线时全体探活失败、每轮都误发掉线
告警；恰好一个账号在线时，那一个连接会替**所有**实例回答"在线"，连协议端根本不存在的实例
也被判成在线。后者与出站消息串号是同一类错误，只是发生在监测面上。

## 状态机

参与监测的实例：`enabled=True` 且 `status` 不在 `UNMANAGED_STATUSES` 内。

| 输入信号                     | 目标状态  |
| ---------------------------- | --------- |
| `on_bot_connect`             | `online`  |
| 探活成功                     | `online`  |
| `on_bot_disconnect`          | `offline` |
| 探活超时 / 探活抛异常        | `offline` |
| 适配器未加载 / 加载失败      | `error`   |

`UNMANAGED_STATUSES` 里的状态由总后台的认领、启停、归档流程，或适配器自己的绑定/续期流程
独占，健康监测绝不改写它们：把一个 `unclaimed` 实例探活成 `online` 会让它从"待认领"列表里
消失，把 `login_required`（或 `wechat_ilink_multi` 的 `binding` / `session_expired`）
探活成 `offline` 会掩盖"容器在跑但需要扫码"、"会话过期需重新绑定"这些更精确的诊断。

`unbound`（从未绑定账号）也在里面，理由与上面同形但更硬：**它探活只有一种可能的结果**。
没有账号就没有连接（`OnebotV11Adapter.probe_instance` 在这种实例上直接抛
`BotInstanceOfflineError`），于是每一轮都把它重新计成掉线，到点发一条「实例掉线告警」——
而它从来没上线过，这条告警从第一次起就是错的，且永远不会自愈。存量里那批
「建了实例、还没绑账号」的空壳行由 `start()` 里的一次性改判摘进这个状态。

落库只在**状态真的变化**时发生（经 `adapter_instance_service.set_runtime_status()`，它一并写审计
事件与 SSE），否则每个探活周期都会产生一条 from==to 的噪声事件。

## 抖动抑制

离线不立即告警。首次观察到离线只记录 `offline_since`；此后每个探活周期重新计算持续时长，
直到 `>= INSTANCE_OFFLINE_ALERT_DELAY_SECONDS` 才发一次告警并置 `alerted`。
恢复在线时，若此前告警过则补发一条恢复通知，并清空 `offline_since` / `alerted`。

静默时段内不置 `alerted`，只跳过本次发送，因此静默窗口结束后仍会补发告警，
而不是永久吞掉。

## 告警策略按实例解析

`INSTANCE_OFFLINE_ALERT_DELAY_SECONDS` / `INSTANCE_ALERT_QUIET_HOURS` /
`INSTANCE_ALERT_WEBHOOK_URL` 三项都标了 `overridable`，因此**告警延迟、静默时段与投递目标
一律按被告警的那个实例解析**（实例 > 适配器 > 全局）。定制版客户可以给自己的实例配自己的
Webhook 与静默时段，不再只能等平台方发现。探活周期
（`INSTANCE_HEALTH_CHECK_INTERVAL_SECONDS`）刻意**不**可覆盖：全进程只有一个探活循环，
它不是按实例存在的东西。

未配任何覆盖时每一项都回落全局值，行为与引入覆盖之前逐字一致。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.schemas.console import InstanceStatus
from nekro_agent.services.adapter_instance_service import adapter_instance_service
from nekro_agent.services.notification import NotificationLevel, dispatch_notification, resolve_alert_config

logger = get_sub_logger("instance_health")

# 单次探活的最长等待时间。探活会真的向协议端发一次请求（见 `BaseAdapter.probe_instance`），
# 所以这个值是「多久没回话就算假连接」的判据，正常协议端远快于此。
#
# 它比 `MIN_CHECK_INTERVAL_SECONDS` 大，但轮次不会因此叠在一起：`_run_loop` 是
# 「sleep 一个周期 → await 整轮探活」的串行结构，卡住的一轮只会把下一轮往后推，
# 不会并发跑两轮。周期被配到下限时，实际间隔因此是周期 + 本轮最慢的那个实例。
PROBE_TIMEOUT_SECONDS: float = 15.0

# 探活周期下限，防止把配置写成 0 或负数后把探活变成忙循环
MIN_CHECK_INTERVAL_SECONDS: int = 5

# 由总后台或适配器自身的生命周期流程独占的状态，健康监测只读不写
UNMANAGED_STATUSES: frozenset[str] = frozenset(
    {
        InstanceStatus.UNCLAIMED.value,
        # 从未绑定账号的实例。没有账号就没有连接，探活对它**只有一种可能的结果**——
        # 失败，于是每一轮都把它重新计成"掉线"，到点发一条「实例掉线告警」。
        # 而它从来没上线过，这条告警从第一次起就是错的，且永远不会自愈。
        # 正确的下一步是去认领一个已连入的协议端或手工绑定账号，那是总后台的动作，
        # 与 login_required（去扫码）同形，所以放在同一个豁免集合里。
        InstanceStatus.UNBOUND.value,
        InstanceStatus.LOGIN_REQUIRED.value,
        InstanceStatus.DISABLED.value,
        InstanceStatus.ARCHIVED.value,
        "deleted",
        # 以下三个不在 InstanceStatus 里，是 `wechat_ilink_multi` 的绑定/续期流程写入的
        # 私有生命周期态（`bot_manager` / `bot_connection`）。它们与 `login_required` 同形：
        # 都表示"连接本来就不该在"，且都带着比 online/offline 更精确的下一步动作
        # （去扫码 / 去重新绑定）。不排除的话，掉线监测每个周期都会把它们改写掉——
        # 覆写方向取决于探活实现，但无论改写成 online 还是 offline，那条诊断都没了。
        "binding",
        "session_expired",
        "renewing",
    },
)

_QUIET_HOURS_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


class InstanceHealthSnapshot(BaseModel):
    """单个实例的健康快照"""

    adapter_key: str = Field(..., description="适配器标识")
    instance_key: str = Field(..., description="实例标识")
    display_name: str = Field(default="", description="实例显示名称")
    status: str = Field(..., description="当前健康状态")
    enabled: bool = Field(default=True, description="是否启用")
    monitored: bool = Field(default=True, description="是否纳入健康监测")
    last_error: str = Field(default="", description="最近错误信息")
    offline_since: Optional[str] = Field(default=None, description="掉线起始时间（ISO 8601）")
    offline_seconds: int = Field(default=0, description="已持续掉线时长（秒），在线时为 0")
    alerted: bool = Field(default=False, description="本次掉线是否已发出告警")
    last_check_at: Optional[str] = Field(default=None, description="最近一次探活时间（ISO 8601）")
    last_probe_ok: bool = Field(default=False, description="最近一次探活是否成功")
    consecutive_failures: int = Field(default=0, description="连续探活失败次数")
    alert_delay_seconds: int = Field(default=0, description="该实例上生效的掉线告警延迟（秒），可被实例层覆盖")


class InstanceHealthListResponse(BaseModel):
    """实例健康状态列表"""

    items: List[InstanceHealthSnapshot] = Field(default_factory=list)
    total: int = Field(default=0)
    check_interval_seconds: int = Field(default=0, description="当前探活周期（秒）")
    alert_delay_seconds: int = Field(default=0, description="当前掉线告警延迟（秒）")


class InstanceHealthEventItem(BaseModel):
    """一条实例状态变更流水"""

    id: int = Field(..., description="事件 ID")
    adapter_key: str = Field(default="", description="适配器标识")
    instance_key: str = Field(default="", description="实例标识")
    display_name: str = Field(default="", description="实例显示名称")
    event_type: str = Field(default="", description="事件类型")
    status_from: str = Field(default="", description="变更前状态")
    status_to: str = Field(default="", description="变更后状态")
    message: str = Field(default="", description="事件说明")
    create_time: str = Field(default="", description="事件时间（ISO 8601）")


class InstanceHealthEventListResponse(BaseModel):
    """实例状态变更流水分页结果"""

    items: List[InstanceHealthEventItem] = Field(default_factory=list)
    total: int = Field(default=0)
    page: int = Field(default=1)
    page_size: int = Field(default=20)


@dataclass
class _HealthState:
    """实例的进程内健康状态

    刻意不落库：这些是易失的观测量，重启后重新观测即可，落库只会在重启时留下
    误导性的"掉线了 3 天"。掉线时长的权威事实来自审计事件流水。
    """

    offline_since: Optional[datetime] = None
    alerted: bool = False
    last_check_at: Optional[datetime] = None
    last_probe_ok: bool = False
    consecutive_failures: int = 0


class InstanceHealthService:
    """实例健康监测服务"""

    def __init__(self) -> None:
        self._states: Dict[Tuple[str, str], _HealthState] = {}
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._reconcile_task: Optional[asyncio.Task[None]] = None
        self._audit_task: Optional[asyncio.Task[None]] = None
        self._running: bool = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动后台探活循环

        起循环**之前**先把存量里「从未绑定账号却挂着 offline」的实例改判为 `unbound`。
        顺序不能反：那批行正是升级前建的空壳实例，不先摘出来，第一轮探活就会把它们当成
        掉线的实例开始计时，到点发一批从第一次起就是错的「实例掉线告警」。

        改判本身是纯状态语义的存量修复，判据与读侧 `resolve_instance_status()` 完全一致，
        因此不会改变界面上已经显示的任何内容，只是把结论落进库里让探活也看得见。
        失败不阻断监测启动：探活对这批实例只是多发几条误告警，而完全不监测是更坏的结果。
        """
        if self._running:
            return

        # 延迟导入：`instance_manager` 会拖进 aiodocker / qdrant，模块级导入把它们钉进
        # 健康监测的导入链，与本文件其余部分的延迟导入取舍一致
        try:
            from nekro_agent.services.console.instance_manager import normalize_unbound_instance_status

            await normalize_unbound_instance_status()
        except Exception as e:
            logger.warning(f"未绑定账号实例的状态改判失败（非致命，这些实例可能仍会误报掉线）: {e}")

        self._running = True
        self._audit_task = asyncio.create_task(self._audit_restart_policies())
        self._loop_task = asyncio.create_task(self._run_loop())
        logger.info(f"实例健康监测已启动，探活周期 {self._check_interval_seconds()}s")

    async def stop(self) -> None:
        """停止后台探活循环，确保不留悬挂任务"""
        self._running = False
        for background in (self._reconcile_task, self._audit_task):
            if background is not None and not background.done():
                background.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await background
        self._reconcile_task = None
        self._audit_task = None
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._states.clear()
        logger.info("实例健康监测已停止")

    # ------------------------------------------------------------------
    # 外部信号入口
    # ------------------------------------------------------------------

    async def report_connect(self, adapter_key: str, instance_key: str, message: str = "") -> None:
        """上报实例连接事件

        Args:
            adapter_key: 适配器标识
            instance_key: 实例标识
            message: 状态变更说明
        """
        await self._report(adapter_key, instance_key, InstanceStatus.ONLINE.value, message or "协议端已连接")

    async def report_disconnect(self, adapter_key: str, instance_key: str, message: str = "") -> None:
        """上报实例断连事件

        Args:
            adapter_key: 适配器标识
            instance_key: 实例标识
            message: 状态变更说明
        """
        await self._report(adapter_key, instance_key, InstanceStatus.OFFLINE.value, message or "协议端已断开")

    async def _report(self, adapter_key: str, instance_key: str, target_status: str, message: str) -> None:
        instance = await adapter_instance_service.get_instance(adapter_key, instance_key)
        if instance is None:
            logger.warning(f"上报健康状态失败，实例不存在: adapter={adapter_key}, instance={instance_key}")
            return
        # 判据必须与 `_probe_all` 的过滤条件逐字对应（`enabled=True` 且状态不在 UNMANAGED 内）。
        # 只看状态的话，`enabled=False` 但状态仍是运行态的脏行（升级前的存量、只改了一列的手工修补）
        # 会走两套判据：探活跳过它，连接事件却把它改写成 online —— 总后台上一个"已停用"的实例
        # 因为协议端自己重连就显示成在线，与停用闸门给出的事实相反。
        if not instance.enabled or instance.status in UNMANAGED_STATUSES:
            logger.debug(
                f"实例不在健康监测范围内，不改写状态: adapter={adapter_key}, "
                f"instance={instance_key}, enabled={instance.enabled}, status={instance.status}",
            )
            return
        await self._apply_status(instance, target_status, message)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def list_health(self, scope_tenant_id: Optional[int] = None) -> InstanceHealthListResponse:
        """获取实例的健康快照

        Args:
            scope_tenant_id: 结果限定在这个租户名下。**None 表示不受限**（平台方）。

        Returns:
            InstanceHealthListResponse: 健康状态列表与当前告警策略参数

        ## 作用域按 `tenant_id` 而不是按 `instance_key` 清单

        `instance_key` 的唯一性只到 `unique_together = (adapter_key, instance_key)`
        （见 `models/db_adapter_instance.py`），`instance_key__in=[租户名下的标识]` 过滤的是
        **列的取值**：同宿主上另一个适配器下同名的那一行会一并命中，于是租户看到别人的实例。
        归属列本身就在这张表上，直接按它过滤既无歧义、也与 `assert_instance_accessible`
        的判据（`instance.tenant_id != scope`）逐字同源。
        """
        query = DBAdapterInstance.filter(enabled=True).exclude(status="deleted")
        if scope_tenant_id is not None:
            query = query.filter(tenant_id=scope_tenant_id)
        instances = await query.order_by("id")
        now = datetime.now()

        items: List[InstanceHealthSnapshot] = []
        for instance in instances:
            state = self._states.get((instance.adapter_key, instance.instance_key), _HealthState())
            offline_seconds = int((now - state.offline_since).total_seconds()) if state.offline_since else 0
            items.append(
                InstanceHealthSnapshot(
                    adapter_key=instance.adapter_key,
                    instance_key=instance.instance_key,
                    display_name=instance.display_name,
                    status=instance.status,
                    enabled=instance.enabled,
                    monitored=instance.status not in UNMANAGED_STATUSES,
                    last_error=instance.last_error,
                    offline_since=state.offline_since.isoformat() if state.offline_since else None,
                    offline_seconds=max(0, offline_seconds),
                    alerted=state.alerted,
                    last_check_at=state.last_check_at.isoformat() if state.last_check_at else None,
                    last_probe_ok=state.last_probe_ok,
                    consecutive_failures=state.consecutive_failures,
                    alert_delay_seconds=self._alert_delay_seconds(instance),
                ),
            )

        return InstanceHealthListResponse(
            items=items,
            total=len(items),
            check_interval_seconds=self._check_interval_seconds(),
            alert_delay_seconds=max(0, config.INSTANCE_OFFLINE_ALERT_DELAY_SECONDS),
        )

    async def list_events(
        self,
        page: int = 1,
        page_size: int = 20,
        adapter_key: str = "",
        instance_key: str = "",
        scope_tenant_id: Optional[int] = None,
    ) -> InstanceHealthEventListResponse:
        """分页获取实例状态变更流水

        Args:
            page: 页码，从 1 开始
            page_size: 每页条数
            adapter_key: 可选，按适配器筛选
            instance_key: 可选，按实例筛选（需与 adapter_key 同时提供才精确）
            scope_tenant_id: 结果限定在这个租户名下，语义同 `list_health`

        Returns:
            InstanceHealthEventListResponse: 分页流水
        """
        instance_filter: Dict[str, Any] = {}
        if adapter_key:
            instance_filter["adapter_key"] = adapter_key
        if instance_key:
            instance_filter["instance_key"] = instance_key
        if scope_tenant_id is not None:
            # 作用域与调用方给的筛选条件是**与**关系：筛选参数只能收窄，不能扩宽。
            # 拼在同一个 filter 里而不是分两步，归属列与筛选条件由 ORM 一起下推，
            # 租户填别人的实例标识时交集为空。
            #
            # 按 `tenant_id` 而不是按 `instance_key__in`，理由见 `list_health`：
            # 后者过滤的是**列的取值**，跨适配器同名的那一行会连带命中。
            instance_filter["tenant_id"] = scope_tenant_id

        query = DBAdapterInstanceEvent.all()
        if instance_filter:
            matched_ids = await DBAdapterInstance.filter(**instance_filter).values_list("id", flat=True)
            if not matched_ids:
                return InstanceHealthEventListResponse(items=[], total=0, page=page, page_size=page_size)
            query = query.filter(instance_id__in=list(matched_ids))

        total = await query.count()
        events = await query.order_by("-id").offset((page - 1) * page_size).limit(page_size)

        instance_ids = {event.instance_id for event in events}
        instances = await DBAdapterInstance.filter(id__in=list(instance_ids)) if instance_ids else []
        instance_map = {instance.id: instance for instance in instances}

        items: List[InstanceHealthEventItem] = []
        for event in events:
            instance = instance_map.get(event.instance_id)
            items.append(
                InstanceHealthEventItem(
                    id=event.id,
                    adapter_key=instance.adapter_key if instance else "",
                    instance_key=instance.instance_key if instance else "",
                    display_name=instance.display_name if instance else "",
                    event_type=event.event_type,
                    status_from=event.status_from,
                    status_to=event.status_to,
                    message=event.message,
                    create_time=event.create_time.isoformat() if event.create_time else "",
                ),
            )

        return InstanceHealthEventListResponse(items=items, total=total, page=page, page_size=page_size)

    # ------------------------------------------------------------------
    # 探活循环
    # ------------------------------------------------------------------

    def _check_interval_seconds(self) -> int:
        return max(MIN_CHECK_INTERVAL_SECONDS, config.INSTANCE_HEALTH_CHECK_INTERVAL_SECONDS)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._check_interval_seconds())
                if not self._running:
                    return
                await self._probe_all()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 单轮失败不能终结循环，否则一次偶发的数据库抖动就让监测永久失效
                logger.exception(f"实例探活轮次异常: {e}")

    async def _reconcile_containers(self) -> None:
        """走 docker-guard 的部署：把「应当在跑却已退出」的内置 NapCat 容器经 guard 重新拉起。

        带 Binds 的容器在 guard 下不能交给 Docker 守护进程自动重启（见 `container_manager.napcat_restart_policy`），
        保活于是落到 NA 手里。不走 guard 时 `reconcile_container_liveness` 第一行就返回，标准部署不受影响。
        任何失败都只记日志。**本方法在独立任务里跑**（见 `_spawn_reconcile`），不在探活的关键路径上。
        """
        try:
            from nekro_agent.services.console.container_manager import reconcile_container_liveness

            await reconcile_container_liveness()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"容器保活检查失败（不影响探活）: {e}")

    def _spawn_reconcile(self) -> None:
        """每个探活周期起一次保活任务；上一轮还没结束就不叠加。

        **不能 await 在探活前面**：保活要对 Docker 发 `start`，守护进程挂起时这个调用会一直不返回，
        串在前面的话全部实例的探活都停摆（本 PR 引入过的回归）。单个实例的时限由 reconcile 自己按实例施加。
        """
        task = self._reconcile_task
        if task is not None and not task.done():
            return
        self._reconcile_task = asyncio.create_task(self._reconcile_containers())

    async def _audit_restart_policies(self) -> None:
        """启动阶段扫一次本部署的内置 NapCat 容器的 RestartPolicy（迁移 / 回滚告警），见 `audit_napcat_restart_policies`。"""
        try:
            from nekro_agent.services.console.container_manager import audit_napcat_restart_policies

            await audit_napcat_restart_policies()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"NapCat 容器 RestartPolicy 扫描失败（不影响启动）: {e}")

    async def _probe_all(self) -> None:
        self._spawn_reconcile()
        instances = await DBAdapterInstance.filter(enabled=True).exclude(status__in=list(UNMANAGED_STATUSES))
        if not instances:
            return

        results = await asyncio.gather(
            *(self._probe_instance(instance) for instance in instances),
            return_exceptions=True,
        )
        for instance, result in zip(instances, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    f"实例探活处理异常: adapter={instance.adapter_key}, "
                    f"instance={instance.instance_key}, error={result}",
                )

    async def _probe_instance(self, instance: DBAdapterInstance) -> None:
        """对单个实例做一次轻量探测

        任何失败都只影响该实例自身的状态，不向上传播。
        """
        # 延迟导入：适配器注册表在应用启动期才装配完成，模块级导入会引入启动顺序耦合
        from nekro_agent.adapters import get_adapter
        from nekro_agent.schemas.errors import AdapterUnavailableError

        state = self._states.setdefault((instance.adapter_key, instance.instance_key), _HealthState())
        state.last_check_at = datetime.now()

        try:
            adapter = get_adapter(instance.adapter_key)
        except AdapterUnavailableError as e:
            await self._apply_status(instance, InstanceStatus.ERROR.value, f"适配器不可用: {e}")
            return

        try:
            await asyncio.wait_for(adapter.probe_instance(instance.instance_key), timeout=PROBE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await self._apply_status(
                instance,
                InstanceStatus.OFFLINE.value,
                f"探活超时（超过 {PROBE_TIMEOUT_SECONDS:.0f}s 未响应）",
            )
            return
        except Exception as e:
            await self._apply_status(instance, InstanceStatus.OFFLINE.value, f"探活失败: {e}")
            return

        await self._apply_status(instance, InstanceStatus.ONLINE.value, "探活成功")

    # ------------------------------------------------------------------
    # 状态机与告警
    # ------------------------------------------------------------------

    async def _apply_status(self, instance: DBAdapterInstance, target_status: str, message: str) -> None:
        state = self._states.setdefault((instance.adapter_key, instance.instance_key), _HealthState())
        previous_status = instance.status
        now = datetime.now()
        label = instance.display_name or f"{instance.adapter_key}/{instance.instance_key}"

        if target_status == InstanceStatus.ONLINE.value:
            was_alerted = state.alerted
            offline_since = state.offline_since
            state.last_probe_ok = True
            state.consecutive_failures = 0
            state.offline_since = None
            state.alerted = False

            if previous_status != target_status:
                await adapter_instance_service.set_runtime_status(instance, target_status, message=message)

            if was_alerted:
                downtime = int((now - offline_since).total_seconds()) if offline_since else 0
                await self._notify(
                    instance,
                    title=f"实例已恢复在线: {label}",
                    body=(
                        f"实例: {label}\n"
                        f"适配器: {instance.adapter_key}\n"
                        f"实例标识: {instance.instance_key}\n"
                        f"本次掉线持续: {self._format_duration(downtime)}\n"
                        f"恢复时间: {now.isoformat(timespec='seconds')}"
                    ),
                    level="info",
                )
            return

        state.last_probe_ok = False
        state.consecutive_failures += 1
        if state.offline_since is None:
            state.offline_since = now

        if previous_status != target_status:
            await adapter_instance_service.set_runtime_status(instance, target_status, message=message)

        if state.alerted:
            return

        offline_seconds = int((now - state.offline_since).total_seconds())
        if offline_seconds < self._alert_delay_seconds(instance):
            logger.debug(f"实例离线未达告警阈值，暂不告警: {label}, 已离线 {offline_seconds}s")
            return

        if self._in_quiet_hours(now, instance):
            # 不置 alerted：静默期结束后仍需补发，否则整夜的故障会被永久吞掉
            logger.debug(f"处于告警静默时段，跳过本次告警: {label}")
            return

        state.alerted = True
        await self._notify(
            instance,
            title=f"实例掉线告警: {label}",
            body=(
                f"实例: {label}\n"
                f"适配器: {instance.adapter_key}\n"
                f"实例标识: {instance.instance_key}\n"
                f"当前状态: {target_status}\n"
                f"已持续掉线: {self._format_duration(offline_seconds)}\n"
                f"掉线起始: {state.offline_since.isoformat(timespec='seconds')}\n"
                f"最近错误: {message}"
            ),
            level="error" if target_status == InstanceStatus.ERROR.value else "warning",
        )

    async def _notify(self, instance: DBAdapterInstance, title: str, body: str, level: NotificationLevel) -> None:
        """按**被告警的那个实例**解析投递通道

        不传实例的话，客户 A 的 Bot 掉线只会发到平台方那一个收件端 —— 那正是 B7 要消灭的形态。
        """
        try:
            outcome = await dispatch_notification(
                title=title,
                body=body,
                level=level,
                adapter_key=instance.adapter_key,
                instance_key=instance.instance_key,
            )
        except Exception as e:
            logger.warning(f"发送实例健康通知失败: {title}, error={e}")
            return
        logger.info(f"实例健康通知已投递: {title}, 结果={outcome}")

    def _alert_delay_seconds(self, instance: DBAdapterInstance) -> int:
        """该实例上生效的掉线告警延迟（秒）"""
        return max(0, resolve_alert_config(instance.adapter_key, instance.instance_key).INSTANCE_OFFLINE_ALERT_DELAY_SECONDS)

    def _in_quiet_hours(self, now: datetime, instance: Optional[DBAdapterInstance] = None) -> bool:
        """判断当前是否处于告警静默时段

        Args:
            now: 当前时间
            instance: 被告警的实例；给出时按该实例解析静默时段，否则用全局值

        Returns:
            bool: 处于静默时段返回 True；未配置或配置非法返回 False
        """
        scoped = resolve_alert_config(instance.adapter_key, instance.instance_key) if instance is not None else config
        raw = scoped.INSTANCE_ALERT_QUIET_HOURS.strip()
        if not raw:
            return False

        matched = _QUIET_HOURS_PATTERN.match(raw)
        if matched is None:
            logger.warning(f"告警静默时段配置格式非法，已忽略: {raw!r}（期望形如 23:00-07:00）")
            return False

        start_hour, start_minute, end_hour, end_minute = (int(part) for part in matched.groups())
        if start_hour > 23 or end_hour > 23 or start_minute > 59 or end_minute > 59:
            logger.warning(f"告警静默时段配置超出取值范围，已忽略: {raw!r}")
            return False

        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        current = now.hour * 60 + now.minute

        if start == end:
            return False
        if start < end:
            return start <= current < end
        # 跨零点，如 23:00-07:00
        return current >= start or current < end

    def _format_duration(self, seconds: int) -> str:
        seconds = max(0, seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}小时{minutes}分{secs}秒"
        if minutes:
            return f"{minutes}分{secs}秒"
        return f"{secs}秒"


# 全局实例健康监测服务
instance_health_service = InstanceHealthService()
