"""运行中开闸也会补跑存量 payload 回填（看护器的触发条件与退避）。

闸关时看护器什么都不做；闸打开后不管是通过哪个入口改的配置，轮询都能发现并在后台跑一次。
回填本身幂等，所以每个进程成功跑一次之后就不再重复。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.payload_backfill import PayloadBackfillWatcher

POLL_SECONDS = 0.01


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """最多等 2 秒、每 10ms 看一次；不靠长 sleep 卡时间"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待条件在超时时间内没有成立")


async def _settle(rounds: int = 5) -> None:
    """给轮询任务几轮机会，用来验证「不该触发」的时刻确实没有触发"""
    for _ in range(rounds):
        await asyncio.sleep(POLL_SECONDS * 2)


@pytest.fixture
def backfill_counter(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """把回填替换成只计数的假实现"""
    calls: list[int] = []

    async def fake_backfill(*_args: Any, **_kwargs: Any) -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(
        qdrant_manager_module.memory_qdrant_manager,
        "backfill_paragraph_payloads",
        fake_backfill,
    )
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    return calls


class TestWatcher:
    async def test_gate_opened_at_runtime_triggers_exactly_once(
        self,
        backfill_counter: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        watcher = PayloadBackfillWatcher(poll_seconds=POLL_SECONDS)
        try:
            watcher.start()
            await _settle()
            assert backfill_counter == []

            monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
            await _wait_until(lambda: len(backfill_counter) == 1)
            await _settle()
            assert len(backfill_counter) == 1

            # 关闸再开闸：本进程已经跑完，不再重复
            monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
            await _settle()
            monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
            await _settle()
            assert len(backfill_counter) == 1
        finally:
            await watcher.stop()

    async def test_gate_open_at_start_runs_immediately_once(
        self,
        backfill_counter: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        watcher = PayloadBackfillWatcher(poll_seconds=POLL_SECONDS)
        try:
            watcher.start()
            await _wait_until(lambda: len(backfill_counter) == 1)
            await _settle()
            assert len(backfill_counter) == 1
        finally:
            await watcher.stop()

    async def test_failure_backs_off_then_retries_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        attempts: list[float] = []

        async def flaky(*_args: Any, **_kwargs: Any) -> int:
            attempts.append(asyncio.get_running_loop().time())
            if len(attempts) == 1:
                raise RuntimeError("qdrant 暂时不可用")
            return 0

        monkeypatch.setattr(
            qdrant_manager_module.memory_qdrant_manager,
            "backfill_paragraph_payloads",
            flaky,
        )
        watcher = PayloadBackfillWatcher(poll_seconds=POLL_SECONDS, retry_after_failure_seconds=0.05)
        try:
            watcher.start()
            await _wait_until(lambda: len(attempts) == 1)
            # 失败窗口内不重试
            await asyncio.sleep(POLL_SECONDS)
            assert len(attempts) == 1

            await _wait_until(lambda: len(attempts) == 2)
            assert attempts[1] - attempts[0] >= 0.05
            await _settle()
            assert len(attempts) == 2
        finally:
            await watcher.stop()

    async def test_master_switch_off_never_runs(
        self,
        backfill_counter: list[int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", False)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        watcher = PayloadBackfillWatcher(poll_seconds=POLL_SECONDS)
        try:
            watcher.start()
            await _settle()
            assert backfill_counter == []
        finally:
            await watcher.stop()

    async def test_stop_cancels_a_running_backfill(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        started = asyncio.Event()

        async def slow(*_args: Any, **_kwargs: Any) -> int:
            started.set()
            await asyncio.sleep(10)
            return 0

        monkeypatch.setattr(
            qdrant_manager_module.memory_qdrant_manager,
            "backfill_paragraph_payloads",
            slow,
        )
        watcher = PayloadBackfillWatcher(poll_seconds=POLL_SECONDS)
        watcher.start()
        await asyncio.wait_for(started.wait(), timeout=2)

        task: Awaitable[None] | None = watcher._task  # noqa: SLF001
        assert task is not None
        await watcher.stop()

        assert watcher._task is not None and watcher._task.done()  # noqa: SLF001
        assert watcher._watch_task is not None and watcher._watch_task.done()  # noqa: SLF001
