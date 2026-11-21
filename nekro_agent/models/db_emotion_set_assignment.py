"""平台方对表情套组的分配

一条分配 = 「把这个套组分给谁」。四种目标：

- `all_instances`：所有实例的会话都能读到；
- `platform_instances`：只有归平台方的实例能读到（存量套组的默认分配就是它）；
- `instance`：某个实例对的会话能读到；
- `preset`：某个生效人设的会话能读到。

**只有 `preset` / `legacy` 套组能被分配**：收集库属于单个实例，按定义不需要分配，允许它被
分配等于让平台方能把一个实例的收集库开给别的实例读，服务层因此直接拒绝（见
`services/emotion_library.py`）。

唯一约束落在「同一个套组 + 同一个目标」上：重复添加返回已有那条而不是报错，控制台连点两次
不会留下两行一模一样、删一条还剩一条的分配。
"""

from tortoise import fields
from tortoise.models import Model


class DBEmotionSetAssignment(Model):
    """一条套组分配：把一个预设 / 存量套组分给所有实例、平台方实例、某实例或某人设"""

    id = fields.IntField(pk=True, generated=True, description="分配 ID")
    set = fields.ForeignKeyField(
        "models.DBEmotionSet",
        related_name="assignments",
        on_delete=fields.CASCADE,
        index=True,
        description="被分配的套组；删套组连带删分配",
    )
    target = fields.CharField(
        # 设计稿写的是 varchar(16)，但 `platform_instances` 有 18 个字符，16 位在 SQLite / PG 上
        # 都存不进去（tortoise 的字段校验直接拒绝）。放宽到 32，四个取值的语义不变。
        max_length=32,
        description="目标类型 all_instances / platform_instances / instance / preset",
    )
    adapter_key = fields.CharField(max_length=64, default="", description="instance 目标的适配器标识")
    instance_key = fields.CharField(max_length=64, default="", description="instance 目标的实例标识")
    preset_id = fields.IntField(default=0, description="preset 目标的人设 ID；其余目标为 0")
    created_by = fields.CharField(max_length=64, default="", description="创建者用户名")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")

    class Meta:  # type: ignore
        table = "emotion_set_assignment"
        # 外键在唯一约束里写关系字段名，tortoise 会落到 `set_id` 列上（同 `wb_entry` 的写法）
        unique_together = (("set", "target", "adapter_key", "instance_key", "preset_id"),)
        indexes = [
            # 会话算可用池时按实例对查；人设套组按 preset_id 查
            ("adapter_key", "instance_key"),
            ("preset_id",),
        ]
