"""每轮提示词明细

记录一轮主对话里提示词注入了哪些记忆、摘要与好感块，以及各自渲染出来的字数。
它只存引用与字数，不存提示词正文：排查时看的是条目**当前**的内容，而不是当轮
快照。被引用到的条目之后可能被隔离、软删或真删，因此本表不加外键，由详情查询
逐条回表给出当前状态。

表里刻意不存 `one_time_code`：那是当轮包裹不可信内容的令牌，记录轮次只需要本表
自己生成的随机 `round_id`。保留量与年龄上限由配置项控制，超出上限的行在写入时
与维护循环里各清一次。
"""

from tortoise import fields
from tortoise.models import Model


class DBMemPromptRound(Model):
    """一轮提示词注入的明细"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    round_id = fields.CharField(max_length=32, unique=True, description="本表生成的随机轮次标识")
    chat_key = fields.CharField(max_length=256, index=True, description="所属频道")
    adapter_key = fields.CharField(max_length=64, default="", description="频道所属适配器标识")
    instance_key = fields.CharField(max_length=64, default="", description="频道所属实例标识")
    preset_id = fields.IntField(default=0, description="渲染时生效的人设标识；0 表示无人设档")
    workspace_id = fields.IntField(default=0, description="渲染时的记忆分区；0 表示域行")
    outcome = fields.CharField(max_length=16, default="ok", description="本轮结局 ok / blocked / failed / cancelled")
    render_count = fields.IntField(default=0, description="本轮渲染提示词的次数")
    memory_state = fields.CharField(
        max_length=16,
        default="disabled",
        description="记忆块状态 injected / empty / disabled / suppressed / error",
    )
    memory_items = fields.JSONField(default=list, description="本轮进入记忆块的条目引用与字数")
    memory_block_chars = fields.IntField(default=0, description="记忆块整段字数")
    cooldown_skipped = fields.IntField(default=0, description="本轮因冷却被跳过的候选数")
    summary_state = fields.CharField(
        max_length=16,
        default="disabled",
        description="摘要块状态 injected / none / disabled / suppressed",
    )
    summary_id = fields.IntField(null=True, description="注入的摘要行 ID")
    summary_version = fields.IntField(null=True, description="注入的摘要版本号")
    summary_chars = fields.IntField(default=0, description="摘要块整段字数")
    affection_shown = fields.BooleanField(default=False, description="好感块是否出现")
    affection_chars = fields.IntField(default=0, description="好感块整段字数")
    affection_people = fields.IntField(default=0, description="好感块里的人数")
    affection_milestone_ids = fields.JSONField(default=list, description="本轮好感块里注入的里程碑段落 ID")
    worldbook_state = fields.CharField(
        max_length=16,
        default="disabled",
        description="世界书状态 injected / none / disabled / suppressed / error",
    )
    worldbook_items = fields.JSONField(default=list, description="世界书条目引用与字数")
    worldbook_chars = fields.IntField(default=0, description="世界书正文总字数")
    tavern_items = fields.JSONField(default=dict, description="人设卡与高级槽位的本轮状态与字数")
    started_at = fields.DatetimeField(description="本轮开始时间")
    finished_at = fields.DatetimeField(description="本轮结束时间")

    class Meta:
        table = "mem_prompt_round"
        indexes = [
            ("adapter_key", "instance_key"),
            ("chat_key", "id"),
        ]
