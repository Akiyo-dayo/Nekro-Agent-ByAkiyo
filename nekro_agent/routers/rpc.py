import json
import pickle
from dataclasses import replace

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
from nekro_agent.services.plugin.utils import get_sandbox_method_type
from nekro_agent.services.rpc_service import decode_rpc_request, execute_rpc_method
from nekro_agent.services.sandbox.credential_guard import (
    collect_platform_secrets,
    note_sandbox_callback,
    redact_sandbox_payload,
    redact_sandbox_text,
    resolve_sandbox_call_grant,
)
from nekro_agent.tools.untrusted_content import UNTRUSTED_RESULT_PLACEHOLDER, UntrustedToolResult

logger = get_sub_logger("rpc_bridge")
router = APIRouter(prefix="/ext", tags=["Tools"])


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
    3. 返回值与异常文本统一过一遍凭据脱敏，避免插件把平台密钥顺着异常带回沙盒。
    """
    # 先记账再鉴权：这个计数量的是「沙盒 -> NA 的包到得了」，不是「这次调用合法」。
    # 沙盒守卫的规则被外部改写时，回调会落进兜底 DROP 一个都到不了，
    # `runner.py` 正是靠「容器超时 + 全程零回调」把这类故障与模型写死循环区分开
    note_sandbox_callback(container_key)
    grant = resolve_sandbox_call_grant(container_key=container_key, token=x_rpc_token, from_chat_key=from_chat_key)
    if not grant:
        logger.warning(f"非法的沙盒 RPC 回调身份: container_key={container_key}")
        raise UnauthorizedError

    rpc_request: RPCRequest = decode_rpc_request(await data.body())

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

    # 沙盒方法的报错是模型可见的正常控制流（参数写错、上游 4xx），不计入插件失败计数，
    # 否则一个爱报错的方法会把整个插件拖进隔离。但"有没有把事件循环占死"必须看得见：
    # 这里跑的是宿主进程内的插件代码，不是沙盒容器里的模型代码。
    with observe_loop_blocking(f"沙盒方法 {rpc_request.method}"):
        result, error_message = await execute_rpc_method(method, args, kwargs)

    secret_values = collect_platform_secrets()
    if error_message:
        return Response(
            content=redact_sandbox_text(error_message, secret_values),
            media_type="application/octet-stream",
            headers={"Method-Type": method_type.value, "Run-Error": "True"},
        )

    if isinstance(result, UntrustedToolResult):
        # 外部内容不落 SYSTEM、不进沙盒，见 `UntrustedToolResult`。其他返回值逐字走下面的老路
        result = replace(
            result,
            body=redact_sandbox_text(result.body, secret_values),
            summary=redact_sandbox_text(result.summary, secret_values),
        )
        hold_untrusted_result(container_key, result)
        await message_service.push_tool_result(chat_key=from_chat_key, result=result)
        return Response(
            content=pickle.dumps(UNTRUSTED_RESULT_PLACEHOLDER),
            media_type="application/octet-stream",
            headers={"Method-Type": method_type.value, "Run-Error": "False"},
        )

    result = redact_sandbox_payload(result, secret_values)
    if method_type in [SandboxMethodType.AGENT, SandboxMethodType.BEHAVIOR]:
        await message_service.push_system_message(chat_key=from_chat_key, agent_messages=str(result))
    if method_type == SandboxMethodType.MULTIMODAL_AGENT:
        result = f"<AGENT_RESULT>{json.dumps(result, ensure_ascii=False)}</AGENT_RESULT>"
    return Response(
        content=pickle.dumps(result),
        media_type="application/octet-stream",
        headers={"Method-Type": method_type.value, "Run-Error": "False"},
    )
