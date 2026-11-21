"""用户邮件偏好

## 记的是什么

一个用户在某**个实例**下留的收件地址、要不要节日祝福、有没有退订。按（适配器, 实例, 平台用户）
一行：同一个人在两个租户的 Bot 眼里是两个身份，偏好互不相干。

## 为什么只有这些列

地址与订阅由用户自己在会话里用自然语言改（`email_utils` 那三个方法），身份取宿主记下的
「本轮发言人」，不认模型填的任何用户 ID——免得群友让 Bot 把别人的祝福改投到自己的邮箱、
或者替别人退订。所以这里没有任何「代管」字段。

## 删除时保留退订标记

后台删一行偏好时只清掉地址与订阅，`unsubscribed` / `unsubscribed_at` 留着：否则删一次偏好
就等于重新订阅了「和本实例开过私聊」那一类默认收件人。只有实例划转时「一并清空」才把退订标记
一起清掉——那时候换的是新主人，旧主人名下拒绝过的事不该由新主人继承。
"""

from tortoise import fields
from tortoise.models import Model


class DBEmailUserPref(Model):
    """某个用户在一个实例下的邮件偏好"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    adapter_key = fields.CharField(max_length=64, description="记下这条偏好的实例所属适配器")
    instance_key = fields.CharField(max_length=64, description="记下这条偏好的实例标识")
    platform_userid = fields.CharField(max_length=128, description="宿主记下的发送者账号")
    email = fields.CharField(max_length=320, default="", description="用户留的收件地址；空 = 用默认地址")
    festival_subscribed = fields.BooleanField(default=False, description="在群里说过要节日祝福邮件")
    unsubscribed = fields.BooleanField(default=False, description="说过别再发邮件；两类收件人都不再发")
    unsubscribed_at = fields.DatetimeField(null=True, description="退订时间")
    set_in_chat_key = fields.CharField(max_length=256, default="", description="最后一次设置发生在哪个频道")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "email_user_pref"
        unique_together = (("adapter_key", "instance_key", "platform_userid"),)
