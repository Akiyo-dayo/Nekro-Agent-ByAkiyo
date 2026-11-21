"""频道每日额度的告警

## 谁收到哪一份

- **平台方**：`dispatch_notification` 的全局渠道（平台方邮箱、平台方 Webhook、运维频道），
  收全部频道的告警，正文里带租户名——平台方要知道是哪个客户。
- **租户**：频道所在实例归属某个租户时，另按实例发一份，走 `scoped_only=True`
  （只到该实例自己那层配的 Webhook）。租户没配就等于不发。正文里不带租户名，
  也不该带——那是他自己的频道。

## 当天去重靠一次条件更新

同一个阈值一天只告警一次。判据不能是「先读 `alerted_level` 再比」，两条消息同时跨过
同一个阈值时两边都会读到旧值、都判成「该告警」。改成
`UPDATE ... SET alerted_level = t WHERE id = ? AND alerted_level < t`，
**影响行数为 1 的那一路**才入队，另一路自然拿不到。

## 合并发送

零点前后大量频道同时用尽时，一条一条发会把运维群刷成瀑布。同一个目的地 10 分钟内
的告警合成一条，按频道去重（同一频道在窗口里跨过两个阈值时只留最后那条状态）。
积压只在进程内存里：重启丢掉未发出的告警是可以接受的——它们说的是「今天这个频道
到过 80%」，而不是「有一笔钱需要退」。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily

from .policy import UNIT_REPLIES, alert_percents

if TYPE_CHECKING:
    from nekro_agent.models.db_chat_channel import DBChatChannel

    from .service import QuotaStatus

logger = get_sub_logger("channel_quota")

# 同一个目的地的两次发送之间至少隔这么久（秒）
FLUSH_INTERVAL_SECONDS = 600.0
# 后台刷出循环的醒来看间隔（秒）
FLUSH_TICK_SECONDS = 30.0

# 目的地：("platform", "", "") 表示平台方的全局渠道；("tenant", 适配器, 实例) 表示某个实例的主人
Destination = Tuple[str, str, str]
PLATFORM_DESTINATION: Destination = ("platform", "", "")


@dataclass(frozen=True)
class ChannelQuotaAlert:
    """一条待发出的频道额度告警（一个频道在某个阈值上的一条）

    刻意**不带任何聊天内容**：这条告警会经由邮件与 Webhook 离开本进程，
    而群里的消息属于群友。带上频道名与会话标识已经足够运维定位。
    """

    chat_key: str
    channel_name: str
    adapter_key: str
    instance_key: str
    tenant_name: str
    unit: str
    used: int
    limit: int
    percent: int
    paused: bool


def _now() -> float:
    """当前时刻（秒）。单列一个函数是为了让用例能注入时间"""
    return time.monotonic()


def _unit_label(unit: str) -> str:
    return "回复次数" if unit == UNIT_REPLIES else "token"


def _alert_line(alert: ChannelQuotaAlert, *, with_tenant: bool) -> str:
    """一个频道一行：频道名与会话标识、实例、（租户名）、单位、用量 / 额度、动作"""
    name = alert.channel_name or alert.chat_key
    instance = f"{alert.adapter_key}/{alert.instance_key}" if alert.adapter_key or alert.instance_key else "未知实例"
    parts = [f"{name}（{alert.chat_key}）", f"实例 {instance}"]
    if with_tenant:
        parts.append(f"租户 {alert.tenant_name or '平台方'}")
    parts.append(f"{_unit_label(alert.unit)} {alert.used}/{alert.limit}（{alert.percent}%）")
    parts.append("已暂停" if alert.paused else "已告警")
    return " | ".join(parts)


def _render_body(alerts: List[ChannelQuotaAlert], *, with_tenant: bool) -> Tuple[str, str]:
    """把一批告警渲染成 (标题, 正文)"""
    channels: Dict[str, ChannelQuotaAlert] = {}
    for alert in alerts:
        # 同一频道在窗口里跨过两个阈值时只留最后那条状态
        channels[alert.chat_key] = alert
    lines = [_alert_line(alert, with_tenant=with_tenant) for alert in channels.values()]
    title = f"频道额度告警：{len(lines)} 个频道"
    return title, "\n".join(lines)


class ChannelQuotaAlertBuffer:
    """进程内的告警缓冲与合并发送

    积压按目的地分开攒；每个目的地记一个「上次发送时刻」，到点才发。
    """

    def __init__(self) -> None:
        self._pending: Dict[Destination, List[ChannelQuotaAlert]] = {}
        self._last_sent: Dict[Destination, float] = {}
        self._task: Optional["asyncio.Task[None]"] = None
        self._running = False

    # ==== 供用例与业务侧使用 ====

    def enqueue(self, alert: ChannelQuotaAlert, destinations: Optional[List[Destination]] = None) -> None:
        """把一条告警放进一个或多个目的地的积压"""
        for destination in destinations or [PLATFORM_DESTINATION]:
            self._pending.setdefault(destination, []).append(alert)

    def pending_count(self, destination: Optional[Destination] = None) -> int:
        if destination is None:
            return sum(len(items) for items in self._pending.values())
        return len(self._pending.get(destination, []))

    def reset(self) -> None:
        """清空积压与发送时刻（**仅供测试**）"""
        self._pending.clear()
        self._last_sent.clear()

    # ==== 刷出 ====

    async def flush_due(self, *, now: Optional[float] = None, force: bool = False) -> int:
        """把到点的积压发出去，返回本次发出的消息条数

        `force=True` 忽略 10 分钟间隔（关停时用：剩下的积压发一次就结束）。
        """
        moment = _now() if now is None else now
        sent = 0
        for destination in list(self._pending):
            items = self._pending.get(destination) or []
            if not items:
                continue
            last = self._last_sent.get(destination)
            if not force and last is not None and moment - last < FLUSH_INTERVAL_SECONDS:
                continue
            self._pending[destination] = []
            self._last_sent[destination] = moment
            await self._send(destination, items)
            sent += 1
        return sent

    async def _send(self, destination: Destination, items: List[ChannelQuotaAlert]) -> None:
        """真正投递一条合并后的告警；失败只记 warning（下一条告警还会再来）"""
        from nekro_agent.services.notification.channels import dispatch_notification

        kind, adapter_key, instance_key = destination
        with_tenant = kind == "platform"
        title, body = _render_body(items, with_tenant=with_tenant)
        try:
            if with_tenant:
                await dispatch_notification(title, body, "warning")
            else:
                await dispatch_notification(
                    title,
                    body,
                    "warning",
                    adapter_key=adapter_key,
                    instance_key=instance_key,
                    scoped_only=True,
                )
        except Exception as exc:  # noqa: BLE001 - 告警是观测，不能反过来打掉业务
            logger.warning(f"频道额度告警投递失败（已忽略）: destination={destination} | {exc}")

    # ==== 后台循环 ====

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """停掉后台循环，并把剩下的积压发一次"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        try:
            await self.flush_due(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"关停时刷出频道额度告警失败（已忽略）: {exc}")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.flush_due()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"频道额度告警刷出循环出错（已忽略）: {exc}")
            await asyncio.sleep(FLUSH_TICK_SECONDS)


alert_buffer = ChannelQuotaAlertBuffer()


async def flush_alerts_now() -> int:
    """把到点的积压发出去（用例直接调这个，生产侧由后台循环调）"""
    return await alert_buffer.flush_due()


async def _tenant_of(adapter_key: str, instance_key: str) -> Tuple[Optional[int], str]:
    """查实例的租户归属，返回 (租户 ID, 租户名)"""
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance
    from nekro_agent.models.db_tenant import DBTenant

    instance = await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).order_by("id").first()
    if instance is None or instance.tenant_id is None:
        return None, ""
    tenant = await DBTenant.get_or_none(id=instance.tenant_id)
    name = (tenant.display_name or tenant.name) if tenant is not None else ""
    return instance.tenant_id, name


async def maybe_enqueue(channel: "DBChatChannel", status: "QuotaStatus") -> bool:
    """按状态做一次告警评估；真的入队了返回 True

    只在一个阈值**首次**被跨过时入队：条件更新的影响行数为 1 的那一路才继续。
    整个函数不抛异常——它跑在消息处理与回合收尾的路径上，那里任何漏出来的异常都会
    变成「群友发了一句话，Bot 报了一个错」。
    """
    try:
        return await _maybe_enqueue(channel, status)
    except Exception as exc:  # noqa: BLE001 - 告警是观测，失败只记 warning
        logger.warning(f"频道额度告警评估失败（已忽略）: chat_key={channel.chat_key} | {exc}")
        return False


async def _maybe_enqueue(channel: "DBChatChannel", status: "QuotaStatus") -> bool:
    """`maybe_enqueue` 的正文（异常由调用方那一层兜住）"""
    from .service import STATE_DISABLED, STATE_PAUSED, STATE_UNLIMITED

    if status.state in (STATE_DISABLED, STATE_UNLIMITED):
        return False
    daily: Optional[DBChannelQuotaDaily] = status.daily
    if daily is None:
        return False

    eligible = [percent for percent in alert_percents() if percent <= status.percent]
    if not eligible:
        return False
    level = max(eligible)
    affected = await DBChannelQuotaDaily.filter(id=daily.id, alerted_level__lt=level).update(alerted_level=level)
    if affected != 1:
        # 并发的另一路已经发过这一档；这里静默，避免同一阈值重复入队
        return False

    tenant_id, tenant_name = await _tenant_of(channel.adapter_key, channel.instance_key)
    alert = ChannelQuotaAlert(
        chat_key=channel.chat_key,
        channel_name=channel.channel_name or "",
        adapter_key=channel.adapter_key,
        instance_key=channel.instance_key,
        tenant_name=tenant_name,
        unit=status.unit,
        used=status.used,
        limit=status.allowed,
        percent=status.percent,
        paused=status.state == STATE_PAUSED,
    )
    destinations: List[Destination] = [PLATFORM_DESTINATION]
    if tenant_id is not None:
        destinations.append(("tenant", channel.adapter_key, channel.instance_key))
    alert_buffer.enqueue(alert, destinations)
    return True


__all__ = [
    "FLUSH_INTERVAL_SECONDS",
    "FLUSH_TICK_SECONDS",
    "PLATFORM_DESTINATION",
    "ChannelQuotaAlert",
    "ChannelQuotaAlertBuffer",
    "alert_buffer",
    "flush_alerts_now",
    "maybe_enqueue",
]
