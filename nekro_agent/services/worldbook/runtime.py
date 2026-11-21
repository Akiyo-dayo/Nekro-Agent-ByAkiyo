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

import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from nekro_agent.core.config import config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.affection.suppression import is_narrative_suppressed
from nekro_agent.services.identity_scope import resolve_domain_members, resolve_memory_domain, resolve_preset_id
from nekro_agent.services.memory.instance_scope import apply_worldbook_condition, worldbook_condition
from nekro_agent.services.memory.persons import expand_accounts, human_accounts, resolve_bot_account
from nekro_agent.services.tavern.macros import sanitize_user_name

from . import matcher, safe_regex, selection, template_lite, tokens, vectors
from . import purpose as wb_purpose
from . import service as wb_service
from .budget import WorldbookBudget, ignore_budget_cap, resolve_budget
from .timed_state import TimedGate, apply_timed, delay_satisfied, entry_gate, timed_state_store
from .types import (
    DEFAULT_MAX_ENTRIES_PER_ROUND,
    SCAN_DEPTH_MAX,
    ActiveScene,
    InjectMode,
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

# Bot 自己发的消息在消息表里的 `sender_id`（`message_service.push_bot_message` 写的就是它）
BOT_SENDER_ID = "-1"


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
        routed_out: 命中了、但按用途或模板不进主提示词的条目与原因（`state_only` /
            `template_unsupported` / `template_error` / `template_empty`，见 `purpose` 模块）。
            它们不占预算、不设锚点，只供明细与触发测试解释「为什么这条没进来」。
        person_labels: 个人书在本轮里该标的账号串（书 ID → 账号串）。渲染层据此在条目前加
            一行「关于谁」；普通书不在其中，输出与没有这个字段时逐字相同。
    """

    books: tuple[DBWbBook, ...]
    entries: tuple[DBWbEntry, ...]
    hits: tuple[selection.Hit, ...]
    chosen: selection.Selection
    groups: selection.RenderGroups
    anchor_entry_ids: tuple[int, ...]
    suppressed_untrusted: int = 0
    routed_out: tuple[tuple[selection.Hit, str], ...] = ()
    person_labels: Mapping[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EffectiveBooks:
    """这一轮对这个频道生效的书

    Attributes:
        books: 通过范围条件与个人书在场判定、并按生效场景过滤之后的书。
        person_labels: 个人书在本轮里该标的账号串（书 ID → `"账号1 / id:账号2"`），只含
            还留在 `books` 里的个人书。窗口里还没发过言的个人书不会出现在这里；账号
            「是否写进设定块」的清洗在下面 :func:`person_labels_of` 里做，这里只给排版。
    """

    books: tuple[DBWbBook, ...]
    person_labels: Mapping[int, str] = field(default_factory=dict)


# 能原样写进提示词设定块的账号：只收平台账号常见的那几个字符。有的适配器的账号是用户
# 自己起的名字（含空格、中文、引号……），那种不能原样进平台方写的设定块。
_SAFE_ACCOUNT = re.compile(r"^[A-Za-z0-9_.@:\-]{1,64}$")

# 一本书最多标几个账号：一个人可能有一堆副号，标太多会把标注行变成另一段正文。
MAX_PERSON_LABEL_ACCOUNTS = 3


def person_labels_of(
    books: Sequence[DBWbBook],
    window: Sequence[WindowMessage],
    *,
    bot_account: str | None,
    alias_groups: Mapping[str, Iterable[str]],
) -> dict[int, str]:
    """个人书在本轮里该标的账号串：书 ID → `"账号1 / id:账号2"`

    账号来自 :func:`matcher.present_person_accounts`（同一套在场判据），再过两道筛选：
    不匹配 :data:`_SAFE_ACCOUNT` 的丢掉（适配器账号可能是用户自己起的名字），最多取前
    :data:`MAX_PERSON_LABEL_ACCOUNTS` 个。一个都不剩的书不进结果，照旧不标。
    """
    labels: dict[int, str] = {}
    for book in books:
        if str(book.scope_kind or "") != "person":
            continue
        accounts = matcher.present_person_accounts(
            book_to_match(book),
            window,
            bot_account=bot_account,
            alias_groups=alias_groups,
        )
        safe = [account for account in accounts if _SAFE_ACCOUNT.fullmatch(account)][:MAX_PERSON_LABEL_ACCOUNTS]
        if safe:
            labels[int(book.id)] = " / id:".join(safe)
    return labels


def scene_allows(active_scene: str, chat_type: ChatType) -> bool:
    """这本书在当前的频道类型下是否生效

    `both` 与不认识的取值（空串、旧数据）一律放行：存量书的这一列都是 `both`，读坏了也
    不该让整本书静默消失。`private` / `group` 只在对应类型下生效；频道类型是 `UNKNOWN`
    时它们都不生效——指定了场景的书在分不清场景时不生效，`both` 的书不受影响，存量书
    不会因此消失。
    """
    if active_scene == ActiveScene.PRIVATE.value:
        return chat_type == ChatType.PRIVATE
    if active_scene == ActiveScene.GROUP.value:
        return chat_type == ChatType.GROUP
    return True


async def effective_books(
    channel: DBChatChannel,
    *,
    preset_id: int,
    window: Sequence[WindowMessage],
) -> EffectiveBooks:
    """算这个频道这一轮生效的书

    从范围条件、个人书在场判定到生效场景过滤都收在这里，供每轮求值与（以后的）查询工具
    共用。**不读本轮缓存**：查询工具可能在本轮主流程之外调用，而本轮新消息也会改变
    「谁在场」，所以每次调用都重新查库、重新判定。
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
    # 频道类型只从 `channel.chat_type` 取；频道替身没有这个属性时按 `UNKNOWN` 处理，
    # `both` 的书照旧生效（与 `DBChatChannel.chat_type` 读坏时的回落一致）。
    chat_type = getattr(channel, "chat_type", ChatType.UNKNOWN)
    books = tuple(book for book in books if scene_allows(str(book.active_scene or ""), chat_type))
    # 「关于谁」的标注要在场景过滤之后算：被场景挡掉的书这一轮一句都不会渲染，
    # 给它们算一遍账号只是白查别名。
    return EffectiveBooks(
        books=books,
        person_labels=person_labels_of(
            books,
            window,
            bot_account=bot_account,
            alias_groups=alias_groups,
        ),
    )


async def evaluate(
    channel: DBChatChannel,
    *,
    preset_id: int,
    window: Sequence[WindowMessage],
    budget: WorldbookBudget,
    max_entries: int,
    use_timed_state: bool,
    suppress_untrusted: bool,
    variables: template_lite.VariableProvider = template_lite.EMPTY_VARIABLES,
    semantic: bool = True,
    constant_only: bool = False,
    requester: tuple[str, str] | None = None,
) -> Evaluation:
    """算一遍「这个频道这一轮注入什么」

    Args:
        channel: 当前频道；适配器 / 实例标识都从它身上取（范围条件只认这两列）。
        preset_id: 这一轮生效的人设；0 表示无人设档，`preset` 腿的书一律不生效。
        window: 扫描窗口，按 `id` 升序。
        budget: 这一轮的 token 预算与分词系数（`budget.resolve_budget` 的结果）。
        max_entries: 每轮条数上限。
        use_timed_state: 是否叠加 sticky / cooldown 的锚点状态。触发测试传 False：
            测试页不该改变正式状态，也不该因为上一次测试而给出不同结论。
        suppress_untrusted: 是否在判定前去掉不可信条目（上一轮被内容审核拦下时）。
        variables: 这一轮模板读变量的来源（`variables.provider_for_round`）；默认是空的那一个，
            与没有变量时逐字相同。打出变量值的条目这一轮按不可信内容注入。
        semantic: 是否做语义触发（向量检索）。默认 True，但没有任何参与条目时不会算查询
            向量、也不会碰 Qdrant；体检等只想看常驻与关键词的调用方传 False。
        constant_only: 只保留常驻条目、并且不做语义检索。给「首次请求被上游拦下后少带内容
            重发」用：关键词 / 语义命中的条目这一轮整条不参与匹配，常驻条目照旧。
        requester: 语义查询向量的用量归属 `(adapter_key, instance_key)`。

    Returns:
        见 :class:`Evaluation`。
    """
    effective = await effective_books(channel, preset_id=preset_id, window=window)
    books = effective.books
    if any(bool(getattr(book, "scan_names", False)) for book in books):
        # 只有真有书要带名字时才查人设名：Bot 自己的消息在扫描文本里用这一轮生效的人设名
        window = await _with_persona_speaker(window, preset_id)

    entries = tuple(await wb_service.active_entries_for_books([book.id for book in books]))
    suppressed_untrusted = 0
    if suppress_untrusted:
        # 判定之前就去掉：让出来的预算要给宿主条目用得上。放到入选之后筛，被跳过的仍是
        # 租户条目占着预算，宿主条目还是进不来。
        kept = tuple(entry for entry in entries if entry.trusted)
        suppressed_untrusted = len(entries) - len(kept)
        entries = kept
    if constant_only:
        # 少带内容重发只留常驻条目：关键词 / 语义命中的条目在这一轮整条不参与匹配，
        # 也不做语义检索（下面 `semantic_scores` 直接为空，不会碰 Qdrant）。
        entries = tuple(entry for entry in entries if entry.constant)
        semantic = False

    books_by_id = {book.id: book for book in books}
    # `lookup` 书的关键词条目这一轮不自动注入：常驻条目照旧，关键词命中的留给模型自己用
    # 查询工具取。必须放在下面逐条匹配、尤其是「推迟到最后判」那一支之前——被剔掉的条目
    # 既不进本轮命中，也不进「命中但没注入」的明细，因为它们按设计就不参与这一轮。
    entries = tuple(
        entry
        for entry in entries
        if entry.constant
        or books_by_id.get(entry.book_id) is None
        or books_by_id[entry.book_id].inject_mode != InjectMode.LOOKUP.value
    )
    # 语义候选与关键词用同一份书单 / 条目单：没有参与条目时这一步直接返回空，不算查询
    # 向量、也不碰 Qdrant（默认路径逐字不变）。放在建 runner 之前，失败与否都不影响关键词。
    semantic_scores = (
        await vectors.semantic_hits(books, entries, window, requester=requester) if semantic else {}
    )
    runner = safe_regex.RegexRunner()

    def match_entry_hit(match_entry: Any) -> matcher.HitReason | None:
        """先照旧判关键词 / 常驻；没命中才接受语义命中

        关键词与常驻优先：同一条既命中关键词又语义相近时，原因必须还是 `key` / `constant`。
        语义命中只是一个 `HitReason`，后面的延迟 / 冷却 / 粘性 / 分流 / 预算全走同一套。
        """
        reason = matcher.entry_hit(match_entry, window, runner)
        if reason is None and match_entry.id in semantic_scores:
            return matcher.HitReason(kind="semantic", score=semantic_scores[match_entry.id])
        return reason
    # 触发测试按窗口条数判 `delay`（与改造前逐字一致），锚点一律当作没有；运行时才去数
    # 频道总条数，且只在真有条目设了 `delay` 时才数——`delay` 数的是频道总条数，
    # 与「本轮窗口里有多少条」不是一回事。
    channel_count = len(window)
    if use_timed_state and _any_delay(entries, books_by_id):
        channel_count = await _channel_count(channel)
    anchors = _AnchorCounter(chat_key=channel.chat_key)

    hits: list[selection.Hit] = []
    wants_anchor: list[int] = []
    routed_out: list[tuple[selection.Hit, str]] = []
    # 匹配之前只做不碰正文分析的分流（开销与条目数成正比、与正文长短无关）：标题标着
    # `[mvu_update]` / `[initvar]` 的，以及缓存里已经知道要分流的（含不支持的模板、状态渲染、
    # 诱饵），都放到最后判——它们命中与否只用来在明细里解释，正则每轮的那点预算要先给真会
    # 注入的条目用。第一次见到的正文要等命中、进预算之前才分析（见下面的 `prepare`）。
    deferred: list[tuple[DBWbEntry, str]] = []
    for entry in entries:
        book = books_by_id.get(entry.book_id)
        if book is None:
            continue
        content = str(entry.content or "")
        out_reason = wb_purpose.quick_route(str(entry.comment or ""), content)
        if out_reason is None and wb_purpose.needs_analysis(content):
            out_reason = wb_purpose.known_route(entry, variables)
        if out_reason is not None:
            deferred.append((entry, out_reason))
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
                hit = match_entry_hit(match_entry)
            final, wants = apply_timed(match_entry, hit, gate, channel_count)
        else:
            # 触发测试：只判 `delay`，命中原因照旧来自关键词 / 常驻 / 语义。
            final, wants = apply_timed(
                match_entry,
                match_entry_hit(match_entry),
                TimedGate.FREE,
                channel_count,
            )
        if final is None:
            continue
        hits.append(selection.Hit(entry=match_entry, reason=final))
        if wants:
            wants_anchor.append(match_entry.id)

    for entry, out_reason in deferred:
        book = books_by_id[entry.book_id]
        match_entry = entry_to_match(entry, book_to_match(book))
        if not delay_satisfied(match_entry.delay, channel_count):
            continue
        hit_reason = match_entry_hit(match_entry)
        if hit_reason is not None:
            routed_out.append((selection.Hit(entry=match_entry, reason=hit_reason), out_reason))

    # 模板在命中之后、进预算之前按入选顺序逐条算（条数上限之后的条目不算）：算出来的文字
    # 替换正文，预算与渲染都按它算；分流、算出错、算完为空、这一轮模板预算用完的不注入，
    # 也不设锚点（这一轮它没有真正生效）。编译与求值都从同一份每轮**模板预算**里扣：按字计，
    # 每次至少记 512 字，每轮最多处理 4 × 条数上限条带模板的命中。它与下面的 token 预算是
    # 两回事——模板预算管的是分析的 CPU 开销，token 预算只数 `prepare` 之后真正注入的正文。
    rows_by_id = {entry.id: entry for entry in entries}
    template_budget = wb_purpose.RoundBudget(analyses_left=max(1, 4 * max_entries))

    def prepare(hit: selection.Hit) -> selection.Hit | str:
        row = rows_by_id.get(hit.entry.id)
        if row is None:
            return hit
        rendered = wb_purpose.prepare_content(row, hit.entry.content, template_budget, variables)
        if rendered.reason is not None:
            return rendered.reason
        if rendered.untrusted:
            # 变量值来自聊天：条目把它打出来，这一轮就按不可信内容注入（清洗、包块），并标出来源。
            return replace(hit, entry=replace(hit.entry, content=rendered.content, trusted=False), from_variables=True)
        if rendered.content is hit.entry.content:
            return hit
        return replace(hit, entry=replace(hit.entry, content=rendered.content))

    # 命中的正文一次数完（合计很长时摘要和计数都在线程里做，见 `tokens.base_counts`），
    # 选条目时取这里的原始数乘系数，事件循环上不再对正文做摘要。`prepare` 换了正文的模板条目
    # （正文已不是原来那一份）按换过的正文另数：模板算出来的文字受每轮模板预算约束，不会很长。
    raw_counts = {
        hit.entry.id: (hit.entry.content, raw)
        for hit, raw in zip(hits, await tokens.base_counts([hit.entry.content for hit in hits]), strict=True)
    }

    def count(entry: Any) -> int:
        known = raw_counts.get(entry.id)
        if known is not None and known[0] is entry.content:
            return tokens.apply_factor(known[1], budget.factor)
        return budget.count(entry.content)

    chosen = selection.select_hits(
        hits,
        max_tokens=budget.budget,
        max_entries=max_entries,
        book_budgets={book.id: book_to_match(book).budget_tokens for book in books},
        count=count,
        prepare=prepare,
        known_route=lambda hit: (
            wb_purpose.known_route(rows_by_id[hit.entry.id], variables) if hit.entry.id in rows_by_id else None
        ),
        ignore_budget_cap=ignore_budget_cap(budget),
    )
    routed_out.extend(chosen.routed_out)
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
        routed_out=tuple(routed_out),
        person_labels=effective.person_labels,
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
        dropped: 被预算跳过的条目 `(entry_id, book_id, chars, tokens, reason)`，只供明细解释
            「为什么这条没进来」，不参与撞甲计数。因条数上限跳过的没数过，`tokens` 记 0。
        budget: 这一轮的预算与它的算法；闸关或求值失败时为 None。
        tokens: 入选条目的 token 数（条目 ID → token），明细与预览按它显示。
        person_labels: 个人书在本轮里该标的账号串（书 ID → 账号串），默认空字典。渲染层拿它
            在个人书条目前加一行「关于谁」；空时渲染输出与没有这个字段时逐字相同。
    """

    state: str
    groups: selection.RenderGroups
    dropped: tuple[tuple[int, int, int, int, str], ...] = ()
    budget: Optional[WorldbookBudget] = None
    tokens: Mapping[int, int] = field(default_factory=dict)
    person_labels: Mapping[int, str] = field(default_factory=dict)


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
    variables: Optional[template_lite.VariableProvider] = None,
    constant_only: bool = False,
) -> RoundWorldbook:
    """解析这一轮的世界书注入，按 `code` 缓存

    Args:
        code: 本轮的一次性令牌（`one_time_code`），缓存键。
        channel: 当前频道。
        channel_config: 这个频道的**生效**配置（`get_effective_config()` 的返回值），
            世界书的开关、预算百分比与 token 上限、主模型组都从它读。
        preset_id_override: 提示词预览「换一个人设看看」时传所选人设；人设书按它判，
            不按频道当前人设。正式回合不传。
        variables: 这一轮模板读变量的来源；不传就是空的那一个。
        constant_only: 只保留常驻条目、不做语义检索（少带内容重发用）。

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
        result = await _resolve_enabled_round(
            code,
            channel,
            channel_config,
            preset_id_override=preset_id_override,
            variables=variables,
            constant_only=constant_only,
        )
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
    variables: Optional[template_lite.VariableProvider] = None,
    constant_only: bool = False,
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
    # 上一轮被拦时这一轮去掉不可信内容；变量值来自聊天，同样不给（模板按空变量算，与没有变量时相同）。
    round_variables = template_lite.EMPTY_VARIABLES if suppressed or variables is None else variables
    # 预算在发请求之前算一次，随这一轮缓存；换备用组重试不重算（见 `budget` 模块）
    await tokens.ensure_loaded_async()
    budget = resolve_budget(channel_config)
    evaluation = await evaluate(
        channel,
        preset_id=preset_id,
        window=window,
        budget=budget,
        max_entries=int(getattr(config, "WORLDBOOK_MAX_ENTRIES_PER_ROUND", DEFAULT_MAX_ENTRIES_PER_ROUND)),
        use_timed_state=True,
        suppress_untrusted=suppressed,
        variables=round_variables,
        semantic=bool(getattr(channel_config, "WORLDBOOK_VECTOR_ENABLED", False)) and not constant_only,
        constant_only=constant_only,
        requester=(channel.adapter_key, channel.instance_key),
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

    counted = evaluation.chosen.tokens
    selected_ids = {hit.entry.id for hit in evaluation.chosen.selected}
    return RoundWorldbook(
        state=state,
        groups=evaluation.groups,
        dropped=tuple(
            (hit.entry.id, hit.entry.book_id, len(hit.entry.content), int(counted.get(hit.entry.id, 0)), reason)
            for hit, reason in (*evaluation.chosen.dropped, *evaluation.routed_out)
        ),
        budget=budget,
        tokens={entry_id: size for entry_id, size in counted.items() if entry_id in selected_ids},
        person_labels=evaluation.person_labels,
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
        .values("id", "sender_id", "platform_userid", "content_text", "sender_nickname")
    )
    # 倒序取最近 N 条再翻回升序：`scan_text` 与 `book_active_for_person` 都按 `id` 升序
    # 理解「最后几条」，直接把倒序列表传进去会让「最后 depth 条」变成「最早 depth 条」。
    # 名字只给群友的消息填（清洗过的昵称）；Bot 自己的消息留空，由 `evaluate` 在有书要带
    # 名字时换成人设名。名字只在书打开「扫描时带上说话人名字」时才进扫描文本。
    return [
        WindowMessage(
            id=int(row["id"]),
            sender_id=str(row["sender_id"] or ""),
            platform_userid=str(row["platform_userid"] or ""),
            content_text=str(row["content_text"] or ""),
            speaker="" if str(row["sender_id"] or "") == BOT_SENDER_ID else speaker_name(row["sender_nickname"]),
        )
        for row in reversed(rows)
    ]


def speaker_name(raw: Any) -> str:
    """扫描文本里的说话人名字：按 `{{user}}` 的口径清洗；空名字返回空串（不拼「用户」）

    群友能改自己的昵称，名字又要拼进扫描文本，所以照 `sanitize_user_name` 去换行、标签与
    零宽字符、限 24 字。那个函数在名字为空时给「用户」，这里不要：拼一个「用户: 」会让写着
    「用户」的条目被每一条没有昵称的消息触发。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    cleaned = sanitize_user_name(text)
    return cleaned if cleaned != "用户" or text == "用户" else ""


async def persona_speaker(preset_id: int) -> str:
    """Bot 自己的消息在扫描文本里用的名字：与 `{{char}}` 同一取法（卡片昵称优先，否则人设名）

    没有人设档（`preset_id` 为 0）或查不到时返回空串，Bot 的消息就只拼正文。
    """
    if preset_id <= 0:
        return ""
    card = await DBPresetCard.get_or_none(preset_id=preset_id)
    if card is not None and str(card.nickname or "").strip():
        return speaker_name(card.nickname)
    preset = await DBPreset.get_or_none(id=preset_id)
    return speaker_name(preset.name) if preset is not None else ""


async def _with_persona_speaker(window: Sequence[WindowMessage], preset_id: int) -> list[WindowMessage]:
    """把窗口里 Bot 自己那几条的名字换成这一轮的人设名"""
    name = await persona_speaker(preset_id)
    return [replace(message, speaker=name) if message.sender_id == BOT_SENDER_ID else message for message in window]


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
    "MAX_PERSON_LABEL_ACCOUNTS",
    "MAX_ROUNDS",
    "EffectiveBooks",
    "Evaluation",
    "RoundWorldbook",
    "cached_round",
    "effective_books",
    "evaluate",
    "finish_round",
    "load_window",
    "person_labels_of",
    "resolve_round",
    "scene_allows",
]
