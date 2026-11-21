"""插件路由的身份、角色、实例与票据校验

这一族端点是插件自己挂的，没有统一的 `@require_role`；错一步就是匿名可读，
或者租户拿自己的实例标识去摸别人的实例。本模块钉住四步判据与票据的失效条件。
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.plugin_route_ticket import (
    PluginRouteTicketRequest,
    issue_route_ticket,
)
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, UnauthorizedError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.plugin import route_auth, route_ticket
from nekro_agent.services.plugin.route_ticket import (
    TICKET_TTL_SECONDS,
    issue_plugin_route_ticket,
    verify_plugin_route_ticket,
)
from nekro_agent.services.user.role import Role

PLUGIN_KEY = "nekro.whiteboard"
TENANT_ID = 11
OTHER_TENANT_ID = 22
OWN_INSTANCE = "inst_a"
OTHER_INSTANCE = "inst_b"
PLATFORM_INSTANCE = "inst_platform"


def make_request(
    *,
    query: Optional[Dict[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Request:
    """构造一个只带查询串与请求头的最小请求"""
    raw_headers = [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in (headers or {}).items()]
    scope: Dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": urlencode(query or {}).encode("ascii"),
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


def make_user(
    *,
    tenant_id: Optional[int],
    tenant_role: TenantRole,
    perm_level: Role,
    username: str = "",
) -> DBUser:
    return DBUser(
        username=username or f"user-{tenant_role.value}-{tenant_id}-{perm_level.value}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def tenant_admin() -> DBUser:
    return make_user(tenant_id=TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for adapter_key, instance_key, tenant_id in (
            ("onebot_v11", OWN_INSTANCE, TENANT_ID),
            ("onebot_v11", OTHER_INSTANCE, OTHER_TENANT_ID),
            ("onebot_v11", PLATFORM_INSTANCE, None),
        ):
            await DBAdapterInstance.create(
                adapter_key=adapter_key,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def active_plugins(monkeypatch: pytest.MonkeyPatch) -> set[tuple[str, str]]:
    """把"插件在这个实例上生效"换成内存集合，避免测试依赖真实配置文件"""
    active: set[tuple[str, str]] = {(PLUGIN_KEY, OWN_INSTANCE), (PLUGIN_KEY, PLATFORM_INSTANCE), (PLUGIN_KEY, "")}
    monkeypatch.setattr(route_auth, "is_plugin_active", lambda key, instance: (key, instance or "") in active)
    return active


@pytest.fixture
def persist_user():
    """把用户落库，票据校验按 uid 回查时要用真实行"""

    async def _create(user: DBUser) -> DBUser:
        await user.save()
        return user

    return _create


def not_found_resource(exc: NotFoundError) -> Any:
    return exc.params.get("resource")


class TestIdentity:
    @pytest.mark.asyncio
    async def test_without_any_credential_is_401(self, db, active_plugins, monkeypatch: pytest.MonkeyPatch):
        async def _must_not_be_called(_connection: Any) -> DBUser:
            raise AssertionError("没有 Authorization 头时不该走后台令牌解析")

        monkeypatch.setattr(route_auth, "get_current_user", _must_not_be_called)
        with pytest.raises(UnauthorizedError) as exc:
            await route_auth.resolve_plugin_route_scope(make_request(), PLUGIN_KEY)
        assert exc.value.http_status == 401

    @pytest.mark.asyncio
    async def test_long_lived_url_token_is_not_accepted(self, db, active_plugins, monkeypatch: pytest.MonkeyPatch):
        async def _must_not_be_called(_connection: Any) -> DBUser:
            raise AssertionError("插件路由不允许 ?token= 长期令牌")

        monkeypatch.setattr(route_auth, "get_current_user", _must_not_be_called)
        request = make_request(query={"token": "a-long-lived-jwt"})
        with pytest.raises(UnauthorizedError):
            await route_auth.resolve_plugin_route_scope(request, PLUGIN_KEY)

    @pytest.mark.asyncio
    async def test_banned_user_with_bearer_is_403_like_active_check(
        self,
        db,
        active_plugins,
        monkeypatch: pytest.MonkeyPatch,
    ):
        user = tenant_admin()
        user.ban_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

        async def _fake_current_user(_connection: Any) -> DBUser:
            return user

        monkeypatch.setattr(route_auth, "get_current_user", _fake_current_user)
        request = make_request(headers={"Authorization": "Bearer whatever"})
        with pytest.raises(PermissionDeniedError):
            await route_auth.resolve_plugin_route_scope(request, PLUGIN_KEY)


class TestRoleAndInstance:
    @pytest.mark.asyncio
    async def test_role_below_admin_is_403(self, db, active_plugins, monkeypatch: pytest.MonkeyPatch):
        user = make_user(tenant_id=TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)

        async def _fake_current_user(_connection: Any) -> DBUser:
            return user

        monkeypatch.setattr(route_auth, "get_current_user", _fake_current_user)
        with pytest.raises(HTTPException) as exc:
            await route_auth.resolve_plugin_route_scope(
                make_request(headers={"Authorization": "Bearer whatever"}),
                PLUGIN_KEY,
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_tenant_without_instance_is_400(self, db, active_plugins, monkeypatch: pytest.MonkeyPatch):
        user = tenant_admin()

        async def _fake_current_user(_connection: Any) -> DBUser:
            return user

        monkeypatch.setattr(route_auth, "get_current_user", _fake_current_user)
        with pytest.raises(ValidationError):
            await route_auth.resolve_plugin_route_scope(
                make_request(headers={"Authorization": "Bearer whatever"}),
                PLUGIN_KEY,
            )

    @pytest.mark.asyncio
    async def test_foreign_and_missing_instance_are_indistinguishable(
        self,
        db,
        active_plugins,
        monkeypatch: pytest.MonkeyPatch,
    ):
        user = tenant_admin()

        async def _fake_current_user(_connection: Any) -> DBUser:
            return user

        monkeypatch.setattr(route_auth, "get_current_user", _fake_current_user)
        messages = []
        for instance_key in (OTHER_INSTANCE, "does_not_exist"):
            request = make_request(
                query={"adapter_key": "onebot_v11", "instance_key": instance_key},
                headers={"Authorization": "Bearer whatever"},
            )
            with pytest.raises(NotFoundError) as exc:
                await route_auth.resolve_plugin_route_scope(request, PLUGIN_KEY)
            messages.append(not_found_resource(exc.value))
        # 两边的文案只有"请求里的实例标识"不同，形态与档位完全一致
        assert messages[0].replace(OTHER_INSTANCE, "X") == messages[1].replace("does_not_exist", "X")
        assert messages[0] == f"实例 '{OTHER_INSTANCE}'"
        assert messages[1] == "实例 'does_not_exist'"

    @pytest.mark.asyncio
    async def test_plugin_disabled_on_instance_uses_the_same_404(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ):
        user = tenant_admin()
        active: set[tuple[str, str]] = set()
        monkeypatch.setattr(route_auth, "is_plugin_active", lambda key, instance: (key, instance or "") in active)

        async def _fake_current_user(_connection: Any) -> DBUser:
            return user

        monkeypatch.setattr(route_auth, "get_current_user", _fake_current_user)
        request = make_request(
            query={"adapter_key": "onebot_v11", "instance_key": OWN_INSTANCE},
            headers={"Authorization": "Bearer whatever"},
        )
        with pytest.raises(NotFoundError) as exc:
            await route_auth.resolve_plugin_route_scope(request, PLUGIN_KEY)
        assert not_found_resource(exc.value) == f"实例 '{OWN_INSTANCE}'"

    @pytest.mark.asyncio
    async def test_platform_global_view_requires_explicit_allow(
        self,
        db,
        active_plugins,
        monkeypatch: pytest.MonkeyPatch,
    ):
        user = platform_owner()

        async def _fake_current_user(_connection: Any) -> DBUser:
            return user

        monkeypatch.setattr(route_auth, "get_current_user", _fake_current_user)
        headers = {"Authorization": "Bearer whatever"}
        with pytest.raises(ValidationError):
            await route_auth.resolve_plugin_route_scope(make_request(headers=headers), PLUGIN_KEY)
        scope = await route_auth.resolve_plugin_route_scope(
            make_request(headers=headers),
            PLUGIN_KEY,
            allow_global_view=True,
        )
        assert (scope.adapter_key, scope.instance_key) == ("", "")
        assert scope.is_platform is True


class TestTickets:
    @pytest.mark.asyncio
    async def test_ticket_round_trip(self, db, persist_user):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE)
        assert await verify_plugin_route_ticket(ticket, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE) == user.id

    @pytest.mark.asyncio
    async def test_expired_ticket_is_rejected(self, db, persist_user):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE, now=1000)
        assert await verify_plugin_route_ticket(ticket, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE, now=1000) == user.id
        assert (
            await verify_plugin_route_ticket(
                ticket,
                PLUGIN_KEY,
                "onebot_v11",
                OWN_INSTANCE,
                now=1000 + TICKET_TTL_SECONDS + 1,
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_tampered_signature_is_rejected(self, db, persist_user):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE)
        replacement = "0" if ticket[-1] != "0" else "1"
        tampered = ticket[:-1] + replacement
        assert await verify_plugin_route_ticket(tampered, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE) is None

    @pytest.mark.asyncio
    async def test_ticket_is_bound_to_plugin_and_instance(self, db, persist_user):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE)
        assert await verify_plugin_route_ticket(ticket, "other.plugin", "onebot_v11", OWN_INSTANCE) is None
        assert await verify_plugin_route_ticket(ticket, PLUGIN_KEY, "onebot_v11", OTHER_INSTANCE) is None
        assert await verify_plugin_route_ticket(ticket, PLUGIN_KEY, "other_adapter", OWN_INSTANCE) is None

    @pytest.mark.asyncio
    async def test_password_generation_change_revokes_ticket(self, db, persist_user):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE)
        user.password_generation = int(user.password_generation) + 1
        await user.save()
        assert await verify_plugin_route_ticket(ticket, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE) is None

    @pytest.mark.asyncio
    async def test_banned_user_ticket_is_rejected(self, db, persist_user):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE)
        user.ban_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        await user.save()
        assert await verify_plugin_route_ticket(ticket, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE) is None

    @pytest.mark.asyncio
    async def test_dependency_accepts_a_valid_ticket(
        self,
        db,
        persist_user,
        active_plugins,
        monkeypatch: pytest.MonkeyPatch,
    ):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE)

        async def _must_not_be_called(_connection: Any) -> DBUser:
            raise AssertionError("有票据时不该再走后台令牌解析")

        monkeypatch.setattr(route_auth, "get_current_user", _must_not_be_called)
        request = make_request(
            query={"adapter_key": "onebot_v11", "instance_key": OWN_INSTANCE, "ticket": ticket},
        )
        scope = await route_auth.resolve_plugin_route_scope(request, PLUGIN_KEY, allow_ticket=True)
        assert scope.instance_key == OWN_INSTANCE

    async def _dependency_rejects_ticket(self, ticket: str, *, query: Optional[Dict[str, str]] = None) -> None:
        """票据从依赖函数这一层被拒：票据的任何问题都必须是 401，而不是别的错误"""
        params = {"adapter_key": "onebot_v11", "instance_key": OWN_INSTANCE, "ticket": ticket}
        params.update(query or {})
        with pytest.raises(UnauthorizedError) as exc:
            await route_auth.resolve_plugin_route_scope(make_request(query=params), PLUGIN_KEY, allow_ticket=True)
        assert exc.value.http_status == 401

    @pytest.mark.asyncio
    async def test_dependency_rejects_expired_ticket(
        self,
        db,
        persist_user,
        active_plugins,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """过期票据：把模块时钟拨到有效期之后，依赖层必须是 401"""
        user = await persist_user(tenant_admin())
        signed_at = 1000
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE, now=signed_at)
        # 依赖层自己不收 now：拨动票据模块的时钟，等价于"票据签发很久之后才来连"。
        # 只换本模块的取时函数，不去改全局的 time.time（那会波及事件循环与别的用例）。
        expired_at = signed_at + TICKET_TTL_SECONDS + 1
        monkeypatch.setattr(route_ticket, "_now", lambda now=None: expired_at if now is None else int(now))
        await self._dependency_rejects_ticket(ticket)

    @pytest.mark.asyncio
    async def test_dependency_rejects_a_flipped_byte(self, db, persist_user, active_plugins):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE)
        replacement = "0" if ticket[-1] != "0" else "1"
        await self._dependency_rejects_ticket(ticket[:-1] + replacement)

    @pytest.mark.asyncio
    async def test_dependency_rejects_plugin_and_instance_mismatch(self, db, persist_user, active_plugins):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE)
        # 同一张票据拿去连另一个实例（票据本身仍然有效，只是绑定的实例对不符）
        await self._dependency_rejects_ticket(ticket, query={"instance_key": OTHER_INSTANCE})

    @pytest.mark.asyncio
    async def test_dependency_rejects_ticket_after_password_change(self, db, persist_user, active_plugins):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE)
        user.password_generation = int(user.password_generation) + 1
        await user.save()
        await self._dependency_rejects_ticket(ticket)

    @pytest.mark.asyncio
    async def test_dependency_rejects_ticket_of_a_banned_user(self, db, persist_user, active_plugins):
        user = await persist_user(tenant_admin())
        ticket = issue_plugin_route_ticket(user, PLUGIN_KEY, "onebot_v11", OWN_INSTANCE)
        user.ban_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        await user.save()
        await self._dependency_rejects_ticket(ticket)

    @pytest.mark.asyncio
    async def test_dependency_rejects_foreign_instance_bound_ticket(self, db, persist_user, active_plugins):
        """票据绑的是别人的实例：实例判定先于票据，仍是 401（票据与实际请求不符）"""
        owner = await persist_user(
            make_user(tenant_id=OTHER_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin),
        )
        ticket = issue_plugin_route_ticket(owner, PLUGIN_KEY, "onebot_v11", OTHER_INSTANCE)
        await self._dependency_rejects_ticket(ticket)


class TestTicketEndpoint:
    @pytest.mark.asyncio
    async def test_tenant_empty_instance_is_400(self, db, active_plugins):
        with pytest.raises(ValidationError):
            await issue_route_ticket(
                PluginRouteTicketRequest(plugin_key=PLUGIN_KEY),
                _current_user=tenant_admin(),
            )

    @pytest.mark.asyncio
    async def test_foreign_instance_is_404(self, db, active_plugins):
        with pytest.raises(NotFoundError):
            await issue_route_ticket(
                PluginRouteTicketRequest(
                    plugin_key=PLUGIN_KEY,
                    adapter_key="onebot_v11",
                    instance_key=OTHER_INSTANCE,
                ),
                _current_user=tenant_admin(),
            )

    @pytest.mark.asyncio
    async def test_normal_ticket_returns_ttl(self, db, active_plugins, persist_user):
        user = await persist_user(tenant_admin())
        response = await issue_route_ticket(
            PluginRouteTicketRequest(
                plugin_key=PLUGIN_KEY,
                adapter_key="onebot_v11",
                instance_key=OWN_INSTANCE,
            ),
            _current_user=user,
        )
        assert response.expires_in == 120
        assert response.ticket
