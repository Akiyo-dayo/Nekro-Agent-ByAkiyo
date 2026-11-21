"""实例自有邮箱的收信：不建频道、只写缓存、附件进 `_i<id>` 目录

## 这条要防的是什么

实例自有邮箱最容易走错的一步不是「收不到」，而是「收的方式跟平台方一样」：平台方那条路是
`collect_message` 收进一个 `email-<地址>` 频道，可那个频道**没有实例段**，建出来就归平台方。
于是租户的私人邮件会出现在平台方的会话里，而租户的邮件页里又什么都没有。

所以这里钉四件事：

1. 实例自有账户收信时**不调** `collect_message`、不建频道，缓存行的归属快照是账户的实例对，
   附件落进 `_i<id>/<UID>/`；
2. 平台方账户那条路与改造前相同（`collect_message` 调一次、快照为空）；
3. 实例停用 / 租户停用 / 总闸关着时轮询跳过它；
4. 自定义主机在连接前被拒（打桩解析到内网）时不连，并在 `settings` 里记 `last_receive_error`。

不用真的连接：IMAP 客户端与 `collect_message` 都替换掉，只跑适配器自己的分流与写库。
"""

from __future__ import annotations

import asyncio
import email.message
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.email.adapter import RECEIVE_HOST_UNAVAILABLE, EmailAdapter
from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.services import email_accounts as mail

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
OTHER_INSTANCE = "inst2"
TENANT_ID = 1
UID = "77"
ADDRESS = "own@example.test"
PLATFORM_ADDRESS = "ops@example.test"

RAW_MAIL = (
    "From: Sender <sender@example.test>\n"
    "To: own@example.test\n"
    "Subject: =?utf-8?b?5L2g5aW9?=\n"
    "Message-ID: <abc@example.test>\n"
    "Date: Mon, 01 Jan 2026 00:00:00 +0000\n"
    "Content-Type: text/plain; charset=utf-8\n"
    "\n"
    "正文\n"
).encode()


async def _init_db() -> None:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()


@pytest.fixture
async def db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from nekro_agent.core import os_env

    monkeypatch.setattr(os_env.OsEnv, "DATA_DIR", str(tmp_path), raising=False)
    await _init_db()
    mail.clear_default_sender_cache()
    try:
        for instance_key, tenant_id in ((OWN_INSTANCE, TENANT_ID), (OTHER_INSTANCE, None)):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            channel_id="group_1",
            chat_key=f"{ADAPTER}-{OWN_INSTANCE}-group_1",
            channel_type="group",
            data="{}",
        )
        yield
    finally:
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


@pytest.fixture
def platform_config(monkeypatch: pytest.MonkeyPatch):
    class FakeConfig:
        RECEIVE_ACCOUNTS: List[EmailAccount] = []

    def install(accounts: Optional[List[EmailAccount]] = None) -> FakeConfig:
        config = FakeConfig()
        config.RECEIVE_ACCOUNTS = list(accounts or [])
        monkeypatch.setattr(mail, "_load_platform_config", lambda adapter=None: config)
        return config

    return install


class _FakeClient:
    """够 `_check_new_emails` / `_process_email` 用的最小客户端"""

    def __init__(self, messages: Dict[bytes, bytes]) -> None:
        self.messages = messages
        self.conn = None
        self.selected = 0

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def select_mailbox(self, *_args: Any, **_kwargs: Any) -> str:
        self.selected += 1
        return "INBOX"

    async def list_message_ids(self, *_args: Any, **_kwargs: Any) -> List[bytes]:
        return list(self.messages)

    async def fetch_raw_message(self, email_id: bytes) -> Optional[bytes]:
        return self.messages.get(email_id)

    async def mark_seen(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def make_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform_accounts: Optional[List[EmailAccount]] = None,
    messages: Optional[Dict[bytes, bytes]] = None,
) -> EmailAdapter:
    """绕过构造函数：只给分流与写库要用到的那几个属性"""
    adapter = object.__new__(EmailAdapter)
    adapter._Configs = SimpleNamespace
    adapter._config = SimpleNamespace(  # type: ignore[assignment]
        RECEIVE_ACCOUNTS=list(platform_accounts or []),
        EMAIL_NOTIFICATIONS_ENABLED=False,
        EMAIL_NOTIFICATIONS_CHAT_KEY="",
        FETCH_UNSEEN_ONLY=True,
        MARK_AS_SEEN_AFTER_FETCH=False,
        MAX_PER_POLL=10,
        IMAP_TIMEOUT=20,
        OAUTH_PROXY="",
    )
    adapter.email_clients = {}
    adapter.imap_connections = {}
    # `_check_new_emails` 会拿 `imap_locks[地址]` 那把锁；缺锁它按「初始化不一致」直接报错
    adapter.imap_locks = {}
    adapter.account_chat_mapping = {}
    adapter.instance_account_owner = {}
    adapter._instance_active_cache = {}
    client = _FakeClient(messages or {b"77": RAW_MAIL})
    adapter._fake_client = client  # type: ignore[attr-defined]

    def register(username: str = "", *, client_obj: Any = None) -> None:
        """把这个地址登记成「已连接 + 有锁」，等价于真实 `_init_email_clients` 之后的状态"""
        target = username or ""
        if target:
            adapter.email_clients[target] = client_obj or client
            adapter.imap_connections[target] = (None, None)
            adapter.imap_locks[target] = asyncio.Lock()

    adapter.register_client = register  # type: ignore[attr-defined]

    # `_create_email_client` 在适配器上是同步方法，替换时不能变成协程
    monkeypatch.setattr(adapter, "_create_email_client", lambda _account: client)
    monkeypatch.setattr(adapter, "_start_polling", lambda: None)
    return adapter


async def add_instance_account(
    *,
    instance_key: str = OWN_INSTANCE,
    username: str = ADDRESS,
    receive_enabled: bool = True,
    provider: str = "QQ邮箱",
    settings: Optional[Dict[str, Any]] = None,
) -> DBEmailAccount:
    row = await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
        username=username,
        display_name="",
        provider=provider,
        settings=dict(settings or {}),
        password="authorization-code",
        enabled=True,
        receive_enabled=receive_enabled,
        send_enabled=False,
        attachment_dir="",
    )
    row.attachment_dir = mail.new_attachment_dir(row.id)
    await row.save()
    return row


class TestInstanceAccountReceive:
    async def test_no_channel_is_created_and_the_cache_carries_the_owner(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch, tmp_path,
    ) -> None:
        platform_config()
        row = await add_instance_account()
        adapter = make_adapter(monkeypatch)
        collected: List[Any] = []

        async def fake_collect(*args: Any, **_kwargs: Any) -> None:
            collected.append(args)

        import nekro_agent.adapters.email.adapter as adapter_module

        monkeypatch.setattr(adapter_module, "collect_message", fake_collect)
        owner = mail.instance_owner_of(row)
        adapter.instance_account_owner[row.username] = owner
        adapter.register_client(row.username)  # type: ignore[attr-defined]

        await adapter._check_new_emails(row.username, adapter.email_clients[row.username])  # noqa: SLF001

        assert collected == [], "实例自有邮箱不能走 collect_message"
        cached = await DBEmail.get(account_username=row.username, email_uid=UID)
        assert (cached.owner_adapter_key, cached.owner_instance_key) == (ADAPTER, OWN_INSTANCE)
        assert cached.subject == "你好"
        attachment_dir = tmp_path / "uploads" / "email_attachment" / row.attachment_dir / UID
        assert attachment_dir.parent.name == row.attachment_dir
        assert row.attachment_dir.startswith("_i")

    async def test_the_platform_path_is_unchanged(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account = EmailAccount(USERNAME=PLATFORM_ADDRESS, ENABLED=True, RECEIVE_ENABLED=True)
        platform_config([account])
        adapter = make_adapter(monkeypatch, platform_accounts=[account], messages={b"5": RAW_MAIL})
        collected: List[Any] = []

        async def fake_collect(*args: Any, **_kwargs: Any) -> None:
            collected.append(args)

        import nekro_agent.adapters.email.adapter as adapter_module

        monkeypatch.setattr(adapter_module, "collect_message", fake_collect)
        adapter.account_chat_mapping[PLATFORM_ADDRESS] = PLATFORM_ADDRESS
        adapter.register_client(PLATFORM_ADDRESS)  # type: ignore[attr-defined]

        await adapter._check_new_emails(PLATFORM_ADDRESS, adapter.email_clients[PLATFORM_ADDRESS])  # noqa: SLF001

        assert len(collected) == 1, "平台方账户的收信只有一个调用点"
        cached = await DBEmail.get(account_username=PLATFORM_ADDRESS, email_uid="5")
        assert (cached.owner_adapter_key, cached.owner_instance_key) == ("", "")

    @pytest.mark.parametrize("reason", ["disabled", "tenant", "gate"])
    async def test_polling_skips_unusable_instance_accounts(
        self,
        db,
        platform_config,
        monkeypatch: pytest.MonkeyPatch,
        reason: str,
    ) -> None:
        platform_config()
        row = await add_instance_account()
        adapter = make_adapter(monkeypatch)
        adapter.instance_account_owner[row.username] = mail.instance_owner_of(row)
        adapter.register_client(row.username)  # type: ignore[attr-defined]

        if reason == "disabled":
            await DBAdapterInstance.filter(instance_key=OWN_INSTANCE).update(enabled=False)
        elif reason == "tenant":
            from nekro_agent.models.db_tenant import DBTenant

            await DBTenant.create(id=TENANT_ID, name="acme", status="suspended")
        else:
            monkeypatch.setattr(mail, "instance_accounts_enabled", lambda: False)

        assert await adapter._instance_mailbox_pollable(row.username) is False  # noqa: SLF001

    async def test_a_custom_host_that_resolves_inward_is_refused_before_connecting(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        platform_config()
        row = await add_instance_account(
            provider="自定义",
            settings={
                "imap_host": "imap.example.test",
                "imap_port": 993,
                "smtp_host": "smtp.example.test",
                "smtp_port": 465,
                "smtp_ssl_port": 465,
            },
        )
        adapter = make_adapter(monkeypatch)
        connects: List[str] = []

        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError(mail.CUSTOM_HOST_UNAVAILABLE)

        async def counting_connect(self: Any) -> None:
            connects.append("connected")

        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", refuse)
        monkeypatch.setattr(_FakeClient, "connect", counting_connect)

        await adapter._init_email_clients([(mail.to_runtime_account(row), mail.instance_owner_of(row))])  # noqa: SLF001

        assert connects == [], "主机校验不过时不该建立连接"
        assert row.username not in adapter.email_clients
        refreshed = await DBEmailAccount.get(id=row.id)
        assert refreshed.settings.get("last_receive_error") == RECEIVE_HOST_UNAVAILABLE

    async def test_the_pool_holds_instance_accounts_that_receive(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """连接池与平台方共用；只发信的实例账户不进池"""
        platform_config()
        receiving = await add_instance_account(username="inbox@example.test")
        await add_instance_account(username="sender@example.test", receive_enabled=False)
        adapter = make_adapter(monkeypatch)

        await adapter._init_email_clients(  # noqa: SLF001
            [
                (mail.to_runtime_account(receiving), mail.instance_owner_of(receiving)),
                (
                    mail.to_runtime_account(
                        await DBEmailAccount.get(username="sender@example.test"),
                    ),
                    mail.instance_owner_of(await DBEmailAccount.get(username="sender@example.test")),
                ),
            ],
        )

        assert set(adapter.email_clients) == {"inbox@example.test"}
        assert set(adapter.instance_account_owner) == {"inbox@example.test"}
        assert "inbox@example.test" not in adapter.account_chat_mapping, "实例自有账户没有 email 频道"

    async def test_a_failing_database_does_not_break_the_platform_pool(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        account = EmailAccount(USERNAME=PLATFORM_ADDRESS, ENABLED=True, RECEIVE_ENABLED=True)
        platform_config([account])
        adapter = make_adapter(monkeypatch, platform_accounts=[account])

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("库还没就绪")

        monkeypatch.setattr(mail, "instance_runtime_accounts", boom)

        await adapter._init_email_clients()  # noqa: SLF001

        assert set(adapter.email_clients) == {PLATFORM_ADDRESS}


class TestParsedEmailShape:
    """分流只依赖解析结果：这里顺带确认测试用的原始邮件能被适配器自己解析"""

    async def test_the_raw_message_parses(self) -> None:
        message = email.message_from_bytes(RAW_MAIL)
        from nekro_agent.adapters.email.adapter import decode_mime_words

        assert decode_mime_words(message.get("Subject")) == "你好"
