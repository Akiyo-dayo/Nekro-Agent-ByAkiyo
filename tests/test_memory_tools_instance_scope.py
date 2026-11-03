"""`memory_tools` 插件的实例作用域回归。

记忆默认按实例隔离，允许配置共享（共享组模式）。这条不变式在
**自动注入**那一侧由 `_inject_memory_context` 执行：它拿 `instance_scope.resolve_memory_scope`
把当前频道换算成 `(域, 人设)` 的可见范围，再传给检索器。

同一个工作区的记忆还有**第二个出口**：`memory_tools` 插件的三个沙盒方法。它们跑在
某个具体频道的这一轮 Agent 里，产出直接进模型上下文、再变成发出去的回复——与自动注入
是同一份数据、同一个模型、同一个聊天窗口。改造前这三个出口**一个都没有做实例过滤**，
只按 `workspace_id` 过滤：

- 一个工作区可以绑定多个频道（`WorkspaceService.bind_channel` 是一对多），
- 于是实例 B 的 Agent 只要被问一句"之前聊过什么"，就能把实例 A 的私密记忆检索出来念出来；
- 更进一步，`memory_id` 是自增整数，`get_memory_detail_tool` / `trace_memory_origin_tool`
  可以直接按 ID 取任意一条，连检索都不用。

本文件钉住三个出口都按实例过滤，且**单实例部署逐字不变**（`instance_key` 为空、
没有实例组时域解析器返回 None，不附加任何实例条件）。
人设维那一半在 `tests/test_memory_preset_scope.py`。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_mem_paragraph import (
    CognitiveType,
    DBMemParagraph,
    KnowledgeType,
    OriginKind,
)
from nekro_agent.services.memory import retriever as retriever_module
from nekro_agent.tools.untrusted_content import UntrustedToolResult
from plugins.builtin.memory_tools import main as _memory_tools_module


class _BodyOnly:
    """三个工具现在返回 `UntrustedToolResult`（记忆是从群聊里提炼的，写的人是群里的任何人）。

    本文件关心的是**作用域**（返回里有没有别的实例的记忆），所以这里把结果拆回正文 JSON，
    原有断言一条不动；"结果必须是不可信包裹"由 `test_agent_results_untrusted.py` 钉。
    """

    def __getattr__(self, name: str):
        tool = getattr(_memory_tools_module, name)

        async def call(*args, **kwargs):
            result = await tool(*args, **kwargs)
            assert isinstance(result, UntrustedToolResult)
            return result.body

        return call


memory_tools = _BodyOnly()

WORKSPACE_ID = 7701
MEMORY_MODELS = [
    # 域解析器要读实例组绑定表；零配置时它是空表，行为与改造前逐字相同。
    "nekro_agent.models.db_instance_group",
    "nekro_agent.models.db_mem_paragraph",
    "nekro_agent.models.db_mem_entity",
    "nekro_agent.models.db_mem_relation",
    "nekro_agent.models.db_mem_episode",
    "nekro_agent.models.db_mem_reinforcement_log",
]

MINE = "这是甲实例的私密内容"
THEIRS = "这是乙实例的私密内容"


@pytest.fixture
async def memory_db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": MEMORY_MODELS})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


class _StubWorkspace:
    def __init__(self, workspace_id: int) -> None:
        self.id = workspace_id


class _StubChannel:
    """`identity_scope.ChannelLike`：域两列 + 人设那条回退链的出口。"""

    def __init__(self, instance_key: str, *, adapter_key: str = "onebot_v11", preset_id: int = 0) -> None:
        self.adapter_key = adapter_key
        self.instance_key = instance_key
        self._preset_id = preset_id

    async def get_effective_preset_id(self) -> int:
        return self._preset_id


class _StubCtx:
    """只实现 `memory_tools` 用到的两个口子：绑定的工作区与当前频道。

    这里没有用 spy 替换 `search_workspace_memories` 之类的被测函数——要保护的正是
    "插件把当前频道的实例作用域传下去"这根线，替换掉它等于先删除被测路径再宣布它是对的。
    """

    def __init__(self, instance_key: str, *, workspace_id: int = WORKSPACE_ID, preset_id: int = 0) -> None:
        self._workspace = _StubWorkspace(workspace_id)
        self.db_chat_channel = _StubChannel(instance_key, preset_id=preset_id)

    async def get_bound_workspace(self) -> _StubWorkspace:
        return self._workspace


class _ScriptedQdrant:
    """按段落 ID 返回固定相似度；实例过滤交给真实的 DB 侧条件去做。

    向量层故意**不**过滤：这样只要 SQL 侧漏了条件，别的实例的段落就会真的被捞出来，
    用例才是在测产品代码而不是在测替身。
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


async def _create_paragraph(content: str, instance_key: str, preset_id: int = 0) -> DBMemParagraph:
    paragraph = await DBMemParagraph.create(
        workspace_id=WORKSPACE_ID,
        instance_key=instance_key,
        preset_id=preset_id,
        memory_source="cc",
        cognitive_type=CognitiveType.SEMANTIC,
        knowledge_type=KnowledgeType.FACT,
        content=content,
        summary=content,
        half_life_seconds=30 * 24 * 3600,
        origin_kind=OriginKind.TASK,
        origin_chat_key="__test__",
    )
    # `instance_key` / `preset_id` 都有 default，create 之后统一回写一次，避免依赖字段默认值的写入时机
    await DBMemParagraph.filter(id=paragraph.id).update(instance_key=instance_key, preset_id=preset_id)
    return paragraph


@pytest.fixture
def two_instance_memories(monkeypatch: pytest.MonkeyPatch):
    async def fake_embed(_text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]

    monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.5)
    monkeypatch.setattr(config, "MEMORY_RETRIEVAL_REINFORCE_BOOST", 0.0)
    monkeypatch.setattr(retriever_module, "embed_text", fake_embed)

    async def build() -> tuple[DBMemParagraph, DBMemParagraph]:
        mine = await _create_paragraph(MINE, "inst_a")
        theirs = await _create_paragraph(THEIRS, "inst_b")
        monkeypatch.setattr(
            retriever_module,
            "memory_qdrant_manager",
            _ScriptedQdrant({mine.id: 0.9, theirs.id: 0.9}),
        )
        return mine, theirs

    return build


class TestSearchToolIsInstanceScoped:
    @pytest.mark.asyncio
    async def test_search_never_returns_another_instance_memory(
        self,
        memory_db: None,
        two_instance_memories: Any,
    ) -> None:
        await two_instance_memories()

        payload = await memory_tools.search_workspace_memories_tool(
            _StubCtx("inst_a"),  # type: ignore[arg-type]
            query="私密内容",
            limit=10,
        )

        # 正例先立住：没有它，"不含乙实例"会因为结果恒空而永远成立
        assert MINE in payload
        assert THEIRS not in payload
        assert json.loads(payload)["total"] == 1

    @pytest.mark.asyncio
    async def test_single_instance_deployment_is_unchanged(
        self,
        memory_db: None,
        two_instance_memories: Any,
    ) -> None:
        """未多实例化的部署逐字不变：`instance_key` 为空 => 不附加任何实例过滤。"""
        await two_instance_memories()

        payload = await memory_tools.search_workspace_memories_tool(
            _StubCtx(""),  # type: ignore[arg-type]
            query="私密内容",
            limit=10,
        )

        assert MINE in payload
        assert THEIRS in payload
        assert json.loads(payload)["total"] == 2


class TestDetailAndTraceToolsAreInstanceScoped:
    """按 ID 直取的两个出口。

    `memory_id` 是自增整数，模型可以直接猜。检索侧堵上而这两个口子不堵，等于只堵了一半。
    """

    @pytest.mark.asyncio
    async def test_detail_of_another_instance_memory_is_not_found(
        self,
        memory_db: None,
        two_instance_memories: Any,
    ) -> None:
        mine, theirs = await two_instance_memories()
        ctx = _StubCtx("inst_a")

        own = await memory_tools.get_memory_detail_tool(ctx, memory_type="paragraph", memory_id=mine.id)  # type: ignore[arg-type]
        assert MINE in own

        with pytest.raises(ValueError, match=str(theirs.id)):
            await memory_tools.get_memory_detail_tool(ctx, memory_type="paragraph", memory_id=theirs.id)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_trace_of_another_instance_memory_is_not_found(
        self,
        memory_db: None,
        two_instance_memories: Any,
    ) -> None:
        mine, theirs = await two_instance_memories()
        ctx = _StubCtx("inst_a")

        own = await memory_tools.trace_memory_origin_tool(ctx, memory_type="paragraph", memory_id=mine.id)  # type: ignore[arg-type]
        assert json.loads(own)["memory_id"] == mine.id

        with pytest.raises(ValueError, match=str(theirs.id)):
            await memory_tools.trace_memory_origin_tool(ctx, memory_type="paragraph", memory_id=theirs.id)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_single_instance_deployment_can_still_read_every_memory(
        self,
        memory_db: None,
        two_instance_memories: Any,
    ) -> None:
        _mine, theirs = await two_instance_memories()

        detail = await memory_tools.get_memory_detail_tool(
            _StubCtx(""),  # type: ignore[arg-type]
            memory_type="paragraph",
            memory_id=theirs.id,
        )
        assert THEIRS in detail


class TestTheThreeExitsCarryThePresetDimensionToo:
    """⚠️ 上面两组只盯着**域**这一维。

    `_require_bound_memory_scope` 一次产出 `(域, 人设)` 两维，三个出口共用它。
    只测域的话，"调用点把 `preset_id` 抹成 `None`"这种**半边掉线**恒绿：
    实例过滤照常成立、结果照常返回，只是把上一个人格的私密记忆一起念出来了。
    实测：把那一行改成 `(await resolve_memory_scope(channel))._replace(preset_id=None)`，
    本文件与 `test_memory_preset_scope.py` 曾经全绿——这一组就是为了让它变红。

    人设维本身的回归在 `tests/test_memory_preset_scope.py`；这里测的是**接线**，
    也就是插件有没有把那两维一起交出去。
    """

    @pytest.fixture
    def two_preset_memories(self, monkeypatch: pytest.MonkeyPatch):
        async def fake_embed(_text: str) -> list[float]:
            return [1.0, 0.0, 0.0, 0.0]

        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(config, "MEMORY_RETRIEVAL_MIN_SIMILARITY", 0.5)
        monkeypatch.setattr(config, "MEMORY_RETRIEVAL_REINFORCE_BOOST", 0.0)
        monkeypatch.setattr(retriever_module, "embed_text", fake_embed)

        async def build() -> tuple[DBMemParagraph, DBMemParagraph]:
            # 同一个实例、同一个工作区，只有人设不同
            mine = await _create_paragraph(MINE, "inst_a", preset_id=1)
            theirs = await _create_paragraph(THEIRS, "inst_a", preset_id=2)
            monkeypatch.setattr(
                retriever_module,
                "memory_qdrant_manager",
                _ScriptedQdrant({mine.id: 0.9, theirs.id: 0.9}),
            )
            return mine, theirs

        return build

    @pytest.mark.asyncio
    async def test_search_never_returns_another_presets_memory(
        self,
        memory_db: None,
        two_preset_memories: Any,
    ) -> None:
        await two_preset_memories()

        payload = await memory_tools.search_workspace_memories_tool(
            _StubCtx("inst_a", preset_id=1),  # type: ignore[arg-type]
            query="私密内容",
            limit=10,
        )

        # 正例先立住：没有它，"不含乙人设"会因为结果恒空而永远成立
        assert MINE in payload
        assert THEIRS not in payload
        assert json.loads(payload)["total"] == 1

    @pytest.mark.asyncio
    async def test_detail_of_another_presets_memory_is_not_found(
        self,
        memory_db: None,
        two_preset_memories: Any,
    ) -> None:
        mine, theirs = await two_preset_memories()
        ctx = _StubCtx("inst_a", preset_id=1)

        own = await memory_tools.get_memory_detail_tool(ctx, memory_type="paragraph", memory_id=mine.id)  # type: ignore[arg-type]
        assert MINE in own

        with pytest.raises(ValueError, match=str(theirs.id)):
            await memory_tools.get_memory_detail_tool(ctx, memory_type="paragraph", memory_id=theirs.id)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_trace_of_another_presets_memory_is_not_found(
        self,
        memory_db: None,
        two_preset_memories: Any,
    ) -> None:
        mine, theirs = await two_preset_memories()
        ctx = _StubCtx("inst_a", preset_id=1)

        own = await memory_tools.trace_memory_origin_tool(ctx, memory_type="paragraph", memory_id=mine.id)  # type: ignore[arg-type]
        assert json.loads(own)["memory_id"] == mine.id

        with pytest.raises(ValueError, match=str(theirs.id)):
            await memory_tools.trace_memory_origin_tool(ctx, memory_type="paragraph", memory_id=theirs.id)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_the_no_preset_bucket_stays_readable(
        self,
        memory_db: None,
        two_preset_memories: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`preset_id=0`（存量记忆、控制台直发 CC）这一档三个出口都必须读得到。

        与 `instance_key=""` 同源，理由见 `MemoryScope.as_filter` 的 docstring。
        这条同时是上面三条的对照组：它证明"读不到"不是因为过滤把一切都挡光了。
        """
        await two_preset_memories()
        legacy = await _create_paragraph("这是改造前写入的存量内容", "inst_a", preset_id=0)
        monkeypatch.setattr(retriever_module, "memory_qdrant_manager", _ScriptedQdrant({legacy.id: 0.9}))

        ctx = _StubCtx("inst_a", preset_id=1)
        payload = await memory_tools.search_workspace_memories_tool(ctx, query="存量内容", limit=10)  # type: ignore[arg-type]
        assert "这是改造前写入的存量内容" in payload

        detail = await memory_tools.get_memory_detail_tool(ctx, memory_type="paragraph", memory_id=legacy.id)  # type: ignore[arg-type]
        assert "这是改造前写入的存量内容" in detail
