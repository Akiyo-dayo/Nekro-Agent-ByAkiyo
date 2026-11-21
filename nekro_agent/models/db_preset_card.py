"""人设的酒馆扩展（一个人设最多一行）

卡片原文、开场白、高级槽位、清理规则与昵称这些数据放在独立的表里，而不是塞进
`presets.ext_data`：它们不只属于「从酒馆导入的人设」，NA 自己建的人设也可以用
开场白与高级槽位，后台第一次保存「酒馆扩展」时就建这一行；按列读写、按人设查、
随人设删除都比塞进一个大 JSON 清楚。`ext_data` 是共享到云端时原样上传的信封位，
混放会让「哪些字段是酒馆卡的、哪些是云端下载的」分不清。

本表只挂在一个已存在的人设上，不负责创建人设；卡片原文存 `card_raw`，导出时以
它为底还原。卡内知识书与正则脚本各有自己的原文存处（人设书的 `ext`、规则的
`original`），这里不重复存一份，免得两边对不上。
"""

from tortoise import fields
from tortoise.models import Model


class DBPresetCard(Model):
    """一个人设的酒馆扩展行"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    # 一对一字段：`preset_id` 列上自带唯一约束，「一个人设最多一行」由数据库保证。
    preset = fields.OneToOneField(
        "models.DBPreset",
        related_name="card",
        on_delete=fields.CASCADE,
        description="所属人设；一个人设最多一行，删人设连带删这一行",
    )
    card_spec = fields.CharField(max_length=8, default="", description="卡片版本：空表示 NA 原生，或 v1 / v2 / v3")
    card_raw = fields.JSONField(null=True, description="导入时的卡片原文，导出时以它为底还原")
    nickname = fields.CharField(max_length=128, default="", description="卡片里的昵称，有就优先用于 {{char}}")
    source_fields = fields.JSONField(
        default=dict,
        description="拼装正文用的卡片字段：system / description / personality / scenario / mes_example",
    )
    assembled_sha256 = fields.CharField(max_length=64, default="", description="上次拼装结果的摘要，用来判断正文有没有被手改")
    macros_enabled = fields.BooleanField(default=True, description="人设正文是否展开宏；NA 原生人设建行时可关")
    greetings = fields.JSONField(default=list, description="开场白列表，first_mes 排第一")
    group_greetings = fields.JSONField(default=list, description="群聊专用开场白")
    greeting_pick = fields.CharField(max_length=8, default="first", description="开场白挑选方式 first / random")
    post_history = fields.TextField(default="", description="人设层的后置指令")
    prefill = fields.TextField(default="", description="人设层的预填文字")
    depth_prompt = fields.TextField(default="", description="深度注入的正文")
    depth_prompt_depth = fields.IntField(default=4, description="深度注入的位置，0 到 100，0 表示最后一条之后")
    depth_prompt_role = fields.CharField(max_length=16, default="system", description="深度注入的角色，只存不用")
    cleanup_rules = fields.JSONField(default=list, description="卡片自带的输出清理规则")
    character_version = fields.CharField(max_length=64, default="", description="卡片的 character_version")
    imported_by = fields.CharField(max_length=64, default="", description="导入人的用户名")
    imported_at = fields.DatetimeField(null=True, description="导入时间；NA 原生人设为空")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "preset_card"
