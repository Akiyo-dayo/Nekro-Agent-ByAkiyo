from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nekro_agent.models.db_user import DBUser
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import require_platform_owner


class QQBotOpenClawStatus(BaseModel):
    configured: bool
    running: bool
    connected: bool
    app_id: str
    session_id: str | None = None
    last_seq: int | None = None
    self_user_id: str | None = None
    last_connected_at: float | None = None
    last_error: str | None = None
    ref_index_entries: int = 0
    onboarding_url: str = "https://q.qq.com/qqbot/openclaw/index.html"
    onboarding_qr_url: str = "https://q.qq.com/qqbot/openclaw/index.html"


class QQBotOpenClawActionResult(BaseModel):
    success: bool
    message: str
    detail: dict[str, Any] | None = None


def create_router(
    *,
    get_status: Callable[[], Awaitable[dict[str, Any]]],
    restart_gateway: Callable[[], Awaitable[dict[str, Any]]],
    clear_ref_index: Callable[[], Awaitable[dict[str, Any]]],
    clear_session: Callable[[], Awaitable[dict[str, Any]]],
    test_token: Callable[[], Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter(prefix="/maintenance", tags=["QQBot OpenClaw Maintenance"])

    # 五个端点都作用于整个部署（单账号适配器），租户管理员够得着就等于能重启平台方的网关
    @router.get("/status")
    @require_role(Role.User)
    async def status(
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> QQBotOpenClawStatus:
        return QQBotOpenClawStatus.model_validate(await get_status())

    @router.post("/restart")
    @require_role(Role.Admin)
    async def restart(
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> QQBotOpenClawActionResult:
        return QQBotOpenClawActionResult.model_validate(await restart_gateway())

    @router.post("/clear-ref-index")
    @require_role(Role.Admin)
    async def clear_refs(
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> QQBotOpenClawActionResult:
        return QQBotOpenClawActionResult.model_validate(await clear_ref_index())

    @router.post("/clear-session")
    @require_role(Role.Admin)
    async def session_clear(
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> QQBotOpenClawActionResult:
        return QQBotOpenClawActionResult.model_validate(await clear_session())

    @router.post("/test-token")
    @require_role(Role.Admin)
    async def token_test(
        _current_user: DBUser = Depends(get_current_active_user),
        _owner: DBUser = Depends(require_platform_owner()),
    ) -> QQBotOpenClawActionResult:
        return QQBotOpenClawActionResult.model_validate(await test_token())

    return router
