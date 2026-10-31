from typing import Any, Dict, Optional

from tortoise.exceptions import IntegrityError
from tortoise.timezone import now as db_now

from nekro_agent.core import logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    ConflictError,
    InvalidCredentialsError,
    OperationFailedError,
)
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.user import (
    UserCreate,
    UserLogin,
    UserToken,
)
from nekro_agent.services.user.auth import (
    build_pwd_gen_claim,
    create_access_token,
    create_refresh_token,
    get_hashed_password,
    verify_password,
)
from nekro_agent.services.user.perm import Role
from nekro_agent.services.user.tenant_perm import (
    TENANT_MAX_PERM_LEVEL,
    TenantSuspendedError,
    tenant_perm_level,
    tenant_role_of,
)


async def user_register(data: UserCreate) -> None:
    logger.info(f"正在注册用户 {data.username} ...")
    if data.username == "admin":
        raise ConflictError(resource="用户名")
    if await DBUser.get_or_none(adapter_key=data.adapter_key, platform_userid=data.platform_userid):
        raise ConflictError(resource="用户")
    try:
        await DBUser.create(
            username=data.username,
            password=get_hashed_password(data.password),
            adapter_key=data.adapter_key,
            platform_userid=data.platform_userid,
            perm_level=Role.User,
            login_time=db_now(),
        )
    except IntegrityError as e:
        # 联合键 (adapter_key, platform_userid) 的部分唯一索引兜底：并发下穿过上面
        # 那次存在性检查的另一路刚把同一行建出来，等价于「用户已存在」
        raise ConflictError(resource="用户") from e
    except Exception as e:
        logger.error(f"注册用户时发生错误: {e}")
        raise OperationFailedError(operation="注册用户") from e


async def get_or_create_platform_user(adapter_key: str, platform_userid: str, username: str) -> Optional[DBUser]:
    """入站消息的平台用户登记：存在即取回，不存在才建

    nonebot 按事件并发处理，同一新用户的首批消息会同时走到这里——"先查后建"输掉
    竞争的那一路靠 `IntegrityError` 之后的回读落回赢家那行（与 `instance.py` 的
    `_register_unclaimed_instance` 同一个手法）；真正的兜底是 `user` 表上
    `(adapter_key, platform_userid)` 的部分唯一索引（迁移 24）。

    Returns:
        用户行；`None` 表示**新建**时昵称被保留词护住——`user_login` 按 username 单列查
        "admin"，放进来一个同名平台用户会把后台登录炸成 MultipleObjectsReturned。
        保留名检查只拦建行：已登记的用户此后把平台昵称改成 "admin"，库里并不会多出
        同名行，必须照常取回，否则他的每条消息都被调用方静默丢弃。
    """
    existing = await DBUser.get_by_union_id(adapter_key, platform_userid)
    if existing is not None:
        return existing
    if username == "admin":
        logger.warning(f"昵称 'admin' 是平台方保留名，拒绝登记平台用户: {adapter_key}:{platform_userid}")
        return None
    try:
        return await DBUser.create(
            username=username,
            password=get_hashed_password(""),
            adapter_key=adapter_key,
            platform_userid=platform_userid,
            perm_level=Role.User,
            login_time=db_now(),
        )
    except IntegrityError:
        # 并发的另一条入站消息刚用同一联合键建好了行，回读它
        raced = await DBUser.get_by_union_id(adapter_key, platform_userid)
        if raced is None:
            raise
        return raced


def build_identity_claims(user: DBUser) -> Dict[str, Any]:
    """组装写进 JWT 的租户身份声明

    仅供前端读取，服务端鉴权一律以 `sub` 回查数据库为准（见 `auth.create_access_token`）。
    注意这里**不含** `pwd_gen`（口令世代）：那是鉴权声明，由 `auth.build_pwd_gen_claim`
    单独组装，两处登录在签发时把它并进同一份 `extra_claims`（见 `_token_extra_claims`）。
    """
    return {"tenant_id": user.tenant_id, "tenant_role": user.tenant_role}


def _token_extra_claims(user: DBUser) -> Dict[str, Any]:
    """登录签发时写进令牌的全部声明：租户身份（前端读）+ 口令世代（鉴权用）

    集中在一处，避免两条登录路径（admin / 租户成员）漏带 `pwd_gen`——
    缺这个 claim 的令牌会被 get_current_user 直接拒绝（fail-closed）。
    """
    return {**build_identity_claims(user), **build_pwd_gen_claim(user)}


async def tenant_member_login(data: UserLogin) -> UserToken:
    """租户成员登录

    与平台方 admin 的环境变量口令不同，租户成员比对数据库里存储的密码哈希。

    查询条件刻意收得很紧，两个条件都是安全边界而不是优化：

    - `adapter_key=""`：只认后台账号。聊天平台用户是消息入站时自动注册的
      （`adapters/interface/collector.py` 调用 `user_register`），它们的密码字段是
      **空串的哈希**，不加这个条件等于让任意群成员用空密码登录后台。
    - `tenant_id__isnull=False`：只认租户成员。未启用多租户的部署里这个集合恒为空，
      非 admin 登录仍然一律被拒，与引入多租户之前完全一致。
    """
    if not data.password:
        # 空密码永远不接受：历史遗留行里存在空串的 bcrypt 哈希，它能被空密码验过
        raise InvalidCredentialsError

    user = await DBUser.get_or_none(username=data.username, adapter_key="", tenant_id__isnull=False)
    if user is None or not user.password or not verify_password(data.password, user.password):
        raise InvalidCredentialsError
    if not user.is_active:
        logger.warning(f"被封禁的租户成员尝试登录: {user.username}")
        raise InvalidCredentialsError

    if not await DBTenant.is_tenant_active(user.tenant_id):
        logger.warning(f"停用租户的成员尝试登录: user={user.username}, tenant_id={user.tenant_id}")
        raise TenantSuspendedError

    # 兜底钉死权限上限：租户成员的 perm_level 绝不允许达到 Role.Super，
    # 否则它能通过总后台的容器接口操作宿主机。正常路径由 tenant_perm_level() 保证，
    # 这里再拦一次，防止手改数据库或历史数据把某个租户账号提权。
    if user.perm_level > TENANT_MAX_PERM_LEVEL:
        logger.warning(f"租户成员 {user.username} 的权限等级越界（{user.perm_level}），已下调至 {TENANT_MAX_PERM_LEVEL}")
        user.perm_level = tenant_perm_level(tenant_role_of(user))

    user.login_time = db_now()
    await user.save(update_fields=["perm_level", "login_time", "update_time"])
    return UserToken(
        access_token=create_access_token(user.username, extra_claims=_token_extra_claims(user)),
        refresh_token=create_refresh_token(user.username, extra_claims=_token_extra_claims(user)),
        token_type="bearer",
    )


async def user_login(data: UserLogin) -> UserToken:
    if data.username != "admin":
        return await tenant_member_login(data)
    if OsEnv.ADMIN_PASSWORD and data.username == "admin" and data.password == OsEnv.ADMIN_PASSWORD:
        user = await DBUser.get_or_none(username="admin")
        if not user:
            user = await DBUser.create(
                username="admin",
                password=get_hashed_password(data.password),
                adapter_key="",
                platform_userid="",
                # admin 是持有 ADMIN_PASSWORD 的宿主机管理员本人，权限上限即 Role.Super。
                # 总后台的实例与协议端容器生命周期属于宿主级操作，按 Role.Super 校验；
                # 这里若仍建成 Role.Admin，唯一可登录的账号会被自己的总后台挡在门外。
                perm_level=Role.Super,
                login_time=db_now(),
                # admin 是平台方本人，永远不绑定租户（这也是 tenant_id 的字段默认值）
                tenant_id=None,
                tenant_role=TenantRole.PLATFORM_OWNER.value,
            )
        elif user.perm_level < Role.Super:
            # 早期版本以 Role.Admin 建出的 admin 行需要就地补齐，理由同上
            user.perm_level = Role.Super
            await user.save(update_fields=["perm_level", "update_time"])
        return UserToken(
            access_token=create_access_token(data.username, extra_claims=_token_extra_claims(user)),
            refresh_token=create_refresh_token(data.username, extra_claims=_token_extra_claims(user)),
            token_type="bearer",
        )
    raise InvalidCredentialsError


async def user_change_password(user: DBUser, new_password: str) -> None:
    try:
        user.password = get_hashed_password(new_password)
        # 口令世代 +1：改密成功即吊销该用户此前签发的全部 access/refresh 令牌
        # （含改密者自己当前会话——下一次请求就会 401，属预期行为）
        user.password_generation += 1
        await user.save()
    except Exception as e:
        raise OperationFailedError(operation="修改密码") from e


async def user_delete(user: DBUser) -> None:
    try:
        await user.delete()
    except Exception as e:
        raise OperationFailedError(operation="删除用户") from e
