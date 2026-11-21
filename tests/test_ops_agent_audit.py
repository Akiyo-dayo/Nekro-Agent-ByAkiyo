"""运维审计表：写入、脱敏、清理、审计工具与 RPC 端到端

## 为什么审计要单独测

「执行前先记、执行后关闭」在进程中途崩掉时才有意义，而这种时序在正常运行里看不出来：
只有把「业务函数被调用时表里是什么」当成断言，才能证明 `pending` 行真的先于副作用。
异常原文不落表、参数脱敏、365 天清理同理，都是「一旦写错就再也没法从结果反推」的规矩。

## 清理用注入的时钟

不注入就只能干等一年。单调时钟与墙钟分开注入：前者决定「该不该清」，后者决定
「早于哪一天」。两次写之间推进单调时钟，就能精确地测出「24 小时内不再清理」。
"""

import datetime
from typing import Any, Dict, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_ops_agent_audit import DBOpsAgentAudit
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.ops_agent import audit as audit_module
from nekro_agent.services.ops_agent import round_grant as grant_module
from nekro_agent.services.ops_agent import tools as tools_module
from nekro_agent.services.ops_agent.audit import (
    ARGS_MAX_KEYS,
    ARGS_MAX_VALUE_CHARS,
    close_ops_event,
    format_ops_event_lines,
    list_ops_events,
    open_ops_event,
    record_ops_event,
)
from nekro_agent.services.ops_agent.round_grant import OpsRoundGrant, open_round, reset_round_grants
from nekro_agent.services.ops_agent.tools import ops_audit_recent
from nekro_agent.services.ops_agent.triggers import TRIGGER_KIND_HUMAN, RoundTrigger, round_trigger_book
from nekro_agent.services.sandbox.runner import build_container_key

ADAPTER = "onebot_v11"
INSTANCE = "opsinst"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_10001"
OPERATOR = "10001"
CONTAINER = build_container_key(CHAT_KEY)
SECRET = "sk-" + "a" * 40


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
    audit_module.reset_audit_state()
    yield
    reset_round_grants()
    audit_module.reset_audit_state()


@pytest.fixture
def ops_on(monkeypatch: pytest.MonkeyPatch):
    for name, value in {
        "OPS_AGENT_TOOLS_ENABLED": True,
        "OPS_AGENT_OPERATORS": [OPERATOR],
        "OPS_AGENT_GROUPS": [CHAT_KEY],
        "OPS_AGENT_PRIVATE_ENABLED": True,
        "OPS_AGENT_WRITE_LIMIT_PER_ROUND": 10,
        "OPS_AGENT_WRITE_LIMIT_PER_HOUR": 10,
    }.items():
        monkeypatch.setattr(config, name, value)


async def seed_instance() -> None:
    await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        display_name=INSTANCE,
        status="online",
        enabled=True,
        provider="remote_onebot",
        provider_account_id="10000012",
    )


async def setup_round() -> OpsRoundGrant:
    round_trigger_book.record(
        CHAT_KEY,
        RoundTrigger(kind=TRIGGER_KIND_HUMAN, adapter_key=ADAPTER, instance_key=INSTANCE, sender_id=OPERATOR, operator_trusted=True),
    )
    channel = await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_10001",
        chat_key=CHAT_KEY,
        channel_type="group",
        data="{}",
    )
    grant = await open_round(channel)
    assert grant is not None
    return grant


def ctx_of() -> AgentCtx:
    return AgentCtx(from_chat_key=CHAT_KEY, container_key=CONTAINER)


async def grant_row(*, outcome: str, tool: str = "ops_find", operator: str = OPERATOR, source: str = "agent") -> None:
    await DBOpsAgentAudit.create(
        source=source,
        operator=operator,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        chat_key=CHAT_KEY,
        tool=tool,
        args_json={},
        outcome=outcome,
    )


class TestAuditRows:
    async def test_open_writes_pending_and_close_finishes_it(self, db) -> None:  # noqa: ARG002
        event_id = await open_ops_event(grant=None, tool="ops_set_channel_active", args={"chat_key": "x"}, source="agent")
        assert event_id is not None

        row = await DBOpsAgentAudit.get(id=event_id)
        assert row.outcome == "pending"
        assert row.finished_at is None
        assert row.source == "agent"

        await close_ops_event(event_id, outcome="executed", summary="已执行：停用频道")
        row = await DBOpsAgentAudit.get(id=event_id)
        assert row.outcome == "executed"
        assert row.finished_at is not None
        assert row.result_summary == "已执行：停用频道"

    async def test_record_writes_a_finished_row(self, db) -> None:  # noqa: ARG002
        event_id = await record_ops_event(
            grant=None,
            tool="ops_list_tenants",
            outcome="refused",
            reason="tainted",
            operator=OPERATOR,
            source="agent",
        )
        assert event_id is not None

        row = await DBOpsAgentAudit.get(id=event_id)
        assert row.outcome == "refused"
        assert row.refuse_reason == "tainted"
        assert row.finished_at is not None

    async def test_grant_fills_operator_round_and_location(self, db, ops_on) -> None:  # noqa: ARG002
        await seed_instance()
        grant = await setup_round()
        event_id = await open_ops_event(grant=grant, tool="ops_overview", source="agent")
        assert event_id is not None

        row = await DBOpsAgentAudit.get(id=event_id)
        assert row.operator == OPERATOR
        assert row.round_id == grant.round_id
        assert (row.adapter_key, row.instance_key, row.chat_key) == (ADAPTER, INSTANCE, CHAT_KEY)

    async def test_arguments_are_redacted_limited_and_truncated(self, db) -> None:  # noqa: ARG002
        args: Dict[str, Any] = {"token": SECRET, "long": "字" * (ARGS_MAX_VALUE_CHARS + 50)}
        for index in range(ARGS_MAX_KEYS + 2):
            args[f"k{index:02d}"] = "v"

        event_id = await record_ops_event(grant=None, tool="ops_find", args=args, outcome="refused", reason="no_grant")
        assert event_id is not None

        row = await DBOpsAgentAudit.get(id=event_id)
        stored = row.args_json
        assert SECRET not in str(stored)
        assert stored["token"] == "[redacted_credential]"
        assert len(stored["long"]) == ARGS_MAX_VALUE_CHARS
        assert len(stored) == ARGS_MAX_KEYS + 1
        assert stored["…"] == "+4"

    async def test_write_failure_returns_none_without_raising(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("database down")

        monkeypatch.setattr(DBOpsAgentAudit, "create", boom)

        assert await open_ops_event(grant=None, tool="ops_overview", source="agent") is None
        assert await record_ops_event(grant=None, tool="ops_overview", outcome="refused", reason="no_grant") is None

    async def test_operator_filter_is_a_contains_match(self, db) -> None:  # noqa: ARG002
        await grant_row(outcome="executed", operator="10001,20002")
        await grant_row(outcome="executed", operator="30003")

        total, rows = await list_ops_events(limit=10, operator="2000")
        assert total == 1
        assert rows[0].operator == "10001,20002"

    async def test_list_is_newest_first_and_paged(self, db) -> None:  # noqa: ARG002
        first = await record_ops_event(grant=None, tool="ops_overview", outcome="executed", source="agent")
        second = await record_ops_event(grant=None, tool="ops_find", outcome="executed", source="agent")
        assert first is not None and second is not None

        total, rows = await list_ops_events(limit=1)
        assert total == 2
        assert [row.id for row in rows] == [second]

        _, rows = await list_ops_events(limit=1, offset=1)
        assert [row.id for row in rows] == [first]

    async def test_tool_and_source_filters_are_exact(self, db) -> None:  # noqa: ARG002
        await grant_row(outcome="executed", tool="ops_find", source="agent")
        await grant_row(outcome="executed", tool="ops_overview", source="agent")
        await record_ops_event(grant=None, tool="na_ops:tools", outcome="executed", source="na_ops", operator=OPERATOR)

        total, rows = await list_ops_events(limit=10, tool="ops_find")
        assert total == 1 and rows[0].tool == "ops_find"

        total, rows = await list_ops_events(limit=10, source="na_ops")
        assert total == 1 and rows[0].tool == "na_ops:tools"


class TestFormatting:
    async def test_a_line_has_the_agreed_shape(self, db) -> None:  # noqa: ARG002
        event_id = await record_ops_event(
            grant=None,
            tool="ops_find",
            outcome="refused",
            reason="tainted",
            summary="没有执行",
            operator=OPERATOR,
            source="agent",
        )
        assert event_id is not None
        row = await DBOpsAgentAudit.get(id=event_id)

        line = format_ops_event_lines([row])
        assert line.startswith(f"#{event_id} ")
        assert " agent " in line
        assert '"ops_find"' in line
        assert "refused（tainted）" in line
        assert f'操作人 "{OPERATOR}"' in line
        assert line.endswith("没有执行")

    async def test_an_empty_table_has_its_own_sentence(self) -> None:
        assert format_ops_event_lines([]) == "最近没有运维操作记录。"


class TestRetention:
    async def test_first_write_purges_older_than_365_days_and_keeps_364(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
        state = {"mono": 1000.0, "now": now}
        monkeypatch.setattr(audit_module, "_clock", lambda: state["mono"])
        monkeypatch.setattr(audit_module, "_wall_clock", lambda: state["now"])

        too_old = await DBOpsAgentAudit.create(source="agent", tool="old", outcome="executed")
        kept = await DBOpsAgentAudit.create(source="agent", tool="recent", outcome="executed")
        await DBOpsAgentAudit.filter(id=too_old.id).update(created_at=now - datetime.timedelta(days=366))
        await DBOpsAgentAudit.filter(id=kept.id).update(created_at=now - datetime.timedelta(days=364))

        assert await record_ops_event(grant=None, tool="ops_overview", outcome="executed", source="agent") is not None

        assert await DBOpsAgentAudit.filter(id=too_old.id).exists() is False
        assert await DBOpsAgentAudit.filter(id=kept.id).exists() is True

    async def test_a_second_write_within_a_day_does_not_purge_again(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
        state = {"mono": 1000.0, "now": now}
        monkeypatch.setattr(audit_module, "_clock", lambda: state["mono"])
        monkeypatch.setattr(audit_module, "_wall_clock", lambda: state["now"])

        assert await record_ops_event(grant=None, tool="ops_overview", outcome="executed", source="agent") is not None

        stale = await DBOpsAgentAudit.create(source="agent", tool="stale", outcome="executed")
        await DBOpsAgentAudit.filter(id=stale.id).update(created_at=now - datetime.timedelta(days=366))

        state["mono"] += 3600
        assert await record_ops_event(grant=None, tool="ops_overview", outcome="executed", source="agent") is not None
        assert await DBOpsAgentAudit.filter(id=stale.id).exists() is True

        state["mono"] += 86400
        assert await record_ops_event(grant=None, tool="ops_overview", outcome="executed", source="agent") is not None
        assert await DBOpsAgentAudit.filter(id=stale.id).exists() is False


class TestAuditTool:
    @pytest.fixture(autouse=True)
    def _enable_ops(self, ops_on) -> None:  # noqa: ARG002
        return None

    async def _grant(self) -> OpsRoundGrant:
        await seed_instance()
        return await setup_round()

    async def test_it_returns_lines_without_arguments_or_error_type(self, db) -> None:  # noqa: ARG002
        await self._grant()
        await record_ops_event(
            grant=None,
            tool="ops_find",
            args={"keyword": "可辨认的参数原文"},
            outcome="executed",
            summary="返回 str",
            operator=OPERATOR,
        )

        text = await ops_audit_recent(ctx_of())
        assert isinstance(text, str)
        assert "可辨认的参数原文" not in text
        assert "error_type" not in text
        assert '"ops_find"' in text

    async def test_empty_table_says_so(self, db) -> None:  # noqa: ARG002
        await self._grant()
        assert await ops_audit_recent(ctx_of()) == "最近没有运维操作记录。"

    async def test_limit_is_clamped_and_bad_types_fall_back_to_ten(self, db) -> None:  # noqa: ARG002
        await self._grant()
        for index in range(25):
            await record_ops_event(grant=None, tool=f"ops_probe{index}", outcome="executed", source="agent")

        assert len((await ops_audit_recent(ctx_of(), 999)).splitlines()) == 20
        assert len((await ops_audit_recent(ctx_of(), 0)).splitlines()) == 1
        assert len((await ops_audit_recent(ctx_of(), True)).splitlines()) == 10  # type: ignore[arg-type]
        assert len((await ops_audit_recent(ctx_of(), "5")).splitlines()) == 10  # type: ignore[arg-type]

    async def test_it_does_not_taint_the_round(self, db) -> None:  # noqa: ARG002
        grant = await self._grant()
        await record_ops_event(grant=None, tool="ops_find", outcome="executed", source="agent")

        await ops_audit_recent(ctx_of())
        assert grant.tainted is False

        allowed, reason = await grant_module.authorize_call(CONTAINER, CHAT_KEY, "ops_set_channel_active", is_write=True)
        assert (allowed, reason) == (True, "")


class TestRpcEndToEnd:
    @staticmethod
    async def _token(methods: List[str]) -> str:
        from nekro_agent.services.sandbox.credential_guard import issue_sandbox_call_grant

        return issue_sandbox_call_grant(container_key=CONTAINER, from_chat_key=CHAT_KEY, method_names=methods)

    @staticmethod
    def _release() -> None:
        from nekro_agent.services.sandbox.credential_guard import revoke_sandbox_call_grant

        revoke_sandbox_call_grant(CONTAINER)

    async def _call(self, method: str, *args: Any) -> Any:
        from nekro_agent.routers.rpc import rpc_exec
        from tests.rpc_sandbox_client import FakeRpcRequest, request_body

        token = await self._token([method])
        try:
            return await rpc_exec(CONTAINER, CHAT_KEY, FakeRpcRequest(request_body(method, *args)), x_rpc_token=token)  # type: ignore[arg-type]
        finally:
            self._release()

    async def test_pending_is_visible_inside_the_business_function_and_receipt_carries_the_record_id(
        self,
        db,
        ops_on,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_instance()
        await setup_round()

        seen: List[str] = []

        async def fake_write(*_args: Any, **_kwargs: Any) -> Any:
            row = await DBOpsAgentAudit.get(tool="ops_set_channel_active")
            seen.append(row.outcome)
            return tools_module.writes.WriteOutcome(ok=True, action_label="停用频道", target_label="测试群")

        async def fake_send(_chat_key: str, _text: str) -> bool:
            return True

        monkeypatch.setattr(tools_module.writes, "set_channel_active", fake_write)
        monkeypatch.setattr(tools_module, "send_receipt", fake_send)

        from nekro_agent.tools.safe_pickle import safe_loads

        response = await self._call("ops_set_channel_active", CHAT_KEY, False)
        assert response.headers["Run-Error"] == "False"
        text = safe_loads(response.body)

        assert seen == ["pending"]
        row = await DBOpsAgentAudit.get(tool="ops_set_channel_active")
        assert row.outcome == "executed"
        assert f"记录号 {row.id}" in text
        assert row.result_summary == text

    async def test_a_business_failure_closes_the_row_as_failed_with_its_category(
        self,
        db,
        ops_on,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_instance()
        await setup_round()

        async def fake_write(*_args: Any, **_kwargs: Any) -> Any:
            return tools_module.writes.WriteOutcome(
                ok=False,
                action_label="停用频道",
                target_label="不存在的群",
                error_category=tools_module.writes.ERROR_NOT_FOUND,
            )

        async def fake_send(_chat_key: str, _text: str) -> bool:
            return True

        monkeypatch.setattr(tools_module.writes, "set_channel_active", fake_write)
        monkeypatch.setattr(tools_module, "send_receipt", fake_send)

        await self._call("ops_set_channel_active", CHAT_KEY, False)

        row = await DBOpsAgentAudit.get(tool="ops_set_channel_active")
        assert row.outcome == "failed"
        assert row.refuse_reason == tools_module.writes.ERROR_NOT_FOUND

    async def test_a_tainted_round_is_refused_and_the_business_function_is_not_called(
        self,
        db,
        ops_on,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_instance()
        grant = await setup_round()
        grant.tainted = True
        calls: List[Any] = []

        async def fake_write(*args: Any, **_kwargs: Any) -> Any:
            calls.append(args)
            return tools_module.writes.WriteOutcome(ok=True, action_label="停用频道", target_label="测试群")

        monkeypatch.setattr(tools_module.writes, "set_channel_active", fake_write)

        response = await self._call("ops_set_channel_active", CHAT_KEY, False)

        assert response.headers["Rpc-Rejected"] == "True"
        assert "已经读过外部内容" in response.body.decode()
        assert calls == []
        row = await DBOpsAgentAudit.get(tool="ops_set_channel_active")
        assert row.outcome == "refused"
        assert row.refuse_reason == "tainted"

    async def test_an_unavailable_audit_blocks_writes_but_not_reads(
        self,
        db,
        ops_on,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_instance()
        await setup_round()
        calls: List[Any] = []

        async def fake_write(*args: Any, **_kwargs: Any) -> Any:
            calls.append(args)
            return tools_module.writes.WriteOutcome(ok=True, action_label="停用频道", target_label="测试群")

        async def broken_create(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("audit db down")

        monkeypatch.setattr(tools_module.writes, "set_channel_active", fake_write)
        monkeypatch.setattr(audit_module.DBOpsAgentAudit, "create", broken_create)

        blocked = await self._call("ops_set_channel_active", CHAT_KEY, False)
        assert blocked.headers["Rpc-Rejected"] == "True"
        assert "运维审计暂时写不进去" in blocked.body.decode()
        assert calls == []

        # 读操作不改东西：审计写不进去时仍要能查
        from nekro_agent.tools.safe_pickle import safe_loads

        read_response = await self._call("ops_list_tenants")
        assert read_response.headers["Run-Error"] == "False"
        assert "租户" in safe_loads(read_response.body)

    async def test_exception_text_never_reaches_the_table(
        self,
        db,
        ops_on,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.ops_agent import provider as ops_provider
        from nekro_agent.services.plugin.schema import SandboxMethod, SandboxMethodType

        await seed_instance()
        await setup_round()

        async def boom(_ctx: Any) -> str:
            raise RuntimeError(f"上游报错 {SECRET}")

        boom._method_type = SandboxMethodType.AGENT  # noqa: SLF001
        original = ops_provider.OPS_METHODS["ops_list_tenants"]
        monkeypatch.setitem(
            ops_provider.OPS_METHODS,
            "ops_list_tenants",
            (SandboxMethod(method_type=SandboxMethodType.AGENT, name="ops_list_tenants", description=original[0].description, func=boom), False),
        )

        response = await self._call("ops_list_tenants")
        assert response.headers["Run-Error"] == "True"

        row = await DBOpsAgentAudit.get(tool="ops_list_tenants")
        assert row.outcome == "failed"
        assert row.error_type == "RuntimeError"
        for column in ("operator", "adapter_key", "instance_key", "chat_key", "round_id", "tool", "refuse_reason", "result_summary", "error_type"):
            assert SECRET not in str(getattr(row, column)), column
        assert SECRET not in str(row.args_json)

    async def test_an_exception_without_a_message_still_closes_the_row_as_failed(
        self,
        db,
        ops_on,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.ops_agent import provider as ops_provider
        from nekro_agent.services.plugin.schema import SandboxMethod, SandboxMethodType

        await seed_instance()
        await setup_round()

        async def silent(_ctx: Any) -> str:
            raise ValueError

        silent._method_type = SandboxMethodType.AGENT  # noqa: SLF001
        original = ops_provider.OPS_METHODS["ops_list_tenants"]
        monkeypatch.setitem(
            ops_provider.OPS_METHODS,
            "ops_list_tenants",
            (SandboxMethod(method_type=SandboxMethodType.AGENT, name="ops_list_tenants", description=original[0].description, func=silent), False),
        )

        await self._call("ops_list_tenants")

        row = await DBOpsAgentAudit.get(tool="ops_list_tenants")
        assert row.outcome == "failed"
        assert row.error_type == "ValueError"

    async def test_a_text_read_is_summarised_by_line_count(
        self,
        db,
        ops_on,  # noqa: ARG002
    ) -> None:
        from nekro_agent.tools.safe_pickle import safe_loads

        await seed_instance()
        await setup_round()

        response = await self._call("ops_list_tenants")
        text = safe_loads(response.body)

        row = await DBOpsAgentAudit.get(tool="ops_list_tenants")
        assert row.outcome == "executed"
        assert row.result_summary == f"返回 str，{len(text.splitlines())} 行"

    async def test_a_write_that_loses_its_grant_is_not_recorded_as_executed(
        self,
        db,
        ops_on,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed_instance()
        await setup_round()
        calls: List[Any] = []

        async def fake_write(*args: Any, **_kwargs: Any) -> Any:
            calls.append(args)
            return tools_module.writes.WriteOutcome(ok=True, action_label="停用频道", target_label="测试群")

        monkeypatch.setattr(tools_module.writes, "set_channel_active", fake_write)
        # 执行前复核已经放行，工具里再取授权时却取不到（授权在两步之间被收回）
        monkeypatch.setattr(tools_module, "get_grant", lambda _key: None)

        await self._call("ops_set_channel_active", CHAT_KEY, False)

        assert calls == []
        row = await DBOpsAgentAudit.get(tool="ops_set_channel_active")
        assert row.outcome == "failed"
        assert row.refuse_reason == "no_grant"


class TestOtherPluginMethodsInAnOpsRound:
    """运维授权回合里调了别的插件方法、结果要回到模型眼前时，一律当作读过外部内容"""

    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, method_type: Any) -> List[str]:
        from nekro_agent.routers import rpc as rpc_module

        async def plugin_echo(_ctx: Any) -> str:
            return "插件拿回来的一段文字"

        plugin_echo._method_type = method_type  # noqa: SLF001
        pushed: List[str] = []

        async def fake_push(*, chat_key: str, agent_messages: str) -> None:
            pushed.append(agent_messages)

        monkeypatch.setattr(rpc_module.plugin_collector, "get_method", lambda name: plugin_echo if name == "plugin_echo" else None)
        monkeypatch.setattr(rpc_module.message_service, "push_system_message", fake_push)
        return pushed

    async def _call(self, method: str) -> Any:
        return await TestRpcEndToEnd()._call(method)  # noqa: SLF001

    @pytest.mark.parametrize("kind", ["AGENT", "MULTIMODAL_AGENT"])
    async def test_a_result_the_model_sees_taints_the_round(
        self,
        db,  # noqa: ARG002
        ops_on,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
        kind: str,
    ) -> None:
        from nekro_agent.services.plugin.schema import SandboxMethodType

        await seed_instance()
        grant = await setup_round()
        self._install(monkeypatch, getattr(SandboxMethodType, kind))

        await self._call("plugin_echo")

        assert grant.tainted is True
        assert grant.taint_source == grant_module.TAINT_TOOL

    async def test_a_behaviour_method_does_not(
        self,
        db,  # noqa: ARG002
        ops_on,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.plugin.schema import SandboxMethodType

        await seed_instance()
        grant = await setup_round()
        pushed = self._install(monkeypatch, SandboxMethodType.BEHAVIOR)

        await self._call("plugin_echo")

        assert grant.tainted is False
        assert pushed == ["插件拿回来的一段文字"]
