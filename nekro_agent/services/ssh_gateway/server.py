"""SSH 网关服务端本体

## 这一层负责什么

- 在登记表给出的每个地址上听 SSH（绝不在 `0.0.0.0` / `::` 上听）；
- 容器侧只允许公钥登录，钥匙必须是登记表里为这个工作区登记的那一把；
- 公钥验证通过后，按授权结论登录一次上游，并把连接放进复用池；
- 每个 channel 进来先复核租约、再查通道数上限、最后交给桥接层；
- 租约结束 / 凭据变更 / 监督任务要求关停时，断开受影响的容器连接与上游。

## 为什么 key 验证里就登录上游

容器里的 SSH 客户端通常只把公钥验证当作"服务器认我"的一步，真正开通道是紧接着的
事。如果在开通道时才登录上游，CC 会先看到一个成功的连接，随后每个命令都失败；而
且上游认证失败时已经没有地方再发认证横幅。这里在 `validate_public_key` 里完成授权
与上游登录，失败就用横幅告诉 CC 原因，成功才返回 `True`。asyncssh 对同一把钥匙可能
问两次（先问能不能用、再验签），所以按连接缓存结果，不重复授权、不重复登录。

## 错误隔离

每个回调/后台任务的最外层都兜住异常：记 ERROR（带 conn_id，不带凭据）、
`supervisor.report_unexpected(exc)`、只关掉这一条连接。一个通道或一条上游的意外
不能让整个网关停摆。

## 通道名额从请求那一刻就占上

会话通道在客户端发 `exec` / `shell` 之前还没有桥，但对端已经可以往它的窗口里塞数据
（asyncssh 暂停读时把数据攒在通道里）。所以名额（每连接、全局）在 `session_requested`
就占上，直到通道关闭才还；否则一条连接先开几千个空会话就能绕过上限把内存吃光。
每个通道还要占所在上游连接的一个名额（`MAX_CHANNELS_PER_UPSTREAM`），满了就另登一条。

## 授权依据变了就断整条连接

开通道时复核不过，或者监督任务每一拍的复核不过（名单、白名单、实例状态改了，租约
过期），不只拒这一个通道，而是把整条容器连接断开：已经开着的 shell 与转发不能
在授权撤销以后继续用到任务结束。

## 异步的开通道回调用异常拒绝

asyncssh 对异步回调是「先回确认、再拿结果」：返回 `None` 会让它在 `None.connection_made`
上出错并断掉整条连接（反向转发时断的还是共用的上游）。这些回调一律抛
`ChannelOpenError` 拒绝。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import deque
from typing import Any, Dict, Optional, Set, Tuple

import asyncssh

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.services.ssh_gateway import (
    audit_queue,
    authority,
    duty_report,
    events,
    keys,
    lease,
    registry,
    supervisor,
)
from nekro_agent.services.ssh_gateway import bridge as bridge_module
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway.authority import Decision
from nekro_agent.services.ssh_gateway.bridge import BridgeContext, ChannelBridge, ClientSide, ServerSide
from nekro_agent.services.ssh_gateway.dbslot import db_slot
from nekro_agent.services.ssh_gateway.upstream import PooledUpstream, UpstreamError, pool

logger = get_sub_logger("ssh_gateway.server")

BIND_WARNING_INTERVAL_SECONDS = 300.0
WILDCARD_ADDRESSES = {"", "0.0.0.0", "::", "*"}
# 一条连接上最多验几次公钥（不同的钥匙才算一次）；正常客户端只送一把
MAX_AUTH_ATTEMPTS = 6
# 上游建会话最多等这么久；目标不回 CHANNEL_OPEN 时不能一直挂着
SESSION_OPEN_TIMEOUT_SECONDS = 30.0

# 容器会话的 `env` 请求：只收语言 / 时区一类（与 OpenSSH 默认 `SendEnv LANG LC_*` 同一类），
# 条数与长度都有上限，转发给上游（目标服务器自己按 `AcceptEnv` 再筛一次）
_ENV_NAME_ALLOWED = re.compile(r"LANG|LANGUAGE|LC_[A-Z_]{1,32}|TZ")
# 复核出错时，上一次复核通过之后多久内仍放行新通道（已开着的连接无论多久都不因出错断开）
RECHECK_ERROR_GRACE_SECONDS = 60.0
MAX_ENV_VARS = 16
MAX_ENV_NAME_BYTES = 64
MAX_ENV_VALUE_BYTES = 256
# `nk-runbook-append` 从 stdin 最多收这么多字节（经验本身截到 800 字，UTF-8 下够用），多的丢掉
RUNBOOK_APPEND_MAX_BYTES = 8192


def _as_limit(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _refuse(message: str = "通道被拒绝") -> asyncssh.ChannelOpenError:
    """异步开通道回调里的拒绝：抛它而不是返回 None（见模块说明）"""
    return asyncssh.ChannelOpenError(asyncssh.OPEN_CONNECT_FAILED, message)


class _ChannelSlot:
    """一个通道占的名额：全局计数、所属连接的计数、所在上游连接的计数

    `release` 幂等；上游名额在拿到上游之后才 `attach`，拿到之前通道就关了的话，
    拿到的那一刻立刻还回去。
    """

    def __init__(self, server: "GatewayServer") -> None:
        self._server = server
        self.pooled: Optional[PooledUpstream] = None
        self.released = False
        server._slots.add(self)
        server.backend._channel_opened(server.workspace_id)

    def attach(self, pooled: PooledUpstream) -> None:
        if self.released:
            pooled.channel_closed()
            return
        self.pooled = pooled

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self._server._slots.discard(self)
        self._server.backend._channel_closed(self._server.workspace_id)
        pooled = self.pooled
        self.pooled = None
        if pooled is not None:
            pooled.channel_closed()


class GatewayBackendImpl:
    """监督任务眼里的后端：监听集合、连接计数、启停"""

    def __init__(self) -> None:
        self._host_key: Optional[asyncssh.SSHKeyPair] = None
        self._listeners: Dict[str, asyncssh.SSHAcceptor] = {}
        self._port = 0
        self._bound_port = 0
        self._connections: Dict[str, "GatewayServer"] = {}
        self._workspace_connections: Dict[int, int] = {}
        self._channels_total = 0
        # 按工作区计的通道数：全局 64 个名额，默认每工作区 8 条连接 × 每条 10 个通道就能
        # 一个容器全占掉，所以另设每工作区上限
        self._workspace_channels: Dict[int, int] = {}
        self._subscribed = False
        self._credential_callback = self._on_credential_changed
        self._started = False
        self._bind_warned_at: Dict[str, float] = {}
        self._closed_at = 0.0

    # ---- 生命周期 ----

    async def start(self) -> None:
        if self._started:
            return
        path = keys.load_or_create_host_key()
        # asyncssh 2.24 的 read_private_key 是同步函数（只是读文件并解析）
        self._host_key = asyncssh.read_private_key(str(path))
        if not self._subscribed:
            lease.on_lease_end(self._on_lease_end)
            # 排在断连接之后：摘要等这一批通道的关闭审计入队后再读
            lease.on_lease_end(duty_report.on_lease_end)
            events.on_credential_changed(self._credential_callback)
            supervisor.supervisor.on_yield_change(self._on_yield_change)
            self._subscribed = True
        self._started = True

    async def sync_listeners(self, addresses: frozenset[str], port: int) -> None:
        """只保留给定地址上的监听；换端口时全部重建

        空地址、通配地址一律跳过：网关只在登记表明确给出的地址上听，避免把
        容器侧 SSH 暴露到宿主机所有网卡。
        """
        if not self._started:
            return
        requested = {str(item).strip() for item in addresses if str(item).strip()}
        wanted = {item for item in requested if item not in WILDCARD_ADDRESSES}
        for item in sorted(requested - wanted):
            self._warn_bind(item, "拒绝在通配地址上监听")
        try:
            new_port = int(port)
        except (TypeError, ValueError):
            new_port = 0
        if new_port != self._port:
            self._close_all_listeners()
        self._port = new_port
        for address in list(self._listeners):
            if address not in wanted:
                self._close_listener(address)
        for address in sorted(wanted):
            if address in self._listeners:
                continue
            await self._open_listener(address, new_port)

    async def _open_listener(self, address: str, port: int) -> None:
        if self._host_key is None:
            raise RuntimeError("SSH 网关主机钥匙未加载")
        factory = lambda: GatewayServer(self)  # noqa: E731 - asyncssh 只接受无参工厂
        try:
            listener = await asyncssh.listen(
                host=address,
                port=port,
                server_factory=factory,
                server_host_keys=[self._host_key],
                config=None,
                encoding=None,
                window=bridge_module.WINDOW_SIZE,
                line_editor=False,
                line_echo=False,
                agent_forwarding=False,
                x11_forwarding=False,
                login_timeout=30,
                keepalive_interval=30,
                keepalive_count_max=3,
                reuse_address=True,
            )
        except OSError as exc:
            self._warn_bind(address, f"监听失败，跳过: {type(exc).__name__}: {exc}")
            return
        self._listeners[address] = listener
        bound = 0
        try:
            bound = int(listener.get_port())
        except Exception:  # noqa: BLE001 - 多地址时 get_port 可能返回 0
            bound = 0
        if bound:
            self._bound_port = bound

    def _warn_bind(self, address: str, message: str) -> None:
        now = time.monotonic()
        last = self._bind_warned_at.get(address, 0.0)
        if now - last < BIND_WARNING_INTERVAL_SECONDS:
            return
        self._bind_warned_at[address] = now
        logger.warning(f"SSH 网关在 {address or '-'} 上的监听未建立：{message}")

    async def stop(self) -> None:
        self._started = False
        self._close_all_listeners()
        for server in list(self._connections.values()):
            server.disconnect()
        self._connections.clear()
        self._workspace_connections.clear()
        self._channels_total = 0
        self._workspace_channels.clear()
        pool.close_all()
        bridge_module.reset_bridges()
        self._closed_at = time.monotonic()

    def _close_listener(self, address: str) -> None:
        listener = self._listeners.pop(address, None)
        if listener is None:
            return
        try:
            listener.close()
        except Exception as exc:  # noqa: BLE001 - 关监听失败只影响这一条
            logger.warning(f"SSH 网关关闭监听 {address} 失败: {type(exc).__name__}: {exc}")

    def _close_all_listeners(self) -> None:
        for address in list(self._listeners):
            self._close_listener(address)
        self._bound_port = 0

    # ---- 状态 ----

    def counters(self) -> dict:
        return {
            "connections": len(self._connections),
            "channels": int(self._channels_total),
            "workspaces": dict(self._workspace_connections),
            "upstreams": pool.count(),
        }

    @property
    def bound_port(self) -> int:
        """实际绑定的端口（测试与状态接口用；配置端口为 0 时即由内核分配）"""
        if self._bound_port:
            return self._bound_port
        for listener in self._listeners.values():
            try:
                port = int(listener.get_port())
            except Exception:  # noqa: BLE001
                continue
            if port:
                return port
        return 0

    # ---- 连接登记 ----

    def _can_accept(self, workspace_id: int) -> bool:
        if audit_queue.is_saturated():
            return False
        max_all = _as_limit(config.SSH_GATEWAY_MAX_CONNECTIONS)
        if max_all and len(self._connections) >= max_all:
            return False
        per_ws = _as_limit(config.SSH_GATEWAY_MAX_CONNECTIONS_PER_WORKSPACE)
        if per_ws and int(self._workspace_connections.get(int(workspace_id), 0)) >= per_ws:
            return False
        return True

    def _register(self, server: "GatewayServer") -> None:
        self._connections[server.conn_id] = server
        self._workspace_connections[server.workspace_id] = (
            self._workspace_connections.get(server.workspace_id, 0) + 1
        )

    def _unregister(self, server: "GatewayServer") -> None:
        if self._connections.pop(server.conn_id, None) is None:
            return
        current = self._workspace_connections.get(server.workspace_id, 0)
        if current <= 1:
            self._workspace_connections.pop(server.workspace_id, None)
        else:
            self._workspace_connections[server.workspace_id] = current - 1

    def _channel_opened(self, workspace_id: int) -> None:
        self._channels_total += 1
        key = int(workspace_id)
        self._workspace_channels[key] = self._workspace_channels.get(key, 0) + 1

    def _channel_closed(self, workspace_id: int) -> None:
        if self._channels_total > 0:
            self._channels_total -= 1
        key = int(workspace_id)
        current = self._workspace_channels.get(key, 0)
        if current <= 1:
            self._workspace_channels.pop(key, None)
        else:
            self._workspace_channels[key] = current - 1

    def workspace_channels(self, workspace_id: int) -> int:
        return int(self._workspace_channels.get(int(workspace_id), 0))

    async def sweep(self) -> None:
        """监督任务每一拍调一次：收掉过期租约，复核在跑的连接，不过的整条断开"""
        lease.sweep_leases()
        for server in list(self._connections.values()):
            try:
                await server.recheck_live()
            except Exception as exc:  # noqa: BLE001 - 一条连接复核出错只断它自己
                logger.warning(f"SSH 网关连接 {server.conn_id} 周期复核出错，断开: {type(exc).__name__}")
                server.disconnect()

    # ---- 事件 ----

    def _on_yield_change(self, value: bool) -> None:
        bridge_module.set_yield_paused(bool(value))

    def _on_lease_end(self, ended: lease.SshLease) -> None:
        try:
            lease_id = str(getattr(ended, "lease_id", "") or "")
            for server in list(self._connections.values()):
                if server.lease_id == lease_id:
                    server.disconnect()
            pool.close_for_lease(lease_id)
            authority.invalidate_lease(lease_id)
        except Exception as exc:  # noqa: BLE001 - 回调出错只记，不影响其他订阅者
            logger.error(f"SSH 网关处理租约结束失败: {type(exc).__name__}: {exc}")
            supervisor.supervisor.report_unexpected(exc)

    def _on_credential_changed(self, credential_id: int) -> None:
        try:
            for server in list(self._connections.values()):
                if server.credential_id == int(credential_id):
                    server.disconnect()
            pool.close_for_credential(int(credential_id))
            authority.invalidate(int(credential_id))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"SSH 网关处理凭据变更失败: {type(exc).__name__}: {exc}")
            supervisor.supervisor.report_unexpected(exc)


class GatewayServer(asyncssh.SSHServer):
    """一条容器侧 SSH 连接"""

    def __init__(self, backend: GatewayBackendImpl) -> None:
        self.backend = backend
        self.conn_id = uuid.uuid4().hex[:16]
        self.conn: Optional[asyncssh.SSHServerConnection] = None
        self.listen_address = ""
        self.peer_address = ""
        self.alias = ""
        self.workspace_id = 0
        self.decision: Optional[Decision] = None
        self.pooled: Optional[PooledUpstream] = None
        self.owner_kind = ""
        self.owner_account = ""
        self.adapter_key = ""
        self.instance_key = ""
        self._channels: Set[ChannelBridge] = set()
        self._slots: Set[_ChannelSlot] = set()
        self._auth_cache: Dict[str, bool] = {}
        self._auth_lock = asyncio.Lock()
        self._auth_attempts = 0
        self._registered = False
        self._authenticated = False
        self._connected_at = time.monotonic()
        # 上一次复核通过的时刻；认证本身就是一次完整判定，所以从连上那一刻算起
        self._last_recheck_ok_at = time.monotonic()

    # ---- 连接 ----

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self.conn = conn
        try:
            sock = conn.get_extra_info("sockname")
            peer = conn.get_extra_info("peername")
            self.listen_address = str(sock[0]) if sock else ""
            self.peer_address = str(peer[0]) if peer else ""
        except Exception:  # noqa: BLE001 - 取不到地址按未知来源处理
            self.listen_address = ""
            self.peer_address = ""
        peer_info = registry.resolve_peer(self.listen_address, self.peer_address)
        if peer_info is None:
            # 不是登记表里任何工作区的来源：不写审计（地址与钥匙都不可信），直接断
            conn.close()
            return
        self.workspace_id = int(peer_info.workspace_id)
        if not self.backend._can_accept(self.workspace_id):
            conn.close()
            return
        self.backend._register(self)
        self._registered = True

    def connection_lost(self, _exc: Optional[Exception]) -> None:
        if self._registered:
            self.backend._unregister(self)
            self._registered = False
        total_up = sum(item.bytes_up for item in list(self._channels))
        total_down = sum(item.bytes_down for item in list(self._channels))
        for item in list(self._channels):
            item.close()
        self._channels.clear()
        for slot in list(self._slots):
            slot.release()
        if self.decision is not None:
            self._audit_connection_close(total_up, total_down)
        # 登录时拿到的那条上游只是"认证通过"的凭证；没有通道在用时由它自己的空闲计时收掉
        self.pooled = None

    def disconnect(self) -> None:
        if self.conn is not None:
            try:
                self.conn.abort()
            except Exception:  # noqa: BLE001 - 已经断开的连接再断一次不该抛
                pass

    def close_gracefully(self) -> None:
        """授权依据变了：关整条连接，但让已经写出去的错误说明先发完"""
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                self.disconnect()

    async def recheck_live(self) -> None:
        """监督任务的周期复核：明确不给就断整条连接；复核出错时沿用上一次的结论，不断"""
        if self.decision is None or not self._authenticated:
            return
        reason = await self._recheck_reason()
        if reason is None or reason == C.REASON_RECHECK_ERROR:
            return
        logger.info(f"SSH 网关连接 {self.conn_id} 周期复核没有通过（{reason}），断开")
        self.close_gracefully()

    def _banner(self, text: str) -> None:
        if not text or self.conn is None:
            return
        try:
            self.conn.send_auth_banner(text)
        except OSError:
            # 认证已经结束（或连接已关）时不能再发横幅
            pass

    # ---- 认证 ----

    def begin_auth(self, username: str) -> bool:
        self.alias = str(username or "")
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return False

    def kbdint_auth_supported(self) -> bool:
        return False

    def host_based_auth_supported(self) -> bool:
        return False

    async def validate_public_key(self, _username: str, key: asyncssh.SSHKey) -> bool:
        try:
            line = keys.public_key_line(key.export_public_key().decode("ascii", "replace"))
        except Exception as exc:  # noqa: BLE001 - 连钥匙都导不出，按坏钥匙处理
            logger.warning(f"SSH 网关连接 {self.conn_id} 导不出客户端公钥: {type(exc).__name__}")
            return False
        cached = self._auth_cache.get(line)
        if cached is not None:
            return cached
        async with self._auth_lock:
            cached = self._auth_cache.get(line)
            if cached is not None:
                return cached
            # 每把新钥匙都要走一遍授权与审计；asyncssh 自己不限次数，不封顶的话一条连接
            # 不停送随机公钥就能把审计队列一直塞满
            self._auth_attempts += 1
            if self._auth_attempts > MAX_AUTH_ATTEMPTS:
                self.disconnect()
                return False
            allowed = await self._authorize_and_login(line)
            self._auth_cache[line] = allowed
            return allowed

    async def _authorize_and_login(self, line: str) -> bool:
        try:
            decision = await authority.authorize_connection(
                listen_address=self.listen_address,
                peer_address=self.peer_address,
                client_key_line=line,
                alias=self.alias,
            )
            if not decision.ok:
                self._banner(decision.banner)
                return False
            target = decision.target
            if target is None:
                self._banner(authority.banner_for(C.REASON_UPSTREAM_UNREACHABLE, alias=self.alias))
                return False
            try:
                self.pooled = await pool.acquire(decision)
            except UpstreamError as exc:
                self._audit_upstream_denied(decision, exc.reason)
                self._banner(authority.banner_for(exc.reason, alias=self.alias))
                return False
            self.decision = decision
            self.owner_kind = str(target.owner_kind or "")
            await self._load_owner_snapshot(int(target.credential_id))
            return True
        except Exception as exc:  # noqa: BLE001 - 授权/登录里的意外只关这条连接
            logger.error(f"SSH 网关连接 {self.conn_id} 授权或上游登录失败: {type(exc).__name__}: {exc}")
            supervisor.supervisor.report_unexpected(exc)
            self.disconnect()
            return False

    async def _load_owner_snapshot(self, credential_id: int) -> None:
        """审计里的归属快照：授权结论只带 owner_kind，其余从行上补一次"""
        try:
            async with db_slot():
                row = await DBWsSshCredential.get_or_none(id=int(credential_id))
        except Exception as exc:  # noqa: BLE001 - 补快照失败不影响连接
            logger.warning(f"SSH 网关连接 {self.conn_id} 读归属快照失败: {type(exc).__name__}")
            return
        if row is None:
            return
        self.owner_kind = str(row.owner_kind or self.owner_kind)
        self.owner_account = str(row.owner_account or "")
        self.adapter_key = str(row.adapter_key or "")
        self.instance_key = str(row.instance_key or "")

    def auth_completed(self) -> None:
        self._authenticated = True
        if self.pooled is None:
            self.disconnect()

    @property
    def credential_id(self) -> int:
        if self.decision is not None and self.decision.target is not None:
            return int(self.decision.target.credential_id)
        return 0

    @property
    def lease_id(self) -> str:
        return str(self.decision.lease_id) if self.decision is not None else ""

    # ---- 通道 ----

    def _context(self) -> BridgeContext:
        decision = self.decision
        assert decision is not None and decision.target is not None
        return BridgeContext(
            conn_id=self.conn_id,
            workspace_id=int(decision.workspace_id or self.workspace_id),
            credential_id=int(decision.target.credential_id),
            owner_kind=self.owner_kind or str(decision.target.owner_kind or ""),
            owner_account=self.owner_account,
            adapter_key=self.adapter_key,
            instance_key=self.instance_key,
            lease_id=str(decision.lease_id),
            task_ref=str(decision.task_ref),
            triggered_by=tuple(decision.triggered_by),
        )

    def _new_bridge(
        self,
        slot: _ChannelSlot,
        *,
        kind: str,
        detail: str = "",
        command: str = "",
        record_input: bool = False,
    ) -> ChannelBridge:
        """在已经占好的名额上建一座桥；桥关闭时连同名额一起还"""
        bridge = ChannelBridge(
            self._context(),
            kind=kind,
            detail=detail,
            command=command,
            record_input=record_input,
        )

        def _closed() -> None:
            self._channels.discard(bridge)
            slot.release()

        bridge.on_close = _closed
        self._channels.add(bridge)
        return bridge

    def _channel_allowed(self) -> bool:
        if not self._authenticated or self.decision is None:
            return False
        if audit_queue.is_saturated():
            return False
        per_conn = _as_limit(config.SSH_GATEWAY_MAX_CHANNELS)
        if per_conn and len(self._slots) >= per_conn:
            return False
        total = _as_limit(config.SSH_GATEWAY_MAX_CHANNELS_TOTAL)
        if total and self.backend._channels_total >= total:
            return False
        per_ws = _as_limit(config.SSH_GATEWAY_MAX_CHANNELS_PER_WORKSPACE)
        if per_ws and self.backend.workspace_channels(self.workspace_id) >= per_ws:
            return False
        return True

    def _reserve_slot(self) -> Optional[_ChannelSlot]:
        """同步判上限并占名额；不够返回 None"""
        if not self._channel_allowed():
            return None
        return _ChannelSlot(self)

    async def _recheck_reason(self) -> Optional[str]:
        decision = self.decision
        if decision is None or decision.target is None:
            return C.REASON_NOT_COVERED
        reason = await authority.recheck_channel(
            workspace_id=int(decision.workspace_id or self.workspace_id),
            credential_id=int(decision.target.credential_id),
            lease_id=str(decision.lease_id),
        )
        if reason is None:
            self._last_recheck_ok_at = time.monotonic()
        return reason

    async def _recheck(self) -> bool:
        """开通道前复核一次；明确不给时把整条连接断开（授权依据变了，已开的通道也不能再用）

        复核**出错**（`recheck_error`，数据库一时不可用等）不是「不给」：不断连接；上一次复核
        通过之后的宽限期内照常放行这个通道，过了宽限期只拒这一个通道。
        """
        reason = await self._recheck_reason()
        if reason == C.REASON_RECHECK_ERROR:
            return time.monotonic() - self._last_recheck_ok_at <= RECHECK_ERROR_GRACE_SECONDS
        if reason:
            logger.info(f"SSH 网关连接 {self.conn_id} 复核没有通过（{reason}），断开整条连接")
            # 放到下一拍：调用方还要先把这个通道的错误说明写出去
            try:
                asyncio.get_running_loop().call_soon(self.close_gracefully)
            except RuntimeError:
                self.close_gracefully()
            return False
        return True

    async def _acquire_upstream(self, slot: _ChannelSlot) -> PooledUpstream:
        """给这个通道拿一条上游（满了另登一条），并把上游名额记在 slot 上"""
        decision = self.decision
        if decision is None:
            raise _refuse("连接没有授权")
        pooled = await pool.acquire(decision, reserve=True)
        slot.attach(pooled)
        return pooled

    # asyncssh 2.24 的 `_process_session_open()` 不 await `session_requested()` 的返回值，
    # 所以这里必须是同步函数：只做同步的通道计数判断并占名额，异步复核放到会话真正开始时。
    def session_requested(self) -> Optional[asyncssh.SSHServerSession]:
        try:
            slot = self._reserve_slot()
            if slot is None:
                return None
            return GatewaySession(self, slot)
        except Exception as exc:  # noqa: BLE001 - 单个通道出错只关这条连接
            logger.error(f"SSH 网关连接 {self.conn_id} 打开会话失败: {type(exc).__name__}: {exc}")
            supervisor.supervisor.report_unexpected(exc)
            self.disconnect()
            return None

    async def connection_requested(
        self,
        dest_host: str,
        dest_port: int,
        orig_host: str,
        orig_port: int,
    ) -> asyncssh.SSHTCPSession:
        slot = self._reserve_slot()
        if slot is None:
            raise _refuse("通道数超过上限")
        bridge: Optional[ChannelBridge] = None
        try:
            if not await self._recheck():
                raise _refuse("授权复核没有通过")
            bridge = self._new_bridge(slot, kind="direct-tcpip", detail=f"{dest_host}:{dest_port}")
            if not bridge.audit_open():
                raise _refuse("审计繁忙")
            pooled = await self._acquire_upstream(slot)
            target_session = ClientSide(bridge)
            await pooled.conn.create_connection(
                lambda: target_session,
                dest_host,
                int(dest_port),
                orig_host,
                int(orig_port),
                encoding=None,
                window=bridge_module.WINDOW_SIZE,
            )
            bridge.attach_target(target_session)
            bridge.start()
            return _ForwardServerSide(bridge)
        except asyncssh.ChannelOpenError:
            self._abandon(slot, bridge)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SSH 网关连接 {self.conn_id} 直连转发失败: {type(exc).__name__}: {exc}")
            self._abandon(slot, bridge)
            raise _refuse("目标拒绝了这个转发") from None

    def _abandon(self, slot: _ChannelSlot, bridge: Optional[ChannelBridge]) -> None:
        if bridge is not None:
            bridge.close()
        slot.release()

    async def server_requested(self, listen_host: str, listen_port: int) -> Optional[object]:
        slot = self._reserve_slot()
        if slot is None:
            return None
        bridge: Optional[ChannelBridge] = None
        try:
            if not await self._recheck():
                slot.release()
                return None
            bridge = self._new_bridge(slot, kind="tcpip-forward", detail=f"{listen_host}:{listen_port}")
            if not bridge.audit_open():
                self._abandon(slot, bridge)
                return None
            # 监听挂在哪条上游上，回来的连接就从哪条上游进来：这条上游要一直活到监听关掉，
            # 所以监听本身占一个上游名额
            pooled = await self._acquire_upstream(slot)
            # 客户端请求的端口可能是 0（让服务器挑）。容器端注册的监听键是最终分配到的
            # 端口，所以 forwarded-tcpip 必须用它，而不是原始的 0。
            actual = {"host": listen_host, "port": int(listen_port)}

            def _on_connection(orig_host: str, orig_port: int) -> object:
                return self._forwarded_tcp_session(
                    pooled,
                    actual["host"],
                    actual["port"],
                    orig_host,
                    orig_port,
                )

            listener = await pooled.conn.create_server(
                _on_connection,
                listen_host,
                int(listen_port),
                encoding=None,
                window=bridge_module.WINDOW_SIZE,
            )
            actual["port"] = int(listener.get_port())
            bridge.detail = f"{actual['host']}:{actual['port']}"
            return _TrackedListener(listener, bridge)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SSH 网关连接 {self.conn_id} 建立反向 TCP 监听失败: {type(exc).__name__}: {exc}")
            self._abandon(slot, bridge)
            return None

    async def _forwarded_tcp_session(
        self,
        pooled: PooledUpstream,
        listen_host: str,
        listen_port: int,
        orig_host: str,
        orig_port: int,
    ) -> asyncssh.SSHTCPSession:
        slot = self._reserve_slot()
        if slot is None:
            raise _refuse("通道数超过上限")
        bridge: Optional[ChannelBridge] = None
        try:
            if not await self._recheck() or self.conn is None:
                raise _refuse("授权复核没有通过")
            pooled.channel_opened()
            slot.attach(pooled)
            bridge = self._new_bridge(slot, kind="tcpip-forward", detail=f"{listen_host}:{listen_port}")
            if not bridge.audit_open():
                raise _refuse("审计繁忙")
            container_session = ClientSide(bridge, is_container=True)
            await self.conn.create_connection(
                lambda: container_session,
                listen_host,
                int(listen_port),
                orig_host,
                int(orig_port),
                encoding=None,
                window=bridge_module.WINDOW_SIZE,
            )
            target_session = ClientSide(bridge)
            bridge.attach_container(container_session)
            bridge.attach_target(target_session)
            bridge.start()
            return target_session
        except asyncssh.ChannelOpenError:
            self._abandon(slot, bridge)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SSH 网关连接 {self.conn_id} 反向转发连接失败: {type(exc).__name__}: {exc}")
            self._abandon(slot, bridge)
            raise _refuse("容器一侧没有接受这个连接") from None

    async def unix_connection_requested(self, dest_path: str) -> asyncssh.SSHUNIXSession:
        slot = self._reserve_slot()
        if slot is None:
            raise _refuse("通道数超过上限")
        bridge: Optional[ChannelBridge] = None
        try:
            if not await self._recheck():
                raise _refuse("授权复核没有通过")
            bridge = self._new_bridge(slot, kind="streamlocal", detail=dest_path)
            if not bridge.audit_open():
                raise _refuse("审计繁忙")
            pooled = await self._acquire_upstream(slot)
            target_session = ClientSide(bridge)
            await pooled.conn.create_unix_connection(
                lambda: target_session,
                dest_path,
                encoding=None,
                window=bridge_module.WINDOW_SIZE,
            )
            bridge.attach_target(target_session)
            bridge.start()
            return _ForwardServerSide(bridge)
        except asyncssh.ChannelOpenError:
            self._abandon(slot, bridge)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SSH 网关连接 {self.conn_id} UNIX 直连失败: {type(exc).__name__}: {exc}")
            self._abandon(slot, bridge)
            raise _refuse("目标拒绝了这个转发") from None

    async def unix_server_requested(self, listen_path: str) -> Optional[object]:
        slot = self._reserve_slot()
        if slot is None:
            return None
        bridge: Optional[ChannelBridge] = None
        try:
            if not await self._recheck():
                slot.release()
                return None
            bridge = self._new_bridge(slot, kind="streamlocal", detail=listen_path)
            if not bridge.audit_open():
                self._abandon(slot, bridge)
                return None
            pooled = await self._acquire_upstream(slot)
            listener = await pooled.conn.create_unix_server(
                lambda: self._forwarded_unix_session(pooled, listen_path),
                listen_path,
                encoding=None,
                window=bridge_module.WINDOW_SIZE,
            )
            return _TrackedListener(listener, bridge)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SSH 网关连接 {self.conn_id} 建立 UNIX 反向监听失败: {type(exc).__name__}: {exc}")
            self._abandon(slot, bridge)
            return None

    async def _forwarded_unix_session(self, pooled: PooledUpstream, listen_path: str) -> asyncssh.SSHUNIXSession:
        slot = self._reserve_slot()
        if slot is None:
            raise _refuse("通道数超过上限")
        bridge: Optional[ChannelBridge] = None
        try:
            if not await self._recheck() or self.conn is None:
                raise _refuse("授权复核没有通过")
            pooled.channel_opened()
            slot.attach(pooled)
            bridge = self._new_bridge(slot, kind="streamlocal", detail=listen_path)
            if not bridge.audit_open():
                raise _refuse("审计繁忙")
            container_session = ClientSide(bridge, is_container=True)
            await self.conn.create_unix_connection(
                lambda: container_session,
                listen_path,
                encoding=None,
                window=bridge_module.WINDOW_SIZE,
            )
            target_session = ClientSide(bridge)
            bridge.attach_container(container_session)
            bridge.attach_target(target_session)
            bridge.start()
            return target_session
        except asyncssh.ChannelOpenError:
            self._abandon(slot, bridge)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SSH 网关连接 {self.conn_id} UNIX 反向转发失败: {type(exc).__name__}: {exc}")
            self._abandon(slot, bridge)
            raise _refuse("容器一侧没有接受这个连接") from None

    # ---- 审计 ----

    def _audit_upstream_denied(self, decision: Decision, reason: str) -> None:
        target = decision.target
        audit_queue.enqueue(
            workspace_id=int(decision.workspace_id or 0),
            credential_id=int(target.credential_id) if target is not None else None,
            owner_kind=str(target.owner_kind or "") if target is not None else "",
            owner_account=self.owner_account,
            adapter_key=self.adapter_key,
            instance_key=self.instance_key,
            event=C.EVENT_CONNECT,
            allowed=False,
            reason=str(reason or ""),
            task_ref=str(decision.task_ref or ""),
            lease_id=str(decision.lease_id or ""),
            conn_id=self.conn_id,
            triggered_by=list(decision.triggered_by),
            channel_kind="",
            detail=f"alias={self.alias}",
            input_head="",
            exit_status=None,
            bytes_up=None,
            bytes_down=None,
            duration_ms=None,
            host_key_fp="",
            actor="gateway",
        )

    def _audit_runbook_channel(self, kind: str, subsystem: str) -> bool:
        """运维手册子系统的通道审计：种类记短名（`channel_kind` 只有 24 字）

        Returns:
            审计有没有入队；没入队（队列满）时调用方拒绝这个通道，与别的通道一样
        """
        decision = self.decision
        if decision is None or decision.target is None:
            return False
        return audit_queue.enqueue(
            workspace_id=int(decision.workspace_id or 0),
            credential_id=int(decision.target.credential_id),
            owner_kind=self.owner_kind,
            owner_account=self.owner_account,
            adapter_key=self.adapter_key,
            instance_key=self.instance_key,
            event=C.EVENT_CHANNEL,
            allowed=True,
            reason="",
            task_ref=str(decision.task_ref or ""),
            lease_id=str(decision.lease_id or ""),
            conn_id=self.conn_id,
            triggered_by=list(decision.triggered_by),
            channel_kind=str(kind),
            detail=str(subsystem),
            input_head="",
            exit_status=None,
            bytes_up=None,
            bytes_down=None,
            duration_ms=None,
            host_key_fp="",
            actor="gateway",
        )

    def _audit_connection_close(self, bytes_up: int, bytes_down: int) -> None:
        decision = self.decision
        if decision is None or decision.target is None:
            return
        target = decision.target
        audit_queue.enqueue(
            workspace_id=int(decision.workspace_id or 0),
            credential_id=int(target.credential_id),
            owner_kind=self.owner_kind,
            owner_account=self.owner_account,
            adapter_key=self.adapter_key,
            instance_key=self.instance_key,
            event=C.EVENT_CLOSE,
            allowed=True,
            reason="",
            task_ref=str(decision.task_ref or ""),
            lease_id=str(decision.lease_id or ""),
            conn_id=self.conn_id,
            triggered_by=list(decision.triggered_by),
            channel_kind="",
            detail="connection",
            input_head="",
            exit_status=None,
            bytes_up=int(bytes_up),
            bytes_down=int(bytes_down),
            duration_ms=max(0, int((time.monotonic() - self._connected_at) * 1000)),
            host_key_fp="",
            actor="gateway",
        )


class _ForwardServerSide(ServerSide):
    """`-L` 容器的 server channel：到齐后把 container 侧挂上桥"""

    def connection_made(self, chan: object) -> None:
        self.chan = chan
        try:
            chan.set_write_buffer_limits(bridge_module.HIGH_WATER, bridge_module.LOW_WATER)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        self._holds.add("starting")
        try:
            chan.pause_reading()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        self.bridge.attach_container(self)
        self.bridge.start()


class _TrackedListener:
    """`-R` 监听器的代理：取消时顺手把 `tcpip-forward` 审计关掉"""

    def __init__(self, listener: object, bridge: ChannelBridge) -> None:
        self._listener = listener
        self._bridge = bridge
        self._closed = False

    def get_port(self) -> int:
        return int(self._listener.get_port())  # type: ignore[attr-defined]

    def get_addresses(self) -> list:
        return list(self._listener.get_addresses())  # type: ignore[attr-defined]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._listener.close()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SSH 网关关闭转发监听失败: {type(exc).__name__}: {exc}")
        self._bridge.close()

    async def wait_closed(self) -> None:
        await self._listener.wait_closed()  # type: ignore[attr-defined]


class GatewaySession(ServerSide):
    """容器侧 session：exec / pty shell / sftp / 自定义 subsystem"""

    def __init__(self, gateway: GatewayServer, slot: _ChannelSlot) -> None:
        # 不调用 `ServerSide.__init__`：桥要等 exec/shell/subsystem 请求到了才知道类型
        self.gateway = gateway
        self.slot = slot
        self.bridge: Optional[ChannelBridge] = None
        self.chan: Optional[object] = None
        self.is_container = True
        self._holds: Set[str] = set()
        self._queue = deque()
        self._pumping = False
        self._bandwidth_handle = None
        self._closed = False
        self._task: Optional[asyncio.Task[None]] = None
        self._pty: Optional[Tuple[str, Tuple[int, int, int, int], object]] = None
        self._env: Dict[str, str] = {}
        self._command = ""
        self._subsystem = ""
        self._last_term_size: Optional[Tuple[int, int, int, int]] = None
        # 网关自己处理的运维手册子系统（不转发给上游）：读 / 追加
        self._runbook_mode = ""
        self._runbook_input = bytearray()
        self._runbook_input_truncated = False

    # ---- 请求 ----

    def connection_made(self, chan: object) -> None:
        self.chan = chan
        try:
            chan.set_write_buffer_limits(bridge_module.HIGH_WATER, bridge_module.LOW_WATER)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        self._holds.add("starting")
        try:
            chan.pause_reading()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        self._install_env_handler(chan)

    def _install_env_handler(self, chan: object) -> None:
        """接管这个通道的 `env` 请求

        asyncssh 的服务端通道自己处理 `env`：一律接受、存进通道的 `_env`，没有任何上限，
        也不会回调会话的 `env_requested`。容器借任何一个打开的会话发几千个 `env` 请求就能
        把 NA 撑爆。asyncssh 按 `getattr(通道, "_process_env_request")` 分派请求，所以在
        这个通道对象上装一个自己的处理函数：只收白名单里的变量（与 OpenSSH 默认
        `SendEnv LANG LC_*` 同一类），条数与长度都有上限，存进会话自己的表，建上游会话时
        原样转发；其余一律回「不接受」，不存。
        """

        def _process_env_request(packet: Any) -> bool:
            name = packet.get_string()
            value = packet.get_string()
            packet.check_end()
            return self._accept_env(name, value)

        try:
            setattr(chan, "_process_env_request", _process_env_request)  # noqa: B010 - 实例上覆盖 asyncssh 的分派目标
        except Exception as exc:  # noqa: BLE001 - 装不上就整条断开：不能退回无上限的默认处理
            logger.error(f"SSH 网关连接 {self.gateway.conn_id} 接管 env 请求失败: {type(exc).__name__}: {exc}")
            self.gateway.disconnect()

    def _accept_env(self, raw_name: bytes, raw_value: bytes) -> bool:
        if len(raw_name) > MAX_ENV_NAME_BYTES or len(raw_value) > MAX_ENV_VALUE_BYTES:
            return False
        name = raw_name.decode("ascii", "replace")
        if not _ENV_NAME_ALLOWED.fullmatch(name):
            return False
        if name not in self._env and len(self._env) >= MAX_ENV_VARS:
            return False
        self._env[name] = raw_value.decode("utf-8", "replace")
        return True

    def pty_requested(
        self,
        term_type: str,
        term_size: Tuple[int, int, int, int],
        term_modes: object,
    ) -> bool:
        self._pty = (str(term_type), term_size, term_modes)
        self._last_term_size = term_size
        return True

    def exec_requested(self, command: str) -> bool:
        self._command = str(command)
        return self._begin("exec", detail=self._command, command=self._command, record_input=True)

    def shell_requested(self) -> bool:
        return self._begin("shell", detail="", record_input=True)

    def subsystem_requested(self, subsystem: str) -> bool:
        self._subsystem = str(subsystem)
        if self._subsystem in (C.SUBSYSTEM_RUNBOOK_READ, C.SUBSYSTEM_RUNBOOK_APPEND):
            return self._begin_runbook(self._subsystem)
        kind = "sftp" if self._subsystem == "sftp" else f"subsystem:{self._subsystem}"
        return self._begin(kind, detail=self._subsystem)

    # ---- 运维手册子系统：网关自己处理，不转发给上游 ----

    def _begin_runbook(self, name: str) -> bool:
        """网关自己处理：不建桥、不取上游会话；通道名额在 `session_requested` 里已经占了

        上游在连接认证时就登过了（`_authorize_and_login`），这里不再碰目标服务器。读手册
        立刻开始；追加要等容器把内容写完（EOF）再复核、写库。
        """
        if self.bridge is not None or self._runbook_mode:
            return False
        if self.chan is None or self.slot.released or self.gateway.decision is None:
            return False
        kind = C.CHANNEL_KIND_RUNBOOK_READ if name == C.SUBSYSTEM_RUNBOOK_READ else C.CHANNEL_KIND_RUNBOOK_APPEND
        if not self.gateway._audit_runbook_channel(kind, name):  # noqa: SLF001
            # 审计队列满：与别的通道一样拒绝，不开一条没有审计的通道
            return False
        self._runbook_mode = name
        self._holds.discard("starting")
        try:
            self.chan.resume_reading()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        if name == C.SUBSYSTEM_RUNBOOK_READ:
            self._task = asyncio.create_task(self._serve_runbook_read())
        return True

    def _runbook_received(self, data: bytes) -> None:
        room = RUNBOOK_APPEND_MAX_BYTES - len(self._runbook_input)
        if room <= 0:
            self._runbook_input_truncated = True
            return
        if len(data) > room:
            self._runbook_input_truncated = True
        self._runbook_input.extend(data[:room])

    async def _runbook_row(self) -> Optional[Any]:
        decision = self.gateway.decision
        if decision is None or decision.target is None:
            return None
        if not await self.gateway._recheck():  # noqa: SLF001
            return None
        from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential

        return await DBWsSshCredential.get_or_none(id=int(decision.target.credential_id))

    async def _serve_runbook_read(self) -> None:
        try:
            row = await self._runbook_row()
            if row is None:
                self._runbook_finish("ssh gateway: 授权没有通过复核\n", 1, to_stderr=True)
                return
            from nekro_agent.services.ssh_gateway.runbook import read_full

            if row.owner_kind != C.OWNER_PLATFORM:
                self._runbook_finish("ssh gateway: 只有平台方服务器有运维手册\n", 1, to_stderr=True)
                return
            self._runbook_finish(await read_full(row), 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读失败给容器一个明确的非零退出
            logger.warning(f"SSH 网关连接 {self.gateway.conn_id} 读运维手册失败: {type(exc).__name__}")
            self._runbook_finish("ssh gateway: 读运维手册失败\n", 1, to_stderr=True)

    async def _serve_runbook_append(self) -> None:
        try:
            row = await self._runbook_row()
            if row is None:
                self._runbook_finish("ssh gateway: 授权没有通过复核\n", 1, to_stderr=True)
                return
            from nekro_agent.services.ssh_gateway.runbook import append_entry

            decision = self.gateway.decision
            text = bytes(self._runbook_input).decode("utf-8", "replace")
            ok, message = await append_entry(
                row,
                text,
                lease_id=str(getattr(decision, "lease_id", "") or ""),
                task_ref=str(getattr(decision, "task_ref", "") or ""),
            )
            if self._runbook_input_truncated:
                message += f"（输入超过 {RUNBOOK_APPEND_MAX_BYTES} 字节，只收了前 {RUNBOOK_APPEND_MAX_BYTES} 字节）"
            self._runbook_finish(message + "\n", 0 if ok else 1, to_stderr=not ok)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SSH 网关连接 {self.gateway.conn_id} 追加运维经验失败: {type(exc).__name__}")
            self._runbook_finish("ssh gateway: 追加运维经验失败\n", 1, to_stderr=True)

    def _runbook_finish(self, text: str, status: int, *, to_stderr: bool = False) -> None:
        if self.chan is None or self._closed:
            return
        try:
            payload = text.encode("utf-8")
            if to_stderr:
                self.chan.write_stderr(payload)  # type: ignore[attr-defined]
            else:
                self.chan.write(payload)  # type: ignore[attr-defined]
            self.chan.exit(int(status))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    def _begin(self, kind: str, *, detail: str, command: str = "", record_input: bool = False) -> bool:
        if self.bridge is not None:
            return False
        if self.chan is None or self.slot.released:
            return False
        bridge = self.gateway._new_bridge(
            self.slot,
            kind=kind,
            detail=detail,
            command=command,
            record_input=record_input,
        )
        bridge.attach_container(self)
        self.bridge = bridge
        if not bridge.audit_open():
            bridge.close()
            return False
        try:
            self._task = asyncio.create_task(self._open_upstream())
        except Exception as exc:  # noqa: BLE001 - 起任务失败也要把通道关干净
            logger.error(f"SSH 网关连接 {self.gateway.conn_id} 启动上游会话失败: {type(exc).__name__}: {exc}")
            self.gateway.disconnect()
            return False
        return True

    async def _open_upstream(self) -> None:
        bridge = self.bridge
        gateway = self.gateway
        if bridge is None or gateway.decision is None:
            self._fail("上游连接不可用")
            return
        try:
            if not await gateway._recheck():
                self._fail("通道复核没有通过")
                return
            pooled = await gateway._acquire_upstream(self.slot)
            if bridge.closed:
                return
            target_session = ClientSide(bridge)
            kwargs: Dict[str, Any] = {
                "encoding": None,
                "window": bridge_module.WINDOW_SIZE,
            }
            if self._pty is not None:
                kwargs["request_pty"] = True
                kwargs["term_type"] = self._pty[0]
                kwargs["term_size"] = self._pty[1]
                kwargs["term_modes"] = self._pty[2]
            if self._env:
                kwargs["env"] = tuple(self._env.items())
            if self._subsystem:
                kwargs["subsystem"] = self._subsystem
            elif self._command:
                kwargs["command"] = self._command
            await asyncio.wait_for(
                pooled.conn.create_session(lambda: target_session, **kwargs),
                timeout=SESSION_OPEN_TIMEOUT_SECONDS,
            )
            bridge.attach_target(target_session)
            bridge.start()
            if self._last_term_size is not None and target_session.chan is not None:
                try:
                    target_session.chan.change_terminal_size(*self._last_term_size)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001 - 上游建会话失败：给容器一个明确的非零退出
            logger.warning(f"SSH 网关连接 {gateway.conn_id} 上游会话失败: {type(exc).__name__}: {exc}")
            self._fail("上游会话建立失败")

    def _fail(self, message: str) -> None:
        if self.chan is None:
            return
        try:
            self.chan.write_stderr(("ssh gateway: " + message + "\n").encode("utf-8"))  # type: ignore[attr-defined]
            self.chan.exit(255)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        if self.bridge is not None:
            self.bridge.close()

    # ---- 数据/信号 ----

    def data_received(self, data: bytes, datatype: Optional[int] = None) -> None:
        if self._runbook_mode:
            if self._runbook_mode == C.SUBSYSTEM_RUNBOOK_APPEND and datatype is None:
                self._runbook_received(bytes(data))
            return
        if self.bridge is not None:
            self.bridge.forward(self, data, datatype)

    def eof_received(self) -> bool:
        if self._runbook_mode:
            if self._runbook_mode == C.SUBSYSTEM_RUNBOOK_APPEND and self._task is None:
                self._task = asyncio.create_task(self._serve_runbook_append())
            return True
        if self.bridge is not None:
            self.bridge.eof(self)
            return True
        return False

    def terminal_size_changed(self, width: int, height: int, pixwidth: int, pixheight: int) -> None:
        size = (int(width), int(height), int(pixwidth), int(pixheight))
        self._last_term_size = size
        if self.bridge is not None and self.bridge.target is not None and self.bridge.target.chan is not None:
            try:
                self.bridge.target.chan.change_terminal_size(*size)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    def break_received(self, msec: int) -> bool:
        if self.bridge is not None and self.bridge.target is not None and self.bridge.target.chan is not None:
            try:
                self.bridge.target.chan.send_break(int(msec))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        return True

    def signal_received(self, signal: str) -> None:
        if self.bridge is not None and self.bridge.target is not None and self.bridge.target.chan is not None:
            try:
                self.bridge.target.chan.send_signal(str(signal))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self.bridge is not None:
            self.bridge.close()
        # 没走到 exec/shell 的会话也占着名额：在这里还
        self.slot.release()

    def pause_writing(self) -> None:
        if self.bridge is not None:
            self.bridge.hold_peer(self, "write")

    def resume_writing(self) -> None:
        if self.bridge is not None:
            self.bridge.unhold_peer(self, "write")


backend = GatewayBackendImpl()
supervisor.supervisor.register_backend(backend)


__all__ = ["GatewayBackendImpl", "GatewayServer", "GatewaySession", "backend"]
