"""记忆系统功能开关辅助。"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover - 只在类型检查时需要频道模型，避免模块导入期成环
    from nekro_agent.models.db_chat_channel import DBChatChannel

logger = get_sub_logger("memory.feature_flags")


class MemoryOperation(StrEnum):
    CONTEXT_INJECTION = "context_injection"
    RETRIEVAL = "retrieval"
    CONSOLIDATION = "consolidation"
    CC_HANDSHAKE = "cc_handshake"
    CC_SEMANTIC_PERSIST = "cc_semantic_persist"
    REBUILD = "rebuild"
    PRUNE = "prune"
    EPISODE_AGGREGATION = "episode_aggregation"
    SCHEDULER = "scheduler"


_MEMORY_OPERATION_LABELS: dict[MemoryOperation, str] = {
    MemoryOperation.CONTEXT_INJECTION: "记忆上下文注入",
    MemoryOperation.RETRIEVAL: "记忆检索",
    MemoryOperation.CONSOLIDATION: "记忆沉淀",
    MemoryOperation.CC_HANDSHAKE: "CC 记忆握手",
    MemoryOperation.CC_SEMANTIC_PERSIST: "CC 语义记忆沉淀",
    MemoryOperation.REBUILD: "记忆重建",
    MemoryOperation.PRUNE: "记忆清理",
    MemoryOperation.EPISODE_AGGREGATION: "Episode 聚合",
    MemoryOperation.SCHEDULER: "记忆调度器",
}


def is_memory_system_enabled() -> bool:
    """返回记忆系统总开关状态。"""
    return bool(config.MEMORY_ENABLE_SYSTEM)


def is_domain_scope_enabled() -> bool:
    """返回记忆是否按（域, 人设）分区检索。

    回滚闸。**每次调用现读配置、不缓存**：把闸从开切回关之后，下一次检索必须立刻只看
    工作区行；缓存会让回滚变成「重启后生效」，而回滚正是出问题时才做的事。

    闸关时与改造前逐字相同；闸开时检索、注入与 `memory_tools` 都按新的条件树工作。
    本函数只回答「分区与过滤条件用哪种形状」，不替调用方读频道开关。
    """
    return bool(config.MEMORY_DOMAIN_SCOPE_ENABLED)


async def is_memory_enabled_for_channel(channel: "DBChatChannel") -> bool:
    """这个频道有没有聊天记忆。

    闸关时沿用改造前的两道闸（总开关 + 绑了工作区），闸开时改看频道生效的
    `MEMORY_ENABLE_FOR_CHAT`、与绑不绑工作区无关。**入口处读、不缓存**：配置是按频道覆盖的，
    缓存会把「刚关掉的那个频道」继续沉淀一轮。

    读配置失败按 False 处理（fail-closed）：这里回答的是「能不能写 / 能不能注入」，
    算不出来时宁可什么都不做，也不要把没有授权的记忆写进去或读出来。
    """
    if not is_memory_system_enabled():
        return False
    if not is_domain_scope_enabled():
        return channel.workspace_id is not None
    try:
        effective = await channel.get_effective_config()
    except Exception as e:
        channel_label = getattr(channel, "chat_key", "") or getattr(channel, "id", "")
        logger.warning(f"读取频道聊天记忆开关失败，按关闭处理: chat_key={channel_label}, error={e}")
        return False
    return bool(effective.MEMORY_ENABLE_FOR_CHAT)


def get_memory_disabled_reason(operation: MemoryOperation) -> str:
    """构造统一的禁用提示。"""
    operation_label = _MEMORY_OPERATION_LABELS[operation]
    return f"记忆系统已禁用，无法执行{operation_label}"


def ensure_memory_system_enabled(operation: MemoryOperation) -> None:
    """要求记忆系统已启用，否则抛出统一错误。"""
    if not is_memory_system_enabled():
        raise ValidationError(reason=get_memory_disabled_reason(operation))
