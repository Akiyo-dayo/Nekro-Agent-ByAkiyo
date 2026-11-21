"""平台方账户的收信开关要经保存接口落盘，关着的不进收信连接池

平台方账户页此前没有「收信」开关，新建账户默认开启收信，适配器会为它连 IMAP 并轮询。
把开关关掉只是前端意图，真正生效要同时满足两件事：

1. `POST/PUT /adapters/email/accounts` 把 `RECEIVE_ENABLED=false` 写进 `RECEIVE_ACCOUNTS`，
   这样下次读账户列表还是关的；
2. 适配器建收信连接池时按这个字段跳过它，轮询不会替一个只发信的账户去收信。

这里走真实的保存与读取端点，只把适配器和配置对象换成替身，避免碰磁盘上的适配器配置。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.email.adapter import EmailAdapter
from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.adapters.email.routers import (
    create_email_account,
    get_email_accounts,
    update_email_account,
)
from nekro_agent.services import email_accounts as mail

ADDRESS = "sender@example.test"


class FakePlatformConfig:
    """平台方配置的替身：`dump_config` 只记调用次数"""

    def __init__(self) -> None:
        self.RECEIVE_ACCOUNTS: list[EmailAccount] = []
        self.dumps = 0

    def dump_config(self) -> None:
        self.dumps += 1


class FakeAdapter:
    """保存端点只需要一个带 `config` 的适配器"""

    def __init__(self, config: FakePlatformConfig) -> None:
        self.config = config


class _PoolOnlyAdapter(EmailAdapter):
    """只够跑 `_init_email_clients` 的适配器：不连真实 IMAP"""

    def __init__(self, accounts: list[EmailAccount]) -> None:
        self._config = SimpleNamespace(RECEIVE_ACCOUNTS=list(accounts))
        self.email_clients: dict[str, Any] = {}
        self.imap_connections: dict[str, Any] = {}
        self.imap_locks: dict[str, Any] = {}

    @property
    def config(self) -> Any:  # type: ignore[override]
        return self._config

    def _create_email_client(self, account: EmailAccount) -> Any:  # type: ignore[override]
        raise AssertionError(f"只发信的账户不该被建收信客户端: {account.USERNAME}")


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.clear_default_sender_cache()
    try:
        yield
    finally:
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch):
    adapter = FakeAdapter(FakePlatformConfig())

    async def sync(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def purge(*_args: Any, **_kwargs: Any) -> int:
        return 0

    async def no_instance_accounts(_self: Any) -> list:
        return []

    monkeypatch.setattr(mail, "sync_runtime_account", sync)
    monkeypatch.setattr(mail, "purge_foreign_cache_rows", purge)
    monkeypatch.setattr(EmailAdapter, "_load_instance_accounts", no_instance_accounts)
    return adapter


async def receive_pool(accounts: list[EmailAccount]) -> _PoolOnlyAdapter:
    pool = _PoolOnlyAdapter(accounts)
    await pool._init_email_clients()  # noqa: SLF001
    return pool


class TestReceiveSwitchIsPersisted:
    async def test_create_with_receive_off_reads_back_off_and_is_not_polled(
        self, db, fake_adapter,
    ) -> None:
        result = await create_email_account(
            {
                "DISPLAY_NAME": "平台发信箱",
                "EMAIL_ACCOUNT": "QQ邮箱",
                "USERNAME": ADDRESS,
                "PASSWORD": "authorization-code",
                "ENABLED": True,
                "RECEIVE_ENABLED": False,
                "SEND_ENABLED": True,
                "AUTH_TYPE": "password",
                "TRANSPORT_TYPE": "imap_smtp",
            },
            adapter=fake_adapter,
        )

        assert result["success"] is True
        saved = (await get_email_accounts(adapter=fake_adapter))["accounts"][0]
        assert saved["RECEIVE_ENABLED"] is False
        assert fake_adapter.config.dumps == 1

        pool = await receive_pool(fake_adapter.config.RECEIVE_ACCOUNTS)
        assert pool.email_clients == {}

    async def test_update_turns_receive_off_and_drops_it_from_the_pool(
        self, db, fake_adapter,
    ) -> None:
        fake_adapter.config.RECEIVE_ACCOUNTS = [
            EmailAccount(
                USERNAME=ADDRESS,
                PASSWORD="authorization-code",
                ENABLED=True,
                RECEIVE_ENABLED=True,
                SEND_ENABLED=True,
            ),
        ]

        await update_email_account(0, {"RECEIVE_ENABLED": False}, adapter=fake_adapter)

        saved = (await get_email_accounts(adapter=fake_adapter))["accounts"][0]
        assert saved["RECEIVE_ENABLED"] is False
        assert fake_adapter.config.dumps == 1

        pool = await receive_pool(fake_adapter.config.RECEIVE_ACCOUNTS)
        assert pool.email_clients == {}
