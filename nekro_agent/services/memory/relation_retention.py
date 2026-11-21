"""沉淀关系的保留时长：跟随来源段落

关系建立时原来一律用 `MEMORY_RELATION_HALF_LIFE_SECONDS`（默认 1 天），不看它是从哪段记忆里
抽出来的；清理线 `MEMORY_PRUNE_RELATION_THRESHOLD` 默认 0.03，不被强化的关系约 5 天就被清。
偏好、事实、决定这类稳定段落按语义半衰期能留 30 天，它的关系却 5 天就没了，只出现过一次的
实体也跟着被当成孤儿清掉：段落本身照样能按向量召回，丢的是关系通道（实体与谓词的字面匹配、
给段落的旁证加权）、记忆详情里的关系，以及记忆图谱里的边和点。这里分三件事：

1. :func:`relation_half_life_seconds`：沉淀时关系的半衰期不短于来源段落（也不短于关系自己的配置）；
2. :func:`extend_stable_relation_half_life`：存量里来源段落仍有效、且是稳定类型的有效关系，
   半衰期补长；只改半衰期，条件写在 UPDATE 里，幂等；
3. :func:`restore_pruned_stable_relations`：一次性捞回已经被自动清理、来源段落仍有效且是稳定
   类型的关系，连同它引用的实体（见那个函数的说明）。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from tortoise.expressions import Subquery
from tortoise.transactions import in_transaction

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import APP_SYSTEM_DIR
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_paragraph import DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory.retention import (
    MANUAL_PRUNE_ACTION,
    RESTORE_BATCH_LIMIT,
    RESTORE_SCAN_PAGE_SIZE,
    RETENTION_RESTORE_ACTION,
    STABLE_KNOWLEDGE_TYPES,
    consolidation_half_life_seconds,
    stable_half_life_seconds,
    write_done_marker,
)

logger = get_sub_logger("memory.relation_retention")

# 做完一遍的标记：与段落捞回的标记放在一处
RELATION_RESTORE_MARKER_PATH = Path(APP_SYSTEM_DIR) / "memory" / "relation_restore_v1.json"


def relation_half_life_seconds(knowledge_type: KnowledgeType) -> int:
    """沉淀出的关系该用的半衰期（秒）：不短于来源段落，也不短于关系自己的配置

    来源段落是偏好 / 事实 / 决定时就是语义半衰期；其余类型取情景半衰期与关系配置里较长的那个
    （两者默认都是 1 天，默认配置下其余类型的行为不变）。
    """
    return max(int(config.MEMORY_RELATION_HALF_LIFE_SECONDS), consolidation_half_life_seconds(knowledge_type))


def _stable_source_paragraph_ids() -> Subquery:
    """来源段落仍有效、来自沉淀、且是稳定类型的那些段落 ID"""
    return Subquery(
        DBMemParagraph.filter(
            is_inactive=False,
            origin_kind=OriginKind.CONSOLIDATION,
            knowledge_type__in=[kind.value for kind in STABLE_KNOWLEDGE_TYPES],
        ).values("id"),
    )


async def extend_stable_relation_half_life() -> int:
    """把来源段落仍有效且是稳定类型的有效关系，半衰期补到语义半衰期，返回改动的行数

    域行与工作区行都补；只改半衰期，已经不短于目标值的不改，所以每次清理之前都可以调一次。
    来源段落已失活、或不是稳定类型的关系不动，照旧按自己的半衰期衰减。
    """
    target = stable_half_life_seconds()
    changed = await DBMemRelation.filter(
        is_inactive=False,
        half_life_seconds__lt=target,
        paragraph_id__in=_stable_source_paragraph_ids(),
    ).update(half_life_seconds=target)
    if changed:
        logger.info(f"来源段落是稳定类型的存量关系，半衰期已补到 {target} 秒: rows={changed}")
    return int(changed or 0)


def relation_restore_done() -> bool:
    """关系捞回是否已经完整做完过一遍"""
    try:
        return RELATION_RESTORE_MARKER_PATH.is_file()
    except OSError:
        return False


@dataclass
class RelationRestoreResult:
    """一次关系捞回的计数（只有计数，不带任何正文与实体名）"""

    restored: int = 0
    entities_reactivated: int = 0
    entities_repointed: int = 0
    already_present: int = 0
    not_auto_pruned: int = 0
    below_line: int = 0
    missing_entity: int = 0
    failed: int = 0
    finished: bool = False


def _was_auto_pruned(
    relation: DBMemRelation,
    paragraph: DBMemParagraph,
    threshold: float,
    manual_runs: set[tuple[int, Any]],
) -> bool:
    """这条失活关系是不是被自动清理掉的（而不是管理员删的、或手动清理时清的）

    关系表上没有「最近手动操作」这一列，失活只有 `is_inactive` 一位，判据只能从数据里推：

    - 被手动清理那一轮清掉的不算：清理在同一次调用里用同一个时间写段落的 `last_manual_action_at`
      与关系的 `update_time`，所以与某个 `manual_prune` 段落同工作区、同时刻失活的关系就是手动
      清理清的（那一轮一段段落都没清时认不出来）；
    - 跟着来源段落一起被清、段落后来被一次性捞回（`retention_restore`）的算：失活时刻不晚于段落
      捞回的时刻；
    - 其余按自己的权重判：失活那一刻、按当时存的半衰期算出的权重已经低于关系清理线，说明是被
      清理按权重清掉的；管理员删的是页面上还活着的关系，那时权重通常在线上，不会被认成这一类。
    """
    deactivated_at = relation.update_time
    if (int(relation.workspace_id), deactivated_at) in manual_runs:
        return False
    restored_at = paragraph.last_manual_action_at
    if paragraph.last_manual_action == RETENTION_RESTORE_ACTION and restored_at is not None and deactivated_at <= restored_at:
        return True
    return relation.compute_effective_weight(deactivated_at.timestamp()) < threshold


async def _resolve_entity(entity_id: int) -> Optional[tuple[int, bool]]:
    """把关系一端的实体换算成「恢复后应该挂在哪个实体上」

    Returns:
        `(实体 ID, 要不要把它改回有效)`；实体行已经不在时返回 None。
        实体还有效：就是它本身。实体已失活、而同一身份（同工作区、实例、人设、类型、规范名；域行
        再加来源频道）已经有一行有效实体（清理之后同名又被提到、新建了一行）：挂到那一行上，不再
        造出第二个同名的有效实体。都没有：把它改回有效。
    """
    entity = await DBMemEntity.filter(id=entity_id).first()
    if entity is None:
        return None
    if not entity.is_inactive:
        return int(entity.id), False
    identity: dict[str, Any] = {
        "workspace_id": entity.workspace_id,
        "instance_key": entity.instance_key,
        "preset_id": entity.preset_id,
        "entity_type": entity.entity_type,
        "canonical_name": entity.canonical_name,
        "is_inactive": False,
    }
    if int(entity.workspace_id) == 0:
        identity["adapter_key"] = entity.adapter_key
        identity["origin_chat_key"] = entity.origin_chat_key
    twin = await DBMemEntity.filter(**identity).order_by("id").first()
    if twin is not None:
        return int(twin.id), False
    return int(entity.id), True


async def _revive(relation: DBMemRelation, target_half_life: int, result: RelationRestoreResult) -> None:
    """把一条关系连同它两端的实体改回有效（同一个事务里）"""
    subject = await _resolve_entity(int(relation.subject_entity_id))
    obj = await _resolve_entity(int(relation.object_entity_id))
    if subject is None or obj is None:
        result.missing_entity += 1
        return
    subject_id, subject_revive = subject
    object_id, object_revive = obj
    duplicate: dict[str, Any] = {
        "workspace_id": relation.workspace_id,
        "instance_key": relation.instance_key,
        "preset_id": relation.preset_id,
        "subject_entity_id": subject_id,
        "predicate": relation.predicate,
        "object_entity_id": object_id,
        "is_inactive": False,
    }
    if int(relation.workspace_id) == 0:
        duplicate["adapter_key"] = relation.adapter_key
        duplicate["origin_chat_key"] = relation.origin_chat_key
    if subject_id == object_id or await DBMemRelation.filter(**duplicate).exists():
        # 同一条三元组在清理之后又被提到、已经有一条有效的了：不再造第二条
        result.already_present += 1
        return

    async with in_transaction():
        for entity_id, revive in ((subject_id, subject_revive), (object_id, object_revive)):
            if revive:
                await DBMemEntity.filter(id=entity_id, is_inactive=True).update(is_inactive=False)
                result.entities_reactivated += 1
        result.entities_repointed += int(subject_id != relation.subject_entity_id) + int(
            object_id != relation.object_entity_id,
        )
        relation.subject_entity_id = subject_id
        relation.object_entity_id = object_id
        relation.half_life_seconds = max(int(relation.half_life_seconds), target_half_life)
        relation.is_inactive = False
        await relation.save(
            update_fields=["subject_entity_id", "object_entity_id", "half_life_seconds", "is_inactive", "update_time"],
        )
    result.restored += 1


async def restore_pruned_stable_relations(
    *,
    limit: int = RESTORE_BATCH_LIMIT,
    page_size: int = RESTORE_SCAN_PAGE_SIZE,
) -> RelationRestoreResult:
    """一次性捞回：被自动清理掉、来源段落仍有效且是稳定类型的关系，连同它引用的实体

    范围：失活的关系，`paragraph_id` 指向仍有效、来自沉淀、偏好 / 事实 / 决定的段落，并且按
    :func:`_was_auto_pruned` 判是被自动清理掉的（管理员删的、手动清理清的不碰）；按语义半衰期
    算现在仍在关系清理线以上。恢复时半衰期补到语义半衰期；两端的实体按 :func:`_resolve_entity`
    改回有效或挂到同身份的有效实体上；同一条三元组已经有有效的一条时不再恢复。

    与段落捞回同一个做法：按主键游标翻完全部候选，名额只算真正恢复的；完整翻完一遍、没有失败
    也没撞上名额，就写下 :data:`RELATION_RESTORE_MARKER_PATH`，此后直接返回。不调外部服务
    （关系没有向量），所以没有「连续失败就停」。日志与标记里只有计数与编号。
    """
    result = RelationRestoreResult()
    if relation_restore_done():
        result.finished = True
        return result

    threshold = float(config.MEMORY_PRUNE_RELATION_THRESHOLD)
    target = stable_half_life_seconds()
    now_ts = time.time()
    manual_runs: set[tuple[int, Any]] = {
        (int(workspace_id), at)
        for workspace_id, at in await DBMemParagraph.filter(
            last_manual_action=MANUAL_PRUNE_ACTION,
            last_manual_action_at__isnull=False,
        ).values_list("workspace_id", "last_manual_action_at")
    }
    hit_limit = False
    cursor = 0
    while not hit_limit:
        page = await (
            DBMemRelation.filter(
                is_inactive=True,
                paragraph_id__in=_stable_source_paragraph_ids(),
                id__gt=cursor,
            )
            .order_by("id")
            .limit(max(1, int(page_size)))
        )
        if not page:
            break
        cursor = page[-1].id
        paragraphs = {
            int(row.id): row for row in await DBMemParagraph.filter(id__in=sorted({int(r.paragraph_id) for r in page}))
        }
        for relation in page:
            if result.restored >= limit:
                hit_limit = True
                break
            paragraph = paragraphs.get(int(relation.paragraph_id))
            if paragraph is None or paragraph.is_inactive:
                continue
            if not _was_auto_pruned(relation, paragraph, threshold, manual_runs):
                result.not_auto_pruned += 1
                continue
            # 按恢复后要用的半衰期判还在不在线上（判「是不是自动清理掉的」用的是当时存的半衰期，在上面）
            stored_half_life = int(relation.half_life_seconds)
            relation.half_life_seconds = max(stored_half_life, target)
            above_line = relation.compute_effective_weight(now_ts) >= threshold
            relation.half_life_seconds = stored_half_life
            if not above_line:
                result.below_line += 1
                continue
            try:
                await _revive(relation, target, result)
            except Exception as e:
                result.failed += 1
                logger.warning(f"捞回被清理的关系失败，下次再试: relation_id={relation.id}, error={type(e).__name__}")
    counts = {key: value for key, value in asdict(result).items() if key != "finished"}
    if result.restored:
        logger.info(f"捞回了来源段落仍在的稳定类型关系: {counts}")
    if not (hit_limit or result.failed):
        write_done_marker(RELATION_RESTORE_MARKER_PATH, counts)
        result.finished = True
        logger.info(f"关系捞回已做完一遍，之后不再执行: {counts}")
    return result


__all__ = [
    "RELATION_RESTORE_MARKER_PATH",
    "RelationRestoreResult",
    "extend_stable_relation_half_life",
    "relation_half_life_seconds",
    "relation_restore_done",
    "restore_pruned_stable_relations",
]
