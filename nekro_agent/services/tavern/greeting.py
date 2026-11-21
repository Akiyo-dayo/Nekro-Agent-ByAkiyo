"""人设开场白：不再发进聊天，只保留「上次为哪个人设记过」的记录与旧初值的清理

自 0.4.7 起开场白不再发进聊天（群里突然冒出一段独白并不自然）。本模块现在只做两件事：
每轮解析出生效人设之后，按记录判断要不要把「上次为哪个人设记过」更新成当前人设；写记录
时清掉上一位人设留下的 `<initvar>` 初值。挑选（:func:`pick_greeting`）与解析
（:func:`extract_initvar_block`）函数仍然保留：导入、预览与角色状态插件还在用它们。

## 为什么用频道 `data` 里的一个「上次为谁记过」的标记判断

「切到这个人设」的入口不止一个：频道绑定、实例默认人设、`AgentCtx.set_preset`、人设被删
之后的回退链。逐个入口打标记迟早会漏一个，而漏掉的那个频道就永远记不上。这里改成
每轮解析出生效人设之后看一眼记录：记录与当前人设不同才更新，所有入口自然都覆盖到。

记录有三种状态：键不存在（升级前的老频道、或还没跑过一轮的新频道）、`-1`（`/reset`
写的）、某个人设 ID。键不存在时用「频道里有没有 Bot 发过的消息」区分老频道与新频道：
老频道只记，不碰别的；新频道也照记。

## 旧初值不能留

开场白不再发出，就不会再有新的初值。切人设或 `/reset` 时，上一位人设留下的那份
`<initvar>` 正文必须清掉（:data:`GREETING_INITVAR_KEY`），否则角色状态插件会拿它初始化
新人设。写记录时一律清掉，不需要再区分「发出去了没有」。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from nekro_agent.core.config import CoreConfig
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel, DefaultPreset
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.schemas.chat_message import ChatType

from .cleanup import PLACEHOLDER_BUDGET_SECONDS, PlaceholderRule, placeholder_rule, placeholder_rules
from .runtime import TavernRound

logger = get_sub_logger("tavern.greeting")

# 频道 `data` 里记「上次为哪个人设记过」的键。`-1` 专用于 `/reset`：
# 不删键是因为删掉以后，有过 Bot 消息的频道会被当成老频道。
GREETED_KEY = "tavern_greeted_preset_id"
RESET_MARK = -1

# 频道 `data` 里记开场白 `<initvar>` 正文的键（`{"preset_id": 人设 ID, "text": 正文}`）。
# 开场白不再发出，运行时不会再写入新的初值，只会在写记录时清掉上一位人设留下的旧值；
# 保留键名与解析函数，供角色状态插件与旧数据照常工作。
GREETING_INITVAR_KEY = "tavern_greeting_initvar"
MAX_GREETING_INITVAR_CHARS = 20_000
# 标签的认法与发送路径的剥离（`cleanup_runtime`）一致：名字按 ASCII 忽略大小写、后面要有词边界，
# 属性最多 1000 字且不含尖括号；自闭合的 `<initvar/>` 不是块。剥离会去掉的块，这里都能取到。
_INITVAR_OPEN = re.compile(r"<\s{0,8}(?a:initvar)\b([^<>]{0,1000})>", re.IGNORECASE)
_INITVAR_CLOSE = re.compile(r"<\s{0,8}/\s{0,8}(?a:initvar)\b[^<>]{0,1000}>", re.IGNORECASE)
_INITVAR_HINT = re.compile(r"<\s{0,8}/?\s{0,8}(?a:initvar)\b", re.IGNORECASE)

PresetLike = DBPreset | DefaultPreset


def extract_initvar_block(text: str) -> Optional[str]:
    """开场白里第一个成对的 `<initvar …>…</initvar>` 的正文

    没有、没闭合、去掉首尾空白后为空、里面还套着 `<initvar`、或超过 :data:`MAX_GREETING_INITVAR_CHARS`
    时为 `None`。开始标签只扫一遍、结束标签只找一次，线性。
    """
    if "<" not in text:
        return None
    for opening in _INITVAR_OPEN.finditer(text):
        if opening.group(1).rstrip().endswith("/"):
            continue
        closing = _INITVAR_CLOSE.search(text, opening.end())
        if closing is None:
            return None
        body = text[opening.end() : closing.start()].strip()
        if not body or len(body) > MAX_GREETING_INITVAR_CHARS or _INITVAR_HINT.search(body) is not None:
            return None
        return body
    return None


@dataclass(frozen=True)
class GreetingDecision:
    """一次开场白判断的结果

    Attributes:
        send: 这一轮要不要发；开场白不再发出，恒为 `False`。
        text: 历史上要发的开场白原文（宏还没展开）；现在恒为 `None`，形状保持不变。
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


def _usable(greetings: list[str], rules: list[PlaceholderRule], deadline: float) -> list[str]:
    """去掉只是标记的开场白；没有规则时原样返回"""
    if not rules:
        return greetings
    return [item for item in greetings if placeholder_rule(item, rules, deadline=deadline) is None]


def pick_greeting(channel: DBChatChannel, card: DBPresetCard) -> Optional[str]:
    """按频道类型与挑选方式选一条开场白；没有可用的一条时返回 `None`

    群聊优先用群聊专用开场白，但那个列表全是空串时仍回落到普通开场白——导入器会把
    缺失的字段写成空列表，空列表与「全是空串」都按没有处理。

    只是给 HTML 规则用的标记的开场白（`cleanup.placeholder_rule`）跳过：它在酒馆里会被换成网页界面，
    在 QQ 里只剩一个标记。导入报告里已经写明。规则只预处理一次，
    判所有开场白合计限时（`PLACEHOLDER_BUDGET_SECONDS`），到点后剩下的按不是标记算。
    """
    raw_rules = getattr(card, "cleanup_rules", None)
    deadline = time.monotonic() + PLACEHOLDER_BUDGET_SECONDS
    rules = placeholder_rules(raw_rules if isinstance(raw_rules, list) else [], deadline=deadline)
    group_greetings = _usable(_valid_greetings(card.group_greetings), rules, deadline)
    greetings = _usable(_valid_greetings(card.greetings), rules, deadline)
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
    """按判据表判断这一轮要不要写记录；开场白不再发出，`send` 恒为 `False`；不写任何东西"""
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
    # 开场白不再发进聊天：原来会走到发送分支的情况，一律只记不发送。
    return GreetingDecision(send=False, text=None, record=True)


async def _write_record(channel: DBChatChannel, preset_id: int) -> None:
    """把记录写成当前人设 ID，并清掉上一位人设留下的 `<initvar>` 初值

    开场白不再发出，就不会再有新的初值；切人设或 `/reset` 时，上一位人设留下的那份必须
    清掉（:data:`GREETING_INITVAR_KEY`），否则角色状态插件会拿它初始化新人设。

    先 `refresh_from_db` 再读改写，缩小与别处写 `data` 互相覆盖的窗口。仍有极小的竞态：
    两次读改写之间另一处也保存了 `data`，后保存的那次会覆盖前一次。现有的
    `set_custom_channel_name`、自动停用标记都是同一种写法，这里不另造一套。
    """
    try:
        await channel.refresh_from_db(fields=["data"])
        data = channel._load_data()  # noqa: SLF001 - 与频道 `data` 的其它读写共用同一份解析
        data[GREETED_KEY] = preset_id
        data.pop(GREETING_INITVAR_KEY, None)
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
    """按判据判断并写记录；开场白不再发出，恒返回 `False`

    记录照写（什么时候为哪个人设记过、老频道与新频道的判据都不变），写记录时清掉上一位
    人设留下的 `<initvar>` 初值。签名与调用方保持不变。

    Args:
        channel: 当前频道；记录写在它的 `data` 里。
        preset: 本轮生效的人设。
        card: 人设的酒馆扩展行；现在只用于保持调用形状，判断不再需要它。
        config: 本频道的生效配置。
        ctx: 本轮上下文；现在不再使用。
        tavern: 本轮酒馆运行时；现在不再使用。
        one_time_code: 本轮的一次性令牌；现在不再使用。

    Returns:
        恒为 `False`。
    """
    preset_id = _preset_id_of(preset)
    decision = await decide(channel, preset_id, card, config)
    if decision.record:
        await _write_record(channel, preset_id)
    return False


async def preview_greeting(
    *,
    channel: DBChatChannel,
    preset: PresetLike,
    card: Optional[DBPresetCard],
    config: CoreConfig,
    tavern: TavernRound,
) -> dict[str, Any]:
    """预览用：开场白不再发出，因此永远返回不发送

    返回的形状与预填的预览元数据一致：`will_send` 恒为 `False`，`text` 恒为空串。签名保持
    不变，预览页面不需要改。
    """
    return {"will_send": False, "text": ""}


__all__ = [
    "GREETED_KEY",
    "GREETING_INITVAR_KEY",
    "MAX_GREETING_INITVAR_CHARS",
    "RESET_MARK",
    "GreetingDecision",
    "decide",
    "extract_initvar_block",
    "maybe_greet",
    "pick_greeting",
    "preview_greeting",
]
