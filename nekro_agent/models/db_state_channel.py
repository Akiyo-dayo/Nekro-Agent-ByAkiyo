"""角色状态：频道 × 人设的当前状态

键与频道滚动摘要相同（`chat_key, preset_id`）。值来自聊天（经更新调用与宿主校验），按不可信内容
对待：注入时包进不可信块，日志里不打。`version` 是乐观锁：更新调用提交时版本变了（管理员刚改过），
这次结果作废，不覆盖管理员的修改。
"""

from tortoise import fields
from tortoise.models import Model


class DBStateChannel(Model):
    """一个频道在一个人设下的当前状态"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    chat_key = fields.CharField(max_length=256, description="所属频道")
    preset_id = fields.IntField(default=0, description="人设标识；0 表示无人设档")
    adapter_key = fields.CharField(max_length=64, default="", description="来源适配器标识（后台作用域用）")
    instance_key = fields.CharField(max_length=64, default="", description="来源实例标识（后台作用域用）")
    template_version = fields.IntField(default=0, description="初始化时用的模板版本")
    data = fields.JSONField(default=dict, description="状态树（MVU 形状，不含个人范围的子树）")
    version = fields.IntField(default=1, description="乐观锁版本，每次提交加一")
    last_update_at = fields.DatetimeField(null=True, description="最近一次成功更新的时间")
    last_message_db_id = fields.IntField(default=0, description="群聊更新的水位：上次更新看到的最后一条消息 ID")
    msgs_since_update = fields.IntField(default=0, description="上次更新以来的消息数（群聊触发用）")
    consecutive_failures = fields.IntField(default=0, description="连续失败次数；到上限后暂停更新")
    paused = fields.BooleanField(default=False, description="是否已暂停更新（状态照常注入）")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "state_channel"
        unique_together = (("chat_key", "preset_id"),)
        indexes = [("adapter_key", "instance_key")]
