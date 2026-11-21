"""频道每日额度的控制台接口

两类断言混在一起：

- **越权**：租户碰不到平台方那一组写接口；碰到别人的实例与频道一律 404（不是 403，
  403 会把平台方的实例清单变成可枚举的）。
- **口径**：三层继承在接口上读得到、写进去的分层没串；租户值不能超过平台方的上限；
  列表的作用域、筛选、排序与分页都对；历史里看不到任何费用字段。
"""

import datetime
import inspect
from typing import Any, Optional

import pytest
from pydantic import ValidationError as PydanticValidationError
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_llm_usage import DBLLMUsageRecord
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console.channel_quota import (
    add_channel_quota_bonus,
    get_channel_quota_history,
    get_channel_quota_policy,
    get_channel_quota_status,
    get_global_quota_policy,
    get_tenant_channel_quota,
    get_tenant_instance_quota,
    list_channel_quota_daily,
    update_channel_quota_policy,
    update_global_quota_policy,
    update_instance_quota_policy,
    update_tenant_channel_quota,
    update_tenant_instance_quota,
)
from nekro_agent.schemas.channel_quota import (
    ChannelQuotaBonusRequest,
    GlobalQuotaPolicyUpdate,
    ScopedQuotaPolicyUpdate,
    TenantChannelQuotaUpdate,
    TenantInstanceQuotaUpdate,
)
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota.policy import SCOPE_GLOBAL, SCOPE_INSTANCE
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

ACME_INSTANCE = "acme-1"
ACME_SECOND_INSTANCE = "acme-2"
RIVAL_INSTANCE = "rival-1"

ACME_CHAT = f"{ADAPTER}-{ACME_INSTANCE}-group_210001"
ACME_SECOND_CHAT = f"{ADAPTER}-{ACME_SECOND_INSTANCE}-group_210002"
RIVAL_CHAT = f"{ADAPTER}-{RIVAL_INSTANCE}-group_210003"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    policy_module.invalidate_policy_cache()
    try:
        await DBTenant.create(id=ACME_TENANT_ID, name="acme", display_name="Acme")
        await DBTenant.create(id=RIVAL_TENANT_ID, name="rival", display_name="Rival")
        for instance_key, tenant_id in (
            (ACME_INSTANCE, ACME_TENANT_ID),
            (ACME_SECOND_INSTANCE, ACME_TENANT_ID),
            (RIVAL_INSTANCE, RIVAL_TENANT_ID),
        ):
            await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
        for chat_key, instance_key, name in (
            (ACME_CHAT, ACME_INSTANCE, "Acme 群一"),
            (ACME_SECOND_CHAT, ACME_SECOND_INSTANCE, "Acme 群二"),
            (RIVAL_CHAT, RIVAL_INSTANCE, "Rival 群"),
        ):
            await DBChatChannel.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                channel_id=chat_key.rsplit("-", 1)[-1],
                chat_key=chat_key,
                channel_type="group",
                channel_name=name,
                data="{}",
            )
        yield
    finally:
        policy_module.invalidate_policy_cache()
        await Tortoise.close_connections()


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        id=abs(hash((tenant_id, tenant_role.value))) % 100000 + 1,
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


def acme_member() -> DBUser:
    return make_user(tenant_id=ACME_TENANT_ID, tenant_role=TenantRole.TENANT_USER, perm_level=Role.User)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


async def write_global(**overrides: Any) -> None:
    fields = {
        "enabled": True,
        "unit": "replies",
        "over_limit_action": "pause",
        "group_daily_limit": 100,
        "private_daily_limit": 20,
    }
    fields.update(overrides)
    await update_global_quota_policy(
        body=GlobalQuotaPolicyUpdate(**fields),
        _current_user=platform_owner(),
    )


# ---------------------------------------------------------------------------
# 三层策略的读写
# ---------------------------------------------------------------------------


class TestPlatformPolicyReadWrite:
    async def test_missing_global_row_reads_as_disabled_defaults(self, db) -> None:  # noqa: ARG002
        info = await get_global_quota_policy(_current_user=platform_owner())

        assert info.enabled is False
        assert info.unit == "replies"
        assert info.over_limit_action == "alert"
        assert (info.group_daily_limit, info.private_daily_limit) == (0, 0)

    async def test_global_write_is_visible_on_read(self, db) -> None:  # noqa: ARG002
        await write_global(unit="tokens", group_daily_limit=12345, pause_message="  今天歇了  ")

        info = await get_global_quota_policy(_current_user=platform_owner())

        assert info.enabled is True
        assert info.unit == "tokens"
        assert info.group_daily_limit == 12345
        assert info.pause_message == "今天歇了", "首尾空白要去掉"

    async def test_write_records_the_author(self, db) -> None:  # noqa: ARG002
        await write_global()

        info = await get_global_quota_policy(_current_user=platform_owner())

        assert info.updated_by == platform_owner().username

    async def test_instance_layer_starts_fully_inheriting(self, db) -> None:  # noqa: ARG002
        info = await update_instance_quota_policy(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=ScopedQuotaPolicyUpdate(),
            _current_user=platform_owner(),
        )

        assert (info.enabled, info.unit, info.over_limit_action) == (None, None, None)
        assert (info.group_daily_limit, info.private_daily_limit) == (None, None)

    async def test_instance_layer_can_override_one_column(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100)
        await update_instance_quota_policy(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=ScopedQuotaPolicyUpdate(group_daily_limit=40),
            _current_user=platform_owner(),
        )
        await update_channel_quota_policy(
            chat_key=ACME_CHAT,
            body=ScopedQuotaPolicyUpdate(group_daily_limit=10),
            _current_user=platform_owner(),
        )

        channel_info = await get_channel_quota_policy(chat_key=ACME_CHAT, _current_user=platform_owner())
        instance_info = await get_global_quota_policy(_current_user=platform_owner())

        assert channel_info.group_daily_limit == 10
        assert instance_info.group_daily_limit == 100, "写频道层不该改到全局"

    async def test_setting_null_clears_a_previous_override(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100)
        await update_instance_quota_policy(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=ScopedQuotaPolicyUpdate(group_daily_limit=40),
            _current_user=platform_owner(),
        )

        info = await update_instance_quota_policy(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=ScopedQuotaPolicyUpdate(group_daily_limit=None),
            _current_user=platform_owner(),
        )

        assert info.group_daily_limit is None

    async def test_write_invalidates_the_policy_cache(self, db) -> None:  # noqa: ARG002
        """平台方改完要立刻生效，不能等一分钟的 TTL"""
        from nekro_agent.services.channel_quota.policy import resolve_policy

        await write_global(group_daily_limit=100)
        assert (await resolve_policy(ADAPTER, ACME_INSTANCE, ACME_CHAT, "group")).effective_limit == 100

        await update_instance_quota_policy(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=ScopedQuotaPolicyUpdate(group_daily_limit=7),
            _current_user=platform_owner(),
        )

        assert (await resolve_policy(ADAPTER, ACME_INSTANCE, ACME_CHAT, "group")).effective_limit == 7

    async def test_missing_instance_is_404(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await update_instance_quota_policy(
                adapter_key=ADAPTER,
                instance_key="no-such-instance",
                body=ScopedQuotaPolicyUpdate(group_daily_limit=1),
                _current_user=platform_owner(),
            )

    async def test_missing_channel_is_404(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await get_channel_quota_policy(chat_key="no-such-chat", _current_user=platform_owner())


class TestPlatformPolicyValidation:
    """写不进去的值必须在入口就拒掉，而不是存进去之后由解析侧兜住"""

    def test_unit_must_be_one_of_the_two(self) -> None:
        with pytest.raises(PydanticValidationError):
            GlobalQuotaPolicyUpdate(enabled=True, unit="requests", over_limit_action="alert")

    def test_action_must_be_one_of_the_two(self) -> None:
        with pytest.raises(PydanticValidationError):
            GlobalQuotaPolicyUpdate(enabled=True, unit="replies", over_limit_action="block")

    def test_negative_limit_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ScopedQuotaPolicyUpdate(group_daily_limit=-1)

    def test_non_integer_limit_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ScopedQuotaPolicyUpdate(group_daily_limit=1.5)  # type: ignore[arg-type]

    def test_pause_message_over_the_cap_is_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ScopedQuotaPolicyUpdate(pause_message="字" * 201)

    def test_pause_message_at_the_cap_is_accepted(self) -> None:
        info = ScopedQuotaPolicyUpdate(pause_message="字" * 200)

        assert info.pause_message is not None
        assert len(info.pause_message) == 200

    def test_blank_pause_message_is_stored_as_null(self) -> None:
        """空串与 NULL 在库里是两个值，语义却只有一个「没说」"""
        assert ScopedQuotaPolicyUpdate(pause_message="   ").pause_message is None

    async def test_global_row_cannot_leave_the_switch_unset(self, db) -> None:  # noqa: ARG002
        """全局是最后一层，三个关键列都不允许留空"""
        with pytest.raises(PydanticValidationError):
            GlobalQuotaPolicyUpdate(unit="replies", over_limit_action="alert")  # type: ignore[call-arg]

    async def test_rejected_write_does_not_leave_a_half_row(self, db) -> None:  # noqa: ARG002
        with pytest.raises(PydanticValidationError):
            GlobalQuotaPolicyUpdate(enabled=True, unit="replies", over_limit_action="alert", group_daily_limit=-5)

        assert await DBChannelQuotaPolicy.all().count() == 0


class TestTenantCannotTouchPlatformSurface:
    """租户碰平台方那一组写接口一律 403

    平台方专属是靠 `Depends(require_platform_owner())` 表达的，直接调端点函数不会经过
    依赖注入，所以这里显式跑一遍那个依赖——断言的是**那道依赖真的拒绝租户**，
    而不是"函数体里恰好没写校验"。路由表上的分档由契约测试另外钉住。
    """

    @staticmethod
    async def _require_platform_owner(user: DBUser) -> None:
        from nekro_agent.services.user.tenant_perm import require_platform_owner

        dependency = require_platform_owner()
        await dependency(current_user=user)

    async def test_tenant_admin_is_rejected_by_the_platform_owner_dependency(self) -> None:
        with pytest.raises(PermissionDeniedError):
            await self._require_platform_owner(acme_admin())

    async def test_platform_owner_passes_the_dependency(self) -> None:
        await self._require_platform_owner(platform_owner())

    async def test_the_platform_only_endpoints_declare_that_dependency(self) -> None:
        """九条平台方专属端点每一条都挂着它；漏一条就等于把写承诺的能力交给租户"""
        from nekro_agent.routers.console import channel_quota as module

        platform_endpoints = [
            module.get_global_quota_policy,
            module.update_global_quota_policy,
            module.get_instance_quota_policy,
            module.update_instance_quota_policy,
            module.get_channel_quota_policy,
            module.update_channel_quota_policy,
            module.add_channel_quota_bonus,
            module.recount_channel_quota,
            module.recount_all_channel_quota,
        ]
        for endpoint in platform_endpoints:
            source = inspect.getsource(inspect.unwrap(endpoint))
            assert "require_platform_owner()" in source, endpoint.__name__


# ---------------------------------------------------------------------------
# 租户能调的那一半
# ---------------------------------------------------------------------------


class TestTenantInstanceQuota:
    async def test_reads_null_before_anything_is_set(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100, private_daily_limit=20)

        info = await get_tenant_instance_quota(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            _current_user=acme_admin(),
        )

        assert info.group_daily_limit is None
        assert info.unit == "replies"
        assert (info.platform_group_daily_limit, info.platform_private_daily_limit) == (100, 20)

    async def test_write_lowers_the_effective_limit(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.channel_quota.policy import resolve_policy

        await write_global(group_daily_limit=100)

        await update_tenant_instance_quota(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=TenantInstanceQuotaUpdate(group_daily_limit=30),
            _current_user=acme_admin(),
        )

        assert (await resolve_policy(ADAPTER, ACME_INSTANCE, ACME_CHAT, "group")).effective_limit == 30

    async def test_write_stamps_the_unit_snapshot(self, db) -> None:  # noqa: ARG002
        await write_global(unit="tokens", group_daily_limit=10000)
        await update_tenant_instance_quota(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=TenantInstanceQuotaUpdate(group_daily_limit=4000),
            _current_user=acme_admin(),
        )

        row = await DBChannelQuotaPolicy.filter(scope=SCOPE_INSTANCE, instance_key=ACME_INSTANCE).first()
        assert row is not None
        assert row.tenant_limit_unit == "tokens"

    async def test_tenant_value_above_the_platform_limit_is_rejected(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100)

        with pytest.raises(ValidationError):
            await update_tenant_instance_quota(
                adapter_key=ADAPTER,
                instance_key=ACME_INSTANCE,
                body=TenantInstanceQuotaUpdate(group_daily_limit=101),
                _current_user=acme_admin(),
            )

        row = await DBChannelQuotaPolicy.filter(scope=SCOPE_INSTANCE, instance_key=ACME_INSTANCE).first()
        assert row is None, "被拒的写入不能留下半行"

    async def test_rejection_names_the_ceiling(self, db) -> None:  # noqa: ARG002
        """报错要说清上限是多少，否则租户只能一次次试"""
        await write_global(group_daily_limit=100)

        with pytest.raises(ValidationError) as raised:
            await update_tenant_instance_quota(
                adapter_key=ADAPTER,
                instance_key=ACME_INSTANCE,
                body=TenantInstanceQuotaUpdate(group_daily_limit=500),
                _current_user=acme_admin(),
            )

        assert "100" in str(raised.value)

    async def test_platform_zero_allows_any_positive_value(self, db) -> None:  # noqa: ARG002
        """平台方不限时，租户自己设多少都行——那时他的值就是唯一的约束"""
        await write_global(group_daily_limit=0)

        info = await update_tenant_instance_quota(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=TenantInstanceQuotaUpdate(group_daily_limit=999999),
            _current_user=acme_admin(),
        )

        assert info.group_daily_limit == 999999

    async def test_zero_is_rejected_for_tenants(self, db) -> None:  # noqa: ARG002
        """0 在额度字段里表示「不限」，租户拿它一填就把自己的上限抹掉了"""
        await write_global(group_daily_limit=100)

        with pytest.raises(PydanticValidationError):
            TenantInstanceQuotaUpdate(group_daily_limit=0)

    async def test_clearing_returns_to_the_platform_limit(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.channel_quota.policy import resolve_policy

        await write_global(group_daily_limit=100)
        await update_tenant_instance_quota(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=TenantInstanceQuotaUpdate(group_daily_limit=30),
            _current_user=acme_admin(),
        )

        await update_tenant_instance_quota(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=TenantInstanceQuotaUpdate(group_daily_limit=None),
            _current_user=acme_admin(),
        )

        assert (await resolve_policy(ADAPTER, ACME_INSTANCE, ACME_CHAT, "group")).effective_limit == 100

    async def test_tenant_pause_message_is_stored_on_its_own_column(self, db) -> None:  # noqa: ARG002
        await write_global(pause_message="平台方的提示")
        await update_tenant_instance_quota(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=TenantInstanceQuotaUpdate(pause_message="我们群歇了"),
            _current_user=acme_admin(),
        )

        row = await DBChannelQuotaPolicy.filter(scope=SCOPE_GLOBAL).first()
        assert row is not None
        assert row.pause_message == "平台方的提示", "租户写不到平台方那一列"

        tenant_row = await DBChannelQuotaPolicy.filter(scope=SCOPE_INSTANCE).first()
        assert tenant_row is not None
        assert tenant_row.tenant_pause_message == "我们群歇了"

    async def test_foreign_instance_is_404(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await get_tenant_instance_quota(
                adapter_key=ADAPTER,
                instance_key=RIVAL_INSTANCE,
                _current_user=acme_admin(),
            )

    async def test_missing_instance_is_also_404(self, db) -> None:  # noqa: ARG002
        """不存在与无权访问必须不可区分"""
        with pytest.raises(NotFoundError):
            await get_tenant_instance_quota(
                adapter_key=ADAPTER,
                instance_key="no-such-instance",
                _current_user=acme_admin(),
            )

    async def test_platform_owner_may_use_the_tenant_endpoints(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100)

        info = await update_tenant_instance_quota(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=TenantInstanceQuotaUpdate(group_daily_limit=50),
            _current_user=platform_owner(),
        )

        assert info.group_daily_limit == 50

    async def test_read_only_member_is_rejected(self, db) -> None:  # noqa: ARG002
        """只读成员连用量页都看不到，额度页同理"""
        with pytest.raises(Exception):  # `require_role` 会抛 HTTPException
            await get_tenant_instance_quota(
                adapter_key=ADAPTER,
                instance_key=ACME_INSTANCE,
                _current_user=acme_member(),
            )


class TestTenantChannelQuota:
    async def test_write_goes_into_the_group_column(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100)

        await update_tenant_channel_quota(
            chat_key=ACME_CHAT,
            body=TenantChannelQuotaUpdate(daily_limit=5),
            _current_user=acme_admin(),
        )

        row = await DBChannelQuotaPolicy.filter(scope="channel", chat_key=ACME_CHAT).first()
        assert row is not None
        assert row.tenant_group_daily_limit == 5
        assert row.tenant_private_daily_limit is None

    async def test_write_goes_into_the_private_column(self, db) -> None:  # noqa: ARG002
        private_chat = f"{ADAPTER}-{ACME_INSTANCE}-private_210009"
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            channel_id="private_210009",
            chat_key=private_chat,
            channel_type="private",
            data="{}",
        )
        await write_global(private_daily_limit=50)

        await update_tenant_channel_quota(
            chat_key=private_chat,
            body=TenantChannelQuotaUpdate(daily_limit=5),
            _current_user=acme_admin(),
        )

        row = await DBChannelQuotaPolicy.filter(scope="channel", chat_key=private_chat).first()
        assert row is not None
        assert row.tenant_private_daily_limit == 5
        assert row.tenant_group_daily_limit is None

    async def test_channel_value_beats_the_instance_value(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.channel_quota.policy import resolve_policy

        await write_global(group_daily_limit=100)
        await update_tenant_instance_quota(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            body=TenantInstanceQuotaUpdate(group_daily_limit=40),
            _current_user=acme_admin(),
        )
        await update_tenant_channel_quota(
            chat_key=ACME_CHAT,
            body=TenantChannelQuotaUpdate(daily_limit=5),
            _current_user=acme_admin(),
        )

        assert (await resolve_policy(ADAPTER, ACME_INSTANCE, ACME_CHAT, "group")).effective_limit == 5
        assert (await resolve_policy(ADAPTER, ACME_INSTANCE, ACME_SECOND_CHAT, "group")).effective_limit == 40

    async def test_above_the_platform_limit_is_rejected(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100)

        with pytest.raises(ValidationError):
            await update_tenant_channel_quota(
                chat_key=ACME_CHAT,
                body=TenantChannelQuotaUpdate(daily_limit=101),
                _current_user=acme_admin(),
            )

    async def test_foreign_channel_is_404(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await get_tenant_channel_quota(chat_key=RIVAL_CHAT, _current_user=acme_admin())

    async def test_read_reports_both_limits(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100)
        await update_tenant_channel_quota(
            chat_key=ACME_CHAT,
            body=TenantChannelQuotaUpdate(daily_limit=5),
            _current_user=acme_admin(),
        )

        info = await get_tenant_channel_quota(chat_key=ACME_CHAT, _current_user=acme_admin())

        assert info.platform_limit == 100
        assert info.effective_limit == 5
        assert info.daily_limit == 5
        assert info.chat_type == "group"


# ---------------------------------------------------------------------------
# 每日用量列表
# ---------------------------------------------------------------------------


async def list_daily(
    user: DBUser,
    *,
    day: Optional[str] = None,
    tenant_id: Optional[int] = None,
    adapter_key: str = "",
    instance_key: str = "",
    state: str = "",
    chat_type: str = "",
    page: int = 1,
    page_size: int = 50,
):
    return await list_channel_quota_daily(
        day=day,
        tenant_id=tenant_id,
        adapter_key=adapter_key,
        instance_key=instance_key,
        state=state,
        chat_type=chat_type,
        page=page,
        page_size=page_size,
        _current_user=user,
    )


class TestDailyList:
    async def test_platform_sees_every_channel(self, db) -> None:  # noqa: ARG002
        response = await list_daily(platform_owner())

        assert {item.chat_key for item in response.items} == {ACME_CHAT, ACME_SECOND_CHAT, RIVAL_CHAT}

    async def test_tenant_sees_only_its_own_instances(self, db) -> None:  # noqa: ARG002
        response = await list_daily(acme_admin())

        assert {item.chat_key for item in response.items} == {ACME_CHAT, ACME_SECOND_CHAT}

    async def test_tenant_cannot_widen_the_scope_with_a_tenant_id(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await list_daily(acme_admin(), tenant_id=RIVAL_TENANT_ID)

    async def test_tenant_cannot_widen_the_scope_with_an_instance_key(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await list_daily(acme_admin(), adapter_key=ADAPTER, instance_key=RIVAL_INSTANCE)

    async def test_instance_filter_is_paired(self, db) -> None:  # noqa: ARG002
        """同一个实例标识在另一个适配器下是另一行；按名字单独筛会串租户"""
        await DBAdapterInstance.create(
            adapter_key="other_adapter",
            instance_key=ACME_INSTANCE,
            display_name="同名实例",
            status="online",
            tenant_id=RIVAL_TENANT_ID,
        )
        await DBChatChannel.create(
            adapter_key="other_adapter",
            instance_key=ACME_INSTANCE,
            channel_id="group_210099",
            chat_key="other_adapter-acme-1-group_210099",
            channel_type="group",
            data="{}",
        )

        tenant_response = await list_daily(acme_admin(), adapter_key=ADAPTER, instance_key=ACME_INSTANCE)
        platform_response = await list_daily(
            platform_owner(),
            adapter_key="other_adapter",
            instance_key=ACME_INSTANCE,
        )

        assert {item.chat_key for item in tenant_response.items} == {ACME_CHAT}
        assert {item.chat_key for item in platform_response.items} == {"other_adapter-acme-1-group_210099"}

    async def test_missing_day_row_reads_as_zero_and_creates_nothing(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10)

        response = await list_daily(platform_owner())

        assert all(item.used == 0 for item in response.items)
        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_sorted_by_percent_descending(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10)
        await DBChannelQuotaDaily.create(chat_key=ACME_CHAT, day=counter.quota_day(), replies=2)
        await DBChannelQuotaDaily.create(chat_key=RIVAL_CHAT, day=counter.quota_day(), replies=9)
        await DBChannelQuotaDaily.create(chat_key=ACME_SECOND_CHAT, day=counter.quota_day(), replies=5)

        response = await list_daily(platform_owner())

        assert [item.chat_key for item in response.items] == [RIVAL_CHAT, ACME_SECOND_CHAT, ACME_CHAT]
        assert [item.percent for item in response.items] == [90, 50, 20]

    async def test_state_filter(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10)
        await DBChannelQuotaDaily.create(chat_key=ACME_CHAT, day=counter.quota_day(), replies=10)
        await DBChannelQuotaDaily.create(chat_key=RIVAL_CHAT, day=counter.quota_day(), replies=1)

        paused = await list_daily(platform_owner(), state="paused")
        normal = await list_daily(platform_owner(), state="normal")

        assert [item.chat_key for item in paused.items] == [ACME_CHAT]
        # 没有计数行的频道按 0 算，落在 normal 里
        assert {item.chat_key for item in normal.items} == {RIVAL_CHAT, ACME_SECOND_CHAT}

    async def test_chat_type_filter(self, db) -> None:  # noqa: ARG002
        private_chat = f"{ADAPTER}-{ACME_INSTANCE}-private_210009"
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=ACME_INSTANCE,
            channel_id="private_210009",
            chat_key=private_chat,
            channel_type="private",
            data="{}",
        )

        groups = await list_daily(platform_owner(), chat_type="group")
        privates = await list_daily(platform_owner(), chat_type="private")

        assert private_chat not in {item.chat_key for item in groups.items}
        assert {item.chat_key for item in privates.items} == {private_chat}

    async def test_pagination_is_over_the_sorted_set(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10)
        await DBChannelQuotaDaily.create(chat_key=ACME_CHAT, day=counter.quota_day(), replies=1)
        await DBChannelQuotaDaily.create(chat_key=ACME_SECOND_CHAT, day=counter.quota_day(), replies=5)
        await DBChannelQuotaDaily.create(chat_key=RIVAL_CHAT, day=counter.quota_day(), replies=9)

        first = await list_daily(platform_owner(), page=1, page_size=2)
        second = await list_daily(platform_owner(), page=2, page_size=2)

        assert first.total == 3
        assert [item.chat_key for item in first.items] == [RIVAL_CHAT, ACME_SECOND_CHAT]
        assert [item.chat_key for item in second.items] == [ACME_CHAT]

    async def test_day_parameter_selects_the_row(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10)
        await DBChannelQuotaDaily.create(chat_key=ACME_CHAT, day="2026-05-01", replies=7)

        response = await list_daily(platform_owner(), day="2026-05-01")

        item = next(item for item in response.items if item.chat_key == ACME_CHAT)
        assert (item.used, item.percent) == (7, 70)

    async def test_tenant_name_is_shown_to_the_platform(self, db) -> None:  # noqa: ARG002
        response = await list_daily(platform_owner())

        by_key = {item.chat_key: item for item in response.items}
        assert by_key[ACME_CHAT].tenant_name == "Acme"
        assert by_key[ACME_CHAT].tenant_id == ACME_TENANT_ID

    async def test_unlimited_reports_no_remaining(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=0)

        response = await list_daily(platform_owner())

        assert all(item.remaining is None for item in response.items)


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------


class TestHistory:
    async def test_days_are_filled_from_missing_rows(self, db) -> None:  # noqa: ARG002
        response = await get_channel_quota_history(chat_key=ACME_CHAT, days=3, _current_user=platform_owner())

        assert len(response.days) == 3
        assert all(day.replies == 0 for day in response.days)

    async def test_days_are_ordered_oldest_first_and_end_today(self, db) -> None:  # noqa: ARG002
        response = await get_channel_quota_history(chat_key=ACME_CHAT, days=3, _current_user=platform_owner())

        assert response.days[-1].day == counter.quota_day()
        assert response.days == sorted(response.days, key=lambda day: day.day)

    async def test_existing_rows_are_reported(self, db) -> None:  # noqa: ARG002
        today = counter.quota_day()
        await DBChannelQuotaDaily.create(
            chat_key=ACME_CHAT,
            day=today,
            replies=4,
            tokens_all=1000,
            tokens_reply=600,
            bonus=5,
        )

        response = await get_channel_quota_history(chat_key=ACME_CHAT, days=1, _current_user=platform_owner())

        day = response.days[0]
        assert (day.replies, day.tokens_all, day.tokens_reply, day.bonus) == (4, 1000, 600, 5)

    async def test_scene_breakdown_comes_from_the_ledger(self, db) -> None:  # noqa: ARG002
        now = datetime.datetime.now(datetime.timezone.utc)
        for scene, tokens in (("chat", 300), ("chat", 200), ("embedding", 100)):
            await DBLLMUsageRecord.create(
                adapter_key=ADAPTER,
                instance_key=ACME_INSTANCE,
                chat_key=ACME_CHAT,
                scene=scene,
                token_total=tokens,
                create_time=now,
            )

        response = await get_channel_quota_history(chat_key=ACME_CHAT, days=1, _current_user=platform_owner())

        by_scene = {row.scene: row.token_total for row in response.by_scene}
        assert by_scene == {"chat": 500, "embedding": 100}

    async def test_ledger_rows_from_another_channel_are_excluded(self, db) -> None:  # noqa: ARG002
        now = datetime.datetime.now(datetime.timezone.utc)
        await DBLLMUsageRecord.create(
            adapter_key=ADAPTER,
            instance_key=RIVAL_INSTANCE,
            chat_key=RIVAL_CHAT,
            scene="chat",
            token_total=999,
            create_time=now,
        )

        response = await get_channel_quota_history(chat_key=ACME_CHAT, days=1, _current_user=platform_owner())

        assert response.by_scene == []

    async def test_no_cost_fields_are_exposed(self, db) -> None:  # noqa: ARG002
        """额度承诺的单位是 token 本身；费用是平台方的定价，不在这个消息里"""
        response = await get_channel_quota_history(chat_key=ACME_CHAT, days=1, _current_user=platform_owner())

        payload = response.model_dump()
        assert "cost" not in str(payload)
        assert "rate" not in str(payload)

    async def test_policy_summary_is_the_current_effective_one(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100, over_limit_action="pause")

        response = await get_channel_quota_history(chat_key=ACME_CHAT, days=1, _current_user=platform_owner())

        assert response.policy.unit == "replies"
        assert response.policy.effective_limit == 100
        assert response.policy.over_limit_action == "pause"

    async def test_foreign_channel_is_404(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await get_channel_quota_history(chat_key=RIVAL_CHAT, days=1, _current_user=acme_admin())

    async def test_days_must_be_within_the_cap(self) -> None:
        """上限在 FastAPI 的 Query 里；这里钉住那个常量，避免被悄悄放宽"""
        from nekro_agent.routers.console.channel_quota import MAX_HISTORY_DAYS

        assert MAX_HISTORY_DAYS == 30


# ---------------------------------------------------------------------------
# 单频道今日状态（频道详情页顶部那一行）
# ---------------------------------------------------------------------------


class TestSingleChannelStatus:
    """与列表同一套拼装：同一时刻两处给出的用量必须是同一个数字"""

    async def test_platform_owner_can_read_any_channel(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100)

        item = await get_channel_quota_status(chat_key=ACME_CHAT, _current_user=platform_owner())

        assert item.chat_key == ACME_CHAT
        assert item.limit == 100

    async def test_tenant_admin_can_read_its_own_channel(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100)
        await DBChannelQuotaDaily.create(chat_key=ACME_CHAT, day=counter.quota_day(), replies=3)

        item = await get_channel_quota_status(chat_key=ACME_CHAT, _current_user=acme_admin())

        assert (item.used, item.limit) == (3, 100)

    async def test_another_tenants_channel_is_404(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await get_channel_quota_status(chat_key=RIVAL_CHAT, _current_user=acme_admin())

    async def test_disabled_still_reports_the_real_count(self, db) -> None:  # noqa: ARG002
        """显示用量不依赖开关：关着的时候用量照样是真实的，只是不拦"""
        await DBChannelQuotaDaily.create(chat_key=ACME_CHAT, day=counter.quota_day(), replies=7)

        item = await get_channel_quota_status(chat_key=ACME_CHAT, _current_user=platform_owner())
        listed = await list_daily(platform_owner())
        listed_item = next(row for row in listed.items if row.chat_key == ACME_CHAT)

        assert item.state == "disabled"
        assert item.used == 7
        assert (listed_item.state, listed_item.used) == (item.state, item.used)

    async def test_missing_counting_row_reads_as_zero_and_creates_nothing(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=100)

        item = await get_channel_quota_status(chat_key=ACME_CHAT, _current_user=platform_owner())

        assert (item.used, item.bonus, item.remaining) == (0, 0, 100)
        assert await DBChannelQuotaDaily.filter(chat_key=ACME_CHAT).count() == 0

    async def test_bonus_and_percent_match_the_list(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10, over_limit_action="alert")
        await DBChannelQuotaDaily.create(chat_key=ACME_CHAT, day=counter.quota_day(), replies=15, bonus=5)

        item = await get_channel_quota_status(chat_key=ACME_CHAT, _current_user=platform_owner())
        listed = await list_daily(platform_owner())
        listed_item = next(row for row in listed.items if row.chat_key == ACME_CHAT)

        assert (item.bonus, item.percent, item.state) == (5, 100, "exceeded")
        assert (listed_item.bonus, listed_item.percent, listed_item.state) == (item.bonus, item.percent, item.state)


# ---------------------------------------------------------------------------
# 临时加额
# ---------------------------------------------------------------------------


class TestBonus:
    async def test_bonus_is_added_and_the_status_returns(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10, over_limit_action="pause")
        await DBChannelQuotaDaily.create(chat_key=ACME_CHAT, day=counter.quota_day(), replies=10)

        response = await add_channel_quota_bonus(
            chat_key=ACME_CHAT,
            body=ChannelQuotaBonusRequest(amount=5),
            _current_user=platform_owner(),
        )

        assert response.bonus == 5
        # 10/10 已暂停，加 5 之后是 10/15 = 66%，回到正常
        assert response.status.state == "normal"
        assert response.status.used == 10
        assert response.status.bonus == 5

    async def test_bonus_creates_the_row_when_missing(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10)

        await add_channel_quota_bonus(
            chat_key=ACME_CHAT,
            body=ChannelQuotaBonusRequest(amount=3),
            _current_user=platform_owner(),
        )

        row = await DBChannelQuotaDaily.filter(chat_key=ACME_CHAT).first()
        assert row is not None
        assert row.bonus == 3

    async def test_bonus_accumulates(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10)

        await add_channel_quota_bonus(
            chat_key=ACME_CHAT,
            body=ChannelQuotaBonusRequest(amount=3),
            _current_user=platform_owner(),
        )
        response = await add_channel_quota_bonus(
            chat_key=ACME_CHAT,
            body=ChannelQuotaBonusRequest(amount=4),
            _current_user=platform_owner(),
        )

        assert response.bonus == 7

    async def test_amount_must_be_positive(self) -> None:
        with pytest.raises(PydanticValidationError):
            ChannelQuotaBonusRequest(amount=0)
        with pytest.raises(PydanticValidationError):
            ChannelQuotaBonusRequest(amount=-1)

    async def test_missing_channel_is_404(self, db) -> None:  # noqa: ARG002
        with pytest.raises(NotFoundError):
            await add_channel_quota_bonus(
                chat_key="no-such-chat",
                body=ChannelQuotaBonusRequest(amount=1),
                _current_user=platform_owner(),
            )


class TestPageSizeCap:
    async def test_page_size_cap_is_two_hundred(self) -> None:
        from nekro_agent.routers.console.channel_quota import MAX_PAGE_SIZE

        assert MAX_PAGE_SIZE == 200


class TestEnumSurface:
    """列表里的状态取值必须与状态机一致（前端按这几个字符串分支）"""

    async def test_state_names_are_the_documented_ones(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10)
        await DBChannelQuotaDaily.create(chat_key=ACME_CHAT, day=counter.quota_day(), replies=10)

        paused = await list_daily(platform_owner(), state="paused")
        disabled = await list_daily(platform_owner(), state="disabled")

        assert paused.items[0].state == "paused"
        assert disabled.total == 0

    async def test_action_is_carried_so_the_ui_can_explain_consequences(self, db) -> None:  # noqa: ARG002
        await write_global(group_daily_limit=10, over_limit_action="alert")

        response = await list_daily(platform_owner())

        assert all(item.action == "alert" for item in response.items)
