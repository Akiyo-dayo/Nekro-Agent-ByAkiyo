"""记忆管理接口的请求与响应模型。

列表一律是 `{total, items}`。段落列表项里的正文只截前 200 字：列表页要的是"能认出是哪
一条"，全文属于详情，而一页 100 条全文会把响应体撑大一个数量级。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ParagraphListItem(BaseModel):
    """段落列表的一行。"""

    id: int
    workspace_id: int
    adapter_key: str
    instance_key: str
    preset_id: int
    origin_chat_key: Optional[str] = None
    origin_chat_type: str = ""
    # 来源追溯：列表页要能标出「这段是里程碑」并回链到产生它的那条流水/任务
    origin_kind: Optional[str] = None
    origin_ref: Optional[str] = None
    summary: str = ""
    content: str
    cognitive_type: str
    knowledge_type: str
    status: str
    is_frozen: bool
    is_protected: bool
    blocked_round_hits: int
    quarantined_at: Optional[datetime] = None
    quarantine_reason: str = ""
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    person_accounts: list[str] = Field(default_factory=list)


class ParagraphListResponse(BaseModel):
    total: int
    items: list[ParagraphListItem]


class EpisodeListItem(BaseModel):
    """episode 列表的一行。"""

    id: int
    workspace_id: int
    adapter_key: str
    instance_key: str
    preset_id: int
    origin_chat_key: Optional[str] = None
    origin_chat_type: str = ""
    title: str
    narrative_summary: str
    status: str
    blocked_round_hits: int
    quarantined_at: Optional[datetime] = None
    quarantine_reason: str = ""
    time_start: Optional[datetime] = None
    time_end: Optional[datetime] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class EpisodeListResponse(BaseModel):
    total: int
    items: list[EpisodeListItem]


class QuarantineListItem(BaseModel):
    """隔离区的一行：段落与 episode 合并后的形状。"""

    memory_type: str
    id: int
    title_or_summary: str
    origin_chat_key: Optional[str] = None
    quarantined_at: Optional[datetime] = None
    quarantine_reason: str = ""
    blocked_round_hits: int = 0


class QuarantineListResponse(BaseModel):
    total: int
    items: list[QuarantineListItem]


class InstanceOption(BaseModel):
    adapter_key: str
    instance_key: str
    display_name: str


class ChannelOption(BaseModel):
    chat_key: str
    channel_name: str
    channel_type: str


class PresetOption(BaseModel):
    preset_id: int
    name: str


class ScopeOptionsResponse(BaseModel):
    """筛选栏的全部选项。"""

    instances: list[InstanceOption] = Field(default_factory=list)
    channels: list[ChannelOption] = Field(default_factory=list)
    presets: list[PresetOption] = Field(default_factory=list)


class MemoryItemDetail(BaseModel):
    """单条详情。

    四种记忆类型的列并不一样，因此只固定三列共有的信息，其余列原样透传：为每种类型各写
    一份模型会让"新增一列"同时要改模型与服务，而它们本来就只是 `to_dict` 的输出。
    """

    model_config = ConfigDict(extra="allow")

    id: int
    memory_type: str
    status: str


class MemoryItemUpdateRequest(BaseModel):
    """编辑请求。四种类型共用，按类型只取自己那几项。"""

    content: Optional[str] = None
    summary: Optional[str] = None
    person_accounts: Optional[list[str]] = None
    title: Optional[str] = None
    narrative_summary: Optional[str] = None


class MemoryItemActionRequest(BaseModel):
    action: str
    value: Optional[float] = None


class ActionResponse(BaseModel):
    ok: bool = True


class HardDeleteReportResponse(BaseModel):
    paragraphs: int = 0
    persons: int = 0
    relations: int = 0
    episodes: int = 0
    entities: int = 0
    reinforcement_logs: int = 0
    vectors_failed: bool = False


class ParagraphFilterSelection(BaseModel):
    """批量按筛选执行时的筛选条件，键名与列表端点的查询参数一致。

    分页与排序也保留在这里：前端会把整份筛选状态原样带过来。它们不影响"选中哪些行"，
    批量匹配时忽略即可；删掉字段反而会让整份状态被 422 拒绝。
    """

    partition: str = "all"
    chat_key: Optional[str] = None
    preset_id: Optional[int] = None
    person: str = ""
    status: str = "active"
    frozen: Optional[bool] = None
    protected: Optional[bool] = None
    cognitive_type: Optional[str] = None
    knowledge_type: Optional[str] = None
    time_from: Optional[str] = None
    time_to: Optional[str] = None
    q: Optional[str] = None
    # 来源类型：列表能按它筛，批量「按筛选全选」就必须认它，否则看着是里程碑、删掉的是全部来源
    origin_kind: Optional[str] = None
    sort: Optional[str] = None
    order: Optional[str] = None
    page: Optional[int] = None
    page_size: Optional[int] = None


class BulkSelection(BaseModel):
    ids: Optional[list[int]] = None
    filter: Optional[ParagraphFilterSelection] = None


class BulkParagraphsRequest(BaseModel):
    selection: BulkSelection
    action: str
    dry_run: bool = False
    expected_count: Optional[int] = None


class BulkResultResponse(BaseModel):
    matched: int
    affected: int = 0
    failed_ids: list[int] = Field(default_factory=list)
    cascade: Optional[HardDeleteReportResponse] = None


class MemoryTraceMessage(BaseModel):
    db_id: int
    message_id: str
    sender_name: str
    send_timestamp: int
    content_preview: str


class MemoryTraceResponse(BaseModel):
    workspace_id: int
    memory_type: str
    memory_id: int
    origin_kind: Optional[str] = None
    origin_ref: Optional[str] = None
    origin_chat_key: Optional[str] = None
    anchor_msg_id: Optional[str] = None
    anchor_msg_id_start: Optional[str] = None
    anchor_msg_id_end: Optional[str] = None
    anchor_timestamp_start: Optional[int] = None
    anchor_timestamp_end: Optional[int] = None
    cross_channel: bool = False
    messages: list[MemoryTraceMessage] = Field(default_factory=list)


class SummaryListEntry(BaseModel):
    """摘要列表的一行：一个（频道, 人设）的最新版与聚合信息。"""

    chat_key: str
    preset_id: int
    adapter_key: str
    instance_key: str
    id: int
    version: int
    content: str
    source: str
    create_time: Optional[datetime] = None
    version_count: int
    paused: bool


class SummaryListResponse(BaseModel):
    total: int
    items: list[SummaryListEntry]


class SummaryVersionItem(BaseModel):
    """摘要历史里的一版。"""

    id: int
    version: int
    content: str
    source: str
    source_version: Optional[int] = None
    edited_by: str = ""
    create_time: Optional[datetime] = None


class SummaryVersionsResponse(BaseModel):
    chat_key: str
    preset_id: int
    paused: bool
    paused_by: str = ""
    paused_at: Optional[datetime] = None
    versions: list[SummaryVersionItem]


class SummaryEditRequest(BaseModel):
    content: str
    base_version: int


class SummaryRollbackRequest(BaseModel):
    to_version: Optional[int] = None


class SummaryPauseRequest(BaseModel):
    paused: bool


class SummaryPauseResponse(BaseModel):
    paused: bool
    paused_by: str = ""
    paused_at: Optional[datetime] = None


class PromptRoundListItem(BaseModel):
    """提示词明细列表的一行：只给排查需要的计数与状态，正文在详情里。"""

    id: int
    chat_key: str
    preset_id: int
    outcome: str
    render_count: int
    memory_state: str
    memory_items_count: int
    memory_block_chars: int
    summary_state: str
    summary_version: Optional[int] = None
    summary_chars: int
    affection_shown: bool
    affection_chars: int
    affection_people: int
    # 这一轮好感块里注入的关系里程碑段落。它们也是记忆段落，能点到记忆页那一段。
    affection_milestone_ids: list[int] = Field(default_factory=list)
    # 世界书一栏：状态与真正注入的条目数、字数。`worldbook_count` 只数注入的条目，被预算
    # 跳过的不算（理由见 `routers/memory_admin` 的同名函数）。
    worldbook_state: str = "disabled"
    worldbook_count: int
    worldbook_chars: int = 0
    # 酒馆一栏：高级槽位闸、各槽位的字数与状态、深度注入与开场白。只记字数与状态，
    # 不记原文；形状随运行时演进，因此原样透传而不是逐键建模。
    tavern_items: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class PromptRoundListResponse(BaseModel):
    total: int
    items: list[PromptRoundListItem]


class PromptRoundMemoryItem(BaseModel):
    """明细里引用的一个记忆条目；带查询时的当前状态与预览。"""

    model_config = ConfigDict(extra="allow")

    type: str
    id: int
    status: str = ""
    preview: str = ""
    chars: int = 0
    renders: int = 0


class PromptRoundDetailResponse(BaseModel):
    """单轮提示词明细。"""

    id: int
    round_id: str
    chat_key: str
    adapter_key: str
    instance_key: str
    preset_id: int
    workspace_id: int
    outcome: str
    render_count: int
    memory_state: str
    memory_items: list[PromptRoundMemoryItem] = Field(default_factory=list)
    memory_block_chars: int
    cooldown_skipped: int
    summary_state: str
    summary_id: Optional[int] = None
    summary_version: Optional[int] = None
    summary_chars: int
    affection_shown: bool
    affection_chars: int
    affection_people: int
    # 好感块里注入的关系里程碑段落 ID，与记忆条目同一套状态查询（软删 / 隔离 / 清理都看得见）。
    affection_milestone_ids: list[int] = Field(default_factory=list)
    # 世界书：状态、引用到的条目（每条已补上「现在」的状态与书名标题，看不见的标 hidden）、
    # 本轮注入的字数。`worldbook_items` 里同时有注入的与被预算跳过的条目，靠各自的
    # `type` 区分。
    worldbook_state: str = "disabled"
    worldbook_items: list[dict[str, Any]] = Field(default_factory=list)
    worldbook_chars: int = 0
    # 酒馆一栏：与列表行同一份内容，详情里给全（服务层已给出这个键，响应模型不再丢）。
    tavern_items: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    summary: dict[str, Any] = Field(default_factory=dict)


def detail_from_payload(payload: dict[str, Any]) -> MemoryItemDetail:
    """把服务层返回的字典收成响应模型；未声明的列由 `extra="allow"` 原样保留。"""
    return MemoryItemDetail.model_validate(payload)
