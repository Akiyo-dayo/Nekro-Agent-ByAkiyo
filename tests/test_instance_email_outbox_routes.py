"""`/api/instance-email/outbox/*`：节日邮件队列的后台接口

## 为什么单独一遍

这四个端点里，前三个（暂停 / 恢复 / 作废）会**真的改变谁收到信**，而它们的入参里
`adapter_key` / `instance_key` / `batch_key` 全是调用方填的：

- 少判一个归属，租户管理员就能作废别的实例的节日邮件——不报错、不可逆；
- 少判一次「这一批真的属于这个实例」，拿别的实例的批次键过来就能改到自己名下；
- 少判一次角色，租户就能替平台方决定发不发。

前两条一律按「不存在」回答（与越权同文），第三条是明确的 403：这一条能说清楚，
而且平台方的操作入口本来就摆在租户界面之外。
"""

from __future__ import annotations

import datetime
from typing import Optional

import pytest
from fastapi import HTTPException
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_email_outbox import DBEmailOutbox
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.instance_email import (
    OutboxBatchAction,
    list_outbox_batches,
    pause_outbox_batch,
    resume_outbox_batch,
    void_outbox_batch,
)
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import email_accounts as mail
from nekro_agent.services import email_outbox
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

FESTIVAL = "中秋"
DATE = "2024-09-17"


def batch_key_of(instance_key: str = OWN_INSTANCE, festival: str = FESTIVAL) -> str:
    return email_outbox.festival_batch_key(festival, DATE, ADAPTER, instance_key)


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


def action(batch_key: str, instance_key: str = OWN_INSTANCE) -> OutboxBatchAction:
    return OutboxBatchAction(adapter_key=ADAPTER, instance_key=instance_key, batch_key=batch_key)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    mail.clear_default_sender_cache()
    email_outbox.reset_outbox_state()
    try:
        for instance_key, tenant_id in ((OWN_INSTANCE, ACME_TENANT_ID), (FOREIGN_INSTANCE, RIVAL_TENANT_ID)):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
        yield
    finally:
        email_outbox.reset_outbox_state()
        mail.clear_default_sender_cache()
        await Tortoise.close_connections()


async def add_row(
    batch_key: str,
    *,
    instance_key: str = OWN_INSTANCE,
    status: str = email_outbox.STATUS_QUEUED,
    to_address: str = "alice@example.test",
) -> DBEmailOutbox:
    return await DBEmailOutbox.create(
        batch_key=batch_key,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        platform_userid="".join(("1234", "567")),
        account_kind=mail.KIND_PLATFORM,
        account_ref="abc",
        to_address=to_address,
        subject="中秋快乐",
        body="小明：\n\n祝你团圆。",
        reason=f"节日祝福：{FESTIVAL}；收件人在本实例开过私聊",
        status=status,
        attempts=0,
        next_at=None,
    )


class TestListing:
    async def test_a_tenant_sees_its_own_batches(self, db) -> None:
        await add_row(batch_key_of())

        batches = await list_outbox_batches(ADAPTER, OWN_INSTANCE, _current_user=acme_admin())

        assert len(batches) == 1
        assert batches[0].festival_name == FESTIVAL
        assert batches[0].date == DATE
        assert batches[0].counts[email_outbox.STATUS_QUEUED] == 1
        assert batches[0].first_queued_at is not None

    async def test_another_tenants_instance_is_not_found(self, db) -> None:
        await add_row(batch_key_of(FOREIGN_INSTANCE), instance_key=FOREIGN_INSTANCE)

        with pytest.raises(NotFoundError):
            await list_outbox_batches(ADAPTER, FOREIGN_INSTANCE, _current_user=acme_admin())

    async def test_a_missing_parameter_is_a_validation_error(self, db) -> None:
        with pytest.raises(ValidationError):
            await list_outbox_batches(ADAPTER, "", _current_user=platform_owner())

    async def test_the_list_does_not_carry_the_body(self, db) -> None:
        """清单只回答「谁、多少封、什么时候」，正文不在这一页"""
        await add_row(batch_key_of())

        batches = await list_outbox_batches(ADAPTER, OWN_INSTANCE, _current_user=acme_admin())

        assert "祝你团圆" not in str(batches[0].model_dump())


class TestTenantCannotOperateTheQueue:
    async def test_pause_is_forbidden(self, db) -> None:
        key = batch_key_of()
        await add_row(key)

        with pytest.raises(HTTPException) as error:
            await pause_outbox_batch(action(key), _current_user=acme_admin())

        assert error.value.status_code == 403
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_QUEUED

    async def test_resume_is_forbidden(self, db) -> None:
        key = batch_key_of()
        await add_row(key, status=email_outbox.STATUS_PAUSED)

        with pytest.raises(HTTPException) as error:
            await resume_outbox_batch(action(key), _current_user=acme_admin())

        assert error.value.status_code == 403
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_PAUSED

    async def test_void_is_forbidden(self, db) -> None:
        key = batch_key_of()
        await add_row(key)

        with pytest.raises(HTTPException) as error:
            await void_outbox_batch(action(key), _current_user=acme_admin())

        assert error.value.status_code == 403
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_QUEUED

    async def test_the_refusal_happens_before_any_lookup(self, db) -> None:
        """先判角色再看批次：否则 404 与 403 的差异能被用来试探别的实例有没有这一批"""
        with pytest.raises(HTTPException) as error:
            await pause_outbox_batch(action("ghost-batch"), _current_user=acme_admin())

        assert error.value.status_code == 403

    async def test_another_tenants_queue_is_forbidden_anyway(self, db) -> None:
        """连自己的实例都不是时，先撞角色这一关（这是平台方专属操作）"""
        key = batch_key_of(FOREIGN_INSTANCE)
        await add_row(key, instance_key=FOREIGN_INSTANCE)

        with pytest.raises(HTTPException) as error:
            await void_outbox_batch(action(key, FOREIGN_INSTANCE), _current_user=rival_admin())

        assert error.value.status_code == 403


class TestThePlatformOwnerOperatesTheQueue:
    async def test_pause_then_resume(self, db) -> None:
        key = batch_key_of()
        await add_row(key)

        paused = await pause_outbox_batch(action(key), _current_user=platform_owner())
        assert paused == {"success": True, "changed": 1}
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_PAUSED

        resumed = await resume_outbox_batch(action(key), _current_user=platform_owner())
        assert resumed == {"success": True, "changed": 1}
        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_QUEUED

    async def test_void_clears_the_body(self, db) -> None:
        key = batch_key_of()
        await add_row(key)

        result = await void_outbox_batch(action(key), _current_user=platform_owner())

        assert result["changed"] == 1
        row = await DBEmailOutbox.get()
        assert row.status == email_outbox.STATUS_SKIPPED
        assert row.body == ""

    async def test_an_unknown_batch_is_not_found(self, db) -> None:
        with pytest.raises(NotFoundError):
            await pause_outbox_batch(action("ghost-batch"), _current_user=platform_owner())

    async def test_another_instances_batch_key_is_not_found(self, db) -> None:
        """拿别的实例的批次键过来：不是「改到零行」，而是这一批在这里不存在"""
        key = batch_key_of(FOREIGN_INSTANCE)
        await add_row(key, instance_key=FOREIGN_INSTANCE)

        with pytest.raises(NotFoundError):
            await pause_outbox_batch(action(key), _current_user=platform_owner())

        assert (await DBEmailOutbox.get()).status == email_outbox.STATUS_QUEUED

    async def test_an_empty_batch_key_is_not_found(self, db) -> None:
        """空键不可能存在：与「这一批不存在」同文，不为它单独造一个校验分支"""
        with pytest.raises(NotFoundError):
            await pause_outbox_batch(action(""), _current_user=platform_owner())

    async def test_a_platform_instance_is_reachable(self, db) -> None:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key="plat1",
            display_name="plat1",
            status="online",
            provider="remote_onebot",
            tenant_id=None,
            metadata_json="",
        )
        key = batch_key_of("plat1")
        await add_row(key, instance_key="plat1")

        result = await pause_outbox_batch(action(key, "plat1"), _current_user=platform_owner())

        assert result["changed"] == 1


class TestThePayloadShape:
    def test_the_action_requires_all_three_fields(self) -> None:
        with pytest.raises(Exception):
            OutboxBatchAction()  # type: ignore[call-arg]

    def test_the_documented_names_are_exported(self) -> None:
        from nekro_agent.routers import instance_email

        assert {"OutboxBatchAction", "OutboxBatchInfo"} <= set(instance_email.__all__)
