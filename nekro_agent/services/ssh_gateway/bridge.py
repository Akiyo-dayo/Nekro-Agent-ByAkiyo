"""容器侧通道与目标服务器侧通道之间的双向桥

## 为什么要自己搬字节

网关不是端口转发器：一条容器侧 SSH 连接上可能同时有 exec、pty shell、sftp、
`-L` 直连、`-R` 反向监听。每个通道都要单独复核授权、单独记审计、单独做背压与
带宽限制，还要在监督任务要求让路时整批暂停。asyncssh 自带的 `forward_connection`
只适合"原样转发"，带不了这些策略，所以这里显式地两边各拿一个 channel/session，
用 `data_received` 回调搬。

## 背压

容器侧或目标侧的写缓冲超过 128 KB，就暂停读另一侧；降到 64 KB 以下再恢复。
两边窗口都设 512 KB，但真正限制在途数据的是写缓冲水位：不暂停读，慢的一端会把
快的一端的内存吃光。

## 带宽

每写一块之前先问 `bandwidth.try_take()`。它返回还需要等几秒；大于 0 就暂停读
这一侧，`call_later` 到点再恢复。数据留在队列里，一行不丢：容器→目标是 `up`，
目标→容器是 `down`。

## 审计

通道打开时记 `channel`（exec 另记 `exec`），关闭时记 `close`，带字节数、时长、
退出码和前若干字节输入。输入按 UTF-8 解码，解不开的字节用 `backslashreplace`
转义，保证审计表里永远是合法文本。审计队列满时拒绝新通道，不让内存继续涨。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional, Set, Tuple

import asyncssh

from nekro_agent.core.config import config
from nekro_agent.services.ssh_gateway import audit_queue, bandwidth
from nekro_agent.services.ssh_gateway import constants as C

HIGH_WATER = 128 * 1024
LOW_WATER = 64 * 1024
WINDOW_SIZE = 512 * 1024

# 监督任务让路时，所有活着的桥一起暂停读
_active: Set["ChannelBridge"] = set()
_yield_paused = False


@dataclass(frozen=True)
class BridgeContext:
    """一条容器连接在授权那一刻定格的审计快照"""

    conn_id: str
    workspace_id: int
    credential_id: int
    owner_kind: str
    owner_account: str
    adapter_key: str
    instance_key: str
    lease_id: str
    task_ref: str
    triggered_by: Tuple[str, ...] = ()


def set_yield_paused(value: bool) -> None:
    """监督任务要求让路/恢复：暂停或恢复所有桥的读"""
    global _yield_paused
    _yield_paused = bool(value)
    for item in list(_active):
        item.set_yield_paused(_yield_paused)


def active_bridges() -> int:
    return len(_active)


def reset_bridges() -> None:
    """测试清理：关掉所有桥并清空让路标记"""
    global _yield_paused
    _yield_paused = False
    for item in list(_active):
        item.close()
    _active.clear()


def _input_limit() -> int:
    try:
        return max(0, int(config.SSH_GATEWAY_AUDIT_INPUT_BYTES))
    except (TypeError, ValueError):
        return 0


class _SideMixin:
    """两侧共用的状态：队列、暂停原因、写缓冲回调"""

    is_container: bool = False

    def _init_side(self, bridge: "ChannelBridge", *, is_container: bool) -> None:
        self.bridge = bridge
        self.is_container = is_container
        self.chan: Optional[object] = None
        self._holds: Set[str] = set()
        self._queue: Deque[Tuple[bytes, Optional[int]]] = deque()
        self._pumping = False
        self._bandwidth_handle: Optional[asyncio.TimerHandle] = None
        self._closed = False

    # ---- asyncssh 会话回调 ----

    def connection_made(self, chan: object) -> None:
        self.chan = chan
        try:
            chan.set_write_buffer_limits(HIGH_WATER, LOW_WATER)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 某些通道类型没有写缓冲水位
            pass
        # 另一侧还没到齐前先不读：此时读到的数据没有地方可写
        self._holds.add("starting")
        try:
            chan.pause_reading()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        self.bridge.side_ready(self)

    def data_received(self, data: bytes, datatype: Optional[int] = None) -> None:
        self.bridge.forward(self, data, datatype)

    def eof_received(self) -> bool:
        self.bridge.eof(self)
        return True

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self._closed = True
        self.bridge.side_lost(self, exc)

    def pause_writing(self) -> None:
        self.bridge.hold_peer(self, "write")

    def resume_writing(self) -> None:
        self.bridge.unhold_peer(self, "write")

    # ---- 桥内部 ----

    def hold(self, reason: str) -> None:
        if reason in self._holds:
            return
        was_empty = not self._holds
        self._holds.add(reason)
        if was_empty and self.chan is not None:
            try:
                self.chan.pause_reading()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    def unhold(self, reason: str) -> None:
        if reason not in self._holds:
            return
        self._holds.discard(reason)
        if not self._holds and self.chan is not None:
            try:
                self.chan.resume_reading()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    def repause(self) -> None:
        """有暂停原因时确保通道真的停读

        asyncssh 接受 `exec` / `shell` / `subsystem` 之后会自己调一次 `resume_reading()`，
        绕过了这里记的暂停原因；上游会话还没建好时数据就会一直进队列。数据一到就补一次
        暂停，之后的数据留在 asyncssh 的通道里，受窗口大小约束，不会无界增长。
        """
        if self._holds and self.chan is not None:
            try:
                self.chan.pause_reading()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    def write_data(self, data: bytes, datatype: Optional[int]) -> None:
        if self._closed or self.chan is None:
            return
        try:
            if datatype:
                self.chan.write(data, datatype)  # type: ignore[attr-defined]
            else:
                self.chan.write(data)  # type: ignore[attr-defined]
        except (OSError, asyncssh.Error):
            self.close_channel()

    def write_eof(self) -> None:
        if self._closed or self.chan is None:
            return
        try:
            self.chan.write_eof()  # type: ignore[attr-defined]
        except (OSError, asyncssh.Error):
            pass

    def close_channel(self) -> None:
        if self._closed or self.chan is None:
            return
        try:
            self.chan.close()  # type: ignore[attr-defined]
        except (OSError, asyncssh.Error):
            pass

    def cancel_timer(self) -> None:
        if self._bandwidth_handle is not None:
            self._bandwidth_handle.cancel()
            self._bandwidth_handle = None


class ServerSide(_SideMixin, asyncssh.SSHServerSession):
    """容器侧的 server channel 回调"""

    is_container = True

    def __init__(self, bridge: "ChannelBridge") -> None:
        self._init_side(bridge, is_container=True)

    def exit(self, status: int) -> None:
        if self.chan is None:
            return
        try:
            self.chan.exit(int(status) & 0xFF)  # type: ignore[attr-defined]
        except (OSError, asyncssh.Error):
            self.close_channel()

    def exit_with_signal(self, signal: str, core_dumped: bool, msg: str, lang: str) -> None:
        if self.chan is None:
            return
        try:
            self.chan.exit_with_signal(signal, core_dumped, msg, lang)  # type: ignore[attr-defined]
        except (OSError, asyncssh.Error):
            self.close_channel()


class ClientSide(_SideMixin, asyncssh.SSHClientSession):
    """client channel 回调；`-R` 时容器侧也是 client channel，用 `is_container` 区分方向"""

    def __init__(self, bridge: "ChannelBridge", *, is_container: bool = False) -> None:
        self._init_side(bridge, is_container=is_container)
        self.exit_status: Optional[int] = None
        self.exit_signal: Optional[Tuple[str, bool, str, str]] = None

    def exit_status_received(self, status: int) -> None:
        self.exit_status = int(status)

    def exit_signal_received(self, signal: str, core_dumped: bool, msg: str, lang: str) -> None:
        self.exit_signal = (str(signal), bool(core_dumped), str(msg), str(lang))


class ChannelBridge:
    """一条通道的双向桥；只由网关会话/转发会话创建"""

    def __init__(
        self,
        context: BridgeContext,
        *,
        kind: str,
        detail: str = "",
        command: str = "",
        record_input: bool = False,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        self.context = context
        self.kind = str(kind)
        self.detail = str(detail or "")
        self.command = str(command or "")
        self.record_input = bool(record_input)
        self.on_close = on_close
        self.container: Optional[_SideMixin] = None
        self.target: Optional[ClientSide] = None
        self.bytes_up = 0
        self.bytes_down = 0
        self.input_head = bytearray()
        self._input_full = False
        self._audit_opened = False
        self._closed = False
        self._pending_eof: Deque[str] = deque()
        self._started_at = time.monotonic()
        self._opened_at: Optional[float] = None
        _active.add(self)

    @property
    def closed(self) -> bool:
        return self._closed

    # ---- 生命周期 ----

    def attach_container(self, side: _SideMixin) -> None:
        self.container = side

    def attach_target(self, side: ClientSide) -> None:
        self.target = side

    def side_ready(self, _side: object) -> None:
        self.start()

    def start(self) -> None:
        """两侧都到齐后开审计，并让两侧开始读；可重复调用"""
        if self._closed:
            return
        if self.container is None or self.target is None:
            return
        if not self._audit_opened:
            if not self._audit_channel_open():
                self.close()
                return
            self._audit_opened = True
            self._opened_at = time.monotonic()
        for side in (self.container, self.target):
            if _yield_paused:
                side.hold("yield")
            side.unhold("starting")
        # 容器侧的数据/EOF 可能比上游 session 先到（exec 请求一到客户端就会发输入），
        # 这些事件已经排进各自队列；现在两侧都到齐，先补泵数据，最后再补发 EOF。
        for side in (self.container, self.target):
            if side is not None:
                self._pump(side)
        self._flush_pending()

    def _flush_pending(self) -> None:
        """补发还压着的 EOF：只有发出 EOF 的那一侧的数据全部搬完才发"""
        if self._closed:
            return
        remaining: Deque[str] = deque()
        while self._pending_eof:
            key = self._pending_eof.popleft()
            side = self.container if key == "container" else self.target
            peer = self.target if key == "container" else self.container
            if peer is None or (side is not None and side._queue):  # type: ignore[attr-defined]
                remaining.append(key)
                continue
            peer.write_eof()
        self._pending_eof = remaining

    def audit_open(self) -> bool:
        """在通道请求阶段先占一行审计；失败时调用方应拒绝请求"""
        if self._audit_opened:
            return True
        if not self._audit_channel_open():
            return False
        self._audit_opened = True
        self._opened_at = time.monotonic()
        return True

    def _audit_channel_open(self) -> bool:
        fields = {
            **self._base_audit_fields(),
            "event": C.EVENT_CHANNEL,
            "allowed": True,
            "reason": "",
            "channel_kind": self.kind,
            "detail": self.detail,
            "input_head": "",
            "exit_status": None,
            "bytes_up": None,
            "bytes_down": None,
            "duration_ms": None,
            "host_key_fp": "",
            "actor": "gateway",
        }
        if not audit_queue.enqueue(**fields):
            return False
        if self.kind == C.EVENT_EXEC:
            exec_fields = {
                **fields,
                "event": C.EVENT_EXEC,
                "detail": self.command,
            }
            if not audit_queue.enqueue(**exec_fields):
                return False
        return True

    def _base_audit_fields(self) -> dict:
        return {
            "workspace_id": int(self.context.workspace_id),
            "credential_id": int(self.context.credential_id),
            "owner_kind": self.context.owner_kind,
            "owner_account": self.context.owner_account,
            "adapter_key": self.context.adapter_key,
            "instance_key": self.context.instance_key,
            "task_ref": self.context.task_ref,
            "lease_id": self.context.lease_id,
            "conn_id": self.context.conn_id,
            "triggered_by": list(self.context.triggered_by),
        }

    def set_yield_paused(self, value: bool) -> None:
        for side in (self.container, self.target):
            if side is None:
                continue
            if value:
                side.hold("yield")
            else:
                side.unhold("yield")

    # ---- 数据 ----

    def forward(self, side: object, data: bytes, datatype: Optional[int]) -> None:
        if self._closed:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        else:
            data = bytes(data)
        side._queue.append((data, datatype))  # type: ignore[attr-defined]
        self._pump(side)  # type: ignore[arg-type]
        if side._queue:  # type: ignore[attr-defined]
            # 还有没搬走的数据：说明这一侧正被某个原因挡着（另一侧没到齐、带宽、让路），
            # 确保通道真的停读，否则队列会跟着对端的发送速度无界增长
            side.repause()  # type: ignore[attr-defined]

    def _pump(self, side: object) -> None:
        if side._pumping or self._closed:  # type: ignore[attr-defined]
            return
        side._pumping = True  # type: ignore[attr-defined]
        try:
            while side._queue:  # type: ignore[attr-defined]
                peer = self.target if side.is_container else self.container  # type: ignore[attr-defined]
                if peer is None:
                    # 另一侧还没到齐：数据留在队列里，等 `start()` 补泵，不能丢
                    return
                data, datatype = side._queue[0]  # type: ignore[attr-defined]
                direction = "up" if side.is_container else "down"  # type: ignore[attr-defined]
                wait = bandwidth.try_take(direction, len(data))
                if wait > 0:
                    side.hold("bandwidth")  # type: ignore[attr-defined]
                    loop = asyncio.get_running_loop()
                    side._bandwidth_handle = loop.call_later(  # type: ignore[attr-defined]
                        wait,
                        self._bandwidth_ready,
                        side,
                    )
                    return
                side._queue.popleft()  # type: ignore[attr-defined]
                self._count(side, data)
                peer.write_data(data, datatype)
        except (OSError, asyncssh.Error):
            self.close()
        finally:
            side._pumping = False  # type: ignore[attr-defined]
        if self._pending_eof and not side._queue:  # type: ignore[attr-defined]
            self._flush_pending()

    def _bandwidth_ready(self, side: object) -> None:
        side._bandwidth_handle = None  # type: ignore[attr-defined]
        side.unhold("bandwidth")  # type: ignore[attr-defined]
        self._pump(side)
        self._flush_pending()

    def _count(self, side: object, data: bytes) -> None:
        if side.is_container:  # type: ignore[attr-defined]
            self.bytes_up += len(data)
            if self.record_input and not self._input_full:
                limit = _input_limit()
                if limit <= 0:
                    self._input_full = True
                    return
                room = limit - len(self.input_head)
                if room > 0:
                    self.input_head.extend(data[:room])
                if len(self.input_head) >= limit:
                    self._input_full = True
        else:
            self.bytes_down += len(data)

    def eof(self, side: object) -> None:
        """EOF 要排在这一侧所有数据之后：带宽限速时最后几块还压在队列里，先转 EOF 会截断"""
        key = "container" if side.is_container else "target"  # type: ignore[attr-defined]
        peer = self.target if key == "container" else self.container
        if peer is None or side._queue:  # type: ignore[attr-defined]
            if key not in self._pending_eof:
                self._pending_eof.append(key)
            return
        peer.write_eof()

    def hold_peer(self, side: object, reason: str) -> None:
        peer = self.target if side.is_container else self.container  # type: ignore[attr-defined]
        if peer is not None:
            peer.hold(reason)

    def unhold_peer(self, side: object, reason: str) -> None:
        peer = self.target if side.is_container else self.container  # type: ignore[attr-defined]
        if peer is not None:
            peer.unhold(reason)

    def side_lost(self, side: object, _exc: Optional[Exception]) -> None:
        if self._closed:
            return
        self._drain_lost_side(side)
        if side is self.target and self.kind in {C.EVENT_EXEC, "shell"}:
            self._send_exit()
        self.close()

    def _drain_lost_side(self, side: object) -> None:
        """一侧断了：它已经收到、还压在队列里的数据照样交给另一侧（不再限速），再补 EOF

        不这样做的话，限速时最后一段数据会随着 `close()` 一起丢掉，文件被静默截断。
        队列本身受暂停读约束，不会很大。
        """
        peer = self.target if side.is_container else self.container  # type: ignore[attr-defined]
        if peer is None:
            return
        queue = side._queue  # type: ignore[attr-defined]
        side.cancel_timer()  # type: ignore[attr-defined]
        while queue:
            data, datatype = queue.popleft()
            self._count(side, data)
            peer.write_data(data, datatype)
        key = "container" if side.is_container else "target"  # type: ignore[attr-defined]
        if key in self._pending_eof:
            self._pending_eof = deque(item for item in self._pending_eof if item != key)
            peer.write_eof()

    def _send_exit(self) -> None:
        if self.container is None or self.target is None:
            return
        target = self.target
        status = target.exit_status
        if status is None and target.chan is not None:
            try:
                status = target.chan.get_exit_status()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                status = None
        if status is not None:
            container = self.container
            if isinstance(container, ServerSide):
                container.exit(int(status) & 0xFF)
            return
        signal = target.exit_signal
        if signal is None and target.chan is not None:
            try:
                raw = target.chan.get_exit_signal()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                raw = None
            if raw:
                signal = (str(raw[0]), bool(raw[1]), str(raw[2]), str(raw[3]))
        if signal is not None and isinstance(self.container, ServerSide):
            self.container.exit_with_signal(signal[0], signal[1], signal[2], signal[3])
        else:
            self.container.close_channel()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for side in (self.container, self.target):
            if side is None:
                continue
            side.cancel_timer()
            side.close_channel()
        _active.discard(self)
        self._audit_close()
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:  # noqa: BLE001 - 计数回调出错不能影响关闭
                pass

    def _audit_close(self) -> None:
        if not self._audit_opened:
            return
        exit_status: Optional[int] = None
        if self.target is not None and self.target.chan is not None and self.kind in {C.EVENT_EXEC, "shell"}:
            try:
                exit_status = self.target.chan.get_returncode()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                exit_status = None
        duration_ms = 0
        if self._opened_at is not None:
            duration_ms = max(0, int((time.monotonic() - self._opened_at) * 1000))
        audit_queue.enqueue(
            **self._base_audit_fields(),
            event=C.EVENT_CLOSE,
            allowed=True,
            reason="",
            channel_kind=self.kind,
            detail=self.detail,
            input_head=self.input_head.decode("utf-8", "backslashreplace") if self.record_input else "",
            exit_status=exit_status,
            bytes_up=self.bytes_up,
            bytes_down=self.bytes_down,
            duration_ms=duration_ms,
            host_key_fp="",
            actor="gateway",
        )


__all__ = [
    "BridgeContext",
    "ChannelBridge",
    "ClientSide",
    "HIGH_WATER",
    "LOW_WATER",
    "ServerSide",
    "WINDOW_SIZE",
    "active_bridges",
    "reset_bridges",
    "set_yield_paused",
]
