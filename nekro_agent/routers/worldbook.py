"""世界书管理接口

## 作用域

可见范围只由 `AdminMemoryScope` 产出。本模块**不拿请求里的实例、人设、频道、人当授权
依据**：它们只用来拼筛选条件，范围本身来自当前身份。世界书只有「按实例」一档，没有记忆
页那两档（按域 / 按实例组），因此 `_resolve_book_scope` 比 `_resolve_admin_scope` 少了
两个参数——少两个参数也就少了两种「传错就放宽」的可能。

- 平台方可以不选实例（看全部，包括归属实例对为空的全局人设书），也可以选一个实例。
- 租户管理员必须带 `instance_key`（没有"全局"选项），先过 `assert_instance_accessible`；
  不是自己的实例一律 404。

## 按 ID 的两个异常必须逐字相同

`_load_visible_book` / `_load_visible_entry` 里「行不存在」与「行不是你的」抛同一个
`NotFoundError(resource="世界书")`，且是同一个 `raise` 语句。任何把两者拆开的写法（先判
存在再判归属、提示文字不同）都会把「这个 ID 存在」变成租户可枚举的信息。

## 为什么条目也按书判归属

`wb_entry` 没有归属列，它的可见范围永远算在书上。因此按 ID 取条目时必须连书一起取，
判的是那一行书——给条目表单独再写一套判据，两套判据迟早分叉，而分叉的那一侧不会报错。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from pydantic import ValidationError as PydanticValidationError

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.routers.presets import (
    GLOBAL_PRESET_OWNER,
    get_visible_preset,
    get_writable_preset,
    visible_presets,
    writable_presets,
)
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.memory_admin import ChannelOption, InstanceOption, PresetOption, ScopeOptionsResponse
from nekro_agent.schemas.worldbook import (
    BookCreateRequest,
    BookDetail,
    BookItem,
    BookListResponse,
    BookUpdateRequest,
    DeleteResponse,
    EntryActionRequest,
    EntryCreateRequest,
    EntryItem,
    EntryListResponse,
    EntryUpdateRequest,
    ImportReport,
    InvalidKeyItem,
    PersonNoteCreateRequest,
    PersonNoteCreateResponse,
    PersonNoteItem,
    PersonNoteListResponse,
    QuarantineItem,
    QuarantineListResponse,
    TriggerBookItem,
    TriggerDroppedItem,
    TriggerGroups,
    TriggerHitItem,
    TriggerRendered,
    TriggerTestRequest,
    TriggerTestResponse,
)
from nekro_agent.services.identity_scope import resolve_preset_id
from nekro_agent.services.memory import admin_service
from nekro_agent.services.memory.instance_scope import (
    AdminFilter,
    AdminMemoryScope,
    admin_row_visible,
    resolve_admin_scope,
)
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, get_tenant_scope
from nekro_agent.services.worldbook import render, safe_regex, st_format
from nekro_agent.services.worldbook import runtime as wb_runtime
from nekro_agent.services.worldbook import selection as wb_selection
from nekro_agent.services.worldbook import service as wb_service
from nekro_agent.services.worldbook.types import WindowMessage

router = APIRouter(prefix="/worldbook", tags=["Worldbook"])

logger = get_sub_logger("worldbook.admin")

# 触发测试取最近多少条消息当窗口。与记忆页的窗口口径一致，管理员在两边看到的是同一段
# 对话；再长就超出「最近一轮」的直觉，再短则看不出群聊里的来回。
_RECENT_WINDOW = 50

# 导出文件名。带上书 ID 是为了同一个浏览器下载多本时不会互相覆盖。
_EXPORT_FILENAME = "worldbook-{book_id}.json"

# 触发测试渲染时用的一次性令牌。真实的令牌每轮由运行时生成（`one_time_code`），测试页没有
# 那一轮，因此用一个固定的占位串：它的作用是让渲染形状与正式提示词一致（租户块要靠它包
# 起来），而不是保密——这里渲染出来的文字不会进任何一轮提示词。
_TEST_TOKEN = "SESSION_TOKEN"

# 租户对平台方提供的书做写操作时的说明。只读开放的书他看得到，这里说清为什么不能改。
_READ_ONLY_DETAIL = "这本书由平台方提供，只读：不能修改、删除、导出或追加导入"


async def _resolve_book_scope(
    user: DBUser,
    *,
    instance_key: str,
    adapter_key: str,
) -> tuple[AdminMemoryScope, Optional[DBAdapterInstance]]:
    """当前身份能看哪些实例，以及请求选中的那个实例行。

    返回实例行是因为按人筛选与建书都要用实例行上的 `adapter_key`：请求里的 `adapter_key`
    可能为空，而空串会把别名展开解析到错误的域。

    **前置判定写在这里而不是服务层**：契约用例只认路由模块里看得见的守卫。租户不带实例
    时与好感页一样报参数错误，而不是退化成"看全部"。
    """
    if get_tenant_scope(user) is not None and not instance_key.strip():
        raise ValidationError(reason="租户身份必须指定实例作用域")

    instance: Optional[DBAdapterInstance] = None
    if instance_key.strip():
        # `adapter_key` 用关键字传：同名实例可以落在两个适配器下，只按标识查会取错行。
        instance = await assert_instance_accessible(user, instance_key, adapter_key=adapter_key)

    scope = await resolve_admin_scope(user, instance_key=instance_key, adapter_key=adapter_key)
    return scope, instance


async def _load_visible_book(user: DBUser, book_id: int) -> DBWbBook:
    """按 ID 取一本**可写**的书并判归属；不存在与不可见抛同一个异常。

    写接口（改、删、导出、追加导入、建条目）都走这里。`get_tenant_scope` 的显式调用是这条
    守卫的入口：平台方在这里直接放行，租户则继续走 `admin_row_visible`。归属实例对为空的书
    （全局人设书）对租户不可写——`admin_row_visible` 对空 `adapter_key` 本来就返回假；其中
    对他只读开放的那一类（见 `wb_service.platform_books_readable_by`）回 403 而不是 404：
    他在列表里本来就看得到这本书，404 只会让人以为书没了。
    """
    book = await wb_service.load_book(book_id)
    if book is None:
        raise NotFoundError(resource="世界书")
    if get_tenant_scope(user) is None:
        return book
    if not await admin_row_visible(user, book.adapter_key, book.instance_key):
        if await wb_service.platform_books_readable_by(user, [book]):
            raise PermissionDeniedError(detail=_READ_ONLY_DETAIL)
        raise NotFoundError(resource="世界书")
    return book


async def _load_readable_book(user: DBUser, book_id: int) -> tuple[DBWbBook, bool]:
    """按 ID 取一本**可读**的书，返回（书, 是否只读）；不存在与不可见抛同一个异常。

    只给读接口（详情、条目列表）用。比 `_load_visible_book` 多放开一类：平台方给全局人设
    建、人设对这个租户的实例可见的书，只读（所有者 2026-11-10 定）。
    """
    book = await wb_service.load_book(book_id)
    if book is None:
        raise NotFoundError(resource="世界书")
    if get_tenant_scope(user) is None:
        return book, False
    if await admin_row_visible(user, book.adapter_key, book.instance_key):
        return book, False
    if await wb_service.platform_books_readable_by(user, [book]):
        return book, True
    raise NotFoundError(resource="世界书")


async def _load_visible_entry(user: DBUser, entry_id: int) -> DBWbEntry:
    """按 ID 取一条**可写**的条目，并按它所属的书判归属；不存在与不可见抛同一个异常。

    只读开放的书里的条目回 403，理由同 `_load_visible_book`。
    """
    entry = await wb_service.load_entry(entry_id)
    if entry is None:
        raise NotFoundError(resource="世界书")
    if get_tenant_scope(user) is None:
        return entry
    if not await admin_row_visible(user, entry.book.adapter_key, entry.book.instance_key):
        if await wb_service.platform_books_readable_by(user, [entry.book]):
            raise PermissionDeniedError(detail=_READ_ONLY_DETAIL)
        raise NotFoundError(resource="世界书")
    return entry


async def _load_visible_channel(user: DBUser, chat_key: str) -> DBChatChannel:
    """按 `chat_key` 取频道并判归属；不存在与不可见抛同一个异常。

    `chat_key` 在频道表上只有索引、没有唯一约束（唯一的是适配器、实例、频道 ID 三元组），
    因此同一 `chat_key` 可能有多行。租户必须对**每一行**都有权限才放行，否则按不可见处理
    （fail-closed）；取第一行时按 ID 排序，结果不随数据库返回顺序变化。
    """
    channels = await DBChatChannel.filter(chat_key=chat_key).order_by("id").all()
    if not channels:
        raise NotFoundError(resource="世界书")
    if get_tenant_scope(user) is None:
        return channels[0]
    for channel in channels:
        if not await admin_row_visible(user, channel.adapter_key, channel.instance_key):
            raise NotFoundError(resource="世界书")
    return channels[0]


async def _resolve_owner_for_create(
    user: DBUser,
    req: BookCreateRequest,
    instance: Optional[DBAdapterInstance],
) -> tuple[str, str]:
    """建书时的归属实例对。

    四种作用域各自的判据不同，但都收敛到「这本书落在哪个实例对下」：`domain` / `person`
    只能靠请求选中的实例，`channel` 跟频道走，`preset` 跟人设走。把归属与作用域绑定分开
    算，是因为平台方可以给全局人设建书（归属为空），而那种书在运行时只对 `preset` 一条腿
    生效——它不该被顺手算成某个实例的书。
    """
    if req.scope_kind in ("domain", "person"):
        if instance is None:
            # 归属为空只对全局人设书成立，域书与个人书没有「全局」形态：它们的条件腿都要求
            # 归属实例对在域里，空归属永远选不中，建出来就是一本永不生效的书。
            raise ValidationError(reason="请先选择实例")
        return (instance.adapter_key, instance.instance_key)

    if req.scope_kind == "channel":
        if not req.chat_key.strip():
            # 先于查库判空：空标识查不到频道会落成 404，管理员只是忘了选频道。
            raise ValidationError(reason="频道书必须指定频道")
        channel = await _load_visible_channel(user, req.chat_key.strip())
        rows = await DBChatChannel.filter(chat_key=channel.chat_key).order_by("id").all()
        pairs = {(row.adapter_key, row.instance_key) for row in rows}
        if len(pairs) > 1:
            # 同一个 `chat_key` 落在多个实例下时，书绑的是哪一个无法确定。与其猜一个，
            # 不如让管理员先把频道收拾干净——猜错的方向是「书在别的租户的群里生效」。
            raise ValidationError(reason="这个频道同时属于多个实例，无法确定归属")
        owner = (channel.adapter_key, channel.instance_key)
        if instance is not None and owner != (instance.adapter_key, instance.instance_key):
            raise NotFoundError(resource="世界书")
        return owner

    # 只剩 `preset`：人设书必须有一个人设。
    if req.preset_id <= 0:
        raise ValidationError(reason="人设书必须选择一个人设")
    if instance is not None:
        if get_tenant_scope(user) is None:
            # 平台方选了实例：人设必须对该实例可见，但不要求可写（平台方本来就管着人设库）。
            await get_visible_preset(req.preset_id, instance.instance_key)
        else:
            # 租户：全局人设 403、别的实例的人设 404，判据全在 `get_writable_preset` 里。
            await get_writable_preset(req.preset_id, instance.instance_key)
        return (instance.adapter_key, instance.instance_key)

    preset = await get_visible_preset(req.preset_id, "")
    if preset.owner_instance_key != GLOBAL_PRESET_OWNER:
        raise ValidationError(reason="这个人设属于某个实例，请先选择实例")
    return ("", "")


def _book_item(
    book: DBWbBook,
    counts: tuple[int, int],
    labels: tuple[str, str, bool],
    *,
    read_only: bool = False,
) -> BookItem:
    entry_count, quarantined_count = counts
    preset_name, channel_name, binding_missing = labels
    return BookItem(
        id=book.id,
        name=book.name,
        description=book.description or "",
        scope_kind=book.scope_kind,
        adapter_key=book.adapter_key,
        instance_key=book.instance_key,
        preset_id=book.preset_id,
        preset_name=preset_name,
        chat_key=book.chat_key,
        channel_name=channel_name,
        person_account=book.person_account,
        enabled=book.enabled,
        scan_depth=book.scan_depth,
        budget_chars=book.budget_chars,
        case_sensitive=book.case_sensitive,
        match_whole_words=book.match_whole_words,
        entry_count=entry_count,
        quarantined_count=quarantined_count,
        update_time=book.update_time,
        binding_missing=binding_missing,
        read_only=read_only,
    )


def _entry_item(entry: DBWbEntry, disabled: dict[int, list[str]]) -> EntryItem:
    """条目行转响应；`ext` 只摘出两个有名字的字段

    无效关键词来自导入（`ext.na_invalid_keys`），熔断关键词来自进程内的正则执行器。两者
    都是「这条条目上有几个关键词现在跑不了」，分开列出管理员才知道该改哪一个。
    """
    invalid: list[InvalidKeyItem] = []
    ext = entry.ext if isinstance(entry.ext, dict) else {}
    raw_invalid = ext.get("na_invalid_keys")
    if isinstance(raw_invalid, list):
        for item in raw_invalid:
            if isinstance(item, dict) and isinstance(item.get("key"), str):
                invalid.append(InvalidKeyItem(key=item["key"], reason=str(item.get("reason", ""))))
    return EntryItem(
        id=entry.id,
        book_id=entry.book_id,
        uid=entry.uid,
        comment=entry.comment or "",
        keys=list(entry.keys or []),
        secondary_keys=list(entry.secondary_keys or []),
        selective_logic=entry.selective_logic,
        use_regex=entry.use_regex,
        constant=entry.constant,
        content=entry.content,
        order=entry.order,
        position=entry.position,
        depth=entry.depth,
        scan_depth=entry.scan_depth,
        case_sensitive=entry.case_sensitive,
        match_whole_words=entry.match_whole_words,
        sticky=entry.sticky,
        cooldown=entry.cooldown,
        delay=entry.delay,
        enabled=entry.enabled,
        trusted=entry.trusted,
        status=entry.status,
        quarantined_at=entry.quarantined_at,
        quarantine_reason=entry.quarantine_reason or "",
        blocked_round_hits=entry.blocked_round_hits,
        create_time=entry.create_time,
        update_time=entry.update_time,
        invalid_keys=invalid,
        slow_disabled_keys=disabled.get(entry.id, []),
    )


def _disabled_by_entry() -> dict[int, list[str]]:
    """把「（条目, 关键词）→ 原因」按条目聚一次

    逐条调 `disabled_regex_keys()` 会在每次列表里把整张表拷一遍；这里只拷一次。
    """
    grouped: dict[int, list[str]] = {}
    for (entry_id, key), _reason in safe_regex.disabled_regex_keys().items():
        grouped.setdefault(entry_id, []).append(key)
    for keys in grouped.values():
        keys.sort()
    return grouped


def _form_errors(exc: PydanticValidationError) -> str:
    """把表单校验错误压成一行给管理员看：字段名加原因"""
    parts = []
    for error in exc.errors():
        field = ".".join(str(item) for item in error.get("loc", ()))
        parts.append(f"{field}: {error.get('msg', '')}" if field else str(error.get("msg", "")))
    return "；".join(parts) or "表单字段不合法"


async def _read_upload(file: UploadFile) -> st_format.ParsedWorld:
    """读上传文件并解析；超限与解析失败都按 400 处理

    多读一个字节再判长度：`UploadFile` 的 `size` 在分块上传时可能为空，只信它会让一个
    超大文件先被整个读进内存。多读的那一个字节足够判断「超了没有」，也不必读完整个文件。
    """
    data = await file.read(st_format.IMPORT_MAX_BYTES + 1)
    if len(data) > st_format.IMPORT_MAX_BYTES:
        raise ValidationError(reason=f"文件超过 {st_format.IMPORT_MAX_BYTES // (1024 * 1024)} MiB，整份未导入")
    try:
        return st_format.parse_world_file(data)
    except st_format.WorldFileRejected as exc:
        # 原因原样回给管理员：他知道自己那份文件哪里不对，改写一句通用文案只会让它更难查。
        raise ValidationError(reason=str(exc)) from exc


@router.get("/scope-options", summary="世界书筛选项", response_model=ScopeOptionsResponse)
@require_role(Role.Admin)
async def get_scope_options(
    instance_key: str = "",
    adapter_key: str = "",
    _current_user: DBUser = Depends(get_current_active_user),
) -> ScopeOptionsResponse:
    """筛选栏能选哪些实例、频道、人设；选项本身也按作用域裁剪。

    实例与频道沿用记忆页的口径；人设不能沿用：记忆页列的是「记忆里出现过的人设」，一个
    还没有任何记忆的实例在那里一个人设都没有，建人设书时就无从选起。这里列的是「能拿来
    建书的人设」，判据与建书时的 `_resolve_owner_for_create` 一致：租户只能用自己实例的
    人设（全局人设建书是 403），所以只列这些；平台方选了实例时列这个实例看得见的（全局 +
    这个实例的），不选实例时列全部。
    """
    scope, instance = await _resolve_book_scope(_current_user, instance_key=instance_key, adapter_key=adapter_key)
    options = await admin_service.scope_options(_current_user, scope)
    scoped_instance_key = instance.instance_key if instance is not None else ""
    preset_query = (
        visible_presets(scoped_instance_key)
        if get_tenant_scope(_current_user) is None
        else writable_presets(scoped_instance_key)
    )
    preset_rows = await preset_query.order_by("id").values_list("id", "title", "name")
    return ScopeOptionsResponse(
        instances=[InstanceOption.model_validate(item) for item in options["instances"]],
        channels=[ChannelOption.model_validate(item) for item in options["channels"]],
        presets=[
            PresetOption(preset_id=int(preset_id), name=str(title or name or f"#{preset_id}"))
            for preset_id, title, name in preset_rows
        ],
    )


@router.get("/books", summary="世界书列表", response_model=BookListResponse)
@require_role(Role.Admin)
async def list_books(
    scope_kind: str = "",
    preset_id: Optional[int] = None,
    chat_key: str = "",
    person: str = "",
    enabled: Optional[bool] = None,
    q: str = "",
    sort: str = "update_time",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
    instance_key: str = "",
    adapter_key: str = "",
    _current_user: DBUser = Depends(get_current_active_user),
) -> BookListResponse:
    """书列表。

    按人筛选走别名展开（`resolve_person_filter`）：同一本书绑的可能是那个人的副号，只按
    管理员输入的那一个账号精确匹配会让他在列表里找不到自己刚建的书。
    """
    scope, instance = await _resolve_book_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
    )
    person_accounts: Optional[frozenset[str]] = None
    if person.strip():
        person_accounts = await admin_service.resolve_person_filter(
            scope,
            person,
            adapter_key=instance.adapter_key if instance is not None else "",
            instance_key=instance.instance_key if instance is not None else "",
        )
    f = AdminFilter(
        chat_key=chat_key.strip() or None,
        preset_id=preset_id,
        person_accounts=person_accounts,
    )
    # 租户的列表并入平台方给全局人设建、人设对这个实例可见的书（只读）；平台方本来就看全部
    is_tenant = get_tenant_scope(_current_user) is not None
    readable_for = (
        (_current_user, instance.instance_key) if is_tenant and instance is not None else None
    )
    total, rows = await wb_service.list_books(
        scope,
        f,
        scope_kind=scope_kind,
        enabled=enabled,
        q=q,
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        readable_for=readable_for,
    )
    counts = await wb_service.book_counts([row.id for row in rows])
    labels = await wb_service.binding_labels(rows)
    read_only = (
        await wb_service.platform_books_readable_by(
            _current_user,
            [row for row in rows if not row.adapter_key and not row.instance_key],
            instance_key=instance.instance_key if instance is not None else None,
        )
        if is_tenant
        else set()
    )
    return BookListResponse(
        total=total,
        items=[
            _book_item(
                row,
                counts.get(row.id, (0, 0)),
                labels.get(row.id, ("", "", False)),
                read_only=row.id in read_only,
            )
            for row in rows
        ],
    )


@router.post("/books", summary="新建世界书", response_model=BookDetail)
@require_role(Role.Admin)
async def create_book(
    req: BookCreateRequest,
    instance_key: str = "",
    adapter_key: str = "",
    _current_user: DBUser = Depends(get_current_active_user),
) -> BookDetail:
    """建书；归属由 `_resolve_owner_for_create` 按作用域类型定。"""
    _, instance = await _resolve_book_scope(_current_user, instance_key=instance_key, adapter_key=adapter_key)
    owner = await _resolve_owner_for_create(_current_user, req, instance)
    book = await wb_service.create_book(owner, req, username=_current_user.username)
    labels = await wb_service.binding_labels([book])
    return BookDetail(
        **_book_item(book, (0, 0), labels.get(book.id, ("", "", False))).model_dump(),
        ext_keys=sorted(book.ext) if isinstance(book.ext, dict) else [],
    )


@router.post("/books/import", summary="导入世界书文件（新建书）", response_model=ImportReport)
@require_role(Role.Admin)
async def import_new_book(
    file: UploadFile = File(...),
    name: str = Form(default=""),
    description: str = Form(default=""),
    scope_kind: str = Form(default="domain"),
    preset_id: int = Form(default=0),
    chat_key: str = Form(default=""),
    person_account: str = Form(default=""),
    enabled: bool = Form(default=True),
    scan_depth: Optional[int] = Form(default=None),
    budget_chars: int = Form(default=0),
    case_sensitive: bool = Form(default=False),
    match_whole_words: bool = Form(default=False),
    instance_key: str = "",
    adapter_key: str = "",
    _current_user: DBUser = Depends(get_current_active_user),
) -> ImportReport:
    """导入成一本新书：先按建书规则定归属，再在一个事务里建书写条目。

    表单字段与 `BookCreateRequest` 一一对应而不是收一段 JSON：前端用 `<form>` 直接传文件
    与字段，不必先拼一份 JSON 再塞进 multipart 的一个字段里。
    """
    _, instance = await _resolve_book_scope(_current_user, instance_key=instance_key, adapter_key=adapter_key)
    try:
        req = BookCreateRequest.model_validate(
            {
                "name": name,
                "description": description,
                "scope_kind": scope_kind,
                "preset_id": preset_id,
                "chat_key": chat_key,
                "person_account": person_account,
                "enabled": enabled,
                "scan_depth": scan_depth,
                "budget_chars": budget_chars,
                "case_sensitive": case_sensitive,
                "match_whole_words": match_whole_words,
            },
        )
    except PydanticValidationError as exc:
        # 表单字段是在函数体里才拼成请求对象的，FastAPI 的 422 处理够不着这里：不接住的话
        # 书名为空这种普通输入错误会变成 500。
        raise ValidationError(reason=_form_errors(exc)) from exc
    owner = await _resolve_owner_for_create(_current_user, req, instance)
    parsed = await _read_upload(file)
    return await wb_service.import_new_book(
        owner,
        req,
        parsed,
        is_platform=get_tenant_scope(_current_user) is None,
        username=_current_user.username,
    )


@router.get("/books/{book_id}", summary="世界书详情", response_model=BookDetail)
@require_role(Role.Admin)
async def get_book(
    book_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> BookDetail:
    """书详情；按 ID 取，不存在与不可见同一段文字。平台方提供的只读书带 `read_only`。"""
    book, read_only = await _load_readable_book(_current_user, book_id)
    counts = await wb_service.book_counts([book.id])
    labels = await wb_service.binding_labels([book])
    return BookDetail(
        **_book_item(
            book,
            counts.get(book.id, (0, 0)),
            labels.get(book.id, ("", "", False)),
            read_only=read_only,
        ).model_dump(),
        ext_keys=sorted(book.ext) if isinstance(book.ext, dict) else [],
    )


@router.put("/books/{book_id}", summary="修改世界书设置", response_model=BookDetail)
@require_role(Role.Admin)
async def update_book(
    book_id: int,
    req: BookUpdateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> BookDetail:
    """改书设置；作用域与绑定对象不在可改字段里（改作用域等于换一本书）。"""
    book = await _load_visible_book(_current_user, book_id)
    await wb_service.update_book(book, req, username=_current_user.username)
    counts = await wb_service.book_counts([book.id])
    labels = await wb_service.binding_labels([book])
    return BookDetail(
        **_book_item(book, counts.get(book.id, (0, 0)), labels.get(book.id, ("", "", False))).model_dump(),
        ext_keys=sorted(book.ext) if isinstance(book.ext, dict) else [],
    )


@router.delete("/books/{book_id}", summary="删除世界书", response_model=DeleteResponse)
@require_role(Role.Admin)
async def delete_book(
    book_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> DeleteResponse:
    """删书并连带删条目，返回删掉的条目数。"""
    book = await _load_visible_book(_current_user, book_id)
    deleted_entries = await wb_service.delete_book(book)
    return DeleteResponse(deleted_entries=deleted_entries)


@router.get("/books/{book_id}/entries", summary="世界书条目列表", response_model=EntryListResponse)
@require_role(Role.Admin)
async def list_entries(
    book_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> EntryListResponse:
    """一本书的全部条目；权重大的在前，同权重按创建顺序。平台方提供的只读书也能看。"""
    book, _read_only = await _load_readable_book(_current_user, book_id)
    entries = await wb_service.list_entries(book)
    disabled = _disabled_by_entry()
    return EntryListResponse(items=[_entry_item(entry, disabled) for entry in entries])


@router.post("/books/{book_id}/entries", summary="新建条目", response_model=EntryItem)
@require_role(Role.Admin)
async def create_entry(
    book_id: int,
    req: EntryCreateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> EntryItem:
    """建条目；`trusted` 由创建者身份决定，请求里带的一律忽略。"""
    book = await _load_visible_book(_current_user, book_id)
    entry = await wb_service.create_entry(
        book,
        req,
        is_platform=get_tenant_scope(_current_user) is None,
        username=_current_user.username,
    )
    return _entry_item(entry, _disabled_by_entry())


@router.put("/entries/{entry_id}", summary="修改条目", response_model=EntryItem)
@require_role(Role.Admin)
async def update_entry(
    entry_id: int,
    req: EntryUpdateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> EntryItem:
    """改条目；隔离状态不因编辑改变，租户改过的条目一律变成不可信。"""
    entry = await _load_visible_entry(_current_user, entry_id)
    await wb_service.update_entry(
        entry,
        req,
        is_platform=get_tenant_scope(_current_user) is None,
        username=_current_user.username,
    )
    return _entry_item(entry, _disabled_by_entry())


@router.delete("/entries/{entry_id}", summary="删除条目", response_model=DeleteResponse)
@require_role(Role.Admin)
async def delete_entry(
    entry_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> DeleteResponse:
    """删一条条目；返回体与删书共用，这个端点删的永远是单条，所以恒为 1。"""
    entry = await _load_visible_entry(_current_user, entry_id)
    await wb_service.delete_entry(entry)
    return DeleteResponse(deleted_entries=1)


@router.post("/entries/{entry_id}/action", summary="条目隔离 / 放回 / 启用 / 停用", response_model=EntryItem)
@require_role(Role.Admin)
async def entry_action(
    entry_id: int,
    req: EntryActionRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> EntryItem:
    """四种状态动作走同一个端点；已经是目标状态时不报错。"""
    entry = await _load_visible_entry(_current_user, entry_id)
    await wb_service.apply_entry_action(entry, req.action)
    return _entry_item(entry, _disabled_by_entry())


@router.get("/quarantine", summary="世界书隔离区", response_model=QuarantineListResponse)
@require_role(Role.Admin)
async def list_quarantine(
    page: int = 1,
    page_size: int = 20,
    instance_key: str = "",
    adapter_key: str = "",
    _current_user: DBUser = Depends(get_current_active_user),
) -> QuarantineListResponse:
    """范围内的书里处于隔离中的条目，按隔离时间倒序。"""
    scope, _ = await _resolve_book_scope(_current_user, instance_key=instance_key, adapter_key=adapter_key)
    total, rows = await wb_service.list_quarantine(scope, page=page, page_size=page_size)
    return QuarantineListResponse(
        total=total,
        items=[
            QuarantineItem(
                entry_id=row.id,
                book_id=row.book_id,
                book_name=row.book.name,
                comment=row.comment or "",
                quarantine_reason=row.quarantine_reason or "",
                blocked_round_hits=row.blocked_round_hits,
                quarantined_at=row.quarantined_at,
            )
            for row in rows
        ],
    )


@router.post("/books/{book_id}/import", summary="追加导入世界书文件", response_model=ImportReport)
@require_role(Role.Admin)
async def import_into_book(
    book_id: int,
    file: UploadFile = File(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ImportReport:
    """追加到已有书：编号撞号的条目重编号，书级 `ext` 保持不动。"""
    book = await _load_visible_book(_current_user, book_id)
    parsed = await _read_upload(file)
    return await wb_service.import_world(
        book,
        parsed,
        is_platform=get_tenant_scope(_current_user) is None,
        username=_current_user.username,
    )


@router.get("/books/{book_id}/export", summary="导出为酒馆世界书 JSON")
@require_role(Role.Admin)
async def export_book(
    book_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> Response:
    """导出成酒馆世界书格式；`ext` 逐字段还原，导出再导入不丢内容。"""
    book = await _load_visible_book(_current_user, book_id)
    data = await wb_service.export_book(book)
    # 用 `json.dumps` 而不是让 FastAPI 序列化：这里要自己控制 `Content-Disposition`，
    # 而且导出结果里可能有非 ASCII 内容，`ensure_ascii=False` 让文件在编辑器里可读。
    payload = json.dumps(data, ensure_ascii=False)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{_EXPORT_FILENAME.format(book_id=book.id)}"'},
    )


@router.post("/person-notes", summary="给某个人记一笔", response_model=PersonNoteCreateResponse)
@require_role(Role.Admin)
async def create_person_note(
    req: PersonNoteCreateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PersonNoteCreateResponse:
    """把一句备注落成这个人的个人书里一条常驻条目。

    ## 必须选实例

    个人书的生效条件是「这个人出现在这个实例下这个频道的最近消息里」，它的归属实例对
    不能为空。平台方不选实例时范围是全量，写进去的书会落在一个没有实例对的归属上，
    而那样的书在运行时一条腿都选不中——建出来就是一本永不生效的书。因此这里与好感页
    同一口径：没选实例报参数错误，而不是含糊地替他挑一个。

    ## 归属只从实例行取

    请求里的 `adapter_key` 可能为空（同名实例可以落在两个适配器下），范围解析用的是
    `_resolve_book_scope`，与列表页同一个入口；服务层再按实例对精确过滤一遍，所以
    平台方即使持有全量范围，也只会写进这个实例的那本书里。
    """
    scope, instance = await _resolve_book_scope(
        _current_user,
        instance_key=req.instance_key,
        adapter_key=req.adapter_key,
    )
    if instance is None:
        raise ValidationError(reason="请先选择实例")
    book, entry, created_book = await wb_service.add_person_note(
        scope,
        (instance.adapter_key, instance.instance_key),
        req.person_account,
        req.content,
        is_platform=get_tenant_scope(_current_user) is None,
        username=_current_user.username,
    )
    logger.info(
        f"记一笔: adapter={instance.adapter_key}, instance={instance.instance_key}, "
        f"person={req.person_account}, book={book.id}, entry={entry.id}, 新建书={created_book}",
    )
    return PersonNoteCreateResponse(book_id=book.id, entry_id=entry.id, created_book=created_book)


@router.get("/person-notes", summary="某个人的已有备注", response_model=PersonNoteListResponse)
@require_role(Role.Admin)
async def list_person_notes(
    instance_key: str = "",
    adapter_key: str = "",
    person_account: str = "",
    _current_user: DBUser = Depends(get_current_active_user),
) -> PersonNoteListResponse:
    """好感详情里这一栏：这个人在个人书里的全部条目，含停用与隔离中的。

    与记一笔同一组守卫（身份 → 范围 → 实例）：范围外的书里的条目不出现，因此别人的实例
    给同一个人记的备注在这里读不到。停用与隔离中的条目照常列出，理由见服务层同名函数。
    """
    scope, instance = await _resolve_book_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
    )
    account = person_account.strip()
    if not account:
        raise ValidationError(reason="必须指定一个人")
    if instance is None:
        raise ValidationError(reason="请先选择实例")
    rows = await wb_service.list_person_notes(
        scope,
        (instance.adapter_key, instance.instance_key),
        account,
    )
    return PersonNoteListResponse(
        items=[
            PersonNoteItem(
                book_id=book.id,
                book_name=book.name,
                entry_id=entry.id,
                comment=entry.comment or "",
                content=entry.content,
                enabled=entry.enabled,
                status=entry.status,
                trusted=entry.trusted,
                update_time=entry.update_time,
            )
            for book, entry in rows
        ],
    )


async def _test_window(
    channel: DBChatChannel,
    req: TriggerTestRequest,
) -> list[WindowMessage]:
    """触发测试的扫描窗口

    `text` 非空就用它造一条合成消息（管理员想看「这句话会触发什么」）；否则按
    `use_recent` 取这个频道最近的消息。合成消息的 `id` 固定为 0：它必须排在真实消息
    之前，这样 `scan_text` 的「最后 N 条」才会把它算进窗口。
    """
    if req.text.strip():
        return [WindowMessage(id=0, sender_id="test", platform_userid="", content_text=req.text)]
    if not req.use_recent:
        return []
    rows = await DBChatMessage.filter(chat_key=channel.chat_key).order_by("-id").limit(_RECENT_WINDOW)
    # 倒序取最近 N 条再翻回升序：`scan_text` 与 `book_active_for_person` 都按 `id` 升序
    # 理解「最后几条」，直接把倒序列表传进去会让「最后 depth 条」变成「最早 depth 条」。
    return [
        WindowMessage(
            id=row.id,
            sender_id=row.sender_id,
            platform_userid=row.platform_userid,
            content_text=row.content_text,
        )
        for row in reversed(rows)
    ]


@router.post("/test", summary="触发测试", response_model=TriggerTestResponse)
@require_role(Role.Admin)
async def test_trigger(
    req: TriggerTestRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> TriggerTestResponse:
    """在给定频道上算一遍「这一轮会注入什么」，不调模型、不写库、不推进定时状态。

    与运行时相比有几处刻意的差异，都写进响应或这里：

    - 定时效果只算 `delay`（用窗口条数判），锚点一律当作没有：测试页不该改变正式状态，
      也不该因为上一次测试而给出不同的结论；
    - 窗口是最近 50 条或一条合成消息，不是运行时那一轮的真实窗口；合成消息没有平台
      账号，个人书在那种窗口里不会生效；
    - 预算读这个频道的**生效**配置（`get_effective_config()`），与运行时同一份入口：
      管理员在测试页看到的「被预算截掉」必须与正式那一轮一致，否则测试页会在配置改动
      之后给出一个不存在的结论。条数上限是平台方全局项，不分频道。
    - `gate_enabled` 报这个频道此刻的闸状态。闸关时结果照算：管理员常常是先看效果再
      决定要不要打开，直接把结果清空会让他以为书有问题。
    - `rendered` 是三个位置渲染出的文字，令牌用固定占位串。租户身份下，条目所在的书
      对他不可见时正文换成固定占位文字——书名与标题照常返回，否则「有没有这本书」会变成
      可以试出来的信息。平台方给全局人设建、人设对他可见的书是只读开放的，正文照常给
      （判据见 `wb_service.books_invisible_to`）。
    """
    channel = await _load_visible_channel(_current_user, req.chat_key)
    if req.preset_id is not None and req.preset_id > 0:
        # 请求指定的人设必须对这个频道的实例可见：不查的话，随手填人设 ID 就能逐个试出
        # 全局人设书的书名与条目标题，哪怕这个实例从来没用过那个人设。
        await get_visible_preset(req.preset_id, channel.instance_key)
        preset_id = req.preset_id
    elif req.preset_id is not None:
        preset_id = req.preset_id
    else:
        preset_id = await resolve_preset_id(channel)

    channel_config = await channel.get_effective_config()
    window = await _test_window(channel, req)
    # 与运行时同一条求值路径（`runtime.evaluate`），只是不带定时锚点、不做下一轮抑制：
    # 测试页不该改变正式状态，也不该因为上一次测试而给出不同的结论。
    evaluation = await wb_runtime.evaluate(
        channel,
        preset_id=preset_id,
        window=window,
        max_chars=int(channel_config.WORLDBOOK_MAX_CHARS),
        max_entries=int(config.WORLDBOOK_MAX_ENTRIES_PER_ROUND),
        use_timed_state=False,
        suppress_untrusted=False,
    )
    books = evaluation.books
    entries = evaluation.entries
    hits = list(evaluation.hits)
    chosen = evaluation.chosen
    groups = evaluation.groups
    channel_count = len(window)

    comment_by_id = {entry.id: entry.comment or "" for entry in entries}

    # 可信 / 不可信分开数：这两个数回答的是「这本书里有多少条会被当成宿主内容注入」，
    # 而触发测试的响应刻意不带正文，管理员只能靠这两个数判断。
    trusted_counts: dict[int, int] = {}
    total_counts: dict[int, int] = {}
    for entry in entries:
        total_counts[entry.book_id] = total_counts.get(entry.book_id, 0) + 1
        if entry.trusted:
            trusted_counts[entry.book_id] = trusted_counts.get(entry.book_id, 0) + 1

    hidden_books = await wb_service.books_invisible_to(_current_user, books)
    return TriggerTestResponse(
        books=[
            TriggerBookItem(
                id=book.id,
                name=book.name,
                scope_kind=book.scope_kind,
                trusted_entries=trusted_counts.get(book.id, 0),
                untrusted_entries=total_counts.get(book.id, 0) - trusted_counts.get(book.id, 0),
            )
            for book in books
        ],
        hits=[
            TriggerHitItem(
                entry_id=hit.entry.id,
                book_id=hit.entry.book_id,
                comment=comment_by_id.get(hit.entry.id, ""),
                reason_kind=hit.reason.kind,
                matched_key=hit.reason.key,
                position=hit.entry.position.value,
                order=hit.entry.order,
                chars=len(hit.entry.content),
                trusted=hit.entry.trusted,
            )
            for hit in hits
        ],
        selected=[hit.entry.id for hit in chosen.selected],
        dropped=[
            TriggerDroppedItem(entry_id=hit.entry.id, reason=reason) for hit, reason in chosen.dropped
        ],
        groups=TriggerGroups(
            after_persona=[hit.entry.id for hit in groups.after_persona],
            before_history=[hit.entry.id for hit in groups.before_history],
            at_depth={str(depth): [hit.entry.id for hit in items] for depth, items in groups.at_depth.items()},
        ),
        window_size=channel_count,
        timed_ignored=True,
        gate_enabled=bool(channel_config.WORLDBOOK_ENABLED),
        rendered=_render_for_test(groups, hidden_books),
    )


async def _books_invisible_to(user: DBUser, books: list[DBWbBook]) -> set[int]:
    """兼容旧名：判据已收口到服务层，避免两处各写一份"""
    return await wb_service.books_invisible_to(user, books)


def _render_for_test(
    groups: wb_selection.RenderGroups,
    hidden_books: set[int],
) -> TriggerRendered:
    """把三个位置渲染成文字，令牌用固定占位串

    租户看不到的书里的条目，正文换成固定占位文字：触发测试的响应本来就不带正文，但
    `rendered` 会把正文带出来，那条「响应不带正文」的性质就没了。占位文字与渲染层的
    「不可见」措辞无关，它只说明「这里有内容，但不是给你看的」。

    位置到 `in_history` 的映射与运行时一致：`after_persona` 在系统提示词里，另外两个
    在聊天记录里。写错这一处的表现是「测试页看到的分隔符与正式提示词不同」，很容易
    被当成渲染 bug 查很久。
    """
    after_persona = groups.after_persona
    before_history = groups.before_history
    if hidden_books:
        after_persona = [_hide_entry(hit, hidden_books) for hit in after_persona]
        before_history = [_hide_entry(hit, hidden_books) for hit in before_history]
        at_depth_hidden = {
            depth: [_hide_entry(hit, hidden_books) for hit in items] for depth, items in groups.at_depth.items()
        }
    else:
        at_depth_hidden = {depth: list(items) for depth, items in groups.at_depth.items()}

    return TriggerRendered(
        after_persona=render.render_block(after_persona, _TEST_TOKEN, in_history=False),
        before_history=render.render_block(before_history, _TEST_TOKEN, in_history=True),
        # 每个深度各是一段独立的块：运行时就是这么插进聊天记录的，合并成一段会让管理员
        # 看不出「这条到底落在第几条之前」。
        at_depth={
            str(depth): render.render_block(items, _TEST_TOKEN, in_history=True)
            for depth, items in at_depth_hidden.items()
        },
    )


def _hide_entry(hit: wb_selection.Hit, hidden_books: set[int]) -> wb_selection.Hit:
    """把不可见书里的条目正文换成占位文字

    `dataclasses.replace` 而不是手工重建 `Hit`：它的字段会随引擎演进增加，重建的那一份
    会在下一次加字段时悄悄丢掉新字段，而丢掉的往往是判据本身。
    """
    if hit.entry.book_id not in hidden_books:
        return hit
    return replace(hit, entry=replace(hit.entry, content=render.HIDDEN_BODY))
