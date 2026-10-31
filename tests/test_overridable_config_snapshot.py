"""作用域覆盖配置不再冻结陈旧快照

覆盖文件（频道 / 实例 / 适配器三层的 `overrides.yaml`、`{chat_key}.yaml`）在创建时
会被 `dump_config()` 写入**全部**可覆盖字段，包括那些覆盖开关关着、根本不参与解析的字段。
这份快照随源码默认值变更而变成陈旧数据：实测线上有频道文件存着
`AFFECTION_ENABLE_SYSTEM: false`（同文件里 `AFFECTION_ENABLE_PROMPT_INJECTION` 却是 `true`），
而源码默认值早已是 `true`。运行时无害（`enable_` 为 false，`config_resolver` 不采用），
但作用域配置页把它当"当前值"显示，用户一勾启用覆盖就把旧值钉死。

这里钉死四条：

1. **读**：覆盖关着的字段一律回落到字段默认值——存量文件因此不需要任何迁移就立刻失效；
2. **读**：真正启用的覆盖一个都不能被动到，否则这条修复就把用户的配置改没了；
3. **写**：只落盘启用了的覆盖对，不再生成新的快照；缺字段的旧文件仍能照常加载；
4. **显示**：覆盖关着的行回给界面的是"当前继承到的有效值"，界面所示即勾选后会钉住的值。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from nekro_agent.core.config import CoreConfig
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.routers.config import _with_inherited_override_values
from nekro_agent.services.config_resolver import config_resolver

# 取一组类型各异、且确实标了 overridable 的字段，避免只覆盖布尔一种形态
SAMPLE_FIELDS = ["AFFECTION_ENABLE_SYSTEM", "AI_CHAT_CONTEXT_MAX_LENGTH", "AI_COMMAND_OUTPUT_PREFIX"]


def source_default(field_name: str) -> Any:
    return CoreConfig.model_fields[field_name].get_default(call_default_factory=True)


def write_override(path: Path, content: Dict[str, Any]) -> Path:
    path.write_text(yaml.dump(content, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


class TestSampleFieldsAreOverridable:
    def test_fields_exist_in_override_model(self) -> None:
        """样本字段必须真的进了覆盖模型，否则下面的用例全是空转"""
        for name in SAMPLE_FIELDS:
            assert name in OverridableConfig.model_fields
            assert f"enable_{name}" in OverridableConfig.model_fields


class TestStaleSnapshotIsIgnoredOnLoad:
    @pytest.mark.parametrize(
        ("field_name", "stale_value"),
        [
            # 线上实测的那份陈旧快照：源码默认值早已改成 True
            pytest.param("AFFECTION_ENABLE_SYSTEM", False, id="bool"),
            pytest.param("AI_CHAT_CONTEXT_MAX_LENGTH", 12345, id="int"),
            pytest.param("AI_COMMAND_OUTPUT_PREFIX", "≡STALE≡:", id="str"),
        ],
    )
    def test_disabled_field_falls_back_to_source_default(self, tmp_path: Path, field_name: str, stale_value: Any) -> None:
        """冻结的旧值不被信任，读出来的是当前源码默认值"""
        default = source_default(field_name)
        assert stale_value != default  # 用例本身要有区分度
        path = write_override(tmp_path / "c.yaml", {f"enable_{field_name}": False, field_name: stale_value})

        loaded = OverridableConfig.load_from_path(path)

        assert getattr(loaded, field_name) == default

    def test_real_override_is_untouched(self, tmp_path: Path) -> None:
        """启用了的覆盖必须原样保留——这是用户真正配置过的数据"""
        path = write_override(
            tmp_path / "c.yaml",
            {
                "enable_AI_CHAT_CONTEXT_MAX_LENGTH": True,
                "AI_CHAT_CONTEXT_MAX_LENGTH": 64,
                "enable_AFFECTION_ENABLE_SYSTEM": False,
                "AFFECTION_ENABLE_SYSTEM": False,
            },
        )

        loaded = OverridableConfig.load_from_path(path)

        assert loaded.enable_AI_CHAT_CONTEXT_MAX_LENGTH is True
        assert loaded.AI_CHAT_CONTEXT_MAX_LENGTH == 64
        assert loaded.AFFECTION_ENABLE_SYSTEM == source_default("AFFECTION_ENABLE_SYSTEM")

    def test_field_missing_from_file_still_loads(self, tmp_path: Path) -> None:
        """瘦身后的文件（乃至空文件）要能照常加载"""
        path = write_override(tmp_path / "c.yaml", {})

        loaded = OverridableConfig.load_from_path(path)

        assert loaded.enable_AFFECTION_ENABLE_SYSTEM is False
        assert loaded.AFFECTION_ENABLE_SYSTEM == source_default("AFFECTION_ENABLE_SYSTEM")


class TestDumpKeepsOnlyActiveOverrides:
    def test_new_file_has_no_snapshot(self, tmp_path: Path) -> None:
        """新建覆盖文件不再冻结任何字段"""
        path = tmp_path / "c.yaml"
        OverridableConfig.load_from_path(path).dump_config()

        assert yaml.safe_load(path.read_text(encoding="utf-8")) == {}

    def test_existing_snapshot_is_slimmed_on_save(self, tmp_path: Path) -> None:
        """存量文件在下一次保存时自然瘦身，启用的那对留下"""
        path = write_override(
            tmp_path / "c.yaml",
            {
                "enable_AI_CHAT_CONTEXT_MAX_LENGTH": True,
                "AI_CHAT_CONTEXT_MAX_LENGTH": 64,
                "enable_AFFECTION_ENABLE_SYSTEM": False,
                "AFFECTION_ENABLE_SYSTEM": False,
                "enable_AI_COMMAND_OUTPUT_PREFIX": False,
                "AI_COMMAND_OUTPUT_PREFIX": "≡OLD≡:",
            },
        )

        OverridableConfig.load_from_path(path).dump_config()

        assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
            "enable_AI_CHAT_CONTEXT_MAX_LENGTH": True,
            "AI_CHAT_CONTEXT_MAX_LENGTH": 64,
        }

    def test_round_trip_preserves_active_override(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        config = OverridableConfig.load_from_path(path)
        config.enable_AI_COMMAND_OUTPUT_PREFIX = True
        config.AI_COMMAND_OUTPUT_PREFIX = "≡NEW≡:"
        config.dump_config()

        reloaded = OverridableConfig.load_from_path(path)

        assert reloaded.enable_AI_COMMAND_OUTPUT_PREFIX is True
        assert reloaded.AI_COMMAND_OUTPUT_PREFIX == "≡NEW≡:"


def make_items(*, toggle_on: bool, stored_value: Any) -> List[Dict[str, Any]]:
    """两行一组的覆盖配置列表：开关行 + 值行（形态同 `/config/list` 的返回）"""
    return [
        {"key": "enable_AFFECTION_ENABLE_SYSTEM", "value": toggle_on, "title": "覆盖: 启用好感度系统"},
        {
            "key": "AFFECTION_ENABLE_SYSTEM",
            "value": stored_value,
            "title": "启用好感度系统",
            "enable_toggle": "enable_AFFECTION_ENABLE_SYSTEM",
        },
        {"key": "SOME_PLAIN_FIELD", "value": "untouched", "title": "普通字段"},
    ]


def value_of(items: List[Dict[str, Any]], key: str) -> Any:
    return next(item["value"] for item in items if item["key"] == key)


class TestDisabledRowsShowInheritedValue:
    """覆盖关着时值列显示上层此刻真正生效的值，而不是文件里存的那份"""

    @pytest.fixture
    def inherited(self, monkeypatch) -> Dict[str, Any]:
        values = {"AFFECTION_ENABLE_SYSTEM": True, "SOME_PLAIN_FIELD": "inherited-should-not-apply"}

        async def fake_inherited_values(config_key: str) -> Optional[Dict[str, Any]]:
            return values if config_key.startswith("channel_config_") else None

        monkeypatch.setattr(config_resolver, "inherited_values", fake_inherited_values)
        return values

    async def test_disabled_row_uses_inherited_value(self, inherited) -> None:  # noqa: ARG002
        items = await _with_inherited_override_values("channel_config_x", make_items(toggle_on=False, stored_value=False))

        assert value_of(items, "AFFECTION_ENABLE_SYSTEM") is True

    async def test_enabled_row_keeps_its_own_value(self, inherited) -> None:  # noqa: ARG002
        """启用了的覆盖显示的必须是它自己的值，否则界面会谎报当前配置"""
        items = await _with_inherited_override_values("channel_config_x", make_items(toggle_on=True, stored_value=False))

        assert value_of(items, "AFFECTION_ENABLE_SYSTEM") is False

    async def test_rows_without_toggle_are_untouched(self, inherited) -> None:  # noqa: ARG002
        items = await _with_inherited_override_values("channel_config_x", make_items(toggle_on=False, stored_value=False))

        assert value_of(items, "SOME_PLAIN_FIELD") == "untouched"
        assert value_of(items, "enable_AFFECTION_ENABLE_SYSTEM") is False

    async def test_non_override_config_is_identity(self, inherited) -> None:  # noqa: ARG002
        """系统配置 / 插件配置不是覆盖层，一个字段都不能被改写"""
        original = make_items(toggle_on=False, stored_value=False)

        items = await _with_inherited_override_values("system", original)

        assert items == original


class TestInheritedValuesScopeDetection:
    async def test_plain_config_keys_have_no_inherited_layer(self) -> None:
        """只有三种覆盖层前缀才有"上层"，其余一律 None（调用方据此退化为恒等变换）"""
        for config_key in ("system", "plugin_foo", "adapter_onebot_v11"):
            assert await config_resolver.inherited_values(config_key) is None
