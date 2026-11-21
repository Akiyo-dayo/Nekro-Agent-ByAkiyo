"""角色状态的模板：一个人设一份

模板描述状态树里有哪些字段、什么类型、谁能改、哪些是私密的，以及一份初值。它由角色状态插件
（`plugins/builtin/state_table/`）读写；核心只提供表，不读它。

归属跟着人设走（`DBPreset.owner_instance_key`）：全局人设的模板只有平台方能改，租户只读。
"""

from tortoise import fields
from tortoise.models import Model


class DBStateTemplate(Model):
    """一个人设的状态模板"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    preset_id = fields.IntField(unique=True, description="所属人设；一个人设一份模板")
    field_defs = fields.JSONField(
        default=dict, description="字段定义：路径、类型、说明、只读、私密、隐藏、范围、上限等"
    )
    initial = fields.JSONField(default=dict, description="初值树（MVU 形状，`$meta` 已转进字段定义）")
    source = fields.CharField(max_length=16, default="manual", description="来源 initvar / greeting / manual")
    source_entry_id = fields.IntField(null=True, description="从世界书条目推导时，取自哪一条")
    version = fields.IntField(default=1, description="模板版本，每次修改加一")
    edited_by = fields.CharField(max_length=64, default="", description="最后修改的操作者；自动生成时为空")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "state_template"
