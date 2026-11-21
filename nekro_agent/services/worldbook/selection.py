"""入选排序与预算截断、按位置的渲染分组

排序规则只有一处，且两个方向都要：

- **入选顺序**按 `order` 从大到小：`order` 大的更重要，先占预算。同值按条目 `id` 从
  小到大，保证同一批数据每次选出的是同一批（没有这一条，`order` 相同的条目会随数据库
  返回顺序漂移，同一轮两次渲染可能注入不同的条目）。
- **渲染顺序**按 `order` 从小到大：`order` 大的离聊天记录末尾更近，与酒馆「顺序越大
  越靠后」一致。

截断是「整条跳过」而不是「把正文切一半」：切一半的条目会给模型一段没有结尾的设定，
比不注入更糟，而且管理员永远不知道被切在哪里。因此一条放不下就整条跳过，并记下原因
（全局字数、书预算、条数上限），供明细页解释「为什么这条没进来」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .matcher import HitReason
from .types import MatchEntry, Position


@dataclass(frozen=True)
class Hit:
    """一条命中的条目及其原因"""

    entry: MatchEntry
    reason: HitReason


@dataclass(frozen=True)
class Selection:
    """入选结果

    Attributes:
        selected: 入选的条目，按入选顺序（`order` 大到小）。
        dropped: 被预算跳过的条目与原因 `global_chars` / `book_chars` / `max_entries`。
        total_chars: 入选正文的总字数。
    """

    selected: tuple[Hit, ...] = ()
    dropped: tuple[tuple[Hit, str], ...] = ()
    total_chars: int = 0


def _selection_key(hit: Hit) -> tuple[int, int]:
    """入选顺序：`order` 大到小，同值按 `id` 小到大"""
    return (-hit.entry.order, hit.entry.id)


def _render_key(hit: Hit) -> tuple[int, int]:
    """渲染顺序：`order` 小到大，同值按 `id` 小到大"""
    return (hit.entry.order, hit.entry.id)


def select_hits(
    hits: Iterable[Hit],
    *,
    max_chars: int,
    max_entries: int,
    book_budgets: Mapping[int, int],
) -> Selection:
    """按预算从命中集合里挑出这一轮真正注入的条目

    Args:
        hits: 命中的条目，顺序不重要（这里会重排）。
        max_chars: 全局每轮正文字数上限。
        max_entries: 全局每轮条数上限。
        book_budgets: 书 ID 到「这本书每轮最多多少字」；缺项或 0 表示不单独限制。

    顺序上先判条数、再判字数：条数上限是硬上限（32 条），触发时该记 `max_entries`，
    而不是被算成「字数不够」。三种跳过原因要分开记，否则明细页只能显示一句「被预算
    截掉」，管理员没法判断是该调全局、调这本书、还是条目太多。
    """
    ordered = sorted(hits, key=_selection_key)
    selected: list[Hit] = []
    dropped: list[tuple[Hit, str]] = []
    used_chars = 0
    used_by_book: dict[int, int] = {}

    for hit in ordered:
        if len(selected) >= max_entries:
            dropped.append((hit, "max_entries"))
            continue
        length = len(hit.entry.content)
        if used_chars + length > max_chars:
            dropped.append((hit, "global_chars"))
            continue
        budget = book_budgets.get(hit.entry.book_id, 0)
        if budget > 0:
            used_in_book = used_by_book.get(hit.entry.book_id, 0)
            if used_in_book + length > budget:
                dropped.append((hit, "book_chars"))
                continue
        selected.append(hit)
        used_chars += length
        used_by_book[hit.entry.book_id] = used_by_book.get(hit.entry.book_id, 0) + length

    return Selection(selected=tuple(selected), dropped=tuple(dropped), total_chars=used_chars)


@dataclass
class RenderGroups:
    """按注入位置分好组的条目

    `at_depth` 用字典按深度分：同一深度的多条要合成一块文字插在同一个位置，分成两个
    键会让它们散在聊天记录的两处。
    """

    after_persona: list[Hit] = field(default_factory=list)
    before_history: list[Hit] = field(default_factory=list)
    at_depth: dict[int, list[Hit]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        """一个位置都没有时调用方可以整段跳过，提示词逐字不变"""
        return not self.after_persona and not self.before_history and not self.at_depth

    def ordered_depths(self) -> list[int]:
        """深度从大到小：深度大表示插得早（离末尾远），渲染时按这个顺序拼接"""
        return sorted(self.at_depth, reverse=True)


def group_for_render(selected: Sequence[Hit]) -> RenderGroups:
    """把入选条目按位置分组，组内按渲染顺序排好

    只在 `selected` 内部排，不看 `dropped`：被预算跳过的条目本来就不该出现在提示词里。
    """
    groups = RenderGroups()
    for hit in sorted(selected, key=_render_key):
        position = hit.entry.position
        if position is Position.AFTER_PERSONA:
            groups.after_persona.append(hit)
        elif position is Position.BEFORE_HISTORY:
            groups.before_history.append(hit)
        else:
            groups.at_depth.setdefault(hit.entry.depth, []).append(hit)
    return groups
