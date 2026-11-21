"""预填（Assistant Prefill）的消息形态与回显处理

预填是让模型从管理员写好的开头继续生成：Anthropic 原生与 OpenRouter 等把末尾一条
assistant 消息当作预填，DeepSeek 的前缀续写还要额外带 `prefix: true`。这两种形态都
是**服务商约定**，不是 NA 的消息模型，所以这里只产出普通的 dict，交给
`gen_openai_chat_response` 原样透传（它本来就把非 `OpenAIChatMessage` 的消息当字典用），
不去改 `OpenAIChatMessage` 的形状。

服务商只返回续写部分，调用方要把预填接回前面再解析；个别中转会把预填回显在回复开头，
接之前先判一次，避免把同一段话叠两遍。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nekro_agent.core.config import ModelConfigGroup

from .model_capability import is_capability_marked

# 能力观测里「预填」这一档的名字；与视觉共用同一套有 TTL 的负判据。
PREFILL_CAPABILITY = "prefill"

# 会真的发预填的两种模式。清单外的取值（包括 off）一律不发：配置里出现一个没认识的
# 模式时，保守地按「关」处理，而不是猜成 assistant 把文字发出去。
_PREFILL_MODES = frozenset({"assistant", "deepseek"})


@dataclass
class PrefillSpec:
    """一次请求的预填：文字与发送结果

    `state` 由发送侧写回，调用方用它记明细：
    `pending`（还没发）、`sent`、`gated`（调用方因槽位关着不发，自己设）、
    `empty`（没有文字）、`unsupported`（模型组关着或在被拒的 TTL 内）、
    `retried_without`（带预填被拒，去掉预填重发的那一次）。
    """

    text: str
    state: str = "pending"


def prefill_message(group: ModelConfigGroup, text: str) -> dict[str, Any] | None:
    """按模型组算出要追加到消息末尾的那条预填消息；不该发时返回 `None`

    三种「不发」都在这里收口：模型组的预填方式不是 assistant / deepseek、文字为空、
    这个组刚被上游拒绝过预填（负判据还没过期）。
    """
    if group.PREFILL_MODE not in _PREFILL_MODES:
        return None
    if not text:
        return None
    if is_capability_marked(group, PREFILL_CAPABILITY):
        return None

    message: dict[str, Any] = {"role": "assistant", "content": text}
    if group.PREFILL_MODE == "deepseek":
        # DeepSeek 的前缀续写：少这个键它就当普通 assistant 消息、不会接着写下去。
        message["prefix"] = True
    return message


def merge_prefill(content: str, prefill: str) -> str:
    """把服务商返回的续写接回预填后面；回复已经以预填开头时原样返回"""
    if not prefill:
        return content
    if content.startswith(prefill):
        return content
    return prefill + content


__all__ = [
    "PREFILL_CAPABILITY",
    "PrefillSpec",
    "merge_prefill",
    "prefill_message",
]
