from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.presets import (
    INSTANCE_SCOPE_DESC,
    resolve_instance_scope,
    visible_presets,
)
from nekro_agent.schemas.errors import CloudServiceError, ConflictError
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import get_tenant_scope, require_platform_owner
from nekro_agent.systems.cloud.api.preset import get_preset, list_presets, list_user_presets

router = APIRouter(prefix="/cloud/presets-market", tags=["Cloud Presets Market"])


def shows_platform_account_state(user: DBUser) -> bool:
    """当前身份能否看到"平台方那个社区账号"自己的状态

    云端返回的 `isFavorited` 说的是 `NEKRO_CLOUD_API_KEY` 背后那个账号收藏没收藏，
    与调用方无关。`GET /cloud/favorites` 已经是平台方专属，这里若照原样回给租户，
    逐页翻一遍市场就能把平台方的人设收藏清单重建出来 —— 同一份数据换个端点读到，
    等于那道门槛没关上。`favoriteCount` 是云端条目的公开总数，不在此列。

    未启用多租户时 `get_tenant_scope()` 恒为 None，返回值恒为 True，行为逐字不变。
    """
    return get_tenant_scope(user) is None


class CloudPreset(BaseModel):
    model_config = ConfigDict(populate_by_name=True, by_alias=True)

    remote_id: str
    is_local: bool
    name: str
    title: str
    avatar: str
    content: str
    description: str
    tags: str
    author: str
    create_time: str
    update_time: str
    favorite_count: Optional[int] = Field(None, alias="favoriteCount")
    is_favorited: Optional[bool] = Field(None, alias="isFavorited")


class CloudPresetListResponse(BaseModel):
    total: int
    items: List[CloudPreset]
    page: int
    page_size: int
    total_pages: int


class UserPresetItem(BaseModel):
    id: str
    name: str
    title: str
    avatar: Optional[str] = None
    favorite_count: Optional[int] = Field(None, alias="favoriteCount")


class UserPresetListResponse(BaseModel):
    items: List[UserPresetItem]
    total: int


class ActionResponse(BaseModel):
    ok: bool = True


@router.get("/list", summary="获取云端人设列表")
@require_role(Role.Admin)
async def get_cloud_presets_list(
    page: int = Query(1, ge=1),
    page_size: int = Query(12, ge=1, le=100),
    keyword: Optional[str] = None,
    tag: Optional[str] = None,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> CloudPresetListResponse:
    """获取云端人设列表

    `is_local` 按当前作用域可见的本地人设判定：别的实例私有的副本不算"已下载"。

    作用域必须过 `resolve_instance_scope` 而不是直接吃 `instance_key`：改前租户传空串
    就退化成平台方全局视图，`is_local` 于是按**全部**本地人设算——顺着"这条云端人设
    本部署下过没有"能把别的租户的人设库探出来；传别人的 `instance_key` 同样照单全收。
    平台方一侧空串仍是全局视图，未启用多租户的部署行为逐字不变。
    """
    scope = await resolve_instance_scope(_current_user, instance_key)
    response = await list_presets(page=page, page_size=page_size, keyword=keyword, tag=tag)

    if not response.success:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    if not response.data or not response.data.items:
        return CloudPresetListResponse(
            total=0,
            items=[],
            page=page,
            page_size=page_size,
            total_pages=0,
        )

    remote_ids = [item.id for item in response.data.items]
    local_presets = await visible_presets(scope).filter(remote_id__in=remote_ids).values("remote_id")
    local_preset_remote_ids = {preset["remote_id"] for preset in local_presets}
    show_account_state = shows_platform_account_state(_current_user)

    result: List[CloudPreset] = []
    for item in response.data.items:
        result.append(
            CloudPreset(
                remote_id=item.id,
                is_local=item.id in local_preset_remote_ids,
                name=item.name,
                title=item.title,
                avatar=item.avatar,
                content=item.content,
                description=item.description,
                tags=item.tags,
                author=item.author,
                create_time=item.created_at or "",
                update_time=item.updated_at or "",
                favorite_count=item.favorite_count,
                is_favorited=item.isFavorited if show_account_state else None,
            ),
        )

    return CloudPresetListResponse(
        total=response.data.total,
        items=result,
        page=page,
        page_size=page_size,
        total_pages=(response.data.total + page_size - 1) // page_size,
    )


@router.post("/download/{remote_id}", summary="下载云端人设到本地")
@require_role(Role.Admin)
async def download_preset(
    remote_id: str,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """下载云端人设到本地

    归属跟随下载时所处的作用域：在实例作用域下下载即归该实例独享，在平台方全局视图下
    下载则落为全局共享人设。重复判定同样按作用域可见性来算，别的实例已下载过的私有副本
    不会挡住本作用域下载。

    这是本组唯一"租户真的有份"的端点：落地物是一条本地 `DBPreset`，它带
    `owner_instance_key` 归属列，不像插件下载那样是把任意代码 clone 进服务端。
    所以走 `resolve_instance_scope` 而不是提到平台方专属——改前租户不传
    `instance_key` 就会把人设落成 `owner_instance_key=""`（`GLOBAL_PRESET_OWNER`），
    即租户单方面往全体共享的人设库里塞内容，且此后自己都改不动（全局人设对实例只读）。

    但落地作用域一收窄，副本就同时变成了**该实例可写**的行，于是 `on_shared` 不能再
    照抄云端的 `is_owner`：`is_owner` 说的是"这把部署级云端 Key 是该发布的所有者"，
    而 `on_shared` 在本地的含义是"这一行就是我们那条云端发布的本地源"。两者混同的后果
    是一条完整的接管链——租户把平台方自己发布的人设下载进 `inst1`，本地随便改内容，
    再 `POST /presets/{id}/sync-to-cloud` 就用平台方的 Key 改写了社区里的公开发布，
    `POST /presets/{id}/unshare` 更是直接把它下架。这与 `PUT`/`DELETE
    /cloud/plugins-market/plugin/{module_name}` 收成平台方专属是同一条理由。
    因此实例作用域下的副本一律 `on_shared=False`（它是下载来的，不是我们发布的），
    平台方全局视图下仍按 `is_owner` 记，未启用多租户的部署逐字不变。
    """
    scope = await resolve_instance_scope(_current_user, instance_key)
    if await visible_presets(scope).filter(remote_id=remote_id).exists():
        raise ConflictError(resource="人设")

    response = await get_preset(remote_id)
    if not response.success or not response.data:
        raise CloudServiceError(reason=str(response.error or response.message or "未知错误"))

    preset_data = response.data
    ext_data = preset_data.ext_data if preset_data.ext_data not in [None, "", "''", '""'] else {}
    await DBPreset.create(
        remote_id=preset_data.id,
        on_shared=bool(preset_data.is_owner) and not scope,
        name=preset_data.name,
        title=preset_data.title,
        avatar=preset_data.avatar,
        content=preset_data.content,
        description=preset_data.description,
        tags=preset_data.tags,
        author=preset_data.author,
        ext_data=ext_data,
        owner_instance_key=scope,
    )

    return ActionResponse(ok=True)


@router.get("/user-presets", summary="获取用户上传的人设列表")
@require_role(Role.Admin)
async def get_user_presets(
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> UserPresetListResponse:
    """列出本部署社区账号已上传的人设

    与 `/cloud/plugins-market/user-plugins` 同理："user" 是云端那个部署级账号，
    返回的是平台方的发布清单，跟调用方是谁无关，也没有租户维度可言。

    门槛写成 `Role.Admin` + `require_platform_owner()` 而不是 `Role.Super`：
    要挡的是"租户"这条身份边界，不是"宿主级操作"那条等级边界。租户账号必定带
    `tenant_id`，`require_platform_owner()` 一律拒掉；而未启用多租户的部署里每个账号
    都是 `(tenant_id=NULL, tenant_role=platform_owner)`，原来的 `Role.Admin` 管理员
    行为逐字不变。写成 `Role.Super` 会把这些部署里的次级管理员一并挡在门外 ——
    那是与多租户无关的行为回退。契约测试的 `PLATFORM_ONLY` 同样认这种写法
    （见 `is_platform_only` 的 `has_owner_dep` 分支）。
    """
    response = await list_user_presets()

    if not response.success:
        raise CloudServiceError(reason=str(response.error or response.message or "获取用户人设列表失败"))

    if not response.data:
        return UserPresetListResponse(items=[], total=0)

    # v2 接口直接返回 avatar，无需额外获取详情
    items = [
        UserPresetItem(
            id=item.id,
            name=item.name,
            title=item.title,
            avatar=item.avatar,
            favoriteCount=item.favorite_count,
        )
        for item in response.data.items
    ]

    return UserPresetListResponse(items=items, total=response.data.total)


@router.get("/detail/{remote_id}", summary="获取人设详情")
@require_role(Role.Admin)
async def get_preset_detail(
    remote_id: str,
    instance_key: str = Query("", description=INSTANCE_SCOPE_DESC),
    _current_user: DBUser = Depends(get_current_active_user),
) -> CloudPreset:
    """获取云端人设详情

    `is_local` 与列表页同源，作用域解析理由见 `get_cloud_presets_list`。
    """
    scope = await resolve_instance_scope(_current_user, instance_key)
    response = await get_preset(remote_id)

    if not response.success or not response.data:
        raise CloudServiceError(reason=str(response.error or response.message or "获取人设详情失败"))

    preset_data = response.data

    # 检查是否已下载到当前作用域可见的本地人设
    is_local = await visible_presets(scope).filter(remote_id=remote_id).exists()

    return CloudPreset(
        remote_id=preset_data.id,
        is_local=is_local,
        name=preset_data.name,
        title=preset_data.title,
        avatar=preset_data.avatar,
        content=preset_data.content,
        description=preset_data.description,
        tags=preset_data.tags,
        author=preset_data.author,
        create_time=preset_data.created_at or "",
        update_time=preset_data.updated_at or "",
        favorite_count=preset_data.favorite_count,
        is_favorited=preset_data.isFavorited if shows_platform_account_state(_current_user) else None,
    )
