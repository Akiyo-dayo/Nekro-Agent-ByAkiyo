"""Agent 回合失败时给用户的可见回执

## 为什么需要这一层

「Bot 装死」有两种形态，代价完全不同：

- **没回执**：用户只看到机器人不理人，既不知道该不该重说一次，也不会来报障。
  现场那条「沙盒超时 120s」的事故正是这样被拖了九个多小时。
- **回执刷屏**：08-11 那天真实群里被刷了十几条「与 LLM 通信出错啦」——每一轮失败都发一条，
  而故障持续了三个小时。回执本身变成了新的洪水。

所以回执要发，但要按会话冷却。这与 `services/instance_concurrency.py` 里那条
`should_notify_rejection` 是同一个思路，但**不能直接复用它**：那条按 `instance_key` 冷却、
读的是沙盒配置项 `SANDBOX_INSTANCE_REJECT_NOTICE_COOLDOWN_SECONDS`。LLM 失败的粒度是会话
（一个实例下几十个群，A 群炸了不该让 B 群也收不到提示），配置项也该跟着
`SESSION_ENABLE_FAILED_LLM_FEEDBACK` 一起走会话覆盖层。

## 文案为什么要分类

「一个字都没返回」和「网络打不通」对用户不是同一件事：前者重说一次大概率就好，
后者通常要等。文案取自**固定集合**，不带任何上游原文，因此不构成凭据/配置的旁路
（与 `services/model_group_test.py` 的归一化结论同一条约束）。
"""

import time
from typing import Dict, Optional, Sequence

from nekro_agent.core.logger import get_sub_logger

from .model_capability import LLMFailureKind

logger = get_sub_logger("agent_runtime")

# 回执文案的固定集合。改这些字面量前先想清楚：它们会出现在真实用户的群里
NOTICE_LLM_FAILED = "哎呀，与 LLM 通信出错啦，请稍后再试~ QwQ"
NOTICE_LLM_EMPTY = "唔…模型这次一个字都没返回，再跟我说一次好吗~ QwQ"
# 「被挡下来」与「一个字都没返回」对用户是两件事：后者重说一次原话大概率就好，
# 前者重说原话必然再被挡，得换个说法。文案里不带上游任何原文（连 finish_reason 都不带），
# 与本文件开头那条「文案取自固定集合」的约束一致
NOTICE_LLM_BLOCKED = "唔…这次的内容被模型挡下来了，换个说法再跟我说一次吧~ QwQ"
NOTICE_ROUND_UNFINISHED = "这轮我没能把事情做完（已经试到最大迭代次数啦），需要的话再说一次吧~ QwQ"
NOTICE_ROUND_TIMEOUT = "这次处理超时了，先跳过这一条，稍后再试~ QwQ"
NOTICE_ROUND_ERROR = "处理这条消息时出错了，稍后再试~ QwQ"
# 以下两条对应 `run_agent` 的两条**提前收尾**路径。它们与 `NOTICE_ROUND_UNFINISHED` 必须分开：
# 那条的潜台词是「再说一次也许就好了」，而这两条是「同样的说法再来一次也是一样的结果」，
# 给用户的下一步动作完全不同。追加在末尾，不重排上面的既有常量。
NOTICE_ROUND_NO_CODE = "唔…模型这次没给出能执行的内容（多半是被安全策略挡下了），换个说法再试试吧~ QwQ"
NOTICE_ROUND_STALLED = "同一个错误一直重复出现，再试也是一样的结果，我先停下了；晚点或者换个说法再来吧~ QwQ"
# 第三条提前收尾路径：渠道把「拒绝受理」当成普通正文返回（`AgentProviderRefusalError`）。
# 与相邻两条都必须分开：
# - 与 `NOTICE_ROUND_NO_CODE`：那条的潜台词是「模型没写代码，重出一次也许就写出来了」，
#   而这条是「请求压根没到模型那儿，重说原话必然还是这一页」，用户的下一步动作不同。
# - 与 `NOTICE_LLM_BLOCKED`：那条对应「上游连正文都没给」（空 content + prohibited_content），
#   这条对应「正文非空、内容却是一页拒绝说明」。两者的检出点、异常类型、日志都不同，
#   文案混用会让「用户到底撞上了哪一种」在事后排查时不可区分。
# 依本文件开头那条约束：不带上游任何原文——不带链接、不带策略名、不带 finish_reason。
NOTICE_ROUND_PROVIDER_REFUSAL = "唔…这次的内容被模型的安全策略挡下了，换个说法再跟我说一次吧~ QwQ"


def describe_llm_failure(kinds: Sequence[LLMFailureKind]) -> str:
    """把一轮里各次尝试的失败归类折成一句用户回执

    只有**每一次尝试**都是同一类时才用那一类的专用文案：混合了网络错误时说「一个字都没返回」
    或者说「被挡下来了」，都会把用户往错的方向引（前者让他傻等，后者让他白改措辞）。
    这条折叠规则对 `CONTENT_BLOCKED` 与 `EMPTY_RESPONSE` 同构，新增一类不改判据形状。
    """
    if kinds and all(kind is LLMFailureKind.CONTENT_BLOCKED for kind in kinds):
        return NOTICE_LLM_BLOCKED
    if kinds and all(kind is LLMFailureKind.EMPTY_RESPONSE for kind in kinds):
        return NOTICE_LLM_EMPTY
    return NOTICE_LLM_FAILED


# 记账条目上限。键是 `chat_key`，而频道数是**不设上限**的增长维度（一个实例几十个群、
# 一个进程几十个实例、还有私聊），与 `model_capability._MAX_MARKS` 的键（模型组配置，
# 天然被配置规模限制住）完全不是一回事，所以这里必须自己封顶。
_MAX_TRACKED_CHATS = 4096


class ChatNoticeThrottle:
    """按会话冷却的聊天回执闸门

    冷却窗口内**仍然打日志、仍然计数**，只是不再往聊天里发——可诊断性一点不减，
    减的只是刷屏。

    记账是进程内、无上限增长的那一类：过了冷却窗口的条目已经没有任何作用
    （它只用来判断「距上次发送是否不足 cooldown」），所以每次记账时顺手清掉，
    再按条数封顶。被清掉的最坏后果是那个会话下一次失败立刻收到回执 —— 正是没有记账时的行为。
    """

    def __init__(self) -> None:
        self._last_sent_at: Dict[str, float] = {}
        self._suppressed: Dict[str, int] = {}

    def _prune(self, now: float, cooldown: float) -> None:
        # 过了冷却窗口的时间戳已经不影响任何判断，直接丢。
        # **不动 `_suppressed`**：那一份是「这个会话被压下去过几条」，下一次真发的时候要打进日志里；
        # 时间戳没了正好走到「last is None」那一支，抑制条数照样报得出来。
        for key in [key for key, sent_at in self._last_sent_at.items() if now - sent_at >= cooldown]:
            self._last_sent_at.pop(key, None)
        while len(self._last_sent_at) > _MAX_TRACKED_CHATS:
            oldest = min(self._last_sent_at, key=lambda key: self._last_sent_at[key])
            self._last_sent_at.pop(oldest, None)
            self._suppressed.pop(oldest, None)
        while len(self._suppressed) > _MAX_TRACKED_CHATS:
            # 抑制条数最少的那条信息量最低，先丢它
            least = min(self._suppressed, key=lambda key: self._suppressed[key])
            self._suppressed.pop(least, None)

    def should_send(self, chat_key: str, cooldown_seconds: float, *, now: Optional[float] = None) -> bool:
        current = time.time() if now is None else now
        cooldown = max(0.0, float(cooldown_seconds))
        last = self._last_sent_at.get(chat_key)
        if cooldown > 0 and last is not None and current - last < cooldown:
            self._suppressed[chat_key] = self._suppressed.get(chat_key, 0) + 1
            return False
        if self._suppressed.get(chat_key):
            logger.info(
                f"[failure_feedback] {chat_key} | 冷却窗口内已抑制 {self._suppressed[chat_key]} 条失败回执",
            )
        self._suppressed.pop(chat_key, None)
        self._prune(current, cooldown)
        self._last_sent_at[chat_key] = current
        return True

    def tracked_count(self) -> int:
        """当前记账的会话数（仅供观测与测试）"""
        return len(self._last_sent_at)

    def suppressed_count(self, chat_key: str) -> int:
        """冷却窗口内被压下去的回执条数（仅供观测与测试）"""
        return self._suppressed.get(chat_key, 0)

    def reset(self) -> None:
        self._last_sent_at.clear()
        self._suppressed.clear()


# 进程内单例：多实例部署下所有实例共用一个进程，按 chat_key 分桶已经足够隔离
failure_notice_throttle = ChatNoticeThrottle()
