"""单账号适配器的维护端点只给平台方

`qqbot_openclaw` 的五个维护动作与 `wechat_openilink` 的登录态都作用于整个部署：
租户管理员调得动，就等于能重启平台方的网关、清空它的会话，或者读到正在登录的二维码链接。
这里钉住租户身份一律 403，平台方行为不变。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nekro_agent.adapters.qqbot_openclaw.routers import create_router as create_qqbot_router
from nekro_agent.adapters.wechat_openilink.routers import create_router as create_openilink_router
from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.now(timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def tenant_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_admin() -> DBUser:
    """平台方仍然是 `Role.Admin`：放行靠的是平台方身份，不是更高等级的角色"""
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


class _Recorder:
    def __init__(self) -> None:
        self.calls: List[str] = []

    async def status(self) -> Dict[str, Any]:
        self.calls.append("status")
        return {
            "configured": True,
            "running": True,
            "connected": True,
            "app_id": "app",
            "session_id": "s1",
            "ref_index_entries": 0,
        }

    async def action(self, name: str) -> Dict[str, Any]:
        self.calls.append(name)
        return {"success": True, "message": "ok"}

    async def restart(self) -> Dict[str, Any]:
        return await self.action("restart")

    async def clear_ref_index(self) -> Dict[str, Any]:
        return await self.action("clear_ref_index")

    async def clear_session(self) -> Dict[str, Any]:
        return await self.action("clear_session")

    async def test_token(self) -> Dict[str, Any]:
        return await self.action("test_token")

    def login_status(self) -> Dict[str, Any]:
        self.calls.append("login_status")
        return {"state": "idle", "logged_in": False}


QQBOT_CALLS: List[Tuple[str, str]] = [
    ("get", "/api/adapters/qqbot_openclaw/maintenance/status"),
    ("post", "/api/adapters/qqbot_openclaw/maintenance/restart"),
    ("post", "/api/adapters/qqbot_openclaw/maintenance/clear-ref-index"),
    ("post", "/api/adapters/qqbot_openclaw/maintenance/clear-session"),
    ("post", "/api/adapters/qqbot_openclaw/maintenance/test-token"),
]
OPENILINK_CALL: Tuple[str, str] = ("get", "/api/adapters/wechat_openilink/login/status")


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def client(recorder: _Recorder) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(
        create_qqbot_router(
            get_status=recorder.status,
            restart_gateway=recorder.restart,
            clear_ref_index=recorder.clear_ref_index,
            clear_session=recorder.clear_session,
            test_token=recorder.test_token,
        ),
        prefix="/api/adapters/qqbot_openclaw",
    )
    app.include_router(
        create_openilink_router(recorder.login_status),
        prefix="/api/adapters/wechat_openilink",
    )
    test_client = TestClient(app, raise_server_exceptions=False)

    def login_as(user: DBUser) -> None:
        app.dependency_overrides[get_current_active_user] = lambda: user

    test_client.login_as = login_as  # type: ignore[attr-defined]
    return test_client


class TestTenantAdminIsRefused:
    @pytest.mark.parametrize(("method", "path"), [*QQBOT_CALLS, OPENILINK_CALL])
    def test_every_endpoint_returns_403_and_does_nothing(
        self,
        client: TestClient,
        recorder: _Recorder,
        method: str,
        path: str,
    ) -> None:
        client.login_as(tenant_admin())  # type: ignore[attr-defined]

        response = getattr(client, method)(path)

        assert response.status_code == 403, response.text
        assert recorder.calls == []


class TestPlatformOwnerIsUnaffected:
    @pytest.mark.parametrize(("method", "path"), [*QQBOT_CALLS, OPENILINK_CALL])
    def test_every_endpoint_still_works(
        self,
        client: TestClient,
        recorder: _Recorder,
        method: str,
        path: str,
    ) -> None:
        client.login_as(platform_admin())  # type: ignore[attr-defined]

        response = getattr(client, method)(path)

        assert response.status_code == 200, response.text
        assert len(recorder.calls) == 1
