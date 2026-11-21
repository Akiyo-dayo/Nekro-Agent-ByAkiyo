"""收信连接池只放开着收信的平台方账户；自定义邮件主机的写法收紧

## 收信连接池

`EmailAdapter.email_clients` 是**收信**连接池：轮询循环逐个遍历它去拉信。只发信的账户
（平台方关了收信的、全部实例自有的）一旦被放进来，轮询会去对它 `select` 信箱、失败后重连 IMAP，
等于替一个没开收信的账户收信——实例自有账户还会因此走上平台方那条「建 email 频道」的路。

## 主机名

`assert_tenant_host_safe` 校验的是 URL 解析出来的主机，而 IMAP / SMTP 客户端拿到的是原字符串。
`a@b`、`a/b` 这类写法在两边会被理解成两台机器，所以只收裸主机名或 `[IPv6]`。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest

from nekro_agent.adapters.email.adapter import EmailAdapter
from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.tools.common_util import UnsafeDownloadUrlError, assert_tenant_host_safe


class _FakeClient:
    def __init__(self, account: EmailAccount, connected: List[str]) -> None:
        self.account = account
        self._connected = connected
        self.conn = None

    async def connect(self) -> None:
        self._connected.append(self.account.USERNAME)


class _PoolOnlyAdapter(EmailAdapter):
    """只够跑 `_init_email_clients` 的适配器：不走真正的初始化，配置与客户端都是假的"""

    def __init__(self, accounts: List[EmailAccount], connected: List[str]) -> None:  # noqa: D107
        self._fake_config = SimpleNamespace(RECEIVE_ACCOUNTS=accounts)
        self._connected = connected
        self.email_clients = {}
        self.imap_connections = {}
        self.imap_locks = {}

    @property
    def config(self) -> Any:  # type: ignore[override]
        return self._fake_config

    def _create_email_client(self, account: EmailAccount) -> Any:  # type: ignore[override]
        return _FakeClient(account, self._connected)


def _bare_adapter(accounts: List[EmailAccount], connected: List[str]) -> EmailAdapter:
    return _PoolOnlyAdapter(accounts, connected)


@pytest.fixture(autouse=True)
def _no_instance_accounts_from_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件不建库：实例自有账户那一半直接给空

    不替换的话 `_init_email_clients()` 会去读实例自有邮箱表；没建库时 Tortoise 按前面用例留下的
    配置懒建一条连接，查询失败被 `_load_instance_accounts` 吞掉，连接却没人关，pytest 收尾时
    进程就退不出去。
    """

    async def no_instance_accounts(_self: Any) -> list:
        return []

    monkeypatch.setattr(EmailAdapter, "_load_instance_accounts", no_instance_accounts)


class TestReceivePoolOnlyHoldsReceivingAccounts:
    async def test_send_only_accounts_are_not_pooled(self) -> None:
        connected: List[str] = []
        adapter = _bare_adapter(
            [
                EmailAccount(USERNAME="inbox@example.test", ENABLED=True, RECEIVE_ENABLED=True),
                EmailAccount(USERNAME="sender@example.test", ENABLED=True, RECEIVE_ENABLED=False, SEND_ENABLED=True),
                EmailAccount(USERNAME="off@example.test", ENABLED=False, RECEIVE_ENABLED=True),
            ],
            connected,
        )

        await adapter._init_email_clients()  # noqa: SLF001

        assert set(adapter.email_clients) == {"inbox@example.test"}
        assert connected == ["inbox@example.test"]

    async def test_instance_accounts_are_not_read_when_building_the_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """建池不查账户表：实例自有账户不收信，启动早期库也未必就绪"""
        called: List[Any] = []

        async def all_accounts(_self: Any) -> list:
            called.append(True)
            return []

        monkeypatch.setattr(EmailAdapter, "all_accounts", all_accounts)
        adapter = _bare_adapter([EmailAccount(USERNAME="inbox@example.test", ENABLED=True, RECEIVE_ENABLED=True)], [])

        await adapter._init_email_clients()  # noqa: SLF001

        assert called == []


class TestAttachmentDownloadStaysInTheAccountDir:
    """UID 是收信服务器给的：实例自有账户可以填自定义 IMAP 服务器，`../..` 形态的 UID 不能把附件写出去"""

    @staticmethod
    def _part() -> Any:
        from email.mime.application import MIMEApplication

        return MIMEApplication(b"attachment-bytes", Name="a.txt")

    async def test_a_normal_uid_lands_under_the_account_dir(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.adapters.email import adapter as adapter_module

        monkeypatch.setattr(adapter_module.OsEnv, "DATA_DIR", str(tmp_path))
        adapter = _bare_adapter([], [])

        await adapter._download_attachment(self._part(), "a.txt", "own@example.test", "主题", "7", directory="_i1")  # noqa: SLF001

        assert (tmp_path / "uploads" / "email_attachment" / "_i1" / "7" / "a.txt").read_bytes() == b"attachment-bytes"

    @pytest.mark.parametrize("uid", ["../../escape", "..", "../_i2", "a/../../b", "..\\escape"])
    async def test_a_hostile_uid_writes_nothing(self, tmp_path, monkeypatch: pytest.MonkeyPatch, uid: str) -> None:
        from nekro_agent.adapters.email import adapter as adapter_module

        monkeypatch.setattr(adapter_module.OsEnv, "DATA_DIR", str(tmp_path))
        adapter = _bare_adapter([], [])

        await adapter._download_attachment(self._part(), "a.txt", "own@example.test", "主题", uid, directory="_i1")  # noqa: SLF001

        assert list(tmp_path.rglob("a.txt")) == []

    async def test_a_dot_dot_subject_without_uid_writes_nothing(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.adapters.email import adapter as adapter_module

        monkeypatch.setattr(adapter_module.OsEnv, "DATA_DIR", str(tmp_path))
        adapter = _bare_adapter([], [])

        await adapter._download_attachment(self._part(), "a.txt", "own@example.test", "..", None, directory="_i1")  # noqa: SLF001

        assert list(tmp_path.rglob("a.txt")) == []


class TestOwnerIsKnownBeforeReconnecting:
    async def test_a_new_instance_account_is_registered_before_the_reconnect(self) -> None:
        """适配器的重连按「地址在不在归属表里」决定走哪条路：新账户必须先登记再重连，
        否则它会按平台方账户去连，跳过总闸与连接前的主机再校验"""
        from nekro_agent.services import email_accounts as mail

        seen: List[bool] = []

        class FakeAdapter:
            def __init__(self) -> None:
                self.instance_account_owner: dict = {}
                self.account_chat_mapping: dict = {}

            async def _reconnect_email_client(self, account: EmailAccount) -> bool:
                seen.append(account.USERNAME in self.instance_account_owner)
                return True

        adapter = FakeAdapter()
        owner = SimpleNamespace(account_id=1, adapter_key="onebot_v11", instance_key="inst1")
        account = EmailAccount(USERNAME="own@example.test", ENABLED=True, RECEIVE_ENABLED=True)

        await mail.sync_runtime_account(adapter, account, owner=owner)  # type: ignore[arg-type]

        assert seen == [True]
        assert "own@example.test" not in adapter.account_chat_mapping

    async def test_a_platform_account_drops_a_stale_owner_before_reconnecting(self) -> None:
        from nekro_agent.services import email_accounts as mail

        seen: List[bool] = []

        class FakeAdapter:
            def __init__(self) -> None:
                self.instance_account_owner: dict = {"ops@example.test": object()}
                self.account_chat_mapping: dict = {}

            async def _reconnect_email_client(self, account: EmailAccount) -> bool:
                seen.append(account.USERNAME in self.instance_account_owner)
                return True

        adapter = FakeAdapter()
        await mail.sync_runtime_account(
            adapter, EmailAccount(USERNAME="ops@example.test", ENABLED=True, RECEIVE_ENABLED=True),
        )

        assert seen == [False]
        assert adapter.account_chat_mapping == {"ops@example.test": "ops@example.test"}


class TestAccountAddressSpelling:
    @pytest.mark.parametrize("address", ["../x@example.test", "a\\b@example.test", "a b@example.test", "no-at-sign"])
    def test_path_like_or_broken_addresses_are_refused(self, address: str) -> None:
        from nekro_agent.services import email_accounts as mail

        with pytest.raises(ValueError):
            mail.validate_mail_address(address)


class TestCustomHostSpelling:
    @pytest.mark.parametrize(
        "host",
        [
            "smtp.example.test@internal",
            "smtp.example.test/path",
            "smtp.example.test#frag",
            "smtp.example.test?x=1",
            "smtp.example.test\\x",
            "smtp example.test",
            "::1",
            "smtp.example.test:25",
        ],
    )
    async def test_ambiguous_spellings_are_refused_before_resolving(
        self, host: str, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        resolved: List[Any] = []

        async def fake_getaddrinfo(*args: Any, **_kwargs: Any) -> list:
            resolved.append(args)
            return []

        monkeypatch.setattr("nekro_agent.tools.common_util._system_getaddrinfo", fake_getaddrinfo)

        with pytest.raises(UnsafeDownloadUrlError, match="主机名不合法"):
            await assert_tenant_host_safe(host, 993, subject="邮箱服务器")
        assert resolved == []

    async def test_bracketed_ipv6_loopback_is_refused_as_non_public(self) -> None:
        """补上方括号之后能正常走到网段判定，而不是在拼 URL 时就报「主机名不合法」
        （租户看到的是统一的那一句：解析不到与指向不允许的地址不分开说）"""
        from nekro_agent.tools.common_util import tenant_target_unavailable_message

        with pytest.raises(UnsafeDownloadUrlError) as excinfo:
            await assert_tenant_host_safe("[::1]", 993, subject="邮箱服务器")
        assert str(excinfo.value) == tenant_target_unavailable_message("邮箱服务器")

    async def test_loopback_name_is_refused(self) -> None:
        with pytest.raises(UnsafeDownloadUrlError):
            await assert_tenant_host_safe("127.0.0.1", 465, subject="邮箱服务器")
