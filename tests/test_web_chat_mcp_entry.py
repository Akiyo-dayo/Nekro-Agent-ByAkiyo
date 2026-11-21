"""Web Chat MCP 入口的鉴权：工作区令牌资格、平台方登录态、外接密钥

上游这里只要「任意 `Role.Admin` 登录态」，令牌则按用户名 `admin` 取账号。本分支的边界在
`na_app._resolve_mcp_user`：工作区令牌按「曾经不归平台方」的标记与**此刻**的归属判资格，
后台登录态必须是平台方账号。这里把这几条钉住。
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import pytest
from tortoise import Tortoise

from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.mcp import web_chat_auth as auth
from nekro_agent.services.mcp.web_chat_mcp import na_app
from nekro_agent.services.mcp.web_chat_mcp.na_app import AuthenticatedMcpApp
from nekro_agent.services.user.role import Role

TENANT_ID = 5


@pytest.fixture(autouse=True)
def token_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(auth, "_EXTERNAL_TOKEN_PATH", tmp_path / "web-chat-mcp-external-token.json")


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_user(
    *,
    user_id: int,
    username: str,
    tenant_id: Optional[int],
    tenant_role: TenantRole,
    perm_level: Role,
) -> DBUser:
    return await DBUser.create(
        id=user_id,
        username=username,
        password="",
        adapter_key="",
        platform_userid=str(user_id),
        perm_level=perm_level,
        login_time=_now(),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


async def _seed_platform_admin() -> DBUser:
    return await _make_user(
        user_id=1,
        username="admin",
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER,
        perm_level=Role.Admin,
    )


async def _make_workspace(*, workspace_id: int, name: str, tenant_id: Optional[int]) -> DBWorkspace:
    """工作区归属从绑定频道推出来：频道带 instance_key，实例行上才有 tenant_id"""
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    workspace = await DBWorkspace.create(id=workspace_id, name=name, metadata={})
    if tenant_id is None:
        return workspace
    await DBAdapterInstance.create(
        adapter_key="wechat_ilink_multi",
        instance_key=f"inst_{workspace_id}",
        name=f"实例 {workspace_id}",
        tenant_id=tenant_id,
    )
    await DBChatChannel.create(
        adapter_key="wechat_ilink_multi",
        instance_key=f"inst_{workspace_id}",
        channel_id=f"group_{workspace_id}",
        channel_name="租户频道",
        chat_key=f"wechat_ilink_multi-inst_{workspace_id}-group_{workspace_id}",
        channel_type=ChatType.GROUP.value,
        workspace_id=workspace_id,
        is_active=True,
        observe_mode=False,
        data="{}",
    )
    return workspace


async def _make_shared_workspace(*, workspace_id: int, name: str) -> DBWorkspace:
    """平台方频道与租户频道同时在同一个工作区上（归属不唯一）"""
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    workspace = await DBWorkspace.create(id=workspace_id, name=name, metadata={})
    for instance_key, tenant_id, suffix in (
        (f"platform_{workspace_id}", None, "platform"),
        (f"tenant_{workspace_id}", TENANT_ID, "tenant"),
    ):
        await DBAdapterInstance.create(
            adapter_key="wechat_ilink_multi",
            instance_key=instance_key,
            name=f"实例 {instance_key}",
            tenant_id=tenant_id,
        )
        await DBChatChannel.create(
            adapter_key="wechat_ilink_multi",
            instance_key=instance_key,
            channel_id=f"group_{suffix}_{workspace_id}",
            channel_name="频道",
            chat_key=f"wechat_ilink_multi-{instance_key}-group_{suffix}_{workspace_id}",
            channel_type=ChatType.GROUP.value,
            workspace_id=workspace_id,
            is_active=True,
            observe_mode=False,
            data="{}",
        )
    return workspace


def _request(*, token: str = "", header: str = "") -> Any:
    from starlette.requests import Request

    headers = [(b"authorization", f"Bearer {header}".encode())] if header else []
    query = f"token={token}".encode() if token else b""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": query,
            "headers": headers,
        },
    )


# ── 工作区令牌：按「标记 + 当前归属」判 ──────────────────────────────────────
# 归属是从绑定频道现场推出来的；只判此刻拦不住「先拿到令牌、再解绑」，所以还要看历史标记。


@pytest.mark.parametrize("passed_as", ["header", "query"])
async def test_platform_workspace_token_is_accepted(db, passed_as: str) -> None:  # noqa: ARG001
    """没绑任何频道的工作区算平台方：令牌以平台方 `admin` 的身份放行"""
    await _seed_platform_admin()
    await _make_workspace(workspace_id=100, name="平台工作区", tenant_id=None)
    token = auth.build_workspace_web_chat_mcp_token(100)

    request = _request(header=token) if passed_as == "header" else _request(token=token)
    user = await na_app._resolve_mcp_user(request)

    assert user.username == "admin"


@pytest.mark.parametrize(
    ("workspace_id", "name", "shared"),
    [(101, "租户工作区", False), (102, "共用工作区", True)],
)
async def test_tenant_owned_or_shared_workspace_token_is_403(
    db,  # noqa: ARG001
    workspace_id: int,
    name: str,
    shared: bool,
) -> None:
    from nekro_agent.schemas.errors import PermissionDeniedError

    await _seed_platform_admin()
    if shared:
        await _make_shared_workspace(workspace_id=workspace_id, name=name)
    else:
        await _make_workspace(workspace_id=workspace_id, name=name, tenant_id=TENANT_ID)

    with pytest.raises(PermissionDeniedError):
        await na_app._resolve_mcp_user(
            _request(header=auth.build_workspace_web_chat_mcp_token(workspace_id)),
        )


async def test_tainted_workspace_token_is_403_even_if_platform_owned(db) -> None:  # noqa: ARG001
    """标记不会因为工作区重新算成平台方而消失"""
    from nekro_agent.schemas.errors import PermissionDeniedError
    from nekro_agent.services.mcp.web_chat_workspace_taint import taint_workspaces

    await _seed_platform_admin()
    await _make_workspace(workspace_id=103, name="被标记的工作区", tenant_id=None)
    taint_workspaces([103], reason="用例")

    with pytest.raises(PermissionDeniedError):
        await na_app._resolve_mcp_user(
            _request(header=auth.build_workspace_web_chat_mcp_token(103)),
        )


async def test_workspace_token_for_missing_workspace_is_401(db) -> None:  # noqa: ARG001
    """MAC 合法但工作区不存在：401，调用方据此换一张令牌"""
    from nekro_agent.schemas.errors import UnauthorizedError

    await _seed_platform_admin()

    with pytest.raises(UnauthorizedError):
        await na_app._resolve_mcp_user(
            _request(header=auth.build_workspace_web_chat_mcp_token(9999)),
        )


async def test_workspace_token_signed_by_another_process_is_401(db) -> None:  # noqa: ARG001
    """旧进程密钥签出来的令牌验不过"""
    from nekro_agent.schemas.errors import UnauthorizedError

    await _seed_platform_admin()
    await _make_workspace(workspace_id=104, name="平台工作区", tenant_id=None)
    stale = auth.build_workspace_web_chat_mcp_token(104)
    auth.rotate_web_chat_mcp_secret()

    with pytest.raises(UnauthorizedError):
        await na_app._resolve_mcp_user(_request(header=stale))


async def test_workspace_token_does_not_fall_back_to_jwt(db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    """`nwc1.` 开头的 Bearer 验不过就 401，不再交给 JWT 解析"""
    from nekro_agent.schemas.errors import UnauthorizedError

    async def must_not_parse_jwt(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("工作区令牌不该走到 JWT 解析")

    def must_not_query(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("伪造的工作区令牌不该触发查库")

    monkeypatch.setattr(na_app, "get_current_user", must_not_parse_jwt)
    monkeypatch.setattr(DBWorkspace, "get_or_none", must_not_query)
    monkeypatch.setattr(DBChatChannel, "filter", must_not_query)

    with pytest.raises(UnauthorizedError):
        await na_app._resolve_mcp_user(_request(header="nwc1.1." + "0" * 64))


async def test_platform_admin_row_must_be_platform_owned(db) -> None:  # noqa: ARG001
    """`admin` 这一行若是租户成员，外接令牌不能借它提权"""
    from nekro_agent.schemas.errors import UnauthorizedError

    await _make_user(
        user_id=2,
        username="admin",
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        perm_level=Role.Admin,
    )
    auth.save_external_web_chat_mcp_token(enabled=True, token="manual-external-token-123456")

    with pytest.raises(UnauthorizedError):
        await na_app._resolve_mcp_user(_request(header="manual-external-token-123456"))


async def test_platform_admin_row_missing_is_401(db) -> None:  # noqa: ARG001
    from nekro_agent.schemas.errors import UnauthorizedError

    auth.save_external_web_chat_mcp_token(enabled=True, token="manual-external-token-123456")

    with pytest.raises(UnauthorizedError):
        await na_app._resolve_mcp_user(_request(header="manual-external-token-123456"))


# ── 外接固定密钥 ──────────────────────────────────────────────────────────────


async def test_external_token_is_accepted_when_enabled(db) -> None:  # noqa: ARG001
    await _seed_platform_admin()
    auth.save_external_web_chat_mcp_token(enabled=True, token="manual-external-token-123456")

    user = await na_app._resolve_mcp_user(_request(header="manual-external-token-123456"))

    assert user.username == "admin"


async def test_external_token_is_rejected_when_disabled(db) -> None:  # noqa: ARG001
    from nekro_agent.schemas.errors import UnauthorizedError

    await _seed_platform_admin()
    auth.save_external_web_chat_mcp_token(enabled=True, token="manual-external-token-123456")
    auth.save_external_web_chat_mcp_token(enabled=False)

    with pytest.raises(UnauthorizedError):
        await na_app._resolve_mcp_user(_request(header="manual-external-token-123456"))


# ── 后台登录态 ────────────────────────────────────────────────────────────────


async def test_platform_owner_jwt_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = DBUser(
        username="owner",
        password="",
        adapter_key="",
        platform_userid="9",
        perm_level=Role.Super,
        login_time=_now(),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )

    async def fake_get_current_user(_request: Any) -> DBUser:
        return owner

    async def fake_active(user: DBUser) -> DBUser:
        return user

    monkeypatch.setattr(na_app, "get_current_user", fake_get_current_user)
    monkeypatch.setattr(na_app, "get_current_active_user", fake_active)

    user = await na_app._resolve_mcp_user(_request(header="some-jwt"))

    assert user is owner


async def test_platform_admin_jwt_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = DBUser(
        username="ops",
        password="",
        adapter_key="",
        platform_userid="10",
        perm_level=Role.Admin,
        login_time=_now(),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )

    async def fake_get_current_user(_request: Any) -> DBUser:
        return admin

    async def fake_active(user: DBUser) -> DBUser:
        return user

    monkeypatch.setattr(na_app, "get_current_user", fake_get_current_user)
    monkeypatch.setattr(na_app, "get_current_active_user", fake_active)

    assert await na_app._resolve_mcp_user(_request(header="some-jwt")) is admin


async def test_tenant_admin_jwt_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游只判 `Role.Admin`，而本分支的租户管理员正好就是它"""
    from nekro_agent.schemas.errors import PermissionDeniedError

    tenant_admin = DBUser(
        username="tenant-admin",
        password="",
        adapter_key="",
        platform_userid="11",
        perm_level=Role.Admin,
        login_time=_now(),
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )

    async def fake_get_current_user(_request: Any) -> DBUser:
        return tenant_admin

    async def fake_active(user: DBUser) -> DBUser:
        return user

    monkeypatch.setattr(na_app, "get_current_user", fake_get_current_user)
    monkeypatch.setattr(na_app, "get_current_active_user", fake_active)

    with pytest.raises(PermissionDeniedError):
        await na_app._resolve_mcp_user(_request(header="some-jwt"))


async def test_missing_credential_is_401() -> None:
    from nekro_agent.schemas.errors import UnauthorizedError

    with pytest.raises(UnauthorizedError):
        await na_app._resolve_mcp_user(_request())


# ── ASGI 包装器 ───────────────────────────────────────────────────────────────


async def test_asgi_wrapper_blocks_unauthenticated_call() -> None:
    async def inner(_scope: dict, _receive: Any, _send: Any) -> None:
        raise AssertionError("未通过鉴权的请求不该到达 MCP 应用")

    transport = httpx.ASGITransport(app=AuthenticatedMcpApp(inner))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/mcp")

    assert response.status_code == 401
    assert response.json()["error"] == "UnauthorizedError"


EXTERNAL_TOKEN = "manual-external-token-123456"


async def test_asgi_wrapper_passes_eligible_workspace_token(db) -> None:  # noqa: ARG001
    """合格工作区的令牌通过入口，身份是平台方 `admin`"""
    from nekro_agent.services.mcp.web_chat_mcp.context import get_current_user

    await _seed_platform_admin()
    await _make_workspace(workspace_id=107, name="平台工作区", tenant_id=None)

    seen: dict[str, str] = {}

    async def inner(_scope: dict, _receive: Any, send: Any) -> None:
        seen["username"] = get_current_user().username
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    transport = httpx.ASGITransport(app=AuthenticatedMcpApp(inner))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/mcp",
            headers={"Authorization": f"Bearer {auth.build_workspace_web_chat_mcp_token(107)}"},
        )

    assert response.status_code == 204
    assert seen["username"] == "admin"


async def test_asgi_wrapper_returns_403_for_tenant_workspace_token(db) -> None:  # noqa: ARG001
    """租户独占工作区的令牌被拒：403，不是 401"""
    await _seed_platform_admin()
    await _make_workspace(workspace_id=108, name="租户工作区", tenant_id=TENANT_ID)

    async def inner(_scope: dict, _receive: Any, _send: Any) -> None:
        raise AssertionError("被拒的请求不该到达 MCP 应用")

    transport = httpx.ASGITransport(app=AuthenticatedMcpApp(inner))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/mcp",
            headers={"Authorization": f"Bearer {auth.build_workspace_web_chat_mcp_token(108)}"},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "PermissionDeniedError"


async def test_asgi_wrapper_passes_authenticated_call_and_sets_identity(
    db,  # noqa: ARG001
) -> None:
    from nekro_agent.services.mcp.web_chat_mcp.context import get_current_user

    await _seed_platform_admin()
    auth.save_external_web_chat_mcp_token(enabled=True, token=EXTERNAL_TOKEN)

    seen: dict[str, str] = {}

    async def inner(_scope: dict, _receive: Any, send: Any) -> None:
        seen["username"] = get_current_user().username
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    transport = httpx.ASGITransport(app=AuthenticatedMcpApp(inner))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/mcp", headers={"Authorization": f"Bearer {EXTERNAL_TOKEN}"})

    assert response.status_code == 204
    assert seen["username"] == "admin"


async def test_asgi_wrapper_resets_identity_after_request(db) -> None:  # noqa: ARG001
    """ContextVar 不 reset，下一个请求就会继承上一个请求的身份"""
    from nekro_agent.schemas.errors import UnauthorizedError
    from nekro_agent.services.mcp.web_chat_mcp.context import get_current_user

    await _seed_platform_admin()
    auth.save_external_web_chat_mcp_token(enabled=True, token=EXTERNAL_TOKEN)

    async def inner(_scope: dict, _receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    transport = httpx.ASGITransport(app=AuthenticatedMcpApp(inner))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/mcp", headers={"Authorization": f"Bearer {EXTERNAL_TOKEN}"})

    assert response.status_code == 204
    with pytest.raises(UnauthorizedError):
        get_current_user()


async def test_asgi_wrapper_starts_session_manager_before_inner_app(db) -> None:  # noqa: ARG001
    await _seed_platform_admin()
    auth.save_external_web_chat_mcp_token(enabled=True, token=EXTERNAL_TOKEN)

    class FakeSessionManager:
        started = False

        @asynccontextmanager
        async def run(self):
            self.started = True
            yield

    session_manager = FakeSessionManager()

    async def inner(_scope: dict, _receive: Any, send: Any) -> None:
        assert session_manager.started is True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    transport = httpx.ASGITransport(app=AuthenticatedMcpApp(inner, session_manager))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/mcp", headers={"Authorization": f"Bearer {EXTERNAL_TOKEN}"})

    assert response.status_code == 204
    assert session_manager.started is True


# ── 复核补充：非 HTTP 连接 ─────────────────────────────────────────────────────


async def test_websocket_scope_is_closed_without_reaching_the_app() -> None:
    async def inner(_scope: dict, _receive: Any, _send: Any) -> None:
        raise AssertionError("websocket 不经鉴权不该到达 MCP 应用")

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "websocket.connect"}

    await AuthenticatedMcpApp(inner)({"type": "websocket", "path": "/mcp", "headers": []}, receive, send)

    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_lifespan_scope_still_reaches_the_app() -> None:
    seen: list[str] = []

    async def inner(scope: dict, _receive: Any, _send: Any) -> None:
        seen.append(scope["type"])

    async def receive() -> dict:
        return {"type": "lifespan.startup"}

    async def send(_message: dict) -> None:
        return None

    await AuthenticatedMcpApp(inner)({"type": "lifespan"}, receive, send)

    assert seen == ["lifespan"]
