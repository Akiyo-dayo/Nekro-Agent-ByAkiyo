"""实例行的写入口只写自己负责的列：一段时间以前读出来的实例对象，不能把别人后来改的列冲回旧值

`adapter_instance_service.set_status` / `schedule_renew` 原来对传入的 `DBAdapterInstance` 整行 `save()`。
微信 iLink 的连接拿着建立时读出的 `self.instance` 跑几个小时，掉线监测拿着本轮开头读出的对象去探活；
这期间平台方在租户管理里把实例改划给别的租户（`filter().update(tenant_id=...)`）、或者在总后台停用它，
下一次续期 / 状态流转就把 `tenant_id`、`enabled` 写回旧值——实例回到原租户手里，停用被撤销。

一、复现：每条用例都先读出对象、再从「另一处」改库、最后让旧对象走一遍写入口，断言改动还在。
二、回归：原来靠整行保存顺带落库的调用方（启停、认领、归档、软删、编辑、复活）改成显式声明列之后，各自改的列仍然落库。
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE

from nekro_agent.adapters.wechat_ilink_multi import bot_manager as bot_manager_module
from nekro_agent.adapters.wechat_ilink_multi import routers as routers_module
from nekro_agent.adapters.wechat_ilink_multi.bot_connection import BotConnection
from nekro_agent.adapters.wechat_ilink_multi.bot_manager import BotManager
from nekro_agent.adapters.wechat_ilink_multi.config import WeChatILinkMultiConfig
from nekro_agent.adapters.wechat_ilink_multi.schemas import (
    BindPollResult,
    BindStatus,
    OpenILinkCredentials,
    RenewPolicyRequest,
    SyncState,
)
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_adapter_instance_session import DBAdapterInstanceSession
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import InstanceUpdateRequest
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.adapter_instance_service import adapter_instance_service
from nekro_agent.services.console import instance_manager
from nekro_agent.services.console.instance_health import InstanceHealthService
from nekro_agent.services.user.role import Role

WECHAT = "wechat_ilink_multi"
OLD_TENANT = 1
NEW_TENANT = 2
CREDENTIALS = OpenILinkCredentials(provider_account_id="acc", access_token="t").model_dump_json()


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def seed(*, adapter_key: str = WECHAT, instance_key: str = "wx1", status: str = "online", **extra: Any) -> DBAdapterInstance:
    fields: dict = {
        "display_name": instance_key,
        "provider": "openilink",
        "provider_account_id": "acc",
        "enabled": True,
        "tenant_id": OLD_TENANT,
        **extra,
    }
    return await DBAdapterInstance.create(adapter_key=adapter_key, instance_key=instance_key, status=status, **fields)


async def elsewhere(instance: DBAdapterInstance, **columns: Any) -> None:
    """另一处（租户管理、总后台）直接改库，手里那份对象不知道"""
    await DBAdapterInstance.filter(id=instance.id).update(**columns)


async def fresh(instance: DBAdapterInstance) -> DBAdapterInstance:
    return await DBAdapterInstance.get(id=instance.id)


async def stale_connection(instance: DBAdapterInstance) -> BotConnection:
    """只填续期用到的字段；真实构造要 SDK 客户端与配置"""
    session = await DBAdapterInstanceSession.create(instance_id=instance.id, credentials_json=CREDENTIALS)
    conn = object.__new__(BotConnection)
    conn.instance = instance
    conn.session = session
    conn.config = WeChatILinkMultiConfig()
    renewed = SimpleNamespace(
        credentials=OpenILinkCredentials(
            provider_account_id="acc",
            access_token="t2",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        ),
        sync_state=SyncState(cursor="c-1"),
    )

    async def renew_session(credentials: Any, sync_state: Any) -> Any:
        return renewed

    conn.client = SimpleNamespace(renew_session=renew_session)
    return conn


# ============================================================
# 一、复现：旧对象不得把别人改的列写回
# ============================================================


class TestWechatConnectionDoesNotUndoChangesMadeWhileItRuns:
    async def test_renewal_keeps_a_tenant_reassignment_and_a_disable(self, db) -> None:
        """连接存活期间平台方把实例改划给别的租户、又停用了它：续期（两次状态流转 + 续期计划）之后两者都还在"""
        instance = await seed()
        conn = await stale_connection(instance)
        await elsewhere(instance, tenant_id=NEW_TENANT, enabled=False)

        await conn.renew_once()

        row = await fresh(instance)
        assert row.tenant_id == NEW_TENANT, "续期把实例的租户归属写回了原租户"
        assert row.enabled is False, "续期撤销了停用"
        assert row.next_renew_at is not None, "续期计划照常写入"
        # 不断言 `status`：续期按连接自己记着的旧状态决定恢复成什么，停用之后会落成 online（`enabled` 仍是 False，
        # 入站闸门不漏）。「连接的状态流转能不能覆盖管理员的停用 / 归档 / 软删」是另一件事，单独登记

    async def test_renewal_keeps_a_lead_time_changed_while_it_runs(self, db) -> None:
        """连接存活期间管理员改了续期提前量：续期用新值排计划，也不把它写回旧值"""
        instance = await seed(renew_before_minutes=60)
        conn = await stale_connection(instance)
        await elsewhere(instance, renew_before_minutes=45)

        await conn.renew_once()

        row = await fresh(instance)
        assert row.renew_before_minutes == 45
        session = await DBAdapterInstanceSession.get(instance_id=instance.id)
        assert session.expires_at is not None and row.next_renew_at is not None
        assert abs((session.expires_at - row.next_renew_at) - timedelta(minutes=45)) < timedelta(seconds=1)

    async def test_monitor_going_online_keeps_a_tenant_reassignment(self, db) -> None:
        instance = await seed(status="binding")
        conn = await stale_connection(instance)
        conn._stopped = asyncio.Event()
        conn._stopped.set()  # 上线之后立即返回，不挂在 `_stopped.wait()` 上
        conn._online_started = asyncio.Event()
        conn._online = False
        conn._startup_error = None

        async def noop(*args: Any, **kwargs: Any) -> None:
            return None

        conn.client = SimpleNamespace(start_monitor=noop, stop_monitor=noop)
        await elsewhere(instance, tenant_id=NEW_TENANT)

        await conn._run_monitor()

        row = await fresh(instance)
        assert row.status == "online"
        assert row.tenant_id == NEW_TENANT


class TestHealthProbeDoesNotUndoChangesMadeDuringTheProbe:
    async def test_status_change_keeps_a_tenant_reassignment(self, db) -> None:
        """掉线监测本轮开头读出对象、探活期间实例被改划：照常落「在线」，租户归属不被冲回"""
        instance = await seed(status="offline")
        await elsewhere(instance, tenant_id=NEW_TENANT)

        await InstanceHealthService()._apply_status(instance, "online", "探活成功")

        row = await fresh(instance)
        assert row.status == "online"
        assert row.tenant_id == NEW_TENANT

    async def test_status_change_keeps_a_disable(self, db) -> None:
        """探活期间实例被停用：`enabled` 不被冲回，状态也不被改写成在线"""
        instance = await seed(status="offline")
        await elsewhere(instance, enabled=False)

        await InstanceHealthService()._apply_status(instance, "online", "探活成功")

        row = await fresh(instance)
        assert (row.enabled, row.status) == (False, "offline")


class TestScheduleRenewOnlyWritesTheRenewPlan:
    async def test_stale_object_keeps_a_tenant_reassignment(self, db) -> None:
        instance = await seed()
        await elsewhere(instance, tenant_id=NEW_TENANT, display_name="改过的名字")
        expires_at = datetime.now(timezone.utc) + timedelta(hours=5)

        await adapter_instance_service.schedule_renew(instance, expires_at, 30)

        row = await fresh(instance)
        assert row.tenant_id == NEW_TENANT
        assert row.display_name == "改过的名字"
        assert row.renew_before_minutes == 30
        assert row.next_renew_at is not None


class TestWechatDirectSavesOnlyWriteTheirColumn:
    async def test_complete_bind_writes_the_account_without_undoing_a_reassignment(self, db, monkeypatch) -> None:
        instance = await seed(status="binding", provider_account_id="")
        manager = BotManager(adapter_key=WECHAT, config=WeChatILinkMultiConfig(), adapter_getter=lambda: None)
        real_set_bind_state = manager._set_bind_state
        moved: List[str] = []

        async def reassign_on_first_bind_state(*args: Any, **kwargs: Any) -> None:
            if not moved:
                moved.append("x")
                await elsewhere(instance, tenant_id=NEW_TENANT)
            await real_set_bind_state(*args, **kwargs)

        class FakeConnection:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def start(self) -> None:
                return None

            async def wait_until_online(self, timeout_seconds: float) -> None:
                return None

        monkeypatch.setattr(manager, "_set_bind_state", reassign_on_first_bind_state)
        monkeypatch.setattr(bot_manager_module, "BotConnection", FakeConnection)

        await manager._complete_bind(
            instance,
            BindPollResult(status=BindStatus.CONFIRMED, credentials=OpenILinkCredentials(provider_account_id="acc", access_token="t")),
        )

        row = await fresh(instance)
        assert moved, "改划没有发生，用例空转"
        assert row.provider_account_id == "acc"
        assert row.tenant_id == NEW_TENANT

    async def test_renew_policy_writes_only_the_lead_time(self, db, monkeypatch) -> None:
        instance = await seed()
        real_get = adapter_instance_service.get_instance

        async def read_then_someone_reassigns(*args: Any, **kwargs: Any) -> Any:
            row = await real_get(*args, **kwargs)
            await elsewhere(instance, tenant_id=NEW_TENANT, enabled=False)
            return row

        monkeypatch.setattr(routers_module.adapter_instance_service, "get_instance", read_then_someone_reassigns)
        route = next(r for r in routers_module.create_router().routes if getattr(r, "path", "").endswith("/renew-policy"))
        owner = DBUser(
            username="owner",
            password="",
            adapter_key="webui",
            platform_userid="0",
            perm_level=Role.Super,
            login_time=datetime.now(timezone.utc),
            tenant_id=None,
            tenant_role=TenantRole.PLATFORM_OWNER.value,
        )

        await route.endpoint(instance_key="wx1", body=RenewPolicyRequest(renew_before_minutes=45), _current_user=owner, _owner=owner)

        row = await fresh(instance)
        assert row.renew_before_minutes == 45
        assert row.tenant_id == NEW_TENANT
        assert row.enabled is False


# ============================================================
# 二、回归：原来靠整行保存落库的列，改成显式声明后仍然落库
# ============================================================


class TestCallersStillPersistWhatTheyChanged:
    """每条也顺带验旧对象：调用之前另一处改划了租户，调用之后改划还在"""

    async def test_disable_persists_enabled(self, db) -> None:
        instance = await seed(adapter_key="onebot_v11", status="online")
        await elsewhere(instance, tenant_id=NEW_TENANT)

        await instance_manager.set_instance_enabled(instance, enabled=False)

        row = await fresh(instance)
        assert (row.enabled, row.status, row.tenant_id) == (False, instance_manager.STATUS_DISABLED, NEW_TENANT)

    async def test_enable_from_disabled_persists_enabled(self, db) -> None:
        instance = await seed(adapter_key="onebot_v11", status=instance_manager.STATUS_DISABLED, enabled=False)
        await elsewhere(instance, tenant_id=NEW_TENANT)

        await instance_manager.set_instance_enabled(instance, enabled=True)

        row = await fresh(instance)
        assert row.enabled is True
        assert row.status != instance_manager.STATUS_DISABLED
        assert row.tenant_id == NEW_TENANT

    async def test_enable_while_not_disabled_persists_enabled(self, db) -> None:
        """不处于停用 / 归档 / 墓碑态时只改 `enabled`、不动状态（末尾那一次保存）"""
        instance = await seed(adapter_key="onebot_v11", status="online", enabled=False)
        await elsewhere(instance, tenant_id=NEW_TENANT)

        await instance_manager.set_instance_enabled(instance, enabled=True)

        row = await fresh(instance)
        assert (row.enabled, row.status, row.tenant_id) == (True, "online", NEW_TENANT)

    async def test_claim_persists_name_and_enabled(self, db) -> None:
        instance = await seed(adapter_key="onebot_v11", status=instance_manager.STATUS_UNCLAIMED, enabled=False, display_name="")
        await elsewhere(instance, tenant_id=NEW_TENANT)

        await instance_manager.claim_instance(instance, "认领后的名字")

        row = await fresh(instance)
        assert (row.display_name, row.enabled, row.tenant_id) == ("认领后的名字", True, NEW_TENANT)

    async def test_archive_persists_enabled(self, db) -> None:
        instance = await seed(adapter_key="onebot_v11", status="online")
        await elsewhere(instance, tenant_id=NEW_TENANT)

        await instance_manager.archive_instance(instance)

        row = await fresh(instance)
        assert (row.enabled, row.status, row.tenant_id) == (False, instance_manager.STATUS_ARCHIVED, NEW_TENANT)

    async def test_soft_delete_persists_enabled(self, db) -> None:
        instance = await seed(status="online")
        await elsewhere(instance, tenant_id=NEW_TENANT)

        await adapter_instance_service.soft_delete_instance(instance)

        row = await fresh(instance)
        assert (row.enabled, row.status, row.tenant_id) == (False, "deleted", NEW_TENANT)

    async def test_edit_with_a_toggle_persists_every_edited_field(self, db) -> None:
        instance = await seed(adapter_key="onebot_v11", status="online", provider_account_id="")
        await elsewhere(instance, tenant_id=NEW_TENANT)

        await instance_manager.update_instance(
            instance,
            InstanceUpdateRequest(display_name="新名字", is_default=True, provider_account_id="10001", enabled=False),
        )

        row = await fresh(instance)
        assert (row.display_name, row.is_default, row.provider_account_id, row.enabled) == ("新名字", True, "10001", False)
        assert row.tenant_id == NEW_TENANT

    async def test_edit_without_a_toggle_persists_every_edited_field(self, db) -> None:
        instance = await seed(adapter_key="onebot_v11", status="online")
        await elsewhere(instance, tenant_id=NEW_TENANT, enabled=False)

        await instance_manager.update_instance(instance, InstanceUpdateRequest(display_name="只改名字"))

        row = await fresh(instance)
        assert row.display_name == "只改名字"
        assert (row.tenant_id, row.enabled) == (NEW_TENANT, False), "只改名字的编辑不该写回别的列"

    async def test_create_instance_revives_a_tombstone_and_persists_its_fields(self, db, monkeypatch) -> None:
        """`create_instance` 自己现读现写；改划插在它读完、写回之前（复活前的账号核对那一步）"""
        instance = await seed(status="deleted", enabled=False)

        async def someone_reassigns_meanwhile(target: Any) -> str:
            await elsewhere(instance, tenant_id=NEW_TENANT)
            return ""

        monkeypatch.setattr(adapter_instance_service, "release_account_if_taken", someone_reassigns_meanwhile)

        await adapter_instance_service.create_instance(WECHAT, "wx1", "复活后的名字", "openilink2", {"k": "v"})

        row = await fresh(instance)
        assert (row.status, row.enabled, row.display_name, row.provider) == ("pending", True, "复活后的名字", "openilink2")
        assert row.metadata_json == '{"k": "v"}'
        assert row.tenant_id == NEW_TENANT


# ============================================================
# 三、真 PostgreSQL：同一列在 update_fields 里出现两次
# ============================================================
#
# PG 对同一列赋值两次直接报 `multiple assignments to same column`，SQLite 不报。
# 设 `NA_TEST_PG_DSN`（如 postgres://postgres@127.0.0.1:55499/natest）开门，未设则 skip；CI 的 backend-tests 工作流会设它。


@pytest.fixture
async def pg_db():
    dsn = os.environ.get("NA_TEST_PG_DSN", "")
    if not dsn:
        pytest.skip("需要真实 PostgreSQL：设 NA_TEST_PG_DSN（如 postgres://postgres@127.0.0.1:55499/natest）。CI 的 backend-tests 工作流会设它。")
    import asyncpg

    schema = f"na_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    EXECUTOR_CACHE.clear()
    await Tortoise.init(
        db_url=f"{dsn}{sep}{urlencode({'schema': schema})}",
        modules={"models": ["nekro_agent.models.db_adapter_instance", "nekro_agent.models.db_adapter_instance_event"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield schema
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


class TestExtraFieldsOnRealPostgres:
    async def test_it_really_is_postgres(self, pg_db: str) -> None:
        rows = await connections.get("default").execute_query_dict(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'adapter_instance'",
        )
        assert [row["table_schema"] for row in rows] == [pg_db]

    async def test_a_column_named_twice_is_written_once(self, pg_db: str) -> None:
        instance = await seed(status="online")

        await adapter_instance_service.set_status(instance, "offline", extra_fields=["status", "update_time", "enabled"])

        row = await fresh(instance)
        assert (row.status, row.enabled) == ("offline", True)
