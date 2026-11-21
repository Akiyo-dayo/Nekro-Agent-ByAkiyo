"""插件故障隔离

单进程跑几十个实例时，插件是**所有实例共用的一份代码**（`PluginCollector.loaded_plugins`
每个插件只加载一份），所以插件里的一次事故不是"某个实例出问题"，而是整个进程出问题：

- 抛异常：分发点没有 per-plugin 兜底时，一个插件抛出会把整轮消息处理带崩，
  排在它后面的插件根本不会被调用；
- 卡住不返回：比抛异常严重得多——`await` 一个永远不完成的协程会把发起这次调用的
  那条链路无限挂起，而它占着的是所有实例共用的这一个事件循环；
- 在事件循环里跑同步重活：不报错也不超时，但期间所有实例的消息处理一起停摆。

本模块给「宿主调用插件代码」的分发点提供统一护栏：**异常隔离 + 超时 + 阻塞观测 +
连续失败自动隔离**。

隔离的边界（有意收窄，避免误伤）
--------------------------------
- 隔离只作用于**被护栏包住的那些回调**，不会连带下线插件的沙盒方法、路由、命令。
  一个插件的 `on_user_message` 一直超时，不代表它的画图方法也坏了。
- **沙盒方法（RPC）的报错不计入失败**：那是模型可见的正常控制流（参数写错、
  上游 4xx），把它算作故障会让一个爱报错的方法把整个插件拖进隔离。
- **加载/清理失败不计入失败**：这两处已经有 `PluginCollector.failed_plugins` 这套
  更合适的记录与展示，不重复造第二套状态。

已知限制
--------
超时靠 `asyncio.wait_for`，而它只能取消**让出过控制权**的协程。插件如果在事件循环里
跑纯同步死循环（`while True: pass`），超时不会触发，因为超时回调本身也排在这个被占满的
循环上。这种情况由阻塞观测在调用返回后告警——**单进程内没有办法抢占它**，
真要硬隔离只能上子进程/子解释器，那是另一个量级的改造。
"""

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterator, Optional, TypeVar

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger

if TYPE_CHECKING:
    from nekro_agent.services.plugin.base import NekroPlugin

logger = get_sub_logger("plugin_system")

T = TypeVar("T")

FAILURE_REASON_ERROR = "error"
FAILURE_REASON_TIMEOUT = "timeout"


@dataclass(frozen=True)
class PluginQuarantine:
    """一条生效中的自动隔离记录"""

    plugin_key: str
    stage: str
    reason: str
    detail: str
    failures: int
    since: float
    until: float


# plugin_key -> 连续失败次数（成功一次即清零）
_consecutive_failures: Dict[str, int] = {}
# plugin_key -> 生效中的隔离记录
_quarantines: Dict[str, PluginQuarantine] = {}


def get_quarantine(plugin_key: str, *, now: Optional[float] = None) -> Optional[PluginQuarantine]:
    """取插件当前的隔离记录；冷却期已过则就地解除并返回 None

    自动过期在读取时判定而不是靠定时器：隔离是稀有事件，为它挂一堆后台任务
    在几十个实例的进程里只是白白多占调度。
    """
    entry = _quarantines.get(plugin_key)
    if entry is None:
        return None
    current = time.time() if now is None else now
    if current >= entry.until:
        _quarantines.pop(plugin_key, None)
        _consecutive_failures.pop(plugin_key, None)
        logger.info(f"插件 {plugin_key} 隔离冷却结束，恢复调用（此前因 {entry.stage} {entry.reason} 被隔离）")
        return None
    return entry


def list_quarantines(*, now: Optional[float] = None) -> Dict[str, PluginQuarantine]:
    """当前仍在冷却期内的全部隔离记录"""
    current = time.time() if now is None else now
    return {key: entry for key in list(_quarantines) if (entry := get_quarantine(key, now=current)) is not None}


def record_success(plugin_key: str) -> None:
    """一次成功调用即清零连续失败计数

    用连续失败而不是累计失败：偶发失败（网络抖动、上游 5xx）不该在几天后攒够数量
    把一个正常工作的插件隔离掉。
    """
    _consecutive_failures.pop(plugin_key, None)


def record_failure(
    plugin_key: str,
    stage: str,
    reason: str,
    detail: str,
    *,
    now: Optional[float] = None,
) -> Optional[PluginQuarantine]:
    """登记一次失败；达到阈值时返回新建的隔离记录，否则返回 None"""
    failures = _consecutive_failures.get(plugin_key, 0) + 1
    _consecutive_failures[plugin_key] = failures

    threshold = int(config.PLUGIN_ISOLATION_FAILURE_THRESHOLD)
    if threshold <= 0 or failures < threshold:
        return None

    current = time.time() if now is None else now
    cooldown = max(0.0, float(config.PLUGIN_ISOLATION_COOLDOWN_SECONDS))
    entry = PluginQuarantine(
        plugin_key=plugin_key,
        stage=stage,
        reason=reason,
        detail=detail,
        failures=failures,
        since=current,
        until=current + cooldown,
    )
    _quarantines[plugin_key] = entry
    return entry


def release_quarantine(plugin_key: str, *, reason: str) -> bool:
    """手动解除隔离并清空失败计数，返回此前是否处于隔离状态

    插件重载 / 重新启用时调用：用户修好插件后不该还要再等满一个冷却期。单独计数的回调
    （键是 `插件键#回调名`，见 :func:`call_plugin` 的 `isolation_key`）一并解除。
    """
    prefix = f"{plugin_key}#"
    keys = [plugin_key, *(key for key in set(_consecutive_failures) | set(_quarantines) if key.startswith(prefix))]
    released = False
    for key in keys:
        _consecutive_failures.pop(key, None)
        entry = _quarantines.pop(key, None)
        if entry is not None:
            released = True
            logger.info(f"插件 {key} 的自动隔离已解除（{reason}）")
    return released


def reset_all() -> None:
    """清空全部隔离状态（仅供测试夹具使用）"""
    _consecutive_failures.clear()
    _quarantines.clear()


@contextmanager
def observe_loop_blocking(what: str, warn: Optional[Callable[[str], Any]] = None) -> Iterator[None]:
    """观测一段代码在事件循环线程上烧掉多少 CPU，超阈值告警

    判据用**事件循环线程的 CPU 时间**而不是墙钟耗时：正当地等 LLM / 下网络文件会让墙钟
    很长但不占 CPU，按墙钟告警会把这些插件全部误报。反过来，CPU 时间涨上去就意味着
    这段时间事件循环没法调度别的任务，所有实例一起被拖住。

    注意归因是粗粒度的：`time.thread_time()` 统计的是整个循环线程，被观测代码 `await`
    期间其他任务消耗的 CPU 也会算进来。因此这里只告警，不计入失败，更不触发隔离。
    """
    wall_start = time.perf_counter()
    cpu_start = time.thread_time()
    try:
        yield
    finally:
        cpu_seconds = time.thread_time() - cpu_start
        threshold = float(config.PLUGIN_BLOCKING_WARN_SECONDS)
        if threshold > 0 and cpu_seconds >= threshold:
            (warn or logger.warning)(
                f"{what} 在事件循环线程上占用了 {cpu_seconds:.2f}s CPU"
                f"（墙钟 {time.perf_counter() - wall_start:.2f}s），期间本进程内所有实例的消息处理都被拖慢。"
                f"请把 CPU 密集或同步阻塞的工作改用 asyncio.to_thread 移出事件循环。",
            )


async def call_plugin(
    plugin: "NekroPlugin",
    stage: str,
    func: Callable[..., Awaitable[T]],
    *args: Any,
    default: T,
    timeout: Optional[float] = None,
    isolation_key: Optional[str] = None,
) -> T:
    """带护栏地调用一次插件回调

    插件正在隔离期内、抛异常、或超时未返回时，一律返回 `default`，
    调用方（以及同一轮里的其他插件、其他实例）继续照常往下走。

    Args:
        plugin: 被调用的插件
        stage: 分发点名称，进入日志与隔离记录，用于说清"是哪个回调坏了"
        func: 插件注册的回调本体
        default: 上述任一情况下的回落值
        timeout: 覆盖全局超时；`None` 取 `PLUGIN_CALL_TIMEOUT_SECONDS`，`<= 0` 表示不限时
        isolation_key: 失败计数与隔离记在哪个键上；默认是插件键（与其它受护栏回调共用）。传
            `插件键#回调名` 时这个回调单独计数、单独隔离，它坏了不连带插件的其它回调。
    """
    key = isolation_key or plugin.key
    if get_quarantine(key) is not None:
        return default

    limit = float(config.PLUGIN_CALL_TIMEOUT_SECONDS) if timeout is None else float(timeout)
    with observe_loop_blocking(f"插件 {plugin.name} 的 {stage} 回调", warn=plugin.logger.warning):
        try:
            result = await asyncio.wait_for(func(*args), timeout=limit) if limit > 0 else await func(*args)
        except asyncio.TimeoutError:
            _handle_failure(plugin, stage, FAILURE_REASON_TIMEOUT, f"超过 {limit:.1f}s 未返回，已取消本次调用", key)
            return default
        except Exception as exc:  # noqa: BLE001 - 护栏的全部意义就是不让插件的异常外泄
            _handle_failure(plugin, stage, FAILURE_REASON_ERROR, f"{type(exc).__name__}: {exc}", key)
            return default
        else:
            record_success(key)
            return result


def _handle_failure(plugin: "NekroPlugin", stage: str, reason: str, detail: str, key: Optional[str] = None) -> None:
    """记一次失败并在触发阈值时告警

    告警同时打在插件子 logger（前端按 plugin_key 过滤得到）和插件系统 logger 上：
    自动隔离必须是用户明确看得见的事件，而不是"功能悄悄消失了"。
    """
    plugin.logger.warning(f"插件 {plugin.name} 的 {stage} 回调失败（{reason}）：{detail}")
    entry = record_failure(key or plugin.key, stage, reason, detail)
    if entry is None:
        return
    message = (
        f"插件 {plugin.name}（{plugin.key}）已被自动隔离：{stage} 连续 {entry.failures} 次失败"
        f"（最后一次：{reason} {detail}）。该插件的受护栏回调将在 "
        f"{max(0.0, entry.until - entry.since):.0f}s 内跳过，其他插件与其他实例不受影响；"
        f"修好后重载或重新启用该插件即可立即恢复。"
    )
    plugin.logger.error(message)
    logger.error(message)
