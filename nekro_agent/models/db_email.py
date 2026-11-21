from tortoise import fields
from tortoise.models import Model


class DBEmail(Model):
    """邮件模型，用于存储已处理的邮件信息

    ## 归属快照

    `owner_adapter_key` / `owner_instance_key` 是**这封信入库时那个账户归谁**的快照，
    不是账户当前归属：账户可以在平台方与某个实例之间转手（「转为实例自有 / 转回平台方」），
    也可以被删掉再重建。读侧一律要求「行上的快照 = 账户当前的归属」，因此一份残留的旧行
    不会以新主人的名义被读到。

    空串 = 平台方，与存量口径一致：老行没有归属列时正好就是平台方自己配的邮箱收的信，
    升级后不需要回填。

    唯一键仍是 `(account_username, email_uid)`：同一个地址换了主人、旧主人的缓存行又没删干净时，
    新信的 UID 可能撞上旧行。写侧用 `update_or_create` 并让 `defaults` 覆盖**全部内容列与两列快照**，
    撞上时整行按新信重写，旧内容不会留下来（见服务层的收信路径）。
    """

    id = fields.IntField(pk=True, generated=True, description="ID")
    account_username = fields.CharField(max_length=256, index=True, description="所属邮箱账户")
    email_uid = fields.CharField(max_length=128, description="IMAP UID")
    message_id = fields.CharField(max_length=512, default="", description="Message-ID 头")
    subject = fields.CharField(max_length=1024, default="", description="邮件主题")
    sender = fields.CharField(max_length=512, default="", description="发件人")
    recipients = fields.TextField(default="", description="收件人(JSON)")
    date = fields.DatetimeField(null=True, description="邮件日期")
    body_text = fields.TextField(default="", description="纯文本正文(截断)")
    has_attachments = fields.BooleanField(default=False, description="是否有附件")
    attachment_names = fields.TextField(default="", description="附件文件名列表(JSON)")
    in_reply_to = fields.CharField(max_length=512, default="", description="In-Reply-To 头")
    references = fields.TextField(default="", description="References 头")
    owner_adapter_key = fields.CharField(
        max_length=64,
        default="",
        description="入库时这封信的账户归属的适配器标识（快照）；空 = 平台方",
    )
    owner_instance_key = fields.CharField(
        max_length=64,
        default="",
        description="入库时这封信的账户归属的实例标识（快照）；空 = 平台方",
    )
    fetched_at = fields.DatetimeField(auto_now_add=True, description="获取时间")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "email"
        unique_together = (("account_username", "email_uid"),)
