"""后台记忆页的图谱：段落、事件、实体、关系，加上「涉及的人」

组装照搬工作区记忆页的图谱接口（`routers/workspaces.py` 的 `get_memory_graph`：节点四类、边六类、
权重与大小的算法都一样），差别只在取数：那边只按 `workspace_id` 过滤，域记忆（`workspace_id = 0`）
与好感里程碑一条都看不到；这里四张表都走 `AdminMemoryScope.apply`，与段落列表同一个范围起点，
筛选（分区、频道、人设、人）也是同一套。

另加两样：

- 「人」节点：本次画出来的段落里涉及的人（`DBMemParagraphPerson`），名字用与列表同一处的口径
  （`person_labels`）；选了实例时同一个人的几个账号并成一个节点，与按人筛选的别名展开同一个域。
  段落到人的边叫 `paragraph_person`。
- 关系里程碑段落在节点元数据里带 `is_milestone`，界面上打标记。

这个模块不判权限：范围由路由按身份算好传进来。单个节点的详情复用 `/memory/items/{type}/{id}`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import DBMemParagraph, OriginKind
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory.admin_service import paragraph_status
from nekro_agent.services.memory.instance_scope import AdminFilter, AdminMemoryScope
from nekro_agent.services.memory.scope_condition import MemTable

if TYPE_CHECKING:  # pragma: no cover - 只在类型检查时需要；运行时延迟导入，避免与身份域模块成环
    from nekro_agent.services.identity_scope import ScopeDomain

# 与工作区图谱接口相同的上下限
GRAPH_LIMIT_MIN = 50
GRAPH_LIMIT_MAX = 600
GRAPH_LIMIT_DEFAULT = 240

# 事件的读时衰减半衰期，与工作区图谱接口相同（30 天）
_EPISODE_HALF_LIFE_SECONDS = 30 * 24 * 3600


@dataclass
class MemoryGraph:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)


def _episode_effective_weight(episode: DBMemEpisode, now_ts: float) -> float:
    base = max(0.1, float(episode.base_weight or 0.0))
    event_time = episode.time_end or episode.time_start
    if event_time is None:
        return base
    age_seconds = max(0.0, now_ts - event_time.timestamp())
    decay_factor = 2 ** (-age_seconds / max(1.0, _EPISODE_HALF_LIFE_SECONDS))
    return base * decay_factor


def _node(**values: Any) -> dict[str, Any]:
    values.setdefault("subtitle", "")
    values.setdefault("cognitive_type", None)
    values.setdefault("weight", 0.0)
    values.setdefault("size", 1.0)
    values.setdefault("importance", 0.0)
    values.setdefault("paragraph_id", None)
    values.setdefault("metadata", {})
    return values


def _edge(**values: Any) -> dict[str, Any]:
    values.setdefault("label", "")
    values.setdefault("weight", 0.0)
    values.setdefault("strength", 0.0)
    values.setdefault("status", "active")
    values.setdefault("cognitive_type", None)
    values.setdefault("metadata", {})
    return values


async def build_admin_memory_graph(
    scope: AdminMemoryScope,
    f: AdminFilter,
    *,
    include_inactive: bool = False,
    time_from: Optional[datetime] = None,
    time_to: Optional[datetime] = None,
    limit: int = GRAPH_LIMIT_DEFAULT,
    domain: Optional[ScopeDomain] = None,
) -> MemoryGraph:
    """按范围与筛选组装图谱

    `f` 的四个维度：段落全用；事件与关系用分区、频道、人设（「人」这一维只在段落表上）；实体只用分区
    （实体按 ID 从关系与事件里取，范围仍由 `scope` 卡住）。按人或按时间筛时，关系只取挂在本次段落上的，
    按人筛时事件也只取本次段落所属的——否则画出来的是一堆与这个人无关的线索。
    """
    limit = max(GRAPH_LIMIT_MIN, min(GRAPH_LIMIT_MAX, int(limit)))
    now_ts = time.time()
    shared = AdminFilter(partition=f.partition, chat_key=f.chat_key, preset_id=f.preset_id)

    paragraph_qs = scope.apply(DBMemParagraph.all(), MemTable.PARAGRAPH, f)
    relation_qs = scope.apply(DBMemRelation.all(), MemTable.RELATION, shared)
    episode_qs = scope.apply(DBMemEpisode.all(), MemTable.EPISODE, shared)
    entity_qs = scope.apply(DBMemEntity.all(), MemTable.ENTITY, AdminFilter(partition=f.partition))
    if not include_inactive:
        paragraph_qs = paragraph_qs.filter(is_inactive=False)
        relation_qs = relation_qs.filter(is_inactive=False)
        episode_qs = episode_qs.filter(is_inactive=False)
        entity_qs = entity_qs.filter(is_inactive=False)
    if time_from is not None:
        paragraph_qs = paragraph_qs.filter(event_time__gte=time_from)
        episode_qs = episode_qs.filter(time_end__gte=time_from)
    if time_to is not None:
        paragraph_qs = paragraph_qs.filter(event_time__lte=time_to)
        episode_qs = episode_qs.filter(time_start__lte=time_to)

    paragraphs: list[DBMemParagraph] = list(await paragraph_qs.order_by("-update_time").limit(limit))
    paragraph_ids = [paragraph.id for paragraph in paragraphs]

    narrowed = time_from is not None or time_to is not None or f.person_accounts is not None
    if narrowed:
        relation_qs = (
            relation_qs.filter(paragraph_id__in=paragraph_ids) if paragraph_ids else relation_qs.filter(id__in=[])
        )
    relations: list[DBMemRelation] = list(await relation_qs.order_by("-update_time").limit(limit))

    if f.person_accounts is not None:
        own_episode_ids = sorted({paragraph.episode_id for paragraph in paragraphs if paragraph.episode_id is not None})
        episode_qs = episode_qs.filter(id__in=own_episode_ids) if own_episode_ids else episode_qs.filter(id__in=[])
    episodes: list[DBMemEpisode] = list(
        await episode_qs.order_by("-time_end", "-update_time").limit(max(20, limit // 2)),
    )

    episode_entity_ids = [entity_id for episode in episodes for entity_id in (episode.participant_entity_ids or [])]
    relation_entity_ids = [relation.subject_entity_id for relation in relations] + [
        relation.object_entity_id for relation in relations
    ]
    entity_ids = sorted(set(relation_entity_ids + episode_entity_ids))
    entities: list[DBMemEntity] = list(await entity_qs.filter(id__in=entity_ids).limit(limit)) if entity_ids else []
    entity_map = {entity.id: entity for entity in entities}

    graph = MemoryGraph()
    paragraph_entity_edges: set[tuple[int, int]] = set()

    for paragraph in paragraphs:
        effective_weight = paragraph.compute_effective_weight(now_ts)
        origin_kind = paragraph.origin_kind.value if paragraph.origin_kind is not None else None
        graph.nodes.append(
            _node(
                id=f"paragraph-{paragraph.id}",
                memory_type="paragraph",
                ref_id=paragraph.id,
                label=paragraph.summary or paragraph.content[:28],
                subtitle=paragraph.knowledge_type.value,
                status="inactive" if paragraph.is_inactive else "active",
                cognitive_type=paragraph.cognitive_type.value,
                weight=effective_weight,
                size=min(1.8, 0.9 + max(0.0, effective_weight) * 0.45),
                importance=effective_weight,
                metadata={
                    "memory_source": paragraph.memory_source,
                    "memory_status": paragraph_status(paragraph),
                    "is_frozen": paragraph.is_frozen,
                    "is_protected": paragraph.is_protected,
                    "origin_kind": origin_kind,
                    "is_milestone": paragraph.origin_kind == OriginKind.AFFECTION,
                    "origin_chat_key": paragraph.origin_chat_key,
                    "episode_id": paragraph.episode_id,
                    "episode_phase": paragraph.episode_phase.value if paragraph.episode_phase else None,
                    "create_time": paragraph.create_time.isoformat(),
                    "update_time": paragraph.update_time.isoformat(),
                },
            ),
        )

    for episode in episodes:
        episode_weight = _episode_effective_weight(episode, now_ts)
        graph.nodes.append(
            _node(
                id=f"episode-{episode.id}",
                memory_type="episode",
                ref_id=episode.id,
                label=episode.title,
                subtitle=episode.narrative_summary[:80],
                status="inactive" if episode.is_inactive else "active",
                cognitive_type="episodic",
                weight=episode_weight,
                size=min(2.0, 1.0 + len(episode.paragraph_ids or []) * 0.08),
                importance=episode_weight + len(episode.paragraph_ids or []) * 0.15,
                metadata={
                    "origin_chat_key": episode.origin_chat_key,
                    "time_start": episode.time_start.isoformat() if episode.time_start else None,
                    "time_end": episode.time_end.isoformat() if episode.time_end else None,
                    "paragraph_ids": episode.paragraph_ids,
                    "participant_entity_ids": episode.participant_entity_ids,
                },
            ),
        )

    for entity in entities:
        importance = float(entity.appearance_count)
        graph.nodes.append(
            _node(
                id=f"entity-{entity.id}",
                memory_type="entity",
                ref_id=entity.id,
                label=entity.canonical_name,
                subtitle=entity.entity_type.value,
                status="inactive" if entity.is_inactive else "active",
                weight=importance,
                size=min(1.6, 0.85 + min(entity.appearance_count, 10) * 0.06),
                importance=importance,
                metadata={
                    "appearance_count": entity.appearance_count,
                    "source_hint": entity.source_hint.value,
                    "create_time": entity.create_time.isoformat(),
                    "update_time": entity.update_time.isoformat(),
                },
            ),
        )

    paragraph_id_set = set(paragraph_ids)
    for relation in relations:
        effective_weight = relation.compute_effective_weight(now_ts)
        status = "inactive" if relation.is_inactive else "active"
        subject = entity_map.get(relation.subject_entity_id)
        obj = entity_map.get(relation.object_entity_id)
        relation_node_id = f"relation-{relation.id}"
        graph.nodes.append(
            _node(
                id=relation_node_id,
                memory_type="relation",
                ref_id=relation.id,
                label=relation.predicate,
                subtitle=(
                    f"{subject.canonical_name if subject else relation.subject_entity_id} -> "
                    f"{obj.canonical_name if obj else relation.object_entity_id}"
                ),
                status=status,
                cognitive_type=relation.cognitive_type,
                weight=effective_weight,
                size=min(1.5, 0.75 + max(0.0, effective_weight) * 0.4),
                importance=effective_weight,
                paragraph_id=relation.paragraph_id,
                metadata={
                    "memory_source": relation.memory_source,
                    "paragraph_id": relation.paragraph_id,
                    "create_time": relation.create_time.isoformat(),
                    "update_time": relation.update_time.isoformat(),
                },
            ),
        )
        strength = min(1.0, 0.25 + max(0.0, effective_weight) * 0.35)
        if subject:
            graph.edges.append(
                _edge(
                    id=f"relation-subject-{relation.id}-{subject.id}",
                    source=relation_node_id,
                    target=f"entity-{subject.id}",
                    edge_type="relation_subject",
                    label="主语",
                    weight=effective_weight,
                    strength=strength,
                    status=status,
                    cognitive_type=relation.cognitive_type,
                ),
            )
        if obj:
            graph.edges.append(
                _edge(
                    id=f"relation-object-{relation.id}-{obj.id}",
                    source=relation_node_id,
                    target=f"entity-{obj.id}",
                    edge_type="relation_object",
                    label="宾语",
                    weight=effective_weight,
                    strength=strength,
                    status=status,
                    cognitive_type=relation.cognitive_type,
                ),
            )
        if relation.paragraph_id and relation.paragraph_id in paragraph_id_set:
            graph.edges.append(
                _edge(
                    id=f"relation-paragraph-{relation.id}-{relation.paragraph_id}",
                    source=relation_node_id,
                    target=f"paragraph-{relation.paragraph_id}",
                    edge_type="relation_paragraph",
                    label="来源段落",
                    weight=effective_weight,
                    strength=min(1.0, 0.2 + max(0.0, effective_weight) * 0.25),
                    status=status,
                    cognitive_type=relation.cognitive_type,
                ),
            )
            if subject:
                paragraph_entity_edges.add((relation.paragraph_id, subject.id))
            if obj:
                paragraph_entity_edges.add((relation.paragraph_id, obj.id))

    for paragraph_id, entity_id in sorted(paragraph_entity_edges):
        graph.edges.append(
            _edge(
                id=f"paragraph-entity-{paragraph_id}-{entity_id}",
                source=f"paragraph-{paragraph_id}",
                target=f"entity-{entity_id}",
                edge_type="paragraph_entity",
                label="提及",
                weight=0.5,
                strength=0.35,
            ),
        )

    for episode in episodes:
        episode_status = "inactive" if episode.is_inactive else "active"
        for paragraph_id in episode.paragraph_ids or []:
            if paragraph_id in paragraph_id_set:
                graph.edges.append(
                    _edge(
                        id=f"episode-paragraph-{episode.id}-{paragraph_id}",
                        source=f"episode-{episode.id}",
                        target=f"paragraph-{paragraph_id}",
                        edge_type="episode_paragraph",
                        label="包含段落",
                        weight=episode.base_weight,
                        strength=0.45,
                        status=episode_status,
                        cognitive_type="episodic",
                    ),
                )
        for entity_id in episode.participant_entity_ids or []:
            if entity_id in entity_map:
                graph.edges.append(
                    _edge(
                        id=f"episode-entity-{episode.id}-{entity_id}",
                        source=f"episode-{episode.id}",
                        target=f"entity-{entity_id}",
                        edge_type="episode_entity",
                        label="参与者",
                        weight=episode.base_weight,
                        strength=0.35,
                        status=episode_status,
                        cognitive_type="episodic",
                    ),
                )

    await _add_person_nodes(graph, paragraphs, domain)
    return graph


async def _add_person_nodes(
    graph: MemoryGraph,
    paragraphs: list[DBMemParagraph],
    domain: Optional[ScopeDomain],
) -> None:
    """段落里涉及的人：一个人一个节点，段落到人一条边

    名字取这个人在本次段落里最新那一段上的显示名（昵称 / 称呼的口径见 `person_labels`）。
    选了实例（有域）时同一个人的几个账号并成一个节点。
    """
    from nekro_agent.services.identity_scope import resolve_person_uids
    from nekro_agent.services.memory.person_labels import label_paragraph_persons
    from nekro_agent.services.memory.persons import load_person_accounts

    if not paragraphs:
        return
    accounts_by_paragraph: dict[int, list[str]] = await load_person_accounts([row.id for row in paragraphs])
    all_accounts = sorted({account for accounts in accounts_by_paragraph.values() for account in accounts})
    if not all_accounts:
        return
    person_of: dict[str, str] = {account: account for account in all_accounts}
    if domain is not None:
        person_of = await resolve_person_uids(domain, all_accounts)
    labels = await label_paragraph_persons(paragraphs, accounts_by_paragraph)

    people: dict[str, dict[str, Any]] = {}
    for paragraph in sorted(paragraphs, key=lambda row: row.id):
        for label in labels.get(paragraph.id, []):
            person = person_of.get(label.account, label.account)
            entry = people.setdefault(person, {"accounts": set(), "paragraphs": {}, "label": label})
            entry["accounts"].add(label.account)
            # 同一段里两个账号是同一个人时只算一次、只连一条边
            entry["paragraphs"][paragraph.id] = None
            # 按段落 ID 升序走，最后留下的是最新那一段上的名字；那一段查不到名字时不覆盖已有的
            if label.display_name or label.call_name or not (entry["label"].display_name or entry["label"].call_name):
                entry["label"] = label

    for person, entry in sorted(people.items()):
        label = entry["label"]
        count = len(entry["paragraphs"])
        name = label.display_name or person
        graph.nodes.append(
            _node(
                id=f"person-{person}",
                memory_type="person",
                ref_id=0,
                label=name,
                subtitle=label.call_name,
                status="active",
                weight=float(count),
                size=min(1.6, 0.85 + min(count, 10) * 0.06),
                importance=min(3.0, 0.6 + count * 0.25),
                metadata={
                    "account": person,
                    "accounts": sorted(entry["accounts"]),
                    "display_name": label.display_name,
                    "call_name": label.call_name,
                    # 名字从哪来：`chat` / `affection`（好感域共用，可能是在别的群或私聊里看到的）/ 空
                    "name_source": label.name_source,
                    "paragraph_count": count,
                },
            ),
        )
        for paragraph_id in entry["paragraphs"]:
            graph.edges.append(
                _edge(
                    id=f"paragraph-person-{paragraph_id}-{person}",
                    source=f"paragraph-{paragraph_id}",
                    target=f"person-{person}",
                    edge_type="paragraph_person",
                    label="涉及",
                    weight=1.0,
                    strength=0.3,
                ),
            )


__all__ = [
    "GRAPH_LIMIT_DEFAULT",
    "GRAPH_LIMIT_MAX",
    "GRAPH_LIMIT_MIN",
    "MemoryGraph",
    "build_admin_memory_graph",
]
