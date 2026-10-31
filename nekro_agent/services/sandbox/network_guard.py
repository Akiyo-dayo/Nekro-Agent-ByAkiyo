"""沙盒容器的网络隔离守卫

## 威胁模型

沙盒里执行的是模型生成的代码，而模型的输入由用户完全控制，因此必须假设沙盒里的代码是敌对的。
`credential_guard` 已经把「容器窃取平台凭据」这条路封死，但容器的**出网能力**此前是完全敞开的：

- 扫宿主所在的 RFC1918 内网，横向打其他业务系统；
- 打云厂商元数据服务 `169.254.169.254`，拿宿主的 IAM 临时凭据（危害最高的一条）；
- 直连同宿主上的其他容器（Postgres、Qdrant、协议端）以及宿主自身监听的端口。

## 处置方式：保留公网，切断内网

沙盒的正当用途包含 `pip install` 拉依赖、模型生成的代码抓网页/调公开 API，全封会直接毁掉产品功能。
真正的风险面是内网横向，所以这里只切断内网/链路本地，其余目的地放行。

具体做法：

1. 沙盒容器挂到一个**专用 user-defined bridge 网络**上，不与任何业务容器共网；
2. 在宿主的包过滤规则里，按「来自沙盒网桥」这一条件下规则。

### 为什么要同时管 DOCKER-USER 和 INPUT

`DOCKER-USER` 挂在 `FORWARD` 上，只看得到**转发**流量：容器 -> 局域网其他主机、容器 -> 元数据服务、
容器 -> 其他网桥上的容器。但容器发往**宿主自身某个 IP** 的包不走 FORWARD，走的是 `INPUT`。
只做 DOCKER-USER 等于把宿主在这些地址上监听的一切（Postgres、Qdrant、NA 自己的管理接口、SSH）
留在门外没关。所以 INPUT 上另有一组规则，且那组规则是**默认拒绝**而非黑名单——
黑名单要求穷举宿主的全部本机地址，漏一个就整段失效。

## 边界：这道防线覆盖到哪里、覆盖不到哪里

**覆盖**：目的地是宿主**本机持有**的任一地址的流量。这类包从沙盒网桥进来后由宿主本机接收，
在 `INPUT` 上命中 `-i <沙盒网桥>` 的默认拒绝。网桥网关、宿主的内网网卡地址、回环、
局域网内其他地址全在此列（实测：`10.0.0.2:8021`、`<网桥网关>:8021` 均被 INPUT 兜底 DROP 计中）。

**覆盖不到**：云上 1:1 NAT / 弹性 IP 拓扑里宿主**对外的公网 IP**。这类地址并不落在宿主的
任何本地网卡上，不属于上面“宿主本机持有的地址”，因此本模块的默认规则不匹配它；
需要切断的部署按下面的方式自行声明。这条缺口的实际增量有限：走公网 IP 的流量沿途受
云安全组 / 防火墙的同一套入站策略约束。

**为什么不自动探测公网 IP 并加规则**：宿主本机拿不到这个信息（无本地地址、`ip route` 只给网关、
裸 KVM 上没有云元数据服务），只能问外部回显服务。而外部回显给出的是**出向** IP，未必等于
入向 IP；地址还可能有多个、可能是 IPv6、可能随时被重新绑定。猜错的代价是往一台**无关的互联网
主机**下 DROP，比缺口本身更糟。因此默认不猜。

**需要堵上的部署自己声明**：`config.NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS` 里填宿主对外的
公网 IP / 网段，这些目的地会追加到转发方向的 DROP 组里（排在所有 ACCEPT 例外之后，
因此不会打断沙盒回连 NA）。实测声明后同样的探测被计中并阻断，且不影响其他公网目的地。

### 刻意留的三个口子

- **NA 自身的 RPC 端点**：沙盒必须能回调 `SANDBOX_CHAT_API_URL`，否则整个沙盒功能瘫痪。
  这条 ACCEPT 排在所有 DROP 之前，并且按 conntrack 记录的**原始目的端口**放行——
  标准 docker-compose 部署里 NA 也在容器中、端口是发布出来的，回调包会先被 DNAT 改写成
  NA 容器的 172.x 地址，只按目的地址放行会让它落进 RFC1918 的 DROP 里（已实测复现）。
- **DNS（udp/tcp 53）**：云上部署（AWS VPC 的 `x.x.0.2`、GCP 的 `169.254.169.254`）的解析器
  就落在被封的网段里。不放行 53 会让所有部署的沙盒连域名都解析不了，pip 直接不可用。
  代价是沙盒可以查询内网 DNS 做名字枚举——相对于「每个云上部署一升级就坏」，这个代价可接受。
- **conntrack ESTABLISHED,RELATED**：只放行「宿主/其他容器先发起」的连接的回包。
  沙盒无法凭空造出这种状态（它的 SYN 会被丢），所以不构成绕过。

被拒的其余目的地不写显式 ACCEPT，而是让规则 RETURN 回原链：显式 ACCEPT 会越过管理员在
DOCKER-USER 后面配置的其他防火墙规则，不该由我们替他做这个决定。

## 失败行为

见 `config.SANDBOX_NETWORK_ISOLATION`：默认 `best_effort`（装不上就响亮告警但继续），
另有 `strict`（装不上拒绝启动沙盒）与 `off`（完全不介入）。

## 规则不在关闭时移除

NA 崩溃退出时不会走关闭流程，若把「退出即撤规则」写进设计，崩溃后防线就悄悄没了，
而这恰恰是最需要防线的时刻。因此规则**保留**，重复堆积的问题由「安装前先按标记清理」解决，
而不是靠退出时清理。

## 标记是宿主全局的，因此「装过」不等于「还在」

`install_marked_rules` 的语义是「先按标记全删、再装自己那套」，而标记只带一个作用域名。
于是**任何一个共用该标记的进程**——同宿主上的第二套部署、一份跑测试的 checkout、
临时改了 `SANDBOX_CHAT_API_URL` 的验证脚本——都会把本进程装好的放行规则整组删掉，
再换上它自己那套（端口很可能不同）。

作用域现在取自 `core/os_env.py:deployment_scope()`：显式设了 `NEKRO_INSTANCE_NAME` 就用它，
**没设时从数据目录派生一个稳定后缀**（`myfork-3f2a91c4-`），而不再一律回落到 `default`。
两套部署不可能共用数据目录，所以「两套部署撞同一个标记」在结构上不再可能发生
——这条路以前只靠「运维记得给其中一套设变量」，而那正是本模块下面那场事故的成因。
代价：从旧版本升级时标记会变一次，旧的 `nekro-sandbox:default` 规则成为孤儿
（它们只匹配旧网桥 `nekro-sbx0`，不影响新规则），处理办法见 `docs/multi-deploy-same-host.md`。

这已经在生产上真实发生过一次：一个把回调端口改到探针端口的验证脚本跑完之后，
链上只剩「放行探针端口 + 兜底 DROP」，NA 真正的回调端口落进兜底 DROP。
DROP 不回 RST，容器里那次 `requests.post` 于是**永远等下去**，
表现是「每条消息的沙盒都恰好卡到 `SANDBOX_RUNNING_TIMEOUT` 才结束」，
而 NA 侧一条错误日志都没有——因为从 NA 的角度看，什么都没发生过。

所以安装状态**不能只记在内存里**：`prepare_sandbox_network` 走缓存快路径时，
会在后台按 TTL 把内核里带本标记的规则读回来与本次生成的规则集比对
（`verify_marked_rules`），对不上就打 ERROR 并作废缓存，下一次跑沙盒强制重装。
核对放在后台是因为 NA 跑在容器里时每一次 iptables 调用都要起一个临时容器，
放在关键路径上会给每次沙盒启动加上数秒延迟。

配套的两条：

- 规则集里**必须**有 RPC 回调放行，否则拒绝安装（见 `build_isolation_rules`）。
  「只有兜底 DROP 没有放行」是一套**保证**把沙盒打瘫的规则，装上去比装不上更糟；
- 隔离没装上时回落到默认网桥（`LEGACY_PLAN`）而不是继续把容器挂到隔离网络上。
  链上可能还残留着别人装的 DROP（规则本来就不在关闭时移除），
  这时把容器挂上去等于把它送进黑洞——`best_effort` 的结果会比 `off` 更糟。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import (
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    List,
    Literal,
    Optional,
    Sequence,
    Set,
    Tuple,
)
from urllib.parse import urlparse

import aiodocker

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import (
    OsEnv,
    deployment_scope,
    describe_deployment_scope,
)

logger = get_sub_logger("sandbox_network")

# 升级前（作用域一律回落到 `default` 的版本）留下的标记。
# 只用来**报告**孤儿规则，绝不自动删除——见 `report_legacy_marker_orphans`
LEGACY_SCOPE = ""

IsolationMode = Literal["best_effort", "strict", "off"]
AddressFamily = Literal["ipv4", "ipv6"]

# 被切断的 IPv4 目的地。169.254/16 是云元数据服务所在段，属于这里最关键的一条
BLOCKED_IPV4_CIDRS: Tuple[str, ...] = (
    "10.0.0.0/8",  # RFC1918 私网 A 段
    "172.16.0.0/12",  # RFC1918 私网 B 段（Docker 默认网段也落在这里）
    "192.168.0.0/16",  # RFC1918 私网 C 段
    "169.254.0.0/16",  # 链路本地：云厂商元数据服务
    "127.0.0.0/8",  # 回环
    "0.0.0.0/8",  # 本网络保留段
    "100.64.0.0/10",  # CGNAT
)

# 被切断的 IPv6 目的地。只做 IPv4 等于留了一扇没关的门
BLOCKED_IPV6_CIDRS: Tuple[str, ...] = (
    "fc00::/7",  # ULA
    "fe80::/10",  # 链路本地
    "::1/128",  # 回环
)

# 沙盒回调 NA 时使用的主机名。容器里的 `SANDBOX_CHAT_API_URL` 默认指向它
HOST_ALIAS_NAMES = frozenset({"host.docker.internal", "localhost", "127.0.0.1", "::1"})

DNS_PORT = 53

# 规则优先落在 DOCKER-USER（Docker 官方保留给管理员、且在 Docker 重启后不会被覆盖的位置）；
# 该链不存在时退回 FORWARD 链首
FORWARD_CHAIN_CANDIDATES: Tuple[str, ...] = ("DOCKER-USER", "FORWARD")
INPUT_CHAIN = "INPUT"

_IPTABLES_BINARIES: Dict[str, str] = {"ipv4": "iptables", "ipv6": "ip6tables"}

# 安装失败后的重试节流：strict 模式每次运行沙盒都会重试，没有节流会把 iptables 调用打成风暴
_RETRY_INTERVAL_SECONDS = 60.0

_MARKER_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_.:-]")


class NetworkIsolationError(RuntimeError):
    """容器网络隔离未能生效，且当前配置要求 fail closed

    本模块的执行通道、规则安装与清理被 CC 工作区守卫（`services/workspace/network_guard.py`）
    共用，因此错误类型也做成共用的基类，两边的调用方可以只捕获一种异常。
    """


class SandboxNetworkIsolationError(NetworkIsolationError):
    """沙盒网络隔离未能生效"""


# =============================================================================
# 命名与标记（纯函数，规则集与清理逻辑的单测都基于这里）
# =============================================================================


def sandbox_network_name(instance_name: str) -> str:
    """沙盒专用网络名

    与 docker-compose 中 `${INSTANCE_NAME}nekro_network` 保持同一命名习惯，
    同宿主多实例部署时各自拥有独立网络与独立子网。
    """
    return f"{instance_name}nekro_sandbox_net"


def sandbox_bridge_name(instance_name: str) -> str:
    """沙盒网络对应的宿主网桥接口名

    显式指定而不依赖 Docker 自动生成的 `br-<id12>`：规则以接口名为匹配条件，
    名字必须在「网络被删掉重建」之后依然可预测。

    受 IFNAMSIZ 限制，长度必须小于 16。
    """
    if not instance_name:
        return "nekro-sbx0"
    return "nksbx-" + hashlib.sha256(instance_name.encode("utf-8")).hexdigest()[:8]


def format_isolation_marker(prefix: str, instance_name: str) -> str:
    """规则上的固定标记

    安装前按该标记清理同实例的旧规则，从而做到「NA 重启不堆积」；
    带上实例名是为了同宿主多实例互不误删，带上前缀是为了让同一实例下的
    沙盒守卫与 CC 工作区守卫互不误删——两者的规则并存于同一批链上。

    Args:
        prefix: 守卫种类前缀，例如 `nekro-sandbox`
        instance_name: 实例名

    Returns:
        可安全用作 iptables `--comment` 的标记
    """
    return f"{prefix}:" + (_MARKER_SAFE_PATTERN.sub("_", instance_name) or "default")


def isolation_marker(instance_name: str) -> str:
    """沙盒隔离规则的标记"""
    return format_isolation_marker("nekro-sandbox", instance_name)


def split_extra_blocked_cidrs(declared: Sequence[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """把运维声明的「额外切断目的地」按地址族拆开并规范化

    这里承载的是本模块唯一堵不上的那类目的地——宿主经云网关发夹回来的公网 IP
    （见模块 docstring 的「边界」一节）。它必须由运维显式声明：宿主本机拿不到自己的公网 IP，
    自动猜测猜错就等于往一台无关的互联网主机下 DROP。

    单个地址会被规范成 `/32`（IPv6 为 `/128`）的网段形式，这样生成的规则文本与 iptables
    读回来的文本一致，按标记清理时能原样匹配上。非法条目直接跳过并告警，不让一条笔误
    把整套规则的安装打断。

    Args:
        declared: 配置里声明的地址或网段字符串

    Returns:
        (IPv4 网段, IPv6 网段)，均已去重且保持声明顺序
    """
    ipv4: List[str] = []
    ipv6: List[str] = []
    for raw in declared:
        text = raw.strip()
        if not text:
            continue
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            logger.warning(f"NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS 中的 {text!r} 不是合法的 IP 或网段，已忽略")
            continue
        bucket = ipv4 if network.version == 4 else ipv6
        value = str(network)
        if value not in bucket:
            bucket.append(value)
    return tuple(ipv4), tuple(ipv6)


# =============================================================================
# 规则集生成（纯函数）
# =============================================================================


@dataclass(frozen=True)
class SandboxNetworkSpec:
    """生成规则集所需的全部输入

    Attributes:
        bridge_interface: 沙盒网络在宿主上的网桥接口名
        ipv4_subnet: 沙盒网络的 IPv4 子网，取不到时为 None（此时只按接口匹配）
        rpc_endpoints: 允许沙盒回连的 NA RPC 端点 (IPv4 地址, TCP 端口)
        marker: 规则标记
        extra_blocked_ipv4: 运维额外声明要切断的 IPv4 目的地（典型用途是宿主对外的公网 IP）
        extra_blocked_ipv6: 同上的 IPv6 部分
    """

    bridge_interface: str
    ipv4_subnet: Optional[str]
    rpc_endpoints: Tuple[Tuple[str, int], ...]
    marker: str
    extra_blocked_ipv4: Tuple[str, ...] = ()
    extra_blocked_ipv6: Tuple[str, ...] = ()


@dataclass(frozen=True)
class IptablesRule:
    """一条待安装的规则

    Attributes:
        chain: 目标链
        args: 规则体（不含 `-I <chain> <index>`）
    """

    chain: str
    args: Tuple[str, ...]


def build_isolation_rules(
    spec: SandboxNetworkSpec,
    family: AddressFamily,
    forward_chain: str,
) -> List[IptablesRule]:
    """生成某个地址族下的完整规则集，顺序即安装顺序

    ACCEPT 例外一律排在 DROP 之前，否则例外不生效。

    Args:
        spec: 网络与例外信息
        family: 地址族
        forward_chain: 转发方向规则落地的链（DOCKER-USER 或 FORWARD）

    Returns:
        按安装顺序排列的规则列表

    Raises:
        SandboxNetworkIsolationError: `spec.rpc_endpoints` 为空。这套规则集的 INPUT 尾部是
            **兜底拒绝**，没有 RPC 放行就等于「保证把沙盒打瘫」，而且是最难查的那种瘫法：
            DROP 不回 RST，容器里的回调会一直等到沙盒运行超时，NA 侧一条错误日志都没有。
            装上去比装不上更糟，所以在这里就拒绝生成，让调用方按档位告警或 fail closed。
    """
    if not spec.rpc_endpoints:
        raise SandboxNetworkIsolationError(
            "拒绝生成「只有兜底拒绝、没有 RPC 回调放行」的沙盒隔离规则集："
            "沙盒必须能回连 NA 的 SANDBOX_CHAT_API_URL，装上这样一套规则会让每一次沙盒执行都静默等到超时。"
            "请检查 SANDBOX_CHAT_API_URL 是否指向**宿主上可解析、可达**的地址",
        )

    scope: List[str] = ["-i", spec.bridge_interface]
    if family == "ipv4" and spec.ipv4_subnet:
        scope += ["-s", spec.ipv4_subnet]

    def make(chain: str, match: Sequence[str], target: str) -> IptablesRule:
        return IptablesRule(
            chain=chain,
            args=(*scope, *match, "-m", "comment", "--comment", spec.marker, "-j", target),
        )

    rpc_ports = sorted({port for _, port in spec.rpc_endpoints})
    rules: List[IptablesRule] = []

    # --- 转发方向：黑名单，公网走原有路径 ---
    rules.append(make(forward_chain, ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"], "ACCEPT"))
    for port in rpc_ports:
        # 按**连接建立时的原始目的端口**放行，而不是按包上此刻的目的地址。
        # 标准 docker-compose 部署里 NA 自己也在容器里、端口是发布出来的，
        # 沙盒发往「宿主:8021」的包会先在 nat PREROUTING 被 DNAT 改写成 NA 容器的 172.x 地址，
        # 等它到达 DOCKER-USER 时已经是一个「目的地在私网」的转发包，
        # 会被下面的 RFC1918 规则丢掉——整个沙盒功能就此瘫痪（实测确认过）。
        # conntrack 记的是 DNAT 前的原始五元组，因此这条例外对两种部署形态都成立。
        rules.append(make(forward_chain, ["-p", "tcp", "-m", "conntrack", "--ctorigdstport", str(port)], "ACCEPT"))
    if family == "ipv4":
        for address, port in spec.rpc_endpoints:
            # 直连形态（回调指向某个内网反向代理）下按目的地址放行，不经过 DNAT 也能命中
            rules.append(make(forward_chain, ["-d", address, "-p", "tcp", "--dport", str(port)], "ACCEPT"))
    for protocol in ("udp", "tcp"):
        rules.append(make(forward_chain, ["-p", protocol, "--dport", str(DNS_PORT)], "ACCEPT"))
    blocked = BLOCKED_IPV4_CIDRS if family == "ipv4" else BLOCKED_IPV6_CIDRS
    for cidr in blocked:
        rules.append(make(forward_chain, ["-d", cidr], "DROP"))
    # 运维声明的额外目的地排在内建黑名单之后、所有 ACCEPT 例外之前的分界线之下：
    # 声明的往往是宿主自己的公网 IP，而 NA 的回调端点可能正好挂在同一个地址上，
    # 放在 ACCEPT 例外之后才不会把沙盒回连 NA 一起切掉
    for cidr in spec.extra_blocked_ipv4 if family == "ipv4" else spec.extra_blocked_ipv6:
        rules.append(make(forward_chain, ["-d", cidr], "DROP"))

    # --- 发往宿主自身：默认拒绝 ---
    # 这里不能用黑名单：宿主自己的地址完全可能是公网 IP，黑名单会整段失效。
    rules.append(make(INPUT_CHAIN, ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"], "ACCEPT"))
    if family == "ipv6":
        # NDP/ICMPv6 被拦掉的话，容器连网关都找不到，公网 IPv6 出网会一并失效
        rules.append(make(INPUT_CHAIN, ["-p", "ipv6-icmp"], "ACCEPT"))
    for port in rpc_ports:
        # 只认端口不认地址：发往 INPUT 的包按定义都终结在宿主上，而该端口上跑的就是 NA 自己。
        # 这样即使 `host.docker.internal` 被 Docker 解析到了另一个宿主地址，回调也不会被误伤
        # ——沙盒功能被隔离规则打瘫是这里最需要防的回归。
        rules.append(make(INPUT_CHAIN, ["-p", "tcp", "--dport", str(port)], "ACCEPT"))
    for protocol in ("udp", "tcp"):
        rules.append(make(INPUT_CHAIN, ["-p", protocol, "--dport", str(DNS_PORT)], "ACCEPT"))
    rules.append(make(INPUT_CHAIN, [], "DROP"))

    return rules


def build_install_commands(rules: Sequence[IptablesRule]) -> List[List[str]]:
    """把规则集转成 iptables 参数

    用 `-I <chain> <index>` 而非 `-A`：链尾可能已经有其他工具下的放行规则，
    追加在后面等于规则不生效。逐条递增序号则保证组内顺序与 `build_isolation_rules` 一致。

    Args:
        rules: 按安装顺序排列的规则

    Returns:
        iptables 参数列表
    """
    positions: Dict[str, int] = {}
    commands: List[List[str]] = []
    for rule in rules:
        index = positions.get(rule.chain, 0) + 1
        positions[rule.chain] = index
        commands.append(["-I", rule.chain, str(index), *rule.args])
    return commands


def rule_comment_value(tokens: Sequence[str]) -> str:
    """取出一条规则上 `-m comment --comment` 的取值

    **判据是「`--comment` 的取值等于标记」而不是「标记出现在某个 token 上」。**
    后者会把一条**别人的**规则算成我们的——只要它任意一个位置上的 token 恰好等于我们的标记
    （`--log-prefix`、某个自定义链名、另一个工具的注释），按标记清理就会连它一起删掉。
    更要命的是盘点侧（`extract_rule_marker`，只认注释取值）与删除侧的口径会不一致：
    预览显示 N 条、执行删掉 N+1 条，而多删的那一条谁都不会注意到。

    Args:
        tokens: `shlex.split` 之后的规则 token

    Returns:
        注释取值；没有注释时为空串
    """
    args = list(tokens)
    for position, token in enumerate(args):
        if token == "--comment" and position + 1 < len(args):
            return args[position + 1]
    return ""


def build_cleanup_commands(chain: str, iptables_save_output: str, marker: str) -> List[List[str]]:
    """从 `iptables -S <chain>` 的输出里解析出需要删除的旧规则

    按标记删除而不是按行号：行号会随每次删除而移动，按行号删是经典的删错规则来源。
    标记按 `--comment` 的**取值**精确匹配，见 `rule_comment_value`。

    Args:
        chain: 链名
        iptables_save_output: `iptables -S <chain>` 的原始输出
        marker: 规则标记

    Returns:
        iptables 删除参数列表
    """
    commands: List[List[str]] = []
    for raw_line in iptables_save_output.splitlines():
        line = raw_line.strip()
        if not line.startswith("-A "):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if len(tokens) < 3 or tokens[1] != chain:
            continue
        if rule_comment_value(tokens) != marker:
            continue
        commands.append(["-D", chain, *tokens[2:]])
    return commands


def build_indexed_cleanup_commands(chain: str, iptables_save_output: str, marker: str) -> List[List[str]]:
    """按**行号**生成删除命令，作为按原文删除失败时的兜底

    `build_cleanup_commands` 把 `iptables -S` 的输出原样重放成 `-D`，前提是这段文本能完整
    还原规则。跨版本读取时这个前提会破：iptables-nft 1.8.9 起把 `-p tcp --dport N` 这类隐式
    端口匹配编码成原生 nft 表达式，旧版本（如 1.8.7）读回来时渲染不出 `--dport`，
    照这段残缺文本发出的 `-D` 匹配不到任何规则，于是**旧规则一条都删不掉、每次重启都再叠一套**。
    过期的 ACCEPT 例外（尤其是按 NA 旧 IP 放行的那条）留在链上是有安全后果的，所以必须兜底。

    行号删除不依赖规则文本，只依赖「这一行带着我们的标记」——而 `--comment` 是真实的 xt 匹配，
    各版本都渲染得出来。从大到小删则保证同一份快照内的行号不会因前面的删除而位移。

    Args:
        chain: 链名
        iptables_save_output: `iptables -S <chain>` 的原始输出
        marker: 规则标记

    Returns:
        按行号从大到小排列的 iptables 删除参数列表
    """
    prefix = f"-A {chain} "
    positions: List[int] = []
    position = 0
    for raw_line in iptables_save_output.splitlines():
        line = raw_line.strip()
        if not line.startswith(prefix):
            continue
        # 无论这一行能不能解析都要占一个行号，否则后面的行号会整体偏移，变成删错规则
        position += 1
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if rule_comment_value(tokens) == marker:
            positions.append(position)
    return [["-D", chain, str(position)] for position in reversed(positions)]


# 摘要里参与比对的端口匹配项。`--dport` 是隐式/显式端口匹配，`--ctorigdstport` 是 DNAT 前的原始端口
_PORT_FLAGS: Tuple[str, ...] = ("--dport", "--ctorigdstport")


@dataclass(frozen=True)
class MarkedRuleSummary:
    """某条链上「带本守卫标记」的规则的可比摘要

    比对**不能**按整行文本严格相等做：`iptables -S` 的输出跨版本会缺匹配项
    （1.8.9 起隐式端口匹配被编码成原生 nft 表达式，旧版本读回来渲染不出 `--dport`，
    详见 `build_indexed_cleanup_commands`），严格比对会得出「每次都不一致」，
    于是每次跑沙盒都重装一遍规则——比不比对还糟。

    所以只比三样各版本都稳的东西：条数、动作、以及端口取值集合。
    实际发生过的故障（别的进程按同一标记把放行端口换成了它自己的探针端口）在
    「端口集合」上一定露馅；而「规则被整组删掉」在「条数」上一定露馅。

    Attributes:
        count: 带标记的规则条数
        targets: 各规则的 target，排序后的多重集（顺序不参与比对：
            别的工具在链首插一条会让顺序变化，但那不是本函数要防的东西）
        ports: 规则里出现过的端口取值集合
    """

    count: int
    targets: Tuple[str, ...]
    ports: FrozenSet[str]


def _summarize_rule_tokens(rules: Sequence[Sequence[str]]) -> MarkedRuleSummary:
    targets: List[str] = []
    ports: Set[str] = set()
    for tokens in rules:
        args = list(tokens)
        targets.append(args[args.index("-j") + 1] if "-j" in args and args.index("-j") + 1 < len(args) else "")
        for position, token in enumerate(args):
            if token in _PORT_FLAGS and position + 1 < len(args):
                ports.add(args[position + 1])
    return MarkedRuleSummary(count=len(targets), targets=tuple(sorted(targets)), ports=frozenset(ports))


def summarize_marked_rules(chain: str, iptables_save_output: str, marker: str) -> MarkedRuleSummary:
    """从 `iptables -S <chain>` 的输出里摘出带标记的规则

    Args:
        chain: 链名
        iptables_save_output: `iptables -S <chain>` 的原始输出
        marker: 规则标记

    Returns:
        该链上带标记规则的摘要
    """
    prefix = f"-A {chain} "
    collected: List[List[str]] = []
    for raw_line in iptables_save_output.splitlines():
        line = raw_line.strip()
        if not line.startswith(prefix):
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if rule_comment_value(tokens) == marker:
            collected.append(tokens)
    return _summarize_rule_tokens(collected)


def summarize_expected_rules(chain: str, rules: Sequence[IptablesRule]) -> MarkedRuleSummary:
    """把本次生成的规则集摘成同一形状，用于与内核读回来的结果比对

    Args:
        chain: 链名
        rules: `build_isolation_rules` 生成的规则（含其他链的，会按链过滤）

    Returns:
        该链上应有规则的摘要
    """
    return _summarize_rule_tokens([list(rule.args) for rule in rules if rule.chain == chain])


def diff_marked_rules(expected: MarkedRuleSummary, actual: MarkedRuleSummary) -> str:
    """比对期望与实际，返回人能看懂的差异描述

    Args:
        expected: 本次生成的规则集摘要
        actual: 内核里读回来的摘要

    Returns:
        差异描述；一致时为空串
    """
    if expected.count and not actual.count:
        return f"链上已经没有本守卫的规则了（期望 {expected.count} 条，实际 0 条）"
    if expected.count != actual.count:
        return f"规则条数不符（期望 {expected.count} 条，实际 {actual.count} 条）"
    if expected.targets != actual.targets:
        return f"规则动作不符（期望 {list(expected.targets)}，实际 {list(actual.targets)}）"
    # 端口只查「多出来的」，不查「少掉的」。
    #
    # 少掉的可能只是渲染不出来：老版本 iptables 读不出隐式端口匹配（`-p tcp --dport N`），
    # 但同一条链上显式匹配模块的端口（`-m conntrack --ctorigdstport N`）照样渲染得出来
    # ——于是实际侧会是期望侧的一个**真子集**，按「集合相等」判会每次都报不一致，
    # 退化成「每跑一次沙盒重装一次规则」，比不核对还糟。
    #
    # 多出来的则不可能是渲染造成的：没有哪个版本会凭空渲染出一个不存在的端口。
    # 而这正是事故的形态——别人按同一标记装了一套指向**它自己**端口的规则。
    unexpected = actual.ports - expected.ports
    if unexpected:
        return f"出现了不该有的放行端口 {sorted(unexpected)}（本次规则集用的是 {sorted(expected.ports)}）"
    return ""


def resolve_rpc_endpoints(api_url: str, host_addresses: Sequence[str]) -> Tuple[Tuple[str, int], ...]:
    """解析出沙盒回连 NA 时的目的地址与端口

    `host.docker.internal` 在宿主上不可解析，它实际指向的是宿主在某个网桥上的地址，
    因此这类别名直接展开成调用方给出的候选宿主地址。

    Args:
        api_url: `SANDBOX_CHAT_API_URL`
        host_addresses: 候选宿主地址（沙盒网络网关、默认网桥网关等）

    Returns:
        去重后的 (IPv4 地址, TCP 端口) 列表；无法解析时为空
    """
    parsed = urlparse(api_url.strip())
    hostname = (parsed.hostname or "").strip()
    if not hostname:
        return ()

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return ()

    if hostname.lower() in HOST_ALIAS_NAMES:
        candidates = list(host_addresses)
    else:
        candidates = _resolve_ipv4_addresses(hostname)
        # 反向代理/别名指回宿主的场景下解析结果未必落在网桥地址上，把候选宿主地址一并放行，
        # 代价只是多开同一个端口，收益是回调不会因为解析差异被打断
        candidates += list(host_addresses)

    endpoints: List[Tuple[str, int]] = []
    for candidate in candidates:
        try:
            ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if (candidate, port) not in endpoints:
            endpoints.append((candidate, port))
    return tuple(endpoints)


def _resolve_ipv4_addresses(hostname: str) -> List[str]:
    try:
        ipaddress.IPv4Address(hostname)
    except ValueError:
        pass
    else:
        return [hostname]
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
    except OSError:
        return []
    return list({str(info[4][0]) for info in infos})


# =============================================================================
# iptables 执行通道
# =============================================================================


class IptablesExecutor:
    """iptables 命令的执行通道"""

    description: str = "unknown"

    async def run(self, family: AddressFamily, args: Sequence[str]) -> Tuple[int, str]:
        """执行一条 iptables 命令

        Args:
            family: 地址族，决定用 iptables 还是 ip6tables
            args: 命令参数

        Returns:
            (退出码, 合并后的输出)
        """
        raise NotImplementedError


class LocalIptablesExecutor(IptablesExecutor):
    """直接在本进程所在的网络命名空间执行

    仅当 NA 跑在宿主上时才可用：NA 若在容器里，本地 iptables 改的是容器自己的命名空间，
    对宿主毫无作用。
    """

    description = "本机 iptables"

    async def run(self, family: AddressFamily, args: Sequence[str]) -> Tuple[int, str]:
        command = [_IPTABLES_BINARIES[family], "-w", "5", *args]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return 1, f"{e}"
        return completed.returncode, f"{completed.stdout}{completed.stderr}".strip()


class HelperContainerIptablesExecutor(IptablesExecutor):
    """借一个宿主网络命名空间的临时容器执行

    NA 自身跑在容器里时唯一可行的路径：临时容器用 `--network=host` 进入宿主网络命名空间，
    并只加 `NET_ADMIN`/`NET_RAW` 两项能力。

    沙盒容器**绝不能**拿到 NET_ADMIN——那样容器里的代码可以自己把规则删掉——
    所以能力只授予这个由 NA 完全控制命令行的临时容器。
    """

    description = "宿主网络命名空间临时容器"

    def __init__(self, docker: aiodocker.Docker, image: str) -> None:
        self._docker = docker
        self._image = image

    async def run(self, family: AddressFamily, args: Sequence[str]) -> Tuple[int, str]:
        command = [_IPTABLES_BINARIES[family], "-w", "5", *args]
        container = None
        try:
            container = await self._docker.containers.run(
                name=f"nekro-netguard-{os.urandom(6).hex()}",
                config={
                    "Image": self._image,
                    "Cmd": command,
                    "Entrypoint": [],
                    # 正常路径下容器在 finally 里被删掉；打标签是为了 NA 被 kill -9 打断时，
                    # 运维能一条命令捞出残留：docker ps -aq -f label=nekro.netguard=1
                    "Labels": {"nekro.netguard": "1"},
                    "HostConfig": {
                        "NetworkMode": "host",
                        "CapAdd": ["NET_ADMIN", "NET_RAW"],
                        # 不用 AutoRemove：需要在容器退出后读退出码
                        "AutoRemove": False,
                    },
                },
            )
            await asyncio.wait_for(container.wait(), timeout=60)
            outputs = await container.log(stdout=True, stderr=True)
            info = await container.show()
            code = int((info.get("State") or {}).get("ExitCode", 1))
            return code, "".join(outputs).strip()
        except Exception as e:
            return 1, f"{e}"
        finally:
            if container is not None:
                try:
                    await container.delete(force=True)
                except Exception as e:
                    logger.debug(f"回收 iptables 临时容器失败: {e}")


def parse_self_container_ids(hostname: str, cgroup_text: str, mountinfo_text: str) -> List[str]:
    """从进程自身的痕迹里推断出「我在哪个容器里」的候选标识

    三条线索都不可靠，所以三条都用：

    - 容器主机名默认就是短 ID，但部署方可以用 `hostname:` 覆盖掉；
    - `/proc/self/cgroup` 在 cgroup v1 下带完整 ID，在 cgroup v2 下常常只有 `0::/`，一点信息都没有；
    - `/proc/self/mountinfo` 里 Docker 为 `/etc/hostname`、`/etc/hosts`、`/etc/resolv.conf`
      做的 bind mount 源路径必然包含完整容器 ID，是三者里最稳的一条。

    Args:
        hostname: `socket.gethostname()` 的结果
        cgroup_text: `/proc/self/cgroup` 内容
        mountinfo_text: `/proc/self/mountinfo` 内容

    Returns:
        去重后的候选标识，按可信度排序
    """
    candidates: List[str] = []

    def add(value: str) -> None:
        cleaned = value.strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    for match in re.finditer(r"/containers/([0-9a-f]{64})/", mountinfo_text):
        add(match.group(1))
    add(hostname)
    for match in re.finditer(r"([0-9a-f]{64})", cgroup_text):
        add(match.group(1))
    return candidates


def _read_proc_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


async def resolve_self_container(docker: aiodocker.Docker):
    """拿到「NA 自己」这个容器对象

    沙盒侧用它取镜像来起 iptables 临时容器，CC 工作区侧用它把 NA 接入工作区专用网络，
    两处都必须先认出自己。认不出时返回 None，由调用方按配置档位决定告警还是拒绝。

    Args:
        docker: Docker 客户端

    Returns:
        NA 自身的容器对象；认不出时为 None
    """
    candidates = parse_self_container_ids(
        socket.gethostname(),
        _read_proc_file("/proc/self/cgroup"),
        _read_proc_file("/proc/self/mountinfo"),
    )
    for candidate in candidates:
        try:
            container = await docker.containers.get(candidate)
            await container.show()
        except Exception:
            continue
        return container
    return None


async def _resolve_self_image(docker: aiodocker.Docker) -> Optional[str]:
    """解析 NA 自身容器所用的镜像

    临时容器需要一个带 iptables 的镜像。NA 自己的镜像一定在本地（进程正跑在里面），
    且 NA 镜像装了 docker.io / iptables，是唯一无需额外拉取就能保证可用的选择。
    """
    container = await resolve_self_container(docker)
    if container is None:
        return None
    try:
        info = await container.show()
    except Exception:
        return None
    return (info.get("Image") or "").strip() or (info.get("Config") or {}).get("Image", "").strip() or None


async def resolve_executor(docker: aiodocker.Docker) -> Optional[IptablesExecutor]:
    """探测出一条可用的 iptables 执行通道

    探测而非假设：宿主可能不是 Linux（开发机常见）、可能没有权限、可能压根没装 iptables。
    任何一环缺失都返回 None，由调用方按配置档位决定告警还是拒绝启动。

    Args:
        docker: Docker 客户端

    Returns:
        可用的执行通道；不可用时为 None
    """
    try:
        info = await docker.system.info()
    except Exception as e:
        logger.warning(f"读取 Docker 守护进程信息失败，无法判断宿主类型: {e}")
        return None

    os_type = str(info.get("OSType", "")).lower()
    if os_type and os_type != "linux":
        logger.info(f"Docker 宿主为 {os_type}，容器网络隔离依赖 Linux 包过滤，跳过（不影响容器运行）")
        return None

    executor: Optional[IptablesExecutor]
    if OsEnv.RUN_IN_DOCKER:
        image = await _resolve_self_image(docker)
        if not image:
            logger.warning("未能解析 NA 自身容器镜像，无法在宿主网络命名空间执行 iptables")
            return None
        executor = HelperContainerIptablesExecutor(docker, image)
    elif sys.platform == "linux" and shutil.which(_IPTABLES_BINARIES["ipv4"]):
        executor = LocalIptablesExecutor()
    else:
        logger.info("当前宿主没有可用的 iptables 执行通道，跳过容器网络隔离（不影响容器运行）")
        return None

    code, output = await executor.run("ipv4", ["-S", "FORWARD"])
    if code != 0:
        logger.warning(f"iptables 探测失败（{executor.description}）: {output}")
        return None
    return executor


# =============================================================================
# 网络与规则的实际安装
# =============================================================================


@dataclass(frozen=True)
class SandboxNetwork:
    """沙盒专用网络的关键属性

    Attributes:
        name: 网络名，直接用作容器的 NetworkMode
        bridge_interface: 宿主网桥接口名
        ipv4_subnet: IPv4 子网
        ipv4_gateway: IPv4 网关（即宿主在该网桥上的地址）
    """

    name: str
    bridge_interface: str
    ipv4_subnet: Optional[str]
    ipv4_gateway: Optional[str]


@dataclass(frozen=True)
class SandboxNetworkPlan:
    """沙盒容器创建时要用的网络参数

    Attributes:
        network_mode: 容器 NetworkMode
        extra_hosts: 容器 ExtraHosts
        isolated: 隔离规则是否确实生效
    """

    network_mode: str
    extra_hosts: Tuple[str, ...]
    isolated: bool


LEGACY_PLAN = SandboxNetworkPlan(
    network_mode="bridge",
    extra_hosts=("host.docker.internal:host-gateway",),
    isolated=False,
)

_install_lock = asyncio.Lock()
# 当前已生效规则对应的输入快照。沙盒可以并发运行，安装动作必须串行且只在输入变化时重做
_applied_signature: Optional[Tuple] = None
# 与上面那份签名对应的规则集输入。后台回读自检要重新生成一遍「应该长什么样」，
# 而重算 spec 需要一次阻塞 DNS 解析加一次 Docker 调用 —— 那正是签名缓存想省掉的开销
_applied_spec: Optional[SandboxNetworkSpec] = None
_last_attempt_at: float = 0.0
_last_failure_reason: str = ""


def reset_isolation_state() -> None:
    """清空安装状态缓存（供测试与配置变更后强制重装使用）"""
    global _applied_signature, _applied_spec, _last_attempt_at, _last_failure_reason
    marker = _applied_spec.marker if _applied_spec else isolation_marker(deployment_scope())
    _applied_signature = None
    _applied_spec = None
    _last_attempt_at = 0.0
    _last_failure_reason = ""
    # 只清本守卫这一个标记的回读时钟：CC 工作区守卫与本模块共用调度表，
    # 全清会顺带让它多做一次无谓的核对
    reset_rule_verification(marker)


async def _ensure_network(docker: aiodocker.Docker) -> SandboxNetwork:
    """确保沙盒专用网络存在并返回其属性

    专用网络的意义在于：沙盒与其他业务容器不同网，规则可以精确地只按这一个网桥匹配，
    也顺带切断了「沙盒直连同网段业务容器」这条最短路径。
    """
    instance_name = deployment_scope()
    name = sandbox_network_name(instance_name)
    bridge_name = sandbox_bridge_name(instance_name)

    try:
        detail = await (await docker.networks.get(name)).show()
    except Exception:
        await docker.networks.create(
            {
                "Name": name,
                "Driver": "bridge",
                "CheckDuplicate": True,
                "Labels": {"nekro.sandbox.network": instance_name or "default"},
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
    for entry in ((detail.get("IPAM") or {}).get("Config") or []):
        candidate_subnet = str(entry.get("Subnet") or "")
        if ":" in candidate_subnet:
            continue
        subnet = candidate_subnet or None
        gateway = str(entry.get("Gateway") or "") or None
        break

    return SandboxNetwork(name=name, bridge_interface=interface, ipv4_subnet=subnet, ipv4_gateway=gateway)


async def _default_bridge_gateway(docker: aiodocker.Docker) -> Optional[str]:
    """默认网桥（docker0）的网关地址

    Docker 的 `host-gateway` 特殊值默认解析到这里。即便我们把 ExtraHosts 钉死到沙盒网络的网关，
    也把它一并放行：多花一条规则，换回调不会因为 Docker 版本差异被误伤。
    """
    try:
        detail = await (await docker.networks.get("bridge")).show()
    except Exception:
        return None
    for entry in ((detail.get("IPAM") or {}).get("Config") or []):
        gateway = str(entry.get("Gateway") or "")
        if gateway and ":" not in gateway:
            return gateway
    return None


RuleBuilder = Callable[[AddressFamily, str], List[IptablesRule]]


async def purge_marked_rules_on_chain(
    executor: IptablesExecutor,
    family: AddressFamily,
    chain: str,
    marker: str,
    initial_dump: Optional[str] = None,
) -> Tuple[int, List[str]]:
    """把某条链上带某个标记的规则删干净：先按原文重放，再按行号兜底

    **这是本项目里唯一允许删除隔离规则的地方**，安装路径（`install_marked_rules` 的
    「先清后装」）与运维路径（`execute_orphan_rule_cleanup` 的孤儿清理）共用它。
    两处各写一份的风险不是多几行代码，而是**只有一份带行号兜底**——那一份会在跨 iptables
    版本时静默地一条都删不掉（详见 `build_indexed_cleanup_commands`）。

    命令一律以 **argv 数组**交给执行通道，不经过 shell：`iptables -S` 的输出里
    `--comment "nekro-sandbox:xxx"` 是**带引号**的，在 shell 里重新分词会把引号丢掉，
    规则从此匹配不上（PROJECT_STATE §2.20：手写 `sed 's/^-A /-D /'` 两条命令全部失败）。

    Args:
        executor: iptables 执行通道
        family: 地址族
        chain: 链名
        marker: 规则标记，token 精确匹配 `--comment` 的取值
        initial_dump: 已经读到手的 `iptables -S <chain>` 输出；省略时自行读一次

    Returns:
        (成功删除的条数, 失败说明列表)
    """
    failures: List[str] = []
    deleted = 0

    dump = initial_dump
    if dump is None:
        code, output = await executor.run(family, ["-S", chain])
        if code != 0:
            return 0, [f"读取 {family} 链 {chain} 失败: {output}"]
        dump = output

    for command in build_cleanup_commands(chain, dump, marker):
        delete_code, delete_output = await executor.run(family, command)
        if delete_code != 0:
            message = f"清理旧的隔离规则失败（{marker} {family} {chain}）: {delete_output}"
            logger.warning(message)
            failures.append(message)
        else:
            deleted += 1

    # 按原文重放删不掉的残留改用行号删除。跨 iptables 版本读取时规则文本会缺匹配项，
    # 不兜底的话旧规则一条都删不掉，每次重启都再叠一套（详见 build_indexed_cleanup_commands）
    code, output = await executor.run(family, ["-S", chain])
    if code != 0:
        return deleted, failures
    for command in build_indexed_cleanup_commands(chain, output, marker):
        residual_code, residual_output = await executor.run(family, command)
        if residual_code != 0:
            message = f"按行号清理残留隔离规则失败（{marker} {family} {chain}）: {residual_output}"
            logger.warning(message)
            failures.append(message)
        else:
            deleted += 1
    return deleted, failures


async def install_marked_rules(executor: IptablesExecutor, marker: str, build: RuleBuilder) -> None:
    """按标记清理旧规则后重新安装整套规则

    先清后装即幂等：NA 重启多少次，同一标记在链上的规则数量都不变。
    沙盒守卫与 CC 工作区守卫共用这条安装路径，只是各自传入不同的标记与规则集生成函数
    ——两者的差异全部落在 `build` 里，链的选择、清理、错误处理不该有第二份实现。

    Args:
        executor: iptables 执行通道
        marker: 规则标记，清理与安装都以它为界
        build: 规则集生成函数，入参为 (地址族, 转发链名)

    Raises:
        NetworkIsolationError: 任一地址族的规则未能装上
    """
    families: Tuple[AddressFamily, ...] = ("ipv4", "ipv6")
    for family in families:
        forward_chain = await pick_forward_chain(executor, family)
        if forward_chain is None:
            if family == "ipv4":
                raise NetworkIsolationError("iptables 上找不到可用的转发链（DOCKER-USER / FORWARD）")
            # IPv6 未启用时 ip6tables 可能整体不可用，此时容器也拿不到 IPv6 地址，不构成缺口
            logger.info("ip6tables 不可用，跳过 IPv6 隔离规则（该宿主上容器同样没有 IPv6 出口）")
            continue

        for chain in (forward_chain, INPUT_CHAIN):
            code, output = await executor.run(family, ["-S", chain])
            if code != 0:
                raise NetworkIsolationError(f"读取 {family} 链 {chain} 失败: {output}")
            await purge_marked_rules_on_chain(executor, family, chain, marker, initial_dump=output)

        for command in build_install_commands(build(family, forward_chain)):
            code, output = await executor.run(family, command)
            if code != 0:
                raise NetworkIsolationError(f"安装 {family} 规则失败 `{' '.join(command)}`: {output}")


async def verify_marked_rules(executor: IptablesExecutor, marker: str, build: RuleBuilder) -> str:
    """把内核里带本标记的规则读回来，与本次生成的规则集比对

    存在的理由见模块 docstring 的「标记是宿主全局的」一节：标记只带实例名，任何共用该标记的
    进程（第二套部署、跑测试的 checkout、验证脚本）都会先按标记全删再装自己那套，
    于是「我装过」与「它还在」是两回事，而两者的差别在故障现场没有任何信号。

    读不出来（链不存在、iptables 忙）时返回空串而**不是**报不一致：核对失败不该演变成
    「每次跑沙盒都重装一遍规则」。真正被改写的场景一定读得出来，读得出来就一定对不上。

    与沙盒/CC 工作区两个守卫共用，因此 `build` 与 `install_marked_rules` 的形参一致。

    Args:
        executor: iptables 执行通道
        marker: 规则标记
        build: 规则集生成函数，入参为 (地址族, 转发链名)

    Returns:
        差异描述；一致或无法判定时为空串
    """
    families: Tuple[AddressFamily, ...] = ("ipv4", "ipv6")
    for family in families:
        forward_chain = await pick_forward_chain(executor, family)
        if forward_chain is None:
            continue
        try:
            expected_rules = build(family, forward_chain)
        except NetworkIsolationError:
            # 规则集本身已经生成不出来（例如回调端点解析不到），交给下一次安装去报错
            return ""
        for chain in (forward_chain, INPUT_CHAIN):
            code, output = await executor.run(family, ["-S", chain])
            if code != 0:
                logger.debug(f"核对隔离规则时读取 {family} 链 {chain} 失败: {output}")
                continue
            difference = diff_marked_rules(
                summarize_expected_rules(chain, expected_rules),
                summarize_marked_rules(chain, output, marker),
            )
            if difference:
                return f"{family} 链 {chain}: {difference}"
    return ""


async def pick_forward_chain(executor: IptablesExecutor, family: AddressFamily) -> Optional[str]:
    """挑出该地址族上可用的转发链，优先 DOCKER-USER"""
    for chain in FORWARD_CHAIN_CANDIDATES:
        code, _ = await executor.run(family, ["-S", chain])
        if code == 0:
            return chain
    return None


async def count_marked_rules_by_marker(
    executor: IptablesExecutor,
    markers: Sequence[str],
) -> Dict[str, int]:
    """一次读链，数清**多个**标记各自还剩多少条规则，**只读不删**

    给「升级换了作用域，旧标记的规则成了孤儿」这件事提供一个可观测量。
    读不出来的链按 0 计：这个数只用来决定要不要提示运维，报少了顶多少提示一次，
    绝不能因为读失败而演变成任何删除动作。

    **为什么要能一次数多个**：CC 工作区的标记是按分组分开的（`nekro-cc:default.tN`），
    每个分组各查一次的话，链的读取次数会随租户分组数线性增长；而 NA 跑在容器里时
    每一条 iptables 命令都要起一个临时容器（见 `HelperContainerIptablesExecutor`）。
    孤儿规则按设计不会被自动删除，也就是说这笔开销**每次启动都要付一遍**，
    分组一多就是几十次容器启停堆在启动路径上。链的内容与标记无关，读一次就够了。

    Args:
        executor: iptables 执行通道
        markers: 要统计的规则标记（重复项会被合并）

    Returns:
        `{标记: 两个地址族、转发链与 INPUT 链上的总条数}`，入参里的每个标记都有一项
    """
    counts: Dict[str, int] = {marker: 0 for marker in markers}
    if not counts:
        return counts
    families: Tuple[AddressFamily, ...] = ("ipv4", "ipv6")
    for family in families:
        forward_chain = await pick_forward_chain(executor, family)
        if forward_chain is None:
            continue
        for chain in (forward_chain, INPUT_CHAIN):
            code, output = await executor.run(family, ["-S", chain])
            if code != 0:
                continue
            for marker in counts:
                counts[marker] += summarize_marked_rules(chain, output, marker).count
    return counts


async def count_marked_rules(executor: IptablesExecutor, marker: str) -> int:
    """数出链上仍带某个标记的规则条数，**只读不删**

    单标记的便捷入口。参数是 `str` 而不是序列是有意的：`str` 本身也是 `Sequence[str]`，
    两个函数用同一个名字的话，把一个标记误传给批量版会变成「按字符逐个统计」而不报错。

    Args:
        executor: iptables 执行通道
        marker: 要统计的规则标记

    Returns:
        两个地址族、转发链与 INPUT 链上带该标记的规则总条数
    """
    return (await count_marked_rules_by_marker(executor, (marker,)))[marker]


@dataclass(frozen=True)
class LegacyMarkerCheck:
    """一次「旧标记还剩多少条」的检查项

    Attributes:
        guard: 守卫名称（含分组），出现在提示语里
        legacy_marker: 升级前用的标记
        current_marker: 本次实际使用的标记
        legacy_network: 升级前那张专用网络的名字 —— 清理前必须确认它上面没有容器还在跑，
            所以提示语里要给出**具体名字**而不是一句「旧网络」
    """

    guard: str
    legacy_marker: str
    current_marker: str
    legacy_network: str


def format_legacy_orphan_hint(
    guard: str,
    legacy_marker: str,
    current_marker: str,
    legacy_network: str,
    count: int,
) -> str:
    """升级后旧标记残留规则的处置说明

    **为什么是「报告 + 手工清理」而不是启动时自动清掉**：本进程分不清链上那组
    `nekro-sandbox:default` 是「我自己升级前装的」还是「同宿主上另一套**尚未升级**的 NA
    正在用的」——两者的规则文本完全一样。自动删除会在后一种情形下亲手复现本模块要消灭的那场
    事故（把邻居的回调放行规则整组删掉，邻居从此每次沙盒执行都静默卡到超时）。
    留着的代价很小：这组规则的匹配条件全部是 `-i 旧网桥`，对挂在新网桥上的容器不生效。

    ⚠️ 提示语里必须写清**第二个**前提：旧网桥不一定真的空了。沙盒容器是一次性的，
    升级后确实不会再有容器挂上去；但 **CC 工作区容器是长命的**，网络在**创建时**定死、
    重启不换网（`services/workspace/container.py:create_and_start`）。升级前建的工作区
    还挂在旧网桥上，而这套规则里既有放行也有兜底 DROP —— 按标记整组删掉之后，
    那些容器的隔离就**静默消失**了（可以直接访问宿主内网与兄弟容器），
    没有任何报错，也不会在任何一条日志里留下痕迹。

    Args:
        guard: 守卫名称，出现在提示语里
        legacy_marker: 旧标记
        current_marker: 本次实际使用的标记
        legacy_network: 旧标记对应的那张专用网络名（前提②要拿它去查）
        count: 旧标记下还剩多少条规则

    Returns:
        可直接打进日志的一段说明，含精确的清理命令与两个前提
    """
    return (
        f"{guard}：链上还有 {count} 条带旧标记 `{legacy_marker}` 的规则。"
        f"本部署现在用的是 `{current_marker}`（作用域改为由部署身份派生，见 docs/multi-deploy-same-host.md），"
        "旧规则不会被自动删除——本进程分不清它们是本部署升级前留下的，"
        "还是同宿主上另一套**尚未升级**的 NA 正在用的，删错就等于亲手打瘫邻居的沙盒。"
        "它们只匹配旧网桥，对挂在新网桥上的容器不生效，留着即可。"
        "**要清理请走总后台「隔离规则」页（平台方专属，`/console/network-rules`）**："
        "那里会先列出将要删除的规则原文、涉及的链、以及"
        f"旧网络 `{legacy_network}` 这类网桥上还有几个容器，逐条判定通过后再回填标记确认执行；"
        "清理动作走产品自己的清理路径（token 精确匹配 `--comment` + argv 数组 + 行号兜底）。"
        "**不要手写 `iptables -S | sed 's/^-A /-D /'` 之类的命令**：`--comment` 的取值带引号，"
        "经 shell 重新分词后引号会丢掉，轻则一条都删不掉、重则半成功"
        "（而被删掉的那半可能正是还在生效的兜底 DROP）"
    )


async def report_legacy_marker_orphans(
    docker: aiodocker.Docker,
    checks: Sequence[LegacyMarkerCheck],
) -> Dict[str, int]:
    """检查并**报告**旧标记的孤儿规则；一条都不删

    作用域没变过的检查项（`current_marker == legacy_marker`）直接跳过：显式设了
    `NEKRO_INSTANCE_NAME` 的部署标记根本没动，不该被这条提示打扰。全都没变时一次
    iptables 都不执行。

    入参是**一批**而不是一条：调用方（尤其是按分组各有一个标记的 CC 工作区守卫）
    必须能把整批一次交进来，否则链的读取次数会随分组数线性增长——理由见
    `count_marked_rules_by_marker`。

    Args:
        docker: Docker 客户端
        checks: 待检查的 (守卫名, 旧标记, 新标记) 批次

    Returns:
        `{旧标记: 残留条数}`；探测不到执行通道或全部跳过时为空字典
    """
    pending = [check for check in checks if check.legacy_marker != check.current_marker]
    if not pending:
        return {}
    try:
        executor = await resolve_executor(docker)
        if executor is None:
            return {}
        counts = await count_marked_rules_by_marker(executor, [check.legacy_marker for check in pending])
    except Exception as e:
        logger.debug(f"检查旧标记 {[check.legacy_marker for check in pending]} 的残留规则失败: {e}")
        return {}
    for check in pending:
        count = counts.get(check.legacy_marker, 0)
        if count:
            logger.warning(
                format_legacy_orphan_hint(
                    check.guard,
                    check.legacy_marker,
                    check.current_marker,
                    check.legacy_network,
                    count,
                ),
            )
    return counts


# =============================================================================
# 孤儿隔离规则的运维出口：盘点 -> 判定 -> 清理
# =============================================================================
#
# `report_legacy_marker_orphans` 只会打一条 WARNING，运维拿到的是一句「自己去 iptables 上删」。
# 那条路已经出过事：手写 `iptables -S | sed 's/^-A /-D /'` 逐条重放时，`--comment "nekro-cc:x"`
# 的引号被 shell 吃掉，两条命令全部 `Bad rule`（PROJECT_STATE §2.20）。半成功的话被删掉的
# 就可能是**还在生效**的那一组。所以这里给出一个产品自己的出口：预览 -> 二次确认 -> 走
# `build_cleanup_commands` 执行，全程 argv 数组，不拼裸命令字符串。
#
# ## 「这个标记确实是孤儿」怎么判
#
# 危险有两个方向，都必须挡住：
#
# 1. **本部署自己正在用的标记** —— 按**作用域前缀**排除，而不是按「等不等于某一个标记」。
#    CC 工作区的标记按分组分开（`nekro-cc:{作用域}.t7`），分组集合会随租户增删而变；
#    枚举当前分组的话，一个「租户的工作区刚被删光、但容器还没重建」的分组就会漏出来，
#    而那组规则里的兜底 DROP 仍然在保护着它的容器。前缀判据对**将来才出现的分组**同样成立。
# 2. **同宿主上另一套仍在运行的部署的标记** —— 它同样「不等于本部署的标记」，
#    所以第 1 条完全挡不住它。这里用四条**否定性证据**联合判定，任一条不成立就拦住：
#
#    | 判据 | 为什么它安全 |
#    |---|---|
#    | 规则里 `-i` 指向的网桥上**没有任何容器**（含已停止但 `NetworkMode` 钉在这张网上的） | 这是 §3 #45 那条教训的落点：沙盒容器是一次性的，**CC 工作区容器是长命的**且网络在创建时定死、重启不换网。删掉规则里的兜底 DROP，这些容器就能直接访问宿主内网，**不报错不留日志** |
#    | 宿主上**没有任何容器**带着这个作用域的归属标签（`nekro.sandbox.instance` / `nekro.owner.scope`） | 这两个标签的取值就是 `deployment_scope()`，一个带着该值的容器意味着确实有部署在用这个作用域。（内置 NapCat 的 `nekro.napcat.owner` **不在此列**：round-16 起它写的也是 `deployment_scope()`（#46 已改），但**存量容器身上仍是升级前的 `INSTANCE_NAME or "default"`**，而这里要清的正是升级前留下的 `…:default` 那批标记——算进来会让它们永远清不掉；何况 NapCat 容器从不落在这些网桥上。理由完整版见 `OWNERSHIP_SCOPE_LABELS` 上方） |
#    | 该标记的放行 `(地址, 端口)` 上**没有进程在监听** | 这组规则存在的唯一理由，就是让容器能回连该端口上的那个 NA。所有者只要还活着就必然在监听它；反过来，端点明确 `ECONNREFUSED` 就说明所有者已经不在了。证据源优先取**同作用域的沙盒**规则（它放行的按构造就是宿主上的回调端点），取不到时回落到这个标记**自己**的放行端点——见 `resolve_liveness_evidence` 与下面的 #50 |
#    | 以上任何一项**读不出来 / 探测不确定**（Docker 清单读失败、探测超时或不可达、两个证据源都拿不出可探端点） | 判不准就不给删，宁可少删 |
#
# 三条否定性证据都通不过时才判 `orphan`。**没有 override**：容器还在、端口还在听、读不出来，
# 都是硬拦，界面上给的是「先把这些工作区重建一遍」这类可执行的下一步，而不是一个强制按钮。
#
# ### 证据源为什么不能只认沙盒规则（#50）
#
# 原判据把「这个标记的所有者还活着吗」的**唯一**证据源钉死在同作用域的沙盒规则上，
# 找不到就直接拦住。但 `nekro-sandbox:<作用域>` 与 `nekro-cc:<作用域>[.tN]` 是**两个独立的
# 标记、要分两次清**——于是只要运维先点了沙盒那一个，同作用域剩下的 CC 标记就**再也清不掉了**，
# 而这个出口存在的全部理由就是把它们清掉（round-18 在真部署上实测复现）。
# fail-closed 本身没问题，错的是把两个可以各自消失的对象绑成了一个。
# 现在找不到沙盒兄弟时回落去探这个标记自己的放行端点，判据强度不变：
# 仍要拿到明确的 `closed` 才放行，`open` / `unknown` 照旧拦住。
#
# ### 「这个端口是我们自己占着的」那条豁免的边界
#
# 豁免的推理是**宿主 TCP 端口独占**：这个端口上应答的只可能是我们自己，探到 `open` 没有信息量。
# 而端口独占只对**宿主持有的地址**成立。CC 工作区规则集在**转发链**上那条放行的 `-d` 是 NA 自己
# 在工作区网桥上的**容器**地址，端口是容器内的中继端口（compose 下恒为 8021，与裸机部署的默认
# 端口一模一样）——别人的容器完全可以在自己的 IP 上占着同一个端口号。所以豁免只对
# `MarkerInventory.host_endpoints`（读自 `INPUT` 链，内核语义保证目的地是宿主本机）
# 与沙盒族的回调端点成立，转发链上的端点一律要真的探一次。
#
# ## 残余风险（如实登记）
#
# 1. 所有者活着、但它的放行端点在本进程这边表现为 `ECONNREFUSED`（例如它只 bind 了
#    `127.0.0.1`，而规则放行的是网桥网关地址），这时会被判成孤儿。这种部署的容器本来就连不上
#    它的 NA，规则里的放行部分已经是摆设；被删掉的兜底 DROP 由该部署的后台回读自检
#    （`verify_marked_rules`，45 秒一轮）发现「链上已经没有本守卫的规则了」并在下一次
#    起容器时重装。**升级前的老代码没有那道回读**，所以这条残余风险对未升级的邻居仍然存在。
# 2. **邻居正好在重启的那几秒**：端口被拒 -> `closed` -> 判成孤儿。TCP 探测无法把
#    「进程死了」与「进程正在重启」分开。缓解的是流程而不是判据——执行前判定会**重跑一遍**
#    （`execute_orphan_rule_cleanup`），而预览与确认之间隔着一次人工回填确认，
#    两次采样要同时落进同一个重启窗口才会误删。
# 3. **中继关闭的 CC 部署**（`relay_port` 为 0）整组规则里一条 `-d <单机地址> --dport` 都没有，
#    两个证据源都拿不出可探端点，因此永远判 `blocked`。这是 fail-closed 的一侧，
#    但它意味着这类部署的旧 CC 标记仍然清不掉。
# 4. **沙盒族的回调端点仍然整组豁免，而它未必真是宿主地址**。上面那条边界只把回落路径
#    收窄到了 `host_endpoints`；沙盒规则集的按地址放行**全在转发链上**（发往宿主的那一组
#    `build_isolation_rules` 刻意「只认端口不认地址」，因此沙盒标记的 `host_endpoints` 恒为空），
#    所以按同一把尺子量，它们一条都不算「宿主持有」。之所以仍然整组豁免，是因为收掉它
#    等于把主用例（清掉本部署自己升级前那组规则，端点就是本机网桥网关加自己的端口）
#    永久拦在「端口上还有人在听」上 —— 而那个「有人」正是我们自己。
#    代价是一个窄口子：`resolve_rpc_endpoints` 在 `SANDBOX_CHAT_API_URL` 的主机名**不是**
#    `host.docker.internal` 这类别名时，会把解析出来的地址（可能是一台内网反向代理，
#    也就是 `build_isolation_rules` 里「直连形态」那条注释说的场景）一并放进 `rpc_endpoints`。
#    同宿主上一套**还活着**的邻居若正好是这种配置、且那个代理的端口与我们自己的端口撞号，
#    它的沙盒端点就会被全部豁免，于是在**零探测**下判成 `orphan`。
#    从 iptables 文本里分不出「这个地址是不是宿主自己的」，所以这里没有判据可加；
#    能做的是把它显式说出来：判定完全靠豁免、一次实际探测都没有时，`notes` 里会写明这一点
#    （见 `_judge_owner_liveness`），页面上照原样显示，运维在按下删除前看得到。

SANDBOX_MARKER_PREFIX = "nekro-sandbox"

# 取值就是 `deployment_scope()` 的容器归属标签。带着某个作用域的容器 = 确实有部署在用它。
# ⚠️ 不要把 `nekro.napcat.owner` 加进来。round-16 起它写的**也是** `deployment_scope()`
# （#46 已改），但**存量容器身上仍然是升级前那套 `INSTANCE_NAME or "default"`**，而本模块
# 要清的恰恰是升级前留下的 `nekro-sandbox:default` 这类旧标记：一个还带着 `default` 的存量
# NapCat 容器会被当成「有部署在用 `default` 这个作用域」，于是那批孤儿永远清不掉。
# 何况 NapCat 容器从不挂在沙盒/工作区网桥上，算进来也提供不了额外的存活证据。
OWNERSHIP_SCOPE_LABELS: Tuple[str, ...] = ("nekro.sandbox.instance", "nekro.owner.scope")

ProbeOutcome = Literal["open", "closed", "unknown"]
EndpointProbe = Callable[[str, int], Awaitable[ProbeOutcome]]
OrphanVerdict = Literal["self", "orphan", "blocked"]

# `connect_ex` 明确表示「没人在听」的错误码。Windows 上的 WSAECONNREFUSED 是 10061，
# 其余错误码（超时、不可达、权限）一律算「判不准」而不是「没人在听」。
#
# ⚠️ **不要往这里加 11（EAGAIN/EWOULDBLOCK）或 10035（WSAEWOULDBLOCK）。**
# 真部署（Linux）上实测：被拒是 `111` 且立即返回，而**超时返回的正是 `11`**
# ——不是直觉中的 `110`（ETIMEDOUT）。把 `11` 读成「没人在听」，等于把
# 「包被防火墙 DROP 掉」判成「所有者已经死了」，而这两件事在网络层完全同形
# （§2.6 那场九小时黑洞的成因就是 DROP 不回 RST）：判错的代价是删掉别人**还在生效**的规则。
# Windows 上更没得分辨——实测被拒与超时都返回 `10035`，于是那台机器上永远只能得到
# `unknown`（整个功能因此在 Windows 上恒为 fail-closed，而 iptables 本来也只在 Linux 上跑）。
_REFUSED_ERRNOS: FrozenSet[int] = frozenset({61, 111, 10061})


@dataclass(frozen=True)
class GuardMarkerFamily:
    """一类守卫的标记族

    沙盒守卫与 CC 工作区守卫各导出一个，由调用方（控制台 router）组装成清单交给盘点函数。
    **刻意不让沙盒模块直接 import 工作区模块**：依赖方向是工作区 -> 沙盒，反过来就是循环。

    Attributes:
        prefix: 标记前缀，例如 `nekro-sandbox`
        label: 给人看的守卫名
        long_lived_containers: 这类容器是不是长命的。沙盒容器一次性、升级后不会再挂上旧网桥；
            CC 工作区容器长命且网络在创建时定死，旧网桥上完全可能还挂着在跑的容器
        cleanup_hint: 被容器拦住时给运维的下一步
    """

    prefix: str
    label: str
    long_lived_containers: bool
    cleanup_hint: str


SANDBOX_MARKER_FAMILY = GuardMarkerFamily(
    prefix=SANDBOX_MARKER_PREFIX,
    label="沙盒网络隔离",
    long_lived_containers=False,
    cleanup_hint="沙盒容器是一次性的，等它跑完自行退出后再来清理",
)


@dataclass(frozen=True)
class MarkedRuleLine:
    """链上一条带标记的规则

    Attributes:
        family: 地址族
        chain: 链名
        text: `iptables -S` 里读回来的**原文**。预览必须逐字展示它——
            运维要能拿这一行与自己 `iptables-save` 看到的对上号
    """

    family: AddressFamily
    chain: str
    text: str


@dataclass(frozen=True)
class MarkerInventory:
    """某个标记在链上的全部痕迹

    Attributes:
        marker: 规则标记
        prefix: 标记前缀（哪一类守卫）
        lines: 规则原文
        interfaces: 规则里 `-i` 指向的网桥接口名。规则**只**对这些接口上进来的包生效，
            所以「这组规则还在保护谁」这个问题等价于「这些网桥上还有没有容器」
        endpoints: 规则里 `-d <单机地址> ... --dport <端口>` 这种形态的放行目的地。
            对沙盒规则集来说，它就是所有者的 NA 回调端点
        host_endpoints: `endpoints` 里**读自 INPUT 链**的那一部分。这不是分类偏好而是内核语义：
            能走到 `INPUT` 的包按定义终结在宿主本机上，所以这类 `-d` 一定是**宿主持有的地址**；
            转发链（DOCKER-USER / FORWARD）上的 `-d` 则按定义**不是**宿主地址
            （CC 工作区规则集里那一条就是 NA 自己在工作区网桥上的**容器**地址）。
            这个区分是「端口是我们自己的 -> 不必探测」那条豁免的成立前提，见 `_judge_owner_liveness`
    """

    marker: str
    prefix: str
    lines: Tuple[MarkedRuleLine, ...]
    interfaces: Tuple[str, ...]
    endpoints: Tuple[Tuple[str, int], ...]
    host_endpoints: Tuple[Tuple[str, int], ...] = ()


@dataclass(frozen=True)
class BridgeNetworkFact:
    """一个网桥接口对应的 Docker 网络与挂在上面的容器

    Attributes:
        interface: 网桥接口名
        network: Docker 网络名；空串表示宿主上没有任何容器引用着这个网桥对应的网络
        container_total: 容器数（含已停止但 `NetworkMode` 仍钉在这张网上的）
        container_names: 容器名，预览里要列出来让人能自己去核
    """

    interface: str
    network: str
    container_total: int
    container_names: Tuple[str, ...]


@dataclass(frozen=True)
class HostContainerFacts:
    """判定要用到的宿主侧事实

    Attributes:
        bridges: 网桥接口名 -> 该网桥上的容器情况
        scopes_claimed: 归属标签取值 -> 带着它的容器名
        readable: 是否完整读到。**读不全一律判不准**，任何一个候选都不给删
        unreadable_reason: 读不全的原因
    """

    bridges: Dict[str, BridgeNetworkFact]
    scopes_claimed: Dict[str, Tuple[str, ...]]
    readable: bool
    unreadable_reason: str = ""


@dataclass(frozen=True)
class OrphanRuleCandidate:
    """一个标记的判定结果

    Attributes:
        marker: 规则标记
        guard_label: 属于哪个守卫
        verdict: `self`（本部署自用）/ `blocked`（有实证或判不准）/ `orphan`（可清理）
        rule_count: 链上带该标记的规则条数
        chains: 涉及哪些链（含地址族前缀）
        rules: 规则原文
        networks: 规则匹配的网桥各自的容器情况
        container_total: 这些网桥上的容器总数
        blockers: 拦住它的原因，`verdict != "orphan"` 时非空
        notes: 判定过程里值得让人看到、但不构成拦截的事实
    """

    marker: str
    guard_label: str
    verdict: OrphanVerdict
    rule_count: int
    chains: Tuple[str, ...]
    rules: Tuple[MarkedRuleLine, ...]
    networks: Tuple[BridgeNetworkFact, ...]
    container_total: int
    blockers: Tuple[str, ...]
    notes: Tuple[str, ...]


@dataclass(frozen=True)
class OrphanCleanupPlan:
    """一次孤儿规则盘点的完整结果

    Attributes:
        scope: 本部署当前的作用域
        scope_source: 作用域的推导来源，与启动日志里那句同源
        available: 有没有可用的 iptables 执行通道
        unavailable_reason: 没有时的原因
        candidates: 链上找到的全部本项目标记及其判定
    """

    scope: str
    scope_source: str
    available: bool
    unavailable_reason: str
    candidates: Tuple[OrphanRuleCandidate, ...]


@dataclass(frozen=True)
class OrphanCleanupOutcome:
    """一次孤儿规则清理的执行结果

    Attributes:
        marker: 被清理的标记
        expected: 执行前链上带该标记的条数
        deleted: 实际删掉的条数
        residual: 执行后仍带该标记的条数（不为 0 说明没删干净）
        failures: 逐条删除失败的说明
    """

    marker: str
    expected: int
    deleted: int
    residual: int
    failures: Tuple[str, ...]


class OrphanCleanupRefused(RuntimeError):
    """拒绝清理某个标记

    携带**具体理由**而不是一句「不允许」：拦住之后运维要能知道下一步做什么
    （重建工作区、停掉那套部署、或者干脆什么都不做）。
    """

    def __init__(self, marker: str, reasons: Sequence[str]) -> None:
        self.marker = marker
        self.reasons: Tuple[str, ...] = tuple(reasons)
        super().__init__(f"拒绝清理标记 {marker}: " + "；".join(self.reasons))


def extract_rule_marker(tokens: Sequence[str], prefixes: Sequence[str]) -> str:
    """从一条规则的 token 里取出本项目的标记

    只认 `--comment` 的**取值**，不做子串搜索：规则里其他位置（例如某个网桥名）
    恰好含有同样的字符时，子串搜索会把一条别人的规则算进我们的标记里。

    Args:
        tokens: `shlex.split` 之后的规则 token
        prefixes: 认哪些标记前缀

    Returns:
        标记原文；不是本项目的规则时为空串
    """
    value = rule_comment_value(tokens)
    return value if any(value.startswith(f"{prefix}:") for prefix in prefixes) else ""


def _token_value(tokens: Sequence[str], flag: str) -> str:
    args = list(tokens)
    try:
        index = args.index(flag)
    except ValueError:
        return ""
    return args[index + 1] if index + 1 < len(args) else ""


def _rule_host_endpoint(tokens: Sequence[str]) -> Optional[Tuple[str, int]]:
    """从一条规则里取出 `-d <单机地址> ... --dport <端口>` 这一对

    `-d` 是网段（`10.0.0.0/8` 这类兜底 DROP）时返回 None：只有指向**某一台主机**的放行
    才可能是「所有者的 NA 在这里监听」。`iptables -S` 会把单机地址渲染成 `/32`，两种形态都收。
    """
    raw = _token_value(tokens, "-d")
    if not raw:
        return None
    try:
        network = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        return None
    if network.version != 4 or network.prefixlen != 32:
        return None
    for flag in _PORT_FLAGS:
        value = _token_value(tokens, flag)
        if value.isdigit():
            return str(network.network_address), int(value)
    return None


def collect_marker_inventory(
    dumps: Sequence[Tuple[AddressFamily, str, str]],
    prefixes: Sequence[str],
) -> Dict[str, MarkerInventory]:
    """把若干份 `iptables -S <chain>` 输出盘点成「每个标记有哪些规则」

    纯函数：单测直接喂真实形态的文本，不需要任何 iptables。

    Args:
        dumps: (地址族, 链名, `iptables -S <chain>` 原始输出) 的列表
        prefixes: 认哪些标记前缀

    Returns:
        标记 -> 该标记在链上的全部痕迹
    """
    lines: Dict[str, List[MarkedRuleLine]] = {}
    prefix_by_marker: Dict[str, str] = {}
    interfaces: Dict[str, List[str]] = {}
    endpoints: Dict[str, List[Tuple[str, int]]] = {}
    host_endpoints: Dict[str, List[Tuple[str, int]]] = {}

    for family, chain, output in dumps:
        head = f"-A {chain} "
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line.startswith(head):
                continue
            try:
                tokens = shlex.split(line)
            except ValueError:
                continue
            marker = extract_rule_marker(tokens, prefixes)
            if not marker:
                continue
            prefix_by_marker[marker] = marker.split(":", 1)[0]
            lines.setdefault(marker, []).append(MarkedRuleLine(family=family, chain=chain, text=line))
            interface = _token_value(tokens, "-i")
            if interface and interface not in interfaces.setdefault(marker, []):
                interfaces[marker].append(interface)
            endpoint = _rule_host_endpoint(tokens)
            if endpoint:
                if endpoint not in endpoints.setdefault(marker, []):
                    endpoints[marker].append(endpoint)
                # 走到 INPUT 的包按定义终结在宿主本机，因此这一条的 `-d` 一定是宿主持有的地址。
                # 转发链上那一条不是（见 `MarkerInventory.host_endpoints`）
                if chain == INPUT_CHAIN and endpoint not in host_endpoints.setdefault(marker, []):
                    host_endpoints[marker].append(endpoint)

    return {
        marker: MarkerInventory(
            marker=marker,
            prefix=prefix_by_marker[marker],
            lines=tuple(collected),
            interfaces=tuple(interfaces.get(marker, [])),
            endpoints=tuple(endpoints.get(marker, [])),
            host_endpoints=tuple(host_endpoints.get(marker, [])),
        )
        for marker, collected in lines.items()
    }


def is_marker_of_scope(marker: str, scope: str, prefixes: Sequence[str]) -> bool:
    """标记是否属于某个作用域（覆盖该作用域下的**全部**守卫与**全部**分组）

    判据用的是与生成端**同一个函数** `format_isolation_marker`，而不是自己再拼一次字符串：
    作用域里的越界字符会被替换成 `_`、空作用域会回落成 `default`，两侧口径分叉的话，
    「本部署自用」这条最重要的排除就会静默失效。

    分组走前缀而不是枚举：CC 工作区的标记是 `nekro-cc:{作用域}.{分组}`，分组集合随租户增删而变，
    枚举当前分组会漏掉「租户工作区暂时为空、但旧规则仍在保护它的容器」这一档。

    Args:
        marker: 待判定的标记
        scope: 作用域
        prefixes: 本项目用到的标记前缀

    Returns:
        属于该作用域时为 True
    """
    for prefix in prefixes:
        base = format_isolation_marker(prefix, scope)
        if marker == base or marker.startswith(f"{base}."):
            return True
    return False


def sandbox_marker_candidates(marker: str) -> Tuple[str, ...]:
    """给定任一本项目标记，给出「同作用域的沙盒标记」的候选形态

    存活判据**首选**沙盒规则集的放行端点：它按构造就是所有者的 NA 回调端点
    （`SANDBOX_CHAT_API_URL` 指的是宿主），比 CC 规则集里那些既可能是容器侧 API 端口、
    也可能是 NA 容器在旧网桥上的地址的端点更直接。
    ⚠️ 但它**不再是唯一**证据源：找不到（或找到了却没有可探端点）时会回落到标记自己的
    放行端点，见 `resolve_liveness_evidence` 与模块 docstring 的 #50 一节。
    这里只负责给出候选形态，不负责决定用不用。

    Args:
        marker: 任意本项目标记

    Returns:
        候选沙盒标记，按可信度排序；作用域里带分组后缀时会多给一个「去掉分组」的形态
    """
    scope_part = marker.split(":", 1)[1] if ":" in marker else ""
    candidates = [f"{SANDBOX_MARKER_PREFIX}:{scope_part}"]
    if "." in scope_part:
        candidates.append(f"{SANDBOX_MARKER_PREFIX}:{scope_part.rsplit('.', 1)[0]}")
    return tuple(dict.fromkeys(candidates))


async def read_marked_rule_dumps(executor: IptablesExecutor) -> List[Tuple[AddressFamily, str, str]]:
    """把两个地址族、转发链与 INPUT 链的 `iptables -S` 全读一遍

    每条链只读一次：NA 跑在容器里时每一条 iptables 命令都要起一个临时容器，
    按标记逐个去读会让开销随标记数线性增长（同 `count_marked_rules_by_marker`）。
    """
    dumps: List[Tuple[AddressFamily, str, str]] = []
    families: Tuple[AddressFamily, ...] = ("ipv4", "ipv6")
    for family in families:
        forward_chain = await pick_forward_chain(executor, family)
        if forward_chain is None:
            continue
        for chain in (forward_chain, INPUT_CHAIN):
            code, output = await executor.run(family, ["-S", chain])
            if code != 0:
                continue
            dumps.append((family, chain, output))
    return dumps


def _container_field(container, key: str):
    try:
        return container[key]
    except (KeyError, TypeError, IndexError):
        return None


def _container_display_name(container) -> str:
    names = _container_field(container, "Names") or []
    for name in names:
        cleaned = str(name).lstrip("/").strip()
        if cleaned:
            return cleaned
    return str(_container_field(container, "Id") or "")[:12]


async def collect_host_container_facts(docker: aiodocker.Docker) -> HostContainerFacts:
    """盘点宿主上的容器与它们挂在哪些网桥上

    **只用 `GET /containers/json`（列表摘要）+ `GET /networks/{name}`（inspect）**，
    刻意不调 `container.show()`：硬化部署下 docker-guard 的容器归属门会让「不属于本部署的
    容器」inspect 直接 404（见 `services/docker_guard/policy.py`），而这里要数的恰恰就是
    别人的容器。列表摘要里已经带了 `Labels` / `NetworkSettings.Networks` / `HostConfig.NetworkMode`。
    列表端点也不能换成 `GET /networks`（列全部网络）—— 那条**不在 guard 的白名单里**。

    已停止的容器同样要算：它们不出现在 `docker network inspect` 的 `Containers` 里，
    但 `NetworkMode` 仍钉在旧网络上，一旦被启动就会重新挂回旧网桥。
    对 CC 工作区尤其重要——它是长命容器，重启不换网。

    Args:
        docker: Docker 客户端

    Returns:
        宿主侧事实；任何一步读失败都返回 `readable=False`（判不准就不给删）
    """
    try:
        containers = await docker.containers.list(all=True)
    except Exception as e:
        logger.warning(f"盘点孤儿隔离规则时读取容器清单失败: {e}")
        return HostContainerFacts(bridges={}, scopes_claimed={}, readable=False, unreadable_reason=f"读取容器清单失败: {e}")

    members: Dict[str, Set[str]] = {}
    scopes: Dict[str, Set[str]] = {}
    for container in containers:
        display = _container_display_name(container)
        labels = _container_field(container, "Labels") or {}
        for label in OWNERSHIP_SCOPE_LABELS:
            raw = labels.get(label)
            if raw is None:
                # 标签**不存在** —— 这才是「与本项目无关的容器」
                continue
            # 标签存在但取值是空串，说明它是「作用域回落到空」的那个历史版本建的容器，
            # 而空作用域被 `format_isolation_marker` 折成 `default` —— 判定侧
            # （`is_marker_of_scope(marker, "", prefixes)`）本来就把它当成一个正经作用域。
            # 这里跟着 `or ""` 一起丢掉的话，两半判据就分叉了：一个还活着的存量容器
            # 认领不到 `nekro-*:default`，那批标记会在「没人认领」的结论下被删掉
            scopes.setdefault(str(raw).strip(), set()).add(display)
        names: Set[str] = set((_container_field(container, "NetworkSettings") or {}).get("Networks") or {})
        mode = str((_container_field(container, "HostConfig") or {}).get("NetworkMode") or "").strip()
        # `container:<ref>` / `host` / `none` 不是网络名；`default` 是 Docker 的别名而不是一张网
        if mode and ":" not in mode and mode not in {"host", "none", "default"}:
            names.add(mode)
        for name in names:
            if name:
                members.setdefault(str(name), set()).add(display)

    bridges: Dict[str, BridgeNetworkFact] = {}
    for name in sorted(members):
        try:
            detail = await (await docker.networks.get(name)).show()
        except Exception as e:
            logger.warning(f"盘点孤儿隔离规则时读取网络 {name} 失败: {e}")
            return HostContainerFacts(
                bridges={},
                scopes_claimed={},
                readable=False,
                unreadable_reason=f"读取网络 {name} 失败: {e}",
            )
        options = detail.get("Options") or {}
        interface = str(options.get("com.docker.network.bridge.name") or "").strip()
        if not interface:
            interface = f"br-{str(detail.get('Id', ''))[:12]}"
        attached = {
            str((entry or {}).get("Name") or "").lstrip("/").strip()
            for entry in (detail.get("Containers") or {}).values()
        }
        occupants = sorted({occupant for occupant in members[name] | attached if occupant})
        existing = bridges.get(interface)
        if existing is not None:
            occupants = sorted(set(existing.container_names) | set(occupants))
        bridges[interface] = BridgeNetworkFact(
            interface=interface,
            network=str(detail.get("Name") or name),
            container_total=len(occupants),
            container_names=tuple(occupants),
        )

    return HostContainerFacts(
        bridges=bridges,
        scopes_claimed={scope: tuple(sorted(names)) for scope, names in scopes.items()},
        readable=True,
    )


def _probe_endpoint_sync(address: str, port: int, timeout: float) -> ProbeOutcome:
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        code = connection.connect_ex((address, port))
    except OSError:
        return "unknown"
    finally:
        connection.close()
    if code == 0:
        return "open"
    if code in _REFUSED_ERRNOS:
        return "closed"
    return "unknown"


async def probe_tcp_endpoint(address: str, port: int, timeout: float = 2.0) -> ProbeOutcome:
    """探测某个 (地址, 端口) 上还有没有进程在监听

    **只有明确的 `ECONNREFUSED` 才算「没人在听」**：超时、不可达、被中间设备丢弃都返回
    `unknown`，由判定侧当成「判不准」拦住。反过来说错的代价不对称——把「还活着」看成「已经死了」
    会删掉别人正在用的规则，而把「已经死了」看成「还活着」只是少删一组。

    连上就立刻关闭，不发送任何字节。

    Args:
        address: IPv4 地址
        port: TCP 端口
        timeout: 连接超时（秒）

    Returns:
        `open` / `closed` / `unknown`
    """
    return await asyncio.to_thread(_probe_endpoint_sync, address, port, timeout)


def judge_orphan_candidates(
    inventory: Dict[str, MarkerInventory],
    families: Sequence[GuardMarkerFamily],
    current_scope: str,
    host: HostContainerFacts,
    liveness: Dict[Tuple[str, int], ProbeOutcome],
    own_ports: FrozenSet[int],
) -> List[OrphanRuleCandidate]:
    """对盘点到的每个标记给出判定

    纯函数（探测与 Docker 读取都在外面做完再传进来），所以判据表可以完全表驱动地测。

    Args:
        inventory: `collect_marker_inventory` 的结果
        families: 本项目的标记族清单
        current_scope: 本部署的作用域
        host: 宿主侧事实
        liveness: 已探测的端点结果
        own_ports: 本部署自己监听的端口。规则放行的正是这些端口时，「有人在听」是**我们自己**，
            不构成「另一套部署还活着」的证据——宿主 TCP 端口是独占的。
            ⚠️ 端口独占只对**宿主持有的地址**成立，所以这条豁免并非对所有端点都套用，
            范围由 `LivenessEvidence.exemptible` 给出（见 `resolve_liveness_evidence`）

    Returns:
        按标记排序的判定结果
    """
    prefixes = [family.prefix for family in families]
    family_by_prefix = {family.prefix: family for family in families}
    candidates: List[OrphanRuleCandidate] = []

    for marker in sorted(inventory):
        entry = inventory[marker]
        guard = family_by_prefix.get(entry.prefix)
        if guard is None:
            continue

        networks = tuple(
            host.bridges.get(interface) or BridgeNetworkFact(interface=interface, network="", container_total=0, container_names=())
            for interface in entry.interfaces
        )
        container_total = sum(fact.container_total for fact in networks)
        chains = tuple(dict.fromkeys(f"{line.family} {line.chain}" for line in entry.lines))

        if is_marker_of_scope(marker, current_scope, prefixes):
            candidates.append(
                OrphanRuleCandidate(
                    marker=marker,
                    guard_label=guard.label,
                    verdict="self",
                    rule_count=len(entry.lines),
                    chains=chains,
                    rules=entry.lines,
                    networks=networks,
                    container_total=container_total,
                    blockers=("这是本部署当前正在使用的标记，永远不会出现在可清理清单里",),
                    notes=(),
                ),
            )
            continue

        blockers: List[str] = []
        notes: List[str] = []

        if not host.readable:
            blockers.append(f"读不全宿主上的容器与网络清单，无法确认这组规则是否还在保护谁（{host.unreadable_reason}）")

        if not entry.interfaces:
            # 「这组规则还在保护谁」这个问题等价于「它匹配的网桥上还有没有容器」。
            # 一条 `-i` 都读不出来的规则集要么不是本项目装的、要么读回来的文本残缺，
            # 两种情况下「网桥上没有容器」这条判据都不成立 —— 判不准就不给删
            blockers.append("这组规则里读不出任何 `-i <网桥>`，无法确定它在保护哪一批容器")

        for fact in networks:
            if fact.container_total:
                occupied = "、".join(fact.container_names[:8])
                extra = "…" if len(fact.container_names) > 8 else ""
                detail = (
                    f"旧网络 {fact.network}（网桥 {fact.interface}）上还有 {fact.container_total} 个容器：{occupied}{extra}"
                )
                if guard.long_lived_containers:
                    detail += (
                        "。这组规则里既有放行也有兜底 DROP，删掉之后这些容器可以直接访问宿主内网，"
                        f"**不报错也不留日志**；{guard.cleanup_hint}"
                    )
                else:
                    detail += f"。{guard.cleanup_hint}"
                blockers.append(detail)
            elif fact.network:
                notes.append(f"旧网络 {fact.network}（网桥 {fact.interface}）上没有容器")
            else:
                notes.append(f"宿主上没有任何容器引用网桥 {fact.interface} 对应的网络")

        for scope_value, holders in sorted(host.scopes_claimed.items()):
            if is_marker_of_scope(marker, scope_value, prefixes):
                blockers.append(
                    f"宿主上有容器带着作用域 {scope_value!r} 的归属标签（{'、'.join(holders[:8])}），"
                    "说明确实有部署在用这个作用域",
                )

        blockers.extend(_judge_owner_liveness(marker, inventory, liveness, own_ports, notes))

        candidates.append(
            OrphanRuleCandidate(
                marker=marker,
                guard_label=guard.label,
                verdict="blocked" if blockers else "orphan",
                rule_count=len(entry.lines),
                chains=chains,
                rules=entry.lines,
                networks=networks,
                container_total=container_total,
                blockers=tuple(blockers),
                notes=tuple(notes),
            ),
        )

    return candidates


@dataclass(frozen=True)
class LivenessEvidence:
    """回答「这个标记的所有者还活着吗」时要探测哪些端点

    判定侧（`_judge_owner_liveness`）与探测侧（`_probe_orphan_endpoints`）**必须**读同一份
    答案：两边各写一次筛选，任何一次分叉都表现为「判定要用的端点根本没被探过」，
    于是 `liveness` 里查不到、按缺省值当成 `unknown`，整组标记永久卡在 `blocked`——
    #50 那个顺序陷阱就是这种形态。所以这里做成一个共用的纯函数。

    Attributes:
        source_marker: 证据取自哪个标记的规则；`refusal` 非空时为空串
        endpoints: 要参与判定的放行端点
        exemptible: `endpoints` 里允许套用「这是本部署自己的端口」豁免的那一部分，见下
        fallback: 是不是回落到了标记**自己的**端点（找不到同作用域的沙盒兄弟）
        refusal: 一个可用的证据源都没有时的拦截理由；非空即拦住
    """

    source_marker: str
    endpoints: Tuple[Tuple[str, int], ...]
    exemptible: FrozenSet[Tuple[str, int]]
    fallback: bool
    refusal: str


def resolve_liveness_evidence(marker: str, inventory: Dict[str, MarkerInventory]) -> LivenessEvidence:
    """挑出用来判断某个标记的所有者是否还在的放行端点

    **首选同作用域的沙盒规则**：它的放行端点按构造就是宿主上的 (地址, 端口)
    （`SANDBOX_CHAT_API_URL` 指的就是宿主），拿它探「那套部署还在不在」最直接。

    **找不到沙盒兄弟时回落到这个标记自己的端点**（#50）。原本这里直接返回一条拦截理由，
    于是只要运维先清掉了 `nekro-sandbox:<作用域>`，同作用域剩下的 `nekro-cc:<作用域>[.tN]`
    就**再也清不掉**——而这个运维出口存在的全部理由就是把它们清掉。两个标记本来就是
    各自独立、要分两次清的对象，把「CC 标记的所有者还在不在」绑死在沙盒标记的存在性上，
    是证据源选错了，不是 fail-closed 的必要代价。判据强度不变：回落之后仍然要拿到明确的
    `closed` 才放行，`open` / `unknown` 照旧拦住。

    ⚠️ **回落时「这是本部署自己的端口」那条豁免只对 `host_endpoints` 成立。**
    豁免的推理是「宿主 TCP 端口独占 -> 这个端口上应答的只可能是我们自己」，而端口独占只对
    **宿主持有的地址**成立。CC 工作区规则集在转发链上那条放行的 `-d` 是 NA 自己在工作区网桥上的
    **容器**地址（NA 在容器里时），端口则是容器内的中继端口——compose 下恒为 8021，
    与裸机部署的默认端口一模一样。把它一并豁免，等于同宿主上一套**还活着**的容器化邻居
    在「没有任何存活证据」下被判成孤儿，而 CC 工作区容器是长命的：删掉那组规则里的兜底 DROP
    之后它们可以直接访问宿主内网，不报错也不留日志（`WORKSPACE_MARKER_FAMILY` 的注释）。
    所以转发链上的端点一律要**真的探一次**。

    Args:
        marker: 待判定的标记
        inventory: `collect_marker_inventory` 的结果

    Returns:
        证据源；`refusal` 非空表示没有任何可用证据
    """
    candidates = sandbox_marker_candidates(marker)
    sibling = next((item for item in candidates if inventory.get(item) and inventory[item].endpoints), "")
    if sibling:
        endpoints = inventory[sibling].endpoints
        # 沙盒规则集的放行端点按构造就是宿主地址，整组都可豁免（round-16 的判据，逐字保留）
        return LivenessEvidence(sibling, endpoints, frozenset(endpoints), False, "")

    own = inventory.get(marker)
    if own is not None and own.endpoints:
        return LivenessEvidence(marker, own.endpoints, frozenset(own.host_endpoints), True, "")

    # 判的就是沙盒标记自己时，候选里的第一个**就是它**——照抄一句「链上没有与之同作用域的
    # 沙盒隔离规则（找过 nekro-sandbox:default）」会让运维看到一条自相矛盾的理由
    # （那条规则明明就在链上，正在被判定）。拦截理由是要照原样显示给人看的，必须说得准。
    others = tuple(item for item in candidates if item != marker)
    refusal = (
        f"链上既没有与之同作用域的沙盒隔离规则（找过 {'、'.join(others)}），"
        "这组规则自己也没有可探测的放行端点，无法判断它的所有者是否还在"
        if others
        else "这组规则自己没有可探测的放行端点，链上也没有别的同作用域证据源，无法判断它的所有者是否还在"
    )
    return LivenessEvidence("", (), frozenset(), False, refusal)


def _judge_owner_liveness(
    marker: str,
    inventory: Dict[str, MarkerInventory],
    liveness: Dict[Tuple[str, int], ProbeOutcome],
    own_ports: FrozenSet[int],
    notes: List[str],
) -> List[str]:
    """「这个标记的所有者还活着吗」——用 `resolve_liveness_evidence` 挑出的放行端点回答"""
    evidence = resolve_liveness_evidence(marker, inventory)
    if evidence.refusal:
        return [evidence.refusal]
    if evidence.fallback:
        notes.append(
            f"链上没有与之同作用域的沙盒隔离规则，改用 {evidence.source_marker} 自己放行的端点判断所有者是否还在",
        )

    blockers: List[str] = []
    undecided: List[str] = []
    consulted = 0
    for endpoint in evidence.endpoints:
        address, port = endpoint
        if port in own_ports and endpoint in evidence.exemptible:
            notes.append(f"{address}:{port} 是本部署自己监听的端口，不作为「另一套部署还活着」的证据")
            continue
        consulted += 1
        outcome = liveness.get(endpoint, "unknown")
        if outcome == "open":
            blockers.append(
                f"{address}:{port} 上仍有进程在监听——这正是隔离规则 {evidence.source_marker} 放行的端点，"
                "它的所有者很可能还在运行",
            )
        elif outcome == "closed":
            notes.append(f"{address}:{port} 明确拒绝连接，没有进程在监听")
        else:
            undecided.append(f"{address}:{port}")
    if undecided:
        blockers.append(
            f"探测 {'、'.join(undecided)} 没有得到明确结果（超时或不可达），"
            "不能据此断定它的所有者已经不在",
        )
    if not consulted:
        # 走到这里必然 `not blockers`：拦截理由只可能由被真正查过的端点产生。
        # 一次实际探测都没做过就得出「所有者不在」——结论完全压在「宿主 TCP 端口独占」
        # 这个前提上，而它只对**宿主持有的地址**成立（模块 docstring 残余风险 4）。
        # 主用例（清本部署自己升级前那组规则）本来就长这样，所以这**不是**拦截理由；
        # 但运维在按下删除前有权知道这一条判定背后没有任何一次观测。
        notes.append(
            f"判存活所用的放行端点（取自 {evidence.source_marker}）全部落在「这是本部署自己的端口」豁免里，"
            "本次判定没有做过任何一次实际探测：结论依赖「宿主 TCP 端口独占」这个前提。"
            "若这些地址其实不属于本宿主（例如另一套部署把回调指向了内网反向代理，而它的端口与本部署撞号），"
            "请先自行确认那套部署确实已经停了",
        )
    return blockers


async def _probe_orphan_endpoints(
    inventory: Dict[str, MarkerInventory],
    current_scope: str,
    prefixes: Sequence[str],
    own_ports: FrozenSet[int],
    probe: Optional[EndpointProbe],
) -> Dict[Tuple[str, int], ProbeOutcome]:
    """探测判定要用到的那些端点，一个端点只探一次

    要探哪些端点由 `resolve_liveness_evidence` 说了算——与判定侧同一个函数。
    这里自己再筛一遍的话，两份筛选一分叉就是「判定要用的端点没被探过」，
    而缺省值是 `unknown`，于是拦得毫无道理又查不出原因。

    `probe` 在**调用时**解析而不是写成默认参数值：默认参数在函数定义期就绑死了，
    测试再怎么替换模块属性都换不掉它，于是单测会真的去连宿主端口（§2.9 同源）。
    """
    probe = probe or probe_tcp_endpoint
    targets: List[Tuple[str, int]] = []
    for marker in inventory:
        if is_marker_of_scope(marker, current_scope, prefixes):
            continue
        evidence = resolve_liveness_evidence(marker, inventory)
        for endpoint in evidence.endpoints:
            if endpoint[1] in own_ports and endpoint in evidence.exemptible:
                continue
            if endpoint not in targets:
                targets.append(endpoint)
    results: Dict[Tuple[str, int], ProbeOutcome] = {}
    for address, port in targets:
        try:
            results[(address, port)] = await probe(address, port)
        except Exception as e:
            # 探测本身出错等同「判不准」，绝不能升级成「可以删」
            logger.debug(f"探测孤儿规则端点 {address}:{port} 失败: {e}")
            results[(address, port)] = "unknown"
    return results


async def plan_orphan_rule_cleanup(
    docker: aiodocker.Docker,
    families: Sequence[GuardMarkerFamily],
    own_ports: FrozenSet[int],
    probe: Optional[EndpointProbe] = None,
) -> OrphanCleanupPlan:
    """盘点链上的全部本项目标记并逐个判定，**只读不删**

    Args:
        docker: Docker 客户端
        families: 本项目的标记族清单（由调用方把沙盒与 CC 工作区两侧组装起来）
        own_ports: 本部署自己监听的端口
        probe: 端点存活探测，供测试注入

    Returns:
        盘点结果
    """
    scope = deployment_scope()
    source = describe_deployment_scope()
    executor = await resolve_executor(docker)
    if executor is None:
        return OrphanCleanupPlan(
            scope=scope,
            scope_source=source,
            available=False,
            unavailable_reason="当前宿主上没有可用的 iptables 执行通道，无法盘点隔离规则",
            candidates=(),
        )

    prefixes = [family.prefix for family in families]
    dumps = await read_marked_rule_dumps(executor)
    inventory = collect_marker_inventory(dumps, prefixes)
    host = await collect_host_container_facts(docker)
    liveness = await _probe_orphan_endpoints(inventory, scope, prefixes, own_ports, probe)
    candidates = judge_orphan_candidates(inventory, families, scope, host, liveness, own_ports)
    return OrphanCleanupPlan(
        scope=scope,
        scope_source=source,
        available=True,
        unavailable_reason="",
        candidates=tuple(candidates),
    )


async def purge_marked_rules(executor: IptablesExecutor, marker: str) -> Tuple[int, List[str]]:
    """把某个标记的规则从两个地址族的转发链与 INPUT 链上全部删掉

    走的是与「先清后装」**同一个** `purge_marked_rules_on_chain`，因此同样带行号兜底、
    同样以 argv 数组执行、同样 token 精确匹配 `--comment`。

    Args:
        executor: iptables 执行通道
        marker: 规则标记

    Returns:
        (删除条数, 失败说明)
    """
    deleted = 0
    failures: List[str] = []
    families: Tuple[AddressFamily, ...] = ("ipv4", "ipv6")
    for family in families:
        forward_chain = await pick_forward_chain(executor, family)
        if forward_chain is None:
            continue
        for chain in (forward_chain, INPUT_CHAIN):
            chain_deleted, chain_failures = await purge_marked_rules_on_chain(executor, family, chain, marker)
            deleted += chain_deleted
            failures.extend(chain_failures)
    return deleted, failures


async def execute_orphan_rule_cleanup(
    docker: aiodocker.Docker,
    families: Sequence[GuardMarkerFamily],
    own_ports: FrozenSet[int],
    marker: str,
    probe: Optional[EndpointProbe] = None,
) -> OrphanCleanupOutcome:
    """清理某个被判为孤儿的标记

    **判定在执行前重跑一遍**，而不是相信预览时的结论：预览与确认之间可能过了几分钟，
    期间完全可能有人把一个 CC 工作区启动起来（它会挂回旧网桥）。拿旧结论去删，
    删掉的就是正在生效的兜底 DROP。

    Args:
        docker: Docker 客户端
        families: 本项目的标记族清单
        own_ports: 本部署自己监听的端口
        marker: 要清理的标记
        probe: 端点存活探测，供测试注入

    Returns:
        执行结果

    Raises:
        OrphanCleanupRefused: 标记不存在、是本部署自用的、或判定没通过
    """
    plan = await plan_orphan_rule_cleanup(docker, families, own_ports, probe)
    if not plan.available:
        raise OrphanCleanupRefused(marker, (plan.unavailable_reason,))

    candidate = next((item for item in plan.candidates if item.marker == marker), None)
    if candidate is None:
        raise OrphanCleanupRefused(marker, ("链上已经没有带这个标记的规则了，无需清理",))
    if candidate.verdict != "orphan":
        raise OrphanCleanupRefused(marker, candidate.blockers or ("判定未通过",))

    executor = await resolve_executor(docker)
    if executor is None:
        raise OrphanCleanupRefused(marker, ("当前宿主上没有可用的 iptables 执行通道",))

    deleted, failures = await purge_marked_rules(executor, marker)
    residual = await count_marked_rules(executor, marker)
    logger.warning(
        f"已清理孤儿隔离规则 {marker}：预期 {candidate.rule_count} 条，实际删除 {deleted} 条，残留 {residual} 条",
    )
    return OrphanCleanupOutcome(
        marker=marker,
        expected=candidate.rule_count,
        deleted=deleted,
        residual=residual,
        failures=tuple(failures),
    )


# =============================================================================
# 规则回读自检的调度（沙盒守卫与 CC 工作区守卫共用）
# =============================================================================

# 两次回读之间的最小间隔。NA 跑在容器里时每一次 iptables 调用都要起一个临时容器，
# 一轮核对是 6~8 次调用；放在后台且按 TTL 节流，稳态成本才可以忽略
RULE_VERIFY_INTERVAL_SECONDS = 45.0


@dataclass
class _VerifyState:
    last_at: float = 0.0
    task: Optional[asyncio.Task] = field(default=None, repr=False)


_verify_states: Dict[str, _VerifyState] = {}


def mark_rules_verified(key: str) -> None:
    """把某个标记的回读时钟拨到现在

    刚装完规则时调用：装完即等于核对过，没必要立刻再读一遍。

    Args:
        key: 调度键，通常就是规则标记
    """
    _verify_states.setdefault(key, _VerifyState()).last_at = time.monotonic()


def reset_rule_verification(key: Optional[str] = None) -> None:
    """清空回读调度状态（供测试与配置变更后强制重来使用）

    Args:
        key: 只清某个调度键；省略时全清
    """
    if key is None:
        _verify_states.clear()
        return
    _verify_states.pop(key, None)


def schedule_rule_verification(key: str, verify: Callable[[], Awaitable[None]]) -> None:
    """按 TTL 在后台核对一次内核里的规则

    **不放在关键路径上**：核对要跑好几条 iptables 命令，NA 在容器里时每条都要起一个临时容器，
    同步做等于给每次沙盒启动加上数秒延迟。放后台的代价是「发现被改写」比「用上被改写的规则」
    晚一步，即最多一轮沙盒执行——而那一轮由容器侧的连接超时兜住（见 `ext_caller_code.py`）。

    Args:
        key: 调度键，通常就是规则标记
        verify: 真正做核对的协程工厂，自行负责建立/关闭 Docker 客户端与失效缓存
    """
    state = _verify_states.setdefault(key, _VerifyState())
    if state.task is not None and not state.task.done():
        return
    now = time.monotonic()
    if state.last_at and now - state.last_at < RULE_VERIFY_INTERVAL_SECONDS:
        return
    state.last_at = now
    state.task = asyncio.create_task(_run_rule_verification(key, verify))


async def _run_rule_verification(key: str, verify: Callable[[], Awaitable[None]]) -> None:
    try:
        await verify()
    except Exception as e:
        # 后台任务里的异常没人接，必须自己吞掉；核对失败本身不改变任何已生效的结论
        logger.debug(f"后台核对隔离规则失败（{key}）: {e}")


async def prepare_sandbox_network(docker: aiodocker.Docker) -> SandboxNetworkPlan:
    """准备沙盒容器的网络参数，必要时安装隔离规则

    每次创建沙盒容器前调用。规则安装结果按签名缓存，配置或网络属性变化时自动重装；
    上一次失败时按 `_RETRY_INTERVAL_SECONDS` 节流重试。

    Args:
        docker: Docker 客户端

    Returns:
        容器创建时使用的网络参数

    Raises:
        SandboxNetworkIsolationError: `strict` 档位下隔离未生效
    """
    global _applied_signature, _applied_spec, _last_attempt_at, _last_failure_reason

    mode: IsolationMode = config.SANDBOX_NETWORK_ISOLATION
    if mode == "off":
        return LEGACY_PLAN

    try:
        network = await _ensure_network(docker)
    except Exception as e:
        message = f"创建沙盒专用网络失败: {e}"
        if mode == "strict":
            raise SandboxNetworkIsolationError(message) from e
        logger.warning(f"{message}；沙盒网络隔离未生效，容器可访问内网")
        return LEGACY_PLAN

    plan_hosts: Tuple[str, ...] = (
        (f"host.docker.internal:{network.ipv4_gateway}",)
        if network.ipv4_gateway
        else ("host.docker.internal:host-gateway",)
    )

    # 缓存键覆盖所有会改变规则集的输入。每次跑沙盒都重算规则集意味着每次都要做一次
    # 阻塞的 DNS 解析和额外的 Docker 调用，而这些输入实际上只在启动或改配置时才会变
    marker = isolation_marker(deployment_scope())
    extra_ipv4, extra_ipv6 = split_extra_blocked_cidrs(config.NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS)
    cache_key = (
        network.bridge_interface,
        network.ipv4_subnet,
        network.ipv4_gateway,
        config.SANDBOX_CHAT_API_URL,
        marker,
        extra_ipv4,
        extra_ipv6,
    )

    async with _install_lock:
        if _applied_signature != cache_key:
            now = time.monotonic()
            # 曾经装成功过说明是配置/网络变了，立即重装；从未成功过则按节流间隔重试
            if _applied_signature is not None or now - _last_attempt_at >= _RETRY_INTERVAL_SECONDS:
                _last_attempt_at = now
                try:
                    host_addresses = [
                        address
                        for address in (network.ipv4_gateway, await _default_bridge_gateway(docker))
                        if address
                    ]
                    # 主机名解析走线程，避免把事件循环卡在一次同步 DNS 查询上
                    rpc_endpoints = await asyncio.to_thread(
                        resolve_rpc_endpoints,
                        config.SANDBOX_CHAT_API_URL,
                        host_addresses,
                    )
                    if not rpc_endpoints:
                        raise SandboxNetworkIsolationError(
                            f"无法从 SANDBOX_CHAT_API_URL={config.SANDBOX_CHAT_API_URL!r} 解析出**宿主可达**的回调端点。"
                            "沙盒容器与 NA 不在同一张 Docker 网络上，compose 里的服务别名（如 http://nekro_agent:8021/api）"
                            "在宿主与沙盒网络里都解析不到；"
                            f"请改回 http://host.docker.internal:{OsEnv.EXPOSE_PORT}/api（两种部署形态都自动指向宿主网关），"
                            "或直接填宿主上可解析的 IP",
                        )
                    spec = SandboxNetworkSpec(
                        bridge_interface=network.bridge_interface,
                        ipv4_subnet=network.ipv4_subnet,
                        rpc_endpoints=rpc_endpoints,
                        marker=marker,
                        extra_blocked_ipv4=extra_ipv4,
                        extra_blocked_ipv6=extra_ipv6,
                    )
                    executor = await resolve_executor(docker)
                    if executor is None:
                        raise SandboxNetworkIsolationError("当前宿主上没有可用的 iptables 执行通道")
                    await install_marked_rules(executor, spec.marker, partial(build_isolation_rules, spec))
                except NetworkIsolationError as e:
                    _applied_signature = None
                    _applied_spec = None
                    _last_failure_reason = str(e)
                except Exception as e:
                    _applied_signature = None
                    _applied_spec = None
                    _last_failure_reason = f"{e}"
                else:
                    _applied_signature = cache_key
                    _applied_spec = spec
                    _last_failure_reason = ""
                    mark_rules_verified(marker)
                    logger.info(
                        f"沙盒网络隔离已生效: 网络 {network.name} (网桥 {spec.bridge_interface}, 子网 {spec.ipv4_subnet})，"
                        f"放行 RPC 端点 {list(spec.rpc_endpoints)}",
                    )

        isolated = _applied_signature == cache_key
        applied_spec = _applied_spec

    if not isolated:
        if mode == "strict":
            raise SandboxNetworkIsolationError(
                f"沙盒网络隔离未生效（{_last_failure_reason}），SANDBOX_NETWORK_ISOLATION=strict 下拒绝启动沙盒",
            )
        logger.warning(
            f"沙盒网络隔离未生效，已回落到默认网桥，容器可访问内网（RFC1918、云元数据服务 169.254.169.254 等）: "
            f"{_last_failure_reason}。"
            "请检查宿主的 iptables 权限；若确认不需要该防护，可将 SANDBOX_NETWORK_ISOLATION 设为 off 以消除此告警",
        )
        # 回落到默认网桥，而不是继续把容器挂到隔离专用网络上：规则本来就不在关闭时移除，
        # 链上完全可能残留着上一次（或别的部署）装的兜底 DROP，把容器挂上去等于送进黑洞，
        # 于是 best_effort 的结果反而比 off 更糟——表现是每条消息的沙盒都卡到超时
        return LEGACY_PLAN

    if applied_spec is not None:
        # 「装过」不等于「还在」：标记是宿主全局的，共用同一标记的进程会先按标记全删再装自己那套。
        # 见模块 docstring。核对放后台，不给沙盒启动增加延迟
        schedule_rule_verification(
            applied_spec.marker,
            partial(_verify_applied_sandbox_rules, applied_spec, cache_key),
        )

    return SandboxNetworkPlan(network_mode=network.name, extra_hosts=plan_hosts, isolated=True)


async def _verify_applied_sandbox_rules(spec: SandboxNetworkSpec, signature: Tuple) -> None:
    """后台核对：内核里带本标记的规则是否仍是我们装的那套

    对不上就作废缓存并打 ERROR，下一次跑沙盒会强制重装。这里刻意**不当场重装**：
    重装要持有安装锁并跑十几条 iptables 命令，而此刻可能正有沙盒在跑；
    交给下一次 `prepare_sandbox_network` 是同一个效果，且串在既有的失败/告警路径上。

    Args:
        spec: 上一次成功安装时用的规则集输入
        signature: 与之对应的缓存签名，只有它仍然当值时才作废（期间配置可能已经变过）
    """
    global _applied_signature, _applied_spec, _last_attempt_at, _last_failure_reason

    docker = aiodocker.Docker()
    try:
        executor = await resolve_executor(docker)
        if executor is None:
            return
        difference = await verify_marked_rules(executor, spec.marker, partial(build_isolation_rules, spec))
    finally:
        await docker.close()

    if not difference:
        return

    async with _install_lock:
        if _applied_signature != signature:
            return
        _applied_signature = None
        _applied_spec = None
        # 失败重试的节流也要一并清掉：它算的是「上一次尝试安装」的时间，而上一次是**成功**的，
        # 不清的话下一次跑沙盒会被节流挡住不重装，白白多降级一分钟
        _last_attempt_at = 0.0
        _last_failure_reason = difference

    logger.error(
        f"沙盒隔离规则已被外部改写（{difference}）：标记 `{spec.marker}` 是**宿主全局**的，"
        "任何按同一标记先全删再装的进程都会顶掉本部署的规则。"
        "后果是沙盒回连 NA 的包落进兜底拒绝被静默吞掉，每一次沙盒执行都卡到超时且 NA 侧没有任何错误日志。"
        "下一次运行沙盒时将强制重装。作用域现在默认由数据目录派生（两套部署不会同名），"
        "所以反复出现只可能是：两套部署显式设了相同的 NEKRO_INSTANCE_NAME、"
        "两套部署共用了同一个 NEKRO_DATA_DIR、或者某个验证脚本沿用了本部署的作用域",
    )


async def setup_sandbox_network_isolation() -> None:
    """启动时预先建好网络并装上规则

    放在启动阶段是为了让「装不上」这件事在启动日志里就暴露出来，而不是等到第一次跑沙盒。
    这里吞掉异常：`strict` 的 fail closed 由 `prepare_sandbox_network` 在真正创建容器时执行，
    启动阶段直接崩掉整个应用不是合适的失败方式。
    """
    if config.SANDBOX_NETWORK_ISOLATION == "off":
        logger.info("SANDBOX_NETWORK_ISOLATION=off，跳过沙盒网络隔离（沙盒容器可访问宿主内网）")
        return

    scope = deployment_scope()
    marker = isolation_marker(scope)
    # 这一行是运维把 iptables 里的标记对回某一套部署的**唯一**依据，
    # 所以取值与推导来源必须同时打出来（见 os_env.describe_deployment_scope）
    logger.info(
        f"沙盒网络隔离作用域 {describe_deployment_scope()}；"
        f"规则标记 {marker}，专用网络 {sandbox_network_name(scope)}（网桥 {sandbox_bridge_name(scope)}）",
    )

    docker = aiodocker.Docker()
    try:
        await prepare_sandbox_network(docker)
        await report_legacy_marker_orphans(
            docker,
            [
                LegacyMarkerCheck(
                    guard="沙盒网络隔离",
                    legacy_marker=isolation_marker(LEGACY_SCOPE),
                    current_marker=marker,
                    legacy_network=sandbox_network_name(LEGACY_SCOPE),
                ),
            ],
        )
    except NetworkIsolationError as e:
        logger.warning(f"{e}")
    except Exception as e:
        logger.warning(f"初始化沙盒网络隔离失败: {e}")
    finally:
        await docker.close()
