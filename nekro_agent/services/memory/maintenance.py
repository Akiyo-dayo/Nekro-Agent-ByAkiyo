"""记忆维护服务。

提供结构化记忆的清理能力，供路由、命令与后台调度统一复用。

清理是后台任务，而单进程要托管几十个 Bot 实例：整表加载、逐行 `save`、逐条向量删除
都会把事件循环长时间占住，所有实例一起卡。因此这里一律按主键游标分页扫描、
成批写库、成批删向量，并在每页之间让出事件循环。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_reinforcement_log import DBMemReinforcementLog
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.services.memory.feature_flags import (
    MemoryOperation,
    ensure_memory_system_enabled,
    is_domain_scope_enabled,
)
from nekro_agent.services.memory.prompt_rounds import prune_prompt_rounds
from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
from nekro_agent.services.memory.relation_retention import extend_stable_relation_half_life
from nekro_agent.services.memory.retention import (
    AUTO_PRUNE_ACTION,
    PRUNE_ACTIONS,
    extend_stable_half_life,
)

logger = get_sub_logger("memory.maintenance")

# 单页扫描的行数。页越大单次占用事件循环越久（ORM 实例化整页是同步的），页越小往返越多。
# 6000 段落的实测：页 100 → 停顿 5.9ms / 3.03s，页 200 → 9.0ms / 2.17s，
# 页 500 → 24.1ms / 2.23s，页 1000 → 40.8ms / 2.12s，200 是拐点。
PRUNE_SCAN_PAGE_SIZE = 200
# 单条 `UPDATE ... WHERE id IN (...)` 携带的 ID 数量，避免触碰 SQLite 的绑定参数上限
PRUNE_WRITE_CHUNK_SIZE = 200
# 工作区之间的间隔。几十个实例共用一个事件循环，串行跑完所有工作区必须留出呼吸位。
PRUNE_WORKSPACE_INTERVAL_SECONDS = 0.05


@dataclass
class MemoryPruneResult:
    paragraphs_pruned: int = 0
    relations_pruned: int = 0
    entities_pruned: int = 0
    logs_pruned: int = 0


def chunked(items: Sequence[int], size: int) -> list[list[int]]:
    """把 ID 序列切成固定大小的批次"""
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


async def _prune_paragraphs(
    workspace_id: int,
    now_ts: float,
    now_dt: datetime,
    *,
    action: str = AUTO_PRUNE_ACTION,
) -> list[int]:
    """失活低权重段落，返回被清理的段落 ID。

    游标只向前推进，而被清理的行会退出 `is_inactive=False` 过滤条件，
    所以这里必须用主键游标而不是 OFFSET —— 后者会因为结果集缩短而漏掉整页数据。
    """
    threshold = config.MEMORY_PRUNE_PARAGRAPH_THRESHOLD
    pruned_ids: list[int] = []
    cursor = 0

    while True:
        page = (
            await DBMemParagraph.filter(
                workspace_id=workspace_id,
                is_inactive=False,
                is_protected=False,
                is_frozen=False,
                id__gt=cursor,
            )
            .order_by("id")
            .limit(PRUNE_SCAN_PAGE_SIZE)
        )
        if not page:
            break
        cursor = page[-1].id

        victims = [
            paragraph.id for paragraph in page if paragraph.compute_effective_weight(now_ts) < threshold
        ]
        if victims:
            for chunk in chunked(victims, PRUNE_WRITE_CHUNK_SIZE):
                await DBMemParagraph.filter(id__in=chunk).update(
                    is_inactive=True,
                    last_manual_action=action,
                    last_manual_action_at=now_dt,
                    update_time=now_dt,
                )
            await memory_qdrant_manager.delete_paragraphs(victims)
            pruned_ids.extend(victims)

        await asyncio.sleep(0)

    return pruned_ids


async def _prune_relations(
    workspace_id: int,
    now_ts: float,
    now_dt: datetime,
    pruned_paragraph_ids: set[int],
) -> int:
    """失活低权重关系，以及来源段落已被清理的关系"""
    threshold = config.MEMORY_PRUNE_RELATION_THRESHOLD
    pruned_count = 0
    cursor = 0

    while True:
        page = (
            await DBMemRelation.filter(
                workspace_id=workspace_id,
                is_inactive=False,
                id__gt=cursor,
            )
            .order_by("id")
            .limit(PRUNE_SCAN_PAGE_SIZE)
        )
        if not page:
            break
        cursor = page[-1].id

        victims = [
            relation.id
            for relation in page
            if relation.compute_effective_weight(now_ts) < threshold
            or relation.paragraph_id in pruned_paragraph_ids
        ]
        for chunk in chunked(victims, PRUNE_WRITE_CHUNK_SIZE):
            await DBMemRelation.filter(id__in=chunk).update(is_inactive=True, update_time=now_dt)
        pruned_count += len(victims)

        await asyncio.sleep(0)

    return pruned_count


async def _prune_orphan_entities(workspace_id: int, now_dt: datetime) -> int:
    """失活不再被任何活跃关系引用、且只出现过一次的实体"""
    active_entity_ids: set[int] = set()
    cursor = 0
    while True:
        rows = (
            await DBMemRelation.filter(
                workspace_id=workspace_id,
                is_inactive=False,
                id__gt=cursor,
            )
            .order_by("id")
            .limit(PRUNE_SCAN_PAGE_SIZE)
            .values_list("id", "subject_entity_id", "object_entity_id")
        )
        if not rows:
            break
        cursor = rows[-1][0]
        for _relation_id, subject_entity_id, object_entity_id in rows:
            active_entity_ids.add(subject_entity_id)
            active_entity_ids.add(object_entity_id)
        await asyncio.sleep(0)

    pruned_count = 0
    cursor = 0
    while True:
        # `appearance_count > 1` 的实体不清理，这一条能下推到 SQL，不必拉回内存再过滤
        entity_ids: list[int] = (
            await DBMemEntity.filter(
                workspace_id=workspace_id,
                is_inactive=False,
                appearance_count__lte=1,
                id__gt=cursor,
            )
            .order_by("id")
            .limit(PRUNE_SCAN_PAGE_SIZE)
            .values_list("id", flat=True)
        )
        if not entity_ids:
            break
        cursor = entity_ids[-1]

        victims = [entity_id for entity_id in entity_ids if entity_id not in active_entity_ids]
        for chunk in chunked(victims, PRUNE_WRITE_CHUNK_SIZE):
            await DBMemEntity.filter(id__in=chunk).update(is_inactive=True, update_time=now_dt)
        pruned_count += len(victims)

        await asyncio.sleep(0)

    return pruned_count


async def prune_workspace_memories(workspace_id: int, *, action: str = AUTO_PRUNE_ACTION) -> MemoryPruneResult:
    """清理单个工作区的低价值记忆。

    Args:
        workspace_id: 工作区；0 是域分区。
        action: 记在被清理段落 `last_manual_action` 上的动作名。维护循环用默认的
            :data:`AUTO_PRUNE_ACTION`；管理员手动触发的（工作区页、运维命令）传
            :data:`MANUAL_PRUNE_ACTION`，捞回不认它。
    """
    if action not in PRUNE_ACTIONS:
        raise ValueError(f"未知的清理动作名: {action}")
    ensure_memory_system_enabled(MemoryOperation.PRUNE)

    now_ts = time.time()
    now_dt = datetime.now(timezone.utc)

    pruned_paragraph_ids = await _prune_paragraphs(workspace_id, now_ts, now_dt, action=action)
    relations_pruned = await _prune_relations(workspace_id, now_ts, now_dt, set(pruned_paragraph_ids))
    entities_pruned = await _prune_orphan_entities(workspace_id, now_dt)

    # 清理过期的强化日志（默认保留30天）
    log_cutoff_dt = datetime.fromtimestamp(now_ts - config.MEMORY_LOG_RETENTION_DAYS * 86400, tz=timezone.utc)
    logs_pruned = await DBMemReinforcementLog.filter(
        workspace_id=workspace_id,
        create_time__lt=log_cutoff_dt,
    ).delete()

    result = MemoryPruneResult(
        paragraphs_pruned=len(pruned_paragraph_ids),
        relations_pruned=relations_pruned,
        entities_pruned=entities_pruned,
        logs_pruned=logs_pruned,
    )
    logger.info(
        f"记忆清理完成: workspace={workspace_id}, "
        f"paragraphs={result.paragraphs_pruned}, relations={result.relations_pruned}, "
        f"entities={result.entities_pruned}, logs={result.logs_pruned}",
    )
    return result


async def prune_all_workspaces() -> dict[int, MemoryPruneResult]:
    """清理所有工作区的低价值记忆。

    闸开时频道记忆写在域分区 0，它不属于任何工作区，工作区遍历碰不到；所以遍历完工作区
    之后额外清理一次分区 0，结果字典的键是 0。函数内部三条扫描都是按主键游标分页的，
    分区再大也不会一次取全量。闸关时分区 0 不是一条有效的记忆归属，行为与今天相同。
    """
    results: dict[int, MemoryPruneResult] = {}
    # 先把存量稳定类型的半衰期补齐再清理（工作区行与域行都补）：否则还按情景半衰期衰减的
    # 偏好、事实、决定会在补齐之前被这一轮清掉。条件写在 UPDATE 里，重复执行不改任何行。
    # 补齐失败时这一轮不清任何段落，宁可多留几个小时，也不按旧半衰期把稳定信息清掉。
    # 关系同理：来源段落是稳定类型的关系先补长，否则它们按 1 天的旧半衰期在这一轮被清掉。
    extended = False
    try:
        await extend_stable_half_life()
        await extend_stable_relation_half_life()
        extended = True
    except Exception as e:
        logger.warning(f"补齐存量沉淀记忆半衰期失败，本轮跳过记忆清理: error={e}")

    cursor = 0
    while extended:
        workspace_ids: list[int] = (
            await DBWorkspace.filter(id__gt=cursor).order_by("id").limit(PRUNE_SCAN_PAGE_SIZE).values_list("id", flat=True)
        )
        if not workspace_ids:
            break
        cursor = workspace_ids[-1]

        for workspace_id in workspace_ids:
            try:
                results[workspace_id] = await prune_workspace_memories(workspace_id, action=AUTO_PRUNE_ACTION)
            except Exception as e:
                logger.warning(f"工作区记忆清理失败: workspace={workspace_id}, error={e}")
            # 让出事件循环：这条链路是后台维护，不能把主对话饿在后面排队
            await asyncio.sleep(PRUNE_WORKSPACE_INTERVAL_SECONDS)

    if extended and is_domain_scope_enabled():
        try:
            results[0] = await prune_workspace_memories(0, action=AUTO_PRUNE_ACTION)
        except Exception as e:
            logger.warning(f"域分区记忆清理失败: workspace=0, error={e}")
        await asyncio.sleep(PRUNE_WORKSPACE_INTERVAL_SECONDS)

    # 提示词明细按频道保留，不挂在任何工作区上；放在工作区与分区 0 都清完之后再按年龄
    # 扫一遍，处理不活跃频道留下的尾巴。失败只记日志，不影响本次记忆清理的结果。
    try:
        await prune_prompt_rounds(config.MEMORY_PROMPT_ROUNDS_MAX_AGE_DAYS)
    except Exception as e:
        logger.warning(f"提示词明细清理失败: error={e}")

    return results
