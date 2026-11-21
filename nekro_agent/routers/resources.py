"""工作区资源路由

## 资源就是平台方的基础设施凭据

`DBWorkspaceResource` 里装的是 SSH 私钥、root 口令、数据库密码、访问令牌
（字段清单见 `services/resources/registry.py`，`secret=True` 的那些），
`secret_payload_encrypted` 解出来会作为环境变量注入绑定了它的工作区容器
（`resolve_workspace_resources_to_env`）。表上**没有归属列**。

因此这里的边界只有一条画法：

- **平台方专属（`Role.Super`）**：全部 `/resources*`，以及"把资源绑上某个工作区"。
  绑定 = 把平台方的生产 SSH 私钥发给那个工作区里的 Claude Code，
  让租户自助完成这一步等于把平台直接交出去。改前这些全是 `Role.Admin`，
  而租户管理员就是 `Role.Admin` —— `GET /resources/{id}` 一个请求即可读走全部凭据明文。
- **租户作用域**：只剩"看自己工作区上绑了什么"、"解绑"、"重排"三条，
  过 `assert_workspace_accessible`，越权 404。

## 角色门槛之外还有一层：凭据不再出响应体

上面那条 `Role.Super` 只挡住"谁能发这个请求"。真正把私钥搬出服务端的是响应体本身，
所以 `secret=True` 字段的取值现在一律换成 `CREDENTIAL_MASK`
（收在 `service._build_detail`，详情/创建/更新三条出口共用），写回时该哨兵表示"保持不变"。
两层各自独立：降错一次角色不会立刻等于泄一次私钥。理由与代价见 `services/resources/service.py`
的模块 docstring。

**残留面**：`WorkspaceResourceSummary.bound_workspaces` / `bound_workspace_count` /
`orphan_binding_count` 描述的是**其他**工作区（含别的租户的），所以租户视角下这三个字段
被清空，只保留"这条资源绑在我这儿"这一个事实。

## 悬空绑定

`binding-health` / `binding-purge` 两条端点是"看得见"那一半。它们读整张
`workspace_resource_binding`，天然是跨工作区、跨租户的，所以与其余 `/resources*`
一样只对平台方开放；租户侧不需要对应端点，因为删工作区时的级联
（`routers/workspaces.py` 的删除路径）已经保证租户自己那一侧不会再产生悬空行。
"""

from fastapi import APIRouter, Depends

from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_workspace import DBWorkspace
from nekro_agent.schemas.errors import NotFoundError, ValidationError
from nekro_agent.schemas.workspace_resource import (
    ActionOkResponse,
    WorkspaceResourceBindBody,
    WorkspaceResourceBindingHealthResponse,
    WorkspaceResourceBindingPurgeResponse,
    WorkspaceResourceBindingsResponse,
    WorkspaceResourceCheckBindBody,
    WorkspaceResourceCheckBindResponse,
    WorkspaceResourceCreate,
    WorkspaceResourceDetailResponse,
    WorkspaceResourceListResponse,
    WorkspaceResourceReorderBody,
    WorkspaceResourceSecretHealthResponse,
    WorkspaceResourceSecretPurgeResponse,
    WorkspaceResourceSecretRecoverBody,
    WorkspaceResourceSecretRecoverResponse,
    WorkspaceResourceTemplatesResponse,
    WorkspaceResourceUpdate,
)
from nekro_agent.services.resources import get_resource_templates, workspace_resource_service
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import get_tenant_scope
from nekro_agent.services.workspace.manager import WorkspaceService
from nekro_agent.services.workspace.tenant_scope import assert_workspace_accessible

router = APIRouter(tags=["Workspace Resources"])


async def _refresh_workspace_claude_md(workspace_id: int) -> None:
    workspace = await DBWorkspace.get_or_none(id=workspace_id)
    if workspace is None:
        raise NotFoundError(resource=f"工作区 {workspace_id}")
    WorkspaceService.update_claude_md(workspace)


@router.get("/resources/templates", summary="获取工作区资源预置模板", response_model=WorkspaceResourceTemplatesResponse)
@require_role(Role.Super)
async def list_resource_templates(
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceTemplatesResponse:
    return WorkspaceResourceTemplatesResponse(items=get_resource_templates())


@router.get("/resources", summary="获取全局工作区资源列表", response_model=WorkspaceResourceListResponse)
@require_role(Role.Super)
async def list_resources(
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceListResponse:
    return WorkspaceResourceListResponse(items=await workspace_resource_service.list_resources())


@router.get(
    "/resources/secret-health",
    summary="检查工作区资源敏感字段可用性",
    response_model=WorkspaceResourceSecretHealthResponse,
)
@require_role(Role.Super)
async def check_resource_secret_health(
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceSecretHealthResponse:
    return await workspace_resource_service.check_secret_payload_health()


@router.post(
    "/resources/secret-recover",
    summary="使用旧密钥恢复工作区资源敏感字段",
    response_model=WorkspaceResourceSecretRecoverResponse,
)
@require_role(Role.Super)
async def recover_resource_secrets(
    body: WorkspaceResourceSecretRecoverBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceSecretRecoverResponse:
    return await workspace_resource_service.recover_legacy_secret_payloads(body.legacy_secret)


@router.post(
    "/resources/secret-purge",
    summary="抹除无法解密的工作区资源敏感字段",
    response_model=WorkspaceResourceSecretPurgeResponse,
)
@require_role(Role.Super)
async def purge_unreadable_resource_secrets(
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceSecretPurgeResponse:
    purged_count = await workspace_resource_service.purge_unreadable_secret_payloads()
    return WorkspaceResourceSecretPurgeResponse(ok=True, purged_count=purged_count)


@router.get(
    "/resources/binding-health",
    summary="检查工作区资源绑定的悬空情况",
    response_model=WorkspaceResourceBindingHealthResponse,
)
@require_role(Role.Super)
async def check_resource_binding_health(
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceBindingHealthResponse:
    """列出指向已不存在工作区（或已不存在资源）的绑定

    这两条端点是资源绑定悬空的"看得见"那一半：改前这些行在后台上没有任何入口，
    上一轮只能直连数据库收拾。它们描述的是**跨工作区的全局状态**
    （包括别的租户的工作区），所以与 `/resources*` 其余端点一样是平台方专属。
    """
    return await workspace_resource_service.check_binding_health()


@router.post(
    "/resources/binding-purge",
    summary="清理悬空的工作区资源绑定",
    response_model=WorkspaceResourceBindingPurgeResponse,
)
@require_role(Role.Super)
async def purge_orphan_resource_bindings(
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceBindingPurgeResponse:
    purged_count = await workspace_resource_service.purge_orphan_bindings()
    return WorkspaceResourceBindingPurgeResponse(ok=True, purged_count=purged_count)


@router.post("/resources", summary="创建工作区资源", response_model=WorkspaceResourceDetailResponse)
@require_role(Role.Super)
async def create_resource(
    body: WorkspaceResourceCreate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceDetailResponse:
    return WorkspaceResourceDetailResponse(item=await workspace_resource_service.create_resource(body))


@router.get("/resources/{resource_id}", summary="获取工作区资源详情", response_model=WorkspaceResourceDetailResponse)
@require_role(Role.Super)
async def get_resource(
    resource_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceDetailResponse:
    return WorkspaceResourceDetailResponse(item=await workspace_resource_service.get_resource_detail(resource_id))


@router.patch("/resources/{resource_id}", summary="更新工作区资源", response_model=WorkspaceResourceDetailResponse)
@require_role(Role.Super)
async def update_resource(
    resource_id: int,
    body: WorkspaceResourceUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceDetailResponse:
    detail = await workspace_resource_service.update_resource(resource_id, body)
    return WorkspaceResourceDetailResponse(item=detail)


@router.delete("/resources/{resource_id}", summary="删除工作区资源", response_model=ActionOkResponse)
@require_role(Role.Super)
async def delete_resource(
    resource_id: int,
    remove_bindings: bool = False,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    await workspace_resource_service.delete_resource(resource_id, remove_bindings=remove_bindings)
    return ActionOkResponse()


@router.get(
    "/workspaces/{workspace_id}/resources",
    summary="获取工作区已绑定资源",
    response_model=WorkspaceResourceBindingsResponse,
)
@require_role(Role.Admin)
async def list_workspace_resources(
    workspace_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceBindingsResponse:
    await assert_workspace_accessible(_current_user, workspace_id)
    items = await workspace_resource_service.list_workspace_bindings(workspace_id)
    if get_tenant_scope(_current_user) is not None:
        # 这三个字段描述的是别的工作区（含别的租户的），租户视角下不该看到。
        # `orphan_binding_count` 同性质：它数的是这条资源挂在别处的悬空绑定，
        # 泄给租户等于告诉他"平台方另有几条绑定挂在别的工作区上"。
        for item in items:
            item.resource.bound_workspaces = []
            item.resource.bound_workspace_count = 0
            item.resource.orphan_binding_count = 0
    return WorkspaceResourceBindingsResponse(items=items)


@router.post(
    "/workspaces/{workspace_id}/resources/check-bind",
    summary="检查资源绑定冲突",
    response_model=WorkspaceResourceCheckBindResponse,
)
@require_role(Role.Super)
async def check_workspace_resource_bind(
    workspace_id: int,
    body: WorkspaceResourceCheckBindBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> WorkspaceResourceCheckBindResponse:
    """检查绑定冲突；工作区不能拿平台方凭据时与绑定接口回同一个拒绝

    在这里也拦一次，是因为前端先调 check-bind 再调绑定：只在绑定时拒的话，界面上这一步
    显示「可以绑定」，点下去才失败，看起来像是绑定接口坏了。
    """
    workspace = await DBWorkspace.get_or_none(id=workspace_id)
    if workspace is None:
        raise NotFoundError(resource=f"工作区 {workspace_id}")
    from nekro_agent.services.workspace.credential_policy import platform_credentials_allowed

    if not await platform_credentials_allowed(workspace):
        raise ValidationError(reason="租户工作区不能绑定平台方资源")
    conflicts = await workspace_resource_service.check_bind_conflicts(workspace_id, body.resource_id)
    return WorkspaceResourceCheckBindResponse(ok=not conflicts, conflicts=conflicts)


@router.post(
    "/workspaces/{workspace_id}/resources/{resource_id}", summary="绑定资源到工作区", response_model=ActionOkResponse
)
@require_role(Role.Super)
async def bind_workspace_resource(
    workspace_id: int,
    resource_id: int,
    body: WorkspaceResourceBindBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    await workspace_resource_service.bind_resource(workspace_id, resource_id, note=body.note)
    await _refresh_workspace_claude_md(workspace_id)
    return ActionOkResponse()


@router.delete(
    "/workspaces/{workspace_id}/resources/{resource_id}", summary="解绑工作区资源", response_model=ActionOkResponse
)
@require_role(Role.Admin)
async def unbind_workspace_resource(
    workspace_id: int,
    resource_id: int,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    await assert_workspace_accessible(_current_user, workspace_id)
    await workspace_resource_service.unbind_resource(workspace_id, resource_id)
    await _refresh_workspace_claude_md(workspace_id)
    return ActionOkResponse()


@router.put("/workspaces/{workspace_id}/resources/reorder", summary="重排工作区资源", response_model=ActionOkResponse)
@require_role(Role.Admin)
async def reorder_workspace_resources(
    workspace_id: int,
    body: WorkspaceResourceReorderBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    await assert_workspace_accessible(_current_user, workspace_id)
    ordered_binding_ids = [item.binding_id for item in sorted(body.items, key=lambda item: item.sort_order)]
    await workspace_resource_service.reorder_bindings(workspace_id, ordered_binding_ids)
    await _refresh_workspace_claude_md(workspace_id)
    return ActionOkResponse()
