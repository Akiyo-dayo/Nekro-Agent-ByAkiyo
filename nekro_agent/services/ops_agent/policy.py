"""运维工具的准入判定：配置快照 + 纯函数

本模块是整个运维工具通道**唯一**的准入闸门，且刻意写成不依赖数据库、不依赖 IO 的纯函数，
这样每一条拒绝理由都能被表驱动测试覆盖。查库的部分（实例归属、频道类型）在
`round_grant.py` 里先算成 `RoundFacts` 再交给这里。

## 判定顺序与理由

1. 总开关关着 → `disabled`；
2. 频道所在实例不归平台方（或已停用）→ `tenant_instance`；
3. 地点：群必须在运维群集合里；私聊要私聊开关开着且对方在名单里；其他类型一律不支持；
4. 触发集合为空 → `no_trigger`；
5. **任何一条**触发不是入站人类消息 → `non_human_trigger`；
6. **任何一条**触发的发送者不在名单里或来源不可信 → `untrusted_trigger`。

第 6 条在判定里做两次：`RoundTrigger.operator_trusted` 是入站任务当时算好的结果（名单匹配 ∧
来源可信），这里再按**当前**名单查一次发送者。两处都过才算数——触发记下到回合开始之间
可能隔着几秒防抖，这段时间里名单被改过的话，按"少给一次工具"处理。

第 5、6 条是「每一条」而不是「有一条」：防抖会把同一窗口里的多条触发并成一轮，
只要其中一条来自群里另一个人，这一轮就不能当作运维人员的指令来执行——否则
「和运维人员同一个窗口里插一句」就是一条搭车路径。

触发批次被标记为不完整（有条目因超限或过期被丢掉）时同样落到 `untrusted_trigger`：
被丢掉的那条完全可能来自别人，它照样并进了这一轮。

## 为什么不复用命令系统的超级用户判定

命令权限可被按频道覆盖，而 `SUPER_USERS` 是一份在任何实例的任何频道都成立的全局列表。
运维工具的地点判据（平台方实例 + 指定群 / 私聊）与它不同，所以这里另立一套，
只在「名单匹配」这一步复用 `services/user/super_users.py: matches_super_users`，
来源可信与否由调用方在入站任务里算好（`RoundTrigger.operator_trusted`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, FrozenSet, Sequence

from nekro_agent.services.user.super_users import ONEBOT_ADAPTER_KEY, matches_super_users

from .triggers import TRIGGER_KIND_HUMAN, RoundTrigger, TriggerBatch

if TYPE_CHECKING:
    from nekro_agent.core.config import CoreConfig


class OpsRoundDecision(str, Enum):
    """一轮能否拿到运维工具

    除 `ALLOWED` 外都是拒绝，取值同时作为日志与用例的索引键。
    """

    ALLOWED = "allowed"
    DISABLED = "disabled"
    TENANT_INSTANCE = "tenant_instance"
    NOT_OPS_GROUP = "not_ops_group"
    PRIVATE_DISABLED = "private_disabled"
    NOT_OPERATOR_PRIVATE = "not_operator_private"
    UNSUPPORTED_CHANNEL = "unsupported_channel"
    NO_TRIGGER = "no_trigger"
    NON_HUMAN_TRIGGER = "non_human_trigger"
    UNTRUSTED_TRIGGER = "untrusted_trigger"


@dataclass(frozen=True)
class OpsAgentPolicy:
    """运维工具的生效配置快照

    只从**全局**配置构造。运维工具的配置项刻意不标记 `overridable`，
    因此它们不会出现在实例 / 频道的覆盖模型里，租户无法在自己的频道里把工具打开。

    Attributes:
        enabled: 总开关
        operators: 运维人员名单条目（`SUPER_USERS` 格式，可能来自回落）
        groups: 允许使用工具的群，完整会话标识精确匹配
        private_enabled: 私聊里是否可用
        write_limit_per_round: 单轮写操作上限（本批只定义，写工具在下一批）
        write_limit_per_hour: 每人每小时写操作上限（同上）
    """

    enabled: bool
    operators: tuple[str, ...]
    groups: FrozenSet[str]
    private_enabled: bool
    write_limit_per_round: int
    write_limit_per_hour: int

    @classmethod
    def from_config(cls, config: "CoreConfig") -> "OpsAgentPolicy":
        """从全局配置构造快照，顺带做空白清洗与回落

        回落只在**本组配置为空**时发生：`OPS_AGENT_OPERATORS` 为空沿用运维 Bot 的人员名单，
        `OPS_AGENT_GROUPS` 为空沿用 `/na_ops` 配的那个运维频道。理由是已配好运维 Bot 的部署
        不该为了试用运维工具再抄一遍名单；但一旦本组填了任何一条，就以本组为准，
        不做两份名单的并集——否则「把人从运维工具名单里删掉」这件事会静默失效。
        """
        operators = tuple(str(item).strip() for item in config.OPS_AGENT_OPERATORS if str(item).strip())
        if not operators:
            operators = tuple(str(item).strip() for item in config.OPS_BOT_OPERATORS if str(item).strip())

        groups = frozenset(str(item).strip() for item in config.OPS_AGENT_GROUPS if str(item).strip())
        if not groups:
            # 运维 Bot 的频道配置不完整（缺适配器 / 实例 / 频道号 / 名单）时无从推出会话标识，
            # 此时不回落：`ops_chat_key` 会拼出 `--` 这种谁也匹配不上的值，落进集合反而更危险
            from nekro_agent.services.ops_bot.policy import OpsBotPolicy

            ops_bot = OpsBotPolicy.from_config(config)
            if ops_bot.is_configured:
                groups = frozenset({ops_bot.ops_chat_key})

        return cls(
            enabled=bool(config.OPS_AGENT_TOOLS_ENABLED),
            operators=operators,
            groups=groups,
            private_enabled=bool(config.OPS_AGENT_PRIVATE_ENABLED),
            write_limit_per_round=max(0, int(config.OPS_AGENT_WRITE_LIMIT_PER_ROUND)),
            write_limit_per_hour=max(0, int(config.OPS_AGENT_WRITE_LIMIT_PER_HOUR)),
        )


def is_operator(policy: OpsAgentPolicy, adapter_key: str, user_id: str) -> bool:
    """只做名单匹配，不判来源

    展示、日志与「这个人值不值得记一条 info」用它；判定权限一律看
    `RoundTrigger.operator_trusted`（名单匹配 ∧ 来源可信）。
    """
    return matches_super_users(adapter_key, user_id, list(policy.operators))


def private_counterpart_from_channel(adapter_key: str, channel_id: str) -> str:
    """从私聊频道标识确定地取出对方账号；取不到返回空串

    不要和 `services/memory/persons.py: resolve_private_counterpart` 混用：那个是按历史消息推断的，
    只能用于记忆归属，不能用于授权。

    只有 OneBot V11 的 `private_<QQ>` 是确定的。其他适配器（SSE、邮件、网页聊天）
    的对方标识可以由调用方填写，用它授权等于把名单交给攻击者，因此一律返回空串——
    这些平台上私聊路径本来也走不到「对方是运维人员」这一步。
    """
    if adapter_key != ONEBOT_ADAPTER_KEY or not channel_id.startswith("private_"):
        return ""
    target = channel_id[len("private_") :].strip()
    if not (target.isascii() and target.isdigit()):
        return ""
    return target


@dataclass(frozen=True)
class RoundFacts:
    """一轮的宿主侧事实，全部由调用方查好

    Attributes:
        adapter_key: 频道所属适配器
        instance_key: 频道所属实例
        chat_key: 完整会话标识
        is_group: 是不是群聊
        is_private: 是不是私聊
        private_counterpart: 私聊对方的账号；非 OneBot 私聊或解析不出时为空串
        instance_platform_owned: 实例存在、未绑定租户、不是墓碑、且当前可用
    """

    adapter_key: str
    instance_key: str
    chat_key: str
    is_group: bool = False
    is_private: bool = False
    private_counterpart: str = ""
    instance_platform_owned: bool = False


@dataclass(frozen=True)
class OpsRoundResult:
    """判定结果

    Attributes:
        decision: 判定
        operators: 本轮被认可的运维人员账号集合；只有 `ALLOWED` 时非空
        reason: 给日志看的补充说明（例如「名单里的人在租户的群里触发了」），可为空
    """

    decision: OpsRoundDecision
    operators: FrozenSet[str] = frozenset()
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is OpsRoundDecision.ALLOWED


def evaluate_round(
    policy: OpsAgentPolicy,
    facts: RoundFacts,
    triggers: Sequence[RoundTrigger] | TriggerBatch,
) -> OpsRoundResult:
    """判定这一轮能否拿到运维工具（纯函数）

    Args:
        policy: 配置快照
        facts: 宿主侧事实
        triggers: 本轮的触发集合；传 `TriggerBatch` 时会一并看它的 `incomplete` 标记

    Returns:
        判定结果；`ALLOWED` 时带出本轮认可的运维人员账号集合
    """
    batch_incomplete = isinstance(triggers, TriggerBatch) and triggers.incomplete
    items: Sequence[RoundTrigger] = triggers.triggers if isinstance(triggers, TriggerBatch) else triggers

    if not policy.enabled:
        return OpsRoundResult(OpsRoundDecision.DISABLED)
    if not facts.instance_platform_owned:
        return OpsRoundResult(OpsRoundDecision.TENANT_INSTANCE)

    if facts.is_group:
        if facts.chat_key not in policy.groups:
            return OpsRoundResult(OpsRoundDecision.NOT_OPS_GROUP)
    elif facts.is_private:
        if not policy.private_enabled:
            return OpsRoundResult(OpsRoundDecision.PRIVATE_DISABLED)
        if not facts.private_counterpart or not is_operator(policy, facts.adapter_key, facts.private_counterpart):
            return OpsRoundResult(OpsRoundDecision.NOT_OPERATOR_PRIVATE)
    else:
        return OpsRoundResult(OpsRoundDecision.UNSUPPORTED_CHANNEL)

    if not items:
        return OpsRoundResult(OpsRoundDecision.NO_TRIGGER)
    if any(trigger.kind != TRIGGER_KIND_HUMAN for trigger in items):
        return OpsRoundResult(OpsRoundDecision.NON_HUMAN_TRIGGER)
    # 名单**再查一遍**：`operator_trusted` 是入站任务算好传进来的，这里不重算来源（那要当前连接），
    # 但「发送者此刻仍在名单里」是能就地查的。两处都过才算可信，任何一处判错都不至于放行。
    if batch_incomplete or any(
        not trigger.operator_trusted or not is_operator(policy, facts.adapter_key, trigger.sender_id) for trigger in items
    ):
        return OpsRoundResult(OpsRoundDecision.UNTRUSTED_TRIGGER)

    operators = frozenset(trigger.sender_id for trigger in items if trigger.sender_id)
    if facts.is_private:
        operators = frozenset({facts.private_counterpart})
    if not operators:
        return OpsRoundResult(OpsRoundDecision.UNTRUSTED_TRIGGER)
    return OpsRoundResult(OpsRoundDecision.ALLOWED, operators=operators)


__all__ = [
    "OpsAgentPolicy",
    "OpsRoundDecision",
    "OpsRoundResult",
    "RoundFacts",
    "evaluate_round",
    "is_operator",
    "private_counterpart_from_channel",
]
