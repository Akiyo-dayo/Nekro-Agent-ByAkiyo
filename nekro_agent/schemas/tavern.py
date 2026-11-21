"""酒馆人设卡接口的请求与响应模型

## 为什么约束不都写在 pydantic 里

`nickname`、开场白条数、槽位字数、清理规则这些上限要回 400 并在消息里指明是第几条，
而 pydantic 在请求解析阶段抛的是 422、消息还是英文的字段路径。因此这里只定形状，
逐条校验与中文原因放在路由里；形状本身（比如 `cleanup_rules` 必须是对象数组）仍由
pydantic 挡住，免得路由里到处写 `isinstance`。

## 报告的形状

导入检查的报告把「卡片字段」「知识书」「清理规则」「头像」「闸状态」分组：它们各自
有独立的计数与原因，平铺成几十个字段会让前端只能靠命名约定猜哪几个是一组。分组键
与设计稿里的名字一致，界面直接按组渲染。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from nekro_agent.schemas.worldbook import ImportReport


class SlotsOffChannel(BaseModel):
    """频道级覆盖把高级提示词槽位单独关着的频道：实例级打开了，这里照样不发送"""

    chat_key: str
    channel_name: str = ""


class TargetGates(BaseModel):
    """导入目标实例上两个闸的生效值

    报告里带它们是为了让管理员在上传前就知道「知识书会不会生效」「后置指令会不会
    发送」。世界书闸不是导入动作能改的，只能在报告里说清楚；高级槽位可以在导入时
    顺带为这个实例打开（`can_enable_advanced_slots`，只在导入检查里填）。
    """

    worldbook_enabled: bool = False
    advanced_slots_enabled: bool = False
    # 导入时能否顺带打开：有目标实例、且这个实例上槽位的生效值是关
    can_enable_advanced_slots: bool = False
    # 全局开没开：全局关、实例没设时，打开的是这个实例的实例级覆盖
    advanced_slots_global_enabled: bool = False
    # 频道级覆盖单独关着的频道；打开实例级后这些频道仍不发送
    advanced_slots_off_channels: list[SlotsOffChannel] = Field(default_factory=list)
    # 打开前要给管理员看的确认文字，与配置表单打开这一项时的确认文案是同一份（键是 zh-CN / en-US）
    advanced_slots_enable_confirm: dict[str, str] = Field(default_factory=dict)


class CardNoteItem(BaseModel):
    """一条解析说明；`kind` 是机读分类，`detail` 是中文说明"""

    field: str = ""
    kind: str = ""
    detail: str = ""


class DepthPromptInspect(BaseModel):
    """深度注入的两段值；空的也照样返回，前端自己决定显不显示"""

    text: str = ""
    depth: int = 4


class CleanupRuleInspect(BaseModel):
    """一条清理规则的导入预览

    `index` 是它在卡片里的序号，管理员按序号决定启不启用；`disabled_reason` 为空表示
    默认启用。
    """

    index: int
    name: str = ""
    find: str = ""
    replace: str = ""
    enabled: bool = True
    disabled_reason: str = ""


class CardBookInspect(BaseModel):
    """卡内知识书的统计；不返回条目正文

    条目正文可能上千条，检查接口只需要告诉管理员「有没有书、多少条、多少条会被判
    无效或带装饰器」，正文在导入后去世界书页看。
    """

    present: bool = False
    entries: int = 0
    invalid_keys: int = 0
    decorated: int = 0
    notes: dict[str, int] = Field(default_factory=dict)


class CardInspectReport(BaseModel):
    """上传后的检查报告；只解析，不落库"""

    spec: str
    source_format: str
    name: str = ""
    nickname: str = ""
    creator: str = ""
    character_version: str = ""
    tags: list[str] = Field(default_factory=list)
    creator_notes: str = ""
    assembled_content: str = ""
    assembled_chars: int = 0
    long_content: bool = False
    system_prompt: str = ""
    post_history_instructions: str = ""
    depth_prompt: DepthPromptInspect = Field(default_factory=DepthPromptInspect)
    greetings_count: int = 0
    group_greetings_count: int = 0
    book: CardBookInspect = Field(default_factory=CardBookInspect)
    cleanup_rules: list[CleanupRuleInspect] = Field(default_factory=list)
    avatar_source: Literal["png", "data_uri", "default"] = "default"
    avatar_note: str = ""
    unsupported: list[CardNoteItem] = Field(default_factory=list)
    notes: list[CardNoteItem] = Field(default_factory=list)
    target: TargetGates = Field(default_factory=TargetGates)


class CardImportOptions(BaseModel):
    """导入选项

    `rule_enabled` 只能关掉默认启用的规则，或打开停用原因是 `html_output` /
    `disabled_in_card` 的规则；索引是规则在卡片里的位置。其余停用原因（安全、
    语义不符）即便请求里写了 `true` 也不会被打开，原因写进结果的 `notes`。
    """

    include_system_prompt: bool = True
    include_post_history: bool = True
    include_depth_prompt: bool = True
    include_book: bool = True
    rule_enabled: dict[int, bool] = Field(default_factory=dict)
    greeting_pick: Literal["first", "random"] = "first"
    # 「打开并导入」：导入后把目标实例的高级提示词槽位打开（实例级覆盖，写审计）；
    # 只在导入了后置指令、目标实例的生效值是关时才动
    enable_advanced_slots: bool = False


class CardImportResult(BaseModel):
    """导入结果

    `book_report` 是卡内知识书的导入报告（没有书时为空）；`notes` 是给管理员看的
    中文说明，例如「规则 X 因安全检查未打开」。
    """

    preset_id: int
    book_id: Optional[int] = None
    book_report: Optional[ImportReport] = None
    notes: list[str] = Field(default_factory=list)
    # 「打开并导入」的结果：没要求 / 不需要（没导入后置指令或没有目标实例）/ 本来就开着 /
    # 打开了 / 没打开（人设已经导入，`advanced_slots_error` 写原因）
    advanced_slots: Literal["not_requested", "not_needed", "already_enabled", "enabled", "failed"] = "not_requested"
    advanced_slots_error: str = ""
    advanced_slots_off_channels: list[SlotsOffChannel] = Field(default_factory=list)


class PresetCardResponse(BaseModel):
    """人设的酒馆扩展行

    不返回 `card_raw`：它是导入时的整张卡原文，可能几十 KB，而扩展页只编辑列。
    清理规则不返回 `original`（那是导出时的还原底稿，界面不需要）。`exists` 为假时
    其余字段是新建人设时的默认值，前端可以照着表单渲染。

    `gates` 按请求的作用域算（实例视图取该实例的生效配置，全局视图取全局配置）：
    扩展页要写明「这段保存了会不会发出去」。`slow_disabled_rules` 是本进程因超时被
    停用的规则序号，`PUT` 保存规则后登记被清掉，因此响应里应为空。
    """

    exists: bool = False
    preset_id: int
    card_spec: str = ""
    nickname: str = ""
    source_fields: dict[str, str] = Field(default_factory=dict)
    assembled_sha256: str = ""
    content_assembled: bool = False
    macros_enabled: bool = True
    greetings: list[str] = Field(default_factory=list)
    group_greetings: list[str] = Field(default_factory=list)
    greeting_pick: str = "first"
    post_history: str = ""
    prefill: str = ""
    depth_prompt: str = ""
    depth_prompt_depth: int = 4
    depth_prompt_role: str = "system"
    cleanup_rules: list[dict[str, Any]] = Field(default_factory=list)
    character_version: str = ""
    imported_by: str = ""
    imported_at: Optional[datetime] = None
    gates: TargetGates = Field(default_factory=TargetGates)
    slow_disabled_rules: list[int] = Field(default_factory=list)


class PresetCardUpdateRequest(BaseModel):
    """改扩展行；只应用请求里真的出现过的字段

    所有字段都可空：`None` 表示「这次不改」，判据是 `model_fields_set`。清理规则用
    `dict` 而不是强类型模型：每条规则要多带一份 `original` 底稿，且校验失败时要在
    路由里逐条拼中文原因，强类型模型会把这两件事拆到两个地方。
    """

    nickname: Optional[str] = None
    source_fields: Optional[dict[str, str]] = None
    macros_enabled: Optional[bool] = None
    greetings: Optional[list[str]] = None
    group_greetings: Optional[list[str]] = None
    greeting_pick: Optional[str] = None
    post_history: Optional[str] = None
    prefill: Optional[str] = None
    depth_prompt: Optional[str] = None
    depth_prompt_depth: Optional[int] = None
    depth_prompt_role: Optional[str] = None
    cleanup_rules: Optional[list[dict[str, Any]]] = None
    character_version: Optional[str] = None


class CardReassembleResponse(BaseModel):
    """重新拼装后的正文与摘要"""

    content: str
    assembled_sha256: str


class TavernPreviewRequest(BaseModel):
    """提示词预览：看某个人设在这个频道里会拼出什么

    `sample_text` 的长度在路由里判：pydantic 的长度错误是 422、英文路径，而这里要的是
    400 与一句中文原因。`preset_id` 为空表示用频道当前人设。
    """

    chat_key: str
    preset_id: Optional[int] = None
    sample_text: Optional[str] = None


class TavernPreviewMessage(BaseModel):
    """预览里的一条消息；图片只写占位，不返回 base64 或本地路径

    `source` 标这条消息来自哪一段：组装顺序固定是「第一条系统、中间都是示例、最后
    一条历史」，界面据此分组显示，而不是让管理员自己数。
    """

    role: str
    text: str
    source: Literal["system", "example", "history"] = "system"


class TavernPreviewPrefill(BaseModel):
    """预览里的预填：文字、会不会发、为什么"""

    text: str = ""
    would_send: bool = False
    reason: str = "empty"


class TavernPreviewResponse(BaseModel):
    """提示词预览的完整结果

    `meta` 是组装层给的各段元数据（字数、槽位层名、世界书条目引用、开场白），形状随
    组装层演进；这里不复制一份强类型模型，免得两边改一处忘一处。
    """

    messages: list[TavernPreviewMessage] = Field(default_factory=list)
    prefill: TavernPreviewPrefill = Field(default_factory=TavernPreviewPrefill)
    meta: dict[str, Any] = Field(default_factory=dict)
    greeting: dict[str, Any] = Field(default_factory=dict)


class TavernTrialRequest(BaseModel):
    """试发：用管理员自己的模型组发一次；长度与组归属在路由里判"""

    chat_key: str
    text: str
    model_group: Optional[str] = None


class TavernTrialUsage(BaseModel):
    """服务商返回的 token 数；失败或上游没给时全是 0"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class TavernTrialResponse(BaseModel):
    """试发结果；代码只返回不执行

    `refusal` 是「渠道把拒绝受理当成正文返回」时命中的类别，空串表示不是拒答；
    `error_kind` 是请求失败时的机器分类（见 `model_capability.LLMFailureKind`），成功时为空。
    """

    model: str = ""
    model_group: str = ""
    latency_ms: int = 0
    reply: str = ""
    code: str = ""
    code_missing: bool = True
    refusal: str = ""
    error_kind: str = ""
    error: str = ""
    usage: TavernTrialUsage = Field(default_factory=TavernTrialUsage)
    prefill_state: str = "empty"


class TavernAuditItem(BaseModel):
    """一条配置变更记录；值是摘要，不含原文"""

    id: int = 0
    created_at: datetime
    operator: str = ""
    operator_is_platform: bool = False
    layer: str = ""
    adapter_key: str = ""
    instance_key: str = ""
    chat_key: str = ""
    preset_id: int = 0
    field: str = ""
    old_value: str = ""
    new_value: str = ""


class TavernAuditResponse(BaseModel):
    """变更记录的一页"""

    total: int = 0
    items: list[TavernAuditItem] = Field(default_factory=list)


__all__ = [
    "CardBookInspect",
    "CardImportOptions",
    "CardImportResult",
    "CardInspectReport",
    "CardNoteItem",
    "CardReassembleResponse",
    "CleanupRuleInspect",
    "DepthPromptInspect",
    "PresetCardResponse",
    "PresetCardUpdateRequest",
    "TargetGates",
    "TavernAuditItem",
    "TavernAuditResponse",
    "TavernPreviewMessage",
    "TavernPreviewPrefill",
    "TavernPreviewRequest",
    "TavernPreviewResponse",
    "TavernTrialRequest",
    "TavernTrialResponse",
    "TavernTrialUsage",
]
