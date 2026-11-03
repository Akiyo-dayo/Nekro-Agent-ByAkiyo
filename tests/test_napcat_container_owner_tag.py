"""内置 NapCat 容器的归属标签：切到 `deployment_scope()` 的过渡期双识别

## 这份测试在守什么

`container_manager.get_owner_tag()` 从 `OsEnv.INSTANCE_NAME or "default"` 换成了
`core/os_env.py:deployment_scope()`（与 iptables 标记 / 沙盒容器归属标签同一套派生）。
一刀切过去会让**升级前建的存量容器立刻变成「不归本部署所有」**：`_assert_owned` 是
fail-closed 的，控制台对它们的启 / 停 / 重启 / 删全部 403，且没有迁移出口。

所以口径是「**读认新旧两套标签，写只写新的**」。这份文件盯三条契约，每条都配了它对应的变异：

| 契约 | 把它改回去 | 该红的用例 |
|---|---|---|
| 认旧标签 | `_assert_owned` 只认 `get_owner_tag()` | `TestLegacyContainersSurviveTheUpgrade` 整组 |
| 旧标签要加限定 | 删掉 `_container_belongs_to_this_deployment` 那半个条件 | `test_the_neighbours_legacy_container_is_still_refused` |
| 只写新标签 | `create_container` 写回 `get_legacy_owner_tag()` | `TestCreateWritesOnlyTheCurrentTag` 整组 |
| **第二个出口也过同一套判据** | `onebot_v11` 适配器路由不调 `assert_container_owned` | `TestTheAdapterRoutesUseTheSameCheck` 整组 |

## 那个必须先想清楚的坑

旧标签在**两种标准部署下都是逐字相同的 `default`**（`NEKRO_INSTANCE_NAME` 默认空串），
而容器名 `nekro_napcat_{instance_key}` 是**宿主全局**的、且不带部署前缀。也就是说：不加限定的
双识别会让部署 B 用自己库里那行同名 `instance_key` 去停 / 删部署 A 的协议端容器 ——
恰好把 #42 刚关掉的那条串扰路径原样打开。限定落在**挂载路径**上（容器的三个 bind 全部派生自
`OsEnv.DATA_DIR`，而数据目录正是 `deployment_scope()` 的派生来源），见
`_container_belongs_to_this_deployment`。`test_the_neighbours_legacy_container_is_still_refused`
连同它的对照断言（两套部署算出的旧标签逐字相同）就是这一条的验收。

## 取材原则

- 表驱动、显式输入、精确期望；不随机 fuzz、不 sleep、不只断言「没抛异常」。
- 环境派生值一律**调用产品自己的函数**现算，不写默认环境下的字面量（§2.9）。
- 路径一律落在 `tmp_path` 下：`/opt/...` 这类字面量在 Windows 上会被 `resolve()` 变成
  `C:\opt\...`，而 `create_container` 会真的去 `mkdir`。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from aiodocker.exceptions import DockerError
from tortoise import Tortoise

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.console import container_manager
from nekro_agent.services.user.role import Role

INSTANCE_KEY = "inst1"
CONTAINER_NAME = "nekro_napcat_inst1"
"""升级前的裸模板容器名（#46 容器名这一半的过渡期读认对象）。

新建容器已改用 `build_container_name()`（带 deployment_scope 前缀），名字与
标签一样读认新旧两套；带前缀名的契约见 `tests/test_napcat_container_name_scope.py`。
"""
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


def owner_tag_of(monkeypatch: pytest.MonkeyPatch, data_dir: Path, instance_name: str = "") -> str:
    """某套部署会**写入**的归属标签（调产品自己的函数现算，§2.9）"""
    with monkeypatch.context() as patch:
        patch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        patch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        return container_manager.get_owner_tag()


def legacy_tag_of(monkeypatch: pytest.MonkeyPatch, data_dir: Path, instance_name: str = "") -> str:
    """某套部署在**升级前**写入的归属标签"""
    with monkeypatch.context() as patch:
        patch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        patch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        return container_manager.get_legacy_owner_tag()


def mount_points_of(monkeypatch: pytest.MonkeyPatch, data_dir: Path, instance_key: str) -> Dict[str, Path]:
    """某套部署会给该实例挂上的「容器内目标 → 宿主机源」

    路径由产品自己的 `get_*_dir()` 算出来，测试不复刻目录布局。
    """
    with monkeypatch.context() as patch:
        patch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        return {
            container_manager.CONTAINER_DATA_DIR: Path(OsEnv.DATA_DIR).resolve(),
            container_manager.CONTAINER_QQ_STATE_DIR: container_manager.get_qq_state_dir(instance_key).resolve(),
            container_manager.CONTAINER_NAPCAT_CONFIG_DIR: container_manager.get_napcat_config_dir(instance_key).resolve(),
        }


# `Mounts` 与 `HostConfig.Binds` 两种形态都要认：前者是 Docker inspect 的结构化字段，
# 后者是创建时原样传进去的字符串。任何一侧读不出来，存量容器都会落回 403
MountStyle = str
MOUNT_STYLES: Tuple[MountStyle, ...] = ("mounts", "binds", "both")


def make_info(
    *,
    container_name: str = CONTAINER_NAME,
    instance_label: Optional[str] = INSTANCE_KEY,
    owner_label: Optional[str] = None,
    mounts: Optional[Dict[str, Path]] = None,
    style: MountStyle = "both",
    bind_options: str = "",
) -> Dict[str, Any]:
    """拼一份 `docker inspect` 形态的容器信息

    `mounts=None` 表示「读不到任何挂载信息」——inspect 结果被裁剪、或容器根本不是 NA 建的。
    """
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
    if mounts is None:
        return info

    if style in ("mounts", "both"):
        info["Mounts"] = [
            {"Type": "bind", "Source": str(source), "Destination": destination}
            for destination, source in mounts.items()
        ]
    if style in ("binds", "both"):
        info["HostConfig"]["Binds"] = [
            f"{source}:{destination}{bind_options}" for destination, source in mounts.items()
        ]
    return info


# ---------------------------------------------------------------------------
# Docker 替身
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


class _FakeContainers:
    def __init__(self, state: Dict[str, Any]) -> None:
        self._state = state

    async def get(self, name: str) -> _FakeContainer:
        container = self._state["containers"].get(name)
        if container is None:
            raise DockerError(404, {"message": f"No such container: {name}"})
        return container

    async def create(self, config: Dict[str, Any], name: str) -> _FakeContainer:
        self._state["created"].append((name, config))
        # inspect 结果按**创建时传进去的那份配置**拼，不另编一套：这样 `create → _assert_owned`
        # 这条来回是真的走完了标签与挂载两条判据。刻意只给 `HostConfig.Binds`、不给 `Mounts`，
        # 让来回用例落在「Docker 只回 Binds」那一侧
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
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """接管 `aiodocker.Docker()`；`_docker_client()` 每次操作新建一个，共享同一份宿主状态"""
    state: Dict[str, Any] = {"containers": {}, "created": [], "clients": []}

    def factory(*_args: Any, **_kwargs: Any) -> _FakeDocker:
        client = _FakeDocker(state)
        state["clients"].append(client)
        return client

    monkeypatch.setattr(container_manager.aiodocker, "Docker", factory)
    return state


def _platform_owner() -> DBUser:
    """平台方身份：`@require_role(Role.Super)` 只读 `perm_level`

    这一组用它是为了走到端点函数体里（越权判定与角色无关，`Role.Super` 也照样被拒）。
    """
    return DBUser(
        username="platform-owner",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


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
# 标签取值本身
# ===========================================================================


class TestOwnerTagFollowsTheDeploymentScope:
    """写入用的标签必须是 `deployment_scope()` 派生的那一个"""

    def test_owner_tag_is_the_deployment_scope(self, monkeypatch: pytest.MonkeyPatch, here: Path) -> None:
        from nekro_agent.core.os_env import deployment_scope

        assert container_manager.get_owner_tag() == deployment_scope()
        # 对照：旧口径在这里给的是与数据目录无关的 `default`
        assert container_manager.get_legacy_owner_tag() == container_manager.LEGACY_OWNER_TAG_FALLBACK
        assert container_manager.get_owner_tag() != container_manager.get_legacy_owner_tag()
        assert owner_tag_of(monkeypatch, here) == container_manager.get_owner_tag()

    @pytest.mark.parametrize("instance_name", ["na_a_", "r16verify", "tenant_a-"])
    def test_explicit_instance_name_makes_both_tags_identical(
        self,
        monkeypatch: pytest.MonkeyPatch,
        deploy_a: Path,
        instance_name: str,
    ) -> None:
        """显式设了 `NEKRO_INSTANCE_NAME` 的部署标签根本没变，双识别对它等于没启用"""
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(deploy_a))
        assert container_manager.get_owner_tag() == instance_name
        assert container_manager.get_legacy_owner_tag() == instance_name

    def test_two_deployments_get_different_owner_tags(
        self,
        monkeypatch: pytest.MonkeyPatch,
        deploy_a: Path,
        deploy_b: Path,
    ) -> None:
        """本条目的核心：标签这一维必须**自己**就能把两套部署分开

        对照的另一半是旧口径 —— 它与数据目录无关，两套部署逐字相同，
        那正是「B 能停掉 A 的协议端容器」的成因。
        """
        assert owner_tag_of(monkeypatch, deploy_a) != owner_tag_of(monkeypatch, deploy_b)
        assert legacy_tag_of(monkeypatch, deploy_a) == legacy_tag_of(monkeypatch, deploy_b) == "default"


# ===========================================================================
# `_assert_owned`：当前标签这一档
# ===========================================================================


class TestCurrentTagIsSelfSufficient:
    """带当前标签的容器无需任何挂载证据即可放行

    这正是双识别能收口的原因：新标签自带部署身份，挂载路径只是替旧标签补身份的拐杖。
    """

    def test_current_tag_alone_authorises(self, here: Path) -> None:  # noqa: ARG002
        info = make_info(owner_label=container_manager.get_owner_tag(), mounts=None)
        container_manager._assert_owned(info, INSTANCE_KEY, CONTAINER_NAME)

    def test_the_neighbours_current_tag_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        deploy_b: Path,
    ) -> None:
        info = make_info(owner_label=owner_tag_of(monkeypatch, deploy_b), mounts=None)
        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(info, INSTANCE_KEY, CONTAINER_NAME)

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            pytest.param({"instance_label": "other"}, "实例标签不符", id="instance-label-mismatch"),
            pytest.param({"instance_label": None}, "实例标签缺失", id="instance-label-missing"),
            pytest.param({"owner_label": None}, "归属标签缺失", id="owner-label-missing"),
            pytest.param({"owner_label": ""}, "归属标签是空串", id="owner-label-empty"),
            pytest.param({"container_name": "nekro_napcat_other"}, "容器名不符", id="name-mismatch"),
        ],
    )
    def test_the_other_guards_are_unchanged(
        self,
        here: Path,  # noqa: ARG002
        kwargs: Dict[str, Any],
        reason: str,
    ) -> None:
        """双识别只放宽了归属标签这一维，容器名与实例标签仍然一票否决"""
        payload: Dict[str, Any] = {"owner_label": container_manager.get_owner_tag(), "mounts": None}
        payload.update(kwargs)
        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(make_info(**payload), INSTANCE_KEY, CONTAINER_NAME)


# ===========================================================================
# `_assert_owned`：旧标签这一档（过渡期）
# ===========================================================================


class TestLegacyContainersSurviveTheUpgrade:
    """升级前建的容器仍然可以被本部署管理

    把「认旧标签」删掉，这一整组都会红 —— 那正是不做双识别时用户会遇到的形态：
    控制台上每一个存量 NapCat 容器的启 / 停 / 重启 / 删都是 403。
    """

    @pytest.mark.parametrize("style", MOUNT_STYLES)
    def test_legacy_tag_plus_our_mounts_is_accepted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        style: MountStyle,
    ) -> None:
        info = make_info(
            owner_label=container_manager.get_legacy_owner_tag(),
            mounts=mount_points_of(monkeypatch, here, INSTANCE_KEY),
            style=style,
        )
        container_manager._assert_owned(info, INSTANCE_KEY, CONTAINER_NAME)

    @pytest.mark.parametrize("bind_options", ["", ":ro", ":rw,z"])
    def test_bind_strings_with_mount_options_are_still_parsed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        bind_options: str,
    ) -> None:
        """`源:目标:选项` 的第三段不能把目标匹配打偏"""
        info = make_info(
            owner_label=container_manager.get_legacy_owner_tag(),
            mounts=mount_points_of(monkeypatch, here, INSTANCE_KEY),
            style="binds",
            bind_options=bind_options,
        )
        container_manager._assert_owned(info, INSTANCE_KEY, CONTAINER_NAME)

    def test_a_windows_style_source_is_not_split_on_the_drive_colon(self, here: Path) -> None:  # noqa: ARG002
        """按 `:` 切 Binds 会把 `C:\\srv\\na` 切成 `C` —— 这里钉住「按目标定位」这个解法

        Windows 上产品代码本来就会算出带盘符的源路径；Linux 上这条同样有意义：它证明解析
        不依赖冒号计数。
        """
        source = "C:\\srv\\na\\data\\napcat_data\\inst1\\napcat"
        info = make_info(
            owner_label=container_manager.get_legacy_owner_tag(),
            mounts=None,
            style="binds",
        )
        info["HostConfig"]["Binds"] = [f"{source}:{container_manager.CONTAINER_NAPCAT_CONFIG_DIR}:ro"]
        assert container_manager._bind_source(info, container_manager.CONTAINER_NAPCAT_CONFIG_DIR) == source

    def test_mounts_field_wins_over_binds(self, monkeypatch: pytest.MonkeyPatch, here: Path) -> None:
        """两个字段都在时以结构化的 `Mounts` 为准（Docker 会把它规范化）"""
        mounts = mount_points_of(monkeypatch, here, INSTANCE_KEY)
        info = make_info(owner_label=container_manager.get_legacy_owner_tag(), mounts=mounts, style="both")
        info["HostConfig"]["Binds"] = [f"/somewhere/else:{container_manager.CONTAINER_NAPCAT_CONFIG_DIR}"]
        container_manager._assert_owned(info, INSTANCE_KEY, CONTAINER_NAME)

    # ---- 限定条件：下面这些必须仍然 403 ----------------------------------

    def test_the_neighbours_legacy_container_is_still_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        deploy_b: Path,
    ) -> None:
        """**本条目最重要的一条**：双识别不能把 #42 关掉的串扰路径重新打开

        场景是真实的：容器名 `nekro_napcat_inst1` 是宿主全局的且不带部署前缀，两套部署库里
        都可能有一行 `inst1`。部署 B 去 inspect 时看到的就是部署 A 建的那个容器。
        """
        legacy_here = container_manager.get_legacy_owner_tag()
        # 对照①：两套部署的旧标签逐字相同 —— 标签这一维在这里**没有**任何区分度
        assert legacy_here == legacy_tag_of(monkeypatch, deploy_b)

        neighbour_info = make_info(
            owner_label=legacy_here,
            mounts=mount_points_of(monkeypatch, deploy_b, INSTANCE_KEY),
        )
        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(neighbour_info, INSTANCE_KEY, CONTAINER_NAME)

        # 对照②：同一条判据、同一个标签，挂载换成本部署的就放行 —— 证明拒绝来自限定条件本身，
        # 而不是这组输入恰好在别处也不匹配
        container_manager._assert_owned(
            make_info(owner_label=legacy_here, mounts=mount_points_of(monkeypatch, here, INSTANCE_KEY)),
            INSTANCE_KEY,
            CONTAINER_NAME,
        )

    def test_legacy_tag_without_any_mount_evidence_is_refused(self, here: Path) -> None:  # noqa: ARG002
        """读不到挂载信息时 fail-closed：宁可让一个存量容器落回 403，也不放行跨部署操作"""
        info = make_info(owner_label=container_manager.get_legacy_owner_tag(), mounts=None)
        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(info, INSTANCE_KEY, CONTAINER_NAME)

    def test_only_the_deployment_wide_mount_is_not_enough(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
    ) -> None:
        """判据必须落在**实例级**挂载上

        `{DATA_DIR}:/app/nekro_agent_data` 只说明「这个容器挂了本部署的数据目录」，
        说不出「它是这一行实例的容器」。只认它等于把 instance_key 这一维交出去。
        """
        mounts = mount_points_of(monkeypatch, here, INSTANCE_KEY)
        info = make_info(
            owner_label=container_manager.get_legacy_owner_tag(),
            mounts={container_manager.CONTAINER_DATA_DIR: mounts[container_manager.CONTAINER_DATA_DIR]},
        )
        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(info, INSTANCE_KEY, CONTAINER_NAME)

    def test_a_mount_pointing_at_another_instance_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
    ) -> None:
        """同一套部署里挂着别的实例目录的容器同样拒绝"""
        info = make_info(
            owner_label=container_manager.get_legacy_owner_tag(),
            mounts=mount_points_of(monkeypatch, here, "inst2"),
        )
        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(info, INSTANCE_KEY, CONTAINER_NAME)

    def test_one_contradicting_mount_vetoes_the_other(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        deploy_b: Path,
    ) -> None:
        """两条实例级挂载有任意一条指向别处即判否，不做「命中一条就算」"""
        ours = mount_points_of(monkeypatch, here, INSTANCE_KEY)
        theirs = mount_points_of(monkeypatch, deploy_b, INSTANCE_KEY)
        info = make_info(
            owner_label=container_manager.get_legacy_owner_tag(),
            mounts={
                container_manager.CONTAINER_NAPCAT_CONFIG_DIR: ours[container_manager.CONTAINER_NAPCAT_CONFIG_DIR],
                container_manager.CONTAINER_QQ_STATE_DIR: theirs[container_manager.CONTAINER_QQ_STATE_DIR],
            },
        )
        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(info, INSTANCE_KEY, CONTAINER_NAME)

    def test_an_unrelated_owner_tag_is_refused_even_with_our_mounts(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
    ) -> None:
        """挂载证据只是旧标签的**附加**条件，不是可以顶掉标签的独立通道"""
        info = make_info(owner_label="somebody-else-", mounts=mount_points_of(monkeypatch, here, INSTANCE_KEY))
        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(info, INSTANCE_KEY, CONTAINER_NAME)


# ===========================================================================
# 接线：破坏性操作真的会走到这套判定上（§2.7）
# ===========================================================================


class TestDestructiveOperationsGoThroughTheCheck:
    """零件对了不等于调用点还在

    `_assert_owned` 单测得再漂亮，也不构成「控制台的启 / 停 / 重启 / 删真的过它」的证据。
    这一组驱动真正的服务层函数，判据是容器上有没有留下动作痕迹。
    """

    @staticmethod
    async def _drive(action: str, instance: DBAdapterInstance, monkeypatch: pytest.MonkeyPatch) -> None:
        if action == "start":
            await container_manager.start_container(instance)
        elif action == "stop":
            await container_manager.stop_container(instance)
        elif action == "restart":
            # 重启会先重放网络配置（读写 DATA_DIR 下的文件），与归属校验无关，换成 no-op
            async def _noop(_instance: DBAdapterInstance) -> None:
                return None

            monkeypatch.setattr(container_manager, "resync_network_config", _noop)
            await container_manager.restart_container(instance)
        elif action == "delete":
            await container_manager.delete_container(instance)
        else:  # pragma: no cover - 参数化表里没有别的取值
            raise AssertionError(action)

    ACTIONS = ("start", "stop", "restart", "delete")

    @pytest.mark.parametrize("action", ACTIONS)
    async def test_a_legacy_container_of_this_deployment_can_still_be_managed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
        action: str,
    ) -> None:
        """存量容器不能因为一次升级就变成「谁都动不了」"""
        container = put_container(
            docker,
            make_info(
                owner_label=container_manager.get_legacy_owner_tag(),
                mounts=mount_points_of(monkeypatch, here, INSTANCE_KEY),
            ),
        )
        await self._drive(action, db, monkeypatch)
        assert [call.split(":", 1)[0] for call in container.calls] == [action]

    @pytest.mark.parametrize("action", ACTIONS)
    async def test_the_neighbours_container_cannot_be_touched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
        action: str,
    ) -> None:
        """同名 instance_key + 旧标签，容器却是邻居那套部署建的 —— 一个动作都不许落地"""
        container = put_container(
            docker,
            make_info(
                owner_label=legacy_tag_of(monkeypatch, deploy_b),
                mounts=mount_points_of(monkeypatch, deploy_b, INSTANCE_KEY),
            ),
        )
        with pytest.raises(PermissionDeniedError):
            await self._drive(action, db, monkeypatch)
        assert container.calls == []

    @pytest.mark.parametrize("action", ACTIONS)
    async def test_a_container_with_the_current_tag_can_be_managed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        docker: Dict[str, Any],
        db: DBAdapterInstance,
        action: str,
    ) -> None:
        container = put_container(docker, make_info(owner_label=container_manager.get_owner_tag(), mounts=None))
        await self._drive(action, db, monkeypatch)
        assert [call.split(":", 1)[0] for call in container.calls] == [action]


# ===========================================================================
# 创建侧：只写新标签
# ===========================================================================


class TestCreateWritesOnlyTheCurrentTag:
    """把这里改回写旧标签，整组都会红

    写回旧口径的后果不是「多写一个字段」：旧标签在两套部署上逐字相同，于是归属判定会**永久**
    退化成只剩挂载路径这一道，过渡期分支再也收不了口。
    """

    async def test_create_writes_the_deployment_scope_tag(
        self,
        here: Path,  # noqa: ARG002
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        await container_manager.create_container(db, image_tag="latest", auto_pull=False)

        assert len(docker["created"]) == 1
        name, config = docker["created"][0]
        # 容器名与标签一样走 deployment_scope() 派生（名字那一半的契约见
        # tests/test_napcat_container_name_scope.py，这里只钉「不再是裸模板名」）
        assert name == container_manager.build_container_name(INSTANCE_KEY)
        assert name != container_manager.build_legacy_container_name(INSTANCE_KEY)
        labels = config["Labels"]
        assert labels[container_manager.LABEL_INSTANCE] == INSTANCE_KEY
        assert labels[container_manager.LABEL_OWNER] == container_manager.get_owner_tag()
        assert labels[container_manager.LABEL_OWNER] != container_manager.get_legacy_owner_tag()

    async def test_created_binds_use_the_documented_mount_points(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        """创建侧的挂载点与归属判定读的那三个常量必须是同一批

        两边分叉时判据会静默退化成「永远读不到挂载」，表现是存量容器全部 403 ——
        而那正是双识别要消灭的症状。
        """
        await container_manager.create_container(db, image_tag="latest", auto_pull=False)
        _name, config = docker["created"][0]
        binds = config["HostConfig"]["Binds"]
        expected = mount_points_of(monkeypatch, here, INSTANCE_KEY)
        assert sorted(binds) == sorted(f"{source}:{destination}" for destination, source in expected.items())

    async def test_the_freshly_created_container_needs_no_mount_evidence(
        self,
        here: Path,  # noqa: ARG002
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        """新建容器的归属由标签**独自**成立

        判据刻意抹掉挂载信息：写旧标签的话，这个容器立刻退回「必须靠挂载路径才认得出」，
        一次裁剪过的 inspect 就能让本部署管不了自己刚建的容器。
        """
        await container_manager.create_container(db, image_tag="latest", auto_pull=False)
        name, config = docker["created"][0]

        label_only = make_info(
            container_name=name,
            owner_label=config["Labels"][container_manager.LABEL_OWNER],
            mounts=None,
        )
        container_manager._assert_owned(label_only, INSTANCE_KEY, name)

    async def test_two_deployments_write_labels_that_tell_them_apart(
        self,
        monkeypatch: pytest.MonkeyPatch,
        deploy_a: Path,
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        """跨部署串扰的正面判据：两套部署写出来的标签必须不同

        对照是旧口径 —— 同样两套部署、同样一个 `instance_key`，它给出逐字相同的 `default`。
        """
        written: List[str] = []
        for data_dir in (deploy_a, deploy_b):
            with monkeypatch.context() as patch:
                patch.setattr(OsEnv, "INSTANCE_NAME", "")
                patch.setattr(OsEnv, "DATA_DIR", str(data_dir))
                docker["containers"].clear()
                docker["created"].clear()
                await container_manager.create_container(db, image_tag="latest", auto_pull=False)
                written.append(docker["created"][0][1]["Labels"][container_manager.LABEL_OWNER])

        assert written[0] != written[1]
        assert legacy_tag_of(monkeypatch, deploy_a) == legacy_tag_of(monkeypatch, deploy_b)

    async def test_the_neighbour_cannot_manage_what_we_just_created(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,
    ) -> None:
        """端到端：本部署建的容器，邻居一个动作都做不了；本部署自己照常

        容器名带上部署前缀之后（#46 名字这一半），邻居的候选表里**根本没有**这个名字，
        于是 404「容器不存在」——比 403 更强：邻居连这个容器的存在都探测不到，
        与项目「越权 404 不泄露存在性」的既定约定一致。顶着裸模板名的存量容器走的是
        另一条路（找得到、归属校验拒），那一条在 `TestDestructiveOperationsGoThroughTheCheck` 里。
        """
        await container_manager.create_container(db, image_tag="latest", auto_pull=False)
        created_name = container_manager.build_container_name(INSTANCE_KEY)
        container = docker["containers"][created_name]

        with monkeypatch.context() as patch:
            patch.setattr(OsEnv, "INSTANCE_NAME", "")
            patch.setattr(OsEnv, "DATA_DIR", str(deploy_b))
            with pytest.raises(NotFoundError):
                await container_manager.stop_container(db)

        assert container.calls == ["start"]
        await container_manager.stop_container(db)
        assert container.calls == ["start", "stop:15"]


# ===========================================================================
# 第二个出口：onebot_v11 适配器的 /container/* 路由
# ===========================================================================


class TestTheAdapterRoutesUseTheSameCheck:
    """同一批容器还有第二条打开方式，它必须过同一套归属校验

    `adapters/onebot_v11/routers.py` 的 `/container/{status,logs,logs/stream,restart,
    napcat-token}` 按同一个模板名自己 `containers.get()`（它还要兼容单实例时代那个没有标签的
    遗留容器名，所以走不了 `_owned_container`）。这批端点里 `/logs` 会把 NapCat 日志原样吐出
    ——**日志里就带着 WebUI 访问令牌**——`/restart` 则直接重启容器，正是 #46 描述的那件事。

    判据落在 `get_container()` 这个所有端点共用的入口上，另配一条「没有第五个端点绕过它」。
    """

    LEGACY_CONTAINER_NAME = "nekro_napcat"

    @staticmethod
    def _adapter_module():
        from nekro_agent.adapters.onebot_v11 import routers as ob_routers

        return ob_routers

    def _patch_legacy_name(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        """遗留容器名来自适配器配置对象，测试里不去加载整个适配器"""
        monkeypatch.setattr(self._adapter_module(), "legacy_container_name", lambda: name)

    async def _open(self, instance_key: Optional[str]) -> str:
        """走一遍 `get_container()`，返回真正打开的那个容器名"""
        ob_routers = self._adapter_module()
        async with ob_routers.get_container(instance_key) as container:
            return str(container.name)

    async def test_the_neighbours_container_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,  # noqa: ARG002
    ) -> None:
        """**本组最重要的一条**：邻居那套部署建的同名容器，这条路也必须打不开

        打开了就等于把 `/logs`（含 WebUI 令牌）与 `/restart` 交给邻居的后台。
        """
        self._patch_legacy_name(monkeypatch, self.LEGACY_CONTAINER_NAME)
        neighbour = put_container(
            docker,
            make_info(
                owner_label=legacy_tag_of(monkeypatch, deploy_b),
                mounts=mount_points_of(monkeypatch, deploy_b, INSTANCE_KEY),
            ),
        )
        with pytest.raises(PermissionDeniedError):
            await self._open(INSTANCE_KEY)
        # 拒绝之后不许悄悄退回遗留名去开另一个容器
        assert neighbour.calls == []

    @pytest.mark.parametrize("tag", ["current", "legacy"])
    async def test_our_own_container_still_opens(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,  # noqa: ARG002
        tag: str,
    ) -> None:
        """本部署自己的容器照常打开——当前标签与过渡期旧标签两档都要通"""
        self._patch_legacy_name(monkeypatch, self.LEGACY_CONTAINER_NAME)
        if tag == "current":
            info = make_info(owner_label=container_manager.get_owner_tag(), mounts=None)
        else:
            info = make_info(
                owner_label=container_manager.get_legacy_owner_tag(),
                mounts=mount_points_of(monkeypatch, here, INSTANCE_KEY),
            )
        put_container(docker, info)
        assert await self._open(INSTANCE_KEY) == CONTAINER_NAME

    async def test_the_unlabelled_legacy_container_is_untouched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        docker: Dict[str, Any],
        db: DBAdapterInstance,  # noqa: ARG002
    ) -> None:
        """单实例时代那个容器由 compose 建、**身上没有任何标签**，行为逐字不变

        对它套用标签判据 = 所有遗留部署的这几个端点集体 403。这条就是那道防线。
        """
        self._patch_legacy_name(monkeypatch, self.LEGACY_CONTAINER_NAME)
        put_container(
            docker,
            make_info(
                container_name=self.LEGACY_CONTAINER_NAME,
                instance_label=None,
                owner_label=None,
                mounts=None,
            ),
        )
        # 模板名的容器不存在 → 回退到遗留名，且不校验
        assert await self._open(INSTANCE_KEY) == self.LEGACY_CONTAINER_NAME
        # 未指定实例（老客户端）也是同一条路
        assert await self._open(None) == self.LEGACY_CONTAINER_NAME

    async def test_the_denial_reaches_the_caller_as_403(
        self,
        monkeypatch: pytest.MonkeyPatch,
        here: Path,  # noqa: ARG002
        deploy_b: Path,
        docker: Dict[str, Any],
        db: DBAdapterInstance,  # noqa: ARG002
    ) -> None:
        """越权要以 403 出去，不能被端点自己的兜底 `except` 改写成 500

        `/napcat-token` 外面包着一层 `except Exception -> OperationFailedError`。
        `PermissionDeniedError` 掉进去的话，一次跨部署越权会显示成「获取令牌失败」——
        既误导排查，也把这条安全信号从响应里抹掉了。
        """
        from nekro_agent.schemas.errors import OperationFailedError

        self._patch_legacy_name(monkeypatch, self.LEGACY_CONTAINER_NAME)
        put_container(
            docker,
            make_info(
                owner_label=legacy_tag_of(monkeypatch, deploy_b),
                mounts=mount_points_of(monkeypatch, deploy_b, INSTANCE_KEY),
            ),
        )
        ob_routers = self._adapter_module()
        with pytest.raises(PermissionDeniedError) as excinfo:
            await ob_routers.get_napcat_token(instance_key=INSTANCE_KEY, _current_user=_platform_owner())
        assert not isinstance(excinfo.value, OperationFailedError)
        assert PermissionDeniedError.http_status == 403

    def test_no_container_endpoint_bypasses_get_container(self) -> None:
        """接线：新加一个自己去 `containers.get()` 的端点，这条会红

        归属校验挂在 `get_container()` 上，所以「每个碰容器的端点都从这里取容器」
        才是它真的生效的前提。`/onebot-token` 只回部署级令牌、不碰容器，显式豁免。
        """
        import inspect

        ob_routers = self._adapter_module()
        exempt = {"/container/onebot-token"}
        checked: List[str] = []
        for route in ob_routers.router.routes:
            path = str(getattr(route, "path", ""))
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None or path in exempt:
                continue
            source = inspect.getsource(endpoint)
            assert "get_container(" in source, f"{path} 没有经过 get_container()，归属校验对它不生效"
            checked.append(path)
        assert len(checked) >= 4, f"只扫到 {checked}，本用例的前提（那批容器端点还在）变了"
