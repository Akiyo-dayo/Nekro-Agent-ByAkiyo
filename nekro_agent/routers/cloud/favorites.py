from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import CloudServiceError
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import require_platform_owner
from nekro_agent.systems.cloud.api.favorite import (
    add_favorite,
    list_favorites,
    remove_favorite,
)

logger = get_sub_logger("cloud_api")
router = APIRouter(prefix="/cloud/favorites", tags=["Cloud Favorites"])


class FavoriteResource(BaseModel):
    """收藏资源信息"""

    id: str = Field(..., description="资源ID")
    name: str = Field(..., description="资源名称")
    title: str = Field(..., description="资源标题")
    avatar: Optional[str] = Field(None, description="头像URL")
    icon: Optional[str] = Field(None, description="插件图标URL")
    author: str = Field(..., description="作者")
    description: str = Field(..., description="资源描述")
    moduleName: Optional[str] = Field(None, description="插件模块名")
    hasWebhook: Optional[bool] = Field(None, description="是否有Webhook")
    isLocal: Optional[bool] = Field(None, description="是否已获取到本地")


class FavoriteItem(BaseModel):
    """收藏列表项"""

    id: str = Field(..., description="收藏ID")
    targetType: str = Field(..., description="目标类型: plugin 或 preset")
    targetId: str = Field(..., description="目标资源ID")
    createdAt: int = Field(..., description="收藏时间戳(毫秒)")
    resource: FavoriteResource = Field(..., description="资源详细信息")


class FavoriteListData(BaseModel):
    """收藏列表数据"""

    items: List[FavoriteItem] = Field(..., description="收藏列表")
    total: int = Field(..., description="总记录数")
    page: int = Field(..., description="当前页码")
    pageSize: int = Field(..., description="每页记录数")
    totalPages: int = Field(..., description="总页数")


class FavoriteListResponse(BaseModel):
    """获取收藏列表响应"""

    success: bool = Field(True, description="是否成功")
    data: FavoriteListData = Field(..., description="响应数据")


class FavoriteCreateRequest(BaseModel):
    """添加收藏请求"""

    target_type: str = Field(..., alias="targetType", description="目标类型: plugin 或 preset")
    target_id: str = Field(..., alias="targetId", description="目标资源ID")


class ActionResponse(BaseModel):
    """操作响应"""

    ok: bool = Field(..., description="操作是否成功")


@router.get("", summary="获取收藏列表")
@require_role(Role.Admin)
async def get_favorites(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=1000, description="每页数量"),
    target_type: Optional[str] = Query(None, description="筛选类型: plugin 或 preset"),
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> FavoriteListResponse:
    """获取本部署社区账号的收藏列表

    收藏挂在**部署绑定的那一个社区账号**上（`NEKRO_CLOUD_API_KEY` 标识的账号），
    不是调用方的账号——本地根本没有"每个用户的云端收藏"这回事。因此这是平台方资产：
    租户读它等于看平台方收藏了什么，写它等于替平台方改一份共享状态。

    门槛是 `Role.Admin` + `require_platform_owner()`：要挡的是"租户"这条身份边界，
    不是"宿主级操作"那条等级边界。租户账号必定带 `tenant_id`，一律被拒；而未启用多租户
    的部署里每个账号都是 `(tenant_id=NULL, tenant_role=platform_owner)`，原有的
    `Role.Admin` 管理员行为逐字不变 —— 单写 `Role.Super` 会把这些部署里的次级管理员
    一并挡住，那是与多租户无关的行为回退。本组其余平台方专属端点同此写法。
    """
    response = await list_favorites(
        page=page,
        page_size=page_size,
        target_type=target_type,
    )
    if not response.success:
        raise CloudServiceError(reason=str(response.error or response.message or "获取收藏列表失败"))

    if not response.data:
        return FavoriteListResponse(
            data=FavoriteListData(
                items=[],
                total=0,
                page=page,
                pageSize=page_size,
                totalPages=0,
            )
        )

    items = [
        FavoriteItem(
            id=item.id,
            targetType=item.target_type,
            targetId=item.target_id,
            createdAt=item.created_at,
            resource=FavoriteResource(
                id=item.resource.id,
                name=item.resource.name,
                title=item.resource.title,
                avatar=item.resource.avatar,
                icon=getattr(item.resource, 'icon', None),
                author=item.resource.author,
                description=item.resource.description,
                moduleName=item.resource.moduleName,
                hasWebhook=getattr(item.resource, 'hasWebhook', None),
                isLocal=getattr(item.resource, 'isLocal', None),
            ),
        )
        for item in response.data.items
    ]

    return FavoriteListResponse(
        data=FavoriteListData(
            items=items,
            total=response.data.total,
            page=response.data.page,
            pageSize=response.data.page_size,
            totalPages=response.data.total_pages,
        )
    )


@router.post("", summary="添加收藏")
@require_role(Role.Admin)
async def create_favorite(
    request: FavoriteCreateRequest,
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> ActionResponse:
    """添加收藏（写的是平台方社区账号的收藏，理由见 `get_favorites`）"""
    response = await add_favorite(
        target_type=request.target_type,
        target_id=request.target_id,
    )

    if not response.success:
        raise CloudServiceError(reason=str(response.error or response.message or "添加收藏失败"))

    return ActionResponse(ok=True)


@router.delete("", summary="取消收藏")
@require_role(Role.Admin)
async def delete_favorite(
    targetType: str = Query(..., alias="targetType", description="目标类型: plugin 或 preset"),
    targetId: str = Query(..., alias="targetId", description="目标资源ID"),
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> ActionResponse:
    """取消收藏（删的是平台方社区账号的收藏，理由见 `get_favorites`）"""
    response = await remove_favorite(
        target_type=targetType,
        target_id=targetId,
    )

    if not response.success:
        raise CloudServiceError(reason=str(response.error or response.message or "取消收藏失败"))

    return ActionResponse(ok=True)
