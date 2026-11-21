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

## 本轮已经注入过哪些

同一轮里迭代时的增量渲染只该补**还没出现在提示词里**的记忆。这里另存一份按
`one_time_code` 的已注入键集合，由注入链路在编排成功、真正拼进提示词之后写入，增量渲染
读取它做排除；`commit_round_recall` 与 `discard_round_recall` 收尾时一并清掉。

## 本轮筛选过哪些

自动注入前的模型筛选会在同一轮里被增量渲染再次问到。这里按 `one_time_code` 另存一份
`(source_type, target_id) -> 是否留下` 的结论：已经判过的候选不再发给模型，只把新出现的
送过去。结论与已注入键集合同一套上限与清理方式，轮末一并清掉。

## 上限与淘汰

正常情况每轮结束都会弹出，字典天然很小。万一有渲染路径没经过 `run_agent` 的收尾（现在
没有），条目会一直躺在里面；超过 `_MAX_ROUNDS` 时按插入顺序丢掉最早记下的一半。丢掉的只是
「这一轮该补记哪些记忆」，最坏结果是那几轮不强化。已注入键集合与筛选结论同样按插入顺序
淘汰，单个令牌下超过 `_MAX_INJECTED_KEYS` 时丢掉最早的那些。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Iterable, Mapping, Sequence

from nekro_agent.core.logger import get_sub_logger

if TYPE_CHECKING:
    from nekro_agent.services.memory.instance_scope import MemoryScope
    from nekro_agent.services.memory.retriever import RetrievedMemory

logger = get_sub_logger("memory.round_recall")

# 条目上限：正常情况每轮结束都会弹出，天然很小。超出时按插入顺序丢掉最早记下的一半。
_MAX_ROUNDS = 4096

# 单个令牌下最多记多少条已注入键；超出时丢掉最早记下的那些。
_MAX_INJECTED_KEYS = 4096


@dataclass
class _RoundRecall:
    """一轮里各次注入的候选与第一次注入时的检索条件。"""

    workspace_id: int
    scope: MemoryScope
    query: str
    memories: list[RetrievedMemory] = field(default_factory=list)


_round_recalls: Dict[str, _RoundRecall] = {}
# `OrderedDict` 只用来保留插入顺序：值是占位，语义上是一组已注入键。
_round_injected: Dict[str, "OrderedDict[tuple[str, int], None]"] = {}
# 按 `(source_type, target_id)` 记这一轮筛选过的结论；`OrderedDict` 只用来保留插入顺序。
_round_judged: Dict[str, "OrderedDict[tuple[str, int], bool]"] = {}


def note_injected(one_time_code: str, keys: Iterable[tuple[str, int]]) -> None:
    """记下这一轮真正注入过提示词的记忆键。

    第一次渲染和迭代时的增量渲染都调用；增量渲染据此只补本轮还没出现过的条目。
    空令牌不记：没有令牌就没有「同一轮」的概念，记下来也没人清。
    """
    if not one_time_code:
        return
    bucket = _round_injected.get(one_time_code)
    if bucket is None:
        if len(_round_injected) >= _MAX_ROUNDS:
            _drop_oldest_injected_half()
        bucket = OrderedDict()
        _round_injected[one_time_code] = bucket
    for key in keys:
        bucket[key] = None
    while len(bucket) > _MAX_INJECTED_KEYS:
        bucket.popitem(last=False)


def injected_keys(one_time_code: str) -> frozenset[tuple[str, int]]:
    """返回这一轮已经注入过的记忆键；没有记录时返回空集。"""
    bucket = _round_injected.get(one_time_code)
    if not bucket:
        return frozenset()
    return frozenset(bucket)


def note_judged(one_time_code: str, verdicts: Mapping[tuple[str, int], bool]) -> None:
    """记下这一轮模型筛过的记忆键与结论。

    第一次渲染与迭代时的增量渲染都调用；增量渲染据此只把还没判过的候选送出去。
    空令牌不记：没有令牌就没有「同一轮」的概念，记下来也没人清。
    """
    if not one_time_code or not verdicts:
        return
    bucket = _round_judged.get(one_time_code)
    if bucket is None:
        if len(_round_judged) >= _MAX_ROUNDS:
            _drop_oldest_judged_half()
        bucket = OrderedDict()
        _round_judged[one_time_code] = bucket
    for key, kept in verdicts.items():
        bucket[key] = bool(kept)
    while len(bucket) > _MAX_INJECTED_KEYS:
        bucket.popitem(last=False)


def judged_verdicts(one_time_code: str) -> dict[tuple[str, int], bool]:
    """返回这一轮已经筛过的记忆键与结论；没有记录时返回空字典。"""
    bucket = _round_judged.get(one_time_code)
    if not bucket:
        return {}
    return dict(bucket)


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
        _round_injected.pop(one_time_code, None)
        _round_judged.pop(one_time_code, None)
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
    _round_injected.pop(one_time_code, None)
    _round_judged.pop(one_time_code, None)


def reset_round_recalls() -> None:
    """清空全部记录，仅供测试使用。"""
    _round_recalls.clear()
    _round_injected.clear()
    _round_judged.clear()


def _drop_oldest_half() -> None:
    for one_time_code in list(_round_recalls)[: max(1, len(_round_recalls) // 2)]:
        _round_recalls.pop(one_time_code, None)


def _drop_oldest_injected_half() -> None:
    for one_time_code in list(_round_injected)[: max(1, len(_round_injected) // 2)]:
        _round_injected.pop(one_time_code, None)


def _drop_oldest_judged_half() -> None:
    for one_time_code in list(_round_judged)[: max(1, len(_round_judged) // 2)]:
        _round_judged.pop(one_time_code, None)


__all__ = [
    "commit_round_recall",
    "discard_round_recall",
    "injected_keys",
    "judged_verdicts",
    "note_injected",
    "note_judged",
    "reset_round_recalls",
    "stage_round_recall",
]
