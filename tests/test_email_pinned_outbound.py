"""邮箱出站的第二轮加固：只解析一次、直连校验过的地址；报错不带地址；引用密钥不轮换；转回平台方不丢核对

## 只解析一次、直连校验过的地址

上一轮的做法是「先校验、再交给 `imaplib` / `smtplib` 去连、连上后核对对端」。核对挡住了授权码，但 DNS 重绑
之后 NA **已经连上**了内网的那个端口，而「连上没连上」本身就能从报错里看出来——测试连接就成了内网端口扫描器。
现在租户填的服务器只解析一次、全部结果都校验过，直接连校验过的地址（TLS 的 SNI 与证书主机名仍是主机名）；
表情的远程图片同理（请求发往校验过的地址，`Host` 头与 SNI 仍是原主机名）。

## Fake-IP 段与 IPv4-translated

租户自填的邮件服务器与下载素材用同一份放行网段：默认放行 198.18.0.0/15（透明代理的 Fake-IP），从放行网段里
删掉就一起拒；真实内网照拒。`::ffff:0:7f00:1` 在 3.11 里 `is_global` 为真，整段拒绝。

## 其余

失败对外只有几句（连接被拒 / 超时 / 对端核对没过是同一句）；账户引用的密钥放数据目录、不随 JWT 密钥变；
转回平台方的账户记住「服务器是租户填的」直到平台方改掉它；附件名首尾空白读写一致；总结邮件不拼错路径；
实例自有账户出错后会重连。

全文件不联网：主机名解析换成一张表，连接类换成记录参数的替身。地址字面量用拼接得出（公开树扫描）。
"""

from __future__ import annotations

import asyncio
import imaplib
import smtplib
import socket
import time
from email.message import EmailMessage
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest

from nekro_agent.adapters.email.clients import imap_smtp_password as client_module
from nekro_agent.adapters.email.clients.imap_smtp_oauth2 import ImapSmtpOAuth2Client
from nekro_agent.adapters.email.clients.imap_smtp_password import (
    ImapSmtpPasswordClient,
    MailPeerRejectedError,
    PinnedIMAP4SSL,
    PinnedSMTP,
    PinnedSMTPSSL,
    _connect_pinned,
    _connect_via_http_proxy,
)
from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.core.os_env import OsEnv
from nekro_agent.services import email_accounts as mail
from nekro_agent.tools import common_util
from nekro_agent.tools.common_util import DEFAULT_EXTRA_ALLOWED_NETWORKS, UnsafeDownloadUrlError

PRIVATE_IP = ".".join(("10", "0", "0", "5"))
PUBLIC_IP = ".".join(("93", "184", "216", "34"))
OTHER_PUBLIC_IP = ".".join(("93", "184", "216", "35"))
FAKE_IP = ".".join(("198", "18", "0", "9"))
PUBLIC_V6 = "2001:4860:4860::8888"


@pytest.fixture(autouse=True)
def _strict_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
    monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)


def tenant_account(**overrides: Any) -> EmailAccount:
    values: Dict[str, Any] = {
        "USERNAME": "own@example.test",
        "PASSWORD": "authorization-code",
        "EMAIL_ACCOUNT": "自定义",
        "CUSTOM_IMAP_HOST": "imap.example.test",
        "CUSTOM_IMAP_PORT": 993,
        "CUSTOM_SMTP_HOST": "smtp.example.test",
        "CUSTOM_SMTP_PORT": 587,
        "CUSTOM_SMTP_SSL_PORT": 465,
        "CUSTOM_SMTP_USE_SSL": True,
        "ENABLED": True,
    }
    values.update(overrides)
    account = EmailAccount(**values)
    account._tenant_endpoint = True
    return account


class FakeDNS:
    """主机名 → 地址列表（可以按次序给不同的答案，模拟重绑）；表里没有的名字解析失败，不联网"""

    def __init__(self) -> None:
        self.table: Dict[str, List[List[str]]] = {
            "imap.example.test": [[PUBLIC_IP]],
            "smtp.example.test": [[PUBLIC_IP]],
            "img.example.test": [[PUBLIC_IP]],
        }
        self.lookups: Dict[str, int] = {}

    def answer(self, name: str, *answers: List[str]) -> None:
        """依次给出的答案；问的次数超过答案个数时一直给最后一个"""
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
                raise socket.gaierror(socket.EAI_NONAME, "not in the test table")
            addresses = answers[min(count, len(answers) - 1)]
            return [
                (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
                for ip in addresses
            ]

        monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> FakeDNS:
    fake = FakeDNS()
    fake.install(monkeypatch)
    return fake


class _Sock:
    def __init__(self, ip: str) -> None:
        self.ip = ip

    def getpeername(self) -> tuple:
        return (self.ip, 993)


class Dialled:
    """连接类的替身：记下主机名、校验过的地址、代理目标；对端由 `peer` 决定"""

    peer = PUBLIC_IP
    instances: List["Dialled"] = []

    def __init__(
        self,
        host: str,
        port: int,
        *args: Any,
        timeout: Any = None,
        connect_host: str = "",
        ssl_context: Any = None,
        context: Any = None,
    ) -> None:
        self.host = host
        self.port = port
        addresses = args[0] if args and isinstance(args[0], (list, tuple)) else None
        self.addresses = tuple(addresses) if addresses is not None else None
        self.proxy_url = args[0] if args and isinstance(args[0], str) else ""
        self.connect_host = connect_host
        self.ssl_context = ssl_context
        self.context = context
        self.sock = _Sock(Dialled.peer)
        self.calls: List[str] = []
        Dialled.instances.append(self)

    def login(self, *_args: Any) -> tuple:
        self.calls.append("login")
        return ("OK", [b""])

    def authenticate(self, _mechanism: str, _authobject: Any) -> tuple:
        self.calls.append("authenticate")
        return ("OK", [b""])

    def ehlo(self, *_args: Any) -> tuple:
        self.calls.append("ehlo")
        return (250, b"")

    def auth(self, _mechanism: str, _authobject: Any, **_kwargs: Any) -> tuple:
        self.calls.append("auth")
        return (235, b"")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def send_message(self, _message: Any) -> None:
        self.calls.append("send")

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def close(self) -> None:
        self.calls.append("close")

    def logout(self) -> None:
        self.calls.append("logout")

    def __enter__(self) -> "Dialled":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.calls.append("quit")


@pytest.fixture
def dialled(monkeypatch: pytest.MonkeyPatch, dns: FakeDNS) -> SimpleNamespace:
    Dialled.instances = []
    Dialled.peer = PUBLIC_IP
    for name in ("PinnedIMAP4SSL", "PinnedSMTP", "PinnedSMTPSSL", "ProxiedIMAP4SSL", "ProxiedSMTP", "ProxiedSMTPSSL"):
        monkeypatch.setattr(client_module, name, Dialled)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", Dialled)
    monkeypatch.setattr(smtplib, "SMTP", Dialled)
    monkeypatch.setattr(smtplib, "SMTP_SSL", Dialled)
    return SimpleNamespace(cls=Dialled, dns=dns)


class TestTheTenantServerIsResolvedOnceAndDialledByAddress:
    async def test_imap_dials_the_checked_address_and_asks_dns_once(self, dialled: SimpleNamespace) -> None:
        await ImapSmtpPasswordClient(tenant_account(), 20).connect()

        server = dialled.cls.instances[0]
        assert server.host == "imap.example.test", "SNI / 证书主机名仍用主机名"
        assert server.addresses == (PUBLIC_IP,)
        assert dialled.dns.lookups == {"imap.example.test": 1}

    async def test_smtp_dials_the_checked_address_and_asks_dns_once(self, dialled: SimpleNamespace) -> None:
        await ImapSmtpPasswordClient(tenant_account(), 20).send_message(MIMEMultipart())

        server = dialled.cls.instances[0]
        assert server.host == "smtp.example.test"
        assert server.addresses == (PUBLIC_IP,)
        assert server.calls[-2:] == ["send", "quit"]
        assert dialled.dns.lookups == {"smtp.example.test": 1}

    async def test_a_rebinding_name_is_dialled_at_the_first_answer(self, dialled: SimpleNamespace) -> None:
        """第一次问是公网、第二次问是内网：只问一次，所以连的永远是校验过的那一个"""
        dialled.dns.answer("imap.example.test", [PUBLIC_IP], [PRIVATE_IP])

        await ImapSmtpPasswordClient(tenant_account(), 20).connect()

        assert dialled.cls.instances[0].addresses == (PUBLIC_IP,)
        assert dialled.dns.lookups["imap.example.test"] == 1

    @pytest.mark.parametrize("answer", [[PRIVATE_IP], [PUBLIC_IP, PRIVATE_IP]])
    async def test_a_bad_answer_is_refused_before_anything_is_dialled(self, dialled: SimpleNamespace, answer: List[str]) -> None:
        dialled.dns.answer("imap.example.test", answer)

        with pytest.raises(UnsafeDownloadUrlError):
            await ImapSmtpPasswordClient(tenant_account(), 20).connect()

        assert dialled.cls.instances == [], "连都没有连"

    async def test_smtp_with_a_private_answer_is_refused_before_dialling(self, dialled: SimpleNamespace) -> None:
        dialled.dns.answer("smtp.example.test", [PRIVATE_IP])

        with pytest.raises(UnsafeDownloadUrlError):
            await ImapSmtpPasswordClient(tenant_account(), 20).check_smtp_login()

        assert dialled.cls.instances == []

    async def test_a_tenant_server_without_checked_addresses_is_not_dialled(self, dialled: SimpleNamespace) -> None:
        """同步入口直接被调时不能退回去让标准库解析主机名"""
        client = ImapSmtpPasswordClient(tenant_account(), 20)

        with pytest.raises(RuntimeError):
            client._connect_sync()  # noqa: SLF001
        with pytest.raises(RuntimeError):
            client._open_smtp("smtp.example.test", 465, True, 20)  # noqa: SLF001

        assert dialled.cls.instances == []

    async def test_a_platform_account_is_dialled_by_name_as_before(self, dialled: SimpleNamespace) -> None:
        """平台方可以用内网邮件服务器：不经这条解析，照旧交给标准库"""
        account = tenant_account()
        account._tenant_endpoint = False

        await ImapSmtpPasswordClient(account, 20).connect()

        assert dialled.cls.instances[0].addresses is None
        assert dialled.dns.lookups == {}

    async def test_through_the_proxy_the_connect_target_is_the_checked_address(self, dialled: SimpleNamespace) -> None:
        """走代理时 CONNECT 的目标是校验过的地址（不交给代理解析）；对端是代理本身，不核对它"""
        dialled.cls.peer = PRIVATE_IP  # 代理在内网很正常

        client = ImapSmtpPasswordClient(tenant_account(), 20, proxy_url="http://proxy.example.test:7890")
        await client.connect()

        server = dialled.cls.instances[0]
        assert server.connect_host == PUBLIC_IP
        assert "login" in server.calls


class _Ctx:
    def __init__(self) -> None:
        self.server_hostname: Optional[str] = None

    def wrap_socket(self, sock: Any, server_hostname: Optional[str] = None) -> Any:
        self.server_hostname = server_hostname
        return sock


class TestTheRealPinnedClassesDialTheAddress:
    """真的连接类：连的是地址，TLS 的主机名是主机名"""

    @pytest.fixture
    def dials(self, monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
        seen: List[tuple] = []

        def fake_create_connection(address: tuple, *_args: Any, **_kwargs: Any) -> Any:
            seen.append(address)
            return SimpleNamespace()

        monkeypatch.setattr(socket, "create_connection", fake_create_connection)
        return seen

    def test_imap(self, dials: List[tuple]) -> None:
        conn = object.__new__(PinnedIMAP4SSL)
        conn.host, conn.port, conn.pinned_addresses = "imap.example.test", 993, (PUBLIC_IP,)
        conn.ssl_context = _Ctx()

        conn._create_socket(5)  # noqa: SLF001

        assert dials == [(PUBLIC_IP, 993)]
        assert conn.ssl_context.server_hostname == "imap.example.test"

    def test_smtp_ssl(self, dials: List[tuple]) -> None:
        conn = object.__new__(PinnedSMTPSSL)
        conn.pinned_addresses = (PUBLIC_IP,)
        conn.context = _Ctx()

        conn._get_socket("smtp.example.test", 465, 5)  # noqa: SLF001

        assert dials == [(PUBLIC_IP, 465)]
        assert conn.context.server_hostname == "smtp.example.test"

    def test_smtp_plain(self, dials: List[tuple]) -> None:
        conn = object.__new__(PinnedSMTP)
        conn.pinned_addresses = (PUBLIC_IP,)

        conn._get_socket("smtp.example.test", 587, 5)  # noqa: SLF001

        assert dials == [(PUBLIC_IP, 587)]

    def test_the_next_address_is_tried_when_one_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: List[tuple] = []

        def flaky(address: tuple, *_args: Any, **_kwargs: Any) -> Any:
            seen.append(address)
            if address[0] == PUBLIC_IP:
                raise ConnectionRefusedError
            return SimpleNamespace()

        monkeypatch.setattr(socket, "create_connection", flaky)

        _connect_pinned((PUBLIC_IP, OTHER_PUBLIC_IP), 993, 5)

        assert seen == [(PUBLIC_IP, 993), (OTHER_PUBLIC_IP, 993)]

    def test_an_ipv6_connect_target_gets_brackets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: List[bytes] = []

        class _ProxySock:
            def sendall(self, data: bytes) -> None:
                sent.append(data)

            def recv(self, _size: int) -> bytes:
                return b"HTTP/1.1 200 OK\r\n\r\n"

            def close(self) -> None:
                pass

        monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: _ProxySock())

        _connect_via_http_proxy(PUBLIC_V6, 993, "http://proxy.example.test:7890", 5)

        assert sent[0].startswith(f"CONNECT [{PUBLIC_V6}]:993 ".encode())


class TestFakeIpAndTranslatedAddresses:
    async def test_fake_ip_is_allowed_for_a_tenant_mail_server_by_default(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        """与下载同一份放行网段：开着透明代理的部署上，租户自填的邮件服务器照常能用"""
        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
        dns.answer("imap.example.test", [FAKE_IP])

        await mail.assert_custom_mail_host_allowed("imap.example.test", 993)
        common_util.assert_tenant_connected_peer_safe(FAKE_IP)

    async def test_fake_ip_is_refused_for_a_tenant_mail_server_once_removed(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", "")
        dns.answer("imap.example.test", [FAKE_IP])

        with pytest.raises(ValueError, match=mail.CUSTOM_HOST_UNAVAILABLE):
            await mail.assert_custom_mail_host_allowed("imap.example.test", 993)
        with pytest.raises(UnsafeDownloadUrlError):
            common_util.assert_tenant_connected_peer_safe(FAKE_IP)

    async def test_fake_ip_stays_allowed_for_downloads(self) -> None:
        await common_util.assert_download_url_safe(f"https://{FAKE_IP}/a.png")

    async def test_extra_allowed_networks_apply_to_a_tenant_mail_server_too(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        """部署方额外放行的网段对下载与租户自填的邮件服务器一并生效（同一份配置）"""
        cgnat_ip = ".".join(("100", "64", "0", "7"))
        dns.answer("imap.example.test", [cgnat_ip])

        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
        with pytest.raises(ValueError, match=mail.CUSTOM_HOST_UNAVAILABLE):
            await mail.assert_custom_mail_host_allowed("imap.example.test", 993)
        with pytest.raises(UnsafeDownloadUrlError):
            common_util.assert_tenant_connected_peer_safe(cgnat_ip)

        networks = ",".join((DEFAULT_EXTRA_ALLOWED_NETWORKS, ".".join(("100", "64", "0", "0")) + "/10"))
        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", networks)
        await common_util.assert_download_url_safe(f"https://{cgnat_ip}/a.png")
        await mail.assert_custom_mail_host_allowed("imap.example.test", 993)
        common_util.assert_tenant_connected_peer_safe(cgnat_ip)

    def test_the_peer_check_message_carries_no_address(self) -> None:
        with pytest.raises(UnsafeDownloadUrlError) as excinfo:
            common_util.assert_tenant_connected_peer_safe(PRIVATE_IP)
        assert PRIVATE_IP not in str(excinfo.value)

    async def test_an_ipv4_translated_address_is_refused(self) -> None:
        import ipaddress

        translated = "::ffff:0:7f00:1"
        assert ipaddress.ip_address(translated).is_global, "前提：3.11 认为它是公网"
        assert not common_util._is_allowed_target_ip(ipaddress.ip_address(translated))  # noqa: SLF001
        with pytest.raises(UnsafeDownloadUrlError):
            await common_util.assert_download_url_safe(f"http://[{translated}]/a.png")


class TestFailuresReadTheSame:
    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            (UnsafeDownloadUrlError("x"), mail.MAIL_FAILURE_HOST),
            (ValueError(mail.CUSTOM_HOST_UNAVAILABLE), mail.MAIL_FAILURE_HOST),
            (smtplib.SMTPAuthenticationError(535, b"no"), mail.MAIL_FAILURE_LOGIN),
            (imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] no"), mail.MAIL_FAILURE_LOGIN),
            (smtplib.SMTPRecipientsRefused({"a@example.test": (550, b"no")}), mail.MAIL_FAILURE_REJECTED),
            (MailPeerRejectedError("x"), mail.MAIL_FAILURE_CONNECT),
            (ConnectionRefusedError(), mail.MAIL_FAILURE_CONNECT),
            (TimeoutError(), mail.MAIL_FAILURE_CONNECT),
            (imaplib.IMAP4.abort("socket error"), mail.MAIL_FAILURE_CONNECT),
            (RuntimeError(f"220 relay at {PRIVATE_IP}"), mail.MAIL_FAILURE_CONNECT),
        ],
    )
    def test_the_classification(self, error: BaseException, kind: str) -> None:
        assert mail.classify_mail_failure(error) == kind

    def test_the_public_text_never_carries_the_peer(self) -> None:
        for error in (ConnectionRefusedError(f"{PRIVATE_IP}:25 refused"), RuntimeError(f"banner {PRIVATE_IP}")):
            text = mail.public_mail_failure(error)
            assert PRIVATE_IP not in text
            assert text == mail.MAIL_FAILURE_TEXTS[mail.MAIL_FAILURE_CONNECT]

    def test_our_own_business_texts_pass_through(self) -> None:
        assert mail.public_mail_failure(ValueError(mail.NO_SENDER)) == mail.NO_SENDER

    async def test_the_remote_sticker_reads_the_same_for_every_network_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from plugins.builtin import emotion

        for error in (httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), UnsafeDownloadUrlError(f"-> {PRIVATE_IP}")):

            async def boom(*_args: Any, _error: BaseException = error, **_kwargs: Any) -> bytes:
                raise _error

            monkeypatch.setattr(emotion, "fetch_url_bytes", boom)
            with pytest.raises(ValueError) as excinfo:
                await emotion._read_image_bytes(SimpleNamespace(), "https://img.example.test/a.png")  # noqa: SLF001
            assert str(excinfo.value) == emotion._REMOTE_IMAGE_REFUSED  # noqa: SLF001


class TestTheRemoteImageGoesToTheCheckedAddress:
    """`fetch_url_bytes`（表情收集等）建连接时连校验过的地址：与 `download_file` 同一个传输层"""

    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch) -> Any:
        import httpcore

        from tests.test_tenant_egress import FakeBackend

        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        backend = FakeBackend(responses=[[b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n\x89PNG"]])
        monkeypatch.setattr(httpcore, "AnyIOBackend", lambda: backend)
        return backend

    async def test_the_connection_goes_to_the_checked_address_with_the_original_host(self, monkeypatch: pytest.MonkeyPatch, dns: FakeDNS) -> None:
        backend = self._install(monkeypatch)
        dns.answer("img.example.test", [PUBLIC_IP])

        assert await common_util.fetch_url_bytes("https://img.example.test/a.png") == b"\x89PNG"

        assert backend.dials == [(PUBLIC_IP, 443)]
        assert backend.tls_hostnames == ["img.example.test"]
        assert "Host: img.example.test\r\n" in backend.request_text()

    async def test_an_answer_rebound_after_the_check_is_never_dialled(self, monkeypatch: pytest.MonkeyPatch, dns: FakeDNS) -> None:
        """预检时是公网、建连接时改口成内网：建连接那一步按同一判据再查一次，一次都不连"""
        backend = self._install(monkeypatch)
        dns.answer("img.example.test", [PUBLIC_IP], [PRIVATE_IP])

        with pytest.raises(UnsafeDownloadUrlError):
            await common_util.fetch_url_bytes("https://img.example.test/a.png")

        assert backend.dials == []

    async def test_a_private_answer_is_never_requested(self, monkeypatch: pytest.MonkeyPatch, dns: FakeDNS) -> None:
        backend = self._install(monkeypatch)
        dns.answer("img.example.test", [PRIVATE_IP])

        with pytest.raises(UnsafeDownloadUrlError):
            await common_util.fetch_url_bytes("https://img.example.test/a.png")

        assert backend.dials == []


class TestTheAccountReferenceKeyDoesNotRotate:
    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> Any:
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
        mail.reset_account_ref_key_cache()
        yield tmp_path / "data"
        mail.reset_account_ref_key_cache()

    def test_a_restart_with_a_new_jwt_key_keeps_the_reference(self, monkeypatch: pytest.MonkeyPatch, _isolated: Any) -> None:
        before = mail.platform_account_ref("ops@example.test")
        assert (_isolated / mail.ACCOUNT_REF_KEY_FILE).is_file()

        monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", "secret:rotated-for-the-test")
        mail.reset_account_ref_key_cache()  # 等于重启

        assert mail.platform_account_ref("ops@example.test") == before

    def test_without_a_writable_data_dir_it_falls_back_to_the_jwt_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.tools import local_secret

        monkeypatch.setattr(local_secret, "load_or_create_local_secret", lambda *_a, **_k: None)
        monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", "secret:a")
        first = mail.platform_account_ref("ops@example.test")
        monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", "secret:b")
        mail.reset_account_ref_key_cache()

        assert mail.platform_account_ref("ops@example.test") != first


def moved_back(**overrides: Any) -> EmailAccount:
    """从实例转回平台方的账户：运行时标记没了，靠落盘的 `TENANT_SUPPLIED_HOSTS`"""
    account = EmailAccount(**tenant_account(**overrides).model_dump())
    account.TENANT_SUPPLIED_HOSTS = account.tenant_hosts_marker()
    return account


class TestTheTenantMarkerSurvivesTheMoveBack:
    def test_the_marker_holds_until_the_platform_changes_the_servers(self) -> None:
        moved = EmailAccount(**tenant_account().model_dump())
        assert moved.is_tenant_endpoint() is False, "前提：运行时标记不落盘"

        moved.TENANT_SUPPLIED_HOSTS = moved.tenant_hosts_marker()
        reloaded = EmailAccount(**moved.model_dump())
        assert reloaded.is_tenant_endpoint() is True, "落盘的标记读回来仍然有效"

        reloaded.CUSTOM_IMAP_HOST = "imap.platform.example.test"
        reloaded.CUSTOM_SMTP_HOST = "smtp.platform.example.test"
        assert reloaded.is_tenant_endpoint() is False, "平台方把两台都换了，就是平台方自己选的了"

    def test_changing_only_the_imap_host_keeps_the_smtp_host_marked(self) -> None:
        """按主机判：平台方只换了 IMAP，没动过的 SMTP 主机仍是租户的"""
        account = moved_back()
        account.CUSTOM_IMAP_HOST = "imap.platform.example.test"

        assert account.is_tenant_host("imap.platform.example.test") is False
        assert account.is_tenant_host("smtp.example.test") is True
        assert account.is_tenant_endpoint() is True

    def test_the_marker_compares_normalized_hosts(self) -> None:
        """大小写、首尾空白、末尾的点都不能让同一台主机逃出标记"""
        account = moved_back(CUSTOM_IMAP_HOST=" IMAP.Example.TEST. ")

        assert account.TENANT_SUPPLIED_HOSTS == "imap.example.test,smtp.example.test"
        for spelling in ("imap.example.test", "imap.example.test.", "Imap.Example.Test", " imap.example.test "):
            assert account.is_tenant_host(spelling) is True, spelling
        assert account.is_tenant_host("") is False
        assert account.is_tenant_host("example.test") is False

    @pytest.mark.parametrize(
        "rewritten",
        [
            "imap。example。test",  # 中文句点
            "imap．example．test",  # 全角句点
            "imap｡example｡test｡",  # 半角句点，末尾再带一个
            "ＩＭＡＰ.example.test",  # 全角大写字母
        ],
    )
    def test_a_rewritten_spelling_of_the_same_host_stays_marked(self, rewritten: str) -> None:
        """平台方把带标记的主机改写成别的写法，连接时解析出来是同一台：标记不能失效"""
        assert rewritten.encode("idna").decode("ascii").rstrip(".") == "imap.example.test", "前提：连接时就是同一台"
        account = moved_back()

        account.CUSTOM_IMAP_HOST = rewritten

        assert account.is_tenant_host(rewritten) is True
        assert account.is_tenant_endpoint() is True

    def test_the_unicode_and_punycode_forms_are_the_same_host(self) -> None:
        unicode_host = "imap.例子.test"
        punycode_host = unicode_host.encode("idna").decode("ascii")
        assert punycode_host.startswith("imap.xn--"), "前提"

        marked_with_punycode = moved_back(CUSTOM_IMAP_HOST=punycode_host)
        marked_with_punycode.CUSTOM_IMAP_HOST = unicode_host
        assert marked_with_punycode.is_tenant_host(unicode_host) is True

        marked_with_unicode = moved_back(CUSTOM_IMAP_HOST=unicode_host)
        assert marked_with_unicode.TENANT_SUPPLIED_HOSTS.split(",")[0] == punycode_host
        assert marked_with_unicode.is_tenant_host(punycode_host.upper()) is True

    def test_a_host_idna_cannot_encode_is_compared_as_written(self) -> None:
        too_long = "a" * 70 + ".例.test"
        account = moved_back(CUSTOM_IMAP_HOST=too_long)

        assert account.is_tenant_host(too_long) is True
        assert account.is_tenant_host("imap.example.test") is False

    def test_an_unmarked_platform_account_is_never_a_tenant_host(self) -> None:
        account = EmailAccount(**tenant_account().model_dump())

        assert account.TENANT_SUPPLIED_HOSTS == ""
        assert account.is_tenant_host("imap.example.test") is False

    async def test_a_marked_platform_account_is_dialled_by_address(self, dialled: SimpleNamespace) -> None:
        await ImapSmtpPasswordClient(moved_back(), 20).connect()

        assert dialled.cls.instances[0].addresses == (PUBLIC_IP,)

    async def test_with_only_the_imap_host_changed_smtp_is_still_pinned(self, dialled: SimpleNamespace) -> None:
        account = moved_back()
        account.CUSTOM_IMAP_HOST = "imap.platform.example.test"
        client = ImapSmtpPasswordClient(account, 20)

        await client.connect()
        await client.send_message(MIMEMultipart())

        imap, smtp = dialled.cls.instances
        assert imap.host == "imap.platform.example.test" and imap.addresses is None, "平台方换上的主机照旧交给标准库"
        assert smtp.host == "smtp.example.test" and smtp.addresses == (PUBLIC_IP,)
        assert dialled.dns.lookups == {"smtp.example.test": 1}


def oauth_account(**overrides: Any) -> EmailAccount:
    """转回平台方、之后改成 OAuth2 的账户；令牌未过期，取令牌不联网"""
    return moved_back(AUTH_TYPE="oauth2", ACCESS_TOKEN="access-token", TOKEN_EXPIRES_AT=int(time.time()) + 3600, **overrides)


class TestOAuth2TakesTheSamePinnedPath:
    """认证方式换成 XOAUTH2，主机的 DNS 仍可能在租户手里：连接走同一条（只解析一次、直连、登录前核对）"""

    async def test_imap_dials_the_checked_address(self, dialled: SimpleNamespace) -> None:
        await ImapSmtpOAuth2Client(oauth_account(), 20).connect()

        server = dialled.cls.instances[0]
        assert server.host == "imap.example.test"
        assert server.addresses == (PUBLIC_IP,)
        assert server.calls == ["authenticate"]
        assert dialled.dns.lookups == {"imap.example.test": 1}

    async def test_smtp_dials_the_checked_address(self, dialled: SimpleNamespace) -> None:
        await ImapSmtpOAuth2Client(oauth_account(), 20).send_message(MIMEMultipart())

        server = dialled.cls.instances[0]
        assert server.host == "smtp.example.test"
        assert server.addresses == (PUBLIC_IP,)
        assert server.calls == ["ehlo", "auth", "send", "quit"]
        assert dialled.dns.lookups == {"smtp.example.test": 1}

    async def test_starttls_comes_before_the_token(self, dialled: SimpleNamespace) -> None:
        await ImapSmtpOAuth2Client(oauth_account(CUSTOM_SMTP_USE_SSL=False), 20).send_message(MIMEMultipart())

        assert dialled.cls.instances[0].calls == ["ehlo", "starttls", "ehlo", "auth", "send", "quit"]

    async def test_a_private_answer_is_refused_before_dialling(self, dialled: SimpleNamespace) -> None:
        dialled.dns.answer("imap.example.test", [PRIVATE_IP])
        dialled.dns.answer("smtp.example.test", [PRIVATE_IP])
        client = ImapSmtpOAuth2Client(oauth_account(), 20)

        with pytest.raises(UnsafeDownloadUrlError):
            await client.connect()
        with pytest.raises(UnsafeDownloadUrlError):
            await client.send_message(MIMEMultipart())

        assert dialled.cls.instances == []

    async def test_a_private_peer_is_cut_before_the_token_is_sent(self, dialled: SimpleNamespace) -> None:
        dialled.cls.peer = PRIVATE_IP
        client = ImapSmtpOAuth2Client(oauth_account(), 20)

        with pytest.raises(MailPeerRejectedError):
            await client.connect()
        with pytest.raises(MailPeerRejectedError):
            await client.send_message(MIMEMultipart())

        imap, smtp = dialled.cls.instances
        assert imap.calls == ["shutdown"]
        assert smtp.calls == ["close"]

    async def test_an_unmarked_platform_account_is_dialled_by_name(self, dialled: SimpleNamespace) -> None:
        account = oauth_account()
        account.TENANT_SUPPLIED_HOSTS = ""

        await ImapSmtpOAuth2Client(account, 20).connect()

        assert dialled.cls.instances[0].addresses is None
        assert dialled.dns.lookups == {}


class TestTheStatusMailSkipsATenantServer:
    """状态邮件经 aiosmtplib 按主机名直连，没有「只连校验过的地址」那一层：发件邮箱的 SMTP 主机是租户的就不发"""

    @pytest.fixture
    def sent(self, monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
        from nekro_agent.services.mail import mail_service

        calls: List[Dict[str, Any]] = []

        async def fake_send(_message: Any, **kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(mail_service, "send", fake_send)
        return calls

    @staticmethod
    def _use_sender(monkeypatch: pytest.MonkeyPatch, account: EmailAccount) -> None:
        from nekro_agent.services.mail import mail_service

        config = SimpleNamespace(
            STATUS_MAIL_ENABLED=True,
            RECEIVE_ACCOUNTS=[account],
            STATUS_MAIL_SENDER_ACCOUNT=account.USERNAME,
            STATUS_MAIL_TARGETS=[SimpleNamespace(EMAIL="ops@example.test")],
        )
        monkeypatch.setattr(mail_service, "_load_email_adapter_config", lambda: config)

    async def test_a_sender_on_a_tenant_smtp_host_sends_nothing(self, monkeypatch: pytest.MonkeyPatch, sent: List[Dict[str, Any]]) -> None:
        from nekro_agent.services.mail import mail_service

        self._use_sender(monkeypatch, moved_back(SEND_ENABLED=True))

        await mail_service.send_email(EmailMessage())

        assert sent == []

    async def test_once_the_platform_changed_the_smtp_host_it_sends(self, monkeypatch: pytest.MonkeyPatch, sent: List[Dict[str, Any]]) -> None:
        """只看 SMTP 主机：IMAP 主机仍是租户的也不影响状态邮件"""
        from nekro_agent.services.mail import mail_service

        account = moved_back(SEND_ENABLED=True)
        account.CUSTOM_SMTP_HOST = "smtp.platform.example.test"
        self._use_sender(monkeypatch, account)

        await mail_service.send_email(EmailMessage())

        assert [call["hostname"] for call in sent] == ["smtp.platform.example.test"]

    async def test_an_ordinary_platform_sender_sends_as_before(self, monkeypatch: pytest.MonkeyPatch, sent: List[Dict[str, Any]]) -> None:
        from nekro_agent.services.mail import mail_service

        self._use_sender(monkeypatch, EmailAccount(**tenant_account(SEND_ENABLED=True).model_dump()))

        await mail_service.send_email(EmailMessage())

        assert [call["hostname"] for call in sent] == ["smtp.example.test"]


class TestAttachmentNamesAreCleanedTheSameWay:
    async def test_a_name_with_surrounding_spaces_is_read_where_it_was_written(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.adapters.email.adapter import EmailAdapter
        from plugins.builtin import email_utils

        class _Bare(EmailAdapter):
            def __init__(self) -> None:  # noqa: D107 - 只够跑 `_download_attachment`
                pass

        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(email_utils, "ATTACHMENT_BASE_DIR", mail.email_attachment_root())
        part = MIMEApplication(b"payload", Name=" report.pdf ")

        await _Bare()._download_attachment(part, " report.pdf ", "own@example.test", "主题", "7", directory="_i1")  # noqa: SLF001
        read_path = email_utils._ensure_attachment_path("_i1", "7", " report.pdf ")  # noqa: SLF001

        assert read_path.read_bytes() == b"payload"
        assert read_path == mail.attachment_file_path("_i1", "7", " report.pdf ")


class TestTheSummaryPathCarriesNoHostPath:
    async def test_attachments_are_names_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from plugins.builtin import email_utils

        message = MIMEMultipart()
        message["Subject"] = "周报"
        message["From"] = "boss@example.test"
        message.attach(MIMEText("正文", "plain", "utf-8"))
        message.attach(MIMEApplication(b"x", Name="a.pdf", _subtype="pdf"))
        message.get_payload()[1].add_header("Content-Disposition", "attachment", filename="a.pdf")  # type: ignore[index, union-attr]

        class _Adapter:
            async def imap_fetch(self, _conn: Any, _email_id: Any, _spec: str) -> tuple:
                return ("OK", [(b"1 (RFC822)", message.as_bytes())])

        monkeypatch.setattr(email_utils.adapter_utils, "get_adapter", lambda _key: _Adapter())

        result = await email_utils._fetch_email_content(SimpleNamespace(), object(), "own@example.test", b"1", 0)  # noqa: SLF001

        assert result is not None
        assert result["attachments"] == [{"filename": "a.pdf"}]
        assert "host_path" not in str(result)


class TestAnInstanceAccountIsReconnected:
    @staticmethod
    def _adapter(instance_account: Optional[EmailAccount], platform: Optional[List[EmailAccount]] = None) -> Any:
        from nekro_agent.adapters.email.adapter import EmailAdapter

        class _Bare(EmailAdapter):
            def __init__(self) -> None:  # noqa: D107 - 只够跑拉信与重连那一段
                self._test_config = SimpleNamespace(
                    RECEIVE_ACCOUNTS=list(platform or []),
                    FETCH_UNSEEN_ONLY=True,
                    MAX_PER_POLL=5,
                    MARK_AS_SEEN_AFTER_FETCH=False,
                )
                self.imap_locks = {"own@example.test": asyncio.Lock()}
                self.instance_account_owner = {"own@example.test": object()}
                self.reconnected: List[EmailAccount] = []

            @property
            def config(self) -> Any:  # type: ignore[override]
                return self._test_config

            async def _load_instance_accounts(self) -> List[Any]:
                return [(instance_account, object())] if instance_account is not None else []

            async def _reconnect_email_client(self, account: EmailAccount) -> bool:
                self.reconnected.append(account)
                return True

        return _Bare()

    async def test_a_failing_instance_account_is_reconnected(self) -> None:
        account = tenant_account(RECEIVE_ENABLED=True)
        adapter = self._adapter(account)

        class _Broken:
            async def select_mailbox(self) -> str:
                raise imaplib.IMAP4.abort("socket error")

        result = await adapter._pull_account_emails("own@example.test", _Broken(), reconnect_on_error=True)  # noqa: SLF001

        assert result.reconnect_attempted is True
        assert [item.USERNAME for item in adapter.reconnected] == ["own@example.test"]

    async def test_a_receive_disabled_instance_account_is_not_reconnected(self) -> None:
        adapter = self._adapter(tenant_account(RECEIVE_ENABLED=False))

        assert await adapter._account_for_reconnect("own@example.test") is None  # noqa: SLF001

    async def test_a_platform_account_is_found_as_before(self) -> None:
        platform = EmailAccount(USERNAME="ops@example.test", RECEIVE_ENABLED=True, ENABLED=True)
        adapter = self._adapter(None, [platform])

        assert await adapter._account_for_reconnect("ops@example.test") is platform  # noqa: SLF001
        assert await adapter._account_for_reconnect("nobody@example.test") is None  # noqa: SLF001
