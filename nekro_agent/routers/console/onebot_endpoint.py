"""总后台：OneBot 协议端识别与「上报自身消息」自检

挂载于 `/api/console` 之下，只读。协议端类型来自 `get_version_info`；开关有没有开
没有任何标准动作能查，只能看 NA 自己发出的消息有没有被回传，判据与内存状态都在
`adapters/onebot_v11/core/endpoint_status.py`。

权限与 `console/qbob.py` 的只读运维那一档相同：`Role.Admin` + 实例归属校验，越权一律 404。
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends
from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

from nekro_agent.adapters.onebot_v11.core.channel import ADAPTER_KEY
from nekro_agent.adapters.onebot_v11.core.endpoint_status import EndpointInfo, endpoint_status, parse_endpoint_info
from nekro_agent.adapters.onebot_v11.core.instance import lookup_cached_account_id, resolve_account_id
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console_onebot import OnebotEndpointStatus
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, get_tenant_scope

logger = get_sub_logger("console.onebot")

router = APIRouter(tags=["Console OneBot"])

# 现场查询的等待上限。状态页可以慢一点，但不能被一个不回话的协议端吊住
VERSION_QUERY_TIMEOUT_SECONDS = 5.0


async def _resolve_onebot_instance(instance_key: str) -> DBAdapterInstance:
    """按 `(onebot_v11, instance_key)` 定位实例，且不取墓碑行

    `instance_key` 只在适配器内唯一：只按标识取第一行会落到另一适配器的同名实例上，
    表现为「状态页看的不是这个实例」。
    """
    instance = (
        await DBAdapterInstance.filter(adapter_key=ADAPTER_KEY, instance_key=instance_key)
        .exclude(status=TOMBSTONE_STATUS)
        .order_by("id")
        .first()
    )
    if instance is None:
        raise NotFoundError(resource=f"实例 '{instance_key}'")
    return instance


async def _resolve_accessible_instance(user: DBUser, instance_key: str) -> DBAdapterInstance:
    """定位实例并校验归属：与 `console/qbob.py` 同一判据

    在本模块里另写一份而不是 import：全路由权限契约按「本模块内走到守卫的函数」
    认端点是否接了守卫，借来的函数不算数。
    """
    instance = await _resolve_onebot_instance(instance_key)
    accessible = await assert_instance_accessible(user, instance_key, adapter_key=ADAPTER_KEY)
    if get_tenant_scope(user) is not None and accessible.id != instance.id:
        logger.warning(f"实例标识 {instance_key!r} 在库里对应多行，归属校验与操作目标不是同一行，拒绝本次访问")
        raise NotFoundError(resource=f"实例 '{instance_key}'")
    return instance


async def _fetch_endpoint_info(bot: OneBotV11Bot) -> Optional[EndpointInfo]:
    """现场调一次 `get_version_info`；失败吞掉，状态页照常返回其余字段"""
    try:
        payload = await asyncio.wait_for(bot.call_api("get_version_info"), timeout=VERSION_QUERY_TIMEOUT_SECONDS)
    except Exception as e:
        logger.debug(f"现场查询协议端版本失败: {e}")
        return None
    info = parse_endpoint_info(payload)
    if info is None:
        logger.debug(f"协议端 get_version_info 返回了非字典值: {type(payload).__name__}")
    return info


@router.get("/instances/{instance_key}/onebot/endpoint", summary="获取 OneBot 协议端与自身消息上报状态")
@require_role(Role.Admin)
async def get_onebot_endpoint_status(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> OnebotEndpointStatus:
    """看这个实例连的是哪种协议端、有没有在上报自身消息

    没绑定账号、账号不在线都按「未连接」返回，不调协议端：状态页不该因为协议端掉线而报错。
    """
    await _resolve_accessible_instance(_current_user, instance_key)
    account = lookup_cached_account_id(instance_key) or await resolve_account_id(instance_key)
    if not account:
        return OnebotEndpointStatus(online=False)

    bot = get_bots().get(account)
    if not isinstance(bot, OneBotV11Bot):
        return OnebotEndpointStatus(online=False, account_id=account)

    snapshot = endpoint_status.snapshot(account)
    if snapshot.info is None:
        info = await _fetch_endpoint_info(bot)
        if info is not None:
            endpoint_status.note_info(account, info)
            snapshot = endpoint_status.snapshot(account)

    info = snapshot.info
    return OnebotEndpointStatus(
        online=True,
        account_id=account,
        app_name=info.app_name if info else "",
        app_version=info.app_version if info else "",
        protocol_version=info.protocol_version if info else "",
        implementation=snapshot.implementation,
        self_report=snapshot.self_report.value,
        own_sends=snapshot.own_sends,
        echoes=snapshot.echoes,
        connected_at=snapshot.connected_at,
        last_echo_at=snapshot.last_echo_at,
    )
