"""孤儿 NapCat 容器与数据目录的运维出口（平台方专属）

## 为什么要有这两个端点

`container_manager` 里每一个能停 / 删容器的函数都以 `DBAdapterInstance` 为入口，
所以**库里那一行一旦没了，它的容器在 WebUI 上就再也够不到**——只能 SSH 上去
`docker rm`。而 `purge_instance()` 从不删容器，于是「删实例」这个最普通的动作
每执行一次就制造一个够不到的孤儿。测试部署上两个这样的容器空转了 19 天，
合计 1.2 GB 内存 + 5.7 GB 磁盘（判据与实证见 `services/console/napcat_orphans.py`）。

出口收进产品自己：预览（只读）→ 回填确认 → 执行，与孤儿隔离规则那一套逐条对齐
（`routers/console/network_rules.py`）。

## 权限

两个端点都是 `@require_role(Role.Super)`。容器与宿主目录是**宿主级**资源，
一个都不属于任何租户；租户在旧权限体系里被钉死在 `Role.Admin`
（`TENANT_MAX_PERM_LEVEL`），因此对他们恒为 403。

**预览也必须是 Super**：它回显的是宿主上的容器名、状态与数据目录绝对路径，
那是宿主拓扑而不是租户资源。

## 二次确认与「删不删数据目录」分开问

回填的确认物是 `instance_key` 本身，与认领 / 并入 / 孤儿规则同一套。

`remove_data_dir` 单独一个开关且**默认不删**：容器删掉可以重建，
`napcat_data/{key}/QQ` 里的登录态删掉要重新扫码，两者的不可逆程度差一个量级。
判定为 `provider_changed` 的那一档尤其——实例还在，人可能只是打算迁回内置形态。
"""

from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.console.napcat_orphans import (
    NapcatOrphanPlan,
    NapcatOrphanRefused,
    execute_napcat_orphan_cleanup,
    plan_napcat_orphan_cleanup,
)
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

logger = get_sub_logger("napcat_orphans.console")

router = APIRouter(prefix="/napcat-orphans", tags=["Console NapCat Orphans"])


class NapcatOrphanCandidateInfo(BaseModel):
    """一个 instance_key 在宿主机上的残留物"""

    instance_key: str = Field(..., description="实例标识")
    verdict: str = Field(..., description="orphaned / provider_changed / in_use")
    reclaimable: bool = Field(..., description="能不能回收")
    reason: str = Field(..., description="判定理由；不可回收时是「为什么不能删」")
    container_name: str = Field(default="", description="容器名；没有容器时为空")
    container_state: str = Field(default="", description="容器状态")
    container_restart_policy: str = Field(default="", description="重启策略——孤儿能永远活着就是因为它")
    data_dir: str = Field(default="", description="数据目录绝对路径；目录不存在时为空")
    data_dir_bytes: int = Field(default=0, description="数据目录占用字节数")


class NapcatOrphanPlanResponse(BaseModel):
    """一次盘点的结果"""

    scope: str = Field(..., description="本部署的归属作用域，与容器标签同源")
    docker_available: bool = Field(..., description="有没有读到容器清单")
    docker_unavailable_reason: str = Field(default="", description="读不到时的原因；目录侧结论仍然有效")
    reclaimable_bytes: int = Field(default=0, description="可回收项的数据目录合计字节数")
    candidates: List[NapcatOrphanCandidateInfo] = Field(default_factory=list, description="全部候选，含不可回收项")


class NapcatOrphanCleanupRequest(BaseModel):
    """回收请求

    `confirm_instance_key` 必须与 `instance_key` 逐字相同 —— 与认领 / 并入 / 孤儿规则
    同一套回填确认。
    """

    instance_key: str = Field(..., description="要回收的实例标识")
    confirm_instance_key: str = Field(..., description="把标识原样再打一遍作为二次确认")
    remove_data_dir: bool = Field(default=False, description="是否连同数据目录一起删除（登录态不可恢复）")


class NapcatOrphanCleanupResponse(BaseModel):
    """回收结果"""

    ok: bool = Field(..., description="是否没有任何失败项")
    instance_key: str = Field(..., description="被回收的实例标识")
    verdict: str = Field(..., description="执行时重跑得到的判定")
    container_removed: bool = Field(..., description="容器是否已删除")
    data_dir_removed: bool = Field(..., description="数据目录是否已删除")
    freed_bytes: int = Field(default=0, description="释放的磁盘字节数")
    failures: List[str] = Field(default_factory=list, description="失败说明")
    message: str = Field(..., description="给人看的一句话结果")


def _to_response(plan: NapcatOrphanPlan) -> NapcatOrphanPlanResponse:
    return NapcatOrphanPlanResponse(
        scope=plan.scope,
        docker_available=plan.docker_available,
        docker_unavailable_reason=plan.docker_unavailable_reason,
        reclaimable_bytes=plan.reclaimable_bytes,
        candidates=[
            NapcatOrphanCandidateInfo(
                instance_key=candidate.instance_key,
                verdict=candidate.verdict.value,
                reclaimable=candidate.reclaimable,
                reason=candidate.reason,
                container_name=candidate.container_name,
                container_state=candidate.container_state,
                container_restart_policy=candidate.container_restart_policy,
                data_dir=candidate.data_dir or "",
                data_dir_bytes=candidate.data_dir_bytes,
            )
            for candidate in plan.candidates
        ],
    )


@router.get("/", summary="盘点宿主上的 NapCat 残留容器与数据目录")
@require_role(Role.Super)
async def get_napcat_orphans(
    _current_user: DBUser = Depends(get_current_active_user),
) -> NapcatOrphanPlanResponse:
    """列出本部署所有 NapCat 容器与数据目录，逐个给出判定

    **只读**：不删任何容器，也不动任何目录。
    """
    return _to_response(await plan_napcat_orphan_cleanup())


@router.post("/cleanup", summary="回收一个孤儿 NapCat 残留")
@require_role(Role.Super)
async def cleanup_napcat_orphan(
    body: NapcatOrphanCleanupRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> NapcatOrphanCleanupResponse:
    """删掉某个被判为孤儿的容器，可选连同数据目录

    一次只回收一个标识：确认物就是标识本身，多个合成一次请求会让「回填的到底在确认哪一个」
    变得含糊。判定在执行前重跑，拦住时返回 400 并把**具体理由**带回去。
    """
    instance_key = body.instance_key.strip()
    if not instance_key:
        raise ValidationError(reason="要回收的实例标识不能为空")
    if body.confirm_instance_key.strip() != instance_key:
        raise ValidationError(reason="回收 NapCat 残留需要回填正确的实例标识作为二次确认")

    try:
        outcome = await execute_napcat_orphan_cleanup(instance_key, remove_data_dir=body.remove_data_dir)
    except NapcatOrphanRefused as error:
        raise ValidationError(reason="；".join(error.reasons)) from error

    logger.warning(
        f"平台方回收 NapCat 残留: user={_current_user.username} instance_key={instance_key} "
        f"verdict={outcome.verdict.value} container_removed={outcome.container_removed} "
        f"data_dir_removed={outcome.data_dir_removed} freed_bytes={outcome.freed_bytes}",
    )

    parts: List[str] = []
    parts.append("已删除容器" if outcome.container_removed else "未删除容器")
    if body.remove_data_dir:
        parts.append(
            f"已删除数据目录（释放 {outcome.freed_bytes} 字节）" if outcome.data_dir_removed else "数据目录删除失败",
        )
    else:
        parts.append("数据目录按请求保留")
    message = f"{instance_key}：" + "，".join(parts)
    if outcome.failures:
        message += "；" + "；".join(outcome.failures)

    return NapcatOrphanCleanupResponse(
        ok=not outcome.failures,
        instance_key=outcome.instance_key,
        verdict=outcome.verdict.value,
        container_removed=outcome.container_removed,
        data_dir_removed=outcome.data_dir_removed,
        freed_bytes=outcome.freed_bytes,
        failures=list(outcome.failures),
        message=message,
    )
