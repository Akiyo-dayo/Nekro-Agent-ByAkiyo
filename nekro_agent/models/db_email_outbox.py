"""节日祝福邮件的发信队列

## 为什么要有队列

节日邮件由插件按名单发（收件人从频道表与偏好表算出来），一个实例一个节日的名单可能几十上百人。
一轮里连发几十封会被收件方当成垃圾邮件，所以要按发件邮箱匀速发出、进程重启后接着发。
这一张表就是那个队列：一个收件人一行。

## `body` 发出或作废后清空

与发信记录「不存正文」同一条：队列行只在「还没发」的那段时间里有正文，发完留的是一行
「发过 / 失败 / 跳过」的结果，长期的那份在 `email_send_log` 里。已结束的行保留 7 天后删除。

## `batch_key`

`<节日名>:<日期>:<实例对哈希>`：同一批的行一起派发、一起作废，后台的「作废某一批」按它选行。
"""

from tortoise import fields
from tortoise.models import Model


class DBEmailOutbox(Model):
    """节日邮件队列里的一封"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    batch_key = fields.CharField(max_length=64, description="批次键：<节日名>:<日期>:<实例对哈希>")
    adapter_key = fields.CharField(max_length=64, description="收件人名单属于哪个适配器")
    instance_key = fields.CharField(max_length=64, description="收件人名单属于哪个实例")
    platform_userid = fields.CharField(max_length=128, description="收件人的平台账号")
    account_kind = fields.CharField(max_length=16, description="发件账户类型：platform / instance")
    account_ref = fields.CharField(max_length=128, description="发件账户引用（口径同发信记录）")
    to_address = fields.CharField(max_length=512, description="收件地址")
    subject = fields.CharField(max_length=256, default="", description="主题")
    body = fields.TextField(default="", description="正文；发出或被作废后清空")
    reason = fields.CharField(max_length=500, default="", description="发信理由（宿主写）")
    status = fields.CharField(
        max_length=16,
        default="queued",
        description="状态：queued / paused / sent / failed / skipped",
    )
    attempts = fields.IntField(default=0, description="已尝试发送次数；失败按 1 分钟、5 分钟重试两次")
    next_at = fields.DatetimeField(null=True, description="下一次可以发的时刻；空表示立即可发")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "email_outbox"
        indexes = [
            # 发信工人取「到点了、还没发」的行
            ("status", "next_at"),
            # 后台按批次作废
            ("batch_key",),
        ]
