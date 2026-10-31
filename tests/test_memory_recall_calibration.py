"""记忆召回的分数校准与中文实体抽取的契约回归测试。

钉死三条性质，它们都是"检索会系统性地返回错东西"这一类正确性缺陷：

1. **字面命中与向量相似度必须同量纲**。关系/Episode 的匹配分曾经是硬编码常量
   （基线 0.55 / 0.58），而向量检索的准入阈值是 0.5：只要切词命中任意一个词，字面候选就
   带着 0.55 挤进候选集，并在上下文编排里以 `similarity_score` 为主排序键排到真正的语义
   命中前面。改造后字面分由**实测的字面覆盖率**折算到本次查询里向量通道实际占据的区间，
   于是"完全字面包含最多与最好的语义命中打平""弱字面命中沉到语义命中之下"成为结构性质，
   不依赖调参。

2. **字面证据不许伪造语义相似度**。被向量通道判为不相关（没有返回）的段落，不能因为有一条
   关系指向它就带着关系的字面分补建成候选——那等于拿字面命中冒充语义命中。同理，被段落
   命中带出来的 Episode 应当继承那条段落的向量得分，而不是拿一个凭空的基线。

3. **中文实体抽取不许把整句登记成实体**。纯空格切分在中文任务描述下会把半句话当成"实体名"
   （实测产出过 `'扫码登录一直失败，qdrant'` 这样的节点），这些噪声实体又会顺着关系召回的
   `icontains` 污染检索结果——两条缺陷是连着的，所以放在同一个文件里回归。
"""

from __future__ import annotations

from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_entity import DBMemEntity, EntityType, MemorySource
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory import semantic_writer as semantic_writer_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.retriever import (
    MemoryRecallQuery,
    MemoryRetriever,
    _LexicalBand,
    compile_memories_for_context,
)
from nekro_agent.services.memory.semantic_writer import persist_cc_task_memory
from nekro_agent.services.memory.text_terms import (
    MAX_CJK_TERM_LENGTH,
    extract_entity_terms,
    extract_query_terms,
    heuristic_segmenter,
    lexical_coverage,
    resolve_segmenter,
)

WORKSPACE_ID = 4201
MEMORY_MODELS = [
    "nekro_agent.models.db_mem_paragraph",
    "nekro_agent.models.db_mem_entity",
    "nekro_agent.models.db_mem_relation",
    "nekro_agent.models.db_mem_episode",
    "nekro_agent.models.db_mem_reinforcement_log",
]


@pytest.fixture
async def memory_db():
    """真表跑记忆检索。

    召回是数据层与打分逻辑的合成结果，用假存储验证等于把要保护的东西替换掉；
    这里只注册记忆相关的模型，注册全量模型会把插件收集器一起拉进来。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": MEMORY_MODELS})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


class _ScriptedQdrant:
    """按段落 ID 返回固定相似度的向量通道替身。

    真实 Qdrant 的余弦得分不可控，而本文件要断言的恰恰是"某个具体的向量得分与字面分谁高"，
    所以把向量通道换成剧本；数据库、打分与编排全是真的。
    """

    def __init__(self, scores: dict[int, float]) -> None:
        self.scores = scores

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        threshold = float(kwargs.get("score_threshold", 0.0))
        return [
            {"id": paragraph_id, "score": score, "payload": {}}
            for paragraph_id, score in sorted(self.scores.items(), key=lambda item: -item[1])
            if score >= threshold
        ]


async def _create_paragraph(content: str, summary: str, episode_id: int | None = None) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=WORKSPACE_ID,
        instance_key="",
        memory_source="cc",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content=content,
        summary=summary,
        episode_id=episode_id,
        half_life_seconds=30 * 24 * 3600,
        origin_kind=OriginKind.TASK,
        origin_chat_key="__test__",
    )


async def _create_relation(subject: str, predicate: str, obj: str, paragraph_id: int | None) -> DBMemRelation:
    subject_entity, _ = await DBMemEntity.find_or_create(
        workspace_id=WORKSPACE_ID,
        entity_type=EntityType.CONCEPT,
        name=subject,
        source=MemorySource.CC,
    )
    object_entity, _ = await DBMemEntity.find_or_create(
        workspace_id=WORKSPACE_ID,
        entity_type=EntityType.CONCEPT,
        name=obj,
        source=MemorySource.CC,
    )
    return await DBMemRelation.create(
        workspace_id=WORKSPACE_ID,
        instance_key="",
        subject_entity_id=subject_entity.id,
        predicate=predicate,
        object_entity_id=object_entity.id,
        paragraph_id=paragraph_id,
        memory_source="cc",
        cognitive_type=CognitiveType.SEMANTIC.value,
        half_life_seconds=30 * 24 * 3600,
    )


async def _retrieve(monkeypatch: pytest.MonkeyPatch, query: str, scores: dict[int, float]):
    async def fake_embed(_text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]

    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.5)
    monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
    monkeypatch.setattr(retriever_module, "memory_qdrant_manager", _ScriptedQdrant(scores))
    result = await MemoryRetriever(WORKSPACE_ID, scope=MemoryScope.unrestricted()).retrieve(query, limit=10, record_access=False)
    return result.memories


# --------------------------------------------------------------------------------------
# 1. 词项切分：无词典档（下限）
#
# 这一节钉的是**没有 jieba 时**的行为，所以逐条显式传 `segmenter=heuristic_segmenter`。
# 不传的话这些表在装了 jieba 的环境上测的是词典档、在没装的环境上测的是启发式档 —— 同一份
# 断言在两台机器上验的是两件事（§2.9 同一条教训）。词典档的表在
# `tests/test_memory_chinese_segmentation.py`。
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 中文长句必须被虚词切开，而不是整句进词表
        ("记忆检索的排序，是怎么算的？", ["记忆检索", "排序"]),
        # 中英混合：标识符整体保留，中文另行切分
        ("napcat 扫码登录失败, qdrant 超时怎么排查", ["napcat", "扫码登录失败", "qdrant", "超时", "排查"]),
        # 文件路径带 `.`/`/` 不能被拆碎
        (
            "看看 services/memory/retriever.py 里的关系召回分数校准",
            ["看看", "services/memory/retriever.py", "关系召回分数校准"],
        ),
        # 英文虚词不进词表
        ("Fix the OAuth callback", ["fix", "oauth", "callback"]),
        # 本身就是词、只是恰好含一个虚词字的短串要保住
        ("对话", ["对话"]),
        # 通篇虚词切不出内容词
        ("是不是这样呢", []),
        ("", []),
    ],
)
def test_extract_query_terms_table(text: str, expected: list[str]) -> None:
    assert extract_query_terms(text, segmenter=heuristic_segmenter) == expected


@pytest.mark.parametrize("segmenter", [heuristic_segmenter, None], ids=["heuristic", "default"])
@pytest.mark.parametrize(
    "task_text",
    [
        "帮我把记忆检索的关系召回分数校准一下，顺便看看实体抽取为什么会产出整句",
        "napcat 扫码登录一直失败，qdrant 那边也超时，帮我排查一下",
        "重构工作区容器网络隔离逻辑并补齐同网桥互通场景的回归测试覆盖",
    ],
)
def test_entity_terms_are_words_not_sentences(task_text: str, segmenter: Any) -> None:
    """实体名必须是词，不能是半句话：这是 M11 噪声图谱的判据。

    两档都要满足——这条不变式与用了哪个分词器无关，换档不该把它换掉。
    """
    terms = extract_entity_terms(task_text, segmenter=segmenter)
    assert terms, "中文任务描述必须能抽出实体，改造前这里是 0 个"
    for term in terms:
        assert len(term) <= max(MAX_CJK_TERM_LENGTH, 32)
        assert not any(char.isspace() for char in term)
        assert not any(char in "，。？！；：、,.?!;:" for char in term)


@pytest.mark.parametrize("segmenter", [heuristic_segmenter, None], ids=["heuristic", "default"])
def test_entity_terms_cjk_never_exceeds_term_length(segmenter: Any) -> None:
    """纯 CJK 词项不得超过词长上限，否则就是把没切开的长串登记成了实体。"""
    terms = extract_entity_terms(
        "重构工作区容器网络隔离逻辑并补齐同网桥互通场景的回归测试覆盖",
        limit=10,
        segmenter=segmenter,
    )
    cjk_terms = [term for term in terms if all("一" <= char <= "鿿" for char in term)]
    assert cjk_terms
    assert all(len(term) <= MAX_CJK_TERM_LENGTH for term in cjk_terms)


@pytest.mark.parametrize(
    ("query", "texts", "expected"),
    [
        # 整条查询被原样包含 = 完全覆盖
        ("记忆检索", ("讨论了记忆检索的排序",), 1.0),
        # 命中 4 字词、漏掉 2 字词：按字符数加权 4/6
        ("记忆检索的排序，是怎么算的？", ("记忆检索", "使用", "向量阈值"), 4 / 6),
        # 一个词都没命中
        ("记忆检索的排序，是怎么算的？", ("好感度榜单", "related_to", "图片卡片"), 0.0),
        # 切不出词项
        ("是不是这样呢", ("随便什么文本",), 0.0),
        ("记忆检索", (),  0.0),
    ],
)
def test_lexical_coverage_table(query: str, texts: tuple[str, ...], expected: float) -> None:
    assert lexical_coverage(query, *texts, segmenter=heuristic_segmenter) == pytest.approx(expected)


# --------------------------------------------------------------------------------------
# 2. 分数校准的边界
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        (0.0, 0.5),  # 零覆盖落在向量准入阈值上，压不过任何语义命中
        (0.5, 0.6),
        (1.0, 0.7),  # 完全字面包含最多与最好的语义命中打平
        (-1.0, 0.5),  # 越界输入被夹住，不会跑到阈值以下
        (2.0, 0.7),  # 也不会越过本次查询的最高向量得分
    ],
)
def test_lexical_band_calibrate_boundaries(coverage: float, expected: float) -> None:
    band = _LexicalBand(floor=0.5, ceiling=0.7)
    assert band.calibrate(coverage) == pytest.approx(expected)


def test_lexical_band_never_exceeds_best_vector_hit() -> None:
    """字面分的上界就是本次查询的最高向量得分——这是 M8 要保证的核心不变量。"""
    for ceiling in (0.51, 0.63, 0.9, 1.0):
        band = _LexicalBand(floor=0.5, ceiling=ceiling)
        assert band.calibrate(1.0) <= ceiling
        assert band.calibrate(0.999) < ceiling


# --------------------------------------------------------------------------------------
# 3. 端到端：字面命中不再压过语义命中
# --------------------------------------------------------------------------------------


async def test_literal_relation_never_outranks_best_semantic_hit(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三元组完全包含查询时，最多与最好的语义命中打平，并且排在它后面进上下文。

    注入块的主排序键是 `similarity_score`，所以这条打平就意味着字面命中拿不到第一位。
    """
    query = "记忆检索的排序，是怎么算的？"
    top = await _create_paragraph("候选先按余弦得分排列，再乘以衰减后的基础权重。", "候选打分规则")
    await _create_relation("记忆检索", "决定", "排序", paragraph_id=None)

    memories = await _retrieve(monkeypatch, query, {top.id: 0.63})

    relation = next(mem for mem in memories if mem.source_type == "relation")
    paragraph = next(mem for mem in memories if mem.source_type == "paragraph")
    assert paragraph.similarity_score == pytest.approx(0.63)
    assert relation.similarity_score <= paragraph.similarity_score

    context = await compile_memories_for_context(
        recall_query=MemoryRecallQuery(query_text=query, focus_text="", focus_points=[], context_texts=[]),
        memories=memories,
        max_length=1200,
    )
    assert context.index("候选先按余弦得分排列") < context.index("记忆检索 - 决定 - 排序")


async def test_weak_literal_hit_no_longer_gets_the_hardcoded_baseline(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只碰上一个词的关系，分数必须低于改造前那个硬编码基线 0.55。

    0.55 高于向量准入阈值 0.5，正是它让"碰巧含某个词"稳定地压过 0.5~0.55 区间里真正的
    语义命中。改造后这个分由实测覆盖率决定，只覆盖 2/6 个查询字符就只能拿到 0.5433。
    """
    query = "记忆检索的排序，是怎么算的？"
    strong = await _create_paragraph("候选先按余弦得分排列。", "候选打分规则")
    await _create_relation("排序", "related_to", "好感度榜单", paragraph_id=None)

    memories = await _retrieve(monkeypatch, query, {strong.id: 0.63})

    relation = next(mem for mem in memories if mem.source_type == "relation")
    assert relation.similarity_score == pytest.approx(0.5 + (0.63 - 0.5) * (2 / 6))
    assert relation.similarity_score < 0.55
    assert relation.similarity_score < 0.63


async def test_literal_score_tracks_coverage(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """覆盖得多的字面命中必须严格高于覆盖得少的。

    改造前两者都是同一个常量，字面命中之间完全没有区分度。
    """
    query = "记忆检索的排序，是怎么算的？"
    strong = await _create_paragraph("候选先按余弦得分排列。", "候选打分规则")
    await _create_relation("记忆检索", "决定", "排序", paragraph_id=None)
    await _create_relation("排序", "related_to", "好感度榜单", paragraph_id=None)

    memories = await _retrieve(monkeypatch, query, {strong.id: 0.63})

    scores = {mem.summary: mem.similarity_score for mem in memories if mem.source_type == "relation"}
    assert scores["记忆检索 - 决定 - 排序"] > scores["排序 - related_to - 好感度榜单"]
    assert max(scores.values()) <= 0.63


async def test_relation_does_not_promote_vector_rejected_paragraph(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """向量通道没选中的段落，不得被关系带着字面分补建成候选。

    改造前这条路径会给一条余弦远低于阈值的段落安上关系的匹配分，实测里它排到了第一位。
    """
    query = "记忆检索的排序，是怎么算的？"
    hit = await _create_paragraph("候选先按余弦得分排列。", "候选打分规则")
    rejected = await _create_paragraph("好感度榜单同步渲染会阻塞事件循环。", "榜单渲染改异步")
    await _create_relation("记忆检索", "related_to", "截图工具", paragraph_id=rejected.id)

    memories = await _retrieve(monkeypatch, query, {hit.id: 0.63})

    paragraph_ids = {mem.target_id for mem in memories if mem.source_type == "paragraph"}
    assert rejected.id not in paragraph_ids
    assert hit.id in paragraph_ids
    # 线索没有丢：关系本身仍然是候选，三元组文本照样会进上下文
    assert any(mem.source_type == "relation" for mem in memories)


async def test_episode_reached_by_paragraph_inherits_vector_score(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """靠段落命中带出来的 Episode 继承段落的向量得分，而不是拿一个凭空的基线。"""
    query = "记忆检索的排序，是怎么算的？"
    episode = await DBMemEpisode.create(
        workspace_id=WORKSPACE_ID,
        instance_key="",
        origin_chat_key="__test__",
        title="一次没有字面重合的复盘",
        narrative_summary="复盘了候选打分与截断。",
        base_weight=1.0,
    )
    paragraph = await _create_paragraph("候选先按余弦得分排列。", "候选打分规则", episode_id=episode.id)

    memories = await _retrieve(monkeypatch, query, {paragraph.id: 0.62})

    episode_memory = next(mem for mem in memories if mem.source_type == "episode")
    assert episode_memory.similarity_score == pytest.approx(0.62)


# --------------------------------------------------------------------------------------
# 4. 写入端：中文任务不再产出整句实体
# --------------------------------------------------------------------------------------


class _StubQdrantWriter:
    async def upsert_paragraph(self, **_kwargs: Any) -> bool:
        return True


async def test_chinese_cc_task_writes_word_sized_entities(
    memory_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中文 CC 任务必须沉淀出词级实体与关系。

    走的是**产品默认路径**（不传 `segmenter`），所以这条同时也是"当前生效的那一档真的接在
    写入链路上"的证据。改造前纯空格切分对这段文本产出 0 个实体（整句长度超过 32 字被过滤），
    或者把 `'扫码登录一直失败，qdrant'` 这种半句话登记成实体名。
    """

    async def fake_embed(_text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]

    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(semantic_writer_module, "embed_text", fake_embed)
    monkeypatch.setattr(semantic_writer_module, "memory_qdrant_manager", _StubQdrantWriter())

    paragraph = await persist_cc_task_memory(
        workspace_id=WORKSPACE_ID,
        task_content="帮我把记忆检索的关系召回分数校准一下，顺便看看实体抽取为什么会产出整句",
        result_content=(
            "已经把关系召回的硬编码基线换成字面覆盖率，并把覆盖率折算到向量相似度的量纲上；"
            "实体抽取改成先按虚词切分再取词，中文任务描述不会再被整句登记成实体。"
        ),
        origin_ref="test-cc-1",
    )
    assert paragraph is not None

    entities = await DBMemEntity.filter(workspace_id=WORKSPACE_ID).all()
    names = [entity.name for entity in entities]
    expected_names = (
        ["记忆", "检索", "关系"]
        if resolve_segmenter() is not heuristic_segmenter
        else ["记忆检索", "关系召回分数校准", "会产出整句"]
    )
    assert names == expected_names
    assert all(len(entity.name) <= MAX_CJK_TERM_LENGTH for entity in entities)
    # 词典档下必须连"会产出整句"这类半句噪声都不再出现——它是无词典档留下的最后一批噪声实体，
    # 只断言"长度不超上限"是抓不住它的（5 个字，合规）。
    if resolve_segmenter() is not heuristic_segmenter:
        assert "会产出整句" not in names

    relations = await DBMemRelation.filter(workspace_id=WORKSPACE_ID).all()
    assert len(relations) == 2
    assert all(relation.paragraph_id == paragraph.id for relation in relations)
