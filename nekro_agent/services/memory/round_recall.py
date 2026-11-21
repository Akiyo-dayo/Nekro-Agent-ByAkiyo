"""本轮召回记录

## 为什么要有

记忆块在主对话之前渲染并注入，强化（`base_weight` 读改写 + 一行强化日志）原本就发生在
检索之后。于是一轮主对话迭代几次、渲染几次，同一条记忆就被强化几次；被审核拦下 / 被供应商
拒答的那一轮注入的段落也照样被强化，越被拦越「重要」。

所以注入链路只把「这一轮真正注入了哪些记忆」记在进程内，由 `run_agent` 在正常收尾后
一次性补记：被拦、超时、无进展、没写代码的轮次直接把记录丢掉，只有正常收尾才强化。

## 只在进程内

记录只活一轮，`run_agent` 的收尾用 `finally` 保证弹出。进程重启就没了，最坏情况是那一轮
没强化成功、记忆少长一点——强化本来就是按轮次慢慢累积的，不值得为它落库。

## 上限与淘汰

正常情况每轮结束都会弹出，字典天然很小。万一有渲染路径没经过 `run_agent` 的收尾（现在
没有），条目会一直躺在里面；超过 `_MAX_ROUNDS` 时按插入顺序丢掉最早记下的一半。丢掉的只是
「这一轮该补记哪些记忆」，最坏结果是那几轮不强化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Sequence

from nekro_agent.core.logger import get_sub_logger

if TYPE_CHECKING:
    from nekro_agent.services.memory.instance_scope import MemoryScope
    from nekro_agent.services.memory.retriever import RetrievedMemory

logger = get_sub_logger("memory.round_recall")

# 条目上限：正常情况每轮结束都会弹出，天然很小。超出时按插入顺序丢掉最早记下的一半。
_MAX_ROUNDS = 4096


@dataclass
class _RoundRecall:
    """一轮里各次注入的候选与第一次注入时的检索条件。"""

    workspace_id: int
    scope: MemoryScope
    query: str
    memories: list[RetrievedMemory] = field(default_factory=list)


_round_recalls: Dict[str, _RoundRecall] = {}


def stage_round_recall(
    one_time_code: str,
    *,
    workspace_id: int,
    scope: MemoryScope,
    query: str,
    memories: Sequence[RetrievedMemory],
) -> None:
    """把这一次注入的候选追加到本轮记录里。

    同一轮多次迭代会调用多次，追加而不是覆盖；工作区、隔离范围与 query 一轮里不会变，
    记第一次的即可。
    """
    record = _round_recalls.get(one_time_code)
    if record is None:
        if len(_round_recalls) >= _MAX_ROUNDS:
            _drop_oldest_half()
        record = _RoundRecall(workspace_id=workspace_id, scope=scope, query=query)
        _round_recalls[one_time_code] = record
    record.memories.extend(memories)


async def commit_round_recall(one_time_code: str) -> int:
    """弹出本轮记录并按追加顺序补记一次强化，返回合并后的条数。

    各次追加的候选按 `(source_type, target_id)` 去重、先到先留。强化失败只记 debug：
    记忆是风味不是功能，不能影响主对话收尾。
    """
    try:
        record = _round_recalls.pop(one_time_code, None)
        if record is None:
            return 0

        merged: list[RetrievedMemory] = []
        seen: set[tuple[str, int]] = set()
        for memory in record.memories:
            key = (memory.source_type, memory.target_id)
            if key in seen:
                continue
            seen.add(key)
            merged.append(memory)
        if not merged:
            return 0

        # 延迟 import：`retriever` 会拖进 Qdrant 等整条导入链，`run_agent` 顶层 import
        # 本模块时不该连带把它们都拖进来。
        from nekro_agent.services.memory.retriever import MemoryRetriever

        await MemoryRetriever(record.workspace_id, scope=record.scope).record_access(merged, record.query)
        return len(merged)
    except Exception as e:
        logger.debug(f"本轮记忆强化失败（可忽略）: code={one_time_code}, error={e}")
        return 0


def discard_round_recall(one_time_code: str) -> None:
    """弹出并丢弃本轮记录，没有也不报错。"""
    _round_recalls.pop(one_time_code, None)


def reset_round_recalls() -> None:
    """清空全部记录，仅供测试使用。"""
    _round_recalls.clear()


def _drop_oldest_half() -> None:
    for one_time_code in list(_round_recalls)[: max(1, len(_round_recalls) // 2)]:
        _round_recalls.pop(one_time_code, None)


__all__ = [
    "commit_round_recall",
    "discard_round_recall",
    "reset_round_recalls",
    "stage_round_recall",
]
