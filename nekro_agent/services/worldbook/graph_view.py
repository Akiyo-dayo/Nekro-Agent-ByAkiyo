"""世界书图谱：书、条目、绑定对象，以及条目之间的关键词引用

图的取数范围与 `GET /worldbook/books` 完全同一套：`_resolve_book_scope` 产出的
`AdminMemoryScope` 决定看得见哪些实例，租户再并入「平台方给全局人设建、人设对本实例
可见」的只读书。本模块不判权限，只把路由算好的范围与那个已校验过的 `book` 拼成图。

三件事没有做，写在这里免得到处找：

- **语义近邻边**：需要逐点取向量再检索（`vectors.py` 只存条目自身的向量，没有
  `recommend` 用法），一个列表接口里做不起。这一版只有关键词引用。
- **「永远进不来」的预算体检**：体检只覆盖常驻条目，而且每租户同时只能算一本，放不进
  一个列表接口。图上用 `starved`（最近明细里 `dropped > 0 and injected == 0`）代替：
  它是真实发生过的「只被预算挤掉、一次都没进去」，不是体检推算。
- **按书聚合注入次数下推 SQL**：`worldbook_items` 是 JSON 列，PostgreSQL 与 SQLite 的
  数组查询语法不同；这里与提示词明细的其它读法一致，取最近若干行在 Python 里逐行数。

节点 ID 与边端点的约束由 :func:`build_worldbook_graph` 自己保证：条目被 `limit` 截掉
时，连到它的边一起不画；关键词引用只在进入图的条目之间算。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import reduce
from operator import or_
from typing import Any, Optional

from tortoise import Tortoise
from tortoise.expressions import Q
from tortoise.functions import Count

from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.services.memory.instance_scope import AdminFilter, AdminMemoryScope
from nekro_agent.services.user.tenant_perm import get_tenant_scope
from nekro_agent.services.worldbook import service as wb_service
from nekro_agent.services.worldbook.matcher import _normalize
from nekro_agent.services.worldbook.safe_regex import parse_regex_key
from nekro_agent.services.worldbook.service import _like_pattern

# 条目节点的上下限；与记忆图同名同值，前端不传时用默认值
GRAPH_LIMIT_MIN = 50
GRAPH_LIMIT_MAX = 600
GRAPH_LIMIT_DEFAULT = 240
# 最多给多少个人节点查昵称（逐个查库）
PERSON_NAME_LOOKUPS = 40
# 明细里「没进去」的原因中，属于预算 / 条数上限的那几种
_BUDGET_DROP_REASONS = frozenset({"global_tokens", "book_tokens", "max_entries", "global_chars", "book_chars"})

# 书与条目的扫描上限。书的 200 本决定「画哪些」，条目的 5000 条决定「在哪些条目里挑」。
BOOK_SCAN_LIMIT = 200
ENTRY_SCAN_LIMIT = 5000
ROUND_SCAN_LIMIT = 3000

# 关键词引用：只用纯文本关键词、每条最多 8 个、正文截 4000 字、边最多 600 条、整体 1.5 秒
MAX_REFERENCE_KEYS = 8
REFERENCE_CONTENT_LIMIT = 4000
REFERENCE_EDGE_LIMIT = 600
REFERENCE_TIMEOUT_SECONDS = 1.5

# 条目正文预览长度（换行压成空格之后）
PREVIEW_CHARS = 300

# 只给界面看的条目列：正文要用来算预览与关键词引用，其余的注入参数不取
_ENTRY_COLUMNS = (
    "id",
    "book_id",
    "uid",
    "comment",
    "keys",
    "content",
    "order",
    "constant",
    "vectorized",
    "ignore_budget",
    "trusted",
    "position",
    "enabled",
    "status",
    "use_regex",
)

_SCOPE_LABELS = {
    "domain": "域",
    "preset": "人设",
    "channel": "频道",
    "person": "个人",
}


@dataclass
class InjectionStats:
    """一个条目在最近若干轮提示词明细里的三种出现次数"""

    injected: int = 0
    dropped: int = 0
    lookups: int = 0


@dataclass
class WorldbookGraph:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    books_total: int = 0
    books_drawn: int = 0
    entries_total: int = 0
    entries_drawn: int = 0
    rounds_scanned: int = 0
    books_truncated: bool = False
    entries_truncated: bool = False
    references_truncated: bool = False


def _node(**values: Any) -> dict[str, Any]:
    values.setdefault("subtitle", "")
    values.setdefault("status", "active")
    values.setdefault("cognitive_type", None)
    values.setdefault("weight", 0.0)
    values.setdefault("size", 1.0)
    values.setdefault("importance", 0.0)
    values.setdefault("paragraph_id", None)
    values.setdefault("metadata", {})
    return values


def _edge(**values: Any) -> dict[str, Any]:
    values.setdefault("label", "")
    values.setdefault("weight", 0.0)
    values.setdefault("strength", 0.0)
    values.setdefault("status", "active")
    values.setdefault("cognitive_type", None)
    values.setdefault("metadata", {})
    return values


async def _load_books(
    scope: AdminMemoryScope,
    *,
    readable_for: Optional[tuple[DBUser, str]],
    book: Optional[DBWbBook],
) -> tuple[list[DBWbBook], int, bool]:
    """作用域内的书；`book` 给定时只画这一本（调用方已经用 `_load_readable_book` 判过）

    列表走 `wb_service.list_books`，与书列表同一套范围与排序；它的每页上限是 100，所以
    最多取两页。超过 200 本时按更新时间留最新的 200 本并标 `books_truncated`。
    """
    if book is not None:
        return [book], 1, False

    total, first = await wb_service.list_books(
        scope,
        AdminFilter(),
        page=1,
        page_size=100,
        sort="update_time",
        order="desc",
        readable_for=readable_for,
    )
    rows = list(first)
    if total > 100:
        _, second = await wb_service.list_books(
            scope,
            AdminFilter(),
            page=2,
            page_size=100,
            sort="update_time",
            order="desc",
            readable_for=readable_for,
        )
        rows.extend(second)
    return rows[:BOOK_SCAN_LIMIT], total, total > BOOK_SCAN_LIMIT


async def _book_entry_counts(book_ids: Sequence[int]) -> dict[int, tuple[int, int, int]]:
    """每本书的（条目数、常驻数、隔离数），数的是这本书的全部条目

    不按当前搜索词或 `include_inactive` 过滤：书面板上的「条目 n、常驻 n、隔离 n」是
    这本书的家底，搜索只影响图上画哪些条目。
    """
    if not book_ids:
        return {}
    rows = (
        await DBWbEntry.filter(book_id__in=list(book_ids))
        .group_by("book_id")
        .annotate(
            total=Count("id"),
            constants=Count("id", _filter=Q(constant=True)),
            quarantined=Count("id", _filter=Q(status="quarantined")),
        )
        .values("book_id", "total", "constants", "quarantined")
    )
    counts: dict[int, tuple[int, int, int]] = {}
    for row in rows:
        counts[int(row["book_id"])] = (
            int(row["total"]),
            int(row["constants"]),
            int(row["quarantined"]),
        )
    return counts


async def _search_entry_ids(
    book_ids: Sequence[int],
    needle: str,
    *,
    active_only: bool,
    limit: int,
) -> tuple[list[int], int]:
    """按条目标题 / 正文 / 主次关键词查一批书里的条目 ID 与总数

    与 `service._search_entry_ids` 同一写法（关键词数组用数据库的 JSON 函数逐个元素比，
    不拿整列 JSON 当文本），只是把「一本书」换成「一批书」。返回的 ID 按 `order` 降序、
    `id` 升序取前 `limit` 条，`COUNT(*) OVER ()` 给的是过滤后的总数。
    """
    conn = Tortoise.get_connection("default")
    dialect = conn.capabilities.dialect
    pattern = _like_pattern(needle)

    if dialect == "postgres":
        slots = ", ".join(f"${index + 1}" for index in range(len(book_ids)))
        param = len(book_ids) + 1
        text_match = f'"comment" ILIKE ${param} ESCAPE \'\\\' OR "content" ILIKE ${param} ESCAPE \'\\\''
        key_match = (
            f'CASE WHEN jsonb_typeof("{{col}}") = \'array\' THEN EXISTS ('
            f'SELECT 1 FROM jsonb_array_elements_text("{{col}}") AS k(v) '
            f'WHERE k.v ILIKE ${param} ESCAPE \'\\\') ELSE FALSE END'
        )
        limit_slot = f"${param + 1}"
    elif dialect == "sqlite":
        slots = ", ".join(f"?{index + 1}" for index in range(len(book_ids)))
        param = len(book_ids) + 1
        text_match = f'"comment" LIKE ?{param} ESCAPE \'\\\' OR "content" LIKE ?{param} ESCAPE \'\\\''
        key_match = (
            f'CASE WHEN json_valid("{{col}}") THEN CASE WHEN json_type("{{col}}") = \'array\' THEN EXISTS ('
            f'SELECT 1 FROM json_each("{{col}}") WHERE json_each.value LIKE ?{param} ESCAPE \'\\\') ELSE 0 END '
            f"ELSE 0 END"
        )
        limit_slot = f"?{param + 1}"
    else:
        # 只支持上面两种库；别的库退回标题与正文，关键词不参与（字典序与 JSON 函数都没有
        # 可移植写法）。项目实际只跑 PostgreSQL 与 SQLite，这一支只为不让接口 500。
        query = DBWbEntry.filter(book_id__in=list(book_ids)).filter(
            Q(comment__icontains=needle) | Q(content__icontains=needle),
        )
        if active_only:
            query = query.filter(enabled=True, status="active")
        total = await query.count()
        ids = await query.order_by("-order", "id").limit(limit).values_list("id", flat=True)
        return [int(entry_id) for entry_id in ids], total

    where = f'"book_id" IN ({slots}) AND ({text_match} OR {key_match.format(col="keys")} OR {key_match.format(col="secondary_keys")})'
    if active_only:
        where += ' AND "enabled" = TRUE AND "status" = \'active\''
    rows = await conn.execute_query_dict(
        f'SELECT "id", COUNT(*) OVER () AS "n" FROM "wb_entry" WHERE {where} ORDER BY "order" DESC, "id" ASC LIMIT {limit_slot}',
        [*book_ids, pattern, limit],
    )
    if not rows:
        return [], 0
    return [int(row["id"]) for row in rows], int(rows[0]["n"])


async def _load_entries(
    book_ids: Sequence[int],
    *,
    needle: str,
    include_inactive: bool,
    limit: int = ENTRY_SCAN_LIMIT,
) -> tuple[list[DBWbEntry], int]:
    """这些书里要参与排名的条目，以及当前条件下的总数

    默认只要启用且未隔离的条目；`include_inactive` 时全要。总数可能大于返回的条数：
    后者是撞上 5000 条扫描上限的那一批。
    """
    if not book_ids:
        return [], 0

    if needle:
        ids, total = await _search_entry_ids(
            book_ids,
            needle,
            active_only=not include_inactive,
            limit=limit,
        )
        if not ids:
            return [], total
        # 搜索最多返回 5000 个 ID；SQLite 的变量上限是 999，分块取，别让一条 IN 撑爆
        rows: list[DBWbEntry] = []
        for start in range(0, len(ids), 500):
            rows.extend(await DBWbEntry.filter(id__in=ids[start : start + 500]).only(*_ENTRY_COLUMNS))
        return rows, total

    query = DBWbEntry.filter(book_id__in=list(book_ids))
    if not include_inactive:
        query = query.filter(enabled=True, status="active")
    total = await query.count()
    rows = await query.order_by("-order", "id").only(*_ENTRY_COLUMNS).limit(limit)
    return list(rows), total


async def _load_injection_stats(scope: AdminMemoryScope) -> tuple[dict[int, InjectionStats], int]:
    """最近若干轮提示词明细里每个条目的注入 / 被预算挤掉 / 被查询次数

    `scope.pairs is None` 是平台方全量，不加实例条件；否则按可见实例对过滤。空元组不是
    「全量」：它表示一个实例都不可见，直接不取行。
    """
    pairs = scope.pairs
    if pairs is not None and not pairs:
        return {}, 0

    query = DBMemPromptRound.all()
    if pairs is not None:
        query = query.filter(reduce(or_, [Q(adapter_key=adapter, instance_key=instance) for adapter, instance in pairs]))
    rows = await query.order_by("-id").limit(ROUND_SCAN_LIMIT).values_list("worldbook_items", flat=True)

    stats: dict[int, InjectionStats] = {}
    for items in rows:
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            entry_id = item.get("id")
            if not isinstance(entry_id, int) or isinstance(entry_id, bool):
                continue
            current = stats.setdefault(entry_id, InjectionStats())
            if kind == "worldbook":
                current.injected += 1
            elif kind == "worldbook_dropped":
                # 只数因为预算 / 条数上限没进去的：按用途分流、模板算空这些原因也记在同一类明细里，
                # 它们本来就不该进主提示词，算进来会把「状态专用」条目误标成被挤掉
                if item.get("reason") in _BUDGET_DROP_REASONS:
                    current.dropped += 1
            elif kind == "worldbook_lookup":
                current.lookups += 1
    return stats, len(rows)


def _preview(content: str) -> str:
    """正文前 300 字；换行压成空格，避免画布上的副标题撑开"""
    return content.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")[:PREVIEW_CHARS]


def _entry_label(entry: DBWbEntry) -> str:
    """条目标题：备注 → 第一个关键词 → `#uid`"""
    comment = (entry.comment or "").strip()
    if comment:
        return comment
    for key in entry.keys or []:
        if isinstance(key, str) and key.strip():
            return key.strip()
    return f"#{entry.uid}"


def _entry_keys(entry: DBWbEntry) -> list[str]:
    """图上展示的关键词，最多 8 个"""
    keys = [key.strip() for key in (entry.keys or []) if isinstance(key, str) and key.strip()]
    return keys[:8]


def _reference_edges(
    entries: Sequence[DBWbEntry],
    *,
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], bool]:
    """在进入图的条目之间算关键词引用（纯函数，在线程里跑）

    B 的正文里出现了 A 的关键词 → B → A。只用纯文本关键词：`use_regex` 的条目整条跳过，
    `/…/flags` 形态的关键词跳过，次关键词不算；关键词去空白后短于 2 个字符的跳过，每条
    最多取前 8 个。正文截 4000 字，按 `matcher._normalize`（大小写不敏感）做子串判断。
    同一对 (B, A) 只留一条边，取第一个命中的关键词；边总数最多 600 条。
    """
    if timeout_seconds <= 0:
        return [], True

    prepared: list[tuple[int, str, list[tuple[str, str]]]] = []
    for entry in entries:
        content = _normalize((entry.content or "")[:REFERENCE_CONTENT_LIMIT], False)
        keys: list[tuple[str, str]] = []
        if not entry.use_regex:
            seen: set[str] = set()
            for raw in (entry.keys or [])[:MAX_REFERENCE_KEYS]:
                if not isinstance(raw, str):
                    continue
                label = raw.strip()
                if len(label) < 2:
                    continue
                if parse_regex_key(label, use_regex=False) is not None:
                    continue
                normalized = _normalize(label, False)
                if len(normalized) < 2 or normalized in seen:
                    continue
                seen.add(normalized)
                keys.append((label, normalized))
        prepared.append((int(entry.id), content, keys))

    deadline = time.monotonic() + timeout_seconds
    edges: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, int]] = set()
    checked = 0
    for source_id, content, _own_keys in prepared:
        for target_id, _target_content, target_keys in prepared:
            if source_id == target_id:
                continue
            pair = (source_id, target_id)
            if pair in seen_pairs:
                continue
            checked += 1
            if checked % 64 == 0 and time.monotonic() >= deadline:
                return edges, True
            for label, normalized in target_keys:
                if normalized in content:
                    seen_pairs.add(pair)
                    edges.append(
                        _edge(
                            id=f"entry-reference-{source_id}-{target_id}",
                            source=f"entry-{source_id}",
                            target=f"entry-{target_id}",
                            edge_type="key_reference",
                            label=label,
                            weight=1.0,
                            strength=1.0,
                            metadata={"derived": True},
                        ),
                    )
                    break
            if len(edges) >= REFERENCE_EDGE_LIMIT:
                return edges, True
    if time.monotonic() >= deadline:
        return edges, True
    return edges, False


def _book_binding_spec(book: DBWbBook, labels: tuple[str, str, bool]) -> Optional[tuple[str, str, int, dict[str, Any]]]:
    """一本书的绑定对象节点：`(节点 ID, 名字, ref_id, 额外元数据)`；域书没有绑定对象"""
    preset_name, channel_name, _missing = labels
    if book.scope_kind == "preset":
        if book.preset_id <= 0:
            return None
        return (
            f"preset-{book.preset_id}",
            preset_name or f"#{book.preset_id}",
            int(book.preset_id),
            {"preset_id": int(book.preset_id)},
        )
    if book.scope_kind == "channel":
        if not book.chat_key:
            return None
        return (
            f"channel-{book.chat_key}",
            channel_name or book.chat_key,
            0,
            {"chat_key": book.chat_key},
        )
    if book.scope_kind == "person":
        account = (book.person_account or "").strip()
        if not account:
            return None
        return (
            f"person-{account}",
            account,
            0,
            {"account": account},
        )
    return None


async def build_worldbook_graph(
    user: DBUser,
    scope: AdminMemoryScope,
    *,
    instance_key: str = "",
    book: Optional[DBWbBook] = None,
    q: str = "",
    include_inactive: bool = False,
    limit: int = GRAPH_LIMIT_DEFAULT,
) -> WorldbookGraph:
    """按范围与筛选组装世界书图

    Args:
        user: 当前身份，只用来判「租户要不要并入平台方只读书」与取书列表的只读标记。
        scope: 路由算好的可见范围。
        instance_key: 请求选中的实例；租户并入平台方只读书时按它判人设可见性。
        book: 已由 `_load_readable_book` 校验过的单本；给了就只画这一本。
        q: 条目标题 / 关键词 / 正文包含。
        include_inactive: 是否带上停用与隔离的条目。
        limit: 条目节点上限，夹在 50 ~ 600。
    """
    limit = max(GRAPH_LIMIT_MIN, min(GRAPH_LIMIT_MAX, int(limit)))
    needle = (q or "").strip()
    is_tenant = get_tenant_scope(user) is not None
    readable_for = (user, instance_key) if is_tenant and instance_key else None

    books, books_total, books_truncated = await _load_books(
        scope,
        readable_for=readable_for,
        book=book,
    )
    read_only_ids = await wb_service.platform_books_readable_by(
        user,
        books,
        instance_key=instance_key or None,
    )
    labels = await wb_service.binding_labels(books)
    counts = await _book_entry_counts([item.id for item in books])

    entries, entries_total = await _load_entries(
        [item.id for item in books],
        needle=needle,
        include_inactive=include_inactive,
    )
    stats, rounds_scanned = await _load_injection_stats(scope)
    max_injected = max((stats.get(int(entry.id), InjectionStats()).injected for entry in entries), default=0)
    ranked = sorted(
        entries,
        key=lambda entry: (
            -stats.get(int(entry.id), InjectionStats()).injected,
            0 if entry.constant else 1,
            -int(entry.order),
            int(entry.id),
        ),
    )
    drawn = ranked[:limit]

    graph = WorldbookGraph(
        books_total=books_total,
        books_drawn=len(books),
        entries_total=entries_total,
        entries_drawn=len(drawn),
        rounds_scanned=rounds_scanned,
        books_truncated=books_truncated,
        entries_truncated=entries_total > ENTRY_SCAN_LIMIT,
    )
    book_names = {book_item.id: book_item.name for book_item in books}

    for book_item in books:
        entry_count, constant_count, quarantined_count = counts.get(book_item.id, (0, 0, 0))
        preset_name, channel_name, _missing = labels.get(book_item.id, ("", "", False))
        graph.nodes.append(
            _node(
                id=f"book-{book_item.id}",
                memory_type="book",
                ref_id=book_item.id,
                label=book_item.name,
                subtitle=_SCOPE_LABELS.get(book_item.scope_kind, book_item.scope_kind),
                status="active" if book_item.enabled else "inactive",
                weight=1.0,
                size=1.0,
                importance=1.0,
                metadata={
                    "scope_kind": book_item.scope_kind,
                    "entry_count": entry_count,
                    "constant_count": constant_count,
                    "quarantined_count": quarantined_count,
                    "inject_mode": book_item.inject_mode,
                    "active_scene": book_item.active_scene,
                    "vector_all": bool(book_item.vector_all),
                    "read_only": book_item.id in read_only_ids,
                    "instance_key": book_item.instance_key,
                    # 面板上要拼「人设 / 频道 / 个人」的绑定文字，省一次回表
                    "preset_id": book_item.preset_id,
                    "preset_name": preset_name,
                    "chat_key": book_item.chat_key,
                    "channel_name": channel_name,
                    "person_account": book_item.person_account,
                },
            ),
        )

    for entry in drawn:
        entry_stats = stats.get(int(entry.id), InjectionStats())
        importance = 0.15 + 0.85 * entry_stats.injected / max(1, max_injected)
        if entry.constant:
            importance += 0.1
        importance = min(1.0, importance)
        status = "quarantined" if entry.status == "quarantined" else ("active" if entry.enabled else "inactive")
        graph.nodes.append(
            _node(
                id=f"entry-{entry.id}",
                memory_type="entry",
                ref_id=int(entry.id),
                label=_entry_label(entry),
                subtitle=book_names.get(int(entry.book_id), ""),
                status=status,
                weight=float(entry_stats.injected),
                size=importance,
                importance=importance,
                metadata={
                    "book_id": int(entry.book_id),
                    "uid": int(entry.uid),
                    "keys": _entry_keys(entry),
                    "constant": bool(entry.constant),
                    "vectorized": bool(entry.vectorized),
                    "ignore_budget": bool(entry.ignore_budget),
                    "trusted": bool(entry.trusted),
                    "position": entry.position,
                    "injected": entry_stats.injected,
                    "dropped": entry_stats.dropped,
                    "lookups": entry_stats.lookups,
                    "starved": entry_stats.dropped > 0 and entry_stats.injected == 0,
                    "preview": _preview(entry.content or ""),
                },
            ),
        )

    # 绑定对象：同一个对象被几本书绑定只建一个节点，`metadata.book_ids` 列出这些书
    binding_books: dict[str, set[int]] = {}
    binding_nodes: dict[str, dict[str, Any]] = {}
    person_owners: dict[str, tuple[str, str]] = {}
    for book_item in books:
        spec = _book_binding_spec(book_item, labels.get(book_item.id, ("", "", False)))
        if spec is None:
            continue
        node_id, label, ref_id, extra = spec
        node = binding_nodes.get(node_id)
        if node is None:
            node = _node(
                id=node_id,
                memory_type=node_id.split("-", 1)[0],
                ref_id=ref_id,
                label=label,
                subtitle=_SCOPE_LABELS.get(book_item.scope_kind, book_item.scope_kind),
                status="active",
                weight=1.0,
                size=1.0,
                importance=0.4,
                metadata={**extra, "book_ids": []},
            )
            binding_nodes[node_id] = node
            if node["memory_type"] == "person":
                # 记下这个人是在哪个实例的书上绑的：下面按这个实例对取昵称
                person_owners[node_id] = (str(book_item.adapter_key or ""), str(book_item.instance_key or ""))
        binding_books.setdefault(node_id, set()).add(int(book_item.id))
        graph.edges.append(
            _edge(
                id=f"book-binding-{book_item.id}-{node_id}",
                source=f"book-{book_item.id}",
                target=node_id,
                edge_type="book_binding",
                label="绑定",
                weight=1.0,
                strength=1.0,
                metadata={},
            ),
        )
    # 人节点显示昵称而不是一串账号：按「绑它的那本书所在的实例」取这个账号最近一条消息的昵称
    # （与按人记一笔时给书起名用的是同一个函数）。绑到人的书通常只有几本，逐个查可以接受；
    # 真有很多时只查前 PERSON_NAME_LOOKUPS 个，其余照旧显示账号。取不到昵称时函数自己退回账号。
    for node_id, owner in list(person_owners.items())[:PERSON_NAME_LOOKUPS]:
        node = binding_nodes[node_id]
        account = str(node["metadata"].get("account") or "")
        try:
            name = await wb_service.person_display_name(owner, account)
        except Exception:  # noqa: BLE001 - 名字只是点缀，取不到就用账号
            name = ""
        if name and name != account:
            node["label"] = name
            node["subtitle"] = account
    for node_id, node in binding_nodes.items():
        node["metadata"]["book_ids"] = sorted(binding_books.get(node_id, set()))
        graph.nodes.append(node)

    for entry in drawn:
        graph.edges.append(
            _edge(
                id=f"book-entry-{entry.book_id}-{entry.id}",
                source=f"book-{entry.book_id}",
                target=f"entry-{entry.id}",
                edge_type="book_entry",
                label="包含",
                weight=1.0,
                strength=1.0,
                metadata={},
            ),
        )

    try:
        reference_edges, references_truncated = await asyncio.wait_for(
            asyncio.to_thread(_reference_edges, drawn, timeout_seconds=REFERENCE_TIMEOUT_SECONDS),
            timeout=REFERENCE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        reference_edges, references_truncated = [], True
    graph.edges.extend(reference_edges)
    graph.references_truncated = references_truncated
    return graph


__all__ = [
    "BOOK_SCAN_LIMIT",
    "ENTRY_SCAN_LIMIT",
    "GRAPH_LIMIT_DEFAULT",
    "GRAPH_LIMIT_MAX",
    "GRAPH_LIMIT_MIN",
    "REFERENCE_EDGE_LIMIT",
    "REFERENCE_TIMEOUT_SECONDS",
    "ROUND_SCAN_LIMIT",
    "WorldbookGraph",
    "build_worldbook_graph",
]
