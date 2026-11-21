"""频道摘要的暂停状态

按「频道 + 人设」一行的开关：没有行就是没暂停，这样零配置部署不需要任何默认数据，
行为与今天逐字相同。暂停只挡自动更新，注入照常使用最新一版摘要。
"""

from tortoise import fields
from tortoise.models import Model


class DBMemChannelSummaryState(Model):
    """某个（频道, 人设）的摘要暂停开关"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    chat_key = fields.CharField(max_length=256, index=True, description="所属频道")
    adapter_key = fields.CharField(max_length=64, default="", description="来源适配器标识")
    instance_key = fields.CharField(max_length=64, default="", description="来源实例标识")
    preset_id = fields.IntField(default=0, description="人设标识；0 表示无人设档")
    paused = fields.BooleanField(default=False, description="是否暂停自动更新")
    paused_by = fields.CharField(max_length=64, default="", description="暂停操作者")
    paused_at = fields.DatetimeField(null=True, description="暂停时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:
        table = "mem_channel_summary_state"
        unique_together = (("chat_key", "preset_id"),)
