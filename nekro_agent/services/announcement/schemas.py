"""机器人公告服务的数据结构定义"""

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field

from nekro_agent.models.db_announcement import AnnouncementScopeType, AnnouncementStatus


class AnnouncementInstanceRef(BaseModel):
    """适配器实例引用

    `instance_key` 为空表示单实例适配器（历史数据同样落在这里），
    因此实例的唯一性由 (adapter_key, instance_key) 二元组共同决定。
    """

    adapter_key: str
    instance_key: str = ""


class AnnouncementScope(BaseModel):
    """公告发送范围"""

    scope_type: AnnouncementScopeType = AnnouncementScopeType.CHANNELS
    instances: List[AnnouncementInstanceRef] = Field(default_factory=list)
    chat_keys: List[str] = Field(default_factory=list)


class AnnouncementTarget(BaseModel):
    """公告投递目标"""

    chat_key: str
    channel_name: str
    adapter_key: str
    instance_key: str


class AnnouncementInstanceOption(BaseModel):
    """可选实例（含其活跃会话数量）"""

    adapter_key: str
    instance_key: str
    display_name: str
    channel_count: int


class AnnouncementPreview(BaseModel):
    """提交前的影响范围预览"""

    channel_count: int
    instance_count: int
    max_channels: int
    exceeds_limit: bool
    instances: List[AnnouncementInstanceOption]


class AnnouncementSummary(BaseModel):
    """公告任务摘要"""

    id: int
    content: str
    scope_type: AnnouncementScopeType
    scope_detail: List[str]
    status: AnnouncementStatus
    total: int
    success_count: int
    failure_count: int
    failure_reason: str
    created_by: str
    create_time: str
    finish_time: Optional[str]


class AnnouncementDeliveryItem(BaseModel):
    """单条投递明细"""

    chat_key: str
    channel_name: str
    adapter_key: str
    instance_key: str
    ok: bool
    error: str
    send_time: str


class AnnouncementDetail(AnnouncementSummary):
    """公告任务详情"""

    deliveries: List[AnnouncementDeliveryItem]


class AnnouncementListResponse(BaseModel):
    """公告历史列表"""

    total: int
    items: List[AnnouncementSummary]


class AnnouncementSubmitResponse(BaseModel):
    """公告提交结果"""

    announcement_id: int
    total: int
    status: AnnouncementStatus


class AnnouncementSnapshotEvent(BaseModel):
    """SSE 快照事件

    任何订阅者（含断流重连、进度条已经跑到一半才打开页面的用户）连接后先收到它，
    因此不依赖此前推送过的增量事件即可还原完整进度。
    """

    type: Literal["snapshot"] = "snapshot"
    announcement: AnnouncementSummary


class AnnouncementDeliveryEvent(BaseModel):
    """SSE 单条投递完成事件"""

    type: Literal["delivery"] = "delivery"
    announcement_id: int
    chat_key: str
    channel_name: str
    instance_key: str
    ok: bool
    error: str
    completed: int
    total: int
    success_count: int
    failure_count: int


class AnnouncementStatusEvent(BaseModel):
    """SSE 任务状态变更事件"""

    type: Literal["status"] = "status"
    announcement_id: int
    status: AnnouncementStatus
    total: int
    success_count: int
    failure_count: int
    failure_reason: str


AnnouncementEvent = Union[AnnouncementSnapshotEvent, AnnouncementDeliveryEvent, AnnouncementStatusEvent]
