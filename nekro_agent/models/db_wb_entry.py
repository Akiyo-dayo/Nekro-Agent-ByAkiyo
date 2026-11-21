"""世界书的条目

条目只经所属的书访问：可见范围算在书这一层（`wb_book` 有归属实例对与作用域列），
条目表自己不参与条件树。这样「这条条目能不能被这个租户看到」永远只有一个答案来源，
不会出现「书挡住了、条目却按别的判据漏出去」的第二处实现。

字段与酒馆世界书条目的对应关系见 `services/worldbook/st_format.py`：映射到列的是本
插件要用的那一部分，整份原始条目留在 `ext` 里，导出时以它为底还原，因此酒馆新增的
字段不会在往返里丢掉。

`uid` 是书内编号，导入时沿用酒馆的 `uid`，新建时取书内最大值加一。唯一约束落在
`(book, uid)` 上而不是只 `uid`：不同书的编号必然重复。字段名就叫 `order`——它是酒馆
自己的字段名，改名会让导入导出的映射多一层翻译，而 SQLite 与 PostgreSQL 都没把它当
保留字（用例外两个后端上都按它排序一次）。
"""

from tortoise import fields
from tortoise.models import Model


class DBWbEntry(Model):
    """世界书里的一条条目"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    book = fields.ForeignKeyField(
        "models.DBWbBook",
        related_name="entries",
        on_delete=fields.CASCADE,
        index=True,
        description="所属的书；删书连带删条目",
    )
    uid = fields.IntField(description="书内编号，沿用酒馆的 uid")
    # 文本列而不是定长：酒馆世界书有把整段备注写在标题里的，导入要一字不截（长度由
    # `types.MAX_COMMENT_CHARS` 这个安全上限挡畸形文件）。
    comment = fields.TextField(default="", description="标题，只给人看")
    keys = fields.JSONField(default=list, description="主关键词数组")
    secondary_keys = fields.JSONField(default=list, description="次关键词数组")
    selective_logic = fields.CharField(
        max_length=8,
        default="and_any",
        description="次关键词逻辑 and_any / and_all / not_any / not_all",
    )
    use_regex = fields.BooleanField(default=False, description="为真时每个关键词都按正则解释")
    constant = fields.BooleanField(default=False, description="常驻：不看关键词，书生效就注入")
    content = fields.TextField(description="注入的正文")
    order = fields.IntField(default=100, description="入选排序用的权重，大的先入选")
    position = fields.CharField(
        max_length=16,
        default="before_history",
        description="注入位置 after_persona / before_history / at_depth",
    )
    depth = fields.IntField(default=4, description="at_depth 用的深度，0 到 100")
    scan_depth = fields.IntField(null=True, description="扫描深度；空表示用书的")
    case_sensitive = fields.BooleanField(null=True, description="大小写；空表示用书的")
    match_whole_words = fields.BooleanField(null=True, description="整词匹配；空表示用书的")
    sticky = fields.IntField(null=True, description="命中后保持多少条消息；空或 0 表示不用")
    cooldown = fields.IntField(null=True, description="命中后多少条消息内不再命中；空或 0 表示不用")
    delay = fields.IntField(null=True, description="频道消息少于这个数时不命中；空或 0 表示不用")
    enabled = fields.BooleanField(default=True, description="条目是否启用")
    trusted = fields.BooleanField(default=False, description="按宿主内容注入")
    status = fields.CharField(max_length=16, default="active", description="状态 active / quarantined")
    quarantined_at = fields.DatetimeField(null=True, description="被隔离的时间")
    quarantine_reason = fields.CharField(max_length=16, default="", description="隔离原因 manual / auto_blocked")
    blocked_round_hits = fields.IntField(default=0, description="注入后被拦的轮次数，用于自动隔离")
    ext = fields.JSONField(default=dict, description="酒馆条目的原始内容与插件自有的 na_ 附加信息")
    created_by = fields.CharField(max_length=64, default="", description="创建者用户名")
    updated_by = fields.CharField(max_length=64, default="", description="最后修改者用户名")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "wb_entry"
        unique_together = (("book", "uid"),)
