"""SSH 审计的保留期清理

审计要能回溯相当长一段时间，又不能无限长。这里照上传图片保留期的挂法：进程启动后
先等一会儿跑一次，之后每天一次；循环每次醒来先看配置，运行中改成 0 也能在一个 tick
内停下，不用重启。

只按 `create_time` 删，不碰凭据、不碰网关状态。值小于等于 0 表示不清理；删了几行
写一条 INFO，不逐行打印——审计行数可能很多，逐行日志只会把有用信息淹没。
"""

from __future__ import annotations

import asyncio
import datetime
import time
from typing import Optional

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit

logger = get_sub_logger("ssh_gateway.audit")

STARTUP_DELAY_SECONDS = 300.0
RUN_INTERVAL_SECONDS = 86400.0
TICK_SECONDS = 600.0
SECONDS_PER_DAY = 86400


def current_retention_days() -> int:
    """当前保留天数；0、负数或写坏的值都按不清理处理"""
    try:
        days = int(config.SSH_GATEWAY_AUDIT_RETENTION_DAYS)
    except (TypeError, ValueError):
        return 0
    return max(days, 0)


async def purge_once(now: Optional[datetime.datetime] = None) -> int:
    """按当前配置删一轮，返回删除的行数；不清理时返回 0"""
    days = current_retention_days()
    if days <= 0:
        return 0
    current = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = current - datetime.timedelta(days=days)
    deleted = await DBWsSshAudit.filter(create_time__lt=cutoff).delete()
    count = int(deleted or 0)
    logger.info(f"SSH 审计保留期清理完成（保留 {days} 天）：删除 {count} 行")
    return count


class SshAuditRetention:
    """SSH 审计保留期的后台循环

    进程内记「上次跑的时间」（单调时钟）；重启后为空，所以启动延迟过后会先跑一次。
    跑出异常也等到下一个周期：能漏出循环的异常是数据库暂时不可用这一类，十分钟后
    再试只会多一条一样的警告。
    """

    def __init__(self) -> None:
        self._task: Optional["asyncio.Task[None]"] = None
        self._running = False
        self._last_run_at: Optional[float] = None

    def reset(self) -> None:
        """忘掉上次跑的时间（仅供测试）"""
        self._last_run_at = None

    async def run_once(self, *, now: Optional[datetime.datetime] = None) -> int:
        return await purge_once(now)

    async def tick(self) -> int:
        if current_retention_days() <= 0:
            return 0
        current = time.monotonic()
        if self._last_run_at is not None and current - self._last_run_at < RUN_INTERVAL_SECONDS:
            return 0
        self._last_run_at = current
        return await self.run_once()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while self._running:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - 清理失败不能打掉后台循环
                logger.warning(f"SSH 审计保留期清理出错（明天再试）: {type(exc).__name__}: {exc}")
            await asyncio.sleep(TICK_SECONDS)


ssh_audit_retention = SshAuditRetention()

__all__ = [
    "RUN_INTERVAL_SECONDS",
    "SECONDS_PER_DAY",
    "STARTUP_DELAY_SECONDS",
    "TICK_SECONDS",
    "SshAuditRetention",
    "current_retention_days",
    "purge_once",
    "ssh_audit_retention",
]
