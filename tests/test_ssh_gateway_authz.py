"""SSH 网关的本轮授权判据

## 这条测试钉的是什么

- 纯函数：批次必须非空、完整、每一条都是可信人类，且每一条都满足对应名单的条件；
  混进别人、系统推送、定时器、来源不可信都一律不覆盖。
- 工作区覆盖：平台方行要过工作区判据、停用与解不开的行不覆盖、总开关关着不查库。
- 后台直发：平台方后台账号覆盖平台方行，租户账号不覆盖任何行。
- 复核：每一档拒绝理由都要能被单独触发，全部通过时返回 `None`。

环境是内存 SQLite；密钥来源是临时目录；`platform_credentials_allowed` 用打桩，
不依赖真 PG、真 Docker、真网络。
"""

from __future__ import annotations

import base64
import datetime
from pathlib import Path
from typing import Any, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.ops_agent.triggers import (
    TRIGGER_KIND_HUMAN,
    TRIGGER_KIND_SYSTEM_PUSH,
    TRIGGER_KIND_TIMER,
    RoundTrigger,
    TriggerBatch,
    round_trigger_book,
)
from nekro_agent.services.ssh_gateway import authz as authz_module
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway.authz import (
    Coverage,
    batch_all_trusted_by,
    compute_chat_coverage,
    compute_console_coverage,
    covers_platform_row,
    covers_user_row,
    recheck,
)
from nekro_agent.services.ssh_gateway.lease import (
    create_lease,
    end_lease,
    mark_running,
    reset_leases,
)
from nekro_agent.services.ssh_gateway.secrets import encrypt_secret
from nekro_agent.services.user.role import Role
from nekro_agent.tools import credential_cipher

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OWNER = "10001"
OTHER = "20002"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_7"
KEY = "ssh-authz-" + "key-A"
PASSWORD = "pw-" + "authz-1"


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
    monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", KEY)
    credential_cipher.reset_credential_cipher_cache()
    try:
        yield
    finally:
        credential_cipher.reset_credential_cipher_cache()


@pytest.fixture(autouse=True)
def clean_round_state():
    round_trigger_book.clear()
    reset_leases()
    yield
    round_trigger_book.clear()
    reset_leases()


@pytest.fixture(autouse=True)
def gateway_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
    monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", [f"{ADAPTER}:{OWNER}"])
    monkeypatch.setattr(config, "SSH_GATEWAY_USER_ACCOUNTS", [f"{ADAPTER}:{OWNER}"])
    monkeypatch.setattr(config, "SSH_GATEWAY_LEASE_MAX_SECONDS", 600)


@pytest.fixture
def platform_allowed(monkeypatch: pytest.MonkeyPatch) -> Any:
    """打桩工作区判据；记下被调用过的入参，便于断言「一次」"""

    class _Policy:
        def __init__(self) -> None:
            self.calls: List[DBWorkspace] = []
            self.allowed = True

        async def __call__(self, workspace: DBWorkspace) -> bool:
            self.calls.append(workspace)
            return self.allowed

    policy = _Policy()
    monkeypatch.setattr(authz_module, "platform_credentials_allowed", policy)
    return policy


def human(sender: str = OWNER, *, source_trusted: bool = True, adapter: str = ADAPTER, instance: str = INSTANCE) -> RoundTrigger:
    return RoundTrigger(
        kind=TRIGGER_KIND_HUMAN,
        adapter_key=adapter,
        instance_key=instance,
        sender_id=sender,
        operator_trusted=source_trusted,
        source_trusted=source_trusted,
    )


def batch(*triggers: RoundTrigger, incomplete: bool = False) -> TriggerBatch:
    return TriggerBatch(triggers=list(triggers), incomplete=incomplete)


async def make_workspace(name: str = "authz-ws") -> DBWorkspace:
    return await DBWorkspace.create(name=name, description=name, status="active")


async def make_channel(workspace: DBWorkspace, chat_key: str = CHAT_KEY) -> DBChatChannel:
    return await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_7",
        chat_key=chat_key,
        channel_type="group",
        data="{}",
        workspace_id=workspace.id,
    )


async def make_row(
    workspace: DBWorkspace,
    *,
    owner_kind: str = C.OWNER_PLATFORM,
    alias: str = "web1",
    owner_account: str = "",
    adapter_key: str = "",
    instance_key: str = "",
    status: str = C.STATUS_ACTIVE,
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
        host="ssh.example.test",
        port=22,
        username="root",
        password_enc=encrypt_secret(PASSWORD) if password_enc is None else password_enc,
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


def undecryptable() -> str:
    payload = base64.urlsafe_b64encode(b"x" * 40).decode("ascii").rstrip("=")
    return "nkenc1:" + payload


def make_user(
    username: str,
    *,
    perm_level: int = Role.Super,
    tenant_id: Optional[int] = None,
    tenant_role: str = TenantRole.PLATFORM_OWNER.value,
) -> DBUser:
    return DBUser(
        username=username,
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role,
    )


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


class TestBatchRules:
    def test_single_owner_trusted_human_passes(self) -> None:
        assert batch_all_trusted_by(batch(human()), lambda trigger: trigger.sender_id == OWNER) is True

    def test_empty_batch_never_passes(self) -> None:
        assert batch_all_trusted_by(batch(), lambda _trigger: True) is False

    def test_incomplete_batch_never_passes(self) -> None:
        assert batch_all_trusted_by(batch(human(), incomplete=True), lambda _trigger: True) is False

    def test_untrusted_source_never_passes(self) -> None:
        assert batch_all_trusted_by(batch(human(source_trusted=False)), lambda _trigger: True) is False

    def test_non_human_trigger_never_passes(self) -> None:
        push = RoundTrigger(kind=TRIGGER_KIND_SYSTEM_PUSH, sender_id=OWNER)
        assert batch_all_trusted_by(batch(push), lambda _trigger: True) is False

    def test_every_trigger_must_satisfy_the_predicate(self) -> None:
        assert batch_all_trusted_by(batch(human(OWNER), human(OTHER)), lambda trigger: trigger.sender_id == OWNER) is False


class TestCoverageTable:
    def test_platform_row_is_covered_by_owner_alone(self) -> None:
        assert covers_platform_row(batch(human()), [f"{ADAPTER}:{OWNER}"]) is True

    def test_platform_row_is_not_covered_by_owner_plus_someone_else(self) -> None:
        assert covers_platform_row(batch(human(OWNER), human(OTHER)), [f"{ADAPTER}:{OWNER}"]) is False

    def test_platform_row_is_not_covered_by_a_system_push(self) -> None:
        push = RoundTrigger(kind=TRIGGER_KIND_SYSTEM_PUSH, adapter_key=ADAPTER, instance_key=INSTANCE, sender_id=OWNER)
        assert covers_platform_row(batch(push), [f"{ADAPTER}:{OWNER}"]) is False

    def test_platform_row_is_not_covered_by_a_timer(self) -> None:
        timer = RoundTrigger(kind=TRIGGER_KIND_TIMER, adapter_key=ADAPTER, instance_key=INSTANCE, sender_id=OWNER)
        assert covers_platform_row(batch(timer), [f"{ADAPTER}:{OWNER}"]) is False

    def test_platform_row_is_not_covered_by_an_incomplete_batch(self) -> None:
        assert covers_platform_row(batch(human(), incomplete=True), [f"{ADAPTER}:{OWNER}"]) is False

    def test_platform_row_is_not_covered_when_the_source_is_untrusted(self) -> None:
        assert covers_platform_row(batch(human(source_trusted=False)), [f"{ADAPTER}:{OWNER}"]) is False

    def test_empty_owner_list_covers_nothing(self) -> None:
        assert covers_platform_row(batch(human()), []) is False

    def test_user_row_is_covered_by_the_owner_alone(self) -> None:
        assert (
            covers_user_row(
                batch(human()),
                row_adapter_key=ADAPTER,
                row_instance_key=INSTANCE,
                owner_account=OWNER,
                channel_adapter_key=ADAPTER,
                channel_instance_key=INSTANCE,
                user_entries=[f"{ADAPTER}:{OWNER}"],
            )
            is True
        )

    def test_user_row_is_not_covered_when_the_trigger_instance_differs(self) -> None:
        assert (
            covers_user_row(
                batch(human(instance="other-inst")),
                row_adapter_key=ADAPTER,
                row_instance_key=INSTANCE,
                owner_account=OWNER,
                channel_adapter_key=ADAPTER,
                channel_instance_key=INSTANCE,
                user_entries=[f"{ADAPTER}:{OWNER}"],
            )
            is False
        )

    def test_user_row_is_not_covered_when_the_channel_instance_differs(self) -> None:
        assert (
            covers_user_row(
                batch(human()),
                row_adapter_key=ADAPTER,
                row_instance_key=INSTANCE,
                owner_account=OWNER,
                channel_adapter_key=ADAPTER,
                channel_instance_key="other-inst",
                user_entries=[f"{ADAPTER}:{OWNER}"],
            )
            is False
        )

    def test_user_row_is_not_covered_when_the_owner_is_not_whitelisted(self) -> None:
        assert (
            covers_user_row(
                batch(human()),
                row_adapter_key=ADAPTER,
                row_instance_key=INSTANCE,
                owner_account=OWNER,
                channel_adapter_key=ADAPTER,
                channel_instance_key=INSTANCE,
                user_entries=[],
            )
            is False
        )

    def test_untrusted_source_covers_a_user_row_only_on_a_tenant_instance(self) -> None:
        kwargs = dict(
            row_adapter_key=ADAPTER,
            row_instance_key=INSTANCE,
            owner_account=OWNER,
            channel_adapter_key=ADAPTER,
            channel_instance_key=INSTANCE,
            user_entries=[f"{ADAPTER}:{OWNER}"],
        )
        untrusted = batch(human(source_trusted=False))
        assert covers_user_row(untrusted, **kwargs) is False
        assert covers_user_row(untrusted, tenant_instance=True, **kwargs) is True
        # 其余判据照旧：别人混进来、系统推送都不行
        assert covers_user_row(batch(human(OWNER, source_trusted=False), human(OTHER, source_trusted=False)), tenant_instance=True, **kwargs) is False
        push = RoundTrigger(kind=TRIGGER_KIND_SYSTEM_PUSH, adapter_key=ADAPTER, instance_key=INSTANCE, sender_id=OWNER)
        assert covers_user_row(batch(push), tenant_instance=True, **kwargs) is False

    def test_user_row_is_not_covered_by_someone_else(self) -> None:
        assert (
            covers_user_row(
                batch(human(OWNER), human(OTHER)),
                row_adapter_key=ADAPTER,
                row_instance_key=INSTANCE,
                owner_account=OWNER,
                channel_adapter_key=ADAPTER,
                channel_instance_key=INSTANCE,
                user_entries=[f"{ADAPTER}:{OWNER}"],
            )
            is False
        )


# ---------------------------------------------------------------------------
# 工作区覆盖
# ---------------------------------------------------------------------------


class TestChatCoverage:
    async def test_gateway_off_returns_empty_without_touching_the_database(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        platform_allowed: Any,
    ) -> None:  # noqa: ARG002
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)
        workspace = await make_workspace()
        channel = await make_channel(workspace)
        round_trigger_book.record(CHAT_KEY, human())
        round_trigger_book.begin_round(CHAT_KEY)

        class _Explode:
            @staticmethod
            def filter(**_kwargs: Any) -> Any:
                raise AssertionError("关闸时不得查库")

        monkeypatch.setattr(authz_module.DBWsSshCredential, "filter", _Explode)

        coverage = await compute_chat_coverage(workspace.id, channel)
        assert coverage == Coverage()
        assert platform_allowed.calls == []

    async def test_platform_row_is_covered_and_the_policy_is_checked_once(
        self,
        db,
        platform_allowed: Any,
    ) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        channel = await make_channel(workspace)
        first = await make_row(workspace, alias="web1")
        second = await make_row(workspace, alias="web2")
        round_trigger_book.record(CHAT_KEY, human())
        round_trigger_book.begin_round(CHAT_KEY)

        coverage = await compute_chat_coverage(workspace.id, channel)
        assert coverage.credential_ids == frozenset({first.id, second.id})
        assert coverage.triggered_by == (OWNER,)
        assert [workspace.id] == [item.id for item in platform_allowed.calls], "平台方判据一次算好给所有行共用"

    async def test_platform_row_is_not_covered_when_the_workspace_is_not_platform(
        self,
        db,
        platform_allowed: Any,
    ) -> None:
        platform_allowed.allowed = False
        workspace = await make_workspace()
        channel = await make_channel(workspace)
        await make_row(workspace)
        round_trigger_book.record(CHAT_KEY, human())
        round_trigger_book.begin_round(CHAT_KEY)

        coverage = await compute_chat_coverage(workspace.id, channel)
        assert coverage.credential_ids == frozenset()
        assert coverage.triggered_by == (OWNER,)

    async def test_inactive_rows_are_not_covered(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        channel = await make_channel(workspace)
        await make_row(workspace, alias="web1", status=C.STATUS_DISABLED)
        round_trigger_book.record(CHAT_KEY, human())
        round_trigger_book.begin_round(CHAT_KEY)

        assert (await compute_chat_coverage(workspace.id, channel)).credential_ids == frozenset()

    async def test_undecryptable_rows_are_not_covered(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        channel = await make_channel(workspace)
        await make_row(workspace, password_enc=undecryptable())
        round_trigger_book.record(CHAT_KEY, human())
        round_trigger_book.begin_round(CHAT_KEY)

        assert (await compute_chat_coverage(workspace.id, channel)).credential_ids == frozenset()

    async def test_a_user_row_needs_its_owner_and_the_same_instance(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        channel = await make_channel(workspace)
        row = await make_row(
            workspace,
            owner_kind=C.OWNER_USER,
            owner_account=OWNER,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
        )
        round_trigger_book.record(CHAT_KEY, human())
        round_trigger_book.begin_round(CHAT_KEY)

        coverage = await compute_chat_coverage(workspace.id, channel)
        assert coverage.credential_ids == frozenset({row.id})
        assert platform_allowed.calls == [], "没有平台方行时不查工作区判据"

    async def test_tenant_instance_user_row_does_not_need_a_trusted_source(
        self,
        db,
        platform_allowed: Any,
    ) -> None:  # noqa: ARG002
        """#211 ②：租户实例上的用户行不要求来源可信；平台方行在同一轮里照旧不覆盖"""
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            provider="remote_onebot",
            tenant_id=7,
            metadata_json="",
        )
        workspace = await make_workspace()
        channel = await make_channel(workspace)
        user_row = await make_row(
            workspace,
            alias="mine",
            owner_kind=C.OWNER_USER,
            owner_account=OWNER,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
        )
        await make_row(workspace, alias="platform")
        round_trigger_book.record(CHAT_KEY, human(source_trusted=False))
        round_trigger_book.begin_round(CHAT_KEY)

        coverage = await compute_chat_coverage(workspace.id, channel)
        assert coverage.credential_ids == frozenset({user_row.id})

    async def test_platform_instance_user_row_still_needs_a_trusted_source(
        self,
        db,
        platform_allowed: Any,
    ) -> None:  # noqa: ARG002
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            provider="remote_onebot",
            tenant_id=None,
            metadata_json="",
        )
        workspace = await make_workspace()
        channel = await make_channel(workspace)
        await make_row(
            workspace,
            owner_kind=C.OWNER_USER,
            owner_account=OWNER,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
        )
        round_trigger_book.record(CHAT_KEY, human(source_trusted=False))
        round_trigger_book.begin_round(CHAT_KEY)

        assert (await compute_chat_coverage(workspace.id, channel)).credential_ids == frozenset()

    async def test_a_batch_that_has_not_begun_covers_nothing(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        channel = await make_channel(workspace)
        await make_row(workspace)
        round_trigger_book.record(CHAT_KEY, human())

        assert (await compute_chat_coverage(workspace.id, channel)).credential_ids == frozenset()


class TestConsoleCoverage:
    async def test_platform_owner_covers_platform_rows_only(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        platform_row = await make_row(workspace, alias="web1")
        await make_row(
            workspace,
            alias="user-server",
            owner_kind=C.OWNER_USER,
            owner_account=OWNER,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
        )

        coverage = await compute_console_coverage(workspace.id, make_user("owner"))
        assert coverage.credential_ids == frozenset({platform_row.id})
        assert coverage.triggered_by == ()

    async def test_tenant_admin_covers_nothing(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        await make_row(workspace)

        coverage = await compute_console_coverage(
            workspace.id,
            make_user("tenant-admin", perm_level=Role.Admin, tenant_id=41, tenant_role=TenantRole.TENANT_ADMIN.value),
        )
        assert coverage == Coverage()
        assert platform_allowed.calls == []

    async def test_platform_owner_below_super_covers_nothing(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        await make_row(workspace)

        coverage = await compute_console_coverage(workspace.id, make_user("plain", perm_level=Role.Admin))
        assert coverage == Coverage()

    async def test_gateway_off_covers_nothing_without_touching_the_database(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        platform_allowed: Any,
    ) -> None:  # noqa: ARG002
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)

        class _Explode:
            @staticmethod
            def get_or_none(**_kwargs: Any) -> Any:
                raise AssertionError("关闸时不得查库")

        monkeypatch.setattr(authz_module.DBWorkspace, "get_or_none", _Explode)
        assert await compute_console_coverage(1, make_user("owner")) == Coverage()

    async def test_undecryptable_platform_rows_are_excluded(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        await make_row(workspace, password_enc=undecryptable())

        assert (await compute_console_coverage(workspace.id, make_user("owner"))).credential_ids == frozenset()


# ---------------------------------------------------------------------------
# 复核
# ---------------------------------------------------------------------------


async def make_lease(workspace: DBWorkspace, rows: List[DBWsSshCredential], **kwargs: Any) -> Any:
    coverage = Coverage(credential_ids=frozenset(row.id for row in rows), triggered_by=(OWNER,))
    lease = create_lease(
        workspace_id=workspace.id,
        task_ref=kwargs.pop("task_ref", f"cc_delegate:{CHAT_KEY}"),
        origin=kwargs.pop("origin", "chat"),
        coverage=coverage,
        chat_key=CHAT_KEY,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        **kwargs,
    )
    assert lease is not None
    mark_running(lease.lease_id)
    return lease


class TestRecheck:
    async def test_everything_passes(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = await make_lease(workspace, [row])

        assert await recheck(lease, row) is None

    async def test_gateway_off(self, db, platform_allowed: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = await make_lease(workspace, [row])
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)

        assert await recheck(lease, row) == C.REASON_GATEWAY_OFF

    @pytest.mark.parametrize("state", ["queued", "ended"])
    async def test_no_running_lease(self, db, platform_allowed: Any, state: str) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = await make_lease(workspace, [row])
        if state == "queued":
            lease.state = "queued"
        else:
            end_lease(lease.lease_id, "done")

        assert await recheck(lease, row) == C.REASON_NO_LEASE

    async def test_expired_lease(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = await make_lease(workspace, [row])
        lease.expires_at = 0.0

        assert await recheck(lease, row) == C.REASON_NO_LEASE

    async def test_row_not_in_the_lease(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        covered = await make_row(workspace, alias="web1")
        other = await make_row(workspace, alias="web2")
        lease = await make_lease(workspace, [covered])

        assert await recheck(lease, other) == C.REASON_NOT_COVERED

    async def test_inactive_row(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = await make_lease(workspace, [row])
        row.status = C.STATUS_DISABLED
        await row.save(update_fields=["status"])

        assert await recheck(lease, row) == C.REASON_ROW_INACTIVE

    async def test_undecryptable_row(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = await make_lease(workspace, [row])
        row.password_enc = undecryptable()
        await row.save(update_fields=["password_enc"])

        assert await recheck(lease, row) == C.REASON_UNDECRYPTABLE

    async def test_workspace_is_no_longer_platform(self, db, platform_allowed: Any) -> None:
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = await make_lease(workspace, [row])
        platform_allowed.allowed = False

        assert await recheck(lease, row) == C.REASON_WORKSPACE_NOT_PLATFORM

    async def test_chat_owner_was_removed_from_the_list(
        self,
        db,
        platform_allowed: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = await make_lease(workspace, [row])
        monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", [])

        assert await recheck(lease, row) == C.REASON_OWNER_CHANGED

    async def test_chat_lease_without_a_triggering_account_is_rejected(
        self,
        db,
        platform_allowed: Any,
    ) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = await make_lease(workspace, [row])
        lease.triggered_by = ()

        assert await recheck(lease, row) == C.REASON_OWNER_CHANGED

    async def test_console_actor_was_downgraded(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace)
        actor = await DBUser.create(
            username="console-admin",
            password="",
            adapter_key="webui",
            platform_userid="0",
            perm_level=Role.Super,
            login_time=datetime.datetime.now(datetime.timezone.utc),
            tenant_id=None,
            tenant_role=TenantRole.PLATFORM_OWNER.value,
        )
        lease = await make_lease(workspace, [row], origin="console", actor=actor.username)

        assert await recheck(lease, row) is None
        actor.perm_level = Role.Admin
        await actor.save(update_fields=["perm_level"])
        assert await recheck(lease, row) == C.REASON_OWNER_CHANGED

    async def test_user_owner_was_removed_from_the_whitelist(
        self,
        db,
        platform_allowed: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            enabled=True,
            provider="remote_onebot",
        )
        row = await make_row(
            workspace,
            owner_kind=C.OWNER_USER,
            owner_account=OWNER,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
        )
        lease = await make_lease(workspace, [row])
        assert await recheck(lease, row) is None

        monkeypatch.setattr(config, "SSH_GATEWAY_USER_ACCOUNTS", [])
        assert await recheck(lease, row) == C.REASON_OWNER_CHANGED

    async def test_user_row_whose_instance_was_deleted_is_refused(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        """实例删了、行还留着：`is_instance_active` 对不存在的实例放行，复核不能跟着放行"""
        workspace = await make_workspace()
        row = await make_row(
            workspace,
            owner_kind=C.OWNER_USER,
            owner_account=OWNER,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
        )
        lease = await make_lease(workspace, [row])

        assert await recheck(lease, row) == C.REASON_OWNER_CHANGED

    async def test_user_instance_was_disabled(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            display_name=INSTANCE,
            status="online",
            enabled=True,
            provider="remote_onebot",
        )
        row = await make_row(
            workspace,
            owner_kind=C.OWNER_USER,
            owner_account=OWNER,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
        )
        lease = await make_lease(workspace, [row])
        assert await recheck(lease, row) is None

        await DBAdapterInstance.filter(adapter_key=ADAPTER, instance_key=INSTANCE).update(enabled=False)
        assert await recheck(lease, row) == C.REASON_OWNER_CHANGED

    async def test_unknown_owner_kind_is_not_covered(self, db, platform_allowed: Any) -> None:  # noqa: ARG002
        workspace = await make_workspace()
        row = await make_row(workspace)
        lease = await make_lease(workspace, [row])
        row.owner_kind = "unknown"

        assert await recheck(lease, row) == C.REASON_NOT_COVERED
