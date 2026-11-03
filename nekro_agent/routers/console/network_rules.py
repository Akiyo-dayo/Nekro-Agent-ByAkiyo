"""孤儿隔离规则的运维出口（平台方专属）

## 为什么要有这两个端点

升级换作用域之后，链上会留下一组用旧标记的规则（`nekro-sandbox:default` /
`nekro-cc:default[.tN]`）。新代码用新标记，因此**永远不会去清它们**；启动时只打一条
WARNING，运维手上没有任何安全的执行手段。之前就是这样出的事：手写
`iptables -S | sed 's/^-A /-D /'` 逐条重放，`--comment "nekro-cc:x"` 的引号被 shell
吃掉，两条命令全部 `Bad rule`。**半成功比全失败更危险**——
被删掉的可能正好是还在生效的那一组。

所以这里把这件事收进产品自己：预览（只读）-> 二次确认 -> 走
`services/sandbox/network_guard.py:execute_orphan_rule_cleanup`，全程 argv 数组，
不拼裸命令字符串。

## 权限

两个端点都是 `@require_role(Role.Super)`。iptables 规则是**宿主级**资源，一条都不属于
任何租户；租户在旧权限体系里被钉死在 `Role.Admin`（`TENANT_MAX_PERM_LEVEL`），
因此对他们恒为 403 —— 这是**能力边界**而不是资源存在性，拒绝发生在进入函数体之前，
不区分任何标记，所以不泄露「链上有哪些标记」。

## 二次确认

沿用认领 / 并入 / 历史归属迁移那一套**回填确认**：请求里必须把标记原样再打一遍
（`confirm_marker == marker`），不另造一套令牌。理由与那几处相同——要删的对象名字
本身就是最不可能误按的确认物。

判定在**执行前重跑一遍**（见 `execute_orphan_rule_cleanup`），预览时的结论不作数：
预览与确认之间完全可能有人把一个 CC 工作区启动起来，而它会挂回旧网桥。
"""

from typing import FrozenSet, List, Tuple

import aiodocker
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import ValidationError
from nekro_agent.services.sandbox.network_guard import (
    SANDBOX_MARKER_FAMILY,
    GuardMarkerFamily,
    OrphanCleanupPlan,
    OrphanCleanupRefused,
    execute_orphan_rule_cleanup,
    plan_orphan_rule_cleanup,
)
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role
from nekro_agent.services.workspace.cc_credential import relay_host_port
from nekro_agent.services.workspace.network_guard import WORKSPACE_MARKER_FAMILY

logger = get_sub_logger("network_rules.console")

router = APIRouter(prefix="/network-rules", tags=["Console Network Rules"])


def guard_marker_families() -> Tuple[GuardMarkerFamily, ...]:
    """本项目会往链上装规则的两个守卫

    在这里组装而不是让沙盒模块直接 import 工作区模块：依赖方向是
    工作区 -> 沙盒，反过来就是循环 import。
    """
    return (SANDBOX_MARKER_FAMILY, WORKSPACE_MARKER_FAMILY)


def own_listening_ports() -> FrozenSet[int]:
    """本部署在**宿主上**占着的 TCP 端口

    存活判据要靠它把「有人在听」拆成两种：探测到的监听者是**我们自己**时不构成
    「另一套部署还活着」的证据 —— 探测打的是宿主上的 `(网桥网关, 端口)`，而我们绑的是
    `0.0.0.0`，所以每一个网桥网关地址上都是我们在应答，探测在这些端口上恒为 `open`、
    信息量为零。这一条豁免不加的话，「清掉自己升级前那组规则」这个主用例永远拦在
    「端口上还有人在听」上。

    ⚠️ **必须是「宿主上」的端口，不能把容器内那个也算进来。** 宿主 TCP 端口独占只对
    宿主自己的 socket 成立：NA 在容器里时它 bind 的 `relay_listen_port()`（compose 下
    容器内恒为 8021）落在**另一个网络命名空间**，宿主上的 8021 完全可能是同宿主上另一套
    **裸机**部署占着的 —— 而裸机部署的默认端口正好也是 8021。把容器内端口算进豁免，
    那套邻居的沙盒放行端点就会被当成「我们自己的端口」跳过探测，于是它的规则在
    「没有任何存活证据」下被判成孤儿删掉，正好复现「邻居的放行规则被整组删掉、
    它的沙盒执行从此静默卡到超时」那场事故。

    所以只收两个**按定义就在宿主侧**的取值：`EXPOSE_PORT`（发布到宿主的那个）与
    `relay_host_port()`（`RUN_IN_DOCKER` 时就是 `EXPOSE_PORT`，否则才是进程 bind 的那个）。
    沙盒回调端点的端口本来也取自 `SANDBOX_CHAT_API_URL`（默认
    `http://host.docker.internal:{EXPOSE_PORT}/api`），走的就是宿主侧口径。
    """
    ports = {OsEnv.EXPOSE_PORT}
    try:
        ports.add(relay_host_port())
    except Exception as e:
        # 拿不到只会让判据更严（少一个"这是我们自己"的豁免 -> 更容易被拦住），不影响安全性
        logger.debug(f"读取本部署监听端口失败，孤儿规则判定按更严的一侧处理: {e}")
    return frozenset(port for port in ports if port > 0)


class OrphanRuleLineInfo(BaseModel):
    """一条规则的原文"""

    family: str = Field(..., description="地址族")
    chain: str = Field(..., description="链名")
    text: str = Field(..., description="`iptables -S` 读回来的原文")


class OrphanRuleNetworkInfo(BaseModel):
    """规则匹配的网桥上的容器情况"""

    interface: str = Field(..., description="网桥接口名")
    network: str = Field(default="", description="对应的 Docker 网络名；空表示宿主上没有容器引用它")
    container_total: int = Field(default=0, description="这张网上还有多少个容器（含已停止但仍钉在这张网上的）")
    container_names: List[str] = Field(default_factory=list, description="容器名")


class OrphanRuleCandidateInfo(BaseModel):
    """一个标记的判定结果"""

    marker: str = Field(..., description="规则标记")
    guard_label: str = Field(..., description="属于哪个守卫")
    verdict: str = Field(..., description="self / blocked / orphan")
    rule_count: int = Field(..., description="链上带该标记的规则条数")
    chains: List[str] = Field(default_factory=list, description="涉及的地址族与链")
    rules: List[OrphanRuleLineInfo] = Field(default_factory=list, description="规则原文")
    networks: List[OrphanRuleNetworkInfo] = Field(default_factory=list, description="规则匹配的网桥")
    container_total: int = Field(default=0, description="这些网桥上的容器总数")
    blockers: List[str] = Field(default_factory=list, description="拦住它的原因")
    notes: List[str] = Field(default_factory=list, description="判定过程中的事实，不构成拦截")


class OrphanRulePlanResponse(BaseModel):
    """一次盘点的结果"""

    scope: str = Field(..., description="本部署当前的作用域")
    scope_source: str = Field(..., description="作用域的推导来源，与启动日志同源")
    available: bool = Field(..., description="有没有可用的 iptables 执行通道")
    unavailable_reason: str = Field(default="", description="没有时的原因")
    candidates: List[OrphanRuleCandidateInfo] = Field(default_factory=list, description="链上找到的全部本项目标记")


class OrphanRuleCleanupRequest(BaseModel):
    """清理请求

    `confirm_marker` 必须与 `marker` 逐字相同 —— 与认领 / 并入 / 历史归属迁移同一套回填确认。
    """

    marker: str = Field(..., description="要清理的规则标记")
    confirm_marker: str = Field(..., description="把标记原样再打一遍作为二次确认")


class OrphanRuleCleanupResponse(BaseModel):
    """清理结果"""

    ok: bool = Field(..., description="是否全部删干净")
    marker: str = Field(..., description="被清理的标记")
    expected: int = Field(..., description="执行前链上带该标记的条数")
    deleted: int = Field(..., description="实际删掉的条数")
    residual: int = Field(..., description="执行后仍带该标记的条数")
    failures: List[str] = Field(default_factory=list, description="逐条删除失败的说明")
    message: str = Field(..., description="给人看的一句话结果")


def _to_response(plan: OrphanCleanupPlan) -> OrphanRulePlanResponse:
    return OrphanRulePlanResponse(
        scope=plan.scope,
        scope_source=plan.scope_source,
        available=plan.available,
        unavailable_reason=plan.unavailable_reason,
        candidates=[
            OrphanRuleCandidateInfo(
                marker=candidate.marker,
                guard_label=candidate.guard_label,
                verdict=candidate.verdict,
                rule_count=candidate.rule_count,
                chains=list(candidate.chains),
                rules=[
                    OrphanRuleLineInfo(family=line.family, chain=line.chain, text=line.text) for line in candidate.rules
                ],
                networks=[
                    OrphanRuleNetworkInfo(
                        interface=fact.interface,
                        network=fact.network,
                        container_total=fact.container_total,
                        container_names=list(fact.container_names),
                    )
                    for fact in candidate.networks
                ],
                container_total=candidate.container_total,
                blockers=list(candidate.blockers),
                notes=list(candidate.notes),
            )
            for candidate in plan.candidates
        ],
    )


@router.get("/orphans", summary="盘点链上的隔离规则标记")
@require_role(Role.Super)
async def get_orphan_isolation_rules(
    _current_user: DBUser = Depends(get_current_active_user),
) -> OrphanRulePlanResponse:
    """列出链上所有本项目的规则标记，逐个给出判定与规则原文

    **只读**：不执行任何 `-D`，也不改动任何 Docker 资源。
    """
    docker = aiodocker.Docker()
    try:
        plan = await plan_orphan_rule_cleanup(docker, guard_marker_families(), own_listening_ports())
    finally:
        await docker.close()
    return _to_response(plan)


@router.post("/orphans/cleanup", summary="清理一组孤儿隔离规则")
@require_role(Role.Super)
async def cleanup_orphan_isolation_rules(
    body: OrphanRuleCleanupRequest,
    _current_user: DBUser = Depends(get_current_active_user),
) -> OrphanRuleCleanupResponse:
    """把某个被判为孤儿的标记从链上删掉

    一次只清一个标记：确认物就是标记本身，多个标记合成一次请求会让「回填的到底在确认哪一个」
    变得含糊。判定在执行前重跑，拦住时返回 400 并把**具体理由**带回去。
    """
    marker = body.marker.strip()
    if not marker:
        raise ValidationError(reason="要清理的规则标记不能为空")
    if body.confirm_marker.strip() != marker:
        raise ValidationError(reason="清理孤儿隔离规则需要回填正确的规则标记作为二次确认")

    docker = aiodocker.Docker()
    try:
        outcome = await execute_orphan_rule_cleanup(
            docker,
            guard_marker_families(),
            own_listening_ports(),
            marker,
        )
    except OrphanCleanupRefused as error:
        raise ValidationError(reason="；".join(error.reasons)) from error
    finally:
        await docker.close()

    logger.info(
        f"平台方清理孤儿隔离规则: user={_current_user.username} marker={marker} "
        f"expected={outcome.expected} deleted={outcome.deleted} residual={outcome.residual}",
    )
    message = f"已清理标记 {marker}：预期 {outcome.expected} 条，删除 {outcome.deleted} 条"
    if outcome.residual:
        message += f"；仍残留 {outcome.residual} 条，可重试本操作补齐"
    return OrphanRuleCleanupResponse(
        ok=outcome.residual == 0,
        marker=outcome.marker,
        expected=outcome.expected,
        deleted=outcome.deleted,
        residual=outcome.residual,
        failures=list(outcome.failures),
        message=message,
    )
