"""生命周期：彻底删除实例、实例归属迁移、租户划转、换会话标识

## 为什么这些要在服务层单独钉一遍

邮箱的归属挂在**两列字符串**上（`owner_adapter_key` / `owner_instance_key`），没有任何外键，
也没有任何东西会因为「实例那一行没了」被数据库顺手带走。漏一步的后果都是**静默**的：

- 漏删账户：`instance_key` 是可重新认领的 slug，同名的新实例（可能归另一个租户）
  一建出来就继承了上一个主人的收件箱 —— 密码重置邮件与验证码都在里面；
- 漏删缓存邮件：账户没了，那些信还占着 `(地址, UID)` 唯一键，地址将来被别人填进来就撞；
- 漏删用户偏好：那是**用户**的地址与订阅意愿，跟着实例划给新租户等于交出一份名单；
- 漏注销平台方共享：同名新实例一建出来就自动拿到那份发信授权；
- 漏迁账户：新实例的「本实例邮箱」是空的，旧账户留在一个没人指向的实例对下。

## 与真实调用链的关系

`purge_instance` / `migrate_instance_ownership` / `bind_tenant_instances` 的接线各自有别的
用例；这里钉的是这三条路径对邮箱的**实际效果**，所以直接调它们，不另写一份等价逻辑。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_email_outbox import DBEmailOutbox
from nekro_agent.models.db_email_send_log import DBEmailSendLog
from nekro_agent.models.db_email_user_pref import DBEmailUserPref
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.console import instance_manager
from nekro_agent.services.console.instance_manager import is_instance_attachment_dir

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OTHER_INSTANCE = "inst2"

ACME_TENANT_ID = 1


def chat_key_of(instance_key: str) -> str:
    return f"{ADAPTER}-{instance_key}-group_{instance_key}"


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
def platform_config(monkeypatch: pytest.MonkeyPatch):
    """把平台方账户换成一组可控的假账户，并记录 `dump_config()` 被调了几次"""

    class FakeConfig:
        def __init__(self) -> None:
            self.RECEIVE_ACCOUNTS: list[EmailAccount] = []
            self.dumped = 0

        def dump_config(self) -> None:
            self.dumped += 1

    def install(accounts: list[EmailAccount]) -> FakeConfig:
        config = FakeConfig()
        config.RECEIVE_ACCOUNTS = accounts
        monkeypatch.setattr(mail, "_load_platform_config", lambda adapter=None: config)
        return config

    return install


@pytest.fixture
def fake_adapters(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """让生命周期里那几处 `loaded_adapters.get("email")` 拿到一个假适配器"""
    import nekro_agent.adapters as adapters_module

    state = SimpleNamespace(removed=[], synced=[])

    class FakeAdapter:
        def __init__(self) -> None:
            self.config = SimpleNamespace(RECEIVE_ACCOUNTS=[], dump_config=lambda: None)

        async def _remove_email_client(self, username: str) -> None:
            state.removed.append(username)

        async def _reconnect_email_client(self, account: Any) -> bool:
            state.synced.append(account.USERNAME)
            return True

    monkeypatch.setitem(adapters_module.loaded_adapters, "email", FakeAdapter())
    return state


async def create_instance(instance_key: str = INSTANCE, *, tenant_id: Optional[int] = None) -> DBAdapterInstance:
    instance = await DBAdapterInstance.create(
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
        instance_key=instance_key,
        channel_id=f"group_{instance_key}",
        chat_key=chat_key_of(instance_key),
        channel_name=f"{instance_key} 的群",
        channel_type="group",
        data="{}",
        is_active=True,
    )
    return instance


async def create_account(
    instance_key: str = INSTANCE,
    username: str = "own@example.test",
    *,
    attachment_dir: str = "",
    own_instance_dir: bool = False,
) -> DBEmailAccount:
    """`own_instance_dir=True`：附件目录用这一行自己的 `_i<id>`（新建实例自有账户时的命名）"""
    row = await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
        username=username,
        display_name="",
        provider="自定义",
        settings={},
        password="secret",
        send_enabled=True,
        attachment_dir=attachment_dir,
    )
    if own_instance_dir:
        row.attachment_dir = mail.new_attachment_dir(row.id)
        await row.save(update_fields=["attachment_dir"])
    return row


# 附件路径里的 UID 段只收数字（IMAP 的 UID）
MAIL_UID = "1001"
STRAY_UID = "2002"


async def create_mail(instance_key: str, username: str = "own@example.test", email_uid: str = MAIL_UID) -> DBEmail:
    return await DBEmail.create(
        account_username=username,
        email_uid=email_uid,
        subject="主题",
        sender="noreply@sender.example",
        recipients=f'["{username}"]',
        body_text="正文",
        has_attachments=False,
        attachment_names="[]",
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
    )


class TestPurgeInstanceRemovesMailboxes:
    """彻底删除实例：五张表 + 附件目录 + 平台方共享名单"""

    async def test_accounts_and_their_data_are_deleted(self, db, fake_adapters) -> None:
        instance = await create_instance()
        await create_account()
        await create_mail(INSTANCE)
        await DBEmailUserPref.create(
            adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="10001", email="alice@example.test",
        )
        await DBEmailOutbox.create(
            batch_key="b1", adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="10001",
            account_kind=mail.KIND_INSTANCE, account_ref="1", to_address="alice@example.test",
            subject="节日", body="正文", reason="", status="queued", attempts=0,
        )
        await DBEmailSendLog.create(
            account_kind=mail.KIND_INSTANCE, account_ref="1", adapter_key=ADAPTER, instance_key=INSTANCE,
            chat_key="", tenant_id=None, purpose=mail.PURPOSE_FESTIVAL,
            to_address="alice@example.test", subject="节日", reason="", ok=True, error="",
        )
        # 另一个实例的行不该被连带删掉
        await create_account(OTHER_INSTANCE, "other@example.test")

        await instance_manager.purge_instance(instance)

        assert await DBEmailAccount.filter(owner_instance_key=INSTANCE).count() == 0
        assert await DBEmail.filter(owner_instance_key=INSTANCE).count() == 0
        assert await DBEmailUserPref.filter(instance_key=INSTANCE).count() == 0
        assert await DBEmailOutbox.filter(instance_key=INSTANCE).count() == 0
        assert await DBEmailSendLog.filter(instance_key=INSTANCE).count() == 0
        assert await DBEmailAccount.filter(owner_instance_key=OTHER_INSTANCE).count() == 1

    async def test_counts_include_the_mailbox_rows(self, db, fake_adapters) -> None:
        instance = await create_instance()
        await create_account()

        counts = await instance_manager.purge_instance(instance)

        assert counts["email_account"] == 1

    async def test_attachment_dir_of_the_instance_form_is_removed(
        self, db, fake_adapters, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance = await create_instance()
        row = await create_account(own_instance_dir=True)
        target = tmp_path / "email_attachment" / row.attachment_dir
        target.mkdir(parents=True, exist_ok=True)
        (target / "invoice.pdf").write_bytes(b"pdf")
        monkeypatch.setattr(mail, "email_attachment_root", lambda: tmp_path / "email_attachment")

        await instance_manager.purge_instance(instance)

        assert not target.exists()

    async def test_another_accounts_instance_dir_is_kept(
        self, db, fake_adapters, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`_i<数字>` 必须正好是这个账户自己的 id：记着别的账户 `_i` 目录名的行，删它不碰那个目录"""
        instance = await create_instance()
        row = await create_account()
        other_dir = mail.new_attachment_dir(row.id + 100)
        row.attachment_dir = other_dir
        await row.save(update_fields=["attachment_dir"])
        target = tmp_path / "email_attachment" / other_dir
        target.mkdir(parents=True, exist_ok=True)
        (target / "invoice.pdf").write_bytes(b"pdf")
        monkeypatch.setattr(mail, "email_attachment_root", lambda: tmp_path / "email_attachment")

        await instance_manager.purge_instance(instance)

        assert (target / "invoice.pdf").exists()

    async def test_a_directory_named_after_another_address_is_kept(
        self, db, fake_adapters, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """目录名既不是 `_i<数字>`、也不是这个账户自己的地址：哪一种都对不上，一个文件都不动"""
        instance = await create_instance()
        await create_account(attachment_dir="ops@example.test")
        await create_mail(INSTANCE)
        target = tmp_path / "email_attachment" / "ops@example.test"
        (target / MAIL_UID).mkdir(parents=True, exist_ok=True)
        (target / "invoice.pdf").write_bytes(b"pdf")
        monkeypatch.setattr(mail, "email_attachment_root", lambda: tmp_path / "email_attachment")

        await instance_manager.purge_instance(instance)

        assert (target / MAIL_UID).is_dir()
        assert (target / "invoice.pdf").exists()

    async def test_an_inherited_address_directory_loses_only_this_accounts_mails(
        self, db, fake_adapters, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """由平台方「转为实例自有」的账户沿用地址目录：只删它被删掉的那几封的 `<UID>/`，别的不动"""
        instance = await create_instance()
        await create_account(attachment_dir="own@example.test")
        await create_mail(INSTANCE)
        root = tmp_path / "email_attachment"
        own_mail = root / "own@example.test" / MAIL_UID
        own_mail.mkdir(parents=True, exist_ok=True)
        (own_mail / "invoice.pdf").write_bytes(b"pdf")
        # 不是这个账户任何一行缓存的 UID：不知道是谁的，留着
        stray = root / "own@example.test" / STRAY_UID
        stray.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mail, "email_attachment_root", lambda: root)

        await instance_manager.purge_instance(instance)

        assert not own_mail.exists()
        assert stray.is_dir()

    async def test_an_inherited_address_directory_that_ends_up_empty_goes_too(
        self, db, fake_adapters, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instance = await create_instance()
        await create_account(attachment_dir="own@example.test")
        await create_mail(INSTANCE)
        root = tmp_path / "email_attachment"
        (root / "own@example.test" / MAIL_UID).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mail, "email_attachment_root", lambda: root)

        await instance_manager.purge_instance(instance)

        assert not (root / "own@example.test").exists()

    async def test_another_owners_rows_under_the_same_address_are_not_used(
        self, db, fake_adapters, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """UID 只取这个账户归属快照下的行：同一地址下别的归属留下的行（换过主人）不算这个账户的信"""
        instance = await create_instance()
        await create_account(attachment_dir="own@example.test")
        await DBEmail.create(
            account_username="own@example.test",
            email_uid="3003",
            subject="主题",
            sender="noreply@sender.example",
            recipients='["own@example.test"]',
            body_text="正文",
            has_attachments=False,
            attachment_names="[]",
            owner_adapter_key="",
            owner_instance_key="",
        )
        root = tmp_path / "email_attachment"
        platform_era = root / "own@example.test" / "3003"
        platform_era.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mail, "email_attachment_root", lambda: root)

        await instance_manager.purge_instance(instance)

        assert platform_era.is_dir()

    async def test_runtime_clients_are_disconnected(self, db, fake_adapters) -> None:
        instance = await create_instance()
        await create_account()

        await instance_manager.purge_instance(instance)

        assert fake_adapters.removed == ["own@example.test"]

    async def test_platform_share_entry_is_revoked(self, db, platform_config, fake_adapters) -> None:
        import nekro_agent.adapters as adapters_module

        instance = await create_instance()
        config = platform_config(
            [
                EmailAccount(
                    USERNAME="ops@example.test",
                    SEND_ENABLED=True,
                    SEND_SHARE="listed",
                    SEND_SHARE_INSTANCES=[f"{ADAPTER}:{INSTANCE}", f"{ADAPTER}:{OTHER_INSTANCE}"],
                ),
            ],
        )
        adapters_module.loaded_adapters["email"].config = config

        await instance_manager.purge_instance(instance)

        assert config.RECEIVE_ACCOUNTS[0].SEND_SHARE_INSTANCES == [f"{ADAPTER}:{OTHER_INSTANCE}"]
        assert config.dumped == 1


class TestAttachmentDirRule:
    """「能不能删」只有一处判据"""

    def test_only_the_instance_form_is_recognised(self) -> None:
        assert mail.new_attachment_dir(3) == "_i3"
        assert is_instance_attachment_dir("_i3") is True
        assert is_instance_attachment_dir("ops@example.test") is False

    async def test_revoke_still_rewrites_the_config_when_the_adapter_is_absent(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """email 适配器没加载也要改：否则配置文件里留着旧实例名，同名实例重建后自动继承授权"""
        import nekro_agent.adapters as adapters_module

        monkeypatch.delitem(adapters_module.loaded_adapters, "email", raising=False)
        config = platform_config(
            [
                EmailAccount(
                    USERNAME="ops@example.test",
                    SEND_ENABLED=True,
                    SEND_SHARE="listed",
                    SEND_SHARE_INSTANCES=[f"{ADAPTER}:{INSTANCE}"],
                ),
            ],
        )

        assert instance_manager.revoke_platform_share_for_instance(ADAPTER, INSTANCE) == 1
        assert config.RECEIVE_ACCOUNTS[0].SEND_SHARE_INSTANCES == []
        assert config.dumped == 1

    def test_platform_config_falls_back_to_the_config_file_without_an_adapter(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """没有加载的适配器时从 `load_adapter_config` 取（与适配器加载后用的是同一个缓存对象）"""
        import nekro_agent.adapters as adapters_module

        sentinel = SimpleNamespace(RECEIVE_ACCOUNTS=[])
        monkeypatch.delitem(adapters_module.loaded_adapters, "email", raising=False)
        monkeypatch.setattr(adapters_module, "load_adapter_config", lambda key: sentinel if key == "email" else None)

        assert mail._load_platform_config(None) is sentinel  # noqa: SLF001


class TestInstanceMigrationMovesMailboxes:
    """实例改判：五张表跟着走"""

    async def test_rows_move_to_the_target_instance(
        self, db, fake_adapters, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services import chat_key_migration

        await create_instance()
        await create_instance(OTHER_INSTANCE)
        await create_account()
        await create_mail(INSTANCE)
        await DBEmailUserPref.create(
            adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="10001", email="alice@example.test",
        )
        await DBEmailOutbox.create(
            batch_key="b1", adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="10001",
            account_kind=mail.KIND_INSTANCE, account_ref="1", to_address="alice@example.test",
            subject="节日", body="正文", reason="", status="queued", attempts=0,
        )
        await DBEmailSendLog.create(
            account_kind=mail.KIND_INSTANCE, account_ref="1", adapter_key=ADAPTER, instance_key=INSTANCE,
            chat_key="", tenant_id=None, purpose=mail.PURPOSE_FESTIVAL,
            to_address="alice@example.test", subject="节日", reason="", ok=True, error="",
        )
        monkeypatch.setattr(chat_key_migration, "_sync_paragraph_vectors", _no_vector_sync)

        await chat_key_migration.migrate_instance_ownership(ADAPTER, INSTANCE, OTHER_INSTANCE)

        assert await DBEmailAccount.filter(owner_instance_key=OTHER_INSTANCE).count() == 1
        assert await DBEmail.filter(owner_instance_key=OTHER_INSTANCE).count() == 1
        assert await DBEmailUserPref.filter(instance_key=OTHER_INSTANCE).count() == 1
        assert await DBEmailOutbox.filter(instance_key=OTHER_INSTANCE).count() == 1
        assert await DBEmailSendLog.filter(instance_key=OTHER_INSTANCE).count() == 1

    async def test_conflicting_user_pref_is_dropped_not_duplicated(
        self, db, fake_adapters, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """目标已有同一个用户的偏好时保留目标那份：改写会撞唯一键"""
        from nekro_agent.services import chat_key_migration

        await create_instance()
        await create_instance(OTHER_INSTANCE)
        await DBEmailUserPref.create(
            adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="10001", email="old@example.test",
            festival_subscribed=True,
        )
        await DBEmailUserPref.create(
            adapter_key=ADAPTER, instance_key=OTHER_INSTANCE, platform_userid="10001", email="new@example.test",
            festival_subscribed=False,
        )
        monkeypatch.setattr(chat_key_migration, "_sync_paragraph_vectors", _no_vector_sync)

        await chat_key_migration.migrate_instance_ownership(ADAPTER, INSTANCE, OTHER_INSTANCE)

        rows = await DBEmailUserPref.filter(adapter_key=ADAPTER).all()
        assert len(rows) == 1
        assert rows[0].instance_key == OTHER_INSTANCE
        assert rows[0].email == "new@example.test"

    async def test_residual_counts_are_zero_after_migration(
        self, db, fake_adapters, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services import chat_key_migration

        await create_instance()
        await create_instance(OTHER_INSTANCE)
        await create_account()
        monkeypatch.setattr(chat_key_migration, "_sync_paragraph_vectors", _no_vector_sync)

        outcome = await chat_key_migration.migrate_instance_ownership(ADAPTER, INSTANCE, OTHER_INSTANCE)

        assert outcome.residual["email_account"] == 0
        assert outcome.residual["email_cache"] == 0
        assert outcome.residual["email_user_pref"] == 0
        assert outcome.residual["email_outbox"] == 0
        assert outcome.residual["email_send_log"] == 0

    async def test_platform_share_entry_follows_the_instance(
        self, db, platform_config, fake_adapters, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """迁移之后那份授权该跟着到目标实例上，而不是留在一个没人用的 slug 上"""
        import nekro_agent.adapters as adapters_module
        from nekro_agent.services import chat_key_migration

        await create_instance()
        await create_instance(OTHER_INSTANCE)
        config = platform_config(
            [
                EmailAccount(
                    USERNAME="ops@example.test",
                    SEND_ENABLED=True,
                    SEND_SHARE="listed",
                    SEND_SHARE_INSTANCES=[f"{ADAPTER}:{INSTANCE}"],
                ),
            ],
        )
        adapters_module.loaded_adapters["email"].config = config
        monkeypatch.setattr(chat_key_migration, "_sync_paragraph_vectors", _no_vector_sync)

        await chat_key_migration.migrate_instance_ownership(ADAPTER, INSTANCE, OTHER_INSTANCE)

        assert config.RECEIVE_ACCOUNTS[0].SEND_SHARE_INSTANCES == [f"{ADAPTER}:{OTHER_INSTANCE}"]


class TestChatKeyRewrite:
    """换会话标识：引用它的两列要一起改"""

    async def test_notify_and_pref_columns_are_rewritten(self, db, fake_adapters) -> None:
        from nekro_agent.services import chat_key_migration

        await create_instance()
        await create_account()
        await DBEmailAccount.filter(owner_instance_key=INSTANCE).update(notify_chat_key=chat_key_of(INSTANCE))
        await DBEmailUserPref.create(
            adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="10001",
            email="alice@example.test", set_in_chat_key=chat_key_of(INSTANCE),
        )
        new_chat_key = f"{ADAPTER}-{INSTANCE}-group_new"

        await chat_key_migration.migrate_chat_key_references(chat_key_of(INSTANCE), new_chat_key)

        assert (await DBEmailAccount.get(owner_instance_key=INSTANCE)).notify_chat_key == new_chat_key
        assert (await DBEmailUserPref.get(instance_key=INSTANCE)).set_in_chat_key == new_chat_key


class TestTheRuntimeOwnerMapFollows:
    """适配器上「地址 → 归属」那张内存表：改了账户行之后要跟着刷新

    实例改判时的刷新早就有（`refresh_runtime_owners`），这里钉住它；换会话标识时通知频道
    也记在那张表里，以前不刷新，重启之前新邮件通知还推到旧标识上。
    """

    @staticmethod
    def install_owner_map(row: DBEmailAccount) -> dict:
        import nekro_agent.adapters as adapters_module

        mapping = {row.username: mail.instance_owner_of(row)}
        adapters_module.loaded_adapters["email"].instance_account_owner = mapping
        return mapping

    async def test_instance_migration_moves_the_runtime_owner(
        self, db, fake_adapters, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services import chat_key_migration

        await create_instance()
        await create_instance(OTHER_INSTANCE)
        row = await create_account()
        mapping = self.install_owner_map(row)
        monkeypatch.setattr(chat_key_migration, "_sync_paragraph_vectors", _no_vector_sync)

        await chat_key_migration.migrate_instance_ownership(ADAPTER, INSTANCE, OTHER_INSTANCE)

        assert mapping[row.username].instance_key == OTHER_INSTANCE
        assert mapping[row.username].account_id == row.id

    async def test_a_chat_key_rewrite_moves_the_notify_chat(self, db, fake_adapters) -> None:
        from nekro_agent.services import chat_key_migration

        await create_instance()
        row = await create_account()
        await DBEmailAccount.filter(id=row.id).update(notify_chat_key=chat_key_of(INSTANCE))
        mapping = self.install_owner_map(await DBEmailAccount.get(id=row.id))
        new_chat_key = f"{ADAPTER}-{INSTANCE}-group_new"

        await chat_key_migration.migrate_chat_key_references(chat_key_of(INSTANCE), new_chat_key)

        assert mapping[row.username].notify_chat_key == new_chat_key

    async def test_another_instances_notify_chat_is_untouched(self, db, fake_adapters) -> None:
        from nekro_agent.services import chat_key_migration

        await create_instance()
        await create_instance(OTHER_INSTANCE)
        other = await create_account(OTHER_INSTANCE, "other@example.test")
        await DBEmailAccount.filter(id=other.id).update(notify_chat_key=chat_key_of(OTHER_INSTANCE))
        mapping = self.install_owner_map(await DBEmailAccount.get(id=other.id))

        await chat_key_migration.migrate_chat_key_references(chat_key_of(INSTANCE), f"{ADAPTER}-{INSTANCE}-group_new")

        assert mapping[other.username].notify_chat_key == chat_key_of(OTHER_INSTANCE)


class TestInstanceTransferDeletesMailboxes:
    """租户划转：实例自有的邮箱默认删除"""

    async def test_delete_instance_mailboxes_removes_accounts_and_data(
        self, db, platform_config, fake_adapters,
    ) -> None:
        from nekro_agent.routers.console import tenants

        platform_config([])
        await create_instance(tenant_id=ACME_TENANT_ID)
        await create_account()
        await create_mail(INSTANCE)
        await DBEmailUserPref.create(
            adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="10001", email="alice@example.test",
            festival_subscribed=True,
        )
        await DBTenant.create(name="acme", display_name="Acme")
        rows = [(await DBAdapterInstance.get(instance_key=INSTANCE), False)]

        # 只调划转里处理邮箱的那两段：整条路径的其余部分（令牌曝光、工作区标记、额度清理）
        # 各有用例，这里要看的是「邮箱真的被删了」
        await tenants._delete_instance_mailboxes(rows, tenant_name="acme")
        await tenants._clear_instance_email_user_prefs(rows)

        assert await DBEmailAccount.filter(owner_instance_key=INSTANCE).count() == 0
        assert await DBEmail.filter(owner_instance_key=INSTANCE).count() == 0
        assert await DBEmailUserPref.filter(instance_key=INSTANCE).count() == 0

    async def test_clear_drops_the_unsubscribe_flag_too(self, db, platform_config, fake_adapters) -> None:
        """「一并清空」是换主人：新主人名下重新开始，退订标记也不留"""
        from nekro_agent.routers.console import tenants

        platform_config([])
        await create_instance(tenant_id=ACME_TENANT_ID)
        await DBEmailUserPref.create(
            adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="10001",
            email="alice@example.test", unsubscribed=True, festival_subscribed=True,
        )

        await tenants._clear_instance_email_user_prefs([(await DBAdapterInstance.get(instance_key=INSTANCE), False)])

        assert await DBEmailUserPref.filter(instance_key=INSTANCE).count() == 0

    async def test_platform_share_is_revoked_even_when_mailboxes_follow(
        self, db, platform_config, fake_adapters,
    ) -> None:
        """不勾「删自有邮箱」时自有邮箱跟着走，但平台方给前一个主人的点名授权照样取消"""
        import nekro_agent.adapters as adapters_module
        from nekro_agent.routers.console import tenants

        config = platform_config(
            [
                EmailAccount(
                    USERNAME="ops@example.test",
                    SEND_ENABLED=True,
                    SEND_SHARE="listed",
                    SEND_SHARE_INSTANCES=[f"{ADAPTER}:{INSTANCE}", f"{ADAPTER}:{OTHER_INSTANCE}"],
                ),
            ],
        )
        adapters_module.loaded_adapters["email"].config = config
        await create_instance(tenant_id=ACME_TENANT_ID)
        await create_account()

        await tenants._release_instance_mail_grants(
            [(await DBAdapterInstance.get(instance_key=INSTANCE), False)], tenant_name="acme",
        )

        assert config.RECEIVE_ACCOUNTS[0].SEND_SHARE_INSTANCES == [f"{ADAPTER}:{OTHER_INSTANCE}"]
        # 自有邮箱没被这一步碰
        assert await DBEmailAccount.filter(owner_instance_key=INSTANCE).count() == 1

    async def test_unsent_festival_mail_is_voided_on_transfer(self, db, platform_config, fake_adapters) -> None:
        from nekro_agent.routers.console import tenants

        platform_config([])
        await create_instance(tenant_id=ACME_TENANT_ID)
        for status in ("queued", "paused", "sent"):
            await DBEmailOutbox.create(
                batch_key=f"b-{status}", adapter_key=ADAPTER, instance_key=INSTANCE, platform_userid="10001",
                account_kind=mail.KIND_PLATFORM, account_ref="abc", to_address="alice@example.test",
                subject="节日", body="正文" if status != "sent" else "", reason="", status=status, attempts=0,
            )

        await tenants._release_instance_mail_grants(
            [(await DBAdapterInstance.get(instance_key=INSTANCE), False)], tenant_name="acme",
        )

        rows = {row.batch_key: row for row in await DBEmailOutbox.filter(instance_key=INSTANCE)}
        assert rows["b-queued"].status == "skipped" and rows["b-queued"].body == ""
        assert rows["b-paused"].status == "skipped" and rows["b-paused"].body == ""
        assert rows["b-sent"].status == "sent"

    async def test_bind_releases_grants_without_the_delete_checkbox(self) -> None:
        """接线：`bind_tenant_instances` 里收回授权那一步不在 `delete_instance_mailboxes` 的分支里"""
        import ast
        import inspect

        from nekro_agent.routers.console import tenants

        tree = ast.parse(inspect.getsource(tenants.bind_tenant_instances).lstrip())
        calls_under_if = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                        calls_under_if.add(inner.func.id)
        all_calls = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "_release_instance_mail_grants" in all_calls
        assert "_release_instance_mail_grants" not in calls_under_if

    async def test_attachment_dir_is_removed_without_a_loaded_adapter(
        self, db, platform_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """附件目录与运行时客户端是两件事：适配器没加载时目录照删"""
        import nekro_agent.adapters as adapters_module
        from nekro_agent.routers.console import tenants

        monkeypatch.delitem(adapters_module.loaded_adapters, "email", raising=False)
        platform_config([])
        await create_instance(tenant_id=ACME_TENANT_ID)
        row = await create_account(own_instance_dir=True)
        target = tmp_path / "email_attachment" / row.attachment_dir
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mail, "email_attachment_root", lambda: tmp_path / "email_attachment")

        await tenants._delete_instance_mailboxes(
            [(await DBAdapterInstance.get(instance_key=INSTANCE), False)], tenant_name="acme",
        )

        assert not target.exists()
        assert await DBEmailAccount.filter(owner_instance_key=INSTANCE).count() == 0

    async def test_attachment_dir_is_removed_with_the_account(
        self, db, platform_config, fake_adapters, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.routers.console import tenants

        platform_config([])
        await create_instance(tenant_id=ACME_TENANT_ID)
        row = await create_account(own_instance_dir=True)
        target = tmp_path / "email_attachment" / row.attachment_dir
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mail, "email_attachment_root", lambda: tmp_path / "email_attachment")

        await tenants._delete_instance_mailboxes(
            [(await DBAdapterInstance.get(instance_key=INSTANCE), False)], tenant_name="acme",
        )

        assert not target.exists()

    async def test_an_inherited_address_directory_loses_the_deleted_mails(
        self, db, platform_config, fake_adapters, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """划转删箱与彻底删除同一套删法：地址目录逐封删，只删这个账户被删掉的那几封"""
        from nekro_agent.routers.console import tenants

        platform_config([])
        await create_instance(tenant_id=ACME_TENANT_ID)
        await create_account(attachment_dir="own@example.test")
        await create_mail(INSTANCE)
        root = tmp_path / "email_attachment"
        own_mail = root / "own@example.test" / MAIL_UID
        own_mail.mkdir(parents=True, exist_ok=True)
        stray = root / "own@example.test" / STRAY_UID
        stray.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mail, "email_attachment_root", lambda: root)

        await tenants._delete_instance_mailboxes(
            [(await DBAdapterInstance.get(instance_key=INSTANCE), False)], tenant_name="acme",
        )

        assert not own_mail.exists()
        assert stray.is_dir()
        assert await DBEmail.filter(owner_instance_key=INSTANCE).count() == 0


async def _no_vector_sync(_paragraph_ids: Any, _instance_key: Any, _new_chat_key: Any = None) -> int:
    """顶掉 Qdrant 同步：它返回的是「同步了几条」，计数会直接进结果，所以不能返回 None"""
    return 0
