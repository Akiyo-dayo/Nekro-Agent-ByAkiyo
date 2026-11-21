"""频道每日额度：会话标识换名、彻底删除实例、实例划归

这两张表都按会话标识存，所以要跟着「换会话标识」（惰性认领、合并重复频道、实例归属迁移）
与「彻底删除实例」一起动，否则就是记忆摘要那张表曾经漏过的同一类问题：

- 换标识后计数留在旧标识上，新标识从 0 开始——当天的用量凭空清零，暂停判定跟着放宽；
- 合并两行频道时，同一天两边都有计数：必须相加，不能像摘要那样「保留新的、删旧的」；
- 删实例后计数与策略留下来，同名的新实例（可能属于另一个租户）会继承旧实例的额度策略。

实例划归另一个租户时同理：旧租户调低的额度与暂停提示是他对自己那些群的设置，
新主人不该继承，也没有「留着」的理由。
"""

import datetime
from typing import Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily, DBChannelQuotaPolicy
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console import tenants as tenants_router
from nekro_agent.schemas.tenant import TenantInstanceBindRequest
from nekro_agent.services import chat_key_migration
from nekro_agent.services.channel_quota import policy as policy_module
from nekro_agent.services.channel_quota.policy import resolve_policy
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE = "qlife1"
OTHER_INSTANCE = "qlife2"
OLD_CHAT = f"{ADAPTER}-{INSTANCE}-group_810001"
NEW_CHAT = f"{ADAPTER}-{INSTANCE}-group_810002"
OTHER_CHAT = f"{ADAPTER}-{OTHER_INSTANCE}-group_810003"
DAY = "2026-11-08"
EARLIER_DAY = "2026-11-07"


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


async def _daily(chat_key: str, day: str, **values: object) -> DBChannelQuotaDaily:
    return await DBChannelQuotaDaily.create(
        chat_key=chat_key,
        adapter_key=ADAPTER,
        instance_key=chat_key.split("-")[1],
        day=day,
        **values,
    )


async def _policy(scope: str, instance_key: str = "", chat_key: str = "", **values: object) -> DBChannelQuotaPolicy:
    return await DBChannelQuotaPolicy.create(
        scope=scope,
        adapter_key=ADAPTER if scope != "global" else "",
        instance_key=instance_key,
        chat_key=chat_key,
        **values,
    )


class TestChatKeyMigration:
    async def test_daily_rows_follow_the_new_chat_key(self, db) -> None:  # noqa: ARG002
        await _daily(OLD_CHAT, DAY, replies=3, tokens_all=100, tokens_reply=60)
        await _daily(OLD_CHAT, EARLIER_DAY, replies=1, tokens_all=10, tokens_reply=10)

        counts = await chat_key_migration.migrate_chat_key_references(OLD_CHAT, NEW_CHAT, instance_key=INSTANCE)

        assert counts["channel_quota_daily"] == 2
        assert await DBChannelQuotaDaily.filter(chat_key=OLD_CHAT).count() == 0
        moved = {row.day: row for row in await DBChannelQuotaDaily.filter(chat_key=NEW_CHAT)}
        assert (moved[DAY].replies, moved[DAY].tokens_all, moved[DAY].tokens_reply) == (3, 100, 60)
        assert moved[EARLIER_DAY].replies == 1

    async def test_merging_adds_the_same_day_instead_of_dropping_it(self, db) -> None:  # noqa: ARG002
        """同一天两边都有计数：相加。只保留一边等于把那一天的用量砍掉一半"""
        sent_at = datetime.datetime(2026, 11, 8, 10, 0, tzinfo=datetime.timezone.utc)
        await _daily(OLD_CHAT, DAY, replies=3, tokens_all=100, tokens_reply=60, bonus=5, alerted_level=80, pause_notice_sent_at=sent_at)
        await _daily(NEW_CHAT, DAY, replies=2, tokens_all=50, tokens_reply=20, bonus=1, alerted_level=0)
        await _daily(OLD_CHAT, EARLIER_DAY, replies=4)

        await chat_key_migration.migrate_chat_key_references(OLD_CHAT, NEW_CHAT, instance_key=INSTANCE)

        assert await DBChannelQuotaDaily.filter(chat_key=OLD_CHAT).count() == 0
        rows = {row.day: row for row in await DBChannelQuotaDaily.filter(chat_key=NEW_CHAT)}
        merged = rows[DAY]
        assert (merged.replies, merged.tokens_all, merged.tokens_reply, merged.bonus) == (5, 150, 80, 6)
        # 告警档位取高的：已经告过 80% 的一天不该在合并后再告一次
        assert merged.alerted_level == 80
        # 暂停提示已经发过就算发过：合并后不该再提示一次
        assert merged.pause_notice_sent_at is not None
        assert rows[EARLIER_DAY].replies == 4
        assert await DBChannelQuotaDaily.filter(chat_key=NEW_CHAT, day=DAY).count() == 1

    async def test_channel_policy_follows_the_new_chat_key(self, db) -> None:  # noqa: ARG002
        await _policy("global", enabled=True, unit="replies", group_daily_limit=50, private_daily_limit=0, over_limit_action="pause")
        await _policy("channel", INSTANCE, OLD_CHAT, group_daily_limit=7)

        counts = await chat_key_migration.migrate_chat_key_references(OLD_CHAT, NEW_CHAT, instance_key=INSTANCE)

        assert counts["channel_quota_policy"] == 1
        assert await DBChannelQuotaPolicy.filter(scope="channel", chat_key=OLD_CHAT).count() == 0
        # 判定读的是进程内缓存：迁移之后必须马上按新标识读到这一行
        effective = await resolve_policy(ADAPTER, INSTANCE, NEW_CHAT, "group")
        assert effective.platform_limit == 7

    async def test_merging_keeps_the_survivors_channel_policy(self, db) -> None:  # noqa: ARG002
        await _policy("channel", INSTANCE, OLD_CHAT, group_daily_limit=7)
        await _policy("channel", INSTANCE, NEW_CHAT, group_daily_limit=9)

        await chat_key_migration.migrate_chat_key_references(OLD_CHAT, NEW_CHAT, instance_key=INSTANCE)

        rows = await DBChannelQuotaPolicy.filter(scope="channel")
        assert [(row.chat_key, row.group_daily_limit) for row in rows] == [(NEW_CHAT, 9)]


class TestPurgeInstance:
    async def test_only_the_purged_instances_quota_rows_go(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        from nekro_agent.services.console import instance_manager

        async def no_client() -> None:
            return None

        monkeypatch.setattr(instance_manager, "get_qdrant_client", no_client)

        instance = await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=INSTANCE, provider_account_id="7000001")
        await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=OTHER_INSTANCE, provider_account_id="7000002")
        await _policy("global", enabled=True, unit="replies", group_daily_limit=50, private_daily_limit=0, over_limit_action="pause")
        await _policy("instance", INSTANCE, group_daily_limit=20)
        await _policy("channel", INSTANCE, OLD_CHAT, group_daily_limit=7)
        await _policy("instance", OTHER_INSTANCE, group_daily_limit=30)
        await _daily(OLD_CHAT, DAY, replies=3)
        await _daily(OTHER_CHAT, DAY, replies=4)

        # 先把策略读进进程缓存，证明删完之后缓存也跟着失效
        assert (await resolve_policy(ADAPTER, INSTANCE, NEW_CHAT, "group")).platform_limit == 20

        counts = await instance_manager.purge_instance(instance)

        assert counts["channel_quota_daily"] == 1
        assert counts["channel_quota_policy"] == 2
        assert await DBChannelQuotaDaily.filter(chat_key=OLD_CHAT).count() == 0
        assert await DBChannelQuotaDaily.filter(chat_key=OTHER_CHAT).count() == 1
        remaining = sorted((row.scope, row.instance_key) for row in await DBChannelQuotaPolicy.all())
        assert remaining == [("global", ""), ("instance", OTHER_INSTANCE)]
        # 同名的新实例不该继承被删实例的额度：回落到全局的 50
        assert (await resolve_policy(ADAPTER, INSTANCE, NEW_CHAT, "group")).platform_limit == 50


TENANT_A_ID = 1
TENANT_B_ID = 2


def _platform_owner() -> DBUser:
    return DBUser(id=1, username="platform", role=Role.Super.value)


def _bind(tenant_id: int, keys: list[str], *, clear_memory: bool = False) -> TenantInstanceBindRequest:
    return TenantInstanceBindRequest(instance_keys=keys, clear_memory_and_affection=clear_memory)


async def _tenant_pair() -> tuple[DBTenant, DBTenant]:
    return await DBTenant.create(id=TENANT_A_ID, name="a"), await DBTenant.create(id=TENANT_B_ID, name="b")


async def _instance(key: str, tenant_id: Optional[int]) -> DBAdapterInstance:
    return await DBAdapterInstance.create(adapter_key=ADAPTER, instance_key=key, tenant_id=tenant_id)


async def _tenant_values(scope: str, instance_key: str, chat_key: str = "") -> dict[str, object]:
    row = await DBChannelQuotaPolicy.get(scope=scope, adapter_key=ADAPTER, instance_key=instance_key, chat_key=chat_key)
    return {
        "tenant_group_daily_limit": row.tenant_group_daily_limit,
        "tenant_private_daily_limit": row.tenant_private_daily_limit,
        "tenant_limit_unit": row.tenant_limit_unit,
        "tenant_pause_message": row.tenant_pause_message,
    }


ALL_CLEARED = {
    "tenant_group_daily_limit": None,
    "tenant_private_daily_limit": None,
    "tenant_limit_unit": None,
    "tenant_pause_message": None,
}


async def _seed_tenant_settings(instance_key: str, chat_key: str) -> None:
    """旧租户在实例层与频道层都留了设置：两层都要清，只清一层等于没清"""
    await _policy(
        "instance",
        instance_key,
        tenant_group_daily_limit=5,
        tenant_private_daily_limit=3,
        tenant_limit_unit="replies",
        tenant_pause_message="今天先到这里",
    )
    await _policy(
        "channel",
        instance_key,
        chat_key,
        tenant_group_daily_limit=2,
        tenant_limit_unit="replies",
        tenant_pause_message="这个群今天先到这里",
    )


class TestOwnershipChangeClearsTenantQuotaSettings:
    """实例换了主人，旧租户调低的额度与暂停提示就该清掉

    那是旧主人对他自己的群设的，新主人既没填过也不知道单位快照是哪一档；留着等于
    让新租户继承一个他从没见过的数字。与「要不要顺手删记忆」是两件事，所以不受那个勾选框控制。
    """

    async def test_assigning_clears_both_layers_of_the_assigned_instance(self, db) -> None:  # noqa: ARG002
        tenant_a, _ = await _tenant_pair()
        await _instance(INSTANCE, None)
        await _seed_tenant_settings(INSTANCE, OLD_CHAT)

        await tenants_router.bind_tenant_instances(TENANT_A_ID, _bind(TENANT_A_ID, [INSTANCE]), _platform_owner())

        assert await _tenant_values("instance", INSTANCE) == ALL_CLEARED
        assert await _tenant_values("channel", INSTANCE, OLD_CHAT) == ALL_CLEARED
        assert tenant_a.id == TENANT_A_ID

    async def test_releasing_clears_too(self, db) -> None:  # noqa: ARG002
        await _tenant_pair()
        await _instance(INSTANCE, TENANT_A_ID)
        await _seed_tenant_settings(INSTANCE, OLD_CHAT)

        await tenants_router.bind_tenant_instances(TENANT_A_ID, _bind(TENANT_A_ID, []), _platform_owner())

        assert await _tenant_values("instance", INSTANCE) == ALL_CLEARED
        assert await _tenant_values("channel", INSTANCE, OLD_CHAT) == ALL_CLEARED

    async def test_replaying_the_same_binding_clears_nothing(self, db) -> None:  # noqa: ARG002
        """覆盖语义下原样重放是常态；它不该把还没交接的设置顺手抹掉"""
        await _tenant_pair()
        await _instance(INSTANCE, TENANT_A_ID)
        await _seed_tenant_settings(INSTANCE, OLD_CHAT)

        await tenants_router.bind_tenant_instances(TENANT_A_ID, _bind(TENANT_A_ID, [INSTANCE]), _platform_owner())

        assert (await _tenant_values("instance", INSTANCE))["tenant_group_daily_limit"] == 5
        assert (await _tenant_values("channel", INSTANCE, OLD_CHAT))["tenant_pause_message"] == "这个群今天先到这里"

    async def test_platform_fields_survive(self, db) -> None:  # noqa: ARG002
        """平台方的承诺与实例归谁无关：清租户设置不能顺手把额度、开关、单位也清掉"""
        await _tenant_pair()
        await _instance(INSTANCE, None)
        await _policy(
            "instance",
            INSTANCE,
            enabled=True,
            unit="tokens",
            group_daily_limit=40,
            private_daily_limit=6,
            over_limit_action="alert",
            pause_message="平台方的提示",
            tenant_group_daily_limit=5,
            tenant_limit_unit="replies",
        )

        await tenants_router.bind_tenant_instances(TENANT_A_ID, _bind(TENANT_A_ID, [INSTANCE]), _platform_owner())

        row = await DBChannelQuotaPolicy.get(scope="instance", instance_key=INSTANCE)
        assert (row.enabled, row.unit, row.group_daily_limit, row.private_daily_limit) == (True, "tokens", 40, 6)
        assert (row.over_limit_action, row.pause_message) == ("alert", "平台方的提示")
        assert row.tenant_group_daily_limit is None

    async def test_other_instances_are_untouched(self, db) -> None:  # noqa: ARG002
        await _tenant_pair()
        await _instance(INSTANCE, None)
        await _instance(OTHER_INSTANCE, TENANT_B_ID)
        await _seed_tenant_settings(INSTANCE, OLD_CHAT)
        await _seed_tenant_settings(OTHER_INSTANCE, OTHER_CHAT)

        await tenants_router.bind_tenant_instances(TENANT_A_ID, _bind(TENANT_A_ID, [INSTANCE]), _platform_owner())

        assert (await _tenant_values("instance", OTHER_INSTANCE))["tenant_group_daily_limit"] == 5
        assert (await _tenant_values("channel", OTHER_INSTANCE, OTHER_CHAT))["tenant_pause_message"] == "这个群今天先到这里"

    async def test_resolution_reads_the_cleared_value_immediately(self, db) -> None:  # noqa: ARG002
        """判定读的是进程内缓存：清完不失效的话，最多还会按旧值拦一分钟"""
        await _tenant_pair()
        await _instance(INSTANCE, None)
        await _policy("global", enabled=True, unit="replies", group_daily_limit=50, private_daily_limit=0, over_limit_action="pause")
        await _seed_tenant_settings(INSTANCE, OLD_CHAT)
        before = await resolve_policy(ADAPTER, INSTANCE, OLD_CHAT, "group")
        assert before.tenant_limit == 2

        await tenants_router.bind_tenant_instances(TENANT_A_ID, _bind(TENANT_A_ID, [INSTANCE]), _platform_owner())

        after = await resolve_policy(ADAPTER, INSTANCE, OLD_CHAT, "group")
        assert after.tenant_limit is None
        assert after.effective_limit == 50

    async def test_preview_counts_the_settings_it_will_clear(self, db) -> None:  # noqa: ARG002
        """预览的数字与保存时真正清掉的行数必须是同一个判据"""
        await _tenant_pair()
        await _instance(INSTANCE, None)
        await _instance(OTHER_INSTANCE, None)
        await _seed_tenant_settings(INSTANCE, OLD_CHAT)
        # 平台方字段有值、租户字段全空的行不算一处设置
        await _policy("channel", INSTANCE, NEW_CHAT, group_daily_limit=9)
        await _seed_tenant_settings(OTHER_INSTANCE, OTHER_CHAT)

        preview = await tenants_router.preview_tenant_instances(
            tenant_id=TENANT_A_ID,
            body=_bind(TENANT_A_ID, [INSTANCE, OTHER_INSTANCE]),
            _current_user=_platform_owner(),
        )

        counts = {change.instance_key: change.channel_quota_tenant_settings for change in preview.changes}
        assert counts == {INSTANCE: 2, OTHER_INSTANCE: 2}

    async def test_preview_is_zero_when_nothing_is_set(self, db) -> None:  # noqa: ARG002
        await _tenant_pair()
        await _instance(INSTANCE, None)
        await _policy("channel", INSTANCE, OLD_CHAT, group_daily_limit=9)

        preview = await tenants_router.preview_tenant_instances(
            tenant_id=TENANT_A_ID,
            body=_bind(TENANT_A_ID, [INSTANCE]),
            _current_user=_platform_owner(),
        )

        assert [change.channel_quota_tenant_settings for change in preview.changes] == [0]
