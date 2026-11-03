import json
import pickle
import time
from dataclasses import replace
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, Header, Request, Response

from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.errors import NotFoundError, UnauthorizedError
from nekro_agent.schemas.rpc import RPCRequest
from nekro_agent.services.agent.session_token import hold_untrusted_result
from nekro_agent.services.message_service import message_service
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.isolation import observe_loop_blocking
from nekro_agent.services.plugin.schema import SandboxMethodType
from nekro_agent.services.plugin.scope import instance_scope
from nekro_agent.services.plugin.utils import get_sandbox_method_type
from nekro_agent.services.rpc_service import (
    RpcBusyError,
    RpcClientGone,
    decode_rpc_request_off_loop,
    execute_rpc_method,
    read_rpc_body,
    rpc_admission,
    rpc_execution,
)
from nekro_agent.services.sandbox.credential_guard import (
    collect_instance_plugin_secrets,
    collect_platform_secrets,
    note_sandbox_callback,
    redact_sandbox_payload,
    redact_sandbox_text,
    resolve_sandbox_call_grant,
)
from nekro_agent.tools.rpc_codec import RpcCodecError
from nekro_agent.tools.safe_pickle import UnsafePickleError, ensure_rpc_safe
from nekro_agent.tools.untrusted_content import (
    MAX_TOOL_RESULT_BODY_CHARS,
    TRUNCATION_MARK,
    UNTRUSTED_RESULT_PLACEHOLDER,
    UntrustedToolResult,
)

logger = get_sub_logger("rpc_bridge")
router = APIRouter(prefix="/ext", tags=["Tools"])


def _rpc_error(message: str, method_type: SandboxMethodType = SandboxMethodType.TOOL) -> Response:
    """以“方法报错”的形态回给沙盒：`api_caller` 会把正文打印出来，模型和插件作者都能看到"""
    return Response(
        content=message,
        media_type="application/octet-stream",
        headers={"Method-Type": method_type.value, "Run-Error": "True"},
    )


class _ThrottledLog:
    """日志节流：每个键每 `interval` 秒最多一行（其余只计数，下一行带上被略过的次数），全局每 `interval` 秒最多 `budget` 行

    日志 sink 是同步写的，沙盒一秒发几百个注定被拒的请求时，逐条记录本身就会把事件循环拖住（实测并发 72 个被拒请求：
    逐条记录时循环最长停顿 200–330 ms），也会把日志刷满。键可能由调用方任意编造（身份不对的请求里的 `container_key`），
    所以表有界、全局另有总量
    """

    def __init__(self, interval: float = 10.0, max_keys: int = 1024, budget: int = 20) -> None:
        self.interval = interval
        self.max_keys = max_keys
        self.budget = budget
        self._state: Dict[str, Tuple[float, int]] = {}
        self._window = (float("-inf"), 0)
        self.suppressed_total = 0

    def emit(self, key: str, message: str) -> None:
        now = time.monotonic()
        last, suppressed = self._state.get(key, (float("-inf"), 0))
        window_start, used = self._window
        if now - window_start >= self.interval:
            window_start, used = now, 0
        if now - last < self.interval or used >= self.budget:
            self._state[key] = (last, suppressed + 1)
            self.suppressed_total += 1
            if len(self._state) > self.max_keys:
                for stale in [k for k, (at, _n) in self._state.items() if now - at >= self.interval]:
                    self._state.pop(stale, None)
                if len(self._state) > self.max_keys:
                    self._state.clear()
            return
        self._window = (window_start, used + 1)
        self._state[key] = (now, 0)
        skipped = f"（此前 {suppressed} 次未逐条记录）" if suppressed else ""
        logger.warning(f"{message}{skipped}")


_rejection_log = _ThrottledLog()
_identity_log = _ThrottledLog()


def _shown_key(container_key: str) -> str:
    """身份不对的请求里，`container_key` 是调用方随便写的：截短、用 repr，免得往日志里写换行"""
    return repr(container_key[:80])


def _log_rejection(container_key: str, reason: str) -> None:
    _rejection_log.emit(container_key, f"拒绝 RPC 请求体: container_key={_shown_key(container_key)}, {reason}")


def _rpc_rejected(message: str, retry_after: Optional[float] = None) -> Response:
    """请求在执行之前就被拒绝（闸门、上限、格式）：仍是 `Run-Error`，另带 `Rpc-Rejected`，
    `api_caller` 据此抛出模型代码能捕获的异常（可以改小参数或稍后重试），而不是结束整段脚本。
    “忙”的拒绝再带 `Rpc-Busy` 与建议的重试间隔，`api_caller` 自动退避重试"""
    response = _rpc_error(message)
    response.headers["Rpc-Rejected"] = "True"
    if retry_after is not None:
        response.headers["Rpc-Busy"] = "True"
        response.headers["Rpc-Retry-After-Ms"] = str(max(1, int(retry_after * 1000)))
    return response


@router.post("/rpc_exec", summary="RPC 命令执行")
async def rpc_exec(
    container_key: str,
    from_chat_key: str,
    data: Request,
    x_rpc_token: str = Header(...),
) -> Response:
    """沙盒容器的回调入口

    这是沙盒里模型生成的代码唯一能触达服务端的通道，因此按「敌对调用方」对待：

    1. 身份必须来自本轮运行签发的调用许可（容器专属令牌 + 绑定的会话标识），
       全局 `RPC_SECRET_KEY` 不再是有效凭证；
    2. 方法必须在该容器本轮的下发白名单内，禁止调用未启用插件或别的作用域的方法；
    3. 返回值与异常文本统一过一遍凭据脱敏，避免插件把平台密钥顺着异常带回沙盒；
    4. 请求体在身份校验**之后**才读，读之前先过并发闸门（每个容器同时在读 / 解码的请求有上限，超出立即拒绝、不排队），
       读时就限长（`Content-Length` + 边读边截断），且只按 JSON + 固定类型标签解码（`tools/rpc_codec.py`）：
       方法名就在请求体里，方法白名单只能在解码之后查，所以解码这一步必须只能产出纯数据——不交出任何类或可调用对象、
       不对任何东西做 setattr，深度 / 元素数 / 容器数 / 长度 / 整数位数 / 哈希碰撞都有上限。解码在专用单线程里串行、
       期间暂停循环 GC（`services/rpc_service.py`），否则 `json.loads` 持着 GIL 建几十万个容器、触发整堆回收，事件循环会被冻住。
       超限、不合法或闸门拒绝一律以 `Run-Error` + `Rpc-Rejected` 回给沙盒，发生在拼日志、调用方法之前；
       `api_caller.py` 看到 `Rpc-Rejected` 抛出模型代码能捕获的异常，而不是结束整段脚本。
    5. 返回方向仍是 pickle（数据由宿主生成），先过 `tools/safe_pickle.py` 的白名单再发。
    """
    # 先记账再鉴权：这个计数量的是「沙盒 -> NA 的包到得了」，不是「这次调用合法」。
    # 沙盒守卫的规则被外部改写时，回调会落进兜底 DROP 一个都到不了，
    # `runner.py` 正是靠「容器超时 + 全程零回调」把这类故障与模型写死循环区分开
    note_sandbox_callback(container_key)
    grant = resolve_sandbox_call_grant(container_key=container_key, token=x_rpc_token, from_chat_key=from_chat_key)
    if not grant:
        _identity_log.emit(container_key, f"非法的沙盒 RPC 回调身份: container_key={_shown_key(container_key)}")
        raise UnauthorizedError

    try:
        with rpc_admission(container_key):
            rpc_request: RPCRequest = await decode_rpc_request_off_loop(await read_rpc_body(data), container_key)
    except RpcClientGone as e:
        # 对端已经断开：没人等这个回应，也不记日志（断开能被反复制造），只计数
        _rejection_log.suppressed_total += 1
        return _rpc_rejected(str(e))
    except RpcCodecError as e:
        # 闸门拒绝、请求体超限、不合法（解码层把 `ValidationError` 也换成了这个）或含不支持的类型：
        # 不执行，把原因当作方法错误回给沙盒（`Run-Error`），
        # 模型和插件作者才看得到“哪里、什么类型、超了哪个上限”，而不是一个没头没尾的 4xx。
        # 消息里只有位置、类型名与上限，不回显请求内容
        _log_rejection(container_key, str(e))
        return _rpc_rejected(str(e), e.retry_after if isinstance(e, RpcBusyError) else None)

    logger.info(f"收到 RPC 执行请求: {rpc_request.method}")

    if rpc_request.method not in grant.method_names:
        logger.warning(f"沙盒尝试调用未授权方法: {rpc_request.method} | container_key={container_key}")
        raise UnauthorizedError

    method = plugin_collector.get_method(rpc_request.method)
    if not method:
        raise NotFoundError(resource="RPC 方法")
    method_type: SandboxMethodType = get_sandbox_method_type(method=method)

    ctx: AgentCtx = await AgentCtx.create_by_chat_key(
        chat_key=from_chat_key,
        container_key=container_key,
    )
    args = [ctx, *rpc_request.args] if rpc_request.args else [ctx]
    kwargs = rpc_request.kwargs or {}

    # 设置实例上下文与出口脱敏必须用同一个标识，提成局部变量避免两处漂移
    instance_key = ctx.db_chat_channel.instance_key if ctx.db_chat_channel else ""

    # 沙盒方法的报错是模型可见的正常控制流（参数写错、上游 4xx），不计入插件失败计数，
    # 否则一个爱报错的方法会把整个插件拖进隔离。但"有没有把事件循环占死"必须看得见：
    # 这里跑的是宿主进程内的插件代码，不是沙盒容器里的模型代码。
    try:
        with (
            rpc_execution(container_key),
            observe_loop_blocking(f"沙盒方法 {rpc_request.method}"),
            # 回调是独立请求，入站时写入的实例 contextvar 传不过来：按频道所属实例显式设置，
            # 插件方法里读配置才会拿到该实例的覆盖
            instance_scope(instance_key),
        ):
            result, error_message = await execute_rpc_method(method, args, kwargs)
    except RpcBusyError as e:
        # 本容器执行中的方法已满：每个执行中的调用都持有解码出的参数，不设上限就能挂起几百个调用占满内存
        _log_rejection(container_key, str(e))
        return _rpc_rejected(str(e), e.retry_after)

    # 实例级覆盖不在 ConfigManager 里，必须按本次执行用的实例单独收集，否则会原样回沙盒
    secret_values = collect_platform_secrets() | collect_instance_plugin_secrets(instance_key)
    if error_message:
        return Response(
            content=redact_sandbox_text(error_message, secret_values),
            media_type="application/octet-stream",
            headers={"Method-Type": method_type.value, "Run-Error": "True"},
        )

    if isinstance(result, UntrustedToolResult):
        # 外部内容不落 SYSTEM、不进沙盒，见 `UntrustedToolResult`。其他返回值逐字走下面的老路
        body = redact_sandbox_text(result.body, secret_values)
        if len(body) > MAX_TOOL_RESULT_BODY_CHARS:
            # 先脱敏再兜底截断：上限是给任何插件的硬底线，见 `MAX_TOOL_RESULT_BODY_CHARS`
            body = body[:MAX_TOOL_RESULT_BODY_CHARS] + TRUNCATION_MARK
        result = replace(result, body=body, summary=redact_sandbox_text(result.summary, secret_values))
        hold_untrusted_result(container_key, result)
        await message_service.push_tool_result(chat_key=from_chat_key, result=result)
        return Response(
            content=pickle.dumps(UNTRUSTED_RESULT_PLACEHOLDER),
            media_type="application/octet-stream",
            headers={"Method-Type": method_type.value, "Run-Error": "False"},
        )

    result = redact_sandbox_payload(result, secret_values)
    # 先确认回得去沙盒，再落 SYSTEM：回不去的返回值在沙盒里就是一次报错，不该留下一条“方法返回了 xx”的系统消息。
    # MULTIMODAL 分支的 `json.dumps` 也在这里面：返回值不是 JSON 可序列化的，同样是插件作者要改的方法错误，不是 500
    try:
        sandbox_value = (
            f"<AGENT_RESULT>{json.dumps(result, ensure_ascii=False)}</AGENT_RESULT>"
            if method_type == SandboxMethodType.MULTIMODAL_AGENT
            else result
        )
        payload = pickle.dumps(sandbox_value)
        ensure_rpc_safe(payload)
    except Exception as e:  # noqa: BLE001 - 返回值既可能不可序列化，也可能含沙盒不允许的类型：都是插件作者要改的东西
        reason = str(e) if isinstance(e, UnsafePickleError) else f"返回值无法序列化（{type(e).__name__}）"
        logger.warning(f"RPC 方法 {rpc_request.method} 的返回值不能回传沙盒: {reason}")
        return _rpc_error(f"方法 {rpc_request.method} 的返回值不能传回沙盒：{reason}", method_type)
    if method_type in [SandboxMethodType.AGENT, SandboxMethodType.BEHAVIOR]:
        await message_service.push_system_message(chat_key=from_chat_key, agent_messages=str(result))
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={"Method-Type": method_type.value, "Run-Error": "False"},
    )
