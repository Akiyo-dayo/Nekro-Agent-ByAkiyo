"""附件按「邮件 ID」取：路径段拼不出来，只能按账户的目录算

## 这条要防的是什么

旧接口是 `/emails/{地址}/{UID}/attachments/{文件名}`，三个路径段都由调用方给。地址改成
实例自有账户之后，附件目录**不再是地址**：新建账户是 `_i<表 id>`，由平台方转来的是原地址。
所以旧接口对实例自有账户要么拼不到（附件永远 404），要么拼到别人的目录（更糟）。

新接口只收邮件 ID，目录由缓存行的归属查账户表得出。这里钉四件事：

1. 平台方的信按地址目录取到，实例的信按 `_i<id>` 取到；
2. 归属 / 存在性 / 文件名穿越全部同一个 404——三种回答如果不同，换一个文件名就能把
   别人的附件探出来；
3. 旧的按地址那条只服务平台方邮箱：租户拿它去取实例的附件一律 404；
4. 路径校验逐层（账户目录 → 邮件目录 → 文件），`../` 写法走不出去。
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_email_account import DBEmailAccount
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.email import get_email_attachment, get_email_attachment_by_id, get_email_detail
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import email_accounts as mail
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"
TENANT_ID = 1
PLATFORM_ADDRESS = "ops@example.test"
INSTANCE_ADDRESS = "own@example.test"
UID = "8801"
FILE_NAME = "report.txt"


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
    for instance_key, tenant_id in ((OWN_INSTANCE, TENANT_ID), (FOREIGN_INSTANCE, 2)):
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )
    try:
        yield tmp_path
    finally:
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


def write_attachment(root: Path, directory: str, *, uid: str = UID, name: str = FILE_NAME) -> None:
    target = root / "uploads" / "email_attachment" / directory / uid
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text("附件正文", encoding="utf-8")


async def add_platform_message() -> DBEmail:
    return await DBEmail.create(
        account_username=PLATFORM_ADDRESS,
        email_uid=UID,
        subject="平台方的信",
        sender="sender@example.test",
        date="2026-01-01 00:00:00",
        body_text="正文",
        body_html="",
        has_attachments=True,
        attachment_names='["report.txt"]',
        attachment_paths="[]",
        raw_content="",
        owner_adapter_key="",
        owner_instance_key="",
    )


async def add_instance_account(instance_key: str = OWN_INSTANCE) -> DBEmailAccount:
    row = await DBEmailAccount.create(
        owner_adapter_key=ADAPTER,
        owner_instance_key=instance_key,
        username=INSTANCE_ADDRESS,
        display_name="",
        provider="QQ邮箱",
        settings={},
        password="authorization-code",
        enabled=True,
        receive_enabled=True,
        send_enabled=False,
        attachment_dir="",
    )
    row.attachment_dir = mail.new_attachment_dir(row.id)
    await row.save()
    return row


async def add_instance_message(row: DBEmailAccount) -> DBEmail:
    return await DBEmail.create(
        account_username=row.username,
        email_uid=UID,
        subject="实例的信",
        sender="sender@example.test",
        date="2026-01-01 00:00:00",
        body_text="正文",
        body_html="",
        has_attachments=True,
        attachment_names='["report.txt"]',
        attachment_paths="[]",
        raw_content="",
        owner_adapter_key=row.owner_adapter_key,
        owner_instance_key=row.owner_instance_key,
    )


class TestByMessageId:
    async def test_the_platform_message_uses_the_address_directory(self, db) -> None:
        write_attachment(db, PLATFORM_ADDRESS)
        email = await add_platform_message()

        response = await get_email_attachment_by_id(
            email.id,
            FILE_NAME,
            adapter_key="",
            instance_key="",
            _current_user=platform_owner(),
        )

        assert Path(response.path).read_text(encoding="utf-8") == "附件正文"
        assert response.headers["content-disposition"].endswith('report.txt"')

    async def test_an_instance_message_uses_its_own_directory(self, db) -> None:
        """目录是 `_i<id>`：按地址拼路径对这一个根本拼不到"""
        row = await add_instance_account()
        write_attachment(db, row.attachment_dir)
        email = await add_instance_message(row)

        response = await get_email_attachment_by_id(
            email.id,
            FILE_NAME,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=tenant_admin(),
        )

        assert Path(response.path).parent.parent.name == row.attachment_dir
        assert Path(response.path).parent.name == UID

    async def test_a_foreign_message_is_not_found(self, db) -> None:
        """看不到与不存在同一个 404：换一个 ID 试不出「这封邮件存在」"""
        other = await add_instance_account(FOREIGN_INSTANCE)
        write_attachment(db, other.attachment_dir)
        email = await add_instance_message(other)

        with pytest.raises(NotFoundError):
            await get_email_attachment_by_id(
                email.id,
                FILE_NAME,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=tenant_admin(),
            )

    async def test_a_missing_file_is_not_found(self, db) -> None:
        email = await add_platform_message()

        with pytest.raises(NotFoundError):
            await get_email_attachment_by_id(
                email.id,
                FILE_NAME,
                adapter_key="",
                instance_key="",
                _current_user=platform_owner(),
            )

    @pytest.mark.parametrize(
        "name",
        ["../secret.txt", "..", "sub/dir.txt", "..\\secret.txt"],
        ids=["dotdot", "dotdot-only", "subdir", "backslash"],
    )
    async def test_traversal_attempts_are_not_found(self, db, name: str) -> None:
        write_attachment(db, PLATFORM_ADDRESS)
        (db / "uploads" / "email_attachment" / "secret.txt").write_text("不该被读到", encoding="utf-8")
        email = await add_platform_message()

        with pytest.raises(NotFoundError):
            await get_email_attachment_by_id(
                email.id,
                name,
                adapter_key="",
                instance_key="",
                _current_user=platform_owner(),
            )

    async def test_the_detail_page_links_to_the_new_route(self, db) -> None:
        """前端与后端只认 URL 这一处契约：详情里给的必须是按 ID 的那条"""
        row = await add_instance_account()
        write_attachment(db, row.attachment_dir)
        email = await add_instance_message(row)

        detail = await get_email_detail(
            email.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=tenant_admin(),
        )

        assert [item.url for item in detail.attachments] == [f"/api/emails/{email.id}/attachments/{FILE_NAME}"]

    async def test_a_missing_attachment_dir_yields_no_links(self, db) -> None:
        """账户被删了（行还在）时目录名为空：列不出附件，但也不该 500"""
        row = await add_instance_account()
        write_attachment(db, row.attachment_dir)
        email = await add_instance_message(row)
        await DBEmailAccount.filter(id=row.id).delete()

        detail = await get_email_detail(
            email.id,
            adapter_key=ADAPTER,
            instance_key=OWN_INSTANCE,
            _current_user=tenant_admin(),
        )

        assert detail.attachments == []


class TestByAddressIsPlatformOnly:
    async def test_the_platform_message_still_works(self, db) -> None:
        write_attachment(db, PLATFORM_ADDRESS)
        await add_platform_message()

        response = await get_email_attachment(
            PLATFORM_ADDRESS,
            UID,
            FILE_NAME,
            adapter_key="",
            instance_key="",
            _current_user=platform_owner(),
        )

        assert Path(response.path).name == FILE_NAME

    async def test_a_tenant_cannot_use_it_for_an_instance_message(self, db) -> None:
        """租户在这个接口上拿到 404——不是「无权」，而是这个接口对实例自有邮箱不存在"""
        row = await add_instance_account()
        write_attachment(db, row.attachment_dir)
        await add_instance_message(row)

        with pytest.raises(NotFoundError):
            await get_email_attachment(
                INSTANCE_ADDRESS,
                UID,
                FILE_NAME,
                adapter_key=ADAPTER,
                instance_key=OWN_INSTANCE,
                _current_user=tenant_admin(),
            )

    async def test_an_unknown_address_is_not_found(self, db) -> None:
        write_attachment(db, "ghost@example.test")

        with pytest.raises(NotFoundError):
            await get_email_attachment(
                "ghost@example.test",
                UID,
                FILE_NAME,
                adapter_key="",
                instance_key="",
                _current_user=platform_owner(),
            )


class TestPathValidation:
    """不吃路由、直接打服务层的路径函数：这里是唯一能把关的地方"""

    def test_each_layer_is_checked(self, db) -> None:
        assert mail.attachment_file_path("..", UID, FILE_NAME) is None
        assert mail.attachment_file_path(PLATFORM_ADDRESS, "..", FILE_NAME) is None
        assert mail.attachment_file_path(PLATFORM_ADDRESS, UID, "..") is None
        assert mail.attachment_file_path("a/b", UID, FILE_NAME) is None
        assert mail.attachment_file_path(PLATFORM_ADDRESS, UID, "sub/x.txt") is None

    def test_a_clean_path_is_resolved_under_the_root(self, db) -> None:
        path = mail.attachment_file_path(PLATFORM_ADDRESS, UID, FILE_NAME)

        assert path is not None
        assert path.parent.parent.parent == mail.email_attachment_root().resolve()
