"""启动恢复与清理都要覆盖分区 0。

闸开后频道记忆写在分区 0，它不属于任何工作区：启动恢复不能只扫绑了工作区的频道，
清理也不能只遍历工作区。两边都按主键游标分页，避免频道/段落一多就整表加载。
"""

from __future__ import annotations

import json
from typing import Any, List

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.services.memory import maintenance as maintenance_module
from nekro_agent.services.memory import scheduler as scheduler_module
from nekro_agent.services.memory.maintenance import prune_all_workspaces
from nekro_agent.services.memory.scheduler import MemoryMaintenanceScheduler

CHAT_PREFIX = "onebot_v11-recovery-group_"


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _domain_paragraph_content(index: int) -> str:
    return f"域分区里低权重的一段记忆 {index}"


async def _create_domain_paragraph(*, base_weight: float = 0.0) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=0,
        instance_key="recovery",
        preset_id=0,
        memory_source="na",
        origin_kind=OriginKind.CONSOLIDATION,
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.CONVERSATION,
        content=_domain_paragraph_content(1),
        summary="低权重",
        base_weight=base_weight,
    )


class TestStartupRecoveryPaging:
    async def test_scans_all_active_channels_in_pages(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del memory_db
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        monkeypatch.setattr(config, "MEMORY_ENABLE_FOR_CHAT", True)
        monkeypatch.setattr(config, "MEMORY_CONSOLIDATION_MSG_THRESHOLD", 2)
        monkeypatch.setattr(config, "MEMORY_STARTUP_RECOVERY_MAX_TASKS", 1000)

        total_channels = 450
        hot_indexes = {0, 150, 300}
        chat_keys = [f"{CHAT_PREFIX}{index}" for index in range(total_channels)]
        await DBChatChannel.bulk_create(
            [
                DBChatChannel(
                    chat_key=chat_key,
                    adapter_key="onebot_v11",
                    instance_key="recovery",
                    channel_id=str(index),
                    channel_name=chat_key,
                    channel_type="group",
                    is_active=True,
                    workspace_id=None,
                    data=json.dumps({"memory_state": {"last_consolidated_message_db_id": 0}}),
                )
                for index, chat_key in enumerate(chat_keys)
            ],
        )
        await DBChatMessage.bulk_create(
            [
                DBChatMessage(
                    sender_id=f"u{index}",
                    sender_name=f"用户{index}",
                    sender_nickname=f"用户{index}",
                    is_tome=0,
                    is_recalled=False,
                    adapter_key="onebot_v11",
                    instance_key="recovery",
                    message_id=f"m{index}-{offset}",
                    chat_key=chat_key,
                    chat_type="group",
                    platform_userid=f"u{index}",
                    content_text="需要恢复的待沉淀消息",
                    content_data="[]",
                    raw_cq_code="",
                    ext_data="{}",
                    send_timestamp=1_700_000_000 + index,
                )
                for index, chat_key in enumerate(chat_keys)
                for offset in range(2 if index in hot_indexes else 1)
            ],
        )

        async def fake_sleep(_seconds: float) -> None:
            return None

        scheduler = MemoryMaintenanceScheduler()
        try:
            monkeypatch.setattr(
                scheduler_module,
                "asyncio",
                type("StubAsyncio", (), {"sleep": staticmethod(fake_sleep)}),
            )
            await scheduler._recover_pending_consolidations()  # noqa: SLF001

            assert len(scheduler.queued_tasks()) == len(hot_indexes)
            scheduled = {(task.workspace_id, task.chat_key) for task in scheduler.queued_tasks()}
            assert scheduled == {(0, chat_keys[index]) for index in hot_indexes}
            assert scheduler._chat_workspace_map[chat_keys[300]] == 0  # noqa: SLF001
        finally:
            await scheduler.stop()


class TestPruneCoversDomainPartition:
    async def test_gate_on_prunes_partition_zero_in_chunks(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del memory_db
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", True)
        monkeypatch.setattr(maintenance_module, "PRUNE_SCAN_PAGE_SIZE", 2)
        monkeypatch.setattr(maintenance_module, "PRUNE_WRITE_CHUNK_SIZE", 2)
        monkeypatch.setattr(config, "MEMORY_PRUNE_PARAGRAPH_THRESHOLD", 0.5)
        monkeypatch.setattr(maintenance_module, "PRUNE_WORKSPACE_INTERVAL_SECONDS", 0.0)

        deleted_batches: List[List[int]] = []

        async def fake_delete(ids: List[int]) -> None:
            deleted_batches.append(list(ids))

        monkeypatch.setattr(maintenance_module.memory_qdrant_manager, "delete_paragraphs", fake_delete)

        await DBWorkspace.create(name="prune-workspace")
        paragraphs = [await _create_domain_paragraph() for _ in range(5)]

        results = await prune_all_workspaces()

        assert 0 in results
        assert results[0].paragraphs_pruned == 5
        assert [len(batch) for batch in deleted_batches] == [2, 2, 1]
        assert sorted(pid for batch in deleted_batches for pid in batch) == sorted(p.id for p in paragraphs)
        assert await DBMemParagraph.filter(id__in=[p.id for p in paragraphs], is_inactive=True).count() == 5

    async def test_gate_off_does_not_touch_partition_zero(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del memory_db
        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_DOMAIN_SCOPE_ENABLED", False)
        monkeypatch.setattr(maintenance_module, "PRUNE_WORKSPACE_INTERVAL_SECONDS", 0.0)

        deleted_batches: List[List[int]] = []

        async def fake_delete(ids: List[int]) -> None:
            deleted_batches.append(list(ids))

        monkeypatch.setattr(maintenance_module.memory_qdrant_manager, "delete_paragraphs", fake_delete)

        await DBWorkspace.create(name="prune-workspace")
        paragraph = await _create_domain_paragraph()

        results = await prune_all_workspaces()

        assert 0 not in results
        assert deleted_batches == []
        assert await DBMemParagraph.filter(id=paragraph.id, is_inactive=False).exists()
