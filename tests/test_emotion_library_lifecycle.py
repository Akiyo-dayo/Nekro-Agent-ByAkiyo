"""生命周期：彻底删除实例、实例归属迁移、租户划转、换会话标识

## 为什么这些要在服务层单独钉一遍

表情的归属挂在套组上，而套组、分配、白板令牌都散在三张表里，谁都不会因为「实例那一行没了」
被数据库顺手带走：`emotion_set` 没有指向实例的外键（`owner_*` 只是两列字符串），
`plugin_data` 里的白板令牌更是与实例毫无关系。漏一步的后果都是**静默**的：

- 漏删收集库：`instance_key` 是可重新认领的 slug，同名的新实例（可能归另一个租户）
  打开会话就直接读到上一个实例收集的表情；
- 漏删白板令牌：实例没了，凭旧链接还能匿名看那块板；
- 漏迁收集库：新实例第一次收集时建一个空库，旧库连同样例永远留在一个没人指向的套组里；
- 漏清 `instance` 分配：划转之后平台方给前一个租户的那份表情直接送进新租户手里。

## 与真实调用链的关系

`purge_instance` / `migrate_instance_ownership` / `bind_tenant_instances` 的接线各自有用例
（`test_emotion_library_wiring`）；这里钉的是服务层那几个函数自己的语义，加上白板键算法与
插件一致这一条。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import pytest
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_emotion_item import DBEmotionItem
from nekro_agent.models.db_emotion_set import DBEmotionSet
from nekro_agent.models.db_emotion_set_assignment import DBEmotionSetAssignment
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.services import emotion_library as lib

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OTHER_INSTANCE = "inst2"
TENANT_ID = 21


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
def vectors(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """桩掉向量侧：记下删了哪些点，不连 Qdrant"""
    state: Dict[str, Any] = {"deleted": [], "payloads": []}

    async def _delete(emotion_ids: Sequence[str]) -> int:
        state["deleted"].extend(emotion_ids)
        return len(emotion_ids)

    async def _set_set_id(emotion_ids: Sequence[str], set_id: int) -> int:
        state["payloads"].append((list(emotion_ids), int(set_id)))
        return len(emotion_ids)

    monkeypatch.setattr(lib, "vector_delete", _delete)
    monkeypatch.setattr(lib, "vector_set_set_id", _set_set_id)
    return state


async def instance(instance_key: str, *, tenant_id: Optional[int] = None) -> DBAdapterInstance:
    return await DBAdapterInstance.create(
        adapter_key=ADAPTER,
        instance_key=instance_key,
        display_name=instance_key,
        status="online",
        provider="remote_onebot",
        tenant_id=tenant_id,
        metadata_json="",
    )


async def collect_with(instance_key: str, emotion_ids: Sequence[str]) -> DBEmotionSet:
    collect = await lib.get_or_create_collect_set(ADAPTER, instance_key)
    for emotion_id in emotion_ids:
        await DBEmotionItem.create(
            emotion_id=emotion_id,
            set_id=collect.id,
            description="描述",
            tags=[],
            file_path="",
            file_sha256="",
            added_at=1,
            updated_at=1,
        )
    return collect


class TestWhiteboardTokenKey:
    def test_the_key_matches_the_whiteboard_plugin_algorithm(self) -> None:
        """核心里写的这份必须与插件算出来的一模一样（否则删 / 迁都落不到那一行）"""
        from plugins.builtin import whiteboard

        for adapter_key, instance_key in (
            (ADAPTER, INSTANCE),
            ("email", ""),
            ("", ""),
            ("onebot_v11", "with space"),
        ):
            assert lib.whiteboard_view_token_store_key(adapter_key, instance_key) == (
                whiteboard._view_token_store_key(adapter_key, instance_key)  # noqa: SLF001
            )

    def test_the_key_is_a_prefix_plus_thirty_two_hex_characters(self) -> None:
        key = lib.whiteboard_view_token_store_key(ADAPTER, INSTANCE)

        assert key.startswith("view_token:")
        digest = key[len("view_token:") :]
        assert len(digest) == 32
        assert digest == digest.lower()
        int(digest, 16)

    def test_the_two_halves_are_separated_by_a_nul(self) -> None:
        """不加分隔符的话 ('ab', 'c') 与 ('a', 'bc') 会算出同一个键"""
        expected = hashlib.sha256(f"{ADAPTER}\x00{INSTANCE}".encode()).hexdigest()[:32]
        assert lib.whiteboard_view_token_store_key(ADAPTER, INSTANCE) == f"view_token:{expected}"
        assert lib.whiteboard_view_token_store_key("ab", "c") != lib.whiteboard_view_token_store_key("a", "bc")

    def test_it_matches_the_whiteboard_plugin_key_of_the_store_row(self) -> None:
        assert lib.WHITEBOARD_PLUGIN_KEY == "nekro.whiteboard"


class TestPurgeInstanceEmotions:
    async def test_it_removes_the_collect_set_its_items_and_the_instance_assignments(
        self, db, files_dir: Path, vectors,
    ) -> None:
        await instance(INSTANCE)
        collect = await collect_with(INSTANCE, ["aaaa0001", "aaaa0002"])
        preset = await lib.create_preset_set(name="平台方预设")
        await lib.add_assignment(preset.id, target=lib.TARGET_INSTANCE, adapter_key=ADAPTER, instance_key=INSTANCE)

        counts = await lib.purge_instance_emotions(ADAPTER, INSTANCE)

        assert counts["sets"] == 1
        assert counts["items"] == 2
        assert counts["assignments"] == 1
        assert await DBEmotionSet.filter(id=collect.id).count() == 0
        assert await DBEmotionItem.all().count() == 0
        assert await DBEmotionSetAssignment.filter(set_id=preset.id).count() == 0
        # 预设套组本身不能跟着没：它属于平台方
        assert await DBEmotionSet.filter(id=preset.id).count() == 1
        assert sorted(vectors["deleted"]) == ["aaaa0001", "aaaa0002"]

    async def test_it_leaves_another_instances_data_alone(self, db, files_dir: Path, vectors) -> None:
        await instance(INSTANCE)
        await instance(OTHER_INSTANCE)
        mine = await collect_with(INSTANCE, ["aaaa0001"])
        theirs = await collect_with(OTHER_INSTANCE, ["bbbb0001"])
        preset = await lib.create_preset_set(name="平台方预设")
        await lib.add_assignment(preset.id, target=lib.TARGET_INSTANCE, adapter_key=ADAPTER, instance_key=OTHER_INSTANCE)

        await lib.purge_instance_emotions(ADAPTER, INSTANCE)

        assert await DBEmotionSet.filter(id=theirs.id).count() == 1
        assert await DBEmotionItem.filter(emotion_id="bbbb0001").count() == 1
        assert await DBEmotionItem.filter(emotion_id="aaaa0001").count() == 0
        assert await DBEmotionSetAssignment.filter(set_id=preset.id).count() == 1
        assert await DBEmotionSet.filter(id=mine.id).count() == 0

    async def test_it_removes_the_whiteboard_view_token(self, db, files_dir: Path, vectors) -> None:
        await instance(INSTANCE)
        await DBPluginData.create(
            plugin_key=lib.WHITEBOARD_PLUGIN_KEY,
            data_key=lib.whiteboard_view_token_store_key(ADAPTER, INSTANCE),
            data_value="token-for-this-instance",
            target_chat_key="",
            target_user_id="",
        )
        await DBPluginData.create(
            plugin_key=lib.WHITEBOARD_PLUGIN_KEY,
            data_key=lib.whiteboard_view_token_store_key(ADAPTER, OTHER_INSTANCE),
            data_value="token-for-another-instance",
            target_chat_key="",
            target_user_id="",
        )

        counts = await lib.purge_instance_emotions(ADAPTER, INSTANCE)

        assert counts["whiteboard_tokens"] == 1
        assert await DBPluginData.filter(plugin_key=lib.WHITEBOARD_PLUGIN_KEY).count() == 1
        remaining = await DBPluginData.filter(plugin_key=lib.WHITEBOARD_PLUGIN_KEY).first()
        assert remaining is not None
        assert remaining.data_key == lib.whiteboard_view_token_store_key(ADAPTER, OTHER_INSTANCE)

    async def test_a_file_shared_with_another_set_survives(self, db, files_dir: Path, vectors) -> None:
        await instance(INSTANCE)
        shared = b"\x89PNG\r\n\x1a\nshared"
        rel_path, digest = lib.store_bytes_by_hash(shared, "png")
        collect = await collect_with(INSTANCE, [])
        preset = await lib.create_preset_set(name="平台方预设")
        await DBEmotionItem.create(
            emotion_id="aaaa0001",
            set_id=collect.id,
            description="我的",
            tags=[],
            file_path=rel_path,
            file_sha256=digest,
            added_at=1,
            updated_at=1,
        )
        await DBEmotionItem.create(
            emotion_id="aaaa0009",
            set_id=preset.id,
            description="平台方也有一份",
            tags=[],
            file_path=rel_path,
            file_sha256=digest,
            added_at=1,
            updated_at=1,
        )

        counts = await lib.purge_instance_emotions(ADAPTER, INSTANCE)

        assert counts["files"] == 0
        assert (files_dir / rel_path).is_file()

    async def test_pre_collected_refs_are_deleted_after_the_rows_are_gone(self, db, files_dir: Path, vectors) -> None:
        """`purge_instance` 在事务里先删行，再带着提前取好的引用调用本函数"""
        await instance(INSTANCE)
        collect = await collect_with(INSTANCE, ["aaaa0001", "aaaa0002"])
        ids, files = await lib.collect_instance_emotion_refs(ADAPTER, INSTANCE)
        assert sorted(ids) == ["aaaa0001", "aaaa0002"]
        await DBEmotionSet.filter(id=collect.id).delete()

        counts = await lib.purge_instance_emotions(ADAPTER, INSTANCE, emotion_ids=ids, file_paths=files)

        assert counts["sets"] == 0
        assert counts["items"] == 0
        assert sorted(vectors["deleted"]) == ["aaaa0001", "aaaa0002"]

    async def test_purging_an_instance_without_emotions_is_harmless(self, db, files_dir: Path, vectors) -> None:
        await instance(INSTANCE)

        counts = await lib.purge_instance_emotions(ADAPTER, INSTANCE)

        assert counts == {"sets": 0, "items": 0, "assignments": 0, "vectors": 0, "files": 0, "whiteboard_tokens": 0}


class TestMigrateInstanceEmotions:
    async def test_the_collect_set_follows_the_instance(self, db, files_dir: Path, vectors) -> None:
        combine = await collect_with(INSTANCE, ["aaaa0001"])

        counts = await lib.migrate_instance_emotions(ADAPTER, INSTANCE, ADAPTER, OTHER_INSTANCE)

        assert counts["sets"] == 1
        migrated = await DBEmotionSet.get(id=combine.id)
        assert (migrated.owner_adapter_key, migrated.owner_instance_key) == (ADAPTER, OTHER_INSTANCE)
        assert await DBEmotionItem.filter(emotion_id="aaaa0001", set_id=combine.id).count() == 1

    async def test_an_existing_target_collect_set_absorbs_the_items(self, db, files_dir: Path, vectors) -> None:
        source = await collect_with(INSTANCE, ["aaaa0001", "aaaa0002"])
        target = await collect_with(OTHER_INSTANCE, ["bbbb0001"])

        counts = await lib.migrate_instance_emotions(ADAPTER, INSTANCE, ADAPTER, OTHER_INSTANCE)

        assert counts["sets"] == 1
        assert counts["moved_items"] == 2
        assert await DBEmotionSet.filter(kind=lib.KIND_COLLECT).count() == 1
        assert await DBEmotionItem.filter(set_id=target.id).count() == 3
        assert await DBEmotionSet.filter(id=source.id).count() == 0
        # 移动会同步向量载荷里的 set_id
        assert vectors["payloads"] == [(["aaaa0001", "aaaa0002"], target.id)]

    async def test_the_instance_assignments_move_with_it(self, db, files_dir: Path, vectors) -> None:
        preset = await lib.create_preset_set(name="平台方预设")
        await lib.add_assignment(preset.id, target=lib.TARGET_INSTANCE, adapter_key=ADAPTER, instance_key=INSTANCE)

        counts = await lib.migrate_instance_emotions(ADAPTER, INSTANCE, ADAPTER, OTHER_INSTANCE)

        assert counts["assignments"] == 1
        row = await DBEmotionSetAssignment.filter(set_id=preset.id).first()
        assert row is not None
        assert (row.adapter_key, row.instance_key) == (ADAPTER, OTHER_INSTANCE)
        assert await DBEmotionSetAssignment.filter(instance_key=INSTANCE).count() == 0

    async def test_the_whiteboard_token_moves_to_the_new_key(self, db, files_dir: Path, vectors) -> None:
        await DBPluginData.create(
            plugin_key=lib.WHITEBOARD_PLUGIN_KEY,
            data_key=lib.whiteboard_view_token_store_key(ADAPTER, INSTANCE),
            data_value="token",
            target_chat_key="",
            target_user_id="",
        )

        await lib.migrate_instance_emotions(ADAPTER, INSTANCE, ADAPTER, OTHER_INSTANCE)

        rows = await DBPluginData.filter(plugin_key=lib.WHITEBOARD_PLUGIN_KEY).all()
        assert len(rows) == 1
        assert rows[0].data_key == lib.whiteboard_view_token_store_key(ADAPTER, OTHER_INSTANCE)
        assert rows[0].data_value == "token"

    async def test_an_existing_target_token_wins_and_the_source_row_is_dropped(self, db, files_dir: Path, vectors) -> None:
        await DBPluginData.create(
            plugin_key=lib.WHITEBOARD_PLUGIN_KEY,
            data_key=lib.whiteboard_view_token_store_key(ADAPTER, INSTANCE),
            data_value="old",
            target_chat_key="",
            target_user_id="",
        )
        await DBPluginData.create(
            plugin_key=lib.WHITEBOARD_PLUGIN_KEY,
            data_key=lib.whiteboard_view_token_store_key(ADAPTER, OTHER_INSTANCE),
            data_value="new",
            target_chat_key="",
            target_user_id="",
        )

        await lib.migrate_instance_emotions(ADAPTER, INSTANCE, ADAPTER, OTHER_INSTANCE)

        rows = await DBPluginData.filter(plugin_key=lib.WHITEBOARD_PLUGIN_KEY).all()
        assert len(rows) == 1
        assert rows[0].data_value == "new"

    async def test_migrating_to_itself_changes_nothing(self, db, files_dir: Path, vectors) -> None:
        await collect_with(INSTANCE, ["aaaa0001"])

        counts = await lib.migrate_instance_emotions(ADAPTER, INSTANCE, ADAPTER, INSTANCE)

        assert counts == {"sets": 0, "assignments": 0, "moved_items": 0, "whiteboard_tokens": 0}
        assert await DBEmotionSet.filter(kind=lib.KIND_COLLECT, owner_instance_key=INSTANCE).count() == 1

    async def test_the_residual_counts_go_to_zero_after_migration(self, db, files_dir: Path, vectors) -> None:
        from nekro_agent.services.chat_key_migration import _count_instance_residual

        await instance(INSTANCE)
        await instance(OTHER_INSTANCE)
        await collect_with(INSTANCE, ["aaaa0001"])
        preset = await lib.create_preset_set(name="平台方预设")
        await lib.add_assignment(preset.id, target=lib.TARGET_INSTANCE, adapter_key=ADAPTER, instance_key=INSTANCE)

        before = await _count_instance_residual(ADAPTER, INSTANCE)
        assert before["emotion_set"] == 1
        assert before["emotion_set_assignment"] == 1

        await lib.migrate_instance_emotions(ADAPTER, INSTANCE, ADAPTER, OTHER_INSTANCE)

        after = await _count_instance_residual(ADAPTER, INSTANCE)
        assert after["emotion_set"] == 0
        assert after["emotion_set_assignment"] == 0


class TestTenantTransferHelpers:
    async def test_revoking_instance_assignments_leaves_the_other_targets(self, db, files_dir: Path, vectors) -> None:
        preset = await lib.create_preset_set(name="平台方预设")
        await lib.add_assignment(preset.id, target=lib.TARGET_INSTANCE, adapter_key=ADAPTER, instance_key=INSTANCE)
        await lib.add_assignment(preset.id, target=lib.TARGET_ALL_INSTANCES)
        await lib.add_assignment(preset.id, target=lib.TARGET_PLATFORM_INSTANCES)

        revoked = await lib.revoke_instance_assignments(ADAPTER, INSTANCE)

        assert revoked == 1
        targets = sorted(await DBEmotionSetAssignment.filter(set_id=preset.id).values_list("target", flat=True))
        assert targets == [lib.TARGET_ALL_INSTANCES, lib.TARGET_PLATFORM_INSTANCES]

    async def test_revoking_twice_is_a_no_op(self, db, files_dir: Path, vectors) -> None:
        assert await lib.revoke_instance_assignments(ADAPTER, INSTANCE) == 0

    async def test_clearing_a_collect_set_empties_it_but_keeps_the_set(self, db, files_dir: Path, vectors) -> None:
        collect = await collect_with(INSTANCE, ["aaaa0001", "aaaa0002"])

        counts = await lib.clear_collect_set(ADAPTER, INSTANCE)

        assert counts == {"sets": 1, "items": 2, "vectors": 2, "files": 0}
        assert await DBEmotionSet.filter(id=collect.id).count() == 1
        assert await DBEmotionItem.filter(set_id=collect.id).count() == 0
        assert sorted(vectors["deleted"]) == ["aaaa0001", "aaaa0002"]

    async def test_clearing_does_not_create_a_collect_set(self, db, files_dir: Path, vectors) -> None:
        counts = await lib.clear_collect_set(ADAPTER, INSTANCE)

        assert counts == {"sets": 0, "items": 0, "vectors": 0, "files": 0}
        assert await DBEmotionSet.filter(kind=lib.KIND_COLLECT).count() == 0

    async def test_counting_reports_items_and_assignments(self, db, files_dir: Path, vectors) -> None:
        await collect_with(INSTANCE, ["aaaa0001", "aaaa0002"])
        preset = await lib.create_preset_set(name="平台方预设")
        await lib.add_assignment(preset.id, target=lib.TARGET_INSTANCE, adapter_key=ADAPTER, instance_key=INSTANCE)

        counts = await lib.count_instance_emotions(ADAPTER, INSTANCE)

        assert counts == {"sets": 1, "items": 2, "assignments": 1}

    async def test_counting_an_instance_without_data_reports_zeroes(self, db, files_dir: Path, vectors) -> None:
        assert await lib.count_instance_emotions(ADAPTER, INSTANCE) == {"sets": 0, "items": 0, "assignments": 0}


class TestCollectedChatKeyRename:
    async def test_renaming_a_chat_key_rewrites_the_collected_column(self, db, files_dir: Path, vectors) -> None:
        from nekro_agent.services.chat_key_migration import migrate_chat_key_references

        collect = await lib.get_or_create_collect_set(ADAPTER, INSTANCE)
        old_key = f"{ADAPTER}-{INSTANCE}-group_1"
        new_key = f"{ADAPTER}-{INSTANCE}-group_2"
        await DBEmotionItem.create(
            emotion_id="aaaa0001",
            set_id=collect.id,
            description="描述",
            tags=[],
            file_path="",
            file_sha256="",
            collected_chat_key=old_key,
            added_at=1,
            updated_at=1,
        )

        counts = await migrate_chat_key_references(old_key, new_key, INSTANCE)

        assert counts["emotion_item_collected"] == 1
        item = await DBEmotionItem.filter(emotion_id="aaaa0001").first()
        assert item is not None
        assert item.collected_chat_key == new_key

    async def test_rows_collected_elsewhere_are_untouched(self, db, files_dir: Path, vectors) -> None:
        from nekro_agent.services.chat_key_migration import migrate_chat_key_references

        collect = await lib.get_or_create_collect_set(ADAPTER, INSTANCE)
        other_key = f"{ADAPTER}-{INSTANCE}-group_9"
        await DBEmotionItem.create(
            emotion_id="aaaa0001",
            set_id=collect.id,
            description="描述",
            tags=[],
            file_path="",
            file_sha256="",
            collected_chat_key=other_key,
            added_at=1,
            updated_at=1,
        )

        counts = await migrate_chat_key_references(
            f"{ADAPTER}-{INSTANCE}-group_1",
            f"{ADAPTER}-{INSTANCE}-group_2",
            INSTANCE,
        )

        assert counts.get("emotion_item_collected", 0) == 0
        item = await DBEmotionItem.filter(emotion_id="aaaa0001").first()
        assert item is not None
        assert item.collected_chat_key == other_key
