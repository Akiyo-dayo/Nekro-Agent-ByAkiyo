from datetime import datetime
from typing import Optional

from pydantic import BaseModel


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


class UserToken(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
