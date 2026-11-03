"""记忆向量库 payload 索引的回归。

之前补 Qdrant 缺的 payload 索引时加了 7 个字段索引，并让 `ensure_collection`
对**已存在**的 collection 也补建。但 `rebuild_collection` 是另一条路径：它
`delete_collection` + `create_collection` 从头重来，**没有**补索引。

而补索引的另一处 `ensure_collection` **整个进程只被调用一次**（`nekro_agent/__init__.py`
的 `_init_memory_scheduler`），所以索引会一直缺到**下次进程重启**为止。
（注意不是因为 `_initialized`：那个字段全库 5 处赋值、**0 个读取方**，它不 gate 任何东西。）

触发它只需要一条运维命令（`memory_reindex -y`）或维度变更后的一次重建。

少了索引不会让过滤失效（Qdrant 会退化成全量扫 payload），所以**没有任何行为断言杀得掉这个
缺陷**——判据只能落在「建完 collection 之后有没有把索引建回来」这一步上（§2.17 同一条教训）。
索引清单本身也一并钉住：`instance_key` 是记忆按实例隔离的过滤字段，它掉出清单不会报错，
只会让每次提示词渲染里的那次检索悄悄变慢。
"""

from __future__ import annotations

from typing import Any, List, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.services.memory import qdrant_manager as qdrant_module
from nekro_agent.services.memory.instance_scope import MemoryScope
from nekro_agent.services.memory.qdrant_manager import MemoryQdrantManager

COLLECTION = "test_memory_paragraphs"

# 与 `_ensure_payload_indexes` 的清单逐字对应。写成常量而不是"读实现再比自己"：
# 后者对任何改动都恒真，等于没断言。
EXPECTED_INDEX_FIELDS = [
    "workspace_id",
    "instance_key",
    "preset_id",
    "paragraph_id",
    "cognitive_type",
    "memory_source",
    "is_inactive",
    "event_time",
]


def _filter_keys(query_filter: Any) -> List[str]:
    """把一份 Qdrant 过滤条件里出现的 payload 字段名全抠出来（含**嵌套** Filter）。

    人设那一档不是一条平铺的 `FieldCondition`：它是「等于当前人设 **或** 等于 0
    （不属于任何人设）**或** 字段缺失（存量 point 的 payload 里没有这个 key）」，
    落在结构上就是 `must` 里嵌了一个 `should`。直接 `condition.key` 会 AttributeError
    ——而那正是"新加的过滤字段没进索引清单"最容易溜过去的形状。
    """
    keys: List[str] = []
    for condition in list(query_filter.must or []) + list(query_filter.should or []):
        nested_key = getattr(condition, "key", None)
        if isinstance(nested_key, str):
            keys.append(nested_key)
            continue
        is_empty = getattr(condition, "is_empty", None)
        if is_empty is not None:
            keys.append(is_empty.key)
            continue
        if getattr(condition, "must", None) is not None or getattr(condition, "should", None) is not None:
            keys.extend(_filter_keys(condition))
    return keys


class _FakeCollections:
    def __init__(self, names: List[str]) -> None:
        self.collections = [type("C", (), {"name": name})() for name in names]


class FakeQdrantClient:
    """只记录调用序列的假客户端，不做任何网络访问。"""

    def __init__(self, existing: List[str] | None = None) -> None:
        self.existing = list(existing or [])
        self.calls: List[Tuple[str, Any]] = []
        self.indexed_fields: List[str] = []
        self.last_query_filter: Any = None

    async def get_collections(self) -> _FakeCollections:
        return _FakeCollections(self.existing)

    async def get_collection(self, name: str) -> Any:
        raise AssertionError(f"本用例不应走到 get_collection: {name}")

    async def delete_collection(self, collection_name: str) -> None:
        self.calls.append(("delete_collection", collection_name))
        if collection_name in self.existing:
            self.existing.remove(collection_name)

    async def create_collection(self, collection_name: str, vectors_config: Any) -> None:
        self.calls.append(("create_collection", collection_name))
        self.existing.append(collection_name)

    async def create_payload_index(self, collection_name: str, field_name: str, field_schema: Any) -> None:
        self.calls.append(("create_payload_index", field_name))
        self.indexed_fields.append(field_name)

    async def search(self, collection_name: str, query_vector: Any, query_filter: Any, limit: int, score_threshold: float) -> List[Any]:
        self.calls.append(("search", query_filter))
        self.last_query_filter = query_filter
        return []


@pytest.fixture
async def paragraph_db():
    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"models": ["nekro_agent.models.db_mem_paragraph"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeQdrantClient:
    client = FakeQdrantClient(existing=[COLLECTION])

    async def get_client() -> FakeQdrantClient:
        return client

    monkeypatch.setattr(qdrant_module, "get_qdrant_client", get_client)
    monkeypatch.setattr(qdrant_module, "get_memory_embedding_dimension", lambda: 1024)
    return client


class TestRebuildCollectionRestoresIndexes:
    @pytest.mark.asyncio
    async def test_indexes_are_recreated_after_rebuild(
        self,
        paragraph_db,
        fake_client: FakeQdrantClient,
    ) -> None:
        manager = MemoryQdrantManager(collection_name=COLLECTION)
        result = await manager.rebuild_collection()

        assert result["total"] == 0
        assert fake_client.indexed_fields == EXPECTED_INDEX_FIELDS

    @pytest.mark.asyncio
    async def test_indexes_are_built_after_the_collection_exists(
        self,
        paragraph_db,
        fake_client: FakeQdrantClient,
    ) -> None:
        """顺序也要对：索引必须建在 `create_collection` 之后，否则每一条都会失败被吞掉。"""
        manager = MemoryQdrantManager(collection_name=COLLECTION)
        await manager.rebuild_collection()

        kinds = [name for name, _ in fake_client.calls]
        assert kinds.index("create_collection") < kinds.index("create_payload_index")
        assert kinds.index("delete_collection") < kinds.index("create_collection")


class TestEnsureCollectionStillBuildsIndexes:
    """既有路径不受影响：新建 collection 时仍然建索引。"""

    @pytest.mark.asyncio
    async def test_fresh_collection_gets_indexes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeQdrantClient(existing=[])

        async def get_client() -> FakeQdrantClient:
            return client

        monkeypatch.setattr(qdrant_module, "get_qdrant_client", get_client)
        manager = MemoryQdrantManager(collection_name=COLLECTION)

        assert await manager.ensure_collection(dimension=1024) is True
        assert client.indexed_fields == EXPECTED_INDEX_FIELDS


class TestIndexFieldListMatchesFilterUsage:
    """清单与真正拿来过滤的字段对得上。

    这里刻意**不**再抄一份"过滤字段清单"——那正是本项目栽过的"同一份清单的第二个副本"：
    以后有人往 `search()` 里加一个新的 `FieldCondition`，写死的第二份清单不会有任何反应。
    改成真的调一次 `search()`（打开全部可选过滤条件），把它交给客户端的 `query_filter`
    里出现的每一个 `key` 抠出来，逐个要求在索引清单里。
    """

    @pytest.mark.asyncio
    async def test_every_field_search_filters_on_is_indexed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeQdrantClient(existing=[])

        async def get_client() -> FakeQdrantClient:
            return client

        monkeypatch.setattr(qdrant_module, "get_qdrant_client", get_client)
        manager = MemoryQdrantManager(collection_name=COLLECTION)
        await manager.ensure_collection(dimension=1024)

        await manager.search(
            query_vector=[0.0, 0.0, 0.0],
            workspace_id=1,
            cognitive_type="episodic",
            include_inactive=False,
            event_time_from=1_700_000_000,
            event_time_to=1_800_000_000,
            scope=MemoryScope(allowed_instance_keys=["ob1", ""], preset_id=7),
        )

        assert client.last_query_filter is not None, "search() 没有把过滤条件交给客户端"
        filtered_keys = _filter_keys(client.last_query_filter)
        # 先确认这次真的构造出了多字段过滤，否则下面的循环是空转
        assert set(filtered_keys) >= {
            "workspace_id",
            "instance_key",
            "preset_id",
            "is_inactive",
            "cognitive_type",
            "event_time",
        }
        for key in filtered_keys:
            assert key in client.indexed_fields, f"search() 按 {key} 过滤，但它不在 payload 索引清单里"

    @pytest.mark.asyncio
    async def test_instance_key_is_indexed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """记忆按实例隔离是一条不变式，它的过滤字段单独钉一条。"""
        client = FakeQdrantClient(existing=[])

        async def get_client() -> FakeQdrantClient:
            return client

        monkeypatch.setattr(qdrant_module, "get_qdrant_client", get_client)
        manager = MemoryQdrantManager(collection_name=COLLECTION)
        await manager.ensure_collection(dimension=1024)

        assert "instance_key" in client.indexed_fields
