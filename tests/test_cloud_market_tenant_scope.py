"""云端市场（`routers/cloud/`）的租户边界

这一族端点全部要用**平台方那一把** `NEKRO_CLOUD_API_KEY` 打上游，所以判定的分界线是
"返回/写入的东西是谁的"：

- 平台方社区账号本身的资料、收藏、发布物 —— 平台方专属，由
  `tests/test_route_permission_contract.py` 的 `PLATFORM_ONLY` 结构断言钉住角色门槛，
  这里不重复；
- **人设市场的浏览与下载** —— 落地物是一条本地 `DBPreset`，它带 `owner_instance_key`
  归属列，租户对它有正当份额。这是本组唯一需要作用域（而不是角色门槛）的地方，
  本模块钉的就是它。

改前实测（租户管理员身份，真实 HTTP）：

    GET  /api/cloud/presets-market/list                          -> 200，`instance_key` 留空
                                                                    即退化成平台方全局视图，
                                                                    `is_local` 按**全部**本地人设算
    GET  /api/cloud/presets-market/list?instance_key=<别人的>    -> 200，照单全收
    POST /api/cloud/presets-market/download/{id}                 -> 200，人设落成
                                                                    `owner_instance_key=""`
                                                                    （`GLOBAL_PRESET_OWNER`），
                                                                    即租户单方面往全体共享的
                                                                    人设库里塞内容

`is_local` 看着只是个布尔值，但它是一条可枚举的存在性信道：拿云端人设 ID 逐个问
"本部署下过没有"，就能把别的租户的人设库探出来。所以它必须按作用域算。
"""

import datetime
from typing import Any, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_preset import GLOBAL_PRESET_OWNER, DBPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.cloud import presets_market
from nekro_agent.schemas.errors import ConflictError, NotFoundError, ValidationError
from nekro_agent.schemas.i18n import SupportedLang
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2
REMOTE_ID = "remote-1"


class _CloudObject:
    """上游云端响应的替身：字段形状与真实 schema 一致，只是不走网络"""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


CLOUD_PRESET = _CloudObject(
    id=REMOTE_ID,
    name="cloud-persona",
    title="Cloud Persona",
    avatar="data:image/png;base64,x",
    content="hello",
    description="desc",
    tags="a,b",
    author="upstream",
    created_at="2026-01-01",
    updated_at="2026-01-02",
    favorite_count=3,
    # 取 True 是为了让"平台方社区账号的收藏状态有没有漏给租户"这件事在断言里可分辨：
    # 漏了是 True，没漏是 None（见 TestPlatformAccountFavoriteStateStaysPlatformSide）。
    isFavorited=True,
    is_owner=False,
    ext_data=None,
)

# 同一条云端人设，但**这把部署级云端 Key 就是它的发布者**。
# 用独立的 remote_id，免得与 fixture 里那条 rival1 私有副本撞上重复判定。
OWNED_REMOTE_ID = "remote-owned-by-this-deployment"
OWNED_CLOUD_PRESET = _CloudObject(**{**CLOUD_PRESET.__dict__, "id": OWNED_REMOTE_ID, "is_owner": True})

CLOUD_LIST = _CloudObject(
    success=True, error=None, message=None,
    data=_CloudObject(total=1, items=[CLOUD_PRESET]),
)

CLOUD_DETAIL = _CloudObject(success=True, error=None, message=None, data=CLOUD_PRESET)


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        username=f"user-{tenant_role.value}-{tenant_id}",
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


def single_tenant_admin() -> DBUser:
    """未启用多租户的部署里的普通管理员：无租户归属、角色为平台方"""
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


UNRESTRICTED_IDENTITIES = [
    pytest.param(platform_owner, id="platform-owner"),
    pytest.param(single_tenant_admin, id="single-tenant"),
]

# 租户传得出来的每一种"不是自己实例"的作用域，对外必须长得一模一样
FOREIGN_SCOPES = ["rival1", "ghost-instance"]


async def list_market(user: DBUser, scope: str = "") -> Any:
    """直调端点函数时 `Query(...)` 默认值不会被 FastAPI 解开，分页参数必须显式给"""
    return await presets_market.get_cloud_presets_list(
        page=1, page_size=12, keyword=None, tag=None, instance_key=scope, _current_user=user,
    )


async def market_detail(user: DBUser, scope: str = "") -> Any:
    return await presets_market.get_preset_detail(
        remote_id=REMOTE_ID, instance_key=scope, _current_user=user,
    )


async def download(user: DBUser, scope: str = "") -> Any:
    return await presets_market.download_preset(
        remote_id=REMOTE_ID, instance_key=scope, _current_user=user,
    )


MARKET_CALLS = [
    pytest.param(list_market, id="list"),
    pytest.param(market_detail, id="detail"),
    pytest.param(download, id="download"),
]


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    async def fake_list_presets(**_kwargs: Any) -> Any:
        return CLOUD_LIST

    async def fake_get_preset(_remote_id: str) -> Any:
        return CLOUD_DETAIL

    monkeypatch.setattr(presets_market, "list_presets", fake_list_presets)
    monkeypatch.setattr(presets_market, "get_preset", fake_get_preset)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, tenant_id in (("inst1", ACME_TENANT_ID), ("rival1", RIVAL_TENANT_ID)):
            await DBAdapterInstance.create(
                adapter_key="onebot_v11", instance_key=instance_key, display_name=instance_key,
                status="online", provider="remote_onebot", tenant_id=tenant_id, metadata_json="",
            )
        # 别的租户已经下载过同一条云端人设，且是它的私有副本
        await DBPreset.create(
            remote_id=REMOTE_ID, on_shared=False, name="rival-copy", title="rival",
            avatar="", content="c", description="d", tags="", author="rival",
            ext_data={}, owner_instance_key="rival1",
        )
        yield
    finally:
        await Tortoise.close_connections()


class TestTenantMustNameItsOwnScope:
    @pytest.mark.parametrize("call", MARKET_CALLS)
    async def test_empty_scope_is_rejected_for_tenants(self, db, call) -> None:  # noqa: ARG002
        """空作用域对租户不能再退化成"全局视图"

        平台方一侧空串就是全局视图，所以不能简单地把空串当非法值；判据必须是身份。
        """
        with pytest.raises(ValidationError):
            await call(acme_admin())

    @pytest.mark.parametrize("call", MARKET_CALLS)
    async def test_foreign_scope_is_indistinguishable_from_absent(self, db, call) -> None:  # noqa: ARG002
        """别人的实例与压根不存在的实例，回给租户的必须是同一句话

        分得开就等于把平台方的实例清单变成可枚举的。
        """
        messages: List[str] = []
        for scope in FOREIGN_SCOPES:
            with pytest.raises(NotFoundError) as exc_info:
                await call(acme_admin(), scope)
            messages.append(exc_info.value.get_message(SupportedLang.ZH_CN))
        assert messages == ["实例 'rival1'不存在", "实例 'ghost-instance'不存在"]


class TestIsLocalDoesNotLeakOtherInstances:
    async def test_list_hides_another_instances_private_copy(self, db) -> None:  # noqa: ARG002
        listing = await list_market(acme_admin(), "inst1")
        assert [(item.remote_id, item.is_local) for item in listing.items] == [(REMOTE_ID, False)]

    async def test_detail_hides_another_instances_private_copy(self, db) -> None:  # noqa: ARG002
        detail = await market_detail(acme_admin(), "inst1")
        assert detail.is_local is False

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_global_view_still_sees_everything(self, db, identity) -> None:  # noqa: ARG002
        """平台方与未启用多租户的部署：空作用域仍是全局视图，`is_local` 逐字不变"""
        listing = await list_market(identity())
        assert [(item.remote_id, item.is_local) for item in listing.items] == [(REMOTE_ID, True)]


class TestDownloadLandsInTheCallersScope:
    async def test_tenant_download_is_private_to_its_own_instance(self, db) -> None:  # noqa: ARG002
        """租户下载的人设归它自己的实例，不能落成全体共享的那一份

        顺带钉住重复判定按**作用域可见性**算：别的租户已有的私有副本不该把本实例挡在
        门外（写成"全表查一次 remote_id"的表现就是租户永远下不了别人下过的人设）。
        """
        await download(acme_admin(), "inst1")
        owners = await DBPreset.filter(remote_id=REMOTE_ID).order_by("id").values_list("owner_instance_key", flat=True)
        assert list(owners) == ["rival1", "inst1"]
        assert GLOBAL_PRESET_OWNER not in owners

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_global_download_still_conflicts_on_any_existing_copy(self, db, identity) -> None:  # noqa: ARG002
        """全局视图下可见性覆盖全部人设，因此别人的副本仍然算"已存在"——改前即如此"""
        with pytest.raises(ConflictError):
            await download(identity())


class TestDownloadedCopyIsNotAHandleOnThePlatformPublication:
    """落进实例作用域的副本不能继承"这是我们那条云端发布"的标记

    `on_shared` 在本地的含义是"这一行就是我们那条云端发布的本地源"，而云端返回的
    `is_owner` 说的是"这把部署级 Key 是该发布的所有者"。两者混同 + 落地作用域收窄成
    实例私有（=该实例**可写**）就凑齐了一条完整的接管链，实测（进程内 ASGI 靶场，
    租户管理员真实 JWT）：

        POST /api/cloud/presets-market/download/<平台方发布的人设>?instance_key=inst1
            -> 200，本地行 owner_instance_key='inst1', on_shared=True
        PUT  /api/presets/{id}   {"content": "TENANT CONTROLLED CONTENT", ...}
            -> 200
        POST /api/presets/{id}/sync-to-cloud?instance_key=inst1
            -> 200，且真的调到了 cloud_update_preset(<平台方的 remote_id>)
               —— 用平台方的 Key 改写了社区里的公开发布内容
        POST /api/presets/{id}/unshare?instance_key=inst1
            -> 200，且真的调到了 cloud_delete_preset(<平台方的 remote_id>)
               —— 直接把平台方的公开发布下架

    这与 `PUT`/`DELETE /cloud/plugins-market/plugin/{module_name}` 被收成平台方专属
    是同一条理由。修法是两处一起收：实例作用域下的副本一律 `on_shared=False`（本测试），
    加上 `POST /presets/refresh-shared-status` 收归平台方（否则下载完刷一次就掰回
    `True`，链子原样复活）。
    """

    @pytest.mark.parametrize(
        ("identity", "scope", "expected_owner", "expected_on_shared"),
        [
            pytest.param(acme_admin, "inst1", "inst1", False, id="tenant-instance-scope"),
            pytest.param(platform_owner, "", GLOBAL_PRESET_OWNER, True, id="platform-global-view"),
            pytest.param(single_tenant_admin, "", GLOBAL_PRESET_OWNER, True, id="single-tenant-global-view"),
        ],
    )
    async def test_on_shared_follows_the_landing_scope_not_the_cloud_owner_flag(
        self, db, monkeypatch: pytest.MonkeyPatch, identity, scope, expected_owner, expected_on_shared,  # noqa: ARG002
    ) -> None:
        async def owned_preset(_remote_id: str) -> Any:
            return _CloudObject(success=True, error=None, message=None, data=OWNED_CLOUD_PRESET)

        monkeypatch.setattr(presets_market, "get_preset", owned_preset)

        await presets_market.download_preset(
            remote_id=OWNED_REMOTE_ID, instance_key=scope, _current_user=identity(),
        )

        landed = await DBPreset.get(remote_id=OWNED_REMOTE_ID)
        assert (landed.owner_instance_key, landed.on_shared) == (expected_owner, expected_on_shared)


class TestPlatformAccountFavoriteStateStaysPlatformSide:
    """`isFavorited` 是平台方社区账号的收藏状态，不是调用方的

    `GET /cloud/favorites` 已经收成平台方专属，但人设市场的列表/详情原样把同一份状态
    逐条回给租户 —— 逐页翻一遍就能把平台方的人设收藏清单重建出来，等于那道门槛没关上。
    `favoriteCount` 是云端条目的公开总数，不在此列，两边都照常返回。
    """

    async def test_tenant_gets_no_platform_favorite_state(self, db) -> None:  # noqa: ARG002
        listing = await list_market(acme_admin(), "inst1")
        detail = await market_detail(acme_admin(), "inst1")
        assert [(item.is_favorited, item.favorite_count) for item in listing.items] == [(None, 3)]
        assert (detail.is_favorited, detail.favorite_count) == (None, 3)

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_platform_side_is_unchanged(self, db, identity) -> None:  # noqa: ARG002
        """平台方与未启用多租户的部署逐字不变"""
        listing = await list_market(identity())
        detail = await market_detail(identity())
        assert [(item.is_favorited, item.favorite_count) for item in listing.items] == [(True, 3)]
        assert (detail.is_favorited, detail.favorite_count) == (True, 3)
