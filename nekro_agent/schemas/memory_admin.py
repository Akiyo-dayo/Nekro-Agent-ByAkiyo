"""记忆管理接口的请求与响应模型。

列表一律是 `{total, items}`。段落列表项里的正文只截前 200 字：列表页要的是"能认出是哪
一条"，全文属于详情，而一页 100 条全文会把响应体撑大一个数量级。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class PersonLabelItem(BaseModel):
    """一段记忆里「涉及的人」的显示名

    `display_name` 取这个账号在来源频道里最近的昵称（没有就取好感记录里的）；`call_name` 是
    好感里模型给这个人记的称呼，属于不可信内容，只当纯文本显示。两者查不到时为空串。
    `name_source` 说明昵称从哪来：`chat`（来源频道）/ `affection`（好感记录：好感域共用的「最近
    一次见到的昵称」，可能是在别的群或私聊里看到的）/ 空串（没查到）。
    """

    account: str
    display_name: str = ""
    call_name: str = ""
    name_source: str = ""


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
    # 敏感事：只在来源频道里被想起；列表上要能看见、也能单独筛
    is_sensitive: bool = False
    blocked_round_hits: int
    quarantined_at: Optional[datetime] = None
    quarantine_reason: str = ""
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    person_accounts: list[str] = Field(default_factory=list)
    # 与 `person_accounts` 一一对应、顺序相同，带上显示名
    persons: list[PersonLabelItem] = Field(default_factory=list)
    # 所属事件；没归入任何事件时为空
    episode_id: Optional[int] = None


class ParagraphListResponse(BaseModel):
    total: int
    items: list[ParagraphListItem]


class MemoryPersonItem(BaseModel):
    """「记忆里出现过的人」的一行（按段数倒序）

    `account` 原样传给段落列表的 `person` 筛选，筛出来的就是这一组；`accounts` 是并进这一行的
    全部账号（选了实例时同一个人的几个账号并成一行）。
    """

    account: str
    accounts: list[str] = Field(default_factory=list)
    display_name: str = ""
    call_name: str = ""
    # 与 `PersonLabelItem.name_source` 同义
    name_source: str = ""
    paragraph_count: int
    last_paragraph_id: int = 0


class MemoryPersonListResponse(BaseModel):
    total: int
    items: list[MemoryPersonItem]


class MemoryTopicItem(BaseModel):
    """按事件分组的一行

    `episode_id` 为空的那一行是「没归入任何事件」的段落；`visible` 为假表示段落还指着这个事件，
    但事件已经没了或不在可见范围里，此时只给编号。
    """

    episode_id: Optional[int] = None
    title: str = ""
    narrative_summary: str = ""
    paragraph_count: int
    last_time: Optional[datetime] = None
    visible: bool = True


class MemoryTopicListResponse(BaseModel):
    total: int
    items: list[MemoryTopicItem]


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
    """隔离区的一行：段落与 episode 合并后的形状。

    `cognitive_type` / `knowledge_type` 只有段落有；episode 行是 `None`。
    """

    memory_type: str
    id: int
    title_or_summary: str
    origin_chat_key: Optional[str] = None
    quarantined_at: Optional[datetime] = None
    quarantine_reason: str = ""
    blocked_round_hits: int = 0
    cognitive_type: Optional[str] = None
    knowledge_type: Optional[str] = None


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
    sensitive: Optional[bool] = None
    cognitive_type: Optional[str] = None
    knowledge_type: Optional[str] = None
    time_from: Optional[str] = None
    time_to: Optional[str] = None
    q: Optional[str] = None
    # 来源类型：列表能按它筛，批量「按筛选全选」就必须认它，否则看着是里程碑、删掉的是全部来源
    origin_kind: Optional[str] = None
    # 所属事件：同上一条的理由。取值与列表的 `episode` 参数相同（空 / `none` / 事件 ID）
    episode: Optional[str] = None
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
    # 这一轮注入的那一版摘要是什么时候生成的；那一版已经不保留（只留最近几版）时为空
    summary_created_at: Optional[datetime] = None
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
    # 注入条目的 token 总数；0.4.7 之前的旧行没有 token 数，为 None（界面按旧口径显示字数）
    worldbook_tokens: Optional[int] = None
    # 酒馆一栏：高级槽位闸、各槽位的字数与状态、深度注入与开场白。只记字数与状态，
    # 不记原文；形状随运行时演进，因此原样透传而不是逐键建模。
    tavern_items: dict[str, Any] = Field(default_factory=dict)
    # 角色状态一栏：块的 token 数、截掉的组、上次更新的结果。只记计数与名字、不记状态值；
    # 形状随插件演进，原样透传。
    state_items: dict[str, Any] = Field(default_factory=dict)
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
    # 自动注入前的模型筛选结果：`mode`（model / fallback）、交给筛选的候选数、留下的条数。
    # 旧行没有这个键，为空。
    memory_judge: Optional[dict[str, Any]] = None
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
    # 这一轮的世界书预算与算法（模型组、上下文长度、百分比、上限、分词系数、最终预算、
    # 备用组上下文更小时的提示），见 `services/worldbook/budget.py`；旧行为空
    worldbook_budget: dict[str, Any] = Field(default_factory=dict)
    # 酒馆一栏：与列表行同一份内容，详情里给全（服务层已给出这个键，响应模型不再丢）。
    tavern_items: dict[str, Any] = Field(default_factory=dict)
    # 角色状态一栏：与列表行同一份内容。
    state_items: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    summary: dict[str, Any] = Field(default_factory=dict)


def detail_from_payload(payload: dict[str, Any]) -> MemoryItemDetail:
    """把服务层返回的字典收成响应模型；未声明的列由 `extra="allow"` 原样保留。"""
    return MemoryItemDetail.model_validate(payload)


class MemoryGraphNode(BaseModel):
    """图谱节点；与工作区记忆页图谱接口同形，`memory_type` 多一种 `person`（涉及的人，`ref_id` 为 0）。"""

    id: str
    memory_type: str
    ref_id: int
    label: str
    subtitle: str = ""
    status: str
    cognitive_type: Optional[str] = None
    weight: float = 0.0
    size: float = 1.0
    importance: float = 0.0
    paragraph_id: Optional[int] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryGraphEdge(BaseModel):
    """图谱的边；在工作区图谱的六类之外多一类 `paragraph_person`（段落涉及的人）。"""

    id: str
    source: str
    target: str
    edge_type: str
    label: str = ""
    weight: float = 0.0
    strength: float = 0.0
    status: str = "active"
    cognitive_type: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryGraphResponse(BaseModel):
    generated_at: datetime
    node_count: int
    edge_count: int
    nodes: list[MemoryGraphNode]
    edges: list[MemoryGraphEdge]
