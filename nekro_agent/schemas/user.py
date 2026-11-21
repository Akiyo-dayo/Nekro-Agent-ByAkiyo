from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

# 界面语言只支持这两种。列表本身不是"随便加的枚举"：前端 `locales/` 只有这两套文案，
# 写进别的值会让界面在启动时找不到资源、回退到中文，而用户以为设置生效了。
# 写入口的合法性由 `UpdateUserLocaleRequest.locale` 的 `Literal` 保证；这份元组是**读**
# 那一侧的口径——库里可能留着旧版本或手工改过的值，读出来必须先按它过滤一遍。
SUPPORTED_UI_LOCALES: tuple[str, ...] = ("zh-CN", "en-US")

# `DBUser.ext_data` 里存界面语言的键名。读取侧的判断与写入侧必须用同一个常量，
# 各写一份字面量迟早会分叉成"写得进去、读不出来"。
UI_LOCALE_FIELD = "ui_locale"


class User(BaseModel):
    id: int
    username: str
    password: str
    perm_level: int
    login_time: datetime
    is_active: bool = True

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str
    password: str
    adapter_key: str
    platform_userid: str
    access_key: Optional[str] = None


class UserUpdate(BaseModel):
    username: str


class UpdatePassword(BaseModel):
    """自助改密请求

    没有 `user_id`：这个接口只改**调用者自己**的口令。它曾经有一个，但那个字段从来没有
    生效过 —— `routers/user.py` 无论如何都把新口令写给 `_current_user`，于是平台方带上
    别人的 `user_id` 时改掉的其实是自己的口令，接口还回 200。重置**他人**口令是另一条
    端点 `POST /user-manager/{user_id}/reset-password`（走 `assert_managed_user_accessible`）。

    `old_password` 是当前口令，服务端必校验。pydantic 默认忽略多余字段，所以旧客户端
    继续带 `user_id` 不会 422，但它不再有任何含义。
    """

    old_password: str = ""
    password: str


class UserLogin(BaseModel):
    username: str
    password: str


class UserLocaleResponse(BaseModel):
    """调用者自己的界面语言

    `None` 表示"没设置过"，由前端决定用浏览器语言还是服务端默认语言。不用空串表达，
    是因为空串在请求体里会与"传了一个空值"混起来，两边的校验口径就分叉了。
    """

    locale: Optional[Literal["zh-CN", "en-US"]] = None


class UpdateUserLocaleRequest(BaseModel):
    """保存自己的界面语言

    只有这一个字段，且用 `Literal` 收口：服务端不认识的取值一律 422，而不是先存下来、
    等前端拿到一个认不出的时候再悄悄回退——那种"设置成功但没生效"最难排查。

    **没有 `user_id`**：与 `UpdatePassword` 同一条设计，这条端点只写调用者自己。
    """

    locale: Literal["zh-CN", "en-US"]


class UserToken(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
