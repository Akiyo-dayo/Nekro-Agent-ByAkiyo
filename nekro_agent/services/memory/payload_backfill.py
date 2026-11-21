"""存量向量 payload 回填的运行期看护

回填按 `payload_schema != 2` 滚动推进，幂等且可中断：每写完一页就把该页的 schema 一起写成 2，
所以中断后重跑只会处理没写完的点。新写入的向量点本来就带新的 payload 键，因此**每个进程
成功跑一次就够**，不需要周期性重复。

运行中开闸不会重启进程，所以这里靠轮询闸状态发现「闸从关变成开」，不挂在修改配置的入口上：
配置可能来自单个接口、批量接口或重新加载 yaml，逐个入口挂钩子总会漏掉一处，而漏掉的那一处
表现为存量记忆按域条件召回不到，却没有任何报错。
"""

from __future__ import annotations

import asyncio
import time

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.memory.feature_flags import is_domain_scope_enabled, is_memory_system_enabled

logger = get_sub_logger("memory.payload_backfill")


class PayloadBackfillWatcher:
    """按闸状态触发一次存量 payload 回填的看护器

    闸关时什么都不做，行为与改造前逐字相同；闸开之后的第一次成功回填把 `_done` 置位，
    本进程内不再重复。
    """

    def __init__(self, *, poll_seconds: float = 10.0, retry_after_failure_seconds: float = 600.0) -> None:
        self._poll_seconds = poll_seconds
        # 失败后隔一段时间再试：Qdrant 短暂不可用时立刻重试只会连着失败，日志被刷满
        self._retry_after_failure_seconds = retry_after_failure_seconds
        # 本进程是否已经成功跑完一次；成功后不再回填
        self._done = False
        # 正在跑的回填任务
        self._task: asyncio.Task[None] | None = None
        # 轮询闸状态的任务
        self._watch_task: asyncio.Task[None] | None = None
        # 上次失败的时刻（`time.monotonic()`），用来算失败窗口
        self._last_failure_at: float | None = None

    def maybe_start(self) -> asyncio.Task | None:
        """闸开着、还没跑过、也不在失败窗口内时起一次回填

        Returns:
            新起的回填任务；本次不启动时返回 None
        """
        if self._done:
            return None
        if self._task is not None and not self._task.done():
            return None
        # 总开关关着时连 Qdrant 都可能没配好，回填没有意义
        if not is_memory_system_enabled():
            return None
        if not is_domain_scope_enabled():
            return None
        if (
            self._last_failure_at is not None
            and time.monotonic() - self._last_failure_at < self._retry_after_failure_seconds
        ):
            return None
        self._task = asyncio.create_task(self._run())
        return self._task

    async def _run(self) -> None:
        """跑一次回填；失败不置 `_done`，等失败窗口过去后再试"""
        # 懒导入：本模块会被启动流程在很早的位置导入，向量库依赖没必要跟着一起加载
        from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

        try:
            count = await memory_qdrant_manager.backfill_paragraph_payloads()
        except asyncio.CancelledError:
            # 关停时取消：原样抛出，让任务按取消收尾
            raise
        except Exception as e:
            self._last_failure_at = time.monotonic()
            logger.warning(
                f"记忆 payload 回填任务失败，{self._retry_after_failure_seconds:.0f} 秒后重试: {e}",
            )
            return
        logger.info(f"记忆 payload 回填任务结束: updated={count}")
        self._done = True

    async def _watch(self) -> None:
        """定时看一眼闸状态；单次异常不能让轮询停掉，跑完一次回填就退出"""
        while not self._done:
            try:
                await asyncio.sleep(self._poll_seconds)
                self.maybe_start()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"记忆 payload 回填轮询出错，继续下一轮: {e}")

    def start(self) -> None:
        """启动轮询，并立刻按当前闸状态尝试一次"""
        if self._watch_task is not None and not self._watch_task.done():
            return
        # 先试一次：启动时闸就开着的部署不必等第一个轮询周期
        self.maybe_start()
        self._watch_task = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        """停止轮询并取消未完成回填"""
        for task in (self._watch_task, self._task):
            if task is None or task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # 取消是这里主动发起的，按正常收尾处理
                pass


payload_backfill_watcher = PayloadBackfillWatcher()
