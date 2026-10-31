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

import pytest

from nekro_agent.core.os_env import OsEnv
from nekro_agent.tools.common_util import (
    DEFAULT_EXTRA_ALLOWED_NETWORKS,
    UnsafeDownloadUrlError,
    assert_download_url_safe,
    assert_tenant_endpoint_url_safe,
    parse_extra_allowed_networks,
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
        """租户接口地址同样走代理，判据必须一致；该路径依然不认 ALLOW_PRIVATE_NETWORK_DOWNLOAD"""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
            await assert_tenant_endpoint_url_safe("https://198.18.4.49/v1")
            with pytest.raises(UnsafeDownloadUrlError):
                await assert_tenant_endpoint_url_safe("http://127.0.0.1:8021/api/config")

    async def test_narrow_internal_allowlist_is_supported(self) -> None:
        """比 ALLOW_PRIVATE_NETWORK_DOWNLOAD 更细的粒度：只放行协议端那一台内网机器"""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
            mp.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", "10.0.0.5/32")
            await assert_download_url_safe("http://10.0.0.5:6099/image.png")
            with pytest.raises(UnsafeDownloadUrlError):
                await assert_download_url_safe("http://10.0.0.6:6099/image.png")


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
