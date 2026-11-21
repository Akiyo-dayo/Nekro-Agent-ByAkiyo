"""频道滚动摘要的历史版本

每个「频道 + 人设」保留最近若干版摘要，而不是只存一行：摘要由沉淀模型在上一版的基础上
改写，上一版本身可能被后续一次错误的改写覆盖，保留几版才能在需要时回看与回滚。版本号
按「频道 + 人设」单调递增，内容与最新一版完全相同时不写新版，避免同一段对话反复沉淀把
历史版本挤掉。
"""

from tortoise import fields
from tortoise.models import Model


class DBMemChannelSummary(Model):
    """频道滚动摘要的一版"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    chat_key = fields.CharField(max_length=256, index=True, description="所属频道")
    adapter_key = fields.CharField(max_length=64, default="", description="来源适配器标识")
    instance_key = fields.CharField(max_length=64, default="", description="来源实例标识")
    preset_id = fields.IntField(default=0, description="人设标识；0 表示无人设档")
    version = fields.IntField(default=1, description="同一频道与人设下的版本号，从 1 开始")
    content = fields.TextField(description="摘要正文")
    last_message_db_id = fields.IntField(default=0, description="生成这一版时覆盖到的最后一条消息 ID")
    # 摘要的来源：默认 auto 就是今天的行为，存量行与现有调用都不需要改。
    source = fields.CharField(max_length=16, default="auto", description="来源 auto / manual / rollback")
    source_version = fields.IntField(null=True, description="回滚时这一版取自哪一版")
    edited_by = fields.CharField(max_length=64, default="", description="手动编辑 / 回滚的操作者")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")

    class Meta:
        table = "mem_channel_summary"
        unique_together = (("chat_key", "preset_id", "version"),)
