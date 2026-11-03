"""微信 iLink 多实例会话表：唯一约束、并发写入与迁移 27，真 Postgres

SQLite 没有行锁（Tortoise 在该方言下忽略 `select_for_update`），事务内撞唯一约束之后的行为也和 PG 不同
（PG 会让整个事务进入 aborted 状态），迁移 27 的 `DISTINCT ON` 更是 PG 专有。所以这三件事只能在真 PG 上证明：
设 `NA_TEST_PG_DSN`（如 `postgres://postgres@127.0.0.1:55499/natest`）开门，未设则 skip；
SQLite 上能跑的部分和迁移文件的形状由 `test_wechat_session_unique.py` 守。CI 的 backend-tests 工作流会设该变量。

表结构不用 `generate_schemas`，而是**执行迁移 11 的建表 SQL**：存量部署的表就是它建的（普通索引），
各用例再按需铺迁移 27，这样测到的是生产上那张表，而不是按新模型生成的另一张。
"""

import asyncio
import importlib.util
import os
import pathlib
import uuid
from types import ModuleType, SimpleNamespace
from typing import Any, List
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE
from tortoise.exceptions import IntegrityError

from nekro_agent.models.db_adapter_instance_event import DBAdapterInstanceEvent
from nekro_agent.models.db_adapter_instance_session import DBAdapterInstanceSession
from nekro_agent.services.adapter_instance_service import AdapterInstanceService, adapter_instance_service

PG_DSN_ENV = "NA_TEST_PG_DSN"
MIGRATION_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "models"


def _pg_dsn_or_skip() -> str:
    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(
            f"需要真实 PostgreSQL：设 {PG_DSN_ENV}（如 postgres://postgres@127.0.0.1:55499/natest）。"
            "SQLite 侧用例与迁移静态契约常绿把关；CI 的 backend-tests 工作流会设它。",
        )
    return dsn


def load_migration(prefix: str) -> ModuleType:
    candidates = sorted(MIGRATION_DIR.glob(f"{prefix}_*.py"))
    assert len(candidates) == 1, f"迁移 {prefix} 应当恰好一个文件，实际 {candidates}"
    spec = importlib.util.spec_from_file_location(f"migration_{prefix}", candidates[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run_migration(prefix: str, direction: str = "upgrade") -> None:
    sql = await getattr(load_migration(prefix), direction)(db=None)
    await connections.get("default").execute_script(sql)


@pytest.fixture
async def pg_env():
    """独占 schema 里按迁移 11 建出的三张实例表（会话表只有普通索引）；EXECUTOR_CACHE 进出两端都清（进程级、不含方言）"""
    dsn = _pg_dsn_or_skip()
    import asyncpg

    schema = f"na_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    EXECUTOR_CACHE.clear()
    await Tortoise.init(
        db_url=f"{dsn}{sep}{urlencode({'schema': schema})}",
        modules={
            "models": [
                "nekro_agent.models.db_adapter_instance_session",
                "nekro_agent.models.db_adapter_instance_event",
            ],
        },
    )
    await run_migration("11")
    try:
        yield schema
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def session_indexes(schema: str) -> List[str]:
    rows = await connections.get("default").execute_query_dict(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = $1 AND tablename = 'adapter_instance_session' "
        "AND indexname <> 'adapter_instance_session_pkey' ORDER BY indexname",
        [schema],
    )
    return [row["indexdef"] for row in rows]


async def seed_raw(rows: str) -> None:
    """按给定的 `update_time` 直接写行（ORM 的 auto_now 会改掉它）；值是字面量，不走占位符"""
    await connections.get("default").execute_script(
        'INSERT INTO "adapter_instance_session" '
        '("id", "instance_id", "credentials_json", "sync_state_json", "update_time") VALUES '
        f"{rows};"
        "SELECT setval('adapter_instance_session_id_seq', (SELECT max(\"id\") FROM \"adapter_instance_session\"));",
    )


def instance(instance_id: int) -> Any:
    """服务层只用到实例的 id；不建 `adapter_instance` 行，免得把后续迁移加的列也拖进来"""
    return SimpleNamespace(id=instance_id)


class TestItReallyIsPostgres:
    async def test_tables_live_in_the_isolated_schema(self, pg_env: str) -> None:
        rows = await connections.get("default").execute_query_dict(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'adapter_instance_session'",
        )
        assert [row["table_schema"] for row in rows] == [pg_env]

    async def test_the_table_starts_in_its_pre_migration_shape(self, pg_env: str) -> None:
        """前置对照：迁移 11 建的是普通索引，同一实例可以有两行——迁移 27 要收拾的就是这个现场"""
        assert [d for d in await session_indexes(pg_env) if "UNIQUE" in d] == []
        await seed_raw("(1, 1, '', '', '2026-10-01'), (2, 1, '', '', '2026-10-02')")
        assert await DBAdapterInstanceSession.filter(instance_id=1).count() == 2


class TestMigration27OnRealPostgres:
    async def test_upgrade_merges_duplicates_by_priority_and_leaves_a_trace(self, pg_env: str) -> None:
        await seed_raw(
            # 实例 1：没凭据但更新、有凭据但旧 → 留有凭据的
            "(1, 1, '', '', '2026-10-02'), (2, 1, '{\"t\":1}', '', '2026-10-01'),"
            # 实例 2：都有凭据，id 小的更新 → 留更新的（不是 id 大的）
            " (3, 2, '{\"t\":2}', '', '2026-10-05'), (4, 2, '{\"t\":3}', '', '2026-10-04'),"
            # 实例 3：都有凭据、更新时间相同 → 留 id 大的
            " (5, 3, '{\"t\":4}', '', '2026-10-03'), (6, 3, '{\"t\":5}', '', '2026-10-03'),"
            # 实例 4：只有一行，不动、不留痕
            " (7, 4, '{\"t\":6}', '', '2026-10-01')",
        )

        await run_migration("27")

        kept = await DBAdapterInstanceSession.all().order_by("instance_id").values_list("instance_id", "id")
        assert list(kept) == [(1, 2), (2, 3), (3, 6), (4, 7)]
        events = await DBAdapterInstanceEvent.filter(event_type="session_dedup").order_by("instance_id")
        assert [e.instance_id for e in events] == [1, 2, 3]
        assert all("迁移27去重" in e.message and "2 条会话行" in e.message and "删除其余 1 条" in e.message for e in events)
        assert all("{" not in e.message and e.payload_json == "" for e in events), "留痕里不写凭据内容"

    async def test_merge_never_crosses_instances(self, pg_env: str) -> None:
        """只在同一 instance_id 内挑：实例 1 的行按优先级全面胜过实例 2 的，实例 2 照样留下自己的一行"""
        await seed_raw(
            "(1, 1, '{\"t\":1}', '', '2026-10-09'), (2, 1, '{\"t\":2}', '', '2026-10-08'),"
            " (3, 2, '', '', '2026-10-02'), (4, 2, '', '', '2026-10-01')",
        )

        await run_migration("27")

        kept = await DBAdapterInstanceSession.all().order_by("instance_id").values_list("instance_id", "id")
        assert list(kept) == [(1, 1), (2, 3)]

    async def test_after_upgrade_the_database_refuses_a_second_row(self, pg_env: str) -> None:
        await run_migration("27")
        assert any("UNIQUE" in d and "instance_id" in d for d in await session_indexes(pg_env))

        await DBAdapterInstanceSession.create(instance_id=9)
        with pytest.raises(IntegrityError):
            await DBAdapterInstanceSession.create(instance_id=9)

    async def test_upgrade_and_downgrade_can_run_twice(self, pg_env: str) -> None:
        await seed_raw("(1, 1, '', '', '2026-10-01'), (2, 1, '', '', '2026-10-02')")
        await run_migration("27")
        await run_migration("27")
        assert await DBAdapterInstanceEvent.filter(event_type="session_dedup").count() == 1, "第二次没有重复行，不再留痕"

        await run_migration("27", "downgrade")
        await run_migration("27", "downgrade")
        indexes = await session_indexes(pg_env)
        assert len(indexes) == 1 and "UNIQUE" not in indexes[0] and "instance_id" in indexes[0], "降级恢复普通索引"


class TestConcurrentWritersOnRealPostgres:
    """用屏障把先查后建的窗口撑开：所有并发调用都过了存在性检查、同时去插入，裁决只剩数据库"""

    @staticmethod
    def _hold_everyone_at_the_insert(monkeypatch: pytest.MonkeyPatch, parties: int) -> None:
        barrier = asyncio.Barrier(parties)
        real_create = DBAdapterInstanceSession.create

        async def create_together(*args: Any, **kwargs: Any) -> Any:
            await asyncio.wait_for(barrier.wait(), timeout=15)
            return await real_create(*args, **kwargs)

        monkeypatch.setattr(DBAdapterInstanceSession, "create", create_together)

    async def test_concurrent_first_upserts_leave_exactly_one_row(self, pg_env: str, monkeypatch) -> None:
        await run_migration("27")
        self._hold_everyone_at_the_insert(monkeypatch, 4)
        credentials = [f'{{"t":{i}}}' for i in range(4)]

        results = await asyncio.gather(
            *(adapter_instance_service.upsert_session(instance(1), c, "", None, f"c-{i}") for i, c in enumerate(credentials)),
            return_exceptions=True,
        )

        failures = [r for r in results if isinstance(r, BaseException)]
        assert not failures, f"并发首次写入不应失败: {failures!r}"
        rows = await DBAdapterInstanceSession.filter(instance_id=1)
        assert len(rows) == 1
        assert rows[0].credentials_json in credentials
        assert rows[0].last_cursor == f"c-{credentials.index(rows[0].credentials_json)}", "同一次调用的几列一起落库"

    async def test_concurrent_writes_on_two_instances_stay_on_their_own_rows(self, pg_env: str, monkeypatch) -> None:
        """跨实例对照：两个实例同时首次写入，各自一行，凭据只来自各自的调用"""
        await run_migration("27")
        self._hold_everyone_at_the_insert(monkeypatch, 4)

        results = await asyncio.gather(
            adapter_instance_service.upsert_session(instance(1), '{"t":"1a"}', "", None),
            adapter_instance_service.upsert_session(instance(2), '{"t":"2a"}', "", None),
            adapter_instance_service.upsert_session(instance(1), '{"t":"1b"}', "", None),
            adapter_instance_service.upsert_session(instance(2), '{"t":"2b"}', "", None),
            return_exceptions=True,
        )

        assert not [r for r in results if isinstance(r, BaseException)], results
        (one,) = await DBAdapterInstanceSession.filter(instance_id=1)
        (two,) = await DBAdapterInstanceSession.filter(instance_id=2)
        assert one.credentials_json in ('{"t":"1a"}', '{"t":"1b"}')
        assert two.credentials_json in ('{"t":"2a"}', '{"t":"2b"}')

    async def test_bind_state_and_credentials_written_together_both_survive(self, pg_env: str, monkeypatch) -> None:
        """还没有会话行时，绑定子状态与凭据同时到：一行，两边写的都在"""
        await run_migration("27")
        self._hold_everyone_at_the_insert(monkeypatch, 2)

        results = await asyncio.gather(
            adapter_instance_service.set_session_state(instance(1), "session_persisted"),
            adapter_instance_service.upsert_session(instance(1), '{"t":1}', "", None, "c-1"),
            return_exceptions=True,
        )

        assert not [r for r in results if isinstance(r, BaseException)], results
        (row,) = await DBAdapterInstanceSession.filter(instance_id=1)
        assert row.session_state == "session_persisted"
        assert row.credentials_json == '{"t":1}'

    @staticmethod
    def _slow_save(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
        """读到行之后、写回之前停一会儿"""
        real_save = DBAdapterInstanceSession.save

        async def slow_save(self: Any, *args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(seconds)
            return await real_save(self, *args, **kwargs)

        monkeypatch.setattr(DBAdapterInstanceSession, "save", slow_save)

    async def test_writers_on_an_existing_row_do_not_overwrite_each_other(self, pg_env: str, monkeypatch) -> None:
        """两个写入口同时改同一行，两边写的都在。

        `update_fields`（只写自己的列）与行锁（后到的一方等先到的提交后再读）各自都足以保证这一条，
        去掉其中一个这里仍然绿；行锁本身由下一条用例单独钉住。
        """
        await run_migration("27")
        await seed_raw("(1, 1, '{\"t\":0}', '', '2026-10-01')")
        self._slow_save(monkeypatch, 0.4)

        await asyncio.gather(
            adapter_instance_service.set_session_state(instance(1), "bound"),
            adapter_instance_service.upsert_session(instance(1), '{"t":1}', "", None, "c-1"),
        )

        (row,) = await DBAdapterInstanceSession.filter(instance_id=1)
        assert row.session_state == "bound"
        assert row.credentials_json == '{"t":1}'
        assert row.last_cursor == "c-1"

    async def test_writers_on_the_same_row_take_turns_under_the_row_lock(self, pg_env: str, monkeypatch) -> None:
        """行锁：后到的一方要等先到的一方提交之后才读得到行，两段「读到之后停 0.5 秒」只能先后发生。

        没有 `select_for_update` 时，两边同时读到行、同时停，只在最后的 UPDATE 上短暂排队，总时长约 0.5 秒。
        """
        import time

        await run_migration("27")
        await seed_raw("(1, 1, '{\"t\":0}', '', '2026-10-01')")
        self._slow_save(monkeypatch, 0.5)

        started = time.monotonic()
        await asyncio.gather(
            adapter_instance_service.set_session_state(instance(1), "bound"),
            adapter_instance_service.upsert_session(instance(1), '{"t":1}', "", None, "c-1"),
        )

        assert time.monotonic() - started >= 0.9, "两次加锁读改写没有串行：行锁没起作用"

    async def test_ensure_row_absorbs_a_real_unique_violation(self, pg_env: str, monkeypatch) -> None:
        """asyncpg 抛的唯一约束错误要被翻成 Tortoise 的 `IntegrityError`，`_ensure_session_row` 才吞得住"""
        await run_migration("27")
        await DBAdapterInstanceSession.create(instance_id=1, credentials_json='{"t":0}')
        raised: List[BaseException] = []
        real_create = DBAdapterInstanceSession.create

        async def not_yet(**kwargs: Any) -> bool:
            return False

        async def spy_create(**kwargs: Any) -> Any:
            try:
                return await real_create(**kwargs)
            except BaseException as e:
                raised.append(e)
                raise

        monkeypatch.setattr(DBAdapterInstanceSession, "exists", not_yet)
        monkeypatch.setattr(DBAdapterInstanceSession, "create", spy_create)

        await AdapterInstanceService._ensure_session_row(1)

        assert len(raised) == 1 and isinstance(raised[0], IntegrityError), raised
        (row,) = await DBAdapterInstanceSession.filter(instance_id=1)
        assert row.credentials_json == '{"t":0}'
