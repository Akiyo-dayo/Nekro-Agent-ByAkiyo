"""世界书运行时的每轮求值与缓存

本模块与 `service.py` 一样查库，因此不在包级 `__init__` 里 re-export：纯函数用例 import
本包时不该顺带把 ORM 查询层拉起来。

## 为什么要有这一层

触发测试（管理端）与每轮运行时（对话主链路）必须用同一套判据算出「这一轮注入什么」。
两边各写一遍的话，管理员在测试页看到的结论会与真实注入慢慢分叉，而分叉的那一侧不会
报错，只会让排查变成猜。求值部分因此收在 :func:`evaluate` 里，两条路径都只调它，
差别只在参数：触发测试不带定时锚点、也不做下一轮抑制，运行时两者都要。

## 一轮只解析一次

`resolve_round` 按 `one_time_code` 缓存结果：一轮里可能渲染多次提示词（重试、续写、
历史重渲染），每次都要查书、查条目、数消息的话，不仅慢，还会让同一轮的多次渲染注入
不同的条目——模型看到的上下文就自相矛盾了。缓存只在轮次存活期内有效，`finish_round`
在收尾时弹出。

定时状态的锚点同理：一轮里多次渲染算出的候选锚点先 `stage` 进暂存区，只有结局为 `ok`
才 `commit`（见 `timed_state` 模块文档）。

## 读路径 fail-open

整段求值包在 `try/except Exception` 里，失败时记 WARNING 并返回 `state="error"`、空分组：
提示词照常发出去，只是这一轮没有世界书。与记忆注入同一个取舍——一条数据库故障不该让
Bot 在群里哑掉，而管理员从明细里能看到 `error`。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.services.affection.suppression import is_narrative_suppressed
from nekro_agent.services.identity_scope import resolve_domain_members, resolve_memory_domain, resolve_preset_id
from nekro_agent.services.memory.instance_scope import apply_worldbook_condition, worldbook_condition
from nekro_agent.services.memory.persons import expand_accounts, human_accounts, resolve_bot_account

from . import matcher, safe_regex, selection
from . import service as wb_service
from .timed_state import TimedGate, apply_timed, delay_satisfied, entry_gate, timed_state_store
from .types import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ENTRIES_PER_ROUND,
    SCAN_DEPTH_MAX,
    WindowMessage,
    book_to_match,
    entry_to_match,
)

logger = get_sub_logger("worldbook.runtime")

# 轮次缓存上限。一轮结束就会 `finish_round` 弹出，正常情况这个字典很小；上限只是兜
# 「某条路径忘了收尾」的底，超了按插入顺序丢最早的一半（与提示词明细同一个做法）。
MAX_ROUNDS = 4096

# 频道消息条数的饱和值。sticky / cooldown / delay 最大 200，数到 201 就够判「不足 n 条」，
# 不必把十年老群的几十万条都数一遍。
CHANNEL_COUNT_SATURATION = 201


@dataclass(frozen=True)
class Evaluation:
    """一次求值的完整中间结果

    Attributes:
        books: 对这个频道生效的书（含个人书在场判定之后）。
        entries: 这些书里参与判定的条目（抑制时已去掉不可信条目）。
        hits: 叠加定时效果之后最终命中的条目。
        chosen: 预算入选结果（含被跳过的条目与原因）。
        groups: 按注入位置分好组、组内按渲染顺序排好的入选条目。
        anchor_entry_ids: 入选且需要设新锚点的条目。被预算跳过的没生效，不设锚点，
            否则一条从未真正注入过的条目会开始 sticky 或进 cooldown。
        suppressed_untrusted: 因为下一轮抑制而跳过的不可信条目数。
    """

    books: tuple[DBWbBook, ...]
    entries: tuple[DBWbEntry, ...]
    hits: tuple[selection.Hit, ...]
    chosen: selection.Selection
    groups: selection.RenderGroups
    anchor_entry_ids: tuple[int, ...]
    suppressed_untrusted: int = 0


async def evaluate(
    channel: DBChatChannel,
    *,
    preset_id: int,
    window: Sequence[WindowMessage],
    max_chars: int,
    max_entries: int,
    use_timed_state: bool,
    suppress_untrusted: bool,
) -> Evaluation:
    """算一遍「这个频道这一轮注入什么」

    Args:
        channel: 当前频道；适配器 / 实例标识都从它身上取（范围条件只认这两列）。
        preset_id: 这一轮生效的人设；0 表示无人设档，`preset` 腿的书一律不生效。
        window: 扫描窗口，按 `id` 升序。
        max_chars: 每轮正文字数上限。
        max_entries: 每轮条数上限。
        use_timed_state: 是否叠加 sticky / cooldown 的锚点状态。触发测试传 False：
            测试页不该改变正式状态，也不该因为上一次测试而给出不同结论。
        suppress_untrusted: 是否在判定前去掉不可信条目（上一轮被内容审核拦下时）。

    Returns:
        见 :class:`Evaluation`。
    """
    domain_pairs = await resolve_domain_members(channel.adapter_key, channel.instance_key)
    bot_account = await resolve_bot_account(channel.adapter_key, channel.instance_key)
    # `human_accounts` 只读 `sender_id` 与 `platform_userid` 两列，窗口消息正好带着它们；
    # 传 `WindowMessage` 而不是回查消息行，是为了让「窗口里有什么」这件事只有一处来源。
    accounts = human_accounts(window, bot_account=bot_account)
    domain = await resolve_memory_domain(channel.adapter_key, channel.instance_key)
    # 按人逐个展开，不能拿整组展开结果给每个账号共用：那样最后说话的任何一个人都会把
    # 窗口里所有人的别名带进来，个人书自己的扫描深度就形同虚设。
    alias_groups = {account: await expand_accounts(domain, [account]) for account in accounts}
    expanded = frozenset(accounts).union(*alias_groups.values())

    cond = worldbook_condition(
        domain_pairs,
        chat_key=channel.chat_key,
        preset_id=preset_id,
        present_accounts=expanded,
    )
    books = tuple(await apply_worldbook_condition(DBWbBook.filter(enabled=True), cond))
    books = tuple(
        book
        for book in books
        if matcher.book_active_for_person(
            book_to_match(book),
            window,
            bot_account=bot_account,
            alias_groups=alias_groups,
        )
    )

    entries = tuple(await wb_service.active_entries_for_books([book.id for book in books]))
    suppressed_untrusted = 0
    if suppress_untrusted:
        # 判定之前就去掉：让出来的预算要给宿主条目用得上。放到入选之后筛，被跳过的仍是
        # 租户条目占着预算，宿主条目还是进不来。
        kept = tuple(entry for entry in entries if entry.trusted)
        suppressed_untrusted = len(entries) - len(kept)
        entries = kept

    books_by_id = {book.id: book for book in books}
    runner = safe_regex.RegexRunner()
    # 触发测试按窗口条数判 `delay`（与改造前逐字一致），锚点一律当作没有；运行时才去数
    # 频道总条数，且只在真有条目设了 `delay` 时才数——`delay` 数的是频道总条数，
    # 与「本轮窗口里有多少条」不是一回事。
    channel_count = len(window)
    if use_timed_state and _any_delay(entries, books_by_id):
        channel_count = await _channel_count(channel)
    anchors = _AnchorCounter(chat_key=channel.chat_key)

    hits: list[selection.Hit] = []
    wants_anchor: list[int] = []
    for entry in entries:
        book = books_by_id.get(entry.book_id)
        if book is None:
            continue
        match_entry = entry_to_match(entry, book_to_match(book))
        if not delay_satisfied(match_entry.delay, channel_count):
            # `delay` 不满足的条目既不命中也不设锚点（`apply_timed` 的第一条规则），先判掉
            # 是为了不替它跑正则：正则有每轮总预算，花在注定不命中的条目上，排在后面、真该
            # 判的条目可能因为预算用完而判不了。改造前的触发测试也是先判 `delay`。
            continue
        if use_timed_state:
            anchor: int | None = None
            since: int | None = None
            if match_entry.sticky > 0 or match_entry.cooldown > 0:
                anchor = timed_state_store.get(channel.chat_key, match_entry.id)
                if anchor is not None:
                    since = await anchors.count(anchor)
            gate = entry_gate(match_entry, anchor_message_id=anchor, msgs_since_anchor=since)
            hit = None
            # 冷却中、或 sticky 期里的非常驻条目：结果与关键词无关，不跑正则
            # （正则那点预算要留给真需要判的条目）。
            if gate is TimedGate.FREE or (gate is TimedGate.STICKY and match_entry.constant):
                hit = matcher.entry_hit(match_entry, window, runner)
            final, wants = apply_timed(match_entry, hit, gate, channel_count)
        else:
            # 触发测试：只判 `delay`，命中原因照旧来自关键词 / 常驻。
            final, wants = apply_timed(
                match_entry,
                matcher.entry_hit(match_entry, window, runner),
                TimedGate.FREE,
                channel_count,
            )
        if final is None:
            continue
        hits.append(selection.Hit(entry=match_entry, reason=final))
        if wants:
            wants_anchor.append(match_entry.id)

    chosen = selection.select_hits(
        hits,
        max_chars=max_chars,
        max_entries=max_entries,
        book_budgets={book.id: book.budget_chars for book in books},
    )
    groups = selection.group_for_render(chosen.selected)
    selected_ids = {hit.entry.id for hit in chosen.selected}
    anchor_entry_ids = tuple(entry_id for entry_id in wants_anchor if entry_id in selected_ids)

    return Evaluation(
        books=books,
        entries=entries,
        hits=tuple(hits),
        chosen=chosen,
        groups=groups,
        anchor_entry_ids=anchor_entry_ids,
        suppressed_untrusted=suppressed_untrusted,
    )


def _any_delay(entries: Sequence[DBWbEntry], books_by_id: Mapping[int, DBWbBook]) -> bool:
    """是否有条目设了 `delay`；没有就不必去数频道总条数"""
    for entry in entries:
        book = books_by_id.get(entry.book_id)
        if book is None:
            continue
        if entry_to_match(entry, book_to_match(book)).delay > 0:
            return True
    return False


async def _channel_count(channel: DBChatChannel) -> int:
    """这个频道一共能数到几条消息，饱和在 :data:`CHANNEL_COUNT_SATURATION`

    不加时间下限：`delay` 数的是频道总条数，与「最近这一轮看了什么」无关。
    """
    rows = await (
        DBChatMessage.filter(chat_key=channel.chat_key)
        .order_by("-id")
        .limit(CHANNEL_COUNT_SATURATION)
        .values_list("id", flat=True)
    )
    return len(rows)


async def _count_since(chat_key: str, anchor_id: int) -> int:
    """锚点之后又来了几条消息，饱和在 :data:`CHANNEL_COUNT_SATURATION`"""
    rows = await (
        DBChatMessage.filter(chat_key=chat_key, id__gt=anchor_id)
        .limit(CHANNEL_COUNT_SATURATION)
        .values_list("id", flat=True)
    )
    return len(rows)


@dataclass
class _AnchorCounter:
    """按锚点值去重的「过了几条」查询器

    同一个锚点上可能挂着好几条条目（同一个频道里多条 sticky / cooldown 条目往往在同一轮
    被设上同一个锚点）。逐条查一次是白花几倍的查询预算，所以这里按锚点值记一次结果。
    只在一次 `evaluate` 里活着，不跨轮缓存：消息条数每轮都在变。
    """

    chat_key: str
    _cache: dict[int, int] = field(default_factory=dict)

    async def count(self, anchor_id: int) -> int:
        """锚点之后的消息条数；同一个锚点只查一次"""
        cached = self._cache.get(anchor_id)
        if cached is not None:
            return cached
        value = await _count_since(self.chat_key, anchor_id)
        self._cache[anchor_id] = value
        return value


@dataclass(frozen=True)
class RoundWorldbook:
    """一轮世界书解析的结果

    Attributes:
        state: `disabled`（闸关）/ `none`（没有命中）/ `injected`（有入选条目）/
            `suppressed`（没有入选，但有条目因为上一轮被拦而跳过）/ `error`（求值失败）。
        groups: 入选条目按注入位置分好的组；`state` 不是 `injected` 时全空。
        dropped: 被预算跳过的条目 `(entry_id, book_id, chars, reason)`，只供明细解释
            「为什么这条没进来」，不参与撞甲计数。
    """

    state: str
    groups: selection.RenderGroups
    dropped: tuple[tuple[int, int, int, str], ...] = ()


_rounds: dict[str, RoundWorldbook] = {}


def cached_round(code: str) -> RoundWorldbook | None:
    """只读缓存，不查库；没解析过（或已经收尾弹出）就是 `None`"""
    return _rounds.get(code)


async def resolve_round(
    code: str,
    channel: DBChatChannel,
    channel_config: Any,
    *,
    preset_id_override: Optional[int] = None,
) -> RoundWorldbook:
    """解析这一轮的世界书注入，按 `code` 缓存

    Args:
        code: 本轮的一次性令牌（`one_time_code`），缓存键。
        channel: 当前频道。
        channel_config: 这个频道的**生效**配置（`get_effective_config()` 的返回值），
            世界书的两项（开关、字数上限）都从它读。
        preset_id_override: 提示词预览「换一个人设看看」时传所选人设；人设书按它判，
            不按频道当前人设。正式回合不传。

    闸关时**不发任何世界书查询，也不查消息**：关掉世界书这件事必须能从查询日志上看出来，
    否则「关着为什么还慢」永远查不清。
    """
    cached = _rounds.get(code)
    if cached is not None:
        return cached

    if not bool(getattr(channel_config, "WORLDBOOK_ENABLED", False)):
        result = RoundWorldbook(state="disabled", groups=selection.RenderGroups())
        _store_round(code, result)
        return result

    try:
        result = await _resolve_enabled_round(code, channel, channel_config, preset_id_override=preset_id_override)
    except Exception as error:  # noqa: BLE001 - 读路径 fail-open：一条故障不该让 Bot 哑掉
        logger.warning(f"世界书解析失败，这一轮不注入: chat_key={channel.chat_key}, error={error!r}")
        result = RoundWorldbook(state="error", groups=selection.RenderGroups())
    _store_round(code, result)
    return result


async def _resolve_enabled_round(
    code: str,
    channel: DBChatChannel,
    channel_config: Any,
    *,
    preset_id_override: Optional[int] = None,
) -> RoundWorldbook:
    """闸开时的实际求值；异常交给 :func:`resolve_round` 兜成 `error`"""
    # 过期秒数取频道生效配置：历史消息那条查询用的是同一份配置（`render_history_data`
    # 的 `config` 参数），这一项可以按实例 / 频道覆盖，读全局值会让两个窗口的下限对不上。
    expire_seconds = float(
        getattr(channel_config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", config.AI_CHAT_CONTEXT_EXPIRE_SECONDS),
    )
    window = await load_window(channel, expire_seconds=expire_seconds)
    preset_id = preset_id_override if preset_id_override is not None else await resolve_preset_id(channel)
    suppressed = is_narrative_suppressed(channel.chat_key, config.AI_CHAT_CONTEXT_EXPIRE_SECONDS)
    evaluation = await evaluate(
        channel,
        preset_id=preset_id,
        window=window,
        max_chars=int(getattr(channel_config, "WORLDBOOK_MAX_CHARS", DEFAULT_MAX_CHARS)),
        max_entries=int(getattr(config, "WORLDBOOK_MAX_ENTRIES_PER_ROUND", DEFAULT_MAX_ENTRIES_PER_ROUND)),
        use_timed_state=True,
        suppress_untrusted=suppressed,
    )

    if window and evaluation.anchor_entry_ids:
        # 锚点取窗口里最新那条消息：sticky / cooldown 的「过了几条」从这里往后数。
        newest = max(message.id for message in window)
        for entry_id in evaluation.anchor_entry_ids:
            timed_state_store.stage(code, channel.chat_key, entry_id, newest)

    if evaluation.chosen.selected:
        state = "injected"
    elif evaluation.suppressed_untrusted > 0:
        state = "suppressed"
    else:
        state = "none"

    return RoundWorldbook(
        state=state,
        groups=evaluation.groups,
        dropped=tuple(
            (hit.entry.id, hit.entry.book_id, len(hit.entry.content), reason)
            for hit, reason in evaluation.chosen.dropped
        ),
    )


async def load_window(channel: DBChatChannel, *, expire_seconds: float | None = None) -> list[WindowMessage]:
    """这个频道的扫描窗口，按 `id` 升序

    时间下限与 `render_history_data` 相同（见 `templates/history.py` 里那条查询）：
    已经过期、不在历史里的旧消息不该触发设定——模型看不到它们，由它们召唤出来的条目
    等于凭空出现。只取四列：判命中只需要「谁发的」与「说了什么」。

    Args:
        channel: 当前频道。
        expire_seconds: 频道生效配置里的 `AI_CHAT_CONTEXT_EXPIRE_SECONDS`；不给时用全局值。
    """
    if expire_seconds is None:
        expire_seconds = float(config.AI_CHAT_CONTEXT_EXPIRE_SECONDS)
    floor = max(
        time.time() - expire_seconds,
        channel.conversation_start_time.timestamp(),
    )
    rows = await (
        DBChatMessage.filter(chat_key=channel.chat_key, send_timestamp__gte=floor)
        .order_by("-id")
        .limit(SCAN_DEPTH_MAX)
        .values("id", "sender_id", "platform_userid", "content_text")
    )
    # 倒序取最近 N 条再翻回升序：`scan_text` 与 `book_active_for_person` 都按 `id` 升序
    # 理解「最后几条」，直接把倒序列表传进去会让「最后 depth 条」变成「最早 depth 条」。
    return [
        WindowMessage(
            id=int(row["id"]),
            sender_id=str(row["sender_id"] or ""),
            platform_userid=str(row["platform_userid"] or ""),
            content_text=str(row["content_text"] or ""),
        )
        for row in reversed(rows)
    ]


def finish_round(code: str, outcome: str) -> None:
    """收尾一轮：结局 `ok` 提交定时锚点，其余一律丢弃；无论哪种都弹出缓存

    绝不抛：它跑在 `finally` 里，收尾失败不该盖住真正的异常。
    """
    try:
        if outcome == "ok":
            timed_state_store.commit(code)
        else:
            timed_state_store.discard(code)
    except Exception as error:  # noqa: BLE001 - 收尾路径不能抛
        logger.warning(f"世界书轮次收尾失败: code={code}, outcome={outcome}, error={error!r}")
    finally:
        _rounds.pop(code, None)


def _store_round(code: str, result: RoundWorldbook) -> None:
    """写缓存；满了按插入顺序丢最早的一半（同 `prompt_rounds._drop_oldest_half`）"""
    if len(_rounds) >= MAX_ROUNDS:
        _drop_oldest_half()
    _rounds[code] = result


def _drop_oldest_half() -> None:
    for code in list(_rounds)[: max(1, len(_rounds) // 2)]:
        _rounds.pop(code, None)


__all__ = [
    "CHANNEL_COUNT_SATURATION",
    "MAX_ROUNDS",
    "Evaluation",
    "RoundWorldbook",
    "cached_round",
    "evaluate",
    "finish_round",
    "load_window",
    "resolve_round",
]
