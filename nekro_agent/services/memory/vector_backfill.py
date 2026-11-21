"""只补缺失的记忆向量

嵌入服务出问题的那段时间（例如 Key 失效，每次都 401），沉淀照常写段落，只是向量没写上：
段落是有效的，`embedding_ref` 却是空的。这些段落向量检索搜不到，近重复检查也比不了它们。
所有写向量的地方成功后都会回写 `embedding_ref`，所以「有效且 `embedding_ref` 为空」就是缺向量。

这里只做一件事：给这些段落嵌入、写向量、回写 `embedding_ref`。不改正文、类型、权重与时间，
不碰已失活或隔离的段落，也不像「重建索引」那样先删整个集合。按主键分批、批间停一下限速；
连续失败到一定次数就停（多半是嵌入服务还没好），没补上的下次再跑会接着补。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_mem_paragraph import DBMemParagraph

logger = get_sub_logger("memory.vector_backfill")

# 每批多少段、批与批之间停多久（秒）
BACKFILL_BATCH_SIZE = 20
BACKFILL_PAUSE_SECONDS = 1.0
# 连续失败这么多次就停：嵌入服务多半还没恢复，接着打只是白白刷失败
MAX_CONSECUTIVE_FAILURES = 5


@dataclass
class VectorBackfillResult:
    """一次补向量的结果"""

    written: int = 0
    failed: int = 0
    skipped_empty: int = 0
    stopped_early: bool = False
    remaining: int = 0


def _missing_query():
    return DBMemParagraph.filter(is_inactive=False, embedding_ref__isnull=True)


async def count_missing_vectors() -> int:
    """有效、正文不是空白、却没有向量的段落数——也就是这里补得了的那些

    与后台嵌入健康页的「缺向量」同一个数、同一处实现：正文全是空白的段落这里会跳过（补不了），
    算进去的话确认提示与「仍缺」永远到不了 0。
    """
    from nekro_agent.services.embedding_health import _count_missing_vectors_now

    return await _count_missing_vectors_now()


async def backfill_missing_vectors(
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
    pause_seconds: float = BACKFILL_PAUSE_SECONDS,
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
) -> VectorBackfillResult:
    """给有效、没有向量的段落补上向量；可中断、可重复执行"""
    from nekro_agent.services.memory.embedding_service import embed_text
    from nekro_agent.services.memory.persons import build_paragraph_payload
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

    result = VectorBackfillResult()
    consecutive_failures = 0
    cursor = 0
    page_size = max(1, int(batch_size))
    while not result.stopped_early:
        page = await _missing_query().filter(id__gt=cursor).order_by("id").limit(page_size)
        if not page:
            break
        cursor = page[-1].id
        for row in page:
            if not (row.content or "").strip():
                result.skipped_empty += 1
                continue
            try:
                embedding = await embed_text(row.content, requester=(row.adapter_key, row.instance_key))
                written = await memory_qdrant_manager.upsert_paragraph(
                    paragraph_id=row.id,
                    embedding=embedding,
                    payload=await build_paragraph_payload(row),
                )
                if not written:
                    raise RuntimeError("向量写入返回失败")
            except Exception as e:
                result.failed += 1
                consecutive_failures += 1
                logger.warning(f"补向量失败，下次再试: paragraph_id={row.id}, error={e}")
                if consecutive_failures >= max(1, int(max_consecutive_failures)):
                    result.stopped_early = True
                    logger.warning(f"补向量连续失败 {consecutive_failures} 次，先停下；嵌入服务恢复后再跑")
                    break
                continue
            consecutive_failures = 0
            # 只回写这一列：正文、权重、时间一概不动；补的过程中被失活 / 隔离的不回写
            await DBMemParagraph.filter(id=row.id, is_inactive=False, embedding_ref__isnull=True).update(
                embedding_ref=str(row.id),
            )
            result.written += 1
        if pause_seconds > 0 and not result.stopped_early:
            await asyncio.sleep(pause_seconds)
    result.remaining = await count_missing_vectors()
    logger.info(
        f"补向量结束: written={result.written}, failed={result.failed}, skipped_empty={result.skipped_empty}, "
        f"stopped_early={result.stopped_early}, remaining={result.remaining}",
    )
    return result


__all__ = [
    "BACKFILL_BATCH_SIZE",
    "BACKFILL_PAUSE_SECONDS",
    "MAX_CONSECUTIVE_FAILURES",
    "VectorBackfillResult",
    "backfill_missing_vectors",
    "count_missing_vectors",
]
