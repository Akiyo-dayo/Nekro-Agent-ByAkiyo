"""自助改密（`PUT /user/password`）的判据

## 这条端点改前是什么

```python
if req_data.user_id is not None and _current_user.id != req_data.user_id and ... < Role.Super:
    raise PermissionDeniedError
await user_change_password(_current_user, req_data.password)     # 注意：写入对象恒为自己
```

三条缺陷，互相独立：

1. **不校验旧口令。** 任何一次令牌泄露都能就地升级成永久接管 —— 令牌会过期，改掉的口令不会。
2. `user_id` 从来没生效过。写入对象恒为 `_current_user`，平台方带别人的 `user_id` 调用时
   改掉的其实是**自己**的口令，接口还回 200。
3. 前端 `pages/profile/index.tsx` 整页是死代码（`/profile` 指向社区账号页），
   于是租户成员**根本没有**自助改密入口，口令只能由平台方一次性设定。

## 两条改密端点，两套判据，不要弄混

| 端点 | 改谁的 | 判据 |
|---|---|---|
| `PUT /user/password` | 只能是自己 | **当前口令**（本文件） |
| `POST /user-manager/{user_id}/reset-password` | 别人 | `assert_managed_user_accessible`（归属 + 等级），**不要**旧口令 |

管理员重置别人的口令时按定义不知道对方的旧口令，所以第二条端点一个字都不该改；
本文件为此留了一组正向对照（`TestAdminResetIsADifferentEndpoint`），
防止将来有人"顺手统一一下"把旧口令要求加到那条上，把平台方的重置能力一并废掉。

## 为什么"当前口令不对"是 403 而不是 401

前端 axios 拦截器对**任何非登录接口**的 401 一律清空登录态并跳回登录页
（`frontend/src/services/api/axios.ts`）。用 `InvalidCredentialsError`(401) 的话，
旧口令打错一个字母 = 当场被踢下线。`TestWrongPasswordDoesNotLogYouOut` 把这条钉住。
"""

import datetime
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.user import (
    EnvManagedPasswordError,
    IncorrectCurrentPasswordError,
    current_password_matches,
)
from nekro_agent.routers.user import (
    password as change_own_password,
)
from nekro_agent.routers.user_manager import (
    SelfResetMustUseSelfServiceError,
)
from nekro_agent.routers.user_manager import (
    reset_password as admin_reset_password,
)
from nekro_agent.schemas.errors import (
    AppError,
    InvalidCredentialsError,
    NotFoundError,
    PermissionDeniedError,
)
from nekro_agent.schemas.errors import (
    ValidationError as AppValidationError,
)
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.user import UpdatePassword
from nekro_agent.services.user.auth import get_hashed_password, verify_password
from nekro_agent.services.user.role import Role

ACME_TENANT_ID = 1
RIVAL_TENANT_ID = 2

# (用户名, tenant_id, 租户角色, perm_level, 口令原文)
#
# `EMPTY_HASH_USER` / `BROKEN_HASH_USER` 不是臆造的边角：前者是聊天平台用户行的真实形态
# （`user_register` 用空串建号），后者兜的是手改数据库 / 历史迁移留下的非 bcrypt 串。
USER_LAYOUT: List[Tuple[str, Optional[int], TenantRole, Role, str]] = [
    ("admin", None, TenantRole.PLATFORM_OWNER, Role.Super, "admin-pw"),
    ("acme_admin", ACME_TENANT_ID, TenantRole.TENANT_ADMIN, Role.Admin, "acme_admin-pw"),
    ("acme_user", ACME_TENANT_ID, TenantRole.TENANT_USER, Role.User, "acme_user-pw"),
    ("rival_admin", RIVAL_TENANT_ID, TenantRole.TENANT_ADMIN, Role.Admin, "rival_admin-pw"),
    ("legacy_empty_hash", ACME_TENANT_ID, TenantRole.TENANT_USER, Role.User, ""),
    # 平台方一侧的非 Super 账号。不是臆造的：`tenant_id` 默认 NULL、`tenant_role` 默认
    # `platform_owner`，所以 `POST /user-manager/create` 建出来的就是这个形状（只是 Role.User）；
    # `Role.Admin` 这一档来自多租户改造之前的存量行，`util.user_login()` 里那句
    # "早期版本以 Role.Admin 建出的 admin 行需要就地补齐"说的就是它们。
    #
    # 它在本文件里的作用是**判据鉴别**：这一行同时满足"无租户作用域"与"perm_level < Super"，
    # 于是「按作用域判」与「按等级判」两种实现在它身上给出相反结果。
    ("legacy_platform_admin", None, TenantRole.PLATFORM_OWNER, Role.Admin, "legacy_platform_admin-pw"),
]

BROKEN_HASH_USER = "legacy_broken_hash"
BROKEN_HASH_VALUE = "not-a-bcrypt-hash"  # noqa: S105


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for username, tenant_id, tenant_role, perm_level, raw in USER_LAYOUT:
            await DBUser.create(
                username=username,
                # 真实哈希：口令有没有被改写这条断言要靠 `verify_password` 读，空串不是合法哈希。
                # `legacy_empty_hash` 存的是**空串的哈希**（合法 bcrypt，验空口令返回 True），
                # 与 `legacy_broken_hash` 的"根本不是哈希"是两种不同的存量脏数据。
                password=get_hashed_password(raw),
                adapter_key="",
                platform_userid="",
                perm_level=perm_level,
                login_time=datetime.datetime.now(datetime.timezone.utc),
                tenant_id=tenant_id,
                tenant_role=tenant_role.value,
            )
        await DBUser.create(
            username=BROKEN_HASH_USER,
            password=BROKEN_HASH_VALUE,
            adapter_key="",
            platform_userid="",
            perm_level=Role.User,
            login_time=datetime.datetime.now(datetime.timezone.utc),
            tenant_id=ACME_TENANT_ID,
            tenant_role=TenantRole.TENANT_USER.value,
        )
        yield
    finally:
        await Tortoise.close_connections()


async def caller(username: str) -> DBUser:
    return await DBUser.get(username=username)


async def hash_of(username: str) -> str:
    return (await DBUser.get(username=username)).password


def req(old: str, new: str) -> UpdatePassword:
    return UpdatePassword(old_password=old, password=new)


def _load_route_contract() -> ModuleType:
    """按路径加载 `test_route_permission_contract`，避免依赖 pytest 往 sys.path 塞了什么"""
    name = "test_route_permission_contract"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# 能自助改密的账号：(用户名, 正确的当前口令)
SELF_SERVICE_ACCOUNTS = [
    pytest.param("acme_admin", "acme_admin-pw", id="tenant-admin"),
    pytest.param("acme_user", "acme_user-pw", id="tenant-user"),
    pytest.param("rival_admin", "rival_admin-pw", id="other-tenant-admin"),
]


class TestCorrectCurrentPasswordPasses:
    """正向：租户成员（管理员与只读成员都算）能自己换掉自己的口令"""

    @pytest.mark.parametrize(("username", "current"), SELF_SERVICE_ACCOUNTS)
    async def test_self_service_change_succeeds(self, db, username: str, current: str) -> None:  # noqa: ARG002
        me = await caller(username)
        result = await change_own_password(req_data=req(current, "brand-new-pw"), _current_user=me)
        assert result.ok is True

        await me.refresh_from_db()
        assert verify_password("brand-new-pw", me.password)
        # 旧口令必须真的失效，否则"改了"只是加了一个可用口令
        assert not verify_password(current, me.password)

    async def test_only_the_caller_row_is_touched(self, db) -> None:  # noqa: ARG002
        """改自己不能顺手动到别人 —— 写入对象是 `_current_user`，这条钉住它没被换成别的查询"""
        untouched = {name: await hash_of(name) for name, *_ in USER_LAYOUT if name != "acme_user"}
        await change_own_password(
            req_data=req("acme_user-pw", "brand-new-pw"),
            _current_user=await caller("acme_user"),
        )
        for name, original in untouched.items():
            assert await hash_of(name) == original, name


# 每一种"当前口令给得不对"的形态，都必须被拒且账号口令原封不动
WRONG_CURRENT_PASSWORDS = [
    pytest.param("acme_admin-pw-typo", id="one-char-off"),
    pytest.param("", id="empty"),
    pytest.param("acme_user-pw", id="someone-elses-password"),
    pytest.param("ACME_ADMIN-PW", id="case-flipped"),
    pytest.param("acme_admin-pw ", id="trailing-space"),
]


class TestWrongCurrentPasswordIsRefused:
    """这是本轮的安全主线：没有当前口令就改不了口令"""

    @pytest.mark.parametrize("wrong", WRONG_CURRENT_PASSWORDS)
    async def test_refused_and_password_unchanged(self, db, wrong: str) -> None:  # noqa: ARG002
        me = await caller("acme_admin")
        original = me.password

        with pytest.raises(IncorrectCurrentPasswordError):
            await change_own_password(req_data=req(wrong, "pwned123"), _current_user=me)

        await me.refresh_from_db()
        assert me.password == original
        assert verify_password("acme_admin-pw", me.password)
        assert not verify_password("pwned123", me.password)

    async def test_stolen_token_cannot_be_upgraded_into_ownership(self, db) -> None:  # noqa: ARG002
        """改前的确切攻击：拿到一张令牌 -> 直接改密 -> 令牌过期后仍然占着账号

        `_current_user` 就是令牌解析出来的那一行，所以"持有令牌"在这条用例里已经成立；
        缺的只有当前口令，而攻击者没有。
        """
        victim = await caller("acme_user")
        with pytest.raises(AppError):
            await change_own_password(req_data=req("", "attacker-owns-it"), _current_user=victim)
        await victim.refresh_from_db()
        assert verify_password("acme_user-pw", victim.password)


class TestWrongPasswordDoesNotLogYouOut:
    """403 而不是 401 —— 判据在前端拦截器上，所以这条必须显式钉住状态码

    `frontend/src/services/api/axios.ts` 对任何非登录接口的 401 都会 `logout()` 并
    `window.location.replace` 到登录页。改密时复用 401 的 `InvalidCredentialsError`，
    "旧口令敲错一个字母"就会变成"当场掉线、页面上什么提示都留不下"。
    """

    def test_incorrect_password_error_is_403(self) -> None:
        assert IncorrectCurrentPasswordError.http_status == 403
        assert InvalidCredentialsError.http_status == 401
        assert IncorrectCurrentPasswordError.http_status != InvalidCredentialsError.http_status

    def test_it_is_an_app_error_so_the_handler_localizes_it(self) -> None:
        """不继承 `AppError` 的话全局处理器会把它当未捕获异常，回 500 + 一整条堆栈"""
        assert issubclass(IncorrectCurrentPasswordError, AppError)
        assert issubclass(EnvManagedPasswordError, AppError)

    async def test_the_endpoint_really_raises_that_class(self, db) -> None:  # noqa: ARG002
        with pytest.raises(IncorrectCurrentPasswordError) as exc_info:
            await change_own_password(
                req_data=req("nope", "whatever"),
                _current_user=await caller("acme_admin"),
            )
        assert exc_info.value.http_status == 403


class TestLegacyPasswordRowsCannotBeWalkedThrough:
    """存量脏数据不能变成绕过口令校验的后门"""

    async def test_empty_stored_hash_does_not_accept_an_empty_current_password(self, db) -> None:  # noqa: ARG002
        """空串的 bcrypt 哈希验空口令返回 True —— 不额外拦，"当前密码留空"正好能过

        同一个坑 `util.tenant_member_login()` 也踩过（它的对策是"空密码永远不接受"）。
        """
        row = await caller("legacy_empty_hash")
        # 先证明这条数据确实处在会出事的形态，否则下面的断言可能只是在验一个不存在的场景
        assert verify_password("", row.password)

        with pytest.raises(IncorrectCurrentPasswordError):
            await change_own_password(req_data=req("", "taken-over"), _current_user=row)
        await row.refresh_from_db()
        assert not verify_password("taken-over", row.password)

    async def test_malformed_stored_hash_is_a_403_not_a_500(self, db) -> None:  # noqa: ARG002
        """`verify_password("x", "not-a-hash")` 抛的是 `UnknownHashError`(ValueError)

        不接住的话它会穿过路由变成 500 —— 一个 4xx 的判定被记成服务器 bug，
        还会带一整条 `logger.exception` 堆栈。
        """
        row = await caller(BROKEN_HASH_USER)
        with pytest.raises(IncorrectCurrentPasswordError):
            await change_own_password(req_data=req("anything", "taken-over"), _current_user=row)
        await row.refresh_from_db()
        assert row.password == BROKEN_HASH_VALUE


# (存储的口令原文 or None 表示非法哈希, 送进来的当前口令, 期望匹配)
MATCH_TABLE = [
    pytest.param("secret", "secret", True, id="exact"),
    pytest.param("secret", "Secret", False, id="case"),
    pytest.param("secret", "", False, id="empty-candidate"),
    pytest.param("", "", False, id="empty-hash-empty-candidate"),
    pytest.param("", "secret", False, id="empty-hash-nonempty-candidate"),
    pytest.param(None, "secret", False, id="malformed-hash"),
    pytest.param(None, "", False, id="malformed-hash-empty-candidate"),
]


class TestCurrentPasswordMatcher:
    """比对函数自己的真值表：上面每条端点用例都压在它身上"""

    @pytest.mark.parametrize(("stored", "candidate", "expected"), MATCH_TABLE)
    def test_match_table(self, stored: Optional[str], candidate: str, expected: bool) -> None:
        user = DBUser(username="probe", password=BROKEN_HASH_VALUE if stored is None else get_hashed_password(stored))
        assert current_password_matches(user, candidate) is expected

    def test_a_blank_password_column_never_matches(self) -> None:
        """列本身是空串（不是空串的哈希）时 passlib 直接抛，这里必须先短路掉"""
        assert current_password_matches(DBUser(username="probe", password=""), "") is False
        assert current_password_matches(DBUser(username="probe", password=""), "x") is False

    def test_the_blank_column_short_circuit_does_not_lean_on_the_exception_handler(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`not user.password` 这条守卫不是 try/except 的重复写法

        今天两条路径看起来等价：passlib 把"这不是哈希"报成 `UnknownHashError`，而它恰好是
        `ValueError` 的子类，于是空口令列走异常分支也能收成 False。**这是巧合，不是契约** ——
        换个 passlib 版本或换掉 hash 后端，异常类型一变，只剩 try/except 的写法就会把
        "口令列是空串"变成 500。这条把短路钉在"调 passlib 之前"，与异常类型解耦。
        """

        def never_called(*_args: object, **_kwargs: object) -> bool:
            raise AssertionError("空口令列必须在触达 passlib 之前就短路掉")

        monkeypatch.setattr("nekro_agent.routers.user.verify_password", never_called)
        assert current_password_matches(DBUser(username="probe", password=""), "x") is False
        # 另一半：候选口令为空时同样不该走到 passlib
        assert current_password_matches(DBUser(username="probe", password=get_hashed_password("s")), "") is False


class TestPlatformAdminIsTold:
    """admin 的口令不在库里，回 200 是在撒谎"""

    async def test_admin_self_change_is_refused_with_a_reason(self, db) -> None:  # noqa: ARG002
        admin = await caller("admin")
        original = admin.password
        with pytest.raises(EnvManagedPasswordError) as exc_info:
            await change_own_password(req_data=req("admin-pw", "new-admin-pw"), _current_user=admin)
        assert exc_info.value.http_status == 403
        # 文案要指向真正能改的地方，否则用户只会反复重试
        assert "ADMIN_PASSWORD" in str(exc_info.value)

        await admin.refresh_from_db()
        assert admin.password == original

    async def test_even_with_the_correct_current_password(self, db) -> None:  # noqa: ARG002
        """挡在旧口令校验**之前**：admin 的库内哈希只是建号那一刻的快照，
        `ADMIN_PASSWORD` 轮换过之后它连"正确的当前口令"都验不过，
        放到校验之后会退化成一条看不懂的"当前密码不正确"。
        """
        with pytest.raises(EnvManagedPasswordError):
            await change_own_password(req_data=req("whatever", "x"), _current_user=await caller("admin"))


class TestEmptyNewPasswordIsRefused:
    """把口令改成空串等于锁死账号：`tenant_member_login()` 对空口令一律拒绝"""

    async def test_empty_new_password_is_a_400(self, db) -> None:  # noqa: ARG002
        me = await caller("acme_user")
        original = me.password
        with pytest.raises(AppValidationError) as exc_info:
            await change_own_password(req_data=req("acme_user-pw", ""), _current_user=me)
        assert exc_info.value.http_status == 400
        await me.refresh_from_db()
        assert me.password == original


class TestThereIsNoWayToNameSomeoneElse:
    """"改别人的 -> 拒"：本端点压根没有可以指向别人的参数

    改前有一个 `user_id`，但它从不生效（写入对象恒为 `_current_user`），
    于是平台方带着别人的 id 调用时改掉的是自己的口令、接口还回 200 —— 一条**静默做错事**
    的路径。字段现在从模型里去掉了，这一组钉住它别被谁"补回来"。
    """

    def test_the_request_model_has_no_target_field(self) -> None:
        assert "user_id" not in UpdatePassword.model_fields
        assert set(UpdatePassword.model_fields) == {"old_password", "password"}

    def test_a_stale_client_sending_user_id_is_not_rejected(self) -> None:
        """pydantic 默认忽略多余字段：老前端继续带 `user_id` 不会 422，只是不再有含义"""
        parsed = UpdatePassword.model_validate({"user_id": 9999, "old_password": "a", "password": "b"})
        assert not hasattr(parsed, "user_id")
        assert parsed.old_password == "a"

    async def test_a_payload_aimed_at_someone_else_still_only_changes_the_caller(self, db) -> None:  # noqa: ARG002
        """连平台方也一样：想改别人得走 `/user-manager/{user_id}/reset-password`"""
        victim = await caller("rival_admin")
        victim_hash = victim.password
        me = await caller("acme_admin")

        payload = UpdatePassword.model_validate(
            {"user_id": victim.id, "old_password": "acme_admin-pw", "password": "changed-by-me"},
        )
        await change_own_password(req_data=payload, _current_user=me)

        await victim.refresh_from_db()
        assert victim.password == victim_hash
        assert verify_password("rival_admin-pw", victim.password)
        await me.refresh_from_db()
        assert verify_password("changed-by-me", me.password)

    async def test_tenant_admin_cannot_use_it_to_take_over_a_member(self, db) -> None:  # noqa: ARG002
        """租户管理员不知道成员的口令时，只能走带守卫的重置端点，改不动这条"""
        member = await caller("acme_user")
        member_hash = member.password
        with pytest.raises(IncorrectCurrentPasswordError):
            await change_own_password(
                req_data=req("acme_user-pw-guess", "taken"),
                _current_user=await caller("acme_admin"),
            )
        await member.refresh_from_db()
        assert member.password == member_hash


class TestAdminResetIsADifferentEndpoint:
    """正向对照：管理员重置**别人**的口令不需要旧口令，且照常工作

    没有这一组，"给改密加旧口令校验"很容易被顺手推广到 `/user-manager/.../reset-password` 上，
    而那条端点的调用者按定义不知道目标的旧口令 —— 加上去等于把平台方的重置能力整个废掉。
    """

    async def test_platform_owner_resets_another_account_without_any_old_password(self, db) -> None:  # noqa: ARG002
        target = await caller("acme_user")
        result = await admin_reset_password(
            user_id=target.id,
            password="reset-by-platform",  # noqa: S106
            _current_user=await caller("admin"),
        )
        assert result.ok is True
        await target.refresh_from_db()
        assert verify_password("reset-by-platform", target.password)

    async def test_tenant_admin_resets_its_own_member_without_any_old_password(self, db) -> None:  # noqa: ARG002
        target = await caller("acme_user")
        await admin_reset_password(
            user_id=target.id,
            password="reset-by-tenant-admin",  # noqa: S106
            _current_user=await caller("acme_admin"),
        )
        await target.refresh_from_db()
        assert verify_password("reset-by-tenant-admin", target.password)

    async def test_the_reset_endpoint_signature_still_takes_no_old_password(self) -> None:
        import inspect

        parameters = set(inspect.signature(admin_reset_password).parameters)
        assert parameters == {"user_id", "password", "_current_user"}

    async def test_a_read_only_tenant_user_cannot_reset_anyone(self, db) -> None:  # noqa: ARG002
        """自助改密对只读成员开放，重置他人仍然不开放 —— 两条端点的角色门槛不一样"""
        with pytest.raises(PermissionDeniedError):
            await admin_reset_password(
                user_id=(await caller("acme_admin")).id,
                password="nope",  # noqa: S106
                _current_user=await caller("acme_user"),
            )


class TestTheResetEndpointIsNotABackDoorToSelf:
    """`PUT /user/password` 的当前口令校验有过**第二个出口**，这一组把它钉死

    ## 漏的是什么

    加了旧口令校验之后，"偷到一张令牌 → 改掉口令 → 永久占着账号"这条路仍然通着，
    只是换一条端点走：`POST /user-manager/{自己的 id}/reset-password` 改的是**同一个**
    `DBUser.password` 字段，却一个字的旧口令都不要。`assert_managed_user_accessible`
    的两条判据对"目标就是自己"恒成立（同租户显然成立；`target.perm_level > user.perm_level`
    自己对自己永远为假），所以它拦不住这一条。

    受影响的恰好是 `Role.Admin` —— **租户管理员本人**，定制版客户的主账号。
    `Role.User` 被 `perm_level < Role.Admin` 挡在门外，于是补丁前那道校验只对权限最低的
    那一档真正生效。这一组的每一条都是"如果只修了正门，它就会红"。

    ## 边界：平台方一侧**故意**不改

    见 `SelfResetMustUseSelfServiceError` 的 docstring。这里用 `test_the_platform_side_is_
    deliberately_unchanged` 正面钉住"这是决定，不是漏改"。
    """

    async def test_a_tenant_admin_cannot_reset_its_own_password_here(self, db) -> None:  # noqa: ARG002
        me = await caller("acme_admin")
        original = me.password

        with pytest.raises(SelfResetMustUseSelfServiceError):
            await admin_reset_password(
                user_id=me.id,
                password="attacker-owns-it",  # noqa: S106
                _current_user=me,
            )

        await me.refresh_from_db()
        assert me.password == original
        assert verify_password("acme_admin-pw", me.password)
        assert not verify_password("attacker-owns-it", me.password)

    async def test_both_doors_are_shut_for_the_same_stolen_token(self, db) -> None:
        """本轮安全主张的完整形态：拿着令牌但不知道口令，**两条**端点都改不动自己的口令

        只修正门时这条会在第二段变绿地走过去 —— 那正是补丁前的真实状态。
        """
        me = await caller("acme_admin")

        with pytest.raises(AppError):  # 正门：PUT /user/password
            await change_own_password(req_data=req("", "attacker-owns-it"), _current_user=me)

        with pytest.raises(AppError):  # 侧门：POST /user-manager/{自己}/reset-password
            await admin_reset_password(
                user_id=me.id,
                password="attacker-owns-it",  # noqa: S106
                _current_user=me,
            )

        await me.refresh_from_db()
        assert verify_password("acme_admin-pw", me.password), "口令被改掉了，说明还有第三个出口"

    async def test_the_refusal_is_403_and_does_not_log_you_out(self, db) -> None:  # noqa: ARG002
        """与 `IncorrectCurrentPasswordError` 同一条理由：401 会被 axios 拦截器变成掉线"""
        assert issubclass(SelfResetMustUseSelfServiceError, AppError)
        assert SelfResetMustUseSelfServiceError.http_status == 403
        assert SelfResetMustUseSelfServiceError.http_status != InvalidCredentialsError.http_status

    async def test_resetting_a_real_member_still_works(self, db) -> None:  # noqa: ARG002
        """反向对照：这条守卫**只**收自助那一格，管理员重置别人一点没受影响

        没有这一条，一个"永远抛 SelfResetMustUseSelfServiceError"的实现也能让上面全绿，
        代价是把租户管理员重置成员口令的能力整个废掉。
        """
        target = await caller("acme_user")
        result = await admin_reset_password(
            user_id=target.id,
            password="reset-by-tenant-admin",  # noqa: S106
            _current_user=await caller("acme_admin"),
        )
        assert result.ok is True
        await target.refresh_from_db()
        assert verify_password("reset-by-tenant-admin", target.password)

    async def test_the_platform_side_is_deliberately_unchanged(self, db) -> None:  # noqa: ARG002
        """平台方自助重置仍然放行，这是**取舍**不是漏改

        理由（写在 `SelfResetMustUseSelfServiceError` 的 docstring 里）：一张被偷的平台方
        令牌本来就能 `POST /user-manager/create` 建一个新的 `Role.Super` 账号，口令持久化
        不增加任何风险；而 `pages/user-manager/components/UserTable.tsx` 的重置按钮
        **每一行都渲染**，包括调用者自己那一行 —— 收掉会改变单部署形态下的现成工作流，
        与"平台方行为逐字不变"冲突。

        改了主意要一起改这条断言，别让它默默变成"被误伤了没人发现"。
        """
        platform_owner = await caller("admin")
        result = await admin_reset_password(
            user_id=platform_owner.id,
            password="platform-self-reset",  # noqa: S106
            _current_user=platform_owner,
        )
        assert result.ok is True
        await platform_owner.refresh_from_db()
        assert verify_password("platform-self-reset", platform_owner.password)

    async def test_the_guard_keys_on_tenant_scope_not_on_perm_level(self, db) -> None:  # noqa: ARG002
        """判据是"调用方有没有租户作用域"，**不是**"等级够不够"

        这条专门用来鉴别一种写错了但看起来能过的实现：把守卫写成
        `_current_user.perm_level < Role.Super`。在只有 `admin(Super)` /
        `acme_admin(Admin)` 的样本上它与正确实现结果完全一致 —— 于是上面那些断言
        一条都不会红，判据却已经从"归属"偷换成了"等级"。

        `legacy_platform_admin` 是拆开这两者的那一行：**无租户作用域**（放行）
        且 **perm_level 只有 Admin**（等级判据会拒）。两个 `Role.Admin` 走出相反结果，
        差别只来自 `tenant_id`。
        """
        rival = await caller("rival_admin")
        legacy = await caller("legacy_platform_admin")
        assert rival.perm_level == legacy.perm_level == Role.Admin, "样本失去鉴别力，这条测试变成空转"

        # 有作用域 → 拒
        with pytest.raises(SelfResetMustUseSelfServiceError):
            await admin_reset_password(user_id=rival.id, password="x", _current_user=rival)  # noqa: S106
        await rival.refresh_from_db()
        assert verify_password("rival_admin-pw", rival.password)

        # 无作用域 → 放行（等级完全相同）
        assert (await admin_reset_password(user_id=legacy.id, password="legacy-ok", _current_user=legacy)).ok  # noqa: S106
        await legacy.refresh_from_db()
        assert verify_password("legacy-ok", legacy.password)

    async def test_cross_tenant_self_lookalike_is_still_a_404(self, db) -> None:  # noqa: ARG002
        """新守卫排在归属判定**之后**：够不着的目标仍然是 404，不能被降级成 403

        403 会告诉对方"这个 user_id 存在"，而 404 不会（项目既定：越权 404，能力边界 403）。
        """
        victim = await caller("rival_admin")
        with pytest.raises(NotFoundError):
            await admin_reset_password(
                user_id=victim.id,
                password="cross-tenant",  # noqa: S106
                _current_user=await caller("acme_admin"),
            )
        await victim.refresh_from_db()
        assert verify_password("rival_admin-pw", victim.password)

    async def test_over_real_http_the_refusal_is_403_not_500(self, db) -> None:  # noqa: ARG002
        """真发一次请求：新错误类要能被全局处理器翻成 403，而不是漏成 500 + 堆栈"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from nekro_agent.core.exception_handlers import register_exception_handlers
        from nekro_agent.routers.user_manager import router as user_manager_router
        from nekro_agent.services.user.deps import get_current_active_user

        me = await caller("acme_admin")
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(user_manager_router, prefix="/api")
        app.dependency_overrides[get_current_active_user] = lambda: me
        client = TestClient(app, raise_server_exceptions=False)

        with client:
            refused = client.post(f"/api/user-manager/{me.id}/reset-password", json={"password": "pwned"})
            allowed = client.post(
                f"/api/user-manager/{(await caller('acme_user')).id}/reset-password",
                json={"password": "fine"},
            )

        assert refused.status_code == 403, refused.text
        assert refused.json()["error"] == "SelfResetMustUseSelfServiceError", refused.text
        # 对照组：同一个 app、同一个调用方，重置别人仍然 200 —— 证明不是整条路由都在报错
        assert allowed.status_code == 200, allowed.text

        await me.refresh_from_db()
        assert verify_password("acme_admin-pw", me.password)


class TestOverRealHttp:
    """真发一次 HTTP 请求，确认端点在 FastAPI 里是**接通的**

    直接 `await` 端点函数验不了这一层：依赖注入、请求体解析、`response_model`、
    以及"业务错误怎么变成状态码"全在 FastAPI 的栈里。PROJECT_STATE §2 里那条
    `@require_role` 参数名写错就 500 的坑，就是只有真发请求才看得见。
    本端点没用那个装饰器，但同一条规矩照跑：**任何一档都不能是 5xx**。
    """

    @staticmethod
    def _client(current_user: DBUser):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from nekro_agent.core.exception_handlers import register_exception_handlers
        from nekro_agent.routers.user import router as user_router
        from nekro_agent.services.user.deps import get_current_active_user

        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(user_router, prefix="/api")
        app.dependency_overrides[get_current_active_user] = lambda: current_user
        # `raise_server_exceptions=False`：未捕获异常要变成一条 500 响应而不是把测试炸掉，
        # 否则"端点 500 了"与"测试自己出错了"分不开
        return TestClient(app, raise_server_exceptions=False)

    async def test_the_four_outcomes_over_http(self, db) -> None:  # noqa: ARG002
        """一张表跑完四档：成功 / 当前口令错 / 新口令空 / admin"""
        cases = [
            ("acme_user", {"old_password": "acme_user-pw", "password": "http-new-pw"}, 200, None),
            ("acme_admin", {"old_password": "wrong", "password": "x"}, 403, "IncorrectCurrentPasswordError"),
            ("acme_admin", {"old_password": "acme_admin-pw", "password": ""}, 400, "ValidationError"),
            ("admin", {"old_password": "admin-pw", "password": "x"}, 403, "EnvManagedPasswordError"),
        ]
        for username, payload, expected_status, expected_error in cases:
            with self._client(await caller(username)) as client:
                response = client.put("/api/user/password", json=payload)
            assert response.status_code == expected_status, (username, response.status_code, response.text)
            assert response.status_code < 500, (username, response.text)
            body = response.json()
            if expected_error is None:
                assert body == {"ok": True}
            else:
                assert body["error"] == expected_error, (username, body)

        # 正向那一档要真的落库
        member = await caller("acme_user")
        await member.refresh_from_db()
        assert verify_password("http-new-pw", member.password)

    async def test_a_stale_client_still_gets_a_4xx_not_a_422(self, db) -> None:  # noqa: ARG002
        """老前端还带着 `user_id` 时不能被请求体校验拦成 422

        它带的是一个已经不存在的字段。pydantic 默认忽略多余字段，所以这条应该按
        "当前口令不正确"处理（403），而不是"请求格式不对"。
        """
        with self._client(await caller("acme_admin")) as client:
            response = client.put(
                "/api/user/password",
                json={"user_id": 1, "old_password": "wrong", "password": "x"},
            )
        assert response.status_code == 403
        assert response.json()["error"] == "IncorrectCurrentPasswordError"

    async def test_the_probe_itself_is_not_vacuous(self, db) -> None:  # noqa: ARG002
        """对照组：这个最小 app 真的把路由挂上了，而不是每一条都在打 404"""
        with self._client(await caller("acme_admin")) as client:
            assert client.get("/api/user/me").status_code == 200
            assert client.put("/api/user/no-such-endpoint", json={}).status_code == 404


class TestRoutePermissionRegistration:
    """登记表：这条端点是"自助"，不是"平台方专属"

    它此前登记在 `PLATFORM_ONLY` 里且**结构断言通过** —— 假阳性：`is_platform_only()`
    有一条"没装饰器但源码出现过 `Role.Super`"的分支，而当时函数体里那句 `Role.Super`
    属于一条授权旁路判断，不是门槛。契约测试于是宣称"租户碰不到"，实际人人可调。
    """

    def test_registered_as_self_service_not_platform_only(self) -> None:
        contract = _load_route_contract()
        key = ("user", "PUT", "/user/password")
        assert key in contract.TENANT_AGNOSTIC
        assert key not in contract.PLATFORM_ONLY

    def test_the_probe_can_actually_see_the_registry(self) -> None:
        """对照组：登记表真的读出来了，否则上面两条会在一个空集合上空转"""
        contract = _load_route_contract()
        assert ("user", "GET", "/user/me") in contract.TENANT_AGNOSTIC
        assert ("user", "DELETE", "/user/delete") in contract.PLATFORM_ONLY


# ---------------------------------------------------------------------------
# 前端接线
#
# 后端把改密修对了，前端够不着照样等于没有 —— 这一页此前就是这么坏的：组件写全了、
# 后端接口通着，只差 `router/index.tsx` 里没有任何一条路由指向它，于是整页是死代码。
# `tsc` / `eslint` / `vite build` 三道全绿（懒加载的 `import()` 只在被引用时才求值，
# 没人引用就没人报错），也没有任何一个前端测试 —— 一个字的运行时信号都没有。
#
# 判据按前端源码文本做，与 `tests/test_console_platform_only_paths.py` /
# `tests/test_upload_signed_url.py` 同一做法（本仓库前端没有测试设施）。
# ---------------------------------------------------------------------------

FRONTEND = Path("frontend/src")
ROUTER_TSX = FRONTEND / "router" / "index.tsx"
NAVIGATION_TSX = FRONTEND / "config" / "navigation.tsx"
ACCOUNT_PAGE = FRONTEND / "pages" / "profile" / "index.tsx"
COMMUNITY_PAGE = FRONTEND / "pages" / "cloud" / "profile.tsx"


def _platform_only_paths(navigation_source: str) -> List[str]:
    import re

    match = re.search(r"export const PLATFORM_ONLY_PATHS = \[(.*?)\] as const", navigation_source, re.S)
    assert match, "没找到 PLATFORM_ONLY_PATHS 数组——它被改名或删掉了，兜底拦截可能整条失效"
    return re.findall(r"'([^']+)'", match.group(1))


def _is_blocked(paths: List[str], candidate: str) -> bool:
    """复刻 `isPlatformOnlyPath()` 的前缀判据"""
    return any(candidate == prefix or candidate.startswith(f"{prefix}/") for prefix in paths)


class TestFrontendWiring:
    """`/profile` 必须指向账号页，而且必须对租户开着"""

    def test_both_pages_exist(self) -> None:
        assert ACCOUNT_PAGE.exists(), "登录者自己的账号页没了，改密入口无处可挂"
        assert COMMUNITY_PAGE.exists(), "社区账号页没了"

    def test_the_account_page_is_actually_routed(self) -> None:
        """这一条就是"死代码"本身：全库没有第二处引用 `pages/profile`

        懒加载的 `import('../pages/profile')` 不被任何路由引用时，编译期与运行期都没有信号。
        """
        routes = ROUTER_TSX.read_text(encoding="utf-8")
        assert "import('../pages/profile')" in routes
        assert "import('../pages/cloud/profile')" in routes

    def test_the_change_password_call_is_wired_to_the_routed_page(self) -> None:
        """接线的另一头：被路由到的那一页里真的有改密调用，而且带上了当前口令"""
        page = ACCOUNT_PAGE.read_text(encoding="utf-8")
        assert "authApi.updatePassword" in page
        assert "old_password" in page

    def test_profile_is_not_platform_only_anymore(self) -> None:
        """租户成员的自助改密是这条路径唯一的入口，登记进 `PLATFORM_ONLY_PATHS` 等于锁死

        `MainLayout` 对命中这份清单的路径直接渲染「这一页不属于你」，页面组件根本不挂载。
        """
        paths = _platform_only_paths(NAVIGATION_TSX.read_text(encoding="utf-8"))
        assert not _is_blocked(paths, "/profile")

    def test_the_community_profile_is_still_platform_only(self) -> None:
        """别把墙拆过头：社区账号页背后那几条接口都是平台方专属，租户进去只会看到一页 403"""
        paths = _platform_only_paths(NAVIGATION_TSX.read_text(encoding="utf-8"))
        assert _is_blocked(paths, "/cloud/profile")

    def test_the_account_menu_entry_has_no_visibility_gate(self) -> None:
        """菜单是第一层：格子藏起来的话，能进得去也等于没有入口"""
        import re

        source = NAVIGATION_TSX.read_text(encoding="utf-8")
        blocks = [block for block in re.findall(r"\{([^{}]*?)\}", source, re.S) if "path: '/profile'" in block]
        assert len(blocks) == 1, "`/profile` 的菜单格子不唯一或不存在，判据落点没了"
        assert "visibility:" not in blocks[0]

    def test_the_criterion_can_actually_see_a_regression(self) -> None:
        """§2.11：先证明这条判据测得出差异

        把 `/profile` 加回 `PLATFORM_ONLY_PATHS`（正是改前的状态），
        `test_profile_is_not_platform_only_anymore` 必须变红。不做这一步的话，
        一个永远返回 False 的 `_is_blocked` 也能让上面两条通过。
        """
        source = NAVIGATION_TSX.read_text(encoding="utf-8")
        broken = source.replace("  '/cloud',\n", "  '/cloud',\n  '/profile',\n", 1)
        assert broken != source, "样本没被改动，这条对照组什么都没验"
        paths = _platform_only_paths(broken)
        assert _is_blocked(paths, "/profile")
        # 另一半对照：解析器不是恒返回空表
        assert _is_blocked(paths, "/user-manager")
        assert len(paths) >= 5
