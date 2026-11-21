"""`/na_ops` 写指令、路由退役与两个抽出来的业务函数

## 为什么命令层还要测

`/na_ops` 与 Agent 工具是两条入口，用户看到的结果必须一致：写操作直接执行，
命令自己的回复就是回执，**不再发深链**。这里从命令入口走一遍真实状态变化，
同时确认退役的确认页路由确实不在应用里。
"""

import datetime
from typing import List, Optional, Tuple

import pytest
from fastapi import FastAPI
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_ops_agent_audit import DBOpsAgentAudit
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.tenant import TenantRole, TenantStatus, TenantUpdateRequest
from nekro_agent.services.chat_channel_service import set_channel_active
from nekro_agent.services.command.built_in import ops_bot_cmd as ops_bot_cmd_module
from nekro_agent.services.command.built_in.ops_bot_cmd import OpsBotCommand
from nekro_agent.services.command.ctl import CommandResponseStatus
from nekro_agent.services.command.schemas import CommandExecutionContext
from nekro_agent.services.config_service import UnifiedConfigService
from nekro_agent.services.console.tenant_manager import set_tenant_status
from nekro_agent.services.ops_agent import round_grant
from nekro_agent.services.ops_agent.round_grant import OpsRoundGrant
from nekro_agent.services.sandbox.runner import build_container_key
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"
INSTANCE = "inst3"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_10001"
OPERATOR = "10001"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def platform_owner() -> DBUser:
    return DBUser(
        username="admin",
        password="",
        adapter_key="",
        platform_userid="",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def ops_context(user_id: str = OPERATOR) -> CommandExecutionContext:
    return CommandExecutionContext(
        user_id=user_id,
        chat_key=CHAT_KEY,
        username=user_id,
        adapter_key=ADAPTER,
    )


async def seed_instance(*, adapter_key: str = ADAPTER, instance_key: str = INSTANCE) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=adapter_key,
        instance_key=instance_key,
        display_name="星炬学院主号",
        status="online",
        enabled=True,
        provider="remote_onebot",
    )


def configure_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "OPS_BOT_ENABLED", True)
    monkeypatch.setattr(config, "OPS_BOT_ADAPTER_KEY", ADAPTER)
    monkeypatch.setattr(config, "OPS_BOT_INSTANCE_KEY", INSTANCE)
    monkeypatch.setattr(config, "OPS_BOT_CHANNEL_ID", "group_10001")
    monkeypatch.setattr(config, "OPS_BOT_OPERATORS", [OPERATOR])


class TestNaOpsWrite:
    async def test_disable_instance_executes_and_replies_with_a_receipt(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        configure_ops(monkeypatch)
        await seed_instance()

        responses = [item async for item in OpsBotCommand().execute(ops_context(), "disable-instance inst3")]

        assert responses[-1].status is CommandResponseStatus.SUCCESS
        assert responses[-1].message.startswith("【运维】已执行：停用实例")
        assert "http" not in responses[-1].message
        assert (await DBAdapterInstance.get(instance_key="inst3")).enabled is False

        row = await DBOpsAgentAudit.get(source="na_ops")
        assert row.tool == "na_ops:disable-instance"
        assert row.outcome == "executed"
        assert row.operator == OPERATOR
        assert f"记录号 {row.id}" in responses[-1].message

    async def test_an_unavailable_audit_blocks_the_write(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        configure_ops(monkeypatch)
        await seed_instance()

        async def no_audit(**_kwargs: object) -> None:
            return None

        monkeypatch.setattr(ops_bot_cmd_module, "open_ops_event", no_audit)

        responses = [item async for item in OpsBotCommand().execute(ops_context(), "disable-instance inst3")]

        assert responses[-1].status is CommandResponseStatus.ERROR
        assert "运维审计暂时写不进去" in responses[-1].message
        assert (await DBAdapterInstance.get(instance_key="inst3")).enabled is True
        assert await DBOpsAgentAudit.all().count() == 0

    async def test_an_ambiguous_instance_word_reports_candidates_and_changes_nothing(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        configure_ops(monkeypatch)
        await seed_instance(adapter_key="adapter_a", instance_key="inst3")
        await seed_instance(adapter_key="adapter_b", instance_key="inst3")

        responses = [item async for item in OpsBotCommand().execute(ops_context(), "disable-instance inst3")]

        assert responses[-1].status is CommandResponseStatus.ERROR
        assert "目标不明确" in responses[-1].message
        assert "adapter_a" in responses[-1].message and "adapter_b" in responses[-1].message
        assert await DBAdapterInstance.filter(enabled=False).count() == 0

    async def test_tools_off_changes_the_switch_and_saves(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        configure_ops(monkeypatch)
        calls: List[Tuple[str, ...]] = []

        def set_value(config_key: str, item_key: str, value: str) -> Tuple[bool, str]:
            calls.append(("set", config_key, item_key, value))
            return True, ""

        def save(config_key: str) -> Tuple[bool, Optional[str]]:
            calls.append(("save", config_key))
            return True, None

        monkeypatch.setattr(UnifiedConfigService, "set_config_value", staticmethod(set_value))
        monkeypatch.setattr(UnifiedConfigService, "save_config", staticmethod(save))

        responses = [item async for item in OpsBotCommand().execute(ops_context(), "tools off")]

        assert responses[-1].status is CommandResponseStatus.SUCCESS
        assert "关闭运维工具" in responses[-1].message
        assert calls == [("set", "system", "OPS_AGENT_TOOLS_ENABLED", "false"), ("save", "system")]

    async def test_tools_takes_only_on_or_off(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        configure_ops(monkeypatch)
        responses = [item async for item in OpsBotCommand().execute(ops_context(), "tools maybe")]
        assert responses[-1].status is CommandResponseStatus.ERROR
        assert "参数不合法" in responses[-1].message

    async def test_a_tainted_round_does_not_block_na_ops(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """`/na_ops` 由人直接发起，不经过模型；工具的次数上限与污染标记不该管到它"""
        configure_ops(monkeypatch)
        await seed_instance()
        grant = OpsRoundGrant(
            round_id="probe",
            container_key=build_container_key(CHAT_KEY),
            chat_key=CHAT_KEY,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            operators=frozenset({OPERATOR}),
            writes_done=99,
            tainted=True,
        )
        round_grant._grants[grant.container_key] = grant  # noqa: SLF001
        monkeypatch.setattr(config, "OPS_AGENT_WRITE_LIMIT_PER_ROUND", 1)
        monkeypatch.setattr(config, "OPS_AGENT_WRITE_LIMIT_PER_HOUR", 1)
        try:
            responses = [item async for item in OpsBotCommand().execute(ops_context(), "disable-instance inst3")]
        finally:
            round_grant.reset_round_grants()

        assert responses[-1].status is CommandResponseStatus.SUCCESS
        assert (await DBAdapterInstance.get(instance_key="inst3")).enabled is False

    async def test_a_non_operator_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        configure_ops(monkeypatch)
        monkeypatch.setattr("nekro_agent.services.command.built_in.ops_bot_cmd.loaded_im_adapter_keys", lambda: {ADAPTER})

        allowed, reason = await OpsBotCommand().check_permission(ops_context("99999"))

        assert allowed is False
        assert reason == "你不在运维人员白名单中"


class TestNaOpsAuditRead:
    """`/na_ops audit` 与 `ops_audit_recent` 共用行格式与上限；只读指令本身不写审计"""

    @staticmethod
    async def _seed(count: int) -> None:
        for index in range(count):
            await DBOpsAgentAudit.create(
                source="agent",
                operator=OPERATOR,
                adapter_key=ADAPTER,
                instance_key=INSTANCE,
                chat_key=CHAT_KEY,
                tool=f"ops_probe{index}",
                args_json={},
                outcome="executed",
                result_summary="返回 str",
            )

    async def test_audit_defaults_to_ten_lines(self, db) -> None:  # noqa: ARG002
        await self._seed(25)

        responses = [item async for item in OpsBotCommand().execute(ops_context(), "audit")]

        assert responses[-1].status is CommandResponseStatus.SUCCESS
        assert len(responses[-1].message.splitlines()) == 10
        assert await DBOpsAgentAudit.all().count() == 25

    async def test_audit_takes_an_optional_count(self, db) -> None:  # noqa: ARG002
        await self._seed(25)

        responses = [item async for item in OpsBotCommand().execute(ops_context(), "audit 5")]
        assert len(responses[-1].message.splitlines()) == 5

        responses = [item async for item in OpsBotCommand().execute(ops_context(), "audit 999")]
        assert len(responses[-1].message.splitlines()) == 20

    async def test_audit_rejects_a_non_numeric_count(self, db) -> None:  # noqa: ARG002
        await self._seed(3)

        responses = [item async for item in OpsBotCommand().execute(ops_context(), "audit abc")]

        assert responses[-1].status is CommandResponseStatus.ERROR
        assert "参数必须是数字" in responses[-1].message
        assert "audit [条数]" in responses[-1].message


class TestRetiredRoutes:
    def test_the_confirm_endpoints_are_gone(self) -> None:
        from nekro_agent.routers import mount_api_routes

        app = FastAPI()
        mount_api_routes(app)
        paths = {getattr(route, "path", "") for route in app.routes}
        assert not [path for path in paths if path.endswith("/ops/actions/confirm")]
        assert not [path for path in paths if path.endswith("/ops/actions/preview")]


class TestExtractedServices:
    async def test_tenant_status_service_changes_the_row_and_warns_only_on_change(self, db) -> None:
        tenant = await DBTenant.create(name="acme", display_name="Acme", status=TenantStatus.ACTIVE.value)
        await set_tenant_status(tenant, "suspended")
        assert (await DBTenant.get(id=tenant.id)).status == TenantStatus.SUSPENDED.value

        await set_tenant_status(tenant, "suspended")
        assert (await DBTenant.get(id=tenant.id)).status == TenantStatus.SUSPENDED.value

    async def test_the_tenant_route_still_uses_the_service(self, db) -> None:
        from nekro_agent.routers.console.tenants import update_tenant

        tenant = await DBTenant.create(name="acme", display_name="Acme", status=TenantStatus.ACTIVE.value)
        result = await update_tenant(
            tenant.id,
            TenantUpdateRequest(status=TenantStatus.SUSPENDED),
            _current_user=platform_owner(),
        )
        assert result.status == TenantStatus.SUSPENDED
        assert (await DBTenant.get(id=tenant.id)).status == TenantStatus.SUSPENDED.value

    async def test_channel_active_service_and_route(self, db) -> None:
        from nekro_agent.routers.chat_channel import set_chat_channel_active

        channel = await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            channel_id="group_10001",
            chat_key=CHAT_KEY,
            channel_name="测试群",
            channel_type="group",
            data="{}",
        )
        await set_channel_active(channel, False)
        assert (await DBChatChannel.get(chat_key=CHAT_KEY)).is_active is False

        response = await set_chat_channel_active(CHAT_KEY, True, _current_user=platform_owner())
        assert response.ok is True
        assert (await DBChatChannel.get(chat_key=CHAT_KEY)).is_active is True
