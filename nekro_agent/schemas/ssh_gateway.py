"""SSH 服务器凭据的后台接口契约

列表与详情里的三个凭据字段**永远是掩码或空串**，明文只走单独的查看接口；前端把
`CREDENTIAL_MASK` 原样提交回来表示"保持不变"，与工作区资源页同一语义。
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

OwnerKind = Literal["platform", "user"]

# 与 `services.ssh_gateway.validation.MAX_PRIVATE_KEY_CHARS` 相同（有用例钉住）；接口层先挡一道，
# 免得超长文本进到解析里
PRIVATE_KEY_MAX_LENGTH = 8192


class SshHostKeyView(BaseModel):
    """钉住的一把目标主机公钥"""

    entry: str = Field(..., description="类型与 base64 两段")
    fingerprint: str = Field(..., description="SHA256 指纹，与 ssh-keygen -lf 一致")


class SshCredentialCreate(BaseModel):
    """新建一行凭据"""

    owner_kind: OwnerKind = Field(default="platform", description="platform / user")
    owner_account: str = Field(default="", description="user 行的凭据主人；platform 行留空")
    adapter_key: str = Field(default="", description="user 行归属实例的适配器")
    instance_key: str = Field(default="", description="user 行归属实例标识")
    alias: str = Field(..., max_length=32, description="工作区内唯一的别名")
    host: str = Field(..., max_length=255, description="地址或域名")
    port: int = Field(default=22, description="SSH 端口")
    username: str = Field(..., max_length=64, description="目标服务器上的登录名")
    password: str = Field(default="", description="密码明文；存库前加密")
    private_key: str = Field(default="", max_length=PRIVATE_KEY_MAX_LENGTH, description="私钥文本；存库前加密")
    passphrase: str = Field(default="", description="私钥口令短语；存库前加密")
    host_keys: List[str] = Field(default_factory=list, description="钉住的主机公钥，可多行")
    note: str = Field(default="", description="给人看的备注")


class SshCredentialUpdate(BaseModel):
    """修改一行凭据；`None` 表示这次没有提交这个字段

    三个凭据字段的取值约定与工作区资源页一致：不提交或提交 `CREDENTIAL_MASK` 表示
    保持不变，提交空串表示清空，其它值表示新值并重新加密。
    """

    alias: Optional[str] = Field(default=None, max_length=32)
    host: Optional[str] = Field(default=None, max_length=255)
    port: Optional[int] = None
    username: Optional[str] = Field(default=None, max_length=64)
    password: Optional[str] = None
    private_key: Optional[str] = Field(default=None, max_length=PRIVATE_KEY_MAX_LENGTH)
    passphrase: Optional[str] = None
    host_keys: Optional[List[str]] = None
    note: Optional[str] = None
    status: Optional[str] = None


class SshCredentialView(BaseModel):
    """列表与详情的对外形态"""

    id: int
    workspace_id: int
    workspace_name: str = ""
    owner_kind: str
    owner_account: str = ""
    adapter_key: str = ""
    instance_key: str = ""
    created_via: str
    import_ref: str = ""
    alias: str
    host: str
    port: int
    username: str
    password: str = ""
    private_key: str = ""
    passphrase: str = ""
    host_keys: List[SshHostKeyView] = Field(default_factory=list)
    status: str
    status_reason: str = ""
    note: str = ""
    created_by: str = ""
    updated_by: str = ""
    last_used_at: Optional[str] = None
    last_error: str = ""
    create_time: str = ""
    update_time: str = ""
    has_password: bool = False
    has_private_key: bool = False
    has_passphrase: bool = False
    secret_state: str = "ok"
    owner_whitelisted: Optional[bool] = None
    can_reveal: bool = False
    can_edit_connection: bool = False


class SshBoundInstance(BaseModel):
    """绑在工作区上的一个实例对（新建用户行时选归属实例用）"""

    adapter_key: str
    instance_key: str


class SshCredentialListResponse(BaseModel):
    items: List[SshCredentialView] = Field(default_factory=list)
    total: int = 0
    # 工作区页用：这个工作区绑定的频道数（S3 提醒）与能选的归属实例（租户只看到自己的）。
    # 全局列表不填
    bound_channel_count: int = 0
    bound_instances: List[SshBoundInstance] = Field(default_factory=list)


class SshCredentialSecretResponse(BaseModel):
    """单独查看接口的响应：只在这一条路径上出现明文"""

    password: str = ""
    private_key: str = ""
    passphrase: str = ""


class SshAuditView(BaseModel):
    """一条审计记录"""

    id: int
    create_time: Optional[str] = None
    workspace_id: int
    credential_id: Optional[int] = None
    owner_kind: str = ""
    owner_account: str = ""
    adapter_key: str = ""
    instance_key: str = ""
    event: str
    allowed: bool
    reason: str = ""
    task_ref: str = ""
    lease_id: str = ""
    conn_id: str = ""
    triggered_by: List[str] = Field(default_factory=list)
    channel_kind: str = ""
    detail: str = ""
    input_head: str = ""
    exit_status: Optional[int] = None
    bytes_up: Optional[int] = None
    bytes_down: Optional[int] = None
    duration_ms: Optional[int] = None
    host_key_fp: str = ""
    actor: str = ""


class SshAuditListResponse(BaseModel):
    items: List[SshAuditView] = Field(default_factory=list)
    next_before_id: Optional[int] = None


class SshActionOkResponse(BaseModel):
    ok: bool = True
    message: str = ""


class SshCredentialTestResponse(BaseModel):
    """后台"测试连接"的结果

    主机钥匙对不上时额外带上服务器这次出示的那把与行上已记的指纹列表，前台据此
    让用户核对后再调用接受接口；两个字段在成功时也可以为空。
    """

    ok: bool = False
    reason: str = ""
    message: str = ""
    fingerprint: str = Field(default="", description="这次服务器出示的主机钥匙 SHA256 指纹")
    pinned: bool = Field(default=False, description="这次是否首次记下了主机钥匙")
    presented_entry: str = Field(default="", description="对不上时服务器这次出示的钥匙")
    presented_fingerprint: str = Field(default="", description="对不上时服务器这次出示的指纹")
    host_keys: List[SshHostKeyView] = Field(default_factory=list, description="行上当前记着的钥匙")


class SshHostKeyAcceptRequest(BaseModel):
    """接受测试里看到的那把新主机钥匙；指纹必须与 10 分钟内测试结果逐字相同"""

    entry: str = Field(..., description="类型与 base64 两段")
    fingerprint: str = Field(..., description="SHA256 指纹")


class SshGatewayStatusResponse(BaseModel):
    """网关状态：只给运行数字，不含任何凭据、主机或目标地址"""

    state: str = Field(..., description="off / starting / running / unavailable / tripped")
    reason: str = Field(default="", description="不可用或熔断时的原因；正常时为空")
    listen_addresses: List[str] = Field(default_factory=list, description="网关应当监听的工作区网络地址")
    port: int = Field(default=0, description="网关监听端口")
    connections: int = Field(default=0, description="当前容器侧连接数")
    channels: int = Field(default=0, description="当前通道数")
    running_leases: int = Field(default=0, description="当前生效中的租约数")


class ImportReportEntry(BaseModel):
    """导入成功的一条：工作区、别名与来源资源名"""

    workspace_id: int
    alias: str
    resource_name: str


class ImportReportSkip(BaseModel):
    """没有导入的一条：点名资源、工作区与原因"""

    resource_name: str
    workspace_id: int
    reason: str


class ImportReport(BaseModel):
    """一次存量资源导入的结果

    `already` 是幂等的产物：同一工作区上同一个来源已经有一行时计进来，不再重复导。
    `skipped` 逐条给出原因（工作区不归平台方、资源停用、载荷解不开、字段不全等），
    不能只报一个总数——平台方要靠它决定每一条是改配置还是补字段。
    """

    imported: List[ImportReportEntry] = Field(default_factory=list)
    already: int = 0
    skipped: List[ImportReportSkip] = Field(default_factory=list)


__all__ = [
    "ImportReport",
    "ImportReportEntry",
    "ImportReportSkip",
    "SshActionOkResponse",
    "SshAuditListResponse",
    "SshAuditView",
    "SshBoundInstance",
    "SshCredentialCreate",
    "SshCredentialListResponse",
    "SshCredentialSecretResponse",
    "SshCredentialTestResponse",
    "SshCredentialUpdate",
    "SshCredentialView",
    "SshGatewayStatusResponse",
    "SshHostKeyAcceptRequest",
    "SshHostKeyView",
]
