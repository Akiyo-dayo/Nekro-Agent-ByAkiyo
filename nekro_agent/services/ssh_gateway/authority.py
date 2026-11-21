"""SSH 网关唯一的授权入口

## 这是 E2 唯一该问的地方

网关服务端（对容器的那一头）不直接读租约、不直接读凭据表、不自己判名单。它只需在
SSH 握手拿到「本地地址、来源地址、客户端公钥、别名」之后调用
:func:`authorize_connection`，拿回一个 `Decision`：放行时 `target` 里是登录目标所需的
全部信息，拒绝时 `reason` 是审计与横幅共用的理由码。所有判定顺序、缓存、失败爆破、
审计都收在这一个模块里，E2 不必重复实现一遍，也不会因为某处漏判而多开一条路。

## 为什么顺序是这样

先判便宜的、不查库的：总开关、登记表、隔离标记、镜像标记、客户端钥匙。这些都在内存
里，一次比较就能拒掉绝大多数垃圾连接。

再判要查库的：别名行、租约、复核。其中租约与复核会读工作区、名单、实例状态，放在
钥匙校验之后，避免一把不对的钥匙也能触发数据库查询。

最后才解密三个凭据列。解密是唯一会把明文带进内存的一步，放在所有授权条件都通过之后，
拒绝路径上就不会碰到任何凭据值。

## 失败爆破

同一工作区 5 分钟内累计 10 次认证类失败（钥匙不对、别名不存在），接下来 10 分钟一律
`limit_exceeded`。只统计这两类：来源地址不对的连接连是哪个工作区都不确定，拿它计数会
让任何一个能访问网桥的人替某个工作区"上锁"；而钥匙与别名是容器里的 CC 才能送出来的，
更接近真实的爆破。

## 缓存

`recheck_channel` 的结果缓存 5 秒：每开一个通道都查库会把连接池占满。缓存键里带着
两份名单与总开关的值，名单一改就是新键，不用等 5 秒；凭据变更通知会立刻清掉相关的
缓存项，所以"停用一行 → 下一个通道被拒"也不需要等缓存过期。作废时递增一个代数，
作废之前就开始、作废之后才算完的复核结果不再写回缓存。

## 别名先看形状再查库

别名来自对端，可以是任意字节（含 NUL）。不合别名规则的直接按 `unknown_alias` 拒，
不拿它去查库：PG 遇到 NUL 会报错，而报错会被算成一次「意外」计入熔断，任何工作区都能
借此让整个网关熔断。

## 锁住期间的拒绝审计限流

失败爆破被锁住以后，每次尝试都写一行审计会让一个工作区把审计队列一直塞满（队列满了
所有工作区的新连接都会被拒）。锁住期间每个工作区每分钟只留一行。

## 审计

每次调用都经 :mod:`audit_queue` 记一行 `connect`（放行或拒绝、理由、任务、租约、叫醒
这一轮的人、别名）。唯一不写库的是 `unknown_peer`：任何人都能往网桥上发连接，按来源
地址刷审计等于给数据库一个放大器，所以只记一条限流后的 debug 日志。

横幅永远不含凭据、主机地址、端口；`unknown_peer` 与 `bad_client_key` 干脆不给横幅，
直接断开。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.services.ssh_gateway import audit_queue, authz, events, registry, row_state
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway.constants import is_valid_alias
from nekro_agent.services.ssh_gateway.dbslot import db_slot
from nekro_agent.services.ssh_gateway.destination import check_user_destination
from nekro_agent.services.ssh_gateway.keys import public_key_line
from nekro_agent.services.ssh_gateway.lease import running_lease
from nekro_agent.services.ssh_gateway.secrets import decrypt_secret

logger = get_sub_logger("ssh_gateway.authority")

CACHE_TTL_SECONDS = 5.0
AUTH_FAILURE_WINDOW_SECONDS = 300.0
AUTH_FAILURE_LIMIT = 10
AUTH_LOCK_SECONDS = 600.0
LOCKED_AUDIT_INTERVAL_SECONDS = 60.0

# 单调时钟；测试注入它来验证爆破窗口与缓存过期，不必真等
_clock: Callable[[], float] = time.monotonic

# 认证类失败的时间戳（每工作区一个队列）与锁到期时刻
_auth_failures: Dict[int, Deque[float]] = defaultdict(deque)
_locked_until: Dict[int, float] = {}
_locked_audit_at: Dict[int, float] = {}
_last_unknown_peer_log_at = 0.0
_UNKNOWN_PEER_LOG_INTERVAL_SECONDS = 60.0

# (credential_id, lease_id, 名单指纹) -> (写入时刻, 结果)
_recheck_cache: Dict[Tuple[int, str, Tuple[Any, ...]], Tuple[float, Optional[str]]] = {}
_cache_generation = 0
# 复核出错的日志限流：数据库不可用时每 5 秒每条连接都会走到这里
RECHECK_ERROR_LOG_INTERVAL_SECONDS = 60.0
_recheck_error_logged_at = float("-inf")


@dataclass
class TargetSpec:
    """一次放行后，E2 登录目标需要的全部信息

    三个凭据字段刻意 `repr=False`：`Decision` 一旦被日志或调试器展开，不该把明文带
    出来。
    """

    credential_id: int
    workspace_id: int
    alias: str
    owner_kind: str
    host: str
    connect_address: str
    port: int
    username: str
    password: str = field(repr=False)
    private_key: str = field(repr=False)
    passphrase: str = field(repr=False)
    host_keys: List[str]
    # 授权那一刻凭据与目标的摘要（`row_state.credential_digest`）；上游池据此判断登录期间
    # 行有没有被改过。聊天登记的一次性校验没有行，为空
    row_digest: str = ""


@dataclass
class Decision:
    """一次连接请求的结论

    Attributes:
        ok: 是否放行
        reason: 拒绝理由码（`constants` 里的取值）；放行为空
        banner: 给 CC 看的中文说明；`unknown_peer` / `bad_client_key` 为空
        workspace_id: 认出来的工作区；来源都认不出时为空
        lease_id / task_ref / triggered_by: 授权依据，审计与排查用
        target: 放行时的登录目标；拒绝时为空
    """

    ok: bool
    reason: str = ""
    banner: str = ""
    workspace_id: Optional[int] = None
    lease_id: str = ""
    task_ref: str = ""
    triggered_by: Tuple[str, ...] = ()
    target: Optional[TargetSpec] = None


_BANNERS = {
    C.REASON_GATEWAY_OFF: "网关没开",
    C.REASON_ISOLATION_OFF: "这个工作区的网络隔离没生效，不提供服务器连接",
    C.REASON_IMAGE_TOO_OLD: "CC 沙盒镜像太旧，请平台方升级",
    C.REASON_UNKNOWN_ALIAS: "没有这个别名；请用 `ssh <别名>`，可用别名见 `CLAUDE.md`",
    C.REASON_NO_LEASE: "当前任务没有使用服务器的授权（只有凭据主人本人叫醒的那一轮委托的任务才能用）",
    C.REASON_NOT_COVERED: "本任务的授权不含这台服务器",
    C.REASON_OWNER_CHANGED: "授权依据变了（名单、白名单、实例状态），本任务不能再用",
    C.REASON_WORKSPACE_NOT_PLATFORM: "这个工作区不再归平台方，平台方的服务器已挂起",
    C.REASON_ROW_INACTIVE: "这条凭据停用了，请平台方或凭据主人处理",
    C.REASON_UNDECRYPTABLE: "这条凭据解不开，请平台方或凭据主人处理",
    C.REASON_TARGET_BLOCKED: "这台服务器解析到了不允许的地址",
    C.REASON_HOST_KEY_MISMATCH: "服务器的主机密钥和登记的不一致，已拒绝，请核对",
    C.REASON_UPSTREAM_AUTH_FAILED: "用登记的凭据登不上（密码或密钥不对），这条凭据已暂停，请凭据主人更新",
    C.REASON_UPSTREAM_UNREACHABLE: "连不上这台服务器",
    C.REASON_MFA_REQUIRED: "服务器要求二次验证，网关不支持",
    C.REASON_LIMIT_EXCEEDED: "连接或通道数超过上限",
    C.REASON_CHANNEL_UNSUPPORTED: "不支持 agent 转发 / X11 转发（只拒这个请求，不断连接）",
}

# 这两种拒绝不给横幅：直接断开，不给对方任何可读的反馈
_NO_BANNER_REASONS = frozenset({C.REASON_UNKNOWN_PEER, C.REASON_BAD_CLIENT_KEY})


def banner_for(reason: str, *, alias: str = "") -> str:
    """拒绝理由对应的中文横幅

    `alias` 目前不拼进文案：别名来自对端，原样回显没有意义，反而可能把一段任意文本
    送给 CC。参数保留是为了后续按别名给更细的提示时不改签名。
    """
    del alias
    if reason in _NO_BANNER_REASONS:
        return ""
    return _BANNERS.get(reason, "")


def _install_events_subscription() -> None:
    """订阅凭据变更；`events` 被测试清空后，下一次判定会自动重新订阅"""
    if not events.is_subscribed(_on_credential_changed):
        events.on_credential_changed(_on_credential_changed)


def _on_credential_changed(credential_id: int) -> None:
    """凭据被改 / 删 / 启停：它的缓存立刻作废，下一次通道复核重新查库"""
    invalidate(credential_id)


def _now() -> float:
    return _clock()


def _is_locked(workspace_id: int, now: float) -> bool:
    until = _locked_until.get(workspace_id)
    if until is None:
        return False
    if now < until:
        return True
    _locked_until.pop(workspace_id, None)
    return False


def _record_auth_failure(workspace_id: int, now: float) -> None:
    """记一次认证类失败；第 10 次时上锁并记一条 WARNING"""
    failures = _auth_failures[workspace_id]
    while failures and now - failures[0] > AUTH_FAILURE_WINDOW_SECONDS:
        failures.popleft()
    failures.append(now)
    if len(failures) >= AUTH_FAILURE_LIMIT and workspace_id not in _locked_until:
        _locked_until[workspace_id] = now + AUTH_LOCK_SECONDS
        logger.warning(
            f"SSH 网关：工作区 {workspace_id} 在 {int(AUTH_FAILURE_WINDOW_SECONDS)} 秒内累计 "
            f"{len(failures)} 次认证失败，接下来 {int(AUTH_LOCK_SECONDS)} 秒一律拒绝",
        )


def _audit_connect(
    *,
    alias: str,
    allowed: bool,
    reason: str,
    workspace_id: Optional[int],
    row: Optional[DBWsSshCredential] = None,
    lease: Optional[object] = None,
) -> bool:
    """写一行 `connect` 审计；队列满返回 `False`"""
    lease_id = str(getattr(lease, "lease_id", "") or "")
    task_ref = str(getattr(lease, "task_ref", "") or "")
    triggered_by = list(getattr(lease, "triggered_by", ()) or ())
    return audit_queue.enqueue(
        workspace_id=int(workspace_id or 0),
        credential_id=int(row.id) if row is not None else None,
        owner_kind=row.owner_kind if row is not None else "",
        owner_account=row.owner_account if row is not None else "",
        adapter_key=row.adapter_key if row is not None else "",
        instance_key=row.instance_key if row is not None else "",
        event=C.EVENT_CONNECT,
        allowed=bool(allowed),
        reason=str(reason or ""),
        task_ref=task_ref,
        lease_id=lease_id,
        conn_id="",
        triggered_by=triggered_by,
        channel_kind="",
        detail=f"alias={str(alias or '')[:64]}",
        input_head="",
        exit_status=None,
        bytes_up=None,
        bytes_down=None,
        duration_ms=None,
        host_key_fp="",
        actor="gateway",
    )


def _deny(
    reason: str,
    *,
    alias: str,
    workspace_id: Optional[int],
    row: Optional[DBWsSshCredential] = None,
    lease: Optional[object] = None,
) -> Decision:
    """构造拒绝结论并记审计；横幅按理由码取，凭据与地址都不进文案"""
    banner = banner_for(reason, alias=alias)
    _audit_connect(
        alias=alias,
        allowed=False,
        reason=reason,
        workspace_id=workspace_id,
        row=row,
        lease=lease,
    )
    return Decision(
        ok=False,
        reason=reason,
        banner=banner,
        workspace_id=workspace_id,
        lease_id=str(getattr(lease, "lease_id", "") or ""),
        task_ref=str(getattr(lease, "task_ref", "") or ""),
        triggered_by=tuple(getattr(lease, "triggered_by", ()) or ()),
    )


def _limit_decision(workspace_id: Optional[int], *, alias: str) -> Decision:
    """队列满时的拒绝：不再尝试入队（队列已经满了）

    被失败爆破锁住的拒绝会走 :func:`_deny`，仍然留一行审计；只有"队列本身满了"
    这一种情况无法审计，因为把行塞进去正是当前做不到的事。
    """
    return Decision(
        ok=False,
        reason=C.REASON_LIMIT_EXCEEDED,
        banner=banner_for(C.REASON_LIMIT_EXCEEDED, alias=alias),
        workspace_id=workspace_id,
    )


def _unknown_peer_log(listen_address: str, peer_address: str, now: float) -> None:
    """来源认不出：不写库，日志也限流，避免任何人刷满审计或日志"""
    global _last_unknown_peer_log_at
    if now - _last_unknown_peer_log_at < _UNKNOWN_PEER_LOG_INTERVAL_SECONDS:
        return
    _last_unknown_peer_log_at = now
    # 只记地址对，不记钥匙与别名：这条路径上它们都不可信
    logger.debug(
        f"SSH 网关拒绝未知来源（不写审计）: listen={listen_address or '-'} peer={peer_address or '-'}",
    )


async def authorize_connection(
    *,
    listen_address: str,
    peer_address: str,
    client_key_line: str,
    alias: str,
) -> Decision:
    """判定一条容器侧连接能不能放行

    Returns:
        `Decision`；放行时 `target` 非空，拒绝时 `reason` / `banner` 按理由码填好。

    失败路径上"判不出来"一律按拒绝处理（例如复核抛异常），没有"出错就放行"的分支。
    """
    _install_events_subscription()
    now = _now()

    # 1. 总开关。关着时连来源都不必认，但仍然留一行审计（工作区未知记 0）
    if not bool(config.SSH_GATEWAY_ENABLED):
        return _deny(C.REASON_GATEWAY_OFF, alias=alias, workspace_id=None)

    # 2. 来源地址 + 本地地址都要对上，才认得出工作区
    peer = registry.resolve_peer(listen_address, peer_address)
    if peer is None:
        _unknown_peer_log(listen_address, peer_address, now)
        return Decision(ok=False, reason=C.REASON_UNKNOWN_PEER, banner="", workspace_id=None)

    workspace_id = int(peer.workspace_id)

    # 失败爆破的锁先看：锁住以后一律 limit_exceeded，不再做任何昂贵判定；
    # 审计每个工作区每分钟只留一行（见模块说明）
    if _is_locked(workspace_id, now):
        last = _locked_audit_at.get(workspace_id)
        if last is None or now - last >= LOCKED_AUDIT_INTERVAL_SECONDS:
            _locked_audit_at[workspace_id] = now
            return _deny(C.REASON_LIMIT_EXCEEDED, alias=alias, workspace_id=workspace_id)
        return _limit_decision(workspace_id, alias=alias)

    # 审计队列满了就不再接新连接：调用方据此拒绝，内存不继续涨
    if audit_queue.is_saturated():
        return _limit_decision(workspace_id, alias=alias)

    # 3. 隔离与镜像：都是内存标记，先判
    if not peer.isolation_ok and not bool(config.SSH_GATEWAY_ALLOW_WITHOUT_ISOLATION):
        return _deny(C.REASON_ISOLATION_OFF, alias=alias, workspace_id=workspace_id)
    if not peer.image_ok:
        return _deny(C.REASON_IMAGE_TOO_OLD, alias=alias, workspace_id=workspace_id)

    # 4. 客户端钥匙：只比较「类型 base64」两段，注释不影响；两段都缺的行按坏钥匙处理，
    #    否则空串对空串会被当成"匹配"
    normalized_client = public_key_line(client_key_line)
    normalized_registered = public_key_line(peer.client_public_key)
    if (
        len(normalized_client.split()) != 2
        or len(normalized_registered.split()) != 2
        or normalized_client != normalized_registered
    ):
        _record_auth_failure(workspace_id, now)
        return _deny(C.REASON_BAD_CLIENT_KEY, alias=alias, workspace_id=workspace_id)

    # 5. 别名行：先看形状（不合规则的不拿去查库），再查库，再谈租约。行不存在时归属快照留空
    if not is_valid_alias(str(alias or "")):
        _record_auth_failure(workspace_id, now)
        return _deny(C.REASON_UNKNOWN_ALIAS, alias=alias, workspace_id=workspace_id)
    async with db_slot():
        row = await DBWsSshCredential.get_or_none(workspace_id=workspace_id, alias=str(alias or ""))
    if row is None:
        _record_auth_failure(workspace_id, now)
        return _deny(C.REASON_UNKNOWN_ALIAS, alias=alias, workspace_id=workspace_id)

    # 6. 这个工作区此刻唯一一份生效中的租约
    lease = running_lease(workspace_id)
    if lease is None:
        return _deny(C.REASON_NO_LEASE, alias=alias, workspace_id=workspace_id, row=row)

    # 7. 复核：租约是否仍覆盖这一行、名单与实例状态是否没变
    async with db_slot():
        try:
            recheck_reason = await authz.recheck(lease, row)
        except Exception as exc:  # noqa: BLE001 - 判不出来一律拒，不猜
            logger.error(f"SSH 网关复核失败，按拒绝处理: {type(exc).__name__}: {exc}")
            recheck_reason = C.REASON_NOT_COVERED
    if recheck_reason:
        return _deny(recheck_reason, alias=alias, workspace_id=workspace_id, row=row, lease=lease)

    # 8. 解密最后做：只有前面全过才把明文带进内存
    password, password_source = decrypt_secret(str(row.password_enc or ""))
    private_key, key_source = decrypt_secret(str(row.private_key_enc or ""))
    passphrase, passphrase_source = decrypt_secret(str(row.passphrase_enc or ""))
    if "undecryptable" in (password_source, key_source, passphrase_source):
        return _deny(C.REASON_UNDECRYPTABLE, alias=alias, workspace_id=workspace_id, row=row, lease=lease)

    # 9. 用户行要按当前解析结果再查一遍，并只连查过的地址；平台方行直连登记的主机
    owner_kind = str(row.owner_kind or "")
    connect_address = str(row.host or "")
    if owner_kind == C.OWNER_USER:
        verdict = await check_user_destination(str(row.host or ""))
        if not verdict.ok or not verdict.addresses:
            return _deny(C.REASON_TARGET_BLOCKED, alias=alias, workspace_id=workspace_id, row=row, lease=lease)
        connect_address = str(verdict.addresses[0])

    # 10. 全过：构造登录目标
    target = TargetSpec(
        credential_id=int(row.id),
        workspace_id=int(row.workspace_id),
        alias=str(row.alias or ""),
        owner_kind=owner_kind,
        host=str(row.host or ""),
        connect_address=connect_address,
        port=int(row.port),
        username=str(row.username or ""),
        password=password,
        private_key=private_key,
        passphrase=passphrase,
        host_keys=[str(item) for item in (row.host_keys or [])],
        row_digest=row_state.credential_digest(row),
    )

    allowed = _audit_connect(
        alias=alias,
        allowed=True,
        reason="",
        workspace_id=workspace_id,
        row=row,
        lease=lease,
    )
    if not allowed:
        # 判定与入队之间队列被填满：按"拒绝新连接"处理，绝不悄悄放过一条没有审计的连接
        return _limit_decision(workspace_id, alias=alias)

    return Decision(
        ok=True,
        reason="",
        banner="",
        workspace_id=workspace_id,
        lease_id=str(lease.lease_id),
        task_ref=str(lease.task_ref),
        triggered_by=tuple(lease.triggered_by),
        target=target,
    )


async def _compute_recheck(workspace_id: int, credential_id: int, lease_id: str) -> Optional[str]:
    """真正查一次复核；只读内存与数据库，不碰上游连接"""
    lease = running_lease(int(workspace_id))
    if lease is None or str(lease.lease_id) != str(lease_id):
        # 租约结束、被顶替或根本不属于这个工作区：一致性对不上，按无授权处理
        return C.REASON_NO_LEASE
    async with db_slot():
        row = await DBWsSshCredential.get_or_none(id=int(credential_id))
        if row is None or int(row.workspace_id) != int(workspace_id):
            return C.REASON_NOT_COVERED
        return await authz.recheck(lease, row)


async def recheck_channel(
    *,
    workspace_id: int,
    credential_id: int,
    lease_id: str,
) -> Optional[str]:
    """每开一个通道前的复核；`None` = 放行，否则是拒绝理由码

    结果缓存 5 秒，键是 `(credential_id, lease_id)`：同一个连接上的多个通道几乎同时
    开，没必要每个都查一遍库。凭据被改动时由变更通知立刻清缓存。
    """
    _install_events_subscription()
    key = (int(credential_id), str(lease_id), _config_fingerprint())
    now = _now()
    cached = _recheck_cache.get(key)
    if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    generation = _cache_generation
    try:
        result = await _compute_recheck(int(workspace_id), int(credential_id), str(lease_id))
    except Exception as exc:  # noqa: BLE001 - 复核出错不是「判定不给」，单独一个理由码，不缓存
        # 不能当成拒绝：周期复核每 5 秒扫一遍所有连接，一次短暂的数据库错误就会把所有租户的
        # 会话全部断掉。怎么处理由调用方定（server._recheck：开着的连接不断，新通道有宽限）
        global _recheck_error_logged_at
        now_error = _now()
        if now_error - _recheck_error_logged_at >= RECHECK_ERROR_LOG_INTERVAL_SECONDS:
            _recheck_error_logged_at = now_error
            logger.error(f"SSH 网关通道复核出错（同类错误 60 秒内只记一条）: {type(exc).__name__}: {exc}")
        return C.REASON_RECHECK_ERROR

    # 复核期间缓存被作废过（凭据变了、租约结束了）：这次的结果可能是作废之前的状态，不写回
    if generation == _cache_generation:
        _drop_expired(now)
        _recheck_cache[key] = (now, result)
    return result


def _config_fingerprint() -> Tuple[Any, ...]:
    """复核依赖的配置项：任何一项变了就是新的缓存键"""
    return (
        bool(config.SSH_GATEWAY_ENABLED),
        tuple(str(item) for item in (config.SSH_GATEWAY_OWNER_ACCOUNTS or [])),
        tuple(str(item) for item in (config.SSH_GATEWAY_USER_ACCOUNTS or [])),
    )


def _drop_expired(now: float) -> None:
    for key in [key for key, (written, _result) in _recheck_cache.items() if now - written >= CACHE_TTL_SECONDS]:
        _recheck_cache.pop(key, None)


def invalidate(credential_id: int) -> None:
    """作废某条凭据的全部复核缓存"""
    global _cache_generation
    _cache_generation += 1
    value = int(credential_id)
    for key in [key for key in _recheck_cache if key[0] == value]:
        _recheck_cache.pop(key, None)


def invalidate_lease(lease_id: str) -> None:
    """作废某份租约的全部复核缓存"""
    global _cache_generation
    _cache_generation += 1
    value = str(lease_id)
    for key in [key for key in _recheck_cache if key[1] == value]:
        _recheck_cache.pop(key, None)


def reset_authority_state() -> None:
    """清空缓存与爆破计数（仅供测试）"""
    _recheck_cache.clear()
    _auth_failures.clear()
    _locked_until.clear()
    _locked_audit_at.clear()
    global _last_unknown_peer_log_at
    _last_unknown_peer_log_at = 0.0


# 模块导入即订阅一次；`_install_events_subscription` 会在测试清空订阅后补回来
_install_events_subscription()


__all__ = [
    "CACHE_TTL_SECONDS",
    "Decision",
    "TargetSpec",
    "authorize_connection",
    "banner_for",
    "invalidate",
    "invalidate_lease",
    "recheck_channel",
    "reset_authority_state",
]
