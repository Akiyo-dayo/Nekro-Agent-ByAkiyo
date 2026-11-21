"""租户出站：解析一次、校验全部结果、直连校验过的地址

## 为什么不能「先校验、再交给连接库」

`assert_tenant_endpoint_url_safe` 校验一次，httpx / 标准库连接时再解析一次，两次之间可以被 DNS 重绑：
校验时是公网，连接时是 `127.0.0.1`，NA 就真的连上了内网的某个端口。

所以租户能填的出站地址一律：解析一次 → 全部结果过校验（拒内网）→ 直接连校验过的地址 → 连上后再核对对端。
判据只有 `common_util._resolve_checked_targets` 那一份，与下载素材相同，不另写网段表：
`DOWNLOAD_EXTRA_ALLOWED_NETWORKS` 里的网段照认（默认放行透明代理的 Fake-IP 段）。

开着 Fake-IP 透明代理（TUN）的部署上，连接拿到的是假地址池里的地址，连过去之后由代理按**域名**再解析一次，
控制那个域名 DNS 的人能让代理替 NA 连进内网，连上之后核对对端也只看得到那个假地址。这条路不额外拦
（与下载一样），内网安全靠代理自己的规则；不要的部署把那一段从 `DOWNLOAD_EXTRA_ALLOWED_NETWORKS` 里删掉。

## 两条路

- **socket**（邮箱的 IMAP / SMTP）：`resolve_tenant_host` 给地址，`connect_pinned` 依次连；走 HTTP CONNECT
  代理时 `connect_via_http_proxy` 让代理连校验过的地址。TLS 的 SNI 与证书主机名由调用方按主机名做。
- **httpx**（模型组、通知渠道、MCP 服务器、插件里租户可配的地址）：`tenant_http_client()` 或
  `TenantHTTPTransport`。URL 里的主机名**不动**：`Host` 头、TLS 的 SNI 与证书主机名、连接池的分组都还是
  它；只有 httpcore 建 TCP 连接那一步换成 `TenantNetworkBackend`（解析、校验、连地址、核对对端）。
  跟随跳转时每一跳都是新连接，同样过这一步。

## 代理

- 透明代理（Fake-IP / TUN）：校验认放行网段里的假地址，连过去由代理接管（见上）。
- HTTP(S) 显式代理（调用方传的 `proxy`，或 `trust_env=True` 时的 `HTTP(S)_PROXY` / `ALL_PROXY` / `NO_PROXY`）：
  与邮箱的 `Proxied*` 同一规矩——NA 自己先解析、校验，再让代理 `CONNECT <校验过的地址>:<端口>`，TLS 仍按
  主机名在隧道里做。所以 http:// 的目标经代理时也走 CONNECT 隧道（不用转发式的绝对 URL）。
- SOCKS 代理：不支持，直接拒绝（不悄悄改成直连）。

## 公网段里的假地址池

有的代理拿一段公网地址当假地址池，从地址上认不出来，删掉放行网段也拦不住；会嗅探 SNI / Host、按域名
重新解析的代理同理：连的虽是校验过的地址，代理照样按域名另连别处。运维文档 §1.7 写明。

## 下载素材

`common_util.download_file` 借用同一套建连接办法（`download_http_transport`），判据换成下载的那一份
（`DOWNLOAD_POLICY`：认 `DOWNLOAD_EXTRA_ALLOWED_NETWORKS`、`ALLOW_PRIVATE_NETWORK_DOWNLOAD`），被拒时抛
`UnsafeDownloadUrlError` 本身而不是「连不上」。
"""

from __future__ import annotations

import asyncio
import base64
import socket
import ssl
import urllib.request
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse

import httpcore
import httpx

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.tools import common_util
from nekro_agent.tools.common_util import (
    UnresolvableDownloadHostError,
    UnsafeDownloadUrlError,
    assert_connected_peer_safe,
    assert_tenant_connected_peer_safe,
    assert_tenant_endpoint_url_safe,
    assert_tenant_host_safe,
    resolve_tenant_host,
    resolve_tenant_targets,
    tenant_target_unavailable_message,
)

logger = get_sub_logger("tenant_egress")

# 出现在报错文案里的主体名
DEFAULT_SUBJECT = "接口地址"
# 读代理 CONNECT 应答头的上限：正常应答几十字节，读不完说明对端不是代理
_PROXY_RESPONSE_LIMIT = 16 * 1024


class TenantEgressRejected(httpcore.ConnectError):
    """租户出站在建连接那一步被拒（地址不合规、解析失败、连上后对端不对）

    继承 `httpcore.ConnectError`：经 httpx 映射成 `httpx.ConnectError`，调用方按「连不上」处理。文案**不带地址**
    （可能一路回到租户眼前，带上地址就等于告诉租户 NA 连上了内网的哪台机器），地址只进日志。
    """


# ---------------------------------------------------------------------------
# socket（邮箱 IMAP / SMTP）
# ---------------------------------------------------------------------------


def connect_pinned(addresses: Sequence[str], port: int, timeout: Any) -> socket.socket:
    """按顺序连校验过的地址，连上第一个就返回；全连不上抛最后一个错误

    `timeout` 原样交给 `socket.create_connection`（`imaplib` 给 None 表示不设，`smtplib` 可能给它自己的默认哨兵）。
    """
    last_error: OSError | None = None
    for address in addresses:
        try:
            if timeout is None:
                return socket.create_connection((address, port))
            return socket.create_connection((address, port), timeout)
        except OSError as e:
            last_error = e
    raise last_error or OSError("没有可连接的地址")


def connect_via_http_proxy(host: str, port: int, proxy_url: str, timeout: int | float | None) -> socket.socket:
    """经 HTTP CONNECT 代理建一条到 `host:port` 的隧道（`host` 应是校验过的地址）；只支持 http:// 代理"""
    parsed = urlparse(proxy_url)
    # 只认 http://。https:// 代理（连代理本身也走 TLS）在这里用不了：邮件那一层的 TLS 要包在通往代理的
    # TLS 连接里面，标准库的 `wrap_socket` 包一个已经是 SSLSocket 的连接时会把外层拆掉，握手必然失败
    # （`UNEXPECTED_MESSAGE`）。与其每次都在握手处失败，不如一开始就说清楚
    if parsed.scheme == "https":
        raise RuntimeError("邮箱收发信的代理只支持 http:// 开头的 HTTP CONNECT 代理，不支持 https:// 代理；请换成 http:// 的代理地址")
    if parsed.scheme != "http":
        raise RuntimeError("IMAP/SMTP 代理目前仅支持 HTTP CONNECT 代理")
    if not parsed.hostname:
        raise RuntimeError("代理地址缺少主机名")
    proxy_port = parsed.port or 80
    raw_sock = socket.create_connection((parsed.hostname, proxy_port), timeout=timeout)

    connect_target = _connect_target(host, port)
    headers = [
        f"CONNECT {connect_target} HTTP/1.1",
        f"Host: {connect_target}",
    ]
    if parsed.username or parsed.password:
        auth = f"{parsed.username or ''}:{parsed.password or ''}"
        headers.append("Proxy-Authorization: Basic " + base64.b64encode(auth.encode()).decode())
    raw_sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = raw_sock.recv(4096)
        if not chunk:
            break
        response += chunk
    status_line = response.split(b"\r\n", 1)[0].decode(errors="ignore")
    if " 200 " not in status_line:
        raw_sock.close()
        raise RuntimeError(f"代理 CONNECT 失败: {status_line}")
    return raw_sock


def _connect_target(host: str, port: int) -> str:
    """CONNECT 的目标：IPv6 字面量要加方括号"""
    return f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"


# ---------------------------------------------------------------------------
# httpx（模型组、通知渠道、MCP、插件）
# ---------------------------------------------------------------------------


ProxySpec = Optional[str]


class EgressPolicy:
    """建连接那一步按哪一套判据：租户（`TENANT_POLICY`，默认）或下载素材（`DOWNLOAD_POLICY`）"""

    # 遇到 SOCKS 代理时照 httpx 原来的做法走（由代理按主机名连，不固定地址），而不是拒绝
    unpinned_socks_proxy: bool = False

    async def addresses(self, host: str, port: int, subject: str) -> Optional[Tuple[str, ...]]:
        """解析一次、校验全部结果，返回要连的地址（至少一个）；None 表示不固定地址（按主机名连、不核对对端）"""
        raise NotImplementedError

    def check_peer(self, peer_ip: Optional[str], subject: str) -> None:
        """连上之后核对实际对端；不合规就抛"""
        raise NotImplementedError


class _TenantPolicy(EgressPolicy):
    """租户填的地址：拒内网，认 `DOWNLOAD_EXTRA_ALLOWED_NETWORKS`（与下载同一份），不认
    `ALLOW_PRIVATE_NETWORK_DOWNLOAD`。被拒按「连不上」交回 httpx（`TenantEgressRejected`），
    「解析不到」与「指向不允许的地址」报同一句话，异常不挂异常链（分开说、或把底下那个异常挂在
    `__cause__` / `__context__` 上，都等于把 NA 那边的 DNS 结果交给租户）。"""

    async def addresses(self, host: str, port: int, subject: str) -> Optional[Tuple[str, ...]]:
        rejected = False
        ips: Tuple[Any, ...] = ()
        try:
            ips = await resolve_tenant_targets(host, port, subject)
        except UnsafeDownloadUrlError:
            rejected = True
        # 在 except 块外面抛：异常链上什么都不挂
        if rejected or not ips:
            raise TenantEgressRejected(tenant_target_unavailable_message(subject))
        return tuple(str(ip) for ip in ips)

    def check_peer(self, peer_ip: Optional[str], subject: str) -> None:
        peer_ok = False
        try:
            assert_tenant_connected_peer_safe(peer_ip)
            peer_ok = True
        except UnsafeDownloadUrlError:
            pass  # 地址已经由 assert_tenant_connected_peer_safe 记进日志
        if not peer_ok:
            raise TenantEgressRejected(f"{subject}连接的对端不是公网地址")


class _DownloadPolicy(EgressPolicy):
    """下载素材（聊天消息里的链接）：判据同 `assert_download_url_safe`——认 `DOWNLOAD_EXTRA_ALLOWED_NETWORKS`
    （默认含 Fake-IP 段），部署方开了 `ALLOW_PRIVATE_NETWORK_DOWNLOAD` 时不固定地址、不核对对端。
    被拒时抛 `UnsafeDownloadUrlError` 本身：它不是 httpcore 的异常，httpx 原样交回，`download_file` 照旧不重试，
    调用方照旧能分辨「解析失败」（`UnresolvableDownloadHostError`）。

    部署方的环境变量是 SOCKS 代理时照原来的做法走（`httpx[socks]`，由代理按主机名连）：这里没有「让 SOCKS 代理连
    校验过的地址、TLS 仍按主机名」的实现，拒绝的话开着 SOCKS 代理的部署就收不到聊天里的图片了。这条路与改动前
    一样只有首跳与逐跳的预检，没有固定地址。"""

    unpinned_socks_proxy = True

    async def addresses(self, host: str, port: int, subject: str) -> Optional[Tuple[str, ...]]:
        if OsEnv.ALLOW_PRIVATE_NETWORK_DOWNLOAD:
            return None
        ips = await common_util._resolve_checked_targets(host, port, subject)  # noqa: SLF001 - 判据只有这一份
        if not ips:
            raise UnresolvableDownloadHostError(f"{subject}主机名解析失败: {host}")
        return tuple(str(ip) for ip in ips)

    def check_peer(self, peer_ip: Optional[str], subject: str) -> None:
        assert_connected_peer_safe(peer_ip)


TENANT_POLICY: EgressPolicy = _TenantPolicy()
DOWNLOAD_POLICY: EgressPolicy = _DownloadPolicy()


class TenantNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore 建 TCP 连接的那一步：解析一次、校验全部结果、直连校验过的地址

    httpcore 把 URL 里的主机名交给 `connect_tcp`；这里不把它交给系统再解析，而是自己解析一次，全部结果过
    判据（`policy`：默认租户判据，拒内网），再依次连**校验过的地址**。直连时连上后按实际对端再核对一次。
    `proxy` 非空时（http / https 代理）先连代理，让它 `CONNECT <校验过的地址>:<端口>`；那时对端是代理本身，
    核对它没有意义，与邮箱的 `Proxied*` 一样不核对。

    例外：代理来自部署方的环境变量（`env_proxy=True`）、部署方又打开了 `EGRESS_ENV_PROXY_RESOLVES_HOSTNAME` 时，
    改发 `CONNECT <主机名>:<端口>`，由代理自己解析——给「本机 DNS 被污染 / 分区解析、靠环境代理出网」的部署。
    这时 NA 照样先按自己的解析做一次校验（与保存时的预检同一判据），但不再把校验过的地址交给代理，
    代理那边解析到哪里、能不能连进内网，靠代理自己。租户填的代理与直连不受这个开关影响。
    """

    def __init__(
        self,
        *,
        proxy: ProxySpec = None,
        pin_proxy: bool = False,
        env_proxy: bool = False,
        subject: str = DEFAULT_SUBJECT,
        inner: Optional[httpcore.AsyncNetworkBackend] = None,
        policy: Optional[EgressPolicy] = None,
    ):
        """
        Args:
            proxy: HTTP(S) 代理；None 直连
            pin_proxy: 代理本身也是租户填的（例如模型组的 `CHAT_PROXY`）：连代理时同样解析一次、校验、
                连校验过的地址并核对对端，不然代理的主机名一样能被重绑进内网。部署方给的代理（环境变量）不用
            env_proxy: 代理来自部署方的环境变量（`HTTP(S)_PROXY` / `ALL_PROXY`）；只有这种代理认
                `EGRESS_ENV_PROXY_RESOLVES_HOSTNAME`
            subject: 出现在报错文案里的主体名
            inner: 真正建 TCP 连接的后端（用例换成假的）
            policy: 判据；None 为租户判据
        """
        self._proxy = _parse_proxy(proxy) if proxy else None
        self._pin_proxy = pin_proxy
        self._env_proxy = bool(env_proxy and proxy and not pin_proxy)
        self._subject = subject
        self._inner: httpcore.AsyncNetworkBackend = inner or httpcore.AnyIOBackend()
        self._policy: EgressPolicy = policy or TENANT_POLICY

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Optional[Iterable[Any]] = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._checked_addresses(host, port, timeout)
        # None：判据说这次不固定地址（下载素材且部署方开了 ALLOW_PRIVATE_NETWORK_DOWNLOAD），按主机名连、不核对对端
        targets: Tuple[str, ...] = addresses if addresses is not None else (host,)
        if self._proxy is not None:
            if self._env_proxy and OsEnv.EGRESS_ENV_PROXY_RESOLVES_HOSTNAME:
                # 部署方声明本机 DNS 与代理看到的不一致：校验照做，CONNECT 发主机名，由代理解析
                targets = (host,)
            return await self._connect_through_proxy(targets, port, timeout, local_address, socket_options)
        return await self._connect_direct(
            targets,
            port,
            timeout,
            local_address,
            socket_options,
            check_peer=addresses is not None,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: Optional[float] = None,
        socket_options: Optional[Iterable[Any]] = None,
    ) -> httpcore.AsyncNetworkStream:
        raise TenantEgressRejected(f"{self._subject}不能连本机套接字")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)

    async def _checked_addresses(
        self,
        host: str,
        port: int,
        timeout: Optional[float],
        subject: Optional[str] = None,
    ) -> Optional[Tuple[str, ...]]:
        """按判据解析一次、校验全部结果（被拒怎么报由判据决定，见 `EgressPolicy`）；解析超时按连接超时交回"""
        label = subject or self._subject
        timed_out = False
        addresses: Optional[Tuple[str, ...]] = None
        try:
            resolving = self._policy.addresses(host, int(port), label)
            addresses = await (asyncio.wait_for(resolving, timeout) if timeout else resolving)
        except asyncio.TimeoutError:
            timed_out = True
        if timed_out:
            raise httpcore.ConnectTimeout(f"{label}主机名解析超时")
        return addresses

    async def _connect_direct(
        self,
        addresses: Sequence[str],
        port: int,
        timeout: Optional[float],
        local_address: Optional[str],
        socket_options: Optional[Iterable[Any]],
        *,
        check_peer: bool = True,
    ) -> httpcore.AsyncNetworkStream:
        last_error: Optional[Exception] = None
        for address in addresses:
            try:
                stream = await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last_error = e
                continue
            if check_peer:
                try:
                    peer = stream.get_extra_info("server_addr")
                    self._policy.check_peer(str(peer[0]) if peer else None, self._subject)
                except BaseException:
                    await stream.aclose()
                    raise
            return stream
        if isinstance(last_error, (httpcore.ConnectError, httpcore.ConnectTimeout)):
            raise last_error
        raise httpcore.ConnectError(f"连不上{self._subject}") from last_error

    async def _connect_through_proxy(
        self,
        addresses: Sequence[str],
        port: int,
        timeout: Optional[float],
        local_address: Optional[str],
        socket_options: Optional[Iterable[Any]],
    ) -> httpcore.AsyncNetworkStream:
        assert self._proxy is not None
        proxy = self._proxy
        proxy_addresses: Optional[Tuple[str, ...]] = None
        if self._pin_proxy:
            proxy_addresses = await self._checked_addresses(proxy.host, proxy.port, timeout, subject="代理地址")
            if proxy_addresses is None:  # 只有下载判据会回 None，而下载不会有租户填的代理；防御一下
                raise TenantEgressRejected(tenant_target_unavailable_message("代理地址"))
        last_error: Optional[Exception] = None
        for address in addresses:
            if proxy_addresses is not None:
                stream = await self._connect_direct(proxy_addresses, proxy.port, timeout, local_address, socket_options)
            else:
                stream = await self._inner.connect_tcp(
                    proxy.host,
                    proxy.port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            try:
                if proxy.tls:
                    stream = await stream.start_tls(ssl.create_default_context(), server_hostname=proxy.host, timeout=timeout)
                await _proxy_connect_handshake(stream, _connect_target(address, port), proxy.authorization, timeout)
            except httpcore.ProxyError as e:
                await stream.aclose()
                last_error = e
                continue
            except BaseException:
                await stream.aclose()
                raise
            return stream
        if last_error is not None:
            raise last_error
        raise httpcore.ProxyError("代理 CONNECT 失败")


class _ProxyEndpoint:
    __slots__ = ("authorization", "host", "port", "tls")

    def __init__(self, host: str, port: int, tls: bool, authorization: str):
        self.host = host
        self.port = port
        self.tls = tls
        self.authorization = authorization


def _with_proxy_scheme(proxy_url: ProxySpec) -> ProxySpec:
    """没写协议的代理地址（`HTTPS_PROXY=127.0.0.1:7890` 这种常见写法）按 `http://` 补上，与 httpx 读环境变量的口径一致"""
    if not proxy_url:
        return None
    text = str(proxy_url).strip()
    if not text:
        return None
    return text if "://" in text else f"http://{text}"


def _is_socks_proxy(proxy_url: str) -> bool:
    return (urlparse(str(proxy_url)).scheme or "").lower().startswith("socks")


def _parse_proxy(proxy_url: str) -> _ProxyEndpoint:
    parsed = urlparse(str(proxy_url))
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        # SOCKS 等：不悄悄改成直连（那会换掉部署方选的出口），也不去连（没有按地址 CONNECT 的实现）
        raise httpx.ProxyError("这类出站只能直连或经 HTTP(S) 代理访问，不支持这种代理")
    if not parsed.hostname:
        raise httpx.ProxyError("代理地址缺少主机名")
    authorization = ""
    if parsed.username or parsed.password:
        raw = f"{parsed.username or ''}:{parsed.password or ''}"
        authorization = "Basic " + base64.b64encode(raw.encode()).decode()
    return _ProxyEndpoint(parsed.hostname, parsed.port or (443 if scheme == "https" else 80), scheme == "https", authorization)


async def _proxy_connect_handshake(
    stream: httpcore.AsyncNetworkStream,
    target: str,
    authorization: str,
    timeout: Optional[float],
) -> None:
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
    if authorization:
        lines.append(f"Proxy-Authorization: {authorization}")
    await stream.write(("\r\n".join(lines) + "\r\n\r\n").encode(), timeout=timeout)
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = await stream.read(4096, timeout=timeout)
        if not chunk:
            break
        response += chunk
        if len(response) > _PROXY_RESPONSE_LIMIT:
            break
    status_line = response.split(b"\r\n", 1)[0].decode(errors="ignore")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or parts[1] != "200":
        raise httpcore.ProxyError(f"代理 CONNECT 失败: {status_line[:80]}")


_POOL_ATTRIBUTES = (
    "_ssl_context",
    "_max_connections",
    "_max_keepalive_connections",
    "_keepalive_expiry",
    "_http1",
    "_http2",
    "_retries",
    "_local_address",
    "_socket_options",
)


class TenantHTTPTransport(httpx.AsyncBaseTransport):
    """租户出站的 httpx 传输层：每个请求按它的代理归属，交给对应的一条 `TenantNetworkBackend` 连接池

    代理按请求的协议与主机选（`proxy` 显式给了就一律用它；否则 `trust_env=True` 时读环境变量，
    `NO_PROXY` 命中的直连），与 httpx 自己读环境变量的口径一致。每种代理一个内层
    `httpx.AsyncHTTPTransport`，它的 httpcore 连接池换成带 `TenantNetworkBackend` 的那一个；
    其余（证书校验、HTTP/2、连接上限）照调用方给的参数。
    """

    def __init__(
        self,
        *,
        proxy: ProxySpec = None,
        proxy_is_tenant: bool = False,
        trust_env: bool = True,
        subject: str = DEFAULT_SUBJECT,
        backend: Optional[httpcore.AsyncNetworkBackend] = None,
        policy: Optional[EgressPolicy] = None,
        **transport_kwargs: Any,
    ):
        if "uds" in transport_kwargs or "proxies" in transport_kwargs:
            raise ValueError("租户出站不支持本机套接字与 proxies 参数")
        proxy = _with_proxy_scheme(proxy or None)
        if proxy is not None:
            _parse_proxy(proxy)  # 不支持的代理在建客户端时就报，而不是第一次请求时
        self._proxy = proxy
        self._proxy_is_tenant = bool(proxy_is_tenant and proxy)
        self._trust_env = trust_env
        self._subject = subject
        self._inner_backend = backend
        self._policy: EgressPolicy = policy or TENANT_POLICY
        self._transport_kwargs = dict(transport_kwargs)
        self._transport_kwargs.setdefault("trust_env", trust_env)
        self._transports: Dict[ProxySpec, httpx.AsyncHTTPTransport] = {}

    def proxy_for(self, url: httpx.URL) -> ProxySpec:
        """这个请求走哪个代理；None 表示直连"""
        if self._proxy is not None:
            return self._proxy
        if not self._trust_env:
            return None
        proxies = urllib.request.getproxies_environment()
        if not proxies:
            return None
        host = url.host
        if proxies.get("no") and urllib.request.proxy_bypass_environment(host, proxies):
            return None
        return _with_proxy_scheme(proxies.get(url.scheme) or proxies.get("all") or None)

    def _transport(self, proxy: ProxySpec) -> httpx.AsyncHTTPTransport:
        transport = self._transports.get(proxy)
        if transport is None and proxy is not None and self._policy.unpinned_socks_proxy and _is_socks_proxy(proxy):
            # 只有下载判据会走到这里（见 `_DownloadPolicy`）：SOCKS 代理照 httpx 原来的做法
            transport = httpx.AsyncHTTPTransport(proxy=proxy, **self._transport_kwargs)
            self._transports[proxy] = transport
        if transport is None:
            transport = httpx.AsyncHTTPTransport(**self._transport_kwargs)
            pool = transport._pool  # noqa: SLF001 - 只换建连接那一步，其余沿用 httpx 按参数建好的连接池设置
            missing = [name for name in _POOL_ATTRIBUTES if not hasattr(pool, name)]
            if missing:
                raise RuntimeError(f"httpcore 连接池的内部字段变了（{missing}），租户出站不能安全地换掉建连接那一步")
            transport._pool = httpcore.AsyncConnectionPool(  # noqa: SLF001
                ssl_context=pool._ssl_context,  # noqa: SLF001
                max_connections=pool._max_connections,  # noqa: SLF001
                max_keepalive_connections=pool._max_keepalive_connections,  # noqa: SLF001
                keepalive_expiry=pool._keepalive_expiry,  # noqa: SLF001
                http1=pool._http1,  # noqa: SLF001
                http2=pool._http2,  # noqa: SLF001
                retries=pool._retries,  # noqa: SLF001
                local_address=pool._local_address,  # noqa: SLF001
                socket_options=pool._socket_options,  # noqa: SLF001
                network_backend=TenantNetworkBackend(
                    proxy=proxy,
                    # 只有调用方显式给的、租户填的那个代理要按租户地址连；环境变量里的代理是部署方的
                    pin_proxy=self._proxy_is_tenant and proxy == self._proxy,
                    # 调用方没给代理、这一条是按环境变量选出来的：部署方的代理
                    env_proxy=self._proxy is None and proxy is not None,
                    subject=self._subject,
                    inner=self._inner_backend,
                    policy=self._policy,
                ),
            )
            self._transports[proxy] = transport
        return transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme not in ("http", "https"):
            raise httpx.UnsupportedProtocol(f"{self._subject}只支持 http / https")
        return await self._transport(self.proxy_for(request.url)).handle_async_request(request)

    async def aclose(self) -> None:
        transports = list(self._transports.values())
        self._transports.clear()
        for transport in transports:
            await transport.aclose()


def tenant_http_client(
    *,
    proxy: ProxySpec = None,
    proxy_is_tenant: bool = False,
    trust_env: bool = True,
    verify: Union[bool, str, ssl.SSLContext] = True,
    http2: bool = False,
    limits: httpx.Limits = httpx.Limits(max_connections=100, max_keepalive_connections=20),
    subject: str = DEFAULT_SUBJECT,
    backend: Optional[httpcore.AsyncNetworkBackend] = None,
    client_class: type = httpx.AsyncClient,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """给租户填的地址用的 `httpx.AsyncClient`

    `proxy` / `trust_env` / `verify` 照调用方原来的语义传；`proxy_is_tenant=True` 表示 `proxy` 也是租户填的
    （连它时同样按租户地址处理）。客户端自己的 `trust_env` 恒为 False——环境代理由传输层按上面的规矩处理，
    客户端再挂一层环境代理就会绕过它。其余参数（`timeout`、`headers`、`follow_redirects`、`event_hooks` 等）
    原样交给客户端。`backend` 只给用例换掉真正建连接的那一层。`client_class` 是 `httpx.AsyncClient` 的子类
    （模型组连接池要记在途请求数，见 `services/agent/openai.py`）。
    """
    if "transport" in client_kwargs or "mounts" in client_kwargs or "proxies" in client_kwargs:
        raise ValueError("租户出站的客户端不能另给 transport / mounts / proxies")
    transport = TenantHTTPTransport(
        proxy=proxy,
        proxy_is_tenant=proxy_is_tenant,
        trust_env=trust_env,
        subject=subject,
        backend=backend,
        verify=verify,
        http2=http2,
        limits=limits,
    )
    return client_class(transport=transport, trust_env=False, **client_kwargs)


DOWNLOAD_SUBJECT = "下载链接"


def download_http_transport(
    *,
    backend: Optional[httpcore.AsyncNetworkBackend] = None,
    proxy: ProxySpec = None,
    tenant: bool = False,
) -> TenantHTTPTransport:
    """下载素材（`common_util.download_file`）用的传输层：与租户出站同一套建连接办法，判据换成下载的那一份

    解析一次、按 `assert_download_url_safe` 的判据校验全部结果、直连校验过的地址并核对对端；环境变量里的
    HTTP(S) 代理照用（`trust_env` 的口径与原来一样），经代理时让它 `CONNECT <校验过的地址>:<端口>`。
    `proxy` 是部署方配的代理（例如系统默认代理）：给了就只走它、不读环境变量，同样 `CONNECT` 校验过的
    地址；只支持 HTTP(S) 代理，别的在这里就抛 `httpx.ProxyError`。

    `tenant=True`：下载是租户触发的（地址来自租户上传的东西），判据换成租户那一份（`TENANT_POLICY`）：
    放行网段仍是 `DOWNLOAD_EXTRA_ALLOWED_NETWORKS` 那一份，但不认 `ALLOW_PRIVATE_NETWORK_DOWNLOAD`（那是部署方
    对自己拉素材的授权，不顺延给租户），环境变量里的代理是 SOCKS 时直接拒（没有让 SOCKS 代理连校验过的地址的
    实现，不固定地址就能经代理被 DNS 重绑）。被拒按「连不上」交回。

    `backend` 只给用例换掉真正建连接的那一层。客户端自己要 `trust_env=False`（见 `tenant_http_client`）。
    """
    policy = TENANT_POLICY if tenant else DOWNLOAD_POLICY
    if proxy:
        return TenantHTTPTransport(
            proxy=proxy, trust_env=False, subject=DOWNLOAD_SUBJECT, backend=backend, policy=policy
        )
    return TenantHTTPTransport(trust_env=True, subject=DOWNLOAD_SUBJECT, backend=backend, policy=policy)


# 模型组的归属登记。运行时视图 `ModelConfigGroup` 上没有归属字段，频道的有效配置里拿到的又可能是拷贝——
# 一轮对话在整个 Agent 循环里都拿着同一份副本，租户中途改了组（哪怕只改密钥）或删掉，旧副本就谁都对不上了。
# 所以登记是**只增不减**的：`model_group_service.refresh_runtime_view` 每次按库重建视图时，把当时的租户组、
# 平台组（组名，与「地址, 密钥, 代理」三项）都加进去，从不删除。判定**失败按租户**：
# 只有「登记过是平台方，而且从没以租户的身份出现过」的才按平台方，其余（见过是租户的、从没见过的）一律按租户
_seen_tenant_names: set = set()
_seen_platform_names: set = set()
_seen_tenant_endpoints: set = set()
_seen_platform_endpoints: set = set()


def _endpoint(base_url: Any, api_key: Any, proxy: Any) -> Tuple[str, str, str]:
    return (
        str(base_url or "").strip().rstrip("/"),
        str(api_key or "").strip(),
        str(proxy or "").strip(),
    )


def _group_endpoint(group: Any) -> Tuple[str, str, str]:
    return _endpoint(getattr(group, "BASE_URL", ""), getattr(group, "API_KEY", ""), getattr(group, "CHAT_PROXY", ""))


def register_model_groups(
    *,
    tenant: Iterable[Tuple[str, Any]],
    platform: Iterable[Tuple[str, Any]],
) -> None:
    """登记当前的租户组与平台组：`(组名, ModelConfigGroup)`；只增不减"""
    for name, group in tenant:
        _seen_tenant_names.add(str(name))
        _seen_tenant_endpoints.add(_group_endpoint(group))
    for name, group in platform:
        _seen_platform_names.add(str(name))
        _seen_platform_endpoints.add(_group_endpoint(group))


def reset_model_group_registry() -> None:
    """清空登记（只给用例用；生产里登记只增不减）"""
    _seen_tenant_names.clear()
    _seen_platform_names.clear()
    _seen_tenant_endpoints.clear()
    _seen_platform_endpoints.clear()


def is_tenant_owned_group(group: Any) -> bool:
    """模型组归不归租户：租户建的组，它的地址与代理都按租户出站处理；平台方的组照旧

    `group` 可以是组名或组对象。组对象按连接参数（地址、密钥、代理）认，拷贝也认得出。

    **判不出来按租户**：只有「登记过是平台方、而且从没以租户的身份出现过」的组名 / 连接参数才按平台方。
    一轮里拿着的旧副本在租户改了密钥或删了组之后仍按租户；平台方的组只有与某个（现在或以前的）租户组三项
    完全相同（连密钥都一样）时才会被一起收紧，只会更严。直接拿请求体里的地址去连的接口（模型组的 inline 测试、
    拉模型列表）不走这里，由调用方按当前用户是不是平台方决定。
    """
    if group is None:
        return True
    if isinstance(group, str):
        return group in _seen_tenant_names or group not in _seen_platform_names
    endpoint = _group_endpoint(group)
    return endpoint in _seen_tenant_endpoints or endpoint not in _seen_platform_endpoints


def is_tenant_owned_endpoint(base_url: Any, api_key: Any, proxy: Any) -> bool:
    """按连接参数（地址、密钥、代理）判归属，给只拿到这三项的底层接口用（`services/agent/openai.py`）

    口径同 `is_tenant_owned_group`：只有登记过是平台方、从没以租户身份出现过的三项才按平台方，判不出来按租户。
    """
    endpoint = _endpoint(base_url, api_key, proxy)
    return endpoint in _seen_tenant_endpoints or endpoint not in _seen_platform_endpoints


__all__ = [
    "DEFAULT_SUBJECT",
    "DOWNLOAD_POLICY",
    "DOWNLOAD_SUBJECT",
    "TENANT_POLICY",
    "EgressPolicy",
    "TenantEgressRejected",
    "TenantHTTPTransport",
    "TenantNetworkBackend",
    "UnresolvableDownloadHostError",
    "UnsafeDownloadUrlError",
    "assert_tenant_connected_peer_safe",
    "assert_tenant_endpoint_url_safe",
    "assert_tenant_host_safe",
    "connect_pinned",
    "connect_via_http_proxy",
    "download_http_transport",
    "is_tenant_owned_endpoint",
    "is_tenant_owned_group",
    "register_model_groups",
    "reset_model_group_registry",
    "resolve_tenant_host",
    "tenant_http_client",
]
