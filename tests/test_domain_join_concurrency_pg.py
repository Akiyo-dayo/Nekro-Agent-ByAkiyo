"""入域并入折叠与同组并发写入的互斥（round-27 第二轮复核 P2-1），真 Postgres

`merge_self_domain_into_group` 折叠时要拿目标行当前的分数算差额。目标行若无锁读取，
同组另一个实例此刻也在并入（或评估正在落分）时，两边各按旧分算差额、再各自叠加，
结果既不是「A 先并入、B 后并入」也不是反过来：目标 50、A 的源分 80、B 的源分 70，
无锁时两边各算 +30 / +20，落成 100；任何串行次序的答案都是 80。

SQLite 没有行锁（Tortoise 在该方言下忽略 `select_for_update`），所以互斥只能在真 PG 上证明：
设 `NA_TEST_PG_DSN`（如 `postgres://postgres@127.0.0.1:55499/natest`）开门，未设则 skip；
调用点在、且 SQLite 上能跑，由 `test_domain_join_migration.py::TestFoldReadsTargetRowsUnderLock` 守。
CI 的 backend-tests 工作流会设该变量。
"""

import asyncio
import os
import uuid
from types import SimpleNamespace
from typing import Any, List
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise
from tortoise.backends.base.executor import EXECUTOR_CACHE

from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.services import identity_admin
from nekro_agent.services.affection.stage import AffectionEventSource, stage_of
from nekro_agent.services.identity_admin import merge_self_domain_into_group
from nekro_agent.services.identity_scope import ScopeDomain

PG_DSN_ENV = "NA_TEST_PG_DSN"
ADAPTER = "onebot_v11"
GROUP_ID = 7


def _pg_dsn_or_skip() -> str:
    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(
            f"需要真实 PostgreSQL：设 {PG_DSN_ENV}（如 postgres://postgres@127.0.0.1:55499/natest）。"
            "SQLite 侧的调用点用例常绿把关；CI 的 backend-tests 工作流会设它。",
        )
    return dsn


@pytest.fixture
async def pg_env():
    """独占 schema 里的好感与身份表；EXECUTOR_CACHE 进出两端都清（进程级、不含方言，见 PROJECT_STATE §2.43）"""
    dsn = _pg_dsn_or_skip()
    import asyncpg

    schema = f"na_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    EXECUTOR_CACHE.clear()
    await Tortoise.init(
        db_url=f"{dsn}{sep}{urlencode({'schema': schema})}",
        modules={"models": ["nekro_agent.models.db_affection", "nekro_agent.models.db_identity"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield schema
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def seed(domain: ScopeDomain, *, uid: str, score: int) -> None:
    await DBAffection.create(
        **domain.as_columns(),
        platform_userid=uid,
        preset_id=0,
        nickname=f"nick-{uid}",
        score=score,
        stage=stage_of(score).value,
    )


async def join(instance_key: str) -> Any:
    group: Any = SimpleNamespace(id=GROUP_ID, name="g")
    return await merge_self_domain_into_group(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        group=group,
        operator="ops",
    )


def widen_the_window_before_the_score_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """读完目标行、算完差额之后，写分之前停 0.6 秒：另一条并入若不被行锁挡住，就在这个窗口里读到旧分"""
    real = identity_admin.apply_delta

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.6)
        return await real(*args, **kwargs)

    monkeypatch.setattr(identity_admin, "apply_delta", slow)


async def group_score(uid: str) -> int:
    row = await DBAffection.get(**ScopeDomain.of_group(GROUP_ID).as_columns(), platform_userid=uid, preset_id=0)
    return row.score


class TestConcurrentJoinsFoldAgainstTheCommittedScore:
    async def test_two_instances_folding_into_the_same_row_land_on_the_serial_answer(
        self,
        pg_env: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """目标 P=50，实例 a 的源分 80、实例 b 的源分 70：两种串行次序都是 80，不能是 100"""
        await seed(ScopeDomain.of_group(GROUP_ID), uid="P", score=50)
        await seed(ScopeDomain.of_instance(ADAPTER, "a"), uid="P", score=80)
        await seed(ScopeDomain.of_instance(ADAPTER, "b"), uid="P", score=70)
        widen_the_window_before_the_score_is_written(monkeypatch)

        results = await asyncio.gather(join("a"), join("b"), return_exceptions=True)

        failures = [r for r in results if isinstance(r, BaseException)]
        assert not failures, f"并发并入不应失败: {failures!r}"
        assert await group_score("P") == 80, "两条并入各按旧分 50 算差额再叠加：分数被重复计入"
        assert await DBAffection.filter(**ScopeDomain.of_group(GROUP_ID).as_columns()).count() == 1

    async def test_compensation_events_add_up_to_the_final_score(self, pg_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """账目对得上：初始分 + 全部入域补偿事件的增量 == 最终分"""
        await seed(ScopeDomain.of_group(GROUP_ID), uid="P", score=50)
        await seed(ScopeDomain.of_instance(ADAPTER, "a"), uid="P", score=80)
        await seed(ScopeDomain.of_instance(ADAPTER, "b"), uid="P", score=70)
        widen_the_window_before_the_score_is_written(monkeypatch)

        await asyncio.gather(join("a"), join("b"))

        deltas: List[int] = list(
            await DBAffectionEvent.filter(source=AffectionEventSource.DOMAIN_MERGE.value).values_list("delta", flat=True),
        )
        assert 50 + sum(deltas) == await group_score("P")

    async def test_different_people_in_the_same_group_both_fold_correctly(
        self,
        pg_env: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """反向对照：两个实例并入的是不同的人。同组并入现在整体串行（事务开头的咨询锁），结果仍各自正确"""
        await seed(ScopeDomain.of_group(GROUP_ID), uid="P", score=50)
        await seed(ScopeDomain.of_group(GROUP_ID), uid="Q", score=10)
        await seed(ScopeDomain.of_instance(ADAPTER, "a"), uid="P", score=80)
        await seed(ScopeDomain.of_instance(ADAPTER, "b"), uid="Q", score=40)
        widen_the_window_before_the_score_is_written(monkeypatch)

        await asyncio.gather(join("a"), join("b"))

        assert await group_score("P") == 80
        assert await group_score("Q") == 40


def hold_the_group_open_after_the_target_rows_are_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """锁读完目标行之后停 0.6 秒：没有「同组串行」的话，另一条并入此刻同样读到「目标域里还没有这个人」"""
    real = identity_admin._lock_target_rows

    async def slow(*args: Any, **kwargs: Any) -> Any:
        rows = await real(*args, **kwargs)
        await asyncio.sleep(0.6)
        return rows

    monkeypatch.setattr(identity_admin, "_lock_target_rows", slow)


class TestSameGroupJoinsAreSerialisedEvenWhenThereIsNoRowToLock:
    """目标域里还没有这个人时无行可锁（round-27 第三轮复核 P2-2，A5）

    行锁只能锁已存在的行。两个实例同时并入「目标域里还没有」的同一个人，各自读到空、各自把源行改键成目标键，
    后到的撞唯一约束（IntegrityError）；源行顺序相反的两个实例则各占一个目标键、再互相等对方（DeadlockDetectedError）。
    路由只接 ValueError，两者都是 500。修法：事务开头按 group_id 取 `pg_advisory_xact_lock`，同组并入串行。
    """

    async def test_two_instances_joining_the_same_new_person_both_succeed(
        self,
        pg_env: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await seed(ScopeDomain.of_instance(ADAPTER, "a"), uid="P", score=80)
        await seed(ScopeDomain.of_instance(ADAPTER, "b"), uid="P", score=70)
        hold_the_group_open_after_the_target_rows_are_locked(monkeypatch)

        results = await asyncio.gather(join("a"), join("b"), return_exceptions=True)

        failures = [r for r in results if isinstance(r, BaseException)]
        assert not failures, f"并发并入新人不应失败: {failures!r}"
        assert await group_score("P") == 80
        assert await DBAffection.filter(**ScopeDomain.of_group(GROUP_ID).as_columns()).count() == 1

    async def test_instances_with_opposite_row_orders_do_not_deadlock(
        self,
        pg_env: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        a, b = ScopeDomain.of_instance(ADAPTER, "a"), ScopeDomain.of_instance(ADAPTER, "b")
        await seed(a, uid="X", score=10)
        await seed(a, uid="Y", score=20)
        await seed(b, uid="Y", score=30)
        await seed(b, uid="X", score=40)
        real_save = DBAffection.save

        async def slow_save(self: Any, *args: Any, **kwargs: Any) -> Any:
            result = await real_save(self, *args, **kwargs)
            await asyncio.sleep(0.4)  # 第一行搬完、第二行开搬之前让另一边也搬出第一行
            return result

        monkeypatch.setattr(DBAffection, "save", slow_save)

        results = await asyncio.gather(join("a"), join("b"), return_exceptions=True)

        failures = [r for r in results if isinstance(r, BaseException)]
        assert not failures, f"源行顺序相反的并入不应死锁: {failures!r}"
        assert await group_score("X") == 40
        assert await group_score("Y") == 30

    async def test_joins_into_different_groups_are_not_serialised(
        self,
        pg_env: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """反向对照：锁按 group_id 分，不同组互不排队（两个 0.6 秒的窗口并行，总时长远小于 1.2 秒）"""
        import time

        await seed(ScopeDomain.of_instance(ADAPTER, "a"), uid="P", score=80)
        await seed(ScopeDomain.of_instance(ADAPTER, "b"), uid="Q", score=70)
        hold_the_group_open_after_the_target_rows_are_locked(monkeypatch)

        async def join_group(instance_key: str, group_id: int) -> Any:
            group: Any = SimpleNamespace(id=group_id, name="g")
            return await merge_self_domain_into_group(adapter_key=ADAPTER, instance_key=instance_key, group=group)

        started = time.monotonic()
        await asyncio.gather(join_group("a", GROUP_ID), join_group("b", GROUP_ID + 1))

        assert time.monotonic() - started < 1.1, "不同组的并入被串行了"
        assert await group_score("P") == 80


class TestARuntimeWriterRacingTheMoveBecomesAConflictError:
    """串行只管并入与并入；运行时写入（评估落分）在锁读与搬行之间把目标行建出来时，仍会撞唯一约束。
    整笔回滚并翻成清晰的 409（`ConcurrentWriteConflictError`），不是 500。"""

    async def test_unique_violation_from_a_real_postgres_is_translated(
        self,
        pg_env: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import contextvars

        from nekro_agent.schemas.errors import ConcurrentWriteConflictError

        await seed(ScopeDomain.of_instance(ADAPTER, "a"), uid="P", score=80)
        real = identity_admin._lock_target_rows

        async def runtime_writer_wins(*args: Any, **kwargs: Any) -> Any:
            rows = await real(*args, **kwargs)
            # 空上下文里的任务拿的是连接池里的另一条连接：它的提交对并入事务可见，且不在并入事务里
            await asyncio.create_task(
                seed(ScopeDomain.of_group(GROUP_ID), uid="P", score=10),
                context=contextvars.Context(),
            )
            return rows

        monkeypatch.setattr(identity_admin, "_lock_target_rows", runtime_writer_wins)

        with pytest.raises(ConcurrentWriteConflictError):
            await join("a")

        assert await group_score("P") == 10, "目标行是运行时写入者建的，并入回滚后它原样保留"
        assert await DBAffection.filter(**ScopeDomain.of_instance(ADAPTER, "a").as_columns()).count() == 1, "源行没搬走"