"""Web Chat MCP 的 ASGI 入口：先由 Nekro Agent 判身份，再交给 MCP SDK

上游在这里只判「任意 `Role.Admin` 登录态」，令牌则按用户名 `admin` 取账号。本分支的差别：

- 后台登录态（JWT）只认平台方：`is_platform_owner` 且至少 `Role.Admin`，租户身份 403；
- 外接固定令牌：验证通过后以平台方 `admin` 行事，且该账号必须是平台方账号；
- 工作区令牌（`nwc1.` 开头）：验签取回工作区，再按「曾经不归平台方」的标记与**此刻**的归属
  判资格（`services/mcp/web_chat_workspace_taint.py`）。工作区只要出现过非平台方归属就永远
  不发也不认它的令牌——归属是从绑定频道现场推出来的，只判此刻拦不住「先拿到令牌、再解绑」。

工作区令牌先判「是不是本服务签发的那一种」，判不出来才回落 JWT。`nwc1.` 开头的令牌不论
哪一支都不回落 JWT：伪造的 Bearer 不该因为 JWT 解析失败而走到「未授权」以外的分支。
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
    parse_workspace_web_chat_mcp_token,
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
            # 这一支不论验签、查库、判资格哪一步不过，都不回落 JWT
            workspace_id = parse_workspace_web_chat_mcp_token(token)
            if workspace_id is None:
                raise UnauthorizedError

            from nekro_agent.models.db_workspace import DBWorkspace
            from nekro_agent.services.mcp.web_chat_workspace_taint import is_workspace_web_chat_token_eligible

            workspace = await DBWorkspace.get_or_none(id=workspace_id)
            if workspace is None:
                raise UnauthorizedError
            if not await is_workspace_web_chat_token_eligible(workspace):
                logger.warning(f"拒绝工作区 {workspace_id} 的网页聊天 MCP 调用：它曾经或此刻不属平台方")
                raise PermissionDeniedError
            return await _platform_admin_user()
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
