"""接线：消息入口记触发、定时任务记触发、回合开合授权、RPC 分派与复核、审计留痕

## 这些用例钉的都是"接口接上了没有"

运维工具是七个新文件加九处接线，功能本身在别处测。这里判的是那些**只在接错时才失效**
的地方：触发记的是哪一种、开关关着时有没有多算一次可信、授权在回合开始与结束时有没有
真的开合、RPC 分派找不找得到运维方法、执行前有没有复核、拒绝与执行有没有留痕。

每一格失效的后果都很具体：触发种类记错等于把定时提醒当成人说的话去开权限；执行前不复核
等于"开回合时开着，这一轮就永远开着"；少了 `mark_tainted` 等于读过外部内容之后照样能写。
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.schemas.chat_message import ChatMessage, ChatType
from nekro_agent.services.ops_agent import audit as audit_module
from nekro_agent.services.ops_agent import round_grant as grant_module
from nekro_agent.services.ops_agent import tools as tools_module
from nekro_agent.services.ops_agent.round_grant import get as get_ops_grant
from nekro_agent.services.ops_agent.round_grant import open_round, reset_round_grants
from nekro_agent.services.ops_agent.triggers import (
    TRIGGER_KIND_HUMAN,
    TRIGGER_KIND_OTHER,
    TRIGGER_KIND_SYSTEM_PUSH,
    TRIGGER_KIND_TIMER,
    TRIGGER_KIND_UNTRUSTED_PUSH,
    round_trigger_book,
)
from nekro_agent.services.sandbox.runner import build_container_key
from nekro_agent.tools.untrusted_content import UntrustedToolResult

ADAPTER = "onebot_v11"
INSTANCE = "opsinst"
CHANNEL_ID = "group_10001"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-{CHANNEL_ID}"
OPERATOR = "10001"
OUTSIDER = "30003"
CONTAINER = build_container_key(CHAT_KEY)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            enabled=True,
            provider="remote_onebot",
            provider_account_id="10000012",
        )
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id=CHANNEL_ID,
            chat_key=CHAT_KEY,
            channel_type=ChatType.GROUP.value,
            channel_name="",
            data="{}",
        )
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def clean_triggers():
    reset_round_grants()
    yield
    reset_round_grants()


@pytest.fixture
def ops_on(monkeypatch: pytest.MonkeyPatch):
    def apply(**overrides: Any) -> None:
        settings: dict[str, Any] = {
            "OPS_AGENT_TOOLS_ENABLED": True,
            "OPS_AGENT_OPERATORS": [OPERATOR],
            "OPS_AGENT_GROUPS": [CHAT_KEY],
            "OPS_AGENT_PRIVATE_ENABLED": True,
        }
        settings.update(overrides)
        for name, value in settings.items():
            monkeypatch.setattr(config, name, value)

    apply()
    return apply


def human_message(sender_id: str = OPERATOR, *, tome: int = 1) -> ChatMessage:
    return ChatMessage(
        message_id="m1",
        sender_id=sender_id,
        sender_name="tester",
        sender_nickname="tester",
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        platform_userid=sender_id,
        is_tome=tome,
        is_recalled=False,
        chat_key=CHAT_KEY,
        chat_type=ChatType.GROUP,
        content_text="在吗",
        content_data=[],
        raw_cq_code="",
        ext_data={},
        send_timestamp=0,
    )


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """包住真的 `schedule_agent_task`，记下每次带进来的触发种类；防抖不跑

    包住而不是替换：触发是不是真的记进了本子，也是这里要断言的事。
    """
    from nekro_agent.services import message_service as module

    calls: List[Any] = []
    original = module.message_service.schedule_agent_task

    async def schedule(chat_key: Optional[str] = None, message: Any = None, ctx: Any = None, **kwargs: Any) -> Any:
        calls.append(
            SimpleNamespace(
                chat_key=chat_key or getattr(message, "chat_key", ""),
                trigger=kwargs.get("trigger"),
                bypass_channel_quota=kwargs.get("bypass_channel_quota", False),
            ),
        )
        return await original(chat_key=chat_key, message=message, ctx=ctx, **kwargs)

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(module.message_service, "schedule_agent_task", schedule)
    monkeypatch.setattr(type(module.message_service), "_debounce_task", noop)
    for key in (CHAT_KEY,):
        module.message_service.pending_messages.pop(key, None)
        module.message_service.debounce_timers.pop(key, None)
    yield calls
    for key in (CHAT_KEY,):
        module.message_service.pending_messages.pop(key, None)
        module.message_service.debounce_timers.pop(key, None)


@pytest.fixture
def quiet_effective_config(monkeypatch: pytest.MonkeyPatch):
    """关键词触发永远命中、随机搭话关掉：把"会不会调度"完全交给被测的那一条判据"""
    from nekro_agent.core.core_utils import ConfigManager

    merged = config.model_copy(
        update={
            "AI_CHAT_TRIGGER_REGEX": [".*"],
            "AI_CHAT_RANDOM_REPLY_PROBABILITY": 0.0,
            "AFFECTION_ENABLE_SYSTEM": False,
            "AI_CHAT_DAILY_REPLY_LIMIT": 0,
            "AI_CHAT_ENABLE_HOURLY_LIMIT": False,
        },
    )

    async def fake(self: DBChatChannel) -> Any:
        return merged

    monkeypatch.setattr(DBChatChannel, "get_effective_config", fake)
    monkeypatch.setattr(ConfigManager, "get_all_config_keys", lambda: ["system"])
    return merged


class TestHumanTrigger:
    async def test_an_operator_message_is_recorded_as_a_trusted_human_trigger(
        self, db, ops_on, scheduled, quiet_effective_config, monkeypatch: pytest.MonkeyPatch,  # noqa: ARG002
    ) -> None:
        from nekro_agent.services import message_service as module

        async def trusted(*_args: Any, **_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(module, "is_trusted_super_user_source", trusted)

        await module.message_service.push_human_message(message=human_message())

        assert scheduled, "这条消息本该触发一轮"
        trigger = scheduled[-1].trigger
        assert trigger.kind == TRIGGER_KIND_HUMAN
        assert trigger.sender_id == OPERATOR
        assert trigger.operator_trusted is True
        assert (trigger.adapter_key, trigger.instance_key) == (ADAPTER, INSTANCE)

    async def test_an_outsider_is_recorded_but_not_trusted(
        self, db, ops_on, scheduled, quiet_effective_config, monkeypatch: pytest.MonkeyPatch,  # noqa: ARG002
    ) -> None:
        """名单外的人不去算来源可信：那一步要读握手记录，白算一次也是白查一次库

        来源判定现在由 `is_trusted_super_user_source(adapter_key, instance_key)` 承担，
        它只看实例级握手记录；名单外的人根本不该走到这里。
        """
        from nekro_agent.services import message_service as module

        seen: List[Any] = []

        async def spy(adapter_key: str, instance_key: str) -> bool:
            seen.append((adapter_key, instance_key))
            return False

        monkeypatch.setattr(module, "is_trusted_super_user_source", spy)

        await module.message_service.push_human_message(message=human_message(OUTSIDER))

        trigger = scheduled[-1].trigger
        assert trigger.kind == TRIGGER_KIND_HUMAN
        assert trigger.sender_id == OUTSIDER
        assert trigger.operator_trusted is False
        assert seen == [], "名单外的人不该触发来源查询"

    async def test_the_switch_being_off_never_evaluates_trust(
        self, db, scheduled, quiet_effective_config, monkeypatch: pytest.MonkeyPatch,  # noqa: ARG002
    ) -> None:
        """存量部署（开关默认关）不得因此多查一次库、多算一次来源"""
        from nekro_agent.services import message_service as module

        monkeypatch.setattr(config, "OPS_AGENT_TOOLS_ENABLED", False)
        monkeypatch.setattr(config, "OPS_AGENT_OPERATORS", [OPERATOR])
        monkeypatch.setattr(config, "AI_CHAT_QUOTA_WHITELIST_USERS", ["quota-only"])

        seen: List[Any] = []

        async def spy(adapter_key: str, instance_key: str) -> bool:
            seen.append((adapter_key, instance_key))
            return False

        monkeypatch.setattr(module, "is_trusted_super_user_source", spy)

        await module.message_service.push_human_message(message=human_message())

        trigger = scheduled[-1].trigger
        assert trigger.kind == TRIGGER_KIND_HUMAN
        assert trigger.operator_trusted is False
        assert seen == [], "开关关着时不得去算来源可信"

    async def test_the_switch_being_off_still_records_a_human_kind(
        self, db, scheduled, quiet_effective_config, monkeypatch: pytest.MonkeyPatch,  # noqa: ARG002
    ) -> None:
        """种类照记：它只在开关打开后才被判定读到，但记错会让排查时看不出这一轮是谁触发的"""
        from nekro_agent.services import message_service as module

        monkeypatch.setattr(config, "OPS_AGENT_TOOLS_ENABLED", False)
        await module.message_service.push_human_message(message=human_message())

        assert scheduled[-1].trigger.kind == TRIGGER_KIND_HUMAN
        assert scheduled[-1].trigger.sender_id == OPERATOR

    async def test_an_untrusted_source_is_not_trusted_even_if_listed(
        self, db, ops_on, scheduled, quiet_effective_config, monkeypatch: pytest.MonkeyPatch,  # noqa: ARG002
    ) -> None:
        from nekro_agent.services import message_service as module

        async def untrusted(*_args: Any, **_kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(module, "is_trusted_super_user_source", untrusted)
        await module.message_service.push_human_message(message=human_message())

        assert scheduled[-1].trigger.operator_trusted is False

    async def test_the_trigger_reaches_the_book(self, db, ops_on, scheduled, quiet_effective_config, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """`schedule_agent_task` 自己把触发记进本子：这是回合开始时唯一能读到它的地方"""
        from nekro_agent.services import message_service as module

        async def trusted(*_args: Any, **_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(module, "is_trusted_super_user_source", trusted)
        await module.message_service.push_human_message(message=human_message())

        seen = round_trigger_book.peek(CHAT_KEY).triggers
        assert [item.kind for item in seen] == [TRIGGER_KIND_HUMAN]
        assert seen[0].operator_trusted is True


class TestSchedulerRecordsTheKind:
    """`schedule_agent_task` 是触发记账的唯一入口：没传种类时记 `other`，绝不冒充人类"""

    @pytest.fixture(autouse=True)
    def quiet_debounce(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services import message_service as module

        async def noop(*_args: Any, **_kwargs: Any) -> None:
            return

        monkeypatch.setattr(type(module.message_service), "_debounce_task", noop)
        module.message_service.pending_messages.pop(CHAT_KEY, None)
        module.message_service.debounce_timers.pop(CHAT_KEY, None)
        yield
        module.message_service.pending_messages.pop(CHAT_KEY, None)
        module.message_service.debounce_timers.pop(CHAT_KEY, None)

    async def test_no_trigger_means_other(self, db, ops_on) -> None:  # noqa: ARG002
        from nekro_agent.services import message_service as module

        await module.message_service.schedule_agent_task(chat_key=CHAT_KEY)
        assert [item.kind for item in round_trigger_book.peek(CHAT_KEY).triggers] == [TRIGGER_KIND_OTHER]

    async def test_the_given_trigger_is_recorded(self, db, ops_on) -> None:  # noqa: ARG002
        from nekro_agent.services import message_service as module
        from nekro_agent.services.ops_agent.triggers import RoundTrigger

        await module.message_service.schedule_agent_task(chat_key=CHAT_KEY, trigger=RoundTrigger(kind=TRIGGER_KIND_TIMER))
        assert [item.kind for item in round_trigger_book.peek(CHAT_KEY).triggers] == [TRIGGER_KIND_TIMER]

    async def test_a_blocked_schedule_is_not_recorded(self, db, ops_on) -> None:  # noqa: ARG002
        """被实例闸门挡下的调度不会跑回合；记下它只会把一批可信触发降级成不可用"""
        from nekro_agent.services import message_service as module

        await DBAdapterInstance.filter(instance_key=INSTANCE).update(enabled=False, status="disabled")
        await module.message_service.schedule_agent_task(chat_key=CHAT_KEY)
        assert round_trigger_book.peek(CHAT_KEY).triggers == []

    async def test_nothing_is_recorded_while_the_switch_is_off(self, db) -> None:  # noqa: ARG002
        """关着时记了也没人取，只会放到过期，开关一打开就把每个频道的第一轮拒掉"""
        from nekro_agent.services import message_service as module
        from nekro_agent.services.ops_agent.triggers import RoundTrigger

        assert config.OPS_AGENT_TOOLS_ENABLED is False
        await module.message_service.schedule_agent_task(chat_key=CHAT_KEY, trigger=RoundTrigger(kind=TRIGGER_KIND_TIMER))
        assert round_trigger_book.peek(CHAT_KEY).triggers == []


class TestPushedTriggers:
    async def test_a_system_push_records_its_own_kind(self, db, scheduled) -> None:
        from nekro_agent.services import message_service as module

        await module.message_service.push_system_message(chat_key=CHAT_KEY, agent_messages="定时任务到点了", trigger_agent=True)
        assert scheduled[-1].trigger.kind == TRIGGER_KIND_SYSTEM_PUSH
        assert scheduled[-1].trigger.operator_trusted is False
        assert scheduled[-1].trigger.sender_id == ""

    async def test_an_untrusted_push_records_its_own_kind(self, db, scheduled) -> None:
        from nekro_agent.services import message_service as module

        await module.message_service.push_untrusted_message(
            chat_key=CHAT_KEY,
            result=UntrustedToolResult(kind="Probe", body="外部正文", summary="宿主写的引导语"),
            trigger_agent=True,
        )
        assert scheduled[-1].trigger.kind == TRIGGER_KIND_UNTRUSTED_PUSH

    async def test_the_two_pushes_are_distinguishable(self, db, scheduled) -> None:
        """两者都不可用；审计与排查时要分得清是系统消息还是外部内容推来的"""
        from nekro_agent.services import message_service as module

        await module.message_service.push_system_message(chat_key=CHAT_KEY, agent_messages="系统消息", trigger_agent=True)
        await module.message_service.push_untrusted_message(
            chat_key=CHAT_KEY,
            result=UntrustedToolResult(kind="Probe", body="外部正文", summary="摘要"),
            trigger_agent=True,
        )
        assert [call.trigger.kind for call in scheduled] == [TRIGGER_KIND_SYSTEM_PUSH, TRIGGER_KIND_UNTRUSTED_PUSH]

    async def test_a_blocked_push_records_nothing(self, db, scheduled, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.schemas.signal import MsgSignal
        from nekro_agent.services import message_service as module
        from nekro_agent.services.plugin.collector import plugin_collector

        async def block(_ctx: Any, _text: str) -> Any:
            return MsgSignal.BLOCK_ALL

        monkeypatch.setattr(plugin_collector, "handle_on_system_message", block)
        await module.message_service.push_system_message(chat_key=CHAT_KEY, agent_messages="系统消息", trigger_agent=True)
        assert scheduled == []


class TestTimerTriggers:
    """定时任务的两种触发都记成 `timer`：它们既不是人类说的，也不是外部内容推来的"""

    @pytest.fixture
    def timer_module(self):
        import sys

        return sys.modules["nekro_agent.services.timer.timer_service"]

    @pytest.fixture(autouse=True)
    def _timer_plugin_on(self, timer_module, monkeypatch: pytest.MonkeyPatch):
        """这些用例只判触发记账；把「timer 插件在该频道生效」替身成恒真"""
        async def _effective(*_args: Any, **_kwargs: Any) -> bool:
            return True

        monkeypatch.setattr(timer_module, "plugin_effective_in_chat", _effective)

    async def test_an_immediate_timer_records_the_kind(self, db, timer_module, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        calls: List[Any] = []

        async def schedule(chat_key: Optional[str] = None, message: Any = None, ctx: Any = None, **kwargs: Any) -> None:
            calls.append(kwargs.get("trigger"))

        monkeypatch.setattr(timer_module.message_service, "schedule_agent_task", schedule)
        await timer_module.timer_service.set_timer(CHAT_KEY, trigger_time=0, event_desc="马上提醒")
        assert calls and calls[-1].kind == TRIGGER_KIND_TIMER

    async def test_a_deferred_timer_records_the_kind(self, db, timer_module, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        calls: List[Any] = []

        async def schedule(chat_key: Optional[str] = None, message: Any = None, ctx: Any = None, **kwargs: Any) -> None:
            calls.append(kwargs.get("trigger"))

        monkeypatch.setattr(timer_module.message_service, "schedule_agent_task", schedule)
        # 无事件描述的定时任务是"到点叫一声"，它走调度而不是推送
        await timer_module.timer_service._execute_task(timer_module.TimerTask(CHAT_KEY, 0, ""))  # noqa: SLF001

        assert calls and calls[-1].kind == TRIGGER_KIND_TIMER

    async def test_a_reminder_is_an_untrusted_push_not_a_timer_trigger(
        self, db, timer_module, monkeypatch: pytest.MonkeyPatch,  # noqa: ARG002
    ) -> None:
        """带描述的提醒是**外部内容**：描述是当初设置时写下的字符串，可能整段是注入串。

        所以它记成 `untrusted_push` 而不是 `timer`——虽然它确实由定时器发起，但它不是
        "宿主叫了一声"，而是一段没人审过的正文到了回合里。
        """
        calls: List[Any] = []

        async def push(chat_key: str, result: Any, trigger_agent: bool = False, **_kwargs: Any) -> None:
            calls.append((chat_key, result, trigger_agent))

        recorded: List[Any] = []

        async def schedule(chat_key: Optional[str] = None, message: Any = None, ctx: Any = None, **kwargs: Any) -> None:
            recorded.append(kwargs.get("trigger"))

        monkeypatch.setattr(timer_module.message_service, "push_untrusted_message", push)
        monkeypatch.setattr(timer_module.message_service, "schedule_agent_task", schedule)
        await timer_module.timer_service._execute_task(timer_module.TimerTask(CHAT_KEY, 0, "到点提醒"))  # noqa: SLF001

        assert calls and calls[-1][1].kind == "TimerReminder"
        assert recorded == [], "提醒走推送，不该再额外调度一次"


class TestRoundOpenAndClose:
    """`run_agent` 在回合开始时签发、结束时撤销

    这一条用源码结构断言：把整条 Agent 回合跑起来要模型、沙盒与容器，那不是这里要测的东西；
    要保证的是「这两件事发生在它该在的位置」——签发在登记本轮令牌之后（授权按容器键存），
    撤销在 `finally` 里（任何失败路径都要撤销）。
    """

    def test_the_round_is_opened_after_the_session_token_is_registered(self) -> None:
        import inspect

        from nekro_agent.services.agent import run_agent

        source = inspect.getsource(run_agent._run_agent_in_instance_scope)  # noqa: SLF001
        assert source.index("register_session_token(") < source.index("await open_ops_round(")
        assert "await open_ops_round(db_chat_channel)" in source

    def test_the_prompt_section_is_read_once_and_passed_to_the_compiler(self) -> None:
        import inspect

        from nekro_agent.services.agent import round_prompt

        # 组装已整体搬进 `round_prompt`：运维说明的读取与传入编译器都在那个函数里，
        # 断言本身不变——只读一次，且同一个变量传给了编译器。
        source = inspect.getsource(round_prompt._build_round_prompt)  # noqa: SLF001
        assert source.count("render_ops_prompt_section(") == 1
        assert "ops_prompt=ops_prompt" in source

    def test_the_round_is_closed_in_the_finally_block(self) -> None:
        import inspect
        import re

        from nekro_agent.services.agent import run_agent

        source = inspect.getsource(run_agent._run_agent_with_affection_suppression)  # noqa: SLF001
        finally_at = source.index("finally:")
        assert "close_ops_round(" in source[finally_at:]
        assert re.search(r"close_ops_round\(build_container_key\(chat_key\)\)", source)

    def test_a_failure_to_open_does_not_break_the_round(self) -> None:
        """查库抖动不该让正常对话哑掉：那一处必须被 `try` 包住并且只记 WARNING"""
        import inspect

        from nekro_agent.services.agent import run_agent

        source = inspect.getsource(run_agent._run_agent_in_instance_scope)  # noqa: SLF001
        opening = source.index("await open_ops_round(")
        assert "try:" in source[:opening][-200:]
        assert "except Exception" in source[opening : opening + 400]

    async def test_opening_then_closing_leaves_no_grant(self, db, ops_on) -> None:  # noqa: ARG002
        """端到端对一遍两个函数的契约：`open_round` 用的是频道行，`close_round` 用的是容器键"""
        from nekro_agent.services.ops_agent.triggers import RoundTrigger

        round_trigger_book.record(
            CHAT_KEY,
            RoundTrigger(kind=TRIGGER_KIND_HUMAN, adapter_key=ADAPTER, instance_key=INSTANCE, sender_id=OPERATOR, operator_trusted=True),
        )
        channel = await DBChatChannel.get_channel(CHAT_KEY)
        assert await open_round(channel) is not None
        assert get_ops_grant(CONTAINER) is not None

        grant_module.close_round(build_container_key(CHAT_KEY))
        assert get_ops_grant(CONTAINER) is None


class TestRpcDispatch:
    """`rpc_exec` 是模型代码唯一能触达服务端的通道"""

    @staticmethod
    async def _grant(method_names: List[str], *, chat_key: str = CHAT_KEY) -> str:
        from nekro_agent.services.sandbox.credential_guard import issue_sandbox_call_grant

        return issue_sandbox_call_grant(container_key=CONTAINER, from_chat_key=chat_key, method_names=method_names)

    @staticmethod
    def _release() -> None:
        from nekro_agent.services.sandbox.credential_guard import revoke_sandbox_call_grant

        revoke_sandbox_call_grant(CONTAINER)

    async def _open_round(self) -> None:
        from nekro_agent.services.ops_agent.triggers import RoundTrigger

        round_trigger_book.record(
            CHAT_KEY,
            RoundTrigger(kind=TRIGGER_KIND_HUMAN, adapter_key=ADAPTER, instance_key=INSTANCE, sender_id=OPERATOR, operator_trusted=True),
        )
        channel = await DBChatChannel.get_channel(CHAT_KEY)
        assert await open_round(channel) is not None

    @pytest.fixture
    def audit_lines(self, monkeypatch: pytest.MonkeyPatch) -> List[str]:
        lines: List[str] = []

        class _Recorder:
            def info(self, message: str, *_args: Any, **_kwargs: Any) -> None:
                lines.append(message)

            def __getattr__(self, _name: str) -> Any:
                return lambda *_a, **_k: None

        monkeypatch.setattr(audit_module, "logger", _Recorder())
        return lines

    @staticmethod
    def _replace(name: str, func: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """把某个运维方法换成替身

        替身要挂在 `provider.OPS_METHODS` 上：那是 `rpc_exec` 分派时读的表。挂在
        `tools.OPS_TOOL_FUNCS` 上只对"直接调用工具函数"的路径有效，不会影响分派。
        """
        from nekro_agent.services.ops_agent import provider as ops_provider
        from nekro_agent.services.plugin.schema import SandboxMethod, SandboxMethodType

        func._method_type = SandboxMethodType.AGENT  # noqa: SLF001
        original = ops_provider.OPS_METHODS[name]
        monkeypatch.setitem(
            ops_provider.OPS_METHODS,
            name,
            (SandboxMethod(method_type=SandboxMethodType.AGENT, name=name, description=original[0].description, func=func), False),
        )

    async def test_a_granted_call_is_dispatched(self, db, ops_on: None) -> None:  # noqa: ARG002
        from nekro_agent.routers.rpc import rpc_exec
        from nekro_agent.tools.safe_pickle import safe_loads
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        await self._open_round()
        token = await self._grant(["ops_list_tenants"])
        try:
            response = await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        assert response.headers["Run-Error"] == "False"
        assert "单租户" in safe_loads(response.body) or "租户列表" in safe_loads(response.body)

    async def test_the_method_type_is_read_for_ops_methods(self, db, ops_on: None) -> None:  # noqa: ARG002
        """`rpc_exec` 对每个方法都要读 `_method_type`；漏设就是一被调用就 AttributeError"""
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        await self._open_round()
        token = await self._grant(["ops_list_tenants"])
        try:
            response = await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        # AGENT：沙盒收到这个头就停下，返回值随下一次迭代回到模型眼前
        assert response.headers["Method-Type"] == "agent"

    async def test_a_method_outside_the_whitelist_is_rejected_before_dispatch(self, db, ops_on: None) -> None:  # noqa: ARG002
        from nekro_agent.routers.rpc import rpc_exec
        from nekro_agent.schemas.errors import UnauthorizedError
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        await self._open_round()
        token = await self._grant([])
        try:
            with pytest.raises(UnauthorizedError):
                await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

    async def test_a_round_that_lost_its_grant_is_refused_before_execution(self, db, ops_on: None, audit_lines: List[str]) -> None:  # noqa: ARG002
        """复核发生在执行前：沙盒许可还在，但这一轮的运维授权没了"""
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        await self._open_round()
        token = await self._grant(["ops_list_tenants"])
        grant_module.close_round(CONTAINER)
        try:
            response = await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        assert response.headers["Run-Error"] == "True"
        assert response.headers["Rpc-Rejected"] == "True"
        assert "运维工具调用被拒绝" in response.body.decode()
        assert "no_grant" in response.body.decode()
        assert any("outcome=refused" in line and "tool=ops_list_tenants" in line for line in audit_lines)

    async def test_the_switch_being_turned_off_mid_round_is_refused(self, db, ops_on, audit_lines: List[str]) -> None:  # noqa: ARG002
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        await self._open_round()
        token = await self._grant(["ops_list_tenants"])
        ops_on(OPS_AGENT_TOOLS_ENABLED=False)
        try:
            response = await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        assert "disabled" in response.body.decode()
        assert any("outcome=refused" in line for line in audit_lines)

    async def test_the_operator_being_removed_mid_round_is_refused(self, db, ops_on, audit_lines: List[str]) -> None:  # noqa: ARG002
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        await self._open_round()
        token = await self._grant(["ops_list_tenants"])
        ops_on(OPS_AGENT_OPERATORS=[OUTSIDER])
        try:
            response = await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        assert "operator_removed" in response.body.decode()

    async def test_a_mismatched_chat_key_is_refused(self, db, ops_on: None) -> None:  # noqa: ARG002
        """沙盒许可绑定的是这个容器与这个会话；两者必须一起对上"""
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        await self._open_round()
        token = await self._grant(["ops_list_tenants"])
        try:
            response = await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
            assert response.headers["Run-Error"] == "False"
        finally:
            self._release()

        other_chat_key = f"{ADAPTER}-{INSTANCE}-group_20002"
        token = await self._grant(["ops_list_tenants"], chat_key=other_chat_key)
        try:
            response = await rpc_exec(CONTAINER, other_chat_key, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        assert "chat_key_mismatch" in response.body.decode()

    async def test_an_executed_call_is_audited(self, db, ops_on: None, audit_lines: List[str]) -> None:  # noqa: ARG002
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        await self._open_round()
        token = await self._grant(["ops_list_tenants"])
        try:
            await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        assert any("outcome=executed" in line and f"operators={OPERATOR}" in line for line in audit_lines)
        assert any(f"chat_key={CHAT_KEY}" in line for line in audit_lines)
        assert any(f"instance={ADAPTER}/{INSTANCE}" in line for line in audit_lines)

    async def test_a_failing_call_is_audited_as_failed(self, db, ops_on: None, audit_lines: List[str], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        from nekro_agent.models.db_ops_agent_audit import DBOpsAgentAudit
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        async def boom(_ctx: Any) -> str:
            raise ValueError("查询炸了")

        self._replace("ops_list_tenants", boom, monkeypatch)
        await self._open_round()
        token = await self._grant(["ops_list_tenants"])
        try:
            response = await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        assert response.headers["Run-Error"] == "True"
        assert any("outcome=failed" in line for line in audit_lines)
        row = await DBOpsAgentAudit.get(tool="ops_list_tenants", outcome="failed")
        assert row.error_type == "ValueError"
        assert "查询炸了" not in (row.refuse_reason + row.result_summary + row.error_type)

    async def test_the_arguments_are_audited_and_redacted(self, db, ops_on: None, audit_lines: List[str], monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        async def echo_name(_ctx: Any, keyword: str = "") -> str:
            return f"ok {keyword}"

        self._replace("ops_find", echo_name, monkeypatch)
        await self._open_round()
        token = await self._grant(["ops_find"])
        try:
            await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_find", "api_key=sk-live-01234abcd")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        line = next(item for item in audit_lines if "outcome=executed" in item)
        assert "arg1=" in line
        assert "sk-live-01234abcd" not in line
        assert "[redacted_credential]" in line

    async def test_reading_external_content_taints_the_round(self, db, ops_on: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        """读过日志这一轮就不能再写：错误原文里可能藏着"照我说的做\""""
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        async def records(**_kwargs: Any) -> List[Dict[str, Any]]:
            return [{"level": "ERROR", "message": "上游报错", "timestamp": "t", "source": "probe"}]

        monkeypatch.setattr(tools_module, "get_log_records", records)
        await self._open_round()
        token = await self._grant(["ops_recent_errors"])
        try:
            response = await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_recent_errors")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        assert response.headers["Run-Error"] == "False"
        grant = get_ops_grant(CONTAINER)
        assert grant is not None and grant.tainted is True

    async def test_a_plain_string_result_does_not_taint_the_round(self, db, ops_on: None) -> None:  # noqa: ARG002
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        await self._open_round()
        token = await self._grant(["ops_list_tenants"])
        try:
            await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_list_tenants")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        grant = get_ops_grant(CONTAINER)
        assert grant is not None and grant.tainted is False

    async def test_the_untrusted_result_is_pushed_as_a_tool_result(self, db, ops_on: None, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        async def records(**_kwargs: Any) -> List[Dict[str, Any]]:
            return [{"level": "ERROR", "message": "上游报错", "timestamp": "t", "source": "probe"}]

        monkeypatch.setattr(tools_module, "get_log_records", records)
        await self._open_round()
        token = await self._grant(["ops_recent_errors"])
        try:
            await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_recent_errors")), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

        rows = await DBChatMessage.filter(chat_key=CHAT_KEY).order_by("id")
        assert rows, "不可信结果应当落成一条消息行"
        content = json.loads(rows[-1].content_data)
        assert content[0]["type"] == "untrusted"
        assert content[0]["kind"] == "OpsErrors"

    async def test_a_write_after_reading_external_content_is_refused(
        self,
        db,
        ops_on: None,  # noqa: ARG002
        audit_lines: List[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """端到端：先读日志（不可信结果），再调写工具 → 拒绝、业务函数没被调用、审计记 refused"""
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        async def records(**_kwargs: Any) -> List[Dict[str, Any]]:
            return [{"level": "ERROR", "message": "上游报错", "timestamp": "t", "source": "probe"}]

        calls: List[Any] = []

        async def fake_write(*args: Any, **_kwargs: Any) -> Any:
            calls.append(args)
            return tools_module.writes.WriteOutcome(ok=True, action_label="动作", target_label="目标")

        monkeypatch.setattr(tools_module, "get_log_records", records)
        monkeypatch.setattr(tools_module.writes, "set_channel_active", fake_write)
        await self._open_round()
        token = await self._grant(["ops_recent_errors", "ops_set_channel_active"])
        try:
            first = await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body("ops_recent_errors")), x_rpc_token=token)  # type: ignore[arg-type]
            second = await rpc_exec(  # type: ignore[arg-type]
                CONTAINER,
                CHAT_KEY,
                FakeRpcRequest(request_body("ops_set_channel_active", CHAT_KEY, False)),
                x_rpc_token=token,
            )
        finally:
            self._release()

        assert first.headers["Run-Error"] == "False"
        assert second.headers["Run-Error"] == "True"
        assert "已经读过外部内容" in second.body.decode()
        assert calls == []
        assert any("outcome=refused" in line and "reason=tainted" in line for line in audit_lines)

    async def test_a_write_without_external_content_executes_and_audits_its_summary(
        self,
        db,
        ops_on: None,  # noqa: ARG002
        audit_lines: List[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.routers.rpc import rpc_exec
        from nekro_agent.tools.safe_pickle import safe_loads
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        calls: List[Any] = []

        async def fake_write(*args: Any, **_kwargs: Any) -> Any:
            calls.append(args)
            return tools_module.writes.WriteOutcome(ok=True, action_label="停用频道", target_label="测试群")

        async def fake_send(_chat_key: str, _text: str) -> bool:
            return True

        monkeypatch.setattr(tools_module.writes, "set_channel_active", fake_write)
        monkeypatch.setattr(tools_module, "send_receipt", fake_send)
        await self._open_round()
        token = await self._grant(["ops_set_channel_active"])
        try:
            response = await rpc_exec(  # type: ignore[arg-type]
                CONTAINER,
                CHAT_KEY,
                FakeRpcRequest(request_body("ops_set_channel_active", CHAT_KEY, False)),
                x_rpc_token=token,
            )
        finally:
            self._release()

        assert response.headers["Run-Error"] == "False"
        assert "已执行" in safe_loads(response.body)
        assert calls == [(CHAT_KEY, False)]
        assert any("outcome=executed" in line and "summary=" in line and "停用频道" in line for line in audit_lines)


class TestAuditModule:
    @pytest.mark.parametrize(
        ("reason", "hint"),
        [
            ("tainted", "已经读过外部内容"),
            ("round_limit", "本轮写操作次数已达上限"),
            ("hourly_limit", "本小时写操作次数已达上限"),
        ],
    )
    def test_write_denials_are_explained_in_human_words(self, reason: str, hint: str) -> None:
        from nekro_agent.routers.rpc import _ops_deny_message

        message = _ops_deny_message(reason)
        assert "运维工具调用被拒绝" in message
        assert hint in message

    async def test_a_missing_grant_still_produces_a_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """调用前必须过复核，理论上不会是空的；真空了也不能把这次调用丢掉"""
        lines: List[str] = []

        class _Recorder:
            def info(self, message: str, *_args: Any, **_kwargs: Any) -> None:
                lines.append(message)

            def __getattr__(self, _name: str) -> Any:
                return lambda *_a, **_k: None

        monkeypatch.setattr(audit_module, "logger", _Recorder())
        await audit_module.record_ops_event(grant=None, tool="ops_overview", outcome="refused", reason="no_grant")

        assert lines and "outcome=refused" in lines[0] and "round=-" in lines[0]
        assert "operators=-" in lines[0]

    async def test_long_arguments_are_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lines: List[str] = []

        class _Recorder:
            def info(self, message: str, *_args: Any, **_kwargs: Any) -> None:
                lines.append(message)

            def __getattr__(self, _name: str) -> Any:
                return lambda *_a, **_k: None

        monkeypatch.setattr(audit_module, "logger", _Recorder())
        grant = grant_module.OpsRoundGrant(
            round_id="probe",
            container_key=CONTAINER,
            chat_key=CHAT_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            operators=frozenset({OPERATOR}),
        )
        await audit_module.record_ops_event(grant=grant, tool="ops_find", args={"keyword": "x" * 2000}, outcome="executed")

        assert len(lines[0]) < 2000
        assert "…" in lines[0]

    async def test_no_argument_renders_an_empty_bracket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        lines: List[str] = []

        class _Recorder:
            def info(self, message: str, *_args: Any, **_kwargs: Any) -> None:
                lines.append(message)

            def __getattr__(self, _name: str) -> Any:
                return lambda *_a, **_k: None

        monkeypatch.setattr(audit_module, "logger", _Recorder())
        await audit_module.record_ops_event(grant=None, tool="ops_overview", outcome="executed")
        assert "args=[]" in lines[0]
