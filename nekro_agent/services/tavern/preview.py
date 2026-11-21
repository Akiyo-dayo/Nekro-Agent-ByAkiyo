"""提示词预览：把一次组装结果变成接口能直接返回的形状

## 为什么单独一层

`build_round_prompt` 产出的是给模型用的消息对象（片段、图片、令牌），而预览接口要的是
「人能读的文本」：图片写 `[图片]`，一次性令牌换成固定占位，预填只留文字与结论。把这些
转换放在服务层而不是路由里，是因为试发也要用同一个历史行格式；两处各拼一次，示例行
迟早与真实消息长得不一样。

## 不写任何东西

本模块只读组装结果。预览的路由负责保证 `dry_run=True`，这里不查库、不落库、不调模型。
"""

from __future__ import annotations

import datetime
from typing import Literal

from nekro_agent.core.config import CoreConfig
from nekro_agent.schemas.tavern import (
    TavernPreviewMessage,
    TavernPreviewPrefill,
    TavernPreviewResponse,
)
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.round_prompt import RoundPrompt
from nekro_agent.services.agent.templates.compiler import EXAMPLE_SESSION_TOKEN
from nekro_agent.tools.untrusted_content import sanitize_block_text

# 示例消息最多 2000 字；与请求模型的校验共用同一个数字，避免两处各写一个。
PREVIEW_SAMPLE_MAX_CHARS = 2000

# 示例行的昵称。写死中文而不是用频道里的名字：这是管理员自己填的一句话，不该伪装成
# 频道里某个真实成员发的，也不该让预览看起来像真的收到了一条消息。
PREVIEW_NICKNAME = "管理员（预览）"

# 图片只报占位：预览不返回 base64、本地路径或远端地址，那些都不该出现在后台页面的
# JSON 里。模型真正收到的仍是图片段。
IMAGE_PLACEHOLDER = "[图片]"

# 一条预览消息的来源；与响应模型的 `source` 同一个字面量集合，写在这里让
# `message_sources` 能被单独调用而不依赖 pydantic 模型。
MessageSource = Literal["system", "example", "history"]


def format_synthetic_history_line(
    code: str,
    nickname: str,
    text: str,
    config: CoreConfig,
) -> str:
    """按真实历史行的格式拼一条「管理员说」的消息

    格式与 `DBChatMessage.parse_chat_history_prompt` 保持一致：可选前缀、时间、空平台
    ID、昵称、`说:`。平台 ID 留空是有意的——这句话不是任何平台账号发的，编一个 ID 会
    让模型以为群里真有这个人。

    `tome:true` 固定写上：预览要证明「群聊里指向 Bot 的消息会被标出来」，而 `tome`
    是否写给模型看由频道配置决定；示例行没有真实的 `is_tome` 记录，用真值会变成
    「配置关着就看不到这个前缀」，预览就演示不了。
    """
    # 行里没有令牌位；保留参数是为了让调用点与真实渲染同形，将来加令牌字段时不用改签名。
    _ = code
    limit = max(0, int(getattr(config, "AI_CONTEXT_LENGTH_PER_MESSAGE", len(text)) or len(text)))
    if limit and len(text) > limit:
        text = text[:limit]
    time_str = datetime.datetime.now().strftime("%m-%d %H:%M:%S")
    return f'(tome:true)[{time_str} id:] "{nickname}" 说: {text}'


def sanitize_sample_text(text: str) -> str:
    """把管理员填的示例消息按不可信文本清洗；预览里它也只能是普通正文"""
    return sanitize_block_text(text, PREVIEW_SAMPLE_MAX_CHARS)


def _message_text(message: OpenAIChatMessage, code: str) -> str:
    """把一条消息的片段拼成可读文本，并把本轮令牌换成固定占位"""
    parts: list[str] = []
    for segment in message.content:
        if segment.get("type") == "text":
            parts.append(str(segment.get("text", "")))
        elif segment.get("type") == "image_url":
            parts.append(IMAGE_PLACEHOLDER)
    text = "".join(parts)
    return text.replace(code, EXAMPLE_SESSION_TOKEN) if code else text


def _preview_prefill(prompt: RoundPrompt) -> TavernPreviewPrefill:
    """预填的最终文字、会不会发与原因

    判据直接取自 104b1 的 `meta["prefill"]`：那里已经把「槽位关着」「组上没开预填」
    「这个组刚被上游拒绝过」分好了，这一层只把状态名翻成接口要的四个词。
    """
    meta_prefill = prompt.meta.get("prefill")
    if not isinstance(meta_prefill, dict):
        return TavernPreviewPrefill()
    text = str(meta_prefill.get("text", "") or "")
    would_send = bool(meta_prefill.get("will_send", False))
    reason = str(meta_prefill.get("reason", "empty") or "empty")
    if reason == "ready":
        reason = "sent"
    elif reason == "gated":
        reason = "gated"
    elif not text and reason != "gated":
        reason = "empty"
    else:
        reason = "unsupported"
    return TavernPreviewPrefill(text=text, would_send=would_send, reason=reason)


def message_sources(count: int) -> list[MessageSource]:
    """给 `count` 条消息逐条标来源：第一条系统、最后一条历史、中间都是示例

    `build_round_prompt` 的拼装顺序是固定的（系统 → 示例 → 历史），所以来源只由位置
    决定，不需要把每段拆开传进来。1 条时只标系统——这种形状实际不会出现，但不能越界；
    0 条返回空表。
    """
    if count <= 0:
        return []
    if count == 1:
        return ["system"]
    return ["system", *(["example"] * (count - 2)), "history"]


_KNOWN_SOURCES = frozenset({"system", "example", "history"})


def _recorded_sources(recorded: object, count: int) -> list[MessageSource]:
    """优先用组装处记下的来源；对不上（条数不同、有不认识的值）才按位置推

    `build_round_prompt` 在拼消息的地方就记下每条来自哪一段，不依赖「第一条系统、最后
    一条历史」这个形状；按位置推只是兜底，给没有这份记录的调用方（单测直接构造的组装结果）。
    """
    if (
        isinstance(recorded, list)
        and len(recorded) == count
        and all(isinstance(item, str) and item in _KNOWN_SOURCES for item in recorded)
    ):
        return list(recorded)  # type: ignore[arg-type]
    return message_sources(count)


def build_preview_response(prompt: RoundPrompt, code: str) -> TavernPreviewResponse:
    """把组装结果转成预览响应

    Args:
        prompt: `build_round_prompt(dry_run=True, ...)` 的结果。
        code: 本轮一次性令牌；消息里出现它的地方都换成固定占位。
    """
    greeting = prompt.meta.get("greeting")
    sources = _recorded_sources(prompt.meta.get("message_sources"), len(prompt.messages))
    return TavernPreviewResponse(
        messages=[
            TavernPreviewMessage(
                role=message.role,
                text=_message_text(message, code),
                source=sources[index],
            )
            for index, message in enumerate(prompt.messages)
        ],
        prefill=_preview_prefill(prompt),
        meta=prompt.meta,
        greeting=greeting if isinstance(greeting, dict) else {},
    )


__all__ = [
    "IMAGE_PLACEHOLDER",
    "PREVIEW_NICKNAME",
    "PREVIEW_SAMPLE_MAX_CHARS",
    "build_preview_response",
    "format_synthetic_history_line",
    "message_sources",
    "sanitize_sample_text",
]
