"""每轮的酒馆运行时上下文：人设正文、深度注入、三个高级槽位与宏

## 为什么按轮次缓存

一轮里提示词可能渲染多次（系统提示词、历史、迭代时的增量历史）。宏里的 `{{time}}` /
`{{random}}` 每次展开都可能不同，人设正文又是整段指令级文字：同一轮里重算一遍会让模型
看到两份不一样的人设。因此按本轮的一次性令牌缓存，与 L4 世界书同一个键、同一套上限
（`MAX_ROUNDS` = 4096，满了丢最早一半），一轮结束由调用方 `discard_tavern_round` 弹出。

## 闸关时一次查询都不做

`TAVERN_COMPAT_ENABLED` 关掉是回滚闸：运行时不读 `preset_card`、不展开任何宏。这条要求
必须能从查询日志上看出来，所以闸关分支在碰任何 ORM 之前就返回，连「这个频道最近一条消息
是谁发的」都不查。

## 读路径 fail-open

整段解析包在 `try/except Exception` 里，失败时记 WARNING 并按闸关的形状返回（人设正文原样、
其余全空）：卡片数据出问题不该让 Bot 在群里哑掉，而提示词因此退回 L4 的行为，与设计上的
回滚闸同一个方向。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Optional

from nekro_agent.core.config import config as global_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel, DefaultPreset
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_rp_preset import DBRpPreset
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.services.agent.prefill import PrefillSpec
from nekro_agent.services.memory import prompt_rounds

from .macros import MacroContext, MacroResult, expand_macros, sanitize_user_name
from .slots import AdvancedSlots, resolve_slots

logger = get_sub_logger("tavern.runtime")

# 轮次缓存上限：与 `worldbook.runtime.MAX_ROUNDS`、`prompt_rounds._MAX_ROUNDS` 同一个做法。
MAX_ROUNDS = 4096

PresetLike = DBPreset | DefaultPreset


@dataclass
class TavernRound:
    """一轮酒馆运行时的解析结果

    字段语义与设计稿一致：`persona_content` 是本轮真正发给模型的人设正文（宏展开之后），
    深度注入、辅助提示词、后置指令都是**已展开**的文字；槽位关着或为空时是空串。
    `slots` 原样保留解析出来的各层，供预览显示「打开了会发什么」。

    Attributes:
        enabled: `TAVERN_COMPAT_ENABLED`；关着时其余字段都是回滚后的形状。
        card: 人设的酒馆扩展行；没有（含内置默认人设）时是 `None`。
        macro: 当轮宏上下文；闸关或解析失败时是 `None`。
        persona_content: 本轮人设正文。
        depth_prompt: 已展开的深度注入正文；空串表示不注入。
        depth: 深度注入插在聊天记录倒数第几条之前（0 = 最后一条之后）。
        auxiliary: 已展开的辅助提示词。
        post_history: 已展开的后置指令。
        prefill: 槽位开着且预填文字非空时才有。
        slots: 三个槽位的逐层解析结果；闸关或解析失败时是 `None`。
    """

    enabled: bool
    card: Optional[DBPresetCard]
    macro: Optional[MacroContext]
    persona_content: str
    depth_prompt: str
    depth: int
    auxiliary: str
    post_history: str
    prefill: Optional[PrefillSpec]
    slots: Optional[AdvancedSlots]
    # 这一轮启用的角色扮演预设；没选、槽位关着、看不见或读库失败时是 `None`。
    rp_preset: Optional[DBRpPreset] = None


_rounds: dict[str, TavernRound] = {}

# 引用了看不见 / 已删除预设时，同一频道 10 分钟内只记一条 WARNING：频道每个回合都会
# 解析一次，不收敛的话日志会被同一个 id 刷满。
_RP_WARNING_INTERVAL_SECONDS = 600.0
_rp_preset_warnings: dict[str, float] = {}


def _warn_rp_preset_once(chat_key: str, message: str) -> None:
    now = time.monotonic()
    last = _rp_preset_warnings.get(chat_key)
    if last is not None and now - last < _RP_WARNING_INTERVAL_SECONDS:
        return
    if len(_rp_preset_warnings) >= MAX_ROUNDS:
        _rp_preset_warnings.clear()
    _rp_preset_warnings[chat_key] = now
    logger.warning(message)


async def _resolve_rp_preset(channel: DBChatChannel, config: Any) -> Optional[DBRpPreset]:
    """按频道生效配置解析这一轮的角色扮演预设

    只在 `PROMPT_RP_PRESET_ID > 0` 时查库；读库失败、行不存在、或归属不是「全局 /
    本实例」都返回 `None`，调用方据此让提示词与没选预设时完全一样（fail-open）。
    """
    try:
        preset_id = int(getattr(config, "PROMPT_RP_PRESET_ID", 0) or 0)
    except (TypeError, ValueError):
        preset_id = 0
    if preset_id <= 0:
        return None
    try:
        row = await DBRpPreset.filter(id=preset_id).first()
    except Exception as error:  # noqa: BLE001 - 读路径 fail-open
        _warn_rp_preset_once(
            channel.chat_key,
            f"读取角色扮演预设失败，这一轮按没选预设处理: chat_key={channel.chat_key}, id={preset_id}, error={error!r}",
        )
        return None
    if row is None:
        _warn_rp_preset_once(
            channel.chat_key,
            f"引用的角色扮演预设不存在，这一轮按没选预设处理: chat_key={channel.chat_key}, id={preset_id}",
        )
        return None
    if row.owner_instance_key not in ("", channel.instance_key):
        _warn_rp_preset_once(
            channel.chat_key,
            f"引用的角色扮演预设不属于本实例，这一轮按没选预设处理: "
            f"chat_key={channel.chat_key}, id={preset_id}, owner={row.owner_instance_key}",
        )
        return None
    return row


def cached_tavern_round(code: str) -> Optional[TavernRound]:
    """只读缓存，不查库；没解析过（或已经收尾弹出）就是 `None`"""
    return _rounds.get(code)


def discard_tavern_round(code: str) -> None:
    """弹出一轮的缓存；收尾与预览都用它，绝不抛"""
    try:
        _rounds.pop(code, None)
    except Exception as error:  # noqa: BLE001 - 收尾路径不能抛
        logger.warning(f"酒馆轮次缓存清理失败: code={code}, error={error!r}")


def _preset_id_of(preset: PresetLike) -> int:
    """人设行 ID；内置默认人设没有 `id`，按 0 处理

    返回 0 的另一个用途是 `{{pick}}` 的种子：内置人设没有稳定行 ID，用 0 让它在
    同一频道里仍然稳定。
    """
    preset_id = getattr(preset, "id", None)
    return preset_id if isinstance(preset_id, int) and preset_id > 0 else 0


def _examples_raw(card: Optional[DBPresetCard]) -> str:
    """卡片示例对话原文；没有卡片或字段形状不对时是空串

    `{{mesExamplesRaw}}` 要的就是导入时保存的那段原文，其中的 `{{char}}` / `{{user}}`
    留给宏展开的下一遍处理。`source_fields` 是 JSON 列，历史数据或手工改库可能不是
    dict，任何一种形状不对都按空串处理，不让一轮对话因此失败。
    """
    if card is None:
        return ""
    fields = getattr(card, "source_fields", None)
    if not isinstance(fields, dict):
        return ""
    value = fields.get("mes_example")
    return value if isinstance(value, str) else ""


async def _resolve_user_name(channel: DBChatChannel, trigger_message: Optional[ChatMessage]) -> str:
    """`{{user}}` 的名字：触发这一轮的人，没有就取最近一条非 Bot 消息，再没有用「用户」

    昵称由群友自己控制，进提示词之前必须过 `sanitize_user_name`（去换行、标签、零宽，
    限 24 字），见设计稿对残余风险的说明。
    """
    nickname = ""
    if trigger_message is not None:
        nickname = str(getattr(trigger_message, "sender_nickname", "") or "").strip()
    if not nickname:
        latest = (
            await DBChatMessage.filter(chat_key=channel.chat_key)
            .exclude(sender_id="-1")
            .order_by("-id")
            .first()
        )
        if latest is not None:
            nickname = str(latest.sender_nickname or "").strip()
    return sanitize_user_name(nickname or None)


def _expand(
    text: str,
    macro: MacroContext,
    text_kind: str,
    stats: dict[str, Any],
    *,
    warn: bool = True,
) -> str:
    """按某一类文字的上下文展开宏，并把展开处数与长度保护累计到本轮统计里

    触发长度保护时记一行 WARNING（设计稿 §7.1）：只写文字类别与前后字数，不写正文——
    明细里的 `guarded` 要事后翻，现场排障得在日志里看得到「这段宏被截停了」。
    `warn=False` 给只为量字数的再次展开用，免得同一段文字记两遍。
    """
    result: MacroResult = expand_macros(text, replace(macro, text_kind=text_kind))
    stats["expanded"] = int(stats.get("expanded", 0)) + result.expanded
    stats["guarded"] = bool(stats.get("guarded")) or result.guarded
    if result.guarded and warn:
        logger.warning(
            f"宏展开触发长度保护，停在上一遍的结果: kind={text_kind}, 原文 {len(text)} 字, 结果 {len(result.text)} 字",
        )
    return result.text


def _slot_detail(enabled: bool, raw_text: str, final_text: str, layers: list[str]) -> dict[str, Any]:
    """一个槽位在明细里的形状：字数、来自哪几层、状态

    `state` 的四种取值与设计稿一致：开着且非空是 `pending`（渲染后由调用方改成 `sent`），
    开着但空是 `empty`，关着但原文非空是 `gated`（管理员能看出「打开后会发」），
    关着且原文为空是 `empty`。字数取本轮真正会发出去的那份文字的长度；关着时没有展开，
    用原文长度，好让管理员知道打开后大概会多多少字。
    """
    if enabled:
        state = "pending" if final_text else "empty"
        chars = len(final_text)
    else:
        state = "gated" if raw_text else "empty"
        chars = len(raw_text)
    return {"chars": chars, "layers": list(layers), "state": state}


def _gated_result(preset: PresetLike) -> TavernRound:
    """闸关 / 解析失败时的形状：人设正文原样，其余全空"""
    return TavernRound(
        enabled=False,
        card=None,
        macro=None,
        persona_content=preset.content,
        depth_prompt="",
        depth=0,
        auxiliary="",
        post_history="",
        prefill=None,
        slots=None,
    )


async def resolve_tavern_round(
    code: str,
    channel: DBChatChannel,
    config: Any,
    preset: PresetLike,
    trigger_message: Optional[ChatMessage],
    *,
    dry_run: bool = False,
) -> TavernRound:
    """解析这一轮的人设卡运行时上下文，按 `code` 缓存

    Args:
        code: 本轮的一次性令牌（缓存键）。
        channel: 当前频道。
        config: 这个频道的**生效**配置；高级槽位的开关从这里读。
        preset: 本轮生效的人设（`get_preset()` 的结果，可能没有行 ID）。
        trigger_message: 触发这一轮的消息；用来取 `{{user}}`。
        dry_run: 预览 / 试发。解析照常做，但不写提示词明细。

    Returns:
        见 :class:`TavernRound`。异常时返回闸关形状并记 WARNING。
    """
    cached = _rounds.get(code)
    if cached is not None:
        return cached

    # 回滚闸是全局项、不可覆盖，所以在碰任何表之前就判掉：闸关时一次查询都不该发生。
    if not bool(global_config.TAVERN_COMPAT_ENABLED):
        result = _gated_result(preset)
        _store_round(code, result)
        return result

    try:
        result = await _resolve_enabled_round(code, channel, config, preset, trigger_message, dry_run=dry_run)
    except Exception as error:  # noqa: BLE001 - 读路径 fail-open：卡片数据故障不该让 Bot 哑掉
        logger.warning(f"酒馆运行时解析失败，这一轮按闸关处理: chat_key={channel.chat_key}, error={error!r}")
        result = _gated_result(preset)
    _store_round(code, result)
    return result


async def _resolve_enabled_round(
    code: str,
    channel: DBChatChannel,
    config: Any,
    preset: PresetLike,
    trigger_message: Optional[ChatMessage],
    *,
    dry_run: bool,
) -> TavernRound:
    """闸开时的实际解析；异常交给 :func:`resolve_tavern_round` 兜成闸关形状"""
    preset_id = _preset_id_of(preset)
    # 内置默认人设没有行 ID：它不是库里的人设，查不到也不该查。
    card = await DBPresetCard.get_or_none(preset_id=preset_id) if preset_id > 0 else None

    macro = MacroContext(
        char_name=(card.nickname if card is not None and card.nickname else str(preset.name or "")),
        user_name=await _resolve_user_name(channel, trigger_message),
        now=datetime.now(),
        lang=str(global_config.SYSTEM_LANG),
        chat_key=channel.chat_key,
        preset_id=preset_id,
        examples_raw=_examples_raw(card),
    )
    stats: dict[str, Any] = {"expanded": 0, "guarded": False}

    # 人设正文只对有卡片数据、且卡片声明展开宏的人设展开；NA 原生人设逐字不变。
    if card is not None and bool(card.macros_enabled):
        persona_content = _expand(preset.content, macro, "persona", stats)
    else:
        persona_content = preset.content

    depth = 0
    depth_prompt = ""
    if card is not None and card.depth_prompt:
        depth_prompt = _expand(card.depth_prompt, macro, "depth_prompt", stats)
        depth = int(card.depth_prompt_depth or 0)

    slots = await resolve_slots(channel, config, card)
    auxiliary = ""
    post_history = ""
    prefill: Optional[PrefillSpec] = None
    if slots.enabled:
        # `{{original}}` 已经在 `resolve_slots` 里逐层串好了，这里按空原文展开其余宏。
        auxiliary = _expand(slots.auxiliary.text, macro, "auxiliary", stats)
        post_history = _expand(slots.post_history.text, macro, "post_history", stats)
        prefill_text = _expand(slots.prefill.text, macro, "prefill", stats)
        prefill = PrefillSpec(text=prefill_text) if prefill_text else None

    result = TavernRound(
        enabled=True,
        card=card,
        macro=macro,
        persona_content=persona_content,
        depth_prompt=depth_prompt,
        depth=depth,
        auxiliary=auxiliary,
        post_history=post_history,
        prefill=prefill,
        slots=slots,
    )
    if slots.enabled:
        result.rp_preset = await _resolve_rp_preset(channel, config)

    if not dry_run:
        prompt_rounds.note_tavern(
            code,
            gate=slots.enabled,
            post_history=_slot_detail(
                slots.enabled,
                slots.post_history.text,
                post_history,
                slots.post_history.layers,
            ),
            auxiliary=_slot_detail(
                slots.enabled,
                slots.auxiliary.text,
                auxiliary,
                slots.auxiliary.layers,
            ),
            prefill=_slot_detail(
                slots.enabled,
                slots.prefill.text,
                prefill.text if prefill is not None else "",
                slots.prefill.layers,
            ),
            depth_prompt={"chars": len(depth_prompt)},
            macros=stats,
        )
    return result


def expander(code: str, *, record: bool = True) -> Callable[[str], str]:
    """取这一轮的宏上下文，返回「展开一段文字」的函数（世界书条目用）

    没有缓存、闸关或解析失败时返回恒等函数：这些情况下提示词必须与 L4 逐字相同。
    展开本身再包一层兜底——世界书渲染失败会让整条提示词消失，而宏只是文字替换，
    出错时保留原文是安全方向。

    `record=False` 用于只为统计字数、生成明细或预览元数据而再展开一次的地方：不累加到
    本轮的宏统计、不记长度保护的 WARNING。真正注入的那次展开已经记过，再记一遍会让
    `expanded` 翻倍，还可能因为一段没发出去的文字误报 `guarded`。
    """
    round_ = _rounds.get(code)
    if round_ is None or not round_.enabled or round_.macro is None:
        return _identity

    macro = round_.macro

    def expand(text: str) -> str:
        stats: dict[str, Any] = {"expanded": 0, "guarded": False}
        try:
            expanded = _expand(text, macro, "worldbook", stats, warn=record)
        except Exception as error:  # noqa: BLE001 - 宏是文字替换，出错时保留原文
            logger.warning(f"世界书宏展开失败，保留原文: code={code}, error={error!r}")
            return text
        # 解析阶段的人设 / 槽位 / 深度注入已经记过一轮，这里只补世界书那部分的增量；
        # `note_tavern` 对 `macros` 做累加，最终合起来就是全轮总数。没有草稿（预览）时是空操作。
        if record and (stats["expanded"] or stats["guarded"]):
            prompt_rounds.note_tavern(code, macros=stats)
        return expanded

    return expand


def _identity(text: str) -> str:
    """闸关 / 没有上下文时的展开函数：原样返回"""
    return text


def _store_round(code: str, result: TavernRound) -> None:
    """写缓存；满了按插入顺序丢最早的一半（与 L4 世界书、提示词明细同一个做法）"""
    if len(_rounds) >= MAX_ROUNDS:
        _drop_oldest_half()
    _rounds[code] = result


def _drop_oldest_half() -> None:
    for code in list(_rounds)[: max(1, len(_rounds) // 2)]:
        _rounds.pop(code, None)


__all__ = [
    "MAX_ROUNDS",
    "TavernRound",
    "cached_tavern_round",
    "discard_tavern_round",
    "expander",
    "resolve_tavern_round",
]
