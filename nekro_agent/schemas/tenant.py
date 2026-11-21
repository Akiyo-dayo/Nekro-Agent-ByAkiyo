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

# 后台账号初始口令的最小长度；平台方和租户管理员建的账号受同一条限制。
MIN_INITIAL_PASSWORD_LENGTH = 12


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

    `clear_memory_and_affection` 只作用于**这次保存里改变归属**的实例（新划入的、被收回的）；
    原样重放不改变任何实例的归属，因此不会清掉任何数据。清的是这些实例的域行记忆、摘要、
    提示词明细、世界书、**自成一域**的好感与收集的表情，工作区行不动（它们跟着工作区走）。
    这是真删、不可恢复。

    收集的表情（`emotion_set` 里那个实例的收集库）清空的是**行、向量与文件**，收集库套组本身
    留着一个空壳：它跟着实例走，下次会话进来还是同一个套组 ID，配置里引用的 ID 不会失效。
    平台方指向这些实例的套组分配**每次都取消**，不受这个开关控制——划转之后实例换了主人，
    原来「给这个实例」的授权不该自动跟过去。

    `delete_ssh_credentials` 同样只作用于改变归属的实例，默认删除：凭据是「能进别人家门」
    的东西，误跟过去比误删代价大（与邮箱划转同一条取舍）。取消勾选才让 `user` 行跟着实例走；
    平台方行没有实例归属，不受影响。

    `delete_instance_mailboxes` 默认删除：实例自己填的邮箱是**上一任主人**的收件箱
    （密码重置、验证码、账单都在里面），跟着实例划到新租户名下等于把那些信交出去。
    同样只作用于改变归属的实例，并在改归属之前处理。取消勾选时账户行跟着实例走，
    新租户在后台看得见、也能改——那是一次有意识的「整个邮箱一起转交」。

    `delete_plugin_settings` 默认删除：实例级与频道级的插件启停、实例级插件配置
    （`plugin_data/<插件>/instances/<实例>/`，API Key 之类就在这里）。同样只作用于改变归属的实例，
    在改归属之前处理；取消勾选时这些设置按实例标识存着，自然跟着实例走。

    `delete_config_overrides` 默认删除：核心配置的实例层覆盖与这个实例频道上的频道层覆盖（告警
    Webhook、对模型组的选择、提示词与输出过滤词表等）。口径与插件设置相同。
    """

    instance_keys: Optional[List[str]] = Field(default=None, description="该租户的完整实例集合；显式传 [] 表示解绑全部")
    clear_memory_and_affection: bool = Field(
        default=False,
        description="一并清空这些实例的记忆、世界书、好感、收集的表情与用户邮件偏好；工作区行不动，不可恢复",
    )
    delete_ssh_credentials: bool = Field(
        default=True,
        description="划转时一并删除这些实例上用户登记的服务器凭据（默认删除；取消才跟着实例走）",
    )
    delete_instance_mailboxes: bool = Field(
        default=True,
        description="划转时一并删除这些实例自己填的邮箱账户（默认删除；取消才跟着实例走）",
    )
    delete_plugin_settings: bool = Field(
        default=True,
        description="划转时一并删除这些实例的插件设置：实例级与频道级启停、实例级插件配置（默认删除；取消才跟着实例走）",
    )
    delete_config_overrides: bool = Field(
        default=True,
        description="划转时一并删除这些实例的配置覆盖：核心配置的实例层与频道层覆盖（默认删除；取消才跟着实例走）",
    )


class TenantInstanceTransferChange(BaseModel):
    """划转预览里的一行：一个实例的归属变化与它会带走的记忆规模

    按**行**而不是按 `instance_key` 列：同名实例可以同时存在于两个适配器下，它们的
    归属与数据是两份。`in_group` 表示保存后会被移出实例组（回到自成一域）。
    """

    adapter_key: str = Field(..., description="适配器标识")
    instance_key: str = Field(..., description="实例标识")
    display_name: str = Field(default="", description="实例显示名")
    from_tenant_id: Optional[int] = Field(default=None, description="原归属租户；收回本租户实例时为本租户")
    from_tenant_name: str = Field(default="", description="原归属租户名；划入的实例为空")
    to_tenant_id: Optional[int] = Field(default=None, description="新归属租户；被收回的实例为空")
    to_tenant_name: str = Field(default="", description="新归属租户名；被收回的实例为空")
    in_group: bool = Field(default=False, description="保存后会被移出实例组")
    memory_paragraphs: int = Field(default=0, description="域行段落数（正常状态）")
    memory_quarantined: int = Field(default=0, description="域行段落数（隔离中）")
    memory_summaries: int = Field(default=0, description="摘要份数，按（频道, 人设）计")
    memory_prompt_rounds: int = Field(default=0, description="提示词明细轮数")
    worldbook_books: int = Field(default=0, description="世界书本数，按这个实例对归属计")
    worldbook_entries: int = Field(default=0, description="这些世界书里的条目数")
    memory_affection_rows: int = Field(default=0, description="自成一域的好感行数")
    memory_affection_events: int = Field(default=0, description="自成一域的好感事件数")
    collected_emotions: int = Field(
        default=0,
        description="这个实例的收集库里有几条表情（保存时勾了清空就删这些；平台方给它的套组分配另算，每次都取消）",
    )
    channel_quota_tenant_settings: int = Field(
        default=0,
        description="该实例上带租户额度设置的策略行数（实例层 + 频道层，四个租户字段任一非空）；保存时这些设置会被清空",
    )
    ssh_credentials: int = Field(
        default=0,
        description="该实例对上用户登记的 SSH 服务器凭据行数；保存时默认删除，取消勾选才跟着实例走",
    )
    email_accounts: int = Field(
        default=0,
        description="该实例自己填的邮箱账户数；保存时默认删除，取消勾选才跟着实例走",
    )
    email_user_prefs: int = Field(
        default=0,
        description="该实例上的用户邮件偏好行数；勾了清空记忆时一并清空，否则跟着实例走",
    )
    plugin_settings: int = Field(
        default=0,
        description="该实例名下有设置的插件个数（实例级启停、频道级启停、实例级配置的并集）；保存时默认删除，取消勾选才跟着实例走",
    )
    config_overrides: int = Field(
        default=0,
        description="该实例名下有生效覆盖的配置文件个数（实例层 + 它的频道）；保存时默认删除，取消勾选才跟着实例走",
    )


class TenantInstanceTransferPreview(BaseModel):
    """一次租户实例划转会改变哪些实例、各自带走多少记忆

    只列**改变归属**的实例；原样重放（集合没变）时 `changes` 为空。预览不写任何东西，
    `clear_memory_and_affection` 的效果由保存时的 PUT 执行。
    """

    changes: List[TenantInstanceTransferChange] = Field(default_factory=list)


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
    # `upsert_platform_group()`、`import_config_group()`，现已收敛到
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
