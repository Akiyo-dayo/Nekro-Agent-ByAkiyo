"""运维指令目录：读写分类、参数约定与写操作落地

## 分类是安全边界，不是排版

`OpsActionKind.READ` 的指令在 IM 里直接出结果；`WRITE` 的指令在 IM 里**只出深链**。
命令层不判断"这条像不像危险操作"，只查本表——分类写在数据里，
新增一条指令时必须显式声明属于哪一类，漏声明会在注册时就暴露而不是在线上暴露。

## 写操作为什么不自己实现业务逻辑

每个写操作都直接调用 `services/console/` 里总后台正在用的同一个函数。
如果运维 Bot 自己再写一遍"停用实例"，两条路径迟早会分叉，
其中一条会漏掉将来新增的副作用（缓存失效、状态广播、审计）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.tenant import TenantStatus

logger = get_sub_logger("ops_bot.actions")


class OpsActionKind(str, Enum):
    """指令类别

    READ  —— 在 IM 里直接返回结果
    WRITE —— 在 IM 里只返回确认深链，实际执行发生在总后台
    """

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class OpsActionSpec:
    """一条运维指令的声明

    Attributes:
        name: 指令名，用户在 IM 里输入的就是它
        kind: 读/写分类
        summary: 一句话说明；写操作的这句话会原样出现在深链落地页上，
            所以必须写成"这条链接点下去会发生什么"，而不是"这个功能是干嘛的"
        target_label: 需要的目标参数名称；空串表示该指令不接受目标
    """

    name: str
    kind: OpsActionKind
    summary: str
    target_label: str = ""

    @property
    def requires_target(self) -> bool:
        return bool(self.target_label)

    @property
    def usage(self) -> str:
        return f"{self.name} <{self.target_label}>" if self.requires_target else self.name


OPS_ACTIONS: Dict[str, OpsActionSpec] = {
    spec.name: spec
    for spec in (
        OpsActionSpec("overview", OpsActionKind.READ, "总览：实例总数 / 在线 / 离线 / 待认领、今日消息量"),
        OpsActionSpec("instances", OpsActionKind.READ, "列出全部实例及其在线状态"),
        OpsActionSpec("health", OpsActionKind.READ, "列出实例探活结果与掉线时长"),
        OpsActionSpec("tenants", OpsActionKind.READ, "列出租户及其状态、名下实例数"),
        OpsActionSpec("errors", OpsActionKind.READ, "最近的错误日志摘要"),
        OpsActionSpec("enable-instance", OpsActionKind.WRITE, "启用指定实例，使其恢复收发消息", "实例标识"),
        OpsActionSpec("disable-instance", OpsActionKind.WRITE, "停用指定实例，使其立即停止收发消息", "实例标识"),
        OpsActionSpec("restart-container", OpsActionKind.WRITE, "重启指定实例的协议端容器", "实例标识"),
        OpsActionSpec("suspend-tenant", OpsActionKind.WRITE, "停用指定租户：其成员无法登录，其实例停止收发", "租户 ID"),
        OpsActionSpec("resume-tenant", OpsActionKind.WRITE, "恢复指定租户为正常状态", "租户 ID"),
    )
}


def resolve_action(name: str) -> Optional[OpsActionSpec]:
    """按指令名取声明，未知指令返回 None"""
    return OPS_ACTIONS.get(name.strip().lower())


@dataclass(frozen=True)
class OpsCommandInput:
    """一条运维指令的解析结果

    `error` 非空即表示解析失败，此时 `action`/`target` 无意义。
    用返回值而不是异常表达失败，是因为"参数写错了"在 IM 里是常态，
    需要回一条可读的提示而不是走异常路径。
    """

    action: str = ""
    target: str = ""
    error: str = ""

    @property
    def is_help(self) -> bool:
        return not self.action and not self.error


def parse_ops_command(raw_text: str) -> OpsCommandInput:
    """解析 `<指令> [目标]`

    刻意不复用命令系统的通用参数解析器：那套解析器会把 `a:b` 识别成 K-V 对，
    而实例标识与租户 ID 都是自由文本，一旦出现冒号就会被静默拆走。
    运维指令的形态固定为两段，自己切更可控也更好测。
    """
    tokens = raw_text.split()
    if not tokens:
        return OpsCommandInput()

    action = tokens[0].lower()
    spec = resolve_action(action)
    if spec is None:
        return OpsCommandInput(error=f"未知指令: {tokens[0]}")
    if len(tokens) > 2:
        return OpsCommandInput(error=f"参数过多，用法: {spec.usage}")

    target = tokens[1] if len(tokens) == 2 else ""
    if spec.requires_target and not target:
        return OpsCommandInput(error=f"缺少目标（{spec.target_label}），用法: {spec.usage}")
    if not spec.requires_target and target:
        return OpsCommandInput(error=f"该指令不接受参数，用法: {spec.usage}")

    return OpsCommandInput(action=spec.name, target=target)


async def execute_write_action(action: str, target: str) -> str:
    """执行一条写操作

    只应由深链落地端点在校验通过后调用。**不要**从命令链路调用本函数——
    那样等于把 IM 消息变成了直接的执行凭据，整个深链设计就失效了。

    Args:
        action: 写操作标识
        target: 操作目标

    Returns:
        给操作者看的结果描述

    Raises:
        ValidationError: 指令未知、类别不是写操作，或目标非法
        NotFoundError: 目标实例/租户不存在
    """
    from nekro_agent.models.db_tenant import DBTenant
    from nekro_agent.schemas.errors import NotFoundError, ValidationError
    from nekro_agent.services.console.instance_manager import get_instance, set_instance_enabled

    spec = resolve_action(action)
    if spec is None or spec.kind is not OpsActionKind.WRITE:
        raise ValidationError(reason=f"未知的运维写操作: {action}")
    if spec.requires_target and not target.strip():
        raise ValidationError(reason=f"运维写操作 {action} 缺少目标（{spec.target_label}）")

    target = target.strip()

    if action in ("enable-instance", "disable-instance"):
        instance = await get_instance(target)
        await set_instance_enabled(instance, enabled=action == "enable-instance")
        state = "已启用" if action == "enable-instance" else "已停用"
        logger.info(f"运维深链执行: {action} instance={target}")
        return f"实例 {target} {state}"

    if action == "restart-container":
        from nekro_agent.services.console.container_manager import resolve_instance, restart_container

        instance = await resolve_instance(target)
        await restart_container(instance)
        logger.info(f"运维深链执行: {action} instance={target}")
        return f"实例 {target} 的协议端容器已重启"

    # 余下两条都是租户状态切换
    if not target.isdigit():
        raise ValidationError(reason=f"租户 ID 必须是数字: {target}")
    tenant = await DBTenant.get_or_none(id=int(target))
    if tenant is None:
        raise NotFoundError(resource=f"租户 '{target}'")

    tenant.status = (TenantStatus.SUSPENDED if action == "suspend-tenant" else TenantStatus.ACTIVE).value
    await tenant.save()
    state = "已停用" if action == "suspend-tenant" else "已恢复"
    logger.info(f"运维深链执行: {action} tenant={target}")
    return f"租户 {tenant.name} {state}"
