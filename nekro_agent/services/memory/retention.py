"""沉淀记忆的保留时长：按知识类型给半衰期

沉淀出的段落一律是情景记忆，半衰期取 `MEMORY_EPISODIC_HALF_LIFE_SECONDS`（默认 1 天），
不被召回强化的话几天之内就会降到自动清理线以下。对「某天抽到了什么」「几点开服」这类
时效内容这正合适；可偏好、事实、决定是关于人的稳定信息（忌口、生日、约好的事），
四五天就失活等于记了又忘。

所以沉淀段落按知识类型分两档：偏好、事实、决定用 `MEMORY_SEMANTIC_HALF_LIFE_DAYS`，
其余仍用情景半衰期。域行（闸开）与工作区行（闸关）同一口径：上游原版沉淀不写半衰期、
落到模型默认值 7200 秒，「1 天」本来就是本仓库自己定的，这里一并改。正文与类型都不动，
只改衰减速度；时效内容照样记，只是自然变旧。

存量段落由 :func:`extend_stable_half_life` 补齐：它只把半衰期**往长里**改，条件写在
UPDATE 自己的 WHERE 里，重复执行不会再改到任何一行，因此不需要「跑过了」的标记。
在这之前已经按旧半衰期被自动清理掉的稳定类型段落，由 :func:`restore_pruned_stable` 捞回；
捞回是**一次性**的修正，完整做完一遍就记下标记，之后不再跑（见那个函数的说明）。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import APP_SYSTEM_DIR
from nekro_agent.models.db_mem_paragraph import DBMemParagraph, KnowledgeType, OriginKind

logger = get_sub_logger("memory.retention")

# 清理写在 `last_manual_action` 上的两个动作名：维护循环自动清理记 `prune`（沿用旧值），
# 管理员在工作区页或用运维命令手动触发的清理记 `manual_prune`。捞回只认前者，
# 不会把管理员刚清掉的段落又捞回来。
AUTO_PRUNE_ACTION = "prune"
MANUAL_PRUNE_ACTION = "manual_prune"
PRUNE_ACTIONS: frozenset[str] = frozenset({AUTO_PRUNE_ACTION, MANUAL_PRUNE_ACTION})

# 被这次修正捞回的段落记的动作名（`last_manual_action` 最长 32 字符）
RETENTION_RESTORE_ACTION = "retention_restore"
# 一次最多捞回多少段：每段要嵌入一次，量大时分几次做完（启动时一次，之后每轮清理前一次，
# 直到做完一遍、记下标记）。只数真正捞回的，按新半衰期仍在线下的不占名额。
RESTORE_BATCH_LIMIT = 500
# 翻候选的页大小
RESTORE_SCAN_PAGE_SIZE = 200
# 连续失败这么多次就停：嵌入服务多半出了问题，接着打只会刷一排 WARNING
RESTORE_MAX_CONSECUTIVE_FAILURES = 5
# 做完一遍的标记：与记忆重建的状态文件放在一处
RESTORE_MARKER_PATH = Path(APP_SYSTEM_DIR) / "memory" / "retention_restore_v1.json"

# 关于人的稳定信息：这三类按语义半衰期保留，其余类型按情景半衰期。
STABLE_KNOWLEDGE_TYPES: frozenset[KnowledgeType] = frozenset(
    {KnowledgeType.PREFERENCE, KnowledgeType.FACT, KnowledgeType.DECISION},
)


# 语义半衰期天数的上限，与配置项的 `le` 一致：`half_life_seconds` 是 32 位整数列，约 24855 天就溢出
SEMANTIC_HALF_LIFE_MAX_DAYS = 3650


def stable_half_life_seconds() -> int:
    """稳定类型的半衰期（秒）"""
    days = min(max(1, int(config.MEMORY_SEMANTIC_HALF_LIFE_DAYS)), SEMANTIC_HALF_LIFE_MAX_DAYS)
    return days * 86400


def consolidation_half_life_seconds(knowledge_type: KnowledgeType) -> int:
    """一条沉淀段落该用的半衰期（秒），域行与工作区行同一口径"""
    if knowledge_type in STABLE_KNOWLEDGE_TYPES:
        return stable_half_life_seconds()
    return int(config.MEMORY_EPISODIC_HALF_LIFE_SECONDS)


async def extend_stable_half_life() -> int:
    """把存量里稳定类型的沉淀段落半衰期补到语义半衰期，返回改动的行数

    域行与工作区行都补；只动来源是沉淀的段落，好感里程碑（本来就冻结）与 CC 结论不碰。
    已经不短于目标值的行不改，所以可以在每次清理之前都调一次：新写的段落本来就是长的，
    这条 UPDATE 在第一次之后通常一行都不命中。
    """
    target = stable_half_life_seconds()
    changed = await DBMemParagraph.filter(
        origin_kind=OriginKind.CONSOLIDATION,
        knowledge_type__in=[kind.value for kind in STABLE_KNOWLEDGE_TYPES],
        half_life_seconds__lt=target,
    ).update(half_life_seconds=target)
    if changed:
        logger.info(f"存量沉淀记忆的稳定类型半衰期已补到 {target} 秒: rows={changed}")
    return int(changed or 0)


def restore_done() -> bool:
    """捞回是否已经完整做完过一遍"""
    try:
        return RESTORE_MARKER_PATH.is_file()
    except OSError:
        return False


def write_done_marker(path: Path, counts: dict[str, int]) -> None:
    """记下一次性修正「做完了」（只有时间与计数）；写不了只记一条 WARNING，下次启动会再完整走一遍"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps({"done_at": datetime.now(timezone.utc).isoformat(), **counts}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except OSError as e:
        logger.warning(f"记录一次性修正已做完失败，下次启动会再检查一遍: marker={path.name}, error={type(e).__name__}")


def _mark_restore_done(*, restored: int, below_line: int) -> None:
    write_done_marker(RESTORE_MARKER_PATH, {"restored_last_pass": restored, "left_below_line": below_line})


async def restore_pruned_stable(
    *,
    limit: int = RESTORE_BATCH_LIMIT,
    page_size: int = RESTORE_SCAN_PAGE_SIZE,
    max_consecutive_failures: int = RESTORE_MAX_CONSECUTIVE_FAILURES,
) -> int:
    """把按旧半衰期被自动清理掉的稳定类型段落捞回来（一次性），返回这一次捞回的段数

    范围：沉淀段落（域行与工作区行）、偏好 / 事实 / 决定三类、`last_manual_action` 是自动清理的
    :data:`AUTO_PRUNE_ACTION`（手动清理记 :data:`MANUAL_PRUNE_ACTION`，人工软删记 `delete`，
    隔离带 `quarantined_at`，都不在范围里），并且按新的半衰期算现在仍在清理线以上。
    其余类型被清掉的照旧不动。

    捞回 = 重新嵌入、写回向量、改回有效，动作记 :data:`RETENTION_RESTORE_ACTION`，后台据此
    看得出它是被这次修正捞回来的。从它抽出、跟着它一起被清掉的关系由
    `relation_retention.restore_pruned_stable_relations` 另行捞回。

    按主键游标翻完全部候选，名额只算真正捞回的（按新半衰期仍在线下的那些早该清掉，不能让它们
    占住前几百个位置、把后面该捞回的饿死）。**一次性**：完整翻完一遍、中途没有失败也没撞上名额，
    就写下 :data:`RESTORE_MARKER_PATH`，此后直接返回。以后调长半衰期或调低清理线不会再悄悄捞回
    一批；撞上名额或有失败时不写标记，下一轮接着做。连续失败 `max_consecutive_failures` 次就停。
    """
    if restore_done():
        return 0

    from nekro_agent.services.memory.embedding_service import embed_text
    from nekro_agent.services.memory.persons import build_paragraph_payload
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

    threshold = float(config.MEMORY_PRUNE_PARAGRAPH_THRESHOLD)
    target = stable_half_life_seconds()
    now_ts = time.time()
    restored = 0
    below_line = 0
    failed = 0
    consecutive_failures = 0
    hit_limit = False
    stopped = False
    cursor = 0
    while not (hit_limit or stopped):
        page = await (
            DBMemParagraph.filter(
                origin_kind=OriginKind.CONSOLIDATION,
                knowledge_type__in=[kind.value for kind in STABLE_KNOWLEDGE_TYPES],
                is_inactive=True,
                quarantined_at__isnull=True,
                last_manual_action=AUTO_PRUNE_ACTION,
                id__gt=cursor,
            )
            .order_by("id")
            .limit(max(1, int(page_size)))
        )
        if not page:
            break
        cursor = page[-1].id
        for row in page:
            if restored >= limit:
                hit_limit = True
                break
            if row.half_life_seconds < target:
                row.half_life_seconds = target
            if row.compute_effective_weight(now_ts) < threshold or not (row.content or "").strip():
                below_line += 1
                continue
            try:
                embedding = await embed_text(row.content, requester=(row.adapter_key, row.instance_key))
                row.embedding_ref = str(row.id)
                written = await memory_qdrant_manager.upsert_paragraph(
                    paragraph_id=row.id,
                    embedding=embedding,
                    payload=await build_paragraph_payload(row),
                )
                if not written:
                    raise RuntimeError("向量写入返回失败")
            except Exception as e:
                failed += 1
                consecutive_failures += 1
                logger.warning(f"捞回被清理的段落失败，下次再试: paragraph_id={row.id}, error={type(e).__name__}")
                if consecutive_failures >= max(1, int(max_consecutive_failures)):
                    stopped = True
                    logger.warning(f"捞回连续失败 {consecutive_failures} 次，先停下；下一轮再试")
                    break
                continue
            consecutive_failures = 0
            row.is_inactive = False
            row.last_manual_action = RETENTION_RESTORE_ACTION
            row.last_manual_action_at = datetime.now(timezone.utc)
            await row.save(
                update_fields=[
                    "is_inactive",
                    "half_life_seconds",
                    "last_manual_action",
                    "last_manual_action_at",
                    "embedding_ref",
                    "update_time",
                ],
            )
            restored += 1
    if restored:
        logger.info(f"按新的半衰期捞回了被自动清理的稳定类型沉淀记忆: rows={restored}")
    if not (hit_limit or stopped or failed):
        _mark_restore_done(restored=restored, below_line=below_line)
        logger.info(f"捞回已做完一遍，之后不再执行: restored={restored}, left_below_line={below_line}")
    return restored


__all__ = [
    "AUTO_PRUNE_ACTION",
    "MANUAL_PRUNE_ACTION",
    "PRUNE_ACTIONS",
    "RESTORE_BATCH_LIMIT",
    "RESTORE_MARKER_PATH",
    "RETENTION_RESTORE_ACTION",
    "STABLE_KNOWLEDGE_TYPES",
    "consolidation_half_life_seconds",
    "extend_stable_half_life",
    "restore_done",
    "restore_pruned_stable",
    "stable_half_life_seconds",
    "write_done_marker",
]
