"""租户成员创建的权限边界与创建者记录

## 钉死什么

1. **租户管理员只能建只读成员。** 租户管理员本身就是 `Role.Admin`，如果它还能建一个
   同样是 `Role.Admin` 的账号，「谁有管理权」就完全落在租户自己手里：客户的主账号被接管
   之后，接管者可以再建一个管理员账号当后门，平台方停用原账号也止不住。
2. **新增管理员由平台方来建。** 平台方走同一路由建 `tenant_admin` 仍然成功，行为不变。
3. **初始口令长度对两边一样。** 后台账号是平台的入口，11 位一律 400，正好 12 位放行，
   且被拒的请求不会在库里留下半行。
4. **谁建的记在 `ext_data.created_by`。** `DBUser` 没有专门的列，`ext_data` 本来就是
   JSONField，因此这条信息不需要加迁移就能落库；后续排查「这个后门账号是谁开的」靠它。

判定都在路由层（`routers/console/tenants.create_tenant_member`），所以这里直接调用端点函数、
用内存 SQLite 上的真表，而不是伪造一个 HTTP 客户端 —— 与其余租户作用域用例同一做法。
"""

import datetime
from typing import Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.tenants import (
    TenantAdminCanOnlyCreateReadOnlyMemberError,
    create_tenant_member,
)
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.tenant import (
    MIN_INITIAL_PASSWORD_LENGTH,
    TenantMemberCreateRequest,
    TenantRole,
    TenantStatus,
)
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

# 正好达到下限的口令；再少一位就是 11 位
GOOD_PASSWORD = "abcdefghijkl"
SHORT_PASSWORD = "abcdefghijk"


def make_user(
    username: str,
    *,
    tenant_id: Optional[int],
    tenant_role: TenantRole,
    perm_level: Role,
) -> DBUser:
    return DBUser(
        username=username,
        password="",
        adapter_key="",
        platform_userid="",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBTenant.create(id=ACME_TENANT_ID, name="acme_ltd", display_name="Acme", status=TenantStatus.ACTIVE.value)
        await DBTenant.create(id=RIVAL_TENANT_ID, name="rival_ltd", display_name="Rival", status=TenantStatus.ACTIVE.value)
        yield
    finally:
        await Tortoise.close_connections()


async def persist_caller(
    username: str,
    *,
    tenant_id: Optional[int],
    tenant_role: TenantRole,
    perm_level: Role,
) -> DBUser:
    """把调用者本身也写进库：`created_by` 记的是**库里那一行**的 id，不是临时对象"""
    user = make_user(username, tenant_id=tenant_id, tenant_role=tenant_role, perm_level=perm_level)
    await user.save()
    return user


async def platform_owner() -> DBUser:
    return await persist_caller("admin", tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


async def acme_admin() -> DBUser:
    return await persist_caller(
        "acme_admin",
        tenant_id=ACME_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        perm_level=Role.Admin,
    )


def request_for(username: str, role: Optional[TenantRole] = None, password: str = GOOD_PASSWORD) -> TenantMemberCreateRequest:
    payload = {"username": username, "password": password}
    if role is not None:
        payload["role"] = role.value
    return TenantMemberCreateRequest(**payload)


class TestTenantAdminCanOnlyCreateReadOnlyMembers:
    async def test_tenant_admin_creates_a_read_only_member(self, db) -> None:  # noqa: ARG002
        creator = await acme_admin()
        info = await create_tenant_member(
            ACME_TENANT_ID,
            request_for("acme_ro", TenantRole.TENANT_USER),
            _current_user=creator,
        )

        assert info.role is TenantRole.TENANT_USER
        row = await DBUser.get(id=info.user_id)
        assert row.tenant_id == ACME_TENANT_ID
        assert row.tenant_role == TenantRole.TENANT_USER.value
        # 只读成员在旧权限体系里是 `Role.User`，够不到任何宿主级操作
        assert row.perm_level == Role.User
        assert row.ext_data["created_by"] == {
            "user_id": creator.id,
            "username": "acme_admin",
            "role": TenantRole.TENANT_ADMIN.value,
        }

    async def test_tenant_admin_cannot_create_another_admin(self, db) -> None:  # noqa: ARG002
        creator = await acme_admin()
        with pytest.raises(TenantAdminCanOnlyCreateReadOnlyMemberError):
            await create_tenant_member(
                ACME_TENANT_ID,
                request_for("acme_admin2", TenantRole.TENANT_ADMIN),
                _current_user=creator,
            )
        assert not await DBUser.filter(username="acme_admin2").exists()

    async def test_tenant_admin_without_role_is_refused(self, db) -> None:  # noqa: ARG002
        """请求体的默认角色仍是 `TENANT_ADMIN`，所以不带 `role` 会被同一条拒掉

        这是**预期**行为而不是漏洞：老客户端不带角色时提交的正是最高的那一档，
        要么被拒、要么就必须静默降级成只读（那会让前端显示与实际权限对不上）。
        """
        assert request_for("acme_admin2").role is TenantRole.TENANT_ADMIN
        creator = await acme_admin()
        with pytest.raises(TenantAdminCanOnlyCreateReadOnlyMemberError):
            await create_tenant_member(ACME_TENANT_ID, request_for("acme_admin2"), _current_user=creator)
        assert not await DBUser.filter(username="acme_admin2").exists()

    async def test_platform_owner_still_creates_an_admin_and_is_recorded(self, db) -> None:  # noqa: ARG002
        """正向对照：这条限制只对租户生效，平台方的既有工作流一步不变"""
        creator = await platform_owner()
        info = await create_tenant_member(
            ACME_TENANT_ID,
            request_for("acme_admin2", TenantRole.TENANT_ADMIN),
            _current_user=creator,
        )

        assert info.role is TenantRole.TENANT_ADMIN
        row = await DBUser.get(id=info.user_id)
        assert row.tenant_role == TenantRole.TENANT_ADMIN.value
        assert row.perm_level == Role.Admin
        assert row.ext_data["created_by"] == {
            "user_id": creator.id,
            "username": "admin",
            "role": TenantRole.PLATFORM_OWNER.value,
        }

    async def test_platform_owner_is_still_refused_as_a_role(self, db) -> None:  # noqa: ARG002
        creator = await platform_owner()
        with pytest.raises(ValidationError):
            await create_tenant_member(
                ACME_TENANT_ID,
                request_for("acme_root", TenantRole.PLATFORM_OWNER),
                _current_user=creator,
            )
        assert not await DBUser.filter(username="acme_root").exists()


class TestInitialPasswordLength:
    def test_the_constant_is_twelve(self) -> None:
        assert MIN_INITIAL_PASSWORD_LENGTH == 12

    @pytest.mark.parametrize("caller_name", ["platform", "tenant_admin"])
    async def test_eleven_characters_are_refused(self, db, caller_name: str) -> None:  # noqa: ARG002
        """平台方与租户管理员走同一条长度检查：这条规则不是"管住租户"的，是管住入口的"""
        creator = await platform_owner() if caller_name == "platform" else await acme_admin()
        with pytest.raises(ValidationError):
            await create_tenant_member(
                ACME_TENANT_ID,
                request_for(f"new_{caller_name}", TenantRole.TENANT_USER, password=SHORT_PASSWORD),
                _current_user=creator,
            )
        assert not await DBUser.filter(username=f"new_{caller_name}").exists()

    @pytest.mark.parametrize("caller_name", ["platform", "tenant_admin"])
    async def test_exactly_twelve_characters_succeed(self, db, caller_name: str) -> None:  # noqa: ARG002
        """对照组：把长度检查写成 `< 12` 还是 `<= 12` 在这一对上才分得开"""
        creator = await platform_owner() if caller_name == "platform" else await acme_admin()
        info = await create_tenant_member(
            ACME_TENANT_ID,
            request_for(f"new_{caller_name}", TenantRole.TENANT_USER, password=GOOD_PASSWORD),
            _current_user=creator,
        )
        assert await DBUser.filter(id=info.user_id).exists()


class TestCreationStillScopesToTheTenant:
    async def test_cross_tenant_creation_is_a_404(self, db) -> None:  # noqa: ARG002
        """多了一条角色判定，越权判定不能跟着松掉"""
        from nekro_agent.schemas.errors import NotFoundError

        creator = await acme_admin()
        with pytest.raises(NotFoundError):
            await create_tenant_member(
                RIVAL_TENANT_ID,
                request_for("rival_ro", TenantRole.TENANT_USER),
                _current_user=creator,
            )
        assert not await DBUser.filter(username="rival_ro").exists()
