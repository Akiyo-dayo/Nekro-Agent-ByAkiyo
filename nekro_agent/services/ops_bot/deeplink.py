"""运维写操作的确认深链：签发与校验

## 深链要挡住什么

IM 消息不是可信信道：它可以被伪造、被转发、被撤回后重放，也没有"你确定吗"这一步。
所以运维 Bot 在 IM 里**从不执行**写操作，只回一条链接。这条链接必须同时满足：

- **签名**：内容被改一个字节就失效，攻击者无法凭空造出一条"停用某租户"的链接
- **短过期**：默认 5 分钟，泄露到聊天记录/截图里也很快失效
- **一次性**：用过即作废，防止同一条链接被翻出来重放
- **落地仍校验身份**：链接本身不是凭据，打开后仍要求当前登录账号是平台方
  （`require_platform_owner()`）。链接转发给别人不会直接生效。

四条缺一不可：只有签名会被重放，只有过期会被在窗口内重放，
只有一次性但不校验身份等于把链接变成一张免登录通行证。

## 密钥与进程生命周期

签名密钥由 `OsEnv.JWT_SECRET_KEY` 派生（HMAC 到一个固定的用途串）。
派生而不是直接复用，是为了做**用途隔离**：即使运维深链的签名被离线分析，
也不能拿去伪造登录态 JWT，反之亦然。

`JWT_SECRET_KEY` 默认是**每进程随机**的（见 `core/os_env.py`），因此重启后
已签发但未使用的链接一律失效。这是 fail-closed 的正确方向，不作为缺陷处理。

## 一次性凭什么成立

签名本身是无状态的，无法表达"用过了"。所以额外维护一张进程内的待用表
（jti -> 过期时间）：签发时写入，消费时删除。校验要求 jti **仍在表里**，
于是"已使用"和"重启后失效"都收敛为同一个拒绝理由——这正是我们想要的，
对外不区分二者可以少泄露一点状态。

这与 `services/sandbox/credential_guard.py` 的授权票据、
`services/agent/session_token.py` 的会话令牌是同一类进程内注册表，
本项目单进程部署，不引入额外存储。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from enum import Enum
from typing import Dict, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

logger = get_sub_logger("ops_bot.deeplink")

# 密钥派生的用途串。改动它会让所有已签发链接立即失效，属于破坏性变更
DEEPLINK_KEY_PURPOSE = b"nekro-agent/ops-bot-deeplink/v1"
SIGNATURE_PREFIX = "v1="
# 单条链接允许的最大长度，防止拿超长串反复喂 base64 解码
MAX_TOKEN_LENGTH = 4096
# TTL 的硬边界：太短没法点，太长失去"短时效"的意义
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 3600


class OpsDeepLinkReason(str, Enum):
    """深链校验失败的原因"""

    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"


class OpsDeepLinkError(Exception):
    """深链校验失败

    刻意不继承 `AppError`：本异常会在路由层被翻译成统一的 400，
    但它也用于命令链路（非 HTTP），不应该带 HTTP 语义。
    """

    def __init__(self, reason: OpsDeepLinkReason):
        super().__init__(reason.value)
        self.reason = reason


class OpsDeepLinkPayload(BaseModel):
    """深链承载的内容

    字段名刻意短：它们会被 base64 塞进 URL，长度直接影响链接可用性。
    """

    v: int = Field(default=1, description="载荷版本")
    action: str = Field(..., description="写操作标识")
    target: str = Field(default="", description="操作目标，如实例标识或租户 ID")
    issuer_user_id: str = Field(..., description="在 IM 里发起该操作的账号")
    issuer_chat_key: str = Field(..., description="发起该操作的运维频道")
    jti: str = Field(..., description="一次性标识")
    exp: int = Field(..., description="过期时间（unix 秒）")


# jti -> 过期时间（unix 秒）。仅进程内有效，重启即清空
_pending_links: Dict[str, int] = {}


def _signing_key() -> bytes:
    """派生深链专用签名密钥"""
    return hmac.new(OsEnv.JWT_SECRET_KEY.encode("utf-8"), DEEPLINK_KEY_PURPOSE, hashlib.sha256).digest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_segment: str) -> str:
    digest = hmac.new(_signing_key(), payload_segment.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def _prune_expired(now: int) -> None:
    for jti in [jti for jti, exp in _pending_links.items() if exp <= now]:
        _pending_links.pop(jti, None)


def normalize_ttl_seconds(ttl_seconds: int) -> int:
    """把配置里的有效期夹到合理区间

    配置项由人手填，填 0 或填一天都可能发生。夹紧而不是报错，
    是因为一个越界的有效期不该让整条运维通道不可用。
    """
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, ttl_seconds))


def issue_ops_deeplink(
    *,
    action: str,
    target: str,
    issuer_user_id: str,
    issuer_chat_key: str,
    ttl_seconds: int,
    now: Optional[int] = None,
) -> Tuple[str, OpsDeepLinkPayload]:
    """签发一条一次性确认深链

    Args:
        action: 写操作标识，取值见 `actions.OPS_ACTIONS`
        target: 操作目标
        issuer_user_id: 在 IM 里发起该操作的账号
        issuer_chat_key: 发起该操作的运维频道
        ttl_seconds: 有效期，会被夹到 [30, 3600]
        now: 当前时间（unix 秒），仅测试注入

    Returns:
        (令牌, 载荷)
    """
    current = int(time.time()) if now is None else now
    _prune_expired(current)

    payload = OpsDeepLinkPayload(
        action=action,
        target=target,
        issuer_user_id=issuer_user_id,
        issuer_chat_key=issuer_chat_key,
        jti=secrets.token_urlsafe(16),
        exp=current + normalize_ttl_seconds(ttl_seconds),
    )
    payload_segment = _b64url_encode(payload.model_dump_json().encode("utf-8"))
    token = f"{payload_segment}.{_sign(payload_segment)}"

    _pending_links[payload.jti] = payload.exp
    logger.info(f"已签发运维确认深链: action={action} target={target or '-'} issuer={issuer_user_id} exp={payload.exp}")
    return token, payload


def _verify_token(token: str, now: int) -> OpsDeepLinkPayload:
    """校验签名、过期与一次性，但不消费"""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise OpsDeepLinkError(OpsDeepLinkReason.MALFORMED)

    payload_segment, separator, signature = token.partition(".")
    if not separator or not payload_segment or not signature:
        raise OpsDeepLinkError(OpsDeepLinkReason.MALFORMED)

    # 先验签再解析载荷：未验签的内容不该进 JSON 解析器
    if not hmac.compare_digest(_sign(payload_segment), signature):
        raise OpsDeepLinkError(OpsDeepLinkReason.BAD_SIGNATURE)

    try:
        payload = OpsDeepLinkPayload.model_validate_json(_b64url_decode(payload_segment))
    except (binascii.Error, ValueError, ValidationError) as exc:
        # 签名已通过却解不出载荷，说明是本进程签发的畸形内容，值得记一笔
        logger.warning(f"运维深链载荷解析失败: {exc}")
        raise OpsDeepLinkError(OpsDeepLinkReason.MALFORMED) from exc

    if payload.exp <= now:
        raise OpsDeepLinkError(OpsDeepLinkReason.EXPIRED)

    _prune_expired(now)
    if payload.jti not in _pending_links:
        # 已被消费，或进程重启后待用表已清空。两者对外不区分
        raise OpsDeepLinkError(OpsDeepLinkReason.ALREADY_USED)

    return payload


def peek_ops_deeplink(token: str, *, now: Optional[int] = None) -> OpsDeepLinkPayload:
    """校验但不消费，用于落地页展示"这条链接要做什么"

    Raises:
        OpsDeepLinkError: 校验不通过
    """
    return _verify_token(token, int(time.time()) if now is None else now)


def consume_ops_deeplink(token: str, *, now: Optional[int] = None) -> OpsDeepLinkPayload:
    """校验并消费，消费后同一条链接不可再用

    Raises:
        OpsDeepLinkError: 校验不通过
    """
    current = int(time.time()) if now is None else now
    payload = _verify_token(token, current)
    _pending_links.pop(payload.jti, None)
    return payload


def clear_pending_links() -> None:
    """清空待用表

    生产链路不调用；供测试隔离用例之间的状态。
    """
    _pending_links.clear()
