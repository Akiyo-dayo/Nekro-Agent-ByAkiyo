import asyncio
import imaplib
from collections.abc import Callable, Sequence
from email.mime.multipart import MIMEMultipart

from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.core.logger import get_sub_logger

from .imap_smtp_password import ImapSmtpPasswordClient
from .oauth import OAuthTokenManager

logger = get_sub_logger("adapter.email.client")


class ImapSmtpOAuth2Client(ImapSmtpPasswordClient):
    """XOAUTH2 登录的 IMAP / SMTP

    连接一律走父类的 `_open_imap` / `_new_smtp`：主机若是租户填的（从实例转回平台方、主机没被平台方改过的
    账户，之后又被改成 OAuth2），同样只解析一次、直连校验过的地址并在登录前核对对端——认证方式换了，
    主机的 DNS 仍可能在租户手里。
    """

    def __init__(
        self,
        account: EmailAccount,
        imap_timeout: int,
        oauth_proxy: str = "",
        on_token_update: Callable[[], None] | None = None,
    ):
        super().__init__(account, imap_timeout, oauth_proxy)
        self.token_manager = OAuthTokenManager(account, oauth_proxy, on_token_update)

    async def connect(self) -> None:
        access_token = await self.token_manager.get_access_token()
        imap_host, imap_port = self._imap_endpoint()
        addresses = await self._checked_addresses(imap_host, imap_port)
        self.conn = await asyncio.to_thread(self._connect_sync_with_token, access_token, addresses)

    async def send_message(self, message: MIMEMultipart) -> None:
        access_token = await self.token_manager.get_access_token()
        provider_config = self.get_provider_config()
        smtp_host = provider_config.get("smtp_host", "")
        smtp_port = int(provider_config.get("smtp_port", 587))
        smtp_ssl_port = int(provider_config.get("smtp_ssl_port", smtp_port))
        use_ssl_preferred = str(provider_config.get("smtp_use_ssl", False)).lower() == "true"
        if not smtp_host:
            raise RuntimeError(f"未找到 {self.account.EMAIL_ACCOUNT} 的SMTP配置")

        try:
            await self._send_mail_oauth2(
                message,
                smtp_host,
                smtp_ssl_port if use_ssl_preferred else smtp_port,
                use_ssl_preferred,
                access_token,
            )
        except Exception:
            if not use_ssl_preferred and smtp_ssl_port != smtp_port:
                await self._send_mail_oauth2(message, smtp_host, smtp_ssl_port, True, access_token)
            else:
                raise

    def _connect_sync_with_token(
        self,
        access_token: str,
        addresses: Sequence[str] | None = None,
    ) -> imaplib.IMAP4_SSL:
        imap_host, imap_port = self._imap_endpoint()
        conn = self._open_imap(imap_host, imap_port, addresses)
        auth_string = self._build_xoauth2_string(access_token)
        conn.authenticate("XOAUTH2", lambda _: auth_string.encode())
        self._post_login_imap(conn, imap_host)
        return conn

    async def _send_mail_oauth2(
        self,
        message: MIMEMultipart,
        smtp_host: str,
        port: int,
        use_ssl: bool,
        access_token: str,
    ) -> None:
        auth_string = self._build_xoauth2_string(access_token)
        addresses = await self._checked_addresses(smtp_host, port)

        def auth_object(_challenge: bytes | None = None) -> str:
            return auth_string

        def _sync_send() -> None:
            with self._new_smtp(smtp_host, port, use_ssl, 60, addresses) as server:
                server.ehlo()
                if not use_ssl:
                    server.starttls(context=self._tls_context(smtp_host))
                    server.ehlo()
                auth_code, auth_response = server.auth("XOAUTH2", auth_object)
                logger.debug(f"SMTP XOAUTH2 认证结果: host={smtp_host}, port={port}, code={auth_code}, response={auth_response!r}")
                server.send_message(message)

        await asyncio.to_thread(_sync_send)

    def _build_xoauth2_string(self, access_token: str) -> str:
        return f"user={self.account.USERNAME}\x01auth=Bearer {access_token}\x01\x01"
