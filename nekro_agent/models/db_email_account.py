"""实例自有邮箱账户

## 为什么平台方的账户不在这张表里

平台方账户仍在 email 适配器的 `RECEIVE_ACCOUNTS` 配置里：OAuth、运行状态邮件、账户页的
新增 / 测试 / 手动收信全都按那份配置的既有形态工作，把它们搬进表里等于同时重写那一整条链。
这张表只放**实例自有**的账户，两处在运行时合成一份（见 `services/email_accounts.py`）。

## `username` 为什么全局唯一

账户地址是发信与收信的对外身份，同一个地址同时出现在两个归属下会带来两个问题：附件目录
按地址 / 按账户分不清、IMAP 轮询同一个邮箱两次会重复入库。实际唯一性是「这张表 + 适配器配置」
合起来的全局唯一，服务层保存前两边都查（见 `assert_address_available`）；表上的唯一约束只是
兜住并发写入这一侧。

存之前统一 `strip().lower()`：邮件服务器的用户名不区分大小写，同一个地址大小写不同会让
「这个地址能不能用」的判定与运行时客户端池的键（也是地址）对不上。

## `settings` 里放什么

只放没被预设覆盖的那部分：自定义服务器的主机与端口、是否 SSL、最近一次测试结果。
选了预设的账户不存主机端口，运行时按预设取——预设改了（主机迁移、端口调整）就跟着生效，
不需要逐个账户回填。

## `attachment_dir`

附件根目录名（相对 `uploads/email_attachment/`）。新建的是 `_i<id>`：表 ID 不复用，所以
地址换了主人之后新主人永远看不到旧主人的文件。由平台方账户「转为实例自有」来的沿用原来的
`<地址>`，文件不搬。
"""

from tortoise import fields
from tortoise.models import Model

from nekro_agent.models.fields import EncryptedTextField


class DBEmailAccount(Model):
    """某个实例自己填的邮箱账户"""

    id = fields.IntField(pk=True, generated=True, description="ID；新建账户的附件目录名取它")
    owner_adapter_key = fields.CharField(max_length=64, description="归属的适配器标识")
    owner_instance_key = fields.CharField(max_length=64, description="归属的实例标识")
    username = fields.CharField(
        max_length=256,
        unique=True,
        description="邮箱地址（去首尾空白、转小写后存）；与适配器配置里的地址合起来全局唯一",
    )
    display_name = fields.CharField(max_length=128, default="", description="显示名；空则显示地址")
    provider = fields.CharField(
        max_length=32,
        default="自定义",
        description="服务商预设键（与 EMAIL_PROVIDER_CONFIGS 的键一致），'自定义' 表示自填服务器",
    )
    settings = fields.JSONField(
        default=dict,
        description="自定义服务器与端口、SSL、最近一次测试结果；选了预设的不存主机端口",
    )
    password = EncryptedTextField(default="", description="授权码 / 密码（静态加密存储）")
    enabled = fields.BooleanField(default=True, description="启用账户；关闭后不参与任何收发")
    receive_enabled = fields.BooleanField(default=True, description="启用收信（IMAP）")
    send_enabled = fields.BooleanField(default=False, description="启用发信（SMTP）")
    is_default_sender = fields.BooleanField(
        default=False,
        description="同一归属实例对里最多一个默认发件人；由接口在保存时互斥",
    )
    notify_chat_key = fields.CharField(
        max_length=256,
        default="",
        description="新邮件通知推到哪个频道；必须属于归属实例",
    )
    attachment_dir = fields.CharField(
        max_length=128,
        default="",
        description="附件根目录名（相对 uploads/email_attachment/）：新建的是 _i<id>，转为实例自有的沿用原地址",
    )
    created_by = fields.CharField(max_length=64, default="", description="创建者用户名")
    updated_by = fields.CharField(max_length=64, default="", description="最后修改者用户名")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "email_account"
        indexes = [
            # 按实例对取「这个实例有哪些自有账户」是最常见的查询
            ("owner_adapter_key", "owner_instance_key"),
        ]
