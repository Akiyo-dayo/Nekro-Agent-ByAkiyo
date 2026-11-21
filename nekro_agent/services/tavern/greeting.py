"""人设开场白：什么时候发、发哪一条、记录写到哪

## 为什么用频道 `data` 里的一个「上次发过谁」的标记判断

「切到这个人设」的入口不止一个：频道绑定、实例默认人设、`AgentCtx.set_preset`、人设被删
之后的回退链。逐个入口打标记迟早会漏一个，而漏掉的那个频道就永远不发开场白。这里改成
每轮解析出生效人设之后看一眼记录：记录与当前人设不同才发，所有入口自然都覆盖到。

记录有三种状态：键不存在（升级前的老频道、或还没跑过一轮的新频道）、`-1`（`/reset`
写的，表示下次要发）、某个人设 ID。键不存在时用「频道里有没有 Bot 发过的消息」区分
老频道与新频道：老频道只记不发，免得升级后每个群突然冒出一条开场白。

## 发送失败不重试

开场白发不出去只记 WARNING，记录照样写。否则这条频道每一轮都会再试一次，而失败原因
（协议端断线、实例停用）通常不会因为多试几次就好；记录写下去以后，下一轮按「已发过」
处理。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, replace
from typing import Any, Optional

from nekro_agent.core.config import CoreConfig
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel, DefaultPreset
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.chat.universal_chat_service import universal_chat_service
from nekro_agent.services.memory import prompt_rounds

from .macros import expand_macros
from .runtime import TavernRound

logger = get_sub_logger("tavern.greeting")

# 频道 `data` 里记「上次为哪个人设发过开场白」的键。`-1` 专用于 `/reset`：
# 不删键是因为删掉以后，有过 Bot 消息的频道会被当成老频道、只记不发。
GREETED_KEY = "tavern_greeted_preset_id"
RESET_MARK = -1

PresetLike = DBPreset | DefaultPreset


@dataclass(frozen=True)
class GreetingDecision:
    """一次开场白判断的结果

    Attributes:
        send: 这一轮要不要发。
        text: 要发的开场白原文（宏还没展开）；`send` 为假时是 `None`。
        record: 这一轮要不要把记录写成当前人设 ID。
    """

    send: bool
    text: Optional[str]
    record: bool


def _preset_id_of(preset: PresetLike) -> int:
    """人设行 ID；内置默认人设没有 `id`，按 0 处理（与运行时同一个口径）"""
    preset_id = getattr(preset, "id", None)
    return preset_id if isinstance(preset_id, int) and preset_id > 0 else 0


def _valid_greetings(value: Any) -> list[str]:
    """从一个 JSON 字段里取出非空的开场白；不是列表就当作没有"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item != ""]


def pick_greeting(channel: DBChatChannel, card: DBPresetCard) -> Optional[str]:
    """按频道类型与挑选方式选一条开场白；没有可用的一条时返回 `None`

    群聊优先用群聊专用开场白，但那个列表全是空串时仍回落到普通开场白——导入器会把
    缺失的字段写成空列表，空列表与「全是空串」都按没有处理。
    """
    group_greetings = _valid_greetings(card.group_greetings)
    greetings = _valid_greetings(card.greetings)
    candidates = group_greetings if channel.chat_type == ChatType.GROUP and group_greetings else greetings
    if not candidates:
        return None
    if card.greeting_pick == "random":
        return random.choice(candidates)
    return candidates[0]


async def _has_bot_message(channel: DBChatChannel) -> bool:
    """频道里有没有 Bot 发过的消息；只在记录键不存在时查一次

    不加时间下限：升级前任何时期有过 Bot 消息都算老频道，这正是判据要的。
    """
    return await DBChatMessage.filter(chat_key=channel.chat_key, sender_id="-1").exists()


async def decide(
    channel: DBChatChannel,
    preset_id: int,
    card: Optional[DBPresetCard],
    config: CoreConfig,
) -> GreetingDecision:
    """按判据表判断这一轮发不发、记不记；不写任何东西"""
    # 用 `getattr` 取：真实调用传的永远是 `CoreConfig`，但预览 / 组装提示词的测试替身
    # 可能只带自己关心的字段。缺字段时按闸关处理（不读、不写），是保守方向。
    if not bool(getattr(config, "TAVERN_COMPAT_ENABLED", False)):
        # 回滚闸关着就回到改造前的行为：连频道 `data` 都不读、不写。
        return GreetingDecision(send=False, text=None, record=False)

    data = channel._load_data()  # noqa: SLF001 - 与「自定义频道名」等标记共用同一份读改写
    recorded = data.get(GREETED_KEY)
    has_record = isinstance(recorded, int) and not isinstance(recorded, bool)

    if has_record and recorded == preset_id:
        # 这个频道已经为当前人设发过（或记过）了，什么都不做。
        return GreetingDecision(send=False, text=None, record=False)

    if not has_record:
        # 键不存在：老频道只记不发，新频道发。Bot 消息查询只发生在这一支。
        if await _has_bot_message(channel):
            return GreetingDecision(send=False, text=None, record=True)
    # 走到这里：键不存在且没有 Bot 消息（新频道），或记录是 `-1` / 另一个人设 ID。

    if not bool(getattr(config, "PERSONA_GREETING_ENABLED", False)) or card is None:
        # 前提不满足时照样记，之后再打开开关不会补发一串。
        return GreetingDecision(send=False, text=None, record=True)

    text = pick_greeting(channel, card)
    if text is None:
        return GreetingDecision(send=False, text=None, record=True)
    return GreetingDecision(send=True, text=text, record=True)


def _expand_greeting(text: str, tavern: TavernRound) -> str:
    """按本轮宏上下文展开开场白；没有上下文（闸关或解析失败）时原样返回"""
    if tavern.macro is None:
        return text
    return expand_macros(text, replace(tavern.macro, text_kind="greeting")).text


async def _write_record(channel: DBChatChannel, preset_id: int) -> None:
    """把记录写成当前人设 ID

    先 `refresh_from_db` 再读改写，缩小与别处写 `data` 互相覆盖的窗口。仍有极小的竞态：
    两次读改写之间另一处也保存了 `data`，后保存的那次会覆盖前一次。现有的
    `set_custom_channel_name`、自动停用标记都是同一种写法，这里不另造一套。
    """
    try:
        await channel.refresh_from_db(fields=["data"])
        data = channel._load_data()  # noqa: SLF001 - 与频道 `data` 的其它读写共用同一份解析
        data[GREETED_KEY] = preset_id
        channel.data = json.dumps(data, ensure_ascii=False)
        await channel.save(update_fields=["data", "update_time"])
    except Exception as error:  # noqa: BLE001 - 记录失败不该让这一轮哑掉
        logger.warning(f"写开场白记录失败: chat_key={channel.chat_key}, preset_id={preset_id}, error={error!r}")


async def maybe_greet(
    *,
    channel: DBChatChannel,
    preset: PresetLike,
    card: Optional[DBPresetCard],
    config: CoreConfig,
    ctx: Any,
    tavern: TavernRound,
    one_time_code: str,
) -> bool:
    """按判据判断并（在需要时）发出开场白；返回是否真的发出去了

    开场白经通用发送服务发出，带 `ctx` 所以会先过输出清理、再过输出过滤，并记进聊天
    记录——这一轮的历史里随后就能看到它。发送失败只记 WARNING（不带开场白原文），
    记录照写，下一轮不会重试。
    """
    preset_id = _preset_id_of(preset)
    decision = await decide(channel, preset_id, card, config)
    sent = False

    if decision.send and decision.text is not None:
        try:
            # 展开也算发送准备：卡片里的怪宏不该让这一轮主回复挂掉。
            text = _expand_greeting(decision.text, tavern)
            await universal_chat_service.send_agent_message(
                chat_key=channel.chat_key,
                messages=text,
                adapter=ctx.adapter,
                ctx=ctx,
                record=True,
            )
        except Exception as error:  # noqa: BLE001 - 开场白失败只影响这一条，不拦本轮回复
            logger.warning(
                f"发送开场白失败: chat_key={channel.chat_key}, preset_id={preset_id}, error={error!r}",
            )
        else:
            sent = True
            prompt_rounds.note_tavern(one_time_code, greeting=True)

    if decision.record:
        await _write_record(channel, preset_id)
    return sent


async def preview_greeting(
    *,
    channel: DBChatChannel,
    preset: PresetLike,
    card: Optional[DBPresetCard],
    config: CoreConfig,
    tavern: TavernRound,
) -> dict[str, Any]:
    """预览用：只判断与展开，不发、不写库

    返回的形状与预填的预览元数据一致：`will_send` 表示「下一次被叫到会不会先发」，
    `text` 是展开后的那条开场白（不发时为空串）。
    """
    decision = await decide(channel, _preset_id_of(preset), card, config)
    if not decision.send or decision.text is None:
        return {"will_send": False, "text": ""}
    return {"will_send": True, "text": _expand_greeting(decision.text, tavern)}


__all__ = [
    "GREETED_KEY",
    "RESET_MARK",
    "GreetingDecision",
    "decide",
    "maybe_greet",
    "pick_greeting",
    "preview_greeting",
]
