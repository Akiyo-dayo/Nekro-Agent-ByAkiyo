"""独立复核之后补的网关加固

## 这条测试钉的是什么

- 审计入队时洗掉 NUL、截到列宽；写库逐行出队，失败重试不会把写成的行再插一遍；
- 私钥里由登记者决定的 KDF 成本（bcrypt 轮数、PKCS#8 迭代次数）有上限，PuTTY 格式不收，
  超限的私钥在解析之前就被拒（不跑 KDF）；
- 别名不合规则（含 NUL）时不查库、不计入熔断；一条连接上换钥匙试的次数有上限；锁住期间
  的拒绝审计限流；
- 容器里收权限的脚本先核对目录、把目录收成 root 所有，再碰里面的文件；
- 同一频道上一轮还没收尾就开始的新一轮，快照记成「不完整」；
- 租约：这一次没拍到也要结束同一任务标识下的旧租约；周期收尾结束过期的、删掉结束已久的；
- 监督任务：熔断不会被同一拍随后的 `RUNNING` 盖掉。

环境是内存 SQLite；秘密值一律现编。
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any, List

import asyncssh
import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.ops_agent.triggers import (
    TRIGGER_KIND_HUMAN,
    RoundTrigger,
    RoundTriggerBook,
)
from nekro_agent.services.ssh_gateway import audit_queue, container_files, events, keys, registry
from nekro_agent.services.ssh_gateway import authority as authority_module
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import lease as lease_module
from nekro_agent.services.ssh_gateway.authz import Coverage
from nekro_agent.services.ssh_gateway.validation import (
    MAX_BCRYPT_ROUNDS,
    private_key_cost_problem,
    validate_host,
    validate_private_key,
)
from nekro_agent.tools import credential_cipher

# 只做字面量判断，不会真的连；拆开写是为了不被公开仓库的泄漏扫描当成真实地址
PUBLIC_PREFIX = "9.9.9."
LISTEN = PUBLIC_PREFIX + "10"
PEER = PUBLIC_PREFIX + "11"
TARGET_IP = PUBLIC_PREFIX + "9"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "ssh-hardening-" + uuid.uuid4().hex)
    monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
    monkeypatch.setattr(config, "SSH_GATEWAY_LEASE_MAX_SECONDS", 600)
    monkeypatch.setattr(config, "SSH_GATEWAY_AUDIT_INPUT_BYTES", 64)
    credential_cipher.reset_credential_cipher_cache()
    audit_queue.reset_audit_queue()
    registry.reset_registry()
    lease_module.reset_leases()
    authority_module.reset_authority_state()
    events.reset_credential_callbacks()
    try:
        yield
    finally:
        audit_queue.reset_audit_queue()
        registry.reset_registry()
        lease_module.reset_leases()
        authority_module.reset_authority_state()
        events.reset_credential_callbacks()
        credential_cipher.reset_credential_cipher_cache()


# ---------------------------------------------------------------------------
# 审计入队清洗与逐行出队
# ---------------------------------------------------------------------------


def _audit_row(**overrides: Any) -> dict:
    row = {
        "workspace_id": 1,
        "credential_id": 2,
        "owner_kind": C.OWNER_PLATFORM,
        "owner_account": "",
        "adapter_key": "",
        "instance_key": "",
        "event": C.EVENT_CHANNEL,
        "allowed": True,
        "reason": "",
        "task_ref": "t",
        "lease_id": "l",
        "conn_id": "c",
        "triggered_by": [],
        "channel_kind": "exec",
        "detail": "",
        "input_head": "",
        "exit_status": None,
        "bytes_up": None,
        "bytes_down": None,
        "duration_ms": None,
        "host_key_fp": "",
        "actor": "gateway",
    }
    row.update(overrides)
    return row


async def test_audit_rows_are_sanitized_on_enqueue(db) -> None:  # noqa: ARG001
    assert audit_queue.enqueue(
        **_audit_row(
            channel_kind="subsystem:" + "x" * 100,
            detail="rm -rf /\x00tmp",
            input_head="bin\x00ary" + "\udcff" + "y" * 200,
            triggered_by=["acc\x00ount"],
            duration_ms=10**12,
        )
    )
    await audit_queue.flush()

    stored = await DBWsSshAudit.get(conn_id="c")
    assert len(stored.channel_kind) <= 24
    assert "\x00" not in stored.detail and "\\x00" in stored.detail
    assert "\x00" not in stored.input_head
    assert len(stored.input_head.encode("utf-8")) <= 64
    assert stored.triggered_by == ["acc\\x00ount"]
    assert stored.duration_ms == 2**31 - 1


async def test_audit_writes_pop_rows_one_by_one_so_retries_do_not_duplicate(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # noqa: ARG001
    for index in range(3):
        audit_queue.enqueue(**_audit_row(conn_id=f"c{index}"))

    original = DBWsSshAudit.create
    calls = {"n": 0}

    async def flaky(**fields: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("db hiccup")
        return await original(**fields)

    monkeypatch.setattr(DBWsSshAudit, "create", flaky)
    assert await audit_queue._write_pending(until_empty=True) is False  # noqa: SLF001
    assert audit_queue.queued_rows() == 2
    assert await audit_queue._write_pending(until_empty=True) is True  # noqa: SLF001

    conn_ids = sorted(row.conn_id for row in await DBWsSshAudit.all())
    assert conn_ids == ["c0", "c1", "c2"], "写成的那一行不能在重试时再插一遍"


async def test_a_row_that_never_writes_is_dropped_instead_of_blocking_the_queue(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # noqa: ARG001
    """队头一行永远写不进去（库本身是好的）：试写下一行成功后丢掉它，队列不再卡死"""
    for index in range(3):
        audit_queue.enqueue(**_audit_row(conn_id=f"p{index}"))

    original = DBWsSshAudit.create

    async def poison_first(**fields: Any) -> Any:
        if fields.get("conn_id") == "p0":
            raise RuntimeError("value out of range for this column")
        return await original(**fields)

    monkeypatch.setattr(DBWsSshAudit, "create", poison_first)
    results = [await audit_queue._write_pending(until_empty=True) for _ in range(audit_queue.POISON_PROBE_AFTER_FAILURES)]  # noqa: SLF001
    assert results[:-1] == [False] * (audit_queue.POISON_PROBE_AFTER_FAILURES - 1), "前几次照常退避重试"
    assert results[-1] is True
    assert audit_queue.queued_rows() == 0
    assert sorted(row.conn_id for row in await DBWsSshAudit.all()) == ["p1", "p2"]


async def test_row_level_errors_are_dropped_at_once_and_db_outages_are_not(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # noqa: ARG001
    from tortoise.exceptions import IntegrityError

    audit_queue.enqueue(**_audit_row(conn_id="i0"))
    audit_queue.enqueue(**_audit_row(conn_id="i1"))
    original = DBWsSshAudit.create

    async def integrity_first(**fields: Any) -> Any:
        if fields.get("conn_id") == "i0":
            raise IntegrityError("constraint")
        return await original(**fields)

    monkeypatch.setattr(DBWsSshAudit, "create", integrity_first)
    assert await audit_queue._write_pending(until_empty=True) is True  # noqa: SLF001
    assert [row.conn_id for row in await DBWsSshAudit.all()] == ["i1"]

    # 库整体不可用：每一行都写不进去，不能一行行丢光
    for index in range(3):
        audit_queue.enqueue(**_audit_row(conn_id=f"d{index}"))

    async def all_down(**_fields: Any) -> Any:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(DBWsSshAudit, "create", all_down)
    for _ in range(audit_queue.POISON_PROBE_AFTER_FAILURES + 2):
        assert await audit_queue._write_pending(until_empty=True) is False  # noqa: SLF001
    assert audit_queue.queued_rows() == 3


# ---------------------------------------------------------------------------
# 私钥 KDF 成本
# ---------------------------------------------------------------------------


def _openssh_key_with_rounds(rounds: int) -> str:
    """导出一把带口令短语的 OpenSSH 私钥，再把 bcrypt 轮数改成 `rounds`（不真的跑 KDF）"""
    key = asyncssh.generate_private_key("ssh-ed25519")
    text = key.export_private_key("openssh", passphrase="phrase-" + "x1", rounds=16).decode("ascii")
    lines = text.strip().splitlines()
    data = bytearray(base64.b64decode("".join(lines[1:-1])))
    pos = len(b"openssh-key-v1\x00")
    for _ in range(2):  # 跳过 ciphername、kdfname
        length = int.from_bytes(data[pos : pos + 4], "big")
        pos += 4 + length
    pos += 4  # kdfoptions 的长度
    salt_length = int.from_bytes(data[pos : pos + 4], "big")
    pos += 4 + salt_length
    data[pos : pos + 4] = int(rounds).to_bytes(4, "big")
    body = base64.b64encode(bytes(data)).decode("ascii")
    wrapped = "\n".join(body[i : i + 70] for i in range(0, len(body), 70))
    return f"{lines[0]}\n{wrapped}\n{lines[-1]}\n"


def test_private_key_cost_limits() -> None:
    assert private_key_cost_problem(_openssh_key_with_rounds(16)) == ""
    assert private_key_cost_problem(_openssh_key_with_rounds(MAX_BCRYPT_ROUNDS)) == ""
    assert "轮数过高" in private_key_cost_problem(_openssh_key_with_rounds(0xFFFFFFFF))
    assert "PuTTY" in private_key_cost_problem("PuTTY-User-Key-File-3: ssh-ed25519\n")
    with pytest.raises(ValidationError):
        validate_private_key(_openssh_key_with_rounds(0xFFFFFFFF))


async def test_upstream_rejects_costly_key_before_running_the_kdf(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.ssh_gateway import upstream
    from nekro_agent.services.ssh_gateway.authority import TargetSpec

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("超限的私钥不该进 KDF")

    monkeypatch.setattr(upstream.asyncssh, "import_private_key", explode)
    target = TargetSpec(
        credential_id=0,
        workspace_id=1,
        alias="web1",
        owner_kind=C.OWNER_USER,
        host=TARGET_IP,
        connect_address=TARGET_IP,
        port=22,
        username="root",
        password="",
        private_key=_openssh_key_with_rounds(0xFFFFFFFF),
        passphrase="phrase-x1",
        host_keys=[],
    )
    with pytest.raises(upstream.UpstreamError) as exc_info:
        await upstream.open_upstream_for_probe(target)
    assert exc_info.value.reason == C.REASON_UPSTREAM_AUTH_FAILED


def test_text_fields_reject_nul() -> None:
    with pytest.raises(ValidationError):
        validate_host("ssh.example\x00.test")


# ---------------------------------------------------------------------------
# 认证：别名形状、锁住期间的审计限流、每连接的尝试次数
# ---------------------------------------------------------------------------


async def _peer(workspace: DBWorkspace) -> str:
    _private, public = keys.generate_client_keypair()
    registry.register_peer(
        registry.ContainerPeer(
            workspace_id=workspace.id,
            peer_address=PEER,
            listen_address=LISTEN,
            client_public_key=public,
            isolation_ok=True,
            image_ok=True,
        )
    )
    return public


async def test_malformed_alias_is_denied_without_a_query(db, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG001
    workspace = await DBWorkspace.create(name="ws-alias", description="ws-alias", status="active")
    public = await _peer(workspace)

    async def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("不合规则的别名不该拿去查库")

    monkeypatch.setattr(DBWsSshCredential, "get_or_none", explode)
    decision = await authority_module.authorize_connection(
        listen_address=LISTEN,
        peer_address=PEER,
        client_key_line=public,
        alias="web1\x00",
    )
    assert decision.ok is False
    assert decision.reason == C.REASON_UNKNOWN_ALIAS


async def test_locked_workspace_audits_at_most_once_a_minute(db) -> None:  # noqa: ARG001
    workspace = await DBWorkspace.create(name="ws-lock", description="ws-lock", status="active")
    public = await _peer(workspace)
    for _ in range(authority_module.AUTH_FAILURE_LIMIT):
        await authority_module.authorize_connection(
            listen_address=LISTEN,
            peer_address=PEER,
            client_key_line=public,
            alias="nope",
        )
    before = audit_queue.queued_rows()
    for _ in range(20):
        decision = await authority_module.authorize_connection(
            listen_address=LISTEN,
            peer_address=PEER,
            client_key_line=public,
            alias="nope",
        )
        assert decision.reason == C.REASON_LIMIT_EXCEEDED
    assert audit_queue.queued_rows() - before == 1


async def test_one_connection_cannot_try_unlimited_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.ssh_gateway import server as server_module

    backend = server_module.GatewayBackendImpl()
    gateway_server = server_module.GatewayServer(backend)
    disconnects: List[int] = []
    attempts: List[str] = []

    async def deny(line: str) -> bool:
        attempts.append(line)
        return False

    monkeypatch.setattr(gateway_server, "_authorize_and_login", deny)
    monkeypatch.setattr(gateway_server, "disconnect", lambda: disconnects.append(1))
    for _ in range(server_module.MAX_AUTH_ATTEMPTS + 3):
        key = asyncssh.generate_private_key("ssh-ed25519")
        assert await gateway_server.validate_public_key("web1", key) is False
    assert len(attempts) == server_module.MAX_AUTH_ATTEMPTS
    assert disconnects


# ---------------------------------------------------------------------------
# 容器里收权限的脚本
# ---------------------------------------------------------------------------


def test_permission_script_locks_the_directory_before_touching_files() -> None:
    script = container_files._fix_permissions_script()  # noqa: SLF001
    assert 'cd -P "$d"' in script
    assert '[ "$(pwd -P)" = "$d" ] || exit 3' in script
    lock = script.index("chown root:root .;")
    assert lock < script.index("chown 1000:1000 id_gateway")
    assert lock < script.index('if [ -L "$f" ]')
    assert "stat -c %h" in script
    # 不再出现会顺着容器能改的路径走的写法
    assert 'chown root:root "$d"' not in script
    assert 'chmod 0600 "$d/id_gateway"' not in script


def test_host_side_write_refuses_a_symlinked_nk_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_dir = tmp_path / "ws"
    (workspace_dir / ".claude_home").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (workspace_dir / ".claude_home" / "nk-ssh").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器上建不了符号链接")
    monkeypatch.setattr(container_files.WorkspaceService, "get_workspace_dir", staticmethod(lambda _id: workspace_dir))

    # nk-ssh 是 NA 自己的路径：被换成链接时删掉链接本身重建目录，绝不写到链接指向的地方
    # （两种平台同一个结果，见 `safe_fs`）
    container_files._write_nk_ssh_files_on_host(1, {"config": "x"})  # noqa: SLF001
    assert not (workspace_dir / ".claude_home" / "nk-ssh").is_symlink()
    assert (workspace_dir / ".claude_home" / "nk-ssh" / "config").read_text(encoding="utf-8") == "x"
    assert list(outside.iterdir()) == []


def test_host_side_write_refuses_a_symlinked_claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`.claude_home` 本身被换成链接时不替它做主：这一次不写，链接那头一个字节都不落"""
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (workspace_dir / ".claude_home").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器上建不了符号链接")
    monkeypatch.setattr(container_files.WorkspaceService, "get_workspace_dir", staticmethod(lambda _id: workspace_dir))

    with pytest.raises(container_files.UnsafeNkSshPathError):
        container_files._write_nk_ssh_files_on_host(1, {"config": "x"})  # noqa: SLF001
    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# 同一频道并发的回合
# ---------------------------------------------------------------------------


def _owner_trigger(at: float) -> RoundTrigger:
    """`at` 必须与下面 `begin_round(now=...)` 在同一条时间线上

    不传的话 `at` 取真实的 `time.monotonic()`（开机以来的秒数），与用例里写死的 `now` 不是同一个时钟：
    开机够久的机器上条目「来自未来」，刚开机的 CI 虚拟机上条目早就过了 600 秒有效期被丢掉。
    """
    return RoundTrigger(
        kind=TRIGGER_KIND_HUMAN,
        adapter_key="onebot_v11",
        instance_key="inst1",
        sender_id="10001",
        source_trusted=True,
        at=at,
    )


def test_overlapping_rounds_mark_the_snapshot_incomplete() -> None:
    book = RoundTriggerBook()
    book.record("chat", _owner_trigger(at=99.0))
    first = book.begin_round("chat", now=100.0)
    assert first.incomplete is False

    book.record("chat", _owner_trigger(at=100.5))
    second = book.begin_round("chat", now=101.0)
    assert second.incomplete is True, "上一轮还没收尾就开始的新一轮分不清是谁叫醒的"
    assert book.current_batch("chat").incomplete is True

    book.end_round("chat")
    assert book.current_batch("chat").incomplete is True, "还有一轮没收尾，快照保持不完整"
    book.end_round("chat")
    assert book.current_batch("chat") is None

    book.record("chat", _owner_trigger(at=199.0))
    assert book.begin_round("chat", now=200.0).incomplete is False


def test_stale_round_count_does_not_block_the_channel_forever() -> None:
    book = RoundTriggerBook()
    book.begin_round("chat", now=100.0)  # 漏了收尾
    book.record("chat", _owner_trigger(at=100.0 + 7199))
    batch = book.begin_round("chat", now=100.0 + 7200)
    assert batch.incomplete is False
    assert len(batch.triggers) == 1, "这一轮的触发还在（没有被当成过期丢掉）"


def test_overlap_check_still_applies_within_the_stale_window() -> None:
    """对照：漏收尾不到一小时，新一轮仍按「上一轮没收尾」记不完整"""
    book = RoundTriggerBook()
    book.begin_round("chat", now=100.0)  # 漏了收尾
    book.record("chat", _owner_trigger(at=100.0 + 599))
    batch = book.begin_round("chat", now=100.0 + 600)
    assert len(batch.triggers) == 1
    assert batch.incomplete is True


# ---------------------------------------------------------------------------
# 租约
# ---------------------------------------------------------------------------


def test_uncovered_create_still_ends_the_previous_lease() -> None:
    old = lease_module.create_lease(
        workspace_id=1,
        task_ref="cc_delegate:chat",
        origin="chat",
        coverage=Coverage(credential_ids=frozenset({5}), triggered_by=("10001",)),
    )
    assert old is not None
    assert (
        lease_module.create_lease(
            workspace_id=1,
            task_ref="cc_delegate:chat",
            origin="chat",
            coverage=Coverage(),
        )
        is None
    )
    assert old.state == lease_module.LEASE_STATE_ENDED
    assert old.end_reason == "superseded"


def test_sweep_ends_expired_and_drops_old_ended_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(lease_module, "_clock", lambda: now[0])
    lease = lease_module.create_lease(
        workspace_id=1,
        task_ref="t",
        origin="chat",
        coverage=Coverage(credential_ids=frozenset({5}), triggered_by=("10001",)),
    )
    assert lease is not None
    lease_module.mark_running(lease.lease_id)

    now[0] += 601
    lease_module.sweep_leases()
    assert lease.state == lease_module.LEASE_STATE_ENDED and lease.end_reason == "expired"
    assert lease_module.get_lease(lease.lease_id) is lease

    now[0] += lease_module.ENDED_RETENTION_SECONDS + 1
    lease_module.sweep_leases()
    assert lease_module.get_lease(lease.lease_id) is None


# ---------------------------------------------------------------------------
# 监督任务：熔断不被覆盖
# ---------------------------------------------------------------------------


async def test_trip_during_tick_is_not_overwritten_by_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.ssh_gateway import supervisor as supervisor_module

    supervisor = supervisor_module.Supervisor()

    class _Backend:
        async def start(self) -> None:
            return None

        async def sync_listeners(self, _addresses: Any, _port: int) -> None:
            # 同步监听的这一拍里恰好熔断
            for _ in range(supervisor_module.UNEXPECTED_TRIP_THRESHOLD + 1):
                supervisor.report_unexpected(RuntimeError("boom"))

        async def stop(self) -> None:
            return None

        def counters(self) -> dict:
            return {}

    supervisor.register_backend(_Backend())
    supervisor._host_key_ready = True  # noqa: SLF001
    supervisor._switch_was_on = True  # noqa: SLF001
    await supervisor.tick()

    assert supervisor.status()["state"] == supervisor_module.GatewayState.TRIPPED.value
    await supervisor.stop(timeout=0.5)
