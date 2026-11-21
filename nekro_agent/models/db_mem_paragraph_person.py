"""记忆段落涉及的人（账号）关联表

这里存的是**账号**（消息表里的平台用户 ID），不是别名表解析出来的「人」。原因是账号合并
可以被撤销：把合并结果写进段落等于把当时的归属永久固定下来，撤销之后这条记忆会继续挂在
错误的人名下。读取时再按当前的别名表把账号现算成人，撤销合并就自动生效。

用关联表而不是在段落上加一个 JSON 列，是因为 JSON 数组的「包含」查询在 PostgreSQL 与
SQLite 上的写法与语义并不一致：同一份条件在两边可能返回不同的行，测试通过在部署上也未必
成立。关联表上的 `(paragraph_id, account)` 唯一约束与普通索引在两种库上行为一致。
"""

from tortoise import fields
from tortoise.models import Model


class DBMemParagraphPerson(Model):
    """段落与涉及账号的关联行"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    paragraph_id = fields.IntField(index=True, description="记忆段落 ID")
    account = fields.CharField(
        max_length=256,
        index=True,
        description="涉及的账号（平台用户 ID，不是解析后的人）",
    )
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")

    class Meta:
        table = "mem_paragraph_person"
        unique_together = (("paragraph_id", "account"),)
