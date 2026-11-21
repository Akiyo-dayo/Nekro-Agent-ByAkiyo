"""MCP 服务器活跑校验

提供"按一次按钮"的 MCP `initialize` 握手能力，对三种传输类型都给出结构化结果。
- HTTP / SSE：直接从 NA 主机使用 `mcp` SDK 走握手；URL 可达性与 CC 容器同网段一致。
- stdio：通过 aiodocker 在 CC 沙盒容器内 exec 子进程，走 newline-delimited JSON-RPC，
        因为 stdio MCP server (npx/uvx 等) 只在容器内可用。

## 出站防护（设计 §3.3）

这是「NA 服务器替用户发起出站连接」的入口之一，判据与模型组 `base_url` 同一套：

- `restrict_to_public_network=True`（租户身份）：连接前校验 URL，拒绝回环 / 内网 / 链路本地
  / IPv6 内嵌 IPv4；mcp SDK 默认 `follow_redirects=True`，这里换成自建 client 工厂，
  **不跟随跳转**，且每个请求发送前、每个响应收到后都再判一次（DNS 重绑定）。
  被拦时返回 `ok=false`、`error_kind="url_not_allowed"`，不发起连接。
- `restrict_to_public_network=False`（平台方）：不设限。平台方本来就是宿主所有者，
  把 MCP 服务放在内网是正常用法。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Literal, Optional

import aiodocker
import httpx
from pydantic import BaseModel

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.tools.common_util import (
    UnsafeDownloadUrlError,
    assert_tenant_connected_peer_safe,
    assert_tenant_endpoint_url_safe,
)
from nekro_agent.tools.tenant_egress import TenantEgressRejected, tenant_http_client

from .schemas import McpServerConfig, McpServerType

logger = get_sub_logger("mcp.validator")

ErrorKind = Literal[
    "schema",
    "spawn_failed",
    "handshake_timeout",
    "transport_error",
    "container_not_running",
    "invalid_response",
    "url_not_allowed",
]

# MCP 协议常量
_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "nekro-agent-validator", "version": "1.0"}


class McpValidationResult(BaseModel):
    """单次 MCP 验证结果"""

    ok: bool
    server_info: Optional[Dict[str, Any]] = None
    capabilities: Optional[Dict[str, Any]] = None
    tools: Optional[List[str]] = None
    latency_ms: float = 0.0
    error: Optional[str] = None
    error_kind: Optional[ErrorKind] = None


async def validate_server(
    workspace: Optional[DBWorkspace],
    server: McpServerConfig,
    timeout_seconds: float = 15.0,
    *,
    restrict_to_public_network: bool = True,
) -> McpValidationResult:
    """对一个 MCP server 配置发起活跑校验

    Args:
        workspace: 显式指定的工作区（stdio 只在这个容器里 exec）；`None` 表示平台方全局测试
        server: 待校验配置
        timeout_seconds: 握手超时
        restrict_to_public_network: 是否强制出站目标为公网。**默认 True**：忘了传参的调用方
            会落在安全的一侧，租户入口不可能因为少传一个关键字就绕过出站校验
    """
    start = time.perf_counter()
    try:
        # 只有平台方走进程内自检：租户提交一条名字 / URL 对得上的配置，不该借此读到网页聊天的
        # 启用状态与工具清单；受限时它就是一条普通的 http 配置，`host.docker.internal` 会被出站校验拦下
        if not restrict_to_public_network and is_web_chat_mcp_entry_payload(server):
            return await _validate_web_chat_in_process(server, start)
        if server.type == McpServerType.http:
            return await _validate_http(server, timeout_seconds, start, restrict_to_public_network)
        if server.type == McpServerType.sse:
            return await _validate_sse(server, timeout_seconds, start, restrict_to_public_network)
        if server.type == McpServerType.stdio:
            return await _validate_stdio(workspace, server, timeout_seconds, start)
    except Exception as e:  # noqa: BLE001
        if _find_unsafe_url_error(e) is not None:
            return _fail("url_not_allowed", str(_find_unsafe_url_error(e)), start)
        logger.exception(f"MCP 验证未捕获异常: {e}")
        return _fail("transport_error", str(e), start)
    return _fail("schema", f"未知 transport 类型: {server.type}", start)


def is_web_chat_mcp_entry_payload(server: McpServerConfig) -> bool:
    """这条配置是不是内置的网页聊天条目（按 id / 名字 / URL 认）"""
    from .web_chat_auth import is_web_chat_mcp_entry

    return is_web_chat_mcp_entry({"id": server.id, "name": server.name, "url": server.url or ""})


async def _validate_web_chat_in_process(server: McpServerConfig, start: float) -> McpValidationResult:
    """内置网页聊天条目不走网络：进程内确认适配器在、工具列得出来即算通过

    没有这一步，平台方点「测试」会从 NA 容器去连 `host.docker.internal:8021` 再连回自己，
    既需要 compose 加 `extra_hosts`，也让一次纯本地自检依赖 DNS 与网络。
    """
    from nekro_agent.adapters import ADAPTER_REGISTRY, adapter_load_errors, get_adapter

    from .web_chat_auth import WEB_CHAT_MCP_SERVER_NAME

    if "web" not in ADAPTER_REGISTRY:
        return _fail("transport_error", "Web Adapter 未注册，内置网页聊天 MCP 不可用", start)
    try:
        adapter = get_adapter("web")
        if not getattr(adapter, "is_enabled", True):
            return _fail("transport_error", "Web Adapter 未启用，内置网页聊天 MCP 不可用", start)
    except Exception as e:  # noqa: BLE001
        return _fail("transport_error", f"Web Adapter 不可用: {adapter_load_errors.get('web') or e}", start)

    try:
        from .web_chat_mcp.server import mcp

        tools = await mcp.list_tools()
    except Exception as e:  # noqa: BLE001
        return _fail("transport_error", f"读取内置网页聊天 MCP 工具失败: {e}", start)

    return McpValidationResult(
        ok=True,
        server_info={
            "name": WEB_CHAT_MCP_SERVER_NAME,
            "version": getattr(mcp, "name", None),
            "protocolVersion": _PROTOCOL_VERSION,
        },
        capabilities={"tools": {}},
        tools=[tool.name for tool in tools],
        latency_ms=_elapsed_ms(start),
    )


# ──────────────────────────────────────────────────────────────
# 出站防护
# ──────────────────────────────────────────────────────────────


def _peer_ip(response: httpx.Response) -> Optional[str]:
    stream = response.extensions.get("network_stream")
    address = stream.get_extra_info("server_addr") if stream is not None else None
    return address[0] if address else None


def _find_unsafe_url_error(exc: BaseException) -> Optional[BaseException]:
    """从异常链（含 ExceptionGroup）里找出出站防护抛的那个

    mcp SDK 会把传输层的异常包几层；`anyio` 的 `ExceptionGroup` 更是把并发任务里的异常
    收进 `.exceptions`。这里整条链都翻一遍，否则被拦的连接会被报成 `transport_error`，
    前端就没法显示「目标地址不允许」。建连接那一步（`tenant_egress`）拒掉的 `TenantEgressRejected` 也算：
    它的文案与请求钩子拒掉时同一句、不带地址，也不挂异常链。
    """
    seen: set[int] = set()
    stack: List[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (UnsafeDownloadUrlError, TenantEgressRejected)):
            return current
        for nested in getattr(current, "exceptions", ()) or ():
            if isinstance(nested, BaseException):
                stack.append(nested)
        for nested in (current.__cause__, current.__context__):
            if isinstance(nested, BaseException):
                stack.append(nested)
    return None


def _public_only_client_factory(default_timeout: float) -> Any:
    """造一个「不跟随跳转 + 逐请求逐响应校验」的 httpx client 工厂

    mcp SDK 的 `httpx_client_factory` 签名是 `(headers, timeout, auth) -> AsyncClient`。
    两处护栏：
    - `follow_redirects=False`：SDK 默认 `True`，一个 302 就能把请求带去内网，
      而 SDK 不会把跳转后的地址再交给我们校验；
    - request / response 钩子：request 钩子里判目标 URL（含 SSE 握手后服务端下发的 POST
      地址），response 钩子里判实际对端（DNS 重绑定）。

    Args:
        default_timeout: SDK 没给 timeout 时的兜底值（正常路径不会用到，SDK 每次都传）
    """

    async def _guard_request(request: httpx.Request) -> None:
        await assert_tenant_endpoint_url_safe(str(request.url))

    async def _guard_response(response: httpx.Response) -> None:
        assert_tenant_connected_peer_safe(_peer_ip(response))

    def factory(
        headers: Optional[Dict[str, Any]] = None,
        timeout: Optional[httpx.Timeout] = None,
        auth: Optional[httpx.Auth] = None,
    ) -> httpx.AsyncClient:
        # 形参名必须是 `headers` / `timeout` / `auth`：SDK 是按关键字调用的
        # （`streamablehttp_client` 与 `sse_client` 都写 `httpx_client_factory(headers=..., timeout=..., auth=...)`），
        # 改个名字不是风格问题，而是 `TypeError: unexpected keyword argument`。
        # 建连接那一步走 `tools/tenant_egress.py`：解析一次、拒内网、直连校验过的地址。
        # 两个钩子照留：请求钩子让被拒的地址在发请求前就报 `url_not_allowed`，响应钩子核对实际对端兜底。
        # 不走代理（`trust_env=False`）照旧：走代理时对端是代理，`_peer_ip` 判的就不是目标
        return tenant_http_client(
            trust_env=False,
            subject="MCP 服务器地址",
            headers=headers,
            timeout=timeout or httpx.Timeout(default_timeout),
            auth=auth,
            follow_redirects=False,
            event_hooks={"request": [_guard_request], "response": [_guard_response]},
        )

    return factory


async def _precheck_url(server: McpServerConfig, start: float) -> Optional[McpValidationResult]:
    """连接前先判 URL；被拦时**不创建连接**"""
    try:
        await assert_tenant_endpoint_url_safe(server.url or "")
    except UnsafeDownloadUrlError as e:
        logger.warning(f"MCP 校验拒绝出站目标: {e}")
        return _fail("url_not_allowed", str(e), start)
    return None


# ──────────────────────────────────────────────────────────────
# HTTP / SSE
# ──────────────────────────────────────────────────────────────


async def _validate_http(
    server: McpServerConfig,
    timeout_seconds: float,
    start: float,
    restrict_to_public_network: bool,
) -> McpValidationResult:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    if not server.url:
        return _fail("schema", "未配置 url", start)

    if restrict_to_public_network:
        blocked = await _precheck_url(server, start)
        if blocked is not None:
            return blocked

    try:
        async with asyncio.timeout(timeout_seconds):
            async with streamablehttp_client(
                server.url,
                headers=server.headers or None,
                timeout=timeout_seconds,
                **(
                    {"httpx_client_factory": _public_only_client_factory(timeout_seconds)}
                    if restrict_to_public_network
                    else {}
                ),
            ) as (read_stream, write_stream, _get_session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    init_result = await session.initialize()
                    tools_result = await session.list_tools()
                    return _ok(init_result, tools_result, start)
    except TimeoutError:
        return _fail("handshake_timeout", f"握手超时 ({timeout_seconds}s)", start)
    except Exception as e:  # noqa: BLE001
        blocked = _find_unsafe_url_error(e)
        if blocked is not None:
            return _fail("url_not_allowed", str(blocked), start)
        return _classify_transport_error(e, start)


async def _validate_sse(
    server: McpServerConfig,
    timeout_seconds: float,
    start: float,
    restrict_to_public_network: bool,
) -> McpValidationResult:
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client

    if not server.url:
        return _fail("schema", "未配置 url", start)

    if restrict_to_public_network:
        blocked = await _precheck_url(server, start)
        if blocked is not None:
            return blocked

    try:
        async with asyncio.timeout(timeout_seconds):
            async with sse_client(
                server.url,
                headers=server.headers or None,
                timeout=timeout_seconds,
                **(
                    {"httpx_client_factory": _public_only_client_factory(timeout_seconds)}
                    if restrict_to_public_network
                    else {}
                ),
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    init_result = await session.initialize()
                    tools_result = await session.list_tools()
                    return _ok(init_result, tools_result, start)
    except TimeoutError:
        return _fail("handshake_timeout", f"握手超时 ({timeout_seconds}s)", start)
    except Exception as e:  # noqa: BLE001
        blocked = _find_unsafe_url_error(e)
        if blocked is not None:
            return _fail("url_not_allowed", str(blocked), start)
        return _classify_transport_error(e, start)


# ──────────────────────────────────────────────────────────────
# stdio (via docker exec inside CC sandbox container)
# ──────────────────────────────────────────────────────────────


async def _validate_stdio(
    workspace: Optional[DBWorkspace],
    server: McpServerConfig,
    timeout_seconds: float,
    start: float,
) -> McpValidationResult:
    """在 CC 沙盒容器里 exec 一个 stdio MCP server

    只有**平台方的全局测试**（`workspace is None`）才允许「随便挑一个在跑的工作区」兜底。
    显式指定了工作区就必须在它自己的容器里跑：否则租户对自己还没起过容器的工作区点测试，
    `npx <任意包>` 就会在别人（甚至平台方）的容器里执行——那是一条跨租户的任意代码执行路径。
    """
    if workspace is None:
        workspace = await _pick_running_workspace()
        if workspace is None or not workspace.container_name:
            return _fail(
                "container_not_running",
                "stdio 验证需要一个正在运行的工作区容器，请先启动任一工作区",
                start,
            )
    elif not workspace.container_name:
        return _fail(
            "container_not_running",
            "该工作区容器未启动，stdio 验证只能在这个工作区自己的容器里执行",
            start,
        )

    cmd_parts = [server.command or "", *(server.args or [])]
    if not cmd_parts[0]:
        return _fail("schema", "stdio 类型缺少 command", start)

    docker = aiodocker.Docker()
    try:
        try:
            container = await docker.containers.get(workspace.container_name)
            info = await container.show()
            if not info.get("State", {}).get("Running"):
                return _fail(
                    "container_not_running",
                    "工作区容器未运行，请先启动",
                    start,
                )
        except Exception as e:  # noqa: BLE001
            return _fail("container_not_running", f"容器查询失败: {e}", start)

        try:
            exec_inst = await container.exec(
                cmd_parts,
                stdin=True,
                stdout=True,
                stderr=True,
                environment=dict(server.env or {}),
            )
        except Exception as e:  # noqa: BLE001
            return _fail("spawn_failed", f"创建 exec 失败: {e}", start)

        try:
            async with asyncio.timeout(timeout_seconds):
                async with exec_inst.start(detach=False) as stream:
                    return await _do_stdio_handshake(stream, start)
        except TimeoutError:
            return _fail("handshake_timeout", f"握手超时 ({timeout_seconds}s)", start)
        except Exception as e:  # noqa: BLE001
            return _classify_transport_error(e, start)
    finally:
        await docker.close()


async def _do_stdio_handshake(stream: Any, start: float) -> McpValidationResult:
    """在已建立的 docker exec stream 上跑 MCP 握手 + tools/list"""
    stderr_buf = bytearray()  # 收集 stderr 用于失败时回传诊断
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        },
    }
    await stream.write_in((json.dumps(init_msg) + "\n").encode("utf-8"))

    init_response = await _read_jsonrpc_response(stream, expected_id=1, stderr_buf=stderr_buf)
    if init_response is None:
        return _fail("invalid_response", _stderr_diag("未收到 initialize 响应", stderr_buf), start)
    if "error" in init_response:
        err = init_response["error"]
        return _fail("transport_error", f"initialize 错误: {err}", start)

    init_result = init_response.get("result", {})
    server_info = init_result.get("serverInfo", {})
    capabilities = init_result.get("capabilities", {})

    # 发送 initialized 通知
    initialized_notification = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    await stream.write_in((json.dumps(initialized_notification) + "\n").encode("utf-8"))

    # 列工具
    list_msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    await stream.write_in((json.dumps(list_msg) + "\n").encode("utf-8"))

    tools_response = await _read_jsonrpc_response(stream, expected_id=2, stderr_buf=stderr_buf)
    tool_names: Optional[List[str]] = None
    if tools_response and "result" in tools_response:
        tools = tools_response["result"].get("tools", [])
        tool_names = [t.get("name", "") for t in tools if t.get("name")]

    return McpValidationResult(
        ok=True,
        server_info={
            "name": server_info.get("name"),
            "version": server_info.get("version"),
            "protocolVersion": init_result.get("protocolVersion"),
        },
        capabilities=capabilities,
        tools=tool_names,
        latency_ms=_elapsed_ms(start),
    )


async def _read_jsonrpc_response(
    stream: Any,
    expected_id: int,
    buffer: Optional[bytearray] = None,
    stderr_buf: Optional[bytearray] = None,
) -> Optional[Dict[str, Any]]:
    """从 docker exec stdout 中读到指定 id 的 JSON-RPC response

    每条 MCP stdio 消息是一行 newline-delimited JSON。stderr 不参与协议，
    但会被收集到 stderr_buf（如提供）以便失败时回传诊断信息。
    """
    if buffer is None:
        buffer = bytearray()

    while True:
        msg = await stream.read_out()
        if msg is None:
            return None
        # msg.stream: 1 = stdout, 2 = stderr。stderr 仅记录日志，不参与协议。
        if getattr(msg, "stream", 1) == 2:
            try:
                if stderr_buf is not None:
                    stderr_buf.extend(msg.data)
                    # 防止 OOM：只保留尾部 4KB
                    if len(stderr_buf) > 4096:
                        del stderr_buf[: len(stderr_buf) - 4096]
                logger.debug(f"MCP stdio stderr: {bytes(msg.data).decode('utf-8', 'replace').strip()}")
            except Exception:  # noqa: BLE001
                pass
            continue
        buffer.extend(msg.data)
        while b"\n" in buffer:
            line, _, rest = buffer.partition(b"\n")
            buffer.clear()
            buffer.extend(rest)
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                logger.debug(f"非 JSON 输出，跳过: {text[:200]}")
                continue
            if not isinstance(payload, dict):
                continue
            # 通知 (无 id) 跳过；id 不匹配的响应也跳过
            if "id" not in payload:
                continue
            if payload.get("id") != expected_id:
                continue
            return payload


# ──────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────


def _ok(init_result: Any, tools_result: Any, start: float) -> McpValidationResult:
    server_info = getattr(init_result, "serverInfo", None)
    capabilities = getattr(init_result, "capabilities", None)
    tools = getattr(tools_result, "tools", None) or []
    return McpValidationResult(
        ok=True,
        server_info=(
            {
                "name": getattr(server_info, "name", None),
                "version": getattr(server_info, "version", None),
                "protocolVersion": getattr(init_result, "protocolVersion", None),
            }
            if server_info is not None
            else None
        ),
        capabilities=(
            capabilities.model_dump() if hasattr(capabilities, "model_dump") else None
        ),
        tools=[getattr(t, "name", "") for t in tools if getattr(t, "name", None)],
        latency_ms=_elapsed_ms(start),
    )


def _fail(kind: ErrorKind, message: str, start: float) -> McpValidationResult:
    return McpValidationResult(
        ok=False,
        error=message,
        error_kind=kind,
        latency_ms=_elapsed_ms(start),
    )


def _classify_transport_error(exc: BaseException, start: float) -> McpValidationResult:
    """把常见异常分类成 error_kind，便于前端给精准提示"""
    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    if "timeout" in low:
        return _fail("handshake_timeout", msg, start)
    if any(k in low for k in ("refused", "unreachable", "name or service", "dns", "resolve")):
        return _fail("transport_error", msg, start)
    return _fail("transport_error", msg, start)


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _stderr_diag(base_message: str, stderr_buf: bytearray) -> str:
    """把 stderr 尾巴拼到诊断消息里，方便前端 Tooltip 直接看到原因"""
    if not stderr_buf:
        return base_message + "（stderr 无输出，进程很可能立刻退出；常见原因：command/包名不存在、参数错误）"
    text = bytes(stderr_buf).decode("utf-8", "replace").strip()
    # 截短超长 stderr，仅保留尾部
    if len(text) > 1500:
        text = "…" + text[-1500:]
    return base_message + f"\n[stderr] {text}"


async def _pick_running_workspace() -> Optional[DBWorkspace]:
    """挑选任一 active 状态的工作区，用于在没有显式 workspace 时 exec stdio"""
    return await DBWorkspace.filter(status="active").exclude(container_name="").first()
