"""酒馆对话补全预设（SillyTavern completion preset）的导入结果

一行就是一份导入的预设。原文原样留在 `raw` 里，导出时直接还回去；规范化后的段落
（顺序、开关、角色、占位标记、深度）放在 `segments`，运行时只读这一份，不再重新解析
原文。预设自带的正则脚本转成 NA 的清理规则存进 `regex_rules`，这一版只保存、不生效。

归属只有一列 `owner_instance_key`，语义照 `DBPreset.owner_instance_key`：空串表示
平台方的全局预设（所有实例可见可用），否则只属于该实例。
"""

from tortoise import fields
from tortoise.models import Model


class DBRpPreset(Model):
    """一份导入的酒馆对话补全预设"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    name = fields.CharField(max_length=128, description="显示名，默认取文件名去扩展名")
    owner_instance_key = fields.CharField(
        max_length=64,
        default="",
        index=True,
        description="所属实例标识，空表示全局共享",
    )
    segments = fields.JSONField(default=list, description="规范化后的段落，含每段的开关")
    raw = fields.JSONField(default=dict, description="导入时的原文件对象，导出与重解析用")
    regex_rules = fields.JSONField(default=list, description="预设自带正则转成的清理规则，只存不用")
    notes = fields.JSONField(default=list, description="导入时的报告条目")
    created_by = fields.CharField(max_length=64, default="", description="导入人")
    updated_by = fields.CharField(max_length=64, default="", description="最后修改人")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "rp_preset"


__all__ = ["DBRpPreset"]
