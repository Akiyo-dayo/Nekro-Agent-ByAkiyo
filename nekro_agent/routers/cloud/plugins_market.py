from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    CloudServiceError,
    ConflictError,
    NotFoundError,
    OperationFailedError,
    PermissionDeniedError,
)
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import require_platform_owner
from nekro_agent.systems.cloud.api.plugin import (
    create_plugin,
    delete_plugin,
    get_plugin,
    get_plugin_repo_info,
    list_plugins,
    list_user_plugins,
)
from nekro_agent.systems.cloud.api.plugin import update_plugin as cloud_update_plugin
from nekro_agent.systems.cloud.schemas.plugin import PluginCreate, PluginUpdate, RepoData
from nekro_agent.tools.common_util import compare_semver, get_app_version
from nekro_agent.tools.image_utils import process_image_data_url

logger = get_sub_logger("cloud_api")
router = APIRouter(prefix="/cloud/plugins-market", tags=["Cloud Plugins Market"])


class CloudPlugin(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    moduleName: str
    description: str
    author: str
    hasWebhook: bool
    homepageUrl: Optional[str] = None
    githubUrl: Optional[str] = None
    cloneUrl: Optional[str] = None
    licenseType: Optional[str] = None
    created_at: Optional[str] = Field(None, alias="createdAt")
    updated_at: Optional[str] = Field(None, alias="updatedAt")
    is_local: bool
    version: Optional[str] = None
    can_update: Optional[bool] = None
    icon: Optional[str] = None
    isOwner: Optional[bool] = None
    favoriteCount: Optional[int] = None  # 收藏数量
    isFavorited: Optional[bool] = None  # 是否已收藏
    minNaVersion: Optional[str] = None
    maxNaVersion: Optional[str] = None


class CloudPluginListResponse(BaseModel):
    total: int
    items: List[CloudPlugin]
    page: int
    page_size: int
    total_pages: int


class UserPlugin(BaseModel):
    id: str
    name: str
    moduleName: str
    icon: Optional[str] = None


class ActionResponse(BaseModel):
    ok: bool = True


class CreateResponse(BaseModel):
    id: str


@router.get("/list", summary="获取云端插件列表")
@require_role(Role.Admin)
async def get_cloud_plugins_list(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    keyword: Optional[str] = None,
    has_webhook: Optional[bool] = None,
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> CloudPluginListResponse:
    """获取云端插件列表

    整个插件市场是平台方专属的一块：它的每一个动作面
    （`download` / `update` / `create` / `plugin/{module_name}` 的改与删）要么把任意
    代码 git-clone 进服务端（那是宿主级动作，仍要 `Role.Super`），要么以平台方的社区
    身份改动公开发布内容。浏览面留给租户没有任何可达的下一步，却让它能枚举平台方账号
    在每个条目上的 `isOwner` / `isFavorited` 状态，并用平台方的云端 Key 打上游。
    因此浏览面与动作面同档。

    门槛写成 `Role.Admin` + `require_platform_owner()`，理由见
    `cloud/favorites.py:get_favorites`：挡的是租户这条身份边界，未启用多租户的部署里
    的次级管理员行为逐字不变。本文件里 `download` / `update` / `create` 保持
    `Role.Super` 不变 —— 它们写的是服务端可信代码，那才是宿主级等级边界。
    """
    response = await list_plugins(page=page, page_size=page_size, keyword=keyword, has_webhook=has_webhook)

    if not response.success:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    if not response.data or not response.data.items:
        return CloudPluginListResponse(
            total=0,
            items=[],
            page=page,
            page_size=page_size,
            total_pages=0,
        )

    local_plugins = []
    local_plugin_ids = plugin_collector.package_data.get_remote_ids()

    result: List[CloudPlugin] = []
    for item in response.data.items:
        is_local = item.id in local_plugin_ids

        version = None
        can_update = False
        if is_local:
            for plugin in local_plugins:
                if getattr(plugin, "remote_id", None) == item.id:
                    version = getattr(plugin, "version", None)
                    can_update = False
                    break

        result.append(
            CloudPlugin(
                id=item.id,
                name=item.name,
                moduleName=item.moduleName,
                description=item.description,
                author=item.author,
                hasWebhook=item.hasWebhook,
                homepageUrl=item.homepageUrl,
                githubUrl=item.githubUrl,
                cloneUrl=item.cloneUrl,
                licenseType=item.licenseType,
                created_at=item.createdAt,
                updated_at=item.updatedAt,
                is_local=is_local,
                version=version,
                can_update=can_update,
                icon=item.icon,
                isOwner=getattr(item, "isOwner", False),
                favoriteCount=getattr(item, "favoriteCount", None),
                isFavorited=getattr(item, "isFavorited", None),
                minNaVersion=getattr(item, "minNaVersion", None),
                maxNaVersion=getattr(item, "maxNaVersion", None),
            ),
        )

    return CloudPluginListResponse(
        total=response.data.total,
        items=result,
        page=page,
        page_size=page_size,
        total_pages=(response.data.total + page_size - 1) // page_size,
    )


@router.get("/user-plugins", summary="获取用户已上传的插件")
@require_role(Role.Admin)
async def get_user_plugins(
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> List[UserPlugin]:
    """列出本部署社区账号已发布的插件

    "user" 指的是云端那个部署级账号，不是调用方；返回的是平台方的发布清单。
    """
    response = await list_user_plugins()

    if not response.success:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    if not response.data or not response.data.items:
        return []

    # v2 接口直接返回 icon
    return [UserPlugin(**item.model_dump()) for item in response.data.items]


@router.get("/detail/{module_name}", summary="获取插件详情")
@require_role(Role.Admin)
async def get_plugin_detail(
    module_name: str,
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> CloudPlugin:
    """获取插件详情（与列表同档，理由见 `get_cloud_plugins_list`）"""
    response = await get_plugin(module_name)

    if not response.success or not response.data:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    plugin_data = response.data

    return CloudPlugin(
        id=plugin_data.id,
        name=plugin_data.name,
        moduleName=plugin_data.moduleName,
        description=plugin_data.description,
        author=plugin_data.author,
        hasWebhook=plugin_data.hasWebhook,
        homepageUrl=plugin_data.homepageUrl,
        githubUrl=plugin_data.githubUrl,
        cloneUrl=plugin_data.cloneUrl,
        licenseType=plugin_data.licenseType,
        created_at=plugin_data.createdAt,
        updated_at=plugin_data.updatedAt,
        is_local=False,
        version=None,
        can_update=False,
        icon=plugin_data.icon,
        isOwner=plugin_data.isOwner,
        favoriteCount=plugin_data.favoriteCount,
        isFavorited=plugin_data.isFavorited,
        minNaVersion=plugin_data.minNaVersion,
        maxNaVersion=plugin_data.maxNaVersion,
    )


@router.get("/repo/{module_name}", summary="获取插件仓库信息")
@require_role(Role.Admin)
async def get_plugin_repo(
    module_name: str,
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> RepoData:
    """获取插件仓库详细信息（`download` 要 clone 的那个仓库，与它同档）"""
    response = await get_plugin_repo_info(module_name)

    if not response.success or not response.data:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    return response.data


@router.post("/download/{module_name}", summary="下载云端插件到本地")
@require_role(Role.Super)
async def download_plugin(
    module_name: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """下载云端插件到本地"""
    existing_package = plugin_collector.package_data.get_package_by_module(module_name)
    if existing_package:
        raise ConflictError(resource="插件")

    response = await get_plugin(module_name)
    if not response.success or not response.data:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    plugin_data = response.data

    if not plugin_data.cloneUrl:
        raise NotFoundError(resource="插件仓库")

    # 版本兼容性校验
    current_version = get_app_version()
    if current_version != "unknown":
        if plugin_data.minNaVersion and compare_semver(current_version, plugin_data.minNaVersion) < 0:
            raise OperationFailedError(
                operation=f"下载插件（当前 NA 版本 {current_version} 低于最低要求 {plugin_data.minNaVersion}）"
            )
        if plugin_data.maxNaVersion and compare_semver(current_version, plugin_data.maxNaVersion) >= 0:
            raise OperationFailedError(
                operation=f"下载插件（当前 NA 版本 {current_version} 不低于最高限制 {plugin_data.maxNaVersion}）"
            )

    await plugin_collector.clone_package(
        module_name=plugin_data.moduleName,
        git_url=plugin_data.cloneUrl,
        remote_id=plugin_data.id,
        auto_load=True,
    )

    await plugin_collector.reload_plugin_by_module_name(plugin_data.moduleName, is_package=True)

    return ActionResponse(ok=True)


@router.post("/update/{module_name}", summary="更新本地插件")
@require_role(Role.Super)
async def update_plugin(
    module_name: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """更新本地插件"""
    existing_package = plugin_collector.package_data.get_package_by_module(module_name)
    if not existing_package:
        raise NotFoundError(resource="插件")

    response = await get_plugin(module_name)
    if not response.success or not response.data:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    logger.info(f"准备更新插件: {module_name}")

    await plugin_collector.update_package(
        module_name=response.data.moduleName,
        auto_reload=True,
    )

    return ActionResponse(ok=True)


@router.post("/create", summary="创建插件")
@require_role(Role.Super)
async def create_cloud_plugin(
    plugin_data: PluginCreate,
    _current_user: DBUser = Depends(get_current_active_user),
) -> CreateResponse:
    """创建云端插件"""
    if plugin_data.icon and plugin_data.icon.startswith("data:image/"):
        plugin_data.icon = await process_image_data_url(plugin_data.icon)

    response = await create_plugin(plugin_data)

    if not response.success:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    if not response.data or "id" not in response.data:
        raise OperationFailedError(operation="创建插件")

    return CreateResponse(id=str(response.data.get("id")))


@router.delete("/plugin/{module_name}", summary="下架云端插件")
@require_role(Role.Admin)
async def delete_cloud_plugin(
    module_name: str,
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> ActionResponse:
    """下架云端插件

    以平台方的社区身份把一个公开发布下架，与 `POST /create` 是同一条发布链的两端。
    """
    response = await delete_plugin(module_name)

    if not response.success:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    return ActionResponse(ok=True)


@router.put("/plugin/{module_name}", summary="更新插件信息")
@require_role(Role.Admin)
async def update_user_plugin(
    module_name: str,
    plugin_data: PluginUpdate,
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> ActionResponse:
    """更新平台方社区身份下已发布的插件信息

    下面那句 `isOwner` 判定是**云端**的归属校验（这条 Key 是不是发布者），
    与本地的租户归属无关，替代不了角色门槛：改前任何租户管理员都能改动平台方
    发布在社区里的公开内容（含 `cloneUrl`，那是别人 `download` 时会 clone 的地址）。
    """
    current_plugin = await get_plugin(module_name)
    if not current_plugin.success:
        raise CloudServiceError(reason=str(current_plugin.error or current_plugin.message or "未知错误"))

    if not current_plugin.data or not current_plugin.data.isOwner:
        raise PermissionDeniedError()

    if plugin_data.icon and plugin_data.icon.startswith("data:image/"):
        plugin_data.icon = await process_image_data_url(plugin_data.icon)

    update_data = plugin_data.model_dump(exclude_unset=True, exclude_none=True)
    response = await cloud_update_plugin(module_name, update_data)

    if not response.success:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    return ActionResponse(ok=True)
