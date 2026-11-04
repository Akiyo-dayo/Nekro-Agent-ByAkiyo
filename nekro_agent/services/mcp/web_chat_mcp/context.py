"""Web Chat MCP 工具调用的身份上下文

MCP 的请求处理由 SDK 自己分派，工具函数拿不到 FastAPI 的 `Request`，所以身份只能放进
`ContextVar`：入口 ASGI 包装器验证通过后 set，工具调用期间读，请求结束时 reset。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import UnauthorizedError

_current_user: ContextVar[Optional[DBUser]] = ContextVar("web_chat_mcp_current_user", default=None)


def set_current_user(user: DBUser) -> Token[Optional[DBUser]]:
    return _current_user.set(user)


def reset_current_user(token: Token[Optional[DBUser]]) -> None:
    _current_user.reset(token)


def get_current_user() -> DBUser:
    user = _current_user.get()
    if user is None:
        raise UnauthorizedError
    return user
