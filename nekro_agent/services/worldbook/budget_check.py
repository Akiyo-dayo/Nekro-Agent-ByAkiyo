"""书的预算体检：常驻条目合计多少 token、只有常驻条目命中时能进几条、哪几条进不来

装法是「跳过式」：一条放不下就跳过、继续试后面的。这让「这条在这个预算下进不进得来」很难靠猜——
预算调大一点，可能是另一条先进来、把它挤掉。所以这里不另写一套，直接拿书里启用的常驻条目当作「这一轮
全都命中了」交给 :func:`selection.select_hits`，结果就是「只有常驻条目命中时」真正注入的那几条。

被跳过的常驻条目**不一定永远进不来**：跳过式对预算不单调，关键词条目命中、把排在前面的常驻条目挤掉
之后，后面一条小的反而可能放得下。只有单条就比预算还大的（`alone_over`）才是真的永远进不来。

- 按用途不进主提示词的常驻条目（变量更新规则、初值这类，见 `purpose`）不占预算，单独计数；
- 被跳过的条目按 `select_hits` 的原因记（全局预算、这本书的预算、条数上限）；
- 只读、不查库：调用方把条目行（或导入时映射出的列）与书传进来。

世界书详情、导入报告（世界书文件与卡内书）都调 :func:`check_constants`；触发测试页的「被预算跳过」
本来就是 `select_hits` 的结果，那边只补「在它前面占掉预算的是哪几条」。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional

from .budget import WorldbookBudget, ignore_budget_cap
from .matcher import HitReason
from .selection import Hit, select_hits
from .types import EntryStatus, book_to_match, entry_to_match

# 报告里每一类最多点名几条、标题显示多少字。
MAX_LISTED = 10
TITLE_CHARS = 20


@dataclass(frozen=True)
class DroppedConstant:
    """一条在「只有常驻条目命中」时被跳过的常驻条目；`alone_over` 为真的才是永远进不来

    Attributes:
        entry_id: 条目 ID（导入时没有库 ID，用序号）。
        uid: 书内编号。
        title: 标题。
        tokens: 正文 token 数。
        reason: `global_tokens` / `book_tokens` / `max_entries`。
        alone_over: 单条就比每轮预算（或这本书的预算）还大。
    """

    entry_id: int
    uid: int
    title: str
    tokens: int
    reason: str
    alone_over: bool


@dataclass(frozen=True)
class BudgetCheck:
    """一本书的预算体检结果"""

    budget: int
    book_budget: int
    max_entries: int
    constant_count: int
    constant_tokens: int
    fit_count: int
    fit_tokens: int
    routed_out: int
    dropped: tuple[DroppedConstant, ...]

    def summary(self) -> str:
        """一句话：常驻合计、只有常驻条目命中时能进几条；单条就超预算（永远进不来）的与其余被跳过的分开点名

        句子以「启用的常驻…」开头，调用方按需在前面加「这本书」「卡内知识书」。
        """
        if not self.constant_count:
            return "没有启用的常驻条目"
        text = f"启用的常驻条目 {self.constant_count} 条，合计约 {self.constant_tokens} token"
        if self.routed_out:
            text += f"（另有 {self.routed_out} 条按用途不进主提示词，不占预算）"
        limit = f"每轮 {self.budget} token"
        if self.book_budget:
            limit += f"、这本书最多 {self.book_budget} token"
        text += f"；按{limit}、最多 {self.max_entries} 条，只有常驻条目命中时能进 {self.fit_count} 条"
        if not self.dropped:
            return text + "，全部能进"
        alone = [item for item in self.dropped if item.alone_over]
        others = [item for item in self.dropped if not item.alone_over]
        if alone:
            text += f"；单条就超预算、永远进不来的 {len(alone)} 条：{_listed(alone)}"
        if others:
            text += f"；只有常驻条目命中时进不来的 {len(others)} 条（关键词条目命中后可能有变化）：{_listed(others)}"
        return text


def _listed(items: list[DroppedConstant]) -> str:
    listed = "、".join(f"「{item.title or '未命名'}」约 {item.tokens} token" for item in items[:MAX_LISTED])
    return listed + (f" 等 {len(items)} 条" if len(items) > MAX_LISTED else "")


def _title(entry: Any) -> str:
    return str(getattr(entry, "comment", "") or "").strip()[:TITLE_CHARS]


def check_constants(
    entries: Iterable[Any],
    book: Any,
    *,
    budget: int,
    max_entries: int,
    count: Callable[[str], int],
    route: Optional[Callable[[Any], Optional[str]]] = None,
    exempt_cap: int = 0,
) -> BudgetCheck:
    """拿一本书里启用的常驻条目算一遍「只有常驻命中时」能进几条

    Args:
        entries: 条目行，或带同名属性的对象（导入时用映射出的列包一层）；停用、隔离、非常驻的跳过。
        book: 书行，或带 `id` / `scan_depth` / `budget_tokens` / `case_sensitive` / `match_whole_words` 等属性的对象。
        budget: 每轮世界书预算（token）。
        max_entries: 每轮条数上限。
        count: 一段正文的 token 数。
        route: 这条按用途不进主提示词的原因（不进就不占预算），`None` 表示照常。
        exempt_cap: 「忽略预算」条目合计最多多少 token（`budget.ignore_budget_cap`），与运行时同一个判据；
            0 表示不豁免。条目要带 `trusted` 与 `ignore_budget` 两个属性，豁免才会生效。
    """
    match_book = book_to_match(book)
    hits: list[Hit] = []
    titles: dict[int, tuple[int, str]] = {}
    routed = 0
    for entry in entries:
        if not bool(getattr(entry, "enabled", True)) or not bool(getattr(entry, "constant", False)):
            continue
        if (
            str(getattr(entry, "status", EntryStatus.ACTIVE.value) or EntryStatus.ACTIVE.value)
            != EntryStatus.ACTIVE.value
        ):
            continue
        if route is not None and route(entry) is not None:
            routed += 1
            continue
        match = entry_to_match(entry, match_book)
        hits.append(Hit(entry=match, reason=HitReason(kind="constant")))
        uid = getattr(entry, "uid", 0)
        titles[match.id] = (uid if isinstance(uid, int) and not isinstance(uid, bool) else 0, _title(entry))

    sizes = {hit.entry.id: count(hit.entry.content) for hit in hits}
    book_budget = max(0, int(getattr(book, "budget_tokens", 0) or 0))
    chosen = select_hits(
        hits,
        max_tokens=budget,
        max_entries=max_entries,
        book_budgets={match_book.id: book_budget} if book_budget else {},
        count=lambda entry: sizes.get(entry.id, 0),
        ignore_budget_cap=exempt_cap,
    )
    limit = min(budget, book_budget) if book_budget else budget
    dropped = tuple(
        DroppedConstant(
            entry_id=hit.entry.id,
            uid=titles.get(hit.entry.id, (0, ""))[0],
            title=titles.get(hit.entry.id, (0, ""))[1],
            tokens=sizes.get(hit.entry.id, 0),
            reason=reason.split("|", 1)[0],
            alone_over=sizes.get(hit.entry.id, 0) > limit,
        )
        for hit, reason in chosen.dropped
    )
    return BudgetCheck(
        budget=budget,
        book_budget=book_budget,
        max_entries=max_entries,
        constant_count=len(hits),
        constant_tokens=sum(sizes.values()),
        fit_count=len(chosen.selected),
        fit_tokens=chosen.total_tokens,
        routed_out=routed,
        dropped=dropped,
    )


def columns_entries(columns_list: Iterable[tuple[str, dict[str, Any]]]) -> list[SimpleNamespace]:
    """导入时映射出的列包成带属性的对象，ID 用序号（还没进库），`entry_key` 是酒馆的 `uid` 原文"""
    wrapped: list[SimpleNamespace] = []
    for index, (entry_key, columns) in enumerate(columns_list, start=1):
        attributes = {**columns, "id": index, "book_id": 0, "status": EntryStatus.ACTIVE.value, "entry_key": entry_key}
        wrapped.append(SimpleNamespace(**attributes))
    return wrapped


# 导入报告里按用途或模板判为不进主提示词的那几类（`purpose.template_import_notes` 的类别）。
ROUTED_IMPORT_KINDS = frozenset({"state_only", "unsupported_template", "suspected_trap"})


def check_import(
    columns_list: Iterable[tuple[str, dict[str, Any]]],
    *,
    budget: Optional[WorldbookBudget],
    max_entries: int,
    routed_keys: Iterable[str] = (),
    book_budget: int = 0,
    trusted: bool = False,
) -> Optional[BudgetCheck]:
    """导入时的预算体检：按导入目标这一层的预算算；预算读不到（`None`）就不算

    `routed_keys` 是导入分析判为不进主提示词的条目（`uid` 原文）。书还没建，书级预算由调用方给
    （新建书时表单里的值；卡内书是 0）。`trusted` 是这批条目写进库时会不会按宿主内容注入（与
    `_write_entries` 同一个判据）：「忽略预算」只对宿主条目生效，体检要与运行时一致。
    """
    if budget is None:
        return None
    routed = set(routed_keys)
    entries = columns_entries(columns_list)
    for entry in entries:
        entry.trusted = trusted
    book = SimpleNamespace(id=0, budget_tokens=book_budget)
    return check_constants(
        entries,
        book,
        budget=budget.budget,
        max_entries=max_entries,
        count=budget.count,
        route=lambda entry: "routed" if str(getattr(entry, "entry_key", "")) in routed else None,
        exempt_cap=ignore_budget_cap(budget),
    )


__all__ = [
    "MAX_LISTED",
    "ROUTED_IMPORT_KINDS",
    "BudgetCheck",
    "DroppedConstant",
    "check_constants",
    "check_import",
    "columns_entries",
]
