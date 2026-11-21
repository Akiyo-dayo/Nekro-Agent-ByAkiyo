"""表情向量 `set_id` 回填：批里混了不存在的点

## 实际部署里看到的

存量导入与启动回填都按 100 一批 `set_payload`。Qdrant 按 ID 列表写载荷时，**批里只要有一个点
不存在，整批回 404、一个都不写**。存量里有 3 条从没写成嵌入的表情恰好落在第 201 ~ 300 条那一批，
于是那一批剩下的 97 个点永远拿不到 `set_id`，按套组过滤的搜索就搜不到它们——每次启动都卡在同一批。

## 桩

旧用例的桩在 `set_payload` 遇到不存在的点时会默默把它建出来，这个缺陷因此一直抓不到。这里的
`StrictQdrant` 照真 Qdrant 的行为：批里有不存在的点就抛 404 的 `UnexpectedResponse`、整批不写；
`retrieve` 对不存在的 ID 不报错、只是不返回。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import httpx
import pytest
from qdrant_client.http.exceptions import UnexpectedResponse
from tortoise import Tortoise

from nekro_agent.models.db_emotion_item import DBEmotionItem
from nekro_agent.models.db_emotion_set import DBEmotionSet
from nekro_agent.services import emotion_library as lib


def _not_found() -> UnexpectedResponse:
    return UnexpectedResponse(status_code=404, reason_phrase="Not Found", content=b"{}", headers=httpx.Headers())


class _Record:
    def __init__(self, point_id: int) -> None:
        self.id = point_id
        self.payload = None
        self.vector = None


class _Collections:
    def __init__(self, names: Sequence[str]) -> None:
        self.collections = [type("C", (), {"name": name})() for name in names]


class StrictQdrant:
    """照真 Qdrant 行为的桩：`set_payload` 遇到任何不存在的点就整批 404"""

    def __init__(self) -> None:
        self.payloads: Dict[int, Dict[str, Any]] = {}
        self.vectors: Dict[int, Any] = {}
        self.retrieve_fails = False
        self.set_payload_calls = 0

    def seed(self, emotion_ids: Sequence[str]) -> None:
        for emotion_id in emotion_ids:
            point_id = int(emotion_id, 16)
            self.payloads[point_id] = {"emotion_id": emotion_id}
            self.vectors[point_id] = [0.0]

    async def get_collections(self) -> _Collections:
        return _Collections([lib.EMOTION_VECTOR_COLLECTION])

    async def retrieve(self, *, collection_name: str, ids: Sequence[int], with_payload: bool = True, with_vectors: bool = False) -> List[Any]:
        if self.retrieve_fails:
            raise RuntimeError("qdrant hiccup")
        return [_Record(int(point_id)) for point_id in ids if int(point_id) in self.payloads]

    async def set_payload(self, *, collection_name: str, payload: Dict[str, Any], points: Sequence[int], wait: bool = True) -> None:
        self.set_payload_calls += 1
        if any(int(point_id) not in self.payloads for point_id in points):
            raise _not_found()
        for point_id in points:
            self.payloads[int(point_id)].update(payload)

    async def upsert(self, *, collection_name: str, points: Sequence[Any], wait: bool = True) -> None:
        for point in points:
            self.payloads[int(point.id)] = dict(point.payload or {})
            self.vectors[int(point.id)] = point.vector

    def set_id_of(self, emotion_id: str) -> Optional[int]:
        payload = self.payloads.get(int(emotion_id, 16))
        return None if payload is None else payload.get("set_id")


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    lib.clear_scope_cache()
    try:
        yield
    finally:
        lib.clear_scope_cache()
        await Tortoise.close_connections()


@pytest.fixture
def qdrant(monkeypatch: pytest.MonkeyPatch) -> StrictQdrant:
    client = StrictQdrant()

    async def _client() -> StrictQdrant:
        return client

    monkeypatch.setattr(lib, "get_qdrant_client", _client)
    return client


def ids(count: int, *, start: int = 0) -> List[str]:
    return [f"{index:08x}" for index in range(start, start + count)]


async def legacy_set_with(emotion_ids: Sequence[str]) -> DBEmotionSet:
    legacy = await lib.ensure_legacy_set()
    for emotion_id in emotion_ids:
        await DBEmotionItem.create(
            emotion_id=emotion_id,
            set_id=legacy.id,
            description=f"表情 {emotion_id}",
            tags=["标签"],
            source_path="",
            file_path=f"{emotion_id}.png",
            file_sha256="",
            collected_chat_key="",
            added_at=0,
            updated_at=0,
        )
    return legacy


class TestOneMissingPointDoesNotSinkTheBatch:
    async def test_the_points_that_exist_get_their_set_id(self, db, qdrant: StrictQdrant) -> None:
        """修复前：整批 404、返回 0、三个点一个都没写上"""
        present = ids(2)
        qdrant.seed(present)
        batch = [present[0], "deadbeef", present[1]]

        written = await lib.vector_set_set_id(batch, 7)

        assert written == 2
        assert qdrant.set_id_of(present[0]) == 7
        assert qdrant.set_id_of(present[1]) == 7
        assert qdrant.set_id_of("deadbeef") is None, "不存在的点不该被凭空建出来"

    async def test_the_detailed_result_names_the_missing_ids(self, db, qdrant: StrictQdrant) -> None:
        qdrant.seed(["0000aaaa"])

        result = await lib.sync_set_id_batch(["0000aaaa", "0000bbbb"], 3)

        assert result.written == 1
        assert result.missing == ("0000bbbb",)
        assert result.failed == 0

    async def test_a_failing_retrieve_falls_back_to_one_by_one(self, db, qdrant: StrictQdrant) -> None:
        """查存在性本身失败时：整批写 404 之后逐条写，404 的记进 missing，其余照写"""
        present = ids(3)
        qdrant.seed(present)
        qdrant.retrieve_fails = True

        result = await lib.sync_set_id_batch([present[0], "deadbeef", present[1], present[2]], 5)

        assert result.written == 3
        assert result.missing == ("deadbeef",)
        assert all(qdrant.set_id_of(emotion_id) == 5 for emotion_id in present)


class TestTheStartupBackfill:
    async def test_a_batch_with_missing_points_no_longer_blocks_the_rest(self, db, qdrant: StrictQdrant) -> None:
        """实际部署的形状：250 条，第二批里有 3 条在向量库里没有点"""
        all_ids = ids(250)
        absent = {all_ids[120], all_ids[150], all_ids[180]}
        qdrant.seed([emotion_id for emotion_id in all_ids if emotion_id not in absent])
        legacy = await legacy_set_with(all_ids)

        counts = await lib.resync_vector_set_ids()

        assert counts["points"] == 247
        assert counts["missing"] == 3
        assert counts["reembedded"] == 0
        stuck = [emotion_id for emotion_id in all_ids if emotion_id not in absent and qdrant.set_id_of(emotion_id) != legacy.id]
        assert stuck == []

    async def test_missing_points_are_re_embedded_with_the_right_set_id(self, db, qdrant: StrictQdrant) -> None:
        all_ids = ids(5)
        qdrant.seed(all_ids[:3])
        legacy = await legacy_set_with(all_ids)
        asked: List[str] = []

        async def embed(item: DBEmotionItem) -> Optional[List[float]]:
            asked.append(item.emotion_id)
            return [0.5, 0.5]

        counts = await lib.resync_vector_set_ids(embed_missing=embed)

        assert sorted(asked) == sorted(all_ids[3:])
        assert counts["reembedded"] == 2
        for emotion_id in all_ids[3:]:
            payload = qdrant.payloads[int(emotion_id, 16)]
            assert payload["set_id"] == legacy.id
            assert payload["emotion_id"] == emotion_id
            assert payload["description"] == f"表情 {emotion_id}"

    async def test_re_embedding_respects_the_per_start_limit(self, db, qdrant: StrictQdrant) -> None:
        all_ids = ids(6)
        qdrant.seed(all_ids[:1])
        await legacy_set_with(all_ids)
        asked: List[str] = []

        async def embed(item: DBEmotionItem) -> Optional[List[float]]:
            asked.append(item.emotion_id)
            return [1.0]

        counts = await lib.resync_vector_set_ids(embed_missing=embed, reembed_limit=2)

        assert counts["missing"] == 5
        assert counts["reembedded"] == 2
        assert len(asked) == 2

    async def test_an_item_whose_file_is_gone_is_not_re_embedded(self, db, qdrant: StrictQdrant) -> None:
        all_ids = ids(2)
        qdrant.seed(all_ids[:1])
        await legacy_set_with(all_ids)

        async def no_file(_item: DBEmotionItem) -> Optional[List[float]]:
            return None

        counts = await lib.resync_vector_set_ids(embed_missing=no_file)

        assert counts["missing"] == 1
        assert counts["reembedded"] == 0
        assert int(all_ids[1], 16) not in qdrant.payloads


class TestTheLegacyImport:
    async def test_import_writes_set_id_to_the_points_that_exist(self, db, qdrant: StrictQdrant, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """导入时的那次同步走同一个函数：批里缺一个点也不会让整批白写"""
        import json

        monkeypatch.setattr(lib, "EMOTION_FILES_DIR", tmp_path)
        entries = {emotion_id: {"description": "d", "tags": [], "file_path": ""} for emotion_id in ids(4)}
        qdrant.seed(ids(3))

        counts = await lib.import_legacy_store(json.dumps({"emotions": entries}))

        legacy = await DBEmotionSet.filter(kind=lib.KIND_LEGACY).first()
        assert legacy is not None
        assert counts["inserted"] == 4
        assert all(qdrant.set_id_of(emotion_id) == legacy.id for emotion_id in ids(3))


class TestThePluginWiring:
    async def test_the_background_backfill_passes_the_re_embed_callback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from plugins.builtin import emotion as plugin_module

        seen: Dict[str, Any] = {}

        async def _resync(**kwargs: Any) -> Dict[str, int]:
            seen.update(kwargs)
            return {"sets": 1, "points": 0, "batches": 1, "missing": 0, "reembedded": 0, "failed": 0}

        monkeypatch.setattr(lib, "resync_vector_set_ids", _resync)

        await plugin_module._resync_vector_set_ids_in_background()  # noqa: SLF001

        assert seen.get("embed_missing") is plugin_module._embed_item_for_backfill  # noqa: SLF001

    async def test_an_item_without_its_file_is_not_re_embedded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from plugins.builtin import emotion as plugin_module

        called: List[str] = []

        async def _embedding(text: str) -> Optional[List[float]]:
            called.append(text)
            return [1.0]

        monkeypatch.setattr(plugin_module, "_emotion_file_of", lambda _item: None)
        monkeypatch.setattr(plugin_module, "_safe_embedding", _embedding)
        item = DBEmotionItem(emotion_id="0000abcd", description="d", tags=["t"])

        assert await plugin_module._embed_item_for_backfill(item) is None  # noqa: SLF001
        assert called == []
