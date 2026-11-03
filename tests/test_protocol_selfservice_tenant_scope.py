"""租户的协议端自助运维（A2 / B7）

## 这份测试在守什么

改之前，`routers/console/containers.py` 的 11 个端点与 `routers/console/health.py` 的
3 个端点**全部** `Role.Super`。于是「QQ 掉线 / 需要重新扫码」——最高频的那类运营事件——
对定制版客户是一个红点加零个按钮。边界应当是：租户
**最多能「重启自己的 NapCat」**，且必须由后端校验该容器确实属于该租户。

现在切成两档，这份文件把两档的**行为**逐条钉住（分档结果本身钉在
`tests/test_route_permission_contract.py::TestProtocolSelfServiceSplitIsPinned`）：

| 契约 | 把它改回去 | 该红的用例 |
|---|---|---|
| 自助运维四条降到 Role.Admin | 改回 `Role.Super` | `TestTenantCanOperateItsOwnProtocolEndpoint` |
| 归属校验落在实例上 | 端点里去掉 `_resolve_accessible_instance` | `TestTenantCannotTouchSomebodyElses` |
| 越权是 404 不是 403 | 守卫换成 `PermissionDeniedError` | `test_*_on_a_foreign_instance_is_404` |
| 宿主资源分配仍是 Super | 把 create/delete/start/stop/network-config 降权 | `TestHostLevelActionsStayPlatformOnly` |
| 只读成员碰不到写操作 | 降到 `Role.User` | `TestReadOnlyTenantMemberIsRefused` |
| 归属校验落在**容器**上 | 读侧去掉 `_assert_owned` | `TestContainerOwnershipIsCheckedOnTheContainerItself` |
| 健康面按作用域过滤 | 去掉 `scope_instance_keys` | `TestHealthSurfaceIsScoped` |
| 告警按实例解析通道 | `dispatch_notification` 不带实例 | `TestAlertChannelsFollowTheInstance` |
| 租户能配自己的 Webhook | 去掉 `scope_owned_secret` 分支 | `TestTenantMayOwnItsAlertWebhook` |
| 出网目标过 SSRF 校验 | 去掉 `is_outbound_url` 分支 | `test_a_private_webhook_is_refused_at_write` |

## 取材原则

表驱动、显式输入、精确期望；不随机 fuzz、不 sleep、不只断言「没抛异常」。
环境派生值（归属标签、挂载点）一律**调产品自己的函数**现算（§2.9），路径落在 `tmp_path` 下。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from aiodocker.exceptions import DockerError
from fastapi import HTTPException
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.core_utils import ConfigManager
from nekro_agent.core.os_env import OsEnv
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.config import _assert_tenant_may_write
from nekro_agent.routers.console import containers as containers_router
from nekro_agent.routers.console import health as health_router
from nekro_agent.schemas.console import ContainerState
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.config_service import (
    ADAPTER_OVERRIDE_KEY_PREFIX,
    instance_override_config_key,
    instance_override_root_dir,
)
from nekro_agent.services.console import container_manager
from nekro_agent.services.console.instance_health import instance_health_service
from nekro_agent.services.notification import channels as notification_channels
from nekro_agent.services.user.role import Role

ADAPTER_KEY = "onebot_v11"

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

# acme 的实例（本部署自己建的容器）与 rival 的实例
ACME_INSTANCE = "acme1"
RIVAL_INSTANCE = "rival1"
# 平台方直接持有、不属于任何租户的实例
PLATFORM_INSTANCE = "plat1"


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------


def make_user(*, user_id: int, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份；`@require_role` 只读 `perm_level`

    `user_id` 要给：`resolve_console_identity()` 会把它装进 `CurrentIdentity`，
    留空会在构造模型时炸掉，而那与被测判定无关。
    """
    user = DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )
    user.id = user_id
    return user


def platform_owner() -> DBUser:
    return make_user(user_id=1, tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def acme_admin() -> DBUser:
    """租户管理员：`TENANT_MAX_PERM_LEVEL` 把他钉死在 Role.Admin"""
    return make_user(user_id=2, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def acme_member() -> DBUser:
    """租户普通用户：只读"""
    return make_user(user_id=3, tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)


def rival_admin() -> DBUser:
    return make_user(user_id=4, tenant_id=RIVAL_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


# ---------------------------------------------------------------------------
# 数据与环境
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, tenant_id in (
            (ACME_INSTANCE, ACME_TENANT_ID),
            (RIVAL_INSTANCE, RIVAL_TENANT_ID),
            (PLATFORM_INSTANCE, None),
        ):
            instance = await DBAdapterInstance.create(
                adapter_key=ADAPTER_KEY,
                instance_key=instance_key,
                display_name=instance_key,
                status="offline",
                provider="builtin_napcat",
                tenant_id=tenant_id,
                metadata_json="",
            )
            await DBAdapterInstanceEvent.create(
                instance_id=instance.id,
                event_type="status_change",
                status_from="online",
                status_to="offline",
                message=f"{instance_key} 掉线",
            )
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def here(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把进程摆到「本部署、未设 NEKRO_INSTANCE_NAME」这个默认形态上"""
    data_dir = tmp_path / "na-a" / "data" / "nekro_agent"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture
def neighbour(tmp_path: Path) -> Path:
    """同一台宿主上的另一套部署"""
    path = tmp_path / "na-b" / "data" / "nekro_agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mount_points_of(monkeypatch: pytest.MonkeyPatch, data_dir: Path, instance_key: str) -> Dict[str, Path]:
    """某套部署会给该实例挂上的「容器内目标 → 宿主机源」（调产品自己的函数现算）"""
    with monkeypatch.context() as patch:
        patch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        return {
            container_manager.CONTAINER_DATA_DIR: Path(OsEnv.DATA_DIR).resolve(),
            container_manager.CONTAINER_QQ_STATE_DIR: container_manager.get_qq_state_dir(instance_key).resolve(),
            container_manager.CONTAINER_NAPCAT_CONFIG_DIR: container_manager.get_napcat_config_dir(instance_key).resolve(),
        }


def owner_tag_of(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> str:
    with monkeypatch.context() as patch:
        patch.setattr(OsEnv, "INSTANCE_NAME", "")
        patch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        return container_manager.get_owner_tag()


def make_info(
    instance_key: str,
    *,
    owner_label: str,
    mounts: Optional[Dict[str, Path]] = None,
    status: str = "running",
) -> Dict[str, Any]:
    """拼一份 `docker inspect` 形态的容器信息"""
    name = container_manager.build_container_name(instance_key)
    info: Dict[str, Any] = {
        "Name": f"/{name}",
        "Config": {
            "Labels": {
                container_manager.LABEL_INSTANCE: instance_key,
                container_manager.LABEL_OWNER: owner_label,
            },
            "Image": "mlikiowa/napcat-docker:latest",
        },
        "HostConfig": {"PortBindings": {f"{container_manager.NAPCAT_WEBUI_PORT}/tcp": [{"HostPort": "16530"}]}},
        "State": {"Status": status, "StartedAt": "2026-01-01T00:00:00Z"},
    }
    if mounts is not None:
        info["Mounts"] = [
            {"Type": "bind", "Source": str(source), "Destination": destination} for destination, source in mounts.items()
        ]
    return info


class _FakeContainer:
    def __init__(self, name: str, info: Dict[str, Any]) -> None:
        self.name = name
        self.info = info
        self.calls: List[str] = []

    async def show(self) -> Dict[str, Any]:
        return self.info

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self, t: int = 0) -> None:
        self.calls.append(f"stop:{t}")

    async def restart(self, t: int = 0) -> None:
        self.calls.append(f"restart:{t}")

    async def delete(self, force: bool = False) -> None:
        self.calls.append(f"delete:{force}")


class _FakeContainers:
    def __init__(self, state: Dict[str, Any]) -> None:
        self._state = state

    async def get(self, name: str) -> _FakeContainer:
        self._state["attempts"].append(name)
        container = self._state["containers"].get(name)
        if container is None:
            raise DockerError(404, {"message": f"No such container: {name}"})
        return container


class _FakeDocker:
    def __init__(self, state: Dict[str, Any]) -> None:
        self.containers = _FakeContainers(state)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """接管 `aiodocker.Docker()`；记录每次按名字取容器的尝试"""
    state: Dict[str, Any] = {"containers": {}, "attempts": []}

    def factory(*_args: Any, **_kwargs: Any) -> _FakeDocker:
        return _FakeDocker(state)

    monkeypatch.setattr(container_manager.aiodocker, "Docker", factory)
    return state


def put_container(docker_state: Dict[str, Any], info: Dict[str, Any]) -> _FakeContainer:
    name = str(info["Name"]).lstrip("/")
    container = _FakeContainer(name, info)
    docker_state["containers"][name] = container
    return container


@pytest.fixture
def no_network_resync(monkeypatch: pytest.MonkeyPatch) -> None:
    """重启前的网络配置重放会读写 DATA_DIR 下的文件，与本文件的判定无关"""

    async def _noop(_instance: DBAdapterInstance) -> None:
        return None

    monkeypatch.setattr(container_manager, "resync_network_config", _noop)


@pytest.fixture
def login_recorder(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """把扫码 / 快速登录换成记录器：本文件判的是「谁能走到这一步」，不是 NapCat 协议本身"""
    reached: List[str] = []

    async def fake_qrcode(instance: DBAdapterInstance):
        reached.append(f"qrcode:{instance.instance_key}")
        from nekro_agent.schemas.console import LoginQrCodeResponse

        return LoginQrCodeResponse(available=True, qrcode_url="https://example.com/qr", message="ok")

    async def fake_quick_login(instance: DBAdapterInstance) -> str:
        reached.append(f"quick-login:{instance.instance_key}")
        return "已触发快速登录"

    monkeypatch.setattr(container_manager, "get_login_qrcode", fake_qrcode)
    monkeypatch.setattr(container_manager, "quick_login", fake_quick_login)
    return reached


# ===========================================================================
# 自助运维：租户对自己的实例
# ===========================================================================


class TestTenantCanOperateItsOwnProtocolEndpoint:
    """R19 那一条：租户最多能「重启我的 NapCat」，扫码 / 快速登录同档"""

    async def test_restart_reaches_the_right_container(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        docker: Dict[str, Any],
        no_network_resync,  # noqa: ARG002
    ) -> None:
        container = put_container(docker, make_info(ACME_INSTANCE, owner_label=container_manager.get_owner_tag()))

        result = await containers_router.restart_container(instance_key=ACME_INSTANCE, _current_user=acme_admin())

        assert result.message == "容器已重启"
        assert [call.split(":", 1)[0] for call in container.calls] == ["restart"]

    async def test_container_state_is_readable(
        self,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        docker: Dict[str, Any],
    ) -> None:
        """读不到容器状态的话，协议端面板对租户就是一片空白"""
        put_container(docker, make_info(ACME_INSTANCE, owner_label=container_manager.get_owner_tag()))

        info = await containers_router.get_container(instance_key=ACME_INSTANCE, _current_user=acme_admin())

        assert info.state == ContainerState.RUNNING
        assert info.name == container_manager.build_container_name(ACME_INSTANCE)

    @pytest.mark.parametrize("action", ["qrcode", "quick-login"])
    async def test_login_recovery_is_reachable(
        self,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        login_recorder: List[str],
        action: str,
    ) -> None:
        """掉登录态时重启治不好，扫码 / 快速登录是自愈的另一半"""
        if action == "qrcode":
            await containers_router.get_login_qrcode(instance_key=ACME_INSTANCE, _current_user=acme_admin())
        else:
            await containers_router.quick_login(instance_key=ACME_INSTANCE, _current_user=acme_admin())

        assert login_recorder == [f"{action}:{ACME_INSTANCE}"]

    async def test_platform_owner_path_is_unchanged(
        self,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        docker: Dict[str, Any],
        no_network_resync,  # noqa: ARG002
    ) -> None:
        """平台方对任何实例照旧，包括不属于任何租户的那些"""
        for instance_key in (ACME_INSTANCE, RIVAL_INSTANCE, PLATFORM_INSTANCE):
            put_container(docker, make_info(instance_key, owner_label=container_manager.get_owner_tag()))
            await containers_router.restart_container(instance_key=instance_key, _current_user=platform_owner())
            container = docker["containers"][container_manager.build_container_name(instance_key)]
            assert [call.split(":", 1)[0] for call in container.calls] == ["restart"]

    async def test_a_malformed_instance_key_still_reports_400_for_the_platform_owner(
        self,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        docker: Dict[str, Any],
    ) -> None:
        """先 resolve 再校归属：反过来的话平台方拿非法标识会从 400 悄悄变成 404

        `ValidationError` 是 400（"这个标识本身不合法"），`NotFoundError` 是 404
        （"没有这个实例"）—— 两者对平台方是不同的诊断，不该因为加了一道归属校验而合并。
        """
        with pytest.raises(ValidationError):
            await containers_router.get_container(instance_key="../../etc", _current_user=platform_owner())
        assert docker["attempts"] == []


class TestTenantCannotTouchSomebodyElses:
    """越权一律 404：403 会告诉租户"这个 instance_key 确实存在"，等于把实例清单变成可枚举的"""

    ENDPOINTS: Tuple[str, ...] = ("container", "restart", "qrcode", "quick-login")

    @staticmethod
    async def _drive(action: str, instance_key: str, user: DBUser) -> None:
        if action == "container":
            await containers_router.get_container(instance_key=instance_key, _current_user=user)
        elif action == "restart":
            await containers_router.restart_container(instance_key=instance_key, _current_user=user)
        elif action == "qrcode":
            await containers_router.get_login_qrcode(instance_key=instance_key, _current_user=user)
        elif action == "quick-login":
            await containers_router.quick_login(instance_key=instance_key, _current_user=user)
        else:  # pragma: no cover - 参数化表里没有别的取值
            raise AssertionError(action)

    @pytest.mark.parametrize("action", ENDPOINTS)
    @pytest.mark.parametrize("target", [RIVAL_INSTANCE, PLATFORM_INSTANCE])
    async def test_a_foreign_instance_is_404_and_never_reaches_docker(
        self,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        docker: Dict[str, Any],
        no_network_resync,  # noqa: ARG002
        login_recorder: List[str],
        action: str,
        target: str,
    ) -> None:
        put_container(docker, make_info(target, owner_label=container_manager.get_owner_tag()))

        with pytest.raises(NotFoundError):
            await self._drive(action, target, acme_admin())

        # 拒绝必须发生在碰 Docker / 碰 NapCat 之前
        assert docker["attempts"] == []
        assert login_recorder == []

    @pytest.mark.parametrize("action", ENDPOINTS)
    async def test_a_nonexistent_instance_is_indistinguishable(
        self,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        docker: Dict[str, Any],
        no_network_resync,  # noqa: ARG002
        login_recorder: List[str],  # noqa: ARG002
        action: str,
    ) -> None:
        """不存在与无权访问对外必须同一个结论，否则 404/403 的区分就白做了"""
        with pytest.raises(NotFoundError):
            await self._drive(action, "nosuchinstance", acme_admin())
        assert docker["attempts"] == []

    async def test_the_other_tenant_sees_its_own(
        self,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        docker: Dict[str, Any],
    ) -> None:
        """对照：同一条端点、同一个实例，换成它的主人就通 —— 证明拒绝来自归属判定本身"""
        put_container(docker, make_info(RIVAL_INSTANCE, owner_label=container_manager.get_owner_tag()))

        info = await containers_router.get_container(instance_key=RIVAL_INSTANCE, _current_user=rival_admin())

        assert info.state == ContainerState.RUNNING


class TestTheAuthorisedRowIsTheOperatedRow:
    """`instance_key` 不是全局唯一的，两条查询可以落在不同的两行上

    唯一性只到 `unique_together = (adapter_key, instance_key)`（见
    `models/db_adapter_instance.py` 的模块 docstring），而
    `resolve_instance()` 排除墓碑、`assert_instance_accessible()` 不排除。于是：

        租户名下 onebot_v11/dup（墓碑，id 小）+ 平台方的 minecraft/dup（在跑）
        ⇒ 归属校验命中租户那行放行，容器操作落到平台方那行

    容器名只由 `instance_key` 派生、归属标签又是本部署的，`_owned_container` 这第二道
    也拦不住 —— 它拦的是跨部署，这里是同部署内跨租户。
    """

    DUP_KEY = "dup"

    @pytest.fixture
    async def duplicated(self, db) -> None:  # noqa: ARG002
        """先建租户的墓碑行（id 小），再建平台方的活行"""
        await DBAdapterInstance.create(
            adapter_key=ADAPTER_KEY,
            instance_key=self.DUP_KEY,
            display_name=self.DUP_KEY,
            status="deleted",
            provider="builtin_napcat",
            tenant_id=ACME_TENANT_ID,
            metadata_json="",
        )
        await DBAdapterInstance.create(
            adapter_key="minecraft",
            instance_key=self.DUP_KEY,
            display_name=self.DUP_KEY,
            status="offline",
            provider="builtin_napcat",
            tenant_id=None,
            metadata_json="",
        )

    async def test_the_tenant_cannot_ride_a_tombstone_onto_the_platform_row(
        self,
        here: Path,  # noqa: ARG002
        duplicated,  # noqa: ARG002
        docker: Dict[str, Any],
        no_network_resync,  # noqa: ARG002
    ) -> None:
        put_container(docker, make_info(self.DUP_KEY, owner_label=container_manager.get_owner_tag()))

        with pytest.raises(NotFoundError):
            await containers_router.restart_container(instance_key=self.DUP_KEY, _current_user=acme_admin())

        assert docker["attempts"] == []

    async def test_the_platform_owner_still_operates_the_live_row(
        self,
        here: Path,  # noqa: ARG002
        duplicated,  # noqa: ARG002
        docker: Dict[str, Any],
        no_network_resync,  # noqa: ARG002
    ) -> None:
        """对照：同一份数据，平台方那一侧的行为逐字不变（否则上一条测的是"整条路坏了"）"""
        container = put_container(docker, make_info(self.DUP_KEY, owner_label=container_manager.get_owner_tag()))

        result = await containers_router.restart_container(instance_key=self.DUP_KEY, _current_user=platform_owner())

        assert result.message == "容器已重启"
        assert [call.split(":", 1)[0] for call in container.calls] == ["restart"]


class TestHostLevelActionsStayPlatformOnly:
    """建/删容器、起/停、以及带着 OneBot 访问令牌的网络配置仍然是宿主级动作"""

    @staticmethod
    async def _drive(action: str, instance_key: str, user: DBUser) -> None:
        if action == "create":
            await containers_router.create_container(instance_key=instance_key, body=None, _current_user=user)
        elif action == "delete":
            await containers_router.delete_container(instance_key=instance_key, _current_user=user)
        elif action == "start":
            await containers_router.start_container(instance_key=instance_key, _current_user=user)
        elif action == "stop":
            await containers_router.stop_container(instance_key=instance_key, _current_user=user)
        elif action == "network-config-read":
            await containers_router.get_network_config(instance_key=instance_key, _current_user=user)
        elif action == "network-config-write":
            from nekro_agent.schemas.console import NetworkConfig

            await containers_router.update_network_config(
                instance_key=instance_key,
                body=NetworkConfig(),
                _current_user=user,
            )
        else:  # pragma: no cover
            raise AssertionError(action)

    ACTIONS: Tuple[str, ...] = (
        "create",
        "delete",
        "start",
        "stop",
        "network-config-read",
        "network-config-write",
    )

    @pytest.mark.parametrize("action", ACTIONS)
    async def test_tenant_admin_is_refused_even_on_its_own_instance(
        self,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        docker: Dict[str, Any],
        action: str,
    ) -> None:
        """这一档是**能力边界**，所以是 403 而不是 404：实例确实是他的，动作不是他能做的"""
        with pytest.raises(HTTPException) as exc:
            await self._drive(action, ACME_INSTANCE, acme_admin())

        assert exc.value.status_code == 403
        assert docker["attempts"] == []


class TestReadOnlyTenantMemberIsRefused:
    """租户普通用户（`Role.User`）是只读成员，自助运维不该向他开放"""

    @pytest.mark.parametrize("action", TestTenantCannotTouchSomebodyElses.ENDPOINTS)
    async def test_member_cannot_operate(
        self,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        docker: Dict[str, Any],
        no_network_resync,  # noqa: ARG002
        login_recorder: List[str],
        action: str,
    ) -> None:
        with pytest.raises(HTTPException) as exc:
            await TestTenantCannotTouchSomebodyElses._drive(action, ACME_INSTANCE, acme_member())  # noqa: SLF001

        assert exc.value.status_code == 403
        assert docker["attempts"] == []
        assert login_recorder == []


# ===========================================================================
# 归属校验落到容器本身
# ===========================================================================


class TestContainerOwnershipIsCheckedOnTheContainerItself:
    """`instance_key` 对得上不代表那个容器是本部署建的

    容器名 `nekro_napcat_{instance_key}` 是**宿主全局**的且不带部署前缀，两套部署库里都可能
    有一行同名 `instance_key`。降权之后这条路径成了租户能触达的第一段，所以读写两侧都要收口。
    """

    async def test_restart_refuses_the_neighbours_container(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        neighbour: Path,
        db,  # noqa: ARG002
        docker: Dict[str, Any],
        no_network_resync,  # noqa: ARG002
    ) -> None:
        container = put_container(
            docker,
            make_info(
                ACME_INSTANCE,
                owner_label=owner_tag_of(monkeypatch, neighbour),
                mounts=mount_points_of(monkeypatch, neighbour, ACME_INSTANCE),
            ),
        )

        with pytest.raises(PermissionDeniedError):
            await containers_router.restart_container(instance_key=ACME_INSTANCE, _current_user=acme_admin())

        assert container.calls == []

    async def test_reading_state_of_the_neighbours_container_reports_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        neighbour: Path,
        db,  # noqa: ARG002
        docker: Dict[str, Any],
    ) -> None:
        """读侧也 fail-closed：不归本部署所有 ⇒ 本部署没有这个容器

        不做这一道的话，后台会照着**邻居的**容器显示状态、镜像与 WebUI 端口，
        而扫码/快速登录正是拿这个判定去决定「容器在不在跑」。
        """
        put_container(
            docker,
            make_info(
                ACME_INSTANCE,
                owner_label=owner_tag_of(monkeypatch, neighbour),
                mounts=mount_points_of(monkeypatch, neighbour, ACME_INSTANCE),
                status="running",
            ),
        )

        info = await containers_router.get_container(instance_key=ACME_INSTANCE, _current_user=acme_admin())

        assert info.state == ContainerState.ABSENT
        assert info.webui_port is None

    async def test_our_own_container_is_still_reported(
        self,
        here: Path,  # noqa: ARG002
        db,  # noqa: ARG002
        docker: Dict[str, Any],
    ) -> None:
        """对照：同一条判定，标签换成本部署的就照常回真实状态"""
        put_container(docker, make_info(ACME_INSTANCE, owner_label=container_manager.get_owner_tag()))

        info = await containers_router.get_container(instance_key=ACME_INSTANCE, _current_user=acme_admin())

        assert info.state == ContainerState.RUNNING
        assert info.webui_port == 16530

    async def test_the_platform_owner_is_refused_all_the_same(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        neighbour: Path,
        db,  # noqa: ARG002
        docker: Dict[str, Any],
    ) -> None:
        """跨部署这一维与身份无关：平台方也是宿主上**这一套**部署的平台方"""
        put_container(
            docker,
            make_info(
                PLATFORM_INSTANCE,
                owner_label=owner_tag_of(monkeypatch, neighbour),
                mounts=mount_points_of(monkeypatch, neighbour, PLATFORM_INSTANCE),
            ),
        )

        info = await containers_router.get_container(instance_key=PLATFORM_INSTANCE, _current_user=platform_owner())

        assert info.state == ContainerState.ABSENT


# ===========================================================================
# 健康面的作用域
# ===========================================================================


class TestHealthSurfaceIsScoped:
    """`/health/instances` 与 `/health/events` 里带着别的租户的实例标识与协议端错误细节"""

    async def test_tenant_sees_only_its_own_instances(self, db) -> None:  # noqa: ARG002
        result = await health_router.get_instance_health(_current_user=acme_admin())

        assert [item.instance_key for item in result.items] == [ACME_INSTANCE]
        assert result.total == 1

    async def test_platform_owner_sees_everything(self, db) -> None:  # noqa: ARG002
        result = await health_router.get_instance_health(_current_user=platform_owner())

        assert sorted(item.instance_key for item in result.items) == sorted(
            [ACME_INSTANCE, RIVAL_INSTANCE, PLATFORM_INSTANCE],
        )

    @staticmethod
    async def _events(user: DBUser, instance_key: str = ""):
        """直呼端点函数时 `Query(...)` 默认值不会被 FastAPI 解析，查询参数一律显式给全"""
        return await health_router.get_instance_health_events(
            page=1,
            page_size=20,
            adapter_key="",
            instance_key=instance_key,
            _current_user=user,
        )

    async def test_events_are_filtered_by_scope(self, db) -> None:  # noqa: ARG002
        result = await self._events(acme_admin())

        assert [item.instance_key for item in result.items] == [ACME_INSTANCE]
        assert result.total == 1

    async def test_platform_owner_sees_every_event(self, db) -> None:  # noqa: ARG002
        result = await self._events(platform_owner())

        assert sorted(item.instance_key for item in result.items) == sorted(
            [ACME_INSTANCE, RIVAL_INSTANCE, PLATFORM_INSTANCE],
        )

    async def test_filtering_by_a_foreign_instance_key_yields_nothing(self, db) -> None:  # noqa: ARG002
        """作用域与筛选参数是**与**关系：筛选只能收窄，不能扩宽

        这一条是本组最重要的：`instance_key` 是调用方能自由填的参数，
        它一旦能覆盖作用域，前面的过滤就等于没做。
        """
        result = await self._events(acme_admin(), instance_key=RIVAL_INSTANCE)

        assert result.items == []
        assert result.total == 0

    async def test_the_same_filter_works_for_its_owner(self, db) -> None:  # noqa: ARG002
        """对照：同一个筛选参数，换成它的主人就取得到"""
        result = await self._events(rival_admin(), instance_key=RIVAL_INSTANCE)

        assert [item.instance_key for item in result.items] == [RIVAL_INSTANCE]

    @pytest.fixture
    async def cross_adapter_twin(self, db) -> None:  # noqa: ARG002
        """另一个适配器下与 acme 的实例**同名**、但归平台方的一行

        `instance_key` 的唯一性只到 `(adapter_key, instance_key)`，这一行是合法数据。
        """
        twin = await DBAdapterInstance.create(
            adapter_key="minecraft",
            instance_key=ACME_INSTANCE,
            display_name="platform-twin",
            status="offline",
            provider="builtin_napcat",
            tenant_id=None,
            metadata_json="",
        )
        await DBAdapterInstanceEvent.create(
            instance_id=twin.id,
            event_type="status_change",
            status_from="online",
            status_to="offline",
            message="平台方那行掉线",
        )

    async def test_a_cross_adapter_namesake_is_not_visible(self, cross_adapter_twin) -> None:  # noqa: ARG002
        """作用域按归属列下推，不按 `instance_key` 的取值

        按 `instance_key__in=[租户名下的标识]` 过滤的是**列的取值**：另一个适配器下同名的
        那一行会一并命中，于是租户在自己的健康面上看到平台方的实例与它的协议端错误细节。
        """
        health = await health_router.get_instance_health(_current_user=acme_admin())
        events = await self._events(acme_admin())

        assert [(item.adapter_key, item.instance_key) for item in health.items] == [(ADAPTER_KEY, ACME_INSTANCE)]
        assert [(item.adapter_key, item.instance_key) for item in events.items] == [(ADAPTER_KEY, ACME_INSTANCE)]

    async def test_the_platform_owner_sees_both_namesakes(self, cross_adapter_twin) -> None:  # noqa: ARG002
        """对照：同名两行对平台方都在，证明上一条不是"这行根本没建出来\""""
        health = await health_router.get_instance_health(_current_user=platform_owner())

        assert sorted(item.adapter_key for item in health.items if item.instance_key == ACME_INSTANCE) == [
            "minecraft",
            ADAPTER_KEY,
        ]

    @pytest.mark.parametrize("endpoint", ["instances", "events", "test-notification"])
    async def test_read_only_member_is_refused(self, db, endpoint: str) -> None:  # noqa: ARG002
        with pytest.raises(HTTPException) as exc:
            if endpoint == "instances":
                await health_router.get_instance_health(_current_user=acme_member())
            elif endpoint == "events":
                await self._events(acme_member())
            else:
                await health_router.send_test_notification(
                    body=health_router.TestNotificationRequest(instance_key=ACME_INSTANCE),
                    _current_user=acme_member(),
                )
        assert exc.value.status_code == 403


def _enable_ops_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """把运维频道这条通道配到"完整"

    `OpsBotPolicy.is_configured` 要求四项**全部**非空，少配一项它就不进渠道列表 ——
    于是"租户试发没打到运维频道"这条断言会因为通道根本没装出来而恒绿。
    """
    monkeypatch.setattr(config, "OPS_BOT_ALERT_ENABLED", True)
    monkeypatch.setattr(config, "OPS_BOT_ENABLED", True)
    monkeypatch.setattr(config, "OPS_BOT_ADAPTER_KEY", ADAPTER_KEY)
    monkeypatch.setattr(config, "OPS_BOT_INSTANCE_KEY", PLATFORM_INSTANCE)
    monkeypatch.setattr(config, "OPS_BOT_CHANNEL_ID", "group_1")
    monkeypatch.setattr(config, "OPS_BOT_OPERATORS", ["10001"])


class TestTestNotificationIsScoped:
    """试发会**真的**外发一条通知，因此租户必须指明是给自己的哪个实例试"""

    @pytest.fixture
    def dispatched(self, monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, str]]:
        calls: List[Dict[str, str]] = []

        async def fake_dispatch(title: str, body: str, level: str = "info", **kwargs: str) -> Dict[str, bool]:
            calls.append({"title": title, **{key: str(value) for key, value in kwargs.items()}})
            return {"email": True}

        monkeypatch.setattr(health_router, "dispatch_notification", fake_dispatch)
        return calls

    async def test_tenant_must_name_an_instance(self, db, dispatched: List[Dict[str, str]]) -> None:  # noqa: ARG002
        """空实例对平台方是"全局通道"，租户没有这一档（与 `resolve_instance_scope` 同源）"""
        with pytest.raises(ValidationError):
            await health_router.send_test_notification(
                body=health_router.TestNotificationRequest(),
                _current_user=acme_admin(),
            )
        assert dispatched == []

    async def test_tenant_cannot_test_a_foreign_instance(self, db, dispatched: List[Dict[str, str]]) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await health_router.send_test_notification(
                body=health_router.TestNotificationRequest(instance_key=RIVAL_INSTANCE),
                _current_user=acme_admin(),
            )
        assert dispatched == []

    async def test_tenant_can_test_its_own(self, db, dispatched: List[Dict[str, str]]) -> None:  # noqa: ARG002
        result = await health_router.send_test_notification(
            body=health_router.TestNotificationRequest(instance_key=ACME_INSTANCE),
            _current_user=acme_admin(),
        )

        assert result.any_succeeded is True
        assert dispatched == [
            {
                "title": "Nekro Agent 通知测试",
                "adapter_key": ADAPTER_KEY,
                "instance_key": ACME_INSTANCE,
                # 租户那一次试发只走自己的通道，理由见 `send_test_notification` 的 docstring
                "scoped_only": "True",
            },
        ]

    async def test_a_forged_adapter_key_is_refused(self, db, dispatched: List[Dict[str, str]]) -> None:  # noqa: ARG002
        """`adapter_key` 是自由字段，归属只校验到 `instance_key` 这一维

        不比对的话，租户拿自己的实例配另一个适配器的标识，就能让告警配置的解析退到
        **那个适配器层**上去 —— 适配器层是跨租户共享、只有平台方写得进的作用域。
        """
        with pytest.raises(ValidationError):
            await health_router.send_test_notification(
                body=health_router.TestNotificationRequest(
                    instance_key=ACME_INSTANCE,
                    adapter_key="some_other_adapter",
                ),
                _current_user=acme_admin(),
            )
        assert dispatched == []

    async def test_a_matching_adapter_key_is_accepted(self, db, dispatched: List[Dict[str, str]]) -> None:  # noqa: ARG002
        """对照：填对了就照走，字段本身没有被废掉"""
        await health_router.send_test_notification(
            body=health_router.TestNotificationRequest(instance_key=ACME_INSTANCE, adapter_key=ADAPTER_KEY),
            _current_user=acme_admin(),
        )

        assert [call["adapter_key"] for call in dispatched] == [ADAPTER_KEY]

    async def test_platform_owner_keeps_the_global_channel(self, db, dispatched: List[Dict[str, str]]) -> None:  # noqa: ARG002
        """平台方不带实例照旧走全局通道，行为逐字不变"""
        await health_router.send_test_notification(
            body=health_router.TestNotificationRequest(),
            _current_user=platform_owner(),
        )

        assert dispatched == [
            {
                "title": "Nekro Agent 通知测试",
                "adapter_key": "",
                "instance_key": "",
                "scoped_only": "False",
            },
        ]

    async def test_a_tenant_test_never_reaches_the_platform_channels(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """接线：这一条不打桩 `dispatch_notification`，走真实的渠道装配

        邮件收件人与运维频道都是平台方的，全局 Webhook 那条地址也是。租户的试发一旦
        照发整份渠道列表，他手上就多出一个「把标题正文任意的消息投递到平台方收件端」的
        原语 —— 而上面每一条按 `instance_key` 判定的用例都还是绿的。
        """
        reached: List[str] = []

        async def _send(self, title: str, body: str, level: str) -> bool:  # noqa: ANN001, ARG001
            reached.append(type(self).name)
            return True

        monkeypatch.setattr(notification_channels.EmailChannel, "send", _send)
        monkeypatch.setattr(notification_channels.OpsBotChannel, "send", _send)
        monkeypatch.setattr(notification_channels.WebhookChannel, "send", _send)
        monkeypatch.setattr(config, "INSTANCE_ALERT_WEBHOOK_URL", "https://platform-owner.example.com/hook")
        _enable_ops_bot(monkeypatch)
        # 先确认这三条通道在**同一份配置**下确实都会被装出来，否则下面的空列表
        # 只是在说"运维频道本来就没开"（变异表里 9 号就是这么活下来的）
        assert sorted(channel.name for channel in notification_channels.build_enabled_channels()) == [
            "email",
            "ops_bot",
            "webhook",
        ]

        result = await health_router.send_test_notification(
            body=health_router.TestNotificationRequest(
                title="任意标题",
                body="任意正文",
                instance_key=ACME_INSTANCE,
            ),
            _current_user=acme_admin(),
        )

        assert reached == []
        assert result.results == {}
        assert result.any_succeeded is False

    async def test_the_platform_owner_still_gets_every_channel(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """对照：同一套装配，平台方那一侧一条都不少（否则上一条测的就是"整个功能坏了"）"""
        reached: List[str] = []

        async def _send(self, title: str, body: str, level: str) -> bool:  # noqa: ANN001, ARG001
            reached.append(type(self).name)
            return True

        monkeypatch.setattr(notification_channels.EmailChannel, "send", _send)
        monkeypatch.setattr(notification_channels.WebhookChannel, "send", _send)
        monkeypatch.setattr(config, "INSTANCE_ALERT_WEBHOOK_URL", "https://platform-owner.example.com/hook")
        monkeypatch.setattr(config, "OPS_BOT_ALERT_ENABLED", False)

        await health_router.send_test_notification(
            body=health_router.TestNotificationRequest(),
            _current_user=platform_owner(),
        )

        assert sorted(reached) == ["email", "webhook"]


# ===========================================================================
# B7：告警通道跟随实例
# ===========================================================================


@pytest.fixture
def instance_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """在实例层装一份覆盖配置，并让解析器看得见它

    `_has_instance_layer()` 按目录存在与否快速判定，判定结果随配置版本号缓存，
    所以这里既要建目录也要 `bump_version()`。
    """
    data_dir = tmp_path / "override-data"
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(data_dir))
    instance_override_root_dir(ADAPTER_KEY).mkdir(parents=True, exist_ok=True)

    registered: List[str] = []

    def install(instance_key: str, **values: Any) -> OverridableConfig:
        overrides = OverridableConfig()
        for name, value in values.items():
            setattr(overrides, f"enable_{name}", True)
            setattr(overrides, name, value)
        config_key = instance_override_config_key(ADAPTER_KEY, instance_key)
        ConfigManager.register_config(config_key, overrides)
        registered.append(config_key)
        config_resolver.bump_version()
        return overrides

    try:
        yield install
    finally:
        for config_key in registered:
            ConfigManager.unregister_config(config_key)
        config_resolver.bump_version()


@pytest.fixture
def adapter_override():
    """在**适配器层**装一份覆盖配置

    适配器层是跨租户共享的作用域，`_assert_config_key_accessible` 对租户一律 403，
    所以写在这一层的取值是**平台方**的。它与实例层在解析上同样命中，
    因此"来自覆盖层"不能一概等同于"租户写得进"。
    """
    registered: List[str] = []

    def install(**values: Any) -> OverridableConfig:
        overrides = OverridableConfig()
        for name, value in values.items():
            setattr(overrides, f"enable_{name}", True)
            setattr(overrides, name, value)
        config_key = f"{ADAPTER_OVERRIDE_KEY_PREFIX}{ADAPTER_KEY}"
        ConfigManager.register_config(config_key, overrides)
        registered.append(config_key)
        config_resolver.bump_version()
        return overrides

    try:
        yield install
    finally:
        for config_key in registered:
            ConfigManager.unregister_config(config_key)
        config_resolver.bump_version()


class TestAlertChannelsFollowTheInstance:
    """「客户 A 的 Bot 掉线」必须能发到客户 A 自己配的收件端"""

    TENANT_WEBHOOK = "https://acme.example.com/alert"

    async def test_instance_layer_webhook_wins(self, instance_override) -> None:
        instance_override(ACME_INSTANCE, INSTANCE_ALERT_WEBHOOK_URL=self.TENANT_WEBHOOK)

        resolved = notification_channels.resolve_alert_config(ADAPTER_KEY, ACME_INSTANCE)

        assert resolved.INSTANCE_ALERT_WEBHOOK_URL == self.TENANT_WEBHOOK

    async def test_an_instance_without_override_falls_back_to_global(self, instance_override) -> None:
        instance_override(ACME_INSTANCE, INSTANCE_ALERT_WEBHOOK_URL=self.TENANT_WEBHOOK)

        resolved = notification_channels.resolve_alert_config(ADAPTER_KEY, RIVAL_INSTANCE)

        assert resolved.INSTANCE_ALERT_WEBHOOK_URL == config.INSTANCE_ALERT_WEBHOOK_URL

    async def test_no_instance_means_the_global_config(self) -> None:
        assert notification_channels.resolve_alert_config() is config
        assert notification_channels.resolve_alert_config(ADAPTER_KEY, "") is config

    @staticmethod
    def _webhook_of(adapter_key: str = "", instance_key: str = "") -> notification_channels.WebhookChannel:
        """按与 `dispatch_notification` 逐字相同的方式装出这个实例上的 Webhook 通道

        判据（取值、来源）不在这里重算 —— 那样测的就是测试自己的一份实现了。
        """
        built = notification_channels.build_enabled_channels(
            notification_channels.resolve_alert_config(adapter_key, instance_key),
            webhook_is_instance_owned=notification_channels.alert_webhook_is_instance_owned(adapter_key, instance_key),
        )
        return next(channel for channel in built if isinstance(channel, notification_channels.WebhookChannel))

    async def test_the_webhook_channel_targets_the_tenant_address(self, instance_override) -> None:
        instance_override(ACME_INSTANCE, INSTANCE_ALERT_WEBHOOK_URL=self.TENANT_WEBHOOK)

        assert self._webhook_of(ADAPTER_KEY, ACME_INSTANCE).url == self.TENANT_WEBHOOK

    async def test_a_tenant_written_address_uses_the_strict_outbound_guard(self, instance_override) -> None:
        """实例层那条不认 `ALLOW_PRIVATE_NETWORK_DOWNLOAD`

        全局那条只有平台方写得进（`system` 配置对租户是 403），自建部署把告警发到内网收件器
        是正当用法；实例层那条租户写得进，放行开关一旦被打开就成了 SSRF 原语。
        """
        instance_override(ACME_INSTANCE, INSTANCE_ALERT_WEBHOOK_URL=self.TENANT_WEBHOOK)

        assert self._webhook_of(ADAPTER_KEY, ACME_INSTANCE).tenant_writable is True

    async def test_the_platform_global_address_keeps_the_original_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """平台方那条行为逐字不变"""
        monkeypatch.setattr(config, "INSTANCE_ALERT_WEBHOOK_URL", "https://platform.example.com/alert")

        webhook = self._webhook_of()

        assert webhook.url == "https://platform.example.com/alert"
        assert webhook.tenant_writable is False

    async def test_an_override_that_matches_the_global_value_is_still_the_tenants(
        self,
        monkeypatch: pytest.MonkeyPatch,
        instance_override,
    ) -> None:
        """来源按层判，不按取值比

        取值撞车（租户把自己实例层那条填成与平台方全局那条逐字相同）时，"最终值 != 全局值"
        这条比较法会把一次真实的实例层覆盖判成"来自全局"，于是租户填的地址反过来拿到
        平台方那一档的宽松出网判据。这条误判没有任何运行时信号。
        """
        collided = "https://collision.example.com/alert"
        monkeypatch.setattr(config, "INSTANCE_ALERT_WEBHOOK_URL", collided)
        instance_override(ACME_INSTANCE, INSTANCE_ALERT_WEBHOOK_URL=collided)

        webhook = self._webhook_of(ADAPTER_KEY, ACME_INSTANCE)

        assert webhook.url == collided
        assert webhook.tenant_writable is True

    async def test_an_instance_without_an_override_is_not_tenant_owned(
        self,
        monkeypatch: pytest.MonkeyPatch,
        instance_override,
    ) -> None:
        """对照：没配覆盖的实例解析到的是平台方那条，判据必须是平台方那一档"""
        monkeypatch.setattr(config, "INSTANCE_ALERT_WEBHOOK_URL", "https://platform.example.com/alert")
        instance_override(ACME_INSTANCE, INSTANCE_ALERT_WEBHOOK_URL=self.TENANT_WEBHOOK)

        webhook = self._webhook_of(ADAPTER_KEY, RIVAL_INSTANCE)

        assert webhook.url == "https://platform.example.com/alert"
        assert webhook.tenant_writable is False

    ADAPTER_WEBHOOK = "http://10.0.0.9/internal-alert"

    async def test_an_adapter_layer_address_is_not_the_tenants(self, adapter_override) -> None:
        """适配器层是**跨租户共享**作用域，只有平台方写得进（`_assert_config_key_accessible` 对租户 403）

        把"来自任何覆盖层"当成"租户写得进"，平台方写在适配器层的内网收件器地址就会被
        严格档拒发 —— 而自建部署把告警发到内网本来是正当用法（`ALLOW_PRIVATE_NETWORK_DOWNLOAD`）。
        """
        adapter_override(INSTANCE_ALERT_WEBHOOK_URL=self.ADAPTER_WEBHOOK)

        webhook = self._webhook_of(ADAPTER_KEY, ACME_INSTANCE)

        assert webhook.url == self.ADAPTER_WEBHOOK
        assert webhook.tenant_writable is False

    async def test_the_instance_layer_still_wins_over_the_adapter_layer(
        self,
        adapter_override,
        instance_override,
    ) -> None:
        """对照：同时存在时实例层优先，且判成租户的 —— 证明上一条不是"适配器层压根没生效\""""
        adapter_override(INSTANCE_ALERT_WEBHOOK_URL=self.ADAPTER_WEBHOOK)
        instance_override(ACME_INSTANCE, INSTANCE_ALERT_WEBHOOK_URL=self.TENANT_WEBHOOK)

        webhook = self._webhook_of(ADAPTER_KEY, ACME_INSTANCE)

        assert webhook.url == self.TENANT_WEBHOOK
        assert webhook.tenant_writable is True

    async def test_the_dispatcher_hands_the_source_verdict_to_the_channel(
        self,
        monkeypatch: pytest.MonkeyPatch,
        instance_override,
    ) -> None:
        """接线：零件对了不等于调用点还在（§2.7）

        上面几条都是自己装配渠道再看结论。`dispatch_notification()` 少传一次来源判定，
        它们照样全绿，而线上每一条租户填的地址都会拿到平台方那一档的宽松出网判据。
        """
        instance_override(ACME_INSTANCE, INSTANCE_ALERT_WEBHOOK_URL=self.TENANT_WEBHOOK)
        seen: List[Tuple[str, bool]] = []

        async def _send_webhook(self, title: str, body: str, level: str) -> bool:  # noqa: ANN001, ARG001
            seen.append((self.url, self.tenant_writable))
            return True

        async def _send_noop(self, title: str, body: str, level: str) -> bool:  # noqa: ANN001, ARG001
            return True

        monkeypatch.setattr(notification_channels.WebhookChannel, "send", _send_webhook)
        monkeypatch.setattr(notification_channels.EmailChannel, "send", _send_noop)

        await notification_channels.dispatch_notification(
            title="t",
            body="b",
            adapter_key=ADAPTER_KEY,
            instance_key=ACME_INSTANCE,
        )

        assert seen == [(self.TENANT_WEBHOOK, True)]

    async def test_quiet_hours_and_delay_are_resolved_per_instance(self, db, instance_override) -> None:  # noqa: ARG002
        instance_override(
            ACME_INSTANCE,
            INSTANCE_ALERT_QUIET_HOURS="23:00-07:00",
            INSTANCE_OFFLINE_ALERT_DELAY_SECONDS=999,
        )
        acme = await DBAdapterInstance.get(instance_key=ACME_INSTANCE)
        rival = await DBAdapterInstance.get(instance_key=RIVAL_INSTANCE)
        midnight = datetime.datetime(2026, 8, 11, 23, 30)

        assert instance_health_service._in_quiet_hours(midnight, acme) is True  # noqa: SLF001
        assert instance_health_service._in_quiet_hours(midnight, rival) is False  # noqa: SLF001
        assert instance_health_service._alert_delay_seconds(acme) == 999  # noqa: SLF001
        assert instance_health_service._alert_delay_seconds(rival) == max(  # noqa: SLF001
            0,
            config.INSTANCE_OFFLINE_ALERT_DELAY_SECONDS,
        )

    async def test_health_snapshot_reports_the_effective_delay(self, db, instance_override) -> None:  # noqa: ARG002
        instance_override(ACME_INSTANCE, INSTANCE_OFFLINE_ALERT_DELAY_SECONDS=999)

        result = await health_router.get_instance_health(_current_user=platform_owner())
        delays = {item.instance_key: item.alert_delay_seconds for item in result.items}

        assert delays[ACME_INSTANCE] == 999
        assert delays[RIVAL_INSTANCE] == max(0, config.INSTANCE_OFFLINE_ALERT_DELAY_SECONDS)

    async def test_the_alert_carries_the_instance_to_the_dispatcher(
        self,
        monkeypatch: pytest.MonkeyPatch,
        db,  # noqa: ARG002
    ) -> None:
        """接线：零件对了不等于调用点还在

        `_notify` 不把实例传下去的话，上面每一条解析用例都还是绿的，
        而线上「客户 A 的 Bot 掉线」照样只发到平台方那一个收件端。
        """
        seen: List[Tuple[str, str]] = []

        async def fake_dispatch(title: str, body: str, level: str = "info", **kwargs: str) -> Dict[str, bool]:
            seen.append((kwargs.get("adapter_key", ""), kwargs.get("instance_key", "")))
            return {}

        monkeypatch.setattr("nekro_agent.services.console.instance_health.dispatch_notification", fake_dispatch)
        acme = await DBAdapterInstance.get(instance_key=ACME_INSTANCE)

        await instance_health_service._notify(acme, title="t", body="b", level="warning")  # noqa: SLF001

        assert seen == [(ADAPTER_KEY, ACME_INSTANCE)]


# ===========================================================================
# B7 的写入侧：租户配自己的 Webhook
# ===========================================================================


WEBHOOK_KEY = "INSTANCE_ALERT_WEBHOOK_URL"

# 出站校验会真的做一次 `getaddrinfo`。这里一律用**字面 IP**，让用例不依赖 DNS：
# 字面量走不到解析器，判定完全落在"这个 IP 是不是公网"这一条上。
# 93.184.216.34 是 example.com 长期公示的地址，`is_global` 为真；
# 私网那几个同样是字面量，不会因为跑在没有网络的机器上而变红。
PUBLIC_WEBHOOK = "https://93.184.216.34/alert"


class TestTenantMayOwnItsAlertWebhook:
    """凭据归属跟着作用域走：写在自己实例层上的地址是他自己的

    这一项必须同时满足两条看起来矛盾的要求：
    - 按凭据脱敏（Slack / 飞书的 webhook URL 就是口令，不脱敏则租户能从"继承值"里读到
      平台方那条全局地址）；
    - 租户能写自己实例层上的那份，否则 B7 白做。
    """

    @pytest.fixture
    def scoped_config(self, instance_override):
        instance_override(ACME_INSTANCE, INSTANCE_ALERT_WEBHOOK_URL="")
        return instance_override_config_key(ADAPTER_KEY, ACME_INSTANCE)

    def test_the_field_carries_the_three_markers(self) -> None:
        """三个标记缺一不可，缺哪个都会让下面某一条用例失去意义"""
        from nekro_agent.core.config import CoreConfig

        extra = CoreConfig.model_fields[WEBHOOK_KEY].json_schema_extra
        assert isinstance(extra, dict)
        assert extra.get("overridable") is True
        assert extra.get("is_secret") is True
        assert extra.get("scope_owned_secret") is True
        assert extra.get("is_outbound_url") is True

    async def test_tenant_can_write_it_on_its_own_instance_layer(self, db, scoped_config: str) -> None:  # noqa: ARG002
        await _assert_tenant_may_write(acme_admin(), scoped_config, {WEBHOOK_KEY: PUBLIC_WEBHOOK})

    async def test_tenant_cannot_write_it_on_the_shared_system_layer(self, db) -> None:  # noqa: ARG002
        """全局那条对所有租户同时生效，写它等于替别人做决定"""
        with pytest.raises(PermissionDeniedError):
            await _assert_tenant_may_write(acme_admin(), "system", {WEBHOOK_KEY: PUBLIC_WEBHOOK})

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("http://127.0.0.1:8021/api/config/list", id="loopback"),
            pytest.param("http://169.254.169.254/latest/meta-data/", id="cloud-metadata"),
            pytest.param("http://10.0.0.5/hook", id="private-range"),
            pytest.param("file:///etc/passwd", id="non-http-scheme"),
        ],
    )
    async def test_a_private_webhook_is_refused_at_write(self, db, scoped_config: str, url: str) -> None:  # noqa: ARG002
        """租户填的地址等于让 NA 代为发起出站请求，必须强制公网"""
        with pytest.raises(ValidationError):
            await _assert_tenant_may_write(acme_admin(), scoped_config, {WEBHOOK_KEY: url})

    @pytest.mark.parametrize("value", ["", "********"])
    async def test_empty_and_mask_are_not_outbound_targets(self, db, scoped_config: str, value: str) -> None:  # noqa: ARG002
        """空串是"停用"，掩码是"保持不变"，两者都不产生新的出站目标"""
        await _assert_tenant_may_write(acme_admin(), scoped_config, {WEBHOOK_KEY: value})

    async def test_a_plain_secret_on_the_same_layer_is_still_refused(self, db, scoped_config: str) -> None:  # noqa: ARG002
        """例外只对显式标了 `scope_owned_secret` 的项开，不是"覆盖层上所有 secret 都放行"

        判据落在字段名兜底那一档（`_CREDENTIAL_NAME_PATTERN`），用一个真实存在、
        同一层上的凭据字段做对照：`_is_scope_owned_write` 退化成"只看层不看标记"时这条会红。
        """
        with pytest.raises(PermissionDeniedError):
            await _assert_tenant_may_write(acme_admin(), scoped_config, {"SANDBOX_ONEBOT_SERVER_ACCESS_TOKEN": "stolen"})

    @pytest.mark.parametrize(
        ("config_key", "item", "expected"),
        [
            pytest.param(
                f"instance_override_{ADAPTER_KEY}__{ACME_INSTANCE}",
                {"scope_owned_secret": True},
                True,
                id="instance-layer-marked",
            ),
            pytest.param("channel_config_x", {"scope_owned_secret": True}, True, id="channel-layer-marked"),
            pytest.param("system", {"scope_owned_secret": True}, False, id="system-layer-marked"),
            pytest.param(f"adapter_override_{ADAPTER_KEY}", {"scope_owned_secret": True}, False, id="adapter-layer-marked"),
            pytest.param(
                f"instance_override_{ADAPTER_KEY}__{ACME_INSTANCE}",
                {"is_secret": True},
                False,
                id="instance-layer-unmarked",
            ),
            pytest.param(f"instance_override_{ADAPTER_KEY}__{ACME_INSTANCE}", None, False, id="unknown-item"),
        ],
    )
    def test_the_exception_needs_both_the_marker_and_an_override_layer(
        self,
        config_key: str,
        item: Optional[Dict[str, Any]],
        expected: bool,
    ) -> None:
        from nekro_agent.routers.config import _is_scope_owned_write

        assert _is_scope_owned_write(config_key, item) is expected
