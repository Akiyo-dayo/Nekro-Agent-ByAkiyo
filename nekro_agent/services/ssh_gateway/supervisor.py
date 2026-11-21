"""SSH 网关监督任务

## 为什么要有它

网关是 NA 主进程里的一个模块，总开关是后台可热改的活配置，没有一个通用的"保存后"钩子
可以依赖。所以这里放一个每 5 秒读一次开关与端口的小循环：关着时它什么都不做（不导入
`asyncssh`、不碰主机钥匙），打开时才去加载服务端、起监听、同步地址；关掉时停监听、
断开全部连接。平台方在后台点一下，最多 5 秒生效。

## 起不来不挡 NA

导入服务端失败、主机钥匙读不出、端口被占、绑不上地址，都只把状态标成「不可用：原因」，
下一拍重试。异常绝不逃出循环，NA 照常启动、照常服务。同一原因 5 分钟只记一条 ERROR，
避免端口长期被占时日志被刷满。

## 熔断

E2 在每个兜住的意外异常处调用 :meth:`Supervisor.report_unexpected`。60 秒内超过 20 次
就进入 `TRIPPED`：立即停掉后端，记 ERROR。此时开关仍然开着也不重启，直到平台方把开关
关掉再打开——那一拍会复位熔断状态。宁可网关停，也不让它反复出错拖累 NA。

`tick` 里每一处 `await` 之后都要重新看一眼是不是已经熔断：熔断可能恰好发生在等待
期间，不能被这一拍随后写下的 `RUNNING` 盖掉。

## 周期复核

后端在跑时，每一拍调一次 `backend.sweep()`：结束过期的租约、复核每条在跑的连接（名单、
白名单、实例状态、租约），不过的整条断开。授权撤销因此最多 5 秒（加复核缓存 5 秒）后
生效，不依赖「下一次开通道」。

## 事件循环让路

另一个 100 ms 的计时任务量事件循环延迟（实际间隔减 100 ms）。超过配置阈值就把
`yield_requested()` 置真，桥接层据此暂停读；回落到阈值一半以下再恢复。只在开关开着时
测，关着时恒为假。

## 状态

`status()` 只返回状态、原因、监听地址、连接数、通道数、在跑租约数，没有任何凭据或
目标地址。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import time
from enum import Enum
from typing import Awaitable, Callable, Dict, Optional, Protocol, Set

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.ssh_gateway import keys, registry
from nekro_agent.services.ssh_gateway.lease import running_lease_count

logger = get_sub_logger("ssh_gateway.supervisor")

# 单调时钟；测试注入它来验证日志限流，不必真等
_clock: Callable[[], float] = time.monotonic

SERVER_MODULE = "nekro_agent.services.ssh_gateway.server"
LAG_INTERVAL_SECONDS = 0.1
UNEXPECTED_WINDOW_SECONDS = 60.0
UNEXPECTED_TRIP_THRESHOLD = 20
ERROR_LOG_INTERVAL_SECONDS = 300.0


class GatewayState(str, Enum):
    """网关的对外状态"""

    OFF = "off"
    STARTING = "starting"
    RUNNING = "running"
    UNAVAILABLE = "unavailable"
    TRIPPED = "tripped"


class GatewayBackend(Protocol):
    """E2 注册进来的真正服务端

    监督任务只经这五个方法跟它打交道，不关心 asyncssh 的细节。
    """

    async def start(self) -> None:
        """起服务端（可以重复调用；已经起着时应当幂等）"""

    async def sync_listeners(self, addresses: frozenset[str], port: int) -> None:
        """让在听的地址集合等于 `addresses`，端口按 `port`；后端自己处理换端口"""

    async def stop(self) -> None:
        """停监听、断开全部连接、关上游"""

    def counters(self) -> dict:
        """连接数 / 通道数等计数，供状态接口展示"""


YieldCallback = Callable[[bool], Awaitable[None] | None]

EnabledChangeCallback = Callable[[bool], Awaitable[None]]

# 总开关「关 -> 开 / 开 -> 关」的那一拍要通知的订阅者。放在模块级而不是实例上，
# 是为了让接线的模块只依赖一个名字，不必先拿到 `supervisor` 实例。
_enabled_change_callbacks: list[EnabledChangeCallback] = []


def on_enabled_change(callback: EnabledChangeCallback) -> None:
    """订阅总开关的跳变；回调抛错只记日志，不影响监督任务与别的订阅者"""
    _enabled_change_callbacks.append(callback)


async def _dispatch_enabled_change(enabled: bool) -> None:
    for callback in list(_enabled_change_callbacks):
        try:
            await callback(enabled)
        except Exception as exc:  # noqa: BLE001 - 一个订阅者失败不能拖垮监督任务
            logger.warning(
                f"SSH 网关开关变化回调失败（已忽略）: enabled={enabled} "
                f"callback={getattr(callback, '__name__', repr(callback))} | {type(exc).__name__}: {exc}",
            )


class Supervisor:
    """网关的启停、熔断、让路与状态"""

    def __init__(self, *, tick_seconds: float = 5.0, lag_seconds: float = LAG_INTERVAL_SECONDS) -> None:
        self._tick_seconds = max(0.01, float(tick_seconds))
        self._lag_seconds = max(0.01, float(lag_seconds))
        self._running = False
        self._loop_task: Optional["asyncio.Task[None]"] = None
        self._lag_task: Optional["asyncio.Task[None]"] = None
        self._state = GatewayState.OFF
        self._reason = ""
        self._backend: Optional[GatewayBackend] = None
        self._backend_started = False
        self._host_key_ready = False
        # 上一拍开关是不是开着：关掉的那一拍要停后端
        self._switch_was_on = False
        self._unexpected_at: list[float] = []
        self._error_log_at: Dict[str, float] = {}
        self._stop_tasks: Set["asyncio.Task[None]"] = set()
        self._yield_requested = False
        self._yield_callbacks: list[YieldCallback] = []
        self._yield_tasks: Set["asyncio.Task[None]"] = set()

    # ==== 生命周期 ====

    async def start(self) -> None:
        """起常驻循环；重复调用无副作用"""
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._run_loop())
        self._lag_task = asyncio.create_task(self._lag_loop())

    async def stop(self, timeout: float = 5.0) -> None:
        """停循环、停后端；最多等 `timeout` 秒，关机不挂住 NA"""
        self._running = False
        for task in (self._loop_task, self._lag_task):
            if task is not None:
                task.cancel()
        for task in (self._loop_task, self._lag_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾不关心异常
                    pass
        self._loop_task = None
        self._lag_task = None

        try:
            await asyncio.wait_for(self._stop_backend_safely(), timeout)
        except asyncio.TimeoutError:
            logger.error("SSH 网关关停超时，已有连接随进程退出直接断开")
        except Exception as exc:  # noqa: BLE001 - 关停失败不能拦住 NA 退出
            logger.warning(f"SSH 网关关停时出错（已忽略）: {type(exc).__name__}: {exc}")
        self._switch_was_on = False
        self._state = GatewayState.OFF
        self._reason = ""
        self._unexpected_at.clear()
        self._set_yield_requested(False)

    async def tick(self) -> None:
        """一拍：读开关与端口，按需启停、同步监听"""
        if not bool(config.SSH_GATEWAY_ENABLED):
            switch_was_on = self._switch_was_on
            if switch_was_on:
                await self._stop_backend_safely()
            self._switch_was_on = False
            self._state = GatewayState.OFF
            self._reason = ""
            # 熔断状态在关掉的这一拍复位：平台方关掉再打开即可恢复
            self._unexpected_at.clear()
            self._set_yield_requested(False)
            if switch_was_on:
                await _dispatch_enabled_change(False)
            return

        # 关 -> 开：先通知订阅者（补网络规则、写容器文件、登记监听地址），
        # 下面这次 `sync_listeners` 就能把新地址一起起起来，不必多等一拍。
        switch_was_on = self._switch_was_on
        self._switch_was_on = True
        if not switch_was_on:
            await _dispatch_enabled_change(True)
        if self._state == GatewayState.TRIPPED:
            # 熔断后不再自动重启；等开关关掉再打开
            return

        try:
            port = int(config.SSH_GATEWAY_PORT)
        except (TypeError, ValueError):
            port = 0

        self._state = GatewayState.STARTING
        if self._backend is None:
            try:
                importlib.import_module(SERVER_MODULE)
            except Exception as exc:  # noqa: BLE001 - 缺依赖、端口问题都只让网关不可用
                self._mark_unavailable(f"服务端模块加载失败: {type(exc).__name__}: {exc}")
                return
            if self._backend is None:
                self._mark_unavailable("服务端模块没有注册后端")
                return

        if not self._host_key_ready:
            try:
                keys.load_or_create_host_key()
            except Exception as exc:  # noqa: BLE001 - 钥匙坏了只能等平台方处理
                self._mark_unavailable(f"主机钥匙不可用: {exc}")
                return
            self._host_key_ready = True

        if not self._backend_started:
            try:
                await self._backend.start()
            except Exception as exc:  # noqa: BLE001 - 端口占用等，下一拍重试
                self._mark_unavailable(f"服务端启动失败: {type(exc).__name__}: {exc}")
                return
            if self._state == GatewayState.TRIPPED:
                return
            self._backend_started = True

        try:
            await self._backend.sync_listeners(registry.desired_listen_addresses(), port)
        except Exception as exc:  # noqa: BLE001 - 绑定失败等，下一拍重试
            self._mark_unavailable(f"监听同步失败: {type(exc).__name__}: {exc}")
            return
        if self._state == GatewayState.TRIPPED:
            return

        sweep = getattr(self._backend, "sweep", None)
        if sweep is not None:
            try:
                await sweep()
            except Exception as exc:  # noqa: BLE001 - 复核出错下一拍再来，不计入熔断
                logger.warning(f"SSH 网关周期复核出错（下一拍继续）: {type(exc).__name__}: {exc}")
        if self._state == GatewayState.TRIPPED:
            return

        self._state = GatewayState.RUNNING
        self._reason = ""

    # ==== 后端注册与熔断 ====

    def register_backend(self, backend: GatewayBackend) -> None:
        """E2 的服务端模块在导入时调用；换一个后端时重新走 start"""
        if backend is self._backend:
            return
        self._backend = backend
        self._backend_started = False

    def report_unexpected(self, exc: BaseException) -> None:
        """E2 在每个兜住的意外异常处调用；用于一分钟内超过阈值时熔断"""
        now = _clock()
        self._unexpected_at = [item for item in self._unexpected_at if now - item <= UNEXPECTED_WINDOW_SECONDS]
        self._unexpected_at.append(now)
        if len(self._unexpected_at) <= UNEXPECTED_TRIP_THRESHOLD:
            return
        if self._state == GatewayState.TRIPPED:
            return
        self._state = GatewayState.TRIPPED
        self._reason = "一分钟内兜住的意外异常过多，已熔断"
        logger.error(
            "SSH 网关已熔断（一分钟内兜住的意外异常超过上限），已停止监听并断开全部连接；"
            "平台方查明后在后台关掉再打开即可恢复",
        )
        self._schedule_backend_stop()

    # ==== 状态与让路 ====

    def status(self) -> dict:
        """状态接口的载荷：不含任何凭据、主机或端口之外的目标信息"""
        counters: dict = {}
        if self._backend is not None and self._backend_started:
            try:
                counters = dict(self._backend.counters() or {})
            except Exception:  # noqa: BLE001 - 计数出错不能拖垮状态接口
                counters = {}
        try:
            port = int(config.SSH_GATEWAY_PORT)
        except (TypeError, ValueError):
            port = 0
        return {
            "state": self._state.value,
            "reason": self._reason,
            "listen_addresses": sorted(registry.desired_listen_addresses()),
            "port": port,
            "connections": int(counters.get("connections", 0) or 0),
            "channels": int(counters.get("channels", 0) or 0),
            "running_leases": running_lease_count(),
        }

    def yield_requested(self) -> bool:
        """桥接层是否应该暂停读（事件循环忙时让路）"""
        return self._yield_requested

    def on_yield_change(self, callback: YieldCallback) -> None:
        """订阅让路状态变化；回调抛错不影响别的订阅者"""
        self._yield_callbacks.append(callback)

    # ==== 内部 ====

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - 监督任务自己出错只记日志，下一拍继续
                logger.error(f"SSH 网关监督任务出错（下一拍继续）: {type(exc).__name__}: {exc}")
            await asyncio.sleep(self._tick_seconds)

    async def _lag_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self._running:
            started = loop.time()
            await asyncio.sleep(self._lag_seconds)
            try:
                enabled = bool(config.SSH_GATEWAY_ENABLED)
                threshold = int(config.SSH_GATEWAY_LOOP_LAG_MS)
            except (TypeError, ValueError):
                enabled = False
                threshold = 0
            if not enabled or threshold <= 0:
                self._set_yield_requested(False)
                # 关着时不必 100 ms 醒一次：只读一个布尔值，隔一秒再看
                await asyncio.sleep(1.0)
                continue
            actual = loop.time() - started
            lag_ms = max(0.0, (actual - self._lag_seconds) * 1000.0)
            if lag_ms > float(threshold):
                self._set_yield_requested(True)
            elif lag_ms < float(threshold) / 2.0:
                self._set_yield_requested(False)

    def _mark_unavailable(self, reason: str) -> None:
        if self._state == GatewayState.TRIPPED:
            return
        self._state = GatewayState.UNAVAILABLE
        self._reason = reason
        now = _clock()
        last = self._error_log_at.get(reason)
        if last is None or now - last >= ERROR_LOG_INTERVAL_SECONDS:
            self._error_log_at[reason] = now
            logger.error(f"SSH 网关不可用: {reason}（下一拍重试，同一原因 5 分钟内只记一条）")

    async def _stop_backend_safely(self) -> None:
        backend = self._backend
        if backend is None or not self._backend_started:
            self._backend_started = False
            return
        self._backend_started = False
        try:
            await backend.stop()
        except Exception as exc:  # noqa: BLE001 - 停后端失败只记日志
            logger.warning(f"SSH 网关后端停止失败（已忽略）: {type(exc).__name__}: {exc}")

    def _schedule_backend_stop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._stop_backend_safely())
        self._stop_tasks.add(task)
        task.add_done_callback(self._stop_tasks.discard)

    def _set_yield_requested(self, value: bool) -> None:
        if value == self._yield_requested:
            return
        self._yield_requested = value
        for callback in list(self._yield_callbacks):
            try:
                result = callback(value)
            except Exception as exc:  # noqa: BLE001 - 一个订阅者出错不能影响别的
                logger.warning(f"SSH 网关让路回调失败（已忽略）: {type(exc).__name__}: {exc}")
                continue
            if inspect.isawaitable(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    continue
                task = loop.create_task(self._await_callback(result))
                self._yield_tasks.add(task)
                task.add_done_callback(self._yield_tasks.discard)

    async def _await_callback(self, result: object) -> None:
        try:
            await result  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SSH 网关让路回调失败（已忽略）: {type(exc).__name__}: {exc}")


supervisor = Supervisor()


__all__ = [
    "EnabledChangeCallback",
    "GatewayBackend",
    "GatewayState",
    "Supervisor",
    "on_enabled_change",
    "supervisor",
]
