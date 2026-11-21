"""后台运维审计页：平台方专属、分页与四个筛选

页面只读一张表，判据都在后端：租户身份必须被 `require_platform_owner()` 挡在门外，
平台方拿到的每一页字段齐全、时间是 ISO 字符串。
"""

import datetime
from typing import Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_ops_agent_audit import DBOpsAgentAudit
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.ops_audit import list_ops_audit
from nekro_agent.schemas.errors import PermissionDeniedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.tenant_perm import require_platform_owner

ACME_TENANT_ID = 1


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
        password="",
        adapter_key="webui",
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


async def seed_rows() -> None:
    await DBOpsAgentAudit.create(
        source="agent",
        operator="10001",
        adapter_key="onebot_v11",
        instance_key="inst1",
        chat_key="onebot_v11-inst1-group_10001",
        tool="ops_find",
        args_json={"keyword": "x"},
        outcome="executed",
        result_summary="返回 str",
        finished_at=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc),
    )
    await DBOpsAgentAudit.create(
        source="na_ops",
        operator="20002",
        adapter_key="onebot_v11",
        instance_key="inst1",
        chat_key="onebot_v11-inst1-group_10001",
        tool="na_ops:tools",
        args_json={"target": "off"},
        outcome="failed",
        refuse_reason="执行出错",
        result_summary="未执行",
    )
    await DBOpsAgentAudit.create(
        source="agent",
        operator="10001,20002",
        adapter_key="onebot_v11",
        instance_key="inst1",
        chat_key="onebot_v11-inst1-group_10001",
        tool="ops_set_channel_active",
        args_json={},
        outcome="refused",
        refuse_reason="tainted",
    )


class TestOpsAuditEndpoint:
    async def test_platform_owner_gets_a_page_with_every_column(self, db) -> None:  # noqa: ARG002
        await seed_rows()

        response = await list_ops_audit(page=1, page_size=2, _current_user=platform_owner())

        assert response.total == 3
        assert [item.id for item in response.items] == [3, 2]
        item = response.items[0]
        assert item.source == "agent"
        assert item.tool == "ops_set_channel_active"
        assert item.outcome == "refused"
        assert item.refuse_reason == "tainted"
        assert item.args_json == {}
        assert "T" in item.created_at
        assert item.finished_at is None

    async def test_the_second_page_is_offset_and_finished_at_is_iso(self, db) -> None:  # noqa: ARG002
        await seed_rows()

        response = await list_ops_audit(page=2, page_size=2, _current_user=platform_owner())

        assert [item.id for item in response.items] == [1]
        assert response.items[0].finished_at == "2026-01-02T03:04:05+00:00"

    async def test_outcome_filter(self, db) -> None:  # noqa: ARG002
        await seed_rows()

        response = await list_ops_audit(page=1, page_size=20, outcome="refused", _current_user=platform_owner())

        assert response.total == 1
        assert response.items[0].tool == "ops_set_channel_active"

    async def test_tool_filter(self, db) -> None:  # noqa: ARG002
        await seed_rows()

        response = await list_ops_audit(page=1, page_size=20, tool="ops_find", _current_user=platform_owner())

        assert response.total == 1
        assert response.items[0].id == 1

    async def test_source_filter(self, db) -> None:  # noqa: ARG002
        await seed_rows()

        response = await list_ops_audit(page=1, page_size=20, source="na_ops", _current_user=platform_owner())

        assert response.total == 1
        assert response.items[0].tool == "na_ops:tools"

    async def test_operator_filter_is_a_contains_match(self, db) -> None:  # noqa: ARG002
        await seed_rows()

        response = await list_ops_audit(page=1, page_size=20, operator="200", _current_user=platform_owner())

        assert response.total == 2
        assert {item.id for item in response.items} == {2, 3}


class TestPlatformOwnerDependency:
    @staticmethod
    async def _require(user: DBUser) -> None:
        dependency = require_platform_owner()
        await dependency(current_user=user)

    async def test_tenant_admin_is_rejected(self) -> None:
        with pytest.raises(PermissionDeniedError):
            await self._require(acme_admin())

    async def test_platform_owner_passes(self) -> None:
        await self._require(platform_owner())

    def test_the_endpoint_declares_the_dependency(self) -> None:
        import inspect

        source = inspect.getsource(inspect.unwrap(list_ops_audit))
        assert "require_platform_owner()" in source
