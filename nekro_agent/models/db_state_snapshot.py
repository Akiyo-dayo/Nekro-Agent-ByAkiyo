"""角色状态的快照与更新记录

每次更新尝试都写一行：提交了的带整棵树（`data`），可以回滚到它；没提交的（解析失败、全部被拒、
冲突、忙、暂停）`data` 为空，只记结果与改了 / 拒了哪几处。管理员改状态、回滚、重置、改模板也各写
一行（`source=admin` 等，带 `editor`），兼作审计。保留份数由角色状态插件按目标裁剪。

`ops_applied` / `ops_rejected` 只记路径、操作与原因，不记值；值只在 `data` 里，后台按作用域看。
"""

from tortoise import fields
from tortoise.models import Model


class DBStateSnapshot(Model):
    """一次状态变化或一次更新尝试"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    target_kind = fields.CharField(max_length=16, description="目标类型 channel / person / template")
    target_id = fields.IntField(description="目标行 ID")
    chat_key = fields.CharField(max_length=256, default="", description="在哪个频道发生")
    adapter_key = fields.CharField(max_length=64, default="", description="来源适配器标识（后台作用域用）")
    instance_key = fields.CharField(max_length=64, default="", description="来源实例标识（后台作用域用）")
    source = fields.CharField(max_length=16, description="来源 update / admin / init / reset / rollback / greeting")
    result = fields.CharField(
        max_length=16,
        description="结果 applied / partial / rejected / parse_failed / error / conflict / busy / paused",
    )
    data = fields.JSONField(null=True, description="提交后的整棵树；没提交时为空")
    ops_applied = fields.JSONField(default=list, description="应用了的操作：路径与操作（不含值）")
    ops_rejected = fields.JSONField(default=list, description="被拒的操作：路径、操作与原因（不含值）")
    model_group = fields.CharField(max_length=128, default="", description="更新调用用的模型组")
    editor = fields.CharField(max_length=64, default="", description="管理员操作时的操作者")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")

    class Meta:  # type: ignore
        table = "state_snapshot"
        indexes = [
            ("target_kind", "target_id", "id"),
            ("adapter_key", "instance_key"),
        ]
