"""世界书（一本书 = 一组同作用域的条目）

一本书只绑一个作用域（`scope_kind`），作用域的绑定对象跟着类型走：`preset` 用
`preset_id`，`channel` 用 `chat_key`，`person` 用 `person_account`，`domain` 谁也不
用。把四种作用域塞进同一张表而不是四张表，是因为它们的生命周期与「书」这一层完全
一致（启用、扫描深度、预算、导入导出都是书的属性），拆开只会让管理页与运行时各写
一遍同样的四个分支。

归属实例对（`adapter_key` / `instance_key`）为空**只**用于平台方给全局人设建的书：
那种书对所有实例生效，因此没法落在任何一个实例对下。除它以外，任何一本书都必须有
归属实例对，范围计算依赖这一点（见 `services/memory/instance_scope.py` 的世界书
条件）。

书与条目都是真删：世界书是给提示词喂内容的东西，留一份看不见的副本只会让「删了还
在注入」这种事故变得可能；导入导出足以保底，不需要软删。
"""

from tortoise import fields
from tortoise.models import Model


class DBWbBook(Model):
    """一本世界书"""

    id = fields.IntField(pk=True, generated=True, description="主键 ID")
    name = fields.CharField(max_length=128, description="书名，只给人看")
    description = fields.TextField(default="", description="书的说明，只给人看，不进提示词")
    scope_kind = fields.CharField(
        max_length=16,
        description="作用域类型 domain / preset / channel / person",
    )
    adapter_key = fields.CharField(max_length=64, default="", description="归属实例的适配器标识")
    instance_key = fields.CharField(max_length=64, default="", description="归属实例标识")
    preset_id = fields.IntField(default=0, description="preset 书绑定的人设；其余为 0")
    chat_key = fields.CharField(max_length=256, default="", index=True, description="channel 书绑定的频道")
    person_account = fields.CharField(max_length=128, default="", description="person 书绑定的人的平台账号")
    enabled = fields.BooleanField(default=True, description="整本书是否生效")
    scan_depth = fields.IntField(default=4, description="扫描最近几条消息，1 到 50")
    budget_tokens = fields.IntField(default=0, description="本书每轮最多注入多少 token；0 表示不单独限制")
    inject_mode = fields.CharField(max_length=16, default="both", description="注入方式 push / lookup / both")
    active_scene = fields.CharField(max_length=16, default="both", description="生效场景 both / private / group")
    case_sensitive = fields.BooleanField(default=False, description="条目没单独设时的大小写默认")
    match_whole_words = fields.BooleanField(default=False, description="条目没单独设时的整词默认")
    scan_names = fields.BooleanField(default=False, description="扫描时每条消息前带上说话人名字（酒馆的 include_names）")
    vector_all = fields.BooleanField(default=False, description="本书全部非常驻条目都参与语义触发")
    vector_query_messages = fields.IntField(default=2, description="语义查询取最近几条非空消息，1 到 10")
    vector_max_entries = fields.IntField(default=5, description="本书每轮最多语义命中几条，1 到 20")
    vector_threshold = fields.FloatField(default=0.25, description="语义相似度下限，0 到 1")
    ext = fields.JSONField(default=dict, description="导入时酒馆书级的其余字段，导出时还原")
    created_by = fields.CharField(max_length=64, default="", description="创建者用户名")
    updated_by = fields.CharField(max_length=64, default="", description="最后修改者用户名")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "wb_book"
        indexes = [
            ("adapter_key", "instance_key"),
            ("scope_kind", "preset_id"),
            ("adapter_key", "instance_key", "person_account"),
        ]
