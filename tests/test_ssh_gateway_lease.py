"""SSH 授权租约：拍下、生效、结束、过期、顶替与审计

## 这条测试钉的是什么

租约是「跟着 CC 任务走」的授权，它的状态机只有三格，但每一格都不能省：`queued` 时
容器里跑的是别人的任务，提前生效等于把服务器借给排队的人；`running` 只认唯一一份，
多一份说明状态机出错，只能拒；结束必须收回并通知订阅者，漏一条就留下一个活着的授权。

环境是内存 SQLite；时钟用单调时钟并在这里注入，过期不靠真等；没有任何网络。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_ws_ssh_audit import DBWsSshAudit
from nekro_agent.services.ssh_gateway import constants as C
from nekro_agent.services.ssh_gateway import lease as lease_module
from nekro_agent.services.ssh_gateway.authz import Coverage
from nekro_agent.services.ssh_gateway.lease import (
    LEASE_STATE_ENDED,
    LEASE_STATE_QUEUED,
    LEASE_STATE_RUNNING,
    SshLease,
    create_lease,
    end_lease,
    find_lease,
    leases_for_workspace,
    mark_running,
    on_lease_end,
    reset_leases,
    running_lease,
)

WORKSPACE = 7
OTHER_WORKSPACE = 8
TASK_REF = "cc_delegate:onebot_v11-inst1-group_7"


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def clean_leases(monkeypatch: pytest.MonkeyPatch):
    reset_leases()
    monkeypatch.setattr(config, "SSH_GATEWAY_LEASE_MAX_SECONDS", 60)
    yield
    reset_leases()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> List[float]:
    now = [1_000.0]
    monkeypatch.setattr(lease_module, "_clock", lambda: now[0])
    return now


@pytest.fixture
def log_messages():
    from loguru import logger

    captured: List[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="DEBUG")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


async def flush_audits() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def coverage(*ids: int, triggered_by: tuple[str, ...] = ("10001",)) -> Coverage:
    return Coverage(credential_ids=frozenset(ids), triggered_by=triggered_by)


def new_lease(*, workspace_id: int = WORKSPACE, task_ref: str = TASK_REF, ids: tuple[int, ...] = (1,)) -> Optional[SshLease]:
    return create_lease(
        workspace_id=workspace_id,
        task_ref=task_ref,
        origin="chat",
        coverage=coverage(*ids),
        chat_key="onebot_v11-inst1-group_7",
        adapter_key="onebot_v11",
        instance_key="inst1",
    )


async def audit_rows() -> List[DBWsSshAudit]:
    await flush_audits()
    return list(await DBWsSshAudit.all().order_by("id"))


class TestLifecycle:
    async def test_create_queued_run_then_end(self, db, clock: List[float]) -> None:  # noqa: ARG002
        lease = new_lease()
        assert lease is not None
        assert lease.state == LEASE_STATE_QUEUED
        assert lease.credential_ids == frozenset({1})
        assert lease.triggered_by == ("10001",)
        assert lease.expires_at == clock[0] + 60
        assert find_lease(WORKSPACE, TASK_REF, state=LEASE_STATE_QUEUED) is lease

        mark_running(lease.lease_id)
        assert lease.state == LEASE_STATE_RUNNING
        assert lease.started_at == clock[0]
        assert running_lease(WORKSPACE) is lease

        end_lease(lease.lease_id, "done")
        assert lease.state == LEASE_STATE_ENDED
        assert lease.end_reason == "done"
        assert running_lease(WORKSPACE) is None
        assert find_lease(WORKSPACE, TASK_REF, state=LEASE_STATE_RUNNING) is None

    async def test_an_empty_coverage_creates_nothing(self, db) -> None:  # noqa: ARG002
        assert new_lease(ids=()) is None
        assert leases_for_workspace(WORKSPACE) == ()
        assert await audit_rows() == []

    async def test_ending_twice_is_idempotent(self, db) -> None:  # noqa: ARG002
        lease = new_lease()
        assert lease is not None
        end_lease(lease.lease_id, "done")
        end_lease(lease.lease_id, "done")

        rows = await audit_rows()
        assert [row.detail for row in rows] == ["create", "end:done"]

    async def test_find_lease_is_scoped_by_workspace_and_state(self, db) -> None:  # noqa: ARG002
        lease = new_lease()
        assert lease is not None
        assert find_lease(OTHER_WORKSPACE, TASK_REF) is None
        assert find_lease(WORKSPACE, "cc_delegate:other") is None
        assert find_lease(WORKSPACE, TASK_REF, state=LEASE_STATE_RUNNING) is None
        assert find_lease(WORKSPACE, TASK_REF, state=LEASE_STATE_QUEUED) is lease


class TestSupersede:
    async def test_a_new_lease_for_the_same_task_ref_ends_the_old_one(self, db) -> None:  # noqa: ARG002
        first = new_lease(ids=(1,))
        assert first is not None
        mark_running(first.lease_id)

        second = new_lease(ids=(2,))
        assert second is not None and second.lease_id != first.lease_id
        assert first.state == LEASE_STATE_ENDED
        assert first.end_reason == "superseded"
        assert second.state == LEASE_STATE_QUEUED
        assert find_lease(WORKSPACE, TASK_REF, state=LEASE_STATE_QUEUED) is second

    async def test_different_task_refs_do_not_supersede_each_other(self, db) -> None:  # noqa: ARG002
        first = new_lease(task_ref="cc_delegate:chat-a")
        second = new_lease(task_ref="cc_delegate:chat-b")
        assert first is not None and second is not None
        assert first.state == LEASE_STATE_QUEUED
        assert second.state == LEASE_STATE_QUEUED


class TestRunningLease:
    async def test_expired_running_lease_is_ended_and_not_returned(self, db, clock: List[float]) -> None:  # noqa: ARG002
        lease = new_lease()
        assert lease is not None
        mark_running(lease.lease_id)

        clock[0] = lease.expires_at + 1
        assert running_lease(WORKSPACE) is None
        assert lease.state == LEASE_STATE_ENDED
        assert lease.end_reason == "expired"
        rows = await audit_rows()
        assert [row.detail for row in rows] == ["create", "start", "end:expired"]

    async def test_two_running_leases_are_rejected_with_an_error(self, db, log_messages: List[str]) -> None:  # noqa: ARG002
        first = new_lease(task_ref="cc_delegate:chat-a")
        second = new_lease(task_ref="cc_delegate:chat-b")
        assert first is not None and second is not None
        mark_running(first.lease_id)
        mark_running(second.lease_id)

        assert running_lease(WORKSPACE) is None
        assert any("状态机异常" in message for message in log_messages), log_messages

    async def test_a_queued_lease_is_not_running(self, db) -> None:  # noqa: ARG002
        assert new_lease() is not None
        assert running_lease(WORKSPACE) is None

    async def test_expired_queued_lease_is_ended_and_not_returned(self, db, clock: List[float]) -> None:  # noqa: ARG002
        lease = new_lease()
        assert lease is not None
        clock[0] = lease.expires_at + 1

        assert running_lease(WORKSPACE) is None
        assert lease.state == LEASE_STATE_ENDED
        assert lease.end_reason == "expired"

    async def test_an_expired_queued_lease_cannot_be_started(self, db, clock: List[float]) -> None:  # noqa: ARG002
        lease = new_lease()
        assert lease is not None
        clock[0] = lease.expires_at + 1

        mark_running(lease.lease_id)
        assert lease.state == LEASE_STATE_ENDED
        assert lease.end_reason == "expired"
        rows = await audit_rows()
        assert [row.detail for row in rows] == ["create", "end:expired"]


class TestEndCallbacks:
    async def test_every_callback_is_called_even_if_one_raises(self, db, log_messages: List[str]) -> None:  # noqa: ARG002
        seen: List[str] = []

        def first(lease: SshLease) -> None:
            seen.append("first")

        def broken(_lease: SshLease) -> None:
            raise RuntimeError("callback boom")

        def last(lease: SshLease) -> None:
            seen.append("last")

        on_lease_end(first)
        on_lease_end(broken)
        on_lease_end(last)

        lease = new_lease()
        assert lease is not None
        end_lease(lease.lease_id, "done")

        assert seen == ["first", "last"], "一个回调抛错不能挡住后面的回调"
        assert any("租约结束回调失败" in message for message in log_messages), log_messages

    async def test_reset_clears_subscribers(self, db) -> None:  # noqa: ARG002
        seen: List[str] = []
        on_lease_end(lambda _lease: seen.append("called"))
        reset_leases()

        lease = new_lease()
        assert lease is not None
        end_lease(lease.lease_id, "done")
        assert seen == []


class TestAudit:
    async def test_every_step_writes_a_row_with_the_lease_fields(self, db) -> None:  # noqa: ARG002
        lease = new_lease(ids=(3,), task_ref="cc_delegate:chat-a")
        assert lease is not None
        mark_running(lease.lease_id)
        end_lease(lease.lease_id, "failed")

        rows = await audit_rows()
        assert [row.detail for row in rows] == ["create", "start", "end:failed"]
        for row in rows:
            assert row.event == C.EVENT_LEASE
            assert row.workspace_id == WORKSPACE
            assert row.lease_id == lease.lease_id
            assert row.task_ref == "cc_delegate:chat-a"
            assert row.triggered_by == ["10001"]
            assert row.actor == ""
            assert (row.owner_kind, row.owner_account, row.adapter_key, row.instance_key) == ("", "", "", "")
            assert row.allowed is True

    async def test_console_lease_writes_the_actor(self, db) -> None:  # noqa: ARG002
        lease = create_lease(
            workspace_id=WORKSPACE,
            task_ref="__user__",
            origin="console",
            coverage=coverage(4, triggered_by=()),
            actor="console-admin",
        )
        assert lease is not None
        rows = await audit_rows()
        assert rows[-1].actor == "console-admin"
        assert rows[-1].triggered_by == []

    async def test_an_audit_failure_does_not_break_the_lease(
        self,
        db,
        monkeypatch: pytest.MonkeyPatch,
        log_messages: List[str],
    ) -> None:  # noqa: ARG002
        async def broken_create(**_kwargs: Any) -> None:
            raise RuntimeError("audit db down")

        monkeypatch.setattr(DBWsSshAudit, "create", broken_create)

        lease = new_lease()
        assert lease is not None
        mark_running(lease.lease_id)
        end_lease(lease.lease_id, "done")

        await flush_audits()
        assert lease.state == LEASE_STATE_ENDED
        assert any("写租约审计失败" in message for message in log_messages), log_messages


def test_expiry_uses_the_monotonic_clock() -> None:
    """租约的到期时刻只用单调时钟，系统时间被调整不会提前或延后收回"""
    lease = SshLease(
        lease_id="x",
        workspace_id=1,
        task_ref="__user__",
        origin="console",
        credential_ids=frozenset({1}),
        expires_at=time.monotonic() + 10,
    )
    assert lease_module.lease_expired(lease) is False
