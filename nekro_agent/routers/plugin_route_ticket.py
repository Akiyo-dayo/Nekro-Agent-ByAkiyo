"""插件路由的短时效票据接口

`EventSource` 与 `<img>` 带不了 `Authorization` 头，页面先用自己的登录态换一张票据，
再把票据拼进查询参数。票据本身只在这里签发，校验在 `services/plugin/route_auth.py`。
"""

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field

from nekro_agent.models.db_user import DBUser
from nekro_agent.services.plugin.route_auth import assert_plugin_route_access
from nekro_agent.services.plugin.route_ticket import TICKET_TTL_SECONDS, issue_plugin_route_ticket
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

router = APIRouter(prefix="/plugin-route", tags=["PluginRoute"])


class PluginRouteTicketRequest(BaseModel):
    """换票据请求体：插件键 + 实例对（全局视图时实例对都为空）"""

    plugin_key: str
    adapter_key: str = ""
    instance_key: str = ""


class PluginRouteTicketResponse(BaseModel):
    ticket: str
    expires_in: int = Field(default=TICKET_TTL_SECONDS, description="票据有效期（秒）")


@router.post("/ticket", summary="换取插件路由短时效票据", response_model=PluginRouteTicketResponse)
@require_role(Role.Admin)
async def issue_route_ticket(
    body: PluginRouteTicketRequest = Body(...),
    _current_user: DBUser = Depends(get_current_active_user),
) -> PluginRouteTicketResponse:
    """用登录态换一张只对指定插件与实例有效的短连接票据

    先做与路由依赖相同的实例可访问、插件生效校验（越权按不存在），再签发。
    租户必须带实例；只有平台方能拿全局视图的票据。
    """
    await assert_plugin_route_access(
        _current_user,
        body.plugin_key,
        body.adapter_key,
        body.instance_key,
        allow_global_view=True,
    )
    ticket = issue_plugin_route_ticket(
        _current_user,
        body.plugin_key,
        body.adapter_key,
        body.instance_key,
    )
    return PluginRouteTicketResponse(ticket=ticket, expires_in=TICKET_TTL_SECONDS)
