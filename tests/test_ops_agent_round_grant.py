"""本轮运维授权：签发、撤销、过期，以及每一次调用前的复核

## 这里钉的两条安全边界

1. **授权按容器键**，不按会话：沙盒回调带回的 `container_key` 由宿主生成、经令牌校验，
   模型改不了。按会话授权会让「同一频道连着两轮」时把上一轮的许可留给下一轮。
2. **执行前必须复核**。`open_round` 是在回合开始时判的，而这一轮可能已经跑了几分钟：
   平台方把工具关了、把某人移出名单、把实例划给租户，任何一条都必须在**这一次调用**上
   生效，而不是等下一轮。所以这里每一条拒绝理由都从「签发时一切正常、之后变掉」的角度测。

开关关着时不查库也是硬约束：存量部署（默认关）的每条消息、每个回合都必须与改造前逐字一致。
"""

from collections import deque
from typing import Any, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.services.ops_agent import round_grant as grant_module
from nekro_agent.services.ops_agent.round_grant import (
    ROUND_TTL_SECONDS,
    authorize_call,
    build_round_facts,
    close_round,
    current_grant_for_chat,
    get,
    mark_tainted,
    open_round,
    reset_round_grants,
    reset_write_counters,
)
from nekro_agent.services.ops_agent.triggers import (
    TRIGGER_KIND_HUMAN,
    TRIGGER_KIND_TIMER,
    RoundTrigger,
    round_trigger_book,
)
from nekro_agent.services.sandbox.runner import build_container_key

ADAPTER = "onebot_v11"
INSTANCE = "opsinst"
CHANNEL_ID = "group_10001"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-{CHANNEL_ID}"
PRIVATE_CHANNEL_ID = "private_10001"
PRIVATE_CHAT_KEY = f"{ADAPTER}-{INSTANCE}-{PRIVATE_CHANNEL_ID}"
OPERATOR = "10001"
OTHER_OPERATOR = "20002"
OUTSIDER = "30003"
CONTAINER = build_container_key(CHAT_KEY)


def human(sender_id: str = OPERATOR, *, trusted: bool = True) -> RoundTrigger:
    return RoundTrigger(
        kind=TRIGGER_KIND_HUMAN,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        sender_id=sender_id,
        operator_trusted=trusted,
    )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def clean_state():
    reset_round_grants()
    yield
    reset_round_grants()


@pytest.fixture
def ops_on(monkeypatch: pytest.MonkeyPatch):
    """打开总开关并配好一个人、一个群；用例可以再改"""

    def apply(**overrides: Any) -> None:
        settings: dict[str, Any] = {
            "OPS_AGENT_TOOLS_ENABLED": True,
            "OPS_AGENT_OPERATORS": [OPERATOR, OTHER_OPERATOR],
            "OPS_AGENT_GROUPS": [CHAT_KEY],
            "OPS_AGENT_PRIVATE_ENABLED": True,
        }
        settings.update(overrides)
        for name, value in settings.items():
            monkeypatch.setattr(config, name, value)

    apply()
    return apply


async def seed_instance(
    *,
    instance_key: str = INSTANCE,
    status: str = "online",
    enabled: bool = True,
    tenant_id: Optional[int] = None,
    adapter_key: str = ADAPTER,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=instance_key,
        status=status,
        enabled=enabled,
        provider="remote_onebot",
        tenant_id=tenant_id,
    )


async def seed_channel(*, channel_id: str = CHANNEL_ID, chat_key: str = CHAT_KEY, channel_type: str = "group") -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id=channel_id,
        chat_key=chat_key,
        channel_type=channel_type,
        data="{}",
    )


async def prepared(*, channel_id: str = CHANNEL_ID, chat_key: str = CHAT_KEY, channel_type: str = "group") -> DBChatChannel:
    """平台方实例 + 频道行 + 一条可信的运维人员触发"""
    await seed_instance()
    channel = await seed_channel(channel_id=channel_id, chat_key=chat_key, channel_type=channel_type)
    round_trigger_book.record(chat_key, human())
    return channel


class TestSwitchOff:
    async def test_open_round_returns_none_without_touching_the_database(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """存量部署每条消息都走这里：多一次查询就是多一个故障点"""
        monkeypatch.setattr(config, "OPS_AGENT_TOOLS_ENABLED", False)

        class _Explode:
            @staticmethod
            def filter(**_kwargs: Any) -> Any:
                raise AssertionError("开关关着时不得查库")

        monkeypatch.setattr(DBAdapterInstance, "filter", _Explode)
        channel = await seed_channel()
        round_trigger_book.record(CHAT_KEY, human())

        assert await open_round(channel) is None
        assert get(build_container_key(CHAT_KEY)) is None

    async def test_authorize_call_refuses_everything(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None

        ops_on(OPS_AGENT_TOOLS_ENABLED=False)
        allowed, reason = await authorize_call(CONTAINER, CHAT_KEY, "ops_overview")
        assert (allowed, reason) == (False, "disabled")

    async def test_leftover_triggers_are_dropped(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """开关打开期间记下、关掉之后才轮到的触发要丢掉

        留着的话，开关下次打开时它们早已过期，会把那个频道的第一轮判成「批次不完整」。
        丢掉只动内存，不查库。
        """
        monkeypatch.setattr(config, "OPS_AGENT_TOOLS_ENABLED", False)
        channel = await seed_channel()
        round_trigger_book.record(CHAT_KEY, human())

        assert await open_round(channel) is None
        assert round_trigger_book.peek(CHAT_KEY).triggers == []


class TestSigning:
    async def test_allowed_round_signs_a_grant(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        grant = await open_round(channel)

        assert grant is not None
        assert grant.chat_key == CHAT_KEY
        assert grant.adapter_key == ADAPTER and grant.instance_key == INSTANCE
        assert grant.operators == frozenset({OPERATOR})
        assert grant.container_key == CONTAINER
        assert grant.is_group is True and grant.private_counterpart == ""
        assert grant.writes_done == 0 and grant.tainted is False
        assert get(CONTAINER) is grant
        assert current_grant_for_chat(CHAT_KEY) is grant

    async def test_the_round_id_contains_the_chat_key_and_is_unique(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        first = await open_round(channel)
        round_trigger_book.record(CHAT_KEY, human())
        second = await open_round(channel)

        assert first is not None and second is not None
        assert CHAT_KEY in first.round_id
        assert first.round_id != second.round_id

    async def test_the_grant_only_covers_the_round_it_was_signed_for(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        first = await open_round(channel)
        round_trigger_book.record(CHAT_KEY, human())
        second = await open_round(channel)

        assert first is not None and second is not None
        assert get(CONTAINER) is second, "新的一轮覆盖旧的"

    async def test_denied_round_clears_the_previous_grant(self, db, ops_on) -> None:  # noqa: ARG002
        """上一轮的授权不能因为这一轮没通过就留在注册表里"""
        channel = await prepared()
        assert await open_round(channel) is not None

        ops_on(OPS_AGENT_GROUPS=["onebot_v11-opsinst-group_99999"])
        round_trigger_book.record(CHAT_KEY, human())
        assert await open_round(channel) is None
        assert get(CONTAINER) is None

    async def test_no_trigger_means_no_grant(self, db, ops_on) -> None:  # noqa: ARG002
        await seed_instance()
        channel = await seed_channel()
        assert await open_round(channel) is None

    async def test_the_batch_is_consumed_by_the_round(self, db, ops_on) -> None:  # noqa: ARG002
        """回合跑起来之后到达的触发属于下一轮，不能算进这一轮"""
        channel = await prepared()
        assert await open_round(channel) is not None
        assert round_trigger_book.peek(CHAT_KEY).triggers == []

        round_trigger_book.record(CHAT_KEY, human())
        assert await open_round(channel) is not None, "下一批全是可信触发，照样能开一轮"

    async def test_a_trigger_arriving_mid_round_does_not_downgrade_the_current_one(self, db, ops_on) -> None:  # noqa: ARG002
        """这一轮已经判定过并签发了授权；中途混进别人的消息属于下一轮，不能反过来把当前这轮撤掉"""
        channel = await prepared()
        grant = await open_round(channel)
        assert grant is not None

        round_trigger_book.record(CHAT_KEY, human(OUTSIDER, trusted=False))
        assert get(CONTAINER) is grant
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (True, "")
        # 生产路径里这一轮的收尾会清掉快照；这里显式收尾，模拟「下一条消息开启下一轮」
        round_trigger_book.end_round(CHAT_KEY)
        assert await open_round(channel) is None, "下一轮才因为那条触发被拒"

    async def test_a_mixed_batch_denies_the_round(self, db, ops_on) -> None:  # noqa: ARG002
        await seed_instance()
        channel = await seed_channel()
        round_trigger_book.record(CHAT_KEY, human())
        round_trigger_book.record(CHAT_KEY, RoundTrigger(kind=TRIGGER_KIND_TIMER))

        assert await open_round(channel) is None


class TestRoundFacts:
    async def test_a_platform_instance_is_owned(self, db) -> None:
        await seed_instance()
        facts = await build_round_facts(await seed_channel())
        assert facts.instance_platform_owned is True
        assert facts.is_group is True and facts.is_private is False
        assert facts.private_counterpart == ""

    async def test_a_tenant_instance_is_not(self, db) -> None:
        tenant = await DBTenant.create(name="acme")
        await seed_instance(tenant_id=tenant.id)
        facts = await build_round_facts(await seed_channel())
        assert facts.instance_platform_owned is False

    @pytest.mark.parametrize("status", ["deleted", "disabled", "archived"])
    async def test_a_tombstoned_or_stopped_instance_is_not(self, db, status: str) -> None:
        await seed_instance(status=status, enabled=False)
        facts = await build_round_facts(await seed_channel())
        assert facts.instance_platform_owned is False

    async def test_a_disabled_switch_on_the_instance_row_is_not(self, db) -> None:
        """`enabled=False` 与状态列是两处开关，任何一处关掉都不该放行"""
        await seed_instance(enabled=False, status="online")
        facts = await build_round_facts(await seed_channel())
        assert facts.instance_platform_owned is False

    async def test_a_missing_instance_is_not(self, db) -> None:
        facts = await build_round_facts(await seed_channel())
        assert facts.instance_platform_owned is False

    async def test_a_tenant_suspension_is_not(self, db) -> None:
        """平台方实例不存在这个问题；但实例若被挂到停用的租户上，可用性判据要跟着变"""
        tenant = await DBTenant.create(name="acme", status="suspended")
        await seed_instance(tenant_id=tenant.id)
        facts = await build_round_facts(await seed_channel())
        assert facts.instance_platform_owned is False

    async def test_a_private_channel_resolves_the_counterpart(self, db) -> None:
        await seed_instance()
        facts = await build_round_facts(await seed_channel(channel_id=PRIVATE_CHANNEL_ID, chat_key=PRIVATE_CHAT_KEY, channel_type="private"))
        assert facts.is_private is True and facts.is_group is False
        assert facts.private_counterpart == OPERATOR

    async def test_a_non_onebot_private_channel_has_no_counterpart(self, db) -> None:
        await seed_instance(adapter_key="sse", instance_key="sse1")
        channel = await DBChatChannel.create(
            adapter_key="sse",
            instance_key="sse1",
            channel_id=PRIVATE_CHANNEL_ID,
            chat_key=f"sse-sse1-{PRIVATE_CHANNEL_ID}",
            channel_type="private",
            data="{}",
        )
        facts = await build_round_facts(channel)
        assert facts.private_counterpart == ""


class TestCloseAndExpiry:
    async def test_close_round_drops_the_grant(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None
        close_round(CONTAINER)
        assert get(CONTAINER) is None
        allowed, reason = await authorize_call(CONTAINER, CHAT_KEY, "ops_overview")
        assert (allowed, reason) == (False, "no_grant")

    async def test_close_round_is_idempotent(self) -> None:
        close_round("sandbox_nothing_here")
        close_round("sandbox_nothing_here")

    async def test_a_grant_older_than_the_ttl_is_gone(self, db, ops_on, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await prepared()
        grant = await open_round(channel)
        assert grant is not None

        import time as time_module

        now = time_module.monotonic()
        monkeypatch.setattr(grant_module.time, "monotonic", lambda: now + ROUND_TTL_SECONDS + 1)
        assert get(CONTAINER) is None
        allowed, reason = await authorize_call(CONTAINER, CHAT_KEY, "ops_overview")
        assert (allowed, reason) == (False, "no_grant")

    async def test_a_grant_inside_the_ttl_survives(self, db, ops_on, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None

        import time as time_module

        now = time_module.monotonic()
        monkeypatch.setattr(grant_module.time, "monotonic", lambda: now + ROUND_TTL_SECONDS - 1)
        allowed, reason = await authorize_call(CONTAINER, CHAT_KEY, "ops_overview")
        assert (allowed, reason) == (True, "")

    def test_the_ttl_is_ten_minutes(self) -> None:
        assert ROUND_TTL_SECONDS == 600


class TestAuthorizeRechecks:
    """签发之后把条件一条条改坏：每一条都必须在**这一次调用**上拒绝"""

    async def test_allowed_round_passes(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (True, "")

    async def test_unknown_container_is_refused(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None
        assert await authorize_call("sandbox_other", CHAT_KEY, "ops_overview") == (False, "no_grant")

    async def test_a_mismatched_chat_key_is_refused(self, db, ops_on) -> None:  # noqa: ARG002
        """回调带来的 chat_key 与授权上的不一致：这一轮的容器被拿去查别人的频道"""
        channel = await prepared()
        assert await open_round(channel) is not None
        assert await authorize_call(CONTAINER, "onebot_v11-opsinst-group_20002", "ops_overview") == (False, "chat_key_mismatch")

    async def test_removing_a_group_refuses(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None

        ops_on(OPS_AGENT_GROUPS=["onebot_v11-opsinst-group_99999"])
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (False, "not_ops_group")

    async def test_removing_the_operator_refuses(self, db, ops_on) -> None:  # noqa: ARG002
        """把人移出名单必须热生效，不能等他那一轮跑完"""
        channel = await prepared()
        assert await open_round(channel) is not None

        ops_on(OPS_AGENT_OPERATORS=[OUTSIDER])
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (False, "operator_removed")

    async def test_emptying_the_operator_list_refuses(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None

        ops_on(OPS_AGENT_OPERATORS=[], OPS_BOT_OPERATORS=[])
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (False, "operator_removed")

    async def test_handing_the_instance_to_a_tenant_refuses(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None

        tenant = await DBTenant.create(name="acme")
        await DBAdapterInstance.filter(instance_key=INSTANCE).update(tenant_id=tenant.id)
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (False, "tenant_instance")

    async def test_disabling_the_instance_refuses(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None

        await DBAdapterInstance.filter(instance_key=INSTANCE).update(enabled=False, status="disabled")
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (False, "tenant_instance")

    async def test_deleting_the_instance_row_refuses(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None

        await DBAdapterInstance.all().delete()
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (False, "tenant_instance")

    async def test_private_disabled_after_signing_refuses(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared(channel_id=PRIVATE_CHANNEL_ID, chat_key=PRIVATE_CHAT_KEY, channel_type="private")
        round_trigger_book.record(PRIVATE_CHAT_KEY, human())
        grant = await open_round(channel)
        assert grant is not None and grant.is_group is False
        container = build_container_key(PRIVATE_CHAT_KEY)

        ops_on(OPS_AGENT_PRIVATE_ENABLED=False)
        assert await authorize_call(container, PRIVATE_CHAT_KEY, "ops_overview") == (False, "private_disabled")

    async def test_private_counterpart_removed_from_the_list_refuses(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared(channel_id=PRIVATE_CHANNEL_ID, chat_key=PRIVATE_CHAT_KEY, channel_type="private")
        round_trigger_book.record(PRIVATE_CHAT_KEY, human())
        assert await open_round(channel) is not None
        container = build_container_key(PRIVATE_CHAT_KEY)

        ops_on(OPS_AGENT_OPERATORS=[OUTSIDER])
        assert await authorize_call(container, PRIVATE_CHAT_KEY, "ops_overview") == (False, "not_operator_private")

    async def test_private_round_passes(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared(channel_id=PRIVATE_CHANNEL_ID, chat_key=PRIVATE_CHAT_KEY, channel_type="private")
        round_trigger_book.record(PRIVATE_CHAT_KEY, human())
        grant = await open_round(channel)
        assert grant is not None
        container = build_container_key(PRIVATE_CHAT_KEY)
        assert await authorize_call(container, PRIVATE_CHAT_KEY, "ops_overview") == (True, "")

    async def test_an_unclassifiable_grant_is_refused(self, db, ops_on) -> None:  # noqa: ARG002
        """既不是群也不是私聊的授权（只能由将来的调用方构造出来）：复核必须给拒绝，而不是默认放行"""
        await seed_instance()
        grant = grant_module.OpsRoundGrant(
            round_id="probe",
            container_key=CONTAINER,
            chat_key=CHAT_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            operators=frozenset({OPERATOR}),
        )
        grant_module._grants[CONTAINER] = grant  # noqa: SLF001

        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (False, "unsupported_channel")


class TestTainted:
    async def test_marking_sets_the_flag(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        grant = await open_round(channel)
        assert grant is not None and grant.tainted is False

        mark_tainted(CONTAINER)
        assert grant.tainted is True

    async def test_marking_without_a_grant_does_nothing(self) -> None:
        mark_tainted("sandbox_nothing_here")

    async def test_marking_twice_keeps_it_set(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        grant = await open_round(channel)
        assert grant is not None
        mark_tainted(CONTAINER)
        mark_tainted(CONTAINER)
        assert grant.tainted is True

    async def test_tainting_does_not_block_the_read_only_tools(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None
        mark_tainted(CONTAINER)
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (True, "")


async def round_with_operators(*operators: str) -> Any:
    """平台方实例 + 频道 + 每位运维人员一条可信触发，返回已签发的授权"""
    await seed_instance()
    channel = await seed_channel()
    for operator in operators:
        round_trigger_book.record(CHAT_KEY, human(operator))
    grant = await open_round(channel)
    assert grant is not None
    return grant


class TestWriteLimits:
    """写操作在五条常规判定之后还要过污染标记与两道次数上限"""

    async def test_a_tainted_round_refuses_writes_but_not_reads(self, db, ops_on) -> None:  # noqa: ARG002
        grant = await round_with_operators(OPERATOR)
        mark_tainted(CONTAINER)

        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_overview") == (True, "")
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (False, "tainted")
        assert grant.writes_done == 0, "被拒的调用不计数"

    async def test_tools_off_is_exempt_from_taint_and_limits(self, db, ops_on) -> None:  # noqa: ARG002
        ops_on(OPS_AGENT_WRITE_LIMIT_PER_ROUND=0, OPS_AGENT_WRITE_LIMIT_PER_HOUR=0)
        grant = await round_with_operators(OPERATOR)
        mark_tainted(CONTAINER)

        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_tools_off", is_write=True) == (True, "")
        assert grant.writes_done == 0, "关闭工具只会缩小权限，不占次数"

    async def test_the_per_round_limit(self, db, ops_on) -> None:  # noqa: ARG002
        ops_on(OPS_AGENT_WRITE_LIMIT_PER_ROUND=2)
        grant = await round_with_operators(OPERATOR)

        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (True, "")
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_quota_bonus", is_write=True) == (True, "")
        assert grant.writes_done == 2

        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_channel_active", is_write=True) == (False, "round_limit")
        assert grant.writes_done == 2, "被拒的那次不计数"

    async def test_a_per_round_limit_of_zero_disallows_every_write(self, db, ops_on) -> None:  # noqa: ARG002
        ops_on(OPS_AGENT_WRITE_LIMIT_PER_ROUND=0)
        await round_with_operators(OPERATOR)
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (False, "round_limit")

    async def test_the_count_happens_on_the_allow_not_on_success(self, db, ops_on) -> None:  # noqa: ARG002
        """上限限制的是一轮能动多少次手：放行就计数，业务函数失败也算一次"""
        ops_on(OPS_AGENT_WRITE_LIMIT_PER_ROUND=1)
        grant = await round_with_operators(OPERATOR)

        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (True, "")
        assert grant.writes_done == 1
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (False, "round_limit")

    async def test_the_hourly_limit_and_window_recovery(self, db, ops_on, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        clock = [1000.0]
        monkeypatch.setattr(grant_module, "_write_clock", lambda: clock[0])
        ops_on(OPS_AGENT_WRITE_LIMIT_PER_ROUND=10, OPS_AGENT_WRITE_LIMIT_PER_HOUR=2)
        await round_with_operators(OPERATOR)

        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (True, "")
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_quota_bonus", is_write=True) == (True, "")
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_channel_active", is_write=True) == (False, "hourly_limit")

        clock[0] += 3601
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_channel_active", is_write=True) == (True, "")

    async def test_any_operator_at_the_hourly_limit_rejects_the_round(self, db, ops_on) -> None:  # noqa: ARG002
        ops_on(OPS_AGENT_OPERATORS=[OPERATOR, OTHER_OPERATOR], OPS_AGENT_WRITE_LIMIT_PER_HOUR=1)
        await round_with_operators(OPERATOR, OTHER_OPERATOR)
        now = grant_module._write_clock()  # noqa: SLF001 - 用同一口时钟，避免自己造一个时间基准
        grant_module._write_events[(ADAPTER, OTHER_OPERATOR)] = deque([now])  # noqa: SLF001

        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (False, "hourly_limit")

    async def test_a_hourly_limit_of_zero_disallows_every_write(self, db, ops_on) -> None:  # noqa: ARG002
        ops_on(OPS_AGENT_WRITE_LIMIT_PER_HOUR=0)
        await round_with_operators(OPERATOR)
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (False, "hourly_limit")

    async def test_reset_write_counters_clears_the_window(self, db, ops_on) -> None:  # noqa: ARG002
        ops_on(OPS_AGENT_WRITE_LIMIT_PER_HOUR=1)
        await round_with_operators(OPERATOR)
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (True, "")
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (False, "hourly_limit")

        reset_write_counters()
        assert await authorize_call(CONTAINER, CHAT_KEY, "ops_set_instance_enabled", is_write=True) == (True, "")


class TestReset:
    async def test_reset_drops_grants_and_triggers(self, db, ops_on) -> None:  # noqa: ARG002
        channel = await prepared()
        assert await open_round(channel) is not None
        round_trigger_book.record(CHAT_KEY, human())

        reset_round_grants()
        assert get(CONTAINER) is None
        assert round_trigger_book.peek(CHAT_KEY).triggers == []


def test_container_key_comes_from_the_runner() -> None:
    """容器键的生成规则只能有一处：两边算出不同的键会让关联静默失效"""
    from nekro_agent.services.ops_agent.round_grant import _container_key

    assert _container_key(CHAT_KEY) == build_container_key(CHAT_KEY)
    assert CONTAINER.startswith("sandbox_")
