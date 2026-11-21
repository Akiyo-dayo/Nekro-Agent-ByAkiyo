"""表情行：越权口径、ID 分配、文件引用计数、移动与复制时的向量载荷

## 这一层挡的是什么

可读 / 可写集合算对之后，剩下的错都在「按 ID 操作」这一步：把只读的当成可写的、把不存在的
当成越权（两种文案不同就等于给模型一个探测器）、删文件时没看别的行是不是还指着它。
这里用一份最小的套组布局把三种结果逐一钉住，文案逐字比对。

## 向量用打桩客户端

真 Qdrant 不参与用例：桩记下每次 `upsert` / `set_payload` / `delete` / `retrieve` 的入参，
断言「移动之后载荷里的 `set_id` 改成了目标套组」「复制时新点的向量与载荷来自源点」这类
服务层自己的责任。向量库落后于库表时的行为（源点不存在）也在这里覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_emotion_item import DBEmotionItem
from nekro_agent.models.db_emotion_set import DBEmotionSet
from nekro_agent.services import emotion_library as lib

ADAPTER = "onebot_v11"
INSTANCE = "inst1"


class FakeQdrant:
    """记下每次调用的 Qdrant 桩：点 ID 与载荷都留在内存里，够断言服务层改了什么"""

    def __init__(self) -> None:
        self.points: Dict[int, Dict[str, Any]] = {}
        self.vectors: Dict[int, Any] = {}
        self.calls: List[Tuple[str, Any]] = []
        self.fail_upsert = False

    async def upsert(self, *, collection_name: str, points: Sequence[Any], wait: bool = True) -> None:
        self.calls.append(("upsert", collection_name))
        if self.fail_upsert:
            raise RuntimeError("qdrant down")
        for point in points:
            self.points[int(point.id)] = dict(point.payload or {})
            self.vectors[int(point.id)] = point.vector

    async def set_payload(self, *, collection_name: str, payload: Dict[str, Any], points: Sequence[int], wait: bool = True) -> None:
        self.calls.append(("set_payload", (collection_name, dict(payload), list(points))))
        for point_id in points:
            self.points.setdefault(int(point_id), {}).update(payload)

    async def delete(self, *, collection_name: str, points_selector: Any) -> None:
        self.calls.append(("delete", collection_name))
        for point_id in getattr(points_selector, "points", []):
            self.points.pop(int(point_id), None)
            self.vectors.pop(int(point_id), None)

    async def retrieve(self, *, collection_name: str, ids: Sequence[int], with_payload: bool = True, with_vectors: bool = False) -> List[Any]:
        self.calls.append(("retrieve", list(ids)))
        records = []
        for point_id in ids:
            if int(point_id) not in self.points:
                continue
            records.append(
                _Record(
                    id=int(point_id),
                    payload=dict(self.points[int(point_id)]) if with_payload else None,
                    vector=self.vectors.get(int(point_id)) if with_vectors else None,
                ),
            )
        return records

    async def search(self, *, collection_name: str, query_vector: Sequence[float], query_filter: Any, limit: int, with_payload: bool = True) -> List[Any]:
        self.calls.append(("search", (collection_name, query_filter, limit)))
        return []


class _Record:
    def __init__(self, *, id: int, payload: Optional[Dict[str, Any]], vector: Any) -> None:
        self.id = id
        self.payload = payload
        self.vector = vector


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
def fake_qdrant(monkeypatch: pytest.MonkeyPatch) -> FakeQdrant:
    client = FakeQdrant()

    async def _client() -> FakeQdrant:
        return client

    monkeypatch.setattr(lib, "get_qdrant_client", _client)
    return client


@pytest.fixture
def files_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把表情文件目录改到临时目录：用例不碰真实的 `data/`"""
    target = tmp_path / "emotions"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lib, "EMOTION_FILES_DIR", target)
    return target


async def make_collect() -> DBEmotionSet:
    return await lib.get_or_create_collect_set(ADAPTER, INSTANCE)


async def make_scope(collect: DBEmotionSet, extra: Sequence[DBEmotionSet] = ()) -> lib.EmotionScope:
    readable = {collect.id, *(row.id for row in extra)}
    return lib.EmotionScope(
        resolved=True,
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        is_platform_instance=False,
        persona_id=0,
        collect_set_id=collect.id,
        readable_set_ids=frozenset(readable),
        writable_set_ids=frozenset({collect.id}),
    )


async def add_item(
    collect: DBEmotionSet,
    *,
    emotion_id: str,
    file_path: str = "h/abc.png",
    file_sha256: str = "",
) -> DBEmotionItem:
    return await DBEmotionItem.create(
        emotion_id=emotion_id,
        set_id=collect.id,
        description="描述",
        tags=["标签"],
        file_path=file_path,
        file_sha256=file_sha256,
        added_at=1,
        updated_at=1,
    )


class TestWritableItem:
    async def test_a_writable_item_comes_back_without_a_message(self, db) -> None:
        collect = await make_collect()
        item = await add_item(collect, emotion_id="aaaa0001")
        scope = await make_scope(collect)

        found, message = await lib.get_writable_item(scope, "aaaa0001")

        assert found is not None
        assert found.id == item.id
        assert message is None

    async def test_a_readonly_item_answers_the_platform_message(self, db) -> None:
        collect = await make_collect()
        preset = await lib.create_preset_set(name="平台方预设")
        item = await add_item(collect, emotion_id="aaaa0002")
        # 把这条表情挪进平台方套组：会话能读、不能写
        item.set_id = preset.id
        await item.save()
        scope = await make_scope(collect, [preset])

        found, message = await lib.get_writable_item(scope, "aaaa0002")

        assert found is None
        assert message == lib.PLATFORM_READONLY_MESSAGE == "这是平台方提供的表情，只能使用不能修改"

    async def test_an_unknown_id_answers_not_found_verbatim(self, db) -> None:
        collect = await make_collect()
        scope = await make_scope(collect)

        found, message = await lib.get_writable_item(scope, "deadbeef")

        assert found is None
        assert message == "Error: Emotion with ID 'deadbeef' not found"

    async def test_an_item_outside_the_readable_sets_answers_not_found(self, db) -> None:
        collect = await make_collect()
        other_collect = await lib.get_or_create_collect_set(ADAPTER, "other1")
        await add_item(other_collect, emotion_id="aaaa0003")
        scope = await make_scope(collect)

        found, message = await lib.get_writable_item(scope, "aaaa0003")

        assert found is None
        assert message == lib.emotion_not_found_message("aaaa0003")


class TestAllocateEmotionId:
    async def test_it_retries_until_a_free_id_is_found(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        collect = await make_collect()
        await add_item(collect, emotion_id="aaaa0001")
        generated = iter(["aaaa0001", "aaaa0001", "bbbb0002"])

        monkeypatch.setattr(lib, "generate_emotion_id", lambda: next(generated))

        assert await lib.allocate_emotion_id() == "bbbb0002"

    async def test_it_gives_up_after_five_collisions(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        collect = await make_collect()
        await add_item(collect, emotion_id="aaaa0001")
        monkeypatch.setattr(lib, "generate_emotion_id", lambda: "aaaa0001")

        with pytest.raises(RuntimeError):
            await lib.allocate_emotion_id()

    async def test_generated_ids_are_eight_lowercase_hex_digits(self) -> None:
        for _ in range(20):
            candidate = lib.generate_emotion_id()
            assert len(candidate) == 8
            assert candidate == candidate.lower()
            int(candidate, 16)


class TestFindReadableBySha256:
    async def test_it_only_finds_items_in_the_readable_sets(self, db) -> None:
        collect = await make_collect()
        other_collect = await lib.get_or_create_collect_set(ADAPTER, "other1")
        await add_item(other_collect, emotion_id="cccc0001", file_sha256="a" * 64)
        scope = await make_scope(collect)

        assert await lib.find_readable_by_sha256(scope, "a" * 64) is None

        mine = await add_item(collect, emotion_id="cccc0002", file_sha256="a" * 64)
        found = await lib.find_readable_by_sha256(scope, "a" * 64)

        assert found is not None
        assert found.id == mine.id

    async def test_an_empty_scope_finds_nothing(self, db) -> None:
        collect = await make_collect()
        await add_item(collect, emotion_id="cccc0003", file_sha256="b" * 64)
        empty = lib.EmotionScope(
            resolved=False,
            adapter_key="",
            instance_key="",
            is_platform_instance=False,
            persona_id=0,
            collect_set_id=None,
            readable_set_ids=frozenset(),
            writable_set_ids=frozenset(),
        )

        assert await lib.find_readable_by_sha256(empty, "b" * 64) is None


class TestDeleteItemFileRefCount:
    async def test_the_file_survives_until_the_last_row_is_deleted(self, db, files_dir: Path, fake_qdrant: FakeQdrant) -> None:
        collect = await make_collect()
        data = b"\x89PNG\r\n\x1a\n" + b"payload"
        rel_path, digest = lib.store_bytes_by_hash(data, "png")
        target = files_dir / rel_path
        assert target.is_file()

        first = await add_item(collect, emotion_id="dddd0001", file_path=rel_path, file_sha256=digest)
        await add_item(collect, emotion_id="dddd0002", file_path=rel_path, file_sha256=digest)

        await lib.delete_item(first)
        assert target.is_file()

        second = await DBEmotionItem.filter(emotion_id="dddd0002").first()
        assert second is not None
        await lib.delete_item(second)
        assert not target.exists()

    async def test_deleting_an_item_removes_its_vector_point(self, db, files_dir: Path, fake_qdrant: FakeQdrant) -> None:
        collect = await make_collect()
        item = await add_item(collect, emotion_id="dddd0003", file_path="h/missing.png")
        fake_qdrant.points[int("dddd0003", 16)] = {"emotion_id": "dddd0003"}

        await lib.delete_item(item)

        assert int("dddd0003", 16) not in fake_qdrant.points
        assert await DBEmotionItem.filter(emotion_id="dddd0003").count() == 0


class TestMoveItems:
    async def test_moving_changes_the_set_and_the_vector_payload(self, db, fake_qdrant: FakeQdrant) -> None:
        collect = await make_collect()
        preset = await lib.create_preset_set(name="目标套组")
        await add_item(collect, emotion_id="eeee0001")
        await add_item(collect, emotion_id="eeee0002")
        fake_qdrant.points[int("eeee0001", 16)] = {"emotion_id": "eeee0001", "set_id": collect.id}
        fake_qdrant.points[int("eeee0002", 16)] = {"emotion_id": "eeee0002", "set_id": collect.id}

        moved = await lib.move_items(["eeee0001", "eeee0002", "ffff0000"], preset.id)

        assert moved == 2
        assert await DBEmotionItem.filter(set_id=preset.id).count() == 2
        assert fake_qdrant.points[int("eeee0001", 16)]["set_id"] == preset.id
        assert fake_qdrant.points[int("eeee0002", 16)]["set_id"] == preset.id
        assert ("set_payload",) == ("set_payload",)
        payloads = [call for call in fake_qdrant.calls if call[0] == "set_payload"]
        assert payloads and payloads[-1][1][1] == {"set_id": preset.id}

    async def test_a_failing_vector_sync_does_not_roll_back_the_rows(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        collect = await make_collect()
        preset = await lib.create_preset_set(name="目标套组")
        await add_item(collect, emotion_id="eeee0003")

        class _BrokenClient:
            async def set_payload(self, **_kwargs: Any) -> None:
                raise RuntimeError("qdrant down")

        async def _client() -> _BrokenClient:
            return _BrokenClient()

        monkeypatch.setattr(lib, "get_qdrant_client", _client)

        moved = await lib.move_items(["eeee0003"], preset.id)

        # 向量同步只记 WARNING；库表已经改完，搜索时的库表复核与下次回填会补上
        assert moved == 1
        assert await DBEmotionItem.filter(emotion_id="eeee0003", set_id=preset.id).count() == 1


class TestCopyItems:
    async def test_copy_creates_new_ids_and_reuses_the_file(self, db, files_dir: Path, fake_qdrant: FakeQdrant) -> None:
        collect = await make_collect()
        preset = await lib.create_preset_set(name="目标套组")
        source = await add_item(collect, emotion_id="abcd0001", file_path="h/same.png")
        fake_qdrant.points[int("abcd0001", 16)] = {"emotion_id": "abcd0001", "set_id": collect.id, "tags": ["标签"]}
        fake_qdrant.vectors[int("abcd0001", 16)] = [0.1, 0.2]

        created = await lib.copy_items(["abcd0001"], preset.id)

        assert len(created) == 1
        new_id = created[0]
        assert new_id != "abcd0001"
        copied = await DBEmotionItem.filter(emotion_id=new_id).first()
        assert copied is not None
        assert copied.set_id == preset.id
        assert copied.file_path == source.file_path
        # 新点：向量与载荷来自源点，ID 与 set_id 换掉
        assert fake_qdrant.vectors[int(new_id, 16)] == [0.1, 0.2]
        assert fake_qdrant.points[int(new_id, 16)]["set_id"] == preset.id
        assert fake_qdrant.points[int(new_id, 16)]["emotion_id"] == new_id

    async def test_copy_skips_the_vector_when_the_source_point_is_missing(self, db, fake_qdrant: FakeQdrant) -> None:
        collect = await make_collect()
        preset = await lib.create_preset_set(name="目标套组")
        await add_item(collect, emotion_id="abcd0002")

        created = await lib.copy_items(["abcd0002"], preset.id)

        assert len(created) == 1
        assert int(created[0], 16) not in fake_qdrant.points


class TestResolveEmotionFile:
    def test_plain_file_names_and_hash_paths_both_resolve(self, files_dir: Path) -> None:
        flat = lib.resolve_emotion_file("old_name_ab12cd.png")
        hashed = lib.resolve_emotion_file("h/" + "a" * 64 + ".png")

        assert flat == (files_dir / "old_name_ab12cd.png").resolve()
        assert hashed == (files_dir / "h" / ("a" * 64 + ".png")).resolve()

    def test_absolute_paths_only_keep_the_file_name(self, files_dir: Path) -> None:
        resolved = lib.resolve_emotion_file("/srv/old/data/emotions/x_ab12cd.png")
        assert resolved == (files_dir / "x_ab12cd.png").resolve()

    def test_parent_directory_escapes_are_rejected(self, files_dir: Path) -> None:
        assert lib.resolve_emotion_file("../secrets.yaml") is None
        assert lib.resolve_emotion_file("h/../../secrets.yaml") is None
        assert lib.resolve_emotion_file("") is None


class TestStoreBytesByHash:
    def test_the_same_content_is_written_once(self, files_dir: Path) -> None:
        data = b"GIF89a" + b"same-bytes"
        first_rel, first_digest = lib.store_bytes_by_hash(data, "gif")
        second_rel, second_digest = lib.store_bytes_by_hash(data, "gif")

        assert first_rel == second_rel == f"h/{first_digest}.gif"
        assert first_digest == second_digest
        assert len(list((files_dir / "h").iterdir())) == 1

    def test_different_content_lands_in_different_files(self, files_dir: Path) -> None:
        first_rel, _ = lib.store_bytes_by_hash(b"GIF89a-one", "gif")
        second_rel, _ = lib.store_bytes_by_hash(b"GIF89a-two", "gif")

        assert first_rel != second_rel
        assert len(list((files_dir / "h").iterdir())) == 2

    def test_the_extension_is_recognised_from_the_header(self) -> None:
        assert lib.detect_image_extension(b"\x89PNG\r\n\x1a\n....") == "png"
        assert lib.detect_image_extension(b"\xff\xd8\xff\xe0....") == "jpg"
        assert lib.detect_image_extension(b"GIF87a....") == "gif"
        assert lib.detect_image_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
        assert lib.detect_image_extension(b"BM\x00\x00....") == "bmp"
        assert lib.detect_image_extension(b"%PDF-1.7") is None
        assert lib.detect_image_extension(b"") is None


class TestVectorSearch:
    async def test_an_empty_set_list_does_not_touch_the_vector_store(self, fake_qdrant: FakeQdrant) -> None:
        assert await lib.vector_search([0.1], set_ids=[], limit=3) == []
        assert fake_qdrant.calls == []

    async def test_the_filter_carries_the_readable_set_ids(self, fake_qdrant: FakeQdrant) -> None:
        await lib.vector_search([0.1], set_ids={3, 1}, limit=5)

        assert fake_qdrant.calls[-1][0] == "search"
        _, query_filter, limit = fake_qdrant.calls[-1][1]
        condition = query_filter.must[0]
        assert condition.key == "set_id"
        assert sorted(condition.match.any) == [1, 3]
        assert limit == 5


class TestVectorFailureTolerance:
    async def test_upsert_swallows_a_dead_vector_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("qdrant down")

        class _Client:
            upsert = staticmethod(_boom)
            set_payload = staticmethod(_boom)
            delete = staticmethod(_boom)
            search = staticmethod(_boom)

        async def _client() -> _Client:
            return _Client()

        monkeypatch.setattr(lib, "get_qdrant_client", _client)

        await lib.vector_upsert("aaaa0009", [0.1], set_id=1, description="d", tags=[])
        assert await lib.vector_set_set_id(["aaaa0009"], 2) == 0
        assert await lib.vector_delete(["aaaa0009"]) == 0
        assert await lib.vector_search([0.1], set_ids={1}, limit=1) == []
