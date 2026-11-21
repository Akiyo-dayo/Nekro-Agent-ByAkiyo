"""只补缺失的记忆向量

嵌入 Key 失效的那段时间，段落照写、向量没写上（`embedding_ref` 为空）。补的时候只给有效、
没有向量的段落嵌入、写向量、回写 `embedding_ref`；正文、权重、时间不动；失活与隔离的不碰；
连续失败就停，下次接着补。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
from nekro_agent.services.memory.vector_backfill import backfill_missing_vectors, count_missing_vectors
from tests.test_memory_consolidation_quality import memory_env as _quality_env

quality_env = _quality_env


async def _row(content: str, *, ref: str | None = None, inactive: bool = False, quarantined: bool = False) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=0,
        instance_key="quality",
        adapter_key="onebot_v11",
        origin_chat_key="onebot_v11-quality-group_vb",
        origin_chat_type="group",
        memory_source="na",
        cognitive_type=CognitiveType.EPISODIC,
        knowledge_type=KnowledgeType.FACT,
        content=content,
        summary=content[:20],
        event_time=datetime(2026, 11, 14, tzinfo=timezone.utc),
        origin_kind=OriginKind.CONSOLIDATION,
        base_weight=1.3,
        embedding_ref=ref,
        is_inactive=inactive or quarantined,
        quarantined_at=datetime.now(timezone.utc) if quarantined else None,
    )


async def _has_point(paragraph_id: int) -> bool:
    client = await qdrant_manager_module.get_qdrant_client()
    return bool(await client.retrieve(memory_qdrant_manager.collection_name, ids=[paragraph_id]))


@pytest.mark.usefixtures("quality_env")
class TestBackfill:
    async def test_only_active_rows_without_vectors_are_filled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        original = embedding_service_module.embed_text

        async def counting(text: str, **kwargs: Any) -> list[float]:
            calls.append(text)
            return await original(text, **kwargs)

        monkeypatch.setattr(embedding_service_module, "embed_text", counting)
        missing = [await _row(f"没向量的段落 {index}") for index in range(3)]
        has_ref = await _row("已经有向量的段落", ref="already")
        inactive = await _row("失活的段落", inactive=True)
        quarantined = await _row("隔离的段落", quarantined=True)
        before = {row.id: (row.content, row.base_weight, row.update_time) for row in missing}
        assert await count_missing_vectors() == 3

        result = await backfill_missing_vectors(pause_seconds=0)

        assert (result.written, result.failed, result.remaining, result.stopped_early) == (3, 0, 0, False)
        assert sorted(calls) == sorted(row.content for row in missing), "只嵌入缺向量的有效段落"
        for row in missing:
            fresh = await DBMemParagraph.get(id=row.id)
            assert fresh.embedding_ref == str(row.id)
            assert (fresh.content, fresh.base_weight, fresh.update_time) == before[row.id], "正文、权重、时间不动"
            assert await _has_point(row.id)
        for row in (has_ref, inactive, quarantined):
            fresh = await DBMemParagraph.get(id=row.id)
            assert fresh.embedding_ref == row.embedding_ref
            assert not await _has_point(row.id)

    async def test_consecutive_failures_stop_and_resume_later(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [await _row(f"段落 {index}") for index in range(8)]
        original = embedding_service_module.embed_text
        broken_after = {"ok": 2}

        async def flaky(text: str, **kwargs: Any) -> list[float]:
            if broken_after["ok"] <= 0:
                raise RuntimeError("401 Invalid token")
            broken_after["ok"] -= 1
            return await original(text, **kwargs)

        monkeypatch.setattr(embedding_service_module, "embed_text", flaky)
        first = await backfill_missing_vectors(pause_seconds=0, max_consecutive_failures=3)
        assert (first.written, first.failed, first.stopped_early) == (2, 3, True)
        assert first.remaining == len(rows) - 2

        monkeypatch.setattr(embedding_service_module, "embed_text", original)
        second = await backfill_missing_vectors(pause_seconds=0)
        assert (second.written, second.remaining, second.stopped_early) == (len(rows) - 2, 0, False), "下次接着补"

    async def test_empty_content_is_skipped_and_not_counted(self) -> None:
        await _row("   ")
        await _row("\n\t")
        await _row("有正文的段落")
        assert await count_missing_vectors() == 1, "补不了的空白段落不算「缺」，与嵌入健康页同一个数"
        result = await backfill_missing_vectors(pause_seconds=0)
        assert (result.written, result.skipped_empty, result.remaining) == (1, 2, 0), "补完能到 0"

    async def test_row_deactivated_meanwhile_is_not_written_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = await _row("补的过程中被删的段落")
        original = embedding_service_module.embed_text

        async def delete_meanwhile(text: str, **kwargs: Any) -> list[float]:
            await DBMemParagraph.filter(id=row.id).update(is_inactive=True)
            return await original(text, **kwargs)

        monkeypatch.setattr(embedding_service_module, "embed_text", delete_meanwhile)
        await backfill_missing_vectors(pause_seconds=0)
        assert (await DBMemParagraph.get(id=row.id)).embedding_ref is None
