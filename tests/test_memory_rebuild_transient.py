"""工作区重建的临时故障处理：单频道失败不中止整个任务。

同一套行为在闸开 / 闸关两条路上都要成立：处理一个频道的函数只拿到「行写哪个分区」，
失败收口与重试都在它内部，任务层的循环只负责接着处理下一个频道。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.services.memory import rebuild as rebuild_module
from nekro_agent.services.memory import rebuild_state_store as store
from nekro_agent.services.memory.consolidator import ConsolidationResult
from nekro_agent.services.memory.rebuild import _rebuild_channels, rebuild_workspace_memories
from nekro_agent.services.memory.rebuild_state_store import (
    MemoryRebuildChannelsFile,
    MemoryRebuildChannelState,
    MemoryRebuildJobState,
    RebuildChannelStatus,
    RebuildJobStatus,
    create_job_state,
    read_job_state,
    write_job_channels,
    write_job_state,
)

WS = 9403
CHAT_FAIL = "onebot_v11-transient-a-group_1"
CHAT_OK = "onebot_v11-transient-b-group_2"
UPPER = 3


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def rebuild_dirs(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "REBUILD_JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(store, "REBUILD_WORKSPACE_DIR", tmp_path / "workspaces")


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rebuild_module, "REBUILD_TRANSIENT_RETRY_DELAYS", (0.01, 0.01, 0.01))
    monkeypatch.setattr(rebuild_module, "REBUILD_CANCEL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)


@pytest.fixture(autouse=True)
def skip_init_and_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ensure(_state: MemoryRebuildJobState, _channels: MemoryRebuildChannelsFile) -> int:
        return 0

    async def fake_replay(_state: MemoryRebuildJobState, _channels: MemoryRebuildChannelsFile) -> int:
        return 0

    monkeypatch.setattr(rebuild_module, "_ensure_initialized", fake_ensure)
    monkeypatch.setattr(rebuild_module, "_replay_cc_semantic_memories", fake_replay)


def make_job(channels: MemoryRebuildChannelsFile) -> MemoryRebuildJobState:
    state = create_job_state(WS, requested_by="test", request_id=None, cutoff=None)
    write_job_channels(state.job_id, channels)
    return state


def make_channels() -> MemoryRebuildChannelsFile:
    return MemoryRebuildChannelsFile(
        channels={
            CHAT_FAIL: MemoryRebuildChannelState(
                status=RebuildChannelStatus.PENDING,
                message_count_total=UPPER,
                cursor_upper_bound_db_id=UPPER,
            ),
            CHAT_OK: MemoryRebuildChannelState(
                status=RebuildChannelStatus.PENDING,
                message_count_total=UPPER,
                cursor_upper_bound_db_id=UPPER,
            ),
        },
    )


def install_consolidation(
    monkeypatch: pytest.MonkeyPatch,
    behaviors: Dict[str, List[ConsolidationResult]],
) -> Dict[str, int]:
    calls: Dict[str, int] = {chat_key: 0 for chat_key in behaviors}

    async def fake(
        workspace_id: int,
        chat_key: str,
        *,
        max_message_db_id: Optional[int] = None,
        start_after_db_id: Optional[int] = None,
        persist_progress: bool = True,
    ) -> ConsolidationResult:
        del workspace_id, max_message_db_id, start_after_db_id, persist_progress
        queue = behaviors[chat_key]
        index = min(calls[chat_key], len(queue) - 1)
        calls[chat_key] += 1
        return queue[index]

    monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake)
    return calls


def success() -> ConsolidationResult:
    return ConsolidationResult(messages_processed=UPPER, paragraphs_created=1, last_processed_message_db_id=UPPER)


def transient() -> ConsolidationResult:
    return ConsolidationResult(failed_reason="temporary provider failure")


def prepare_gate(
    monkeypatch: pytest.MonkeyPatch,
    gate_open: bool,
) -> None:
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", gate_open)
    if gate_open:

        async def fake_reset(_chat_key: str) -> int:
            return 0

        monkeypatch.setattr(rebuild_module, "reset_domain_channel_memory", fake_reset)


@pytest.mark.parametrize("gate_open", [False, True])
class TestTransientFailures:
    async def test_one_channel_fails_after_retries_and_the_job_completes(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
        gate_open: bool,
    ) -> None:
        prepare_gate(monkeypatch, gate_open)
        state = make_job(make_channels())
        calls = install_consolidation(
            monkeypatch,
            {CHAT_FAIL: [transient()], CHAT_OK: [success()]},
        )

        result = await rebuild_workspace_memories(state.job_id)

        final = read_job_state(state.job_id)
        assert final is not None
        assert final.status == RebuildJobStatus.COMPLETED
        assert final.failed_channels == [CHAT_FAIL]
        assert result.channels_failed == 1
        assert result.failed_channels == [CHAT_FAIL]
        assert result.channels_completed == 1
        assert result.messages_processed == UPPER
        # 一次原始调用 + 三次原地重试。
        assert calls[CHAT_FAIL] == 4
        channels = store.read_job_channels(state.job_id)
        assert channels.channels[CHAT_FAIL].status == RebuildChannelStatus.FAILED
        assert channels.channels[CHAT_FAIL].last_error == "temporary provider failure"
        assert channels.channels[CHAT_FAIL].retry_count == 1
        assert channels.channels[CHAT_OK].status == RebuildChannelStatus.COMPLETED
        events = store.get_job_events_path(state.job_id).read_text(encoding="utf-8")
        assert "channel_failed" in events
        assert CHAT_FAIL in events

    async def test_success_after_one_retry_completes_the_channel(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
        gate_open: bool,
    ) -> None:
        prepare_gate(monkeypatch, gate_open)
        state = make_job(make_channels())
        calls = install_consolidation(
            monkeypatch,
            {CHAT_FAIL: [transient(), success()], CHAT_OK: [success()]},
        )

        result = await rebuild_workspace_memories(state.job_id)

        final = read_job_state(state.job_id)
        assert final is not None
        assert final.status == RebuildJobStatus.COMPLETED
        assert final.failed_channels == []
        assert result.channels_failed == 0
        assert calls[CHAT_FAIL] == 2
        channels = store.read_job_channels(state.job_id)
        assert channels.channels[CHAT_FAIL].status == RebuildChannelStatus.COMPLETED

    async def test_cancel_interrupts_the_retry_wait(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
        gate_open: bool,
    ) -> None:
        prepare_gate(monkeypatch, gate_open)
        state = make_job(make_channels())
        # 让重建流程用的就是测试手里这个状态对象：取消请求要在同一个实例上置位。
        monkeypatch.setattr(rebuild_module, "read_job_state", lambda _job_id: state)

        async def fake(
            workspace_id: int,
            chat_key: str,
            *,
            max_message_db_id: Optional[int] = None,
            start_after_db_id: Optional[int] = None,
            persist_progress: bool = True,
        ) -> ConsolidationResult:
            del workspace_id, max_message_db_id, start_after_db_id, persist_progress
            if chat_key == CHAT_FAIL:
                # 第一批失败的同时收到取消请求：重试等待应该立刻把任务收尾，
                # 不把剩下两个间隔等完，也不把频道标成 FAILED。
                state.cancel_requested = True
                return transient()
            return success()

        monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake)

        result = await rebuild_workspace_memories(state.job_id)

        final = read_job_state(state.job_id)
        assert final is not None
        assert final.status == RebuildJobStatus.CANCELLED
        assert final.failed_channels == []
        assert result.channels_failed == 0
        channels = store.read_job_channels(state.job_id)
        assert channels.channels[CHAT_FAIL].status != RebuildChannelStatus.FAILED

    async def test_partial_write_errors_do_not_fail_the_channel(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
        gate_open: bool,
    ) -> None:
        prepare_gate(monkeypatch, gate_open)
        state = make_job(make_channels())
        partial = ConsolidationResult(
            messages_processed=UPPER,
            paragraphs_created=1,
            last_processed_message_db_id=UPPER,
            errors=["一条记忆写库失败"],
        )
        install_consolidation(
            monkeypatch,
            {CHAT_FAIL: [partial], CHAT_OK: [success()]},
        )

        result = await rebuild_workspace_memories(state.job_id)

        final = read_job_state(state.job_id)
        assert final is not None
        assert final.status == RebuildJobStatus.COMPLETED
        assert final.failed_channels == []
        assert result.channels_failed == 0
        channels = store.read_job_channels(state.job_id)
        assert channels.channels[CHAT_FAIL].status == RebuildChannelStatus.COMPLETED
        assert channels.channels[CHAT_FAIL].last_error == "一条记忆写库失败"

    async def test_channel_without_forward_progress_fails_alone(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
        gate_open: bool,
    ) -> None:
        prepare_gate(monkeypatch, gate_open)
        state = make_job(make_channels())
        install_consolidation(
            monkeypatch,
            {CHAT_FAIL: [ConsolidationResult()], CHAT_OK: [success()]},
        )

        result = await rebuild_workspace_memories(state.job_id)

        final = read_job_state(state.job_id)
        assert final is not None
        assert final.status == RebuildJobStatus.COMPLETED
        assert final.failed_channels == [CHAT_FAIL]
        assert result.channels_failed == 1
        assert result.channels_completed == 1
        channels = store.read_job_channels(state.job_id)
        assert channels.channels[CHAT_FAIL].status == RebuildChannelStatus.FAILED
        assert channels.channels[CHAT_OK].status == RebuildChannelStatus.COMPLETED


class TestLegacyStateFile:
    async def test_state_without_failed_channels_reads_as_empty(self) -> None:
        state = create_job_state(WS, requested_by="test", request_id=None, cutoff=None)
        payload = json.loads(state.model_dump_json())
        payload.pop("failed_channels", None)
        store.get_job_state_path(state.job_id).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        loaded = read_job_state(state.job_id)

        assert loaded is not None
        assert loaded.failed_channels == []

    async def test_old_state_can_finish_a_rebuild(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prepare_gate(monkeypatch, True)
        state = make_job(make_channels())
        write_job_state(state)
        payload = json.loads(store.get_job_state_path(state.job_id).read_text(encoding="utf-8"))
        payload.pop("failed_channels", None)
        store.get_job_state_path(state.job_id).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        install_consolidation(monkeypatch, {CHAT_FAIL: [success()], CHAT_OK: [success()]})

        result = await rebuild_workspace_memories(state.job_id)

        assert result.channels_failed == 0
        final = read_job_state(state.job_id)
        assert final is not None
        assert final.status == RebuildJobStatus.COMPLETED
        assert final.failed_channels == []


class TestRebuildChannelsDirectly:
    async def test_failed_channel_records_event_and_continues(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prepare_gate(monkeypatch, False)
        channels = make_channels()
        state = make_job(channels)
        install_consolidation(monkeypatch, {CHAT_FAIL: [transient()], CHAT_OK: [success()]})

        processed, created = await _rebuild_channels(state, channels)

        assert processed == UPPER
        assert created == 1
        assert channels.channels[CHAT_FAIL].status == RebuildChannelStatus.FAILED
        assert channels.channels[CHAT_OK].status == RebuildChannelStatus.COMPLETED
        assert state.failed_channels == [CHAT_FAIL]
