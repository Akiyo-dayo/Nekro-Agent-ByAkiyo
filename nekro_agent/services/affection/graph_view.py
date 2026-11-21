"""后台好感页的关系图：人设在中间，人、关系里程碑、频道围在外面

一份好感是 `(域, 人, 人设)` 上的一行，图就照这个画：

- **人设**节点：一个 `(域, 人设)` 一个，放在中间；
- **人**节点：一个 `(域, 人)` 一个。同一个人换过人设就有几份好感，人还是一个，连到几个人设上；
- **人 → 人设**的边就是那一份好感：`weight` 是分数，阶段与阶段名放在元数据里，前端按分数定粗细、
  按阶段定颜色；
- **关系里程碑**节点挂在它所属的人身上（每份好感最多取最新的几条）；
- **频道**节点是这个人最近一次出现的频道（`last_chat_key`），人 → 频道一条边。

**人与人之间不画边**：数据里没有人和人的关系，图因此是以人设为中心的星形，加上挂在人身上的
里程碑与频道，不编造任何人与人的连线。

本模块只拼图，不碰数据库、不判权限：哪些行可见、里程碑的正文给不给、频道名给不给，都由路由按
好感页同一套作用域与判据算好传进来（见 `routers/affection.py`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from nekro_agent.models.db_affection import DBAffection
from nekro_agent.services.affection.stage import AFFECTION_MAX_SCORE, AFFECTION_MIN_SCORE, stage_label, stage_of
from nekro_agent.services.identity_scope import ScopeDomain

# 每份好感最多画几条里程碑（最新的那几条）；详情对话框里能看到全部
MILESTONES_PER_RECORD = 5

# 里程碑节点标签取段落摘要的前多少个字
_MILESTONE_LABEL_CHARS = 28


@dataclass(frozen=True)
class GraphRow:
    """一份要画进图里的好感，连同路由替它算好的东西

    `milestones` 是好感详情页「关系里程碑」一栏的条目（`AffectionMilestoneItem` 的字典形状，新的在前），
    正文给不给已经按那一栏的规则处理过（挪到别的租户、被真删的，正文为空）。
    """

    record: DBAffection
    domain: ScopeDomain
    domain_label: str
    preset_name: str
    in_use_channels: int
    milestones: Sequence[Mapping[str, Any]] = ()


@dataclass
class AffectionGraph:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _node(**values: Any) -> dict[str, Any]:
    values.setdefault("subtitle", "")
    values.setdefault("status", "active")
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


def preset_node_id(domain_id: str, preset_id: int) -> str:
    return f"preset-{domain_id}-{preset_id}"


def person_node_id(domain_id: str, account: str) -> str:
    return f"person-{domain_id}-{account}"


def _score_strength(score: int) -> float:
    """分数在整个取值区间里的位置，0 ~ 1"""
    span = AFFECTION_MAX_SCORE - AFFECTION_MIN_SCORE
    return max(0.0, min(1.0, (score - AFFECTION_MIN_SCORE) / span))


def _latest(rows: Sequence[GraphRow]) -> GraphRow:
    """一个人的几份好感里最近更新的那一份：名字、称呼都取它的"""
    return max(rows, key=lambda row: (row.record.update_time, row.record.id))


def build_affection_graph(
    rows: Sequence[GraphRow],
    *,
    channel_names: Mapping[str, str],
    milestones_per_record: int = MILESTONES_PER_RECORD,
) -> AffectionGraph:
    """把几份好感拼成关系图

    Args:
        rows: 要画的好感（调用方已经去掉了合并后休眠的副号行）
        channel_names: 会话标识 → 频道名；只放这个身份看得见的频道。不在里面的频道只显示会话标识
            （好感列表本来就给 `last_chat_key`），不另外给名字
        milestones_per_record: 每份好感最多画几条里程碑
    """
    graph = AffectionGraph()

    presets: dict[tuple[str, int], list[GraphRow]] = {}
    people: dict[tuple[str, str], list[GraphRow]] = {}
    for row in rows:
        domain_id = row.domain.domain_id
        presets.setdefault((domain_id, row.record.preset_id), []).append(row)
        people.setdefault((domain_id, row.record.platform_userid), []).append(row)

    for (domain_id, preset_id), preset_rows in sorted(presets.items()):
        first = preset_rows[0]
        person_count = len({row.record.platform_userid for row in preset_rows})
        graph.nodes.append(
            _node(
                id=preset_node_id(domain_id, preset_id),
                memory_type="preset",
                ref_id=preset_id,
                label=first.preset_name,
                subtitle=first.domain_label,
                weight=float(person_count),
                importance=1.0 + min(1.0, person_count / 10),
                metadata={
                    "domain_id": domain_id,
                    "domain_label": first.domain_label,
                    "preset_id": preset_id,
                    "person_count": person_count,
                    "in_use_channels": first.in_use_channels,
                },
            ),
        )

    channel_people: dict[str, set[str]] = {}
    drawn: set[str] = set()
    for (domain_id, account), person_rows in sorted(people.items()):
        node_id = person_node_id(domain_id, account)
        latest = _latest(person_rows)
        best_score = max(row.record.score for row in person_rows)
        seen = [(row.record.last_interact_at, row) for row in person_rows if row.record.last_interact_at is not None]
        last_seen = max(seen, key=lambda pair: (pair[0], pair[1].record.id))[1] if seen else None
        records: list[dict[str, Any]] = []
        milestone_count = 0
        for row in sorted(person_rows, key=lambda item: (-item.record.score, item.record.id)):
            record = row.record
            stage = stage_of(record.score)
            records.append(
                {
                    "record_id": record.id,
                    "preset_id": record.preset_id,
                    "preset_name": row.preset_name,
                    "score": record.score,
                    "stage": stage.value,
                    "stage_label": stage_label(stage),
                    "call_name": record.call_name,
                    "state_text": record.state_text,
                    "interaction_count": record.interaction_count,
                    "last_interact_at": _iso(record.last_interact_at),
                    "last_chat_key": record.last_chat_key,
                    "in_use_channels": row.in_use_channels,
                    "milestone_count": len(row.milestones),
                },
            )
            milestone_count += len(row.milestones)
            graph.edges.append(
                _edge(
                    id=f"affection-{record.id}",
                    source=node_id,
                    target=preset_node_id(domain_id, record.preset_id),
                    edge_type="affection",
                    label=stage_label(stage),
                    weight=float(record.score),
                    strength=_score_strength(record.score),
                    metadata={
                        "record_id": record.id,
                        "score": record.score,
                        "stage": stage.value,
                        "stage_label": stage_label(stage),
                    },
                ),
            )
            for item in list(row.milestones)[: max(0, milestones_per_record)]:
                _add_milestone(graph, item, person_id=node_id, record=record, drawn=drawn)
            if record.last_chat_key:
                channel_people.setdefault(record.last_chat_key, set()).add(node_id)
                edge_id = f"seen-{node_id}-{record.last_chat_key}"
                if edge_id not in drawn:
                    drawn.add(edge_id)
                    graph.edges.append(
                        _edge(
                            id=edge_id,
                            source=node_id,
                            target=f"channel-{record.last_chat_key}",
                            edge_type="last_seen",
                            label="最近出现",
                            metadata={"chat_key": record.last_chat_key},
                        ),
                    )
        nickname = (latest.record.nickname or "").strip()
        graph.nodes.append(
            _node(
                id=node_id,
                memory_type="person",
                ref_id=latest.record.id,
                label=nickname or account,
                subtitle=latest.record.call_name,
                weight=float(best_score),
                # 最高的那份好感定亮度与大小：越亲近越显眼，厌恶的人淡淡地在边上
                importance=max(0.05, min(2.0, (best_score - AFFECTION_MIN_SCORE) / 150)),
                metadata={
                    "domain_id": domain_id,
                    "account": account,
                    "nickname": nickname,
                    "call_name": latest.record.call_name,
                    "best_score": best_score,
                    "last_interact_at": _iso(last_seen.record.last_interact_at) if last_seen else None,
                    "last_chat_key": last_seen.record.last_chat_key if last_seen else "",
                    "records": records,
                    "milestone_count": milestone_count,
                },
            ),
        )

    for chat_key, linked in sorted(channel_people.items()):
        name = channel_names.get(chat_key, "")
        graph.nodes.append(
            _node(
                id=f"channel-{chat_key}",
                memory_type="channel",
                ref_id=0,
                label=name or chat_key,
                subtitle=chat_key if name else "",
                weight=float(len(linked)),
                importance=0.6 + min(1.2, len(linked) * 0.2),
                metadata={"chat_key": chat_key, "channel_name": name, "person_count": len(linked)},
            ),
        )
    return graph


def _add_milestone(
    graph: AffectionGraph,
    item: Mapping[str, Any],
    *,
    person_id: str,
    record: DBAffection,
    drawn: set[str],
) -> None:
    event_id = int(item.get("event_id") or 0)
    node_id = f"milestone-{event_id}"
    if node_id in drawn:
        return
    drawn.add(node_id)
    status = str(item.get("status") or "active")
    summary = str(item.get("summary") or "") or str(item.get("content") or "")
    event_time = item.get("event_time")
    graph.nodes.append(
        _node(
            id=node_id,
            memory_type="milestone",
            ref_id=int(item.get("paragraph_id") or 0),
            label=summary[:_MILESTONE_LABEL_CHARS],
            subtitle=str(event_time)[:10] if event_time else "",
            status=status,
            importance=1.2 if status == "active" else 0.5,
            paragraph_id=int(item.get("paragraph_id") or 0) or None,
            metadata={
                "event_id": event_id,
                "paragraph_id": int(item.get("paragraph_id") or 0),
                "record_id": record.id,
                "preset_id": record.preset_id,
                "event_time": event_time,
                "status": status,
                "content": str(item.get("content") or ""),
                "summary": str(item.get("summary") or ""),
            },
        ),
    )
    graph.edges.append(
        _edge(
            id=f"milestone-{event_id}-{person_id}",
            source=node_id,
            target=person_id,
            edge_type="milestone",
            label="关系里程碑",
            metadata={"record_id": record.id},
        ),
    )


__all__ = [
    "MILESTONES_PER_RECORD",
    "AffectionGraph",
    "GraphRow",
    "build_affection_graph",
    "person_node_id",
    "preset_node_id",
]
