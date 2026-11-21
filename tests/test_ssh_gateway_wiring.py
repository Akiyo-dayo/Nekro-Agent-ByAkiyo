"""两个派发入口接上授权租约

## 这条测试钉的是什么

插件委托与后台直发是仅有的两个「叫醒 CC」的入口。租约必须在这里拍、在流的第一条
正文到达时生效、在任务收尾时收回；总开关关着时则一次库都不查。这些接线的共同失效
方式很隐蔽：任务照常跑，只是服务器连不上，或者更糟——上一个任务的授权还留着。

环境是内存 SQLite；沙盒客户端与任务 API 全部打桩，不碰 Docker、不碰网络。
"""

from __future__ import annotations

import asyncio
import datetime
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.models.db_ws_ssh_credential import DBWsSshCredential
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.schemas.workspace import CommSendBody
from nekro_agent.services import message_service as message_module
from nekro_agent.services.ops_agent.triggers import (
    TRIGGER_KIND_HUMAN,
    RoundTrigger,
    round_trigger_book,
)
from nekro_agent.services.plugin.task import AsyncTaskHandle
from nekro_agent.services.ssh_gateway import authz as authz_module
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway.authz import Coverage
from nekro_agent.services.ssh_gateway.lease import (
    LEASE_STATE_QUEUED,
    LEASE_STATE_RUNNING,
    create_lease,
    find_lease,
    leases_for_workspace,
    reset_leases,
)
from nekro_agent.services.ssh_gateway.secrets import encrypt_secret
from nekro_agent.services.user.role import Role
from nekro_agent.tools import credential_cipher

_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins"
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

cc = importlib.import_module("builtin.cc_workspace.main")

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_7"
OWNER = "10001"
KEY = "ssh-wiring-" + "key-A"
PASSWORD = "pw-" + "wiring-1"
TASK_REF = f"cc_delegate:{CHAT_KEY}"


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
def clean_state():
    round_trigger_book.clear()
    reset_leases()
    yield
    round_trigger_book.clear()
    reset_leases()
    for key in (CHAT_KEY,):
        message_module.message_service.pending_messages.pop(key, None)
        message_module.message_service.debounce_timers.pop(key, None)


@pytest.fixture(autouse=True)
def gateway_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", True)
    monkeypatch.setattr(config, "SSH_GATEWAY_OWNER_ACCOUNTS", [f"{ADAPTER}:{OWNER}"])
    monkeypatch.setattr(config, "SSH_GATEWAY_USER_ACCOUNTS", [f"{ADAPTER}:{OWNER}"])
    monkeypatch.setattr(config, "SSH_GATEWAY_LEASE_MAX_SECONDS", 600)


@pytest.fixture
def platform_allowed(monkeypatch: pytest.MonkeyPatch) -> Any:
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


async def make_workspace_and_row() -> tuple[DBWorkspace, DBWsSshCredential]:
    workspace = await DBWorkspace.create(name="wiring-ws", description="wiring-ws", status="active")
    await DBChatChannel.create(
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        channel_id="group_7",
        chat_key=CHAT_KEY,
        channel_type=ChatType.GROUP.value,
        data="{}",
        workspace_id=workspace.id,
    )
    row = await DBWsSshCredential.create(
        workspace_id=workspace.id,
        owner_kind=C.OWNER_PLATFORM,
        owner_account="",
        adapter_key="",
        instance_key="",
        created_via=C.CREATED_VIA_ADMIN,
        import_ref="",
        alias="web1",
        host="ssh.example.test",
        port=22,
        username="root",
        password_enc=encrypt_secret(PASSWORD),
        private_key_enc="",
        passphrase_enc="",
        host_keys=[],
        status=C.STATUS_ACTIVE,
        status_reason="",
        note="",
        created_by="tester",
        updated_by="tester",
        last_error="",
    )
    return workspace, row


def begin_owner_round(sender: str = OWNER) -> None:
    round_trigger_book.record(
        CHAT_KEY,
        RoundTrigger(
            kind=TRIGGER_KIND_HUMAN,
            adapter_key=ADAPTER,
            instance_key=INSTANCE,
            sender_id=sender,
            operator_trusted=True,
            source_trusted=True,
        ),
    )
    round_trigger_book.begin_round(CHAT_KEY)


def make_user(
    username: str = "owner",
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


async def quiet(*_args: Any, **_kwargs: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# 插件委托
# ---------------------------------------------------------------------------


@pytest.fixture
def delegate(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    captured: Dict[str, Any] = {"started": [], "raise": None}

    async def bound(_self: Any) -> Any:
        return captured["workspace"]

    async def start(*_args: Any, **_kwargs: Any) -> None:
        captured["started"].append(_kwargs)
        if captured["raise"] is not None:
            raise captured["raise"]

    monkeypatch.setattr(AgentCtx, "get_bound_workspace", bound)
    monkeypatch.setattr(cc.task_api, "is_running", lambda *_args: False)
    monkeypatch.setattr(cc.task_api, "start", start)
    monkeypatch.setattr(cc.WorkspaceService, "read_na_context", staticmethod(lambda _workspace_id: ("", None)))
    return captured


class TestDelegateToCc:
    async def test_owner_round_creates_a_queued_lease(self, db, platform_allowed: Any, delegate: Dict[str, Any]) -> None:  # noqa: ARG002
        workspace, row = await make_workspace_and_row()
        delegate["workspace"] = workspace
        begin_owner_round()

        await cc.delegate_to_cc(AgentCtx(from_chat_key=CHAT_KEY), "做点事")

        lease = find_lease(workspace.id, TASK_REF)
        assert lease is not None
        assert lease.state == LEASE_STATE_QUEUED
        assert lease.credential_ids == frozenset({row.id})
        assert lease.triggered_by == (OWNER,)
        assert delegate["started"], "任务照样要提交"
        assert delegate["started"][-1].get("ssh_lease_id") == lease.lease_id, "租约 id 显式交给任务"

    async def test_uncovered_delegation_ends_the_previous_unfinished_lease(
        self,
        db,
        platform_allowed: Any,
        delegate: Dict[str, Any],
    ) -> None:  # noqa: ARG002
        """上一次委托留下一份没结束的租约，这一次没拍到租约：旧的必须结束，任务也不带租约 id"""
        workspace, _row = await make_workspace_and_row()
        delegate["workspace"] = workspace
        begin_owner_round()
        await cc.delegate_to_cc(AgentCtx(from_chat_key=CHAT_KEY), "第一次")
        stale = find_lease(workspace.id, TASK_REF)
        assert stale is not None and stale.state == LEASE_STATE_QUEUED

        round_trigger_book.clear()
        begin_owner_round(sender="20002")
        await cc.delegate_to_cc(AgentCtx(from_chat_key=CHAT_KEY), "第二次")

        assert stale.state == "ended"
        assert "ssh_lease_id" not in delegate["started"][-1]

    async def test_gateway_off_creates_no_lease_and_queries_nothing(
        self,
        db,
        platform_allowed: Any,
        delegate: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        workspace, _row = await make_workspace_and_row()
        delegate["workspace"] = workspace
        monkeypatch.setattr(config, "SSH_GATEWAY_ENABLED", False)

        async def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("关闸时不得查覆盖")

        monkeypatch.setattr(authz_module, "compute_chat_coverage", explode)

        await cc.delegate_to_cc(AgentCtx(from_chat_key=CHAT_KEY), "做点事")
        assert leases_for_workspace(workspace.id) == ()

    async def test_start_failure_ends_the_lease(
        self,
        db,
        platform_allowed: Any,
        delegate: Dict[str, Any],
    ) -> None:  # noqa: ARG002
        workspace, _row = await make_workspace_and_row()
        delegate["workspace"] = workspace
        delegate["raise"] = ValueError("cannot start")
        begin_owner_round()

        with pytest.raises(ValueError):
            await cc.delegate_to_cc(AgentCtx(from_chat_key=CHAT_KEY), "做点事")

        lease = find_lease(workspace.id, TASK_REF)
        assert lease is not None
        assert lease.state == "ended"
        assert lease.end_reason == "start_failed"


# ---------------------------------------------------------------------------
# 插件任务流
# ---------------------------------------------------------------------------


class _StreamClient:
    """按模式产出排队事件与正文；在正文之后记录租约状态"""

    states: List[str] = []
    task_ref = TASK_REF
    workspace_id = 7
    mode = "success"

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def stream_message(self, *_args: Any, on_queued: Any = None, **_kwargs: Any) -> Any:
        if on_queued is not None:
            await on_queued({"type": "queued", "queue_length": 1})
        if self.mode == "fail":
            from nekro_agent.services.workspace.client import CCSandboxError

            raise CCSandboxError("boom")
        yield "hello"
        if self.task_ref == "*":
            # 后台直发每次一个独立的任务标识：取这个工作区最新的一份
            found = leases_for_workspace(self.workspace_id)
            current = found[-1] if found else None
        else:
            current = find_lease(self.workspace_id, self.task_ref)
        self.states.append(current.state if current is not None else "missing")


def _install_task_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def prepare(_workspace: Any) -> Dict[str, str]:
        return {}

    monkeypatch.setattr("nekro_agent.services.workspace.credential_policy.prepare_cc_dispatch", prepare)
    monkeypatch.setattr(cc, "CCSandboxClient", _StreamClient)
    monkeypatch.setattr(cc, "publish_system_event", quiet)
    monkeypatch.setattr(cc, "_broadcast_cc_status", quiet)
    monkeypatch.setattr(cc.comm_broadcast, "publish", quiet)


class TestDelegateTaskStream:
    @pytest.fixture
    def lease_and_handle(self):
        async def build(mode: str) -> tuple[Any, AsyncTaskHandle, int]:
            workspace = await DBWorkspace.create(name=f"stream-{mode}", description="stream", status="active")
            row = await DBWsSshCredential.create(
                workspace_id=workspace.id,
                owner_kind=C.OWNER_PLATFORM,
                owner_account="",
                adapter_key="",
                instance_key="",
                created_via=C.CREATED_VIA_ADMIN,
                import_ref="",
                alias="web1",
                host="ssh.example.test",
                port=22,
                username="root",
                password_enc=encrypt_secret(PASSWORD),
                private_key_enc="",
                passphrase_enc="",
                host_keys=[],
                status=C.STATUS_ACTIVE,
                status_reason="",
                note="",
                created_by="tester",
                updated_by="tester",
                last_error="",
            )
            lease = create_lease(
                workspace_id=workspace.id,
                task_ref=TASK_REF,
                origin="chat",
                coverage=Coverage(credential_ids=frozenset({row.id}), triggered_by=(OWNER,)),
                chat_key=CHAT_KEY,
                adapter_key=ADAPTER,
                instance_key=INSTANCE,
            )
            assert lease is not None
            handle = AsyncTaskHandle(task_id=CHAT_KEY, chat_key=CHAT_KEY, plugin=SimpleNamespace())
            return workspace, handle, lease.lease_id

        return build

    @pytest.mark.parametrize("mode", ["success", "fail", "cancel"])
    async def test_terminal_states_end_the_lease(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        lease_and_handle: Any,
        mode: str,
    ) -> None:  # noqa: ARG002
        workspace, handle, lease_id = await lease_and_handle(mode)
        _StreamClient.mode = "fail" if mode == "fail" else "success"
        _StreamClient.states = []
        _StreamClient.task_ref = TASK_REF
        _StreamClient.workspace_id = workspace.id
        if mode == "cancel":
            handle.cancel_all()
        _install_task_stubs(monkeypatch)

        # 生产里的任务调度拿到终态就退出循环、不再把生成器跑到 `finally`：
        # 这里也在第一个终态处停下，租约必须在交出终态之前就已经结束、原因正确
        generator = cc._cc_delegate_task(handle, workspace.id, "做点事", ssh_lease_id=lease_id)  # noqa: SLF001
        ctls = []
        async for ctl in generator:
            ctls.append(ctl)
            if ctl.signal.value in {"success", "fail", "cancel"}:
                break

        lease = find_lease(workspace.id, TASK_REF)
        assert lease is not None
        assert lease.state == "ended"
        expected = {"success": "done", "fail": "failed", "cancel": "cancelled"}[mode]
        assert lease.end_reason == expected
        if mode == "success":
            assert _StreamClient.states == [LEASE_STATE_RUNNING], "第一条正文到达时租约才生效"
        assert ctls, "任务照样把终态交给上层"
        await generator.aclose()

    async def test_task_without_lease_id_does_not_claim_a_leftover_lease(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        lease_and_handle: Any,
    ) -> None:  # noqa: ARG002
        """同一频道留着一份没结束的租约，这一次委托没拍到租约：任务不能认领它"""
        workspace, handle, lease_id = await lease_and_handle("success")
        _StreamClient.mode = "success"
        _StreamClient.states = []
        _StreamClient.task_ref = TASK_REF
        _StreamClient.workspace_id = workspace.id
        _install_task_stubs(monkeypatch)

        [ctl async for ctl in cc._cc_delegate_task(handle, workspace.id, "做点事")]  # noqa: SLF001

        leftover = find_lease(workspace.id, TASK_REF)
        assert leftover is not None and leftover.lease_id == lease_id
        assert leftover.state == LEASE_STATE_QUEUED, "没把 id 交过来的任务不让它生效"

    async def test_queued_state_is_not_running_before_the_first_chunk(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        lease_and_handle: Any,
    ) -> None:  # noqa: ARG002
        workspace, handle, lease_id = await lease_and_handle("success")
        seen: List[str] = []

        class _QueuedClient(_StreamClient):
            async def stream_message(self, *_args: Any, on_queued: Any = None, **_kwargs: Any) -> Any:
                if on_queued is not None:
                    await on_queued({"type": "queued", "queue_length": 1})
                current = find_lease(workspace.id, TASK_REF)
                seen.append(current.state if current is not None else "missing")
                yield "hello"

        _StreamClient.mode = "success"
        _install_task_stubs(monkeypatch)
        monkeypatch.setattr(cc, "CCSandboxClient", _QueuedClient)

        [ctl async for ctl in cc._cc_delegate_task(handle, workspace.id, "做点事", ssh_lease_id=lease_id)]  # noqa: SLF001

        assert seen == [LEASE_STATE_QUEUED], "排队事件本身不能让租约生效"


# ---------------------------------------------------------------------------
# 后台直发
# ---------------------------------------------------------------------------


@pytest.fixture
def direct_send(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    from nekro_agent.routers import workspaces as router

    captured: Dict[str, Any] = {"tasks": []}

    async def accessible(_user: Any, _workspace_id: int) -> Any:
        return captured["workspace"]

    async def prepare(_workspace: Any) -> Dict[str, str]:
        return {}

    class _Envelope:
        def to_prompt(self) -> str:
            return "prompt"

        def to_metadata_json(self) -> str:
            return "{}"

    async def handshake(_workspace_id: int, _content: str) -> Any:
        return _Envelope()

    async def persist(**_kwargs: Any) -> None:
        return None

    def capture(coro: Any) -> Any:
        task = asyncio.ensure_future(coro)
        captured["tasks"].append(task)
        return task

    from nekro_agent.services.workspace import comm_broadcast as comm_module

    monkeypatch.setattr(router, "assert_workspace_accessible", accessible)
    monkeypatch.setattr("nekro_agent.services.workspace.credential_policy.prepare_cc_dispatch", prepare)
    monkeypatch.setattr(router, "_build_cc_memory_handshake", handshake)
    monkeypatch.setattr(router, "persist_cc_task_memory", persist)
    monkeypatch.setattr(router, "publish_system_event", quiet)
    monkeypatch.setattr(comm_module, "publish", quiet)
    # 只替换模块里的 `create_task`：`router.asyncio` 就是 asyncio 模块本身，
    # 换掉整个模块会让 `_cc_task` 里的其它 `asyncio.*` 一起失效
    monkeypatch.setattr(asyncio, "create_task", capture)
    return captured


class TestConsoleSend:
    async def test_platform_owner_creates_a_lease_that_lives_until_the_task_ends(
        self,
        db,
        platform_allowed: Any,
        direct_send: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        from nekro_agent.routers import workspaces as router

        workspace, row = await make_workspace_and_row()
        direct_send["workspace"] = workspace
        _StreamClient.mode = "success"
        _StreamClient.states = []
        _StreamClient.task_ref = "*"
        _StreamClient.workspace_id = workspace.id
        monkeypatch.setattr(router, "CCSandboxClient", _StreamClient)

        result = await router.user_send_to_cc(workspace.id, CommSendBody(content="hi"), _current_user=make_user())
        assert result == {"ok": True}

        leases = leases_for_workspace(workspace.id)
        assert len(leases) == 1
        lease = leases[0]
        assert lease.task_ref.startswith("__user__:")
        assert lease.state == LEASE_STATE_QUEUED
        assert lease.credential_ids == frozenset({row.id})
        assert lease.triggered_by == ()
        assert lease.actor == "owner"

        await asyncio.gather(*direct_send["tasks"])
        assert lease.state == "ended"
        assert lease.end_reason == "done"
        assert _StreamClient.states == [LEASE_STATE_RUNNING]

    async def test_second_console_send_does_not_end_the_first_lease(
        self,
        db,
        platform_allowed: Any,
        direct_send: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        """后台直发每次一个独立的任务标识：第二次直发不能把第一次正在用的授权顶掉"""
        from nekro_agent.routers import workspaces as router

        workspace, _row = await make_workspace_and_row()
        direct_send["workspace"] = workspace
        _StreamClient.mode = "success"
        _StreamClient.task_ref = "*"
        _StreamClient.workspace_id = workspace.id
        monkeypatch.setattr(router, "CCSandboxClient", _StreamClient)

        await router.user_send_to_cc(workspace.id, CommSendBody(content="one"), _current_user=make_user())
        await router.user_send_to_cc(workspace.id, CommSendBody(content="two"), _current_user=make_user())

        first, second = leases_for_workspace(workspace.id)
        assert first.task_ref != second.task_ref
        assert first.end_reason != "superseded", "第一次的租约不能被第二次顶掉"
        await asyncio.gather(*direct_send["tasks"])
        assert first.end_reason == "done" and second.end_reason == "done"

    async def test_tenant_admin_creates_no_lease(
        self,
        db,
        platform_allowed: Any,
        direct_send: Dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # noqa: ARG002
        from nekro_agent.routers import workspaces as router

        workspace, _row = await make_workspace_and_row()
        direct_send["workspace"] = workspace
        _StreamClient.mode = "success"
        _StreamClient.task_ref = "__user__"
        _StreamClient.workspace_id = workspace.id
        monkeypatch.setattr(router, "CCSandboxClient", _StreamClient)

        tenant_admin = make_user(
            "tenant-admin",
            perm_level=Role.Admin,
            tenant_id=41,
            tenant_role=TenantRole.TENANT_ADMIN.value,
        )
        await router.user_send_to_cc(workspace.id, CommSendBody(content="hi"), _current_user=tenant_admin)

        assert leases_for_workspace(workspace.id) == ()
        await asyncio.gather(*direct_send["tasks"])
