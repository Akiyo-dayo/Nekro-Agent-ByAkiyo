"""表情行

一条表情只属于一个套组（`set` 外键）：套组之间「移动」就是改这一列，「复制」是新建一行指向
同一个文件。归属因此永远只有一处答案来源，会话能读什么由它所属套组是否在可读集合里决定。

## 文件按内容哈希命名

`file_path` 是相对插件表情目录的路径。存量行是平铺目录里的文件名
（`<原名>_<md5 前 6 位>.<后缀>`），新收集与控制台上传的是 `h/<sha256>.<扩展名>`：文件名
就是内容，同一张图只存一份，不同套组、不同实例的行指向同一个文件，不会互相覆盖。
`file_sha256` 用于去重（可读集合里已有同一张图就直接返回那条）与删除时的文件引用计数；
存量导入时补算，文件不在的留空。用 sha256 而不是 md5：md5 可以人为造碰撞，内容寻址存储下
碰撞等于让别人的行指向你造的图。

## 时间列为什么是 int

存量 JSON 里记的是秒级时间戳（`added_time` / `last_updated`），导入要原样保留，列表也按它
倒序排「最近收集」。用 int 存省掉时区换算这一层不确定性，存量与新数据可以直接比较。
"""

from tortoise import fields
from tortoise.models import Model


class DBEmotionItem(Model):
    """一条表情：描述、标签、文件与所属套组"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    emotion_id = fields.CharField(
        max_length=16,
        unique=True,
        description="模型看到的 8 位十六进制 ID；存量 ID 原样保留",
    )
    set = fields.ForeignKeyField(
        "models.DBEmotionSet",
        related_name="items",
        on_delete=fields.CASCADE,
        index=True,
        description="所属套组；删套组连带删表情行",
    )
    description = fields.TextField(description="描述，搜索与注入都用它")
    tags = fields.JSONField(default=list, description="标签数组")
    source_path = fields.TextField(default="", description="收集时模型给的来源，只作记录")
    file_path = fields.CharField(
        max_length=512,
        description="相对插件表情目录的路径：存量是平铺文件名，新的是 h/<sha256>.<扩展名>",
    )
    file_sha256 = fields.CharField(
        max_length=64,
        default="",
        index=True,
        description="文件内容哈希，用于去重与删除时的引用计数；存量文件缺失时为空串",
    )
    collected_chat_key = fields.CharField(
        max_length=256,
        default="",
        description="新收集的记下是哪个频道收的（存量为空），只用于控制台展示与排查",
    )
    added_at = fields.IntField(description="加入时间，秒级时间戳，沿用存量 JSON 的值")
    updated_at = fields.IntField(description="最后修改时间，秒级时间戳")

    class Meta:  # type: ignore
        table = "emotion_item"
        indexes = [
            ("set", "added_at"),
            ("set", "file_sha256"),
        ]
