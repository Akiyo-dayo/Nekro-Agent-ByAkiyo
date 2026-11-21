"""容器侧 SSH 到上游的通道桥接测试

这条链路是真的：容器侧客户端连网关（127.0.0.1 随机端口），网关登录另一台真的
asyncssh 目标服务器，`exec` / pty shell / sftp / `-L` / `-R` 都走完整桥接。测试
只从"容器"这一侧观察，断言数据、退出码、审计字段与限流/让路行为。
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import asyncssh
import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.services.ssh_gateway import audit_queue, bandwidth, bridge, dbslot, events, keys, registry
from nekro_agent.services.ssh_gateway import authority as authority_module
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import supervisor as supervisor_module
from nekro_agent.services.ssh_gateway.authz import Coverage
from nekro_agent.services.ssh_gateway.lease import create_lease, end_lease, mark_running, reset_leases
from nekro_agent.services.ssh_gateway.secrets import encrypt_secret
from nekro_agent.tools import credential_cipher

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssh_gateway_e2e import (  # noqa: E402
    EchoContainerSession,
    EchoTCPServer,
    TargetConfig,
    TargetHarness,
)

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OWNER = "10001"


@dataclass
class SessionEnv:
    workspace: DBWorkspace
    row: DBWsSshCredential
    lease: Any
    client_private: str
    client_public: str


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
async def clean_state(db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "ssh-bridge-" + uuid.uuid4().hex)
    monkeypatch.setattr(config, "NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS", [])
    monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
    monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", [f"{ADAPTER}:{OWNER}"])
    monkeypatch.setattr(config, "SSH_GATEWAY_USER_ACCOUNTS", [f"{ADAPTER}:{OWNER}"])
    monkeypatch.setattr(config, "SSH_GATEWAY_LEASE_MAX_SECONDS", 600)
    monkeypatch.setattr(config, "SSH_GATEWAY_ALLOW_WITHOUT_ISOLATION", False)
    monkeypatch.setattr(config, "SSH_GATEWAY_PORT", 2222)
    monkeypatch.setattr(config, "SSH_GATEWAY_MAX_BYTES_PER_SEC", 0)
    monkeypatch.setattr(config, "SSH_GATEWAY_AUDIT_INPUT_BYTES", 65536)
    monkeypatch.setattr(config, "SSH_GATEWAY_UPSTREAM_IDLE_SECONDS", 60)
    monkeypatch.setattr(config, "SSH_GATEWAY_MAX_CONNECTIONS", 32)
    monkeypatch.setattr(config, "SSH_GATEWAY_MAX_CONNECTIONS_PER_WORKSPACE", 8)
    monkeypatch.setattr(config, "SSH_GATEWAY_MAX_CHANNELS", 10)
    monkeypatch.setattr(config, "SSH_GATEWAY_MAX_CHANNELS_TOTAL", 64)
    credential_cipher.reset_credential_cipher_cache()
    registry.reset_registry()
    reset_leases()
    authority_module.reset_authority_state()
    events.reset_credential_callbacks()
    bandwidth.reset_bandwidth()
    audit_queue.reset_audit_queue()
    dbslot.reset_db_slots()
    bridge.reset_bridges()
    bridge.set_yield_paused(False)
    supervisor_module.supervisor._unexpected_at.clear()

    async def _platform_allowed(_workspace: DBWorkspace) -> bool:
        return True

    monkeypatch.setattr(authority_module.authz, "platform_credentials_allowed", _platform_allowed)
    monkeypatch.setattr("nekro_agent.services.ssh_gateway.credentials.platform_credentials_allowed", _platform_allowed)

    try:
        yield
    finally:
        await audit_queue.stop(timeout=0.2)
        audit_queue.reset_audit_queue()
        registry.reset_registry()
        reset_leases()
        authority_module.reset_authority_state()
        events.reset_credential_callbacks()
        bandwidth.reset_bandwidth()
        bridge.reset_bridges()
        bridge.set_yield_paused(False)
        credential_cipher.reset_credential_cipher_cache()


@pytest.fixture
async def target(tmp_path: Path):
    harness = await TargetHarness(
        TargetConfig(password="pw-" + uuid.uuid4().hex, sftp_root=tmp_path / "sftp")
    ).start()
    try:
        yield harness
    finally:
        await harness.stop()


@pytest.fixture
async def gateway(clean_state):
    import importlib

    server_module = importlib.import_module("nekro_agent.services.ssh_gateway.server")
    supervisor_module.supervisor._backend = None
    backend = server_module.GatewayBackendImpl()
    supervisor_module.supervisor.register_backend(backend)
    await backend.start()
    await backend.sync_listeners(frozenset({"127.0.0.1"}), 0)
    try:
        yield backend
    finally:
        await backend.stop()
        supervisor_module.supervisor._backend = None
        supervisor_module.supervisor._yield_callbacks.clear()
        supervisor_module.supervisor._yield_requested = False


async def make_workspace(name: str = "ws-bridge") -> DBWorkspace:
    return await DBWorkspace.create(name=name, description=name, status="active")


async def make_row(
    workspace: DBWorkspace,
    target: TargetHarness,
    *,
    alias: str = "web1",
    status: str = C.STATUS_ACTIVE,
) -> DBWsSshCredential:
    return await DBWsSshCredential.create(
        workspace_id=workspace.id,
        owner_kind=C.OWNER_PLATFORM,
        owner_account="",
        adapter_key="",
        instance_key="",
        created_via=C.CREATED_VIA_ADMIN,
        import_ref="",
        alias=alias,
        host="127.0.0.1",
        port=target.port,
        username=target.config.username,
        password_enc=encrypt_secret(target.config.password),
        private_key_enc="",
        passphrase_enc="",
        host_keys=[],
        status=status,
        status_reason="",
        note="",
        created_by="tester",
        updated_by="tester",
        last_error="",
    )


async def prepare(
    target: TargetHarness,
    *,
    alias: str = "web1",
    status: str = C.STATUS_ACTIVE,
) -> SessionEnv:
    workspace = await make_workspace("ws-" + uuid.uuid4().hex[:12])
    row = await make_row(workspace, target, alias=alias, status=status)
    client_private, client_public = keys.generate_client_keypair()
    registry.register_peer(
        registry.ContainerPeer(
            workspace_id=workspace.id,
            peer_address="127.0.0.1",
            listen_address="127.0.0.1",
            client_public_key=client_public,
            isolation_ok=True,
            image_ok=True,
        )
    )
    lease = create_lease(
        workspace_id=workspace.id,
        task_ref="cc_delegate:bridge-test",
        origin="chat",
        coverage=Coverage(credential_ids=frozenset({int(row.id)}), triggered_by=(OWNER,)),
        chat_key=f"{ADAPTER}-{INSTANCE}-group-bridge",
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
    )
    assert lease is not None
    mark_running(lease.lease_id)
    return SessionEnv(
        workspace=workspace,
        row=row,
        lease=lease,
        client_private=client_private,
        client_public=client_public,
    )


async def connect(
    backend: Any,
    env: SessionEnv,
    *,
    alias: str = "web1",
    private_key: Optional[str] = None,
    **kwargs: Any,
) -> asyncssh.SSHClientConnection:
    private_text = env.client_private if private_key is None else private_key
    return await asyncssh.connect(
        "127.0.0.1",
        backend.bound_port,
        username=alias,
        client_keys=[asyncssh.import_private_key(private_text)],
        known_hosts=None,
        config=None,
        agent_path=None,
        agent_identities=None,
        encoding=None,
        login_timeout=10,
        **kwargs,
    )


async def close_conn(conn: asyncssh.SSHClientConnection) -> None:
    conn.close()
    try:
        await asyncio.wait_for(conn.wait_closed(), timeout=5.0)
    except Exception:  # noqa: BLE001 - 关测试连接慢不该让用例失败
        pass


async def wait_closed(conn: asyncssh.SSHClientConnection, timeout: float = 5.0) -> None:
    await asyncio.wait_for(conn.wait_closed(), timeout=timeout)


class TestExecAndShell:
    async def test_exec_stdout_stderr_exit_and_audit(
        self,
        gateway: Any,
        target: TargetHarness,
    ) -> None:
        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            proc = await conn.run("cat", input=b"top-secret", check=False)
            assert proc.stdout == b"top-secret"
            assert proc.stderr == b""
            assert proc.exit_status == 0

            failed = await conn.run("fail", check=False)
            assert failed.stderr == b"boom\n"
            assert failed.exit_status == 7
            assert gateway.counters()["connections"] == 1
        finally:
            await close_conn(conn)

        await audit_queue.flush()
        audits = list(await DBWsSshAudit.all().order_by("id"))
        channels = [item for item in audits if item.event == C.EVENT_CHANNEL]
        execs = [item for item in audits if item.event == C.EVENT_EXEC]
        closes = [item for item in audits if item.event == C.EVENT_CLOSE and item.channel_kind == "exec"]
        assert channels and execs and closes
        channel = channels[0]
        assert channel.workspace_id == env.workspace.id
        assert channel.credential_id == env.row.id
        assert channel.lease_id == env.lease.lease_id
        assert channel.task_ref == "cc_delegate:bridge-test"
        assert channel.conn_id
        assert channel.channel_kind == "exec"
        assert channel.detail == "cat"
        assert OWNER in (channel.triggered_by or [])
        assert any(item.detail == "cat" for item in execs)
        assert any(item.exit_status == 0 for item in closes)
        assert any(item.input_head == "top-secret" for item in closes)
        assert all(not item.input_head or "top-secret" not in item.input_head for item in audits if item.channel_kind != "exec")
        assert all(target.config.password not in (item.detail or "") for item in audits)
        assert all(target.config.password not in (item.reason or "") for item in audits)
        assert all(target.config.password not in (item.input_head or "") for item in audits)

    async def test_pty_shell_resize_break_and_signal(
        self,
        gateway: Any,
        target: TargetHarness,
    ) -> None:
        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            proc = await conn.create_process(
                request_pty=True,
                term_type="xterm",
                term_size=(80, 24),
            )
            assert await asyncio.wait_for(proc.stdout.readline(), timeout=5.0) == b"shell-ready\n"
            # pty 建立后 asyncssh 会补一个初始窗口事件，目标把 80x24 回显出来；这是真实行为，
            # 先消费掉，别把它当成 resize 的结果。
            first = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
            if first.startswith(b"size:"):
                assert first == b"size:80x24\n"
                first = b""
            proc.stdin.write(b"hello\n")
            await proc.stdin.drain()
            if first:
                assert first == b"hello\n"
            else:
                assert await asyncio.wait_for(proc.stdout.readline(), timeout=5.0) == b"hello\n"

            proc.change_terminal_size(100, 40)
            assert await asyncio.wait_for(proc.stdout.readline(), timeout=5.0) == b"size:100x40\n"

            proc.send_break(200)
            assert await asyncio.wait_for(proc.stdout.readline(), timeout=5.0) == b"break:200\n"

            proc.send_signal("TERM")
            assert await asyncio.wait_for(proc.stdout.readline(), timeout=5.0) == b"signal:TERM\n"
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        finally:
            await close_conn(conn)
        assert (80, 24, 0, 0) in target.config.term_sizes
        assert (100, 40, 0, 0) in target.config.term_sizes
        assert 200 in target.config.breaks
        assert "TERM" in target.config.signals


class TestSftpAndForwarding:
    async def test_sftp_roundtrip(self, gateway: Any, target: TargetHarness, tmp_path: Path) -> None:
        env = await prepare(target)
        payload = ("sftp-" + uuid.uuid4().hex).encode("utf-8") * 64
        local = tmp_path / "upload.bin"
        local.write_bytes(payload)
        downloaded = tmp_path / "download.bin"
        conn = await connect(gateway, env)
        try:
            async with conn.start_sftp_client() as sftp:
                await sftp.put(str(local), "/upload.bin")
                await sftp.get("/upload.bin", str(downloaded))
        finally:
            await close_conn(conn)
        assert downloaded.read_bytes() == payload

    async def test_direct_tcpip_connects_from_target_side(self, gateway: Any, target: TargetHarness) -> None:
        env = await prepare(target)
        echo = await EchoTCPServer().start()
        try:
            conn = await connect(gateway, env)
            try:
                reader, writer = await conn.open_connection("127.0.0.1", echo.port, encoding=None)
                writer.write(b"ping")
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(4), timeout=5.0) == b"ping"
                writer.close()
                assert target.config.direct_requests >= 1
            finally:
                await close_conn(conn)
        finally:
            await echo.stop()
        assert bytes(echo.received) == b"ping"
        await audit_queue.flush()
        kinds = {item.channel_kind for item in await DBWsSshAudit.filter(event=C.EVENT_CHANNEL)}
        assert "direct-tcpip" in kinds

    async def test_reverse_tcpip_goes_through_target_listener(self, gateway: Any, target: TargetHarness) -> None:
        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            listener = await conn.create_server(
                lambda _orig_host, _orig_port: EchoContainerSession(),
                "127.0.0.1",
                0,
            )
            try:
                remote_port = int(listener.get_port())
                assert remote_port > 0
                reader, writer = await asyncio.open_connection("127.0.0.1", remote_port)
                try:
                    writer.write(b"ping")
                    await writer.drain()
                    assert await asyncio.wait_for(reader.readexactly(9), timeout=5.0) == b"echo:ping"
                finally:
                    writer.close()
                    await writer.wait_closed()
                assert target.config.forward_requests >= 1
            finally:
                listener.close()
        finally:
            await close_conn(conn)
        await audit_queue.flush()
        kinds = {item.channel_kind for item in await DBWsSshAudit.filter(event=C.EVENT_CHANNEL)}
        assert "tcpip-forward" in kinds


class TestLimitsYieldAndBandwidth:
    async def test_channel_limit_rejects_second_session(self, gateway: Any, target: TargetHarness, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_MAX_CHANNELS", 1)
        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            first = await conn.create_process(request_pty=True)
            assert await asyncio.wait_for(first.stdout.readline(), timeout=5.0) == b"shell-ready\n"
            with pytest.raises(asyncssh.ChannelOpenError):
                await conn.create_process(request_pty=True)
            first.close()
            await asyncio.wait_for(first.wait(), timeout=5.0)
        finally:
            await close_conn(conn)

    async def test_yield_pauses_reading_until_released(self, gateway: Any, target: TargetHarness) -> None:
        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            proc = await conn.create_process(request_pty=True)
            assert await asyncio.wait_for(proc.stdout.readline(), timeout=5.0) == b"shell-ready\n"
            supervisor_module.supervisor._set_yield_requested(True)
            proc.stdin.write(b"held\n")
            await proc.stdin.drain()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(proc.stdout.read(1), timeout=0.4)
            supervisor_module.supervisor._set_yield_requested(False)
            assert await asyncio.wait_for(proc.stdout.readline(), timeout=5.0) == b"held\n"
        finally:
            supervisor_module.supervisor._set_yield_requested(False)
            await close_conn(conn)

    async def test_bandwidth_limit_delays_large_echo(
        self,
        gateway: Any,
        target: TargetHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_MAX_BYTES_PER_SEC", 64 * 1024)
        bandwidth.reset_bandwidth()
        env = await prepare(target)
        echo = await EchoTCPServer().start()
        payload = b"x" * (160 * 1024)
        try:
            conn = await connect(gateway, env)
            try:
                reader, writer = await conn.open_connection("127.0.0.1", echo.port, encoding=None)
                started = time.monotonic()
                writer.write(payload)
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(len(payload)), timeout=10.0) == payload
                elapsed = time.monotonic() - started
                writer.close()
                assert elapsed >= 0.8
            finally:
                await close_conn(conn)
        finally:
            await echo.stop()


class TestRejectionsAndIsolation:
    async def test_unknown_alias_is_rejected_and_audited(self, gateway: Any, target: TargetHarness) -> None:
        env = await prepare(target)
        with pytest.raises((asyncssh.PermissionDenied, asyncssh.ConnectionLost, OSError)):
            await connect(gateway, env, alias="not-registered")
        await audit_queue.flush()
        denied = list(await DBWsSshAudit.filter(event=C.EVENT_CONNECT, allowed=False))
        assert any(item.reason == C.REASON_UNKNOWN_ALIAS for item in denied)

    async def test_wrong_client_key_is_rejected(self, gateway: Any, target: TargetHarness) -> None:
        env = await prepare(target)
        other_private, _other_public = keys.generate_client_keypair()
        with pytest.raises((asyncssh.PermissionDenied, asyncssh.ConnectionLost, OSError)):
            await connect(gateway, env, private_key=other_private)
        await audit_queue.flush()
        denied = list(await DBWsSshAudit.filter(event=C.EVENT_CONNECT, allowed=False))
        assert any(item.reason == C.REASON_BAD_CLIENT_KEY for item in denied)

    async def test_no_lease_and_disabled_row_are_rejected(self, gateway: Any, target: TargetHarness) -> None:
        env = await prepare(target)
        end_lease(env.lease.lease_id, "test")
        with pytest.raises((asyncssh.PermissionDenied, asyncssh.ConnectionLost, OSError)):
            await connect(gateway, env)
        await audit_queue.flush()
        denied = list(await DBWsSshAudit.filter(event=C.EVENT_CONNECT, allowed=False))
        assert any(item.reason == C.REASON_NO_LEASE for item in denied)

        env2 = await prepare(target, alias="web2")
        env2.row.status = C.STATUS_DISABLED
        await env2.row.save(update_fields=["status"])
        with pytest.raises((asyncssh.PermissionDenied, asyncssh.ConnectionLost, OSError)):
            await connect(gateway, env2, alias="web2")

    async def test_gateway_switch_off_rejects(self, gateway: Any, target: TargetHarness, monkeypatch: pytest.MonkeyPatch) -> None:
        env = await prepare(target)
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        with pytest.raises((asyncssh.PermissionDenied, asyncssh.ConnectionLost, OSError)):
            await connect(gateway, env)
        await audit_queue.flush()
        denied = list(await DBWsSshAudit.filter(event=C.EVENT_CONNECT, allowed=False))
        assert any(item.reason == C.REASON_GATEWAY_OFF for item in denied)

    async def test_lease_end_disconnects_live_connection(self, gateway: Any, target: TargetHarness) -> None:
        env = await prepare(target)
        conn = await connect(gateway, env)
        assert not conn.is_closed()
        end_lease(env.lease.lease_id, "test")
        await wait_closed(conn)
        assert conn.is_closed()

    async def test_credential_change_disconnects_live_connection(self, gateway: Any, target: TargetHarness) -> None:
        env = await prepare(target)
        conn = await connect(gateway, env)
        assert not conn.is_closed()
        await events.notify_credential_changed(int(env.row.id))
        await wait_closed(conn)
        assert conn.is_closed()

    async def test_authorization_exception_is_isolated(
        self,
        gateway: Any,
        target: TargetHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env = await prepare(target)
        original = authority_module.authorize_connection

        async def _boom(**_kwargs: object) -> object:
            raise RuntimeError("injected authorization failure")

        monkeypatch.setattr(authority_module, "authorize_connection", _boom)
        with pytest.raises((asyncssh.PermissionDenied, asyncssh.ConnectionLost, OSError)):
            await connect(gateway, env)
        monkeypatch.setattr(authority_module, "authorize_connection", original)
        conn = await connect(gateway, env)
        try:
            proc = await conn.run("echo", check=False)
            assert proc.exit_status == 0
        finally:
            await close_conn(conn)
        assert supervisor_module.supervisor._unexpected_at


class TestSupervisorWiring:
    async def test_backend_binds_and_reports_counters(self, gateway: Any, target: TargetHarness) -> None:
        assert gateway.bound_port > 0
        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            assert gateway.counters()["connections"] == 1
            proc = await conn.run("echo", check=False)
            assert proc.exit_status == 0
        finally:
            await close_conn(conn)
        for _ in range(20):
            await asyncio.sleep(0.02)
            if gateway.counters()["connections"] == 0:
                break
        assert gateway.counters()["connections"] == 0
        status = supervisor_module.supervisor.status()
        assert "connections" in status
        assert "channels" in status


# ---------------------------------------------------------------------------
# 独立复核之后补的：名额、上游计数、转发拒绝、周期复核
# ---------------------------------------------------------------------------


def _server_of(gateway: Any) -> Any:
    servers = list(gateway._connections.values())  # noqa: SLF001
    assert len(servers) == 1
    return servers[0]


class TestRound125Hardening:
    async def test_sessions_take_a_slot_before_exec(
        self,
        gateway: Any,
        target: TargetHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """还没发 exec / shell 的会话也占名额：开一堆空会话绕不过上限"""
        monkeypatch.setattr(config, "SSH_GATEWAY_MAX_CHANNELS", 3)
        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            server = _server_of(gateway)
            sessions = [server.session_requested() for _ in range(3)]
            assert all(session is not None for session in sessions)
            assert server.session_requested() is None, "第 4 个空会话必须被拒"
            assert gateway.counters()["channels"] == 3

            sessions[0].connection_lost(None)
            assert gateway.counters()["channels"] == 2
            again = server.session_requested()
            assert again is not None
            for session in [*sessions[1:], again]:
                session.connection_lost(None)
            assert gateway.counters()["channels"] == 0
        finally:
            await close_conn(conn)

    async def test_refused_forward_does_not_drop_the_connection(self, gateway: Any, target: TargetHarness) -> None:
        env = await prepare(target)
        echo = await EchoTCPServer().start()
        closed_port = echo.port
        await echo.stop()
        conn = await connect(gateway, env)
        try:
            with pytest.raises(asyncssh.ChannelOpenError):
                await conn.open_connection("127.0.0.1", closed_port, encoding=None)
            proc = await conn.run("cat", input=b"still-alive", check=False)
            assert proc.stdout == b"still-alive"
        finally:
            await close_conn(conn)

    async def test_channels_are_counted_on_the_upstream_and_idle_upstream_closes(
        self,
        gateway: Any,
        target: TargetHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.ssh_gateway.upstream import pool

        monkeypatch.setattr(config, "SSH_GATEWAY_UPSTREAM_IDLE_SECONDS", 0.2)
        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            proc = await conn.create_process(request_pty=True)
            assert await asyncio.wait_for(proc.stdout.readline(), timeout=5.0) == b"shell-ready\n"
            assert pool.channels() == 1
            proc.close()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
            for _ in range(50):
                await asyncio.sleep(0.05)
                if pool.count() == 0:
                    break
            assert pool.channels() == 0
            assert pool.count() == 0, "没有通道在用的上游按空闲收掉"

            logins = target.config.auth_attempts
            proc = await conn.run("cat", input=b"again", check=False)
            assert proc.stdout == b"again"
            assert target.config.auth_attempts > logins, "上游收掉后下一个通道重新登录"
        finally:
            await close_conn(conn)

    async def test_periodic_recheck_disconnects_after_owner_is_removed(
        self,
        gateway: Any,
        target: TargetHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """名单改了不会发凭据变更通知：监督任务每一拍的复核要把已开的连接断掉"""
        env = await prepare(target)
        conn = await connect(gateway, env)
        proc = await conn.create_process(request_pty=True)
        assert await asyncio.wait_for(proc.stdout.readline(), timeout=5.0) == b"shell-ready\n"

        monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", [])
        await gateway.sweep()

        await wait_closed(conn)
        assert conn.is_closed()

    async def test_expired_lease_is_ended_by_the_sweep(
        self,
        gateway: Any,
        target: TargetHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.services.ssh_gateway import lease as lease_module

        env = await prepare(target)
        conn = await connect(gateway, env)
        now = lease_module._clock()  # noqa: SLF001
        monkeypatch.setattr(lease_module, "_clock", lambda: now + 10_000)

        await gateway.sweep()

        assert env.lease.state == "ended"
        assert env.lease.end_reason == "expired"
        await wait_closed(conn)

    async def test_data_before_the_upstream_is_ready_pauses_the_channel(self) -> None:
        """asyncssh 接受 exec 之后会自己恢复读：上游会话还没建好时数据一到就补一次暂停"""

        class _FakeChan:
            def __init__(self) -> None:
                self.paused = False
                self.pause_calls = 0

            def set_write_buffer_limits(self, *_args: Any) -> None:
                return None

            def pause_reading(self) -> None:
                self.paused = True
                self.pause_calls += 1

            def resume_reading(self) -> None:
                self.paused = False

            def close(self) -> None:
                return None

        context = bridge.BridgeContext(
            conn_id="c1",
            workspace_id=1,
            credential_id=1,
            owner_kind=C.OWNER_PLATFORM,
            owner_account="",
            adapter_key="",
            instance_key="",
            lease_id="l1",
            task_ref="t1",
        )
        channel_bridge = bridge.ChannelBridge(context, kind="exec", command="cat")
        side = bridge.ServerSide(channel_bridge)
        channel_bridge.attach_container(side)
        chan = _FakeChan()
        side.connection_made(chan)
        assert chan.paused is True

        chan.resume_reading()  # asyncssh 在接受 exec 之后做的事
        side.data_received(b"x" * 1024)

        assert chan.paused is True, "目标侧还没到齐，数据一到就要重新停读"
        channel_bridge.close()


class TestEnvRequestsAndWorkspaceChannelCap:
    async def test_env_flood_is_not_stored_and_only_language_vars_are_kept(
        self,
        gateway: Any,
        target: TargetHarness,
    ) -> None:
        """asyncssh 自己存 env 没有上限：几千个 env 请求不能落进任何地方，白名单里的照常转发"""
        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            flood = {f"X{i}": "v" * 200 for i in range(2000)}
            flood["LANG"] = "C.UTF-8"
            flood["LC_ALL"] = "C.UTF-8"
            proc = await conn.create_process("cat", env=flood)
            proc.stdin.write(b"still-alive\n")
            assert await asyncio.wait_for(proc.stdout.readline(), timeout=10.0) == b"still-alive\n"

            # 会话还开着的时候看：这时桥与会话都还在
            server = _server_of(gateway)
            sessions = [bridge.container for bridge in server._channels if bridge.container is not None]  # noqa: SLF001
            assert sessions, "找不到这条连接上的会话"
            for session in sessions:
                assert session._env == {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}  # noqa: SLF001
                assert not getattr(session.chan, "_env", {}), "asyncssh 自己的 env 表不能被写进去"
            proc.stdin.write_eof()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        finally:
            await close_conn(conn)

    async def test_env_count_and_size_caps(self, gateway: Any, target: TargetHarness) -> None:
        from nekro_agent.services.ssh_gateway import server as server_module

        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            server = _server_of(gateway)
            session = server.session_requested()
            assert session is not None
            accept = session._accept_env  # noqa: SLF001
            assert accept(b"LANG", b"C.UTF-8") is True
            assert accept(b"PATH", b"/tmp") is False
            assert accept(b"LANG", b"x" * (server_module.MAX_ENV_VALUE_BYTES + 1)) is False
            for index in range(server_module.MAX_ENV_VARS + 5):
                accept(f"LC_{chr(65 + index % 26)}{chr(65 + index // 26)}".encode(), b"v")
            assert len(session._env) == server_module.MAX_ENV_VARS  # noqa: SLF001
            assert session._env["LANG"] == "C.UTF-8"  # noqa: SLF001
            session.connection_lost(None)
        finally:
            await close_conn(conn)

    async def test_recheck_error_keeps_connections_and_only_explicit_denial_disconnects(
        self,
        gateway: Any,
        target: TargetHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """一次短暂的数据库错误不能把所有会话断掉：出错沿用上一次结论，明确不给才断"""
        from nekro_agent.services.ssh_gateway import authority as authority_module
        from nekro_agent.services.ssh_gateway import server as server_module

        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            server = _server_of(gateway)

            async def broken(*_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("db down")

            monkeypatch.setattr(authority_module, "_compute_recheck", broken)
            authority_module._recheck_cache.clear()  # noqa: SLF001
            await server.recheck_live()
            assert not conn.is_closed()
            assert await server._recheck() is True, "宽限期内新通道照常放行"  # noqa: SLF001
            server._last_recheck_ok_at -= server_module.RECHECK_ERROR_GRACE_SECONDS + 1  # noqa: SLF001
            assert await server._recheck() is False, "过了宽限期只拒新通道"  # noqa: SLF001
            await asyncio.sleep(0.2)
            assert not conn.is_closed(), "复核出错不能断开整条连接"

            async def denied(*_args: Any, **_kwargs: Any) -> Any:
                return C.REASON_NOT_COVERED

            monkeypatch.setattr(authority_module, "_compute_recheck", denied)
            authority_module._recheck_cache.clear()  # noqa: SLF001
            await server.recheck_live()
            await wait_closed(conn)
        finally:
            await close_conn(conn)

    async def test_channels_are_capped_per_workspace(
        self,
        gateway: Any,
        target: TargetHarness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """全局名额不能被一个工作区占满：同一工作区所有连接合计有上限"""
        monkeypatch.setattr(config, "SSH_GATEWAY_MAX_CHANNELS", 10)
        monkeypatch.setattr(config, "SSH_GATEWAY_MAX_CHANNELS_PER_WORKSPACE", 2)
        env = await prepare(target)
        conn = await connect(gateway, env)
        try:
            server = _server_of(gateway)
            sessions = [server.session_requested() for _ in range(2)]
            assert all(session is not None for session in sessions)
            assert server.session_requested() is None, "同一工作区第 3 个通道必须被拒"
            assert gateway.workspace_channels(env.workspace.id) == 2
            sessions[0].connection_lost(None)
            assert gateway.workspace_channels(env.workspace.id) == 1
            again = server.session_requested()
            assert again is not None
            for session in [sessions[1], again]:
                session.connection_lost(None)
            assert gateway.workspace_channels(env.workspace.id) == 0
        finally:
            await close_conn(conn)
