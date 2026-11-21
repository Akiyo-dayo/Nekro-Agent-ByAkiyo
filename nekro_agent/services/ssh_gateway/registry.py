"""工作区容器与网关监听地址的登记表

## 为什么按「本地地址 + 来源地址」两个都对才认

Docker 的工作区网络是按归属分组建的，不同网络上完全可能复用同一段地址：同一个来源
地址在 A 网络上是工作区 7 的容器，在 B 网络上可能是工作区 9 的容器。只看来源地址等于
让同分组的兄弟工作区互相冒用；只看本地地址则等于认不出是谁。两个地址同时对上，才
确定"这条连接来自某张工作区网络上的某个容器"。

## 为什么同一工作区只留最新一份

容器重建会换地址、换网关专用钥匙。旧的那份必须立刻失效：留着它，旧地址上的连接还能
通过来源地址认出这个工作区；旧钥匙也还能过认证。登记表按 `workspace_id` 覆盖，天然
保证一个工作区只有一份当前事实。

## 为什么进程重启即清空

这里是纯内存表，事实来源是 Docker inspect 与容器启动流程。NA 重启后由启动流程（后续
任务）按在跑的容器重建；不做持久化，是因为持久化的旧地址只会让"已经不在的容器"继续
被认出来，比暂时认不出更危险。

## 监听地址有两种来源

一种是已经登记的容器——它的 `listen_address` 就是 NA 在这张工作区网络上的地址；另一种
是 F 在接入一张新的工作区网络时直接登记的地址，此时容器还没起来，但监听要提前就绪。
`desired_listen_addresses()` 把两种来源并起来，监督任务每拍把它交给后端同步。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Set


@dataclass(frozen=True)
class ContainerPeer:
    """一个已登记的工作区容器

    Attributes:
        workspace_id: 这个容器属于哪个工作区
        peer_address: 容器在它那张工作区网络上的 IP
        listen_address: NA 在同一张网络上、给容器连的地址（容器 ssh 配置里的 HostName）
        client_public_key: 这个容器那把网关专用钥匙的 OpenSSH 公钥行（不含注释）
        isolation_ok: 这个工作区的网络隔离是否生效
        image_ok: 容器镜像是否带网关所需的 ssh 配置
    """

    workspace_id: int
    peer_address: str
    listen_address: str
    client_public_key: str
    isolation_ok: bool
    image_ok: bool


_peers: Dict[int, ContainerPeer] = {}
# F 在接入工作区网络时直接登记的监听地址；容器起来后会再登记一份带 peer 的
_extra_listen_addresses: Set[str] = set()


def register_peer(peer: ContainerPeer) -> None:
    """登记（或覆盖）一个工作区容器；同一工作区只留最新一份"""
    _peers[int(peer.workspace_id)] = peer


def unregister_workspace(workspace_id: int) -> None:
    """容器停了或工作区删了：忘掉它，别让旧地址继续被认出来"""
    _peers.pop(int(workspace_id), None)


def resolve_peer(listen_address: str, peer_address: str) -> ContainerPeer | None:
    """按「本地地址 + 来源地址」两个都对，找出是哪个工作区的容器

    两个地址都能复用，所以必须同时匹配。反过来，同一个工作区不会登记两份，命中即当前
    事实。
    """
    local = str(listen_address or "")
    remote = str(peer_address or "")
    if not local or not remote:
        return None
    for peer in _peers.values():
        if peer.listen_address == local and peer.peer_address == remote:
            return peer
    return None


def peer_for_workspace(workspace_id: int) -> ContainerPeer | None:
    """这个工作区当前登记的容器；没有就返回 `None`"""
    return _peers.get(int(workspace_id))


def desired_listen_addresses() -> FrozenSet[str]:
    """网关现在应该监听的全部地址

    已登记容器的 `listen_address` 与 F 直接加的地址取并集。后端每一拍按它同步，集合
    相等时应当什么也不做。
    """
    addresses = {peer.listen_address for peer in _peers.values() if peer.listen_address}
    addresses.update(address for address in _extra_listen_addresses if address)
    return frozenset(addresses)


def add_listen_address(address: str) -> None:
    """登记一个要监听的地址（接入工作区网络时调用，不必等容器起来）"""
    value = str(address or "").strip()
    if value:
        _extra_listen_addresses.add(value)


def remove_listen_address(address: str) -> None:
    """撤掉一个 F 直接登记的地址；已登记容器自己的 `listen_address` 不受影响"""
    _extra_listen_addresses.discard(str(address or "").strip())


def reset_registry() -> None:
    """清空整张表（测试与关停用）"""
    _peers.clear()
    _extra_listen_addresses.clear()


__all__ = [
    "ContainerPeer",
    "add_listen_address",
    "desired_listen_addresses",
    "peer_for_workspace",
    "register_peer",
    "remove_listen_address",
    "reset_registry",
    "resolve_peer",
    "unregister_workspace",
]
