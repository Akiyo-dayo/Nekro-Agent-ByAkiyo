"""CC 语义记忆写入器。

将 CC 任务的输入与输出沉淀为长期语义记忆，供后续委托握手与检索复用。
"""

from __future__ import annotations

from datetime import datetime

from tortoise.timezone import now as db_now

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType, MemorySource
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory.embedding_service import embed_text
from nekro_agent.services.memory.feature_flags import is_memory_system_enabled
from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
from nekro_agent.services.memory.text_terms import extract_entity_terms

logger = get_sub_logger("memory.semantic_writer")


def _build_semantic_content(task_content: str, result_content: str) -> str:
    """构造用于沉淀的第三人称语义记忆文本。"""
    task = task_content.strip().replace("\n", " ")[: config.MEMORY_SEMANTIC_MAX_TASK_LENGTH]
    result = result_content.strip()[: config.MEMORY_SEMANTIC_MAX_RESULT_LENGTH]
    return f"CC 在当前工作区处理了任务“{task}”，并产出以下可复用结论或结果：\n{result}"


async def persist_cc_task_memory(
    workspace_id: int,
    task_content: str,
    result_content: str,
    source_chat_key: str = "__user__",
    origin_ref: str | None = None,
    event_time: datetime | None = None,
    instance_key: str = "",
    preset_id: int = 0,
) -> DBMemParagraph | None:
    """将 CC 任务结果沉淀为语义记忆。

    Args:
        instance_key: 来源实例标识。`source_chat_key` 可能是 `"__user__"` 这类伪值，
            此时并不对应真实频道，调用方保持默认空串即可（该记忆对所有实例可见）。
        preset_id: 来源频道当前生效的人设标识，0 = 无人设档。与 `instance_key` 成对：
            控制台直发（`__user__`）两个都留默认，落成"不属于任何实例、任何人设"的一档；
            从某个频道委托出去的任务则两个都要带上，否则那条结论会跨域/跨人设可见。
    """
    if not is_memory_system_enabled():
        return None

    task = task_content.strip()
    result = result_content.strip()
    if not task or len(result) < config.MEMORY_SEMANTIC_MIN_RESULT_LENGTH:
        return None

    content = _build_semantic_content(task, result)
    summary = task[: config.MEMORY_SEMANTIC_MAX_SUMMARY_LENGTH]

    if origin_ref:
        existing = await DBMemParagraph.filter(
            workspace_id=workspace_id,
            instance_key=instance_key,
            preset_id=preset_id,
            memory_source="cc",
            cognitive_type=CognitiveType.SEMANTIC,
            origin_kind=OriginKind.TASK,
            origin_ref=origin_ref,
        ).first()
        if existing is not None:
            return existing

    paragraph = await DBMemParagraph.create(
        workspace_id=workspace_id,
        instance_key=instance_key,
        preset_id=preset_id,
        memory_source="cc",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.EXPERIENCE,
        content=content,
        summary=summary,
        # `event_time` 是 `DatetimeField`，写它的时间一律取 `tortoise.timezone.now()`：
        # 朴素值在 ORM 时区 ≠ 进程本地时区时会整体错开一个偏移量，而检索侧的时间窗
        # （`retrieve(time_from=..., time_to=...)`）拿的是 aware 值来比。
        event_time=event_time or db_now(),
        half_life_seconds=config.MEMORY_SEMANTIC_HALF_LIFE_DAYS * 24 * 3600,
        origin_kind=OriginKind.TASK,
        origin_ref=origin_ref,
        origin_chat_key=source_chat_key,
    )

    try:
        embedding = await embed_text(content)
        await memory_qdrant_manager.upsert_paragraph(
            paragraph_id=paragraph.id,
            embedding=embedding,
            payload=paragraph.to_qdrant_payload(),
        )
        paragraph.embedding_ref = str(paragraph.id)
        await paragraph.save(update_fields=["embedding_ref"])
    except Exception as e:
        logger.warning(f"CC 语义记忆向量化失败，已仅保存数据库记录: {e}")

    logger.info(f"CC 语义记忆已沉淀: workspace={workspace_id}, paragraph_id={paragraph.id}")
    await _persist_cc_task_relations(workspace_id, paragraph.id, task, result, instance_key, preset_id)
    return paragraph


async def _persist_cc_task_relations(
    workspace_id: int,
    paragraph_id: int,
    task_content: str,
    result_content: str,
    instance_key: str,
    preset_id: int,
) -> None:
    """从 CC 任务文本中保守生成少量实体与关系。

    切词走 `text_terms`，与检索侧共用同一套规则——写进去的词和查询切出来的词必须是同一
    量级的东西，否则图谱建了也召不回。改造前这里是纯空格切分，中文任务描述没有空格，
    整句会被当成一个"实体名"登记，产出的噪声实体又会顺着关系召回的 `icontains` 污染检索。
    """
    entity_names = extract_entity_terms(
        f"{task_content[: config.MEMORY_SEMANTIC_MAX_TASK_LENGTH]}\n"
        f"{result_content[: config.MEMORY_SEMANTIC_MAX_RESULT_LENGTH]}",
    )
    if len(entity_names) < 2:
        return

    entities: list[DBMemEntity] = []
    for name in entity_names:
        entity, _ = await DBMemEntity.find_or_create(
            workspace_id=workspace_id,
            entity_type=EntityType.CONCEPT,
            name=name,
            source=MemorySource.CC,
            instance_key=instance_key,
            preset_id=preset_id,
        )
        entities.append(entity)

    for idx in range(len(entities) - 1):
        await DBMemRelation.find_or_create(
            workspace_id=workspace_id,
            subject_entity_id=entities[idx].id,
            predicate="related_to",
            object_entity_id=entities[idx + 1].id,
            paragraph_id=paragraph_id,
            memory_source="cc",
            cognitive_type=CognitiveType.SEMANTIC.value,
            instance_key=instance_key,
            preset_id=preset_id,
        )
