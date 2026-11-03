"""wechat_ilink_multi 实例管理接口的租户收口

租户管理员在旧权限体系里也是 `Role.Admin`，而这一族此前只有 `@require_role(Role.Admin)`：
列表、新建、删除、启停、续期全都是「填一个实例标识就能操作任意实例」。本文件钉住改造后的分档：

- 平台方（`tenant_id=None` + `platform_owner`）行为不变；
- 租户只能看、只能登录自己名下的实例，别人的实例一律 404（不暴露存在性）；
- 实例的新建、删除、启停、续期与续期策略是平台方专属，租户身份一律 403；
- 同名陷阱：acme 名下的 `onebot_v11/wxrival` 与 rival 的微信实例同名，守卫必须按
  `(adapter_key, instance_key)` 成对判定，不能把 OneBot 那一行当成微信这一行。
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tortoise import Tortoise

from nekro_agent.adapters.wechat_ilink_multi import routers as routers_module
from nekro_agent.adapters.wechat_ilink_multi.routers import create_router
from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role

WECHAT = "wechat_ilink_multi"
ONEBOT = "onebot_v11"
API_PREFIX = "/api/adapters/wechat_ilink_multi"

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

ACME_METADATA = '{"quota_daily_reply_max": 5}'


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBTenant.create(id=ACME_TENANT_ID, name="acme", display_name="acme")
        await DBTenant.create(id=RIVAL_TENANT_ID, name="rival", display_name="rival")
        await DBAdapterInstance.create(
            adapter_key=WECHAT,
            instance_key="wxacme",
            display_name="wxacme",
            status="offline",
            provider="ilink",
            tenant_id=ACME_TENANT_ID,
            metadata_json=ACME_METADATA,
        )
        await DBAdapterInstance.create(
            adapter_key=WECHAT,
            instance_key="wxrival",
            display_name="wxrival",
            status="offline",
            provider="ilink",
            tenant_id=RIVAL_TENANT_ID,
        )
        await DBAdapterInstance.create(
            adapter_key=WECHAT,
            instance_key="wxplat",
            display_name="wxplat",
            status="offline",
            provider="ilink",
            tenant_id=None,
        )
        # 同名陷阱：acme 名下的 OneBot 实例与 rival 的微信实例同名
        await DBAdapterInstance.create(
            adapter_key=ONEBOT,
            instance_key="wxrival",
            display_name="onebot-wxrival",
            status="offline",
            provider="ilink",
            tenant_id=ACME_TENANT_ID,
        )
        rival_row = await DBAdapterInstance.get(adapter_key=WECHAT, instance_key="wxrival")
        await DBAdapterInstanceEvent.create(
            instance_id=rival_row.id,
            event_type="status_change",
            status_from="offline",
            status_to="online",
            message="rival 的事件",
        )
        yield
    finally:
        await Tortoise.close_connections()


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


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_admin() -> DBUser:
    """平台方账号仍然是 `Role.Admin`：证明行为不变靠的是平台方身份而不是 `Role.Super`"""
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


class FakeBotManager:
    """记录每次调用，并给出可序列化的返回值"""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, ...]] = []

    async def start_bind(self, instance_key: str) -> SimpleNamespace:
        self.calls.append(("start_bind", instance_key))
        return SimpleNamespace(
            session_id="s1",
            qr_url="https://example.invalid/qr",
            expires_at=datetime.now(timezone.utc),
        )

    async def poll_bind(self, instance_key: str, bind_session_id: str) -> SimpleNamespace:
        self.calls.append(("poll_bind", instance_key, bind_session_id))
        return SimpleNamespace(status=SimpleNamespace(value="pending"), error_message="")

    async def start_instance(self, instance_key: str) -> object:
        self.calls.append(("start_instance", instance_key))
        return object()

    async def stop_instance(self, instance_key: str, *, update_status: bool = True) -> None:
        # 记下调用那一刻实例在库里的状态：删除接口必须先落软删标记、再停连接
        row = await DBAdapterInstance.get_or_none(adapter_key=WECHAT, instance_key=instance_key)
        self.calls.append(("stop_instance", instance_key, str(update_status), row.status if row else ""))

    async def renew_instance(self, instance_key: str) -> DBAdapterInstance:
        self.calls.append(("renew_instance", instance_key))
        return await DBAdapterInstance.get(adapter_key=WECHAT, instance_key=instance_key)


@pytest.fixture
def fake_bot_manager(monkeypatch) -> FakeBotManager:
    fake = FakeBotManager()
    monkeypatch.setattr(routers_module, "_get_bot_manager", lambda: fake)
    return fake


@pytest.fixture
def client() -> TestClient:
    """真实路由 + 真实异常处理器，身份用依赖覆盖注入"""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(create_router(), prefix=API_PREFIX)
    test_client = TestClient(app, raise_server_exceptions=False)

    def login_as(user: Optional[DBUser]) -> None:
        if user is None:
            app.dependency_overrides.pop(get_current_active_user, None)
            return
        app.dependency_overrides[get_current_active_user] = lambda: user

    test_client.login_as = login_as  # type: ignore[attr-defined]
    return test_client


class TestInstanceList:
    def test_tenant_sees_only_its_own_instance(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.get(f"{API_PREFIX}/instances")

        assert response.status_code == 200, response.text
        items = response.json()["instances"]
        assert [item["instance_key"] for item in items] == ["wxacme"]
        assert items[0]["metadata_json"] == ""

    def test_platform_admin_sees_every_instance_with_metadata(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(platform_admin())  # type: ignore[attr-defined]

        response = client.get(f"{API_PREFIX}/instances")

        assert response.status_code == 200, response.text
        items = {item["instance_key"]: item for item in response.json()["instances"]}
        assert sorted(items) == ["wxacme", "wxplat", "wxrival"]
        assert items["wxacme"]["metadata_json"] == ACME_METADATA


class TestInstanceDetail:
    def test_tenant_reads_own_instance_without_metadata(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.get(f"{API_PREFIX}/instances/wxacme")

        assert response.status_code == 200, response.text
        assert response.json()["metadata_json"] == ""

    def test_tenant_cannot_read_a_same_named_instance_of_another_adapter(
        self,
        db,  # noqa: ARG002
        client: TestClient,
    ) -> None:
        """acme 名下确实有 `onebot_v11/wxrival`，但那条归属不能算到微信这一行上"""
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.get(f"{API_PREFIX}/instances/wxrival")

        assert response.status_code == 404, response.text

    def test_tenant_cannot_read_a_platform_instance(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.get(f"{API_PREFIX}/instances/wxplat")

        assert response.status_code == 404, response.text

    def test_platform_admin_reads_any_instance(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(platform_admin())  # type: ignore[attr-defined]

        response = client.get(f"{API_PREFIX}/instances/wxrival")

        assert response.status_code == 200, response.text
        assert response.json()["instance_key"] == "wxrival"


class TestInstanceEvents:
    def test_tenant_cannot_read_another_tenants_events(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.get(f"{API_PREFIX}/instances/wxrival/events")

        assert response.status_code == 404, response.text

    def test_tenant_reads_own_instance_events(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.get(f"{API_PREFIX}/instances/wxacme/events")

        assert response.status_code == 200, response.text
        assert response.json() == []

    def test_platform_admin_reads_any_instance_events(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(platform_admin())  # type: ignore[attr-defined]

        response = client.get(f"{API_PREFIX}/instances/wxrival/events")

        assert response.status_code == 200, response.text
        events = response.json()
        assert [event["event_type"] for event in events] == ["status_change"]


class TestBind:
    def test_tenant_cannot_start_bind_on_another_tenants_instance(
        self,
        db,  # noqa: ARG002
        client: TestClient,
        fake_bot_manager: FakeBotManager,
    ) -> None:
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.post(f"{API_PREFIX}/instances/wxrival/bind/start")

        assert response.status_code == 404, response.text
        assert [call for call in fake_bot_manager.calls if call[0] == "start_bind"] == []

    def test_tenant_can_start_bind_on_own_instance(
        self,
        db,  # noqa: ARG002
        client: TestClient,
        fake_bot_manager: FakeBotManager,
    ) -> None:
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.post(f"{API_PREFIX}/instances/wxacme/bind/start")

        assert response.status_code == 200, response.text
        assert fake_bot_manager.calls == [("start_bind", "wxacme")]

    def test_tenant_cannot_poll_another_tenants_bind_status(
        self,
        db,  # noqa: ARG002
        client: TestClient,
        fake_bot_manager: FakeBotManager,
    ) -> None:
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = client.get(f"{API_PREFIX}/instances/wxrival/bind/status/s1")

        assert response.status_code == 404, response.text
        assert [call for call in fake_bot_manager.calls if call[0] == "poll_bind"] == []


PLATFORM_ONLY_CALLS: List[Tuple[str, str, Dict[str, Any]]] = [
    ("post", "/instances", {"json": {"instance_key": "wxnew"}}),
    ("delete", "/instances/wxacme", {}),
    ("post", "/instances/wxacme/start", {}),
    ("post", "/instances/wxacme/stop", {}),
    ("post", "/instances/wxacme/renew", {}),
    ("patch", "/instances/wxacme/renew-policy", {"json": {"renew_before_minutes": 30}}),
]


class TestPlatformOnlyActions:
    @pytest.mark.parametrize(("method", "path", "kwargs"), PLATFORM_ONLY_CALLS)
    async def test_tenant_is_refused_and_nothing_happens(
        self,
        db,  # noqa: ARG002
        client: TestClient,
        fake_bot_manager: FakeBotManager,
        method: str,
        path: str,
        kwargs: Dict[str, Any],
    ) -> None:
        """六条都对 acme 自己的实例发：能力边界看调用方身份，不看目标归属"""
        client.login_as(acme_admin())  # type: ignore[attr-defined]

        response = getattr(client, method)(f"{API_PREFIX}{path}", **kwargs)

        assert response.status_code == 403, response.text
        assert [call for call in fake_bot_manager.calls if call[0] == "start_instance"] == []
        assert [call for call in fake_bot_manager.calls if call[0] == "stop_instance"] == []
        assert [call for call in fake_bot_manager.calls if call[0] == "renew_instance"] == []
        assert (await DBAdapterInstance.filter(adapter_key=WECHAT, instance_key="wxnew").exists()) is False
        wxacme = await DBAdapterInstance.get(adapter_key=WECHAT, instance_key="wxacme")
        assert wxacme.status == "offline"
        assert wxacme.renew_before_minutes == 60


class TestPlatformAdminActions:
    async def test_platform_admin_can_create(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(platform_admin())  # type: ignore[attr-defined]

        response = client.post(f"{API_PREFIX}/instances", json={"instance_key": "wxnew"})

        assert response.status_code == 201, response.text
        assert await DBAdapterInstance.filter(adapter_key=WECHAT, instance_key="wxnew").exists()

    async def test_platform_admin_can_delete(self, db, client: TestClient) -> None:  # noqa: ARG002
        client.login_as(platform_admin())  # type: ignore[attr-defined]

        response = client.delete(f"{API_PREFIX}/instances/wxplat")

        assert response.status_code == 204, response.text
        row = await DBAdapterInstance.get(adapter_key=WECHAT, instance_key="wxplat")
        assert row.status == "deleted"

    async def test_delete_stops_the_running_connection_after_marking_deleted(
        self,
        db,  # noqa: ARG002
        client: TestClient,
        fake_bot_manager: FakeBotManager,
    ) -> None:
        """只落软删标记不会让长轮询退出；停连接时库里已是 deleted，且不再改写状态"""
        client.login_as(platform_admin())  # type: ignore[attr-defined]

        response = client.delete(f"{API_PREFIX}/instances/wxplat")

        assert response.status_code == 204, response.text
        assert fake_bot_manager.calls == [("stop_instance", "wxplat", "False", "deleted")]
