"""高级提示词槽位：逐层串起 `{{original}}` 并整理出预览需要的层信息

三个槽位的内容按「全局 → 适配器 / 实例 / 频道 → 人设」从低到高叠加：某一层写了
`{{original}}` 就把下面那层的结果嵌进来，没写就整段替换。这里只处理这一种占位：
其它宏要等运行时按当轮上下文展开（人设名、`{{user}}`、时间都只有跑一轮时才知道），
提前展开会把预览与真实请求分成两套结果。

开关关着时文字照算：预览页要显示「打开后会发什么」，由调用方决定发不发。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from nekro_agent.core.config import CoreConfig
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.services.config_resolver import config_resolver

# 大小写不敏感、允许花括号内侧有空白：卡片作者写 `{{ Original }}` 的不少。
_ORIGINAL = re.compile(r"\{\{\s*original\s*\}\}", re.IGNORECASE)

# 三个槽位对应的配置项名。写成常量而不是到处拼字符串：拼错一个字母的后果是
# `layered_values` 拿不到覆盖，预览里槽位悄悄变成「只有全局值」。
POST_HISTORY_FIELD = "PROMPT_POST_HISTORY"
AUXILIARY_FIELD = "PROMPT_AUXILIARY"
PREFILL_FIELD = "PROMPT_PREFILL"

# 人设层在 `layers` 里的层名；`ConfigLayer` 没有 preset，因为配置解析不认识人设行。
PRESET_LAYER = "preset"


@dataclass
class SlotText:
    """一个槽位的最终文字与真正贡献了文字的层

    `layers` 从低到高，层名用 `ConfigLayer` 的值，人设层写 `preset`。
    某一层整段替换掉下面的值以后，下面的层就不在里面了；`{{original}}` 保留的层仍在。
    """

    text: str
    layers: list[str] = field(default_factory=list)


@dataclass
class AdvancedSlots:
    """三个高级槽位的一次解析结果"""

    enabled: bool
    post_history: SlotText
    auxiliary: SlotText
    prefill: SlotText


def _chain(layers: Sequence[tuple[str, str]]) -> tuple[str, list[str]]:
    """从低到高串层，返回 (最终文字, 贡献了文字的层名)

    第一个非空层是起点；之后每一层：写了 `{{original}}` 就把当前结果嵌进去并保留
    之前贡献过的层，没写就整段替换、之前那些层不再贡献任何文字。
    """
    text = ""
    contributors: list[str] = []
    for index, (layer, value) in enumerate(layers):
        if index == 0:
            text = value
            contributors = [layer] if value else []
            continue

        if _ORIGINAL.search(value):
            text = _ORIGINAL.sub(lambda _match: text, value)
            if value:
                contributors.append(layer)
            continue

        text = value
        contributors = [layer] if value else []
    return text, contributors


def chain_original(values: Sequence[str]) -> str:
    """按从低到高的顺序展开 `{{original}}`；空序列返回空串

    没写 `{{original}}` 的层整段替换下面的值，写了则把下面的结果嵌进当前层。
    """
    text, _contributors = _chain([(str(index), value) for index, value in enumerate(values)])
    return text


def _slot(layer_values: Sequence[tuple[str, Any]], preset_text: str) -> SlotText:
    """把配置层与人设层串成一个槽位；人设层非空时作为最上一层参与"""
    chain: list[tuple[str, str]] = [(layer, str(value)) for layer, value in layer_values]
    if preset_text:
        chain.append((PRESET_LAYER, preset_text))
    text, contributors = _chain(chain)
    return SlotText(text=text, layers=contributors)


async def resolve_slots(channel: DBChatChannel, config: CoreConfig, card: DBPresetCard | None) -> AdvancedSlots:
    """按频道的生效配置与人设扩展行算出三个槽位

    `enabled` 取频道生效配置里的开关；关着时三个槽位的文字照样算出来，调用方据此
    决定「不发」，预览页照样能显示。人设层只有非空时才参与，空的人设行不会把配置层
    辛苦串好的文字清空。
    """
    post_history_values = await config_resolver.layered_values(
        channel.chat_key,
        POST_HISTORY_FIELD,
        channel.instance_key,
    )
    auxiliary_values = await config_resolver.layered_values(
        channel.chat_key,
        AUXILIARY_FIELD,
        channel.instance_key,
    )
    prefill_values = await config_resolver.layered_values(
        channel.chat_key,
        PREFILL_FIELD,
        channel.instance_key,
    )

    return AdvancedSlots(
        enabled=bool(config.PROMPT_ADVANCED_SLOTS_ENABLED),
        post_history=_slot(post_history_values, card.post_history if card is not None else ""),
        auxiliary=_slot(auxiliary_values, ""),
        prefill=_slot(prefill_values, card.prefill if card is not None else ""),
    )


__all__ = [
    "AUXILIARY_FIELD",
    "AdvancedSlots",
    "POST_HISTORY_FIELD",
    "PREFILL_FIELD",
    "PRESET_LAYER",
    "SlotText",
    "chain_original",
    "resolve_slots",
]
