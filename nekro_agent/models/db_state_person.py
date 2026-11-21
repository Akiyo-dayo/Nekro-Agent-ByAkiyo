"""角色状态：一个人在故事里的个人档案

键与好感记录相同（域三列 + 人 + 人设，见 `services/affection/key.py` 的 `AffectionKey`），
所以同一个域里的几个频道共用一份档案，跨租户不会串。私密字段也存在这里，只在与本人的私聊里
注入与更新（渲染与校验时过滤）。值按不可信内容对待。
"""

from tortoise import fields
from tortoise.models import Model


class DBStatePerson(Model):
    """一个人在一个人设下的个人档案"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    adapter_key = fields.CharField(max_length=64, default="", description="自成一域时的适配器标识；属于实例组时为空串")
    instance_key = fields.CharField(max_length=64, default="", description="自成一域时的实例标识；属于实例组时为空串")
    domain_group_id = fields.IntField(default=0, description="所属实例组 ID；0 表示自成一域")
    platform_userid = fields.CharField(max_length=256, description="人的标识（= 主账号平台用户 ID）")
    preset_id = fields.IntField(default=0, description="人设标识；0 表示无人设档")
    data = fields.JSONField(default=dict, description="个人范围的子树（含私密字段）")
    version = fields.IntField(default=1, description="乐观锁版本，每次提交加一")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "state_person"
        unique_together = (("adapter_key", "instance_key", "domain_group_id", "platform_userid", "preset_id"),)
