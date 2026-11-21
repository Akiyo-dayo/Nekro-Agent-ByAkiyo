"""频道每日额度的策略表与计数表

## 为什么策略不进可覆盖配置

`overridable=True` 的实例层与频道层是**租户可写**的（`PUT /config/instance/...` 只要求
`Role.Admin` 加归属校验）。平台方给每个群、每个私聊承诺的每日额度是计费边界，一旦落在
租户能写的那一层，租户就能自己把它改掉，承诺当场作废。项目里已经有同一个先例：
平台方给实例设的回复配额上限存在 `DBAdapterInstance.metadata_json` 里，而不是做成可覆盖
配置项，理由正是「元数据只有平台方接口能写」。

所以三层策略（全局 / 实例 / 频道）单独一张表，只有平台方接口能写平台方字段；
租户能调的只有 `tenant_*` 那几列，写进去也只是「在平台方给的额度以内再调低」。

## 计数表是账本的可重算缓存

`DBLLMUsageRecord` 是 token 的唯一事实来源，但按 `chat_key` + 时间窗做 `SUM` 放在
**每条入站消息的判定路径**上太贵：一次判定要扫的是一天的流水行，而消息来得越密、
判定越频繁，恰好也是流水写得越多的时候。因此另存一张按天累加的计数表，判定只读一行。

它是**缓存**而不是事实：丢了可以按账本重算。回复次数是个例外——账本里没有「回合」这一维，
回复次数只能以这张表为准。

## 两列 token 都记，是为了不必回算

`tokens_all` 记账本里记在该频道头上的全部 token，`tokens_reply` 记其中属于回复主链路与
上下文规划的那一部分。平台方可以中途把「token 计入范围」从全部调用改成只算回复链路，
反过来也可以。两列都记着，切换时只是换一列去读，历史天不需要按账本重算一遍。
"""

from tortoise import fields
from tortoise.models import Model


class DBChannelQuotaPolicy(Model):
    """频道每日额度的三层策略

    一行一个作用域：`global` 一行、每个实例最多一行、每个频道最多一行。
    平台方字段为 NULL 表示继承上一层；租户字段为 NULL 表示租户没有调过。
    """

    id = fields.IntField(pk=True, generated=True, description="ID")

    # ==== 作用域（唯一键的四列；不用的列留空串而不是 NULL，避免唯一约束在 NULL 上失效） ====
    scope = fields.CharField(max_length=16, description="作用域：global / instance / channel")
    adapter_key = fields.CharField(max_length=64, default="", description="适配器标识；实例与频道层填写")
    instance_key = fields.CharField(max_length=64, default="", description="实例标识；实例与频道层填写")
    chat_key = fields.CharField(max_length=256, default="", description="会话标识；频道层填写")

    # ==== 平台方字段（NULL = 继承上一层；全局行是最后一层，不允许为 NULL） ====
    enabled = fields.BooleanField(null=True, description="是否启用频道额度")
    unit = fields.CharField(max_length=16, null=True, description="额度单位：replies / tokens")
    group_daily_limit = fields.BigIntField(null=True, description="群的每日额度；0 表示不限")
    private_daily_limit = fields.BigIntField(null=True, description="私聊的每日额度；0 表示不限")
    over_limit_action = fields.CharField(max_length=16, null=True, description="超额动作：alert / pause")
    pause_message = fields.TextField(null=True, description="暂停后第一次被叫到时回的提示")

    # ==== 租户字段（只有平台方与租户管理员能写；写入时按当时的生效单位打快照） ====
    tenant_group_daily_limit = fields.BigIntField(null=True, description="租户调低的群额度；NULL 表示未调")
    tenant_private_daily_limit = fields.BigIntField(null=True, description="租户调低的私聊额度；NULL 表示未调")
    # 单位是平台方随时能改的。租户填的数字只在「他填的那一刻那个单位」下有意义，
    # 单位换了之后旧数字既不是回复数也不是 token 数，只能作废（视为未设）而不是猜一个换算。
    tenant_limit_unit = fields.CharField(max_length=16, null=True, description="租户设值时的单位快照")
    tenant_pause_message = fields.TextField(null=True, description="租户自定义的暂停提示；NULL 表示用平台方的")

    updated_by = fields.CharField(max_length=128, default="", description="最后一次写入者的用户名")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "channel_quota_policy"
        unique_together = (("scope", "adapter_key", "instance_key", "chat_key"),)


class DBChannelQuotaDaily(Model):
    """一个频道在额度时区里的某一天的用量

    `day` 是额度时区下的 `YYYY-MM-DD`。行不存在就等于当天用量为 0，读的时候**不建行**：
    判定在每条入站消息的路径上，为了读一个 0 而写库会把只读路径变成写路径。
    """

    id = fields.BigIntField(pk=True, generated=True, description="ID")

    chat_key = fields.CharField(max_length=256, index=True, description="会话标识")
    adapter_key = fields.CharField(max_length=64, default="", description="适配器标识")
    instance_key = fields.CharField(max_length=64, default="", description="实例标识")
    day = fields.CharField(max_length=10, description="额度时区下的日期键 YYYY-MM-DD")

    replies = fields.IntField(default=0, description="当天完成的回复回合数")
    tokens_all = fields.BigIntField(default=0, description="账本里记在该频道的全部 token")
    tokens_reply = fields.BigIntField(default=0, description="其中回复主链路与上下文规划的部分")

    bonus = fields.BigIntField(default=0, description="平台方当天临时加额（按生效单位）")
    alerted_level = fields.IntField(default=0, description="当天已发过的最高告警阈值（百分比）；0 = 没发过")
    pause_notice_sent_at = fields.DatetimeField(null=True, description="当天暂停提示发出的时间；NULL = 还没发")

    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "channel_quota_daily"
        unique_together = (("chat_key", "day"),)


__all__ = ["DBChannelQuotaDaily", "DBChannelQuotaPolicy"]
