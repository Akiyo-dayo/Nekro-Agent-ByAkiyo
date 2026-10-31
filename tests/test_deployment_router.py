from types import SimpleNamespace

import pytest

from nekro_agent.routers import deployment as deployment_router
from nekro_agent.routers.deployment import (
    _extract_latest_agent_version,
    build_agent_version_response,
    create_deployment_backup,
    create_deployment_restore,
    create_deployment_update,
    router,
)
from nekro_agent.services.deployment.schemas import (
    DeploymentCreateBackupRequest,
    DeploymentRestoreRequest,
    DeploymentUpdateRequest,
)
from nekro_agent.services.user.deps import get_current_active_user


class _FakeDeploymentClient:
    def __init__(self) -> None:
        self.called = False
        self.request: DeploymentUpdateRequest | DeploymentCreateBackupRequest | DeploymentRestoreRequest | None = None
        self.username: str | None = None

    async def create_update(self, request: DeploymentUpdateRequest, *, username: str) -> dict[str, str]:
        self.called = True
        self.request = request
        self.username = username
        return {"job_id": "upd_1", "status": "queued"}

    async def create_backup(self, request: DeploymentCreateBackupRequest, *, username: str) -> dict[str, str]:
        self.called = True
        self.request = request
        self.username = username
        return {"job_id": "upd_2", "type": "backup", "status": "queued"}

    async def create_restore(self, request: DeploymentRestoreRequest, *, username: str) -> dict[str, str]:
        self.called = True
        self.request = request
        self.username = username
        return {"job_id": "upd_3", "type": "restore", "status": "queued"}


@pytest.mark.asyncio
async def test_update_handler_passes_current_user_to_client() -> None:
    fake_client = _FakeDeploymentClient()
    request = DeploymentUpdateRequest(channel="stable", client_request_id="request-1")

    response = await create_deployment_update(
        request,
        current_user=SimpleNamespace(username="owner"),
        client=fake_client,
    )

    assert response == {"job_id": "upd_1", "status": "queued"}
    assert fake_client.called is True
    assert fake_client.username == "owner"
    assert fake_client.request is not None
    assert fake_client.request.client_request_id == "request-1"


@pytest.mark.asyncio
async def test_backup_handler_passes_current_user_to_client() -> None:
    fake_client = _FakeDeploymentClient()
    request = DeploymentCreateBackupRequest(name="webui", client_request_id="backup-1")

    response = await create_deployment_backup(
        request,
        current_user=SimpleNamespace(username="owner"),
        client=fake_client,
    )

    assert response == {"job_id": "upd_2", "type": "backup", "status": "queued"}
    assert fake_client.called is True
    assert fake_client.username == "owner"
    assert isinstance(fake_client.request, DeploymentCreateBackupRequest)
    assert fake_client.request.name == "webui"


@pytest.mark.asyncio
async def test_restore_handler_passes_current_user_to_client() -> None:
    fake_client = _FakeDeploymentClient()
    request = DeploymentRestoreRequest(backup_id="backup.tar.gz", client_request_id="restore-1")

    response = await create_deployment_restore(
        request,
        current_user=SimpleNamespace(username="owner"),
        client=fake_client,
    )

    assert response == {"job_id": "upd_3", "type": "restore", "status": "queued"}
    assert fake_client.called is True
    assert fake_client.username == "owner"
    assert isinstance(fake_client.request, DeploymentRestoreRequest)
    assert fake_client.request.backup_id == "backup.tar.gz"


def _dependency_calls(dependant) -> set:
    """依赖是树状的，逐层展开

    `require_platform_owner()` 自己就依赖 `get_current_active_user`，只看第一层会漏掉它。
    """
    calls = set()
    for sub in dependant.dependencies:
        calls.add(sub.call)
        calls |= _dependency_calls(sub)
    return calls


def test_all_deployment_routes_require_platform_owner() -> None:
    """部署接口做的是宿主级动作（升级、备份、用备份覆盖回去），只有平台方能碰

    改前这些端点只挂 `get_current_active_user`，**没有任何角色校验** ——
    任何能登录的账号都够，包括只读的租户普通用户（`Role.User`），
    一次 `POST /deployment/restore` 就能把整套部署连同数据库回滚。

    这条同时保住了原来那条"每个部署路由都要求已登录活跃用户"的契约：
    `require_platform_owner()` 内部就依赖 `get_current_active_user`。
    """
    routes = [route for route in router.routes if getattr(route, "path", "").startswith("/deployment")]

    assert routes
    for route in routes:
        calls = _dependency_calls(route.dependant)
        assert get_current_active_user in calls, route.path
        owner_gates = [
            call
            for call in calls
            if getattr(call, "__qualname__", "") == "require_platform_owner.<locals>.dependency"
        ]
        assert owner_gates, route.path


def test_deployment_proxy_routes_have_explicit_response_models() -> None:
    proxy_paths = {
        "/deployment/instance",
        "/deployment/update",
        "/deployment/backups",
        "/deployment/backup",
        "/deployment/restore",
        "/deployment/jobs/{job_id}",
        "/deployment/jobs/{job_id}/logs",
        "/deployment/jobs/{job_id}/cancel",
    }
    routes = {route.path: route for route in router.routes if getattr(route, "path", None) in proxy_paths}

    assert set(routes) == proxy_paths
    for route in routes.values():
        assert route.response_model is not None
        assert 503 in route.responses


def test_extract_latest_agent_version_from_cloud_payload() -> None:
    payload = {"success": True, "data": {"latestVersion": "2.3.3"}}

    assert _extract_latest_agent_version(payload) == "2.3.3"


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "data": {"latestVersion": "2.3.3"}},
        {"success": True, "data": {}},
        {"success": True, "data": {"latestVersion": ""}},
        [{"name": "v2.3.3"}],
    ],
)
def test_extract_latest_agent_version_rejects_invalid_cloud_payload(payload: object) -> None:
    assert _extract_latest_agent_version(payload) is None


@pytest.mark.asyncio
async def test_agent_version_response_detects_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deployment_router, "get_app_version", lambda: "2.3.0")

    async def fetch_latest_version() -> str:
        return "v2.4.0"

    response = await build_agent_version_response(fetch_latest_version)

    assert response.current_version == "2.3.0"
    assert response.latest_version == "v2.4.0"
    assert response.checked is True
    assert response.update_available is True


@pytest.mark.asyncio
async def test_agent_version_response_hides_update_for_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deployment_router, "get_app_version", lambda: "2.4.0")

    async def fetch_latest_version() -> str:
        return "v2.4.0"

    response = await build_agent_version_response(fetch_latest_version)

    assert response.checked is True
    assert response.update_available is False


@pytest.mark.asyncio
async def test_agent_version_response_hides_update_when_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployment_router, "get_app_version", lambda: "2.3.0")

    async def fetch_latest_version() -> str:
        raise ValueError("bad payload")

    response = await build_agent_version_response(fetch_latest_version)

    assert response.checked is False
    assert response.update_available is False
    assert response.error_code == "latest_version_unavailable"
