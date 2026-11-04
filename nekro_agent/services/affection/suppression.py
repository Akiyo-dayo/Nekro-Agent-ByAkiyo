"""好感描述的频道级临时抑制

## 为什么要有

好感块里的两段描述（`feeling:` / `recently:`）由评估模型写、每轮注入主对话。一旦其中混进了
会被上游审核拦下的内容，这个频道的每一轮主对话都会被拦（用户侧就是空回或拒答），而评估
那一侧的自愈（去掉旧描述重试、清掉可疑描述）要等下一轮评估才发生。这段时间里频道是锁死的。

所以主对话一轮以「内容审核拦截」或「供应商拒答」收尾时，就给这个频道记一个标记：标记有效期间，
好感块只注入昵称和亲密阶段，不注入两段描述。这个频道下一次正常回复成功时清掉标记。
描述不是被删了，只是暂时不往这个频道注入；分数、阶段、库里的数据一概不动。

## 只在进程内

标记放在进程内存里，进程重启就没了——可以接受：重启后最坏情况是再被拦一轮、再记一次标记，
而换成落库就得为一个最长 30 分钟的临时状态加表、加清理。多进程部署下各进程各记各的，同理。

有效期取 `AI_CHAT_CONTEXT_EXPIRE_SECONDS`（与主对话历史、好感取对话同一个窗口），
在**读**的时候按当时生效的配置判断，记标记的地方因此不需要拿配置。
"""

from __future__ import annotations

import time
from typing import Dict, Optional

# 条目上限：每个被拦过的频道一条，正常回复后就摘掉，天然很小。超出时扔掉最早记下的一半
# （有效期在读的时候才知道，记的时候判断不了谁过期）——丢掉的只是「暂不注入描述」的提示，
# 最坏结果是某个频道多被拦一轮。
_MAX_ENTRIES = 4096

_marked_at: Dict[str, float] = {}


def suppress_narrative(chat_key: str, *, now: Optional[float] = None) -> None:
    """主对话被拦 / 被拒答之后调用：这个频道暂时不注入好感描述"""
    moment = time.time() if now is None else now
    if chat_key not in _marked_at and len(_marked_at) >= _MAX_ENTRIES:
        _drop_oldest_half()
    _marked_at[chat_key] = moment


def lift_narrative_suppression(chat_key: str) -> None:
    """这个频道正常回复成功之后调用：恢复注入"""
    _marked_at.pop(chat_key, None)


def is_narrative_suppressed(chat_key: str, ttl_seconds: int, *, now: Optional[float] = None) -> bool:
    """这个频道此刻是否处在抑制期内

    Args:
        chat_key: 频道
        ttl_seconds: 有效期，取 `AI_CHAT_CONTEXT_EXPIRE_SECONDS`；非正值表示不抑制
        now: 仅供测试注入
    """
    marked = _marked_at.get(chat_key)
    if marked is None:
        return False
    moment = time.time() if now is None else now
    if ttl_seconds <= 0 or moment - marked >= ttl_seconds:
        _marked_at.pop(chat_key, None)
        return False
    return True


def reset_narrative_suppression() -> None:
    """清空全部标记，仅供测试使用"""
    _marked_at.clear()


def _drop_oldest_half() -> None:
    for chat_key, _ in sorted(_marked_at.items(), key=lambda item: item[1])[: max(1, len(_marked_at) // 2)]:
        _marked_at.pop(chat_key, None)


__all__ = [
    "is_narrative_suppressed",
    "lift_narrative_suppression",
    "reset_narrative_suppression",
    "suppress_narrative",
]
