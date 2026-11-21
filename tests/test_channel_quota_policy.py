"""频道每日额度：三层策略的继承与租户口径

这一族钉的是「平台方承诺」与「租户能调的那部分」之间的边界：

- 每一层只写自己想改的列，其余留 NULL 表示跟上一层；
- 平台方额度为 0（不限）时，租户自己设的值才是唯一的约束；
- 单位被平台方换掉之后，租户填的旧数字作废（不猜换算）；
- 提示语三层优先级，最后落在默认那句上。

行为一侧（暂停、告警、接口）在别的文件里。
"""

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_channel_quota import DBChannelQuotaPolicy
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota.policy import (
    DEFAULT_PAUSE_MESSAGE,
    SCOPE_CHANNEL,
    SCOPE_GLOBAL,
    SCOPE_INSTANCE,
    resolve_policy,
)

ADAPTER = "onebot_v11"
INSTANCE = "quota1"
OTHER_INSTANCE = "quota2"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_700001"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    policy_module.invalidate_policy_cache()
    try:
        yield
    finally:
        policy_module.invalidate_policy_cache()
        await Tortoise.close_connections()


async def global_row(**fields) -> DBChannelQuotaPolicy:
    """写全局那一行；默认是「关着、按回复数、两种额度 0、只告警」"""
    defaults = {
        "enabled": True,
        "unit": "replies",
        "group_daily_limit": 100,
        "private_daily_limit": 50,
        "over_limit_action": "alert",
        "pause_message": None,
    }
    defaults.update(fields)
    row = await DBChannelQuotaPolicy.create(scope=SCOPE_GLOBAL, **defaults)
    policy_module.invalidate_policy_cache()
    return row


async def instance_row(instance_key: str = INSTANCE, adapter_key: str = ADAPTER, **fields) -> DBChannelQuotaPolicy:
    row = await DBChannelQuotaPolicy.create(
        scope=SCOPE_INSTANCE,
        adapter_key=adapter_key,
        instance_key=instance_key,
        **fields,
    )
    policy_module.invalidate_policy_cache()
    return row


async def channel_row(chat_key: str = CHAT_KEY, **fields) -> DBChannelQuotaPolicy:
    row = await DBChannelQuotaPolicy.create(
        scope=SCOPE_CHANNEL,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        chat_key=chat_key,
        **fields,
    )
    policy_module.invalidate_policy_cache()
    return row


class TestDefaultsWhenNothingConfigured:
    """一行策略都没有时功能必须是关的，且其余字段给出可用的默认值"""

    async def test_missing_global_row_means_disabled(self, db) -> None:  # noqa: ARG002
        effective = await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")

        assert effective.enabled is False
        assert effective.unit == "replies"
        assert effective.effective_limit == 0
        assert effective.over_limit_action == "alert"
        assert effective.pause_message == DEFAULT_PAUSE_MESSAGE

    async def test_global_switch_is_the_master_gate(self, db) -> None:  # noqa: ARG002
        """全局那一行是总闸：它关着时下层写 `enabled=True` 也打不开

        存量部署升级上来时全局行还不存在，若下层的值能把它打开，功能就会「默认开启」，
        而那正是最不能出的错。层只能把总闸关得更小（`test_channel_layer_can_disable_...`）。
        """
        await global_row(enabled=False)
        await instance_row(enabled=True)
        await channel_row(enabled=True)

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).enabled is False

    async def test_missing_global_row_is_the_master_gate_too(self, db) -> None:  # noqa: ARG002
        await instance_row(enabled=True)

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).enabled is False

    async def test_channel_layer_can_disable_inside_an_enabled_instance(self, db) -> None:  # noqa: ARG002
        await global_row(enabled=True)
        await channel_row(enabled=False)

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).enabled is False
        assert (await resolve_policy(ADAPTER, INSTANCE, "other-chat", "group")).enabled is True

    async def test_instance_layer_can_disable_inside_an_enabled_global(self, db) -> None:  # noqa: ARG002
        await global_row(enabled=True)
        await instance_row(enabled=False)

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).enabled is False
        assert (await resolve_policy(ADAPTER, OTHER_INSTANCE, CHAT_KEY, "group")).enabled is True


class TestFieldWiseInheritance:
    """逐字段继承：每一层只覆盖它写了的那几列"""

    async def test_channel_overrides_only_its_own_column(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=100, private_daily_limit=50, over_limit_action="alert")
        await instance_row(group_daily_limit=80)
        await channel_row(group_daily_limit=30)

        effective = await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")

        assert effective.effective_limit == 30, "额度取频道层"
        assert effective.over_limit_action == "alert", "动作没在频道层写，继续继承全局"

    async def test_instance_layer_sits_between_global_and_channel(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=100)
        await instance_row(group_daily_limit=80)

        effective = await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")
        assert effective.effective_limit == 80

    async def test_null_in_a_layer_means_inherit_not_zero(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=100)
        await instance_row(group_daily_limit=None)

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 100

    async def test_global_unlimited_is_zero_not_missing(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=0)

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 0


class TestScopeMatching:
    """作用域匹配：实例按 `(适配器, 实例)` 成对，群与私聊取不同的列"""

    async def test_instance_row_does_not_leak_across_adapters(self, db) -> None:  # noqa: ARG002
        """同名实例在另一个适配器下是另一行；按实例标识单独匹配会串租户"""
        await global_row(group_daily_limit=100)
        await instance_row(instance_key="same", adapter_key="other_adapter", group_daily_limit=10)

        effective = await resolve_policy(ADAPTER, "same", CHAT_KEY, "group")
        assert effective.effective_limit == 100

    async def test_group_and_private_take_different_columns(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=100, private_daily_limit=20)

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 100
        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "private")).effective_limit == 20

    async def test_non_group_channel_types_fall_back_to_private(self, db) -> None:  # noqa: ARG002
        """网页聊天、邮件这类一对一形态没有「群额度」可言，按私聊额度算"""
        await global_row(group_daily_limit=100, private_daily_limit=20)

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "webui")).effective_limit == 20
        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, None)).effective_limit == 20

    async def test_flag_marks_the_column_actually_used(self, db) -> None:  # noqa: ARG002
        await global_row()

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).is_group is True
        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "private")).is_group is False


class TestTenantLimits:
    """租户值：只能在平台方给的额度以内调低，且只在同一个单位下算数"""

    async def test_tenant_value_lowers_the_effective_limit(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=100)
        await instance_row(tenant_group_daily_limit=40, tenant_limit_unit="replies")

        effective = await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")
        assert effective.effective_limit == 40
        assert effective.tenant_limit == 40

    async def test_tenant_value_never_raises_the_platform_limit(self, db) -> None:  # noqa: ARG002
        """即使库里已经有一行超过平台方的值（接口校验被绕过），解析侧也只取小的那个"""
        await global_row(group_daily_limit=100)
        await instance_row(tenant_group_daily_limit=500, tenant_limit_unit="replies")

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 100

    async def test_platform_zero_means_tenant_value_is_the_only_constraint(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=0)
        await instance_row(tenant_group_daily_limit=25, tenant_limit_unit="replies")

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 25

    async def test_platform_zero_without_tenant_value_is_unlimited(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=0)

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 0

    async def test_stale_tenant_unit_is_discarded(self, db) -> None:  # noqa: ARG002
        """单位从回复数换成 token 之后，旧的 40 既不是回复数也不是 token 数，只能作废"""
        await global_row(unit="tokens", group_daily_limit=100)
        await instance_row(tenant_group_daily_limit=40, tenant_limit_unit="replies")

        effective = await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")
        assert effective.tenant_limit is None
        assert effective.effective_limit == 100

    async def test_tenant_value_matching_the_unit_counts(self, db) -> None:  # noqa: ARG002
        await global_row(unit="tokens", group_daily_limit=10000)
        await instance_row(tenant_group_daily_limit=4000, tenant_limit_unit="tokens")

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 4000

    async def test_channel_layer_beats_instance_layer_for_tenant_value(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=100)
        await instance_row(tenant_group_daily_limit=40, tenant_limit_unit="replies")
        await channel_row(tenant_group_daily_limit=10, tenant_limit_unit="replies")

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 10

    async def test_group_and_private_tenant_values_are_separate(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=100, private_daily_limit=100)
        await instance_row(
            tenant_group_daily_limit=30,
            tenant_private_daily_limit=5,
            tenant_limit_unit="replies",
        )

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 30
        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "private")).effective_limit == 5


class TestPauseMessagePriority:
    """提示语三层：租户频道 > 租户实例 > 平台方三层 > 默认那句"""

    async def test_default_when_nobody_set_one(self, db) -> None:  # noqa: ARG002
        await global_row()

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).pause_message == DEFAULT_PAUSE_MESSAGE

    async def test_platform_channel_message_beats_platform_instance(self, db) -> None:  # noqa: ARG002
        await global_row(pause_message="全局提示")
        await instance_row(pause_message="实例提示")
        await channel_row(pause_message="频道提示")

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).pause_message == "频道提示"

    async def test_platform_instance_message_beats_global(self, db) -> None:  # noqa: ARG002
        await global_row(pause_message="全局提示")
        await instance_row(pause_message="实例提示")

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).pause_message == "实例提示"

    async def test_tenant_message_beats_every_platform_layer(self, db) -> None:  # noqa: ARG002
        await global_row(pause_message="全局提示")
        await instance_row(pause_message="实例提示", tenant_pause_message="租户提示")

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).pause_message == "租户提示"

    async def test_tenant_channel_message_beats_tenant_instance(self, db) -> None:  # noqa: ARG002
        await global_row()
        await instance_row(tenant_pause_message="租户实例提示")
        await channel_row(tenant_pause_message="租户频道提示")

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).pause_message == "租户频道提示"

    async def test_whitespace_only_tenant_message_falls_back(self, db) -> None:  # noqa: ARG002
        """租户填了三个空格等于没填：不能把一句空白发进群里"""
        await global_row(pause_message="全局提示")
        await instance_row(tenant_pause_message="   ")

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).pause_message == "全局提示"


class TestPolicyCache:
    """进程内缓存：改了策略要能立刻看到，否则平台方会以为自己没保存成功"""

    async def test_invalidate_picks_up_a_new_row(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=100)
        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 100

        await DBChannelQuotaPolicy.filter(scope=SCOPE_GLOBAL).update(group_daily_limit=7)
        policy_module.invalidate_policy_cache()

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 7

    async def test_without_invalidate_the_snapshot_is_reused(self, db) -> None:  # noqa: ARG002
        await global_row(group_daily_limit=100)
        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 100

        await DBChannelQuotaPolicy.filter(scope=SCOPE_GLOBAL).update(group_daily_limit=7)

        assert (await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")).effective_limit == 100

    async def test_cache_error_degrades_to_no_policy(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """读表失败时按「没有策略」处理，也就是功能关闭——不能把消息路径打掉"""

        async def boom():
            raise RuntimeError("数据库不可用")

        monkeypatch.setattr(DBChannelQuotaPolicy, "all", classmethod(lambda cls: boom()))

        effective = await resolve_policy(ADAPTER, INSTANCE, CHAT_KEY, "group")
        assert effective.enabled is False
