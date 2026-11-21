"""每轮提示词明细的进程内采集、落库与自动隔离

## 为什么要有

排查「模型这一轮到底看到了什么」时，光看聊天记录不够：记忆块、频道摘要与好感块
都是宿主拼进去的，同一段对话在不同轮次注入的条目可能完全不同。本模块只在进程内
按 `one_time_code` 收集这一轮的渲染结果，轮次结束后异步写一行明细，供管理端回看。

采集只**记**，不改任何提示词文字：所有函数拿到的都是已经渲染完成的字符串或长度，
写入失败也只在后台记日志。这样即使明细功能出问题，主对话的提示词与回复仍逐字不变。

## 为什么只活一轮

记录与 `round_recall` 同一个键、同一套上限：正常情况每轮结束都会弹出，字典天然很小。
超过上限时按插入顺序丢掉最早记下的一半——丢掉的只是「这一轮的明细」，最坏结果是
少一行排查数据，不值得为它让主对话失败。

## 自动隔离

被内容审核拦下或被供应商拒答的轮次如果注入过某条记忆，就给那条记忆加一次计数；
同一条记忆累计到阈值后转成隔离。判据只看「被拦轮次里注入过谁」，不让模型判断
哪条内容有问题；关系与实体只在明细里显示，不参与计数。
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from tortoise.expressions import F
from tortoise.transactions import in_transaction

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_mem_channel_summary import DBMemChannelSummary
from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_prompt_round import DBMemPromptRound
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory.instance_scope import AdminFilter, AdminMemoryScope
from nekro_agent.services.memory.scope_condition import MemTable

if TYPE_CHECKING:
    from nekro_agent.models.db_chat_channel import DBChatChannel

logger = get_sub_logger("memory.prompt_rounds")

# 条目上限：正常情况每轮结束都会弹出，天然很小。超出时按插入顺序丢掉最早记下的一半。
_MAX_ROUNDS = 4096

# 后台落库任务。用集合持有强引用，避免任务对象在完成前被回收。
_TASKS: set[asyncio.Task[None]] = set()

_OUTCOMES = frozenset({"ok", "blocked", "failed", "cancelled"})


@dataclass
class _MemoryItemDraft:
    """一条记忆在本轮里的引用与渲染字数。"""

    item_type: str
    item_id: int
    chars: int = 0
    renders: int = 0
    # 这一条在本轮检索里的相似度（闸开时记）；旧行与闸关路径没有这个键，以后调阈值靠它。
    score: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        """转成 JSON 列里的条目。"""
        item: dict[str, Any] = {
            "type": self.item_type,
            "id": self.item_id,
            "chars": self.chars,
            "renders": self.renders,
        }
        if self.score is not None:
            item["score"] = round(self.score, 4)
        return item


@dataclass
class _WorldbookItemDraft:
    """一条世界书条目在本轮里的引用与渲染次数。

    `tokens` 是预算口径（宏展开前的正文按这一轮的分词系数数）；`chars` 仍记渲染出来的字数，
    方便对照。0.4.7 之前的旧行没有 `tokens`。
    """

    entry_id: int
    book_id: int
    chars: int = 0
    position: str = ""
    trusted: bool = False
    renders: int = 0
    tokens: Optional[int] = None
    # 命中方式是语义时记 `semantic` 与相似度；旧行没有这两个键，读的地方要能缺省。
    reason: Optional[str] = None
    score: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        """转成 JSON 列里的条目。"""
        item: dict[str, Any] = {
            "type": "worldbook",
            "id": self.entry_id,
            "book_id": self.book_id,
            "chars": self.chars,
            "position": self.position,
            "trusted": self.trusted,
            "renders": self.renders,
        }
        if self.tokens is not None:
            item["tokens"] = self.tokens
        if self.reason is not None:
            item["reason"] = self.reason
        if self.score is not None:
            item["score"] = round(self.score, 2)
        return item


@dataclass
class _WorldbookLookupDraft:
    """模型在轮内主动查过的一条世界书条目

    `chars` / `tokens` 是这一轮真正交回给模型的正文长度；同一条查多次取最大值，
    预览与全文都查过时记全文那一份。它只用于明细与撞甲计数，不影响「注入了几条」。
    """

    entry_id: int
    book_id: int
    chars: int = 0
    tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        """转成 JSON 列里的条目。"""
        return {
            "type": "worldbook_lookup",
            "id": self.entry_id,
            "book_id": self.book_id,
            "chars": self.chars,
            "tokens": self.tokens,
        }


@dataclass
class _RoundDraft:
    """一轮里各次渲染的合并结果；只在轮次结束前活着。"""

    channel: "DBChatChannel"
    chat_key: str
    adapter_key: str
    instance_key: str
    started_at: datetime
    preset_id: Optional[int] = None
    workspace_id: Optional[int] = None
    render_count: int = 0
    # 这一轮的首次请求被上游按内容拦下、并少带内容重发过。落库时写进明细的
    # `tavern_items["lean_retry"]`，同时让这轮即使 outcome 是 ok 也推进撞甲计数：
    # 第一次带着这些记忆 / 世界书条目被拦过，它们本来就该记一次。
    blocked_attempt: bool = False
    memory_state: str = "disabled"
    memory_items: dict[tuple[str, int], _MemoryItemDraft] = field(default_factory=dict)
    memory_block_chars: int = 0
    cooldown_skipped: int = 0
    # 自动注入前的模型筛选结果：`mode` / `pool` / `kept`。落库时放进 `tavern_items`
    # 现有的 JSON 列（键名 `memory_judge`），不新增迁移；旧行没有这个键。
    memory_judge: Optional[dict[str, Any]] = None
    summary_state: str = "disabled"
    summary_id: Optional[int] = None
    summary_version: Optional[int] = None
    summary_chars: int = 0
    affection_recorded: bool = False
    affection_shown: bool = False
    affection_chars: int = 0
    affection_people: int = 0
    # 这一轮好感块里真正渲染出来的里程碑段落。它们也是记忆段落，因此照样参与被拦计数：
    # 一条被拦轮次里反复出现的关系里程碑，与被拦轮次里反复出现的记忆是同一种风险。
    affection_milestone_ids: list[int] = field(default_factory=list)
    worldbook_state: str = "disabled"
    worldbook_items: dict[int, _WorldbookItemDraft] = field(default_factory=dict)
    # 模型在轮内主动查过的条目：只进明细与撞甲计数，不参与世界书注入条数 / 字数的统计。
    worldbook_lookups: dict[int, _WorldbookLookupDraft] = field(default_factory=dict)
    # 被预算跳过的条目 `(条目 ID, 书 ID, 字数, token 数, 原因)`
    worldbook_dropped: list[tuple[int, int, int, int, str]] = field(default_factory=list)
    # 这一轮的预算与它的算法（`WorldbookBudget.as_dict()`），以及入选条目的 token 数
    worldbook_budget: dict[str, Any] = field(default_factory=dict)
    worldbook_tokens: dict[int, int] = field(default_factory=dict)
    # 酒馆兼容的运行时状态：闸、三个槽位、深度注入与宏计数。只写字数、层名与状态，
    # 不写任何原文——它是管理员手写（或卡片带进来）的文字，同 L3 只存引用同一个取舍。
    tavern_items: dict[str, Any] = field(default_factory=dict)
    # 角色状态（插件）的本轮状态：块的 token 数、截掉的组、上次更新的结果。状态值来自聊天，
    # 这里只记计数与名字，不记任何值——同 `tavern_items` 只记字数的取舍。
    state_items: dict[str, Any] = field(default_factory=dict)

    def memory_items_json(self) -> list[dict[str, Any]]:
        """按条目类型与 ID 排序后转成 JSON 列；顺序稳定便于比对。"""
        return [item.as_dict() for item in sorted(self.memory_items.values(), key=lambda item: (item.item_type, item.item_id))]

    def worldbook_items_json(self) -> list[dict[str, Any]]:
        """注入条目按 ID 排序，后面接被预算跳过的条目，最后接模型主动查过的条目。

        跳过的条目只供明细解释「为什么没进来」，**不参与撞甲计数**：它没进提示词，把它的
        被拦次数往上加等于让一条从没出现在上下文里的条目被隔离。主动查过的条目单独一种
        `type`：它不进 `worldbook_chars`，也不算「注入了几条」。
        """
        for item in self.worldbook_items.values():
            if item.tokens is None and item.entry_id in self.worldbook_tokens:
                item.tokens = self.worldbook_tokens[item.entry_id]
        injected = [item.as_dict() for item in sorted(self.worldbook_items.values(), key=lambda item: item.entry_id)]
        dropped = [
            {
                "type": "worldbook_dropped",
                "id": entry_id,
                "book_id": book_id,
                "chars": chars,
                "tokens": tokens,
                "reason": reason,
            }
            for entry_id, book_id, chars, tokens, reason in sorted(self.worldbook_dropped, key=lambda item: item[0])
        ]
        looked_up = [
            item.as_dict()
            for item in sorted(self.worldbook_lookups.values(), key=lambda item: item.entry_id)
        ]
        return injected + dropped + looked_up

    @property
    def worldbook_chars(self) -> int:
        """本轮注入的世界书正文总字数（渲染出来的字数，与 `tokens` 对照用）"""
        return sum(item.chars for item in self.worldbook_items.values())


_rounds: dict[str, _RoundDraft] = {}


def begin(code: str, channel: "DBChatChannel") -> None:
    """登记一轮的开始。

    草稿**总是**建：它同时是自动隔离的依据（被拦轮次里注入过谁）。明细保留轮数为 0
    只表示不写明细表，不能连带把自动隔离也关掉——两个开关在设计上相互独立。草稿只在
    进程内活一轮，开销可以忽略。
    """
    try:
        if not code:
            return
        if len(_rounds) >= _MAX_ROUNDS:
            _drop_oldest_half()
        _rounds[code] = _RoundDraft(
            channel=channel,
            chat_key=str(getattr(channel, "chat_key", "") or ""),
            adapter_key=str(getattr(channel, "adapter_key", "") or ""),
            instance_key=str(getattr(channel, "instance_key", "") or ""),
            started_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        logger.debug(f"提示词明细开始记录失败（已忽略）: code={code}, error={e}")


def note_memory(
    code: str,
    *,
    state: str,
    workspace_id: Optional[int] = None,
    preset_id: Optional[int] = None,
    items: Sequence[tuple[str, int, int]] = (),
    block_chars: int = 0,
    cooldown_skipped: int = 0,
    scores: Optional[Mapping[tuple[str, int], float]] = None,
    judge: Optional[Mapping[str, Any]] = None,
) -> None:
    """记一次记忆块渲染。

    `items` 是这一行真正进了记忆块的条目 `(类型, ID, 这一条渲染出来的字数)`。
    同一轮多次渲染时按 `(类型, ID)` 合并：字数取最后一次，渲染次数累加。
    `scores` 是这些条目在本轮检索里的相似度，按 `(类型, ID)` 对上；没有就不记。
    `judge` 是这一轮自动注入前的筛选结果 `mode / pool / kept`。同一轮只留第一次渲染
    记下的值：增量渲染不覆盖它。
    """
    try:
        draft = _rounds.get(code)
        if draft is None:
            return
        draft.render_count += 1
        draft.memory_state = str(state)
        if workspace_id is not None:
            draft.workspace_id = int(workspace_id)
        if preset_id is not None:
            draft.preset_id = int(preset_id)
        if judge is not None and draft.memory_judge is None:
            draft.memory_judge = {str(key): value for key, value in judge.items()}
        for item_type, item_id, chars in items:
            key = (str(item_type), int(item_id))
            score = scores.get(key) if scores else None
            existing = draft.memory_items.get(key)
            if existing is None:
                draft.memory_items[key] = _MemoryItemDraft(
                    item_type=str(item_type),
                    item_id=int(item_id),
                    chars=int(chars),
                    renders=1,
                    score=float(score) if score is not None else None,
                )
                continue
            existing.chars = int(chars)
            existing.renders += 1
            if score is not None:
                existing.score = float(score)
        draft.memory_block_chars = int(block_chars)
        draft.cooldown_skipped = int(cooldown_skipped)
    except Exception as e:
        logger.debug(f"提示词明细记录记忆块失败（已忽略）: code={code}, error={e}")


def note_summary(
    code: str,
    *,
    state: str,
    summary_id: Optional[int] = None,
    summary_version: Optional[int] = None,
    chars: int = 0,
) -> None:
    """记一次摘要块渲染；同一轮多次渲染时全部取最后一次。"""
    try:
        draft = _rounds.get(code)
        if draft is None:
            return
        draft.summary_state = str(state)
        draft.summary_id = None if summary_id is None else int(summary_id)
        draft.summary_version = None if summary_version is None else int(summary_version)
        draft.summary_chars = int(chars)
    except Exception as e:
        logger.debug(f"提示词明细记录摘要块失败（已忽略）: code={code}, error={e}")


def note_affection(
    code: str,
    *,
    shown: bool,
    chars: int,
    people: int,
    milestone_ids: Sequence[int] = (),
) -> None:
    """记一次好感块渲染；同一轮只渲染一次，直接覆盖。

    `milestone_ids` 是这一块里真正渲染出来的关系里程碑段落，去重保序：同一段在一轮里
    只会渲染一次，去重只是防调用方重复传。它不参与「好感块是否出现」的判断。
    """
    try:
        draft = _rounds.get(code)
        if draft is None:
            return
        draft.affection_recorded = True
        draft.affection_shown = bool(shown)
        draft.affection_chars = int(chars)
        draft.affection_people = int(people)
        unique_ids: list[int] = []
        for value in milestone_ids:
            item = int(value)
            if item not in unique_ids:
                unique_ids.append(item)
        draft.affection_milestone_ids = unique_ids
    except Exception as e:
        logger.debug(f"提示词明细记录好感块失败（已忽略）: code={code}, error={e}")


def note_worldbook(
    code: str,
    *,
    state: str,
    dropped: Sequence[tuple[int, int, int, int, str]] = (),
    budget: Optional[Mapping[str, Any]] = None,
    tokens: Optional[Mapping[int, int]] = None,
) -> None:
    """记一次世界书解析的结果；同一轮多次调用时取最后一次。

    `dropped` 是被预算跳过的条目 `(条目 ID, 书 ID, 字数, token 数, 原因)`，只供明细解释「为什么
    这条没进来」，不参与撞甲计数。`budget` 是这一轮的预算与它的算法，`tokens` 是入选条目的
    token 数（条目 ID → token）。
    """
    try:
        draft = _rounds.get(code)
        if draft is None:
            return
        draft.worldbook_state = str(state)
        draft.worldbook_dropped = [
            (int(entry_id), int(book_id), int(chars), int(size), str(reason))
            for entry_id, book_id, chars, size, reason in dropped
        ]
        draft.worldbook_budget = dict(budget) if budget else {}
        draft.worldbook_tokens = {int(entry_id): int(size) for entry_id, size in (tokens or {}).items()}
    except Exception as e:
        logger.debug(f"提示词明细记录世界书状态失败（已忽略）: code={code}, error={e}")


def note_worldbook_items(
    code: str,
    items: Sequence[tuple[Any, ...]],
) -> None:
    """记一次世界书块渲染。

    `items` 是这一块里真正渲染出来的条目 `(条目 ID, 书 ID, 字数, 位置, 是否宿主内容)`，
    可选的第六、七项是命中原因与相似度（语义命中时才带）。旧调用方仍可只传五项。
    同一轮多次渲染时按条目 ID 合并：字数取最后一次，渲染次数累加——同一轮里系统提示词与
    历史各渲染一次时，模型确实看到了两遍，`renders` 必须能反映这一点。
    """
    try:
        draft = _rounds.get(code)
        if draft is None:
            return
        for raw_item in items:
            entry_id, book_id, chars, position, trusted = raw_item[:5]
            reason = str(raw_item[5]) if len(raw_item) > 5 and raw_item[5] is not None else None
            score = float(raw_item[6]) if len(raw_item) > 6 and raw_item[6] is not None else None
            key = int(entry_id)
            existing = draft.worldbook_items.get(key)
            if existing is None:
                draft.worldbook_items[key] = _WorldbookItemDraft(
                    entry_id=key,
                    book_id=int(book_id),
                    chars=int(chars),
                    position=str(position),
                    trusted=bool(trusted),
                    renders=1,
                    reason=reason,
                    score=score,
                )
                continue
            existing.chars = int(chars)
            existing.position = str(position)
            existing.trusted = bool(trusted)
            existing.renders += 1
            if reason is not None:
                existing.reason = reason
                existing.score = score
    except Exception as e:
        logger.debug(f"提示词明细记录世界书条目失败（已忽略）: code={code}, error={e}")


def note_worldbook_lookup(
    code: str,
    items: Sequence[tuple[int, int, int, int]],
) -> None:
    """记一次模型主动查询世界书的结果。

    `items` 是这一轮真正交回给模型的条目 `(条目 ID, 书 ID, 字数, token 数)`；同一条查
    多次时字数与 token 数取最大值（先看预览、后读全文时记全文那一份），书 ID 覆盖。
    没有 `begin` 过的令牌是空操作。
    """
    try:
        draft = _rounds.get(code)
        if draft is None:
            return
        for entry_id, book_id, chars, tokens in items:
            key = int(entry_id)
            existing = draft.worldbook_lookups.get(key)
            if existing is None:
                draft.worldbook_lookups[key] = _WorldbookLookupDraft(
                    entry_id=key,
                    book_id=int(book_id),
                    chars=int(chars),
                    tokens=int(tokens),
                )
                continue
            existing.book_id = int(book_id)
            existing.chars = max(existing.chars, int(chars))
            existing.tokens = max(existing.tokens, int(tokens))
    except Exception as e:
        logger.debug(f"提示词明细记录世界书查询失败（已忽略）: code={code}, error={e}")


def note_blocked_attempt(code: str) -> None:
    """记下这一轮的首次请求被拦、并做了少带内容重发。

    只改草稿上的一个标记：明细落库时写进 `tavern_items["lean_retry"]`，撞甲计数也从
    「outcome == blocked」放宽到「outcome == blocked 或 有过这次重发」。
    """
    try:
        draft = _rounds.get(code)
        if draft is None:
            return
        draft.blocked_attempt = True
    except Exception as e:
        logger.debug(f"提示词明细记录被拦重发失败（已忽略）: code={code}, error={e}")


def note_tavern(
    code: str,
    *,
    post_history_state: Optional[str] = None,
    auxiliary_state: Optional[str] = None,
    prefill_state: Optional[str] = None,
    **fields: Any,
) -> None:
    """记一次酒馆运行时的状态；没有 `begin` 过的令牌是空操作。

    `fields` 浅合并进 `tavern_items`，其中 `macros` 例外：它记的是**全轮累计**的展开处数与
    长度保护，多次调用要把两个计数加上去，后一次不能把前一次覆盖掉。

    三个 `*_state` 是「渲染之后才知道」的状态回写（`pending` → `sent` / `unsupported` /
    `retried_without`）：渲染发生在解析之后，只能等那一步回来再覆盖，不能一开始就写死。
    槽位本身在解析时已经记下 `chars` 与 `layers`，这里只改 `state`。
    """
    try:
        draft = _rounds.get(code)
        if draft is None:
            return
        for key, value in fields.items():
            if key == "macros" and isinstance(value, dict):
                existing = draft.tavern_items.get("macros")
                merged: dict[str, Any] = (
                    dict(existing) if isinstance(existing, dict) else {"expanded": 0, "guarded": False}
                )
                merged["expanded"] = int(merged.get("expanded", 0) or 0) + int(value.get("expanded", 0) or 0)
                merged["guarded"] = bool(merged.get("guarded")) or bool(value.get("guarded"))
                draft.tavern_items["macros"] = merged
                continue
            draft.tavern_items[key] = value
        for key, state in (
            ("post_history", post_history_state),
            ("auxiliary", auxiliary_state),
            ("prefill", prefill_state),
        ):
            if state is None:
                continue
            slot = draft.tavern_items.get(key)
            updated = dict(slot) if isinstance(slot, dict) else {}
            updated["state"] = str(state)
            draft.tavern_items[key] = updated
    except Exception as e:
        logger.debug(f"提示词明细记录酒馆状态失败（已忽略）: code={code}, error={e}")


def note_state(code: str, **fields: Any) -> None:
    """记一次角色状态的本轮状态；没有 `begin` 过的令牌是空操作

    `fields` 浅合并进 `state_items`。调用方只传计数、token 数、组名与结果这类不含状态值的东西：
    这一列进后台明细，状态值只在角色状态自己的表里、按作用域给管理员看。
    """
    try:
        draft = _rounds.get(code)
        if draft is None:
            return
        draft.state_items.update(fields)
    except Exception as e:
        logger.debug(f"提示词明细记录角色状态失败（已忽略）: code={code}, error={e}")


def finish(code: str, outcome: str) -> None:
    """弹出一轮草稿并交给后台落库。

    一次提示词都没渲染、也没记过好感的轮次直接丢掉：它不是「什么都没注入」，而是
    根本没走到注入这一步（例如频道刚被删除、请求在更早的地方失败）。只注入了世界书的
    轮次同样要写：`render_count` 记的是记忆块渲染次数，世界书走的是另一个计数。
    """
    try:
        draft = _rounds.pop(code, None)
        if draft is None:
            return
        if (
            draft.render_count == 0
            and not draft.affection_recorded
            and not draft.worldbook_items
            and not draft.worldbook_lookups
            and not draft.tavern_items
            and not draft.state_items
            and not draft.blocked_attempt
        ):
            return
        normalized = outcome if outcome in _OUTCOMES else "failed"
        task = asyncio.create_task(_persist(draft, normalized))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    except Exception as e:
        logger.debug(f"提示词明细结束记录失败（已忽略）: code={code}, error={e}")


async def drain() -> None:
    """等待所有后台落库任务结束；只给测试用，生产链路不需要。"""
    while _TASKS:
        await asyncio.gather(*list(_TASKS), return_exceptions=True)


def reset_prompt_rounds() -> None:
    """清空进程内草稿；只给测试用。"""
    _rounds.clear()


async def list_rounds(
    scope: AdminMemoryScope,
    *,
    chat_key: str,
    page: int,
    page_size: int,
) -> tuple[int, list[DBMemPromptRound]]:
    """按管理端范围列出某个频道的轮次，按 ID 倒序。

    频道必填：没有频道的明细列表对排查没有意义，也会让管理端意外扫全表。
    """
    if not chat_key:
        raise ValueError("chat_key 必填")
    resolved_page = max(1, int(page))
    resolved_size = max(1, min(int(page_size), 100))
    queryset = scope.apply(
        DBMemPromptRound.all(),
        MemTable.PROMPT_ROUND,
        AdminFilter(chat_key=chat_key),
    )
    total = await queryset.count()
    rows = await queryset.order_by("-id").offset((resolved_page - 1) * resolved_size).limit(resolved_size)
    return total, list(rows)


async def round_detail(row: DBMemPromptRound) -> dict[str, Any]:
    """单轮明细：行本身 + 条目的当前状态与预览 + 摘要版本状态。

    条目状态按类型各查一次批量取回，不逐条回表：一轮最多十几条记忆，逐条查会把
    一次详情请求放大成十几次数据库往返。
    """
    from nekro_agent.services.memory import admin_service

    raw_items = row.memory_items if isinstance(row.memory_items, list) else []
    items: list[dict[str, Any]] = []
    ids_by_type: dict[str, set[int]] = {
        "paragraph": set(),
        "episode": set(),
        "relation": set(),
        "entity": set(),
    }
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_type = str(raw.get("type") or "")
        try:
            item_id = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        enriched = dict(raw)
        enriched["type"] = item_type
        enriched["id"] = item_id
        items.append(enriched)
        if item_type in ids_by_type:
            ids_by_type[item_type].add(item_id)

    paragraph_rows = (
        {paragraph.id: paragraph for paragraph in await DBMemParagraph.filter(id__in=sorted(ids_by_type["paragraph"]))}
        if ids_by_type["paragraph"]
        else {}
    )
    episode_rows = (
        {episode.id: episode for episode in await DBMemEpisode.filter(id__in=sorted(ids_by_type["episode"]))}
        if ids_by_type["episode"]
        else {}
    )
    relation_rows = (
        {relation.id: relation for relation in await DBMemRelation.filter(id__in=sorted(ids_by_type["relation"]))}
        if ids_by_type["relation"]
        else {}
    )
    # 关系预览要拼实体名，所以实体查询同时覆盖「实体条目」与「关系的两端」。
    entity_ids = set(ids_by_type["entity"])
    for relation in relation_rows.values():
        entity_ids.add(relation.subject_entity_id)
        entity_ids.add(relation.object_entity_id)
    entity_rows = {entity.id: entity for entity in await DBMemEntity.filter(id__in=sorted(entity_ids))} if entity_ids else {}

    for item in items:
        item_type = str(item["type"])
        item_id = int(item["id"])
        if item_type == "paragraph":
            paragraph = paragraph_rows.get(item_id)
            if paragraph is None:
                item["status"] = "hard_deleted"
                item["preview"] = ""
                continue
            item["status"] = admin_service.paragraph_status(paragraph)
            preview = (paragraph.summary or "").strip() or (paragraph.content or "").strip()
            item["preview"] = preview[:80]
            continue
        if item_type == "episode":
            episode = episode_rows.get(item_id)
            if episode is None:
                item["status"] = "hard_deleted"
                item["preview"] = ""
                continue
            item["status"] = admin_service.episode_status(episode)
            item["preview"] = (episode.title or "")[:80]
            continue
        if item_type == "relation":
            relation = relation_rows.get(item_id)
            if relation is None:
                item["status"] = "hard_deleted"
                item["preview"] = ""
                continue
            item["status"] = "deleted" if relation.is_inactive else "active"
            subject = entity_rows.get(relation.subject_entity_id)
            obj = entity_rows.get(relation.object_entity_id)
            if subject is not None and obj is not None:
                item["preview"] = f"{subject.name} {relation.predicate} {obj.name}"
            else:
                item["preview"] = str(relation.id)
            continue
        if item_type == "entity":
            entity = entity_rows.get(item_id)
            if entity is None:
                item["status"] = "hard_deleted"
                item["preview"] = ""
                continue
            item["status"] = "deleted" if entity.is_inactive else "active"
            item["preview"] = (entity.name or "")[:80]
            continue
        item["status"] = "unknown"
        item["preview"] = ""

    tavern_items = row.tavern_items if isinstance(row.tavern_items, dict) else {}
    detail: dict[str, Any] = {
        "id": row.id,
        "round_id": row.round_id,
        "chat_key": row.chat_key,
        "adapter_key": row.adapter_key,
        "instance_key": row.instance_key,
        "preset_id": row.preset_id,
        "workspace_id": row.workspace_id,
        "outcome": row.outcome,
        "render_count": row.render_count,
        "memory_state": row.memory_state,
        "memory_items": items,
        "memory_block_chars": row.memory_block_chars,
        "cooldown_skipped": row.cooldown_skipped,
        "memory_judge": tavern_items.get("memory_judge"),
        "summary_state": row.summary_state,
        "summary_id": row.summary_id,
        "summary_version": row.summary_version,
        "summary_chars": row.summary_chars,
        "affection_shown": row.affection_shown,
        "affection_chars": row.affection_chars,
        "affection_people": row.affection_people,
        "affection_milestone_ids": list(row.affection_milestone_ids or []),
        "worldbook_state": row.worldbook_state,
        "worldbook_items": row.worldbook_items or [],
        "worldbook_chars": row.worldbook_chars,
        "worldbook_budget": row.worldbook_budget or {},
        "tavern_items": row.tavern_items or {},
        "state_items": row.state_items or {},
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "summary": await _summary_detail(row),
    }
    return detail


async def _summary_detail(row: DBMemPromptRound) -> dict[str, Any]:
    """摘要那一版当前还在不在、是不是最新一版；还在的话带上它的生成时间与正文

    正文就是这一轮注入的那一版（截断之前的全文）：后台要能点开看「这一轮 Bot 看到的摘要」，
    而版号本身不说明任何事。摘要只留最近几版，那一版已经不保留时只有版号。

    纵深防御：明细行的可见性已经按频道判过，但摘要行是按主键取的；它的频道或人设与这一轮
    对不上（不该发生，比如脏数据）时按「已不保留」处理，不把别处的正文带出来。
    """
    if row.summary_id is None:
        return {"id": None, "version": None, "state": "none", "is_latest": False, "create_time": None, "content": ""}
    summary = await DBMemChannelSummary.get_or_none(id=row.summary_id)
    if summary is not None and (summary.chat_key != row.chat_key or summary.preset_id != row.preset_id):
        summary = None
    if summary is None:
        return {
            "id": row.summary_id,
            "version": row.summary_version,
            "state": "deleted",
            "is_latest": False,
            "create_time": None,
            "content": "",
        }
    latest = (
        await DBMemChannelSummary.filter(chat_key=summary.chat_key, preset_id=summary.preset_id)
        .order_by("-version")
        .first()
    )
    is_latest = latest is not None and latest.id == summary.id
    return {
        "id": summary.id,
        "version": summary.version,
        "is_latest": is_latest,
        "state": "current" if is_latest else "outdated",
        "create_time": summary.create_time,
        "content": summary.content,
    }


async def prune_prompt_rounds(max_age_days: int) -> int:
    """按结束时间删除超龄明细，返回删除条数；每批 1000 条。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(max_age_days)))
    deleted_total = 0
    while True:
        ids = await DBMemPromptRound.filter(finished_at__lt=cutoff).limit(1000).values_list("id", flat=True)
        if not ids:
            break
        deleted = await DBMemPromptRound.filter(id__in=list(ids)).delete()
        deleted_total += int(deleted)
        await asyncio.sleep(0)
    return deleted_total


async def _persist(draft: _RoundDraft, outcome: str) -> None:
    """写一行、裁掉同频道超限的旧行；被拦轮次另外推进自动隔离。

    两件事各自兜底：明细写失败不能连带跳过自动隔离（那是安全兜底），反之亦然。
    """
    try:
        await _write_round(draft, outcome)
    except Exception as e:
        logger.warning(f"提示词明细落库失败（可忽略）: chat_key={draft.chat_key}, outcome={outcome}, error={e}")
    if outcome != "blocked" and not draft.blocked_attempt:
        return
    try:
        await _auto_quarantine(draft)
    except Exception as e:
        logger.warning(f"被拦轮次自动隔离失败: chat_key={draft.chat_key}, error={e}")


async def _write_round(draft: _RoundDraft, outcome: str) -> None:
    """写一行明细并裁掉同频道超出保留轮数的旧行；保留轮数为 0 时什么都不写。"""
    keep = int(config.MEMORY_PROMPT_ROUNDS_KEEP)
    if keep <= 0:
        return
    preset_id = draft.preset_id
    if preset_id is None:
        from nekro_agent.services.identity_scope import resolve_preset_id

        preset_id = await resolve_preset_id(draft.channel)
    # 求值说 `injected`、但一条都没真正渲染出来（例如历史消息在「没有新消息」处早退，
    # 系统提示词那一组又是空的）：这一轮模型实际没看到任何世界书，记 `none` 才对得上。
    worldbook_state = draft.worldbook_state
    if worldbook_state == "injected" and not draft.worldbook_items:
        worldbook_state = "none"
    # 被拦后少带内容重发过的轮次在现有 JSON 列里留一个标记（照 `note_tavern` 放进
    # `tavern_items`）：后台能看出这一轮第一次被拦、重发过，而只存引用与字数。
    tavern_items = dict(draft.tavern_items)
    if draft.blocked_attempt:
        tavern_items["lean_retry"] = True
    if draft.memory_judge is not None:
        tavern_items["memory_judge"] = draft.memory_judge
    await DBMemPromptRound.create(
        round_id=secrets.token_hex(16),
        chat_key=draft.chat_key,
        adapter_key=draft.adapter_key,
        instance_key=draft.instance_key,
        preset_id=preset_id,
        workspace_id=draft.workspace_id or 0,
        outcome=outcome,
        render_count=draft.render_count,
        memory_state=draft.memory_state,
        memory_items=draft.memory_items_json(),
        memory_block_chars=draft.memory_block_chars,
        cooldown_skipped=draft.cooldown_skipped,
        summary_state=draft.summary_state,
        summary_id=draft.summary_id,
        summary_version=draft.summary_version,
        summary_chars=draft.summary_chars,
        affection_shown=draft.affection_shown,
        affection_chars=draft.affection_chars,
        affection_people=draft.affection_people,
        affection_milestone_ids=draft.affection_milestone_ids,
        worldbook_state=worldbook_state,
        worldbook_items=draft.worldbook_items_json(),
        worldbook_chars=draft.worldbook_chars,
        worldbook_budget=draft.worldbook_budget,
        tavern_items=tavern_items,
        state_items=draft.state_items,
        started_at=draft.started_at,
        finished_at=datetime.now(timezone.utc),
    )
    # 只留同频道最新的 N 行：取第 N 新的 ID，再把比它小的一次删掉。一条 DELETE
    # 比逐条删省往返，也避免在并发落库时留下半截。
    threshold_ids: list[int] = await (
        DBMemPromptRound.filter(chat_key=draft.chat_key)
        .order_by("-id")
        .offset(keep - 1)
        .limit(1)
        .values_list("id", flat=True)
    )
    if threshold_ids:
        await DBMemPromptRound.filter(chat_key=draft.chat_key, id__lt=threshold_ids[0]).delete()


async def _auto_quarantine(draft: _RoundDraft) -> None:
    """给被拦轮次里注入过 / 查过的段落、episode 与世界书条目加计数，达到阈值就隔离。

    加计数与转隔离在**同一个事务**里：分开提交的话，计数到了阈值、隔离那一步却失败
    （或进程在两步之间退出），这条记忆下一轮照样注入，而只有**再一次**被拦才会重新
    命中阈值——自动隔离这个兜底就悬空了。向量不参与数据库事务，提交后再删点。

    世界书条目与记忆段落共用这个事务、但各有各的阈值配置：两处阈值在管理页上是两个
    独立开关，共用一个会让「只关掉其中一个」变成做不到的事。
    """
    from nekro_agent.services.memory import admin_service
    from nekro_agent.services.worldbook import service as wb_service

    items = list(draft.memory_items.values())
    paragraph_ids = sorted({item.item_id for item in items if item.item_type == "paragraph"})
    # 好感块里注入过的里程碑也是记忆段落，与记忆块里的段落合并去重后一起计数：
    # 分两套计数会让同一条内容在两条路径上各攒一半、永远到不了阈值。
    paragraph_ids = sorted(set(paragraph_ids) | set(draft.affection_milestone_ids))
    episode_ids = sorted({item.item_id for item in items if item.item_type == "episode"})
    # 只算真正注入过的条目与模型主动查过的条目：`worldbook_dropped` 里的条目没进提示词，
    # 把它的被拦次数往上加等于让一条从没出现在上下文里的条目被隔离。
    worldbook_ids = sorted(set(draft.worldbook_items) | set(draft.worldbook_lookups))
    if not paragraph_ids and not episode_ids and not worldbook_ids:
        return
    threshold = int(config.MEMORY_AUTO_QUARANTINE_BLOCKED_ROUNDS)
    worldbook_threshold = int(config.WORLDBOOK_AUTO_QUARANTINE_BLOCKED_ROUNDS)

    changed: list[int] = []
    changed_episodes: list[int] = []
    changed_worldbook: list[tuple[int, int, int]] = []
    async with in_transaction():
        if paragraph_ids:
            await DBMemParagraph.filter(id__in=paragraph_ids).update(
                blocked_round_hits=F("blocked_round_hits") + 1,
            )
        if episode_ids:
            await DBMemEpisode.filter(id__in=episode_ids).update(
                blocked_round_hits=F("blocked_round_hits") + 1,
            )

        if threshold > 0 and paragraph_ids:
            paragraph_hits = [
                int(value)
                for value in await DBMemParagraph.filter(
                    id__in=paragraph_ids,
                    blocked_round_hits__gte=threshold,
                    is_inactive=False,
                ).values_list("id", flat=True)
            ]
            if paragraph_hits:
                changed = await admin_service.mark_paragraphs_quarantined(paragraph_hits, reason="auto_blocked")

        if threshold > 0 and episode_ids:
            episode_hits = [
                int(value)
                for value in await DBMemEpisode.filter(
                    id__in=episode_ids,
                    blocked_round_hits__gte=threshold,
                    is_inactive=False,
                ).values_list("id", flat=True)
            ]
            if episode_hits:
                changed_episodes = await admin_service.quarantine_episodes(episode_hits, reason="auto_blocked")

        if worldbook_ids:
            # 计数与转隔离都在 `record_blocked_round` 里，用的是同一个连接上的同一个事务。
            changed_worldbook = await wb_service.record_blocked_round(
                worldbook_ids,
                threshold=worldbook_threshold,
            )

    if changed:
        await admin_service.delete_paragraph_vectors(changed)
        logger.warning(
            f"频道记忆自动隔离: chat_key={draft.chat_key}, type=paragraph, ids={changed}, threshold={threshold}",
        )
    if changed_episodes:
        logger.warning(
            f"频道记忆自动隔离: chat_key={draft.chat_key}, type=episode, "
            f"ids={changed_episodes}, threshold={threshold}",
        )
    if changed_worldbook:
        logger.warning(
            f"世界书条目自动隔离: chat_key={draft.chat_key}, "
            f"entry_ids={[entry_id for entry_id, _book_id, _hits in changed_worldbook]}, "
            f"book_ids={[book_id for _entry_id, book_id, _hits in changed_worldbook]}, "
            f"hits={[hits for _entry_id, _book_id, hits in changed_worldbook]}, "
            f"threshold={worldbook_threshold}",
        )


def _drop_oldest_half() -> None:
    """按插入顺序丢掉最早记下的一半草稿。"""
    for code in list(_rounds)[: max(1, len(_rounds) // 2)]:
        _rounds.pop(code, None)
