"""运维指令目录：读写分类、参数约定与写操作落地

## 分类是安全边界，不是排版

`OpsActionKind.READ` 的指令在 IM 里直接出结果；`WRITE` 的指令**直接执行**，
执行后在本频道回一条回执。命令层不判断"这条像不像危险操作"，只查本表——
分类写在数据里，新增一条指令时必须显式声明属于哪一类，漏声明会在注册时就暴露
而不是在线上暴露。

## 写操作为什么不自己实现业务逻辑

每个写操作都落到 `services/ops_agent/writes.py`，与给模型调用的运维工具走同一条执行路径；
`writes.py` 再去调总后台正在用的同一个业务函数。如果运维 Bot 自己再写一遍"停用实例"，
两条路径迟早会分叉，其中一条会漏掉将来新增的副作用（缓存失效、状态广播、审计）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from nekro_agent.services.ops_agent import writes
from nekro_agent.services.ops_agent.text import ident


class OpsActionKind(str, Enum):
    """指令类别

    READ  —— 在 IM 里直接返回结果
    WRITE —— 直接执行，执行后在本频道回执
    """

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class OpsActionSpec:
    """一条运维指令的声明

    Attributes:
        name: 指令名，用户在 IM 里输入的就是它
        kind: 读/写分类
        summary: 一句话说明；回执与帮助文本都用它
        target_label: 需要的目标参数名称；空串表示该指令不接受目标
        takes_target: 接受一个**可选**目标（目前只有 `audit` 的条数）；与 `target_label`
            一起决定用法文本与解析规则
    """

    name: str
    kind: OpsActionKind
    summary: str
    target_label: str = ""
    takes_target: bool = False

    @property
    def requires_target(self) -> bool:
        return bool(self.target_label) and not self.takes_target

    @property
    def usage(self) -> str:
        if self.requires_target:
            return f"{self.name} <{self.target_label}>"
        if self.takes_target:
            return f"{self.name} [{self.target_label}]"
        return self.name


OPS_ACTIONS: Dict[str, OpsActionSpec] = {
    spec.name: spec
    for spec in (
        OpsActionSpec("overview", OpsActionKind.READ, "总览：实例总数 / 在线 / 离线 / 待认领、今日消息量"),
        OpsActionSpec("instances", OpsActionKind.READ, "列出全部实例及其在线状态"),
        OpsActionSpec("health", OpsActionKind.READ, "列出实例探活结果与掉线时长"),
        OpsActionSpec("tenants", OpsActionKind.READ, "列出租户及其状态、名下实例数"),
        OpsActionSpec("errors", OpsActionKind.READ, "最近的错误日志摘要"),
        OpsActionSpec("audit", OpsActionKind.READ, "最近的运维操作记录（默认 10 条，最多 20 条）", "条数", takes_target=True),
        OpsActionSpec("enable-instance", OpsActionKind.WRITE, "启用指定实例，使其恢复收发消息", "实例标识"),
        OpsActionSpec("disable-instance", OpsActionKind.WRITE, "停用指定实例，使其立即停止收发消息", "实例标识"),
        OpsActionSpec("restart-container", OpsActionKind.WRITE, "重启指定实例的协议端容器", "实例标识"),
        OpsActionSpec("suspend-tenant", OpsActionKind.WRITE, "停用指定租户：其成员无法登录，其实例停止收发", "租户 ID"),
        OpsActionSpec("resume-tenant", OpsActionKind.WRITE, "恢复指定租户为正常状态", "租户 ID"),
        OpsActionSpec("tools", OpsActionKind.WRITE, "打开或关闭运维工具；打开只能在后台或用本命令", "on|off"),
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
    if not spec.requires_target and not spec.takes_target and target:
        return OpsCommandInput(error=f"该指令不接受参数，用法: {spec.usage}")
    if spec.takes_target and target and not target.isdigit():
        return OpsCommandInput(error=f"参数必须是数字，用法: {spec.usage}")

    return OpsCommandInput(action=spec.name, target=target)


async def execute_write_action(action: str, target: str) -> writes.WriteOutcome:
    """执行一条写操作

    与运维工具共用 `writes.py`；本函数只做指令名到执行函数的映射。
    调用方（`/na_ops`）负责权限判定、回执与审计。

    Args:
        action: 写操作标识
        target: 操作目标

    Returns:
        结构化结果；成功与失败都由 `WriteOutcome` 表达，不抛业务异常
    """
    spec = resolve_action(action)
    if spec is None or spec.kind is not OpsActionKind.WRITE:
        return writes.WriteOutcome(
            ok=False,
            action_label="运维写操作",
            target_label=ident(target),
            error_category=writes.ERROR_INVALID_ARGUMENT,
        )
    return await writes.execute_na_ops_write(spec.name, target)


__all__ = [
    "OPS_ACTIONS",
    "OpsActionKind",
    "OpsActionSpec",
    "OpsCommandInput",
    "execute_write_action",
    "parse_ops_command",
    "resolve_action",
]
