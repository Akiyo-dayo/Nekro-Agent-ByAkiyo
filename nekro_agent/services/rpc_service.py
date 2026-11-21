import asyncio
import gc
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional, Tuple, Union

from pydantic import ValidationError as PydanticValidationError
from starlette.requests import ClientDisconnect, Request

from nekro_agent.core.config import config
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.rpc import RPCRequest
from nekro_agent.tools.rpc_codec import (
    DECODE_BUDGET_BURST,
    DECODE_BUDGET_PER_SECOND,
    MAX_BODY_BYTES,
    MAX_EXECUTING_PER_CONTAINER,
    MAX_IN_FLIGHT_PER_CONTAINER,
    MAX_PENDING_DECODES,
    RpcCodecError,
    decode_rpc_request_body,
)

_CONTENT_LENGTH = re.compile(r"[0-9]{1,19}")


class RpcBusyError(RpcCodecError):
    """闸门拒绝了这次调用（在途 / 执行中过多、解码预算用完、全进程排队已满）。不排队，立即回给沙盒

    `retry_after` 是建议的重试间隔（秒）；沙盒侧 `api_caller.py` 据此自动退避重试
    """

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RpcClientGone(RpcCodecError):
    """读请求体时对端已经断开：没有人等这个回应，只计数，不记日志（断开本身可以被反复制造）"""


class _DecodeBudget:
    """每个容器的解码时间预算（令牌桶）：每秒补 `DECODE_BUDGET_PER_SECOND` 秒，最多攒 `DECODE_BUDGET_BURST` 秒

    解码线程按实测耗时扣（可以扣成负数，欠账期间的请求在读请求体之前就被拒绝）。事件循环线程读、解码线程写，所以加锁。
    只记有过消耗的容器；补满的条目在表变大时清掉，攻击者换不出无限多的容器键（键由令牌绑定）
    """

    _MAX_KEYS = 4096

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: Dict[str, Tuple[float, float]] = {}

    def _level(self, key: str, now: float) -> float:
        level, at = self._buckets.get(key, (DECODE_BUDGET_BURST, now))
        return min(DECODE_BUDGET_BURST, level + (now - at) * DECODE_BUDGET_PER_SECOND)

    def available(self, key: str) -> float:
        with self._lock:
            return self._level(key, self._clock())

    def charge(self, key: str, seconds: float) -> None:
        with self._lock:
            now = self._clock()
            self._buckets[key] = (self._level(key, now) - seconds, now)
            if len(self._buckets) > self._MAX_KEYS:
                for stale in [k for k in self._buckets if self._level(k, now) >= DECODE_BUDGET_BURST]:
                    self._buckets.pop(stale, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


decode_budget = _DecodeBudget()

# 每个容器正在读请求体 / 等待解码 / 解码中的请求数，与正在执行的方法数。只在事件循环线程里改，不需要锁
_in_flight: Dict[str, int] = {}
_executing: Dict[str, int] = {}


def _increment(table: Dict[str, int], key: str) -> None:
    table[key] = table.get(key, 0) + 1


def _decrement(table: Dict[str, int], key: str) -> None:
    remaining = table.get(key, 1) - 1
    if remaining > 0:
        table[key] = remaining
    else:
        table.pop(key, None)


@contextmanager
def rpc_admission(container_key: str) -> Iterator[None]:
    """读请求体之前的闸门，三道都是“超出立即拒绝、不排队”：

    1. 本容器正在执行的方法达到 :data:`MAX_EXECUTING_PER_CONTAINER`：每个执行中的调用都持有解码出的参数，
       没有上限的话，沙盒每秒几十次打到会等外部 I/O 的方法就能挂起几百个调用、占掉 GB 级内存；
    2. 本容器同时在读或解码的请求达到 :data:`MAX_IN_FLIGHT_PER_CONTAINER`：排队的请求体也要占内存；
    3. 本容器的解码时间预算欠账：只限并发不够，单个容器背靠背地发、每个都被拒，解码线程照样满负荷。

    正常的 `api_caller.py` 收到这种拒绝会按建议的间隔自动重试。

    Raises:
        RpcBusyError: 任何一道闸拒绝
    """
    executing = _executing.get(container_key, 0)
    if executing >= MAX_EXECUTING_PER_CONTAINER:
        raise RpcBusyError(
            f"RPC 请求被拒绝：本容器已有 {executing} 个方法正在执行（上限 {MAX_EXECUTING_PER_CONTAINER}），请稍后重试",
            retry_after=0.2,
        )
    count = _in_flight.get(container_key, 0)
    if count >= MAX_IN_FLIGHT_PER_CONTAINER:
        raise RpcBusyError(
            f"RPC 请求被拒绝：本容器已有 {count} 个调用正在传输或解码（上限 {MAX_IN_FLIGHT_PER_CONTAINER}），"
            "不排队，请稍后重试",
            retry_after=0.05,
        )
    level = decode_budget.available(container_key)
    if level < 0:
        wait = -level / DECODE_BUDGET_PER_SECOND
        raise RpcBusyError(
            f"RPC 请求被拒绝：本容器的解码时间预算用完了（每秒 {DECODE_BUDGET_PER_SECOND * 1000:.0f} ms），"
            f"约 {wait * 1000:.0f} ms 后再试。正常大小的参数解码不到 1 ms，碰到这道闸说明在反复发送很大的请求体",
            retry_after=min(wait, 5.0),
        )
    _increment(_in_flight, container_key)
    try:
        yield
    finally:
        _decrement(_in_flight, container_key)


@contextmanager
def rpc_execution(container_key: str) -> Iterator[None]:
    """方法执行期间占一个名额；解码结束到开始执行之间名额可能已经被占满，这里再查一次

    Raises:
        RpcBusyError: 本容器执行中的方法已满
    """
    executing = _executing.get(container_key, 0)
    if executing >= MAX_EXECUTING_PER_CONTAINER:
        raise RpcBusyError(
            f"RPC 请求被拒绝：本容器已有 {executing} 个方法正在执行（上限 {MAX_EXECUTING_PER_CONTAINER}），请稍后重试",
            retry_after=0.2,
        )
    _increment(_executing, container_key)
    try:
        yield
    finally:
        _decrement(_executing, container_key)


async def read_rpc_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
    """读沙盒发来的请求体，最多读 `limit` 字节

    先看 `Content-Length`：声明超限的一个字节都不读。没有声明（分块传输）或声明不实的，边读边数，
    超过上限立刻停下，不会先把整个请求体读进内存（旧实现 `await request.body()` 没有上限，沙盒能流式灌 GB 级数据）。
    读到一半对端断开时抛 :class:`RpcClientGone`，由路由静默处理（这种断开能被反复制造，逐条记堆栈就是刷日志）。

    Raises:
        RpcCodecError: 超过上限或 `Content-Length` 不合法
        RpcClientGone: 对端断开
    """
    declared = request.headers.get("content-length")
    if declared is not None and (not _CONTENT_LENGTH.fullmatch(declared) or int(declared) > limit):
        raise RpcCodecError(f"RPC 请求被拒绝：请求体超过 {limit} 字节（Content-Length）")
    received = bytearray()
    try:
        async for chunk in request.stream():
            if len(received) + len(chunk) > limit:
                raise RpcCodecError(f"RPC 请求被拒绝：请求体超过 {limit} 字节")
            received += chunk
    except ClientDisconnect as e:
        raise RpcClientGone("RPC 请求被拒绝：读请求体时对端断开") from e
    return bytes(received)


def decode_rpc_request(raw_body: bytes) -> RPCRequest:
    """解码沙盒发来的请求体

    请求体来自沙盒里模型生成的代码，按敌对输入处理：只经 :func:`decode_rpc_request_body`（JSON + 固定的类型标签，
    逐项设上限）。请求方向不再用 pickle：白名单 `Unpickler` 交出去的是真实的类，`BUILD` 能改写宿主进程里的类属性
    （见 `tools/rpc_codec.py`）。

    Raises:
        RpcCodecError: 不合法、含不支持的类型或超限；消息可直接回给沙盒，用于诊断
        ValidationError: 解出来的结构不是 `RPCRequest`
    """
    method, args, kwargs = decode_rpc_request_body(raw_body)
    try:
        return RPCRequest.model_validate({"method": method, "args": args, "kwargs": kwargs})
    except PydanticValidationError as e:
        raise ValidationError(reason=str(e)) from e


# 解码专用的单线程：所有容器的解码在这里串行，不占共用的默认线程池（`to_thread` 的网络守卫下发、邮件、知识库等）
_DECODE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rpc-decode")
_pending_lock = threading.Lock()
_pending_decodes = 0


def max_pending_decodes() -> int:
    """全进程等待解码的上限：不小于 2 × 沙盒并发上限（每个容器在途 2 个），运营方调大沙盒并发时跟着放大"""
    return max(MAX_PENDING_DECODES, 2 * int(getattr(config, "SANDBOX_MAX_CONCURRENT", 0) or 0))


def _decode_with_gc_paused(raw_body: bytes, budget_key: Optional[str] = None) -> Union[RPCRequest, str]:
    """在解码线程里执行：解码期间暂停循环 GC，返回解出的请求，或拒绝原因（字符串）；耗时记到容器的解码预算上

    为什么要暂停 GC：`json.loads` 是 C 代码、全程持 GIL，而 1 MiB 的 `[[],[],…]` 会新建几十万个容器，触发整堆回收；
    导入完整应用之后，同一个请求体 GC 开着要 280 ms，关掉只要 15 ms——光放进线程，事件循环照样被冻住。
    拒绝时不把异常带出线程：异常的回溯帧引用着半成品结构，带回事件循环就会在 GC 恢复之后被整批提升进老年代。
    这里只交回一句原因，异常在 `except` 块结束时就被丢掉，半成品随之在暂停 GC 的窗口里被引用计数释放。
    `ValidationError` 也在这里变成原因字符串，调用方只需要处理 :class:`RpcCodecError`。
    """
    started = time.perf_counter()
    paused = gc.isenabled()
    if paused:
        gc.disable()
    try:
        try:
            return decode_rpc_request(raw_body)
        except (RpcCodecError, ValidationError) as e:
            return str(e)
    finally:
        if paused:
            gc.enable()
        if budget_key is not None:
            decode_budget.charge(budget_key, time.perf_counter() - started)


def _release_pending(_future: "Future[Any]") -> None:
    global _pending_decodes
    with _pending_lock:
        _pending_decodes -= 1


async def decode_rpc_request_off_loop(raw_body: bytes, container_key: Optional[str] = None) -> RPCRequest:
    """解码沙盒的请求体：交给专用的单线程串行解码，解码期间暂停循环 GC，耗时记到容器的解码预算上

    单线程串行，所以无论多少容器并发，同一时刻只有一个解码在持 GIL；暂停 GC 之后单次解码持 GIL 约 15–35 ms
    （1 MiB 的最坏形态；容器密集的形态在解析之前就被结构预估拒掉，不解析）。等待解码的请求全进程最多
    :func:`max_pending_decodes` 个，超出立即拒绝。名额在解码真正结束时才释放（调用方被取消了，已经交出去的解码照样占着名额）。

    Raises:
        RpcCodecError: 不合法、含不支持的类型或超限（`RpcBusyError`：排队已满）
    """
    global _pending_decodes
    limit = max_pending_decodes()
    with _pending_lock:
        if _pending_decodes >= limit:
            raise RpcBusyError(f"RPC 请求被拒绝：宿主正在解码的请求已满（{limit} 个），请稍后重试", retry_after=0.1)
        _pending_decodes += 1
    try:
        future = _DECODE_EXECUTOR.submit(_decode_with_gc_paused, raw_body, container_key)
    except BaseException:
        with _pending_lock:
            _pending_decodes -= 1
        raise
    future.add_done_callback(_release_pending)
    outcome = await asyncio.wrap_future(future)
    if isinstance(outcome, str):
        raise RpcCodecError(outcome)
    return outcome


async def execute_rpc_method_detailed(method: Any, args: list[Any], kwargs: dict[str, Any]) -> Tuple[Any, str, str]:
    """执行方法，连同「异常类名」一起返回

    审计只记类名：异常原文里可能有上游返回、路径与凭据。返回给沙盒的内容仍是
    `(结果, 错误文本)` 两段，由 :func:`execute_rpc_method` 丢掉第三项，既有调用方
    的行为逐字不变。
    """
    try:
        if asyncio.iscoroutinefunction(method):
            result = await method(*args, **kwargs)
        else:
            result = method(*args, **kwargs)
        return result, "", ""
    except Exception as e:
        return None, str(e), type(e).__name__


async def execute_rpc_method(method: Any, args: list[Any], kwargs: dict[str, Any]) -> Tuple[Any, str]:
    result, error_message, _error_type = await execute_rpc_method_detailed(method, args, kwargs)
    return result, error_message
