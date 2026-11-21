"""发信记录

## 为什么单独一张表

`send_email` 的每一次成功与失败都要留痕：发信没有数量上限（把关靠 Agent 自审与发信理由），
兜底就是这份记录加上平台方随时取消共享。记录里**不存正文**——它是「谁在什么场合用哪个账户
给谁发了一封什么主题的信、理由是什么」，不是外部内容的第二份副本。

## 两个 `account_ref`

- 平台方账户：地址的 `sha256` 前 16 个十六进制。记录本身要能回答「哪个账户发的」，
  而这张表的读者里也有租户管理员，不能直接落地址；用哈希是为了同一个账户的行能对上。
- 实例自有账户：直接存 `email_account.id`（表 ID 不复用，不会指错）。

## `tenant_id` 是快照

发信时的租户归属。划转之后新租户按它过滤，看不到上一任租户记录下来的那些行——这与
「发信记录留在原处、不跟着实例走」是同一条取舍。

## 保留 365 天

写入成功后顺手删掉超过 365 天的行，进程内每 24 小时最多一次（与运维审计同一个做法：
不加后台循环，功能从没开过的部署表是空的）。
"""

from tortoise import fields
from tortoise.models import Model


class DBEmailSendLog(Model):
    """一次发信尝试"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    account_kind = fields.CharField(max_length=16, description="发件账户类型：platform / instance")
    account_ref = fields.CharField(
        max_length=128,
        description="发件账户引用：平台方存地址 sha256 前 16 位，实例自有存 email_account.id",
    )
    adapter_key = fields.CharField(max_length=64, default="", description="发信实例所属适配器")
    instance_key = fields.CharField(max_length=64, default="", description="发信实例标识")
    chat_key = fields.CharField(max_length=256, default="", description="发信的会话；节日邮件为空")
    tenant_id = fields.IntField(null=True, description="发信时的租户快照；平台方为 NULL")
    purpose = fields.CharField(max_length=16, description="用途：send_email / festival")
    to_address = fields.CharField(max_length=512, description="收件地址")
    subject = fields.CharField(max_length=256, default="", description="主题（截断 256 字）")
    reason = fields.CharField(max_length=500, default="", description="发信理由（截断 500 字）")
    ok = fields.BooleanField(default=False, description="是否发送成功")
    error = fields.CharField(max_length=256, default="", description="失败原因（截断 256 字），成功时为空")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")

    class Meta:  # type: ignore
        table = "email_send_log"
        indexes = [
            # 后台按实例翻记录
            ("adapter_key", "instance_key", "create_time"),
            # 租户只看自己那份快照
            ("tenant_id", "create_time"),
            # 节日邮件按天去重：同一个发件账户、同一个地址今天发过没有
            ("adapter_key", "instance_key", "purpose", "to_address", "create_time"),
        ]
