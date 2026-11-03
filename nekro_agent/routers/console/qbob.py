"""总后台：附带 qbob（QQ 官方机器人 → OneBot v11）的接入与运维路由

挂载于 `/api/console` 之下。业务逻辑全部在 `services/console/qbob_manager.py`，这一层只做
参数收敛、权限分档与归属校验。设计见 `docs/qbob-integration.md` §8、§9。

## 两档权限（对照 `console/containers.py`）

| 档 | 端点 | 要求 |
|---|---|---|
| 部署级 / 接入拓扑 | 服务状态、手工设置服务账号、扫码接入、手填接入、重新接线、解绑 | `Role.Super` |
| 自助运维 | 读 bot 状态、启用/停用与两项开关、重载 | `Role.Admin` + 实例归属校验 |

接入归平台方：它决定「哪个 QQ 机器人接到哪个实例」，并写入带实例令牌的出站端点，
与 NapCat 的 `network-config` 同档。自助运维那一档的响应里没有任何凭据（见 `schemas/console_qbob.py`）。
"""

from fastapi import APIRouter, Depends

from nekro_agent.adapters.onebot_v11.core.channel import ADAPTER_KEY as ONEBOT_ADAPTER_KEY
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.console import ActionOkResponse
from nekro_agent.schemas.console_qbob import (
    QbobAttachRequest,
    QbobBotInfo,
    QbobBotOptionsRequest,
    QbobQrPollResponse,
    QbobQrStartResponse,
    QbobServiceCredentialRequest,
    QbobServiceStatus,
)
from nekro_agent.schemas.errors import NotFoundError
from nekro_agent.services.console import container_manager, qbob_manager
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.user.tenant_perm import assert_instance_accessible, get_tenant_scope

logger = get_sub_logger("console.qbob")

router = APIRouter(tags=["Console qbob"])


async def _resolve_qbob_instance(instance_key: str) -> DBAdapterInstance:
    """按 `(onebot_v11, instance_key)` 定位 qbob 实例，且不取墓碑行

    qbob 实例必定挂在 OneBot 适配器下。只按 `instance_key` 取 `.first()` 会拿到同名的
    另一适配器行：租户看到 404，平台方的操作落到非 qbob 行上报「不是内置 qbob 实例」。
    """
    container_manager.validate_instance_key(instance_key)
    instance = (
        await DBAdapterInstance.filter(adapter_key=ONEBOT_ADAPTER_KEY, instance_key=instance_key)
        .exclude(status=TOMBSTONE_STATUS)
        .order_by("id")
        .first()
    )
    if instance is None:
        raise NotFoundError(resource=f"实例 '{instance_key}'")
    return instance


async def _resolve_accessible_instance(user: DBUser, instance_key: str) -> DBAdapterInstance:
    """定位实例并校验归属：与 `console/containers.py` 同名函数同一判据（理由见那里的 docstring）

    在本模块里另写一份而不是 import：全路由权限契约按「本模块内走到守卫的函数」认端点是否接了守卫，
    借来的函数不算数。
    """
    instance = await _resolve_qbob_instance(instance_key)
    accessible = await assert_instance_accessible(user, instance_key, adapter_key=ONEBOT_ADAPTER_KEY)
    if get_tenant_scope(user) is not None and accessible.id != instance.id:
        logger.warning(f"实例标识 {instance_key!r} 在库里对应多行，归属校验与操作目标不是同一行，拒绝本次操作")
        raise NotFoundError(resource=f"实例 '{instance_key}'")
    return instance


# ---------------------------------------------------------------------------
# 部署级
# ---------------------------------------------------------------------------


@router.get("/qbob/service", summary="获取附带 qbob 服务状态")
@require_role(Role.Super)
async def get_service_status(
    _current_user: DBUser = Depends(get_current_active_user),
) -> QbobServiceStatus:
    """本部署有没有附带 qbob、连不连得上、NA 的服务账号是否可用；不可用时返回说明而不是报错"""
    return await qbob_manager.service_status()


@router.put("/qbob/service/credential", summary="手工设置 qbob 服务账号")
@require_role(Role.Super)
async def set_service_credential(
    body: QbobServiceCredentialRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """qbob 的 admin 初始口令文件已不在时，由平台方提供一个 qbob admin 账号；只写不读"""
    await qbob_manager.set_service_credential(body.username, body.password)
    return ActionOkResponse(message="qbob 服务账号已保存")


# ---------------------------------------------------------------------------
# 接入拓扑（平台方）
# ---------------------------------------------------------------------------


@router.post("/instances/{instance_key}/qbob/qrconnect", summary="生成 qbob 扫码接入二维码")
@require_role(Role.Super)
async def start_qrconnect(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> QbobQrStartResponse:
    instance = await _resolve_qbob_instance(instance_key)
    return await qbob_manager.start_qrconnect(instance)


@router.get("/instances/{instance_key}/qbob/qrconnect/{task_id}", summary="查询 qbob 扫码接入结果")
@require_role(Role.Super)
async def poll_qrconnect(
    instance_key: str,
    task_id: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> QbobQrPollResponse:
    """扫码完成后由后端直接接入；拿到的 AppSecret 不回传"""
    instance = await _resolve_qbob_instance(instance_key)
    return await qbob_manager.poll_qrconnect(instance, task_id)


@router.post("/instances/{instance_key}/qbob/attach", summary="手填凭据接入 qbob bot")
@require_role(Role.Super)
async def attach(
    instance_key: str,
    body: QbobAttachRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> QbobBotInfo:
    instance = await _resolve_qbob_instance(instance_key)
    return await qbob_manager.attach_with_credentials(instance, body.appid, body.secret)


@router.post("/instances/{instance_key}/qbob/rewire", summary="重新接线 qbob bot")
@require_role(Role.Super)
async def rewire(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> QbobBotInfo:
    """把 bot 的反向 WS 端点写回 NA 期望的那一条（地址与令牌由后端推导）"""
    instance = await _resolve_qbob_instance(instance_key)
    return await qbob_manager.rewire(instance)


@router.delete("/instances/{instance_key}/qbob", summary="解绑 qbob bot")
@require_role(Role.Super)
async def detach(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """从 qbob 删除本实例托管的 bot，并解除实例的账号绑定；实例本身保留，可重新接入"""
    instance = await _resolve_qbob_instance(instance_key)
    await qbob_manager.detach(instance)
    return ActionOkResponse(message="已解绑 qbob bot")


# ---------------------------------------------------------------------------
# 自助运维（租户管理员 + 归属校验）
# ---------------------------------------------------------------------------


@router.get("/instances/{instance_key}/qbob", summary="获取 qbob bot 状态")
@require_role(Role.Admin)
async def get_bot_info(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> QbobBotInfo:
    """取不到时返回说明而不是报错；响应里没有任何凭据，租户看到的服务级说明不带内部地址"""
    instance = await _resolve_accessible_instance(_current_user, instance_key)
    qbob_manager.require_qbob_instance(instance)
    return await qbob_manager.get_bot_info(instance, platform_view=get_tenant_scope(_current_user) is None)


@router.patch("/instances/{instance_key}/qbob", summary="修改 qbob bot 开关")
@require_role(Role.Admin)
async def update_bot_options(
    instance_key: str,
    body: QbobBotOptionsRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> QbobBotInfo:
    instance = await _resolve_accessible_instance(_current_user, instance_key)
    return await qbob_manager.update_bot_options(
        instance, body, platform_view=get_tenant_scope(_current_user) is None
    )


@router.post("/instances/{instance_key}/qbob/reload", summary="重载 qbob bot")
@require_role(Role.Admin)
async def reload_bot(
    instance_key: str,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionOkResponse:
    """bot 卡住时的第一步自愈，与「重启我的 NapCat」同一定位"""
    instance = await _resolve_accessible_instance(_current_user, instance_key)
    await qbob_manager.reload_bot(instance, platform_view=get_tenant_scope(_current_user) is None)
    return ActionOkResponse(message="qbob bot 已重载")
