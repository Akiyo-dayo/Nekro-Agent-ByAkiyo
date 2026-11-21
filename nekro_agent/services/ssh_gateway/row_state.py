"""凭据行上那几个"网关写、后台看"的小字段

网关在 NAS 进程里跑，凭据行是后台和聊天登记共用的同一张表。连接成功、认证失败、
首次看到目标主机钥匙、接受新指纹，都要落回行上，后台才能看到"这把钥匙是什么时候
记下的""最近为什么连不上"。

## 为什么每个写入口都自己取一次行

网关不缓存凭据行：后台改一条、聊天登记一条，下一次连接就该看到新值。取行放在
`db_slot()` 里，和授权、审计共用同一个并发闸门，避免 SQLite 上同时开太多写事务。
`pin_host_key` 还要"先读再写"地保证首次记下只发生一次，所以它在模块级锁里做，
不依赖数据库的唯一约束——`host_keys` 是 JSON 列，没有可以加唯一索引的字段。

## 审计只记行状态的变化

`pin_host_key` / `append_host_key` 记 `host_key`，`mark_auth_failed` 记一条
`connect`（`detail=auth_failed`），三者都带行的归属快照。`touch_last_used` 与
`set_last_error` 只是时间戳和一句人看的说明，不单独记审计：连接成功/失败本身
已经有 `connect` 行了，再记一条只会把同一件事说两遍。
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any, Dict, List, Optional, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.services.ssh_gateway import audit_queue, events
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway.dbslot import db_slot
from nekro_agent.services.ssh_gateway.validation import host_key_fingerprint

logger = get_sub_logger("ssh_gateway.row_state")

_LAST_ERROR_LIMIT = 255

# 首次记下是"读空 -> 写一把"，必须串行；并发两个连接同时看到空行时，只让第一个写
_pin_lock = asyncio.Lock()

# 后台"测试连接"看到、但还没被接受的那把钥匙。只放进程内存，10 分钟过期：
# 接受接口要求请求体里的指纹与它逐字相同，避免点确认时目标已经换了钥匙。
_PENDING_TTL_SECONDS = 600.0
_pending_host_keys: Dict[int, Tuple[str, str, float]] = {}

# 后台「测试连接」登录成功过的行：id -> (当时的凭据与目标摘要, 过期时刻)。只放进程内存，
# 10 分钟有效；摘要对不上（凭据、主机、端口、账号改过）就不算
_test_passed: Dict[int, Tuple[str, float]] = {}


def credential_digest(row: Any) -> str:
    """凭据与目标的摘要：三列密文、主机、端口、账号（不含钉住的主机钥匙与状态）"""
    return _credential_digest(row)


def _credential_digest(row: Any) -> str:
    import hashlib

    parts = [
        str(getattr(row, "password_enc", "") or ""),
        str(getattr(row, "private_key_enc", "") or ""),
        str(getattr(row, "passphrase_enc", "") or ""),
        str(getattr(row, "host", "") or ""),
        str(getattr(row, "port", "") or ""),
        str(getattr(row, "username", "") or ""),
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8", "surrogatepass")).hexdigest()


def note_test_passed(row: Any) -> None:
    """测试连接用这组凭据登录成功：之后 10 分钟内可以不改凭据直接启用登不上的行"""
    _test_passed[int(getattr(row, "id"))] = (_credential_digest(row), _now_ts() + _PENDING_TTL_SECONDS)


def test_passed_recently(row: Any) -> bool:
    item = _test_passed.get(int(getattr(row, "id", 0) or 0))
    if item is None:
        return False
    digest, expires_at = item
    if _now_ts() >= expires_at:
        _test_passed.pop(int(getattr(row, "id")), None)
        return False
    return digest == _credential_digest(row)


def _now_ts() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def remember_pending_host_key(credential_id: int, entry: str, fingerprint: str) -> None:
    """记住"测试连接"这次看到的那把钥匙，等后台确认接受"""
    _pending_host_keys[int(credential_id)] = (str(entry), str(fingerprint), _now_ts() + _PENDING_TTL_SECONDS)


def get_pending_host_key(credential_id: int) -> Optional[Tuple[str, str]]:
    """取 10 分钟内最后一次测试看到的主机钥匙（entry, fingerprint）"""
    item = _pending_host_keys.get(int(credential_id))
    if item is None:
        return None
    entry, fingerprint, expires_at = item
    if _now_ts() >= expires_at:
        _pending_host_keys.pop(int(credential_id), None)
        return None
    return entry, fingerprint


def clear_pending_host_key(credential_id: int) -> None:
    _pending_host_keys.pop(int(credential_id), None)


def reset_pending_host_keys() -> None:
    """测试清空进程内待接受表与测试通过记录"""
    _pending_host_keys.clear()
    _test_passed.clear()


def _clip(value: str) -> str:
    return str(value or "")[:_LAST_ERROR_LIMIT]


def _row_snapshot(row: DBWsSshCredential) -> Dict[str, Any]:
    return {
        "workspace_id": int(row.workspace_id),
        "credential_id": int(row.id),
        "owner_kind": str(row.owner_kind or ""),
        "owner_account": str(row.owner_account or ""),
        "adapter_key": str(row.adapter_key or ""),
        "instance_key": str(row.instance_key or ""),
    }


def _audit_host_key(
    row: DBWsSshCredential,
    *,
    entry: str,
    actor: str,
    action: str,
) -> None:
    try:
        fingerprint = host_key_fingerprint(entry)
    except Exception:  # noqa: BLE001 - 指纹只是审计里的展示字段，算不出也不能挡住写行
        fingerprint = ""
    audit_queue.enqueue(
        **_row_snapshot(row),
        event=C.EVENT_HOST_KEY,
        allowed=True,
        reason="",
        task_ref="",
        lease_id="",
        conn_id="",
        triggered_by=[],
        channel_kind="",
        detail=str(action or ""),
        input_head="",
        exit_status=None,
        bytes_up=None,
        bytes_down=None,
        duration_ms=None,
        host_key_fp=fingerprint,
        actor=str(actor or ""),
    )


def _audit_auth_failed(row: DBWsSshCredential, *, actor: str) -> None:
    audit_queue.enqueue(
        **_row_snapshot(row),
        event=C.EVENT_CONNECT,
        allowed=False,
        reason=C.REASON_UPSTREAM_AUTH_FAILED,
        task_ref="",
        lease_id="",
        conn_id="",
        triggered_by=[],
        channel_kind="",
        detail="auth_failed",
        input_head="",
        exit_status=None,
        bytes_up=None,
        bytes_down=None,
        duration_ms=None,
        host_key_fp="",
        actor=str(actor or ""),
    )


async def pin_host_key(credential_id: int, entry: str, *, actor: str) -> bool:
    """`host_keys` 为空时记下这一把；已有钥匙则不动并返回 `False`

    只在"这一行还没有任何钥匙"时写，避免第一次连接恰好并发时后到的那个把
    别人的钥匙追加进去。写成功后清掉空字符串项（导入的老行可能留了空串）。
    """
    entry = str(entry or "").strip()
    if not entry:
        return False
    async with _pin_lock:
        async with db_slot():
            row = await DBWsSshCredential.get_or_none(id=int(credential_id))
            if row is None:
                return False
            current: List[str] = [str(item) for item in (row.host_keys or []) if str(item).strip()]
            if current:
                return False
            row.host_keys = [entry]
            await row.save(update_fields=["host_keys", "update_time"])
        _audit_host_key(row, entry=entry, actor=actor, action="pin")
    await events.notify_credential_changed(int(credential_id))
    try:
        shown = host_key_fingerprint(entry)
    except Exception:  # noqa: BLE001 - 日志里没有指纹也能记
        shown = ""
    logger.info(f"SSH 凭据 {credential_id} 首次记下目标主机钥匙：{shown}")
    return True


async def append_host_key(credential_id: int, entry: str, *, actor: str) -> None:
    """追加一把（去重、保序）；用于后台点击"接受新指纹\""""
    entry = str(entry or "").strip()
    if not entry:
        return
    async with db_slot():
        row = await DBWsSshCredential.get_or_none(id=int(credential_id))
        if row is None:
            return
        current: List[str] = [str(item) for item in (row.host_keys or []) if str(item).strip()]
        if entry in current:
            return
        row.host_keys = [*current, entry]
        await row.save(update_fields=["host_keys", "update_time"])
    _audit_host_key(row, entry=entry, actor=actor, action="append")
    await events.notify_credential_changed(int(credential_id))


async def mark_auth_failed(credential_id: int, message: str) -> None:
    """认证被拒：在用的行改 `auth_failed`，之后授权复核会按 `row_inactive` 拒绝

    只改状态，不清空凭据：后台要能看到原来填的是什么，改好再启用。只改 `active` 的行：
    停用的行只记一个「登不上」的原因（仍保持停用，以后启用前要先更新凭据），挂起的行
    状态不动——后台测试连接失败不能把挂起变成 `auth_failed`、再借改凭据回到 `active`，
    绕过「挂起是粘的」。
    """
    text = _clip(message)
    async with db_slot():
        row = await DBWsSshCredential.get_or_none(id=int(credential_id))
        if row is None:
            return
        changed = await DBWsSshCredential.filter(id=int(credential_id), status=C.STATUS_ACTIVE).update(
            status=C.STATUS_AUTH_FAILED,
            status_reason="",
            last_error=text,
        )
        if not changed:
            await DBWsSshCredential.filter(id=int(credential_id), status=C.STATUS_DISABLED).update(
                status_reason=C.STATUS_AUTH_FAILED,
                last_error=text,
            )
            await DBWsSshCredential.filter(
                id=int(credential_id),
                status__in=[C.STATUS_SUSPENDED, C.STATUS_AUTH_FAILED],
            ).update(last_error=text)
    _audit_auth_failed(row, actor="gateway")
    await events.notify_credential_changed(int(credential_id))
    if changed:
        logger.warning(f"SSH 凭据 {credential_id} 上游认证失败，行状态改为 auth_failed")


async def touch_last_used(credential_id: int) -> None:
    """登录成功：记一次最近使用时间，不改状态

    登录成功不代表这行"没问题"：可能是后台刚测试通过、还没被授权使用。状态只由
    启停与认证结果改。
    """
    async with db_slot():
        await DBWsSshCredential.filter(id=int(credential_id)).update(
            last_used_at=datetime.datetime.now(),
            last_error="",
        )


async def set_last_error(credential_id: int, message: str) -> None:
    """记一句最近失败原因；不写审计、不改状态

    网络错误、二次验证、主密钥解不开都可能走到这里。审计由调用方按事件记，
    这里只负责让后台行上有一句可读的说明。
    """
    async with db_slot():
        await DBWsSshCredential.filter(id=int(credential_id)).update(
            last_error=_clip(message),
        )


__all__ = [
    "append_host_key",
    "clear_pending_host_key",
    "credential_digest",
    "get_pending_host_key",
    "mark_auth_failed",
    "note_test_passed",
    "pin_host_key",
    "remember_pending_host_key",
    "reset_pending_host_keys",
    "set_last_error",
    "test_passed_recently",
    "touch_last_used",
]
