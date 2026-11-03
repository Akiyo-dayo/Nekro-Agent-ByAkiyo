"""#54: 改密吊销已签发令牌（口令世代方案）

## 机制

- `DBUser.password_generation`（迁移 25 补列，默认 0）：自助改密
  （`util.user_change_password`）与管理员重置（`user_manager.reset_password`）
  成功时 +1。
- 签发时把**签发那一刻**的世代写进令牌（`auth.build_pwd_gen_claim` 的 `pwd_gen`
  claim，access 与 refresh 都带，见 `util._token_extra_claims`）。
- `deps.get_current_user` 按 `sub` 回查用户行之后**纯内存比对**世代
  （`deps.verify_password_generation`），不一致 → 401 `TokenRevokedError`
  （文案指向「请重新登录」，与 `TokenExpiredError` 同档；前端 axios 拦截器对
  非登录接口的 401 会清空登录态跳回登录页，正是吊销期望的行为）。

## fail-closed 是有意的取舍

令牌缺 `pwd_gen` 声明（存量令牌全部如此）**也拒绝**：部署本版后所有已登录
会话失效、需重新登录。「缺声明就放行」会得到一个对存量令牌不生效的假吊销
（吊销这件事半做比不做更糟），所以宁可采用硬过渡。

## refresh 票的现状（重要）

仓库里**没有**用 refresh 票换新票的端点——`create_refresh_token` 只在两处登录
签发，`JWT_REFRESH_SECRET_KEY` 从未被任何校验路径消费，旧 refresh 票在当前
代码里本来就无处可兑。因此这里对 refresh 的断言落在共同咽喉上：旧 refresh 票
的 payload 过 `verify_password_generation` 必须 401（将来补上换票端点时**必须**
调它，否则 access 侧的吊销被旁路）。若那条端点落地，请把真端点用例补进来。

## 迁移幂等

测试库（测试部署）里已存在上游 `23_20260829235235_add_user_password_generation`
铺出的同名列（仓库无该文件、列在库里），迁移 25 必须 `ADD COLUMN IF NOT EXISTS`
才能在那样的库上重放不炸。静态契约常绿；对真 PG 的双执行用例由
`NA54_TEST_PG_DSN` 显式开门（迁移落盘时已在临时 PG 上实证过，见交付报告）。
"""

import datetime
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from jose import jwt
from tortoise import Tortoise

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.user import password as change_own_password
from nekro_agent.routers.user_manager import reset_password as admin_reset_password
from nekro_agent.schemas.errors import AppError, UnauthorizedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.user import UpdatePassword, UserLogin, UserToken
from nekro_agent.services.user.auth import (
    PWD_GEN_CLAIM,
    create_access_token,
    get_hashed_password,
)
from nekro_agent.services.user.deps import (
    TokenRevokedError,
    get_current_user,
    verify_password_generation,
)
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.util import (
    _token_extra_claims,
    tenant_member_login,
    user_change_password,
    user_login,
)

ACME_TENANT_ID = 1

# 平台方 admin 的口令走环境变量（user_login 对它比对 OsEnv.ADMIN_PASSWORD），
# 用例里用 monkeypatch 注入，避免依赖机器环境
ADMIN_RAW_PASSWORD = "admin-pw-for-test"  # noqa: S105

MIGRATION_DIR = Path(__file__).parent.parent / "migrations" / "models"
PG_DSN_ENV = "NA54_TEST_PG_DSN"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def seed_default_users() -> None:
    """本文件的标准布局：一个租户成员（自助改密主线）+ 平台方 admin（重置主线）"""
    await DBUser.create(
        username="acme_user",
        password=get_hashed_password("acme_user-pw"),  # noqa: S106
        adapter_key="",
        platform_userid="",
        perm_level=Role.User,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=ACME_TENANT_ID,
        tenant_role=TenantRole.TENANT_USER.value,
    )
    await DBUser.create(
        username="admin",
        password=get_hashed_password(ADMIN_RAW_PASSWORD),
        adapter_key="",
        platform_userid="",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


async def member_login(username: str = "acme_user", password: str = "acme_user-pw") -> UserToken:
    """走真实租户成员登录路径签发一对令牌（不手搓 JWT）"""
    return await tenant_member_login(UserLogin(username=username, password=password))


def decode_access(token: str) -> Dict[str, Any]:
    return jwt.decode(token, OsEnv.JWT_SECRET_KEY, algorithms=[OsEnv.ENCRYPT_ALGORITHM])


def decode_refresh(token: str) -> Dict[str, Any]:
    return jwt.decode(token, OsEnv.JWT_REFRESH_SECRET_KEY, algorithms=[OsEnv.ENCRYPT_ALGORITHM])


class _StubConnection:
    """最小 HTTPConnection 替身：get_current_user 只碰 query_params / headers 的 .get"""

    def __init__(self, headers: Optional[Dict[str, str]] = None) -> None:
        self.query_params: Dict[str, str] = {}
        self.headers: Dict[str, str] = headers or {}


def _client() -> Any:
    """挂真实依赖（不 override get_current_user）的最小 app：验的是完整鉴权链"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nekro_agent.core.exception_handlers import register_exception_handlers
    from nekro_agent.routers.user import router as user_router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(user_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


def _bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 签发侧：两种令牌都要带上签发那一刻的世代
# ---------------------------------------------------------------------------


class TestIssuanceCarriesGeneration:
    async def test_both_tokens_carry_pwd_gen(self, db) -> None:  # noqa: ARG002
        await seed_default_users()
        tokens = await member_login()

        assert decode_access(tokens.access_token)[PWD_GEN_CLAIM] == 0
        assert decode_refresh(tokens.refresh_token)[PWD_GEN_CLAIM] == 0

    async def test_claim_reflects_the_row_at_issuance_time(self, db) -> None:  # noqa: ARG002
        """世代不是恒 0：行拨到几，新签的令牌就带几"""
        await seed_default_users()
        await DBUser.filter(username="acme_user").update(password_generation=5)
        tokens = await member_login()

        assert decode_access(tokens.access_token)[PWD_GEN_CLAIM] == 5
        assert decode_refresh(tokens.refresh_token)[PWD_GEN_CLAIM] == 5

    async def test_admin_branch_login_also_carries_the_claim(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        """user_login 的 admin 分支是第二条签发路径，漏带 claim 它发的令牌会全灭"""
        monkeypatch.setattr(OsEnv, "ADMIN_PASSWORD", ADMIN_RAW_PASSWORD, raising=False)
        await seed_default_users()
        tokens = await user_login(UserLogin(username="admin", password=ADMIN_RAW_PASSWORD))

        # admin 的声明是 "<库内世代>.<ADMIN_PASSWORD 的带密钥指纹>"（#54 ②，机制与用例见
        # test_admin_password_rotation_revocation.py）；租户成员仍是裸整数（上面两条用例钉着）
        claim = decode_access(tokens.access_token)[PWD_GEN_CLAIM]
        assert isinstance(claim, str) and claim.startswith("0."), claim


# ---------------------------------------------------------------------------
# 主线：自助改密成功 → 旧票全灭、新登录可用
# ---------------------------------------------------------------------------


class TestSelfServiceChangeRevokes:
    async def test_old_access_token_is_401_immediately_after_change(self, db) -> None:  # noqa: ARG002
        await seed_default_users()
        old_tokens = await member_login()

        await change_own_password(
            req_data=UpdatePassword(old_password="acme_user-pw", password="brand-new-pw"),  # noqa: S106
            _current_user=await DBUser.get(username="acme_user"),
        )

        with _client() as client:
            response = client.get("/api/user/me", headers=_bearer(old_tokens.access_token))
        assert response.status_code == 401, response.text
        assert response.json()["error"] == "TokenRevokedError", response.text

    async def test_generation_increments_in_db_exactly_once(self, db) -> None:  # noqa: ARG002
        await seed_default_users()
        me = await DBUser.get(username="acme_user")
        assert me.password_generation == 0  # 建号基线

        await user_change_password(me, "brand-new-pw")  # noqa: S106
        await me.refresh_from_db()
        assert me.password_generation == 1

        await user_change_password(me, "brand-new-pw-2")  # noqa: S106
        await me.refresh_from_db()
        assert me.password_generation == 2  # 是累加，不是写成定值

    async def test_wrong_old_password_does_not_revoke_anything(self, db) -> None:  # noqa: ARG002
        """世代只在改密**成功**后 +1：口令校验没过就把旧票全吊销，等于自造事故"""
        from nekro_agent.routers.user import IncorrectCurrentPasswordError

        await seed_default_users()
        tokens = await member_login()

        with pytest.raises(IncorrectCurrentPasswordError):
            await change_own_password(
                req_data=UpdatePassword(old_password="typo", password="pwned"),  # noqa: S106
                _current_user=await DBUser.get(username="acme_user"),
            )

        with _client() as client:
            assert client.get("/api/user/me", headers=_bearer(tokens.access_token)).status_code == 200

    async def test_new_login_after_change_works(self, db) -> None:  # noqa: ARG002
        await seed_default_users()
        await change_own_password(
            req_data=UpdatePassword(old_password="acme_user-pw", password="brand-new-pw"),  # noqa: S106
            _current_user=await DBUser.get(username="acme_user"),
        )

        new_tokens = await member_login(password="brand-new-pw")  # noqa: S106
        with _client() as client:
            response = client.get("/api/user/me", headers=_bearer(new_tokens.access_token))
        assert response.status_code == 200, response.text
        assert response.json()["username"] == "acme_user"

    async def test_the_401_tells_the_user_to_relogin(self, db) -> None:  # noqa: ARG002
        """错误文案要能看懂：与 TokenExpiredError 同档的「请重新登录」，而不是「未授权访问」"""
        await seed_default_users()
        old_tokens = await member_login()
        await change_own_password(
            req_data=UpdatePassword(old_password="acme_user-pw", password="brand-new-pw"),  # noqa: S106
            _current_user=await DBUser.get(username="acme_user"),
        )

        with _client() as client:
            response = client.get("/api/user/me", headers=_bearer(old_tokens.access_token))
        body = response.json()
        assert response.status_code == 401
        assert body["error"] == "TokenRevokedError"
        assert "重新登录" in body["message"], body
        # 与既有 401 体系同构：AppError 子类，全局处理器按类名回错误码
        assert issubclass(TokenRevokedError, AppError)
        assert TokenRevokedError.http_status == 401

    async def test_old_refresh_token_cannot_outlive_the_password(self, db) -> None:  # noqa: ARG002
        """旧 refresh 票立即失效：世代不符 → 401 档的 TokenRevokedError

        仓库当前没有「refresh 换新票」端点（见模块 docstring），所以断言落在
        换票端点**必须**调用的共同咽喉 verify_password_generation 上——
        access 与 refresh 走同一处校验，旧 refresh 票的 payload 在改密后必须被拒。
        """
        await seed_default_users()
        old_refresh = (await member_login()).refresh_token

        await change_own_password(
            req_data=UpdatePassword(old_password="acme_user-pw", password="brand-new-pw"),  # noqa: S106
            _current_user=await DBUser.get(username="acme_user"),
        )

        me = await DBUser.get(username="acme_user")
        assert me.password_generation == 1
        with pytest.raises(TokenRevokedError) as exc_info:
            verify_password_generation(decode_refresh(old_refresh), me)
        assert exc_info.value.http_status == 401

        # 对照：改密后新登录签发的 refresh 票（世代 1）能过同一处校验
        new_refresh = (await member_login(password="brand-new-pw")).refresh_token  # noqa: S106
        verify_password_generation(decode_refresh(new_refresh), me)  # 不抛即通过


# ---------------------------------------------------------------------------
# 管理员重置：同一条规矩的第二处 +1
# ---------------------------------------------------------------------------


class TestAdminResetRevokes:
    async def test_reset_bumps_generation_and_revokes_target_tokens(self, db) -> None:  # noqa: ARG002
        await seed_default_users()
        victim_tokens = await member_login()

        victim = await DBUser.get(username="acme_user")
        admin = await DBUser.get(username="admin")
        await admin_reset_password(user_id=victim.id, password="reset-by-admin", _current_user=admin)  # noqa: S106

        await victim.refresh_from_db()
        assert victim.password_generation == 1
        with _client() as client:
            response = client.get("/api/user/me", headers=_bearer(victim_tokens.access_token))
        assert response.status_code == 401, response.text
        assert response.json()["error"] == "TokenRevokedError", response.text

    async def test_reset_repeatedly_accumulates_generation(self, db) -> None:  # noqa: ARG002
        await seed_default_users()
        victim = await DBUser.get(username="acme_user")
        admin = await DBUser.get(username="admin")

        await admin_reset_password(user_id=victim.id, password="r1", _current_user=admin)  # noqa: S106
        await admin_reset_password(user_id=victim.id, password="r2", _current_user=admin)  # noqa: S106
        await victim.refresh_from_db()
        assert victim.password_generation == 2

        tokens = await member_login(password="r2")  # noqa: S106
        with _client() as client:
            assert client.get("/api/user/me", headers=_bearer(tokens.access_token)).status_code == 200

    async def test_platform_admin_self_reset_also_revokes_own_session(self, db) -> None:  # noqa: ARG002
        """平台方自重置是被保留的放行路径（见 SelfResetMustUseSelfServiceError 的 docstring），
        但口令一旦变了，世代照样 +1：admin 自己的旧令牌也被吊销。"""
        await seed_default_users()
        admin = await DBUser.get(username="admin")
        access = create_access_token("admin", extra_claims=_token_extra_claims(admin))

        # 对照：重置前这张票是好的（不然下面的 401 可能来自别的毛病）
        with _client() as client:
            assert client.get("/api/user/me", headers=_bearer(access)).status_code == 200

        await admin_reset_password(user_id=admin.id, password="self-reset", _current_user=admin)  # noqa: S106

        with _client() as client:
            response = client.get("/api/user/me", headers=_bearer(access))
        assert response.status_code == 401, response.text
        assert response.json()["error"] == "TokenRevokedError", response.text


# ---------------------------------------------------------------------------
# fail-closed：缺声明 / 类型不对 / 世代不符，一律拒绝
# ---------------------------------------------------------------------------


class TestFailClosed:
    async def test_token_without_the_claim_is_rejected(self, db) -> None:  # noqa: ARG002
        """存量令牌的形态（本特性上线前签发的一律没有 pwd_gen）：硬拒，不留过渡窗"""
        await seed_default_users()
        legacy_style = create_access_token("acme_user")  # 不带 extra_claims

        with _client() as client:
            response = client.get("/api/user/me", headers=_bearer(legacy_style))
        assert response.status_code == 401, response.text
        assert response.json()["error"] == "TokenRevokedError", response.text

    async def test_token_for_a_missing_user_is_still_unauthorized_not_revoked(self, db) -> None:  # noqa: ARG002
        """用户行都不存在时维持原判 UnauthorizedError：世代比对排在按 sub 回查之后"""
        await seed_default_users()
        ghost = create_access_token("no-such-user", extra_claims={PWD_GEN_CLAIM: 0})

        with pytest.raises(UnauthorizedError):
            await get_current_user(request=_StubConnection(), token=ghost)

    @pytest.mark.parametrize(
        ("claim_value", "db_generation"),
        [
            pytest.param(True, 1, id="bool-true-against-gen-1"),
            pytest.param(False, 0, id="bool-false-against-gen-0"),
            pytest.param("0", 0, id="string-against-gen-0"),
            pytest.param(0.0, 0, id="float-against-gen-0"),
            pytest.param(None, 0, id="none"),
        ],
    )
    async def test_non_int_claims_are_rejected(self, db, claim_value: Any, db_generation: int) -> None:  # noqa: ARG002
        """bool 是 int 的子类（True == 1）、"0" == 0 都会发生宽松相等，类型必须严格"""
        await seed_default_users()
        await DBUser.filter(username="acme_user").update(password_generation=db_generation)
        me = await DBUser.get(username="acme_user")

        with pytest.raises(TokenRevokedError):
            verify_password_generation({PWD_GEN_CLAIM: claim_value}, me)

    async def test_matching_generation_passes(self, db) -> None:  # noqa: ARG002
        """世代匹配的正常令牌照常通过（正向对照，防住「永远拒绝」的实现）"""
        await seed_default_users()
        await DBUser.filter(username="acme_user").update(password_generation=7)
        me = await DBUser.get(username="acme_user")

        verify_password_generation({PWD_GEN_CLAIM: 7}, me)  # 不抛即通过
        with pytest.raises(TokenRevokedError):
            verify_password_generation({PWD_GEN_CLAIM: 6}, me)

    async def test_stale_int_generation_over_http(self, db) -> None:  # noqa: ARG002
        """世代数字对不上（而非缺声明）同样 401——这是改密吊销的正命中形态"""
        await seed_default_users()
        tokens = await member_login()
        # 不改口令，只把行拨到下一代：等价于「令牌是上一世代签发的」
        await DBUser.filter(username="acme_user").update(password_generation=1)

        with _client() as client:
            response = client.get("/api/user/me", headers=_bearer(tokens.access_token))
        assert response.status_code == 401, response.text
        assert response.json()["error"] == "TokenRevokedError", response.text


# ---------------------------------------------------------------------------
# 性能约束：世代比对不得引入任何额外数据库查询（每条 SSE 长连接都走这里）
# ---------------------------------------------------------------------------


class TestNoExtraQuery:
    @staticmethod
    def _count_backend_queries(monkeypatch: pytest.MonkeyPatch) -> List[str]:
        """给 sqlite 连接的所有查询入口包一层计数器"""
        conn = Tortoise.get_connection("default")
        seen: List[str] = []

        for method_name in ("execute_query", "execute_query_dict", "execute_insert", "execute_many"):
            original = getattr(conn, method_name, None)
            if original is None:
                continue

            def make_wrapper(orig: Any, name: str) -> Any:
                async def wrapper(*args: Any, **kwargs: Any) -> Any:
                    seen.append(f"{name}:{args[0] if args else kwargs}")
                    return await orig(*args, **kwargs)

                return wrapper

            monkeypatch.setattr(conn, method_name, make_wrapper(original, method_name), raising=False)
        return seen

    async def test_generation_check_runs_with_exactly_one_query(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        """通过路径：JWT 解码 + 按 sub 回查用户行，仅此一条 SQL"""
        await seed_default_users()
        tokens = await member_login()

        seen = self._count_backend_queries(monkeypatch)
        user = await get_current_user(request=_StubConnection(), token=tokens.access_token)
        assert user.username == "acme_user"
        assert len(seen) == 1, seen  # 只有 get_or_none 那一条，世代比对零查询

    async def test_revoked_path_also_runs_with_exactly_one_query(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        """拒绝路径同理：世代比对是纯内存动作，不能「先查一遍世代再查用户」"""
        await seed_default_users()
        tokens = await member_login()
        await DBUser.filter(username="acme_user").update(password_generation=1)

        seen = self._count_backend_queries(monkeypatch)
        with pytest.raises(TokenRevokedError):
            await get_current_user(request=_StubConnection(), token=tokens.access_token)
        assert len(seen) == 1, seen


# ---------------------------------------------------------------------------
# 迁移幂等：测试库里已有同名列（上游 23 铺的），重放不能炸
# ---------------------------------------------------------------------------


def _load_migration_25() -> Any:
    """按文件名约定加载迁移 25（编号在仓库唯一，不锁死时间戳）"""
    candidates = sorted(MIGRATION_DIR.glob("25_*password_generation.py"))
    assert len(candidates) == 1, f"迁移 25 应恰好一个文件，实际: {candidates}"
    spec = importlib.util.spec_from_file_location("migration_25_password_generation", candidates[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["migration_25_password_generation"] = module
    spec.loader.exec_module(module)
    return module


def _statements(script: str) -> List[str]:
    """把 aerich 迁移脚本拆成单条语句（剥掉 -- 注释行；全文注释里没有分号分隔歧义）"""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


class TestMigrationIdempotent:
    async def test_upgrade_sql_is_idempotent_by_shape(self) -> None:
        """静态契约：目标语句必须是幂等形态，且不存在任何非幂等的同列 ADD"""
        migration = _load_migration_25()
        upgrade_sql = await migration.upgrade(db=None)
        downgrade_sql = await migration.downgrade(db=None)

        # 目标语句逐字在位（INT NOT NULL DEFAULT 0 = 存量行世代基线 0，与模型默认一致）
        assert 'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS "password_generation" INT NOT NULL DEFAULT 0' in upgrade_sql
        # 不存在裸 ADD / 带 COLUMN 但缺 IF NOT EXISTS 的同列写法（ aerich 重新生成的默认产物长这样 ）
        bare = re.findall(r'ADD(?:\s+COLUMN)?\s+"password_generation"', upgrade_sql)
        assert not bare, f"存在非幂等的 ADD COLUMN 写法: {bare}"
        # 回滚同样幂等
        assert 'DROP COLUMN IF EXISTS "password_generation"' in downgrade_sql

    async def test_double_apply_against_real_postgres(self) -> None:
        """真库双执行：还原「列已存在」的现场（上游 23 铺过），再跑两遍 upgrade 原文"""
        dsn = os.environ.get(PG_DSN_ENV, "")
        if not dsn:
            pytest.skip(
                f"需要真实 PostgreSQL：设 {PG_DSN_ENV}（如 postgres://postgres@localhost:55499/na54）。"
                "静态契约由 test_upgrade_sql_is_idempotent_by_shape 常绿把关；"
                "迁移落盘时已在临时 PG 上双执行实证（见交付报告）。",
            )

        import asyncpg

        migration = _load_migration_25()
        upgrade_sql = await migration.upgrade(db=None)
        downgrade_sql = await migration.downgrade(db=None)

        async def column_count(conn: Any) -> int:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name='user' AND column_name='password_generation'",
            )

        conn = await asyncpg.connect(dsn=dsn)
        try:
            # 现场还原：表在、列在（等价于上游 23 已铺过的库），然后 upgrade 跑两遍
            await conn.execute('DROP TABLE IF EXISTS "user"')
            await conn.execute('CREATE TABLE "user" ("id" SERIAL PRIMARY KEY)')
            await conn.execute(
                'ALTER TABLE "user" ADD COLUMN "password_generation" INT NOT NULL DEFAULT 0',
            )
            for _ in range(2):
                for stmt in _statements(upgrade_sql):
                    await conn.execute(stmt)
            assert await column_count(conn) == 1

            # 回滚也幂等：跑两遍，列没了、第二遍不炸
            for _ in range(2):
                for stmt in _statements(downgrade_sql):
                    await conn.execute(stmt)
            assert await column_count(conn) == 0
        finally:
            await conn.close()
