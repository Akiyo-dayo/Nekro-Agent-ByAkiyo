"""运维写操作：目标解析、业务函数复用、回执与失败脱敏

## 这一层为什么值得单独测

写操作直接改系统状态，而目标标识与名字都可能是租户可改的数据。这里钉四件事：

1. **实例目标成对**：同名实例跨适配器时，运维工具按 `(adapter_key, instance_key)` 精确命中；
   `/na_ops` 只给实例标识且候选不止一个时必须报 `目标不明确`，不能替人挑第一个。
2. **业务函数就是总后台那一份**：每个写操作都调 `services/console/` / `channel_quota` / 配置服务
   里正在用的函数，而不是另写一遍。
3. **回执固定且不带上游原文**：异常里可能有用户原话甚至凭据，回执与返回值里只出现固定类别。
4. **回执不产生对话副作用**：不写聊天消息、不调度回合、不计用量。
"""

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_channel_quota import DBChannelQuotaDaily
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.tenant import TenantStatus
from nekro_agent.services.channel_quota import counter
from nekro_agent.services.console import instance_manager
from nekro_agent.services.ops_agent import receipt as receipt_module
from nekro_agent.services.ops_agent import round_grant as grant_module
from nekro_agent.services.ops_agent import tools as tools_module
from nekro_agent.services.ops_agent import writes as writes_module
from nekro_agent.services.ops_agent.round_grant import OpsRoundGrant
from nekro_agent.services.ops_agent.tools import (
    ops_quota_bonus,
    ops_restart_protocol,
    ops_set_channel_active,
    ops_set_instance_enabled,
    ops_set_tenant_status,
    ops_tools_off,
)
from nekro_agent.services.ops_agent.writes import WriteOutcome
from nekro_agent.services.sandbox.runner import build_container_key

ADAPTER = "onebot_v11"
INSTANCE = "inst3"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_10001"
OPERATOR = "10001"
OTHER_OPERATOR = "20002"
CONTAINER = build_container_key(CHAT_KEY)
SECRET = "sk-live-9f3c2a7d1e"


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
    grant_module.reset_round_grants()
    yield
    grant_module.reset_round_grants()


async def seed_instance(
    *,
    adapter_key: str = ADAPTER,
    instance_key: str = INSTANCE,
    display_name: str = "星炬学院主号",
    status: str = "online",
    enabled: bool = True,
    provider: str = "remote_onebot",
    tenant_id: Optional[int] = None,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name=display_name,
        status=status,
        enabled=enabled,
        provider=provider,
        tenant_id=tenant_id,
    )


async def seed_channel(
    *,
    chat_key: str = CHAT_KEY,
    channel_name: str = "测试群",
    channel_type: str = "group",
) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_10001",
        chat_key=chat_key,
        channel_name=channel_name,
        channel_type=channel_type,
        data="{}",
    )


def install_grant(operators: Tuple[str, ...] = (OPERATOR,)) -> OpsRoundGrant:
    grant = OpsRoundGrant(
        round_id="test-round",
        container_key=CONTAINER,
        chat_key=CHAT_KEY,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        operators=frozenset(operators),
    )
    grant_module._grants[CONTAINER] = grant  # noqa: SLF001 - 直接放一份授权，避免每个工具用例都建库
    return grant


def ctx() -> AgentCtx:
    return AgentCtx(from_chat_key=CHAT_KEY, container_key=CONTAINER)


class TestFormatReceipt:
    def test_success(self) -> None:
        text = receipt_module.format_receipt(WriteOutcome(ok=True, action_label="停用实例", target_label="目标"), ["10001"])
        assert text == '【运维】已执行：停用实例 目标。操作人 "10001"。'

    def test_failure_with_candidates(self) -> None:
        outcome = WriteOutcome(
            ok=False,
            action_label="停用实例",
            target_label="inst3",
            error_category="目标不明确",
            candidates=("a:inst3", "b:inst3", "c:inst3", "d:inst3", "e:inst3", "f:inst3"),
        )
        text = receipt_module.format_receipt(outcome, ["20002", "10001"])
        assert text.startswith("【运维】未执行：停用实例 inst3。原因：目标不明确。候选：")
        assert text.count('inst3"') == 5, "候选最多五个"
        assert text.endswith('操作人 "10001"、"20002"。')

    def test_empty_operators(self) -> None:
        assert "操作人 -。" in receipt_module.format_receipt(WriteOutcome(ok=True, action_label="动作", target_label="目标"), [])

    def test_control_characters_and_newlines_are_squashed(self) -> None:
        outcome = WriteOutcome(ok=True, action_label="动作", target_label="目标")
        text = receipt_module.format_receipt(outcome, ["10\n001\x00"])
        assert "\n" not in text and "\x00" not in text
        assert '"10 001"' in text, "换行被压成空格，回执仍是单行"


class _FakeAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.requests: List[Any] = []
        self.fail = fail

    async def forward_message(self, request: Any) -> Any:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError(f"上游拒绝: {SECRET}")
        return SimpleNamespace(success=True, error_message="")


class TestSendReceipt:
    async def test_it_sends_without_creating_chat_side_effects(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _FakeAdapter()

        async def get_adapter(_chat_key: str) -> Any:
            return adapter

        async def passthrough(request: Any) -> Any:
            return request

        @contextmanager
        def no_filter():
            yield

        schedule_calls: List[Any] = []
        counter_calls: List[Any] = []
        monkeypatch.setattr(receipt_module.adapter_utils, "get_adapter_for_chat", get_adapter)
        monkeypatch.setattr(receipt_module, "filter_platform_request", passthrough)
        monkeypatch.setattr(receipt_module, "outbound_filtered", no_filter)
        monkeypatch.setattr(
            "nekro_agent.services.message_service.message_service.schedule_agent_task",
            lambda **kwargs: schedule_calls.append(kwargs),
        )
        monkeypatch.setattr(counter, "record_reply", lambda *a, **k: counter_calls.append(("reply", a, k)))
        monkeypatch.setattr(counter, "record_tokens", lambda *a, **k: counter_calls.append(("tokens", a, k)))

        assert await receipt_module.send_receipt(CHAT_KEY, "回执") is True

        assert len(adapter.requests) == 1
        assert adapter.requests[0].chat_key == CHAT_KEY
        assert adapter.requests[0].segments[0].content == "回执"
        assert await DBChatMessage.all().count() == 0
        assert schedule_calls == []
        assert counter_calls == []

    async def test_send_failure_returns_false_and_never_raises(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _FakeAdapter(fail=True)

        async def get_adapter(_chat_key: str) -> Any:
            return adapter

        async def passthrough(request: Any) -> Any:
            return request

        @contextmanager
        def no_filter():
            yield

        monkeypatch.setattr(receipt_module.adapter_utils, "get_adapter_for_chat", get_adapter)
        monkeypatch.setattr(receipt_module, "filter_platform_request", passthrough)
        monkeypatch.setattr(receipt_module, "outbound_filtered", no_filter)

        result = await receipt_module.send_receipt(CHAT_KEY, "回执")

        assert result is False
        assert SECRET not in str(result)

    async def test_an_adapter_lookup_exception_is_also_swallowed(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        async def get_adapter(_chat_key: str) -> Any:
            raise RuntimeError(f"适配器注册表坏了: {SECRET}")

        monkeypatch.setattr(receipt_module.adapter_utils, "get_adapter_for_chat", get_adapter)

        assert await receipt_module.send_receipt(CHAT_KEY, "回执") is False


class TestInstanceWrites:
    async def test_disable_then_enable_changes_the_row(self, db) -> None:
        await seed_instance()
        outcome = await writes_module.set_instance_enabled(ADAPTER, INSTANCE, False)
        assert outcome.ok and outcome.action_label == "停用实例"
        assert "星炬学院主号" in outcome.target_label
        row = await DBAdapterInstance.get(instance_key=INSTANCE)
        assert row.enabled is False

        again = await writes_module.set_instance_enabled(ADAPTER, INSTANCE, True)
        assert again.ok and again.action_label == "启用实例"
        row = await DBAdapterInstance.get(instance_key=INSTANCE)
        assert row.enabled is True

    async def test_missing_instance(self, db) -> None:
        outcome = await writes_module.set_instance_enabled(ADAPTER, "nope", False)
        assert (outcome.ok, outcome.error_category) == (False, "目标不存在")

    async def test_a_remote_instance_cannot_be_restarted(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        await seed_instance(provider="remote_onebot")
        calls: List[Any] = []

        async def fake_restart(instance: Any) -> None:
            calls.append(instance)

        monkeypatch.setattr(writes_module, "restart_container", fake_restart)
        outcome = await writes_module.restart_protocol(ADAPTER, INSTANCE)
        assert (outcome.ok, outcome.error_category) == (False, "不支持该实例")
        assert calls == []

    async def test_a_builtin_instance_restarts_through_the_console_function(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        await seed_instance(provider="builtin_napcat")
        calls: List[Any] = []

        async def fake_restart(instance: Any) -> None:
            calls.append(instance)

        monkeypatch.setattr(writes_module, "restart_container", fake_restart)
        outcome = await writes_module.restart_protocol(ADAPTER, INSTANCE)
        assert outcome.ok is True
        assert [instance.instance_key for instance in calls] == [INSTANCE]

    async def test_same_name_across_adapters_is_hit_exactly(self, db) -> None:
        await seed_instance(adapter_key="adapter_a", instance_key="inst3", display_name="A")
        await seed_instance(adapter_key="adapter_b", instance_key="inst3", display_name="B")

        outcome = await writes_module.set_instance_enabled("adapter_b", "inst3", False)

        assert outcome.ok is True
        assert (await DBAdapterInstance.get(adapter_key="adapter_a", instance_key="inst3")).enabled is True
        assert (await DBAdapterInstance.get(adapter_key="adapter_b", instance_key="inst3")).enabled is False

    @pytest.mark.parametrize("flag", ["false", "False", "0", 0, 1, None])
    async def test_a_non_boolean_flag_is_refused_not_coerced(self, db, flag: Any) -> None:
        """`bool("false")` 是 True：宽松转换会把「停用」执行成「启用」"""
        await seed_instance(enabled=False)

        outcome = await writes_module.set_instance_enabled(ADAPTER, INSTANCE, flag)

        assert (outcome.ok, outcome.error_category) == (False, "参数不合法")
        assert (await DBAdapterInstance.get(instance_key=INSTANCE)).enabled is False


class TestTenantWrites:
    async def test_suspend_and_resume(self, db) -> None:
        tenant = await DBTenant.create(name="acme", display_name="Acme")
        suspended = await writes_module.set_tenant_status(tenant.id, "suspended")
        assert suspended.ok and suspended.action_label == "停用租户"
        assert (await DBTenant.get(id=tenant.id)).status == TenantStatus.SUSPENDED.value

        resumed = await writes_module.set_tenant_status(tenant.id, "active")
        assert resumed.ok and resumed.action_label == "恢复租户"
        assert (await DBTenant.get(id=tenant.id)).status == TenantStatus.ACTIVE.value

    async def test_invalid_status_is_refused_before_any_write(self, db) -> None:
        tenant = await DBTenant.create(name="acme", status=TenantStatus.ACTIVE.value)
        outcome = await writes_module.set_tenant_status(tenant.id, "deleted")
        assert (outcome.ok, outcome.error_category) == (False, "参数不合法")
        assert (await DBTenant.get(id=tenant.id)).status == TenantStatus.ACTIVE.value

    async def test_missing_tenant(self, db) -> None:
        outcome = await writes_module.set_tenant_status(987654, "active")
        assert (outcome.ok, outcome.error_category) == (False, "目标不存在")

    @pytest.mark.parametrize("tenant_id", [True, False, 0, -1, "abc", "", None])
    async def test_a_malformed_tenant_id_is_refused(self, db, tenant_id: Any) -> None:
        """`int(True)` 是 1：不单独排除布尔值，模型写个 `True` 就会停掉 1 号租户"""
        tenant = await DBTenant.create(name="acme", status=TenantStatus.ACTIVE.value)
        assert tenant.id == 1

        outcome = await writes_module.set_tenant_status(tenant_id, "suspended")

        assert (outcome.ok, outcome.error_category) == (False, "参数不合法")
        assert (await DBTenant.get(id=tenant.id)).status == TenantStatus.ACTIVE.value

    async def test_a_digit_string_tenant_id_is_accepted(self, db) -> None:
        tenant = await DBTenant.create(name="acme", status=TenantStatus.ACTIVE.value)

        outcome = await writes_module.set_tenant_status(str(tenant.id), "suspended")

        assert outcome.ok is True
        assert (await DBTenant.get(id=tenant.id)).status == TenantStatus.SUSPENDED.value


class TestChannelWrites:
    async def test_disable_and_enable(self, db) -> None:
        await seed_channel()
        disabled = await writes_module.set_channel_active(CHAT_KEY, False)
        assert disabled.ok and disabled.action_label == "停用频道"
        assert (await DBChatChannel.get(chat_key=CHAT_KEY)).is_active is False

        enabled = await writes_module.set_channel_active(CHAT_KEY, True)
        assert enabled.ok and enabled.action_label == "启用频道"
        assert (await DBChatChannel.get(chat_key=CHAT_KEY)).is_active is True

    async def test_missing_channel(self, db) -> None:
        outcome = await writes_module.set_channel_active("onebot_v11-x-group_1", True)
        assert (outcome.ok, outcome.error_category) == (False, "目标不存在")

    @pytest.mark.parametrize("flag", ["false", "0", 0, None])
    async def test_a_non_boolean_flag_is_refused_not_coerced(self, db, flag: Any) -> None:
        await seed_channel()
        await writes_module.set_channel_active(CHAT_KEY, False)

        outcome = await writes_module.set_channel_active(CHAT_KEY, flag)

        assert (outcome.ok, outcome.error_category) == (False, "参数不合法")
        assert (await DBChatChannel.get(chat_key=CHAT_KEY)).is_active is False

    @pytest.mark.parametrize("amount", [0, -1, True, "5"])
    async def test_bonus_amount_must_be_a_positive_integer(self, db, amount: Any) -> None:
        await seed_channel()
        outcome = await writes_module.add_quota_bonus(CHAT_KEY, amount)
        assert (outcome.ok, outcome.error_category) == (False, "参数不合法")
        assert await DBChannelQuotaDaily.all().count() == 0

    async def test_bonus_is_written_to_today(self, db) -> None:
        await seed_channel()
        outcome = await writes_module.add_quota_bonus(CHAT_KEY, 7)
        assert outcome.ok is True
        row = await DBChannelQuotaDaily.get(chat_key=CHAT_KEY, day=counter.quota_day())
        assert row.bonus == 7

    async def test_bonus_on_a_missing_channel(self, db) -> None:
        outcome = await writes_module.add_quota_bonus("onebot_v11-x-group_1", 1)
        assert (outcome.ok, outcome.error_category) == (False, "目标不存在")


class TestToolsSwitch:
    async def test_it_writes_the_config_and_saves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: List[Tuple[str, ...]] = []

        def set_value(config_key: str, item_key: str, value: str) -> Tuple[bool, str]:
            calls.append(("set", config_key, item_key, value))
            return True, ""

        def save(config_key: str) -> Tuple[bool, Optional[str]]:
            calls.append(("save", config_key))
            return True, None

        monkeypatch.setattr(writes_module.UnifiedConfigService, "set_config_value", staticmethod(set_value))
        monkeypatch.setattr(writes_module.UnifiedConfigService, "save_config", staticmethod(save))

        outcome = await writes_module.set_tools_enabled(False)

        assert outcome.ok is True
        assert calls == [("set", "system", "OPS_AGENT_TOOLS_ENABLED", "false"), ("save", "system")]

    async def test_a_save_failure_is_an_execution_error_and_the_live_switch_is_rolled_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.core.config import config as core_config

        monkeypatch.setattr(core_config, "OPS_AGENT_TOOLS_ENABLED", True)
        values: List[str] = []

        def set_value(_config_key: str, _item_key: str, value: str) -> Tuple[bool, str]:
            values.append(value)
            return True, ""

        monkeypatch.setattr(writes_module.UnifiedConfigService, "set_config_value", staticmethod(set_value))
        monkeypatch.setattr(writes_module.UnifiedConfigService, "save_config", staticmethod(lambda _key: (False, "磁盘只读")))

        outcome = await writes_module.set_tools_enabled(False)

        assert (outcome.ok, outcome.error_category) == (False, "执行出错")
        assert values == ["false", "true"], "落盘失败要把活开关改回去，回执才不撒谎"

    async def test_a_save_exception_is_an_execution_error_and_the_live_switch_is_rolled_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.core.config import config as core_config

        monkeypatch.setattr(core_config, "OPS_AGENT_TOOLS_ENABLED", True)
        values: List[str] = []

        def set_value(_config_key: str, _item_key: str, value: str) -> Tuple[bool, str]:
            values.append(value)
            return True, ""

        def save(_config_key: str) -> Tuple[bool, Optional[str]]:
            raise OSError("磁盘掉了")

        monkeypatch.setattr(writes_module.UnifiedConfigService, "set_config_value", staticmethod(set_value))
        monkeypatch.setattr(writes_module.UnifiedConfigService, "save_config", staticmethod(save))

        outcome = await writes_module.set_tools_enabled(False)

        assert (outcome.ok, outcome.error_category) == (False, "执行出错")
        assert values == ["false", "true"]

    @pytest.mark.parametrize("enabled", [False, True])
    async def test_rollback_restores_the_value_read_before_the_write(
        self,
        monkeypatch: pytest.MonkeyPatch,
        enabled: bool,
    ) -> None:
        """本来就是这个值时再写一次、落盘失败：回滚后仍是原值，不能由目标值反推出相反的那个"""
        from nekro_agent.core.config import config as core_config

        monkeypatch.setattr(core_config, "OPS_AGENT_TOOLS_ENABLED", enabled)
        values: List[str] = []

        def set_value(_config_key: str, _item_key: str, value: str) -> Tuple[bool, str]:
            values.append(value)
            return True, ""

        monkeypatch.setattr(writes_module.UnifiedConfigService, "set_config_value", staticmethod(set_value))
        monkeypatch.setattr(writes_module.UnifiedConfigService, "save_config", staticmethod(lambda _key: (False, "磁盘只读")))

        outcome = await writes_module.set_tools_enabled(enabled)

        assert outcome.ok is False
        expected = "true" if enabled else "false"
        assert values == [expected, expected]


class TestNaOpsTargetResolution:
    async def test_a_single_instance_word_resolves(self, db) -> None:
        await seed_instance(instance_key="inst9")
        outcome = await writes_module.execute_na_ops_write("disable-instance", "inst9")
        assert outcome.ok is True
        assert (await DBAdapterInstance.get(instance_key="inst9")).enabled is False

    async def test_adapter_qualified_target_resolves(self, db) -> None:
        await seed_instance(adapter_key="adapter_a", instance_key="inst3")
        await seed_instance(adapter_key="adapter_b", instance_key="inst3")
        outcome = await writes_module.execute_na_ops_write("disable-instance", "adapter_a:inst3")
        assert outcome.ok is True
        assert (await DBAdapterInstance.get(adapter_key="adapter_a", instance_key="inst3")).enabled is False
        assert (await DBAdapterInstance.get(adapter_key="adapter_b", instance_key="inst3")).enabled is True

    async def test_an_ambiguous_instance_word_lists_candidates_and_changes_nothing(self, db) -> None:
        await seed_instance(adapter_key="adapter_a", instance_key="inst3")
        await seed_instance(adapter_key="adapter_b", instance_key="inst3")

        outcome = await writes_module.execute_na_ops_write("disable-instance", "inst3")

        assert (outcome.ok, outcome.error_category) == (False, "目标不明确")
        assert set(outcome.candidates) == {'"adapter_a":"inst3"', '"adapter_b":"inst3"'}
        assert (await DBAdapterInstance.filter(instance_key="inst3", enabled=False).count()) == 0

    async def test_tools_on_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: List[bool] = []

        async def fake_set(enabled: bool) -> WriteOutcome:
            seen.append(enabled)
            return WriteOutcome(ok=True, action_label="动作", target_label="目标")

        monkeypatch.setattr(writes_module, "set_tools_enabled", fake_set)

        assert (await writes_module.execute_na_ops_write("tools", "on")).ok is True
        assert (await writes_module.execute_na_ops_write("tools", "off")).ok is True
        assert seen == [True, False]
        assert (await writes_module.execute_na_ops_write("tools", "maybe")).error_category == "参数不合法"


class TestWriteTools:
    @pytest.mark.parametrize(
        ("func", "kwargs", "attr", "expected_call"),
        [
            (ops_set_instance_enabled, {"adapter_key": ADAPTER, "instance_key": INSTANCE, "enabled": False}, "set_instance_enabled", (ADAPTER, INSTANCE, False)),
            (ops_restart_protocol, {"adapter_key": ADAPTER, "instance_key": INSTANCE}, "restart_protocol", (ADAPTER, INSTANCE)),
            (ops_set_tenant_status, {"tenant_id": 7, "status": "suspended"}, "set_tenant_status", (7, "suspended")),
            (ops_set_channel_active, {"chat_key": CHAT_KEY, "active": False}, "set_channel_active", (CHAT_KEY, False)),
            (ops_quota_bonus, {"chat_key": CHAT_KEY, "amount": 3}, "add_quota_bonus", (CHAT_KEY, 3)),
            (ops_tools_off, {}, "set_tools_enabled", (False,)),
        ],
    )
    async def test_each_tool_calls_the_shared_layer_and_sends_a_receipt(
        self,
        func: Any,
        kwargs: Dict[str, Any],
        attr: str,
        expected_call: Tuple[Any, ...],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        install_grant()
        calls: List[Tuple[Any, ...]] = []
        receipts: List[Tuple[str, str]] = []

        async def fake_write(*args: Any, **_kwargs: Any) -> WriteOutcome:
            calls.append(tuple(args))
            return WriteOutcome(ok=True, action_label="动作", target_label="目标")

        async def fake_send(chat_key: str, text: str) -> bool:
            receipts.append((chat_key, text))
            return True

        monkeypatch.setattr(tools_module.writes, attr, fake_write)
        monkeypatch.setattr(tools_module, "send_receipt", fake_send)

        reply = await func(ctx(), **kwargs)

        assert calls == [expected_call]
        assert receipts == [(CHAT_KEY, '【运维】已执行：动作 目标。操作人 "10001"。')]
        assert reply == '已执行：动作 目标。操作人 "10001"。'

    async def test_an_exception_text_never_reaches_receipt_or_reply(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        await seed_instance()
        install_grant()
        receipts: List[str] = []

        async def boom(_instance: Any, _enabled: bool) -> Any:
            raise RuntimeError(f"上游返回了密钥 {SECRET}")

        async def fake_send(_chat_key: str, text: str) -> bool:
            receipts.append(text)
            return True

        monkeypatch.setattr(instance_manager, "set_instance_enabled", boom)
        monkeypatch.setattr(tools_module, "send_receipt", fake_send)

        reply = await ops_set_instance_enabled(ctx(), ADAPTER, INSTANCE, False)

        assert "执行出错" in reply and SECRET not in reply
        assert receipts and "执行出错" in receipts[0] and SECRET not in receipts[0]

    async def test_ambiguous_reply_asks_which_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_grant()

        async def fake_write(*_args: Any) -> WriteOutcome:
            return WriteOutcome(
                ok=False,
                action_label="停用实例",
                target_label="inst3",
                error_category="目标不明确",
                candidates=('"a":"inst3"', '"b":"inst3"'),
            )

        async def fake_send(_chat_key: str, _text: str) -> bool:
            return True

        monkeypatch.setattr(tools_module.writes, "set_instance_enabled", fake_write)
        monkeypatch.setattr(tools_module, "send_receipt", fake_send)

        reply = await ops_set_instance_enabled(ctx(), ADAPTER, INSTANCE, False)
        assert "候选：" in reply
        assert "请向运维人员确认是哪一个" in reply

    async def test_without_a_grant_nothing_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: List[Any] = []

        async def fake_write(*args: Any, **_kwargs: Any) -> WriteOutcome:
            calls.append(args)
            return WriteOutcome(ok=True, action_label="动作", target_label="目标")

        monkeypatch.setattr(tools_module.writes, "set_instance_enabled", fake_write)
        grant_module.close_round(CONTAINER)

        assert await ops_set_instance_enabled(ctx(), ADAPTER, INSTANCE, False) == tools_module.UNAVAILABLE
        assert calls == []


def test_config_model_ignores_the_retired_deeplink_keys() -> None:
    """旧 YAML 里残留这两个键不能让启动失败：配置模型按默认规则忽略未知字段"""
    loaded = CoreConfig(
        OPS_BOT_DEEPLINK_TTL_SECONDS=300,
        OPS_BOT_CONSOLE_BASE_URL="https://console.example.com",
    )
    dumped = loaded.model_dump()
    assert "OPS_BOT_DEEPLINK_TTL_SECONDS" not in dumped
    assert "OPS_BOT_CONSOLE_BASE_URL" not in dumped
