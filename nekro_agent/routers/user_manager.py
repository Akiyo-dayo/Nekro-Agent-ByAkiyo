"""账号管理路由

## 这里的权限判定为什么不能只看调用方

多租户改造之前，"能登录后台"与"是平台方管理员"是同一件事，因此
`if _current_user.perm_level < Role.Admin` 这一句就够了。改造之后租户管理员同样是
`Role.Admin`（`TENANT_MAX_PERM_LEVEL`），这一句于是变成一条完整的平台接管链：
租户管理员 `POST /{平台方 user_id}/reset-password` 重置平台方口令后直接登录，
或者把平台方账号 ban / delete 掉。

所以本模块每个吃 `{user_id}` 的端点都必须过 `assert_managed_user_accessible`，
它同时校验**目标账号的归属**与**目标账号的等级**。判据与边界写在那个函数的 docstring 里。

## 各端点的归属

- **平台方专属**：`POST /create`。它建出来的是 `tenant_id` 为 NULL 的账号，
  也就是平台方一侧的账号；让租户建一个自己随即看不见的东西是保证会出 bug 的设计
  （与工作区"创建提到 Role.Super"同一条理由）。租户建自己的成员走
  `POST /console/tenants/{tenant_id}/members`，那里由 `tenant_perm_level()` 钉死等级上限。
- **租户作用域**：其余 7 个端点。列表按归属过滤，单账号越权 404。
"""

from datetime import datetime
from typing import ClassVar, List, Optional, cast

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from tortoise.expressions import Q

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    AppError,
    PermissionDeniedError,
    UnauthorizedError,
)
from nekro_agent.schemas.i18n import I18nDict, i18n_text
from nekro_agent.schemas.user import UserCreate, UserUpdate
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.manager import command_manager
from nekro_agent.services.user.auth import get_hashed_password
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role, get_perm_role
from nekro_agent.services.user.super_users import matches_super_users
from nekro_agent.services.user.tenant_perm import (
    assert_managed_user_accessible,
    get_tenant_scope,
    require_platform_owner,
)
from nekro_agent.services.user.util import user_register

router = APIRouter(prefix="/user-manager", tags=["UserManager"])

logger = get_sub_logger("user.manager")


class SelfResetMustUseSelfServiceError(AppError):
    """租户身份不能用「重置他人口令」这条端点重置**自己**的口令

    ## 这条守卫补的是什么

    `PUT /user/password` 现在要求当前口令，理由是"一次令牌泄露不该被就地升级成永久接管"。
    但那条守卫只锁了正门：本端点改的是**同一个** `DBUser.password` 字段，
    而 `assert_managed_user_accessible` 的两条判据（归属 + 目标等级）对"目标就是自己"
    **恒成立** —— 同租户显然成立，`target.perm_level > user.perm_level` 在自己对自己时
    也永远为假。于是租户管理员拿着一张偷来的令牌调
    `POST /user-manager/{自己的 id}/reset-password`，不需要任何旧口令就能改掉自己的口令，
    正门那道校验一点作用都没起。

    受影响的恰好是 `Role.Admin`，也就是**租户管理员本人** —— 定制版客户的主账号。
    `Role.User` 的成员被本模块开头那句 `perm_level < Role.Admin` 挡在门外，所以
    改密校验对他们是真的生效的；换句话说改前的防线只覆盖了权限最低的那一档。

    ## 为什么是"拒绝自助"而不是"给这条也加旧口令"

    本端点的语义是**管理员重置别人的口令**，调用者按定义不知道目标的旧口令；
    加一个 `old_password` 参数会把平台方的重置能力一并废掉（`TestAdminResetIsADifferentEndpoint`
    钉的就是这一条）。而"改自己的口令"本来就有专属端点 `PUT /user/password`，
    这一轮刚刚把它的前端入口 `/profile` 打通。所以这里没有能力损失，只有出口收敛：
    改别人走这条，改自己走那条。

    ## 为什么只对租户调用方生效

    与 `assert_managed_user_accessible` 里"目标等级"那条判据的取舍完全一致：
    平台方一侧不存在需要防的边界（一张被偷的平台方令牌本来就能
    `POST /user-manager/create` 建一个新的 `Role.Super` 账号，口令持久化不增加任何风险），
    而给平台方也加上会改变既有行为 —— `pages/user-manager/components/UserTable.tsx`
    的重置按钮**每一行都渲染**，包括调用者自己那一行，那是单部署形态下的现成工作流。
    未启用多租户时 `get_tenant_scope()` 恒为 None，本守卫整条不进，行为逐字不变。

    403 而不是 404：这里不掩盖任何存在性（目标就是调用者自己，他当然知道自己存在），
    这是一条"换个端点做"的能力边界。
    """

    http_status: ClassVar[int] = 403
    i18n_message: ClassVar[I18nDict] = i18n_text(
        zh_CN="修改自己的登录口令请走「账号设置」，需要提供当前口令",
        en_US="To change your own password use the account settings page; the current password is required",
    )


class ActionResponse(BaseModel):
    ok: bool = True


class UserInfo(BaseModel):
    id: int
    username: str
    adapter_key: str
    platform_userid: str
    unique_id: str
    perm_level: int
    perm_role: str
    command_permission: str
    login_time: Optional[datetime]
    ban_until: Optional[datetime]
    prevent_trigger_until: Optional[datetime]
    is_active: bool
    is_prevent_trigger: bool
    create_time: Optional[datetime]
    update_time: Optional[datetime]
    ext_data: Optional[dict] = None


class UserListResponse(BaseModel):
    total: int
    items: List[UserInfo]
    page: int
    page_size: int


def _resolve_effective_command_permission(adapter_key: str, platform_userid: str) -> CommandPermission:
    """聊天命令里的**全局**生效档位，与 `collector._resolve_user_command_flags` 同一套判据

    - 后台账号（平台方 admin、租户成员）没有平台身份，`platform_userid` 为空，恒为普通用户；
    - 超级用户只认 `SUPER_USERS`，这里只做**名单匹配**（号码属于哪个平台）：用户行不带实例，
      判不了来源；真正执行命令时还要看消息来源是否可信（`services/user/super_users.py`）。
      存储里的 `SUPER_USER` 按频道管理员展示；
    - 按频道的授权不在这里：它只在那一个频道生效，展示在频道的用户列表里。
    """
    if not platform_userid:
        return CommandPermission.USER
    if matches_super_users(adapter_key, platform_userid):
        return CommandPermission.SUPER_USER
    permission = command_manager.get_user_permission(adapter_key=adapter_key, platform_userid=platform_userid)
    if permission in (CommandPermission.ADVANCED, CommandPermission.SUPER_USER):
        return CommandPermission.ADVANCED
    return CommandPermission.USER


@router.get("/list", summary="获取用户列表", response_model=UserListResponse)
async def list_users(
    page: int = 1,
    page_size: int = 10,
    search: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    _current_user: DBUser = Depends(get_current_active_user),
) -> UserListResponse:
    """获取用户列表；租户只看得到自己租户下的账号"""
    if _current_user.perm_level < Role.Admin:
        raise PermissionDeniedError

    scope = get_tenant_scope(_current_user)
    # total 必须跟着一起过滤，否则"一共有多少账号"连同翻页时的空白页一起泄露出去
    query = DBUser.all() if scope is None else DBUser.filter(tenant_id=scope)

    if search:
        search_filter = (
            Q(username__icontains=search)
            | Q(adapter_key__icontains=search)
            | Q(platform_userid__icontains=search)
        )
        if ":" in search:
            adapter_key, platform_userid = search.split(":", 1)
            search_filter |= Q(adapter_key__icontains=adapter_key, platform_userid__icontains=platform_userid)
        query = query.filter(search_filter)

    allowed_sort_fields = {"id", "username", "create_time", "adapter_key"}
    sort_field = sort_by if sort_by in allowed_sort_fields else "id"
    sort_direction = sort_order if sort_order in {"asc", "desc"} else "desc"
    order_by = f"-{sort_field}" if sort_direction == "desc" else sort_field
    query = query.order_by(order_by)

    total = await query.count()
    users = await query.offset((page - 1) * page_size).limit(page_size)

    user_list: List[UserInfo] = []
    for user in users:
        user_list.append(
            UserInfo(
                id=user.id,
                username=user.username,
                adapter_key=user.adapter_key,
                platform_userid=user.platform_userid,
                unique_id=user.unique_id,
                perm_level=user.perm_level,
                perm_role=get_perm_role(user.perm_level),
                command_permission=_resolve_effective_command_permission(
                    user.adapter_key,
                    user.platform_userid,
                ).value,
                login_time=user.login_time,
                ban_until=user.ban_until,
                prevent_trigger_until=user.prevent_trigger_until,
                is_active=user.is_active,
                is_prevent_trigger=user.is_prevent_trigger,
                create_time=user.create_time,
                update_time=user.update_time,
            ),
        )

    return UserListResponse(
        total=total,
        items=user_list,
        page=page,
        page_size=page_size,
    )


@router.get("/{user_id}", summary="获取用户详情", response_model=UserInfo)
async def get_user(
    user_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> UserInfo:
    """获取用户详情"""
    if _current_user.perm_level < Role.Admin:
        raise PermissionDeniedError

    user = await assert_managed_user_accessible(_current_user, user_id)

    return UserInfo(
        id=user.id,
        username=user.username,
        adapter_key=user.adapter_key,
        platform_userid=user.platform_userid,
        unique_id=user.unique_id,
        perm_level=user.perm_level,
        perm_role=get_perm_role(user.perm_level),
        command_permission=_resolve_effective_command_permission(
            user.adapter_key,
            user.platform_userid,
        ).value,
        login_time=user.login_time,
        ban_until=user.ban_until,
        prevent_trigger_until=user.prevent_trigger_until,
        is_active=user.is_active,
        is_prevent_trigger=user.is_prevent_trigger,
        ext_data=user.ext_data,
        create_time=user.create_time,
        update_time=user.update_time,
    )


@router.post("/create", summary="创建新用户", response_model=ActionResponse)
async def create_user(
    user_data: UserCreate,
    _current_user: DBUser = Depends(require_platform_owner()),
) -> ActionResponse:
    """创建新用户，仅平台方可用

    `user_register` 建出来的账号 `tenant_id` 为 NULL，也就是平台方一侧的账号；
    租户建自己的成员走 `POST /console/tenants/{tenant_id}/members`。

    `SUPER_ACCESS_KEY` 的校验保留：它是"持有部署密钥"的第二重证明，与身份判定正交。
    """
    if user_data.access_key != OsEnv.SUPER_ACCESS_KEY:
        raise UnauthorizedError

    await user_register(user_data)
    return ActionResponse(ok=True)


@router.put("/{user_id}", summary="更新用户信息", response_model=ActionResponse)
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """更新用户信息"""
    if _current_user.perm_level < Role.Admin:
        raise PermissionDeniedError

    user = await assert_managed_user_accessible(_current_user, user_id)

    user.username = user_data.username
    await user.save()

    return ActionResponse(ok=True)


class BanUserRequest(BaseModel):
    ban_until: Optional[datetime] = None


@router.post("/{user_id}/ban", summary="封禁/解封用户", response_model=ActionResponse)
async def ban_user(
    user_id: int,
    ban_data: BanUserRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """封禁/解封用户"""
    if _current_user.perm_level < Role.Admin:
        raise PermissionDeniedError

    user = await assert_managed_user_accessible(_current_user, user_id)

    user.ban_until = cast(datetime, ban_data.ban_until)
    await user.save()

    return ActionResponse(ok=True)


class PreventTriggerRequest(BaseModel):
    prevent_trigger_until: Optional[datetime] = None


@router.post("/{user_id}/prevent-trigger", summary="设置触发权限", response_model=ActionResponse)
async def prevent_trigger(
    user_id: int,
    prevent_data: PreventTriggerRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """设置触发权限"""
    if _current_user.perm_level < Role.Admin:
        raise PermissionDeniedError

    user = await assert_managed_user_accessible(_current_user, user_id)

    user.prevent_trigger_until = cast(datetime, prevent_data.prevent_trigger_until)
    await user.save()

    return ActionResponse(ok=True)


@router.post("/{user_id}/reset-password", summary="重置用户密码", response_model=ActionResponse)
async def reset_password(
    user_id: int,
    password: str = Body(..., embed=True),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """重置**他人**的登录口令（不需要旧口令）

    自助改密是另一条端点 `PUT /user/password`，它必校验当前口令。两条端点、两套判据：
    调用者重置别人时按定义不知道对方的旧口令，所以这里不能要求它；反过来，
    "改自己的"必须要求，否则 `PUT /user/password` 那道校验就是可绕过的装饰。
    收口在 `SelfResetMustUseSelfServiceError` 上，边界与代价写在那个类的 docstring 里。
    """
    if _current_user.perm_level < Role.Admin:
        raise PermissionDeniedError

    user = await assert_managed_user_accessible(_current_user, user_id)

    # 租户身份不得用这条端点改自己的口令——那会绕开 `PUT /user/password` 的当前口令校验。
    # 平台方不进这一条（get_tenant_scope() 为 None），理由见上面的类 docstring。
    if get_tenant_scope(_current_user) is not None and user.id == _current_user.id:
        logger.warning(
            f"租户身份 {_current_user.username} 试图用重置端点改自己的口令，已拒绝并引导至自助改密",
        )
        raise SelfResetMustUseSelfServiceError

    user.password = get_hashed_password(password)
    # 口令世代 +1：重置成功即吊销目标用户已签发的全部令牌（含其当前会话）。
    # 与自助改密（util.user_change_password）同一条规矩：改口令的路径改了几个，
    # 世代就要在哪几个上 +1，漏一处就是一个不吊销的后门。
    user.password_generation += 1
    await user.save()
    return ActionResponse(ok=True)


@router.delete("/{user_id}", summary="删除用户", response_model=ActionResponse)
async def delete_user(
    user_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """删除用户"""
    if _current_user.perm_level < Role.Admin:
        raise PermissionDeniedError

    user = await assert_managed_user_accessible(_current_user, user_id)

    await user.delete()
    return ActionResponse(ok=True)
