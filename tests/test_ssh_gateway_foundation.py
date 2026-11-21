"""SSH 网关地基：登记表、钥匙、授权、审计队列、监督任务

## 这条测试钉的是什么

这一轮做的是不需要真网络的地基，但每一块都决定后面 E2 能不能安全地接上：

- 登记表必须两个地址都对才认，否则同组兄弟工作区可以互相冒用；
- 主机钥匙只生成一次，损坏时不能覆盖；
- 授权入口按固定顺序判完所有拒绝理由，横幅里不出现凭据、主机与端口；
- 认证失败爆破与通道复核缓存必须真的生效；
- 审计队列满了要能告诉调用方，写库失败要重试而不是丢，关停超时要报出丢了几行；
- 凭据变更通知要在写库之后发出，且一个订阅者出错不影响别的；
- 监督任务关着时不导入 `asyncssh`、打开后按次序 start / sync / stop，熔断后关掉再打开能恢复；
- 事件循环让路与带宽令牌桶返回的是"要不要等 / 等多久"，不是阻塞。

环境是内存 SQLite、临时数据目录、打桩的解析与后端；不起真网络，不导入 `asyncssh`。
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from cryptography.hazmat.primitives import serialization
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.routers import ssh_gateway as ssh_router
from nekro_agent.schemas.ssh_gateway import SshCredentialUpdate
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.ssh_gateway import audit_queue, bandwidth, dbslot, events, keys, registry
from nekro_agent.services.ssh_gateway import authority as authority_module
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import supervisor as supervisor_module
from nekro_agent.services.ssh_gateway.authz import Coverage
from nekro_agent.services.ssh_gateway.credentials import ssh_credential_service
from nekro_agent.services.ssh_gateway.destination import DestinationVerdict
from nekro_agent.services.ssh_gateway.lease import (
    create_lease,
    end_lease,
    mark_running,
    reset_leases,
)
from nekro_agent.services.ssh_gateway.secrets import encrypt_secret
from nekro_agent.services.ssh_gateway.suspend import suspend_platform_rows
from nekro_agent.services.user.role import Role
from nekro_agent.tools import credential_cipher

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OWNER = "10001"
PASSWORD = "pw-" + "foundation-8f3a"
NEW_PASSWORD = "pw-" + "foundation-1b7c"
KEY = "ssh-foundation-" + "key-A"
LISTEN = "192.0.2.10"
PEER = "192.0.2.11"
HOST = "192.0.2.20"
CONNECT_IP = "192.0.2.21"
SAFE_HOST = "ssh.example.test"


# ---------------------------------------------------------------------------
# 环境与夹具
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
async def clean_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY)
    monkeypatch.setattr(config, "NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS", [])
    monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
    monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", [f"{ADAPTER}:{OWNER}"])
    monkeypatch.setattr(config, "SSH_GATEWAY_USER_ACCOUNTS", [f"{ADAPTER}:{OWNER}"])
    monkeypatch.setattr(config, "SSH_GATEWAY_LEASE_MAX_SECONDS", 600)
    monkeypatch.setattr(config, "SSH_GATEWAY_ALLOW_WITHOUT_ISOLATION", False)
    monkeypatch.setattr(config, "SSH_GATEWAY_PORT", 2222)
    monkeypatch.setattr(config, "SSH_GATEWAY_MAX_BYTES_PER_SEC", 0)
    monkeypatch.setattr(config, "SSH_GATEWAY_AUDIT_INPUT_BYTES", 65536)
    monkeypatch.setattr(config, "SSH_GATEWAY_LOOP_LAG_MS", 200)
    credential_cipher.reset_credential_cipher_cache()
    registry.reset_registry()
    reset_leases()
    authority_module.reset_authority_state()
    events.reset_credential_callbacks()
    bandwidth.reset_bandwidth()
    audit_queue.reset_audit_queue()
    dbslot.reset_db_slots()

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
        credential_cipher.reset_credential_cipher_cache()


@pytest.fixture
def captured_logs():
    from loguru import logger

    messages: List[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="DEBUG")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


@pytest.fixture
def client_key() -> Tuple[str, str]:
    return keys.generate_client_keypair()


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


def tenant_admin() -> DBUser:
    return DBUser(
        username="tenant",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        tenant_id=41,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


async def make_workspace(name: str = "ws-foundation") -> DBWorkspace:
    return await DBWorkspace.create(name=name, description=name, status="active")


async def make_row(
    workspace: DBWorkspace,
    alias: str = "web1",
    *,
    owner_kind: str = C.OWNER_PLATFORM,
    owner_account: str = "",
    adapter_key: str = "",
    instance_key: str = "",
    host: str = HOST,
    status: str = C.STATUS_ACTIVE,
    password: str = PASSWORD,
    password_enc: Optional[str] = None,
) -> DBWsSshCredential:
    return await DBWsSshCredential.create(
        workspace_id=workspace.id,
        owner_kind=owner_kind,
        owner_account=owner_account,
        adapter_key=adapter_key,
        instance_key=instance_key,
        created_via=C.CREATED_VIA_ADMIN,
        import_ref="",
        alias=alias,
        host=host,
        port=22,
        username="root",
        password_enc=encrypt_secret(password) if password_enc is None else password_enc,
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


async def make_user_row(workspace: DBWorkspace, alias: str = "web1", **kwargs: Any) -> Any:
    # 用户行跟着实例走：复核要求实例行存在（实例删了还留着的行一律不能用）
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    if not await DBAdapterInstance.filter(adapter_key=ADAPTER, instance_key=INSTANCE).exists():
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            enabled=True,
            provider="remote_onebot",
        )
    return await make_row(
        workspace,
        alias,
        owner_kind=C.OWNER_USER,
        owner_account=OWNER,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        **kwargs,
    )


def make_peer(
    workspace_id: int,
    public_key: str,
    *,
    isolation_ok: bool = True,
    image_ok: bool = True,
    listen: str = LISTEN,
    peer: str = PEER,
) -> registry.ContainerPeer:
    return registry.ContainerPeer(
        workspace_id=workspace_id,
        peer_address=peer,
        listen_address=listen,
        client_public_key=public_key,
        isolation_ok=isolation_ok,
        image_ok=image_ok,
    )


def start_lease(workspace: DBWorkspace, row: DBWsSshCredential) -> Any:
    lease = create_lease(
        workspace_id=workspace.id,
        task_ref=f"cc_delegate:{ADAPTER}-{INSTANCE}-group_7",
        origin="chat",
        coverage=Coverage(credential_ids=frozenset({int(row.id)}), triggered_by=(OWNER,)),
        chat_key=f"{ADAPTER}-{INSTANCE}-group_7",
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
    )
    assert lease is not None
    mark_running(lease.lease_id)
    return lease


async def connect(
    public_key: str,
    *,
    alias: str = "web1",
    listen: str = LISTEN,
    peer: str = PEER,
) -> authority_module.Decision:
    return await authority_module.authorize_connection(
        listen_address=listen,
        peer_address=peer,
        client_key_line=public_key,
        alias=alias,
    )


async def connect_rows() -> List[DBWsSshAudit]:
    await audit_queue.flush()
    return list(await DBWsSshAudit.filter(event=C.EVENT_CONNECT).order_by("id"))


# ---------------------------------------------------------------------------
# 登记表
# ---------------------------------------------------------------------------


class TestRegistry:
    async def test_register_resolve_and_overwrite(self) -> None:
        peer = make_peer(7, "ssh-ed25519 AAAA-one")
        registry.register_peer(peer)
        assert registry.resolve_peer(LISTEN, PEER) is peer
        assert registry.peer_for_workspace(7) is peer

        # 同工作区重建：地址与钥匙全换，旧的那份必须立刻失效
        newer = make_peer(7, "ssh-ed25519 AAAA-two", listen="192.0.2.31", peer="192.0.2.32")
        registry.register_peer(newer)
        assert registry.resolve_peer(LISTEN, PEER) is None
        assert registry.resolve_peer("192.0.2.31", "192.0.2.32") is newer
        assert registry.peer_for_workspace(7) is newer

    async def test_both_addresses_must_match(self) -> None:
        registry.register_peer(make_peer(7, "ssh-ed25519 AAAA-one"))
        assert registry.resolve_peer(LISTEN, "192.0.2.99") is None
        assert registry.resolve_peer("192.0.2.91", PEER) is None
        assert registry.resolve_peer("", PEER) is None

    async def test_unregister_and_desired_addresses(self) -> None:
        registry.register_peer(make_peer(7, "ssh-ed25519 AAAA-one"))
        registry.add_listen_address("192.0.2.41")
        registry.add_listen_address("192.0.2.41")
        assert registry.desired_listen_addresses() == frozenset({LISTEN, "192.0.2.41"})

        registry.remove_listen_address("192.0.2.41")
        assert registry.desired_listen_addresses() == frozenset({LISTEN})

        registry.unregister_workspace(7)
        assert registry.peer_for_workspace(7) is None
        assert registry.desired_listen_addresses() == frozenset()

    async def test_same_peer_address_on_two_networks(self) -> None:
        first = make_peer(7, "ssh-ed25519 AAAA-one", listen="192.0.2.10", peer="192.0.2.11")
        second = make_peer(8, "ssh-ed25519 AAAA-two", listen="192.0.2.51", peer="192.0.2.11")
        registry.register_peer(first)
        registry.register_peer(second)
        assert registry.resolve_peer("192.0.2.10", "192.0.2.11") is first
        assert registry.resolve_peer("192.0.2.51", "192.0.2.11") is second


# ---------------------------------------------------------------------------
# 钥匙
# ---------------------------------------------------------------------------


class TestKeys:
    async def test_host_key_is_created_once_and_reused(self) -> None:
        path = keys.gateway_host_key_path()
        assert not path.exists()
        created = keys.load_or_create_host_key()
        assert created == path
        assert path.exists()
        first = path.read_bytes()

        assert keys.load_or_create_host_key() == path
        assert path.read_bytes() == first

        public = keys.gateway_host_public_key()
        parts = public.split()
        assert parts[0] == "ssh-ed25519"
        assert len(parts) == 2

        # 读回来的公钥必须就是文件里那把
        private = serialization.load_ssh_private_key(first, password=None)
        expected = private.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        ).decode("ascii")
        assert keys.public_key_line(public) == keys.public_key_line(expected)

    async def test_corrupt_host_key_is_not_overwritten(self) -> None:
        path = keys.gateway_host_key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a key")
        with pytest.raises(keys.HostKeyUnavailableError):
            keys.load_or_create_host_key()
        assert path.read_bytes() == b"this is not a key"

    async def test_client_keypair_is_readable_and_matches(self) -> None:
        private_text, public_text = keys.generate_client_keypair()
        assert private_text.startswith("-----BEGIN OPENSSH " + "PRIVATE KEY-----")
        loaded = serialization.load_ssh_private_key(private_text.encode("ascii"), password=None)
        expected = loaded.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        ).decode("ascii")
        assert keys.public_key_line(public_text) == keys.public_key_line(expected)
        assert keys.public_key_line(public_text + " me@example") == keys.public_key_line(public_text)


# ---------------------------------------------------------------------------
# 授权入口
# ---------------------------------------------------------------------------


class TestAuthorize:
    async def _valid_platform(
        self,
        client_key: Tuple[str, str],
        *,
        image_ok: bool = True,
        isolation_ok: bool = True,
    ) -> Tuple[DBWorkspace, DBWsSshCredential, Any]:
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = start_lease(workspace, row)
        registry.register_peer(
            make_peer(workspace.id, client_key[1], image_ok=image_ok, isolation_ok=isolation_ok),
        )
        return workspace, row, lease

    async def test_gateway_off_denies_and_is_audited(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        decision = await connect("ssh-ed25519 AAAA-any")
        assert decision.ok is False
        assert decision.reason == C.REASON_GATEWAY_OFF
        assert decision.banner
        rows = await connect_rows()
        assert len(rows) == 1
        assert rows[0].reason == C.REASON_GATEWAY_OFF
        assert rows[0].workspace_id == 0

    async def test_unknown_peer_has_no_banner_and_no_audit(self, db) -> None:  # noqa: ARG002
        decision = await connect("ssh-ed25519 AAAA-any", listen="192.0.2.91", peer="192.0.2.92")
        assert decision.ok is False
        assert decision.reason == C.REASON_UNKNOWN_PEER
        assert decision.banner == ""
        assert await connect_rows() == []

    async def test_isolation_off_and_dev_switch(
        self,
        db,
        client_key: Tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        await self._valid_platform(client_key, isolation_ok=False)
        denied = await connect(client_key[1])
        assert denied.reason == C.REASON_ISOLATION_OFF
        assert denied.banner

        monkeypatch.setattr(config, "SSH_GATEWAY_ALLOW_WITHOUT_ISOLATION", True)
        allowed = await connect(client_key[1])
        assert allowed.ok is True

    async def test_image_too_old(self, db, client_key: Tuple[str, str]) -> None:  # noqa: ARG002
        await self._valid_platform(client_key, image_ok=False)
        decision = await connect(client_key[1])
        assert decision.reason == C.REASON_IMAGE_TOO_OLD
        assert decision.banner

    async def test_bad_client_key(self, db, client_key: Tuple[str, str]) -> None:  # noqa: ARG002
        await self._valid_platform(client_key)
        decision = await connect("ssh-ed25519 AAAA-wrong")
        assert decision.reason == C.REASON_BAD_CLIENT_KEY
        assert decision.banner == ""
        rows = await connect_rows()
        assert rows[-1].reason == C.REASON_BAD_CLIENT_KEY
        assert rows[-1].allowed is False

    async def test_unknown_alias(self, db, client_key: Tuple[str, str]) -> None:  # noqa: ARG002
        await self._valid_platform(client_key)
        decision = await connect(client_key[1], alias="web2")
        assert decision.reason == C.REASON_UNKNOWN_ALIAS
        assert "CLAUDE.md" in decision.banner
        rows = await connect_rows()
        assert rows[-1].detail == "alias=web2"

    async def test_no_lease(self, db, client_key: Tuple[str, str]) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        await make_row(workspace)
        registry.register_peer(make_peer(workspace.id, client_key[1]))
        decision = await connect(client_key[1])
        assert decision.reason == C.REASON_NO_LEASE
        assert decision.banner

    async def test_recheck_row_inactive(self, db, client_key: Tuple[str, str]) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace, status=C.STATUS_DISABLED)
        start_lease(workspace, row)
        registry.register_peer(make_peer(workspace.id, client_key[1]))
        decision = await connect(client_key[1])
        assert decision.reason == C.REASON_ROW_INACTIVE

    async def test_recheck_owner_changed(
        self,
        db,
        client_key: Tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        await self._valid_platform(client_key)
        monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", [f"{ADAPTER}:20002"])
        decision = await connect(client_key[1])
        assert decision.reason == C.REASON_OWNER_CHANGED

    async def test_undecryptable(self, db, client_key: Tuple[str, str]) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        payload = base64.urlsafe_b64encode(b"x" * 40).decode("ascii").rstrip("=")
        row = await make_row(workspace, password_enc="nkenc1:" + payload)
        start_lease(workspace, row)
        registry.register_peer(make_peer(workspace.id, client_key[1]))
        decision = await connect(client_key[1])
        assert decision.reason == C.REASON_UNDECRYPTABLE

    async def test_user_target_blocked(
        self,
        db,
        client_key: Tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_user_row(workspace)
        start_lease(workspace, row)
        registry.register_peer(make_peer(workspace.id, client_key[1]))

        async def _blocked(_host: str) -> DestinationVerdict:
            return DestinationVerdict(ok=False, reason=C.REASON_TARGET_BLOCKED, addresses=["192.0.2.28"])

        monkeypatch.setattr(authority_module, "check_user_destination", _blocked)
        decision = await connect(client_key[1])
        assert decision.reason == C.REASON_TARGET_BLOCKED
        assert decision.banner

    async def test_success_platform_target(self, db, client_key: Tuple[str, str]) -> None:  # noqa: ARG002
        workspace, row, lease = await self._valid_platform(client_key)
        decision = await connect(client_key[1])
        assert decision.ok is True
        assert decision.reason == ""
        assert decision.banner == ""
        assert decision.workspace_id == workspace.id
        assert decision.lease_id == lease.lease_id
        assert decision.triggered_by == (OWNER,)
        assert decision.target is not None
        assert decision.target.credential_id == int(row.id)
        assert decision.target.alias == "web1"
        assert decision.target.connect_address == HOST
        assert decision.target.port == 22
        assert decision.target.username == "root"
        assert decision.target.password == PASSWORD
        assert decision.target.host_keys == []

        rows = await connect_rows()
        assert len(rows) == 1
        assert rows[0].allowed is True
        assert rows[0].credential_id == int(row.id)
        assert rows[0].task_ref == lease.task_ref
        assert rows[0].lease_id == lease.lease_id
        assert rows[0].triggered_by == [OWNER]
        assert rows[0].owner_kind == C.OWNER_PLATFORM

    async def test_success_user_connects_checked_ip(
        self,
        db,
        client_key: Tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_user_row(workspace, host=SAFE_HOST)
        start_lease(workspace, row)
        registry.register_peer(make_peer(workspace.id, client_key[1]))

        async def _ok(_host: str) -> DestinationVerdict:
            return DestinationVerdict(ok=True, reason="", addresses=[CONNECT_IP, "192.0.2.22"])

        monkeypatch.setattr(authority_module, "check_user_destination", _ok)
        decision = await connect(client_key[1])
        assert decision.ok is True
        assert decision.target is not None
        assert decision.target.host == SAFE_HOST
        assert decision.target.connect_address == CONNECT_IP
        rows = await connect_rows()
        assert rows[0].owner_kind == C.OWNER_USER
        assert rows[0].owner_account == OWNER

    async def test_banners_never_leak_host_port_or_password(self, db, client_key: Tuple[str, str]) -> None:  # noqa: ARG002
        # 把每个拒绝理由都走一遍（只取横幅，不关心具体判定细节）
        decisions = [
            authority_module.Decision(False, reason=reason, banner=authority_module.banner_for(reason))
            for reason in (
                C.REASON_GATEWAY_OFF,
                C.REASON_ISOLATION_OFF,
                C.REASON_IMAGE_TOO_OLD,
                C.REASON_UNKNOWN_ALIAS,
                C.REASON_NO_LEASE,
                C.REASON_NOT_COVERED,
                C.REASON_OWNER_CHANGED,
                C.REASON_WORKSPACE_NOT_PLATFORM,
                C.REASON_ROW_INACTIVE,
                C.REASON_UNDECRYPTABLE,
                C.REASON_TARGET_BLOCKED,
                C.REASON_LIMIT_EXCEEDED,
            )
        ]
        for decision in decisions:
            assert HOST not in decision.banner
            assert CONNECT_IP not in decision.banner
            assert PASSWORD not in decision.banner
            assert "22" not in decision.banner

    async def test_unknown_peer_does_not_touch_the_audit_queue(self, db) -> None:  # noqa: ARG002
        before = audit_queue.queued_rows()
        await connect("ssh-ed25519 AAAA-any", listen="192.0.2.91", peer="192.0.2.92")
        assert audit_queue.queued_rows() == before


# ---------------------------------------------------------------------------
# 失败爆破与通道复核缓存
# ---------------------------------------------------------------------------


class TestBruteForceAndCache:
    async def test_ten_failures_lock_for_ten_minutes(
        self,
        db,
        client_key: Tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        now = [100.0]
        monkeypatch.setattr(authority_module, "_clock", lambda: now[0])
        workspace = await make_workspace()
        await make_row(workspace)
        registry.register_peer(make_peer(workspace.id, client_key[1]))

        for _ in range(10):
            decision = await connect("ssh-ed25519 AAAA-wrong")
            assert decision.reason == C.REASON_BAD_CLIENT_KEY

        locked = await connect(client_key[1])
        assert locked.reason == C.REASON_LIMIT_EXCEEDED
        assert locked.banner

        # 十分钟内即使一切正确也一律拒
        now[0] += 599.0
        still_locked = await connect(client_key[1])
        assert still_locked.reason == C.REASON_LIMIT_EXCEEDED

        # 锁过期后重新判定；这里还没有租约，所以走到 no_lease 而不是 limit
        now[0] += 2.0
        unlocked = await connect(client_key[1])
        assert unlocked.reason == C.REASON_NO_LEASE

    async def test_recheck_cache_ttl_and_invalidate(
        self,
        db,
        client_key: Tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        now = [200.0]
        monkeypatch.setattr(authority_module, "_clock", lambda: now[0])
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = start_lease(workspace, row)
        registry.register_peer(make_peer(workspace.id, client_key[1]))

        assert await authority_module.recheck_channel(
            workspace_id=workspace.id,
            credential_id=int(row.id),
            lease_id=lease.lease_id,
        ) is None

        end_lease(lease.lease_id, "done")
        # 缓存还没过期：仍返回旧结果
        assert await authority_module.recheck_channel(
            workspace_id=workspace.id,
            credential_id=int(row.id),
            lease_id=lease.lease_id,
        ) is None

        now[0] += authority_module.CACHE_TTL_SECONDS + 1.0
        assert await authority_module.recheck_channel(
            workspace_id=workspace.id,
            credential_id=int(row.id),
            lease_id=lease.lease_id,
        ) == C.REASON_NO_LEASE

    async def test_credential_change_notification_clears_cache(
        self,
        db,
        client_key: Tuple[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        now = [300.0]
        monkeypatch.setattr(authority_module, "_clock", lambda: now[0])
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = start_lease(workspace, row)
        registry.register_peer(make_peer(workspace.id, client_key[1]))

        assert await authority_module.recheck_channel(
            workspace_id=workspace.id,
            credential_id=int(row.id),
            lease_id=lease.lease_id,
        ) is None
        end_lease(lease.lease_id, "done")

        await events.notify_credential_changed(int(row.id))
        assert await authority_module.recheck_channel(
            workspace_id=workspace.id,
            credential_id=int(row.id),
            lease_id=lease.lease_id,
        ) == C.REASON_NO_LEASE


# ---------------------------------------------------------------------------
# 审计队列
# ---------------------------------------------------------------------------


class TestAuditQueue:
    async def test_batch_write_and_flush(self, db) -> None:  # noqa: ARG002
        for index in range(3):
            assert audit_queue.enqueue(workspace_id=1, event=C.EVENT_CONNECT, allowed=True, detail=f"row-{index}")
        await audit_queue.start()
        await audit_queue.flush()
        rows = list(await DBWsSshAudit.filter(event=C.EVENT_CONNECT).order_by("id"))
        assert [row.detail for row in rows] == ["row-0", "row-1", "row-2"]
        assert audit_queue.queued_rows() == 0
        await audit_queue.stop(timeout=1.0)

    async def test_queue_full_returns_false(self, db) -> None:  # noqa: ARG002
        for index in range(audit_queue.MAX_ROWS):
            assert audit_queue.enqueue(workspace_id=1, event=C.EVENT_CONNECT, allowed=True, detail=f"row-{index}")
        assert audit_queue.is_saturated() is True
        assert audit_queue.enqueue(workspace_id=1, event=C.EVENT_CONNECT, allowed=True) is False
        # 不要把这一千行留给夹具的 stop 去写：这里只验证队列上限
        audit_queue.reset_audit_queue()

    async def test_write_failure_keeps_rows_for_retry(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        written: List[Dict[str, Any]] = []
        failing = [True]

        class _FakeAudit:
            @staticmethod
            async def create(**fields: Any) -> None:
                if failing[0]:
                    raise RuntimeError("db down")
                written.append(fields)

        monkeypatch.setattr(audit_queue, "DBWsSshAudit", _FakeAudit)
        assert audit_queue.enqueue(workspace_id=1, event=C.EVENT_CONNECT, allowed=True)
        assert audit_queue.enqueue(workspace_id=1, event=C.EVENT_CONNECT, allowed=True)

        await audit_queue.flush()
        assert audit_queue.queued_rows() == 2
        assert written == []

        failing[0] = False
        await audit_queue.flush()
        assert audit_queue.queued_rows() == 0
        assert len(written) == 2

    async def test_stop_timeout_reports_dropped_rows(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
        captured_logs: List[str],
    ) -> None:
        class _BlockingAudit:
            @staticmethod
            async def create(**fields: Any) -> None:
                del fields
                await asyncio.sleep(30)

        monkeypatch.setattr(audit_queue, "DBWsSshAudit", _BlockingAudit)
        for _ in range(3):
            assert audit_queue.enqueue(workspace_id=1, event=C.EVENT_CONNECT, allowed=True)
        await audit_queue.start()
        await asyncio.sleep(0.05)
        await audit_queue.stop(timeout=0.05)
        assert audit_queue.queued_rows() == 0
        assert any("丢弃 3 行" in message for message in captured_logs)

    async def test_input_and_detail_are_truncated(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        monkeypatch.setattr(config, "SSH_GATEWAY_AUDIT_INPUT_BYTES", 3)
        assert audit_queue.enqueue(
            workspace_id=1,
            event=C.EVENT_CONNECT,
            allowed=True,
            input_head="abcdef",
            detail="x" * 5000,
        )
        await audit_queue.flush()
        row = await DBWsSshAudit.filter(event=C.EVENT_CONNECT).first()
        assert row is not None
        assert row.input_head == "abc"
        assert len(row.detail) == audit_queue.DETAIL_LIMIT

        audit_queue.reset_audit_queue()
        monkeypatch.setattr(config, "SSH_GATEWAY_AUDIT_INPUT_BYTES", 0)
        assert audit_queue.enqueue(
            workspace_id=1,
            event=C.EVENT_CONNECT,
            allowed=True,
            input_head="anything",
        )
        await audit_queue.flush()
        row = await DBWsSshAudit.filter(event=C.EVENT_CONNECT).order_by("-id").first()
        assert row is not None
        assert row.input_head == ""


# ---------------------------------------------------------------------------
# 凭据变更通知
# ---------------------------------------------------------------------------


class TestCredentialEvents:
    async def test_update_delete_status_and_suspend_notify(
        self,
        db,  # noqa: ARG002
    ) -> None:
        seen: List[int] = []

        async def _record(credential_id: int) -> None:
            seen.append(credential_id)

        events.on_credential_changed(_record)
        owner = platform_owner()

        workspace = await make_workspace("ws-update")
        row = await make_row(workspace)
        await ssh_credential_service.update_credential(
            owner,
            workspace.id,
            int(row.id),
            SshCredentialUpdate(note="changed"),
        )
        assert seen[-1] == int(row.id)

        await ssh_credential_service.set_status(owner, workspace.id, int(row.id), enable=False)
        assert seen[-1] == int(row.id)
        await ssh_credential_service.set_status(owner, workspace.id, int(row.id), enable=True)
        assert seen[-1] == int(row.id)

        await ssh_credential_service.delete_credential(owner, workspace.id, int(row.id))
        assert seen[-1] == int(row.id)

        suspended_ws = await make_workspace("ws-suspend")
        suspended_row = await make_row(suspended_ws, alias="web2")
        changed = await suspend_platform_rows([suspended_ws.id], reason="测试挂起")
        assert changed == 1
        assert seen[-1] == int(suspended_row.id)

    async def test_one_callback_error_does_not_block_the_rest(
        self,
        db,  # noqa: ARG002
    ) -> None:
        seen: List[int] = []

        def _bad(_credential_id: int) -> None:
            raise RuntimeError("subscriber down")

        def _good(credential_id: int) -> None:
            seen.append(credential_id)

        events.on_credential_changed(_bad)
        events.on_credential_changed(_good)
        await events.notify_credential_changed(7)
        assert seen == [7]


# ---------------------------------------------------------------------------
# 监督任务
# ---------------------------------------------------------------------------


class FakeBackend:
    def __init__(self) -> None:
        self.calls: List[Tuple[Any, ...]] = []
        self.start_failures = 0

    async def start(self) -> None:
        self.calls.append(("start",))
        if self.start_failures > 0:
            self.start_failures -= 1
            raise OSError("port in use")

    async def sync_listeners(self, addresses: frozenset[str], port: int) -> None:
        self.calls.append(("sync", tuple(sorted(addresses)), port))

    async def stop(self) -> None:
        self.calls.append(("stop",))

    def counters(self) -> dict:
        return {"connections": 2, "channels": 3}


class TestSupervisor:
    async def test_off_on_off_sequence(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        supervisor = supervisor_module.Supervisor(tick_seconds=60.0)
        backend = FakeBackend()
        imports: List[str] = []

        def _import(name: str) -> object:
            imports.append(name)
            supervisor.register_backend(backend)
            return object()

        monkeypatch.setattr(supervisor_module.importlib, "import_module", _import)
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        await supervisor.tick()
        assert supervisor.status()["state"] == supervisor_module.GatewayState.OFF.value
        assert imports == []
        assert backend.calls == []

        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        registry.add_listen_address("192.0.2.10")
        await supervisor.tick()
        assert supervisor.status()["state"] == supervisor_module.GatewayState.RUNNING.value
        assert imports == [supervisor_module.SERVER_MODULE]
        assert backend.calls[:2] == [("start",), ("sync", ("192.0.2.10",), 2222)]

        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        await supervisor.tick()
        assert supervisor.status()["state"] == supervisor_module.GatewayState.OFF.value
        assert backend.calls[-1] == ("stop",)

    async def test_import_failure_then_retry(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        supervisor = supervisor_module.Supervisor(tick_seconds=60.0)
        backend = FakeBackend()

        def _broken(_name: str) -> object:
            raise ImportError("asyncssh missing")

        def _working(_name: str) -> object:
            supervisor.register_backend(backend)
            return object()

        monkeypatch.setattr(supervisor_module.importlib, "import_module", _broken)
        await supervisor.tick()
        status = supervisor.status()
        assert status["state"] == supervisor_module.GatewayState.UNAVAILABLE.value
        assert "加载失败" in status["reason"]

        monkeypatch.setattr(supervisor_module.importlib, "import_module", _working)
        await supervisor.tick()
        assert supervisor.status()["state"] == supervisor_module.GatewayState.RUNNING.value

    async def test_start_failure_then_retry(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        supervisor = supervisor_module.Supervisor(tick_seconds=60.0)
        backend = FakeBackend()
        backend.start_failures = 1

        def _import(_name: str) -> object:
            supervisor.register_backend(backend)
            return object()

        monkeypatch.setattr(supervisor_module.importlib, "import_module", _import)
        await supervisor.tick()
        assert supervisor.status()["state"] == supervisor_module.GatewayState.UNAVAILABLE.value
        assert "启动失败" in supervisor.status()["reason"]

        await supervisor.tick()
        assert supervisor.status()["state"] == supervisor_module.GatewayState.RUNNING.value
        assert [call[0] for call in backend.calls] == ["start", "start", "sync"]

    async def test_twenty_one_unexpected_trips_then_recovers(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        supervisor = supervisor_module.Supervisor(tick_seconds=60.0)
        backend = FakeBackend()

        def _import(_name: str) -> object:
            supervisor.register_backend(backend)
            return object()

        monkeypatch.setattr(supervisor_module.importlib, "import_module", _import)
        await supervisor.tick()
        assert supervisor.status()["state"] == supervisor_module.GatewayState.RUNNING.value

        for _ in range(21):
            supervisor.report_unexpected(RuntimeError("bridge failed"))
        assert supervisor.status()["state"] == supervisor_module.GatewayState.TRIPPED.value
        await asyncio.sleep(0.02)
        assert ("stop",) in backend.calls

        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        await supervisor.tick()
        assert supervisor.status()["state"] == supervisor_module.GatewayState.OFF.value

        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        await supervisor.tick()
        assert supervisor.status()["state"] == supervisor_module.GatewayState.RUNNING.value
        assert [call[0] for call in backend.calls].count("start") == 2

    async def test_loop_survives_exceptions(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        supervisor = supervisor_module.Supervisor(tick_seconds=0.01)
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        calls = [0]

        async def _flaky() -> None:
            calls[0] += 1
            if calls[0] < 3:
                raise RuntimeError("tick failed")

        monkeypatch.setattr(supervisor, "tick", _flaky)
        await supervisor.start()
        await asyncio.sleep(0.08)
        await supervisor.stop()
        assert calls[0] >= 3

    async def test_status_has_no_credentials(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        supervisor = supervisor_module.Supervisor(tick_seconds=60.0)
        backend = FakeBackend()

        def _import(_name: str) -> object:
            supervisor.register_backend(backend)
            return object()

        monkeypatch.setattr(supervisor_module.importlib, "import_module", _import)
        registry.add_listen_address("192.0.2.10")
        await supervisor.tick()
        status = supervisor.status()
        assert status == {
            "state": "running",
            "reason": "",
            "listen_addresses": ["192.0.2.10"],
            "port": 2222,
            "connections": 2,
            "channels": 3,
            "running_leases": 0,
        }
        assert not any("password" in key or "key" in key for key in status)

    async def test_switch_off_never_imports_asyncssh(self, db) -> None:  # noqa: ARG002
        # 不能在这里断言进程级 `sys.modules`：同一次 pytest 里别的用例会 import asyncssh，
        # 这个断言会随收集顺序变化。真正的「开关关着不加载」由下面的干净子进程验证。
        script = (
            "import asyncio, sys\n"
            "from nekro_agent.core.config import config\n"
            "from nekro_agent.services.ssh_gateway.supervisor import Supervisor\n"
            "config.SSH_GATEWAY_ENABLED = False\n"
            "async def main():\n"
            "    supervisor = Supervisor()\n"
            "    await supervisor.tick()\n"
            "    print('RESULT:', 'asyncssh' in sys.modules)\n"
            "    await supervisor.stop()\n"
            "asyncio.run(main())\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        assert "RESULT: False" in completed.stdout


# ---------------------------------------------------------------------------
# 事件循环让路与带宽
# ---------------------------------------------------------------------------


class TestYieldAndBandwidth:
    async def test_yield_requested_turns_on_and_off(
        self,
        db,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
        supervisor = supervisor_module.Supervisor(tick_seconds=60.0, lag_seconds=0.05)
        changes: List[bool] = []
        supervisor.on_yield_change(lambda value: changes.append(value))
        await supervisor.start()
        await asyncio.sleep(0.06)

        time.sleep(0.3)
        # 只让事件循环跑一次，够让让路任务量到这次延迟；再多等就会进入下一次正常采样
        await asyncio.sleep(0.001)
        assert supervisor.yield_requested() is True
        assert True in changes

        await asyncio.sleep(0.25)
        assert supervisor.yield_requested() is False
        assert changes[-1] is False
        await supervisor.stop()

    async def test_token_bucket_rate_and_unlimited(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = [500.0]
        monkeypatch.setattr(bandwidth, "_clock", lambda: now[0])
        monkeypatch.setattr(config, "SSH_GATEWAY_MAX_BYTES_PER_SEC", 1000)

        assert bandwidth.try_take("up", 1000) == 0.0
        assert bandwidth.try_take("up", 500) == pytest.approx(0.5)

        now[0] += 0.25
        assert bandwidth.try_take("up", 500) == pytest.approx(0.25)

        now[0] += 0.25
        assert bandwidth.try_take("up", 500) == 0.0
        # down 是另一个桶，不受 up 影响
        assert bandwidth.try_take("down", 1000) == 0.0

        # 配置改了下一次取用生效：新速率下桶是满的
        monkeypatch.setattr(config, "SSH_GATEWAY_MAX_BYTES_PER_SEC", 2000)
        assert bandwidth.try_take("up", 2000) == 0.0

        # 单块大于桶容量：桶满时放行，避免永久等不到足够令牌
        now[0] += 1.0
        assert bandwidth.try_take("up", 5000) == 0.0

        monkeypatch.setattr(config, "SSH_GATEWAY_MAX_BYTES_PER_SEC", 0)
        assert bandwidth.try_take("up", 999999) == 0.0


# ---------------------------------------------------------------------------
# 状态接口
# ---------------------------------------------------------------------------


class TestStatusRoute:
    async def test_platform_can_read_and_tenant_is_forbidden(self, db) -> None:  # noqa: ARG002
        response = await ssh_router.get_ssh_gateway_status(_current_user=platform_owner())
        payload = response.model_dump()
        assert set(payload) == {
            "state",
            "reason",
            "listen_addresses",
            "port",
            "connections",
            "channels",
            "running_leases",
        }
        assert "password" not in payload
        assert "private_key" not in payload
        assert "target" not in payload

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as denied:
            await ssh_router.get_ssh_gateway_status(_current_user=tenant_admin())
        assert denied.value.status_code == 403
