from typing import List, Literal, Optional

from pydantic import BaseModel, Field

ResourceFieldValueKind = Literal[
    "text",
    "password",
    "host",
    "port",
    "private_key",
    "username",
    "database",
    "json",
]
ResourceExportMode = Literal["env", "none"]


class ResourceField(BaseModel):
    field_key: str = ""
    label: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)
    secret: bool = False
    value_kind: ResourceFieldValueKind = "text"
    order: int = 0
    export_mode: ResourceExportMode = "env"
    fixed_aliases: List[str] = Field(default_factory=list)
    value: str = ""


class ResourceTemplate(BaseModel):
    key: str
    name: str
    summary: str
    resource_note: str = ""
    resource_tags: List[str] = Field(default_factory=list)
    resource_prompt: str = ""
    fields: List[ResourceField] = Field(default_factory=list)


class WorkspaceResourceBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    template_key: Optional[str] = Field(default=None, max_length=64)
    resource_note: str = Field(default="", max_length=2000)
    resource_tags: List[str] = Field(default_factory=list)
    resource_prompt: str = Field(default="", max_length=4000)
    fields: List[ResourceField] = Field(default_factory=list)
    enabled: bool = True


class WorkspaceResourceCreate(WorkspaceResourceBase):
    pass


class WorkspaceResourceUpdate(WorkspaceResourceBase):
    pass


class BoundWorkspaceInfo(BaseModel):
    id: int
    name: str


class WorkspaceResourceSummary(BaseModel):
    id: int
    resource_key: str
    name: str
    template_key: Optional[str] = None
    resource_note: str = ""
    resource_tags: List[str] = Field(default_factory=list)
    resource_prompt: str = ""
    field_count: int = 0
    fixed_aliases: List[str] = Field(default_factory=list)
    enabled: bool = True
    bound_workspace_count: int = 0
    bound_workspaces: List[BoundWorkspaceInfo] = Field(default_factory=list)
    # 指向已不存在的工作区的绑定条数。与 `bound_workspace_count` 分开数是有意的：
    # 合在一起时资源卡片会显示"已绑定 1 个工作区"而下面的工作区清单是空的，
    # 用户看到一个自相矛盾的界面，且没有任何入口能处理它。
    orphan_binding_count: int = 0
    create_time: str
    update_time: str


class WorkspaceResourceDetail(WorkspaceResourceSummary):
    fields: List[ResourceField] = Field(default_factory=list)


class WorkspaceResourceListResponse(BaseModel):
    items: List[WorkspaceResourceSummary]


class WorkspaceResourceDetailResponse(BaseModel):
    item: WorkspaceResourceDetail


class WorkspaceResourceTemplatesResponse(BaseModel):
    items: List[ResourceTemplate]


class WorkspaceResourceBinding(BaseModel):
    binding_id: int
    resource_id: int
    enabled: bool = True
    sort_order: int = 0
    note: str = ""
    resource: WorkspaceResourceSummary


class WorkspaceResourceBindingsResponse(BaseModel):
    items: List[WorkspaceResourceBinding]


class WorkspaceResourceBindBody(BaseModel):
    note: str = Field(default="", max_length=256)


class WorkspaceResourceReorderItem(BaseModel):
    binding_id: int
    sort_order: int


class WorkspaceResourceReorderBody(BaseModel):
    items: List[WorkspaceResourceReorderItem]


class WorkspaceResourceCheckBindBody(BaseModel):
    resource_id: int


class WorkspaceResourceConflict(BaseModel):
    env_name: str
    existing_resource_id: int
    existing_resource_name: str
    target_resource_id: int
    target_resource_name: str


class WorkspaceResourceCheckBindResponse(BaseModel):
    ok: bool
    conflicts: List[WorkspaceResourceConflict] = Field(default_factory=list)


class WorkspaceResourceSecretIssue(BaseModel):
    resource_id: int
    resource_name: str
    reason: str


class WorkspaceResourceSecretHealthResponse(BaseModel):
    ok: bool
    total: int = 0
    encrypted_count: int = 0
    plaintext_count: int = 0
    empty_count: int = 0
    legacy_encrypted_count: int = 0
    invalid_count: int = 0
    # 下一次启动会被重写的行数（存量明文 + 用历史密钥加密的行）
    stale_count: int = 0
    issues: List[WorkspaceResourceSecretIssue] = Field(default_factory=list)


class WorkspaceResourceSecretRecoverBody(BaseModel):
    legacy_secret: str = Field(..., min_length=1, description="旧 NEKRO_JWT_SECRET_KEY")


class WorkspaceResourceSecretRecoverResponse(BaseModel):
    ok: bool
    recovered_count: int = 0
    invalid_count: int = 0
    issues: List[WorkspaceResourceSecretIssue] = Field(default_factory=list)


class WorkspaceResourceSecretPurgeResponse(BaseModel):
    ok: bool
    purged_count: int = 0


OrphanBindingReason = Literal["workspace_missing", "resource_missing", "both_missing"]


class WorkspaceResourceOrphanBinding(BaseModel):
    """一条悬空的工作区资源绑定

    `workspace_resource_binding` 的两个 ID 列都是普通 `IntField` 而不是外键，
    所以两端各自都可能先消失。两种都在这里报出来：它们在后台上同样是不可见的
    （`list_workspace_bindings` 按 `workspace_id` 查得从工作区那头进来，
    而且对查不到资源行的绑定直接过滤）。
    """

    binding_id: int
    workspace_id: int
    resource_id: int
    resource_name: str = ""
    reason: OrphanBindingReason
    note: str = ""
    create_time: str = ""


class WorkspaceResourceBindingHealthResponse(BaseModel):
    ok: bool
    total: int = 0
    live_count: int = 0
    orphan_count: int = 0
    orphans: List[WorkspaceResourceOrphanBinding] = Field(default_factory=list)


class WorkspaceResourceBindingPurgeResponse(BaseModel):
    ok: bool = True
    purged_count: int = 0


class ActionOkResponse(BaseModel):
    ok: bool = True
