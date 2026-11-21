"""表情套组

## 三种套组为什么放在同一张表

- `preset`：平台方按分类整理的预设套组；
- `legacy`：存量 JSON 导入时建的那一个「待整理」套组，也是平台方的；
- `collect`：每个实例自己的收集库。

三者的字段、生命周期与「能不能被分配」不同，但它们是同一种东西——一组表情的容器，控制台里
的列表、排序、计数、移动与复制都按同一套逻辑处理。拆成三张表只会让每个读写入口都写三遍
分支，且「把一个表情从收集库移进预设套组」会变成跨表搬行。

归属列（`owner_adapter_key` / `owner_instance_key`）只对 `collect` 有意义：`preset` / `legacy`
两列为空串，表示归平台方。空串而不是 NULL，与 `wb_book` / `preset` 的存量口径一致，
「平台方」与「单租户部署」因此是同一个取值，升级后不需要回填。

每个实例最多一个收集库由服务层在进程内锁里「先查后建」保证（见 `services/emotion_library.py`），
这里不加唯一约束：`preset` / `legacy` 行的归属列都是空串，一条全局唯一约束会把它们判成互相冲突，
而当前工具链又不支持带谓词的部分唯一索引（同 `db_adapter_instance` 模块文档里的取舍）。
"""

from tortoise import fields
from tortoise.models import Model


class DBEmotionSet(Model):
    """一组表情：平台方预设套组 / 存量待整理套组 / 某个实例的收集库"""

    id = fields.IntField(pk=True, generated=True, description="套组 ID，插件配置里引用它")
    kind = fields.CharField(
        max_length=16,
        description="套组类型 preset（平台方预设）/ legacy（存量待整理）/ collect（实例收集库）",
    )
    name = fields.CharField(max_length=64, description="套组名称；收集库默认「本实例收集」")
    category = fields.CharField(max_length=32, default="", description="分类，控制台按它分组")
    description = fields.TextField(default="", description="说明，只给人看，不进提示词")
    owner_adapter_key = fields.CharField(
        max_length=64,
        default="",
        description="收集库归属的适配器标识；平台方套组为空串",
    )
    owner_instance_key = fields.CharField(
        max_length=64,
        default="",
        description="收集库归属的实例标识；平台方套组为空串",
    )
    sort = fields.IntField(default=0, description="控制台排序，小的在前")
    created_by = fields.CharField(max_length=64, default="", description="创建者用户名")
    updated_by = fields.CharField(max_length=64, default="", description="最后修改者用户名")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "emotion_set"
        indexes = [
            # 收集库按实例对查（每个实例最多一个）；预设 / 存量套组按类型查
            ("kind", "owner_adapter_key", "owner_instance_key"),
        ]
