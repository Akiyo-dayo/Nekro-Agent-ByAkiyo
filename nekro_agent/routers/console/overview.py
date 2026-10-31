"""总后台首页概览路由

只读端点，放宽到 `Role.Admin` 以便定制版客户能看到自己的概览；
统计范围由租户作用域决定，容器托管能力恒为平台方独占。
"""

import asyncio

from fastapi import APIRouter, Depends
from tortoise.timezone import now as db_now

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import ConsoleOverview, InstanceStatus
from nekro_agent.services.console.instance_manager import build_overview, resolve_instance_status
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import get_tenant_scope

router = APIRouter(tags=["Console"])


@router.get("/overview", summary="获取总后台概览")
@require_role(Role.Admin)
async def get_console_overview(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ConsoleOverview:
    """获取实例、会话与协议端托管能力的概览

    平台方看全局；租户只统计自己名下的实例，以及这些实例下的会话与消息。
    """
    scope = get_tenant_scope(_current_user)
    if scope is None:
        return await build_overview()

    instances = await DBAdapterInstance.filter(tenant_id=scope)
    instance_keys = [instance.instance_key for instance in instances]
    today_start = db_now().replace(hour=0, minute=0, second=0, microsecond=0)

    channel_total, message_count_today = await asyncio.gather(
        DBChatChannel.filter(instance_key__in=instance_keys).count(),
        DBChatMessage.filter(instance_key__in=instance_keys, create_time__gte=today_start).count(),
    )

    # 与 `build_overview()` 同一套状态解析：未绑定账号的实例必须从"离线"里分出来，
    # 否则租户后台会把一堆从来没接上的空壳实例报成掉线
    statuses = [
        resolve_instance_status(instance.status, instance.provider, instance.provider_account_id)
        for instance in instances
    ]
    return ConsoleOverview(
        instance_total=len(statuses),
        instance_online=sum(1 for status in statuses if status is InstanceStatus.ONLINE),
        instance_offline=sum(1 for status in statuses if status is InstanceStatus.OFFLINE),
        instance_unbound=sum(1 for status in statuses if status is InstanceStatus.UNBOUND),
        instance_unclaimed=sum(1 for status in statuses if status is InstanceStatus.UNCLAIMED),
        channel_total=channel_total,
        message_count_today=message_count_today,
        # 容器托管是平台方独占能力（NA 挂着宿主机的 docker.sock），对租户恒为不可用
        docker_available=False,
    )
