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
from urllib.parse import urlparse

import aiofiles
import httpx
import magic
import toml
from PIL import Image

from nekro_agent.core import logger
from nekro_agent.core.config import CoreConfig
from nekro_agent.core.os_env import USER_UPLOAD_DIR, OsEnv
from nekro_agent.tools.path_convertor import is_url_path, sanitize_chat_key_for_path

_APP_VERSION: str = ""

# 外部资源下载约束：下载链接可能完全由消息发送方构造（如合并转发内嵌的图片 URL），
# 缺少这些限制时可被用于内网探测、云元数据窃取或磁盘打爆。
ALLOWED_DOWNLOAD_SCHEMES = frozenset({"http", "https"})
DOWNLOAD_TIMEOUT_SECONDS = 20.0
MAX_DOWNLOAD_SIZE_BYTES = 64 * 1024 * 1024
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
# 就是 127.0.0.1，`::127.0.0.1` 是兼容格式，`2002::/16` 是 6to4），`fec0::/10` 是废弃的站点本地段。
# 都不是正常公网服务会用的地址，整段拒绝，要用的部署写进 DOWNLOAD_EXTRA_ALLOWED_NETWORKS。
_DENIED_IPV6_NETWORKS: Tuple[ipaddress.IPv6Network, ...] = tuple(
    ipaddress.IPv6Network(entry) for entry in ("::/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "fec0::/10")
)


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


def _is_allowed_target_ip(ip: _IPAddress) -> bool:
    """判断出站目标 IP 是否允许访问"""
    if ip.is_global and not _is_denied_ipv6_form(ip):
        return True
    return any(ip.version == network.version and ip in network for network in parse_extra_allowed_networks(OsEnv.DOWNLOAD_EXTRA_ALLOWED_NETWORKS))


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
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_DOWNLOAD_SCHEMES:
        raise UnsafeDownloadUrlError(f"不支持的{subject}协议: {scheme or '(空)'}")

    host = parsed.hostname
    if not host:
        raise UnsafeDownloadUrlError(f"{subject}缺少主机名")

    port = parsed.port or _DEFAULT_SCHEME_PORTS[scheme]
    try:
        addr_infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnresolvableDownloadHostError(f"{subject}主机名解析失败: {host}") from e

    for addr_info in addr_infos:
        ip = _normalize_ip(addr_info[4][0])
        if not _is_allowed_target_ip(ip):
            # 调用方普遍以 debug 级别吞掉下载异常，这里主动升级日志级别，
            # 既保证安全事件可见，也避免正常部署被拦截时无从排查
            logger.warning(
                f"拒绝指向非公网地址的{subject}: {host} -> {ip}"
                f"（若这是透明代理的 Fake-IP，请把对应网段写入 NEKRO_DOWNLOAD_EXTRA_ALLOWED_NETWORKS）",
            )
            raise UnsafeDownloadUrlError(f"{subject}指向非公网地址: {host} -> {ip}")


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
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ALLOWED_DOWNLOAD_SCHEMES:
            raise UnsafeDownloadUrlError(f"不支持的下载链接协议: {parsed.scheme.lower() or '(空)'}")
        if not parsed.hostname:
            raise UnsafeDownloadUrlError("下载链接缺少主机名")
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


# 本二改分支自己的版本号与署名。
#
# **不要把它写回 `pyproject.toml` 的 `version`**：`get_app_version()` 读的那个值同时用于
# 云端插件市场的兼容判定（`routers/cloud/plugins_market.py`）、更新检查
# （`routers/deployment.py`）与遥测上报（`systems/cloud/collector.py`）。
# 把它降成 0.x 会让所有声明了最低 NA 版本的云端插件被判为不兼容。
# 因此上游版本继续驱动兼容逻辑，这里只是**面向用户展示**的分支版本。
#
# 升版只改这一处（`tests/test_fork_version_and_registry.py` 钉住它与 CHANGELOG 标题、publish.yml 读取方式一致）：
# `.github/workflows/publish.yml` 从这一行读出版本号作为 GHCR 镜像 tag，并要求 git tag 恰为 `v<版本号>`。
FORK_VERSION = "0.3.0"
FORK_AUTHOR = "Akiyo_dayo"


def get_fork_version() -> str:
    """本二改分支的展示版本号

    与 `get_app_version()`（上游版本，驱动兼容判定）是两个不同的量，别混用。
    """
    return FORK_VERSION


def get_app_version() -> str:
    """获取上游 NekroAgent 的版本号

    这是**兼容判定用**的版本：云端插件市场、更新检查、遥测都按它比较。
    面向用户展示分支版本请用 `get_fork_version()`。

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

    # 校验失败属于确定性拒绝，重试不会有不同结果，因此放在重试逻辑之外直接抛出
    await assert_download_url_safe(url)

    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                declared_length = response.headers.get("content-length")
                if declared_length and declared_length.isdigit() and int(declared_length) > MAX_DOWNLOAD_SIZE_BYTES:
                    raise UnsafeDownloadUrlError(f"下载内容超过大小上限: {declared_length} bytes")
                chunks: list[bytes] = []
                downloaded = 0
                async for chunk in response.aiter_bytes():
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_SIZE_BYTES:
                        raise UnsafeDownloadUrlError(f"下载内容超过大小上限: >{MAX_DOWNLOAD_SIZE_BYTES} bytes")
                    chunks.append(chunk)
            content = b"".join(chunks)
            if not use_suffix:
                mime = magic.from_buffer(content, mime=True)
                use_suffix = f'.{mime.split("/")[1]}' if mime and len(mime.split("/")) > 1 else ""
            if not file_path:
                file_name = file_name or f"{hashlib.md5(content).hexdigest()}{use_suffix}"
                if from_chat_key:
                    save_path = Path(USER_UPLOAD_DIR) / sanitize_chat_key_for_path(from_chat_key) / Path(file_name)
                else:
                    save_path = Path(USER_UPLOAD_DIR) / Path(file_name)
                save_path.parent.mkdir(parents=True, exist_ok=True)
                file_path = str(save_path)
            Path(file_path).write_bytes(content)
            Path(file_path).chmod(0o755)
    except UnsafeDownloadUrlError:
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
        file_name = file_name or f"{hashlib.md5(bytes_data).hexdigest()}{use_suffix}"
        if from_chat_key:
            save_path = Path(USER_UPLOAD_DIR) / sanitize_chat_key_for_path(from_chat_key) / Path(file_name)
        else:
            save_path = Path(USER_UPLOAD_DIR) / Path(file_name)
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
        file_name = file_name or f"{hashlib.md5(base64_str.encode()).hexdigest()}{use_suffix}"
        if from_chat_key:
            save_path = Path(USER_UPLOAD_DIR) / sanitize_chat_key_for_path(from_chat_key) / Path(file_name)
        else:
            save_path = Path(USER_UPLOAD_DIR) / Path(file_name)
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
    if not file_name:
        file_name = f"{hashlib.md5(Path(file_path).read_bytes()).hexdigest()}{use_suffix}"
    if from_chat_key:
        save_path = Path(USER_UPLOAD_DIR) / sanitize_chat_key_for_path(from_chat_key) / Path(file_name)
    else:
        save_path = Path(USER_UPLOAD_DIR) / Path(file_name)
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
