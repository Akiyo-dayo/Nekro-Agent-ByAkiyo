"""客户备注（`DBTenant.note`）不进租户拿得到的响应体

## 这条为什么是本轮才要紧的

`GET /console/tenants/{tenant_id}` 的守卫是 `assert_tenant_accessible` ——
**租户看得到自己那一行**（看别人 404）。它一直如此，而 `TenantInfo` 一直带着 `note`：
那是平台方在「客户资料」卡片里写给自己看的备注（报价、催款、对接情况都可能落在这里），
不是客户的联系方式（那是 `contact`，客户本来就知道）。

round-19 之前这条没有实际泄漏面：整个前端里没有任何租户身份能到达的页面会调它
（`/console/tenants` 全路径都在 `PLATFORM_ONLY_PATHS` 里）。本轮新增的
`/console/team`（租户自助的「我的团队」）**每次打开都会调它一次** ——
页面不渲染 `note` 并不算数，响应体里有就是发出去了，浏览器的网络面板逐字可读。
判据与 #29「私钥不该出现在任何一次 HTTP 响应里」是同一条。

## 判据

1. 平台方**看得到**这段备注（正向对照：证明这条通道确实会回显它，
   否则一个"永远返回空"的坏接口也能让下面两条通过 —— PROJECT_STATE §2.11）；
2. 租户管理员与只读成员都拿到空串，且**除此之外逐字相同**
   （只清这一个字段，不是顺手把整个响应改窄）；
3. 越权仍是 404（不因为多了一个分支而把存在性泄露出去）；
4. `note` 只有这一个租户够得到的出口 —— 其余产出 `TenantInfo` 的路由必须是
   `require_platform_owner()`。这一条防的是"同一类资源的第二个出口"。
"""

import datetime
import inspect
from typing import Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console import tenants as tenants_router
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.tenant import TenantInfo, TenantRole, TenantStatus
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

# 平台方写给自己看的那段话。刻意不是空串：备注为空时平台方与租户的响应逐字相同，
# 「打了码」与「压根没有值」在那组输入上无法区分（§2.11 的原话）。
NOTE_SENTINEL = "内部备注-欠费两期-勿告知客户"
CONTACT = "acme@example.com"

PLATFORM_OWNER_DEP = "require_platform_owner.<locals>.dependency"


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


PLATFORM_OWNER = make_user("admin", tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)
ACME_ADMIN = make_user("acme_admin", tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)
ACME_MEMBER = make_user("acme_ro", tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)
RIVAL_ADMIN = make_user("rival_admin", tenant_id=RIVAL_TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)
# 挂着租户却写着 platform_owner 的脏行。正常路径造不出来（`create_tenant_member` 直接 400），
# 手改数据库或历史数据能。`is_platform_owner` 要求 `tenant_id is None` **且** 角色对得上，
# 只比角色的实现会把这一行判成平台方 —— 这一档就是钉住"复用现成判定，别另写一套"。
SPOOFED = make_user("spoofed", tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBTenant.create(
            id=ACME_TENANT_ID,
            name="acme_ltd",
            display_name="Acme 定制版",
            status=TenantStatus.ACTIVE.value,
            contact=CONTACT,
            note=NOTE_SENTINEL,
        )
        await DBTenant.create(
            id=RIVAL_TENANT_ID,
            name="other_corp",
            display_name="别家",
            status=TenantStatus.ACTIVE.value,
            contact="",
            note="别家的内部备注",
        )
        yield
    finally:
        await Tortoise.close_connections()


class TestTenantDetailHidesThePlatformNote:
    async def test_platform_owner_still_reads_the_note(self, db) -> None:
        """正向对照：这条通道确实会把备注回显出来

        没有这一条的话，下面两条断言可以被一个"永远返回空 note"的坏接口满足。
        """
        info = await tenants_router.get_tenant(ACME_TENANT_ID, PLATFORM_OWNER)
        assert info.note == NOTE_SENTINEL

    @pytest.mark.parametrize(
        "viewer",
        [ACME_ADMIN, ACME_MEMBER, SPOOFED],
        ids=["tenant_admin", "tenant_user", "tenant_row_claiming_platform_owner"],
    )
    async def test_tenant_side_gets_an_empty_note(self, db, viewer: DBUser) -> None:
        info = await tenants_router.get_tenant(ACME_TENANT_ID, viewer)
        assert info.note == ""
        assert NOTE_SENTINEL not in info.model_dump_json()

    @pytest.mark.parametrize(
        "viewer",
        [ACME_ADMIN, ACME_MEMBER, SPOOFED],
        ids=["tenant_admin", "tenant_user", "tenant_row_claiming_platform_owner"],
    )
    async def test_only_the_note_changes(self, db, viewer: DBUser) -> None:
        """只清这一个字段：其余每一项都要与平台方看到的逐字相同

        少了这一条，"把整个响应改窄"也能让上面那条通过，而
        `/console/team` 的页头正是靠 `display_name` / 计数 / 创建时间渲染的。
        """
        owner_view = (await tenants_router.get_tenant(ACME_TENANT_ID, PLATFORM_OWNER)).model_dump()
        tenant_view = (await tenants_router.get_tenant(ACME_TENANT_ID, viewer)).model_dump()
        assert set(tenant_view) == set(owner_view)
        differing = {key for key in owner_view if owner_view[key] != tenant_view[key]}
        assert differing == {"note"}
        # 客户自己的联系方式不属于"平台方内部"，照常下发
        assert tenant_view["contact"] == CONTACT

    async def test_cross_tenant_is_still_a_404(self, db) -> None:
        """多一个分支不能把越权判定弄松：别家的租户仍然是 404 而不是"一份没有备注的详情" """
        with pytest.raises(NotFoundError):
            await tenants_router.get_tenant(ACME_TENANT_ID, RIVAL_ADMIN)

    async def test_the_note_is_really_in_the_row(self, db) -> None:
        """对照组的另一半：固件真的写进去了

        固件没写进去（字段改名、默认值覆盖）的话，上面全部断言都恒成立而什么都没验。
        """
        assert (await DBTenant.get(id=ACME_TENANT_ID)).note == NOTE_SENTINEL


class TestTheNoteHasOnlyThisOneTenantReachableDoor:
    """同一类资源的第二个出口：还有谁会把 `TenantInfo` 交出去"""

    def test_every_other_tenant_info_route_is_platform_owner_only(self) -> None:
        leaky = []
        for route in tenants_router.router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue
            signature = inspect.signature(inspect.unwrap(endpoint))
            returns_tenant_info = signature.return_annotation in (TenantInfo, "TenantInfo") or "TenantListResponse" in str(
                signature.return_annotation,
            )
            if not returns_tenant_info:
                continue
            has_owner_dep = any(
                getattr(getattr(parameter.default, "dependency", None), "__qualname__", "") == PLATFORM_OWNER_DEP
                for parameter in signature.parameters.values()
            )
            if not has_owner_dep and inspect.unwrap(endpoint).__name__ != "get_tenant":
                leaky.append(f"{sorted(route.methods or [])} {route.path} -> {inspect.unwrap(endpoint).__name__}")
        assert not leaky, f"这些路由会把带 note 的 TenantInfo 交给非平台方：{leaky}"

    def test_the_criterion_can_see_the_routes_at_all(self) -> None:
        """§2.11：先证明这个扫描器确实找得到路由，否则 `leaky` 恒为空"""
        names = {
            inspect.unwrap(route.endpoint).__name__ for route in tenants_router.router.routes if hasattr(route, "endpoint")
        }
        assert {"get_tenant", "list_tenants", "create_tenant", "update_tenant"} <= names
