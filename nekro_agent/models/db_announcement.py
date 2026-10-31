"""机器人公告任务与投递明细模型

公告群发属于账号风控高危操作，必须留档：任务本身记录范围、状态与统计，
每一条实际投递单独成行，便于事后排查是哪个实例、哪个会话失败。
"""

from enum import StrEnum

from tortoise import fields
from tortoise.models import Model


class AnnouncementScopeType(StrEnum):
    """公告发送范围类型"""

    ALL = "all"
    """全部启用实例的全部活跃会话"""

    INSTANCES = "instances"
    """指定实例的全部活跃会话"""

    CHANNELS = "channels"
    """指定 chat_key 列表"""


class AnnouncementStatus(StrEnum):
    """公告任务状态"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_ANNOUNCEMENT_STATUSES: frozenset[AnnouncementStatus] = frozenset(
    {
        AnnouncementStatus.COMPLETED,
        AnnouncementStatus.FAILED,
        AnnouncementStatus.CANCELLED,
    },
)


class DBAnnouncement(Model):
    """机器人公告任务"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    content = fields.TextField(description="公告内容")
    scope_type = fields.CharField(max_length=16, index=True, description="范围类型: all/instances/channels")
    scope_value = fields.TextField(default="[]", description="范围取值(JSON 数组)")
    status = fields.CharField(
        max_length=16,
        default=AnnouncementStatus.PENDING.value,
        index=True,
        description="任务状态: pending/running/completed/failed/cancelled",
    )
    total = fields.IntField(default=0, description="目标会话总数")
    success_count = fields.IntField(default=0, description="成功投递数")
    failure_count = fields.IntField(default=0, description="失败投递数")
    failure_reason = fields.TextField(default="", description="任务级失败原因(熔断/重启中断等)")
    created_by = fields.CharField(max_length=128, default="", description="提交人用户名")
    create_time = fields.DatetimeField(auto_now_add=True, index=True, description="创建时间")
    finish_time = fields.DatetimeField(null=True, description="结束时间")

    class Meta:  # type: ignore
        table = "announcement"


class DBAnnouncementDelivery(Model):
    """机器人公告单条投递结果"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    announcement_id = fields.IntField(index=True, description="所属公告任务 ID")
    chat_key = fields.CharField(max_length=256, index=True, description="目标会话唯一标识")
    channel_name = fields.CharField(max_length=128, default="", description="投递时的会话名称快照")
    adapter_key = fields.CharField(max_length=64, default="", description="适配器标识")
    instance_key = fields.CharField(max_length=64, default="", index=True, description="适配器实例标识")
    ok = fields.BooleanField(default=False, description="是否投递成功")
    error = fields.TextField(default="", description="失败原因")
    send_time = fields.DatetimeField(auto_now_add=True, description="投递时间")

    class Meta:  # type: ignore
        table = "announcement_delivery"
