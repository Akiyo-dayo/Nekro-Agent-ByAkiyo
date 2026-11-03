"""同宿主两套 NA 部署：宿主全局命名空间不再相撞

## 这份测试在守什么

有一批名字属于**宿主**而不属于某一套部署：iptables 规则的 `--comment` 标记、Docker 网络名与
网桥接口名、沙盒容器的归属标签。它们从前只带 `OsEnv.INSTANCE_NAME`，而它默认是空串，于是
同一台机器上的两套部署会落在同一个名字上。

两类后果，代价差别极大：

| 撞的东西 | 表现 |
|---|---|
| compose 的容器名 / 卷名 | `docker compose up` 当场失败，一眼看得见 |
| iptables 标记 | **静默**：后启动的一方按标记先全删再装自己那套，先启动一方的沙盒回调放行规则被换掉，从此每次沙盒执行都恰好卡到运行超时，NA 侧一条错误日志都没有（真实排查过九个多小时） |
| 沙盒容器归属标签 | **静默**：启动清理按标签 `kill + delete`，会把邻居正在跑的沙盒杀掉 |

所以本文件盯的是「派生作用域」这条结构性保证，而不是某一条规则的文本。

## 用例的取材原则

- 表驱动、显式输入、精确期望；不做随机 fuzz、不睡眠、不只断言「没抛异常」。
- 凡是断言「两套部署不撞」的，都同时给出**同一条判据在旧口径下会撞**的对照组
  （`_legacy_scope()`）——否则「不撞」可能只是这组输入区分不出来（§2.11）。
- 环境派生值一律在**调用时**算，不写默认环境下的字面量（§2.9）。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from nekro_agent.core.os_env import (
    OsEnv,
    deployment_scope,
    derive_deployment_scope,
    describe_deployment_scope,
)
from nekro_agent.services.sandbox import runner as sandbox_runner
from nekro_agent.services.sandbox.network_guard import (
    INPUT_CHAIN,
    AddressFamily,
    IptablesExecutor,
    LegacyMarkerCheck,
    count_marked_rules,
    count_marked_rules_by_marker,
    format_legacy_orphan_hint,
    isolation_marker,
    report_legacy_marker_orphans,
    sandbox_bridge_name,
    sandbox_network_name,
)
from nekro_agent.services.workspace.network_guard import (
    PLATFORM_GROUP,
    workspace_bridge_name,
    workspace_isolation_marker,
    workspace_network_name,
)

# 同一台宿主上两套部署的数据目录。两者都是绝对路径，派生结果与本机 cwd 无关
DEPLOY_A = "/opt/ExampleApp_Prod/data/nekro_agent"
DEPLOY_B = "/opt/na-second/data/nekro_agent"

# 派生值允许出现的字符：它要能直接进 Docker 网络名、iptables `--comment` 与容器标签。
# 首字符必须是字母或数字 —— 网络名是 `{作用域}nekro_sandbox_net`，Docker 的名字校验
# 要求首字符是字母或数字，而这个前缀是代码自己生成的（运维写错还看得见，代码生成错了不会）
_SCOPE_CHARSET = re.compile(r"^[a-z0-9][a-z0-9_]*-[0-9a-f]{8}-$")


def _legacy_scope() -> str:
    """改造前的口径：`OsEnv.INSTANCE_NAME or ""`

    对照组专用。凡是断言「两套部署的某个名字不同」，都要用它证明**同一条判据**在旧口径下
    确实会给出相同的名字 —— 不然那条「不同」可能只是被别的因素满足了。
    """
    return OsEnv.INSTANCE_NAME or ""


@pytest.fixture
def warning_logs():
    """收集 WARNING 及以上的日志正文

    产品代码用 loguru，它不走 stdlib logging，`caplog` 恒为空 —— 拿它断言「日志里有某句话」
    会永远通过，等于没断言。
    """
    from loguru import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="WARNING")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


@pytest.fixture
def info_logs():
    """收集 INFO 及以上的日志正文（启动那条「作用域 + 推导来源」说明就在这一档）"""
    from loguru import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="INFO")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


class TestDerivedScopeIsStableAndDistinct:
    """派生值的三条硬要求：稳定、有区分度、字符集安全"""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (DEPLOY_A, DEPLOY_B),
            ("/srv/na-a/data/nekro_agent", "/srv/na-b/data/nekro_agent"),
            # 只差一个字符的目录也必须分开
            ("/opt/na1/data", "/opt/na2/data"),
            # 同名目录挂在不同父目录下
            ("/home/alice/na/data/nekro_agent", "/home/bob/na/data/nekro_agent"),
        ],
    )
    def test_different_data_dirs_get_different_scopes(self, left: str, right: str) -> None:
        assert derive_deployment_scope(left) != derive_deployment_scope(right)

    @pytest.mark.parametrize("data_dir", [DEPLOY_A, DEPLOY_B, "/srv/x/data", "./data/nekro_agent"])
    def test_same_data_dir_gets_the_same_scope_every_time(self, data_dir: str) -> None:
        """同一进程内清了缓存也算得出同一个值

        `derive_deployment_scope` 带 `lru_cache`，所以「连着调两次相同」证明不了什么；
        这里额外清一次缓存再算。**但这条抓不住 pid** —— 同一进程里 pid 是常数，
        真正钉住「只由路径决定」的是下面那条。
        """
        first = derive_deployment_scope(data_dir)
        derive_deployment_scope.cache_clear()
        assert derive_deployment_scope(data_dir) == first

    @pytest.mark.parametrize("data_dir", [DEPLOY_A, DEPLOY_B, "/srv/x/data", "/data", "./data/nekro_agent"])
    def test_the_digest_is_computed_from_the_path_and_nothing_else(self, data_dir: str) -> None:
        """哈希的输入必须**只有**解析后的路径

        这条是「重启前后必须相同」的真正判据：掺进 pid / 时间戳 / 随机数 / 任何环境量，
        算出来的摘要就与这里的期望对不上。上面那条清缓存的用例在同一进程里跑，
        pid 恒定，压根区分不出来（实测：掺 pid 的变异从它手里活了下来）。

        这里刻意把公式抄一遍而不是调产品函数——目的就是把公式本身钉死。
        """
        import hashlib
        from pathlib import Path

        expected = hashlib.sha256(Path(data_dir).expanduser().resolve().as_posix().encode("utf-8")).hexdigest()[:8]
        assert derive_deployment_scope(data_dir).endswith(f"-{expected}-")

    @pytest.mark.parametrize(
        "data_dir",
        [
            DEPLOY_A,
            DEPLOY_B,
            "/data",
            "/",
            "/srv",
            "./data/nekro_agent",
            # 首字符是下划线 / 点 / 连字符的目录名：slug 不能把它们原样带进网络名的开头
            "/opt/_staging/data/nekro_agent",
            "/opt/.na-hidden/data",
            "/opt/--dashes--/data",
            "/opt/___/data",  # 全被掐光 -> 回落到固定前缀
            "/opt/中文目录/data",  # 非 ASCII 同样被清空
        ],
    )
    def test_scope_only_uses_characters_that_are_safe_everywhere(self, data_dir: str) -> None:
        """字符集要同时满足 Docker 网络名、iptables --comment 与容器标签

        `format_isolation_marker` 会把越界字符替换成 `_`，一旦派生值需要被替换，
        「标记」与「网络名」就会各自长成不同的样子，排查时对不上。
        """
        scope = derive_deployment_scope(data_dir)
        assert _SCOPE_CHARSET.match(scope), scope

    @pytest.mark.parametrize(
        ("data_dir", "expected_slug"),
        [
            # 末尾的 data / nekro_agent 在每套部署里都一样，跳过它们才有区分度
            ("/opt/na-second/data/nekro_agent", "nasecond"),
            ("/srv/myfork/data", "myfork"),
            ("/opt/ExampleApp_Prod/data/nekro_agent", "exampleapp_p"),
            # 全是通用段时回落到固定前缀，靠哈希区分
            ("/data", "na"),
            ("/srv", "na"),
            # 下划线开头的目录名：掐掉首字符，剩下的照常保留
            ("/opt/_staging/data/nekro_agent", "staging"),
            # 掐光之后没剩下东西，回落到固定前缀（不能产出 `-3f2a91c4-` 这种空 slug）
            ("/opt/___/data", "na"),
        ],
    )
    def test_slug_is_the_first_part_that_actually_identifies_the_deployment(
        self,
        data_dir: str,
        expected_slug: str,
    ) -> None:
        """前半段给人看：运维在 iptables 里看到标记要能猜出是哪套"""
        assert derive_deployment_scope(data_dir).startswith(f"{expected_slug}-")

    def test_relative_data_dir_resolves_to_the_deployment_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认的 `./data/nekro_agent` 是相对路径，解析后自然带上各自的部署根目录

        这正是裸机形态下两套 checkout 各自分开的机制。
        """
        import os
        from pathlib import Path

        cwd = Path(os.getcwd()).resolve().as_posix()
        assert derive_deployment_scope("./data/nekro_agent") == derive_deployment_scope(f"{cwd}/data/nekro_agent")


class TestExplicitInstanceNameStillWins:
    """显式 `NEKRO_INSTANCE_NAME` 的优先级不变"""

    @pytest.mark.parametrize("instance_name", ["r12verify", "tenant_a-", "rpc_probe-", "na-b"])
    def test_explicit_name_is_returned_verbatim(
        self,
        monkeypatch: pytest.MonkeyPatch,
        instance_name: str,
    ) -> None:
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_A)
        assert deployment_scope() == instance_name

    @pytest.mark.parametrize("data_dir", [DEPLOY_A, DEPLOY_B])
    def test_empty_name_falls_back_to_the_derived_suffix(
        self,
        monkeypatch: pytest.MonkeyPatch,
        data_dir: str,
    ) -> None:
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        monkeypatch.setattr(OsEnv, "DATA_DIR", data_dir)
        assert deployment_scope() == derive_deployment_scope(data_dir)
        # 旧口径在这里给的是空串，也就是标记里的 `default`
        assert _legacy_scope() == ""
        assert deployment_scope() != _legacy_scope()

    @pytest.mark.parametrize("data_dir", [DEPLOY_A, DEPLOY_B])
    def test_the_startup_line_can_map_a_marker_back_to_a_deployment(
        self,
        monkeypatch: pytest.MonkeyPatch,
        data_dir: str,
    ) -> None:
        """派生值必须能被对回某一套部署，否则运维只看见两串看不懂的哈希"""
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        monkeypatch.setattr(OsEnv, "DATA_DIR", data_dir)
        described = describe_deployment_scope()
        assert derive_deployment_scope(data_dir) in described
        assert data_dir in described

        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "na-b")
        assert "na-b" in describe_deployment_scope()
        assert "NEKRO_INSTANCE_NAME" in describe_deployment_scope()


class TestTwoDeploymentsNeverShareAHostGlobalName:
    """本条目的核心契约：两套部署的宿主全局名字逐项不同

    每一条都配一个「旧口径下会撞」的对照，证明这组输入确实区分得出来。
    """

    @staticmethod
    def _names(scope: str, group: str) -> Dict[str, str]:
        return {
            "sandbox_marker": isolation_marker(scope),
            "sandbox_network": sandbox_network_name(scope),
            "sandbox_bridge": sandbox_bridge_name(scope),
            "workspace_marker": workspace_isolation_marker(scope, group),
            "workspace_network": workspace_network_name(scope, group),
            "workspace_bridge": workspace_bridge_name(scope, group),
        }

    @pytest.mark.parametrize("group", [PLATFORM_GROUP, "t1", "t42", "shared"])
    def test_every_host_global_name_differs_between_the_two_deployments(self, group: str) -> None:
        a = self._names(derive_deployment_scope(DEPLOY_A), group)
        b = self._names(derive_deployment_scope(DEPLOY_B), group)
        collisions = sorted(key for key in a if a[key] == b[key])
        assert collisions == [], f"这些名字仍然相撞: {collisions}"

    @pytest.mark.parametrize("group", [PLATFORM_GROUP, "t1", "t42", "shared"])
    def test_the_old_scope_really_did_collide(
        self,
        monkeypatch: pytest.MonkeyPatch,
        group: str,
    ) -> None:
        """对照组：旧口径（两套都没设 `NEKRO_INSTANCE_NAME`）下**每一项都撞**

        没有这条，上面那条「全都不同」有可能只是因为这组名字本来就带别的区分维度。

        ⚠️ 这条必须**跟真的一样走一遍数据目录**：写成 `_names("", g) == _names("", g)`
        的话它就是 `f(x) == f(x)`，无论产品代码怎么改都恒真，等于没断言（§2.11）。
        所以这里两次都把 `OsEnv.DATA_DIR` 换成不同的部署、各自过一遍 `_legacy_scope()`
        —— 旧口径压根不读数据目录，这才是「换个部署也算出同一组名字」的成因。
        对照的另一半（同样两个数据目录、走 `deployment_scope()` 就分得开）在上一条。
        """
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")

        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_A)
        legacy_a = self._names(_legacy_scope(), group)
        current_a = self._names(deployment_scope(), group)

        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_B)
        legacy_b = self._names(_legacy_scope(), group)
        current_b = self._names(deployment_scope(), group)

        assert legacy_a == legacy_b, "旧口径本该与数据目录无关，这条对照已经不成立了"
        # 同一批名字、同样两个数据目录，新口径必须逐项分开——两半合起来才证明差异来自这次改动
        assert all(current_a[key] != current_b[key] for key in current_a)

    @pytest.mark.parametrize("data_dir", [DEPLOY_A, DEPLOY_B, "/data", "./data/nekro_agent"])
    @pytest.mark.parametrize("group", [PLATFORM_GROUP, "t1", "shared"])
    def test_bridge_names_stay_within_ifnamsiz(self, data_dir: str, group: str) -> None:
        """网桥接口名受 IFNAMSIZ 限制必须短于 16，派生值变长了也不能破这条"""
        scope = derive_deployment_scope(data_dir)
        assert len(sandbox_bridge_name(scope)) < 16
        assert len(workspace_bridge_name(scope, group)) < 16

    def test_sandbox_and_workspace_guards_still_do_not_share_a_marker(self) -> None:
        """同一套部署里两个守卫的规则并存于同一批链上，标记必须仍然分得开"""
        for data_dir in (DEPLOY_A, DEPLOY_B):
            scope = derive_deployment_scope(data_dir)
            assert isolation_marker(scope) != workspace_isolation_marker(scope)
            for group in ("t1", "shared"):
                assert isolation_marker(scope) != workspace_isolation_marker(scope, group)


# =============================================================================
# 沙盒守卫：真正装到链上的那个标记
# =============================================================================


class _FakeNetwork:
    def __init__(self, detail: Dict) -> None:
        self._detail = detail

    async def show(self) -> Dict:
        return self._detail


class _FakeNetworks:
    def __init__(self, details: Dict[str, Dict]) -> None:
        self._details = details
        self.requested: List[str] = []
        self.created: List[str] = []

    async def get(self, name: str) -> _FakeNetwork:
        self.requested.append(name)
        if name in self._details:
            return _FakeNetwork(self._details[name])
        raise RuntimeError(f"network {name} not found")

    async def create(self, config: Dict) -> None:
        # 先记账再抛：`best_effort` 会把这里的异常当成「隔离没装上」吞掉，
        # 于是「fake 脱靶」与「真的装不上」在结果上无法区分（§2.9 ①）
        self.created.append(str(config.get("Name", "")))
        raise AssertionError("单测里不该真的去建 Docker 网络")


class _FakeSystem:
    async def info(self) -> Dict:
        return {"OSType": "linux"}


class _FakeGuardDocker:
    def __init__(self, details: Dict[str, Dict]) -> None:
        self.networks = _FakeNetworks(details)
        self.system = _FakeSystem()


def _sandbox_network_details(scope: str) -> Dict[str, Dict]:
    """按被测代码**当下**会算出的名字建 fake（§2.9）"""
    return {
        sandbox_network_name(scope): {
            "Id": "a" * 64,
            "Options": {"com.docker.network.bridge.name": sandbox_bridge_name(scope)},
            "IPAM": {"Config": [{"Subnet": "172.28.0.0/16", "Gateway": "172.28.0.1"}]},
        },
        "bridge": {
            "Id": "b" * 64,
            "Options": {},
            "IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]},
        },
    }


class TestTheMarkerActuallyInstalledIsScoped:
    """装到 iptables 上的标记必须带本部署的作用域

    纯函数 `isolation_marker()` 测得再漂亮也不构成「安装路径上用的是它」的证据：
    `prepare_sandbox_network` 里那一行退回 `OsEnv.INSTANCE_NAME or ""` 时，
    命名函数的全部用例照样全绿（实测：这个变异第一轮活了下来）。所以这里驱动真实的
    `prepare_sandbox_network`，把 `install_marked_rules` 收到的标记捞出来看。
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        from nekro_agent.services.sandbox import network_guard

        network_guard.reset_isolation_state()
        yield
        network_guard.reset_isolation_state()

    @staticmethod
    def _arm(monkeypatch: pytest.MonkeyPatch, data_dir: str, instance_name: str = "") -> List[str]:
        from nekro_agent.core.config import config
        from nekro_agent.services.sandbox import network_guard

        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", instance_name)
        monkeypatch.setattr(OsEnv, "DATA_DIR", data_dir)
        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "strict")
        monkeypatch.setattr(config, "SANDBOX_CHAT_API_URL", "http://host.docker.internal:8021/api")
        monkeypatch.setattr(config, "NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS", [])
        monkeypatch.setattr(network_guard, "resolve_executor", lambda docker: _resolved(_RecordingExecutor({})))

        markers: List[str] = []

        async def _record(executor, marker, build) -> None:  # noqa: ANN001 - 与被测签名一致
            markers.append(marker)
            # 顺带确认规则集真的生成得出来（生成不出来时被测代码会走另一条分支）
            build("ipv4", "DOCKER-USER")

        monkeypatch.setattr(network_guard, "install_marked_rules", _record)
        return markers

    @pytest.mark.parametrize("data_dir", [DEPLOY_A, DEPLOY_B])
    async def test_the_installed_marker_follows_the_deployment_scope(
        self,
        monkeypatch: pytest.MonkeyPatch,
        data_dir: str,
    ) -> None:
        from nekro_agent.services.sandbox.network_guard import prepare_sandbox_network

        markers = self._arm(monkeypatch, data_dir)
        docker = _FakeGuardDocker(_sandbox_network_details(deployment_scope()))
        plan = await prepare_sandbox_network(docker)  # type: ignore[arg-type]

        assert plan.isolated
        assert docker.networks.created == [], "fake 脱靶：被测代码要的网络名与 fake 提供的对不上"
        assert markers == [isolation_marker(derive_deployment_scope(data_dir))]
        # 这一句才是本条目要保证的事：两套部署不会再共用 `nekro-sandbox:default`
        assert markers != [isolation_marker("")]

    async def test_two_deployments_install_under_different_markers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.sandbox.network_guard import prepare_sandbox_network

        installed: List[str] = []
        for data_dir in (DEPLOY_A, DEPLOY_B):
            with monkeypatch.context() as patch:
                markers = self._arm(patch, data_dir)
                docker = _FakeGuardDocker(_sandbox_network_details(deployment_scope()))
                await prepare_sandbox_network(docker)  # type: ignore[arg-type]
                installed.extend(markers)
                # 每套部署各自从零开始，否则第二次会命中第一次的签名缓存
                from nekro_agent.services.sandbox import network_guard

                network_guard.reset_isolation_state()
        assert len(installed) == 2
        assert installed[0] != installed[1]

    async def test_explicit_instance_name_still_decides_the_marker(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.sandbox.network_guard import prepare_sandbox_network

        markers = self._arm(monkeypatch, DEPLOY_A, instance_name="rpc_probe-")
        docker = _FakeGuardDocker(_sandbox_network_details("rpc_probe-"))
        await prepare_sandbox_network(docker)  # type: ignore[arg-type]
        assert markers == ["nekro-sandbox:rpc_probe-"]

    def test_reset_only_clears_this_deployments_verification_clock(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`reset_isolation_state` 拨的也必须是本作用域那把时钟

        回读自检的调度表与 CC 工作区守卫共用，键就是标记。这里算错标记的后果是
        「清了别人的时钟、自己那把没清」——没有任何日志，只会表现为多做/少做一次核对。
        """
        from nekro_agent.services.sandbox import network_guard

        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_A)
        mine = isolation_marker(deployment_scope())
        legacy = isolation_marker("")
        assert mine != legacy

        network_guard.mark_rules_verified(mine)
        network_guard.mark_rules_verified(legacy)
        network_guard.reset_isolation_state()

        assert mine not in network_guard._verify_states
        assert legacy in network_guard._verify_states, "不该顺手清掉别的标记的回读时钟"
        network_guard.reset_rule_verification()


# =============================================================================
# 沙盒容器归属标签
# =============================================================================


class _FakeSandboxContainer:
    def __init__(self, name: str, owner: Optional[str]) -> None:
        self._name = name
        self._owner = owner
        self.killed = False
        self.deleted = False

    async def show(self) -> Dict:
        labels = {} if self._owner is None else {sandbox_runner.SANDBOX_CONTAINER_LABEL: self._owner}
        return {"Name": f"/{self._name}", "Config": {"Labels": labels}}

    async def kill(self) -> None:
        self.killed = True

    async def delete(self, force: bool = False) -> None:  # noqa: ARG002 - 与 aiodocker 签名一致
        self.deleted = True


class _FakeSandboxContainers:
    def __init__(self, containers: Sequence[_FakeSandboxContainer]) -> None:
        self._containers = list(containers)

    async def list(self, all: bool = False) -> List[_FakeSandboxContainer]:  # noqa: A002 - aiodocker 就叫 all
        return list(self._containers)


class _FakeSandboxDocker:
    def __init__(self, containers: Sequence[_FakeSandboxContainer]) -> None:
        self.containers = _FakeSandboxContainers(containers)

    async def close(self) -> None:
        return None


class TestSandboxContainerOwnership:
    """启动清理只能碰本部署的沙盒容器

    这一组是**行为级**的：`cleanup_sandbox_containers` 的判据以前写在模块常量里、
    import 期算一次，改成函数之后如果调用点没跟着换，纯函数测试照样全绿（§2.7）。
    """

    def test_owner_tag_follows_the_deployment_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_A)
        assert sandbox_runner.sandbox_container_owner() == deployment_scope()

        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "na-b")
        assert sandbox_runner.sandbox_container_owner() == "na-b"

    def test_two_deployments_get_different_owner_tags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_A)
        owner_a, legacy_a = sandbox_runner.sandbox_container_owner(), _legacy_scope() or "default"
        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_B)
        owner_b, legacy_b = sandbox_runner.sandbox_container_owner(), _legacy_scope() or "default"
        assert owner_a != owner_b
        # 对照：旧口径下两套部署的标签**逐字相同**，正是「启动就把邻居的沙盒杀了」的成因
        assert legacy_a == legacy_b == "default"

    def test_the_label_written_at_creation_comes_from_the_same_function(self) -> None:
        """打标签的那一行必须调 `sandbox_container_owner()`

        清理那一侧是行为级覆盖的（下一条），但**打标签**这一侧埋在 `run_code_in_sandbox`
        中段、要真库真配置才驱动得起来。两侧口径分叉的后果是最难看的一种：
        标签写 A、清理找 B，于是本部署的遗留容器一个都清不掉，而邻居的照样被清
        —— 两条纯函数测试都会全绿（§2.7）。所以这里按 AST 钉住**实参**本身。
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(sandbox_runner))
        found: List[ast.expr] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Name) and key.id == "SANDBOX_CONTAINER_LABEL":
                    found.append(value)

        assert len(found) == 1, f"期望恰好一处给沙盒容器打归属标签，实际 {len(found)} 处"
        value = found[0]
        assert isinstance(value, ast.Call), f"标签值必须是函数调用，实际是 {type(value).__name__}"
        assert isinstance(value.func, ast.Name) and value.func.id == "sandbox_container_owner", (
            "标签值必须来自 sandbox_container_owner()，否则与启动清理的判据是两套口径"
        )

    async def test_cleanup_leaves_the_other_deployments_containers_alone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_A)
        mine = _FakeSandboxContainer("nekro-agent-sandbox-mine", sandbox_runner.sandbox_container_owner())

        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_B)
        neighbour_owner = sandbox_runner.sandbox_container_owner()
        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_A)

        neighbour = _FakeSandboxContainer("nekro-agent-sandbox-neighbour", neighbour_owner)
        legacy = _FakeSandboxContainer("nekro-agent-sandbox-legacy", "default")
        unrelated = _FakeSandboxContainer("some-other-container", None)

        docker = _FakeSandboxDocker([mine, neighbour, legacy, unrelated])
        monkeypatch.setattr(sandbox_runner.aiodocker, "Docker", lambda *a, **kw: docker)

        await sandbox_runner.cleanup_sandbox_containers()

        assert (mine.killed, mine.deleted) == (True, True)
        # 邻居那套部署的容器**可能正在跑**，碰不得
        assert (neighbour.killed, neighbour.deleted) == (False, False)
        # 升级前留下的旧标签容器也不归本次清理管（它的归属已经无法判定）
        assert (legacy.killed, legacy.deleted) == (False, False)
        assert (unrelated.killed, unrelated.deleted) == (False, False)


# =============================================================================
# 旧标记的孤儿规则：报告，绝不自动删除
# =============================================================================


def _rule_line(chain: str, marker: str, target: str = "ACCEPT") -> str:
    return f'-A {chain} -i nekro-sbx0 -p tcp --dport 8021 -m comment --comment "{marker}" -j {target}'


class _RecordingExecutor(IptablesExecutor):
    """按 (地址族, 链) 喂 `iptables -S` 输出，并记下收到的每一条命令"""

    description = "fake"

    def __init__(self, tables: Dict[Tuple[str, str], str]) -> None:
        self._tables = tables
        self.calls: List[Tuple[str, Tuple[str, ...]]] = []

    async def run(self, family: AddressFamily, args: Sequence[str]) -> Tuple[int, str]:
        self.calls.append((family, tuple(args)))
        if len(args) == 2 and args[0] == "-S":
            key = (family, args[1])
            if key in self._tables:
                return 0, self._tables[key]
            return 1, f"chain {args[1]} not found"
        return 0, ""


class TestLegacyMarkerOrphansAreReportedNotDeleted:
    """升级换标记之后旧规则怎么办

    选的是「报告 + 手工清理」而不是「启动时自动清掉」：本进程分不清链上那组
    `nekro-sandbox:default` 是自己升级前装的，还是同宿主上另一套**尚未升级**的 NA 正在用的
    —— 自动删除会在后一种情形下亲手复现 §2.6 那场事故。
    """

    LEGACY = "nekro-sandbox:default"
    CURRENT = "nekro-sandbox:na-b-"
    LEGACY_NET = "nekro_sandbox_net"

    def _check(self, legacy: Optional[str] = None, current: Optional[str] = None) -> LegacyMarkerCheck:
        return LegacyMarkerCheck(
            guard="沙盒网络隔离",
            legacy_marker=self.LEGACY if legacy is None else legacy,
            current_marker=self.CURRENT if current is None else current,
            legacy_network=self.LEGACY_NET,
        )

    def _tables(self, legacy_count: int) -> Dict[Tuple[str, str], str]:
        forward = "\n".join([_rule_line("DOCKER-USER", self.CURRENT)] * 2 + [_rule_line("DOCKER-USER", self.LEGACY)] * legacy_count)
        inputs = "\n".join([_rule_line(INPUT_CHAIN, self.LEGACY, "DROP")] * legacy_count)
        return {("ipv4", "DOCKER-USER"): forward, ("ipv4", INPUT_CHAIN): inputs}

    @pytest.mark.parametrize(("legacy_count", "expected"), [(0, 0), (1, 2), (3, 6)])
    async def test_counting_is_read_only(self, legacy_count: int, expected: int) -> None:
        executor = _RecordingExecutor(self._tables(legacy_count))
        assert await count_marked_rules(executor, self.LEGACY) == expected
        assert all(args[0] == "-S" for _, args in executor.calls), "统计过程只允许读，出现了非 -S 的命令"

    async def test_current_marker_rules_are_not_counted_as_orphans(self) -> None:
        executor = _RecordingExecutor(self._tables(1))
        assert await count_marked_rules(executor, self.CURRENT) == 2

    async def test_report_warns_and_deletes_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        warning_logs: List[str],
    ) -> None:
        from nekro_agent.services.sandbox import network_guard

        executor = _RecordingExecutor(self._tables(3))
        monkeypatch.setattr(network_guard, "resolve_executor", lambda docker: _resolved(executor))

        counts = await report_legacy_marker_orphans(object(), [self._check()])  # type: ignore[arg-type]

        assert counts == {self.LEGACY: 6}
        assert not any(args[0] == "-D" for _, args in executor.calls), "孤儿规则不允许被自动删除"
        assert any(self.LEGACY in line for line in warning_logs)

    async def test_nothing_is_reported_when_the_scope_did_not_change(
        self,
        monkeypatch: pytest.MonkeyPatch,
        warning_logs: List[str],
    ) -> None:
        """显式设了 `NEKRO_INSTANCE_NAME` 的部署标记根本没变，不该被这条提示打扰"""
        from nekro_agent.services.sandbox import network_guard

        executor = _RecordingExecutor(self._tables(3))
        monkeypatch.setattr(network_guard, "resolve_executor", lambda docker: _resolved(executor))

        assert await report_legacy_marker_orphans(object(), [self._check(current=self.LEGACY)]) == {}  # type: ignore[arg-type]
        assert executor.calls == []
        assert warning_logs == []

    @pytest.mark.parametrize("group_count", [1, 2, 8])
    async def test_reading_the_chains_does_not_scale_with_the_number_of_markers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        group_count: int,
    ) -> None:
        """一批标记只读一遍链

        CC 工作区的标记按分组分开，而孤儿规则**按设计永不自动删除** —— 也就是说这条检查
        每次启动都要跑一遍。NA 跑在容器里时每条 iptables 命令都要起一个临时容器
        （`HelperContainerIptablesExecutor`），按分组各查一次的话，启动开销会随租户分组数
        线性膨胀。这里把执行次数钉死成与标记条数无关。
        """
        from nekro_agent.services.sandbox import network_guard

        executor = _RecordingExecutor(self._tables(2))
        monkeypatch.setattr(network_guard, "resolve_executor", lambda docker: _resolved(executor))

        checks = [
            LegacyMarkerCheck(
                guard=f"分组 {index}",
                legacy_marker=f"nekro-cc:default.t{index}",
                current_marker=f"nekro-cc:na-b-.t{index}",
                legacy_network=f"nekro_cc_net_t{index}",
            )
            for index in range(group_count)
        ]
        await report_legacy_marker_orphans(object(), checks)  # type: ignore[arg-type]

        # ipv4: DOCKER-USER 探测命中 1 次 + 读 DOCKER-USER/INPUT 各 1 次；
        # ipv6: 两个候选链都不在 fake 里 -> 探测 2 次后放弃。合计 5 次，与分组数无关
        assert len(executor.calls) == 5, f"读链次数随标记条数变了: {executor.calls}"

    async def test_every_marker_in_the_batch_gets_its_own_count(self) -> None:
        """批量统计不能把几个标记的条数混成一个数"""
        tables = {
            ("ipv4", "DOCKER-USER"): "\n".join(
                [_rule_line("DOCKER-USER", "nekro-cc:default")] * 2
                + [_rule_line("DOCKER-USER", "nekro-cc:default.t7")] * 3
                + [_rule_line("DOCKER-USER", self.CURRENT)],
            ),
        }
        executor = _RecordingExecutor(tables)
        counts = await count_marked_rules_by_marker(
            executor,
            ["nekro-cc:default", "nekro-cc:default.t7", "nekro-cc:nobody"],
        )
        assert counts == {"nekro-cc:default": 2, "nekro-cc:default.t7": 3, "nekro-cc:nobody": 0}

    @pytest.mark.parametrize("count", [1, 18])
    def test_the_hint_points_at_the_product_path_and_warns_off_the_manual_one(self, count: int) -> None:
        """提示语要给出**可执行的产品出口**，而不是一段让人自己去 iptables 上敲的命令

        原来这条提示给的是 `iptables-save | grep ... | sed 's/^-A /-D /'` 那条路，
        而它已经真的翻过车：`--comment` 的取值带引号，经 shell 重新分词后引号丢掉，
        `-D` 一条都匹配不上。**半成功比全失败更糟** ——
        被删掉的那半可能正是还在生效的兜底 DROP。现在改指总后台的「隔离规则」页，
        那里先预览再二次确认，执行走产品自己的 `build_cleanup_commands`。
        """
        hint = format_legacy_orphan_hint("沙盒网络隔离", self.LEGACY, self.CURRENT, self.LEGACY_NET, count)
        assert self.LEGACY in hint
        assert self.CURRENT in hint
        assert str(count) in hint
        # 必须给出**具体的**去处，否则这条提示只是让人焦虑
        assert "/console/network-rules" in hint
        # 也必须写清「先确认没有别的 NA 在用」——照着删掉邻居的规则就是本条目要消灭的事故
        assert "尚未升级" in hint
        # 明确劝阻手写 iptables：这条提示是运维唯一会看到的指引，指错方向就是在教人踩坑
        assert "不要手写" in hint
        assert "sed" in hint
        assert "iptables-save | grep" not in hint
        # 仍要给出**具体**的旧网络名：运维要能把链上那个标记对回一张网络
        assert self.LEGACY_NET in hint

    def test_the_hint_names_the_group_specific_legacy_network(self) -> None:
        """CC 工作区按分组各有一张旧网络，提示语里不能都写成平台方那一张"""
        hint = format_legacy_orphan_hint(
            "CC 工作区网络隔离（分组 t7）",
            "nekro-cc:default.t7",
            "nekro-cc:na-b-.t7",
            "nekro_cc_net_t7",
            15,
        )
        assert "nekro_cc_net_t7" in hint
        assert "nekro_sandbox_net" not in hint


async def _resolved(value):
    return value


# =============================================================================
# 启动入口的接线（§2.7：零件全对、调用点没了 —— 测试会全绿）
# =============================================================================


class _FakeCloseableDocker:
    """`setup_*_network_isolation` 里 `aiodocker.Docker()` 的替身

    这两个入口只把它当句柄往下传（`prepare_*` 在本组里被换成 no-op、另有专门覆盖），
    自己只调 `close()`。
    """

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class TestStartupEntryPointsAreWiredUp:
    """两个启动入口必须真的把新能力接上

    这一组盯的不是零件而是**接线**：`report_legacy_marker_orphans` /
    `describe_deployment_scope` 单独测得再漂亮，也不构成「启动时真的会跑到」的证据。
    实测（变异）：在补这一组之前，把 `setup_sandbox_network_isolation` 里的孤儿检查与
    那行作用域日志**整段删掉**，全仓相关的 335 条测试一条都不红 —— 与 §2.7 记的
    「6 个调用点变异活了 5 个」是同一种失效。

    这两处删掉不会有任何报错：孤儿规则本来就不删，日志本来就只是日志。
    但它们正是「运维能不能把 iptables 里的标记对回某套部署」的**唯一**依据
    （见 docs/multi-deploy-same-host.md §0 与 §5），静默失效的代价是整条排查路径消失。
    """

    LEGACY_SANDBOX = "nekro-sandbox:default"

    @staticmethod
    def _legacy_tables(marker: str, count: int) -> Dict[Tuple[str, str], str]:
        return {
            ("ipv4", "DOCKER-USER"): "\n".join([_rule_line("DOCKER-USER", marker)] * count),
            ("ipv4", INPUT_CHAIN): "",
        }

    async def test_sandbox_startup_logs_the_scope_and_reports_orphans(
        self,
        monkeypatch: pytest.MonkeyPatch,
        info_logs: List[str],
        warning_logs: List[str],
    ) -> None:
        from nekro_agent.core.config import config
        from nekro_agent.services.sandbox import network_guard

        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_A)
        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "strict")

        executor = _RecordingExecutor(self._legacy_tables(self.LEGACY_SANDBOX, 4))
        monkeypatch.setattr(network_guard, "resolve_executor", lambda docker: _resolved(executor))
        monkeypatch.setattr(network_guard, "prepare_sandbox_network", lambda docker: _resolved(None))
        monkeypatch.setattr(network_guard.aiodocker, "Docker", lambda *a, **kw: _FakeCloseableDocker())

        await network_guard.setup_sandbox_network_isolation()

        scope = deployment_scope()
        # ① 作用域说明行：取值 + 推导来源 + 标记 + 网络 + 网桥，缺一项都对不回来
        startup = [line for line in info_logs if "沙盒网络隔离作用域" in line]
        assert len(startup) == 1, f"启动日志里没有作用域说明行: {info_logs}"
        for expected in (
            scope,
            DEPLOY_A,
            isolation_marker(scope),
            sandbox_network_name(scope),
            sandbox_bridge_name(scope),
        ):
            assert expected in startup[0], f"作用域说明行缺了 {expected!r}: {startup[0]}"

        # ② 孤儿检查确实跑到了，且查的是**升级前那个**标记与**升级前那张网络**
        orphan = [line for line in warning_logs if self.LEGACY_SANDBOX in line]
        assert orphan, warning_logs
        assert sandbox_network_name("") in orphan[0], orphan[0]
        assert not any(args[0] == "-D" for _, args in executor.calls), "启动路径上不允许删规则"

    async def test_sandbox_startup_stays_silent_when_isolation_is_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        warning_logs: List[str],
    ) -> None:
        """`off` 档一条规则都不装，也就没有孤儿可报——不能凭空打扰运维"""
        from nekro_agent.core.config import config
        from nekro_agent.services.sandbox import network_guard

        monkeypatch.setattr(config, "SANDBOX_NETWORK_ISOLATION", "off")
        executor = _RecordingExecutor(self._legacy_tables(self.LEGACY_SANDBOX, 4))
        monkeypatch.setattr(network_guard, "resolve_executor", lambda docker: _resolved(executor))

        await network_guard.setup_sandbox_network_isolation()

        assert executor.calls == []
        assert warning_logs == []

    async def test_workspace_startup_logs_the_scope_and_reports_orphans_per_group(
        self,
        monkeypatch: pytest.MonkeyPatch,
        info_logs: List[str],
        warning_logs: List[str],
    ) -> None:
        from nekro_agent.core.config import config
        from nekro_agent.models.db_workspace import DBWorkspace
        from nekro_agent.services.workspace import network_guard as ws_guard

        monkeypatch.setattr(OsEnv, "INSTANCE_NAME", "")
        monkeypatch.setattr(OsEnv, "DATA_DIR", DEPLOY_A)
        monkeypatch.setattr(config, "CC_WORKSPACE_NETWORK_ISOLATION", "strict")

        groups = [PLATFORM_GROUP, "t7"]
        legacy_markers = [workspace_isolation_marker("", group) for group in groups]
        tables = {
            ("ipv4", "DOCKER-USER"): "\n".join(_rule_line("DOCKER-USER", marker) for marker in legacy_markers),
            ("ipv4", INPUT_CHAIN): "",
        }
        executor = _RecordingExecutor(tables)

        async def _all():
            return [object(), object()]

        async def _group_of(workspace, _seen=iter(groups)):  # noqa: ANN001, ARG001
            return next(_seen)

        monkeypatch.setattr(DBWorkspace, "all", staticmethod(_all))
        monkeypatch.setattr(ws_guard, "resolve_workspace_network_group", _group_of)
        monkeypatch.setattr(ws_guard, "prepare_workspace_network", lambda docker, group: _resolved(None))
        monkeypatch.setattr(ws_guard.aiodocker, "Docker", lambda *a, **kw: _FakeCloseableDocker())

        from nekro_agent.services.sandbox import network_guard as sbx_guard

        monkeypatch.setattr(sbx_guard, "resolve_executor", lambda docker: _resolved(executor))

        await ws_guard.setup_workspace_network_isolation()

        scope = deployment_scope()
        startup = [line for line in info_logs if "CC 工作区网络隔离作用域" in line]
        assert len(startup) == 1, f"启动日志里没有作用域说明行: {info_logs}"
        for expected in (
            scope,
            DEPLOY_A,
            workspace_isolation_marker(scope),
            workspace_network_name(scope),
            workspace_bridge_name(scope),
        ):
            assert expected in startup[0], f"作用域说明行缺了 {expected!r}: {startup[0]}"

        # 每个分组各有自己的旧标记与旧网络，一个都不能漏（漏掉的那组孤儿从此没人报），
        # 也不能几个分组都指着平台方那一张网络——运维照着查会得到 0 个容器的假安全感
        for group, marker in zip(groups, legacy_markers):
            # 按反引号包住的标记精确取行：`nekro-cc:default` 是 `nekro-cc:default.t7` 的前缀，
            # 裸 `in` 会让平台方那条断言被租户那条满足（两条都在时永远绿）
            reported = [line for line in warning_logs if f"`{marker}`" in line]
            assert reported, f"分组标记 {marker} 没被报出来: {warning_logs}"
            assert workspace_network_name("", group) in reported[0], reported[0]
        assert not any(args[0] == "-D" for _, args in executor.calls), "启动路径上不允许删规则"
        # 整批一次交给孤儿检查，而不是每个分组各调一次：读链次数必须与分组数无关。
        # 这里两个分组 —— 按分组各调一次的话是 10 次（每次 5 次），批量是 5 次。
        # NA 在容器里时每一次都是一个临时容器的启停，而这条检查每次启动都要跑
        assert len(executor.calls) == 5, f"读链次数随分组数变了（没有整批交进去）: {executor.calls}"
