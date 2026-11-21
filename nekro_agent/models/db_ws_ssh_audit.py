"""SSH 审计记录

与凭据表分开放：凭据表的行会被改、被删，审计行只追加。`workspace_id` 与
`credential_id` **不设外键**，因为工作区或凭据删除后，审计仍要能回答"谁在什么时候
看过、改过、连过这台服务器"；级联删除会把唯一的证据一起删掉。

归属的四个字段是**写入时的快照**，不是从凭据行实时联出来的：行删了以后没有地方
可联，而且归属后来变化时历史记录也应保持当时的解释。租户审计列表按快照过滤，
理由见 `services/ssh_gateway/credentials.py`。

`detail` 只放字段名、命令原文、转发目的地这类信息，**永远不放凭据值**；
服务器输出本身不存，`sftp` 与转发通道只记字节数。
"""

from tortoise import fields
from tortoise.models import Model
from tortoise.signals import pre_save

from nekro_agent.models.nul_guard import strip_nul_before_save


class DBWsSshAudit(Model):
    """一条 SSH 相关审计记录"""

    id = fields.BigIntField(pk=True, generated=True, description="记录号")
    create_time = fields.DatetimeField(auto_now_add=True, index=True, description="写入时间；保留清理按它")
    workspace_id = fields.IntField(index=True, description="工作区 ID；不设外键")
    credential_id = fields.IntField(null=True, index=True, description="凭据 ID；不设外键")
    owner_kind = fields.CharField(max_length=16, default="", description="归属快照：platform / user")
    owner_account = fields.CharField(max_length=128, default="", description="归属快照：凭据主人")
    adapter_key = fields.CharField(max_length=64, default="", description="归属快照：实例适配器")
    instance_key = fields.CharField(max_length=64, default="", description="归属快照：实例标识")
    event = fields.CharField(max_length=24, description="事件名，见 constants 的 EVENT_*")
    allowed = fields.BooleanField(description="这次操作是否被放行")
    reason = fields.CharField(max_length=48, default="", description="拒绝理由码；放行为空")
    task_ref = fields.CharField(max_length=256, default="", description="CC 任务标识，或后台直发的 __user__")
    lease_id = fields.CharField(max_length=32, default="", description="同一份租约的几行靠它串起来")
    conn_id = fields.CharField(max_length=32, default="", description="同一个连接的几行靠它串起来")
    triggered_by = fields.JSONField(default=list, description="叫醒那一轮的账号，只记账号不记消息")
    channel_kind = fields.CharField(max_length=24, default="", description="exec / shell / sftp / direct-tcpip 等")
    detail = fields.TextField(default="", description="字段名、命令原文、转发目的地；不含凭据值")
    input_head = fields.TextField(default="", description="shell / exec 容器到服务器方向的前若干字节")
    exit_status = fields.IntField(null=True, description="通道或连接的退出码")
    bytes_up = fields.BigIntField(null=True, description="上行字节数")
    bytes_down = fields.BigIntField(null=True, description="下行字节数")
    duration_ms = fields.IntField(null=True, description="持续毫秒数")
    host_key_fp = fields.CharField(max_length=80, default="", description="目标主机公钥的 SHA256 指纹")
    actor = fields.CharField(max_length=128, default="", description="后台用户名、chat:<账号> 或 gateway")

    class Meta:
        table = "ws_ssh_audit"
        # 审计列表永远按"某个工作区 / 某条凭据，按时间倒序"取，两个复合索引各服务一种
        indexes = (
            ("workspace_id", "create_time"),
            ("credential_id", "create_time"),
        )


# 审计里有容器送来的文本：网关的写入队列入队时已经洗过，这里是所有直写路径的兜底
pre_save(DBWsSshAudit)(strip_nul_before_save)


__all__ = ["DBWsSshAudit"]
