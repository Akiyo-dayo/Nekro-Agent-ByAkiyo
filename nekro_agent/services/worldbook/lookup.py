"""模型主动查询本频道这一轮生效的世界书

插件 `worldbook_tools` 只是薄薄一层，作用域判定、次数上限、打分与正文渲染都在这里，
便于单测与审查。三条底线：

- **作用域只来自服务端**：频道、人设、书单、条目全部由当前 `AgentCtx` 推导，绝不接受
  模型传来的书编号 / 人设编号 / 频道。
- **次数与 token 两条上限都在核心侧强制**：两个查询方法共用同一份每轮计数；查不到的
  书、停用或隔离的条目一律按「不存在」处理，连编号之外的信息都不回。
- **结果整份是不可信资料**：正文走与注入同一套模板求值、宏展开与清洗；书名、标题、
  关键词是单行清洗，正文是块清洗。

日志只记方法名、条数、`missing` 个数与是否截断，不记查询原文、标题与正文。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from nekro_agent.core.config import config as global_config
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.schemas.chat_message import ChatType
from nekro_agent.services.affection.suppression import is_narrative_suppressed
from nekro_agent.services.identity_scope import resolve_preset_id
from nekro_agent.services.memory import prompt_rounds
from nekro_agent.services.memory.text_terms import extract_query_terms
from nekro_agent.services.tavern import runtime as tavern_runtime
from nekro_agent.tools.untrusted_content import defang_boundary_tags, sanitize_block_text, sanitize_inline_text

from . import purpose as wb_purpose
from . import service as wb_service
from . import template_lite, tokens
from . import variables as wb_variables
from .budget import WorldbookBudget, resolve_budget
from .runtime import effective_books, load_window

if TYPE_CHECKING:  # pragma: no cover - 只在类型检查时需要，运行时不把 schema 拖进这条链路
    from nekro_agent.schemas.agent_ctx import AgentCtx

logger = get_sub_logger("worldbook.lookup")

# 查询原文只截到这么长：再长的输入对字面匹配没有额外信息量，却会让分词与打分一起变慢。
MAX_QUERY_CHARS = 200
# `search` 每轮最多带回几条；`limit` 参数夹在这个区间里。
DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 10
# `read` 一次最多读几个编号，多的只取前几个并在结果里说明。
MAX_READ_IDS = 5
# 放不下的条目给前这么多字的预览；预览同样计入 token 上限。
MAX_PREVIEW_CHARS = 200
# 单条正文进结果前的清洗上限：它是兜底，正常条目的 token 上限远小于这里。
MAX_ITEM_CONTENT_CHARS = 40_000
# 书名 / 标题 / 关键词这类必须单行的字段各自的上限。
MAX_BOOK_NAME_CHARS = 120
MAX_TITLE_CHARS = 160
MAX_KEY_CHARS = 60
# 截断正文时按字数估算后最多再用尺子核对这么多次；还放不下就不给。
MAX_TRUNCATE_TRIES = 6
# 单轮作用域缓存与每轮调用计数的键上限：正常一轮结束就会弹出，上限只兜「忘了收尾」。
MAX_SCOPE_KEYS = 256
MAX_CALL_KEYS = 4096
# 作用域缓存再按条目正文总字数封顶：只按键数封顶挡不住几本大书同时留在缓存里的内存。
# 放进去的那一个哪怕自己就超了也保留（见 `_store_scope`），所以实际占用不会超过一份大书。
MAX_SCOPE_CACHE_CHARS = 20_000_000

# 模板预算的条数名额：与运行时同一个口径（4 × 条数上限），每次调用新建一份。
ANALYSES_PER_ENTRY = 4


@dataclass(frozen=True)
class LookupDenied:
    """一次查询被拒绝

    Attributes:
        code: 机读原因码（日志与用例按它判断，不依赖中文说明）。
        message: 给模型看的一句中文说明，不含任何条目正文。
    """

    code: str
    message: str


@dataclass(frozen=True)
class EntryText:
    """一条条目在加载时预算好的打分文字

    只装普通字符串与编号，不装 ORM 对象：打分在线程里跑，只碰这一份数据。模板条目的
    `content` 是空串——第一段打分不让模板原文参与（没走到的分支也能被关键词命中就等于
    剧透），渲染后的正文在第二段补算。
    """

    id: int
    order: int
    title: str
    keywords: tuple[str, ...]
    content: str
    is_template: bool


@dataclass(frozen=True)
class LookupScope:
    """这一轮这个频道能查的世界书

    Attributes:
        channel: 当前频道；书单与窗口都由它推导。
        config: 这个频道的生效配置。
        one_time_code: 本轮一次性令牌，次数上限与作用域缓存都按它记。
        books: 可查的书，按编号索引。
        entries: 可查的条目，按编号索引。
        entry_texts: 每条条目预算好的打分文字（`casefold` 后的标题、关键词与正文）。
        variables: 这一轮模板读变量的来源。
        expand: 这一轮正文的宏展开函数（`record=False`，不重复记宏统计）。
        content_chars: 这些条目正文的总字数，作用域缓存按它封顶。
    """

    channel: Any
    config: Any
    one_time_code: str
    books: dict[int, DBWbBook]
    entries: dict[int, DBWbEntry]
    entry_texts: tuple[EntryText, ...]
    variables: template_lite.VariableProvider
    expand: Callable[[str], str] = field(default=lambda text: text)
    content_chars: int = 0


@dataclass(frozen=True)
class LookupItem:
    """查询结果里的一条条目

    `book_id` / `chars` / `tokens` 只给提示词明细与日志用，不进交给模型的 JSON；
    交给模型的字段见 :meth:`as_dict`。
    """

    id: int
    book_id: int
    book: str
    title: str
    keys: tuple[str, ...]
    content: str
    truncated: bool
    chars: int
    tokens: int

    def as_dict(self) -> dict[str, Any]:
        """交给模型的形状：条目编号、书名、标题、主关键词、正文、是否截断。"""
        return {
            "id": self.id,
            "book": self.book,
            "title": self.title,
            "keys": list(self.keys),
            "content": self.content,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class LookupResult:
    """一次成功查询的结果

    Attributes:
        items: 按顺序给出的条目。
        missing: 按传入顺序列出「不存在」的编号（别的书、停用、隔离、别的租户、不存在
            这几种情况在返回里不做区分）。
        note: 给模型看的一句中文说明（截断提示、条数截取说明等），不含正文。
    """

    items: tuple[LookupItem, ...] = ()
    missing: tuple[int, ...] = ()
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        """交给模型的 JSON 形状。"""
        return {
            "items": [item.as_dict() for item in self.items],
            "missing": list(self.missing),
            "note": self.note,
        }


# 同一个 `one_time_code` 的作用域结果（含拒绝）在本轮内复用：书单一轮不变，插件每次渲染
# 都会调 `collect`，不能每次都重查。有上限，超了按插入顺序丢最旧的；另外按条目正文总字数封顶。
_scope_cache: "OrderedDict[str, LookupScope | LookupDenied]" = OrderedDict()
# `availability` 的结果（含「可以提供工具」的 `None`）单独缓存一份：`collect` 每轮渲染都会调，
# 但条目要等两个方法第一次真正被调用才加载。同样是按令牌记、有键数上限的插入序字典。
_availability_cache: "OrderedDict[str, LookupDenied | None]" = OrderedDict()
# 正在进行的首次加载：同一个令牌的并发调用共用同一个任务，不各自查库。完成（成功或拒绝）后
# 结果进作用域缓存、这里拿掉；抛异常时也拿掉，下一次调用可以重试。
_loading_tasks: dict[str, "asyncio.Task[LookupScope | LookupDenied]"] = {}
# 每个令牌已经用掉的查询次数；`search` 与 `read` 共用。
_used_calls: "OrderedDict[str, int]" = OrderedDict()
# 已经收尾的轮次。沙盒迟到的回调仍能按容器键取到那一轮的令牌（服务端令牌要等下一轮才覆盖），
# 不记下来的话会用旧令牌重建作用域、次数从零算。
_closed_codes: "OrderedDict[str, None]" = OrderedDict()

_ROUND_CLOSED = LookupDenied("round_closed", "本轮已经结束，不能再查询世界书。")


def discard_round(code: str) -> None:
    """一轮收尾：清掉两份缓存与调用计数，取消还没完成的首次加载，并记下这个令牌已结束

    空令牌是空操作。加载任务被取消时，正在等它的调用会收到 `CancelledError`；共用同一个
    任务的其它调用有 `asyncio.shield` 兜着，不会被一起取消。
    """
    if not code:
        return
    _scope_cache.pop(code, None)
    _availability_cache.pop(code, None)
    _used_calls.pop(code, None)
    task = _loading_tasks.pop(code, None)
    if task is not None and not task.done():
        task.cancel()
    _closed_codes[code] = None
    _closed_codes.move_to_end(code)
    while len(_closed_codes) > MAX_CALL_KEYS:
        _closed_codes.popitem(last=False)


def reset_lookup_state() -> None:
    """清空全部进程内状态；只给用例用。"""
    for task in _loading_tasks.values():
        if not task.done():
            task.cancel()
    _loading_tasks.clear()
    _scope_cache.clear()
    _availability_cache.clear()
    _used_calls.clear()
    _closed_codes.clear()


def _round_closed(code: str) -> bool:
    return bool(code) and code in _closed_codes


def _scope_chars() -> int:
    """作用域缓存里所有条目的正文总字数（拒绝项不算）"""
    return sum(
        result.content_chars for result in _scope_cache.values() if isinstance(result, LookupScope)
    )


def _store_scope(code: str, result: "LookupScope | LookupDenied") -> None:
    """写作用域缓存；满了先按键数、再按条目正文总字数从最旧的开始丢

    刚放进来的那一个在末尾，永远不丢：只剩它自己时哪怕超过字数上限也保留。
    """
    _scope_cache[code] = result
    _scope_cache.move_to_end(code)
    while len(_scope_cache) > MAX_SCOPE_KEYS:
        _scope_cache.popitem(last=False)
    while len(_scope_cache) > 1 and _scope_chars() > MAX_SCOPE_CACHE_CHARS:
        _scope_cache.popitem(last=False)


def _max_calls(config: Any) -> int:
    """这一轮允许的查询次数；读不到按默认值。"""
    try:
        return max(0, int(getattr(config, "WORLDBOOK_LOOKUP_MAX_CALLS_PER_ROUND", 4)))
    except (TypeError, ValueError):
        return 4


def _max_tokens(config: Any) -> int:
    """每次查询最多带回多少 token；读不到按默认值，至少 1。"""
    try:
        return max(1, int(getattr(config, "WORLDBOOK_LOOKUP_MAX_TOKENS", 3000)))
    except (TypeError, ValueError):
        return 3000


def _reserve_call(code: str, max_calls: int) -> bool:
    """先判次数再占一次；次数用完（或这一轮已经收尾）返回假，调用方不再查库。"""
    if _round_closed(code):
        return False
    used = _used_calls.get(code, 0)
    if used >= max_calls:
        return False
    _used_calls[code] = used + 1
    _used_calls.move_to_end(code)
    while len(_used_calls) > MAX_CALL_KEYS:
        _used_calls.popitem(last=False)
    return True


def _exhausted_before_lookup(ctx: "AgentCtx") -> Optional[LookupDenied]:
    """次数已用完时在解析作用域之前就拒绝

    正常一轮里 `collect` 在渲染提示词时已经把作用域放进缓存，因此模型调用时这里能直接
    算出上限；先拒绝就不再碰任何世界书表。缓存被挤出（忘了收尾的极端情况）时返回 `None`，
    由 `resolve_scope` 正常判定后仍会在 `_reserve_call` 那里被拒绝。
    """
    code = str(getattr(ctx, "one_time_code", "") or "")
    if not code:
        return None
    if _round_closed(code):
        return _ROUND_CLOSED
    cached = _scope_cache.get(code)
    if not isinstance(cached, LookupScope):
        return None
    if _used_calls.get(code, 0) >= _max_calls(cached.config):
        return LookupDenied("call_limit", "本轮查询次数已用完。")
    return None


@dataclass(frozen=True)
class _RoundGate:
    """共用判定通过、还没读条目时的那一份上下文

    `availability` 只需要知道它成立；作用域加载拿它接着读条目、取变量来源。
    """

    channel: Any
    config: Any
    code: str
    preset_id: int
    books: tuple[DBWbBook, ...]


async def _round_gate(ctx: "AgentCtx") -> "_RoundGate | LookupDenied":
    """作用域判定里除「读条目」以外的全部判断，顺序与拒绝码固定

    `availability` 与 `_resolve_scope_uncached` 共用同一份判据，不写两份：已收尾 → 频道 →
    本轮令牌 → 世界书开关 → 次数上限 → 上一轮被拦 → 书单（`push` 的书不算，只看是否为空）。
    只读 `ctx`，不接受任何来自模型的参数；异常不吞，由调用方处理。
    """
    code = str(getattr(ctx, "one_time_code", "") or "")
    if _round_closed(code):
        return _ROUND_CLOSED
    channel = getattr(ctx, "db_chat_channel", None)
    if channel is None and getattr(ctx, "from_chat_key", ""):
        from nekro_agent.models.db_chat_channel import DBChatChannel

        channel = await DBChatChannel.get_or_none(chat_key=ctx.from_chat_key)
    if channel is None:
        return LookupDenied("no_channel", "无法定位当前频道，已拒绝本次世界书查询。")
    if not code:
        return LookupDenied("no_round_token", "本轮缺少一次性令牌，已拒绝本次世界书查询。")

    # 频道生效配置必须与主流程 `round_prompt` 用的是同一份。`ctx` 预填了频道时走
    # `get_core_config()`；频道是从 `from_chat_key` 反查出来的（少见）时直接问那个频道。
    if getattr(ctx, "db_chat_channel", None) is not None:
        core_config = await ctx.get_core_config()
    else:
        core_config = await channel.get_effective_config()

    if not bool(getattr(core_config, "WORLDBOOK_ENABLED", False)):
        return LookupDenied("worldbook_disabled", "当前未启用世界书，无法查询。")
    if _max_calls(core_config) == 0:
        return LookupDenied("lookup_disabled", "当前没有开启模型查询世界书。")
    # 与 `runtime._resolve_enabled_round` 抑制不可信条目用的是同一个判据与同一份过期时长。
    if is_narrative_suppressed(channel.chat_key, global_config.AI_CHAT_CONTEXT_EXPIRE_SECONDS):
        return LookupDenied("round_suppressed", "上一轮被内容审核拦截，这一轮不能查询世界书。")

    expire_seconds = float(
        getattr(core_config, "AI_CHAT_CONTEXT_EXPIRE_SECONDS", global_config.AI_CHAT_CONTEXT_EXPIRE_SECONDS),
    )
    window = await load_window(channel, expire_seconds=expire_seconds)
    preset_id = await resolve_preset_id(channel)
    effective = await effective_books(channel, preset_id=preset_id, window=window)
    # `push` 的书只能被动推送，模型不能查；`lookup` / `both` 都可以。
    books = tuple(book for book in effective.books if str(book.inject_mode or "") != "push")
    if not books:
        return LookupDenied("no_books", "本频道没有可查的世界书。")
    return _RoundGate(channel=channel, config=core_config, code=code, preset_id=preset_id, books=books)


async def availability(ctx: "AgentCtx") -> Optional[LookupDenied]:
    """这一轮能不能把查询方法交给模型；只看书单是否为空，不读条目

    判定与拒绝码跟 :func:`resolve_scope` 同一套（共用 :func:`_round_gate`），只是省掉读条目
    与取变量来源这两步。结果按 `one_time_code` 缓存到本轮结束：提示词渲染每轮会反复调
    `collect`，不能每次都重查书单。已收尾的令牌不缓存、直接拒绝；空令牌不缓存。

    Returns:
        能提供工具时为 `None`，否则是这一轮的拒绝原因。
    """
    code = str(getattr(ctx, "one_time_code", "") or "")
    if _round_closed(code):
        return _ROUND_CLOSED
    if code and code in _availability_cache:
        _availability_cache.move_to_end(code)
        return _availability_cache[code]
    gate = await _round_gate(ctx)
    denied = gate if isinstance(gate, LookupDenied) else None
    if code:
        _availability_cache[code] = denied
        _availability_cache.move_to_end(code)
        while len(_availability_cache) > MAX_SCOPE_KEYS:
            _availability_cache.popitem(last=False)
    return denied


async def resolve_scope(ctx: "AgentCtx") -> "LookupScope | LookupDenied":
    """按服务端的频道、人设与开关算出这一轮能查的书与条目

    只读 `ctx`，不接受任何来自模型的参数。任何一条判据不满足都拒绝（fail-closed）：
    频道取不到、本轮令牌为空、这一轮已经收尾、世界书关着、查询次数上限为 0、上一轮被拦、
    书单为空。成功与拒绝都按 `one_time_code` 缓存到本轮结束；已收尾的令牌不缓存、不重建。

    条目要等这个方法第一次被调用才读：`collect` 只调 :func:`availability`。同一个令牌的并发
    首次加载共用同一个任务，不各自查库；一个调用被取消不连带取消别人。加载抛异常时不写缓存，
    下一次调用可以重试。
    """
    code = str(getattr(ctx, "one_time_code", "") or "")
    if _round_closed(code):
        return _ROUND_CLOSED
    if not code:
        return await _resolve_scope_uncached(ctx)
    cached = _scope_cache.get(code)
    if cached is not None:
        _scope_cache.move_to_end(code)
        return cached
    task = _loading_tasks.get(code)
    if task is None:
        task = asyncio.ensure_future(_load_scope_once(ctx, code))
        _loading_tasks[code] = task

        def _forget(done: "asyncio.Task[LookupScope | LookupDenied]", *, code: str = code) -> None:
            """任务结束（成功、拒绝或抛异常）就从「进行中」拿掉"""
            if _loading_tasks.get(code) is done:
                _loading_tasks.pop(code, None)

        task.add_done_callback(_forget)
    # `shield`：等它的这一方被取消时，共享的加载任务继续跑，别的调用不受影响。
    return await asyncio.shield(task)


async def _load_scope_once(ctx: "AgentCtx", code: str) -> "LookupScope | LookupDenied":
    """首次加载：读条目、预算打分文字、写作用域缓存

    异常不写缓存（完成回调会把任务从「进行中」拿掉），下一次调用可以重试。
    """
    result = await _resolve_scope_uncached(ctx)
    if not _round_closed(code):
        _store_scope(code, result)
    return result


async def _resolve_scope_uncached(ctx: "AgentCtx") -> "LookupScope | LookupDenied":
    """没命中缓存时的实际判定；异常不吞：由调用方插件层转成拒绝。"""
    gate = await _round_gate(ctx)
    if isinstance(gate, LookupDenied):
        return gate

    rows = await wb_service.active_entries_for_books([book.id for book in gate.books])
    # 按用途不进主提示词的条目（`[mvu_update]` / `[initvar]` / 状态渲染）同样不提供给模型：
    # 它们本来就不是「设定资料」，查出来只会让模型误以为该照着做。
    entries = tuple(
        row for row in rows if wb_purpose.quick_route(str(row.comment or ""), str(row.content or "")) is None
    )
    variables = await _variables_for_round(
        ctx=ctx,
        channel=gate.channel,
        config=gate.config,
        code=gate.code,
        preset_id=gate.preset_id,
    )
    await tokens.ensure_loaded_async()
    return LookupScope(
        channel=gate.channel,
        config=gate.config,
        one_time_code=gate.code,
        books={book.id: book for book in gate.books},
        entries={row.id: row for row in entries},
        entry_texts=tuple(_entry_text(row) for row in entries),
        variables=variables,
        expand=tavern_runtime.expander(gate.code, record=False),
        content_chars=sum(len(str(row.content or "")) for row in entries),
    )


async def _variables_for_round(
    *,
    ctx: "AgentCtx",
    channel: Any,
    config: Any,
    code: str,
    preset_id: int,
) -> template_lite.VariableProvider:
    """这一轮的模板变量来源；没有登记、出错或超时都回落空提供者。

    字段照抄 `round_prompt` 里构造 `RoundVariableContext` 的那一段，让查询侧与注入侧
    对同一条模板算出同一段文字。

    `dry_run=False` 不是写死的假设：走到这里必须先有本轮令牌，而令牌只在正式一轮里由
    `run_agent` 绑到上下文上；提示词预览的上下文不绑定，在 `_resolve_scope_uncached` 的令牌判断
    那一步就被拒绝。变量来源（例如角色状态插件）因此不会在预览里建模板、写状态。
    """
    try:
        provider = await wb_variables.provider_for_round(
            wb_variables.RoundVariableContext(
                one_time_code=code,
                channel=channel,
                chat_key=str(channel.chat_key),
                preset_id=preset_id if preset_id > 0 else None,
                is_private=getattr(channel, "chat_type", None) == ChatType.PRIVATE,
                trigger_person_uid=str(getattr(ctx, "from_platform_userid", "") or ""),
                config=config,
                dry_run=False,
            ),
        )
    except Exception:  # noqa: BLE001 - 变量来源不该挡住查询；按空提供者算
        return template_lite.EMPTY_VARIABLES
    return provider or template_lite.EMPTY_VARIABLES


def _entry_keywords(entry: DBWbEntry) -> list[str]:
    """主关键词与次关键词，统一转小写；正则写法的关键词按原文当普通文字比。"""
    values: list[str] = []
    for field_name in ("keys", "secondary_keys"):
        raw = getattr(entry, field_name, None)
        if not isinstance(raw, (list, tuple)):
            continue
        for value in raw:
            text = str(value or "").strip().casefold()
            if text:
                values.append(text)
    return values


def _keyword_hit(keywords: Iterable[str], term: str) -> bool:
    """查询词是否命中任一关键词；关键词至少 2 字，双向包含都算。"""
    if not term:
        return False
    for keyword in keywords:
        if len(keyword) < 2:
            continue
        if term in keyword or keyword in term:
            return True
    return False


def _entry_text(entry: DBWbEntry) -> EntryText:
    """把一条条目压成打分要用的文字（加载时算一次，之后不再 `casefold`）

    模板条目（`prepare_content` 用 `needs_analysis` 判的那一类）的 `content` 留空：模板原文
    里没走到的分支不该被查询词命中，正文那部分分数等第二段拿到渲染后的文字再补。
    """
    content = str(getattr(entry, "content", "") or "")
    is_template = wb_purpose.needs_analysis(content)
    return EntryText(
        id=int(getattr(entry, "id", 0) or 0),
        order=int(getattr(entry, "order", 0) or 0),
        title=str(getattr(entry, "comment", "") or "").casefold(),
        keywords=tuple(_entry_keywords(entry)),
        content="" if is_template else content.casefold(),
        is_template=is_template,
    )


def _query_terms(query: str) -> tuple[str, list[str]]:
    """整句（去首尾空白、casefold）与查询词表；整句也算一个查询词，保持出现顺序并去重"""
    raw_query = str(query or "")[:MAX_QUERY_CHARS]
    whole = raw_query.strip().casefold()
    extracted = [str(term).casefold() for term in extract_query_terms(raw_query, limit=8) if str(term).strip()]
    ordered_terms: list[str] = []
    for term in ([whole] if whole else []) + extracted:
        if term and term not in ordered_terms:
            ordered_terms.append(term)
    return whole, ordered_terms


def _score_one(whole: str, terms: Sequence[str], entry: EntryText, *, content: Optional[str] = None) -> float:
    """一条条目的总分；`content` 给了就用它代替预算好的正文（模板条目第二段用渲染结果）"""
    title = entry.title
    body = entry.content if content is None else content
    keywords = entry.keywords
    score = 0.0
    if whole:
        if whole in title:
            score += 0.6
        if _keyword_hit(keywords, whole):
            score += 0.5
        if whole in body:
            score += 0.3
    matched = 0
    for term in terms:
        hit = False
        if term in title:
            score += 0.15
            hit = True
        if _keyword_hit(keywords, term):
            score += 0.12
            hit = True
        if term in body:
            score += 0.05
            hit = True
        if hit:
            matched += 1
    score += matched / len(terms) * 0.2
    return score


def _score_prepared(
    whole: str,
    terms: Sequence[str],
    entries: Sequence[EntryText],
) -> list[tuple[float, int, int]]:
    """对加载时预算好的文字打分，纯函数；返回 `(分数, order, 编号)`，按分数降序、order 降序、编号升序

    只碰普通字符串与编号，不碰 ORM 对象，`search` 把它整个放进线程里跑。总分为 0 的条目
    不返回。
    """
    if not terms:
        return []
    scored: list[tuple[float, int, int]] = []
    for entry in entries:
        score = _score_one(whole, terms, entry)
        if score > 0:
            scored.append((score, entry.order, entry.id))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return scored


def score_entries(query: str, entries: Iterable[DBWbEntry]) -> list[tuple[float, DBWbEntry]]:
    """按字面命中给条目打分，纯函数

    权重与 `services/kb/search_service.py` 的关键词通道同一个方向（标题最高、关键词
    高于正文、整句命中最高），但不引入知识库的文档类型：

    - 整句命中标题 0.6、命中任一关键词 0.5、命中正文 0.3；
    - 每个查询词命中标题 0.15、关键词 0.12、正文 0.05；
    - 再按「命中的查询词数 / 查询词总数」加最多 0.2。

    查询词来自 `extract_query_terms(query, limit=8)`，整句（去首尾空白、casefold）
    也算一个查询词。总分为 0 的不返回；排序按分数降序、条目 `order` 降序、`id` 升序。
    模板条目的正文不参与（与 `search` 的第一段一致），正文那部分在渲染后另行补算。
    """
    rows = list(entries)
    whole, ordered_terms = _query_terms(query)
    if not ordered_terms:
        return []
    prepared = [_entry_text(entry) for entry in rows]
    by_id = {text.id: entry for text, entry in zip(prepared, rows)}
    ranked = _score_prepared(whole, ordered_terms, prepared)
    return [(score, by_id[entry_id]) for score, _order, entry_id in ranked if entry_id in by_id]


def _inline(value: str, max_length: int) -> str:
    """单行字段清洗，再拆掉像边界标签的片段（与注入路径同一个顺序）。"""
    return defang_boundary_tags(sanitize_inline_text(str(value or ""), max_length))


def _clean_content(text: str) -> str:
    """正文清洗，再拆掉像边界标签的片段；返回空串表示这一条什么都不给。"""
    return defang_boundary_tags(sanitize_block_text(str(text or ""), MAX_ITEM_CONTENT_CHARS))


def _render_entry(
    entry: DBWbEntry,
    scope: LookupScope,
    template_budget: wb_purpose.RoundBudget,
) -> Optional[str]:
    """按与注入同一套方式算这一条的正文；不给的条目返回 None

    `prepare_content` 的 `reason` 不为空时，这一条按设计就不进主提示词（分流、不支持的
    模板、疑似防盗、出错、为空、模板预算用完），查询侧一样不给。`rendered.untrusted`
    不用另外处理：整份查询结果本来就是不可信块。
    """
    rendered = wb_purpose.prepare_content(
        entry,
        str(entry.content or ""),
        template_budget,
        scope.variables,
    )
    if rendered.reason is not None:
        return None
    try:
        text = scope.expand(rendered.content)
    except Exception:  # noqa: BLE001 - 宏展开出错只影响这一条
        return None
    cleaned = _clean_content(text)
    return cleaned or None


@dataclass(frozen=True)
class _ItemMeta:
    """一条结果除正文以外的字段（已清洗），以及它们占的 token"""

    book: str
    title: str
    keys: tuple[str, ...]
    tokens: int


def _item_meta(entry: DBWbEntry, scope: LookupScope, budget: WorldbookBudget) -> _ItemMeta:
    """书名、标题、关键词同样计入 token 上限；书名字段缺失时退回空串，不抛。"""
    book_row = scope.books.get(entry.book_id)
    book = _inline(str(getattr(book_row, "name", "") or ""), MAX_BOOK_NAME_CHARS)
    title = _inline(str(entry.comment or ""), MAX_TITLE_CHARS)
    keys = tuple(_inline(str(value), MAX_KEY_CHARS) for value in (entry.keys or [])[:5] if str(value).strip())
    text = "\n".join(part for part in (book, title, *keys) if part)
    return _ItemMeta(book=book, title=title, keys=keys, tokens=budget.count(text) if text else 0)


def _make_item(
    entry: DBWbEntry,
    meta: _ItemMeta,
    content: str,
    *,
    truncated: bool,
    budget: WorldbookBudget,
) -> LookupItem:
    """拼一条结果；`tokens` 是书名、标题、关键词与正文合计。"""
    return LookupItem(
        id=int(entry.id),
        book_id=int(entry.book_id),
        book=meta.book,
        title=meta.title,
        keys=meta.keys,
        content=content,
        truncated=truncated,
        chars=len(content),
        tokens=meta.tokens + budget.count(content),
    )


def _strict_int(value: Any) -> Optional[int]:
    """模型传来的数字参数：只认整数与数值上是整数的有限浮点数（及其字符串），其余返回 None

    `int(float("inf"))` 会抛 `OverflowError`、`int(float("nan"))` 抛 `ValueError`；`bool` 是 `int`
    的子类，`True` 不能当 1 用。
    """
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and value != number:
        return None
    return number


_BAD_ARGUMENT = "参数不对：limit 应为 1 ~ 10 的整数，entry_ids 应为整数编号的列表。"


def _preview(text: str) -> str:
    """放不下的条目给前 200 字预览；末尾加省略号，让模型一眼看出不是全文。"""
    if len(text) <= MAX_PREVIEW_CHARS:
        return text
    return text[:MAX_PREVIEW_CHARS] + "…"


def _fit_to_tokens(text: str, budget: WorldbookBudget, remaining: int) -> tuple[str, int] | None:
    """把一条正文截到 `remaining` token 以内；放不下返回 None

    先按字数比例估算，再用这一轮的尺子核对；超了就按比例缩，最多核对 6 次。每轮缩完
    后至少比上一次少一个字，避免估算一直落在边界上兜圈。
    """
    if remaining <= 0 or not text:
        return None
    full = budget.count(text)
    if full <= remaining:
        return text, full
    chars = max(1, int(len(text) * remaining / max(1, full)))
    chars = min(chars, len(text) - 1)
    for _ in range(MAX_TRUNCATE_TRIES):
        if chars <= 0:
            return None
        candidate = text[:chars].rstrip()
        if not candidate:
            return None
        marked = candidate + "…"
        size = budget.count(marked)
        if size <= remaining:
            return marked, size
        next_chars = int(chars * remaining / max(1, size))
        chars = min(chars - 1, next_chars)
    return None


def _note_truncated() -> str:
    return f"部分条目只给了前 {MAX_PREVIEW_CHARS} 字预览，完整正文请用 read_worldbook_entries 按条目编号读取。"


async def search(ctx: "AgentCtx", query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> "LookupResult | LookupDenied":
    """按查询词查本频道生效的世界书

    `limit` 夹到 1 ~ 10；空查询拒绝。打分分两段：第一段在线程里对加载时预算好的文字打分，
    先取前 `limit × 3` 个候选（普通条目的标题、关键词、正文都参与；模板条目只按标题与关键词
    进候选，正文不参与）；第二段逐条算出这一轮真正要给的正文，模板条目用渲染后的正文重算
    正文那一部分分数（权重不变），按总分重排、丢掉总分为 0 的，再取前 `limit` 条。

    模板条目如果只在正文里命中、标题与关键词都没命中，第一段就进不了候选，这次搜不到；
    需要时可以先用 `read_worldbook_entries` 按编号读取。这是不让模板原文参与打分的代价：
    没走到的分支也就不会被查询词命中、拿来探测剧透。

    token 上限按顺序累加：放得下的给全文；放不下的那条及之后只给 200 字预览并标
    `truncated`，预览也放不下就不再给，结果里写明还剩几条。
    """
    exhausted = _exhausted_before_lookup(ctx)
    if exhausted is not None:
        return exhausted
    scope = await resolve_scope(ctx)
    if isinstance(scope, LookupDenied):
        return scope
    raw_query = str(query or "").strip()
    if not raw_query:
        return LookupDenied("empty_query", "查询内容不能为空。")
    # 参数校验在占次数之前：传错参数不该吃掉这一轮的名额。
    parsed_limit = _strict_int(limit)
    if parsed_limit is None:
        return LookupDenied("bad_argument", _BAD_ARGUMENT)
    resolved_limit = max(1, min(MAX_SEARCH_LIMIT, parsed_limit))
    if not _reserve_call(scope.one_time_code, _max_calls(scope.config)):
        return LookupDenied("call_limit", "本轮查询次数已用完。")

    budget = resolve_budget(scope.config)
    max_tokens = _max_tokens(scope.config)
    # 第二段要把前 `limit × 3` 个候选都算一遍正文（模板条目重排要用），名额按候选数给。
    template_budget = wb_purpose.RoundBudget(analyses_left=max(1, ANALYSES_PER_ENTRY * resolved_limit * 3))

    whole, terms = _query_terms(raw_query)
    # 线程里只碰加载时预算好的文字与编号，不碰 ORM 对象。
    ranked = await asyncio.to_thread(_score_prepared, whole, terms, scope.entry_texts)
    prepared_by_id = {text.id: text for text in scope.entry_texts}

    reranked: list[tuple[float, int, int, DBWbEntry, str]] = []
    for score, _order, entry_id in ranked[: resolved_limit * 3]:
        entry = scope.entries.get(entry_id)
        prepared = prepared_by_id.get(entry_id)
        if entry is None or prepared is None:
            continue
        text = _render_entry(entry, scope, template_budget)
        if text is None:
            continue
        total = score
        if prepared.is_template:
            total = _score_one(whole, terms, prepared, content=text.casefold())
        if total <= 0:
            continue
        reranked.append((total, prepared.order, entry_id, entry, text))
    reranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
    candidates: list[tuple[DBWbEntry, str]] = [(row[3], row[4]) for row in reranked[:resolved_limit]]

    items: list[LookupItem] = []
    used = 0
    preview_only = False
    truncated = False
    dropped = 0
    for index, (entry, text) in enumerate(candidates):
        meta = _item_meta(entry, scope, budget)
        if not preview_only:
            item = _make_item(entry, meta, text, truncated=False, budget=budget)
            if used + item.tokens <= max_tokens:
                items.append(item)
                used += item.tokens
                continue
            preview_only = True
        item = _make_item(entry, meta, _preview(text), truncated=True, budget=budget)
        if used + item.tokens > max_tokens:
            # 预览都放不下：这一条与之后的都不给，不再逐条试。
            dropped = len(candidates) - index
            break
        items.append(item)
        used += item.tokens
        truncated = True

    note_parts: list[str] = []
    if truncated:
        note_parts.append(_note_truncated())
    if dropped:
        note_parts.append(f"还有 {dropped} 条没放下。")
    result = LookupResult(items=tuple(items), note="".join(note_parts))

    _note_lookup(scope, result)
    logger.info(
        f"世界书查询: method=search, items={len(result.items)}, missing={len(result.missing)}, "
        f"truncated={truncated}",
    )
    return result


async def read(ctx: "AgentCtx", entry_ids: Sequence[int]) -> "LookupResult | LookupDenied":
    """按条目编号读取本频道生效的世界书正文

    最多取前 5 个编号并去重，多了在结果里说明；不在作用域里的编号一律当「不存在」，
    只列进 `missing`。按传入顺序出正文，token 上限下放不下的那条截到放得下为止并标
    `truncated`；核对 6 次还放不下就不给。
    """
    exhausted = _exhausted_before_lookup(ctx)
    if exhausted is not None:
        return exhausted
    scope = await resolve_scope(ctx)
    if isinstance(scope, LookupDenied):
        return scope

    # 参数校验在占次数之前：传错参数不该吃掉这一轮的名额。
    if isinstance(entry_ids, (str, bytes)) or not isinstance(entry_ids, Iterable):
        return LookupDenied("bad_argument", _BAD_ARGUMENT)
    resolved_ids: list[int] = []
    for raw in entry_ids:
        value = _strict_int(raw)
        if value is None:
            return LookupDenied("bad_argument", _BAD_ARGUMENT)
        if value not in resolved_ids:
            resolved_ids.append(value)
        # 凑够「上限 + 1」个不同的编号就停：只需要知道多没多，模型传来几万个编号也不逐个去重。
        if len(resolved_ids) > MAX_READ_IDS:
            break
    if not _reserve_call(scope.one_time_code, _max_calls(scope.config)):
        return LookupDenied("call_limit", "本轮查询次数已用完。")
    over_limit = len(resolved_ids) > MAX_READ_IDS
    resolved_ids = resolved_ids[:MAX_READ_IDS]

    budget = resolve_budget(scope.config)
    max_tokens = _max_tokens(scope.config)
    template_budget = wb_purpose.RoundBudget(analyses_left=max(1, ANALYSES_PER_ENTRY * max(1, len(resolved_ids))))

    items: list[LookupItem] = []
    missing: list[int] = []
    used = 0
    truncated = False
    dropped = 0
    for entry_id in resolved_ids:
        entry = scope.entries.get(entry_id)
        if entry is None:
            missing.append(entry_id)
            continue
        text = _render_entry(entry, scope, template_budget)
        if text is None:
            # 按用途或模板不给的条目，与不存在的条目一样处理，不区分原因。
            missing.append(entry_id)
            continue
        meta = _item_meta(entry, scope, budget)
        fitted = _fit_to_tokens(text, budget, max_tokens - used - meta.tokens)
        if fitted is None:
            dropped += 1
            continue
        content, _size = fitted
        item = _make_item(entry, meta, content, truncated=content != text, budget=budget)
        items.append(item)
        used += item.tokens
        if content != text:
            truncated = True

    note_parts: list[str] = []
    if over_limit:
        note_parts.append(f"一次最多读取 {MAX_READ_IDS} 个编号，只处理了前 {MAX_READ_IDS} 个。")
    if truncated:
        note_parts.append("部分条目因长度上限只给了截断后的正文。")
    if dropped:
        note_parts.append(f"还有 {dropped} 条因长度上限没放下。")
    result = LookupResult(items=tuple(items), missing=tuple(missing), note="".join(note_parts))

    _note_lookup(scope, result)
    logger.info(
        f"世界书查询: method=read, items={len(result.items)}, missing={len(result.missing)}, "
        f"truncated={truncated}",
    )
    return result


def _note_lookup(scope: LookupScope, result: LookupResult) -> None:
    """把这次真正给出去了的条目记进提示词明细；只记编号、字数与 token，不记正文。

    明细写失败只在 `prompt_rounds` 里记调试日志，这里不额外处理：查询结果已经准备好，
    不能因为明细出问题把结果吞掉。
    """
    if not result.items:
        return
    prompt_rounds.note_worldbook_lookup(
        scope.one_time_code,
        [(item.id, item.book_id, item.chars, item.tokens) for item in result.items],
    )


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "MAX_PREVIEW_CHARS",
    "MAX_QUERY_CHARS",
    "MAX_READ_IDS",
    "MAX_SCOPE_CACHE_CHARS",
    "MAX_SEARCH_LIMIT",
    "EntryText",
    "LookupDenied",
    "LookupItem",
    "LookupResult",
    "LookupScope",
    "availability",
    "discard_round",
    "read",
    "reset_lookup_state",
    "resolve_scope",
    "score_entries",
    "search",
]
