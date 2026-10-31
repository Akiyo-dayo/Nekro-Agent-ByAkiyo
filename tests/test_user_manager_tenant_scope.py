"""账号管理接口的租户作用域

`routers/user_manager.py` 的 8 个端点此前只写 `if _current_user.perm_level < Role.Admin`，
**只看调用方、从不看目标**。租户管理员本身就是 `Role.Admin`（`TENANT_MAX_PERM_LEVEL`），
于是这一句构成一条完整的平台接管链 —— 实测（改前，真实 HTTP）：

    POST /api/user-manager/{admin_id}/reset-password  -> 200
    之后 verify_password("pwned123", admin.password) == True
    verify_password(真实 ADMIN_PASSWORD, ...)         == False

ban / rename / delete 同样 200，跨租户重置另一个客户管理员的口令也是 200。

这里钉死四条：

1. **目标归属**：租户只能管自己租户下的账号；平台方账号、别的租户的账号、
   以及聊天平台用户行（`tenant_id` 恒为 NULL）一律不可达。
2. **目标等级**：调用方权限低于目标时拒绝，兜住手改数据库留下的越界行。
3. **越权是 404 且与"不存在"逐字相同**，否则 user_id 变成可枚举的。
4. **平台方与未启用多租户的部署一个分支都不进**。

聊天平台用户为什么归平台方：`ban_until` 是账号行上的**一个全局字段**，而一个 QQ 号可以
同时出现在多个租户的群里，给任一租户封禁权就等于让它替另一个租户做决定 ——
与工作区 `SHARED_GROUP` 判成平台方专属是同一条理由。
"""

import datetime
import inspect
from typing import Any, List, Optional, Sequence

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.user_manager import (
    ban_user,
    create_user,
    delete_user,
    get_user,
    list_users,
    prevent_trigger,
    reset_password,
    update_user,
)
from nekro_agent.routers.user_manager import (
    router as user_manager_router,
)
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError
from nekro_agent.schemas.i18n import SupportedLang
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.user import UserUpdate
from nekro_agent.services.user.auth import get_hashed_password, verify_password
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.tenant_perm import assert_managed_user_accessible

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2
MISSING_USER_ID = 9999


def make_user(
    username: str,
    *,
    tenant_id: Optional[int],
    tenant_role: TenantRole,
    perm_level: Role,
    adapter_key: str = "",
) -> DBUser:
    return DBUser(
        username=username,
        password="",
        adapter_key=adapter_key,
        platform_userid="",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


# (用户名, tenant_id, 租户角色, perm_level, adapter_key)
USER_LAYOUT = [
    ("admin", None, TenantRole.PLATFORM_OWNER, Role.Super, ""),
    ("acme_admin", ACME_TENANT_ID, TenantRole.TENANT_ADMIN, Role.Admin, ""),
    ("acme_user", ACME_TENANT_ID, TenantRole.TENANT_USER, Role.User, ""),
    ("rival_admin", RIVAL_TENANT_ID, TenantRole.TENANT_ADMIN, Role.Admin, ""),
    # 聊天平台用户：消息入站时由 `user_register` 自动建出，tenant_id 恒为 NULL
    ("QQ Nickname", None, TenantRole.PLATFORM_OWNER, Role.User, "onebot_v11"),
]


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for username, tenant_id, tenant_role, perm_level, adapter_key in USER_LAYOUT:
            user = make_user(
                username,
                tenant_id=tenant_id,
                tenant_role=tenant_role,
                perm_level=perm_level,
                adapter_key=adapter_key,
            )
            user.platform_userid = "1000000009" if adapter_key else ""
            # 真实哈希：口令是否被改写这条断言要靠 `verify_password` 来读，空串不是合法哈希
            user.password = get_hashed_password(f"{username}-pw")
            await user.save()
        yield
    finally:
        await Tortoise.close_connections()


async def user_id_of(username: str) -> int:
    return (await DBUser.get(username=username)).id


async def caller(username: str) -> DBUser:
    return await DBUser.get(username=username)


def platform_owner() -> DBUser:
    return make_user("admin", tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def single_tenant_admin() -> DBUser:
    """未启用多租户的部署里的普通管理员：无租户归属、角色为平台方"""
    return make_user("legacy", tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


UNRESTRICTED_IDENTITIES = [
    pytest.param(platform_owner, id="platform-owner"),
    pytest.param(single_tenant_admin, id="single-tenant"),
]

# 租户管理员碰不到的目标：平台方账号、别的租户、聊天平台用户行、不存在的 ID
FOREIGN_USERNAMES = ["admin", "rival_admin", "QQ Nickname"]


class TestGuardScopesByOwnership:
    async def test_tenant_cannot_reach_foreign_accounts(self, db) -> None:  # noqa: ARG002
        acme = await caller("acme_admin")
        for username in FOREIGN_USERNAMES:
            with pytest.raises(NotFoundError):
                await assert_managed_user_accessible(acme, await user_id_of(username))
        with pytest.raises(NotFoundError):
            await assert_managed_user_accessible(acme, MISSING_USER_ID)

    async def test_tenant_reaches_its_own_members(self, db) -> None:  # noqa: ARG002
        acme = await caller("acme_admin")
        for username in ("acme_admin", "acme_user"):
            target = await assert_managed_user_accessible(acme, await user_id_of(username))
            assert target.username == username

    async def test_denial_is_indistinguishable_from_absence(self, db) -> None:  # noqa: ARG002
        """存在的别人家账号与压根不存在的，对外必须是同一个错误、同一段文案"""
        assert NotFoundError.http_status == 404
        acme = await caller("acme_admin")
        messages: List[str] = []
        for user_id in (await user_id_of("admin"), MISSING_USER_ID):
            with pytest.raises(NotFoundError) as exc_info:
                await assert_managed_user_accessible(acme, user_id)
            messages.append(exc_info.value.get_message(SupportedLang.ZH_CN))
        assert messages == ["用户不存在", "用户不存在"]

    async def test_higher_privileged_target_is_refused_even_inside_own_tenant(self, db) -> None:  # noqa: ARG002
        """兜底：手改数据库把一个同租户成员提到 Role.Super，它不能反过来成为提权入口

        这一条只对租户调用方生效；平台方一侧不加，否则会改变未启用多租户的部署的既有行为
        （那里的 `Role.Admin` 账号一直可以管理 `Role.Super` 的 admin）。
        """
        rogue = await DBUser.get(username="acme_user")
        rogue.perm_level = Role.Super
        await rogue.save()
        with pytest.raises(NotFoundError):
            await assert_managed_user_accessible(await caller("acme_admin"), rogue.id)

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_reaches_every_account(self, db, identity) -> None:  # noqa: ARG002
        me = identity()
        for username, *_ in USER_LAYOUT:
            target = await assert_managed_user_accessible(me, await user_id_of(username))
            assert target.username == username

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_still_404s_on_absent(self, db, identity) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await assert_managed_user_accessible(identity(), MISSING_USER_ID)


class TestPlatformOwnerAccountIsUnreachable:
    """这是本轮的 P0：改前这一组全部返回 200，平台方口令被重置后账号即被接管"""

    async def test_reset_password_cannot_touch_platform_owner(self, db) -> None:  # noqa: ARG002
        admin = await DBUser.get(username="admin")
        original_hash = admin.password
        with pytest.raises(NotFoundError):
            await reset_password(
                user_id=admin.id,
                password="pwned123",  # noqa: S106
                _current_user=await caller("acme_admin"),
            )
        await admin.refresh_from_db()
        assert admin.password == original_hash
        assert not verify_password("pwned123", admin.password)

    async def test_ban_rename_and_delete_cannot_touch_platform_owner(self, db) -> None:  # noqa: ARG002
        admin = await DBUser.get(username="admin")
        acme = await caller("acme_admin")
        far_future = datetime.datetime(2099, 1, 1, tzinfo=datetime.timezone.utc)

        from nekro_agent.routers.user_manager import BanUserRequest, PreventTriggerRequest

        with pytest.raises(NotFoundError):
            await ban_user(user_id=admin.id, ban_data=BanUserRequest(ban_until=far_future), _current_user=acme)
        with pytest.raises(NotFoundError):
            await prevent_trigger(
                user_id=admin.id,
                prevent_data=PreventTriggerRequest(prevent_trigger_until=far_future),
                _current_user=acme,
            )
        with pytest.raises(NotFoundError):
            await update_user(user_id=admin.id, user_data=UserUpdate(username="renamed"), _current_user=acme)
        with pytest.raises(NotFoundError):
            await delete_user(user_id=admin.id, _current_user=acme)

        await admin.refresh_from_db()
        assert admin.username == "admin"
        assert admin.ban_until is None
        assert admin.prevent_trigger_until is None
        assert await DBUser.filter(id=admin.id).exists()

    async def test_cross_tenant_admin_is_unreachable(self, db) -> None:  # noqa: ARG002
        rival = await DBUser.get(username="rival_admin")
        with pytest.raises(NotFoundError):
            await delete_user(user_id=rival.id, _current_user=await caller("acme_admin"))
        assert await DBUser.filter(id=rival.id).exists()

    @pytest.mark.parametrize("username", FOREIGN_USERNAMES)
    async def test_detail_leaks_nothing_about_foreign_accounts(self, db, username: str) -> None:  # noqa: ARG002
        """详情接口回的是 perm_level / unique_id / ext_data，读到就等于拿到侦察情报"""
        with pytest.raises(NotFoundError):
            await get_user(user_id=await user_id_of(username), _current_user=await caller("acme_admin"))


class TestTenantKeepsManagingItsOwnMembers:
    """别把隔离做过头：自己团队的账号必须仍然可管"""

    async def test_reset_password_of_own_member(self, db) -> None:  # noqa: ARG002
        member = await DBUser.get(username="acme_user")
        await reset_password(
            user_id=member.id,
            password="new-password",  # noqa: S106
            _current_user=await caller("acme_admin"),
        )
        await member.refresh_from_db()
        assert verify_password("new-password", member.password)

    async def test_rename_own_member(self, db) -> None:  # noqa: ARG002
        member = await DBUser.get(username="acme_user")
        await update_user(
            user_id=member.id,
            user_data=UserUpdate(username="acme_user_renamed"),
            _current_user=await caller("acme_admin"),
        )
        await member.refresh_from_db()
        assert member.username == "acme_user_renamed"


class TestListIsFilteredByTenant:
    async def test_tenant_sees_only_its_own_members(self, db) -> None:  # noqa: ARG002
        result = await list_users(page=1, page_size=50, _current_user=await caller("acme_admin"))
        assert sorted(item.username for item in result.items) == ["acme_admin", "acme_user"]
        # total 必须跟着过滤，否则账号总数连同翻页时的空白页一起泄露出去
        assert result.total == len(result.items)

    async def test_other_tenant_sees_only_its_own(self, db) -> None:  # noqa: ARG002
        result = await list_users(page=1, page_size=50, _current_user=await caller("rival_admin"))
        assert [item.username for item in result.items] == ["rival_admin"]
        assert result.total == 1

    async def test_search_cannot_widen_the_scope(self, db) -> None:  # noqa: ARG002
        result = await list_users(page=1, page_size=50, search="admin", _current_user=await caller("acme_admin"))
        assert [item.username for item in result.items] == ["acme_admin"]
        assert result.total == 1

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identity_sees_everyone(self, db, identity) -> None:  # noqa: ARG002
        result = await list_users(page=1, page_size=50, _current_user=identity())
        assert result.total == len(USER_LAYOUT)

    async def test_read_only_tenant_user_is_refused(self, db) -> None:  # noqa: ARG002
        """403 而非 404：角色不够是权限边界，不是存在性问题"""
        with pytest.raises(PermissionDeniedError):
            await list_users(page=1, page_size=50, _current_user=await caller("acme_user"))


def _min_role_of(endpoint: Any) -> Optional[Role]:
    try:
        return inspect.getclosurevars(endpoint).nonlocals.get("min_role")
    except TypeError:
        return None


def _routes() -> Sequence[Any]:
    return [route for route in user_manager_router.routes if getattr(route, "endpoint", None) is not None]


class TestNoEndpointEscapesTheGuard:
    """事故形态是「8 个端点一路加下来，没有一个看目标」，所以约束必须钉在路由表上"""

    def test_every_user_scoped_endpoint_calls_the_guard(self) -> None:
        missing: List[str] = []
        for route in _routes():
            if "{user_id}" not in route.path:
                continue
            source = inspect.getsource(inspect.unwrap(route.endpoint))
            if "assert_managed_user_accessible" not in source:
                missing.append(route.endpoint.__name__)
        assert missing == []

    def test_list_filters_and_create_is_platform_only(self) -> None:
        """两个不吃 `{user_id}` 的端点各有各的处置"""
        by_name = {route.endpoint.__name__: route for route in _routes()}
        assert "get_tenant_scope" in inspect.getsource(inspect.unwrap(by_name["list_users"].endpoint))
        assert "require_platform_owner" in inspect.getsource(inspect.unwrap(by_name["create_user"].endpoint))

    async def test_create_is_refused_for_tenant_admin(self, db) -> None:  # noqa: ARG002
        """403 而非 404：这是"这个动作不属于你"，不涉及任何具体资源的存在性"""
        from nekro_agent.services.user.tenant_perm import require_platform_owner

        dependency = require_platform_owner()
        with pytest.raises(PermissionDeniedError):
            await dependency(current_user=await caller("acme_admin"))
        assert (await dependency(current_user=await caller("admin"))).username == "admin"
        # 端点签名确实挂着这个依赖，而不是只在源码里出现过
        assert inspect.signature(create_user).parameters["_current_user"].default.dependency.__qualname__ == (
            "require_platform_owner.<locals>.dependency"
        )
