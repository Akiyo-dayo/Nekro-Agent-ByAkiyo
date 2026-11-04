"""网页聊天整块能力只给平台方

`web` 适配器的每个端点都作用于整个部署（会话列表、任意会话的历史、上传目录、外接密钥），
没有"这一条归哪个租户"的概念。所以按**能力边界**处理：租户身份（含租户管理员）一律 403，
而不是用 404 掩盖资源存在性 —— 后者是资源归属的语义，这里不适用。

另外钉住网页会话的频道行 `instance_key` 为空：它不会落进任何租户的实例集合，
因此租户的频道列表 / 详情 / 实时流按既有作用域规则看不到它。
"""

from datetime import datetime, timezone
from typing import List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nekro_agent.adapters.web import routers as web_routers
from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1

# (method, path, kwargs)。请求体给最小合法值，租户身份应当在读 body 之前就被挡下
WEB_CALLS: List[Tuple[str, str, dict]] = [
    ("get", "/api/adapters/web/mcp-auth", {}),
    ("put", "/api/adapters/web/mcp-auth/external", {"json": {"enabled": False}}),
    ("post", "/api/adapters/web/mcp-auth/external/generate", {}),
    ("delete", "/api/adapters/web/mcp-auth/external", {}),
    ("get", "/api/adapters/web/sessions", {}),
    ("post", "/api/adapters/web/sessions", {"json": {"name": "会话"}}),
    ("put", "/api/adapters/web/sessions/web-session_x", {"json": {"name": "改名"}}),
    ("delete", "/api/adapters/web/sessions/web-session_x", {"params": {"confirm": True}}),
    ("post", "/api/adapters/web/sessions/web-session_x/messages", {"json": {"content": "hi"}}),
    (
        "post",
        "/api/adapters/web/sessions/web-session_x/messages/upload",
        {"files": {"file": ("a.txt", b"data", "text/plain")}},
    ),
]


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


def tenant_user() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)


def platform_admin() -> DBUser:
    """平台方仍然是 `Role.Admin`：放行靠平台方身份，不是更高等级的角色"""
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


class _FakeAdapter:
    class config:  # noqa: N801 - 与真实适配器同形
        MESSAGE_MAX_LENGTH = 8000
        FILE_UPLOAD_MAX_SIZE_MB = 10


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # 适配器实例只在放行之后的路径上才会被取，这里给个最小替身
    monkeypatch.setattr(web_routers, "_get_adapter", lambda: _FakeAdapter())

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(web_routers.router, prefix="/api/adapters/web")
    test_client = TestClient(app, raise_server_exceptions=False)

    def login_as(user: DBUser) -> None:
        app.dependency_overrides[get_current_active_user] = lambda: user

    test_client.login_as = login_as  # type: ignore[attr-defined]
    return test_client


class TestTenantIsRefused:
    @pytest.mark.parametrize(("method", "path", "kwargs"), WEB_CALLS)
    def test_tenant_admin_gets_403(
        self,
        client: TestClient,
        method: str,
        path: str,
        kwargs: dict,
    ) -> None:
        client.login_as(tenant_admin())  # type: ignore[attr-defined]

        response = getattr(client, method)(path, **kwargs)

        assert response.status_code == 403, response.text

    @pytest.mark.parametrize(("method", "path", "kwargs"), WEB_CALLS)
    def test_tenant_user_gets_403(
        self,
        client: TestClient,
        method: str,
        path: str,
        kwargs: dict,
    ) -> None:
        client.login_as(tenant_user())  # type: ignore[attr-defined]

        response = getattr(client, method)(path, **kwargs)

        assert response.status_code == 403, response.text


class TestPlatformOwnerIsUnaffected:
    @pytest.mark.parametrize(("method", "path", "kwargs"), WEB_CALLS)
    def test_platform_owner_is_not_refused_by_the_boundary(
        self,
        client: TestClient,
        method: str,
        path: str,
        kwargs: dict,
    ) -> None:
        """平台方不被这道边界拒绝

        具体状态码取决于后续业务（未建库时可能是 400/500），这里只钉住"不是 403"，
        权限边界与业务失败必须可区分。
        """
        client.login_as(platform_admin())  # type: ignore[attr-defined]

        response = getattr(client, method)(path, **kwargs)

        assert response.status_code != 403, response.text


def test_every_web_endpoint_is_registered_in_the_contract() -> None:
    """端点清单与路由契约登记表必须一致：新加端点不登记就会红"""
    from tests.test_route_permission_contract import ADAPTER_PLATFORM_ONLY, _scan_adapter_routers

    scanned = {key for key in _scan_adapter_routers() if key[0] == "web"}
    registered = {key for key in ADAPTER_PLATFORM_ONLY if key[0] == "web"}

    assert scanned == registered
    assert len(scanned) == len(WEB_CALLS)


def test_web_sessions_have_no_instance_key() -> None:
    """网页会话是单实例的：`instance_key` 显式留空

    这是"租户看不到网页会话"的根据 —— 租户的可见性按 `(adapter_key, instance_key)` 成对判定，
    空 instance_key 不落进任何租户的实例集合。
    """
    import inspect

    source = inspect.getsource(web_routers.create_web_session)

    assert 'instance_key=""' in source
