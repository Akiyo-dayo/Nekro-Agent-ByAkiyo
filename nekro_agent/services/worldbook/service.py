"""世界书管理的数据访问层

本包其它模块都是纯函数，只有这里查库。放成一个单独模块而不是散在路由里，是因为「一条
条目怎么判命中」必须能在没有数据库的情况下被穷举用例钉住；反过来，「这一行归谁」必须
只有一处实现——两件事混在一个文件里，下一次改动就会顺手把授权判据带进引擎。

## 范围一律来自 `instance_scope`

列表与隔离区用 `scope.apply(DBWbBook.all(), MemTable.WB_BOOK, ...)`，运行时视角用
`apply_worldbook_condition`。本目录被静态守卫扫描：`DBWbBook` / `DBWbEntry` 的查询里不
许直接出现 `adapter_key` / `instance_key` / `preset_id` / `chat_key` / `person_account`
这些隔离列——它们一旦拼到查询上，就等于绕开了唯一那份范围条件。

## 条目只经书访问

`wb_entry` 没有归属列，可见范围永远算在书上。因此这里的条目查询一律先拿到书（或在
子查询里先按范围筛书），不单独给条目表拼一套判据：两套判据迟早会分叉，而分叉的那一侧
不会报错，只会静默多给几行。
"""

from __future__ import annotations

import asyncio
import copy
import weakref
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from tortoise.expressions import F, Q, Subquery
from tortoise.functions import Count
from tortoise.transactions import in_transaction

from nekro_agent.core.config import config
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import GLOBAL_PRESET_OWNER, DBPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.schemas.worldbook import (
    BookCreateRequest,
    BookUpdateRequest,
    EntryCreateRequest,
    EntryUpdateRequest,
    ImportDetailItem,
    ImportReport,
)
from nekro_agent.services.config_resolver import config_resolver
from nekro_agent.services.memory.instance_scope import AdminFilter, AdminMemoryScope, admin_row_visible
from nekro_agent.services.memory.scope_condition import MemTable
from nekro_agent.services.user.tenant_perm import accessible_instance_refs, get_tenant_scope

from . import safe_regex, st_format
from .types import (
    DEFAULT_BOOK_SCAN_DEPTH,
    DEFAULT_PERSON_BOOK_SCAN_DEPTH,
    MAX_NAME_CHARS,
    EntryStatus,
    Position,
    QuarantineReason,
    ScopeKind,
)

# 列表允许的排序字段与方向。白名单而不是把参数直接拼进 `order_by`：拼错字段名时
# Tortoise 会抛 `FieldError`（500），而管理员只是点错了一列。
_SORTS = frozenset({"update_time", "name", "id"})
_ORDERS = frozenset({"asc", "desc"})

# 条目上可以为空的列：空表示跟着书走（扫描深度、大小写、整词）或不用（三个定时效果）。
# 改条目时只有这几列接受显式 `null`；其余列为空没有意义，`null` 一律当作没传。
_NULLABLE_ENTRY_FIELDS = frozenset(
    {"scan_depth", "case_sensitive", "match_whole_words", "sticky", "cooldown", "delay"},
)

# 导入报告明细的条数上限。一份两千条的文件可能产生上万条明细，全带回去只会把响应体
# 撑大一个数量级，而管理员实际只看前几条。
MAX_IMPORT_DETAILS = 200

# 导入时每批写入的条目数。一批一条 INSERT，几千条的书几个往返就写完；批太大单条语句
# 过长，PostgreSQL 的参数个数也有上限（每行十几列，500 行远在上限以内）。
IMPORT_BATCH_SIZE = 500

# 「记一笔」写出来的条目权重。取 100（建条目的默认权重）而不是某个更大的数：备注之间
# 按创建顺序排就够读了，压过书上其它条目的权重会让一句随手写的话挤到正经设定前面。
PERSON_NOTE_ORDER = 100

# 「记一笔」的「找书 → 没有就建 → 写条目」要串行：两个管理员同时给同一个人记第一笔时，
# 不串行就会各建一本同名个人书；同时往同一本书里写，条目编号都取「最大值 + 1」，后一个
# 撞唯一约束报 500。记一笔是低频的手动操作，整段用一把锁串起来就够了，不必按人分锁。
# 锁按事件循环各建一把：`asyncio.Lock` 一旦在某个循环上等待过就绑在那个循环上，跨循环
# 复用会抛错（用例每条一个循环）。只管本进程——NA 单进程运行；多进程部署时仍可能多建
# 一本书，后果只是世界书页里多一本同名的个人书，两本里的备注照常各自生效。
_person_note_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _person_note_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _person_note_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _person_note_locks[loop] = lock
    return lock

# 条目的列名清单，与 `st_format.st_entry_to_columns` 返回的键完全一致。导出时用它把
# 一行 ORM 对象读成同一形状，两边就不会出现「导入写了这一列、导出漏了它」。
_ENTRY_COLUMNS = (
    "uid",
    "comment",
    "keys",
    "secondary_keys",
    "selective_logic",
    "use_regex",
    "constant",
    "content",
    "order",
    "position",
    "depth",
    "scan_depth",
    "case_sensitive",
    "match_whole_words",
    "sticky",
    "cooldown",
    "delay",
    "enabled",
)


def _validate_sort_order(sort: str, order: str) -> str:
    if sort not in _SORTS:
        raise ValidationError(reason=f"不支持的排序字段: {sort}")
    if order not in _ORDERS:
        raise ValidationError(reason=f"不支持的排序方向: {order}")
    return f"-{sort}" if order == "desc" else sort


def _validate_page(page: int, page_size: int) -> int:
    if page < 1:
        raise ValidationError(reason="页码必须从 1 开始")
    if page_size < 1:
        raise ValidationError(reason="每页条数必须大于 0")
    # 上限直接截断而不是报错：前端把「全部」当成一个很大的数时，截断仍能给出可用结果。
    return min(page_size, 100)


def _parse_scope_kind(value: str) -> Optional[str]:
    """作用域类型筛选；空串表示不筛"""
    text = (value or "").strip().lower()
    if not text:
        return None
    try:
        return ScopeKind(text).value
    except ValueError as exc:
        raise ValidationError(reason=f"未知的作用域类型: {value}") from exc


async def list_books(
    scope: AdminMemoryScope,
    f: AdminFilter,
    *,
    scope_kind: str = "",
    enabled: Optional[bool] = None,
    q: str = "",
    page: int = 1,
    page_size: int = 20,
    sort: str = "update_time",
    order: str = "desc",
    readable_for: Optional[tuple[DBUser, str]] = None,
) -> tuple[int, list[DBWbBook]]:
    """书列表：范围条件 + 普通筛选 + 分页

    `q` 同时匹配书名与说明：管理页只有一个搜索框，让管理员先选「搜标题还是搜说明」是
    把实现细节推给他。

    `readable_for` 是（租户身份, 他选的实例）：给了就把平台方给全局人设建、人设对这个实例
    可见的书一并列出来（只读，见 :func:`platform_books_readable_by`）。这类书不属于任何频道、
    任何人，按频道或按人筛选时不并入；按人设筛选时照样只留那个人设的。
    """
    size = _validate_page(page, page_size)
    ordering = _validate_sort_order(sort, order)
    queryset = scope.apply(DBWbBook.all(), MemTable.WB_BOOK, f)
    if readable_for is not None and f.chat_key is None and f.person_accounts is None:
        user, instance_key = readable_for
        readable_q = _platform_preset_book_q(await _tenant_preset_owner_q(user, instance_key))
        if f.preset_id is not None:
            readable_q &= Q(preset_id=f.preset_id)
        queryset = DBWbBook.filter(
            Q(id__in=Subquery(queryset.values("id"))) | Q(id__in=Subquery(DBWbBook.filter(readable_q).values("id"))),
        )

    kind = _parse_scope_kind(scope_kind)
    if kind is not None:
        queryset = queryset.filter(scope_kind=kind)
    if enabled is not None:
        queryset = queryset.filter(enabled=enabled)
    keyword = (q or "").strip()
    if keyword:
        queryset = queryset.filter(Q(name__icontains=keyword) | Q(description__icontains=keyword))

    total = await queryset.count()
    rows = await queryset.order_by(ordering, "id").offset((page - 1) * size).limit(size)
    return total, list(rows)


async def book_counts(book_ids: Iterable[int]) -> dict[int, tuple[int, int]]:
    """一次聚合出每本书的条目数与隔离中的条目数

    逐本查会在列表页变成每页 100 次往返；这里用一条 `GROUP BY` 拿全部。
    """
    ids = sorted({int(book_id) for book_id in book_ids})
    if not ids:
        return {}
    rows = (
        await DBWbEntry.filter(book_id__in=ids)
        .group_by("book_id")
        .annotate(
            total=Count("id"),
            quarantined=Count("id", _filter=Q(status=EntryStatus.QUARANTINED.value)),
        )
        .values("book_id", "total", "quarantined")
    )
    counts: dict[int, tuple[int, int]] = {}
    for row in rows:
        counts[int(row["book_id"])] = (int(row["total"]), int(row["quarantined"]))
    return counts


async def binding_labels(rows: Iterable[DBWbBook]) -> dict[int, tuple[str, str, bool]]:
    """批量取每本书的绑定对象名字与「绑定还在不在」

    人设与频道各一次查询。查不到就是「绑定对象已删」——这个结论只对书自己的绑定字段
    成立，所以逐本判断 `scope_kind`，而不是把「查不到」一律当成缺失。
    """
    books = list(rows)
    if not books:
        return {}

    preset_ids = sorted({book.preset_id for book in books if book.scope_kind == ScopeKind.PRESET.value and book.preset_id > 0})
    preset_names: dict[int, str] = {}
    if preset_ids:
        preset_rows = await DBPreset.filter(id__in=preset_ids).values_list("id", "title", "name")
        preset_names = {int(preset_id): str(title or name) for preset_id, title, name in preset_rows}

    chat_keys = sorted({book.chat_key for book in books if book.scope_kind == ScopeKind.CHANNEL.value and book.chat_key})
    channel_names: dict[str, str] = {}
    if chat_keys:
        channel_rows = await DBChatChannel.filter(chat_key__in=chat_keys).order_by("id").values_list("chat_key", "channel_name")
        for chat_key, channel_name in channel_rows:
            # 同一个 `chat_key` 可能有多行（唯一约束是适配器、实例、频道 ID 三元组），
            # 取第一行按 ID 排序，结果不随数据库返回顺序变化。
            channel_names.setdefault(str(chat_key), str(channel_name or ""))

    labels: dict[int, tuple[str, str, bool]] = {}
    for book in books:
        preset_name = ""
        channel_name = ""
        binding_missing = False
        if book.scope_kind == ScopeKind.PRESET.value:
            preset_name = preset_names.get(book.preset_id, "")
            # 人设书没有绑定人设时永远不可能命中，与「人设被删」在管理页上是同一件事。
            binding_missing = book.preset_id <= 0 or book.preset_id not in preset_names
        elif book.scope_kind == ScopeKind.CHANNEL.value:
            channel_name = channel_names.get(book.chat_key, "")
            binding_missing = not book.chat_key or book.chat_key not in channel_names
        labels[book.id] = (preset_name, channel_name, binding_missing)
    return labels


async def load_book(book_id: int) -> Optional[DBWbBook]:
    """按主键取一行；归属在路由里判，这里不重复一遍授权"""
    return await DBWbBook.get_or_none(id=book_id)


def _platform_preset_book_q(preset_owner_q: Q) -> Q:
    """「平台方给全局人设建的书，且人设满足 `preset_owner_q`」的条件

    只认三样都满足的书：作用域是人设、归属实例对为空、绑的人设在给定的可见范围里。归属
    为空的域书 / 个人书 / 频道书不存在（建书时就拒了），即便库里有脏行也不放开。
    """
    return Q(
        scope_kind=ScopeKind.PRESET.value,
        adapter_key="",
        instance_key="",
        preset_id__in=Subquery(DBPreset.filter(preset_owner_q).values("id")),
    )


async def _tenant_preset_owner_q(user: DBUser, instance_key: Optional[str]) -> Q:
    """租户看得见的人设：全局共享人设，加上指定实例（不指定时是名下全部实例）自己的人设

    判据与人设页的 `visible_presets` 同一条：全局人设对所有实例可见，实例私有人设只对它
    自己可见。人设的归属列只有实例标识（没有适配器），所以这里取的是租户名下实例对里的
    实例标识——实例对来自 `accessible_instance_refs`，归属判据本身仍按实例对算。
    """
    if instance_key:
        owners = [instance_key]
    else:
        owners = sorted({ref_instance for _adapter, ref_instance in await accessible_instance_refs(user)})
    return Q(owner_instance_key=GLOBAL_PRESET_OWNER) | Q(owner_instance_key__in=owners)


async def platform_books_readable_by(
    user: DBUser,
    books: Iterable[DBWbBook],
    *,
    instance_key: Optional[str] = None,
) -> set[int]:
    """这些书里，平台方给全局人设建、当前租户可以**只读**查看的那些书 ID

    所有者 2026-11-10 定：平台方给全局人设导入的世界书，只要这个人设对租户的实例可见，
    租户就能看书名、条目与正文，但不能改、删、导出、追加导入。人设对他的实例不可见的书
    照旧看不到；别的租户的书、平台方给某个实例建的书不在这里放开（它们的归属实例对非空，
    走 `admin_row_visible`）。

    平台方恒为空集：他本来就能看能改，不是「只读」。`instance_key` 给定时只按那个实例的
    可见人设判（列表页带着实例作用域），不给时按租户名下任一实例判（按 ID 取书没有实例参数）。
    """
    if get_tenant_scope(user) is not None:
        candidates = [
            book
            for book in books
            if book.scope_kind == ScopeKind.PRESET.value
            and not book.adapter_key
            and not book.instance_key
            and book.preset_id > 0
        ]
    else:
        candidates = []
    if not candidates:
        return set()
    preset_ids = {book.preset_id for book in candidates}
    visible_ids = set(
        await DBPreset.filter(await _tenant_preset_owner_q(user, instance_key), id__in=sorted(preset_ids)).values_list(
            "id",
            flat=True,
        ),
    )
    return {book.id for book in candidates if book.preset_id in visible_ids}


async def books_invisible_to(user: DBUser, books: Iterable[DBWbBook]) -> set[int]:
    """这些书里当前身份看不到的那些书 ID

    平台方恒为空集。租户逐本判：判据与按 ID 取书那段完全同一个（`admin_row_visible`，
    加上 :func:`platform_books_readable_by` 放开的只读那一类），不另写一遍「归属实例对在
    不在我的实例里」——那正是最容易与授权入口分叉的地方。

    放在服务层而不是路由里：提示词预览也要用同一个判据决定哪些书要遮正文，两处各写一份
    迟早分叉，而分叉的那一侧不会报错。
    """
    if get_tenant_scope(user) is None:
        return set()
    books = list(books)
    invisible: set[int] = set()
    for book in books:
        if not await admin_row_visible(user, book.adapter_key, book.instance_key):
            invisible.add(book.id)
    if invisible:
        invisible -= await platform_books_readable_by(user, [book for book in books if book.id in invisible])
    return invisible


async def load_entry(entry_id: int) -> Optional[DBWbEntry]:
    """按主键取一条并连上它的书：归属判在书上，读条目时必须能拿到那一行"""
    return await DBWbEntry.get_or_none(id=entry_id).select_related("book")


def _scan_depth_for(req: BookCreateRequest) -> int:
    """建书时的扫描深度：请求没给就按作用域取默认"""
    if req.scan_depth is not None:
        return req.scan_depth
    if req.scope_kind is ScopeKind.PERSON:
        return DEFAULT_PERSON_BOOK_SCAN_DEPTH
    return DEFAULT_BOOK_SCAN_DEPTH


async def create_book(
    owner: tuple[str, str],
    req: BookCreateRequest,
    *,
    username: str,
) -> DBWbBook:
    """建一本书；作用域字段按类型清干净

    四种作用域共用一张表，因此没被这一种用到的绑定字段必须清成 0 / ""：留着上一次的
    值（或请求里顺手带的值）会让「这本书绑的是谁」出现两个互相矛盾的答案。
    """
    preset_id = 0
    chat_key = ""
    person_account = ""
    if req.scope_kind is ScopeKind.PRESET:
        if req.preset_id <= 0:
            raise ValidationError(reason="人设书必须选择一个人设")
        preset_id = req.preset_id
    elif req.scope_kind is ScopeKind.CHANNEL:
        chat_key = req.chat_key.strip()
        if not chat_key:
            raise ValidationError(reason="频道书必须指定频道")
    elif req.scope_kind is ScopeKind.PERSON:
        person_account = req.person_account.strip()
        if not person_account:
            raise ValidationError(reason="个人书必须指定一个人")

    adapter_key, instance_key = owner
    return await DBWbBook.create(
        name=req.name,
        description=req.description,
        scope_kind=req.scope_kind.value,
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset_id,
        chat_key=chat_key,
        person_account=person_account,
        enabled=req.enabled,
        scan_depth=_scan_depth_for(req),
        budget_chars=req.budget_chars,
        case_sensitive=req.case_sensitive,
        match_whole_words=req.match_whole_words,
        created_by=username,
        updated_by=username,
    )


async def update_book(book: DBWbBook, req: BookUpdateRequest, *, username: str) -> DBWbBook:
    """改书设置；`None` 的字段保持原值

    作用域与绑定对象不在这里出现：改作用域等于换一本书，删了重建才是可解释的操作。
    """
    changes: dict[str, Any] = {}
    if req.name is not None:
        changes["name"] = req.name
    if req.description is not None:
        changes["description"] = req.description
    if req.enabled is not None:
        changes["enabled"] = req.enabled
    if req.scan_depth is not None:
        changes["scan_depth"] = req.scan_depth
    if req.budget_chars is not None:
        changes["budget_chars"] = req.budget_chars
    if req.case_sensitive is not None:
        changes["case_sensitive"] = req.case_sensitive
    if req.match_whole_words is not None:
        changes["match_whole_words"] = req.match_whole_words
    if not changes:
        return book
    changes["updated_by"] = username
    await book.update_from_dict(changes).save()
    return book


async def delete_book(book: DBWbBook) -> int:
    """删书并连带删条目，返回删掉的条目数

    先数再删：删完之后再数只能得到 0，而返回值是管理员确认「确实删掉了这些」的唯一凭据。
    条目由外键的 `CASCADE` 删除，不在这里再写一遍批量删除。
    """
    entry_ids = [int(entry_id) for entry_id in await DBWbEntry.filter(book_id=book.id).values_list("id", flat=True)]
    await book.delete()
    # 与删单条一样清熔断表：外键级联删掉的条目不经过 `delete_entry`。
    for entry_id in entry_ids:
        safe_regex.forget_entry(entry_id)
    return len(entry_ids)


async def list_entries(book: DBWbBook) -> list[DBWbEntry]:
    """一本书的条目：权重大的在前，同权重按创建顺序"""
    return list(await DBWbEntry.filter(book_id=book.id).order_by("-order", "id"))


async def next_uid(book: DBWbBook) -> int:
    """书内下一个可用编号：现有最大值加一，空书从 0 开始

    取 `order_by("-uid").limit(1)` 而不是聚合：`(book, uid)` 上有唯一索引，这条查询走的
    就是那个索引，而聚合在空表上还要单独处理「没有行」的返回形状。
    """
    rows = await DBWbEntry.filter(book_id=book.id).order_by("-uid").limit(1).values_list("uid", flat=True)
    if not rows:
        return 0
    return int(rows[0]) + 1


def _validate_keys(keys: Iterable[str], secondary_keys: Iterable[str], *, use_regex: bool) -> None:
    """逐个校验关键词；有无效的就整体拒绝并列出原因

    手动保存 400 而不是「先存下再标记无效」：管理员就在页面上，让他当场改一个错的正则
    远好过让一条每轮都被跳过的条目悄悄躺在库里。导入走另一条路（记进 `na_invalid_keys`），
    因为那份文件不是他此刻能改的。
    """
    problems: list[str] = []
    for key in keys:
        reason = safe_regex.validate_regex_key(key, use_regex=use_regex)
        if reason is not None:
            problems.append(f"{key}：{reason}")
    for key in secondary_keys:
        reason = safe_regex.validate_regex_key(key, use_regex=use_regex)
        if reason is not None:
            problems.append(f"{key}：{reason}")
    if problems:
        raise ValidationError(reason="；".join(problems))


async def create_entry(
    book: DBWbBook,
    req: EntryCreateRequest,
    *,
    is_platform: bool,
    username: str,
) -> DBWbEntry:
    """新建一条条目

    信任级别由**创建者身份**决定，请求里的 `trusted` 一律忽略：租户若能自己声明「我这条
    是可信内容」，这个标记就失去意义了。
    """
    _validate_keys(req.keys, req.secondary_keys, use_regex=req.use_regex)
    uid = await next_uid(book)
    return await DBWbEntry.create(
        book=book,
        uid=uid,
        comment=req.comment,
        keys=list(req.keys),
        secondary_keys=list(req.secondary_keys),
        selective_logic=req.selective_logic.value,
        use_regex=req.use_regex,
        constant=req.constant,
        content=req.content,
        order=req.order,
        position=req.position.value,
        depth=req.depth,
        scan_depth=req.scan_depth,
        case_sensitive=req.case_sensitive,
        match_whole_words=req.match_whole_words,
        sticky=req.sticky,
        cooldown=req.cooldown,
        delay=req.delay,
        enabled=req.enabled,
        trusted=is_platform,
        created_by=username,
        updated_by=username,
    )


async def update_entry(
    entry: DBWbEntry,
    req: EntryUpdateRequest,
    *,
    is_platform: bool,
    username: str,
) -> DBWbEntry:
    """改一条条目

    - 信任级别：租户改任何条目都写成不可信（他可以把平台方给的可信条目改成自己的话术，
      留着标记等于让这段文字继续被当成宿主内容注入）；平台方只有在请求里明确带了
      `trusted` 时才改，不带就保持原值。
    - `status` 不因编辑改变：隔离中的条目可以改，改完仍在隔离区，要放回是另一个动作。
    - 关键词用**最终的** `use_regex` 校验：同一次请求里把 `use_regex` 打开，就必须按
      正则口径校验，否则打开开关那一刻库里的普通关键词会变成坏正则。
    - 可空的几列（`_NULLABLE_ENTRY_FIELDS`）：请求里**显式**写了 `null` 就清空，回到
      「用书的」/「不用」；没写才是不改。不区分这两种的话，一条条目的扫描深度或大小写
      一旦单独设过，就再也回不到跟着书走。
    """
    changes: dict[str, Any] = {}
    for field in (
        "comment",
        "selective_logic",
        "use_regex",
        "constant",
        "content",
        "order",
        "position",
        "depth",
        "scan_depth",
        "case_sensitive",
        "match_whole_words",
        "sticky",
        "cooldown",
        "delay",
        "enabled",
    ):
        value = getattr(req, field)
        if value is None:
            if field in _NULLABLE_ENTRY_FIELDS and field in req.model_fields_set:
                changes[field] = None
            continue
        changes[field] = value.value if hasattr(value, "value") else value
    if req.keys is not None:
        changes["keys"] = list(req.keys)
    if req.secondary_keys is not None:
        changes["secondary_keys"] = list(req.secondary_keys)

    final_keys = changes.get("keys", entry.keys or [])
    final_secondary = changes.get("secondary_keys", entry.secondary_keys or [])
    final_use_regex = bool(changes.get("use_regex", entry.use_regex))
    _validate_keys(final_keys, final_secondary, use_regex=final_use_regex)

    if not is_platform:
        changes["trusted"] = False
    elif req.trusted is not None:
        changes["trusted"] = req.trusted

    # 关键词已经重新校验过，导入留下的无效关键词记录随之作废：留着会让一条改好的正则
    # 继续被跳过，管理员改完却看不到效果。
    ext = dict(entry.ext or {})
    ext.pop("na_invalid_keys", None)
    changes["ext"] = ext
    changes["updated_by"] = username

    await entry.update_from_dict(changes).save()
    safe_regex.forget_entry(entry.id)
    return entry


async def apply_entry_action(entry: DBWbEntry, action: str) -> DBWbEntry:
    """隔离 / 放回 / 启用 / 停用；已经是目标状态时不报错

    幂等是刻意的：管理页上两个管理员同时点「隔离」，后一个不该收到一个看不懂的错误。
    """
    changes: dict[str, Any] = {}
    if action == "quarantine":
        if entry.status != EntryStatus.QUARANTINED.value:
            changes["status"] = EntryStatus.QUARANTINED.value
        if entry.quarantined_at is None:
            changes["quarantined_at"] = datetime.now(timezone.utc)
        if entry.quarantine_reason != QuarantineReason.MANUAL.value:
            changes["quarantine_reason"] = QuarantineReason.MANUAL.value
    elif action == "restore":
        if entry.status != EntryStatus.ACTIVE.value:
            changes["status"] = EntryStatus.ACTIVE.value
        if entry.quarantined_at is not None:
            changes["quarantined_at"] = None
        if entry.quarantine_reason:
            changes["quarantine_reason"] = ""
        # 放回是「给它一次机会」：被拦计数不归零的话，下一次注入后只要再被拦一次就会
        # 立刻被自动隔离规则抓回去，管理员会觉得放回没生效。
        if entry.blocked_round_hits:
            changes["blocked_round_hits"] = 0
    elif action == "enable":
        if not entry.enabled:
            changes["enabled"] = True
    elif action == "disable":
        if entry.enabled:
            changes["enabled"] = False
    if not changes:
        return entry
    await entry.update_from_dict(changes).save()
    return entry


async def delete_entry(entry: DBWbEntry) -> None:
    """删一条条目，并清掉它在正则熔断表里的记录

    不清的话，新建的条目拿到同一个主键时会继承一条「运行过慢已停用」的旧记录，
    表现为「刚建的条目怎么不命中」。
    """
    entry_id = entry.id
    await entry.delete()
    safe_regex.forget_entry(entry_id)


async def list_quarantine(
    scope: AdminMemoryScope,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[DBWbEntry]]:
    """范围内的书里处于隔离中的条目

    书先用范围条件筛成子查询，条目再用 `book_id__in` 挂上去：不把书 ID 全拉到内存里
    再拼 `__in` 列表——平台方全量视图下的书可能有几千本，那样一条 SQL 会变成两条，
    第二条还会长到超出参数上限。
    """
    size = _validate_page(page, page_size)
    books_query = scope.apply(DBWbBook.all(), MemTable.WB_BOOK, AdminFilter())
    queryset = DBWbEntry.filter(
        status=EntryStatus.QUARANTINED.value,
        book_id__in=Subquery(books_query.values("id")),
    )
    total = await queryset.count()
    rows = await queryset.select_related("book").order_by("-quarantined_at", "-id").offset((page - 1) * size).limit(size)
    return total, list(rows)


def entry_columns(entry: DBWbEntry) -> dict[str, Any]:
    """把一行条目读成与 `st_entry_to_columns` 同名的列字典"""
    return {column: getattr(entry, column) for column in _ENTRY_COLUMNS}


def import_budget_chars(adapter_key: str, instance_key: str) -> int:
    """导入提示用的每轮正文预算：这个实例的生效值，没有实例时用全局值

    频道还能再往下调，但导入时不知道书会在哪些频道生效；按实例这一层提示，与管理员在
    「系统配置 → 世界书」里看到的数一致。读不到就不提示（返回 0），提示不该让导入失败。
    世界书文件导入与酒馆卡的检查、导入共用这一处。
    """
    try:
        effective = (
            config_resolver.get_instance_effective_config(adapter_key, instance_key) if instance_key else config
        )
        return int(getattr(effective, "WORLDBOOK_MAX_CHARS", 0) or 0)
    except Exception:  # noqa: BLE001 - 只是提示，读配置失败不影响导入
        return 0


def _import_budget_chars(book: DBWbBook) -> int:
    """这本书归属实例的导入提示预算，见 :func:`import_budget_chars`"""
    return import_budget_chars(book.adapter_key, book.instance_key)


async def _create_entries(
    book: DBWbBook,
    parsed: st_format.ParsedWorld,
    *,
    is_platform: bool,
    username: str,
    is_new_book: bool,
) -> ImportReport:
    """把解析好的文件写进一本书；调用方必须已经开好事务

    条目按批 `bulk_create`：几千条逐条 `create` 在 PostgreSQL 上每条一个往返，一份几 MB 的书
    要导几十秒，请求早就超时了。整批仍在调用方的同一个事务里，失败照样一条不留。
    """
    notes: list[st_format.ImportNote] = []
    existing = {int(uid) for uid in await DBWbEntry.filter(book_id=book.id).values_list("uid", flat=True)}
    used = set(existing)
    next_number = max(existing) + 1 if existing else 0
    created = 0
    pending: list[DBWbEntry] = []
    mapped: list[tuple[str, dict[str, Any]]] = []

    for entry_key, raw in parsed.entries:
        if not isinstance(raw, dict):
            # 酒馆的 `entries` 是对象，值却可能是任何东西（手工拼过的文件）。整条跳过并
            # 记一条明细，比让 `st_entry_to_columns` 抛异常更接近管理员的预期。
            notes.append(st_format.ImportNote(entry_key, "", "skipped", "条目不是一个对象，已跳过"))
            continue

        columns, entry_notes = st_format.st_entry_to_columns(raw)
        notes.extend(entry_notes)

        uid = columns.get("uid")
        if uid is None or uid in used:
            while next_number in used:
                next_number += 1
            new_uid = next_number
            next_number += 1
            detail = f"原编号缺失，改为 {new_uid}" if uid is None else f"原编号 {uid} 与书内已有编号冲突，改为 {new_uid}"
            notes.append(st_format.ImportNote(entry_key, "uid", "renumbered", detail))
            columns["uid"] = new_uid
            uid = new_uid
        used.add(int(uid))

        # 原始条目整份进 `ext`：导出时以它为底还原，酒馆新增的字段就不会在往返里丢掉。
        ext = copy.deepcopy(raw)
        invalid = st_format.validate_entry_regexes(columns)
        if invalid:
            # 导入不因为坏正则整份失败：文件是别人给的，管理员此刻改不了它。坏关键词留在
            # `keys` 里（导出不丢），匹配时按 `na_invalid_keys` 跳过。
            ext["na_invalid_keys"] = invalid
            # 报告里逐个列出来：只记进 `ext` 的话，管理员要一条条点开条目才知道哪些关键词
            # 被判无效、永远不会命中。
            main_keys = set(columns.get("keys") or [])
            for item in invalid:
                field = "key" if item["key"] in main_keys else "keysecondary"
                notes.append(
                    st_format.ImportNote(entry_key, field, "invalid_key", f"{item['key']}：{item['reason']}"),
                )

        pending.append(
            DBWbEntry(
                book=book,
                trusted=is_platform,
                status=EntryStatus.ACTIVE.value,
                ext=ext,
                created_by=username,
                updated_by=username,
                **columns,
            ),
        )
        mapped.append((entry_key, columns))
        created += 1

    if pending:
        await DBWbEntry.bulk_create(pending, batch_size=IMPORT_BATCH_SIZE)
    notes.extend(st_format.budget_notes(mapped, max_chars=_import_budget_chars(book)))

    if is_new_book:
        # 只有新建的书才写书级 `ext`：追加导入时覆盖它，会把这本书原有的酒馆字段冲掉。
        book.ext = copy.deepcopy(parsed.book_ext)
        book.updated_by = username
        await book.save()

    return ImportReport(
        book_id=book.id,
        created=created,
        notes=st_format.summarize_notes(notes),
        details=[
            ImportDetailItem(entry_key=note.entry_key, field=note.field, kind=note.kind, detail=note.detail)
            for note in notes[:MAX_IMPORT_DETAILS]
        ],
    )


async def import_world(
    book: DBWbBook,
    parsed: st_format.ParsedWorld,
    *,
    is_platform: bool,
    username: str,
    is_new_book: bool = False,
) -> ImportReport:
    """追加导入到已有书

    整本放在一个事务里：一次导入要么整份进库，要么一条都不进。中途失败留下半本书，管理
    员既看不出缺了哪些条目，再导一次还会因为编号冲突而全部重编号。
    """
    async with in_transaction():
        return await _create_entries(
            book,
            parsed,
            is_platform=is_platform,
            username=username,
            is_new_book=is_new_book,
        )


async def import_new_book(
    owner: tuple[str, str],
    req: BookCreateRequest,
    parsed: st_format.ParsedWorld,
    *,
    is_platform: bool,
    username: str,
) -> ImportReport:
    """建书并导入，整笔放在一个事务里

    建书与写条目必须同一个事务：分开的话导入失败会留下一本空书，而管理页上「导入失败」
    与「多了一本空书」这两件事同时发生，管理员只能手工删掉它。
    """
    async with in_transaction():
        book = await create_book(owner, req, username=username)
        return await _create_entries(
            book,
            parsed,
            is_platform=is_platform,
            username=username,
            is_new_book=True,
        )


async def export_book(book: DBWbBook) -> dict[str, Any]:
    """导出成酒馆世界书格式

    以每条自己的 `ext` 为底还原：导出必须能一字不差地还回用户导进来的那份文件，否则
    「导出备份一下再导入」这条路会悄悄丢字段。
    """
    entries = await list_entries(book)
    return st_format.export_world(
        book.ext,
        [(entry_columns(entry), entry.ext or {}) for entry in entries],
    )


async def active_entries_for_books(book_ids: Iterable[int]) -> list[DBWbEntry]:
    """取这些书里启用且未隔离的条目，给触发测试用"""
    ids = sorted({int(book_id) for book_id in book_ids})
    if not ids:
        return []
    return list(
        await DBWbEntry.filter(
            book_id__in=ids,
            enabled=True,
            status=EntryStatus.ACTIVE.value,
        ).order_by("-order", "id"),
    )


async def find_person_books(
    scope: AdminMemoryScope,
    owner: tuple[str, str],
    person_account: str,
) -> list[DBWbBook]:
    """范围内属于这个实例、绑在这个人身上的个人书，按 `id` 升序

    按人筛走 `AdminFilter(person_accounts=...)`，**精确等于**而不是别名展开：这个函数
    只服务「记一笔」，而记一笔要落到某一本具体的书上——地址簿展开会把「这个人的副号
    也建过一本」一并捞出来，于是每次记一笔都可能写到另一本里去。别名展开留给列表页的
    那把筛子（`resolve_person_filter`），那里多捞一本只是多显示一行。

    `owner` 的过滤在 Python 里做：范围已经由 `scope` 收窄，这一步只是把「同一个人在别的
    实例下的书」排除掉；比较对象属性不是查询条件，因此不绕开范围条件。

    书表没有工作区分区，`AdminFilter` 只带人这一维。
    """
    account = person_account.strip()
    if not account:
        return []
    queryset = scope.apply(
        DBWbBook.all(),
        MemTable.WB_BOOK,
        AdminFilter(person_accounts=frozenset({account})),
    )
    # `scope_kind` 是普通筛选（不是授权判据），拼在范围条件之后。
    books = await queryset.filter(scope_kind=ScopeKind.PERSON.value).order_by("id")
    return [book for book in books if (book.adapter_key, book.instance_key) == owner]


async def person_display_name(owner: tuple[str, str], person_account: str) -> str:
    """这个实例下这个账号最近一条消息的昵称；取不到就退回账号本身

    昵称只是书名的点缀（「关于 小明」比「关于 u12345」好认），所以取不到不是错误，也不
    该让记一笔失败：新入群、消息被清理、账号只在别的实例发过言都会走到这里。优先用
    `sender_nickname`（群名片，人在群里看到的那个名字），它为空时退回 `sender_name`，
    再退到账号本身。

    查询按**实例对**限定：同一个账号可能在好几个实例里都说过话，取到别的实例的昵称会让
    书名与这本书的归属对不上，而书名是管理员日后在世界书页里认书的第一眼线索。
    """
    account = person_account.strip()
    adapter_key, instance_key = owner
    if not account:
        return ""
    rows = (
        await DBChatMessage.filter(
            adapter_key=adapter_key,
            instance_key=instance_key,
            platform_userid=account,
        )
        .order_by("-id")
        .limit(1)
        .values_list("sender_nickname", "sender_name")
    )
    if not rows:
        return account
    nickname, name = rows[0]
    return str(nickname or name or account)


async def add_person_note(
    scope: AdminMemoryScope,
    owner: tuple[str, str],
    person_account: str,
    content: str,
    *,
    is_platform: bool,
    username: str,
) -> tuple[DBWbBook, DBWbEntry, bool]:
    """给一个人写一句备注，落成他的个人书里一条常驻条目

    返回 `(书, 条目, 是否新建了书)`。

    ## 为什么是常驻条目

    备注的意思是「这个人出现时就该知道这件事」，而不是「提到某个关键词才想起来」。因此
    条目写成 `constant=True`、`position=before_history`、`order=100`：常驻让它不看关键词，
    位置与权重让它在多本个人书之间有个稳定次序（同一位置按权重排，权重相同按创建顺序）。

    ## 为什么找不到书要现建一本

    好感详情页是「想到什么记一句」的场景，要求管理员先跳到世界书页建一本书再回来，
    等于把这个功能藏起来。书按这个人命名（「关于 昵称」），管理员事后在世界书页看到
    它就是一本普通的个人书，可以照常改名、停用、删掉。

    ## 复用建书与建条目那两个入口

    书名截断、作用域字段清空、编号取最大值加一、信任级别只看身份，这些规矩在
    `create_book` / `create_entry` 里各只有一份。这里另写一段 INSERT 的话，下一次改
    其中一条规矩（比如信任级别）就会漏掉记一笔这条路——而漏掉的那一侧不会报错。

    ## 事务

    建书与写条目在同一个事务里：分开的话，建完书那一步失败会留下一本空书，而界面上
    「记一笔失败」与「多了一本空书」同时发生，管理员只能手工去删。
    """
    account = person_account.strip()
    if not account:
        raise ValidationError(reason="必须指定一个人")

    # 找书也在锁里：锁外先找、锁内再建，两个请求仍会都以为「还没有书」。
    async with _person_note_lock():
        books = await find_person_books(scope, owner, account)
        async with in_transaction():
            created_book = False
            if books:
                book = books[0]
            else:
                created_book = True
                book = await create_book(
                    owner,
                    BookCreateRequest(
                        # 昵称来自群名片，长度不受控；书名上限由 `BookCreateRequest` 把关，
                        # 这里先截断，免得一个爱改长名字的人让「记一笔」永远 422。
                        name=f"关于 {await person_display_name(owner, account)}"[:MAX_NAME_CHARS],
                        scope_kind=ScopeKind.PERSON,
                        person_account=account,
                    ),
                    username=username,
                )
            entry = await create_entry(
                book,
                EntryCreateRequest(
                    content=content,
                    comment=content[:40],
                    constant=True,
                    position=Position.BEFORE_HISTORY,
                    order=PERSON_NOTE_ORDER,
                ),
                is_platform=is_platform,
                username=username,
            )
    return book, entry, created_book


async def list_person_notes(
    scope: AdminMemoryScope,
    owner: tuple[str, str],
    person_account: str,
) -> list[tuple[DBWbBook, DBWbEntry]]:
    """这个人在这个实例的个人书里的全部条目，含停用与隔离中的

    好感详情要的是「我给这个人记过什么」，因此不能只看启用中的：停用一条不等于删掉它，
    列表里缺一行会让管理员以为它已经不在了，于是再记一遍。
    """
    books = await find_person_books(scope, owner, person_account)
    if not books:
        return []
    book_by_id = {book.id: book for book in books}
    entries = await DBWbEntry.filter(book_id__in=sorted(book_by_id)).order_by("book_id", "-order", "id")
    return [(book_by_id[entry.book_id], entry) for entry in entries]


async def describe_round_items(raw_items: Iterable[Any]) -> list[dict[str, Any]]:
    """提示词明细里引用的世界书条目现在长什么样

    每项在原字典基础上加 `status` / `comment` / `book_name`，另带三个内部键
    `_book_adapter_key` / `_book_instance_key` / `_book_id` 供路由判「这一条该不该让当前身份看到」。
    内部键由路由在返回前删掉，不进响应。

    状态取**此刻**的，不是当时的：明细记的是「这一轮注入了哪条」，而管理页要回答的是
    「它现在还能不能用」。两者混在一起会让「已隔离的条目在旧明细里仍显示正常」。
    优先级：条目行不存在 → `hard_deleted`；隔离 → `quarantined`；条目或书停用 →
    `disabled`；否则 `active`。隔离排在停用之前：一条既停用又隔离的条目，管理页要看到
    的是需要人做放回动作的那一个状态。

    一次批量回表而不是逐条查：一轮可能引用几十条，逐条查会把一次详情请求放大成几十次
    数据库往返。
    """
    items = [dict(item) for item in raw_items if isinstance(item, dict)]
    entry_ids: set[int] = set()
    for item in items:
        try:
            entry_ids.add(int(item.get("id")))
        except (TypeError, ValueError):
            continue
    rows = (
        [row async for row in DBWbEntry.filter(id__in=sorted(entry_ids)).select_related("book")] if entry_ids else []
    )
    by_id = {row.id: row for row in rows}

    described: list[dict[str, Any]] = []
    for item in items:
        try:
            entry_id = int(item.get("id"))
        except (TypeError, ValueError):
            described.append(item)
            continue
        row = by_id.get(entry_id)
        if row is None:
            item["status"] = "hard_deleted"
            item["comment"] = ""
            item["book_name"] = ""
            item["_book_adapter_key"] = ""
            item["_book_instance_key"] = ""
            item["_book_id"] = 0
            described.append(item)
            continue
        if row.status == EntryStatus.QUARANTINED.value:
            status = "quarantined"
        elif not row.enabled or not row.book.enabled:
            # 书停用等于它下面所有条目都不生效，明细里仍要显示成「停用」而不是「正常」：
            # 管理员据此决定是去条目上改还是去书上开。
            status = "disabled"
        else:
            status = "active"
        item["status"] = status
        item["comment"] = row.comment or ""
        item["book_name"] = row.book.name
        item["_book_adapter_key"] = row.book.adapter_key
        item["_book_instance_key"] = row.book.instance_key
        item["_book_id"] = row.book.id
        described.append(item)
    return described


# 一个被拦轮次里注入的关键词条目超过这个数，这一轮只计数、不转隔离：条目一多，被拦
# 的原因落在哪一条上就说不清了，按「注入过就有嫌疑」一并隔离会误伤一批无辜条目。
AUTO_QUARANTINE_MAX_KEYWORD_ENTRIES = 5


async def record_blocked_round(entry_ids: Iterable[int], *, threshold: int) -> list[tuple[int, int, int]]:
    """被拦轮次：这些条目计数 + 1，达到阈值且仍是正常状态的关键词条目转隔离

    返回这一轮真正被转成隔离的条目 `(条目 ID, 书 ID, 被拦次数)`，调用方据此打日志。

    调用方负责事务：本函数只发 UPDATE，计数与转隔离必须在**同一个事务**里提交，
    否则计数到了、隔离那一步失败（或进程在两步之间退出），条目下一轮照样注入，而只有
    **再一次**被拦才会重新命中阈值——自动隔离这个兜底就悬空了。

    计数不看状态（被拦这件事已经发生了，管理页要知道），转隔离只看 `active`：已隔离 /
    已停用的条目本来就不会再注入，重复把它们改成隔离既没有意义，也会刷新
    `quarantined_at`，让「什么时候被隔离的」变得不可信。

    两种情况只计数、不转隔离：

    - 常驻条目：它每一轮都注入，被拦的轮次它必然在场，计数到阈值说明不了它就是原因。
      按计数隔离的结果是一本书的常驻设定在几次被拦之后整批消失，人设跟着散架；
    - 这一轮注入的关键词条目超过 `AUTO_QUARANTINE_MAX_KEYWORD_ENTRIES` 条：嫌疑分摊到
      太多条目上，同样说不清是哪一条。常驻条目不算在这个数里。
    """
    ids = sorted({int(entry_id) for entry_id in entry_ids})
    if not ids:
        return []
    await DBWbEntry.filter(id__in=ids).update(blocked_round_hits=F("blocked_round_hits") + 1)
    if threshold <= 0:
        # 阈值为 0 表示关掉自动隔离，只留计数供管理页参考。
        return []
    keyword_entries = await DBWbEntry.filter(id__in=ids, constant=False).count()
    if keyword_entries > AUTO_QUARANTINE_MAX_KEYWORD_ENTRIES:
        return []
    rows = await DBWbEntry.filter(
        id__in=ids,
        constant=False,
        status=EntryStatus.ACTIVE.value,
        blocked_round_hits__gte=threshold,
    ).values("id", "book_id", "blocked_round_hits")
    if not rows:
        return []
    hits = [(int(row["id"]), int(row["book_id"]), int(row["blocked_round_hits"])) for row in rows]
    now = datetime.now(timezone.utc)
    await DBWbEntry.filter(id__in=[entry_id for entry_id, _book_id, _hits in hits]).update(
        status=EntryStatus.QUARANTINED.value,
        quarantined_at=now,
        quarantine_reason=QuarantineReason.AUTO_BLOCKED.value,
    )
    for entry_id, _book_id, _hits in hits:
        # 条目被隔离后它自己的正则熔断记录也该清掉：放回时按新条目重新计。
        safe_regex.forget_entry(entry_id)
    return sorted(hits)


__all__ = [
    "AUTO_QUARANTINE_MAX_KEYWORD_ENTRIES",
    "MAX_IMPORT_DETAILS",
    "PERSON_NOTE_ORDER",
    "active_entries_for_books",
    "add_person_note",
    "apply_entry_action",
    "binding_labels",
    "book_counts",
    "books_invisible_to",
    "create_book",
    "create_entry",
    "delete_book",
    "delete_entry",
    "describe_round_items",
    "entry_columns",
    "export_book",
    "find_person_books",
    "import_new_book",
    "import_world",
    "list_books",
    "list_entries",
    "list_person_notes",
    "list_quarantine",
    "load_book",
    "load_entry",
    "next_uid",
    "person_display_name",
    "record_blocked_round",
    "update_book",
    "update_entry",
]
