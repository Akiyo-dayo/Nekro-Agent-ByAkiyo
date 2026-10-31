"""CC 工作区容器的网络隔离守卫

## 为什么工作区也要隔离

CC 工作区里跑的是 Claude Code，它执行的同样是模型生成的代码，而模型的输入由用户完全控制。
这与 `services/sandbox/network_guard.py` 处理的一次性沙盒属于**同一威胁类**，但工作区此前的
处境更糟：它被直接挂在 NA 自己的 `nekro_network` 上，于是模型生成的代码可以

- 直连同网络的全部兄弟容器（Postgres、Qdrant、协议端……），这些服务在内网里通常不设防；
- 扫宿主所在的 RFC1918 内网；
- 打云厂商元数据服务 `169.254.169.254` 拿宿主的 IAM 临时凭据。

## 与沙盒的差异：方向反过来了

**沙盒**是「容器主动回连 NA」——它必须能访问 `SANDBOX_CHAT_API_URL`，所以沙盒规则集里有一条
按 conntrack 原始目的端口放行的 RPC 例外。

**CC 工作区在任务通道上恰好相反：NA 始终是客户端，容器只是服务端。**
`INTERNAL_API_TOKEN` 是 NA 作为客户端带上的 Bearer（见 `services/workspace/client.py`），
连「NA 掉线期间完成的结果」都是缓存在容器里等 NA 来 `GET /pending-results`，而不是推给 NA。

唯一的例外是**模型 API 中继**：容器里的 `claude` CLI 把 `ANTHROPIC_BASE_URL` 指向 NA 的
`/api/cc-relay`，由 NA 换上平台方的真实 Key 转发到上游，真实 Key 因此不进容器
（判据见 `services/workspace/cc_credential.py`）。所以规则集里有且只有一条容器发起方向的放行，
目的地钉死在「NA 在本网桥上的地址 + NA 的监听端口」；`NEKRO_CC_CREDENTIAL_MODE=direct`
的部署不生成这条规则，回到「一条都没有」的形态。

工作区之所以原本被放在 `nekro_network` 上，唯一原因是 `DBWorkspace.api_endpoint` 用
`http://{container_name}:{port}` 按**容器名**寻址——这依赖 Docker 内嵌 DNS，而内嵌 DNS 只在
NA 与容器共处同一个 user-defined 网络时才解析得到对方。查清这一点很关键：它不是「工作区需要
访问 NA 的某个接口」，而是「NA 需要访问工作区」。

所以处置方式是：把工作区挪到专用网络，**并把 NA 自己也接入该网络**。这样

- 容器名寻址与健康检查原样可用，工作区功能零改动；
- 工作区与 Postgres/Qdrant/协议端不再共网，最短的横向路径直接消失。

### 兄弟工作区之间：靠「每个租户一张网」隔开，而不是靠包过滤规则

规则集里的 RFC1918 DROP 在语义上涵盖「工作区 A -> 工作区 B」（目的地就落在本网段），
但**同一网桥内的容器互访是二层交换的，根本不进 iptables**：只有宿主加载了 `br_netfilter`
且 `net.bridge.bridge-nf-call-iptables=1` 时，这类流量才会经过 FORWARD 被我们拦下。
实测（Ubuntu 5.15 + Docker 29，未加载 br_netfilter）：同网桥互访畅通，而覆盖该目的地的
DROP 规则计数器**始终为 0**——包一次都没进过这条链。

不去自动改那个 sysctl：它是全局开关，会把宿主上**所有**网桥的二层流量拉进 iptables，
足以打乱与 NA 无关的其他容器网络，不该由 NA 替宿主管理员做这个决定。

所以隔离改由**网络拓扑**承担：工作区按归属分组，**一个分组一张 Docker 网络**
（见 `workspace_group_key`）。跨网络的容器互访不依赖 `br_netfilter`——Docker 自己会在
`table ip raw / PREROUTING`（优先级早于 conntrack，更早于 DOCKER-USER）下一条
`iifname != "<该网络的网桥>" ip daddr <容器地址> drop`。实测跨网络探测被这条规则计中并阻断，
而本模块的规则计数器一动不动，说明这是 Docker 自身在兜底，与我们的规则无关。

**同一分组内的工作区仍然互通**，这是有意接受的：同租户的工作区本就同属一个信任域，
而它们的 API 各有独立 Bearer（`INTERNAL_API_TOKEN` 按工作区生成），拿不到彼此的数据。
真正要切断的是**跨租户**。

代价是 NA 自己的流量也从这些网桥出去，会撞上我们的 DROP，所以规则集头部有一条
**放行 NA 自身源地址**的例外（见 `WorkspaceNetworkSpec.controller_addresses`）；
NA 会按需接入每一张分组网络，容器名寻址因此在所有分组上都成立。

### 分组数量的天花板

每个分组要占一张 bridge 网络，而 Docker 的默认地址池是有限的：`172.16.0.0/12` 切 `/16`
给 16 张、`192.168.0.0/16` 切 `/20` 再给 16 张。实测在一台已有 6 张网络的宿主上，
再建到第 27 张时 `docker network create` 失败。也就是说**默认配置下约 25 个租户分组封顶**。
撞到上限时建网络会失败，落到既有的失败分支：`strict` 拒绝启动工作区，`best_effort` 告警并
退回改造前的形态。需要更多分组的部署应在 `/etc/docker/daemon.json` 里放大
`default-address-pools`，而不是让 NA 去悄悄复用别的分组的网络。

### 那条例外为什么用 ACCEPT 而不是 RETURN

`RETURN` 在自建链（DOCKER-USER）里是「回到 FORWARD 继续」，但在内建链（本模块在
DOCKER-USER 缺失时会退回 FORWARD）里是「直接套用链策略」，而 Docker 常把 FORWARD 策略设为
DROP——同一条规则在两种部署下语义相反，会以「NA 连不上工作区」的形式静默炸掉。
NA 是完全受信的第一方容器，对它的包直接 ACCEPT 是明确且两种链下都成立的选择。

### 沿用沙盒踩过的坑

`DOCKER-USER` 挂在 FORWARD 上，看不见发往宿主自身的包，所以 INPUT 上另有一组**默认拒绝**的
规则（黑名单要穷举宿主的全部本机地址，漏一个就整段失效）；DNS 53 必须放行，否则 AWS/GCP 上
解析器坐在被封网段里会让容器连域名都解析不了，`git clone` / `pip install` / 调用 LLM API
全部不可用；IPv6 一并处理；规则带固定标记、装前先清同标记旧规则以保证幂等；容器不给 `NET_ADMIN`。

**「装过」不等于「还在」**：标记是宿主全局的，任何共用该标记的进程都会先按标记全删再装它自己
那套。沙盒侧已经因此出过一次生产事故（放行端口被换成别人的探针端口，回调包落进兜底 DROP，
每次执行都静默卡到超时）。所以这里同样走缓存快路径时会在**后台**按 TTL 把内核里的规则读回来
比对（`verify_marked_rules`），对不上就打 ERROR 并作废该分组的缓存。

标记里的作用域取自 `core/os_env.py:deployment_scope()`：显式的 `NEKRO_INSTANCE_NAME` 优先，
没设时由数据目录派生一个稳定后缀，于是「同宿主两套部署撞同一个标记」在结构上不再可能
（两套部署不可能共用数据目录）。升级换标记的代价与处置见 `docs/multi-deploy-same-host.md`。

**宿主公网 IP 的发夹缺口在这里同样存在**，边界与处置见 `services/sandbox/network_guard.py`
模块 docstring 的「边界」一节；两侧共用 `config.NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS`。

## 为什么是独立配置项而不是复用 SANDBOX_NETWORK_ISOLATION

两者的正当出网需求并不相同。沙盒是一次性的代码执行环境，工作区是长期存在的开发环境：
它要 `git clone`、装 npm/pypi 依赖、跑浏览器，实践中更可能被部署方指向**内网的** git 服务器
或私有制品库——那正是本守卫会切断的地址。把两者绑在同一个开关上，会逼运维为了让工作区能连
内网 registry 而把沙盒的防护一起关掉。独立开关让这两个决定各自作数。

## 失败行为与规则留存

与沙盒一致：`best_effort` 装不上就告警但继续，`strict` 装不上则拒绝启动工作区容器，`off`
完全不介入；规则在关闭时**不移除**，因为 NA 崩溃退出不会走关闭流程，而那恰是最需要防线的时刻。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Dict, List, Literal, Optional, Sequence, Tuple

import aiodocker

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import (
    OsEnv,
    deployment_scope,
    describe_deployment_scope,
)
from nekro_agent.services.sandbox.network_guard import (
    BLOCKED_IPV4_CIDRS,
    BLOCKED_IPV6_CIDRS,
    DNS_PORT,
    INPUT_CHAIN,
    LEGACY_SCOPE,
    AddressFamily,
    GuardMarkerFamily,
    IptablesRule,
    LegacyMarkerCheck,
    NetworkIsolationError,
    format_isolation_marker,
    install_marked_rules,
    mark_rules_verified,
    report_legacy_marker_orphans,
    reset_rule_verification,
    resolve_executor,
    resolve_self_container,
    schedule_rule_verification,
    split_extra_blocked_cidrs,
    verify_marked_rules,
)
from nekro_agent.services.workspace.cc_credential import credential_mode, relay_host_port, relay_listen_port

if TYPE_CHECKING:
    from nekro_agent.models.db_workspace import DBWorkspace

logger = get_sub_logger("workspace_network")

IsolationMode = Literal["best_effort", "strict", "off"]

# 安装失败后的重试节流：strict 模式下每次创建工作区都会重试，没有节流会把 iptables 调用打成风暴
_RETRY_INTERVAL_SECONDS = 60.0


class WorkspaceNetworkIsolationError(NetworkIsolationError):
    """CC 工作区网络隔离未能生效，且当前配置要求 fail closed"""


# =============================================================================
# 命名与标记（纯函数，规则集与清理逻辑的单测都基于这里）
# =============================================================================


PLATFORM_GROUP = ""
SHARED_GROUP = "shared"
_GROUP_SAFE_PATTERN = re.compile(r"[^a-z0-9]")

WORKSPACE_MARKER_PREFIX = "nekro-cc"

# 孤儿规则运维出口（`services/sandbox/network_guard.py` 的「盘点 -> 判定 -> 清理」）用的标记族。
#
# `long_lived_containers=True` 是这一族与沙盒族**唯一**也是最要命的差别：沙盒容器是一次性的，
# 升级换了作用域之后不会再有新容器挂到旧网桥上，旧规则留着纯属占地方；而 **CC 工作区容器是长命的**，
# 网络在**创建时**就写死进 `NetworkMode`、重启不换网（`services/workspace/container.py:create_and_start`），
# 于是升级前建的工作区**至今仍挂在旧网桥上**。这一组规则里既有放行也有兜底 DROP ——
# 按标记整组删掉之后，那些容器的隔离**静默消失**：可以直接访问宿主内网、兄弟容器、
# Postgres/Qdrant，不报错、不留日志、没有任何运行时信号。
# 所以判定侧对这一族的「旧网桥上还有容器」是硬拦，且提示语必须给出可执行的下一步。
WORKSPACE_MARKER_FAMILY = GuardMarkerFamily(
    prefix=WORKSPACE_MARKER_PREFIX,
    label="CC 工作区网络隔离",
    long_lived_containers=True,
    cleanup_hint="要清理就先把这些工作区**重建一遍**（重建会落到新网桥上），再回来删规则",
)


def workspace_group_key(owner_tenant_ids: Sequence[Optional[int]]) -> str:
    """把工作区绑定频道的归属集合归并成一个网络分组键

    分组键决定这个工作区落在哪张 Docker 网络上，也就决定了它能看见哪些兄弟工作区。
    判据是「所有绑定频道是不是同一个归属」，而不是「有没有租户频道」：
    一个既服务平台方频道又服务某租户频道的工作区，两边的数据都在它里面，
    把它塞进任何一侧的分组都等于替用户扩大了那一侧的可见范围。

    Args:
        owner_tenant_ids: 该工作区所绑定频道各自的 `tenant_id`，`None` 表示归平台方

    Returns:
        分组键：`PLATFORM_GROUP`（平台方自有，含尚未绑定任何频道的工作区）、
        `t{tenant_id}`（单一租户）或 `SHARED_GROUP`（归属不唯一）
    """
    owners = set(owner_tenant_ids)
    if not owners or owners == {None}:
        return PLATFORM_GROUP
    if len(owners) == 1:
        return f"t{next(iter(owners))}"
    return SHARED_GROUP


def normalize_group_key(group: str) -> str:
    """把分组键收敛到可安全嵌进网络名、网桥名与规则标记的字符集

    分组键最终会出现在 Docker 网络名和 iptables `--comment` 里，越界字符会让创建或安装直接失败。
    调用方给出的键理论上都由 `workspace_group_key` 生成，这里是防御性收口。
    """
    return _GROUP_SAFE_PATTERN.sub("", group.strip().lower())[:16]


def workspace_network_name(instance_name: str, group: str = PLATFORM_GROUP) -> str:
    """CC 工作区专用网络名

    与 docker-compose 中 `${INSTANCE_NAME}nekro_network` 保持同一命名习惯，
    同宿主多实例部署时各自拥有独立网络与独立子网。

    刻意不以 `nekro_network` 结尾：`services/workspace/container.py` 与
    `services/console/container_manager.py` 都用 `endswith("nekro_network")` 去认 NA 的业务网络，
    撞上这个后缀会让「自动探测 NA 所在网络」随机认到工作区网络上。

    平台方分组沿用改造前的名字，存量部署升级后仍命中同一张网络，不会凭空多出一张空网络。
    """
    suffix = normalize_group_key(group)
    return f"{instance_name}nekro_cc_net" + (f"_{suffix}" if suffix else "")


def workspace_bridge_name(instance_name: str, group: str = PLATFORM_GROUP) -> str:
    """工作区网络对应的宿主网桥接口名

    显式指定而不依赖 Docker 自动生成的 `br-<id12>`：规则以接口名为匹配条件，
    名字必须在「网络被删掉重建」之后依然可预测。

    受 IFNAMSIZ 限制，长度必须小于 16，因此分组键也参与哈希而不是直接拼接。
    平台方分组保持改造前的算法，存量网络的接口名不变。
    """
    suffix = normalize_group_key(group)
    if not suffix:
        if not instance_name:
            return "nekro-cc0"
        return "nkcc-" + hashlib.sha256(instance_name.encode("utf-8")).hexdigest()[:8]
    digest = hashlib.sha256(f"{instance_name}\x00{suffix}".encode("utf-8")).hexdigest()
    return "nkcc-" + digest[:9]


def workspace_isolation_marker(instance_name: str, group: str = PLATFORM_GROUP) -> str:
    """工作区隔离规则的标记

    前缀与沙盒不同：两套规则并存于同一批链上，清理时必须只清自己的那一份。
    分组也必须进标记：安装某个分组的规则前会按标记清掉同标记旧规则，
    标记不区分分组的话，起一个租户的工作区就会把别的租户的规则全部删掉。
    """
    suffix = normalize_group_key(group)
    if not suffix:
        return format_isolation_marker(WORKSPACE_MARKER_PREFIX, instance_name)
    return format_isolation_marker(WORKSPACE_MARKER_PREFIX, f"{instance_name or 'default'}.{suffix}")


# =============================================================================
# 规则集生成（纯函数）
# =============================================================================


@dataclass(frozen=True)
class WorkspaceNetworkSpec:
    """生成规则集所需的全部输入

    Attributes:
        bridge_interface: 工作区网络在宿主上的网桥接口名
        ipv4_subnet: 工作区网络的 IPv4 子网，取不到时为 None（此时只按接口匹配）
        controller_addresses: NA 自身在该网桥上的 IPv4 地址。NA 与工作区容器共用这个网桥，
            它的包同样是 `-i <bridge>`，不放行会让 NA 连不上自己刚起的工作区
        api_port: 工作区容器监听的 HTTP 端口，NA 的放行例外收窄到这个端口
        relay_port: 容器回连 NA 模型中继时的目的端口。为 0 表示不开「容器 -> NA」的口子
            （`NEKRO_CC_CREDENTIAL_MODE=direct`，真实 Key 直接进容器，容器不需要回连）
        host_relay_addresses: NA **跑在宿主上**时，容器回连它要用的宿主地址（本网桥网关等）。
            NA 在容器里时为空 —— 那时中继走网桥内部直连，发往宿主的方向仍然一条例外都没有
        marker: 规则标记
        extra_blocked_ipv4: 运维额外声明要切断的 IPv4 目的地（典型用途是宿主对外的公网 IP）
        extra_blocked_ipv6: 同上的 IPv6 部分
    """

    bridge_interface: str
    ipv4_subnet: Optional[str]
    controller_addresses: Tuple[str, ...]
    api_port: int
    marker: str
    relay_port: int = 0
    host_relay_addresses: Tuple[str, ...] = ()
    extra_blocked_ipv4: Tuple[str, ...] = ()
    extra_blocked_ipv6: Tuple[str, ...] = ()


def build_workspace_isolation_rules(
    spec: WorkspaceNetworkSpec,
    family: AddressFamily,
    forward_chain: str,
) -> List[IptablesRule]:
    """生成某个地址族下的完整规则集，顺序即安装顺序

    ACCEPT 例外一律排在 DROP 之前，否则例外不生效。
    与沙盒规则集的结构性差异：多了「NA -> 容器」的放行；「容器 -> NA」只放行模型中继那一个端口，
    而且是**发往宿主的方向完全没有**例外（沙盒的 RPC 回调走的正是那个方向）。

    Args:
        spec: 网络与例外信息
        family: 地址族
        forward_chain: 转发方向规则落地的链（DOCKER-USER 或 FORWARD）

    Returns:
        按安装顺序排列的规则列表

    Raises:
        WorkspaceNetworkIsolationError: 中继开着（`relay_port` 非 0）却一个可放行的地址都没有。
            与沙盒守卫拒绝生成「只有兜底拒绝、没有 RPC 回调放行」的规则集是同一条不变式：
            这一组的两端都是默认拒绝（转发方向的 RFC1918 DROP + 发往宿主的兜底 DROP），
            没有放行就等于**保证**容器连不上模型中继，而且瘫得毫无线索——
            DROP 不回 RST，容器里那次请求会一直等到自己超时。装上去比装不上更糟。
    """
    if spec.relay_port and not spec.controller_addresses and not spec.host_relay_addresses:
        # 触发路径：NA 在容器里时 `_attach_controller` 保证拿得到地址（拿不到就抛错、隔离直接判失败），
        # 但 NA 跑在宿主上时 `host_relay_addresses` 取自网络网关，网关读不出来就会是空元组，
        # 于是「中继开着、却一条放行都没有」这套必瘫的规则会被安安静静装上去
        raise WorkspaceNetworkIsolationError(
            f"拒绝生成「中继端口 {spec.relay_port} 开着、却没有任何放行地址」的工作区隔离规则集："
            "容器回连模型中继的包会落进兜底拒绝，CC 工作区的每一次模型调用都会静默等到超时。"
            "请检查工作区专用网络的网关地址是否可读（NA 跑在宿主上时中继只能经它回连）",
        )

    scope: List[str] = ["-i", spec.bridge_interface]

    def make(chain: str, match: Sequence[str], target: str) -> IptablesRule:
        return IptablesRule(
            chain=chain,
            args=(*scope, *match, "-m", "comment", "--comment", spec.marker, "-j", target),
        )

    # 容器自身的流量额外按源网段收窄；NA 的例外不能带这个约束，它要按自己的地址匹配
    guarded: List[str] = ["-s", spec.ipv4_subnet] if family == "ipv4" and spec.ipv4_subnet else []
    # 例外只在 IPv4 侧生成：NA 在该网络上的地址由 Docker 的 IPAM 分配，默认只有 IPv4
    controllers = spec.controller_addresses if family == "ipv4" else ()

    rules: List[IptablesRule] = []

    # --- 转发方向：黑名单，公网走原有路径 ---
    for address in controllers:
        # NA -> 工作区容器。两者同处一个网桥，这个包是 `-i br -o br`，
        # 目的地就落在本网段（RFC1918），不先放行就会被下面的 DROP 吃掉，
        # 表现为「工作区容器起来了但健康检查永远超时」。
        #
        # 收窄到「目的地在本网段的工作区 API 端口」而不是笼统地放行 NA 的源地址：
        # Linux 网桥不做源地址校验，工作区里的代码可以伪造成 NA 的地址来套用这条例外。
        # 伪造者收不到回包（回包发往真正的 NA），但仍能对内网做单向盲发。
        # 把例外钉死在「本网段 + 工作区端口」上，伪造能换来的最大权限就只剩
        # 「访问兄弟工作区的 API」——而那本来就要过工作区自己的 Bearer 鉴权。
        match = ["-s", address]
        if spec.ipv4_subnet:
            match += ["-d", spec.ipv4_subnet]
        rules.append(make(forward_chain, [*match, "-p", "tcp", "--dport", str(spec.api_port)], "ACCEPT"))
    for address in controllers if spec.relay_port else ():
        # 工作区容器 -> NA 的模型 API 中继（`routers/cc_relay.py`）。
        # 这是本规则集里**唯一**一条容器发起方向的放行，代价换来的是平台方的模型 Key
        # 不再进容器（见 `services/workspace/cc_credential.py`）。
        #
        # 按**目的地**匹配而不是源地址：网桥不做源地址校验，按源放行的例外可以被容器伪造，
        # 而目的地是 NA 自己在本网桥上的地址加它的监听端口，伪造源地址换不到任何额外权限。
        # 中继端点本身按工作区专属令牌鉴权，且只放行 `/v1/messages` 那两条路径。
        rules.append(
            make(forward_chain, [*guarded, "-d", address, "-p", "tcp", "--dport", str(spec.relay_port)], "ACCEPT"),
        )
    rules.append(make(forward_chain, [*guarded, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"], "ACCEPT"))
    for protocol in ("udp", "tcp"):
        # 云上部署（AWS VPC 的 x.x.0.2、GCP 的 169.254.169.254）的解析器就落在被封的网段里，
        # 不放行 53 会让工作区连域名都解析不了：git clone、pip/npm 装依赖、调用 LLM API 全部不可用
        rules.append(make(forward_chain, [*guarded, "-p", protocol, "--dport", str(DNS_PORT)], "ACCEPT"))
    blocked = BLOCKED_IPV4_CIDRS if family == "ipv4" else BLOCKED_IPV6_CIDRS
    for cidr in blocked:
        rules.append(make(forward_chain, [*guarded, "-d", cidr], "DROP"))
    # 运维声明的额外目的地（典型是宿主对外的公网 IP，见沙盒守卫 docstring 的「边界」一节）
    for cidr in spec.extra_blocked_ipv4 if family == "ipv4" else spec.extra_blocked_ipv6:
        rules.append(make(forward_chain, [*guarded, "-d", cidr], "DROP"))

    # --- 发往宿主自身：默认拒绝 ---
    # 这里不能用黑名单：宿主自己的地址完全可能是公网 IP，黑名单会整段失效。
    #
    # 与沙盒的关键差异：这一组里**没有任何按源地址放行的例外**。沙盒必须经宿主网关回连 NA 的
    # RPC 端点，工作区的模型中继（NA 在容器里时）走网桥内部直连 NA 的容器地址，从不发往宿主；
    # NA 自己也只在本网桥上访问工作区容器。所以这里是彻底的默认拒绝，比沙盒更紧，
    # 也不给伪造源地址留下通往宿主的口子。
    #
    # 唯一的例外是「NA 跑在宿主上」的部署：那时中继没有容器地址可连，只能走本网桥的网关，
    # 例外按**目的地**收窄到那一个地址加一个端口（`host_relay_addresses`，见下）。
    # 注意这种部署确实会走到这里 —— `prepare_workspace_network` 只在 `RUN_IN_DOCKER` 时
    # 调 `_attach_controller`，NA 在宿主上时它压根不被调用，隔离照样装得上。
    rules.append(make(INPUT_CHAIN, [*guarded, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"], "ACCEPT"))
    if family == "ipv4":
        for address in spec.host_relay_addresses if spec.relay_port else ():
            # NA 跑在宿主上的部署：中继端点没有容器地址可连，只能走本网桥的网关。
            # 这是「发往宿主」方向唯一的例外，收窄到一个地址加一个端口，
            # 且该端点按工作区专属令牌鉴权、只放行 `/v1/messages` 两条路径。
            # NA 在容器里时 `host_relay_addresses` 为空，这一组一条都不生成。
            rules.append(
                make(INPUT_CHAIN, [*guarded, "-d", address, "-p", "tcp", "--dport", str(spec.relay_port)], "ACCEPT"),
            )
    if family == "ipv6":
        # NDP/ICMPv6 被拦掉的话，容器连网关都找不到，公网 IPv6 出网会一并失效
        rules.append(make(INPUT_CHAIN, [*guarded, "-p", "ipv6-icmp"], "ACCEPT"))
    for protocol in ("udp", "tcp"):
        rules.append(make(INPUT_CHAIN, [*guarded, "-p", protocol, "--dport", str(DNS_PORT)], "ACCEPT"))
    rules.append(make(INPUT_CHAIN, guarded, "DROP"))

    return rules


# =============================================================================
# 网络与规则的实际安装
# =============================================================================


@dataclass(frozen=True)
class WorkspaceNetwork:
    """工作区专用网络的关键属性

    Attributes:
        name: 网络名，直接用作容器的 NetworkMode
        bridge_interface: 宿主网桥接口名
        ipv4_subnet: IPv4 子网
        ipv4_gateway: IPv4 网关（即宿主在该网桥上的地址）
    """

    name: str
    bridge_interface: str
    ipv4_subnet: Optional[str]
    ipv4_gateway: Optional[str] = None


@dataclass(frozen=True)
class WorkspaceNetworkPlan:
    """工作区容器创建时要用的网络参数

    Attributes:
        network_mode: 容器 NetworkMode；为空表示沿用 Docker 默认网络（非容器化部署）
        isolated: 隔离规则是否确实生效
        controller_address: NA 自身在这张网络上的 IPv4 地址，容器据此回连模型中继；
            NA 不在容器里、或未接入该网络时为空串
        host_address: 该网络的网关地址（= 宿主在这张网桥上的地址）。NA 跑在宿主上时
            容器只能经它回连中继，两者都取不到时中继地址退回 `host.docker.internal`
    """

    network_mode: str
    isolated: bool
    controller_address: str = ""
    host_address: str = ""


_install_lock = asyncio.Lock()
# 各分组当前已生效规则对应的输入快照，键为分组键。
# 必须按分组分别记账：共用一份状态的话，起一个租户的工作区会把另一个租户的「已装好」结论顶掉，
# 于是每来一个不同分组的容器就全量重装一次规则
_applied_signatures: Dict[str, Tuple] = {}
# 与上面那份签名对应的规则集输入，供后台回读自检重新生成「应该长什么样」。
# 与沙盒守卫同理：标记是**宿主全局**的，共用同一标记的进程会先按标记全删再装它自己那套，
# 于是「我装过」与「它还在」是两回事（沙盒侧已经因此出过一次生产事故，
# 详见 `services/sandbox/network_guard.py` 模块 docstring）
_applied_specs: Dict[str, WorkspaceNetworkSpec] = {}
_last_attempt_at: Dict[str, float] = {}
_last_failure_reason: Dict[str, str] = {}


def reset_isolation_state() -> None:
    """清空安装状态缓存（供测试与配置变更后强制重装使用）"""
    markers = [spec.marker for spec in _applied_specs.values()]
    _applied_signatures.clear()
    _applied_specs.clear()
    _last_attempt_at.clear()
    _last_failure_reason.clear()
    # 只清本守卫这些标记的回读时钟：调度表与沙盒守卫共用，全清会顺带让它多做一次无谓的核对
    for marker in markers:
        reset_rule_verification(marker)


async def resolve_workspace_network_group(workspace: "DBWorkspace") -> str:
    """按工作区绑定的频道推出它该落在哪个网络分组

    工作区表本身没有租户列，归属只能从「它服务哪些频道」推出来：
    频道带 `instance_key`，实例行上才有 `tenant_id`。

    读不到归属时（查询失败、频道行缺失）返回平台方分组：这是既有行为，
    不会把一个工作区错误地塞进某个租户的网络里。

    Args:
        workspace: 工作区记录

    Returns:
        `workspace_group_key` 给出的分组键
    """
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance
    from nekro_agent.models.db_chat_channel import DBChatChannel

    try:
        instance_keys = await DBChatChannel.filter(workspace_id=workspace.id).values_list("instance_key", flat=True)
    except Exception as e:
        logger.warning(f"读取工作区 {workspace.id} 的频道绑定失败，按平台方分组处理: {e}")
        return PLATFORM_GROUP
    if not instance_keys:
        return PLATFORM_GROUP

    named = sorted({str(key) for key in instance_keys if key})
    tenant_by_key: Dict[str, Optional[int]] = {}
    if named:
        try:
            rows = await DBAdapterInstance.filter(instance_key__in=named).values("instance_key", "tenant_id")
        except Exception as e:
            logger.warning(f"读取工作区 {workspace.id} 所属实例的租户归属失败，按平台方分组处理: {e}")
            return PLATFORM_GROUP
        tenant_by_key = {str(row["instance_key"]): row["tenant_id"] for row in rows}

    # 空 instance_key 是多账号化之前尚未被认领的历史频道，查不到实例行的同理，两者都按平台方计入
    return workspace_group_key([tenant_by_key.get(str(key)) if key else None for key in instance_keys])


async def _ensure_network(docker: aiodocker.Docker, group: str) -> WorkspaceNetwork:
    """确保某个分组的工作区专用网络存在并返回其属性"""
    instance_name = deployment_scope()
    name = workspace_network_name(instance_name, group)
    bridge_name = workspace_bridge_name(instance_name, group)

    try:
        detail = await (await docker.networks.get(name)).show()
    except Exception:
        await docker.networks.create(
            {
                "Name": name,
                "Driver": "bridge",
                "CheckDuplicate": True,
                "Labels": {
                    "nekro.workspace.network": instance_name or "default",
                    "nekro.workspace.group": normalize_group_key(group) or "platform",
                },
                "Options": {"com.docker.network.bridge.name": bridge_name},
            },
        )
        detail = await (await docker.networks.get(name)).show()

    options = detail.get("Options") or {}
    interface = str(options.get("com.docker.network.bridge.name") or "").strip()
    if not interface:
        # 早于本特性创建的网络没有固定网桥名，退回 Docker 的默认命名
        interface = f"br-{str(detail.get('Id', ''))[:12]}"

    subnet: Optional[str] = None
    gateway: Optional[str] = None
    for entry in (detail.get("IPAM") or {}).get("Config") or []:
        candidate_subnet = str(entry.get("Subnet") or "")
        if ":" in candidate_subnet:
            continue
        subnet = candidate_subnet or None
        gateway = str(entry.get("Gateway") or "") or None
        break

    return WorkspaceNetwork(
        name=name,
        bridge_interface=interface,
        ipv4_subnet=subnet,
        ipv4_gateway=gateway,
    )


def extract_network_ipv4(container_info: Dict, network_name: str) -> str:
    """从容器 inspect 结果里取出它在指定网络上的 IPv4 地址

    Args:
        container_info: `docker inspect` 结果
        network_name: 网络名

    Returns:
        IPv4 地址；未接入该网络或尚未分配地址时为空串
    """
    networks = ((container_info.get("NetworkSettings") or {}).get("Networks") or {})
    entry = networks.get(network_name) or {}
    return str(entry.get("IPAddress") or "").strip()


async def _attach_controller(docker: aiodocker.Docker, network_name: str) -> str:
    """把 NA 自己接入工作区网络，返回它在该网络上的 IPv4 地址

    NA 通过容器名寻址工作区（`DBWorkspace.api_endpoint`），而 Docker 内嵌 DNS 只解析
    「与自己同处一个 user-defined 网络」的容器名。不接入就等于把工作区功能整个打瘫。

    Raises:
        WorkspaceNetworkIsolationError: 认不出自己、接不进去或拿不到地址
    """
    container = await resolve_self_container(docker)
    if container is None:
        raise WorkspaceNetworkIsolationError("未能识别 NA 自身容器，无法将其接入工作区专用网络")

    info = await container.show()
    address = extract_network_ipv4(info, network_name)
    if address:
        return address

    try:
        network = await docker.networks.get(network_name)
        await network.connect({"Container": container.id})
    except Exception as e:
        raise WorkspaceNetworkIsolationError(f"将 NA 接入工作区专用网络失败: {e}") from e

    address = extract_network_ipv4(await container.show(), network_name)
    if not address:
        raise WorkspaceNetworkIsolationError("NA 已接入工作区专用网络但未取得 IPv4 地址")
    return address


async def prepare_workspace_network(
    docker: aiodocker.Docker,
    group: str = PLATFORM_GROUP,
) -> WorkspaceNetworkPlan:
    """准备某个分组的工作区容器网络参数，必要时安装隔离规则

    每次创建工作区容器前调用。规则安装结果按「分组 + 输入签名」缓存，配置或网络属性变化时
    自动重装；上一次失败时按 `_RETRY_INTERVAL_SECONDS` 节流重试。

    分组之间彼此独立：一个分组装不上规则，不影响另一个分组已经生效的隔离。

    Args:
        docker: Docker 客户端
        group: 网络分组键，见 `workspace_group_key`

    Returns:
        容器创建时使用的网络参数

    Raises:
        WorkspaceNetworkIsolationError: `strict` 档位下隔离未生效
    """
    mode: IsolationMode = config.CC_WORKSPACE_NETWORK_ISOLATION
    if mode == "off":
        return await _legacy_plan(docker)

    group = normalize_group_key(group)
    try:
        network = await _ensure_network(docker, group)
    except Exception as e:
        message = f"创建工作区专用网络失败（分组 {group or 'platform'}）: {e}"
        if mode == "strict":
            raise WorkspaceNetworkIsolationError(message) from e
        logger.warning(
            f"{message}；工作区网络隔离未生效，容器可访问内网。"
            "若是地址池耗尽（Docker 默认只能再切出约 30 张 bridge 网络），"
            "请放大 /etc/docker/daemon.json 的 default-address-pools",
        )
        return await _legacy_plan(docker)

    marker = workspace_isolation_marker(deployment_scope(), group)
    extra_ipv4, extra_ipv6 = split_extra_blocked_cidrs(config.NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS)
    relaying = credential_mode() == "relay"
    # NA 在容器里 -> 容器走网桥内部直连它，端口是 NA 进程 bind 的那个，发往宿主的方向不开口子；
    # NA 在宿主上 -> 只能连本网桥的网关，端口是宿主上监听的那个，必须额外放行 INPUT 的一条
    relay_port = (relay_listen_port() if OsEnv.RUN_IN_DOCKER else relay_host_port()) if relaying else 0
    host_relay_addresses: Tuple[str, ...] = (
        (network.ipv4_gateway,) if relaying and not OsEnv.RUN_IN_DOCKER and network.ipv4_gateway else ()
    )
    # 缓存键覆盖所有会改变规则集的输入：改了工作区端口而不重装规则，NA 就再也连不上新工作区
    cache_key = (
        network.bridge_interface,
        network.ipv4_subnet,
        config.CC_SANDBOX_INTERNAL_PORT,
        marker,
        relay_port,
        host_relay_addresses,
        extra_ipv4,
        extra_ipv6,
    )

    controller_address = ""
    async with _install_lock:
        if _applied_signatures.get(group) != cache_key:
            now = time.monotonic()
            # 曾经装成功过说明是配置/网络变了，立即重装；从未成功过则按节流间隔重试
            attempted_at = _last_attempt_at.get(group, 0.0)
            if group in _applied_signatures or now - attempted_at >= _RETRY_INTERVAL_SECONDS:
                _last_attempt_at[group] = now
                try:
                    # NA 跑在宿主上时不需要接入该网络：它走的是容器发布到 127.0.0.1 的端口，
                    # 那条路径由宿主本机路由完成，不经过本守卫匹配的网桥入口
                    controllers = (await _attach_controller(docker, network.name),) if OsEnv.RUN_IN_DOCKER else ()
                    spec = WorkspaceNetworkSpec(
                        bridge_interface=network.bridge_interface,
                        ipv4_subnet=network.ipv4_subnet,
                        controller_addresses=controllers,
                        api_port=config.CC_SANDBOX_INTERNAL_PORT,
                        marker=marker,
                        relay_port=relay_port,
                        host_relay_addresses=host_relay_addresses,
                        extra_blocked_ipv4=extra_ipv4,
                        extra_blocked_ipv6=extra_ipv6,
                    )
                    executor = await resolve_executor(docker)
                    if executor is None:
                        raise WorkspaceNetworkIsolationError("当前宿主上没有可用的 iptables 执行通道")
                    await install_marked_rules(executor, marker, partial(build_workspace_isolation_rules, spec))
                except NetworkIsolationError as e:
                    _applied_signatures.pop(group, None)
                    _applied_specs.pop(group, None)
                    _last_failure_reason[group] = str(e)
                except Exception as e:
                    _applied_signatures.pop(group, None)
                    _applied_specs.pop(group, None)
                    _last_failure_reason[group] = f"{e}"
                else:
                    _applied_signatures[group] = cache_key
                    _applied_specs[group] = spec
                    _last_failure_reason[group] = ""
                    mark_rules_verified(marker)
                    logger.info(
                        f"CC 工作区网络隔离已生效（分组 {group or 'platform'}）: 网络 {network.name} "
                        f"(网桥 {spec.bridge_interface}, 子网 {spec.ipv4_subnet})，"
                        f"放行 NA 自身地址 {list(spec.controller_addresses)}",
                    )

        isolated = _applied_signatures.get(group) == cache_key
        if isolated and OsEnv.RUN_IN_DOCKER:
            # 规则命中缓存时上面那段不会跑，而中继地址每次创建容器都要拿到，
            # 所以在这里补一次。`_attach_controller` 对已接入的网络是一次 inspect，不重复连接。
            try:
                controller_address = await _attach_controller(docker, network.name)
            except Exception as e:
                logger.warning(f"读取 NA 在工作区网络 {network.name} 上的地址失败，模型中继将退回宿主网关: {e}")

    if not isolated:
        reason = _last_failure_reason.get(group, "")
        if mode == "strict":
            raise WorkspaceNetworkIsolationError(
                f"CC 工作区网络隔离未生效（分组 {group or 'platform'}，{reason}），"
                "CC_WORKSPACE_NETWORK_ISOLATION=strict 下拒绝启动工作区容器",
            )
        logger.warning(
            f"CC 工作区网络隔离未生效（分组 {group or 'platform'}），容器可访问内网（NA 的兄弟容器、RFC1918、"
            f"云元数据服务 169.254.169.254 等）: {reason}。"
            "请检查宿主的 iptables 权限；若确认不需要该防护，"
            "可将 CC_WORKSPACE_NETWORK_ISOLATION 设为 off 以消除此告警",
        )
        return await _legacy_plan(docker)

    applied_spec = _applied_specs.get(group)
    if applied_spec is not None:
        # 「装过」不等于「还在」，理由与沙盒守卫相同；核对放后台，不给工作区启动增加延迟
        schedule_rule_verification(
            marker,
            partial(_verify_applied_workspace_rules, group, applied_spec, cache_key),
        )

    return WorkspaceNetworkPlan(
        network_mode=network.name,
        isolated=True,
        controller_address=controller_address,
        host_address=network.ipv4_gateway or "",
    )


async def _verify_applied_workspace_rules(group: str, spec: WorkspaceNetworkSpec, signature: Tuple) -> None:
    """后台核对：内核里带本分组标记的规则是否仍是我们装的那套

    对不上就作废该分组的缓存并打 ERROR，下一次创建工作区容器时强制重装。

    Args:
        group: 网络分组键
        spec: 上一次成功安装时用的规则集输入
        signature: 与之对应的缓存签名，只有它仍然当值时才作废
    """
    docker = aiodocker.Docker()
    try:
        executor = await resolve_executor(docker)
        if executor is None:
            return
        difference = await verify_marked_rules(executor, spec.marker, partial(build_workspace_isolation_rules, spec))
    finally:
        await docker.close()

    if not difference:
        return

    async with _install_lock:
        if _applied_signatures.get(group) != signature:
            return
        _applied_signatures.pop(group, None)
        _applied_specs.pop(group, None)
        # 失败重试的节流一并清掉：它算的是「上一次尝试安装」的时间，而上一次是**成功**的，
        # 不清的话下一次创建工作区会被节流挡住不重装，白白多降级一分钟
        _last_attempt_at.pop(group, None)
        _last_failure_reason[group] = difference

    logger.error(
        f"CC 工作区隔离规则已被外部改写（分组 {group or 'platform'}，{difference}）：标记 `{spec.marker}` 是"
        "**宿主全局**的，任何按同一标记先全删再装的进程都会顶掉本部署的规则。"
        "下一次创建工作区容器时将强制重装。作用域现在默认由数据目录派生（两套部署不会同名），"
        "所以反复出现只可能是：两套部署显式设了相同的 NEKRO_INSTANCE_NAME、"
        "两套部署共用了同一个 NEKRO_DATA_DIR、或者某个验证脚本沿用了本部署的作用域",
    )


async def _legacy_plan(docker: aiodocker.Docker) -> WorkspaceNetworkPlan:
    """改造前的网络形态：容器直接挂在 NA 自己的业务网络上

    这条路径上没有任何本模块的规则，容器与宿主之间本来就通，所以中继只需要一个能连上的地址：
    取容器实际所在网络的网关（NA 在宿主上时它就是宿主），拿不到就让 `host.docker.internal` 兜底。
    """
    from nekro_agent.services.workspace.container import resolve_nekro_network

    network_mode = await resolve_nekro_network(docker) if OsEnv.RUN_IN_DOCKER else ""
    return WorkspaceNetworkPlan(
        network_mode=network_mode,
        isolated=False,
        host_address=await _network_gateway(docker, network_mode or "bridge"),
    )


async def resolve_container_relay_addresses(docker: aiodocker.Docker, container_name: str) -> Tuple[str, str]:
    """读出一个**已经在跑**的工作区容器实际所在网络上的中继落点

    重启不重建容器，它留在自己创建时那张网络上。按当前频道绑定重新算一次分组可能得到
    另一张网（改过绑定就会），把 NA 在那张网上的地址写进 `settings.json` 等于把中继
    指到一个这个容器根本连不到的地方，所以这里按容器 inspect 的结果反查。

    拿不到就返回空串：调用方会沿用 `metadata` 里存着的旧地址，而不是把它覆盖成猜的。

    Args:
        docker: Docker 客户端
        container_name: 工作区容器名

    Returns:
        `(NA 在该网络上的 IPv4, 该网络的网关地址)`，读不到的那一项为空串
    """
    try:
        info = await (await docker.containers.get(container_name)).show()
    except Exception as e:
        logger.warning(f"读取工作区容器 {container_name} 的网络失败，中继地址沿用旧值: {e}")
        return "", ""

    names = [name for name in ((info.get("NetworkSettings") or {}).get("Networks") or {}) if name]
    if not names:
        return "", ""
    network_name = names[0]

    controller_address = ""
    if OsEnv.RUN_IN_DOCKER:
        # 只读不接：`_attach_controller` 在读不到地址时会把 NA **连进**那张网络，
        # 而这里的网络名是从容器 inspect 反查来的，可能是 Docker 默认网桥。
        # 重启一个工作区不该有「把 NA 接进另一张网」这种副作用。
        try:
            container = await resolve_self_container(docker)
            controller_address = extract_network_ipv4(await container.show(), network_name) if container else ""
        except Exception as e:
            logger.warning(f"读取 NA 在网络 {network_name} 上的地址失败，中继将退回网关地址: {e}")
    return controller_address, await _network_gateway(docker, network_name)


async def _network_gateway(docker: aiodocker.Docker, network_name: str) -> str:
    """读某张 Docker 网络的 IPv4 网关地址，读不到返回空串"""
    try:
        detail = await (await docker.networks.get(network_name)).show()
    except Exception as e:
        logger.debug(f"读取网络 {network_name} 的网关失败: {e}")
        return ""
    for entry in (detail.get("IPAM") or {}).get("Config") or []:
        gateway = str(entry.get("Gateway") or "")
        if gateway and ":" not in gateway:
            return gateway
    return ""


async def setup_workspace_network_isolation() -> None:
    """启动时预先建好网络并装上规则

    放在启动阶段是为了让「装不上」这件事在启动日志里就暴露出来，而不是等到第一次创建工作区。

    除平台方分组外，还要把**存量工作区所属的分组**一并布防：规则在 NA 退出时刻意不移除，
    但网络被重建后子网可能变化，而那些分组的容器可能已经在跑了。等到「下次创建工作区」
    才重装，等于让已经在跑的租户工作区裸奔一整个运行周期。

    这里吞掉异常：`strict` 的 fail closed 由 `prepare_workspace_network` 在真正创建容器时执行，
    启动阶段直接崩掉整个应用不是合适的失败方式。
    """
    if config.CC_WORKSPACE_NETWORK_ISOLATION == "off":
        logger.info("CC_WORKSPACE_NETWORK_ISOLATION=off，跳过工作区网络隔离（工作区容器可访问宿主内网）")
        return

    scope = deployment_scope()
    # 与沙盒侧同理：这一行是把 iptables 标记对回某一套部署的唯一依据
    logger.info(
        f"CC 工作区网络隔离作用域 {describe_deployment_scope()}；"
        f"平台方分组的规则标记 {workspace_isolation_marker(scope)}，"
        f"专用网络 {workspace_network_name(scope)}（网桥 {workspace_bridge_name(scope)}）",
    )

    groups = [PLATFORM_GROUP]
    try:
        from nekro_agent.models.db_workspace import DBWorkspace

        for workspace in await DBWorkspace.all():
            group = await resolve_workspace_network_group(workspace)
            if group not in groups:
                groups.append(group)
    except Exception as e:
        logger.warning(f"枚举存量工作区的网络分组失败，仅布防平台方分组: {e}")

    docker = aiodocker.Docker()
    try:
        for group in groups:
            try:
                await prepare_workspace_network(docker, group)
            except NetworkIsolationError as e:
                logger.warning(f"{e}")
        # 每个分组各有自己的标记，旧标记的残留要按分组各报一条（只报告，不删）；
        # 但**整批一次交进去**：孤儿规则按设计不会被删掉，这条检查每次启动都要跑一遍，
        # 而 NA 在容器里时每条 iptables 命令都要起一个临时容器 —— 按分组各查一次的话，
        # 启动开销会随租户分组数线性膨胀。链的内容与标记无关，读一次就够数清全部分组
        await report_legacy_marker_orphans(
            docker,
            [
                LegacyMarkerCheck(
                    guard=f"CC 工作区网络隔离（分组 {normalize_group_key(group) or 'platform'}）",
                    legacy_marker=workspace_isolation_marker(LEGACY_SCOPE, group),
                    current_marker=workspace_isolation_marker(scope, group),
                    legacy_network=workspace_network_name(LEGACY_SCOPE, group),
                )
                for group in groups
            ],
        )
    except Exception as e:
        logger.warning(f"初始化工作区网络隔离失败: {e}")
    finally:
        await docker.close()
