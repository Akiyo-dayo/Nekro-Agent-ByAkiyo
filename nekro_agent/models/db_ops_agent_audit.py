"""运维调用审计表

## 这张表为什么存在

运维工具与 `/na_ops` 都能让 Bot 直接改动宿主上的实例、租户与频道。结构化日志只
留在进程的日志环里，重启就没了，也翻不动；这张表是「谁、在哪儿、对什么、动了没动」
的唯一事实来源：每次调用在**执行之前**先写一行 `pending`，执行完再补结果，进程中途
崩掉也留得下「尝试过」的痕迹。后台审计页、`ops_audit_recent` 与 `/na_ops audit`
读的都是它。

## 为什么不存异常原文

异常文本经常夹带上游返回、宿主路径甚至凭据，而这张表的读者比写者多（后台页、聊天
里的审计行）。所以失败只记异常**类名**（`error_type`），参数与原因在入库前都经过
脱敏与限长；正文与工具返回内容一律不落表。

## 保留 365 天

审计要能回溯一整年，又不能让表无限长。`services/ops_agent/audit.py` 在写入成功后
顺手删掉 `created_at` 早于「现在 − 365 天」的行；没有后台循环——功能从没开过的部署
表是空的，不需要清。
"""

from tortoise import fields
from tortoise.models import Model


class DBOpsAgentAudit(Model):
    """一条运维调用记录

    `outcome="pending"` 表示已开始执行、结果未知；正常路径会被关闭成 `executed` /
    `failed`，被授权复核拒绝的则直接以 `refused` 一次写完整行。
    """

    id = fields.IntField(pk=True, generated=True, description="记录号")
    created_at = fields.DatetimeField(auto_now_add=True, index=True, description="写入时间；保留清理按它")
    finished_at = fields.DatetimeField(null=True, description="关闭时间；拒绝的行写入时就填")

    source = fields.CharField(max_length=16, description="来源：agent（沙盒调用）/ na_ops（聊天命令）")
    operator = fields.CharField(max_length=256, default="", description="授权上的运维人员，排序后逗号连接；/na_ops 是发命令的人")
    adapter_key = fields.CharField(max_length=64, default="", description="来源适配器")
    instance_key = fields.CharField(max_length=128, default="", description="来源实例")
    chat_key = fields.CharField(max_length=256, default="", description="来源会话")
    round_id = fields.CharField(max_length=64, default="", description="回合标识；/na_ops 为空串")
    tool = fields.CharField(max_length=64, description="工具名，或 na_ops:<指令>")
    args_json = fields.JSONField(default=dict, description="调用参数：每个值脱敏并截断，最多 10 个键")
    outcome = fields.CharField(max_length=16, description="结果：pending / executed / refused / failed")
    refuse_reason = fields.CharField(max_length=64, default="", description="拒绝原因码，或写操作失败的固定类别")
    result_summary = fields.CharField(max_length=512, default="", description="宿主写的结果摘要，已脱敏截断")
    error_type = fields.CharField(max_length=128, default="", description="异常类名，不含原文")

    class Meta:  # type: ignore
        table = "ops_agent_audit"


__all__ = ["DBOpsAgentAudit"]
