"""工作区重建在闸开 / 闸关两条路上的分工。

闸开时工作区重建先重置工作区行、重放 CC 语义日志，再对每个绑定频道做域行重建：删除
这个频道的旧域行（段落、涉及的人、向量、关系、实体、episode），然后以分区 0 回放。
这样不会出现「旧域行还在、又写一遍」的重复。

闸关时保持原路径：频道阶段照常逐批调用 `consolidate_workspace(persist_progress=False)`。

回看窗口里没有消息的频道也要删旧域行；删除只做一次（落盘标记），续跑不重复删。
完成统计里的 episode 数按域分区里本次绑定频道的行数算。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Optional, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_episode import DBMemEpisode
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
    read_job_channels,
    read_job_state,
    write_job_channels,
)

WS = 9401


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def rebuild_dirs(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    job_dir = tmp_path / "jobs"
    workspace_dir = tmp_path / "workspaces"
    monkeypatch.setattr(store, "REBUILD_JOB_DIR", job_dir)
    monkeypatch.setattr(store, "REBUILD_WORKSPACE_DIR", workspace_dir)


def make_job(channels: MemoryRebuildChannelsFile) -> MemoryRebuildJobState:
    state = create_job_state(WS, requested_by="test", request_id=None, cutoff=None)
    write_job_channels(state.job_id, channels)
    return state


def make_channels() -> MemoryRebuildChannelsFile:
    return MemoryRebuildChannelsFile(
        channels={
            "onebot_v11-rebuild-a-group_1": MemoryRebuildChannelState(
                status=RebuildChannelStatus.PENDING,
                message_count_total=10,
                cursor_upper_bound_db_id=10,
            ),
            "onebot_v11-rebuild-b-group_2": MemoryRebuildChannelState(
                status=RebuildChannelStatus.PENDING,
                message_count_total=5,
                cursor_upper_bound_db_id=5,
            ),
        },
    )


def fake_consolidation(
    calls: List[Tuple[int, str, Optional[int], Optional[int], bool]],
    *,
    paragraphs_created: int = 1,
) -> Any:
    async def _fake(
        workspace_id: int,
        chat_key: str,
        *,
        max_message_db_id: Optional[int] = None,
        start_after_db_id: Optional[int] = None,
        persist_progress: bool = True,
    ) -> ConsolidationResult:
        calls.append((workspace_id, chat_key, max_message_db_id, start_after_db_id, persist_progress))
        upper = max_message_db_id or 0
        return ConsolidationResult(
            messages_processed=upper,
            paragraphs_created=paragraphs_created,
            last_processed_message_db_id=upper,
        )

    return _fake


class TestGateOpen:
    async def test_bound_channels_are_domain_rebuilt(
        self,
        rebuild_dirs: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        channels = make_channels()
        state = make_job(channels)
        reset_calls: List[str] = []
        consolidation_calls: List[Tuple[int, str, Optional[int], Optional[int], bool]] = []

        async def fake_reset(chat_key: str) -> int:
            reset_calls.append(chat_key)
            return 0

        monkeypatch.setattr(rebuild_module, "reset_domain_channel_memory", fake_reset)
        monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake_consolidation(consolidation_calls))

        processed, created = await _rebuild_channels(state, channels)

        assert (processed, created) == (15, 2)
        assert all(channel.status == RebuildChannelStatus.COMPLETED for channel in channels.channels.values())
        assert state.progress.completed_channels == 2
        assert state.progress.processed_messages == 15
        assert sorted(reset_calls) == sorted(channels.channels)
        assert [call[0] for call in consolidation_calls] == [0, 0]
        events = store.get_job_events_path(state.job_id).read_text(encoding="utf-8")
        assert "channel_phase_skipped" not in events
        assert "channel_domain_reset" in events
        assert "channel_completed" in events

    async def test_cc_semantic_replay_still_runs(
        self,
        memory_db: None,
        rebuild_dirs: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        channels = make_channels()
        state = make_job(channels)
        replayed: List[str] = []
        consolidation_calls: List[Tuple[int, str, Optional[int], Optional[int], bool]] = []

        async def fake_ensure(_state: MemoryRebuildJobState, _channels: MemoryRebuildChannelsFile) -> int:
            return 0

        async def fake_replay(_state: MemoryRebuildJobState, _channels: MemoryRebuildChannelsFile) -> int:
            replayed.append(_state.job_id)
            return 3

        async def fake_reset(_chat_key: str) -> int:
            return 0

        monkeypatch.setattr(rebuild_module, "_ensure_initialized", fake_ensure)
        monkeypatch.setattr(rebuild_module, "_replay_cc_semantic_memories", fake_replay)
        monkeypatch.setattr(rebuild_module, "reset_domain_channel_memory", fake_reset)
        monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake_consolidation(consolidation_calls))

        result = await rebuild_workspace_memories(state.job_id)

        assert replayed == [state.job_id]
        assert result.semantic_tasks_replayed == 3
        assert result.messages_processed == 15
        assert result.channels_completed == 2
        final = read_job_state(state.job_id)
        assert final is not None
        assert final.status == RebuildJobStatus.COMPLETED

    async def test_zero_message_channel_still_has_domain_rows_reset(
        self,
        rebuild_dirs: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 回看窗口里一条消息都没有的频道，快照时就标成 COMPLETED。它的旧域行照样要删：
        # 重放 0 条是对的，留着旧行就等于这次重建没清掉它的记忆。
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        quiet_chat = "onebot_v11-rebuild-quiet-group_3"
        busy_chat = "onebot_v11-rebuild-busy-group_4"
        channels = MemoryRebuildChannelsFile(
            channels={
                quiet_chat: MemoryRebuildChannelState(status=RebuildChannelStatus.COMPLETED),
                busy_chat: MemoryRebuildChannelState(
                    status=RebuildChannelStatus.PENDING,
                    message_count_total=4,
                    cursor_upper_bound_db_id=4,
                ),
            },
        )
        state = make_job(channels)
        reset_calls: List[str] = []
        consolidation_calls: List[Tuple[int, str, Optional[int], Optional[int], bool]] = []

        async def fake_reset(chat_key: str) -> int:
            reset_calls.append(chat_key)
            return 2

        monkeypatch.setattr(rebuild_module, "reset_domain_channel_memory", fake_reset)
        monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake_consolidation(consolidation_calls))

        processed, _created = await _rebuild_channels(state, channels)

        assert sorted(reset_calls) == sorted([quiet_chat, busy_chat])
        # 没有消息的频道只删、不回放。
        assert [call[1] for call in consolidation_calls] == [busy_chat]
        assert processed == 4
        assert channels.channels[quiet_chat].status == RebuildChannelStatus.COMPLETED
        persisted = read_job_channels(state.job_id)
        assert persisted.channels[quiet_chat].domain_reset_done is True
        assert persisted.channels[busy_chat].domain_reset_done is True

    async def test_resume_does_not_reset_again(
        self,
        rebuild_dirs: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 删完旧行、还没回放出第一批就中断：续跑时不能再删一遍（落盘标记挡住），
        # 已完成的空频道也不再碰。
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        quiet_chat = "onebot_v11-rebuild-quiet-group_5"
        busy_chat = "onebot_v11-rebuild-busy-group_6"
        channels = MemoryRebuildChannelsFile(
            channels={
                quiet_chat: MemoryRebuildChannelState(
                    status=RebuildChannelStatus.COMPLETED,
                    domain_reset_done=True,
                ),
                busy_chat: MemoryRebuildChannelState(
                    status=RebuildChannelStatus.PENDING,
                    message_count_total=4,
                    cursor_upper_bound_db_id=4,
                    domain_reset_done=True,
                ),
            },
        )
        state = make_job(channels)
        consolidation_calls: List[Tuple[int, str, Optional[int], Optional[int], bool]] = []

        async def refuse_reset(_chat_key: str) -> int:
            raise AssertionError("续跑时不应该再删一次域行")

        monkeypatch.setattr(rebuild_module, "reset_domain_channel_memory", refuse_reset)
        monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake_consolidation(consolidation_calls))

        processed, _created = await _rebuild_channels(state, channels)

        assert processed == 4
        assert [call[1] for call in consolidation_calls] == [busy_chat]

    async def test_old_channels_file_without_reset_flag_still_loads(self, rebuild_dirs: None) -> None:
        # 升级前写下的状态文件没有 domain_reset_done 键，读成 False。
        channels = make_channels()
        state = make_job(channels)
        path = store.get_job_channels_path(state.job_id)
        raw = json.loads(path.read_text(encoding="utf-8"))
        for channel in raw["channels"].values():
            channel.pop("domain_reset_done", None)
        path.write_text(json.dumps(raw), encoding="utf-8")

        loaded = read_job_channels(state.job_id)

        assert sorted(loaded.channels) == sorted(channels.channels)
        assert all(channel.domain_reset_done is False for channel in loaded.channels.values())

    async def test_episode_count_follows_domain_partition(
        self,
        memory_db: None,
        rebuild_dirs: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 闸开时回放写进分区 0；完成统计按工作区数会恒为 0。只数本次绑定频道的域 episode，
        # 别的频道的域 episode 不算。
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        channels = make_channels()
        state = make_job(channels)
        bound_chat = "onebot_v11-rebuild-a-group_1"
        other_chat = "onebot_v11-rebuild-other-group_9"

        async def fake_ensure(_state: MemoryRebuildJobState, _channels: MemoryRebuildChannelsFile) -> int:
            return 0

        async def fake_replay(_state: MemoryRebuildJobState, _channels: MemoryRebuildChannelsFile) -> int:
            return 0

        async def fake_reset(_chat_key: str) -> int:
            return 0

        monkeypatch.setattr(rebuild_module, "_ensure_initialized", fake_ensure)
        monkeypatch.setattr(rebuild_module, "_replay_cc_semantic_memories", fake_replay)
        monkeypatch.setattr(rebuild_module, "reset_domain_channel_memory", fake_reset)
        monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake_consolidation([]))

        now = datetime.now()
        for index, chat_key in enumerate((bound_chat, bound_chat, other_chat), start=1):
            await DBMemEpisode.create(
                workspace_id=0,
                origin_chat_key=chat_key,
                title=f"episode-{index}",
                narrative_summary="",
                time_start=now,
                time_end=now,
            )

        result = await rebuild_workspace_memories(state.job_id)

        assert result.episodes_created == 2


class TestGateOff:
    async def test_channel_phase_still_consolidates(
        self,
        rebuild_dirs: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        channels = MemoryRebuildChannelsFile(
            channels={
                "onebot_v11-rebuild-a-group_1": MemoryRebuildChannelState(
                    status=RebuildChannelStatus.PENDING,
                    message_count_total=3,
                    cursor_upper_bound_db_id=3,
                ),
            },
        )
        state = make_job(channels)
        calls: List[Tuple[int, str, Optional[int], Optional[int], bool]] = []

        async def fake_reset(_chat_key: str) -> int:
            raise AssertionError("闸关时不应该做域行重置")

        monkeypatch.setattr(rebuild_module, "reset_domain_channel_memory", fake_reset)
        monkeypatch.setattr(rebuild_module, "consolidate_workspace", fake_consolidation(calls, paragraphs_created=2))

        processed, created = await _rebuild_channels(state, channels)

        assert (processed, created) == (3, 2)
        assert calls == [(WS, "onebot_v11-rebuild-a-group_1", 3, 0, False)]
        assert channels.channels["onebot_v11-rebuild-a-group_1"].status == RebuildChannelStatus.COMPLETED
        events = store.get_job_events_path(state.job_id).read_text(encoding="utf-8")
        assert "channel_phase_skipped" not in events
