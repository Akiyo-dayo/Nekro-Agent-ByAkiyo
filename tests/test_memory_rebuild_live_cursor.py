"""频道重建完成后，实时沉淀的游标推到重建覆盖到的最后一条消息

重建拿自己的游标把一段历史重新沉淀一遍，不碰频道 `memory_state` 里的实时游标。实时游标
如果还停在这段历史里面，调度器恢复后会从它接着沉淀，把重建刚处理过的消息再沉淀一次，
写出重复记忆。这里钉住：

1. `advance_consolidation_cursor` 只前进不后退，没有游标时补上，不动失败计数等其他状态；
2. 工作区重建（闸开闸关都一样）与单频道域行重建完成后，实时游标都到了重建上界；
3. 频道重建失败时不推进（这段消息还要靠实时沉淀或下次重建）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.services.memory import rebuild as rebuild_module
from nekro_agent.services.memory import rebuild_state_store as store
from nekro_agent.services.memory.consolidator import ConsolidationResult, advance_consolidation_cursor
from nekro_agent.services.memory.rebuild import rebuild_domain_channel, rebuild_workspace_memories
from nekro_agent.services.memory.rebuild_state_store import (
    MemoryRebuildChannelsFile,
    MemoryRebuildChannelState,
    MemoryRebuildJobState,
    RebuildChannelStatus,
    create_job_state,
    write_job_channels,
)

WS = 9411
CHAT = "onebot_v11-livecursor-group_7"
UPPER = 40


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def rebuild_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "REBUILD_JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(store, "REBUILD_WORKSPACE_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(rebuild_module, "REBUILD_TRANSIENT_RETRY_DELAYS", (0.01,))
    monkeypatch.setattr(rebuild_module, "REBUILD_CANCEL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)

    async def fake_ensure(_state: MemoryRebuildJobState, _channels: MemoryRebuildChannelsFile) -> int:
        return 0

    async def fake_replay(_state: MemoryRebuildJobState, _channels: MemoryRebuildChannelsFile) -> int:
        return 0

    async def fake_reset(_chat_key: str) -> int:
        return 0

    monkeypatch.setattr(rebuild_module, "_ensure_initialized", fake_ensure)
    monkeypatch.setattr(rebuild_module, "_replay_cc_semantic_memories", fake_replay)
    monkeypatch.setattr(rebuild_module, "reset_domain_channel_memory", fake_reset)


async def _channel(memory_state: Optional[Dict[str, Any]]) -> DBChatChannel:
    data: Dict[str, Any] = {} if memory_state is None else {"memory_state": memory_state}
    return await DBChatChannel.create(
        adapter_key="onebot_v11",
        instance_key="livecursor",
        channel_id="group_7",
        channel_name="live cursor",
        channel_type="group",
        chat_key=CHAT,
        data=json.dumps(data),
    )


async def _memory_state() -> Dict[str, Any]:
    channel = await DBChatChannel.get(chat_key=CHAT)
    data = json.loads(channel.data or "{}")
    state = data.get("memory_state", {})
    assert isinstance(state, dict)
    return state


def _install_consolidation(monkeypatch: pytest.MonkeyPatch, result: ConsolidationResult) -> None:
    async def fake(
        workspace_id: int,
        chat_key: str,
        *,
        max_message_db_id: Optional[int] = None,
        start_after_db_id: Optional[int] = None,
        persist_progress: bool = True,
    ) -> ConsolidationResult:
        del workspace_id, chat_key, max_message_db_id, start_after_db_id, persist_progress
        return result

    monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake)


class TestAdvanceCursor:
    async def test_moves_forward_only_and_keeps_other_state(self, memory_db: None) -> None:
        await _channel({"last_consolidated_message_db_id": 5, "consecutive_failures": 2, "skipped_batches": 1})

        assert await advance_consolidation_cursor(CHAT, 30) is True
        state = await _memory_state()
        assert state["last_consolidated_message_db_id"] == 30
        assert state["consecutive_failures"] == 2
        assert state["skipped_batches"] == 1

        assert await advance_consolidation_cursor(CHAT, 12) is False
        assert (await _memory_state())["last_consolidated_message_db_id"] == 30

    async def test_sets_cursor_when_missing(self, memory_db: None) -> None:
        await _channel(None)
        assert await advance_consolidation_cursor(CHAT, 9) is True
        assert (await _memory_state())["last_consolidated_message_db_id"] == 9

    async def test_missing_channel_or_zero_target_is_a_no_op(self, memory_db: None) -> None:
        assert await advance_consolidation_cursor("onebot_v11-nobody-group_0", 9) is False
        await _channel({"last_consolidated_message_db_id": 3})
        assert await advance_consolidation_cursor(CHAT, 0) is False
        assert (await _memory_state())["last_consolidated_message_db_id"] == 3


@pytest.mark.parametrize("gate_open", [False, True])
async def test_workspace_rebuild_advances_live_cursor(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
    gate_open: bool,
) -> None:
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", gate_open)
    await _channel({"last_consolidated_message_db_id": 10})
    channels = MemoryRebuildChannelsFile(
        channels={
            CHAT: MemoryRebuildChannelState(
                status=RebuildChannelStatus.PENDING,
                message_count_total=UPPER,
                cursor_upper_bound_db_id=UPPER,
            ),
        },
    )
    state = create_job_state(WS, requested_by="test", request_id=None, cutoff=None)
    write_job_channels(state.job_id, channels)
    _install_consolidation(
        monkeypatch,
        ConsolidationResult(messages_processed=UPPER, last_processed_message_db_id=UPPER),
    )

    await rebuild_workspace_memories(state.job_id)

    assert (await _memory_state())["last_consolidated_message_db_id"] == UPPER


@pytest.mark.parametrize("gate_open", [False, True])
async def test_failed_channel_does_not_advance_live_cursor(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
    gate_open: bool,
) -> None:
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", gate_open)
    await _channel({"last_consolidated_message_db_id": 10})
    channels = MemoryRebuildChannelsFile(
        channels={
            CHAT: MemoryRebuildChannelState(
                status=RebuildChannelStatus.PENDING,
                message_count_total=UPPER,
                cursor_upper_bound_db_id=UPPER,
            ),
        },
    )
    state = create_job_state(WS, requested_by="test", request_id=None, cutoff=None)
    write_job_channels(state.job_id, channels)
    _install_consolidation(monkeypatch, ConsolidationResult(failed_reason="timeout"))

    await rebuild_workspace_memories(state.job_id)

    assert (await _memory_state())["last_consolidated_message_db_id"] == 10


async def test_domain_channel_rebuild_advances_live_cursor(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
    await _channel({"last_consolidated_message_db_id": 10})

    async def fake_cutoff_cursor(_chat_key: str, _cutoff: Any) -> int:
        return 0

    async def fake_latest(_chat_key: str) -> int:
        return UPPER

    async def fake_count(_chat_key: str, _start: int, _upper: int) -> int:
        return UPPER

    monkeypatch.setattr(rebuild_module, "_find_last_message_db_id_before_cutoff", fake_cutoff_cursor)
    monkeypatch.setattr(rebuild_module, "_find_latest_message_db_id", fake_latest)
    monkeypatch.setattr(rebuild_module, "_count_channel_messages", fake_count)
    _install_consolidation(
        monkeypatch,
        ConsolidationResult(messages_processed=UPPER, last_processed_message_db_id=UPPER),
    )

    result = await rebuild_domain_channel(CHAT)

    assert result.channels_completed == 1
    assert (await _memory_state())["last_consolidated_message_db_id"] == UPPER
