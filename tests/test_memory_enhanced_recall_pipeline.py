"""增强记忆检索链路（M10）的开销与失败面回归测试。

M10 的原文是「增强检索模式每次渲染 prompt 都同步打一次 LLM 做检索规划 + N 次 embedding
+ N 次 Qdrant 查询，全串行」。这条链路整个挂在**主对话的正前方**：它慢一秒用户就晚一秒
收到回复，它不返回用户就永远收不到回复。本文件钉住四条性质：

1. **一次注入里的 N 条 query 并发执行**。它们彼此完全独立（各自一次 embedding + 一次
   向量检索），串行等于把等待时间原样叠加。对照组用串行实现跑同一批输入，证明这条断言
   真的能区分两种实现，而不是"两种实现下都成立"。

2. **强化写库仍然留在检索之后、按输入顺序串行**。`_record_access` 的 `reinforce()` 是
   读改写，并靠 `REINFORCE_COOLDOWN_SECONDS` 的窗口判定挡住重复强化 —— 而那个判定读的
   正是上一次写入的 `last_reinforced_at`。把它一起并发，同一条记忆就会在**同一次注入内**
   被多条 query 各强化一次，正是冷却窗口当初要消灭的写放大。

3. **单条 query 失败不连坐**。改造前 N 条检索写在一个 `for` 里、外面套一个大 `try`，
   第二条抛异常会把第一条已经拿到的记忆一起丢掉；记忆注入是风味不是功能，读路径
   fail-open 是硬要求。

4. **规划模型调用有超时**。这一跳打在主 LLM 之前，上游挂住就等于这条消息永远没有回复。
   超时后回退规则检索，并且日志要说清**回退的理由是超时**（而不是与"模型返回了非法 JSON"
   共用一句话——那样兜底分支的信息量就是零）。

外加一条接线契约：`_inject_memory_context` 真的走批量入口。零件全对而调用点没换过去时，
上面四条会全绿而线上一点没变。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import ModelConfigGroup, config
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.models.db_mem_reinforcement_log import DBMemReinforcementLog
from nekro_agent.services.agent.templates import history as history_module
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.services.memory.instance_scope import MemoryScope, resolve_allowed_instance_keys
from nekro_agent.services.memory.recall_contract import (
    MemoryRecallPlan,
    MemoryRecallQuerySpec,
)
from nekro_agent.services.memory.retriever import (
    BatchRecallRequest,
    MemoryRetriever,
    RetrievedMemory,
    retrieve_memories,
    retrieve_memories_batch,
)

WORKSPACE_ID = 4301
MEMORY_MODELS = [
    "nekro_agent.models.db_mem_paragraph",
    "nekro_agent.models.db_mem_entity",
    "nekro_agent.models.db_mem_relation",
    "nekro_agent.models.db_mem_episode",
    "nekro_agent.models.db_mem_reinforcement_log",
    # 域解析器要读实例组绑定表。**不要**把它替换成假存储：
    # 「零配置 = 空表 = 与改造前逐字相同」正是这里要保护的性质。
    "nekro_agent.models.db_instance_group",
]

QUERIES = ("q1", "q2", "q3")


class _StubChannel:
    """`_inject_memory_context` 只需要频道提供域与人设这两样（`identity_scope.ChannelLike`）。

    人设直接给定值而不是查库：本文件测的是**接线**，人设那条回退链自己的回归在
    `tests/test_identity_scope_foundation.py`。
    """

    def __init__(self, instance_key: str, *, adapter_key: str = "onebot_v11", preset_id: int = 0) -> None:
        self.adapter_key = adapter_key
        self.instance_key = instance_key
        self._preset_id = preset_id

    async def get_effective_preset_id(self) -> int:
        return self._preset_id


@pytest.fixture
def warning_logs():
    """收集 WARNING 及以上的日志正文。

    产品代码用的是 loguru，它不经过 stdlib logging，`caplog` 恒为空 —— 拿它断言"日志里
    有某句话"会永远通过，等于没断言。
    """
    from loguru import logger

    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(message.record["message"]), level="WARNING")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


@pytest.fixture
async def memory_db():
    """真表跑检索与强化写库。

    第 2 条性质要断言的正是"库里被写了几次"，用假存储验证等于把要保护的东西替换掉。
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": MEMORY_MODELS})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# 每条 query 对应一个可识别的查询向量，让向量通道替身能反查出自己正在服务哪一条。
_QUERY_MARKER = {query: float(index + 1) for index, query in enumerate(QUERIES)}
_MARKER_QUERY = {marker: query for query, marker in _QUERY_MARKER.items()}


class _ScriptedQdrant:
    """按段落 ID 返回固定相似度的向量通道替身，并把每次调用记进事件日志。

    故障注入放在这一层而**不是** `embed_text`：`MemoryRetriever.retrieve` 自己就把
    向量化异常收成一个空结果（`except Exception -> RetrievalResult(memories=[])`），
    从那里注入的异常根本到不了批量入口，拿它验"批量入口的失败隔离"等于什么都没验
    —— 实测把 `return_exceptions` 改成 `False`，那种写法下 18 条用例全绿。
    向量搜索这一跳没有任何 try，异常会真的冒到 `asyncio.gather`。
    """

    def __init__(self, scores: dict[int, float], events: list[str], failing_queries: frozenset[str]) -> None:
        self.scores = scores
        self.events = events
        self.failing_queries = failing_queries

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        query_name = _MARKER_QUERY.get(kwargs["query_vector"][0], "?")
        self.events.append(f"search:enter:{query_name}")
        await asyncio.sleep(0)
        self.events.append(f"search:leave:{query_name}")
        if query_name in self.failing_queries:
            raise RuntimeError(f"qdrant unreachable for {query_name}")
        threshold = float(kwargs.get("score_threshold", 0.0))
        return [
            {"id": paragraph_id, "score": score, "payload": {}}
            for paragraph_id, score in sorted(self.scores.items(), key=lambda item: -item[1])
            if score >= threshold
        ]


def _install_memory_fakes(
    monkeypatch: pytest.MonkeyPatch,
    scores: dict[int, float],
    events: list[str],
    failing_queries: frozenset[str] = frozenset(),
) -> None:
    """装上向量通道替身。

    `embed_text` 在 start / end 之间**恰好让出一次事件循环**：串行实现下日志必然是
    `start1,end1,start2,end2,...`，并发实现下必然是 `start1,start2,...,end1,end2,...`。
    两者都由 asyncio 就绪队列的 FIFO 语义唯一确定，不依赖任何真实耗时。
    """

    async def fake_embed(text: str) -> list[float]:
        events.append(f"embed:start:{text}")
        await asyncio.sleep(0)
        events.append(f"embed:end:{text}")
        return [_QUERY_MARKER.get(text, 0.0), 0.0, 0.0, 0.0]

    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.5)
    monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
    monkeypatch.setattr(
        retriever_module,
        "memory_qdrant_manager",
        _ScriptedQdrant(scores, events, failing_queries),
    )


async def _create_paragraph(content: str) -> DBMemParagraph:
    return await DBMemParagraph.create(
        workspace_id=WORKSPACE_ID,
        instance_key="",
        memory_source="cc",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content=content,
        summary=content,
        half_life_seconds=30 * 24 * 3600,
        origin_kind=OriginKind.TASK,
        origin_chat_key="__test__",
    )


def _embed_events(events: list[str]) -> list[str]:
    return [event for event in events if event.startswith("embed:")]


def _retrieved(target_id: int, content: str) -> RetrievedMemory:
    """构造一条可被 `compile_memories_for_context` 编排出来的段落候选。"""
    return RetrievedMemory(
        target_id=target_id,
        source_type="paragraph",
        episode_id=None,
        paragraph_id=target_id,
        relation_id=None,
        content=content,
        summary="",
        cognitive_type=CognitiveType.SEMANTIC.value,
        knowledge_type=KnowledgeType.FACT.value,
        similarity_score=0.9,
        effective_weight=1.0,
        event_time=None,
        origin_chat_key="__test__",
    )


# --------------------------------------------------------------------------------------
# 1. N 条 query 并发（含证明断言测得出差异的对照组）
# --------------------------------------------------------------------------------------


class TestBatchRetrievalIsConcurrent:
    """批量检索必须并发，且对照组证明这条断言能区分串并行。"""

    @pytest.mark.asyncio
    async def test_batch_interleaves_all_embeddings(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paragraph = await _create_paragraph("并发检索")
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events)

        await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query=query, limit=5) for query in QUERIES],
            scope=MemoryScope.unrestricted(),
        )

        assert _embed_events(events) == [
            "embed:start:q1",
            "embed:start:q2",
            "embed:start:q3",
            "embed:end:q1",
            "embed:end:q2",
            "embed:end:q3",
        ]

    @pytest.mark.asyncio
    async def test_serial_loop_does_not_interleave(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """对照组：改造前那种 `for` 循环逐条 await 的写法在同一批输入下必然不交错。

        没有这一条，上面那条断言可能只是"这批输入下两种实现无法区分"。
        """
        paragraph = await _create_paragraph("串行检索")
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events)

        for query in QUERIES:
            await retrieve_memories(
                workspace_id=WORKSPACE_ID,
                query=query,
                limit=5,
                scope=MemoryScope.unrestricted(),
            )

        assert _embed_events(events) == [
            "embed:start:q1",
            "embed:end:q1",
            "embed:start:q2",
            "embed:end:q2",
            "embed:start:q3",
            "embed:end:q3",
        ]

    @pytest.mark.asyncio
    async def test_results_keep_request_order(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """并发之后结果必须按**输入顺序**返回，否则调用方的逐条聚合会张冠李戴。"""
        paragraph = await _create_paragraph("顺序")
        events: list[str] = []
        # q2 的 embedding 失败 -> 该条为空；q1/q3 正常。位置能唯一指认是哪一条空了。
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events, failing_queries=frozenset({"q2"}))

        results = await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query=query, limit=5) for query in QUERIES],
            scope=MemoryScope.unrestricted(),
        )

        assert [len(memories) for memories in results] == [1, 0, 1]

    @pytest.mark.asyncio
    async def test_empty_requests_is_a_noop(self, memory_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {}, events)
        assert await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[],
            scope=MemoryScope.unrestricted(),
        ) == []
        assert events == []


# --------------------------------------------------------------------------------------
# 2. 强化写库仍然串行、且冷却窗口仍然成立
# --------------------------------------------------------------------------------------


class TestReinforcementStaysSerialAfterRetrieval:
    """强化是读改写 + 冷却窗口判定，并发执行会在一次注入内重复强化同一条记忆。"""

    @pytest.mark.asyncio
    async def test_same_paragraph_hit_by_every_query_is_reinforced_once(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paragraph = await _create_paragraph("被三条 query 同时命中")
        weight_before = paragraph.base_weight
        boost = 0.3
        monkeypatch.setattr(config, "MEMORY_RETRIEVAL_REINFORCE_BOOST", boost)
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events)

        await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query=query, limit=5) for query in QUERIES],
            scope=MemoryScope.unrestricted(),
        )

        refreshed = await DBMemParagraph.get(id=paragraph.id)
        assert refreshed.base_weight == pytest.approx(weight_before + boost)

        logs = await DBMemReinforcementLog.filter(target_id=paragraph.id).order_by("id")
        # 三条 query 各留一行访问记录，但只有第一行真的带 boost，后两行被冷却窗口挡成 0。
        #
        # 判定必须落在这一行而不是上面的 base_weight：并发写库时三次读改写会读到同一个旧值，
        # base_weight 照样只涨一次（lost update），上面那条断言在坏实现下也成立。
        # 实测把 `record_access` 放回并发路径，这里拿到的是 [0.3, 0.3, 0.3] ——
        # 日志说强化了三次，数据只落了一次。
        assert [log.boost_amount for log in logs] == [boost, 0.0, 0.0]

    @pytest.mark.asyncio
    async def test_no_write_starts_before_every_retrieval_finished(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """全部检索 I/O 结束之后才允许出现第一次强化写库，且写库按输入顺序进行。

        **这条比它看上去弱**：实测把 `record_access` 放回并发路径（即上一条用例杀掉的那个变异）
        时它照样通过 —— `_record_access` 在 `retrieve()` 里排在很多个 await 之后，三条 query
        并发时天然也是"搜完了才轮到写"。真正判定写放大的是上一条用例的强化日志计数。
        这条钉的是另一件事：批量入口今后要是被改成"边检索边写"（例如换成
        `as_completed` 流式处理），这里会红。
        """
        paragraph = await _create_paragraph("写序")
        monkeypatch.setattr(config, "MEMORY_RETRIEVAL_REINFORCE_BOOST", 0.3)
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events)

        original_log = DBMemReinforcementLog.log_reinforcement

        async def spying_log(**kwargs: Any) -> Any:
            events.append(f"reinforce-log:{kwargs['trigger_ref']}")
            return await original_log(**kwargs)

        monkeypatch.setattr(DBMemReinforcementLog, "log_reinforcement", spying_log)

        await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query=query, limit=5) for query in QUERIES],
            scope=MemoryScope.unrestricted(),
        )

        write_events = [event for event in events if event.startswith("reinforce-log:")]
        assert write_events == ["reinforce-log:q1", "reinforce-log:q2", "reinforce-log:q3"]

        last_retrieval_index = max(
            index for index, event in enumerate(events) if event.startswith(("embed:", "search:"))
        )
        first_write_index = min(index for index, event in enumerate(events) if event.startswith("reinforce-log:"))
        assert first_write_index > last_retrieval_index


# --------------------------------------------------------------------------------------
# 3. 单条失败不连坐
# --------------------------------------------------------------------------------------


class TestBatchFailureIsolation:
    @pytest.mark.asyncio
    async def test_one_failing_query_does_not_discard_the_others(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paragraph = await _create_paragraph("失败隔离")
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events, failing_queries=frozenset({"q1"}))

        results = await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query=query, limit=5) for query in QUERIES],
            scope=MemoryScope.unrestricted(),
        )

        assert [len(memories) for memories in results] == [0, 1, 1]
        assert [memory.target_id for memory in results[1]] == [paragraph.id]

    @pytest.mark.asyncio
    async def test_cancellation_is_not_downgraded_into_an_empty_result(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """取消不是"这条检索失败了"，降级成空列表会让取消信号凭空消失。"""
        paragraph = await _create_paragraph("取消")
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events)

        original_retrieve = MemoryRetriever.retrieve

        async def cancelling_retrieve(self: MemoryRetriever, query: str, **kwargs: Any) -> Any:
            if query == "q2":
                raise asyncio.CancelledError
            return await original_retrieve(self, query, **kwargs)

        monkeypatch.setattr(MemoryRetriever, "retrieve", cancelling_retrieve)

        with pytest.raises(asyncio.CancelledError):
            await retrieve_memories_batch(
                workspace_id=WORKSPACE_ID,
                requests=[BatchRecallRequest(query=query, limit=5) for query in QUERIES],
                scope=MemoryScope.unrestricted(),
            )

    @pytest.mark.asyncio
    async def test_every_query_failing_yields_empty_lists_not_an_exception(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paragraph = await _create_paragraph("全失败")
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events, failing_queries=frozenset(QUERIES))

        results = await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query=query, limit=5) for query in QUERIES],
            scope=MemoryScope.unrestricted(),
        )
        assert results == [[], [], []]


# --------------------------------------------------------------------------------------
# 4. 规划模型调用的超时
# --------------------------------------------------------------------------------------


def _plan_json() -> str:
    return json.dumps(
        {
            "intent_type": "mixed",
            "answer_style": "core_plus_evidence",
            "queries": [{"query_text": "上次的部署决定", "focus_text": "部署"}],
        },
    )


class _FakeLLMResponse:
    def __init__(self, content: str) -> None:
        self.response_content = content


def _install_planner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    behavior: str,
    timeout: float,
) -> list[str]:
    """把规划模型换成剧本，返回调用轨迹。"""
    import nekro_agent.services.agent.openai as openai_module

    trace: list[str] = []

    async def fake_gen(**_kwargs: Any) -> _FakeLLMResponse:
        trace.append("planner:enter")
        if behavior == "hang":
            # 上游挂住：不是慢，是永远不返回。没有超时的话这条消息就永远没有回复。
            await asyncio.Event().wait()
        trace.append("planner:done")
        return _FakeLLMResponse(_plan_json())

    monkeypatch.setattr(config, "MEMORY_ENABLE_ENHANCED_RETRIEVAL", True)
    monkeypatch.setattr(config, "MEMORY_ENHANCED_RETRIEVAL_MODEL_GROUP", "planner")
    monkeypatch.setattr(config, "MEMORY_ENHANCED_RETRIEVAL_TIMEOUT_SECONDS", timeout)
    monkeypatch.setattr(
        config,
        "MODEL_GROUPS",
        {"planner": ModelConfigGroup(CHAT_MODEL="m", BASE_URL="http://x", API_KEY="k")},
    )
    monkeypatch.setattr(openai_module, "gen_openai_chat_response", fake_gen)
    return trace


class _Message:
    """`_build_enhanced_memory_recall_plan` 只读这四个字段。"""

    def __init__(self, text: str) -> None:
        self.content_text = text
        self.sender_nickname = "小明"
        self.sender_name = "xiaoming"
        self.sender_id = "10001"


class TestEnhancedPlanTimeout:
    @pytest.mark.asyncio
    async def test_hanging_planner_falls_back_within_the_budget(
        self,
        monkeypatch: pytest.MonkeyPatch,
        warning_logs: list[str],
    ) -> None:
        trace = _install_planner(monkeypatch, behavior="hang", timeout=0.05)

        # 外层这道时限只是护栏：产品代码没有自己的超时时，这条用例会以失败收场而不是把
        # 整个测试进程挂死（护栏比被测时限宽两个量级，不参与判定）。
        plan = await asyncio.wait_for(
            history_module._build_enhanced_memory_recall_plan([_Message("上次是怎么决定的")]),  # type: ignore[arg-type]
            timeout=5.0,
        )

        assert plan is None
        assert trace == ["planner:enter"]
        # 兜底结果本身信息量为零：必须一并断言兜底的**理由**是超时，
        # 而不是与"模型返回了非法 JSON"共用一句话
        assert len(warning_logs) == 1
        assert "超时" in warning_logs[0]
        assert "回退规则检索" in warning_logs[0]

    @pytest.mark.asyncio
    async def test_invalid_json_reports_a_different_reason(
        self,
        monkeypatch: pytest.MonkeyPatch,
        warning_logs: list[str],
    ) -> None:
        """规划失败的两种理由必须能在日志上分开，否则运维只知道"回退了"、不知道为什么。"""
        _install_planner(monkeypatch, behavior="ok", timeout=5.0)
        import nekro_agent.services.agent.openai as openai_module

        async def broken_gen(**_kwargs: Any) -> _FakeLLMResponse:
            return _FakeLLMResponse("not json at all")

        monkeypatch.setattr(openai_module, "gen_openai_chat_response", broken_gen)

        assert await history_module._build_enhanced_memory_recall_plan([_Message("上次是怎么决定的")]) is None  # type: ignore[arg-type]
        assert len(warning_logs) == 1
        assert "解析失败" in warning_logs[0]
        assert "超时" not in warning_logs[0]

    @pytest.mark.asyncio
    async def test_responsive_planner_is_used_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        trace = _install_planner(monkeypatch, behavior="ok", timeout=5.0)

        plan = await history_module._build_enhanced_memory_recall_plan([_Message("上次是怎么决定的")])  # type: ignore[arg-type]

        assert plan is not None
        assert [query.query_text for query in plan.queries] == ["上次的部署决定"]
        assert trace == ["planner:enter", "planner:done"]

    @pytest.mark.asyncio
    async def test_zero_timeout_means_no_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """填 0 逐字回到改造前：不加任何时限。"""
        trace = _install_planner(monkeypatch, behavior="hang", timeout=0.0)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                history_module._build_enhanced_memory_recall_plan([_Message("上次是怎么决定的")]),  # type: ignore[arg-type]
                timeout=0.05,
            )
        assert trace == ["planner:enter"]

    @pytest.mark.asyncio
    async def test_disabled_switch_never_calls_the_planner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        trace = _install_planner(monkeypatch, behavior="ok", timeout=5.0)
        monkeypatch.setattr(config, "MEMORY_ENABLE_ENHANCED_RETRIEVAL", False)

        assert await history_module._build_enhanced_memory_recall_plan([_Message("上次是怎么决定的")]) is None  # type: ignore[arg-type]
        assert trace == []


# --------------------------------------------------------------------------------------
# 5. 接线契约：注入链路真的走批量入口
# --------------------------------------------------------------------------------------


class TestInjectionUsesBatchEntryPoint:
    """零件全对而调用点没换过去时，上面四条会全绿而线上一点没变。"""

    @pytest.mark.asyncio
    async def test_inject_issues_exactly_one_batch_call_with_ordered_requests(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: list[list[BatchRecallRequest]] = []

        async def fake_batch(
            workspace_id: int,
            requests: Any,
            *,
            scope: Any = None,
            record_access: bool = True,
        ) -> list[list[Any]]:
            captured.append(list(requests))
            return [[] for _ in requests]

        async def fake_plan(_messages: Any) -> MemoryRecallPlan:
            return MemoryRecallPlan(
                queries=[
                    MemoryRecallQuerySpec(query_text="第一条"),
                    MemoryRecallQuerySpec(query_text="第二条"),
                    MemoryRecallQuerySpec(query_text="第三条"),
                ],
            )

        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_batch)
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)

        result = await history_module._inject_memory_context(
            workspace_id=WORKSPACE_ID,
            recent_messages=[],
            channel=_StubChannel(""),
            one_time_code="test-code",
            max_memories=8,
        )

        assert result == ""
        assert len(captured) == 1, "三条 query 必须合成一次批量调用，而不是三次串行检索"
        assert [request.query for request in captured[0]] == ["第一条", "第二条", "第三条"]
        assert [request.limit for request in captured[0]] == [8, 8, 8]

    @pytest.mark.asyncio
    async def test_serial_entry_point_is_no_longer_reachable_from_injection(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """逐条入口 `retrieve_memories` 不再出现在注入链路上。

        只断言"批量入口被调用过"挡不住"两个入口都在跑"这种半截改造。
        """
        calls: list[str] = []

        async def fake_batch(
            workspace_id: int,
            requests: Any,
            *,
            scope: Any = None,
            record_access: bool = True,
        ) -> list[list[Any]]:
            calls.append("batch")
            return [[] for _ in requests]

        async def fake_single(**_kwargs: Any) -> list[Any]:
            calls.append("single")
            return []

        async def fake_plan(_messages: Any) -> MemoryRecallPlan:
            return MemoryRecallPlan(queries=[MemoryRecallQuerySpec(query_text="唯一一条")])

        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_batch)
        monkeypatch.setattr(retriever_module, "retrieve_memories", fake_single)
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)

        await history_module._inject_memory_context(
            workspace_id=WORKSPACE_ID,
            recent_messages=[],
            channel=_StubChannel(""),
            one_time_code="test-code",
            max_memories=8,
        )

        assert calls == ["batch"]


# --------------------------------------------------------------------------------------
# 6. 未改动的默认部署：单条 query 的行为逐字不变
# --------------------------------------------------------------------------------------


class TestSingleQueryPathUnchanged:
    """默认部署（增强检索关闭）只有一条 query，批量路径必须与改造前逐字等价。"""

    @pytest.mark.asyncio
    async def test_single_request_still_records_access(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        paragraph = await _create_paragraph("单条")
        weight_before = paragraph.base_weight
        monkeypatch.setattr(config, "MEMORY_RETRIEVAL_REINFORCE_BOOST", 0.3)
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events)

        results = await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query="only", limit=5)],
            scope=MemoryScope.unrestricted(),
        )

        assert [memory.target_id for memory in results[0]] == [paragraph.id]
        refreshed = await DBMemParagraph.get(id=paragraph.id)
        assert refreshed.base_weight == pytest.approx(weight_before + 0.3)
        assert await DBMemReinforcementLog.filter(target_id=paragraph.id).count() == 1

    @pytest.mark.asyncio
    async def test_instance_filter_is_forwarded(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """实例隔离条件必须原样传到每一个检索器，否则批量入口会成为一条越权召回路径。"""
        paragraph = await _create_paragraph("隔离")
        await DBMemParagraph.filter(id=paragraph.id).update(instance_key="inst_a")
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events)

        mine = await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query="q", limit=5)],
            scope=MemoryScope(allowed_instance_keys=["inst_a"], preset_id=None),
        )
        others = await retrieve_memories_batch(
            workspace_id=WORKSPACE_ID,
            requests=[BatchRecallRequest(query="q", limit=5)],
            scope=MemoryScope(allowed_instance_keys=["inst_b"], preset_id=None),
        )

        assert [memory.target_id for memory in mine[0]] == [paragraph.id]
        assert others[0] == []

    @pytest.mark.asyncio
    async def test_single_entry_point_still_records_access_by_default(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`retrieve()` 的 `record_access` 默认值必须仍是 True。

        批量入口把强化改成了"检索时关掉、事后补记"。**另一半**是所有没走批量入口的调用方
        —— `query_service.search_workspace_memories`（memory_tools 插件）、
        `routers/workspaces._build_cc_memory_handshake`——它们靠的正是这个默认值。
        把默认值翻成 False 来"简化"批量入口，这些路径会静默停止强化：记忆只衰减不续命，
        低于清理阈值后被停用删向量，而没有任何一条日志会提到这件事。

        实测：把 `record_access: bool = True` 改成 `False`，改本条之前的 19 条用例全绿。
        """
        paragraph = await _create_paragraph("默认记账")
        weight_before = paragraph.base_weight
        monkeypatch.setattr(config, "MEMORY_RETRIEVAL_REINFORCE_BOOST", 0.3)
        events: list[str] = []
        _install_memory_fakes(monkeypatch, {paragraph.id: 0.9}, events)

        memories = await retrieve_memories(
            workspace_id=WORKSPACE_ID,
            query="q1",
            limit=5,
            scope=MemoryScope.unrestricted(),
        )

        assert [memory.target_id for memory in memories] == [paragraph.id]
        refreshed = await DBMemParagraph.get(id=paragraph.id)
        assert refreshed.base_weight == pytest.approx(weight_before + 0.3)
        assert await DBMemReinforcementLog.filter(target_id=paragraph.id).count() == 1


# --------------------------------------------------------------------------------------
# 7. 接线契约（第二批）：作用域、时间窗、逐条配对，以及"记忆真的进了提示词"
# --------------------------------------------------------------------------------------


class TestInjectionCallSiteContract:
    """调用点这根线自己的回归。

    §5 那两条只钉住了"批量入口被调用了一次、query 与 limit 对"。对抗式复核实测：把
    `scope=` 从调用点删掉、把 `time_from/time_to` 从 `BatchRecallRequest`
    里删掉、把结果与 query 配错位、甚至让聚合循环一条记忆都不收（整条注入链路空转），
    改本节之前的 19 条用例**全绿**。零件测试挡不住接线回归。
    """

    @pytest.mark.asyncio
    async def test_other_instance_memories_never_reach_the_prompt(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """端到端走**真实**批量入口 + 真表：别的实例的私密记忆不能出现在本实例的提示词里。

        这里刻意不拿替身拦 `retrieve_memories_batch` —— 要保护的正是"调用点把作用域
        传下去"这根线，把它替换掉等于先删掉被测路径再宣布它是对的。
        调用点少传一个 `scope=`，过滤条件就整个消失。
        """
        mine = await _create_paragraph("这是甲实例的私密内容")
        await DBMemParagraph.filter(id=mine.id).update(instance_key="inst_a")
        theirs = await _create_paragraph("这是乙实例的私密内容")
        await DBMemParagraph.filter(id=theirs.id).update(instance_key="inst_b")

        events: list[str] = []
        _install_memory_fakes(monkeypatch, {mine.id: 0.9, theirs.id: 0.9}, events)

        async def fake_plan(_messages: Any) -> MemoryRecallPlan:
            return MemoryRecallPlan(queries=[MemoryRecallQuerySpec(query_text="私密内容")])

        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)

        context = await history_module._inject_memory_context(
            workspace_id=WORKSPACE_ID,
            recent_messages=[],
            channel=_StubChannel("inst_a"),
            one_time_code="test-code",
        )

        # 正例先立住：注入块必须真的有东西，否则下一条"不含乙实例"会因为全空而恒成立
        assert "这是甲实例的私密内容" in context
        assert "这是乙实例的私密内容" not in context

    @pytest.mark.asyncio
    async def test_instance_scope_and_time_window_reach_the_batch_entry_point(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """实例作用域与每条 query 各自的时间窗都必须原样传到批量入口。"""
        captured: dict[str, Any] = {}
        time_from = datetime(2026, 1, 2, tzinfo=timezone.utc)
        time_to = datetime(2026, 1, 3, tzinfo=timezone.utc)

        async def fake_batch(
            workspace_id: int,
            requests: Any,
            *,
            scope: Any = None,
            record_access: bool = True,
        ) -> list[list[Any]]:
            captured["scope"] = scope
            captured["requests"] = list(requests)
            return [[] for _ in requests]

        async def fake_plan(_messages: Any) -> MemoryRecallPlan:
            return MemoryRecallPlan(
                queries=[
                    MemoryRecallQuerySpec(query_text="带时间窗", time_from=time_from, time_to=time_to),
                    MemoryRecallQuerySpec(query_text="不带时间窗"),
                ],
            )

        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_batch)
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)

        await history_module._inject_memory_context(
            workspace_id=WORKSPACE_ID,
            recent_messages=[],
            channel=_StubChannel("inst_a"),
            one_time_code="test-code",
        )

        # 两条断言缺一不可：右边那条证明调用点确实走了域解析器（零配置下它逐字等于旧函数），
        # 左边的字面量证明结果不是 None（None 就是"不施加任何实例过滤"，也就是泄露）
        assert captured["scope"].allowed_instance_keys == ["inst_a", ""]
        assert captured["scope"].allowed_instance_keys == resolve_allowed_instance_keys("inst_a")
        assert [(request.time_from, request.time_to) for request in captured["requests"]] == [
            (time_from, time_to),
            (None, None),
        ]

    @pytest.mark.asyncio
    async def test_preset_dimension_also_reaches_the_batch_entry_point(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """人设那一维也必须原样传到批量入口——上一条只盯着实例维。

        这里是自动注入，是记忆泄漏面最大的那个消费点（每一轮提示词都走它）。
        只断言实例维的话，"调用点把 `preset_id` 抹成 `None`"这种**半边掉线**恒绿：
        域过滤照常成立、结果照常返回，只是把上一个人格的记忆一起念出来了。
        实测：把这里改成 `resolve_memory_scope(channel)._replace(preset_id=None)`，
        本文件与 `test_memory_preset_scope.py` 全绿——这条用例就是为了让它变红。
        """
        captured: dict[str, Any] = {}

        async def fake_batch(
            workspace_id: int,
            requests: Any,
            *,
            scope: Any = None,
            record_access: bool = True,
        ) -> list[list[Any]]:
            captured["scope"] = scope
            return [[] for _ in requests]

        async def fake_plan(_messages: Any) -> MemoryRecallPlan:
            return MemoryRecallPlan(queries=[MemoryRecallQuerySpec(query_text="唯一一条")])

        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_batch)
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)

        await history_module._inject_memory_context(
            workspace_id=WORKSPACE_ID,
            recent_messages=[],
            channel=_StubChannel("inst_a", preset_id=42),
            one_time_code="test-code",
        )

        # 断言落在**真的传下去的那个值**上：`None` 表示"这一维不过滤"，也就是泄露
        assert captured["scope"].preset_id == 42
        # 过滤条件真的展开了这一维（`as_filter()` 是四张表共用的那一份）
        assert captured["scope"].as_filter()["preset_id__in"][0] == 42

    @pytest.mark.asyncio
    async def test_single_instance_deployment_still_sends_no_instance_filter(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """未多实例化的部署逐字不变：instance_key 为空时不附加任何实例过滤条件。"""
        captured: dict[str, Any] = {}

        async def fake_batch(
            workspace_id: int,
            requests: Any,
            *,
            scope: Any = None,
            record_access: bool = True,
        ) -> list[list[Any]]:
            captured["scope"] = scope
            return [[] for _ in requests]

        async def fake_plan(_messages: Any) -> MemoryRecallPlan:
            return MemoryRecallPlan(queries=[MemoryRecallQuerySpec(query_text="唯一一条")])

        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_batch)
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)

        await history_module._inject_memory_context(
            workspace_id=WORKSPACE_ID,
            recent_messages=[],
            channel=_StubChannel(""),
            one_time_code="test-code",
        )

        assert captured["scope"].allowed_instance_keys is None

    @pytest.mark.asyncio
    async def test_each_query_importance_lands_on_its_own_results(
        self,
        memory_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """第 i 条 query 的 importance 必须落在第 i 条 query 的结果上，且结果真的进注入块。

        改造把"检索"从聚合循环里提了出去，逐条配对从此依赖 `zip` 的顺序。配错位不会抛异常
        也不会少一条记忆 —— 只会把加权系数张冠李戴，线上表现为"记忆召回了但排序莫名其妙"。
        """
        boosted = _retrieved(901, "应当被放大的一条")
        damped = _retrieved(902, "应当被压低的一条")

        async def fake_batch(
            workspace_id: int,
            requests: Any,
            *,
            scope: Any = None,
            record_access: bool = True,
        ) -> list[list[Any]]:
            return [[boosted], [damped]]

        async def fake_plan(_messages: Any) -> MemoryRecallPlan:
            return MemoryRecallPlan(
                queries=[
                    MemoryRecallQuerySpec(query_text="重要的那条", importance=2.0),
                    MemoryRecallQuerySpec(query_text="次要的那条", importance=0.5),
                ],
            )

        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(retriever_module, "retrieve_memories_batch", fake_batch)
        monkeypatch.setattr(history_module, "_build_memory_recall_plan", fake_plan)

        context = await history_module._inject_memory_context(
            workspace_id=WORKSPACE_ID,
            recent_messages=[],
            channel=_StubChannel(""),
            one_time_code="test-code",
        )

        assert boosted.effective_weight == pytest.approx(2.0)
        assert damped.effective_weight == pytest.approx(0.5)
        # 聚合循环真的把两条都收进去了：少了这两条，"整条注入链路空转"也测不出来
        assert "应当被放大的一条" in context
        assert "应当被压低的一条" in context
