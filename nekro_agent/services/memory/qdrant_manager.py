"""Qdrant 向量数据库管理服务

负责记忆系统的 Collection 管理、向量存储与检索。
"""

import asyncio
from typing import Any

from qdrant_client import models as qdrant_models
from qdrant_client.http.exceptions import UnexpectedResponse

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.vector_db import get_qdrant_client
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.services.memory.embedding_service import embed_batch, get_memory_embedding_dimension
from nekro_agent.services.memory.instance_scope import NO_PRESET_ID, MemoryScope

logger = get_sub_logger("memory.qdrant")

# 「不属于任何人设」在 payload 里的取值。**别在这里写第二个字面量**——数据库侧与向量侧
# 一旦分叉，同一次检索的两条腿会给出不同的可见集合。`instance_scope` 是两侧共同的下层
# （本模块已经从它 import `MemoryScope`），直接取它那一份。
# 仍然不 import `identity_scope.PRESET_NONE`：那会把模型层整条导入链拖进启动早期。
PRESET_NONE_PAYLOAD = NO_PRESET_ID

# Collection 名称常量
MEMORY_PARAGRAPH_COLLECTION = "nekro_memory_paragraphs"
MAX_UPSERT_RETRIES = 3
UPSERT_RETRY_DELAY = 0.5
# 单次 Qdrant 删除请求携带的 point 数量。逐条删等于每条记忆一次 HTTP 往返，
# 清理上千条时会把事件循环钉在网络等待上。
DELETE_CHUNK_SIZE = 256

class MemoryQdrantManager:
    """记忆系统 Qdrant 管理器

    提供 Collection 初始化、向量 CRUD 操作。
    """

    def __init__(self, collection_name: str = MEMORY_PARAGRAPH_COLLECTION):
        self.collection_name = collection_name
        self._initialized = False

    @staticmethod
    def _is_retryable_qdrant_error(error: Exception) -> bool:
        """判断是否为可重试的 Qdrant 临时错误"""
        if isinstance(error, UnexpectedResponse):
            if error.status_code >= 500:
                return True
            if "Please retry" in str(error):
                return True
        return False

    async def _upsert_points_with_retry(
        self,
        points: list[qdrant_models.PointStruct],
    ) -> None:
        """执行带重试的 Qdrant upsert"""
        client = await get_qdrant_client()
        if client is None:
            raise RuntimeError("Qdrant 客户端不可用")

        last_error: Exception | None = None
        for attempt in range(MAX_UPSERT_RETRIES):
            try:
                await client.upsert(
                    collection_name=self.collection_name,
                    points=points,
                )
                return
            except Exception as e:
                last_error = e
                if not self._is_retryable_qdrant_error(e) or attempt >= MAX_UPSERT_RETRIES - 1:
                    raise
                logger.warning(
                    f"Qdrant upsert 临时失败，准备重试: "
                    f"attempt={attempt + 1}/{MAX_UPSERT_RETRIES}, error={e}",
                )
                await asyncio.sleep(UPSERT_RETRY_DELAY * (attempt + 1))

        raise last_error or RuntimeError("Qdrant upsert 失败")

    async def _ensure_payload_indexes(self, client: Any) -> None:
        """为检索过滤用到的 payload 字段建立索引

        缺少索引时 Qdrant 只能全量扫描 payload 完成过滤，而记忆检索发生在每次提示词渲染的
        同步路径上（见 `services/agent/templates/history.py`），段落量上万后延迟会显著劣化。
        对已存在的索引重复调用会报错，直接忽略即可。
        """
        for field_name, field_schema in (
            ("workspace_id", qdrant_models.PayloadSchemaType.INTEGER),
            ("instance_key", qdrant_models.PayloadSchemaType.KEYWORD),
            ("preset_id", qdrant_models.PayloadSchemaType.INTEGER),
            ("paragraph_id", qdrant_models.PayloadSchemaType.INTEGER),
            ("cognitive_type", qdrant_models.PayloadSchemaType.KEYWORD),
            ("memory_source", qdrant_models.PayloadSchemaType.KEYWORD),
            ("is_inactive", qdrant_models.PayloadSchemaType.BOOL),
            ("event_time", qdrant_models.PayloadSchemaType.INTEGER),
        ):
            try:
                await client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field_name,
                    field_schema=field_schema,
                )
            except Exception as e:
                logger.debug(f"记忆 payload index 跳过: field={field_name}, error={e}")

    async def ensure_collection(self, dimension: int | None = None) -> bool:
        """确保 Collection 存在，不存在则创建

        Args:
            dimension: 向量维度，未指定时读取当前配置

        Returns:
            是否新创建了 Collection
        """
        resolved_dimension = dimension or get_memory_embedding_dimension()
        client = await get_qdrant_client()
        if client is None:
            logger.warning("Qdrant 客户端不可用，跳过 Collection 初始化")
            return False

        try:
            collections = await client.get_collections()
            existing_names = [c.name for c in collections.collections]

            if self.collection_name in existing_names:
                info = await client.get_collection(self.collection_name)
                current_size = None
                try:
                    current_size = info.config.params.vectors.size
                except Exception:
                    current_size = None

                if current_size is not None and current_size != resolved_dimension:
                    points_count = int(info.points_count or 0)
                    if points_count == 0:
                        logger.warning(
                            f"记忆 Collection 维度不匹配且当前为空，准备重建: "
                            f"current={current_size}, expected={resolved_dimension}",
                        )
                        await client.delete_collection(self.collection_name)
                    else:
                        logger.warning(
                            f"记忆 Collection 维度不匹配，但已有数据，跳过自动重建: "
                            f"current={current_size}, expected={resolved_dimension}, points={points_count}",
                        )
                        await self._ensure_payload_indexes(client)
                        self._initialized = True
                        return False
                else:
                    logger.debug(f"Collection {self.collection_name} 已存在")
                    await self._ensure_payload_indexes(client)
                    self._initialized = True
                    return False

            if self.collection_name in existing_names:
                collections = await client.get_collections()
                existing_names = [c.name for c in collections.collections]
                if self.collection_name in existing_names:
                    logger.debug(f"Collection {self.collection_name} 已存在")
                    await self._ensure_payload_indexes(client)
                    self._initialized = True
                    return False

            # 创建新 Collection
            await client.create_collection(
                collection_name=self.collection_name,
                vectors_config=qdrant_models.VectorParams(
                    size=resolved_dimension,
                    distance=qdrant_models.Distance.COSINE,
                ),
            )
            await self._ensure_payload_indexes(client)
            logger.info(f"记忆系统 Collection {self.collection_name} 创建成功")
            self._initialized = True
            return True

        except Exception as e:
            logger.exception(f"初始化 Collection 失败: {e}")
            raise

    async def upsert_paragraph(
        self,
        paragraph_id: int,
        embedding: list[float],
        payload: dict[str, Any],
    ) -> bool:
        """插入或更新段落向量

        Args:
            paragraph_id: 段落 ID（作为 Qdrant point ID）
            embedding: 向量数据
            payload: 元数据

        Returns:
            是否成功
        """
        client = await get_qdrant_client()
        if client is None:
            logger.warning("Qdrant 客户端不可用")
            return False

        try:
            await self._upsert_points_with_retry(
                [
                    qdrant_models.PointStruct(
                        id=paragraph_id,
                        vector=embedding,
                        payload=payload,
                    ),
                ]
            )
            logger.debug(f"向量 upsert 成功: paragraph_id={paragraph_id}")
            return True
        except Exception as e:
            logger.exception(f"向量 upsert 失败: {e}")
            return False

    async def batch_upsert(
        self,
        points: list[tuple[int, list[float], dict[str, Any]]],
        batch_size: int = 100,
    ) -> int:
        """批量插入向量

        Args:
            points: (paragraph_id, embedding, payload) 元组列表
            batch_size: 批次大小

        Returns:
            成功插入的数量
        """
        client = await get_qdrant_client()
        if client is None:
            return 0

        success_count = 0
        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            try:
                await self._upsert_points_with_retry(
                    [
                        qdrant_models.PointStruct(
                            id=pid,
                            vector=emb,
                            payload=pl,
                        )
                        for pid, emb, pl in batch
                    ]
                )
                success_count += len(batch)
            except Exception as e:
                logger.exception(f"批量 upsert 失败: {e}")

        return success_count

    async def set_paragraph_instance_key(self, paragraph_ids: list[int], instance_key: str) -> int:
        """批量改写段落向量 payload 中的 instance_key

        标识升级后回填了段落行的 instance_key，向量侧若不同步，按实例过滤的检索会漏掉这批记忆。
        这里只覆盖单个 payload 字段，向量本身不动，因此不需要重新计算 embedding。

        Args:
            paragraph_ids: 需要改写的段落 ID（即 Qdrant point ID）
            instance_key: 目标实例标识

        Returns:
            成功改写的点数量；Qdrant 不可用或写入失败时返回 0
        """
        if not paragraph_ids:
            return 0

        client = await get_qdrant_client()
        if client is None:
            logger.warning(f"Qdrant 客户端不可用，跳过 instance_key payload 同步: count={len(paragraph_ids)}")
            return 0

        try:
            await client.set_payload(
                collection_name=self.collection_name,
                payload={"instance_key": instance_key},
                points=list(paragraph_ids),
                wait=True,
            )
        except Exception as e:
            logger.warning(f"instance_key payload 同步失败: count={len(paragraph_ids)}, error={e}")
            return 0

        logger.info(f"instance_key payload 同步完成: count={len(paragraph_ids)}, instance_key={instance_key}")
        return len(paragraph_ids)

    @staticmethod
    def _build_preset_condition(preset_id: int) -> Any:
        """人设维的向量侧过滤条件

        判据必须与数据库侧的 :meth:`MemoryScope.as_filter` **逐字对应**，否则同一次检索
        的两条腿会给出不同的可见集合：向量召回一批 ID，SQL 侧再按条件取回行，
        松的那一侧白召回、紧的那一侧静默丢结果。那边是 `preset_id in (当前人设, 0)`，
        这边就得是三档：

        - 等于当前人设；
        - 等于 0（「不属于任何人设」：存量记忆、控制台直发 CC，理由全在
          `MemoryScope.as_filter` 的 docstring 里，不在这里复述第二遍）；
        - **payload 里没有 `preset_id` 这个 key**——改造前写入的 point 就是这样，
          而 Qdrant 的 `must` 对缺字段的 point 判为**不匹配**。少了这一档，升级那一刻
          每一个存量向量都会落选，表现为 Bot 集体失忆，没有任何报错指向这一行。
          它与数据库侧的 `DEFAULT 0` 是同一件事：那边存量行读出来是 0，
          这边把"没有这个 key"读成同一个 0。
        """
        should: list[Any] = [
            qdrant_models.FieldCondition(
                key="preset_id",
                match=qdrant_models.MatchValue(value=preset_id),
            ),
        ]
        if preset_id != PRESET_NONE_PAYLOAD:
            should.append(
                qdrant_models.FieldCondition(
                    key="preset_id",
                    match=qdrant_models.MatchValue(value=PRESET_NONE_PAYLOAD),
                ),
            )
        should.append(
            qdrant_models.IsEmptyCondition(
                is_empty=qdrant_models.PayloadField(key="preset_id"),
            ),
        )
        return qdrant_models.Filter(should=should)

    async def search(
        self,
        query_vector: list[float],
        workspace_id: int,
        limit: int = 10,
        score_threshold: float = 0.5,
        cognitive_type: str | None = None,
        include_inactive: bool = False,
        event_time_from: int | None = None,
        event_time_to: int | None = None,
        *,
        scope: MemoryScope,
    ) -> list[dict[str, Any]]:
        """语义搜索记忆段落

        Args:
            query_vector: 查询向量
            workspace_id: 工作区 ID（必须，隔离边界）
            limit: 返回数量
            score_threshold: 相似度阈值
            cognitive_type: 可选，过滤认知类型
            include_inactive: 是否包含已失活记忆
            scope: 可见范围（域换算出的实例集合 + 人设），**必填**。
                两维各自的 `None` 表示该维不过滤；要全量请显式传
                `MemoryScope.unrestricted()`。

        Returns:
            搜索结果列表
        """
        client = await get_qdrant_client()
        if client is None:
            return []

        # 构建过滤条件
        must_conditions: list[Any] = [
            qdrant_models.FieldCondition(
                key="workspace_id",
                match=qdrant_models.MatchValue(value=workspace_id),
            ),
        ]

        if scope.allowed_instance_keys is not None:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="instance_key",
                    match=qdrant_models.MatchAny(any=list(scope.allowed_instance_keys)),
                ),
            )

        if scope.preset_id is not None:
            must_conditions.append(self._build_preset_condition(scope.preset_id))

        if not include_inactive:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="is_inactive",
                    match=qdrant_models.MatchValue(value=False),
                ),
            )

        if cognitive_type:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="cognitive_type",
                    match=qdrant_models.MatchValue(value=cognitive_type),
                ),
            )

        if event_time_from is not None:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="event_time",
                    range=qdrant_models.Range(gte=event_time_from),
                ),
            )

        if event_time_to is not None:
            must_conditions.append(
                qdrant_models.FieldCondition(
                    key="event_time",
                    range=qdrant_models.Range(lte=event_time_to),
                ),
            )

        try:
            results = await client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                query_filter=qdrant_models.Filter(must=must_conditions),
                limit=limit,
                score_threshold=score_threshold,
            )

            return [
                {
                    "id": r.id,
                    "score": r.score,
                    "payload": r.payload,
                }
                for r in results
            ]
        except Exception as e:
            logger.exception(f"向量搜索失败: {e}")
            return []

    async def delete_paragraph(self, paragraph_id: int) -> bool:
        """删除段落向量

        Args:
            paragraph_id: 段落 ID

        Returns:
            是否成功
        """
        client = await get_qdrant_client()
        if client is None:
            return False

        try:
            await client.delete(
                collection_name=self.collection_name,
                points_selector=qdrant_models.PointIdsList(
                    points=[paragraph_id],
                ),
            )
            logger.debug(f"向量删除成功: paragraph_id={paragraph_id}")
            return True
        except Exception as e:
            logger.exception(f"向量删除失败: {e}")
            return False

    async def delete_paragraphs(self, paragraph_ids: list[int]) -> int:
        """批量删除段落向量

        Args:
            paragraph_ids: 段落 ID 列表（即 Qdrant point ID）

        Returns:
            成功提交删除的 point 数量；Qdrant 不可用时返回 0
        """
        if not paragraph_ids:
            return 0

        client = await get_qdrant_client()
        if client is None:
            logger.warning(f"Qdrant 客户端不可用，跳过向量删除: count={len(paragraph_ids)}")
            return 0

        deleted = 0
        for start in range(0, len(paragraph_ids), DELETE_CHUNK_SIZE):
            chunk = paragraph_ids[start : start + DELETE_CHUNK_SIZE]
            try:
                await client.delete(
                    collection_name=self.collection_name,
                    points_selector=qdrant_models.PointIdsList(points=list(chunk)),
                )
            except Exception as e:
                logger.warning(f"批量向量删除失败: count={len(chunk)}, error={e}")
                continue
            deleted += len(chunk)
            await asyncio.sleep(0)

        logger.debug(f"批量向量删除完成: requested={len(paragraph_ids)}, deleted={deleted}")
        return deleted

    async def delete_by_workspace(self, workspace_id: int) -> bool:
        """删除工作区所有向量（工作区清理时使用）

        Args:
            workspace_id: 工作区 ID

        Returns:
            是否成功
        """
        client = await get_qdrant_client()
        if client is None:
            return False

        try:
            await client.delete(
                collection_name=self.collection_name,
                points_selector=qdrant_models.FilterSelector(
                    filter=qdrant_models.Filter(
                        must=[
                            qdrant_models.FieldCondition(
                                key="workspace_id",
                                match=qdrant_models.MatchValue(value=workspace_id),
                            ),
                        ],
                    ),
                ),
            )
            logger.info(f"工作区 {workspace_id} 的所有向量已删除")
            return True
        except Exception as e:
            logger.exception(f"删除工作区向量失败: {e}")
            return False

    async def get_collection_info(self) -> dict[str, Any] | None:
        """获取 Collection 信息（用于监控）"""
        client = await get_qdrant_client()
        if client is None:
            return None

        try:
            info = await client.get_collection(self.collection_name)
            return {
                "name": self.collection_name,
                "vectors_count": info.vectors_count,
                "points_count": info.points_count,
                "status": info.status.name,
            }
        except Exception as e:
            logger.exception(f"获取 Collection 信息失败: {e}")
            return None

    async def rebuild_collection(self, batch_size: int = 50) -> dict[str, int]:
        """重建记忆库 Qdrant 索引"""
        client = await get_qdrant_client()
        if client is None:
            raise RuntimeError("Qdrant 客户端不可用")

        dimension = get_memory_embedding_dimension()
        collections = await client.get_collections()
        existing_names = [collection.name for collection in collections.collections]

        if self.collection_name in existing_names:
            await client.delete_collection(collection_name=self.collection_name)
            logger.info(f"已删除现有记忆 Collection: {self.collection_name}")

        await client.create_collection(
            collection_name=self.collection_name,
            vectors_config=qdrant_models.VectorParams(
                size=dimension,
                distance=qdrant_models.Distance.COSINE,
            ),
        )
        # `delete_collection` 把 payload 索引一起带走了，重建时必须在这里原样补回来：
        # 补索引的另一处是 `ensure_collection`，而它**整个进程只被调用一次**
        # （`nekro_agent/__init__.py` 的 `_init_memory_scheduler`）。所以一条 `memory_reindex -y`
        # 之后，索引会一直缺到下次进程重启为止。少了 `instance_key` / `workspace_id` 索引不会让
        # 过滤失效（Qdrant 会退化成全量扫 payload），但记忆检索发生在每次提示词渲染的同步路径上。
        await self._ensure_payload_indexes(client)
        logger.info(f"已创建记忆 Collection: {self.collection_name} (dimension={dimension})")

        # 按主键游标分页，不把整表拉进内存；每页内部走 embed_batch 并发向量化，
        # 再一次性 upsert + 批量回写 embedding_ref，最后让出一次事件循环。
        page_size = max(1, batch_size)
        total_count = 0
        success_count = 0
        error_count = 0
        skipped_count = 0
        cursor = 0

        while True:
            page = await DBMemParagraph.filter(id__gt=cursor).order_by("id").limit(page_size)
            if not page:
                break
            cursor = page[-1].id
            total_count += len(page)

            pending = [paragraph for paragraph in page if paragraph.content.strip()]
            skipped_count += len(page) - len(pending)
            if not pending:
                await asyncio.sleep(0)
                continue

            embeddings = await embed_batch([paragraph.content.strip() for paragraph in pending])

            points: list[qdrant_models.PointStruct] = []
            embedded: list[DBMemParagraph] = []
            for paragraph, embedding in zip(pending, embeddings, strict=True):
                if embedding is None:
                    logger.warning(f"重建记忆索引失败: paragraph_id={paragraph.id}, error=embedding 生成失败")
                    error_count += 1
                    continue
                points.append(
                    qdrant_models.PointStruct(
                        id=paragraph.id,
                        vector=embedding,
                        payload=paragraph.to_qdrant_payload(),
                    ),
                )
                paragraph.embedding_ref = str(paragraph.id)
                embedded.append(paragraph)

            if points:
                try:
                    await self._upsert_points_with_retry(points)
                except Exception as e:
                    logger.warning(f"重建记忆索引批次写入失败: count={len(points)}, error={e}")
                    error_count += len(points)
                else:
                    await DBMemParagraph.bulk_update(
                        embedded,
                        fields=["embedding_ref", "update_time"],
                        batch_size=page_size,
                    )
                    success_count += len(points)

            await asyncio.sleep(0)

        self._initialized = True
        return {
            "total": total_count,
            "success": success_count,
            "error": error_count,
            "skipped": skipped_count,
            "dimension": dimension,
        }


# 全局单例
memory_qdrant_manager = MemoryQdrantManager()
