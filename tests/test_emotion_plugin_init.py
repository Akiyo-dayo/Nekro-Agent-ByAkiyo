"""插件启动时的存量导入与向量回填

## 这一层钉的是什么

旧版把整库存在插件存储的一行 JSON 里。改造后库表是权威，那一行**不能改也不能删**：它是
回滚的唯一凭据。所以启动路径必须满足三件事，任何一件做错都不会报错，只会悄悄毁掉回滚：

1. 导入只做一次（标记里记着导入时看到的条数，条数没变就整段跳过）；
2. 已经删掉的表情不会被下一次启动重新导入（删除只删库表行，不回写 JSON）；
3. 导入之后按套组回填向量载荷里的 `set_id`，且只回填、不重建。

用例把 `plugin.store` 换成一个内存字典、把 `resync_vector_set_ids` 换成记账桩，
其余走真实库表与真实服务层导入函数。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_emotion_item import DBEmotionItem
from nekro_agent.models.db_emotion_set import DBEmotionSet
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.services import emotion_library as lib
from plugins.builtin import emotion as plugin_module

ADAPTER = "onebot_v11"
LEGACY_JSON = json.dumps(
    {
        "emotions": {
            "aaaa0001": {
                "emotion_id": "aaaa0001",
                "file_path": "cat.png",
                "description": "一只猫",
                "tags": ["猫"],
            },
            "aaaa0002": {
                "emotion_id": "aaaa0002",
                "file_path": "dog.png",
                "description": "一只狗",
                "tags": ["狗"],
            },
        },
    },
)



@pytest.fixture
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    files = tmp_path / "emotions"
    files.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lib, "EMOTION_FILES_DIR", files)
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    lib.clear_scope_cache()
    try:
        yield files
    finally:
        lib.clear_scope_cache()
        await Tortoise.close_connections()


@pytest.fixture
def store() -> "StoreRows":
    """用真实的 `plugin_data` 表，只包一层方便的读写

    `plugin.store` 是只读属性（每次访问现造一个 `PluginStore`），不能 monkeypatch；而它的
    实现就是这张表的一行，直接写表反而更接近生产：键名、空串频道键、覆盖写都走真实代码。
    """
    return StoreRows()


class StoreRows:
    async def seed(self, key: str, value: str) -> None:
        await DBPluginData.create(
            plugin_key=plugin_module.plugin.key,
            data_key=key,
            data_value=value,
            target_chat_key="",
            target_user_id="",
        )

    async def read(self, key: str) -> Optional[str]:
        row = await DBPluginData.filter(plugin_key=plugin_module.plugin.key, data_key=key).first()
        return row.data_value if row is not None else None

    async def keys(self) -> Dict[str, str]:
        rows = await DBPluginData.filter(plugin_key=plugin_module.plugin.key)
        return {row.data_key: row.data_value for row in rows}


@pytest.fixture
def resync(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """回填换成记账桩：真实现要连 Qdrant"""
    calls: List[str] = []

    async def _resync(**_kwargs: Any) -> Dict[str, int]:
        # 插件会把「补缺点」的嵌入回调传进来（见 test_emotion_vector_setid_backfill）
        calls.append("resync")
        return {"sets": 1, "points": 2, "batches": 1}

    monkeypatch.setattr(lib, "resync_vector_set_ids", _resync)
    return calls


class TestLegacyImport:
    async def test_the_json_is_imported_into_the_legacy_set(self, db, store, resync):
        await store.seed("emotion_store", LEGACY_JSON)

        await plugin_module._import_legacy_store_once()

        legacy = await DBEmotionSet.get(kind=lib.KIND_LEGACY)
        rows = await DBEmotionItem.filter(set_id=legacy.id)
        assert {row.emotion_id for row in rows} == {"aaaa0001", "aaaa0002"}
        assert legacy.name == lib.LEGACY_SET_NAME

    async def test_the_marker_records_the_count(self, db, store, resync):
        await store.seed("emotion_store", LEGACY_JSON)

        await plugin_module._import_legacy_store_once()

        assert await store.read(lib.LEGACY_IMPORT_MARKER) == "2"

    async def test_the_json_row_is_never_rewritten(self, db, store, resync):
        await store.seed("emotion_store", LEGACY_JSON)
        before = await store.read("emotion_store")

        await plugin_module._import_legacy_store_once()
        await plugin_module._import_legacy_store_once()

        assert await store.read("emotion_store") == before

    async def test_a_second_start_does_not_import_again(self, db, store, resync, monkeypatch: pytest.MonkeyPatch):
        await store.seed("emotion_store", LEGACY_JSON)
        await plugin_module._import_legacy_store_once()

        called: List[str] = []

        async def _spy(raw: Optional[str]) -> Dict[str, int]:
            called.append(str(raw))
            return {"total": 0, "inserted": 0, "existing": 0, "missing_files": 0, "vector_batches": 0}

        monkeypatch.setattr(lib, "import_legacy_store", _spy)
        await plugin_module._import_legacy_store_once()

        assert called == []
        assert await DBEmotionItem.all().count() == 2

    async def test_a_deleted_item_is_not_brought_back(self, db, store, resync):
        await store.seed("emotion_store", LEGACY_JSON)
        await plugin_module._import_legacy_store_once()
        await DBEmotionItem.filter(emotion_id="aaaa0001").delete()

        # 重启：标记的条数没变，整段跳过 —— JSON 里那条不会再被导入
        await plugin_module._import_legacy_store_once()

        assert not await DBEmotionItem.filter(emotion_id="aaaa0001").exists()
        assert await DBEmotionItem.filter(emotion_id="aaaa0002").exists()

    async def test_an_empty_store_does_nothing(self, db, store, resync):
        await plugin_module._import_legacy_store_once()

        assert await store.keys() == {}
        assert await DBEmotionSet.all().count() == 0

    async def test_a_broken_json_does_not_raise(self, db, store, resync):
        await store.seed("emotion_store", "{not json")

        await plugin_module._import_legacy_store_once()

        assert await store.read(lib.LEGACY_IMPORT_MARKER) is None


class TestResyncIsBackground:
    async def test_the_resync_runs_after_the_import(self, db, store, resync, monkeypatch: pytest.MonkeyPatch):
        """回填放后台任务：大库上要跑几十秒，不能卡住插件加载"""
        await store.seed("emotion_store", LEGACY_JSON)
        created: List[Any] = []

        class _Client:
            async def get_collections(self) -> Any:
                return type("Collections", (), {"collections": [type("C", (), {"name": lib.EMOTION_VECTOR_COLLECTION})()]})()

        async def _client() -> Any:
            return _Client()

        def _create_task(coro: Any) -> Any:
            # 必须是**同步**函数：`asyncio.create_task` 不是协程，写成 `async def` 只会造出一个
            # 没人 await 的协程对象，任务根本没建，断言会假绿
            created.append(coro)
            return coro

        monkeypatch.setattr(plugin_module, "get_qdrant_client", _client)
        monkeypatch.setattr(plugin_module.asyncio, "create_task", _create_task)

        await plugin_module.init_vector_db()

        assert len(created) == 1
        assert resync == []
        await created[0]
        assert resync == ["resync"]

    async def test_a_missing_collection_is_created(self, db, store, resync, monkeypatch: pytest.MonkeyPatch):
        created: List[str] = []

        class _Client:
            async def get_collections(self) -> Any:
                return type("Collections", (), {"collections": []})()

            async def create_collection(self, *, collection_name: str, vectors_config: Any) -> None:
                created.append(collection_name)

        async def _client() -> Any:
            return _Client()

        def _no_task(coro: Any) -> Any:
            coro.close()
            return None

        monkeypatch.setattr(plugin_module, "get_qdrant_client", _client)
        monkeypatch.setattr(plugin_module.asyncio, "create_task", _no_task)

        await plugin_module.init_vector_db()

        assert created == [lib.EMOTION_VECTOR_COLLECTION]

    async def test_a_resync_failure_is_only_a_warning(self, db, store, monkeypatch: pytest.MonkeyPatch):
        async def _boom() -> Dict[str, int]:
            raise RuntimeError("qdrant down")

        monkeypatch.setattr(lib, "resync_vector_set_ids", _boom)

        await plugin_module._resync_vector_set_ids_in_background()
