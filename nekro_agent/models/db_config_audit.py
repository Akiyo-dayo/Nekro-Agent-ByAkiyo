"""配置变更审计

高级槽位的文字是管理员配置的破限文字，审计表只记“谁在什么时候改了哪一项、长度
多少、摘要是什么”，不记原文：能追溯改动，又不必在另一张表里再存一份敏感文字。
布尔项写 `true` / `false` / `inherit`；文字项写 `len=N sha=<前 12 位>`。

按层记录归属：系统层没有具体实例或频道，适配器 / 实例 / 频道 / 人设层按实际填写。
不自动清理，因为写的都是配置动作，量很小。
"""

from tortoise import fields
from tortoise.models import Model


class DBConfigChangeAudit(Model):
    """一次配置变更"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    created_at = fields.DatetimeField(auto_now_add=True, index=True, description="变更时间")
    operator = fields.CharField(max_length=64, default="", description="操作人用户名")
    operator_is_platform = fields.BooleanField(default=False, description="操作人是否是平台方")
    layer = fields.CharField(max_length=16, description="变更所在层 system / adapter / instance / channel / preset")
    adapter_key = fields.CharField(max_length=64, default="", description="适配器标识，系统层为空")
    instance_key = fields.CharField(max_length=64, default="", index=True, description="实例标识，系统层为空")
    chat_key = fields.CharField(max_length=256, default="", description="频道标识，非频道层为空")
    preset_id = fields.IntField(default=0, description="人设标识，非人设层为 0")
    field = fields.CharField(max_length=64, description="被修改的配置项或字段名")
    old_value = fields.CharField(max_length=128, default="", description="旧值的摘要：布尔值或长度与摘要，不存原文")
    new_value = fields.CharField(max_length=128, default="", description="新值的摘要：布尔值或长度与摘要，不存原文")

    class Meta:  # type: ignore
        table = "config_change_audit"
        indexes = (("instance_key", "created_at"),)
