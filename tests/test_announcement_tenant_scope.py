"""机器人公告与平台方收件箱的租户作用域

公告是整个后台**唯一一条"能往别人群里发消息"**的批量写入面，改前那 8 个端点
一个作用域都没有：租户管理员提交 `scope_type=all` 就会打进平台方与其它租户的全部活跃群。
`routers/email.py` 那 4 条则是另一种形态 —— 它们根本没有可收敛的租户维度。

这里钉住四件事：

1. **范围在进入服务层之前就被收紧。** `announcement_service` 是按 (适配器, 实例) 分组
   并发投递的，它拿到什么就发什么，所以过滤必须发生在 `_scope_within_reach`，
   而不是靠服务层事后补救。
2. **归属从"它打到了谁的实例上"反推。** 公告表没有租户列（历史行也不可能有），
   判据是"申报范围 ∪ 已落库的投递明细"；`scope_type=all` 恒判平台方。
3. **越权与不存在不可区分。** 单条读/取消/订阅一律 404；废弃的批量发送里，
   别人的 `chat_key` 与压根不存在的 `chat_key` 得到逐字相同的那一条错误。
4. **平台方与未启用多租户的部署一个分支都不进。**

`tests/test_route_permission_contract.py` 只能钉住"结构上接了守卫"，
所以这里补的是"守卫真的按预期判"。
"""

import datetime
import json
from typing import Callable, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_announcement import DBAnnouncement, DBAnnouncementDelivery
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_email import DBEmail
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.chat_channel import (
    AnnouncementScopeRequest,
    AnnouncementSendRequest,
    AnnouncementSubmitRequest,
    _scope_within_reach,
    cancel_bot_announcement,
    get_bot_announcement,
    list_announcement_instances,
    list_bot_announcements,
    preview_bot_announcement,
    send_bot_announcement,
    submit_bot_announcement,
)
from nekro_agent.routers.email import get_email_detail, get_emails
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.announcement import AnnouncementInstanceRef, AnnouncementScope
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.tenant_perm import require_platform_owner

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
FOREIGN_INSTANCE = "rival1"
PLATFORM_INSTANCE = "plat1"

# (instance_key, tenant_id)：acme 名下只有 inst1，与测试服务器上的形态一致
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


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


def single_tenant_admin() -> DBUser:
    """未启用多租户的部署里的普通管理员：无租户归属、角色为平台方"""
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Admin)


UNRESTRICTED_IDENTITIES = [
    pytest.param(platform_owner, id="platform-owner"),
    pytest.param(single_tenant_admin, id="single-tenant"),
]

# 三条历史公告，对应归属推导的三种输入：
# 申报了实例 / 申报了频道且留有投递明细 / 范围是 all
ACME_ANNOUNCEMENT_ID = 1
RIVAL_ANNOUNCEMENT_ID = 2
PLATFORM_ALL_ANNOUNCEMENT_ID = 3


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, tenant_id in INSTANCE_LAYOUT:
            await DBAdapterInstance.create(
                adapter_key=ADAPTER, instance_key=instance_key, display_name=instance_key,
                status="online", provider="remote_onebot", tenant_id=tenant_id, metadata_json="",
            )
            await DBChatChannel.create(
                adapter_key=ADAPTER, instance_key=instance_key, channel_id=f"group_{instance_key}",
                chat_key=chat_key_of(instance_key), channel_name=f"{instance_key} 的群",
                channel_type="group", data="{}", is_active=True,
            )

        await DBAnnouncement.create(
            id=ACME_ANNOUNCEMENT_ID, content="acme 的公告", scope_type="instances",
            scope_value=json.dumps([{"adapter_key": ADAPTER, "instance_key": OWN_INSTANCE}]),
            status="completed", total=1, success_count=1, created_by="acme_admin",
        )
        await DBAnnouncement.create(
            id=RIVAL_ANNOUNCEMENT_ID, content="rival 的公告", scope_type="channels",
            scope_value=json.dumps([chat_key_of(FOREIGN_INSTANCE)]),
            status="completed", total=1, success_count=1, created_by="rival_admin",
        )
        await DBAnnouncementDelivery.create(
            announcement_id=RIVAL_ANNOUNCEMENT_ID, chat_key=chat_key_of(FOREIGN_INSTANCE),
            channel_name="rival 的群", adapter_key=ADAPTER, instance_key=FOREIGN_INSTANCE, ok=True,
        )
        await DBAnnouncement.create(
            id=PLATFORM_ALL_ANNOUNCEMENT_ID, content="平台方全站公告", scope_type="all", scope_value="[]",
            status="completed", total=3, success_count=3, created_by="platform_admin",
        )

        await DBEmail.create(
            account_username="ops@example.com", email_uid="1001",
            subject="[重置密码] 验证码 998877", sender="noreply@bank.example",
            recipients='["ops@example.com"]', body_text="验证码 998877",
            has_attachments=False, attachment_names="[]",
        )
        yield
    finally:
        await Tortoise.close_connections()


class TestScopeIsNarrowedBeforeItReachesTheService:
    """范围收敛发生在服务层之前"""

    async def test_tenant_all_scope_becomes_its_own_instances(self, db) -> None:
        """`all` 必须被改写成显式实例清单

        不改写的话 `scope_value` 落库就是空数组，事后无从判断这条公告覆盖了谁，
        历史列表与取消接口就再也判不出归属。
        """
        narrowed = await _scope_within_reach(acme_admin(), AnnouncementScope(scope_type="all"))

        assert narrowed.scope_type == "instances"
        assert [(ref.adapter_key, ref.instance_key) for ref in narrowed.instances] == [(ADAPTER, OWN_INSTANCE)]

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identities_keep_the_scope_verbatim(self, db, identity: Callable[[], DBUser]) -> None:
        original = AnnouncementScope(scope_type="all")

        assert await _scope_within_reach(identity(), original) is original

    @pytest.mark.parametrize(
        ("scope", "expected_resource"),
        [
            pytest.param(
                AnnouncementScope(scope_type="channels", chat_keys=[chat_key_of(FOREIGN_INSTANCE)]),
                "聊天频道",
                id="foreign-channel",
            ),
            pytest.param(
                AnnouncementScope(scope_type="channels", chat_keys=[f"{ADAPTER}-nope-group_nope"]),
                "聊天频道",
                id="missing-channel",
            ),
            pytest.param(
                AnnouncementScope(
                    scope_type="instances",
                    instances=[AnnouncementInstanceRef(adapter_key=ADAPTER, instance_key=FOREIGN_INSTANCE)],
                ),
                f"实例 '{FOREIGN_INSTANCE}'",
                id="foreign-instance",
            ),
            pytest.param(
                AnnouncementScope(scope_type="channels", chat_keys=[chat_key_of(PLATFORM_INSTANCE)]),
                "聊天频道",
                id="platform-channel",
            ),
        ],
    )
    async def test_out_of_reach_targets_are_rejected_as_missing(
        self, db, scope: AnnouncementScope, expected_resource: str,
    ) -> None:
        """越权目标与不存在的目标抛同一个 404

        `foreign-channel` 与 `missing-channel` 拿到逐字相同的错误 ——
        403 会让公告接口变成枚举别的租户频道的探针。
        """
        with pytest.raises(NotFoundError) as excinfo:
            await _scope_within_reach(acme_admin(), scope)

        assert excinfo.value.params["resource"] == expected_resource

    async def test_preview_counts_only_reachable_channels(self, db) -> None:
        """预览与提交走同一条收敛，否则预览会把别的租户的会话数报出来"""
        preview = await preview_bot_announcement(
            AnnouncementScopeRequest(scope=AnnouncementScope(scope_type="all")),
            _current_user=acme_admin(),
        )

        assert preview.channel_count == 1
        assert [option.instance_key for option in preview.instances] == [OWN_INSTANCE]

    async def test_platform_preview_still_covers_everything(self, db) -> None:
        preview = await preview_bot_announcement(
            AnnouncementScopeRequest(scope=AnnouncementScope(scope_type="all")),
            _current_user=platform_owner(),
        )

        assert preview.channel_count == len(INSTANCE_LAYOUT)

    async def test_submitting_into_a_foreign_group_never_reaches_the_service(self, db) -> None:
        """这是这一族最危险的那条路径：提交即投递，拦不住就是往别人群里说话了"""
        before = await DBAnnouncement.all().count()

        with pytest.raises(NotFoundError):
            await submit_bot_announcement(
                AnnouncementSubmitRequest(
                    content="打进别人的群",
                    scope=AnnouncementScope(scope_type="channels", chat_keys=[chat_key_of(FOREIGN_INSTANCE)]),
                ),
                _current_user=acme_admin(),
            )

        assert await DBAnnouncement.all().count() == before

    async def test_instance_options_are_filtered(self, db) -> None:
        tenant_options = await list_announcement_instances(_current_user=acme_admin())
        platform_options = await list_announcement_instances(_current_user=platform_owner())

        assert [option.instance_key for option in tenant_options.items] == [OWN_INSTANCE]
        assert sorted(option.instance_key for option in platform_options.items) == sorted(
            instance_key for instance_key, _ in INSTANCE_LAYOUT
        )


class TestOwnershipIsDerivedFromTheTargetedInstances:
    """归属从"它打到了谁的实例上"反推"""

    async def test_history_shows_only_announcements_confined_to_own_instances(self, db) -> None:
        """rival 的那条只有投递明细能定位（申报的是 chat_key），all 的那条判平台方"""
        listed = await list_bot_announcements(_current_user=acme_admin())

        assert listed.total == 1
        assert [item.id for item in listed.items] == [ACME_ANNOUNCEMENT_ID]

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_history_is_unfiltered_for_unrestricted_identities(
        self, db, identity: Callable[[], DBUser],
    ) -> None:
        listed = await list_bot_announcements(_current_user=identity())

        assert listed.total == 3
        assert [item.id for item in listed.items] == [
            PLATFORM_ALL_ANNOUNCEMENT_ID,
            RIVAL_ANNOUNCEMENT_ID,
            ACME_ANNOUNCEMENT_ID,
        ]

    @pytest.mark.parametrize(
        "announcement_id",
        [
            pytest.param(RIVAL_ANNOUNCEMENT_ID, id="another-tenant"),
            pytest.param(PLATFORM_ALL_ANNOUNCEMENT_ID, id="scope-all"),
            pytest.param(9999, id="missing"),
        ],
    )
    async def test_reading_and_cancelling_out_of_reach_announcements_is_404(
        self, db, announcement_id: int,
    ) -> None:
        for endpoint in (get_bot_announcement, cancel_bot_announcement):
            with pytest.raises(NotFoundError):
                await endpoint(announcement_id, _current_user=acme_admin())

    async def test_unknown_scope_type_is_platform_only_instead_of_breaking_the_list(self, db) -> None:
        """`scope_type` 落到枚举之外时判平台方专属，而不是让租户的整份历史 500

        租户一侧的归属推导是对**全表**逐行做的，所以这一行必须在进入 `load_scope`
        （里面的 `AnnouncementScopeType(...)` 会抛 `ValueError`）之前就被判掉；
        平台方一侧不走这条推导，行为不变。
        """
        await DBAnnouncement.create(
            content="版本回退后读到的未知范围类型", scope_type="regions", scope_value="[]", status="completed",
        )

        listed = await list_bot_announcements(_current_user=acme_admin())

        assert [item.id for item in listed.items] == [ACME_ANNOUNCEMENT_ID]

    async def test_own_announcement_passes_the_guard(self, db) -> None:
        """自己的那条读得到；取消它落在业务判定上（已终态），说明卡住它的不是作用域"""
        detail = await get_bot_announcement(ACME_ANNOUNCEMENT_ID, _current_user=acme_admin())
        assert detail.id == ACME_ANNOUNCEMENT_ID

        with pytest.raises(ValidationError):
            await cancel_bot_announcement(ACME_ANNOUNCEMENT_ID, _current_user=acme_admin())


class TestDeprecatedBatchSend:
    """废弃的同步批量发送同样不能成为越权投递或频道枚举的入口"""

    async def test_foreign_and_missing_channels_are_indistinguishable(self, db) -> None:
        response = await send_bot_announcement(
            AnnouncementSendRequest(
                chat_keys=[chat_key_of(FOREIGN_INSTANCE), f"{ADAPTER}-nope-group_nope"],
                message="越权群发",
            ),
            _current_user=acme_admin(),
        )

        assert response.failure_count == 2
        assert {(item.ok, item.error, item.channel_name) for item in response.results} == {
            (False, "聊天频道不存在", None),
        }


class TestPlatformMailboxIsOwnerOnly:
    """平台方收件箱没有可收敛的租户维度，只能是平台方专属

    收件账号存在 email 适配器的适配器层配置里（跨租户共享的作用域），
    且 email 是单实例适配器 —— `DBEmail` 里没有任何一列能把某封邮件判给某个租户。
    """

    async def test_tenant_identity_is_refused(self, db) -> None:
        with pytest.raises(PermissionDeniedError):
            await require_platform_owner()(current_user=acme_admin())

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identities_still_read_the_mailbox(
        self, db, identity: Callable[[], DBUser],
    ) -> None:
        """未启用多租户的部署里 `require_platform_owner()` 恒放行，行为逐字不变"""
        user = identity()
        assert await require_platform_owner()(current_user=user) is user

        # 直接调用端点函数时 FastAPI 的 `Query(...)` 默认值不会被解析，分页参数要显式给
        listed = await get_emails(offset=0, limit=25, account=None, _current_user=user, _owner=user)
        detail = await get_email_detail(1, _current_user=user, _owner=user)

        assert [item.subject for item in listed.items] == ["[重置密码] 验证码 998877"]
        assert detail.body_text == "验证码 998877"


class TestAnnouncementScopeRoundTrips:
    """留档的 `scope_value` 必须能原样还原，否则归属推导会静默判错"""

    @pytest.mark.parametrize(
        "scope",
        [
            pytest.param(AnnouncementScope(scope_type="channels", chat_keys=["a", "b"]), id="channels"),
            pytest.param(
                AnnouncementScope(
                    scope_type="instances",
                    instances=[AnnouncementInstanceRef(adapter_key=ADAPTER, instance_key=OWN_INSTANCE)],
                ),
                id="instances",
            ),
            pytest.param(AnnouncementScope(scope_type="all"), id="all"),
        ],
    )
    async def test_dump_then_load_is_lossless(self, db, scope: AnnouncementScope) -> None:
        from nekro_agent.services.announcement import announcement_service

        announcement = await DBAnnouncement.create(
            content="round trip",
            scope_type=scope.scope_type.value,
            scope_value=announcement_service._dump_scope_value(scope),  # noqa: SLF001
            status="completed",
        )

        assert announcement_service.load_scope(announcement) == scope

    async def test_malformed_scope_value_degrades_instead_of_raising(self, db) -> None:
        """一条脏数据不该让整个历史列表 500"""
        from nekro_agent.services.announcement import announcement_service

        announcement = await DBAnnouncement.create(
            content="broken", scope_type="instances", scope_value='[{"nope": 1}, "kept"]', status="completed",
        )

        loaded = announcement_service.load_scope(announcement)

        assert loaded.instances == []
        assert loaded.chat_keys == ["kept"]


async def resolve_mailbox_guard(user: DBUser) -> DBUser:
    """按 FastAPI 的顺序跑完邮箱适配器 router 的整条依赖链

    两层缺一不可，所以不能只调其中一个：外层 `require_platform_owner()` 挡租户身份，
    内层的角色下限挡未启用多租户的部署里自助注册出来的 `Role.User`。
    """
    from nekro_agent.adapters.email.routers import require_platform_mailbox_admin

    return await require_platform_mailbox_admin(_owner=await require_platform_owner()(current_user=user))


class TestEmailAdapterRouterCannotBypassTheMailboxGuard:
    """`/api/adapters/email/*` 必须与 `routers/email.py` 同一档门槛

    这一族曾经**完全不鉴权**（`adapters/__init__.py:load_adapters_api` 与
    `routers/__init__.py:152` 都是无依赖挂载，`mount_middlewares` 只有 CORS + 计时）。
    实测匿名请求可以读出收件账号、增删收件账号，`POST /raw-email` 返回的更是
    `GET /emails/{id}/raw-content` 的同一份数据 —— 把上面那四条锁成平台方专属，
    却留着这条路，等于没锁。

    钉两件事：**每一条**路由都在门槛之后（新增端点自动被覆盖），以及门槛本身按预期判。
    """

    def test_every_route_sits_behind_the_guard(self) -> None:
        from nekro_agent.adapters.email.routers import require_platform_mailbox_admin
        from nekro_agent.adapters.email.routers import router as email_adapter_router

        unguarded = [
            f"{sorted(route.methods or [])} {route.path}"
            for route in email_adapter_router.routes
            if not any(
                dependency.dependency is require_platform_mailbox_admin
                for dependency in getattr(route, "dependencies", [])
            )
        ]

        assert unguarded == []
        assert len(email_adapter_router.routes) >= 13

    @pytest.mark.parametrize("identity", UNRESTRICTED_IDENTITIES)
    async def test_unrestricted_identities_keep_access(self, db, identity: Callable[[], DBUser]) -> None:
        """平台方与未启用多租户的部署里的管理员照常可用"""
        user = identity()

        assert await resolve_mailbox_guard(user) is user

    @pytest.mark.parametrize(
        "user_factory",
        [
            pytest.param(acme_admin, id="tenant-admin"),
            pytest.param(
                lambda: make_user(
                    tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User,
                ),
                id="tenant-user",
            ),
            pytest.param(
                lambda: make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.User),
                id="single-tenant-role-user",
            ),
        ],
    )
    async def test_tenants_and_low_privilege_accounts_are_refused(
        self, db, user_factory: Callable[[], DBUser],
    ) -> None:
        """租户身份被 `require_platform_owner` 挡下；自助注册的 `Role.User` 被角色下限挡下

        后者与 `routers/email.py` 把 `Role.User` 提到 `Role.Admin` 是同一条理由：
        收件箱里有密码重置邮件与验证码。
        """
        with pytest.raises(PermissionDeniedError):
            await resolve_mailbox_guard(user_factory())
