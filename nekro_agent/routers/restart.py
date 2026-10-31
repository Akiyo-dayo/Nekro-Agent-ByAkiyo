"""重启整个 NA 进程

## 「不支持」不是「失败」

改前这个模块只有一条能走通的路：`OsEnv.RUN_IN_DOCKER` 时调 Docker API 重启自己的容器；
其余部署一律落到最后一行 `raise OperationFailedError(operation="重启系统")` ——
HTTP 500、文案只有「重启系统失败」、日志里还有一整条堆栈。
裸机部署（`uv run bot`）因此**必然**失败，而管理员看到的信号是「有 bug」，
只能去翻日志找一个不存在的原因。

现在分成三种结果：

- Docker：沿用 `restart_self()`，行为逐字不变。
- systemd 且 `Restart=always`：优雅退出，由 systemd 拉起来。
- 其余：`DeploymentCapabilityError`（4xx），文案说清楚「当前部署形态没有能把进程拉回来的
  监管者」以及该怎么办。

并新增 `GET /restart/capability`，让前端在**点之前**就知道能不能重启 ——
禁用按钮 + 说明，而不是点了再报错。判据只有一处（`probe_restart_capability()`），
capability 与真正的重启共用它，不会出现「按钮可点但接口拒绝」。
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import DeploymentCapabilityError, OperationFailedError
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.tools.docker_util import restart_self
from nekro_agent.tools.process_supervisor import (
    RestartMode,
    probe_restart_capability,
    schedule_self_restart,
)

logger = get_sub_logger("system_control")
# 重启的是整个 NA 进程，一个进程承载全部租户的实例 —— 宿主级动作，故 Role.Super。
# 改前是 Role.Admin，等于任何一个客户管理员都能让所有客户的 Bot 一起断线。
router = APIRouter(prefix="/restart", tags=["Restart"])

RESTART_OPERATION = "重启系统"


class ActionResponse(BaseModel):
    ok: bool = True


class RestartCapabilityResponse(BaseModel):
    """当前部署形态支不支持从后台重启，以及不支持时的原因"""

    supported: bool
    mode: str
    reason_code: str = ""
    detail: str = ""
    unit: Optional[str] = None


@router.get("/capability", summary="查询当前部署形态是否支持重启")
@require_role(Role.Super)
async def get_restart_capability(
    _current_user: DBUser = Depends(get_current_active_user),
) -> RestartCapabilityResponse:
    """给前端用来决定「重启系统」按钮是可点、还是禁用并说明原因"""
    capability = await probe_restart_capability()
    return RestartCapabilityResponse(
        supported=capability.supported,
        mode=capability.mode.value,
        reason_code=capability.reason_code,
        detail=capability.detail,
        unit=capability.unit,
    )


@router.post("", summary="重启系统")
@require_role(Role.Super)
async def restart_system(
    _current_user: DBUser = Depends(get_current_active_user),
) -> ActionResponse:
    """重启整个系统服务"""
    capability = await probe_restart_capability()

    if capability.mode is RestartMode.DOCKER:
        logger.info("在Docker环境中执行重启操作")
        if await restart_self():
            return ActionResponse(ok=True)
        raise OperationFailedError(operation=RESTART_OPERATION)

    if capability.mode is RestartMode.SYSTEMD:
        logger.info(f"由 systemd 服务 {capability.unit} 接管重启，当前进程即将优雅退出")
        schedule_self_restart()
        return ActionResponse(ok=True)

    logger.warning(f"当前部署形态不支持重启: {capability.reason_code}")
    raise DeploymentCapabilityError(
        operation=RESTART_OPERATION,
        reason=capability.detail,
        # 与 `GET /restart/capability` 同形。`detail` 只有中文（它是「前端翻不到时的兜底」），
        # 前端要按 `reason_code` 取自己的本地化文案，而 systemd 那两个原因里要说出是哪个 unit ——
        # 少带 `unit` 就只能回落成中文，等于把 en-US 用户挡在门外。
        data={
            "reason_code": capability.reason_code,
            "mode": capability.mode.value,
            "unit": capability.unit,
        },
    )
