
from tortoise import fields
from tortoise.models import Model

# `DBPreset.owner_instance_key` 表示"全局共享"的取值。
# 人设库对所有实例共享，只有在某个实例作用域下新建/下载的人设才写入具体实例标识并独享隔离；
# 存量数据与单实例部署全部落在这个空串取值上，因此升级后不会有任何人设"消失"。
GLOBAL_PRESET_OWNER = ""


class DBPreset(Model):
    """数据库预设模型"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    remote_id = fields.CharField(max_length=54, null=True, index=True, description="远程预设ID")
    on_shared = fields.BooleanField(default=False, description="是否在共享预设中")

    owner_instance_key = fields.CharField(
        max_length=64,
        default="",
        index=True,
        description="所属实例标识，空表示全局共享人设",
    )

    name = fields.CharField(max_length=256, index=True, description="预设名称")
    title = fields.CharField(max_length=128, index=True, description="标题")
    avatar = fields.TextField(description="预设头像(base64)")
    content = fields.TextField(description="预设内容")
    description = fields.TextField(description="预设描述")
    tags = fields.CharField(max_length=512, description="预设标签(逗号分隔)")
    ext_data = fields.JSONField(null=True, description="扩展数据")

    author = fields.CharField(max_length=128, description="作者")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:
        table = "presets"
