"""CC 工作区网络隔离的规则集回归测试

保护的契约（对应 `services/workspace/network_guard.py`）：

1. **NA -> 工作区必须畅通**：工作区是纯服务端，NA 用容器名寻址访问它。NA 与工作区共用同一个
   网桥，NA 的包同样是 `-i <bridge>`，例外若缺失或排在 DROP 之后，表现就是「容器起来了但
   健康检查永远超时」——这是本特性最需要防的回归；
2. **容器 -> NA 的方向有且只有模型中继那一条例外**：这是与沙盒的结构性差异。开这条口子是为了
   让平台方的模型 Key 不进容器（`services/workspace/cc_credential.py`），代价必须收窄到
   「目的地 = NA 的地址 + 中继端口」；退化成「顺手照抄沙盒的 RPC 例外」或按源地址放行，
   都会白白扩大攻击面。`NEKRO_CC_CREDENTIAL_MODE=direct` 的部署必须逐字回到「一条都没有」；
3. **内网/链路本地必须被切断**，尤其是云元数据服务 `169.254.169.254`，IPv4/IPv6 都要覆盖；
4. **发往宿主自身默认拒绝**，且不留源地址例外——网桥不做源地址校验，留了就等于给伪造源地址
   开了一条通往宿主的路；
5. **标记与沙盒互不重叠**：两套规则并存于同一批链上，清理时只能清自己那一份；
6. **按归属分组的网络彼此不串**：同网桥的容器互访是二层交换、不进 iptables，跨租户隔离
   只能靠「不同租户不同网络」来做，因此分组的网络名/网桥名/规则标记三者都必须互不相同，
   而平台方分组必须保持改造前的名字，否则存量部署的老规则永远清不掉。

真实 iptables 交互不在这里覆盖（单测不调真命令），由 Linux 环境实测负责。
"""

import ipaddress
from dataclasses import replace
from typing import List

import pytest

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv, deployment_scope
from nekro_agent.services.sandbox.network_guard import (
    BLOCKED_IPV4_CIDRS,
    BLOCKED_IPV6_CIDRS,
    INPUT_CHAIN,
    IptablesRule,
    build_cleanup_commands,
    build_install_commands,
    isolation_marker,
)
from nekro_agent.services.workspace import network_guard
from nekro_agent.services.workspace.network_guard import (
    PLATFORM_GROUP,
    WorkspaceNetworkIsolationError,
    WorkspaceNetworkSpec,
    build_workspace_isolation_rules,
    extract_network_ipv4,
    normalize_group_key,
    reset_isolation_state,
    workspace_bridge_name,
    workspace_group_key,
    workspace_isolation_marker,
    workspace_network_name,
)

FORWARD_CHAIN = "DOCKER-USER"
MARKER = "nekro-cc:default"
API_PORT = 7021

SPEC = WorkspaceNetworkSpec(
    bridge_interface="nekro-cc0",
    ipv4_subnet="172.29.0.0/16",
    controller_addresses=("172.29.0.2",),
    api_port=API_PORT,
    marker=MARKER,
)

# NA 跑在宿主上时不接入该网络，规则集里就没有放行例外
HOST_MODE_SPEC = WorkspaceNetworkSpec(
    bridge_interface="nekro-cc0",
    ipv4_subnet="172.29.0.0/16",
    controller_addresses=(),
    api_port=API_PORT,
    marker=MARKER,
)


RELAY_PORT = 8021
GATEWAY = "172.29.0.1"

# 中继开着、NA 在容器里：容器走网桥内部直连 NA 的地址，发往宿主的方向仍然一条例外都没有
RELAY_SPEC = replace(SPEC, relay_port=RELAY_PORT)

# 中继开着、NA 跑在宿主上：容器只能连本网桥的网关，这时 INPUT 上多一条同样收窄的例外
HOST_RELAY_SPEC = replace(HOST_MODE_SPEC, relay_port=RELAY_PORT, host_relay_addresses=(GATEWAY,))


def _chain_rules(rules: List[IptablesRule], chain: str) -> List[IptablesRule]:
    return [rule for rule in rules if rule.chain == chain]


def _index_of(rules: List[IptablesRule], predicate) -> int:
    for index, rule in enumerate(rules):
        if predicate(rule):
            return index
    raise AssertionError("未找到期望的规则")


def _option(rule: IptablesRule, flag: str) -> str:
    """取出规则里某个选项的取值，取不到时返回空串"""
    args = list(rule.args)
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else ""


def _is_drop_to(rule: IptablesRule, cidr: str) -> bool:
    return rule.args[-1] == "DROP" and _option(rule, "-d") == cidr


class TestControllerException:
    """NA -> 工作区容器：本特性最容易把功能打瘫的一环"""

    def test_controller_accept_precedes_every_drop(self) -> None:
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        first_drop = _index_of(rules, lambda rule: rule.args[-1] == "DROP")
        accept = _index_of(rules, lambda rule: _option(rule, "-s") == "172.29.0.2")
        assert accept < first_drop, "NA 的放行例外排在 DROP 之后，等于不生效"

    def test_controller_accept_targets_the_workspace_api_port(self) -> None:
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        rule = rules[_index_of(rules, lambda rule: _option(rule, "-s") == "172.29.0.2")]
        assert _option(rule, "--dport") == str(API_PORT)
        assert _option(rule, "-p") == "tcp"
        assert rule.args[-1] == "ACCEPT"

    def test_controller_accept_is_confined_to_the_workspace_subnet(self) -> None:
        """网桥不做源地址校验，工作区里的代码可以伪造成 NA 的源地址套用这条例外。
        把目的地钉死在本网段上，伪造能换来的最大权限就只剩访问兄弟工作区的 API。"""
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        rule = rules[_index_of(rules, lambda rule: _option(rule, "-s") == "172.29.0.2")]
        assert _option(rule, "-d") == "172.29.0.0/16"

    def test_no_controller_exception_reaches_the_host_chain(self) -> None:
        """发往宿主的链上不留源地址例外：留了就等于给伪造源地址开一条通往宿主的路"""
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        assert all(_option(rule, "-s") != "172.29.0.2" for rule in rules)

    def test_host_mode_emits_no_controller_exception(self) -> None:
        """NA 跑在宿主上时走的是发布到 127.0.0.1 的端口，不经过本守卫匹配的网桥入口"""
        rules = build_workspace_isolation_rules(HOST_MODE_SPEC, "ipv4", FORWARD_CHAIN)
        assert all(rule.args[-1] != "ACCEPT" or _option(rule, "--dport") != str(API_PORT) for rule in rules)

    def test_controller_exception_is_ipv4_only(self) -> None:
        """NA 在该网络上的地址由 Docker IPAM 分配，默认只有 IPv4；混进 IPv6 规则集会装不上"""
        rules = build_workspace_isolation_rules(SPEC, "ipv6", FORWARD_CHAIN)
        assert all("172.29.0.2" not in rule.args for rule in rules)


class TestNoWorkspaceToControllerPath:
    """与沙盒的结构性差异：工作区容器发起方向只开模型中继那一个口子"""

    def test_no_conntrack_original_port_exception(self) -> None:
        """沙盒靠 --ctorigdstport 放行回连 NA 的 DNAT 流量；工作区照抄这条等于白送攻击面"""
        for family in ("ipv4", "ipv6"):
            rules = build_workspace_isolation_rules(RELAY_SPEC, family, FORWARD_CHAIN)  # type: ignore[arg-type]
            assert all("--ctorigdstport" not in rule.args for rule in rules)

    def test_host_chain_accepts_nothing_but_dns_and_return_traffic(self) -> None:
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        accepted_ports = {_option(rule, "--dport") for rule in rules if rule.args[-1] == "ACCEPT"}
        assert accepted_ports <= {"", "53"}, f"发往宿主的链上出现了额外放行: {accepted_ports}"


class TestModelRelayException:
    """容器 -> NA 的模型中继：本规则集里唯一一条容器发起方向的放行

    它是拿「平台方的模型 Key 不进容器」换来的，所以口子的形状就是这笔交易的全部内容：
    开宽一点、或者排错位置，换来的东西还在、付出的代价却变大了，而且没有任何运行时信号。
    """

    def test_direct_mode_is_byte_identical_to_having_no_exception_at_all(self) -> None:
        """关掉中继的部署必须逐字回到「一条容器发起方向的放行都没有」的形态

        `relay_port=0` 是「不开口子」的唯一判据，此时连 `host_relay_addresses`
        都不许生效——否则关掉开关只是让规则少了一条、另一条还在。
        """
        for family in ("ipv4", "ipv6"):
            baseline = build_workspace_isolation_rules(SPEC, family, FORWARD_CHAIN)  # type: ignore[arg-type]
            with_stale_addresses = build_workspace_isolation_rules(
                replace(SPEC, relay_port=0, host_relay_addresses=(GATEWAY,)),
                family,  # type: ignore[arg-type]
                FORWARD_CHAIN,
            )
            assert with_stale_addresses == baseline

    def test_relay_accept_precedes_every_drop(self) -> None:
        """NA 的地址落在本网段里，例外排在 DROP 之后就等于容器打不到中继 —— CC 全线失灵"""
        rules = _chain_rules(build_workspace_isolation_rules(RELAY_SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        first_drop = _index_of(rules, lambda rule: rule.args[-1] == "DROP")
        relay = _index_of(rules, lambda rule: _option(rule, "--dport") == str(RELAY_PORT))
        assert relay < first_drop

    def test_relay_accept_matches_the_destination_not_the_source(self) -> None:
        """网桥不做源地址校验：按源放行的例外可以被容器伪造，按目的地就伪造不出额外权限"""
        rules = _chain_rules(build_workspace_isolation_rules(RELAY_SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        rule = rules[_index_of(rules, lambda rule: _option(rule, "--dport") == str(RELAY_PORT))]
        assert _option(rule, "-d") == "172.29.0.2"
        assert _option(rule, "-s") == "172.29.0.0/16"
        assert _option(rule, "-p") == "tcp"
        assert rule.args[-1] == "ACCEPT"

    def test_relay_opens_exactly_one_container_initiated_port(self) -> None:
        """开的是「一个端口」而不是「NA 这台机器」：多出来的任何一个端口都是白送的攻击面"""
        rules = _chain_rules(build_workspace_isolation_rules(RELAY_SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        container_initiated = [
            _option(rule, "--dport")
            for rule in rules
            if rule.args[-1] == "ACCEPT" and _option(rule, "-s") == "172.29.0.0/16" and _option(rule, "--dport")
        ]
        assert sorted(container_initiated) == sorted(["53", "53", str(RELAY_PORT)])

    def test_relay_in_docker_leaves_the_host_chain_untouched(self) -> None:
        """NA 在容器里时中继走网桥内部，发往宿主的方向必须仍然一条例外都没有"""
        rules = _chain_rules(build_workspace_isolation_rules(RELAY_SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        accepted_ports = {_option(rule, "--dport") for rule in rules if rule.args[-1] == "ACCEPT"}
        assert accepted_ports <= {"", "53"}, f"发往宿主的链上出现了额外放行: {accepted_ports}"

    def test_host_mode_relay_opens_one_narrow_input_exception(self) -> None:
        """NA 跑在宿主上时中继只能走网关，这条例外缺了 CC 会直接失联"""
        rules = _chain_rules(build_workspace_isolation_rules(HOST_RELAY_SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        exceptions = [rule for rule in rules if rule.args[-1] == "ACCEPT" and _option(rule, "--dport") == str(RELAY_PORT)]
        assert len(exceptions) == 1
        assert _option(exceptions[0], "-d") == GATEWAY
        assert _option(exceptions[0], "-p") == "tcp"
        assert _index_of(rules, lambda rule: rule is exceptions[0]) < _index_of(
            rules,
            lambda rule: rule.args[-1] == "DROP",
        )

    def test_host_relay_exception_is_ipv4_only(self) -> None:
        """网关地址来自 Docker IPAM 的 IPv4 配置，混进 IPv6 规则集会整批装不上"""
        rules = build_workspace_isolation_rules(HOST_RELAY_SPEC, "ipv6", FORWARD_CHAIN)
        assert all(GATEWAY not in rule.args for rule in rules)


class TestForwardChainRules:
    """转发方向：黑名单切断内网，公网保持可达"""

    @pytest.mark.parametrize("cidr", BLOCKED_IPV4_CIDRS)
    def test_blocks_every_ipv4_internal_range(self, cidr: str) -> None:
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        assert any(_is_drop_to(rule, cidr) for rule in rules), f"{cidr} 未被拦截"

    @pytest.mark.parametrize("cidr", BLOCKED_IPV6_CIDRS)
    def test_blocks_every_ipv6_internal_range(self, cidr: str) -> None:
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv6", FORWARD_CHAIN), FORWARD_CHAIN)
        assert any(_is_drop_to(rule, cidr) for rule in rules), f"{cidr} 未被拦截"

    def test_cloud_metadata_range_is_blocked(self) -> None:
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        assert any(_is_drop_to(rule, "169.254.0.0/16") for rule in rules)

    def test_workspace_subnet_itself_is_covered_by_a_drop(self) -> None:
        """工作区网段自身也必须落在某条 DROP 里：这既挡住「工作区 -> NA 的其他业务网络」，
        也是「工作区 A -> 工作区 B」唯一的拦截点。

        注意后者只在宿主启用了 br_netfilter 时才真正生效——同网桥容器互访是二层交换的，
        默认不进 iptables（已实测确认）。这里断言的是规则覆盖到位，不是断言该路径一定被拦。"""
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        drop_cidrs = [_option(rule, "-d") for rule in rules if rule.args[-1] == "DROP"]
        assert any(
            ipaddress.ip_network(SPEC.ipv4_subnet or "").subnet_of(ipaddress.ip_network(cidr))
            for cidr in drop_cidrs
        ), f"工作区网段 {SPEC.ipv4_subnet} 未被任何 DROP 覆盖: {drop_cidrs}"

    def test_dns_accept_precedes_every_drop(self) -> None:
        """AWS/GCP 的解析器坐在被封网段里，不放行 53 会让 git clone / pip install 全部不可用"""
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        first_drop = _index_of(rules, lambda rule: rule.args[-1] == "DROP")
        for protocol in ("udp", "tcp"):
            index = _index_of(
                rules,
                lambda rule, protocol=protocol: (
                    rule.args[-1] == "ACCEPT" and _option(rule, "-p") == protocol and _option(rule, "--dport") == "53"
                ),
            )
            assert index < first_drop

    def test_public_destinations_are_not_matched(self) -> None:
        """公网出网靠「不匹配任何规则」实现，转发链上不能出现无目的地约束的规则"""
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        for rule in rules:
            assert any(
                _option(rule, flag) for flag in ("-d", "--dport", "--ctstate")
            ), f"转发链出现无条件规则，会波及公网出网: {rule.args}"

    def test_every_rule_is_scoped_to_the_workspace_bridge(self) -> None:
        for family in ("ipv4", "ipv6"):
            for rule in build_workspace_isolation_rules(SPEC, family, FORWARD_CHAIN):  # type: ignore[arg-type]
                assert rule.args[:2] == ("-i", SPEC.bridge_interface), f"规则未限定工作区网桥: {rule.args}"

    def test_guarded_rules_are_scoped_to_the_workspace_subnet(self) -> None:
        """例外之外的规则都按源网段收窄，避免网桥名被别的网络复用时误伤"""
        rules = build_workspace_isolation_rules(HOST_MODE_SPEC, "ipv4", FORWARD_CHAIN)
        for rule in rules:
            assert _option(rule, "-s") == "172.29.0.0/16"

    def test_missing_subnet_falls_back_to_interface_scope(self) -> None:
        spec = WorkspaceNetworkSpec(
            bridge_interface="nekro-cc0",
            ipv4_subnet=None,
            controller_addresses=("172.29.0.2",),
            api_port=API_PORT,
            marker=MARKER,
        )
        rules = build_workspace_isolation_rules(spec, "ipv4", FORWARD_CHAIN)
        for rule in rules:
            assert rule.args[:2] == ("-i", "nekro-cc0")
            assert _option(rule, "-s") in ("", "172.29.0.2")


class TestHostInputRules:
    """发往宿主自身：默认拒绝，只留 DNS 与回包"""

    def test_defaults_to_deny(self) -> None:
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        catch_all = rules[-1]
        assert catch_all.args[-1] == "DROP"
        assert "-d" not in catch_all.args, "发往宿主的兜底拒绝不能限定目的地址：宿主地址可能是公网 IP"

    def test_ipv6_allows_icmpv6_before_catch_all_drop(self) -> None:
        """丢掉 NDP 会让容器连网关都找不到，公网 IPv6 出网会一并失效"""
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv6", FORWARD_CHAIN), INPUT_CHAIN)
        drop_index = _index_of(rules, lambda rule: rule.args[-1] == "DROP")
        icmp_index = _index_of(rules, lambda rule: "ipv6-icmp" in rule.args)
        assert icmp_index < drop_index

    def test_established_return_traffic_is_accepted_first(self) -> None:
        rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        assert "ESTABLISHED,RELATED" in rules[0].args
        assert rules[0].args[-1] == "ACCEPT"


class TestInstallAndCleanup:
    def test_insert_indexes_preserve_declared_order_per_chain(self) -> None:
        rules = build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN)
        commands = build_install_commands(rules)
        assert len(commands) == len(rules)

        seen: dict = {}
        for command, rule in zip(commands, rules):
            assert command[0] == "-I"
            assert command[1] == rule.chain
            expected = seen.get(rule.chain, 0) + 1
            seen[rule.chain] = expected
            assert command[2] == str(expected)
            assert command[3:] == list(rule.args)

    def test_every_rule_carries_the_marker(self) -> None:
        for family in ("ipv4", "ipv6"):
            for rule in build_workspace_isolation_rules(SPEC, family, FORWARD_CHAIN):  # type: ignore[arg-type]
                assert "--comment" in rule.args
                assert MARKER in rule.args

    def test_deletion_round_trips_installed_rules(self) -> None:
        """安装出来的规则必须能被清理逻辑原样删掉，否则重启就会堆积"""
        forward_rules = _chain_rules(build_workspace_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        save_output = "\n".join(f"-A {FORWARD_CHAIN} " + " ".join(rule.args) for rule in forward_rules)
        commands = build_cleanup_commands(FORWARD_CHAIN, save_output, MARKER)
        assert [command[2:] for command in commands] == [list(rule.args) for rule in forward_rules]

    def test_cleanup_leaves_sandbox_rules_untouched(self) -> None:
        """两套规则并存于同一批链上，清理只能清自己那一份"""
        save_output = "\n".join(
            [
                f"-A {FORWARD_CHAIN} -i nekro-cc0 -s 172.29.0.0/16 -d 10.0.0.0/8 "
                '-m comment --comment "nekro-cc:default" -j DROP',
                f"-A {FORWARD_CHAIN} -i nekro-sbx0 -s 172.28.0.0/16 -d 10.0.0.0/8 "
                '-m comment --comment "nekro-sandbox:default" -j DROP',
            ],
        )
        commands = build_cleanup_commands(FORWARD_CHAIN, save_output, MARKER)
        assert len(commands) == 1
        assert "nekro-sandbox:default" not in commands[0]


class TestNetworkGrouping:
    """按归属分组：同网桥内的容器互访是二层交换、不进 iptables，跨租户隔离只能靠拓扑来做

    实测（Docker 29，未加载 br_netfilter）：同网桥兄弟容器互通且覆盖该目的地的 DROP 计数器
    恒为 0；换成两张网络后互访被 Docker 自己在 `table ip raw / PREROUTING` 下的
    `iifname != <网桥> ip daddr <容器> drop` 拦住，计数器实打实地涨。
    """

    @pytest.mark.parametrize(
        ("owners", "expected"),
        [
            ((), ""),
            ((None,), ""),
            ((None, None), ""),
            ((3,), "t3"),
            ((3, 3), "t3"),
            ((7, 7, 7), "t7"),
            ((None, 3), "shared"),
            ((3, 4), "shared"),
            ((None, 3, 4), "shared"),
        ],
    )
    def test_group_key_follows_channel_ownership(self, owners: tuple, expected: str) -> None:
        assert workspace_group_key(owners) == expected

    def test_mixed_ownership_never_lands_in_either_side(self) -> None:
        """既服务平台方频道又服务某租户频道的工作区，两边的数据都在它里面：
        塞进任何一侧的分组都等于替用户扩大了那一侧的可见范围"""
        assert workspace_group_key((None, 3)) not in (PLATFORM_GROUP, "t3")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", ""),
            ("t3", "t3"),
            ("shared", "shared"),
            ("  T3  ", "t3"),
            ('bad"name; rm', "badnamerm"),
            ("t" + "9" * 40, "t" + "9" * 15),
        ],
    )
    def test_group_key_is_sanitised_for_names_and_comments(self, raw: str, expected: str) -> None:
        """分组键会进 Docker 网络名与 iptables --comment，越界字符会让创建或安装直接失败"""
        assert normalize_group_key(raw) == expected

    def test_platform_group_keeps_the_pre_grouping_names(self) -> None:
        """存量部署升级后必须继续命中同一张网络、同一个标记，
        否则老规则永远清不掉、老网络变成孤儿"""
        for instance in ("", "inst1-"):
            assert workspace_network_name(instance, PLATFORM_GROUP) == workspace_network_name(instance)
            assert workspace_bridge_name(instance, PLATFORM_GROUP) == workspace_bridge_name(instance)
            assert workspace_isolation_marker(instance, PLATFORM_GROUP) == workspace_isolation_marker(instance)
        assert workspace_network_name("") == "nekro_cc_net"
        assert workspace_bridge_name("") == "nekro-cc0"
        assert workspace_isolation_marker("") == "nekro-cc:default"

    def test_each_group_gets_its_own_network_bridge_and_marker(self) -> None:
        groups = [PLATFORM_GROUP, "t3", "t4", "shared"]
        for instance in ("", "inst1-"):
            names = [workspace_network_name(instance, group) for group in groups]
            bridges = [workspace_bridge_name(instance, group) for group in groups]
            markers = [workspace_isolation_marker(instance, group) for group in groups]
            assert len(set(names)) == len(groups)
            assert len(set(bridges)) == len(groups)
            assert len(set(markers)) == len(groups)

    def test_group_names_stay_distinct_across_instances(self) -> None:
        """同宿主多实例部署里，两个实例的同名分组不能撞到同一张网络上"""
        assert workspace_network_name("inst1-", "t3") != workspace_network_name("inst2-", "t3")
        assert workspace_bridge_name("inst1-", "t3") != workspace_bridge_name("inst2-", "t3")
        assert workspace_isolation_marker("inst1-", "t3") != workspace_isolation_marker("inst2-", "t3")

    def test_group_bridge_name_fits_kernel_interface_limit(self) -> None:
        for instance in ("", "inst1-", "a" * 64):
            for group in (PLATFORM_GROUP, "t3", "shared", "t" + "9" * 40):
                assert len(workspace_bridge_name(instance, group)) < 16

    def test_group_network_name_does_not_look_like_the_business_network(self) -> None:
        for instance in ("", "inst1-"):
            for group in (PLATFORM_GROUP, "t3", "shared"):
                assert not workspace_network_name(instance, group).endswith("nekro_network")

    def test_cleanup_of_one_group_leaves_the_others_alone(self) -> None:
        """装规则前会按标记清同标记旧规则。标记不区分分组的话，
        起一个租户的工作区就会把别的租户的规则全部删掉——隔离静默消失"""
        tenant_marker = workspace_isolation_marker("", "t3")
        other_marker = workspace_isolation_marker("", "t4")
        save_output = "\n".join(
            [
                f'-A {FORWARD_CHAIN} -i nkcc-a -d 10.0.0.0/8 -m comment --comment "{tenant_marker}" -j DROP',
                f'-A {FORWARD_CHAIN} -i nkcc-b -d 10.0.0.0/8 -m comment --comment "{other_marker}" -j DROP',
                f"-A {FORWARD_CHAIN} -i nekro-cc0 -d 10.0.0.0/8 "
                '-m comment --comment "nekro-cc:default" -j DROP',
                f"-A {FORWARD_CHAIN} -i nekro-sbx0 -d 10.0.0.0/8 "
                '-m comment --comment "nekro-sandbox:default" -j DROP',
            ],
        )
        commands = build_cleanup_commands(FORWARD_CHAIN, save_output, tenant_marker)
        assert len(commands) == 1
        assert tenant_marker in commands[0]
        for survivor in (other_marker, "nekro-cc:default", "nekro-sandbox:default"):
            assert survivor not in commands[0]

    def test_group_marker_never_collides_with_the_sandbox_guard(self) -> None:
        for instance in ("", "inst1-"):
            for group in (PLATFORM_GROUP, "t3", "shared"):
                assert workspace_isolation_marker(instance, group) != isolation_marker(instance)


class TestNamingIsolation:
    def test_marker_never_collides_with_the_sandbox_guard(self) -> None:
        for instance in ("", "inst1-", "inst2-"):
            assert workspace_isolation_marker(instance) != isolation_marker(instance)
        assert workspace_isolation_marker("") == "nekro-cc:default"
        assert workspace_isolation_marker("inst1-") == "nekro-cc:inst1-"

    def test_names_are_instance_scoped(self) -> None:
        assert workspace_network_name("") == "nekro_cc_net"
        assert workspace_network_name("inst1-") == "inst1-nekro_cc_net"
        assert workspace_bridge_name("inst1-") != workspace_bridge_name("inst2-")

    def test_network_name_does_not_look_like_the_business_network(self) -> None:
        """container.py / console/container_manager.py 用 endswith('nekro_network') 认 NA 的业务网络，
        撞上这个后缀会让自动探测随机认到工作区网络上"""
        for instance in ("", "inst1-"):
            assert not workspace_network_name(instance).endswith("nekro_network")

    def test_bridge_name_fits_kernel_interface_limit(self) -> None:
        for instance in ("", "inst1-", "a" * 64):
            assert len(workspace_bridge_name(instance)) < 16


class TestControllerAddressExtraction:
    NETWORK = "nekro_cc_net"

    def test_reads_the_address_on_the_requested_network(self) -> None:
        info = {
            "NetworkSettings": {
                "Networks": {
                    "nekro_network": {"IPAddress": "172.18.0.5"},
                    self.NETWORK: {"IPAddress": "172.29.0.2"},
                },
            },
        }
        assert extract_network_ipv4(info, self.NETWORK) == "172.29.0.2"

    @pytest.mark.parametrize(
        "info",
        [
            {},
            {"NetworkSettings": {}},
            {"NetworkSettings": {"Networks": {}}},
            {"NetworkSettings": {"Networks": {"nekro_network": {"IPAddress": "172.18.0.5"}}}},
            {"NetworkSettings": {"Networks": {NETWORK: {"IPAddress": ""}}}},
        ],
    )
    def test_absent_attachment_yields_empty(self, info: dict) -> None:
        assert extract_network_ipv4(info, self.NETWORK) == ""


class TestRelayWithoutAnyAllowedAddressIsRefused:
    """中继开着却一条放行都没有 —— 拒绝生成

    与沙盒守卫拒绝「只有兜底拒绝、没有 RPC 回调放行」是同一条不变式。这一组的两端都是
    默认拒绝，缺了放行就是一套**保证**打瘫工作区的规则，而且瘫法和那次生产事故一模一样：
    DROP 不回 RST，容器里那次请求一直等到自己超时，宿主侧一条错误日志都没有。

    可达路径：NA 跑在宿主上时中继只能经工作区网络的网关回连，而网关地址读不出来时
    `host_relay_addresses` 就是空元组，`controller_addresses` 在这种部署下本来也是空的。
    """

    BROKEN = replace(HOST_MODE_SPEC, relay_port=RELAY_PORT, host_relay_addresses=())

    @pytest.mark.parametrize("family", ["ipv4", "ipv6"])
    def test_refuses_to_build(self, family: str) -> None:
        with pytest.raises(WorkspaceNetworkIsolationError):
            build_workspace_isolation_rules(self.BROKEN, family, FORWARD_CHAIN)  # type: ignore[arg-type]

    def test_error_names_what_to_check(self) -> None:
        with pytest.raises(WorkspaceNetworkIsolationError, match="网关"):
            build_workspace_isolation_rules(self.BROKEN, "ipv4", FORWARD_CHAIN)

    def test_relay_disabled_is_still_fine_without_addresses(self) -> None:
        """关掉中继的部署本来就一条容器发起方向的放行都没有，那是正确形态，不该被这条挡住"""
        assert build_workspace_isolation_rules(HOST_MODE_SPEC, "ipv4", FORWARD_CHAIN)

    @pytest.mark.parametrize("spec_name", ["RELAY_SPEC", "HOST_RELAY_SPEC"])
    def test_both_real_relay_shapes_still_build(self, spec_name: str) -> None:
        """NA 在容器里（走 controller 地址）和 NA 在宿主上（走网关）两种形态都不受影响"""
        assert build_workspace_isolation_rules(globals()[spec_name], "ipv4", FORWARD_CHAIN)


# =============================================================================
# 规则集**输入**的推导（`prepare_workspace_network`），而不是规则集本身
# =============================================================================


class _FakeWorkspaceNetwork:
    def __init__(self, detail: dict) -> None:
        self._detail = detail

    async def show(self) -> dict:
        return self._detail


class _FakeWorkspaceNetworks:
    """按**被测代码当下会算出的**网络名建 fake（§2.9：写死默认环境下的字面量 = 换台机器就脱靶）"""

    def __init__(self, subnets: dict) -> None:
        self._subnets = subnets  # group -> subnet
        self.created: List[str] = []

    async def get(self, name: str) -> _FakeWorkspaceNetwork:
        instance = deployment_scope()
        for group, subnet in self._subnets.items():
            if workspace_network_name(instance, group) == name:
                return _FakeWorkspaceNetwork(
                    {
                        "Id": "b" * 64,
                        "Options": {"com.docker.network.bridge.name": workspace_bridge_name(instance, group)},
                        "IPAM": {"Config": [{"Subnet": subnet, "Gateway": _gateway_of(subnet)}]},
                    },
                )
        raise RuntimeError(f"network {name} not found")

    async def create(self, config: dict) -> None:
        # 先记账再抛：`best_effort` 会把这里抛出的任何异常当成「隔离没装上」吞掉并回落，
        # 于是「fake 脱靶」与「真的装不上」在结果上无法区分（§2.9 ①）
        self.created.append(str(config.get("Name", "")))
        raise AssertionError("单测里不该真的去建 Docker 网络")


class _FakeWorkspaceDocker:
    def __init__(self, subnets: dict) -> None:
        self.networks = _FakeWorkspaceNetworks(subnets)


class _FakeSelfContainer:
    """`RUN_IN_DOCKER=True` 时 `_attach_controller` 认出的「NA 自己」

    inspect 结果里报告自己已经接在**每一个**工作区网络上，各有一个 IPv4 ——
    于是 `_attach_controller` 走「已接入」那条早返回，不会去调 `networks.connect`。
    """

    id = "c" * 64

    def __init__(self, subnets: dict) -> None:
        instance = deployment_scope()
        self._networks = {
            workspace_network_name(instance, group): {"IPAddress": _controller_of(subnet)}
            for group, subnet in subnets.items()
        }

    async def show(self) -> dict:
        return {"NetworkSettings": {"Networks": dict(self._networks)}}


def _gateway_of(subnet: str) -> str:
    return str(ipaddress.ip_network(subnet).network_address + 1)


def _controller_of(subnet: str) -> str:
    """NA 自己在该网络上的地址：与网关不同，否则「用了网关地址」这类错位测不出来"""
    return str(ipaddress.ip_network(subnet).network_address + 2)


async def _resolved(value: object) -> object:
    return value


# 两种部署形态下的中继端口必须取不同值，否则「把这两个调换过来」在规则里毫无差别
HOST_RELAY_PORT = RELAY_PORT  # NA 跑在宿主上：compose 发布到宿主的那个端口
LISTEN_RELAY_PORT = 8031  # NA 在容器里：进程自己 bind 的那个端口


# 一张网络一个分组，子网各不相同 —— 现场那份规则里平台组是 172.20/16、租户组是另一段，
# 分组之间共用同一个子网的话「按分组各算各的网关」这条就测不出来了
_GROUP_SUBNETS = {
    PLATFORM_GROUP: "172.20.0.0/16",
    "t1": "172.21.0.0/16",
    "t7": "172.24.0.0/16",
    "shared": "172.25.0.0/16",
}


class TestRelayExceptionIsDerivedTheSameWayForEveryGroup:
    """租户分组与平台分组必须推出**结构相同**的规则集

    这一组存在的直接理由：测试服务器上曾出现平台组网桥带着
    `-d <网关> --dport 8021 ACCEPT`、而租户组网桥只有 `ESTABLISHED + DNS + DROP` 的现场，
    形态与 §2.6 那次「装了兜底 DROP 却没装对应放行」的生产事故一模一样。
    实测结论是虚惊——两份规则由相隔约 36 小时的两个代码版本装上，租户那份是中继特性
    落地**之前**装的、之后再没有工作区落到该分组所以从未重装。

    但排查过程暴露了一个真问题：`build_workspace_isolation_rules` 有 60+ 条用例，
    而**喂给它的 `WorkspaceNetworkSpec` 是怎么算出来的**一条都没有
    （`prepare_workspace_network` 此前零覆盖）。于是「只给平台组带上中继放行」这种变异
    在整份测试里完全不可见 —— 正是 §2.7「零件全对、调用点没了，测试会全绿」。

    所以这里驱动的是 `prepare_workspace_network` 本身，断言的是它**实际交给安装函数**的
    那套规则：逐分组取出、把分组相关的字面量（网桥/子网/网关/标记）归一化之后必须逐条相等。
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        reset_isolation_state()
        yield
        reset_isolation_state()

    @pytest.fixture
    def installed(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        """拦下安装动作，记录每个分组真正会被装上的 IPv4 规则

        仍然调用真正的 `build`（`partial(build_workspace_isolation_rules, spec)`），
        所以「中继开着却没有放行地址就拒绝生成」那条不变式照常参与。

        两个端口**给不同的取值**：`prepare_workspace_network` 要按 `RUN_IN_DOCKER` 在
        `relay_listen_port()`（NA 在容器里，走网桥内部直连）与 `relay_host_port()`
        （NA 在宿主上，走发布到宿主的那个）之间二选一。给成同一个值的话，把这两个
        调换过来的变异在规则里看不出任何差别——而选错端口的后果是容器的中继包全落进
        兜底 DROP，每一次 LLM 调用静默等到超时。
        """
        recorded: dict = {}

        async def _install(executor, marker: str, build) -> None:  # noqa: ANN001
            recorded[marker] = list(build("ipv4", FORWARD_CHAIN))

        async def _executor(docker) -> object:  # noqa: ANN001
            # 不打真 iptables：`resolve_executor` 会去探本机的 iptables 并可能动宿主规则
            return object()

        monkeypatch.setattr(network_guard, "install_marked_rules", _install)
        monkeypatch.setattr(network_guard, "resolve_executor", _executor)
        monkeypatch.setattr(network_guard, "mark_rules_verified", lambda key: None)
        monkeypatch.setattr(network_guard, "schedule_rule_verification", lambda key, verify: None)
        monkeypatch.setattr(config, "CC_WORKSPACE_NETWORK_ISOLATION", "strict")
        monkeypatch.setattr(config, "CC_SANDBOX_INTERNAL_PORT", API_PORT)
        monkeypatch.setattr(config, "NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS", [])
        monkeypatch.setattr(network_guard, "relay_host_port", lambda: HOST_RELAY_PORT)
        monkeypatch.setattr(network_guard, "relay_listen_port", lambda: LISTEN_RELAY_PORT)
        return recorded

    @staticmethod
    def _normalize(rules: List[IptablesRule], group: str) -> List[tuple]:
        """把分组相关的字面量换成占位符，剩下的必须逐条相等"""
        instance = deployment_scope()
        subnet = _GROUP_SUBNETS[group]
        substitutions = {
            workspace_bridge_name(instance, group): "<bridge>",
            workspace_isolation_marker(instance, group): "<marker>",
            subnet: "<subnet>",
            _gateway_of(subnet): "<gateway>",
            _controller_of(subnet): "<controller>",
        }
        return [
            (rule.chain, tuple(substitutions.get(arg, arg) for arg in rule.args))
            for rule in rules
        ]

    async def _install_all(
        self,
        installed: dict,
        monkeypatch: pytest.MonkeyPatch,
        *,
        relaying: bool,
        in_docker: bool = False,
    ) -> dict:
        monkeypatch.setattr(network_guard, "credential_mode", lambda: "relay" if relaying else "direct")
        monkeypatch.setattr(OsEnv, "RUN_IN_DOCKER", in_docker)
        self_container = _FakeSelfContainer(_GROUP_SUBNETS)
        monkeypatch.setattr(network_guard, "resolve_self_container", lambda docker: _resolved(self_container))
        docker = _FakeWorkspaceDocker(_GROUP_SUBNETS)
        per_group = {}
        for group in _GROUP_SUBNETS:
            plan = await network_guard.prepare_workspace_network(docker, group)  # type: ignore[arg-type]
            assert plan.isolated, f"分组 {group!r} 的隔离没装上，后面的断言测的就不是它声称的那件事"
            per_group[group] = installed[workspace_isolation_marker(deployment_scope(), group)]
        assert docker.networks.created == [], "fake 脱靶：被测代码算出的网络名与 fake 里的对不上"
        return per_group

    @pytest.mark.parametrize(
        ("in_docker", "expected_port", "expected_target"),
        [
            # 裸机部署（测试服务器与 #33 现场就是这一种）：中继只能经本网桥的网关回连，
            # 端口是宿主上监听的那个，且必须额外放行 INPUT 的一条
            (False, HOST_RELAY_PORT, "gateway"),
            # compose 部署：NA 自己也在网桥上，容器直连它的地址，端口是 NA 进程 bind 的那个，
            # **发往宿主的方向一个口子都不能开**（模块 docstring 第 4 条）
            (True, LISTEN_RELAY_PORT, "controller"),
        ],
    )
    async def test_every_group_gets_exactly_one_relay_accept_to_its_own_network(
        self,
        installed: dict,
        monkeypatch: pytest.MonkeyPatch,
        in_docker: bool,
        expected_port: int,
        expected_target: str,
    ) -> None:
        """现场那条疑点的直接判据：租户组也必须有中继放行，且指向**自己那张网**的落点

        两种部署形态各跑一遍：端口取值与放行落点都随形态变，只测其中一种等于把另一半
        （以及「两个端口取反」这类错位）整个放过去。
        """
        per_group = await self._install_all(installed, monkeypatch, relaying=True, in_docker=in_docker)
        for group, rules in per_group.items():
            subnet = _GROUP_SUBNETS[group]
            address = _gateway_of(subnet) if expected_target == "gateway" else _controller_of(subnet)
            accepts = [
                rule
                for rule in rules
                if rule.args[-1] == "ACCEPT" and _option(rule, "--dport") == str(expected_port)
            ]
            assert len(accepts) == 1, (
                f"分组 {group!r}（RUN_IN_DOCKER={in_docker}）指向 {expected_port} 的中继放行有 {len(accepts)} 条"
            )
            relay = accepts[0]
            # 规则里存的是裸地址，`iptables -S` 回显时才补成 /32
            assert _option(relay, "-d") == address, (
                f"分组 {group!r} 的中继放行指向 {_option(relay, '-d')!r}，不是它自己那张网的 {expected_target} {address}"
            )
            # 落在哪条链上也是判据：裸机那条是「发往宿主」的唯一例外（INPUT），
            # 而 NA 在容器里时这个方向必须一条都没有
            assert relay.chain == (INPUT_CHAIN if expected_target == "gateway" else FORWARD_CHAIN)
            host_bound = [rule for rule in _chain_rules(rules, INPUT_CHAIN) if _option(rule, "-d")]
            assert len(host_bound) == (1 if expected_target == "gateway" else 0), (
                f"分组 {group!r}（RUN_IN_DOCKER={in_docker}）发往宿主方向的例外有 {len(host_bound)} 条"
            )
            # 放行必须排在同链的兜底 DROP 之前，否则等于没放行
            same_chain = _chain_rules(rules, relay.chain)
            assert same_chain.index(relay) < _index_of(same_chain, lambda rule: rule.args[-1] == "DROP")

            # 另一种形态的端口不该同时出现——「两个都发」也能让上面那条绿
            other_port = LISTEN_RELAY_PORT if expected_port == HOST_RELAY_PORT else HOST_RELAY_PORT
            assert not [rule for rule in rules if _option(rule, "--dport") == str(other_port)], (
                f"分组 {group!r} 同时带上了另一种部署形态的端口 {other_port}"
            )

    @pytest.mark.parametrize("in_docker", [False, True])
    async def test_group_rule_sets_are_identical_once_names_are_normalized(
        self,
        installed: dict,
        monkeypatch: pytest.MonkeyPatch,
        in_docker: bool,
    ) -> None:
        """整套规则逐条相等，而不只是「中继那条也有」

        只盯中继那一条的话，「租户组少了 DNS 放行」「租户组多了一条 DROP」这类同源缺陷
        照样漏网 —— 现场那份规则的问题本来就是「少了一条」，而不是「某条写错了」。
        """
        per_group = await self._install_all(installed, monkeypatch, relaying=True, in_docker=in_docker)
        baseline = self._normalize(per_group[PLATFORM_GROUP], PLATFORM_GROUP)
        for group, rules in per_group.items():
            assert self._normalize(rules, group) == baseline, f"分组 {group!r} 的规则集与平台组不同构"

    @pytest.mark.parametrize("in_docker", [False, True])
    async def test_direct_mode_removes_the_relay_accept_from_every_group_alike(
        self,
        installed: dict,
        monkeypatch: pytest.MonkeyPatch,
        in_docker: bool,
    ) -> None:
        """对照组：证明上面两条测得出差异，而不是「无论如何都成立」

        没有这一条的话，把中继放行写成无条件生成（连 `direct` 都发）同样能让前两条全绿。
        `direct` 是「改造前的行为逐字不变」那一档，两种部署形态都必须回到「一条都没有」。
        """
        per_group = await self._install_all(installed, monkeypatch, relaying=False, in_docker=in_docker)
        for group, rules in per_group.items():
            assert not [
                rule
                for rule in rules
                if _option(rule, "--dport") in (str(HOST_RELAY_PORT), str(LISTEN_RELAY_PORT))
            ], f"`direct` 模式下分组 {group!r} 仍然带着中继放行"
            assert not [
                rule for rule in _chain_rules(rules, INPUT_CHAIN) if _option(rule, "-d")
            ], f"`direct` 模式下分组 {group!r} 仍然留着发往宿主的例外"
        baseline = self._normalize(per_group[PLATFORM_GROUP], PLATFORM_GROUP)
        for group, rules in per_group.items():
            assert self._normalize(rules, group) == baseline, f"分组 {group!r} 的规则集与平台组不同构"
