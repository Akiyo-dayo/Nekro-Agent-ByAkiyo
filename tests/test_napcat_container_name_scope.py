"""内置 NapCat 容器名带上部署前缀：`deployment_scope()` 派生 + 过渡期双识别（名字这一半）

## 这份测试在守什么

此前已经把**归属标签**切到了 `deployment_scope()`；这里把**容器名**也切过去：
`build_container_name()` 从裸模板名 `nekro_napcat_{instance_key}` 换成
`{deployment_scope()}nekro_napcat_{instance_key}`。名字是 Docker 的**宿主全局**命名空间，
两套部署上同名 `instance_key` 时裸模板名逐字相同——后建的一方 `create_container` 抛 409，
且（在 #46 标签那一半修掉之前）归属判定会互相接管。前缀让两套部署的名字结构上不可能相同。

和标签那一半一样，直接切会让**升级前建的存量容器当场失联**：所有按名取容器的路径只认
新名的话，启 / 停 / 重启 / 删 / 日志 / 扫码 / 快速登录 / 登录账号推导在升级当天集体 404。
所以按名取容器一律走 `container_name_candidates()`（新名在前、裸模板名在后），`_assert_owned`
的名字维度对新旧两个模板名都放行。这份文件盯四条契约：

| 契约 | 把它改回去 | 该红的用例 |
|---|---|---|
| 新建用带前缀的新名 | `build_container_name` 去掉前缀 | `TestNewNamesCarryTheDeploymentScope` 整组 |
| 双部署不撞名 | 同上 | `TestTwoDeploymentsCreateSideBySide` |
| 旧名容器在所有路径上仍可达 | 候选表 / 名字双识别任一处删掉旧名 | `TestLegacyNamedContainersStayReachable` 整组 |
| 双识别不放串扰进来 | 名字双识别放行后标签 / 挂载判据被顺带删掉 | `TestTheNeighboursBareNamedContainer` 整组 |

## 取材原则

与 `tests/test_napcat_container_owner_tag.py` 一致：表驱动、显式输入、精确期望；
环境派生值一律调产品自己的函数现算（§2.9）；路径落在 `tmp_path` 下。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from aiodocker.exceptions import DockerError
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11 import routers as ob_routers
from nekro_agent.core.os_env import OsEnv, deployment_scope
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.schemas.console import ContainerState
from nekro_agent.schemas.errors import ConflictError, PermissionDeniedError
from nekro_agent.services.console import container_manager
from nekro_agent.services.console.napcat_orphans import OrphanVerdict, plan_napcat_orphan_cleanup

INSTANCE_KEY = "inst1"
LEGACY_NAME = "nekro_napcat_inst1"
"""升级前的裸模板容器名（两套部署逐字相同——这正是它必须被替换掉的原因）。"""

ADAPTER_LEGACY_NAME = "nekro_napcat"
"""适配器配置 `NAPCAT_CONTAINER_NAME` 的默认值：单实例时代的容器名，与模板名无关。"""

ACME_TENANT_ID = 1


# ---------------------------------------------------------------------------
# 环境与路径
# ---------------------------------------------------------------------------


@pytest.fixture
def deploy_a(tmp_path: Path) -> Path:
    """本部署的数据目录"""
    path = tmp_path / "na-a" / "data" / "nekro_agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def deploy_b(tmp_path: Path) -> Path:
    """同一台宿主上的另一套部署"""
    path = tmp_path / "na-b" / "data" / "nekro_agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def here(monkeypatch: pytest.MonkeyPatch, deploy_a: Path) -> Path:
    """把进程摆到「部署 A、未设 NEKRO_INSTANCE_NAME」这个默认形态上"""
    monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(deploy_a))
    return deploy_a


def scoped_name_of(monkeypatch: pytest.MonkeyPatch, data_dir: Path, instance_key: str = INSTANCE_KEY) -> str:
    """某套部署会给该实例起的**当前口径**容器名（调产品自己的函数现算，§2.9）"""
    with monkeypatch.context() as patch:
        patch.setattr(OsEnv, "INSTANCE_NAME", "")
        patch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        return container_manager.build_container_name(instance_key)


def mount_points_of(monkeypatch: pytest.MonkeyPatch, data_dir: Path, instance_key: str) -> Dict[str, Path]:
    """某套部署会给该实例挂上的「容器内目标 → 宿主机源」"""
    with monkeypatch.context() as patch:
        patch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        return {
            container_manager.CONTAINER_QQ_STATE_DIR: container_manager.get_qq_state_dir(instance_key).resolve(),
            container_manager.CONTAINER_NAPCAT_CONFIG_DIR: container_manager.get_napcat_config_dir(instance_key).resolve(),
        }


def make_info(
    *,
    container_name: str,
    instance_label: Optional[str] = INSTANCE_KEY,
    owner_label: Optional[str] = None,
    mounts: Optional[Dict[str, Path]] = None,
) -> Dict[str, Any]:
    """拼一份 `docker inspect` 形态的容器信息（`mounts=None` 表示读不到挂载信息）"""
    labels: Dict[str, str] = {}
    if instance_label is not None:
        labels[container_manager.LABEL_INSTANCE] = instance_label
    if owner_label is not None:
        labels[container_manager.LABEL_OWNER] = owner_label

    info: Dict[str, Any] = {
        "Name": f"/{container_name}",
        "Config": {"Labels": labels, "Image": "mlikiowa/napcat-docker:latest"},
        "HostConfig": {},
        "State": {"Status": "running", "StartedAt": "2026-01-01T00:00:00Z"},
    }
    if mounts is not None:
        info["Mounts"] = [
            {"Type": "bind", "Source": str(source), "Destination": destination}
            for destination, source in mounts.items()
        ]
    return info


def legacy_named_container_of(
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    instance_key: str = INSTANCE_KEY,
) -> Dict[str, Any]:
    """一套部署在**升级前**会建出来的容器：裸模板名 + 旧归属标签 + 本部署挂载"""
    with monkeypatch.context() as patch:
        patch.setattr(OsEnv, "INSTANCE_NAME", "")
        patch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        return make_info(
            container_name=container_manager.build_legacy_container_name(instance_key),
            owner_label=container_manager.get_legacy_owner_tag(),
            mounts=mount_points_of(monkeypatch, data_dir, instance_key),
        )


# ---------------------------------------------------------------------------
# Docker 替身（一台共享的「宿主」，两套部署的容器都摆在同一个名字空间里）
# ---------------------------------------------------------------------------


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

    async def log(self, **kwargs: Any) -> List[str]:
        self.calls.append(f"log:{kwargs.get('tail')}")
        return [f"line-from-{self.name}"]


class _FakeContainers:
    def __init__(self, state: Dict[str, Any]) -> None:
        self._state = state

    async def get(self, name: str) -> _FakeContainer:
        container = self._state["containers"].get(name)
        if container is None:
            raise DockerError(404, {"message": f"No such container: {name}"})
        return container

    async def list(self, all: bool = False) -> List[_FakeContainer]:  # noqa: A002, FBT001, FBT002
        return list(self._state["containers"].values())

    async def create(self, config: Dict[str, Any], name: str) -> _FakeContainer:
        self._state["created"].append((name, config))
        host_config = config.get("HostConfig") or {}
        container = _FakeContainer(
            name,
            {
                "Name": f"/{name}",
                "Config": {"Labels": dict(config.get("Labels") or {}), "Image": config.get("Image", "")},
                "HostConfig": {
                    "Binds": list(host_config.get("Binds") or []),
                    "PortBindings": host_config.get("PortBindings") or {},
                },
                "State": {"Status": "running", "StartedAt": "2026-01-01T00:00:00Z"},
            },
        )
        self._state["containers"][name] = container
        return container


class _FakeImages:
    async def inspect(self, image: str) -> Dict[str, Any]:  # noqa: ARG002 - 与 aiodocker 签名一致
        return {"Id": "sha256:" + "0" * 64}


class _FakeDocker:
    def __init__(self, state: Dict[str, Any]) -> None:
        self.containers = _FakeContainers(state)
        self.images = _FakeImages()

    async def close(self) -> None:
        return None


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """接管 `aiodocker.Docker`（container_manager 与 napcat_orphans 共享这一份宿主状态）"""
    import aiodocker

    state: Dict[str, Any] = {"containers": {}, "created": []}

    def factory(*_args: Any, **_kwargs: Any) -> _FakeDocker:
        return _FakeDocker(state)

    monkeypatch.setattr(aiodocker, "Docker", factory)
    return state


def put_container(docker_state: Dict[str, Any], info: Dict[str, Any]) -> _FakeContainer:
    name = str(info["Name"]).lstrip("/")
    container = _FakeContainer(name, info)
    docker_state["containers"][name] = container
    return container


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key=INSTANCE_KEY,
            display_name=INSTANCE_KEY,
            status="online",
            provider="builtin_napcat",
            tenant_id=ACME_TENANT_ID,
            metadata_json="",
        )
    finally:
        await Tortoise.close_connections()


# ===========================================================================
# 一、新建容器名带部署前缀
# ===========================================================================


class TestNewNamesCarryTheDeploymentScope:
    """`build_container_name` 是新建容器名的唯一来源，必须自带部署身份"""

    def test_new_name_is_scope_plus_legacy_template(self, here: Path) -> None:  # noqa: ARG002
        current = container_manager.build_container_name(INSTANCE_KEY)
        legacy = container_manager.build_legacy_container_name(INSTANCE_KEY)
        assert current == f"{deployment_scope()}{legacy}"
        # 对照：旧口径与部署身份无关，两套部署逐字相同
        assert legacy == LEGACY_NAME

    def test_new_name_matches_docker_charset(self, here: Path) -> None:  # noqa: ARG002
        name = container_manager.build_container_name(INSTANCE_KEY)
        assert container_manager._DOCKER_NAME_PATTERN.match(name)  # noqa: SLF001 - 钉住拼接结果可用

    @pytest.mark.parametrize("instance_name", ["na_a_", "r20verify"])
    def test_explicit_instance_name_becomes_the_prefix(
        self,
        monkeypatch: pytest.MonkeyPatch,
        deploy_a: Path,
        instance_name: str,
    ) -> None:
        """显式 `NEKRO_INSTANCE_NAME` 优先于数据目录派生，与归属标签同一套口径"""
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(deploy_a))
        assert container_manager.build_container_name(INSTANCE_KEY) == f"{instance_name}{LEGACY_NAME}"

    def test_invalid_scope_is_rejected_before_reaching_docker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        deploy_a: Path,
    ) -> None:
        """`NEKRO_INSTANCE_NAME` 带非法字符时在这里 400，而不是守护进程里一句难定位的错"""
        from nekro_agent.schemas.errors import ValidationError

        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "我的/部署")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(deploy_a))
        with pytest.raises(ValidationError):
            container_manager.build_container_name(INSTANCE_KEY)

    def test_candidate_table_orders_new_before_legacy(self, here: Path) -> None:  # noqa: ARG002
        """候选表顺序就是升级语义：新名在前（新部署一次命中），裸模板名兜底（存量容器）"""
        assert container_manager.container_name_candidates(INSTANCE_KEY) == [
            container_manager.build_container_name(INSTANCE_KEY),
            LEGACY_NAME,
        ]

    async def test_create_uses_the_scoped_name_and_records_it(
        self,
        here: Path,  # noqa: ARG002
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        """创建侧三件事都用当前口径：Docker create 的名字、日志、写进实例元数据的名字"""
        await container_manager.create_container(db, image_tag="latest", auto_pull=False)

        assert [name for name, _config in docker["created"]] == [
            container_manager.build_container_name(INSTANCE_KEY),
        ]
        assert container_manager.read_container_metadata(db).container_name == container_manager.build_container_name(INSTANCE_KEY)


# ===========================================================================
# 二、双部署不撞名
# ===========================================================================


class TestTwoDeploymentsCreateSideBySide:
    """本条目的正面判据：同一台宿主、同一个 instance_key，两套部署各自的容器都能建出来"""

    def test_two_deployments_get_different_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
        deploy_a: Path,
        deploy_b: Path,
    ) -> None:
        """名字这一维自己就要能把两套部署分开；对照是旧口径的逐字相同"""
        assert scoped_name_of(monkeypatch, deploy_a) != scoped_name_of(monkeypatch, deploy_b)
        assert container_manager.build_legacy_container_name(INSTANCE_KEY) == LEGACY_NAME

    async def test_both_deployments_create_on_the_same_host(
        self,
        monkeypatch: pytest.MonkeyPatch,
        deploy_a: Path,
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,  # noqa: ARG002 - 两套部署各自的库在测试里共用一行，只影响元数据
    ) -> None:
        """改造前：后建的一方在这里撞 Docker 全局容器名（409）。现在两个容器并存。"""
        names: List[str] = []
        for data_dir in (deploy_a, deploy_b):
            with monkeypatch.context() as patch:
                patch.setattr(OsEnv, "INSTANCE_NAME", "")
                patch.setattr(OsEnv, "DATA_DIR", str(data_dir))
                await container_manager.create_container(db, image_tag="latest", auto_pull=False)
            names.append(scoped_name_of(monkeypatch, data_dir))

        assert len(docker["containers"]) == 2
        assert names[0] != names[1]

        # 部署 B 视角里「这个实例的容器」就是它自己那个；A 的容器不在 B 的候选表里，
        # 一次 stop 之后 A 的容器一个动作都没吃到（名字这一维已经把两套部署分开）
        with monkeypatch.context() as patch:
            patch.setattr(OsEnv, "INSTANCE_NAME", "")
            patch.setattr(OsEnv, "DATA_DIR", str(deploy_b))
            await container_manager.stop_container(db)
        assert docker["containers"][names[0]].calls == ["start"]
        assert docker["containers"][names[1]].calls == ["start", "stop:15"]


# ===========================================================================
# 三、升级路径：裸模板名的存量容器在所有按名取容器的路径上仍可达
# ===========================================================================


class TestLegacyNamedContainersStayReachable:
    """升级前建的容器（裸模板名 + 旧标签 + 本部署挂载）一个动作都不能少

    漏掉任何一条按名取容器的路径，那条路径上就是「升级当天 404」的线上事故。
    逐路径列表见 `container_name_candidates` 的 docstring。
    """

    @staticmethod
    async def _drive(action: str, instance: DBAdapterInstance, monkeypatch: pytest.MonkeyPatch) -> None:
        if action == "start":
            await container_manager.start_container(instance)
        elif action == "stop":
            await container_manager.stop_container(instance)
        elif action == "restart":

            async def _noop(_instance: DBAdapterInstance) -> None:
                return None

            monkeypatch.setattr(container_manager, "resync_network_config", _noop)
            await container_manager.restart_container(instance)
        elif action == "delete":
            await container_manager.delete_container(instance)
        elif action == "logs":
            await container_manager._read_container_logs(instance, tail=7)  # noqa: SLF001 - 扫码兜底走的就是它
        else:  # pragma: no cover - 参数化表里没有别的取值
            raise AssertionError(action)

    PATHS = ("start", "stop", "restart", "delete", "logs")

    @staticmethod
    def recorded_action(path: str) -> str:
        """fake 容器上记下的动作名（aiodocker 的日志方法叫 `log`，不叫 `logs`）"""
        return "log" if path == "logs" else path

    @pytest.mark.parametrize("path", PATHS)
    async def test_lifecycle_and_log_paths_still_reach_the_legacy_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
        path: str,
    ) -> None:
        """写侧全族（启/停/重启/删）+ 扫码兜底的日志读取"""
        container = put_container(docker, legacy_named_container_of(monkeypatch, here))
        await self._drive(path, db, monkeypatch)
        assert [call.split(":", 1)[0] for call in container.calls] == [self.recorded_action(path)]

    async def test_read_side_reports_state_not_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        """`get_container_info`：存量容器照常读到状态（扫码 / 快速登录的「容器在不在跑」判定）"""
        put_container(docker, legacy_named_container_of(monkeypatch, here))

        info = await container_manager.get_container_info(db)

        assert info is not None
        assert info.state == ContainerState.RUNNING
        assert info.name == LEGACY_NAME

    async def test_read_side_reports_absent_when_nothing_is_there(
        self,
        here: Path,  # noqa: ARG002
        docker: Dict[str, Any],  # noqa: ARG002
        db: DBAdapterInstance,
    ) -> None:
        """对照：宿主上什么都没有时按「不存在」回答，提示用的是当前口径名"""
        info = await container_manager.get_container_info(db)
        assert info is not None
        assert info.state == ContainerState.ABSENT
        assert info.name == container_manager.build_container_name(INSTANCE_KEY)

    async def test_create_conflicts_with_our_own_legacy_container(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        """本部署的存量容器还在（裸模板名）时，创建必须拒绝——否则同一个实例两个容器"""
        put_container(docker, legacy_named_container_of(monkeypatch, here))

        with pytest.raises(ConflictError):
            await container_manager.create_container(db, image_tag="latest", auto_pull=False)
        assert docker["created"] == []

    async def test_create_ignores_the_neighbours_grab_of_the_bare_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        """邻居占着裸模板名不拦新建：新容器带部署前缀，两者不再共享命名空间（本修复的目的）"""
        neighbour = put_container(docker, legacy_named_container_of(monkeypatch, deploy_b))

        await container_manager.create_container(db, image_tag="latest", auto_pull=False)

        assert [name for name, _config in docker["created"]] == [
            container_manager.build_container_name(INSTANCE_KEY),
        ]
        assert neighbour.calls == []


class TestLegacyNamedContainersOnTheAdapterRoutes:
    """第二个出口：`/container/{status,logs,logs/stream,restart,napcat-token}` 家族"""

    @staticmethod
    def _patch_adapter_legacy_name(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ob_routers, "legacy_container_name", lambda: ADAPTER_LEGACY_NAME)

    async def test_opens_the_bare_template_name_after_the_scoped_one_misses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,  # noqa: ARG002 - `container_name_candidates` 要先 resolve 实例
    ) -> None:
        """候选顺序：带前缀名 404 → 裸模板名命中；`/logs` `/restart` 全走这一个入口"""
        self._patch_adapter_legacy_name(monkeypatch)
        put_container(docker, legacy_named_container_of(monkeypatch, here))

        async with ob_routers.get_container(INSTANCE_KEY) as container:
            assert container.name == LEGACY_NAME

    async def test_scoped_name_wins_when_both_exist(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,  # noqa: ARG002
    ) -> None:
        """删掉重建（收口出口）之后，新名优先，旧的裸名容器即便还在也不抢"""
        self._patch_adapter_legacy_name(monkeypatch)
        put_container(docker, legacy_named_container_of(monkeypatch, here))
        scoped = put_container(
            docker,
            make_info(
                container_name=container_manager.build_container_name(INSTANCE_KEY),
                owner_label=container_manager.get_owner_tag(),
            ),
        )

        async with ob_routers.get_container(INSTANCE_KEY) as container:
            assert container.name == container_manager.build_container_name(INSTANCE_KEY)
        assert scoped.calls == []


class TestLegacyNamedContainersOnTheWebuiPath:
    """容器网络里的 WebUI 基地址：RUN_IN_DOCKER 时按容器名当主机名直连"""

    async def test_resolves_the_bare_name_for_legacy_containers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        """升级前建的容器还顶着裸名，按新名拼 URL 会 DNS 失败——扫码 / 快速登录 /
        登录账号推导（入站链路）全部跟着失效"""
        put_container(docker, legacy_named_container_of(monkeypatch, here))
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)

        base = await container_manager._webui_base_url(db)  # noqa: SLF001 - 入站链路最终都汇到这里

        assert base == f"http://{LEGACY_NAME}:{container_manager.NAPCAT_WEBUI_PORT}"

    async def test_resolves_the_scoped_name_for_fresh_containers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        scoped = put_container(
            docker,
            make_info(
                container_name=container_manager.build_container_name(INSTANCE_KEY),
                owner_label=container_manager.get_owner_tag(),
            ),
        )
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)

        base = await container_manager._webui_base_url(db)  # noqa: SLF001

        assert base == f"http://{scoped.name}:{container_manager.NAPCAT_WEBUI_PORT}"

    async def test_docker_unreachable_falls_back_to_the_current_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],  # noqa: ARG002
        db: DBAdapterInstance,
    ) -> None:
        """与改造前行为形态一致：连不上 Docker 就按当前口径拼一个名字，让连接自己失败"""
        import aiodocker

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise DockerError(500, {"message": "docker socket 不可用"})

        monkeypatch.setattr(aiodocker, "Docker", _boom)
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)

        name = await container_manager._resolve_container_dns_name(INSTANCE_KEY)  # noqa: SLF001

        assert name == container_manager.build_container_name(INSTANCE_KEY)


class TestLegacyNamedContainersInOrphanReclaim:
    """孤儿盘点：库里没有行、宿主上还顶着的存量容器必须照常列出来、删得掉"""

    async def test_orphan_plan_lists_the_bare_named_container(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
    ) -> None:
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
        await Tortoise.generate_schemas()
        try:
            put_container(docker, legacy_named_container_of(monkeypatch, here))

            plan = await plan_napcat_orphan_cleanup()

            candidate = next(c for c in plan.candidates if c.instance_key == INSTANCE_KEY)
            assert candidate.verdict is OrphanVerdict.ORPHANED
            assert candidate.container_name == LEGACY_NAME
        finally:
            await Tortoise.close_connections()

    async def test_orphan_execute_deletes_it_by_the_bare_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
    ) -> None:
        from nekro_agent.services.console.napcat_orphans import execute_napcat_orphan_cleanup

        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
        await Tortoise.generate_schemas()
        try:
            container = put_container(docker, legacy_named_container_of(monkeypatch, here))

            outcome = await execute_napcat_orphan_cleanup(INSTANCE_KEY, remove_data_dir=False)

            assert outcome.container_removed is True
            assert [call.split(":", 1)[0] for call in container.calls] == ["delete"]
        finally:
            await Tortoise.close_connections()


# ===========================================================================
# 四、归属校验对新旧名都成立，且双识别不把串扰放回来
# ===========================================================================


class TestOwnershipAcceptsBothNames:
    """`_assert_owned` / `assert_container_owned` 的名字维度双识别"""

    @pytest.mark.parametrize("expected_name_kind", ["current", "legacy"])
    def test_assert_owned_accepts_both_template_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        expected_name_kind: str,
    ) -> None:
        """调用方两种传法都要通：`_owned_container` 传当前口径名，孤儿回收传实际名"""
        info = legacy_named_container_of(monkeypatch, here)
        expected = (
            container_manager.build_container_name(INSTANCE_KEY)
            if expected_name_kind == "current"
            else container_manager.build_legacy_container_name(INSTANCE_KEY)
        )
        container_manager._assert_owned(info, INSTANCE_KEY, expected)  # noqa: SLF001

    async def test_assert_container_owned_accepts_a_bare_named_handle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
    ) -> None:
        """adapter 路由用的公开出口：拿到的是裸模板名容器时同样成立"""
        container = put_container(docker, legacy_named_container_of(monkeypatch, here))
        await container_manager.assert_container_owned(container, INSTANCE_KEY)

    def test_an_unrelated_name_is_still_refused(self, here: Path) -> None:  # noqa: ARG002
        """名字双识别只放宽到「两个模板名」，别的名字照旧一票否决"""
        info = make_info(
            container_name="totally_different",
            owner_label=container_manager.get_owner_tag(),
        )
        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(  # noqa: SLF001
                info, INSTANCE_KEY, container_manager.build_container_name(INSTANCE_KEY),
            )


class TestTheNeighboursBareNamedContainer:
    """名字这一维认了裸模板名之后，标签 / 挂载判据必须把邻居挡在外面

    裸模板名在两套部署上逐字相同，这是名字双识别最危险的一档：邻居的存量容器就在我们的
    候选表里。每一条都要验证「找得到、但一个动作都落不了地」。
    """

    @pytest.mark.parametrize("path", TestLegacyNamedContainersStayReachable.PATHS)
    async def test_write_paths_are_denied(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
        path: str,
    ) -> None:
        """邻居的容器一个动作都落不了地。

        日志兜底那条（`_read_container_logs`）**刻意吞掉** PermissionDeniedError 返回空列表
        （扫码页要的是状态不是 500），所以它不抛——判据落在「容器上没有留下任何动作」。
        """
        neighbour = put_container(docker, legacy_named_container_of(monkeypatch, deploy_b))
        if path == "logs":
            assert await container_manager._read_container_logs(db, tail=7) == []  # noqa: SLF001
        else:
            with pytest.raises(PermissionDeniedError):
                await TestLegacyNamedContainersStayReachable._drive(path, db, monkeypatch)
        assert neighbour.calls == []

    async def test_read_side_answers_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        put_container(docker, legacy_named_container_of(monkeypatch, deploy_b))
        info = await container_manager.get_container_info(db)
        assert info is not None
        assert info.state == ContainerState.ABSENT

    async def test_adapter_route_is_denied(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,  # noqa: ARG002
    ) -> None:
        """/logs（含 WebUI 令牌）与 /restart 的入口打不开邻居的容器，也不回退到遗留配置名"""
        monkeypatch.setattr(ob_routers, "legacy_container_name", lambda: ADAPTER_LEGACY_NAME)
        neighbour = put_container(docker, legacy_named_container_of(monkeypatch, deploy_b))

        with pytest.raises(PermissionDeniedError):
            async with ob_routers.get_container(INSTANCE_KEY):
                pytest.fail("不应该产出容器")
        assert neighbour.calls == []

    async def test_webui_hostname_resolution_skips_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,  # noqa: ARG002
    ) -> None:
        """按名解析容器网络主机名时跳过不归本部署的候选，回落当前口径名（连接自己失败）"""
        put_container(docker, legacy_named_container_of(monkeypatch, deploy_b))
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)

        name = await container_manager._resolve_container_dns_name(INSTANCE_KEY)  # noqa: SLF001

        assert name == container_manager.build_container_name(INSTANCE_KEY)

    async def test_orphan_plan_does_not_list_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        deploy_b: Path,
        docker: Dict[str, Any],
    ) -> None:
        """邻居的容器不归本部署收口，也不该出现在本部署的孤儿清单里"""
        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
        await Tortoise.generate_schemas()
        try:
            put_container(docker, legacy_named_container_of(monkeypatch, deploy_b))

            plan = await plan_napcat_orphan_cleanup()

            assert INSTANCE_KEY not in {c.instance_key for c in plan.candidates}
        finally:
            await Tortoise.close_connections()
