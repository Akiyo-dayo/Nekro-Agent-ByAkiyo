import time
from typing import Dict

from fastapi import APIRouter, Depends, Query

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import CloudServiceError
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import require_platform_owner
from nekro_agent.systems.cloud.api.auth import check_official_repos_starred
from nekro_agent.systems.cloud.api.user import get_community_user_profile
from nekro_agent.systems.cloud.schemas.auth import StarCheckData
from nekro_agent.systems.cloud.schemas.user import CommunityUserData

logger = get_sub_logger("cloud_api")
router = APIRouter(prefix="/cloud/auth", tags=["Cloud Authentication"])

# 内存缓存
_star_cache: Dict[str, Dict] = {}
# 未star状态的缓存过期时间（秒）
_UNSTARRED_CACHE_EXPIRES = 5  # 5 秒
# 已star状态的缓存过期时间（秒）
_STARRED_CACHE_EXPIRES = 86400  # 24小时

# 社区用户信息缓存
_user_cache: Dict[str, Dict] = {}
_USER_CACHE_EXPIRES = 3600  # 1小时


def clear_star_status_cache() -> None:
    """清除GitHub Star状态缓存"""
    global _star_cache
    _star_cache.pop("star_status", None)


@router.get("/github-stars")
async def check_github_stars(
    force: bool = Query(False, description="是否强制检查（忽略缓存）"),
    clear_cache: bool = Query(False, description="是否清除缓存后再检查"),
    _current_user: DBUser = Depends(get_current_active_user),
) -> StarCheckData:
    """检查本部署是否已Star官方GitHub仓库

    有意保持"任何已登录身份可读"：返回体只有两个公开仓库名列表和一个布尔值，
    既没有账号标识也没有租户维度，而它驱动的是一个**整部署共用**的前端门槛
    （`stores/githubStar.ts` → 主题/壁纸可用性）。提到平台方专属会让所有非
    `Role.Super` 账号在任何部署下都被这道门槛永久挡住，那是与租户无关的行为回退。
    与 `cloud/announcement` 同档，见契约登记表的 TENANT_AGNOSTIC。
    """

    if clear_cache:
        clear_star_status_cache()

    # 检查缓存
    if not force and "star_status" in _star_cache:
        cached_data = _star_cache["star_status"]
        current_time = time.time()

        # 检查缓存是否过期
        if current_time < cached_data.get("expires_at", 0):
            return StarCheckData.model_validate(cached_data["data"])

    result = await check_official_repos_starred()

    if not result.success:
        raise CloudServiceError(reason=str(result.message or result.error or "未知错误"))
    if not result.data:
        raise CloudServiceError(reason="检查GitHub Stars状态失败")

    current_time = time.time()
    all_starred = result.data.all_starred
    expires_at = current_time + (_STARRED_CACHE_EXPIRES if all_starred else _UNSTARRED_CACHE_EXPIRES)

    _star_cache["star_status"] = {
        "data": result.data.model_dump(by_alias=True),
        "expires_at": expires_at,
        "timestamp": current_time,
    }

    logger.debug(f"已缓存 GitHub Star状态 {all_starred}，过期时间: {int(expires_at - current_time)} 秒")

    return result.data


@router.get("/community-user")
@require_role(Role.Admin)
async def get_community_user(
    force: bool = Query(False, description="是否强制刷新（忽略缓存）"),
    _current_user: DBUser = Depends(get_current_active_user),
    _platform: DBUser = Depends(require_platform_owner()),
) -> CommunityUserData:
    """获取本部署绑定的社区账号信息

    返回的是 `NEKRO_CLOUD_API_KEY` 背后那个账号的 `githubId` / `username` /
    **`email`** / 头像——运维方本人的身份信息，与调用方是谁无关。改前任何已登录账号
    （含租户成员、`Role.User`）都能读到这段 PII，因此提到平台方专属。

    门槛写成 `Role.Admin` + `require_platform_owner()`，理由同
    `cloud/favorites.py:get_favorites`：挡的是租户这条身份边界，
    未启用多租户的部署里的管理员不受影响。
    """

    if not force and "user_profile" in _user_cache:
        cached_data = _user_cache["user_profile"]
        current_time = time.time()
        if current_time < cached_data.get("expires_at", 0):
            return CommunityUserData.model_validate(cached_data["data"])

    result = await get_community_user_profile()

    if not result.success:
        raise CloudServiceError(reason=str(result.message or result.error or "未知错误"))
    if not result.data:
        raise CloudServiceError(reason="获取社区用户信息失败")

    current_time = time.time()
    _user_cache["user_profile"] = {
        "data": result.data.model_dump(by_alias=True),
        "expires_at": current_time + _USER_CACHE_EXPIRES,
        "timestamp": current_time,
    }

    return result.data
