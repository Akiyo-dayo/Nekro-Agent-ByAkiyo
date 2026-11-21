"""插件实例配置的「平台方专属」与「只能调低」

插件配置能按实例覆盖，但写入时原先没有这两道判据：租户能改平台方用于计费 / 出站的
嵌入模型参数，也能把禁言时长上限调高。本模块钉住写入闸门与读接口的标记。
"""

from __future__ import annotations

import ast
import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from pydantic import Field
from tortoise import Tortoise

from nekro_agent.core.core_utils import ConfigBase, ExtraField
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers.plugins import (
    SavePluginConfigRequest,
    get_plugin_instance_config_handler,
    save_plugin_instance_config_handler,
)
from nekro_agent.schemas.errors import PermissionDeniedError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.config_service import ConfigService
from nekro_agent.services.plugin import scope as scope_mod
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.user.role import Role
from plugins.builtin.emotion import EmotionConfig
from plugins.builtin.judgement import JudgementConfig

TENANT_ID = 31
OTHER_TENANT_ID = 32
INSTANCE = "cfg_a"
OTHER_INSTANCE = "cfg_b"
GLOBAL_MUTE = 3600


class EmotionProbeConfig(EmotionConfig):
    """复用真实字段定义（含四个平台方专属标记），只换一个插件键"""


class JudgementProbeConfig(JudgementConfig):
    """复用真实字段定义（MAX_MUTE_DURATION 标了只能调低）"""


class BoolProbeConfig(ConfigBase):
    """bool 版「只能调低」：全局开、租户只能关"""

    BOOL_LIMIT: bool = Field(
        default=True,
        title="布尔上限",
        description="测试用",
        json_schema_extra=ExtraField(tenant_may_only_lower=True).model_dump(),
    )


def make_user(*, tenant_id: Optional[int], tenant_role: TenantRole, perm_level: Role) -> DBUser:
    return DBUser(
        username=f"cfg-{tenant_role.value}-{tenant_id}-{perm_level.value}",
        password="",
        adapter_key="webui",
        platform_userid="0",
        perm_level=perm_level,
        login_time=datetime.datetime.now(datetime.timezone.utc),
        tenant_id=tenant_id,
        tenant_role=tenant_role.value,
    )


def tenant_admin() -> DBUser:
    return make_user(tenant_id=TENANT_ID, tenant_role=TenantRole.TENANT_ADMIN, perm_level=Role.Admin)


def platform_owner() -> DBUser:
    return make_user(tenant_id=None, tenant_role=TenantRole.PLATFORM_OWNER, perm_level=Role.Super)


@pytest.fixture
async def db():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, tenant_id in ((INSTANCE, TENANT_ID), (OTHER_INSTANCE, OTHER_TENANT_ID)):
            await DBAdapterInstance.create(
                adapter_key="onebot_v11",
                instance_key=instance_key,
                display_name=instance_key,
                status="online",
                provider="remote_onebot",
                tenant_id=tenant_id,
                metadata_json="",
            )
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def build_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """按生产路径建插件，但把配置与覆盖目录整体改道到 tmp_path"""
    data_dir = tmp_path / "plugin_data"
    monkeypatch.setattr(scope_mod, "PLUGIN_DATA_DIR", data_dir)
    monkeypatch.setattr(scope_mod, "SCOPE_OVERRIDE_DIR", data_dir / "_overrides")
    scope_mod._scope_override_cache.clear()  # noqa: SLF001
    scope_mod._scoped_config_cache.clear()  # noqa: SLF001

    created: List[NekroPlugin] = []

    def _build(config_cls: type[ConfigBase], module_name: str) -> NekroPlugin:
        probe = NekroPlugin(
            name=f"配置探针 {module_name}",
            module_name=module_name,
            description="仅用于插件配置测试",
            version="0.1.0",
            author="Probe",
            url="",
        )
        # 路径要在挂载配置类之前改掉：mount_config 会当场把默认值落盘一次
        probe._plugin_path = data_dir / probe.key  # noqa: SLF001
        probe._plugin_config_path = probe._plugin_path / "config.yaml"  # noqa: SLF001
        probe.mount_config()(config_cls)
        plugin_collector.loaded_plugins[probe.key] = probe
        created.append(probe)
        return probe

    try:
        yield _build
    finally:
        for probe in created:
            plugin_collector.loaded_plugins.pop(probe.key, None)
        scope_mod._scope_override_cache.clear()  # noqa: SLF001
        scope_mod._scoped_config_cache.clear()  # noqa: SLF001


def set_global(plugin: NekroPlugin, **values: Any) -> None:
    global_config = plugin.get_global_config()
    for key, value in values.items():
        setattr(global_config, key, value)
    plugin.save_config(global_config)


async def save(plugin_key: str, configs: Dict[str, str], user: DBUser) -> Any:
    if user.id is None:
        # 模型组归属判定要按 user_id 查可见性，身份必须已经落库
        await user.save()
    return await save_plugin_instance_config_handler(
        plugin_key,
        INSTANCE,
        SavePluginConfigRequest(configs=configs),
        _current_user=user,
    )


class TestPlatformOnly:
    @pytest.mark.asyncio
    async def test_tenant_cannot_change_a_platform_only_field(self, db, build_plugin):
        plugin = build_plugin(EmotionProbeConfig, "emotion_probe")
        set_global(plugin, EMBEDDING_DIMENSION=1024)

        with pytest.raises(PermissionDeniedError) as exc:
            await save(plugin.key, {"EMBEDDING_DIMENSION": "2048"}, tenant_admin())
        assert "EMBEDDING_DIMENSION" in (exc.value.detail or "")
        assert "平台方" in (exc.value.detail or "")

    @pytest.mark.asyncio
    async def test_tenant_sending_the_same_value_back_is_allowed(self, db, build_plugin):
        plugin = build_plugin(EmotionProbeConfig, "emotion_probe_same")
        set_global(plugin, EMBEDDING_DIMENSION=1024)

        response = await save(plugin.key, {"EMBEDDING_DIMENSION": "1024"}, tenant_admin())
        assert response.ok is True
        assert "EMBEDDING_DIMENSION" not in scope_mod.load_instance_config_overrides(plugin.key, INSTANCE)

    @pytest.mark.asyncio
    async def test_platform_owner_can_change_it(self, db, build_plugin):
        plugin = build_plugin(EmotionProbeConfig, "emotion_probe_platform")
        set_global(plugin, EMBEDDING_DIMENSION=1024)

        response = await save(plugin.key, {"EMBEDDING_DIMENSION": "2048"}, platform_owner())
        assert response.ok is True
        assert scope_mod.load_instance_config_overrides(plugin.key, INSTANCE)["EMBEDDING_DIMENSION"] == 2048


class TestOnlyLower:
    @pytest.mark.asyncio
    async def test_tenant_cannot_raise_max_mute_duration(self, db, build_plugin):
        plugin = build_plugin(JudgementProbeConfig, "judgement_probe")
        set_global(plugin, MAX_MUTE_DURATION=GLOBAL_MUTE)

        with pytest.raises(PermissionDeniedError) as exc:
            await save(plugin.key, {"MAX_MUTE_DURATION": str(GLOBAL_MUTE * 2)}, tenant_admin())
        assert "MAX_MUTE_DURATION" in (exc.value.detail or "")
        assert "租户只能调低" in (exc.value.detail or "")

    @pytest.mark.asyncio
    async def test_tenant_can_lower_and_come_back_to_the_global_value(self, db, build_plugin):
        plugin = build_plugin(JudgementProbeConfig, "judgement_probe_lower")
        set_global(plugin, MAX_MUTE_DURATION=GLOBAL_MUTE)

        assert (await save(plugin.key, {"MAX_MUTE_DURATION": "1800"}, tenant_admin())).ok is True
        assert scope_mod.load_instance_config_overrides(plugin.key, INSTANCE)["MAX_MUTE_DURATION"] == 1800

        # 调低之后回到不超过全局值的位置放行
        assert (await save(plugin.key, {"MAX_MUTE_DURATION": str(GLOBAL_MUTE)}, tenant_admin())).ok is True
        assert "MAX_MUTE_DURATION" not in scope_mod.load_instance_config_overrides(plugin.key, INSTANCE)

        # 再往上抬就拒绝
        with pytest.raises(PermissionDeniedError):
            await save(plugin.key, {"MAX_MUTE_DURATION": str(GLOBAL_MUTE + 1)}, tenant_admin())

    @pytest.mark.asyncio
    async def test_bool_can_only_be_switched_off(self, db, build_plugin):
        plugin = build_plugin(BoolProbeConfig, "bool_probe")
        set_global(plugin, BOOL_LIMIT=True)

        assert (await save(plugin.key, {"BOOL_LIMIT": "false"}, tenant_admin())).ok is True
        assert scope_mod.load_instance_config_overrides(plugin.key, INSTANCE)["BOOL_LIMIT"] is False

        with pytest.raises(PermissionDeniedError) as exc:
            await save(plugin.key, {"BOOL_LIMIT": "true"}, tenant_admin())
        assert "BOOL_LIMIT" in (exc.value.detail or "")
        assert "租户只能调低" in (exc.value.detail or "")


class TestReadMarkers:
    @pytest.mark.asyncio
    async def test_instance_config_carries_both_markers(self, db, build_plugin):
        plugin = build_plugin(EmotionProbeConfig, "emotion_probe_read")
        set_global(plugin, EMBEDDING_DIMENSION=1024)

        user = tenant_admin()
        await user.save()
        items = await get_plugin_instance_config_handler(plugin.key, INSTANCE, _current_user=user)
        by_key = {item["key"]: item for item in items}
        assert by_key["EMBEDDING_DIMENSION"]["platform_only"] is True
        assert by_key["EMBEDDING_MODEL"]["platform_only"] is True
        assert by_key["EMBEDDING_REQUEST_TIMEOUT"]["platform_only"] is True
        assert by_key["IMAGE_DOWNLOAD_TIMEOUT"]["platform_only"] is True

    @pytest.mark.asyncio
    async def test_global_config_list_carries_both_markers(self, db, build_plugin):
        plugin = build_plugin(JudgementProbeConfig, "judgement_probe_global")
        global_config = plugin.get_global_config()
        items = ConfigService.get_config_list(global_config)
        by_key = {item["key"]: item for item in items}
        assert by_key["MAX_MUTE_DURATION"]["tenant_may_only_lower"] is True


def test_real_builtin_markers() -> None:
    assert EmotionConfig.model_fields["EMBEDDING_MODEL"].json_schema_extra["platform_only"] is True
    assert EmotionConfig.model_fields["EMBEDDING_DIMENSION"].json_schema_extra["platform_only"] is True
    assert EmotionConfig.model_fields["EMBEDDING_REQUEST_TIMEOUT"].json_schema_extra["platform_only"] is True
    assert EmotionConfig.model_fields["IMAGE_DOWNLOAD_TIMEOUT"].json_schema_extra["platform_only"] is True
    assert JudgementConfig.model_fields["MAX_MUTE_DURATION"].json_schema_extra["tenant_may_only_lower"] is True
    assert JudgementConfig.model_fields["MAX_MUTE_DURATION"].annotation is int


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def test_tenant_may_only_lower_fields_are_bool_or_int() -> None:
    builtin_dir = Path(__file__).resolve().parents[1] / "plugins" / "builtin"
    found: List[tuple[str, str, str]] = []
    for path in sorted(builtin_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name) or node.value is None:
                continue
            for call in ast.walk(node.value):
                if not isinstance(call, ast.Call) or _call_name(call.func) != "ExtraField":
                    continue
                lowered = any(
                    keyword.arg == "tenant_may_only_lower"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in call.keywords
                )
                if lowered:
                    found.append((path.name, node.target.id, ast.unparse(node.annotation)))

    assert found, "至少应该扫到 judgement.MAX_MUTE_DURATION"
    for file_name, field_name, annotation in found:
        assert annotation in {"bool", "int"}, f"{file_name}:{field_name} 的类型是 {annotation}，不是 bool/int"
