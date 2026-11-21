"""酒馆人设卡接口的作用域、审计与导出

直接调用路由函数（与世界书接口用例同一做法），跑在真表上。两种身份各走一遍：平台方
的全局视图与租户管理员自己的实例；另钉住只读成员被权限装饰器挡住、检查接口不落库、
导入的归属、审计不带原文、清理规则校验与导出的媒体类型 / 文件名。
"""

from __future__ import annotations

import io
import json
from typing import Any, AsyncIterator

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_config_audit import DBConfigChangeAudit
from nekro_agent.models.db_preset import DBPreset
from nekro_agent.models.db_preset_card import DBPresetCard
from nekro_agent.models.db_user import DBUser
from nekro_agent.models.db_wb_book import DBWbBook
from nekro_agent.models.db_wb_entry import DBWbEntry
from nekro_agent.routers import tavern
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.tavern import PresetCardUpdateRequest
from nekro_agent.schemas.tenant import TenantRole
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


def _tenant_user() -> DBUser:
    return DBUser(
        id=21,
        username="tenant-admin",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.Admin,
        tenant_id=1,
        tenant_role=TenantRole.TENANT_ADMIN.value,
    )


def _readonly_user() -> DBUser:
    return DBUser(
        id=22,
        username="tenant-reader",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=Role.User,
        tenant_id=1,
        tenant_role=TenantRole.TENANT_USER.value,
    )


def _v2_card(name: str = "导入角色") -> dict[str, Any]:
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": name,
            "description": "描述",
            "personality": "性格",
            "scenario": "场景",
            "first_mes": "开场",
            "mes_example": "示例",
            "creator_notes": "备注",
            "system_prompt": "系统提示",
            "post_history_instructions": "后置",
            "alternate_greetings": ["备选"],
            "tags": ["标签"],
            "creator": "作者",
            "character_version": "1.0",
            "extensions": {},
            "character_book": {"name": "卡内书", "entries": [{"id": 1, "keys": ["甲"], "content": "知识"}]},
        },
    }


def _upload(card: dict[str, Any] | None = None) -> UploadFile:
    raw = json.dumps(card or _v2_card()).encode("utf-8")
    return UploadFile(file=io.BytesIO(raw), filename="card.json")


async def _preset(name: str, owner_instance_key: str) -> DBPreset:
    return await DBPreset.create(
        name=name,
        title=name,
        avatar="",
        content=f"{name}的正文",
        description="",
        tags="",
        author="测试",
        owner_instance_key=owner_instance_key,
    )


async def _book(name: str, preset: DBPreset, adapter_key: str, instance_key: str) -> DBWbBook:
    book = await DBWbBook.create(
        name=name,
        scope_kind="preset",
        adapter_key=adapter_key,
        instance_key=instance_key,
        preset_id=preset.id,
    )
    await DBWbEntry.create(book=book, uid=0, keys=["甲"], content="知识书正文")
    return book


@pytest.fixture
async def world() -> AsyncIterator[dict[str, Any]]:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key="t1a",
            display_name="租户一",
            tenant_id=1,
            provider_account_id="bot_t1a",
        )
        await DBAdapterInstance.create(
            adapter_key=ADAPTER,
            instance_key="t2a",
            display_name="租户二",
            tenant_id=2,
            provider_account_id="bot_t2a",
        )
        presets = {
            "global": await _preset("全局人设", ""),
            "t1a": await _preset("租户一人设", "t1a"),
            "t2a": await _preset("租户二人设", "t2a"),
        }
        books = {
            "global": await _book("全局人设书", presets["global"], "", ""),
            "t1a": await _book("租户一人设书", presets["t1a"], ADAPTER, "t1a"),
        }
        yield {
            "platform": _platform_user(),
            "tenant": _tenant_user(),
            "readonly": _readonly_user(),
            "presets": presets,
            "books": books,
        }
    finally:
        await Tortoise.close_connections()


class TestInspect:
    async def test_inspect_does_not_write_anything(self, world: dict[str, Any]) -> None:
        before = (
            await DBPreset.all().count(),
            await DBPresetCard.all().count(),
            await DBWbBook.all().count(),
            await DBWbEntry.all().count(),
        )

        report = await tavern.inspect_card_endpoint(
            file=_upload(),
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )

        assert report.name == "导入角色"
        assert report.book.present is True
        assert report.book.entries == 1
        assert report.target.worldbook_enabled == bool(getattr(config, "WORLDBOOK_ENABLED", False))
        after = (
            await DBPreset.all().count(),
            await DBPresetCard.all().count(),
            await DBWbBook.all().count(),
            await DBWbEntry.all().count(),
        )
        assert after == before

    async def test_tenant_without_instance_is_rejected(self, world: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            await tavern.inspect_card_endpoint(
                file=_upload(),
                instance_key="",
                adapter_key="",
                _current_user=world["tenant"],
            )

    async def test_tenant_using_another_instance_gets_404(self, world: dict[str, Any]) -> None:
        with pytest.raises(NotFoundError):
            await tavern.inspect_card_endpoint(
                file=_upload(),
                instance_key="t2a",
                adapter_key=ADAPTER,
                _current_user=world["tenant"],
            )


class TestImport:
    async def test_tenant_import_owns_the_instance_and_book(self, world: dict[str, Any]) -> None:
        result = await tavern.import_card_endpoint(
            file=_upload(),
            options="{}",
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )

        preset = await DBPreset.get(id=result.preset_id)
        assert preset.owner_instance_key == "t1a"
        book = await DBWbBook.get(id=result.book_id)
        assert (book.adapter_key, book.instance_key) == (ADAPTER, "t1a")
        assert book.scope_kind == "preset"
        assert book.preset_id == preset.id

    async def test_platform_global_view_creates_global_preset_and_book(self, world: dict[str, Any]) -> None:
        result = await tavern.import_card_endpoint(
            file=_upload(),
            options="{}",
            instance_key="",
            adapter_key="",
            _current_user=world["platform"],
        )

        preset = await DBPreset.get(id=result.preset_id)
        assert preset.owner_instance_key == ""
        book = await DBWbBook.get(id=result.book_id)
        assert (book.adapter_key, book.instance_key) == ("", "")

    async def test_bad_options_is_400(self, world: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            await tavern.import_card_endpoint(
                file=_upload(),
                options="{not json",
                instance_key="t1a",
                adapter_key=ADAPTER,
                _current_user=world["tenant"],
            )


class TestGetAndUpdate:
    async def test_get_card_of_another_instance_is_404(self, world: dict[str, Any]) -> None:
        with pytest.raises(NotFoundError):
            await tavern.get_preset_card(
                world["presets"]["t2a"].id,
                instance_key="t1a",
                _current_user=world["tenant"],
            )

    async def test_get_card_without_row_returns_defaults(self, world: dict[str, Any]) -> None:
        response = await tavern.get_preset_card(
            world["presets"]["t1a"].id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.exists is False
        assert response.greeting_pick == "first"

    async def test_tenant_cannot_write_global_preset(self, world: dict[str, Any]) -> None:
        with pytest.raises(PermissionDeniedError):
            await tavern.update_preset_card(
                world["presets"]["global"].id,
                PresetCardUpdateRequest(post_history="后置"),
                instance_key="t1a",
                _current_user=world["tenant"],
            )

    async def test_post_history_change_writes_audit_without_text(self, world: dict[str, Any]) -> None:
        preset = world["presets"]["t1a"]
        values = ["第一段后置", "第二段后置"]

        for value in values:
            await tavern.update_preset_card(
                preset.id,
                PresetCardUpdateRequest(post_history=value),
                instance_key="t1a",
                _current_user=world["tenant"],
            )

        rows = await DBConfigChangeAudit.all().order_by("id")
        assert len(rows) == 2
        for row, value in zip(rows, values):
            assert row.layer == "preset"
            assert row.preset_id == preset.id
            assert row.field == "preset.post_history"
            assert row.instance_key == "t1a"
            assert row.operator == "tenant-admin"
            assert row.operator_is_platform is False
            assert value not in row.old_value and value not in row.new_value
        assert rows[0].old_value == ""
        assert rows[0].new_value.startswith("len=")
        assert rows[1].old_value == rows[0].new_value

    async def test_unchanged_value_does_not_write_audit(self, world: dict[str, Any]) -> None:
        preset = world["presets"]["t1a"]
        await tavern.update_preset_card(
            preset.id,
            PresetCardUpdateRequest(prefill="预填"),
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        await tavern.update_preset_card(
            preset.id,
            PresetCardUpdateRequest(prefill="预填"),
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert await DBConfigChangeAudit.all().count() == 1

    async def test_cleanup_rule_validation_is_400_with_index(self, world: dict[str, Any]) -> None:
        with pytest.raises(ValidationError) as excinfo:
            await tavern.update_preset_card(
                world["presets"]["t1a"].id,
                PresetCardUpdateRequest(
                    cleanup_rules=[
                        {"name": "好规则", "find": "/ok/g", "replace": "x", "trim": [], "enabled": True},
                        {"name": "坏规则", "find": "(", "replace": "x", "trim": [], "enabled": True},
                    ],
                ),
                instance_key="t1a",
                _current_user=world["tenant"],
            )

        assert "第 2 条" in str(excinfo.value)

    async def test_cleanup_rule_original_is_restored_by_index_and_name(self, world: dict[str, Any]) -> None:
        preset = world["presets"]["t1a"]
        original = {"scriptName": "规则", "findRegex": "/a/g", "placement": [2], "disabled": False}
        await tavern.update_preset_card(
            preset.id,
            PresetCardUpdateRequest(
                cleanup_rules=[
                    {
                        "name": "规则",
                        "find": "/a/g",
                        "replace": "b",
                        "trim": [],
                        "enabled": True,
                        "original": original,
                    },
                ],
            ),
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        await tavern.update_preset_card(
            preset.id,
            PresetCardUpdateRequest(
                cleanup_rules=[
                    {"name": "规则", "find": "/a/g", "replace": "b", "trim": [], "enabled": True},
                ],
            ),
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        card = await DBPresetCard.get(preset_id=preset.id)
        assert card.cleanup_rules[0]["original"] == original

    async def test_cleanup_rule_change_forgets_the_disabled_registry(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nekro_agent.routers import tavern as tavern_module

        preset = world["presets"]["t1a"]
        calls: list[int] = []
        monkeypatch.setattr(tavern_module.cleanup_runtime_service, "forget_preset", calls.append)

        await tavern.update_preset_card(
            preset.id,
            PresetCardUpdateRequest(
                cleanup_rules=[
                    {"name": "规则", "find": "/a/g", "replace": "b", "trim": [], "enabled": True},
                ],
            ),
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert calls == [preset.id]

    async def test_reassemble_uses_source_fields(self, world: dict[str, Any]) -> None:
        preset = world["presets"]["t1a"]
        result = await tavern.import_card_endpoint(
            file=_upload(),
            options="{}",
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )
        preset = await DBPreset.get(id=result.preset_id)
        preset.content = "手改"
        await preset.save()

        response = await tavern.reassemble_preset_card(
            preset.id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert "手改" not in response.content
        assert "描述" in response.content
        card = await DBPresetCard.get(preset_id=preset.id)
        assert card.assembled_sha256 == response.assembled_sha256


class TestExport:
    async def test_tenant_exports_own_preset_json(self, world: dict[str, Any]) -> None:
        response = await tavern.export_preset_card(
            world["presets"]["t1a"].id,
            format="json",
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.media_type == "application/json"
        assert "filename*=UTF-8''" in response.headers["content-disposition"]
        exported = json.loads(response.body)
        assert exported["data"]["character_book"]["entries"][0]["content"] == "知识书正文"

    async def test_export_png_media_type_and_filename(self, world: dict[str, Any]) -> None:
        response = await tavern.export_preset_card(
            world["presets"]["t1a"].id,
            format="png",
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.media_type == "image/png"
        assert response.body.startswith(b"\x89PNG\r\n\x1a\n")
        assert ".png" in response.headers["content-disposition"]

    async def test_tenant_export_of_global_preset_has_no_book(self, world: dict[str, Any]) -> None:
        response = await tavern.export_preset_card(
            world["presets"]["global"].id,
            format="json",
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        exported = json.loads(response.body)
        assert "character_book" not in exported["data"]


def _card_with_long_entry() -> dict[str, Any]:
    card = _v2_card()
    card["data"]["character_book"] = {
        "name": "卡内书",
        "entries": [
            {"id": 1, "keys": ["甲"], "comment": "长条目", "content": "设定" * 10},
            {"id": 2, "keys": ["乙"], "comment": "短条目", "content": "短"},
        ],
    }
    return card


class TestRound146ImportPath:
    async def test_inspect_names_entries_longer_than_the_round_budget(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "WORLDBOOK_MAX_CHARS", 5)

        report = await tavern.inspect_card_endpoint(
            file=_upload(_card_with_long_entry()),
            instance_key="",
            adapter_key="",
            _current_user=world["platform"],
        )

        assert report.book.notes["over_budget"] == 1
        hints = [item for item in report.notes if item.kind == "over_budget"]
        assert len(hints) == 1
        assert "1 条" in hints[0].detail
        assert "WORLDBOOK_MAX_CHARS（5 字）" in hints[0].detail
        assert "「长条目」20 字" in hints[0].detail
        assert "短条目" not in hints[0].detail

    async def test_inspect_uses_the_budget_of_the_target_instance(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[tuple[str, str]] = []

        def fake_budget(adapter_key: str, instance_key: str) -> int:
            seen.append((adapter_key, instance_key))
            return 1

        monkeypatch.setattr(tavern.worldbook_service, "import_budget_chars", fake_budget)

        report = await tavern.inspect_card_endpoint(
            file=_upload(_card_with_long_entry()),
            instance_key="t1a",
            adapter_key=ADAPTER,
            _current_user=world["tenant"],
        )

        assert seen == [(ADAPTER, "t1a")]
        # 预算 1 字：20 字的长条目超了；1 字的「短」刚好等于预算，不算超。
        assert report.book.notes["over_budget"] == 1

    async def test_import_report_carries_the_budget_hint(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "WORLDBOOK_MAX_CHARS", 5)

        result = await tavern.import_card_endpoint(
            file=_upload(_card_with_long_entry()),
            options="{}",
            instance_key="",
            adapter_key="",
            _current_user=world["platform"],
        )

        assert result.book_report is not None
        assert result.book_report.notes["over_budget"] == 1
        assert [item.entry_key for item in result.book_report.details if item.kind == "over_budget"] == ["1"]
        # 只是提示：长条目照样完整进库。
        assert await DBWbEntry.filter(book_id=result.book_id, content="设定" * 10).count() == 1

    async def test_world_link_to_the_embedded_book_is_not_reported(self, world: dict[str, Any]) -> None:
        card = _v2_card()
        card["data"]["extensions"] = {"world": "卡内书"}

        report = await tavern.inspect_card_endpoint(
            file=_upload(card),
            instance_key="",
            adapter_key="",
            _current_user=world["platform"],
        )

        assert [item.field for item in report.unsupported if item.field == "extensions.world"] == []

    async def test_too_large_under_the_platform_limit_names_it_and_suggests_json(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_MB", 1)
        oversized = UploadFile(file=io.BytesIO(b"x" * (1024 * 1024 + 1)), filename="card.png")

        with pytest.raises(ValidationError) as caught:
            await tavern.inspect_card_endpoint(
                file=oversized,
                instance_key="",
                adapter_key="",
                _current_user=world["platform"],
            )

        reason = caught.value.params["reason"]
        assert reason.startswith("文件超过 1 MiB")
        assert "MAX_UPLOAD_SIZE_MB 当前是 1 MB" in reason
        assert "卡片本身最大 20 MiB" in reason
        assert "导出成 JSON 再导入" in reason

    async def test_too_large_under_the_card_limit_only_names_the_size(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_MB", 100)
        monkeypatch.setattr(tavern, "MAX_UPLOAD_BYTES", 2 * 1024 * 1024)
        oversized = UploadFile(file=io.BytesIO(b"x" * (2 * 1024 * 1024 + 1)), filename="card.png")

        with pytest.raises(ValidationError) as caught:
            await tavern.import_card_endpoint(
                file=oversized,
                options="{}",
                instance_key="",
                adapter_key="",
                _current_user=world["platform"],
            )

        reason = caught.value.params["reason"]
        assert reason == "文件超过 2 MiB。可以在酒馆里把卡导出成 JSON 再导入"

    def test_card_limit_is_twenty_but_the_platform_default_keeps_ten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.config import CoreConfig
        from nekro_agent.services.tavern.limits import MAX_UPLOAD_BYTES

        default_mb = CoreConfig.model_fields["MAX_UPLOAD_SIZE_MB"].default
        monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_MB", default_mb)

        assert MAX_UPLOAD_BYTES == 20 * 1024 * 1024
        assert default_mb == 10
        assert tavern._max_upload_bytes() == 10 * 1024 * 1024


class TestReadonlyMember:
    async def test_readonly_member_is_rejected(self, world: dict[str, Any]) -> None:
        calls = [
            tavern.inspect_card_endpoint(
                file=_upload(),
                instance_key="t1a",
                adapter_key=ADAPTER,
                _current_user=world["readonly"],
            ),
            tavern.get_preset_card(
                world["presets"]["t1a"].id,
                instance_key="t1a",
                _current_user=world["readonly"],
            ),
            tavern.update_preset_card(
                world["presets"]["t1a"].id,
                PresetCardUpdateRequest(prefill="x"),
                instance_key="t1a",
                _current_user=world["readonly"],
            ),
            tavern.reassemble_preset_card(
                world["presets"]["t1a"].id,
                instance_key="t1a",
                _current_user=world["readonly"],
            ),
            tavern.import_card_endpoint(
                file=_upload(),
                options="{}",
                instance_key="t1a",
                adapter_key=ADAPTER,
                _current_user=world["readonly"],
            ),
            tavern.export_preset_card(
                world["presets"]["t1a"].id,
                format="json",
                instance_key="t1a",
                _current_user=world["readonly"],
            ),
        ]
        for call in calls:
            with pytest.raises(HTTPException) as excinfo:
                await call
            assert excinfo.value.status_code == 403
