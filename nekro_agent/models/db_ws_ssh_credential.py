"""工作区 SSH 服务器凭据

## 为什么只存在 NA 里

容器里有完整的 shell，任何写进容器文件或进程环境的密码、私钥都能被 CC 的 Bash
一句 `echo` 取走。因此这些值只落 NA 的库，网关（后续任务）在 NA 进程里拿它们登录
目标服务器；容器只知道"有一个叫某别名的服务器可以连"，不知道钥匙长什么样。

## 三个 `_enc` 列是 `nkenc1:` 密文

`password_enc` / `private_key_enc` / `passphrase_enc` 与模型组、工作区资源共用
`tools.credential_cipher` 的主密钥与信封格式。没有可用密钥时本表**拒绝写入**，
不像模型组那样退化成明文：SSH 私钥与 root 口令未必有第二份，写坏了就是永久丢失。
存量明文与历史密钥密文由启动时的 `ssh_credentials_upgrade_at_rest()` 整批重写，
解不开的行跳过并点名，绝不把密文当明文覆盖回去。

## 归属为什么存成两列实例对

`user` 行按实例归属（适配器 + 实例标识）跟着租户走，不在行上另存租户 ID：工作区
本身的归属已经由绑定频道推导，凭据行再存一份就会与网络分组、频道绑定产生第二套
事实来源。审计表另存归属快照的理由见 `db_ws_ssh_audit.py`。
"""

from tortoise import fields
from tortoise.models import Model
from tortoise.signals import pre_save

from nekro_agent.models.nul_guard import strip_nul_before_save


class DBWsSshCredential(Model):
    """一条工作区服务器凭据：一行 = 一台服务器的一个账号"""

    id = fields.IntField(pk=True, generated=True, description="ID")
    workspace = fields.ForeignKeyField(
        "models.DBWorkspace",
        related_name="ssh_credentials",
        on_delete=fields.CASCADE,
        index=True,
        description="挂在哪个工作区；删工作区连带删凭据",
    )
    owner_kind = fields.CharField(max_length=16, description="归属：platform / user")
    owner_account = fields.CharField(max_length=128, default="", description="user 行的凭据主人；platform 行为空")
    adapter_key = fields.CharField(max_length=64, default="", description="user 行归属实例的适配器；platform 行为空")
    instance_key = fields.CharField(max_length=64, default="", description="user 行归属实例标识；platform 行为空")
    created_via = fields.CharField(max_length=16, description="登记途径：admin / chat / import")
    import_ref = fields.CharField(max_length=64, default="", description="导入来源，如 resource:12；非空时由服务层保证工作区内唯一")
    alias = fields.CharField(max_length=32, description="工作区内唯一的别名，CC 用 ssh <别名>")
    host = fields.CharField(max_length=255, description="地址或域名")
    port = fields.IntField(default=22, description="SSH 端口")
    username = fields.CharField(max_length=64, description="目标服务器上的登录名")
    password_enc = fields.TextField(default="", description="密码，nkenc1 信封")
    private_key_enc = fields.TextField(default="", description="私钥文本，nkenc1 信封")
    passphrase_enc = fields.TextField(default="", description="私钥口令短语，nkenc1 信封")
    host_keys = fields.JSONField(default=list, description="钉住的目标主机公钥，类型与 base64 两段")
    status = fields.CharField(max_length=16, default="active", description="active / disabled / suspended / auth_failed")
    status_reason = fields.CharField(max_length=64, default="", description="状态原因，如 workspace_not_platform")
    note = fields.TextField(default="", description="给人看的备注，不进任何提示词")
    created_by = fields.CharField(max_length=128, default="", description="后台用户名，或 chat:<账号> / import")
    updated_by = fields.CharField(max_length=128, default="", description="最后修改者")
    last_used_at = fields.DatetimeField(null=True, description="最近一次放行的连接")
    last_error = fields.CharField(max_length=255, default="", description="最近一次失败的简述，不含凭据")
    create_time = fields.DatetimeField(auto_now_add=True, description="创建时间")
    update_time = fields.DatetimeField(auto_now=True, description="更新时间")

    class Meta:  # type: ignore
        table = "ws_ssh_credential"
        unique_together = (("workspace", "alias"),)
        indexes = [
            ("adapter_key", "instance_key"),
            ("owner_kind", "workspace_id"),
            ("workspace_id", "import_ref"),
        ]


# 备注等自由文本可能带 NUL；字段校验已经拒了主机、账号与私钥里的 NUL，这里是兜底
pre_save(DBWsSshCredential)(strip_nul_before_save)


__all__ = ["DBWsSshCredential"]
