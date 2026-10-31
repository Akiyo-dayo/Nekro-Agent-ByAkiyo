"""内置 NapCat 容器名的长度上限：超过 63 改用稳定短名（§3 #46 复核补上）

## 这份测试在守什么

容器在 compose 网络里靠容器名当主机名被访问（`_webui_base_url` 在容器内拼
`http://{容器名}:6099`），而 DNS 单段最长 63 个字符。`build_container_name` 的展开形态是
`{deployment_scope()}nekro_napcat_{instance_key}`：派生前缀最长 22 + 模板 13 + 实例标识最长 32
= 67，超了；显式的 `NEKRO_INSTANCE_NAME` 更长。Docker 对这样的名字照样放行，结果是建出一个
控制台管得到、NA 却解析不了主机名的容器（扫码 / 快速登录 / 登录账号推导全部连不上）。

口径：展开后超过 63 → `{部署前缀}nekro_napcat_{哈希}`（实例段换成哈希）；**不超长的一律原样**，
已部署的容器名不变。本文件钉六件事，每件都有对应的变异：

| 契约 | 把它改回去 | 该红的用例 |
|---|---|---|
| 任何输入组合都不超 63 | `build_container_name` 不收短 | `TestNameNeverExceedsTheDnsLabel` 整组 |
| 不超长的原样不动（不改变已部署名字） | 无条件换哈希 | `TestShortNamesAreUntouched` |
| 短名稳定可复算（重启不换名） | 哈希掺进随机量 / 改了哈希公式 | `TestShortFormIsStableAndDistinct` |
| 不同实例 / 不同部署不撞名 | 哈希不含 scope / 不含实例标识 | 同上 |
| 展开形态的旧长名仍可读认 | 候选表与 `_assert_owned` 去掉它 | `TestOldLongNamesStayReachable` 整组 |
| 邻居的短名不被放行 | 名字维度放宽成「只看前缀」 | `TestTheNeighboursShortNameIsRefused` |

## 取材原则

与 `test_napcat_container_name_scope.py` 一致：表驱动、显式输入、精确期望；环境派生值调产品自己的
函数现算（§2.9）；路径落在 `tmp_path` 下。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import pytest
from aiodocker.exceptions import DockerError
from tortoise import Tortoise

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.schemas.errors import ConflictError, PermissionDeniedError
from nekro_agent.services.console import container_manager
from nekro_agent.services.console.napcat_orphans import OrphanVerdict, plan_napcat_orphan_cleanup

DNS_LABEL_MAX = 63
LONGEST_KEY = "k" * 32
TEMPLATE_LENGTH = len("nekro_napcat_")

# 钉住「短名算法一旦变了，已建容器全部失联」：下面这条是 scope=`deployslug12-3f2a91c4-`（22 字符，派生前缀的最长形态）、32 位实例标识下
# 的真实输出。改哈希公式 / 位数 / 拼接方式会让它变红——那是一次需要迁移说明的破坏性变更。
PINNED_SCOPE = "deployslug12-3f2a91c4-"
PINNED_KEY = "k" * 32
PINNED_SHORT_NAME = "deployslug12-3f2a91c4-nekro_napcat_8eca5567c5"

ACME_TENANT_ID = 1


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


@pytest.fixture
def scoped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """返回一个函数：把进程摆到「显式 NEKRO_INSTANCE_NAME=某值」的形态上"""
    # 目录名取满 12 个字符（派生前缀 slug 的上限）：默认派生前缀因此是最长的 22 字符，
    # 与最长的 32 位实例标识拼起来正好是 #46 复核时量出的 67。
    data_dir = tmp_path / "deployslug12" / "data" / "nekro_agent"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(data_dir))

    def apply(instance_name: str) -> str:
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        return instance_name

    return apply


def scope_of_length(length: int) -> str:
    """长度恰为 `length` 的合法部署前缀（`a` 开头、以 `-` 收尾，字符集合法）"""
    assert length >= 2
    return "a" + "s" * (length - 2) + "-"


def key_of_length(length: int) -> str:
    return "k" * length


# 一张覆盖面表：派生前缀的最长形态（22）、临界、远超限（显式 NEKRO_INSTANCE_NAME 过长）、
# 超过「前缀 + 哈希段」本身就放不下的极端
SCOPE_LENGTHS = (2, 10, 22, 30, 40, 49, 50, 51, 63, 70, 120)
KEY_LENGTHS = (1, 5, 18, 32)


# ---------------------------------------------------------------------------
# Docker 替身（与 test_napcat_container_name_scope.py 同形）
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
                "HostConfig": {"Binds": list(host_config.get("Binds") or [])},
                "State": {"Status": "running", "StartedAt": "2026-01-01T00:00:00Z"},
            },
        )
        self._state["containers"][name] = container
        return container


class _FakeImages:
    async def inspect(self, image: str) -> Dict[str, Any]:  # noqa: ARG002
        return {"Id": "sha256:" + "0" * 64}


class _FakeDocker:
    def __init__(self, state: Dict[str, Any]) -> None:
        self.containers = _FakeContainers(state)
        self.images = _FakeImages()

    async def close(self) -> None:
        return None


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    import aiodocker

    state: Dict[str, Any] = {"containers": {}, "created": []}
    monkeypatch.setattr(aiodocker, "Docker", lambda *_a, **_k: _FakeDocker(state))
    return state


def put_container(state: Dict[str, Any], info: Dict[str, Any]) -> _FakeContainer:
    name = str(info["Name"]).lstrip("/")
    container = _FakeContainer(name, info)
    state["containers"][name] = container
    return container


def make_info(*, container_name: str, instance_label: str, owner_label: Optional[str] = None) -> Dict[str, Any]:
    labels: Dict[str, str] = {container_manager.LABEL_INSTANCE: instance_label}
    if owner_label is not None:
        labels[container_manager.LABEL_OWNER] = owner_label
    return {
        "Name": f"/{container_name}",
        "Config": {"Labels": labels, "Image": "mlikiowa/napcat-docker:latest"},
        "HostConfig": {},
        "State": {"Status": "running", "StartedAt": "2026-01-01T00:00:00Z"},
    }


@pytest.fixture
async def empty_db():
    """孤儿盘点要反查库：一张空库，库里没有任何行认领这些实例标识"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield None
    finally:
        await Tortoise.close_connections()


@pytest.fixture
async def instance_row():
    """一条真实实例行：键取最长的 32 位，保证默认派生前缀下展开名就是超长的（67）"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield await DBAdapterInstance.create(
            adapter_key="onebot_v11",
            instance_key=LONGEST_KEY,
            display_name="long",
            status="online",
            provider="builtin_napcat",
            tenant_id=ACME_TENANT_ID,
            metadata_json="",
        )
    finally:
        await Tortoise.close_connections()


# ===========================================================================
# 一、任何输入组合都不超 63
# ===========================================================================


class TestNameNeverExceedsTheDnsLabel:
    @pytest.mark.parametrize("scope_length", SCOPE_LENGTHS)
    @pytest.mark.parametrize("key_length", KEY_LENGTHS)
    def test_every_combination_fits(self, scoped, scope_length: int, key_length: int) -> None:
        scoped(scope_of_length(scope_length))

        name = container_manager.build_container_name(key_of_length(key_length))

        assert len(name) <= DNS_LABEL_MAX, f"scope={scope_length} key={key_length} -> {len(name)} 字符: {name}"
        assert container_manager._DOCKER_NAME_PATTERN.match(name)  # noqa: SLF001
        assert all(len(label) <= DNS_LABEL_MAX for label in name.split("."))

    def test_the_default_derived_scope_with_the_longest_key_is_the_67_char_case(self, scoped) -> None:
        """前置对照：默认派生前缀 + 最长实例标识，展开形态确实是 67——这条用例证明「超长」是真问题"""
        scoped("")
        unshortened = container_manager.build_unshortened_container_name(LONGEST_KEY)
        assert len(unshortened) > DNS_LABEL_MAX
        assert len(container_manager.build_container_name(LONGEST_KEY)) <= DNS_LABEL_MAX

    def test_the_boundary_is_exactly_63(self, scoped) -> None:
        """63 本身不收短，64 才收短：边界不能差一"""
        key = key_of_length(10)
        exactly_63 = scope_of_length(DNS_LABEL_MAX - TEMPLATE_LENGTH - len(key))
        scoped(exactly_63)
        assert len(container_manager.build_unshortened_container_name(key)) == DNS_LABEL_MAX
        assert container_manager.build_container_name(key) == f"{exactly_63}nekro_napcat_{key}"

        scoped(scope_of_length(len(exactly_63) + 1))
        assert len(container_manager.build_unshortened_container_name(key)) == DNS_LABEL_MAX + 1
        assert container_manager.build_container_name(key) != container_manager.build_unshortened_container_name(key)
        assert len(container_manager.build_container_name(key)) <= DNS_LABEL_MAX

    async def test_create_uses_the_short_name_everywhere_it_matters(
        self,
        scoped,
        docker: Dict[str, Any],
        instance_row: DBAdapterInstance,
    ) -> None:
        """Docker create 的名字、写进实例元数据的名字都是短名；标签里仍是完整实例标识"""
        scoped("")
        expected = container_manager.build_container_name(LONGEST_KEY)
        assert expected != container_manager.build_unshortened_container_name(LONGEST_KEY)

        await container_manager.create_container(instance_row, image_tag="latest", auto_pull=False)

        (created_name, config), = docker["created"]
        assert created_name == expected and len(created_name) <= DNS_LABEL_MAX
        assert config["Labels"][container_manager.LABEL_INSTANCE] == LONGEST_KEY
        assert container_manager.read_container_metadata(instance_row).container_name == expected

    async def test_the_in_container_hostname_is_a_valid_dns_label(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scoped,
        docker: Dict[str, Any],
        instance_row: DBAdapterInstance,
    ) -> None:
        """#46 的直接症状：容器内 `_webui_base_url` 拼出的主机名不能超 63"""
        scoped("")
        await container_manager.create_container(instance_row, image_tag="latest", auto_pull=False)
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)

        base = await container_manager._webui_base_url(instance_row)  # noqa: SLF001

        host = urlparse(base).hostname or ""
        assert host == container_manager.build_container_name(LONGEST_KEY)
        assert 0 < len(host) <= DNS_LABEL_MAX


# ===========================================================================
# 二、不超长的原样不动
# ===========================================================================


class TestShortNamesAreUntouched:
    @pytest.mark.parametrize(
        ("scope", "key"),
        [
            ("na_a_", "inst1"),
            ("r20verify", "inst1"),
            ("myfork-3f2a91c4-", "inst1"),
            ("myfork-3f2a91c4-", "k" * 20),  # 16 + 13 + 20 = 49
        ],
    )
    def test_names_within_the_limit_keep_the_pre_existing_form(self, scoped, scope: str, key: str) -> None:
        """已部署的容器名不因本修复而变：展开后 ≤ 63 的一律逐字不动"""
        scoped(scope)

        name = container_manager.build_container_name(key)

        assert name == f"{scope}nekro_napcat_{key}"
        assert name == container_manager.build_unshortened_container_name(key)

    def test_candidate_table_has_no_duplicate_for_a_short_name(self, scoped) -> None:
        scoped("na_a_")
        assert container_manager.container_name_candidates("inst1") == ["na_a_nekro_napcat_inst1", "nekro_napcat_inst1"]


# ===========================================================================
# 三、短名稳定、可复算、互不撞名
# ===========================================================================


class TestShortFormIsStableAndDistinct:
    def test_it_keeps_the_scope_prefix_and_replaces_the_instance_segment(self, scoped) -> None:
        scoped(PINNED_SCOPE)

        name = container_manager.build_container_name(PINNED_KEY)

        assert len(PINNED_SCOPE) + TEMPLATE_LENGTH + len(PINNED_KEY) > DNS_LABEL_MAX, "前提：这组输入确实超长"
        assert name.startswith(f"{PINNED_SCOPE}nekro_napcat_")
        assert PINNED_KEY not in name, "实例段应当换成哈希，而不是截断"
        tail = name[len(f"{PINNED_SCOPE}nekro_napcat_") :]
        assert re.fullmatch(r"[0-9a-f]{%d}" % container_manager.CONTAINER_NAME_HASH_LENGTH, tail), tail

    def test_it_is_deterministic(self, scoped) -> None:
        scoped(PINNED_SCOPE)
        assert container_manager.build_container_name(PINNED_KEY) == container_manager.build_container_name(PINNED_KEY)

    def test_the_algorithm_is_pinned(self, scoped) -> None:
        """短名一旦变了，已建容器的读认全部失联——算法变更必须是显式的破坏性决定"""
        scoped(PINNED_SCOPE)
        assert container_manager.build_container_name(PINNED_KEY) == PINNED_SHORT_NAME
        # 公式本身的独立复算（不经过产品函数）：sha256(scope + NUL + key) 前 10 位
        digest = hashlib.sha256(f"{PINNED_SCOPE}\0{PINNED_KEY}".encode()).hexdigest()[:10]
        assert PINNED_SHORT_NAME == f"{PINNED_SCOPE}nekro_napcat_{digest}"

    def test_different_instances_on_one_deployment_never_collide(self, scoped) -> None:
        scoped(PINNED_SCOPE)
        names = {container_manager.build_container_name(f"{'k' * 25}{index:07d}") for index in range(300)}
        assert len(names) == 300

    def test_the_same_instance_key_on_two_deployments_never_collides(self, scoped) -> None:
        """#46 的初衷：两套部署上同名 instance_key 的容器名结构上不同——收短之后仍要成立"""
        names = set()
        for scope in ("alpha-0a1b2c3d-", "alpha-0a1b2c3e-", "beta-ffffffff-"):
            scoped(scope)
            names.add(container_manager.build_container_name(LONGEST_KEY))
        assert len(names) == 3

    def test_two_overlong_scopes_sharing_a_prefix_still_differ(self, scoped) -> None:
        """显式前缀长到必须被截断时：只共享开头几十个字符的两套部署不能得到同一个名字"""
        common = "a" * 60
        names = set()
        for suffix in ("-x-", "-y-"):
            scoped(common + suffix)
            names.add(container_manager.build_container_name("inst1"))
        assert len(names) == 2
        assert all(len(name) <= DNS_LABEL_MAX for name in names)


# ===========================================================================
# 四、旧长名（展开形态，64~67 字符）仍可读认
# ===========================================================================


def old_long_container(scope_env) -> Dict[str, Any]:
    """按展开形态建出来的存量容器：名字 > 63、当前归属标签（round-26 起写的就是当前口径）"""
    scope_env("")
    name = container_manager.build_unshortened_container_name(LONGEST_KEY)
    assert len(name) > DNS_LABEL_MAX
    return make_info(
        container_name=name,
        instance_label=LONGEST_KEY,
        owner_label=container_manager.get_owner_tag(),
    )


class TestOldLongNamesStayReachable:
    def test_candidate_table_is_short_then_old_long_then_bare(self, scoped) -> None:
        scoped("")
        assert container_manager.container_name_candidates(LONGEST_KEY) == [
            container_manager.build_container_name(LONGEST_KEY),
            container_manager.build_unshortened_container_name(LONGEST_KEY),
            container_manager.build_legacy_container_name(LONGEST_KEY),
        ]

    def test_assert_owned_accepts_the_old_long_name(self, scoped) -> None:
        info = old_long_container(scoped)
        container_manager._assert_owned(  # noqa: SLF001
            info,
            LONGEST_KEY,
            container_manager.build_container_name(LONGEST_KEY),
        )

    @pytest.mark.parametrize("action", ["stop", "start", "delete"])
    async def test_lifecycle_reaches_the_old_long_named_container(
        self,
        scoped,
        docker: Dict[str, Any],
        instance_row: DBAdapterInstance,
        action: str,
    ) -> None:
        container = put_container(docker, old_long_container(scoped))

        await getattr(container_manager, f"{action}_container")(instance_row)

        assert [call.split(":", 1)[0] for call in container.calls] == [action]

    async def test_create_refuses_while_the_old_long_container_is_still_there(
        self,
        scoped,
        docker: Dict[str, Any],
        instance_row: DBAdapterInstance,
    ) -> None:
        """否则同一个实例会同时有两个容器（旧长名 + 新短名），旧的还占着 WebUI 宿主端口"""
        put_container(docker, old_long_container(scoped))

        with pytest.raises(ConflictError):
            await container_manager.create_container(instance_row, image_tag="latest", auto_pull=False)
        assert docker["created"] == []

    async def test_orphan_scan_sees_both_name_forms(self, scoped, docker: Dict[str, Any], empty_db: None) -> None:
        """孤儿盘点按标签取实例标识、按同一份归属判据过滤：短名容器与旧长名容器都盘得到"""
        scoped("")
        short_named_key, old_long_key = "j" * 32, LONGEST_KEY
        put_container(
            docker,
            make_info(
                container_name=container_manager.build_container_name(short_named_key),
                instance_label=short_named_key,
                owner_label=container_manager.get_owner_tag(),
            ),
        )
        put_container(docker, old_long_container(scoped))

        plan = await plan_napcat_orphan_cleanup()

        by_key = {candidate.instance_key: candidate for candidate in plan.candidates}
        assert set(by_key) == {short_named_key, old_long_key}
        assert by_key[short_named_key].container_name == container_manager.build_container_name(short_named_key)
        assert by_key[short_named_key].verdict == OrphanVerdict.ORPHANED
        assert by_key[old_long_key].container_name == container_manager.build_unshortened_container_name(old_long_key)
        assert by_key[old_long_key].verdict == OrphanVerdict.ORPHANED


# ===========================================================================
# 五、新建出来的短名容器全链路可管理；邻居的短名不被放行
# ===========================================================================


class TestTheNeighboursShortNameIsRefused:
    async def test_a_freshly_created_short_named_container_is_manageable(
        self,
        scoped,
        docker: Dict[str, Any],
        instance_row: DBAdapterInstance,
    ) -> None:
        scoped("")
        await container_manager.create_container(instance_row, image_tag="latest", auto_pull=False)
        short = container_manager.build_container_name(LONGEST_KEY)

        await container_manager.stop_container(instance_row)

        # create 时启动过一次，stop 落在同一个短名容器上
        assert docker["containers"][short].calls == ["start", "stop:15"]

    def test_another_deployments_short_name_is_not_ours(self, scoped) -> None:
        """名字维度只放行本部署候选表里的名字：邻居的短名（哪怕标签被伪造成一致）按名字就拒"""
        scoped("alpha-0a1b2c3d-")
        neighbour_name = container_manager.build_container_name(LONGEST_KEY)
        scoped("beta-ffffffff-")
        info = make_info(
            container_name=neighbour_name,
            instance_label=LONGEST_KEY,
            owner_label=container_manager.get_owner_tag(),
        )

        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(  # noqa: SLF001
                info,
                LONGEST_KEY,
                container_manager.build_container_name(LONGEST_KEY),
            )

    def test_a_same_prefix_different_hash_name_is_refused(self, scoped) -> None:
        """只对上前缀、哈希段不是本实例的名字不放行（另一个 32 位实例标识的短名）"""
        scoped("")
        other = container_manager.build_container_name("j" * 32)
        info = make_info(
            container_name=other,
            instance_label=LONGEST_KEY,
            owner_label=container_manager.get_owner_tag(),
        )

        with pytest.raises(PermissionDeniedError):
            container_manager._assert_owned(  # noqa: SLF001
                info,
                LONGEST_KEY,
                container_manager.build_container_name(LONGEST_KEY),
            )
