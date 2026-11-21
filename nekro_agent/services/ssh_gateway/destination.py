"""用户服务器目标地址的检查

## 为什么用户的服务器不许是内网地址

网关在 NA 的位置发起连接。如果允许用户登记一个内网地址，他就能让 NA 替自己去访问
只有 NA 才够得着的东西：宿主上的管理接口、数据库、云厂商元数据服务、同一宿主上的
其他容器。容器里的 CC 本来被网络隔离挡在这些地址之外，网关不能变成翻墙的洞。

平台方行不查：平台方登记内网机器是正当用法（运维自己就在这些网段里工作），给平台方
加这条限制只会把正常的排障挡掉。

## 不是公网地址就拒

封禁网段列表是给沙盒网络守卫用的，列不全：`::`（Linux 上连它等于连本机）、保留段、
组播这些都不在里面。所以先问一句「这是不是公网地址」（`is_global`），不是的一律拒；
IPv6 里嵌着 IPv4 的几种写法（IPv4 映射、NAT64 `64:ff9b::/96`、6to4 `2002::/16`、Teredo、
已废弃的 `::a.b.c.d`）把里面的 IPv4 取出来再判一遍。解析结果里解不成地址的也按拒绝处理。

## 这只是一道登记时的检查

真正的防线在后续网关里：每次连接都会按当前解析结果再查一遍，然后**直接连查过的
地址**，而不是再解析一次主机名。否则攻击者可以让同一域名第一次解析到公网、连的时候
解析到内网（DNS 重绑定）。本函数只负责在登记时尽早给出反馈，不承担运行时防护。
"""

import asyncio
import ipaddress
from dataclasses import dataclass, field
from typing import List, Optional

from nekro_agent.core.config import config
from nekro_agent.services.sandbox.network_guard import (
    BLOCKED_IPV4_CIDRS,
    BLOCKED_IPV6_CIDRS,
)
from nekro_agent.services.ssh_gateway.constants import REASON_TARGET_BLOCKED


@dataclass(frozen=True)
class DestinationVerdict:
    """一次目标检查的结论

    `reason` 对"解析不了"给一句人话，对"落进封禁网段"用网关与审计共用的
    `target_blocked`；`addresses` 是这次实际判断过的地址，便于后台显示"是哪一条中招"。
    """

    ok: bool
    reason: str
    addresses: List[str] = field(default_factory=list)


def _extra_blocked_networks() -> List[ipaddress._BaseNetwork]:
    networks: List[ipaddress._BaseNetwork] = []
    for raw in config.NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS:
        try:
            networks.append(ipaddress.ip_network(str(raw).strip(), strict=False))
        except ValueError:
            # 配置里写坏的段不该让一次登记直接 500；网络守卫那条路径会另行告警
            continue
    return networks


_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)

# SIIT 的「IPv4 转换地址」`::ffff:0:a.b.c.d`：里面同样嵌着一个 IPv4
_IPV4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")

# Python 3.11 的 `is_global` 放行、但不该当公网目标的段（`common_util` 的下载守卫已经拒了
# 站点本地 `fec0::/10`，这里对齐）。`::ffff:0:0:0/96` 另外按嵌着的 IPv4 再判一遍
_ALWAYS_BLOCKED_V6 = (
    ipaddress.ip_network("fec0::/10"),
    _IPV4_TRANSLATED,
)


def _embedded_ipv4(address: ipaddress._BaseAddress) -> List[ipaddress.IPv4Address]:
    """IPv6 地址里嵌着的 IPv4（映射、NAT64、6to4、Teredo、IPv4 兼容）"""
    if address.version != 6:
        return []
    assert isinstance(address, ipaddress.IPv6Address)
    found: List[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if any(address in network for network in _NAT64_NETWORKS) or address in _IPV4_TRANSLATED:
        found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        found.extend(address.teredo)
    if int(address) >> 32 == 0:
        # `::/96`：已废弃的 IPv4 兼容写法（含 `::` 与 `::1`）
        found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return found


def _is_blocked(address: ipaddress._BaseAddress, extra: List[ipaddress._BaseNetwork]) -> bool:
    """这个地址能不能当用户服务器的目标：不是公网地址、落进封禁网段，都算封禁"""
    if address.version == 6 and any(address in network for network in _ALWAYS_BLOCKED_V6):
        return True
    candidates: List[ipaddress._BaseAddress] = [address, *_embedded_ipv4(address)]
    for candidate in candidates:
        if not candidate.is_global or candidate.is_multicast or candidate.is_unspecified:
            return True
        cidrs = BLOCKED_IPV4_CIDRS if candidate.version == 4 else BLOCKED_IPV6_CIDRS
        for raw in cidrs:
            if candidate in ipaddress.ip_network(raw, strict=False):
                return True
        for network in extra:
            if candidate.version == network.version and candidate in network:
                return True
    return False


def _parse_literal(host: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


async def check_user_destination(host: str) -> DestinationVerdict:
    """检查一个用户登记的目标主机能不能连

    主机是 IP 字面量就直接判；否则在当前事件循环的默认执行器里解析（与沙盒回调共用
    同一套解析器，也便于测试替换 `loop.getaddrinfo`）。任何一个解析结果落在封禁网段
    就整体不通过——多地址里混一个内网地址同样危险，不能"取第一个公网的放行"。
    """
    value = host.strip()
    if not value:
        return DestinationVerdict(ok=False, reason="主机不能为空")

    extra = _extra_blocked_networks()
    literal = _parse_literal(value)
    if literal is not None:
        if _is_blocked(literal, extra):
            return DestinationVerdict(ok=False, reason=REASON_TARGET_BLOCKED, addresses=[str(literal)])
        return DestinationVerdict(ok=True, reason="", addresses=[str(literal)])

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(value, None)
    except (OSError, UnicodeError):
        return DestinationVerdict(ok=False, reason="解析不了这个主机名")

    addresses: List[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw_address = str(sockaddr[0])
        if raw_address in addresses:
            continue
        addresses.append(raw_address)
        try:
            address = ipaddress.ip_address(raw_address.split("%", 1)[0])
        except ValueError:
            # 解不成地址的结果判不了，判不了就拒
            return DestinationVerdict(ok=False, reason=REASON_TARGET_BLOCKED, addresses=addresses)
        if _is_blocked(address, extra):
            return DestinationVerdict(ok=False, reason=REASON_TARGET_BLOCKED, addresses=addresses)

    if not addresses:
        return DestinationVerdict(ok=False, reason="解析不了这个主机名")
    return DestinationVerdict(ok=True, reason="", addresses=addresses)


__all__ = ["DestinationVerdict", "check_user_destination"]
