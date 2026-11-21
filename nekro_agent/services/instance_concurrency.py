"""实例级并发隔离与配额

## 改造前的现状

沙盒执行是整个进程唯一的重资源闸门，改造前它由 `services/sandbox/runner.py` 里
**一个全局 `asyncio.Semaphore(SANDBOX_MAX_CONCURRENT)`** 把守：没有任何分层，
所有实例挤在同一个信号量上。单进程托管几十个实例时这有两个后果，而且第二个更要命：

1. **占用不设防**：`message_service` 只保证「一个频道同时最多一个 Agent 任务」，
   所以一个实例能同时发起的执行数等于它的活跃频道数，可以把全部名额吃干净；
2. **排队顺序不公平**：`asyncio.Semaphore` 的等待者是一条 FIFO 队列。某个实例先塞进
   50 个等待者之后，另一个实例的**第一个**请求排在第 51 位——它要等前面 50 次沙盒执行
   （每次可达 `SANDBOX_RUNNING_TIMEOUT` 秒）跑完才轮得到。

## 为什么是「配额 + 公平轮转」，而不是只加配额

只加"每实例最多占 N 个名额"解决不了第 2 条：名额上限约束的是**同时占用量**，
而饥饿是**排队位次**造成的。给突发实例设上限 2、全局 4，它照样可以在队列里堆 48 个
等待者，后来的实例仍然排在这 48 个之后。

因此本模块把全局信号量换成：**每实例一条等待队列 + 轮转派发 + 每实例并发上限**。

- **公平轮转**是解决饥饿的那一半：派发时按轮转顺序挑下一个实例，一个实例被放行后
  排到轮转队尾。于是任一实例的队首请求，最多等「当前有排队的实例数」次派发就轮到，
  与别人堆了多少个无关。
- **并发上限**是收窄爆炸半径的那一半：轮转保证了"轮得到"，但如果一个实例已经占满全部
  名额，新来的实例仍要等一次执行跑完才有空位。上限把这个等待压到最多一个名额的时间。
  它默认 0（不额外限制），因为单实例部署下设上限等于凭空砍掉吞吐。

两者都保留：只要公平轮转在，配额调低不会造成饥饿；只要配额在，公平轮转的收敛时间有界。

## 配额耗尽时的行为

**排队为主，拒绝为辅，绝不静默丢弃。** 超出队列长度或等待超时时抛
`InstanceConcurrencyRejected`，由调用链把它变成一条用户可见的提示（见
`services/message_service.py`），并计入 `stats()` 供总后台展示。降级执行（跳过沙盒直接
回复）没有采用：沙盒是 Agent 产出内容的唯一途径，跳过它等于回复空话。

## 与四层配置的关系

三项配额都是 `overridable` 字段，按 **实例层 > 适配器层 > 全局层** 解析。
有意**不看频道层**：并发配额是实例这一层的资源分配，让某个频道的覆盖决定整个实例
能占多少名额是说不通的。全局容量 `SANDBOX_MAX_CONCURRENT` 保持不可覆盖。

## 已知限制

容量是每次派发时现读的，因此调**小**立刻生效（新的派发不再超发），调**大**要等下一次
acquire/release 触发派发才会唤醒排队者。有排队就意味着有在途执行，release 很快就会来，
所以不为此另挂定时器。
"""

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Deque, Dict, List, Optional

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("instance_concurrency")

# 无实例标识（非 onebot 适配器、或升级前的历史 chat_key）时归入的公共桶，
# 与 `services/sandbox/runner.py` 的 `.packages` 分目录取同一个名字，语义一致
SHARED_INSTANCE_KEY = "_shared"

REJECT_REASON_QUEUE_FULL = "queue_full"
REJECT_REASON_TIMEOUT = "timeout"

# 参与实例层解析的配额字段
_LIMIT_FIELD_MAX_CONCURRENT = "SANDBOX_INSTANCE_MAX_CONCURRENT"
_LIMIT_FIELD_QUEUE_LIMIT = "SANDBOX_INSTANCE_QUEUE_LIMIT"
_LIMIT_FIELD_QUEUE_TIMEOUT = "SANDBOX_INSTANCE_QUEUE_TIMEOUT_SECONDS"
_LIMIT_FIELDS = (_LIMIT_FIELD_MAX_CONCURRENT, _LIMIT_FIELD_QUEUE_LIMIT, _LIMIT_FIELD_QUEUE_TIMEOUT)


class InstanceConcurrencyRejected(Exception):
    """实例并发配额耗尽，本次执行未被受理

    调用方必须把它变成用户可见的结果，不能吞掉——静默丢弃在聊天里的表现是
    "机器人偶尔就是不理人"，比明确说一句"排队满了"糟糕得多。
    """

    def __init__(self, instance_key: str, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.instance_key = instance_key
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class InstanceLimits:
    """某个实例此刻生效的并发配额"""

    max_concurrent: int
    queue_limit: int
    wait_timeout: float


@dataclass
class InstanceConcurrencyStats:
    """单个实例的并发运行统计，用于总后台观测"""

    instance_key: str
    active: int = 0
    queued: int = 0
    max_concurrent: int = 0
    queue_limit: int = 0
    admitted_total: int = 0
    queued_total: int = 0
    rejected_queue_full: int = 0
    rejected_timeout: int = 0
    max_wait_seconds: float = 0.0
    last_rejected_at: Optional[float] = None


@dataclass
class _Counters:
    """累计计数，与瞬时状态分开存放，实例队列清空后仍然保留"""

    admitted_total: int = 0
    queued_total: int = 0
    rejected_queue_full: int = 0
    rejected_timeout: int = 0
    max_wait_seconds: float = 0.0
    last_rejected_at: Optional[float] = None
    last_notified_at: Optional[float] = None
    limits: InstanceLimits = field(default_factory=lambda: InstanceLimits(0, 0, 0.0))


class InstanceFairScheduler:
    """按实例公平轮转派发全局沙盒名额

    全部状态都在事件循环线程里读写，不需要锁：派发只发生在 `acquire` / `release` 中，
    而两者都是同步代码段（`acquire` 的 await 点在名额发放**之后**）。
    """

    def __init__(self) -> None:
        self._active_total = 0
        # instance_key -> 正在占用的名额数
        self._active: Dict[str, int] = {}
        # instance_key -> 等待名额的 future 队列
        self._queues: Dict[str, Deque[asyncio.Future]] = {}
        # 有排队者的实例，按"下一个该轮到谁"排列；被放行的实例排到队尾
        self._rotation: List[str] = []
        # instance_key -> 累计计数
        self._counters: Dict[str, _Counters] = {}

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    async def acquire(self, instance_key: str, limits: InstanceLimits) -> None:
        """申请一个名额；拿不到时排队，队列满或等待超时抛 `InstanceConcurrencyRejected`"""
        counters = self._counters.setdefault(instance_key, _Counters())
        counters.limits = limits

        queue = self._queues.setdefault(instance_key, deque())
        if limits.queue_limit > 0 and len(queue) >= limits.queue_limit:
            self._record_rejection(instance_key, REJECT_REASON_QUEUE_FULL)
            raise InstanceConcurrencyRejected(
                instance_key,
                REJECT_REASON_QUEUE_FULL,
                f"实例 {instance_key} 已有 {len(queue)} 次执行在排队（上限 {limits.queue_limit}），本次不受理",
            )

        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        queue.append(waiter)
        if instance_key not in self._rotation:
            self._rotation.append(instance_key)

        self._dispatch()

        if waiter.done():
            # 直接拿到名额，没有真正排队
            counters.admitted_total += 1
            return

        counters.queued_total += 1
        enqueued_at = time.perf_counter()
        timeout = limits.wait_timeout if limits.wait_timeout > 0 else None
        try:
            await asyncio.wait_for(waiter, timeout)
        except asyncio.TimeoutError:
            self._abandon(instance_key, waiter)
            self._record_rejection(instance_key, REJECT_REASON_TIMEOUT)
            raise InstanceConcurrencyRejected(
                instance_key,
                REJECT_REASON_TIMEOUT,
                f"实例 {instance_key} 等待并发名额超过 {limits.wait_timeout:.0f}s，本次不受理",
            ) from None
        except asyncio.CancelledError:
            self._abandon(instance_key, waiter)
            raise

        waited = time.perf_counter() - enqueued_at
        counters.admitted_total += 1
        counters.max_wait_seconds = max(counters.max_wait_seconds, waited)

    def release(self, instance_key: str) -> None:
        """归还一个名额并立刻派发给下一位"""
        holding = self._active.get(instance_key)
        if holding is None:
            # 名额没发出去过（例如 acquire 阶段就被拒），不能凭空把全局计数减掉
            return
        if holding > 1:
            self._active[instance_key] = holding - 1
        else:
            del self._active[instance_key]
        self._active_total = max(0, self._active_total - 1)
        self._dispatch()

    def stats(self) -> Dict[str, InstanceConcurrencyStats]:
        """当前全部实例的并发统计（含已无在途执行但历史上出现过的实例）"""
        keys = set(self._counters) | set(self._active) | set(self._queues)
        result: Dict[str, InstanceConcurrencyStats] = {}
        for key in sorted(keys):
            counters = self._counters.get(key, _Counters())
            result[key] = InstanceConcurrencyStats(
                instance_key=key,
                active=self._active.get(key, 0),
                queued=len(self._queues.get(key, ())),
                max_concurrent=counters.limits.max_concurrent,
                queue_limit=counters.limits.queue_limit,
                admitted_total=counters.admitted_total,
                queued_total=counters.queued_total,
                rejected_queue_full=counters.rejected_queue_full,
                rejected_timeout=counters.rejected_timeout,
                max_wait_seconds=counters.max_wait_seconds,
                last_rejected_at=counters.last_rejected_at,
            )
        return result

    def capacity_snapshot(self) -> Dict[str, int]:
        """全局容量与占用，供总后台显示"""
        return {"capacity": self._capacity(), "active": self._active_total, "queued": sum(len(q) for q in self._queues.values())}

    def should_notify_rejection(self, instance_key: str, *, now: Optional[float] = None) -> bool:
        """是否该为这次拒绝发一条聊天提示

        过载时每条被拒消息都提示一次，等于把提示本身刷成新的洪水，因此按实例做冷却。
        冷却窗口内仍然计数、仍然打日志，只是不再往聊天里发。
        """
        cooldown = max(0.0, float(config.SANDBOX_INSTANCE_REJECT_NOTICE_COOLDOWN_SECONDS))
        current = time.time() if now is None else now
        counters = self._counters.setdefault(instance_key, _Counters())
        last = counters.last_notified_at
        if cooldown > 0 and last is not None and current - last < cooldown:
            return False
        counters.last_notified_at = current
        return True

    def reset_all(self) -> None:
        """清空全部调度状态（仅供测试夹具使用）"""
        for queue in self._queues.values():
            for waiter in queue:
                if not waiter.done():
                    waiter.cancel()
        self._active_total = 0
        self._active.clear()
        self._queues.clear()
        self._rotation.clear()
        self._counters.clear()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _capacity(self) -> int:
        return max(1, int(config.SANDBOX_MAX_CONCURRENT))

    def _dispatch(self) -> None:
        """在容量允许的范围内，按轮转顺序把名额发给下一个可放行的实例"""
        while self._active_total < self._capacity():
            instance_key = self._next_admissible()
            if instance_key is None:
                return
            waiter = self._queues[instance_key].popleft()
            self._active[instance_key] = self._active.get(instance_key, 0) + 1
            self._active_total += 1
            waiter.set_result(None)

            # 放行过的实例排到轮转队尾——这就是"公平"的全部内容
            self._rotation.remove(instance_key)
            if self._queues[instance_key]:
                self._rotation.append(instance_key)
            else:
                del self._queues[instance_key]

    def _next_admissible(self) -> Optional[str]:
        """按轮转顺序找出下一个可放行的实例，顺带清掉已取消的等待者与空队列"""
        for instance_key in list(self._rotation):
            queue = self._queues.get(instance_key)
            while queue and queue[0].done():
                # 等待者已被取消（超时 / 上层任务被取消），名额不能发给它
                queue.popleft()
            if not queue:
                self._rotation.remove(instance_key)
                self._queues.pop(instance_key, None)
                continue
            counters = self._counters.get(instance_key)
            limit = counters.limits.max_concurrent if counters is not None else 0
            if limit > 0 and self._active.get(instance_key, 0) >= limit:
                continue
            return instance_key
        return None

    def _abandon(self, instance_key: str, waiter: "asyncio.Future") -> None:
        """放弃一个等待中的名额申请

        两种情况都要覆盖：还在排队（把它从队列里摘掉），以及"超时与派发撞在一起、
        名额其实已经发下来了"（必须还回去，否则这一格永远漏掉）。
        """
        queue = self._queues.get(instance_key)
        if queue is not None:
            try:
                queue.remove(waiter)
            except ValueError:
                pass
            if not queue:
                self._queues.pop(instance_key, None)
                if instance_key in self._rotation:
                    self._rotation.remove(instance_key)
        if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
            self.release(instance_key)
        elif not waiter.done():
            waiter.cancel()

    def _record_rejection(self, instance_key: str, reason: str) -> None:
        counters = self._counters.setdefault(instance_key, _Counters())
        counters.last_rejected_at = time.time()
        if reason == REJECT_REASON_QUEUE_FULL:
            counters.rejected_queue_full += 1
        else:
            counters.rejected_timeout += 1
        logger.warning(
            f"实例 {instance_key} 的沙盒执行被并发配额拒绝（{reason}）："
            f"占用 {self._active.get(instance_key, 0)}/{counters.limits.max_concurrent or '不限'}，"
            f"排队 {len(self._queues.get(instance_key, ()))}/{counters.limits.queue_limit or '不限'}，"
            f"全局 {self._active_total}/{self._capacity()}。其他实例不受影响。",
        )


scheduler = InstanceFairScheduler()


# ------------------------------------------------------------------
# 配额解析（四层配置：实例层 > 适配器层 > 全局层）
# ------------------------------------------------------------------

# (config_version, instance_key) -> 解析结果，随配置版本号自然失效
_limits_cache: Dict[str, InstanceLimits] = {}
_limits_cache_version = -1


def resolve_instance_limits(adapter_key: str, instance_key: str) -> InstanceLimits:
    """解析该实例生效的并发配额

    结果按配置版本号缓存：这是沙盒热路径上的调用，而覆盖配置一旦写入
    `config_resolver` 就会自增版本号，因此缓存不会读到陈旧值。
    """
    global _limits_cache_version

    from nekro_agent.services.config_resolver import config_resolver

    version = config_resolver.current_version()
    if version != _limits_cache_version:
        _limits_cache.clear()
        _limits_cache_version = version

    cache_key = f"{adapter_key}/{instance_key}"
    cached = _limits_cache.get(cache_key)
    if cached is not None:
        return cached

    values: Dict[str, Any] = {name: getattr(config, name) for name in _LIMIT_FIELDS}
    taken: set[str] = set()
    for overrides in _override_layers(adapter_key, instance_key):
        for name in _LIMIT_FIELDS:
            if name in taken:
                continue
            if getattr(overrides, f"enable_{name}", False):
                values[name] = getattr(overrides, name)
                taken.add(name)

    limits = InstanceLimits(
        max_concurrent=max(0, int(values[_LIMIT_FIELD_MAX_CONCURRENT])),
        queue_limit=max(0, int(values[_LIMIT_FIELD_QUEUE_LIMIT])),
        wait_timeout=max(0.0, float(values[_LIMIT_FIELD_QUEUE_TIMEOUT])),
    )
    _limits_cache[cache_key] = limits
    return limits


def _override_layers(adapter_key: str, instance_key: str) -> List[Any]:
    """按优先级从高到低取出实例层与适配器层的覆盖配置

    只在覆盖文件确实存在时才去加载：`UnifiedConfigService._get_config_instance` 会
    顺手把文件建出来，而实例覆盖目录一旦存在，`config_resolver` 就认为"该适配器有实例层"，
    给所有频道的配置解析都加上一次反查。为读一个配额把整套解析开销打开是不划算的。
    """
    from nekro_agent.core.overridable_config import OverridableConfig
    from nekro_agent.services.config_service import (
        ADAPTER_OVERRIDE_KEY_PREFIX,
        UnifiedConfigService,
        adapter_override_config_path,
        instance_override_config_key,
        instance_override_config_path,
    )

    layers: List[Any] = []
    if instance_key and instance_key != SHARED_INSTANCE_KEY:
        try:
            instance_path = instance_override_config_path(adapter_key, instance_key)
            instance_config_key = instance_override_config_key(adapter_key, instance_key)
        except ValueError:
            instance_path, instance_config_key = None, ""
        if instance_path is not None and instance_path.is_file():
            overrides = UnifiedConfigService._get_config_instance(instance_config_key)  # noqa: SLF001
            if isinstance(overrides, OverridableConfig):
                layers.append(overrides)

    adapter_path = adapter_override_config_path(adapter_key)
    if adapter_path.is_file():
        adapter_overrides = UnifiedConfigService._get_config_instance(f"{ADAPTER_OVERRIDE_KEY_PREFIX}{adapter_key}")  # noqa: SLF001
        if isinstance(adapter_overrides, OverridableConfig):
            layers.append(adapter_overrides)
    return layers


def instance_key_of(chat_key: str) -> str:
    """把会话标识归到某个并发桶

    没有实例段的会话（非 onebot 适配器、历史数据）统一归入公共桶，与改造前"大家共用
    一个信号量"的语义一致——它们之间不隔离，但不会再挤占有实例标识的那些实例。
    """
    from nekro_agent.adapters.onebot_v11.core.channel import is_valid_instance_key, parse_instance_key

    instance_key = parse_instance_key(chat_key)
    if not instance_key or not is_valid_instance_key(instance_key):
        return SHARED_INSTANCE_KEY
    return instance_key


@asynccontextmanager
async def instance_slot(chat_key: str) -> AsyncIterator[str]:
    """占用一个属于该会话所属实例的沙盒名额

    Raises:
        InstanceConcurrencyRejected: 该实例排队已满或等待超时
    """
    from nekro_agent.adapters import resolve_adapter_key_from_chat_key

    instance_key = instance_key_of(chat_key)
    limits = resolve_instance_limits(resolve_adapter_key_from_chat_key(chat_key), instance_key)
    await scheduler.acquire(instance_key, limits)
    try:
        yield instance_key
    finally:
        scheduler.release(instance_key)
