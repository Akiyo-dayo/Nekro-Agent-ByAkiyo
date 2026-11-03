"""#54 ②：轮换 `ADMIN_PASSWORD` 之后，旧的 admin 令牌必须失效

## 缺口

#54 的口令世代只管**库里有口令哈希的账号**：改密 / 重置把 `password_generation` +1，旧令牌世代对不上即 401。
但平台方 `admin` 的口令**不在库里**——`user_login` 只比对环境变量 `OsEnv.ADMIN_PASSWORD`
（`routers/user.py` 明说 admin 口令不入库）。于是运维把口令泄漏后轮换环境变量，
**此前签出去的 admin 令牌照旧有效**，要吊销只能走 user-manager 自重置（世代 +1）。

## 做法（不新增迁移）

admin 的 `pwd_gen` 声明不再是裸整数，而是 `"<库内世代>.<口令指纹>"`：

- 指纹 = `HMAC(HMAC(JWT 密钥, 固定用途串), ADMIN_PASSWORD)` 的前 128 位十六进制。**带密钥**——令牌载荷是公开可读的
  （JWT 只签名不加密），把口令的无密钥哈希放进去，等于把离线撞库的靶子贴在每一张令牌上；
- 签发与校验共用 `auth.expected_pwd_gen_claim` 这**一处**逻辑（两侧各算一遍、将来改了一边就是
  「签出来的票自己验不过」或「验的比签的松」）；
- **租户成员路径逐字不变**：声明仍是裸整数，校验仍是「必须是 int、bool 不算、数值相等」；
- 不特殊处理空口令：`ADMIN_PASSWORD` 为空时 admin 本来就登不上，指纹照算（空串的 HMAC），
  于是「从有口令改成空」同样吊销旧票。
"""

import datetime
import hashlib
import hmac
import json
from typing import Any, Dict, List

import pytest
from jose import jwt
from loguru import logger
from tortoise import Tortoise

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.user import UserLogin
from nekro_agent.services.user.auth import (
    PWD_GEN_CLAIM,
    admin_password_fingerprint,
    build_pwd_gen_claim,
    create_access_token,
    expected_pwd_gen_claim,
    get_hashed_password,
)
from nekro_agent.services.user.deps import TokenRevokedError, get_current_user, verify_password_generation
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.util import _token_extra_claims, tenant_member_login, user_login

OLD_PASSWORD = "old-admin-password-0xA1"  # noqa: S105
NEW_PASSWORD = "new-admin-password-0xB2"  # noqa: S105
ACME_TENANT_ID = 1


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def admin_password(monkeypatch: pytest.MonkeyPatch):
    """把 ADMIN_PASSWORD 钉成 OLD，并返回一个「轮换到 X」的函数。

    用 `monkeypatch.setattr(OsEnv, ...)` 而不是 setenv：`OsEnv` 的取值在 import 期定死，
    setenv 改不动它（§2.33）；产品代码是**调用时**读 `OsEnv.ADMIN_PASSWORD`，所以这样注入是等价的。
    """
    monkeypatch.setattr(OsEnv, "ADMIN_PASSWORD", OLD_PASSWORD)

    def rotate(to: str) -> None:
        monkeypatch.setattr(OsEnv, "ADMIN_PASSWORD", to)

    return rotate


async def seed_users() -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    await DBUser.create(
        username="admin",
        password=get_hashed_password(OLD_PASSWORD),
        adapter_key="",
        platform_userid="",
        perm_level=Role.Super,
        login_time=now,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )
    await DBUser.create(
        username="acme_user",
        password=get_hashed_password("acme_user-pw"),
        adapter_key="",
        platform_userid="",
        perm_level=Role.User,
        login_time=now,
        tenant_id=ACME_TENANT_ID,
        tenant_role=TenantRole.TENANT_USER.value,
    )


async def admin_login() -> Any:
    """走真实的 admin 登录路径（比对 `OsEnv.ADMIN_PASSWORD`），不手搓 JWT"""
    return await user_login(UserLogin(username="admin", password=OsEnv.ADMIN_PASSWORD))


def decode_access(token: str) -> Dict[str, Any]:
    return jwt.decode(token, OsEnv.JWT_SECRET_KEY, algorithms=[OsEnv.ENCRYPT_ALGORITHM])


def decode_refresh(token: str) -> Dict[str, Any]:
    return jwt.decode(token, OsEnv.JWT_REFRESH_SECRET_KEY, algorithms=[OsEnv.ENCRYPT_ALGORITHM])


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


def _me(token: str) -> Any:
    with _client() as client:
        return client.get("/api/user/me", headers=_bearer(token))


class _StubConnection:
    def __init__(self) -> None:
        self.query_params: Dict[str, str] = {}
        self.headers: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# 主线：轮换口令 → 旧 admin 令牌失效
# ---------------------------------------------------------------------------


class TestRotatingTheAdminPasswordRevokesTheOldTokens:
    async def test_the_old_token_is_401_revoked_after_rotation_and_a_new_login_works(
        self,
        db,  # noqa: ARG002
        admin_password,
    ) -> None:
        await seed_users()
        old = await admin_login()
        assert _me(old.access_token).status_code == 200  # 对照：轮换前这张票是好的

        admin_password(NEW_PASSWORD)

        stale = _me(old.access_token)
        assert stale.status_code == 401, stale.text
        assert stale.json()["error"] == "TokenRevokedError", stale.text
        assert "重新登录" in stale.json()["message"]
        fresh = await admin_login()
        assert _me(fresh.access_token).status_code == 200

    async def test_the_old_password_can_no_longer_log_in_either(self, db, admin_password) -> None:  # noqa: ARG002
        """对照：吊销的是「令牌」，登录本来就只认当前环境变量（这条不是本次改动，钉住别退化）"""
        from nekro_agent.schemas.errors import AppError

        await seed_users()
        admin_password(NEW_PASSWORD)

        with pytest.raises(AppError):
            await user_login(UserLogin(username="admin", password=OLD_PASSWORD))

    async def test_the_same_password_does_not_revoke(self, db, admin_password) -> None:  # noqa: ARG002
        """同一口令（进程重启、环境变量原样再设一遍）不能误吊销——否则每次重启全员重登"""
        await seed_users()
        token = await admin_login()

        admin_password(OLD_PASSWORD)  # 等价于重启后环境变量不变

        assert _me(token.access_token).status_code == 200
        again = await admin_login()
        assert decode_access(again.access_token)[PWD_GEN_CLAIM] == decode_access(token.access_token)[PWD_GEN_CLAIM]

    async def test_rotating_to_empty_also_revokes(self, db, admin_password) -> None:  # noqa: ARG002
        """从有口令改成空（admin 无法登录）：旧票同样作废。不特殊处理空串，指纹照算"""
        await seed_users()
        token = await admin_login()

        admin_password("")

        assert _me(token.access_token).status_code == 401

    async def test_rotating_back_makes_the_old_token_valid_again(self, db, admin_password) -> None:  # noqa: ARG002
        """指纹只取决于（JWT 密钥, 当前口令, 库内世代）：换回原口令，原票自然又对得上。
        这是「没有口令世代表」的必然（不新增迁移）；要真的吊销就走 user-manager 自重置（世代 +1）。"""
        await seed_users()
        token = await admin_login()

        admin_password(NEW_PASSWORD)
        assert _me(token.access_token).status_code == 401
        admin_password(OLD_PASSWORD)
        assert _me(token.access_token).status_code == 200

    async def test_refresh_token_carries_the_same_claim_and_is_rejected_by_the_choke_point(
        self,
        db,  # noqa: ARG002
        admin_password,
    ) -> None:
        await seed_users()
        token = await admin_login()
        admin_row = await DBUser.get(username="admin")
        refresh_payload = decode_refresh(token.refresh_token)
        assert refresh_payload[PWD_GEN_CLAIM] == decode_access(token.access_token)[PWD_GEN_CLAIM]

        verify_password_generation(refresh_payload, admin_row)  # 轮换前通过
        admin_password(NEW_PASSWORD)
        with pytest.raises(TokenRevokedError):
            verify_password_generation(refresh_payload, admin_row)

    async def test_db_generation_still_revokes_on_its_own(self, db, admin_password) -> None:  # noqa: ARG002
        """既有的 user-manager 自重置路径（世代 +1）不能被指纹掩盖：口令没变、世代变了，照样吊销"""
        await seed_users()
        token = await admin_login()

        await DBUser.filter(username="admin").update(password_generation=1)

        assert _me(token.access_token).status_code == 401


# ---------------------------------------------------------------------------
# 声明的形态与指纹性质
# ---------------------------------------------------------------------------


class TestClaimShape:
    async def test_admin_claim_is_generation_dot_fingerprint(self, db, admin_password) -> None:  # noqa: ARG002
        await seed_users()
        await DBUser.filter(username="admin").update(password_generation=3)
        admin_row = await DBUser.get(username="admin")

        claim = build_pwd_gen_claim(admin_row)[PWD_GEN_CLAIM]

        assert isinstance(claim, str)
        generation, fingerprint = claim.split(".")
        assert generation == "3"
        assert len(fingerprint) == 32 and int(fingerprint, 16) >= 0
        assert fingerprint == admin_password_fingerprint()

    async def test_tenant_claim_stays_a_bare_integer_verbatim(self, db) -> None:  # noqa: ARG002
        await seed_users()
        await DBUser.filter(username="acme_user").update(password_generation=5)
        tokens = await tenant_member_login(UserLogin(username="acme_user", password="acme_user-pw"))

        claim = decode_access(tokens.access_token)[PWD_GEN_CLAIM]
        assert claim == 5 and type(claim) is int
        assert decode_refresh(tokens.refresh_token)[PWD_GEN_CLAIM] == 5

    async def test_fingerprint_depends_on_the_password_and_nothing_else_random(
        self,
        db,  # noqa: ARG002
        admin_password,
    ) -> None:
        old_fp = admin_password_fingerprint()
        assert admin_password_fingerprint() == old_fp  # 确定性：同一口令每次算出同一个值

        admin_password(NEW_PASSWORD)
        assert admin_password_fingerprint() != old_fp

    async def test_fingerprint_matches_an_independent_recomputation(
        self,
        db,  # noqa: ARG002
        admin_password,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """算法本身钉成一条离线向量：`HMAC(HMAC(JWT 密钥, 用途串), 口令)` 的前 32 个十六进制字符。
        自洽的往返用例（本进程签、本进程验）换套派生照样通过；这条把用途串与两级 HMAC 的结构钉死
        （改用途串 = 所有 admin 令牌当场失效，属破坏性变更，不能是顺手一改）。"""
        monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", "golden-jwt-secret-for-the-vector-test-0123456789")
        monkeypatch.setattr(OsEnv, "ADMIN_PASSWORD", "golden-admin-password")
        derived_key = hmac.new(
            b"golden-jwt-secret-for-the-vector-test-0123456789",
            b"nekro-agent/admin-password-fingerprint/v1",
            hashlib.sha256,
        ).digest()
        expected = hmac.new(derived_key, b"golden-admin-password", hashlib.sha256).hexdigest()[:32]

        assert admin_password_fingerprint() == expected

    async def test_fingerprint_is_keyed_by_the_jwt_secret(
        self,
        db,  # noqa: ARG002
        admin_password,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """同一口令、换一把 JWT 密钥，指纹就不同——证明它是 HMAC 而不是口令的无密钥哈希"""
        before = admin_password_fingerprint()

        monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", OsEnv.JWT_SECRET_KEY + "-rotated")

        assert admin_password_fingerprint() != before

    async def test_the_token_never_carries_the_password_or_any_unkeyed_hash_of_it(
        self,
        db,  # noqa: ARG002
        admin_password,  # noqa: ARG002
    ) -> None:
        await seed_users()
        token = await admin_login()
        admin_row = await DBUser.get(username="admin")
        payloads = [decode_access(token.access_token), decode_refresh(token.refresh_token)]
        serialised = json.dumps(payloads, default=str)
        fingerprint = admin_password_fingerprint()

        password = OLD_PASSWORD.encode()
        unkeyed = {
            "password": OLD_PASSWORD,
            "sha256": hashlib.sha256(password).hexdigest(),
            "sha1": hashlib.sha1(password).hexdigest(),  # noqa: S324
            "md5": hashlib.md5(password).hexdigest(),  # noqa: S324
            "sha512": hashlib.sha512(password).hexdigest(),
            "库里的 bcrypt 行": admin_row.password,
            "JWT 密钥": OsEnv.JWT_SECRET_KEY,
        }
        for name, material in unkeyed.items():
            assert material not in serialised, f"令牌里出现了 {name}"
        # 指纹不是无密钥哈希的任何前缀（128 位 = 32 个十六进制字符）
        for name in ("sha256", "sha1", "md5", "sha512"):
            assert not unkeyed[name].startswith(fingerprint), f"指纹等于 {name} 的前缀"
            assert fingerprint not in unkeyed[name]

    async def test_tenant_tokens_ignore_the_admin_password_entirely(
        self,
        db,  # noqa: ARG002
        admin_password,
    ) -> None:
        """租户成员的令牌与 ADMIN_PASSWORD 无关：轮换 admin 口令不能把租户全员踢下线"""
        await seed_users()
        tokens = await tenant_member_login(UserLogin(username="acme_user", password="acme_user-pw"))
        assert _me(tokens.access_token).status_code == 200

        admin_password(NEW_PASSWORD)

        assert _me(tokens.access_token).status_code == 200


# ---------------------------------------------------------------------------
# 校验侧：fail-closed
# ---------------------------------------------------------------------------


class TestAdminValidationIsFailClosed:
    @staticmethod
    async def _admin() -> DBUser:
        await seed_users()
        return await DBUser.get(username="admin")

    async def test_the_current_claim_passes_and_anything_else_is_rejected(
        self,
        db,  # noqa: ARG002
        admin_password,  # noqa: ARG002
    ) -> None:
        admin = await self._admin()
        current = expected_pwd_gen_claim(admin)

        verify_password_generation({PWD_GEN_CLAIM: current}, admin)  # 正向对照：不抛即通过

        generation, fingerprint = str(current).split(".")
        wrong_fingerprint = ("0" if fingerprint[0] != "0" else "1") + fingerprint[1:]
        missing = object()
        bad_claims: Dict[str, Any] = {
            "缺声明": missing,
            "None": None,
            "旧格式裸整数（世代恰好对上）": 0,
            "bool": True,
            "只有世代、没有指纹": generation,
            "指纹为空": f"{generation}.",
            "指纹错一位": f"{generation}.{wrong_fingerprint}",
            "世代不符": f"1.{fingerprint}",
            "指纹多一位": f"{generation}.{fingerprint}x",
            "大小写不同": f"{generation}.{fingerprint}".upper(),
            "列表": ["0", fingerprint],
            "非 ASCII 字符不能让比较抛 TypeError": "０.１２３",
        }
        for label, claim in bad_claims.items():
            payload = {} if claim is missing else {PWD_GEN_CLAIM: claim}
            with pytest.raises(TokenRevokedError):
                verify_password_generation(payload, admin)
            assert label  # 失败时 pytest 的回溯里能看到是哪一格

    async def test_a_tenant_style_integer_claim_is_rejected_for_admin_even_at_the_right_generation(
        self,
        db,  # noqa: ARG002
        admin_password,  # noqa: ARG002
    ) -> None:
        """上线那一刻的存量 admin 令牌（裸整数世代）必须被拒——那正是「轮换前签出的票」的形态"""
        await seed_users()
        legacy = create_access_token("admin", extra_claims={PWD_GEN_CLAIM: 0})

        response = _me(legacy)

        assert response.status_code == 401 and response.json()["error"] == "TokenRevokedError"

    async def test_a_forged_admin_token_with_a_guessed_fingerprint_shape_is_rejected(
        self,
        db,  # noqa: ARG002
        admin_password,  # noqa: ARG002
    ) -> None:
        await seed_users()
        forged = create_access_token("admin", extra_claims={PWD_GEN_CLAIM: "0." + "0" * 32})

        assert _me(forged).status_code == 401

    async def test_a_string_claim_is_still_rejected_for_a_tenant_member(self, db) -> None:  # noqa: ARG002
        """租户路径逐字不变：形态必须是 int，admin 才有的 `世代.指纹` 字符串对租户不成立"""
        await seed_users()
        member = await DBUser.get(username="acme_user")

        for claim in ("0", "0.deadbeef", True):
            with pytest.raises(TokenRevokedError):
                verify_password_generation({PWD_GEN_CLAIM: claim}, member)
        verify_password_generation({PWD_GEN_CLAIM: 0}, member)  # 对照：裸整数照常通过


# ---------------------------------------------------------------------------
# 签发与校验共用一处逻辑
# ---------------------------------------------------------------------------


class TestIssuanceAndValidationShareOneImplementation:
    async def test_the_issued_claim_is_exactly_what_the_validator_expects(
        self,
        db,  # noqa: ARG002
        admin_password,  # noqa: ARG002
    ) -> None:
        await seed_users()
        for username in ("admin", "acme_user"):
            row = await DBUser.get(username=username)
            assert build_pwd_gen_claim(row) == {PWD_GEN_CLAIM: expected_pwd_gen_claim(row)}
            assert _token_extra_claims(row)[PWD_GEN_CLAIM] == expected_pwd_gen_claim(row)

    def test_call_sites_exist_and_nobody_else_computes_the_fingerprint(self) -> None:
        """AST / 文本判据（§2.7）：两侧真的调用同一个函数，且指纹只在 auth.py 里算"""
        import ast
        from pathlib import Path

        root = Path(__file__).parent.parent / "nekro_agent"
        auth_source = (root / "services" / "user" / "auth.py").read_text(encoding="utf-8")
        deps_source = (root / "services" / "user" / "deps.py").read_text(encoding="utf-8")

        def calls_in(source: str, function: str) -> List[str]:
            tree = ast.parse(source)
            node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
            return [ast.unparse(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)]

        assert "expected_pwd_gen_claim" in calls_in(auth_source, "build_pwd_gen_claim")
        assert "expected_pwd_gen_claim" in calls_in(deps_source, "verify_password_generation")
        assert "admin_password_fingerprint" in calls_in(auth_source, "expected_pwd_gen_claim")
        # 指纹是秘密派生值：比较必须是常数时间的（`==` 在行为上等价，只有这条静态判据能看见它被换掉）
        assert "hmac.compare_digest" in calls_in(deps_source, "_pwd_gen_matches")
        offenders = [
            str(path.relative_to(root))
            for path in root.rglob("*.py")
            if path.name != "auth.py" and "admin_password_fingerprint" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"指纹在别处被调用 / 复制了: {offenders}"


# ---------------------------------------------------------------------------
# 性能与日志
# ---------------------------------------------------------------------------


class TestNoExtraQueryAndNoLeakInLogs:
    async def test_admin_check_is_one_query(
        self,
        db,  # noqa: ARG002
        admin_password,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """指纹是纯内存计算：admin 这一路与租户一样，只有「按 sub 回查用户行」那一条 SQL"""
        await seed_users()
        token = await admin_login()
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

        user = await get_current_user(request=_StubConnection(), token=token.access_token)

        assert user.username == "admin"
        assert len(seen) == 1, seen

    async def test_a_rejected_admin_token_logs_neither_fingerprint_nor_password(
        self,
        db,  # noqa: ARG002
        admin_password,
    ) -> None:
        await seed_users()
        token = await admin_login()
        old_fingerprint = admin_password_fingerprint()
        old_claim = decode_access(token.access_token)[PWD_GEN_CLAIM]
        admin_password(NEW_PASSWORD)
        new_fingerprint = admin_password_fingerprint()

        captured: List[str] = []
        sink_id = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
        try:
            with pytest.raises(TokenRevokedError):
                await get_current_user(request=_StubConnection(), token=token.access_token)
        finally:
            logger.remove(sink_id)

        joined = "\n".join(captured)
        assert "rejected" in joined, "拒绝时的调试日志本身没打出来，这条用例什么都没验"
        for secret in (old_fingerprint, new_fingerprint, str(old_claim), OLD_PASSWORD, NEW_PASSWORD):
            assert secret not in joined

    async def test_the_tenant_rejection_log_line_is_unchanged(self, db) -> None:  # noqa: ARG002
        """租户路径逐字不变：连调试日志的措辞都不动"""
        await seed_users()
        tokens = await tenant_member_login(UserLogin(username="acme_user", password="acme_user-pw"))
        await DBUser.filter(username="acme_user").update(password_generation=1)

        captured: List[str] = []
        sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="DEBUG")
        try:
            with pytest.raises(TokenRevokedError):
                await get_current_user(request=_StubConnection(), token=tokens.access_token)
        finally:
            logger.remove(sink_id)

        expected = (
            "Token of 'acme_user' rejected: pwd_gen claim is missing or stale (token=0, db=1)"
        )
        assert expected in captured, captured

