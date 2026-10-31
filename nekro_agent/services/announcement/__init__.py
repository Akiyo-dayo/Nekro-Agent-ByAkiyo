"""机器人公告服务

服务端异步任务模型：提交后立即返回任务 ID，投递在后台按适配器实例并发执行，
进度通过 SSE 推送，全部结果落库留档。
"""

from .schemas import (
    AnnouncementDeliveryEvent,
    AnnouncementDeliveryItem,
    AnnouncementDetail,
    AnnouncementEvent,
    AnnouncementInstanceOption,
    AnnouncementInstanceRef,
    AnnouncementListResponse,
    AnnouncementPreview,
    AnnouncementScope,
    AnnouncementSnapshotEvent,
    AnnouncementStatusEvent,
    AnnouncementSubmitResponse,
    AnnouncementSummary,
    AnnouncementTarget,
)
from .service import AnnouncementService, AnnouncementSubscription, announcement_service

__all__ = [
    "AnnouncementDeliveryEvent",
    "AnnouncementDeliveryItem",
    "AnnouncementDetail",
    "AnnouncementEvent",
    "AnnouncementInstanceOption",
    "AnnouncementInstanceRef",
    "AnnouncementListResponse",
    "AnnouncementPreview",
    "AnnouncementScope",
    "AnnouncementService",
    "AnnouncementSnapshotEvent",
    "AnnouncementStatusEvent",
    "AnnouncementSubmitResponse",
    "AnnouncementSubscription",
    "AnnouncementSummary",
    "AnnouncementTarget",
    "announcement_service",
]
