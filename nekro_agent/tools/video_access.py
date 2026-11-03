"""WebUI 视频预览的取件签名

`<video src>` 带不了 `Authorization` 头，又不能把长效 JWT 放进 URL，所以与聊天上传文件一样用
短时效签名（`tools/upload_access.py`），差别是作用域更窄：

- 上传文件按频道签：能看频道的人能看全部文件，没有更细的边界。
- 视频每取一次都要向协议端要链接、再替调用方跑流量，而且链接背后是这个账号看得见的消息，
  所以绑定到 `(频道, 消息编号, 用户)`。换一条消息、换一个频道、换一个人都用不了。

密钥由 `JWT_SECRET_KEY` 派生到自己的用途串，与上传令牌、登录令牌互不通用。

过期时间按时间窗取整，一个时间窗内 URL 稳定，`<video>` 拖动时的多次 Range 请求用的是同一个地址。
播放到一半过期时，下一次 Range 会 404，前端重新取一个令牌接着放。

## 两段签名

令牌形如 `v1.{exp}.{uid}.{签名}.{世代签名}`：

- **签名**覆盖 `(频道, 消息, 用户, 过期时间)`，不需要查库就能验。接口不带登录态，伪造的令牌在这一步就被挡掉，
  一次数据库都不碰；用户号还要在 `DBUser.id` 列装得下的范围里（1 ~ 2^31-1），
  畸形的大数不会变成查库时的 500（SQLite 抛 OverflowError、PostgreSQL 抛 DataError）。
- **世代签名**在上面的基础上再签进签发那一刻的口令世代，取值与登录令牌的 `pwd_gen` 是同一个函数
  （`services/user/auth.py:expected_pwd_gen_claim`）。自助改密、管理员重置口令、admin 轮换环境变量口令之后，
  已经签出去的播放地址与登录令牌一起作废。世代只进 HMAC，不进 URL：admin 的世代里带口令指纹。
  它要用到用户行，所以排在第一段签名通过、按用户号取到行之后（`video_token_matches_generation`）。

验签通过之后接口还要再复核一遍用户此刻的状态（未封禁、至少 Admin、能访问这个频道）：
令牌在有效期内，用户可能已被封禁、降级或移出租户。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import NamedTuple, Optional, Union
from urllib.parse import quote

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

logger = get_sub_logger("video_access")

VIDEO_ACCESS_KEY_PURPOSE = b"nekro-agent/video-access/v1"
TOKEN_PREFIX = "v1"
# 查询参数名沿用 `sig`：访问日志脱敏按这个名字工作（`nekro_log_hygiene.py`、`core/logger.py`）
TOKEN_QUERY_PARAM = "sig"
# 时间窗长度：实际有效期落在 [600, 1200) 秒
VIDEO_ACCESS_WINDOW_SECONDS = 600
MAX_REMAINING_SECONDS = VIDEO_ACCESS_WINDOW_SECONDS * 2
MAX_TOKEN_LENGTH = 256
DIGEST_HEX_LENGTH = 64
# `DBUser.id` 是 `IntField`（32 位有符号）；更大的数不可能是签出去的，也不该拿去查库
MAX_USER_ID = 2**31 - 1

PasswordGeneration = Union[int, str]


class _TokenParts(NamedTuple):
    expiry: int
    user_id: int
    digest: str
    generation_digest: str


def _signing_key() -> bytes:
    return hmac.new(OsEnv.JWT_SECRET_KEY.encode("utf-8"), VIDEO_ACCESS_KEY_PURPOSE, hashlib.sha256).digest()


def _fields(chat_key: str, message_id: str, user_id: int, expiry: int) -> bytes:
    # 换行不会出现在这几个字段里：`chat_key` 来自路径且已确认存在，`message_id` 只含数字
    return b"\n".join(
        [
            VIDEO_ACCESS_KEY_PURPOSE,
            chat_key.encode("utf-8"),
            message_id.encode("ascii"),
            str(user_id).encode("ascii"),
            str(expiry).encode("ascii"),
        ],
    )


def _digest(fields: bytes) -> str:
    return hmac.new(_signing_key(), fields, hashlib.sha256).hexdigest()


def _generation_digest(fields: bytes, generation: PasswordGeneration) -> str:
    # 世代带上类型：整数 3 与字符串 "3" 不是同一个世代
    tagged = fields + b"\ngeneration\n" + f"{type(generation).__name__}:{generation}".encode("utf-8")
    return hmac.new(_signing_key(), tagged, hashlib.sha256).hexdigest()


def issue_video_access_token(
    chat_key: str,
    message_id: str,
    user_id: int,
    *,
    generation: PasswordGeneration,
    now: Optional[int] = None,
) -> str:
    """签发令牌，形如 `v1.{exp}.{uid}.{签名}.{世代签名}`

    `generation` 取 `expected_pwd_gen_claim(user)`，与登录令牌同一个来源；它只进签名，不进令牌。
    """
    current = int(time.time()) if now is None else now
    expiry = (current // VIDEO_ACCESS_WINDOW_SECONDS + 2) * VIDEO_ACCESS_WINDOW_SECONDS
    fields = _fields(chat_key, message_id, user_id, expiry)
    return f"{TOKEN_PREFIX}.{expiry}.{user_id}.{_digest(fields)}.{_generation_digest(fields, generation)}"


def video_token_expiry(token: str) -> int:
    """读出过期时间，只给刚签发它的一方用；不成形时返回 0"""
    parts = token.split(".")
    if len(parts) != 5 or not parts[1].isascii() or not parts[1].isdigit():
        return 0
    return int(parts[1])


def _parse(token: str, current: int) -> Optional[_TokenParts]:
    """只看格式与过期时间，不验签

    字符集先收死到 ASCII，理由见 `upload_access.verify_upload_access_token`：
    这个串完全由请求方给，而接口不带登录态，畸形输入不能变成匿名可触发的 500。
    """
    if not token or len(token) > MAX_TOKEN_LENGTH or not token.isascii():
        return None
    parts = token.split(".")
    if (
        len(parts) != 5
        or parts[0] != TOKEN_PREFIX
        or not parts[1].isdigit()
        or not parts[2].isdigit()
        or len(parts[3]) != DIGEST_HEX_LENGTH
        or len(parts[4]) != DIGEST_HEX_LENGTH
    ):
        return None
    expiry = int(parts[1])
    user_id = int(parts[2])
    # 令牌总长不超过 256，int() 的开销可以忽略；要挡的是“签名通过之后拿一个装不进列的数去查库”
    if not 0 < user_id <= MAX_USER_ID:
        return None
    if expiry <= current:
        logger.debug("视频取件令牌已过期，拒绝")
        return None
    return _TokenParts(expiry, user_id, parts[3], parts[4])


def verify_video_access_token(token: str, chat_key: str, message_id: str, *, now: Optional[int] = None) -> Optional[int]:
    """校验第一段签名，通过时返回签发时的用户 ID，否则返回 None。不查库

    调用方一律翻译成 404，不区分没签名、签错了、过期了，以免泄露频道与消息是否存在。
    通过之后还要按用户 ID 取行、用它此刻的口令世代过 `video_token_matches_generation`。
    """
    current = int(time.time()) if now is None else now
    if not message_id.isascii():
        return None
    parts = _parse(token, current)
    if parts is None:
        return None
    if not hmac.compare_digest(_digest(_fields(chat_key, message_id, parts.user_id, parts.expiry)), parts.digest):
        logger.debug("视频取件令牌签名不匹配，拒绝")
        return None
    if parts.expiry - current > MAX_REMAINING_SECONDS:
        logger.warning(f"视频取件令牌有效期超出上限（剩余 {parts.expiry - current}s），拒绝")
        return None
    return parts.user_id


def video_token_matches_generation(
    token: str,
    chat_key: str,
    message_id: str,
    *,
    generation: PasswordGeneration,
    now: Optional[int] = None,
) -> bool:
    """第二段签名：令牌签发时的口令世代是否等于 `generation`（令牌声称的那个用户**此刻**的世代）"""
    if not message_id.isascii():
        return False
    parts = _parse(token, int(time.time()) if now is None else now)
    if parts is None:
        return False
    fields = _fields(chat_key, message_id, parts.user_id, parts.expiry)
    if not hmac.compare_digest(_generation_digest(fields, generation), parts.generation_digest):
        logger.debug("视频取件令牌签发后改过口令，拒绝")
        return False
    return True


def build_video_url(chat_key: str, message_id: str, token: str) -> str:
    return f"/api/chat-channel/{quote(chat_key, safe='')}/messages/{message_id}/video?{TOKEN_QUERY_PARAM}={token}"
