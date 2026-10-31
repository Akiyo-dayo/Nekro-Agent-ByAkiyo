"""记忆系统的词项切分。

记忆图谱的**写入端**（把任务文本变成实体节点）与**检索端**（把查询切成字面召回词）
必须用同一套切分规则：写进去的是整句、查出来的是短词，两边永远对不上，图谱就等于没建。
所以这里只有一份切分实现，两端各取一个入口。

字符类划分沿用知识库检索里已经在用的做法（`services/kb/search_service.py:_tokenize_query`）：
拉丁/数字/标识符是一类，CJK 是一类，其余字符只当分隔符。知识库到此为止就够用——它拿词去
`in` 一段长正文；而记忆图谱要拿词**本身**当实体名和 `icontains` 的参数，所以这里多一步：
把连续的 CJK 串切成词。

## 两档 CJK 切分

| 档 | 何时生效 | 行为 |
|---|---|---|
| `jieba_segmenter` | `jieba` 已安装**且词典已加载完** | 词典 + HMM 分词 |
| `heuristic_segmenter` | 缺包 / 词典还没加载完 / 加载失败 | 按虚词字符切开，切不动的整块丢弃 |

启发式档是**没有词典时的下限**，不是等价物：它靠一组虚词字符下刀，连续内容词之间没有虚词
就切不开（"处理上下文更新" 整块留下、"图片卡片渲染改成异步" 一个词都产不出），偶尔还会切歪
（"一直失败" → "直失败"）。差多少有具体数字，且**锁在测试里**而不是写在这段注释里：
见 `tests/test_memory_chinese_segmentation.py` 的
`test_heuristic_backend_recall_is_the_measured_floor` /
`test_dictionary_backend_recovers_the_missed_terms`（同一组固定中文查询上的字面召回
8/41 → 41/41，"一个实体都召不回"的查询 14 条 → 0 条）。

**缺包不是故障**：`jieba` 导入失败一律降级到启发式档并打一条 INFO，绝不让缺一个可选依赖
变成启动失败或检索异常。

## 词典加载放在哪

`jieba` 的 `import` 本身很便宜（约 70ms，此时**不**读词典），真正的开销在第一次切词触发的
`initialize()`（本机冷缓存约 0.85s 纯 CPU）。单进程要托管几十个实例，这 0.85s 压在事件循环上
就是所有实例一起停摆，所以：

- **主路径**：`MemoryMaintenanceScheduler.start()` 用 `asyncio.to_thread` 排一次预热，
  不 await，启动链路不为它多等；
- **兜底**：`resolve_segmenter()` 发现自己正跑在事件循环里而词典还没好，就起一个 daemon 线程
  预热并**让这一次调用回落到启发式**（记忆总开关在运行期被打开、或调用方压根没走 NA 启动
  链路时靠这条兜住）；
- 不在事件循环里（脚本、测试、`asyncio.to_thread` 派生的工作线程）时才允许同步阻塞加载。

`warm_up_tokenizer()` 幂等且自带锁，两条路径重复调用是安全的。
"""

from __future__ import annotations

import asyncio
import re
import threading
from types import ModuleType
from typing import Callable

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("memory.text_terms")

# 与 `services/kb/search_service.py` 保持一致的字符类：标识符（含 `.`/`_`/`-`，用于
# `retriever.py` 这类文件名与 `pg_trgm` 这类符号）与 CJK/假名/谚文。
#
# **必须写成 `\uXXXX` 转义，不要写字面汉字。** 这一行原本用的是字面端点，而
# `U+F900`（CJK 兼容汉字区首字）与 `U+8C48`（统一汉字）**字形完全一样，都是“豈”**：
# 任何一次 NFC/NFKC 规范化——编辑器保存、剪贴板往返、`unicodedata.normalize` 都算——
# 都会把前者悄悄换成后者，于是 `U+F900-U+FAFF` 变成 `U+8C48-U+FAFF`，
# 整个私用区（`U+E000-U+F8FF`）、代理区与 Yi/Vai/爪哇/Latin Ext-D 等 11600 个码位
# 一并被当成“CJK”。`git diff` 上这一行看起来**分毫未变**。
# 本轮的对抗式复核实测到了这次替换（见 `test_cjk_char_class_matches_the_kb_tokenizer`）。
_CJK_CLASS = (
    "\u4e00-\u9fff"  # CJK 统一汉字
    "\u3400-\u4dbf"  # CJK 扩展 A
    "\uf900-\ufaff"  # CJK 兼容汉字
    "\u3040-\u309f"  # 平假名
    "\u30a0-\u30ff"  # 片假名
    "\uac00-\ud7af"  # 谚文音节
)
_TOKEN_PATTERN = re.compile(rf"[a-z0-9_./-]+|[{_CJK_CLASS}]+")
_CJK_PATTERN = re.compile(rf"[{_CJK_CLASS}]")

MIN_TERM_LENGTH = 2
MAX_TERM_LENGTH = 32
# CJK 词项长度上限。中文内容词绝大多数是 2~4 字，复合词到 8 字；超过这个长度的连续串说明
# 切分没能把它拆开，此时宁可整块丢弃，也不要把半句话登记成实体——那正是 M11 的病灶。
MAX_CJK_TERM_LENGTH = 8

# 用于切开长 CJK 串的虚词字符（**只有启发式档用**）。取值原则：只收几乎不会出现在内容词内部
# 的助词、代词、介词与否定/程度副词。像 使/用/能/会/更/中/上/下/时/内/外/前/后/间/文/本/数/
# 为/以/到/有/要 这些高频字被**刻意排除**，因为它们大量出现在本项目的词汇里（使用、功能、
# 更新、上下文、时间、内存、数据、因为、可以、需要……），拿它们去切会把真词切碎。
_CJK_FUNCTION_CHARS = (
    "的了着过吗呢吧啊呀嘛哦"
    "把被给让请帮叫"
    "和与或及并而却就也都还再又"
    "我你您他她它们咱谁"
    "这那哪什么之其此怎啥咋"
    "是在于从向对跟由按依"
    "不没很太最只才已将且些个一"
)
_CJK_FUNCTION_CHAR_SET = frozenset(_CJK_FUNCTION_CHARS)

# CJK 虚词**词**表。过滤发生在 `_iter_terms`，也就是**两档都过**——上面那张字符表是
# 启发式档下刀用的，这张是切出来之后筛词用的，两者不是"各管一档"的关系。
# （启发式档也会产出 "这次"/"那次" 这类整块回落的结果，同样该筛掉。）
# 分词器给出的是词而不是字，单字虚词已经被 `MIN_TERM_LENGTH` 挡掉了，这里挡的是 2 字以上的
# 疑问词、指代词与纯功能副词。
#
# 为什么要挡：`retriever` 只取前 5 个词去拼 `icontains` 的 OR 分支，一个 "怎么" 占掉一格
# 就少一条真线索。判据是"这个词永远不会是实体名的一部分"，拿不准的（"问题""方案""原因"
# 这类）一律不收——漏挡只是浪费一格，误挡是把一条真线索删掉。
_CJK_STOPWORDS = frozenset(
    {
        # 疑问 / 指代
        "什么", "怎么", "怎样", "怎么样", "为什么", "为何", "如何", "多少", "哪些", "哪个",
        "哪里", "什么样", "这个", "那个", "这些", "那些", "这样", "那样", "这里", "那里",
        "我们", "你们", "他们", "她们", "它们", "咱们", "自己", "别人",
        # 数量 / 程度 / 时体
        "一个", "一下", "一些", "一点", "一直", "一样", "一起", "一次", "一定", "一切",
        "已经", "正在", "曾经", "马上", "立刻", "总是", "经常", "偶尔", "刚刚", "刚才",
        "非常", "特别", "十分", "比较", "稍微", "有点", "更加", "最为",
        # 连接 / 判断 / 情态
        "因为", "所以", "但是", "不过", "然后", "而且", "并且", "或者", "以及", "如果",
        "虽然", "由于", "于是", "因此", "还是", "就是", "只是", "也是", "都是", "不是",
        "没有", "有没有", "是不是", "能不能", "要不要", "可以", "可能", "应该", "需要",
        "不要", "不能", "不用", "不会", "不再", "其实", "确实", "真的", "到底", "究竟",
        # 时间指代（不指向具体事件，指向"哪一次"，对字面召回无贡献）
        "之前", "之后", "以前", "以后", "上次", "下次", "那次", "这次", "当时", "现在",
        "目前", "最近", "刚好", "正好", "时候",
    },
)

# 拉丁虚词。只收纯功能词，技术词汇一律不收。
_LATIN_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "not", "no", "of", "in", "on", "at", "to", "for",
        "with", "by", "from", "into", "as", "is", "am", "are", "was", "were", "be", "been",
        "do", "does", "did", "done", "have", "has", "had", "it", "its", "this", "that", "these",
        "those", "he", "she", "they", "them", "we", "us", "you", "your", "our", "my", "me",
        "can", "will", "would", "should", "could", "may", "might", "must", "shall",
        "if", "then", "than", "so", "such", "there", "here", "what", "which", "who", "how",
        "all", "any", "some", "each", "very", "just", "also", "about", "over", "under",
    },
)

CjkSegmenter = Callable[[str], list[str]]


# ---------------------------------------------------------------------------
# 启发式档（无词典）
# ---------------------------------------------------------------------------


def heuristic_segmenter(run: str) -> list[str]:
    """把一段连续 CJK 文本切成候选词项（无词典档）。

    先按虚词切；切出来一个合法块都没有时才考虑整块保留——"对话""这次"这类本身就是词、
    只是恰好含一个虚词字的短串靠这条回落保住，而"是怎么算的"这种虚词占了一半以上的串
    直接丢弃。切不动的长串同样丢弃：没有词典就没法证明该在哪里下刀，与其造一个假实体，
    不如少一个词。

    注意这里保住的只是"能不能切出这一块"；`_iter_terms` 之后还会用 `_CJK_STOPWORDS`
    再筛一道，"这次"这类纯时间指代最终仍不会进词表（两档同筛）。
    """
    chunks: list[str] = []
    current: list[str] = []
    for char in run:
        if char in _CJK_FUNCTION_CHAR_SET:
            chunks.append("".join(current))
            current = []
            continue
        current.append(char)
    chunks.append("".join(current))

    usable = [chunk for chunk in chunks if MIN_TERM_LENGTH <= len(chunk) <= MAX_CJK_TERM_LENGTH]
    if usable:
        return usable

    function_char_count = sum(1 for char in run if char in _CJK_FUNCTION_CHAR_SET)
    if MIN_TERM_LENGTH <= len(run) <= MAX_CJK_TERM_LENGTH and function_char_count * 2 <= len(run):
        return [run]
    return []


# ---------------------------------------------------------------------------
# jieba 档（有词典）
# ---------------------------------------------------------------------------

_jieba_module: ModuleType | None = None
_jieba_import_failed = False
_jieba_warm_up_failed = False
_warm_up_lock = threading.Lock()
_background_warm_up: threading.Thread | None = None


def _import_jieba() -> ModuleType | None:
    """按需导入 jieba。缺包/导入期异常一律降级，不向上抛。

    `import jieba` 不读词典（实测约 70ms），所以这一步在事件循环里做是安全的。
    """
    global _jieba_module, _jieba_import_failed  # noqa: PLW0603

    if _jieba_module is not None:
        return _jieba_module
    if _jieba_import_failed:
        return None
    try:
        import jieba  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001 - 缺包只是降级，不该让调用方感知为故障
        _jieba_import_failed = True
        logger.info(f"未启用词典分词（jieba 不可用，回落到启发式切分）: {e}")
        return None
    # jieba 自带一个往 stderr 打「Building prefix dict…」的 logger，屏蔽掉，
    # 词典状态由本模块用项目自己的 logger 汇报。
    try:
        jieba.setLogLevel(60)
    except Exception:  # noqa: BLE001,S110 - 只是日志噪声，失败不影响分词
        pass
    _jieba_module = jieba
    return jieba


def _dictionary_ready(module: ModuleType) -> bool:
    """词典是否已经加载完。判据取自 jieba 自己的状态，不另外维护一份。"""
    return bool(getattr(module.dt, "initialized", False))


def warm_up_tokenizer() -> bool:
    """同步加载 jieba 词典，返回加载后是否处于 jieba 档。

    **会阻塞约 0.85s（冷缓存）**，调用方必须保证自己不在事件循环里
    （`asyncio.to_thread(...)` 或 daemon 线程）。幂等，可重复调用。
    """
    global _jieba_warm_up_failed  # noqa: PLW0603

    module = _import_jieba()
    if module is None:
        return False
    if _dictionary_ready(module):
        return True
    with _warm_up_lock:
        if _dictionary_ready(module):
            return True
        if _jieba_warm_up_failed:
            return False
        try:
            module.initialize()
        except Exception as e:  # noqa: BLE001 - 词典损坏/磁盘只读也只是降级
            _jieba_warm_up_failed = True
            logger.warning(f"jieba 词典加载失败，记忆切词回落到启发式: {e}")
            return False
    logger.info("记忆切词已启用词典分词（jieba）")
    return True


async def warm_up_tokenizer_async() -> bool:
    """在线程里预热词典。启动链路用这个，不要直接 `await` 同步版。"""
    return await asyncio.to_thread(warm_up_tokenizer)


def _running_in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _start_background_warm_up() -> None:
    """在事件循环里发现词典没好时，起一个 daemon 线程去加载。

    只保证「最终会好」，不保证本次调用能用上——本次照旧走启发式档。

    **这里刻意不加锁**：能用的那把锁是 `_warm_up_lock`，而它在加载期间被持有约 0.85s，
    在事件循环里去抢它就正好变成这个函数要避免的那件事。没有锁的代价只是极端并发下可能
    多起一个线程，它进 `warm_up_tokenizer` 后会在锁上等一下然后发现已经加载好、直接返回。
    """
    global _background_warm_up  # noqa: PLW0603

    if _background_warm_up is not None and _background_warm_up.is_alive():
        return
    thread = threading.Thread(
        target=warm_up_tokenizer,
        name="memory-term-tokenizer-warmup",
        daemon=True,
    )
    _background_warm_up = thread
    thread.start()


def _jieba_segmenter(run: str) -> list[str]:
    """词典档：把一段连续 CJK 文本切成词。"""
    module = _jieba_module
    if module is None:  # pragma: no cover - resolve_segmenter 已经保证非空
        return heuristic_segmenter(run)
    return [piece for piece in module.lcut(run) if piece.strip()]


def resolve_segmenter() -> CjkSegmenter:
    """挑出本次调用可用的 CJK 切分档。"""
    module = _import_jieba()
    if module is None or _jieba_warm_up_failed:
        return heuristic_segmenter
    if _dictionary_ready(module):
        return _jieba_segmenter
    if _running_in_event_loop():
        # 事件循环里绝不做 0.85s 的同步词典加载：单进程托管几十个实例，那是全体停摆。
        _start_background_warm_up()
        return heuristic_segmenter
    return _jieba_segmenter if warm_up_tokenizer() else heuristic_segmenter


def tokenizer_backend() -> str:
    """当前生效的切分档，取值 `"jieba"` / `"heuristic"`。供日志与测试断言使用。"""
    return "jieba" if resolve_segmenter() is _jieba_segmenter else "heuristic"


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def _iter_terms(text: str, segmenter: CjkSegmenter, limit: int | None = None) -> list[str]:
    """按统一规则切出候选词项，保持出现顺序并去重。

    `limit` 不是切完再截断，而是**够了就不往下切**：写入端的输入是 CC 任务正文（上限
    `MEMORY_SEMANTIC_MAX_TASK_LENGTH + MEMORY_SEMANTIC_MAX_RESULT_LENGTH` = 4300 字），
    而它只要前 3 个词。整篇切一遍在词典档下实测约 10ms 的同步 CPU，全压在事件循环上；
    提前收手之后与需要的词数成正比。结果与"切完再截断"逐字相同——去重与顺序都只依赖前缀。
    """
    terms: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_PATTERN.findall(text.lower()):
        is_cjk = bool(_CJK_PATTERN.match(token))
        pieces = segmenter(token) if is_cjk else [token.strip("./-_")]
        for piece in pieces:
            if not MIN_TERM_LENGTH <= len(piece) <= MAX_TERM_LENGTH:
                continue
            if is_cjk and len(piece) > MAX_CJK_TERM_LENGTH:
                continue
            if piece in _LATIN_STOPWORDS or piece in _CJK_STOPWORDS or piece in seen:
                continue
            seen.add(piece)
            terms.append(piece)
            if limit is not None and len(terms) >= limit:
                return terms
    return terms


def extract_query_terms(query: str, limit: int = 8, *, segmenter: CjkSegmenter | None = None) -> list[str]:
    """切出用于字面召回的查询词。

    不把整条查询也塞进词表：中文查询没有空格，整句 `LIKE '%整句%'` 只会稳定地什么都命中
    不到，白占一个 `OR` 分支。

    `segmenter` 只给测试用来钉住某一档，产品调用点一律不传。
    """
    return _iter_terms(query, segmenter or resolve_segmenter(), limit)


def extract_entity_terms(text: str, limit: int = 3, *, segmenter: CjkSegmenter | None = None) -> list[str]:
    """切出可以登记为实体节点的词。

    与查询侧共用同一套切分：`icontains` 的方向是「查询词是实体名的子串」，两端来自同一个
    词表才对得上。产出上限比查询侧低，是因为噪声实体会顺着 `icontains` 一路带进召回结果，
    代价高于漏掉一个节点。
    """
    return _iter_terms(text, segmenter or resolve_segmenter(), limit)


def lexical_coverage(query: str, *texts: str, segmenter: CjkSegmenter | None = None) -> float:
    """候选文本对查询的**字面覆盖率**，取值 [0, 1]。

    这是字面命中唯一的强度来源，替代改造前 0.55/0.58 那种与向量相似度不可比的硬编码基线。
    按字符数加权而不是按词数：长词更具体，命中一个长词比命中一个两字词更能说明问题。
    整条查询被原样包含时直接记满分——那就是字面意义上的完全覆盖。
    """
    haystack = " ".join(text for text in texts if text).lower()
    normalized_query = query.strip().lower()
    if not haystack or not normalized_query:
        return 0.0
    if normalized_query in haystack:
        return 1.0

    terms = _iter_terms(normalized_query, segmenter or resolve_segmenter())
    if not terms:
        return 0.0
    total_weight = sum(len(term) for term in terms)
    matched_weight = sum(len(term) for term in terms if term in haystack)
    return matched_weight / total_weight
