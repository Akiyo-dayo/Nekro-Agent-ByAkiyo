"""运维 Bot：把总后台的只读观测能力接到一个 IM 频道里

## 它解决什么问题

平台方（你）手里可能有几十个子实例。要知道"谁掉线了""哪个租户异常"，
只能打开总后台逐页翻。运维 Bot 把这些**只读**信息接到一个 QQ 群/私聊里，
随手一条指令就能看到。

## 权限主体是「人」，不是「实例」

一个自然的做法是"指定某个实例拥有管理总后台的能力"。**这是错的**：
实例的身份是一条 WS 连接，而在 IM 里跟 Bot 说话的是**人**。
按实例授权等于那个群里所有人（包括被拉进群的任意陌生人）都能指挥它管后台。

因此授权模型是：**运维人员白名单（账号列表） + 指定的运维频道**，
两者同时满足才受理。实例只是通道，不是权限主体。见 `policy.py`。

## 读写分离

- **读操作**直接在 IM 里返回结果（实例在线状态、租户概览、近期错误…）
- **写操作**在 IM 里**不执行**，只回一条带签名、短时效、一次性的深链，
  点进去在总后台里看清楚要做什么再确认

理由：IM 消息可以被伪造、被转发、被撤回后重放，且没有二次确认界面。
这与本项目既有的「合并转发消息伪造防护」（`nekro_agent/tools/untrusted_content.py`）
是同一套威胁模型——**只要内容能被第三方构造，就不能直接拿它触发高危动作**。
高危动作必须回到有会话认证的后台，由 `Role.Super` 的平台方账号亲自点一次。

## 为什么是核心服务而不是插件

1. 插件的启用状态与配置对**租户管理员**可见可改（`plugin_activation`、插件配置页），
   把运维能力放进插件等于给租户开了一条自助开关。
2. 插件可以被热重载、卸载、从市场覆盖安装；运维通道的鉴权逻辑不应该有这种生命周期。
3. 写操作需要复用 `services/console/` 与 `routers/console/` 的落地端点，
   本来就在核心层。

## 与多租户的关系

运维 Bot 是**平台方专属**。租户不能有自己的运维 Bot——那等于给租户一条
绕过总后台权限体系的通道。落地端点一律走 `require_platform_owner()`。
"""

from .actions import (
    OPS_ACTIONS,
    OpsActionKind,
    OpsActionSpec,
    OpsCommandInput,
    execute_write_action,
    parse_ops_command,
    resolve_action,
)
from .deeplink import (
    OpsDeepLinkError,
    OpsDeepLinkPayload,
    OpsDeepLinkReason,
    consume_ops_deeplink,
    issue_ops_deeplink,
    peek_ops_deeplink,
)
from .policy import (
    OpsAccessDecision,
    OpsBotPolicy,
    evaluate_ops_access,
)

__all__ = [
    "OPS_ACTIONS",
    "OpsAccessDecision",
    "OpsActionKind",
    "OpsActionSpec",
    "OpsBotPolicy",
    "OpsCommandInput",
    "OpsDeepLinkError",
    "OpsDeepLinkPayload",
    "OpsDeepLinkReason",
    "consume_ops_deeplink",
    "evaluate_ops_access",
    "execute_write_action",
    "issue_ops_deeplink",
    "parse_ops_command",
    "peek_ops_deeplink",
    "resolve_action",
]
