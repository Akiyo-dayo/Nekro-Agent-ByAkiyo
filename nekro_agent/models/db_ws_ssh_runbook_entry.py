"""Bot 往服务器运维手册里追加的经验

所有者写的手册存在凭据行的 `runbook` 列；这张表存 CC 每次修完、经网关子系统
`nk-runbook-append` 追加的一条条经验（踩过的坑、修法）。拍到租约时最近几条随手册进
任务正文，外面包不可信边界：它们来自 CC 的输出，可能被当时处理的外部内容带偏。

每台服务器最多留 `RUNBOOK_MAX_ENTRIES` 条，超过时删最旧的；后台可以逐条删除。
凭据行删掉时连带删除。
"""

from tortoise import fields
from tortoise.models import Model
from tortoise.signals import pre_save

from nekro_agent.models.nul_guard import strip_nul_before_save


class DBWsSshRunbookEntry(Model):
    """一条 Bot 追加的运维经验"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    credential = fields.ForeignKeyField(
        "models.DBWsSshCredential",
        related_name="runbook_entries",
        on_delete=fields.CASCADE,
        index=True,
        description="哪台服务器的经验；删凭据行连带删除",
    )
    content = fields.TextField(description="经验正文，去控制字符后截到上限")
    task_ref = fields.CharField(max_length=256, default="", description="追加时的 CC 任务标识")
    lease_id = fields.CharField(max_length=32, default="", description="追加时的租约，用来按租约限条数")
    created_at = fields.DatetimeField(auto_now_add=True, index=True, description="追加时间")

    class Meta:  # type: ignore
        table = "ws_ssh_runbook_entry"


pre_save(DBWsSshRunbookEntry)(strip_nul_before_save)


__all__ = ["DBWsSshRunbookEntry"]
