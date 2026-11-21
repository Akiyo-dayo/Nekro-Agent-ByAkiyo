"""`/api/instance-email/*`：实例自有邮箱的后台接口

## 为什么这些端点要单独钉一遍

它们有一条**看起来顺理成章、实际是越权**的捷径：`adapter_key` + `instance_key` 都是
查询参数，直接拿来查表就会把「别人的实例」当成「我的实例」。三类错误都不会报错：

1. 少一个参数时**默认**成平台方或空串 → 一次失败的请求变成一次看到别人数据的请求；
2. 按 ID 取账户时不判归属 → 换一个数字就能读到别的实例的账户（含 `HAS_PASSWORD`）；
3. 凭据回显 → 授权码进了浏览器历史、日志、前端的状态树。

所以这里全部按「公开接口」测：只依赖端点函数的入参与身份，不碰内部实现。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_email_user_pref import DBEmailUserPref
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.instance_email import (
    InstanceEmailAccountPayload,
    create_instance_email_account,
    delete_instance_email_account,
    delete_user_pref,
    list_instance_email_accounts,
    list_platform_accounts_for_instance,
    list_providers,
    list_send_logs,
    list_user_prefs,
    update_instance_email_account,
)
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"
PLATFORM_INSTANCE = "plat1"

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

INSTANCE_LAYOUT = [
    (OWN_INSTANCE, ACME_TENANT_ID),
    (FOREIGN_INSTANCE, RIVAL_TENANT_ID),
    (PLATFORM_INSTANCE, None),
]


def chat_key_of(instance_key: str) -> str:
    return f"{ADAPTER}-{instance_key}-group_{instance_key}"


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}-{perm_level}",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def acme_admin() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def rival_admin() -> DBUser:
    return make_user(tenant_id=RIVAL_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.clear_default_sender_cache()
    try:
        for instance_key, tenant_id in INSTANCE_LAYOUT:
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
                instance_key=instance_key,
                channel_id=f"group_{instance_key}",
                chat_key=chat_key_of(instance_key),
                channel_name=f"{instance_key} 的群",
                channel_type="group",
                data="{}",
                is_active=True,
            )
        yield
    finally:
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


@pytest.fixture
def platform_config(monkeypatch: pytest.MonkeyPatch):
    class FakeConfig:
        RECEIVE_ACCOUNTS: list[EmailAccount] = []

    def install(accounts: list[EmailAccount]) -> None:
        config = FakeConfig()
        config.RECEIVE_ACCOUNTS = accounts
        monkeypatch.setattr(mail, "_load_platform_config", lambda adapter=None: config)

    return install


@pytest.fixture
def no_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """运行时适配器不存在时端点也不该失败：账户表本身还能维护"""

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(mail, "sync_runtime_account", noop)
    monkeypatch.setattr(mail, "remove_runtime_account", noop)


def payload(**overrides: Any) -> InstanceEmailAccountPayload:
    values: dict[str, Any] = {
        "username": "own@example.test",
        "password": "authorization-code",
        "display_name": "本实例",
        "provider": "自定义",
        "settings": {
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "smtp_host": "smtp.example.test",
            "smtp_port": 465,
            "smtp_ssl_port": 465,
            "smtp_use_ssl": True,
        },
        "enabled": True,
        "receive_enabled": True,
        "send_enabled": True,
        "is_default_sender": False,
        "notify_chat_key": "",
    }
    values.update(overrides)
    return InstanceEmailAccountPayload(**values)


async def add_account(
    instance_key: str = OWN_INSTANCE,
    username: str = "own@example.test",
) -> DBEmailAccount:
    return await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
        username=username,
        display_name="",
        provider="自定义",
        settings={},
        password="secret",
        send_enabled=True,
        attachment_dir="",
    )


class TestScopeIsRequired:
    """两个参数缺一不可，且必须是自己的实例"""

    async def test_missing_instance_key_is_a_validation_error(self, db) -> None:
        with pytest.raises(ValidationError):
            await list_instance_email_accounts(ADAPTER, "", _current_user=platform_owner())

    async def test_missing_adapter_key_is_a_validation_error(self, db) -> None:
        with pytest.raises(ValidationError):
            await list_instance_email_accounts("", OWN_INSTANCE, _current_user=platform_owner())

    async def test_another_tenants_instance_is_not_found(self, db) -> None:
        """越权与不存在同一个 404：换一个标识试不出「这个实例存在」"""
        with pytest.raises(NotFoundError):
            await list_instance_email_accounts(ADAPTER, FOREIGN_INSTANCE, _current_user=acme_admin())

    async def test_unknown_instance_is_not_found(self, db) -> None:
        with pytest.raises(NotFoundError):
            await list_instance_email_accounts(ADAPTER, "ghost", _current_user=acme_admin())

    async def test_platform_owner_reaches_any_instance(self, db) -> None:
        await add_account(FOREIGN_INSTANCE, "rival@example.test")

        rows = await list_instance_email_accounts(ADAPTER, FOREIGN_INSTANCE, _current_user=platform_owner())

        assert [row.username for row in rows] == ["rival@example.test"]


class TestCredentialIsWriteOnly:
    """凭据只写不读"""

    async def test_list_reports_has_password_instead_of_the_password(self, db, no_runtime) -> None:
        await add_account()

        rows = await list_instance_email_accounts(ADAPTER, OWN_INSTANCE, _current_user=acme_admin())
        dumped = rows[0].model_dump()

        assert dumped["HAS_PASSWORD"] is True
        assert "password" not in dumped
        assert "secret" not in str(dumped)

    async def test_create_returns_has_password(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_address_available", ok)
        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", ok)

        created = await create_instance_email_account(
            payload(), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
        )

        assert created.HAS_PASSWORD is True
        assert "password" not in created.model_dump()

    async def test_empty_password_on_update_keeps_the_stored_one(
        self, db, no_runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """编辑显示名不该把授权码清掉"""
        row = await add_account()
        # 主机与服务商不变，只改显示名
        await DBEmailAccount.filter(id=row.id).update(settings=dict(payload().settings))

        async def ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", ok)
        await update_instance_email_account(
            row.id, payload(password="", display_name="改了名字"), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
        )

        assert (await DBEmailAccount.get(id=row.id)).password == "secret"

    @pytest.mark.parametrize(
        "change",
        [
            {"provider": "QQ邮箱", "settings": {}},
            {"settings": {"imap_host": "imap.attacker.example", "imap_port": 993, "smtp_host": "smtp.example.test", "smtp_port": 465, "smtp_ssl_port": 465, "smtp_use_ssl": True}},
            {"settings": {"imap_host": "imap.example.test", "imap_port": 993, "smtp_host": "smtp.attacker.example", "smtp_port": 465, "smtp_ssl_port": 465, "smtp_use_ssl": True}},
        ],
    )
    async def test_changing_the_server_requires_the_password_again(
        self, db, no_runtime, monkeypatch: pytest.MonkeyPatch, change: dict,
    ) -> None:
        """平台方转来的账户，授权码是平台方填的：把主机改成自己的服务器再点「测试」就能截到它"""
        row = await add_account()
        await DBEmailAccount.filter(id=row.id).update(settings=dict(payload().settings))

        async def ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", ok)
        with pytest.raises(ValidationError, match="重新填写授权码"):
            await update_instance_email_account(
                row.id, payload(password="", **change), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )
        stored = await DBEmailAccount.get(id=row.id)
        assert stored.provider == "自定义"
        assert stored.settings == dict(payload().settings)

        # 重新填了授权码就可以改
        await update_instance_email_account(
            row.id, payload(password="new-code", **change), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
        )
        assert (await DBEmailAccount.get(id=row.id)).password == "new-code"

    async def test_the_address_cannot_be_renamed(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        """地址是缓存、连接池、附件目录的键：换地址请删掉重建"""
        row = await add_account()

        async def ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", ok)
        with pytest.raises(ValidationError, match="不能修改"):
            await update_instance_email_account(
                row.id, payload(username="renamed@example.test", password="x"), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )
        assert (await DBEmailAccount.get(id=row.id)).username == "own@example.test"


class TestAddressAndHostValidation:
    """地址唯一与自定义主机校验在后台接口上也确实接上了"""

    async def test_conflicting_address_is_refused(self, db, no_runtime) -> None:
        await add_account(FOREIGN_INSTANCE, "taken@example.test")

        with pytest.raises(ValidationError, match=mail.ADDRESS_UNAVAILABLE):
            await create_instance_email_account(
                payload(username="taken@example.test"), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )

    async def test_custom_host_is_validated(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError(mail.CUSTOM_HOST_UNAVAILABLE)

        async def address_ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_address_available", address_ok)
        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", boom)

        with pytest.raises(ValidationError, match=mail.CUSTOM_HOST_UNAVAILABLE):
            await create_instance_email_account(
                payload(), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )

    async def test_preset_provider_skips_the_host_check(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        """预设的主机是平台方写死的常量，不是租户填的"""
        called: list[Any] = []

        async def boom(*args: Any, **_kwargs: Any) -> None:
            called.append(args)

        async def address_ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_address_available", address_ok)
        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", boom)

        await create_instance_email_account(
            payload(provider="QQ邮箱", settings={}), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
        )

        assert called == []

    @pytest.mark.parametrize("address", ["../x@example.test", "a b@example.test", "two@example.test,three@example.test"])
    async def test_a_malformed_address_is_refused(self, db, no_runtime, address: str) -> None:
        """地址会成为缓存与连接池的键、转回平台方时还会成为目录名：不是一个正常地址就不收"""
        with pytest.raises(ValidationError):
            await create_instance_email_account(
                payload(username=address, provider="QQ邮箱", settings={}), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )
        assert await DBEmailAccount.filter(owner_instance_key=OWN_INSTANCE).count() == 0

    async def test_unknown_provider_is_refused(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        """不认识的服务商键不能当成「免校验的预设」放行：运行时会拿它的 settings 主机去连"""
        checked: list[Any] = []

        async def record(*args: Any, **_kwargs: Any) -> None:
            checked.append(args)

        async def address_ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_address_available", address_ok)
        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", record)

        with pytest.raises(ValidationError, match="不认识的邮箱服务商"):
            await create_instance_email_account(
                payload(provider="not-a-preset"), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )
        assert await DBEmailAccount.filter(owner_instance_key=OWN_INSTANCE).count() == 0

        row = await add_account()
        with pytest.raises(ValidationError, match="不认识的邮箱服务商"):
            await update_instance_email_account(
                row.id, payload(provider="not-a-preset"), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )
        assert (await DBEmailAccount.get(id=row.id)).provider == "自定义"

    async def test_every_custom_port_is_checked(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        """SSL 端口也要过白名单：发信客户端优先走它"""
        checked: list[tuple[str, int]] = []

        async def record(host: str, port: int) -> None:
            checked.append((host, port))
            if port not in mail.CUSTOM_MAIL_PORTS:
                raise ValueError(mail.CUSTOM_HOST_UNAVAILABLE)

        async def address_ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_address_available", address_ok)
        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", record)
        settings = {
            "imap_host": "imap.example.test",
            "imap_port": 993,
            "smtp_host": "smtp.example.test",
            "smtp_port": 587,
            "smtp_ssl_port": 6379,
            "smtp_use_ssl": True,
        }

        with pytest.raises(ValidationError, match=mail.CUSTOM_HOST_UNAVAILABLE):
            await create_instance_email_account(
                payload(settings=settings), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )
        assert ("smtp.example.test", 6379) in checked

    async def test_missing_custom_hosts_are_refused(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        async def address_ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_address_available", address_ok)

        with pytest.raises(ValidationError, match=mail.CUSTOM_HOST_UNAVAILABLE):
            await create_instance_email_account(
                payload(settings={"imap_host": "imap.example.test"}), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )

    async def test_no_password_on_create_is_refused(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_address_available", ok)
        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", ok)

        with pytest.raises(ValidationError, match="授权码"):
            await create_instance_email_account(
                payload(password=""), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )


class TestNotifyChatKey:
    """通知频道必须属于这个实例"""

    async def test_own_chat_key_is_accepted(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_address_available", ok)
        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", ok)

        created = await create_instance_email_account(
            payload(notify_chat_key=chat_key_of(OWN_INSTANCE)), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
        )

        assert created.notify_chat_key == chat_key_of(OWN_INSTANCE)

    async def test_another_instances_chat_key_is_refused(
        self, db, no_runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_address_available", ok)
        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", ok)

        with pytest.raises(ValidationError, match="通知频道"):
            await create_instance_email_account(
                payload(notify_chat_key=chat_key_of(FOREIGN_INSTANCE)),
                ADAPTER,
                OWN_INSTANCE,
                _current_user=acme_admin(),
            )


class TestByIDEndpointsJudgeOwnership:
    """按 ID 访问一律先取行再判归属"""

    async def test_update_of_another_instances_account_is_not_found(self, db, no_runtime) -> None:
        row = await add_account(FOREIGN_INSTANCE, "rival@example.test")

        with pytest.raises(NotFoundError):
            await update_instance_email_account(
                row.id, payload(username="rival@example.test"), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )

    async def test_delete_of_another_instances_account_is_not_found(self, db, no_runtime) -> None:
        row = await add_account(FOREIGN_INSTANCE, "rival@example.test")

        with pytest.raises(NotFoundError):
            await delete_instance_email_account(row.id, ADAPTER, OWN_INSTANCE, _current_user=acme_admin())

        assert await DBEmailAccount.filter(id=row.id).exists()

    async def test_delete_of_a_missing_account_is_the_same_error(self, db, no_runtime) -> None:
        with pytest.raises(NotFoundError):
            await delete_instance_email_account(999, ADAPTER, OWN_INSTANCE, _current_user=acme_admin())


class TestDeletingAnInheritedMailbox:
    """由平台方「转为实例自有」的账户沿用地址目录：删账户时逐封删它自己的那几封"""

    async def test_only_the_deleted_mails_leave_the_address_directory(
        self, db, no_runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.models.db_email import DBEmail

        row = await add_account()
        row.attachment_dir = "own@example.test"
        await row.save()
        await DBEmail.create(
            account_username="own@example.test",
            email_uid="1001",
            subject="主题",
            sender="noreply@sender.example",
            recipients='["own@example.test"]',
            body_text="正文",
            has_attachments=True,
            attachment_names='["a.pdf"]',
            owner_adapter_key=ADAPTER,
            owner_instance_key=OWN_INSTANCE,
        )
        root = tmp_path / "email_attachment"
        own_mail = root / "own@example.test" / "1001"
        own_mail.mkdir(parents=True)
        (own_mail / "a.pdf").write_bytes(b"pdf")
        stray = root / "own@example.test" / "2002"
        stray.mkdir(parents=True)
        monkeypatch.setattr(mail, "email_attachment_root", lambda: root)

        await delete_instance_email_account(row.id, ADAPTER, OWN_INSTANCE, _current_user=acme_admin())

        assert not own_mail.exists()
        assert stray.is_dir()
        assert await DBEmail.filter(account_username="own@example.test").count() == 0


class TestDefaultSenderIsExclusiveOnSave:
    """保存时只留一个默认发件人"""

    async def test_setting_a_new_default_clears_the_previous(
        self, db, no_runtime, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = await add_account(OWN_INSTANCE, "first@example.test")
        second = await add_account(OWN_INSTANCE, "second@example.test")
        await DBEmailAccount.filter(id=first.id).update(is_default_sender=True)

        async def ok(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(mail, "assert_custom_mail_host_allowed", ok)
        await update_instance_email_account(
            second.id,
            payload(username="second@example.test", is_default_sender=True),
            ADAPTER,
            OWN_INSTANCE,
            _current_user=acme_admin(),
        )

        assert (await DBEmailAccount.get(id=first.id)).is_default_sender is False
        assert (await DBEmailAccount.get(id=second.id)).is_default_sender is True


class TestPlatformAccountsAreNotLeaked:
    """平台方账户只给显示名、引用与是否默认"""

    async def test_only_labels_and_refs_are_returned(self, db, platform_config) -> None:
        platform_config([EmailAccount(USERNAME="ops@example.test", DISPLAY_NAME="通知", SEND_ENABLED=True)])

        rows = await list_platform_accounts_for_instance(
            ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
        )
        dumped = str([row.model_dump() for row in rows])

        assert "ops@example.test" not in dumped
        assert rows[0].display_label == "平台方提供：通知"
        assert rows[0].account_ref.startswith("platform:")

    async def test_platform_instance_sees_the_real_name(self, db, platform_config) -> None:
        platform_config([EmailAccount(USERNAME="ops@example.test", DISPLAY_NAME="通知", SEND_ENABLED=True)])

        rows = await list_platform_accounts_for_instance(
            ADAPTER, PLATFORM_INSTANCE, _current_user=platform_owner(),
        )

        assert rows[0].display_label == "通知"

    async def test_platform_owner_viewing_a_tenant_instance_sees_the_share_setting(
        self, db, platform_config,
    ) -> None:
        """「是不是平台方实例」看实例归谁，不看谁在看：页面要与会话里实际能用的一致"""
        platform_config(
            [
                EmailAccount(USERNAME="shared@example.test", DISPLAY_NAME="共享", SEND_ENABLED=True),
                EmailAccount(USERNAME="private@example.test", DISPLAY_NAME="不共享", SEND_ENABLED=True, SEND_SHARE="none"),
            ],
        )

        rows = await list_platform_accounts_for_instance(
            ADAPTER, OWN_INSTANCE, _current_user=platform_owner(),
        )

        assert [row.display_label for row in rows] == ["共享"]


class TestSendLogs:
    """发信记录：租户只看自己租户的，平台方账户那几行不给地址"""

    async def test_tenant_sees_only_its_own_tenant_rows(self, db, platform_config, monkeypatch: pytest.MonkeyPatch) -> None:
        platform_config([])
        monkeypatch.setattr(mail, "_clock", lambda: 1000.0)
        monkeypatch.setattr(mail, "_wall_clock", lambda: __import__("tortoise").timezone.now())
        mail.reset_send_log_state()
        view = mail.build_account_view(
            kind=mail.KIND_INSTANCE, username="own@example.test", display_name="", send_enabled=True, readable=True,
            instance_account_id=1,
        )
        for tenant_id, subject in ((ACME_TENANT_ID, "自己的"), (RIVAL_TENANT_ID, "别人的")):
            await mail.record_send(
                account=view,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                chat_key="",
                tenant_id=tenant_id,
                purpose=mail.PURPOSE_SEND_EMAIL,
                to_address="someone@example.test",
                subject=subject,
                reason="",
                ok=True,
            )

        result = await list_send_logs(ADAPTER, OWN_INSTANCE, 0, 50, _current_user=acme_admin())

        assert [item["subject"] for item in result["items"]] == ["自己的"]

    async def test_platform_account_rows_hide_the_label_for_tenants(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        platform_config([EmailAccount(USERNAME="ops@example.test", DISPLAY_NAME="通知", SEND_ENABLED=True)])
        monkeypatch.setattr(mail, "_clock", lambda: 1000.0)
        monkeypatch.setattr(mail, "_wall_clock", lambda: __import__("tortoise").timezone.now())
        mail.reset_send_log_state()
        view = mail.build_account_view(
            kind=mail.KIND_PLATFORM, username="ops@example.test", display_name="通知", send_enabled=True, readable=False,
        )
        await mail.record_send(
            account=view,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            chat_key="",
            tenant_id=ACME_TENANT_ID,
            purpose=mail.PURPOSE_SEND_EMAIL,
            to_address="someone@example.test",
            subject="主题",
            reason="",
            ok=True,
        )

        result = await list_send_logs(ADAPTER, OWN_INSTANCE, 0, 50, _current_user=acme_admin())

        assert result["items"][0]["display_label"] == "平台方提供：通知"
        assert "ops@example.test" not in str(result)

    async def test_platform_account_errors_are_blank_for_tenants(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """原始 SMTP 报错里带平台方的地址与服务器：租户只看到「失败」，平台方看得到原文"""
        platform_config([EmailAccount(USERNAME="ops@example.test", DISPLAY_NAME="通知", SEND_ENABLED=True)])
        monkeypatch.setattr(mail, "_clock", lambda: 1000.0)
        monkeypatch.setattr(mail, "_wall_clock", lambda: __import__("tortoise").timezone.now())
        mail.reset_send_log_state()
        view = mail.build_account_view(
            kind=mail.KIND_PLATFORM, username="ops@example.test", display_name="通知", send_enabled=True, readable=False,
        )
        await mail.record_send(
            account=view,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            chat_key="",
            tenant_id=ACME_TENANT_ID,
            purpose=mail.PURPOSE_SEND_EMAIL,
            to_address="someone@example.test",
            subject="主题",
            reason="r",
            ok=False,
            error="535 auth failed for ops@example.test on smtp.ops.example",
        )

        tenant_view = await list_send_logs(ADAPTER, OWN_INSTANCE, 0, 50, _current_user=acme_admin())
        platform_view = await list_send_logs(ADAPTER, OWN_INSTANCE, 0, 50, _current_user=platform_owner())

        assert tenant_view["items"][0]["ok"] is False
        assert tenant_view["items"][0]["error"] == ""
        assert "ops@example.test" not in str(tenant_view)
        assert "535" in platform_view["items"][0]["error"]

    @staticmethod
    async def _record_own_failure(error: str) -> None:
        view = mail.build_account_view(
            kind=mail.KIND_INSTANCE, username="own@example.test", display_name="", send_enabled=True, readable=True,
            instance_account_id=1,
        )
        await mail.record_send(
            account=view,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            chat_key="",
            tenant_id=ACME_TENANT_ID,
            purpose=mail.PURPOSE_SEND_EMAIL,
            to_address="someone@example.test",
            subject="主题",
            reason="r",
            ok=False,
            error=error,
        )

    async def test_an_old_raw_error_on_an_own_row_is_replaced_for_tenants(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """旧版本把原始异常写进了实例自有邮箱的记录（可能带对端地址、分得出被拒与超时）：读出来时再收一次"""
        platform_config([])
        monkeypatch.setattr(mail, "_clock", lambda: 1000.0)
        monkeypatch.setattr(mail, "_wall_clock", lambda: __import__("tortoise").timezone.now())
        mail.reset_send_log_state()
        internal = ".".join(("10", "0", "0", "5"))
        await self._record_own_failure(f"[Errno 111] Connection refused to {internal}:25")

        tenant_view = await list_send_logs(ADAPTER, OWN_INSTANCE, 0, 50, _current_user=acme_admin())
        platform_view = await list_send_logs(ADAPTER, OWN_INSTANCE, 0, 50, _current_user=platform_owner())

        assert tenant_view["items"][0]["error"] == mail.STORED_SEND_ERROR_FALLBACK
        assert internal not in str(tenant_view)
        assert internal in platform_view["items"][0]["error"], "平台方照旧看原文"

    @pytest.mark.parametrize(
        "error",
        [mail.MAIL_FAILURE_TEXTS[mail.MAIL_FAILURE_CONNECT], mail.MAIL_FAILURE_TEXTS[mail.MAIL_FAILURE_LOGIN], mail.NO_SENDER],
    )
    async def test_a_classified_error_is_shown_as_written(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch, error: str,
    ) -> None:
        platform_config([])
        monkeypatch.setattr(mail, "_clock", lambda: 1000.0)
        monkeypatch.setattr(mail, "_wall_clock", lambda: __import__("tortoise").timezone.now())
        mail.reset_send_log_state()
        await self._record_own_failure(error)

        tenant_view = await list_send_logs(ADAPTER, OWN_INSTANCE, 0, 50, _current_user=acme_admin())

        assert tenant_view["items"][0]["error"] == error

    async def test_a_row_written_before_the_upgrade_keeps_its_label(
        self, db, platform_config, monkeypatch: pytest.MonkeyPatch, tmp_path,
    ) -> None:
        """升级前写下的平台方记录存的是从 JWT 密钥派生的旧版引用：显示名照样翻得出来"""
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.models.db_email_send_log import DBEmailSendLog

        platform_config([EmailAccount(USERNAME="ops@example.test", DISPLAY_NAME="通知", SEND_ENABLED=True)])
        monkeypatch.setattr(mail, "_clock", lambda: 1000.0)
        monkeypatch.setattr(mail, "_wall_clock", lambda: __import__("tortoise").timezone.now())
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", "secret:fixed-for-the-test")
        mail.reset_account_ref_key_cache()
        mail.reset_send_log_state()
        try:
            view = mail.build_account_view(
                kind=mail.KIND_PLATFORM, username="ops@example.test", display_name="通知", send_enabled=True, readable=False,
            )
            await mail.record_send(
                account=view,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                chat_key="",
                tenant_id=ACME_TENANT_ID,
                purpose=mail.PURPOSE_SEND_EMAIL,
                to_address="someone@example.test",
                subject="主题",
                reason="",
                ok=True,
            )
            legacy = mail.legacy_send_log_refs(view)[0]
            assert legacy != mail.account_send_log_ref(view), "前提：新旧两版引用不同"
            await DBEmailSendLog.all().update(account_ref=legacy)

            result = await list_send_logs(ADAPTER, OWN_INSTANCE, 0, 50, _current_user=acme_admin())

            assert result["items"][0]["display_label"] == "平台方提供：通知"
        finally:
            mail.reset_account_ref_key_cache()


class TestUserPrefs:
    """用户邮件偏好：地址遮掩、删除只清地址与订阅"""

    async def test_address_is_masked(self, db) -> None:
        await DBEmailUserPref.create(
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            platform_userid="10001",
            email="alice@example.test",
            festival_subscribed=True,
            set_in_chat_key=chat_key_of(OWN_INSTANCE),
        )

        result = await list_user_prefs(ADAPTER, OWN_INSTANCE, 0, 50, _current_user=acme_admin())

        assert result["items"][0]["email"] == "a***@example.test"

    async def test_delete_clears_address_and_subscription_but_keeps_the_unsubscribe_flag(self, db) -> None:
        """删一行偏好不该把这个人重新变成默认收件人"""
        row = await DBEmailUserPref.create(
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            platform_userid="10001",
            email="alice@example.test",
            festival_subscribed=True,
            unsubscribed=True,
            set_in_chat_key=chat_key_of(OWN_INSTANCE),
        )

        result = await delete_user_pref(row.id, ADAPTER, OWN_INSTANCE, _current_user=acme_admin())

        refreshed = await DBEmailUserPref.get(id=row.id)
        assert refreshed.email == ""
        assert refreshed.festival_subscribed is False
        assert refreshed.unsubscribed is True
        assert result["unsubscribed"] is True

    async def test_delete_of_another_instances_pref_is_not_found(self, db) -> None:
        row = await DBEmailUserPref.create(
            adapter_key=ADAPTER,
            instance_key=FOREIGN_INSTANCE,
            platform_userid="20002",
            email="bob@example.test",
        )

        with pytest.raises(NotFoundError):
            await delete_user_pref(row.id, ADAPTER, OWN_INSTANCE, _current_user=acme_admin())


class TestProviders:
    """服务商预设是一份编译进代码的清单，人人拿到的都一样"""

    async def test_presets_carry_hosts_and_ports(self, db) -> None:
        presets = await list_providers(_current_user=acme_admin())
        keys = {preset["key"] for preset in presets}

        assert {"QQ邮箱", "163邮箱", "Gmail", "Outlook", "自定义"} <= keys
        for preset in presets:
            if preset["is_custom"]:
                continue
            assert preset["imap_host"]
            assert preset["smtp_host"]
            assert int(preset["imap_port"]) > 0

    async def test_custom_entry_is_marked(self, db) -> None:
        presets = await list_providers(_current_user=acme_admin())
        custom = [preset for preset in presets if preset["is_custom"]]

        assert len(custom) == 1
        assert custom[0]["key"] == "自定义"


class TestQuotaIsEnforcedOnCreate:
    async def test_create_is_refused_at_the_limit(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mail, "instance_account_quota", lambda: 1)
        await add_account(OWN_INSTANCE, "first@example.test")

        with pytest.raises(ValidationError, match="最多"):
            await create_instance_email_account(
                payload(username="second@example.test"), ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
            )


class TestTestEndpointDoesNotLeakHostPaths:
    """测试连接失败时只回类型与半句消息"""

    async def test_failure_message_stays_short(self, db, no_runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.routers import instance_email

        row = await add_account()

        async def boom(_account: Any) -> str:
            raise OSError("C:\\very\\long\\path\\to\\something\\on\\the\\host")

        monkeypatch.setattr(instance_email, "_test_smtp_connection", boom)

        result = await instance_email.test_instance_email_account(
            row.id, ADAPTER, OWN_INSTANCE, _current_user=acme_admin(),
        )

        assert result["success"] is False
        assert len(result["message"]) <= 300
        settings = dict((await DBEmailAccount.get(id=row.id)).settings or {})
        assert settings["last_test_success"] is False
