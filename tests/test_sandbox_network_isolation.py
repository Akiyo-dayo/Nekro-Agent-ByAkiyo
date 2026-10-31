"""沙盒网络隔离的规则集回归测试

保护的契约（对应 `services/sandbox/network_guard.py`）：

1. **例外必须排在拦截之前**：NA 自身的 RPC 端点、DNS 若排在 DROP 之后就等于不存在，
   沙盒会被自己的防线打瘫——这是本特性最需要防的回归；
2. **内网/链路本地必须被切断**，尤其是云元数据服务 `169.254.169.254`，
   且 IPv6 与 IPv4 必须同时覆盖，只做一半等于留了一扇没关的门；
3. **发往宿主自身的流量是默认拒绝**，不是黑名单——黑名单要穷举宿主的全部本机地址，漏一个就整段失效；
4. **规则安装幂等**：清理按固定标记做，NA 重启不得让规则在链上堆积，
   也不得误删同宿主上另一个 NA 实例的规则；
5. **运维声明的额外目的地不能反过来打瘫沙盒**：它补的是「宿主公网 IP 经云网关发夹回来」这条
   守卫覆盖不到的路径，而那个地址上常常正挂着 NA 的回调端点，排序错了就是沙盒功能整个瘫痪；
6. **绝不生成「只有兜底拒绝、没有 RPC 放行」的规则集**：那是一套保证把沙盒打瘫的规则，
   且瘫得毫无线索（DROP 不回 RST，容器一路等到运行超时，NA 侧零错误日志）；
7. **「装过」不等于「还在」**：标记是宿主全局的，共用同一标记的进程会先按标记全删再装它自己
   那套，所以内存里的安装状态必须能被内核里的实际规则推翻（`verify_marked_rules`）。
   这一条是一次真实生产事故的直接产物，事故形态见 `network_guard` 模块 docstring。

真实 iptables 交互不在这里覆盖（单测不调真命令），由 Linux 环境实测负责。
"""

import shlex
from typing import Dict, List, Sequence, Tuple

import pytest

from nekro_agent.services.sandbox.network_guard import (
    BLOCKED_IPV4_CIDRS,
    BLOCKED_IPV6_CIDRS,
    INPUT_CHAIN,
    AddressFamily,
    IptablesRule,
    MarkedRuleSummary,
    SandboxNetworkIsolationError,
    SandboxNetworkSpec,
    build_cleanup_commands,
    build_indexed_cleanup_commands,
    build_install_commands,
    build_isolation_rules,
    diff_marked_rules,
    isolation_marker,
    parse_self_container_ids,
    resolve_rpc_endpoints,
    sandbox_bridge_name,
    sandbox_network_name,
    split_extra_blocked_cidrs,
    summarize_expected_rules,
    summarize_marked_rules,
    verify_marked_rules,
)

FORWARD_CHAIN = "DOCKER-USER"
MARKER = "nekro-sandbox:default"

SPEC = SandboxNetworkSpec(
    bridge_interface="nekro-sbx0",
    ipv4_subnet="172.28.0.0/16",
    rpc_endpoints=(("172.28.0.1", 8021), ("172.17.0.1", 8021)),
    marker=MARKER,
)


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


def _is_accept_on_port(rule: IptablesRule, protocol: str, port: str) -> bool:
    return rule.args[-1] == "ACCEPT" and _option(rule, "-p") == protocol and _option(rule, "--dport") == port


class TestForwardChainRules:
    """转发方向：黑名单切断内网，公网保持可达"""

    @pytest.mark.parametrize("cidr", BLOCKED_IPV4_CIDRS)
    def test_blocks_every_ipv4_internal_range(self, cidr: str) -> None:
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        assert any(_is_drop_to(rule, cidr) for rule in rules), f"{cidr} 未被拦截"

    @pytest.mark.parametrize("cidr", BLOCKED_IPV6_CIDRS)
    def test_blocks_every_ipv6_internal_range(self, cidr: str) -> None:
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv6", FORWARD_CHAIN), FORWARD_CHAIN)
        assert any(_is_drop_to(rule, cidr) for rule in rules), f"{cidr} 未被拦截"

    def test_cloud_metadata_range_is_blocked(self) -> None:
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        assert any(_is_drop_to(rule, "169.254.0.0/16") for rule in rules)

    def test_rpc_accept_precedes_every_drop(self) -> None:
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        first_drop = _index_of(rules, lambda rule: rule.args[-1] == "DROP")
        for address, port in SPEC.rpc_endpoints:
            accept_index = _index_of(
                rules,
                lambda rule, address=address, port=port: (
                    _option(rule, "-d") == address and _is_accept_on_port(rule, "tcp", str(port))
                ),
            )
            assert accept_index < first_drop, f"RPC 例外 {address}:{port} 排在 DROP 之后，等于不生效"

    def test_rpc_accept_survives_dnat_via_conntrack_original_port(self) -> None:
        """标准 docker-compose 部署里 NA 的端口是发布出来的：沙盒发往「宿主:8021」的包
        会先被 DNAT 改写成 NA 容器的 172.x 地址，到达转发链时看起来就是一个「目的地在私网」
        的包，只按目的地址放行会把它丢掉，整个沙盒功能瘫痪（已在 Linux 上实测复现）。
        因此必须按 conntrack 记录的原始目的端口放行。"""
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        first_drop = _index_of(rules, lambda rule: rule.args[-1] == "DROP")
        index = _index_of(
            rules,
            lambda rule: _option(rule, "--ctorigdstport") == "8021" and rule.args[-1] == "ACCEPT",
        )
        assert index < first_drop

    def test_dnat_exception_is_emitted_once_per_port(self) -> None:
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        assert len([rule for rule in rules if _option(rule, "--ctorigdstport") == "8021"]) == 1

    def test_dns_accept_precedes_every_drop(self) -> None:
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        first_drop = _index_of(rules, lambda rule: rule.args[-1] == "DROP")
        for protocol in ("udp", "tcp"):
            index = _index_of(rules, lambda rule, protocol=protocol: _is_accept_on_port(rule, protocol, "53"))
            assert index < first_drop

    def test_public_destinations_are_not_matched(self) -> None:
        """公网出网靠「不匹配任何规则」实现，转发链上不能出现无目的地约束的规则"""
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        for rule in rules:
            narrowed = any(
                _option(rule, flag) for flag in ("-d", "--dport", "--ctstate", "--ctorigdstport")
            )
            assert narrowed, f"转发链出现无条件规则，会波及公网出网: {rule.args}"

    def test_every_rule_is_scoped_to_sandbox_bridge(self) -> None:
        for family in ("ipv4", "ipv6"):
            for rule in build_isolation_rules(SPEC, family, FORWARD_CHAIN):  # type: ignore[arg-type]
                assert rule.args[:2] == ("-i", SPEC.bridge_interface), f"规则未限定沙盒网桥: {rule.args}"

    def test_ipv4_rules_are_scoped_to_sandbox_subnet(self) -> None:
        for rule in build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN):
            assert rule.args[2:4] == ("-s", "172.28.0.0/16")

    def test_missing_subnet_falls_back_to_interface_scope(self) -> None:
        spec = SandboxNetworkSpec(
            bridge_interface="nekro-sbx0",
            ipv4_subnet=None,
            rpc_endpoints=(("172.28.0.1", 8021),),
            marker=MARKER,
        )
        for rule in build_isolation_rules(spec, "ipv4", FORWARD_CHAIN):
            assert "-s" not in rule.args
            assert rule.args[:2] == ("-i", "nekro-sbx0")


class TestHostInputRules:
    """发往宿主自身：默认拒绝，只留 NA 回调与 DNS"""

    def test_defaults_to_deny(self) -> None:
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        catch_all = rules[-1]
        assert catch_all.args[-1] == "DROP"
        assert "-d" not in catch_all.args, "发往宿主的兜底拒绝不能限定目的地址：宿主地址可能是公网 IP"

    def test_rpc_port_accept_precedes_catch_all_drop(self) -> None:
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        drop_index = _index_of(rules, lambda rule: rule.args[-1] == "DROP")
        accept_index = _index_of(rules, lambda rule: _is_accept_on_port(rule, "tcp", "8021"))
        assert accept_index < drop_index

    def test_rpc_accept_is_port_only(self) -> None:
        """按端口而非地址放行：ExtraHosts 指到哪个宿主地址都不会误伤回调"""
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        accepts = [rule for rule in rules if _option(rule, "--dport") == "8021"]
        assert accepts
        for rule in accepts:
            assert "-d" not in rule.args

    def test_duplicate_rpc_ports_yield_one_rule(self) -> None:
        """两个 RPC 端点共用 8021 端口，按端口放行时不该产出两条一样的规则"""
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        assert len([rule for rule in rules if _option(rule, "--dport") == "8021"]) == 1

    def test_ipv6_allows_icmpv6_before_catch_all_drop(self) -> None:
        """丢掉 NDP 会让容器连网关都找不到，公网 IPv6 出网会一并失效"""
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv6", FORWARD_CHAIN), INPUT_CHAIN)
        drop_index = _index_of(rules, lambda rule: rule.args[-1] == "DROP")
        icmp_index = _index_of(rules, lambda rule: "ipv6-icmp" in rule.args)
        assert icmp_index < drop_index

    def test_established_return_traffic_is_accepted_first(self) -> None:
        rules = _chain_rules(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        assert "ESTABLISHED,RELATED" in rules[0].args
        assert rules[0].args[-1] == "ACCEPT"


class TestInstallCommands:
    def test_insert_indexes_preserve_declared_order_per_chain(self) -> None:
        rules = build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN)
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

    def test_uses_insert_not_append(self) -> None:
        """链尾可能已有其他工具下的放行规则，追加等于规则不生效"""
        for command in build_install_commands(build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN)):
            assert command[0] == "-I"

    def test_every_rule_carries_the_marker(self) -> None:
        for family in ("ipv4", "ipv6"):
            for rule in build_isolation_rules(SPEC, family, FORWARD_CHAIN):  # type: ignore[arg-type]
                assert "--comment" in rule.args
                assert MARKER in rule.args


class TestCleanupCommands:
    SAVE_OUTPUT = "\n".join(
        [
            "-N DOCKER-USER",
            "-A DOCKER-USER -j RETURN",
            "-A DOCKER-USER -i nekro-sbx0 -s 172.28.0.0/16 -d 10.0.0.0/8 "
            '-m comment --comment "nekro-sandbox:default" -j DROP',
            "-A DOCKER-USER -i nekro-sbx0 -s 172.28.0.0/16 -d 169.254.0.0/16 "
            '-m comment --comment "nekro-sandbox:default" -j DROP',
            "-A DOCKER-USER -i nksbx-aabbccdd -d 10.0.0.0/8 "
            '-m comment --comment "nekro-sandbox:other_instance" -j DROP',
            "-A INPUT -i nekro-sbx0 -m comment --comment nekro-sandbox:default -j DROP",
        ],
    )

    def test_removes_only_this_instance_rules(self) -> None:
        commands = build_cleanup_commands("DOCKER-USER", self.SAVE_OUTPUT, MARKER)
        assert len(commands) == 2
        for command in commands:
            assert command[0] == "-D"
            assert command[1] == "DOCKER-USER"
            assert MARKER in command
        assert all("nekro-sandbox:other_instance" not in command for command in commands)

    def test_leaves_foreign_rules_untouched(self) -> None:
        commands = build_cleanup_commands("DOCKER-USER", self.SAVE_OUTPUT, MARKER)
        assert all("-j RETURN" not in " ".join(command) for command in commands)

    def test_scopes_to_the_requested_chain(self) -> None:
        commands = build_cleanup_commands(INPUT_CHAIN, self.SAVE_OUTPUT, MARKER)
        assert commands == [["-D", "INPUT", "-i", "nekro-sbx0", "-m", "comment", "--comment", MARKER, "-j", "DROP"]]

    def test_deletion_round_trips_installed_rules(self) -> None:
        """安装出来的规则必须能被自己的清理逻辑原样删掉，否则重启就会堆积"""
        rules = build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN)
        forward_rules = _chain_rules(rules, FORWARD_CHAIN)
        save_output = "\n".join(f"-A {FORWARD_CHAIN} " + " ".join(rule.args) for rule in forward_rules)
        commands = build_cleanup_commands(FORWARD_CHAIN, save_output, MARKER)
        assert [command[2:] for command in commands] == [list(rule.args) for rule in forward_rules]

    def test_empty_chain_yields_no_deletion(self) -> None:
        assert build_cleanup_commands("DOCKER-USER", "-N DOCKER-USER\n-A DOCKER-USER -j RETURN", MARKER) == []


class TestIndexedCleanupFallback:
    """按原文重放删不掉时的兜底

    实测发现的真实故障：宿主 iptables 1.8.7 读回 1.8.11 写的规则时渲染不出 `--dport`
    （1.8.9 起隐式端口匹配被编码成原生 nft 表达式），照残缺文本发出的 `-D` 匹配不到任何规则，
    旧规则一条都删不掉、每次重启再叠一套，过期的 ACCEPT 例外会一直留在链上。
    """

    # 第 2、4 条带本实例标记；第 3 条是别的实例的，绝不能动
    SAVE_OUTPUT = "\n".join(
        [
            "-N DOCKER-USER",
            "-A DOCKER-USER -j RETURN",
            '-A DOCKER-USER -i nekro-sbx0 -p udp -m comment --comment "nekro-sandbox:default" -j ACCEPT',
            '-A DOCKER-USER -i nksbx-aabbccdd -d 10.0.0.0/8 -m comment --comment "nekro-sandbox:other" -j DROP',
            '-A DOCKER-USER -i nekro-sbx0 -d 169.254.0.0/16 -m comment --comment "nekro-sandbox:default" -j DROP',
        ],
    )

    def test_deletes_marked_rules_by_line_number(self) -> None:
        assert build_indexed_cleanup_commands("DOCKER-USER", self.SAVE_OUTPUT, MARKER) == [
            ["-D", "DOCKER-USER", "4"],
            ["-D", "DOCKER-USER", "2"],
        ]

    def test_deletes_from_the_bottom_up(self) -> None:
        """从大到小删，前面的删除才不会让后面的行号位移"""
        commands = build_indexed_cleanup_commands("DOCKER-USER", self.SAVE_OUTPUT, MARKER)
        indexes = [int(command[2]) for command in commands]
        assert indexes == sorted(indexes, reverse=True)

    def test_line_numbers_survive_a_rule_that_cannot_be_tokenised(self) -> None:
        """解析不了的行也要占一个行号，否则后面的行号整体偏移，就会删错规则"""
        save_output = "\n".join(
            [
                '-A DOCKER-USER -i eth0 -m comment --comment "unbalanced quote -j ACCEPT',
                f'-A DOCKER-USER -i nekro-sbx0 -d 10.0.0.0/8 -m comment --comment "{MARKER}" -j DROP',
            ],
        )
        assert build_indexed_cleanup_commands("DOCKER-USER", save_output, MARKER) == [["-D", "DOCKER-USER", "2"]]

    def test_ignores_other_chains(self) -> None:
        save_output = "\n".join(
            [
                f'-A INPUT -i nekro-sbx0 -m comment --comment "{MARKER}" -j DROP',
                f'-A DOCKER-USER -i nekro-sbx0 -m comment --comment "{MARKER}" -j DROP',
            ],
        )
        assert build_indexed_cleanup_commands("DOCKER-USER", save_output, MARKER) == [["-D", "DOCKER-USER", "1"]]

    def test_nothing_to_delete_yields_no_command(self) -> None:
        assert build_indexed_cleanup_commands("DOCKER-USER", "-N DOCKER-USER\n-A DOCKER-USER -j RETURN", MARKER) == []

    def test_catches_what_text_replay_misses_across_iptables_versions(self) -> None:
        """1.8.7 读 1.8.11 写的规则时丢掉了 `-m udp --dport 53`；
        按原文重放的 `-D` 会删掉一条**语义不同**的规则或什么都删不掉，行号删除则照样命中"""
        installed = '-A DOCKER-USER -i nekro-sbx0 -p udp -m udp --dport 53 -m comment --comment "%s" -j ACCEPT' % MARKER
        rendered_by_old_iptables = '-A DOCKER-USER -i nekro-sbx0 -p udp -m comment --comment "%s" -j ACCEPT' % MARKER

        replayed = build_cleanup_commands("DOCKER-USER", rendered_by_old_iptables, MARKER)
        assert replayed and replayed[0][2:] != shlex.split(installed)[2:], "残缺文本重放出的删除命令与实际规则不一致"
        assert build_indexed_cleanup_commands("DOCKER-USER", rendered_by_old_iptables, MARKER) == [
            ["-D", "DOCKER-USER", "1"],
        ]


class TestRpcEndpointResolution:
    def test_host_alias_expands_to_host_addresses(self) -> None:
        endpoints = resolve_rpc_endpoints("http://host.docker.internal:8021/api", ["172.28.0.1", "172.17.0.1"])
        assert endpoints == (("172.28.0.1", 8021), ("172.17.0.1", 8021))

    def test_ip_literal_keeps_its_own_address(self) -> None:
        endpoints = resolve_rpc_endpoints("http://192.168.9.9:9000/api", ["172.28.0.1"])
        assert endpoints == (("192.168.9.9", 9000), ("172.28.0.1", 9000))

    @pytest.mark.parametrize(
        ("url", "expected_port"),
        [
            ("http://host.docker.internal/api", 80),
            ("https://host.docker.internal/api", 443),
            ("http://host.docker.internal:8021/api", 8021),
        ],
    )
    def test_port_defaults_follow_scheme(self, url: str, expected_port: int) -> None:
        endpoints = resolve_rpc_endpoints(url, ["172.28.0.1"])
        assert endpoints == (("172.28.0.1", expected_port),)

    def test_deduplicates_repeated_host_addresses(self) -> None:
        endpoints = resolve_rpc_endpoints("http://host.docker.internal:8021/api", ["172.28.0.1", "172.28.0.1"])
        assert endpoints == (("172.28.0.1", 8021),)

    @pytest.mark.parametrize("url", ["", "   ", "not-a-url", "http://"])
    def test_unparseable_url_yields_no_exception_rule(self, url: str) -> None:
        assert resolve_rpc_endpoints(url, ["172.28.0.1"]) == ()

    def test_ipv6_host_addresses_are_dropped(self) -> None:
        """例外规则只在 IPv4 侧生成，IPv6 地址混进来会产出装不上的规则"""
        assert resolve_rpc_endpoints("http://host.docker.internal:8021/api", ["fe80::1"]) == ()


class TestSelfContainerDetection:
    """NA 跑在容器里时，规则要靠 NA 自己的镜像起临时容器来下；认不出自己 = 整条防线退化成告警"""

    # 取自 Ubuntu 22.04 + cgroup v2 宿主上真实容器的 /proc 内容
    CGROUP_V2 = "0::/\n"
    CGROUP_V1 = "12:pids:/docker/" + "a" * 64 + "\n11:cpu:/docker/" + "a" * 64 + "\n"
    MOUNTINFO = (
        "1223 1214 252:1 /var/lib/docker/containers/"
        + "2" * 64
        + "/resolv.conf /etc/resolv.conf rw,relatime - ext4 /dev/vda1 rw\n"
        "1224 1214 252:1 /var/lib/docker/containers/"
        + "2" * 64
        + "/hostname /etc/hostname rw,relatime - ext4 /dev/vda1 rw\n"
    )

    def test_mountinfo_wins_when_cgroup_v2_carries_no_id(self) -> None:
        """cgroup v2 下 /proc/self/cgroup 只有 `0::/`，唯一可靠的线索是 mountinfo"""
        candidates = parse_self_container_ids("214704e42d7f", self.CGROUP_V2, self.MOUNTINFO)
        assert candidates[0] == "2" * 64
        assert "214704e42d7f" in candidates

    def test_hostname_alone_is_enough(self) -> None:
        assert parse_self_container_ids("214704e42d7f", "", "") == ["214704e42d7f"]

    def test_cgroup_v1_id_is_picked_up(self) -> None:
        assert "a" * 64 in parse_self_container_ids("", self.CGROUP_V1, "")

    def test_candidates_are_deduplicated(self) -> None:
        candidates = parse_self_container_ids("2" * 64, self.CGROUP_V2, self.MOUNTINFO)
        assert candidates == ["2" * 64]

    def test_no_traces_yields_no_candidate(self) -> None:
        assert parse_self_container_ids("", self.CGROUP_V2, "") == []


class TestExtraBlockedDestinations:
    """运维显式声明的额外切断目的地

    补的是本守卫唯一堵不上的那类目的地：云上 1:1 NAT 里宿主对外的公网 IP 不在宿主任何本地
    网卡上，容器发往它的包经云网关发夹回来，既不匹配转发链上的私有段黑名单，也不匹配 INPUT 上
    按沙盒网桥收窄的默认拒绝（实测：全部规则计数器保持 0）。这类地址只能由运维声明。
    """

    @pytest.mark.parametrize(
        ("declared", "expected_ipv4", "expected_ipv6"),
        [
            ((), (), ()),
            (("203.0.113.7",), ("203.0.113.7/32",), ()),
            (("203.0.113.0/24",), ("203.0.113.0/24",), ()),
            (("2001:db8::1",), (), ("2001:db8::1/128",)),
            (("203.0.113.7", "2001:db8::/32"), ("203.0.113.7/32",), ("2001:db8::/32",)),
            (("  203.0.113.7  ", ""), ("203.0.113.7/32",), ()),
            (("203.0.113.7", "203.0.113.7/32"), ("203.0.113.7/32",), ()),
            (("not-an-ip", "203.0.113.7"), ("203.0.113.7/32",), ()),
            (("203.0.113.7/24",), ("203.0.113.0/24",), ()),
        ],
    )
    def test_declared_values_are_split_and_normalised(
        self,
        declared: tuple,
        expected_ipv4: tuple,
        expected_ipv6: tuple,
    ) -> None:
        assert split_extra_blocked_cidrs(declared) == (expected_ipv4, expected_ipv6)

    def test_empty_declaration_changes_nothing(self) -> None:
        """默认留空必须与引入本特性之前逐条一致，否则升级会静默改变每个部署的规则集"""
        for family in ("ipv4", "ipv6"):
            baseline = build_isolation_rules(SPEC, family, FORWARD_CHAIN)  # type: ignore[arg-type]
            declared = build_isolation_rules(
                SandboxNetworkSpec(
                    bridge_interface=SPEC.bridge_interface,
                    ipv4_subnet=SPEC.ipv4_subnet,
                    rpc_endpoints=SPEC.rpc_endpoints,
                    marker=SPEC.marker,
                ),
                family,  # type: ignore[arg-type]
                FORWARD_CHAIN,
            )
            assert baseline == declared

    def test_declared_destination_is_dropped_after_every_accept(self) -> None:
        """运维声明的往往正是宿主自己的公网 IP，而 NA 的回调端点可能就挂在同一个地址上：
        这条 DROP 排在 ACCEPT 例外之前就会把沙盒回连 NA 一起切掉，沙盒功能整个瘫痪。"""
        spec = SandboxNetworkSpec(
            bridge_interface=SPEC.bridge_interface,
            ipv4_subnet=SPEC.ipv4_subnet,
            rpc_endpoints=SPEC.rpc_endpoints,
            marker=SPEC.marker,
            extra_blocked_ipv4=("203.0.113.7/32",),
        )
        rules = _chain_rules(build_isolation_rules(spec, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        index = _index_of(rules, lambda rule: _is_drop_to(rule, "203.0.113.7/32"))
        last_accept = max(i for i, rule in enumerate(rules) if rule.args[-1] == "ACCEPT")
        assert index > last_accept

    def test_declared_destination_stays_out_of_the_host_chain(self) -> None:
        """发往宿主的链已经是默认拒绝，再加地址匹配没有意义，只会让规则数无谓翻倍"""
        spec = SandboxNetworkSpec(
            bridge_interface=SPEC.bridge_interface,
            ipv4_subnet=SPEC.ipv4_subnet,
            rpc_endpoints=SPEC.rpc_endpoints,
            marker=SPEC.marker,
            extra_blocked_ipv4=("203.0.113.7/32",),
        )
        rules = _chain_rules(build_isolation_rules(spec, "ipv4", FORWARD_CHAIN), INPUT_CHAIN)
        assert all("203.0.113.7/32" not in rule.args for rule in rules)

    def test_families_do_not_leak_into_each_other(self) -> None:
        """把 IPv6 网段下进 iptables（或反过来）会让整套规则安装失败"""
        spec = SandboxNetworkSpec(
            bridge_interface=SPEC.bridge_interface,
            ipv4_subnet=SPEC.ipv4_subnet,
            rpc_endpoints=SPEC.rpc_endpoints,
            marker=SPEC.marker,
            extra_blocked_ipv4=("203.0.113.7/32",),
            extra_blocked_ipv6=("2001:db8::/32",),
        )
        ipv4_rules = build_isolation_rules(spec, "ipv4", FORWARD_CHAIN)
        ipv6_rules = build_isolation_rules(spec, "ipv6", FORWARD_CHAIN)
        assert all("2001:db8::/32" not in rule.args for rule in ipv4_rules)
        assert all("203.0.113.7/32" not in rule.args for rule in ipv6_rules)

    def test_declared_rules_round_trip_through_cleanup(self) -> None:
        """声明的规则也必须能被按标记清理掉，否则改一次配置就在链上留一条过期 DROP"""
        spec = SandboxNetworkSpec(
            bridge_interface=SPEC.bridge_interface,
            ipv4_subnet=SPEC.ipv4_subnet,
            rpc_endpoints=SPEC.rpc_endpoints,
            marker=SPEC.marker,
            extra_blocked_ipv4=("203.0.113.7/32",),
        )
        rules = _chain_rules(build_isolation_rules(spec, "ipv4", FORWARD_CHAIN), FORWARD_CHAIN)
        save_output = "\n".join(f"-A {FORWARD_CHAIN} " + " ".join(rule.args) for rule in rules)
        commands = build_cleanup_commands(FORWARD_CHAIN, save_output, MARKER)
        assert [command[2:] for command in commands] == [list(rule.args) for rule in rules]


class TestRpcAcceptIsMandatory:
    """没有 RPC 放行的规则集必须装不上

    INPUT 的尾部是兜底拒绝，少了 RPC 放行就是一套**保证**把沙盒打瘫的规则，
    而且瘫得毫无线索：DROP 不回 RST，容器里的 `requests.post` 一路等到沙盒运行超时，
    NA 侧一条错误日志都没有——外面只看得到「沙盒又超时了」。
    这种规则装上去比装不上更糟，所以在生成阶段就拒绝。
    """

    EMPTY_ENDPOINT_SPEC = SandboxNetworkSpec(
        bridge_interface="nekro-sbx0",
        ipv4_subnet="172.28.0.0/16",
        rpc_endpoints=(),
        marker=MARKER,
    )

    @pytest.mark.parametrize("family", ["ipv4", "ipv6"])
    def test_empty_rpc_endpoints_refuse_to_build(self, family: str) -> None:
        with pytest.raises(SandboxNetworkIsolationError):
            build_isolation_rules(self.EMPTY_ENDPOINT_SPEC, family, FORWARD_CHAIN)  # type: ignore[arg-type]

    def test_error_names_the_offending_setting(self) -> None:
        """报错要指名道姓，否则运维只能从「沙盒又超时了」倒推到防火墙"""
        with pytest.raises(SandboxNetworkIsolationError, match="SANDBOX_CHAT_API_URL"):
            build_isolation_rules(self.EMPTY_ENDPOINT_SPEC, "ipv4", FORWARD_CHAIN)

    @pytest.mark.parametrize("family", ["ipv4", "ipv6"])
    def test_host_chain_never_ends_up_deny_only(self, family: str) -> None:
        """反过来钉住正常路径：只要生成得出规则集，发往宿主的链里就一定有 ACCEPT 排在 DROP 之前"""
        rules = _chain_rules(build_isolation_rules(SPEC, family, FORWARD_CHAIN), INPUT_CHAIN)  # type: ignore[arg-type]
        accepts = [index for index, rule in enumerate(rules) if rule.args[-1] == "ACCEPT"]
        drops = [index for index, rule in enumerate(rules) if rule.args[-1] == "DROP"]
        assert accepts, "发往宿主的链只剩拒绝，沙盒回连 NA 会被静默吞掉"
        assert min(accepts) < min(drops)


def _render_save_output(chain: str, rules: List[IptablesRule], foreign: Sequence[str] = ()) -> str:
    """把规则集渲染成 `iptables -S <chain>` 的样子，可掺入别人的规则"""
    lines = [f"-N {chain}", *foreign]
    lines += [f"-A {chain} " + " ".join(rule.args) for rule in rules if rule.chain == chain]
    return "\n".join(lines)


def _strip_port_matches(save_output: str) -> str:
    """模拟老版本 iptables 读不出隐式端口匹配（1.8.9 起被编码成原生 nft 表达式）"""
    stripped: List[str] = []
    for line in save_output.splitlines():
        tokens = line.split()
        cleaned: List[str] = []
        skip = False
        for token in tokens:
            if skip:
                skip = False
                continue
            if token == "--dport":
                skip = True
                continue
            cleaned.append(token)
        stripped.append(" ".join(cleaned))
    return "\n".join(stripped)


class _FakeIptables:
    """按 (地址族, 链) 回放一段 `iptables -S` 输出

    只实现 `verify_marked_rules` 用到的那一个动作，因此不需要真的 iptables 也能把
    「规则被换成了别人的那套」这类故障钉进单测。
    """

    description = "fake"

    def __init__(
        self,
        chains: Dict[Tuple[str, str], str],
        unsupported_families: Sequence[str] = ("ipv6",),
        failing_chains: Sequence[str] = (),
    ) -> None:
        self._chains = chains
        self._unsupported = set(unsupported_families)
        self._failing = set(failing_chains)
        self.calls: List[Tuple[str, Tuple[str, ...]]] = []

    async def run(self, family: AddressFamily, args: Sequence[str]) -> Tuple[int, str]:
        self.calls.append((family, tuple(args)))
        if family in self._unsupported:
            return 1, "ip6tables unavailable"
        if len(args) == 2 and args[0] == "-S":
            chain = args[1]
            if chain in self._failing:
                return 1, "Resource temporarily unavailable"
            if (family, chain) not in self._chains:
                return 1, f"No chain/target/match by that name: {chain}"
            return 0, self._chains[(family, chain)]
        return 0, ""


def _fake_from_rules(rules: List[IptablesRule], transform=lambda text: text) -> _FakeIptables:
    return _FakeIptables(
        {
            ("ipv4", FORWARD_CHAIN): transform(_render_save_output(FORWARD_CHAIN, rules)),
            ("ipv4", INPUT_CHAIN): transform(_render_save_output(INPUT_CHAIN, rules)),
        },
    )


class TestMarkedRuleSummary:
    """回读摘要：比什么、不比什么

    严格按整行文本比对是行不通的——`iptables -S` 跨版本会缺匹配项，硬比会得出「每次都不一致」，
    于是退化成「每跑一次沙盒重装一次规则」，比不比对还糟。所以只比条数、动作与端口集合。
    """

    RULES = build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN)

    def test_reads_back_what_we_installed(self) -> None:
        for chain in (FORWARD_CHAIN, INPUT_CHAIN):
            expected = summarize_expected_rules(chain, self.RULES)
            actual = summarize_marked_rules(chain, _render_save_output(chain, self.RULES), MARKER)
            assert diff_marked_rules(expected, actual) == ""

    def test_ignores_rules_belonging_to_another_instance(self) -> None:
        foreign = [
            f"-A {INPUT_CHAIN} -i nksbx-aabbccdd -p tcp --dport 9999 "
            '-m comment --comment "nekro-sandbox:other" -j ACCEPT',
            f"-A {INPUT_CHAIN} -j RETURN",
        ]
        actual = summarize_marked_rules(
            INPUT_CHAIN,
            _render_save_output(INPUT_CHAIN, self.RULES, foreign=foreign),
            MARKER,
        )
        assert diff_marked_rules(summarize_expected_rules(INPUT_CHAIN, self.RULES), actual) == ""

    def test_port_swap_is_detected(self) -> None:
        """事故现场的形态：条数与动作都对得上，只有放行端口被换成了别人的探针端口"""
        swapped = _render_save_output(INPUT_CHAIN, self.RULES).replace("8021", "19311")
        difference = diff_marked_rules(
            summarize_expected_rules(INPUT_CHAIN, self.RULES),
            summarize_marked_rules(INPUT_CHAIN, swapped, MARKER),
        )
        assert "19311" in difference

    def test_wiped_chain_is_detected(self) -> None:
        difference = diff_marked_rules(
            summarize_expected_rules(INPUT_CHAIN, self.RULES),
            summarize_marked_rules(INPUT_CHAIN, f"-N {INPUT_CHAIN}\n-A {INPUT_CHAIN} -j RETURN", MARKER),
        )
        assert difference

    def test_missing_one_rule_is_detected(self) -> None:
        partial_rules = [rule for rule in self.RULES if rule.chain != INPUT_CHAIN or rule.args[-1] != "DROP"]
        difference = diff_marked_rules(
            summarize_expected_rules(INPUT_CHAIN, self.RULES),
            summarize_marked_rules(INPUT_CHAIN, _render_save_output(INPUT_CHAIN, partial_rules), MARKER),
        )
        assert difference

    @pytest.mark.parametrize("chain", [FORWARD_CHAIN, INPUT_CHAIN])
    def test_unrenderable_ports_do_not_count_as_a_mismatch(self, chain: str) -> None:
        """老版本 iptables 渲染不出隐式的 `-p tcp --dport N`，但**显式匹配模块**的
        `-m conntrack --ctorigdstport N` 照样渲染得出来。于是转发链上实际读到的端口集合会是
        期望集合的一个真子集——按「集合相等」判就会每次核对都误报，退化成每跑一次沙盒重装一次。
        （这条不是假想：改成按「多出来的端口」判之前，本用例在转发链上真的红过。）"""
        actual = summarize_marked_rules(
            chain,
            _strip_port_matches(_render_save_output(chain, self.RULES)),
            MARKER,
        )
        assert actual.ports <= summarize_expected_rules(chain, self.RULES).ports
        assert diff_marked_rules(summarize_expected_rules(chain, self.RULES), actual) == ""

    @pytest.mark.parametrize(
        ("expected", "actual", "should_differ"),
        [
            (MarkedRuleSummary(0, (), frozenset()), MarkedRuleSummary(0, (), frozenset()), False),
            (MarkedRuleSummary(2, ("ACCEPT", "DROP"), frozenset({"8021"})), MarkedRuleSummary(0, (), frozenset()), True),
            (
                MarkedRuleSummary(2, ("ACCEPT", "DROP"), frozenset({"8021"})),
                MarkedRuleSummary(2, ("ACCEPT", "DROP"), frozenset({"8021"})),
                False,
            ),
            (
                MarkedRuleSummary(2, ("ACCEPT", "DROP"), frozenset({"8021"})),
                MarkedRuleSummary(2, ("DROP", "DROP"), frozenset({"8021"})),
                True,
            ),
            (
                MarkedRuleSummary(2, ("ACCEPT", "DROP"), frozenset({"8021"})),
                MarkedRuleSummary(2, ("ACCEPT", "DROP"), frozenset({"19311"})),
                True,
            ),
            # 期望有端口、实际一个都读不出来 = 版本渲染不出，不算不一致
            (
                MarkedRuleSummary(2, ("ACCEPT", "DROP"), frozenset({"8021"})),
                MarkedRuleSummary(2, ("ACCEPT", "DROP"), frozenset()),
                False,
            ),
            # 只渲染得出一部分（显式匹配模块的端口）同理，是子集不是不一致
            (
                MarkedRuleSummary(3, ("ACCEPT", "ACCEPT", "DROP"), frozenset({"8021", "53"})),
                MarkedRuleSummary(3, ("ACCEPT", "ACCEPT", "DROP"), frozenset({"8021"})),
                False,
            ),
            # 多出来的端口不可能是渲染造成的，一定是别人装的
            (
                MarkedRuleSummary(3, ("ACCEPT", "ACCEPT", "DROP"), frozenset({"8021", "53"})),
                MarkedRuleSummary(3, ("ACCEPT", "ACCEPT", "DROP"), frozenset({"8021", "53", "19311"})),
                True,
            ),
        ],
    )
    def test_diff_truth_table(
        self,
        expected: MarkedRuleSummary,
        actual: MarkedRuleSummary,
        should_differ: bool,
    ) -> None:
        assert bool(diff_marked_rules(expected, actual)) is should_differ


class TestVerifyMarkedRules:
    """把内核里的规则读回来核对

    这一组守的是那次真实事故：一个共用同一标记的验证脚本按标记把放行规则整组删掉、
    换上指向自己探针端口的那套，NA 内存里仍然记着「装过了」，于是九个多小时里
    每一次沙盒执行都静默卡到超时，NA 侧零错误日志。
    """

    RULES = build_isolation_rules(SPEC, "ipv4", FORWARD_CHAIN)
    BUILD = staticmethod(lambda family, forward_chain: build_isolation_rules(SPEC, family, forward_chain))

    async def test_intact_rules_report_no_difference(self) -> None:
        assert await verify_marked_rules(_fake_from_rules(self.RULES), MARKER, self.BUILD) == ""

    async def test_port_swapped_by_another_process_is_reported(self) -> None:
        executor = _fake_from_rules(self.RULES, transform=lambda text: text.replace("8021", "19311"))
        difference = await verify_marked_rules(executor, MARKER, self.BUILD)
        assert difference
        assert "19311" in difference

    async def test_wiped_rules_are_reported(self) -> None:
        executor = _FakeIptables(
            {
                ("ipv4", FORWARD_CHAIN): f"-N {FORWARD_CHAIN}\n-A {FORWARD_CHAIN} -j RETURN",
                ("ipv4", INPUT_CHAIN): f"-N {INPUT_CHAIN}",
            },
        )
        assert await verify_marked_rules(executor, MARKER, self.BUILD)

    async def test_old_iptables_rendering_is_not_a_false_alarm(self) -> None:
        """误报的代价是「每跑一次沙盒重装一次规则」，所以这条必须钉死"""
        executor = _fake_from_rules(self.RULES, transform=_strip_port_matches)
        assert await verify_marked_rules(executor, MARKER, self.BUILD) == ""

    async def test_unreadable_chain_is_not_reported_as_a_mismatch(self) -> None:
        """读不出来只说明这次核对没做成，不该演变成一次重装"""
        executor = _FakeIptables(
            {
                ("ipv4", FORWARD_CHAIN): _render_save_output(FORWARD_CHAIN, self.RULES),
                ("ipv4", INPUT_CHAIN): _render_save_output(INPUT_CHAIN, self.RULES),
            },
            failing_chains=(INPUT_CHAIN,),
        )
        assert await verify_marked_rules(executor, MARKER, self.BUILD) == ""

    async def test_missing_ip6tables_is_skipped_like_installation_does(self) -> None:
        """`install_marked_rules` 在 ip6tables 不可用时跳过 IPv6，核对必须用同一套判据，
        否则每次核对都报「IPv6 规则不见了」"""
        executor = _fake_from_rules(self.RULES)
        assert await verify_marked_rules(executor, MARKER, self.BUILD) == ""
        assert any(family == "ipv6" for family, _ in executor.calls), "IPv6 侧应当尝试过"

    async def test_another_instances_rules_do_not_trigger_reinstall(self) -> None:
        """同宿主上的第二套部署有自己的标记，它的规则不该被误读成「我的规则被改了」"""
        foreign = [
            f"-A {INPUT_CHAIN} -i nksbx-aabbccdd -p tcp --dport 19311 "
            '-m comment --comment "nekro-sandbox:other" -j ACCEPT',
            f'-A {INPUT_CHAIN} -i nksbx-aabbccdd -m comment --comment "nekro-sandbox:other" -j DROP',
        ]
        executor = _FakeIptables(
            {
                ("ipv4", FORWARD_CHAIN): _render_save_output(FORWARD_CHAIN, self.RULES),
                ("ipv4", INPUT_CHAIN): _render_save_output(INPUT_CHAIN, self.RULES, foreign=foreign),
            },
        )
        assert await verify_marked_rules(executor, MARKER, self.BUILD) == ""

    async def test_unbuildable_ruleset_does_not_report_a_mismatch(self) -> None:
        """规则集本身已经生成不出来（回调端点解析不到）时，该由安装路径去报错，
        核对不该抢在前面把缓存作废掉、把日志刷成两份"""

        def build(family, forward_chain):  # noqa: ANN001, ANN202
            return build_isolation_rules(
                SandboxNetworkSpec(
                    bridge_interface=SPEC.bridge_interface,
                    ipv4_subnet=SPEC.ipv4_subnet,
                    rpc_endpoints=(),
                    marker=MARKER,
                ),
                family,
                forward_chain,
            )

        assert await verify_marked_rules(_fake_from_rules(self.RULES), MARKER, build) == ""


class TestNamingIsolation:
    def test_names_are_instance_scoped(self) -> None:
        assert sandbox_network_name("") == "nekro_sandbox_net"
        assert sandbox_network_name("inst1-") == "inst1-nekro_sandbox_net"
        assert isolation_marker("") == "nekro-sandbox:default"
        assert isolation_marker("inst1-") == "nekro-sandbox:inst1-"
        assert sandbox_bridge_name("inst1-") != sandbox_bridge_name("inst2-")

    def test_bridge_name_fits_kernel_interface_limit(self) -> None:
        for instance in ("", "inst1-", "a" * 64):
            assert len(sandbox_bridge_name(instance)) < 16

    def test_marker_strips_characters_that_break_rule_parsing(self) -> None:
        marker = isolation_marker('bad name"with spaces')
        assert " " not in marker
        assert '"' not in marker
