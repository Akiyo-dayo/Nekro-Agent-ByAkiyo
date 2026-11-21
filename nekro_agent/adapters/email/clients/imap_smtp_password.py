import asyncio
import base64
import email
import imaplib
import re
import smtplib
import socket
import ssl
from contextlib import suppress
from email.message import Message
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Sequence

import aiofiles

from nekro_agent.adapters.email.base import EMAIL_PROVIDER_CONFIGS
from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.core.logger import get_sub_logger

# 「连校验过的地址」与「让代理 CONNECT 校验过的地址」是租户出站的公共做法（`tools/tenant_egress.py`），
# 这里沿用原来的名字，调用处与用例都不用改
from nekro_agent.tools.tenant_egress import connect_pinned as _connect_pinned
from nekro_agent.tools.tenant_egress import connect_via_http_proxy as _connect_via_http_proxy

from .base import EmailRawContent

logger = get_sub_logger("email_client")

_SEQUENCE_NUMBER = re.compile(rb"[0-9]{1,20}")


def is_imap_sequence_number(value: bytes | str) -> bool:
    """IMAP 消息序号 / UID：只能是纯 ASCII 数字（最多 20 位）"""
    raw = value.encode("ascii", errors="replace") if isinstance(value, str) else bytes(value)
    return bool(_SEQUENCE_NUMBER.fullmatch(raw))


def make_mail_tls_context(verify: bool) -> ssl.SSLContext:
    """收发信用的 TLS 上下文

    `verify=True`：`ssl.create_default_context()`，校验证书链、核对主机名。
    `verify=False`：只给平台方内网自签证书的邮件服务器用（`EmailAccount.tls_verify_required`）。
    """
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class MailPeerRejectedError(ConnectionError):
    """连上之后核对对端没过

    对外按「连不上」处理（`ConnectionError`），不与「地址不能用」区分、不带地址：两者分得开的话，
    租户就能拿「测试连接」判断 NA 是不是连上了内网的某个端口。
    """


class ProxiedIMAP4SSL(imaplib.IMAP4_SSL):
    """经 HTTP CONNECT 代理连 IMAP；`connect_host` 非空时让代理连这个（校验过的）地址，SNI 仍用主机名"""

    def __init__(
        self,
        host: str,
        port: int,
        proxy_url: str,
        timeout: int,
        connect_host: str = "",
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.proxy_url = proxy_url
        self.connect_host = connect_host
        super().__init__(host=host, port=port, timeout=timeout, ssl_context=ssl_context)

    def _create_socket(self, timeout):
        sock = _connect_via_http_proxy(self.connect_host or self.host, self.port, self.proxy_url, timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


class ProxiedSMTP(smtplib.SMTP):
    def __init__(self, host: str, port: int, proxy_url: str, timeout: int, connect_host: str = ""):
        self.proxy_url = proxy_url
        self.connect_host = connect_host
        super().__init__(host=host, port=port, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        return _connect_via_http_proxy(self.connect_host or host, port, self.proxy_url, timeout)


class ProxiedSMTPSSL(smtplib.SMTP_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        proxy_url: str,
        timeout: int,
        connect_host: str = "",
        context: ssl.SSLContext | None = None,
    ):
        self.proxy_url = proxy_url
        self.connect_host = connect_host
        super().__init__(host=host, port=port, timeout=timeout, context=context)

    def _get_socket(self, host, port, timeout):
        sock = _connect_via_http_proxy(self.connect_host or host, port, self.proxy_url, timeout)
        return self.context.wrap_socket(sock, server_hostname=host)


class PinnedIMAP4SSL(imaplib.IMAP4_SSL):
    """直接连校验过的地址（不让 `imaplib` 再解析一遍主机名）；SNI 与证书主机名仍用主机名"""

    def __init__(
        self,
        host: str,
        port: int,
        addresses: Sequence[str],
        timeout: int,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.pinned_addresses = tuple(addresses)
        super().__init__(host=host, port=port, timeout=timeout, ssl_context=ssl_context)

    def _create_socket(self, timeout):
        sock = _connect_pinned(self.pinned_addresses, self.port, timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


class PinnedSMTP(smtplib.SMTP):
    """直接连校验过的地址；`starttls()` 的 SNI 取 `self._host`，那仍是主机名"""

    def __init__(self, host: str, port: int, addresses: Sequence[str], timeout: int):
        self.pinned_addresses = tuple(addresses)
        super().__init__(host=host, port=port, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        return _connect_pinned(self.pinned_addresses, port, timeout)


class PinnedSMTPSSL(smtplib.SMTP_SSL):
    def __init__(
        self,
        host: str,
        port: int,
        addresses: Sequence[str],
        timeout: int,
        context: ssl.SSLContext | None = None,
    ):
        self.pinned_addresses = tuple(addresses)
        super().__init__(host=host, port=port, timeout=timeout, context=context)

    def _get_socket(self, host, port, timeout):
        sock = _connect_pinned(self.pinned_addresses, port, timeout)
        return self.context.wrap_socket(sock, server_hostname=host)


class ImapSmtpPasswordClient:
    def __init__(self, account: EmailAccount, imap_timeout: int, proxy_url: str = ""):
        self.account = account
        self.account_username = account.USERNAME
        self.imap_timeout = imap_timeout
        self.proxy_url = proxy_url
        self.conn: imaplib.IMAP4_SSL | None = None

    async def connect(self) -> None:
        imap_host, imap_port = self._imap_endpoint()
        addresses = await self._checked_addresses(imap_host, imap_port)
        self.conn = await asyncio.to_thread(self._connect_sync, addresses)

    async def _checked_addresses(self, host: str, port: int) -> tuple[str, ...] | None:
        """租户填的服务器：解析一次、校验全部结果（拒内网，判据与下载同一份），返回要连的地址；其余返回 None

        连接时直接连这些地址（`Pinned*` / 代理的 `connect_host`），不让 `imaplib` / `smtplib` 再解析一遍：
        校验一次、连接再解析一次，两次之间可以被 DNS 重绑，NA 就真的连上了内网的某个端口。
        按**这一台主机**判（`EmailAccount.is_tenant_host`）：转回平台方的账户，平台方只改了 IMAP 主机时，
        没动过的 SMTP 主机仍是租户的。平台方的主机照旧交给标准库解析——平台方可以用内网邮件服务器。

        Raises:
            UnsafeDownloadUrlError: 主机名不合法、解析失败或指向不允许的地址（这时还没有发起任何连接）
        """
        if not self.account.is_tenant_host(host):
            return None
        from nekro_agent.tools.tenant_egress import resolve_tenant_host

        return await resolve_tenant_host(host, port, subject="邮箱服务器")

    async def close(self) -> None:
        if not self.conn:
            return
        conn = self.conn
        self.conn = None
        await asyncio.to_thread(self._close_sync, conn)

    async def select_mailbox(self, preferred: str = "INBOX", override_folder: str | None = None) -> str:
        conn = self._require_conn()
        folders = await asyncio.to_thread(self._get_mailbox_folders_sync, conn)
        target = self._pick_mailbox(folders, preferred=preferred, override=override_folder)
        status, data = await asyncio.to_thread(conn.select, target)
        if status != "OK" and target.upper() == "INBOX":
            status, data = await asyncio.to_thread(conn.select, '"INBOX"')
        if status != "OK":
            raise RuntimeError(f"Failed to select mailbox {target} for account {self.account_username}: {data!r}")
        return target

    async def get_mailbox_folders_debug(self) -> list[str]:
        conn = self._require_conn()
        return await asyncio.to_thread(self._get_mailbox_folders_sync, conn)

    async def list_message_ids(self, unseen_only: bool) -> list[bytes]:
        """IMAP `SEARCH` 返回的邮件编号；**只收纯 ASCII 数字**

        编号是收信服务器给的，后面会被拿去拼附件目录（`<账户目录>/<编号>/`）。实例自有账户可以
        填自定义服务器，一个不老实的服务器回 `* SEARCH /app/nekro_agent` 或 `../..`，就能让附件
        落到附件目录外面（pathlib 拼接时右边是绝对路径会把左边整个丢掉）。IMAP 的消息序号按规范
        本来就只是数字，不是数字的一律丢掉并记 WARNING。
        """
        conn = self._require_conn()
        criteria = "UNSEEN" if unseen_only else "ALL"
        status, messages = await asyncio.to_thread(conn.search, None, criteria)
        if status != "OK":
            raise RuntimeError(f"邮件搜索失败: {status}")
        tokens = list(messages[0].split()) if messages and messages[0] else []
        valid = [token for token in tokens if is_imap_sequence_number(token)]
        if len(valid) != len(tokens):
            logger.warning(f"邮箱 {self.account_username} 的 SEARCH 回复里有 {len(tokens) - len(valid)} 个不是数字的编号，已丢弃")
        return valid

    async def fetch_raw_message(self, email_id: bytes) -> bytes | None:
        conn = self._require_conn()
        status, msg_data = await asyncio.to_thread(conn.fetch, email_id, "(RFC822)")
        if status != "OK":
            return None
        return msg_data[0][1] if msg_data and msg_data[0] else None

    async def mark_seen(self, email_id: bytes) -> None:
        conn = self._require_conn()
        await asyncio.to_thread(conn.store, email_id, "+FLAGS", "\\Seen")

    async def get_raw_email_content(self, email_id: str, folder: str | None = None) -> EmailRawContent:
        await self.select_mailbox(override_folder=folder)
        raw_email_data = await self.fetch_raw_message(email_id.encode() if isinstance(email_id, str) else email_id)
        raw_email_base64 = base64.b64encode(raw_email_data).decode("utf-8") if raw_email_data else ""
        html_content = ""
        text_content = ""
        if raw_email_data:
            email_message = email.message_from_bytes(raw_email_data)
            html_content, text_content = self._extract_body(email_message)
        return EmailRawContent(
            raw_email_base64=raw_email_base64,
            raw_email_size=len(raw_email_data) if raw_email_data else 0,
            html_content=html_content,
            text_content=text_content,
        )

    async def send_message(self, message: MIMEMultipart) -> None:
        provider_config = self.get_provider_config()
        smtp_host = provider_config.get("smtp_host", "")
        smtp_port = int(provider_config.get("smtp_port", 587))
        smtp_ssl_port = int(provider_config.get("smtp_ssl_port", smtp_port))
        use_ssl_preferred = str(provider_config.get("smtp_use_ssl", False)).lower() == "true"
        if not smtp_host:
            raise RuntimeError(f"未找到 {self.account.EMAIL_ACCOUNT} 的SMTP配置")

        try:
            await self._send_mail(
                message,
                smtp_host,
                smtp_ssl_port if use_ssl_preferred else smtp_port,
                use_ssl_preferred,
            )
        except Exception:
            if not use_ssl_preferred and smtp_ssl_port != smtp_port:
                await self._send_mail(message, smtp_host, smtp_ssl_port, True)
            else:
                raise

    async def download_attachment(self, part: Message, filename: str, target_path: Path) -> None:
        attachment_data = await asyncio.to_thread(part.get_payload, decode=True)
        if not attachment_data:
            raise RuntimeError(f"附件数据为空: {filename}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(target_path, "wb") as f:
            await f.write(attachment_data)

    def get_provider_config(self) -> dict[str, str]:
        if self.account.EMAIL_ACCOUNT == "自定义":
            return {
                "imap_host": self.account.CUSTOM_IMAP_HOST,
                "imap_port": str(self.account.CUSTOM_IMAP_PORT),
                "smtp_host": self.account.CUSTOM_SMTP_HOST,
                "smtp_port": str(self.account.CUSTOM_SMTP_PORT),
                "smtp_ssl_port": str(self.account.CUSTOM_SMTP_SSL_PORT),
                "smtp_use_ssl": str(self.account.CUSTOM_SMTP_USE_SSL).lower(),
            }
        return EMAIL_PROVIDER_CONFIGS.get(self.account.EMAIL_ACCOUNT, {})

    def _require_conn(self) -> imaplib.IMAP4_SSL:
        if not self.conn:
            raise RuntimeError(f"账户 {self.account_username} 未连接或不存在")
        return self.conn

    def _guard_peer(self, sock: socket.socket | None, host: str) -> None:
        """租户填的服务器：连上之后、发凭据之前再核对一次实际对端 IP

        连接本来就只连 `_checked_addresses` 校验过的地址，这一步是兜底（连接类被换掉、标准库行为变了）。
        不是允许的地址就在登录之前断开，授权码不会发给内网里的任何东西；对外按「连不上」报
        （`MailPeerRejectedError`），不带地址。走代理时对端是代理本身，核对它没有意义——那时 CONNECT
        的目标已经是校验过的地址，不再交给代理解析。平台方的主机（`is_tenant_host(host)` 为假）不受影响。
        """
        if not self.account.is_tenant_host(host) or self.proxy_url:
            return
        from nekro_agent.tools.common_util import UnsafeDownloadUrlError, assert_tenant_connected_peer_safe

        peer: str | None = None
        if sock is not None:
            with suppress(OSError):
                peer = str(sock.getpeername()[0])
        try:
            assert_tenant_connected_peer_safe(peer)
        except UnsafeDownloadUrlError as e:
            raise MailPeerRejectedError("连不上邮件服务器") from e

    def _imap_endpoint(self) -> tuple[str, int]:
        provider_config = self.get_provider_config()
        imap_host = provider_config.get("imap_host")
        if not imap_host:
            raise ValueError(f"邮箱提供商 {self.account.EMAIL_ACCOUNT} 缺少 imap_host 配置")
        return str(imap_host), int(provider_config.get("imap_port", 993))

    def _tls_context(self, host: str) -> ssl.SSLContext:
        return make_mail_tls_context(self.account.tls_verify_required(host))

    def _open_imap(self, imap_host: str, imap_port: int, addresses: Sequence[str] | None) -> imaplib.IMAP4_SSL:
        """建 IMAP 连接（构造函数里就连上了）并核对对端，**不登录**；密码与 OAuth2 两种客户端共用

        `addresses` 是 `_checked_addresses` 校验过的地址：租户填的主机必须给，否则拒绝连接——
        不能退回去让 `imaplib` 自己解析主机名。
        """
        context = self._tls_context(imap_host)
        if addresses:
            if self.proxy_url:
                conn: imaplib.IMAP4_SSL = ProxiedIMAP4SSL(
                    imap_host,
                    imap_port,
                    self.proxy_url,
                    self.imap_timeout,
                    connect_host=addresses[0],
                    ssl_context=context,
                )
            else:
                conn = PinnedIMAP4SSL(imap_host, imap_port, addresses, self.imap_timeout, ssl_context=context)
        elif self.account.is_tenant_host(imap_host):
            raise RuntimeError("租户填的邮件服务器要先校验地址，再按校验过的地址连接")
        elif self.proxy_url:
            conn = ProxiedIMAP4SSL(imap_host, imap_port, self.proxy_url, self.imap_timeout, ssl_context=context)
        else:
            conn = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=context, timeout=self.imap_timeout)
        try:
            self._guard_peer(getattr(conn, "sock", None), imap_host)
        except Exception:
            with suppress(Exception):
                conn.shutdown()
            raise
        return conn

    def _connect_sync(self, addresses: Sequence[str] | None = None) -> imaplib.IMAP4_SSL:
        """建 IMAP 连接并登录（在线程里跑）；`addresses` 见 `_open_imap`"""
        imap_host, imap_port = self._imap_endpoint()
        conn = self._open_imap(imap_host, imap_port, addresses)
        conn.login(self.account.USERNAME, self.account.PASSWORD)
        self._post_login_imap(conn, imap_host)
        return conn

    def _close_sync(self, conn: imaplib.IMAP4_SSL) -> None:
        with suppress(Exception):
            conn.close()
        with suppress(Exception):
            conn.logout()

    def _open_smtp(
        self,
        smtp_host: str,
        port: int,
        use_ssl: bool,
        timeout: int,
        addresses: Sequence[str] | None = None,
    ) -> smtplib.SMTP:
        """建 SMTP 连接、核对对端，再 STARTTLS 与密码登录，返回已登录的会话"""
        server = self._new_smtp(smtp_host, port, use_ssl, timeout, addresses)
        try:
            if not use_ssl:
                server.starttls(context=self._tls_context(smtp_host))
            server.login(self.account.USERNAME, self.account.PASSWORD)
        except Exception:
            with suppress(Exception):
                server.close()
            raise
        return server

    def _new_smtp(
        self,
        smtp_host: str,
        port: int,
        use_ssl: bool,
        timeout: int,
        addresses: Sequence[str] | None = None,
    ) -> smtplib.SMTP:
        """建 SMTP 连接（构造函数里就连上了）并核对对端，**不 STARTTLS、不登录**；两种客户端共用

        `addresses` 同 `_open_imap`：租户填的主机只连校验过的地址，没给就拒绝连接。
        """
        server: smtplib.SMTP
        if addresses:
            if use_ssl:
                server = (
                    ProxiedSMTPSSL(
                        smtp_host,
                        port,
                        self.proxy_url,
                        timeout,
                        connect_host=addresses[0],
                        context=self._tls_context(smtp_host),
                    )
                    if self.proxy_url
                    else PinnedSMTPSSL(smtp_host, port, addresses, timeout, context=self._tls_context(smtp_host))
                )
            else:
                server = (
                    ProxiedSMTP(smtp_host, port, self.proxy_url, timeout, connect_host=addresses[0])
                    if self.proxy_url
                    else PinnedSMTP(smtp_host, port, addresses, timeout)
                )
        elif self.account.is_tenant_host(smtp_host):
            raise RuntimeError("租户填的邮件服务器要先校验地址，再按校验过的地址连接")
        elif use_ssl:
            if self.proxy_url:
                server = ProxiedSMTPSSL(smtp_host, port, self.proxy_url, timeout, context=self._tls_context(smtp_host))
            else:
                server = smtplib.SMTP_SSL(smtp_host, port, timeout=timeout, context=self._tls_context(smtp_host))
        elif self.proxy_url:
            server = ProxiedSMTP(smtp_host, port, self.proxy_url, timeout)
        else:
            server = smtplib.SMTP(smtp_host, port, timeout=timeout)
        try:
            self._guard_peer(getattr(server, "sock", None), smtp_host)
        except Exception:
            with suppress(Exception):
                server.close()
            raise
        return server

    async def _send_mail(self, message: MIMEMultipart, smtp_host: str, port: int, use_ssl: bool) -> None:
        addresses = await self._checked_addresses(smtp_host, port)

        def _sync_send() -> None:
            with self._open_smtp(smtp_host, port, use_ssl, 60, addresses) as server:
                server.send_message(message)

        await asyncio.to_thread(_sync_send)

    async def check_smtp_login(self, timeout: int = 20) -> str:
        """只连上并登录 SMTP、不发信（后台「测试连接」用），返回实际用的主机与端口

        端口取法与 `send_message` 相同：要 SSL 就走 SSL 端口，否则普通端口 + STARTTLS，失败时
        回退到另一个端口。对端核对也走同一条（`_open_smtp`）。
        """
        provider_config = self.get_provider_config()
        smtp_host = provider_config.get("smtp_host", "")
        smtp_port = int(provider_config.get("smtp_port", 587))
        smtp_ssl_port = int(provider_config.get("smtp_ssl_port", smtp_port))
        use_ssl_preferred = str(provider_config.get("smtp_use_ssl", False)).lower() == "true"
        if not smtp_host:
            raise RuntimeError(f"未找到 {self.account.EMAIL_ACCOUNT} 的SMTP配置")

        async def _check(port: int, use_ssl: bool) -> None:
            addresses = await self._checked_addresses(smtp_host, port)

            def _sync_check() -> None:
                with self._open_smtp(smtp_host, port, use_ssl, timeout, addresses):
                    pass

            await asyncio.to_thread(_sync_check)

        first_port = smtp_ssl_port if use_ssl_preferred else smtp_port
        try:
            await _check(first_port, use_ssl_preferred)
        except Exception:
            if not use_ssl_preferred and smtp_ssl_port != smtp_port:
                await _check(smtp_ssl_port, True)
                return f"{smtp_host}:{smtp_ssl_port}"
            raise
        return f"{smtp_host}:{first_port}"

    def _post_login_imap(self, conn: imaplib.IMAP4, host: str) -> None:
        host_lower = str(host or "").lower()
        if any(domain in host_lower for domain in ["163.com", "126.com", "yeah.net"]):
            with suppress(Exception):
                self._send_imap_id(conn)

    def _send_imap_id(self, conn: imaplib.IMAP4) -> None:
        with suppress(Exception):
            imaplib.Commands["ID"] = ("AUTH", "SELECTED")
        payload = "(" + " ".join(
            [
                f'"{k}" "{v}"'
                for k, v in {
                    "name": "nekro-agent",
                    "version": "1.0.0",
                    "vendor": "nekro-agent",
                    "support-email": self.account.USERNAME,
                }.items()
            ],
        ) + ")"
        conn._simple_command("ID", payload)  # noqa: SLF001

    def _get_mailbox_folders_sync(self, conn: imaplib.IMAP4_SSL) -> list[str]:
        status, folders = conn.list()
        if status != "OK":
            return []
        folder_names: list[str] = []
        for folder in folders or []:
            line = folder.decode(errors="ignore") if isinstance(folder, bytes) else str(folder)
            name = self._parse_imap_list_line(line)
            if name:
                folder_names.append(name)
        if "INBOX" in folder_names:
            folder_names = ["INBOX"] + [f for f in folder_names if f != "INBOX"]
        return folder_names

    def _parse_imap_list_line(self, line: str) -> str | None:
        quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', line)
        if len(quoted) >= 2:
            name = quoted[-1].replace(r'\"', '"')
            return name if name else None
        tokens = line.split()
        if not tokens:
            return None
        name = tokens[-1].strip('"')
        return name if name else None

    def _pick_mailbox(self, folders: list[str], preferred: str = "INBOX", override: str | None = None) -> str:
        if override:
            return override
        for folder in folders:
            if folder.upper() == preferred.upper():
                return folder
        if preferred.upper() == "INBOX":
            return "INBOX"
        if not folders:
            raise RuntimeError("no folders available")
        return folders[0]

    def _extract_body(self, email_message: Message) -> tuple[str, str]:
        html_content = ""
        text_content = ""
        if email_message.is_multipart():
            for part in email_message.walk():
                ctype = part.get_content_type()
                if part.get_content_disposition() in ("attachment", "inline"):
                    continue
                if ctype in ("text/html", "text/plain"):
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        payload = part.get_payload(decode=True)
                        content = payload.decode(charset, errors="ignore") if isinstance(payload, bytes) else str(payload)
                    except Exception:
                        content = ""
                    if ctype == "text/html":
                        html_content = content
                    else:
                        text_content = content
        else:
            ctype = email_message.get_content_type()
            charset = email_message.get_content_charset() or "utf-8"
            try:
                payload = email_message.get_payload(decode=True)
                content = payload.decode(charset, errors="ignore") if isinstance(payload, bytes) else str(payload)
            except Exception:
                content = ""
            if ctype == "text/html":
                html_content = content
            elif ctype == "text/plain":
                text_content = content
        return html_content, text_content
