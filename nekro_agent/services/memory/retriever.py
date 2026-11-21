"""记忆检索服务

提供语义检索能力，为 NA 上下文注入提供记忆支持。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from tortoise.expressions import Q

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph
from nekro_agent.models.db_mem_reinforcement_log import (
    DBMemReinforcementLog,
    TargetType,
    TriggerSource,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory.embedding_service import embed_text
from nekro_agent.services.memory.feature_flags import is_memory_system_enabled
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
from nekro_agent.services.memory.recall_contract import (
    MEMORY_CONTEXT_BLOCK_TITLE,
    MEMORY_CONTEXT_CATEGORY_LABELS,
    MEMORY_CONTEXT_CORE_SECTION,
    MEMORY_CONTEXT_EVIDENCE_SECTION,
    MemoryAnswerStyle,
    MemoryIntentType,
    MemoryKnowledgeHint,
    MemoryTypeHint,
)
from nekro_agent.services.memory.text_terms import extract_query_terms, lexical_coverage
from nekro_agent.tools.untrusted_content import sanitize_inline_text

logger = get_sub_logger("memory.retriever")

# 同一条记忆在该窗口内被反复命中时只强化一次，避免高频会话产生大量重复写库
REINFORCE_COOLDOWN_SECONDS = 300


@dataclass(frozen=True)
class _LexicalBand:
    """把字面命中折算到向量相似度量纲上所用的区间。

    改造前关系与 Episode 的匹配分是硬编码常量（基线 0.55 / 0.58），而向量检索的准入阈值是
    `MEMORY_RETRIEVAL_MIN_SIMILARITY`（默认 0.5）：只要切词命中任意一个词，字面候选就带着
    0.55 挤进候选集，并在 `_select_context_memories` 里以 `similarity_score` 为主排序键排到
    真正的语义命中前面——一条只是碰巧含某个词的记忆因此系统性地压过语义上真正相关的记忆。

    这里改成把覆盖率线性映射到本次查询中向量通道**实际占据**的区间：
    - 下界取向量检索的准入阈值，覆盖率趋近 0 的字面候选因此沉到所有语义命中之下；
    - 上界取本次查询拿到的最高向量得分，完全字面包含时最多与最好的语义命中打平，压不过它。

    两个端点都是系统里已经存在的量，不引入新的经验常数。
    """

    floor: float
    ceiling: float

    def calibrate(self, coverage: float) -> float:
        """把 [0, 1] 的字面覆盖率折算成与向量相似度可比的分数。"""
        return self.floor + (self.ceiling - self.floor) * min(1.0, max(0.0, coverage))


@dataclass
class RetrievedMemory:
    """检索到的记忆"""

    target_id: int
    source_type: str
    episode_id: int | None
    paragraph_id: int | None
    relation_id: int | None
    content: str
    summary: str
    cognitive_type: str
    knowledge_type: str
    similarity_score: float
    effective_weight: float
    event_time: datetime | None
    origin_chat_key: str | None


@dataclass
class RetrievalResult:
    """检索结果"""

    memories: list[RetrievedMemory]
    query_embedding_time_ms: float
    search_time_ms: float
    total_candidates: int


@dataclass
class MemoryRecallQuery:
    """面向上下文注入的检索查询。"""

    query_text: str
    focus_text: str
    focus_points: list[str]
    context_texts: list[str]
    intent_type: MemoryIntentType = MemoryIntentType.MIXED
    answer_style: MemoryAnswerStyle = MemoryAnswerStyle.CORE_PLUS_EVIDENCE
    prefer_memory_types: list[MemoryTypeHint] | None = None
    prefer_knowledge_types: list[MemoryKnowledgeHint] | None = None
    avoid_knowledge_types: list[MemoryKnowledgeHint] | None = None
    entity_hints: list[str] | None = None
    time_from: datetime | None = None
    time_to: datetime | None = None


class MemoryRetriever:
    """记忆检索器

    职责：
    1. 语义检索相关记忆
    2. 计算有效权重
    3. 记录访问日志（用于后续强化）
    """

    def __init__(self, workspace_id: int, *, scope: MemoryScope):
        """初始化检索器

        Args:
            workspace_id: 工作区 ID
            scope: 本次检索的可见范围（域换算出的实例集合 + 人设）。
                **必填**：默认成"不过滤"正是 #45 的形状——少传一个参数，
                过滤条件整个消失而检索照常返回结果、不报任何错。要全量请显式写
                `MemoryScope.unrestricted()`。
        """
        self.workspace_id = workspace_id
        self.scope = scope

    @property
    def _scope_filter(self) -> dict[str, Any]:
        """隔离维度的查询过滤条件（域 + 人设），未设限的维度不出现在条件里。

        注意：域换算出的允许集合里会包含空串，以保证升级前写入、`instance_key`
        为空的存量记忆仍可召回（`instance_scope.LEGACY_INSTANCE_KEY`）。
        """
        return self.scope.as_filter()

    async def retrieve(
        self,
        query: str,
        limit: int | None = None,
        min_similarity: float | None = None,
        cognitive_type: CognitiveType | None = None,
        include_inactive: bool = False,
        record_access: bool = True,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> RetrievalResult:
        """检索相关记忆

        Args:
            query: 查询文本
            limit: 返回数量上限
            min_similarity: 最低相似度阈值
            cognitive_type: 可选，过滤认知类型
            include_inactive: 是否包含已失活记忆
            record_access: 是否记录访问日志

        Returns:
            检索结果
        """
        if not is_memory_system_enabled():
            return RetrievalResult(
                memories=[],
                query_embedding_time_ms=0,
                search_time_ms=0,
                total_candidates=0,
            )

        resolved_limit = limit if limit is not None else config.MEMORY_RETRIEVAL_DEFAULT_LIMIT
        resolved_min_similarity = (
            min_similarity if min_similarity is not None else config.MEMORY_RETRIEVAL_MIN_SIMILARITY
        )
        # 时间窗先统一成 aware，再往下发。入参**可能是朴素值**：规划模型吐的 JSON
        # （`recall_contract.RecallQuery.time_from`）和管理端 query 参数里不带偏移量的 ISO 串
        # 都会解析成 naive。而下面两条通道对同一个朴素值的解释**不一样**——
        # 向量通道走 `int(value.timestamp())`，Python 按**进程本地时区**折算；Episode 通道把它
        # 交给 Tortoise 与 `DatetimeField` 比较，`use_tz=True` 下按 **UTC** 解释。
        # 不统一的话同一次检索里两条通道的时间窗会差一个时区偏移量（#5h 只修了写入侧，
        # 这里是拿去与 `event_time` 比较的那一半）。`_normalize_datetime` 的口径与 ORM 一致
        # （朴素 = UTC），传 aware 值时是同一时刻的等价改写，行为逐字不变。
        resolved_time_from = self._normalize_datetime(time_from) if time_from else None
        resolved_time_to = self._normalize_datetime(time_to) if time_to else None
        # 生成查询向量
        t0 = time.perf_counter()
        try:
            query_embedding = await embed_text(query)
        except Exception as e:
            logger.warning(f"查询向量化失败: {e}")
            return RetrievalResult(
                memories=[],
                query_embedding_time_ms=0,
                search_time_ms=0,
                total_candidates=0,
            )
        embedding_time = (time.perf_counter() - t0) * 1000

        # 向量搜索
        t1 = time.perf_counter()
        search_results = await memory_qdrant_manager.search(
            query_vector=query_embedding,
            workspace_id=self.workspace_id,
            limit=resolved_limit * 2,  # 多取一些用于后处理
            score_threshold=resolved_min_similarity,
            cognitive_type=cognitive_type.value if cognitive_type else None,
            include_inactive=include_inactive,
            event_time_from=int(resolved_time_from.timestamp()) if resolved_time_from else None,
            event_time_to=int(resolved_time_to.timestamp()) if resolved_time_to else None,
            scope=self.scope,
        )
        search_time = (time.perf_counter() - t1) * 1000

        # 获取段落详情并计算有效权重
        paragraph_candidates: dict[int, RetrievedMemory] = {}
        paragraph_ids = [r["id"] for r in search_results]

        paragraphs = await DBMemParagraph.filter(
            id__in=paragraph_ids,
            workspace_id=self.workspace_id,
            **self._scope_filter,
        ).all()
        paragraph_map = {p.id: p for p in paragraphs}

        for result in search_results:
            para_id = result["id"]
            if para_id not in paragraph_map:
                continue

            paragraph = paragraph_map[para_id]
            similarity = result["score"]

            # 计算有效权重
            effective_weight = self._compute_effective_weight(paragraph, similarity)

            paragraph_candidates[para_id] = RetrievedMemory(
                target_id=para_id,
                source_type="paragraph",
                episode_id=paragraph.episode_id,
                paragraph_id=para_id,
                relation_id=None,
                content=paragraph.content,
                summary=paragraph.summary or "",
                cognitive_type=paragraph.cognitive_type.value,
                knowledge_type=paragraph.knowledge_type.value,
                similarity_score=similarity,
                effective_weight=effective_weight,
                event_time=paragraph.event_time,
                origin_chat_key=paragraph.origin_chat_key,
            )

        # 字面召回的分数区间要贴着本次查询里向量通道真实占据的区间，见 `_LexicalBand`。
        # 向量通道这次一条都没命中时没有可压制的对象，放开到满量程，否则字面候选会全挤在
        # 阈值上、彼此失去区分度。
        lexical_band = _LexicalBand(
            floor=resolved_min_similarity,
            ceiling=(
                max(resolved_min_similarity, max(result["score"] for result in search_results))
                if search_results
                else 1.0
            ),
        )

        relation_memories = await self._retrieve_relation_memories(
            query=query,
            limit=resolved_limit,
            paragraph_candidates=paragraph_candidates,
            include_inactive=include_inactive,
            lexical_band=lexical_band,
        )
        episode_memories = await self._retrieve_episode_memories(
            query=query,
            limit=max(2, min(4, resolved_limit)),
            paragraph_candidates=paragraph_candidates,
            include_inactive=include_inactive,
            time_from=resolved_time_from,
            time_to=resolved_time_to,
            lexical_band=lexical_band,
        )

        memories = list(paragraph_candidates.values()) + relation_memories + episode_memories
        # 按有效权重排序
        memories.sort(key=lambda m: m.effective_weight, reverse=True)
        memories = memories[:resolved_limit]

        # 记录访问日志
        if record_access and memories:
            await self._record_access(memories, query)

        logger.debug(
            f"记忆检索完成: query={query[:30]}..., "
            f"results={len(memories)}, embedding={embedding_time:.1f}ms, search={search_time:.1f}ms",
        )

        return RetrievalResult(
            memories=memories,
            query_embedding_time_ms=embedding_time,
            search_time_ms=search_time,
            total_candidates=len(search_results) + len(relation_memories) + len(episode_memories),
        )

    def _compute_effective_weight(
        self,
        paragraph: DBMemParagraph,
        similarity: float,
    ) -> float:
        """计算有效权重

        综合考虑：
        - 基础权重（衰减后）
        - 相似度分数
        - 认知类型加成
        - 近期记忆加成
        """
        # 基础有效权重（包含衰减）
        base_weight = paragraph.compute_effective_weight(time.time())

        # 相似度加权
        score = base_weight * similarity

        # 情景记忆加成（更具体，更有价值）
        if paragraph.cognitive_type == CognitiveType.EPISODIC:
            score *= config.MEMORY_RETRIEVAL_EPISODIC_BOOST

        # 近期记忆加成
        if paragraph.event_time:
            event_time = self._normalize_datetime(paragraph.event_time)
            hours_ago = (datetime.now(timezone.utc) - event_time).total_seconds() / 3600
            if hours_ago < config.MEMORY_RETRIEVAL_RECENT_BOOST_HOURS:
                score *= config.MEMORY_RETRIEVAL_RECENT_BOOST_FACTOR

        return score

    @staticmethod
    def _normalize_datetime(value: datetime) -> datetime:
        """统一转为 UTC aware datetime，避免 naive/aware 混算。"""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _retrieve_relation_memories(
        self,
        query: str,
        limit: int,
        paragraph_candidates: dict[int, RetrievedMemory],
        include_inactive: bool,
        lexical_band: _LexicalBand,
    ) -> list[RetrievedMemory]:
        """从实体关系图谱中检索相关记忆，并对关联段落进行加权。"""
        terms = extract_query_terms(query)
        if not terms:
            return []

        entity_query = Q()
        for term in terms[:5]:
            entity_query |= Q(canonical_name__icontains=term) | Q(name__icontains=term)

        matched_entities = await DBMemEntity.filter(
            entity_query,
            workspace_id=self.workspace_id,
            is_inactive=False,
            **self._scope_filter,
        ).limit(config.MEMORY_RETRIEVAL_RELATION_MATCH_LIMIT)
        entity_map = {entity.id: entity for entity in matched_entities}
        matched_ids = list(entity_map.keys())

        relation_query = Q()
        if matched_ids:
            relation_query |= Q(subject_entity_id__in=matched_ids) | Q(object_entity_id__in=matched_ids)
        for term in terms[:3]:
            relation_query |= Q(predicate__icontains=term)

        if not relation_query.children:
            return []

        relation_qs = DBMemRelation.filter(
            relation_query,
            workspace_id=self.workspace_id,
            **self._scope_filter,
        )
        if not include_inactive:
            relation_qs = relation_qs.filter(is_inactive=False)
        relations = await relation_qs.order_by("-update_time").limit(limit * 3)

        missing_entity_ids = {
            relation.subject_entity_id for relation in relations if relation.subject_entity_id not in entity_map
        } | {
            relation.object_entity_id for relation in relations if relation.object_entity_id not in entity_map
        }
        if missing_entity_ids:
            extra_entities = await DBMemEntity.filter(
                id__in=list(missing_entity_ids),
                workspace_id=self.workspace_id,
                **self._scope_filter,
            )
            entity_map.update({entity.id: entity for entity in extra_entities})

        relation_memories: list[RetrievedMemory] = []
        for relation in relations:
            subject_name = entity_map.get(relation.subject_entity_id).canonical_name if relation.subject_entity_id in entity_map else str(relation.subject_entity_id)
            object_name = entity_map.get(relation.object_entity_id).canonical_name if relation.object_entity_id in entity_map else str(relation.object_entity_id)

            coverage = lexical_coverage(query, subject_name, relation.predicate, object_name)
            if coverage <= 0:
                # 三元组的每个字段都参与了 SQL 侧的 `icontains` 匹配，覆盖率为 0 说明这条
                # 关系是被别的条件顺带捞出来的，没有任何属于自己的字面证据。
                continue
            match_score = lexical_band.calibrate(coverage)
            effective_weight = (
                relation.compute_effective_weight(time.time())
                * match_score
                * config.MEMORY_RETRIEVAL_RELATION_BOOST
            )
            summary = f"{subject_name} - {relation.predicate} - {object_name}"
            content = f"关系记忆：{subject_name} {relation.predicate} {object_name}"

            # 关系只对**向量通道已经召回**的段落加权：它是旁证，不是段落自身的语义距离。
            # 改造前这里会把向量通道没选中的来源段落补建成候选，并直接把关系的字面分当成它的
            # `similarity_score`——被语义检索判为不相关的段落，就这样带着一个凭空造出来的
            # 相似度挤进注入块，实测里它甚至排到了第一位。段落进不来不等于线索丢失：关系
            # 本身就是候选，三元组文本照样会进上下文。
            if relation.paragraph_id:
                paragraph = paragraph_candidates.get(relation.paragraph_id)
                if paragraph is not None:
                    paragraph.effective_weight += effective_weight * 0.35

            relation_memories.append(
                RetrievedMemory(
                    target_id=relation.id,
                    source_type="relation",
                    episode_id=None,
                    paragraph_id=relation.paragraph_id,
                    relation_id=relation.id,
                    content=content,
                    summary=summary,
                    cognitive_type=relation.cognitive_type,
                    knowledge_type="relation",
                    similarity_score=match_score,
                    effective_weight=effective_weight,
                    event_time=None,
                    origin_chat_key=None,
                )
            )

        return relation_memories

    async def _retrieve_episode_memories(
        self,
        query: str,
        limit: int,
        paragraph_candidates: dict[int, RetrievedMemory],
        include_inactive: bool,
        time_from: datetime | None,
        time_to: datetime | None,
        lexical_band: _LexicalBand,
    ) -> list[RetrievedMemory]:
        """检索 Episode 级事件记忆，并给关联段落做轻量加权。"""
        terms = extract_query_terms(query)
        # 被段落命中带出来的 Episode，证据在段落的向量得分上而不在字面上，直接继承过来，
        # 不要凭空给它一个字面基线分。
        inherited_similarity: dict[int, float] = {}
        for mem in paragraph_candidates.values():
            if mem.source_type != "paragraph" or mem.episode_id is None:
                continue
            inherited_similarity[mem.episode_id] = max(
                inherited_similarity.get(mem.episode_id, 0.0),
                mem.similarity_score,
            )
        candidate_ids = set(inherited_similarity)

        query_q = Q()
        for term in terms[:4]:
            query_q |= Q(title__icontains=term) | Q(narrative_summary__icontains=term)

        episode_qs = DBMemEpisode.filter(workspace_id=self.workspace_id, **self._scope_filter)
        if not include_inactive:
            episode_qs = episode_qs.filter(is_inactive=False)
        if time_from:
            episode_qs = episode_qs.filter(Q(time_end__gte=time_from) | Q(time_start__gte=time_from))
        if time_to:
            episode_qs = episode_qs.filter(Q(time_start__lte=time_to) | Q(time_end__lte=time_to))

        if candidate_ids and query_q.children:
            episodes = await episode_qs.filter(Q(id__in=list(candidate_ids)) | query_q).limit(limit * 4)
        elif candidate_ids:
            episodes = await episode_qs.filter(id__in=list(candidate_ids)).limit(limit * 3)
        elif query_q.children:
            episodes = await episode_qs.filter(query_q).limit(limit * 3)
        else:
            return []

        memories: list[RetrievedMemory] = []
        seen_ids: set[int] = set()
        for episode in episodes:
            if episode.id in seen_ids:
                continue
            seen_ids.add(episode.id)

            coverage = lexical_coverage(query, episode.title, episode.narrative_summary)
            match_score = max(
                inherited_similarity.get(episode.id, 0.0),
                lexical_band.calibrate(coverage) if coverage > 0 else 0.0,
            )
            if match_score <= 0:
                continue

            event_time = episode.time_end or episode.time_start
            recent_bonus = 1.0
            if event_time:
                hours_ago = (datetime.now(timezone.utc) - self._normalize_datetime(event_time)).total_seconds() / 3600
                if hours_ago < config.MEMORY_RETRIEVAL_RECENT_BOOST_HOURS:
                    recent_bonus = config.MEMORY_RETRIEVAL_RECENT_BOOST_FACTOR

            # Episode 就是情景记忆，加成沿用配置里的情景记忆系数，不再单独写死一个 1.18。
            effective_weight = (
                episode.base_weight * match_score * config.MEMORY_RETRIEVAL_EPISODIC_BOOST * recent_bonus
            )
            memories.append(
                RetrievedMemory(
                    target_id=episode.id,
                    source_type="episode",
                    episode_id=episode.id,
                    paragraph_id=None,
                    relation_id=None,
                    content=episode.narrative_summary,
                    summary=episode.title,
                    cognitive_type=CognitiveType.EPISODIC.value,
                    knowledge_type="experience",
                    similarity_score=match_score,
                    effective_weight=effective_weight,
                    event_time=event_time,
                    origin_chat_key=episode.origin_chat_key,
                )
            )

            for paragraph in paragraph_candidates.values():
                if paragraph.source_type == "paragraph" and paragraph.episode_id == episode.id:
                    paragraph.effective_weight += effective_weight * 0.18

        memories.sort(key=lambda mem: (-mem.effective_weight, -mem.similarity_score))
        return memories[:limit]

    async def record_access(self, memories: list[RetrievedMemory], query: str) -> None:
        """补记访问日志与强化。

        供 `retrieve(record_access=False)` 的调用方在检索结束后**串行**补记，
        理由见 `retrieve_memories_batch` 的 docstring。
        """
        await self._record_access(memories, query)

    async def _record_access(
        self,
        memories: list[RetrievedMemory],
        query: str,
    ) -> None:
        """记录访问日志并强化命中的记忆

        强化会重置衰减计时，这是记忆能长期留存的唯一途径：没有它，情景记忆的有效权重
        只会从创建时刻单向衰减，低于清理阈值后被停用并删除向量，无论期间被想起过多少次。
        """
        boost = config.MEMORY_RETRIEVAL_REINFORCE_BOOST
        now_timestamp = time.time()
        try:
            for mem in memories[:5]:  # 只记录前 5 条
                if mem.source_type == "episode":
                    continue
                is_relation = mem.source_type == "relation"
                applied_boost = 0.0
                if boost > 0:
                    # 强化会写库，必须复用与检索一致的隔离条件（工作区 + 实例），
                    # 否则同 ID 的其他工作区/实例记忆会被误强化。
                    target = (
                        await DBMemRelation.get_or_none(
                            id=mem.target_id,
                            workspace_id=self.workspace_id,
                            **self._scope_filter,
                        )
                        if is_relation
                        else await DBMemParagraph.get_or_none(
                            id=mem.target_id,
                            workspace_id=self.workspace_id,
                            **self._scope_filter,
                        )
                    )
                    # 冷却窗口内不重复写库，避免高频会话把强化变成写放大。
                    # 与 compute_effective_weight 一致地用时间戳比较，规避时区表示差异。
                    if target and (
                        not target.last_reinforced_at
                        or now_timestamp - target.last_reinforced_at.timestamp() >= REINFORCE_COOLDOWN_SECONDS
                    ):
                        await target.reinforce(boost)
                        applied_boost = boost
                await DBMemReinforcementLog.log_reinforcement(
                    workspace_id=self.workspace_id,
                    target_type=TargetType.RELATION if is_relation else TargetType.PARAGRAPH,
                    target_id=mem.target_id,
                    trigger_source=TriggerSource.NA_RETRIEVAL,
                    trigger_ref=query[:100],
                    boost_amount=applied_boost,
                )
        except Exception as e:
            logger.debug(f"记录访问日志失败: {e}")

    async def retrieve_by_entity(
        self,
        entity_name: str,
        limit: int | None = None,
    ) -> list[RetrievedMemory]:
        """按实体名称检索相关记忆

        Args:
            entity_name: 实体名称
            limit: 返回数量上限

        Returns:
            相关记忆列表
        """
        # 使用实体名称作为查询
        result = await self.retrieve(
            query=entity_name,
            limit=limit or config.MEMORY_RETRIEVAL_DEFAULT_LIMIT,
            min_similarity=0.6,  # 实体查询使用更高阈值
        )
        return result.memories

    async def get_recent_memories(
        self,
        hours: int = 24,
        limit: int = 20,
    ) -> list[RetrievedMemory]:
        """获取近期记忆

        Args:
            hours: 时间范围（小时）
            limit: 返回数量上限

        Returns:
            近期记忆列表
        """

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        paragraphs = await DBMemParagraph.filter(
            workspace_id=self.workspace_id,
            event_time__gte=cutoff,
            is_inactive=False,
            **self._scope_filter,
        ).order_by("-event_time").limit(limit)

        return [
            RetrievedMemory(
                target_id=p.id,
                source_type="paragraph",
                episode_id=p.episode_id,
                paragraph_id=p.id,
                relation_id=None,
                content=p.content,
                summary=p.summary or "",
                cognitive_type=p.cognitive_type.value,
                knowledge_type=p.knowledge_type.value,
                similarity_score=1.0,  # 时间查询不涉及相似度
                effective_weight=p.compute_effective_weight(time.time()),
                event_time=p.event_time,
                origin_chat_key=p.origin_chat_key,
            )
            for p in paragraphs
        ]


async def retrieve_memories(
    workspace_id: int,
    query: str,
    limit: int | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    *,
    scope: MemoryScope,
) -> list[RetrievedMemory]:
    """便捷函数：检索记忆

    Args:
        scope: 本次检索的可见范围（域 + 人设），**必填**。
            由 `instance_scope.resolve_memory_scope(channel)` 产生；确实要看
            整个工作区的管理端调用方显式传 `MemoryScope.unrestricted()`。
    """
    retriever = MemoryRetriever(workspace_id, scope=scope)
    result = await retriever.retrieve(query, limit=limit, time_from=time_from, time_to=time_to)
    return result.memories


@dataclass(frozen=True)
class BatchRecallRequest:
    """`retrieve_memories_batch` 的单条检索请求。"""

    query: str
    limit: int | None = None
    time_from: datetime | None = None
    time_to: datetime | None = None


async def retrieve_memories_batch(
    workspace_id: int,
    requests: Sequence[BatchRecallRequest],
    *,
    scope: MemoryScope,
    record_access: bool = True,
) -> list[list[RetrievedMemory]]:
    """并发执行多条检索，返回与 `requests` **同序**的结果列表。

    一次注入里的多条 query 彼此独立（各自一次 embedding + 一次 Qdrant 查询 + 若干读库），
    串行执行等于把这些等待时间原样叠加到主对话链路上，所以检索本身走 `asyncio.gather`。

    `record_access=True`（默认）时，检索之后按输入顺序**串行**补记访问：`_record_access`
    里的 `reinforce()` 是读改写（`base_weight = min(base_weight + boost, 2.0)` + 写
    `last_reinforced_at`），并由 `REINFORCE_COOLDOWN_SECONDS` 的窗口判定挡住重复强化 ——
    而这个判定读的正是上一次写入的 `last_reinforced_at`。并发补记就等于让同一条记忆在
    **同一次调用内**被多条 query 各强化一次、并多写几行强化日志。

    注入链路传 `record_access=False`：检索时不强化，注入了哪些记进本轮记录，由
    `round_recall.commit_round_recall` 在主对话成功返回后补记一次。这样一轮主对话迭代
    几次也只算一次，被拦下的轮次则完全不强化。

    单条 query 失败不连坐：该条回空列表，其余照常返回（记忆注入是风味不是功能，
    读路径 fail-open 是硬要求）。
    """
    if not requests:
        return []

    retrievers = [MemoryRetriever(workspace_id, scope=scope) for _ in requests]
    results = await asyncio.gather(
        *(
            retriever.retrieve(
                request.query,
                limit=request.limit,
                time_from=request.time_from,
                time_to=request.time_to,
                record_access=False,
            )
            for retriever, request in zip(retrievers, requests, strict=True)
        ),
        return_exceptions=True,
    )

    memories_per_request: list[list[RetrievedMemory]] = []
    for index, (retriever, request, result) in enumerate(zip(retrievers, requests, results, strict=True), 1):
        # `return_exceptions=True` 连 CancelledError 一起收进结果里。取消不是"这条检索失败了"，
        # 把它降级成一个空列表会让上层以为一切正常，取消信号就此消失。
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            logger.warning(f"记忆并发检索单条失败，该条返回空: index={index}/{len(requests)}, error={result}")
            memories_per_request.append([])
            continue
        memories_per_request.append(result.memories)
        if record_access and result.memories:
            await retriever.record_access(result.memories, request.query)

    return memories_per_request


async def compile_memories_for_context(
    recall_query: MemoryRecallQuery,
    memories: list[RetrievedMemory],
    max_length: int = 2000,
) -> str:
    """将检索候选编排为真正用于 prompt 注入的上下文块。"""
    if not memories:
        logger.debug("记忆上下文编排跳过: 输入 memories 为空")
        return ""

    selected = _select_context_memories(memories, recall_query)
    if not selected:
        logger.debug("记忆上下文编排跳过: 经过筛选后没有可注入的记忆")
        return ""

    lines: list[str] = [MEMORY_CONTEXT_BLOCK_TITLE]
    current_length = len(lines[0])

    primary_items: list[RetrievedMemory] = []
    supporting_items: list[RetrievedMemory] = []
    for index, mem in enumerate(selected):
        if index < 3 and mem.source_type != "relation":
            primary_items.append(mem)
        else:
            supporting_items.append(mem)

    sections: list[tuple[str, list[RetrievedMemory]]] = []
    if primary_items:
        sections.append((MEMORY_CONTEXT_CORE_SECTION, primary_items))
    if supporting_items:
        sections.append((MEMORY_CONTEXT_EVIDENCE_SECTION, supporting_items[:4]))

    for title, items in sections:
        if current_length + len(title) + 1 > max_length:
            break
        lines.append(title)
        current_length += len(title) + 1

        for mem in items:
            line = _format_compiled_memory_line(mem)
            if current_length + len(line) + 1 > max_length:
                break
            lines.append(line)
            current_length += len(line) + 1

    context = "\n".join(lines)
    query_preview = recall_query.query_text.strip().replace("\n", " ")[:120]
    logger.debug(
        f"记忆上下文编排完成: input={len(memories)}, selected={len(selected)}, "
        f"length={len(context)}, query={query_preview}",
    )
    return context


async def format_memories_for_context(
    memories: list[RetrievedMemory],
    max_length: int = 2000,
) -> str:
    """格式化记忆为上下文字符串

    Args:
        memories: 记忆列表
        max_length: 最大长度

    Returns:
        格式化后的字符串
    """
    return await compile_memories_for_context(
        recall_query=MemoryRecallQuery(query_text="", focus_text="", focus_points=[], context_texts=[]),
        memories=memories,
        max_length=max_length,
    )


def _resolve_context_category(mem: RetrievedMemory) -> MemoryKnowledgeHint:
    if mem.source_type == "relation":
        return MemoryKnowledgeHint.RELATION
    if mem.knowledge_type == "task":
        return MemoryKnowledgeHint.DECISION
    if mem.knowledge_type in {"skill", "experience"}:
        return MemoryKnowledgeHint.EXPERIENCE
    if mem.knowledge_type in {"fact", "preference"}:
        return MemoryKnowledgeHint(mem.knowledge_type)
    if mem.knowledge_type == "profile":
        return MemoryKnowledgeHint.FACT
    if mem.cognitive_type == CognitiveType.EPISODIC.value:
        return MemoryKnowledgeHint.CONVERSATION
    return MemoryKnowledgeHint.CONVERSATION


def _memory_type_rank(mem: RetrievedMemory, preferred_types: set[MemoryTypeHint]) -> int:
    if not preferred_types:
        return 0
    return 0 if mem.source_type in preferred_types else 1


def _category_rank(mem: RetrievedMemory, preferred_categories: set[MemoryKnowledgeHint], avoided_categories: set[MemoryKnowledgeHint]) -> int:
    category = _resolve_context_category(mem)
    if category in avoided_categories:
        return 2
    if preferred_categories:
        return 0 if category in preferred_categories else 1
    return 0


def _build_context_quotas(recall_query: MemoryRecallQuery) -> tuple[dict[str, int], dict[MemoryKnowledgeHint, int]]:
    if recall_query.answer_style == MemoryAnswerStyle.CORE_ONLY:
        return {"paragraph": 3, "episode": 1, "relation": 1}, {
            MemoryKnowledgeHint.DECISION: 2,
            MemoryKnowledgeHint.FACT: 2,
            MemoryKnowledgeHint.PREFERENCE: 1,
            MemoryKnowledgeHint.EXPERIENCE: 1,
            MemoryKnowledgeHint.CONVERSATION: 1,
            MemoryKnowledgeHint.EMOTION: 0,
            MemoryKnowledgeHint.RELATION: 1,
        }
    if recall_query.answer_style == MemoryAnswerStyle.PREFERENCE_SUMMARY:
        return {"paragraph": 3, "episode": 0, "relation": 1}, {
            MemoryKnowledgeHint.DECISION: 1,
            MemoryKnowledgeHint.FACT: 1,
            MemoryKnowledgeHint.PREFERENCE: 3,
            MemoryKnowledgeHint.EXPERIENCE: 1,
            MemoryKnowledgeHint.CONVERSATION: 1,
            MemoryKnowledgeHint.EMOTION: 0,
            MemoryKnowledgeHint.RELATION: 1,
        }
    if recall_query.answer_style == MemoryAnswerStyle.TIMELINE:
        return {"paragraph": 4, "episode": 3, "relation": 1}, {
            MemoryKnowledgeHint.DECISION: 1,
            MemoryKnowledgeHint.FACT: 1,
            MemoryKnowledgeHint.PREFERENCE: 1,
            MemoryKnowledgeHint.EXPERIENCE: 3,
            MemoryKnowledgeHint.CONVERSATION: 3,
            MemoryKnowledgeHint.EMOTION: 0,
            MemoryKnowledgeHint.RELATION: 1,
        }
    return {"paragraph": 4, "episode": 2, "relation": 2}, {
        MemoryKnowledgeHint.DECISION: 3,
        MemoryKnowledgeHint.FACT: 2,
        MemoryKnowledgeHint.PREFERENCE: 1,
        MemoryKnowledgeHint.EXPERIENCE: 2,
        MemoryKnowledgeHint.CONVERSATION: 2,
        MemoryKnowledgeHint.EMOTION: 0,
        MemoryKnowledgeHint.RELATION: 2,
    }


def _select_context_memories(memories: list[RetrievedMemory], recall_query: MemoryRecallQuery) -> list[RetrievedMemory]:
    """按当前检索意图选出用于上下文编排的候选。"""
    preferred_types = set(recall_query.prefer_memory_types or [])
    preferred_categories = set(recall_query.prefer_knowledge_types or [])
    avoided_categories = set(recall_query.avoid_knowledge_types or [])
    type_quota, category_quota = _build_context_quotas(recall_query)

    sorted_memories = sorted(
        memories,
        key=lambda mem: (
            _category_rank(mem, preferred_categories, avoided_categories),
            _memory_type_rank(mem, preferred_types),
            -mem.similarity_score,
            -mem.effective_weight,
            0 if mem.cognitive_type == CognitiveType.SEMANTIC.value else 1,
        ),
    )

    selected: list[RetrievedMemory] = []
    seen_ids: set[tuple[str, int]] = set()
    used_type_quota = {key: 0 for key in type_quota}
    used_category_quota = {key: 0 for key in category_quota}

    for mem in sorted_memories:
        memory_key = (mem.source_type, mem.target_id)
        if memory_key in seen_ids:
            continue
        category = _resolve_context_category(mem)
        if category in avoided_categories:
            continue
        if used_type_quota.get(mem.source_type, 0) >= type_quota.get(mem.source_type, 0):
            continue
        if used_category_quota.get(category, 0) >= category_quota.get(category, 0):
            continue

        selected.append(mem)
        seen_ids.add(memory_key)
        used_type_quota[mem.source_type] = used_type_quota.get(mem.source_type, 0) + 1
        used_category_quota[category] = used_category_quota.get(category, 0) + 1
        if len(selected) >= 8:
            break

    return selected


def _format_compiled_memory_line(mem: RetrievedMemory) -> str:
    """将单条记忆编排为更适合 prompt 的上下文行。"""
    category = _resolve_context_category(mem)
    label = MEMORY_CONTEXT_CATEGORY_LABELS.get(category, "记忆")
    # 记忆正文全部来自群聊，任何群成员都能往里写：先折成单行再限长，
    # 否则一个换行就能在提示词里伪造出一条新的渲染行。
    if mem.source_type == "relation":
        return f"- [{label}] {sanitize_inline_text(mem.summary or '', 120)}"
    if mem.source_type == "episode":
        time_str = mem.event_time.strftime("%m-%d %H:%M") if mem.event_time else "未知时间"
        title = sanitize_inline_text(mem.summary or "", 120) or "事件"
        narrative = sanitize_inline_text(mem.content or "", 160)
        return f"- [{label} {time_str}] {title}：{narrative}"

    time_str = mem.event_time.strftime("%m-%d %H:%M") if mem.event_time else "未知时间"
    content = sanitize_inline_text(mem.content or "", 140)
    summary = sanitize_inline_text(mem.summary or "", 120)
    body = content or summary
    if summary and summary not in body and len(summary) > 6:
        body = f"{summary}：{body}"
    body = sanitize_inline_text(body, 140)
    return f"- [{label} {time_str}] {body}"
