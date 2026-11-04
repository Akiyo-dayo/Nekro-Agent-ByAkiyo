"""账号绑定之后，在线 OneBot 连接按新绑定复核、入站逐条判（绑定变化的机制本身）

规则见 `nekro_agent/adapters/onebot_v11/core/access_token.py` 的「账号绑定之后：旧连接」：

- 一条连接对某个账号「此刻仍被认可」= 它握手时**出示**的令牌，此刻重新握手仍会被接受；
- 只在账号绑到一个**已认领**实例时判；该实例应出示的令牌为空一律认可；
- 两处执行：绑定变化时复核在线连接（`revalidate_live_connections`），入站每条事件再判一次
  （`unclaimed_dispatch.is_inbound_dispatch_allowed`，对不上就断开当前连接）。

这里只搭判据需要的环境：真的握手中间件（驱动 ASGI，不起服务器）、真的 `LiveConnection`、
真的入站闸门；不拉 NoneBot 适配器（那条链路由 `tests/test_onebot_binding_takeover.py` 端到端钉住）。
"""

import asyncio
import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, MutableMapping, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.adapters.onebot_v11.core import access_token as gate
from nekro_agent.adapters.onebot_v11.core.access_token import (
    HandshakeRecord,
    LiveConnection,
    OneBotAccessTokenMiddleware,
    TokenSource,
    connection_accepted_by,
    current_connection,
    revalidate_live_connections,
    schedule_live_connection_revalidation,
    wait_for_pending_revalidation,
)
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.services import unclaimed_dispatch
from nekro_agent.services.console import instance_manager
from nekro_agent.services.unclaimed_dispatch import is_inbound_dispatch_allowed
from nekro_agent.services.user import token_exposure
from nekro_agent.services.user.token_exposure import token_fingerprint

ONEBOT = "onebot_v11"
DEPLOYMENT_TOKEN = "deployment-token"
ACCOUNT = "40001"
PLATFORM = "plat"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from nekro_agent.core.os_env import OsEnv

    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    token_exposure._write([], seeded=True)  # noqa: SLF001
    monkeypatch.setattr(token_exposure, "_rebuild_pending", False)
    monkeypatch.setattr(token_exposure, "_rebuild_lock", None)
    monkeypatch.setattr(token_exposure, "_unwritten_fingerprints", set())
    monkeypatch.setattr(unclaimed_dispatch, "_last_warned", {})
    monkeypatch.setattr(unclaimed_dispatch, "_token_mismatch_last_warned", {})
    set_deployment_token(monkeypatch, DEPLOYMENT_TOKEN)
    gate._presented_by_unbound_account.clear()  # noqa: SLF001
    gate._live_connections.clear()  # noqa: SLF001
    gate._revalidation_scheduled = False  # noqa: SLF001
    yield
    gate._live_connections.clear()  # noqa: SLF001
    gate._revalidation_scheduled = False  # noqa: SLF001


def set_deployment_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    if token:
        monkeypatch.setenv("ONEBOT_ACCESS_TOKEN", token)
    else:
        monkeypatch.delenv("ONEBOT_ACCESS_TOKEN", raising=False)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def add_instance(
    instance_key: str,
    *,
    account: str = "",
    token: str = "",
    status: str = "offline",
    provider: str = "remote_onebot",
    tenant_id: Optional[int] = None,
) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ONEBOT,
        instance_key=instance_key,
        display_name=instance_key,
        provider=provider,
        provider_account_id=account,
        status=status,
        enabled=True,
        tenant_id=tenant_id,
        metadata_json=json.dumps({"access_token": token}) if token else "",
    )


def record(
    presented: str,
    *,
    self_id: str = ACCOUNT,
    instance_key: str = "",
    expected: Optional[str] = None,
    source: TokenSource = TokenSource.INSTANCE,
) -> HandshakeRecord:
    """手工构造一条握手记录；`expected` 不传时与出示的令牌相同（正常握手的形态）"""
    return HandshakeRecord(
        self_id=self_id,
        source=source,
        instance_key=instance_key,
        token_fingerprint=token_fingerprint(presented if expected is None else expected),
        presented_fingerprint=token_fingerprint(presented),
    )


class RecordingConnection(LiveConnection):
    """把发出去的消息记下来的 `LiveConnection`（服务端那一侧的 `send`）"""

    def __init__(self, handshake: HandshakeRecord) -> None:
        self.sent: List[Dict[str, Any]] = []
        super().__init__(handshake, self._record)

    async def _record(self, message: MutableMapping[str, Any]) -> None:
        self.sent.append(dict(message))


def register(handshake: HandshakeRecord) -> RecordingConnection:
    """直接往登记表里放一条连接（不经过 ASGI），返回它"""
    connection = RecordingConnection(handshake)
    gate._live_connections.add(connection)  # noqa: SLF001
    return connection


@contextmanager
def connection_context(handshake: Optional[HandshakeRecord], live: Optional[LiveConnection] = None) -> Iterator[None]:
    """让下面的代码像是跑在「凭这条握手记录连上来的连接」派发的事件里"""
    reset_handshake = gate._current_handshake.set(handshake)  # noqa: SLF001
    reset_connection = gate._current_connection.set(live)  # noqa: SLF001
    try:
        yield
    finally:
        gate._current_connection.reset(reset_connection)  # noqa: SLF001
        gate._current_handshake.reset(reset_handshake)  # noqa: SLF001


async def allowed(instance_key: str, handshake: Optional[HandshakeRecord], live: Optional[LiveConnection] = None) -> bool:
    with connection_context(handshake, live):
        return await is_inbound_dispatch_allowed(ONEBOT, instance_key)


# ---------------------------------------------------------------------------
# 判据：纯函数
# ---------------------------------------------------------------------------


class TestConnectionAcceptedBy:
    def test_an_empty_expected_token_accepts_anything(self) -> None:
        assert connection_accepted_by(record(""), "") is True
        assert connection_accepted_by(record("whatever"), "") is True

    def test_the_same_presented_token_is_accepted(self) -> None:
        assert connection_accepted_by(record("own"), "own") is True

    def test_a_different_presented_token_is_refused(self) -> None:
        assert connection_accepted_by(record("tenant"), "platform") is False

    def test_nothing_presented_is_refused_when_a_token_is_expected(self) -> None:
        assert connection_accepted_by(record(""), "platform") is False


# ---------------------------------------------------------------------------
# 中间件：登记表与 ContextVar
# ---------------------------------------------------------------------------


def ws_scope(self_id: str, token: Optional[str]) -> Dict[str, Any]:
    headers: List[Tuple[bytes, bytes]] = [(b"host", b"testserver"), (b"x-self-id", self_id.encode("latin-1"))]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
        "path": "/onebot/v11/ws",
        "raw_path": b"/onebot/v11/ws",
        "query_string": b"",
        "headers": headers,
        "subprotocols": [],
        "state": {},
    }


def http_scope(self_id: str, token: Optional[str]) -> Dict[str, Any]:
    headers: List[Tuple[bytes, bytes]] = [(b"host", b"testserver"), (b"x-self-id", self_id.encode("latin-1"))]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
    return {"type": "http", "method": "POST", "path": "/onebot/v11/", "headers": headers}


class Seen:
    """下游看到的东西：握手记录、连接句柄、这条连接是否还在登记表里"""

    def __init__(self) -> None:
        self.handshake: Optional[HandshakeRecord] = None
        self.connection: Optional[LiveConnection] = None
        self.registered: bool = False


async def through_gate(scope: Dict[str, Any], seen: Seen, *, release: Optional[asyncio.Event] = None) -> None:
    async def downstream(_scope: MutableMapping[str, Any], _receive: Any, _send: Any) -> None:
        seen.handshake = await asyncio.create_task(_read_handshake())
        seen.connection = current_connection()
        seen.registered = seen.connection in gate._live_connections  # noqa: SLF001
        if release is not None:
            await release.wait()

    async def receive() -> Dict[str, Any]:
        return {"type": "websocket.connect"} if scope["type"] == "websocket" else {"type": "http.request"}

    async def send(_message: MutableMapping[str, Any]) -> None:
        return None

    await OneBotAccessTokenMiddleware(downstream)(scope, receive, send)


async def _read_handshake() -> Optional[HandshakeRecord]:
    await asyncio.sleep(0)
    return gate.current_handshake()


class TestMiddlewareRegistration:
    async def test_an_accepted_ws_is_registered_and_readable_downstream(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        seen = Seen()

        await through_gate(ws_scope(ACCOUNT, "own"), seen)

        assert seen.handshake is not None and seen.handshake.presented_fingerprint == token_fingerprint("own")
        assert seen.connection is not None and seen.connection.record is seen.handshake
        assert seen.registered is True
        # 连接结束后移除，ContextVar 也复位
        assert gate._live_connections == set()  # noqa: SLF001
        assert current_connection() is None

    async def test_an_empty_deployment_token_still_records_what_was_presented(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """部署级令牌为空时握手不校验，`token_fingerprint` 是空串，但出示了什么必须记下来"""
        set_deployment_token(monkeypatch, "")
        seen = Seen()

        await through_gate(ws_scope(ACCOUNT, "own"), seen)

        assert seen.handshake is not None
        assert seen.handshake.token_fingerprint == ""
        assert seen.handshake.presented_fingerprint == token_fingerprint("own")

    async def test_http_reports_are_not_registered(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        seen = Seen()

        await through_gate(http_scope(ACCOUNT, "own"), seen)

        assert seen.handshake is not None
        assert seen.connection is None
        assert gate._live_connections == set()  # noqa: SLF001

    async def test_a_rejected_handshake_is_not_registered(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        seen = Seen()

        await through_gate(ws_scope(ACCOUNT, "wrong"), seen)

        assert seen.handshake is None
        assert seen.connection is None
        assert gate._live_connections == set()  # noqa: SLF001


# ---------------------------------------------------------------------------
# LiveConnection：断开一次、之后拒绝写
# ---------------------------------------------------------------------------


class TestLiveConnection:
    async def test_cut_sends_one_close_and_then_returns_false(self) -> None:
        live = register(record("own"))

        assert live.cut("reason") is True
        assert live.cut("reason") is False
        await asyncio.sleep(0)

        closes = [message for message in live.sent if message["type"] == "websocket.close"]
        assert len(closes) == 1
        assert closes[0]["code"] == 1008
        assert closes[0]["reason"] == "reason"

    async def test_after_cut_accept_and_close_are_swallowed_and_send_raises(self) -> None:
        live = register(record("own"))
        live.cut("reason")
        await asyncio.sleep(0)
        live.sent.clear()

        await live.send({"type": "websocket.accept"})
        await live.send({"type": "websocket.close", "code": 1000})
        assert live.sent == []

        # `OSError`：Starlette 把它翻成 `WebSocketDisconnect(1006)`，与往真断掉的连接上写一样
        with pytest.raises(OSError, match="closed by NekroAgent"):
            await live.send({"type": "websocket.send", "text": "{}"})

    async def test_after_cut_starlette_reports_a_dropped_connection(self) -> None:
        """经过 Starlette 的 `WebSocket.send`：断开之后出站得到的是 `WebSocketDisconnect`，不是莫名的错误"""
        from starlette.websockets import WebSocket, WebSocketDisconnect

        live = register(record("own"))

        async def receive() -> Dict[str, Any]:
            return {"type": "websocket.connect"}

        websocket = WebSocket({"type": "websocket", "path": "/onebot/v11/ws", "headers": []}, receive, live.send)
        await websocket.accept()
        live.cut("reason")

        with pytest.raises(WebSocketDisconnect):
            await websocket.send_text("{}")

    async def test_a_live_connection_forwards_everything(self) -> None:
        live = register(record("own"))
        message = {"type": "websocket.send", "text": "{}"}

        await live.send(message)

        assert live.sent == [message]


# ---------------------------------------------------------------------------
# 复核在线连接
# ---------------------------------------------------------------------------


class TestRevalidateLiveConnections:
    async def test_an_unbound_account_is_not_judged(self, db) -> None:
        live = register(record("whatever"))

        assert await revalidate_live_connections() == 0
        assert live.closed is False

    async def test_an_unclaimed_instance_is_not_judged(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own", status="unclaimed")
        live = register(record("whatever"))

        assert await revalidate_live_connections() == 0
        assert live.closed is False

    async def test_a_matching_connection_survives(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        live = register(record("own"))

        assert await revalidate_live_connections() == 0
        assert live.closed is False

    async def test_a_mismatched_connection_is_cut(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        live = register(record("tenant"))

        assert await revalidate_live_connections() == 1
        assert live.closed is True
        await asyncio.sleep(0)
        assert [message["type"] for message in live.sent] == ["websocket.close"]

    async def test_only_the_mismatched_connection_of_an_account_is_cut(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        good = register(record("own"))
        bad = register(record("tenant"))

        assert await revalidate_live_connections() == 1
        assert good.closed is False
        assert bad.closed is True

    async def test_a_failing_query_leaves_every_connection_of_that_account_alone(
        self, db, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        live = register(record("tenant"))

        async def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("数据库抖了一下")

        monkeypatch.setattr(DBAdapterInstance, "get_by_bound_account", broken)
        assert await revalidate_live_connections() == 0
        assert live.closed is False

    async def test_an_empty_expected_token_accepts_the_connection(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        """实例自己没有令牌、部署级令牌也为空：本来就不校验，不能断"""
        set_deployment_token(monkeypatch, "")
        await add_instance(PLATFORM, account=ACCOUNT)
        live = register(record("tenant"))

        assert await revalidate_live_connections() == 0
        assert live.closed is False

    async def test_a_broken_metadata_row_leaves_the_connection_alone(self, db) -> None:
        row = await add_instance(PLATFORM, account=ACCOUNT, token="own")
        await DBAdapterInstance.filter(id=row.id).update(metadata_json=json.dumps({"access_token": ["不是字符串"]}))
        live = register(record("tenant"))

        assert await revalidate_live_connections() == 0
        assert live.closed is False


# ---------------------------------------------------------------------------
# 排复核：只在有连接、有事件循环时排一个
# ---------------------------------------------------------------------------


class TestScheduleRevalidation:
    async def test_nothing_to_do_without_live_connections(self) -> None:
        schedule_live_connection_revalidation()
        assert gate._revalidation_tasks == set()  # noqa: SLF001

    def test_no_running_loop_is_not_an_error(self) -> None:
        register(record("own"))
        schedule_live_connection_revalidation()  # 不抛
        assert gate._revalidation_tasks == set()  # noqa: SLF001

    async def test_two_schedules_before_it_starts_run_once(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        register(record("tenant"))

        schedule_live_connection_revalidation()
        schedule_live_connection_revalidation()
        assert len(gate._revalidation_tasks) == 1  # noqa: SLF001

        await wait_for_pending_revalidation()
        assert gate._revalidation_tasks == set()  # noqa: SLF001
        assert gate._revalidation_scheduled is False  # noqa: SLF001


# ---------------------------------------------------------------------------
# 入站闸门：已认领实例的兜底
# ---------------------------------------------------------------------------


class TestGate:
    async def test_a_matching_handshake_is_dispatched(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        live = register(record("own", instance_key=PLATFORM))

        assert await allowed(PLATFORM, live.record, live) is True
        assert live.closed is False

    async def test_a_mismatched_handshake_is_refused_and_cuts_the_connection(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        live = register(record("tenant", instance_key=PLATFORM))

        assert await allowed(PLATFORM, live.record, live) is False
        assert live.closed is True

    async def test_a_handshake_of_another_account_is_not_judged_and_cuts_nothing(self, db) -> None:
        """继承了另一条连接上下文的任务替这个实例投递：握手账号不是它绑定的账号，不判、不断开那条无关的连接"""
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        live = register(record("tenant", self_id="49999"))

        assert await allowed(PLATFORM, live.record, live) is True
        assert live.closed is False

    async def test_no_handshake_is_dispatched(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")

        assert await allowed(PLATFORM, None) is True

    async def test_another_adapter_with_a_mismatched_handshake_is_dispatched(self, db) -> None:
        """别的适配器的实例行真实存在、元数据里也有 `access_token`：握手记录对它没有意义，不判、不断开"""
        await DBAdapterInstance.create(
            adapter_key="telegram",
            instance_key=PLATFORM,
            display_name=PLATFORM,
            provider="native",
            provider_account_id=ACCOUNT,
            status="online",
            enabled=True,
            metadata_json=json.dumps({"access_token": "own"}),
        )
        live = register(record("tenant"))

        with connection_context(live.record, live):
            assert await is_inbound_dispatch_allowed("telegram", PLATFORM) is True
        assert live.closed is False

    async def test_a_failing_metadata_read_refuses_without_cutting(self, db) -> None:
        row = await add_instance(PLATFORM, account=ACCOUNT, token="own")
        await DBAdapterInstance.filter(id=row.id).update(metadata_json=json.dumps({"access_token": ["不是字符串"]}))
        live = register(record("tenant"))

        assert await allowed(PLATFORM, live.record, live) is False
        assert live.closed is False

    async def test_the_warning_is_rate_limited_per_instance(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own")
        warnings: List[str] = []

        class _Recorder:
            def __getattr__(self, _name: str) -> Any:
                return lambda *_a, **_k: None

            def warning(self, message: str, *_a: Any, **_k: Any) -> None:
                warnings.append(message)

        monkeypatch.setattr(unclaimed_dispatch, "logger", _Recorder())
        handshake = record("tenant", instance_key=PLATFORM)

        for _ in range(3):
            assert await allowed(PLATFORM, handshake) is False

        assert len(warnings) == 1
        assert PLATFORM in warnings[0] and "令牌" in warnings[0]
        # 与未认领那张限频表互不影响
        assert list(unclaimed_dispatch._token_mismatch_last_warned) == [(ONEBOT, PLATFORM)]  # noqa: SLF001
        assert unclaimed_dispatch._last_warned == {}  # noqa: SLF001

    async def test_an_unclaimed_instance_does_not_take_this_step(self, db) -> None:
        """未认领那一格由 #100 的判据管：握手对不上也不在这里断"""
        await add_instance(PLATFORM, account=ACCOUNT, token="own", status="unclaimed")
        live = register(record("tenant"))

        assert await allowed(PLATFORM, live.record, live) is False
        assert live.closed is False


# ---------------------------------------------------------------------------
# 触发点：绑定变了就复核
# ---------------------------------------------------------------------------


class TestTriggers:
    async def test_rebinding_an_account_cuts_the_old_connection(self, db) -> None:
        from nekro_agent.schemas.console import InstanceUpdateRequest

        platform = await add_instance(PLATFORM, token="own")
        live = register(record("tenant", instance_key=""))

        fresh = await DBAdapterInstance.get(id=platform.id)
        await instance_manager.update_instance(fresh, InstanceUpdateRequest(provider_account_id=ACCOUNT))
        await wait_for_pending_revalidation()

        assert live.closed is True

    async def test_reviving_a_tombstone_revalidates_too(self, db) -> None:
        await add_instance(PLATFORM, account=ACCOUNT, token="own", status="deleted")
        live = register(record("tenant"))

        await instance_manager.set_instance_enabled(await DBAdapterInstance.get(instance_key=PLATFORM), enabled=True)
        await wait_for_pending_revalidation()

        assert live.closed is True
