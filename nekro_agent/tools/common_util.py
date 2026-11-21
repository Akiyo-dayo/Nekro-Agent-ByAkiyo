import asyncio
import base64
import difflib
import hashlib
import ipaddress
import mimetypes
import random
import re
import socket
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple, Union
from urllib.parse import urljoin

import aiofiles
import httpx
import magic
import toml
from PIL import Image

from nekro_agent.core import logger
from nekro_agent.core.config import CoreConfig
from nekro_agent.core.os_env import USER_UPLOAD_DIR, OsEnv
from nekro_agent.tools.path_convertor import (
    UnsafeUploadPathError,
    is_url_path,
    safe_upload_file_name,
    sanitize_chat_key_for_path,
)

_APP_VERSION: str = ""

# 外部资源下载约束：下载链接可能完全由消息发送方构造（如合并转发内嵌的图片 URL），
# 缺少这些限制时可被用于内网探测、云元数据窃取或磁盘打爆。
ALLOWED_DOWNLOAD_SCHEMES = frozenset({"http", "https"})
DOWNLOAD_TIMEOUT_SECONDS = 20.0
MAX_DOWNLOAD_SIZE_BYTES = 64 * 1024 * 1024
# 跳转由 download_file 手动跟随、每一跳重新校验（httpx 自己跟跳转时不会再过校验），超过这个次数按失败处理
MAX_DOWNLOAD_REDIRECTS = 3
_DOWNLOAD_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
# 只收未压缩的正文：httpx 解压 gzip / deflate 时不限输出长度，`Content-Encoding: gzip, gzip` 的几十 KB
# 一次读就能展开成几十 GB，等不到边读边计数就把进程撑爆。服务端不理 identity 照样压缩的，按失败处理
_DOWNLOAD_REQUEST_HEADERS = {"Accept-Encoding": "identity"}
_DEFAULT_SCHEME_PORTS = {"http": 80, "https": 443}


# 默认额外放行的网段（与 OsEnv.DOWNLOAD_EXTRA_ALLOWED_NETWORKS 的默认值保持一致）
#
# 这个防护要防的是"攻击者把内网地址塞进聊天消息的图片链接，借 NA 的网络位置探测部署方内网"。
# 威胁面是**部署方网络里真实存在的服务**：回环上的管理端口、RFC1918 的内网设备、
# 169.254.169.254 云元数据。`ip.is_global` 恰好覆盖这些，所以它是个不错的默认判据 —— 但它
# 把"保留/未分配"和"内网"混为一谈，而透明代理的 Fake-IP 段正好落在前者里。
#
# 198.18.0.0/15 是 RFC 2544 的基准测试保留段（Clash 默认的 198.18.0.1/16 也在其中），
# Shadowrocket / Clash 等把它当 Fake-IP 池用：DNS 返回这一段里的一个假地址，连接被代理接管、
# 按**原域名**转发出去。也就是说这个 IP 根本不代表一台主机。放进威胁模型看：
#   1. 部署方没开代理时，RFC 2544 规定这一段不得出现在公网，连过去只会超时 —— 攻击者拿不到
#      任何信息增量，和现在被拒绝的结果没有区别；
#   2. 部署方开了代理时，流量由代理按域名转发，探测不到部署方内网的任何服务。
# 两种情况下它都不在本防护要防的范围内，所以默认把它从"内网"里摘出来，让开了代理的普通部署
# 开箱可用。真正的威胁面（回环 / RFC1918 / 链路本地 / CGNAT / 组播 等）一律仍然拒绝。
#
# 顺带说明两个不需要特殊处理的网段：28.0.0.0/8 已分配给 DoD，是公网地址，`is_global` 本来就
# 放行；100.64.0.0/10（CGNAT，Tailscale 也用它）仍被拒绝 —— 它确实可能对应部署方网络里的真实
# 主机，属于威胁面，需要的部署应显式写进 DOWNLOAD_EXTRA_ALLOWED_NETWORKS。
DEFAULT_EXTRA_ALLOWED_NETWORKS = "198.18.0.0/15"

_IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
_IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# `is_global` 对这几类返回 True，而它们能绕到 IPv4 内网：地址里嵌着 IPv4（`64:ff9b::7f00:1` 经 NAT64
# 就是 127.0.0.1，`::127.0.0.1` 是兼容格式，`::ffff:0:7f00:1` 是 SIIT 的 IPv4-translated 格式，
# `2002::/16` 是 6to4），`fec0::/10` 是废弃的站点本地段。
# 都不是正常公网服务会用的地址，整段拒绝，要用的部署写进 DOWNLOAD_EXTRA_ALLOWED_NETWORKS。
_DENIED_IPV6_NETWORKS: Tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.IPv6Network(entry)
    for entry in ("::/96", "::ffff:0:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "fec0::/10")
)

# 透明代理的 Fake-IP 段（见 DEFAULT_EXTRA_ALLOWED_NETWORKS 的说明）。下载素材默认放行它，但**租户自填的
# 邮件服务器**不行：连到这一段时对端永远是那个假地址，代理再按域名自己去解析——租户控制那个域名的 DNS，
# 就能让代理替 NA 连进内网，连接后的对端核对也只看得到假地址。所以租户这一侧对它一律拒绝，
# 不管 DOWNLOAD_EXTRA_ALLOWED_NETWORKS 里写没写它。平台方账户不受影响
_FAKE_IP_NETWORKS: Tuple[ipaddress.IPv4Network, ...] = (ipaddress.IPv4Network("198.18.0.0/15"),)


class UnsafeDownloadUrlError(ValueError):
    """下载链接指向不允许访问的目标"""


class UnresolvableDownloadHostError(UnsafeDownloadUrlError):
    """主机名解析失败：不是策略拒绝，调用方需要时可以和“指向内网”区分开（仍是 `UnsafeDownloadUrlError`，老的捕获不受影响）"""


@lru_cache(maxsize=8)
def parse_extra_allowed_networks(raw: str) -> Tuple[_IPNetwork, ...]:
    """解析 DOWNLOAD_EXTRA_ALLOWED_NETWORKS 配置为网段列表

    单个条目写错不应让整个部署起不来，因此非法条目跳过并告警。按原始字符串缓存，
    既避免每次下载都重新解析，也保证告警只在配置变化时打一次。

    Args:
        raw: 逗号分隔的 CIDR 列表，可含空白；空串表示不额外放行任何网段

    Returns:
        Tuple[_IPNetwork, ...]: 解析成功的网段
    """
    networks: list[_IPNetwork] = []
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            logger.warning(f"DOWNLOAD_EXTRA_ALLOWED_NETWORKS 含非法网段，已忽略: {entry!r}")
            continue
        # 写进这里的网段等于部署方声明"它不是我的内网"，因此对下载与租户提交的接口地址一并生效。
        # 覆盖到真实内网时不拦（细粒度放行单台内网机器是合理用法，比开 ALLOW_PRIVATE_NETWORK_DOWNLOAD
        # 放开整个内网更安全），但必须告警，避免有人误以为这只是个"兼容代理"的无害开关。
        if not network.is_global:
            logger.warning(
                f"DOWNLOAD_EXTRA_ALLOWED_NETWORKS 放行了非公网网段 {network}，"
                f"该网段内的地址将对下载链接与租户接口地址一并开放，请确认这是预期行为",
            )
        networks.append(network)
    return tuple(networks)


def _normalize_ip(raw: str) -> _IPAddress:
    """把 getaddrinfo 返回的地址串转成用于判定的 IP 对象

    IPv4-mapped IPv6（`::ffff:198.18.4.49`）还原为 IPv4，否则网段包含判定会因协议族不同而漏判。
    """
    ip = ipaddress.ip_address(raw)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _is_denied_ipv6_form(ip: _IPAddress) -> bool:
    return ip.version == 6 and any(ip in network for network in _DENIED_IPV6_NETWORKS)


def _is_allowed_target_ip(ip: _IPAddress, *, exclude_fake_ip: bool = False) -> bool:
    """判断出站目标 IP 是否允许访问

    `exclude_fake_ip=True`（租户自填的邮件服务器）：只认真正的公网地址——198.18.0.0/15 一律拒绝，
    `DOWNLOAD_EXTRA_ALLOWED_NETWORKS` 里的非公网网段也**不认**：那些网段多半是部署方的透明代理 / TUN
    地址池或内网，连过去之后由代理按域名再解析，等于把「连到哪里」交给了租户的 DNS。
    落在公网段里的假地址池（有的代理用 28.0.0.0/8 这类段）从地址上认不出来：开着 Fake-IP / TUN 透明代理
    的部署，实例自填邮箱仍不安全（运维文档 §1.7 写明）。
    """
    if exclude_fake_ip:
        if any(ip.version == network.version and ip in network for network in _FAKE_IP_NETWORKS):
            return False
        return ip.is_global and not _is_denied_ipv6_form(ip)
    if ip.is_global and not _is_denied_ipv6_form(ip):
        return True
    return any(ip.version == network.version and ip in network for network in parse_extra_allowed_networks(OsEnv.DOWNLOAD_EXTRA_ALLOWED_NETWORKS))


def _split_outbound_url(url: str, subject: str) -> Tuple[str, str, int]:
    """拆出协议、主机、端口，口径与 httpx 发起连接时一致

    主机取 httpx 的 `raw_host`（ASCII），不能用 `urlparse(...).hostname`：后者是 Unicode，交给 getaddrinfo 时按
    Python 自带的 IDNA 2003 编码，httpx 连接时按 IDNA 2008 编码，`ß` / `ς` 这类字符两边编出两个不同的域名
    （`ss.example` 与 `xn--zca.example`）。攻击者让前者解析到公网、后者解析到内网，就能不靠任何时间窗口地绕过校验。

    Raises:
        UnsafeDownloadUrlError: 不是合法 URL、协议不被允许或缺少主机名
    """
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL as e:
        raise UnsafeDownloadUrlError(f"{subject}不是合法的 URL") from e
    scheme = target.scheme
    if scheme not in ALLOWED_DOWNLOAD_SCHEMES:
        raise UnsafeDownloadUrlError(f"不支持的{subject}协议: {scheme or '(空)'}")
    host = target.raw_host.decode("ascii")
    if not host:
        raise UnsafeDownloadUrlError(f"{subject}缺少主机名")
    return scheme, host, target.port or _DEFAULT_SCHEME_PORTS[scheme]


async def _assert_url_targets_public_network(url: str, subject: str) -> None:
    """校验 URL 的协议与解析结果，确保出站请求不会打进内网

    仅允许 http/https，且解析出的**所有** IP 都必须是公网地址（或落在
    DOWNLOAD_EXTRA_ALLOWED_NETWORKS 显式声明的非内网网段里，见 DEFAULT_EXTRA_ALLOWED_NETWORKS）。
    逐个校验全部解析结果，避免域名同时返回公网与内网地址时被绕过。

    Args:
        url: 待校验的链接
        subject: 出现在报错文案里的主体名（如 "下载链接" / "接口地址"）

    Raises:
        UnsafeDownloadUrlError: 协议不被允许、主机无法解析或指向非公网地址
    """
    _scheme, host, port = _split_outbound_url(url, subject)
    await _resolve_checked_targets(host, port, subject)


async def _resolve_checked_targets(
    host: str,
    port: int,
    subject: str,
    *,
    exclude_fake_ip: bool = False,
) -> Tuple[_IPAddress, ...]:
    """解析一次主机名，逐个校验**全部**结果，返回去重后的地址（按解析顺序）

    要「只解析一次、直接连校验过的那个地址」的调用方拿返回值去连（见 `resolve_tenant_host`、
    `fetch_url_bytes`）：校验一次、连接时再解析一次，两次之间可以被 DNS 重绑。

    Raises:
        UnresolvableDownloadHostError: 解析失败
        UnsafeDownloadUrlError: 任何一个结果不允许访问
    """
    try:
        addr_infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        # UnicodeError：httpx 放行、但 getaddrinfo 的 idna 编码拒绝的主机名（空标签 `a..b`、超过 63 字符的标签），没有发出查询
        raise UnresolvableDownloadHostError(f"{subject}主机名解析失败: {host}") from e

    resolved: list[_IPAddress] = []
    for addr_info in addr_infos:
        ip = _normalize_ip(addr_info[4][0])
        if not _is_allowed_target_ip(ip, exclude_fake_ip=exclude_fake_ip):
            # 调用方普遍以 debug 级别吞掉下载异常，这里主动升级日志级别，
            # 既保证安全事件可见，也避免正常部署被拦截时无从排查
            hint = (
                "（租户自填的服务器不接受透明代理的 Fake-IP 段）"
                if exclude_fake_ip and any(ip.version == n.version and ip in n for n in _FAKE_IP_NETWORKS)
                else "（若这是透明代理的 Fake-IP，请把对应网段写入 NEKRO_DOWNLOAD_EXTRA_ALLOWED_NETWORKS）"
            )
            logger.warning(f"拒绝指向非公网地址的{subject}: {host} -> {ip}{hint}")
            raise UnsafeDownloadUrlError(f"{subject}指向非公网地址: {host} -> {ip}")
        if ip not in resolved:
            resolved.append(ip)
    return tuple(resolved)


async def assert_download_url_safe(url: str) -> None:
    """校验下载链接可安全访问

    Args:
        url: 待校验的下载链接

    Raises:
        UnsafeDownloadUrlError: 协议不被允许、主机无法解析或指向非公网地址
    """
    if OsEnv.ALLOW_PRIVATE_NETWORK_DOWNLOAD:
        # 该开关只放宽"下载素材"这一类出站，且仅由部署方设置。多租户下由租户填写的地址
        # 不走这里，见 assert_tenant_endpoint_url_safe
        _split_outbound_url(url, "下载链接")
        return
    await _assert_url_targets_public_network(url, "下载链接")


def assert_connected_peer_safe(peer_ip: Optional[str]) -> None:
    """校验已经建立的连接的对端 IP

    `assert_download_url_safe` 解析一次，httpx 连接时再解析一次，DNS 重绑定就发生在这两次之间。
    读响应体之前按实际对端再判一次，取不到对端按不安全处理。

    Raises:
        UnsafeDownloadUrlError: 对端不是允许访问的地址，或取不到对端地址
    """
    if OsEnv.ALLOW_PRIVATE_NETWORK_DOWNLOAD:
        return
    try:
        ip = _normalize_ip(peer_ip or "")
    except ValueError as e:
        raise UnsafeDownloadUrlError("无法确认下载连接的对端地址") from e
    if not _is_allowed_target_ip(ip):
        logger.warning(f"拒绝连接到非公网对端的下载: {ip}")
        raise UnsafeDownloadUrlError("下载连接的对端不是公网地址")


def assert_tenant_connected_peer_safe(peer_ip: Optional[str], *, exclude_fake_ip: bool = False) -> None:
    """校验租户发起的连接的实际对端 IP

    与 `assert_connected_peer_safe` 的差别同 `assert_tenant_endpoint_url_safe` 与
    `assert_download_url_safe`：**不认 `ALLOW_PRIVATE_NETWORK_DOWNLOAD`**。那个开关是部署方
    对自己「允许从内网拉素材」的授权，把它顺延成「允许租户连内网」就是越权通道。

    `DOWNLOAD_EXTRA_ALLOWED_NETWORKS` 仍然生效（判据沿用 `_is_allowed_target_ip`）：它显式
    声明的是「这些非内网网段算安全」，与部署方是否允许内网下载是两件事。`exclude_fake_ip=True`
    时 Fake-IP 段一律拒绝（租户自填的邮件服务器，见 `_FAKE_IP_NETWORKS`）。

    报错文案**不带地址**：它可能一路回到租户眼前，带上地址就等于告诉租户「NA 连上了内网的哪台机器」。
    地址只进日志。

    Raises:
        UnsafeDownloadUrlError: 对端不是允许访问的地址，或取不到对端地址
    """
    try:
        ip = _normalize_ip(peer_ip or "")
    except ValueError as e:
        raise UnsafeDownloadUrlError("无法确认连接的对端地址") from e
    if not _is_allowed_target_ip(ip, exclude_fake_ip=exclude_fake_ip):
        logger.warning(f"拒绝连接到非公网对端的租户出站连接: {ip}")
        raise UnsafeDownloadUrlError("连接的对端不是公网地址")


async def assert_tenant_endpoint_url_safe(url: str) -> None:
    """校验租户提交的出站接口地址

    租户能填任意 `base_url`，等于拿到"从 NA 服务器发起出站请求"的能力：填
    `http://127.0.0.1:8021/api/...` 就能用 NA 自己的网络身份打内网。因此租户侧一律强制公网校验。

    与 `assert_download_url_safe` 的差别只有一处：**不认 `ALLOW_PRIVATE_NETWORK_DOWNLOAD`**。
    那个开关的语义是"本部署允许从内网拉取素材"，是部署方对自己的授权；把它顺延成
    "允许租户打内网"会让一个运维便利开关变成越权通道。

    Raises:
        UnsafeDownloadUrlError: 协议不被允许、主机无法解析或指向非公网地址
    """
    await _assert_url_targets_public_network(url, "接口地址")


async def assert_tenant_host_safe(
    host: str,
    port: int,
    *,
    subject: str = "接口地址",
    exclude_fake_ip: bool = False,
) -> None:
    """按「主机名 + 端口」做与 `assert_tenant_endpoint_url_safe` 完全相同的公网校验

    给不是 URL 的出站连接用（邮箱的自定义 IMAP/SMTP 服务器只填主机与端口）。只做校验、不要
    地址的调用方用它；要连接的调用方用 `resolve_tenant_host`，拿校验过的地址直接去连。

    Raises:
        UnsafeDownloadUrlError: 主机无法解析或指向非公网地址，或主机名不合法
    """
    await resolve_tenant_host(host, port, subject=subject, exclude_fake_ip=exclude_fake_ip)


async def resolve_tenant_host(
    host: str,
    port: int,
    *,
    subject: str = "接口地址",
    exclude_fake_ip: bool = False,
) -> Tuple[str, ...]:
    """校验租户填的「主机名 + 端口」，返回**校验过的**地址（按解析顺序，至少一个）

    只解析一次：调用方必须直接连返回的地址（TLS 的 SNI 与证书主机名仍用原主机名），不能再把
    主机名交给连接库去解析一遍——校验与连接各解析一次，两次之间可以被 DNS 重绑，NA 就真的
    连上了内网的某个端口（哪怕之后的对端核对把它断开，「连上了」本身已经可以拿来探测内网）。

    **不另写一套网段表**：判定复用 `_resolve_checked_targets` 那一份，主机名统一从
    `httpx.URL` 的 `raw_host` 取（与 httpx 真正发起连接时的 IDNA 编码同一来源）。

    主机名只许「一个裸主机名」或方括号括起来的 IPv6 字面量：带 `/`、`@`、`#`、`?`、反斜杠、
    空白或方括号外的冒号的一律拒绝。这里校验的是 URL 解析出来的主机，而 IMAP / SMTP 客户端拿到的
    是原字符串——`a@b` 这种写法在两边会被理解成两台不同的机器。

    Args:
        host: 主机名或 `[IPv6]`
        port: 端口
        subject: 出现在报错文案里的主体名
        exclude_fake_ip: Fake-IP 段也拒绝（租户自填的邮件服务器，见 `_FAKE_IP_NETWORKS`）

    Raises:
        UnsafeDownloadUrlError: 主机无法解析或指向非公网地址，或主机名不合法
    """
    host_text = str(host or "").strip()
    if not host_text:
        raise UnsafeDownloadUrlError(f"{subject}缺少主机名")
    # 只收 ASCII 主机名：这里按 httpx 的 IDNA 2008 编码校验，而 `smtplib` / `imaplib` 连接时 Python 按
    # IDNA 2003 编码——`straße.x` 校验的是 `xn--strae-oqa.x`、实际连的是 `strasse.x`，两边可以解析到
    # 完全不同的地址。国际化域名请填它的 `xn--` 形式（连接后另有对端 IP 核对兜底）
    if not host_text.isascii():
        raise UnsafeDownloadUrlError(f"{subject}主机名不合法（国际化域名请填 xn-- 形式）")
    if not _BARE_HOST_PATTERN.fullmatch(host_text):
        raise UnsafeDownloadUrlError(f"{subject}主机名不合法")
    try:
        target = httpx.URL(f"//{host_text}:{int(port)}")
    except (httpx.InvalidURL, ValueError) as e:
        raise UnsafeDownloadUrlError(f"{subject}主机名不合法") from e
    raw_host = target.raw_host
    if not raw_host:
        raise UnsafeDownloadUrlError(f"{subject}缺少主机名")
    # IPv6 字面量在 `raw_host` 里不带方括号，getaddrinfo 正要这个形态
    ips = await _resolve_checked_targets(
        raw_host.decode("ascii"),
        int(port),
        subject,
        exclude_fake_ip=exclude_fake_ip,
    )
    if not ips:
        raise UnresolvableDownloadHostError(f"{subject}主机名解析失败")
    return tuple(str(ip) for ip in ips)


# 裸主机名（可以是国际化域名）或 `[IPv6]`；见 `assert_tenant_host_safe`
_BARE_HOST_PATTERN = re.compile(r"\[[0-9A-Fa-f:.]+\]|[^\s/@#?\\:\[\]]+")


# 本二改分支自己的版本号与署名。
#
# **不要把它写回 `pyproject.toml` 的 `version`**：`get_app_version()` 读的那个值同时用于
# 云端插件市场的兼容判定（`routers/cloud/plugins_market.py`）、更新检查
# （`routers/deployment.py`）与遥测上报（`systems/cloud/collector.py`）。
# 把它降成 0.x 会让所有声明了最低 NA 版本的云端插件被判为不兼容。
# 因此 `pyproject.toml` 的 `version` 是插件兼容版本、继续驱动兼容逻辑，这里只是**面向用户展示**的分支版本。
#
# 升版只改这一处（`tests/test_fork_version_and_registry.py` 钉住它与 CHANGELOG 标题、publish.yml 读取方式一致）：
# `.github/workflows/publish.yml` 从这一行读出版本号作为 GHCR 镜像 tag，并要求触发发布的 git tag 与它一致（标签写法见 publish.yml）。
FORK_VERSION = "0.4.6"
FORK_AUTHOR = "Akiyo_dayo"

# 本分支代码所基于的上游 NekroAgent 发布版，只用于署名「基于 NekroAgent 某版本」。
#
# 它和 `get_app_version()`（`pyproject.toml` 的 `version`，插件兼容版本）不是一回事：
# 插件可见的接口追平上游某版本时，插件兼容版本就可以先提上去，让市场里按那个版本写的插件装得上；
# 但只有上游那个版本的功能都已吸收（或明确不跟）之后，才能说本分支「基于」它。
# 公开版 NOTICE 告诉读者「拿本树与上游这个版本比对就能看出改了哪些文件」，写早了会误导读者。
# 2.3.3 -> 2.4.0：上游 v2.4.0 以内的提交都已移植或已覆盖，所有者 2026-11-03 拍板改口径；
# 按本分支多租户规则改了做法的几处写在 CHANGELOG 与公开版 NOTICE 里。
UPSTREAM_BASE_VERSION = "2.4.0"


def get_fork_version() -> str:
    """本二改分支的展示版本号

    与 `get_app_version()`（插件兼容版本）、`get_upstream_base_version()`（署名用的上游基线）是三个不同的量，别混用。
    """
    return FORK_VERSION


def get_upstream_base_version() -> str:
    """本分支代码所基于的上游 NekroAgent 发布版（署名用）"""
    return UPSTREAM_BASE_VERSION


def get_app_version() -> str:
    """获取插件兼容版本（`pyproject.toml` 的 `version`）

    这是**兼容判定用**的版本：云端插件市场、更新检查、遥测都按它比较，含义是「插件可见接口与上游这个版本一致」。
    面向用户展示分支版本请用 `get_fork_version()`；署名「基于 NekroAgent 某版本」请用 `get_upstream_base_version()`。

    Returns:
        str: 应用版本号
    """
    global _APP_VERSION
    if _APP_VERSION:
        return _APP_VERSION
    pyproject = toml.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    try:
        _APP_VERSION = pyproject["project"]["version"]
    except KeyError:
        _APP_VERSION = "unknown"
    return _APP_VERSION


def compare_semver(a: str, b: str) -> int:
    """比较两个语义化版本号

    按 major.minor.patch 逐段比较，不支持预发布标签。
    格式异常的版本号视为无约束，返回 0。

    Args:
        a: 版本号字符串，如 "2.3.0"
        b: 版本号字符串，如 "2.4.1"

    Returns:
        int: a < b 返回 -1，a == b 返回 0，a > b 返回 1
    """
    def parse(v: str) -> list[int]:
        parts = [p for p in v.strip().split(".") if p]
        result: list[int] = []
        for p in parts[:3]:
            if not p.isdigit():
                raise ValueError(f"非法版本段: {p!r}")
            result.append(int(p))
        return result

    try:
        pa, pb = parse(a), parse(b)
    except (ValueError, AttributeError):
        return 0

    # 补齐长度到 3 段
    while len(pa) < 3:
        pa.append(0)
    while len(pb) < 3:
        pb.append(0)

    for x, y in zip(pa, pb):
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


async def _fetch_following_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: Optional[int] = None,
    check_peer: bool = False,
    pin_addresses: bool = False,
) -> bytes:
    """GET 下载正文，手动跟随跳转

    `pin_addresses=True`（含首跳）：每一跳只解析一次、校验全部结果，请求直接发给校验过的那个地址
    （见 `_pinned_download_request`），不让 httpx 再解析一遍——否则 DNS 重绑之后 NA 会真的连上内网的
    某个端口，哪怕随后的对端核对把它断开，「连上了没有」本身已经能拿来探测内网。

    `url` 必须已经过 `assert_download_url_safe`（`download_file` 在重试逻辑之外校验首跳）。之后每一跳的目标
    都在发请求之前重新校验：跳转目标由对方服务器决定，跟随而不校验等于让首跳的公网地址替内网地址背书。
    跳转响应的正文不读；只有最终那一跳的正文按大小上限边读边计数。上限默认 `MAX_DOWNLOAD_SIZE_BYTES`
    （调用时才读，测试改这个模块常量照样生效）；调用方可以传更小的 `max_bytes`。

    时限：从首跳发出到最终响应头到达，整条链（含每一跳的校验解析）共用 `DOWNLOAD_TIMEOUT_SECONDS`，
    跳转多了不会多拿时间。读正文沿用客户端每次读的超时加大小上限，不另设总时限——有意的取舍，
    不改变大文件慢速下载的既有行为。

    Raises:
        UnsafeDownloadUrlError: 跳转目标被拒、跳转超过 `MAX_DOWNLOAD_REDIRECTS` 次、跳转没有目标地址、
            链接或跳转目标不是合法 URL、正文带压缩编码、正文超过大小上限
        httpx.HTTPStatusError: 最终响应不是 2xx（不跟随的 3xx 也走这里）
        TimeoutError: 整条链在时限内没拿到最终响应头
    """
    limit = MAX_DOWNLOAD_SIZE_BYTES if max_bytes is None else max_bytes
    deadline = asyncio.get_running_loop().time() + DOWNLOAD_TIMEOUT_SECONDS
    current = url
    for redirects_followed in range(MAX_DOWNLOAD_REDIRECTS + 1):
        try:
            async with asyncio.timeout_at(deadline):
                if pin_addresses:
                    request = await _pinned_download_request(client, current)
                else:
                    if redirects_followed:
                        await assert_download_url_safe(current)
                    request = client.build_request("GET", current, headers=_DOWNLOAD_REQUEST_HEADERS)
                response = await client.send(request, stream=True)
        except httpx.InvalidURL as e:
            # httpx 不跟跳转也会按 Location 预先构造下一跳（`response.next_request`，不发出去），`javascript:alert(1)`
            # 这类目标在那里就抛 InvalidURL。确定性失败，不重试。Location 本身解析不了时 httpx 抛的是
            # RemoteProtocolError，和真正的协议错误分不开，照普通失败处理（会重试，同样什么都没发出去）
            raise UnsafeDownloadUrlError("下载链接或跳转目标不是合法的 URL") from e
        try:
            if check_peer:
                # 发请求前那次校验解析一次、httpx 连接时再解析一次，DNS 重绑就发生在两次之间：
                # 以实际连上的地址为准（`check_peer` 由不走代理的调用方打开，见 `fetch_url_bytes`）
                assert_connected_peer_safe(_response_peer_ip(response))
            if response.status_code not in _DOWNLOAD_REDIRECT_STATUS:
                response.raise_for_status()
                if response.headers.get("content-encoding", "").strip().lower() not in ("", "identity"):
                    raise UnsafeDownloadUrlError("下载内容带有压缩编码（请求的是 identity）")
                declared_length = response.headers.get("content-length")
                if declared_length and declared_length.isdigit() and int(declared_length) > limit:
                    raise UnsafeDownloadUrlError(f"下载内容超过大小上限: {declared_length} bytes")
                chunks: list[bytes] = []
                downloaded = 0
                # aiter_raw：不经 httpx 的解压，读进来多少就是多少
                async for chunk in response.aiter_raw():
                    downloaded += len(chunk)
                    if downloaded > limit:
                        raise UnsafeDownloadUrlError(f"下载内容超过大小上限: >{limit} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
            location = response.headers.get("location")
        finally:
            await response.aclose()
        # 文案不带 URL：查询串里可能有签名，这些报错会进日志
        if not location:
            raise UnsafeDownloadUrlError("下载链接返回了跳转，但没有目标地址")
        try:
            current = urljoin(current, location)
        except ValueError as e:
            raise UnsafeDownloadUrlError("下载链接的跳转目标不是合法的 URL") from e
    # 并不是所有调用方都会打印下载异常，这里自己留一条；只记主机名
    logger.warning(f"下载链接跳转超过 {MAX_DOWNLOAD_REDIRECTS} 次，已放弃: {httpx.URL(url).host}")
    raise UnsafeDownloadUrlError(f"下载链接跳转超过 {MAX_DOWNLOAD_REDIRECTS} 次")


async def fetch_url_bytes(url: str, *, max_bytes: Optional[int] = None, timeout: Optional[float] = None) -> bytes:
    """把一个外部链接的正文读进内存（不落盘），与 `download_file` 同一套守卫

    给「拿到字节自己处理」的调用方用（例如表情收集：先算内容哈希再决定存不存）：首跳过
    `assert_download_url_safe`（拒回环 / 私有 / 链路本地等地址，按解析出的 IP 判），跳转逐跳复检，
    只收未压缩正文，边读边按 `max_bytes`（默认 `MAX_DOWNLOAD_SIZE_BYTES`）计数。不重试：调用方多半
    在一次模型调用里，失败直接交回比静默重试三次更好。

    Args:
        url: 外部链接（http / https）
        max_bytes: 正文大小上限；不传用全局上限
        timeout: 客户端每次读的超时（秒）；不传用 `DOWNLOAD_TIMEOUT_SECONDS`

    Raises:
        UnsafeDownloadUrlError: 链接或跳转目标被拒、正文带压缩编码、超过大小上限等
        httpx.HTTPError: 网络错误或最终响应不是 2xx
    """
    client_timeout = DOWNLOAD_TIMEOUT_SECONDS if timeout is None else timeout
    # `trust_env=False`：走环境代理时对端是代理，核对实际对端就没有意义了（与 video.py 同一做法）。
    # 首跳的校验在 `_fetch_following_redirects` 里与连接共用一次解析（`pin_addresses`）
    async with httpx.AsyncClient(timeout=client_timeout, follow_redirects=False, trust_env=False) as client:
        return await _fetch_following_redirects(client, url, max_bytes=max_bytes, check_peer=True, pin_addresses=True)


async def _pinned_download_request(client: httpx.AsyncClient, url: str) -> httpx.Request:
    """为 `url` 造一个直接发往**校验过的地址**的请求

    解析一次、校验全部结果（与 `assert_download_url_safe` 同一套判据），URL 的主机换成第一个地址；
    `Host` 头与 TLS 的 SNI / 证书主机名仍是原主机名（httpcore 的 `sni_hostname` 请求扩展），
    所以对方看到的请求、证书校验都与直接按主机名请求一样。
    部署方开了 `ALLOW_PRIVATE_NETWORK_DOWNLOAD` 时不做解析与换址，照原样请求。

    Raises:
        UnsafeDownloadUrlError: 链接不合法、协议不被允许、主机解析失败或指向非公网地址
    """
    scheme, host, port = _split_outbound_url(url, "下载链接")
    if OsEnv.ALLOW_PRIVATE_NETWORK_DOWNLOAD:
        return client.build_request("GET", url, headers=_DOWNLOAD_REQUEST_HEADERS)
    ips = await _resolve_checked_targets(host, port, "下载链接")
    if not ips:
        raise UnresolvableDownloadHostError("下载链接主机名解析失败")
    target = httpx.URL(url)
    headers = {**_DOWNLOAD_REQUEST_HEADERS, "Host": target.netloc.decode("ascii")}
    extensions = {"sni_hostname": host} if scheme == "https" else {}
    return client.build_request("GET", target.copy_with(host=str(ips[0])), headers=headers, extensions=extensions)


def _response_peer_ip(response: httpx.Response) -> Optional[str]:
    """这一跳实际连上的对端 IP（取不到返回 None，由 `assert_connected_peer_safe` 按拒绝处理）"""
    stream = response.extensions.get("network_stream")
    address = stream.get_extra_info("server_addr") if stream is not None else None
    return str(address[0]) if address else None


def _upload_save_path(file_name: str, from_chat_key: str) -> Path:
    """上传目录里的落盘目标；`file_name` 必须已经过 `safe_upload_file_name`

    再按 resolve 之后的路径确认一次仍在上传目录里：会话目录本身是符号链接之类的情况，
    文件名清洗管不到。
    """
    base = Path(USER_UPLOAD_DIR) / sanitize_chat_key_for_path(from_chat_key) if from_chat_key else Path(USER_UPLOAD_DIR)
    save_path = base / file_name
    if not save_path.resolve().is_relative_to(Path(USER_UPLOAD_DIR).resolve()):
        raise UnsafeUploadPathError("落盘目标不在上传目录里")
    return save_path


async def download_file(
    url: str,
    file_path: str = "",
    file_name: str = "",
    use_suffix: str = "",
    retry_count: int = 3,
    from_chat_key: str = "",
) -> Tuple[str, str]:
    """下载文件

    Args:
        url (str): 下载链接
        file_path (str): 保存路径

    Returns:
        Tuple[str, str]: 文件路径, 文件名
    """

    # 校验失败属于确定性拒绝，重试不会有不同结果，因此放在重试逻辑之外直接抛出。
    # 这里只校验首跳，跳转目标在 _fetch_following_redirects 里逐跳校验（同样抛 UnsafeDownloadUrlError，不重试）
    await assert_download_url_safe(url)

    try:
        # follow_redirects=False：交给 httpx 跟跳转，跳转目标就不会再过上面的校验
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
            content = await _fetch_following_redirects(client, url)
            if not use_suffix:
                mime = magic.from_buffer(content, mime=True)
                use_suffix = f'.{mime.split("/")[1]}' if mime and len(mime.split("/")) > 1 else ""
            if not file_path:
                file_name = safe_upload_file_name(file_name) or f"{hashlib.md5(content).hexdigest()}{use_suffix}"
                save_path = _upload_save_path(file_name, from_chat_key)
                save_path.parent.mkdir(parents=True, exist_ok=True)
                file_path = str(save_path)
            Path(file_path).write_bytes(content)
            Path(file_path).chmod(0o755)
    except (UnsafeDownloadUrlError, UnsafeUploadPathError):
        raise
    except Exception:
        if retry_count > 0:
            return await download_file(
                url,
                file_path,
                file_name,
                use_suffix,
                retry_count=retry_count - 1,
                from_chat_key=from_chat_key,
            )
        raise
    else:
        return file_path, file_name


async def download_file_from_bytes(
    bytes_data: bytes,
    file_path: str = "",
    file_name: str = "",
    use_suffix: str = "",
    from_chat_key: str = "",
) -> Tuple[str, str]:
    """下载文件

    Args:
        url (str): 下载链接
        file_path (str): 保存路径

    Returns:
        Tuple[str, str]: 文件路径, 文件名
    """

    if not file_path:
        file_name = safe_upload_file_name(file_name) or f"{hashlib.md5(bytes_data).hexdigest()}{use_suffix}"
        save_path = _upload_save_path(file_name, from_chat_key)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        file_path = str(save_path)
    Path(file_path).write_bytes(bytes_data)
    Path(file_path).chmod(0o755)
    return file_path, file_name


async def download_file_from_base64(
    base64_str: str,
    file_path: str = "",
    file_name: str = "",
    use_suffix: str = "",
    from_chat_key: str = "",
) -> Tuple[str, str]:
    """下载文件(从base64字符串)

    Args:
        base64_str (str): base64字符串
        file_path (str): 保存路径

    Returns:
        Tuple[str, str]: 文件路径, 文件名
    """
    logger.debug(f"下载文件(从base64字符串): {base64_str[:100]}")
    if base64_str.startswith("data:") and not use_suffix:
        mime_type = mimetypes.guess_type(base64_str)[0] or ""
        use_suffix = f".{mime_type.split('/')[1]}" if mime_type and len(mime_type.split("/")) > 1 else ""
    if base64_str.startswith("data:"):
        base64_str = base64_str.split(",")[1]

    if not file_path:
        file_name = safe_upload_file_name(file_name) or f"{hashlib.md5(base64_str.encode()).hexdigest()}{use_suffix}"
        save_path = _upload_save_path(file_name, from_chat_key)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        file_path = str(save_path)
    Path(file_path).write_bytes(base64.b64decode(base64_str.encode(encoding="utf-8")))
    Path(file_path).chmod(0o755)
    return file_path, file_name


async def copy_to_upload_dir(
    file_path: str,
    file_name: str = "",
    use_suffix: str = "",
    from_chat_key: str = "",
) -> Tuple[str, str]:
    """复制文件到上传目录

    Args:
        file_path (str): 文件路径
        file_name (str): 文件名

    Returns:
        Tuple[str, str]: 文件路径, 文件名
    """
    file_name = safe_upload_file_name(file_name) or f"{hashlib.md5(Path(file_path).read_bytes()).hexdigest()}{use_suffix}"
    save_path = _upload_save_path(file_name, from_chat_key)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    Path(save_path).write_bytes(Path(file_path).read_bytes())
    Path(save_path).chmod(0o755)
    return str(save_path), file_name


def random_chat_check(config: CoreConfig) -> bool:
    """随机聊天检测

    Returns:
        bool: 是否随机聊天
    """

    return random.random() < config.AI_CHAT_RANDOM_REPLY_PROBABILITY


def check_content_trigger(content: str, config: CoreConfig) -> bool:
    """内容触发检测

    Args:
        content (str): 内容

    Returns:
        bool: 是否触发
    """

    for reg_text in config.AI_CHAT_TRIGGER_REGEX:
        reg = re.compile(reg_text)
        if reg.search(content):
            return True
    return False


def check_forbidden_message(content: str, config: CoreConfig) -> bool:
    """忽略消息检测

    Args:
        content (str): 内容

    Returns:
        bool: 是否忽略
    """

    for reg_text in config.AI_CHAT_IGNORE_REGEX:
        reg = re.compile(reg_text)
        _r = reg.search(content)
        if _r:
            logger.info(f'忽略消息: "{content}" - 命中正则: "{reg_text}" 匹配内容: "{_r.group(0)}"')
            return True
    return False


def compress_image(image_path: Path, size_limit_kb: int) -> Path:
    """压缩图片到指定大小以下，仅通过降低分辨率实现

    Args:
        image_path: 原图片路径
        size_limit_kb: 目标大小（KB）

    Returns:
        压缩后的图片路径
    """
    compressed_suffix = "_compressed"
    # 检查是否已经有压缩版本
    compressed_path = image_path.parent / f"{image_path.stem}{compressed_suffix}{image_path.suffix}"
    if compressed_path.exists():
        return compressed_path

    # 打开图片
    img = Image.open(image_path)

    # 确保图片在 RGB 模式
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # 初始缩放比例
    scale = 1.0
    output_path = compressed_path

    while True:
        # 计算新的尺寸
        new_width = int(img.width * scale)
        new_height = int(img.height * scale)
        resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

        # 保存压缩后的图片（使用最高质量）
        resized_img.save(output_path, quality=100)

        # 检查文件大小
        if output_path.stat().st_size <= size_limit_kb * 1024 or scale < 0.1:
            break

        # 降低分辨率继续尝试
        scale *= 0.8

    return output_path


def limited_text_output(text: str, limit: int = 1000, placeholder: str = "...") -> str:
    """限制文本输出

    Args:
        text (str): 文本
        limit (int): 限制长度
    """

    if len(text) <= limit:
        return text
    left_limit = limit // 2 - len(placeholder) // 2
    right_limit = limit - left_limit
    return text[:left_limit] + placeholder + text[-right_limit:]


async def calculate_file_md5(file_path: str, strict: bool = False) -> str:
    """计算文件的 MD5 值或获取标识

    Args:
        file_path (str): 文件路径或 URL

    Returns:
        str: 本地文件返回 MD5 哈希值，URL 返回其链接
    """
    # 对于网络资源，直接返回 URL 作为标识
    if is_url_path(file_path):
        return hashlib.md5(file_path.encode()).hexdigest()

    # 处理本地文件
    try:
        md5_hash = hashlib.md5()
        async with aiofiles.open(file_path, "rb") as f:
            while True:
                chunk = await f.read(4096)
                if not chunk:
                    break
                md5_hash.update(chunk)
        return md5_hash.hexdigest()
    except Exception as e:
        if strict:
            raise
        logger.warning(f"计算文件 MD5 失败: {e}")
        return file_path  # 如果无法计算 MD5，则返回文件路径作为标识


def calculate_text_similarity(text1: str, text2: str, min_length: int = 12) -> float:
    """计算两段文本的相似度

    Args:
        text1 (str): 第一段文本
        text2 (str): 第二段文本

    Returns:
        float: 相似度（0-1）
    """
    if len(text1) < min_length or len(text2) < min_length:
        return 0
    return difflib.SequenceMatcher(None, text1, text2).ratio()
