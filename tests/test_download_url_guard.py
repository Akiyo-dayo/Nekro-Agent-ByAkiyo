"""外部资源下载的 SSRF 防护回归测试

保护的契约：下载链接可能完全由消息发送方构造（合并转发内嵌的图片 URL、AI 调用
`download_from_url`、租户自建模型组的 base_url 等），因此必须：

- 只允许 http/https，杜绝 `file://` 之类的本地读取
- 拒绝解析到内网/回环/链路本地地址的目标，防止内网探测与云元数据窃取
- 逃生口仅通过环境变量开启，不暴露到 WebUI（未来的租户管理员不应能关闭它）
- 透明代理的 Fake-IP 段（198.18.0.0/15）默认放行：它不指向部署方网络里的任何主机，
  拦它只会让开了代理的部署收不到图片，拦不到任何真实的内网探测

全部用例使用 IP 字面量，`getaddrinfo` 不会发起 DNS 查询，测试可离线运行。
"""

import socket
from typing import Any, Dict, List

import httpx
import pytest

from nekro_agent.core.os_env import OsEnv
from nekro_agent.tools.common_util import (
    DEFAULT_EXTRA_ALLOWED_NETWORKS,
    UnresolvableDownloadHostError,
    UnsafeDownloadUrlError,
    assert_download_url_safe,
    assert_tenant_endpoint_url_safe,
    parse_extra_allowed_networks,
    tenant_target_unavailable_message,
)


class TestSchemeAllowlist:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://8.8.8.8/x",
            "gopher://8.8.8.8/x",
            "data:text/plain;base64,aGk=",
            "/etc/passwd",
        ],
    )
    async def test_rejects_non_http_schemes(self, url: str) -> None:
        with pytest.raises(UnsafeDownloadUrlError):
            await assert_download_url_safe(url)

    async def test_rejects_missing_host(self) -> None:
        with pytest.raises(UnsafeDownloadUrlError):
            await assert_download_url_safe("http:///path-only")


class TestPrivateNetworkRejection:
    @pytest.mark.parametrize(
        ("url", "why"),
        [
            ("http://127.0.0.1:8021/api/config", "回环：可打到 NA 自身的内部接口"),
            ("http://[::1]:8021/api", "IPv6 回环"),
            ("http://10.0.0.5/admin", "私网 A 段"),
            ("http://172.16.0.5/admin", "私网 B 段"),
            ("http://192.168.1.1/admin", "私网 C 段"),
            ("http://169.254.169.254/latest/meta-data/", "云厂商元数据端点"),
            ("http://0.0.0.0/x", "未指定地址"),
            ("http://100.64.0.1/x", "CGNAT / Tailscale：可能对应部署方网络里的真实主机"),
            ("http://[fd00::1]/x", "IPv6 唯一本地地址"),
            ("http://[fe80::1]/x", "IPv6 链路本地"),
        ],
    )
    async def test_rejects_non_public_addresses(self, url: str, why: str) -> None:
        with pytest.raises(UnsafeDownloadUrlError), pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
            mp.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
            await assert_download_url_safe(url)

    @pytest.mark.parametrize(
        ("url", "why"),
        [
            ("https://8.8.8.8/image.png", "公网"),
            ("http://1.1.1.1:8080/image.png", "公网带端口"),
            ("http://28.0.0.1/x", "28.0.0.0/8 已分配给 DoD，是公网地址，无需特殊处理"),
            ("http://[2001:4860:4860::8888]/x", "IPv6 公网"),
        ],
    )
    async def test_allows_public_addresses(self, url: str, why: str) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
            mp.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
            await assert_download_url_safe(url)


class TestProxyFakeIpNetworks:
    """透明代理 Fake-IP 段的默认放行

    这是生产事故的直接成因：宿主机开了 Shadowrocket 后 `multimedia.nt.qq.com.cn` 被解析到
    198.18.4.49，`is_global` 判其为非公网，含图片的消息整条进不了 Message Collect。
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://198.18.4.49/gchatpic_new/0",  # 事故现场的实际地址
            "http://198.18.0.1/x",  # Clash 默认 Fake-IP 池起始地址
            "http://198.19.255.254/x",  # /15 的另一半，同属 RFC 2544 保留段
        ],
    )
    async def test_fake_ip_allowed_by_default(self, url: str) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
            mp.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
            await assert_download_url_safe(url)

    async def test_fake_ip_can_be_locked_down(self) -> None:
        """在 198.18/15 上确有自建服务的部署可以清空配置恢复严格行为"""
        with pytest.raises(UnsafeDownloadUrlError), pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
            mp.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", "")
            await assert_download_url_safe("http://198.18.4.49/x")

    async def test_extra_networks_do_not_open_real_internal_ranges(self) -> None:
        """放行 Fake-IP 不得顺带放开真实内网 —— 回归防护本体"""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
            mp.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
            for url in ("http://127.0.0.1/x", "http://10.0.0.5/x", "http://169.254.169.254/x"):
                with pytest.raises(UnsafeDownloadUrlError):
                    await assert_download_url_safe(url)

    async def test_fake_ip_also_allowed_for_tenant_endpoints(self) -> None:
        """租户接口地址与下载同一份放行网段：Fake-IP 默认放行（开着透明代理的部署照常能用），
        真实内网照拒；该路径依然不认 ALLOW_PRIVATE_NETWORK_DOWNLOAD"""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)
            await assert_tenant_endpoint_url_safe("https://198.18.4.49/v1")
            with pytest.raises(UnsafeDownloadUrlError):
                await assert_tenant_endpoint_url_safe("http://127.0.0.1:8021/api/config")

    async def test_fake_ip_can_be_locked_down_for_tenant_endpoints_too(self) -> None:
        """把 Fake-IP 段从放行网段里删掉，租户接口地址与下载一起拒"""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", "")
            with pytest.raises(UnsafeDownloadUrlError):
                await assert_tenant_endpoint_url_safe("https://198.18.4.49/v1")

    async def test_narrow_internal_allowlist_is_supported(self) -> None:
        """比 ALLOW_PRIVATE_NETWORK_DOWNLOAD 更细的粒度：只放行协议端那一台内网机器"""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
            mp.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", "10.0.0.5/32")
            await assert_download_url_safe("http://10.0.0.5:6099/image.png")
            with pytest.raises(UnsafeDownloadUrlError):
                await assert_download_url_safe("http://10.0.0.6:6099/image.png")


def _fake_dns(monkeypatch: pytest.MonkeyPatch, zone: Dict[str, str]) -> List[str]:
    """让出站解析按 zone 回答，记下被查的主机名；不在 zone 里的一律解析失败（不发真实 DNS 查询）"""
    looked_up: List[str] = []

    async def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode("ascii") if isinstance(host, bytes) else host
        looked_up.append(name)
        if name not in zone:
            raise socket.gaierror(socket.EAI_NONAME, "not in fake zone")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (zone[name], port))]

    monkeypatch.setattr("nekro_agent.tools.common_util._system_getaddrinfo", fake)
    return looked_up


class TestResolvesTheHostHttpxConnectsTo:
    """校验解析的主机名必须就是 httpx 连接时用的那个

    `urlparse(...).hostname` 是 Unicode，交给 getaddrinfo 时按 Python 自带的 IDNA 2003 编码，httpx 按 IDNA 2008：
    `ß` 一边是 `ss`、一边是 `xn--zca`。攻击者让前者解析到公网、后者解析到内网，就能确定性地绕过校验。
    """

    @pytest.mark.parametrize("label", ["ß", "ς"])
    async def test_the_idna_2008_spelling_is_resolved(self, monkeypatch: pytest.MonkeyPatch, label: str) -> None:
        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
        url = f"http://{label}.attacker.example/latest/meta-data/"
        stdlib_spelling = f"{label.encode('idna').decode('ascii')}.attacker.example"
        httpx_spelling = httpx.URL(url).raw_host.decode("ascii")
        assert stdlib_spelling != httpx_spelling
        looked_up = _fake_dns(monkeypatch, {stdlib_spelling: "8.8.8.8", httpx_spelling: "127.0.0.1"})
        with pytest.raises(UnsafeDownloadUrlError):
            await assert_download_url_safe(url)
        with pytest.raises(UnsafeDownloadUrlError):
            await assert_tenant_endpoint_url_safe(url)
        assert looked_up == [httpx_spelling, httpx_spelling]

    async def test_a_public_idna_2008_spelling_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
        url = "http://ß.attacker.example/x"
        _fake_dns(monkeypatch, {"ss.attacker.example": "127.0.0.1", httpx.URL(url).raw_host.decode("ascii"): "8.8.8.8"})
        await assert_download_url_safe(url)

    @pytest.mark.parametrize("url", ["http://a..b.example/x", f"http://{'a' * 64}.example/x"])
    async def test_a_host_the_resolver_cannot_encode_is_a_guard_error(self, url: str) -> None:
        """httpx 放行、getaddrinfo 编码时抛 UnicodeError（不会发出查询）：要落成守卫的异常，而不是一个调用方没预料到的 UnicodeError"""
        with pytest.raises(UnresolvableDownloadHostError):
            await assert_download_url_safe(url)
        # 租户这一侧不分「解析不到」与「指向不允许的地址」（同一句话，免得成了查 DNS 的工具）
        with pytest.raises(UnsafeDownloadUrlError) as excinfo:
            await assert_tenant_endpoint_url_safe(url)
        assert str(excinfo.value) == tenant_target_unavailable_message("接口地址")

    @pytest.mark.parametrize("url", ["http://[::1", "http://8.8.8.8/\x00", " file:///etc/passwd"])
    async def test_a_url_httpx_cannot_use_is_refused(self, url: str) -> None:
        with pytest.raises(UnsafeDownloadUrlError):
            await assert_download_url_safe(url)
        with pytest.raises(UnsafeDownloadUrlError), pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)
            await assert_download_url_safe(url)


class TestExtraAllowedNetworksParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", ()),
            ("   ", ()),
            ("198.18.0.0/15", ("198.18.0.0/15",)),
            (" 198.18.0.0/15 , 10.0.0.5/32 ", ("198.18.0.0/15", "10.0.0.5/32")),
            ("198.18.0.0/15,,10.0.0.5/32", ("198.18.0.0/15", "10.0.0.5/32")),
            ("2001:db8::/32", ("2001:db8::/32",)),
            ("10.0.0.5", ("10.0.0.5/32",)),  # 裸 IP 视为 /32
            ("10.0.0.5/8", ("10.0.0.0/8",)),  # strict=False：容忍带主机位的写法
        ],
    )
    def test_parses_cidr_list(self, raw: str, expected: tuple) -> None:
        assert tuple(str(n) for n in parse_extra_allowed_networks(raw)) == expected

    def test_invalid_entries_are_skipped_not_fatal(self) -> None:
        """单条写错不该让整个部署起不来，但也不该悄悄吞掉合法条目"""
        parsed = parse_extra_allowed_networks("not-an-ip, 198.18.0.0/15 ,999.0.0.0/8")
        assert tuple(str(n) for n in parsed) == ("198.18.0.0/15",)


class TestEscapeHatch:
    async def test_env_flag_allows_private_addresses(self) -> None:
        """部分部署的协议端确实从内网提供资源，需要显式放行"""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)
            await assert_download_url_safe("http://127.0.0.1:6099/image.png")

    async def test_env_flag_does_not_bypass_scheme_check(self) -> None:
        """逃生口只放宽地址范围，不得放宽协议白名单"""
        with pytest.raises(UnsafeDownloadUrlError), pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)
            await assert_download_url_safe("file:///etc/passwd")
