"""`/api/emails/*`：平台方邮箱与实例自有邮箱之间的隔离

## 改造前后的一句话对比

改造前：这四条只能是平台方专属，因为 `DBEmail` 里没有任何一列能把某封邮件判给某个租户。
改造后：`DBEmail` 带归属快照（`owner_*` 为空 = 平台方），于是判据从「身份是不是平台方」
细化成「这封邮件属于哪个实例对」。

## 四条不能错的地方

1. **平台方不带实例参数时看到的仍然是原来那一份**（升级后行为逐字不变）；
2. **租户必须给出自己的实例**，不给是 400，而不是「默认给平台方那份」；
3. **越权与不存在同一个 404**：两种回答如果不同，租户就能用「换个 ID 试试」枚举邮件；
4. **附件先查缓存行再碰文件系统**：路径里带地址与 UID，不判归属就能读任意目录下的文件。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core import os_env
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.email import get_email_attachment, get_email_attachment_by_id, get_email_detail, get_emails
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"
PLATFORM_INSTANCE = "plat1"

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

PLATFORM_MAIL_ID = 1
OWN_MAIL_ID = 2
FOREIGN_MAIL_ID = 3

INSTANCE_LAYOUT = [
    (OWN_INSTANCE, ACME_TENANT_ID),
    (FOREIGN_INSTANCE, RIVAL_TENANT_ID),
    (PLATFORM_INSTANCE, None),
]


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


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


async def create_mail(*, email_id: int, subject: str, owner_instance: str = "", username: str = "ops@example.test") -> DBEmail:
    return await DBEmail.create(
        id=email_id,
        account_username=username,
        email_uid=str(1000 + email_id),
        subject=subject,
        sender="noreply@bank.example",
        recipients=f'["{username}"]',
        body_text=f"{subject} 的正文",
        has_attachments=False,
        attachment_names="[]",
        owner_adapter_key=ADAPTER if owner_instance else "",
        owner_instance_key=owner_instance,
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
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
        await create_mail(email_id=PLATFORM_MAIL_ID, subject="平台方的信")
        await create_mail(
            email_id=OWN_MAIL_ID, subject="acme 的信", owner_instance=OWN_INSTANCE,
            username="own@example.test",
        )
        await create_mail(
            email_id=FOREIGN_MAIL_ID, subject="rival 的信", owner_instance=FOREIGN_INSTANCE,
            username="rival@example.test",
        )
        yield
    finally:
        await Tortoise.close_connections()


class TestPlatformScopeKeepsTheOldBehaviour:
    """平台方不带实例参数：只看 `owner_*` 为空的那一份"""

    async def test_list_shows_only_platform_mail(self, db) -> None:
        listed = await get_emails(
            offset=0, limit=25, account=None, adapter_key="", instance_key="", _current_user=platform_owner(),
        )

        assert [item.subject for item in listed.items] == ["平台方的信"]

    async def test_detail_of_a_platform_mail_works(self, db) -> None:
        detail = await get_email_detail(
            PLATFORM_MAIL_ID, adapter_key="", instance_key="", _current_user=platform_owner(),
        )

        assert detail.subject == "平台方的信"

    async def test_instance_mail_is_not_reachable_without_the_instance(self, db) -> None:
        with pytest.raises(NotFoundError):
            await get_email_detail(OWN_MAIL_ID, adapter_key="", instance_key="", _current_user=platform_owner())

    async def test_platform_owner_can_scope_into_an_instance(self, db) -> None:
        listed = await get_emails(
            offset=0, limit=25, account=None, adapter_key=ADAPTER, instance_key=OWN_INSTANCE,
            _current_user=platform_owner(),
        )

        assert [item.subject for item in listed.items] == ["acme 的信"]

    async def test_platform_owner_needs_both_parameters_for_an_instance(self, db) -> None:
        with pytest.raises(ValidationError):
            await get_emails(
                offset=0, limit=25, account=None, adapter_key="", instance_key=OWN_INSTANCE,
                _current_user=platform_owner(),
            )


class TestTenantScope:
    """租户必须给出自己的实例，且只能看到自己那个实例的邮箱"""

    async def test_missing_parameters_are_a_validation_error(self, db) -> None:
        """不给是 400，而不是「默认给平台方那份」"""
        with pytest.raises(ValidationError):
            await get_emails(offset=0, limit=25, account=None, adapter_key="", instance_key="", _current_user=acme_admin())

    async def test_only_one_parameter_is_a_validation_error(self, db) -> None:
        with pytest.raises(ValidationError):
            await get_emails(
                offset=0, limit=25, account=None, adapter_key=ADAPTER, instance_key="", _current_user=acme_admin(),
            )

    async def test_tenant_sees_its_own_mail(self, db) -> None:
        listed = await get_emails(
            offset=0, limit=25, account=None, adapter_key=ADAPTER, instance_key=OWN_INSTANCE,
            _current_user=acme_admin(),
        )

        assert [item.subject for item in listed.items] == ["acme 的信"]

    async def test_tenant_does_not_see_the_platform_mail(self, db) -> None:
        listed = await get_emails(
            offset=0, limit=25, account=None, adapter_key=ADAPTER, instance_key=OWN_INSTANCE,
            _current_user=acme_admin(),
        )

        assert "平台方的信" not in [item.subject for item in listed.items]

    async def test_another_tenants_instance_is_not_found(self, db) -> None:
        with pytest.raises(NotFoundError):
            await get_emails(
                offset=0, limit=25, account=None, adapter_key=ADAPTER, instance_key=FOREIGN_INSTANCE,
                _current_user=acme_admin(),
            )

    async def test_unknown_instance_is_the_same_error(self, db) -> None:
        with pytest.raises(NotFoundError):
            await get_emails(
                offset=0, limit=25, account=None, adapter_key=ADAPTER, instance_key="ghost",
                _current_user=acme_admin(),
            )


class TestByIDAccessIsIndistinguishable:
    """「不是你的」与「不存在」抛同一个 `NotFoundError`"""

    async def test_foreign_mail_and_missing_mail_are_the_same_error(self, db) -> None:
        errors = []
        for email_id in (FOREIGN_MAIL_ID, 9999):
            with pytest.raises(NotFoundError) as caught:
                await get_email_detail(
                    email_id, adapter_key=ADAPTER, instance_key=OWN_INSTANCE, _current_user=acme_admin(),
                )
            errors.append(str(caught.value))

        assert errors[0] == errors[1]

    async def test_platform_mail_is_not_reachable_by_id_from_a_tenant(self, db) -> None:
        with pytest.raises(NotFoundError):
            await get_email_detail(
                PLATFORM_MAIL_ID, adapter_key=ADAPTER, instance_key=OWN_INSTANCE, _current_user=acme_admin(),
            )


class TestAccountFilter:
    """列表的 `account` 筛选不能跨过归属条件"""

    async def test_filter_stays_inside_the_scope(self, db) -> None:
        listed = await get_emails(
            offset=0, limit=25, account="rival@example.test", adapter_key=ADAPTER, instance_key=OWN_INSTANCE,
            _current_user=acme_admin(),
        )

        assert listed.items == []


class TestAttachmentIsCheckedAgainstTheCacheRow:
    """附件：先取缓存行并判归属，再碰文件系统

    实例自有账户的附件目录不是地址（新建的是 `_i<id>`），所以租户走的是按邮件 ID 的那一条；
    按地址的那一条只剩平台方邮箱（存量书签与外部脚本），这里也钉一下它对租户不成立。
    """

    @pytest.fixture
    def uploads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        # 附件根目录由服务层一处给出（`email_accounts.email_attachment_root`），指向临时目录
        monkeypatch.setattr(os_env.OsEnv, "DATA_DIR", str(tmp_path), raising=False)
        target = tmp_path / "uploads" / "email_attachment" / "ops@example.test" / str(1000 + PLATFORM_MAIL_ID)
        target.mkdir(parents=True, exist_ok=True)
        (target / "invoice.pdf").write_bytes(b"%PDF-1.4 fake")
        return target

    def attachment_url(self, email_id: int, filename: str) -> str:
        return f"/api/emails/{email_id}/attachments/{filename}"

    async def test_the_platform_attachment_is_served_by_id(self, db, uploads: Path) -> None:
        response = await get_email_attachment_by_id(
            PLATFORM_MAIL_ID, "invoice.pdf",
            adapter_key="", instance_key="", _current_user=platform_owner(),
        )

        assert Path(response.path).name == "invoice.pdf"

    async def test_a_tenant_cannot_get_a_platform_attachment_by_id(self, db, uploads: Path) -> None:
        with pytest.raises(NotFoundError):
            await get_email_attachment_by_id(
                PLATFORM_MAIL_ID, "invoice.pdf",
                adapter_key=ADAPTER, instance_key=OWN_INSTANCE, _current_user=acme_admin(),
            )

    async def test_an_unknown_id_is_the_same_error(self, db, uploads: Path) -> None:
        errors = []
        for email_id in (PLATFORM_MAIL_ID, 9999):
            with pytest.raises(NotFoundError) as caught:
                await get_email_attachment_by_id(
                    email_id, "invoice.pdf",
                    adapter_key=ADAPTER, instance_key=OWN_INSTANCE, _current_user=acme_admin(),
                )
            errors.append(str(caught.value))

        assert errors[0] == errors[1]

    async def test_unknown_file_name_is_not_found(self, db, uploads: Path) -> None:
        with pytest.raises(NotFoundError):
            await get_email_attachment_by_id(
                PLATFORM_MAIL_ID, "secret.txt",
                adapter_key="", instance_key="", _current_user=platform_owner(),
            )

    async def test_path_traversal_in_the_name_is_neutralised(self, db, uploads: Path) -> None:
        """文件名只收单段：`../` 连路径都拼不出来"""
        outside = uploads.parent.parent / "outside.txt"
        outside.write_bytes(b"not yours")

        with pytest.raises(NotFoundError):
            await get_email_attachment_by_id(
                PLATFORM_MAIL_ID, "../../../outside.txt",
                adapter_key="", instance_key="", _current_user=platform_owner(),
            )

    async def test_the_address_route_refuses_instance_mail(self, db, uploads: Path) -> None:
        """按地址那条只给平台方：租户拿它取实例的附件一律 404"""
        with pytest.raises(NotFoundError):
            await get_email_attachment(
                "own@example.test", str(1000 + OWN_MAIL_ID), "invoice.pdf",
                adapter_key=ADAPTER, instance_key=OWN_INSTANCE, _current_user=acme_admin(),
            )

    async def test_the_address_route_still_serves_platform_mail(self, db, uploads: Path) -> None:
        response = await get_email_attachment(
            "ops@example.test", str(1000 + PLATFORM_MAIL_ID), "invoice.pdf",
            adapter_key="", instance_key="", _current_user=platform_owner(),
        )

        assert Path(response.path).name == "invoice.pdf"


class TestListPayloadShape:
    """列表项只带预览，不带正文与收件人明细"""

    async def test_preview_is_truncated(self, db) -> None:
        await DBEmail.create(
            id=50,
            account_username="own@example.test",
            email_uid="50",
            subject="长正文",
            sender="noreply@bank.example",
            recipients='["own@example.test"]',
            body_text="x" * 500,
            has_attachments=False,
            attachment_names="[]",
            owner_adapter_key=ADAPTER,
            owner_instance_key=OWN_INSTANCE,
        )

        listed = await get_emails(
            offset=0, limit=25, account=None, adapter_key=ADAPTER, instance_key=OWN_INSTANCE,
            _current_user=acme_admin(),
        )
        item = next(item for item in listed.items if item.id == 50)

        assert len(item.body_preview) == 200
