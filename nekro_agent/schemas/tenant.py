"""多租户的对外数据契约

本模块是后端各子模块与前端之间的唯一契约来源，改动需同步前端 `frontend/src/services/api/tenant.ts`。

## 运营模型

平台方（你）运营两类 Bot：

- **共用版**：多个客户共用平台方的实例与模型凭据
- **定制版**：客户拥有独立实例，可以访问自己的后台自主管理

因此租户不是"另做一套后台"，而是**被限定在自己实例作用域里的同一套 UI**。

## 三级权限

```
PLATFORM_OWNER  平台方，全局可见可管，独占模型凭据与容器操作
    └─ TENANT_ADMIN  租户管理员，只能管理自己名下的实例与资源
         └─ TENANT_USER  租户下的普通用户，只读
```

## 凭据隔离是硬约束

定制版客户**永远不能拿到平台方的模型 API Key**，无论通过 WebUI、API、AI 生成的沙盒代码还是日志。
但客户**可以自建模型组接入自己的 API**，此时那份凭据属于他自己，他可读可写。

由此模型组分成两类，见 `ModelGroupOwnership`。
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from nekro_agent.core.config import ModelConfigGroup


class TenantRole(str, Enum):
    """租户体系下的角色"""

    PLATFORM_OWNER = "platform_owner"
    TENANT_ADMIN = "tenant_admin"
    TENANT_USER = "tenant_user"


class TenantStatus(str, Enum):
    """租户状态"""

    ACTIVE = "active"
    SUSPENDED = "suspended"  # 停用：登录被拒，其实例停止收发
    ARCHIVED = "archived"


class ModelGroupOwnership(str, Enum):
    """模型组的归属

    决定凭据可见性，是整个多租户里最敏感的一个判定。
    """

    PLATFORM = "platform"  # 平台方所有：租户可使用，但**永远读不到** api_key
    TENANT = "tenant"  # 租户自建：该租户可读可写自己的凭据


class TenantInfo(BaseModel):
    """租户概要"""

    id: int = Field(..., description="租户 ID")
    name: str = Field(..., description="租户标识（唯一）")
    display_name: str = Field(default="", description="显示名称")
    status: TenantStatus = Field(default=TenantStatus.ACTIVE)
    instance_count: int = Field(default=0, description="名下实例数")
    member_count: int = Field(default=0, description="成员数")
    contact: str = Field(default="", description="联系方式")
    note: str = Field(default="", description="备注")
    create_time: Optional[str] = Field(default=None)


class TenantListResponse(BaseModel):
    items: List[TenantInfo] = Field(default_factory=list)
    total: int = Field(default=0)


class TenantCreateRequest(BaseModel):
    name: str = Field(..., description="租户标识，仅小写字母数字下划线，创建后不可改")
    display_name: str = Field(default="")
    contact: str = Field(default="")
    note: str = Field(default="")


class TenantUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(default=None)
    status: Optional[TenantStatus] = Field(default=None)
    contact: Optional[str] = Field(default=None)
    note: Optional[str] = Field(default=None)


class TenantMemberInfo(BaseModel):
    """租户成员"""

    user_id: int = Field(...)
    username: str = Field(...)
    role: TenantRole = Field(default=TenantRole.TENANT_USER)
    is_active: bool = Field(default=True)


class TenantMemberCreateRequest(BaseModel):
    username: str = Field(..., description="登录名")
    password: str = Field(..., description="初始密码")
    role: TenantRole = Field(default=TenantRole.TENANT_ADMIN)


class TenantInstanceBindRequest(BaseModel):
    """把实例划归某租户

    `instance_keys` 是该租户的**完整实例集合**（幂等覆盖），不在列表里的既有绑定会被解除。

    默认值刻意是 `None` 而不是空列表：这两者在覆盖语义下的后果天差地别——显式传 `[]`
    是「解绑全部」的有意操作，而根本没带这个字段是客户端出了问题。默认成空列表会让后者
    静默变成前者，把一次失败的请求变成一次全量解绑，因此这里由路由层拒掉 `None`。
    """

    instance_keys: Optional[List[str]] = Field(default=None, description="该租户的完整实例集合；显式传 [] 表示解绑全部")


class ModelGroupInfo(BaseModel):
    """模型组（对外形态）

    `api_key` 字段的取值规则是本契约最关键的部分：

    - 平台模型组 + 平台方查看 → 明文
    - 平台模型组 + 租户查看   → **恒为掩码串，且 `editable=False`**
    - 租户模型组 + 该租户查看 → 明文
    - 租户模型组 + 其他租户   → 该组根本不出现在列表里

    `config` 是同一份数据的完整形态，供编辑表单直接回填；它里面的
    `API_KEY` / `BASE_URL` / `CHAT_PROXY` 走的是与上面完全相同的掩码规则
    （见 `model_group_service.mask_config_group`），不存在"顶层掩码了但 config 里是明文"
    这种绕过口子。顶层的 `base_url` / `api_key` 保留下来是为了列表展示，不再重复解释一遍规则。
    """

    name: str = Field(..., description="模型组名")
    ownership: ModelGroupOwnership = Field(default=ModelGroupOwnership.PLATFORM)
    owner_tenant_id: Optional[int] = Field(default=None, description="归属租户；平台组为 None")
    # **刻意没有默认值。** 这里曾经写着 `default="shared"` —— 那是"新建组默认可见性"这个
    # 判据的第**五**份副本（另外四份：字段默认、`create_group()` 形参、
    # `upsert_platform_group()`、`import_config_group()`，round-22 已收敛到
    # `models/db_model_group.DEFAULT_VISIBILITY`）。它今天恰好走不到（唯一的构造点
    # `build_model_group_info()` 每次都显式传 `group.visibility`，把它改成 "private"
    # 一条用例都不会红），但它会通过 OpenAPI schema 把 "shared" 宣称成默认值，
    # 且下一个构造点忘了传就会静默落回一个与后端真实默认相反的值。
    # 这一层不该有默认：可见性是行上的事实，谁构造谁负责给出。
    visibility: str = Field(
        ...,
        description="平台组对租户的开放程度 shared/private；租户自建组恒为 private",
    )
    base_url: str = Field(default="", description="接口地址；平台组对租户展示时同样掩码")
    api_key: str = Field(default="", description="凭据；不可见时为掩码串而非真实值")
    chat_model: str = Field(default="")
    model_type: str = Field(default="chat")
    editable: bool = Field(default=False, description="当前身份能否编辑该组")
    credential_visible: bool = Field(default=False, description="当前身份能否看到真实凭据")
    config: ModelConfigGroup = Field(
        default_factory=ModelConfigGroup,
        description="完整配置，凭据字段按 credential_visible 掩码；供编辑表单回填",
    )


class ModelGroupListResponse(BaseModel):
    items: List[ModelGroupInfo] = Field(default_factory=list)
    total: int = Field(default=0)


class CurrentIdentity(BaseModel):
    """当前登录身份，前端据此决定展示哪一套 UI"""

    user_id: int = Field(...)
    username: str = Field(...)
    role: TenantRole = Field(default=TenantRole.PLATFORM_OWNER)
    tenant_id: Optional[int] = Field(default=None)
    tenant_name: str = Field(default="")
    accessible_instance_keys: List[str] = Field(
        default_factory=list,
        description="该身份可访问的实例；平台方为空列表表示不受限",
    )


# 凭据掩码的统一形态。前端据此判断"这是掩码不是真值"，避免把掩码当密钥保存回去
CREDENTIAL_MASK = "********"
