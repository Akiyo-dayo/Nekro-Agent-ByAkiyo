"""白板按实例分板与会话可见性

白板的状态以前是一份模块级单例，提示词注入与事件流都不看会话：租户 A 的群能改、
清租户 B 正在直播的画面，B 的模型下一轮还能从注入里读到 A 写的内容。本模块钉住
"按实例对分板"之后必须成立的几件事。
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any, Dict, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.errors import NotFoundError, UnauthorizedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.plugin import route_auth
from nekro_agent.services.plugin.route_auth import PluginRouteScope
from nekro_agent.services.user.role import Role
from plugins.builtin import whiteboard

ADAPTER = "onebot_v11"
INSTANCE_A = "wb_a"
INSTANCE_B = "wb_b"
CHAT_A = f"{ADAPTER}-{INSTANCE_A}-group_100"
CHAT_B = f"{ADAPTER}-{INSTANCE_B}-group_200"
BOARD_A = (ADAPTER, INSTANCE_A)
BOARD_B = (ADAPTER, INSTANCE_B)
TENANT_A = 7
TENANT_B = 8


def make_request(*, query: Optional[Dict[str, str]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
    from urllib.parse import urlencode

    from starlette.requests import Request

    raw_headers = [(key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in (headers or {}).items()]
    scope = {
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


async def seed(tenants: bool) -> None:
    rows = [
        (INSTANCE_A, TENANT_A if tenants else None),
        (INSTANCE_B, TENANT_B if tenants else None),
    ]
    for instance_key, tenant_id in rows:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )
    for instance_key, chat_key in ((INSTANCE_A, CHAT_A), (INSTANCE_B, CHAT_B)):
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
            chat_key=chat_key,
            channel_type="group",
            data="{}",
        )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await seed(tenants=True)
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
async def single_tenant_db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await seed(tenants=False)
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def fake_auth(monkeypatch: pytest.MonkeyPatch):
    """路由依赖里的 Bearer 解析换成固定租户管理员，作用域判定仍走真实 DB"""
    user = DBUser(
        username="route-user",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=TENANT_A,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )

    async def _fake_current_user(_connection: Any) -> DBUser:
        return user

    monkeypatch.setattr(route_auth, "get_current_user", _fake_current_user)


@pytest.fixture(autouse=True)
def clean_boards(monkeypatch: pytest.MonkeyPatch):
    whiteboard._board_states.clear()
    whiteboard._board_tenants.clear()
    whiteboard.client_manager.clients.clear()
    whiteboard.client_manager.is_shutting_down = False
    active: set[tuple[str, str]] = {
        (whiteboard.plugin.key, INSTANCE_A),
        (whiteboard.plugin.key, INSTANCE_B),
        (whiteboard.plugin.key, ""),
    }
    monkeypatch.setattr(whiteboard, "is_plugin_active", lambda key, instance: (key, instance or "") in active)
    monkeypatch.setattr(route_auth, "is_plugin_active", lambda key, instance: (key, instance or "") in active)
    yield active
    for board_clients in whiteboard.client_manager.clients.values():
        for client in board_clients.values():
            client.is_alive = False
    whiteboard.client_manager.clients.clear()
    whiteboard._board_states.clear()
    whiteboard._board_tenants.clear()


def ctx_of(chat_key: str) -> AgentCtx:
    return AgentCtx(from_chat_key=chat_key)


def endpoints() -> Dict[str, Any]:
    return {route.path: route.endpoint for route in whiteboard.create_router().routes}


def make_scope(instance_key: str = INSTANCE_A) -> PluginRouteScope:
    return PluginRouteScope(
        user=DBUser(
            username="scope-user",
            password="",
            adapter_key="webui",
            platform_userid="0",
            perm_level=Role.Admin,
            login_time=datetime.datetime.now(datetime.timezone.utc),
            tenant_id=TENANT_A,
            tenant_role=TenantRole.TENANT_ADMIN.value,
        ),
        adapter_key=ADAPTER,
        instance_key=instance_key,
        is_platform=False,
    )


class TestPerBoardState:
    @pytest.mark.asyncio
    async def test_two_instances_have_separate_boards(self, db):
        await whiteboard.display_text(ctx_of(CHAT_A), "租户 A 的内容")
        await whiteboard.display_text(ctx_of(CHAT_B), "租户 B 的内容")

        state_a = whiteboard._board_states[BOARD_A]
        state_b = whiteboard._board_states[BOARD_B]
        assert [item.content for item in state_a.contents] == ["租户 A 的内容"]
        assert [item.content for item in state_b.contents] == ["租户 B 的内容"]

        prompt_a = await whiteboard.whiteboard_prompt(ctx_of(CHAT_A))
        prompt_b = await whiteboard.whiteboard_prompt(ctx_of(CHAT_B))
        assert "租户 A 的内容" in prompt_a
        assert "租户 B 的内容" not in prompt_a
        assert "租户 B 的内容" in prompt_b
        assert "租户 A 的内容" not in prompt_b

    @pytest.mark.asyncio
    async def test_broadcast_only_reaches_this_board(self, db):
        client_a = whiteboard.client_manager.register_client(BOARD_A, "client-a")
        client_b = whiteboard.client_manager.register_client(BOARD_B, "client-b")

        await whiteboard.display_text(ctx_of(CHAT_A), "只给 A")
        assert client_a.event_queue.qsize() == 1
        assert client_b.event_queue.qsize() == 0

        await whiteboard.display_text(ctx_of(CHAT_B), "只给 B")
        assert client_a.event_queue.qsize() == 1
        assert client_b.event_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_unresolvable_session_fails_closed_in_multitenant(self, db):
        result = await whiteboard.display_text(ctx_of("onebot_v11-ghost-group_999"), "越界")
        assert result == whiteboard.BOARD_UNAVAILABLE_MESSAGE
        assert whiteboard._board_states == {}
        assert await whiteboard.whiteboard_prompt(ctx_of("onebot_v11-ghost-group_999")) == ""

    @pytest.mark.asyncio
    async def test_unresolvable_session_uses_the_global_board_in_single_tenant(self, single_tenant_db):
        await whiteboard.display_text(ctx_of("onebot_v11-unknown-group_999"), "全局板内容")
        assert ("", "") in whiteboard._board_states
        assert [item.content for item in whiteboard._board_states[("", "")].contents] == ["全局板内容"]

    @pytest.mark.asyncio
    async def test_clear_only_clears_this_board(self, db):
        await whiteboard.display_text(ctx_of(CHAT_A), "A 的内容")
        await whiteboard.display_text(ctx_of(CHAT_B), "B 的内容")

        clear = endpoints()["/clear"]
        await clear(scope=make_scope(INSTANCE_A))

        assert whiteboard._board_states[BOARD_A].contents == []
        assert [item.content for item in whiteboard._board_states[BOARD_B].contents] == ["B 的内容"]


class TestOwnershipChange:
    """实例划给别的租户之后：板上的旧内容不给新主人看，已经连着的前租户连接要断开"""

    @staticmethod
    async def transfer_a_to_tenant_b() -> None:
        await DBAdapterInstance.filter(adapter_key=ADAPTER, instance_key=INSTANCE_A).update(tenant_id=TENANT_B)

    @pytest.mark.asyncio
    async def test_a_transferred_instance_starts_with_a_blank_board(self, db):
        await whiteboard.display_text(ctx_of(CHAT_A), "前租户的内容")
        await self.transfer_a_to_tenant_b()

        assert "前租户的内容" not in await whiteboard.whiteboard_prompt(ctx_of(CHAT_A))
        await whiteboard.display_text(ctx_of(CHAT_A), "新主人的内容")
        assert [item.content for item in whiteboard._board_states[BOARD_A].contents] == ["新主人的内容"]

    @pytest.mark.asyncio
    async def test_the_same_owner_keeps_its_board(self, db):
        await whiteboard.display_text(ctx_of(CHAT_A), "第一条")
        await whiteboard.display_text(ctx_of(CHAT_A), "第二条")

        assert [item.content for item in whiteboard._board_states[BOARD_A].contents] == ["第一条", "第二条"]

    @pytest.mark.asyncio
    async def test_the_status_route_does_not_show_the_previous_tenants_board(self, db):
        await whiteboard.display_text(ctx_of(CHAT_A), "前租户的内容")
        await self.transfer_a_to_tenant_b()

        status = await endpoints()["/status"](scope=make_scope(INSTANCE_A))

        assert status["data"]["state"]["contents"] == []

    @pytest.mark.asyncio
    async def test_authorization_is_rechecked(self, db):
        token = await whiteboard._get_or_create_view_token(BOARD_A)
        at_connect = await whiteboard._board_owner(BOARD_A)
        assert at_connect == (True, TENANT_A)

        assert await whiteboard._still_authorized(BOARD_A, token, at_connect) is True
        assert await whiteboard._still_authorized(BOARD_A, None, at_connect) is True

        # 只读链接被重置：拿旧令牌连着的断开，登录着连的不受影响
        await whiteboard.store.set(store_key=whiteboard._view_token_store_key(*BOARD_A), value="rotated")
        assert await whiteboard._still_authorized(BOARD_A, token, at_connect) is False
        assert await whiteboard._still_authorized(BOARD_A, None, at_connect) is True

        # 实例换了租户：两种连接都断开
        await self.transfer_a_to_tenant_b()
        assert await whiteboard._still_authorized(BOARD_A, None, at_connect) is False

    @pytest.mark.asyncio
    async def test_a_deleted_instance_is_no_longer_authorized(self, db):
        at_connect = await whiteboard._board_owner(BOARD_A)
        await DBAdapterInstance.filter(adapter_key=ADAPTER, instance_key=INSTANCE_A).delete()

        assert await whiteboard._board_owner(BOARD_A) == (False, None)
        assert await whiteboard._still_authorized(BOARD_A, None, at_connect) is False

    @pytest.mark.asyncio
    async def test_an_open_stream_is_dropped_after_the_transfer(self, db, monkeypatch: pytest.MonkeyPatch):
        """前租户拿只读链接连着：划转之后下一次复核就断开，不再收到新主人的内容"""
        monkeypatch.setattr(whiteboard, "_REAUTHORIZE_SECONDS", 0.0)
        token = await whiteboard._get_or_create_view_token(BOARD_A)
        request = make_request(query={"adapter_key": ADAPTER, "instance_key": INSTANCE_A, "view": token})

        async def connected() -> bool:
            return False

        monkeypatch.setattr(request, "is_disconnected", connected)
        response = await endpoints()["/events"](request)
        stream = response.body_iterator

        first = await asyncio.wait_for(stream.__anext__(), timeout=5)
        second = await asyncio.wait_for(stream.__anext__(), timeout=5)
        assert '"connected"' in str(first) and '"update_state"' in str(second)

        await self.transfer_a_to_tenant_b()

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(stream.__anext__(), timeout=5)
        assert whiteboard.client_manager.get_client_count(BOARD_A) == 0


class TestViewToken:
    @pytest.mark.asyncio
    async def test_token_only_opens_its_own_board(self, db):
        token = await whiteboard._get_or_create_view_token(BOARD_A)
        assert await whiteboard._load_view_token(BOARD_A) == token
        assert whiteboard.is_plugin_active(whiteboard.plugin.key, INSTANCE_A) is True

        board_key, view = await whiteboard._authorize_events_request(
            make_request(query={"adapter_key": ADAPTER, "instance_key": INSTANCE_A, "view": token}),
        )
        assert board_key == BOARD_A
        assert view == token

        with pytest.raises(NotFoundError):
            await whiteboard._authorize_events_request(
                make_request(query={"adapter_key": ADAPTER, "instance_key": INSTANCE_B, "view": token}),
            )

    @pytest.mark.asyncio
    async def test_a_valid_token_is_404_once_the_plugin_is_off_on_that_instance(self, db, clean_boards):
        """插件在这个实例上关掉以后，旧链接里的令牌即便没换过也打不开这块板"""
        token = await whiteboard._get_or_create_view_token(BOARD_A)
        query = {"adapter_key": ADAPTER, "instance_key": INSTANCE_A, "view": token}
        board_key, _ = await whiteboard._authorize_events_request(make_request(query=query))
        assert board_key == BOARD_A

        clean_boards.discard((whiteboard.plugin.key, INSTANCE_A))
        with pytest.raises(NotFoundError):
            await whiteboard._authorize_events_request(make_request(query=query))

    @pytest.mark.asyncio
    async def test_a_garbled_token_is_the_same_404(self, db):
        """乱填的令牌（含非 ASCII 字符）也只是 404，不能变成 500"""
        await whiteboard._get_or_create_view_token(BOARD_A)
        with pytest.raises(NotFoundError):
            await whiteboard._authorize_events_request(
                make_request(query={"adapter_key": ADAPTER, "instance_key": INSTANCE_A, "view": "令牌" * 8}),
            )

    @pytest.mark.asyncio
    async def test_rotate_invalidates_old_token_and_disconnects_old_clients(self, db):
        token = await whiteboard._get_or_create_view_token(BOARD_A)
        old_client = whiteboard.client_manager.register_client(BOARD_A, "viewer", view_token=token)
        ticket_client = whiteboard.client_manager.register_client(BOARD_A, "admin", view_token=None)

        data = await endpoints()["/view-link/rotate"](scope=make_scope(INSTANCE_A))

        assert data["url"].startswith(f"/plugins/{whiteboard.plugin.key}/?")
        assert data["disconnected"] == 1
        assert old_client.is_alive is False
        assert ticket_client.is_alive is True
        with pytest.raises(NotFoundError):
            await whiteboard._authorize_events_request(
                make_request(query={"adapter_key": ADAPTER, "instance_key": INSTANCE_A, "view": token}),
            )

    @pytest.mark.asyncio
    async def test_view_link_generated_on_first_request_and_reused(self, db):
        first = await endpoints()["/view-link"](scope=make_scope(INSTANCE_A))
        second = await endpoints()["/view-link"](scope=make_scope(INSTANCE_A))
        assert first["url"] == second["url"]


class TestRouteGuards:
    @pytest.mark.asyncio
    async def test_status_requires_login(self, db):
        with pytest.raises(UnauthorizedError):
            await route_auth.resolve_plugin_route_scope(make_request(), whiteboard.plugin.key)

    @pytest.mark.asyncio
    async def test_status_rejects_foreign_instance(self, db):
        with pytest.raises(NotFoundError):
            await route_auth.resolve_plugin_route_scope(
                make_request(
                    query={"adapter_key": ADAPTER, "instance_key": INSTANCE_B},
                    headers={"Authorization": "Bearer whatever"},
                ),
                whiteboard.plugin.key,
                allow_global_view=False,
            )

    @pytest.mark.asyncio
    async def test_disabled_plugin_on_instance_is_404(self, db, clean_boards):
        clean_boards.discard((whiteboard.plugin.key, INSTANCE_A))
        with pytest.raises(NotFoundError) as exc:
            await route_auth.resolve_plugin_route_scope(
                make_request(
                    query={"adapter_key": ADAPTER, "instance_key": INSTANCE_A},
                    headers={"Authorization": "Bearer whatever"},
                ),
                whiteboard.plugin.key,
            )
        assert exc.value.params.get("resource") == f"实例 '{INSTANCE_A}'"

    @pytest.mark.asyncio
    async def test_demo_page_is_data_free_for_anonymous(self, db):
        html = await endpoints()["/"]()
        body = html.body.decode("utf-8")
        assert "请先登录 NA 后台" in body
        assert "/api/plugin-route/ticket" in body
        assert "复制只读链接" in body
        assert "租户 A 的内容" not in body


def build_http_client() -> Any:
    """把白板路由按生产方式挂到一个最小 app 上，带真实异常处理器

    这几条断言关心的是**状态码**（401 / 404），只有把 `AppError` 的处理器接上才看得到：
    直接调端点函数只会拿到被抛出的异常对象，看不出它最终翻译成几号。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nekro_agent.core.exception_handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(whiteboard.create_router(), prefix=f"/plugins/{whiteboard.plugin.key}")
    return TestClient(app, raise_server_exceptions=False)


class TestStatusAndClearEndpoints:
    """`/status` 与 `/clear` 的真实 HTTP 行为：不登录 401、别的实例 404、只作用于本板"""

    def test_status_without_login_is_401(self, db):
        response = build_http_client().get(
            f"/plugins/{whiteboard.plugin.key}/status",
            params={"adapter_key": ADAPTER, "instance_key": INSTANCE_A},
        )
        assert response.status_code == 401

    def test_clear_without_login_is_401(self, db):
        response = build_http_client().post(
            f"/plugins/{whiteboard.plugin.key}/clear",
            params={"adapter_key": ADAPTER, "instance_key": INSTANCE_A},
        )
        assert response.status_code == 401

    def test_status_on_another_tenants_instance_is_404(self, db):
        response = build_http_client().get(
            f"/plugins/{whiteboard.plugin.key}/status",
            params={"adapter_key": ADAPTER, "instance_key": INSTANCE_B},
            headers={"Authorization": "Bearer whatever"},
        )
        assert response.status_code == 404

    def test_clear_on_a_disabled_instance_is_404(self, db, clean_boards):
        clean_boards.discard((whiteboard.plugin.key, INSTANCE_A))
        response = build_http_client().post(
            f"/plugins/{whiteboard.plugin.key}/clear",
            params={"adapter_key": ADAPTER, "instance_key": INSTANCE_A},
            headers={"Authorization": "Bearer whatever"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_clear_endpoint_only_clears_this_board(self, db):
        """清空接口只清本板：另一块板的内容与连接都不受影响"""
        await whiteboard.display_text(ctx_of(CHAT_A), "A 的内容")
        await whiteboard.display_text(ctx_of(CHAT_B), "B 的内容")
        client_b = whiteboard.client_manager.register_client(BOARD_B, "client-b")

        await endpoints()["/clear"](make_scope(INSTANCE_A))

        assert whiteboard._board_states[BOARD_A].contents == []
        assert [item.content for item in whiteboard._board_states[BOARD_B].contents] == ["B 的内容"]
        assert client_b.event_queue.qsize() == 0


class TestClientManagerShutdown:
    @pytest.mark.asyncio
    async def test_cleanup_disconnects_every_board(self, db):
        first = whiteboard.client_manager.register_client(BOARD_A, "a")
        second = whiteboard.client_manager.register_client(BOARD_B, "b")
        await whiteboard.client_manager.stop()
        assert first.is_alive is False
        assert second.is_alive is False
        assert whiteboard.client_manager.get_client_count() == 0
        whiteboard.client_manager.is_shutting_down = False
        await asyncio.sleep(0)
