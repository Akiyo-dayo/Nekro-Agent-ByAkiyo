"""通用静态资源路由

## 归属

- **平台方专属（`Role.Super`）**：壁纸的上传与删除。壁纸目录是整个部署共用的一份，
  任何一个客户改它，全部客户的后台背景都跟着变；上传还是一条往宿主磁盘写文件的路径。
  改前这两条**一个角色校验都没有**，只挂 `get_current_active_user`，
  连只读的租户普通用户都能删掉别人上传的壁纸。
- **任意已登录身份**：`GET /wallpaper/list`。
- **租户作用域**：`GET /uploads-access-token`（签发聊天上传目录的取件令牌）。
- **有意不做用户身份鉴权**：`GET /wallpaper/{filename}` 与 `GET /uploads/{chat_key}/{filename}`。

## 两条不带 `Authorization` 的静态取文件接口

它们要被 `<img src>` 直接加载，拿不到请求头里的登录态。

`GET /uploads/{chat_key}/{filename}` 因此改成**签名 URL**：请求必须带
`?sig=`，由 `tools/upload_access.py` 用 `JWT_SECRET_KEY` 派生密钥签出、覆盖
"某个频道的上传目录 + 过期时间"。令牌只能从 `GET /uploads-access-token` 拿到，
而那条接口走 `assert_channel_accessible`，于是"知道 chat_key 与文件名就能取到
任意租户的聊天文件"这条路被切断——`chat_key` 可枚举不再等于文件可取。
签名不通过一律 404（与越权同形，不泄露"这个频道/文件存在"）。

`GET /wallpaper/{filename}` 保持公开：壁纸目录是整个部署共用的一份、内容由平台方上传，
文件名就是内容的 md5（既是标识也是访问凭据），没有租户维度可收敛。
"""

from hashlib import md5
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import USER_UPLOAD_DIR, WALLPAPER_DIR
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.chat_channel import assert_channel_accessible
from nekro_agent.schemas.errors import AppFileNotFoundError, InvalidFileTypeError
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.tools.path_convertor import assert_path_within, sanitize_chat_key_for_path
from nekro_agent.tools.upload_access import (
    TOKEN_QUERY_PARAM,
    issue_upload_access_token,
    token_expiry_seconds,
    verify_upload_access_token,
)

logger = get_sub_logger("common_api")
router = APIRouter(prefix="/common", tags=["Common"])

# 确保壁纸目录存在
WALLPAPER_PATH = Path(WALLPAPER_DIR)
WALLPAPER_PATH.mkdir(parents=True, exist_ok=True)

# 支持的图片类型
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


class WallpaperInfo(BaseModel):
    id: str
    filename: str
    url: str


class ActionResponse(BaseModel):
    ok: bool = True


@router.post("/wallpaper/upload", response_model=WallpaperInfo)
@require_role(Role.Super)
async def upload_wallpaper(
    file: UploadFile = File(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> WallpaperInfo:
    """上传壁纸"""
    filename = file.filename or "unknown.jpg"
    file_ext = Path(filename).suffix.lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        raise InvalidFileTypeError(file_type=file_ext or "unknown")

    content = await file.read()

    file_md5 = md5(content).hexdigest()

    filepath = WALLPAPER_PATH / f"{file_md5}{file_ext}"

    with filepath.open("wb") as f:
        f.write(content)

    logger.info(f"用户 {_current_user.username} 上传壁纸: {filepath}")

    return WallpaperInfo(
        id=file_md5,
        filename=filename,
        url=f"/api/common/wallpaper/{file_md5}{file_ext}",
    )


@router.get("/wallpaper/list", response_model=List[WallpaperInfo])
async def list_wallpapers(_current_user: DBUser = Depends(get_current_active_user)) -> List[WallpaperInfo]:
    """获取所有壁纸列表"""
    wallpapers: List[WallpaperInfo] = []

    for file in WALLPAPER_PATH.iterdir():
        if file.is_file() and file.suffix.lower() in ALLOWED_EXTENSIONS:
            file_id = file.stem
            wallpapers.append(
                WallpaperInfo(
                    id=file_id,
                    filename=file.name,
                    url=f"/api/common/wallpaper/{file.name}",
                )
            )

    return wallpapers


@router.delete("/wallpaper/{wallpaper_id}", response_model=ActionResponse)
@require_role(Role.Super)
async def delete_wallpaper(
    wallpaper_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """删除壁纸"""
    found = False
    for file in WALLPAPER_PATH.iterdir():
        if file.is_file() and file.stem == wallpaper_id:
            file.unlink()
            found = True
            logger.info(f"用户 {_current_user.username} 删除壁纸: {file}")
            break

    if not found:
        raise AppFileNotFoundError(filename=wallpaper_id)

    return ActionResponse(ok=True)


@router.get("/wallpaper/{filename}")
async def get_wallpaper(filename: str):
    """获取壁纸文件 (无需鉴权)"""
    filepath = WALLPAPER_PATH / filename
    if not filepath.exists():
        raise AppFileNotFoundError(filename=filename)

    headers = {"Cache-Control": "public, max-age=604800", "ETag": f'"{filename}"'}

    return FileResponse(filepath, headers=headers, media_type=f"image/{filepath.suffix.lstrip('.')}" )


UPLOAD_PATH = Path(USER_UPLOAD_DIR)


class UploadAccessToken(BaseModel):
    """聊天上传目录的取件令牌"""

    token: str
    query_param: str
    expires_at: int


@router.get("/uploads-access-token", response_model=UploadAccessToken)
@require_role(Role.Admin)
async def get_upload_access_token(
    chat_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> UploadAccessToken:
    """签发某个频道上传目录的取件令牌

    这是整条签名 URL 链路上**唯一**的授权点：能拿到令牌 == 能看这个频道。
    门槛与消息历史（`GET /chat-channel/{chat_key}/messages`）一致取 `Role.Admin`，
    归属校验交给 `assert_channel_accessible`（越权 404）。

    路径故意不写成 `/uploads/{chat_key}/token`：那会与取件路由
    `/uploads/{chat_key}/{filename}` 形状相同，靠注册顺序决定谁先匹配，
    等于把一个安全边界压在文件名不叫 `token` 上。
    """
    await assert_channel_accessible(_current_user, chat_key)
    token = issue_upload_access_token(chat_key)
    return UploadAccessToken(
        token=token,
        query_param=TOKEN_QUERY_PARAM,
        expires_at=token_expiry_seconds(token),
    )


@router.get("/uploads/{chat_key}/{filename}")
async def get_upload_file(
    chat_key: str,
    filename: str,
    sig: str = Query(default="", description="取件签名，见 tools/upload_access.py"),
):
    """获取聊天上传文件（凭签名取件，供 img 标签等直接访问）

    签名无效与文件不存在都返回同一个 404：`chat_key` 是可枚举的，
    把两者分开会让"这个频道有没有这个文件"变成可探测的。
    """
    if not verify_upload_access_token(sig, chat_key):
        logger.debug(f"取件签名校验未通过，按 404 处理: chat_key={chat_key}")
        raise AppFileNotFoundError(filename=filename)

    # 目录名用与写入侧同一个规整函数（`tools/common_util.py` 等落盘时用的就是它），
    # 否则含 `:` 之类字符的 chat_key 会读到一个不存在的目录；
    # `assert_path_within` 再消解一次 `..` 与软链，兜住路径穿越
    safe_chat_key = sanitize_chat_key_for_path(chat_key)
    safe_filename = Path(filename).name
    try:
        filepath = assert_path_within(UPLOAD_PATH, UPLOAD_PATH / safe_chat_key / safe_filename)
    except ValueError as exc:
        raise AppFileNotFoundError(filename=filename) from exc

    if not filepath.exists() or not filepath.is_file():
        raise AppFileNotFoundError(filename=filename)

    # 根据后缀推断 media type
    suffix = filepath.suffix.lower().lstrip(".")
    media_map = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "mp4": "video/mp4",
        "pdf": "application/pdf",
    }
    media_type = media_map.get(suffix, "application/octet-stream")

    # `private`：这是某个租户的聊天文件，只允许终端浏览器缓存，不进共享代理缓存。
    # 浏览器缓存要留着——取件 URL 在一个时间窗内是稳定的，没有缓存就会每次重新下载
    headers = {"Cache-Control": "private, max-age=86400", "ETag": f'"{safe_filename}"'}
    # 非已知媒体类型强制下载，防止浏览器执行未知内容
    if media_type == "application/octet-stream":
        headers["Content-Disposition"] = f'attachment; filename="{safe_filename}"'
    return FileResponse(filepath, headers=headers, media_type=media_type)
