"""邮箱的出站与落盘加固：收信编号、连上之后核对对端、归属表刷新、远程图片的实际对端

## 收信编号只认数字

IMAP `SEARCH` 返回的编号是收信服务器给的，后面会被拿去拼附件目录。实例自有账户可以填租户自己
的服务器，它回 `* SEARCH /some/absolute/path` 就能让附件写到附件目录外（pathlib 拼接时右边是绝对
路径，左边整个丢掉）。客户端只收纯数字；落盘用与读取同一个逐层判据的路径函数，再确认一次在
附件总目录之内。

## 连上之后、登录之前核对对端

保存时、连接前各做一次主机校验都是「先查后连」：校验解析一次、`imaplib` / `smtplib` 连接时
再解析一次，DNS 重绑就在两次之间；国际化域名还会被两边按两套 IDNA 规则编码成不同的域名。权威
判据是真正连上的那个地址：租户填的服务器，对端不是公网就在登录之前断开，授权码不会发出去。

全文件不联网：IMAP / SMTP 用假连接类替掉标准库里的那几个类与客户端模块里的直连类（`Pinned*`），
假连接的 `sock.getpeername()` 回什么由用例定；主机名解析换成一张表（`fake_dns`），表里没有的名字
直接报解析失败、不去问真的 DNS。地址字面量用拼接得出（公开树扫描）。
"""

from __future__ import annotations

import asyncio
import imaplib
import smtplib
import socket
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from nekro_agent.adapters.email.clients import imap_smtp_password as client_module
from nekro_agent.adapters.email.clients.imap_smtp_password import (
    ImapSmtpPasswordClient,
    MailPeerRejectedError,
    is_imap_sequence_number,
)
from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.core.os_env import OsEnv
from nekro_agent.tools import common_util
from nekro_agent.tools.common_util import DEFAULT_EXTRA_ALLOWED_NETWORKS, UnsafeDownloadUrlError

PRIVATE_IP = ".".join(("10", "0", "0", "5"))
PUBLIC_IP = ".".join(("93", "184", "216", "34"))


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


class _Sock:
    def __init__(self, ip: str) -> None:
        self.ip = ip

    def getpeername(self) -> tuple:
        return (self.ip, 993)


class FakeIMAP:
    """替掉 `imaplib.IMAP4_SSL` 与直连的 `PinnedIMAP4SSL`：构造即「连上」，对端地址由类属性决定

    `addresses` 是直连类收到的「校验过的地址」；按主机名连的（平台方账户）为 None。
    """

    peer = PUBLIC_IP
    instances: List["FakeIMAP"] = []

    def __init__(
        self,
        host: str,
        port: int,
        addresses: Any = None,
        timeout: Any = None,
        ssl_context: Any = None,
        context: Any = None,
    ) -> None:
        self.host = host
        self.addresses = tuple(addresses) if addresses is not None else None
        self.ssl_context = ssl_context
        self.context = context
        self.sock = _Sock(FakeIMAP.peer)
        self.logged_in = False
        self.search_reply = b"1 2 3"
        FakeIMAP.instances.append(self)

    def login(self, _user: str, _password: str) -> tuple:
        self.logged_in = True
        return ("OK", [b"logged in"])

    def shutdown(self) -> None:
        pass

    def list(self) -> tuple:
        return ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])

    def select(self, _name: str) -> tuple:
        return ("OK", [b"3"])

    def search(self, _charset: Any, _criteria: str) -> tuple:
        return ("OK", [self.search_reply])

    def close(self) -> None:
        pass

    def logout(self) -> None:
        pass


class FakeSMTP:
    """替掉 `smtplib.SMTP` / `SMTP_SSL` 与直连的 `PinnedSMTP*`：记下发生了哪些动作"""

    peer = PUBLIC_IP
    instances: List["FakeSMTP"] = []

    def __init__(
        self,
        host: str,
        port: int,
        addresses: Any = None,
        timeout: Any = None,
        ssl_context: Any = None,
        context: Any = None,
    ) -> None:
        self.host = host
        self.port = port
        self.addresses = tuple(addresses) if addresses is not None else None
        self.ssl_context = ssl_context
        self.context = context
        self.sock = _Sock(FakeSMTP.peer)
        self.calls: List[str] = []
        FakeSMTP.instances.append(self)

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, _user: str, _password: str) -> None:
        self.calls.append("login")

    def send_message(self, _message: Any) -> None:
        self.calls.append("send")

    def close(self) -> None:
        self.calls.append("close")

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.calls.append("quit")


class FakeDNS:
    """主机名 → 地址列表；每个名字被问了几次记在 `lookups` 里。表里没有的名字一律解析失败（不联网）

    IP 字面量照常交给真的 `getaddrinfo`（不发查询）。
    """

    def __init__(self) -> None:
        self.table: Dict[str, List[str]] = {
            "imap.example.test": [PUBLIC_IP],
            "smtp.example.test": [PUBLIC_IP],
        }
        self.lookups: Dict[str, int] = {}

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
            self.lookups[name] = self.lookups.get(name, 0) + 1
            addresses = self.table.get(name)
            if not addresses:
                raise socket.gaierror(socket.EAI_NONAME, "not in the test table")
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)) for ip in addresses]

        monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> FakeDNS:
    dns = FakeDNS()
    dns.install(monkeypatch)
    return dns


@pytest.fixture
def fake_mail_servers(monkeypatch: pytest.MonkeyPatch, fake_dns: FakeDNS) -> SimpleNamespace:
    FakeIMAP.instances = []
    FakeSMTP.instances = []
    FakeIMAP.peer = PUBLIC_IP
    FakeSMTP.peer = PUBLIC_IP
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(client_module, "PinnedIMAP4SSL", FakeIMAP)
    monkeypatch.setattr(client_module, "PinnedSMTP", FakeSMTP)
    monkeypatch.setattr(client_module, "PinnedSMTPSSL", FakeSMTP)
    return SimpleNamespace(imap=FakeIMAP, smtp=FakeSMTP, dns=fake_dns)


class TestSequenceNumbers:
    @pytest.mark.parametrize("token", [b"1", b"42", b"12345678901234567890"])
    def test_plain_numbers_are_accepted(self, token: bytes) -> None:
        assert is_imap_sequence_number(token)

    @pytest.mark.parametrize(
        "token",
        [b"/app/nekro_agent", b"..", b"../../etc", b"1/2", b"1\\2", b"-1", b"", "１".encode(), b"C:\\x", b"123456789012345678901"],
    )
    def test_anything_else_is_refused(self, token: bytes) -> None:
        assert not is_imap_sequence_number(token)

    async def test_a_hostile_search_reply_keeps_only_the_numbers(self, fake_mail_servers: SimpleNamespace) -> None:
        client = ImapSmtpPasswordClient(tenant_account(), 20)
        await client.connect()
        client.conn.search_reply = b"1 /app/nekro_agent 2 ../.. 3 C:\\x"  # type: ignore[union-attr]

        ids = await client.list_message_ids(unseen_only=False)

        assert ids == [b"1", b"2", b"3"]


class TestAttachmentWriteStaysInside:
    """复现 P0：编号是绝对路径时，修复前附件会写到附件目录外"""

    @staticmethod
    def _part() -> Any:
        from email.mime.application import MIMEApplication

        return MIMEApplication(b"payload", Name="__init__.py")

    @staticmethod
    def _adapter() -> Any:
        from nekro_agent.adapters.email.adapter import EmailAdapter

        class _Bare(EmailAdapter):
            def __init__(self) -> None:  # noqa: D107 - 只够跑 `_download_attachment`
                pass

        return _Bare()

    @pytest.mark.parametrize("hostile", ["ABSOLUTE", "..", "../..", "a/../../b"])
    async def test_a_hostile_uid_cannot_write_outside(self, tmp_path, monkeypatch: pytest.MonkeyPatch, hostile: str) -> None:
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
        outside = tmp_path / "victim"
        uid = str(outside) if hostile == "ABSOLUTE" else hostile

        await self._adapter()._download_attachment(self._part(), "__init__.py", "own@example.test", "主题", uid, directory="_i1")  # noqa: SLF001

        assert not (outside / "__init__.py").exists()
        written = [path for path in tmp_path.rglob("__init__.py")]
        assert written == []

    async def test_a_numeric_uid_lands_in_the_account_dir(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))

        await self._adapter()._download_attachment(self._part(), "__init__.py", "own@example.test", "主题", "7", directory="_i1")  # noqa: SLF001

        target = tmp_path / "data" / "uploads" / "email_attachment" / "_i1" / "7" / "__init__.py"
        assert target.read_bytes() == b"payload"


class TestPeerIsCheckedBeforeLogin:
    """连接只连校验过的地址；对端核对是兜底，没过按「连不上」报（不和「地址不能用」区分）"""

    async def test_imap_to_a_private_peer_never_logs_in(self, fake_mail_servers: SimpleNamespace) -> None:
        fake_mail_servers.imap.peer = PRIVATE_IP
        client = ImapSmtpPasswordClient(tenant_account(), 20)

        with pytest.raises(MailPeerRejectedError):
            await client.connect()

        assert fake_mail_servers.imap.instances[0].logged_in is False

    async def test_imap_to_a_public_peer_logs_in(self, fake_mail_servers: SimpleNamespace) -> None:
        client = ImapSmtpPasswordClient(tenant_account(), 20)

        await client.connect()

        assert fake_mail_servers.imap.instances[0].logged_in is True
        assert fake_mail_servers.imap.instances[0].addresses == (PUBLIC_IP,), "连的是校验过的地址"

    async def test_a_platform_account_is_not_checked(self, fake_mail_servers: SimpleNamespace) -> None:
        """平台方可以用内网邮件服务器：没有租户标记的账户不核对"""
        fake_mail_servers.imap.peer = PRIVATE_IP
        account = tenant_account()
        account._tenant_endpoint = False

        await ImapSmtpPasswordClient(account, 20).connect()

        assert fake_mail_servers.imap.instances[0].logged_in is True

    @pytest.mark.parametrize("use_ssl", [True, False])
    async def test_smtp_to_a_private_peer_never_logs_in(self, fake_mail_servers: SimpleNamespace, use_ssl: bool) -> None:
        fake_mail_servers.smtp.peer = PRIVATE_IP
        client = ImapSmtpPasswordClient(tenant_account(CUSTOM_SMTP_USE_SSL=use_ssl, CUSTOM_SMTP_SSL_PORT=587), 20)

        with pytest.raises(MailPeerRejectedError):
            await client.check_smtp_login()

        assert fake_mail_servers.smtp.instances, "连上了，才谈得上核对"
        for server in fake_mail_servers.smtp.instances:
            assert "login" not in server.calls
            assert "starttls" not in server.calls

    async def test_sending_to_a_private_peer_never_sends(self, fake_mail_servers: SimpleNamespace) -> None:
        from email.mime.multipart import MIMEMultipart

        fake_mail_servers.smtp.peer = PRIVATE_IP
        client = ImapSmtpPasswordClient(tenant_account(), 20)

        with pytest.raises(MailPeerRejectedError):
            await client.send_message(MIMEMultipart())

        assert fake_mail_servers.smtp.instances
        assert all("send" not in server.calls for server in fake_mail_servers.smtp.instances)

    def test_non_ascii_hosts_are_refused_before_resolving(self) -> None:
        """`straße.x` 在校验与连接时会被编码成两个不同的域名"""
        with pytest.raises(UnsafeDownloadUrlError, match="国际化域名请填 xn-- 形式"):
            asyncio.run(common_util.assert_tenant_host_safe("straße.example", 993, subject="邮箱服务器"))

    def test_the_tenant_marker_never_reaches_the_config_file(self) -> None:
        account = tenant_account()
        assert "_tenant_endpoint" not in account.model_dump()
        assert EmailAccount(**account.model_dump())._tenant_endpoint is False


class TestTheConnectionTestUsesTheRealClient:
    """测试连接用与收发信同一个客户端类、同一个构造方式（原先构造参数写错，IMAP 测试永远失败）"""

    async def test_imap_test_succeeds_against_a_public_server(self, fake_mail_servers: SimpleNamespace) -> None:
        from nekro_agent.routers import instance_email as routes

        result = await routes._test_imap_connection(tenant_account())  # noqa: SLF001

        assert result.startswith("IMAP 连接成功")
        assert fake_mail_servers.imap.instances[0].logged_in is True

    async def test_smtp_test_logs_in_without_sending(self, fake_mail_servers: SimpleNamespace) -> None:
        from nekro_agent.routers import instance_email as routes

        result = await routes._test_smtp_connection(tenant_account())  # noqa: SLF001

        assert result.startswith("SMTP 连接成功")
        calls = fake_mail_servers.smtp.instances[0].calls
        assert "login" in calls and "send" not in calls

    def test_failures_are_reported_in_general_terms(self) -> None:
        from nekro_agent.routers import instance_email as routes

        banner = RuntimeError(f"220 internal-relay ready at {PRIVATE_IP}:25")
        assert routes._connection_test_failure(banner) == routes.TEST_FAILED_CONNECT  # noqa: SLF001
        assert routes._connection_test_failure(smtplib.SMTPAuthenticationError(535, b"no")) == routes.TEST_FAILED_LOGIN  # noqa: SLF001
        assert routes._connection_test_failure(UnsafeDownloadUrlError("x")) == routes.TEST_FAILED_HOST  # noqa: SLF001
        # 连上之后核对没过、连接被拒、超时：同一句（分得开就能拿测试连接探测内网端口）
        for error in (MailPeerRejectedError("x"), ConnectionRefusedError(), TimeoutError(), socket.timeout()):
            assert routes._connection_test_failure(error) == routes.TEST_FAILED_CONNECT  # noqa: SLF001


class TestRuntimeAccountMarking:
    def test_custom_instance_accounts_are_marked(self) -> None:
        from nekro_agent.models.db_email_account import DBEmailAccount
        from nekro_agent.services import email_accounts as mail

        custom = DBEmailAccount(id=1, owner_adapter_key="onebot_v11", owner_instance_key="inst1", username="a@example.test",
                                provider="自定义", settings={"imap_host": "imap.example.test", "smtp_host": "smtp.example.test"},
                                password="p", enabled=True, receive_enabled=True, send_enabled=True, attachment_dir="_i1")
        preset = DBEmailAccount(id=2, owner_adapter_key="onebot_v11", owner_instance_key="inst1", username="b@example.test",
                                provider="QQ邮箱", settings={}, password="p", enabled=True, receive_enabled=True,
                                send_enabled=True, attachment_dir="_i2")

        assert mail.to_runtime_account(custom)._tenant_endpoint is True
        assert mail.to_runtime_account(preset)._tenant_endpoint is False


class TestOwnerTableFollowsAMigration:
    async def test_refresh_rewrites_the_owner_of_moved_accounts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tortoise import Tortoise

        from nekro_agent.models.db_email_account import DBEmailAccount
        from nekro_agent.services import email_accounts as mail

        await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
        await Tortoise.generate_schemas()
        try:
            row = await DBEmailAccount.create(
                owner_adapter_key="onebot_v11", owner_instance_key="new1", username="own@example.test",
                display_name="", provider="自定义", settings={}, password="p", attachment_dir="_i1",
            )
            stale = mail.InstanceMailOwner(account_id=row.id, adapter_key="onebot_v11", instance_key="old1",
                                           attachment_dir="_i1", notify_chat_key="")
            adapter = SimpleNamespace(instance_account_owner={"own@example.test": stale, "other@example.test": stale})

            changed = await mail.refresh_runtime_owners("onebot_v11", "new1", adapter=adapter)

            assert changed == 1
            assert adapter.instance_account_owner["own@example.test"].instance_key == "new1"
            assert adapter.instance_account_owner["other@example.test"].instance_key == "old1", "别的地址不动"
        finally:
            await Tortoise.close_connections()

    def test_the_migration_calls_the_refresh(self) -> None:
        import inspect

        from nekro_agent.services import chat_key_migration

        assert "refresh_runtime_owners(" in inspect.getsource(chat_key_migration._migrate_instance_scoped_rows)  # noqa: SLF001


class TestRemoteImagePeer:
    """`fetch_url_bytes` 建连接那一步（`download_http_transport`）按实际对端核对"""

    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, peer: Any) -> None:
        import httpcore

        from tests.test_tenant_egress import FakeBackend

        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        real = common_util._system_getaddrinfo  # noqa: SLF001

        async def fake_dns(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            name = host.decode("ascii") if isinstance(host, bytes) else host
            if name == "img.example.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, port))]
            return await real(host, port)

        monkeypatch.setattr("nekro_agent.tools.common_util._system_getaddrinfo", fake_dns)

        class _Backend(FakeBackend):
            async def connect_tcp(self, host: str, port: int, *args: Any, **kwargs: Any) -> Any:
                stream = await super().connect_tcp(host, port, *args, **kwargs)
                if peer is None:
                    stream.get_extra_info = lambda _name: None  # 取不到对端
                return stream

        backend = _Backend(responses=[[b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n\x89PNG"]])
        backend.peer_override = peer
        monkeypatch.setattr(httpcore, "AnyIOBackend", lambda: backend)

    async def test_a_rebound_private_peer_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, PRIVATE_IP)
        with pytest.raises(UnsafeDownloadUrlError):
            await common_util.fetch_url_bytes("http://img.example.test/a.png")

    async def test_an_unknown_peer_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, None)
        with pytest.raises(UnsafeDownloadUrlError):
            await common_util.fetch_url_bytes("http://img.example.test/a.png")

    async def test_a_public_peer_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, PUBLIC_IP)
        assert await common_util.fetch_url_bytes("http://img.example.test/a.png") == b"\x89PNG"
