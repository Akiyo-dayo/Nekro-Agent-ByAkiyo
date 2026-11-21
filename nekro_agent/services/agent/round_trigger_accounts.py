"""本轮触发者账号：给插件日志写「这一轮是谁叫的 Bot」，也给用户邮件偏好认「本轮发言人」

## 为什么自己记一份，不借运维工具的触发记录簿

`services/ops_agent/triggers.py` 的 `round_trigger_book` 只在运维工具总开关
`OPS_AGENT_TOOLS_ENABLED` 或 SSH 网关开关打开时才记（`message_service.schedule_agent_task`），
而它们默认都关；它的 `peek` 也不按存活时间过滤。借它的话，默认部署里日志永远写 `unknown`，
打开以后又可能把上一轮遗留的条目算进本轮。所以这里单独记，不受运维开关影响，运维那一侧一行不改。

## 两段式

- **到达时**（`note_pending_trigger`）：`schedule_agent_task` 在两道闸门之后调用，与运维记账同一个位置。
  防抖会把多条触发并进一轮，所以先攒在「待处理」里。
- **回合开始时**（`begin_round_triggers`）：`run_agent` 把这个频道攒下的整体取走，作为本轮的记录；
  回合跑起来之后到达的触发留在待处理里，属于下一轮（与运维的取法一致）。
- **回合结束时**（`clear_round_triggers`）：清掉本轮记录。

两张表都按 `chat_key` 存、每个频道最多留 `MAX_TRIGGER_ACCOUNTS` 个账号；键的个数以频道数为上限，
不会无界增长。全部在事件循环线程里调用（没有 `await`），因此不需要锁。只记账号或触发种类，不记消息内容。

## 第二份记录：结构化的「本轮发言人」

第一份表是给人读日志用的，形状是「账号或触发种类」的字符串列表；用户邮件偏好要回答的是另一个问题：
**这一轮是不是恰好一个人在说话，他是谁**。两者判据不同（第一份里 `timer` 与 `unknown` 混在账号里），
所以另存一份 `RoundSpeakerFacts`，第一份的行为一行不改（`plugins/builtin/message_recall.py` 与
`tests/test_message_recall.py` 都在用）。

### 为什么要求「全部是人、只有一个人、批次完整」

用户邮件偏好只能改「正在说话的这个人自己的」设置。如果这一轮里有定时器或系统推送，说话的人可能
根本没开口，改的就不是「他自己的」；如果这一轮有两个人，模型分不清该听谁的，群友可以诱导它把别人的
祝福改投到自己的邮箱、或者替别人退订；如果攒的条目因为超过上限被丢过（`overflow`），我们甚至不知道
丢掉的是不是另一个人的触发，此时「只剩一个人」是假象。三种情况一律不给发言人，让方法拒绝并请用户
单独再说一次——比猜一个更安全，也更容易解释。

### 为什么 10 分钟

与运维授权同一个时长：回合卡住、`clear_round_triggers` 没跑到时，不能让一份旧记录一直有效。
沙盒方法在一轮里可能被调用很多次、间隔也可能很长（模型自己排的重试），10 分钟够覆盖正常的一轮，
又不会长到让「上一轮的人」在下一轮里继续被当成发言人。
"""

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from nekro_agent.services.ops_agent.triggers import TRIGGER_KIND_HUMAN, RoundTrigger

# 每个频道最多留几个触发者账号：日志字段，留多了只会把一行日志撑得很长
MAX_TRIGGER_ACCOUNTS = 10

# 取不到时的占位：日志里要能看出「这一轮没有触发记录」，又不能空着像字段漏了
UNKNOWN_TRIGGER_ACCOUNT = "unknown"

# 本轮发言人的有效期：与运维授权同样 10 分钟，回合卡住时不让一份旧记录一直有效
SPEAKER_TTL_SECONDS = 600.0

# 到达了、还没开回合的触发：chat_key -> 账号或触发种类（按出现顺序去重）
_pending_trigger_accounts: Dict[str, List[str]] = {}
# 正在跑的这一轮：chat_key -> 账号或触发种类
_round_trigger_accounts: Dict[str, Tuple[str, ...]] = {}


@dataclass(frozen=True)
class RoundSpeakerFacts:
    """这一轮的触发构成（只记「能不能定出一个人」，不记消息内容）

    Attributes:
        human_senders: 本轮人类触发的发送者账号，按出现顺序去重，最多 `MAX_TRIGGER_ACCOUNTS` 个
        has_non_human: 本轮有没有非人类触发（定时器、系统推送、其他），或人类触发却没有账号
        overflow: 攒的条目超过上限被丢过（此时「只剩一个人」不可信）
        opened_at: 回合开始的单调时钟，用来判 `SPEAKER_TTL_SECONDS`
    """

    human_senders: Tuple[str, ...] = ()
    has_non_human: bool = False
    overflow: bool = False
    opened_at: float = 0.0


# 待处理的结构化记录：chat_key -> 与上面第一份表同批的 RoundSpeakerFacts
_pending_speaker_facts: Dict[str, RoundSpeakerFacts] = {}
# 本轮的结构化记录：chat_key -> RoundSpeakerFacts
_round_speaker_facts: Dict[str, RoundSpeakerFacts] = {}

# 供测试注入的单调时钟。默认就是 `time.monotonic`，与运维授权过期用同一个时间源
_clock: Callable[[], float] = time.monotonic


def reset_clock() -> None:
    """恢复默认时钟（测试用）"""
    global _clock
    _clock = time.monotonic


def _account_of(trigger: RoundTrigger) -> str:
    """人发起的触发记账号，其余记触发种类（`timer`、`system_push` 等）"""
    if trigger.kind == TRIGGER_KIND_HUMAN and trigger.sender_id:
        return trigger.sender_id
    return trigger.kind


def note_pending_trigger(chat_key: str, trigger: RoundTrigger) -> None:
    """一条触发到达时记下（`schedule_agent_task` 在闸门之后调用）

    两份记录在同一处攒：第一份给人读日志，第二份判「本轮发言人」。
    """
    if not chat_key:
        return
    account = _account_of(trigger)
    if not account:
        return
    # 第一份表：给人读日志用的账号 / 触发种类列表。满了就丢，丢了不影响第二份的 overflow 判定
    pending = _pending_trigger_accounts.setdefault(chat_key, [])
    if account not in pending and len(pending) < MAX_TRIGGER_ACCOUNTS:
        pending.append(account)

    facts = _pending_speaker_facts.get(chat_key, RoundSpeakerFacts())
    if trigger.kind == TRIGGER_KIND_HUMAN and trigger.sender_id:
        senders = list(facts.human_senders)
        if trigger.sender_id not in senders:
            if len(senders) >= MAX_TRIGGER_ACCOUNTS:
                # 丢过条目就不能再说「只剩一个人」：丢掉的那个可能就是另一个发言人
                facts = RoundSpeakerFacts(
                    human_senders=tuple(senders),
                    has_non_human=facts.has_non_human,
                    overflow=True,
                    opened_at=facts.opened_at,
                )
            else:
                senders.append(trigger.sender_id)
                facts = RoundSpeakerFacts(
                    human_senders=tuple(senders),
                    has_non_human=facts.has_non_human,
                    overflow=facts.overflow,
                    opened_at=facts.opened_at,
                )
    else:
        # 非人类触发，或人类触发却没有账号（拿不到发送者）：都算「这一轮不只有一个说话的人」
        facts = RoundSpeakerFacts(
            human_senders=facts.human_senders,
            has_non_human=True,
            overflow=facts.overflow,
            opened_at=facts.opened_at,
        )
    _pending_speaker_facts[chat_key] = facts


def begin_round_triggers(chat_key: str) -> None:
    """回合开始时调用：把这个频道攒下的触发整体取走，作为本轮记录；没有攒下的记 `unknown`

    结构化记录无论如何都会建一份（哪怕是空的）：不建的话 `current_round_speaker` 会读到
    上一轮留下的那份，「上一轮的人」就成了这一轮的发言人。
    """
    if not chat_key:
        return
    pending = _pending_trigger_accounts.pop(chat_key, [])
    _round_trigger_accounts[chat_key] = tuple(pending) or (UNKNOWN_TRIGGER_ACCOUNT,)

    facts = _pending_speaker_facts.pop(chat_key, None) or RoundSpeakerFacts()
    _round_speaker_facts[chat_key] = RoundSpeakerFacts(
        human_senders=facts.human_senders,
        has_non_human=facts.has_non_human,
        overflow=facts.overflow,
        opened_at=_clock(),
    )


def current_round_trigger_accounts(chat_key: str) -> Tuple[str, ...]:
    """取这一轮的触发者账号；没有记录时返回 `("unknown",)`"""
    return _round_trigger_accounts.get(chat_key, (UNKNOWN_TRIGGER_ACCOUNT,))


def current_round_speaker(chat_key: str, *, now: Optional[float] = None) -> Optional[str]:
    """这一轮唯一的那个人类发言人；定不出来时返回 None

    只有「本轮记录存在、没过期、批次完整、全是人、恰好一个账号」才返回它。
    任何一条不满足都返回 None：调用方据此拒绝「改我的邮件偏好」这类只能由本人做的动作。

    Args:
        chat_key: 宿主签发的会话标识（`SessionOwner.chat_key`）
        now: 单调时钟；不传时用模块时钟（测试注入）
    """
    if not chat_key:
        return None
    facts = _round_speaker_facts.get(chat_key)
    if facts is None:
        return None
    moment = _clock() if now is None else float(now)
    if moment - facts.opened_at > SPEAKER_TTL_SECONDS:
        return None
    if facts.overflow or facts.has_non_human:
        return None
    if len(facts.human_senders) != 1:
        return None
    return facts.human_senders[0]


def clear_round_triggers(chat_key: str) -> None:
    """回合结束时清掉这个频道的本轮记录（待处理的不动，属于下一轮）"""
    _round_trigger_accounts.pop(chat_key, None)
    _round_speaker_facts.pop(chat_key, None)


def reset_round_triggers() -> None:
    """清空全部记录（测试用）"""
    _pending_trigger_accounts.clear()
    _round_trigger_accounts.clear()
    _pending_speaker_facts.clear()
    _round_speaker_facts.clear()


__all__ = [
    "MAX_TRIGGER_ACCOUNTS",
    "SPEAKER_TTL_SECONDS",
    "UNKNOWN_TRIGGER_ACCOUNT",
    "RoundSpeakerFacts",
    "begin_round_triggers",
    "clear_round_triggers",
    "current_round_speaker",
    "current_round_trigger_accounts",
    "note_pending_trigger",
    "reset_clock",
    "reset_round_triggers",
]
