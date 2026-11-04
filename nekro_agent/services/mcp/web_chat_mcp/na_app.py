"""Web Chat MCP 的 ASGI 入口：先由 Nekro Agent 判身份，再交给 MCP SDK

上游在这里只判「任意 `Role.Admin` 登录态」，令牌则按用户名 `admin` 取账号。本分支的差别
（设计 §3.2）：

- 后台登录态（JWT）只认平台方：`is_platform_owner` 且至少 `Role.Admin`，租户身份 403；
- 外接固定令牌：验证通过后以平台方 `admin` 行事，且该账号必须是平台方账号；
- **工作区令牌（`nwc1.` 开头）这一版一律 401**，不论签名对不对、工作区归谁。

工作区令牌关掉的原因：工作区归属是从绑定频道现场推出来的，没有频道就算平台方。平台方新建
工作区时写进 `.mcp.json` 的令牌，在租户频道绑上来以后留在了租户能读的地方；租户再把自己的频道
解绑，工作区重新算成平台方，那张令牌就能以平台方 `admin` 的身份调用。调用时判归属拦不住
「先拿到、再解绑」，要根治得给出现过非平台方归属的工作区打不会自动消失的标记（待做），
在那之前这条路不开。令牌派生与解析函数留在 `web_chat_auth`，只是入口不认。
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import AppError, PermissionDeniedError, UnauthorizedError
from nekro_agent.schemas.i18n import SupportedLang
from nekro_agent.services.mcp.web_chat_auth import (
    WORKSPACE_TOKEN_PREFIX,
    is_valid_external_web_chat_mcp_token,
)
from nekro_agent.services.user.deps import get_current_active_user, get_current_user
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.tenant_perm import is_platform_owner

from .context import reset_current_user, set_current_user
from .server import mcp

logger = get_sub_logger("web_chat_mcp.entry")


class AuthenticatedMcpApp:
    """在 MCP 之前跑一遍 NA 的鉴权，通过后把身份放进 ContextVar"""

    def __init__(self, app: ASGIApp, session_manager: Optional[Any] = None) -> None:
        self.app = app
        self.session_manager = session_manager
        self._startup_lock = asyncio.Lock()
        self._exit_stack: Optional[AsyncExitStack] = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # MCP 只走 HTTP；其余连接类型（websocket）不鉴权就转进去等于开了一个旁门，直接关掉
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        request = Request(scope, receive)
        try:
            user = await _resolve_mcp_user(request)
        except AppError as exc:
            await self._send_error(
                scope,
                receive,
                send,
                status_code=exc.http_status,
                message=exc.get_message(SupportedLang.ZH_CN),
            )
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Web Chat MCP 入口鉴权异常: {e}")
            await self._send_error(scope, receive, send, status_code=401, message="未授权访问")
            return

        token = set_current_user(user)
        try:
            await self._ensure_started()
            await self.app(scope, receive, send)
        finally:
            reset_current_user(token)

    async def _ensure_started(self) -> None:
        if self.session_manager is None or self._exit_stack is not None:
            return
        async with self._startup_lock:
            if self._exit_stack is not None:
                return
            exit_stack = AsyncExitStack()
            await exit_stack.enter_async_context(self.session_manager.run())
            self._exit_stack = exit_stack

    @staticmethod
    async def _send_error(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        message: str,
    ) -> None:
        await JSONResponse(
            {
                "error": "UnauthorizedError" if status_code == 401 else "PermissionDeniedError",
                "message": message,
            },
            status_code=status_code,
        )(scope, receive, send)


def create_web_chat_mcp_app() -> ASGIApp:
    app = mcp.streamable_http_app()
    return AuthenticatedMcpApp(app, mcp.session_manager)


async def _resolve_mcp_user(request: Request) -> DBUser:
    token = _extract_token(request)
    if token:
        if token.startswith(f"{WORKSPACE_TOKEN_PREFIX}."):
            # 不验签、不查归属、也不回落 JWT：这一版不认工作区令牌（理由见模块说明）
            logger.warning("拒绝工作区令牌调用网页聊天 MCP：这一版不开放工作区令牌")
            raise UnauthorizedError
        if is_valid_external_web_chat_mcp_token(token):
            return await _platform_admin_user()

    user = await get_current_active_user(await get_current_user(request))
    if not is_platform_owner(user) or user.perm_level < Role.Admin:
        raise PermissionDeniedError
    return user


async def _platform_admin_user() -> DBUser:
    """令牌通过后以平台方 `admin` 行事

    `admin` 这一行是后台账号（`adapter_key` 为空）。它必须确实是平台方账号：把 `admin`
    建成租户成员、或把 `tenant_role` 改成普通角色，都不该让一张令牌变成租户提权通道。
    """
    user = await DBUser.get_or_none(username="admin")
    if user is None or not is_platform_owner(user):
        raise UnauthorizedError
    return await get_current_active_user(user)


def _extract_token(request: Request) -> str:
    url_token = request.query_params.get("token")
    if url_token:
        return url_token.removeprefix("Bearer ").strip()
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header.removeprefix("Bearer ").strip()
    return ""
