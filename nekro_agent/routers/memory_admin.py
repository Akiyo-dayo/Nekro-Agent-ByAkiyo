"""记忆管理接口

## 作用域

可见范围只由 `AdminMemoryScope` 产出。本模块**不拿请求里的实例、频道、人设、人当授权依据**：
它们只用来拼筛选条件，范围本身来自当前身份。把两者分开的理由与检索侧一致——授权判据散落
到第二处实现时，漏掉的那一处不会报错，只会静默多给几行。

- 平台方可以不选实例（看全部，包括 `adapter_key` 为空的存量工作区行），也可以选一个实例。
- 租户管理员必须带 `instance_key`（没有"全局"选项），先过 `assert_instance_accessible`；
  不是自己的实例一律 404。

## 按 ID 的两个异常必须逐字相同

`_load_visible_item` 里「行不存在」与「行不是你的」抛同一个 `NotFoundError(resource="记忆")`，
且是同一个 `raise` 语句。任何把两者拆开的写法（比如先判存在再判归属、提示文字不同）都会
把「这个 ID 存在」变成租户可枚举的信息。

## 列表为什么在路由里补「涉及的人」

服务层只返回段落行；列表页要显示"涉及的人"，由路由用 `load_person_accounts` **一次**批量取。
逐行查会在每页 100 条时变成 100 次往返。显示名（来源频道里的昵称、好感里的称呼）同样批量配，
见 `services/memory/person_labels.py`。

## 分组视图

`/persons` 与 `/topics` 是只读的聚合：满足筛选的段落里出现过哪些人、按所属事件各几段。
它们与段落列表走同一个范围（`_resolve_admin_scope` + `AdminMemoryScope.apply`）和同一套
筛选参数，展开一组时用段落列表的 `person` / `episode` 参数取组内段落。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional, cast

from fastapi import APIRouter, Depends

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import DBMemParagraph, OriginKind
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.memory_admin import (
    ActionResponse,
    BulkParagraphsRequest,
    BulkResultResponse,
    ChannelOption,
    EpisodeListItem,
    EpisodeListResponse,
    HardDeleteReportResponse,
    InstanceOption,
    MemoryGraphEdge,
    MemoryGraphNode,
    MemoryGraphResponse,
    MemoryItemActionRequest,
    MemoryItemDetail,
    MemoryItemUpdateRequest,
    MemoryPersonItem,
    MemoryPersonListResponse,
    MemoryTopicItem,
    MemoryTopicListResponse,
    MemoryTraceMessage,
    MemoryTraceResponse,
    ParagraphFilterSelection,
    ParagraphListItem,
    ParagraphListResponse,
    PersonLabelItem,
    PresetOption,
    PromptRoundDetailResponse,
    PromptRoundListItem,
    PromptRoundListResponse,
    QuarantineListItem,
    QuarantineListResponse,
    ScopeOptionsResponse,
    SummaryEditRequest,
    SummaryListResponse,
    SummaryPauseRequest,
    SummaryPauseResponse,
    SummaryRollbackRequest,
    SummaryVersionItem,
    SummaryVersionsResponse,
    detail_from_payload,
)
from nekro_agent.services.memory import admin_service, channel_summary, prompt_rounds
from nekro_agent.services.memory.admin_service import (
    BulkResult,
    HardDeleteReport,
    ParagraphListFilters,
    QuarantineEntry,
)
from nekro_agent.services.memory.graph_view import GRAPH_LIMIT_DEFAULT, build_admin_memory_graph
from nekro_agent.services.memory.instance_scope import (
    AdminFilter,
    AdminMemoryScope,
    admin_row_visible,
    resolve_admin_scope,
)
from nekro_agent.services.memory.person_labels import PersonLabel, label_paragraph_persons
from nekro_agent.services.memory.persons import load_person_accounts
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, get_tenant_scope

router = APIRouter(prefix="/memory", tags=["Memory"])

logger = get_sub_logger("memory.admin")

_ALL_PARTITIONS = frozenset({"all", "domain", "workspace"})


def _parse_iso_datetime(value: str, field_name: str) -> Optional[datetime]:
    """把查询参数里的 ISO 时间收成 `datetime`；空串表示不限。"""
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValidationError(reason=f"{field_name} 不是合法的 ISO 时间: {value}") from e
    # 不带时区的时间按 UTC 解释：库里存的是带时区的时间，直接比较会因驱动不同而结果不一。
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _admin_filter(
    *,
    partition: str,
    chat_key: str,
    preset_id: Optional[int],
    person_accounts: Optional[frozenset[str]],
) -> AdminFilter:
    normalized = partition.strip().lower() or "all"
    if normalized not in _ALL_PARTITIONS:
        raise ValidationError(reason=f"未知的分区筛选: {partition}")
    return AdminFilter(
        partition=cast(Literal["all", "domain", "workspace"], normalized),
        chat_key=chat_key.strip() or None,
        preset_id=preset_id,
        person_accounts=person_accounts,
    )


def _parse_origin_kind(value: str) -> Optional[str]:
    """把来源类型筛选项收成枚举值；空串表示不筛。

    校验放在这里而不是交给数据库：写错的值到了 SQL 里只会安静地返回零行，
    前端看到的是「一条都没有」而不是「参数写错了」。
    """
    text = value.strip()
    if not text:
        return None
    try:
        return OriginKind(text).value
    except ValueError as e:
        raise ValidationError(reason=f"未知的来源类型筛选: {value}") from e


# 事件 ID 是 32 位有符号整数列：超过它的值在 PostgreSQL 上会溢出成 500，超长的数字串
# 换成整数也要白算一遍，都在这里挡成 400
_EPISODE_ID_MAX = 2**31 - 1
_EPISODE_ID_MAX_DIGITS = 10


def _parse_episode(value: str) -> Optional[int]:
    """事件筛选：空串不筛，`none` 是「没归入任何事件」，正整数是事件 ID。

    与来源类型同一个理由在这里校验：写错的值到了 SQL 里只会安静地返回零行。列表、分组视图与
    批量「按筛选全选」（请求体里的 `episode`）都走这里。
    """
    text = value.strip().lower()
    if not text:
        return None
    if text == "none":
        return admin_service.NO_EPISODE
    if (
        text.isascii()
        and text.isdigit()
        and len(text) <= _EPISODE_ID_MAX_DIGITS
        and 0 < int(text) <= _EPISODE_ID_MAX
    ):
        return int(text)
    raise ValidationError(reason=f"未知的事件筛选: {value[:32]}")


def _paragraph_filters(
    *,
    status: str,
    frozen: Optional[bool],
    protected: Optional[bool],
    cognitive_type: str,
    knowledge_type: str,
    time_from: str,
    time_to: str,
    q: str,
    origin_kind: str = "",
    episode: str = "",
    sensitive: Optional[bool] = None,
) -> ParagraphListFilters:
    return ParagraphListFilters(
        status=status.strip().lower() or "all",
        frozen=frozen,
        protected=protected,
        sensitive=sensitive,
        cognitive_type=cognitive_type.strip() or None,
        knowledge_type=knowledge_type.strip() or None,
        time_from=_parse_iso_datetime(time_from, "time_from"),
        time_to=_parse_iso_datetime(time_to, "time_to"),
        q=q.strip() or None,
        origin_kind=_parse_origin_kind(origin_kind),
        episode=_parse_episode(episode),
    )


async def _resolve_admin_scope(
    user: DBUser,
    *,
    instance_key: str,
    adapter_key: str,
    domain: str,
    group_id: Optional[int],
) -> tuple[AdminMemoryScope, Optional[DBAdapterInstance]]:
    """当前身份能看哪些实例，以及请求选中的那个实例行。

    返回实例行是因为 `/paragraphs` 的按人筛选需要实例行上的 `adapter_key`：请求里的
    `adapter_key` 可能为空，而空串会把别名展开解析到错误的域。

    **前置判定写在这里而不是服务层**：契约用例只认路由模块里看得见的守卫。租户不带实例
    时与好感页一样报参数错误，而不是退化成"看全部"。
    """
    normalized_domain = (domain or "self").strip().lower()
    if normalized_domain not in ("self", "group"):
        raise ValidationError(reason=f"未知的域筛选: {domain}")

    if get_tenant_scope(user) is not None and not instance_key.strip():
        raise ValidationError(reason="租户身份必须指定实例作用域")

    instance: Optional[DBAdapterInstance] = None
    if instance_key.strip():
        # `adapter_key` 用关键字传：同名实例可以落在两个适配器下，只按标识查会取错行。
        instance = await assert_instance_accessible(user, instance_key, adapter_key=adapter_key)

    scope = await resolve_admin_scope(
        user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=cast(Literal["self", "group"], normalized_domain),
        group_id=group_id,
    )
    return scope, instance


async def _load_visible_item(user: DBUser, memory_type: str, item_id: int) -> Any:
    """按 ID 取一行并判归属；不存在与不可见抛同一个异常。

    `get_tenant_scope` 的显式调用是这条守卫的入口：平台方在这里直接放行，租户则继续走
    `admin_row_visible`。把它留在函数体里（而不是只在被调用的服务里），契约用例才看得见。
    """
    row = await admin_service.load_item(memory_type, item_id)
    if row is None:
        raise NotFoundError(resource="记忆")
    if get_tenant_scope(user) is None:
        return row
    if not await admin_row_visible(user, row.adapter_key, row.instance_key):
        raise NotFoundError(resource="记忆")
    return row


async def _load_visible_channel(user: DBUser, chat_key: str) -> DBChatChannel:
    """按 `chat_key` 取频道并判归属；不存在与不可见抛同一个异常。

    摘要与提示词明细按频道访问，归属判据就是频道行上的 `(adapter_key, instance_key)`。
    这条守卫与按 ID 的 `_load_visible_item` 用同一段文字：租户不能靠 404 文案区分
    「这个频道不存在」和「这个频道不是你的」。

    `chat_key` 在频道表上只有索引、没有唯一约束（唯一的是适配器、实例、频道 ID 三元组），
    而摘要的读写只按 `chat_key`。万一有两行频道共用一个 `chat_key`，租户必须对**每一行**
    都有权限才放行，否则按不可见处理（fail-closed）；取第一行时按 ID 排序，结果不随数据库
    返回顺序变化。
    """
    channels = await DBChatChannel.filter(chat_key=chat_key).order_by("id").all()
    if not channels:
        raise NotFoundError(resource="记忆")
    if get_tenant_scope(user) is None:
        return channels[0]
    for channel in channels:
        if not await admin_row_visible(user, channel.adapter_key, channel.instance_key):
            raise NotFoundError(resource="记忆")
    return channels[0]


def _summary_version_item(row: DBMemChannelSummary) -> SummaryVersionItem:
    return SummaryVersionItem(
        id=row.id,
        version=row.version,
        content=row.content,
        source=row.source,
        source_version=row.source_version,
        edited_by=row.edited_by,
        create_time=row.create_time,
    )


def _worldbook_injected_count(raw: Any) -> int:
    """明细行里真正注入过的世界书条目数

    `worldbook_items` 里除了注入的条目，还有被预算跳过的（`type = worldbook_dropped`）。
    列表页那一栏说的是「注入了几条」，把跳过的也算进去会让两栏对不上——因此这里只数
    `type == "worldbook"`，而不是「不是 dropped 的都算」：未来说不定还有第三种记录。
    """
    if not isinstance(raw, list):
        return 0
    return sum(1 for item in raw if isinstance(item, dict) and item.get("type") == "worldbook")


def _worldbook_injected_tokens(raw: Any) -> Optional[int]:
    """明细行里注入条目的 token 总数；0.4.7 之前的旧行条目上没有 `tokens`，返回 None（界面按字数显示）"""
    if not isinstance(raw, list):
        return None
    injected = [item for item in raw if isinstance(item, dict) and item.get("type") == "worldbook"]
    if not injected or any(not isinstance(item.get("tokens"), int) for item in injected):
        return None
    return sum(int(item["tokens"]) for item in injected)


# 租户看不到的书里的条目只留这些键：哪一条、多长（字数与 token）、落在哪、信不信任、渲染了几次、为什么被跳过。
_HIDDEN_ITEM_KEYS = ("type", "id", "chars", "tokens", "position", "trusted", "renders", "reason")


async def _visible_worldbook_items(user: DBUser, raw: Any) -> list[dict[str, Any]]:
    """明细里的世界书条目补上「现在」的状态，并按身份裁掉看不见的书

    条目行本身没有归属列，可见范围算在书上；`describe_round_items` 把归属实例对经两个
    内部键带回来，这里用与其它接口同一个判据（`admin_row_visible`）判一次。

    为什么明细已经按频道归属过了范围还要再判一次：明细行的范围是**频道**的，而世界书可以
    由域内另一个实例的书、或平台方给全局人设建的书组成——那本书可能压根不属于这个租户。
    这一步是 fail-closed 的兜底：判不了就写 `hidden` 并抹掉书 ID、书名与标题，宁可少显示。

    平台方（`get_tenant_scope` 为空）不需要这一步过滤，但仍然走同一条补状态的路：明细页
    要显示「这条现在已隔离」。两个内部键在任何身份下都在返回前删掉。
    """
    from nekro_agent.services.worldbook import service as wb_service

    if not isinstance(raw, list):
        return []
    is_platform = get_tenant_scope(user) is None
    hidden: dict[tuple[str, str], bool] = {}

    described = await wb_service.describe_round_items(raw)
    # 归属实例对为空的书里，平台方给全局人设建、人设对这个租户可见的那些只读放开（所有者
    # 2026-11-10 定），判据与世界书页同一个函数。一次取完，不逐条查。
    readable_platform_books: set[int] = set()
    if not is_platform:
        global_book_ids = sorted(
            {
                int(item.get("_book_id") or 0)
                for item in described
                if not item.get("_book_adapter_key") and not item.get("_book_instance_key")
            }
            - {0},
        )
        if global_book_ids:
            readable_platform_books = await wb_service.platform_books_readable_by(
                user,
                await DBWbBook.filter(id__in=global_book_ids),
            )

    enriched: list[dict[str, Any]] = []
    for item in described:
        adapter_key = str(item.pop("_book_adapter_key", ""))
        instance_key = str(item.pop("_book_instance_key", ""))
        book_id = int(item.pop("_book_id", 0) or 0)
        owner = (adapter_key, instance_key)
        if not is_platform:
            if not adapter_key and not instance_key:
                # 书被删、或归属实例对为空：只有平台方给全局人设建、人设对这个租户可见的书
                # 放开（只读）；其余判不了归属的一律按不可见处理——判不了就放行是授权边界上
                # 唯一不能选的那一侧。
                owner_visible = book_id in readable_platform_books
            else:
                if owner not in hidden:
                    hidden[owner] = await admin_row_visible(user, adapter_key, instance_key)
                owner_visible = hidden[owner]
            if not owner_visible:
                # 白名单而不是逐个抹：明细行里以后多记一个字段（比如标题），逐个抹的写法会把
                # 它漏给看不到这本书的人。
                item = {key: item[key] for key in _HIDDEN_ITEM_KEYS if key in item}
                item["status"] = "hidden"
                item["book_name"] = ""
                item["comment"] = ""
        enriched.append(item)
    return enriched


def _prompt_round_item(row: DBMemPromptRound, summary_created_at: Optional[datetime] = None) -> PromptRoundListItem:
    return PromptRoundListItem(
        id=row.id,
        chat_key=row.chat_key,
        preset_id=row.preset_id,
        outcome=row.outcome,
        render_count=row.render_count,
        memory_state=row.memory_state,
        memory_items_count=len(row.memory_items) if isinstance(row.memory_items, list) else 0,
        memory_block_chars=row.memory_block_chars,
        summary_state=row.summary_state,
        summary_version=row.summary_version,
        summary_created_at=summary_created_at,
        summary_chars=row.summary_chars,
        affection_shown=row.affection_shown,
        affection_chars=row.affection_chars,
        affection_people=row.affection_people,
        affection_milestone_ids=list(row.affection_milestone_ids or []),
        worldbook_state=row.worldbook_state,
        worldbook_count=_worldbook_injected_count(row.worldbook_items),
        worldbook_chars=row.worldbook_chars,
        worldbook_tokens=_worldbook_injected_tokens(row.worldbook_items),
        tavern_items=row.tavern_items or {},
        state_items=row.state_items or {},
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


async def _person_accounts(
    scope: AdminMemoryScope,
    person: str,
    instance: Optional[DBAdapterInstance],
) -> Optional[frozenset[str]]:
    """把「某个人」展开成账号集合；没填人时返回 `None`，表示不加这条筛选。

    展开走**实例行上的**适配器与实例标识：请求里传来的 `adapter_key` 可能为空。
    """
    text = person.strip()
    if not text:
        return None
    adapter_key = instance.adapter_key if instance is not None else ""
    instance_key = instance.instance_key if instance is not None else ""
    return await admin_service.resolve_person_filter(
        scope,
        text,
        adapter_key=adapter_key,
        instance_key=instance_key,
    )


def _paragraph_item(
    row: DBMemParagraph,
    accounts: list[str],
    labels: Optional[list[PersonLabel]] = None,
) -> ParagraphListItem:
    # 来源类型在库里是枚举，列表对外给字符串：前端拿到的是可比较的 `"affection"`，
    # 而不是某个 Python 枚举的表示形式。
    origin_kind = row.origin_kind.value if row.origin_kind is not None else None
    return ParagraphListItem(
        id=row.id,
        workspace_id=row.workspace_id,
        adapter_key=row.adapter_key,
        instance_key=row.instance_key,
        preset_id=row.preset_id,
        origin_chat_key=row.origin_chat_key,
        origin_chat_type=row.origin_chat_type,
        origin_kind=origin_kind,
        origin_ref=row.origin_ref or None,
        summary=row.summary or "",
        content=row.content[:200],
        cognitive_type=row.cognitive_type.value,
        knowledge_type=row.knowledge_type.value,
        status=admin_service.paragraph_status(row),
        is_frozen=row.is_frozen,
        is_protected=row.is_protected,
        is_sensitive=row.is_sensitive,
        blocked_round_hits=row.blocked_round_hits,
        quarantined_at=row.quarantined_at,
        quarantine_reason=row.quarantine_reason,
        create_time=row.create_time,
        update_time=row.update_time,
        person_accounts=accounts,
        persons=[
            PersonLabelItem.model_validate(label.as_dict())
            for label in (labels if labels is not None else [PersonLabel(account=account) for account in accounts])
        ],
        episode_id=row.episode_id,
    )


def _episode_item(row: DBMemEpisode) -> EpisodeListItem:
    return EpisodeListItem(
        id=row.id,
        workspace_id=row.workspace_id,
        adapter_key=row.adapter_key,
        instance_key=row.instance_key,
        preset_id=row.preset_id,
        origin_chat_key=row.origin_chat_key,
        origin_chat_type=row.origin_chat_type,
        title=row.title,
        narrative_summary=row.narrative_summary,
        status=admin_service.episode_status(row),
        blocked_round_hits=row.blocked_round_hits,
        quarantined_at=row.quarantined_at,
        quarantine_reason=row.quarantine_reason,
        time_start=row.time_start,
        time_end=row.time_end,
        create_time=row.create_time,
        update_time=row.update_time,
    )


def _quarantine_item(entry: QuarantineEntry) -> QuarantineListItem:
    return QuarantineListItem(
        memory_type=entry.memory_type,
        id=entry.id,
        title_or_summary=entry.title_or_summary,
        origin_chat_key=entry.origin_chat_key,
        quarantined_at=entry.quarantined_at,
        quarantine_reason=entry.quarantine_reason,
        blocked_round_hits=entry.blocked_round_hits,
        cognitive_type=entry.cognitive_type,
        knowledge_type=entry.knowledge_type,
    )


async def _detail_payload(memory_type: str, row: Any, user: DBUser) -> MemoryItemDetail:
    """单条详情：段落用服务层的详情构造，其余类型 `to_dict` 后补状态。

    段落所属的事件按事件行自己的实例对再判一次范围（与「按事件分组」一致），范围外只给编号。
    """
    if isinstance(row, DBMemParagraph):

        async def episode_visible(episode: DBMemEpisode) -> bool:
            return await admin_row_visible(user, episode.adapter_key, episode.instance_key)

        payload = await admin_service.build_paragraph_detail(row, episode_visible=episode_visible)
    else:
        payload = row.to_dict()
        if isinstance(row, DBMemEpisode):
            payload["status"] = admin_service.episode_status(row)
        else:
            payload["status"] = "active" if not row.is_inactive else "deleted"
    payload["memory_type"] = memory_type
    return detail_from_payload(payload)


def _hard_delete_report(report: HardDeleteReport) -> HardDeleteReportResponse:
    return HardDeleteReportResponse.model_validate(report.__dict__)


async def _hard_delete(memory_type: str, item_id: int) -> HardDeleteReport:
    if memory_type == "paragraph":
        return await admin_service.hard_delete_paragraphs([item_id])
    if memory_type == "episode":
        return await admin_service.hard_delete_episodes([item_id])
    if memory_type == "relation":
        return await admin_service.hard_delete_relations([item_id])
    if memory_type == "entity":
        return await admin_service.hard_delete_entities([item_id])
    raise ValidationError(reason=f"未知的记忆类型: {memory_type}")


def _soft_delete_report(memory_type: str) -> HardDeleteReport:
    """软删不上报连带删除：只有被删的那一行真的变了，其余计数保持 0。"""
    report = HardDeleteReport()
    if memory_type == "paragraph":
        report.paragraphs = 1
    elif memory_type == "episode":
        report.episodes = 1
    elif memory_type == "relation":
        report.relations = 1
    elif memory_type == "entity":
        report.entities = 1
    return report


def _bulk_response(result: BulkResult) -> BulkResultResponse:
    cascade = _hard_delete_report(result.cascade) if result.cascade is not None else None
    return BulkResultResponse(
        matched=result.matched,
        affected=result.affected,
        failed_ids=result.failed_ids,
        cascade=cascade,
    )


@router.get("/summaries", summary="摘要列表", response_model=SummaryListResponse)
@require_role(Role.Admin)
async def list_summaries(
    chat_key: str = "",
    preset_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
    instance_key: str = "",
    adapter_key: str = "",
    domain: str = "self",
    group_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SummaryListResponse:
    """每个（频道, 人设）一行，按最新版创建时间倒序。"""
    scope, _ = await _resolve_admin_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=domain,
        group_id=group_id,
    )
    f = AdminFilter(chat_key=chat_key.strip() or None, preset_id=preset_id)
    total, entries = await channel_summary.list_summaries(scope, f, page=page, page_size=page_size)
    return SummaryListResponse(total=total, items=entries)


@router.get(
    "/summaries/{chat_key}/{preset_id}",
    summary="摘要历史版本",
    response_model=SummaryVersionsResponse,
)
@require_role(Role.Admin)
async def get_summary_versions(
    chat_key: str,
    preset_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SummaryVersionsResponse:
    """最近保留的几版全文，新到旧；同时给出暂停状态。"""
    await _load_visible_channel(_current_user, chat_key)
    versions = await channel_summary.get_versions(chat_key, preset_id)
    state = await channel_summary.get_summary_state(chat_key, preset_id)
    paused = state is not None and state.paused
    return SummaryVersionsResponse(
        chat_key=chat_key,
        preset_id=preset_id,
        paused=paused,
        paused_by=state.paused_by if paused and state is not None else "",
        paused_at=state.paused_at if paused and state is not None else None,
        versions=[_summary_version_item(row) for row in versions],
    )


@router.put("/summaries/{chat_key}/{preset_id}", summary="编辑摘要", response_model=SummaryVersionItem)
@require_role(Role.Admin)
async def edit_channel_summary(
    chat_key: str,
    preset_id: int,
    body: SummaryEditRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SummaryVersionItem:
    """手动编辑存成新的一版；`base_version` 不是当前最新版就 409。"""
    channel = await _load_visible_channel(_current_user, chat_key)
    summary = await channel_summary.edit_summary(
        chat_key=chat_key,
        preset_id=preset_id,
        adapter_key=channel.adapter_key,
        instance_key=channel.instance_key,
        content=body.content,
        base_version=body.base_version,
        editor=_current_user.username,
    )
    logger.info(
        f"频道摘要已编辑: operator={_current_user.username}, chat_key={chat_key}, "
        f"preset_id={preset_id}, action=edit, version={summary.version}",
    )
    return _summary_version_item(summary)


@router.post(
    "/summaries/{chat_key}/{preset_id}/rollback",
    summary="回滚摘要",
    response_model=SummaryVersionItem,
)
@require_role(Role.Admin)
async def rollback_channel_summary(
    chat_key: str,
    preset_id: int,
    body: SummaryRollbackRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SummaryVersionItem:
    """把指定旧版（默认上一版）写成新的一版。"""
    await _load_visible_channel(_current_user, chat_key)
    summary = await channel_summary.rollback_summary(
        chat_key=chat_key,
        preset_id=preset_id,
        to_version=body.to_version,
        editor=_current_user.username,
    )
    logger.info(
        f"频道摘要已回滚: operator={_current_user.username}, chat_key={chat_key}, "
        f"preset_id={preset_id}, action=rollback, version={summary.version}, "
        f"source_version={summary.source_version}",
    )
    return _summary_version_item(summary)


@router.post(
    "/summaries/{chat_key}/{preset_id}/pause",
    summary="暂停或恢复摘要",
    response_model=SummaryPauseResponse,
)
@require_role(Role.Admin)
async def pause_channel_summary(
    chat_key: str,
    preset_id: int,
    body: SummaryPauseRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> SummaryPauseResponse:
    """只挡沉淀自动更新；摘要注入不受影响。"""
    channel = await _load_visible_channel(_current_user, chat_key)
    state = await channel_summary.set_paused(
        chat_key=chat_key,
        preset_id=preset_id,
        adapter_key=channel.adapter_key,
        instance_key=channel.instance_key,
        paused=body.paused,
        editor=_current_user.username,
    )
    logger.info(
        f"频道摘要暂停状态已更新: operator={_current_user.username}, chat_key={chat_key}, "
        f"preset_id={preset_id}, action={'pause' if body.paused else 'resume'}",
    )
    return SummaryPauseResponse(paused=state.paused, paused_by=state.paused_by, paused_at=state.paused_at)


@router.get("/prompt-rounds", summary="提示词明细列表", response_model=PromptRoundListResponse)
@require_role(Role.Admin)
async def list_prompt_rounds(
    chat_key: str = "",
    page: int = 1,
    page_size: int = 20,
    instance_key: str = "",
    adapter_key: str = "",
    domain: str = "self",
    group_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PromptRoundListResponse:
    """某个频道最近的轮次，倒序；频道必填。"""
    normalized_chat_key = chat_key.strip()
    if not normalized_chat_key:
        raise ValidationError(reason="chat_key 必填")
    await _load_visible_channel(_current_user, normalized_chat_key)
    scope, _ = await _resolve_admin_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=domain,
        group_id=group_id,
    )
    total, rows = await prompt_rounds.list_rounds(
        scope,
        chat_key=normalized_chat_key,
        page=page,
        page_size=page_size,
    )
    # 「第 N 版」换成那一版的生成时间：明细行记着摘要行的主键，一次批量取回。摘要只留最近
    # 几版，取不到的（已经不保留）留空，界面退回显示版号。摘要行的频道与人设要与这一轮对得上
    # （与详情同一条纵深防御），对不上的同样留空。
    summary_ids = sorted({row.summary_id for row in rows if row.summary_id is not None})
    summaries: dict[int, tuple[str, int, Optional[datetime]]] = {}
    if summary_ids:
        summary_rows = await DBMemChannelSummary.filter(id__in=summary_ids, chat_key=normalized_chat_key).values(
            "id",
            "chat_key",
            "preset_id",
            "create_time",
        )
        summaries = {
            int(item["id"]): (str(item["chat_key"]), int(item["preset_id"]), item["create_time"])
            for item in summary_rows
        }

    def created_at(row: DBMemPromptRound) -> Optional[datetime]:
        found = summaries.get(row.summary_id) if row.summary_id is not None else None
        if found is None or (found[0], found[1]) != (row.chat_key, row.preset_id):
            return None
        return found[2]

    return PromptRoundListResponse(
        total=total,
        items=[_prompt_round_item(row, created_at(row)) for row in rows],
    )


@router.get(
    "/prompt-rounds/{round_pk}",
    summary="提示词明细详情",
    response_model=PromptRoundDetailResponse,
)
@require_role(Role.Admin)
async def get_prompt_round(
    round_pk: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> PromptRoundDetailResponse:
    """按主键取一行；用行上的实例对判归属，不可见与不存在同一段文字。"""
    row = await DBMemPromptRound.get_or_none(id=round_pk)
    if row is None:
        raise NotFoundError(resource="记忆")
    if get_tenant_scope(_current_user) is not None and not await admin_row_visible(
        _current_user,
        row.adapter_key,
        row.instance_key,
    ):
        raise NotFoundError(resource="记忆")
    detail = await prompt_rounds.round_detail(row)
    # 世界书条目按身份补状态并裁掉看不见的书：明细行的范围是频道的，书可能属于域内另一个
    # 实例、或压根是平台方的全局人设书。
    detail["worldbook_items"] = await _visible_worldbook_items(_current_user, detail.get("worldbook_items"))
    return PromptRoundDetailResponse.model_validate(detail)


@router.get("/graph", summary="记忆图谱", response_model=MemoryGraphResponse)
@require_role(Role.Admin)
async def get_memory_graph(
    partition: str = "all",
    chat_key: str = "",
    preset_id: Optional[int] = None,
    person: str = "",
    include_inactive: bool = False,
    time_from: str = "",
    time_to: str = "",
    limit: int = GRAPH_LIMIT_DEFAULT,
    instance_key: str = "",
    adapter_key: str = "",
    domain: str = "self",
    group_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> MemoryGraphResponse:
    """段落、事件、实体、关系与「涉及的人」的图谱；只读

    组装照搬工作区记忆页的图谱接口，取数换成与段落列表同一个范围（`_resolve_admin_scope` +
    `AdminMemoryScope.apply`），所以看得到域记忆；筛选参数与段落列表同名同义。`limit` 夹在
    50 ~ 600 之间。单个节点的详情用 `/items/{type}/{id}`（「人」节点没有详情，信息在元数据里）。
    """
    from nekro_agent.services.identity_scope import resolve_memory_domain

    scope, instance = await _resolve_admin_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=domain,
        group_id=group_id,
    )
    person_accounts = await _person_accounts(scope, person, instance)
    f = _admin_filter(
        partition=partition,
        chat_key=chat_key,
        preset_id=preset_id,
        person_accounts=person_accounts,
    )
    memory_domain = (
        await resolve_memory_domain(instance.adapter_key, instance.instance_key) if instance is not None else None
    )
    graph = await build_admin_memory_graph(
        scope,
        f,
        include_inactive=include_inactive,
        time_from=_parse_iso_datetime(time_from, "time_from"),
        time_to=_parse_iso_datetime(time_to, "time_to"),
        limit=limit,
        domain=memory_domain,
    )
    return MemoryGraphResponse(
        generated_at=datetime.now(timezone.utc),
        node_count=len(graph.nodes),
        edge_count=len(graph.edges),
        nodes=[MemoryGraphNode.model_validate(node) for node in graph.nodes],
        edges=[MemoryGraphEdge.model_validate(edge) for edge in graph.edges],
    )


@router.get("/scope-options", summary="记忆管理筛选项", response_model=ScopeOptionsResponse)
@require_role(Role.Admin)
async def get_scope_options(
    instance_key: str = "",
    adapter_key: str = "",
    domain: str = "self",
    group_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ScopeOptionsResponse:
    """筛选栏能选哪些实例、频道、人设；选项本身也按作用域裁剪。"""
    scope, _ = await _resolve_admin_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=domain,
        group_id=group_id,
    )
    options = await admin_service.scope_options(_current_user, scope)
    return ScopeOptionsResponse(
        instances=[InstanceOption.model_validate(item) for item in options["instances"]],
        channels=[ChannelOption.model_validate(item) for item in options["channels"]],
        presets=[PresetOption.model_validate(item) for item in options["presets"]],
    )


@router.get("/paragraphs", summary="段落记忆列表", response_model=ParagraphListResponse)
@require_role(Role.Admin)
async def list_paragraphs(
    partition: str = "all",
    chat_key: str = "",
    preset_id: Optional[int] = None,
    person: str = "",
    status: str = "active",
    frozen: Optional[bool] = None,
    protected: Optional[bool] = None,
    sensitive: Optional[bool] = None,
    cognitive_type: str = "",
    knowledge_type: str = "",
    time_from: str = "",
    time_to: str = "",
    q: str = "",
    origin_kind: str = "",
    episode: str = "",
    sort: str = "create_time",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
    instance_key: str = "",
    adapter_key: str = "",
    domain: str = "self",
    group_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ParagraphListResponse:
    """段落列表。`status` 默认只看正常行，隔离区与已删除要显式选。

    `episode` 按所属事件筛：`none` 是没归入任何事件的段落，正整数是事件 ID。每行带上
    「涉及的人」的显示名（`persons`，与 `person_accounts` 一一对应）。
    """
    scope, instance = await _resolve_admin_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=domain,
        group_id=group_id,
    )
    person_accounts = await _person_accounts(scope, person, instance)
    f = _admin_filter(
        partition=partition,
        chat_key=chat_key,
        preset_id=preset_id,
        person_accounts=person_accounts,
    )
    filters = _paragraph_filters(
        status=status,
        frozen=frozen,
        protected=protected,
        cognitive_type=cognitive_type,
        knowledge_type=knowledge_type,
        time_from=time_from,
        time_to=time_to,
        q=q,
        origin_kind=origin_kind,
        episode=episode,
        sensitive=sensitive,
    )
    total, rows = await admin_service.list_paragraphs(
        scope,
        f,
        status=filters.status,
        frozen=filters.frozen,
        protected=filters.protected,
        sensitive=filters.sensitive,
        cognitive_type=filters.cognitive_type,
        knowledge_type=filters.knowledge_type,
        time_from=filters.time_from,
        time_to=filters.time_to,
        q=filters.q,
        origin_kind=filters.origin_kind,
        episode=filters.episode,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )
    accounts = await load_person_accounts([row.id for row in rows])
    labels = await label_paragraph_persons(rows, accounts)
    return ParagraphListResponse(
        total=total,
        items=[_paragraph_item(row, accounts.get(row.id, []), labels.get(row.id)) for row in rows],
    )


@router.get("/persons", summary="记忆里出现过的人", response_model=MemoryPersonListResponse)
@require_role(Role.Admin)
async def list_memory_persons(
    partition: str = "all",
    chat_key: str = "",
    preset_id: Optional[int] = None,
    status: str = "active",
    frozen: Optional[bool] = None,
    protected: Optional[bool] = None,
    sensitive: Optional[bool] = None,
    cognitive_type: str = "",
    knowledge_type: str = "",
    time_from: str = "",
    time_to: str = "",
    q: str = "",
    origin_kind: str = "",
    episode: str = "",
    account_q: str = "",
    page: int = 1,
    page_size: int = 50,
    instance_key: str = "",
    adapter_key: str = "",
    domain: str = "self",
    group_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> MemoryPersonListResponse:
    """满足筛选的段落里出现过哪些人、各几段，按段数倒序；只读

    给「按人筛选」的候选与「按人分组」的视图用。筛选参数与段落列表同名同义（没有 `person`：
    这里列的就是人）；`account_q` 只按账号匹配，名字由界面在拿到的这一页里再筛。
    选了实例时同一个人的几个账号并成一行（与按人筛选的别名展开同一个域）。
    范围与段落列表同一个：`_resolve_admin_scope` + `AdminMemoryScope.apply`。
    """
    from nekro_agent.services.identity_scope import resolve_memory_domain

    scope, instance = await _resolve_admin_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=domain,
        group_id=group_id,
    )
    f = _admin_filter(partition=partition, chat_key=chat_key, preset_id=preset_id, person_accounts=None)
    filters = _paragraph_filters(
        status=status,
        frozen=frozen,
        protected=protected,
        cognitive_type=cognitive_type,
        knowledge_type=knowledge_type,
        time_from=time_from,
        time_to=time_to,
        q=q,
        origin_kind=origin_kind,
        episode=episode,
        sensitive=sensitive,
    )
    memory_domain = (
        await resolve_memory_domain(instance.adapter_key, instance.instance_key) if instance is not None else None
    )
    total, entries = await admin_service.list_memory_persons(
        scope,
        f,
        filters,
        domain=memory_domain,
        q=account_q,
        page=page,
        page_size=page_size,
    )
    return MemoryPersonListResponse(
        total=total,
        items=[
            MemoryPersonItem(
                account=entry.account,
                accounts=list(entry.accounts),
                display_name=entry.display_name,
                call_name=entry.call_name,
                name_source=entry.name_source,
                paragraph_count=entry.paragraph_count,
                last_paragraph_id=entry.last_paragraph_id,
            )
            for entry in entries
        ],
    )


@router.get("/topics", summary="段落按事件分组", response_model=MemoryTopicListResponse)
@require_role(Role.Admin)
async def list_memory_topics(
    partition: str = "all",
    chat_key: str = "",
    preset_id: Optional[int] = None,
    person: str = "",
    status: str = "active",
    frozen: Optional[bool] = None,
    protected: Optional[bool] = None,
    sensitive: Optional[bool] = None,
    cognitive_type: str = "",
    knowledge_type: str = "",
    time_from: str = "",
    time_to: str = "",
    q: str = "",
    origin_kind: str = "",
    page: int = 1,
    page_size: int = 20,
    instance_key: str = "",
    adapter_key: str = "",
    domain: str = "self",
    group_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> MemoryTopicListResponse:
    """满足筛选的段落按所属事件分组，各几段、组内最新一段的时间；只读

    给「按事件分组」的视图用；展开一组时用段落列表的 `episode` 参数取组内段落。筛选参数与
    段落列表同名同义（没有 `episode`：这里分的就是事件）。
    范围与段落列表同一个，事件标题按同一个范围查，范围外的事件只给编号。
    """
    scope, instance = await _resolve_admin_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=domain,
        group_id=group_id,
    )
    person_accounts = await _person_accounts(scope, person, instance)
    f = _admin_filter(
        partition=partition,
        chat_key=chat_key,
        preset_id=preset_id,
        person_accounts=person_accounts,
    )
    filters = _paragraph_filters(
        status=status,
        frozen=frozen,
        protected=protected,
        cognitive_type=cognitive_type,
        knowledge_type=knowledge_type,
        time_from=time_from,
        time_to=time_to,
        q=q,
        origin_kind=origin_kind,
        sensitive=sensitive,
    )
    total, entries = await admin_service.list_memory_topics(scope, f, filters, page=page, page_size=page_size)
    return MemoryTopicListResponse(
        total=total,
        items=[
            MemoryTopicItem(
                episode_id=entry.episode_id,
                title=entry.title,
                narrative_summary=entry.narrative_summary,
                paragraph_count=entry.paragraph_count,
                last_time=entry.last_time,
                visible=entry.visible,
            )
            for entry in entries
        ],
    )


@router.get("/episodes", summary="episode 列表", response_model=EpisodeListResponse)
@require_role(Role.Admin)
async def list_episodes(
    partition: str = "all",
    chat_key: str = "",
    preset_id: Optional[int] = None,
    status: str = "active",
    cognitive_type: str = "",
    time_from: str = "",
    time_to: str = "",
    q: str = "",
    sort: str = "create_time",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
    instance_key: str = "",
    adapter_key: str = "",
    domain: str = "self",
    group_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> EpisodeListResponse:
    """episode 列表；没有按人、冻结、保护、知识类型这几维。"""
    scope, _ = await _resolve_admin_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=domain,
        group_id=group_id,
    )
    f = _admin_filter(
        partition=partition,
        chat_key=chat_key,
        preset_id=preset_id,
        person_accounts=None,
    )
    time_from_value = _parse_iso_datetime(time_from, "time_from")
    time_to_value = _parse_iso_datetime(time_to, "time_to")
    if cognitive_type.strip():
        raise ValidationError(reason="episode 不支持按认知类型筛选")
    total, rows = await admin_service.list_episodes(
        scope,
        f,
        status=status.strip().lower() or "all",
        time_from=time_from_value,
        time_to=time_to_value,
        q=q.strip() or None,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )
    return EpisodeListResponse(total=total, items=[_episode_item(row) for row in rows])


@router.get("/quarantine", summary="隔离区列表", response_model=QuarantineListResponse)
@require_role(Role.Admin)
async def list_quarantine(
    partition: str = "all",
    chat_key: str = "",
    preset_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
    instance_key: str = "",
    adapter_key: str = "",
    domain: str = "self",
    group_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> QuarantineListResponse:
    """段落与 episode 的隔离行合并在一起，按隔离时间倒序。"""
    scope, _ = await _resolve_admin_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=domain,
        group_id=group_id,
    )
    f = _admin_filter(
        partition=partition,
        chat_key=chat_key,
        preset_id=preset_id,
        person_accounts=None,
    )
    total, entries = await admin_service.list_quarantine(scope, f, page=page, page_size=page_size)
    return QuarantineListResponse(total=total, items=[_quarantine_item(entry) for entry in entries])


@router.get(
    "/items/paragraph/{item_id}/trace",
    summary="追溯段落来源",
    response_model=MemoryTraceResponse,
)
@require_role(Role.Admin)
async def trace_memory_item(
    item_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> MemoryTraceResponse:
    """只有段落需要追溯原话；其余类型没有来源锚点。"""
    row = await _load_visible_item(_current_user, "paragraph", item_id)
    if not isinstance(row, DBMemParagraph):
        raise NotFoundError(resource="记忆")
    trace = await admin_service.trace_paragraph(row)
    return MemoryTraceResponse(
        workspace_id=trace.workspace_id,
        memory_type=trace.memory_type,
        memory_id=trace.memory_id,
        origin_kind=trace.origin_kind,
        origin_ref=trace.origin_ref,
        origin_chat_key=trace.origin_chat_key,
        anchor_msg_id=trace.anchor_msg_id,
        anchor_msg_id_start=trace.anchor_msg_id_start,
        anchor_msg_id_end=trace.anchor_msg_id_end,
        anchor_timestamp_start=trace.anchor_timestamp_start,
        anchor_timestamp_end=trace.anchor_timestamp_end,
        cross_channel=trace.cross_channel,
        messages=[
            MemoryTraceMessage(
                db_id=message.db_id,
                message_id=message.message_id,
                sender_name=message.sender_name,
                send_timestamp=message.send_timestamp,
                content_preview=message.content_preview,
            )
            for message in trace.messages
        ],
    )


@router.get("/items/{memory_type}/{item_id}", summary="记忆单条详情", response_model=MemoryItemDetail)
@require_role(Role.Admin)
async def get_memory_item(
    memory_type: str,
    item_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> MemoryItemDetail:
    """按 ID 取一条；不存在与不是自己的响应逐字相同。"""
    row = await _load_visible_item(_current_user, memory_type, item_id)
    return await _detail_payload(memory_type, row, _current_user)


@router.put("/items/{memory_type}/{item_id}", summary="编辑记忆", response_model=MemoryItemDetail)
@require_role(Role.Admin)
async def update_memory_item(
    memory_type: str,
    item_id: int,
    body: MemoryItemUpdateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> MemoryItemDetail:
    """编辑段落正文 / 摘要 / 涉及的人，或 episode 标题 / 叙事摘要。"""
    row = await _load_visible_item(_current_user, memory_type, item_id)
    if memory_type == "paragraph":
        if not isinstance(row, DBMemParagraph):
            raise NotFoundError(resource="记忆")
        await admin_service.edit_paragraph(
            row,
            content=body.content,
            summary=body.summary,
            person_accounts=body.person_accounts,
            editor=_current_user.username,
        )
    elif memory_type == "episode":
        if not isinstance(row, DBMemEpisode):
            raise NotFoundError(resource="记忆")
        await admin_service.edit_episode(
            row,
            title=body.title,
            narrative_summary=body.narrative_summary,
        )
    else:
        raise ValidationError(reason="关系与实体不支持编辑")
    logger.info(
        f"记忆已编辑: operator={_current_user.username}, memory_type={memory_type}, item_id={item_id}",
    )
    return await _detail_payload(memory_type, row, _current_user)


@router.post(
    "/items/{memory_type}/{item_id}/actions",
    summary="执行记忆动作",
    response_model=ActionResponse,
)
@require_role(Role.Admin)
async def act_on_memory_item(
    memory_type: str,
    item_id: int,
    body: MemoryItemActionRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """隔离 / 放回 / 冻结 / 保护 / 调权；动作白名单由服务层按类型判。"""
    row = await _load_visible_item(_current_user, memory_type, item_id)
    await admin_service.apply_action(memory_type, row, body.action, value=body.value)
    logger.info(
        f"记忆动作已执行: operator={_current_user.username}, "
        f"memory_type={memory_type}, item_id={item_id}, action={body.action}",
    )
    return ActionResponse()


@router.delete(
    "/items/{memory_type}/{item_id}",
    summary="删除记忆",
    response_model=HardDeleteReportResponse,
)
@require_role(Role.Admin)
async def delete_memory_item(
    memory_type: str,
    item_id: int,
    hard: bool = False,
    _current_user: DBUser = Depends(get_current_active_user),
) -> HardDeleteReportResponse:
    """默认软删；`hard=true` 时按类型走真删并返回连带删除报告。"""
    row = await _load_visible_item(_current_user, memory_type, item_id)
    if hard:
        report = await _hard_delete(memory_type, item_id)
    else:
        await admin_service.soft_delete(memory_type, row)
        report = _soft_delete_report(memory_type)
    logger.info(
        f"记忆已删除: operator={_current_user.username}, memory_type={memory_type}, "
        f"item_id={item_id}, hard={hard}",
    )
    return _hard_delete_report(report)


@router.post("/paragraphs/bulk", summary="批量操作段落", response_model=BulkResultResponse)
@require_role(Role.Admin)
async def bulk_paragraphs(
    body: BulkParagraphsRequest,
    instance_key: str = "",
    adapter_key: str = "",
    domain: str = "self",
    group_id: Optional[int] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> BulkResultResponse:
    """按 ID 或按筛选批量执行；可见范围始终来自当前身份。

    请求体里的筛选条件只当条件用：租户即使把别人的频道填进 `chat_key`，与自己的范围取交
    之后也是空集，不会动到范围外的行。
    """
    scope, instance = await _resolve_admin_scope(
        _current_user,
        instance_key=instance_key,
        adapter_key=adapter_key,
        domain=domain,
        group_id=group_id,
    )
    selection = body.selection
    f: Optional[AdminFilter] = None
    filters: Optional[ParagraphListFilters] = None
    if selection.filter is not None:
        spec: ParagraphFilterSelection = selection.filter
        person_accounts = await _person_accounts(scope, spec.person, instance)
        f = _admin_filter(
            partition=spec.partition,
            chat_key=spec.chat_key or "",
            preset_id=spec.preset_id,
            person_accounts=person_accounts,
        )
        filters = _paragraph_filters(
            status=spec.status,
            frozen=spec.frozen,
            protected=spec.protected,
            sensitive=spec.sensitive,
            cognitive_type=spec.cognitive_type or "",
            knowledge_type=spec.knowledge_type or "",
            time_from=spec.time_from or "",
            time_to=spec.time_to or "",
            q=spec.q or "",
            origin_kind=spec.origin_kind or "",
            episode=spec.episode or "",
        )
    else:
        f = AdminFilter()

    result = await admin_service.bulk_paragraphs(
        scope,
        ids=selection.ids,
        f=f,
        filters=filters,
        action=body.action,
        dry_run=body.dry_run,
        expected_count=body.expected_count,
    )
    logger.info(
        f"记忆批量操作: operator={_current_user.username}, action={body.action}, "
        f"matched={result.matched}, affected={result.affected}",
    )
    return _bulk_response(result)
