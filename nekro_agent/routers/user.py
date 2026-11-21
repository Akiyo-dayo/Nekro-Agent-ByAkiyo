from datetime import datetime, timedelta
from typing import ClassVar, Literal, cast

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    AppError,
    NotFoundError,
    PermissionDeniedError,
    TooManyAttemptsError,
    UnauthorizedError,
    ValidationError,
)
from nekro_agent.schemas.i18n import I18nDict, i18n_text
from nekro_agent.schemas.tenant import CurrentIdentity
from nekro_agent.schemas.user import (
    SUPPORTED_UI_LOCALES,
    UI_LOCALE_FIELD,
    UpdatePassword,
    UpdateUserLocaleRequest,
    UserCreate,
    UserLocaleResponse,
    UserLogin,
    UserToken,
)
from nekro_agent.services.user.auth import verify_password
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role, get_perm_role
from nekro_agent.services.user.tenant_perm import resolve_current_identity
from nekro_agent.services.user.util import (
    user_change_password,
    user_delete,
    user_login,
    user_register,
)

logger = get_sub_logger("auth")
router = APIRouter(prefix="/user", tags=["User"])

# 登录尝试记录：{username: {attempts: int, last_attempt: datetime}}
login_attempts = {}
# 最大尝试次数和锁定时间
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=30)

# 平台方 admin 的登录口令**不在数据库里**：`util.user_login()` 对这个用户名比对的是
# `OsEnv.ADMIN_PASSWORD`，DBUser 行上的哈希只是建号那一刻的快照，此后从不参与登录判定。
PLATFORM_ADMIN_USERNAME = "admin"


class IncorrectCurrentPasswordError(AppError):
    """改密时提供的当前口令不正确

    刻意**不复用** 401 的 `InvalidCredentialsError`：前端 axios 拦截器对任何非登录接口的
    401 一律清空登录态并跳回登录页（`frontend/src/services/api/axios.ts`），于是
    "旧口令打错一个字母"会变成"当场被踢下线"。语义上这里也不是"你没登录"，
    而是"你登录着，但这一次改密没通过二次确认"，与能力边界同属 403 一档。

    按约定业务错误应集中在 `schemas/errors.py`，这里就地定义的理由与
    `services/user/tenant_perm.py:TenantSuspendedError` 相同：不与并行开发的其他模块
    争抢同一个共享文件。全局异常处理器按 `AppError` 捕获，落在哪个模块不影响响应结构。
    """

    http_status: ClassVar[int] = 403
    i18n_message: ClassVar[I18nDict] = i18n_text(
        zh_CN="当前密码不正确",
        en_US="Current password is incorrect",
    )


class EnvManagedPasswordError(AppError):
    """该账号的口令由部署环境变量维护，改数据库没有意义

    只有 `admin` 落进这一档，理由见 `PLATFORM_ADMIN_USERNAME`。改前这个分支不存在：
    admin 调本接口会写一条新哈希、回 200，而下一次登录仍然只认 `ADMIN_PASSWORD` ——
    一个**报告成功却什么都没改**的出口。前端一直用 `isAdminUser` 在客户端拦着它，
    也就是说这条真相此前只写在前端。
    """

    http_status: ClassVar[int] = 403
    i18n_message: ClassVar[I18nDict] = i18n_text(
        zh_CN="平台方 admin 账号的登录口令由部署环境变量 ADMIN_PASSWORD 决定，请在部署配置中修改",
        en_US=(
            "The platform owner account's password is defined by the ADMIN_PASSWORD "
            "deployment variable; change it there"
        ),
    )


def current_password_matches(user: DBUser, candidate: str) -> bool:
    """比对 `candidate` 是不是 `user` 当前的口令

    两条兜底都是安全边界，不是防御性冗余：

    - `candidate` 为空一律不匹配。历史数据里存在**空串的 bcrypt 哈希**（聊天平台用户行
      就是这么建出来的），passlib 对它验空口令返回 True —— 不拦的话"不填当前密码"
      正好能通过校验，等于这次改动白做。同一个坑 `util.tenant_member_login()` 也踩过。
    - 存量口令字段不是合法哈希时按不匹配处理。`verify_password("x", "")` 抛的
      `UnknownHashError` 是 `ValueError` 的子类，不接住会把一个 403 变成 500。
    """
    if not candidate or not user.password:
        return False
    try:
        return verify_password(candidate, user.password)
    except (ValueError, TypeError):
        logger.warning(f"用户 {user.username} 的口令字段不是合法哈希，改密时按当前口令不匹配处理")
        return False


class ActionResponse(BaseModel):
    ok: bool = True


class UserInfoResponse(BaseModel):
    username: str
    userId: int
    perm_level: int
    perm_role: str


# 清理过期的登录尝试记录
def clean_expired_attempts():
    now = datetime.now()
    expired_users = []
    for username, data in login_attempts.items():
        if data["last_attempt"] + LOCKOUT_DURATION < now:
            expired_users.append(username)
    for username in expired_users:
        login_attempts.pop(username)


@router.post("/register", summary="用户注册", response_model=ActionResponse)
async def register(req_data: UserCreate) -> ActionResponse:
    if req_data.access_key != OsEnv.SUPER_ACCESS_KEY:
        raise UnauthorizedError
    await user_register(req_data)
    return ActionResponse(ok=True)


@router.post("/login", summary="用户登录", response_model=UserToken)
async def login(req_data: UserLogin) -> UserToken:
    # 清理过期的尝试记录
    clean_expired_attempts()

    # 检查当前用户是否已经尝试过多次
    if req_data.username in login_attempts:
        user_attempts = login_attempts[req_data.username]
        if user_attempts["attempts"] >= MAX_LOGIN_ATTEMPTS:
            now = datetime.now()
            lock_expires = user_attempts["last_attempt"] + LOCKOUT_DURATION
            if now < lock_expires:
                logger.warning(f"用户 {req_data.username} 尝试登录次数过多，账户被锁定到 {lock_expires}")
                raise TooManyAttemptsError
            # 锁定已过期，重置计数
            login_attempts.pop(req_data.username)

    try:
        login_token = await user_login(req_data)
        if req_data.username in login_attempts:
            login_attempts.pop(req_data.username)
        return login_token
    except AppError:
        now = datetime.now()
        if req_data.username not in login_attempts:
            login_attempts[req_data.username] = {"attempts": 1, "last_attempt": now}
        else:
            login_attempts[req_data.username]["attempts"] += 1
            login_attempts[req_data.username]["last_attempt"] = now
        logger.warning(
            f"用户 {req_data.username} 登录失败，当前尝试次数: {login_attempts[req_data.username]['attempts']}"
        )
        raise


@router.put("/password", summary="修改自己的登录口令", response_model=ActionResponse)
async def password(
    req_data: UpdatePassword,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """修改**自己**的登录口令，必须提供当前口令

    ## 改前是什么样

    ```python
    if req_data.user_id is not None and _current_user.id != req_data.user_id and ... < Role.Super:
        raise PermissionDeniedError
    await user_change_password(_current_user, req_data.password)   # 注意：永远是自己
    ```

    两个问题：

    1. **不校验旧口令。** 任何一次令牌泄露都能被就地升级成永久接管 —— 令牌会过期，
       改掉的口令不会。这是本次改动的主因。
    2. `user_id` 是个从不生效的字段。写入对象恒为 `_current_user`，所以平台方带别人的
       `user_id` 调用时改掉的是**自己**的口令，接口还回 200。它现在从模型里去掉了，
       重置他人口令请走 `POST /user-manager/{user_id}/reset-password`。

    ## 为什么这里不像 `/user-manager/.../reset-password` 那样免旧口令

    那条是**管理员重置别人**的口令，调用者按定义不知道目标的旧口令，判据是
    `assert_managed_user_accessible`（目标归属 + 目标等级）。本条是**自己改自己**，
    唯一能证明"是本人而不是捡到令牌的人"的东西就是当前口令。两条端点、两套判据，
    互不替代。

    ⚠️ 但它们**不是互不影响的** —— 这里曾经漏过一次。两条端点改的是同一个
    `DBUser.password` 字段，而 `assert_managed_user_accessible` 的两条判据对
    "目标就是调用者自己"恒成立（同租户显然成立；`target.perm_level > user.perm_level`
    自己对自己永远为假）。于是只加了本端点的校验时，租户管理员拿着一张偷来的令牌改打
    `POST /user-manager/{自己的 id}/reset-password`，不需要任何旧口令照样改掉自己的口令，
    下面这道校验一点作用都没起 —— 而受影响的恰好是 `Role.Admin`，也就是定制版客户的主账号
    （`Role.User` 被那条端点的 `perm_level < Role.Admin` 挡着，所以当时只有权限最低的
    那一档真正受保护）。那个出口现在收在
    `routers/user_manager.py:SelfResetMustUseSelfServiceError` 上，
    回归用例见 `tests/test_self_service_password_change.py:TestTheResetEndpointIsNotABackDoorToSelf`。
    **以后再往"改口令"这件事上加判据，先把两条端点一起数一遍。**

    ## 首次登录强制改密会不会被这条挡掉

    不会 —— 全库**没有**这种流程。OOBE（`frontend/src/pages/oobe`、`services/api/oobe.ts`）
    只配语言 / 模型组 / 云端 Key，一行都不碰口令；`DBUser` 上也没有 `must_change_password`
    之类的列。租户成员的初始口令由平台方在 `POST /console/tenants/{id}/members` 时设定，
    成员拿着它登录后到这里换掉，全程知道当前口令。

    ## 改密后吊销已签发的令牌（口令世代方案）

    按「口令世代」方案实现：

    - 用户行有 `password_generation`（迁移 25 补列），本端点改密成功时 +1
      （`util.user_change_password`），管理员重置口令同样 +1
      （`routers/user_manager.py:reset_password`）。
    - 令牌签发时带上签发那一刻的世代（`services/user/auth.py:build_pwd_gen_claim`
      的 `pwd_gen` claim，access 与 refresh 都带）。
    - `services/user/deps.py:get_current_user` 按 `sub` 回查用户行后纯内存比对世代，
      不一致即 401（`TokenRevokedError`，文案指向「请重新登录」）。**fail-closed**：
      缺 `pwd_gen` 声明的令牌也拒绝——所以部署本版后存量令牌全部失效、需重新登录
      （有意取舍：「缺声明就放行」会得到一个对存量令牌不生效的假吊销）。
    - 后果：**改密会踢掉包括自己在内的全部会话**，下一次请求即 401 → 前端跳回登录页。
      前端账号页若有「改密不会使当前登录失效」之类旧文案，需随本行为一并更新。

    仍未由吊销覆盖、另行登记的同源路径：租户管理员 `POST /console/tenants/{id}/members`
    建新成员——吊销解决不了「建一个新账号再进来」，要收紧的是成员创建本身。
    """
    # admin 的口令不在库里，写进去也不影响登录，所以这里必须显式说"做不到"而不是回 200
    if _current_user.username == PLATFORM_ADMIN_USERNAME:
        raise EnvManagedPasswordError
    if not req_data.password:
        # 空口令会把账号锁死：`tenant_member_login()` 对空口令一律拒绝，
        # 而空串的 bcrypt 哈希又恰好验得过空口令，两条合起来等于"谁都登不进来"
        raise ValidationError(reason="新密码不能为空")
    if not current_password_matches(_current_user, req_data.old_password):
        logger.warning(f"用户 {_current_user.username} 修改口令时当前口令校验失败")
        raise IncorrectCurrentPasswordError
    await user_change_password(_current_user, req_data.password)
    logger.info(f"用户 {_current_user.username} 已修改自己的登录口令")
    return ActionResponse(ok=True)


@router.get("/me", summary="用户个人信息", response_model=UserInfoResponse)
async def info(_current_user: DBUser = Depends(get_current_active_user)) -> UserInfoResponse:
    return UserInfoResponse(
        username=_current_user.username,
        userId=_current_user.id,
        perm_level=_current_user.perm_level,
        perm_role=get_perm_role(_current_user.perm_level),
    )


@router.get("/me/identity", summary="当前登录身份", response_model=CurrentIdentity)
async def me_identity(_current_user: DBUser = Depends(get_current_active_user)) -> CurrentIdentity:
    """当前登录身份，前端据此决定展示平台方 UI 还是租户 UI

    `accessible_instance_keys` 对平台方是空列表，语义为"不受限"而非"看不到任何实例"。
    未启用多租户时所有账号都是平台方，该接口的返回与单管理员时代等价。
    """
    return await resolve_current_identity(_current_user)


@router.get("/me/locale", summary="读取自己的界面语言", response_model=UserLocaleResponse)
async def get_my_locale(_current_user: DBUser = Depends(get_current_active_user)) -> UserLocaleResponse:
    """读取**自己**的界面语言，没设置过时返回 `None`

    存在 `ext_data` 里而不是加一列：界面语言是"这台账号的偏好"，不参与任何授权判定，
    为它走一次迁移（含回填与回滚路径）不划算，而 `ext_data` 本来就是按用户挂扩展数据的地方。

    读出来的值先按 `SUPPORTED_UI_LOCALES` 过一遍再返回。库里的值可能是旧版本写的、
    手工改的，或者两套文案里已经删掉的那一种；照直返回会让前端拿着一个查不到的语种去
    加载资源，界面回退成中文而设置页仍显示"已选英文"。这类"设置了但没生效"最难排查。

    `ext_data` 是 JSON 列，历史行上可能是 `None`、也可能被写成非对象的值（字符串、数组），
    两种都按"空扩展数据"处理——本函数只读，不该因为别的模块写坏了这一列而 500。
    """
    ext = _current_user.ext_data if isinstance(_current_user.ext_data, dict) else {}
    raw = ext.get(UI_LOCALE_FIELD)
    if isinstance(raw, str) and raw in SUPPORTED_UI_LOCALES:
        return UserLocaleResponse(locale=cast(Literal["zh-CN", "en-US"], raw))
    return UserLocaleResponse(locale=None)


@router.put("/me/locale", summary="保存自己的界面语言", response_model=ActionResponse)
async def update_my_locale(
    req_data: UpdateUserLocaleRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """保存**自己**的界面语言

    ## 为什么租户账号需要这条端点

    界面语言原先是全局配置 `SYSTEM_LANG`，只有平台方能改（`POST /config/batch/system`：
    `system` 是跨租户共享的作用域，租户写入一律 403）。租户账号改语言会当场 403，
    于是"切换语言"对它们等于不可用——而语言恰恰是
    每个账号自己的偏好，不该由平台方代劳、也不该全站统一。

    ## 为什么不复用全局配置

    `SYSTEM_LANG` 回答的是"这个部署默认说什么话"（例如系统消息、给未设置偏好的账号的
    回退值），本条回答的是"这个账号自己想看什么话"。两者一旦共用一个字段，任何租户改语言
    都会改掉全站默认值——那是跨租户越权，不只是体验问题。

    ## 没有跨账号目标标识

    请求体里没有 `user_id`、路径里也没有：写死的目标是 `_current_user`。这条端点挂在
    `TENANT_AGNOSTIC` 桶里（`tests/test_route_permission_contract.py`），任何登录账号都
    能用，包括只读成员——它改的只有自己那一行。

    ## 只改 `ext_data` 里的这一个键

    `ext_data` 是共享的扩展字典（成员创建时写进去的 `created_by` 就在里面）。整块覆盖会
    把那些字段一起抹掉，所以这里读出来、只替换 `ui_locale`、再整列写回。
    `update_fields` 显式列出 `update_time`：Tortoise 的 `save(update_fields=...)` 不会自动
    带上它，漏掉的话"最近修改时间"停在这一行上一次被别处写过的时刻。
    """
    ext = dict(_current_user.ext_data) if isinstance(_current_user.ext_data, dict) else {}
    ext[UI_LOCALE_FIELD] = req_data.locale
    _current_user.ext_data = ext
    await _current_user.save(update_fields=["ext_data", "update_time"])
    logger.info(f"用户 {_current_user.username} 已保存界面语言: {req_data.locale}")
    return ActionResponse(ok=True)


@router.delete("/delete", summary="删除用户", response_model=ActionResponse)
async def delete(_id: int, _current_user: DBUser = Depends(get_current_active_user)) -> ActionResponse:
    if int(str(_current_user.perm_level)) < Role.Super:
        raise PermissionDeniedError
    user = await DBUser.get_or_none(id=_id)
    if user is None:
        raise NotFoundError(resource="用户")
    await user_delete(user)
    return ActionResponse(ok=True)
