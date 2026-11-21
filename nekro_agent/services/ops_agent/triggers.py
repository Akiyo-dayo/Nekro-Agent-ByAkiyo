"""一轮对话的触发集合

## 为什么不能只看「最后一条消息」

`services/message_service.py` 按频道防抖：窗口内多次触发并成一轮，跑的是**最后一次**
调度带进来的消息与上下文；回合正在跑时到达的触发只更新待处理消息，等这一轮结束后
`_run_chat_agent_task` 的 `finally` 会**用旧 ctx 重跑**。因此「这一轮是谁触发的」在
回合真正开始时已经无从还原：`AgentCtx._trigger_db_user` 只记得一个人，而且可能是上一轮的人。

运维工具要的判据是「这一轮的每一条触发都是可信的运维人员」，所以触发必须在**到达时**
就记下来，并在回合开始时整体取走。

## 为什么要标记「不完整」

本子最多留 `MAX_TRIGGERS_PER_CHANNEL` 条、只留 `TRIGGER_TTL_SECONDS` 以内的条目。
一旦因为超限或过期丢掉过任何一条，这一批就不能再当作「全是运维人员触发的」：
被丢掉的那条完全可能来自群里另一个人，而它照样会参与并进这一轮。不完整的批次
按「混进了非运维触发」处理（判定侧见 `policy.evaluate_round`），宁可少给一次工具。

## 为什么两家共用一份、回合开始只取一次

本子原本只服务运维工具（D6），现在 SSH 网关的授权也要按「这一轮是谁叫醒的」判。
两家各取一次是不行的：`take` 会把这一批从本子里删掉，先取的那家拿走全部条目，后取
的那家只能拿到空批，而空批在两边都等于「这一轮没有人叫醒 Bot」——D6 会少给一次工具，
网关会拒绝一次本该放行的委托。

所以取的动作收成一次：`begin_round` 在回合真正开始时整批取走，存成这个频道的**本轮
快照**；D6 与网关都只读 `current_batch`。快照只活到回合收尾的 `end_round`。回合跑起来
之后到达的触发仍然留在本子里，属于下一轮，不会被这一轮看见。

## 并发

全部操作都在事件循环线程里完成，没有 `await`，因此不需要锁：`record` 与 `take` 之间
不存在让出点，读改写是原子的。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# 每个频道最多留多少条触发
MAX_TRIGGERS_PER_CHANNEL = 50
# 触发条目的最长存活时间（秒）：超过这个时长的触发不可能还属于「眼前这一轮」
TRIGGER_TTL_SECONDS = 600
# 一个频道的「正在跑的回合」计数多久没有新回合就不再算数（漏了收尾的兜底）
STALE_ROUND_SECONDS = 3600


@dataclass(frozen=True)
class RoundTrigger:
    """一条回合触发

    Attributes:
        kind: 触发种类。只有 `human` 可能带可信的发送者，其余一律视为非运维触发
        adapter_key: 触发来源的适配器标识
        instance_key: 触发来源的实例标识
        sender_id: 发送者账号；只有 `human` 有
        operator_trusted: 发送者在运维人员名单里**且**来源可信。只可能在入站事件
            自己的任务里算出来（来源判定要看当前连接），因此由调用方算好传进来
        source_trusted: 这个发送者的账号来自可信来源。与 `operator_trusted` 分开：
            后者还要求命中运维名单，网关的所有者 / 用户白名单是另一组名单。同一次
            入站事件里只查一次来源，两个字段共用那个结果
        at: 记录时刻（`time.monotonic()`）。用单调时钟，避免系统时间被调整后
            一批刚记下的触发集体过期
    """

    kind: str
    adapter_key: str = ""
    instance_key: str = ""
    sender_id: str = ""
    operator_trusted: bool = False
    source_trusted: bool = False
    at: float = field(default_factory=time.monotonic)


TRIGGER_KIND_HUMAN = "human"
TRIGGER_KIND_SYSTEM_PUSH = "system_push"
TRIGGER_KIND_UNTRUSTED_PUSH = "untrusted_push"
TRIGGER_KIND_TIMER = "timer"
TRIGGER_KIND_OTHER = "other"

TRIGGER_KINDS = frozenset(
    {
        TRIGGER_KIND_HUMAN,
        TRIGGER_KIND_SYSTEM_PUSH,
        TRIGGER_KIND_UNTRUSTED_PUSH,
        TRIGGER_KIND_TIMER,
        TRIGGER_KIND_OTHER,
    },
)


@dataclass(frozen=True)
class TriggerBatch:
    """一次取走的触发批次

    Attributes:
        triggers: 按记录顺序排列的触发
        incomplete: 这一批里是否有条目因为超限或过期被丢掉
    """

    triggers: List[RoundTrigger]
    incomplete: bool = False


class RoundTriggerBook:
    """进程内的触发记录簿，按会话标识分组"""

    def __init__(self, *, max_per_channel: int = MAX_TRIGGERS_PER_CHANNEL, ttl_seconds: float = TRIGGER_TTL_SECONDS):
        self.max_per_channel = max(1, int(max_per_channel))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._entries: Dict[str, List[RoundTrigger]] = {}
        # 某个频道的记录被丢过之后置真，直到下一次 `take` 把它带出去
        self._incomplete: Dict[str, bool] = {}
        # 本轮快照：`begin_round` 整批取走时存下，`end_round` 清掉。
        # 与 `_entries` 分开，是因为它属于「正在跑的回合」，而 `_entries` 属于「还没轮到」。
        self._snapshots: Dict[str, TriggerBatch] = {}
        # 每个频道正在跑的回合数与最近一次开始的时刻（单调时钟）
        self._active: Dict[str, Tuple[int, float]] = {}

    def record(self, chat_key: str, trigger: RoundTrigger) -> None:
        """记一条触发；超出上限时丢最早的一条并标记本批不完整"""
        if not chat_key:
            return
        entries = self._entries.setdefault(chat_key, [])
        entries.append(trigger)
        if len(entries) > self.max_per_channel:
            # 丢最早的：留下的是「离这一轮最近」的那批，但仍然算不完整——
            # 被丢掉的那条照样并进了这一轮
            del entries[: len(entries) - self.max_per_channel]
            self._incomplete[chat_key] = True

    def take(self, chat_key: str, *, now: float | None = None) -> TriggerBatch:
        """快照并清空一个频道的触发；过期条目在这里丢掉并标记不完整"""
        entries = self._entries.pop(chat_key, [])
        incomplete = self._incomplete.pop(chat_key, False)
        if not entries:
            return TriggerBatch(triggers=[], incomplete=incomplete)

        current = time.monotonic() if now is None else now
        fresh: List[RoundTrigger] = []
        for trigger in entries:
            if current - trigger.at > self.ttl_seconds:
                incomplete = True
                continue
            fresh.append(trigger)
        return TriggerBatch(triggers=fresh, incomplete=incomplete)

    def peek(self, chat_key: str) -> TriggerBatch:
        """只看不取（排查用）"""
        entries = list(self._entries.get(chat_key, []))
        return TriggerBatch(triggers=entries, incomplete=self._incomplete.get(chat_key, False))

    def begin_round(self, chat_key: str, *, now: float | None = None) -> TriggerBatch:
        """回合开始：按 `take` 的规则整批取走，并存成这个频道的本轮快照

        D6 与网关都只读这份快照，谁先谁后都拿得到同一批。快照按频道只存一份，所以前提是
        「同一频道先结束上一轮，再开始下一轮」。消息调度在极窄的时序窗口里会让同一频道
        同时跑两轮：这时两轮会读到同一份快照，分不清谁是谁叫醒的。所以上一轮还没收尾时
        开始的新一轮，快照一律记成「不完整」，两轮都拿不到授权；等这几轮全部收尾才清掉。
        漏了收尾的计数（一小时以上没有新回合）不再算数，免得一次漏收尾把频道永久压住。
        """
        batch = self.take(chat_key, now=now)
        if chat_key:
            current = time.monotonic() if now is None else now
            count, started_at = self._active.get(chat_key, (0, 0.0))
            if count > 0 and current - started_at > STALE_ROUND_SECONDS:
                count = 0
            if count > 0:
                batch = TriggerBatch(triggers=list(batch.triggers), incomplete=True)
            self._snapshots[chat_key] = batch
            self._active[chat_key] = (count + 1, current)
        return batch

    def current_batch(self, chat_key: str) -> TriggerBatch | None:
        """读本轮快照；这个频道还没开始过回合时返回 None

        返回 `None` 而不是空批，调用方才能区分「没走过回合开始」与「这一轮确实没有触发」。
        """
        return self._snapshots.get(chat_key)

    def end_round(self, chat_key: str) -> None:
        """回合收尾：清掉本轮快照

        快照不能留到下一轮：下一轮要按那时到达的触发重新判，留着上一轮的人等于把
        一次授权无限续期。同一频道还有别的回合没收尾时（见 `begin_round`），保留那份
        「不完整」的快照，等最后一轮收尾再清。
        """
        count, started_at = self._active.get(chat_key, (0, 0.0))
        if count > 1:
            self._active[chat_key] = (count - 1, started_at)
            return
        self._active.pop(chat_key, None)
        self._snapshots.pop(chat_key, None)

    def clear(self) -> None:
        """清空全部记录（进程内状态，供测试与关停使用）"""
        self._entries.clear()
        self._incomplete.clear()
        self._snapshots.clear()
        self._active.clear()


# 进程内唯一的一份。`message_service` 往里记，`run_agent` 在回合开始时取走
round_trigger_book = RoundTriggerBook()
