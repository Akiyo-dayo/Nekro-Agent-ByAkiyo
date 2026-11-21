"""邮箱收发信都校验服务器证书：IMAP、SMTP over SSL、SMTP + STARTTLS

修复前 `imaplib.IMAP4_SSL` / `smtplib.SMTP_SSL` / `SMTP.starttls()` 都不传 TLS 上下文，用的是标准库的
`ssl._create_stdlib_context()`：不校验证书链、不核对主机名。链路上只要有人能冒充那台邮件服务器（同网段
ARP、被污染的 DNS、不可信的出口），授权码就交给它了。

这里在回环上起极简的假邮件服务器（`127.0.0.1` 上监听 0 号端口拿随机端口），证书是现生成的：一张测试 CA、
由它签发的一张服务器证书（SAN 只有 `DNS:localhost`）、一张自签的服务器证书。要证明的是四件事：

1. 自签证书不被信任时连接失败，假服务器**一行凭据都收不到**；
2. 信任测试 CA 时三种连接都能正常登录（不是「一律连不上」）；
3. 主机名不符（连 `127.0.0.1`、证书只有 `localhost`）时同样拿不到凭据；
4. 只有平台方「自定义」账户打开「不校验服务器证书」才放行自签证书，租户的账户与预设服务商一律校验。

其余两类用例不联网：一类把连接类换成记录参数的替身，逐个走遍 `_open_imap` / `_new_smtp` / `_open_smtp` /
`_send_mail_oauth2` 的每个分支，断言每一处都拿到了校验证书的上下文；另一类只看判据、状态邮件与
「测试连接」的文案。
"""

from __future__ import annotations

import datetime
import imaplib
import smtplib
import socket
import ssl
import threading
from contextlib import suppress
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from nekro_agent.adapters.email.clients import imap_smtp_password as client_module
from nekro_agent.adapters.email.clients.imap_smtp_oauth2 import ImapSmtpOAuth2Client
from nekro_agent.adapters.email.clients.imap_smtp_password import ImapSmtpPasswordClient
from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.adapters.email.routers import describe_test_failure

LOOPBACK = ".".join(("127", "0", "0", "1"))
SOCKET_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# 证书：测试 CA / 由它签发 / 自签
# ---------------------------------------------------------------------------


def _server_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def _issue_server_certificate(
    *,
    cert_path: Path,
    key_path: Path,
    not_before: datetime.datetime,
    not_after: datetime.datetime,
    ca_certificate: Optional[x509.Certificate] = None,
    ca_key: Optional[rsa.RSAPrivateKey] = None,
) -> SimpleNamespace:
    """签一张只对 `localhost` 有效的服务器证书，写进 PEM；返回证书文件、私钥文件与服务器端上下文

    给了 `ca_certificate` / `ca_key` 就是「由这个 CA 签发」，不给就是自签（subject 与 issuer 同名、
    用自己的私钥签）。
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    issuer = ca_certificate.subject if ca_certificate is not None else subject
    signer = ca_key if ca_key is not None else key
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(signer, hashes.SHA256())
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    return SimpleNamespace(cert=cert_path, key=key_path, context=_server_context(cert_path, key_path))


@pytest.fixture(scope="module")
def certs(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """测试 CA、它签发的服务器证书、一张自签的服务器证书（都是 PEM 文件，SAN 只有 `DNS:localhost`）"""
    root = tmp_path_factory.mktemp("mail-tls")
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(minutes=5)
    not_after = now + datetime.timedelta(days=1)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mail-test-ca")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_file = root / "ca.pem"
    ca_file.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))

    trusted = _issue_server_certificate(
        cert_path=root / "trusted.pem",
        key_path=root / "trusted.key",
        not_before=not_before,
        not_after=not_after,
        ca_certificate=ca_certificate,
        ca_key=ca_key,
    )
    self_signed = _issue_server_certificate(
        cert_path=root / "self-signed.pem",
        key_path=root / "self-signed.key",
        not_before=not_before,
        not_after=not_after,
    )

    return SimpleNamespace(ca_file=ca_file, trusted=trusted, self_signed=self_signed)


@pytest.fixture
def trust_test_ca(monkeypatch: pytest.MonkeyPatch, certs: SimpleNamespace) -> None:
    """让客户端信任测试 CA：monkeypatch 模块级的 `make_mail_tls_context`（用例要的就是这个入口）"""
    real = client_module.make_mail_tls_context

    def factory(verify: bool) -> ssl.SSLContext:
        context = real(verify)
        if verify:
            context.load_verify_locations(cafile=str(certs.ca_file))
        return context

    monkeypatch.setattr(client_module, "make_mail_tls_context", factory)


# ---------------------------------------------------------------------------
# 回环上的假邮件服务器
# ---------------------------------------------------------------------------


class FakeMailServer:
    """极简假邮件服务器，线程里跑；一个用例一个端口

    `mode` 三种：

    - `imap`：TLS 握手后发 `* OK ready`，之后逐行读；
    - `smtp_ssl`：先 TLS 再发 `220`（465 那种）；
    - `smtp_starttls`：明文 `220`，`EHLO` 之后收到 `STARTTLS` 再包上 TLS。

    收到的每一行都记进 `lines`：用例据此断言「假服务器一行 `LOGIN` / `AUTH` 都没收到」。

    `starttls` 只对 `smtp_starttls` 有用：`ok` 照常；`strip` 是 `EHLO` 里不宣告 `STARTTLS`（链路上有人把它
    剥掉的样子）；`refuse` 是宣告了、但收到 `STARTTLS` 回 `454`。
    """

    def __init__(self, mode: str, context: ssl.SSLContext, *, starttls: str = "ok") -> None:
        self.mode = mode
        self.context = context
        self.starttls = starttls
        self.lines: List[str] = []
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((LOOPBACK, 0))
        self._listener.listen(1)
        self._listener.settimeout(SOCKET_TIMEOUT)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._run, name=f"fake-mail-{mode}", daemon=True)

    def start(self) -> "FakeMailServer":
        self._thread.start()
        return self

    def close(self) -> None:
        with suppress(OSError):
            self._listener.close()
        self._thread.join(timeout=SOCKET_TIMEOUT * 2)

    def credentials_received(self) -> bool:
        for line in self.lines:
            tokens = {token.upper() for token in line.split()}
            if "LOGIN" in tokens or "AUTH" in tokens:
                return True
        return False

    def _record(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)

    def _run(self) -> None:
        conn: Optional[socket.socket] = None
        try:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            conn.settimeout(SOCKET_TIMEOUT)
            if self.mode == "smtp_starttls":
                self._serve_smtp(conn, secured=False)
            else:
                tls = self.context.wrap_socket(conn, server_side=True)
                if self.mode == "imap":
                    self._serve_imap(tls)
                else:
                    self._serve_smtp(tls, secured=True)
        except Exception:  # noqa: BLE001 - 客户端不信任证书时握手失败，是预期的一条路
            pass
        finally:
            if conn is not None:
                with suppress(OSError):
                    conn.close()

    def _serve_imap(self, sock: socket.socket) -> None:
        sock.sendall(b"* OK ready\r\n")
        reader = sock.makefile("rb")
        try:
            while True:
                raw = reader.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").strip()
                self._record(line)
                parts = line.split()
                tag = parts[0] if parts else "*"
                command = parts[1].upper() if len(parts) > 1 else ""
                if command == "CAPABILITY":
                    sock.sendall(b"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n" + tag.encode() + b" OK CAPABILITY done\r\n")
                elif command == "LOGIN":
                    sock.sendall(b"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n" + tag.encode() + b" OK logged in\r\n")
                elif command == "LOGOUT":
                    sock.sendall(b"* BYE logging out\r\n" + tag.encode() + b" OK LOGOUT done\r\n")
                    return
                else:
                    sock.sendall(tag.encode() + b" OK done\r\n")
        finally:
            with suppress(OSError):
                reader.close()

    def _serve_smtp(self, sock: socket.socket, *, secured: bool) -> None:
        sock.sendall(b"220 fake ESMTP ready\r\n")
        reader = sock.makefile("rb")
        try:
            while True:
                raw = reader.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", "replace").strip()
                self._record(line)
                word = line.split(" ", 1)[0].upper()
                if word in {"EHLO", "HELO"}:
                    if secured or self.starttls == "strip":
                        sock.sendall(b"250-fake\r\n250-SIZE 10240000\r\n250 AUTH PLAIN LOGIN\r\n")
                    else:
                        sock.sendall(b"250-fake\r\n250-SIZE 10240000\r\n250-STARTTLS\r\n250 AUTH PLAIN LOGIN\r\n")
                elif word == "STARTTLS":
                    if self.starttls == "refuse":
                        sock.sendall(b"454 4.7.0 TLS not available\r\n")
                        continue
                    sock.sendall(b"220 2.0.0 Ready to start TLS\r\n")
                    with suppress(OSError):
                        reader.close()
                    sock = self.context.wrap_socket(sock, server_side=True)
                    reader = sock.makefile("rb")
                    secured = True
                elif word == "AUTH":
                    sock.sendall(b"235 2.7.0 Authentication successful\r\n")
                elif word == "QUIT":
                    sock.sendall(b"221 bye\r\n")
                    return
                else:
                    sock.sendall(b"250 ok\r\n")
        finally:
            with suppress(OSError):
                reader.close()


class FakeConnectProxy:
    """回环上的极简 HTTP CONNECT 代理：只接一条连接，记下 CONNECT 的目标，回 200 之后双向转发字节

    邮件那一层的 TLS 是客户端在隧道里和假邮件服务器直接握的，代理看不到明文；用例据 `targets`
    确认流量确实经过了代理。
    """

    def __init__(self) -> None:
        self.targets: List[str] = []
        self._sockets: List[socket.socket] = []
        self._threads: List[threading.Thread] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((LOOPBACK, 0))
        self._listener.listen(1)
        self._listener.settimeout(SOCKET_TIMEOUT)
        self.port = int(self._listener.getsockname()[1])
        self.url = f"http://{LOOPBACK}:{self.port}"
        self._thread = threading.Thread(target=self._run, name="fake-connect-proxy", daemon=True)

    def start(self) -> "FakeConnectProxy":
        self._thread.start()
        return self

    def close(self) -> None:
        with suppress(OSError):
            self._listener.close()
        for sock in list(self._sockets):
            with suppress(OSError):
                sock.close()
        self._thread.join(timeout=SOCKET_TIMEOUT * 2)
        for thread in list(self._threads):
            thread.join(timeout=SOCKET_TIMEOUT * 2)

    def _run(self) -> None:
        try:
            client, _ = self._listener.accept()
        except OSError:
            return
        self._sockets.append(client)
        try:
            client.settimeout(SOCKET_TIMEOUT)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            target = head.split(b"\r\n", 1)[0].split()[1].decode("ascii")
            self.targets.append(target)
            host, port = target.rsplit(":", 1)
            upstream = socket.create_connection((host.strip("[]"), int(port)), timeout=SOCKET_TIMEOUT)
            self._sockets.append(upstream)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        except (OSError, ValueError, IndexError):
            return
        forward = threading.Thread(target=self._pump, args=(client, upstream), name="fake-connect-pump", daemon=True)
        self._threads.append(forward)
        forward.start()
        self._pump(upstream, client)

    @staticmethod
    def _pump(source: socket.socket, sink: socket.socket) -> None:
        try:
            while True:
                data = source.recv(4096)
                if not data:
                    break
                sink.sendall(data)
        except OSError:
            pass
        finally:
            with suppress(OSError):
                sink.shutdown(socket.SHUT_WR)


class _Servers:
    """本用例起过的假服务器与代理：用例结束时一定关掉、join 线程（不能挂住）"""

    def __init__(self) -> None:
        self.started: List[FakeMailServer] = []
        self.proxies: List[FakeConnectProxy] = []

    def start(self, mode: str, context: ssl.SSLContext, *, starttls: str = "ok") -> FakeMailServer:
        server = FakeMailServer(mode, context, starttls=starttls).start()
        self.started.append(server)
        return server

    def start_proxy(self) -> FakeConnectProxy:
        proxy = FakeConnectProxy().start()
        self.proxies.append(proxy)
        return proxy

    def close(self) -> None:
        for proxy in self.proxies:
            proxy.close()
        for server in self.started:
            server.close()


@pytest.fixture
def servers() -> Iterator[_Servers]:
    started = _Servers()
    try:
        yield started
    finally:
        started.close()


# ---------------------------------------------------------------------------
# 账户与断言帮手
# ---------------------------------------------------------------------------


def platform_account(**overrides: Any) -> EmailAccount:
    """平台方的「自定义」账户（不走代理）：默认连 `localhost`，端口由各用例改成假服务器的端口"""
    values: Dict[str, Any] = {
        "USERNAME": "bot@example.test",
        "PASSWORD": "pw-" + "t7q2",
        "EMAIL_ACCOUNT": "自定义",
        "CUSTOM_IMAP_HOST": "localhost",
        "CUSTOM_IMAP_PORT": 993,
        "CUSTOM_SMTP_HOST": "localhost",
        "CUSTOM_SMTP_PORT": 587,
        "CUSTOM_SMTP_SSL_PORT": 465,
        "CUSTOM_SMTP_USE_SSL": True,
        "ENABLED": True,
    }
    values.update(overrides)
    return EmailAccount(**values)


def cert_verification_error(error: BaseException) -> Optional[ssl.SSLCertVerificationError]:
    """异常链（自己、`__cause__`、`__context__`）里那个证书校验错误"""
    current: Optional[BaseException] = error
    seen = 0
    while current is not None and seen < 10:
        if isinstance(current, ssl.SSLCertVerificationError):
            return current
        current = current.__cause__ or current.__context__
        seen += 1
    return None


def assert_verifying(context: Any) -> None:
    assert isinstance(context, ssl.SSLContext), f"没传 TLS 上下文: {context!r}"
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def assert_not_verifying(context: Any) -> None:
    assert isinstance(context, ssl.SSLContext), f"没传 TLS 上下文: {context!r}"
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


# ---------------------------------------------------------------------------
# 1. 自签证书、不信任 → 不发凭据
# ---------------------------------------------------------------------------


class TestAnUntrustedCertificateGetsNoCredentials:
    async def test_imap(self, servers: _Servers, certs: SimpleNamespace) -> None:
        server = servers.start("imap", certs.self_signed.context)
        client = ImapSmtpPasswordClient(platform_account(CUSTOM_IMAP_PORT=server.port), 5)

        with pytest.raises(Exception) as excinfo:
            await client.connect()

        assert cert_verification_error(excinfo.value) is not None
        assert server.credentials_received() is False, "证书没过时授权码不能发出去"

    async def test_smtp_ssl(self, servers: _Servers, certs: SimpleNamespace) -> None:
        server = servers.start("smtp_ssl", certs.self_signed.context)
        client = ImapSmtpPasswordClient(
            platform_account(CUSTOM_SMTP_USE_SSL=True, CUSTOM_SMTP_SSL_PORT=server.port, CUSTOM_SMTP_PORT=server.port),
            5,
        )

        with pytest.raises(Exception) as excinfo:
            await client.check_smtp_login(timeout=5)

        assert cert_verification_error(excinfo.value) is not None
        assert server.credentials_received() is False

    async def test_smtp_starttls(self, servers: _Servers, certs: SimpleNamespace) -> None:
        server = servers.start("smtp_starttls", certs.self_signed.context)
        client = ImapSmtpPasswordClient(
            platform_account(CUSTOM_SMTP_USE_SSL=False, CUSTOM_SMTP_PORT=server.port, CUSTOM_SMTP_SSL_PORT=server.port),
            5,
        )

        with pytest.raises(Exception) as excinfo:
            await client.check_smtp_login(timeout=5)

        assert cert_verification_error(excinfo.value) is not None
        assert server.credentials_received() is False, "STARTTLS 之后才登录：证书没过就不能发凭据"


# ---------------------------------------------------------------------------
# 2. 信任测试 CA → 正常登录（证明不是「一律连不上」）
# ---------------------------------------------------------------------------


class TestATrustedCertificateLogsIn:
    @pytest.mark.usefixtures("trust_test_ca")
    async def test_imap(self, servers: _Servers, certs: SimpleNamespace) -> None:
        server = servers.start("imap", certs.trusted.context)
        client = ImapSmtpPasswordClient(platform_account(CUSTOM_IMAP_PORT=server.port), 5)

        await client.connect()
        assert server.credentials_received() is True
        await client.close()

    @pytest.mark.usefixtures("trust_test_ca")
    async def test_smtp_ssl(self, servers: _Servers, certs: SimpleNamespace) -> None:
        server = servers.start("smtp_ssl", certs.trusted.context)
        client = ImapSmtpPasswordClient(
            platform_account(CUSTOM_SMTP_USE_SSL=True, CUSTOM_SMTP_SSL_PORT=server.port, CUSTOM_SMTP_PORT=server.port),
            5,
        )

        assert await client.check_smtp_login(timeout=5) == f"localhost:{server.port}"
        assert server.credentials_received() is True

    @pytest.mark.usefixtures("trust_test_ca")
    async def test_smtp_starttls(self, servers: _Servers, certs: SimpleNamespace) -> None:
        server = servers.start("smtp_starttls", certs.trusted.context)
        client = ImapSmtpPasswordClient(
            platform_account(CUSTOM_SMTP_USE_SSL=False, CUSTOM_SMTP_PORT=server.port, CUSTOM_SMTP_SSL_PORT=server.port),
            5,
        )

        assert await client.check_smtp_login(timeout=5) == f"localhost:{server.port}"
        assert server.credentials_received() is True


# ---------------------------------------------------------------------------
# 3. 主机名不符 → 不发凭据
# ---------------------------------------------------------------------------


class TestAHostnameMismatchGetsNoCredentials:
    @pytest.mark.usefixtures("trust_test_ca")
    async def test_imap(self, servers: _Servers, certs: SimpleNamespace) -> None:
        """证书只有 `DNS:localhost`，账户却填 `127.0.0.1`：证书链是好的，主机名对不上"""
        server = servers.start("imap", certs.trusted.context)
        client = ImapSmtpPasswordClient(platform_account(CUSTOM_IMAP_HOST=LOOPBACK, CUSTOM_IMAP_PORT=server.port), 5)

        with pytest.raises(Exception) as excinfo:
            await client.connect()

        assert cert_verification_error(excinfo.value) is not None
        assert server.credentials_received() is False

    @pytest.mark.usefixtures("trust_test_ca")
    async def test_smtp_starttls(self, servers: _Servers, certs: SimpleNamespace) -> None:
        server = servers.start("smtp_starttls", certs.trusted.context)
        client = ImapSmtpPasswordClient(
            platform_account(
                CUSTOM_SMTP_HOST=LOOPBACK,
                CUSTOM_SMTP_USE_SSL=False,
                CUSTOM_SMTP_PORT=server.port,
                CUSTOM_SMTP_SSL_PORT=server.port,
            ),
            5,
        )

        with pytest.raises(Exception) as excinfo:
            await client.check_smtp_login(timeout=5)

        assert cert_verification_error(excinfo.value) is not None
        assert server.credentials_received() is False


# ---------------------------------------------------------------------------
# 4. 开关打开 → 自签也能连（只给平台方内网自签证书的服务器用）
# ---------------------------------------------------------------------------


class TestTheSkipSwitchLetsASelfSignedServerIn:
    async def test_imap_with_the_switch_on(self, servers: _Servers, certs: SimpleNamespace) -> None:
        server = servers.start("imap", certs.self_signed.context)
        client = ImapSmtpPasswordClient(
            platform_account(CUSTOM_IMAP_PORT=server.port, SKIP_TLS_VERIFY=True),
            5,
        )

        await client.connect()

        assert server.credentials_received() is True
        await client.close()


# ---------------------------------------------------------------------------
# 5. 判据：谁一律校验、谁能不校验
# ---------------------------------------------------------------------------


class TestTheVerifyRule:
    def test_a_preset_provider_is_always_verified(self) -> None:
        account = EmailAccount(EMAIL_ACCOUNT="QQ邮箱", SKIP_TLS_VERIFY=True)

        assert account.tls_verify_required("imap.qq.example") is True

    def test_an_instance_owned_account_is_always_verified(self) -> None:
        account = platform_account(SKIP_TLS_VERIFY=True)
        account._instance_owned = True

        assert account.tls_verify_required("localhost") is True

    def test_a_tenant_supplied_host_is_always_verified(self) -> None:
        account = platform_account(SKIP_TLS_VERIFY=True, TENANT_SUPPLIED_HOSTS="imap.example.test,smtp.example.test")

        assert account.tls_verify_required("imap.example.test") is True
        assert account.tls_verify_required("smtp.example.test") is True
        assert account.tls_verify_required("localhost") is False, "没被标记的主机仍是平台方自己选的"

    def test_a_tenant_endpoint_is_always_verified(self) -> None:
        account = platform_account(SKIP_TLS_VERIFY=True)
        account._tenant_endpoint = True

        assert account.tls_verify_required("localhost") is True

    def test_only_the_platforms_own_custom_server_follows_the_switch(self) -> None:
        assert platform_account(SKIP_TLS_VERIFY=True).tls_verify_required("localhost") is False
        assert platform_account(SKIP_TLS_VERIFY=False).tls_verify_required("localhost") is True


# ---------------------------------------------------------------------------
# 6. 实例自有账户的运行时对象
# ---------------------------------------------------------------------------


class TestTheRuntimeAccountOfAnInstance:
    def test_it_is_always_verified_and_the_switch_never_reaches_the_dump(self) -> None:
        from nekro_agent.models.db_email_account import DBEmailAccount
        from nekro_agent.services import email_accounts as mail

        row = DBEmailAccount(
            id=1,
            owner_adapter_key="onebot_v11",
            owner_instance_key="inst1",
            username="own@example.test",
            provider="自定义",
            settings={"imap_host": "imap.example.test", "smtp_host": "smtp.example.test", "skip_tls_verify": True},
            password="pw-" + "t7q2",
            enabled=True,
            receive_enabled=True,
            send_enabled=True,
            attachment_dir="_i1",
        )

        account = mail.to_runtime_account(row)

        assert account._instance_owned is True
        assert account.tls_verify_required("imap.example.test") is True
        assert account.tls_verify_required("localhost") is True
        assert account.model_dump()["SKIP_TLS_VERIFY"] is False


# ---------------------------------------------------------------------------
# 7. 每个连接点都传了校验上下文
# ---------------------------------------------------------------------------


class _RecordingConn:
    """连接类的替身（按名字生成子类）：只记参数，不真连"""

    store: Dict[str, List[Any]] = {}

    def __init__(
        self,
        host: str = "",
        port: int = 0,
        *args: Any,
        timeout: Any = None,
        connect_host: str = "",
        ssl_context: Optional[ssl.SSLContext] = None,
        context: Optional[ssl.SSLContext] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.args = args
        self.timeout = timeout
        self.connect_host = connect_host
        self.ssl_context = ssl_context
        self.context = context
        self.sock = None
        self.calls: List[str] = []
        self.store.setdefault(type(self).__name__, []).append(self)

    def login(self, *_args: Any, **_kwargs: Any) -> tuple:
        self.calls.append("login")
        return ("OK", [b""])

    def authenticate(self, *_args: Any, **_kwargs: Any) -> tuple:
        self.calls.append("authenticate")
        return ("OK", [b""])

    def ehlo(self, *_args: Any, **_kwargs: Any) -> tuple:
        self.calls.append("ehlo")
        return (250, b"")

    def auth(self, *_args: Any, **_kwargs: Any) -> tuple:
        self.calls.append("auth")
        return (235, b"")

    def starttls(self, *_args: Any, **kwargs: Any) -> None:
        self.calls.append("starttls")
        self.store.setdefault("starttls", []).append(kwargs.get("context"))

    def send_message(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls.append("send")

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def close(self) -> None:
        self.calls.append("close")

    def logout(self) -> None:
        self.calls.append("logout")

    def __enter__(self) -> "_RecordingConn":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.calls.append("quit")


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    store: Dict[str, List[Any]] = {}
    _RecordingConn.store = store

    def make(kind: str) -> type:
        return type(kind, (_RecordingConn,), {})

    for name in (
        "ProxiedIMAP4SSL",
        "PinnedIMAP4SSL",
        "ProxiedSMTP",
        "ProxiedSMTPSSL",
        "PinnedSMTP",
        "PinnedSMTPSSL",
    ):
        monkeypatch.setattr(client_module, name, make(name))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", make("IMAP4_SSL"))
    monkeypatch.setattr(smtplib, "SMTP", make("SMTP"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", make("SMTP_SSL"))
    return store


class TestEveryConnectionPointGetsAVerifyingContext:
    @staticmethod
    def _context_of(conn: _RecordingConn) -> Any:
        return conn.ssl_context if conn.ssl_context is not None else conn.context

    async def test_all_the_branches(self, recorded: Dict[str, List[Any]]) -> None:
        pinned = (LOOPBACK,)
        client = ImapSmtpPasswordClient(platform_account(), 5)
        proxied = ImapSmtpPasswordClient(platform_account(), 5, proxy_url="http://127.0.0.1:8080")

        # IMAP：直连、校验过的地址 + 直连、平台方经代理、校验过的地址 + 代理
        client._open_imap("imap.example.test", 993, None)
        client._open_imap("imap.example.test", 993, pinned)
        proxied._open_imap("imap.example.test", 993, None)
        proxied._open_imap("imap.example.test", 993, pinned)

        # SMTP over SSL：同上四条
        client._new_smtp("smtp.example.test", 465, True, 5, None)
        client._new_smtp("smtp.example.test", 465, True, 5, pinned)
        proxied._new_smtp("smtp.example.test", 465, True, 5, None)
        proxied._new_smtp("smtp.example.test", 465, True, 5, pinned)

        # SMTP 明文：构造时不需要上下文（STARTTLS 那一步才要）
        plain = [
            client._new_smtp("smtp.example.test", 587, False, 5, None),
            client._new_smtp("smtp.example.test", 587, False, 5, pinned),
            proxied._new_smtp("smtp.example.test", 587, False, 5, None),
            proxied._new_smtp("smtp.example.test", 587, False, 5, pinned),
        ]
        for conn in plain:
            assert self._context_of(conn) is None

        # _open_smtp：明文四条各 STARTTLS 一次，SSL 两条
        client._open_smtp("smtp.example.test", 587, False, 5, None)
        client._open_smtp("smtp.example.test", 587, False, 5, pinned)
        proxied._open_smtp("smtp.example.test", 587, False, 5, None)
        proxied._open_smtp("smtp.example.test", 587, False, 5, pinned)
        client._open_smtp("smtp.example.test", 465, True, 5, None)
        proxied._open_smtp("smtp.example.test", 465, True, 5, pinned)

        # OAuth2 客户端：SSL 与 STARTTLS 两条
        oauth = ImapSmtpOAuth2Client(platform_account(AUTH_TYPE="oauth2"), 5)
        await oauth._send_mail_oauth2(MIMEMultipart(), "smtp.example.test", 465, True, "token")
        await oauth._send_mail_oauth2(MIMEMultipart(), "smtp.example.test", 587, False, "token")

        for kind in ("IMAP4_SSL", "PinnedIMAP4SSL", "ProxiedIMAP4SSL", "SMTP_SSL", "PinnedSMTPSSL", "ProxiedSMTPSSL"):
            assert recorded.get(kind), f"{kind} 这个分支没走到"
            for conn in recorded[kind]:
                assert_verifying(self._context_of(conn))
        for kind in ("SMTP", "PinnedSMTP", "ProxiedSMTP"):
            assert recorded.get(kind), f"{kind} 这个分支没走到"

        starttls_contexts = recorded.get("starttls") or []
        assert len(starttls_contexts) == 5, "四条 `_open_smtp` 明文分支 + OAuth2 那条 STARTTLS，每条都要带上下文"
        for context in starttls_contexts:
            assert_verifying(context)

    async def test_the_switch_on_gives_an_unverified_context(self, recorded: Dict[str, List[Any]]) -> None:
        client = ImapSmtpPasswordClient(platform_account(SKIP_TLS_VERIFY=True), 5)

        client._open_imap("localhost", 993, None)
        client._new_smtp("localhost", 465, True, 5, None)
        client._open_smtp("localhost", 587, False, 5, None)

        assert_not_verifying(self._context_of(recorded["IMAP4_SSL"][0]))
        assert_not_verifying(self._context_of(recorded["SMTP_SSL"][0]))
        assert_not_verifying(recorded["starttls"][0])

    async def test_a_tenant_host_keeps_verifying_even_with_the_switch_on(self, recorded: Dict[str, List[Any]]) -> None:
        account = platform_account(SKIP_TLS_VERIFY=True, TENANT_SUPPLIED_HOSTS="imap.example.test")
        client = ImapSmtpPasswordClient(account, 5)

        # `addresses` 给了就只有「只连校验过的地址」那条分支；对端核对在 `getpeername` 拿不到地址时
        # 照样会拒（那条逻辑不改），这里只看上下文
        with suppress(Exception):
            client._open_imap("imap.example.test", 993, (LOOPBACK,))

        assert_verifying(self._context_of(recorded["PinnedIMAP4SSL"][0]))


# ---------------------------------------------------------------------------
# 8. 状态邮件
# ---------------------------------------------------------------------------


class TestTheStatusMail:
    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, accounts: List[EmailAccount], sender: str) -> List[Dict[str, Any]]:
        from nekro_agent.services.mail import mail_service

        calls: List[Dict[str, Any]] = []

        async def fake_send(_message: Any, **kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(mail_service, "send", fake_send)
        adapter_config = SimpleNamespace(
            STATUS_MAIL_ENABLED=True,
            RECEIVE_ACCOUNTS=accounts,
            STATUS_MAIL_SENDER_ACCOUNT=sender,
            STATUS_MAIL_TARGETS=[SimpleNamespace(EMAIL="ops@example.test")],
        )
        monkeypatch.setattr(mail_service, "_load_email_adapter_config", lambda: adapter_config)
        return calls

    def test_the_core_config_branch_verifies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.mail import mail_service

        self._install(monkeypatch, [], "")

        assert mail_service._get_runtime_mail_settings()["validate_certs"] is True

    def test_a_platform_custom_account_with_the_switch_on_does_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.mail import mail_service

        account = platform_account(SEND_ENABLED=True, SKIP_TLS_VERIFY=True)
        self._install(monkeypatch, [account], account.USERNAME)

        assert mail_service._get_runtime_mail_settings()["validate_certs"] is False

    async def test_send_email_passes_the_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.mail import mail_service

        loose = platform_account(SEND_ENABLED=True, SKIP_TLS_VERIFY=True)
        calls = self._install(monkeypatch, [loose], loose.USERNAME)

        await mail_service.send_email(EmailMessage())

        assert [call["validate_certs"] for call in calls] == [False]

    async def test_a_tenant_account_keeps_verifying(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.mail import mail_service

        # 主机是租户填的：状态邮件本来就不发（见 `send_email` 那一段），所以这里只看判据
        account = platform_account(SEND_ENABLED=True, SKIP_TLS_VERIFY=True, TENANT_SUPPLIED_HOSTS="localhost")
        self._install(monkeypatch, [account], account.USERNAME)

        assert mail_service._get_runtime_mail_settings()["validate_certs"] is True


class TestTheStatusMailOnTheOldCoreSettings:
    """没选发件邮箱时走已弃用的核心配置 `MAIL_*`：465 连上就握 TLS；其余端口没开 STARTTLS 又要登录就不发"""

    @staticmethod
    def _install(
        monkeypatch: pytest.MonkeyPatch,
        *,
        port: int,
        starttls: bool,
        username: str = "ops@example.test",
        password: str = "pw-" + "c4m8",
    ) -> List[Dict[str, Any]]:
        from nekro_agent.services.mail import mail_service

        calls: List[Dict[str, Any]] = []

        async def fake_send(_message: Any, **kwargs: Any) -> None:
            calls.append(kwargs)

        monkeypatch.setattr(mail_service, "send", fake_send)
        monkeypatch.setattr(
            mail_service,
            "_load_email_adapter_config",
            lambda: SimpleNamespace(
                STATUS_MAIL_ENABLED=False,
                RECEIVE_ACCOUNTS=[],
                STATUS_MAIL_SENDER_ACCOUNT="",
                STATUS_MAIL_TARGETS=[],
            ),
        )
        monkeypatch.setattr(
            mail_service,
            "config",
            SimpleNamespace(
                MAIL_ENABLED=True,
                MAIL_USERNAME=username,
                MAIL_PASSWORD=password,
                MAIL_TARGET=["ops@example.test"],
                MAIL_HOSTNAME="smtp.example.test",
                MAIL_PORT=port,
                MAIL_STARTTLS=starttls,
            ),
        )
        return calls

    def test_port_465_is_implicit_tls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.mail import mail_service

        self._install(monkeypatch, port=465, starttls=True)
        settings = mail_service._get_runtime_mail_settings()

        assert (settings["use_tls"], settings["starttls"], settings["plaintext_login"]) == (True, False, False)

    def test_starttls_on_587(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.mail import mail_service

        self._install(monkeypatch, port=587, starttls=True)
        settings = mail_service._get_runtime_mail_settings()

        assert (settings["use_tls"], settings["starttls"], settings["plaintext_login"]) == (False, True, False)

    async def test_a_plaintext_login_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.mail import mail_service

        calls = self._install(monkeypatch, port=587, starttls=False)

        assert mail_service._get_runtime_mail_settings()["plaintext_login"] is True
        await mail_service.send_email(EmailMessage())
        assert calls == [], "账号和授权码不能明文发出去"

    async def test_a_plaintext_relay_without_credentials_still_sends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不登录的明文中继（本机邮件服务）没有秘密可漏，照常发"""
        from nekro_agent.services.mail import mail_service

        calls = self._install(monkeypatch, port=25, starttls=False, username="", password="")

        await mail_service.send_email(EmailMessage())
        assert [(call["use_tls"], call["start_tls"]) for call in calls] == [(False, False)]


class TestAnHttpsProxyIsRefusedUpFront:
    def test_https_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """https:// 代理握不通邮件 TLS（外层 TLS 会被标准库拆掉）：一开始就拒绝，不去连代理"""

        def no_connection(*_args: Any, **_kwargs: Any) -> socket.socket:
            raise AssertionError("不该去连代理")

        monkeypatch.setattr(client_module.socket, "create_connection", no_connection)

        with pytest.raises(RuntimeError, match="http://"):
            client_module._connect_via_http_proxy("localhost", 993, "https://proxy.example.test:443", 5)  # noqa: SLF001


# ---------------------------------------------------------------------------
# 9. 「测试连接」的文案
# ---------------------------------------------------------------------------


class TestTheConnectionTestWording:
    def test_a_certificate_error_is_explained(self) -> None:
        error = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")

        message = describe_test_failure("连接服务器", error, platform_account(), "localhost")

        assert "证书校验没通过" in message
        assert "不校验服务器证书" in message
        assert "self-signed certificate" in message

    @pytest.mark.parametrize(
        "account",
        [
            pytest.param(platform_account(EMAIL_ACCOUNT="QQ邮箱"), id="preset-provider"),
            pytest.param(platform_account(TENANT_SUPPLIED_HOSTS="localhost"), id="tenant-supplied-host"),
            pytest.param(None, id="unknown-account"),
        ],
    )
    def test_the_switch_is_only_suggested_where_it_works(self, account: Optional[EmailAccount]) -> None:
        """预设服务商、租户填的主机，开关开了也照样校验：这时不提开关，只说去查证书"""
        error = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")

        message = describe_test_failure("连接服务器", error, account, "localhost")

        assert "证书校验没通过" in message
        assert "不校验服务器证书" not in message
        assert "不能关闭证书校验" in message

    def test_an_instance_owned_account_gets_no_switch_hint(self) -> None:
        account = platform_account()
        account._instance_owned = True  # noqa: SLF001
        error = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")

        assert "不校验服务器证书" not in describe_test_failure("连接服务器", error, account, "localhost")

    def test_the_host_comes_from_the_imap_endpoint(self) -> None:
        from nekro_agent.adapters.email.routers import _test_failure_host

        imap_client = ImapSmtpPasswordClient(platform_account(CUSTOM_IMAP_HOST="mail.example.test"), 5)

        assert _test_failure_host(imap_client) == "mail.example.test"
        assert _test_failure_host(SimpleNamespace()) == "", "Gmail API / Graph 客户端没有 IMAP 主机"

    def test_the_reason_uses_the_verify_message(self) -> None:
        error = ssl.SSLCertVerificationError(1, "generic")
        error.verify_message = "Hostname mismatch, certificate is not valid for 'localhost'."

        message = describe_test_failure("连接服务器", error)

        assert "Hostname mismatch" in message
        assert "generic" not in message

    def test_a_wrapped_certificate_error_is_found_through_the_cause(self) -> None:
        cert_error = ssl.SSLCertVerificationError(1, "certificate verify failed: hostname mismatch")
        outer = RuntimeError("connection failed")
        outer.__cause__ = cert_error

        message = describe_test_failure("连接服务器", outer)

        assert "证书校验没通过" in message
        assert "hostname mismatch" in message

    def test_a_context_certificate_error_is_found_too(self) -> None:
        cert_error = ssl.SSLCertVerificationError(1, "certificate verify failed: expired")
        outer = RuntimeError("boom")
        outer.__context__ = cert_error

        assert "证书校验没通过" in describe_test_failure("连接服务器", outer)

    def test_an_ordinary_error_reads_as_before(self) -> None:
        error = RuntimeError("boom")

        assert describe_test_failure("连接服务器", error) == "连接服务器失败: boom"

    def test_a_deep_chain_stops_at_five_levels(self) -> None:
        cert_error = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")
        error: BaseException = cert_error
        for _ in range(6):
            wrapped = RuntimeError("layer")
            wrapped.__cause__ = error
            error = wrapped

        assert "证书校验没通过" not in describe_test_failure("连接服务器", error)
        assert describe_test_failure("连接服务器", error) == "连接服务器失败: layer"


class TestTheWrapperClassesHandTheContextOn:
    """`Pinned*` / `Proxied*` 自己调标准库的构造函数：上下文要原样交给它

    上面「每个连接点」那组把这几个类整个换成了替身，只能证明调用方传了上下文；这里用真的类，
    只把建连那一步换掉，证明类把上下文交给了标准库。少交一处，标准库就退回不校验证书的那一个。
    """

    def test_imap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(imaplib.IMAP4, "open", lambda self, host="", port=0, timeout=None: None)
        monkeypatch.setattr(imaplib.IMAP4, "_connect", lambda self: None)
        context = ssl.create_default_context()

        pinned = client_module.PinnedIMAP4SSL("localhost", 993, ("127.0.0.1",), 5, ssl_context=context)
        proxied = client_module.ProxiedIMAP4SSL("localhost", 993, "http://127.0.0.1:1", 5, ssl_context=context)

        assert pinned.ssl_context is context
        assert proxied.ssl_context is context

    def test_smtp_ssl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(smtplib.SMTP, "connect", lambda self, host="localhost", port=0, source_address=None: (220, b"ok"))
        context = ssl.create_default_context()

        pinned = client_module.PinnedSMTPSSL("localhost", 465, ("127.0.0.1",), 5, context=context)
        proxied = client_module.ProxiedSMTPSSL("localhost", 465, "http://127.0.0.1:1", 5, context=context)

        assert pinned.context is context
        assert proxied.context is context


# ---------------------------------------------------------------------------
# 6. 按校验过的地址直连（Pinned）、经代理、代理加校验过的地址：真实握手
# ---------------------------------------------------------------------------

# 三条路：`addresses` 给了就走「只连校验过的地址」（租户填的主机平时走这条），`via_proxy` 为真就经 CONNECT 代理
CONNECTION_PATHS = [
    pytest.param(False, (LOOPBACK,), id="pinned"),
    pytest.param(True, None, id="proxy"),
    pytest.param(True, (LOOPBACK,), id="proxy-pinned"),
]
PROTOCOLS = ["imap", "smtp_ssl", "smtp_starttls"]


def _client_for(
    servers: _Servers,
    *,
    via_proxy: bool,
    host: str,
    port: int,
) -> tuple[ImapSmtpPasswordClient, Optional[FakeConnectProxy]]:
    proxy = servers.start_proxy() if via_proxy else None
    account = platform_account(CUSTOM_IMAP_HOST=host, CUSTOM_IMAP_PORT=port, CUSTOM_SMTP_HOST=host)
    client = ImapSmtpPasswordClient(account, 5, proxy_url=proxy.url if proxy is not None else "")
    return client, proxy


def _log_in(client: ImapSmtpPasswordClient, protocol: str, host: str, port: int, addresses: Optional[tuple[str, ...]]) -> None:
    """走客户端自己的建连与登录（IMAP：`_connect_sync`；SMTP：`_open_smtp`），登上就退出"""
    if protocol == "imap":
        conn = client._connect_sync(addresses)  # noqa: SLF001
        client._close_sync(conn)  # noqa: SLF001
        return
    server = client._open_smtp(host, port, protocol == "smtp_ssl", 5, addresses)  # noqa: SLF001
    with suppress(Exception):
        server.quit()


class TestPinnedAndProxiedPathsHandshakeForReal:
    """`Pinned*` 连的是 IP、证书按主机名核对；`Proxied*` 在 CONNECT 隧道里握手

    上面「每个连接点」那组用替身只证明调用方把上下文传进去了；这组用真的连接类和真的握手证明：
    这几个类要是丢了 `ssl_context=` / `context=`，标准库会悄悄退回不校验证书的上下文，自签证书就连得上、
    授权码就发出去了。
    """

    @pytest.mark.parametrize("protocol", PROTOCOLS)
    @pytest.mark.parametrize(("via_proxy", "addresses"), CONNECTION_PATHS)
    def test_a_self_signed_certificate_gets_no_credentials(
        self,
        servers: _Servers,
        certs: SimpleNamespace,
        protocol: str,
        via_proxy: bool,
        addresses: Optional[tuple[str, ...]],
    ) -> None:
        server = servers.start(protocol, certs.self_signed.context)
        client, _proxy = _client_for(servers, via_proxy=via_proxy, host="localhost", port=server.port)

        with pytest.raises(Exception) as excinfo:
            _log_in(client, protocol, "localhost", server.port, addresses)

        assert cert_verification_error(excinfo.value) is not None
        assert server.credentials_received() is False

    @pytest.mark.usefixtures("trust_test_ca")
    @pytest.mark.parametrize("protocol", PROTOCOLS)
    @pytest.mark.parametrize(("via_proxy", "addresses"), CONNECTION_PATHS)
    def test_a_trusted_certificate_logs_in(
        self,
        servers: _Servers,
        certs: SimpleNamespace,
        protocol: str,
        via_proxy: bool,
        addresses: Optional[tuple[str, ...]],
    ) -> None:
        server = servers.start(protocol, certs.trusted.context)
        client, proxy = _client_for(servers, via_proxy=via_proxy, host="localhost", port=server.port)

        _log_in(client, protocol, "localhost", server.port, addresses)

        assert server.credentials_received() is True
        if proxy is not None:
            expected = addresses[0] if addresses else "localhost"
            assert proxy.targets == [f"{expected}:{server.port}"], "流量要经过代理"

    @pytest.mark.usefixtures("trust_test_ca")
    @pytest.mark.parametrize("protocol", PROTOCOLS)
    @pytest.mark.parametrize(("via_proxy", "addresses"), CONNECTION_PATHS)
    def test_a_hostname_mismatch_gets_no_credentials(
        self,
        servers: _Servers,
        certs: SimpleNamespace,
        protocol: str,
        via_proxy: bool,
        addresses: Optional[tuple[str, ...]],
    ) -> None:
        """证书只有 `DNS:localhost`；连的地址没错，核对的主机名不对

        按地址连时主机名随便填（`mail.example.test`，不需要解析得到）；只经代理时代理要能连到它，
        所以填回环地址本身。
        """
        server = servers.start(protocol, certs.trusted.context)
        host = "mail.example.test" if addresses else LOOPBACK
        client, _proxy = _client_for(servers, via_proxy=via_proxy, host=host, port=server.port)

        with pytest.raises(Exception) as excinfo:
            _log_in(client, protocol, host, server.port, addresses)

        assert cert_verification_error(excinfo.value) is not None
        assert server.credentials_received() is False

    @pytest.mark.usefixtures("trust_test_ca")
    @pytest.mark.parametrize("starttls", ["strip", "refuse"])
    @pytest.mark.parametrize("addresses", [None, (LOOPBACK,)], ids=["direct", "pinned"])
    def test_a_server_that_will_not_start_tls_gets_no_credentials(
        self,
        servers: _Servers,
        certs: SimpleNamespace,
        starttls: str,
        addresses: Optional[tuple[str, ...]],
    ) -> None:
        """`EHLO` 里没有 `STARTTLS`（被剥掉），或者 `STARTTLS` 回 `454`：不能退回明文登录"""
        server = servers.start("smtp_starttls", certs.trusted.context, starttls=starttls)
        client, _proxy = _client_for(servers, via_proxy=False, host="localhost", port=server.port)

        with pytest.raises(smtplib.SMTPException):
            _log_in(client, "smtp_starttls", "localhost", server.port, addresses)

        assert server.credentials_received() is False
        assert not any(line.upper().startswith(("AUTH", "MAIL")) for line in server.lines)
