"""记忆召回冷却（进程内）

被自动注入过的记忆在接下来若干轮里不再出现，免得同一段旧事每轮都占着提示词的位置；
相似度足够高时视为「这一轮直接问到它」，照常注入。

## 只在进程内

记录不落库：重启之后冷却表清空，最坏的结果是重启后的第一轮把某段记忆多注入一次。
这是可以接受的——冷却的目的是让提示词里的记忆轮换，不是授权或计费，多注入一次没有副作用，
而为一轮性的状态写库、清理、迁移反而引入新的失败面。

## 轮次怎么数

键是 `(chat_key, preset_id)`：同一个频道、同一个人设各自记轮次。一轮由 `one_time_code`
标识；同一轮里主对话迭代几次、渲染几次都只算一轮，只有拿到一个新的 `one_time_code`
才推进轮次计数。这样同一轮的前后两次渲染结果一致。

## 上限与淘汰

照 `round_recall` 的写法：键超过上限时丢最早的一半，单个键里的记忆表超过上限时丢掉
轮次最旧的一半。丢掉的只是冷却记录，最坏结果是那几段记忆提前再次注入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Sequence, Tuple

from nekro_agent.core.logger import get_sub_logger

if TYPE_CHECKING:  # pragma: no cover - 只用于类型标注，避免把检索链拖进导入期
    from nekro_agent.services.memory.retriever import RetrievedMemory

logger = get_sub_logger("memory.recall_cooldown")

# 键是 (chat_key, preset_id)。闸开时人设标识总能解析出来，这里仍允许 None，
# 因为 `MemoryScope.unrestricted()` 那一档的人设维是「不按人设过滤」。
CooldownKey = tuple[str, int | None]
# 一条记忆的身份与检索结果、强化记录用的是同一套 `(source_type, target_id)`。
MemoryKey = Tuple[str, int]

# 键数上限。绝大多数部署的频道数远小于这个量级。
MAX_KEYS = 4096
# 单个键里的记忆表上限。一个频道在冷却窗口里同时活跃的记忆不会有这么多。
MAX_MEMORIES_PER_KEY = 512


@dataclass
class _CooldownState:
    """一个 `(chat_key, preset_id)` 的轮次与「记忆 → 最近注入轮次」表。"""

    one_time_code: str
    round_index: int
    injected: Dict[MemoryKey, int] = field(default_factory=dict)


_cooldowns: Dict[CooldownKey, _CooldownState] = {}


def _advance(state: _CooldownState, one_time_code: str) -> int:
    """把状态推进到这次渲染所在的轮次，返回当前轮次。

    同一个 `one_time_code` 是同一轮：主对话迭代几次都只算一轮，只有换了 code 才推进。
    """
    if state.one_time_code != one_time_code:
        state.one_time_code = one_time_code
        state.round_index += 1
    return state.round_index


def filter_cooled(
    key: CooldownKey,
    one_time_code: str,
    memories: Sequence["RetrievedMemory"],
    *,
    rounds: int,
    bypass_similarity: float,
) -> list["RetrievedMemory"]:
    """去掉还在冷却期内的记忆，保持入参顺序。

    `rounds <= 0` 表示不冷却，原样返回。同一轮（轮次差为 0）不去掉——一轮里前后两次渲染
    必须一致。相似度达到 `bypass_similarity` 时也不去掉：这一轮已经直接问到它了。
    """
    if rounds <= 0:
        return list(memories)

    state = _cooldowns.get(key)
    if state is None:
        if len(_cooldowns) >= MAX_KEYS:
            _drop_oldest_keys()
        state = _CooldownState(one_time_code=one_time_code, round_index=0)
        _cooldowns[key] = state
    # 轮次由 `one_time_code` 推进，而不是由「这次有没有注入」推进：所有候选都被冷却掉的
    # 轮次不会调 `mark_injected`，如果这里不推进，下一轮就永远停在同一个差值上。
    current_round = _advance(state, one_time_code)
    kept: list["RetrievedMemory"] = []
    for memory in memories:
        recorded_round = state.injected.get((memory.source_type, memory.target_id))
        if recorded_round is None:
            kept.append(memory)
            continue
        gap = current_round - recorded_round
        if 1 <= gap <= rounds and memory.similarity_score < bypass_similarity:
            continue
        kept.append(memory)
    return kept


def mark_injected(
    key: CooldownKey,
    one_time_code: str,
    memories: Sequence["RetrievedMemory"],
) -> None:
    """把这些记忆记成当前轮注入。

    在真正注入了提示词之后调用（编排结果非空才算），不等主对话成功：冷却关心的是
    「提示词里已经出现过」，不是「主对话有没有用它」。
    """
    state = _cooldowns.get(key)
    if state is None:
        if len(_cooldowns) >= MAX_KEYS:
            _drop_oldest_keys()
        state = _CooldownState(one_time_code=one_time_code, round_index=0)
        _cooldowns[key] = state
    current_round = _advance(state, one_time_code)
    for memory in memories:
        state.injected[(memory.source_type, memory.target_id)] = current_round
    if len(state.injected) > MAX_MEMORIES_PER_KEY:
        _drop_oldest_memories(state)


def reset_recall_cooldown() -> None:
    """清空全部冷却记录，仅供测试使用。"""
    _cooldowns.clear()


def _drop_oldest_keys() -> None:
    for key in list(_cooldowns)[: max(1, len(_cooldowns) // 2)]:
        _cooldowns.pop(key, None)


def _drop_oldest_memories(state: _CooldownState) -> None:
    """丢掉轮次最旧的一半记忆记录。"""
    oldest = sorted(state.injected, key=lambda memory_key: state.injected[memory_key])
    for memory_key in oldest[: max(1, len(oldest) // 2)]:
        state.injected.pop(memory_key, None)


__all__ = [
    "MAX_KEYS",
    "MAX_MEMORIES_PER_KEY",
    "filter_cooled",
    "mark_injected",
    "reset_recall_cooldown",
]
