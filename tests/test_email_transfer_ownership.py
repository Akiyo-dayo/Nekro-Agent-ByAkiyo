"""两个方向的所有权转移：平台方 → 实例，实例 → 平台方

## 这条要防的是什么

「把一个邮箱换给谁」是本轮里唯一**同时改配置、改表、改缓存行、改磁盘目录**的动作。
它最容易出的错不是报错，而是**只改了一半**：

- 表里建了行、配置里还留着 → 同一个地址两边都有，客户端池与附件目录分不清是谁的；
- 缓存行的快照没改 → 搬过去之后那封信对两个主人都不出现；
- 附件目录没跟着改 → 地址变了、文件还在旧目录，页面上附件全 404，而且再也找不回来。

所以这里钉的是「一个事务 / 全局唯一 / 目录跟着走 / 拒绝不该转的东西」，两侧各一组。

两个方向的门槛也不一样：`to-instance` 挂平台方门槛（它动的是平台方的 `RECEIVE_ACCOUNTS`），
`to-platform` 显式再判一次平台方身份（租户 403，而不是 404——这是能力边界，不是资源归属）。
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.email.config import EmailAccount
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import instance_email as routes
from nekro_agent.routers.instance_email import transfer_instance_account_to_platform
from nekro_agent.schemas.errors import PermissionDeniedError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
TENANT_ID = 1
ADDRESS = "shared@example.test"
UID = "4401"


def platform_owner() -> DBUser:
    return DBUser(
        username="owner",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def tenant_admin() -> DBUser:
    return DBUser(
        username="acme-admin",
        password="",
        adapter_key="",
        platform_userid="0",
        perm_level=Role.Admin,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


@pytest.fixture
async def db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from nekro_agent.core import os_env

    monkeypatch.setattr(os_env.OsEnv, "DATA_DIR", str(tmp_path), raising=False)
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.clear_default_sender_cache()
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=OWN_INSTANCE,
        display_name=OWN_INSTANCE,
        status="online",
        provider="remote_onebot",
        tenant_id=TENANT_ID,
        metadata_json="",
    )
    try:
        yield tmp_path
    finally:
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


class FakePlatformConfig:
    """平台方配置的替身：`dump_config` 只记调用次数"""

    def __init__(self, accounts: Optional[List[EmailAccount]] = None) -> None:
        self.RECEIVE_ACCOUNTS = list(accounts or [])
        self.dumps = 0

    def dump_config(self) -> None:
        self.dumps += 1


class FakeAdapter:
    """够两个端点用：配置对象 + 记下被同步过的账户"""

    def __init__(self, config: FakePlatformConfig) -> None:
        self.config = config
        self.instance_account_owner: Dict[str, Any] = {}
        self.synced: List[EmailAccount] = []
        self.removed: List[EmailAccount] = []


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch):
    config = FakePlatformConfig()

    def install(accounts: Optional[List[EmailAccount]] = None) -> FakeAdapter:
        config.RECEIVE_ACCOUNTS = list(accounts or [])
        adapter = FakeAdapter(config)

        async def sync(_adapter: Any, account: EmailAccount, *, owner: Any = None) -> None:
            adapter.synced.append(account)
            if owner is not None:
                adapter.instance_account_owner[account.USERNAME] = owner
            else:
                adapter.instance_account_owner.pop(account.USERNAME, None)

        async def remove(_adapter: Any, account: EmailAccount) -> None:
            adapter.removed.append(account)

        monkeypatch.setattr(routes, "_get_email_adapter", lambda: adapter)
        monkeypatch.setattr("nekro_agent.adapters.email.routers.get_email_adapter", lambda: adapter)
        monkeypatch.setattr(mail, "sync_runtime_account", sync)
        monkeypatch.setattr(mail, "remove_runtime_account", remove)
        return adapter

    return install


def platform_account(**overrides: Any) -> EmailAccount:
    values: Dict[str, Any] = {
        "DISPLAY_NAME": "共享邮箱",
        "EMAIL_ACCOUNT": "QQ邮箱",
        "USERNAME": ADDRESS,
        "PASSWORD": "authorization-code",
        "ENABLED": True,
        "RECEIVE_ENABLED": True,
        "SEND_ENABLED": True,
        "AUTH_TYPE": "password",
        "TRANSPORT_TYPE": "imap_smtp",
        "CUSTOM_IMAP_HOST": "imap.example.test",
        "CUSTOM_IMAP_PORT": 993,
        "CUSTOM_SMTP_HOST": "smtp.example.test",
        "CUSTOM_SMTP_PORT": 465,
    }
    values.update(overrides)
    return EmailAccount(**values)


async def add_instance_account(attachment_dir: str = "") -> DBEmailAccount:
    return await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=OWN_INSTANCE,
        username=ADDRESS,
        display_name="共享邮箱",
        provider="QQ邮箱",
        settings={},
        password="authorization-code",
        enabled=True,
        receive_enabled=True,
        send_enabled=True,
        attachment_dir=attachment_dir,
    )


async def add_cache_row(*, owner_adapter_key: str, owner_instance_key: str) -> DBEmail:
    return await DBEmail.create(
        account_username=ADDRESS,
        email_uid=UID,
        subject="一封信",
        sender="sender@example.test",
        date="2026-01-01 00:00:00",
        body_text="正文",
        body_html="",
        has_attachments=False,
        attachment_names="[]",
        attachment_paths="[]",
        raw_content="",
        owner_adapter_key=owner_adapter_key,
        owner_instance_key=owner_instance_key,
    )


class TestPlatformToInstance:
    async def test_the_account_moves_with_its_cache_rows(self, db, fake_adapter) -> None:
        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account()])
        await add_cache_row(owner_adapter_key="", owner_instance_key="")

        result = await transfer_email_account_to_instance(
            0,
            {"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE, "expected_username": ADDRESS},
            adapter=adapter,
        )

        assert result["success"] is True
        assert adapter.config.RECEIVE_ACCOUNTS == [], "配置里不能再留着它"
        row = await DBEmailAccount.get(id=result["id"])
        assert (row.owner_adapter_key, row.owner_instance_key) == (ADAPTER, OWN_INSTANCE)
        assert row.attachment_dir == ADDRESS, "附件目录沿用原地址：文件不搬"
        cached = await DBEmail.get(account_username=ADDRESS, email_uid=UID)
        assert (cached.owner_adapter_key, cached.owner_instance_key) == (ADAPTER, OWN_INSTANCE)
        assert ADDRESS in adapter.instance_account_owner

    async def test_another_instances_leftover_rows_are_not_handed_over(self, db, fake_adapter) -> None:
        """同一个地址下别的实例留下的旧缓存行（删账户时没删掉）不能跟着转给目标实例"""
        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account()])
        await add_cache_row(owner_adapter_key="", owner_instance_key="")
        leftover = await DBEmail.create(
            account_username=ADDRESS,
            email_uid="other-uid",
            subject="别的实例的旧信",
            sender="sender@example.test",
            date="2026-01-01 00:00:00",
            body_text="正文",
            body_html="",
            has_attachments=False,
            attachment_names="[]",
            attachment_paths="[]",
            raw_content="",
            owner_adapter_key=ADAPTER,
            owner_instance_key="someone-else",
        )

        await transfer_email_account_to_instance(
            0,
            {"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE, "expected_username": ADDRESS},
            adapter=adapter,
        )

        moved = await DBEmail.get(account_username=ADDRESS, email_uid=UID)
        assert (moved.owner_adapter_key, moved.owner_instance_key) == (ADAPTER, OWN_INSTANCE)
        kept = await DBEmail.get(id=leftover.id)
        assert (kept.owner_adapter_key, kept.owner_instance_key) == (ADAPTER, "someone-else")

    async def test_an_oauth_account_is_refused(self, db, fake_adapter) -> None:
        from fastapi import HTTPException

        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account(AUTH_TYPE="oauth2", TRANSPORT_TYPE="microsoft_graph")])

        with pytest.raises(HTTPException) as excinfo:
            await transfer_email_account_to_instance(
                0,
                {"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE, "expected_username": ADDRESS},
                adapter=adapter,
            )

        assert excinfo.value.status_code == 400
        assert adapter.config.RECEIVE_ACCOUNTS, "拒绝时配置不能动"
        assert await DBEmailAccount.all().count() == 0

    async def test_an_unknown_instance_is_refused(self, db, fake_adapter) -> None:
        from fastapi import HTTPException

        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account()])

        with pytest.raises(HTTPException) as excinfo:
            await transfer_email_account_to_instance(
                0,
                {"adapter_key": ADAPTER, "instance_key": "ghost", "expected_username": ADDRESS},
                adapter=adapter,
            )

        assert excinfo.value.status_code == 404

    async def test_the_quota_is_checked(self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi import HTTPException

        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account()])

        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError("?????? 2 ???????")

        monkeypatch.setattr(mail, "assert_instance_account_quota", refuse)

        with pytest.raises(HTTPException) as excinfo:
            await transfer_email_account_to_instance(
                0,
                {"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE, "expected_username": ADDRESS},
                adapter=adapter,
            )

        assert excinfo.value.status_code == 400
        assert await DBEmailAccount.all().count() == 0

    async def test_a_failure_restores_the_platform_config(self, db, fake_adapter, monkeypatch) -> None:
        """配置是内存对象：事务回滚数据库，配置得自己复原，否则运行时以为它已经搬走了"""
        from fastapi import HTTPException

        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account()])

        async def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("数据库写不进去")

        monkeypatch.setattr("nekro_agent.models.db_email.DBEmail.filter", boom)

        with pytest.raises(HTTPException):
            await transfer_email_account_to_instance(
                0,
                {"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE, "expected_username": ADDRESS},
                adapter=adapter,
            )

        assert [account.USERNAME for account in adapter.config.RECEIVE_ACCOUNTS] == [ADDRESS]


OTHER_ADDRESS = "other-platform@example.test"


class TestPlatformToInstanceChecksTheAddress:
    """账户按数组下标定位：列表在别处变过时同一个下标是别的邮箱，必须按地址核对，对不上就 409"""

    async def test_a_shifted_index_is_refused(self, db, fake_adapter) -> None:
        """页面加载时 0 号是 ADDRESS；之后另一位管理员在前面加了一个账户，0 号成了别的邮箱"""
        from fastapi import HTTPException

        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account(USERNAME=OTHER_ADDRESS), platform_account()])
        await add_cache_row(owner_adapter_key="", owner_instance_key="")

        with pytest.raises(HTTPException) as excinfo:
            await transfer_email_account_to_instance(
                0,
                {"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE, "expected_username": ADDRESS},
                adapter=adapter,
            )

        assert excinfo.value.status_code == 409
        assert [account.USERNAME for account in adapter.config.RECEIVE_ACCOUNTS] == [OTHER_ADDRESS, ADDRESS]
        assert await DBEmailAccount.all().count() == 0
        cached = await DBEmail.get(account_username=ADDRESS, email_uid=UID)
        assert (cached.owner_adapter_key, cached.owner_instance_key) == ("", "")

    async def test_the_expected_address_is_compared_normalized(self, db, fake_adapter) -> None:
        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account()])

        result = await transfer_email_account_to_instance(
            0,
            {"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE, "expected_username": f"  {ADDRESS.upper()} "},
            adapter=adapter,
        )

        assert result["success"] is True

    async def test_without_the_expected_address_nothing_happens(self, db, fake_adapter) -> None:
        from fastapi import HTTPException

        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account()])

        with pytest.raises(HTTPException) as excinfo:
            await transfer_email_account_to_instance(
                0,
                {"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE},
                adapter=adapter,
            )

        assert excinfo.value.status_code == 400
        assert [account.USERNAME for account in adapter.config.RECEIVE_ACCOUNTS] == [ADDRESS]
        assert await DBEmailAccount.all().count() == 0

    async def test_an_account_added_meanwhile_is_kept(self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch) -> None:
        """检查与写回之间有好几次等待：这期间别人在前面加了一个账户，写回时按地址重新找，别人加的那个不能被弄掉"""
        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account()])

        async def someone_adds_an_account(*_args: Any, **_kwargs: Any) -> None:
            adapter.config.RECEIVE_ACCOUNTS = [platform_account(USERNAME=OTHER_ADDRESS), *adapter.config.RECEIVE_ACCOUNTS]

        monkeypatch.setattr(mail, "assert_instance_account_quota", someone_adds_an_account)

        result = await transfer_email_account_to_instance(
            0,
            {"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE, "expected_username": ADDRESS},
            adapter=adapter,
        )

        assert result["success"] is True
        assert [account.USERNAME for account in adapter.config.RECEIVE_ACCOUNTS] == [OTHER_ADDRESS]
        row = await DBEmailAccount.get(id=result["id"])
        assert row.username == ADDRESS

    async def test_an_account_removed_meanwhile_is_not_moved(self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi import HTTPException

        from nekro_agent.adapters.email.routers import transfer_email_account_to_instance

        adapter = fake_adapter([platform_account()])
        await add_cache_row(owner_adapter_key="", owner_instance_key="")

        async def someone_deletes_it(*_args: Any, **_kwargs: Any) -> None:
            adapter.config.RECEIVE_ACCOUNTS = []

        monkeypatch.setattr(mail, "assert_instance_account_quota", someone_deletes_it)

        with pytest.raises(HTTPException) as excinfo:
            await transfer_email_account_to_instance(
                0,
                {"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE, "expected_username": ADDRESS},
                adapter=adapter,
            )

        assert excinfo.value.status_code == 409
        assert await DBEmailAccount.all().count() == 0
        cached = await DBEmail.get(account_username=ADDRESS, email_uid=UID)
        assert (cached.owner_adapter_key, cached.owner_instance_key) == ("", "")


class TestInstanceToPlatform:
    async def test_a_tenant_server_is_remembered_as_such(self, db, fake_adapter) -> None:
        """自定义服务器是租户填的：转回平台方之后仍按租户服务器连（只连校验过的地址），直到平台方改掉它"""
        adapter = fake_adapter()
        row = await add_instance_account()
        row.provider = "自定义"
        row.settings = {"imap_host": "imap.example.test", "smtp_host": "smtp.example.test"}
        await row.save()

        await transfer_instance_account_to_platform(
            row.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        moved = adapter.config.RECEIVE_ACCOUNTS[0]
        assert moved.TENANT_SUPPLIED_HOSTS == "imap.example.test,smtp.example.test"
        assert moved.is_tenant_host("imap.example.test") and moved.is_tenant_host("smtp.example.test")
        assert moved.is_tenant_endpoint() is True
        assert "TENANT_SUPPLIED_HOSTS" in moved.model_dump(), "要落盘，重启之后还在"

    async def test_a_preset_provider_is_not_marked(self, db, fake_adapter) -> None:
        adapter = fake_adapter()
        row = await add_instance_account()

        await transfer_instance_account_to_platform(
            row.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        assert adapter.config.RECEIVE_ACCOUNTS[0].TENANT_SUPPLIED_HOSTS == ""
        assert adapter.config.RECEIVE_ACCOUNTS[0].is_tenant_endpoint() is False

    async def test_the_account_goes_back_with_its_rows(self, db, fake_adapter) -> None:
        adapter = fake_adapter()
        row = await add_instance_account()
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=OWN_INSTANCE)

        result = await transfer_instance_account_to_platform(
            row.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        assert result["success"] is True
        assert [account.USERNAME for account in adapter.config.RECEIVE_ACCOUNTS] == [ADDRESS]
        assert adapter.config.dumps == 1
        assert await DBEmailAccount.filter(id=row.id).exists() is False
        cached = await DBEmail.get(account_username=ADDRESS, email_uid=UID)
        assert (cached.owner_adapter_key, cached.owner_instance_key) == ("", "")

    async def test_a_failed_config_write_leaves_everything_as_it_was(
        self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """缓存行快照、删表行、写配置在一个事务里：配置写不进去，表行、缓存行、内存配置、目录名都回到原样"""
        adapter = fake_adapter()
        row = await add_instance_account()
        row.attachment_dir = mail.new_attachment_dir(row.id)
        await row.save()
        instance_dir = db / "uploads" / "email_attachment" / row.attachment_dir
        instance_dir.mkdir(parents=True)
        await add_cache_row(owner_adapter_key=ADAPTER, owner_instance_key=OWN_INSTANCE)

        def boom() -> None:
            raise OSError("disk full")

        monkeypatch.setattr(adapter.config, "dump_config", boom)

        with pytest.raises(ValidationError):
            await transfer_instance_account_to_platform(
                row.id,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=platform_owner(),
            )

        assert adapter.config.RECEIVE_ACCOUNTS == []
        assert await DBEmailAccount.filter(id=row.id).exists() is True
        cached = await DBEmail.get(account_username=ADDRESS, email_uid=UID)
        assert (cached.owner_adapter_key, cached.owner_instance_key) == (ADAPTER, OWN_INSTANCE)
        assert instance_dir.is_dir(), "目录改名要改回去"
        assert not (db / "uploads" / "email_attachment" / ADDRESS).exists()

    async def test_a_tenant_gets_403(self, db, fake_adapter) -> None:
        """这是平台方的决定：租户不该能替它做（403 而不是 404，是能力边界）"""
        adapter = fake_adapter()
        row = await add_instance_account()

        with pytest.raises(PermissionDeniedError):
            await transfer_instance_account_to_platform(
                row.id,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=tenant_admin(),
            )

        assert await DBEmailAccount.filter(id=row.id).exists() is True
        assert adapter.config.RECEIVE_ACCOUNTS == []

    async def test_a_duplicate_platform_address_is_refused(self, db, fake_adapter) -> None:
        fake_adapter([platform_account()])
        row = await add_instance_account()

        with pytest.raises(ValidationError):
            await transfer_instance_account_to_platform(
                row.id,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=platform_owner(),
            )

        assert await DBEmailAccount.filter(id=row.id).exists() is True

    async def test_the_instance_directory_is_renamed_to_the_address(self, db, fake_adapter) -> None:
        adapter = fake_adapter()
        row = await add_instance_account()
        row.attachment_dir = mail.new_attachment_dir(row.id)
        await row.save()
        source = db / "uploads" / "email_attachment" / row.attachment_dir / UID
        source.mkdir(parents=True, exist_ok=True)
        (source / "a.txt").write_text("x", encoding="utf-8")

        result = await transfer_instance_account_to_platform(
            row.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        assert result["attachment_dir"] == ADDRESS
        moved = db / "uploads" / "email_attachment" / ADDRESS / UID / "a.txt"
        assert moved.read_text(encoding="utf-8") == "x"
        assert not (db / "uploads" / "email_attachment" / row.attachment_dir).exists()
        assert [account.USERNAME for account in adapter.synced] == [ADDRESS]

    async def test_an_existing_target_directory_is_refused(self, db, fake_adapter) -> None:
        """两个目录合并会有同名文件互相覆盖的风险：拒绝，而不是挑一个赢家"""
        fake_adapter()
        row = await add_instance_account()
        row.attachment_dir = mail.new_attachment_dir(row.id)
        await row.save()
        (db / "uploads" / "email_attachment" / row.attachment_dir).mkdir(parents=True)
        (db / "uploads" / "email_attachment" / ADDRESS).mkdir(parents=True)

        with pytest.raises(ValidationError):
            await transfer_instance_account_to_platform(
                row.id,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=platform_owner(),
            )

        assert await DBEmailAccount.filter(id=row.id).exists() is True

    async def test_an_address_shaped_directory_is_left_alone(self, db, fake_adapter) -> None:
        """本来就是从平台方转来的（目录=原地址）：不搬、也不删"""
        adapter = fake_adapter()
        row = await add_instance_account(attachment_dir=ADDRESS)
        kept = db / "uploads" / "email_attachment" / ADDRESS / UID
        kept.mkdir(parents=True, exist_ok=True)

        result = await transfer_instance_account_to_platform(
            row.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        assert result["attachment_dir"] == ADDRESS
        assert kept.is_dir()
        assert [account.USERNAME for account in adapter.config.RECEIVE_ACCOUNTS] == [ADDRESS]

    async def test_the_gate_blocks_it(self, db, fake_adapter, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_adapter()
        row = await add_instance_account()
        monkeypatch.setattr(mail, "instance_accounts_enabled", lambda: False)

        with pytest.raises(ValidationError):
            await transfer_instance_account_to_platform(
                row.id,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=platform_owner(),
            )
