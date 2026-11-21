"""表情在生命周期事件里的接线

服务层自己的语义在 `test_emotion_library_lifecycle.py` 里钉过；本文件钉的是**调用链**：
`purge_instance`、`migrate_instance_ownership`、`bind_tenant_instances` 与预览接口真的走到了
那几步，而且顺序是对的。

## 顺序为什么是要紧的

`purge_instance` 在事务里删行、提交后再删向量与文件。向量点靠 `emotion_id` 定位，行一删就
查不出该删哪些点——所以引用必须在事务里（删行**之前**）取好，再带出去。写错顺序不会报错，
只会留下永远删不掉的向量：表情搜得到、点开却是一条已经不存在的记录。

## 划转的两个动作是分开的

取消平台方的 `instance` 分配**每次都做**，不受 `clear_memory_and_affection` 控制：
那是给前一个租户的授权。清空收集库里的表情只在这个开关为真时做，而且只清行 / 向量 / 文件，
收集库套组本身留着（下次会话进来还是同一个 ID）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Sequence

import pytest
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_emotion_item import DBEmotionItem
from nekro_agent.models.db_emotion_set import DBEmotionSet
from nekro_agent.models.db_emotion_set_assignment import DBEmotionSetAssignment
from nekro_agent.models.db_plugin_data import DBPluginData
from nekro_agent.models.db_tenant import DBTenant
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.console import tenants as tenants_router
from nekro_agent.schemas.tenant import TenantInstanceBindRequest, TenantRole, TenantStatus
from nekro_agent.services import chat_key_migration
from nekro_agent.services import emotion_library as lib
from nekro_agent.services.console import instance_manager
from nekro_agent.services.user.role import Role

ADAPTER = "onebot_v11"


def _platform_user() -> DBUser:
    return DBUser(
        id=1,
        username="platform",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Super,
        tenant_id=None,
        tenant_role=TenantRole.PLATFORM_OWNER.value,
    )


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Dict[str, Any]]:
    monkeypatch.setattr(config, "MEMORY_INSTANCE_SHARING_GROUPS", {})
    # 表情文件目录改到临时目录：删文件那一步不碰真实的 `data/`
    files = tmp_path / "emotions"
    files.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lib, "EMOTION_FILES_DIR", files)

    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    lib.clear_scope_cache()
    try:
        platform = _platform_user()
        tenant = await DBTenant.create(
            id=1,
            name="alpha",
            display_name="租户甲",
            status=TenantStatus.ACTIVE.value,
        )
        instances = {
            key: await DBAdapterInstance.create(
                adapter_key=ADAPTER,
                instance_key=key,
                display_name=key,
                tenant_id=None,
            )
            for key in ("a1", "a2", "src", "dst")
        }
        yield {"platform": platform, "tenant": tenant, "instances": instances, "files": files}
    finally:
        lib.clear_scope_cache()
        await Tortoise.close_connections()


@pytest.fixture
def vectors(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """桩掉向量侧：服务层不连 Qdrant，记下删了哪些点"""
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


async def _collect(instance_key: str, emotion_ids: Sequence[str]) -> DBEmotionSet:
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


class TestPurgeInstanceWiring:
    async def test_purging_an_instance_removes_its_emotions_and_counts_them(
        self, world, vectors, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        collect = await _collect("a1", ["aaaa0001", "aaaa0002"])
        kept = await _collect("a2", ["bbbb0001"])
        preset = await lib.create_preset_set(name="平台方预设")
        await lib.add_assignment(preset.id, target=lib.TARGET_INSTANCE, adapter_key=ADAPTER, instance_key="a1")

        async def no_vectors(_instance_key: str) -> None:
            """记忆向量不在本用例的契约里"""

        monkeypatch.setattr(instance_manager, "purge_instance_vectors", no_vectors)
        monkeypatch.setattr(
            instance_manager,
            "_reclaim_purged_instance_container",
            _no_container,
        )

        counts = await instance_manager.purge_instance(world["instances"]["a1"])

        assert counts["emotion_set"] == 1
        assert counts["emotion_item"] == 2
        assert counts["emotion_set_assignment"] == 1
        assert await DBEmotionSet.filter(id=collect.id).count() == 0
        assert await DBEmotionItem.all().count() == 1
        assert await DBEmotionItem.filter(set_id=kept.id).count() == 1
        assert await DBEmotionSetAssignment.filter(set_id=preset.id).count() == 0
        assert await DBEmotionSet.filter(id=preset.id).count() == 1

    async def test_the_vector_points_are_deleted_even_though_the_rows_are_gone(
        self, world, vectors, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """事务里先删行、提交后再删向量：引用必须提前取好，否则这些点永远删不掉"""
        await _collect("a1", ["aaaa0001", "aaaa0002"])

        async def no_vectors(_instance_key: str) -> None:
            """记忆向量不在本用例的契约里"""

        monkeypatch.setattr(instance_manager, "purge_instance_vectors", no_vectors)
        monkeypatch.setattr(instance_manager, "_reclaim_purged_instance_container", _no_container)

        await instance_manager.purge_instance(world["instances"]["a1"])

        assert sorted(vectors["deleted"]) == ["aaaa0001", "aaaa0002"]

    async def test_purging_also_removes_the_whiteboard_view_token(
        self, world, vectors, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await DBPluginData.create(
            plugin_key=lib.WHITEBOARD_PLUGIN_KEY,
            data_key=lib.whiteboard_view_token_store_key(ADAPTER, "a1"),
            data_value="token",
            target_chat_key="",
            target_user_id="",
        )

        async def no_vectors(_instance_key: str) -> None:
            """记忆向量不在本用例的契约里"""

        monkeypatch.setattr(instance_manager, "purge_instance_vectors", no_vectors)
        monkeypatch.setattr(instance_manager, "_reclaim_purged_instance_container", _no_container)

        await instance_manager.purge_instance(world["instances"]["a1"])

        assert await DBPluginData.filter(plugin_key=lib.WHITEBOARD_PLUGIN_KEY).count() == 0


async def _no_container(instance_key: str, *, had_builtin_container: bool) -> None:
    """容器回收（Docker）不在本文件的契约里"""


class TestInstanceMigrationWiring:
    async def test_the_collect_set_follows_the_instance(self, world, vectors) -> None:
        collect = await _collect("src", ["aaaa0001"])

        outcome = await chat_key_migration.migrate_instance_ownership(ADAPTER, "src", "dst")

        assert outcome.counts["emotion_set"] == 1
        migrated = await DBEmotionSet.get(id=collect.id)
        assert migrated.owner_instance_key == "dst"
        assert outcome.residual["emotion_set"] == 0
        assert outcome.residual["emotion_set_assignment"] == 0

    async def test_an_existing_target_collect_set_absorbs_the_items(self, world, vectors) -> None:
        source = await _collect("src", ["aaaa0001"])
        target = await _collect("dst", ["bbbb0001"])

        outcome = await chat_key_migration.migrate_instance_ownership(ADAPTER, "src", "dst")

        assert outcome.counts["emotion_item"] == 1
        assert await DBEmotionSet.filter(id=source.id).count() == 0
        assert await DBEmotionItem.filter(set_id=target.id).count() == 2

    async def test_the_instance_assignments_follow_and_the_residual_is_zero(self, world, vectors) -> None:
        preset = await lib.create_preset_set(name="平台方预设")
        await lib.add_assignment(preset.id, target=lib.TARGET_INSTANCE, adapter_key=ADAPTER, instance_key="src")

        before = await chat_key_migration._count_instance_residual(ADAPTER, "src")
        assert before["emotion_set_assignment"] == 1

        outcome = await chat_key_migration.migrate_instance_ownership(ADAPTER, "src", "dst")

        assert outcome.counts["emotion_set_assignment"] == 1
        row = await DBEmotionSetAssignment.filter(set_id=preset.id).first()
        assert row is not None
        assert row.instance_key == "dst"
        after = await chat_key_migration._count_instance_residual(ADAPTER, "src")
        assert after["emotion_set_assignment"] == 0

    async def test_the_whiteboard_token_moves_with_the_instance(self, world, vectors) -> None:
        await DBPluginData.create(
            plugin_key=lib.WHITEBOARD_PLUGIN_KEY,
            data_key=lib.whiteboard_view_token_store_key(ADAPTER, "src"),
            data_value="token",
            target_chat_key="",
            target_user_id="",
        )

        await chat_key_migration.migrate_instance_ownership(ADAPTER, "src", "dst")

        rows = await DBPluginData.filter(plugin_key=lib.WHITEBOARD_PLUGIN_KEY).all()
        assert len(rows) == 1
        assert rows[0].data_key == lib.whiteboard_view_token_store_key(ADAPTER, "dst")

    async def test_a_second_run_is_a_no_op(self, world, vectors) -> None:
        await _collect("src", ["aaaa0001"])
        await chat_key_migration.migrate_instance_ownership(ADAPTER, "src", "dst")

        second = await chat_key_migration.migrate_instance_ownership(ADAPTER, "src", "dst")

        assert second.counts["emotion_set"] == 0
        assert second.counts["emotion_item"] == 0
        assert second.residual["emotion_set"] == 0


class TestTenantTransferWiring:
    async def _preview(self, world, keys: List[str]) -> Any:
        return await tenants_router.preview_tenant_instances(
            tenant_id=world["tenant"].id,
            body=TenantInstanceBindRequest(instance_keys=keys),
            _current_user=world["platform"],
        )

    async def test_the_preview_reports_the_collected_emotion_count(self, world, vectors) -> None:
        await _collect("a1", ["aaaa0001", "aaaa0002", "aaaa0003"])
        await _collect("a2", ["bbbb0001"])

        preview = await self._preview(world, ["a1"])

        change = next(item for item in preview.changes if item.instance_key == "a1")
        assert change.collected_emotions == 3

    async def test_the_preview_reports_zero_without_a_collect_set(self, world, vectors) -> None:
        preview = await self._preview(world, ["a2"])

        change = next(item for item in preview.changes if item.instance_key == "a2")
        assert change.collected_emotions == 0

    async def test_binding_revokes_the_instance_assignment_even_without_clearing(self, world, vectors) -> None:
        preset = await lib.create_preset_set(name="平台方预设")
        await lib.add_assignment(preset.id, target=lib.TARGET_INSTANCE, adapter_key=ADAPTER, instance_key="a1")
        await lib.add_assignment(preset.id, target=lib.TARGET_ALL_INSTANCES)
        await _collect("a1", ["aaaa0001"])

        await tenants_router.bind_tenant_instances(
            tenant_id=world["tenant"].id,
            body=TenantInstanceBindRequest(instance_keys=["a1"], clear_memory_and_affection=False),
            _current_user=world["platform"],
        )

        targets = sorted(await DBEmotionSetAssignment.filter(set_id=preset.id).values_list("target", flat=True))
        assert targets == [lib.TARGET_ALL_INSTANCES]
        # 没勾清空：收集库里的表情一条不少
        assert await DBEmotionItem.filter(emotion_id="aaaa0001").count() == 1

    async def test_clearing_empties_the_collect_set_but_keeps_it(self, world, vectors) -> None:
        collect = await _collect("a1", ["aaaa0001", "aaaa0002"])

        await tenants_router.bind_tenant_instances(
            tenant_id=world["tenant"].id,
            body=TenantInstanceBindRequest(instance_keys=["a1"], clear_memory_and_affection=True),
            _current_user=world["platform"],
        )

        assert await DBEmotionSet.filter(id=collect.id).count() == 1
        assert await DBEmotionItem.filter(set_id=collect.id).count() == 0
        assert sorted(vectors["deleted"]) == ["aaaa0001", "aaaa0002"]

    async def test_clearing_does_not_touch_another_instances_emotions(self, world, vectors) -> None:
        await _collect("a1", ["aaaa0001"])
        kept = await _collect("a2", ["bbbb0001"])

        await tenants_router.bind_tenant_instances(
            tenant_id=world["tenant"].id,
            body=TenantInstanceBindRequest(instance_keys=["a1"], clear_memory_and_affection=True),
            _current_user=world["platform"],
        )

        assert await DBEmotionItem.filter(set_id=kept.id).count() == 1

    async def test_binding_revokes_the_whiteboard_view_link_even_without_clearing(self, world, vectors) -> None:
        """白板的只读链接是发给前一个主人的：不管勾不勾清空都作废，别的实例的不动"""
        for instance_key in ("a1", "a2"):
            await DBPluginData.create(
                plugin_key=lib.WHITEBOARD_PLUGIN_KEY,
                data_key=lib.whiteboard_view_token_store_key(ADAPTER, instance_key),
                data_value=f"token-{instance_key}",
                target_chat_key="",
                target_user_id="",
            )

        await tenants_router.bind_tenant_instances(
            tenant_id=world["tenant"].id,
            body=TenantInstanceBindRequest(instance_keys=["a1"], clear_memory_and_affection=False),
            _current_user=world["platform"],
        )

        remaining = await DBPluginData.filter(plugin_key=lib.WHITEBOARD_PLUGIN_KEY).values_list("data_key", flat=True)
        assert list(remaining) == [lib.whiteboard_view_token_store_key(ADAPTER, "a2")]


class TestSchemaWording:
    def test_the_clear_option_mentions_the_collected_emotions(self) -> None:
        from nekro_agent.schemas.tenant import TenantInstanceBindRequest

        description = TenantInstanceBindRequest.model_fields["clear_memory_and_affection"].description or ""
        assert description == "一并清空这些实例的记忆、世界书、好感、收集的表情与用户邮件偏好；工作区行不动，不可恢复"

    def test_the_transfer_change_carries_a_collected_emotion_count(self) -> None:
        from nekro_agent.schemas.tenant import TenantInstanceTransferChange

        fields = TenantInstanceTransferChange.model_fields
        assert "collected_emotions" in fields
        assert fields["collected_emotions"].default == 0
