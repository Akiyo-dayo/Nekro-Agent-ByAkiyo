"""知识库关键词通道的中文切词与阈值标定（§3 #45）。

改之前 `services/kb/search_service.py:_tokenize_query` 只按字符类切：`[a-z0-9_./-]+` 是一类，
**一整段连续 CJK 是另一类**，对中文一刀不下。于是「工作区网络隔离是怎么做的」变成一个 12 字的
token，而下游判据是 `token in 正文`，要求这 12 个字原样连续出现在文档里 —— 中文查询的关键词
通道因此基本恒为 0 命中，混合检索静默退化成纯向量。

（改前那么写对**英文/标识符**是成立的：空格已经把词分开了。中文没有空格，前提不成立。）

## 这个文件测什么

| 组 | 性质 | 为什么单独一组 |
|---|---|---|
| 1 | 表驱动切词：中文长句 / 中英混合 / 纯标识符 / 代码片段 / 含标点 / 超短 | 切分本身 |
| 2 | **对照组**：拉丁与标识符查询的 token 与改前**逐字相同** | "英文不许退化"要能被证伪 |
| 3 | **单调性**：改前能命中的文本，改后一定还命中 | 阈值抬高之后仍成立才敢抬 |
| 4 | **标定数据**：改前/改后的召回与误召（真实语料，锁数字） | 阈值是用它推出来的 |
| 5 | 阈值 0.26 由语料**推导**而非拍脑袋，且两档分词都安全 | 改数不重标就红 |
| 6 | CJK 字符类与 `text_terms` 逐码位一致 | §2.18 那次事故的第二处实现 |
| 7 | CJK token 上限是成本护栏而非质量旋钮 | 免得被当成调质量的旋钮乱调 |
| 8 | **接线**：`search_workspace_kb` 真的跑到了新切词（§2.7） | 零件全对、调用点没了会全绿 |
| 9 | **第二个出口 / 第二个消费者**：素材那一路、阈值的两个比较点、向量通道的两个 tokens 消费者 | 复核变异实测存活，见下 |
| 9d | 阈值必须**严格高于 0.25**（元数据贡献的结构性封顶） | 与语料无关的不变式，标定表看不见 |

第 2/3/4 组都需要一个**能真跑起来**的改前实现，所以 `_pre_fix_tokenize` 把它逐字冻结在下面。

## 第 9 组是怎么来的（对抗式复核的变异存活登记）

第 1~8 组建好之后，把改动的**每一处调用点/判据**逐条改坏再跑一遍，以下变异**全部存活**
（也就是这些行整段写错，175 条测试一条不红）：

| 变异 | 内容 | 现由谁杀 |
|---|---|---|
| X1 | `search_workspace_kb` 给**素材**那一路传 `tokens=[]` | `test_asset_exit_gets_the_same_tokens_...` |
| X2 | `_collect_keyword_asset_hits` 的阈值写死 0.22 | 同上（夹逼） |
| X3 | `_collect_keyword_hits` 的阈值写死 0.22（常量不动） | `test_document_chunk_gate_reads_the_calibrated_constant` |
| X4 | 向量通道的 `_apply_metadata_bonus` 拿不到 tokens | `test_vector_pass_consumes_the_same_tokens` |
| X5 | 去掉 token 去重 | `test_terms_are_deduplicated_across_separate_cjk_runs` |
| X6 | `_QUERY_TOKEN_PATTERN` 单独收窄（与 `_CJK_CHAR_PATTERN` 漂开） | `test_query_segmenter_char_class_matches_the_dispatch_class` |
| X7 | `cjk_budget` 不递减（预算变成**每段**一份） | `test_cjk_budget_is_spent_across_runs_not_per_run` |
| X8 | `score_threshold=0.48 if tokens else 0.42` 改成恒定 0.48（文档侧） | `test_vector_pass_consumes_the_same_tokens` |
| X11 | 素材那一路的 `_apply_metadata_bonus` 拿不到 tokens | `test_asset_vector_pass_consumes_the_same_tokens` |
| X12 | 素材那一路的 `0.48 / 0.42` 改成恒定 0.48 | 同上 |

`tokens` 一共有**四个**消费点（{关键词, 向量} × {文档, 素材}），X1/X3 是关键词那两个、
X4/X8/X11/X12 是向量那两个——补完文档侧之后素材侧仍然全绿，所以两侧各有一条用例。

补完之后 13 条有效变异 12 杀 1 存活（存活的那条是上面注明的等价变异）。另有两条阈值变异
（0.26 → 0.25 与 0.26 → 0.22）各 3 条用例红，其中 0.25 那条**只有** 9d 那条不变式盯得住
「刚好落在元数据封顶值上」这一格。

（另有一条 `.match` → `.search` 的变异同样存活，但那是**等价变异**：`_QUERY_TOKEN_PATTERN`
的两支互斥，拉丁 token 里不可能出现 CJK 字符，两者行为逐字相同，不补。）

## 语料从哪来

`_DOCS` / `_QUERY_CASES` 是照着本项目自己的运维/架构文档写的一组真语料（11 篇 × 3 段 = 33 段，
29 条查询），`gold` 是人工标注的应召段落。**误召只统计来自非 gold 文档的段落** —— 同一篇文档内
的相邻段落由 `max_chunks_per_document` 收口，把它算成噪声会系统性地高估。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, cast

import pytest
from tortoise import Tortoise

from nekro_agent.services.kb import search_service
from nekro_agent.services.kb.search_service import (
    _QUERY_TOKEN_PATTERN,
    KEYWORD_CHUNK_SCORE_THRESHOLD,
    KEYWORD_DOC_CANDIDATE_FACTOR,
    KEYWORD_DOCUMENT_FALLBACK_THRESHOLD,
    KEYWORD_MAX_CJK_TERMS,
    _keyword_chunk_score,
    _keyword_document_score,
    _tokenize_query,
)
from nekro_agent.services.memory import text_terms

# ======================================================================================
# 固件
# ======================================================================================


def require_jieba() -> None:
    """依赖词典档的用例入口守卫（`tests/conftest.py` 已在会话开头预热过一次）。"""
    text_terms.warm_up_tokenizer()
    if text_terms.tokenizer_backend() != "jieba":
        pytest.skip("jieba 不可用，词典档用例跳过")


@pytest.fixture
def heuristic_backend() -> Iterator[None]:
    """把整个进程按到无词典档上，跑完还原。

    无词典档不是假想状态：进程启动后词典加载完之前、以及容器 `--read-only` 且没有可写
    临时目录（`jieba.initialize()` 抛 `FileNotFoundError`）的部署，跑的都是它。
    阈值必须在**这一档也安全**，否则「装不上 jieba 只让召回变差」就成了「还会掉命中」。
    """
    saved = (
        text_terms._jieba_module,  # noqa: SLF001
        text_terms._jieba_import_failed,  # noqa: SLF001
        text_terms._jieba_warm_up_failed,  # noqa: SLF001
    )
    text_terms._jieba_module = None  # noqa: SLF001
    text_terms._jieba_import_failed = True  # noqa: SLF001
    text_terms._jieba_warm_up_failed = False  # noqa: SLF001
    try:
        assert text_terms.tokenizer_backend() == "heuristic"
        yield
    finally:
        (
            text_terms._jieba_module,  # noqa: SLF001
            text_terms._jieba_import_failed,  # noqa: SLF001
            text_terms._jieba_warm_up_failed,  # noqa: SLF001
        ) = saved


def _pre_fix_tokenize(query: str) -> list[str]:
    """#45 修复**之前**的 `_tokenize_query`，逐字冻结在这里。

    第 2/3/4 组要断言的是「与改前相比如何」，而这三条性质都需要一个**能真的跑起来**的改前
    实现——凭记忆描述改前长什么样，会在下一次改动时悄悄失真。字符类直接取产品那一份，
    这样这个替身只冻结「不切 CJK」这一件事，不会连字符类一起冻旧。
    """
    lowered = query.lower().strip()
    tokens = re.findall(rf"[a-z0-9_./-]+|[{search_service._CJK_CLASS}]+", lowered)  # noqa: SLF001
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        normalized = token.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


# ======================================================================================
# 1. 表驱动切词
# ======================================================================================

# (类别, 查询, 词典档期望 token)
_TOKENIZE_CASES: list[tuple[str, str, list[str]]] = [
    # --- 中文长句：#45 的正题，改前整句是一个 token ---
    ("中文长句", "工作区网络隔离是怎么做的", ["工作", "网络", "隔离"]),
    ("中文长句", "沙盒容器为什么一直超时没有任何反应", ["沙盒", "容器", "超时", "任何", "反应"]),
    ("中文长句", "数据库迁移文件可以手写吗", ["数据库", "迁移", "文件", "手写"]),
    ("中文长句", "租户越权的时候应该返回什么状态码", ["租户", "越权", "返回", "状态"]),
    # --- 中英混合：拉丁段与 CJK 段各走各的路 ---
    ("中英混合", "iptables 规则被谁改掉了", ["iptables", "规则", "改掉"]),
    ("中英混合", "docker 地址池不够用了怎么办", ["docker", "地址", "不够", "怎么办"]),
    # 后半段 `是在哪里用的` 切出来只剩虚词（`哪里` 在停用词表里、其余都是单字），
    # 于是整串按"切不出词就保持原样"回落——与改前逐字相同，不是漏切。
    ("中英混合", "asyncio.to_thread 是在哪里用的", ["asyncio.to_thread", "是在哪里用的"]),
    # --- 纯标识符：必须与改前逐字相同（长路径不许被 32 字上限吃掉）---
    ("纯标识符", "_tokenize_query", ["_tokenize_query"]),
    ("纯标识符", "KEYWORD_CHUNK_SCORE_THRESHOLD", ["keyword_chunk_score_threshold"]),
    ("纯标识符", "kb_qdrant_manager.search_grouped", ["kb_qdrant_manager.search_grouped"]),
    (
        "纯标识符",
        "nekro_agent/services/kb/search_service.py",
        ["nekro_agent/services/kb/search_service.py"],
    ),
    # --- 代码片段 ---
    ("代码片段", "poe db-revision", ["poe", "db-revision"]),
    ("代码片段", "uv run poe db-migrate --env=dev", ["uv", "run", "poe", "db-migrate", "--env", "dev"]),
    ("代码片段", "SELECT * FROM kb_chunk WHERE workspace_id = 1", ["select", "from", "kb_chunk", "where", "workspace_id", "1"]),
    # --- 含标点：标点只当分隔符，中英标点都是 ---
    ("含标点", "越权？返回 404 还是 403？", ["越权", "返回", "404", "还是", "403"]),
    ("含标点", "DROP 不回 RST，会怎样？", ["drop", "不回", "rst", "会怎样"]),
    # --- 超短查询 ---
    ("超短", "隔离", ["隔离"]),
    ("超短", "好感度", ["好感度"]),
    ("超短", "pg_trgm", ["pg_trgm"]),
    # --- 切不出词的专名：整串保留，不许一个 token 都不剩 ---
    ("专名回落", "甯浩", ["甯浩"]),
    ("专名回落", "昇腾", ["昇腾"]),
    ("专名回落", "这个是什么", ["这个是什么"]),
    # --- 空与纯标点 ---
    ("空输入", "", []),
    ("空输入", "？！。，", []),
]


@pytest.mark.parametrize(
    ("kind", "query", "expected"),
    _TOKENIZE_CASES,
    ids=[f"{kind}-{query or 'empty'}" for kind, query, _ in _TOKENIZE_CASES],
)
def test_tokenize_query_table(kind: str, query: str, expected: list[str]) -> None:
    """词典档下的逐条切分结果。

    中文那几行是这次修复的直接判据：把 `_tokenize_query` 改回「CJK 不切」之后，
    `["工作", "网络", "隔离"] == ["工作区网络隔离是怎么做的"]` 当场红。
    """
    require_jieba()
    assert _tokenize_query(query) == expected, f"{kind}: {query!r}"


def test_chinese_sentence_is_not_one_giant_token() -> None:
    """把 #45 的病灶写成一条独立断言：整句绝不许原样进 token 表。

    上面那张表已经覆盖了，但表里的期望值是「切成了什么」，读起来看不出**为什么**要切。
    这一条直接钉住那句判据：token 的长度必须远小于整句，且没有任何一个 token 等于整句。
    """
    require_jieba()
    query = "工作区网络隔离是怎么做的"
    tokens = _tokenize_query(query)
    assert query not in tokens, "整句又变回一个 token 了，关键词通道对中文会重新恒为 0 命中"
    assert len(tokens) >= 2
    assert max(len(token) for token in tokens) <= 4


def test_heuristic_backend_still_cuts_something(heuristic_backend: None) -> None:
    """缺 jieba 时也要切得动一点——回落档是下限，不是「退回改前」。

    「工作区网络隔离是怎么做的」在无词典档下靠虚词字符（是/怎/么/的）下刀，切出 7 字的
    `工作区网络隔离`——比改前那个 12 字整句短了一半，而文档标题里正好含 `工作区网络`。
    这一档切不出 `工作`/`网络`/`隔离` 这样的细词，所以召回只有词典档的一小半（见第 4 组），
    但它是**下限**而不是"退回改前"。
    """
    query = "工作区网络隔离是怎么做的"
    assert _tokenize_query(query) == ["工作区网络隔离"]
    assert _tokenize_query(query) != _pre_fix_tokenize(query), "无词典档也必须比改前切得细"
    assert _tokenize_query("nekro_agent/services/kb/search_service.py") == [
        "nekro_agent/services/kb/search_service.py"
    ]


# ======================================================================================
# 2. 对照组：拉丁 / 标识符逐字不变
# ======================================================================================

_LATIN_CONTROL_QUERIES = [
    "_tokenize_query",
    "KEYWORD_CHUNK_SCORE_THRESHOLD",
    "kb_qdrant_manager.search_grouped",
    "nekro_agent/services/kb/search_service.py",
    "hybrid retrieval keyword pass",
    "how to configure the sandbox network",
    "deduplicates the tokens",
    "poe db-revision",
    "default-address-pools",
    "GET /api/kb/search",
    "SELECT * FROM kb_chunk;",
    "pg_trgm",
    "v1.2.3-rc1",
    "--flag=value",
    "a b c 1 2 3",
]


@pytest.mark.parametrize("query", _LATIN_CONTROL_QUERIES)
def test_latin_queries_are_byte_identical_to_the_pre_fix_tokenizer(query: str) -> None:
    """英文/标识符查询的 token 必须与改前**逐字相同**——这是"不许退化"的判据。

    这条不是形式主义：最直白的改法是把整条查询丢给 `text_terms.extract_query_terms`，
    而它按记忆图谱的需要做了两件对知识库有害的事 ——
    ① `MAX_TERM_LENGTH = 32` 会把 41 字的
    `nekro_agent/services/kb/search_service.py` **整个丢掉**（路径查询当场从强命中变成 0 命中）；
    ② 拉丁虚词表会吃掉 `how`/`the`/`to`。所以拉丁那一半压根不进 `text_terms`。
    """
    require_jieba()
    assert _tokenize_query(query) == _pre_fix_tokenize(query)


def test_latin_control_group_is_backend_independent(heuristic_backend: None) -> None:
    """拉丁那一半连"哪一档分词"都不该看得出来——它根本没走分词器。"""
    for query in _LATIN_CONTROL_QUERIES:
        assert _tokenize_query(query) == _pre_fix_tokenize(query), query


def test_the_control_group_can_actually_detect_a_regression() -> None:
    """先证明对照组测得出差异（§2.11）：直接用 `extract_query_terms` 切拉丁查询就会退化。

    没有这一条，上面那 15 条「相等」可以被任何原因满足（比如两边都返回空）。
    """
    require_jieba()
    naive = text_terms.extract_query_terms("nekro_agent/services/kb/search_service.py")
    assert naive == [], "text_terms 按 32 字上限丢掉长路径——这正是不能整条查询丢给它的原因"
    assert _tokenize_query("nekro_agent/services/kb/search_service.py") == [
        "nekro_agent/services/kb/search_service.py"
    ]


# ======================================================================================
# 3. 单调性：改前能命中的，改后一定还命中
# ======================================================================================


def _all_haystacks() -> list[str]:
    texts = [f"{heading} {body}" for doc in _DOCS for heading, body in doc.chunks]
    texts += [f"{doc.source.title} {doc.source.source_path} {doc.source.summary}" for doc in _DOCS]
    return [text.lower() for text in texts]


def test_every_pre_fix_hit_survives() -> None:
    """不变式：改前的任一 token 命中某段文本时，改后至少有一个 token 也命中它。

    成立的理由是构造性的：拉丁 token 逐字不变；每个 CJK 串要么被切成它自己的**子串**
    （原串命中则子串必然命中），要么因为切不出词而**原样保留**。少了"原样保留"那一支，
    `甯浩`/`昇腾` 这类词典里没有的专名会被 jieba 逐字切开、碎片再被 `MIN_TERM_LENGTH` 全部
    丢掉，于是整串一个 token 都不剩 —— 那恰恰是改前唯一能命中的那类查询。
    """
    require_jieba()
    haystacks = _all_haystacks()
    probes = [case.text for case in _QUERY_CASES] + [
        "甯浩", "昇腾", "言和", "洛天依", "乐正绫", "这个是什么", "工作区", "ABC工作区DEF",
    ]
    for query in probes:
        old = _pre_fix_tokenize(query)
        new = _tokenize_query(query)
        for haystack in haystacks:
            if any(token in haystack for token in old):
                assert any(token in haystack for token in new), (
                    f"改前命中、改后落空: query={query!r} old={old} new={new}"
                )


def test_token_emptiness_matches_the_pre_fix_tokenizer() -> None:
    """`tokens` 的空/非空必须与改前逐字一致。

    `search_workspace_kb` 里有一句 `score_threshold=0.48 if tokens else 0.42`：切词若让某类
    查询从"有 token"变成"没有 token"，**向量通道的准入阈值会跟着变**，而那与本次修复毫无关系。
    "切不出词就保留原串"那一支同时兜住了这条。
    """
    require_jieba()
    for query in [case.text for case in _QUERY_CASES] + ["甯浩", "这个是什么", "", "？！。，", "  "]:
        assert bool(_tokenize_query(query)) == bool(_pre_fix_tokenize(query)), query


# ======================================================================================
# 语料：11 篇文档 × 3 段
# ======================================================================================


@dataclass
class _FakeSource:
    title: str
    source_path: str
    category: str
    summary: str
    tags: list[str] = field(default_factory=list)


@dataclass
class _Doc:
    key: str
    source: _FakeSource
    chunks: list[tuple[str, str]]


_DOCS: list[_Doc] = [
    _Doc(
        key="D1",
        source=_FakeSource(
            title="沙盒网络隔离",
            source_path="docs/sandbox-network.md",
            category="运维",
            summary="沙盒容器的网桥、放行规则与兜底策略",
            tags=["沙盒", "网络"],
        ),
        chunks=[
            (
                "沙盒网络隔离 > 网桥与放行",
                "沙盒容器默认挂在一张独立网桥上，宿主通过 iptables 规则放行容器回连 NA 的 8021 端口。"
                "这组规则按标记分组，标记由部署作用域派生，装规则的语义是先按标记全删再装自己那一套。",
            ),
            (
                "沙盒网络隔离 > 兜底策略",
                "没有匹配到放行规则的入站流量落进兜底的 DROP。DROP 不回 RST，"
                "于是一次配置错误会表现为容器一直等待，直到 NA 按运行超时把它杀掉，日志里没有任何异常。",
            ),
            (
                "沙盒网络隔离 > 标记冲突",
                "同一台宿主上跑两套部署时，如果两套共用同一个标记，后装的那一套会把先装的规则整组删掉，"
                "生产实例的放行规则就这样被悄悄换掉了。",
            ),
        ],
    ),
    _Doc(
        key="D2",
        source=_FakeSource(
            title="工作区网络与归属分组",
            source_path="docs/workspace-network.md",
            category="运维",
            summary="工作区容器按归属分组各占一张网络",
            tags=["工作区", "网络"],
        ),
        chunks=[
            (
                "工作区网络 > 归属分组",
                "工作区容器按归属分组隔离，一个分组一张 Docker 网络，避免同一张网桥上的容器互相访问。"
                "单一租户的工作区落进租户分组，全平台方或没有绑定的落进平台组。",
            ),
            (
                "工作区网络 > 地址池",
                "Docker 默认地址池实测再切 26 张网络即耗尽，大约对应 25 个租户分组。"
                "租户数接近这个量级时要先放大 default-address-pools，否则新建工作区会直接失败。",
            ),
            (
                "工作区网络 > 中继放行",
                "租户的 CC 工作区需要一条回连中继端口的放行规则，否则容器里的请求会一直超时。",
            ),
        ],
    ),
    _Doc(
        key="D3",
        source=_FakeSource(
            title="好感度系统",
            source_path="docs/affection.md",
            category="功能",
            summary="好感度做成核心服务，影响提示词注入",
            tags=["好感度"],
        ),
        chunks=[
            (
                "好感度 > 定位",
                "好感度做成核心服务而不是插件。数值只是内部实现，对外的面孔是模型生成的、"
                "带人设口吻的状态自述，机器人不许把档位名和数字说出口。",
            ),
            (
                "好感度 > 榜单渲染",
                "好感度榜单同步渲染实测会让事件循环停摆 1.3 秒，必须用 asyncio.to_thread 把这段"
                "密集计算移出事件循环，否则一个插件就能拖垮进程里的全部实例。",
            ),
            (
                "好感度 > 防速刷",
                "涨跌不要太快，群聊和私聊合并计一份，分寸靠提示词语境约束。",
            ),
        ],
    ),
    _Doc(
        key="D4",
        source=_FakeSource(
            title="记忆系统与中文分词",
            source_path="docs/memory-tokenize.md",
            category="功能",
            summary="字面召回依赖中文分词，两档切分",
            tags=["记忆", "分词"],
        ),
        chunks=[
            (
                "中文分词 > 两档切分",
                "记忆检索的字面召回依赖中文分词。装了 jieba 就走词典档，缺包或词典加载失败时"
                "回落到启发式档，按虚词字符下刀，切不动的整块丢弃。缺包只降级不报错。",
            ),
            (
                "中文分词 > 词典加载",
                "词典在第一次切词时触发加载，冷缓存约 0.85 秒纯计算，绝不能压在事件循环上，"
                "所以启动链路用 asyncio.to_thread 排一次预热。",
            ),
            (
                "中文分词 > 两端同一套规则",
                "写入端把任务正文切成实体名，检索端把查询切成召回词，两端必须共用同一套切分规则，"
                "否则写进去的是整句、查出来的是短词，图谱等于没建。",
            ),
        ],
    ),
    _Doc(
        key="D5",
        source=_FakeSource(
            title="租户与权限模型",
            source_path="docs/tenant-permission.md",
            category="架构",
            summary="三级权限与越权返回码",
            tags=["租户", "权限"],
        ),
        chunks=[
            (
                "权限 > 三级角色",
                "平台方、租户管理员、租户用户三级。租户的权限上限被钉在管理员一档，永远够不到平台方那一级。",
            ),
            (
                "权限 > 越权返回码",
                "越权一律返回 404 而不是 403。403 会泄露「该资源存在」这件事，让资源标识可以被枚举；"
                "能力边界不足才用 403。",
            ),
            (
                "权限 > 作用域",
                "租户没有全局视图这一档，实例标识为空对平台方是看全部，对租户则直接 400。",
            ),
        ],
    ),
    _Doc(
        key="D6",
        source=_FakeSource(
            title="模型组配置",
            source_path="docs/model-group.md",
            category="配置",
            summary="视觉能力声明与凭据归属",
            tags=["模型组"],
        ),
        chunks=[
            (
                "模型组 > 视觉能力",
                "模型组上的视觉能力开关声明这个模型能不能吃图片。判错成看不到只少一路信息，"
                "判错成能看会让整轮对话失败，而且图片取自整个上下文窗口，连纯文字消息都被拖下水。",
            ),
            (
                "模型组 > 凭据归属",
                "平台方的密钥绝不回显给租户；租户自建的模型组用的是他自己的密钥，回显没问题。"
                "所以判断能不能看凭据要按归属分支，不能一刀切掩码。",
            ),
            (
                "模型组 > 预置项",
                "模型组的接口地址下拉里有一个置顶的预置中转站。",
            ),
        ],
    ),
    _Doc(
        key="D7",
        source=_FakeSource(
            title="数据库迁移",
            source_path="docs/migration.md",
            category="运维",
            summary="迁移生成、序号连续性与时区",
            tags=["迁移", "数据库"],
        ),
        chunks=[
            (
                "迁移 > 怎么生成",
                "迁移文件禁止手写，必须用 poe db-revision 生成再 poe db-migrate 执行，"
                "这样才能保证状态字段被正确写入。",
            ),
            (
                "迁移 > 序号连续性",
                "每轮部署之后核对 migrations/models 目录的序号连续性，注意 ls 给的是字典序不是数字序，"
                "曾经出现过 14 到 16 的空档。",
            ),
            (
                "迁移 > 时区",
                "ORM 配置的时区与进程本地时区不一致时，时间窗查询会静默错位，没有任何报错。",
            ),
        ],
    ),
    _Doc(
        key="D8",
        source=_FakeSource(
            title="kb search service",
            source_path="nekro_agent/services/kb/search_service.py",
            category="code",
            summary="hybrid retrieval for workspace knowledge base",
            tags=["kb", "search"],
        ),
        chunks=[
            (
                "search_workspace_kb",
                "search_workspace_kb runs a vector pass through kb_qdrant_manager.search_grouped and a "
                "keyword pass through _collect_keyword_hits, then merges both hit lists and buckets them per document.",
            ),
            (
                "_tokenize_query",
                "_tokenize_query lowers the query and splits it with re.findall over [a-z0-9_./-]+ and the CJK "
                "character class, then deduplicates the tokens while keeping their original order.",
            ),
            (
                "thresholds",
                "KEYWORD_CHUNK_SCORE_THRESHOLD gates chunk level keyword hits and "
                "KEYWORD_DOCUMENT_FALLBACK_THRESHOLD gates the document level fallback hit.",
            ),
        ],
    ),
    _Doc(
        key="D9",
        source=_FakeSource(
            title="启动日志与凭据卫生",
            source_path="docs/log-hygiene.md",
            category="安全",
            summary="日志里的凭据泄漏面",
            tags=["日志", "凭据"],
        ),
        chunks=[
            (
                "日志 > 配置回显",
                "启动时那条把配置整个打出来的调试日志会带上全部凭据，凭据字段要遮蔽，"
                "否则一次启动就把密钥写进磁盘。",
            ),
            (
                "日志 > 诊断渲染",
                "日志库的诊断渲染默认开着，会把每一行源码里的变量名连同取值一起渲染出来，"
                "连库失败时连数据库口令都会打出来。",
            ),
            (
                "日志 > 历史文件",
                "这些修复对已经落盘的历史日志无效，上线前要把历史日志清掉或者单独归档。",
            ),
        ],
    ),
    _Doc(
        key="D10",
        source=_FakeSource(
            title="插件加载与故障隔离",
            source_path="docs/plugin-isolation.md",
            category="架构",
            summary="插件调用护栏与残余风险",
            tags=["插件"],
        ),
        chunks=[
            (
                "插件 > 调用护栏",
                "宿主调用插件代码一律走统一的隔离入口，里面有异常兜底、超时切断、阻塞观测"
                "以及连续失败自动隔离且可恢复。",
            ),
            (
                "插件 > 残余风险",
                "纯同步的死循环不带 await，无法被超时切断，单进程内没有抢占它的办法，"
                "只能靠告警发现再人工处理。",
            ),
            (
                "插件 > 加载失败排查",
                "插件加载失败时去看启动日志里的堆栈，确认依赖装齐、入口类名没写错。",
            ),
        ],
    ),
    _Doc(
        key="D11",
        source=_FakeSource(
            title="运维工作流程",
            source_path="docs/ops-workflow.md",
            category="运维",
            summary="上线前后的例行工作",
            tags=["运维"],
        ),
        chunks=[
            (
                "流程 > 上线清单",
                "上线前的工作按清单逐项确认，每一项都要把实际输出贴进发布记录，全部符合才允许接真实流量。",
            ),
            (
                "流程 > 维护窗口",
                "换密钥这类工作要放在维护窗口做，全部已签发的令牌会当场失效，所有人都要重新登录。",
            ),
            (
                "流程 > 网络排查",
                "网络不通时先看端口是不是绑在 0.0.0.0 上，再看规则有没有被别人顶掉，最后才怀疑应用本身。",
            ),
        ],
    ),
]


@dataclass
class _QueryCase:
    kind: str
    text: str
    gold: tuple[str, ...]


_CN_KINDS = frozenset({"中文长句", "中英混合", "含标点", "超短"})
_EN_KINDS = frozenset({"纯标识符", "英文", "代码片段"})

_QUERY_CASES: list[_QueryCase] = [
    _QueryCase("中文长句", "工作区网络隔离是怎么做的", ("D2C1",)),
    _QueryCase("中文长句", "沙盒容器为什么一直超时没有任何反应", ("D1C2",)),
    _QueryCase("中文长句", "好感度榜单渲染卡住事件循环那次是怎么修的", ("D3C2",)),
    _QueryCase("中文长句", "中文分词现在用的是什么方案", ("D4C1",)),
    _QueryCase("中文长句", "租户越权的时候应该返回什么状态码", ("D5C2",)),
    _QueryCase("中文长句", "数据库迁移文件可以手写吗", ("D7C1",)),
    _QueryCase("中文长句", "启动日志里会不会泄露凭据", ("D9C1",)),
    _QueryCase("中文长句", "插件加载失败要怎么排查", ("D10C3",)),
    _QueryCase("中文长句", "模型组的视觉能力是怎么判断的", ("D6C1",)),
    _QueryCase("中文长句", "同一台宿主上跑两套部署规则会不会冲突", ("D1C3",)),
    _QueryCase("中文长句", "词典加载会不会阻塞事件循环", ("D4C2",)),
    _QueryCase("中文长句", "历史日志要不要单独归档", ("D9C3",)),
    _QueryCase("中英混合", "Docker 地址池不够用了怎么办", ("D2C2",)),
    _QueryCase("中英混合", "jieba 词典档和启发式档有什么区别", ("D4C1",)),
    _QueryCase("中英混合", "iptables 规则被谁改掉了", ("D1C3",)),
    _QueryCase("中英混合", "asyncio.to_thread 是在哪里用的", ("D3C2",)),
    _QueryCase("纯标识符", "_tokenize_query", ("D8C2",)),
    _QueryCase("纯标识符", "KEYWORD_CHUNK_SCORE_THRESHOLD", ("D8C3",)),
    _QueryCase("纯标识符", "kb_qdrant_manager.search_grouped", ("D8C1",)),
    _QueryCase("纯标识符", "nekro_agent/services/kb/search_service.py", ("D8C1",)),
    _QueryCase("英文", "hybrid retrieval keyword pass", ("D8C1",)),
    _QueryCase("英文", "deduplicates the tokens", ("D8C2",)),
    _QueryCase("代码片段", "poe db-revision", ("D7C1",)),
    _QueryCase("代码片段", "default-address-pools", ("D2C2",)),
    _QueryCase("含标点", "越权？返回 404 还是 403？", ("D5C2",)),
    _QueryCase("含标点", "DROP 不回 RST，会怎样？", ("D1C2",)),
    _QueryCase("超短", "地址池", ("D2C2",)),
    _QueryCase("超短", "好感度", ("D3C1",)),
    _QueryCase("超短", "兜底", ("D1C2",)),
]


# ======================================================================================
# 4. 标定：改前 / 改后的召回与误召
# ======================================================================================


def _collect(tokens: list[str], query: str, threshold: float, limit: int = 5) -> set[str]:
    """复刻 `_collect_keyword_hits` 的判据链，返回命中的段落键（形如 `D2C1`）。

    只把 token 换掉，文档闸门、chunk 阈值、文档级兜底三步全部调产品自己的打分函数，
    这样标定出来的数字与线上是同一套算法（而不是另写一个近似模型）。
    """
    scored = [
        (doc, _keyword_document_score(query=query, tokens=tokens, source=cast(Any, doc.source)))
        for doc in _DOCS
    ]
    scored = [(doc, score) for doc, score in scored if score > 0]
    scored.sort(key=lambda item: item[1], reverse=True)
    candidates = scored[: max(limit * KEYWORD_DOC_CANDIDATE_FACTOR, 12)]

    hits: set[str] = set()
    for doc, doc_score in candidates:
        doc_hits = [
            f"{doc.key}C{index}"
            for index, (heading, body) in enumerate(doc.chunks, start=1)
            if _keyword_chunk_score(
                query=query,
                tokens=tokens,
                heading_path=heading,
                chunk_text=body,
                source=cast(Any, doc.source),
            )
            >= threshold
        ]
        if not doc_hits and doc_score >= KEYWORD_DOCUMENT_FALLBACK_THRESHOLD:
            doc_hits = [f"{doc.key}C1"]
        hits.update(doc_hits)
    return hits


def _measure(tokenizer: Any, threshold: float, kinds: frozenset[str]) -> tuple[int, int, int]:
    """返回 (召回数, 应召总数, 误召数)。误召只算来自**非 gold 文档**的段落。"""
    recalled = ceiling = noise = 0
    for case in _QUERY_CASES:
        if case.kind not in kinds:
            continue
        hits = _collect(tokenizer(case.text), case.text, threshold)
        gold = set(case.gold)
        gold_docs = {key[: key.index("C")] for key in gold}
        recalled += len(hits & gold)
        ceiling += len(gold)
        noise += len([key for key in hits if key[: key.index("C")] not in gold_docs])
    return recalled, ceiling, noise


def test_pre_fix_chinese_keyword_channel_is_effectively_dead() -> None:
    """改前的量化：21 条应召段落只召回 3 条，且**一条误召都没有**。

    "误召 0"不是它准，是它几乎什么都没返回 —— 下一条的 24/319 才是根因。
    这两个数字就是 #45 "中文查询的关键词通道基本恒为 0 命中"的实测依据。
    """
    assert _measure(_pre_fix_tokenize, 0.22, _CN_KINDS) == (3, 21, 0)


def test_pre_fix_documents_almost_never_pass_the_candidate_gate() -> None:
    """改前中文查询连**文档闸门**都过不去：29 × 11 = 319 次机会里只有 5 次 `score > 0`。

    `_collect_keyword_hits` 先按 `_keyword_document_score(...) > 0` 挑候选文档，挑不出来就
    直接 `return []`。整句 token 既不在标题也不在摘要里，于是这一步就把整条关键词通道关掉了，
    chunk 阈值定多少根本没有观测意义 —— 这也是为什么"阈值重标"必须在切词修好之后做。
    """
    require_jieba()
    def _passes(tokenizer: Any) -> int:
        return sum(
            1
            for case in _QUERY_CASES
            for doc in _DOCS
            if _keyword_document_score(
                query=case.text, tokens=tokenizer(case.text), source=cast(Any, doc.source)
            )
            > 0
        )

    assert _passes(_pre_fix_tokenize) == 5
    assert _passes(_tokenize_query) == 24


def test_post_fix_chinese_recall_at_the_calibrated_threshold() -> None:
    """改后（词典档 + 阈值 0.26）：中文召回 3/21 → 14/21，误召 0 → 3。

    这就是 #45 的收益与代价。误召那 3 条全部来自同一条查询（「工作区网络隔离是怎么做的」
    把《沙盒网络隔离》也带了出来）—— 主题相邻，不是随机噪声。
    """
    require_jieba()
    assert _measure(_tokenize_query, KEYWORD_CHUNK_SCORE_THRESHOLD, _CN_KINDS) == (14, 21, 3)


def test_post_fix_english_recall_is_unchanged() -> None:
    """英文/标识符那一组改前改后**逐字相同**——这是"不许退化"在端到端一侧的判据。

    上面第 2 组盯的是 token，这一条盯的是**过完整条打分链之后的命中集合**：token 一样但
    阈值抬过头，一样会把英文命中掐掉。
    """
    require_jieba()
    before = _measure(_pre_fix_tokenize, 0.22, _EN_KINDS)
    after = _measure(_tokenize_query, KEYWORD_CHUNK_SCORE_THRESHOLD, _EN_KINDS)
    assert before == (2, 8, 0)
    assert after == before


def test_heuristic_backend_recall_does_not_regress(heuristic_backend: None) -> None:
    """缺 jieba 的部署也只该变好不该变差：3/21 → 4/21，误召仍是 0。

    阈值是按词典档标的，所以必须单独确认它在回落档上没有把命中掐掉。
    """
    assert _measure(_pre_fix_tokenize, 0.22, _CN_KINDS) == (3, 21, 0)
    assert _measure(_tokenize_query, KEYWORD_CHUNK_SCORE_THRESHOLD, _CN_KINDS) == (4, 21, 0)
    assert _measure(_tokenize_query, KEYWORD_CHUNK_SCORE_THRESHOLD, _EN_KINDS) == (2, 8, 0)


def test_no_pre_fix_hit_is_lost_end_to_end() -> None:
    """逐条查询：改后@0.26 的命中集合必须**包含**改前@0.22 的全部命中。

    第 3 组证明的是 token 级单调，这一条把阈值那一跳也算进来 —— 抬阈值是可能把老命中掐掉的，
    所以"抬到 0.26 安全"这件事必须自己有判据，不能从 token 单调性推出来。
    """
    require_jieba()
    for case in _QUERY_CASES:
        before = _collect(_pre_fix_tokenize(case.text), case.text, 0.22)
        after = _collect(_tokenize_query(case.text), case.text, KEYWORD_CHUNK_SCORE_THRESHOLD)
        assert before <= after, f"{case.text!r} 丢了改前的命中: {sorted(before - after)}"


# ======================================================================================
# 5. 阈值是推出来的，不是拍的
# ======================================================================================


def test_threshold_is_the_low_edge_of_the_measured_safe_window() -> None:
    """0.26 是实测安全窗口 [0.26, 0.28] 的下沿。

    扫描 0.17~0.34（步长 0.01）在同一份语料上得到的形状：

    * 中文召回从 0.17 起一路平到 0.34 都是 14/21 —— 在这段区间里抬阈值**不花召回**；
    * 中文误召 10 → 3，最后一次下降正好发生在 **0.26**；
    * **上沿由英文定**：英文那一组唯一一条靠 chunk 自身分数过线的命中打 0.2875 分，
      0.29 起就掉。中文误召其实到 0.34 还能再降到 1，但那已经在英文的窗外，不能要。

    于是窗口是 [0.26, 0.28]，取**下沿** 0.26：给那条 0.2875 分的英文命中留 0.0275 的余量
    （取 0.28 只剩 0.0075，换一种措辞就翻车）。

    **改阈值不重标就会红**：< 0.26 误召必然更多，≥ 0.29 英文必然掉命中。
    """
    require_jieba()
    assert KEYWORD_CHUNK_SCORE_THRESHOLD == 0.26

    steps = [round(step / 100, 2) for step in range(17, 35)]
    recall = {t: _measure(_tokenize_query, t, _CN_KINDS)[0] for t in steps}
    noise = {t: _measure(_tokenize_query, t, _CN_KINDS)[2] for t in steps}

    # 召回在整段扫描区间内是平的 —— "抬阈值不花召回"这句话的判据
    assert set(recall.values()) == {14}, recall

    # 英文安全窗：0.28 及以下不掉，0.29 起掉
    english_safe = [t for t in steps if t <= 0.28]
    assert all(_measure(_tokenize_query, t, _EN_KINDS)[0] == 2 for t in english_safe)
    assert _measure(_tokenize_query, 0.29, _EN_KINDS)[0] == 1

    # 在英文安全窗内，0.26 是**最早**达到最小误召的那个取值
    best_noise = min(noise[t] for t in english_safe)
    assert best_noise == 3
    assert min(t for t in english_safe if noise[t] == best_noise) == KEYWORD_CHUNK_SCORE_THRESHOLD
    assert noise[0.17] == 10, "低阈值那一端要真的更糟，否则这条扫描没有信息量"
    assert noise[0.25] == 4


def test_document_fallback_threshold_is_unaffected() -> None:
    """`KEYWORD_DOCUMENT_FALLBACK_THRESHOLD` 实测**不需要动**，所以没动。

    它管的是"整篇文档一个 chunk 都没过线，但文档元数据分够高"时补一条命中。
    直觉上抬高 chunk 阈值会让它触发得更多（更多文档没有过线的 chunk），
    实测在这份语料上：改前 / 改后词典档 / 改后启发式档、chunk 阈值 0.22 与 0.26 六种组合下
    它都只触发 **1 次**，而且触发的是对的那一篇（路径查询命中 `search_service.py`）。
    没有数据支持就不动它 —— 这条测试同时是"下次想动它先拿数据来"的锚点。
    """
    require_jieba()

    def _fires(tokenizer: Any, chunk_threshold: float) -> list[str]:
        fired: list[str] = []
        for case in _QUERY_CASES:
            tokens = tokenizer(case.text)
            for doc in _DOCS:
                doc_score = _keyword_document_score(
                    query=case.text, tokens=tokens, source=cast(Any, doc.source)
                )
                if doc_score <= 0 or doc_score < KEYWORD_DOCUMENT_FALLBACK_THRESHOLD:
                    continue
                passed = any(
                    _keyword_chunk_score(
                        query=case.text,
                        tokens=tokens,
                        heading_path=heading,
                        chunk_text=body,
                        source=cast(Any, doc.source),
                    )
                    >= chunk_threshold
                    for heading, body in doc.chunks
                )
                if not passed:
                    fired.append(f"{case.text} -> {doc.key}")
        return fired

    expected = ["nekro_agent/services/kb/search_service.py -> D8"]
    assert _fires(_pre_fix_tokenize, 0.22) == expected
    assert _fires(_tokenize_query, 0.22) == expected
    assert _fires(_tokenize_query, KEYWORD_CHUNK_SCORE_THRESHOLD) == expected


# ======================================================================================
# 6. 字符类：第二处实现不许漂
# ======================================================================================


def test_cjk_char_class_matches_text_terms() -> None:
    """知识库这份 CJK 字符类必须与 `text_terms` 那份**逐码位**相同。

    这不是洁癖：`text_terms` 负责把串切成词，知识库这一份负责决定**哪一段该送去切**。
    两边不一致时，落在差集里的 CJK 串会绕过分词、原样变成一个整块 token —— 正是 #45 的病灶
    在一个更隐蔽的位置复发。

    `tests/test_memory_chinese_segmentation.py::test_cjk_char_class_matches_the_kb_tokenizer`
    把 `text_terms` 那份钉在一张期望区间表上，但它**并不读知识库这个文件**，所以"两处必须一致"
    这句话此前没有任何东西在执行。这条补上跨文件的那一半。
    """
    for code in range(0x20, 0x10000):
        expected = bool(text_terms._CJK_PATTERN.match(chr(code)))  # noqa: SLF001
        actual = bool(search_service._CJK_CHAR_PATTERN.match(chr(code)))  # noqa: SLF001
        assert actual is expected, f"U+{code:04X} 的 CJK 归类两处不一致"


def test_source_file_is_nfc_stable() -> None:
    """字符类的端点必须写 `\\uXXXX` 转义（§2.18）。

    写字面汉字时 `U+F900`（兼容汉字）与 `U+8C48`（统一汉字）字形完全一样（都是"豈"），
    任何一次 NFC 规范化都会把字符类悄悄吹宽 11600 个码位，而 `git diff` 上那一行分毫未变。
    全仓已有一条 `test_no_source_file_contains_nfc_unstable_characters` 扫这个，这里再放一条
    本文件专属的近距离断言：改这一行的人第一眼就能看到判据。
    """
    import pathlib
    import unicodedata

    path = pathlib.Path(search_service.__file__)
    text = path.read_bytes().decode("utf-8")
    assert unicodedata.normalize("NFC", text) == text
    assert "\\u4e00-\\u9fff" in text, "字符类端点必须是转义写法，不能是字面汉字"


# ======================================================================================
# 7. 成本护栏
# ======================================================================================


def test_cjk_term_budget_bounds_a_long_query() -> None:
    """长中文查询的 token 数必须被压住。

    这是本次修复**引入**的成本：改前一段连续 CJK 恒等于 1 个 token，与长度无关；改后一句
    上千字的查询能切出几百个词，而每个 token 都要对每篇候选文档的每个 chunk 做 7 次子串查找。
    `schemas/kb.py:KBSearchRequest.query` 只有 `min_length=1`、没有上限（下面第一句断言现取，
    不是抄注释——原文写的是并不存在的 `KBSearchBody`），所以这条护栏不是假想防御。

    **上限写字面值，不读 `KEYWORD_MAX_CJK_TERMS`**：读常量的话「把常量调到 10 亿」这个变异
    会让断言恒成立——第一版就是这么写的，变异实测**存活**。
    """
    require_jieba()
    from nekro_agent.schemas.kb import KBSearchRequest

    # 护栏的前提**现取**：入参 schema 真的收得下一条超长查询（有上限的话这条护栏就该重新论证）
    assert len(KBSearchRequest(query="工" * 5000).query) == 5000
    long_query = re.sub(
        rf"[^{search_service._CJK_CLASS}]",  # noqa: SLF001
        "",
        "".join(body for doc in _DOCS for _heading, body in doc.chunks),
    )
    assert len(long_query) > 1000, "先确认这条查询真的够长"
    # 正向对照：不设上限时它真的能切出几百个词，否则下面那句上限没有信息量
    assert len(text_terms.extract_query_terms(long_query, limit=10**6)) > 150
    assert len(_tokenize_query(long_query)) <= 20
    assert KEYWORD_MAX_CJK_TERMS <= 20


def test_cjk_term_budget_is_a_cost_guard_not_a_quality_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    """把上限在 6 / 8 / 16 / 无穷之间来回调，召回与误召逐字不变。

    有这一条才敢在注释里写"这是成本护栏不是质量旋钮"——否则下一个人会把它当成调准确率的
    旋钮来拧。实测语料里最长的查询也只切出 6 个词，上限根本够不着。
    """
    require_jieba()
    baseline_cn = _measure(_tokenize_query, KEYWORD_CHUNK_SCORE_THRESHOLD, _CN_KINDS)
    baseline_en = _measure(_tokenize_query, KEYWORD_CHUNK_SCORE_THRESHOLD, _EN_KINDS)
    for budget in (6, 8, 16, 10**6):
        monkeypatch.setattr(search_service, "KEYWORD_MAX_CJK_TERMS", budget)
        assert _measure(_tokenize_query, KEYWORD_CHUNK_SCORE_THRESHOLD, _CN_KINDS) == baseline_cn
        assert _measure(_tokenize_query, KEYWORD_CHUNK_SCORE_THRESHOLD, _EN_KINDS) == baseline_en


def test_budget_exhaustion_falls_back_to_pre_fix_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """预算耗尽之后剩下的 CJK 串原样保留——也就是回到改前，绝不整段丢弃。

    "预算用完就不再产出 token"是最容易写出来的实现，而它会让长查询的后半段**静默消失**。
    """
    require_jieba()
    monkeypatch.setattr(search_service, "KEYWORD_MAX_CJK_TERMS", 0)
    assert _tokenize_query("工作区网络隔离是怎么做的") == _pre_fix_tokenize("工作区网络隔离是怎么做的")


# ======================================================================================
# 8. 接线：`search_workspace_kb` 真的跑到了新切词（§2.7）
# ======================================================================================

_WIRING_WORKSPACE_ID = 4501
_WIRING_TEXT = (
    "工作区容器按归属分组隔离，一个分组一张 Docker 网络，"
    "避免同一张网桥上的容器互相访问。这一段里没有任何一处写着完整的问句。"
)


@pytest.fixture
async def kb_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _seed_and_search(monkeypatch: pytest.MonkeyPatch, query: str) -> list[str]:
    """建一篇真文档 + 一条真 chunk，走**关键词回退**那条路跑真实检索。

    `embed_kb_text` 抛异常 ⇒ `query_vector` 为 None ⇒ 整条向量分支不进，
    于是不需要 Qdrant，命中与否完全由关键词通道决定，判定是确定性的。
    """
    from nekro_agent.models.db_kb_chunk import DBKBChunk
    from nekro_agent.models.db_kb_document import DBKBDocument

    async def _no_embedding(_text: str) -> list[float]:
        raise RuntimeError("测试里不打 embedding，走关键词回退")

    monkeypatch.setattr(search_service, "embed_kb_text", _no_embedding)
    monkeypatch.setattr(search_service, "read_normalized_content", lambda _doc: _WIRING_TEXT)

    if not await DBKBDocument.filter(workspace_id=_WIRING_WORKSPACE_ID).exists():
        document = await DBKBDocument.create(
            workspace_id=_WIRING_WORKSPACE_ID,
            title="工作区网络与归属分组",
            category="运维",
            summary="工作区容器按归属分组各占一张网络",
            tags=["工作区", "网络"],
            file_name="workspace-network.md",
            file_ext=".md",
            mime_type="text/markdown",
            format="markdown",
            source_path="docs/workspace-network.md",
            normalized_text_path="workspace-network.txt",
            content_hash="h-wsnet",
            file_size=1,
            extract_status="ready",
            sync_status="ready",
        )
        await DBKBChunk.create(
            workspace_id=_WIRING_WORKSPACE_ID,
            document_id=document.id,
            chunk_index=0,
            heading_path="工作区网络 > 归属分组",
            char_start=0,
            char_end=len(_WIRING_TEXT),
        )

    response = await search_service.search_workspace_kb(
        workspace_id=_WIRING_WORKSPACE_ID, query=query, limit=5
    )
    return [item.title for item in response.documents]


async def test_search_workspace_kb_really_uses_the_new_tokenizer(
    kb_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端：中文长句查询必须命中，而整句在正文里**一个字都不连续出现**。

    这是 §2.7 那条教训的落点。前面 7 组全部直接调 `_tokenize_query` 或打分函数，
    把 `search_workspace_kb` 里那一句 `tokens = _tokenize_query(query)` 换回整句 token，
    它们**一条都不会红**；而这条会 —— 正文里没有"工作区网络隔离是怎么做的"这 12 个字，
    改前的整句 token 必然落空。
    """
    require_jieba()
    assert await _seed_and_search(monkeypatch, "工作区网络隔离是怎么做的") == ["工作区网络与归属分组"]


async def test_the_wiring_case_can_actually_go_empty(
    kb_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反面对照（§2.11）：换一条与正文无关的中文查询必须返回空。

    没有这一条，上面那条"命中了"可以被任何原因满足——比如检索压根不过滤、有文档就返回。
    """
    require_jieba()
    assert await _seed_and_search(monkeypatch, "好感度榜单是怎么渲染的") == []


async def test_search_does_not_load_the_dictionary_on_the_event_loop(
    kb_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """词典没就绪时，知识库检索**这一次**回落到启发式档，加载挪到后台线程。

    知识库是 `extract_query_terms` 的第三个调用点，而它的预热只挂在记忆系统的调度器上
    （`MEMORY_ENABLE_SYSTEM` 关掉的部署压根不排预热）。所以"检索路径不会在事件循环里做
    0.85 秒同步加载"这条契约必须在**这个**调用点上也有判据，不能只在记忆那边测过就算数。

    判据是"加载发生在哪个线程"，不是耗时——后者只能靠计时，而计时不确定（§2.17）。
    """
    import threading

    saved = (
        text_terms._jieba_module,  # noqa: SLF001
        text_terms._jieba_import_failed,  # noqa: SLF001
        text_terms._jieba_warm_up_failed,  # noqa: SLF001
        text_terms._background_warm_up,  # noqa: SLF001
    )
    initialize_threads: list[int] = []
    ready = threading.Event()

    class _FakeJieba:
        def __init__(self) -> None:
            self.dt = type("_Dt", (), {"initialized": False})()

        def setLogLevel(self, _level: int) -> None:  # noqa: N802 - 镜像 jieba 的驼峰命名
            return

        def initialize(self) -> None:
            initialize_threads.append(threading.get_ident())
            self.dt.initialized = True
            ready.set()

        def lcut(self, run: str) -> list[str]:
            return [run]

    text_terms._jieba_module = cast(Any, _FakeJieba())  # noqa: SLF001
    text_terms._jieba_import_failed = False  # noqa: SLF001
    text_terms._jieba_warm_up_failed = False  # noqa: SLF001
    text_terms._background_warm_up = None  # noqa: SLF001
    try:
        await _seed_and_search(monkeypatch, "工作区网络隔离是怎么做的")
        assert threading.get_ident() not in initialize_threads, (
            "检索在事件循环里同步加载了词典——单进程托管几十个实例时这是全体停摆"
        )
        thread = text_terms._background_warm_up  # noqa: SLF001
        assert thread is not None, "必须把加载排到后台线程，否则永远升不上词典档"
        thread.join(timeout=10)
        assert ready.is_set()
    finally:
        (
            text_terms._jieba_module,  # noqa: SLF001
            text_terms._jieba_import_failed,  # noqa: SLF001
            text_terms._jieba_warm_up_failed,  # noqa: SLF001
            text_terms._background_warm_up,  # noqa: SLF001
        ) = saved


# ======================================================================================
# 9. 第二个出口 / 第二个消费者（复核补测，变异存活表见文件头）
# ======================================================================================

_LATIN_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_./-")

# 「标题/路径/摘要都命中查询，但正文里一次都没出现」——短关键词查询下很常见的一类文档。
# `_keyword_document_score` 只看元数据，所以它拿满分 1.2；而 `_keyword_chunk_score` 里
# 元数据的贡献是 `min(doc_score * 0.25, 0.25)`，**封顶 0.25**。
_METADATA_ONLY_QUERY = "sandbox"
_METADATA_ONLY_SOURCE = _FakeSource(
    title="sandbox",
    source_path="docs/sandbox.md",
    category="ops",
    summary="sandbox network notes",
)
_METADATA_ONLY_BODIES = (
    "first paragraph, no literal keyword here at all.",
    "second paragraph, also nothing.",
    "third paragraph, still nothing.",
    "fourth paragraph, still nothing.",
)
# 元数据比上面那篇弱一档（只有标题沾边）的对照文档
_WEAKER_METADATA_SOURCE = _FakeSource(
    title="sandbox playbook appendix",
    source_path="docs/misc.md",
    category="ops",
    summary="",
)


def _best_document_hit_score(source: _FakeSource, threshold: float) -> float:
    """复刻 `_collect_keyword_hits` 给一篇文档定分的那两步：chunk 过线取最高分，否则走兜底。"""
    tokens = _tokenize_query(_METADATA_ONLY_QUERY)
    typed = cast(Any, source)
    document_score = _keyword_document_score(query=_METADATA_ONLY_QUERY, tokens=tokens, source=typed)
    chunk_scores = [
        _keyword_chunk_score(
            query=_METADATA_ONLY_QUERY, tokens=tokens, heading_path="", chunk_text=body, source=typed
        )
        for body in _METADATA_ONLY_BODIES
    ]
    passed = [score for score in chunk_scores if score >= threshold]
    if passed:
        return max(passed)
    return document_score * 0.85 if document_score >= KEYWORD_DOCUMENT_FALLBACK_THRESHOLD else 0.0


def test_the_chunk_gate_sits_above_the_document_metadata_cap() -> None:
    """chunk 闸门必须**严格高于 0.25**——否则 chunk 能只靠所属文档的元数据分过线。

    这是 0.22 → 0.26 这一跳真正跨过的东西，而修复报告只把它记成「误召最后一次下降正好在
    0.26」。机制是结构性的、与语料无关：`_keyword_chunk_score` 里文档元数据那一项写作
    `min(_keyword_document_score(...) * 0.25, 0.25)`，**封顶 0.25**。于是

    * 阈值 0.22 时，`doc_score >= 0.88` 的文档，它的**每一个** chunk 都能只靠元数据过线，
      正文里一个字都不用沾；
    * 阈值 0.26 时这条通道整个关掉（0.25 < 0.26），这类文档改走文档级兜底，只出 1 条命中。

    为什么这是**好事**而不只是「少了几条」：改前那条通道会造成一次**排序倒挂**。
    元数据越强的文档，越容易靠 0.25 这个封顶值过线，于是它以 0.25 分入榜；而元数据**更弱**、
    弱到 chunk 一条都过不了线的文档反而落进兜底，拿到 `doc_score * 0.85`（0.4 起步即 0.34，
    通常远高于 0.25）。结果就是**匹配得更好的那篇排在更差的那篇后面**。下面第三段把这次倒挂
    量出来：改前 0.25 < 0.7395，改后 1.02 > 0.7395。

    标定语料测不出这一条：`_collect` 比的是**命中段落的集合**，而这类文档改前改后都在结果里
    （改前是它的若干 chunk，改后是兜底那一条），集合差不多，倒挂发生在**分数**上。
    所以这条不看语料，直接钉那个不变式。
    """
    require_jieba()
    tokens = _tokenize_query(_METADATA_ONLY_QUERY)
    typed = cast(Any, _METADATA_ONLY_SOURCE)
    document_score = _keyword_document_score(query=_METADATA_ONLY_QUERY, tokens=tokens, source=typed)
    metadata_cap = min(document_score * 0.25, 0.25)
    chunk_scores = [
        _keyword_chunk_score(
            query=_METADATA_ONLY_QUERY, tokens=tokens, heading_path="", chunk_text=body, source=typed
        )
        for body in _METADATA_ONLY_BODIES
    ]

    # 前提：这几段正文确实一个 token 都不沾，chunk 分**完全**来自元数据那一项
    assert metadata_cap == 0.25
    assert chunk_scores == [0.25] * len(_METADATA_ONLY_BODIES)

    # 对照：改前那档阈值下，这些「正文毫不相干」的 chunk 全都能过线
    assert all(score >= 0.22 for score in chunk_scores), "对照组失效，这条用例测不出东西"

    # 不变式：现在一条都过不去
    assert KEYWORD_CHUNK_SCORE_THRESHOLD > 0.25, "chunk 闸门跌回元数据封顶值之下，光靠标题就能命中正文"
    assert not any(score >= KEYWORD_CHUNK_SCORE_THRESHOLD for score in chunk_scores)

    # 倒挂：元数据更强的文档，改前反而排在更弱的那篇后面
    weaker_before = _best_document_hit_score(_WEAKER_METADATA_SOURCE, 0.22)
    stronger_before = _best_document_hit_score(_METADATA_ONLY_SOURCE, 0.22)
    weaker_after = _best_document_hit_score(_WEAKER_METADATA_SOURCE, KEYWORD_CHUNK_SCORE_THRESHOLD)
    stronger_after = _best_document_hit_score(_METADATA_ONLY_SOURCE, KEYWORD_CHUNK_SCORE_THRESHOLD)
    assert stronger_before < weaker_before, "对照组失效：改前本该是倒挂的"
    assert stronger_after > weaker_after, "元数据更强的文档没有排到前面"


def test_query_segmenter_char_class_matches_the_dispatch_class() -> None:
    """**划 token 边界**的字符类与**决定送不送去分词**的字符类必须同源。

    第 6 组盯的是 `_CJK_CHAR_PATTERN`——它只回答「这一段要不要交给 `text_terms` 切」。
    真正划出 token 边界的是另一份实现 `_QUERY_TOKEN_PATTERN`，而**没有任何判据在盯它**：
    把它单独收窄成只认统一汉字（`[\\u4e00-\\u9fff]`），假名 / 谚文 / 扩展 A / 兼容汉字就从
    「token」直接退化成「分隔符」——日文、韩文查询的 token 表整个变空，关键词通道恒为 0 命中，
    连 `search_workspace_kb` 里 `score_threshold=0.48 if tokens else 0.42` 那条向量分支都会翻面。
    实测这个变异下第 6 组那条逐码位用例**全绿**（它比的是 `_CJK_CHAR_PATTERN`，没被动过）。

    判据取自 `text_terms._CJK_PATTERN`（那一份已由
    `test_memory_chinese_segmentation.py::test_cjk_char_class_matches_the_kb_tokenizer`
    钉在一张硬编码区间表上），于是三处实现被串成一条链，谁先漂谁红。
    """
    for code in range(0x20, 0x10000):
        char = chr(code)
        produced = _QUERY_TOKEN_PATTERN.findall(char * 2)
        if text_terms._CJK_PATTERN.match(char):  # noqa: SLF001
            assert produced == [char * 2], f"U+{code:04X} 是 CJK，却被分段器当成了分隔符"
        elif char not in _LATIN_TOKEN_CHARS:
            assert produced == [], f"U+{code:04X} 不是 token 字符，不该产出 token"


def test_terms_are_deduplicated_across_separate_cjk_runs() -> None:
    """去重必须**跨串**生效——这是切词修复新增出来的重复面。

    改前一个连续 CJK 串就是一个 token，重复只可能来自两段一模一样的原串；改后
    「网络隔离」与「网络分组」是两段不同的原串，却都会切出 `网络`。重复 token 会同时进
    `_keyword_document_score` 的分子与分母（`matched_tokens / len(tokens)`），把「命中比例」
    按重复次数放大，而 `KEYWORD_MAX_CJK_TERMS` 那份预算也会被同一个词吃掉两格。
    """
    require_jieba()
    tokens = _tokenize_query("网络隔离，网络分组")
    assert tokens == ["网络", "隔离", "分组"]
    assert len(tokens) == len(set(tokens))


# 12 段互不相同的连续 CJK，用中文逗号隔开——逗号两类字符集都不收，因此每一段各自成一个 run。
_MULTI_RUN_PHRASES = (
    "沙盒容器网络隔离", "工作区归属分组", "数据库迁移文件", "租户越权返回码",
    "模型组视觉能力", "好感度榜单渲染", "插件加载失败排查", "启动日志凭据遮蔽",
    "词典加载事件循环", "地址池耗尽扩容", "标记冲突整组删除", "规则清理接口调用",
)


def test_cjk_budget_is_spent_across_runs_not_per_run() -> None:
    """预算是**一次查询一份**，不是**一段一份**。

    第 7 组那条长查询是把语料里的非 CJK 字符全部剥掉拼出来的，于是它是**一整段**连续 CJK ——
    只会触发一次 `extract_query_terms`，`cjk_budget -= len(terms)` 那一句删掉照样全绿（实测存活）。
    真实查询里中文是被标点断开的：这条用 12 段真实短语，删掉递减之后每段各领一份完整预算，
    切出的词从 16 涨到 42，护栏形同虚设。

    上限写**字面值**，不读 `KEYWORD_MAX_CJK_TERMS`——读常量的话「把常量调到 10 亿」这个变异
    会让断言恒成立（第 7 组已经踩过一次）。
    """
    require_jieba()
    query = "，".join(_MULTI_RUN_PHRASES)
    runs = set(_QUERY_TOKEN_PATTERN.findall(query))
    assert len(runs) == len(_MULTI_RUN_PHRASES), "标点没把这些短语断开，这条用例就失去前提了"

    tokens = _tokenize_query(query)
    segmented = [token for token in tokens if token not in runs]

    # 正向对照：每段各自切、互不扣预算时总量远超上限，否则下面那句上限没有信息量
    per_run_total = sum(
        len(text_terms.extract_query_terms(phrase, limit=10**6)) for phrase in _MULTI_RUN_PHRASES
    )
    assert per_run_total > 30, f"这批短语一共只切出 {per_run_total} 个词，压不到预算上"

    assert len(segmented) <= 20, f"跨串预算失控，切出了 {len(segmented)} 个词"
    assert KEYWORD_MAX_CJK_TERMS <= 20

    # 预算耗尽之后剩下的串**原样保留**（回到改前），而不是被整段丢掉
    assert set(tokens) & runs, "预算用完之后的 CJK 串被静默丢弃了"


# --------------------------------------------------------------------------------------
# 9b. 阈值的两个比较点 + 素材那一路（端到端）
# --------------------------------------------------------------------------------------

_BRACKET_QUERY = "工作区网络隔离是怎么做的"  # 词典档切成 ["工作", "网络", "隔离"]
# 正文含 `网络`/`隔离` 但不含 `工作`，也不含整句；标题只含 `网络`。
# 于是文档元数据分 0.20 < KEYWORD_DOCUMENT_FALLBACK_THRESHOLD（兜底不会把「没命中」补成「命中」），
# 而 chunk 分落在一个可以从两侧夹逼的中间值上。
_BRACKET_TITLE = "网络基础"
_BRACKET_PATH = "docs/base.md"
_BRACKET_TEXT = "容器网络隔离的说明段落，这一段里没有写完整的问句。"
_BRACKET_DOC_WORKSPACE_ID = 4502
_BRACKET_ASSET_WORKSPACE_ID = 4503


def _bracket_scores() -> tuple[float, float]:
    """用**产品自己的**打分函数算出这份固件的 (文档分, chunk 分)。

    不写死数字：打分权重将来若改动，夹逼的两个端点跟着动，这条用例测的仍是
    「比较点读的是不是那个常量」，而不是「权重有没有变」。
    """
    source = cast(Any, _FakeSource(title=_BRACKET_TITLE, source_path=_BRACKET_PATH, category="", summary=""))
    tokens = _tokenize_query(_BRACKET_QUERY)
    return (
        _keyword_document_score(query=_BRACKET_QUERY, tokens=tokens, source=source),
        _keyword_chunk_score(
            query=_BRACKET_QUERY, tokens=tokens, heading_path="", chunk_text=_BRACKET_TEXT, source=source
        ),
    )


def _assert_bracket_fixture_is_usable() -> float:
    document_score, chunk_score = _bracket_scores()
    assert document_score < KEYWORD_DOCUMENT_FALLBACK_THRESHOLD, (
        "文档级兜底会触发，'没命中' 就会被补成一条命中，夹逼失效"
    )
    assert document_score > 0, "文档闸门过不去的话根本轮不到 chunk 阈值"
    assert chunk_score > KEYWORD_CHUNK_SCORE_THRESHOLD, "默认阈值下就该命中，否则夹逼的下沿没意义"
    return chunk_score


def _no_embedding_factory() -> Any:
    async def _no_embedding(_text: str) -> list[float]:
        raise RuntimeError("测试里不打 embedding，走关键词回退")

    return _no_embedding


async def _seed_bracket_document(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.models.db_kb_chunk import DBKBChunk
    from nekro_agent.models.db_kb_document import DBKBDocument

    monkeypatch.setattr(search_service, "embed_kb_text", _no_embedding_factory())
    monkeypatch.setattr(search_service, "read_normalized_content", lambda _doc: _BRACKET_TEXT)

    document = await DBKBDocument.create(
        workspace_id=_BRACKET_DOC_WORKSPACE_ID,
        title=_BRACKET_TITLE,
        category="",
        summary="",
        tags=[],
        file_name="base.md",
        file_ext=".md",
        mime_type="text/markdown",
        format="markdown",
        source_path=_BRACKET_PATH,
        normalized_text_path="base.txt",
        content_hash="h-bracket-doc",
        file_size=1,
        extract_status="ready",
        sync_status="ready",
    )
    await DBKBChunk.create(
        workspace_id=_BRACKET_DOC_WORKSPACE_ID,
        document_id=document.id,
        chunk_index=0,
        heading_path="",
        char_start=0,
        char_end=len(_BRACKET_TEXT),
    )


async def _seed_bracket_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.models.db_kb_asset import DBKBAsset
    from nekro_agent.models.db_kb_asset_binding import DBKBAssetBinding
    from nekro_agent.models.db_kb_asset_chunk import DBKBAssetChunk

    monkeypatch.setattr(search_service, "embed_kb_text", _no_embedding_factory())
    monkeypatch.setattr(search_service, "read_asset_normalized_content", lambda _asset: _BRACKET_TEXT)

    asset = await DBKBAsset.create(
        title=_BRACKET_TITLE,
        category="",
        summary="",
        tags=[],
        file_name="base.md",
        file_ext=".md",
        mime_type="text/markdown",
        format="markdown",
        source_path=_BRACKET_PATH,
        content_hash="h-bracket-asset",
        file_size=1,
        extract_status="ready",
        sync_status="ready",
    )
    await DBKBAssetChunk.create(
        asset_id=asset.id, chunk_index=0, heading_path="", char_start=0, char_end=len(_BRACKET_TEXT)
    )
    await DBKBAssetBinding.create(asset_id=asset.id, workspace_id=_BRACKET_ASSET_WORKSPACE_ID)


async def _bracket_titles(workspace_id: int) -> list[str]:
    response = await search_service.search_workspace_kb(
        workspace_id=workspace_id, query=_BRACKET_QUERY, limit=5
    )
    return [item.title for item in response.documents]


async def test_document_chunk_gate_reads_the_calibrated_constant(
    kb_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_collect_keyword_hits` 里那个比较点用的必须**是常量本身**。

    第 5 组把 `KEYWORD_CHUNK_SCORE_THRESHOLD == 0.26` 这件事钉得很死，但它和第 4 组的标定
    全都跑在 `_collect`（判据链的复刻）上，**没有一条真的走进 `_collect_keyword_hits`**：
    把那一行改成 `if chunk_score < 0.22:`（常量原封不动地留在文件顶上），175 条测试一条不红
    ——「常量标定得再准，比较点没在用它」正是 §2.7 的形态。

    做法是把常量按住到分数两侧各夹一次：写死字面值的实现对 monkeypatch 无感，两侧必有一侧翻车。
    """
    require_jieba()
    chunk_score = _assert_bracket_fixture_is_usable()
    await _seed_bracket_document(monkeypatch)

    assert await _bracket_titles(_BRACKET_DOC_WORKSPACE_ID) == [_BRACKET_TITLE]

    monkeypatch.setattr(search_service, "KEYWORD_CHUNK_SCORE_THRESHOLD", chunk_score - 0.01)
    assert await _bracket_titles(_BRACKET_DOC_WORKSPACE_ID) == [_BRACKET_TITLE]

    monkeypatch.setattr(search_service, "KEYWORD_CHUNK_SCORE_THRESHOLD", chunk_score + 0.01)
    assert await _bracket_titles(_BRACKET_DOC_WORKSPACE_ID) == [], "阈值抬过 chunk 分了还在命中"


async def test_asset_exit_gets_the_same_tokens_and_the_same_gate(
    kb_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**素材是关键词通道的第二个出口**，切词与阈值在它身上也得真的生效。

    `search_workspace_kb` 把 `tokens` 分别喂给 `_collect_keyword_hits`（文档）和
    `_collect_keyword_asset_hits`（素材），两边各有一处 `KEYWORD_CHUNK_SCORE_THRESHOLD` 比较。
    第 1~8 组一处都没碰素材：把素材那一路的实参改成 `tokens=[]`、或把它的阈值写死 0.22，
    175 条测试一条不红（实测两条变异均存活）。全仓唯一跑到素材检索的
    `test_kb_and_resource_tenant_scope.py` 用的是英文查询，而且命中走的是**文档级兜底**
    （元数据分 0.87 ≥ 0.4），碰不到切词也碰不到 chunk 阈值。

    这里用中文查询 + 元数据分 0.20（兜底够不着）把两件事一起钉住：
    传空 tokens ⇒ 文档闸门直接 0 分 ⇒ 素材整个消失；阈值写死 ⇒ 夹逼的某一侧翻车。
    """
    require_jieba()
    chunk_score = _assert_bracket_fixture_is_usable()
    await _seed_bracket_asset(monkeypatch)

    assert await _bracket_titles(_BRACKET_ASSET_WORKSPACE_ID) == [_BRACKET_TITLE]

    monkeypatch.setattr(search_service, "KEYWORD_CHUNK_SCORE_THRESHOLD", chunk_score - 0.01)
    assert await _bracket_titles(_BRACKET_ASSET_WORKSPACE_ID) == [_BRACKET_TITLE]

    monkeypatch.setattr(search_service, "KEYWORD_CHUNK_SCORE_THRESHOLD", chunk_score + 0.01)
    assert await _bracket_titles(_BRACKET_ASSET_WORKSPACE_ID) == [], "素材那一路的阈值没在用常量"


# --------------------------------------------------------------------------------------
# 9c. 向量通道也在消费同一份 tokens
# --------------------------------------------------------------------------------------

_VECTOR_WORKSPACE_ID = 4504
_VECTOR_ASSET_WORKSPACE_ID = 4505
_VECTOR_TITLE = "alpha"
_VECTOR_PATH = "docs/vector.md"
_VECTOR_ASSET_PATH = "docs/vector-asset.md"
_VECTOR_TEXT = "alpha beta gamma delta"  # 与查询词毫无交集 ⇒ 关键词通道全程静默
_VECTOR_HEADING = "网络隔离"  # 只有标题层级路径沾边 ⇒ 分数变化只可能来自 metadata bonus


def _assert_keyword_channel_is_silent(source_path: str) -> None:
    """先证明关键词通道在这份固件上全程 0 分，否则向量侧的分数不能归因给 metadata bonus。"""
    source = cast(Any, _FakeSource(title=_VECTOR_TITLE, source_path=source_path, category="", summary=""))
    assert _keyword_document_score(
        query=_BRACKET_QUERY, tokens=_tokenize_query(_BRACKET_QUERY), source=source
    ) == 0.0


def _fake_grouped(chunk_id: int, captured: dict[str, Any]) -> Any:
    async def _search_grouped(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return [{"hits": [{"id": chunk_id, "payload": {}, "score": 0.5}]}]

    return _search_grouped


async def _fake_embed(_text: str) -> list[float]:
    return [0.0, 0.0, 0.0]


async def _assert_vector_pass_uses_tokens(workspace_id: int, captured: dict[str, Any]) -> None:
    """向量通道对 `tokens` 的两处消费各自有判据：准入阈值分支 + metadata bonus。"""
    response = await search_service.search_workspace_kb(
        workspace_id=workspace_id, query=_BRACKET_QUERY, limit=5
    )
    assert captured, "向量分支根本没跑起来，这条用例什么都没测到"
    assert captured["score_threshold"] == 0.48, "有 token 的查询走的是收紧那一档"
    assert [item.title for item in response.documents] == [_VECTOR_TITLE]
    # 0.5（向量基分）+ 0.08 × 2（`网络`/`隔离` 命中 heading_path）
    assert response.documents[0].document_score == pytest.approx(0.66)

    # 反面对照：切不出 token 的查询必须放宽到 0.42，且拿不到 metadata bonus
    captured.clear()
    empty_token_response = await search_service.search_workspace_kb(
        workspace_id=workspace_id, query="？！。，", limit=5
    )
    assert _tokenize_query("？！。，") == []
    assert captured["score_threshold"] == 0.42, "没有 token 的查询必须放宽向量准入"
    assert empty_token_response.documents[0].document_score == pytest.approx(0.5)


async def test_vector_pass_consumes_the_same_tokens(
    kb_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tokens` 有**两个**消费者，向量那一个一条用例都没有。

    `search_workspace_kb` 把同一份 `tokens` 送去两个地方：关键词通道（第 1~8 组盯的）和
    向量通道的 `_apply_metadata_bonus` / `score_threshold=0.48 if tokens else 0.42`。
    第 8 组的接线用例刻意让 `embed_kb_text` 抛异常，于是**整条向量分支从头到尾没进过**：
    把 `_apply_metadata_bonus(tokens=tokens)` 改成 `tokens=[]`、或把 0.48/0.42 那条分支
    改成恒定 0.48，175 条测试一条不红（实测两条变异均存活）。

    `_tokenize_query` 的 docstring 里「`0.48 if tokens else 0.42` 那条分支不受影响」这句话
    此前也没有任何东西在执行——它依赖的前提（token 空/非空与改前一致）第 3 组测了，
    但「谁在消费这个前提」没测。这条把消费端补上。

    固件刻意让文档元数据一个 token 都不沾（`_keyword_document_score == 0` ⇒ 关键词通道
    在文档闸门就 `return []`），于是最终分数 = 向量基分 + metadata bonus，可以逐位断言。
    """
    require_jieba()
    from nekro_agent.models.db_kb_chunk import DBKBChunk
    from nekro_agent.models.db_kb_document import DBKBDocument

    document = await DBKBDocument.create(
        workspace_id=_VECTOR_WORKSPACE_ID,
        title=_VECTOR_TITLE,
        category="",
        summary="",
        tags=[],
        file_name="vector.md",
        file_ext=".md",
        mime_type="text/markdown",
        format="markdown",
        source_path=_VECTOR_PATH,
        normalized_text_path="vector.txt",
        content_hash="h-vector",
        file_size=1,
        extract_status="ready",
        sync_status="ready",
    )
    chunk = await DBKBChunk.create(
        workspace_id=_VECTOR_WORKSPACE_ID,
        document_id=document.id,
        chunk_index=0,
        heading_path=_VECTOR_HEADING,
        char_start=0,
        char_end=len(_VECTOR_TEXT),
    )

    captured: dict[str, Any] = {}
    monkeypatch.setattr(search_service, "embed_kb_text", _fake_embed)
    monkeypatch.setattr(search_service, "read_normalized_content", lambda _doc: _VECTOR_TEXT)
    monkeypatch.setattr(search_service.kb_qdrant_manager, "search_grouped", _fake_grouped(chunk.id, captured))

    _assert_keyword_channel_is_silent(_VECTOR_PATH)
    await _assert_vector_pass_uses_tokens(_VECTOR_WORKSPACE_ID, captured)


async def test_asset_vector_pass_consumes_the_same_tokens(
    kb_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """向量 × 素材——`tokens` 四个消费点里的第四个。

    上一条盯的是「向量 × 文档」。素材那一路是**另一份逐字重复的实现**
    （`kb_library_qdrant_manager.search_grouped` + 它自己那句
    `score_threshold=0.48 if tokens else 0.42` + 它自己那次 `_apply_metadata_bonus`），
    只要文档那一路测到了就以为素材也测到了，正是 §2 反复记的那种形态：实测把素材那两处
    分别改坏，加上上一条之后仍然**一条不红**。
    """
    require_jieba()
    from nekro_agent.models.db_kb_asset import DBKBAsset
    from nekro_agent.models.db_kb_asset_binding import DBKBAssetBinding
    from nekro_agent.models.db_kb_asset_chunk import DBKBAssetChunk

    asset = await DBKBAsset.create(
        title=_VECTOR_TITLE,
        category="",
        summary="",
        tags=[],
        file_name="vector-asset.md",
        file_ext=".md",
        mime_type="text/markdown",
        format="markdown",
        source_path=_VECTOR_ASSET_PATH,
        content_hash="h-vector-asset",
        file_size=1,
        extract_status="ready",
        sync_status="ready",
    )
    chunk = await DBKBAssetChunk.create(
        asset_id=asset.id,
        chunk_index=0,
        heading_path=_VECTOR_HEADING,
        char_start=0,
        char_end=len(_VECTOR_TEXT),
    )
    await DBKBAssetBinding.create(asset_id=asset.id, workspace_id=_VECTOR_ASSET_WORKSPACE_ID)

    captured: dict[str, Any] = {}
    monkeypatch.setattr(search_service, "embed_kb_text", _fake_embed)
    monkeypatch.setattr(search_service, "read_asset_normalized_content", lambda _asset: _VECTOR_TEXT)
    monkeypatch.setattr(
        search_service.kb_library_qdrant_manager, "search_grouped", _fake_grouped(chunk.id, captured)
    )

    _assert_keyword_channel_is_silent(_VECTOR_ASSET_PATH)
    await _assert_vector_pass_uses_tokens(_VECTOR_ASSET_WORKSPACE_ID, captured)
