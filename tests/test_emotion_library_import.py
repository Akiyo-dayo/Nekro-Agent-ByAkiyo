"""存量 JSON 导入

## 这一层要保证什么

存量是**旧版全局一份**的 JSON：里面每条表情只记了来源路径与时间，没有归属。导入把它整体放进
一个「存量（待整理）」套组，且只分给平台方实例——那些图是所有人一起收的，直接开给租户会
把别人的图送出去。

导入必须**幂等**：插件初始化的判定是「JSON 条数比已导入的多才比一遍」，重复调用是常态。
它也必须**不改旧数据**：本函数不读也不写插件存储里那一行 JSON，回滚时旧 JSON 还是好的。

坏 JSON 不抛：一份坏掉的旧数据不该挡住插件启动，记 WARNING 返回全 0 就够。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_emotion_item import DBEmotionItem
from nekro_agent.models.db_emotion_set import DBEmotionSet
from nekro_agent.models.db_emotion_set_assignment import DBEmotionSetAssignment
from nekro_agent.services import emotion_library as lib

PNG = b"\x89PNG\r\n\x1a\n" + b"legacy-image"


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
def files_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "emotions"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lib, "EMOTION_FILES_DIR", target)
    return target


@pytest.fixture
def vector_calls(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[List[str], int]]:
    """记下每次 `vector_set_set_id` 的入参，避免真去连 Qdrant"""
    calls: List[Tuple[List[str], int]] = []

    async def _record(emotion_ids: Sequence[str], set_id: int) -> int:
        calls.append((list(emotion_ids), int(set_id)))
        return len(emotion_ids)

    monkeypatch.setattr(lib, "vector_set_set_id", _record)
    return calls


def legacy_json(entries: Dict[str, Dict[str, Any]], *, recent: Optional[List[str]] = None) -> str:
    return json.dumps(
        {
            "emotions": entries,
            "recent_emotion_ids": recent or [],
            "last_updated": "2026-01-01T00:00:00+00:00",
        },
        ensure_ascii=False,
    )


def entry(*, description: str = "描述", tags: Optional[List[str]] = None, file_path: str = "") -> Dict[str, Any]:
    return {
        "description": description,
        "tags": tags or ["标签"],
        "source_path": "https://example.invalid/src",
        "file_path": file_path,
        "added_time": 1_700_000_000,
        "last_updated": 1_700_000_100,
    }


class TestImportIntoLegacySet:
    async def test_every_entry_lands_in_the_legacy_set(self, db, files_dir: Path, vector_calls) -> None:
        (files_dir / "first_aa11bb.png").write_bytes(PNG)
        (files_dir / "second_cc22dd.png").write_bytes(PNG + b"-2")
        raw = legacy_json(
            {
                "aaaa0001": entry(description="第一张", file_path="first_aa11bb.png"),
                "aaaa0002": entry(description="第二张", file_path="second_cc22dd.png"),
            },
        )

        counts = await lib.import_legacy_store(raw)

        assert counts["total"] == 2
        assert counts["inserted"] == 2
        assert counts["existing"] == 0
        assert counts["missing_files"] == 0
        assert await DBEmotionItem.all().count() == 2
        legacy = await DBEmotionSet.filter(kind=lib.KIND_LEGACY).first()
        assert legacy is not None
        assert legacy.name == lib.LEGACY_SET_NAME == "存量（待整理）"
        assert legacy.category == lib.LEGACY_SET_CATEGORY == "未整理"
        assert await DBEmotionItem.filter(set_id=legacy.id).count() == 2

    async def test_the_legacy_set_is_only_assigned_to_platform_instances(self, db, files_dir: Path, vector_calls) -> None:
        await lib.import_legacy_store(legacy_json({"aaaa0001": entry()}))

        legacy = await DBEmotionSet.filter(kind=lib.KIND_LEGACY).first()
        assert legacy is not None
        targets = await DBEmotionSetAssignment.filter(set_id=legacy.id).values_list("target", flat=True)
        assert list(targets) == [lib.TARGET_PLATFORM_INSTANCES]

    async def test_fields_survive_the_round_trip(self, db, files_dir: Path, vector_calls) -> None:
        raw = legacy_json(
            {
                "aaaa0001": {
                    "description": "带标签的",
                    "tags": ["日常", "节日"],
                    "source_path": "群里的那张",
                    "file_path": "note_ee33ff.png",
                    "added_time": 1_650_000_000,
                    "last_updated": 1_650_000_500,
                },
            },
        )
        counted = await lib.import_legacy_store(raw)

        assert counted["missing_files"] == 1  # 文件不在
        item = await DBEmotionItem.filter(emotion_id="aaaa0001").first()
        assert item is not None
        assert item.description == "带标签的"
        assert item.tags == ["日常", "节日"]
        assert item.source_path == "群里的那张"
        assert item.file_path == "note_ee33ff.png"
        assert item.file_sha256 == ""
        assert (item.added_at, item.updated_at) == (1_650_000_000, 1_650_000_500)
        assert item.collected_chat_key == ""

    async def test_the_hash_is_computed_when_the_file_is_there(self, db, files_dir: Path, vector_calls) -> None:
        (files_dir / "hash_ab12cd.png").write_bytes(PNG)

        await lib.import_legacy_store(legacy_json({"aaaa0001": entry(file_path="hash_ab12cd.png")}))

        item = await DBEmotionItem.filter(emotion_id="aaaa0001").first()
        assert item is not None
        assert item.file_sha256 == lib.sha256_of_bytes(PNG)

    async def test_absolute_paths_only_keep_the_file_name(self, db, files_dir: Path, vector_calls) -> None:
        (files_dir / "abs_ab12cd.png").write_bytes(PNG)

        await lib.import_legacy_store(legacy_json({"aaaa0001": entry(file_path="/srv/old/emotions/abs_ab12cd.png")}))

        item = await DBEmotionItem.filter(emotion_id="aaaa0001").first()
        assert item is not None
        assert item.file_path == "abs_ab12cd.png"
        assert item.file_sha256 == lib.sha256_of_bytes(PNG)

    async def test_missing_files_are_counted_not_fatal(self, db, files_dir: Path, vector_calls) -> None:
        raw = legacy_json(
            {
                "aaaa0001": entry(file_path="gone1.png"),
                "aaaa0002": entry(file_path=""),
                "aaaa0003": entry(file_path="gone3.png"),
            },
        )

        counts = await lib.import_legacy_store(raw)

        assert counts["inserted"] == 3
        assert counts["missing_files"] == 3
        assert await DBEmotionItem.all().count() == 3


class TestIdempotence:
    async def test_a_second_import_inserts_nothing(self, db, files_dir: Path, vector_calls) -> None:
        raw = legacy_json({"aaaa0001": entry(), "aaaa0002": entry()})
        await lib.import_legacy_store(raw)

        second = await lib.import_legacy_store(raw)

        assert second["total"] == 2
        assert second["inserted"] == 0
        assert second["existing"] == 2
        assert await DBEmotionItem.all().count() == 2
        assert await DBEmotionSet.filter(kind=lib.KIND_LEGACY).count() == 1

    async def test_existing_rows_are_not_rewritten(self, db, files_dir: Path, vector_calls) -> None:
        await lib.import_legacy_store(legacy_json({"aaaa0001": entry(description="原来的")}))
        # 平台方在控制台改过这条的描述
        await DBEmotionItem.filter(emotion_id="aaaa0001").update(description="改过的")

        await lib.import_legacy_store(legacy_json({"aaaa0001": entry(description="JSON 里的")}))

        item = await DBEmotionItem.filter(emotion_id="aaaa0001").first()
        assert item is not None
        assert item.description == "改过的"

    async def test_new_entries_of_a_later_import_still_land_in_the_legacy_set(self, db, files_dir: Path, vector_calls) -> None:
        await lib.import_legacy_store(legacy_json({"aaaa0001": entry()}))
        counts = await lib.import_legacy_store(legacy_json({"aaaa0001": entry(), "aaaa0002": entry()}))

        assert counts["inserted"] == 1
        assert counts["existing"] == 1
        legacy = await DBEmotionSet.filter(kind=lib.KIND_LEGACY).first()
        assert legacy is not None
        assert await DBEmotionItem.filter(set_id=legacy.id).count() == 2


class TestVectorBackfill:
    async def test_newly_inserted_ids_get_their_set_id_batched_by_hundred(self, db, files_dir: Path, vector_calls) -> None:
        entries = {f"{index:08x}": entry() for index in range(250)}
        counts = await lib.import_legacy_store(legacy_json(entries))

        assert counts["inserted"] == 250
        assert counts["vector_batches"] == 3
        assert [len(ids) for ids, _ in vector_calls] == [100, 100, 50]
        legacy = await DBEmotionSet.filter(kind=lib.KIND_LEGACY).first()
        assert legacy is not None
        assert {set_id for _, set_id in vector_calls} == {legacy.id}

    async def test_a_repeat_import_does_not_touch_the_vector_store(self, db, files_dir: Path, vector_calls) -> None:
        raw = legacy_json({"aaaa0001": entry()})
        await lib.import_legacy_store(raw)
        vector_calls.clear()

        counts = await lib.import_legacy_store(raw)

        assert counts["vector_batches"] == 0
        assert vector_calls == []


class TestBadInput:
    async def test_broken_json_returns_zeroes_and_does_not_raise(self, db, files_dir: Path, vector_calls) -> None:
        counts = await lib.import_legacy_store("{not json at all")

        assert counts == {
            "total": 0,
            "inserted": 0,
            "existing": 0,
            "missing_files": 0,
            "vector_batches": 0,
        }
        assert await DBEmotionSet.filter(kind=lib.KIND_LEGACY).count() == 0

    async def test_an_empty_or_missing_store_is_a_no_op(self, db, files_dir: Path, vector_calls) -> None:
        assert (await lib.import_legacy_store(None))["total"] == 0
        assert (await lib.import_legacy_store(""))["total"] == 0
        assert (await lib.import_legacy_store(json.dumps({"emotions": {}})))["total"] == 0

    async def test_a_json_array_instead_of_an_object_is_rejected(self, db, files_dir: Path, vector_calls) -> None:
        counts = await lib.import_legacy_store(json.dumps([1, 2, 3]))
        assert counts["total"] == 0

    async def test_an_entry_that_is_not_an_object_does_not_break_the_import(self, db, files_dir: Path, vector_calls) -> None:
        raw = json.dumps({"emotions": {"aaaa0001": "not-a-dict", "aaaa0002": entry()}})

        counts = await lib.import_legacy_store(raw)

        assert counts["inserted"] == 2
        broken = await DBEmotionItem.filter(emotion_id="aaaa0001").first()
        assert broken is not None
        assert broken.description == ""
        assert broken.tags == []
        assert broken.file_path == ""


class TestTheFunctionDoesNotTouchPluginStorage:
    async def test_no_plugin_data_rows_are_read_or_written(self, db, files_dir: Path, vector_calls) -> None:
        from nekro_agent.models.db_plugin_data import DBPluginData

        await DBPluginData.create(
            plugin_key="KroMiose.emotion",
            data_key="emotion_store",
            data_value=legacy_json({"aaaa0001": entry()}),
            target_chat_key="",
            target_user_id="",
        )

        await lib.import_legacy_store(legacy_json({"aaaa0001": entry(), "aaaa0002": entry()}))

        row = await DBPluginData.filter(plugin_key="KroMiose.emotion", data_key="emotion_store").first()
        assert row is not None
        # 旧 JSON 原样留着：回滚时还要用它
        assert json.loads(row.data_value)["emotions"].keys() == {"aaaa0001"}
