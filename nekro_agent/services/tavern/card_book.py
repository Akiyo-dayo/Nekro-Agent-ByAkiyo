"""卡内知识书与 L4 世界书之间的双向转换

酒馆把人设卡里的 `character_book` 当作一本内嵌世界书，字段名与它自己的世界书条目
不完全相同（`keys` / `secondary_keys` / `insertion_order` / `enabled`…）。这里照酒馆
`convertCharacterBook` 与 `convertWorldInfoToCharacterBook` 逐字段移植，不另造一套
映射：两张表字段名一旦分叉，导入与导出就会各自漂移，而漂移出来的那一半通常只在
「导入再导出」时才被发现。

## 为什么 `use_regex` 不照抄

酒馆导出卡片时把所有条目的 `use_regex` 一律写成 `true`（它自己的注释是「ST keys are
always regex」），但它匹配普通关键词时并不按正则解释。照抄会让大量带 `?`、`(` 的
普通关键词在 NA 里变成无效正则。因此导入时这一列一律 `False`（与酒馆的实际行为一致：
只有 `/…/flags` 形态的关键词才按正则），原值留在 `ext.na_card_entry` 里，导出时还原。

## 原文存两处

条目 `ext` 是转换后的世界书形状（L4 的导出照常能用）再加 `ext.na_card_entry` 的卡内
原条目；书级 `ext.na_card_book` 是卡内书去掉 `entries` 的其余键。导出时以原文为底、
只覆盖改过的字段——与 L4 的往返规则同一思路，没编辑过的条目就能逐字还原。
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Optional, Sequence

from nekro_agent.core.config import config
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.worldbook import BookCreateRequest, ImportDetailItem, ImportReport
from nekro_agent.services.tavern.errors import CardFileRejected
from nekro_agent.services.worldbook import budget_check as wb_budget_check
from nekro_agent.services.worldbook import purpose as wb_purpose
from nekro_agent.services.worldbook import service as wb_service
from nekro_agent.services.worldbook import st_format
from nekro_agent.services.worldbook import tokens as wb_tokens
from nekro_agent.services.worldbook.budget import WorldbookBudget
from nekro_agent.services.worldbook.types import (
    SCAN_DEPTH_MAX,
    SCAN_DEPTH_MIN,
    EntryStatus,
    ScopeKind,
)

# 卡内书的扫描深度缺省值，与 L4 普通书的默认一致。
DEFAULT_BOOK_SCAN_DEPTH = 4
# 书名上限取自 `DBWbBook.name`；超长时截断而不是让整张卡导入失败。
MAX_BOOK_NAME_CHARS = 128
# 卡内书条目的 uid 缺省按序号；酒馆的 `id` 不是整数时也退回序号。
_INVALID = object()


def _is_number(value: Any) -> bool:
    """布尔不算数字：`True` 在 JSON 里常被写成 1，但它不是深度或概率"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fallback(value: Any, default: Any) -> Any:
    """照酒馆的 `??` / `||` 习惯取默认值：`None` 与缺失都算没有"""
    return default if value is None else value


def card_entry_to_world_entry(entry: Mapping[str, Any], index: int) -> dict[str, Any]:
    """把一条卡内条目转成酒馆世界书条目形状

    以 `st_format.ST_ENTRY_DEFAULTS` 为底，逐字段按 `convertCharacterBook` 的取值
    规则覆盖。`id` 缺省用序号；`extensions.case_sensitive` 缺省而条目顶层
    `case_sensitive` 是布尔时用顶层值（V2 规范里那个字段在顶层，酒馆的转换会漏掉它）。
    """
    raw_ext = entry.get("extensions")
    extensions = copy.deepcopy(raw_ext) if isinstance(raw_ext, dict) else {}

    raw_id = entry.get("id", _INVALID)
    uid: Any = index if raw_id is _INVALID or raw_id is None else raw_id

    position = extensions.get("position")
    if position is None:
        position = 0 if entry.get("position") == "before_char" else 1

    case_sensitive = extensions.get("case_sensitive")
    if case_sensitive is None:
        top_level_case = entry.get("case_sensitive")
        if isinstance(top_level_case, bool):
            case_sensitive = top_level_case

    comment = entry.get("comment") or ""
    world: dict[str, Any] = copy.deepcopy(st_format.ST_ENTRY_DEFAULTS)
    world.update(
        {
            "uid": uid,
            "key": entry.get("keys") if entry.get("keys") is not None else [],
            "keysecondary": entry.get("secondary_keys") or [],
            "comment": comment,
            "content": entry.get("content") if entry.get("content") is not None else "",
            "constant": bool(entry.get("constant") or False),
            "selective": bool(entry.get("selective") or False),
            "order": _fallback(entry.get("insertion_order"), st_format.ST_ENTRY_DEFAULTS["order"]),
            "position": position,
            "excludeRecursion": _fallback(extensions.get("exclude_recursion"), False),
            "preventRecursion": _fallback(extensions.get("prevent_recursion"), False),
            "delayUntilRecursion": _fallback(extensions.get("delay_until_recursion"), False),
            "disable": not bool(entry.get("enabled")),
            "addMemo": bool(comment),
            "displayIndex": _fallback(extensions.get("display_index"), index),
            "probability": _fallback(extensions.get("probability"), 100),
            "useProbability": _fallback(extensions.get("useProbability"), True),
            "depth": _fallback(extensions.get("depth"), st_format.ST_ENTRY_DEFAULTS["depth"]),
            "selectiveLogic": _fallback(extensions.get("selectiveLogic"), 0),
            "outletName": _fallback(extensions.get("outlet_name"), ""),
            "group": _fallback(extensions.get("group"), ""),
            "groupOverride": _fallback(extensions.get("group_override"), False),
            "groupWeight": _fallback(
                extensions.get("group_weight"),
                st_format.ST_ENTRY_DEFAULTS["groupWeight"],
            ),
            "scanDepth": _fallback(extensions.get("scan_depth"), None),
            "caseSensitive": case_sensitive,
            "matchWholeWords": _fallback(extensions.get("match_whole_words"), None),
            "useGroupScoring": _fallback(extensions.get("use_group_scoring"), None),
            "automationId": _fallback(extensions.get("automation_id"), ""),
            "role": _fallback(extensions.get("role"), 0),
            "vectorized": _fallback(extensions.get("vectorized"), False),
            "sticky": _fallback(extensions.get("sticky"), None),
            "cooldown": _fallback(extensions.get("cooldown"), None),
            "delay": _fallback(extensions.get("delay"), None),
            "matchPersonaDescription": _fallback(extensions.get("match_persona_description"), False),
            "matchCharacterDescription": _fallback(extensions.get("match_character_description"), False),
            "matchCharacterPersonality": _fallback(extensions.get("match_character_personality"), False),
            "matchCharacterDepthPrompt": _fallback(extensions.get("match_character_depth_prompt"), False),
            "matchScenario": _fallback(extensions.get("match_scenario"), False),
            "matchCreatorNotes": _fallback(extensions.get("match_creator_notes"), False),
            "extensions": extensions,
            "triggers": extensions.get("triggers") or [],
            "ignoreBudget": _fallback(extensions.get("ignore_budget"), False),
        },
    )
    return world


def book_scan_depth(book: Mapping[str, Any]) -> int:
    """卡内书声明的扫描深度；是整数时夹到 1 到 50，否则用 4"""
    raw = book.get("scan_depth")
    if not isinstance(raw, int) or isinstance(raw, bool):
        return DEFAULT_BOOK_SCAN_DEPTH
    return max(SCAN_DEPTH_MIN, min(SCAN_DEPTH_MAX, raw))


def build_book_rows(
    book: Mapping[str, Any],
    *,
    budget: Optional[WorldbookBudget] = None,
    rewrite_names: bool = True,
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]], list[st_format.ImportNote]]:
    """把一本卡内书拆成书级 `ext`、条目 `(列, ext)` 与导入说明

    非对象的条目跳过并记 `skipped`；无效关键词写进条目的 `na_invalid_keys` 并记
    `invalid_key`。给了 `budget` 时，正文 token 数比每轮预算还多的条目各记一条 `over_budget`，
    与世界书文件导入用同一个判据 `st_format.budget_notes`。这里只算不写库，导入事务里再逐条 `create`。
    `rewrite_names` 为假时不把正文里读名字的变量换成宏（导入选项）；卡片检查与导出基线一直用默认值。
    导入与卡片检查都经 :func:`prepare_card_book` 调它（先在同一个线程里把 token 数进缓存）。
    """
    book_ext = {"na_card_book": copy.deepcopy({key: value for key, value in book.items() if key != "entries"})}
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    notes: list[st_format.ImportNote] = []

    entries = book.get("entries")
    if not isinstance(entries, list):
        entries = []
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            notes.append(st_format.ImportNote("", "", "skipped", f"第 {index + 1} 条不是对象，已跳过"))
            continue
        world = card_entry_to_world_entry(raw, index)
        try:
            # 与世界书文件导入同一套安全上限：超了整张卡拒绝，不截断卡内书的条目
            st_format.check_entry_limits(world, label=f"卡内知识书第 {index + 1} 条")
        except st_format.WorldFileRejected as exc:
            raise CardFileRejected(str(exc)) from exc
        columns, entry_notes = st_format.st_entry_to_columns(world, rewrite_names=rewrite_names)
        notes.extend(entry_notes)
        # 酒馆导出时把所有条目都写成正则，这里一律不按正则解释；原值另存在
        # `na_card_entry` 里，导出时还原。
        columns["use_regex"] = False

        ext = copy.deepcopy(world)
        ext["na_card_entry"] = copy.deepcopy(raw)
        invalid = st_format.validate_entry_regexes(columns)
        if invalid:
            ext["na_invalid_keys"] = invalid
            main_keys = set(columns.get("keys") or [])
            for item in invalid:
                field = "key" if item["key"] in main_keys else "keysecondary"
                notes.append(
                    st_format.ImportNote(
                        str(world.get("uid", "")),
                        field,
                        "invalid_key",
                        f"{item['key']}：{item['reason']}",
                    ),
                )
        rows.append((columns, ext))
    notes.extend(
        st_format.budget_notes(
            ((str(columns.get("uid", "")), columns) for columns, _ext in rows),
            budget=budget,
        ),
    )
    # 与世界书文件导入同一个判据：哪些条目按用途不进主提示词、哪些含不支持的模板、
    # 哪些疑似防盗。只是提示，条目照样导入、启用状态不改。
    keyed = [(str(columns.get("uid", "")), columns) for columns, _ext in rows]
    template_notes = wb_purpose.template_import_notes(keyed)
    notes.extend(template_notes)
    # 预算体检也与世界书文件导入同一个判据；卡内书建出来没有书级预算。
    check = wb_budget_check.check_import(
        keyed,
        budget=budget,
        max_entries=int(config.WORLDBOOK_MAX_ENTRIES_PER_ROUND),
        routed_keys=(note.entry_key for note in template_notes if note.kind in wb_budget_check.ROUTED_IMPORT_KINDS),
    )
    if check is not None and check.constant_count:
        notes.append(st_format.ImportNote("", "", "budget_check", check.summary()))
    return book_ext, rows, notes


PreparedCardBook = tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]], list[st_format.ImportNote]]


def prepare_card_book(
    book: Mapping[str, Any],
    *,
    budget: Optional[WorldbookBudget] = None,
    rewrite_names: bool = True,
) -> PreparedCardBook:
    """数 token、拆条目、分析模板：只读卡片、不碰数据库，在导入 / 卡片检查的执行器线程里跑

    token 数在本线程里数进缓存（不交给默认线程池），再交给 :func:`build_book_rows`。
    `rewrite_names` 见 :func:`build_book_rows`：导入时可以关掉，卡片检查一直开着。
    """
    entries = book.get("entries")
    if isinstance(entries, list):
        wb_tokens.prefetch_sync(str(raw.get("content") or "") for raw in entries if isinstance(raw, dict))
    return build_book_rows(book, budget=budget, rewrite_names=rewrite_names)


def prepare_card_book_for_owner(
    book: Mapping[str, Any],
    owner: tuple[str, str],
    *,
    rewrite_names: bool = True,
) -> PreparedCardBook:
    """同 :func:`prepare_card_book`，预算按书的归属实例在本线程里算"""
    return prepare_card_book(book, budget=wb_service.import_budget(*owner), rewrite_names=rewrite_names)


async def create_card_book(
    owner: tuple[str, str],
    preset_id: int,
    book_name: str,
    book: Mapping[str, Any],
    *,
    username: str,
    prepared: PreparedCardBook,
) -> tuple[DBWbBook, ImportReport]:
    """建一本绑在人设上的世界书并逐条写条目；调用方必须已经开好事务

    `prepared` 是事务之前在导入执行器里算好的 :func:`prepare_card_book_for_owner` 结果：分析要
    几秒到几十秒，不能在事务里做、占着数据库连接。条目信任级别恒为假：卡片来自网上，即便
    导入人是平台方也不能把卡里的文字当成宿主内容。编号撞号时照世界书文件导入的规则取书内
    下一个可用值并记 `renumbered`。
    """
    book_ext, rows, notes = prepared
    notes = list(notes)
    name = (book_name or "").strip() or "知识书"
    name = name[:MAX_BOOK_NAME_CHARS]
    created_book = await wb_service.create_book(
        owner,
        BookCreateRequest(
            scope_kind=ScopeKind.PRESET,
            preset_id=preset_id,
            name=name,
            scan_depth=book_scan_depth(book),
            # 卡内知识书与世界书文件导入同一口径：新导入的书默认带说话人名字。
            scan_names=True,
        ),
        username=username,
    )
    created_book.ext = book_ext
    created_book.updated_by = username
    await created_book.save()

    used: set[int] = set()
    next_number = 0
    created = 0
    for columns, ext in rows:
        uid = columns.get("uid")
        if uid is None or uid in used:
            while next_number in used:
                next_number += 1
            new_uid = next_number
            next_number += 1
            detail = f"原编号缺失，改为 {new_uid}" if uid is None else f"原编号 {uid} 与书内已有编号冲突，改为 {new_uid}"
            notes.append(st_format.ImportNote("", "uid", "renumbered", detail))
            columns["uid"] = new_uid
            uid = new_uid
        used.add(int(uid))
        await DBWbEntry.create(
            book=created_book,
            trusted=False,
            status=EntryStatus.ACTIVE.value,
            ext=ext,
            created_by=username,
            updated_by=username,
            **columns,
        )
        created += 1

    report = ImportReport(
        book_id=created_book.id,
        created=created,
        notes=st_format.summarize_notes(notes),
        details=[
            ImportDetailItem(entry_key=note.entry_key, field=note.field, kind=note.kind, detail=note.detail)
            for note in notes[: wb_service.MAX_IMPORT_DETAILS]
        ],
    )
    return created_book, report


def world_entry_to_card_entry(world: Mapping[str, Any], *, use_regex: bool) -> dict[str, Any]:
    """把一条酒馆世界书条目转成卡内书条目形状

    照 `convertWorldInfoToCharacterBook` 逐字段移植，只有 `use_regex` 由调用方给：
    酒馆导出时写死 `true`，但 NA 导入时不按正则解释普通关键词，写死会让往返丢语义。
    """
    raw_ext = world.get("extensions")
    extensions = copy.deepcopy(raw_ext) if isinstance(raw_ext, dict) else {}
    extensions.update(
        {
            "position": _fallback(world.get("position"), 0),
            "exclude_recursion": _fallback(world.get("excludeRecursion"), False),
            "display_index": world.get("displayIndex"),
            "probability": _fallback(world.get("probability"), None),
            "useProbability": _fallback(world.get("useProbability"), False),
            "depth": _fallback(world.get("depth"), 4),
            "selectiveLogic": _fallback(world.get("selectiveLogic"), 0),
            "outlet_name": _fallback(world.get("outletName"), ""),
            "group": _fallback(world.get("group"), ""),
            "group_override": _fallback(world.get("groupOverride"), False),
            "group_weight": _fallback(world.get("groupWeight"), None),
            "prevent_recursion": _fallback(world.get("preventRecursion"), False),
            "delay_until_recursion": _fallback(world.get("delayUntilRecursion"), False),
            "scan_depth": _fallback(world.get("scanDepth"), None),
            "match_whole_words": _fallback(world.get("matchWholeWords"), None),
            "use_group_scoring": _fallback(world.get("useGroupScoring"), False),
            "case_sensitive": _fallback(world.get("caseSensitive"), None),
            "automation_id": _fallback(world.get("automationId"), ""),
            "role": _fallback(world.get("role"), 0),
            "vectorized": _fallback(world.get("vectorized"), False),
            "sticky": _fallback(world.get("sticky"), None),
            "cooldown": _fallback(world.get("cooldown"), None),
            "delay": _fallback(world.get("delay"), None),
            "match_persona_description": _fallback(world.get("matchPersonaDescription"), False),
            "match_character_description": _fallback(world.get("matchCharacterDescription"), False),
            "match_character_personality": _fallback(world.get("matchCharacterPersonality"), False),
            "match_character_depth_prompt": _fallback(world.get("matchCharacterDepthPrompt"), False),
            "match_scenario": _fallback(world.get("matchScenario"), False),
            "match_creator_notes": _fallback(world.get("matchCreatorNotes"), False),
            "triggers": world.get("triggers") or [],
            "ignore_budget": _fallback(world.get("ignoreBudget"), False),
        },
    )
    return {
        "id": world.get("uid"),
        "keys": list(world.get("key") or []),
        "secondary_keys": list(world.get("keysecondary") or []),
        "comment": _fallback(world.get("comment"), ""),
        "content": _fallback(world.get("content"), ""),
        "constant": _fallback(world.get("constant"), False),
        "selective": _fallback(world.get("selective"), False),
        "insertion_order": _fallback(world.get("order"), 100),
        "enabled": not bool(world.get("disable")),
        "position": "before_char" if _fallback(world.get("position"), 0) == 0 else "after_char",
        "use_regex": bool(use_regex),
        "extensions": extensions,
    }


def _merge_top_level(result: dict[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    """`after` 与 `before` 相同就保留 `result` 的原值，不同就写 `after` 的值

    顶层逐个字段比较；`extensions` 再逐个子键比较——酒馆的扩展字段随时会加，整块替换
    会把没编辑过的未知键一起冲掉。
    """
    for key, after_value in after.items():
        if key == "extensions" and isinstance(after_value, dict):
            original_ext = result.get("extensions")
            before_ext = before.get("extensions")
            before_ext = before_ext if isinstance(before_ext, dict) else {}
            # 先算出真的改过的子键。原条目没有 `extensions` 时不能整块补上：
            # `before` 是按导入规则映射出的默认值，整块补会把默认值写成一次编辑。
            changed = {
                sub_key: sub_value
                for sub_key, sub_value in after_value.items()
                if sub_key not in before_ext or before_ext[sub_key] != sub_value
            }
            if not changed:
                continue
            if not isinstance(original_ext, dict):
                original_ext = {}
                result["extensions"] = original_ext
            for sub_key, sub_value in changed.items():
                original_ext[sub_key] = copy.deepcopy(sub_value)
            continue
        if key not in before or before[key] != after_value:
            result[key] = copy.deepcopy(after_value)


def card_entry_from_row(columns: Mapping[str, Any], ext: Mapping[str, Any]) -> dict[str, Any]:
    """把一行条目还原成卡内书条目；没编辑过的条目逐字还原

    基准是 `ext.na_card_entry`（卡内原条目）。把原条目按导入规则再映射一次得到
    `before`，把当前列按导出规则映射一次得到 `after`；两者相同说明这一处没有编辑，
    保留原条目里的值（包括 NA 不用的 `priority`、`name`、`use_regex` 等）。
    """
    current_world = st_format.columns_to_st_entry(columns, ext)
    original = ext.get("na_card_entry") if isinstance(ext, Mapping) else None
    if not isinstance(original, dict):
        return world_entry_to_card_entry(current_world, use_regex=bool(columns.get("use_regex")))

    # 导入时把转换后的世界书形状整份存进了 `ext`，它就是最准的 `before`：直接拿它比，
    # 不必再按 `orig` 猜数组序号。猜序号在「条目没有 id、却显式写了 display_index」时
    # 一定会猜错，结果给没编辑过的条目凭空补一个 id。
    stored_world = {key: value for key, value in ext.items() if not str(key).startswith("na_")} if isinstance(ext, Mapping) else {}
    if stored_world:
        baseline_world = stored_world
    else:
        original_id = original.get("id")
        stored_index = ext.get("displayIndex") if isinstance(ext, Mapping) else None
        if isinstance(stored_index, int) and not isinstance(stored_index, bool):
            index = stored_index
        elif isinstance(original_id, int) and not isinstance(original_id, bool):
            index = original_id
        else:
            index = 0
        baseline_world = card_entry_to_world_entry(original, index)
    before = world_entry_to_card_entry(
        baseline_world,
        use_regex=bool(original.get("use_regex", False)),
    )

    # 酒馆读条目时 extensions 里的同名字段优先于顶层字段，而 `columns_to_st_entry` 只改
    # 顶层、不会同步 extensions。这里把真改过的列写回 extensions，否则改完深度/位置导出
    # 后重新导入会被旧的 extensions 覆盖回去。
    baseline_columns, _ = st_format.st_entry_to_columns(baseline_world)
    if not isinstance(current_world.get("extensions"), dict):
        current_world["extensions"] = {}
    current_ext = current_world["extensions"]
    for column, ext_key, world_key in (
        ("position", "position", "position"),
        ("depth", "depth", "depth"),
        ("scan_depth", "scan_depth", "scanDepth"),
        ("case_sensitive", "case_sensitive", "caseSensitive"),
        ("match_whole_words", "match_whole_words", "matchWholeWords"),
        ("ignore_budget", "ignore_budget", "ignoreBudget"),
        ("selective_logic", "selectiveLogic", "selectiveLogic"),
        ("sticky", "sticky", "sticky"),
        ("cooldown", "cooldown", "cooldown"),
        ("delay", "delay", "delay"),
    ):
        if columns.get(column) != baseline_columns.get(column):
            current_ext[ext_key] = copy.deepcopy(current_world.get(world_key))

    after = world_entry_to_card_entry(current_world, use_regex=bool(original.get("use_regex", False)))
    result = copy.deepcopy(original)
    _merge_top_level(result, before, after)
    return result


def build_character_book(books: Sequence[tuple[DBWbBook, list[DBWbEntry]]]) -> dict[str, Any] | None:
    """把若干本人设书合并成一张卡内知识书；一本都没有返回 `None`

    书级以第一本的 `ext.na_card_book` 为底（没有就用书名），条目按（书 ID, 条目 uid）
    顺序合并。调用方已经按可见范围筛过书，这里不再判归属。
    """
    if not books:
        return None
    first_book = books[0][0]
    first_ext = first_book.ext if isinstance(first_book.ext, dict) else {}
    card_book = first_ext.get("na_card_book")
    if isinstance(card_book, dict):
        result = copy.deepcopy(card_book)
    else:
        result = {"name": first_book.name}
    result.pop("entries", None)

    entries: list[dict[str, Any]] = []
    ordered = sorted(
        ((book, entry) for book, book_entries in books for entry in book_entries),
        key=lambda item: (item[0].id, item[1].uid),
    )
    for _book, entry in ordered:
        entries.append(card_entry_from_row(wb_service.entry_columns(entry), entry.ext or {}))
    result["entries"] = entries
    return result


__all__ = [
    "DEFAULT_BOOK_SCAN_DEPTH",
    "book_scan_depth",
    "build_book_rows",
    "build_character_book",
    "card_entry_from_row",
    "card_entry_to_world_entry",
    "create_card_book",
    "prepare_card_book",
    "prepare_card_book_for_owner",
    "world_entry_to_card_entry",
]
