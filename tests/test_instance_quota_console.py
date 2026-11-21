"""总后台实例配额的作用域与计费边界

配额入口从聊天命令搬到总后台之后，多出两条必须钉死的线：

1. **租户作用域**：租户只看得到自己名下的实例，改别人的实例一律 **404 而不是 403**
   （403 会把平台方的实例清单变成可枚举的，见 `tenant_perm.assert_instance_accessible`）；
2. **计费边界**：平台方给某个实例设的上限，租户不能自己突破。这条有四种绕法，全部要挡住：
   - 直接把数值调到上限之上；
   - 把上限填成 `0`——配额字段里 0 表示"不限制"，在有上限时它是最大的越界而不是最小的取值；
   - 把实例层覆盖**清掉**，让配额回落到全局层的"不限制"；
   - 绕开配额接口，改从通用配置接口（配置页）写同一个字段。

第 4 条尤其重要：总后台的配额接口拦得再严，只要 `PUT /config/instance/...` 还开着，
租户在配置页里就能把自己的配额改上去。因此判定做在服务里，两条写路径共用。
"""

import datetime
from typing import Dict, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.core_utils import ConfigManager
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.config import _assert_config_key_accessible
from nekro_agent.routers.console.quota import (
    batch_update_instance_quota,
    list_instance_quotas,
    update_instance_quota,
)
from nekro_agent.schemas.console import InstanceQuotaBatchRequest, InstanceQuotaUpdateRequest
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.config_service import instance_override_config_key
from nekro_agent.services.quota_service import (
    assert_tenant_quota_within_ceiling,
    collect_instance_quota_usage,
    is_quota_within_ceiling,
    load_quota_ceiling,
    resolve_instance_daily_limit,
    save_quota_ceiling,
    set_instance_daily_limit,
)
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
OTHER_TENANT_ID = 2

ADAPTER = "onebot_v11"
OWN_INSTANCE = "inst1"
OWN_SECOND_INSTANCE = "inst2"
FOREIGN_INSTANCE = "rival1"

# 取自测试环境的形态：acme_ltd 名下 inst1/inst2，另一租户名下 rival1
INSTANCE_LAYOUT: List[Tuple[str, Optional[int]]] = [
    (OWN_INSTANCE, ACME_TENANT_ID),
    (OWN_SECOND_INSTANCE, ACME_TENANT_ID),
    (FOREIGN_INSTANCE, OTHER_TENANT_ID),
]


@pytest.fixture(autouse=True)
def isolated_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """把覆盖配置的落盘目录指到临时目录，并在用例之间清掉配置缓存

    覆盖配置实例被 `ConfigManager` 按配置键全局缓存，不清理会让上一个用例写的配额
    泄漏到下一个用例。
    """
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "AI_CHAT_DAILY_REPLY_LIMIT", 0)
    registered = [key for _, key in _all_override_keys()]
    for key in registered:
        ConfigManager.unregister_config(key)
    config_resolver.bump_version()
    yield
    for key in registered:
        ConfigManager.unregister_config(key)
    config_resolver.bump_version()


def _all_override_keys() -> List[Tuple[str, str]]:
    return [(instance_key, instance_override_config_key(ADAPTER, instance_key)) for instance_key, _ in INSTANCE_LAYOUT]


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
        yield
    finally:
        await Tortoise.close_connections()


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    """构造一个未落库的登录身份；作用域判定只读这三个字段"""
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


async def set_ceiling(instance_key: str, ceiling: Optional[int]) -> None:
    instance = await DBAdapterInstance.get(adapter_key=ADAPTER, instance_key=instance_key)
    await save_quota_ceiling(instance, ceiling)


class TestCeilingPredicate:
    """上限判定的边界，含 0 的特殊语义"""

    @pytest.mark.parametrize(
        ("limit", "ceiling", "expected"),
        [
            (50, None, True),  # 未设上限，任何取值都放行
            (0, None, True),
            (50, 100, True),
            (100, 100, True),  # 等于上限是允许的
            (101, 100, False),
            (0, 100, False),  # 0 表示不限制，在有上限时是最大的越界
            (-1, 100, False),
        ],
    )
    def test_is_quota_within_ceiling(self, limit: int, ceiling: Optional[int], expected: bool):
        assert is_quota_within_ceiling(limit, ceiling) is expected


class TestTenantScope:
    """租户只看得到、只改得动自己名下的实例"""

    async def test_list_only_returns_own_instances(self, db):
        response = await list_instance_quotas(_current_user=acme_admin())
        assert [item.instance_key for item in response.items] == [OWN_INSTANCE, OWN_SECOND_INSTANCE]
        assert response.editable_ceiling is False, "租户不能设平台方上限"

    async def test_platform_owner_sees_all_instances(self, db):
        response = await list_instance_quotas(_current_user=platform_owner())
        assert [item.instance_key for item in response.items] == [OWN_INSTANCE, OWN_SECOND_INSTANCE, FOREIGN_INSTANCE]
        assert response.editable_ceiling is True

    async def test_foreign_instance_is_404_not_403(self, db):
        """越权必须与"不存在"不可区分，否则实例清单可被枚举"""
        with pytest.raises(NotFoundError):
            await update_instance_quota(
                FOREIGN_INSTANCE,
                InstanceQuotaUpdateRequest(daily_limit=10),
                _current_user=acme_admin(),
            )

    async def test_missing_instance_is_also_404(self, db):
        with pytest.raises(NotFoundError):
            await update_instance_quota(
                "no_such_instance",
                InstanceQuotaUpdateRequest(daily_limit=10),
                _current_user=acme_admin(),
            )


class TestBillingBoundary:
    """租户可以给自己降额，但不能提高平台方设定的上限"""

    async def test_tenant_may_lower_within_ceiling(self, db):
        await set_ceiling(OWN_INSTANCE, 100)
        result = await update_instance_quota(
            OWN_INSTANCE,
            InstanceQuotaUpdateRequest(daily_limit=40),
            _current_user=acme_admin(),
        )
        assert result.daily_limit == 40
        assert result.limit_source == "instance"
        assert result.ceiling == 100

    async def test_tenant_cannot_exceed_ceiling(self, db):
        await set_ceiling(OWN_INSTANCE, 100)
        with pytest.raises(PermissionDeniedError):
            await update_instance_quota(
                OWN_INSTANCE,
                InstanceQuotaUpdateRequest(daily_limit=101),
                _current_user=acme_admin(),
            )
        limit, source, _ = await resolve_instance_daily_limit(ADAPTER, OWN_INSTANCE)
        assert (limit, source) == (0, "system"), "被拒之后不能留下半截写入"

    async def test_tenant_cannot_set_unlimited_when_ceiling_exists(self, db):
        """0 表示不限制，在有上限时同样要拒绝"""
        await set_ceiling(OWN_INSTANCE, 100)
        with pytest.raises(PermissionDeniedError):
            await update_instance_quota(
                OWN_INSTANCE,
                InstanceQuotaUpdateRequest(daily_limit=0),
                _current_user=acme_admin(),
            )

    async def test_tenant_cannot_clear_override_to_escape_ceiling(self, db):
        """清除实例层覆盖会回落到全局层的"不限制"，这同样是突破上限"""
        await set_ceiling(OWN_INSTANCE, 100)
        set_instance_daily_limit(ADAPTER, OWN_INSTANCE, 40)
        with pytest.raises(PermissionDeniedError):
            await update_instance_quota(
                OWN_INSTANCE,
                InstanceQuotaUpdateRequest(clear_override=True),
                _current_user=acme_admin(),
            )
        limit, _, _ = await resolve_instance_daily_limit(ADAPTER, OWN_INSTANCE)
        assert limit == 40, "覆盖必须原封不动"

    async def test_tenant_cannot_set_ceiling(self, db):
        with pytest.raises(PermissionDeniedError):
            await update_instance_quota(
                OWN_INSTANCE,
                InstanceQuotaUpdateRequest(set_ceiling=True, ceiling=9999),
                _current_user=acme_admin(),
            )
        instance = await DBAdapterInstance.get(adapter_key=ADAPTER, instance_key=OWN_INSTANCE)
        assert load_quota_ceiling(instance) is None

    async def test_platform_owner_sets_ceiling_and_limit_in_one_call(self, db):
        result = await update_instance_quota(
            OWN_INSTANCE,
            InstanceQuotaUpdateRequest(set_ceiling=True, ceiling=200, daily_limit=150),
            _current_user=platform_owner(),
        )
        assert result.ceiling == 200
        assert result.daily_limit == 150

    async def test_platform_owner_is_not_bound_by_its_own_ceiling(self, db):
        """上限约束的是租户，平台方随时可以自己改高"""
        await set_ceiling(OWN_INSTANCE, 100)
        result = await update_instance_quota(
            OWN_INSTANCE,
            InstanceQuotaUpdateRequest(daily_limit=500),
            _current_user=platform_owner(),
        )
        assert result.daily_limit == 500


class TestConfigPageBypass:
    """通用配置接口是同一条边界上的另一扇门，判定必须共用"""

    async def test_instance_override_write_is_blocked(self, db):
        await set_ceiling(OWN_INSTANCE, 100)
        config_key = instance_override_config_key(ADAPTER, OWN_INSTANCE)
        with pytest.raises(PermissionDeniedError):
            await assert_tenant_quota_within_ceiling(
                config_key,
                {"enable_AI_CHAT_DAILY_REPLY_LIMIT": "true", "AI_CHAT_DAILY_REPLY_LIMIT": "5000"},
            )

    async def test_turning_the_override_off_is_blocked(self, db):
        """只提交开关、不提交数值，同样要按"关掉之后生效的是什么"来判"""
        await set_ceiling(OWN_INSTANCE, 100)
        set_instance_daily_limit(ADAPTER, OWN_INSTANCE, 40)
        config_key = instance_override_config_key(ADAPTER, OWN_INSTANCE)
        with pytest.raises(PermissionDeniedError):
            await assert_tenant_quota_within_ceiling(config_key, {"enable_AI_CHAT_DAILY_REPLY_LIMIT": "false"})

    async def test_within_ceiling_write_passes(self, db):
        await set_ceiling(OWN_INSTANCE, 100)
        config_key = instance_override_config_key(ADAPTER, OWN_INSTANCE)
        await assert_tenant_quota_within_ceiling(
            config_key,
            {"enable_AI_CHAT_DAILY_REPLY_LIMIT": "true", "AI_CHAT_DAILY_REPLY_LIMIT": "80"},
        )

    async def test_unrelated_fields_are_untouched(self, db):
        """不涉及配额字段的写入不进入本判定，避免拖慢每一次配置保存"""
        await set_ceiling(OWN_INSTANCE, 100)
        config_key = instance_override_config_key(ADAPTER, OWN_INSTANCE)
        await assert_tenant_quota_within_ceiling(config_key, {"enable_AI_CHAT_ENABLE_HOURLY_LIMIT": "true"})

    async def test_instance_without_ceiling_is_unconstrained(self, db):
        """未设上限的实例（含未启用多租户的部署）行为完全不变"""
        config_key = instance_override_config_key(ADAPTER, OWN_INSTANCE)
        await assert_tenant_quota_within_ceiling(
            config_key,
            {"enable_AI_CHAT_DAILY_REPLY_LIMIT": "true", "AI_CHAT_DAILY_REPLY_LIMIT": "999999"},
        )


class TestConfigKeyOwnership:
    """`/config/set/{config_key}/{item_key}` 与 `/config/batch/{config_key}` 的归属判定

    `config_key` 不是不透明标识，它能编码出实例作用域与频道作用域，所以这两条写路径必须
    自己校验归属。判定要用**生成配置键时的同一个分隔符**（`INSTANCE_OVERRIDE_KEY_SEP`）：
    按 `:` 切会让真实键 `instance_override_onebot_v11__inst1` 切不出 instance_key，
    结果是租户连自己的实例都写不了——安全上 fail-closed，功能上却把合法路径整个堵死。
    """

    async def test_foreign_instance_is_404(self, db):
        config_key = instance_override_config_key(ADAPTER, FOREIGN_INSTANCE)
        with pytest.raises(NotFoundError):
            await _assert_config_key_accessible(acme_admin(), config_key)

    async def test_own_instance_is_allowed(self, db):
        """用真实生成的配置键，租户必须能过自己的实例这一关"""
        config_key = instance_override_config_key(ADAPTER, OWN_INSTANCE)
        await _assert_config_key_accessible(acme_admin(), config_key)

    async def test_unknown_instance_is_404(self, db):
        with pytest.raises(NotFoundError):
            await _assert_config_key_accessible(acme_admin(), instance_override_config_key(ADAPTER, "ghost1"))

    @pytest.mark.parametrize("config_key", ["system", "adapter_onebot_v11", "adapter_override_onebot_v11"])
    async def test_shared_scopes_are_403(self, db, config_key: str):
        """跨租户共享的作用域是权限边界而非资源存在性，用 403"""
        with pytest.raises(PermissionDeniedError):
            await _assert_config_key_accessible(acme_admin(), config_key)

    async def test_platform_owner_passes_everything(self, db):
        owner = platform_owner()
        for config_key in [
            "system",
            "adapter_onebot_v11",
            instance_override_config_key(ADAPTER, FOREIGN_INSTANCE),
            instance_override_config_key(ADAPTER, OWN_INSTANCE),
        ]:
            await _assert_config_key_accessible(owner, config_key)


class TestBatchUpdate:
    """批量设置逐个实例结算，不因为一个失败就把已生效的改动说成没发生"""

    async def test_batch_reports_per_instance_outcome(self, db):
        response = await batch_update_instance_quota(
            InstanceQuotaBatchRequest(
                instance_keys=[OWN_INSTANCE, FOREIGN_INSTANCE, OWN_SECOND_INSTANCE],
                update=InstanceQuotaUpdateRequest(daily_limit=30),
            ),
            _current_user=acme_admin(),
        )
        assert response.succeeded == [OWN_INSTANCE, OWN_SECOND_INSTANCE]
        assert response.failed == [FOREIGN_INSTANCE], "别人的实例失败，自己的两个照常生效"
        assert response.ok is False

        for instance_key in (OWN_INSTANCE, OWN_SECOND_INSTANCE):
            limit, source, _ = await resolve_instance_daily_limit(ADAPTER, instance_key)
            assert (limit, source) == (30, "instance")


class TestQuotaUsage:
    """配额判定发生在频道粒度，实例视角的指标要如实反映这一点"""

    async def test_usage_counts_reply_rounds_per_channel(self, db):
        await _seed_channel_replies(
            {
                f"{ADAPTER}-{OWN_INSTANCE}-group_1": 5,
                f"{ADAPTER}-{OWN_INSTANCE}-group_2": 2,
                f"{ADAPTER}-{OWN_SECOND_INSTANCE}-group_3": 1,
            },
        )
        usage = await collect_instance_quota_usage(
            [OWN_INSTANCE, OWN_SECOND_INSTANCE],
            {OWN_INSTANCE: 5, OWN_SECOND_INSTANCE: 0},
        )
        assert usage[OWN_INSTANCE] == {
            "replies_today": 7,
            "active_channels": 2,
            "peak_channel_replies": 5,
            "channels_at_limit": 1,
        }
        assert usage[OWN_SECOND_INSTANCE]["channels_at_limit"] == 0, "上限为 0（不限制）时不该有频道被算作撞线"


async def _seed_channel_replies(counts: Dict[str, int]) -> None:
    """按频道写入当天的回复回合数

    数据源是 `DBChannelQuotaDaily.replies`：一个回合只记一次，不再按 Bot 消息条数统计。
    """
    from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily
    from nekro_agent.services.channel_quota.counter import quota_day

    day = quota_day()
    for chat_key, count in counts.items():
        instance_key = chat_key.split("-")[1]
        await DBChannelQuotaDaily.create(
            chat_key=chat_key,
            adapter_key=ADAPTER,
            instance_key=instance_key,
            day=day,
            replies=count,
        )
