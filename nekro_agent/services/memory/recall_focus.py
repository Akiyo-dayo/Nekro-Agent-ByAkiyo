"""闸开时自动注入记忆的排序与冷却旁路（纯函数）

验收时段落召回对题率低。原来按「衰减后权重 × 相似度」取前几条，几小时前的闲聊权重高，
压过几天前真正相关的偏好，相似度很低的尾巴也一并注入。这里改成：

- 按相似度排，权重只在相似度几乎一样（相差不到 0.02）时起作用；
- 相似度低于 `DOMAIN_MIN_SIMILARITY` 的不要，比本轮最高相似度低出 `RELATIVE_BAND` 以上的也不要。

两个阈值来自实际部署上的回放评测（同一批历史轮次、同一时刻的记忆库，新旧两套逻辑各算一遍，
由模型盲评对不对题，人工抽查核对）：对题的段落相似度中位数约 0.59、不对题的约 0.56，
下限取 0.55 去掉了四成多不对题的条目，只丢一成对题的；注入精度从约 18% 升到约 26%，
有注入的轮次里至少一条对题的比例基本不变或略升，答案明确的问答没有变差。

当时还试过两件事，回放显示都会丢掉对题的记忆，所以没有采用：只拿触发句检索（群里前几句的
上下文常常正是要找的东西），以及寒暄、短句不检索段落（被跳过的轮次里约一半原本能召回对题的）。

另外，问句说明对方在直接问，被召回冷却挡掉的那条要更容易放出来：问句用低一档的冷却旁路阈值
（管理员把旁路设成 1.0 关掉时不放宽）。

闸关路径不经过这里，行为与改造前逐字相同。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from nekro_agent.tools.at_markup import AT_MARKUP_PATTERN

# 注入时的相似度下限与相对截断：比本轮最高相似度低出这么多的不要。
DOMAIN_MIN_SIMILARITY = 0.55
RELATIVE_BAND = 0.15
# 相似度差不到这个数时视为一样，再看权重
SIMILARITY_TIE = 0.02

# 问句的冷却旁路阈值（不高于配置值）
QUESTION_BYPASS_SIMILARITY = 0.7

_QUESTION_MARKS = ("?", "？")
_QUESTION_WORDS = (
    "吗",
    "呢",
    "嘛",
    "啥",
    "什么",
    # 「几」单独出现多半是「几天」「几个人」这种约数，只认明确在问的搭配
    "几号",
    "几点",
    "几岁",
    "几时",
    "几月",
    "第几",
    "谁",
    "哪",
    "怎么",
    "怎样",
    "为什么",
    "为啥",
    "多少",
    "是不是",
    "有没有",
    "记得",
    "还记",
)


def strip_markup(text: str) -> str:
    """去掉 @ 标记，只留说的话"""
    return AT_MARKUP_PATTERN.sub(" ", text or "").strip()


def is_question(text: str) -> bool:
    """粗判是不是在问：带问号，或带常见的疑问词"""
    body = strip_markup(text)
    if any(mark in body for mark in _QUESTION_MARKS):
        return True
    return any(word in body for word in _QUESTION_WORDS)


def rank_for_injection(
    memories: Sequence[Any],
    *,
    min_similarity: float = DOMAIN_MIN_SIMILARITY,
    relative_band: float = RELATIVE_BAND,
    similarity: Callable[[Any], float] = lambda memory: float(memory.similarity_score),
    weight: Callable[[Any], float] = lambda memory: float(memory.effective_weight),
) -> list[Any]:
    """相似度优先排序，去掉绝对下限以下与比最好那条低太多的尾巴

    从相似度最高的往下分组：与组里最高那条相差不到 `SIMILARITY_TIE` 的归进同一组，组内按权重排
    （新近、被强化过的靠前）；差到 `SIMILARITY_TIE` 及以上就开新的一组。
    """
    kept = [memory for memory in memories if similarity(memory) >= min_similarity]
    if not kept:
        return []
    best = max(similarity(memory) for memory in kept)
    kept = [memory for memory in kept if similarity(memory) >= best - relative_band]
    groups: list[list[Any]] = []
    for memory in sorted(kept, key=similarity, reverse=True):
        if groups and similarity(groups[-1][0]) - similarity(memory) < SIMILARITY_TIE:
            groups[-1].append(memory)
        else:
            groups.append([memory])
    return [memory for group in groups for memory in sorted(group, key=weight, reverse=True)]


def cooldown_bypass_similarity(configured: float, *, question: bool) -> float:
    """问句用低一档的冷却旁路阈值，但不高于配置值

    配置成 1.0 及以上是管理员关掉了旁路（相似度到不了），问句也不放宽。
    """
    if not question or configured >= 1.0:
        return configured
    return min(configured, QUESTION_BYPASS_SIMILARITY)


__all__ = [
    "DOMAIN_MIN_SIMILARITY",
    "QUESTION_BYPASS_SIMILARITY",
    "RELATIVE_BAND",
    "cooldown_bypass_similarity",
    "is_question",
    "rank_for_injection",
    "strip_markup",
]
