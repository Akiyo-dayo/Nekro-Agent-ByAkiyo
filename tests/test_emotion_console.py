"""表情控制台接口（`/console/api`）的实例范围与写权限

## 这一层钉的是什么

控制台是插件自己挂的路由，页面不在 NA 前端工程里（设计如此），所以接口就是全部的攻击面：

- 租户不带实例、带别人的实例、带一个插件关着的实例 —— 都由 `plugin_route_scope` 统一判，
  这里钉的是**判完之后**插件有没有把范围收住；
- 看不见的套组 / 表情必须与「不存在」同一个 404（否则 403 就成了一张存在性清单）；
- 看得见但不可写（平台方预设套组）才给 403，且与沙盒方法那句只读文案一致；
- 平台方专属的写操作对租户一律 403。

用例直接调端点函数（与白板路由的用例同一种形态）：鉴权依赖已被换成一个现成的
`PluginRouteScope`，其余全走真实库表与服务层。
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException
from tortoise import Tortoise

from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_emotion_item import DBEmotionItem
from nekro_agent.models.db_emotion_set import DBEmotionSet
from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.i18n import SupportedLang
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import emotion_library as lib
from nekro_agent.services.plugin.route_auth import PluginRouteScope
from nekro_agent.services.user.role import Role
from plugins.builtin import emotion as plugin_module

ADAPTER = "onebot_v11"
INSTANCE_A = "acme1"
INSTANCE_B = "rival1"
TENANT_A = 11
TENANT_B = 12

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
GIF = b"GIF89a" + b"1" * 64


def _user(*, tenant_id: Optional[int], role: TenantRole, perm: Role = Role.Admin) -> DBUser:
    return DBUser(
        id=1,
        username="console-user",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=role.value,
    )


def tenant_scope(instance_key: str = INSTANCE_A) -> PluginRouteScope:
    return PluginRouteScope(
        user=_user(tenant_id=TENANT_A, role=TenantRole.TENANT_ADMIN),
        adapter_key=ADAPTER,
        instance_key=instance_key,
        is_platform=False,
    )


def platform_scope(instance_key: str = "") -> PluginRouteScope:
    return PluginRouteScope(
        user=_user(tenant_id=None, role=TenantRole.PLATFORM_OWNER, perm=Role.Super),
        adapter_key=ADAPTER if instance_key else "",
        instance_key=instance_key,
        is_platform=True,
    )


async def seed() -> None:
    for instance_key, tenant_id in ((INSTANCE_A, TENANT_A), (INSTANCE_B, TENANT_B)):
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key=instance_key,
            display_name=instance_key,
            status="online",
            provider="remote_onebot",
            tenant_id=tenant_id,
            metadata_json="",
        )


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    lib.clear_scope_cache()
    try:
        await seed()
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


@pytest.fixture(autouse=True)
def quiet_vectors(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """向量侧全部打桩：控制台接口的用例不该连 Qdrant"""
    recorded: List[Any] = []

    async def _upsert(emotion_id: str, vector: Any, *, set_id: int, description: str, tags: List[str]) -> None:
        recorded.append(("upsert", emotion_id, int(set_id)))

    async def _delete(emotion_ids: Any) -> int:
        recorded.append(("delete", list(emotion_ids)))
        return len(list(emotion_ids))

    async def _embedding(_text: str) -> List[float]:
        return [0.1, 0.2]

    monkeypatch.setattr(lib, "vector_upsert", _upsert)
    monkeypatch.setattr(lib, "vector_delete", _delete)
    monkeypatch.setattr(plugin_module, "generate_embedding", _embedding)
    return recorded


def endpoints(method: str = "GET") -> Dict[str, Any]:
    """按方法取端点：同一个路径上挂着 GET 与 POST（或 PATCH 与 DELETE），只按路径取会串"""
    return {
        route.path: route.endpoint
        for route in plugin_module.create_router().routes
        if method in (route.methods or set())
    }


async def make_collect(instance_key: str = INSTANCE_A) -> DBEmotionSet:
    return await lib.get_or_create_collect_set(ADAPTER, instance_key)


async def make_preset(name: str = "预设") -> DBEmotionSet:
    return await lib.create_preset_set(name=name, category="分类", description="", created_by="platform")


async def add_item(set_id: int, *, description: str = "描述", name: str = "") -> DBEmotionItem:
    return await DBEmotionItem.create(
        emotion_id=await lib.allocate_emotion_id(),
        set_id=set_id,
        description=description,
        tags=["标签"],
        file_path=name,
        file_sha256="",
        added_at=1,
        updated_at=1,
    )


async def attach(files_dir: Path, item: DBEmotionItem, name: str = "a.png") -> Path:
    target = files_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG)
    item.file_path = name
    await item.save()
    return target


class _Upload:
    """`UploadFile` 的最小替身：接口只读 `read()` 与 `filename`"""

    def __init__(self, data: bytes, filename: str = "x.png") -> None:
        self._data = data
        self.filename = filename

    async def read(self) -> bytes:
        return self._data


# ---------------------------------------------------------------------------
# 套组列表
# ---------------------------------------------------------------------------


class TestListSets:
    async def test_a_tenant_sees_only_its_own_collection(self, db, files_dir, quiet_vectors):
        own = await make_collect(INSTANCE_A)
        other = await make_collect(INSTANCE_B)
        preset = await make_preset()
        await lib.add_assignment(
            preset.id,
            target=lib.TARGET_INSTANCE,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_B,
            preset_id=0,
            created_by="platform",
        )

        data = await endpoints()["/console/api/sets"](scope=tenant_scope())

        ids = {item["id"] for item in data["items"]}
        assert ids == {own.id}
        assert other.id not in ids
        assert preset.id not in ids

    async def test_a_tenant_sees_a_preset_assigned_to_it_as_read_only(self, db, files_dir, quiet_vectors):
        await make_collect(INSTANCE_A)
        preset = await make_preset()
        await lib.add_assignment(
            preset.id,
            target=lib.TARGET_INSTANCE,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_A,
            preset_id=0,
            created_by="platform",
        )

        data = await endpoints()["/console/api/sets"](scope=tenant_scope())

        by_id = {item["id"]: item for item in data["items"]}
        assert by_id[preset.id]["writable"] is False
        assert by_id[preset.id]["kind"] == lib.KIND_PRESET

    async def test_the_platform_without_an_instance_sees_everything(self, db, files_dir, quiet_vectors):
        await make_collect(INSTANCE_A)
        await make_collect(INSTANCE_B)
        await make_preset()

        data = await endpoints()["/console/api/sets"](scope=platform_scope())

        assert len(data["items"]) >= 3
        assert all("assignments" in item for item in data["items"])

    async def test_the_set_payload_carries_no_file_path(self, db, files_dir, quiet_vectors):
        collect = await make_collect()
        item = await add_item(collect.id)
        await attach(files_dir, item)

        data = await endpoints()["/console/api/sets"](scope=tenant_scope())

        assert "file_path" not in str(data)
        assert str(files_dir) not in str(data)


# ---------------------------------------------------------------------------
# 表情列表与取图
# ---------------------------------------------------------------------------


class TestItems:
    async def test_an_invisible_set_is_the_same_404_as_missing(self, db, files_dir, quiet_vectors):
        other = await make_collect(INSTANCE_B)

        with pytest.raises(NotFoundError) as invisible:
            await endpoints()["/console/api/items"](set_id=other.id, offset=0, limit=50, keyword="", scope=tenant_scope())
        with pytest.raises(NotFoundError) as missing:
            await endpoints()["/console/api/items"](set_id=9999, offset=0, limit=50, keyword="", scope=tenant_scope())

        # 同一个错误类、同一句模板、同一组参数名；只有被点名的 ID 不同（那一位本来就在请求里）
        assert invisible.value.get_error_name() == missing.value.get_error_name()
        assert invisible.value.params.keys() == missing.value.params.keys()
        assert invisible.value.get_message(SupportedLang.ZH_CN).replace(str(other.id), "{id}") == (
            missing.value.get_message(SupportedLang.ZH_CN).replace("9999", "{id}")
        )

    async def test_the_page_limit_is_capped(self, db, files_dir, quiet_vectors):
        collect = await make_collect()
        for index in range(3):
            await add_item(collect.id, description=f"第 {index} 条")

        data = await endpoints()["/console/api/items"](
            set_id=collect.id,
            offset=0,
            limit=10000,
            keyword="",
            scope=tenant_scope(),
        )

        assert len(data["items"]) == 3
        assert data["total"] == 3

    async def test_the_keyword_filter_matches_description(self, db, files_dir, quiet_vectors):
        collect = await make_collect()
        await add_item(collect.id, description="一只开心的猫")
        await add_item(collect.id, description="一只难过的狗")

        data = await endpoints()["/console/api/items"](
            set_id=collect.id,
            offset=0,
            limit=50,
            keyword="开心",
            scope=tenant_scope(),
        )

        assert [item["description"] for item in data["items"]] == ["一只开心的猫"]

    async def test_the_image_route_returns_the_bytes_without_a_path(self, db, files_dir, quiet_vectors):
        collect = await make_collect()
        item = await add_item(collect.id)
        await attach(files_dir, item)

        response = await endpoints()["/console/api/items/{emotion_id}/image"](emotion_id=item.emotion_id, scope=tenant_scope())

        assert response.media_type == "image/png"
        assert response.path == str(files_dir / "a.png")

    async def test_an_invisible_items_image_is_404(self, db, files_dir, quiet_vectors):
        other = await make_collect(INSTANCE_B)
        item = await add_item(other.id)
        await attach(files_dir, item)

        with pytest.raises(NotFoundError):
            await endpoints()["/console/api/items/{emotion_id}/image"](emotion_id=item.emotion_id, scope=tenant_scope())

    async def test_a_missing_file_is_404_not_500(self, db, files_dir, quiet_vectors):
        collect = await make_collect()
        item = await add_item(collect.id, name="gone.png")

        with pytest.raises(NotFoundError):
            await endpoints()["/console/api/items/{emotion_id}/image"](emotion_id=item.emotion_id, scope=tenant_scope())


# ---------------------------------------------------------------------------
# 改 / 删一条表情
# ---------------------------------------------------------------------------


class TestWriteItems:
    async def test_a_tenant_can_edit_its_own_collection(self, db, files_dir, quiet_vectors):
        collect = await make_collect()
        item = await add_item(collect.id)

        data = await endpoints("PATCH")["/console/api/items/{emotion_id}"](
            emotion_id=item.emotion_id,
            payload=plugin_module.ItemUpdateRequest(description="新描述", tags=["新"]),
            scope=tenant_scope(),
        )

        assert data["description"] == "新描述"
        assert ("upsert", item.emotion_id, collect.id) in quiet_vectors

    async def test_editing_a_preset_set_is_403_with_the_readonly_sentence(self, db, files_dir, quiet_vectors):
        preset = await make_preset()
        item = await add_item(preset.id)
        await lib.add_assignment(
            preset.id,
            target=lib.TARGET_INSTANCE,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_A,
            preset_id=0,
            created_by="platform",
        )

        with pytest.raises(plugin_module.PlatformReadonlyError) as error:
            await endpoints("PATCH")["/console/api/items/{emotion_id}"](
                emotion_id=item.emotion_id,
                payload=plugin_module.ItemUpdateRequest(description="改"),
                scope=tenant_scope(),
            )

        assert error.value.get_message(SupportedLang.ZH_CN) == lib.PLATFORM_READONLY_MESSAGE

    async def test_an_invisible_item_is_404_before_any_permission_check(self, db, files_dir, quiet_vectors):
        other = await make_collect(INSTANCE_B)
        item = await add_item(other.id)

        with pytest.raises(NotFoundError):
            await endpoints("PATCH")["/console/api/items/{emotion_id}"](
                emotion_id=item.emotion_id,
                payload=plugin_module.ItemUpdateRequest(description="改"),
                scope=tenant_scope(),
            )

    async def test_the_platform_can_edit_any_set(self, db, files_dir, quiet_vectors):
        other = await make_collect(INSTANCE_B)
        item = await add_item(other.id)

        data = await endpoints("PATCH")["/console/api/items/{emotion_id}"](
            emotion_id=item.emotion_id,
            payload=plugin_module.ItemUpdateRequest(description="平台方改的"),
            scope=platform_scope(),
        )

        assert data["description"] == "平台方改的"

    async def test_too_many_tags_is_422(self, db, files_dir, quiet_vectors):
        collect = await make_collect()
        item = await add_item(collect.id)

        with pytest.raises(HTTPException) as error:
            await endpoints("PATCH")["/console/api/items/{emotion_id}"](
                emotion_id=item.emotion_id,
                payload=plugin_module.ItemUpdateRequest(tags=[f"t{index}" for index in range(21)]),
                scope=tenant_scope(),
            )

        assert error.value.status_code == 422

    async def test_a_too_long_tag_is_422(self, db, files_dir, quiet_vectors):
        collect = await make_collect()
        item = await add_item(collect.id)

        with pytest.raises(HTTPException) as error:
            await endpoints("PATCH")["/console/api/items/{emotion_id}"](
                emotion_id=item.emotion_id,
                payload=plugin_module.ItemUpdateRequest(tags=["x" * 33]),
                scope=tenant_scope(),
            )

        assert error.value.status_code == 422

    async def test_a_tenant_can_delete_its_own_item(self, db, files_dir, quiet_vectors):
        collect = await make_collect()
        item = await add_item(collect.id)
        stored = await attach(files_dir, item)

        data = await endpoints("DELETE")["/console/api/items/{emotion_id}"](emotion_id=item.emotion_id, scope=tenant_scope())

        assert data["deleted"] is True
        assert not await DBEmotionItem.filter(emotion_id=item.emotion_id).exists()
        assert not stored.exists()

    async def test_deleting_a_preset_item_is_403(self, db, files_dir, quiet_vectors):
        preset = await make_preset()
        item = await add_item(preset.id)
        await lib.add_assignment(
            preset.id,
            target=lib.TARGET_INSTANCE,
            adapter_key=ADAPTER,
            instance_key=INSTANCE_A,
            preset_id=0,
            created_by="platform",
        )

        with pytest.raises(plugin_module.PlatformReadonlyError):
            await endpoints("DELETE")["/console/api/items/{emotion_id}"](emotion_id=item.emotion_id, scope=tenant_scope())


# ---------------------------------------------------------------------------
# 套组的增删改
# ---------------------------------------------------------------------------


class TestSetsWrite:
    async def test_a_tenant_cannot_create_a_set(self, db, files_dir, quiet_vectors):
        with pytest.raises(PermissionDeniedError):
            await endpoints("POST")["/console/api/sets"](
                payload=plugin_module.SetCreateRequest(name="租户自建"),
                scope=tenant_scope(),
            )

    async def test_the_platform_can_create_and_rename(self, db, files_dir, quiet_vectors):
        created = await endpoints("POST")["/console/api/sets"](
            payload=plugin_module.SetCreateRequest(name="新套组", category="分类"),
            scope=platform_scope(),
        )
        renamed = await endpoints("PATCH")["/console/api/sets/{set_id}"](
            set_id=created["id"],
            payload=plugin_module.SetUpdateRequest(name="改过名"),
            scope=platform_scope(),
        )

        assert renamed["name"] == "改过名"
        assert (await DBEmotionSet.get(id=created["id"])).kind == lib.KIND_PRESET

    async def test_renaming_a_missing_set_is_404(self, db, files_dir, quiet_vectors):
        with pytest.raises(NotFoundError):
            await endpoints("PATCH")["/console/api/sets/{set_id}"](
                set_id=9999,
                payload=plugin_module.SetUpdateRequest(name="x"),
                scope=platform_scope(),
            )

    async def test_a_tenant_cannot_delete_a_set(self, db, files_dir, quiet_vectors):
        preset = await make_preset()

        with pytest.raises(PermissionDeniedError):
            await endpoints("DELETE")["/console/api/sets/{set_id}"](set_id=preset.id, scope=tenant_scope())

    async def test_the_collection_set_cannot_be_deleted(self, db, files_dir, quiet_vectors):
        collect = await make_collect()

        with pytest.raises(ValidationError):
            await endpoints("DELETE")["/console/api/sets/{set_id}"](set_id=collect.id, scope=platform_scope())

    async def test_a_preset_set_is_deleted_with_its_items(self, db, files_dir, quiet_vectors):
        preset = await make_preset()
        item = await add_item(preset.id)
        stored = await attach(files_dir, item)

        data = await endpoints("DELETE")["/console/api/sets/{set_id}"](set_id=preset.id, scope=platform_scope())

        assert data["items"] == 1
        assert not await DBEmotionSet.filter(id=preset.id).exists()
        assert not stored.exists()


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


class TestUpload:
    async def test_an_upload_lands_in_the_target_set(self, db, files_dir, quiet_vectors):
        preset = await make_preset()

        data = await endpoints("POST")["/console/api/sets/{set_id}/upload"](
            set_id=preset.id,
            image=_Upload(PNG),
            description="上传的图",
            tags="猫,可爱",
            scope=platform_scope(),
        )

        item = await DBEmotionItem.get(emotion_id=data["emotion_id"])
        assert item.set_id == preset.id
        assert item.tags == ["猫", "可爱"]
        assert (files_dir / item.file_path).is_file()
        assert data["exists"] is False

    async def test_the_same_image_uploaded_twice_returns_the_first_one(self, db, files_dir, quiet_vectors):
        preset = await make_preset()
        first = await endpoints("POST")["/console/api/sets/{set_id}/upload"](
            set_id=preset.id,
            image=_Upload(PNG),
            description="第一次",
            tags="",
            scope=platform_scope(),
        )
        second = await endpoints("POST")["/console/api/sets/{set_id}/upload"](
            set_id=preset.id,
            image=_Upload(PNG),
            description="第二次",
            tags="",
            scope=platform_scope(),
        )

        assert second["emotion_id"] == first["emotion_id"]
        assert second["exists"] is True
        assert await DBEmotionItem.filter(set_id=preset.id).count() == 1

    async def test_a_tenant_cannot_upload(self, db, files_dir, quiet_vectors):
        collect = await make_collect()

        with pytest.raises(PermissionDeniedError):
            await endpoints("POST")["/console/api/sets/{set_id}/upload"](
                set_id=collect.id,
                image=_Upload(PNG),
                description="",
                tags="",
                scope=tenant_scope(),
            )

    async def test_the_collection_set_does_not_accept_uploads(self, db, files_dir, quiet_vectors):
        collect = await make_collect()

        with pytest.raises(ValidationError):
            await endpoints("POST")["/console/api/sets/{set_id}/upload"](
                set_id=collect.id,
                image=_Upload(PNG),
                description="",
                tags="",
                scope=platform_scope(),
            )

    async def test_a_non_image_is_422(self, db, files_dir, quiet_vectors):
        preset = await make_preset()

        with pytest.raises(HTTPException) as error:
            await endpoints("POST")["/console/api/sets/{set_id}/upload"](
                set_id=preset.id,
                image=_Upload(b"not an image"),
                description="",
                tags="",
                scope=platform_scope(),
            )

        assert error.value.status_code == 422

    async def test_an_oversized_upload_is_422(self, db, files_dir, quiet_vectors, monkeypatch: pytest.MonkeyPatch):
        preset = await make_preset()
        monkeypatch.setattr(plugin_module, "_MAX_UPLOAD_BYTES", 8)

        with pytest.raises(HTTPException) as error:
            await endpoints("POST")["/console/api/sets/{set_id}/upload"](
                set_id=preset.id,
                image=_Upload(PNG),
                description="",
                tags="",
                scope=platform_scope(),
            )

        assert error.value.status_code == 422


# ---------------------------------------------------------------------------
# 移动 / 复制
# ---------------------------------------------------------------------------


class TestMoveCopy:
    async def test_move_relinks_the_row_and_the_point(self, db, files_dir, quiet_vectors):
        source = await make_preset("源")
        target = await make_preset("目标")
        item = await add_item(source.id)

        data = await endpoints("POST")["/console/api/items/move"](
            payload=plugin_module.ItemsMoveRequest(emotion_ids=[item.emotion_id], target_set_id=target.id),
            scope=platform_scope(),
        )

        await item.refresh_from_db()
        assert data["moved"] == 1
        assert item.set_id == target.id

    async def test_copy_creates_a_new_row_sharing_the_file(self, db, files_dir, quiet_vectors):
        source = await make_preset("源")
        target = await make_preset("目标")
        item = await add_item(source.id)
        await attach(files_dir, item)

        data = await endpoints("POST")["/console/api/items/copy"](
            payload=plugin_module.ItemsMoveRequest(emotion_ids=[item.emotion_id], target_set_id=target.id),
            scope=platform_scope(),
        )

        assert len(data["created"]) == 1
        copied = await DBEmotionItem.get(emotion_id=data["created"][0])
        assert copied.set_id == target.id
        assert copied.file_path == item.file_path
        assert await DBEmotionItem.filter(set_id=source.id).count() == 1

    async def test_a_missing_target_set_is_404(self, db, files_dir, quiet_vectors):
        source = await make_preset("源")
        item = await add_item(source.id)

        with pytest.raises(NotFoundError):
            await endpoints("POST")["/console/api/items/move"](
                payload=plugin_module.ItemsMoveRequest(emotion_ids=[item.emotion_id], target_set_id=9999),
                scope=platform_scope(),
            )

    async def test_a_tenant_cannot_move(self, db, files_dir, quiet_vectors):
        collect = await make_collect()
        item = await add_item(collect.id)
        preset = await make_preset()

        with pytest.raises(PermissionDeniedError):
            await endpoints("POST")["/console/api/items/move"](
                payload=plugin_module.ItemsMoveRequest(emotion_ids=[item.emotion_id], target_set_id=preset.id),
                scope=tenant_scope(),
            )


# ---------------------------------------------------------------------------
# 分配
# ---------------------------------------------------------------------------


class TestAssignments:
    async def test_an_assignment_round_trip(self, db, files_dir, quiet_vectors):
        preset = await make_preset()

        created = await endpoints("POST")["/console/api/assignments"](
            payload=plugin_module.AssignmentRequest(
                set_id=preset.id,
                target=lib.TARGET_INSTANCE,
                adapter_key=ADAPTER,
                instance_key=INSTANCE_A,
            ),
            scope=platform_scope(),
        )
        listed = await endpoints("GET")["/console/api/assignments"](set_id=None, scope=platform_scope())
        removed = await endpoints("DELETE")["/console/api/assignments/{assignment_id}"](
            assignment_id=created["id"],
            scope=platform_scope(),
        )

        assert listed["total"] == 1
        assert listed["items"][0]["preset_missing"] is False
        assert removed["deleted"] is True
        assert await lib.list_sets_visible_to_instance(ADAPTER, INSTANCE_A, is_platform_instance=False) == []

    async def test_a_preset_assignment_with_a_missing_preset_is_marked(self, db, files_dir, quiet_vectors):
        preset = await make_preset()
        await lib.add_assignment(
            preset.id,
            target=lib.TARGET_PRESET,
            adapter_key="",
            instance_key="",
            preset_id=9876,
            created_by="platform",
        )

        data = await endpoints("GET")["/console/api/assignments"](set_id=None, scope=platform_scope())

        assert data["items"][0]["preset_missing"] is True

    async def test_assigning_a_collection_set_is_400(self, db, files_dir, quiet_vectors):
        collect = await make_collect()

        with pytest.raises(ValidationError):
            await endpoints("POST")["/console/api/assignments"](
                payload=plugin_module.AssignmentRequest(
                    set_id=collect.id,
                    target=lib.TARGET_ALL_INSTANCES,
                ),
                scope=platform_scope(),
            )

    async def test_a_tenant_cannot_read_assignments(self, db, files_dir, quiet_vectors):
        with pytest.raises(PermissionDeniedError):
            await endpoints("GET")["/console/api/assignments"](set_id=None, scope=tenant_scope())


# ---------------------------------------------------------------------------
# 存量进度
# ---------------------------------------------------------------------------


class TestLegacyProgress:
    async def test_the_progress_counts_rows_already_in_a_preset(self, db, files_dir, quiet_vectors):
        legacy = await lib.ensure_legacy_set()
        preset = await make_preset()
        digest = hashlib.sha256(PNG).hexdigest()
        await DBEmotionItem.create(
            emotion_id=await lib.allocate_emotion_id(),
            set_id=legacy.id,
            description="还没整理",
            tags=[],
            file_path="old.png",
            file_sha256=digest,
            added_at=1,
            updated_at=1,
        )
        await DBEmotionItem.create(
            emotion_id=await lib.allocate_emotion_id(),
            set_id=legacy.id,
            description="另一条",
            tags=[],
            file_path="old2.png",
            file_sha256="",
            added_at=1,
            updated_at=1,
        )
        await DBEmotionItem.create(
            emotion_id=await lib.allocate_emotion_id(),
            set_id=preset.id,
            description="已经整理过来了",
            tags=[],
            file_path="new.png",
            file_sha256=digest,
            added_at=1,
            updated_at=1,
        )

        data = await endpoints()["/console/api/legacy-progress"](scope=platform_scope())

        assert data["set_id"] == legacy.id
        assert data["remaining"] == 2
        assert data["already_in_preset"] == 1

    async def test_a_tenant_cannot_read_the_progress(self, db, files_dir, quiet_vectors):
        with pytest.raises(PermissionDeniedError):
            await endpoints()["/console/api/legacy-progress"](scope=tenant_scope())


# ---------------------------------------------------------------------------
# 路由契约
# ---------------------------------------------------------------------------


def _scope_dependencies(dependant: Any) -> List[Any]:
    """沿依赖树递归找出挂了 `plugin_route_scope` 的依赖函数"""
    found: List[Any] = []
    for dependency in dependant.dependencies:
        if getattr(dependency.call, "__plugin_route_scope__", False):
            found.append(dependency.call)
        found.extend(_scope_dependencies(dependency))
    return found


def _dependency_flags(dependency: Any) -> Dict[str, Any]:
    """读出依赖函数闭包里的构造参数

    `plugin_route_scope(plugin.key, allow_ticket=...)` 把参数留在闭包里，没有别的公开读法；
    这里按 `co_freevars` 的名字取，取不到就是空——判据因此不会因为改名而恒真。
    """
    cells = dict(zip(dependency.__code__.co_freevars, dependency.__closure__ or ()))
    return {name: cell.cell_contents for name, cell in cells.items() if name in {"allow_ticket", "allow_global_view"}}


def _api_routes() -> List[Any]:
    """接口路由：除了那一个不含数据的页面壳（`CONSOLE_PAGE_PATH`），其余都是接口"""
    return [route for route in plugin_module.create_router().routes if route.path != plugin_module.CONSOLE_PAGE_PATH]


class TestRouteShape:
    def test_every_console_route_is_under_the_prefix(self) -> None:
        paths = {route.path for route in _api_routes()}
        assert paths
        for path in paths:
            assert path.startswith("/console/api/"), path

    def test_every_console_route_has_the_scope_guard(self) -> None:
        """插件自挂路由默认匿名可达，每条都必须挂守卫（`test_plugin_route_guard.py` 也扫）"""
        for route in _api_routes():
            assert _scope_dependencies(route.dependant), route.path

    async def test_the_page_shell_carries_no_data(self) -> None:
        """页面壳匿名可取：里面不能有任何数据，只有去调接口的脚本；脚本不拼 HTML"""
        page = next(route for route in plugin_module.create_router().routes if route.path == plugin_module.CONSOLE_PAGE_PATH)
        assert not _scope_dependencies(page.dependant)
        response = await page.endpoint()
        html = bytes(response.body).decode("utf-8")
        assert "auth-storage" in html and "/api/plugin-route/ticket" in html
        assert "innerHTML" not in html, "模型写的描述与标签只能走 textContent"

    def test_the_image_route_accepts_a_ticket(self) -> None:
        """`<img>` 带不了请求头，取图必须认短时效票据；其余接口不认"""
        routes = {route.path: route for route in _api_routes()}
        image_flags = _dependency_flags(_scope_dependencies(routes["/console/api/items/{emotion_id}/image"].dependant)[0])
        list_flags = _dependency_flags(_scope_dependencies(routes["/console/api/sets"].dependant)[0])

        assert image_flags["allow_ticket"] is True
        assert list_flags["allow_ticket"] is False

    def test_the_global_view_is_only_allowed_where_intended(self) -> None:
        """平台方不带实例时能看全局的端点；租户那条限制（必须带实例）由依赖本身保证

        表情列表、取图、改、删四条也在里面：控制台页面上，平台方不选实例时要能整理还没分给
        任何实例的预设套组（按实例范围看不到它们）。
        """
        routes = {route.path: route for route in _api_routes()}
        allowed = {
            "/console/api/sets",
            "/console/api/sets/{set_id}",
            "/console/api/sets/{set_id}/upload",
            "/console/api/items",
            "/console/api/items/{emotion_id}",
            "/console/api/items/{emotion_id}/image",
            "/console/api/items/move",
            "/console/api/items/copy",
            "/console/api/assignments",
            "/console/api/assignments/{assignment_id}",
            "/console/api/legacy-progress",
        }
        seen = set()
        for path, route in routes.items():
            flags = _dependency_flags(_scope_dependencies(route.dependant)[0])
            seen.add(path)
            if path in allowed:
                assert flags["allow_global_view"] is True, path
            else:
                assert flags["allow_global_view"] is False, path
        assert seen >= allowed
