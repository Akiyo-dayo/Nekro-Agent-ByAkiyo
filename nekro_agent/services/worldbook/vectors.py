"""世界书条目的向量同步与语义检索

条目上标了「参与语义触发」（或它所在的书开了「全部条目参与」）时，除了关键词，还用最近
几条消息的向量去找相似条目。命中的条目与关键词命中同等对待，之后照常走延迟 / 冷却 /
粘性、按用途路由、模板、预算、条数上限与信任级别。

## 只在这一份里判「参与」

「参与」只有 :func:`participates` 一个判据：条目启用、状态正常、不是常驻，并且条目自己
标了 `vectorized` 或书开了 `vector_all`。同步、检索、书详情的计数都调它，避免出现
「同步了却搜不到」或「页面上算参与、检索里不算」的分叉。

## 不接 L2 的查询向量复用

设计稿里「这一轮 L2 已算过同一段查询文本的向量就直接复用」这一版没有接：记忆召回的查询
文本是另一种拼法，而且它在世界书求值之后才算，两边的文本本来就不相同。模块内的短缓存
（见 :func:`_query_vector`）覆盖了同一轮迭代与触发测试反复点的情况。

## 读路径 fail-open

语义触发绝不能让一轮对话失败或明显变慢：整个检索包在 :func:`semantic_hits` 的超时里，
超时或任何异常都按「没有语义命中」处理。只有 :class:`asyncio.CancelledError` 原样抛出。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from typing import Any

from qdrant_client import models as qdrant_models
from tortoise.expressions import Q

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.vector_db import get_qdrant_client
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.services.memory import embedding_service as embedding_service_module
from nekro_agent.services.memory.embedding_service import (
    get_memory_embedding_dimension,
    get_memory_embedding_model_group,
)

from .types import EntryStatus, WindowMessage

logger = get_sub_logger("worldbook.vectors")

COLLECTION = "nekro_worldbook_entries"
PAYLOAD_SCHEMA = 1
# 语义检索的总超时。世界书求值在提示词渲染的同步路径上，一次慢查询会拖慢整轮对话。
SEMANTIC_TIMEOUT_SECONDS = 8.0
# 一次同步最多处理多少条；剩余的留给下一轮（有成果就 2 秒后再跑）。
SYNC_BATCH_LIMIT = 64
# 条目正文参与嵌入的最大长度：超出的部分对语义没有帮助，却会让请求体变大。
EMBED_MAX_CHARS = 8000
# 查询文本的上限：最近几条消息可能都很长，截断后仍能覆盖主题。
QUERY_MAX_CHARS = 2000
# 查询向量缓存：键是（模型组, 文本哈希），最多 256 项、存活 120 秒。
QUERY_CACHE_MAX = 256
QUERY_CACHE_TTL_SECONDS = 120.0
# 看护循环的节奏与失败退避。
WATCHER_INITIAL_DELAY_SECONDS = 30.0
WATCHER_ACTIVE_DELAY_SECONDS = 2.0
WATCHER_IDLE_DELAY_SECONDS = 60.0
WATCHER_BACKOFF_SECONDS = (60.0, 120.0, 300.0)
# 同一种错误 5 分钟内只记一次：检索失败会在每一轮重复，日志不能被它刷满。
ERROR_LOG_INTERVAL_SECONDS = 300.0
LOG_SAMPLE_MAX = 64
# Qdrant 单次删除请求携带的点数，避免一次请求过大。
DELETE_CHUNK_SIZE = 256
# 检索时比书的上限多取的条数，留给过滤掉的陈旧点
SEARCH_OVERFETCH = 16

# 已确认可用的（客户端, 维度）。测试里每个用例换一个内存客户端，用客户端身份区分。
_ready_signature: tuple[int, int] | None = None
# 进程级查询向量缓存：值是（写入时刻 monotonic, 向量）。
_query_cache: OrderedDict[tuple[str, str], tuple[float, list[float]]] = OrderedDict()
# 语义检索失败的最近记录：错误键 → 上次记日志的时刻。
_error_log_at: dict[str, float] = {}


def participates(entry: Any, book: Any) -> bool:
    """这条条目是否参与语义触发（整个模块唯一的「参与」判据）"""
    return (
        bool(getattr(entry, "enabled", False))
        and str(getattr(entry, "status", "")) == EntryStatus.ACTIVE.value
        and not bool(getattr(entry, "constant", False))
        and (bool(getattr(entry, "vectorized", False)) or bool(getattr(book, "vector_all", False)))
    )


def embed_source(content: str) -> str:
    """参与嵌入的正文：去首尾空白、截到上限"""
    return (content or "").strip()[:EMBED_MAX_CHARS]


def content_hash(text: str) -> str:
    """正文哈希（sha256 十六进制）"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _ensure_payload_indexes(client: Any) -> None:
    """为按书过滤建 payload 索引；已存在时的报错直接忽略"""
    try:
        await client.create_payload_index(
            collection_name=COLLECTION,
            field_name="book_id",
            field_schema=qdrant_models.PayloadSchemaType.INTEGER,
        )
    except Exception as error:  # noqa: BLE001 - 索引是优化，失败不影响正确性
        logger.debug(f"世界书 payload index 跳过: field=book_id, error={error}")


async def ensure_collection(dimension: int | None = None) -> bool:
    """确保向量集合存在，维度变了就重建

    维度不一致且集合里还有点：删掉集合重建，并把全部条目的 `vector_hash` 清空，让看护
    循环按当前模型重新生成。返回是否新建 / 重建了集合。

    客户端不可用（压测模式）时安静返回 False；Qdrant 调用出错则抛出，启动侧与看护循环
    各自决定怎么记日志 / 退避。
    """
    global _ready_signature

    resolved_dimension = dimension or get_memory_embedding_dimension()
    client = await get_qdrant_client()
    if client is None:
        return False

    signature = (id(client), resolved_dimension)
    if _ready_signature == signature:
        return False

    collections = await client.get_collections()
    existing_names = [collection.name for collection in collections.collections]
    if COLLECTION in existing_names:
        info = await client.get_collection(COLLECTION)
        try:
            current_size = int(info.config.params.vectors.size)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 结构不认识时按「维度未知」处理，重建更安全
            current_size = None
        if current_size is not None and current_size != resolved_dimension:
            points_count = int(info.points_count or 0)
            if points_count == 0:
                logger.warning(
                    f"世界书向量集合维度不匹配且当前为空，准备重建: "
                    f"current={current_size}, expected={resolved_dimension}",
                )
            else:
                logger.warning(
                    f"世界书向量集合维度不匹配，删除重建并清空全部条目的 vector_hash: "
                    f"current={current_size}, expected={resolved_dimension}, points={points_count}",
                )
                await DBWbEntry.all().update(vector_hash="")
            await client.delete_collection(COLLECTION)
        else:
            await _ensure_payload_indexes(client)
            _ready_signature = signature
            return False

    await client.create_collection(
        collection_name=COLLECTION,
        vectors_config=qdrant_models.VectorParams(
            size=resolved_dimension,
            distance=qdrant_models.Distance.COSINE,
        ),
    )
    await _ensure_payload_indexes(client)
    _ready_signature = signature
    logger.info(f"世界书向量集合 {COLLECTION} 创建成功")
    return True


async def _pending_entries(limit: int) -> list[DBWbEntry]:
    """参与、但还没有可用向量的条目（连书一起取，等会儿要判书级开关）"""
    queryset = DBWbEntry.filter(
        enabled=True,
        status=EntryStatus.ACTIVE.value,
        constant=False,
        vector_hash="",
    ).filter(Q(vectorized=True) | Q(book__vector_all=True))
    return list(await queryset.select_related("book").order_by("id").limit(limit))


def _point(row: DBWbEntry, vector: list[float], digest: str) -> qdrant_models.PointStruct:
    return qdrant_models.PointStruct(
        id=int(row.id),
        vector=vector,
        payload={
            "entry_id": int(row.id),
            "book_id": int(row.book_id),
            "content_hash": digest,
            "payload_schema": PAYLOAD_SCHEMA,
        },
    )


async def _write_hash(row: DBWbEntry, digest: str) -> bool:
    """条件写回哈希：正文在这期间被改过就不写，避免把新正文标成已同步"""
    updated = await DBWbEntry.filter(id=row.id, content=row.content).update(vector_hash=digest)
    return bool(updated)


async def sync_pending(limit: int = SYNC_BATCH_LIMIT) -> int:
    """为还未同步的参与条目生成向量，返回这一轮成功写回的条数

    正文为空的条目直接把 `vector_hash` 写成空正文的哈希并跳过（它们不需要向量）。
    嵌入或写入失败的条目保持 `vector_hash == ""`，下一轮再看。
    """
    client = await get_qdrant_client()
    if client is None:
        return 0

    await ensure_collection()
    rows = await _pending_entries(limit)
    if not rows:
        return 0

    synced = 0
    pending: list[tuple[DBWbEntry, str]] = []
    for row in rows:
        text = embed_source(row.content)
        digest = content_hash(text)
        if not text:
            # 空正文没有向量可算，但也要把哈希写掉，否则每一轮都会被当成待办。
            if await _write_hash(row, digest):
                synced += 1
            continue
        pending.append((row, text))

    if not pending:
        return synced

    vectors = await embedding_service_module.embed_batch([text for _row, text in pending])
    points: list[qdrant_models.PointStruct] = []
    prepared: list[tuple[DBWbEntry, str]] = []
    for (row, text), vector in zip(pending, vectors, strict=False):
        if vector is None:
            continue
        digest = content_hash(text)
        points.append(_point(row, vector, digest))
        prepared.append((row, digest))

    if not points:
        # 这一批一条向量都没算出来（嵌入模型组没配好、Key 失效等）：抛出去让看护循环退避，
        # 否则每分钟都会把同一批再算一遍，白白打上游。
        raise RuntimeError(f"世界书向量生成全部失败: count={len(pending)}")

    try:
        await client.upsert(collection_name=COLLECTION, points=points)
    except Exception as error:  # noqa: BLE001 - 失败的留空等下次
        logger.warning(f"世界书向量写入失败，下一轮重试: count={len(points)}, error={error!r}")
        return synced

    for row, digest in prepared:
        if await _write_hash(row, digest):
            synced += 1
    return synced


async def delete_entries(entry_ids: Iterable[int]) -> None:
    """删除这些条目对应的向量点；尽力而为，失败只记 WARNING"""
    ids = sorted({int(entry_id) for entry_id in entry_ids})
    if not ids:
        return
    client = await get_qdrant_client()
    if client is None:
        return
    try:
        for start in range(0, len(ids), DELETE_CHUNK_SIZE):
            chunk = ids[start : start + DELETE_CHUNK_SIZE]
            await client.delete(
                collection_name=COLLECTION,
                points_selector=qdrant_models.PointIdsList(points=chunk),
            )
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 孤儿点没有害处，检索时会与数据库候选取交集
        logger.warning(f"删除世界书条目向量失败（忽略）: count={len(ids)}, error={error!r}")


async def delete_books(book_ids: Iterable[int]) -> None:
    """按 payload 里的 `book_id` 删除整本书的向量点；尽力而为"""
    ids = sorted({int(book_id) for book_id in book_ids})
    if not ids:
        return
    client = await get_qdrant_client()
    if client is None:
        return
    try:
        for book_id in ids:
            await client.delete(
                collection_name=COLLECTION,
                points_selector=qdrant_models.FilterSelector(
                    filter=qdrant_models.Filter(
                        must=[
                            qdrant_models.FieldCondition(
                                key="book_id",
                                match=qdrant_models.MatchValue(value=book_id),
                            ),
                        ],
                    ),
                ),
            )
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001
        logger.warning(f"删除世界书向量失败（忽略）: books={len(ids)}, error={error!r}")


async def participation_counts(book: DBWbBook) -> tuple[int, int]:
    """这本书里参与语义触发的条目数，以及其中还没有向量的条数"""
    rows = await DBWbEntry.filter(book_id=book.id).only(
        "id",
        "enabled",
        "status",
        "constant",
        "vectorized",
        "vector_hash",
    )
    participating = [row for row in rows if participates(row, book)]
    pending = sum(1 for row in participating if not row.vector_hash)
    return len(participating), pending


def _query_text(window: Sequence[WindowMessage], count: int) -> str:
    """窗口里最后 count 条非空消息，用换行连接并截断"""
    texts = [message.content_text for message in window if message.content_text and message.content_text.strip()]
    if not texts:
        return ""
    return "\n".join(texts[-max(1, count) :])[:QUERY_MAX_CHARS]


async def _query_vector(
    model_group: str,
    text: str,
    *,
    requester: tuple[str, str] | None,
) -> list[float]:
    """取查询向量；相同（模型组, 文本）在 120 秒内只算一次"""
    key = (model_group, content_hash(text))
    now = time.monotonic()
    cached = _query_cache.get(key)
    if cached is not None and now - cached[0] <= QUERY_CACHE_TTL_SECONDS:
        _query_cache.move_to_end(key)
        return cached[1]
    if cached is not None:
        _query_cache.pop(key, None)

    vector = await embedding_service_module.embed_text(text, requester=requester)
    _query_cache[key] = (now, vector)
    while len(_query_cache) > QUERY_CACHE_MAX:
        _query_cache.popitem(last=False)
    return vector


def _log_semantic_error(error: BaseException) -> None:
    """同一种错误 5 分钟内只记一次 WARNING"""
    key = f"{type(error).__name__}:{error}"
    now = time.monotonic()
    last = _error_log_at.get(key)
    if last is not None and now - last < ERROR_LOG_INTERVAL_SECONDS:
        return
    _error_log_at[key] = now
    while len(_error_log_at) > LOG_SAMPLE_MAX:
        _error_log_at.pop(next(iter(_error_log_at)))
    logger.warning(f"世界书语义检索失败，这一轮按没有语义命中处理: {error!r}")


async def _semantic_hits(
    books: Sequence[DBWbBook],
    entries: Sequence[DBWbEntry],
    window: Sequence[WindowMessage],
    *,
    requester: tuple[str, str] | None,
) -> dict[int, float]:
    book_by_id = {int(book.id): book for book in books}
    candidates: dict[int, dict[int, DBWbEntry]] = {}
    for entry in entries:
        book = book_by_id.get(int(entry.book_id))
        if book is None or not entry.vector_hash:
            continue
        if not participates(entry, book):
            continue
        candidates.setdefault(int(book.id), {})[int(entry.id)] = entry

    # 没有任何候选就不算查询向量——默认路径（没人打开这个开关）一次嵌入请求都不发。
    if not candidates:
        return {}

    client = await get_qdrant_client()
    if client is None:
        return {}

    model_group = get_memory_embedding_model_group()
    scores: dict[int, float] = {}
    for book_id, book_candidates in candidates.items():
        book = book_by_id[book_id]
        text = _query_text(window, int(book.vector_query_messages or 2))
        if not text:
            continue
        query_vector = await _query_vector(model_group, text, requester=requester)
        max_hits = max(1, int(book.vector_max_entries or 5))
        results = await client.search(
            collection_name=COLLECTION,
            query_vector=query_vector,
            query_filter=qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="book_id",
                        match=qdrant_models.MatchValue(value=book_id),
                    ),
                ],
            ),
            # 多取一些：库里可能有已停用 / 已改成常驻 / 已不参与的条目留下的点，它们会占名额，
            # 过滤之后再按书的上限截。
            limit=max_hits + SEARCH_OVERFETCH,
            score_threshold=float(book.vector_threshold),
        )
        kept = 0
        for result in results:
            if kept >= max_hits:
                break
            entry_id = int(result.id)
            # 只保留这本书里仍然参与且已同步的候选：丢掉孤儿点、停用 / 改成常驻 / 已不参与的。
            if entry_id in book_candidates:
                scores[entry_id] = float(result.score)
                kept += 1
    return scores


async def semantic_hits(
    books: Sequence[DBWbBook],
    entries: Sequence[DBWbEntry],
    window: Sequence[WindowMessage],
    *,
    requester: tuple[str, str] | None,
) -> dict[int, float]:
    """这一轮语义命中的条目 id 到相似度；没有候选、超时或出错时返回空字典"""
    try:
        return await asyncio.wait_for(
            _semantic_hits(books, entries, window, requester=requester),
            timeout=SEMANTIC_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError as error:
        _log_semantic_error(error)
        return {}
    except Exception as error:  # noqa: BLE001 - 语义触发绝不能让一轮对话失败
        _log_semantic_error(error)
        return {}


async def backfill_legacy_vectorized() -> int:
    """把旧导入留在 `ext.vectorized` 里的标记回填到列上（启动时跑一次）

    分块读全部条目的 `id, ext`；`ext.get("vectorized") is True` 的置列。跑完在全局配置里
    写隐藏标记 `WORLDBOOK_VECTOR_BACKFILLED = True`，之后启动不再跑。失败时不写标记，
    下次启动重试；已经回填过的条目重复置 True 没有副作用。
    """
    if bool(getattr(config, "WORLDBOOK_VECTOR_BACKFILLED", False)):
        return 0

    total = 0
    last_id = 0
    while True:
        rows = list(
            await DBWbEntry.filter(id__gt=last_id)
            .order_by("id")
            .limit(500)
            .values("id", "ext"),
        )
        if not rows:
            break
        ids = [
            int(row["id"])
            for row in rows
            if isinstance(row.get("ext"), dict) and row["ext"].get("vectorized") is True
        ]
        if ids:
            total += await DBWbEntry.filter(id__in=ids).update(vectorized=True)
        last_id = int(rows[-1]["id"])

    setattr(config, "WORLDBOOK_VECTOR_BACKFILLED", True)
    if total:
        logger.info(f"世界书存量向量激活回填：回填了 {total} 条")
    return total


class VectorSyncWatcher:
    """在后台把待同步条目慢慢补上向量的看护循环

    启动后先睡 30 秒（避开启动时的数据库 / Qdrant 初始化）；之后每轮 `sync_pending()`，
    有成果就 2 秒后接着跑，没有待办就睡 60 秒；连续失败按 60 → 120 → 300 秒退避。
    `kick()` 让循环立刻醒一次：保存条目 / 书之后只叫醒它，不在请求路径上算向量。
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    def start(self) -> None:
        """启动看护循环（已启动时什么也不做）"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """停掉看护循环（关停时调用；没启动过时什么也不做）"""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as error:  # noqa: BLE001 - 关停路径不往外抛
            logger.warning(f"世界书向量同步看护停止时出错（已忽略）: {error!r}")

    def kick(self) -> None:
        """叫醒看护循环，立刻检查一次待办"""
        self._wake.set()

    async def _sleep(self, seconds: float) -> None:
        """睡一会儿；`kick()` 可以让它提前醒来"""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except TimeoutError:
            return
        self._wake.clear()

    async def _run(self) -> None:
        failures = 0
        try:
            await self._sleep(WATCHER_INITIAL_DELAY_SECONDS)
            while True:
                try:
                    count = await sync_pending()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - 看护循环不能因一次失败退出
                    delay = WATCHER_BACKOFF_SECONDS[min(failures, len(WATCHER_BACKOFF_SECONDS) - 1)]
                    failures += 1
                    logger.warning(f"世界书向量同步失败，{delay:.0f} 秒后重试: {error!r}")
                    await self._sleep(delay)
                    continue
                failures = 0
                await self._sleep(
                    WATCHER_ACTIVE_DELAY_SECONDS if count > 0 else WATCHER_IDLE_DELAY_SECONDS,
                )
        except asyncio.CancelledError:
            raise


vector_sync_watcher = VectorSyncWatcher()


def kick() -> None:
    """叫醒后台同步：保存条目 / 书之后只做这一件事，不在请求路径上算向量"""
    vector_sync_watcher.kick()


__all__ = [
    "COLLECTION",
    "EMBED_MAX_CHARS",
    "QUERY_CACHE_MAX",
    "QUERY_CACHE_TTL_SECONDS",
    "SEMANTIC_TIMEOUT_SECONDS",
    "VectorSyncWatcher",
    "backfill_legacy_vectorized",
    "content_hash",
    "delete_books",
    "delete_entries",
    "embed_source",
    "ensure_collection",
    "kick",
    "participates",
    "participation_counts",
    "semantic_hits",
    "sync_pending",
    "vector_sync_watcher",
]
