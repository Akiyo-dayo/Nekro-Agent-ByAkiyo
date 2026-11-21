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
from nekro_agent.services.memory.instance_scope import CONSOLE_ONLY_PRESET_ID, CONSOLE_USER_CHAT_KEY
from nekro_agent.services.memory.persons import build_paragraph_payload
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
        instance_key: 来源实例标识。`source_chat_key` 是 `"__user__"` 这类伪值时不代表任何
            频道，本函数会把它强制收成空串（见下面的收口），调用方传什么都不影响结果。
        preset_id: 来源频道当前生效的人设标识。与 `instance_key` 成对：从某个频道委托出去的
            任务两个都要带上，否则那条结论会跨域/跨人设可见。
            **控制台直发（`__user__`）的结论一律落进 `CONSOLE_ONLY_PRESET_ID`**——
            一个人设 ID 永远不可能是的负数，于是任何群和私聊都召回不到它
            （频道侧只放行「当前人设」与 0），而后台记忆页与 CC 握手的
            `MemoryScope.unrestricted()` 不按人设过滤，照旧读得到。
    """
    if not is_memory_system_enabled():
        return None

    # 以控制台身份写回的**唯一**出口收口在这里：不管调用方传了什么，
    # `__user__` 的结论都落成「不属于任何实例 + 仅控制台可见的那一档」。
    # 放在函数开头（而不是散在各个调用点）是因为这条判据是"这条结论从哪来"，
    # 任何新增的控制台写回路径都会自动落进这一档，不需要各自记得传对参数。
    if source_chat_key == CONSOLE_USER_CHAT_KEY:
        if instance_key != "" or preset_id != CONSOLE_ONLY_PRESET_ID:
            logger.debug(
                f"控制台直发的 CC 结论强制落入仅控制台档: workspace={workspace_id}, "
                f"传入 instance_key={instance_key!r}, preset_id={preset_id}",
            )
        instance_key = ""
        preset_id = CONSOLE_ONLY_PRESET_ID

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
            payload=await build_paragraph_payload(paragraph),
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
