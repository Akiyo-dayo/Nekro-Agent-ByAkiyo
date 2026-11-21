"""记忆管理端的服务函数

可见范围只由 :class:`AdminMemoryScope` 产出，本模块里所有列表 / 批量 / 详情查询都先经过
`scope.apply(...)`；状态、时间、关键词、排序这些**不是范围**的条件才由这里自己拼。把两者
分开的理由与召回侧一致：范围条件散落到第二处实现时，漏掉的那一处不会报错，只会静默多给
几行。

状态转换的写入顺序统一为「先数据库、提交后向量」。Qdrant 不参与数据库事务，把它放进事务
里失败会连带回滚已经正确的删除；反过来「行已失活、点还在」是检索侧已经挡住的形态
（`is_inactive` 在回表前就被过滤），而「点删了、行还正常」会让这条记忆**不再被召回**却仍
出现在列表里，是更难解释的中间态。

本模块不做权限判断：单条访问的归属由路由用 `admin_row_visible` 判。服务函数只接受已经
算好的范围与行。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from tortoise.expressions import Q, Subquery
from tortoise.queryset import QuerySet
from tortoise.transactions import in_transaction

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_affection import DBAffection, DBAffectionEvent
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_instance_group import SELF_DOMAIN_GROUP_ID
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_channel_summary_state import DBMemChannelSummaryState
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import CognitiveType, DBMemParagraph, KnowledgeType, OriginKind
from nekro_agent.models.db_mem_paragraph_person import DBMemParagraphPerson
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_mem_reinforcement_log import DBMemReinforcementLog, TargetType
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.errors import ConflictError, ValidationError
from nekro_agent.services.memory.instance_scope import CONSOLE_ONLY_PRESET_ID, AdminFilter, AdminMemoryScope
from nekro_agent.services.memory.scope_condition import MemTable

logger = get_sub_logger("memory.admin")

# 一条 SQL 的 `IN (...)` 参数上限（asyncpg 是 32767，但这里取更保守的 1000，同时让单条
# 语句的执行时间可控）。真删的整条链路都按这个大小分批。
_BATCH_SIZE = 1000

# 批量操作一次最多匹配多少条。超过就让调用方缩小筛选；`restore` 要逐条重嵌，单独收紧。
_BULK_LIMIT = 5000
_BULK_RESTORE_LIMIT = 200

# 每个段落最多能挂多少个「涉及的人」。与沉淀写入侧的名单规模一致，超过说明请求拼错了。
_MAX_PERSON_ACCOUNTS = 10

MemoryType = Literal["paragraph", "episode", "relation", "entity"]


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def paragraph_status(row: DBMemParagraph) -> Literal["active", "quarantined", "deleted", "pruned"]:
    """段落的四态判据。

    隔离与软删都复用 `is_inactive`，靠 `quarantined_at` 分开；维护任务清理掉的行带有
    `last_manual_action = "prune"`，它和手动软删在页面上是两回事。
    """
    if not row.is_inactive:
        return "active"
    if row.quarantined_at is not None:
        return "quarantined"
    if row.last_manual_action == "prune":
        return "pruned"
    return "deleted"


def episode_status(row: DBMemEpisode) -> Literal["active", "quarantined", "deleted"]:
    """episode 没有 `last_manual_action` 列，失活且没隔离就是「已删除」。"""
    if not row.is_inactive:
        return "active"
    if row.quarantined_at is not None:
        return "quarantined"
    return "deleted"


_VALID_STATUSES = frozenset({"active", "quarantined", "deleted", "pruned", "all"})


def status_q(status: str) -> Q:
    """列表用的状态条件；`all` 不加条件。

    `deleted` 在这里**包含** `pruned`：本函数同时服务段落与 episode，不能引用只有段落
    有的 `last_manual_action`。段落列表要区分两者时，在调用点再叠一条
    `last_manual_action__not="prune"`（Tortoise 的 `__not` 自带 `OR IS NULL`，
    不会像裸 `NOT` 那样把 NULL 行一起排除）。
    """
    if status == "all":
        return Q()
    if status == "active":
        return Q(is_inactive=False)
    if status == "quarantined":
        return Q(is_inactive=True, quarantined_at__isnull=False)
    if status == "deleted":
        return Q(is_inactive=True, quarantined_at__isnull=True)
    if status == "pruned":
        return Q(is_inactive=True, quarantined_at__isnull=True, last_manual_action="prune")
    raise ValidationError(reason=f"未知的状态筛选: {status}")


# ---------------------------------------------------------------------------
# 列表与详情
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParagraphListFilters:
    """段落列表的普通筛选（不含可见范围，也不含排序与分页）。

    列表与批量共用它，条件只拼一次：批量按筛选执行时若与列表用了两份条件，「预览看到的
    条数」和「实际删掉的条数」就会在某个新增筛选维度上分叉。
    """

    status: str = "all"
    frozen: Optional[bool] = None
    protected: Optional[bool] = None
    cognitive_type: Optional[str] = None
    knowledge_type: Optional[str] = None
    time_from: Optional[datetime] = None
    time_to: Optional[datetime] = None
    q: Optional[str] = None
    # 来源类型，已经是 `OriginKind` 的取值；`None` 表示不筛
    origin_kind: Optional[str] = None


@dataclass(frozen=True)
class QuarantineEntry:
    """隔离区的一行（段落与 episode 合并后的形状）。"""

    memory_type: str
    id: int
    title_or_summary: str
    origin_chat_key: Optional[str]
    quarantined_at: datetime
    quarantine_reason: str
    blocked_round_hits: int


_SORTS = frozenset({"create_time", "update_time", "blocked_round_hits"})
_ORDERS = frozenset({"asc", "desc"})
_MAX_KEYWORD_LENGTH = 64


def _validate_status(status: str) -> str:
    if status not in _VALID_STATUSES:
        raise ValidationError(reason=f"未知的状态筛选: {status}")
    return status


def _validate_sort_order(sort: str, order: str) -> None:
    if sort not in _SORTS:
        raise ValidationError(reason=f"不支持的排序字段: {sort}")
    if order not in _ORDERS:
        raise ValidationError(reason=f"不支持的排序方向: {order}")


def _validate_page(page: int, page_size: int) -> int:
    if page < 1:
        raise ValidationError(reason="页码必须从 1 开始")
    if page_size < 1:
        raise ValidationError(reason="每页条数必须大于 0")
    # 上限直接截断而不是报错：前端把「全部」当成一个很大的数时，截断仍能给出可用结果。
    return min(page_size, 100)


def _order_by(sort: str, order: str) -> str:
    return f"-{sort}" if order == "desc" else sort


def _parse_cognitive_type(value: Optional[str]) -> Optional[CognitiveType]:
    if value is None or not value.strip():
        return None
    try:
        return CognitiveType(value.strip().lower())
    except ValueError as e:
        raise ValidationError(reason=f"未知的认知类型: {value}") from e


def _parse_knowledge_type(value: Optional[str]) -> Optional[KnowledgeType]:
    if value is None or not value.strip():
        return None
    try:
        return KnowledgeType(value.strip().lower())
    except ValueError as e:
        raise ValidationError(reason=f"未知的知识类型: {value}") from e


def _parse_origin_kind(value: Optional[str]) -> Optional[OriginKind]:
    """来源类型筛选；`None` / 空串表示不筛。

    服务层也校验一遍：调用方不止路由一处，写错的值到这里必须变成 400，
    而不是一条永远为真的 SQL 条件（那会把整页返回成全量）。
    """
    if value is None or not value.strip():
        return None
    try:
        return OriginKind(value.strip().lower())
    except ValueError as e:
        raise ValidationError(reason=f"未知的来源类型筛选: {value}") from e


def _paragraph_filter_q(filters: ParagraphListFilters) -> Optional[Q]:
    """段落列表 / 批量共用的普通条件；没有任何条件时返回 None。"""
    _validate_status(filters.status)
    conditions: list[Q] = []
    if filters.status != "all":
        status_condition = status_q(filters.status)
        if filters.status == "deleted":
            status_condition = status_condition & Q(last_manual_action__not="prune")
        conditions.append(status_condition)
    if filters.frozen is not None:
        conditions.append(Q(is_frozen=filters.frozen))
    if filters.protected is not None:
        conditions.append(Q(is_protected=filters.protected))
    cognitive_type = _parse_cognitive_type(filters.cognitive_type)
    if cognitive_type is not None:
        conditions.append(Q(cognitive_type=cognitive_type))
    knowledge_type = _parse_knowledge_type(filters.knowledge_type)
    if knowledge_type is not None:
        conditions.append(Q(knowledge_type=knowledge_type))
    origin_kind = _parse_origin_kind(filters.origin_kind)
    if origin_kind is not None:
        conditions.append(Q(origin_kind=origin_kind))
    if filters.time_from is not None:
        conditions.append(Q(event_time__gte=filters.time_from))
    if filters.time_to is not None:
        conditions.append(Q(event_time__lte=filters.time_to))
    if filters.q is not None and filters.q.strip():
        keyword = filters.q.strip()[:_MAX_KEYWORD_LENGTH]
        conditions.append(Q(content__icontains=keyword) | Q(summary__icontains=keyword))
    if not conditions:
        return None
    combined = conditions[0]
    for condition in conditions[1:]:
        combined = combined & condition
    return combined


def _filtered_paragraphs(
    scope: AdminMemoryScope,
    f: AdminFilter,
    filters: ParagraphListFilters,
) -> QuerySet[DBMemParagraph]:
    """范围 + 普通条件，列表与批量唯一的查询起点。"""
    queryset = scope.apply(DBMemParagraph.all(), MemTable.PARAGRAPH, f)
    condition = _paragraph_filter_q(filters)
    if condition is not None:
        queryset = queryset.filter(condition)
    return queryset


async def list_paragraphs(
    scope: AdminMemoryScope,
    f: AdminFilter,
    *,
    status: str = "all",
    frozen: Optional[bool] = None,
    protected: Optional[bool] = None,
    cognitive_type: Optional[str] = None,
    knowledge_type: Optional[str] = None,
    time_from: Optional[datetime] = None,
    time_to: Optional[datetime] = None,
    q: Optional[str] = None,
    origin_kind: Optional[str] = None,
    sort: str = "create_time",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[DBMemParagraph]]:
    """段落列表，返回 `(总数, 当前页)`。"""
    _validate_sort_order(sort, order)
    size = _validate_page(page, page_size)
    filters = ParagraphListFilters(
        status=status,
        frozen=frozen,
        protected=protected,
        cognitive_type=cognitive_type,
        knowledge_type=knowledge_type,
        time_from=time_from,
        time_to=time_to,
        q=q,
        origin_kind=origin_kind,
    )
    queryset = _filtered_paragraphs(scope, f, filters)
    total = await queryset.count()
    items = await queryset.order_by(_order_by(sort, order)).offset((page - 1) * size).limit(size)
    return total, list(items)


async def list_episodes(
    scope: AdminMemoryScope,
    f: AdminFilter,
    *,
    status: str = "all",
    time_from: Optional[datetime] = None,
    time_to: Optional[datetime] = None,
    q: Optional[str] = None,
    sort: str = "create_time",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[DBMemEpisode]]:
    """episode 列表。

    episode 没有认知 / 知识类型、冻结、保护这些列，就不接对应的参数；按人筛选在它上面
    也不成立，给了非空集合直接拒绝，空集合按「恒空」返回。
    """
    _validate_sort_order(sort, order)
    _validate_status(status)
    size = _validate_page(page, page_size)
    if f.person_accounts is not None:
        if not f.person_accounts:
            return 0, []
        raise ValidationError(reason="episode 不支持按人筛选")
    if status == "pruned":
        # episode 没有维护清理这一态。
        return 0, []

    queryset = scope.apply(DBMemEpisode.all(), MemTable.EPISODE, f)
    queryset = queryset.filter(status_q(status))
    if time_from is not None:
        queryset = queryset.filter(time_end__gte=time_from)
    if time_to is not None:
        queryset = queryset.filter(time_start__lte=time_to)
    if q is not None and q.strip():
        keyword = q.strip()[:_MAX_KEYWORD_LENGTH]
        queryset = queryset.filter(Q(title__icontains=keyword) | Q(narrative_summary__icontains=keyword))

    total = await queryset.count()
    items = await queryset.order_by(_order_by(sort, order)).offset((page - 1) * size).limit(size)
    return total, list(items)


async def list_quarantine(
    scope: AdminMemoryScope,
    f: AdminFilter,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[QuarantineEntry]]:
    """隔离区的段落与 episode 合并列表。

    两张表的行数都可能很大，但「按隔离时间倒序」的合并只能做在内存里。两边各取前
    `page * page_size` 条（不是全量）就够覆盖前 `page` 页：任何落在第 `page` 页的条目，
    在它自己那张表里的名次一定不超过 `page * page_size`。上限因此是 `2 * page * page_size`。
    """
    size = _validate_page(page, page_size)
    limit = page * size

    paragraph_base = scope.apply(
        DBMemParagraph.filter(is_inactive=True, quarantined_at__isnull=False),
        MemTable.PARAGRAPH,
        f,
    )
    # 人这一维只挂在段落上；隔离区是两类数据的合并视图，按人筛选时 episode 一侧不参与
    # 这条筛选（它本来就没有「涉及的人」），否则整个查询会因字段不存在而报错。
    episode_filter = replace(f, person_accounts=None)
    episode_base = scope.apply(
        DBMemEpisode.filter(is_inactive=True, quarantined_at__isnull=False),
        MemTable.EPISODE,
        episode_filter,
    )

    total = await paragraph_base.count() + await episode_base.count()
    paragraphs = await paragraph_base.order_by("-quarantined_at").limit(limit)
    episodes = await episode_base.order_by("-quarantined_at").limit(limit)

    entries: list[QuarantineEntry] = [
        QuarantineEntry(
            memory_type="paragraph",
            id=row.id,
            title_or_summary=row.summary or row.content[:80],
            origin_chat_key=row.origin_chat_key,
            quarantined_at=row.quarantined_at,
            quarantine_reason=row.quarantine_reason,
            blocked_round_hits=row.blocked_round_hits,
        )
        for row in paragraphs
        if row.quarantined_at is not None
    ]
    entries.extend(
        QuarantineEntry(
            memory_type="episode",
            id=row.id,
            title_or_summary=row.title,
            origin_chat_key=row.origin_chat_key,
            quarantined_at=row.quarantined_at,
            quarantine_reason=row.quarantine_reason,
            blocked_round_hits=row.blocked_round_hits,
        )
        for row in episodes
        if row.quarantined_at is not None
    )
    entries.sort(key=lambda entry: entry.quarantined_at, reverse=True)
    start = (page - 1) * size
    return total, entries[start : start + size]


_ITEM_MODELS: dict[str, type[Any]] = {
    "paragraph": DBMemParagraph,
    "episode": DBMemEpisode,
    "relation": DBMemRelation,
    "entity": DBMemEntity,
}


async def load_item(
    memory_type: str,
    item_id: int,
) -> Optional[DBMemParagraph | DBMemEpisode | DBMemRelation | DBMemEntity]:
    """按主键取一行，类型非法报 400，取不到返回 `None`。

    **不判权限**：归属由路由用 `admin_row_visible` 判，服务层拿不到用户身份。把权限判断
    放在路由的同一个守卫里，才能保证「不存在」与「无权访问」对外响应逐字相同。
    """
    model = _ITEM_MODELS.get(memory_type)
    if model is None:
        raise ValidationError(reason=f"未知的记忆类型: {memory_type}")
    return await model.get_or_none(id=item_id)


async def build_paragraph_detail(row: DBMemParagraph) -> dict[str, Any]:
    """段落详情：模型字典 + 状态、涉及的人、来源频道、所属 episode、强化次数。"""
    from nekro_agent.services.memory.persons import BOT_SENDER_ID, load_person_accounts

    accounts = (await load_person_accounts([row.id]))[row.id]
    persons: list[dict[str, str]] = []
    for account in accounts:
        message: Optional[DBChatMessage] = None
        if row.origin_chat_key:
            # 这里查的是「显示名」而不是记忆范围，但仍写成 Q：本模块的范围条件只能由
            # `AdminMemoryScope` 产出，避免下一次有人顺手把频道维度也拼进来。
            message = (
                await DBChatMessage.filter(
                    Q(chat_key=row.origin_chat_key) & Q(platform_userid=account),
                )
                .exclude(sender_id=BOT_SENDER_ID)
                .order_by("-id")
                .first()
            )
        persons.append({"account": account, "display_name": (message.sender_nickname or "") if message else ""})

    channel_name = ""
    if row.origin_chat_key:
        channel = await DBChatChannel.filter(Q(chat_key=row.origin_chat_key)).order_by("id").first()
        if channel is not None:
            channel_name = channel.channel_name or ""

    episode: Optional[dict[str, Any]] = None
    if row.episode_id is not None:
        episode_row = await DBMemEpisode.get_or_none(id=row.episode_id)
        if episode_row is not None:
            episode = {"id": episode_row.id, "title": episode_row.title}

    reinforcement_count = await DBMemReinforcementLog.filter(
        target_type=TargetType.PARAGRAPH,
        target_id=row.id,
    ).count()

    return row.to_dict() | {
        "status": paragraph_status(row),
        "persons": persons,
        "origin_channel_name": channel_name,
        "episode": episode,
        "reinforcement_count": reinforcement_count,
    }


async def trace_paragraph(row: DBMemParagraph) -> Any:
    """管理端的追溯：全量视角、不脱敏，直接复用召回侧的锚点逻辑。"""
    from nekro_agent.services.memory.instance_scope import MemoryScope
    from nekro_agent.services.memory.query_service import MemoryObjectType, trace_workspace_memory_origin

    return await trace_workspace_memory_origin(
        row.workspace_id,
        MemoryObjectType.PARAGRAPH,
        row.id,
        scope=MemoryScope.unrestricted(),
    )


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------


async def _validated_person_accounts(row: DBMemParagraph, accounts: Sequence[str]) -> list[str]:
    """校验「涉及的人」只能来自来源频道里发过言的非 Bot 账号。

    这是隐私边界：页面能选谁，就等于能把这段记忆关联到谁；不校验等于让管理员把任意账号
    （包括从未在这个频道出现过的）挂到记忆上，之后「跟人走」的召回就会把它带给那个人。
    """
    from nekro_agent.services.memory.persons import BOT_SENDER_ID, resolve_bot_account

    normalized = sorted({account.strip() for account in accounts if account and account.strip()})
    if len(normalized) > _MAX_PERSON_ACCOUNTS:
        raise ValidationError(reason=f"涉及的人最多 {_MAX_PERSON_ACCOUNTS} 个")
    if not normalized:
        return []
    if not row.origin_chat_key:
        raise ValidationError(reason="这段记忆没有来源频道，无法校验涉及的人")

    bot_account = await resolve_bot_account(row.adapter_key, row.instance_key)
    for account in normalized:
        if bot_account is not None and account == bot_account:
            raise ValidationError(reason=f"账号 {account} 是 Bot 自己，不能算作涉及的人")
        appeared = (
            await DBChatMessage.filter(
                Q(chat_key=row.origin_chat_key) & Q(platform_userid=account),
            )
            .exclude(sender_id=BOT_SENDER_ID)
            .exists()
        )
        if not appeared:
            raise ValidationError(reason=f"账号 {account} 没有在来源频道发过言，不能算作涉及的人")
    return normalized


async def edit_paragraph(
    row: DBMemParagraph,
    *,
    content: Optional[str],
    summary: Optional[str],
    person_accounts: Optional[list[str]],
    editor: str,
) -> DBMemParagraph:
    """编辑段落。正常状态同步向量；隔离 / 删除状态只改数据库，放回时会重嵌。"""
    from nekro_agent.services.memory.embedding_service import embed_text
    from nekro_agent.services.memory.persons import build_paragraph_payload, replace_paragraph_persons
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

    content_updated = False
    update_fields: list[str] = ["last_manual_action", "last_manual_action_at", "update_time"]
    if content is not None:
        if not content.strip():
            raise ValidationError(reason="段落正文不能为空")
        content_updated = content != row.content
        row.content = content
        update_fields.append("content")
    if summary is not None:
        row.summary = summary[:512]
        update_fields.append("summary")

    normalized_persons: Optional[list[str]] = None
    if person_accounts is not None:
        normalized_persons = await _validated_person_accounts(row, person_accounts)
        await replace_paragraph_persons(row.id, normalized_persons)

    row.last_manual_action = "edit"
    row.last_manual_action_at = datetime.now(timezone.utc)
    await row.save(update_fields=update_fields)
    logger.info(f"管理端编辑段落: paragraph_id={row.id}, editor={editor}")

    # 隔离 / 已删除的段落不碰向量：它的点已经删掉，此时写回会让这条内容重新具备被检索
    # 的形状，而列表里的状态仍是隔离。
    if paragraph_status(row) != "active":
        return row

    try:
        if content_updated:
            embedding = await embed_text(row.content)
            await memory_qdrant_manager.upsert_paragraph(
                paragraph_id=row.id,
                embedding=embedding,
                payload=await build_paragraph_payload(row),
            )
            if not row.embedding_ref:
                # 与沉淀写入侧同一规则：向量 ID 就是段落主键的字符串。
                row.embedding_ref = str(row.id)
                await row.save(update_fields=["embedding_ref", "update_time"])
        elif normalized_persons is not None:
            await memory_qdrant_manager.set_paragraph_payload([row.id], {"person_accounts": normalized_persons})
    except Exception as e:
        logger.warning(f"编辑后刷新段落向量失败: paragraph_id={row.id}, error={e}")
    return row


async def edit_episode(
    row: DBMemEpisode,
    *,
    title: Optional[str],
    narrative_summary: Optional[str],
) -> DBMemEpisode:
    """编辑 episode 的标题与叙事摘要（没有向量，只写数据库）。"""
    update_fields = ["update_time"]
    if title is not None:
        if not title.strip():
            raise ValidationError(reason="episode 标题不能为空")
        row.title = title
        update_fields.append("title")
    if narrative_summary is not None:
        if not narrative_summary.strip():
            raise ValidationError(reason="episode 叙事摘要不能为空")
        row.narrative_summary = narrative_summary
        update_fields.append("narrative_summary")
    await row.save(update_fields=update_fields)
    return row


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------


_ACTION_WHITELIST: dict[str, frozenset[str]] = {
    "paragraph": frozenset(
        {"freeze", "unfreeze", "protect", "unprotect", "reinforce", "demote", "quarantine", "restore"},
    ),
    "episode": frozenset({"quarantine", "restore"}),
    "relation": frozenset({"reinforce", "demote"}),
    "entity": frozenset(),
}


def _normalized_reason(reason: str) -> str:
    normalized = (reason or "manual").strip() or "manual"
    return normalized[:16]


def _strength(value: Optional[float], *, default: float) -> float:
    amount = default if value is None else value
    if amount < 0.05 or amount > 1.0:
        raise ValidationError(reason="权重调整幅度必须在 0.05 与 1.0 之间")
    return amount


async def _delete_vectors(ids: Sequence[int]) -> bool:
    """批量删点；失败只记日志，由调用方决定是否标记 `vectors_failed`。"""
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

    if not ids:
        return True
    try:
        deleted = await memory_qdrant_manager.delete_paragraphs(list(ids))
    except Exception as e:
        logger.error(f"删除段落向量失败: count={len(ids)}, error={e}")
        return False
    if deleted < len(ids):
        logger.error(f"删除段落向量不完整: requested={len(ids)}, deleted={deleted}")
        return False
    return True


async def quarantine(row: DBMemParagraph | DBMemEpisode, *, reason: str = "manual") -> None:
    """隔离一条记忆：先写数据库，提交后删向量。

    行已隔离时是空操作（幂等）；已删除 / 已清理的行不能隔离——那不是「把一条正常记忆
    收起来」，而是改变一条已经不在注入集合里的行的语义。
    """
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

    if isinstance(row, DBMemParagraph):
        current = paragraph_status(row)
    elif isinstance(row, DBMemEpisode):
        current = episode_status(row)
    else:
        raise ValidationError(reason="该类型不支持隔离")

    if current == "quarantined":
        return
    if current != "active":
        raise ValidationError(reason="只有正常状态的记忆可以隔离")

    now = datetime.now(timezone.utc)
    row.is_inactive = True
    row.quarantined_at = now
    row.quarantine_reason = _normalized_reason(reason)
    update_fields = ["is_inactive", "quarantined_at", "quarantine_reason", "update_time"]
    if isinstance(row, DBMemParagraph):
        row.last_manual_action = "quarantine"
        row.last_manual_action_at = now
        update_fields.extend(["last_manual_action", "last_manual_action_at"])
    await row.save(update_fields=update_fields)

    if isinstance(row, DBMemParagraph):
        # 数据库已经提交：删点失败时点还在，但召回回表按库里的 is_inactive 排除
        # （`retriever.py` 的段落回表），取不到行就不会注入，因此记 ERROR 而不是回滚。
        try:
            deleted = await memory_qdrant_manager.delete_paragraph(row.id)
        except Exception as e:
            logger.error(f"隔离后删除段落向量失败: paragraph_id={row.id}, error={e}")
            return
        if not deleted:
            logger.error(f"隔离后删除段落向量失败: paragraph_id={row.id}")


async def restore(row: DBMemParagraph | DBMemEpisode) -> None:
    """放回一条隔离的记忆。

    段落先重嵌并写点，再改数据库；数据库写失败时尽力把刚写的点删掉（fail-closed）。
    episode 没有向量，只改数据库。
    """
    from nekro_agent.services.memory.embedding_service import embed_text
    from nekro_agent.services.memory.persons import build_paragraph_payload
    from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager

    is_paragraph = isinstance(row, DBMemParagraph)
    if is_paragraph:
        current = paragraph_status(row)
    elif isinstance(row, DBMemEpisode):
        current = episode_status(row)
    else:
        raise ValidationError(reason="该类型不支持放回")
    if current != "quarantined":
        raise ValidationError(reason="只有隔离状态的记忆可以放回")

    now = datetime.now(timezone.utc)
    if is_paragraph and isinstance(row, DBMemParagraph):
        # 嵌入失败就直接抛出，数据库与向量都没有改动。
        embedding = await embed_text(row.content)
        old_state = (row.is_inactive, row.quarantined_at, row.quarantine_reason, row.blocked_round_hits)
        row.is_inactive = False
        row.quarantined_at = None
        row.quarantine_reason = ""
        row.blocked_round_hits = 0
        row.last_manual_action = "restore"
        row.last_manual_action_at = now
        try:
            written = await memory_qdrant_manager.upsert_paragraph(
                paragraph_id=row.id,
                embedding=embedding,
                payload=await build_paragraph_payload(row),
            )
            if not written:
                raise RuntimeError("向量写入返回失败")
        except Exception as e:
            row.is_inactive, row.quarantined_at, row.quarantine_reason, row.blocked_round_hits = old_state
            logger.error(f"放回段落时写入向量失败: paragraph_id={row.id}, error={e}")
            try:
                await memory_qdrant_manager.delete_paragraph(row.id)
            except Exception as delete_error:
                logger.error(f"放回失败后清理向量也失败: paragraph_id={row.id}, error={delete_error}")
            raise

        update_fields = [
            "is_inactive",
            "quarantined_at",
            "quarantine_reason",
            "blocked_round_hits",
            "last_manual_action",
            "last_manual_action_at",
            "update_time",
        ]
        if not row.embedding_ref:
            row.embedding_ref = str(row.id)
            update_fields.append("embedding_ref")
        try:
            await row.save(update_fields=update_fields)
        except Exception:
            # 行仍是隔离状态，但点已经写成正常状态；删掉它才是 fail-closed。
            try:
                await memory_qdrant_manager.delete_paragraph(row.id)
            except Exception as delete_error:
                logger.error(f"放回写库失败后清理向量也失败: paragraph_id={row.id}, error={delete_error}")
            raise
        return

    # episode：没有向量，只改数据库。
    assert isinstance(row, DBMemEpisode)
    row.is_inactive = False
    row.quarantined_at = None
    row.quarantine_reason = ""
    row.blocked_round_hits = 0
    await row.save(
        update_fields=[
            "is_inactive",
            "quarantined_at",
            "quarantine_reason",
            "blocked_round_hits",
            "update_time",
        ],
    )


async def quarantine_paragraphs(ids: Iterable[int], *, reason: str = "manual") -> list[int]:
    """批量隔离段落：一条 UPDATE 只改正常状态的行，返回实际改动的 ID，提交后批量删点。

    供管理端批量使用。先按批次查出「当前仍是正常状态」的 ID，再带着 `is_inactive=False`
    更新，重复执行时已隔离的行不会被再写一次。
    """
    changed = await mark_paragraphs_quarantined(ids, reason=reason)
    await _delete_vectors(changed)
    return changed


async def mark_paragraphs_quarantined(ids: Iterable[int], *, reason: str = "manual") -> list[int]:
    """只改数据库、不删向量的批量隔离，返回实际改动的 ID。

    给需要把隔离与别的写入放进**同一个事务**的调用方（自动隔离：加计数与转隔离要么都
    提交、要么都不提交）。向量不参与数据库事务，调用方须在事务提交后再用
    :func:`delete_paragraph_vectors` 删点。
    """
    unique = _unique_ids(ids)
    changed: list[int] = []
    now = datetime.now(timezone.utc)
    normalized_reason = _normalized_reason(reason)
    for start in range(0, len(unique), _BATCH_SIZE):
        chunk = unique[start : start + _BATCH_SIZE]
        if not chunk:
            continue
        matched = [int(value) for value in await DBMemParagraph.filter(id__in=chunk, is_inactive=False).values_list("id", flat=True)]
        if not matched:
            continue
        await DBMemParagraph.filter(id__in=matched, is_inactive=False).update(
            is_inactive=True,
            quarantined_at=now,
            quarantine_reason=normalized_reason,
            last_manual_action="quarantine",
            last_manual_action_at=now,
        )
        changed.extend(matched)
    return changed


async def delete_paragraph_vectors(ids: Sequence[int]) -> bool:
    """批量删点；失败只记日志并返回 False（召回回表按 is_inactive 排除，不会因此注入）。"""
    return await _delete_vectors(ids)


async def quarantine_episodes(ids: Iterable[int], *, reason: str = "manual") -> list[int]:
    """批量隔离 episode；episode 没有向量，改完数据库即完成。"""
    unique = _unique_ids(ids)
    changed: list[int] = []
    now = datetime.now(timezone.utc)
    normalized_reason = _normalized_reason(reason)
    for start in range(0, len(unique), _BATCH_SIZE):
        chunk = unique[start : start + _BATCH_SIZE]
        if not chunk:
            continue
        matched = [int(value) for value in await DBMemEpisode.filter(id__in=chunk, is_inactive=False).values_list("id", flat=True)]
        if not matched:
            continue
        await DBMemEpisode.filter(id__in=matched, is_inactive=False).update(
            is_inactive=True,
            quarantined_at=now,
            quarantine_reason=normalized_reason,
        )
        changed.extend(matched)
    return changed


async def apply_action(
    memory_type: str,
    row: DBMemParagraph | DBMemEpisode | DBMemRelation | DBMemEntity,
    action: str,
    *,
    value: Optional[float] = None,
) -> None:
    """按白名单执行一个动作；数值动作的默认值与工作区页一致。"""
    allowed = _ACTION_WHITELIST.get(memory_type)
    if allowed is None:
        raise ValidationError(reason=f"未知的记忆类型: {memory_type}")
    if action not in allowed:
        raise ValidationError(reason=f"{memory_type} 不支持动作 {action}")

    now = datetime.now(timezone.utc)
    if action == "reinforce":
        if not isinstance(row, (DBMemParagraph, DBMemRelation)):
            raise ValidationError(reason="该类型不支持强化")
        await row.reinforce(_strength(value, default=0.2))
        return

    if action == "demote":
        delta = _strength(value, default=0.2)
        if isinstance(row, DBMemParagraph):
            row.manual_weight_delta -= delta
            row.last_manual_action = "demote"
            row.last_manual_action_at = now
            await row.save(
                update_fields=["manual_weight_delta", "last_manual_action", "last_manual_action_at", "update_time"],
            )
        elif isinstance(row, DBMemRelation):
            row.base_weight = max(0.0, row.base_weight - delta)
            await row.save(update_fields=["base_weight", "update_time"])
        else:
            raise ValidationError(reason="该类型不支持降权")
        return

    if action in {"freeze", "unfreeze"}:
        if not isinstance(row, DBMemParagraph):
            raise ValidationError(reason="当前仅段落记忆支持冻结")
        row.is_frozen = action == "freeze"
        row.last_manual_action = action
        row.last_manual_action_at = now
        await row.save(update_fields=["is_frozen", "last_manual_action", "last_manual_action_at", "update_time"])
        return

    if action in {"protect", "unprotect"}:
        if not isinstance(row, DBMemParagraph):
            raise ValidationError(reason="当前仅段落记忆支持保护")
        row.is_protected = action == "protect"
        row.last_manual_action = action
        row.last_manual_action_at = now
        await row.save(update_fields=["is_protected", "last_manual_action", "last_manual_action_at", "update_time"])
        return

    if action == "quarantine":
        await quarantine(row)
        return

    if action == "restore":
        await restore(row)
        return

    raise ValidationError(reason=f"未知的动作: {action}")


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


@dataclass
class HardDeleteReport:
    """一次真删连带删掉的行数；`vectors_failed` 只表示提交后删点是否完整。"""

    paragraphs: int = 0
    persons: int = 0
    relations: int = 0
    episodes: int = 0
    entities: int = 0
    reinforcement_logs: int = 0
    vectors_failed: bool = False


async def soft_delete(
    memory_type: str,
    row: DBMemParagraph | DBMemEpisode | DBMemRelation | DBMemEntity,
) -> None:
    """软删：写 `is_inactive`，段落 / episode 清掉隔离字段，段落删点。"""
    now = datetime.now(timezone.utc)
    if memory_type == "paragraph" and isinstance(row, DBMemParagraph):
        row.is_inactive = True
        row.quarantined_at = None
        row.quarantine_reason = ""
        row.last_manual_action = "delete"
        row.last_manual_action_at = now
        await row.save(
            update_fields=[
                "is_inactive",
                "quarantined_at",
                "quarantine_reason",
                "last_manual_action",
                "last_manual_action_at",
                "update_time",
            ],
        )
        await _delete_vectors([row.id])
        return

    if memory_type == "episode" and isinstance(row, DBMemEpisode):
        row.is_inactive = True
        row.quarantined_at = None
        row.quarantine_reason = ""
        await row.save(update_fields=["is_inactive", "quarantined_at", "quarantine_reason", "update_time"])
        # 按 episode_id 定位，不加工作区条件：episode 是全局主键，成员段落可能跨工作区
        # （存量数据），漏清会让它们继续挂在一个已删除的 episode 下。
        await DBMemParagraph.filter(episode_id=row.id).update(episode_id=None, episode_phase=None)
        return

    if memory_type in {"relation", "entity"} and isinstance(row, (DBMemRelation, DBMemEntity)):
        row.is_inactive = True
        await row.save(update_fields=["is_inactive", "update_time"])
        return

    raise ValidationError(reason=f"未知的记忆类型: {memory_type}")


def _unique_ids(ids: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(int(item_id) for item_id in ids))


def _chunks(values: Sequence[int]) -> list[list[int]]:
    return [list(values[start : start + _BATCH_SIZE]) for start in range(0, len(values), _BATCH_SIZE)]


async def _episode_ids_of_paragraphs(chunk: Sequence[int]) -> set[int]:
    rows = await DBMemParagraph.filter(id__in=list(chunk)).values_list("episode_id", flat=True)
    return {int(value) for value in rows if value is not None}


async def _paragraph_relation_ids(chunk: Sequence[int]) -> list[int]:
    rows = await DBMemRelation.filter(paragraph_id__in=list(chunk)).values_list("id", flat=True)
    return [int(value) for value in rows]


async def preview_hard_delete_paragraphs(ids: Iterable[int]) -> HardDeleteReport:
    """只数不删：批量预览与确认框要显示的连带条数。"""
    unique = _unique_ids(ids)
    report = HardDeleteReport()
    if not unique:
        return report

    episode_ids: set[int] = set()
    for chunk in _chunks(unique):
        report.persons += await DBMemParagraphPerson.filter(paragraph_id__in=chunk).count()
        report.reinforcement_logs += await DBMemReinforcementLog.filter(
            target_type=TargetType.PARAGRAPH,
            target_id__in=chunk,
        ).count()
        relation_ids = await _paragraph_relation_ids(chunk)
        report.relations += len(relation_ids)
        for relation_chunk in _chunks(relation_ids):
            report.reinforcement_logs += await DBMemReinforcementLog.filter(
                target_type=TargetType.RELATION,
                target_id__in=relation_chunk,
            ).count()
        episode_ids |= await _episode_ids_of_paragraphs(chunk)
        report.paragraphs += await DBMemParagraph.filter(id__in=chunk).count()
    for chunk in _chunks(sorted(episode_ids)):
        report.episodes += await DBMemEpisode.filter(id__in=chunk).count()
    return report


async def hard_delete_paragraphs(ids: Iterable[int]) -> HardDeleteReport:
    """真删段落及其连带数据：一个事务里删 PostgreSQL，提交后删向量。

    删除顺序照设计稿：强化日志 → 涉及的人 → 关系（连带它们的日志）→ 包含这些段落的
    episode（整条删，其余成员清空归属）→ 段落行。关系先于段落删，episode 先于段落删都
    只是风格；真正必须的是关联行先于主行，否则会留下指向不存在段落的孤儿行。
    """
    unique = _unique_ids(ids)
    report = HardDeleteReport()
    if not unique:
        return report

    episode_ids: set[int] = set()
    async with in_transaction():
        for chunk in _chunks(unique):
            report.persons += await DBMemParagraphPerson.filter(paragraph_id__in=chunk).delete()
            report.reinforcement_logs += await DBMemReinforcementLog.filter(
                target_type=TargetType.PARAGRAPH,
                target_id__in=chunk,
            ).delete()
            relation_ids = await _paragraph_relation_ids(chunk)
            for relation_chunk in _chunks(relation_ids):
                report.reinforcement_logs += await DBMemReinforcementLog.filter(
                    target_type=TargetType.RELATION,
                    target_id__in=relation_chunk,
                ).delete()
            report.relations += await DBMemRelation.filter(paragraph_id__in=chunk).delete()
            episode_ids |= await _episode_ids_of_paragraphs(chunk)
            report.paragraphs += await DBMemParagraph.filter(id__in=chunk).delete()

        for chunk in _chunks(sorted(episode_ids)):
            await DBMemParagraph.filter(episode_id__in=chunk).update(episode_id=None, episode_phase=None)
            report.episodes += await DBMemEpisode.filter(id__in=chunk).delete()

    report.vectors_failed = not await _delete_vectors(unique)
    return report


async def hard_delete_episodes(ids: Iterable[int]) -> HardDeleteReport:
    """真删 episode：只删 episode 行，成员段落的归属清空，段落本身不动。"""
    unique = _unique_ids(ids)
    report = HardDeleteReport()
    if not unique:
        return report
    async with in_transaction():
        for chunk in _chunks(unique):
            await DBMemParagraph.filter(episode_id__in=chunk).update(episode_id=None, episode_phase=None)
            report.episodes += await DBMemEpisode.filter(id__in=chunk).delete()
    return report


async def hard_delete_relations(ids: Iterable[int]) -> HardDeleteReport:
    """真删关系，连同指向它们的强化日志。"""
    unique = _unique_ids(ids)
    report = HardDeleteReport()
    if not unique:
        return report
    async with in_transaction():
        for chunk in _chunks(unique):
            report.reinforcement_logs += await DBMemReinforcementLog.filter(
                target_type=TargetType.RELATION,
                target_id__in=chunk,
            ).delete()
            report.relations += await DBMemRelation.filter(id__in=chunk).delete()
    return report


async def hard_delete_entities(ids: Iterable[int]) -> HardDeleteReport:
    """真删实体；引用这些实体的关系一并真删，连同它们的强化日志。

    实体本身只是一个名字，变成孤儿的关系才是「还在被召回」的那一半，所以先删关系。
    """
    unique = _unique_ids(ids)
    report = HardDeleteReport()
    if not unique:
        return report

    relation_ids: set[int] = set()
    for chunk in _chunks(unique):
        rows = await DBMemRelation.filter(
            Q(subject_entity_id__in=chunk) | Q(object_entity_id__in=chunk),
        ).values_list("id", flat=True)
        relation_ids |= {int(value) for value in rows}

    async with in_transaction():
        for chunk in _chunks(sorted(relation_ids)):
            report.reinforcement_logs += await DBMemReinforcementLog.filter(
                target_type=TargetType.RELATION,
                target_id__in=chunk,
            ).delete()
            report.relations += await DBMemRelation.filter(id__in=chunk).delete()
        for chunk in _chunks(unique):
            report.entities += await DBMemEntity.filter(id__in=chunk).delete()
    return report


# ---------------------------------------------------------------------------
# 实例级统计与清空（划转 / 入组对话框）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstanceMemoryCounts:
    """一个实例留在库里的记忆规模，供划转与入组对话框如实回显。

    `paragraphs` / `quarantined` 只数**域行**：工作区行跟着工作区走，不随实例划转，
    对话框里说的「跟着实例走」也不包括它们。`summaries` 按「频道 + 人设」计份数而不是
    版本数——一个频道最多保留几个版本，版本数不是运营能感知的单位。
    """

    paragraphs: int = 0
    quarantined: int = 0
    summaries: int = 0
    prompt_rounds: int = 0
    worldbook_books: int = 0
    worldbook_entries: int = 0
    affection_rows: int = 0
    affection_events: int = 0


async def count_instance_memory(adapter_key: str, instance_key: str) -> InstanceMemoryCounts:
    """数一个实例的记忆条数。

    可见范围仍由 :class:`AdminMemoryScope` 产出：这里要的是「这个实例对」的域行，
    不是「某个频道此刻能看到什么」，因此不走 `MemoryScope`。摘要、明细按频道归属，
    没有工作区分区，`AdminFilter` 只带实例对。

    世界书按书表的范围条件数：书的归属实例对是查询条件（走 `scope.apply`，不自己拼
    `adapter_key` / `instance_key`），条目再按书 ID 的子查询数——条目表没有归属列，
    它的范围永远算在书上，这里也不另写一套判据。
    """
    scope = AdminMemoryScope.for_pairs([(adapter_key, instance_key)])
    domain_filter = AdminFilter(partition="domain")

    paragraph_queryset = scope.apply(DBMemParagraph.all(), MemTable.PARAGRAPH, domain_filter)
    paragraphs = await paragraph_queryset.filter(is_inactive=False).count()
    quarantined = await paragraph_queryset.filter(is_inactive=True, quarantined_at__isnull=False).count()

    summary_queryset = scope.apply(DBMemChannelSummary.all(), MemTable.SUMMARY, AdminFilter())
    # DISTINCT 交给数据库：同一（频道, 人设）下每个保留的版本都是一行，这里要的是份数。
    summary_keys = await summary_queryset.distinct().values_list("chat_key", "preset_id")
    prompt_rounds = await scope.apply(DBMemPromptRound.all(), MemTable.PROMPT_ROUND, AdminFilter()).count()

    book_queryset = scope.apply(DBWbBook.all(), MemTable.WB_BOOK, AdminFilter())
    worldbook_books = await book_queryset.count()
    worldbook_entries = await DBWbEntry.filter(book_id__in=Subquery(book_queryset.values("id"))).count()

    affection_filter = {
        "adapter_key": adapter_key,
        "instance_key": instance_key,
        "domain_group_id": SELF_DOMAIN_GROUP_ID,
    }
    affection_rows = await DBAffection.filter(**affection_filter).count()
    affection_events = await DBAffectionEvent.filter(**affection_filter).count()

    return InstanceMemoryCounts(
        paragraphs=paragraphs,
        quarantined=quarantined,
        summaries=len(list(summary_keys)),
        prompt_rounds=prompt_rounds,
        worldbook_books=worldbook_books,
        worldbook_entries=worldbook_entries,
        affection_rows=affection_rows,
        affection_events=affection_events,
    )


async def purge_instance_domain_memory(adapter_key: str, instance_key: str) -> dict[str, int]:
    """真删一个实例的**域行**记忆、摘要、提示词明细与自成一域的好感，返回各类条数。

    ## 删什么、不删什么

    - 段落走 :func:`hard_delete_paragraphs`：它会连带「涉及的人」、强化日志、关系与
      包含这些段落的 episode，并在自己的事务提交后删向量；
    - 段落删完后仍留在本实例域内的 episode / 关系 / 实体再各走一次对应的真删函数，
      避免「段落删了、只挂在 episode 或实体上的行还在」；
    - 摘要与摘要暂停行、提示词明细按实例对真删；自成一域的好感（`domain_group_id = 0`
      且本行实例对）与事件整行删，软删过的事件也一样（删除就是要它看不见）；
    - 世界书按书表的范围条件真删（条目由外键级联删除）；条目在本进程正则熔断表里的
      记录在事务提交后逐个清掉，与删书接口同一个理由；
    - 工作区行（`workspace_id > 0`）一条都不碰：它们跟着工作区走，工作区有自己的归属。

    ## 事务与向量顺序

    `hard_delete_paragraphs` 自己开一个事务，并在提交后才删向量——向量库不参与数据库
    事务，把它放进事务里失败会连带回滚已经正确的删除。因此段落这一步单独调用，不包进
    外层事务。其余表没有向量，放在**一个**事务里（其中的 `hard_delete_*` 是嵌套保存点），
    失败时整体回滚，不会留下「摘要删了、好感还在」这种更难解释的中间态。

    代价是段落与其余表之间没有跨事务原子性：段落删掉之后其余表那一步失败时，这个实例
    的核心记忆已经不可恢复地删掉了（真删本来就是不可逆操作）。调用方会在失败时让请求
    报错，避免操作者以为整次清空没发生。
    """
    scope = AdminMemoryScope.for_pairs([(adapter_key, instance_key)])
    domain_filter = AdminFilter(partition="domain")

    paragraph_ids = [
        int(value)
        for value in await scope.apply(DBMemParagraph.all(), MemTable.PARAGRAPH, domain_filter).values_list(
            "id",
            flat=True,
        )
    ]

    counts: dict[str, int] = {
        "paragraphs": 0,
        "persons": 0,
        "relations": 0,
        "episodes": 0,
        "entities": 0,
        "reinforcement_logs": 0,
        "summaries": 0,
        "summary_states": 0,
        "prompt_rounds": 0,
        "worldbook_books": 0,
        "worldbook_entries": 0,
        "affection_rows": 0,
        "affection_events": 0,
        "vectors_failed": 0,
    }

    paragraph_report = await hard_delete_paragraphs(paragraph_ids)
    counts["paragraphs"] = paragraph_report.paragraphs
    counts["persons"] = paragraph_report.persons
    counts["relations"] = paragraph_report.relations
    counts["episodes"] = paragraph_report.episodes
    counts["entities"] = paragraph_report.entities
    counts["reinforcement_logs"] = paragraph_report.reinforcement_logs
    counts["vectors_failed"] = int(paragraph_report.vectors_failed)

    async with in_transaction():
        counts["summaries"] = await scope.apply(
            DBMemChannelSummary.all(),
            MemTable.SUMMARY,
            AdminFilter(),
        ).delete()
        counts["summary_states"] = await scope.apply(
            DBMemChannelSummaryState.all(),
            MemTable.SUMMARY_STATE,
            AdminFilter(),
        ).delete()
        counts["prompt_rounds"] = await scope.apply(
            DBMemPromptRound.all(),
            MemTable.PROMPT_ROUND,
            AdminFilter(),
        ).delete()
        # 条目 ID 要在删书之前取：删完之后查不到它们，而进程内的正则熔断表还挂着这些 ID。
        worldbook_entry_ids = [
            int(value)
            for value in await DBWbEntry.filter(
                book_id__in=Subquery(scope.apply(DBWbBook.all(), MemTable.WB_BOOK, AdminFilter()).values("id")),
            ).values_list("id", flat=True)
        ]
        counts["worldbook_entries"] = len(worldbook_entry_ids)
        counts["worldbook_books"] = await scope.apply(DBWbBook.all(), MemTable.WB_BOOK, AdminFilter()).delete()
        counts["affection_events"] = await DBAffectionEvent.filter(
            adapter_key=adapter_key,
            instance_key=instance_key,
            domain_group_id=SELF_DOMAIN_GROUP_ID,
        ).delete()
        counts["affection_rows"] = await DBAffection.filter(
            adapter_key=adapter_key,
            instance_key=instance_key,
            domain_group_id=SELF_DOMAIN_GROUP_ID,
        ).delete()

        # 段落删掉之后重新取一次：被连带删掉的 episode / 关系 / 实体不应再出现在这里。
        remaining_episode_ids = [
            int(value)
            for value in await scope.apply(DBMemEpisode.all(), MemTable.EPISODE, domain_filter).values_list(
                "id",
                flat=True,
            )
        ]
        episode_report = await hard_delete_episodes(remaining_episode_ids)
        counts["episodes"] += episode_report.episodes

        remaining_relation_ids = [
            int(value)
            for value in await scope.apply(DBMemRelation.all(), MemTable.RELATION, domain_filter).values_list(
                "id",
                flat=True,
            )
        ]
        relation_report = await hard_delete_relations(remaining_relation_ids)
        counts["relations"] += relation_report.relations
        counts["reinforcement_logs"] += relation_report.reinforcement_logs

        remaining_entity_ids = [
            int(value)
            for value in await scope.apply(DBMemEntity.all(), MemTable.ENTITY, domain_filter).values_list(
                "id",
                flat=True,
            )
        ]
        entity_report = await hard_delete_entities(remaining_entity_ids)
        counts["entities"] += entity_report.entities
        counts["relations"] += entity_report.relations
        counts["reinforcement_logs"] += entity_report.reinforcement_logs

    # 好感快照有缓存：行已经删了，缓存若还在，下一个读请求会把刚清掉的那份又念出来。
    if counts["affection_rows"] or counts["affection_events"]:
        from nekro_agent.services.affection.store import invalidate_cache

        invalidate_cache()

    # 外键级联删掉的条目不经过 `delete_entry`：正则熔断表里的记录必须显式清掉，
    # 否则它会一直指着已经不存在（或已被别人占用）的条目主键。
    from nekro_agent.services.worldbook import safe_regex

    for worldbook_entry_id in worldbook_entry_ids:
        safe_regex.forget_entry(worldbook_entry_id)

    return counts


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------


@dataclass
class BulkResult:
    """批量操作的结果；`cascade` 只在真删时有值。"""

    matched: int
    affected: int = 0
    failed_ids: list[int] = field(default_factory=list)
    cascade: Optional[HardDeleteReport] = None


_BULK_ACTIONS = frozenset({"delete", "hard_delete", "quarantine", "restore", "freeze", "unfreeze"})


async def _bulk_matched_ids(
    scope: AdminMemoryScope,
    f: AdminFilter,
    ids: Optional[list[int]],
    filters: Optional[ParagraphListFilters],
) -> list[int]:
    if ids is not None:
        unique = _unique_ids(ids)
        if not unique:
            return []
        queryset = scope.apply(DBMemParagraph.filter(id__in=unique), MemTable.PARAGRAPH, f)
    else:
        assert filters is not None
        queryset = _filtered_paragraphs(scope, f, filters)
    return [int(value) for value in await queryset.values_list("id", flat=True)]


async def _bulk_soft_delete(ids: Sequence[int]) -> int:
    """批量软删：正常与隔离两种状态都能删（与单条软删一致），已删除 / 已清理的不再碰。

    隔离区页面上的「批量删除」就是从隔离状态删；只认正常状态会让那个按钮什么都不做。
    """
    now = datetime.now(timezone.utc)
    affected = 0
    changed: list[int] = []
    deletable = Q(is_inactive=False) | Q(quarantined_at__isnull=False)
    for chunk in _chunks(list(ids)):
        matched = [
            int(value)
            for value in await DBMemParagraph.filter(Q(id__in=chunk) & deletable).values_list("id", flat=True)
        ]
        if not matched:
            continue
        affected += await DBMemParagraph.filter(Q(id__in=matched) & deletable).update(
            is_inactive=True,
            quarantined_at=None,
            quarantine_reason="",
            last_manual_action="delete",
            last_manual_action_at=now,
        )
        changed.extend(matched)
    await _delete_vectors(changed)
    return affected


async def soft_delete_paragraphs(ids: Sequence[int]) -> int:
    """按 ID 批量软删段落，返回真正被改动的条数。

    给记忆之外的调用方（好感里程碑的重置联动）用的公开入口。它**只做委托**，不复制
    `_bulk_soft_delete` 的判断：隔离中与正常的都能删、已删除的不再碰、删完清向量，
    这几条口径必须只有一份，否则两条路径迟早会对「隔离中的段落算不算删掉」给出不同答案。
    """
    unique = _unique_ids(list(ids))
    if not unique:
        return 0
    return await _bulk_soft_delete(unique)


async def _bulk_set_frozen(ids: Sequence[int], *, frozen: bool) -> int:
    now = datetime.now(timezone.utc)
    affected = 0
    for chunk in _chunks(list(ids)):
        affected += await DBMemParagraph.filter(id__in=chunk).update(
            is_frozen=frozen,
            last_manual_action="freeze" if frozen else "unfreeze",
            last_manual_action_at=now,
        )
    return affected


async def bulk_paragraphs(
    scope: AdminMemoryScope,
    *,
    ids: Optional[list[int]],
    f: Optional[AdminFilter],
    filters: Optional[ParagraphListFilters],
    action: str,
    dry_run: bool,
    expected_count: Optional[int],
) -> BulkResult:
    """批量操作段落。`ids` 与 `filters` 二选一；执行前必须带与当场重算一致的条数。"""
    if action not in _BULK_ACTIONS:
        raise ValidationError(reason=f"不支持的批量动作: {action}")
    if (ids is None) == (filters is None):
        raise ValidationError(reason="批量操作必须在 ID 列表与筛选条件之间二选一")

    effective_filter = f if f is not None else AdminFilter()
    matched_ids = await _bulk_matched_ids(scope, effective_filter, ids, filters)
    matched = len(matched_ids)

    limit = _BULK_RESTORE_LIMIT if action == "restore" else _BULK_LIMIT
    if matched > limit:
        raise ValidationError(reason=f"匹配 {matched} 条，超过单次上限 {limit} 条，请缩小筛选")

    if dry_run:
        cascade = await preview_hard_delete_paragraphs(matched_ids) if action == "hard_delete" else None
        return BulkResult(matched=matched, affected=0, failed_ids=[], cascade=cascade)

    if expected_count is None or expected_count != matched:
        raise ConflictError(
            resource=f"批量操作预览（期望 {expected_count} 条，当前 {matched} 条，请重新预览）",
            detail=f"expected_count={expected_count}, matched={matched}",
        )

    if action == "hard_delete":
        cascade = await hard_delete_paragraphs(matched_ids)
        return BulkResult(matched=matched, affected=cascade.paragraphs, failed_ids=[], cascade=cascade)

    if action == "restore":
        affected = 0
        failed_ids: list[int] = []
        rows = await DBMemParagraph.filter(id__in=matched_ids).order_by("id")
        for row in rows:
            try:
                await restore(row)
            except Exception as e:
                logger.warning(f"批量放回单条失败: paragraph_id={row.id}, error={e}")
                failed_ids.append(row.id)
                continue
            affected += 1
        return BulkResult(matched=matched, affected=affected, failed_ids=failed_ids, cascade=None)

    if action == "quarantine":
        changed = await quarantine_paragraphs(matched_ids)
        return BulkResult(matched=matched, affected=len(changed), failed_ids=[], cascade=None)

    if action == "delete":
        affected = await _bulk_soft_delete(matched_ids)
        return BulkResult(matched=matched, affected=affected, failed_ids=[], cascade=None)

    frozen = action == "freeze"
    affected = await _bulk_set_frozen(matched_ids, frozen=frozen)
    return BulkResult(matched=matched, affected=affected, failed_ids=[], cascade=None)


# ---------------------------------------------------------------------------
# 筛选项
# ---------------------------------------------------------------------------


async def scope_options(user: DBUser, scope: AdminMemoryScope) -> dict[str, list[dict[str, Any]]]:
    """筛选栏的选项：实例、频道、人设。

    频道的归属按实例对成对比较（`instance_refs_filter`），不能只比 `instance_key`：
    另一个适配器下可以有同名实例归别人。
    """
    from nekro_agent.services.identity_admin import preset_names
    from nekro_agent.services.user.tenant_perm import (
        accessible_instance_refs,
        get_tenant_scope,
        instance_refs_filter,
    )

    instance_rows = await DBAdapterInstance.all().order_by("id")
    if get_tenant_scope(user) is None:
        visible_instances = list(instance_rows)
    else:
        allowed_refs = await accessible_instance_refs(user)
        visible_instances = [
            row for row in instance_rows if (row.adapter_key, row.instance_key) in allowed_refs
        ]
    instances = [
        {
            "adapter_key": row.adapter_key,
            "instance_key": row.instance_key,
            "display_name": row.display_name or row.instance_key,
        }
        for row in visible_instances
    ]

    if scope.pairs is None:
        channel_queryset = DBChatChannel.all()
    else:
        channel_queryset = DBChatChannel.filter(instance_refs_filter(scope.pairs))
    channel_rows = await channel_queryset.order_by("-update_time").limit(500)
    channels = [
        {
            "chat_key": row.chat_key,
            "channel_name": row.channel_name or "",
            "channel_type": row.channel_type or "",
        }
        for row in channel_rows
    ]

    # DISTINCT 交给数据库：平台方全量视图下段落表可能有几十万行，逐行取回再去重太重。
    preset_id_rows = (
        await scope.apply(DBMemParagraph.all(), MemTable.PARAGRAPH, AdminFilter())
        .distinct()
        .values_list("preset_id", flat=True)
    )
    preset_ids = sorted({int(value) for value in preset_id_rows})
    names = await preset_names(preset_ids)
    presets: list[dict[str, Any]] = []
    for preset_id in preset_ids:
        if preset_id == 0:
            presets.append({"preset_id": 0, "name": "无人设档"})
        elif preset_id == CONSOLE_ONLY_PRESET_ID:
            # 负数档不是"被删掉的人设"：它是控制台直发 CC 的结论专用的档。
            # 显示成「已删除的人设 #-1」会让管理员以为数据坏了，去清一件不该清的东西。
            presets.append({"preset_id": CONSOLE_ONLY_PRESET_ID, "name": "仅控制台（控制台直发 CC 的结论）"})
        else:
            presets.append({"preset_id": preset_id, "name": names.get(preset_id, f"已删除的人设 #{preset_id}")})

    return {"instances": instances, "channels": channels, "presets": presets}


async def resolve_person_filter(
    scope: AdminMemoryScope,
    account: str,
    *,
    adapter_key: str,
    instance_key: str,
) -> frozenset[str]:
    """把「某个人」展开成账号集合。

    选了实例时走该实例记忆域的别名展开（同一人的副号也要命中）；平台方全量视图没有
    实例，就没有域可解析，只能按传入的那一个账号本身筛。
    """
    from nekro_agent.services.identity_scope import resolve_memory_domain
    from nekro_agent.services.memory.persons import expand_accounts

    normalized = account.strip()
    if not normalized:
        return frozenset()
    if not instance_key:
        return frozenset({normalized})
    domain = await resolve_memory_domain(adapter_key, instance_key)
    return await expand_accounts(domain, [normalized])
