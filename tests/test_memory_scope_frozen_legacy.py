"""改造前记忆过滤条件的逐字冻结副本。

这里的函数是仓库里现有实现的**拷贝**，只改了名字与签名，逻辑一个字符都没有动。它只服务于
一件事：以后每一轮改记忆作用域时，拿新代码算出来的条件与这份副本逐项比较。副本一旦被
"顺手优化"，比较就失去意义，所以本文件刻意不 import 被测函数来复用。

搜索条件那一段连**条件的种类与顺序**都冻结了：`must` 是有序列表，顺序变了虽然语义等价，
但向量库的执行计划与调试输出都会跟着变，而这个文件要证明的正是"逐字相同"。
"""

from __future__ import annotations

from typing import Any

import pytest
from qdrant_client import models as qdrant_models
from tortoise import Tortoise

from nekro_agent.models.db_mem_entity import DBMemEntity
from nekro_agent.models.db_mem_episode import DBMemEpisode
from nekro_agent.models.db_mem_paragraph import DBMemParagraph
from nekro_agent.models.db_mem_relation import DBMemRelation
from nekro_agent.services.memory import qdrant_manager as qdrant_manager_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.qdrant_manager import memory_qdrant_manager
from nekro_agent.services.memory.scope_condition import MemTable

_FROZEN_LEGACY_INSTANCE_KEY = ""
_FROZEN_NO_PRESET_ID = 0
_FROZEN_PRESET_NONE_PAYLOAD = 0


def _frozen_build_instance_filter(allowed_instance_keys: list[str] | None) -> dict[str, list[str]]:
    if allowed_instance_keys is None:
        return {}
    return {"instance_key__in": allowed_instance_keys}


def _frozen_as_filter(allowed_instance_keys: list[str] | None, preset_id: int | None) -> dict[str, Any]:
    query: dict[str, Any] = dict(_frozen_build_instance_filter(allowed_instance_keys))
    if preset_id is None:
        return query
    if preset_id == _FROZEN_NO_PRESET_ID:
        query["preset_id"] = _FROZEN_NO_PRESET_ID
    else:
        query["preset_id__in"] = [preset_id, _FROZEN_NO_PRESET_ID]
    return query


def _frozen_build_preset_condition(preset_id: int) -> Any:
    should: list[Any] = [
        qdrant_models.FieldCondition(
            key="preset_id",
            match=qdrant_models.MatchValue(value=preset_id),
        ),
    ]
    if preset_id != _FROZEN_PRESET_NONE_PAYLOAD:
        should.append(
            qdrant_models.FieldCondition(
                key="preset_id",
                match=qdrant_models.MatchValue(value=_FROZEN_PRESET_NONE_PAYLOAD),
            ),
        )
    should.append(
        qdrant_models.IsEmptyCondition(
            is_empty=qdrant_models.PayloadField(key="preset_id"),
        ),
    )
    return qdrant_models.Filter(should=should)


def _frozen_search_filter(
    workspace_id: int,
    scope: MemoryScope,
    cognitive_type: str | None,
    include_inactive: bool,
    event_time_from: int | None,
    event_time_to: int | None,
) -> qdrant_models.Filter:
    must_conditions: list[Any] = [
        qdrant_models.FieldCondition(
            key="workspace_id",
            match=qdrant_models.MatchValue(value=workspace_id),
        ),
    ]

    if scope.allowed_instance_keys is not None:
        must_conditions.append(
            qdrant_models.FieldCondition(
                key="instance_key",
                match=qdrant_models.MatchAny(any=list(scope.allowed_instance_keys)),
            ),
        )

    if scope.preset_id is not None:
        must_conditions.append(_frozen_build_preset_condition(scope.preset_id))

    if not include_inactive:
        must_conditions.append(
            qdrant_models.FieldCondition(
                key="is_inactive",
                match=qdrant_models.MatchValue(value=False),
            ),
        )

    if cognitive_type:
        must_conditions.append(
            qdrant_models.FieldCondition(
                key="cognitive_type",
                match=qdrant_models.MatchValue(value=cognitive_type),
            ),
        )

    if event_time_from is not None:
        must_conditions.append(
            qdrant_models.FieldCondition(
                key="event_time",
                range=qdrant_models.Range(gte=event_time_from),
            ),
        )

    if event_time_to is not None:
        must_conditions.append(
            qdrant_models.FieldCondition(
                key="event_time",
                range=qdrant_models.Range(lte=event_time_to),
            ),
        )

    return qdrant_models.Filter(must=must_conditions)


_ALLOWED_INSTANCE_SETS: list[list[str] | None] = [
    None,
    ["ob1", ""],
    ["ob1", "ob2", ""],
]
_PRESET_IDS: list[int | None] = [None, 0, 7]
_WORKSPACE_IDS = [1, 5]
_COGNITIVE_TYPES: list[str | None] = [None, "episodic"]
_TIME_WINDOWS: list[tuple[int | None, int | None]] = [
    (None, None),
    (100, None),
    (None, 200),
    (100, 200),
]


class _RecordingQdrantClient:
    """只记录 `search` 收到的过滤条件，不返回任何候选。"""

    def __init__(self) -> None:
        self.query_filters: list[Any] = []

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.query_filters.append(kwargs["query_filter"])
        return []


@pytest.mark.parametrize("preset_id", _PRESET_IDS)
@pytest.mark.parametrize("allowed_instance_keys", _ALLOWED_INSTANCE_SETS)
def test_database_filter_matches_the_frozen_copy(
    allowed_instance_keys: list[str] | None,
    preset_id: int | None,
) -> None:
    scope = MemoryScope(allowed_instance_keys=allowed_instance_keys, preset_id=preset_id)

    assert scope.as_filter() == _frozen_as_filter(allowed_instance_keys, preset_id)


@pytest.mark.parametrize(
    ("workspace_id", "cognitive_type", "include_inactive", "event_time_from", "event_time_to"),
    [
        (workspace_id, cognitive_type, include_inactive, time_from, time_to)
        for workspace_id in _WORKSPACE_IDS
        for cognitive_type in _COGNITIVE_TYPES
        for include_inactive in (False, True)
        for time_from, time_to in _TIME_WINDOWS
    ],
)
@pytest.mark.parametrize("preset_id", _PRESET_IDS)
@pytest.mark.parametrize("allowed_instance_keys", _ALLOWED_INSTANCE_SETS)
async def test_vector_filter_matches_the_frozen_copy(
    monkeypatch: pytest.MonkeyPatch,
    allowed_instance_keys: list[str] | None,
    preset_id: int | None,
    workspace_id: int,
    cognitive_type: str | None,
    include_inactive: bool,
    event_time_from: int | None,
    event_time_to: int | None,
) -> None:
    client = _RecordingQdrantClient()

    async def fake_get_qdrant_client() -> _RecordingQdrantClient:
        return client

    monkeypatch.setattr(qdrant_manager_module, "get_qdrant_client", fake_get_qdrant_client)
    scope = MemoryScope(allowed_instance_keys=allowed_instance_keys, preset_id=preset_id)

    await memory_qdrant_manager.search(
        query_vector=[0.0, 0.0, 0.0, 0.0],
        workspace_id=workspace_id,
        cognitive_type=cognitive_type,
        include_inactive=include_inactive,
        event_time_from=event_time_from,
        event_time_to=event_time_to,
        scope=scope,
    )

    assert len(client.query_filters) == 1
    assert client.query_filters[0] == _frozen_search_filter(
        workspace_id,
        scope,
        cognitive_type,
        include_inactive,
        event_time_from,
        event_time_to,
    )


_MODEL_TABLES = (
    (DBMemParagraph, MemTable.PARAGRAPH),
    (DBMemEntity, MemTable.ENTITY),
    (DBMemRelation, MemTable.RELATION),
    (DBMemEpisode, MemTable.EPISODE),
)


@pytest.fixture
async def sqlite_dialect():
    """只为 `.sql()` 提供 SQLite 方言；不建表，也不读写数据。"""
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={
            "models": [
                "nekro_agent.models.db_mem_paragraph",
                "nekro_agent.models.db_mem_entity",
                "nekro_agent.models.db_mem_relation",
                "nekro_agent.models.db_mem_episode",
            ],
        },
    )
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.mark.parametrize("workspace_id", _WORKSPACE_IDS)
@pytest.mark.parametrize("preset_id", _PRESET_IDS)
@pytest.mark.parametrize("allowed_instance_keys", _ALLOWED_INSTANCE_SETS)
async def test_apply_renders_the_frozen_database_filter(
    sqlite_dialect: None,
    allowed_instance_keys: list[str] | None,
    preset_id: int | None,
    workspace_id: int,
) -> None:
    """旧形状的 `apply` 必须与今天各出口的 `filter(workspace_id=..., **as_filter())` 逐字相同。"""
    scope = MemoryScope(allowed_instance_keys=allowed_instance_keys, preset_id=preset_id)
    frozen = _frozen_as_filter(allowed_instance_keys, preset_id)
    for model, table in _MODEL_TABLES:
        actual = scope.apply(model.filter(), table, workspace_id=workspace_id).sql()
        expected = model.filter(workspace_id=workspace_id, **frozen).sql()
        assert actual == expected, f"{model.__name__} 的 SQL 与冻结副本不同"
        assert model.filter(scope.pg_q(table, workspace_id=workspace_id)).sql() == expected


@pytest.mark.parametrize("workspace_id", _WORKSPACE_IDS)
@pytest.mark.parametrize("preset_id", _PRESET_IDS)
@pytest.mark.parametrize("allowed_instance_keys", _ALLOWED_INSTANCE_SETS)
def test_qdrant_must_equals_the_frozen_first_three_conditions(
    allowed_instance_keys: list[str] | None,
    preset_id: int | None,
    workspace_id: int,
) -> None:
    """旧形状的 `qdrant_must` 就是今天 `search()` 的前三类条件，顺序与对象都不变。"""
    scope = MemoryScope(allowed_instance_keys=allowed_instance_keys, preset_id=preset_id)
    expected: list[Any] = [
        qdrant_models.FieldCondition(
            key="workspace_id",
            match=qdrant_models.MatchValue(value=workspace_id),
        ),
    ]
    if allowed_instance_keys is not None:
        expected.append(
            qdrant_models.FieldCondition(
                key="instance_key",
                match=qdrant_models.MatchAny(any=list(allowed_instance_keys)),
            ),
        )
    if preset_id is not None:
        expected.append(_frozen_build_preset_condition(preset_id))
    assert scope.qdrant_must(workspace_id=workspace_id) == expected
