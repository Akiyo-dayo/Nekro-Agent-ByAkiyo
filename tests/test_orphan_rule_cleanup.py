"""孤儿隔离规则的运维出口：判定、预览、清理

## 这组测试要钉住的是什么

链上的隔离规则标记是**宿主全局**的。升级换作用域之后旧标记的规则成了孤儿，新代码永远不会
去清它们；而运维手上从前只有「自己去 iptables 上敲」这一条路，上一轮已经因此出过事
（`--comment "nekro-cc:x"` 的引号被 shell 吃掉，`-D` 全部失败 —— 半成功的话被删掉的
就可能是还在生效的那一组，PROJECT_STATE §2.20）。

本模块给出的判据只有一个目标：**绝不删掉还有人在用的那一组**。所以测试的重心不在
「能删」，而在「该拦的每一种形态都真的拦住了」：

| 形态 | 期望 |
| --- | --- |
| 本部署自己的标记（含它将来才会出现的租户分组） | `self`，永不出现在可清理清单里 |
| 旧网桥上还挂着容器（**含已停止**、`NetworkMode` 仍钉在旧网上的 CC 工作区） | `blocked`，且提示语要说清「删掉之后隔离静默消失」 |
| 宿主上有容器带着该作用域的归属标签 | `blocked` |
| 同作用域的沙盒规则放行的端点上仍有进程在监听 | `blocked` |
| 探测不确定 / 读不到宿主状态 / 没有可判的沙盒规则 | `blocked`（判不准就不给删） |
| 以上全部通过 | `orphan` |

`iptables` 一律用假 executor **回放真实形态的 `-S` 文本**并真的执行 `-D`
（按 token 匹配，与内核语义一致），因此「别人的规则逐字不动」是可以断言的事实而不是推断。
规则文本由**产品自己的规则集生成函数**渲染，不是手抄的常量——手抄的那份会在规则集改动后
悄悄失去代表性。
"""

import ast
import inspect
import shlex
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

import pytest
from fastapi import HTTPException

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console import network_rules as console_network_rules
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.sandbox import network_guard as guard
from nekro_agent.services.sandbox.network_guard import (
    INPUT_CHAIN,
    SANDBOX_MARKER_FAMILY,
    BridgeNetworkFact,
    HostContainerFacts,
    IptablesRule,
    MarkerInventory,
    OrphanCleanupRefused,
    SandboxNetworkSpec,
    build_isolation_rules,
    collect_marker_inventory,
    execute_orphan_rule_cleanup,
    extract_rule_marker,
    install_marked_rules,
    is_marker_of_scope,
    isolation_marker,
    judge_orphan_candidates,
    plan_orphan_rule_cleanup,
    rule_comment_value,
    sandbox_marker_candidates,
)
from nekro_agent.services.user.role import Role
from nekro_agent.services.workspace.network_guard import (
    WORKSPACE_MARKER_FAMILY,
    WorkspaceNetworkSpec,
    build_workspace_isolation_rules,
    workspace_isolation_marker,
)

FORWARD_CHAIN = "DOCKER-USER"
FAMILIES = (SANDBOX_MARKER_FAMILY, WORKSPACE_MARKER_FAMILY)
PREFIXES = [family.prefix for family in FAMILIES]

# 本部署（升级后）的作用域。显式设 `NEKRO_INSTANCE_NAME` 是为了让判据与本机的数据目录无关
# ——由数据目录派生的话，同一份测试在两台机器上判的就不是同一件事（§2.13）。
CURRENT_SCOPE = "r16fork-"

# 上一轮实测留在测试部署上的三个孤儿标记（PROJECT_STATE §9.2）
LEGACY_SANDBOX_MARKER = isolation_marker("")
LEGACY_CC_MARKER = workspace_isolation_marker("")
LEGACY_CC_TENANT_MARKER = workspace_isolation_marker("", "t1")

OWN_PORTS: FrozenSet[int] = frozenset({8021})


# =============================================================================
# 真实形态的 `iptables -S` 文本
# =============================================================================


def _render_line(chain: str, rule: IptablesRule) -> str:
    """把一条规则渲染成 `iptables -S <chain>` 那一行

    `--comment` 的取值**带引号**——真实输出就是这样，而那对引号正是上一轮手写 `-D`
    翻车的原因（shell 重新分词时丢掉引号，规则匹配不上）。产品侧走 `shlex.split`
    + argv 数组，所以这里必须把引号原样放进来，否则测的就不是真实形态。
    """
    tokens: List[str] = []
    previous = ""
    for token in rule.args:
        tokens.append(f'"{token}"' if previous == "--comment" else token)
        previous = token
    return f"-A {chain} " + " ".join(tokens)


def sandbox_rules(marker: str, bridge: str, subnet: str, endpoint: Tuple[str, int]) -> List[IptablesRule]:
    spec = SandboxNetworkSpec(
        bridge_interface=bridge,
        ipv4_subnet=subnet,
        rpc_endpoints=(endpoint,),
        marker=marker,
    )
    return build_isolation_rules(spec, "ipv4", FORWARD_CHAIN)


def workspace_rules(marker: str, bridge: str, subnet: str, relay: Tuple[str, int]) -> List[IptablesRule]:
    spec = WorkspaceNetworkSpec(
        bridge_interface=bridge,
        ipv4_subnet=subnet,
        controller_addresses=(),
        api_port=8080,
        marker=marker,
        relay_port=relay[1],
        host_relay_addresses=(relay[0],),
    )
    return build_workspace_isolation_rules(spec, "ipv4", FORWARD_CHAIN)


FOREIGN_LINES = (
    f"-A {FORWARD_CHAIN} -j RETURN",
    f'-A {FORWARD_CHAIN} -i br-deadbeef -m comment --comment "someone-else:default" -j DROP',
    # **别人的规则，但某个 token 恰好等于我们的标记**。判据写成「标记出现在任意 token 上」时，
    # 盘点侧（只认 `--comment` 取值）会说 N 条、删除侧会删掉 N+1 条 —— 多删的那一条是别人的，
    # 而且没有任何信号。这一行就是那条判据的回归标本
    f'-A {INPUT_CHAIN} -j LOG --log-prefix nekro-sandbox:default',
)


def marked_with(line: str, marker: str) -> bool:
    """这一行是不是**带着**该标记（按 `--comment` 取值，与产品判据同源）"""
    try:
        return rule_comment_value(shlex.split(line)) == marker
    except ValueError:
        return False


def build_chains(groups: Sequence[Tuple[str, List[IptablesRule]]]) -> Dict[Tuple[str, str], List[str]]:
    """把若干组规则铺进 (地址族, 链) -> 规则行 的表里，并掺进别人的规则"""
    chains: Dict[Tuple[str, str], List[str]] = {
        ("ipv4", FORWARD_CHAIN): [FOREIGN_LINES[0], FOREIGN_LINES[1]],
        ("ipv4", INPUT_CHAIN): [FOREIGN_LINES[2]],
    }
    for _marker, rules in groups:
        for rule in rules:
            chains[("ipv4", rule.chain)].append(_render_line(rule.chain, rule))
    return chains


DEFAULT_GROUPS: Tuple[Tuple[str, List[IptablesRule]], ...] = (
    (LEGACY_SANDBOX_MARKER, sandbox_rules(LEGACY_SANDBOX_MARKER, "nekro-sbx0", "172.19.0.0/16", ("172.19.0.1", 8031))),
    (LEGACY_CC_MARKER, workspace_rules(LEGACY_CC_MARKER, "nekro-cc0", "172.20.0.0/16", ("172.20.0.1", 8031))),
    (
        LEGACY_CC_TENANT_MARKER,
        workspace_rules(LEGACY_CC_TENANT_MARKER, "nkcc-legacyt1", "172.21.0.0/16", ("172.21.0.1", 8031)),
    ),
    (
        isolation_marker(CURRENT_SCOPE),
        sandbox_rules(isolation_marker(CURRENT_SCOPE), "nksbx-11112222", "172.30.0.0/16", ("172.30.0.1", 8021)),
    ),
    (
        workspace_isolation_marker(CURRENT_SCOPE),
        workspace_rules(workspace_isolation_marker(CURRENT_SCOPE), "nkcc-333344445", "172.31.0.0/16", ("172.31.0.1", 8021)),
    ),
)


# =============================================================================
# 假件
# =============================================================================


class FakeIptables:
    """回放 `-S` 并**真的执行** `-D`

    删除按 token 匹配（`shlex.split(line)[2:] == argv[2:]`），与内核「按规则而不是按文本」
    的语义一致；按行号删则真的按下标摘。这样「只删掉目标标记、别人的规则逐字不动」
    才是可断言的事实，而不是从命令列表里推断出来的。
    """

    description = "fake"

    def __init__(
        self,
        chains: Dict[Tuple[str, str], List[str]],
        unsupported_families: Sequence[str] = ("ipv6",),
        text_delete_broken: bool = False,
    ) -> None:
        self.chains = {key: list(value) for key, value in chains.items()}
        self._unsupported = set(unsupported_families)
        self.text_delete_broken = text_delete_broken
        self.calls: List[Tuple[str, Tuple[str, ...]]] = []

    def dump(self, family: str, chain: str) -> str:
        return "\n".join([f"-N {chain}", *self.chains[(family, chain)]])

    def lines(self, family: str, chain: str) -> List[str]:
        return list(self.chains.get((family, chain), []))

    async def run(self, family: str, args: Sequence[str]) -> Tuple[int, str]:
        argv = list(args)
        self.calls.append((family, tuple(argv)))
        if family in self._unsupported:
            return 1, "ip6tables: command not found"
        if not argv:
            return 1, "empty"
        chain = argv[1] if len(argv) > 1 else ""
        if argv[0] == "-S":
            if (family, chain) not in self.chains:
                return 1, f"No chain/target/match by that name: {chain}"
            return 0, self.dump(family, chain)
        if argv[0] == "-D":
            return self._delete(family, chain, argv[2:])
        if argv[0] == "-I":
            index = int(argv[2])
            self.chains.setdefault((family, chain), []).insert(index - 1, _render_argv(chain, argv[3:]))
            return 0, ""
        return 0, ""

    def _delete(self, family: str, chain: str, rest: List[str]) -> Tuple[int, str]:
        lines = self.chains.get((family, chain))
        if lines is None:
            return 1, f"No chain/target/match by that name: {chain}"
        if len(rest) == 1 and rest[0].isdigit():
            index = int(rest[0])
            if 1 <= index <= len(lines):
                lines.pop(index - 1)
                return 0, ""
            return 1, "iptables: Index of deletion too big."
        if self.text_delete_broken:
            return 1, "iptables: Bad rule (does a matching rule exist in that chain?)."
        for position, line in enumerate(lines):
            try:
                tokens = shlex.split(line)
            except ValueError:
                continue
            if tokens[:2] == ["-A", chain] and tokens[2:] == rest:
                lines.pop(position)
                return 0, ""
        return 1, "iptables: Bad rule (does a matching rule exist in that chain?)."


def _render_argv(chain: str, tokens: Sequence[str]) -> str:
    rendered: List[str] = []
    previous = ""
    for token in tokens:
        rendered.append(f'"{token}"' if previous == "--comment" else token)
        previous = token
    return f"-A {chain} " + " ".join(rendered)


class FakeNetwork:
    def __init__(self, detail: Dict) -> None:
        self._detail = detail

    async def show(self) -> Dict:
        return self._detail


class FakeNetworks:
    def __init__(self, details: Dict[str, Dict], failing: Sequence[str] = ()) -> None:
        self._details = details
        self._failing = set(failing)

    async def get(self, name: str) -> FakeNetwork:
        if name in self._failing:
            raise RuntimeError(f"network {name} unavailable")
        if name not in self._details:
            raise RuntimeError(f"no such network: {name}")
        return FakeNetwork(self._details[name])


class FakeContainers:
    def __init__(self, summaries: Sequence[Dict], broken: bool = False) -> None:
        self._summaries = list(summaries)
        self._broken = broken

    async def list(self, **kwargs) -> List[Dict]:
        # 已停止的容器只在 `all=True` 时才出现，而它的 `NetworkMode` 仍钉在旧网络上，
        # 一旦被启动就会挂回旧网桥。漏掉这个参数 = 把一个还会用到旧规则的工作区判成"没人用"
        assert kwargs.get("all") is True, "盘点容器必须带 all=True，否则看不见已停止的 CC 工作区"
        if self._broken:
            raise RuntimeError("docker daemon unreachable")
        return list(self._summaries)


class FakeDocker:
    def __init__(
        self,
        containers: Sequence[Dict] = (),
        networks: Optional[Dict[str, Dict]] = None,
        failing_networks: Sequence[str] = (),
        broken_containers: bool = False,
    ) -> None:
        self.containers = FakeContainers(containers, broken=broken_containers)
        self.networks = FakeNetworks(networks or {}, failing=failing_networks)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def network_detail(name: str, bridge: str, attached: Sequence[str] = ()) -> Dict:
    return {
        "Name": name,
        "Id": f"{name}-id",
        "Options": {"com.docker.network.bridge.name": bridge},
        "Containers": {f"cid-{index}": {"Name": item} for index, item in enumerate(attached)},
    }


def container_summary(name: str, networks: Sequence[str] = (), mode: str = "", labels: Optional[Dict] = None) -> Dict:
    return {
        "Id": f"id-{name}",
        "Names": [f"/{name}"],
        "Labels": labels or {},
        "NetworkSettings": {"Networks": {item: {} for item in networks}},
        "HostConfig": {"NetworkMode": mode},
    }


@pytest.fixture(autouse=True)
def _pin_scope(monkeypatch: pytest.MonkeyPatch):
    """把本部署的作用域钉死，别让它随本机数据目录变化"""
    monkeypatch.setattr(OsEnv, "INSTANCE_NAME", CURRENT_SCOPE)
    monkeypatch.setattr(OsEnv, "EXPOSE_PORT", 8021)
    yield


def readable_host(bridges: Sequence[BridgeNetworkFact] = (), scopes: Optional[Dict[str, Tuple[str, ...]]] = None):
    return HostContainerFacts(
        bridges={fact.interface: fact for fact in bridges},
        scopes_claimed=dict(scopes or {}),
        readable=True,
    )


def judge(
    chains: Optional[Dict[Tuple[str, str], List[str]]] = None,
    host: Optional[HostContainerFacts] = None,
    liveness: Optional[Dict[Tuple[str, int], str]] = None,
    own_ports: FrozenSet[int] = OWN_PORTS,
):
    """走一遍纯判定，返回 标记 -> 判定结果"""
    dumps = []
    source = chains if chains is not None else build_chains(DEFAULT_GROUPS)
    for (family, chain), lines in source.items():
        dumps.append((family, chain, "\n".join([f"-N {chain}", *lines])))
    inventory = collect_marker_inventory(dumps, PREFIXES)
    candidates = judge_orphan_candidates(
        inventory,
        FAMILIES,
        CURRENT_SCOPE,
        host if host is not None else readable_host(),
        dict(liveness or {}),
        own_ports,
    )
    return {candidate.marker: candidate for candidate in candidates}


ALL_CLOSED = {
    ("172.19.0.1", 8031): "closed",
    ("172.20.0.1", 8031): "closed",
    ("172.21.0.1", 8031): "closed",
}


# =============================================================================
# 盘点
# =============================================================================


class TestMarkerInventory:
    """从 `iptables -S` 文本里认出本项目的标记"""

    def test_marker_comes_from_the_comment_value_only(self) -> None:
        """网桥名恰好等于某个标记时不能被算进来——子串搜索会误算，token 精确匹配不会"""
        tokens = shlex.split(FOREIGN_LINES[2])
        assert extract_rule_marker(tokens, PREFIXES) == ""

    def test_every_marker_on_the_chain_is_collected(self) -> None:
        inventory = judge()
        assert set(inventory) == {
            LEGACY_SANDBOX_MARKER,
            LEGACY_CC_MARKER,
            LEGACY_CC_TENANT_MARKER,
            isolation_marker(CURRENT_SCOPE),
            workspace_isolation_marker(CURRENT_SCOPE),
        }

    def test_foreign_markers_are_ignored(self) -> None:
        assert "someone-else:default" not in judge()

    def test_rule_text_is_kept_verbatim(self) -> None:
        """预览要逐字展示原文，运维才能拿它跟自己 `iptables-save` 看到的对上号"""
        candidate = judge()[LEGACY_SANDBOX_MARKER]
        assert candidate.rule_count == len(candidate.rules)
        for line in candidate.rules:
            assert line.text.startswith(f"-A {line.chain} ")
            assert f'--comment "{LEGACY_SANDBOX_MARKER}"' in line.text

    def test_interfaces_and_endpoints_are_extracted(self) -> None:
        dumps = [
            (family, chain, "\n".join([f"-N {chain}", *lines]))
            for (family, chain), lines in build_chains(DEFAULT_GROUPS).items()
        ]
        inventory = collect_marker_inventory(dumps, PREFIXES)
        assert inventory[LEGACY_SANDBOX_MARKER].interfaces == ("nekro-sbx0",)
        assert inventory[LEGACY_SANDBOX_MARKER].endpoints == (("172.19.0.1", 8031),)

    def test_cidr_destinations_are_not_endpoints(self) -> None:
        """兜底 DROP 的 `-d 10.0.0.0/8` 不是「某台主机在监听」，不能拿去做存活探测"""
        dumps = [
            (family, chain, "\n".join([f"-N {chain}", *lines]))
            for (family, chain), lines in build_chains(DEFAULT_GROUPS).items()
        ]
        inventory = collect_marker_inventory(dumps, PREFIXES)
        for _address, port in inventory[LEGACY_SANDBOX_MARKER].endpoints:
            assert port == 8031

    def test_host_destination_rendered_with_slash32_is_still_an_endpoint(self) -> None:
        """内核读回来的单机地址带 `/32`，两种形态都要认得"""
        line = (
            f'-A {FORWARD_CHAIN} -i nekro-sbx0 -d 172.19.0.1/32 -p tcp --dport 8031 '
            f'-m comment --comment "{LEGACY_SANDBOX_MARKER}" -j ACCEPT'
        )
        inventory = collect_marker_inventory([("ipv4", FORWARD_CHAIN, f"-N {FORWARD_CHAIN}\n{line}")], PREFIXES)
        assert inventory[LEGACY_SANDBOX_MARKER].endpoints == (("172.19.0.1", 8031),)


# =============================================================================
# 「这是不是本部署自己的标记」
# =============================================================================


class TestScopeOwnership:
    """本部署自用的标记永远不可删，判据按作用域前缀而不是逐个枚举"""

    @pytest.mark.parametrize(
        ("marker", "scope", "expected"),
        [
            ("nekro-sandbox:r16fork-", "r16fork-", True),
            ("nekro-cc:r16fork-", "r16fork-", True),
            # 分组走前缀：这个租户分组今天可能一个工作区都没有，但它的规则仍在保护旧容器
            ("nekro-cc:r16fork-.t7", "r16fork-", True),
            ("nekro-cc:r16fork-.shared", "r16fork-", True),
            ("nekro-sandbox:default", "r16fork-", False),
            ("nekro-cc:default.t1", "r16fork-", False),
            # 前缀不能"半匹配"：另一套部署的作用域恰好以本作用域开头时不算自己的
            ("nekro-sandbox:r16fork-other", "r16fork-", False),
            # 空作用域会被 `format_isolation_marker` 折成 default，两侧口径必须一致
            ("nekro-sandbox:default", "", True),
            ("nekro-cc:default.t1", "", True),
            # 越界字符两侧都要过同一次替换
            ("nekro-sandbox:a_b", "a/b", True),
        ],
    )
    def test_scope_membership_truth_table(self, marker: str, scope: str, expected: bool) -> None:
        assert is_marker_of_scope(marker, scope, PREFIXES) is expected

    def test_current_deployment_markers_are_self(self) -> None:
        verdicts = judge(liveness=ALL_CLOSED)
        assert verdicts[isolation_marker(CURRENT_SCOPE)].verdict == "self"
        assert verdicts[workspace_isolation_marker(CURRENT_SCOPE)].verdict == "self"

    def test_a_tenant_group_that_has_no_workspace_today_is_still_self(self) -> None:
        """按分组枚举会漏掉它；按作用域前缀不会"""
        marker = workspace_isolation_marker(CURRENT_SCOPE, "t42")
        chains = build_chains(
            (*DEFAULT_GROUPS, (marker, workspace_rules(marker, "nkcc-t42", "172.32.0.0/16", ("172.32.0.1", 8021)))),
        )
        assert judge(chains=chains, liveness=ALL_CLOSED)[marker].verdict == "self"

    def test_sandbox_marker_candidates_cover_the_group_suffix(self) -> None:
        assert sandbox_marker_candidates("nekro-cc:default.t1") == (
            "nekro-sandbox:default.t1",
            "nekro-sandbox:default",
        )


# =============================================================================
# 判据表
# =============================================================================


class TestOrphanVerdicts:
    """每一条拦截判据都要能单独把 verdict 从 orphan 打回 blocked"""

    def test_all_clear_is_an_orphan(self) -> None:
        verdicts = judge(liveness=ALL_CLOSED)
        for marker in (LEGACY_SANDBOX_MARKER, LEGACY_CC_MARKER, LEGACY_CC_TENANT_MARKER):
            assert verdicts[marker].verdict == "orphan", verdicts[marker].blockers

    def test_containers_on_the_old_bridge_block(self) -> None:
        host = readable_host(
            [BridgeNetworkFact("nekro-cc0", "nekro_cc_net", 2, ("nekro-cc-aabb", "nekro-cc-ccdd"))],
        )
        candidate = judge(host=host, liveness=ALL_CLOSED)[LEGACY_CC_MARKER]
        assert candidate.verdict == "blocked"
        assert candidate.container_total == 2
        blocker = "".join(candidate.blockers)
        assert "还有 2 个容器" in blocker
        assert "nekro-cc-aabb" in blocker
        # §3 #45：CC 工作区容器是长命的，删掉兜底 DROP 之后隔离静默消失
        assert "不报错也不留日志" in blocker
        assert "重建" in blocker

    def test_sandbox_containers_block_with_a_different_next_step(self) -> None:
        """同样拦住，但沙盒容器是一次性的，给的下一步不是「重建工作区」"""
        host = readable_host([BridgeNetworkFact("nekro-sbx0", "nekro_sandbox_net", 1, ("nekro-agent-sandbox-x",))])
        candidate = judge(host=host, liveness=ALL_CLOSED)[LEGACY_SANDBOX_MARKER]
        assert candidate.verdict == "blocked"
        assert "重建" not in "".join(candidate.blockers)
        assert "一次性" in "".join(candidate.blockers)

    def test_a_stopped_workspace_container_still_blocks(self) -> None:
        """已停止的容器不出现在 `network inspect` 的 Containers 里，但重启就会挂回旧网桥"""
        host = readable_host([BridgeNetworkFact("nekro-cc0", "nekro_cc_net", 1, ("nekro-cc-stopped",))])
        assert judge(host=host, liveness=ALL_CLOSED)[LEGACY_CC_MARKER].verdict == "blocked"

    def test_ownership_label_claims_the_scope(self) -> None:
        host = readable_host(scopes={"": ("nekro-cc-legacy",)})
        verdicts = judge(host=host, liveness=ALL_CLOSED)
        assert verdicts[LEGACY_CC_MARKER].verdict == "blocked"
        assert verdicts[LEGACY_SANDBOX_MARKER].verdict == "blocked"
        assert "归属标签" in "".join(verdicts[LEGACY_CC_MARKER].blockers)

    def test_a_foreign_scope_label_does_not_block_us(self) -> None:
        host = readable_host(scopes={"someone-else-": ("their-container",)})
        assert judge(host=host, liveness=ALL_CLOSED)[LEGACY_CC_MARKER].verdict == "orphan"

    def test_a_rule_set_without_an_interface_cannot_be_judged(self) -> None:
        """`-i` 是「这组规则在保护谁」的唯一线索，读不出来就判不准

        本项目装的规则一定带 `-i`（两个规则集生成函数的第一段就是它）。读不出来只可能是
        规则不是我们装的、或者读回来的文本残缺——两种情况下「网桥上没有容器」都不成立。
        """
        line = f'-A {INPUT_CHAIN} -m comment --comment "{LEGACY_SANDBOX_MARKER}" -j DROP'
        chains = {("ipv4", INPUT_CHAIN): [line]}
        candidate = judge(chains=chains, liveness=ALL_CLOSED)[LEGACY_SANDBOX_MARKER]
        assert candidate.verdict == "blocked"
        assert "读不出任何" in "".join(candidate.blockers)

    def test_unreadable_host_state_blocks_everything(self) -> None:
        host = HostContainerFacts(bridges={}, scopes_claimed={}, readable=False, unreadable_reason="daemon down")
        verdicts = judge(host=host, liveness=ALL_CLOSED)
        for marker in (LEGACY_SANDBOX_MARKER, LEGACY_CC_MARKER, LEGACY_CC_TENANT_MARKER):
            assert verdicts[marker].verdict == "blocked"
            assert "daemon down" in "".join(verdicts[marker].blockers)

    def test_a_listening_owner_blocks_the_whole_scope(self) -> None:
        """另一套部署还在跑：它的规则放行的回调端点上仍有进程在监听"""
        liveness = {**ALL_CLOSED, ("172.19.0.1", 8031): "open"}
        verdicts = judge(liveness=liveness)
        for marker in (LEGACY_SANDBOX_MARKER, LEGACY_CC_MARKER, LEGACY_CC_TENANT_MARKER):
            assert verdicts[marker].verdict == "blocked"
            assert "仍有进程在监听" in "".join(verdicts[marker].blockers)

    def test_an_undecided_probe_blocks(self) -> None:
        liveness = {**ALL_CLOSED, ("172.19.0.1", 8031): "unknown"}
        assert judge(liveness=liveness)[LEGACY_SANDBOX_MARKER].verdict == "blocked"

    def test_a_missing_probe_result_is_undecided_not_orphan(self) -> None:
        """漏探等同判不准。默认值取错方向的话，一次探测失败就会变成一次删除"""
        assert judge(liveness={})[LEGACY_SANDBOX_MARKER].verdict == "blocked"

    def test_our_own_port_is_not_evidence_of_a_neighbour(self) -> None:
        """旧规则放行的正好是本部署现在监听的端口时，「有人在听」是我们自己

        测试部署上的实测形态：`nekro-sandbox:default` 那组放行的就是 8021。
        宿主 TCP 端口独占，我们占着它就不可能有第二个 NA 也占着。
        """
        marker = LEGACY_SANDBOX_MARKER
        chains = build_chains(
            (
                (marker, sandbox_rules(marker, "nekro-sbx0", "172.19.0.0/16", ("172.19.0.1", 8021))),
                (LEGACY_CC_MARKER, workspace_rules(LEGACY_CC_MARKER, "nekro-cc0", "172.20.0.0/16", ("172.20.0.1", 8021))),
            ),
        )
        verdicts = judge(chains=chains, liveness={("172.19.0.1", 8021): "open"})
        assert verdicts[marker].verdict == "orphan"
        assert any("本部署自己监听的端口" in note for note in verdicts[marker].notes)

    def test_a_scope_without_sandbox_rules_falls_back_to_its_own_endpoints(self) -> None:
        """#50：沙盒兄弟被先清掉之后，CC 标记改用**自己**放行的端点判存活

        原判据把「CC 标记的所有者还在不在」绑死在同作用域沙盒标记的**存在性**上，
        而两个标记是各自独立、要分两次清的对象 —— 于是只要运维先点了沙盒那一个，
        剩下的 CC 标记就永久卡在 `blocked`，而这个出口存在的全部理由就是把它们清掉。
        """
        chains = build_chains(
            ((LEGACY_CC_MARKER, workspace_rules(LEGACY_CC_MARKER, "nekro-cc0", "172.20.0.0/16", ("172.20.0.1", 8031))),),
        )
        candidate = judge(chains=chains, liveness=ALL_CLOSED)[LEGACY_CC_MARKER]
        assert candidate.verdict == "orphan", candidate.blockers
        assert any("改用" in note and LEGACY_CC_MARKER in note for note in candidate.notes)

    @pytest.mark.parametrize("outcome", ["open", "unknown"])
    def test_the_fallback_still_demands_a_definite_closed(self, outcome: str) -> None:
        """判据强度不变：回落之后仍然只有明确的 `closed` 才放行

        `open`（所有者还在）与 `unknown`（超时 / 不可达 —— 与「被 DROP 吞掉」在网络层
        完全同形，§2.6）都照旧拦住。
        """
        chains = build_chains(
            ((LEGACY_CC_MARKER, workspace_rules(LEGACY_CC_MARKER, "nekro-cc0", "172.20.0.0/16", ("172.20.0.1", 8031))),),
        )
        candidate = judge(chains=chains, liveness={("172.20.0.1", 8031): outcome})[LEGACY_CC_MARKER]
        assert candidate.verdict == "blocked"

    def test_the_fallback_blocks_when_the_probe_result_is_missing(self) -> None:
        """漏探等同判不准 —— 回落这条路上默认值取错方向同样是「一次探测失败 = 一次删除」"""
        chains = build_chains(
            ((LEGACY_CC_MARKER, workspace_rules(LEGACY_CC_MARKER, "nekro-cc0", "172.20.0.0/16", ("172.20.0.1", 8031))),),
        )
        assert judge(chains=chains, liveness={})[LEGACY_CC_MARKER].verdict == "blocked"

    def test_a_marker_with_no_probeable_endpoint_at_all_is_blocked(self) -> None:
        """两个证据源都没有：沙盒兄弟不在链上，这组规则自己也一条放行端点都没有

        真实形态是「CC 工作区不走模型中继」（`relay_port` 为 0 -> 整组规则里没有任何
        `-d <单机地址> --dport`）。回落必须在这里止步，而不是退化成「没有拦截理由 = 可以删」。
        """
        spec = WorkspaceNetworkSpec(
            bridge_interface="nekro-cc0",
            ipv4_subnet="172.20.0.0/16",
            controller_addresses=(),
            api_port=8080,
            marker=LEGACY_CC_MARKER,
        )
        chains = build_chains(((LEGACY_CC_MARKER, build_workspace_isolation_rules(spec, "ipv4", FORWARD_CHAIN)),))
        candidate = judge(chains=chains, liveness=ALL_CLOSED)[LEGACY_CC_MARKER]
        assert candidate.verdict == "blocked"
        assert "自己也没有可探测的放行端点" in "".join(candidate.blockers)

    def test_a_sandbox_sibling_without_endpoints_does_not_short_circuit_the_fallback(self) -> None:
        """沙盒兄弟在链上、但一条可探端点都没有

        这一档最危险：把它当成"证据源已找到"选中，端点集合是空的，循环一条都不跑，
        于是**一条拦截理由都产生不了** —— `orphan` 就这样从一个空的证据集里得出来。
        所以证据源的判据是「有端点可探」而不是「标记存在」。
        """
        crippled = f'-A {INPUT_CHAIN} -i nekro-sbx0 -m comment --comment "{LEGACY_SANDBOX_MARKER}" -j DROP'
        chains = build_chains(
            ((LEGACY_CC_MARKER, workspace_rules(LEGACY_CC_MARKER, "nekro-cc0", "172.20.0.0/16", ("172.20.0.1", 8031))),),
        )
        chains[("ipv4", INPUT_CHAIN)].append(crippled)
        verdicts = judge(chains=chains, liveness=ALL_CLOSED)
        # 沙盒标记自己：两个证据源都空 -> 拦住
        assert verdicts[LEGACY_SANDBOX_MARKER].verdict == "blocked"
        # CC 标记：跳过这个没用的兄弟，回落到自己的端点
        assert verdicts[LEGACY_CC_MARKER].verdict == "orphan", verdicts[LEGACY_CC_MARKER].blockers

    def test_the_sandbox_sibling_still_wins_when_it_has_endpoints(self) -> None:
        """回落只在**找不到**沙盒兄弟时发生：兄弟在，就仍然由它说了算

        少了这条，「总是用自己的端点」这个变异不会红，而那会让 CC 标记的判定
        与同作用域沙盒规则给出的存活证据脱钩。
        """
        liveness = {**ALL_CLOSED, ("172.19.0.1", 8031): "open"}
        verdicts = judge(liveness=liveness)
        for marker in (LEGACY_CC_MARKER, LEGACY_CC_TENANT_MARKER):
            assert verdicts[marker].verdict == "blocked"
            assert LEGACY_SANDBOX_MARKER in "".join(verdicts[marker].blockers)
            assert not any("改用" in note for note in verdicts[marker].notes)

    def test_tenant_group_marker_inherits_the_scope_liveness(self) -> None:
        """`nekro-cc:default.t1` 的存活判据回落到 `nekro-sandbox:default`"""
        liveness = {**ALL_CLOSED, ("172.19.0.1", 8031): "open"}
        assert judge(liveness=liveness)[LEGACY_CC_TENANT_MARKER].verdict == "blocked"

    def test_self_markers_never_become_deletable(self) -> None:
        """哪怕它的网桥空了、端口也没人听，本部署自用的标记也不进可删清单"""
        verdicts = judge(liveness=ALL_CLOSED)
        for marker in (isolation_marker(CURRENT_SCOPE), workspace_isolation_marker(CURRENT_SCOPE)):
            assert verdicts[marker].verdict == "self"
            assert verdicts[marker].blockers


# =============================================================================
# 宿主事实的采集
# =============================================================================


class TestHostContainerFacts:
    async def test_running_and_stopped_containers_both_count(self) -> None:
        docker = FakeDocker(
            containers=[
                container_summary("running-cc", networks=["nekro_cc_net"]),
                # 已停止：`NetworkSettings.Networks` 是空的，只剩 NetworkMode 钉着旧网络
                container_summary("stopped-cc", mode="nekro_cc_net"),
            ],
            networks={"nekro_cc_net": network_detail("nekro_cc_net", "nekro-cc0", ["running-cc"])},
        )
        facts = await guard.collect_host_container_facts(docker)
        assert facts.readable
        assert facts.bridges["nekro-cc0"].container_total == 2
        assert facts.bridges["nekro-cc0"].container_names == ("running-cc", "stopped-cc")

    async def test_ownership_labels_are_indexed_by_scope(self) -> None:
        docker = FakeDocker(
            containers=[
                container_summary("cc", labels={"nekro.owner.scope": "default"}),
                container_summary("sbx", labels={"nekro.sandbox.instance": "r16fork-"}),
                # NapCat 的归属标签是另一套命名契约（§3 #46），不能算进作用域
                container_summary("napcat", labels={"nekro.napcat.owner": "default"}),
            ],
        )
        facts = await guard.collect_host_container_facts(docker)
        assert facts.scopes_claimed == {"default": ("cc",), "r16fork-": ("sbx",)}

    async def test_a_broken_container_listing_is_unreadable(self) -> None:
        facts = await guard.collect_host_container_facts(FakeDocker(broken_containers=True))
        assert facts.readable is False

    async def test_a_network_that_cannot_be_inspected_is_unreadable(self) -> None:
        docker = FakeDocker(
            containers=[container_summary("cc", networks=["nekro_cc_net"])],
            failing_networks=["nekro_cc_net"],
        )
        facts = await guard.collect_host_container_facts(docker)
        assert facts.readable is False

    async def test_container_show_is_never_called(self) -> None:
        """硬化部署下 docker-guard 会让「别人的容器」inspect 直接 404，而这里要数的就是别人的容器

        假件里的容器是纯 dict，压根没有 `show()`；真的调了就会 AttributeError。
        """
        docker = FakeDocker(
            containers=[container_summary("foreign", networks=["nekro_cc_net"])],
            networks={"nekro_cc_net": network_detail("nekro_cc_net", "nekro-cc0")},
        )
        facts = await guard.collect_host_container_facts(docker)
        assert facts.readable


# =============================================================================
# 端到端：预览与执行
# =============================================================================


async def _ready(executor: FakeIptables) -> FakeIptables:
    """把一个现成的假 executor 包成 `resolve_executor` 那个协程的返回值"""
    return executor


def install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    executor: FakeIptables,
    docker: FakeDocker,
    liveness: Optional[Dict[Tuple[str, int], str]] = None,
):
    async def fake_resolve_executor(_docker):
        return executor

    async def fake_probe(address: str, port: int, timeout: float = 2.0):
        return (liveness or ALL_CLOSED).get((address, port), "unknown")

    monkeypatch.setattr(guard, "resolve_executor", fake_resolve_executor)
    monkeypatch.setattr(guard, "probe_tcp_endpoint", fake_probe)
    monkeypatch.setattr(console_network_rules.aiodocker, "Docker", lambda *_a, **_k: docker)


class TestPlanAndExecute:
    async def test_plan_lists_every_marker_with_its_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)

        plan = await plan_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS)
        by_marker = {candidate.marker: candidate for candidate in plan.candidates}
        assert plan.available
        assert plan.scope == CURRENT_SCOPE
        assert by_marker[LEGACY_SANDBOX_MARKER].verdict == "orphan"
        assert by_marker[isolation_marker(CURRENT_SCOPE)].verdict == "self"
        # 预览是只读的：一条 -D 都没发过
        assert all(argv[0] != "-D" for _family, argv in executor.calls)

    async def test_cleanup_removes_only_the_target_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)

        untouched_before = {
            key: [line for line in lines if not marked_with(line, LEGACY_SANDBOX_MARKER)]
            for key, lines in executor.chains.items()
        }
        outcome = await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, LEGACY_SANDBOX_MARKER)

        assert outcome.deleted == outcome.expected > 0
        assert outcome.residual == 0
        assert outcome.failures == ()
        for key, lines in executor.chains.items():
            assert lines == untouched_before[key], f"{key} 上别人的规则被动过了"

    async def test_cleanup_refuses_a_blocked_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker(
            containers=[container_summary("nekro-cc-live", mode="nekro_cc_net")],
            networks={"nekro_cc_net": network_detail("nekro_cc_net", "nekro-cc0")},
        )
        install_fakes(monkeypatch, executor, docker)
        snapshot = {key: list(lines) for key, lines in executor.chains.items()}

        with pytest.raises(OrphanCleanupRefused) as excinfo:
            await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, LEGACY_CC_MARKER)
        assert "还有 1 个容器" in "".join(excinfo.value.reasons)
        assert executor.chains == snapshot

    async def test_cleanup_refuses_our_own_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)
        snapshot = {key: list(lines) for key, lines in executor.chains.items()}

        with pytest.raises(OrphanCleanupRefused):
            await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, isolation_marker(CURRENT_SCOPE))
        assert executor.chains == snapshot

    async def test_cleanup_refuses_an_unknown_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)

        with pytest.raises(OrphanCleanupRefused):
            await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, "nekro-sandbox:never-installed")

    async def test_the_verdict_is_recomputed_at_execution_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """预览通过、确认之前有人把工作区启动起来 —— 拿旧结论去删就是删掉正在生效的兜底 DROP"""
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        idle = FakeDocker()
        install_fakes(monkeypatch, executor, idle)
        plan = await plan_orphan_rule_cleanup(idle, FAMILIES, OWN_PORTS)
        assert {c.marker: c.verdict for c in plan.candidates}[LEGACY_CC_MARKER] == "orphan"

        busy = FakeDocker(
            containers=[container_summary("nekro-cc-just-started", networks=["nekro_cc_net"])],
            networks={"nekro_cc_net": network_detail("nekro_cc_net", "nekro-cc0", ["nekro-cc-just-started"])},
        )
        install_fakes(monkeypatch, executor, busy)
        with pytest.raises(OrphanCleanupRefused):
            await execute_orphan_rule_cleanup(busy, FAMILIES, OWN_PORTS, LEGACY_CC_MARKER)

    async def test_an_owner_that_comes_back_between_preview_and_confirm_blocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TOCTOU 的另一半：变的不是容器数，而是**探测结果**

        原来只有「预览之后有人把工作区启动起来」这一条用例，而存活探测同样会变
        ——邻居那套部署在预览时正好在重启（端口被拒 -> `closed`），几秒后回来。
        执行前重跑判定必须把这一种也接住。
        """
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker, liveness=ALL_CLOSED)
        plan = await plan_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS)
        assert {c.marker: c.verdict for c in plan.candidates}[LEGACY_SANDBOX_MARKER] == "orphan"

        install_fakes(monkeypatch, executor, docker, liveness={**ALL_CLOSED, ("172.19.0.1", 8031): "open"})
        with pytest.raises(OrphanCleanupRefused) as excinfo:
            await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, LEGACY_SANDBOX_MARKER)
        assert "仍有进程在监听" in "；".join(excinfo.value.reasons)
        assert all(argv[0] != "-D" for _family, argv in executor.calls)

    async def test_a_foreign_marker_on_the_chain_is_not_deletable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """别人的规则就在同一条链上，标记原样回填也不该删得掉

        `marker` 是请求体里的任意字符串，唯一的闸门是「它必须出现在判定结果里」，
        而判定只收本项目前缀的标记。这一条把那个闸门钉住：`someone-else:default`
        确实在链上（`FOREIGN_LINES[1]`），照样一条 `-D` 都发不出去。
        """
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)
        snapshot = {key: list(lines) for key, lines in executor.chains.items()}

        for marker in ("someone-else:default", "nekro-sandbox", "", "*"):
            with pytest.raises((OrphanCleanupRefused, ValidationError)):
                await console_network_rules.cleanup_orphan_isolation_rules(
                    console_network_rules.OrphanRuleCleanupRequest(marker=marker, confirm_marker=marker),
                    _current_user=make_user(Role.Super),
                )
        assert executor.chains == snapshot

    async def test_indexed_fallback_still_clears_the_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """跨 iptables 版本按原文重放删不掉时，行号兜底必须接住"""
        executor = FakeIptables(build_chains(DEFAULT_GROUPS), text_delete_broken=True)
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)

        untouched_before = {
            key: [line for line in lines if not marked_with(line, LEGACY_CC_TENANT_MARKER)]
            for key, lines in executor.chains.items()
        }
        outcome = await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, LEGACY_CC_TENANT_MARKER)
        assert outcome.residual == 0
        assert outcome.deleted > 0
        for key, lines in executor.chains.items():
            assert lines == untouched_before[key]

    async def test_delete_commands_are_argv_arrays_not_shell_strings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """上一轮翻车的正是「拼成一行交给 shell」——引号丢了，规则匹配不上"""
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)
        await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, LEGACY_SANDBOX_MARKER)

        deletes = [argv for _family, argv in executor.calls if argv and argv[0] == "-D"]
        assert deletes
        for argv in deletes:
            assert all(isinstance(token, str) for token in argv)
            # 标记以**独立一个 argv 元素**出现，没有任何引号残留
            if LEGACY_SANDBOX_MARKER in argv:
                assert f'"{LEGACY_SANDBOX_MARKER}"' not in argv
            assert not any(token.startswith("iptables") for token in argv)


class TestInstallStillUsesTheSharedPurge:
    """「先清后装」与孤儿清理共用同一段删除逻辑，重构不能把幂等性弄丢"""

    async def test_reinstall_does_not_stack_rules(self) -> None:
        marker = isolation_marker(CURRENT_SCOPE)
        rules = sandbox_rules(marker, "nksbx-11112222", "172.30.0.0/16", ("172.30.0.1", 8021))
        chains = build_chains(((marker, rules),))
        executor = FakeIptables(chains)
        before = {key: len(lines) for key, lines in executor.chains.items()}

        def build(family, forward_chain):
            spec = SandboxNetworkSpec(
                bridge_interface="nksbx-11112222",
                ipv4_subnet="172.30.0.0/16",
                rpc_endpoints=(("172.30.0.1", 8021),),
                marker=marker,
            )
            return build_isolation_rules(spec, family, forward_chain)

        await install_marked_rules(executor, marker, build)
        assert {key: len(lines) for key, lines in executor.chains.items()} == before

    async def test_reinstall_clears_stale_rules_even_when_text_replay_fails(self) -> None:
        marker = isolation_marker(CURRENT_SCOPE)
        stale = sandbox_rules(marker, "nksbx-old", "172.29.0.0/16", ("172.29.0.1", 9999))
        executor = FakeIptables(build_chains(((marker, stale),)), text_delete_broken=True)

        def build(family, forward_chain):
            spec = SandboxNetworkSpec(
                bridge_interface="nksbx-11112222",
                ipv4_subnet="172.30.0.0/16",
                rpc_endpoints=(("172.30.0.1", 8021),),
                marker=marker,
            )
            return build_isolation_rules(spec, family, forward_chain)

        await install_marked_rules(executor, marker, build)
        remaining = [line for lines in executor.chains.values() for line in lines if "nksbx-old" in line]
        assert remaining == []


# =============================================================================
# 接线：这条路径真的被跑到，而且真的走产品自己的清理函数
# =============================================================================


def _calls_in(function) -> List[str]:
    tree = ast.parse(inspect.getsource(function).lstrip())
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.append(target.id)
            elif isinstance(target, ast.Attribute):
                names.append(target.attr)
    return names


class TestWiring:
    """零件对不等于接上了（§2.7）：判定与清理必须真的串在控制台这条路径上"""

    def test_router_calls_the_product_cleanup_path(self) -> None:
        assert "execute_orphan_rule_cleanup" in _calls_in(console_network_rules.cleanup_orphan_isolation_rules)
        assert "plan_orphan_rule_cleanup" in _calls_in(console_network_rules.get_orphan_isolation_rules)

    def test_cleanup_goes_through_build_cleanup_commands(self) -> None:
        assert "purge_marked_rules" in _calls_in(guard.execute_orphan_rule_cleanup)
        assert "purge_marked_rules_on_chain" in _calls_in(guard.purge_marked_rules)
        chain_calls = _calls_in(guard.purge_marked_rules_on_chain)
        assert "build_cleanup_commands" in chain_calls
        assert "build_indexed_cleanup_commands" in chain_calls

    def test_install_shares_the_same_purge(self) -> None:
        """两处各写一份的话，只有一份带行号兜底——另一份会静默地一条都删不掉"""
        assert "purge_marked_rules_on_chain" in _calls_in(guard.install_marked_rules)

    def test_execute_replans_before_deleting(self) -> None:
        source = inspect.getsource(guard.execute_orphan_rule_cleanup)
        assert source.index("plan_orphan_rule_cleanup") < source.index("purge_marked_rules(")

    def test_router_is_registered_in_the_console_package(self) -> None:
        text = Path("nekro_agent/routers/console/__init__.py").read_text(encoding="utf-8")
        assert "from .network_rules import router as network_rules_router" in text
        assert "router.include_router(network_rules_router)" in text

    def test_both_guards_contribute_a_marker_family(self) -> None:
        families = console_network_rules.guard_marker_families()
        assert {family.prefix for family in families} == {"nekro-sandbox", "nekro-cc"}
        assert next(f for f in families if f.prefix == "nekro-cc").long_lived_containers is True
        assert next(f for f in families if f.prefix == "nekro-sandbox").long_lived_containers is False


# =============================================================================
# 权限与二次确认
# =============================================================================


def make_user(perm_level: Role) -> DBUser:
    return DBUser(
        username=f"user-{perm_level}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
    )


class ExplodingIptables:
    description = "exploding"

    async def run(self, family, args):  # pragma: no cover - 被调用即视为失败
        raise AssertionError(f"权限闸门之后不该还有 iptables 调用: {family} {list(args)}")


class TestConsolePermission:
    """平台方专属：iptables 规则是宿主级资源，一条都不属于任何租户"""

    async def test_tenant_admin_is_rejected_by_the_role_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(_docker):
            return ExplodingIptables()

        monkeypatch.setattr(guard, "resolve_executor", boom)
        monkeypatch.setattr(
            console_network_rules.aiodocker,
            "Docker",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("权限闸门之前不该建 Docker 客户端")),
        )

        with pytest.raises(HTTPException) as excinfo:
            await console_network_rules.get_orphan_isolation_rules(_current_user=make_user(Role.Admin))
        assert excinfo.value.status_code == 403

        with pytest.raises(HTTPException) as excinfo:
            await console_network_rules.cleanup_orphan_isolation_rules(
                console_network_rules.OrphanRuleCleanupRequest(
                    marker=LEGACY_SANDBOX_MARKER,
                    confirm_marker=LEGACY_SANDBOX_MARKER,
                ),
                _current_user=make_user(Role.Admin),
            )
        assert excinfo.value.status_code == 403

    async def test_platform_owner_gets_the_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)
        response = await console_network_rules.get_orphan_isolation_rules(_current_user=make_user(Role.Super))
        assert response.available
        assert {item.marker for item in response.candidates} >= {LEGACY_SANDBOX_MARKER, LEGACY_CC_MARKER}
        assert docker.closed


class TestConfirmBackfill:
    """回填确认，与认领 / 并入 / 历史归属迁移同一套形态"""

    async def test_mismatched_confirmation_is_rejected_before_touching_iptables(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            console_network_rules.aiodocker,
            "Docker",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("确认不匹配时不该建 Docker 客户端")),
        )
        with pytest.raises(ValidationError):
            await console_network_rules.cleanup_orphan_isolation_rules(
                console_network_rules.OrphanRuleCleanupRequest(
                    marker=LEGACY_SANDBOX_MARKER,
                    confirm_marker="nekro-sandbox:typo",
                ),
                _current_user=make_user(Role.Super),
            )

    @pytest.mark.parametrize(
        "confirmation",
        [
            LEGACY_SANDBOX_MARKER.upper(),  # 大小写不同 —— 标记本身大小写敏感
            LEGACY_SANDBOX_MARKER.replace(":", ": "),  # 中间多一个空格
            LEGACY_SANDBOX_MARKER[:-1],  # 少一个字
            LEGACY_SANDBOX_MARKER + "x",  # 多一个字
            "",  # 空确认
            "*",  # 通配意图
            LEGACY_CC_MARKER,  # 回填的是**另一个**同样在链上的标记
        ],
    )
    async def test_only_a_verbatim_backfill_is_accepted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        confirmation: str,
    ) -> None:
        """回填确认只认逐字相同：大小写、内部空格、少一字多一字、通配、串台全部拦住

        闸门在**建 Docker 客户端之前**，所以这些形态连一次 iptables 读取都触发不了。
        """
        monkeypatch.setattr(
            console_network_rules.aiodocker,
            "Docker",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("确认不匹配时不该建 Docker 客户端")),
        )
        with pytest.raises(ValidationError):
            await console_network_rules.cleanup_orphan_isolation_rules(
                console_network_rules.OrphanRuleCleanupRequest(
                    marker=LEGACY_SANDBOX_MARKER,
                    confirm_marker=confirmation,
                ),
                _current_user=make_user(Role.Super),
            )

    async def test_surrounding_whitespace_is_normalised_on_both_sides(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """对照组：前后空白两侧都 `strip()`，所以粘贴时多带的空格不算「错字」

        少了这条，「两侧都别 strip」与「只 strip 一侧」这两个变异分不出来 ——
        只 strip 一侧会让**正确**的回填被拒（可用性），两侧都不 strip 则让粘贴永远失败。
        """
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)
        response = await console_network_rules.cleanup_orphan_isolation_rules(
            console_network_rules.OrphanRuleCleanupRequest(
                marker=f"  {LEGACY_SANDBOX_MARKER} ",
                confirm_marker=f"\t{LEGACY_SANDBOX_MARKER}\n",
            ),
            _current_user=make_user(Role.Super),
        )
        assert response.ok
        assert response.marker == LEGACY_SANDBOX_MARKER

    async def test_empty_marker_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            console_network_rules.aiodocker,
            "Docker",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("空标记时不该建 Docker 客户端")),
        )
        with pytest.raises(ValidationError):
            await console_network_rules.cleanup_orphan_isolation_rules(
                console_network_rules.OrphanRuleCleanupRequest(marker="   ", confirm_marker="   "),
                _current_user=make_user(Role.Super),
            )

    async def test_matching_confirmation_executes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)
        response = await console_network_rules.cleanup_orphan_isolation_rules(
            console_network_rules.OrphanRuleCleanupRequest(
                marker=LEGACY_SANDBOX_MARKER,
                confirm_marker=LEGACY_SANDBOX_MARKER,
            ),
            _current_user=make_user(Role.Super),
        )
        assert response.ok
        assert response.residual == 0
        assert response.deleted == response.expected > 0
        assert not any(marked_with(line, LEGACY_SANDBOX_MARKER) for lines in executor.chains.values() for line in lines)

    async def test_blocked_marker_returns_the_reasons(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker(
            containers=[container_summary("nekro-cc-live", networks=["nekro_cc_net"])],
            networks={"nekro_cc_net": network_detail("nekro_cc_net", "nekro-cc0", ["nekro-cc-live"])},
        )
        install_fakes(monkeypatch, executor, docker)
        with pytest.raises(ValidationError) as excinfo:
            await console_network_rules.cleanup_orphan_isolation_rules(
                console_network_rules.OrphanRuleCleanupRequest(
                    marker=LEGACY_CC_MARKER,
                    confirm_marker=LEGACY_CC_MARKER,
                ),
                _current_user=make_user(Role.Super),
            )
        assert "容器" in str(excinfo.value)


class _FakeDriverConfig:
    def __init__(self, port: int) -> None:
        self.port = port


class _FakeDriver:
    def __init__(self, port: int) -> None:
        self.config = _FakeDriverConfig(port)


def pin_process_bound_port(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    """让 `relay_listen_port()` 真的返回容器内那个端口

    **不能去替换 `cc_credential` 上的那个函数属性**：
    `routers/console/network_rules.py` 是 `from ... import` 进来的，模块加载时就把函数对象
    抄进了自己的命名空间，替换源模块属性对它无效 —— 实测把进程内那个端口
    加回 `own_listening_ports()` 的变异，在那种打法下**存活**（§2.9：假件没打在
    被测代码真正会查的那个名字上）。这里改成替换它真正读的那一跳
    （nonebot 驱动的 `config.port`），两个调用点一起覆盖。
    """
    import nonebot

    monkeypatch.setattr(nonebot, "get_driver", lambda: _FakeDriver(port))


class TestOwnListeningPorts:
    """「这是我们自己的端口」这条豁免的边界

    豁免本身是必要的：探测打的是宿主上的 `(网桥网关, 端口)`，而 NA 绑的是 `0.0.0.0`，
    于是每一个网桥网关地址上都是我们在应答 —— 不豁免的话「清掉自己升级前那组规则」
    这个主用例永远拦在「端口上还有人在听」上。

    但它必须收敛到**宿主侧**端口。NA 在容器里时进程 bind 的那个（compose 下恒为 8021）
    落在另一个网络命名空间，宿主上的 8021 完全可能是同宿主上另一套**裸机**部署占着的
    —— 而裸机部署的默认端口正好也是 8021。
    """

    def test_expose_port_is_always_included(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(OsEnv, "EXPOSE_PORT", 8041)
        assert 8041 in console_network_rules.own_listening_ports()

    def test_the_container_internal_port_is_not_a_host_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NA 在容器里时，容器内那个端口**不是**我们在宿主上占着的端口

        把它算进豁免，同宿主上一套裸机邻居（默认也在 8021）的沙盒放行端点就会被跳过探测，
        于是它在「没有任何存活证据」下被判成孤儿 —— 正好复现 §2.6 那场事故。
        """
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)
        monkeypatch.setattr(OsEnv, "EXPOSE_PORT", 8031)
        pin_process_bound_port(monkeypatch, 8021)
        ports = console_network_rules.own_listening_ports()
        assert ports == frozenset({8031})
        assert 8021 not in ports

    def test_bare_metal_still_exempts_the_port_it_binds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照组：裸机部署 bind 的就是宿主端口，那一档豁免不能跟着一起丢掉"""
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", False)
        monkeypatch.setattr(OsEnv, "EXPOSE_PORT", 8031)
        pin_process_bound_port(monkeypatch, 8021)
        assert console_network_rules.own_listening_ports() == frozenset({8031, 8021})


class TestProbeFailuresNeverBecomePermissionToDelete:
    """探测**自己抛异常**那一档

    `_probe_orphan_endpoints` 把探测异常收成 `unknown`，判定侧再把 `unknown` 当成
    「判不准」拦住。这条兜底的方向错了就是最贵的一种错：一次 `EMFILE`（探测在
    `asyncio.to_thread` 里新建 socket，进程 fd 用尽时 `socket.socket()` 就抛）
    会被读成「没人在听」，于是邻居**还在跑**的那组规则被判成孤儿删掉。

    §2.16 那条教训的同一个形态：写了「出错就降级」，就必须有一条用例**真的让它出错**。
    实测把这一档改成 `closed` 时，改前的整套用例（含端到端）一条都不红。
    """

    def _chains(self):
        marker = LEGACY_SANDBOX_MARKER
        return build_chains(
            ((marker, sandbox_rules(marker, "nekro-sbx0", "172.19.0.0/16", ("172.19.0.1", 8031))),),
        )

    async def test_an_exploding_injected_probe_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def exploding_probe(address: str, port: int):
            raise OSError(24, "Too many open files")

        executor = FakeIptables(self._chains())
        docker = FakeDocker()
        monkeypatch.setattr(guard, "resolve_executor", lambda _docker: _ready(executor))

        plan = await plan_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, probe=exploding_probe)
        candidate = next(item for item in plan.candidates if item.marker == LEGACY_SANDBOX_MARKER)
        assert candidate.verdict == "blocked"
        assert "没有得到明确结果" in "".join(candidate.blockers)

    async def test_an_exploding_default_probe_also_blocks_and_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认那条路（router 不传 `probe`）同样要拦住，并且真的拒绝执行"""

        async def exploding_probe(address: str, port: int, timeout: float = 2.0):
            raise RuntimeError("thread pool exhausted")

        executor = FakeIptables(self._chains())
        docker = FakeDocker()
        monkeypatch.setattr(guard, "resolve_executor", lambda _docker: _ready(executor))
        monkeypatch.setattr(guard, "probe_tcp_endpoint", exploding_probe)

        with pytest.raises(OrphanCleanupRefused) as excinfo:
            await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, LEGACY_SANDBOX_MARKER)
        assert "没有得到明确结果" in "；".join(excinfo.value.reasons)
        assert all(argv[0] != "-D" for _family, argv in executor.calls)

    async def test_the_same_setup_is_deletable_when_the_probe_answers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """对照组（§2.11）：只把探测换成正常应答，同一组输入立刻判得出 orphan

        没有这一条的话，上面两条也可能只是因为「这组输入本来就删不掉」而通过。
        """

        async def answering_probe(address: str, port: int, timeout: float = 2.0):
            return "closed"

        executor = FakeIptables(self._chains())
        docker = FakeDocker()
        monkeypatch.setattr(guard, "resolve_executor", lambda _docker: _ready(executor))
        monkeypatch.setattr(guard, "probe_tcp_endpoint", answering_probe)

        plan = await plan_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS)
        candidate = next(item for item in plan.candidates if item.marker == LEGACY_SANDBOX_MARKER)
        assert candidate.verdict == "orphan"


class TestANeighbourOnTheSamePortNumberIsNotOurs:
    """我们在容器里、邻居在裸机上，两边的端口号都是 8021

    这不是构造出来的巧合：compose 形态下容器内恒为 8021，而裸机部署的默认端口也是 8021。
    两个 socket 在不同的网络命名空间里，**不冲突** —— 「宿主 TCP 端口独占」这句话
    对容器内那个端口不成立。
    """

    NEIGHBOUR_ENDPOINT = ("172.17.0.1", 8021)

    def _chains(self):
        marker = LEGACY_SANDBOX_MARKER
        return build_chains(
            (
                (marker, sandbox_rules(marker, "nekro-sbx0", "172.19.0.0/16", self.NEIGHBOUR_ENDPOINT)),
                (
                    isolation_marker(CURRENT_SCOPE),
                    sandbox_rules(isolation_marker(CURRENT_SCOPE), "nksbx-11112222", "172.30.0.0/16", ("172.30.0.1", 8031)),
                ),
            ),
        )

    def _as_container_deployment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", True)
        monkeypatch.setattr(OsEnv, "EXPOSE_PORT", 8031)
        pin_process_bound_port(monkeypatch, 8021)

    async def test_a_live_neighbour_on_8021_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._as_container_deployment(monkeypatch)
        executor = FakeIptables(self._chains())
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker, liveness={self.NEIGHBOUR_ENDPOINT: "open"})

        plan = await plan_orphan_rule_cleanup(docker, FAMILIES, console_network_rules.own_listening_ports())
        candidate = next(item for item in plan.candidates if item.marker == LEGACY_SANDBOX_MARKER)
        assert candidate.verdict == "blocked"
        assert "仍有进程在监听" in "".join(candidate.blockers)

        with pytest.raises(OrphanCleanupRefused):
            await execute_orphan_rule_cleanup(
                docker,
                FAMILIES,
                console_network_rules.own_listening_ports(),
                LEGACY_SANDBOX_MARKER,
            )
        assert all(argv[0] != "-D" for _family, argv in executor.calls)

    async def test_a_dead_owner_on_8021_is_still_deletable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照组（§2.11）：同一组输入下探测改成 `closed` 就判得出 orphan

        少了这条，上面那条也可能只是因为「这台机器上什么都删不掉」而通过。
        """
        self._as_container_deployment(monkeypatch)
        executor = FakeIptables(self._chains())
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker, liveness={self.NEIGHBOUR_ENDPOINT: "closed"})

        plan = await plan_orphan_rule_cleanup(docker, FAMILIES, console_network_rules.own_listening_ports())
        candidate = next(item for item in plan.candidates if item.marker == LEGACY_SANDBOX_MARKER)
        assert candidate.verdict == "orphan"

    async def test_the_endpoint_is_really_probed_not_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """接线：豁免收窄之后，这个端点必须**真的**被探一次

        判据落在「探测被调用过」而不是只落在判定结果上 —— 豁免掉的端点连探都不探，
        两者的差别只有在这里看得见。
        """
        self._as_container_deployment(monkeypatch)
        probed: List[Tuple[str, int]] = []

        async def recording_probe(address: str, port: int, timeout: float = 2.0):
            probed.append((address, port))
            return "open"

        executor = FakeIptables(self._chains())
        docker = FakeDocker()
        monkeypatch.setattr(guard, "resolve_executor", lambda _docker: _ready(executor))
        monkeypatch.setattr(guard, "probe_tcp_endpoint", recording_probe)
        await plan_orphan_rule_cleanup(docker, FAMILIES, console_network_rules.own_listening_ports())
        assert self.NEIGHBOUR_ENDPOINT in probed

    async def test_our_own_host_port_is_still_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """反向对照：宿主侧那个端口仍然豁免，主用例没有被这次收窄误伤"""
        self._as_container_deployment(monkeypatch)
        marker = LEGACY_SANDBOX_MARKER
        chains = build_chains(((marker, sandbox_rules(marker, "nekro-sbx0", "172.19.0.0/16", ("172.19.0.1", 8031))),))
        probed: List[Tuple[str, int]] = []

        async def recording_probe(address: str, port: int, timeout: float = 2.0):
            probed.append((address, port))
            return "open"

        executor = FakeIptables(chains)
        docker = FakeDocker()
        monkeypatch.setattr(guard, "resolve_executor", lambda _docker: _ready(executor))
        monkeypatch.setattr(guard, "probe_tcp_endpoint", recording_probe)
        plan = await plan_orphan_rule_cleanup(docker, FAMILIES, console_network_rules.own_listening_ports())
        assert probed == []
        candidate = next(item for item in plan.candidates if item.marker == marker)
        assert candidate.verdict == "orphan"


def containerised_workspace_rules(marker: str, bridge: str, subnet: str, controller: Tuple[str, int]) -> List[IptablesRule]:
    """NA **在容器里**的部署所装的 CC 工作区规则集

    与裸机形态的差别正是本组测试要钉的那一点：放行的 `-d` 是 NA 自己在工作区网桥上的
    **容器**地址（落在转发链上），端口是**容器内**的中继端口 —— compose 下恒为 8021。
    裸机形态那条则在 `INPUT` 上，`-d` 是网桥网关，也就是宿主持有的地址。
    """
    spec = WorkspaceNetworkSpec(
        bridge_interface=bridge,
        ipv4_subnet=subnet,
        controller_addresses=(controller[0],),
        api_port=8080,
        marker=marker,
        relay_port=controller[1],
    )
    return build_workspace_isolation_rules(spec, "ipv4", FORWARD_CHAIN)


class TestTheOwnPortExemptionOnlyCoversHostHeldAddresses:
    """「这个端口是我们自己占着的」这条豁免的成立前提

    豁免的推理是**宿主 TCP 端口独占**：这个端口上应答的只可能是我们自己，所以探到 `open`
    没有信息量。而端口独占只对**宿主持有的地址**成立 —— 转发链上那条 `-d` 是另一套部署的
    NA **容器**地址，别人的容器完全可以在自己的 IP 上占着同一个端口号。

    这一档在 #50 之前碰不到（CC 标记根本没有机会拿自己的端点做判据），回落打开之后
    它就成了主路径：容器化邻居的 CC 端点端口恒为 8021，与裸机部署的默认端口一模一样。
    豁免范围不收窄的话，一套**还活着**的容器化邻居会在零证据下被判成孤儿，
    而 CC 工作区容器是长命的 —— 删掉兜底 DROP 之后它们直接访问宿主内网，不报错也不留日志。
    """

    NEIGHBOUR_CONTAINER_ENDPOINT = ("172.22.0.2", 8021)

    def _chains(self):
        return build_chains(
            (
                (
                    LEGACY_CC_MARKER,
                    containerised_workspace_rules(
                        LEGACY_CC_MARKER,
                        "nekro-cc0",
                        "172.22.0.0/16",
                        self.NEIGHBOUR_CONTAINER_ENDPOINT,
                    ),
                ),
            ),
        )

    def test_a_forward_chain_endpoint_is_not_a_host_endpoint(self) -> None:
        """盘点侧的事实：转发链上的 `-d` 不进 `host_endpoints`，INPUT 上的进"""
        dumps = [
            (family, chain, "\n".join([f"-N {chain}", *lines])) for (family, chain), lines in self._chains().items()
        ]
        entry = collect_marker_inventory(dumps, PREFIXES)[LEGACY_CC_MARKER]
        assert entry.endpoints == (self.NEIGHBOUR_CONTAINER_ENDPOINT,)
        assert entry.host_endpoints == ()

        bare_metal = build_chains(
            ((LEGACY_CC_MARKER, workspace_rules(LEGACY_CC_MARKER, "nekro-cc0", "172.20.0.0/16", ("172.20.0.1", 8031))),),
        )
        dumps = [(family, chain, "\n".join([f"-N {chain}", *lines])) for (family, chain), lines in bare_metal.items()]
        entry = collect_marker_inventory(dumps, PREFIXES)[LEGACY_CC_MARKER]
        assert entry.host_endpoints == (("172.20.0.1", 8031),)

    def test_a_live_containerised_neighbour_on_our_port_number_blocks(self) -> None:
        """端口号与我们相同（8021），但地址是**别人容器**的 —— 必须真探，探到 open 就拦住"""
        candidate = judge(
            chains=self._chains(),
            liveness={self.NEIGHBOUR_CONTAINER_ENDPOINT: "open"},
            own_ports=frozenset({8021}),
        )[LEGACY_CC_MARKER]
        assert candidate.verdict == "blocked"
        assert "仍有进程在监听" in "".join(candidate.blockers)
        assert not any("本部署自己监听的端口" in note for note in candidate.notes)

    def test_the_same_setup_is_deletable_once_the_neighbour_is_gone(self) -> None:
        """对照组（§2.11）：只把探测结果换成 `closed`，同一组输入立刻判得出 orphan"""
        candidate = judge(
            chains=self._chains(),
            liveness={self.NEIGHBOUR_CONTAINER_ENDPOINT: "closed"},
            own_ports=frozenset({8021}),
        )[LEGACY_CC_MARKER]
        assert candidate.verdict == "orphan", candidate.blockers

    async def test_the_container_side_endpoint_is_really_probed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """接线：豁免收窄之后这个端点必须**真的**被探一次

        判据落在「探测被调用过」而不是只落在判定结果上 —— 被豁免掉的端点连探都不探，
        两者的差别只有在这里看得见（`TestANeighbourOnTheSamePortNumberIsNotOurs` 同一形态）。
        """
        probed: List[Tuple[str, int]] = []

        async def recording_probe(address: str, port: int, timeout: float = 2.0):
            probed.append((address, port))
            return "open"

        executor = FakeIptables(self._chains())
        docker = FakeDocker()
        monkeypatch.setattr(guard, "resolve_executor", lambda _docker: _ready(executor))
        monkeypatch.setattr(guard, "probe_tcp_endpoint", recording_probe)
        await plan_orphan_rule_cleanup(docker, FAMILIES, frozenset({8021}))
        assert probed == [self.NEIGHBOUR_CONTAINER_ENDPOINT]

    def test_the_bare_metal_host_endpoint_is_still_exempt(self) -> None:
        """反向对照：INPUT 上那条（网桥网关 = 宿主地址）仍然豁免

        这一档正是 #50 实测那台机器的形态（裸机部署、中继端口就是 8021）。
        豁免一并收掉的话，主用例又会退回「永远拦在端口上还有人在听」。
        """
        chains = build_chains(
            ((LEGACY_CC_MARKER, workspace_rules(LEGACY_CC_MARKER, "nekro-cc0", "172.20.0.0/16", ("172.20.0.1", 8021))),),
        )
        candidate = judge(chains=chains, liveness={}, own_ports=frozenset({8021}))[LEGACY_CC_MARKER]
        assert candidate.verdict == "orphan", candidate.blockers
        assert any("本部署自己监听的端口" in note for note in candidate.notes)


class TestAVerdictBackedByNoProbeAtAllSaysSo:
    """判定完全靠豁免、一次实际探测都没做过时，`notes` 里必须写明这一点

    豁免的推理是「宿主 TCP 端口独占 -> 这个端口上应答的只可能是我们自己」，而端口独占
    只对**宿主持有的地址**成立。沙盒族的回调端点整组都套用这条豁免（收掉它，主用例就会
    永久拦在「端口上还有人在听」上——那个「有人」正是我们自己），可它们按 `host_endpoints`
    那把尺子量其实一条都不算宿主地址：`build_isolation_rules` 里发往宿主的那一组刻意
    「只认端口不认地址」，按地址放行的全在转发链上。于是 `resolve_rpc_endpoints` 把
    非别名主机名解析出来的地址（模块里明说支持的「直连形态 / 内网反向代理」）也一并
    豁免掉——同宿主上一套**还活着**、且代理端口与本部署撞号的邻居，会在**零探测**下判成孤儿。

    从 iptables 文本里分不出「这个地址是不是宿主自己的」，所以这里加不了判据；
    能做的是把它说出来，让运维在按下删除前看得到（页面把 `notes` 原样显示）。
    """

    NOTE = "没有做过任何一次实际探测"

    def _bare_metal(self, port: int):
        return build_chains(
            (
                (
                    LEGACY_CC_MARKER,
                    workspace_rules(LEGACY_CC_MARKER, "nekro-cc0", "172.20.0.0/16", ("172.20.0.1", port)),
                ),
            ),
        )

    def test_an_all_exempt_verdict_carries_the_disclosure(self) -> None:
        """端点端口正好是我们自己的 -> 全被豁免 -> 判定没有任何一次观测支撑"""
        candidate = judge(chains=self._bare_metal(8021), liveness={}, own_ports=frozenset({8021}))[LEGACY_CC_MARKER]
        assert candidate.verdict == "orphan", candidate.blockers
        assert any(self.NOTE in note for note in candidate.notes)

    def test_it_stays_a_note_and_never_becomes_a_blocker(self) -> None:
        """对照：这条披露不改判定 —— 变成拦截理由就等于把主用例（清自己升级前那组）堵死"""
        candidate = judge(chains=self._bare_metal(8021), liveness={}, own_ports=frozenset({8021}))[LEGACY_CC_MARKER]
        assert candidate.blockers == ()
        assert not any(self.NOTE in blocker for blocker in candidate.blockers)

    def test_a_verdict_backed_by_a_real_probe_does_not_carry_it(self) -> None:
        """反向对照（§2.11）：只把端口换成不撞号的那一个，端点就被真探了 -> 不该有这句

        少了这一条，「无脑每次都加一句」也能让上面两条变绿。
        """
        candidate = judge(
            chains=self._bare_metal(8031),
            liveness={("172.20.0.1", 8031): "closed"},
            own_ports=frozenset({8021}),
        )[LEGACY_CC_MARKER]
        assert candidate.verdict == "orphan", candidate.blockers
        assert not any(self.NOTE in note for note in candidate.notes)

    def test_the_sandbox_family_is_the_one_this_disclosure_exists_for(self) -> None:
        """沙盒标记自己：端点全在转发链上（`host_endpoints` 为空），却整组豁免"""
        marker = LEGACY_SANDBOX_MARKER
        chains = build_chains(((marker, sandbox_rules(marker, "nekro-sbx0", "172.19.0.0/16", ("172.19.0.1", 8021))),))
        dumps = [(family, chain, "\n".join([f"-N {chain}", *lines])) for (family, chain), lines in chains.items()]
        entry = collect_marker_inventory(dumps, PREFIXES)[marker]
        # 这才是这条披露存在的理由：按 host_endpoints 那把尺子，沙盒端点一条都不算宿主地址
        assert entry.endpoints == (("172.19.0.1", 8021),)
        assert entry.host_endpoints == ()

        candidate = judge(chains=chains, liveness={}, own_ports=frozenset({8021}))[marker]
        assert candidate.verdict == "orphan", candidate.blockers
        assert any(self.NOTE in note for note in candidate.notes)


class TestTheRefusalReasonIsNotSelfContradictory:
    """拦截理由是照原样显示给人看的，必须说得准

    判的就是 `nekro-sandbox:<作用域>` 自己时，候选沙盒标记里的第一个**就是它**。
    照抄「链上没有与之同作用域的沙盒隔离规则（找过 nekro-sandbox:default）」，
    运维会读到一条自相矛盾的话——那条规则明明就在链上，正在被判定。
    """

    def test_a_sandbox_marker_judging_itself_does_not_claim_it_is_missing(self) -> None:
        crippled = f'-A {INPUT_CHAIN} -i nekro-sbx0 -m comment --comment "{LEGACY_SANDBOX_MARKER}" -j DROP'
        chains = build_chains(())
        chains[("ipv4", INPUT_CHAIN)].append(crippled)
        candidate = judge(chains=chains, liveness={})[LEGACY_SANDBOX_MARKER]
        assert candidate.verdict == "blocked"
        reasons = "".join(candidate.blockers)
        assert "没有可探测的放行端点" in reasons
        assert "链上既没有与之同作用域的沙盒隔离规则" not in reasons
        assert f"找过 {LEGACY_SANDBOX_MARKER}" not in reasons

    def test_a_cc_marker_still_names_the_sandbox_siblings_it_looked_for(self) -> None:
        """反向对照：CC 标记那一档的理由必须仍然点名它找过哪些沙盒标记"""
        spec = WorkspaceNetworkSpec(
            bridge_interface="nekro-cc0",
            ipv4_subnet="172.20.0.0/16",
            controller_addresses=(),
            api_port=8080,
            marker=LEGACY_CC_TENANT_MARKER,
        )
        chains = build_chains(((LEGACY_CC_TENANT_MARKER, build_workspace_isolation_rules(spec, "ipv4", FORWARD_CHAIN)),))
        candidate = judge(chains=chains, liveness={})[LEGACY_CC_TENANT_MARKER]
        assert candidate.verdict == "blocked"
        reasons = "".join(candidate.blockers)
        for sibling in sandbox_marker_candidates(LEGACY_CC_TENANT_MARKER):
            assert sibling in reasons
        assert "自己也没有可探测的放行端点" in reasons


class TestTheCleanupOrderTrap:
    """#50 的端到端形态：先清沙盒、再清 CC，两步都要真的成功

    实测复现（PROJECT_STATE §3 #50）就是这个顺序：清掉 `nekro-sandbox:default` 之后，
    同一个预览接口把 `nekro-cc:default` 与 `nekro-cc:default.t1` 从 `orphan` 翻成了
    `blocked`，理由是「链上没有与之同作用域的沙盒隔离规则」。这不是别的条件变了 ——
    网桥上的容器数前后都是 0。
    """

    async def test_cleaning_the_sandbox_marker_first_does_not_strand_the_cc_markers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)

        before = {item.marker: item.verdict for item in (await plan_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS)).candidates}
        assert before[LEGACY_SANDBOX_MARKER] == "orphan"
        assert before[LEGACY_CC_MARKER] == "orphan"
        assert before[LEGACY_CC_TENANT_MARKER] == "orphan"

        # 运维最容易先点的就是清单上面那一个
        await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, LEGACY_SANDBOX_MARKER)

        after = {item.marker: item.verdict for item in (await plan_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS)).candidates}
        assert LEGACY_SANDBOX_MARKER not in after
        assert after[LEGACY_CC_MARKER] == "orphan"
        assert after[LEGACY_CC_TENANT_MARKER] == "orphan"

        for marker in (LEGACY_CC_MARKER, LEGACY_CC_TENANT_MARKER):
            outcome = await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, marker)
            assert outcome.residual == 0
            assert outcome.deleted == outcome.expected > 0

        remaining = [line for lines in executor.chains.values() for line in lines]
        assert not any(marked_with(line, marker) for line in remaining for marker in (LEGACY_CC_MARKER, LEGACY_CC_TENANT_MARKER))
        # 本部署自己的两组、以及别人的规则一条不少
        for foreign in FOREIGN_LINES:
            assert foreign in remaining
        assert any(marked_with(line, isolation_marker(CURRENT_SCOPE)) for line in remaining)

    async def test_the_stranded_marker_still_blocks_when_its_own_owner_answers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """对照组：沙盒兄弟没了、但 CC 自己那个端点上有人应答 -> 照旧拦住

        没有这一条的话，上面那条也可能是被「回落之后一律放行」蒙对的。
        """
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker()
        install_fakes(monkeypatch, executor, docker)
        await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, LEGACY_SANDBOX_MARKER)

        install_fakes(monkeypatch, executor, docker, liveness={**ALL_CLOSED, ("172.20.0.1", 8031): "open"})
        with pytest.raises(OrphanCleanupRefused) as excinfo:
            await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, LEGACY_CC_MARKER)
        assert "仍有进程在监听" in "；".join(excinfo.value.reasons)


class TestLivenessEvidenceIsResolvedOnce:
    """判定侧与探测侧读的是**同一个**函数

    两边各写一次筛选，任何一次分叉都表现为「判定要用的端点根本没被探过」，
    于是 `liveness` 里查不到、按缺省值当成 `unknown`，整组标记永久卡在 `blocked`
    —— #50 那个顺序陷阱就是这种形态。
    """

    def test_both_sides_call_the_shared_resolver(self) -> None:
        assert "resolve_liveness_evidence" in _calls_in(guard._judge_owner_liveness)
        assert "resolve_liveness_evidence" in _calls_in(guard._probe_orphan_endpoints)

    def test_the_probe_side_does_not_filter_by_marker_prefix(self) -> None:
        """回落打开之后，「只探沙盒前缀的标记」这一句就是把 CC 端点全跳过的那把锁"""
        source = inspect.getsource(guard._probe_orphan_endpoints)
        assert "SANDBOX_MARKER_PREFIX" not in source

    @pytest.mark.parametrize(
        ("marker", "expected_source", "fallback"),
        [
            # 沙盒兄弟在 -> 由它说了算
            (LEGACY_CC_MARKER, LEGACY_SANDBOX_MARKER, False),
            (LEGACY_CC_TENANT_MARKER, LEGACY_SANDBOX_MARKER, False),
            # 沙盒标记自己 -> 证据源就是它自己，且不算回落
            (LEGACY_SANDBOX_MARKER, LEGACY_SANDBOX_MARKER, False),
        ],
    )
    def test_evidence_source_truth_table(self, marker: str, expected_source: str, fallback: bool) -> None:
        dumps = [
            (family, chain, "\n".join([f"-N {chain}", *lines]))
            for (family, chain), lines in build_chains(DEFAULT_GROUPS).items()
        ]
        evidence = guard.resolve_liveness_evidence(marker, collect_marker_inventory(dumps, PREFIXES))
        assert evidence.source_marker == expected_source
        assert evidence.fallback is fallback
        assert evidence.refusal == ""

    def test_evidence_falls_back_to_the_marker_itself(self) -> None:
        chains = build_chains(
            ((LEGACY_CC_MARKER, workspace_rules(LEGACY_CC_MARKER, "nekro-cc0", "172.20.0.0/16", ("172.20.0.1", 8031))),),
        )
        dumps = [(family, chain, "\n".join([f"-N {chain}", *lines])) for (family, chain), lines in chains.items()]
        evidence = guard.resolve_liveness_evidence(LEGACY_CC_MARKER, collect_marker_inventory(dumps, PREFIXES))
        assert evidence.source_marker == LEGACY_CC_MARKER
        assert evidence.fallback is True
        assert evidence.endpoints == (("172.20.0.1", 8031),)
        assert evidence.exemptible == frozenset({("172.20.0.1", 8031)})


class _FakeSocket:
    """`socket.socket` 的替身：`connect_ex` 直接给出预设的返回码或异常"""

    def __init__(self, outcome) -> None:
        self._outcome = outcome
        self.timeout: Optional[float] = None
        self.closed = False
        self.target: Optional[Tuple[str, int]] = None

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def connect_ex(self, target: Tuple[str, int]) -> int:
        self.target = target
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    def close(self) -> None:
        self.closed = True


class TestProbeOutcomeMapping:
    """`connect_ex` 的返回码 -> `open` / `closed` / `unknown`

    这是整个功能里最要命的一张映射表：`closed` 是**唯一**能让一组规则被删掉的正向证据。
    改这一版之前它一条用例都没有 —— 把任何一个「判不准」的错误码挪进 `_REFUSED_ERRNOS`，
    全量测试一条都不会红，而代价是「一次探测失败 = 一次删除」。

    表里的数字不是查手册抄来的，是在**真部署**（测试部署，Linux）上实测的：

    ```
    refused   127.0.0.1:9      -> code 111  (0.0s)
    blackhole 192.0.2.1:8021   -> code 11   (1.0s，正好等于 timeout)
    listening 127.0.0.1:8021   -> code 0    (0.0s)
    ```

    注意**超时返回的是 `11`（EAGAIN/EWOULDBLOCK）而不是 `110`（ETIMEDOUT）**。
    「被防火墙 DROP 掉」与「真的没人监听」在网络层完全同形，靠的就是这一格不能判错。
    """

    OUTCOMES = [
        (0, "open"),
        (111, "closed"),  # Linux ECONNREFUSED —— 真部署上实测的那一个
        (61, "closed"),  # macOS ECONNREFUSED
        (10061, "closed"),  # Windows WSAECONNREFUSED
        (11, "unknown"),  # Linux 上**超时**返回的就是它（实测），绝不能算「没人在听」
        (110, "unknown"),  # ETIMEDOUT
        (113, "unknown"),  # EHOSTUNREACH：旧网桥还在、地址上已经没有容器，ARP 拿不到应答
        (101, "unknown"),  # ENETUNREACH：旧网络已被删掉，宿主没有到该网段的路由
        (13, "unknown"),  # EACCES：本机防火墙把出向包挡掉了
        (10035, "unknown"),  # Windows 上超时与被拒**同码**，因此那台机器上只能判不准
    ]

    def _probe(self, monkeypatch: pytest.MonkeyPatch, outcome, timeout: float = 2.0):
        created: List[_FakeSocket] = []

        def factory(family, kind):
            assert (family, kind) == (guard.socket.AF_INET, guard.socket.SOCK_STREAM)
            created.append(_FakeSocket(outcome))
            return created[-1]

        monkeypatch.setattr(guard.socket, "socket", factory)
        verdict = guard._probe_endpoint_sync("172.19.0.1", 8031, timeout)
        return verdict, created

    @pytest.mark.parametrize(("code", "expected"), OUTCOMES)
    def test_connect_ex_code_truth_table(self, monkeypatch: pytest.MonkeyPatch, code: int, expected: str) -> None:
        verdict, created = self._probe(monkeypatch, code)
        assert verdict == expected
        assert created[0].target == ("172.19.0.1", 8031)

    @pytest.mark.parametrize(
        "error",
        [
            OSError(101, "Network is unreachable"),
            OSError(13, "Permission denied"),
            TimeoutError("timed out"),  # 某些平台上超时是抛出来的，不是返回码
        ],
    )
    def test_a_connect_exception_is_never_read_as_closed(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        assert self._probe(monkeypatch, error)[0] == "unknown"

    def test_a_socket_that_cannot_be_created_at_all_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`socket()` 自己抛（EMFILE：进程 fd 用尽）时异常穿出本函数

        接住它的是 `_probe_orphan_endpoints` 那个 `except`，收成 `unknown`
        （`TestProbeFailuresNeverBecomePermissionToDelete` 钉的就是那一段）。
        这里只钉住「本函数不会把它吞成一个判定」—— 吞成 `closed` 就是一次删除。
        """

        def factory(_family, _kind):
            raise OSError(24, "Too many open files")

        monkeypatch.setattr(guard.socket, "socket", factory)
        with pytest.raises(OSError):
            guard._probe_endpoint_sync("172.19.0.1", 8031, 1.0)

    def test_the_socket_is_always_closed_and_the_timeout_is_applied(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """不设超时的探测会把整个预览请求挂死在一个被 DROP 的地址上；不关 socket 则会烧光 fd"""
        for outcome in (0, 111, OSError(24, "Too many open files")):
            _verdict, created = self._probe(monkeypatch, outcome, timeout=1.5)
            assert created[0].closed is True
            assert created[0].timeout == 1.5

    async def test_the_async_wrapper_does_not_block_the_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """探测是同步阻塞调用，必须搬到线程里 —— 判据落在「在哪个线程跑」这个事实上（§2.17）"""
        import threading

        loop_thread = threading.get_ident()
        seen: List[int] = []

        def factory(_family, _kind):
            seen.append(threading.get_ident())
            return _FakeSocket(111)

        monkeypatch.setattr(guard.socket, "socket", factory)
        assert await guard.probe_tcp_endpoint("172.19.0.1", 8031) == "closed"
        assert seen and seen[0] != loop_thread


class TestIpv6RulesNeverProduceEndpoints:
    """IPv6 侧不产生放行端点，探测也只走 AF_INET —— 两侧口径必须一致

    不一致的形态是具体的：盘点侧收下一个 IPv6 端点，探测侧拿 `AF_INET` 去连它，
    必然失败成 `unknown`，于是**每一个**带 IPv6 规则的标记都永久卡在 `blocked`。
    两个规则集生成函数里那条 `-d <地址>` 放行本来就只在 `family == "ipv4"` 时生成，
    这条用例把「读回来的文本里出现了 IPv6 单机地址也不当端点」钉住。
    """

    def test_an_ipv6_host_destination_is_not_an_endpoint(self) -> None:
        line = (
            f'-A {INPUT_CHAIN} -i nekro-cc0 -d fd00::1/128 -p tcp --dport 8031 '
            f'-m comment --comment "{LEGACY_CC_MARKER}" -j ACCEPT'
        )
        inventory = collect_marker_inventory([("ipv6", INPUT_CHAIN, f"-N {INPUT_CHAIN}\n{line}")], PREFIXES)
        assert inventory[LEGACY_CC_MARKER].endpoints == ()
        assert inventory[LEGACY_CC_MARKER].host_endpoints == ()

    def test_a_marker_with_only_ipv6_rules_is_blocked_not_deleted(self) -> None:
        """判不准就不给删：产品的 IPv6 规则集里一条可探端点都没有 -> 拦住

        规则由**产品自己的生成函数**渲染（手抄的常量会在规则集改动后悄悄失去代表性），
        `family="ipv6"` 时那条按地址放行的例外压根不生成 —— 于是这一档只能是 `blocked`。
        """
        spec = WorkspaceNetworkSpec(
            bridge_interface="nekro-cc0",
            ipv4_subnet="172.20.0.0/16",
            controller_addresses=(),
            api_port=8080,
            marker=LEGACY_CC_MARKER,
            relay_port=8031,
            host_relay_addresses=("172.20.0.1",),
        )
        rules = build_workspace_isolation_rules(spec, "ipv6", FORWARD_CHAIN)
        by_chain: Dict[str, List[str]] = {}
        for rule in rules:
            by_chain.setdefault(rule.chain, []).append(_render_line(rule.chain, rule))
        ipv6_only = [("ipv6", chain, "\n".join([f"-N {chain}", *lines])) for chain, lines in by_chain.items()]
        inventory = collect_marker_inventory(ipv6_only, PREFIXES)
        assert inventory[LEGACY_CC_MARKER].endpoints == ()
        candidates = judge_orphan_candidates(inventory, FAMILIES, CURRENT_SCOPE, readable_host(), {}, OWN_PORTS)
        candidate = next(item for item in candidates if item.marker == LEGACY_CC_MARKER)
        assert candidate.verdict == "blocked"
        assert "没有可探测的放行端点" in "".join(candidate.blockers)


class TestOwnershipLabelPresentButEmpty:
    """归属标签**存在但取值为空**：那是「作用域回落到空串」那个历史版本建的容器

    空作用域被 `format_isolation_marker` 折成 `default`，判定侧
    （`is_marker_of_scope(marker, "", prefixes)`）本来就把它当成一个正经作用域
    —— 而采集侧原本用 `str(labels.get(label) or "").strip()` 把它与「标签压根不存在」
    一并丢掉了。两半判据一分叉，一个还活着的存量容器就认领不到 `nekro-*:default`，
    那批标记会在「没人认领」的结论下被删掉。
    """

    async def test_an_empty_scope_label_still_claims_the_default_scope(self) -> None:
        docker = FakeDocker(
            containers=[
                container_summary("legacy-cc", labels={"nekro.owner.scope": ""}),
                container_summary("unrelated-postgres", labels={"com.docker.compose.project": "other"}),
            ],
        )
        facts = await guard.collect_host_container_facts(docker)
        assert facts.scopes_claimed == {"": ("legacy-cc",)}

    async def test_it_blocks_the_legacy_markers_end_to_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executor = FakeIptables(build_chains(DEFAULT_GROUPS))
        docker = FakeDocker(containers=[container_summary("legacy-cc", labels={"nekro.owner.scope": ""})])
        install_fakes(monkeypatch, executor, docker)

        plan = await plan_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS)
        verdicts = {item.marker: item for item in plan.candidates}
        for marker in (LEGACY_SANDBOX_MARKER, LEGACY_CC_MARKER, LEGACY_CC_TENANT_MARKER):
            assert verdicts[marker].verdict == "blocked"
            assert "归属标签" in "".join(verdicts[marker].blockers)

        with pytest.raises(OrphanCleanupRefused):
            await execute_orphan_rule_cleanup(docker, FAMILIES, OWN_PORTS, LEGACY_SANDBOX_MARKER)
        assert all(argv[0] != "-D" for _family, argv in executor.calls)

    async def test_a_container_without_the_label_at_all_claims_nothing(self) -> None:
        """反向对照：标签**不存在**才是「与本项目无关」，不能一起被算成空作用域"""
        docker = FakeDocker(containers=[container_summary("postgres", labels={})])
        facts = await guard.collect_host_container_facts(docker)
        assert facts.scopes_claimed == {}


class TestMarkerMatchIsCommentValueOnly:
    """盘点侧与删除侧必须用同一条判据，否则预览说 N 条、执行删 N+1 条

    多删的那一条是**别人的**规则，而且没有任何运行时信号——这正是本轮修掉的那个缺陷：
    `build_cleanup_commands` / `build_indexed_cleanup_commands` / `summarize_marked_rules`
    原本判的是「标记出现在任意一个 token 上」。
    """

    DUMP = "\n".join(
        [
            f"-N {INPUT_CHAIN}",
            FOREIGN_LINES[2],
            f'-A {INPUT_CHAIN} -i nekro-sbx0 -m comment --comment "{LEGACY_SANDBOX_MARKER}" -j DROP',
        ],
    )

    def test_text_cleanup_skips_a_rule_that_only_mentions_the_marker(self) -> None:
        commands = guard.build_cleanup_commands(INPUT_CHAIN, self.DUMP, LEGACY_SANDBOX_MARKER)
        assert len(commands) == 1
        assert "--log-prefix" not in commands[0]

    def test_indexed_cleanup_skips_it_too(self) -> None:
        """行号兜底更危险：删错的话连"删的是哪一条"都无从追查"""
        commands = guard.build_indexed_cleanup_commands(INPUT_CHAIN, self.DUMP, LEGACY_SANDBOX_MARKER)
        assert commands == [["-D", INPUT_CHAIN, "2"]]

    def test_readback_summary_skips_it_too(self) -> None:
        """回读自检把它算进来的话，条数比对会永远对不上，退化成每跑一次沙盒重装一次"""
        assert guard.summarize_marked_rules(INPUT_CHAIN, self.DUMP, LEGACY_SANDBOX_MARKER).count == 1

    def test_preview_count_equals_what_execution_deletes(self) -> None:
        """两侧口径一致的可观测形式：盘点数出来的条数 == 生成的删除命令条数"""
        dumps = [
            (family, chain, "\n".join([f"-N {chain}", *lines]))
            for (family, chain), lines in build_chains(DEFAULT_GROUPS).items()
        ]
        inventory = collect_marker_inventory(dumps, PREFIXES)
        for marker, entry in inventory.items():
            planned = sum(
                len(guard.build_cleanup_commands(chain, dump, marker)) for _family, chain, dump in dumps
            )
            assert planned == len(entry.lines), marker


class TestMarkerInventoryShape:
    def test_inventory_entry_carries_its_prefix(self) -> None:
        dumps = [
            (family, chain, "\n".join([f"-N {chain}", *lines]))
            for (family, chain), lines in build_chains(DEFAULT_GROUPS).items()
        ]
        inventory = collect_marker_inventory(dumps, PREFIXES)
        entry: MarkerInventory = inventory[LEGACY_CC_TENANT_MARKER]
        assert entry.prefix == "nekro-cc"
        assert entry.interfaces == ("nkcc-legacyt1",)
