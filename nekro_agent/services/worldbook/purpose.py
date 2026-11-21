"""世界书条目的用途分类、模板求值与「疑似防盗」判断

## 为什么按用途路由，而不是导入时停用

MVU 一类的卡把世界书当成两样东西用：一部分是给主模型看的设定，另一部分是给「变量更新」
那一步看的规则（怎么写 `<UpdateVariable>`、变量初值、把整份变量渲染出来的模板）。后者进了
主提示词，模型就会在群里写出变量块。导入时把这些条目停用等于替管理员改数据，以后有了状态
功能还得一条条再打开，所以不那样做，而是**运行时按用途决定进不进主提示词**：用途由本模块的
纯函数从标题与正文算出来，不改库、不迁移、不碰启用状态。

| 用途 | 判据 | 主提示词 |
|---|---|---|
| 普通 | 其它 | 照常（有模板就按安全子集算） |
| 剧情专用 `mvu_plot` | 标题带 `[mvu_plot]`（正文讲 `<UpdateVariable>` / `_.set(` 格式的按更新规则算） | 照常 |
| 更新规则 `mvu_update` | 标题带 `[mvu_update]`，或标题带 `[mvu_plot]` 且正文讲变量更新格式 | 不进（只给以后的更新调用） |
| 初值 `initvar` | 标题带 `[initvar]` / `[InitialVariables]`，或正文以 `@@initial_variables` 开头 | 不进 |
| 状态渲染 `state_render` | 正文调用 `getMessageVar` / `applyVarYamlAnnotate` / `format_message_variable` 把整份变量打出来，且模板代码占一半以上 | 不进（以后由 NA 的状态块取代） |
| 含不支持的模板 `unsupported_template` | 安全子集解析失败（含超长、超深） | 不进 |

前三种不进主提示词的统称「仅在开启角色状态时使用」：状态功能上线后它们也只给更新调用或
初始化用，同样不进主提示词，所以这里不需要一个「状态功能开没开」的开关。

## 每轮的开销有上限

运行时（`runtime.evaluate`）分两步：

1. **匹配之前只看标题**（:func:`quick_route`，正则查标题，不碰正文）：`[mvu_update]` /
   `[initvar]` 这类条目直接分流，放到最后判命中、只为在明细里解释；
2. **命中之后、进预算之前才分析正文**（:func:`prepare_content`，由 `selection.select_hits`
   按入选顺序逐条调用）：没有模板的条目原样返回；有模板的才编译（按 `(条目 ID, 更新时间)`
   缓存）、判用途、按空变量求值。条数上限之后的条目根本不会被分析。

分析（编译加几次对比渲染）从一份**每轮模板预算**（:class:`RoundBudget`）里扣：按字数（每次
至少按 512 字记账），带模板的命中条目还按条数封顶（4 × 每轮条数上限），扣完了剩下的模板条目
这一轮记成 `template_budget`、不注入。它与 token 预算是两回事：模板预算管分析的 CPU 开销，
token 预算只数真正注入的正文。

变量提供者为空（状态功能上线之前）时，按空变量渲染出的文字只取决于条目本身，所以分析时就把它
算好、跟语法树一起缓存：以后每轮直接用，不再求值；算出来为空（`template_empty`）或出错
（`template_error`）的也就成了「已知要分流」。语法树缓存是 LRU，按条数与估算的内存两头封顶。
运行时不跑防盗扫描——那只给导入报告与书详情用；书详情另有一份只存摘要的缓存，不碰语法树缓存。

已经知道要分流的条目（摘要或语法树缓存里有结论的）在匹配时也推迟到最后判，与按标题分流的
一样，不抢正则每轮的预算、不占模板条数；第一次见到的要等命中、分析之后才知道。

## 书详情与导入的分析不占共享线程池

书详情的分析在一个专用的单线程执行器里跑，同一时间只跑一个请求的：执行器忙的时候，这一页
没缓存的条目直接返回「分析中」（页面会自己再取），不排队；正在算的条目记在一个集合里，别的
请求不会重复算。导入（世界书文件、卡内书）的分析在导入专用的执行器里跑（两个线程），卡片检查页
另有一个单线程执行器；每个租户同一时间只能有一个导入、一个卡片检查在分析，多出来的直接 409。
这样世界书的分析最多占四个线程，不会把 `asyncio.to_thread` 共用的默认线程池占满、拖慢别处
（token 计数、导入之外的几十个调用点）。

## 疑似防盗条目

有的卡针对「不执行模板的客户端」埋了诱饵，三种都在导入报告与书详情里单列：

- 模板先探测运行环境（读 `localStorage`、看 `window` 上有没有某个标记），再按结果输出大段
  文字——这种写法在子集之外，本来就不注入；
- 一个永远不成立的 `if (false)`，分支里藏着大段文字——按子集求值时这段不输出；
- 变量读不到时才出现的大段文字（`if (!getvar('stat_data'))`、`<%= getvar('s') || '…' %>` 等）。
  NA 现在恰好就是「没有这些变量的客户端」，这一段会被算出来，所以这种条目**整条不注入**
  （原因 `template_trap`）。判法是几次渲染对比（`template_lite.compare_renders`：变量全读不到一次，
  变量都有、只有一部分变量各几次），不逐种写法去认。
"""

from __future__ import annotations

import asyncio
import functools
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Iterable, Mapping, TypeVar

from . import template_lite
from .template_lite import Template, TemplateUnsupported

# 分析缓存的上限：条数与估算内存（字节）两头封顶，LRU 淘汰。
MAX_CACHE_ENTRIES = 8192
MAX_CACHE_BYTES = 64 * 1024 * 1024
# 每轮模板编译与求值的预算（字）。按真实卡片的量，一轮命中的模板条目合计不过几万字。
TEMPLATE_ROUND_CHARS = 512 * 1024

# 「模板代码占一半以上」的门槛（代码字数 / 正文字数）。
STATE_RENDER_CODE_SHARE = 0.5


class EntryPurpose(StrEnum):
    """条目的用途"""

    NORMAL = "normal"
    MVU_PLOT = "mvu_plot"
    MVU_UPDATE = "mvu_update"
    INITVAR = "initvar"
    STATE_RENDER = "state_render"
    UNSUPPORTED_TEMPLATE = "unsupported_template"


# 不进主提示词的用途 → 明细、触发测试里记的原因。
STATE_ONLY_PURPOSES = frozenset({EntryPurpose.MVU_UPDATE, EntryPurpose.INITVAR, EntryPurpose.STATE_RENDER})
REASON_STATE_ONLY = "state_only"
REASON_TEMPLATE_UNSUPPORTED = "template_unsupported"
REASON_TEMPLATE_TRAP = "template_trap"
REASON_TEMPLATE_ERROR = "template_error"
REASON_TEMPLATE_EMPTY = "template_empty"
REASON_TEMPLATE_BUDGET = "template_budget"

_TITLE_UPDATE = re.compile(r"\[mvu_update\]", re.IGNORECASE)
_TITLE_INIT = re.compile(r"\[(?:initvar|initialvariables)\]", re.IGNORECASE)
_TITLE_PLOT = re.compile(r"\[mvu_plot\]", re.IGNORECASE)
# 标题带 `[mvu_plot]` 时才扫正文：这种条目有的在讲剧情，有的其实是变量更新格式说明。
_MVU_FORMAT_BODY = re.compile(r"<\s{0,8}/?\s{0,8}UpdateVariable\b|_\.set\s{0,8}\(", re.IGNORECASE)
_INIT_BODY = re.compile(r"\s{0,64}@@initial_variables")
_STATE_RENDER_CALL = re.compile(r"getMessageVar\s{0,8}\(|applyVarYamlAnnotate\s{0,8}\(|format_message_variable")
# 只在 `{{` 与 `}}` 之间那一小段上用，不在整段正文上扫。
_STATE_MACRO_HEAD = re.compile(r"\{\{\s{0,8}(?:format|get)_(?:message|chat|global)_variable::")


def _code_chars(content: str) -> int:
    """正文里模板代码（`<% … %>` 标签与渲染变量的宏）一共多少字

    手写的线性扫描：`<%.*?%>` 这类懒惰正则在大量未闭合的 `<%` 上对每个起点都扫到末尾，
    是二次方的。没闭合的标签把剩下的全算作代码（这种条目本来就判不支持）。
    """
    total = 0
    position = content.find("<%")
    while position >= 0:
        end = content.find("%>", position + 2)
        if end < 0:
            total += len(content) - position
            break
        total += end + 2 - position
        position = content.find("<%", end + 2)
    position = content.find("{{")
    while position >= 0:
        end = content.find("}}", position + 2)
        if end < 0:
            break
        if _STATE_MACRO_HEAD.match(content, position, end):
            total += end + 2 - position
        position = content.find("{{", end + 2)
    return total


def title_purpose(comment: str, content: str) -> EntryPurpose | None:
    """只看标题（与正文开头；标题带 `[mvu_plot]` 时再看正文里有没有变量更新格式）就能定的用途

    MVU 自己也是按标题包含这些标记来认的。正文只有在上面的标题判断都没命中、且标题带
    `[mvu_plot]` 时才扫：普通条目不因为正文里出现 `<UpdateVariable>` 就改判。
    """
    if _TITLE_INIT.search(comment) or _INIT_BODY.match(content):
        return EntryPurpose.INITVAR
    if _TITLE_UPDATE.search(comment):
        return EntryPurpose.MVU_UPDATE
    if _TITLE_PLOT.search(comment) and _MVU_FORMAT_BODY.search(content):
        return EntryPurpose.MVU_UPDATE
    return None


def quick_route(comment: str, content: str) -> str | None:
    """匹配之前的分流：只按标题，不分析正文；照常参与匹配时为 `None`"""
    return REASON_STATE_ONLY if title_purpose(comment, content) is not None else None


def needs_analysis(content: str) -> bool:
    """正文里有没有要分析的东西（模板标签，或渲染整份变量的宏）"""
    return template_lite.has_template(content) or "_variable::" in content


def _is_state_render(content: str) -> bool:
    """调用了「把整份变量打出来」的函数 / 宏，并且模板代码占一半以上"""
    if not content or not _STATE_RENDER_CALL.search(content):
        return False
    return _code_chars(content) / len(content) > STATE_RENDER_CODE_SHARE


@dataclass(frozen=True)
class EntryAnalysis:
    """一条条目的分析结果

    Attributes:
        purpose: 用途。
        template: 能按子集编译时的语法树；正文没有模板或编译失败时为 `None`。
        unsupported_reason: 编译失败的原因（给管理员看，已截短）；没失败时为空串。
        suspected_trap: 疑似防盗条目（只在带防盗扫描的分析里算）。
        trap_reason: 判为疑似防盗的依据（给管理员看）。
        missing_variable_chars: 变量读不到时才会注入的字（几次渲染对比）；够门槛时整条不注入。
        trap_scanned: 这份结果有没有跑过防盗扫描。
        rendered: 按空变量渲染出的文字（有模板且求值成功时）；求值出错时为 `None`。变量提供者为空
            期间它只取决于条目本身，所以跟着分析一起缓存；接入真实变量以后不能再这样用。
        partial_renders: 诱饵判断里「只有一部分变量」的渲染做了几次（记账用，见 :func:`_partial_cost`）。
    """

    purpose: EntryPurpose
    template: Template | None = field(default=None, repr=False)
    unsupported_reason: str = ""
    suspected_trap: bool = False
    trap_reason: str = ""
    missing_variable_chars: int = 0
    trap_scanned: bool = False
    rendered: str | None = field(default=None, repr=False)
    partial_renders: int = 0

    def routed_reason_for(self, variables: template_lite.VariableProvider) -> str | None:
        """用这个变量提供者时，不进主提示词的原因；照常注入时为 `None`

        「渲染为空 / 出错」只在 :func:`static_render_ok` 成立（开关开着、提供者是空的那一个）时
        按分析时缓存的渲染结果判；否则要等这一轮真正求值才知道。
        """
        if self.purpose in STATE_ONLY_PURPOSES:
            return REASON_STATE_ONLY
        if self.purpose is EntryPurpose.UNSUPPORTED_TEMPLATE:
            return REASON_TEMPLATE_UNSUPPORTED
        if self.missing_variable_chars >= template_lite.TRAP_MIN_CHARS:
            return REASON_TEMPLATE_TRAP
        if self.template is not None and static_render_ok(variables):
            if self.rendered is None:
                return REASON_TEMPLATE_ERROR
            if not self.rendered.strip():
                return REASON_TEMPLATE_EMPTY
        return None

    @property
    def routed_out_reason(self) -> str | None:
        """不进主提示词时的原因（按空变量，也就是现在运行时用的提供者）；照常注入时为 `None`"""
        return self.routed_reason_for(template_lite.EMPTY_VARIABLES)


# 空变量的渲染结果跟着分析缓存（`EntryAnalysis.rendered`），由它得出的「渲染为空 / 出错」进摘要、
# 进 `known_route`：这只在变量提供者为空时成立（结果只取决于条目本身）。变量提供者见
# `services/worldbook/variables.py`：没有功能登记变量来源的频道仍拿到空的那一个，照旧用这份缓存；
# 拿到真实提供者的频道由 :func:`static_render_ok` 判出来，`routed_reason_for`、`render_content`、
# `prepare_content`、`known_route` 都按这一轮的提供者走（现算、按模板正文记账、摘要里的
# `template_empty` / `template_error` 不算已知），打出变量值的条目这一轮按不可信内容注入
# （:attr:`Rendered.untrusted`）。诱饵判定（`compare_renders`）照旧只看结构与哨兵，不读这一轮的
# 变量，所以分类结论与有没有提供者无关。这个开关只在要整体停用这份缓存时才改成 False。
STATIC_RENDER_CACHE = True
_RENDER_REASONS = frozenset({REASON_TEMPLATE_EMPTY, REASON_TEMPLATE_ERROR})


def static_render_ok(variables: template_lite.VariableProvider) -> bool:
    """能不能用分析时按空变量算好的渲染结果：开关开着，并且这一轮用的就是空的提供者"""
    return STATIC_RENDER_CACHE and variables is template_lite.EMPTY_VARIABLES


# 诱饵判断里随机那两组哨兵的密钥：从服务端凭据加密的主密钥按用途派生（`tools/credential_cipher.py`
# 的 `derive_purpose_key`；那把密钥不随 JWT 轮换，换了库里的密文就解不开），所以重启前后、几个工作
# 进程之间都一样。取不到时（凭据加密不可用、单独跑这个模块）用 `template_lite` 每个进程随机的那把，
# 结论就只在一个进程里稳定。整个进程只取一次：中途换密钥会让同一条正文前后结论不一致。
_SENTINEL_KEY_PURPOSE = "worldbook-template-sentinels"
_sentinel_key_lock = threading.Lock()
_sentinel_key_value: bytes | None = None
_sentinel_key_resolved = False


def _sentinel_key() -> bytes | None:
    global _sentinel_key_value, _sentinel_key_resolved  # noqa: PLW0603
    if not _sentinel_key_resolved:
        with _sentinel_key_lock:
            if not _sentinel_key_resolved:
                try:
                    from nekro_agent.tools.credential_cipher import derive_purpose_key

                    _sentinel_key_value = derive_purpose_key(_SENTINEL_KEY_PURPOSE)
                except Exception:  # noqa: BLE001 - 取不到就退回每个进程随机的那把
                    _sentinel_key_value = None
                _sentinel_key_resolved = True
    return _sentinel_key_value


def _missing_variable_reason(chars: int) -> str:
    return f"变量读不到时比变量都有时多出约 {chars} 字（NA 现在没有这些变量，会注入这一段），这一条不注入"


def _trap(content: str, template: Template | None, missing_chars: int) -> tuple[bool, str]:
    """疑似防盗的三种情形（见模块文档）；只给导入报告与书详情用"""
    if missing_chars >= template_lite.TRAP_MIN_CHARS:
        return True, _missing_variable_reason(missing_chars)
    scan = template_lite.scan_template(content, template)
    payload = scan.conditional_chars + scan.code_string_chars
    if scan.env_identifiers and payload >= template_lite.TRAP_MIN_CHARS:
        names = "、".join(sorted(scan.env_identifiers))
        return True, f"模板探测运行环境（{names}），并按判断结果输出约 {payload} 字"
    if scan.dead_branch_chars >= template_lite.TRAP_MIN_CHARS:
        return True, f"永远不成立的判断分支里藏着约 {scan.dead_branch_chars} 字"
    return False, ""


def analyze_entry(comment: str, content: str, *, with_trap: bool = True) -> EntryAnalysis:
    """纯函数：从标题与正文算出用途、编译模板，按需判断疑似防盗

    任何意外（包括解析器里漏掉的 `RecursionError`）都只让这一条判为「含不支持的模板」，
    不往外抛：一条坏条目不该让整轮世界书、整页条目列表失败。
    """
    comment = comment or ""
    content = content or ""
    try:
        return _analyze(comment, content, with_trap=with_trap)
    except Exception:  # noqa: BLE001 - 兜底：坏条目只影响它自己
        return EntryAnalysis(
            purpose=EntryPurpose.UNSUPPORTED_TEMPLATE,
            unsupported_reason="模板分析出错",
            trap_scanned=with_trap,
        )


def _analyze(comment: str, content: str, *, with_trap: bool) -> EntryAnalysis:
    tagged = title_purpose(comment, content)
    has_template = template_lite.has_template(content)

    template: Template | None = None
    unsupported = ""
    if has_template:
        try:
            template = template_lite.compile_template(content)
        except TemplateUnsupported as error:
            unsupported = template_lite.short_reason(str(error))
        except Exception:  # noqa: BLE001 - 解析器的任何意外都按不支持处理
            unsupported = "模板解析出错"

    missing = 0
    partial_renders = 0
    rendered: str | None = None
    if template is not None:
        # 种子用正文、密钥用服务端那把持久的：同一条正文在导入报告、书详情与运行时，重启前后，诱饵结论一致。
        comparison = template_lite.compare_renders(template, seed=content, key=_sentinel_key())
        missing = comparison.bait_chars
        rendered = comparison.empty_text
        partial_renders = comparison.partial_renders

    suspected, trap_reason = False, ""
    if with_trap and has_template:
        suspected, trap_reason = _trap(content, template, missing)

    if tagged is not None:
        purpose = tagged
    elif (has_template or "_variable::" in content) and _is_state_render(content):
        # 没有模板标签也没有变量宏的正文，模板代码占比是 0，不可能是状态渲染：不必去扫一百万字。
        purpose = EntryPurpose.STATE_RENDER
    elif unsupported:
        purpose = EntryPurpose.UNSUPPORTED_TEMPLATE
    elif _TITLE_PLOT.search(comment):
        purpose = EntryPurpose.MVU_PLOT
    else:
        purpose = EntryPurpose.NORMAL
    return EntryAnalysis(
        purpose=purpose,
        template=template,
        unsupported_reason=unsupported,
        suspected_trap=suspected,
        trap_reason=trap_reason,
        missing_variable_chars=missing,
        trap_scanned=with_trap,
        rendered=rendered,
        partial_renders=partial_renders,
    )


# ---------------------------------------------------------------------------
# 两份缓存：语法树（运行时求值用）与摘要（书详情、运行时分流用）
# ---------------------------------------------------------------------------
#
# 语法树大（一条六万多字的密集模板能占几 MB），只给运行时用，LRU，条数与估算内存两头封顶；
# 书详情与导入报告不往里放，免得管理员翻一页条目就把运行时正要用的树挤掉。摘要只有用途、
# 原因与是否疑似防盗几个字段，一条几百字节，单独一份 LRU、容量大得多：书详情按它出标签，
# 运行时按它把已知要分流的条目推迟到最后判。书详情的分析跑在工作线程里，事件循环同时也在用
# 这两份缓存，读写都在一把锁里。


@dataclass
class _Cached:
    analysis: EntryAnalysis
    cost: int


@dataclass(frozen=True)
class EntrySummary:
    """一条条目分析结果的精简版：不带语法树

    Attributes:
        purpose: 用途。
        unsupported_reason: 含不支持的模板时的原因。
        routed_reason: 不进主提示词时的原因（同 :attr:`EntryAnalysis.routed_out_reason`）。
        suspected_trap: 疑似防盗；`None` 表示还没跑过防盗扫描（运行时的分析不跑）。
        trap_reason: 判为疑似防盗的依据。
    """

    purpose: EntryPurpose
    unsupported_reason: str = ""
    routed_reason: str | None = None
    suspected_trap: bool | None = None
    trap_reason: str = ""

    @classmethod
    def of(cls, analysis: EntryAnalysis) -> "EntrySummary":
        return cls(
            purpose=analysis.purpose,
            unsupported_reason=analysis.unsupported_reason,
            routed_reason=analysis.routed_out_reason,
            suspected_trap=analysis.suspected_trap if analysis.trap_scanned else None,
            trap_reason=analysis.trap_reason,
        )


# 摘要缓存的条数上限；一条几百字节，十万条也就几十 MB 以内。
MAX_SUMMARY_ENTRIES = 100_000

_lock = threading.RLock()
_cache: "OrderedDict[tuple[Any, Any], _Cached]" = OrderedDict()
_cache_bytes = 0
_summaries: "OrderedDict[tuple[Any, Any], EntrySummary]" = OrderedDict()


def _estimate_bytes(content: str, analysis: EntryAnalysis) -> int:
    """估一条分析结果占多少内存：文字节点约是原文的两倍，每个语法树节点约一两百字节

    CPython 的字符串按最宽的字符定宽：含 BMP 以外字符（如表情）的每字 4 字节，按 4 倍估。缓存的
    渲染结果另算。
    """
    nodes = analysis.template.node_count if analysis.template is not None else 0
    text = 0
    if analysis.template is not None:
        text = len(content) * (4 if content and max(content) > _BMP_MAX else 2)
    if analysis.rendered:
        text += len(analysis.rendered) * (4 if max(analysis.rendered) > _BMP_MAX else 2)
    return 256 + text + nodes * 160 + len(analysis.unsupported_reason) * 4 + len(analysis.trap_reason) * 4


_BMP_MAX = chr(0xFFFF)


def _cache_key(entry: Any) -> tuple[Any, Any]:
    return (getattr(entry, "id", None), getattr(entry, "update_time", None))


def cached_analysis(entry: Any) -> EntryAnalysis | None:
    """语法树缓存里有就返回（并挪到最近使用），没有返回 `None`；不分析"""
    key = _cache_key(entry)
    if key[0] is None:
        return None
    with _lock:
        cached = _cache.get(key)
        if cached is None:
            return None
        _cache.move_to_end(key)
        return cached.analysis


def _store(key: tuple[Any, Any], content: str, analysis: EntryAnalysis) -> None:
    global _cache_bytes
    cost = _estimate_bytes(content, analysis)
    with _lock:
        old = _cache.pop(key, None)
        if old is not None:
            _cache_bytes -= old.cost
        if cost > MAX_CACHE_BYTES:
            return
        _cache[key] = _Cached(analysis, cost)
        _cache_bytes += cost
        while _cache and (len(_cache) > MAX_CACHE_ENTRIES or _cache_bytes > MAX_CACHE_BYTES):
            _evicted_key, evicted = _cache.popitem(last=False)
            _cache_bytes -= evicted.cost


def cached_summary(entry: Any) -> EntrySummary | None:
    """摘要缓存里有就返回（并挪到最近使用），没有返回 `None`；不分析"""
    key = _cache_key(entry)
    if key[0] is None:
        return None
    with _lock:
        summary = _summaries.get(key)
        if summary is not None:
            _summaries.move_to_end(key)
        return summary


def _store_summary(key: tuple[Any, Any], summary: EntrySummary) -> None:
    if key[0] is None:
        return
    with _lock:
        old = _summaries.get(key)
        # 运行时的分析不跑防盗扫描：不能用它盖掉书详情已经算出来的防盗结论。
        if old is not None and old.suspected_trap is not None and summary.suspected_trap is None:
            summary = EntrySummary(
                purpose=summary.purpose,
                unsupported_reason=summary.unsupported_reason,
                routed_reason=summary.routed_reason,
                suspected_trap=old.suspected_trap,
                trap_reason=old.trap_reason,
            )
        _summaries[key] = summary
        _summaries.move_to_end(key)
        while len(_summaries) > MAX_SUMMARY_ENTRIES:
            _summaries.popitem(last=False)


def analysis_for(entry: Any, *, with_trap: bool = False) -> EntryAnalysis:
    """运行时：按条目行取带语法树的分析结果，按 `(id, update_time)` 缓存；顺手更新摘要"""
    comment = str(getattr(entry, "comment", "") or "")
    content = str(getattr(entry, "content", "") or "")
    key = _cache_key(entry)
    cached = cached_analysis(entry)
    if cached is not None and (cached.trap_scanned or not with_trap):
        return cached
    analysis = analyze_entry(comment, content, with_trap=with_trap)
    if key[0] is not None:
        _store(key, content, analysis)
        _store_summary(key, EntrySummary.of(analysis))
    return analysis


def summary_for(entry: Any, *, with_trap: bool = True) -> EntrySummary:
    """书详情：只要摘要；没有就分析一遍，结果只进摘要缓存、不进语法树缓存"""
    return _summary_and_partial(entry, with_trap=with_trap)[0]


def _summary_and_partial(entry: Any, *, with_trap: bool) -> tuple[EntrySummary, int]:
    """同 :func:`summary_for`，另外返回这次分析里「只有一部分变量」的渲染做了几次（用缓存时是 0）"""
    cached = cached_summary(entry)
    if cached is not None and (cached.suspected_trap is not None or not with_trap):
        return cached, 0
    analysis = analyze_entry(
        str(getattr(entry, "comment", "") or ""),
        str(getattr(entry, "content", "") or ""),
        with_trap=with_trap,
    )
    summary = EntrySummary.of(analysis)
    _store_summary(_cache_key(entry), summary)
    return summary, analysis.partial_renders


def known_route(
    entry: Any,
    variables: template_lite.VariableProvider = template_lite.EMPTY_VARIABLES,
) -> str | None:
    """不做分析、只查缓存：这条已知要分流的话返回原因，不知道或照常注入返回 `None`

    缓存里的「渲染为空 / 出错」是按空变量算的：这一轮用的是真实的变量提供者（或开关关了）时
    不算已知——同一条正文有了变量可能就算得出字来，要等 `prepare_content` 这一轮现算。与变量
    无关的三类（仅在开启角色状态时使用、不支持的模板、诱饵）照常算已知。
    """
    summary = cached_summary(entry)
    if summary is not None:
        reason = summary.routed_reason
    else:
        analysis = cached_analysis(entry)
        reason = analysis.routed_out_reason if analysis is not None else None
    if reason in _RENDER_REASONS and not static_render_ok(variables):
        return None
    return reason


# 书详情一次请求最多分析多少字（每条至少按 :data:`MIN_ANALYSIS_CHARGE` 记账，最多按扫描上限加对比
# 渲染记）。超出的条目这次先不分析，标「分析中」，下次请求接着算。按现在的记账，最坏的一页（每条
# 六万多字、几万个哨兵的模板）一次请求约 1 ~ 1.5 秒 CPU（在书详情自己的线程里），一般的页面远低于此。
LIST_ANALYSIS_CHARS = 1024 * 1024
# 每做一次模板分析至少记这么多字：几万条只有几个字的模板条目，按字数记账形同不封顶。
MIN_ANALYSIS_CHARGE = 512


def _render_cost(content: str) -> int:
    # 诱饵判断的完整渲染（变量全读不到一次、四组哨兵各一次；不读变量的模板只渲染一次）：只有编译
    # 通过的才渲染，超过模板正文上限的在编译前就拒了、不渲染。每次按四分之三份正文记——实测最坏
    # （一个或几百个变量各取几十层属性，每组几万个哨兵）一次约是编译的 0.6 倍，一般的模板远低于编译。
    # 「只有一部分变量」的那几次大多用不着渲染，做了才按 :func:`_partial_cost` 补记。
    if len(content) > template_lite.MAX_TEMPLATE_CHARS:
        return 0
    renders = 1 + template_lite.SENTINEL_SETS if template_lite.reads_variables(content) else 1
    return len(content) * renders * 3 // 4


def _partial_cost(content: str, renders: int) -> int:
    # 「只有一部分变量」的渲染（复用已经建好的哨兵）每次按四分之一份正文记：实测最坏一次约是编译的
    # 0.12 倍。分析做完才知道做了几次（`EntryAnalysis.partial_renders`），由调用方事后补记；根变量
    # 不超过 8 个时最多 9 次，再多也只按个数的对数涨（六万多字的模板最多 27 次）。
    return len(content) * renders // 4


def _analysis_charge(content: str) -> int:
    """运行时分析一条的记账：编译加对比渲染"""
    return len(content) + _render_cost(content)


def _listing_cost(content: str) -> int:
    # 书详情分析一条的开销：编译最多看模板正文上限那么多字（更长的直接拒），防盗扫描最多看
    # 扫描上限那么多字，再加对比渲染。
    return max(MIN_ANALYSIS_CHARGE, min(len(content), template_lite.MAX_SCAN_CHARS) + _render_cost(content))


# 正在分析的条目（`(条目 ID, 更新时间)`）：另一个请求碰到它直接标「分析中」，不重复算。
_inflight: set[tuple[Any, Any]] = set()


def _cached_listing_summary(entry: Any) -> EntrySummary | None:
    cached = cached_summary(entry)
    return cached if cached is not None and cached.suspected_trap is not None else None


def summaries_for_listing(entries: Iterable[Any], *, budget_chars: int | None = None) -> dict[Any, EntrySummary | None]:
    """书详情一页条目的摘要（带防盗扫描）；在分析执行器的线程里跑

    缓存里有的直接用；别的请求正在算的标 `None`；没有模板的条目分析很便宜（不扫状态渲染），
    总是算；有模板的从这次请求的预算里扣（分析前按 :func:`_listing_cost` 扣，分析后按实际做了的
    「只有一部分变量」的渲染补扣），第一条总是算（一条再大也有上限），超出预算的返回 `None`，由
    页面标「分析中」、稍后再取。
    """
    budget = LIST_ANALYSIS_CHARS if budget_chars is None else budget_chars
    spent = 0
    result: dict[Any, EntrySummary | None] = {}
    for entry in entries:
        entry_id = getattr(entry, "id", None)
        cached = _cached_listing_summary(entry)
        if cached is not None:
            result[entry_id] = cached
            continue
        content = str(getattr(entry, "content", "") or "")
        if needs_analysis(content):
            cost = _listing_cost(content)
            if spent and spent + cost > budget:
                result[entry_id] = None
                continue
            spent += cost
        key = _cache_key(entry)
        with _lock:
            if key in _inflight:
                result[entry_id] = None
                continue
            _inflight.add(key)
        try:
            result[entry_id], partial = _summary_and_partial(entry, with_trap=True)
            # 「只有一部分变量」的渲染做了才补记（可能让这次请求超出预算一条的量，下一条就不算了）
            spent += _partial_cost(content, partial)
        finally:
            with _lock:
                _inflight.discard(key)
    return result


# ---------------------------------------------------------------------------
# 分析执行器：书详情、导入、卡片检查页各一个线程，都不占默认线程池
# ---------------------------------------------------------------------------

_listing_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wb-listing")
# 导入两个线程：每个租户同一时间只有一个导入，两个线程让一个大导入不至于把别的租户的小导入
# 堵在后面（纯 Python 的活受 GIL 限制，再多线程也不会更快）。
_import_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wb-import")
_inspect_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wb-inspect")
# 书详情的执行器同一时间只有一个任务（一页或一条）：拿不到槽位就说明正忙，不排队。槽位在
# 任务的 `Future` 结束时释放（跑完、出错，或者还没开始就被取消都算），请求被取消也不会让它
# 永远占着。
_listing_slot = threading.Lock()

_T = TypeVar("_T")


def _release_listing_slot(_future: Any) -> None:
    _listing_slot.release()


async def _submit_listing(func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T | None:
    """拿到槽位就把任务交给书详情的执行器，拿不到返回 `None`（调用方标「分析中」）"""
    if not _listing_slot.acquire(blocking=False):
        return None
    try:
        future = _listing_executor.submit(functools.partial(func, *args, **kwargs))
    except BaseException:
        _listing_slot.release()
        raise
    future.add_done_callback(_release_listing_slot)
    return await asyncio.wrap_future(future)


async def listing_summaries(entries: Iterable[Any]) -> dict[Any, EntrySummary | None]:
    """书详情一页条目的摘要：缓存里有的直接给；执行器忙就把没缓存的都标「分析中」，不排队"""
    rows = list(entries)
    result = {getattr(entry, "id", None): _cached_listing_summary(entry) for entry in rows}
    if all(summary is not None for summary in result.values()):
        return result
    computed = await _submit_listing(summaries_for_listing, rows)
    return result if computed is None else computed


async def single_summary(entry: Any) -> EntrySummary | None:
    """新建、修改、启停一条之后返回的摘要；书详情的执行器正忙时返回 `None`（页面标「分析中」）"""
    cached = _cached_listing_summary(entry)
    if cached is not None:
        return cached
    return await _submit_listing(summary_for, entry, with_trap=True)


# 一次导入（或一次卡片检查）最多分析多少字的模板（记账同书详情：每条至少 512 字，编译 / 扫描
# 加对比渲染）。超出的条目报告里记「导入时未分析」，运行时命中后照常分析、照常分流。
IMPORT_ANALYSIS_CHARS = 4 * 1024 * 1024
NOTE_NOT_ANALYZED = "template_not_analyzed"

# 每个租户（平台方算一个）同一时间只能有一个导入在分析、一个卡片检查在分析；多出来的直接拒绝。
_gate_lock = threading.Lock()
_import_running: set[str] = set()
_inspect_running: set[str] = set()


def tenant_slot_key(tenant_id: int | None) -> str:
    """限并发用的租户键：平台方算一个"""
    return "platform" if tenant_id is None else f"tenant:{int(tenant_id)}"


def _ensure_free(running: set[str], tenant_key: str) -> None:
    from nekro_agent.schemas.errors import WorldbookAnalysisBusyError

    with _gate_lock:
        busy = tenant_key in running
    if busy:
        raise WorldbookAnalysisBusyError()


def ensure_import_slot_free(tenant_key: str) -> None:
    """这个租户已有导入在分析就先拒掉（接口在读上传文件之前调）

    上传在进到接口之前已经被框架整个收下（multipart 表单先解析完才调接口），这一步省不了带宽，省的是
    把上传文件读进内存、解析（卡片还有头像处理）这些事件循环上的活。只是提前检查；真正占名额仍在
    :func:`run_import_analysis` 里，两个请求同时过了这一关也只有一个能占到。
    """
    _ensure_free(_import_running, tenant_key)


def ensure_inspect_slot_free(tenant_key: str) -> None:
    """同 :func:`ensure_import_slot_free`，给卡片检查页"""
    _ensure_free(_inspect_running, tenant_key)


async def _run_gated(
    executor: ThreadPoolExecutor,
    running: set[str],
    tenant_key: str,
    func: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> _T:
    from nekro_agent.schemas.errors import WorldbookAnalysisBusyError

    with _gate_lock:
        if tenant_key in running:
            raise WorldbookAnalysisBusyError()
        running.add(tenant_key)

    def release(_future: Any) -> None:
        with _gate_lock:
            running.discard(tenant_key)

    try:
        future = executor.submit(functools.partial(func, *args, **kwargs))
    except BaseException:
        release(None)
        raise
    future.add_done_callback(release)
    return await asyncio.wrap_future(future)


async def run_import_analysis(func: Callable[..., _T], *args: Any, tenant_key: str, **kwargs: Any) -> _T:
    """导入（世界书文件、卡内书）的分析：导入专用的执行器，每个租户同一时间只跑一个

    调用方必须在**开事务之前**调它：分析期间不占数据库连接。不同租户的导入在执行器里排队，
    每个的分析字数有上限（:data:`IMPORT_ANALYSIS_CHARS`），排队的时间也就有上限。
    """
    return await _run_gated(_import_executor, _import_running, tenant_key, func, *args, **kwargs)


async def run_inspect_analysis(func: Callable[..., _T], *args: Any, tenant_key: str, **kwargs: Any) -> _T:
    """卡片检查页的分析：自己一个执行器，不和导入抢队列；每个租户同一时间只跑一个"""
    return await _run_gated(_inspect_executor, _inspect_running, tenant_key, func, *args, **kwargs)


def clear_cache() -> None:
    """清空两份缓存（用例用）"""
    global _cache_bytes
    with _lock:
        _cache.clear()
        _cache_bytes = 0
        _summaries.clear()


def cache_stats() -> tuple[int, int]:
    """语法树缓存的（条数, 估算字节）；给用例与实测用"""
    with _lock:
        return len(_cache), _cache_bytes


# ---------------------------------------------------------------------------
# 运行时：命中之后、进预算之前
# ---------------------------------------------------------------------------

# 每轮最多分析 / 求值多少条带模板的命中条目；运行时按 4 × 每轮条数上限传进来，这里是默认值。
TEMPLATE_ROUND_ANALYSES = 128


def _round_chars() -> int:
    return TEMPLATE_ROUND_CHARS


def _round_analyses() -> int:
    return TEMPLATE_ROUND_ANALYSES


@dataclass
class RoundBudget:
    """一轮里模板编译与求值的预算：字数（每次至少按 :data:`MIN_ANALYSIS_CHARGE` 记账）与条数

    扣不动的条目这一轮记成 `template_budget`。分流出去的命中不占每轮条数上限，所以光按字数
    记账不够：几万条只有几个字的模板条目同时命中时，每条都要查一次、分析一次。
    """

    chars_left: int = field(default_factory=_round_chars)
    analyses_left: int = field(default_factory=_round_analyses)

    def take(self, chars: int) -> bool:
        charge = max(MIN_ANALYSIS_CHARGE, chars)
        if charge > self.chars_left:
            return False
        self.chars_left -= charge
        return True

    def spend(self, chars: int) -> None:
        """事后补记已经花掉的字数（可以扣成负数：之后的 :meth:`take` 都扣不动）"""
        self.chars_left -= chars

    def take_entry(self) -> bool:
        if self.analyses_left <= 0:
            return False
        self.analyses_left -= 1
        return True


@dataclass(frozen=True)
class Rendered:
    """一条条目这一轮实际要注入的正文

    `reason` 不为空时这一条不注入：`state_only` / `template_unsupported` / `template_trap`
    （用途或模板分流）、`template_error`（求值出错或输出超限）、`template_empty`（算完一个字也
    没剩）、`template_budget`（这一轮的模板预算用完）。

    `untrusted` 为真：这一轮用的是真实的变量提供者，而这条模板的输出标签把变量值打了出来。变量值
    来自聊天，调用方要把这一条按不可信内容注入（清洗、包进不可信块），不管它原来是谁写的。
    只用变量挑段落的模板输出的都是原文，不算。
    """

    content: str
    reason: str | None = None
    untrusted: bool = False


def render_content(
    content: str,
    analysis: EntryAnalysis,
    variables: template_lite.VariableProvider = template_lite.EMPTY_VARIABLES,
) -> Rendered:
    """按分析结果算出这一条要注入的正文；没有模板的条目原样返回

    调用方先用 :attr:`EntryAnalysis.routed_out_reason` 判过路由，这里只处理会注入的条目。
    任何意外都只让这一条记成 `template_error`。
    """
    if analysis.template is None:
        return Rendered(content)
    untrusted = False
    if static_render_ok(variables):
        # 空变量的结果在分析时已经算好（见 :attr:`EntryAnalysis.rendered`）。
        text = analysis.rendered
        if text is None:
            return Rendered("", REASON_TEMPLATE_ERROR)
    else:
        try:
            text = template_lite.render(analysis.template, variables)
            untrusted = variables is not template_lite.EMPTY_VARIABLES and template_lite.outputs_variables(
                analysis.template,
            )
        except Exception:  # noqa: BLE001 - 求值的任何意外都只影响这一条
            return Rendered("", REASON_TEMPLATE_ERROR)
    if not text.strip():
        return Rendered("", REASON_TEMPLATE_EMPTY)
    return Rendered(text, untrusted=untrusted)


def prepare_content(
    entry: Any,
    content: str,
    budget: RoundBudget,
    variables: template_lite.VariableProvider = template_lite.EMPTY_VARIABLES,
) -> Rendered:
    """运行时：一条命中的条目这一轮注入什么（由 `select_hits` 按入选顺序调用）

    没有模板的条目原样返回，不花预算。有模板的：先占一个条数名额；语法树缓存里没有就先从
    字数预算里扣（编译加对比渲染）再分析（不跑防盗扫描），分析完按实际做了的「只有一部分变量」的
    渲染补扣；分流的条目返回原因；会注入的用分析时已经按空变量算好的文字，不再求值、不再扣字数
    （:func:`static_render_ok` 不成立时——接入真实变量以后——这一轮现算，按模板正文记账）。
    """
    if not needs_analysis(content):
        return Rendered(content)
    if not budget.take_entry():
        return Rendered("", REASON_TEMPLATE_BUDGET)
    analysis = cached_analysis(entry)
    if analysis is None:
        if not budget.take(_analysis_charge(content)):
            return Rendered("", REASON_TEMPLATE_BUDGET)
        analysis = analysis_for(entry)
        budget.spend(_partial_cost(content, analysis.partial_renders))
    reason = analysis.routed_reason_for(variables)
    if reason is not None:
        return Rendered("", reason)
    if analysis.template is not None and not static_render_ok(variables):
        if not budget.take(analysis.template.source_chars):
            return Rendered("", REASON_TEMPLATE_BUDGET)
    return render_content(content, analysis, variables)


# ---------------------------------------------------------------------------
# 导入报告
# ---------------------------------------------------------------------------


def template_import_notes(
    columns_list: Iterable[tuple[str, Mapping[str, Any]]],
    *,
    budget_chars: int | None = None,
) -> list[Any]:
    """导入报告里的几类说明：仅在开启角色状态时使用、含不支持的模板、疑似防盗条目、导入时未分析

    返回 `st_format.ImportNote`；在函数里导入是为了不让 `st_format` 与本模块互相引用。带模板的
    条目从 `budget_chars`（默认 :data:`IMPORT_ANALYSIS_CHARS`）里扣（记账同书详情），扣完的只看标题
    （按标题定的用途照样报），其余记「导入时未分析」——运行时命中后照常分析、照常分流，不会因此
    注入代码或诱饵。
    """
    from .st_format import ImportNote

    budget = IMPORT_ANALYSIS_CHARS if budget_chars is None else budget_chars
    spent = 0
    notes: list[ImportNote] = []
    for entry_key, columns in columns_list:
        comment = str(columns.get("comment") or "")
        content = str(columns.get("content") or "")
        if needs_analysis(content):
            cost = _listing_cost(content)
            if spent and spent + cost > budget:
                tagged = title_purpose(comment, content)
                if tagged is not None:
                    notes.append(
                        ImportNote(
                            entry_key,
                            "comment",
                            "state_only",
                            f"用途是「{_PURPOSE_LABELS[tagged]}」，仅在开启角色状态时使用，不进主提示词",
                        ),
                    )
                else:
                    notes.append(
                        ImportNote(
                            entry_key,
                            "content",
                            NOTE_NOT_ANALYZED,
                            "这份文件里的模板太多，导入时没有逐条分析这一条；运行时命中后照常分析，"
                            "不支持的写法、疑似防盗照样不注入",
                        ),
                    )
                continue
            spent += cost
        analysis = analyze_entry(comment, content)
        spent += _partial_cost(content, analysis.partial_renders)
        if analysis.purpose in STATE_ONLY_PURPOSES:
            notes.append(
                ImportNote(
                    entry_key,
                    "comment",
                    "state_only",
                    f"用途是「{_PURPOSE_LABELS[analysis.purpose]}」，仅在开启角色状态时使用，不进主提示词",
                ),
            )
        elif analysis.purpose is EntryPurpose.UNSUPPORTED_TEMPLATE:
            notes.append(
                ImportNote(
                    entry_key,
                    "content",
                    "unsupported_template",
                    f"含不支持的模板（{analysis.unsupported_reason}），这一条不会注入",
                ),
            )
        if analysis.suspected_trap:
            notes.append(ImportNote(entry_key, "content", "suspected_trap", f"疑似防盗条目：{analysis.trap_reason}"))
    return notes


_PURPOSE_LABELS = {
    EntryPurpose.MVU_UPDATE: "变量更新规则",
    EntryPurpose.INITVAR: "变量初值",
    EntryPurpose.STATE_RENDER: "状态渲染",
}


__all__ = [
    "LIST_ANALYSIS_CHARS",
    "MAX_CACHE_BYTES",
    "MAX_CACHE_ENTRIES",
    "MAX_SUMMARY_ENTRIES",
    "MIN_ANALYSIS_CHARGE",
    "REASON_STATE_ONLY",
    "REASON_TEMPLATE_BUDGET",
    "REASON_TEMPLATE_EMPTY",
    "REASON_TEMPLATE_ERROR",
    "REASON_TEMPLATE_TRAP",
    "REASON_TEMPLATE_UNSUPPORTED",
    "STATE_ONLY_PURPOSES",
    "STATIC_RENDER_CACHE",
    "TEMPLATE_ROUND_ANALYSES",
    "TEMPLATE_ROUND_CHARS",
    "EntryAnalysis",
    "EntryPurpose",
    "EntrySummary",
    "Rendered",
    "RoundBudget",
    "analysis_for",
    "analyze_entry",
    "cache_stats",
    "cached_analysis",
    "cached_summary",
    "clear_cache",
    "ensure_import_slot_free",
    "ensure_inspect_slot_free",
    "known_route",
    "listing_summaries",
    "needs_analysis",
    "prepare_content",
    "quick_route",
    "run_import_analysis",
    "run_inspect_analysis",
    "single_summary",
    "static_render_ok",
    "tenant_slot_key",
    "render_content",
    "summaries_for_listing",
    "summary_for",
    "template_import_notes",
    "title_purpose",
]
