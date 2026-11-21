"""插件路由的短时效票据

## 为什么需要

`EventSource`（SSE）与 `<img src>` 都带不了 `Authorization` 请求头，而把长期登录
令牌拼进 URL 会把它写进访问日志、浏览器历史与 Referer。这张票据只解决"这一次建连"
的问题：有效期 120 秒、绑定用户 ID + 口令世代 + 插件键 + 实例对，泄漏后的窗口极短，
且改过口令（`password_generation` 变化）后旧票据立即失效。

## 格式与签名

`base64url(payload_json) + "." + hex(HMAC-SHA256(key, b"plugin-route-ticket\\x00" + payload_bytes))`

签名密钥与 `tools/upload_access.py` 的取件签名同源（同一个 `JWT_SECRET_KEY` 派生），
再用用途前缀做域分离：两边令牌互不通用，一边的签名被离线分析也伪造不出另一边。

票据里只放**标识**，不放任何配置或数据；实例归属与插件生效状态在验票之后重新查库。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.tools.upload_access import UPLOAD_ACCESS_KEY_PURPOSE

# 用途前缀：与取件令牌的用途串不同，两套签名互不通用
TICKET_SIGNING_PREFIX = b"plugin-route-ticket\x00"
TICKET_VERSION = 1
TICKET_TTL_SECONDS = 120
# 票据里只有几个短字符串与整数，正常长度远小于这个值；先收死长度，避免拿超长串反复解析
MAX_TICKET_LENGTH = 4096


def _signing_key() -> bytes:
    """派生插件路由票据专用密钥

    与 `tools/upload_access.py` 的 `_signing_key()` 同一个来源：都是拿 `JWT_SECRET_KEY`
    做一次 HMAC-SHA256。这里不直接 import 那个私有函数，但保持同一取法与同一个用途串，
    任何一处轮换密钥时两边一起失效。
    """
    return hmac.new(OsEnv.JWT_SECRET_KEY.encode("utf-8"), UPLOAD_ACCESS_KEY_PURPOSE, hashlib.sha256).digest()


def _sign(payload_bytes: bytes) -> str:
    return hmac.new(_signing_key(), TICKET_SIGNING_PREFIX + payload_bytes, hashlib.sha256).hexdigest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


def _now(now: Optional[int] = None) -> int:
    """当前 unix 秒；测试可注入固定时钟"""
    return int(time.time()) if now is None else int(now)


def issue_plugin_route_ticket(
    user: DBUser,
    plugin_key: str,
    adapter_key: str,
    instance_key: str,
    *,
    now: Optional[int] = None,
) -> str:
    """为一次短连接签发票据

    Args:
        user: 已登录用户行
        plugin_key: 插件键，票据只对这一个插件有效
        adapter_key: 实例对里的适配器标识；全局视图为空串
        instance_key: 实例标识；全局视图为空串
        now: 当前 unix 秒，仅测试注入

    Returns:
        `base64url(payload).hex_signature` 形式的票据
    """
    payload: Dict[str, Any] = {
        "v": TICKET_VERSION,
        "uid": int(user.id),
        "gen": int(user.password_generation),
        "pk": plugin_key,
        "ak": adapter_key or "",
        "ik": instance_key or "",
        "exp": _now(now) + TICKET_TTL_SECONDS,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"{_b64encode(payload_bytes)}.{_sign(payload_bytes)}"


def _decode_and_verify(ticket: str, *, now: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """验签并读出 payload；格式 / 签名 / 过期任一不对返回 None

    这里只判票据本身，用户是否仍然有效、有没有权限访问目标实例由调用方继续判。
    """
    if not isinstance(ticket, str) or not ticket or len(ticket) > MAX_TICKET_LENGTH or not ticket.isascii():
        return None
    parts = ticket.split(".")
    if len(parts) != 2:
        return None
    encoded, signature = parts
    if len(signature) != 64 or not encoded:
        return None
    try:
        payload_bytes = _b64decode(encoded)
    except (ValueError, binascii.Error):
        return None
    if not hmac.compare_digest(_sign(payload_bytes), signature):
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("v") != TICKET_VERSION:
        return None
    expiry = payload.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, int):
        return None
    if expiry <= _now(now):
        return None
    return payload


async def verify_plugin_route_ticket(
    ticket: str,
    plugin_key: str,
    adapter_key: str,
    instance_key: str,
    *,
    now: Optional[int] = None,
) -> Optional[int]:
    """校验票据并返回用户 ID；任何一项不满足都返回 None

    除了格式、签名、过期与插件键 / 实例对绑定之外，票据里还带着签发时的口令世代；
    调用方拿到 uid 后必须回查用户行。为了让"回查并比对世代"只有一个实现，这里一口气
    做完：用户被删除、被封禁、改过口令都返回 None。票据本身没有服务端状态，回查用户行
    是唯一的撤销点。

    Args:
        ticket: 待校验票据
        plugin_key: 这次请求要访问的插件键
        adapter_key: 这次请求的适配器标识
        instance_key: 这次请求的实例标识
        now: 当前 unix 秒，仅测试注入

    Returns:
        票据里 `uid` 对应用户仍然有效时返回用户 ID，否则返回 None
    """
    payload = _decode_and_verify(ticket, now=now)
    if payload is None:
        return None

    if payload.get("pk") != plugin_key:
        return None
    if payload.get("ak") != (adapter_key or ""):
        return None
    if payload.get("ik") != (instance_key or ""):
        return None

    uid = payload.get("uid")
    generation = payload.get("gen")
    if isinstance(uid, bool) or not isinstance(uid, int):
        return None
    if isinstance(generation, bool) or not isinstance(generation, int):
        return None

    user = await DBUser.get_or_none(id=uid)
    if user is None or not user.is_active:
        return None
    if int(user.password_generation) != generation:
        return None
    return uid
