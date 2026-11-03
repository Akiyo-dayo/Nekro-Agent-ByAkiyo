"""租户权限依赖

## 两套权限体系必须分开看

项目里同时存在两个权限维度，它们**不是**同一件事的两种写法：

- `Role`（`services/user/role.py`，数值比较）—— 决定**能做多危险的操作**。
  `Role.Super` 是宿主机管理员：总后台的实例增删与协议端容器生命周期都按它校验，
  拿到它等于拿到宿主机 root。
- `TenantRole`（`schemas/tenant.py`）—— 决定**看得到哪些资源**，是横向的数据作用域。

因此租户管理员在旧体系里的上限被钉死在 `Role.Admin`，见 `TENANT_MAX_PERM_LEVEL`。
只要有一处把租户成员建成 `Role.Super`，客户就能通过 Console 的容器接口操作宿主机，
这是本模块最需要守住的一条线。

## 未启用多租户时的行为

没有任何租户记录时，全部账号的 `tenant_id` 都是 NULL、`tenant_role` 都是
`platform_owner`，本模块的每个判定都会走"平台方 / 不受限"分支，
行为与引入多租户之前完全一致。
"""

import operator
from functools import reduce
from typing import Any, Callable, ClassVar, Iterable, List, Optional, Set, Tuple

from fastapi import Depends
from tortoise.expressions import Q

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import AppError, NotFoundError, PermissionDeniedError
from nekro_agent.schemas.i18n import I18nDict, i18n_text
from nekro_agent.schemas.tenant import CurrentIdentity, TenantRole
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role

logger = get_sub_logger("tenant.perm")

# 租户成员在旧权限体系里的硬上限。
# Console 的实例增删与容器操作要求 Role.Super（宿主级），租户成员绝不能达到该等级。
TENANT_MAX_PERM_LEVEL = Role.Admin

# 一个实例的完整标识。`instance_key` 只在同一适配器内唯一。
InstanceRef = Tuple[str, str]


class TenantSuspendedError(AppError):
    """所属租户已停用

    按约定业务错误应集中在 `schemas/errors.py`，这里就地定义是为了不与并行开发的其他模块
    争抢同一个共享文件；全局异常处理器按 `AppError` 捕获，落在哪个模块不影响响应结构。
    """

    http_status: ClassVar[int] = 403
    i18n_message: ClassVar[I18nDict] = i18n_text(
        zh_CN="所属租户已被停用，请联系服务提供方",
        en_US="Your tenant has been suspended, please contact the service provider",
    )

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)


def tenant_role_of(user: DBUser) -> TenantRole:
    """读取用户的租户角色

    数据库里是自由字符串，无法识别时按最小权限的 `TENANT_USER` 处理而不是回落平台方，
    避免一条脏数据把某个账号提权成全局可见。
    """
    try:
        role = TenantRole(user.tenant_role)
    except ValueError:
        logger.warning(f"用户 {user.username} 的 tenant_role 取值非法: {user.tenant_role!r}，按租户普通用户处理")
        return TenantRole.TENANT_USER
    if role is not TenantRole.PLATFORM_OWNER and user.tenant_id is None:
        # 声明了租户角色却没有租户归属，是一条给不出合法作用域的脏数据。
        # 按平台方处理等于凭一条坏数据提权到全局可见，因此退回最小权限。
        logger.warning(f"用户 {user.username} 声明了租户角色 {role.value} 但未绑定租户，按租户普通用户处理")
        return TenantRole.TENANT_USER
    return role


def is_platform_owner(user: DBUser) -> bool:
    """是否为平台方

    只有"未绑定租户"且"角色为 platform_owner"才算平台方，两个条件缺一不可。
    """
    return user.tenant_id is None and user.tenant_role == TenantRole.PLATFORM_OWNER.value


def tenant_perm_level(role: TenantRole) -> Role:
    """把租户角色映射到旧体系的权限等级

    这是"租户永远拿不到 Role.Super"这条不变量的唯一出口：租户管理员至多 `Role.Admin`，
    租户普通用户为 `Role.User`。任何创建/修改租户成员的路径都必须经过这里，
    不允许在调用点自行拼一个 perm_level。
    """
    if role is TenantRole.TENANT_ADMIN:
        return TENANT_MAX_PERM_LEVEL
    if role is TenantRole.TENANT_USER:
        return Role.User
    # PLATFORM_OWNER 不由本函数决定等级：平台方账号的 perm_level 由 admin 登录流程维护
    return Role.Super


def get_tenant_scope(user: DBUser) -> Optional[int]:
    """取当前身份的数据作用域

    Returns:
        None 表示平台方，查询不加租户过滤；否则为该身份只能看到的 `tenant_id`。

    Raises:
        PermissionDeniedError: 账号既不是平台方又没有租户归属。这种行给不出任何合法作用域，
            而 None 在本函数里恰好是"不受限"，直接返回会把一条脏数据变成越权。
    """
    if is_platform_owner(user):
        return None
    if user.tenant_id is None:
        logger.warning(f"用户 {user.username} 既非平台方又未绑定租户，拒绝访问以免作用域退化为不受限")
        raise PermissionDeniedError
    return user.tenant_id


async def accessible_instance_keys(user: DBUser) -> List[str]:
    """列出该身份可访问的实例标识

    平台方返回空列表，语义是"不受限"（见 `CurrentIdentity.accessible_instance_keys` 的契约），
    不要把它理解成"一个实例都看不到"。

    **只用于展示，不要拿去过滤数据。** `instance_key` 的唯一性只到
    `(adapter_key, instance_key)`，另一个适配器下可以有同名实例归别的租户；按取值做
    `instance_key__in` 会把那一行的数据一并放进来。过滤用 `accessible_instance_refs`。
    """
    scope = get_tenant_scope(user)
    if scope is None:
        return []
    return list(await DBAdapterInstance.filter(tenant_id=scope).order_by("id").values_list("instance_key", flat=True))


async def accessible_instance_refs(user: DBUser) -> Set[InstanceRef]:
    """列出该身份可访问的实例，按 `(adapter_key, instance_key)` 成对给出

    平台方返回空集合，语义同 `accessible_instance_keys`：不受限，调用方须先判 `get_tenant_scope`。
    与频道、消息这类同样带两列的数据比对归属时，必须成对比较，见 `instance_refs_filter`。
    """
    scope = get_tenant_scope(user)
    if scope is None:
        return set()
    rows = await DBAdapterInstance.filter(tenant_id=scope).values_list("adapter_key", "instance_key")
    return {(str(adapter_key), str(instance_key)) for adapter_key, instance_key in rows}


def instance_refs_filter(refs: Iterable[InstanceRef]) -> Q:
    """把一组实例编成过滤条件，用于同时带 `adapter_key` 与 `instance_key` 两列的表

    空集合编成恒假条件，而不是"不加条件"——一个实例都没有的租户什么都看不到。
    """
    conditions = [Q(adapter_key=adapter_key, instance_key=instance_key) for adapter_key, instance_key in refs]
    if not conditions:
        return Q(id__in=[])
    return reduce(operator.or_, conditions)


async def assert_instance_accessible(user: DBUser, instance_key: str, adapter_key: str = "") -> DBAdapterInstance:
    """校验当前身份能否访问某个实例，并返回该实例

    越权访问抛 **404 而不是 403**：403 会告诉租户"这个 instance_key 确实存在"，
    等于把平台方的实例清单变成可枚举的。不存在与无权访问对外必须不可区分。

    调用方手上有 `adapter_key` 时必须一起传：同一个 `instance_key` 在不同适配器下可以是
    两行、分属两个租户。只按标识查时，归属条件下推进查询，租户拿到的只可能是自己那一行，
    而不是"随便取一行再比租户"——那样会把别人的同名实例判给自己的请求，或者反过来把
    自己的实例误判成不存在。
    """
    query = DBAdapterInstance.filter(instance_key=instance_key)
    if adapter_key:
        query = query.filter(adapter_key=adapter_key)
    scope = get_tenant_scope(user)
    if scope is not None:
        query = query.filter(tenant_id=scope)
    instance = await query.order_by("id").first()
    if instance is None:
        raise NotFoundError(resource=f"实例 '{instance_key}'")
    return instance


async def assert_managed_user_accessible(user: DBUser, user_id: int) -> DBUser:
    """校验当前身份能否管理某个账号，并返回该账号

    与本模块其他守卫的差别在于：这里的判据不只是调用方的角色，还有**目标账号本身**。
    只看调用方就是 `routers/user_manager.py` 曾经的形态 —— 租户管理员是 `Role.Admin`，
    于是 `POST /{平台方 user_id}/reset-password` 能重置平台方口令后直接登录，
    ban / delete 同理。一条完整的平台接管链。

    两条判据，缺一不可：

    1. **归属**：租户只能管自己租户下的账号（`tenant_id == 自己的 tenant_id`）。
       聊天平台用户（消息入站时自动注册）的 `tenant_id` 恒为 NULL，因而落在平台方一侧 ——
       它们没有归属列，而一个 QQ 号可以同时出现在多个租户的群里，`ban_until` 却是账号行上
       的**一个全局字段**：给任一租户封禁权就等于让它替另一个租户做决定。
       这与工作区 `SHARED_GROUP` 判成平台方专属是同一条理由。
       租户要处理自己群里的成员，走已带作用域的 `/chat-channel/{chat_key}/users`。
    2. **目标等级**：租户调用方的权限低于目标时一律拒绝。租户成员的等级被
       `TENANT_MAX_PERM_LEVEL` 钉死在 `Role.Admin`，正常路径下同租户内不会出现更高的行；
       这一条是兜底，防止手改数据库或历史数据留下的越界行反过来成为提权入口。
       **只对租户调用方生效** —— 平台方一侧不存在需要防的边界，而给它加一条
       "Role.Admin 碰不到 Role.Super" 会改变未启用多租户的部署的既有行为。

    越权与不存在同样抛 **404**，理由见 `assert_instance_accessible`。

    未启用多租户时 `get_tenant_scope()` 恒为 None，两条判据都不进，
    行为与引入多租户之前逐字一致。
    """
    scope = get_tenant_scope(user)
    target = await DBUser.get_or_none(id=user_id)
    if target is None:
        raise NotFoundError(resource="用户")
    if scope is None:
        return target
    if target.tenant_id != scope or target.perm_level > user.perm_level:
        logger.warning(
            f"账号管理越权：{user.username}(tenant={scope}, perm={user.perm_level}) "
            f"试图操作 {target.username}(tenant={target.tenant_id}, perm={target.perm_level})",
        )
        raise NotFoundError(resource="用户")
    return target


async def assert_tenant_accessible(user: DBUser, tenant_id: int) -> DBTenant:
    """校验当前身份能否访问某个租户，并返回该租户

    同样以 404 掩盖存在性，理由见 `assert_instance_accessible`。
    """
    scope = get_tenant_scope(user)
    if scope is not None and scope != tenant_id:
        raise NotFoundError(resource=f"租户 '{tenant_id}'")
    tenant = await DBTenant.get_or_none(id=tenant_id)
    if tenant is None:
        raise NotFoundError(resource=f"租户 '{tenant_id}'")
    return tenant


def require_platform_owner() -> Callable[..., object]:
    """FastAPI 依赖：仅平台方可用

    用法: `_user: DBUser = Depends(require_platform_owner())`
    """

    async def dependency(current_user: DBUser = Depends(get_current_active_user)) -> DBUser:
        if not is_platform_owner(current_user):
            logger.debug(f"非平台方访问平台方专属接口: user={current_user.username}")
            raise PermissionDeniedError
        return current_user

    return dependency


def require_tenant_admin() -> Callable[..., object]:
    """FastAPI 依赖：平台方或租户管理员可用

    租户普通用户（只读）被挡在外面。通过本依赖只说明"角色够"，
    **不代表**可以访问任意租户的数据，具体的作用域仍要走
    `assert_tenant_accessible` / `assert_instance_accessible`。
    """

    async def dependency(current_user: DBUser = Depends(get_current_active_user)) -> DBUser:
        if is_platform_owner(current_user):
            return current_user
        if tenant_role_of(current_user) is TenantRole.TENANT_ADMIN and current_user.tenant_id is not None:
            return current_user
        logger.debug(f"权限不足，需要租户管理员: user={current_user.username}")
        raise PermissionDeniedError

    return dependency


async def resolve_current_identity(user: DBUser) -> CurrentIdentity:
    """组装当前登录身份，前端据此决定展示平台方 UI 还是租户 UI"""
    role = tenant_role_of(user)
    tenant_name = ""
    if user.tenant_id is not None:
        tenant = await DBTenant.get_or_none(id=user.tenant_id)
        if tenant is not None:
            tenant_name = tenant.display_name or tenant.name

    return CurrentIdentity(
        user_id=user.id,
        username=user.username,
        role=role,
        tenant_id=user.tenant_id,
        tenant_name=tenant_name,
        accessible_instance_keys=await accessible_instance_keys(user),
    )
