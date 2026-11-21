"""入选排序与预算截断、按位置的渲染分组

排序规则只有一处，且两个方向都要：

- **入选顺序**按 `order` 从大到小：`order` 大的更重要，先占预算。同值按条目 `id` 从
  小到大，保证同一批数据每次选出的是同一批（没有这一条，`order` 相同的条目会随数据库
  返回顺序漂移，同一轮两次渲染可能注入不同的条目）。
- **渲染顺序**按 `order` 从小到大：`order` 大的离聊天记录末尾更近，与酒馆「顺序越大
  越靠后」一致。

截断是「整条跳过」而不是「把正文切一半」：切一半的条目会给模型一段没有结尾的设定，
比不注入更糟，而且管理员永远不知道被切在哪里。因此一条放不下就整条跳过、继续试后面的，
并记下原因（全局预算、书预算、条数上限），供明细页解释「为什么这条没进来」。

预算按 token 计（0.4.7 起；口径见 `budget` 模块）。数法由调用方以 `count` 传进来：引擎
不认模型组，也不该自己去找系数；纯函数用例可以传任何数法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence, Union

from .matcher import HitReason
from .types import MatchEntry, Position


@dataclass(frozen=True)
class Hit:
    """一条命中的条目及其原因

    `from_variables`：这一轮模板把变量值（来自聊天的状态）打进了正文，因此按不可信内容注入；
    渲染时据此换一个说得准的标题。
    """

    entry: MatchEntry
    reason: HitReason
    from_variables: bool = False


@dataclass(frozen=True)
class Selection:
    """入选结果

    Attributes:
        selected: 入选的条目，按入选顺序（`order` 大到小）。
        dropped: 被预算跳过的条目与原因 `global_tokens` / `book_tokens` / `max_entries`
            （条数上限之后、缓存里已知要分流的写成 `max_entries|原因`）。
        total_tokens: 入选正文的 token 总数。
        tokens: 条目 ID 到它的 token 数（入选的与因预算跳过的都有；因条数跳过的、被 `prepare` 判为
            不注入的没数）。数的是 `prepare` 之后的正文。
        routed_out: `prepare` 判为不注入的条目与原因（用途、模板，见 `purpose` 模块）；
            它们不占条数与预算。
        ignored_budget: 靠「忽略预算」进来的条目 ID（没有这个开关就会被预算挡掉的不一定在内，
            凡是按豁免路径入选的都在）。
    """

    selected: tuple[Hit, ...] = ()
    dropped: tuple[tuple[Hit, str], ...] = ()
    total_tokens: int = 0
    tokens: Mapping[int, int] = field(default_factory=dict)
    routed_out: tuple[tuple[Hit, str], ...] = ()
    ignored_budget: frozenset[int] = frozenset()


def _selection_key(hit: Hit) -> tuple[int, int]:
    """入选顺序：`order` 大到小，同值按 `id` 小到大"""
    return (-hit.entry.order, hit.entry.id)


def _render_key(hit: Hit) -> tuple[int, int]:
    """渲染顺序：`order` 小到大，同值按 `id` 小到大"""
    return (hit.entry.order, hit.entry.id)


def select_hits(
    hits: Iterable[Hit],
    *,
    max_tokens: int,
    max_entries: int,
    book_budgets: Mapping[int, int],
    count: Callable[[MatchEntry], int],
    prepare: Callable[[Hit], Union[Hit, str]] | None = None,
    known_route: Callable[[Hit], str | None] | None = None,
    ignore_budget_cap: int = 0,
) -> Selection:
    """按预算从命中集合里挑出这一轮真正注入的条目

    Args:
        hits: 命中的条目，顺序不重要（这里会重排）。
        max_tokens: 全局每轮 token 预算。
        max_entries: 全局每轮条数上限。
        book_budgets: 书 ID 到「这本书每轮最多多少 token」；缺项或 0 表示不单独限制。
        count: 一条条目正文的 token 数（调用方按这一轮的系数数）；数的是 `prepare` 之后的条目。
        prepare: 进预算之前按入选顺序逐条调用：返回（可能换了正文的）命中，或者一个
            「不注入」的原因。放在这里而不是命中之后统一做，是为了让条数上限之后的条目
            根本不必分析——模板的编译与求值有成本，每轮最多只入选几十条。
        known_route: 不做分析、只查缓存的「这条已知要分流吗」。条数上限之后的条目不分析，
            但已知要分流的，原因与 `max_entries` 一起记（`max_entries|state_only` 这样用 `|`
            连起来），明细里两个都看得到。
        ignore_budget_cap: 「忽略预算」条目这一轮合计最多多少 token（`budget.ignore_budget_cap`）；
            0 表示这一轮不豁免任何条目。

    顺序上先判条数、再判预算：条数上限是硬上限（32 条），触发时该记 `max_entries`，
    而不是被算成「预算不够」。三种跳过原因要分开记，否则明细页只能显示一句「被预算
    截掉」，管理员没法判断是该调全局、调这本书、还是条目太多。

    **忽略预算**（酒馆的 `ignoreBudget`）：宿主条目（`trusted`）上打了这个标记的，命中后不受
    全局与书预算拦，仍受条数上限；它的 token **照样计入已用量**（全局与这本书的），后面普通
    条目的余量相应变少，与酒馆一致。租户条目上的标记不起作用，否则租户能绕过平台方给的预算
    上限。豁免有兜底：这类条目合计超过 `ignore_budget_cap` 时，超出的那几条按普通条目判。
    用它是为了把「一定要进」和「排在哪」拆开：`order` 同时决定渲染位置，为了让一条必进而把
    `order` 调到最高，会把它挪出原本夹着它的前后标记条目。
    """
    ordered = sorted(hits, key=_selection_key)
    selected: list[Hit] = []
    dropped: list[tuple[Hit, str]] = []
    routed_out: list[tuple[Hit, str]] = []
    counted: dict[int, int] = {}
    used_tokens = 0
    used_by_book: dict[int, int] = {}
    used_ignoring = 0
    ignored: set[int] = set()

    for hit in ordered:
        if len(selected) >= max_entries:
            routed = known_route(hit) if known_route is not None else None
            dropped.append((hit, f"max_entries|{routed}" if routed else "max_entries"))
            continue
        if prepare is not None:
            prepared = prepare(hit)
            if isinstance(prepared, str):
                routed_out.append((hit, prepared))
                continue
            hit = prepared
        size = count(hit.entry)
        counted[hit.entry.id] = size
        exempt = hit.entry.ignore_budget and hit.entry.trusted and used_ignoring + size <= ignore_budget_cap
        if exempt:
            used_ignoring += size
            ignored.add(hit.entry.id)
        else:
            if used_tokens + size > max_tokens:
                dropped.append((hit, "global_tokens"))
                continue
            budget = book_budgets.get(hit.entry.book_id, 0)
            if budget > 0:
                used_in_book = used_by_book.get(hit.entry.book_id, 0)
                if used_in_book + size > budget:
                    dropped.append((hit, "book_tokens"))
                    continue
        selected.append(hit)
        used_tokens += size
        used_by_book[hit.entry.book_id] = used_by_book.get(hit.entry.book_id, 0) + size

    return Selection(
        selected=tuple(selected),
        dropped=tuple(dropped),
        total_tokens=used_tokens,
        tokens=counted,
        routed_out=tuple(routed_out),
        ignored_budget=frozenset(ignored),
    )


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
