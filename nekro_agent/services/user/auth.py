import hashlib
import hmac
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from jose import JWTError, jwt
from passlib.context import CryptContext

from nekro_agent.core.os_env import OsEnv

if TYPE_CHECKING:
    from nekro_agent.models.db_user import DBUser

ACCESS_TOKEN_EXPIRE_MINUTES = 24 * 60  # 1 day
REFRESH_TOKEN_EXPIRE_MINUTES = 60 * 24 * OsEnv.ACCESS_TOKEN_EXPIRE_DAYS
JWT_SECRET_KEY = OsEnv.JWT_SECRET_KEY
JWT_REFRESH_SECRET_KEY = OsEnv.JWT_REFRESH_SECRET_KEY

password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 口令世代 claim 的键名。get_current_user 对缺这个 claim 的令牌一律拒绝（fail-closed），
# 因此**所有** access / refresh 签发路径都必须带上它（见 build_pwd_gen_claim）。
PWD_GEN_CLAIM = "pwd_gen"

# 平台方 admin 的用户名。`expected_pwd_gen_claim` 按它分流 `pwd_gen` 的形态（与 `deps.get_current_user`
# 里 `sub == "admin"` 取 admin 行是同一个判据）
ADMIN_USERNAME = "admin"

# admin 口令指纹的密钥派生用途串。改它 = 所有 admin 令牌当场失效（属破坏性变更），与取件签名各用各的串
ADMIN_FINGERPRINT_PURPOSE = b"nekro-agent/admin-password-fingerprint/v1"


def get_hashed_password(password: str) -> str:
    """获取加密后的密码"""

    return password_context.hash(password)


def verify_password(password: str, hashed_pass: str) -> bool:
    """验证密码"""

    return password_context.verify(password, hashed_pass)


def admin_password_fingerprint() -> str:
    """平台方 admin 当前口令（`OsEnv.ADMIN_PASSWORD`）的带密钥指纹，128 位十六进制

    admin 的口令**不在库里**（`user_login` 只比对环境变量），库内世代管不到「运维轮换了环境变量」
    这件事。把这份指纹并进 admin 的 `pwd_gen`，口令一换指纹就变，此前签出的 admin 令牌随之失效。

    **为什么是 HMAC 而不是口令的哈希**：令牌载荷是公开可读的（JWT 只签名不加密），
    无密钥哈希放进去等于把离线撞库的靶子贴在每一张令牌上。这里 `HMAC(HMAC(JWT 密钥, 用途串), 口令)`：
    不知道 JWT 密钥就推不回口令，而知道 JWT 密钥的人本来就能自签 admin 令牌。
    用途串与 `upload_access` 同一种派生套路（HMAC 到固定用途串），
    各用途的密钥互不相通。

    调用时读 `OsEnv`、不缓存：测试要能换口令 / 换密钥（§2.9），产品里两个值进程内也不会变。
    口令为空时照算（空串的 HMAC），不特殊处理：admin 此时本就登不上，而「从有口令改成空」同样应当吊销旧票。
    """
    key = hmac.new(OsEnv.JWT_SECRET_KEY.encode("utf-8"), ADMIN_FINGERPRINT_PURPOSE, hashlib.sha256).digest()
    return hmac.new(key, OsEnv.ADMIN_PASSWORD.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def expected_pwd_gen_claim(user: "DBUser") -> Union[int, str]:
    """某个用户行**此刻**应当对应的 `pwd_gen` 声明值——签发与校验共用的**唯一**一处逻辑

    - 租户成员（以及一切非 `admin` 的行）：库内 `password_generation` 裸整数，**逐字不变**；
    - 平台方 `admin`：`"<库内世代>.<口令指纹>"` 字符串（见 `admin_password_fingerprint`）。
      库内世代仍在，所以 user-manager 自重置（世代 +1）这条老的吊销路径照旧生效。

    分流判据是用户名恰为 `admin`，与 `deps.get_current_user` 里 `sub == "admin"` 取 admin 行是同一个判据
    （按 sub 回查到的行，签发时用的是同一行）。签发（`build_pwd_gen_claim`）与校验
    （`deps.verify_password_generation`）都只调用本函数：两侧各算一遍的后果是签出来的票自己验不过，
    或验的比签的松。
    """
    generation = int(user.password_generation)
    if user.username == ADMIN_USERNAME:
        return f"{generation}.{admin_password_fingerprint()}"
    return generation


def build_pwd_gen_claim(user: "DBUser") -> Dict[str, Any]:
    """组装口令世代声明（`pwd_gen`）：令牌签发那一刻的期望值（见 `expected_pwd_gen_claim`）

    这是**鉴权用途**的声明，与 `util.build_identity_claims` 里那些仅供前端读取的
    租户身份声明不同：改密/重置会把用户行世代 +1（admin 还会因 `ADMIN_PASSWORD` 轮换而指纹改变），
    于是带旧值的令牌在 `deps.get_current_user` 的比对中立即失效。缺这个 claim 的令牌会被直接拒绝
    （fail-closed，存量令牌部署后全部失效需重新登录），所以新增任何签发路径时
    都必须把它并进 `extra_claims`——两处现有调用见 `util.py` 的两个登录函数。
    """
    return {PWD_GEN_CLAIM: expected_pwd_gen_claim(user)}


def create_access_token(
    subject: Union[str, Any],
    expires_delta: Optional[int] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    """创建token

    `extra_claims` 用于附加租户身份（`tenant_id` / `tenant_role`）等非鉴权用途的声明。
    鉴权仍以 `sub` 回查数据库为准，令牌里的租户信息只是给前端省一次请求，
    不要在服务端把它当作权限判定依据（用户改组后旧令牌里的值会过期）。
    唯一的例外是 `pwd_gen`（口令世代，见 `build_pwd_gen_claim`）：它参与
    get_current_user 的吊销比对，签发时必须带上，否则令牌一律无效。
    """

    if expires_delta is not None:
        expires_delta = datetime.utcnow() + expires_delta  # type: ignore
    else:
        expires_delta = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)  # type: ignore

    to_encode: Dict[str, Any] = {**(extra_claims or {}), "exp": expires_delta, "sub": str(subject)}
    return jwt.encode(to_encode, JWT_SECRET_KEY, OsEnv.ENCRYPT_ALGORITHM)


def create_refresh_token(
    subject: Union[str, Any],
    expires_delta: Optional[int] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    """创建refresh token

    与 access token 同样要求 `extra_claims` 带上 `pwd_gen`（口令世代）：
    将来任何用 refresh 票换新票的端点都必须先过 `deps.verify_password_generation`，
    否则改密吊销对 refresh 票不生效，旧票仍能续命。
    """

    if expires_delta is not None:
        expires_delta = datetime.utcnow() + expires_delta  # type: ignore
    else:
        expires_delta = datetime.utcnow() + timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES)  # type: ignore

    to_encode: Dict[str, Any] = {**(extra_claims or {}), "exp": expires_delta, "sub": str(subject)}
    return jwt.encode(to_encode, JWT_REFRESH_SECRET_KEY, OsEnv.ENCRYPT_ALGORITHM)


async def get_user_from_token(token: str) -> bool:
    """从token验证用户"""
    try:
        jwt.decode(token, OsEnv.JWT_SECRET_KEY, algorithms=[OsEnv.ENCRYPT_ALGORITHM])
    except JWTError:
        return False
    else:
        return True
