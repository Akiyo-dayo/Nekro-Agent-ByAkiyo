"""SSH 服务器凭据的存储、可见性与审计

## 这条测试钉的是什么

凭据只存 NA、三个 `_enc` 列是 `nkenc1:` 信封；平台方与租户看得到哪些行、租户对聊天
登记的凭据只能停用与删除；增改删、查看明文、启停都留审计；`host_keys` 能按 SSH 线格式
校验并算出与 `ssh-keygen -lf` 一致的指纹；启动重加密只碰该碰的行。

## 环境

密钥来源 monkeypatch 到临时目录；数据库是内存 SQLite；目标地址全部是文档地址或封禁
网段里的地址，网络解析用打桩，不碰真网络。秘密值一律现编，不是任何真实凭据。
"""

import asyncio
import base64
import datetime
import hashlib
import ipaddress
import socket
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException, Response
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.routers import ssh_gateway as ssh_router
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.ssh_gateway import SshCredentialCreate, SshCredentialUpdate
from nekro_agent.schemas.tenant import CREDENTIAL_MASK, TenantRole
from nekro_agent.services.sandbox.network_guard import BLOCKED_IPV4_CIDRS, BLOCKED_IPV6_CIDRS
from nekro_agent.services.ssh_gateway import audit_retention as retention_module
from nekro_agent.services.ssh_gateway import credentials as credentials_module
from nekro_agent.services.ssh_gateway.constants import (
    EVENT_CRED_CREATE,
    EVENT_CRED_DELETE,
    EVENT_CRED_REVEAL,
    EVENT_CRED_UPDATE,
    REASON_FORBIDDEN,
)
from nekro_agent.services.ssh_gateway.credentials import (
    ChatRegisteredEditForbiddenError,
    ChatRegisteredSecretForbiddenError,
    ssh_credential_service,
)
from nekro_agent.services.ssh_gateway.destination import check_user_destination
from nekro_agent.services.ssh_gateway.secrets import (
    SshSecretEncryptError,
    decrypt_secret,
    encrypt_secret,
    ssh_credentials_upgrade_at_rest,
)
from nekro_agent.services.ssh_gateway.validation import (
    host_key_fingerprint,
    parse_host_keys,
)
from nekro_agent.services.user.role import Role
from nekro_agent.tools import credential_cipher

TENANT_A = 41
TENANT_B = 42
KEY_A = "ssh-at-rest-" + "A-2c9"
KEY_B = "ssh-at-rest-" + "B-7d4"
KEY_C = "ssh-at-rest-" + "C-5e1"
PASSWORD = "pw-" + "sentinel-8f3a"
NEW_PASSWORD = "pw-" + "sentinel-1b7c"
PRIVATE_KEY = (
    "-----BEGIN OPENSSH " + "PRIVATE KEY-----\n"
    + "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQyNTUxOQ"
    + "\n-----END OPENSSH " + "PRIVATE KEY-----"
)
PASSPHRASE = "phrase-" + "sentinel-6d1"
# 目的地检查只放行真正的公网地址（文档网段 192.0.2.0/24 也不算）。这里只做字面量判断与
# 打桩的解析结果，测试不会真的去连这些地址
SAFE_IP = "9.9.9." + "44"
SAFE_IP_2 = "9.9.9." + "45"
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
def cipher_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_A)
    monkeypatch.setattr(OsEnv, "JWT_SECRET_KEY", "unrelated-jwt-" + "sentinel")
    monkeypatch.setattr(config, "SSH_GATEWAY_USER_ACCOUNTS", ["onebot_v11:user-a"])
    monkeypatch.setattr(config, "NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS", [])
    credential_cipher.reset_credential_cipher_cache()
    try:
        yield
    finally:
        credential_cipher.reset_credential_cipher_cache()


@pytest.fixture(autouse=True)
def allow_platform_credentials(monkeypatch: pytest.MonkeyPatch):
    async def _allowed(_workspace: DBWorkspace) -> bool:
        return True

    monkeypatch.setattr(credentials_module, "platform_credentials_allowed", _allowed)
    return _allowed


@pytest.fixture
def log_messages():
    from loguru import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="DEBUG")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def platform_owner() -> DBUser:
    return DBUser(
        username="owner",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


def tenant_admin(tenant_id: int, name: str) -> DBUser:
    return DBUser(
        username=name,
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


def tenant_reader(tenant_id: int, name: str) -> DBUser:
    return DBUser(
        username=name,
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.User,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_USER.value,
    )


async def make_instance(instance_key: str, tenant_id: Any) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key="onebot_v11",
        instance_key=instance_key,
        display_name=instance_key,
        status="online",
        provider="remote_onebot",
        tenant_id=tenant_id,
        metadata_json="",
    )


async def make_workspace(name: str, instance_key: str = "") -> DBWorkspace:
    workspace = await DBWorkspace.create(name=name, description=name)
    if instance_key:
        await DBChatChannel.create(
            adapter_key="onebot_v11",
            instance_key=instance_key,
            channel_id=f"group_{workspace.id}_{instance_key}",
            chat_key=f"onebot_v11-{instance_key}-group_{workspace.id}",
            channel_type="group",
            data="{}",
            workspace_id=workspace.id,
        )
    return workspace


def create_body(
    alias: str,
    *,
    owner_kind: str = "platform",
    owner_account: str = "",
    adapter_key: str = "",
    instance_key: str = "",
    host: str = SAFE_IP,
    password: str = PASSWORD,
    private_key: str = "",
    passphrase: str = "",
    host_keys: List[str] | None = None,
    username: str = "root",
) -> SshCredentialCreate:
    return SshCredentialCreate(
        owner_kind=owner_kind,
        owner_account=owner_account,
        adapter_key=adapter_key,
        instance_key=instance_key,
        alias=alias,
        host=host,
        port=22,
        username=username,
        password=password,
        private_key=private_key,
        passphrase=passphrase,
        host_keys=host_keys or [],
        note="",
    )


async def make_user_row(
    workspace: DBWorkspace,
    alias: str,
    *,
    instance_key: str,
    created_via: str = "admin",
    owner_account: str = "user-a",
) -> DBWsSshCredential:
    return await DBWsSshCredential.create(
        workspace_id=workspace.id,
        owner_kind="user",
        owner_account=owner_account,
        adapter_key="onebot_v11",
        instance_key=instance_key,
        created_via=created_via,
        alias=alias,
        host=SAFE_IP,
        port=22,
        username="root",
        password_enc=encrypt_secret(PASSWORD),
        note="",
        status="active",
        created_by="seed",
        updated_by="seed",
    )


async def make_platform_row(workspace: DBWorkspace, alias: str) -> DBWsSshCredential:
    return await DBWsSshCredential.create(
        workspace_id=workspace.id,
        owner_kind="platform",
        owner_account="",
        adapter_key="",
        instance_key="",
        created_via="admin",
        alias=alias,
        host=SAFE_IP,
        port=22,
        username="root",
        password_enc=encrypt_secret(PASSWORD),
        note="",
        status="active",
        created_by="seed",
        updated_by="seed",
    )


async def setup_tenants() -> Tuple[DBWorkspace, DBWorkspace]:
    await make_instance("inst-a", TENANT_A)
    await make_instance("inst-b", TENANT_B)
    ws_a = await make_workspace("ws-a", "inst-a")
    ws_b = await make_workspace("ws-b", "inst-b")
    return ws_a, ws_b


def _ssh_string(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def _ed25519_public_entry() -> Tuple[str, str]:
    """现编一把 ed25519 公钥，返回 (entry, 期望指纹)"""
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    blob = _ssh_string(b"ssh-ed25519") + _ssh_string(raw)
    encoded = base64.b64encode(blob).decode("ascii")
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return f"ssh-ed25519 {encoded}", expected


# ---------------------------------------------------------------------------
# 存储与加密
# ---------------------------------------------------------------------------


class TestEncryptionAtRest:
    async def test_create_writes_envelopes_and_service_can_read_plaintext(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-a", "inst-a")
        await make_instance("inst-a", TENANT_A)
        owner = platform_owner()
        entry, expected_fp = _ed25519_public_entry()
        created = await ssh_credential_service.create_credential(
            owner,
            ws.id,
            create_body(
                "web1",
                password=PASSWORD,
                private_key=PRIVATE_KEY,
                passphrase=PASSPHRASE,
                host_keys=[f"host.example {entry}"],
            ),
        )
        row = await DBWsSshCredential.get(id=created.id)
        for column in ("password_enc", "private_key_enc", "passphrase_enc"):
            stored = getattr(row, column)
            assert stored.startswith(credential_cipher.ENVELOPE_PREFIX)
            assert PASSWORD not in stored
            assert "PRIVATE KEY" not in stored
        assert row.created_via == "admin"
        assert row.created_by == "owner"
        secret = await ssh_credential_service.reveal_secret(owner, ws.id, created.id)
        assert secret.password == PASSWORD
        assert secret.private_key == PRIVATE_KEY
        assert secret.passphrase == PASSPHRASE
        assert created.host_keys[0].fingerprint == expected_fp
        assert created.host_keys[0].entry.startswith("ssh-ed25519 ")

    async def test_create_is_refused_without_a_usable_key(self, db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-a", "inst-a")
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(blocker / "nested"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        credential_cipher.reset_credential_cipher_cache()
        assert credential_cipher.credential_cipher_status().available is False

        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.create_credential(
                platform_owner(),
                ws.id,
                create_body("web1"),
            )
        assert "NEKRO_CREDENTIAL_SECRET_KEY" in str(exc_info.value)
        assert await DBWsSshCredential.all().count() == 0

    async def test_encrypt_secret_rejects_empty_key_source(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(blocker / "nested"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        credential_cipher.reset_credential_cipher_cache()
        assert encrypt_secret("") == ""
        with pytest.raises(SshSecretEncryptError):
            encrypt_secret(PASSWORD)

    async def test_decrypt_secret_reports_source(self) -> None:
        stored = encrypt_secret(PASSWORD)
        plain, source = decrypt_secret(stored)
        assert plain == PASSWORD
        assert source == "primary"
        assert decrypt_secret("") == ("", "empty")

    async def test_upgrade_rewrites_plaintext_and_historical_rows_only(self, db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log_messages: List[str]) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-a", "inst-a")

        plain_row = await DBWsSshCredential.create(
            workspace_id=ws.id,
            owner_kind="platform",
            created_via="admin",
            alias="plain",
            host=SAFE_IP,
            username="root",
            password_enc=PASSWORD,
            status="active",
        )

        # 历史密钥：先用数据目录里的文件密钥写一份信封，再把环境变量主密钥换成 A；
        # 数据目录保持同一个，那把文件密钥就降级成"解历史密文"用的 fallback
        old_dir = tmp_path / "old-key"
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(old_dir))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        credential_cipher.reset_credential_cipher_cache()
        old_envelope = encrypt_secret(PASSWORD + "-old")
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_A)
        credential_cipher.reset_credential_cipher_cache()
        fallback_row = await DBWsSshCredential.create(
            workspace_id=ws.id,
            owner_kind="platform",
            created_via="admin",
            alias="fallback",
            host=SAFE_IP,
            username="root",
            password_enc=old_envelope,
            status="active",
        )

        # 解不开：用一把既不是主密钥、也不在钥匙串里的密钥写一封
        third_dir = tmp_path / "third-key"
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(third_dir))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_C)
        credential_cipher.reset_credential_cipher_cache()
        broken_envelope = encrypt_secret(PASSWORD + "-broken")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(old_dir))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_A)
        credential_cipher.reset_credential_cipher_cache()
        broken_row = await DBWsSshCredential.create(
            workspace_id=ws.id,
            owner_kind="platform",
            created_via="admin",
            alias="broken",
            host=SAFE_IP,
            username="root",
            password_enc=broken_envelope,
            status="active",
        )

        before = {
            row.id: row.update_time
            for row in await DBWsSshCredential.all()
        }

        rewritten, unreadable = await ssh_credentials_upgrade_at_rest()
        assert rewritten == [plain_row.id, fallback_row.id]
        assert unreadable == [broken_row.id]

        fresh_plain = await DBWsSshCredential.get(id=plain_row.id)
        assert fresh_plain.password_enc.startswith(credential_cipher.ENVELOPE_PREFIX)
        assert decrypt_secret(fresh_plain.password_enc)[0] == PASSWORD
        fresh_fallback = await DBWsSshCredential.get(id=fallback_row.id)
        assert decrypt_secret(fresh_fallback.password_enc) == (PASSWORD + "-old", "primary")
        fresh_broken = await DBWsSshCredential.get(id=broken_row.id)
        assert fresh_broken.password_enc == broken_envelope
        for row_id, update_time in before.items():
            assert (await DBWsSshCredential.get(id=row_id)).update_time == update_time

        assert any(str(broken_row.id) in message and "broken" in message for message in log_messages)

        rewritten_again, unreadable_again = await ssh_credentials_upgrade_at_rest()
        assert rewritten_again == []
        assert unreadable_again == [broken_row.id]

    async def test_upgrade_does_not_write_when_cipher_is_unavailable(self, db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-a", "inst-a")
        row = await DBWsSshCredential.create(
            workspace_id=ws.id,
            owner_kind="platform",
            created_via="admin",
            alias="plain",
            host=SAFE_IP,
            username="root",
            password_enc=PASSWORD,
            status="active",
        )
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(blocker / "nested"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        credential_cipher.reset_credential_cipher_cache()
        rewritten, unreadable = await ssh_credentials_upgrade_at_rest()
        assert rewritten == []
        assert unreadable == []
        assert (await DBWsSshCredential.get(id=row.id)).password_enc == PASSWORD


# ---------------------------------------------------------------------------
# 校验、主机公钥与目的地
# ---------------------------------------------------------------------------


class TestValidationAndHostKeys:
    async def test_alias_rules_and_uniqueness(self, db) -> None:  # noqa: ARG002
        ws_a = await make_workspace("ws-a", "inst-a")
        ws_b = await make_workspace("ws-b", "inst-b")
        await make_instance("inst-a", TENANT_A)
        owner = platform_owner()

        for bad in ("Web_1", "nk-web", "-web"):
            with pytest.raises(ValidationError):
                await ssh_credential_service.create_credential(owner, ws_a.id, create_body(bad))

        await ssh_credential_service.create_credential(owner, ws_a.id, create_body("web1"))
        with pytest.raises(ValidationError):
            await ssh_credential_service.create_credential(owner, ws_a.id, create_body("web1"))
        # 不同工作区可以同名
        await ssh_credential_service.create_credential(owner, ws_b.id, create_body("web1"))

    async def test_host_keys_accept_three_shapes_and_compute_fingerprint(self) -> None:
        entry, expected = _ed25519_public_entry()
        parsed = parse_host_keys(
            "\n".join(
                [
                    "# 注释",
                    "",
                    f"host.example {entry}",
                    entry,
                    f"host.example {entry}",
                ],
            ),
        )
        assert parsed == [entry]
        assert host_key_fingerprint(entry) == expected
        assert host_key_fingerprint(f"host.example {entry}") == expected

    async def test_host_keys_name_the_offending_line(self) -> None:
        entry, _ = _ed25519_public_entry()
        encoded = entry.split(" ", 1)[1]
        cases = [
            f"ssh-rsa {encoded}",
            f"ssh-ed25519 {encoded[:-4]}!!!!",
            f"ssh-dss {encoded}",
        ]
        for line in cases:
            with pytest.raises(ValidationError) as exc_info:
                parse_host_keys(f"# 头\nhost.example {entry}\n{line}")
            assert "第 3 行" in str(exc_info.value)

    async def test_check_user_destination_blocks_private_ranges(self) -> None:
        for host in (
            "10.0.0.5",
            "127.0.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "::1",
            "fc00::1",
            "::ffff:192.168.1.20",
            # 不在封禁列表里、但同样不是公网的：未指定、IPv4 兼容写法、NAT64 / 6to4 / Teredo
            # 嵌着内网 IPv4、文档网段、保留段、组播
            "::",
            "0.0.0.0",
            "::7f00:1",
            "64:ff9b::7f00:1",
            "64:ff9b::a00:5",
            "2002:7f00:1::1",
            "2001:0:4136:e378:8000:63bf:3fff:fdd2",
            # Python 3.11 的 is_global 放行的两段：站点本地、SIIT 的 IPv4 转换地址
            "fec0::1",
            "::ffff:0:a00:5",
            "192.0.2.10",
            "240.0.0." + "1",
            "224.0.0." + "1",
            "ff02::1",
        ):
            verdict = await check_user_destination(host)
            assert verdict.ok is False, host
            assert verdict.reason == "target_blocked", host

    async def test_check_user_destination_allows_nat64_of_public_address(self) -> None:
        verdict = await check_user_destination("64:ff9b::909:92c")
        assert verdict.ok is True

    async def test_check_user_destination_blocks_unparseable_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def odd_getaddrinfo(self, host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-address", 0))]

        monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", odd_getaddrinfo)
        verdict = await check_user_destination("odd.example.test")
        assert verdict.ok is False
        assert verdict.reason == "target_blocked"

    async def test_check_user_destination_allows_public_address(self) -> None:
        for blocked in BLOCKED_IPV4_CIDRS:
            assert ipaddress.ip_address(SAFE_IP) not in ipaddress.ip_network(blocked)
        for blocked in BLOCKED_IPV6_CIDRS:
            assert ipaddress.ip_address(SAFE_IP) not in ipaddress.ip_network(blocked)
        verdict = await check_user_destination(SAFE_IP)
        assert verdict.ok is True
        assert verdict.addresses == [SAFE_IP]

    async def test_check_user_destination_blocks_when_any_resolved_address_is_private(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: List[Tuple[str, Any]] = []

        async def fake_getaddrinfo(self, host, port, *args, **kwargs):
            calls.append((host, port))
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (SAFE_IP_2, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.99", 0)),
            ]

        monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)
        verdict = await check_user_destination("mixed.example.test")
        assert verdict.ok is False
        assert verdict.reason == "target_blocked"
        assert calls and calls[0][1] is None

    async def test_check_user_destination_allows_resolved_public_address(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_getaddrinfo_address(self, host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (SAFE_IP, 0))]

        monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake_getaddrinfo_address)
        verdict = await check_user_destination("safe.example.test")
        assert verdict.ok is True
        assert verdict.addresses == [SAFE_IP]

    async def test_check_user_destination_reports_unresolvable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_getaddrinfo(self, host, port, *args, **kwargs):
            raise socket.gaierror("not found")

        monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)
        verdict = await check_user_destination("missing.example.test")
        assert verdict.ok is False
        assert verdict.reason == "解析不了这个主机名"

    async def test_check_user_destination_honours_extra_blocked_cidrs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config, "NETWORK_ISOLATION_EXTRA_BLOCKED_CIDRS", ["9.9.8." + "0/24"])
        assert (await check_user_destination(SAFE_IP)).ok is True
        verdict = await check_user_destination("9.9.8." + "9")
        assert verdict.ok is False
        assert verdict.reason == "target_blocked"

    async def test_user_row_creation_runs_destination_check(self, db) -> None:  # noqa: ARG002
        ws_a, _ws_b = await setup_tenants()
        admin = tenant_admin(TENANT_A, "admin-a")
        body = create_body(
            "web1",
            owner_kind="user",
            owner_account="user-a",
            adapter_key="onebot_v11",
            instance_key="inst-a",
            host="10.0.0.5",
        )
        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.create_credential(admin, ws_a.id, body)
        assert "不允许的地址" in str(exc_info.value)
        # 不回显解析出的地址：后台的「新建」不能变成探内网 DNS 的工具
        assert "10.0.0.5" not in str(exc_info.value)

    @pytest.mark.parametrize(
        "host",
        [
            "10.0.0.5",
            "127.0.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "::1",
            "fc00::1",
            "::ffff:192.168.1.20",
        ],
    )
    async def test_user_row_creation_rejects_every_blocked_target(self, db, host: str) -> None:  # noqa: ARG002
        ws_a, _ws_b = await setup_tenants()
        admin = tenant_admin(TENANT_A, "admin-a")
        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.create_credential(
                admin,
                ws_a.id,
                create_body(
                    "web1",
                    owner_kind="user",
                    owner_account="user-a",
                    adapter_key="onebot_v11",
                    instance_key="inst-a",
                    host=host,
                ),
            )
        assert "不允许的地址" in str(exc_info.value)
        assert await DBWsSshCredential.all().count() == 0

    async def test_user_row_creation_checks_resolved_addresses(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        ws_a, _ws_b = await setup_tenants()
        admin = tenant_admin(TENANT_A, "admin-a")

        async def private_getaddrinfo(self, host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.98", 0))]

        monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", private_getaddrinfo)
        with pytest.raises(ValidationError):
            await ssh_credential_service.create_credential(
                admin,
                ws_a.id,
                create_body(
                    "hostname-blocked",
                    owner_kind="user",
                    owner_account="user-a",
                    adapter_key="onebot_v11",
                    instance_key="inst-a",
                    host="private.example.test",
                ),
            )

        async def safe_getaddrinfo(self, host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (SAFE_IP, 0))]

        monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "getaddrinfo", safe_getaddrinfo)
        created = await ssh_credential_service.create_credential(
            admin,
            ws_a.id,
            create_body(
                "hostname-safe",
                owner_kind="user",
                owner_account="user-a",
                adapter_key="onebot_v11",
                instance_key="inst-a",
                host="safe.example.test",
            ),
        )
        assert created.host == "safe.example.test"


# ---------------------------------------------------------------------------
# 可见性与聊天登记的行
# ---------------------------------------------------------------------------


class TestVisibility:
    async def _seed(self) -> Dict[str, Any]:
        ws_a, ws_b = await setup_tenants()
        platform = platform_owner()
        admin_a = tenant_admin(TENANT_A, "admin-a")
        admin_b = tenant_admin(TENANT_B, "admin-b")

        platform_row = await ssh_credential_service.create_credential(
            platform,
            ws_a.id,
            create_body("platform-box"),
        )
        admin_row = await ssh_credential_service.create_credential(
            admin_a,
            ws_a.id,
            create_body(
                "admin-box",
                owner_kind="user",
                owner_account="user-a",
                adapter_key="onebot_v11",
                instance_key="inst-a",
            ),
        )
        chat_row = await make_user_row(ws_a, "chat-box", instance_key="inst-a", created_via="chat")
        b_row = await ssh_credential_service.create_credential(
            admin_b,
            ws_b.id,
            create_body(
                "b-box",
                owner_kind="user",
                owner_account="user-b",
                adapter_key="onebot_v11",
                instance_key="inst-b",
            ),
        )
        return {
            "ws_a": ws_a,
            "ws_b": ws_b,
            "platform": platform,
            "admin_a": admin_a,
            "admin_b": admin_b,
            "platform_row": platform_row,
            "admin_row": admin_row,
            "chat_row": chat_row,
            "b_row": b_row,
        }

    async def test_list_and_detail_follow_the_matrix(self, db) -> None:  # noqa: ARG002
        seeded = await self._seed()
        ws_a, ws_b = seeded["ws_a"], seeded["ws_b"]

        platform_items = await ssh_credential_service.list_credentials(seeded["platform"], ws_a.id)
        assert {item.alias for item in platform_items} == {"platform-box", "admin-box", "chat-box"}

        tenant_items = await ssh_credential_service.list_credentials(seeded["admin_a"], ws_a.id)
        assert {item.alias for item in tenant_items} == {"admin-box", "chat-box"}
        assert all(item.owner_kind == "user" for item in tenant_items)

        with pytest.raises(NotFoundError):
            await ssh_credential_service.list_credentials(seeded["admin_b"], ws_a.id)
        with pytest.raises(NotFoundError):
            await ssh_credential_service.list_credentials(seeded["admin_a"], ws_b.id)

        detail = await ssh_credential_service.get_credential(seeded["admin_a"], ws_a.id, seeded["admin_row"].id)
        assert detail.password == CREDENTIAL_MASK
        assert detail.can_reveal is True
        chat_detail = await ssh_credential_service.get_credential(seeded["admin_a"], ws_a.id, seeded["chat_row"].id)
        assert chat_detail.password == CREDENTIAL_MASK
        assert chat_detail.can_reveal is False
        assert chat_detail.can_edit_connection is False

    async def test_invisible_row_uses_the_same_not_found_text(self, db) -> None:  # noqa: ARG002
        seeded = await self._seed()
        ws_a = seeded["ws_a"]
        admin_a = seeded["admin_a"]

        with pytest.raises(NotFoundError) as invisible:
            await ssh_credential_service.get_credential(admin_a, ws_a.id, seeded["platform_row"].id)
        with pytest.raises(NotFoundError) as wrong_workspace:
            await ssh_credential_service.get_credential(admin_a, ws_a.id, seeded["b_row"].id)
        with pytest.raises(NotFoundError) as missing:
            await ssh_credential_service.get_credential(admin_a, ws_a.id, 999_001)

        assert str(invisible.value) == str(missing.value) == str(wrong_workspace.value)

    async def test_platform_global_list_includes_workspace_name_and_rejects_tenant(self, db) -> None:  # noqa: ARG002
        seeded = await self._seed()
        items = await ssh_credential_service.list_all_credentials(seeded["platform"])
        assert {item.alias for item in items} == {"platform-box", "admin-box", "chat-box", "b-box"}
        assert all(item.workspace_name for item in items)

        with pytest.raises(PermissionDeniedError):
            await ssh_credential_service.list_all_credentials(seeded["admin_a"])

    async def test_reveal_undecryptable_row_is_refused_and_audited(self, db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-a", "inst-a")
        other_dir = tmp_path / "other-key"
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(other_dir))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_C)
        credential_cipher.reset_credential_cipher_cache()
        broken = encrypt_secret(PASSWORD + "-broken")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_A)
        credential_cipher.reset_credential_cipher_cache()
        row = await DBWsSshCredential.create(
            workspace_id=ws.id,
            owner_kind="platform",
            created_via="admin",
            alias="broken",
            host=SAFE_IP,
            username="root",
            password_enc=broken,
            status="active",
        )
        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.reveal_secret(platform_owner(), ws.id, row.id)
        assert "密钥解不开" in str(exc_info.value)
        audit = await DBWsSshAudit.filter(
            credential_id=row.id,
            event=EVENT_CRED_REVEAL,
            allowed=False,
        ).first()
        assert audit is not None
        assert audit.reason == "undecryptable"

    async def test_tenant_cannot_update_or_delete_platform_and_other_tenant_rows(self, db) -> None:  # noqa: ARG002
        seeded = await self._seed()
        ws_a = seeded["ws_a"]
        admin_a = seeded["admin_a"]

        for row in (seeded["platform_row"], seeded["b_row"]):
            with pytest.raises(NotFoundError):
                await ssh_credential_service.update_credential(
                    admin_a,
                    ws_a.id,
                    row.id,
                    SshCredentialUpdate(note="x"),
                )
            with pytest.raises(NotFoundError):
                await ssh_credential_service.delete_credential(admin_a, ws_a.id, row.id)
            with pytest.raises(NotFoundError):
                await ssh_credential_service.set_status(admin_a, ws_a.id, row.id, enable=False)
            with pytest.raises(NotFoundError):
                await ssh_credential_service.reveal_secret(admin_a, ws_a.id, row.id)

    async def test_platform_can_reveal_chat_row_but_tenant_cannot(self, db) -> None:  # noqa: ARG002
        seeded = await self._seed()
        ws_a = seeded["ws_a"]
        chat_row = seeded["chat_row"]

        platform_secret = await ssh_credential_service.reveal_secret(
            seeded["platform"],
            ws_a.id,
            chat_row.id,
        )
        assert platform_secret.password == PASSWORD

        with pytest.raises(ChatRegisteredSecretForbiddenError) as exc_info:
            await ssh_credential_service.reveal_secret(seeded["admin_a"], ws_a.id, chat_row.id)
        assert "这条凭据的明文只有平台方能看" in str(exc_info.value)
        audit = await DBWsSshAudit.filter(
            credential_id=chat_row.id,
            event=EVENT_CRED_REVEAL,
            allowed=False,
        ).order_by("-id").first()
        assert audit is not None
        assert audit.reason == REASON_FORBIDDEN

    async def test_tenant_can_change_note_and_status_but_not_connection(self, db) -> None:  # noqa: ARG002
        seeded = await self._seed()
        ws_a = seeded["ws_a"]
        admin_a = seeded["admin_a"]
        chat_row = seeded["chat_row"]

        with pytest.raises(ChatRegisteredEditForbiddenError):
            await ssh_credential_service.update_credential(
                admin_a,
                ws_a.id,
                chat_row.id,
                SshCredentialUpdate(host=SAFE_IP_2),
            )
        with pytest.raises(ChatRegisteredEditForbiddenError):
            await ssh_credential_service.update_credential(
                admin_a,
                ws_a.id,
                chat_row.id,
                SshCredentialUpdate(password=NEW_PASSWORD),
            )

        updated = await ssh_credential_service.update_credential(
            admin_a,
            ws_a.id,
            chat_row.id,
            SshCredentialUpdate(note="只在聊天里登记的机器"),
        )
        assert updated.note == "只在聊天里登记的机器"

        disabled = await ssh_credential_service.set_status(admin_a, ws_a.id, chat_row.id, enable=False)
        assert disabled.status == "disabled"
        enabled = await ssh_credential_service.set_status(admin_a, ws_a.id, chat_row.id, enable=True)
        assert enabled.status == "active"

        await ssh_credential_service.delete_credential(admin_a, ws_a.id, chat_row.id)
        assert await DBWsSshCredential.filter(id=chat_row.id).exists() is False

    async def test_platform_row_creation_requires_platform_workspace(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        ws_a, _ws_b = await setup_tenants()
        admin = tenant_admin(TENANT_A, "admin-a")

        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.create_credential(
                admin,
                ws_a.id,
                create_body("platform-box"),
            )
        assert "租户不能登记平台方服务器" in str(exc_info.value)

        async def _denied(_workspace: DBWorkspace) -> bool:
            return False

        monkeypatch.setattr(credentials_module, "platform_credentials_allowed", _denied)
        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.create_credential(
                platform_owner(),
                ws_a.id,
                create_body("platform-box-2"),
            )
        assert "不能挂平台方服务器" in str(exc_info.value)

    async def test_user_row_requires_bound_instance_and_accessible_tenant(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        ws_a, _ws_b = await setup_tenants()
        admin = tenant_admin(TENANT_A, "admin-a")

        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.create_credential(
                admin,
                ws_a.id,
                create_body(
                    "not-bound",
                    owner_kind="user",
                    owner_account="user-a",
                    adapter_key="onebot_v11",
                    instance_key="inst-b",
                ),
            )
        assert "没有绑定" in str(exc_info.value)

        # 工作区归属推导用自己的一份实例查询，不受这个桩影响；桩只让服务层看到
        # "这个实例不在调用者的实例对里"，用来单独钉住第二道判定
        async def _no_refs(_user: DBUser) -> set[Tuple[str, str]]:
            return set()

        monkeypatch.setattr(credentials_module, "accessible_instance_refs", _no_refs)
        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.create_credential(
                admin,
                ws_a.id,
                create_body(
                    "not-mine",
                    owner_kind="user",
                    owner_account="user-a",
                    adapter_key="onebot_v11",
                    instance_key="inst-a",
                ),
            )
        assert "不属于你所在的租户" in str(exc_info.value)

    async def test_owner_whitelisted_flag_uses_user_allowlist(self, db) -> None:  # noqa: ARG002
        ws_a, _ws_b = await setup_tenants()
        admin = tenant_admin(TENANT_A, "admin-a")
        row = await ssh_credential_service.create_credential(
            admin,
            ws_a.id,
            create_body(
                "admin-box",
                owner_kind="user",
                owner_account="user-a",
                adapter_key="onebot_v11",
                instance_key="inst-a",
            ),
        )
        assert row.owner_whitelisted is True

        out_row = await ssh_credential_service.create_credential(
            admin,
            ws_a.id,
            create_body(
                "admin-box-2",
                owner_kind="user",
                owner_account="user-z",
                adapter_key="onebot_v11",
                instance_key="inst-a",
            ),
        )
        assert out_row.owner_whitelisted is False

        platform_row = await make_platform_row(ws_a, "platform-box")
        platform_view = await ssh_credential_service.get_credential(
            platform_owner(),
            ws_a.id,
            platform_row.id,
        )
        assert platform_view.owner_whitelisted is None


# ---------------------------------------------------------------------------
# 更新语义
# ---------------------------------------------------------------------------


class TestUpdateSemantics:
    async def _row(self) -> Tuple[DBWorkspace, DBUser, DBWsSshCredential]:
        ws = await make_workspace("ws-a", "inst-a")
        await make_instance("inst-a", TENANT_A)
        owner = platform_owner()
        view = await ssh_credential_service.create_credential(
            owner,
            ws.id,
            create_body("web1", password=PASSWORD, private_key=PRIVATE_KEY, passphrase=PASSPHRASE),
        )
        row = await DBWsSshCredential.get(id=view.id)
        return ws, owner, row

    async def test_mask_keeps_stored_ciphertext(self, db) -> None:  # noqa: ARG002
        ws, owner, row = await self._row()
        before = (row.password_enc, row.private_key_enc, row.passphrase_enc)
        await ssh_credential_service.update_credential(
            owner,
            ws.id,
            row.id,
            SshCredentialUpdate(
                password=CREDENTIAL_MASK,
                private_key=CREDENTIAL_MASK,
                passphrase=CREDENTIAL_MASK,
                note="new note",
            ),
        )
        fresh = await DBWsSshCredential.get(id=row.id)
        assert (fresh.password_enc, fresh.private_key_enc, fresh.passphrase_enc) == before

    async def test_empty_string_clears_one_secret_and_both_empty_is_refused(self, db) -> None:  # noqa: ARG002
        ws, owner, row = await self._row()
        updated = await ssh_credential_service.update_credential(
            owner,
            ws.id,
            row.id,
            SshCredentialUpdate(passphrase=""),
        )
        assert updated.has_passphrase is False

        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.update_credential(
                owner,
                ws.id,
                row.id,
                SshCredentialUpdate(password="", private_key=""),
            )
        assert "至少要留一样" in str(exc_info.value)

    async def test_unreadable_row_keeps_ciphertext_when_only_note_changes(self, db, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-a", "inst-a")
        other_dir = tmp_path / "other-key"
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(other_dir))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_C)
        credential_cipher.reset_credential_cipher_cache()
        broken = encrypt_secret(PASSWORD + "-broken")
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY_A)
        credential_cipher.reset_credential_cipher_cache()
        row = await DBWsSshCredential.create(
            workspace_id=ws.id,
            owner_kind="platform",
            created_via="admin",
            alias="broken",
            host=SAFE_IP,
            username="root",
            password_enc=broken,
            status="active",
        )
        view = await ssh_credential_service.update_credential(
            platform_owner(),
            ws.id,
            row.id,
            SshCredentialUpdate(note="still broken"),
        )
        assert view.secret_state == "undecryptable"
        assert (await DBWsSshCredential.get(id=row.id)).password_enc == broken

    async def test_auth_failed_becomes_active_after_password_change(self, db) -> None:  # noqa: ARG002
        ws, owner, row = await self._row()
        await DBWsSshCredential.filter(id=row.id).update(status="auth_failed")
        view = await ssh_credential_service.update_credential(
            owner,
            ws.id,
            row.id,
            SshCredentialUpdate(password=NEW_PASSWORD),
        )
        assert view.status == "active"

    async def test_enable_auth_failed_and_suspended_are_refused(self, db) -> None:  # noqa: ARG002
        ws, owner, row = await self._row()
        await DBWsSshCredential.filter(id=row.id).update(status="auth_failed")
        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.set_status(owner, ws.id, row.id, enable=True)
        assert "请先更新凭据" in str(exc_info.value)

        await DBWsSshCredential.filter(id=row.id).update(status="suspended")
        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.set_status(owner, ws.id, row.id, enable=True)
        assert "工作区已不归平台方" in str(exc_info.value)

    async def test_disable_then_enable_cannot_launder_suspended_or_auth_failed(self, db) -> None:  # noqa: ARG002
        ws, owner, row = await self._row()

        # 挂起是粘的：不能停用，也就不能借「停用→启用」回到 active
        await DBWsSshCredential.filter(id=row.id).update(status="suspended")
        with pytest.raises(ValidationError):
            await ssh_credential_service.set_status(owner, ws.id, row.id, enable=False)
        with pytest.raises(ValidationError):
            await ssh_credential_service.update_credential(owner, ws.id, row.id, SshCredentialUpdate(status="disabled"))
        assert (await DBWsSshCredential.get(id=row.id)).status == "suspended"

        # 登不上的行可以停用，但停用后再启用仍要求先改凭据
        await DBWsSshCredential.filter(id=row.id).update(status="auth_failed", status_reason="")
        await ssh_credential_service.set_status(owner, ws.id, row.id, enable=False)
        stored = await DBWsSshCredential.get(id=row.id)
        assert (stored.status, stored.status_reason) == ("disabled", "auth_failed")
        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.set_status(owner, ws.id, row.id, enable=True)
        assert "请先更新凭据" in str(exc_info.value)

        # 改了密码：记号去掉、仍停用；之后可以正常启用
        await ssh_credential_service.update_credential(owner, ws.id, row.id, SshCredentialUpdate(password=NEW_PASSWORD))
        stored = await DBWsSshCredential.get(id=row.id)
        assert (stored.status, stored.status_reason) == ("disabled", "")
        view = await ssh_credential_service.set_status(owner, ws.id, row.id, enable=True)
        assert view.status == "active"

    async def test_note_only_edit_does_not_reactivate_a_key_only_auth_failed_row(self, db) -> None:  # noqa: ARG002
        """编辑表单把没填过的密码以空串提交：原本就空的列不算「换了凭据」，不能借此回到 active"""
        ws, owner, row = await self._row()
        await DBWsSshCredential.filter(id=row.id).update(
            password_enc="",
            private_key_enc=encrypt_secret(PRIVATE_KEY),
            status="auth_failed",
        )
        await ssh_credential_service.update_credential(
            owner,
            ws.id,
            row.id,
            SshCredentialUpdate(note="only note", password="", private_key=CREDENTIAL_MASK, passphrase=""),
        )
        stored = await DBWsSshCredential.get(id=row.id)
        assert stored.status == "auth_failed"
        audit = await DBWsSshAudit.filter(credential_id=row.id, event="cred_update").order_by("-id").first()
        assert audit is not None and "password" not in audit.detail

    async def test_recent_successful_test_allows_enable_without_new_secret(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.ssh_gateway import row_state

        ws, owner, row = await self._row()
        await DBWsSshCredential.filter(id=row.id).update(status="auth_failed")
        stored = await DBWsSshCredential.get(id=row.id)
        row_state.note_test_passed(stored)
        try:
            view = await ssh_credential_service.set_status(owner, ws.id, row.id, enable=True)
            assert view.status == "active"

            # 测试通过之后凭据又改过：那次测试不再算数
            await DBWsSshCredential.filter(id=row.id).update(status="auth_failed")
            row_state.note_test_passed(await DBWsSshCredential.get(id=row.id))
            await DBWsSshCredential.filter(id=row.id).update(password_enc=encrypt_secret(NEW_PASSWORD))
            with pytest.raises(ValidationError):
                await ssh_credential_service.set_status(owner, ws.id, row.id, enable=True)
        finally:
            row_state.reset_pending_host_keys()

    async def test_gateway_auth_failure_keeps_suspended_and_disabled_rows(self, db) -> None:  # noqa: ARG002
        from nekro_agent.services.ssh_gateway import audit_queue, row_state

        ws, _owner, row = await self._row()
        try:
            await DBWsSshCredential.filter(id=row.id).update(status="suspended", status_reason="workspace_not_platform")
            await row_state.mark_auth_failed(row.id, "服务器拒绝了这组凭据")
            stored = await DBWsSshCredential.get(id=row.id)
            assert (stored.status, stored.status_reason) == ("suspended", "workspace_not_platform")

            await DBWsSshCredential.filter(id=row.id).update(status="disabled", status_reason="")
            await row_state.mark_auth_failed(row.id, "服务器拒绝了这组凭据")
            stored = await DBWsSshCredential.get(id=row.id)
            assert (stored.status, stored.status_reason) == ("disabled", "auth_failed")

            await DBWsSshCredential.filter(id=row.id).update(status="active", status_reason="")
            await row_state.mark_auth_failed(row.id, "服务器拒绝了这组凭据")
            assert (await DBWsSshCredential.get(id=row.id)).status == "auth_failed"
        finally:
            audit_queue.reset_audit_queue()

    async def test_rename_to_existing_alias_is_a_validation_error(self, db) -> None:  # noqa: ARG002
        ws, owner, row = await self._row()
        await ssh_credential_service.create_credential(owner, ws.id, create_body("taken"))
        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.update_credential(owner, ws.id, row.id, SshCredentialUpdate(alias="taken"))
        assert "同名" in str(exc_info.value)

    async def test_user_row_host_change_is_checked_again(self, db) -> None:  # noqa: ARG002
        ws_a, _ws_b = await setup_tenants()
        admin = tenant_admin(TENANT_A, "admin-a")
        created = await ssh_credential_service.create_credential(
            admin,
            ws_a.id,
            create_body(
                "web1",
                owner_kind="user",
                owner_account="user-a",
                adapter_key="onebot_v11",
                instance_key="inst-a",
            ),
        )
        with pytest.raises(ValidationError) as exc_info:
            await ssh_credential_service.update_credential(
                admin,
                ws_a.id,
                created.id,
                SshCredentialUpdate(host="10.0.0.5"),
            )
        assert "不允许的地址" in str(exc_info.value)
        assert (await DBWsSshCredential.get(id=created.id)).host == SAFE_IP


# ---------------------------------------------------------------------------
# 表结构
# ---------------------------------------------------------------------------


def test_table_and_column_names_match_the_design() -> None:
    assert DBWsSshCredential._meta.db_table == "ws_ssh_credential"
    assert set(DBWsSshCredential._meta.fields_map) == {
        "id",
        "workspace",
        "workspace_id",
        "owner_kind",
        "owner_account",
        "adapter_key",
        "instance_key",
        "created_via",
        "import_ref",
        "alias",
        "host",
        "port",
        "username",
        "password_enc",
        "private_key_enc",
        "passphrase_enc",
        "host_keys",
        "status",
        "status_reason",
        "note",
        # 值守模式：行上的模式与所有者写的手册；Bot 追加的经验在另一张表，这里是反向关系
        "duty_mode",
        "runbook",
        "runbook_entries",
        "created_by",
        "updated_by",
        "last_used_at",
        "last_error",
        "create_time",
        "update_time",
    }
    assert DBWsSshAudit._meta.db_table == "ws_ssh_audit"
    assert set(DBWsSshAudit._meta.fields_map) == {
        "id",
        "create_time",
        "workspace_id",
        "credential_id",
        "owner_kind",
        "owner_account",
        "adapter_key",
        "instance_key",
        "event",
        "allowed",
        "reason",
        "task_ref",
        "lease_id",
        "conn_id",
        "triggered_by",
        "channel_kind",
        "detail",
        "input_head",
        "exit_status",
        "bytes_up",
        "bytes_down",
        "duration_ms",
        "host_key_fp",
        "actor",
    }


# ---------------------------------------------------------------------------
# 路由与审计
# ---------------------------------------------------------------------------


class TestRoutesAndAudit:
    async def test_read_only_member_is_refused_by_the_route(self, db) -> None:  # noqa: ARG002
        ws, _ws_b = await setup_tenants()
        reader = tenant_reader(TENANT_A, "reader-a")
        with pytest.raises(HTTPException) as exc_info:
            await ssh_router.list_ssh_credentials(ws.id, _current_user=reader)
        assert exc_info.value.status_code == 403

    async def test_secret_response_carries_no_store(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-a", "inst-a")
        await make_instance("inst-a", TENANT_A)
        owner = platform_owner()
        created = await ssh_credential_service.create_credential(owner, ws.id, create_body("web1"))
        response = Response()
        secret = await ssh_router.get_ssh_credential_secret(
            ws.id,
            created.id,
            response,
            _current_user=owner,
        )
        assert secret.password == PASSWORD
        assert response.headers["Cache-Control"] == "no-store"

    async def test_audit_is_written_for_each_operation_and_has_no_values(self, db) -> None:  # noqa: ARG002
        ws = await make_workspace("ws-a", "inst-a")
        await make_instance("inst-a", TENANT_A)
        owner = platform_owner()
        created = await ssh_credential_service.create_credential(owner, ws.id, create_body("web1"))
        await ssh_credential_service.update_credential(
            owner,
            ws.id,
            created.id,
            SshCredentialUpdate(password=NEW_PASSWORD, note="updated"),
        )
        await ssh_credential_service.reveal_secret(owner, ws.id, created.id)
        await ssh_credential_service.set_status(owner, ws.id, created.id, enable=False)
        await ssh_credential_service.set_status(owner, ws.id, created.id, enable=True)
        await ssh_credential_service.delete_credential(owner, ws.id, created.id)

        rows = await DBWsSshAudit.filter(credential_id=created.id).order_by("id")
        events = [row.event for row in rows]
        assert EVENT_CRED_CREATE in events
        assert EVENT_CRED_UPDATE in events
        assert EVENT_CRED_REVEAL in events
        assert EVENT_CRED_DELETE in events
        for row in rows:
            assert PASSWORD not in row.detail
            assert NEW_PASSWORD not in row.detail
            assert PRIVATE_KEY not in row.detail

        create_audit = next(row for row in rows if row.event == EVENT_CRED_CREATE)
        assert "password" in create_audit.detail
        update_audit = next(row for row in rows if row.event == EVENT_CRED_UPDATE)
        assert "password" in update_audit.detail
        assert "note" in update_audit.detail

    async def test_audit_list_filters_by_snapshot_and_paginates(self, db) -> None:  # noqa: ARG002
        ws_a, ws_b = await setup_tenants()
        admin_a = tenant_admin(TENANT_A, "admin-a")
        platform = platform_owner()
        row_a = await make_user_row(ws_a, "a-box", instance_key="inst-a")
        row_b = await make_user_row(ws_b, "b-box", instance_key="inst-b")
        platform_row = await make_platform_row(ws_a, "p-box")

        for index in range(3):
            await DBWsSshAudit.create(
                workspace_id=ws_a.id,
                credential_id=row_a.id,
                owner_kind="user",
                owner_account="user-a",
                adapter_key="onebot_v11",
                instance_key="inst-a",
                event=EVENT_CRED_UPDATE,
                allowed=True,
                task_ref=f"task-{index}",
                detail="note",
                actor="seed",
            )
        await DBWsSshAudit.create(
            workspace_id=ws_b.id,
            credential_id=row_b.id,
            owner_kind="user",
            owner_account="user-b",
            adapter_key="onebot_v11",
            instance_key="inst-b",
            event=EVENT_CRED_UPDATE,
            allowed=True,
            detail="note",
            actor="seed",
        )
        await DBWsSshAudit.create(
            workspace_id=ws_a.id,
            credential_id=platform_row.id,
            owner_kind="platform",
            event=EVENT_CRED_UPDATE,
            allowed=True,
            detail="note",
            actor="seed",
        )

        tenant_items, _ = await ssh_credential_service.list_audit(admin_a, limit=100)
        assert {item.credential_id for item in tenant_items} == {row_a.id}
        assert all(item.owner_kind == "user" for item in tenant_items)

        platform_items, _ = await ssh_credential_service.list_audit(platform, limit=100)
        assert {item.credential_id for item in platform_items} == {row_a.id, row_b.id, platform_row.id}

        first_page, next_before = await ssh_credential_service.list_audit(platform, limit=2)
        assert len(first_page) == 2
        assert next_before is not None
        second_page, _ = await ssh_credential_service.list_audit(
            platform,
            limit=2,
            before_id=next_before,
        )
        assert {item.id for item in first_page}.isdisjoint({item.id for item in second_page})

        task_items, _ = await ssh_credential_service.list_audit(platform, task_ref="task-1")
        assert len(task_items) == 1
        assert task_items[0].task_ref == "task-1"


# ---------------------------------------------------------------------------
# 保留期与配置
# ---------------------------------------------------------------------------


class TestRetentionAndConfig:
    async def test_old_audit_rows_are_deleted_and_new_ones_kept(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        old = await DBWsSshAudit.create(
            workspace_id=1,
            owner_kind="platform",
            event=EVENT_CRED_CREATE,
            allowed=True,
            actor="seed",
        )
        fresh = await DBWsSshAudit.create(
            workspace_id=1,
            owner_kind="platform",
            event=EVENT_CRED_CREATE,
            allowed=True,
            actor="seed",
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        await DBWsSshAudit.filter(id=old.id).update(create_time=now - datetime.timedelta(days=200))

        monkeypatch.setattr(config, "SSH_GATEWAY_AUDIT_RETENTION_DAYS", 180)
        assert retention_module.current_retention_days() == 180
        deleted = await retention_module.purge_once(now=now)
        assert deleted == 1
        assert await DBWsSshAudit.filter(id=old.id).exists() is False
        assert await DBWsSshAudit.filter(id=fresh.id).exists() is True

    async def test_zero_days_keeps_everything(self, db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        row = await DBWsSshAudit.create(
            workspace_id=1,
            owner_kind="platform",
            event=EVENT_CRED_CREATE,
            allowed=True,
            actor="seed",
        )
        await DBWsSshAudit.filter(id=row.id).update(
            create_time=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=500),
        )
        monkeypatch.setattr(config, "SSH_GATEWAY_AUDIT_RETENTION_DAYS", 0)
        assert await retention_module.purge_once() == 0
        assert await DBWsSshAudit.filter(id=row.id).exists() is True

    def test_config_defaults(self) -> None:
        from nekro_agent.core.config import CoreConfig

        fields = CoreConfig.model_fields
        assert fields["SSH_GATEWAY_ENABLED"].default is False
        assert fields["SSH_GATEWAY_OWNER_ACCOUNTS"].default == []
        assert fields["SSH_GATEWAY_USER_ACCOUNTS"].default == []
        assert fields["SSH_GATEWAY_LEASE_MAX_SECONDS"].default == 7200
        assert fields["SSH_GATEWAY_AUDIT_RETENTION_DAYS"].default == 180
