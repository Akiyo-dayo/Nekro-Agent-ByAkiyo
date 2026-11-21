"""配置里的非法值不能让 NA 起不来；后台写入非法值当场 400

## 由来

一次部署里 `WORLDBOOK_MAX_CHARS` 被设成了 200000（字段上限 20000），NA 重启后直接起不来。
读配置一律 `model_validate`，一个字段越界整份配置抛异常：全局配置在模块导入时加载，失败就
`exit(1)`；实例 / 频道覆盖文件失败时整份被丢掉（那一层所有覆盖悄悄失效）；插件的实例覆盖整份
回落到全局。写入侧只做类型转换、直接 `setattr`（配置模型不开赋值校验），所以后台能把越界值
存进文件。

## 这份测试钉住的

- **加载**（全局配置文件、实例 / 频道覆盖文件、插件全局配置、插件实例覆盖、适配器配置）：写进
  非法值，加载不崩；越界的截到边界，超长的截到上限，类型不对、枚举外的回到默认值；其他字段照常
  加载；每处记一条 ERROR，写明文件、字段、原值、改用的值。
- **启动**：子进程里导入全局配置，越界值不再让它退出。
- **写入**：后台保存（全局 / 实例 / 频道、单项与批量）、插件实例配置、聊天命令 `conf_set` 写非法值
  当场拒绝，说明允许范围；批量里有一项不合法就一项都不改。
- **覆盖层回到上一层**：插件实例覆盖、核心配置的实例 / 频道覆盖里，类型不对、枚举外的值去掉后
  生效的是上一层的值，不是代码默认值（否则「租户只能调低」的项会被悄悄放宽）；越界的仍截到边界。
- **写回前备份**：全局配置加载时做过修正，先把原文件逐字节备份成仅属主可访问的
  `nekro-agent.yaml.pre-repair-<时间>`，ERROR 日志里写出备份路径；没修正就不备份。
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import pytest
import yaml
from loguru import logger
from pydantic import Field

from nekro_agent.core import core_utils
from nekro_agent.core.config import CoreConfig
from nekro_agent.core.config import config as GLOBAL_CONFIG
from nekro_agent.core.core_utils import (
    PRE_REPAIR_BACKUP_INFIX,
    ConfigBase,
    ConfigManager,
    validate_config_data,
)
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import config as config_router
from nekro_agent.schemas.errors import ConfigInvalidError
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services import config_service as config_service_module
from nekro_agent.services.command.built_in.config_cmd import ConfSetCommand
from nekro_agent.services.command.schemas import CommandExecutionContext, CommandResponseStatus
from nekro_agent.services.config_resolver import ConfigLayer, config_resolver
from nekro_agent.services.config_service import CHANNEL_OVERRIDE_KEY_PREFIX, ConfigService, UnifiedConfigService
from nekro_agent.services.plugin import manager as plugin_manager
from nekro_agent.services.plugin import scope as plugin_scope
from nekro_agent.services.user.role import Role
from nekro_agent.tools import private_file

REPO_ROOT = Path(__file__).resolve().parents[1]
WORLDBOOK_MAX = 20000
INSTANCE_KEY = "instance_override_onebot_v11__cfgval"


@pytest.fixture
def error_logs() -> Any:
    """收下这条用例里打出的 ERROR 日志正文"""
    messages: List[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="ERROR")
    yield messages
    logger.remove(sink_id)


def _write_yaml(path: Path, data: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _assert_logged(messages: List[str], *fragments: str) -> None:
    assert any(all(fragment in message for fragment in fragments) for message in messages), messages


def _pre_repair_backups(config_file: Path) -> List[Path]:
    return sorted(config_file.parent.glob(f"{config_file.name}{PRE_REPAIR_BACKUP_INFIX}*"))


class _PluginLikeConfig(ConfigBase):
    """插件配置的同形：一个有范围的整数、一个普通字符串"""

    LIMIT: int = Field(default=10, ge=1, le=100)
    NAME: str = "global"


def platform_owner() -> DBUser:
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


# ============================================================
# 加载：每一层写进非法值都不崩、就近修正、其余照常、记 ERROR
# ============================================================


class TestGlobalConfigFile:
    def test_out_of_range_value_is_clamped_and_the_rest_loads(self, tmp_path: Path, error_logs: List[str]) -> None:
        path = _write_yaml(
            tmp_path / "nekro-agent.yaml",
            {
                "WORLDBOOK_MAX_CHARS": 200000,
                "AI_CHAT_CONTEXT_MAX_LENGTH": 15,
                "AI_DEBOUNCE_WAIT_SECONDS": "not-a-number",
                "MODEL_GROUPS": {"default": {"CHAT_MODEL": "m1", "MODEL_TYPE": "bogus"}},
            },
        )

        loaded = CoreConfig.load_from_path(path)

        assert loaded.WORLDBOOK_MAX_CHARS == WORLDBOOK_MAX
        assert loaded.AI_CHAT_CONTEXT_MAX_LENGTH == 15
        assert loaded.AI_DEBOUNCE_WAIT_SECONDS == CoreConfig.model_fields["AI_DEBOUNCE_WAIT_SECONDS"].default
        assert loaded.MODEL_GROUPS["default"].MODEL_TYPE == "chat"
        assert loaded.MODEL_GROUPS["default"].CHAT_MODEL == "m1"
        _assert_logged(error_logs, str(path), "WORLDBOOK_MAX_CHARS", "200000", str(WORLDBOOK_MAX))
        _assert_logged(error_logs, str(path), "AI_DEBOUNCE_WAIT_SECONDS", "not-a-number")
        _assert_logged(error_logs, str(path), "MODEL_GROUPS.default.MODEL_TYPE", "bogus", "'chat'")

    def test_an_empty_file_loads_as_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "nekro-agent.yaml"
        path.write_text("", encoding="utf-8")

        assert CoreConfig.load_from_path(path).WORLDBOOK_MAX_CHARS == CoreConfig().WORLDBOOK_MAX_CHARS

    @staticmethod
    def _import_config_in_a_subprocess(tmp_path: Path, data_dir: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["NEKRO_DATA_DIR"] = str(data_dir)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH", "")]))
        for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
            env.pop(name, None)
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from nekro_agent.core.config import config; "
                "print('LIMIT', config.WORLDBOOK_MAX_CHARS, 'CTX', config.AI_CHAT_CONTEXT_MAX_LENGTH)",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )

    def test_importing_the_config_at_startup_no_longer_exits(self, tmp_path: Path) -> None:
        """NA 启动时在模块导入阶段加载全局配置；以前越界值在这里 `exit(1)`

        导入期紧接着整份写回：写回前原文件备份了一份，第二次启动文件已经干净，不再备份。
        """
        data_dir = tmp_path / "data"
        config_file = _write_yaml(
            data_dir / "configs" / "nekro-agent.yaml",
            {"WORLDBOOK_MAX_CHARS": 200000, "AI_CHAT_CONTEXT_MAX_LENGTH": 15},
        )
        original_bytes = config_file.read_bytes()

        result = self._import_config_in_a_subprocess(tmp_path, data_dir)
        output = result.stdout + result.stderr

        assert result.returncode == 0, output
        assert f"LIMIT {WORLDBOOK_MAX} CTX 15" in result.stdout, output
        assert "WORLDBOOK_MAX_CHARS" in output and "200000" in output, "没有记下修正的 ERROR"
        # 导入期那次落盘写回的是修正后的值：下次启动不再报
        assert yaml.safe_load(config_file.read_text(encoding="utf-8"))["WORLDBOOK_MAX_CHARS"] == WORLDBOOK_MAX
        (backup,) = _pre_repair_backups(config_file)
        assert backup.read_bytes() == original_bytes, "备份的应是修正前的原文件"
        assert str(backup) in output, "ERROR 日志里要写出备份路径"

        again = self._import_config_in_a_subprocess(tmp_path, data_dir)

        assert again.returncode == 0, again.stdout + again.stderr
        assert _pre_repair_backups(config_file) == [backup], "没做修正的那次启动不该再备份"


class TestOverrideFiles:
    def test_an_invalid_override_does_not_drop_the_others(self, tmp_path: Path, error_logs: List[str]) -> None:
        path = _write_yaml(
            tmp_path / "overrides.yaml",
            {
                "enable_WORLDBOOK_MAX_CHARS": True,
                "WORLDBOOK_MAX_CHARS": 200000,
                "enable_AI_CHAT_CONTEXT_MAX_LENGTH": True,
                "AI_CHAT_CONTEXT_MAX_LENGTH": 15,
            },
        )

        loaded = OverridableConfig.load_from_path(path)

        assert loaded.enable_WORLDBOOK_MAX_CHARS is True
        assert loaded.WORLDBOOK_MAX_CHARS == WORLDBOOK_MAX
        assert loaded.enable_AI_CHAT_CONTEXT_MAX_LENGTH is True
        assert loaded.AI_CHAT_CONTEXT_MAX_LENGTH == 15
        _assert_logged(error_logs, str(path), "WORLDBOOK_MAX_CHARS", "200000")

    def test_a_channel_override_file_with_a_bad_value_still_takes_effect(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """以前这里整份加载失败、返回 None：这个频道的全部覆盖悄悄失效"""
        chat_key = "onebot_v11-group_424242"
        config_key = f"{CHANNEL_OVERRIDE_KEY_PREFIX}{chat_key}"
        monkeypatch.setattr(config_service_module, "CHANNEL_CONFIG_DIR", tmp_path)
        _write_yaml(
            tmp_path / "onebot_v11" / f"{chat_key}.yaml",
            {
                "enable_WORLDBOOK_MAX_CHARS": True,
                "WORLDBOOK_MAX_CHARS": 200000,
                "enable_AI_CHAT_CONTEXT_MAX_LENGTH": True,
                "AI_CHAT_CONTEXT_MAX_LENGTH": 15,
            },
        )
        ConfigManager.unregister_config(config_key)
        try:
            layer = UnifiedConfigService._get_config_instance(config_key)  # noqa: SLF001
        finally:
            ConfigManager.unregister_config(config_key)

        assert isinstance(layer, OverridableConfig)
        assert layer.WORLDBOOK_MAX_CHARS == WORLDBOOK_MAX
        assert layer.AI_CHAT_CONTEXT_MAX_LENGTH == 15


class TestPluginConfig:
    def test_global_plugin_config_file(self, tmp_path: Path, error_logs: List[str]) -> None:
        path = _write_yaml(tmp_path / "plugin.yaml", {"LIMIT": 1000, "NAME": "mine"})

        loaded = _PluginLikeConfig.load_config(file_path=path, auto_register=False)

        assert loaded.LIMIT == 100
        assert loaded.NAME == "mine"
        _assert_logged(error_logs, str(path), "LIMIT", "1000", "100")

    def test_an_invalid_instance_override_keeps_the_other_overrides(
        self,
        monkeypatch: pytest.MonkeyPatch,
        error_logs: List[str],
    ) -> None:
        """以前整份回落到全局：`NAME` 也回到 "global"，这个实例的覆盖全部失效"""
        monkeypatch.setattr(
            plugin_scope, "load_instance_config_overrides", lambda *_a: {"LIMIT": 0, "NAME": "instance"}
        )
        plugin_scope.invalidate_scoped_config("cfgval.plugin")
        try:
            merged = plugin_scope.resolve_scoped_config("cfgval.plugin", _PluginLikeConfig, _PluginLikeConfig(), "inst")
        finally:
            plugin_scope.invalidate_scoped_config("cfgval.plugin")

        assert isinstance(merged, _PluginLikeConfig)
        assert merged.LIMIT == 1
        assert merged.NAME == "instance"
        _assert_logged(error_logs, "LIMIT", "与全局配置合并后")


class _FakePlugin:
    key = "cfgval.plugin"
    config_class = _PluginLikeConfig

    def get_global_config(self) -> _PluginLikeConfig:
        return _PluginLikeConfig()


class TestPluginInstanceSave:
    @pytest.fixture
    def disk(self, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
        store: Dict[str, Any] = {"overrides": {}, "saved": None}
        monkeypatch.setattr(plugin_manager.plugin_collector, "get_plugin", lambda _plugin_id: _FakePlugin())
        monkeypatch.setattr(plugin_manager, "load_instance_config_overrides", lambda *_a: dict(store["overrides"]))

        def save(_plugin_key: str, _instance_key: str, overrides: Dict[str, Any]) -> None:
            store["saved"] = dict(overrides)

        monkeypatch.setattr(plugin_manager, "save_instance_config_overrides", save)
        return store

    async def test_an_out_of_range_value_is_rejected_with_the_range(self, disk: Dict[str, Any]) -> None:
        success, message = await plugin_manager.save_plugin_instance_config("cfgval.plugin", "inst", {"LIMIT": "1000"})

        assert success is False
        assert message is not None and "允许范围 1 ~ 100" in message
        assert disk["saved"] is None

    async def test_a_bad_value_already_on_disk_does_not_block_saving(self, disk: Dict[str, Any]) -> None:
        """以前草稿 `model_validate` 失败，这个实例的插件配置再也存不进去"""
        disk["overrides"] = {"LIMIT": 1000}

        success, message = await plugin_manager.save_plugin_instance_config("cfgval.plugin", "inst", {"NAME": "x"})

        assert success is True, message
        assert disk["saved"] == {"LIMIT": 100, "NAME": "x"}


class TestAdapterConfig:
    def test_web_adapter_config_file(self, tmp_path: Path, error_logs: List[str]) -> None:
        from nekro_agent.adapters.web.config import WebAdapterConfig

        path = _write_yaml(
            tmp_path / "web.yaml",
            {"MESSAGE_MAX_LENGTH": 999999, "FILE_UPLOAD_MAX_SIZE_MB": 0, "WEB_USER_NAME_TEMPLATE": "x" * 80},
        )

        loaded = WebAdapterConfig.load_from_path(path)

        assert loaded.MESSAGE_MAX_LENGTH == 32000
        assert loaded.FILE_UPLOAD_MAX_SIZE_MB == 1
        assert loaded.WEB_USER_NAME_TEMPLATE == "x" * 64
        _assert_logged(error_logs, str(path), "MESSAGE_MAX_LENGTH")
        _assert_logged(error_logs, str(path), "WEB_USER_NAME_TEMPLATE")


# ============================================================
# 覆盖层：去掉的项回到上一层，不是代码默认值
# ============================================================


class _PluginWithChoices(ConfigBase):
    """插件配置的同形：一个枚举、一个开关、一个普通字符串；代码默认值与全局配置里的值不同"""

    MODE: Literal["fast", "slow", "off"] = "fast"
    ALLOW_UPLOAD: bool = True
    NAME: str = "global"


def _plugin_global() -> _PluginWithChoices:
    """管理员在全局配置里调过的值：MODE 关、ALLOW_UPLOAD 关"""
    return _PluginWithChoices(MODE="off", ALLOW_UPLOAD=False)


class _FakeChoicesPlugin:
    key = "cfgval.choices"
    config_class = _PluginWithChoices

    def get_global_config(self) -> _PluginWithChoices:
        return _plugin_global()


class TestPluginOverrideFallsBackToTheGlobalValue:
    def test_enum_outside_and_type_error_take_the_global_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
        error_logs: List[str],
    ) -> None:
        monkeypatch.setattr(
            plugin_scope,
            "load_instance_config_overrides",
            lambda *_a: {"MODE": "turbo", "ALLOW_UPLOAD": "sometimes", "NAME": "instance"},
        )
        plugin_scope.invalidate_scoped_config("cfgval.choices")
        try:
            merged = plugin_scope.resolve_scoped_config("cfgval.choices", _PluginWithChoices, _plugin_global(), "inst")
        finally:
            plugin_scope.invalidate_scoped_config("cfgval.choices")

        assert isinstance(merged, _PluginWithChoices)
        assert merged.MODE == "off", "枚举外的值应回到全局配置里的 off，而不是代码默认值 fast"
        assert merged.ALLOW_UPLOAD is False, "回到代码默认值 True 等于把全局关掉的开关悄悄打开"
        assert merged.NAME == "instance"
        _assert_logged(error_logs, "MODE", "turbo", "上一层的值 'off'")
        _assert_logged(error_logs, "ALLOW_UPLOAD", "sometimes", "上一层的值 False")

    async def test_saving_drops_the_bad_override_instead_of_pinning_the_code_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """以前去掉的项回到代码默认值 fast，与全局的 off 不同，于是被当成覆盖写回了文件"""
        saved: Dict[str, Any] = {}
        monkeypatch.setattr(plugin_manager.plugin_collector, "get_plugin", lambda _plugin_id: _FakeChoicesPlugin())
        monkeypatch.setattr(plugin_manager, "load_instance_config_overrides", lambda *_a: {"MODE": "turbo"})
        monkeypatch.setattr(
            plugin_manager,
            "save_instance_config_overrides",
            lambda _plugin_key, _instance_key, overrides: saved.update(overrides=dict(overrides)),
        )

        success, message = await plugin_manager.save_plugin_instance_config("cfgval.choices", "inst", {"NAME": "x"})

        assert success is True, message
        assert saved["overrides"] == {"NAME": "x"}

    def test_a_fallback_that_is_itself_invalid_does_not_loop(self) -> None:
        loaded = validate_config_data(
            _PluginWithChoices,
            {"MODE": "turbo"},
            source="unit",
            fallback={"MODE": "also-bad"},
        )

        assert loaded.MODE == "fast"


class TestCoreOverrideLayerInheritsTheUpperLayer:
    def test_bad_enabled_values_turn_the_override_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_logs: List[str],
    ) -> None:
        monkeypatch.setattr(GLOBAL_CONFIG, "OUTPUT_FILTER_MODE", "whole")
        monkeypatch.setattr(GLOBAL_CONFIG, "AI_CHAT_CONTEXT_MAX_LENGTH", 50)
        path = _write_yaml(
            tmp_path / "channel.yaml",
            {
                "enable_OUTPUT_FILTER_MODE": True,
                "OUTPUT_FILTER_MODE": "everything",
                "enable_AI_CHAT_CONTEXT_MAX_LENGTH": True,
                "AI_CHAT_CONTEXT_MAX_LENGTH": "lots",
                "enable_WORLDBOOK_MAX_CHARS": True,
                "WORLDBOOK_MAX_CHARS": 200000,
            },
        )
        instance_layer = OverridableConfig(enable_AI_CHAT_CONTEXT_MAX_LENGTH=True, AI_CHAT_CONTEXT_MAX_LENGTH=40)

        channel_layer = OverridableConfig.load_from_path(path)

        assert channel_layer.enable_OUTPUT_FILTER_MODE is False
        assert channel_layer.enable_AI_CHAT_CONTEXT_MAX_LENGTH is False
        # 截到边界的不受影响：截完的值合法，覆盖照常生效
        assert channel_layer.enable_WORLDBOOK_MAX_CHARS is True
        assert channel_layer.WORLDBOOK_MAX_CHARS == WORLDBOOK_MAX
        effective = config_resolver._merge(  # noqa: SLF001
            [(ConfigLayer.CHANNEL, channel_layer), (ConfigLayer.INSTANCE, instance_layer)],
        )
        assert effective["OUTPUT_FILTER_MODE"] == "whole", "频道层停用后继承全局配置，而不是代码默认值 fragment"
        assert effective["AI_CHAT_CONTEXT_MAX_LENGTH"] == 40, "上一层是实例层，继承它的 40"
        assert effective["WORLDBOOK_MAX_CHARS"] == WORLDBOOK_MAX
        _assert_logged(error_logs, str(path), "OUTPUT_FILTER_MODE", "everything", "enable_OUTPUT_FILTER_MODE 已关闭")
        _assert_logged(error_logs, str(path), "AI_CHAT_CONTEXT_MAX_LENGTH", "lots", "继承上一层")

        # 下次保存这一层，停用的项不再落盘
        saved = tmp_path / "saved.yaml"
        channel_layer.dump_config(saved)
        on_disk = yaml.safe_load(saved.read_text(encoding="utf-8"))
        assert "OUTPUT_FILTER_MODE" not in on_disk and "enable_OUTPUT_FILTER_MODE" not in on_disk
        assert "AI_CHAT_CONTEXT_MAX_LENGTH" not in on_disk
        assert on_disk["WORLDBOOK_MAX_CHARS"] == WORLDBOOK_MAX

    def test_an_instance_override_file_through_the_real_loader(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """实例层走 `_get_config_instance` 的真实加载路径：枚举外的覆盖被停用、开关一并关掉"""
        layer_file = _write_yaml(
            tmp_path / "onebot_v11" / "cfgval.yaml",
            {"enable_OUTPUT_FILTER_MODE": True, "OUTPUT_FILTER_MODE": 7},
        )
        monkeypatch.setattr(config_service_module, "instance_override_config_path", lambda *_a: layer_file)
        ConfigManager.unregister_config(INSTANCE_KEY)
        try:
            layer = UnifiedConfigService._get_config_instance(INSTANCE_KEY)  # noqa: SLF001
        finally:
            ConfigManager.unregister_config(INSTANCE_KEY)

        assert isinstance(layer, OverridableConfig)
        assert layer.enable_OUTPUT_FILTER_MODE is False


# ============================================================
# 写回前备份：全局配置加载时做过修正，先把原文件备份一份
# ============================================================


class _FakeIcacls:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.calls: List[List[str]] = []

    def __call__(self, args: List[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, self.returncode, "", "")


class TestGlobalConfigIsBackedUpBeforeTheRepairedRewrite:
    @staticmethod
    def _bad_file(tmp_path: Path) -> Path:
        path = tmp_path / "nekro-agent.yaml"
        # 带注释与 CRLF：备份必须与原文件逐字节一致，不是重新序列化出来的
        path.write_bytes("# 手写的注释\r\nWORLDBOOK_MAX_CHARS: 200000\r\nOUTPUT_FILTER_MODE: everything\r\n".encode())
        return path

    def test_a_repaired_load_backs_up_the_original_and_names_it_in_the_log(
        self,
        tmp_path: Path,
        error_logs: List[str],
    ) -> None:
        path = self._bad_file(tmp_path)
        original = path.read_bytes()

        CoreConfig.load_from_path(path)

        (backup,) = _pre_repair_backups(path)
        assert backup.name.startswith("nekro-agent.yaml.pre-repair-")
        assert backup.read_bytes() == original
        assert path.read_bytes() == original, "加载本身不改原文件，写回是导入期那次 dump 的事"
        _assert_logged(error_logs, "WORLDBOOK_MAX_CHARS", str(backup))
        _assert_logged(error_logs, "OUTPUT_FILTER_MODE", str(backup))

    def test_a_clean_load_makes_no_backup(self, tmp_path: Path, error_logs: List[str]) -> None:
        path = _write_yaml(tmp_path / "nekro-agent.yaml", {"WORLDBOOK_MAX_CHARS": 4000})

        CoreConfig.load_from_path(path)

        assert _pre_repair_backups(path) == []
        assert not any("备份" in message for message in error_logs), error_logs

    def test_layers_that_are_not_rewritten_on_load_are_not_backed_up(self, tmp_path: Path) -> None:
        """覆盖层、插件配置加载后不会立刻整份写回：不备份，免得每加载一次攒一份"""
        overrides = _write_yaml(
            tmp_path / "overrides.yaml",
            {"enable_WORLDBOOK_MAX_CHARS": True, "WORLDBOOK_MAX_CHARS": 200000},
        )
        plugin = _write_yaml(tmp_path / "plugin.yaml", {"LIMIT": 1000})

        OverridableConfig.load_from_path(overrides)
        _PluginLikeConfig.load_from_path(plugin)

        assert _pre_repair_backups(overrides) == [] and _pre_repair_backups(plugin) == []

    def test_a_failed_backup_does_not_stop_the_load_and_says_so(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_logs: List[str],
    ) -> None:
        def refuse(_path: Path, _content: Any) -> bool:
            raise PermissionError("denied")

        monkeypatch.setattr(core_utils, "write_private_file", refuse)
        path = self._bad_file(tmp_path)

        loaded = CoreConfig.load_from_path(path)

        assert loaded.WORLDBOOK_MAX_CHARS == WORLDBOOK_MAX
        assert _pre_repair_backups(path) == []
        _assert_logged(error_logs, "WORLDBOOK_MAX_CHARS", "没能备份", "PermissionError")

    def test_the_backup_is_created_0600_and_never_overwrites(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """跨平台钉住创建参数：0600 + O_EXCL（Windows 上 stat 读不出权限位，改为截获 os.open）"""
        calls: List[tuple] = []
        real_open = os.open

        def spy(target: Any, flags: int, mode: int = 0o777, *args: Any, **kwargs: Any) -> int:
            calls.append((str(target), flags, mode))
            return real_open(target, flags, mode, *args, **kwargs)

        monkeypatch.setattr(private_file.os, "open", spy)
        path = self._bad_file(tmp_path)

        CoreConfig.load_from_path(path)

        (backup,) = _pre_repair_backups(path)
        (call,) = [c for c in calls if c[0] == str(backup)]
        assert call[2] == 0o600
        assert call[1] & os.O_EXCL and call[1] & os.O_CREAT

    @pytest.mark.parametrize(
        ("windows", "icacls_returncode", "expected", "absent"),
        [
            (False, 0, "0600", "icacls"),
            (True, 0, "仅当前用户", "0600"),
            (True, 5, "未能收紧", "0600"),
        ],
    )
    def test_the_log_states_the_protection_per_platform(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_logs: List[str],
        windows: bool,
        icacls_returncode: int,
        expected: str,
        absent: str,
    ) -> None:
        fake = _FakeIcacls(icacls_returncode)
        monkeypatch.setattr(private_file, "is_windows", lambda: windows)
        monkeypatch.setattr(private_file.subprocess, "run", fake)
        monkeypatch.setenv("USERNAME", "na-test-user")
        path = self._bad_file(tmp_path)

        CoreConfig.load_from_path(path)

        (backup,) = _pre_repair_backups(path)
        assert len(fake.calls) == (1 if windows else 0)
        (message,) = [m for m in error_logs if "WORLDBOOK_MAX_CHARS" in m and str(backup) in m]
        assert expected in message and absent not in message, message

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows 的 st_mode 不反映真实权限")
    def test_real_posix_mode_is_0600(self, tmp_path: Path) -> None:
        path = self._bad_file(tmp_path)

        CoreConfig.load_from_path(path)

        (backup,) = _pre_repair_backups(path)
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform != "win32", reason="真实 ACL 只在 Windows 上有意义")
    def test_real_windows_acl_has_no_other_account_and_no_inheritance(self, tmp_path: Path) -> None:
        path = self._bad_file(tmp_path)

        CoreConfig.load_from_path(path)

        (backup,) = _pre_repair_backups(path)
        listing = subprocess.run(["icacls", str(backup)], capture_output=True, text=True, check=True).stdout
        entries = [line.replace(str(backup), "").strip().lower() for line in listing.splitlines() if ":(" in line]
        # 完整性级别标签（Mandatory Label）不是授权项，不管谁能读
        entries = [entry for entry in entries if not entry.startswith("mandatory label\\")]
        assert entries, listing
        user = os.environ["USERNAME"].lower()
        assert any(user in entry for entry in entries), listing
        # pytest 的临时目录是以 0o700 建的，Windows 上那会给目录写上显式的 SYSTEM / Administrators /
        # OWNER RIGHTS 项，收紧后它们可能还留在文件上：前两个本来就能读任何文件，OWNER RIGHTS 就是属主自己，
        # 都不算放宽。普通数据目录下收紧后只剩当前用户（本机验过）
        privileged = ("nt authority\\system:", "builtin\\administrators:", "owner rights:")
        for entry in entries:
            assert "(i)" not in entry, f"仍在继承目录权限: {entry}"
            assert user in entry or entry.startswith(privileged), f"ACL 里还有别人: {entry}"


# ============================================================
# 写入：非法值当场拒绝，说明允许范围；批量全有或全无
# ============================================================


class TestConfigServiceWrites:
    def test_out_of_range_is_rejected_with_the_range(self) -> None:
        cfg = CoreConfig()
        before = cfg.WORLDBOOK_MAX_CHARS

        success, message = ConfigService.set_config_value(cfg, "WORLDBOOK_MAX_CHARS", "200000")

        assert success is False
        assert "无效" in message and "允许范围 0 ~ 20000" in message
        assert cfg.WORLDBOOK_MAX_CHARS == before

    def test_in_range_still_works(self) -> None:
        cfg = CoreConfig()

        assert ConfigService.set_config_value(cfg, "WORLDBOOK_MAX_CHARS", "20000") == (True, "")
        assert cfg.WORLDBOOK_MAX_CHARS == WORLDBOOK_MAX

    def test_a_non_number_is_rejected_as_invalid(self) -> None:
        success, message = ConfigService.set_config_value(CoreConfig(), "WORLDBOOK_MAX_CHARS", "abc")

        assert success is False
        assert "无效" in message and "需要整数" in message

    def test_batch_is_all_or_nothing(self) -> None:
        cfg = CoreConfig()
        before = cfg.AI_CHAT_CONTEXT_MAX_LENGTH

        success, message = ConfigService.batch_update_config(
            cfg,
            {"AI_CHAT_CONTEXT_MAX_LENGTH": str(before + 7), "WORLDBOOK_MAX_CHARS": "200000"},
        )

        assert success is False
        assert message is not None and "WORLDBOOK_MAX_CHARS" in message
        assert cfg.AI_CHAT_CONTEXT_MAX_LENGTH == before, "前面那一项不该先被写进去"

    def test_override_layer_is_checked_too(self) -> None:
        layer = OverridableConfig()

        success, message = ConfigService.batch_update_config(
            layer,
            {"enable_WORLDBOOK_MAX_CHARS": "true", "WORLDBOOK_MAX_CHARS": "200000"},
        )

        assert success is False
        assert "允许范围 0 ~ 20000" in (message or "")
        assert layer.enable_WORLDBOOK_MAX_CHARS is False


class _RouterHarness:
    """给真实路由装一份内存里的配置对象，记下有没有落盘"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, layers: Dict[str, Any]) -> None:
        self.saved: List[str] = []
        monkeypatch.setattr(
            UnifiedConfigService,
            "_get_config_instance",
            staticmethod(lambda config_key: layers.get(config_key)),
        )
        monkeypatch.setattr(UnifiedConfigService, "save_config", staticmethod(self._save))

        async def no_audit_target(_config_key: str) -> None:
            return None

        monkeypatch.setattr(config_router, "_audit_target_for", no_audit_target)

        # 平台方写实例层 / 频道层覆盖要求目标实例存在（查库）；这里测的是取值校验，没有库，目标一律当作存在
        async def target_exists(_config_key: str) -> None:
            return None

        monkeypatch.setattr(config_router, "_assert_override_target_exists", target_exists)

    def _save(self, config_key: str, _file_path: Optional[Path] = None) -> tuple[bool, None]:
        self.saved.append(config_key)
        return True, None


class TestRouterWrites:
    async def test_batch_save_of_the_global_config_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = CoreConfig()
        harness = _RouterHarness(monkeypatch, {"system": cfg})

        with pytest.raises(ConfigInvalidError) as excinfo:
            await config_router.batch_update_config(
                "system",
                config_router.BatchUpdateConfig(configs={"WORLDBOOK_MAX_CHARS": "200000"}),
                _current_user=platform_owner(),
            )

        assert excinfo.value.http_status == 400
        assert "允许范围 0 ~ 20000" in str(excinfo.value.params["reason"])
        assert harness.saved == []
        assert cfg.WORLDBOOK_MAX_CHARS == CoreConfig().WORLDBOOK_MAX_CHARS

    async def test_batch_save_of_an_instance_override_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        layer = OverridableConfig()
        harness = _RouterHarness(monkeypatch, {INSTANCE_KEY: layer})

        with pytest.raises(ConfigInvalidError):
            await config_router.batch_update_config(
                INSTANCE_KEY,
                config_router.BatchUpdateConfig(
                    configs={"enable_WORLDBOOK_MAX_CHARS": "true", "WORLDBOOK_MAX_CHARS": "200000"},
                ),
                _current_user=platform_owner(),
            )

        assert harness.saved == []
        assert layer.enable_WORLDBOOK_MAX_CHARS is False

    async def test_single_item_set_is_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = CoreConfig()
        _RouterHarness(monkeypatch, {"system": cfg})

        with pytest.raises(ConfigInvalidError) as excinfo:
            await config_router.set_config_value(
                "system",
                "WORLDBOOK_MAX_CHARS",
                value="200000",
                _current_user=platform_owner(),
            )

        assert excinfo.value.http_status == 400


class TestChatCommand:
    @staticmethod
    def _context() -> CommandExecutionContext:
        return CommandExecutionContext(
            user_id="platform-user",
            chat_key="group_1",
            username="tester",
            adapter_key="fake",
            is_super_user=True,
            is_advanced_user=False,
        )

    async def test_conf_set_rejects_an_out_of_range_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        global_config = GLOBAL_CONFIG
        monkeypatch.setattr(global_config, "WORLDBOOK_MAX_CHARS", 3000)

        response = await ConfSetCommand().execute(self._context(), expr="WORLDBOOK_MAX_CHARS=200000")

        assert response.status == CommandResponseStatus.ERROR
        assert "允许范围 0 ~ 20000" in response.message
        assert global_config.WORLDBOOK_MAX_CHARS == 3000

    async def test_conf_set_false_sets_a_bool_to_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """以前先判 int 再判 bool：布尔项走 `bool("false")`，设出来是 True"""
        global_config = GLOBAL_CONFIG
        monkeypatch.setattr(global_config, "AI_ALWAYS_INCLUDE_MSG_ID", True)

        response = await ConfSetCommand().execute(self._context(), expr="AI_ALWAYS_INCLUDE_MSG_ID=false")

        assert response.status == CommandResponseStatus.SUCCESS
        assert global_config.AI_ALWAYS_INCLUDE_MSG_ID is False
