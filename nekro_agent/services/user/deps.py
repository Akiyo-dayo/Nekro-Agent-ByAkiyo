import hmac
from typing import Any, ClassVar, Mapping, Optional, Union

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError
from starlette.requests import HTTPConnection

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.auth_token import TokenData
from nekro_agent.schemas.errors import (
    AppError,
    PermissionDeniedError,
    TokenExpiredError,
    UnauthorizedError,
)
from nekro_agent.schemas.i18n import I18nDict, i18n_text
from nekro_agent.services.user.auth import PWD_GEN_CLAIM, expected_pwd_gen_claim
from nekro_agent.services.user.role import Role

logger = get_sub_logger("auth")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/token")


class TokenRevokedError(AppError):
    """令牌已被吊销：口令世代与用户行不一致，或令牌缺少世代声明

    改密（自助）/ 管理员重置口令成功后用户行 `password_generation` +1，该用户此前
    签发的全部令牌立即失效。沿用 401 +「请重新登录」文案（与 `TokenExpiredError`
    同档）：前端 axios 拦截器对非登录接口的 401 会清空登录态跳回登录页，这正是
    令牌被吊销时期望的行为，不能沿用「未授权访问」这种让用户摸不着头脑的文案。

    fail-closed 是有意取舍（项目所有者拍板）：本特性上线前签发的存量令牌都没有
    `pwd_gen` 声明，一律走这里——部署后所有已登录会话失效、需重新登录。反过来
    「缺声明就放行」会让吊销对存量令牌形同虚设（PROJECT_STATE §3 #54 的
    「半做更糟」警告）。就地定义而不进 `schemas/errors.py` 的理由与
    `routers/user.py:IncorrectCurrentPasswordError` 相同：不与并行开发的其他模块
    争抢同一个共享文件。
    """

    http_status: ClassVar[int] = 401
    i18n_message: ClassVar[I18nDict] = i18n_text(
        zh_CN="登录状态已失效，请重新登录",
        en_US="Your session is no longer valid, please log in again",
    )


def _pwd_gen_matches(claim: Any, expected: Union[int, str]) -> bool:
    """令牌里的 `pwd_gen` 与期望值是否一致——**类型严格**，比较方式随期望值的形态走

    - 期望值是 int（租户成员，逐字不变）：声明必须是 int，bool 不算（`True == 1` 会在世代 1 时蒙混过关）；
    - 期望值是 str（平台方 admin，`"<世代>.<口令指纹>"`）：声明必须是 str，且按**常数时间**比较
      （指纹是秘密派生值）。比较前先编码成字节：`compare_digest` 对非 ASCII 的 str 会抛 TypeError，
      而一张伪造令牌不该能靠它把拒绝变成 500。
    """
    if isinstance(expected, str):
        return isinstance(claim, str) and hmac.compare_digest(claim.encode("utf-8"), expected.encode("utf-8"))
    return not isinstance(claim, bool) and isinstance(claim, int) and claim == expected


def verify_password_generation(payload: Mapping[str, Any], user: DBUser) -> None:
    """令牌口令世代与用户行当前期望值比对，不符即抛 TokenRevokedError

    期望值来自 `auth.expected_pwd_gen_claim`——与签发（`auth.build_pwd_gen_claim`）**共用同一处逻辑**。
    租户成员是库内世代整数；平台方 admin 额外带 `ADMIN_PASSWORD` 的带密钥指纹，
    于是运维轮换环境变量口令后旧 admin 令牌立即失效（#54 ②）。

    - fail-closed：令牌缺少 `pwd_gen` 声明、或形态不对（对租户：不是整数，bool 也算不是——
      `True == 1` 会在世代 1 时蒙混过关；对 admin：不是 `世代.指纹` 字符串，旧格式的裸整数也不行），
      一律按已吊销拒绝。
    - 纯内存比对（指纹是两次 HMAC），**不引入任何额外数据库查询**——get_current_user 是每个已认证
      请求与每条 SSE 长连接的唯一咽喉，用户行本来就已按 `sub` 回查在手。
    - 将来任何用 refresh 票换新票的端点必须同样走这里：否则旧 refresh 票
      能在改密后续命，access 侧的吊销就被旁路了（当前仓库尚无该端点）。
    """
    pwd_gen = payload.get(PWD_GEN_CLAIM)
    expected = expected_pwd_gen_claim(user)
    if not _pwd_gen_matches(pwd_gen, expected):
        if isinstance(expected, str):
            # admin 的声明里带着口令指纹：虽然它是 HMAC、推不回口令，仍不进日志（调试日志也不行）
            logger.debug(f"Token of '{user.username}' rejected: pwd_gen claim is missing or stale")
        else:
            logger.debug(
                f"Token of '{user.username}' rejected: pwd_gen claim is missing or stale "
                f"(token={pwd_gen!r}, db={user.password_generation})",
            )
        raise TokenRevokedError


async def get_current_user(request: HTTPConnection, token: Optional[str] = None) -> DBUser:
    """
    Get current user from token.
    """
    try:
        # 首先尝试从 URL 参数获取 token
        url_token = request.query_params.get("token")
        if url_token:
            # ⚠️ 这里**不许**把 token 打出来。它是一张**当场可用**的访问令牌，而走 URL 传令牌的
            # 正是那几个 SSE 端点（`/logs/stream`、`/events`、命令输出流、NapCat 日志流），
            # 其中 `/logs/stream` 是 `Role.Super` —— 打出来等于把平台方的活令牌逐字写进
            # `app.log` / stdout / `/logs/download`，谁拿到日志谁就能免登录接管后台。
            # 只在 `APP_LOG_LEVEL=DEBUG` 时触发，所以它是隐患不是现状（测试部署实测 0 命中），
            # 但 `.env.example` 里 `LOG_LEVEL=DEBUG` 是默认写着的，开发机上就是现状。
            # 排障需要的信息（有没有拿到、长度对不对、前缀是不是 Bearer）全部保留。
            has_bearer_prefix = url_token.startswith("Bearer ")
            logger.debug(f"Token from URL: len={len(url_token)} bearer_prefix={has_bearer_prefix}")
            # 处理可能带有 Bearer 前缀的 token
            if has_bearer_prefix:
                url_token = url_token.split(" ")[1]
            token = url_token
        # 如果 URL 中没有 token，则尝试从 header 获取
        elif not token:
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.split(" ")[1]
            else:
                logger.debug("No valid token found in header")
                raise UnauthorizedError

        if not token:
            logger.debug("No token found in URL or header")
            raise UnauthorizedError

        payload = jwt.decode(token, OsEnv.JWT_SECRET_KEY, algorithms=[OsEnv.ENCRYPT_ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise UnauthorizedError
        token_data = TokenData(username=username)
    except ExpiredSignatureError as e:
        logger.debug(f"JWT expired: {e!s}")
        raise TokenExpiredError from e
    except JWTError as e:
        logger.debug(f"JWT validation failed: {e!s}")
        raise UnauthorizedError from e
    if token_data.username == "admin":
        user = await DBUser.get_or_none(username="admin")
    else:
        # 后台账号（admin 与租户成员）的 adapter_key 恒为空串；聊天平台用户是消息入站时
        # 自动注册的，用户名由平台昵称决定、可被任意伪造。不加这个条件，一个昵称与租户成员
        # 同名的群成员行就可能被令牌解析命中，造成身份错配。
        user = await DBUser.get_or_none(username=token_data.username, adapter_key="")
    if user is None:
        logger.debug(f"User '{token_data.username}' not found")
        raise UnauthorizedError
    # 口令世代比对放在按 sub 回查用户行**之后**：行已在手上，纯内存比对不增加任何查询。
    # 改密/重置后世代 +1，旧令牌（以及缺 pwd_gen 声明的存量令牌）在这里被拒。
    verify_password_generation(payload, user)
    return user


async def get_current_active_user(current_user: DBUser = Depends(get_current_user)) -> DBUser:
    """获取当前活跃用户"""
    if not current_user.is_active:
        raise PermissionDeniedError
    return current_user


async def get_current_super_user(current_user: DBUser = Depends(get_current_active_user)) -> DBUser:
    """获取当前超级用户"""
    if current_user.perm_level < Role.Super:
        logger.debug(f"User {current_user.username} is not a super user")
        raise PermissionDeniedError
    return current_user
