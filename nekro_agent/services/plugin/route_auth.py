"""插件自带路由的身份、角色、实例与生效状态校验

## 为什么需要

插件路由挂在 `/plugins/<插件键>/` 下，挂载时只判"进程里这个插件还活着"，没有身份、
租户或实例校验。第三方插件可以自愿使用本零件；内置插件由静态用例兜底。

## 四步判据

1. **身份**：`Authorization: Bearer` 走后台同一条 `get_current_user` 路径（含口令世代
   比对）；拿不到请求头的长连接（SSE、`<img>`）改用 120 秒票据。**不认 `?token=`**：
   长期令牌进 URL 会落进日志与浏览器历史。
2. **角色**：低于 `min_role` 直接 403，错误与 `require_role` 同一档。
3. **实例**：租户必须带 `instance_key`，越权与不存在都由 `assert_instance_accessible`
   抛同一个 404；平台方不带实例时只有 `allow_global_view=True` 的端点才放行。
4. **插件在这个实例上生效**：不生效时抛与"实例不存在"逐字相同的 404。租户因此分不出
   "实例没有"与"插件在这个实例上关着"。

`instance_key` 只在同一适配器内唯一，所以查询参数同时收 `adapter_key`，并原样交给
`assert_instance_accessible` 成对判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import HTTPException, Request
from starlette import status
from starlette.requests import HTTPConnection

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, UnauthorizedError, ValidationError
from nekro_agent.services.plugin.route_ticket import verify_plugin_route_ticket
from nekro_agent.services.plugin.scope import is_plugin_active
from nekro_agent.services.user.deps import get_current_user
from nekro_agent.services.user.role import Role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, get_tenant_scope

logger = get_sub_logger("plugin_route_auth")


@dataclass(frozen=True)
class PluginRouteScope:
    """一次插件路由请求解析出的作用域

    Attributes:
        user: 已通过身份与角色校验的用户行
        adapter_key: 实例对里的适配器标识；平台方的全局视图为空串
        instance_key: 实例标识；平台方的全局视图为空串
        is_platform: 用户本身是平台方（`tenant_id` 为空）
    """

    user: DBUser
    adapter_key: str
    instance_key: str
    is_platform: bool


def _token_only_connection(request: Request) -> HTTPConnection:
    """给 `get_current_user` 一个**去掉查询串**的连接

    `get_current_user` 会优先读 `?token=`，那是给几个老 SSE 端点留的兼容路径。
    插件路由明确不认 URL 里的长期令牌，所以只把请求头交给它；查询参数由本模块自己读。
    """
    scope = dict(request.scope)
    scope["query_string"] = b""
    return HTTPConnection(scope)


def _has_bearer_token(request: Request) -> bool:
    auth_header = request.headers.get("Authorization")
    return bool(auth_header and auth_header.startswith("Bearer "))


async def _resolve_user(
    request: Request,
    plugin_key: str,
    adapter_key: str,
    instance_key: str,
    *,
    allow_ticket: bool,
) -> DBUser:
    """按请求头或短时效票据解析用户，并做与 `get_current_active_user` 相同的活跃检查"""
    if _has_bearer_token(request):
        user = await get_current_user(_token_only_connection(request))
        if not user.is_active:
            # 与 `get_current_active_user` 同一个错误：封禁 / 不活跃是 403，不是"没登录"
            raise PermissionDeniedError
        return user

    ticket = request.query_params.get("ticket") if allow_ticket else None
    if not ticket:
        # 注意这里**不读** `request.query_params["token"]`：长期令牌不许走 URL
        raise UnauthorizedError

    user_id = await verify_plugin_route_ticket(ticket, plugin_key, adapter_key, instance_key)
    if user_id is None:
        # 票据格式 / 签名 / 过期 / 绑定不符 / 用户已失效，一律按"没登录"处理
        raise UnauthorizedError
    user = await DBUser.get_or_none(id=user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError
    return user


async def assert_plugin_route_access(
    user: DBUser,
    plugin_key: str,
    adapter_key: str,
    instance_key: str,
    *,
    allow_global_view: bool,
) -> tuple[str, str]:
    """第 3、4 步：实例可访问且插件在这个实例上生效

    Returns:
        规整后的 `(adapter_key, instance_key)`；全局视图为两个空串。

    Raises:
        ValidationError: 租户没带实例，或平台方在不允许全局视图的端点没带实例
        NotFoundError: 实例不存在 / 不是他的，或插件在这个实例上关着（两者同文）
    """
    adapter_key = adapter_key or ""
    instance_key = instance_key or ""
    is_platform = get_tenant_scope(user) is None

    if instance_key:
        # 越权与不存在都是它抛的那个 404，原样往外抛，调用方不做二次文案。
        # `adapter_key` 用关键字传：`instance_key` 只在同一适配器内唯一，路由契约用例
        # 要求"手上有适配器就必须显式交给归属守卫"，位置参数看不出交没交。
        await assert_instance_accessible(user, instance_key, adapter_key=adapter_key)
    elif not is_platform or not allow_global_view or adapter_key:
        raise ValidationError(reason="需要指定实例")

    if not is_plugin_active(plugin_key, instance_key):
        # 与"实例不存在"逐字相同：租户不该从状态码或文案里区分这两种情况
        if instance_key:
            raise NotFoundError(resource=f"实例 '{instance_key}'")
        raise NotFoundError(resource="插件")

    return adapter_key, instance_key


async def resolve_plugin_route_scope(
    request: Request,
    plugin_key: str,
    *,
    min_role: Role = Role.Admin,
    allow_ticket: bool = False,
    allow_global_view: bool = False,
) -> PluginRouteScope:
    """完整的四步校验，供依赖函数与需要显式调用的处理函数共用

    处理函数自己第一行鉴权的场景（例如白板事件流要先判只读令牌）直接调它，避免把
    依赖逻辑抄第二份。
    """
    adapter_key = request.query_params.get("adapter_key") or ""
    instance_key = request.query_params.get("instance_key") or ""

    user = await _resolve_user(
        request,
        plugin_key,
        adapter_key,
        instance_key,
        allow_ticket=allow_ticket,
    )
    if user.perm_level < min_role:
        # 与 `require_role` 同一个错误码与文案
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足")

    normalized_adapter_key, normalized_instance_key = await assert_plugin_route_access(
        user,
        plugin_key,
        adapter_key,
        instance_key,
        allow_global_view=allow_global_view,
    )
    return PluginRouteScope(
        user=user,
        adapter_key=normalized_adapter_key,
        instance_key=normalized_instance_key,
        is_platform=get_tenant_scope(user) is None,
    )


def plugin_route_scope(
    plugin_key: str,
    *,
    min_role: Role = Role.Admin,
    allow_ticket: bool = False,
    allow_global_view: bool = False,
) -> Callable[..., Awaitable[PluginRouteScope]]:
    """构造一个 FastAPI 依赖，产出 :class:`PluginRouteScope`

    插件在定义路由时显式写 `Depends(plugin_route_scope(plugin.key))`，不改
    `mount_plugin_router` 的挂载方式。依赖函数本身带
    `__plugin_route_scope__ = True`，静态守卫靠它识别"这条路由挂了鉴权"。
    """

    async def dependency(request: Request) -> PluginRouteScope:
        return await resolve_plugin_route_scope(
            request,
            plugin_key,
            min_role=min_role,
            allow_ticket=allow_ticket,
            allow_global_view=allow_global_view,
        )

    dependency.__plugin_route_scope__ = True  # type: ignore[attr-defined]
    return dependency
