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
from nekro_agent.services.ops_bot.actions import OPS_ACTIONS, OpsActionKind, parse_ops_command
from nekro_agent.services.ops_bot.deeplink import issue_ops_deeplink, normalize_ttl_seconds
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

CONSOLE_CONFIRM_PATH = "/console/ops-confirm"


def build_confirm_url(base_url: str, token: str) -> str:
    """拼出确认链接

    未配置后台地址时给相对路径而不是猜一个域名：猜错会把运维人员引到一个
    并不存在的站点上，而相对路径至少能让人知道该去后台的哪一页。
    """
    return f"{base_url}{CONSOLE_CONFIRM_PATH}?token={token}"


def render_help() -> str:
    """按读/写分组列出全部指令"""
    read_lines = [f"  {spec.usage} - {spec.summary}" for spec in OPS_ACTIONS.values() if spec.kind is OpsActionKind.READ]
    write_lines = [f"  {spec.usage} - {spec.summary}" for spec in OPS_ACTIONS.values() if spec.kind is OpsActionKind.WRITE]
    return "\n".join(
        [
            "[运维 Bot]",
            "只读指令（直接返回结果）:",
            *read_lines,
            "写操作（只返回确认链接，需在总后台确认后执行）:",
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
            description="运维 Bot：查询总后台状态，写操作返回确认链接",
            i18n_description=i18n_text(
                zh_CN="运维 Bot：查询总后台状态，写操作返回确认链接",
                en_US="Ops Bot: query console status; write operations return a confirmation link",
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
            yield CmdCtl.success(await READ_RENDERERS[parsed.action]())
            return

        # 写操作在这里**不执行**，只签发一条一次性确认链接
        policy = OpsBotPolicy.from_config(config)
        token, _payload = issue_ops_deeplink(
            action=spec.name,
            target=parsed.target,
            issuer_user_id=context.user_id,
            issuer_chat_key=context.chat_key,
            ttl_seconds=policy.deeplink_ttl_seconds,
        )
        yield CmdCtl.success(
            "\n".join(
                [
                    "[需要在总后台确认]",
                    f"操作: {spec.summary}",
                    f"目标: {parsed.target}",
                    f"有效期: {normalize_ttl_seconds(policy.deeplink_ttl_seconds)} 秒，仅可使用一次",
                    "该操作不会在聊天里执行。请用平台方账号打开下面的链接，看清内容后确认：",
                    build_confirm_url(policy.console_base_url, token),
                ],
            ),
        )
