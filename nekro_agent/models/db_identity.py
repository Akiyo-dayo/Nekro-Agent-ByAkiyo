"""「人」与账号别名

## 为什么需要「人」这一层

隔离键直接钉在 `platform_userid` 上时，**换一个 QQ 号 = 这个人的记忆与好感全部归零**。
这是明确不期望的行为，也是「(记忆域, 人, 人设)」这条设计的承重墙。

## 人的标识就是他主账号的 `platform_userid`

`person_uid` **恒等于该人主账号的平台用户 ID**。于是：

- **零默认数据**：从没合并过的人不需要任何一行——他的 `person_uid` 就是他自己的账号号码。
  这与「不建绑定行就等于自成一域」是同一条设计原则，也是**存量好感行原地成立**的原因：
  `affection.platform_userid` 里存着的值，正好就是这个人的 `person_uid`。
- **合并只加一行别名**：`(域, 副账号) → 主账号`。数据行一条都不搬。
- **删别名即撤销**：副账号立刻回到自己的那份数据，合并期间写入的内容留在主账号名下——
  这正是「合错了要能拆开」要的语义。真把两个人的数据物理并到一起就再也分不开了。

## 别名不成链

`DBIdentityAlias.person_uid` 指向的必须是一个**自己不是别名**的账号。解析因此只做一跳，
热路径上永远是一次点查。维护这条不变式是合并服务的职责：把 A 并进 C 时，
若已有 B→A，必须把 B→C 一并改写。解析器发现链式指向时会**告警但不追链**——
静默追链会让一次错误合并的影响范围随时间悄悄扩大，而告警能让它被发现。

## 人的标识必须与域绑定

`domain_id` 是别名表的第一维，**不是可选的**。少了它，一个租户在自己的域里声明
「这两个号是同一个人」会**泄进另一个租户的域**：对方那边的同号用户会突然读到不属于他的
好感与记忆。这是本模块唯一一条不能妥协的性质，用例见
`tests/test_identity_scope_foundation.py::TestAliasesAreScopedToTheirDomain`。

## 域的字符串编码

本表的 `domain_id` 存的是 :attr:`identity_scope.ScopeDomain.domain_id` 那个字符串
（`g:<组ID>` / `i:<适配器>:<实例>`）。好感表用的是另一种编码（三列键，见 `db_affection.py`），
理由是存量行要能零回填落位。**两种编码只有一个产生者**（`ScopeDomain`），
不要在别处自己拼这个字符串。

`domain_id` 的 96 字符是**算过的**，不是随手取的：`i:` + 适配器 + `:` + 实例，
适配器标识来自 `adapters/` 下的目录名（当前最长 18 字符），实例标识由
`console/container_manager.py` 的 `^[a-z0-9_]{1,32}$` 卡死，合起来上界是 53。
把实例标识的长度上限放宽到 92 以上时**必须同时加宽本列**——Postgres 上超长会直接报错，
而报错的位置是"建别名"，看起来与实例标识毫无关系。
"""

from tortoise import fields
from tortoise.models import Model

# 别名的来源。`SELF_REPORT` 表示由用户在 Bot 内自助申报、管理员在后台批准后建立；
# 两者都要求人点过一次——**不做自动推断**，把两个不同的人合并的代价远大于多点一下。
ALIAS_SOURCE_MANUAL = "manual"
ALIAS_SOURCE_SELF_REPORT = "self_report"


class DBIdentityPerson(Model):
    """「人」的档案行（**可选**，缺席不影响解析）

    解析器**永不查这张表**：`person_uid` 的默认值就是账号号码本身，需要一行才能确定"人存在"
    会把一次写失败变成一个人的数据全部失联。本表只承载后台要展示的元数据
    （显示名、备注、合并审计），以及"这个域里有哪些被合并过的人"这类管理视图。
    """

    id = fields.IntField(pk=True, generated=True, description="ID")

    domain_id = fields.CharField(max_length=96, index=True, description="所属域标识（ScopeDomain.domain_id）")
    person_uid = fields.CharField(max_length=256, index=True, description="人的稳定标识（= 主账号平台用户 ID）")

    # 由管理员填写，属不可信内容，展示前按 `tools/untrusted_content.py` 清洗
    display_name = fields.CharField(max_length=128, default="", description="显示名（后台展示用）")
    note = fields.TextField(default="", description="备注")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "identity_person"
        unique_together = (("domain_id", "person_uid"),)


class DBIdentityAlias(Model):
    """账号别名：`(域, 平台用户 ID) → 人`

    一个账号在一个域内**只能**指向一个人（唯一约束），否则解析没有确定答案。
    删除本行即撤销合并。
    """

    id = fields.IntField(pk=True, generated=True, description="ID")

    domain_id = fields.CharField(max_length=96, index=True, description="所属域标识（ScopeDomain.domain_id）")
    platform_userid = fields.CharField(max_length=256, index=True, description="别名账号的平台用户 ID")
    person_uid = fields.CharField(max_length=256, index=True, description="指向的人（= 主账号平台用户 ID）")

    source = fields.CharField(max_length=16, default=ALIAS_SOURCE_MANUAL, index=True, description="别名来源")
    operator = fields.CharField(max_length=128, default="", description="建立本条别名的操作者用户名")
    note = fields.TextField(default="", description="备注")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "identity_alias"
        # 「一个账号在一个域内只指向一个人」的数据库兜底。少了它，两条并发的合并请求能写出两行，
        # 读侧确定性取最早那行——不崩，但后建的那条永远不生效，表现为"合并了没反应"。
        unique_together = (("domain_id", "platform_userid"),)


# 申报单的状态。`PENDING` 等管理员审批；`CANCELED` 是申报人自己撤回。
ALIAS_REQUEST_PENDING = "pending"
ALIAS_REQUEST_APPROVED = "approved"
ALIAS_REQUEST_REJECTED = "rejected"
ALIAS_REQUEST_CANCELED = "canceled"

# 申报单上两个平台账号列的宽度。服务层在入库前按它校验（超长给操作者一句人话，
# 而不是到 Postgres 里炸成 StringDataRightTruncation）；列宽与校验限值只此一份。
ALIAS_REQUEST_USERID_MAX_LENGTH = 256


class DBIdentityAliasRequest(Model):
    """账号合并自助申报单——「后台手动为主、Bot 内自助申报为辅」的后者

    用户在 Bot 里申报「那个账号也是我」时，本表留下一行 `pending`；管理员在后台
    批准才建别名（批准走与手动合并完全相同的 `merge_accounts` 路径，差别只在别名行的
    `source=self_report`）。**申报本身不碰任何数据行**——被批准的申报才是合并，
    被拒的申报只是留痕。

    `domain_id` 取**提交那一刻**解析出的域，此后不随实例移动变化：实例此后换域，
    这张单子仍属于"它被提交时所在的那个域"，由那个域的管理员审。批准时服务端会
    复核申报人的「人」标识——中途发生过别的合并导致归属漂移的申报直接拒掉，
    不允许一张旧单子在语义已经变了之后悄悄兑现。
    """

    id = fields.IntField(pk=True, generated=True, description="ID")

    domain_id = fields.CharField(max_length=96, index=True, description="申报发生的域标识（ScopeDomain.domain_id）")
    alias_platform_userid = fields.CharField(
        max_length=ALIAS_REQUEST_USERID_MAX_LENGTH,
        index=True,
        description="申报要并入的副账号（通常是旧号）",
    )
    reporter_userid = fields.CharField(
        max_length=ALIAS_REQUEST_USERID_MAX_LENGTH,
        index=True,
        description="发起申报的平台账号（= 未来的主账号）",
    )
    reporter_person_uid = fields.CharField(max_length=256, description="提交时解析出的申报人「人」标识；批准时复核")
    reporter_chat_key = fields.CharField(max_length=256, description="申报所在会话标识，审批结果回这里通知")
    note = fields.TextField(default="", description="申报人备注")

    status = fields.CharField(max_length=16, default=ALIAS_REQUEST_PENDING, index=True, description="申报单状态")
    reviewed_by = fields.CharField(max_length=128, default="", description="审批操作者用户名")
    review_note = fields.TextField(default="", description="审批备注（拒绝理由等）")
    reviewed_at = fields.DatetimeField(null=True, description="审批时间")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "identity_alias_request"
        # pending 闸门（一人一域一张 pending / 一个账号一域只被一张 pending 指着）的最终裁决权在
        # **数据库的两条部分唯一索引**（迁移 26，`WHERE status = 'pending'`）：
        #     uid_alias_req_pending_reporter  (domain_id, reporter_userid)
        #     uid_alias_req_pending_claimed   (domain_id, alias_platform_userid)
        # Tortoise 的 Meta 表达不了部分唯一约束（按 status 过滤的唯一键），所以它们只存在于迁移里、
        # 不在模型状态里（与迁移 24 的 user 联合键索引同一形态）——**新建库用 generate_schemas 铺表
        # 不会带上它们**，生产与测试 PG 一律走迁移。
        # 服务层 `submit_alias_request` 里的先查后建只负责给出友好文案：READ COMMITTED 下它挡不住
        # 并发双写，穿过去的那一路由索引的 IntegrityError 兜住并翻成 `AliasRequestError`。
        # 这里自己只留查询用的复合索引。
        indexes = (("domain_id", "status"),)
