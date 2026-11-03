"""自动来源写实例状态：不覆盖停用 / 归档 / 软删

连接、续期、探活、掉线监测、账号回写这些自动来源只能在运行态之间切换，且前提（实例启用、不在停用 / 归档 / 软删）
在**写入那一刻**用条件更新判断。原来它们按手里旧对象的状态做决定、无条件写入：

- 微信 iLink 连接存活期间实例被停用、归档或软删，续期、监控上线、续期失败把状态写回 online / renewing / error；
- 掉线监测本轮开头读出对象、探活期间实例被软删，墓碑被写回活态；「离开墓碑态先核对账号」的守卫看的是旧对象的状态，
  于是跳过核对，另一个实例刚绑上的账号就有了两行活实例；
- 进程退出时停掉连接，把仍连着的停用实例写成 offline。

一、复现：先做一次管理员处置，再让自动来源（拿着处置之前读出的对象）走一遍，断言处置还在。
二、对照：启用中的实例照常写运行态并记事件；跳过时不记事件；不许自动来源写管理员意图状态。
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

from nekro_agent.adapters.wechat_ilink_multi.bot_connection import BotConnection
from nekro_agent.adapters.wechat_ilink_multi.bot_manager import BotManager
from nekro_agent.adapters.wechat_ilink_multi.config import WeChatILinkMultiConfig
from nekro_agent.adapters.wechat_ilink_multi.schemas import BindStartResult, OpenILinkCredentials, SyncState
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.models.db_adapter_instance_session import DBAdapterInstanceSession
from nekro_agent.services import adapter_instance_service as service_module
from nekro_agent.services.adapter_instance_service import adapter_instance_service
from nekro_agent.services.console import instance_manager
from nekro_agent.services.console.instance_health import InstanceHealthService

WECHAT = "wechat_ilink_multi"
ONEBOT = "onebot_v11"
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
    fields: dict = {"display_name": instance_key, "provider": "openilink", "provider_account_id": "acc", "enabled": True, **extra}
    return await DBAdapterInstance.create(adapter_key=adapter_key, instance_key=instance_key, status=status, **fields)


async def fresh(instance: DBAdapterInstance) -> DBAdapterInstance:
    return await DBAdapterInstance.get(id=instance.id)


async def connection_for(instance: DBAdapterInstance, *, renew_fails: bool = False) -> BotConnection:
    """只填被测方法用到的字段；真实构造要 SDK 客户端与配置"""
    session = await DBAdapterInstanceSession.create(instance_id=instance.id, credentials_json=CREDENTIALS)
    conn = object.__new__(BotConnection)
    conn.instance = instance
    conn.session = session
    conn.config = WeChatILinkMultiConfig()
    conn._stopped = asyncio.Event()
    conn._stopped.set()  # 监控上线之后立即返回，不挂在 `_stopped.wait()` 上
    conn._online_started = asyncio.Event()
    conn._online = False
    conn._startup_error = None

    async def renew_session(credentials: Any, sync_state: Any) -> Any:
        if renew_fails:
            raise RuntimeError("上游拒绝续期")
        return SimpleNamespace(
            credentials=OpenILinkCredentials(
                provider_account_id="acc",
                access_token="t2",
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            ),
            sync_state=SyncState(cursor="c-1"),
        )

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    conn.client = SimpleNamespace(renew_session=renew_session, start_monitor=noop, stop_monitor=noop)
    return conn


async def live_rows_with_account(account: str) -> int:
    return await DBAdapterInstance.filter(provider_account_id=account).exclude(status=TOMBSTONE_STATUS).count()


# ============================================================
# 一、复现：自动来源不得覆盖管理员的处置
# ============================================================


class TestWechatConnectionRespectsAdminDecisions:
    async def test_renewal_after_a_disable_leaves_it_disabled(self, db) -> None:
        instance = await seed()
        conn = await connection_for(instance)
        await instance_manager.set_instance_enabled(await fresh(instance), enabled=False)

        await conn.renew_once()

        assert (await fresh(instance)).status == instance_manager.STATUS_DISABLED, "续期把停用的实例写回了运行态"

    async def test_monitor_going_online_after_an_archive_leaves_it_archived(self, db) -> None:
        instance = await seed(status="binding")
        conn = await connection_for(instance)
        await instance_manager.archive_instance(await fresh(instance))

        await conn._run_monitor()

        assert (await fresh(instance)).status == instance_manager.STATUS_ARCHIVED

    async def test_renewal_failure_after_a_soft_delete_leaves_the_tombstone(self, db) -> None:
        instance = await seed()
        conn = await connection_for(instance, renew_fails=True)
        await adapter_instance_service.soft_delete_instance(await fresh(instance))

        with pytest.raises(RuntimeError):
            await conn.renew_once()

        assert (await fresh(instance)).status == TOMBSTONE_STATUS

    @pytest.mark.parametrize("held", [instance_manager.STATUS_DISABLED, instance_manager.STATUS_ARCHIVED])
    async def test_renewing_right_after_a_manual_start_does_not_fail(self, db, held: str) -> None:
        """总后台对一个停用 / 归档、连接没在跑的实例点「续期」：管理器先手动拉起连接再立刻续期，此时内存里的状态
        还是库里读出的停用 / 归档。续期要照常完成、不报错，库里的处置保持不变"""
        instance = await seed(status=held, enabled=False)
        conn = await connection_for(await fresh(instance))

        await conn.renew_once()

        row = await fresh(instance)
        assert row.status == held
        assert row.next_renew_at is not None, "续期本身照常完成"
        assert conn.instance.status == "online", "连接自己记的运行态：刚续期成功，是在线"

    async def test_renew_endpoint_reports_the_stored_status(self, db) -> None:
        """续期接口的响应取库里的当前行：连接内存里记的是它自己的运行态，停用之后库里并没有跟着变"""
        instance = await seed()
        manager = BotManager(adapter_key=WECHAT, config=WeChatILinkMultiConfig(), adapter_getter=lambda: None)
        conn = await connection_for(instance)
        manager._connections["wx1"] = conn
        await instance_manager.set_instance_enabled(await fresh(instance), enabled=False)

        returned = await manager.renew_instance("wx1")

        assert returned.status == instance_manager.STATUS_DISABLED
        assert conn.instance.status == "online", "前置对照：连接内存里确实记着在线"

    async def test_shutdown_leaves_a_disabled_instance_disabled(self, db) -> None:
        """停用不断连：进程退出时停掉这条连接，不该把「停用」改写成 offline"""
        instance = await seed()
        manager = BotManager(adapter_key=WECHAT, config=WeChatILinkMultiConfig(), adapter_getter=lambda: None)

        class StillConnected:
            async def stop(self) -> None:
                return None

        manager._connections["wx1"] = StillConnected()  # type: ignore[assignment]
        await instance_manager.set_instance_enabled(await fresh(instance), enabled=False)

        await manager.stop_all()

        assert (await fresh(instance)).status == instance_manager.STATUS_DISABLED

    async def test_binding_a_disabled_instance_leaves_it_disabled(self, db) -> None:
        """扫码绑定描述的是连接现状，不是对实例的处置：停用中的实例扫码，状态仍显示停用"""
        instance = await seed(status=instance_manager.STATUS_DISABLED, enabled=False)

        class BindClient:
            async def start_bind(self) -> BindStartResult:
                return BindStartResult(
                    session_id="s1",
                    qr_url="https://example.invalid/qr",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                )

        manager = BotManager(
            adapter_key=WECHAT,
            config=WeChatILinkMultiConfig(),
            adapter_getter=lambda: None,
            client_factory=lambda config: BindClient(),  # type: ignore[arg-type, return-value]
        )

        await manager.start_bind("wx1")

        assert (await fresh(instance)).status == instance_manager.STATUS_DISABLED


class TestHealthProbeRespectsAdminDecisions:
    async def test_probe_racing_a_soft_delete_keeps_the_tombstone_and_one_live_binding(self, db, monkeypatch) -> None:
        """探活挂起期间实例被软删、另一个实例随即绑上同一账号：探活回来不能把墓碑写回活态，账号只有一行活实例"""
        tomb = await seed(adapter_key=ONEBOT, instance_key="qq1", status="offline", provider="napcat")
        probing = asyncio.Event()
        release = asyncio.Event()

        class SlowAdapter:
            async def probe_instance(self, instance_key: str) -> None:
                probing.set()
                await release.wait()

        monkeypatch.setattr("nekro_agent.adapters.get_adapter", lambda adapter_key: SlowAdapter())
        stale = await fresh(tomb)

        async def admin_deletes_and_rebinds() -> None:
            await probing.wait()
            await adapter_instance_service.soft_delete_instance(await fresh(tomb))
            await seed(adapter_key=ONEBOT, instance_key="qq2", status="online", provider="napcat")
            release.set()

        await asyncio.gather(InstanceHealthService()._probe_instance(stale), admin_deletes_and_rebinds())

        assert (await fresh(tomb)).status == TOMBSTONE_STATUS, "探活把软删的实例写回了活态"
        assert await live_rows_with_account("acc") == 1, "同一账号出现了两行活实例"

    async def test_probe_does_not_undo_a_disable_made_during_the_probe(self, db) -> None:
        instance = await seed(adapter_key=ONEBOT, instance_key="qq1", status="offline", provider="napcat")
        stale = await fresh(instance)
        await instance_manager.set_instance_enabled(await fresh(instance), enabled=False)

        await InstanceHealthService()._apply_status(stale, "online", "探活成功")

        assert (await fresh(instance)).status == instance_manager.STATUS_DISABLED


class TestAccountSyncRespectsAdminDecisions:
    async def test_account_writeback_after_a_disable_leaves_it_disabled(self, db) -> None:
        """内置 NapCat 回写登录账号时会按账号对齐状态；读出之后实例被停用，对齐不能把停用写成 offline / unbound"""
        instance = await seed(adapter_key=ONEBOT, instance_key="qq1", status="online", provider="napcat", provider_account_id="")
        stale = await fresh(instance)
        await instance_manager.set_instance_enabled(await fresh(instance), enabled=False)

        await instance_manager.sync_account_binding_status(stale)

        assert (await fresh(instance)).status == instance_manager.STATUS_DISABLED

    async def test_startup_normalisation_skips_a_disabled_row(self, db) -> None:
        """启动改判只动启用中的实例：`enabled=False` 而状态仍是 offline 的旧数据不被改判"""
        await seed(adapter_key=ONEBOT, instance_key="qq1", status="offline", provider="napcat", provider_account_id="", enabled=False)
        await seed(adapter_key=ONEBOT, instance_key="qq2", status="offline", provider="napcat", provider_account_id="")

        rewritten = await instance_manager.normalize_unbound_instance_status()

        assert (await DBAdapterInstance.get(instance_key="qq1")).status == "offline"
        assert (await DBAdapterInstance.get(instance_key="qq2")).status == instance_manager.STATUS_UNBOUND
        assert rewritten == ["qq2"]

    async def test_startup_normalisation_racing_a_disable_leaves_it_disabled(self, db, monkeypatch) -> None:
        """候选读出之后、改判写入之前实例被停用：这次不写（判据在写入那一刻）"""
        instance = await seed(adapter_key=ONEBOT, instance_key="qq1", status="offline", provider="napcat", provider_account_id="")
        reached: List[str] = []

        async def admin_disables_it() -> None:
            reached.append("x")
            await instance_manager.set_instance_enabled(await fresh(instance), enabled=False)

        # 停用插在「改判写入」的前一刻：不管改判走哪个写入口，都先让另一个请求把停用提交了
        real_runtime = getattr(adapter_instance_service, "set_runtime_status", None)
        real_set_status = adapter_instance_service.set_status

        async def runtime_after_the_disable(target: Any, *args: Any, **kwargs: Any) -> Any:
            await admin_disables_it()
            assert real_runtime is not None
            return await real_runtime(target, *args, **kwargs)

        async def set_status_after_the_disable(target: Any, new_status: str, *args: Any, **kwargs: Any) -> Any:
            if new_status == instance_manager.STATUS_UNBOUND:  # 停用自己也经 set_status，只在改判那一笔前插入
                await admin_disables_it()
            return await real_set_status(target, new_status, *args, **kwargs)

        # raising=False：修复前的代码没有 set_runtime_status，这条用例在那里也要能跑起来、红在断言上
        monkeypatch.setattr(adapter_instance_service, "set_runtime_status", runtime_after_the_disable, raising=False)
        monkeypatch.setattr(adapter_instance_service, "set_status", set_status_after_the_disable)

        await instance_manager.normalize_unbound_instance_status()

        assert reached, "用例要真的走到改判写入那一步"
        assert (await fresh(instance)).status == instance_manager.STATUS_DISABLED


# ============================================================
# 二、对照
# ============================================================


class TestRuntimeStatusWrites:
    async def test_an_enabled_instance_is_written_and_audited(self, db) -> None:
        instance = await seed(status="online")

        await adapter_instance_service.set_runtime_status(instance, "offline", "掉线")

        assert (await fresh(instance)).status == "offline"
        events = await DBAdapterInstanceEvent.filter(instance_id=instance.id, event_type="status_change")
        assert [(e.status_from, e.status_to) for e in events] == [("online", "offline")]

    async def test_error_keeps_its_message_and_other_states_clear_it(self, db) -> None:
        instance = await seed(status="online")

        await adapter_instance_service.set_runtime_status(instance, "error", "连不上")
        assert (await fresh(instance)).last_error == "连不上"

        await adapter_instance_service.set_runtime_status(instance, "online", "恢复")
        assert (await fresh(instance)).last_error == ""

    async def test_a_skipped_write_records_no_event_and_no_broadcast(self, db, monkeypatch) -> None:
        instance = await seed(status=instance_manager.STATUS_DISABLED, enabled=False)
        broadcasts: List[Any] = []

        async def record(**kwargs: Any) -> None:
            broadcasts.append(kwargs)

        monkeypatch.setattr(service_module, "publish_adapter_instance_status", record)

        await adapter_instance_service.set_runtime_status(instance, "online", "上线")

        assert await DBAdapterInstanceEvent.filter(instance_id=instance.id).count() == 0
        assert broadcasts == []

    async def test_the_enabled_flag_alone_blocks_the_write(self, db) -> None:
        """两个判据各自独立起作用：`enabled=False` 而状态仍是运行态（旧数据、或停用只改了开关）也不写"""
        instance = await seed(status="offline", enabled=False)

        await adapter_instance_service.set_runtime_status(instance, "online", "上线")

        assert (await fresh(instance)).status == "offline"

    @pytest.mark.parametrize("held", [instance_manager.STATUS_ARCHIVED, TOMBSTONE_STATUS, instance_manager.STATUS_DISABLED])
    async def test_an_admin_status_alone_blocks_the_write(self, db, held: str) -> None:
        """另一个判据：状态是停用 / 归档 / 软删而 `enabled` 恰好仍是 True（旧数据），同样不写——墓碑保护不能只靠开关"""
        instance = await seed(status=held, enabled=True)

        await adapter_instance_service.set_runtime_status(instance, "online", "上线")

        assert (await fresh(instance)).status == held

    async def test_the_callers_view_is_kept_in_memory_even_when_skipped(self, db) -> None:
        """连接靠内存里的状态记「自己这条连接的运行态」（续期循环据此判断凭据是否已过期）；库里写没写由条件更新决定"""
        instance = await seed(status=instance_manager.STATUS_DISABLED, enabled=False)

        returned = await adapter_instance_service.set_runtime_status(instance, "session_expired", "凭据过期")

        assert returned.status == "session_expired"
        assert (await fresh(instance)).status == instance_manager.STATUS_DISABLED

    @pytest.mark.parametrize("target", [instance_manager.STATUS_DISABLED, instance_manager.STATUS_ARCHIVED, TOMBSTONE_STATUS])
    async def test_admin_intent_targets_are_ignored_not_raised(self, db, target: str) -> None:
        """自动来源想写管理员意图状态（由旧状态推导出来的）：不写、不改内存、不记事件，也不抛（抛错会变成接口 500）"""
        instance = await seed(status="online")

        returned = await adapter_instance_service.set_runtime_status(instance, target, "")

        assert (await fresh(instance)).status == "online"
        assert returned.status == "online"
        assert await DBAdapterInstanceEvent.filter(instance_id=instance.id).count() == 0

    def test_admin_intent_set_matches_the_console_constants(self) -> None:
        assert service_module.ADMIN_INTENT_STATUSES == {
            instance_manager.STATUS_DISABLED,
            instance_manager.STATUS_ARCHIVED,
            TOMBSTONE_STATUS,
        }


# ============================================================
# 三、真 PostgreSQL：条件更新的判据在 PG 上同样成立
# ============================================================
#
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


class TestConditionalWriteOnRealPostgres:
    async def test_it_really_is_postgres(self, pg_db: str) -> None:
        rows = await connections.get("default").execute_query_dict(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'adapter_instance'",
        )
        assert [row["table_schema"] for row in rows] == [pg_db]

    async def test_running_instances_are_written_and_admin_decisions_are_not(self, pg_db: str) -> None:
        running = await seed(instance_key="wx1", status="online")
        disabled = await seed(instance_key="wx2", status="offline", enabled=False)
        tomb = await seed(instance_key="wx3", status=TOMBSTONE_STATUS, enabled=True)

        for target in (running, disabled, tomb):
            await adapter_instance_service.set_runtime_status(target, "error", "连不上")

        assert [(await fresh(row)).status for row in (running, disabled, tomb)] == ["error", "offline", TOMBSTONE_STATUS]
        assert await DBAdapterInstanceEvent.filter(event_type="status_change").count() == 1
