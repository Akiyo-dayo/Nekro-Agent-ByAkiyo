"""embedding 用量归属：requester 从注入 / 工具 / 调度一路带到向量化调用。

向量化是记忆链路上唯一按「每条 query 一次请求」放大的花销，用量流水必须能回答
「这笔钱是谁花的」。归属来源是频道的 `(adapter_key, instance_key)`，由各入口显式传入；
不传的调用方（知识库等）保持改造前的空归属。

这里逐层钉住转发，不测计费实现本身：

- `EmbeddingService.embed_text` 把 requester 写进 `LLMUsageContext`；
- `MemoryRetriever.retrieve` 把它传给 `embed_text`；
- `retrieve_memories` / `retrieve_memories_batch` 传给检索器；
- `search_workspace_memories` 传给 `retrieve_memories`。
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional, Sequence, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.services import usage as usage_module
from nekro_agent.services.memory import query_service
from nekro_agent.services.memory.embedding_service import EmbeddingService
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.retriever import BatchRecallRequest, MemoryRetriever, RetrievalResult

# `memory/__init__.py` 把 `embedding_service` 这个**实例**导出成了包属性，
# `from ... import embedding_service` 拿到的是它而不是模块；monkeypatch 必须打在模块上。
embedding_module = importlib.import_module("nekro_agent.services.memory.embedding_service")
retriever_module = importlib.import_module("nekro_agent.services.memory.retriever")

REQUESTER = ("onebot_v11", "inst-embed")


@pytest.fixture(autouse=True)
async def memory_db() -> Any:
    """检索器在空结果时仍会构造 ORM 查询，需要一条默认连接。"""
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


class UsageRecorder:
    def __init__(self) -> None:
        self.contexts: List[Any] = []

    @contextmanager
    def __call__(self, context: Any) -> Iterator[None]:
        self.contexts.append(context)
        yield


class TestEmbeddingContext:
    async def test_requester_lands_on_usage_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = UsageRecorder()

        async def fake_embed(**_kwargs: Any) -> List[float]:
            return [0.1, 0.2, 0.3]

        monkeypatch.setattr(embedding_module, "gen_openai_embeddings", fake_embed)
        monkeypatch.setattr(embedding_module.EmbeddingService, "_get_model_config", lambda _self: {
            "model": "fake-embedding",
            "api_key": "sk-test",
            "base_url": "https://example.invalid/v1",
        })
        monkeypatch.setattr(usage_module, "usage_scope", recorder)

        service = EmbeddingService(model_group="default", dimension=3)
        vector = await service.embed_text("查询文本", requester=REQUESTER)

        assert vector == [0.1, 0.2, 0.3]
        assert recorder.contexts[0].adapter_key == "onebot_v11"
        assert recorder.contexts[0].instance_key == "inst-embed"

    async def test_without_requester_keeps_old_empty_attribution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = UsageRecorder()

        async def fake_embed(**_kwargs: Any) -> List[float]:
            return [0.1, 0.2, 0.3]

        monkeypatch.setattr(embedding_module, "gen_openai_embeddings", fake_embed)
        monkeypatch.setattr(embedding_module.EmbeddingService, "_get_model_config", lambda _self: {
            "model": "fake-embedding",
            "api_key": "sk-test",
            "base_url": "https://example.invalid/v1",
        })
        monkeypatch.setattr(usage_module, "usage_scope", recorder)

        await EmbeddingService(model_group="default", dimension=3).embed_text("知识库文本")

        assert recorder.contexts[0].adapter_key == ""
        assert recorder.contexts[0].instance_key == ""


class TestRetrieverForwarding:
    async def test_retrieve_passes_requester_to_embedding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: List[Optional[Tuple[str, str]]] = []

        async def fake_embed(_query: str, *, requester: Optional[Tuple[str, str]] = None) -> List[float]:
            captured.append(requester)
            return [0.1, 0.2, 0.3]

        async def fake_search(**_kwargs: Any) -> List[Any]:
            return []

        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
        monkeypatch.setattr(retriever_module.memory_qdrant_manager, "search", fake_search)

        await MemoryRetriever(0, scope=MemoryScope.unrestricted()).retrieve("查询", requester=REQUESTER)

        assert captured == [REQUESTER]

    async def test_retrieve_without_requester_calls_embedding_without_kwarg(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: List[str] = []

        async def fake_embed(_query: str) -> List[float]:
            calls.append("plain")
            return [0.1, 0.2, 0.3]

        async def fake_search(**_kwargs: Any) -> List[Any]:
            return []

        monkeypatch.setattr(config, "MEMORY_ENABLE_SYSTEM", True)
        monkeypatch.setattr(retriever_module, "embed_text", fake_embed)
        monkeypatch.setattr(retriever_module.memory_qdrant_manager, "search", fake_search)

        await MemoryRetriever(0, scope=MemoryScope.unrestricted()).retrieve("查询")

        assert calls == ["plain"]

    async def test_module_helpers_forward_requester(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: List[Optional[Tuple[str, str]]] = []

        async def fake_retrieve(
            _self: MemoryRetriever,
            _query: str,
            **_kwargs: Any,
        ) -> RetrievalResult:
            captured.append(_kwargs.get("requester"))
            return RetrievalResult(memories=[], query_embedding_time_ms=0, search_time_ms=0, total_candidates=0)

        monkeypatch.setattr(MemoryRetriever, "retrieve", fake_retrieve)

        await retriever_module.retrieve_memories(
            0,
            "q1",
            scope=MemoryScope.unrestricted(),
            requester=REQUESTER,
        )
        await retriever_module.retrieve_memories_batch(
            0,
            [BatchRecallRequest(query="q2")],
            scope=MemoryScope.unrestricted(),
            requester=REQUESTER,
        )

        assert captured == [REQUESTER, REQUESTER]

    async def test_query_service_forwards_requester(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: List[Optional[Tuple[str, str]]] = []

        async def fake_retrieve_memories(*_args: Any, **kwargs: Any) -> Sequence[Any]:
            captured.append(kwargs.get("requester"))
            return []

        monkeypatch.setattr(query_service, "retrieve_memories", fake_retrieve_memories)

        await query_service.search_workspace_memories(
            0,
            "q",
            scope=MemoryScope.unrestricted(),
            requester=REQUESTER,
        )

        assert captured == [REQUESTER]
