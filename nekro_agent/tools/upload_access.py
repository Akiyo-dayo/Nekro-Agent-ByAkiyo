"""聊天上传文件的取件签名

## 这一条要挡住什么

`GET /api/common/uploads/{chat_key}/{filename}` 要被 `<img src>` 直接加载，
所以它拿不到 `Authorization` 头，上游把它做成了完全公开的。路径穿越是堵住的
（两段都过 `Path(...).name`），但**知道两个名字就能取到任意租户的聊天文件**：
`chat_key` 形如 `onebot_v11-{instance_key}-group_{群号}` 是可枚举的，
文件名在同一条消息里出现过。改成签名 URL 之后，"猜到两个名字"不再足够。

## 为什么是"按频道签"而不是"按文件签"

授权判据本来就是按频道的（`chat_channel.assert_channel_accessible`）：
能看这个频道的人，能看这个频道的全部上传文件。所以令牌的作用域取成频道目录，
与授权边界完全对齐，不多给也不少给。

按文件签会让每一个签发点都要枚举文件名 —— 合并转发里的嵌套图片、
命令输出里临时物化的附件都要各自签一遍，而多签出来的权限并不比按频道少。

## 为什么不是"一次性"

一次性令牌会让图片的第二次加载直接裂掉：图片会被浏览器、图片预览弹窗、聊天客户端
反复加载（缓存重验证、换标签页重渲染都会再取一次）。这里要的是"短时效 + 不可枚举"。

其余三条同源：HMAC 签名、短过期、密钥由 `JWT_SECRET_KEY` 派生到固定用途串。
用途串不同 ⇒ 密钥不同，两边的令牌互不通用，其中一边的签名被离线分析也伪造不出另一边。

## 令牌为什么不带目录名

签名覆盖了目录名，但目录名不写进令牌 —— 校验方从**请求路径**里拿到它。
少一段 base64 让 URL 短一截，而且天然杜绝了"令牌里的目录"与"路径里的目录"
不一致时该信谁的问题：只有两者一致，签名才对得上。

## 过期时间为什么取整到时间窗

`exp` 由时间窗对齐（`_bucket_expiry`）而不是 `now + ttl`，于是同一个时间窗内
签出来的令牌**逐字节相同**。这不是为了省 CPU，是为了让 URL 在窗口内保持稳定：
URL 一变浏览器缓存就失效，图片会被重新下载一遍。同一个文件在命令输出里由服务端签、
在消息历史里由前端拼，两条路径也因此得到同一个 URL、共用同一份缓存。

代价是有效期不是恒定的 `TTL`，而是落在 `[TTL, 2*TTL]` 区间内 —— 这是刻意的，
它保证任何时刻拿到的令牌都至少还有一个完整时间窗可用，前端不需要精确对齐刷新时机。

## 进程重启

`JWT_SECRET_KEY` 默认每进程随机（见 `core/os_env.py`），所以重启后已签发的 URL 全部失效。
方向是 fail-closed，前端下一次取令牌就恢复，不作为缺陷处理。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional
from urllib.parse import quote

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.tools.path_convertor import sanitize_chat_key_for_path

logger = get_sub_logger("upload_access")

# 密钥派生的用途串。改动它会让所有已签发的取件 URL 立即失效，属于破坏性变更
UPLOAD_ACCESS_KEY_PURPOSE = b"nekro-agent/upload-access/v1"
TOKEN_PREFIX = "v1"
# 查询参数名。前端与服务端共用这一个常量，改名要两侧一起改
TOKEN_QUERY_PARAM = "sig"
# 时间窗长度：令牌在窗口内不变，实际有效期落在 [900, 1800) 秒
UPLOAD_ACCESS_WINDOW_SECONDS = 900
# 校验侧允许的最大剩余有效期。签发侧本来就不会超过 2 个窗口，
# 这条是纵深防御：万一签发侧被改出一个超长有效期，它也活不过这里
MAX_REMAINING_SECONDS = UPLOAD_ACCESS_WINDOW_SECONDS * 2
# 令牌长度上限，防止拿超长串反复喂解析
MAX_TOKEN_LENGTH = 256
# sha256 hexdigest 的固定长度。长度先对上，`compare_digest` 才只剩内容一个变量
DIGEST_HEX_LENGTH = 64


def _signing_key() -> bytes:
    """派生取件签名专用密钥"""
    return hmac.new(OsEnv.JWT_SECRET_KEY.encode("utf-8"), UPLOAD_ACCESS_KEY_PURPOSE, hashlib.sha256).digest()


def _digest(scope_dir: str, expiry: int) -> str:
    # 换行是分隔符：目录名经 `sanitize_chat_key_for_path` 后只含 [a-zA-Z0-9_.-]，
    # 不可能含换行，因此 (目录, 过期时间) 的拼接是无歧义的
    message = b"\n".join([UPLOAD_ACCESS_KEY_PURPOSE, scope_dir.encode("utf-8"), str(expiry).encode("ascii")])
    return hmac.new(_signing_key(), message, hashlib.sha256).hexdigest()


def _bucket_expiry(now: int) -> int:
    """把过期时间对齐到下一个时间窗边界之后再加一个窗

    加两个窗而不是一个：只加一个的话，窗口末尾签出的令牌会立刻过期。
    """
    return (now // UPLOAD_ACCESS_WINDOW_SECONDS + 2) * UPLOAD_ACCESS_WINDOW_SECONDS


def issue_upload_access_token(chat_key: str, *, now: Optional[int] = None) -> str:
    """为一个频道的上传目录签发取件令牌

    Args:
        chat_key: 频道标识，会按 `sanitize_chat_key_for_path` 规整为目录名后签名
        now: 当前时间（unix 秒），仅测试注入

    Returns:
        形如 `v1.{exp}.{hexdigest}` 的令牌
    """
    current = int(time.time()) if now is None else now
    scope_dir = sanitize_chat_key_for_path(chat_key)
    expiry = _bucket_expiry(current)
    return f"{TOKEN_PREFIX}.{expiry}.{_digest(scope_dir, expiry)}"


def token_expiry_seconds(token: str) -> int:
    """读出令牌里的过期时间戳，供接口把有效期回给前端

    只解析不校验：调用方是刚刚签发它的那一方。对不成形的串返回 0 而不是抛异常，
    `isascii()` 是这条契约的一部分：`str.isdigit()` 对 `²` 之类字符为真而 `int()` 不收。
    """
    parts = token.split(".")
    if len(parts) != 3 or not parts[1].isascii() or not parts[1].isdigit():
        return 0
    return int(parts[1])


def verify_upload_access_token(token: str, chat_key: str, *, now: Optional[int] = None) -> bool:
    """校验取件令牌是否覆盖该频道的上传目录

    对外只回布尔值：调用方一律翻译成 404，不区分"没签名"/"签错了"/"过期了"，
    以免把"这个频道存在"这件事泄露出去。具体原因记在服务端日志里。
    """
    current = int(time.time()) if now is None else now
    # 这个串完全由请求方给，而取件路由不带登录态：任何没被这里挡住的畸形输入
    # 都是一条**匿名可触发的 500**。所以先把字符集收死到 ASCII，再谈格式：
    # - `int()` 只认十进制数字，而 `str.isdigit()` 对 `²` / `①` 这类字符也为真，
    #   放过去就是 `ValueError`；
    # - `hmac.compare_digest()` 对含非 ASCII 字符的 str 直接抛 `TypeError`。
    # 两条都靠 `isascii()` 一次性堵死，剩下的判据才成立
    if not token or len(token) > MAX_TOKEN_LENGTH or not token.isascii():
        logger.debug("取件令牌缺失、超长或含非 ASCII 字符，拒绝")
        return False

    parts = token.split(".")
    if (
        len(parts) != 3
        or parts[0] != TOKEN_PREFIX
        or not parts[1].isdigit()
        or len(parts[2]) != DIGEST_HEX_LENGTH
    ):
        logger.debug("取件令牌格式非法，拒绝")
        return False

    expiry = int(parts[1])
    if expiry <= current:
        logger.debug("取件令牌已过期，拒绝")
        return False

    scope_dir = sanitize_chat_key_for_path(chat_key)
    if not hmac.compare_digest(_digest(scope_dir, expiry), parts[2]):
        logger.debug("取件令牌签名不匹配，拒绝")
        return False

    # 有效期上限放在验签**之后**：这条是纵深防御，针对的是"签发侧被改出一个超长有效期"，
    # 只有签名对得上时才说明确实是本服务签的，那才值得一条 WARNING。
    # 放在验签前的话，任何人随手发一个 `?sig=v1.9999999999.<64 位>` 就能匿名往日志里灌 WARNING
    if expiry - current > MAX_REMAINING_SECONDS:
        logger.warning(f"取件令牌有效期超出上限（剩余 {expiry - current}s），拒绝")
        return False

    return True


def build_signed_upload_url(chat_key: str, file_name: str, *, now: Optional[int] = None) -> str:
    """拼出带签名的取件 URL

    服务端主动把 URL 交给前端时用它（命令输出的富媒体附件）。前端自己拼 URL 的场景
    （消息历史）走 `/common/uploads-access-token` 拿令牌后在前端拼，两条路径在同一个
    时间窗内得到完全相同的 URL。
    """
    token = issue_upload_access_token(chat_key, now=now)
    path = f"/api/common/uploads/{quote(chat_key, safe='')}/{quote(file_name, safe='')}"
    return f"{path}?{TOKEN_QUERY_PARAM}={token}"
