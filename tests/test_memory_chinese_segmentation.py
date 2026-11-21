"""中文分词从"无词典启发式"换成 jieba 词典分词的回归测试。

四组性质：

1. **词典档的切分结果**（表驱动）。`test_memory_recall_calibration.py` 里那张表钉的是**无词典
   档**的下限，这里钉的是装了 jieba 之后真正生效的那一档。

2. **换档带来的召回差异**。用一组固定的中文查询 + 固定的实体表，按 `retriever` 真实的匹配
   语义（`Q(name__icontains=term)`，即"查询词是实体名的子串"）各算一遍召回率。这组数字就是
   "值不值得上 jieba" 的依据，锁在测试里免得哪天悄悄退回去。

3. **缺包 / 词典未就绪时优雅回落，且绝不在事件循环里做同步加载**。这条是可用性契约：
   少一个可选依赖只该让召回变差，不该让启动失败或检索抛异常；而约 0.85s 的同步词典加载压在
   事件循环上，等于单进程里的几十个实例一起停摆。

4. **接线**（§2.7）：切分档真的被检索链路用上了、预热真的被调度器排上了。零件对了而调用点
   没接上时，前三组会全绿。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import sys
import threading
import unicodedata
from collections.abc import Iterator
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType, MemorySource
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import CognitiveType
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory import scheduler as scheduler_module
from nekro_agent.services.memory import text_terms
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.retriever import MemoryRetriever
from nekro_agent.services.memory.text_terms import (
    _CJK_STOPWORDS,
    extract_query_terms,
    heuristic_segmenter,
    resolve_segmenter,
    tokenizer_backend,
    warm_up_tokenizer,
)

WORKSPACE_ID = 4301
MEMORY_MODELS = [
    "nekro_agent.models.db_mem_paragraph",
    "nekro_agent.models.db_mem_entity",
    "nekro_agent.models.db_mem_relation",
    "nekro_agent.models.db_mem_episode",
    "nekro_agent.models.db_mem_reinforcement_log",
]


# ======================================================================================
# 固件
# ======================================================================================


def require_jieba() -> None:
    """依赖词典档的用例入口守卫。

    `tests/conftest.py` 已经在会话开头预热过一次（消除"哪一档生效"的时序依赖），这里只负责
    在 jieba 确实不可用的环境上把用例标成 skip 而不是红。
    """
    warm_up_tokenizer()
    if tokenizer_backend() != "jieba":
        pytest.skip("jieba 不可用，词典档用例跳过")


@pytest.fixture
def tokenizer_state() -> Iterator[None]:
    """隔离 `text_terms` 的模块级分档状态。

    这些测试要造出"没装 jieba""词典还没加载完""加载失败"三种进程状态，而它们都写在模块
    全局上；不还原的话下一条用例（乃至下一个文件）会继承一个被人为弄坏的分词器。
    """
    saved = (
        text_terms._jieba_module,  # noqa: SLF001
        text_terms._jieba_import_failed,  # noqa: SLF001
        text_terms._jieba_warm_up_failed,  # noqa: SLF001
        text_terms._background_warm_up,  # noqa: SLF001
    )
    try:
        yield
    finally:
        (
            text_terms._jieba_module,  # noqa: SLF001
            text_terms._jieba_import_failed,  # noqa: SLF001
            text_terms._jieba_warm_up_failed,  # noqa: SLF001
            text_terms._background_warm_up,  # noqa: SLF001
        ) = saved


@pytest.fixture
async def memory_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": MEMORY_MODELS})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


class _FakeJieba:
    """行为可控的 jieba 替身。

    只实现 `text_terms` 真正用到的三处：`dt.initialized`、`initialize()`、`lcut()`。
    `initialize()` 记录**是哪个线程**调用的——"没在事件循环里同步加载"这条契约的判据就是它，
    断言"没卡住"只能靠计时，而计时是不确定的。
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.dt = type("_Dt", (), {"initialized": False})()
        self.fail = fail
        self.initialize_calls = 0
        self.initialize_threads: list[int] = []
        self.ready = threading.Event()

    def setLogLevel(self, _level: int) -> None:  # noqa: N802 - 镜像 jieba 的驼峰命名
        return

    def initialize(self) -> None:
        self.initialize_calls += 1
        self.initialize_threads.append(threading.get_ident())
        try:
            if self.fail:
                raise RuntimeError("词典文件损坏")
            self.dt.initialized = True
        finally:
            self.ready.set()

    def lcut(self, run: str) -> list[str]:
        return [run[index : index + 2] for index in range(0, len(run), 2)]


def _install_fake(fake: _FakeJieba | None, *, import_failed: bool = False) -> None:
    text_terms._jieba_module = fake  # type: ignore[assignment]  # noqa: SLF001
    text_terms._jieba_import_failed = import_failed  # noqa: SLF001
    text_terms._jieba_warm_up_failed = False  # noqa: SLF001
    text_terms._background_warm_up = None  # noqa: SLF001


# ======================================================================================
# 1. 词典档的切分结果
# ======================================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 无词典档整块留下的长串，词典档能切开（#24 原文列的两个例子）
        ("之前是怎么处理上下文更新的", ["处理", "上下文", "更新"]),
        ("图片卡片渲染改成异步", ["图片", "卡片", "渲染", "改成", "异步"]),
        # 无词典档会切歪成"直失败"的那句
        ("沙盒容器为什么一直失败", ["沙盒", "容器", "失败"]),
        # 标识符与路径仍然整体保留：换的只是 CJK 那一段的切法
        (
            "看看 services/memory/retriever.py 里的关系召回分数校准",
            ["看看", "services/memory/retriever.py", "关系", "召回", "分数", "校准"],
        ),
        ("napcat 扫码登录失败, qdrant 超时怎么排查", ["napcat", "扫码", "登录", "失败", "qdrant", "超时", "排查"]),
        # 英文虚词表照旧生效
        ("Fix the OAuth callback", ["fix", "oauth", "callback"]),
        # 中文虚词词表：疑问词/指代词不占 OR 分支的名额
        ("这个问题是什么时候出现的", ["问题", "出现"]),
        # 通篇虚词切不出内容词
        ("是不是这样呢", []),
        ("", []),
    ],
)
def test_dictionary_segmentation_table(text: str, expected: list[str]) -> None:
    require_jieba()
    assert extract_query_terms(text) == expected


def test_dictionary_words_longer_than_the_cap_are_dropped() -> None:
    """词典里 1195 条 >8 字的纯中文词条（多是数量短语）不许进词表。

    这条上限**不是**启发式档的遗留物：`heuristic_segmenter` 自己就已经按上限过滤过，所以
    真正会触发它的只有词典档——jieba 认得 `一万一千五百二十颗` 这种词并原样吐出来，而把它
    登记成实体名正是 M11 要消灭的噪声形态（`find_or_create` 永远去重不到第二条）。
    """
    require_jieba()
    assert extract_query_terms("库存一万一千五百二十颗，帮忙核对下数量", limit=32) == [
        "库存",
        "帮忙",
        "核对",
        "数量",
    ]


_LONG_TASK_TEXT = (
    "帮我把记忆检索的关系召回分数校准一下\n"
    + "已经把关系召回的硬编码基线换成字面覆盖率，并把覆盖率折算到向量相似度的量纲上；"
    "实体抽取改成先按虚词切分再取词，中文任务描述不会再被整句登记成实体。"
    "工作区网络隔离按归属分组，一个分组一张网络，跨组容器互相不可达。" * 45
)[:4300]


@pytest.mark.parametrize("segmenter", [heuristic_segmenter, None], ids=["heuristic", "default"])
@pytest.mark.parametrize(("extract", "limit"), [(text_terms.extract_entity_terms, 3), (text_terms.extract_query_terms, 8)])
def test_early_stop_is_equivalent_to_truncating_afterwards(
    segmenter: Any,
    extract: Any,
    limit: int,
) -> None:
    """"够了就不往下切"必须与"切完再截断"逐字相同。

    这条不是性能测试，是**等价性**测试：提前收手是为了不在事件循环上白切一篇 4300 字的正文
    （词典档实测 10ms → 0.16ms），但它一旦改变了结果，写入端登记的实体名就变了。
    """
    resolved = segmenter or resolve_segmenter()
    assert extract(_LONG_TASK_TEXT, segmenter=segmenter) == text_terms._iter_terms(  # noqa: SLF001
        _LONG_TASK_TEXT,
        resolved,
    )[:limit]


@pytest.mark.parametrize("func_name", ["extract_query_terms", "extract_entity_terms"])
def test_public_entries_pass_limit_down_into_the_splitter(func_name: str) -> None:
    """两个入口必须把 `limit` **传给** `_iter_terms`，而不是切完再截断。

    这条只能按调用点判：提前收手的整个契约就是"结果逐字不变"，所以把 `limit` 拿掉、改回
    `_iter_terms(...)[:limit]` 之后**没有任何行为断言会红**（变异实测存活），而代价是每次
    CC 写入都在事件循环上白切一篇 4300 字的正文（词典档实测 10ms）。
    """
    tree = ast.parse(inspect.getsource(text_terms))
    func = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == func_name
    )
    calls = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_iter_terms"
    ]
    assert len(calls) == 1, f"{func_name} 应当只有一处 _iter_terms 调用"
    call = calls[0]
    arg_names = [ast.unparse(arg) for arg in call.args] + [kw.arg for kw in call.keywords]
    assert "limit" in arg_names, f"{func_name} 没有把 limit 传下去，提前收手会失效"
    # 反面：传下去之后就不该再在外面截断一次（否则说明 limit 只是被忽略的摆设）
    assert "[:limit]" not in ast.unparse(func)


# `services/kb/search_service.py:_tokenize_query` 里那一份的逐条等价物。两处必须一致：
# `text_terms` 的模块注释就是这么写的，而"第二处实现悄悄漂移"是本项目反复栽的形态。
_EXPECTED_CJK_RANGES = (
    (0x4E00, 0x9FFF),  # CJK 统一汉字
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0xF900, 0xFAFF),  # CJK 兼容汉字
    (0x3040, 0x309F),  # 平假名
    (0x30A0, 0x30FF),  # 片假名
    (0xAC00, 0xD7AF),  # 谚文音节
)


def test_cjk_char_class_matches_the_kb_tokenizer() -> None:
    """CJK 字符类必须逐个码位等于知识库那份，多一个码位都不行。

    这条不是洁癖。`_CJK_CLASS` 曾经写成**字面端点**，而兼容汉字区首字 `\\uf900` 与统一汉字
    `\\u8c48` 字形完全一样（都是"豈"）：任何一次 NFC/NFKC 规范化——编辑器保存、剪贴板往返都算
    ——都会把前者换成后者，于是 `\\uf900-\\ufaff` 变成 `\\u8c48-\\ufaff`，整个私用区、代理区与
    Yi/Vai/爪哇/Latin Ext-D 共 11600 个码位一并被当成 CJK。**`git diff` 上那一行看起来分毫未变**，
    上一轮就是这么混进来的。
    """
    for code in range(0x20, 0x10000):
        expected = any(low <= code <= high for low, high in _EXPECTED_CJK_RANGES)
        actual = bool(text_terms._CJK_PATTERN.match(chr(code)))  # noqa: SLF001
        assert actual is expected, f"U+{code:04X} 的 CJK 归类漂了: 期望 {expected}，实际 {actual}"


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("私用区起点", 0xE000),
        ("私用区终点", 0xF8FF),
        ("彝文音节", 0xA000),
        ("Latin Ext-D", 0xA730),
        ("谚文音节区之后", 0xD7B0),
    ],
)
def test_non_cjk_blocks_never_become_terms(label: str, code: int) -> None:
    """非 CJK 码位只能当分隔符，绝不能被粘进词项里。

    上面那条盯的是字符类本身，这条盯**产品出口**：字符类一旦放宽，这些码位会先跟旁边的汉字
    聚成同一个 token，再被切成一个含私用区字符的"词"，最后以实体名的身份写进图谱、以
    `icontains` 的参数身份进检索——QQ 客户端的自定义表情正是整片落在私用区里的。

    **判据钉在启发式档上，不是随手选的**：词典档遇到不认识的码位会逐字切开，长度 1 的碎片
    被 `MIN_TERM_LENGTH` 顺手挡住，于是同一个变异在词典档下**照样全绿**（实测），这条断言
    在那一档信息量为零。启发式档才是能观察到差异的那一档，而它同时也是真实运行状态——
    进程启动后词典加载完之前、以及只读 tmp 装不上词典的部署，跑的都是它。
    """
    char = chr(code)
    terms = extract_query_terms(f"{char * 3}记忆检索的排序", segmenter=heuristic_segmenter)
    assert terms, "正向对照：同一句里的中文词必须照常切得出来"
    assert all(char not in term for term in terms), f"{label} 混进了词项: {terms}"


def test_no_source_file_contains_nfc_unstable_characters() -> None:
    """全仓 `.py` 里不许出现「保存一次就会变成别的字」的字符。

    上面两条只盯 `text_terms` 这一处，而这个陷阱是**全仓通用**的：CJK 兼容汉字
    （`U+F900`–`U+FAFF`）、带圈/全角兼容字形等在 NFC 下都会折叠成别的码位，一旦被当成
    正则端点或比较边界用，判据就会在某次无关的保存里悄悄变宽/变窄，而 `git diff` 上看不出来。
    这条就是那次事故（§2.18）的**另一半**：修一处不如把整类挡住。

    只扫 `.py`（0.14s）。前端与文档里出现兼容字形是排版选择、不构成判据，不在这条的射程内。
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    scanned = 0
    for package in ("nekro_agent", "plugins", "tests"):
        for path in (root / package).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            text = path.read_bytes().decode("utf-8")
            if unicodedata.normalize("NFC", text) != text:
                unstable = sorted({f"U+{ord(ch):04X}" for ch in text if unicodedata.normalize("NFC", ch) != ch})
                offenders.append(f"{path.relative_to(root)}: {unstable}")
    # 覆盖面自检（§4：先审扫描器自己）——扫不到文件时这条会变成一句空断言
    assert scanned > 400, f"扫描面塌了，只看到 {scanned} 个 .py"
    assert not offenders, "这些源文件里的字符 NFC 之后会变成别的码位:\n" + "\n".join(offenders)


def test_cjk_stopwords_never_reach_the_term_list() -> None:
    """虚词词表里的词一个都不许进词表——`retriever` 只取前 5 个词，一格都不能浪费。"""
    require_jieba()
    sentence = "为什么这个东西之前一直没有办法可以用，现在又可以了"
    terms = extract_query_terms(sentence, limit=32)
    assert not (set(terms) & _CJK_STOPWORDS)
    # 正向对照：同一句里的内容词照常留下，说明上面那条不是因为整句被切没了才成立
    assert "办法" in terms


# ======================================================================================
# 2. 换档带来的召回差异（#24 的量化依据）
# ======================================================================================

# 实体名：`consolidator` 里 LLM 抽出来的那类 2~6 字概念/人名
_ENTITIES = [
    "上下文", "上下文更新", "图片卡片", "卡片渲染", "异步渲染", "沙盒", "沙盒容器",
    "防火墙规则", "好感度", "好感度榜单", "记忆图谱", "向量检索", "语义检索", "分词",
    "中文分词", "工作区", "工作区网络", "网络隔离", "租户", "租户隔离", "模型组",
    "视觉能力", "凭据", "凭据脱敏", "日志", "启动日志", "迁移", "数据库迁移", "实例",
    "实例标识", "多实例", "插件加载", "小明", "阿绫", "王老师", "连接池", "配额",
    "并发配额", "超时", "重试", "事件循环",
]

# 查询 → 这条查询里**字面上就含有**的实体名。这是字面召回的天花板：实体名本身就是查询串的
# 子串，一个完美的分词器必然把它切得出来。判据是客观的（子串关系），不靠人工标注偏好。
_QUERY_CASES = [
    "之前是怎么处理上下文更新的",
    "图片卡片渲染改成异步了吗",
    "沙盒容器为什么一直失败",
    "防火墙规则被谁改掉了",
    "好感度榜单渲染卡住事件循环的那次",
    "记忆图谱的向量检索准不准",
    "中文分词现在用的什么方案",
    "工作区网络隔离是怎么做的",
    "租户隔离有没有漏掉的地方",
    "模型组的视觉能力怎么判断",
    "凭据脱敏做在哪一层",
    "启动日志里会不会泄露凭据",
    "数据库迁移文件能不能手写",
    "多实例的实例标识怎么生成",
    "插件加载失败要怎么排查",
    "小明和阿绫上次聊了什么",
    "连接池复用是在哪里做的",
    "并发配额耗尽之后会怎样",
    "王老师说的那个超时重试策略",
    "语义检索和字面命中怎么排序",
]


def _literal_recall(segmenter: Any) -> tuple[int, int]:
    """按 `_retrieve_relation_memories` 的真实语义算召回：命中数 / 天花板。"""
    recalled = 0
    ceiling = 0
    for query in _QUERY_CASES:
        ideal = {name for name in _ENTITIES if name in query}
        ceiling += len(ideal)
        terms = extract_query_terms(query, segmenter=segmenter)[:5]
        hit = {name for name in _ENTITIES if any(term in name for term in terms)}
        recalled += len(hit & ideal)
    return recalled, ceiling


def _zero_recall_queries(segmenter: Any) -> list[str]:
    """一个应召回的实体都没召回的查询。"""
    dead: list[str] = []
    for query in _QUERY_CASES:
        ideal = {name for name in _ENTITIES if name in query}
        if not ideal:
            continue
        terms = extract_query_terms(query, segmenter=segmenter)[:5]
        if not {name for name in _ENTITIES if any(term in name for term in terms)} & ideal:
            dead.append(query)
    return dead


def test_heuristic_backend_recall_is_the_measured_floor() -> None:
    """无词典档在这组查询上只能召回 8/41，20 条查询里 14 条一个实体都召不回。

    这两个数字就是 #24 "会漏词"的量化，也是"值得上词典"的依据。
    """
    assert _literal_recall(heuristic_segmenter) == (8, 41)
    assert len(_zero_recall_queries(heuristic_segmenter)) == 14


def test_dictionary_backend_recovers_the_missed_terms() -> None:
    """词典档在同一组查询上召回 41/41，没有一条查询是零召回。"""
    require_jieba()
    assert _literal_recall(resolve_segmenter()) == (41, 41)
    assert _zero_recall_queries(resolve_segmenter()) == []


def test_dictionary_backend_strictly_dominates_per_query() -> None:
    """逐条比较，词典档在**每一条**查询上都不比无词典档差。

    只看总数会掩盖"这里多召回三条、那里少召回两条"的交换；这条要求的是单调改进。
    """
    require_jieba()
    dictionary = resolve_segmenter()
    for query in _QUERY_CASES:
        ideal = {name for name in _ENTITIES if name in query}
        scores = []
        for segmenter in (heuristic_segmenter, dictionary):
            terms = extract_query_terms(query, segmenter=segmenter)[:5]
            hit = {name for name in _ENTITIES if any(term in name for term in terms)}
            scores.append(len(hit & ideal))
        assert scores[1] >= scores[0], f"词典档在「{query}」上反而更差: {scores}"


# ======================================================================================
# 3. 回落：缺包 / 未就绪 / 加载失败
# ======================================================================================


def test_missing_package_falls_back_instead_of_raising(tokenizer_state: None) -> None:
    """缺包只降级，不抛异常——这是"缺一个可选依赖不该让启动失败"的判据。"""
    _install_fake(None, import_failed=True)
    assert resolve_segmenter() is heuristic_segmenter
    assert tokenizer_backend() == "heuristic"
    assert extract_query_terms("记忆检索的排序，是怎么算的？") == ["记忆检索", "排序"]


def test_import_error_is_swallowed_by_the_import_helper(
    tokenizer_state: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真的让 `import jieba` 失败一次，走的是 `_import_jieba` 里那个 try。

    上一条用例是直接把"已经导入失败"这个状态摆进去的，于是 `except` 分支一行都没跑到——
    把它改成 `raise` 时那条用例照样绿（变异实测存活）。这里用 `sys.modules['jieba'] = None`
    让导入语句本身抛 `ImportError`，测试环境里 jieba 是装着的，不这么造就没有反面输入。
    """
    monkeypatch.setitem(sys.modules, "jieba", None)
    _install_fake(None)

    assert text_terms._import_jieba() is None  # noqa: SLF001
    assert text_terms._jieba_import_failed is True  # noqa: SLF001
    assert resolve_segmenter() is heuristic_segmenter
    # 检索链路不该因此抛异常，只是切得糙一点
    assert extract_query_terms("记忆检索的排序，是怎么算的？") == ["记忆检索", "排序"]


def test_dictionary_load_failure_is_not_retried_forever(tokenizer_state: None) -> None:
    """词典加载失败后记住这件事，不要每次切词都重试一遍。"""
    fake = _FakeJieba(fail=True)
    _install_fake(fake)
    assert resolve_segmenter() is heuristic_segmenter
    assert fake.initialize_calls == 1
    for _ in range(3):
        assert resolve_segmenter() is heuristic_segmenter
    assert fake.initialize_calls == 1


def test_sync_context_loads_the_dictionary_inline(tokenizer_state: None) -> None:
    """不在事件循环里时允许同步加载：脚本/测试/工作线程等一次 0.85s 是可以接受的。"""
    fake = _FakeJieba()
    _install_fake(fake)
    segmenter = resolve_segmenter()
    assert segmenter is not heuristic_segmenter
    assert fake.initialize_threads == [threading.get_ident()]


async def test_event_loop_never_blocks_on_dictionary_load(tokenizer_state: None) -> None:
    """事件循环里发现词典没就绪时：本次回落到启发式，加载挪到另一个线程去。

    判据是**加载发生在哪个线程**，不是"耗时多少"——后者只能靠计时，而计时不确定。
    """
    fake = _FakeJieba()
    _install_fake(fake)

    assert resolve_segmenter() is heuristic_segmenter
    # 判据只看"这个线程有没有去加载"，不看耗时——后台线程随时可能已经跑完，比时间会不稳。
    assert threading.get_ident() not in fake.initialize_threads

    thread = text_terms._background_warm_up  # noqa: SLF001
    assert thread is not None, "事件循环里必须把加载排到后台线程，否则永远升不上词典档"
    thread.join(timeout=10)
    assert fake.ready.is_set()
    assert fake.initialize_threads and fake.initialize_threads[0] != threading.get_ident()

    # 加载完成之后同一个进程自动升到词典档，不需要重启
    assert resolve_segmenter() is not heuristic_segmenter


async def test_background_warm_up_is_started_once(tokenizer_state: None) -> None:
    """连续切词不该每次都起一个线程。"""
    fake = _FakeJieba()
    _install_fake(fake)
    for _ in range(5):
        resolve_segmenter()
    thread = text_terms._background_warm_up  # noqa: SLF001
    assert thread is not None
    thread.join(timeout=10)
    assert fake.initialize_calls == 1


def test_warm_up_is_idempotent(tokenizer_state: None) -> None:
    fake = _FakeJieba()
    _install_fake(fake)
    assert warm_up_tokenizer() is True
    assert warm_up_tokenizer() is True
    assert fake.initialize_calls == 1


# ======================================================================================
# 4. 接线：切分档真的被用上了 / 预热真的被排上了
# ======================================================================================


async def _seed_relation() -> None:
    subject, _ = await DBMemEntity.find_or_create(
        workspace_id=WORKSPACE_ID,
        entity_type=EntityType.CONCEPT,
        name="上下文更新",
        source=MemorySource.CC,
    )
    obj, _ = await DBMemEntity.find_or_create(
        workspace_id=WORKSPACE_ID,
        entity_type=EntityType.CONCEPT,
        name="异步渲染",
        source=MemorySource.CC,
    )
    await DBMemRelation.create(
        workspace_id=WORKSPACE_ID,
        instance_key="",
        subject_entity_id=subject.id,
        predicate="改成",
        object_entity_id=obj.id,
        paragraph_id=None,
        memory_source="cc",
        cognitive_type=CognitiveType.SEMANTIC.value,
        half_life_seconds=30 * 24 * 3600,
    )


async def _retrieve_relation_summaries(monkeypatch: pytest.MonkeyPatch, query: str) -> list[str]:
    async def fake_embed(_text: str, **_kwargs: object) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]

    class _EmptyQdrant:
        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.5)
    monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
    monkeypatch.setattr(retriever_module, "memory_qdrant_manager", _EmptyQdrant())
    result = await MemoryRetriever(WORKSPACE_ID, scope=MemoryScope.unrestricted()).retrieve(query, limit=10, record_access=False)
    return [mem.summary for mem in result.memories if mem.source_type == "relation"]


async def test_retriever_really_uses_the_active_backend(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_state: None,
) -> None:
    """走真实检索链路：同一条中文查询，词典档召得回、无词典档召不回。

    这条是"接线"的证据（§2.7）——前面那些召回率都是直接调切词函数算出来的，即便
    `retriever` 改回自己 split 也照样全绿。反面对照（把 jieba 摘掉后变成 0 条）同时证明这条
    用例**测得出差异**，不是"这个查询本来就能命中"（§2.11）。
    """
    require_jieba()
    await _seed_relation()
    query = "之前是怎么处理上下文更新的"

    assert await _retrieve_relation_summaries(monkeypatch, query) == ["上下文更新 - 改成 - 异步渲染"]

    _install_fake(None, import_failed=True)
    assert await _retrieve_relation_summaries(monkeypatch, query) == []


async def _seed_episode() -> None:
    await DBMemEpisode.create(
        workspace_id=WORKSPACE_ID,
        instance_key="",
        origin_chat_key=None,
        title="上下文更新方案",
        narrative_summary="把上下文更新改成异步渲染，顺带修了卡片刷新",
    )


async def _retrieve_episode_summaries(monkeypatch: pytest.MonkeyPatch, query: str) -> list[str]:
    async def fake_embed(_text: str, **_kwargs: object) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]

    class _EmptyQdrant:
        async def search(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.5)
    monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
    monkeypatch.setattr(retriever_module, "memory_qdrant_manager", _EmptyQdrant())
    result = await MemoryRetriever(WORKSPACE_ID, scope=MemoryScope.unrestricted()).retrieve(query, limit=10, record_access=False)
    return [mem.summary for mem in result.memories if mem.source_type == "episode"]


async def test_episode_retrieval_really_uses_the_active_backend(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
    tokenizer_state: None,
) -> None:
    """**Episode 通道是 `extract_query_terms` 的第二个调用点**，要单独盯。

    对抗式复核实测：把 `_retrieve_episode_memories` 里那一句改回 `query.split()`（中文查询没有
    空格 → 整句当一个词 → `title__icontains=整句` 恒不命中），记忆相关的 **120 条测试全绿** ——
    因为上一条只断言了 `source_type == "relation"`。这正是 §2.7 那张表的形态：零件对、第二个
    调用点没人看。
    """
    require_jieba()
    await _seed_episode()
    query = "之前是怎么处理上下文更新的"

    assert await _retrieve_episode_summaries(monkeypatch, query) == ["上下文更新方案"]

    # 反面对照：换成无词典档就召不回（"处理上下文更新"整块不是标题的子串），
    # 说明这条用例测得出档位差异，不是"这个查询本来就能命中"（§2.11）。
    _install_fake(None, import_failed=True)
    assert await _retrieve_episode_summaries(monkeypatch, query) == []


async def test_scheduler_start_schedules_the_warm_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """调度器启动时必须把词典预热排出去。

    只测 `warm_up_tokenizer` 本身是不够的：调用点被删掉时它照样绿，而后果是每个部署的第一批
    检索都跑在启发式档上，直到某次切词碰巧触发兜底线程。
    """
    calls: list[str] = []

    async def fake_warm_up() -> bool:
        calls.append("warm")
        return True

    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_PRUNE_ENABLED", False)
    monkeypatch.setattr(config, "MEMORY_STARTUP_RECOVERY_ENABLED", False)
    monkeypatch.setattr(scheduler_module, "warm_up_tokenizer_async", fake_warm_up)

    scheduler = scheduler_module.MemoryMaintenanceScheduler()
    await scheduler.start()
    try:
        assert scheduler._tokenizer_warmup_task is not None  # noqa: SLF001
        await asyncio.wait_for(scheduler._tokenizer_warmup_task, timeout=5)  # noqa: SLF001
        assert calls == ["warm"]
    finally:
        await scheduler.stop()


async def test_warm_up_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch, tokenizer_state: None) -> None:
    """`warm_up_tokenizer_async` 必须把同步加载放到别的线程里。"""
    fake = _FakeJieba()
    _install_fake(fake)
    assert await text_terms.warm_up_tokenizer_async() is True
    assert fake.initialize_calls == 1
    assert fake.initialize_threads[0] != threading.get_ident()
