"""内置命令 - 运维 Bot: na_ops

## 为什么它自带一套权限判定

`BaseCommand.check_permission()` 的默认实现依赖两样东西，运维通道**都不能用**：

1. `command_manager.get_command_permission()` —— 命令权限可以被系统级/频道级覆盖，
   也就是说 `na_ops` 能被改成 `PUBLIC`；
2. `context.is_super_user` —— 它有两个"伪来源"会无条件置真：
   模型的 Tool-Use 调用（`services/command/tool_export.py`）与
   WebUI 的 `/commands/webui-execute`（仅要求 `Role.Admin`，租户管理员正好够）。

所以这里整个覆写 `check_permission()`，只认 `services/ops_bot/policy.py` 的判定。

## `internal=True` 的作用

命令注册表把 `internal` 的命令排除在 `list_all_commands()` 之外，于是它既不会出现在
`na_help` 里，也不会被 `AgentToolExporter.export_tools()` 导出给模型。
这不是安全边界（安全边界是上面的 `check_permission`），是**减少暴露面**：
模型连这条命令存在都不知道，就少了一整类提示词注入的尝试空间。
"""

from collections.abc import AsyncIterator
from typing import Annotated

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.i18n import i18n_text
from nekro_agent.services.command.base import BaseCommand, CommandMetadata, CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import Arg, CommandExecutionContext, CommandResponse
from nekro_agent.services.ops_agent.audit import close_ops_event, open_ops_event
from nekro_agent.services.ops_agent.receipt import format_receipt
from nekro_agent.services.ops_bot.actions import OPS_ACTIONS, OpsActionKind, execute_write_action, parse_ops_command
from nekro_agent.services.ops_bot.policy import (
    OpsAccessDecision,
    OpsBotPolicy,
    evaluate_ops_access,
    loaded_im_adapter_keys,
)
from nekro_agent.services.ops_bot.reports import READ_RENDERERS
from nekro_agent.services.user.super_users import is_trusted_super_user_source

logger = get_sub_logger("ops_bot.command")

DENY_MESSAGES = {
    OpsAccessDecision.DISABLED: "运维 Bot 未启用",
    OpsAccessDecision.NOT_CONFIGURED: "运维 Bot 配置不完整（需要适配器、实例、频道与运维人员白名单）",
    OpsAccessDecision.NON_IM_ORIGIN: "运维指令只能从运维频道的真实消息发起",
    OpsAccessDecision.WRONG_CHANNEL: "当前频道不是运维频道",
    OpsAccessDecision.NOT_OPERATOR: "你不在运维人员白名单中",
    OpsAccessDecision.UNTRUSTED_SOURCE: "运维频道所在的实例不是平台方可信的来源（见启动日志里的超级用户来源告警）",
}


def render_help() -> str:
    """按读/写分组列出全部指令"""
    read_lines = [f"  {spec.usage} - {spec.summary}" for spec in OPS_ACTIONS.values() if spec.kind is OpsActionKind.READ]
    write_lines = [f"  {spec.usage} - {spec.summary}" for spec in OPS_ACTIONS.values() if spec.kind is OpsActionKind.WRITE]
    return "\n".join(
        [
            "[运维 Bot]",
            "只读指令（直接返回结果）:",
            *read_lines,
            "写操作（直接执行，结果在本频道回执）:",
            *write_lines,
        ],
    )


class OpsBotCommand(BaseCommand):
    """运维 Bot 指令入口"""

    @property
    def metadata(self) -> CommandMetadata:
        return CommandMetadata(
            name="na_ops",
            aliases=[],
            description="运维 Bot：查询总后台状态，写操作直接执行并在本频道回执",
            i18n_description=i18n_text(
                zh_CN="运维 Bot：查询总后台状态，写操作直接执行并在本频道回执",
                en_US="Ops Bot: query console status; write operations execute immediately and reply in this channel",
            ),
            usage="na_ops <指令> [目标]",
            # 名义权限设为最高档只是为了在 WebUI 的命令列表里如实展示，
            # 真正的判定在 check_permission 里，与这里的取值无关
            permission=CommandPermission.SUPER_USER,
            category="运维",
            i18n_category=i18n_text(zh_CN="运维", en_US="Operations"),
            internal=True,
            params_schema=self._auto_params_schema(),
        )

    async def check_permission(self, context: CommandExecutionContext) -> tuple[bool, str | None]:
        """只认运维通道判定，完全绕开可被覆盖的命令权限层"""
        policy = OpsBotPolicy.from_config(config)
        decision = evaluate_ops_access(
            policy,
            adapter_key=context.adapter_key,
            chat_key=context.chat_key,
            user_id=context.user_id,
            im_adapter_keys=loaded_im_adapter_keys(),
        )
        # 第五道闸门：白名单只比用户 ID，运维频道所在实例上报的身份还得是平台方能担保的
        if decision is OpsAccessDecision.ALLOWED and not await is_trusted_super_user_source(
            policy.adapter_key,
            policy.instance_key,
        ):
            decision = OpsAccessDecision.UNTRUSTED_SOURCE
        if decision is OpsAccessDecision.ALLOWED:
            return True, None

        logger.warning(
            f"运维指令被拒绝: reason={decision.value} adapter={context.adapter_key} "
            f"chat_key={context.chat_key} user={context.user_id}",
        )
        return False, DENY_MESSAGES[decision]

    async def execute(
        self,
        context: CommandExecutionContext,
        raw_text: Annotated[str, Arg("指令与目标", positional=True, greedy=True)] = "",
    ) -> AsyncIterator[CommandResponse]:
        parsed = parse_ops_command(raw_text)
        if parsed.error:
            yield CmdCtl.failed(f"{parsed.error}\n\n{render_help()}")
            return
        if parsed.is_help:
            yield CmdCtl.success(render_help())
            return

        spec = OPS_ACTIONS[parsed.action]
        if spec.kind is OpsActionKind.READ:
            logger.info(f"运维只读指令: action={parsed.action} user={context.user_id}")
            yield CmdCtl.success(await READ_RENDERERS[parsed.action](parsed.target))
            return

        # 写操作直接执行，结果就是回执正文；命令自己的回复即回执，不再另发一条。
        # 与 Agent 工具同一条规矩：执行前先记一行，记不进去就不执行。
        policy = OpsBotPolicy.from_config(config)
        event_id = await open_ops_event(
            grant=None,
            tool=f"na_ops:{parsed.action}",
            args={"target": parsed.target},
            operator=context.user_id,
            adapter_key=policy.adapter_key,
            instance_key=policy.instance_key,
            chat_key=context.chat_key,
            source="na_ops",
        )
        if event_id is None:
            yield CmdCtl.failed("运维审计暂时写不进去，这次没有执行，请稍后再试或回后台操作。")
            return

        try:
            outcome = await execute_write_action(parsed.action, parsed.target)
        except Exception as exc:  # noqa: BLE001 - 异常原文不上聊天，也不进审计
            await close_ops_event(event_id, outcome="failed", error_type=type(exc).__name__)
            yield CmdCtl.failed("运维操作执行失败，请稍后重试或回后台操作。")
            return

        receipt = format_receipt(outcome, [context.user_id], record_id=event_id)
        await close_ops_event(
            event_id,
            outcome="executed" if outcome.ok else "failed",
            reason="" if outcome.ok else outcome.error_category,
            summary=receipt.removeprefix("【运维】"),
        )
        yield CmdCtl.success(receipt) if outcome.ok else CmdCtl.failed(receipt)
