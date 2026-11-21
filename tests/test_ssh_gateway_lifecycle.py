"""SSH 服务器凭据的生命周期：自动挂起、删实例、换实例对、划转

## 这条测试钉的是什么

凭据行的归属不是它自己的事：工作区变成非平台方归属时，平台方行要挂起（且不会自动解除）；
实例被彻底删除时，用户行跟着删；实例改判到另一个实例对时，用户行跟着改；租户划转时，
用户行默认直接删除，只有明确取消勾选才跟着实例走。四个动作都要留审计，且平台方行不该被
后三个动作碰到。

## 环境

内存 SQLite；主密钥 monkeypatch 到临时目录；多租户 / 实例 / 频道 / 工作区照既有用例造。
「曾经不归平台方」标记文件由全局固件改道到临时目录；后台挂起与撤销任务在每条用例里等完，
避免它们落进下一条用例的库会话。秘密值一律现编。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.routers.console import instances as instances_router
from nekro_agent.routers.console import tenants as tenants_router
from nekro_agent.schemas.console import InstanceAdoptRequest, InstanceMigrationRequest
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.schemas.tenant import TenantInstanceBindRequest, TenantRole
from nekro_agent.services import chat_key_migration
from nekro_agent.services.console import instance_manager
from nekro_agent.services.mcp import web_chat_workspace_taint as taint
from nekro_agent.services.ssh_gateway import suspend
from nekro_agent.services.ssh_gateway.constants import (
    EVENT_CRED_DELETE,
    EVENT_SUSPEND,
    REASON_WORKSPACE_NOT_PLATFORM,
    STATUS_ACTIVE,
    STATUS_SUSPENDED,
)
from nekro_agent.services.ssh_gateway.credentials import ssh_credential_service
from nekro_agent.services.ssh_gateway.secrets import encrypt_secret
from nekro_agent.services.user.role import Role
from nekro_agent.services.workspace.credential_policy import wait_pending_revokes
from nekro_agent.tools import credential_cipher

ADAPTER = "onebot_v11"
TENANT_A = 41
TENANT_B = 42
PASSWORD = "pw-" + "lifecycle-7e2"
KEY = "lifecycle-at-rest-" + "5d8"


# ---------------------------------------------------------------------------
# 环境与夹具
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def cipher_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY)
    credential_cipher.reset_credential_cipher_cache()
    try:
        yield
    finally:
        credential_cipher.reset_credential_cipher_cache()


@pytest.fixture
def no_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """向量库不参与本契约；实例删除时它不在本模块的断言范围内"""
    async def _noop(_instance_key: str) -> None:
        return None

    monkeypatch.setattr(instance_manager, "purge_instance_vectors", _noop)


def platform_owner() -> DBUser:
    return DBUser(
        username="owner",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def tenant_admin(tenant_id: int, name: str) -> DBUser:
    return DBUser(
        username=name,
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


async def make_tenant(name: str) -> DBTenant:
    return await DBTenant.create(name=name, display_name=name)


async def make_instance(instance_key: str, tenant_id: Optional[int]) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=instance_key,
        status="online",
        provider="remote_onebot",
        tenant_id=tenant_id,
        metadata_json="",
    )


async def make_workspace(name: str, instance_key: str) -> DBWorkspace:
    workspace = await DBWorkspace.create(name=name, description=name)
    await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        channel_id=f"group_{workspace.id}",
        chat_key=f"{ADAPTER}-{instance_key}-group_{workspace.id}",
        channel_type="group",
        data="{}",
        workspace_id=workspace.id,
    )
    return workspace


async def make_user_row(
    workspace: DBWorkspace,
    alias: str,
    *,
    instance_key: str,
    owner_account: str = "user-a",
) -> DBWsSshCredential:
    return await DBWsSshCredential.create(
        workspace_id=workspace.id,
        owner_kind="user",
        owner_account=owner_account,
        adapter_key=ADAPTER,
        instance_key=instance_key,
        created_via="admin",
        alias=alias,
        host="192.0.2." + "44",
        port=22,
        username="root",
        password_enc=encrypt_secret(PASSWORD),
        status=STATUS_ACTIVE,
        created_by="seed",
        updated_by="seed",
    )


async def make_platform_row(workspace: DBWorkspace, alias: str) -> DBWsSshCredential:
    return await DBWsSshCredential.create(
        workspace_id=workspace.id,
        owner_kind="platform",
        owner_account="",
        adapter_key="",
        instance_key="",
        created_via="admin",
        alias=alias,
        host="192.0.2." + "45",
        port=22,
        username="root",
        password_enc=encrypt_secret(PASSWORD),
        status=STATUS_ACTIVE,
        created_by="seed",
        updated_by="seed",
    )


async def settle() -> None:
    """把挂起与撤销两条后台任务都等完，避免它们落进下一条用例的库会话"""
    await suspend.wait_pending_suspends()
    await wait_pending_revokes()


# ---------------------------------------------------------------------------
# 自动挂起
# ---------------------------------------------------------------------------


class TestSuspendOnTaint:
    async def test_real_taint_path_suspends_platform_rows_and_keeps_user_rows(self, db) -> None:  # noqa: ARG002
        tenant = await make_tenant("tenant-a")
        await make_instance("inst-a", tenant.id)
        workspace = await make_workspace("ws-a", "inst-a")
        platform_row = await make_platform_row(workspace, "platform-server")
        user_row = await make_user_row(workspace, "user-server", instance_key="inst-a")

        added = taint.taint_workspaces([workspace.id], reason="用例归属变化")
        assert added == {workspace.id}
        await settle()

        fresh_platform = await DBWsSshCredential.get_or_none(id=platform_row.id)
        assert fresh_platform is not None
        assert fresh_platform.status == STATUS_SUSPENDED
        assert fresh_platform.status_reason == REASON_WORKSPACE_NOT_PLATFORM
        fresh_user = await DBWsSshCredential.get_or_none(id=user_row.id)
        assert fresh_user is not None and fresh_user.status == STATUS_ACTIVE

        audits = await DBWsSshAudit.filter(event=EVENT_SUSPEND)
        assert len(audits) == 1
        assert audits[0].credential_id == platform_row.id
        assert audits[0].actor == "system"
        assert audits[0].detail == "用例归属变化"

    async def test_repeated_taint_does_not_write_duplicate_audit(self, db) -> None:  # noqa: ARG002
        await make_instance("inst-a", TENANT_A)
        workspace = await make_workspace("ws-a", "inst-a")
        await make_platform_row(workspace, "platform-server")

        taint.taint_workspaces([workspace.id], reason="第一次")
        await settle()
        second = taint.taint_workspaces([workspace.id], reason="第二次")
        await settle()

        assert second == set()
        assert await DBWsSshAudit.filter(event=EVENT_SUSPEND).count() == 1

    async def test_suspend_is_idempotent_and_returns_zero_second_time(self, db) -> None:  # noqa: ARG002
        await make_instance("inst-a", TENANT_A)
        workspace = await make_workspace("ws-a", "inst-a")
        await make_platform_row(workspace, "platform-server")

        first = await suspend.suspend_platform_rows([workspace.id], reason="用例")
        second = await suspend.suspend_platform_rows([workspace.id], reason="用例")

        assert first == 1
        assert second == 0
        assert await DBWsSshAudit.filter(event=EVENT_SUSPEND).count() == 1


class TestStartupSuspend:
    async def test_startup_suspends_workspace_that_no_longer_allows_platform(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        await make_instance("inst-a", TENANT_A)
        workspace = await make_workspace("ws-a", "inst-a")
        platform_row = await make_platform_row(workspace, "platform-server")

        async def _not_platform(_workspace: DBWorkspace) -> bool:
            return True

        monkeypatch.setattr(suspend, "_definitely_not_platform", _not_platform)
        changed = await suspend.suspend_platform_rows_on_startup()

        assert changed == 1
        fresh = await DBWsSshCredential.get_or_none(id=platform_row.id)
        assert fresh is not None and fresh.status == STATUS_SUSPENDED
        audit = await DBWsSshAudit.filter(event=EVENT_SUSPEND).first()
        assert audit is not None and audit.detail == "启动复核"

    async def test_startup_leaves_allowed_workspaces_alone(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        workspace = await DBWorkspace.create(name="ws-platform", description="ws-platform")
        platform_row = await make_platform_row(workspace, "platform-server")

        async def _still_platform(_workspace: DBWorkspace) -> bool:
            return False

        monkeypatch.setattr(suspend, "_definitely_not_platform", _still_platform)
        changed = await suspend.suspend_platform_rows_on_startup()

        assert changed == 0
        fresh = await DBWsSshCredential.get_or_none(id=platform_row.id)
        assert fresh is not None and fresh.status == STATUS_ACTIVE

    async def test_startup_does_not_suspend_when_the_verdict_is_unavailable(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        """标记记录读不出来：授权路径按「不能拿」拒，但粘的挂起不能因为一次读失败就落下"""
        await make_instance("inst-a", TENANT_A)
        workspace = await make_workspace("ws-a", "inst-a")
        platform_row = await make_platform_row(workspace, "platform-server")

        from nekro_agent.services.mcp import web_chat_workspace_taint
        from nekro_agent.services.plugin import scope_guard

        async def _has_tenants() -> bool:
            return True

        async def _record_unavailable() -> bool:
            return False

        monkeypatch.setattr(scope_guard, "deployment_has_tenants", _has_tenants)
        monkeypatch.setattr(web_chat_workspace_taint, "ensure_taint_record", _record_unavailable)
        changed = await suspend.suspend_platform_rows_on_startup()

        assert changed == 0
        fresh = await DBWsSshCredential.get_or_none(id=platform_row.id)
        assert fresh is not None and fresh.status == STATUS_ACTIVE

    async def test_startup_suspends_even_when_gateway_is_off(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        await make_instance("inst-a", TENANT_A)
        workspace = await make_workspace("ws-a", "inst-a")
        platform_row = await make_platform_row(workspace, "platform-server")

        async def _not_platform(_workspace: DBWorkspace) -> bool:
            return True

        monkeypatch.setattr(suspend, "_definitely_not_platform", _not_platform)
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        changed = await suspend.suspend_platform_rows_on_startup()

        assert changed == 1
        fresh = await DBWsSshCredential.get_or_none(id=platform_row.id)
        assert fresh is not None and fresh.status == STATUS_SUSPENDED


# ---------------------------------------------------------------------------
# 彻底删除实例
# ---------------------------------------------------------------------------


class TestPurgeInstance:
    async def test_deletes_only_user_rows_of_this_instance(self, db, no_vectors) -> None:  # noqa: ARG002
        tenant = await make_tenant("tenant-a")
        instance = await make_instance("inst-a", tenant.id)
        await make_instance("inst-b", tenant.id)
        workspace = await make_workspace("ws-a", "inst-a")
        user_row = await make_user_row(workspace, "mine", instance_key="inst-a")
        other_row = await make_user_row(workspace, "other", instance_key="inst-b")
        platform_row = await make_platform_row(workspace, "platform")

        counts = await instance_manager.purge_instance(instance)

        assert counts["ssh_credentials"] == 1
        assert not await DBWsSshCredential.filter(id=user_row.id).exists()
        assert await DBWsSshCredential.filter(id=other_row.id).exists()
        assert await DBWsSshCredential.filter(id=platform_row.id).exists()
        audit = await DBWsSshAudit.filter(event=EVENT_CRED_DELETE).first()
        assert audit is not None
        assert audit.credential_id == user_row.id
        assert audit.actor == "system"
        assert audit.detail == "实例被删除"


# ---------------------------------------------------------------------------
# 换实例对
# ---------------------------------------------------------------------------


class TestInstanceMigration:
    async def test_user_row_moves_to_the_target_instance(self, db) -> None:  # noqa: ARG002
        workspace = await make_workspace("ws-a", "inst-src")
        user_row = await make_user_row(workspace, "mine", instance_key="inst-src")
        platform_row = await make_platform_row(workspace, "platform")

        counts = await chat_key_migration._migrate_instance_scoped_rows(  # noqa: SLF001
            ADAPTER,
            "inst-src",
            "inst-dst",
        )

        assert counts["ssh_credential"] == 1
        fresh = await DBWsSshCredential.get_or_none(id=user_row.id)
        assert fresh is not None and (fresh.adapter_key, fresh.instance_key) == (ADAPTER, "inst-dst")
        fresh_platform = await DBWsSshCredential.get_or_none(id=platform_row.id)
        assert fresh_platform is not None
        assert (fresh_platform.adapter_key, fresh_platform.instance_key) == ("", "")

        residual = await chat_key_migration._count_instance_residual(ADAPTER, "inst-src")  # noqa: SLF001
        assert residual["ssh_credential"] == 0

    @pytest.mark.parametrize("delete", [False, True])
    async def test_moved_and_deleted_rows_notify_the_gateway_after_commit(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
        delete: bool,
    ) -> None:
        """跟过去的与删掉的行都要通知网关：按旧归属授权的连接立刻断，不等下一次复核"""
        from nekro_agent.services.ssh_gateway import events as ssh_events

        workspace = await make_workspace("ws-a", "inst-src")
        rows = [await make_user_row(workspace, alias, instance_key="inst-src") for alias in ("one", "two")]
        notified: list[tuple[int, Optional[str]]] = []

        async def _record(credential_id: int) -> None:
            fresh = await DBWsSshCredential.get_or_none(id=credential_id)
            notified.append((credential_id, None if fresh is None else str(fresh.instance_key)))

        monkeypatch.setattr(ssh_events, "notify_credential_changed", _record)

        await chat_key_migration._migrate_instance_scoped_rows(  # noqa: SLF001
            ADAPTER,
            "inst-src",
            "inst-dst",
            delete_ssh_credentials=delete,
            actor="owner",
        )

        # 通知发出时库已经改好（事务已提交）：删掉的读不到，跟过去的已经在目标实例上
        expected_state = None if delete else "inst-dst"
        assert sorted(notified) == sorted((int(row.id), expected_state) for row in rows)


class TestConsoleInstanceMigration:
    """总后台换实例对：跨租户时照划转的做法，默认删除用户登记的服务器凭据"""

    async def _world(self, *, same_tenant: bool = False) -> Any:
        tenant_a = await make_tenant("tenant-a")
        tenant_b = tenant_a if same_tenant else await make_tenant("tenant-b")
        await make_instance("inst-src", tenant_a.id)
        await make_instance("inst-dst", tenant_b.id)
        workspace = await make_workspace("ws-a", "inst-src")
        user_row = await make_user_row(workspace, "mine", instance_key="inst-src")
        platform_row = await make_platform_row(workspace, "platform")
        return workspace, user_row, platform_row

    async def _run(self, **kwargs: Any) -> Any:
        result = await instances_router.run_console_instance_migration(
            "inst-dst",
            InstanceMigrationRequest(
                source_instance_key="inst-src",
                confirm_source_instance_key="inst-src",
                **kwargs,
            ),
            _current_user=platform_owner(),
        )
        await settle()
        return result

    async def test_preview_reports_count_and_cross_tenant(self, db) -> None:  # noqa: ARG002
        await self._world()
        preview = await instances_router.preview_console_instance_migration(
            "inst-dst",
            "inst-src",
            _current_user=platform_owner(),
        )
        assert preview.cross_tenant is True
        assert preview.ssh_credentials == 1

    async def test_preview_same_tenant_is_not_cross_tenant(self, db) -> None:  # noqa: ARG002
        await self._world(same_tenant=True)
        preview = await instances_router.preview_console_instance_migration(
            "inst-dst",
            "inst-src",
            _current_user=platform_owner(),
        )
        assert preview.cross_tenant is False
        assert preview.ssh_credentials == 1

    async def test_cross_tenant_default_deletes_with_audit(self, db) -> None:  # noqa: ARG002
        _workspace, user_row, platform_row = await self._world()

        result = await self._run()

        assert not await DBWsSshCredential.filter(id=user_row.id).exists()
        assert await DBWsSshCredential.filter(id=platform_row.id).exists()
        assert result.counts["ssh_credential_deleted"] == 1
        assert result.counts["ssh_credential"] == 0
        assert result.residual["ssh_credential"] == 0
        assert "服务器凭据" in result.message
        audit = await DBWsSshAudit.filter(event=EVENT_CRED_DELETE).first()
        assert audit is not None
        assert audit.credential_id == user_row.id
        assert audit.actor == "owner"
        assert audit.detail == "实例迁移（跨租户）"
        assert (audit.adapter_key, audit.instance_key) == (ADAPTER, "inst-src")

    async def test_cross_tenant_unchecked_moves_rows(self, db) -> None:  # noqa: ARG002
        _workspace, user_row, _platform_row = await self._world()

        result = await self._run(delete_ssh_credentials=False)

        fresh = await DBWsSshCredential.get_or_none(id=user_row.id)
        assert fresh is not None and fresh.instance_key == "inst-dst"
        assert result.counts["ssh_credential"] == 1
        assert result.counts["ssh_credential_deleted"] == 0
        assert await DBWsSshAudit.filter(event=EVENT_CRED_DELETE).count() == 0

    async def test_same_tenant_always_moves_rows(self, db) -> None:  # noqa: ARG002
        """同一归属下的迁移，开关不起作用：默认值为真也照样跟过去"""
        _workspace, user_row, _platform_row = await self._world(same_tenant=True)

        result = await self._run()

        fresh = await DBWsSshCredential.get_or_none(id=user_row.id)
        assert fresh is not None and fresh.instance_key == "inst-dst"
        assert result.counts["ssh_credential_deleted"] == 0
        assert await DBWsSshAudit.filter(event=EVENT_CRED_DELETE).count() == 0

    async def test_delete_failure_rolls_back_and_keeps_rows(self, db, monkeypatch) -> None:  # noqa: ARG002
        """审计写不下来时整段回滚：凭据原样留在源实例，不会半删半跟"""
        _workspace, user_row, _platform_row = await self._world()

        async def broken_audit(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("audit down")

        monkeypatch.setattr(ssh_credential_service, "_write_audit", broken_audit)
        with pytest.raises(RuntimeError):
            await self._run()

        fresh = await DBWsSshCredential.get_or_none(id=user_row.id)
        assert fresh is not None and fresh.instance_key == "inst-src"


class TestAdoptUnclaimedInstance:
    """待认领协议端并入既有实例：与跨租户换实例对同一口径，默认删除用户登记的服务器凭据"""

    async def _world(self, *, target_tenant: bool = True) -> DBWsSshCredential:
        source = await make_instance("ob_src", None)
        await DBAdapterInstance.filter(id=source.id).update(status="unclaimed")
        tenant_id = (await make_tenant("tenant-b")).id if target_tenant else None
        await make_instance("inst_dst", tenant_id)
        workspace = await make_workspace("ws-a", "ob_src")
        return await make_user_row(workspace, "mine", instance_key="ob_src")

    async def _adopt(self, **kwargs: Any) -> Any:
        result = await instances_router.adopt_console_instance(
            "inst_dst",
            InstanceAdoptRequest(source_instance_key="ob_src", confirm_source_instance_key="ob_src", **kwargs),
            _current_user=platform_owner(),
        )
        await settle()
        return result

    async def test_migration_preview_gives_the_count_for_the_adopt_pair(self, db) -> None:  # noqa: ARG002
        """并入对话框用换实例对的预览取条数与是否跨租户"""
        await self._world()
        preview = await instances_router.preview_console_instance_migration(
            "inst_dst",
            "ob_src",
            _current_user=platform_owner(),
        )
        assert preview.cross_tenant is True
        assert preview.ssh_credentials == 1

    async def test_cross_tenant_default_deletes_with_audit(self, db) -> None:  # noqa: ARG002
        user_row = await self._world()

        result = await self._adopt()

        assert result.ok is True
        assert result.source_archived is True
        assert not await DBWsSshCredential.filter(id=user_row.id).exists()
        assert result.migration is not None
        assert result.migration.counts["ssh_credential_deleted"] == 1
        assert result.migration.counts["ssh_credential"] == 0
        assert "服务器凭据" in result.message
        audit = await DBWsSshAudit.filter(event=EVENT_CRED_DELETE).first()
        assert audit is not None
        assert audit.credential_id == user_row.id
        assert audit.actor == "owner"
        assert audit.detail == "待认领协议端并入（跨租户）"
        assert (audit.adapter_key, audit.instance_key) == (ADAPTER, "ob_src")

    async def test_cross_tenant_unchecked_moves_rows(self, db) -> None:  # noqa: ARG002
        user_row = await self._world()

        result = await self._adopt(delete_ssh_credentials=False)

        fresh = await DBWsSshCredential.get_or_none(id=user_row.id)
        assert fresh is not None and fresh.instance_key == "inst_dst"
        assert result.migration is not None
        assert result.migration.counts["ssh_credential"] == 1
        assert result.migration.counts["ssh_credential_deleted"] == 0
        assert await DBWsSshAudit.filter(event=EVENT_CRED_DELETE).count() == 0

    async def test_same_owner_always_moves_rows(self, db) -> None:  # noqa: ARG002
        """待认领行与目标都归平台方：开关不起作用，默认值为真也照样跟过去"""
        user_row = await self._world(target_tenant=False)

        result = await self._adopt()

        fresh = await DBWsSshCredential.get_or_none(id=user_row.id)
        assert fresh is not None and fresh.instance_key == "inst_dst"
        assert result.migration is not None
        assert result.migration.counts["ssh_credential_deleted"] == 0
        assert await DBWsSshAudit.filter(event=EVENT_CRED_DELETE).count() == 0

    async def test_service_default_keeps_rows(self, db) -> None:  # noqa: ARG002
        """服务层默认不删：只有总后台的并入接口按所有者口径默认传真"""
        user_row = await self._world()
        source = await DBAdapterInstance.get(instance_key="ob_src")
        target = await DBAdapterInstance.get(instance_key="inst_dst")

        await instance_manager.adopt_unclaimed_instance(source, target)
        await settle()

        fresh = await DBWsSshCredential.get_or_none(id=user_row.id)
        assert fresh is not None and fresh.instance_key == "inst_dst"


# ---------------------------------------------------------------------------
# 划转
# ---------------------------------------------------------------------------


class TestTenantTransfer:
    async def _world(self) -> Any:
        tenant_a = await make_tenant("tenant-a")
        tenant_b = await make_tenant("tenant-b")
        instance = await make_instance("inst-a", tenant_a.id)
        workspace = await make_workspace("ws-a", "inst-a")
        user_row = await make_user_row(workspace, "mine", instance_key="inst-a")
        platform_row = await make_platform_row(workspace, "platform")
        return tenant_a, tenant_b, instance, workspace, user_row, platform_row

    async def _unbind(self, tenant: DBTenant) -> None:
        """先收回成平台方实例；这一步不删凭据，下一步才测划入时的删除"""
        await tenants_router.bind_tenant_instances(
            tenant_id=tenant.id,
            body=TenantInstanceBindRequest(instance_keys=[], delete_ssh_credentials=False),
            _current_user=platform_owner(),
        )
        await settle()

    async def test_preview_reports_user_credential_count(self, db) -> None:  # noqa: ARG002
        tenant_a, tenant_b, _instance, _workspace, _user_row, _platform_row = await self._world()
        await self._unbind(tenant_a)

        preview = await tenants_router.preview_tenant_instances(
            tenant_id=tenant_b.id,
            body=TenantInstanceBindRequest(instance_keys=["inst-a"]),
            _current_user=platform_owner(),
        )

        assert len(preview.changes) == 1
        assert preview.changes[0].ssh_credentials == 1

    async def test_default_deletes_user_rows_and_writes_audit(self, db) -> None:  # noqa: ARG002
        tenant_a, tenant_b, _instance, workspace, user_row, platform_row = await self._world()
        await self._unbind(tenant_a)

        await tenants_router.bind_tenant_instances(
            tenant_id=tenant_b.id,
            body=TenantInstanceBindRequest(instance_keys=["inst-a"]),
            _current_user=platform_owner(),
        )
        await settle()

        assert not await DBWsSshCredential.filter(id=user_row.id).exists()
        assert await DBWsSshCredential.filter(id=platform_row.id).exists()
        audit = await DBWsSshAudit.filter(event=EVENT_CRED_DELETE).first()
        assert audit is not None
        assert audit.credential_id == user_row.id
        assert audit.actor == "owner"
        assert audit.detail == "实例划转"
        # 平台方行的状态可能已被归属变化挂起，但行本身不该被划转删掉
        assert await DBWsSshCredential.filter(workspace_id=workspace.id, owner_kind="platform").count() == 1

    async def test_keeping_rows_moves_visibility_to_the_new_tenant(self, db) -> None:  # noqa: ARG002
        tenant_a, tenant_b, _instance, workspace, user_row, _platform_row = await self._world()

        old_admin = tenant_admin(tenant_a.id, "old-admin")
        assert len(await ssh_credential_service.list_credentials(old_admin, workspace.id)) == 1

        await self._unbind(tenant_a)

        await tenants_router.bind_tenant_instances(
            tenant_id=tenant_b.id,
            body=TenantInstanceBindRequest(instance_keys=["inst-a"], delete_ssh_credentials=False),
            _current_user=platform_owner(),
        )
        await settle()

        assert await DBWsSshCredential.filter(id=user_row.id).exists()
        with pytest.raises(NotFoundError):
            await ssh_credential_service.list_credentials(old_admin, workspace.id)

        new_admin = tenant_admin(tenant_b.id, "new-admin")
        visible = await ssh_credential_service.list_credentials(new_admin, workspace.id)
        assert [row.id for row in visible] == [user_row.id]

    async def test_replay_without_ownership_change_keeps_rows(self, db) -> None:  # noqa: ARG002
        tenant_a, tenant_b, _instance, _workspace, user_row, _platform_row = await self._world()
        await self._unbind(tenant_a)
        await tenants_router.bind_tenant_instances(
            tenant_id=tenant_b.id,
            body=TenantInstanceBindRequest(instance_keys=["inst-a"], delete_ssh_credentials=False),
            _current_user=platform_owner(),
        )
        await settle()
        assert await DBWsSshCredential.filter(id=user_row.id).exists()

        # 原样重放：集合没变，归属一个都没变，默认删除也不该动任何行
        await tenants_router.bind_tenant_instances(
            tenant_id=tenant_b.id,
            body=TenantInstanceBindRequest(instance_keys=["inst-a"]),
            _current_user=platform_owner(),
        )
        await settle()

        assert await DBWsSshCredential.filter(id=user_row.id).exists()
        assert await DBWsSshAudit.filter(event=EVENT_CRED_DELETE).count() == 0
