"""自助申报 pending 闸门的并发安全（迁移 26）与审批互斥

## 根因

`submit_alias_request` 的两道闸门（一人一域一张 pending、一个账号一域只被一张 pending 指着）
原先是「事务内先查后建」。Postgres 默认 READ COMMITTED，两个并发事务可以**各自查到没有**
然后**各自建出**——闸门被穿过，同一个账号会同时挂着两张 pending。
`models/db_identity.py` 的旧注释说 Tortoise 表达不了部分唯一约束，但迁移 24 已经示范过
手写部分唯一索引：这里同样在迁移 26 里补两条 `WHERE status = 'pending'` 的部分唯一索引，
闸门的**最终裁决权交给数据库**，服务层的先查后建只负责给出友好文案。

## 本文件钉什么

1. 迁移 26 的静态契约（两条部分唯一索引、回滚、模型状态不变）——常绿；
2. 服务层把 `IntegrityError` 翻成 `AliasRequestError`（SQLite 里用「赢家先落库」伪造那个窗口）、
   `claimed` 长度校验（列是 256，超长不能变成数据库错误）、
   批准/拒绝读单时加行锁（`select_for_update`，SQLite 下是空操作但必须能跑）；
3. **真 Postgres**（`NA_TEST_PG_DSN` 开门）：迁移 26 对存量重复行的去重、索引语义（只约束 pending）、
   回滚与重复执行、**用屏障逼出真并发**后恰好一个胜出、批准与拒绝并发时恰好一个生效。
"""

import asyncio
import base64
import importlib.util
import json
import os
import uuid
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any, List
from urllib.parse import urlencode

import pytest
from tortoise import Tortoise, connections
from tortoise.backends.base.executor import EXECUTOR_CACHE
from tortoise.exceptions import IntegrityError
from tortoise.queryset import QuerySet

from nekro_agent.models.db_identity import (
    ALIAS_REQUEST_APPROVED,
    ALIAS_REQUEST_CANCELED,
    ALIAS_REQUEST_PENDING,
    ALIAS_REQUEST_REJECTED,
    ALIAS_REQUEST_USERID_MAX_LENGTH,
    DBIdentityAliasRequest,
)
from nekro_agent.services import identity_requests
from nekro_agent.services.affection.merge import MergeResult
from nekro_agent.services.identity_requests import (
    AliasRequestError,
    approve_alias_request,
    cancel_alias_request,
    get_request_in_domain,
    reject_alias_request,
    submit_alias_request,
)
from nekro_agent.services.identity_scope import ScopeDomain

PG_DSN_ENV = "NA_TEST_PG_DSN"
MIGRATION_DIR = Path(__file__).parent.parent / "migrations" / "models"

DOMAIN = ScopeDomain.of_group(1)
OTHER_DOMAIN = ScopeDomain.of_group(2)
CHAT_KEY = "onebot_v11-inst1-group_777"
MAX_USERID = 256


# ============================================================
# 公共夹具与小工具
# ============================================================


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def _no_real_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    """通知走适配器，测试环境里没有；它本来就是尽力而为，这里直接换成空操作"""

    async def quiet(request: Any, message: str) -> None:
        return None

    monkeypatch.setattr(identity_requests, "_notify_reporter", quiet)


async def submit(reporter: str, claimed: str, domain: ScopeDomain = DOMAIN) -> DBIdentityAliasRequest:
    return await submit_alias_request(
        domain,
        reporter_userid=reporter,
        reporter_chat_key=CHAT_KEY,
        claimed_userid=claimed,
    )


async def make_pending(reporter: str, claimed: str, domain: ScopeDomain = DOMAIN) -> DBIdentityAliasRequest:
    """绕过服务层直接落一张单子（用来摆现场，不经过闸门）"""
    return await DBIdentityAliasRequest.create(
        domain_id=domain.domain_id,
        alias_platform_userid=claimed,
        reporter_userid=reporter,
        reporter_person_uid=reporter,
        reporter_chat_key=CHAT_KEY,
    )


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


# ============================================================
# 一、迁移 26 的静态契约
# ============================================================


class TestMigration26Contract:
    async def test_creates_two_partial_unique_indexes_on_pending_only(self) -> None:
        sql = await load_migration("26").upgrade(db=None)

        for columns in ('"domain_id", "reporter_userid"', '"domain_id", "alias_platform_userid"'):
            statement = next(
                (s for s in sql.split(";") if "CREATE UNIQUE INDEX" in s and columns in s),
                None,
            )
            assert statement is not None, f"缺少 ({columns}) 上的唯一索引"
            # 部分索引：只约束 pending，否则已审结的历史单会把同一对账号永远锁死
            assert "WHERE" in statement and "'pending'" in statement
            assert "IF NOT EXISTS" in statement, "迁移要能重复执行"

    async def test_dedupes_existing_pending_duplicates_before_indexing_without_deleting(self) -> None:
        sql = await load_migration("26").upgrade(db=None)

        first_index = sql.index("CREATE UNIQUE INDEX")
        assert sql.index("UPDATE") < first_index, "不先清存量重复行，建索引当场失败"
        assert "DELETE" not in sql, "申报单是审计留痕，去重只改状态不删行"
        assert "'canceled'" in sql

    async def test_downgrade_drops_both_indexes(self) -> None:
        sql = await load_migration("26").downgrade(db=None)

        assert sql.count("DROP INDEX IF EXISTS") == 2

    def test_models_state_is_unchanged_from_25(self) -> None:
        """部分唯一索引 Tortoise 模型表达不了，所以本迁移不改模型：状态必须与 25 逐项相同
        （同时是「手写迁移没写歪」的护栏——state 错了 aerich 下次 diff 就乱）"""
        assert decoded_state(load_migration("26")) == decoded_state(load_migration("25"))


# ============================================================
# 二、服务层（SQLite 即可）
# ============================================================


class TestIntegrityErrorBecomesAliasRequestError:
    """部分唯一索引兜底那一路：并发下输掉的一方得到的是一句人话，不是数据库错误"""

    async def _lose_the_race(self, monkeypatch: pytest.MonkeyPatch, *, winner_reporter: str, winner_claimed: str) -> None:
        """伪造输掉竞争的那个交错：我的 create 撞了索引，而赢家的单子在我回读时已经提交

        真库上赢家是**另一个事务**，它的行不随我这个事务回滚；SQLite 单连接里做不到这一点
        （赢家若在我的事务里落库，会随我一起回滚），所以赢家在「我已回滚、开始回读」的那一刻落库——
        与真库上的可见性顺序等价。真正的交错由下面的真 Postgres 用例（屏障）证明。
        """

        async def hit_the_index(*args: Any, **kwargs: Any) -> Any:
            raise IntegrityError("duplicate key value violates unique constraint")

        real_explain = identity_requests._explain_gate_conflict
        real_create = DBIdentityAliasRequest.create

        async def winner_is_visible_by_now(*args: Any, **kwargs: Any) -> Any:
            await real_create(
                domain_id=DOMAIN.domain_id,
                alias_platform_userid=winner_claimed,
                reporter_userid=winner_reporter,
                reporter_person_uid=winner_reporter,
                reporter_chat_key=CHAT_KEY,
            )
            return await real_explain(*args, **kwargs)

        monkeypatch.setattr(DBIdentityAliasRequest, "create", hit_the_index)
        monkeypatch.setattr(identity_requests, "_explain_gate_conflict", winner_is_visible_by_now)

    async def test_same_reporter_lost_race_reports_the_existing_pending(self, db, monkeypatch) -> None:
        await self._lose_the_race(monkeypatch, winner_reporter="r1", winner_claimed="old-1")

        with pytest.raises(AliasRequestError, match="待审批的申报"):
            await submit("r1", "old-2")

        assert await DBIdentityAliasRequest.filter(status=ALIAS_REQUEST_PENDING).count() == 1

    async def test_same_claimed_lost_race_names_the_claimed_account(self, db, monkeypatch) -> None:
        await self._lose_the_race(monkeypatch, winner_reporter="r-winner", winner_claimed="old-1")

        with pytest.raises(AliasRequestError) as info:
            await submit("r-loser", "old-1")

        assert "old-1" in str(info.value) and "已经有一张待审批" in str(info.value)

    async def test_integrity_error_with_no_pending_to_blame_is_still_a_clean_error(self, db, monkeypatch) -> None:
        """回读找不到任何 pending（约束违例不是闸门造成的）也不能把 IntegrityError 冲出去"""

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise IntegrityError("some other constraint")

        monkeypatch.setattr(DBIdentityAliasRequest, "create", boom)

        with pytest.raises(AliasRequestError):
            await submit("r1", "old-1")

    async def test_no_race_no_false_positive(self, db) -> None:
        """反向对照：正常提交不受影响"""
        request = await submit("r1", "old-1")

        assert request.status == ALIAS_REQUEST_PENDING


class TestAccountIdLengthIsValidatedBeforeTheDatabase:
    """`alias_platform_userid` / `reporter_userid` 列是 256：超长要变成 `AliasRequestError`，
    不能到数据库里才炸成一个 500 级的错误（Postgres 上是 StringDataRightTruncation）"""

    async def test_overlong_claimed_is_rejected(self, db) -> None:
        with pytest.raises(AliasRequestError, match="过长"):
            await submit("r1", "x" * (MAX_USERID + 1))

        assert await DBIdentityAliasRequest.all().count() == 0

    async def test_claimed_of_exactly_the_column_width_is_accepted(self, db) -> None:
        request = await submit("r1", "x" * MAX_USERID)

        assert len(request.alias_platform_userid) == MAX_USERID

    async def test_overlong_reporter_is_rejected(self, db) -> None:
        with pytest.raises(AliasRequestError, match="过长"):
            await submit("r" * (MAX_USERID + 1), "old-1")

    async def test_length_is_measured_after_stripping(self, db) -> None:
        """空白在入库前就被去掉了，按去掉之后的长度算"""
        request = await submit("r1", " " + "x" * MAX_USERID + " ")

        assert len(request.alias_platform_userid) == MAX_USERID

    async def test_length_limit_tracks_the_model_column(self) -> None:
        """限值不是另写一份：取自模型列宽，列宽变了校验自动跟着变"""
        fields = DBIdentityAliasRequest._meta.fields_map
        assert ALIAS_REQUEST_USERID_MAX_LENGTH == MAX_USERID
        assert fields["alias_platform_userid"].max_length == ALIAS_REQUEST_USERID_MAX_LENGTH  # type: ignore[attr-defined]
        assert fields["reporter_userid"].max_length == ALIAS_REQUEST_USERID_MAX_LENGTH  # type: ignore[attr-defined]


class TestReviewTakesTheRowLock:
    """批准与拒绝读单时加 `select_for_update`，防止两条审批互相覆盖

    SQLite 没有行锁（Tortoise 在该方言下忽略它），所以这里只能证明「调用点在、且能跑」；
    真正的互斥由下面的真 Postgres 用例证明。
    """

    @pytest.fixture
    def lock_calls(self, monkeypatch: pytest.MonkeyPatch) -> List[str]:
        calls: List[str] = []
        original = QuerySet.select_for_update

        def spy(self: QuerySet, *args: Any, **kwargs: Any) -> Any:
            calls.append(self.model.__name__)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(QuerySet, "select_for_update", spy)
        return calls

    async def test_approve_locks_the_request_row(self, db, lock_calls, monkeypatch) -> None:
        request = await submit("r1", "old-1")
        monkeypatch.setattr(identity_requests, "merge_accounts", _stub_merge)

        await approve_alias_request(DOMAIN, request.id, operator="ops")

        assert lock_calls == ["DBIdentityAliasRequest"]

    async def test_reject_locks_the_request_row(self, db, lock_calls) -> None:
        request = await submit("r1", "old-1")

        await reject_alias_request(DOMAIN, request.id, operator="ops", note="不认识")

        assert lock_calls == ["DBIdentityAliasRequest"]

    async def test_cancel_locks_the_request_row(self, db, lock_calls) -> None:
        """撤回与批准/拒绝用同一把行锁"""
        await submit("r1", "old-1")

        canceled = await cancel_alias_request(DOMAIN, "r1")

        assert lock_calls == ["DBIdentityAliasRequest"]
        assert canceled.status == ALIAS_REQUEST_CANCELED
        assert (await DBIdentityAliasRequest.get(id=canceled.id)).status == ALIAS_REQUEST_CANCELED

    async def test_cancel_never_overwrites_a_request_that_stopped_being_pending(self, db, monkeypatch) -> None:
        """锁之外的第二道：写入带 `status=pending` 条件。

        SQLite 没有行锁，这里在「撤回读到 pending」与「撤回写入」之间让批准抢先落地，
        撤回必须认输，而不是把 approved 覆盖成 canceled。
        """
        request = await submit("r1", "old-1")
        original = QuerySet.update

        def approve_first(self: QuerySet, **kwargs: Any) -> Any:
            async def run() -> Any:
                if self.model is DBIdentityAliasRequest and kwargs.get("status") == ALIAS_REQUEST_CANCELED:
                    await original(DBIdentityAliasRequest.filter(id=request.id), status=ALIAS_REQUEST_APPROVED)
                return await original(self, **kwargs)

            return run()

        monkeypatch.setattr(QuerySet, "update", approve_first)

        with pytest.raises(AliasRequestError, match="没有待审批"):
            await cancel_alias_request(DOMAIN, "r1")

        # SQLite 单连接：撤回认输抛错时整个事务连同「抢先的批准」一起回滚，所以不断言 approved，
        # 只断言撤回没有写下 canceled（真并发下批准是另一条连接已提交的，见下面的真 PG 用例）
        assert (await DBIdentityAliasRequest.get(id=request.id)).status != ALIAS_REQUEST_CANCELED

    async def test_plain_lookup_does_not_lock(self, db, lock_calls) -> None:
        """后台列表/详情走的读路径不该白白占锁"""
        request = await submit("r1", "old-1")

        await get_request_in_domain(DOMAIN, request.id)

        assert lock_calls == []

    async def test_second_review_of_the_same_request_is_refused(self, db, monkeypatch) -> None:
        request = await submit("r1", "old-1")
        monkeypatch.setattr(identity_requests, "merge_accounts", _stub_merge)
        await approve_alias_request(DOMAIN, request.id, operator="ops")

        with pytest.raises(AliasRequestError, match="已经审过了"):
            await reject_alias_request(DOMAIN, request.id, operator="ops2")

        assert (await DBIdentityAliasRequest.get(id=request.id)).status == ALIAS_REQUEST_APPROVED


class TestApprovalInvalidatesTheAffectionCacheAfterCommit:
    """批准的补偿经 `apply_delta` 在事务里写入，缓存失效必须有一次发生在提交**之后**

    只在提交之前失效：并发读者在失效与提交之间读到旧分数并重新缓存，补进去的好感在 TTL 内读不到。
    事务内 `apply_delta` 自带的那次失效无害（被提交后的那次覆盖），要钉住的是「最后一次在提交之后」。
    用记录器包住两处 `invalidate_cache`，在调用瞬间看当前连接是不是事务连接。
    """

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> List[bool]:
        from tortoise.backends.base.client import TransactionalDBClient

        from nekro_agent.services.affection import store

        seen: List[bool] = []
        real = store.invalidate_cache

        def record(*args: Any, **kwargs: Any) -> None:
            seen.append(isinstance(connections.get("default"), TransactionalDBClient))
            real(*args, **kwargs)

        monkeypatch.setattr(store, "invalidate_cache", record)
        monkeypatch.setattr(identity_requests, "invalidate_cache", record)
        return seen

    @staticmethod
    async def _seed_borrowable_pair() -> None:
        from nekro_agent.models.db_affection import DBAffection

        for uid, score in (("r1", 20), ("old-1", 70)):
            await DBAffection.create(
                **DOMAIN.as_columns(),
                platform_userid=uid,
                preset_id=0,
                nickname=uid,
                score=score,
                stage="stranger",
            )

    async def test_the_last_invalidation_happens_after_the_transaction_commits(self, db, calls: List[bool]) -> None:
        await self._seed_borrowable_pair()
        request = await submit("r1", "old-1")

        _, result = await approve_alias_request(DOMAIN, request.id, operator="ops")

        assert result.total_delta == 50, "前置对照：补偿真的落了地"
        assert calls, "批准兑现了补偿却没有失效缓存"
        assert calls[-1] is False, "最后一次缓存失效仍在事务内，提交前的窗口里读者会把旧分重新缓存"

    async def test_the_cached_person_snapshot_is_dropped_after_the_approval(self, db) -> None:
        """端到端：批准前缓存着的「人」的旧分数，批准之后读到的是补偿后的新分数"""
        from nekro_agent.services.affection.key import AffectionKey
        from nekro_agent.services.affection.store import load_snapshot

        await self._seed_borrowable_pair()
        key = AffectionKey.of_domain(DOMAIN, person_uid="r1", preset_id=0)
        assert (await load_snapshot(key, 3600)).score == 20  # 把旧值放进缓存
        request = await submit("r1", "old-1")

        await approve_alias_request(DOMAIN, request.id, operator="ops")

        assert (await load_snapshot(key, 3600)).score == 70

    async def test_rejection_does_not_touch_the_cache(self, db, calls: List[bool]) -> None:
        """反向对照：拒绝不动分数，也就没有失效的理由"""
        await self._seed_borrowable_pair()
        request = await submit("r1", "old-1")

        await reject_alias_request(DOMAIN, request.id, operator="ops", note="不认识")

        assert calls == []

    async def test_a_failed_approval_does_not_invalidate_either(self, db, calls: List[bool]) -> None:
        """批准在合并校验处失败时事务整体回滚，没有任何提交，也就没有失效"""
        await self._seed_borrowable_pair()
        request = await submit("r1", "old-1")
        await reject_alias_request(DOMAIN, request.id, operator="ops")
        calls.clear()

        with pytest.raises(AliasRequestError):
            await approve_alias_request(DOMAIN, request.id, operator="ops")

        assert calls == []


async def _stub_merge(domain: ScopeDomain, **kwargs: Any) -> MergeResult:
    return MergeResult(
        domain_id=domain.domain_id,
        person_uid=kwargs["person_uid"],
        alias_platform_userid=kwargs["alias_platform_userid"],
        transfers=[],
        rewritten_aliases=[],
    )


# ============================================================
# 三、真 Postgres
# ============================================================


def _pg_dsn_or_skip() -> str:
    dsn = os.environ.get(PG_DSN_ENV, "")
    if not dsn:
        pytest.skip(
            f"需要真实 PostgreSQL：设 {PG_DSN_ENV}（如 postgres://postgres@127.0.0.1:55499/natest）。"
            "静态契约与 SQLite 侧用例常绿把关；CI 的 backend-tests 工作流会设它。",
        )
    return dsn


@pytest.fixture
async def pg_env():
    """独占 schema 里的 `identity_*` 三张表（由 Tortoise 按模型建，**不含**迁移 26 的索引）

    索引由各用例按需 `apply_migration_26()` 铺——有的用例要先摆出「铺索引之前的存量重复行」。
    EXECUTOR_CACHE 进出两端都清（进程级、不含方言）。
    """
    dsn = _pg_dsn_or_skip()
    import asyncpg

    schema = f"na_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn=dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    EXECUTOR_CACHE.clear()
    await Tortoise.init(
        db_url=f"{dsn}{sep}{urlencode({'schema': schema})}",
        modules={"models": ["nekro_agent.models.db_identity"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield schema
    finally:
        await Tortoise.close_connections()
        EXECUTOR_CACHE.clear()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def apply_migration_26() -> None:
    await connections.get("default").execute_script(await load_migration("26").upgrade(db=None))


async def revert_migration_26() -> None:
    await connections.get("default").execute_script(await load_migration("26").downgrade(db=None))


async def gate_indexes(schema: str) -> List[str]:
    rows = await connections.get("default").execute_query_dict(
        "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND indexname LIKE 'uid_alias_req_pending_%' "
        "ORDER BY indexname",
        [schema],
    )
    return [row["indexname"] for row in rows]


class TestMigration26OnRealPostgres:
    async def test_it_really_is_postgres_in_an_isolated_schema(self, pg_env: str) -> None:
        rows = await connections.get("default").execute_query_dict(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'identity_alias_request'",
        )
        assert [row["table_schema"] for row in rows] == [pg_env]

    async def test_upgrade_dedupes_pending_duplicates_keeping_the_earliest(self, pg_env: str) -> None:
        # 铺索引之前竞态写出来的现场：同一申报人两张、同一账号两张，另有无关单子
        same_reporter_first = await make_pending("r1", "a1")
        same_reporter_second = await make_pending("r1", "a2")
        same_claimed_first = await make_pending("r2", "a3")
        same_claimed_second = await make_pending("r3", "a3")
        other_domain = await make_pending("r1", "a1", OTHER_DOMAIN)
        history = await make_pending("r4", "a4")
        history.status = ALIAS_REQUEST_APPROVED
        await history.save()
        another_pending_on_history = await make_pending("r5", "a4")

        await apply_migration_26()

        async def status_of(request: DBIdentityAliasRequest) -> str:
            return (await DBIdentityAliasRequest.get(id=request.id)).status

        assert await status_of(same_reporter_first) == ALIAS_REQUEST_PENDING
        assert await status_of(same_reporter_second) == ALIAS_REQUEST_CANCELED
        assert await status_of(same_claimed_first) == ALIAS_REQUEST_PENDING
        assert await status_of(same_claimed_second) == ALIAS_REQUEST_CANCELED
        # 不相干的单子一张没动：另一个域、已审结的历史单、只与历史单同账号的 pending
        assert await status_of(other_domain) == ALIAS_REQUEST_PENDING
        assert await status_of(history) == ALIAS_REQUEST_APPROVED
        assert await status_of(another_pending_on_history) == ALIAS_REQUEST_PENDING
        # 去重留痕不删行
        assert await DBIdentityAliasRequest.all().count() == 7
        assert "迁移26去重" in (await DBIdentityAliasRequest.get(id=same_reporter_second.id)).review_note
        assert await gate_indexes(pg_env) == ["uid_alias_req_pending_claimed", "uid_alias_req_pending_reporter"]

    async def test_upgrade_and_downgrade_can_run_twice(self, pg_env: str) -> None:
        await apply_migration_26()
        await apply_migration_26()
        assert len(await gate_indexes(pg_env)) == 2

        await revert_migration_26()
        await revert_migration_26()
        assert await gate_indexes(pg_env) == []

    async def test_indexes_only_constrain_pending_rows(self, pg_env: str) -> None:
        await apply_migration_26()
        first = await make_pending("r1", "a1")

        # 同一申报人 / 同一账号再来一张 pending：数据库直接拒
        with pytest.raises(IntegrityError):
            await make_pending("r1", "a2")
        with pytest.raises(IntegrityError):
            await make_pending("r9", "a1")

        # 反向对照：别的域、以及原单审结之后，同一对账号可以重新申报
        await make_pending("r1", "a1", OTHER_DOMAIN)
        first.status = ALIAS_REQUEST_REJECTED
        await first.save()
        again = await make_pending("r1", "a1")
        assert again.status == ALIAS_REQUEST_PENDING


class TestGateHoldsUnderRealConcurrency:
    """用屏障把先查后建的窗口撑开：所有并发提交都过了存在性检查、同时去插入，裁决只剩数据库"""

    @staticmethod
    def _hold_everyone_at_the_insert(monkeypatch: pytest.MonkeyPatch, parties: int) -> None:
        barrier = asyncio.Barrier(parties)
        real_create = DBIdentityAliasRequest.create

        async def create_together(*args: Any, **kwargs: Any) -> Any:
            await asyncio.wait_for(barrier.wait(), timeout=15)
            return await real_create(*args, **kwargs)

        monkeypatch.setattr(DBIdentityAliasRequest, "create", create_together)

    async def test_same_reporter_different_accounts_only_one_wins(self, pg_env: str, monkeypatch) -> None:
        await apply_migration_26()
        self._hold_everyone_at_the_insert(monkeypatch, 3)

        results = await asyncio.gather(
            submit("r1", "old-1"),
            submit("r1", "old-2"),
            submit("r1", "old-3"),
            return_exceptions=True,
        )

        winners = [r for r in results if isinstance(r, DBIdentityAliasRequest)]
        losers = [r for r in results if isinstance(r, AliasRequestError)]
        assert len(winners) == 1, f"期望恰好一个胜出，实际 {results}"
        assert len(losers) == 2, f"输家必须是 AliasRequestError 而不是数据库错误，实际 {results}"
        assert await DBIdentityAliasRequest.filter(status=ALIAS_REQUEST_PENDING).count() == 1

    async def test_different_reporters_same_account_only_one_wins(self, pg_env: str, monkeypatch) -> None:
        await apply_migration_26()
        self._hold_everyone_at_the_insert(monkeypatch, 3)

        results = await asyncio.gather(
            submit("r1", "old-1"),
            submit("r2", "old-1"),
            submit("r3", "old-1"),
            return_exceptions=True,
        )

        winners = [r for r in results if isinstance(r, DBIdentityAliasRequest)]
        losers = [r for r in results if isinstance(r, AliasRequestError)]
        assert len(winners) == 1, f"期望恰好一个胜出，实际 {results}"
        assert len(losers) == 2, f"输家必须是 AliasRequestError 而不是数据库错误，实际 {results}"
        assert all("old-1" in str(loser) for loser in losers)
        assert await DBIdentityAliasRequest.filter(status=ALIAS_REQUEST_PENDING).count() == 1

    async def test_independent_submissions_all_succeed(self, pg_env: str, monkeypatch) -> None:
        """反向对照：互不相干的并发提交一个都不该被误伤"""
        await apply_migration_26()
        self._hold_everyone_at_the_insert(monkeypatch, 3)

        results = await asyncio.gather(
            submit("r1", "old-1"),
            submit("r2", "old-2"),
            submit("r3", "old-3"),
            return_exceptions=True,
        )

        assert all(isinstance(r, DBIdentityAliasRequest) for r in results), results

    async def test_overlong_claimed_never_reaches_postgres(self, pg_env: str) -> None:
        await apply_migration_26()

        with pytest.raises(AliasRequestError):
            await submit("r1", "x" * (MAX_USERID + 1))


class TestConcurrentReviewsDoNotOverwriteEachOther:
    """批准与拒绝同时到：没有行锁时两边都读到 pending、都写成功，后写的覆盖先写的——
    申报人收到「已通过」的通知，库里却是 rejected（或反过来）"""

    async def test_approve_and_reject_race_has_exactly_one_effective_outcome(self, pg_env: str, monkeypatch) -> None:
        await apply_migration_26()
        request = await submit("r1", "old-1")

        approve_inside = asyncio.Event()

        async def slow_merge(domain: ScopeDomain, **kwargs: Any) -> MergeResult:
            # 批准方已经读过单、通过了 pending 检查，正在做合并——把窗口撑开 0.5 秒
            approve_inside.set()
            await asyncio.sleep(0.5)
            return await _stub_merge(domain, **kwargs)

        monkeypatch.setattr(identity_requests, "merge_accounts", slow_merge)

        async def reject_when_approve_is_midway() -> DBIdentityAliasRequest:
            await asyncio.wait_for(approve_inside.wait(), timeout=15)
            return await reject_alias_request(DOMAIN, request.id, operator="ops-b", note="不认识")

        approve_result, reject_result = await asyncio.gather(
            approve_alias_request(DOMAIN, request.id, operator="ops-a"),
            reject_when_approve_is_midway(),
            return_exceptions=True,
        )

        approve_ok = not isinstance(approve_result, BaseException)
        reject_ok = not isinstance(reject_result, BaseException)
        assert approve_ok != reject_ok, f"两个审批不能同时成功: approve={approve_result!r}, reject={reject_result!r}"
        loser = reject_result if approve_ok else approve_result
        assert isinstance(loser, AliasRequestError) and "已经审过了" in str(loser)

        final = await DBIdentityAliasRequest.get(id=request.id)
        assert final.status == (ALIAS_REQUEST_APPROVED if approve_ok else ALIAS_REQUEST_REJECTED)
        assert final.reviewed_by == ("ops-a" if approve_ok else "ops-b")

    async def test_approve_and_cancel_race_never_turns_approved_into_canceled(self, pg_env: str, monkeypatch) -> None:
        """撤回与批准并发：撤回无锁时先读到 pending，写入阻塞在批准持有的行锁上，
        批准提交之后撤回把 approved 覆盖成 canceled——别名已建、补偿已记，单子却显示申报人撤回了"""
        await apply_migration_26()
        request = await submit("r1", "old-1")

        approve_inside = asyncio.Event()

        async def slow_merge(domain: ScopeDomain, **kwargs: Any) -> MergeResult:
            approve_inside.set()
            await asyncio.sleep(0.5)
            return await _stub_merge(domain, **kwargs)

        monkeypatch.setattr(identity_requests, "merge_accounts", slow_merge)

        async def cancel_when_approve_is_midway() -> DBIdentityAliasRequest:
            await asyncio.wait_for(approve_inside.wait(), timeout=15)
            return await cancel_alias_request(DOMAIN, "r1")

        approve_result, cancel_result = await asyncio.gather(
            approve_alias_request(DOMAIN, request.id, operator="ops-a"),
            cancel_when_approve_is_midway(),
            return_exceptions=True,
        )

        assert not isinstance(approve_result, BaseException), f"批准持锁在先，必须成功: {approve_result!r}"
        assert isinstance(cancel_result, AliasRequestError), f"撤回必须输给已提交的批准: {cancel_result!r}"
        final = await DBIdentityAliasRequest.get(id=request.id)
        assert final.status == ALIAS_REQUEST_APPROVED
        assert final.reviewed_by == "ops-a"
