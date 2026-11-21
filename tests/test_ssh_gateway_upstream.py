"""上游登录、主机钥匙与复用池的端到端测试

目标服务器是真的 asyncssh 服务器（127.0.0.1 上随机端口），网关这边只调用
`open_upstream` / `probe_host_key` / 后台测试接口，不经过容器侧 SSH。这样能把
"本机 SSH 设置有没有被偷偷读"、"钉住的钥匙是不是唯一判据"、"认证失败有没有落到
行状态"这些事钉死，而不需要真的起一个容器。
"""

from __future__ import annotations

import asyncio
import socket
import sys
import uuid
from pathlib import Path
from typing import Any, List, Optional, Tuple

import asyncssh
import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.routers import ssh_gateway as ssh_router
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.ssh_gateway import SshHostKeyAcceptRequest
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.ssh_gateway import (
    audit_queue,
    bandwidth,
    bridge,
    dbslot,
    events,
    registry,
    row_state,
    upstream,
)
from nekro_agent.services.ssh_gateway import authority as authority_module
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway.authority import Decision, TargetSpec
from nekro_agent.services.ssh_gateway.credentials import ChatRegisteredEditForbiddenError
from nekro_agent.services.ssh_gateway.lease import reset_leases
from nekro_agent.services.ssh_gateway.secrets import decrypt_secret, encrypt_secret
from nekro_agent.services.ssh_gateway.validation import host_key_fingerprint
from nekro_agent.services.user.role import Role
from nekro_agent.tools import credential_cipher

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ssh_gateway_e2e import TargetConfig, TargetHarness, public_line  # noqa: E402

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OWNER = "10001"


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
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "ssh-upstream-" + uuid.uuid4().hex)
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
    upstream.reset_upstream_state()
    row_state.reset_pending_host_keys()
    bridge.reset_bridges()

    async def _platform_allowed(_workspace: DBWorkspace) -> bool:
        return True

    monkeypatch.setattr(authority_module.authz, "platform_credentials_allowed", _platform_allowed)
    monkeypatch.setattr("nekro_agent.services.ssh_gateway.credentials.platform_credentials_allowed", _platform_allowed)

    try:
        yield
    finally:
        upstream.pool.close_all()
        upstream.reset_upstream_state()
        row_state.reset_pending_host_keys()
        bridge.reset_bridges()
        await audit_queue.stop(timeout=0.2)
        audit_queue.reset_audit_queue()
        registry.reset_registry()
        reset_leases()
        authority_module.reset_authority_state()
        events.reset_credential_callbacks()
        bandwidth.reset_bandwidth()
        credential_cipher.reset_credential_cipher_cache()


@pytest.fixture
async def target(tmp_path: Path):
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    config_obj = TargetConfig(
        password="pw-" + uuid.uuid4().hex,
        client_public_key=public_line(client_key),
        sftp_root=tmp_path / "sftp",
    )
    harness = await TargetHarness(config_obj).start()
    try:
        yield harness, client_key
    finally:
        await harness.stop()


def platform_owner() -> DBUser:
    return DBUser(
        username="owner",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


async def make_workspace(name: str = "ws-upstream") -> DBWorkspace:
    return await DBWorkspace.create(name=name, description=name, status="active")


async def make_row(
    workspace: DBWorkspace,
    target: TargetHarness,
    *,
    alias: str = "web1",
    password: Optional[str] = None,
    private_key: str = "",
    passphrase: str = "",
    host_keys: Optional[List[str]] = None,
    status: str = C.STATUS_ACTIVE,
    created_via: str = C.CREATED_VIA_ADMIN,
) -> DBWsSshCredential:
    password = target.config.password if password is None else password
    return await DBWsSshCredential.create(
        workspace_id=workspace.id,
        owner_kind=C.OWNER_PLATFORM,
        owner_account="",
        adapter_key="",
        instance_key="",
        created_via=created_via,
        import_ref="",
        alias=alias,
        host="127.0.0.1",
        port=target.port,
        username=target.config.username,
        password_enc=encrypt_secret(password) if password else "",
        private_key_enc=encrypt_secret(private_key) if private_key else "",
        passphrase_enc=encrypt_secret(passphrase) if passphrase else "",
        host_keys=list(host_keys or []),
        status=status,
        status_reason="",
        note="",
        created_by="tester",
        updated_by="tester",
        last_error="",
    )


def spec_from_row(row: DBWsSshCredential) -> TargetSpec:
    password, _ = decrypt_secret(str(row.password_enc or ""))
    private_key, _ = decrypt_secret(str(row.private_key_enc or ""))
    passphrase, _ = decrypt_secret(str(row.passphrase_enc or ""))
    return TargetSpec(
        credential_id=int(row.id),
        workspace_id=int(row.workspace_id),
        alias=str(row.alias),
        owner_kind=str(row.owner_kind),
        host=str(row.host),
        connect_address=str(row.host),
        port=int(row.port),
        username=str(row.username),
        password=password,
        private_key=private_key,
        passphrase=passphrase,
        host_keys=[str(item) for item in (row.host_keys or [])],
    )


def decision_for(row: DBWsSshCredential) -> Decision:
    return Decision(
        ok=True,
        reason="",
        banner="",
        workspace_id=int(row.workspace_id),
        lease_id="lease-" + uuid.uuid4().hex,
        task_ref="cc_delegate:test",
        triggered_by=(OWNER,),
        target=spec_from_row(row),
    )


async def close_conn(conn: asyncssh.SSHClientConnection) -> None:
    conn.close()
    try:
        await asyncio.wait_for(conn.wait_closed(), timeout=5.0)
    except Exception:  # noqa: BLE001 - 关测试连接慢不该让用例失败
        pass


class TestUpstreamLogin:
    async def test_password_login_and_last_used(self, target: Tuple[TargetHarness, Any]) -> None:
        harness, _key = target
        workspace = await make_workspace()
        row = await make_row(workspace, harness)
        conn = await upstream.open_upstream(spec_from_row(row))
        try:
            assert not conn.is_closed()
        finally:
            await close_conn(conn)
        await row.refresh_from_db()
        assert row.last_used_at is not None
        assert harness.config.password_attempts >= 1

    async def test_private_key_with_passphrase(self, target: Tuple[TargetHarness, Any]) -> None:
        harness, key = target
        passphrase = "pp-" + uuid.uuid4().hex
        private_text = key.export_private_key("openssh", passphrase=passphrase).decode("ascii")
        workspace = await make_workspace()
        row = await make_row(
            workspace,
            harness,
            password="",
            private_key=private_text,
            passphrase=passphrase,
        )
        conn = await upstream.open_upstream(spec_from_row(row))
        try:
            assert not conn.is_closed()
        finally:
            await close_conn(conn)
        assert harness.config.public_key_attempts >= 1
        assert harness.config.password_attempts == 0

    async def test_keyboard_interactive_password(self, target: Tuple[TargetHarness, Any]) -> None:
        harness, _key = target
        harness.config.allow_password = False
        harness.config.kbdint_mode = "password"
        workspace = await make_workspace()
        row = await make_row(workspace, harness)
        conn = await upstream.open_upstream(spec_from_row(row))
        try:
            assert not conn.is_closed()
        finally:
            await close_conn(conn)
        assert harness.config.kbdint_attempts >= 1
        assert harness.config.password_attempts == 0

    async def test_mfa_is_rejected_without_marking_auth_failed(self, target: Tuple[TargetHarness, Any]) -> None:
        harness, _key = target
        harness.config.allow_password = False
        harness.config.kbdint_mode = "mfa"
        workspace = await make_workspace()
        row = await make_row(workspace, harness)
        with pytest.raises(upstream.UpstreamError) as exc_info:
            await upstream.open_upstream(spec_from_row(row))
        assert exc_info.value.reason == C.REASON_MFA_REQUIRED
        await row.refresh_from_db()
        assert row.status == C.STATUS_ACTIVE
        assert "二次验证" in row.last_error


class TestHostKeyPinning:
    async def test_only_pinned_key_is_accepted(self, target: Tuple[TargetHarness, Any]) -> None:
        harness, _key = target
        wrong = asyncssh.generate_private_key("ssh-ed25519")
        workspace = await make_workspace()
        row = await make_row(workspace, harness, host_keys=[public_line(wrong)])
        with pytest.raises(upstream.UpstreamError) as exc_info:
            await upstream.open_upstream(spec_from_row(row))
        assert exc_info.value.reason == C.REASON_HOST_KEY_MISMATCH
        assert harness.config.auth_attempts == 0
        await row.refresh_from_db()
        assert row.status == C.STATUS_ACTIVE
        assert row.host_keys == [public_line(wrong)]

    async def test_first_success_pins_presented_key(self, target: Tuple[TargetHarness, Any]) -> None:
        harness, _key = target
        workspace = await make_workspace()
        row = await make_row(workspace, harness, host_keys=[])
        conn = await upstream.open_upstream(spec_from_row(row))
        await close_conn(conn)
        await row.refresh_from_db()
        assert len(row.host_keys) == 1
        assert public_line(row.host_keys[0]) == harness.host_entry
        await audit_queue.flush()
        audits = list(await DBWsSshAudit.filter(event=C.EVENT_HOST_KEY))
        assert len(audits) == 1
        assert audits[0].allowed is True
        assert audits[0].actor == "gateway"

    async def test_probe_returns_presented_key_without_auth(self, target: Tuple[TargetHarness, Any]) -> None:
        harness, _key = target
        entry = await upstream.probe_host_key("127.0.0.1", harness.port)
        assert public_line(entry) == harness.host_entry
        assert harness.config.auth_attempts == 0

    async def test_local_ssh_settings_are_not_read(
        self,
        target: Tuple[TargetHarness, Any],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        harness, _key = target
        home = tmp_path / "home"
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir(parents=True)
        # 如果网关读了本机配置，这两份文件足以让连接失败或走错代理
        (ssh_dir / "config").write_text(
            "Host *\n"
            "    ProxyCommand C:/definitely/not/a/command %h %p\n"
            "    IdentityFile C:/definitely/not/a/key\n",
            encoding="utf-8",
        )
        (ssh_dir / "known_hosts").write_text("127.0.0.1 ssh-ed25519 AAAA-wrong\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        workspace = await make_workspace()
        row = await make_row(workspace, harness, host_keys=[])
        conn = await upstream.open_upstream(spec_from_row(row))
        await close_conn(conn)
        await row.refresh_from_db()
        assert len(row.host_keys) == 1


class TestUpstreamErrors:
    async def test_auth_failure_marks_row(self, target: Tuple[TargetHarness, Any]) -> None:
        harness, _key = target
        workspace = await make_workspace()
        row = await make_row(workspace, harness, password="wrong-" + uuid.uuid4().hex)
        with pytest.raises(upstream.UpstreamError) as exc_info:
            await upstream.open_upstream(spec_from_row(row))
        assert exc_info.value.reason == C.REASON_UPSTREAM_AUTH_FAILED
        await row.refresh_from_db()
        assert row.status == C.STATUS_AUTH_FAILED
        assert row.last_error
        await audit_queue.flush()
        audits = list(await DBWsSshAudit.filter(event=C.EVENT_CONNECT, allowed=False))
        assert any(a.reason == C.REASON_UPSTREAM_AUTH_FAILED for a in audits)

    async def test_unreachable_backoff_uses_injected_clock(
        self,
        target: Tuple[TargetHarness, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        harness, _key = target
        workspace = await make_workspace()
        row = await make_row(workspace, harness)
        calls: List[Tuple[str, int]] = []
        now = [1000.0]

        def _fake_connect(address: str, port: int) -> socket.socket:
            calls.append((address, int(port)))
            raise OSError("refused")

        monkeypatch.setattr(upstream, "_blocking_connect", _fake_connect)
        monkeypatch.setattr(upstream, "_clock", lambda: now[0])

        with pytest.raises(upstream.UpstreamError) as first:
            await upstream.open_upstream(spec_from_row(row))
        assert first.value.reason == C.REASON_UPSTREAM_UNREACHABLE
        assert len(calls) == 1

        with pytest.raises(upstream.UpstreamError) as second:
            await upstream.open_upstream(spec_from_row(row))
        assert second.value.reason == C.REASON_UPSTREAM_UNREACHABLE
        assert "秒后再试" in second.value.message
        assert len(calls) == 1
        await row.refresh_from_db()
        assert row.last_error

        now[0] += upstream.UNREACHABLE_BACKOFF_SECONDS + 1.0
        with pytest.raises(upstream.UpstreamError):
            await upstream.open_upstream(spec_from_row(row))
        assert len(calls) == 2


class TestUpstreamPool:
    async def test_reuses_up_to_channel_limit_then_opens_new(
        self,
        target: Tuple[TargetHarness, Any],
    ) -> None:
        harness, _key = target
        workspace = await make_workspace()
        row = await make_row(workspace, harness, host_keys=[])
        decision = decision_for(row)
        first = await upstream.pool.acquire(decision)
        second = await upstream.pool.acquire(decision)
        assert first is second
        for _ in range(upstream.MAX_CHANNELS_PER_UPSTREAM):
            first.channel_opened()
        third = await upstream.pool.acquire(decision)
        assert third is not first
        assert upstream.pool.count() == 2
        upstream.pool.close_all()
        assert upstream.pool.count() == 0

    async def test_idle_upstream_is_closed_after_last_channel(
        self,
        target: Tuple[TargetHarness, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        harness, _key = target
        monkeypatch.setattr(config, "SSH_GATEWAY_UPSTREAM_IDLE_SECONDS", 0)
        workspace = await make_workspace()
        row = await make_row(workspace, harness, host_keys=[])
        pooled = await upstream.pool.acquire(decision_for(row))
        pooled.channel_opened()
        pooled.channel_closed()
        for _ in range(20):
            await asyncio.sleep(0.02)
            if upstream.pool.count() == 0:
                break
        assert upstream.pool.count() == 0


class TestTestConnectionEndpoint:
    async def test_pins_and_reports_mismatch_then_accepts(
        self,
        target: Tuple[TargetHarness, Any],
    ) -> None:
        harness, _key = target
        workspace = await make_workspace()
        row = await make_row(workspace, harness, host_keys=[])
        user = platform_owner()

        result = await ssh_router.test_ssh_credential(workspace.id, row.id, _current_user=user)
        assert result.ok is True
        assert result.pinned is True
        assert result.fingerprint.startswith("SHA256:")
        await row.refresh_from_db()
        assert len(row.host_keys) == 1

        wrong = asyncssh.generate_private_key("ssh-ed25519")
        row.host_keys = [public_line(wrong)]
        await row.save(update_fields=["host_keys"])
        mismatch = await ssh_router.test_ssh_credential(workspace.id, row.id, _current_user=user)
        assert mismatch.ok is False
        assert mismatch.reason == C.REASON_HOST_KEY_MISMATCH
        assert public_line(mismatch.presented_entry) == harness.host_entry
        assert mismatch.presented_fingerprint == host_key_fingerprint(harness.host_entry)
        assert mismatch.host_keys and public_line(mismatch.host_keys[0].entry) == public_line(wrong)

        pending = row_state.get_pending_host_key(int(row.id))
        assert pending is not None
        entry, fingerprint = pending
        with pytest.raises(ValidationError):
            await ssh_router.accept_ssh_host_key(
                workspace.id,
                row.id,
                SshHostKeyAcceptRequest(entry=entry, fingerprint="SHA256:not-the-one"),
                _current_user=user,
            )
        accepted = await ssh_router.accept_ssh_host_key(
            workspace.id,
            row.id,
            SshHostKeyAcceptRequest(entry=entry, fingerprint=fingerprint),
            _current_user=user,
        )
        assert accepted.ok is True
        await row.refresh_from_db()
        assert public_line(row.host_keys[-1]) == harness.host_entry
        assert row_state.get_pending_host_key(int(row.id)) is None

        await audit_queue.flush()
        tests = list(await DBWsSshAudit.filter(event=C.EVENT_CRED_TEST))
        assert tests
        assert any(item.actor == user.username and item.allowed for item in tests)

    async def test_accept_requires_a_fresh_test(
        self,
        target: Tuple[TargetHarness, Any],
    ) -> None:
        harness, _key = target
        workspace = await make_workspace()
        row = await make_row(workspace, harness, host_keys=[])
        with pytest.raises(ValidationError):
            await ssh_router.accept_ssh_host_key(
                workspace.id,
                row.id,
                SshHostKeyAcceptRequest(entry=harness.host_entry, fingerprint=harness.host_entry),
                _current_user=platform_owner(),
            )

    async def test_chat_registered_row_cannot_accept_for_tenant(
        self,
        target: Tuple[TargetHarness, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        harness, _key = target
        workspace = await make_workspace()
        row = await make_row(workspace, harness, host_keys=[], created_via=C.CREATED_VIA_CHAT)
        row_state.remember_pending_host_key(int(row.id), harness.host_entry, "SHA256:test")

        async def _fake_visible(*_args: object, **_kwargs: object) -> Tuple[DBWsSshCredential, int]:
            return row, 1

        async def _fake_accessible(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(ssh_router.ssh_credential_service, "_load_visible", _fake_visible)
        monkeypatch.setattr(ssh_router, "assert_workspace_accessible", _fake_accessible)
        with pytest.raises(ChatRegisteredEditForbiddenError):
            await ssh_router.accept_ssh_host_key(
                workspace.id,
                row.id,
                SshHostKeyAcceptRequest(entry=harness.host_entry, fingerprint="SHA256:test"),
                _current_user=platform_owner(),
            )

    async def test_test_endpoint_works_with_gateway_switch_off(
        self,
        target: Tuple[TargetHarness, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        harness, _key = target
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        workspace = await make_workspace()
        row = await make_row(workspace, harness, host_keys=[])
        result = await ssh_router.test_ssh_credential(workspace.id, row.id, _current_user=platform_owner())
        assert result.ok is True
        assert result.fingerprint.startswith("SHA256:")
