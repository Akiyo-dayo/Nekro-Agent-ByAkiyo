"""微信 iLink 多实例：一个实例只有一行会话，每个写入口只写自己的列

`adapter_instance_session.instance_id` 原来只有普通索引，`upsert_session` / `set_session_state` 是先查后建，
并发绑定 / 续期会插出多条；之后按 `instance_id` 取单行的地方（`get_or_none` 是 limit 2 + 单行语义）每次都抛
`MultipleObjectsReturned`，实例从此起不来。修法移植上游 `d130486` / `67ddc4b`：列加唯一约束，「确保行存在」放在事务外，
事务内加锁更新；迁移 27 先归并存量重复行再建唯一索引。

本分支追加：每个写入口只写自己负责的列（`update_fields`）。`BotConnection` 拿着连接建立时读出来的会话对象整行写回，
会把别的入口在这之间写的凭据、绑定子状态冲回旧值。

行锁与并发插入只能在真 PG 上证明（SQLite 没有行锁），见 `test_wechat_session_unique_pg.py`；
本文件守 SQLite 上能跑的部分和迁移文件的形状。
"""

import base64
import copy
import importlib.util
import json
import pathlib
import zlib
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List

import pytest
from tortoise import Tortoise
from tortoise.exceptions import IntegrityError

from nekro_agent.adapters.wechat_ilink_multi import bot_manager as bot_manager_module
from nekro_agent.adapters.wechat_ilink_multi.bot_connection import BotConnection
from nekro_agent.adapters.wechat_ilink_multi.bot_manager import BotManager
from nekro_agent.adapters.wechat_ilink_multi.config import WeChatILinkMultiConfig
from nekro_agent.adapters.wechat_ilink_multi.schemas import (
    BindPollResult,
    BindStatus,
    ContextToken,
    OpenILinkCredentials,
    OpenILinkMessage,
    OpenILinkRecipient,
    RecipientKind,
    SyncState,
)
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_adapter_instance_session import DBAdapterInstanceSession
from nekro_agent.services import adapter_instance_service as service_module
from nekro_agent.services.adapter_instance_service import AdapterInstanceService, adapter_instance_service

MIGRATION_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "models"
OLD_CREDENTIALS = json.dumps({"provider_account_id": "acc", "access_token": "old-token"})
NEW_CREDENTIALS = json.dumps({"provider_account_id": "acc", "access_token": "new-token"})


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _seed_instance() -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key="wechat_ilink_multi",
        instance_key="wx1",
        display_name="wx1",
        status="online",
        enabled=True,
        provider="openilink",
        provider_account_id="acc",
    )


async def _only_row(instance: DBAdapterInstance) -> DBAdapterInstanceSession:
    rows = await DBAdapterInstanceSession.filter(instance_id=instance.id)
    assert len(rows) == 1, f"一个实例应当恰好一行会话，实际 {len(rows)} 行"
    return rows[0]


def _another_writer_lands_before_the_save(monkeypatch: pytest.MonkeyPatch, **columns: Any) -> List[str]:
    """在第一次 `save` 真正落库之前，用另一条语句把 `columns` 写进同一行：模拟别的入口恰好在「读」与「写」之间落了一笔"""
    real_save = DBAdapterInstanceSession.save
    landed: List[str] = []

    async def save(self: DBAdapterInstanceSession, *args: Any, **kwargs: Any) -> None:
        if not landed:
            landed.append("x")
            await DBAdapterInstanceSession.filter(id=self.id).update(**columns)
        await real_save(self, *args, **kwargs)

    monkeypatch.setattr(DBAdapterInstanceSession, "save", save)
    return landed


def _bare_connection(instance: DBAdapterInstance, session: DBAdapterInstanceSession) -> BotConnection:
    """只填被测方法用到的字段；真实构造要 SDK 客户端与配置，与这里关心的写回无关"""
    conn = object.__new__(BotConnection)
    conn.instance = instance
    conn.session = session
    return conn


# ============================================================
# 一、一个实例只有一行
# ============================================================


class TestOneRowPerInstance:
    def test_model_declares_instance_id_unique(self) -> None:
        assert DBAdapterInstanceSession._meta.fields_map["instance_id"].unique is True

    async def test_database_rejects_a_second_row(self, db) -> None:
        await DBAdapterInstanceSession.create(instance_id=7)
        with pytest.raises(IntegrityError):
            await DBAdapterInstanceSession.create(instance_id=7)

    async def test_losing_the_insert_race_is_absorbed(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """判断时还没有、插入时已经有了（别的协程先插进去）：吞掉唯一约束错误，不抛、不多插"""
        await DBAdapterInstanceSession.create(instance_id=7, credentials_json=OLD_CREDENTIALS)
        attempts: List[Dict[str, Any]] = []
        real_create = DBAdapterInstanceSession.create

        async def not_yet(**kwargs: Any) -> bool:
            return False

        async def spy_create(**kwargs: Any) -> Any:
            attempts.append(kwargs)
            return await real_create(**kwargs)

        monkeypatch.setattr(DBAdapterInstanceSession, "exists", not_yet)
        monkeypatch.setattr(DBAdapterInstanceSession, "create", spy_create)

        await AdapterInstanceService._ensure_session_row(7)

        assert attempts == [{"instance_id": 7}], "用例要真的走到撞约束那一步，否则什么也没证明"
        rows = await DBAdapterInstanceSession.filter(instance_id=7)
        assert len(rows) == 1
        assert rows[0].credentials_json == OLD_CREDENTIALS, "已有的那行原样保留"

    async def test_an_unrelated_integrity_error_is_not_hidden(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """反向对照：吞的只是 `IntegrityError`，别的错误照常往上抛"""

        async def boom(**kwargs: Any) -> Any:
            raise RuntimeError("db down")

        monkeypatch.setattr(DBAdapterInstanceSession, "create", boom)
        with pytest.raises(RuntimeError):
            await AdapterInstanceService._ensure_session_row(7)

    async def test_upsert_twice_updates_the_same_row(self, db) -> None:
        instance = await _seed_instance()

        first = await adapter_instance_service.upsert_session(instance, OLD_CREDENTIALS, "", None, "c-1")
        second = await adapter_instance_service.upsert_session(instance, NEW_CREDENTIALS, "", None, "c-2")

        row = await _only_row(instance)
        assert first.id == second.id == row.id
        assert row.credentials_json == NEW_CREDENTIALS
        assert row.last_cursor == "c-2"

    async def test_service_writes_go_through_the_row_lock(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """SQLite 上 `select_for_update` 是空操作，这里只钉住调用点：两个写入口都先锁行再改"""
        instance = await _seed_instance()
        locked: List[str] = []
        real = DBAdapterInstanceSession.select_for_update

        def spy(*args: Any, **kwargs: Any) -> Any:
            locked.append("lock")
            return real(*args, **kwargs)

        monkeypatch.setattr(DBAdapterInstanceSession, "select_for_update", spy)

        await adapter_instance_service.set_session_state(instance, "pending")
        await adapter_instance_service.upsert_session(instance, OLD_CREDENTIALS, "", None)

        assert locked == ["lock", "lock"]


# ============================================================
# 二、每个写入口只写自己的列
# ============================================================


class TestServiceWritersOnlyTouchTheirOwnColumns:
    async def test_set_session_state_does_not_write_credentials_back(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = await _seed_instance()
        await adapter_instance_service.upsert_session(instance, OLD_CREDENTIALS, "", None, "c-1")
        landed = _another_writer_lands_before_the_save(monkeypatch, credentials_json=NEW_CREDENTIALS, last_cursor="c-2")

        await adapter_instance_service.set_session_state(instance, "bound")

        assert landed, "插入的那一笔没有发生，用例空转"
        row = await _only_row(instance)
        assert row.session_state == "bound"
        assert row.credentials_json == NEW_CREDENTIALS, "绑定子状态的写入把刚落库的新凭据冲回了旧值"
        assert row.last_cursor == "c-2"

    async def test_upsert_does_not_write_bind_state_back(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = await _seed_instance()
        await adapter_instance_service.set_session_state(instance, "pending")
        landed = _another_writer_lands_before_the_save(monkeypatch, session_state="session_persisted")

        await adapter_instance_service.upsert_session(instance, NEW_CREDENTIALS, "", None, "c-9")

        assert landed
        row = await _only_row(instance)
        assert row.credentials_json == NEW_CREDENTIALS
        assert row.session_state == "session_persisted", "凭据写入把绑定子状态冲回了旧值"


class TestBotConnectionDoesNotWriteAStaleRowBack:
    """连接拿着建立时读出来的 `self.session`；在它写回之前，重新绑定已经把新凭据写进了库"""

    @staticmethod
    async def _stale_connection() -> BotConnection:
        instance = await _seed_instance()
        await adapter_instance_service.upsert_session(instance, OLD_CREDENTIALS, SyncState(cursor="c-1").model_dump_json(), None)
        await adapter_instance_service.set_session_state(instance, "session_persisted")
        stale = await DBAdapterInstanceSession.get(instance_id=instance.id)
        await DBAdapterInstanceSession.filter(id=stale.id).update(credentials_json=NEW_CREDENTIALS, session_state="bound")
        return _bare_connection(instance, stale)

    async def test_sync_state_callback(self, db) -> None:
        conn = await self._stale_connection()

        await conn._handle_sync_state(SyncState(cursor="c-2"))

        row = await _only_row(conn.instance)
        assert row.last_cursor == "c-2"
        assert SyncState.model_validate_json(row.sync_state_json).cursor == "c-2"
        assert row.credentials_json == NEW_CREDENTIALS
        assert row.session_state == "bound"

    async def test_message_cursor(self, db) -> None:
        conn = await self._stale_connection()
        message = OpenILinkMessage(
            message_id="m-1",
            sender_id="u-1",
            recipient=OpenILinkRecipient(kind=RecipientKind.USER, id="u-1"),
            timestamp=datetime.now(timezone.utc),
            context_token=ContextToken(conversation_id="conv", sync_cursor="c-3"),
        )

        await conn._persist_message_cursor(message)

        row = await _only_row(conn.instance)
        assert row.last_cursor == "c-3"
        assert row.last_message_remote_id == "m-1"
        assert row.credentials_json == NEW_CREDENTIALS
        assert row.session_state == "bound"

    async def test_message_without_a_cursor_leaves_the_cursor_alone(self, db) -> None:
        """消息没带游标：只记消息 ID，不把内存里那个旧游标写回去"""
        conn = await self._stale_connection()
        await DBAdapterInstanceSession.filter(instance_id=conn.instance.id).update(last_cursor="c-new")
        message = OpenILinkMessage(
            message_id="m-2",
            sender_id="u-1",
            recipient=OpenILinkRecipient(kind=RecipientKind.USER, id="u-1"),
            timestamp=datetime.now(timezone.utc),
            context_token=ContextToken(conversation_id="conv"),
        )

        await conn._persist_message_cursor(message)

        row = await _only_row(conn.instance)
        assert row.last_message_remote_id == "m-2"
        assert row.last_cursor == "c-new"

    async def test_renewed_at_after_renewal(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """续期写完新凭据之后、记 `renewed_at` 之前，绑定子状态被别的入口改了：记时间那一笔不能把它冲回去"""
        conn = await self._stale_connection()
        renewed = SimpleNamespace(
            credentials=SimpleNamespace(model_dump_json=lambda: NEW_CREDENTIALS, expires_at=None),
            sync_state=SyncState(cursor="c-4"),
        )

        async def renew_session(credentials: Any, sync_state: Any) -> Any:
            return renewed

        async def keep_status(instance: Any, *args: Any, **kwargs: Any) -> Any:
            return instance

        real_upsert = adapter_instance_service.upsert_session

        async def upsert_then_bind_state_moves(*args: Any, **kwargs: Any) -> Any:
            session = await real_upsert(*args, **kwargs)
            await DBAdapterInstanceSession.filter(id=session.id).update(session_state="rebinding")
            return session

        conn.client = SimpleNamespace(renew_session=renew_session)
        monkeypatch.setattr(service_module.adapter_instance_service, "set_runtime_status", keep_status)
        monkeypatch.setattr(service_module.adapter_instance_service, "upsert_session", upsert_then_bind_state_moves)

        await conn.renew_once()

        row = await _only_row(conn.instance)
        assert row.renewed_at is not None
        assert row.last_cursor == "c-4"
        assert row.session_state == "rebinding"


class TestRebindStopsTheOldConnectionBeforeWritingTheNewSession:
    """绑定期间实例可能又被拉起来；旧连接若在新凭据写入之后还在跑，它的游标会落在新凭据旁边"""

    async def test_old_connection_is_stopped_before_any_session_write(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        instance = await _seed_instance()
        manager = BotManager(adapter_key="wechat_ilink_multi", config=WeChatILinkMultiConfig(), adapter_getter=lambda: None)
        calls: List[str] = []

        class Abort(Exception):
            """写到新会话就停：后面要真起连接，与这里关心的先后无关"""

        async def stop_instance(instance_key: str, *, update_status: bool = True) -> None:
            calls.append(f"stop:{instance_key}")

        async def set_session_state(target: Any, state: str) -> None:
            calls.append(f"state:{state}")

        async def upsert_session(*args: Any, **kwargs: Any) -> Any:
            calls.append("upsert")
            raise Abort

        monkeypatch.setattr(manager, "stop_instance", stop_instance)
        monkeypatch.setattr(bot_manager_module.adapter_instance_service, "set_session_state", set_session_state)
        monkeypatch.setattr(bot_manager_module.adapter_instance_service, "upsert_session", upsert_session)
        result = BindPollResult(
            status=BindStatus.CONFIRMED,
            credentials=OpenILinkCredentials(provider_account_id="acc", access_token="t"),
        )

        with pytest.raises(Abort):
            await manager._complete_bind(instance, result)

        assert calls[0] == "stop:wx1", f"写会话之前要先停旧连接，实际顺序 {calls}"
        assert "upsert" in calls, "用例要真的走到写新会话那一步"

    async def test_a_connection_started_during_the_bind_is_stopped_not_orphaned(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """写新会话期间有人把实例拉了起来：挂新连接时要先停掉它，不能把它顶成没人管的孤儿"""
        instance = await _seed_instance()
        manager = BotManager(adapter_key="wechat_ilink_multi", config=WeChatILinkMultiConfig(), adapter_getter=lambda: None)
        stopped: List[str] = []

        class FakeConnection:
            def __init__(self, *, label: str = "new", **kwargs: Any) -> None:
                self.label = label

            async def start(self) -> None:
                return None

            async def wait_until_online(self, timeout_seconds: float) -> None:
                return None

            async def stop(self) -> None:
                stopped.append(self.label)

        real_upsert = adapter_instance_service.upsert_session

        async def someone_starts_the_instance_meanwhile(*args: Any, **kwargs: Any) -> Any:
            manager._connections["wx1"] = FakeConnection(label="racer")  # type: ignore[assignment]
            return await real_upsert(*args, **kwargs)

        monkeypatch.setattr(bot_manager_module, "BotConnection", FakeConnection)
        monkeypatch.setattr(bot_manager_module.adapter_instance_service, "upsert_session", someone_starts_the_instance_meanwhile)
        result = BindPollResult(
            status=BindStatus.CONFIRMED,
            credentials=OpenILinkCredentials(provider_account_id="acc", access_token="t"),
        )

        await manager._complete_bind(instance, result)

        assert stopped == ["racer"], "绑定期间被拉起的连接没有被停掉"
        assert manager._connections["wx1"].label == "new"  # type: ignore[attr-defined]


# ============================================================
# 三、迁移 27 的静态契约
# ============================================================


def load_migration(prefix: str) -> ModuleType:
    candidates = sorted(MIGRATION_DIR.glob(f"{prefix}_*.py"))
    assert len(candidates) == 1, f"迁移 {prefix} 应当恰好一个文件，实际 {candidates}"
    spec = importlib.util.spec_from_file_location(f"migration_{prefix}", candidates[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def decoded_state(module: ModuleType) -> Any:
    return json.loads(zlib.decompress(base64.b64decode(module.MODELS_STATE)))


def instance_id_field(state: Any) -> Dict[str, Any]:
    fields = state["models.DBAdapterInstanceSession"]["data_fields"]
    return next(f for f in fields if f["name"] == "instance_id")


class TestMigration27Contract:
    def test_it_is_the_session_unique_migration_with_lf_line_endings(self) -> None:
        (path,) = MIGRATION_DIR.glob("27_*.py")
        assert path.name.endswith("_unique_adapter_instance_session.py")
        assert b"\r\n" not in path.read_bytes(), "仓库里的迁移都是 LF"

    def test_models_state_only_flips_instance_id_to_unique(self) -> None:
        """`MODELS_STATE` 由 aerich 生成：相对 26 只有这一处变化（state 错了，aerich 下次 diff 就乱）"""
        before = decoded_state(load_migration("26"))
        after = decoded_state(load_migration("27"))

        assert instance_id_field(before)["unique"] is False
        assert instance_id_field(after)["unique"] is True
        expected = copy.deepcopy(before)
        instance_id_field(expected)["unique"] = True
        assert after == expected

    async def test_upgrade_merges_duplicates_before_creating_the_unique_index(self) -> None:
        sql = await load_migration("27").upgrade(db=None)

        audit = sql.index('INSERT INTO "adapter_instance_event"')
        merge = sql.index('DELETE FROM "adapter_instance_session"')
        drop_plain = sql.index('DROP INDEX IF EXISTS "idx_adapter_ins_instanc_4cbd6e"')
        unique = sql.index('CREATE UNIQUE INDEX IF NOT EXISTS "uid_adapter_ins_instanc_4cbd6e"')
        assert audit < merge < drop_plain < unique, "先留痕、再归并，最后换唯一索引；顺序反了建索引当场失败或留痕为空"
        assert "HAVING COUNT(*) > 1" in sql, "只给真有重复行的实例留痕"

    async def test_merge_keeps_credentials_then_newest_then_largest_id(self) -> None:
        sql = await load_migration("27").upgrade(db=None)
        order_by = sql[sql.index("ORDER BY") :]

        keys = ['("credentials_json" <> \'\') DESC', '"update_time" DESC', '"id" DESC']
        positions = [order_by.index(key) for key in keys]
        assert positions == sorted(positions), "保留优先级：有凭据 > 更新时间更新 > id 更大"
        assert 'DISTINCT ON ("instance_id")' in sql

    async def test_downgrade_restores_the_plain_index(self) -> None:
        sql = await load_migration("27").downgrade(db=None)

        assert sql.index('DROP INDEX IF EXISTS "uid_adapter_ins_instanc_4cbd6e"') < sql.index(
            'CREATE INDEX IF NOT EXISTS "idx_adapter_ins_instanc_4cbd6e"',
        )
