"""本轮的运维授权，以及每一次调用前的复核

## 为什么授权按「容器」而不是按「会话」

沙盒 RPC 回调带回的是 `container_key`，它由宿主在启动容器时生成并经令牌校验，
模型改不了（见 `services/sandbox/credential_guard.py`）。按容器授权意味着：
即使有人拿到了上一轮的对话上下文，只要容器不是那一个、或者那一轮已经结束，
就查不到授权。按会话授权则会在「同一频道连着两轮」时把上一轮的许可留给下一轮。

## 为什么执行前还要复核一次

`open_round` 是在回合开始时判的，而沙盒里的代码可以在**这一轮运行期间**被模型反复调用。
判定依据的几样东西都可能在回合中途变掉：平台方在后台把工具关了、把某人移出名单、
把实例划给租户。授权一旦签发就不再重判，等于「开回合时开着，这一轮就永远开着」。
因此每次执行前重读配置并重查实例归属（`authorize_call`），任何一条不过就拒绝。

## 关着时不查库

总开关关着时 `open_round` 直接返回 `None`，一次数据库都不碰：存量部署（开关默认关）
的每条消息、每个回合的行为必须与改造前逐字一致，多一次查询就是多一个故障点。

## 有效期

10 分钟。它比正常回合长得多（模型写代码、跑代码都在这个窗口里），又比「永远有效」
短得多——进程被卡住、回合没收尾（`close_round` 没跑到）时，授权不会长期留在内存里。

## 写操作的次数上限

每轮上限记在授权上（`writes_done`），随授权一起消失。每人每小时上限只能记在进程内：
一张 `(adapter_key, operator) -> 最近一次写操作时刻` 的滑动窗口表，进程重启即清零。
不用库的理由是本批不加表、不生成迁移；重启清零也是可接受的——重启本身已经打断了
正在进行的对话，且上限防的是「一轮里被诱导连续动手」，不是跨天的配额。

计数发生在**放行时**而不是执行成功后：上限限制的是一轮能动多少次手，执行失败的尝试
同样占一次机会。`ops_tools_off` 例外——它只会缩小权限，因此不计数，读过外部内容之后
也照样放行。

## 「读过外部内容」有两种来源

- `tool`：本轮调用的方法返回了外部内容（`routers/rpc.py`）。运维人员再单独说一句，
  下一轮就能写——这是设计稿定下的流程。
- `context`：回合开始时渲染进上下文的历史里本来就有外部内容（名单外的人的发言、推送进来的
  正文，见 `context_guard.py`）。再说一句没有用，那些消息还在上下文里；拒绝时给模型的说明不同。

为了区分「推送进来的」与「之前某个运维回合里运维人员要求读的工具结果」，进程内记下每个频道
最近几轮运维授权回合的起止时刻（墙钟），进程重启即清零——清零之后旧的工具结果一律按推送算，
只会让写操作更难。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, FrozenSet, Optional, Tuple

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import NON_DISPATCHABLE_STATUSES, TOMBSTONE_STATUS, DBAdapterInstance

from .policy import (
    OpsAgentPolicy,
    RoundFacts,
    evaluate_round,
    is_operator,
    private_counterpart_from_channel,
)
from .triggers import round_trigger_book

logger = get_sub_logger("ops_agent")

# 授权的有效期（秒）
ROUND_TTL_SECONDS = 600
# 运维授权回合起止时刻的保留时长与每个频道的条数上限
ROUND_SPAN_KEEP_SECONDS = 7 * 86400
ROUND_SPAN_MAX_PER_CHAT = 200

TAINT_TOOL = "tool"
TAINT_CONTEXT = "context"

# 墙钟：回合起止要和消息的发送时间戳比，单调时钟比不了。供测试注入
_wall_clock: Callable[[], float] = time.time
# 每小时写操作上限的滑动窗口（秒）
WRITE_LIMIT_WINDOW_SECONDS = 3600


@dataclass
class OpsRoundGrant:
    """一个回合的运维授权

    Attributes:
        round_id: 本轮标识，只用于把审计日志里的几条记录串起来
        container_key: 沙盒容器键，授权的键
        chat_key: 会话标识；执行时请求带来的 `chat_key` 必须与它一致
        adapter_key: 频道所属适配器
        instance_key: 频道所属实例
        operators: 本轮认可的运维人员账号
        opened_at: 签发时刻（单调时钟）
        writes_done: 本轮已放行的写操作数（失败的尝试也算）
        tainted: 本轮是否读过外部内容；读过之后写操作一律拒绝（`ops_tools_off` 例外）
        is_group: 群聊还是私聊，复核地点时用
        private_counterpart: 私聊对方账号，复核「对方此刻仍在名单里」时用
        opened_wall: 签发时刻（墙钟），与消息发送时间戳比较用
        taint_source: `tainted` 的来源，`tool` 或 `context`（见模块文档）
    """

    round_id: str
    container_key: str
    chat_key: str
    adapter_key: str
    instance_key: str
    operators: FrozenSet[str]
    opened_at: float = field(default_factory=time.monotonic)
    writes_done: int = 0
    tainted: bool = False
    is_group: bool = False
    private_counterpart: str = ""
    opened_wall: float = field(default_factory=lambda: _wall_clock())
    taint_source: str = ""

    @property
    def expires_at(self) -> float:
        return self.opened_at + ROUND_TTL_SECONDS


_grants: Dict[str, OpsRoundGrant] = {}
_round_seq = 0
# 每个频道最近几轮运维授权回合的 `(签发, 收回)` 墙钟时刻
_round_spans: Dict[str, Deque[Tuple[float, float]]] = {}

# 进程内的每小时写操作记录：`(adapter_key, operator) -> 最近的写操作时刻`
_write_events: Dict[Tuple[str, str], Deque[float]] = {}
# 供测试注入的单调时钟。默认就是 `time.monotonic`，与授权过期用同一个时间源
_write_clock: Callable[[], float] = time.monotonic


def reset_write_counters() -> None:
    """清空每小时写操作记录（进程内状态，供测试与关停使用）"""
    _write_events.clear()


def _recent_write_count(adapter_key: str, operator: str, now: float) -> int:
    """某人在过去一小时内被放行的写操作数（顺手丢掉过窗的条目）"""
    key = (adapter_key, operator)
    events = _write_events.get(key)
    if events is None:
        return 0
    cutoff = now - WRITE_LIMIT_WINDOW_SECONDS
    while events and events[0] <= cutoff:
        events.popleft()
    if not events:
        _write_events.pop(key, None)
        return 0
    return len(events)


def _record_write(adapter_key: str, operator: str, now: float) -> None:
    _write_events.setdefault((adapter_key, operator), deque()).append(now)


def _next_round_id(chat_key: str) -> str:
    global _round_seq
    _round_seq += 1
    return f"{_round_seq:x}-{chat_key}"


def reset_round_grants() -> None:
    """清空全部授权、触发记录与写操作计数（进程内状态，供测试与关停使用）"""
    _grants.clear()
    _round_spans.clear()
    round_trigger_book.clear()
    reset_write_counters()


def _remember_round_span(grant: OpsRoundGrant) -> None:
    """记下一轮运维授权回合的起止时刻，顺手丢掉过期的"""
    now = _wall_clock()
    spans = _round_spans.setdefault(grant.chat_key, deque(maxlen=ROUND_SPAN_MAX_PER_CHAT))
    spans.append((grant.opened_wall, now))
    cutoff = now - ROUND_SPAN_KEEP_SECONDS
    while spans and spans[0][1] < cutoff:
        spans.popleft()


def inside_ops_round(chat_key: str, timestamp: float) -> bool:
    """这个时刻是否落在本频道某一个已经结束的运维授权回合里

    消息的发送时间戳是秒级的，两端各放宽 1 秒。
    """
    for start, end in _round_spans.get(chat_key, ()):
        if start - 1 <= timestamp <= end + 1:
            return True
    return False


def get(container_key: str) -> Optional[OpsRoundGrant]:
    """取这个容器的授权；过期就顺手清掉"""
    grant = _grants.get(container_key)
    if grant is None:
        return None
    if time.monotonic() - grant.opened_at > ROUND_TTL_SECONDS:
        _grants.pop(container_key, None)
        return None
    return grant


async def _instance_platform_owned(adapter_key: str, instance_key: str) -> bool:
    """实例是否归平台方且当前可用

    「归平台方」的判据是 `tenant_id IS NULL`，与总后台的展示口径一致。可用性直接复用
    `DBAdapterInstance.is_instance_active`（实例开关 + 所属租户开关），不再另写一遍。
    """
    if not adapter_key or not instance_key:
        return False
    instance = (
        await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key)
        .exclude(status=TOMBSTONE_STATUS)
        .order_by("id")
        .first()
    )
    if instance is None:
        return False
    if instance.tenant_id is not None:
        return False
    if not instance.enabled or str(instance.status or "").strip().lower() in NON_DISPATCHABLE_STATUSES:
        return False
    return await DBAdapterInstance.is_instance_active(adapter_key, instance_key)


async def build_round_facts(db_chat_channel) -> RoundFacts:
    """由频道行算出判定所需的事实"""
    adapter_key = str(db_chat_channel.adapter_key or "")
    instance_key = str(db_chat_channel.instance_key or "")
    channel_type = str(db_chat_channel.channel_type or "").strip().lower()
    channel_id = str(db_chat_channel.channel_id or "")
    is_private = channel_type == "private"
    return RoundFacts(
        adapter_key=adapter_key,
        instance_key=instance_key,
        chat_key=str(db_chat_channel.chat_key or ""),
        is_group=channel_type == "group",
        is_private=is_private,
        private_counterpart=private_counterpart_from_channel(adapter_key, channel_id) if is_private else "",
        instance_platform_owned=await _instance_platform_owned(adapter_key, instance_key),
    )


def _container_key(chat_key: str) -> str:
    """会话标识 -> 沙盒容器键

    生成规则只有 `services/sandbox/runner.py:build_container_key` 一处（注释里写明了理由），
    这里必须复用它而不是自己拼。函数内导入：`runner` 会拖进消息服务，模块级导入会成环。
    """
    from nekro_agent.services.sandbox.runner import build_container_key

    return build_container_key(chat_key)


async def open_round(db_chat_channel) -> Optional[OpsRoundGrant]:
    """回合开始时判定并签发授权

    触发集合**不再由这里取走**：`run_agent` 在回合开始时已经用 `begin_round` 整批取成
    快照，D6 与 SSH 网关共用那一份。这里只读快照；没经过 `run_agent` 的调用（例如测试
    直接调进来）读不到快照时，当场 `begin_round` 取一次，行为与改造前一致。

    总开关关着时直接返回 `None`，不查库。两家都关着时仍把本子里残留的触发丢掉
    （纯内存操作），免得开关下次打开时它们作为过期条目把第一轮拒掉；网关开着时不能
    取——快照已经取过，此刻本子里的是回合开始之后到达的、属于下一轮的触发。
    """
    chat_key = str(db_chat_channel.chat_key or "")
    if not bool(config.OPS_AGENT_TOOLS_ENABLED):
        if not bool(config.SSH_GATEWAY_ENABLED):
            round_trigger_book.take(chat_key)
        return None

    policy = OpsAgentPolicy.from_config(config)
    batch = round_trigger_book.current_batch(chat_key)
    if batch is None:
        batch = round_trigger_book.begin_round(chat_key)
    facts = await build_round_facts(db_chat_channel)
    result = evaluate_round(policy, facts, batch)

    container_key = _container_key(chat_key)
    if not result.allowed:
        _grants.pop(container_key, None)
        # 「名单里的人触发了、但这一轮没通过」是运维人员会想知道的那一类：
        # 多半是他把工具用在了租户的群 / 没配进名单的群里。其余拒绝只记 debug，
        # 否则群里每说一句话都要写一行日志
        if any(trigger.operator_trusted for trigger in batch.triggers):
            logger.info(
                f"[ops_agent] 运维人员触发但本轮不给工具: chat_key={chat_key} reason={result.decision.value} "
                f"instance_platform_owned={facts.instance_platform_owned}",
            )
        else:
            logger.debug(f"[ops_agent] 本轮不给工具: chat_key={chat_key} reason={result.decision.value}")
        return None

    grant = OpsRoundGrant(
        round_id=_next_round_id(chat_key),
        container_key=container_key,
        chat_key=chat_key,
        adapter_key=facts.adapter_key,
        instance_key=facts.instance_key,
        operators=result.operators,
        is_group=facts.is_group,
        private_counterpart=facts.private_counterpart,
    )
    _grants[container_key] = grant
    logger.info(
        f"[ops_agent] 本轮授权已签发: round={grant.round_id} chat_key={chat_key} "
        f"operators={len(grant.operators)}",
    )
    return grant


def close_round(container_key: str) -> None:
    """回合结束时撤销授权

    直接按容器键删：收尾一定发生在同一容器的下一轮 `open_round` 之前（防抖重跑的下一轮
    在 `finally` 里才启动），所以不存在「删掉新一轮」的时序。过期条目本来也要删。
    """
    grant = _grants.pop(container_key, None)
    if grant is not None:
        _remember_round_span(grant)


def taint_grant(grant: OpsRoundGrant, source: str = TAINT_TOOL) -> None:
    """把一个授权标成读过外部内容；`context` 一旦置上就不再被 `tool` 盖掉"""
    if not grant.tainted:
        logger.info(f"[ops_agent] 本轮读入了外部内容，后续写操作将被拒绝: round={grant.round_id} source={source}")
    grant.tainted = True
    if grant.taint_source != TAINT_CONTEXT:
        grant.taint_source = source


def mark_tainted(container_key: str, source: str = TAINT_TOOL) -> None:
    """标记本轮读过外部内容；没有授权时什么都不做"""
    grant = _grants.get(container_key)
    if grant is None:
        return
    taint_grant(grant, source)


async def authorize_call(
    container_key: str,
    chat_key: str,
    method_name: str,
    *,
    is_write: bool = False,
) -> Tuple[bool, str]:
    """执行前复核一次

    Args:
        container_key: 回调带回的容器键
        chat_key: 回调带来的会话标识
        method_name: 方法名（用于写操作豁免判定与日志）
        is_write: 是不是写操作；是的话在五条判定之后还要过 `tainted` 与两道次数上限

    Returns:
        `(是否放行, 拒绝理由)`；放行时理由为空串
    """
    if not bool(config.OPS_AGENT_TOOLS_ENABLED):
        return False, "disabled"

    grant = get(container_key)
    if grant is None:
        return False, "no_grant"
    if grant.chat_key != chat_key:
        return False, "chat_key_mismatch"

    policy = OpsAgentPolicy.from_config(config)
    if grant.is_group:
        if grant.chat_key not in policy.groups:
            return False, "not_ops_group"
    elif grant.private_counterpart:
        if not policy.private_enabled:
            return False, "private_disabled"
        if not is_operator(policy, grant.adapter_key, grant.private_counterpart):
            return False, "not_operator_private"
    else:
        return False, "unsupported_channel"

    # 名单重读：把某人移出名单必须热生效，不能等他那一轮跑完
    if not grant.operators or any(not is_operator(policy, grant.adapter_key, item) for item in grant.operators):
        return False, "operator_removed"

    if not await _instance_platform_owned(grant.adapter_key, grant.instance_key):
        return False, "tenant_instance"

    if is_write:
        # 函数内导入：`tools` 依赖本模块的 `get`，模块级导入会成环
        from .tools import OPS_LIMIT_EXEMPT_TOOLS

        if method_name in OPS_LIMIT_EXEMPT_TOOLS:
            # 关掉只会缩小权限：不计数，读过外部内容之后也放行
            return True, ""
        if grant.tainted:
            return False, "tainted_context" if grant.taint_source == TAINT_CONTEXT else "tainted"

        per_round = int(config.OPS_AGENT_WRITE_LIMIT_PER_ROUND)
        if per_round <= 0 or grant.writes_done >= per_round:
            return False, "round_limit"

        per_hour = int(config.OPS_AGENT_WRITE_LIMIT_PER_HOUR)
        now = _write_clock()
        if per_hour <= 0 or any(
            _recent_write_count(grant.adapter_key, operator, now) >= per_hour for operator in grant.operators
        ):
            return False, "hourly_limit"

        # 放行即计数：上限限制的是一轮能动多少次手，执行失败的尝试也算一次
        grant.writes_done += 1
        for operator in grant.operators:
            _record_write(grant.adapter_key, operator, now)

    return True, ""


def current_grant_for_chat(chat_key: str) -> Optional[OpsRoundGrant]:
    """按会话标识取当前授权（只给提示词渲染与排查用）"""
    return get(_container_key(chat_key))


__all__ = [
    "OpsRoundGrant",
    "ROUND_SPAN_KEEP_SECONDS",
    "ROUND_TTL_SECONDS",
    "TAINT_CONTEXT",
    "TAINT_TOOL",
    "WRITE_LIMIT_WINDOW_SECONDS",
    "authorize_call",
    "build_round_facts",
    "close_round",
    "current_grant_for_chat",
    "get",
    "inside_ops_round",
    "mark_tainted",
    "open_round",
    "reset_round_grants",
    "reset_write_counters",
    "taint_grant",
]
