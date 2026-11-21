"""角色扮演预设接口：作用域、检查不落库、导入、开关、删除与审计"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from starlette.datastructures import UploadFile
from tortoise import Tortoise

from nekro_agent.core.config import config
from nekro_agent.core.core_utils import ConfigManager
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_config_audit import DBConfigChangeAudit
from nekro_agent.models.db_rp_preset import DBRpPreset
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.routers import rp_presets
from nekro_agent.schemas.errors import NotFoundError, PermissionDeniedError, ValidationError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import config_service
from nekro_agent.services.tavern import preset_refs
from nekro_agent.services.tavern.rp_preset import parse_preset, segment_as_dict
from nekro_agent.services.user.role import Role
from tests.rp_preset_fixtures import standard_preset, wrapped_preset

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


def _upload(raw: dict[str, Any] | None = None, filename: str = "preset.json") -> UploadFile:
    data = json.dumps(raw if raw is not None else standard_preset(), ensure_ascii=False).encode("utf-8")
    return UploadFile(file=io.BytesIO(data), filename=filename)


async def _row(name: str, owner: str) -> DBRpPreset:
    segments, rules, notes = parse_preset(standard_preset())
    return await DBRpPreset.create(
        name=name,
        owner_instance_key=owner,
        segments=[segment_as_dict(segment) for segment in segments],
        raw=standard_preset(),
        regex_rules=rules,
        notes=notes,
        created_by="seed",
        updated_by="seed",
    )


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
        yield {
            "platform": _platform_user(),
            "tenant": _tenant_user(),
            "global": await _row("全局预设", ""),
            "t1a": await _row("租户一预设", "t1a"),
            "t2a": await _row("租户二预设", "t2a"),
        }
    finally:
        await Tortoise.close_connections()


class TestInspect:
    async def test_inspect_does_not_write_and_hides_content(self, world: dict[str, Any]) -> None:
        before = await DBRpPreset.all().count()
        report = await rp_presets.inspect_rp_preset_endpoint(
            file=_upload(wrapped_preset()),
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert await DBRpPreset.all().count() == before
        assert report.name == "preset"
        assert report.regex_count == 0
        assert any(note["kind"] == "summary" for note in report.notes)
        first = report.segments[0]
        assert not hasattr(first, "content")
        assert first.chars > 0

    async def test_inspect_rejects_missing_chat_history(self, world: dict[str, Any]) -> None:
        raw = standard_preset()
        raw["prompts"] = [prompt for prompt in raw["prompts"] if prompt["identifier"] != "chatHistory"]
        with pytest.raises(ValidationError, match="chatHistory"):
            await rp_presets.inspect_rp_preset_endpoint(
                file=_upload(raw),
                instance_key="t1a",
                _current_user=world["tenant"],
            )

    async def test_inspect_rejects_bad_json(self, world: dict[str, Any]) -> None:
        bad = UploadFile(file=io.BytesIO(b"not json"), filename="bad.json")
        with pytest.raises(ValidationError, match="JSON"):
            await rp_presets.inspect_rp_preset_endpoint(
                file=bad,
                instance_key="t1a",
                _current_user=world["tenant"],
            )


class TestImportAndVisibility:
    async def test_tenant_imports_into_its_own_instance(self, world: dict[str, Any]) -> None:
        detail = await rp_presets.import_rp_preset_endpoint(
            file=_upload(filename="我的预设.json"),
            instance_key="t1a",
            name=None,
            _current_user=world["tenant"],
        )

        assert detail.name == "我的预设"
        assert detail.owner_instance_key == "t1a"
        assert detail.read_only is False
        assert detail.segment_count == 13
        assert detail.enabled_count == 10
        row = await DBRpPreset.get(id=detail.id)
        assert row.raw["prompts"][0]["identifier"] == "main"

    async def test_platform_can_create_a_global_preset(self, world: dict[str, Any]) -> None:
        detail = await rp_presets.import_rp_preset_endpoint(
            file=_upload(filename="global.json"),
            instance_key="",
            name="平台预设",
            _current_user=world["platform"],
        )

        assert detail.owner_instance_key == ""
        assert detail.read_only is False

    async def test_tenant_list_sees_global_and_own(self, world: dict[str, Any]) -> None:
        result = await rp_presets.list_rp_presets(instance_key="t1a", _current_user=world["tenant"])

        names = {item.name for item in result.items}
        assert names == {"全局预设", "租户一预设"}
        assert result.total == 2
        by_name = {item.name: item for item in result.items}
        assert by_name["全局预设"].read_only is True
        assert by_name["租户一预设"].read_only is False

    async def test_tenant_cannot_read_or_delete_another_tenants_preset(self, world: dict[str, Any]) -> None:
        other = world["t2a"]
        with pytest.raises(NotFoundError):
            await rp_presets.get_rp_preset_endpoint(
                preset_id=other.id,
                instance_key="t1a",
                _current_user=world["tenant"],
            )
        with pytest.raises(NotFoundError):
            await rp_presets.delete_rp_preset_endpoint(
                preset_id=other.id,
                instance_key="t1a",
                _current_user=world["tenant"],
            )

    async def test_tenant_cannot_delete_a_global_preset(self, world: dict[str, Any]) -> None:
        with pytest.raises(PermissionDeniedError):
            await rp_presets.delete_rp_preset_endpoint(
                preset_id=world["global"].id,
                instance_key="t1a",
                _current_user=world["tenant"],
            )

    async def test_platform_list_in_global_view_sees_everything(self, world: dict[str, Any]) -> None:
        result = await rp_presets.list_rp_presets(instance_key="", _current_user=world["platform"])

        assert result.total == 3

    async def test_platform_scope_requires_an_existing_instance(self, world: dict[str, Any]) -> None:
        with pytest.raises(NotFoundError):
            await rp_presets.list_rp_presets(instance_key="missing", _current_user=world["platform"])


class TestUpdateAndDelete:
    async def test_update_toggles_and_renames_without_touching_content(self, world: dict[str, Any]) -> None:
        row = world["t1a"]
        detail = await rp_presets.update_rp_preset_endpoint(
            preset_id=row.id,
            body=rp_presets.RpPresetUpdateRequest(
                name="新名字",
                enabled={"nsfw": True, "main": False, "not-there": True},
            ),
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert detail.name == "新名字"
        by_id = {segment.identifier: segment for segment in detail.segments}
        assert by_id["nsfw"].enabled is True
        assert by_id["main"].enabled is False
        assert "not-there" not in by_id
        assert by_id["main"].content == "MAIN_BODY {{char}} {{getvar::mood}} {{// a comment}}"

    async def test_update_rejects_empty_name(self, world: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="预设名"):
            await rp_presets.update_rp_preset_endpoint(
                preset_id=world["t1a"].id,
                body=rp_presets.RpPresetUpdateRequest(name="  "),
                instance_key="t1a",
                _current_user=world["tenant"],
            )

    async def test_delete_returns_not_referenced(self, world: dict[str, Any]) -> None:
        result = await rp_presets.delete_rp_preset_endpoint(
            preset_id=world["t1a"].id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert result.ok is True
        assert result.referenced is False
        assert await DBRpPreset.filter(id=world["t1a"].id).exists() is False


class TestConfigReference:
    async def test_tenant_may_reference_global_or_own_but_not_another_tenants(
        self,
        world: dict[str, Any],
    ) -> None:
        config_key = "instance_override_onebot_v11__t1a"
        own = world["t1a"]
        global_id = world["global"]
        other = world["t2a"]

        await config_router._assert_rp_preset_reference(
            world["tenant"],
            config_key,
            {"PROMPT_RP_PRESET_ID": str(own.id)},
        )
        await config_router._assert_rp_preset_reference(
            world["tenant"],
            config_key,
            {"PROMPT_RP_PRESET_ID": str(global_id.id)},
        )
        with pytest.raises(PermissionDeniedError):
            await config_router._assert_rp_preset_reference(
                world["tenant"],
                config_key,
                {"PROMPT_RP_PRESET_ID": str(other.id)},
            )

    async def test_reference_must_exist(self, world: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="不存在"):
            await config_router._assert_rp_preset_reference(
                world["tenant"],
                "instance_override_onebot_v11__t1a",
                {"PROMPT_RP_PRESET_ID": "99999"},
            )

    async def test_platform_may_reference_any_existing_preset(self, world: dict[str, Any]) -> None:
        await config_router._assert_rp_preset_reference(
            world["platform"],
            "instance_override_onebot_v11__t2a",
            {"PROMPT_RP_PRESET_ID": str(world["t2a"].id)},
        )


class TestExportAndAudit:
    async def test_export_returns_the_original_json(self, world: dict[str, Any]) -> None:
        response = await rp_presets.export_rp_preset_endpoint(
            preset_id=world["t1a"].id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        body = json.loads(bytes(response.body).decode("utf-8"))
        assert body["prompts"][0]["identifier"] == "main"
        assert "attachment" in response.headers["content-disposition"]

    async def test_import_audit_has_no_body_text(self, world: dict[str, Any]) -> None:
        await rp_presets.import_rp_preset_endpoint(
            file=_upload(),
            instance_key="t1a",
            name="审计用",
            _current_user=world["tenant"],
        )

        audit = await DBConfigChangeAudit.filter(field="rp_preset.import").order_by("-id").first()
        assert audit is not None
        assert audit.layer == "rp_preset"
        assert "审计用" in audit.new_value
        assert "MAIN_BODY" not in audit.new_value
        assert "JAIL_BODY" not in audit.new_value


def _override_file(path: Path, preset_id: int, *, enabled: bool = True) -> None:
    """写一份覆盖层文件：只有 `PROMPT_RP_PRESET_ID` 这一项"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"enable_PROMPT_RP_PRESET_ID: {str(enabled).lower()}\nPROMPT_RP_PRESET_ID: {preset_id}\n",
        encoding="utf-8",
    )


@pytest.fixture
def override_cache() -> Any:
    """清掉覆盖层配置的全局缓存

    覆盖对象按配置键缓存，其中没有路径；上一条用例读进来的对象在本条用例里会被直接返回，
    哪怕 `DATA_DIR` 已经指到另一个临时目录。函数级固件在 conftest 的目录隔离之后执行。
    """
    from nekro_agent.services.config_resolver import config_resolver
    from nekro_agent.services.config_service import (
        ADAPTER_OVERRIDE_KEY_PREFIX,
        CHANNEL_OVERRIDE_KEY_PREFIX,
        INSTANCE_OVERRIDE_KEY_PREFIX,
    )

    prefixes = (ADAPTER_OVERRIDE_KEY_PREFIX, INSTANCE_OVERRIDE_KEY_PREFIX, CHANNEL_OVERRIDE_KEY_PREFIX)

    def clear() -> None:
        for key in list(ConfigManager.get_all_config_keys()):
            if key.startswith(prefixes):
                ConfigManager.unregister_config(key)
        config_resolver.bump_version()

    clear()
    yield
    clear()


class TestReferences:
    """删除前查引用：四层都要找到，作用域与可见性与详情接口一致"""

    async def test_all_four_layers_are_found(
        self,
        world: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        override_cache: Any,
    ) -> None:
        preset_id = world["global"].id
        chat_key = "onebot_v11-t1a-group_1"
        monkeypatch.setattr(config, "PROMPT_RP_PRESET_ID", preset_id)
        _override_file(config_service.adapter_override_config_path(ADAPTER), preset_id)
        _override_file(config_service.instance_override_config_path(ADAPTER, "t1a"), preset_id)
        _override_file(config_service.channel_override_config_path(chat_key), preset_id)
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key="t1a",
            channel_id="group_1",
            channel_name="测试频道",
            chat_key=chat_key,
            channel_type="group",
            data="{}",
        )

        response = await rp_presets.get_rp_preset_references_endpoint(
            preset_id=preset_id,
            instance_key="",
            _current_user=world["platform"],
        )

        by_layer = {item.layer: item for item in response.items}
        assert response.total == 4
        assert set(by_layer) == {"system", "adapter", "instance", "channel"}
        assert by_layer["system"].label == "system"
        assert by_layer["adapter"].adapter_key == ADAPTER
        assert by_layer["adapter"].label == ADAPTER
        assert by_layer["instance"].instance_key == "t1a"
        assert by_layer["instance"].label == "租户一"
        assert by_layer["channel"].chat_key == chat_key
        assert by_layer["channel"].label == "测试频道"

    async def test_toggle_off_and_other_preset_are_not_counted(
        self,
        world: dict[str, Any],
        override_cache: Any,
    ) -> None:
        preset_id = world["t1a"].id
        other_id = world["t2a"].id
        instance_path = config_service.instance_override_config_path(ADAPTER, "t1a")
        channel_key = "onebot_v11-t1a-group_2"
        _override_file(instance_path, preset_id, enabled=False)
        _override_file(config_service.channel_override_config_path(channel_key), other_id)

        found = await preset_refs.find_rp_preset_references(preset_id, only_instance_key="t1a")
        assert found == []

    async def test_tenant_scope_only_sees_own_instance_and_channels(
        self,
        world: dict[str, Any],
        override_cache: Any,
    ) -> None:
        preset_id = world["t1a"].id
        own_channel = "onebot_v11-t1a-group_3"
        other_channel = "onebot_v11-t2a-group_4"
        _override_file(config_service.instance_override_config_path(ADAPTER, "t1a"), preset_id)
        _override_file(config_service.instance_override_config_path(ADAPTER, "t2a"), preset_id)
        _override_file(config_service.channel_override_config_path(own_channel), preset_id)
        _override_file(config_service.channel_override_config_path(other_channel), preset_id)
        await DBChatChannel.create(
            adapter_key=ADAPTER,
            instance_key="t1a",
            channel_id="group_3",
            chat_key=own_channel,
            channel_type="group",
            data="{}",
        )

        response = await rp_presets.get_rp_preset_references_endpoint(
            preset_id=preset_id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.total == 2
        assert [item.layer for item in response.items] == ["instance", "channel"]
        assert {item.instance_key for item in response.items} == {"t1a"}
        assert [item.chat_key for item in response.items if item.layer == "channel"] == [own_channel]

    async def test_same_instance_key_under_another_adapter_is_not_reported(
        self,
        world: dict[str, Any],
        override_cache: Any,
    ) -> None:
        """实例标识只在适配器内唯一：别的适配器下、属于另一个租户的同名实例不能出现在结果里"""
        preset_id = world["global"].id
        other_adapter = "telegram"
        await DBAdapterInstance.create(
            adapter_key=other_adapter,
            instance_key="t1a",
            display_name="别人的同名实例",
            tenant_id=2,
            provider_account_id="bot_other",
        )
        _override_file(config_service.instance_override_config_path(ADAPTER, "t1a"), preset_id)
        _override_file(config_service.instance_override_config_path(other_adapter, "t1a"), preset_id)

        response = await rp_presets.get_rp_preset_references_endpoint(
            preset_id=preset_id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.total == 1
        assert [(item.adapter_key, item.label) for item in response.items] == [(ADAPTER, "租户一")]
    async def test_invisible_preset_is_still_404(self, world: dict[str, Any], override_cache: Any) -> None:
        with pytest.raises(NotFoundError):
            await rp_presets.get_rp_preset_references_endpoint(
                preset_id=world["t2a"].id,
                instance_key="t1a",
                _current_user=world["tenant"],
            )

    async def test_many_channels_truncate_items_but_keep_total(
        self,
        world: dict[str, Any],
        override_cache: Any,
    ) -> None:
        preset_id = world["t1a"].id
        _override_file(config_service.instance_override_config_path(ADAPTER, "t1a"), preset_id)
        total_channels = preset_refs.MAX_REFERENCES_PER_PRESET + 5
        for index in range(total_channels):
            chat_key = f"onebot_v11-t1a-group_{10 + index}"
            _override_file(config_service.channel_override_config_path(chat_key), preset_id)

        response = await rp_presets.get_rp_preset_references_endpoint(
            preset_id=preset_id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert response.total == total_channels + 1
        assert len(response.items) == preset_refs.MAX_REFERENCES_PER_PRESET

    async def test_delete_reports_reference_and_still_deletes(
        self,
        world: dict[str, Any],
        override_cache: Any,
    ) -> None:
        row = world["t1a"]
        _override_file(config_service.instance_override_config_path(ADAPTER, "t1a"), row.id)

        result = await rp_presets.delete_rp_preset_endpoint(
            preset_id=row.id,
            instance_key="t1a",
            _current_user=world["tenant"],
        )

        assert result.referenced is True
        assert await DBRpPreset.filter(id=row.id).exists() is False
