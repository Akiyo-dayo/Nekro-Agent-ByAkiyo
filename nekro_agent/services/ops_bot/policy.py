"""运维 Bot 的受理判定

本模块是整个运维通道**唯一**的准入闸门，且刻意写成不依赖数据库、不依赖 IO 的纯函数，
这样每一条拒绝理由都能被表驱动测试覆盖。

## 四道闸门缺一不可

1. **全局开关**：默认关闭。
2. **来源必须是真实 IM 适配器**。命令系统里存在两个"伪来源"，它们都会把
   `is_super_user` 置成 True：
   - `adapter_key="agent"`（`services/command/tool_export.py`）—— **模型**调用命令，
     也就是说不挡住的话，任意频道里的一句提示词就能让 AI 自己去操作总后台；
   - `adapter_key="webui"`（`routers/commands.py` 的 `/commands/webui-execute`）——
     该端点只要求 `Role.Admin`，而租户管理员的上限恰好就是 `Role.Admin`
     （见 `services/user/tenant_perm.py` 的 `TENANT_MAX_PERM_LEVEL`）。
   因此这里用**白名单**而不是黑名单：来源必须是当前真实加载的适配器之一。
3. **频道必须精确等于配置的运维频道**（适配器 + 实例 + 频道三段全等）。
   不做前缀匹配、不做"属于该实例即可"，避免运维实例被拉进别的群后在那边也生效。
4. **发言人必须在运维人员白名单里**。这是"权限主体是人不是实例"的落点。

第五道在本模块之外：运维频道所在的实例上报的发送者身份必须是平台方能担保的
（`services/user/super_users.py` 的 `is_trusted_super_user_source`，与聊天命令的超级用户同一套来源判定）。
白名单只比用户 ID，而 SSE 的 `from_id`、租户自运行的协议端上报的号码都能填成白名单里的值。
它要查库，所以放在 `OpsBotCommand.check_permission`，本模块保持纯函数。

## 为什么不复用 `CommandPermission.SUPER_USER`

命令系统的权限是**可被覆盖的**：`CommandManager.set_command_permission()`
支持把任意命令在任意频道降级成 `PUBLIC`，而且 `config.SUPER_USERS` 是一份全局列表，
在任何实例的任何频道都成立。运维通道不能建立在这两者之上，
所以 `OpsBotCommand.check_permission()` 直接调用本模块，绕开那一层。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Collection, FrozenSet

if TYPE_CHECKING:
    from nekro_agent.core.config import CoreConfig


class OpsAccessDecision(str, Enum):
    """受理判定结果

    除 `ALLOWED` 外都是拒绝，取值同时作为日志与拒绝文案的索引键。
    """

    ALLOWED = "allowed"
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    NON_IM_ORIGIN = "non_im_origin"
    WRONG_CHANNEL = "wrong_channel"
    NOT_OPERATOR = "not_operator"
    UNTRUSTED_SOURCE = "untrusted_source"
    """运维频道所在实例的发送者身份不是平台方能担保的（第五道闸门，判定不在本模块）"""


@dataclass(frozen=True)
class OpsBotPolicy:
    """运维通道的生效配置快照

    只从**全局**配置构造。运维相关的配置项刻意不标记 `overridable`，
    因此它们不会出现在适配器/实例/频道的覆盖模型里
    （`core/overridable_config.py` 只收集 `overridable=True` 的字段），
    租户无法在自己的频道里把运维 Bot 打开。
    """

    enabled: bool
    adapter_key: str
    instance_key: str
    channel_id: str
    operators: FrozenSet[str]
    deeplink_ttl_seconds: int
    console_base_url: str

    @classmethod
    def from_config(cls, config: "CoreConfig") -> "OpsBotPolicy":
        """从全局配置构造快照，顺带做空白清洗"""
        return cls(
            enabled=bool(config.OPS_BOT_ENABLED),
            adapter_key=config.OPS_BOT_ADAPTER_KEY.strip(),
            instance_key=config.OPS_BOT_INSTANCE_KEY.strip(),
            channel_id=config.OPS_BOT_CHANNEL_ID.strip(),
            operators=frozenset(str(item).strip() for item in config.OPS_BOT_OPERATORS if str(item).strip()),
            deeplink_ttl_seconds=int(config.OPS_BOT_DEEPLINK_TTL_SECONDS),
            console_base_url=config.OPS_BOT_CONSOLE_BASE_URL.strip().rstrip("/"),
        )

    @property
    def is_configured(self) -> bool:
        """配置是否完整到足以判定"""
        return bool(self.adapter_key and self.instance_key and self.channel_id and self.operators)

    @property
    def ops_chat_key(self) -> str:
        """运维频道的完整会话标识

        与 `adapters/onebot_v11/core/channel.py:build_chat_key()` 同构，
        但不绑定具体适配器：`{adapter_key}-{instance_key}-{channel_id}`。
        """
        return f"{self.adapter_key}-{self.instance_key}-{self.channel_id}"


def evaluate_ops_access(
    policy: OpsBotPolicy,
    *,
    adapter_key: str,
    chat_key: str,
    user_id: str,
    im_adapter_keys: Collection[str],
) -> OpsAccessDecision:
    """判定一条指令是否可以被运维通道受理

    Args:
        policy: 全局配置快照
        adapter_key: 命令上下文声明的来源标识
        chat_key: 命令上下文的会话标识
        user_id: 发言人在该平台的账号标识
        im_adapter_keys: 当前真实加载的适配器标识集合，用于识别伪来源

    Returns:
        `OpsAccessDecision.ALLOWED` 表示放行，其余取值即拒绝理由
    """
    if not policy.enabled:
        return OpsAccessDecision.DISABLED
    if not policy.is_configured:
        return OpsAccessDecision.NOT_CONFIGURED
    if adapter_key != policy.adapter_key or adapter_key not in im_adapter_keys:
        return OpsAccessDecision.NON_IM_ORIGIN
    if chat_key != policy.ops_chat_key:
        return OpsAccessDecision.WRONG_CHANNEL
    if str(user_id).strip() not in policy.operators:
        return OpsAccessDecision.NOT_OPERATOR
    return OpsAccessDecision.ALLOWED


def loaded_im_adapter_keys() -> FrozenSet[str]:
    """当前真实加载的适配器标识

    与判定逻辑分开，是为了让 `evaluate_ops_access` 保持纯函数、可被表驱动测试。
    """
    from nekro_agent.adapters import loaded_adapters

    return frozenset(loaded_adapters.keys())
