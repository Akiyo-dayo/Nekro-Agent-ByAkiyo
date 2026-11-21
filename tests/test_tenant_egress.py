"""租户出站的公共模块（`nekro_agent/tools/tenant_egress.py`）

## 这条测试钉的是什么

- 解析一次、全部结果过校验：域名解析到内网一律拒绝，不发起任何连接；透明代理的 Fake-IP 段与下载
  同一份放行网段（默认放行，删掉就拒）；
- 连的就是校验过的那个地址：真正建 TCP 连接时拿到的是 IP，不是主机名（连接库没有机会再解析一遍），
  DNS 第二次返回别的地址也不跟；
- TLS 的 SNI 与 `Host` 头仍是主机名；
- 连上后核对实际对端；
- HTTP(S) 显式代理：`CONNECT <校验过的地址>:<端口>`，SNI 仍是主机名；租户填的代理本身也按租户地址连；
  环境变量代理与 `NO_PROXY`；SOCKS 代理直接拒绝；
- 跟随跳转时每一跳都重新校验。

网络与 DNS 全部是假的：DNS 打桩 `socket.getaddrinfo`，建连接换成一个记录下一切的假后端，不联网。
"""

from __future__ import annotations

import socket
from typing import Any, Dict, List, Optional, Tuple

import httpcore
import httpx
import pytest

from nekro_agent.tools import tenant_egress
from nekro_agent.tools.tenant_egress import TenantNetworkBackend, tenant_http_client

PUBLIC_IP = ".".join(("93", "184", "216", "34"))
OTHER_PUBLIC_IP = ".".join(("93", "184", "216", "35"))
PROXY_IP = ".".join(("93", "184", "216", "40"))
PRIVATE_IP = ".".join(("10", "0", "0", "5"))
LOOPBACK_IP = ".".join(("127", "0", "0", "1"))
FAKE_IP = ".".join(("198", "18", "0", "9"))

OK_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nok"


class FakeDNS:
    """主机名 → 依次给出的答案；问的次数超过答案个数时一直给最后一个。IP 字面量照真的解析"""

    def __init__(self) -> None:
        self.table: Dict[str, List[List[str]]] = {
            "api.example.test": [[PUBLIC_IP]],
            "proxy.example.test": [[PROXY_IP]],
        }
        self.lookups: Dict[str, int] = {}

    def answer(self, name: str, *answers: List[str]) -> None:
        self.table[name] = list(answers)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = socket.getaddrinfo

        def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            name = host.decode("ascii") if isinstance(host, bytes) else str(host)
            try:
                socket.inet_pton(socket.AF_INET6 if ":" in name else socket.AF_INET, name)
            except OSError:
                pass
            else:
                return real(host, port, *args, **kwargs)
            count = self.lookups.get(name, 0)
            self.lookups[name] = count + 1
            answers = self.table.get(name)
            if not answers:
                raise socket.gaierror(socket.EAI_NONAME, "not found")
            current = answers[min(count, len(answers) - 1)]
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, int(port or 0)))
                for address in current
            ]

        monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> FakeDNS:
    fake = FakeDNS()
    fake.install(monkeypatch)
    return fake


@pytest.fixture(autouse=True)
def no_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


class FakeStream(httpcore.AsyncNetworkStream):
    """记下写进来的字节与 `start_tls` 的 SNI，按顺序吐出预设的应答"""

    def __init__(self, backend: "FakeBackend", host: str, port: int, responses: List[bytes]):
        self.backend = backend
        self.host = host
        self.port = port
        self.responses = list(responses)
        self.written = b""
        self.closed = False

    async def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        if not self.responses:
            return b""
        chunk = self.responses.pop(0)
        if len(chunk) > max_bytes:
            self.responses.insert(0, chunk[max_bytes:])
            chunk = chunk[:max_bytes]
        return chunk

    async def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        self.written += buffer
        self.backend.written.append(buffer)

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(self, ssl_context: Any, server_hostname: Optional[str] = None, timeout: Optional[float] = None) -> "FakeStream":
        self.backend.tls_hostnames.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> Any:
        if info == "server_addr":
            return (self.backend.peer_override or self.host, self.port)
        return None


class FakeBackend(httpcore.AsyncNetworkBackend):
    """真正建 TCP 连接的那一层：只记下被要求连到哪里，不联网"""

    def __init__(self, responses: Optional[List[List[bytes]]] = None):
        self.dials: List[Tuple[str, int]] = []
        self.tls_hostnames: List[Optional[str]] = []
        self.written: List[bytes] = []
        self.responses = responses or []
        self.refuse: set = set()
        self.peer_override: Optional[str] = None

    async def connect_tcp(self, host: str, port: int, timeout: Any = None, local_address: Any = None, socket_options: Any = None) -> FakeStream:
        self.dials.append((host, port))
        if host in self.refuse:
            raise httpcore.ConnectError("refused")
        responses = self.responses.pop(0) if self.responses else [OK_RESPONSE]
        return FakeStream(self, host, port, responses)

    async def connect_unix_socket(self, path: str, timeout: Any = None, socket_options: Any = None) -> FakeStream:
        raise AssertionError("不该连本机套接字")

    async def sleep(self, seconds: float) -> None:
        return None

    def request_text(self) -> str:
        return b"".join(self.written).decode("latin-1")


def client_with(backend: FakeBackend, **kwargs: Any) -> httpx.AsyncClient:
    return tenant_http_client(backend=backend, **kwargs)


# ---------------------------------------------------------------------------
# 直连
# ---------------------------------------------------------------------------


class TestDirect:
    async def test_connects_to_the_checked_address_with_hostname_sni_and_host(self, dns: FakeDNS) -> None:
        backend = FakeBackend()
        async with client_with(backend) as client:
            response = await client.get("https://api.example.test/v1/models")

        assert response.status_code == 200
        assert backend.dials == [(PUBLIC_IP, 443)], "建 TCP 连接时拿到的是校验过的 IP，不是主机名"
        assert backend.tls_hostnames == ["api.example.test"], "SNI 与证书主机名仍是主机名"
        assert "Host: api.example.test\r\n" in backend.request_text()
        assert dns.lookups == {"api.example.test": 1}

    async def test_plain_http_keeps_the_host_header(self, dns: FakeDNS) -> None:
        backend = FakeBackend()
        async with client_with(backend) as client:
            await client.get("http://api.example.test:8080/x")

        assert backend.dials == [(PUBLIC_IP, 8080)]
        assert backend.tls_hostnames == []
        assert "Host: api.example.test:8080\r\n" in backend.request_text()

    @pytest.mark.parametrize("address", [PRIVATE_IP, LOOPBACK_IP])
    async def test_private_and_loopback_are_refused_before_any_connection(self, dns: FakeDNS, address: str) -> None:
        dns.answer("api.example.test", [address])
        backend = FakeBackend()
        async with client_with(backend) as client:
            with pytest.raises(httpx.ConnectError) as excinfo:
                await client.get("https://api.example.test/v1/models")

        assert backend.dials == []
        assert address not in str(excinfo.value), "报错不带地址"

    async def test_fake_ip_is_allowed_by_default_and_dialled_as_checked(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        """与下载同一份放行网段：Fake-IP 段默认放行（开着透明代理的部署照常能用），连的仍是校验过的那个地址"""
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.tools.common_util import DEFAULT_EXTRA_ALLOWED_NETWORKS

        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
        dns.answer("api.example.test", [FAKE_IP])
        backend = FakeBackend()
        async with client_with(backend) as client:
            response = await client.get("https://api.example.test/")
        assert response.status_code == 200
        assert backend.dials == [(FAKE_IP, 443)]
        assert backend.tls_hostnames == ["api.example.test"]

    async def test_fake_ip_is_refused_once_removed_from_the_extra_networks(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.os_env import OsEnv

        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", "")
        dns.answer("api.example.test", [FAKE_IP])
        backend = FakeBackend()
        async with client_with(backend) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/")
        assert backend.dials == []

    @pytest.mark.parametrize(
        "address",
        [
            ".".join(("127", "0", "0", "1")),
            ".".join(("10", "1", "2", "3")),
            ".".join(("172", "16", "0", "1")),
            ".".join(("192", "168", "1", "1")),
            ".".join(("169", "254", "169", "254")),
            ".".join(("100", "64", "0", "7")),
        ],
    )
    async def test_real_internal_ranges_stay_refused_with_the_default_extra_networks(
        self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch, address: str
    ) -> None:
        """回环 / RFC1918 / 链路本地（云元数据）/ CGNAT：放行 Fake-IP 不顺带放开它们"""
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.tools.common_util import DEFAULT_EXTRA_ALLOWED_NETWORKS

        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
        dns.answer("api.example.test", [address])
        backend = FakeBackend()
        async with client_with(backend) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/")
        assert backend.dials == []

    async def test_a_mixed_answer_is_refused_as_a_whole(self, dns: FakeDNS) -> None:
        dns.answer("api.example.test", [PUBLIC_IP, PRIVATE_IP])
        backend = FakeBackend()
        async with client_with(backend) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/")
        assert backend.dials == []

    async def test_rebinding_after_the_check_is_not_followed(self, dns: FakeDNS) -> None:
        """同一条连接只解析一次：第二次 DNS 改成内网也不会被用上（连接库拿到的是 IP，不再解析主机名）"""
        dns.answer("api.example.test", [PUBLIC_IP], [PRIVATE_IP])
        backend = FakeBackend(responses=[[OK_RESPONSE, OK_RESPONSE]])
        async with client_with(backend) as client:
            await client.get("https://api.example.test/a")
            await client.get("https://api.example.test/b")

        assert backend.dials == [(PUBLIC_IP, 443)], "第二个请求复用连接，没有再去解析"
        assert dns.lookups == {"api.example.test": 1}

    async def test_a_new_connection_resolves_again_and_refuses_the_rebound_answer(self, dns: FakeDNS) -> None:
        dns.answer("api.example.test", [PUBLIC_IP], [PRIVATE_IP])
        backend = FakeBackend()
        async with client_with(backend) as client:
            await client.get("https://api.example.test/a")
        async with client_with(backend) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/b")

        assert backend.dials == [(PUBLIC_IP, 443)]

    async def test_the_next_checked_address_is_tried_when_one_refuses(self, dns: FakeDNS) -> None:
        dns.answer("api.example.test", [PUBLIC_IP, OTHER_PUBLIC_IP])
        backend = FakeBackend()
        backend.refuse.add(PUBLIC_IP)
        async with client_with(backend) as client:
            await client.get("https://api.example.test/")

        assert backend.dials == [(PUBLIC_IP, 443), (OTHER_PUBLIC_IP, 443)]

    async def test_a_private_peer_after_connecting_is_refused(self, dns: FakeDNS) -> None:
        backend = FakeBackend()
        backend.peer_override = PRIVATE_IP
        async with client_with(backend) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/")

    async def test_a_redirect_into_the_private_network_is_refused(self, dns: FakeDNS) -> None:
        dns.answer("internal.example.test", [PRIVATE_IP])
        redirect = b"HTTP/1.1 302 Found\r\nLocation: http://internal.example.test/admin\r\nContent-Length: 0\r\n\r\n"
        backend = FakeBackend(responses=[[redirect]])
        async with client_with(backend, follow_redirects=True) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/")

        assert backend.dials == [(PUBLIC_IP, 443)], "跳转目标在连接前就被拒"

    async def test_unix_sockets_are_refused(self) -> None:
        with pytest.raises(httpcore.ConnectError):
            await TenantNetworkBackend().connect_unix_socket("/var/run/docker.sock")


# ---------------------------------------------------------------------------
# 代理
# ---------------------------------------------------------------------------


CONNECT_OK = b"HTTP/1.1 200 Connection established\r\n\r\n"


class TestProxy:
    async def test_explicit_proxy_connects_to_the_checked_address(self, dns: FakeDNS) -> None:
        backend = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        async with client_with(backend, proxy="http://user:pw@platform-proxy.example.test:7890") as client:
            response = await client.get("https://api.example.test/v1")

        assert response.status_code == 200
        assert backend.dials == [("platform-proxy.example.test", 7890)], "部署方给的代理照常按主机名连"
        assert backend.written[0].startswith(f"CONNECT {PUBLIC_IP}:443 HTTP/1.1\r\n".encode())
        assert b"Proxy-Authorization: Basic " in backend.written[0]
        assert backend.tls_hostnames == ["api.example.test"], "隧道里的 TLS 仍按主机名"
        assert "Host: api.example.test\r\n" in backend.request_text()

    async def test_target_is_still_checked_when_going_through_a_proxy(self, dns: FakeDNS) -> None:
        dns.answer("api.example.test", [PRIVATE_IP])
        backend = FakeBackend()
        async with client_with(backend, proxy="http://platform-proxy.example.test:7890") as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/")
        assert backend.dials == []

    async def test_a_tenant_proxy_is_itself_resolved_once_and_checked(self, dns: FakeDNS) -> None:
        backend = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        async with client_with(backend, proxy="http://proxy.example.test:3128", proxy_is_tenant=True) as client:
            await client.get("https://api.example.test/")

        assert backend.dials == [(PROXY_IP, 3128)], "租户填的代理也按校验过的地址连"

    async def test_a_tenant_proxy_on_the_private_network_is_refused(self, dns: FakeDNS) -> None:
        dns.answer("proxy.example.test", [PRIVATE_IP])
        backend = FakeBackend()
        async with client_with(backend, proxy="http://proxy.example.test:3128", proxy_is_tenant=True) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/")
        assert backend.dials == []

    async def test_a_failed_connect_is_a_proxy_error(self, dns: FakeDNS) -> None:
        backend = FakeBackend(responses=[[b"HTTP/1.1 403 Forbidden\r\n\r\n"]])
        async with client_with(backend, proxy="http://platform-proxy.example.test:7890") as client:
            with pytest.raises(httpx.ProxyError):
                await client.get("https://api.example.test/")

    async def test_environment_proxy_and_no_proxy(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.test:8888")
        monkeypatch.setenv("NO_PROXY", "skip.example.test")
        dns.answer("skip.example.test", [OTHER_PUBLIC_IP])
        backend = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE], [OK_RESPONSE]])
        async with client_with(backend) as client:
            await client.get("https://api.example.test/")
            await client.get("https://skip.example.test/")

        assert backend.dials == [("env-proxy.example.test", 8888), (OTHER_PUBLIC_IP, 443)]

    async def test_environment_proxy_is_ignored_when_trust_env_is_off(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.test:8888")
        backend = FakeBackend()
        async with client_with(backend, trust_env=False) as client:
            await client.get("https://api.example.test/")
        assert backend.dials == [(PUBLIC_IP, 443)]

    def test_socks_proxies_are_refused(self) -> None:
        with pytest.raises(httpx.ProxyError):
            tenant_http_client(proxy="socks5://proxy.example.test:1080")


# ---------------------------------------------------------------------------
# 其它
# ---------------------------------------------------------------------------


def test_extra_client_transports_are_refused() -> None:
    with pytest.raises(ValueError):
        tenant_http_client(transport=httpx.AsyncHTTPTransport())


def test_httpcore_pool_fields_the_module_relies_on_still_exist() -> None:
    """建连接那一步是换掉 httpx 连接池实现的；httpcore 升级改了内部字段时这里先红"""
    pool = httpx.AsyncHTTPTransport()._pool  # noqa: SLF001
    for name in tenant_egress._POOL_ATTRIBUTES:  # noqa: SLF001
        assert hasattr(pool, name), name


# ---------------------------------------------------------------------------
# 模型组归属：只增不减，判不出来按租户
# ---------------------------------------------------------------------------


class TestGroupOwnership:
    """一轮对话在整个 Agent 循环里拿着同一份组副本；租户中途改了组或删了组，旧副本也不能因此按平台方出站"""

    @pytest.fixture(autouse=True)
    def registry(self) -> Any:
        tenant_egress.reset_model_group_registry()
        yield
        tenant_egress.reset_model_group_registry()

    @staticmethod
    def groups() -> Tuple[Any, Any]:
        from nekro_agent.core.config import ModelConfigGroup

        tenant = ModelConfigGroup(BASE_URL="https://api.example.test/v1/", API_KEY="k-tenant", CHAT_PROXY="")
        platform = ModelConfigGroup(BASE_URL="https://api.example.test/v1", API_KEY="k-platform", CHAT_PROXY="")
        return tenant, platform

    def test_is_recognised_by_name_and_by_connection_parameters(self) -> None:
        from nekro_agent.core.config import ModelConfigGroup

        tenant, platform = self.groups()
        tenant_egress.register_model_groups(tenant=[("tenant-g", tenant)], platform=[("platform-g", platform)])

        assert tenant_egress.is_tenant_owned_group("tenant-g")
        assert not tenant_egress.is_tenant_owned_group("platform-g")
        assert tenant_egress.is_tenant_owned_group(tenant.model_copy())
        assert tenant_egress.is_tenant_owned_group(ModelConfigGroup(**tenant.model_dump())), "拷贝也认得出"
        assert not tenant_egress.is_tenant_owned_group(platform)
        assert not tenant_egress.is_tenant_owned_group(platform.model_copy())

    def test_what_cannot_be_recognised_is_treated_as_tenant(self) -> None:
        from nekro_agent.core.config import ModelConfigGroup

        _tenant, platform = self.groups()
        tenant_egress.register_model_groups(tenant=[], platform=[("platform-g", platform)])

        assert tenant_egress.is_tenant_owned_group(None)
        assert tenant_egress.is_tenant_owned_group("never-registered")
        assert tenant_egress.is_tenant_owned_group(ModelConfigGroup(BASE_URL="https://other.example.test/v1", API_KEY="k"))
        tenant_egress.reset_model_group_registry()
        assert tenant_egress.is_tenant_owned_group(platform), "进程刚起、还没登记过任何组时也按租户"

    def test_a_stale_copy_after_the_tenant_changes_the_key_mid_round_is_still_tenant(self) -> None:
        tenant, platform = self.groups()
        tenant_egress.register_model_groups(tenant=[("tenant-g", tenant)], platform=[("platform-g", platform)])
        stale = tenant.model_copy()  # 这一轮开始时拿到的副本

        rotated = tenant.model_copy(update={"API_KEY": "k-rotated"})
        tenant_egress.register_model_groups(tenant=[("tenant-g", rotated)], platform=[("platform-g", platform)])

        assert tenant_egress.is_tenant_owned_group(stale)
        assert tenant_egress.is_tenant_owned_group(rotated)

    def test_a_stale_copy_after_the_tenant_deletes_the_group_mid_round_is_still_tenant(self) -> None:
        tenant, platform = self.groups()
        tenant_egress.register_model_groups(tenant=[("tenant-g", tenant)], platform=[("platform-g", platform)])
        stale = tenant.model_copy()

        tenant_egress.register_model_groups(tenant=[], platform=[("platform-g", platform)])

        assert tenant_egress.is_tenant_owned_group(stale)
        assert tenant_egress.is_tenant_owned_group("tenant-g")

    def test_a_platform_key_change_keeps_the_platform_group_on_the_platform_path(self) -> None:
        _tenant, platform = self.groups()
        tenant_egress.register_model_groups(tenant=[], platform=[("platform-g", platform)])
        stale = platform.model_copy()
        rotated = platform.model_copy(update={"API_KEY": "k-platform-rotated"})
        tenant_egress.register_model_groups(tenant=[], platform=[("platform-g", rotated)])

        assert not tenant_egress.is_tenant_owned_group(stale)
        assert not tenant_egress.is_tenant_owned_group(rotated)

    def test_overlap_with_a_tenant_group_only_tightens(self) -> None:
        """平台方的组与某个租户组三项完全相同、或同名的租户组出现过：一起按租户（只会更严）"""
        tenant, _platform = self.groups()
        twin = tenant.model_copy()
        tenant_egress.register_model_groups(tenant=[("tenant-g", tenant)], platform=[("platform-twin", twin)])
        assert tenant_egress.is_tenant_owned_group(twin)

        tenant_egress.register_model_groups(tenant=[("shared-name", tenant)], platform=[("shared-name", twin)])
        assert tenant_egress.is_tenant_owned_group("shared-name")


# ---------------------------------------------------------------------------
# 给租户看的报错：两种情况一句话，不带地址，不挂异常链
# ---------------------------------------------------------------------------


class TestTenantFacingErrors:
    async def test_unresolvable_and_private_read_the_same_at_connect_time(self, dns: FakeDNS) -> None:
        dns.answer("internal.example.test", [PRIVATE_IP])
        backend = FakeBackend()
        messages = []
        for host in ("internal.example.test", "missing.example.test"):
            async with client_with(backend) as client:
                with pytest.raises(httpx.ConnectError) as excinfo:
                    await client.get(f"https://{host}/v1")
            messages.append(str(excinfo.value))

        assert messages[0] == messages[1], "解析不到与指向内网说成同一句"
        assert PRIVATE_IP not in messages[0]
        assert backend.dials == []

    async def test_the_connect_time_rejection_carries_no_exception_chain(self, dns: FakeDNS) -> None:
        dns.answer("internal.example.test", [PRIVATE_IP])
        backend = FakeBackend()
        async with client_with(backend) as client:
            with pytest.raises(httpx.ConnectError) as excinfo:
                await client.get("https://internal.example.test/")

        rejected = excinfo.value.__cause__
        assert isinstance(rejected, tenant_egress.TenantEgressRejected)
        assert rejected.__cause__ is None
        assert rejected.__context__ is None

    @pytest.mark.parametrize("host", ["internal.example.test", "missing.example.test"])
    async def test_the_precheck_says_the_same_sentence_without_a_chain(self, dns: FakeDNS, host: str) -> None:
        from nekro_agent.tools.common_util import (
            UnsafeDownloadUrlError,
            assert_tenant_endpoint_url_safe,
            tenant_target_unavailable_message,
        )

        dns.answer("internal.example.test", [PRIVATE_IP])
        with pytest.raises(UnsafeDownloadUrlError) as excinfo:
            await assert_tenant_endpoint_url_safe(f"https://{host}/v1")

        assert str(excinfo.value) == tenant_target_unavailable_message("接口地址")
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__context__ is None

    @pytest.mark.parametrize("host", ["internal.example.test", "missing.example.test"])
    async def test_the_mail_server_check_says_the_same_sentence(self, dns: FakeDNS, host: str) -> None:
        from nekro_agent.tools.common_util import (
            UnsafeDownloadUrlError,
            resolve_tenant_host,
            tenant_target_unavailable_message,
        )

        dns.answer("internal.example.test", [PRIVATE_IP])
        with pytest.raises(UnsafeDownloadUrlError) as excinfo:
            await resolve_tenant_host(host, 993, subject="邮箱服务器")

        assert str(excinfo.value) == tenant_target_unavailable_message("邮箱服务器")
        assert excinfo.value.__context__ is None

    async def test_the_download_guard_no_longer_puts_the_resolved_address_in_the_error(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.tools.common_util import UnsafeDownloadUrlError, assert_download_url_safe

        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
        dns.answer("internal.example.test", [PRIVATE_IP])
        with pytest.raises(UnsafeDownloadUrlError) as excinfo:
            await assert_download_url_safe("https://internal.example.test/a.png")
        assert PRIVATE_IP not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 下载素材（`download_file`）借用同一套建连接办法
# ---------------------------------------------------------------------------


@pytest.fixture
def strict_download(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.core.os_env import OsEnv
    from nekro_agent.tools.common_util import DEFAULT_EXTRA_ALLOWED_NETWORKS

    monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
    monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)


def download_client(backend: FakeBackend) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=tenant_egress.download_http_transport(backend=backend), trust_env=False)


@pytest.mark.usefixtures("strict_download")
class TestDownloadTransport:
    async def test_connects_to_the_checked_address_with_hostname_sni_and_host(self, dns: FakeDNS) -> None:
        dns.answer("img.example.test", [PUBLIC_IP])
        backend = FakeBackend()
        async with download_client(backend) as client:
            response = await client.get("https://img.example.test/a.png")

        assert response.status_code == 200
        assert backend.dials == [(PUBLIC_IP, 443)]
        assert backend.tls_hostnames == ["img.example.test"]
        assert "Host: img.example.test\r\n" in backend.request_text()

    async def test_rebinding_between_the_check_and_the_connection_is_refused(self, dns: FakeDNS) -> None:
        from nekro_agent.tools.common_util import UnsafeDownloadUrlError, assert_download_url_safe

        dns.answer("img.example.test", [PUBLIC_IP], [PRIVATE_IP])
        await assert_download_url_safe("https://img.example.test/a.png")  # 校验时是公网
        backend = FakeBackend()
        async with download_client(backend) as client:
            with pytest.raises(UnsafeDownloadUrlError):  # 原样交回，不是「连不上」：download_file 照旧不重试
                await client.get("https://img.example.test/a.png")
        assert backend.dials == []

    async def test_a_private_peer_is_refused_and_the_connection_closed(self, dns: FakeDNS) -> None:
        from nekro_agent.tools.common_util import UnsafeDownloadUrlError

        dns.answer("img.example.test", [PUBLIC_IP])
        backend = FakeBackend()
        backend.peer_override = PRIVATE_IP
        async with download_client(backend) as client:
            with pytest.raises(UnsafeDownloadUrlError):
                await client.get("https://img.example.test/a.png")
        assert backend.dials == [(PUBLIC_IP, 443)]

    async def test_the_fake_ip_default_for_downloads_is_unchanged(self, dns: FakeDNS) -> None:
        """下载默认放行 Fake-IP 段（所有者定：保持现状）：这里钉住它没被改掉，而且连的是校验过的那个地址"""
        dns.answer("img.example.test", [FAKE_IP])
        backend = FakeBackend()
        async with download_client(backend) as client:
            await client.get("https://img.example.test/a.png")
        assert backend.dials == [(FAKE_IP, 443)]

    async def test_the_escape_hatch_dials_the_hostname_without_pinning(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.os_env import OsEnv

        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)
        dns.answer("img.example.test", [PRIVATE_IP])
        backend = FakeBackend()
        backend.peer_override = PRIVATE_IP
        async with download_client(backend) as client:
            response = await client.get("https://img.example.test/a.png")
        assert response.status_code == 200
        assert backend.dials == [("img.example.test", 443)], "部署方放开内网下载时照原样按主机名连"

    async def test_the_environment_proxy_is_kept_and_asked_to_connect_to_the_checked_address(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "env-proxy.example.test:8888")  # 没写协议：按 http:// 补
        dns.answer("img.example.test", [PUBLIC_IP])
        backend = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        async with download_client(backend) as client:
            await client.get("https://img.example.test/a.png")

        assert backend.dials == [("env-proxy.example.test", 8888)]
        assert backend.written[0].startswith(f"CONNECT {PUBLIC_IP}:443 HTTP/1.1\r\n".encode())
        assert backend.tls_hostnames == ["img.example.test"]

    async def test_an_explicit_deployment_proxy_replaces_the_environment_one(
        self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """部署方配的代理（系统默认代理，下载远程世界书时可选）：只走它、不读环境变量，同样 CONNECT 校验过的地址"""
        monkeypatch.setenv("HTTPS_PROXY", "env-proxy.example.test:8888")
        dns.answer("img.example.test", [PUBLIC_IP])
        backend = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        transport = tenant_egress.download_http_transport(
            backend=backend, proxy="http://u:p@sys-proxy.example.test:7890"
        )
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            await client.get("https://img.example.test/a.png")

        assert backend.dials == [("sys-proxy.example.test", 7890)]
        assert backend.written[0].startswith(f"CONNECT {PUBLIC_IP}:443 HTTP/1.1\r\n".encode())
        assert b"Proxy-Authorization: Basic " in backend.written[0]
        assert backend.tls_hostnames == ["img.example.test"]

    async def test_tenant_downloads_ignore_the_private_network_switch(
        self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """租户触发的下载（远程世界书）按租户的判据：部署方开了 ALLOW_PRIVATE_NETWORK_DOWNLOAD 也照样拒内网"""
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.tools.common_util import UnsafeDownloadUrlError, fetch_url_bytes

        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)
        dns.answer("img.example.test", [PRIVATE_IP])
        backend = FakeBackend()
        transport = tenant_egress.download_http_transport(backend=backend, tenant=True)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://img.example.test/a.png")
        assert backend.dials == []
        # 首跳预检同样按租户的判据，「解析不到」与「指向内网」说同一句话
        with pytest.raises(UnsafeDownloadUrlError, match="不可用"):
            await fetch_url_bytes("https://img.example.test/a.png", tenant=True)

    def test_tenant_downloads_refuse_a_socks_environment_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SOCKS 代理没有「连校验过的地址」的实现，不固定地址就能经代理被 DNS 重绑：租户触发的下载直接拒"""
        monkeypatch.setenv("ALL_PROXY", "socks5://env-proxy.example.test:1080")
        url = httpx.URL("https://img.example.test/a.png")
        download = tenant_egress.download_http_transport(tenant=True)
        with pytest.raises(httpx.ProxyError):
            download._transport(download.proxy_for(url))  # noqa: SLF001

    def test_an_explicit_socks_proxy_is_refused_up_front(self) -> None:
        """显式给的代理只认 HTTP(S)：SOCKS 在建传输层时就报，不悄悄改成直连"""
        with pytest.raises(httpx.ProxyError):
            tenant_egress.download_http_transport(proxy="socks5://sys-proxy.example.test:1080")

    def test_a_socks_environment_proxy_keeps_working_for_downloads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """部署方用 SOCKS 代理时下载照原来的做法（`httpx[socks]`，代理按主机名连）；租户出站仍然拒绝 SOCKS"""
        monkeypatch.setenv("ALL_PROXY", "socks5://env-proxy.example.test:1080")
        url = httpx.URL("https://img.example.test/a.png")

        download = tenant_egress.download_http_transport()
        proxy = download.proxy_for(url)
        assert proxy == "socks5://env-proxy.example.test:1080"
        assert type(download._transport(proxy)._pool).__name__ == "AsyncSOCKSProxy"  # noqa: SLF001

        tenant = tenant_egress.TenantHTTPTransport(trust_env=True)
        with pytest.raises(httpx.ProxyError):
            tenant._transport(tenant.proxy_for(url))  # noqa: SLF001

    async def test_download_file_refuses_a_rebound_answer_without_connecting_or_retrying(
        self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from nekro_agent.tools.common_util import UnsafeDownloadUrlError, download_file

        backend = FakeBackend()
        monkeypatch.setattr(httpcore, "AnyIOBackend", lambda: backend)
        dns.answer("img.example.test", [PUBLIC_IP], [PRIVATE_IP])
        with pytest.raises(UnsafeDownloadUrlError):
            await download_file("https://img.example.test/a.png", file_path=str(tmp_path / "a.png"), use_suffix=".png")

        assert backend.dials == []
        assert dns.lookups == {"img.example.test": 2}, "首跳校验一次、建连接一次；被拒不重试"
        assert not (tmp_path / "a.png").exists()

    async def test_download_file_connects_to_the_checked_address(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
        from nekro_agent.tools.common_util import download_file

        backend = FakeBackend(responses=[[b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nGIF"]])
        monkeypatch.setattr(httpcore, "AnyIOBackend", lambda: backend)
        dns.answer("img.example.test", [PUBLIC_IP])
        path, _name = await download_file("https://img.example.test/a.gif", file_path=str(tmp_path / "a.gif"), use_suffix=".gif")

        assert backend.dials == [(PUBLIC_IP, 443)]
        assert backend.tls_hostnames == ["img.example.test"]
        assert (tmp_path / "a.gif").read_bytes() == b"GIF"
        assert path == str(tmp_path / "a.gif")


# ---------------------------------------------------------------------------
# 零碎：代理协议、组播 / 未指定地址、解析并发上限
# ---------------------------------------------------------------------------


class TestProxyScheme:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("env-proxy.example.test:7890", "http://env-proxy.example.test:7890"),
            ("localhost:7890", "http://localhost:7890"),
            ("https://env-proxy.example.test:7890", "https://env-proxy.example.test:7890"),
        ],
    )
    def test_an_environment_proxy_without_a_scheme_is_read_as_http(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
        monkeypatch.setenv("HTTPS_PROXY", raw)
        transport = tenant_egress.TenantHTTPTransport(trust_env=True)
        assert transport.proxy_for(httpx.URL("https://api.example.test/")) == expected

    async def test_the_environment_proxy_without_a_scheme_is_used(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "env-proxy.example.test:8888")
        backend = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        async with client_with(backend) as client:
            await client.get("https://api.example.test/")
        assert backend.dials == [("env-proxy.example.test", 8888)]
        assert backend.written[0].startswith(f"CONNECT {PUBLIC_IP}:443 HTTP/1.1\r\n".encode())


class TestEnvProxyResolvesHostname:
    """`EGRESS_ENV_PROXY_RESOLVES_HOSTNAME`：只管部署方环境代理这一路；默认关（CONNECT 校验过的地址），
    开了改发 CONNECT 主机名、由代理解析（本机 DNS 被污染 / 分区解析、靠环境代理出网的部署）"""

    @pytest.fixture
    def switch(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        from nekro_agent.core.os_env import OsEnv

        def set_switch(on: bool) -> None:
            monkeypatch.setattr(OsEnv, "EGRESS_ENV_PROXY_RESOLVES_HOSTNAME", on)

        set_switch(False)
        return set_switch

    def test_it_is_off_unless_the_deployment_sets_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.core_utils import OsEnvTypes

        monkeypatch.delenv("NEKRO_EGRESS_ENV_PROXY_RESOLVES_HOSTNAME", raising=False)
        assert OsEnvTypes.Bool("EGRESS_ENV_PROXY_RESOLVES_HOSTNAME") is False

    async def test_off_the_env_proxy_is_asked_for_the_checked_address(self, dns: FakeDNS, switch: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.test:8888")
        backend = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        async with client_with(backend) as client:
            await client.get("https://api.example.test/v1")

        assert backend.dials == [("env-proxy.example.test", 8888)]
        assert backend.written[0].startswith(f"CONNECT {PUBLIC_IP}:443 HTTP/1.1\r\n".encode())

    async def test_on_the_env_proxy_is_asked_for_the_host_name(self, dns: FakeDNS, switch: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        switch(True)
        monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.test:8888")
        backend = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        async with client_with(backend) as client:
            response = await client.get("https://api.example.test/v1")

        assert response.status_code == 200
        assert backend.dials == [("env-proxy.example.test", 8888)]
        assert backend.written[0].startswith(b"CONNECT api.example.test:443 HTTP/1.1\r\n")
        assert backend.tls_hostnames == ["api.example.test"]
        assert dns.lookups == {"api.example.test": 1}, "NA 照样按自己的解析校验一次"

    async def test_on_a_name_that_resolves_inside_is_still_refused(self, dns: FakeDNS, switch: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        switch(True)
        monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.test:8888")
        dns.answer("api.example.test", [PRIVATE_IP])
        backend = FakeBackend()
        async with client_with(backend) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/v1")
        assert backend.dials == []

    async def test_on_proxies_that_are_not_from_the_environment_still_get_the_checked_address(
        self, dns: FakeDNS, switch: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """租户填的代理（模型组的 `CHAT_PROXY`）与调用方显式给的代理不受开关影响"""
        switch(True)
        monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.test:8888")
        tenant = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        async with client_with(tenant, proxy="http://proxy.example.test:3128", proxy_is_tenant=True) as client:
            await client.get("https://api.example.test/v1")
        assert tenant.dials == [(PROXY_IP, 3128)]
        assert tenant.written[0].startswith(f"CONNECT {PUBLIC_IP}:443 HTTP/1.1\r\n".encode())

        explicit = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        async with client_with(explicit, proxy="http://platform-proxy.example.test:7890") as client:
            await client.get("https://api.example.test/v1")
        assert explicit.written[0].startswith(f"CONNECT {PUBLIC_IP}:443 HTTP/1.1\r\n".encode())

    async def test_on_direct_connections_still_dial_the_checked_address(self, dns: FakeDNS, switch: Any) -> None:
        switch(True)
        backend = FakeBackend()
        async with client_with(backend) as client:
            await client.get("https://api.example.test/v1")
        assert backend.dials == [(PUBLIC_IP, 443)]

    @pytest.mark.usefixtures("strict_download")
    async def test_on_downloads_through_the_env_proxy_send_the_host_name(self, dns: FakeDNS, switch: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        switch(True)
        monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.test:8888")
        dns.answer("img.example.test", [PUBLIC_IP])
        backend = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        async with download_client(backend) as client:
            await client.get("https://img.example.test/a.png")
        assert backend.written[0].startswith(b"CONNECT img.example.test:443 HTTP/1.1\r\n")

    @pytest.mark.usefixtures("strict_download")
    async def test_on_fetch_url_bytes_through_the_env_proxy_sends_the_host_name(
        self, dns: FakeDNS, switch: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """表情收集等用的 `fetch_url_bytes` 也走同一个传输层：环境代理照用，开关对它同样生效"""
        from nekro_agent.tools.common_util import fetch_url_bytes

        switch(True)
        monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.test:8888")
        dns.answer("img.example.test", [PUBLIC_IP])
        backend = FakeBackend(responses=[[CONNECT_OK, b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nPNG"]])
        monkeypatch.setattr(httpcore, "AnyIOBackend", lambda: backend)
        assert await fetch_url_bytes("https://img.example.test/a.png") == b"PNG"
        assert backend.dials == [("env-proxy.example.test", 8888)]
        assert backend.written[0].startswith(b"CONNECT img.example.test:443 HTTP/1.1\r\n")

    @pytest.mark.usefixtures("strict_download")
    async def test_off_downloads_through_the_env_proxy_send_the_checked_address(self, dns: FakeDNS, switch: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy.example.test:8888")
        dns.answer("img.example.test", [PUBLIC_IP])
        backend = FakeBackend(responses=[[CONNECT_OK, OK_RESPONSE]])
        async with download_client(backend) as client:
            await client.get("https://img.example.test/a.png")
        assert backend.written[0].startswith(f"CONNECT {PUBLIC_IP}:443 HTTP/1.1\r\n".encode())


class TestMulticastAndUnspecified:
    MULTICAST_V4 = ".".join(("224", "0", "0", "1"))
    GLOBAL_MULTICAST_V4 = ".".join(("233", "252", "0", "1"))
    UNSPECIFIED_V4 = ".".join(("0", "0", "0", "0"))
    MULTICAST_V6 = ":".join(("ff0e", "", "1"))
    UNSPECIFIED_V6 = "::"

    @pytest.mark.parametrize("raw", [MULTICAST_V4, GLOBAL_MULTICAST_V4, UNSPECIFIED_V4, MULTICAST_V6, UNSPECIFIED_V6])
    def test_are_refused_for_every_policy(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        import ipaddress

        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.tools import common_util

        # 写进放行网段也不行
        extra = (".".join(("224", "0", "0", "0")) + "/3", ":".join(("ff00", "", "")) + "/8", ".".join(("0", "0", "0", "0")) + "/8")
        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", ",".join(extra))
        ip = ipaddress.ip_address(raw)
        assert not common_util._is_allowed_target_ip(ip)  # noqa: SLF001

    async def test_a_name_resolving_to_multicast_is_refused_before_connecting(self, dns: FakeDNS) -> None:
        dns.answer("api.example.test", [self.GLOBAL_MULTICAST_V4])
        backend = FakeBackend()
        async with client_with(backend) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/")
        assert backend.dials == []


class TestResolverPool:
    """出站解析在自己的线程池里跑：卡住的解析不拖住默认线程池；同一主机同时只查一次；没人等的查询撤掉"""

    @pytest.fixture
    def pool(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        import weakref

        from nekro_agent.tools import common_util

        monkeypatch.setattr(common_util, "_RESOLVER_WORKERS", 2)
        monkeypatch.setattr(common_util, "_resolver_pool", None)
        monkeypatch.setattr(common_util, "_inflight_lookups", weakref.WeakKeyDictionary())
        yield common_util
        created = common_util._resolver_pool  # noqa: SLF001
        if created is not None:
            created.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def blocking_dns(monkeypatch: pytest.MonkeyPatch, release: Any, calls: List[str], delay: float = 0.0) -> None:
        """`slow*` 的名字卡到 release 为止（模拟被黑洞的域名），其余照常快速回答"""
        import time

        def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            name = host.decode("ascii") if isinstance(host, bytes) else str(host)
            calls.append(name)
            if name.startswith("slow"):
                release.wait(10)
            elif delay:
                time.sleep(delay)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, int(port or 0)))]

        monkeypatch.setattr(socket, "getaddrinfo", fake)

    async def test_stuck_lookups_do_not_hold_up_the_default_executor(self, pool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio
        import threading
        import time

        release = threading.Event()
        calls: List[str] = []
        self.blocking_dns(monkeypatch, release, calls)
        try:
            for i in range(4):  # 线程池只有 2 个线程：两个卡在线程里，两个在排队
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(pool._getaddrinfo_shared(f"slow{i}.attacker.test", 443), 0.05)  # noqa: SLF001

            started = time.monotonic()
            assert await asyncio.wait_for(asyncio.to_thread(lambda: 1), 1) == 1
            await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo("platform-llm.example.test", 443), 1)
            assert time.monotonic() - started < 0.5, "默认线程池（to_thread、平台方 httpx 的解析）不受影响"

            await asyncio.sleep(0.1)
            assert sorted(name for name in calls if name.startswith("slow")) == ["slow0.attacker.test", "slow1.attacker.test"], (
                "没人等的查询从队列里撤掉了，只有已经排上线程的两次真的跑了"
            )
        finally:
            release.set()

    async def test_the_same_host_is_looked_up_once_at_a_time(self, pool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio
        import threading

        calls: List[str] = []
        self.blocking_dns(monkeypatch, threading.Event(), calls, delay=0.05)
        results = await asyncio.gather(*(pool._getaddrinfo_shared("same.example.test", 443) for _ in range(5)))  # noqa: SLF001

        assert calls == ["same.example.test"]
        assert all(result == results[0] for result in results)

    async def test_one_waiter_giving_up_does_not_cancel_the_lookup_for_the_others(self, pool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio
        import threading

        release = threading.Event()
        calls: List[str] = []
        self.blocking_dns(monkeypatch, release, calls)
        try:
            impatient = asyncio.ensure_future(pool._getaddrinfo_shared("slow-shared.example.test", 443))  # noqa: SLF001
            patient = asyncio.ensure_future(pool._getaddrinfo_shared("slow-shared.example.test", 443))  # noqa: SLF001
            await asyncio.sleep(0.05)
            impatient.cancel()
            await asyncio.sleep(0.01)
            release.set()
            result = await asyncio.wait_for(patient, 2)
        finally:
            release.set()

        assert result[0][4][0] == PUBLIC_IP
        assert calls == ["slow-shared.example.test"]

    async def test_a_lookup_being_cancelled_is_not_handed_to_a_new_caller(self, pool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """最后一个等待者放弃后，那次查询要过一两轮事件循环才真正结束；这期间新来的调用方不能复用它，
        否则会被转交一个凭空的 CancelledError"""
        import asyncio
        import threading

        release = threading.Event()
        calls: List[str] = []
        self.blocking_dns(monkeypatch, release, calls)
        key = ("slow-race.example.test", 443)
        try:
            first = asyncio.ensure_future(pool._getaddrinfo_shared(*key))  # noqa: SLF001
            await asyncio.sleep(0.05)
            first.cancel()
            await asyncio.sleep(0)  # 轮到 first 处理取消、跑完 finally；那次查询本身此时还没结束
            table = pool._inflight_lookups.get(asyncio.get_running_loop())  # noqa: SLF001
            assert table is not None and key not in table, "没人等了的查询要立刻从表里摘掉"

            second = asyncio.ensure_future(pool._getaddrinfo_shared(*key))  # noqa: SLF001
            release.set()
            result = await asyncio.wait_for(second, 2)
        finally:
            release.set()

        assert first.cancelled()
        assert result[0][4][0] == PUBLIC_IP, "新来的调用方另起一次查询，拿到正常结果，而不是 CancelledError"
        assert calls == [key[0], key[0]]
