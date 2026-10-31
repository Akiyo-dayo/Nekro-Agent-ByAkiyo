"""配置重载之后，沙盒出口脱敏仍要遮住平台模型组的明文 Key（round-27 复核 P1-1）

## 背景

DECISIONS 的「API Key 隔离」硬约束路径 3：沙盒 RPC 方法的返回值与报错发回容器之前，
`credential_guard.collect_platform_secrets()` 遍历 `ConfigManager` 里注册的**全部**配置实例收集凭据，
`routers/rpc.py` 与 `routers/cc_relay.py` 据此脱敏。#61 之后落盘的 YAML 里 `MODEL_GROUPS.*.API_KEY`
是占位符，明文只在运行时视图（数据库接管后重建的 `config.MODEL_GROUPS`）里。

## 缺陷

两条重载路径都会把 `ConfigManager["system"]` 换成**一个没有明文的新实例**：

- `core.config.reload_config()`（聊天命令 `/reload`）：`CoreConfig.load_config()` 默认
  `auto_register=True`，注册的是读 YAML 得到的新实例；随后只把字段值抄回旧的 `config`，
  注册表里却留着那个新实例；
- `UnifiedConfigService.reload_config("system")`（`POST /config/reload/system`）：
  `load_from_path` + `register_config`，同样换掉注册表项，还绕过了占位符归一。

重载之后到下次重启之前，`collect_platform_secrets()` 收不到平台 Key，沙盒脱敏与 cc_relay 都不再遮它们。

## 用例形态

两个调用点各一组：真实走 `reload_config()` / `UnifiedConfigService.reload_config("system")`
（只把 YAML 路径指到 tmp，不 mock 加载函数），再断言注册表里仍是同一个活对象、
`collect_platform_secrets()` 仍含明文、`redact_sandbox_text` / `redact_sandbox_payload` 真的把明文遮成掩码。
样例凭据自造，不碰真实 Key。
"""

import os
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest
import yaml

from nekro_agent.core.config import (
    MODEL_GROUP_API_KEY_PLACEHOLDER,
    CoreConfig,
    ModelConfigGroup,
    config,
    reload_config,
)
from nekro_agent.core.core_utils import ConfigManager
from nekro_agent.services.config_service import UnifiedConfigService
from nekro_agent.services.sandbox.credential_guard import (
    REDACTED_PLACEHOLDER,
    collect_platform_secrets,
    redact_sandbox_payload,
    redact_sandbox_text,
)

SENTINEL_KEY = "sk-reload-SENTINEL-9f3a71c0e5"


@pytest.fixture
def reload_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """活配置里放一个带明文 Key 的平台模型组，YAML 路径指到 tmp（里头只有占位符）

    重载会把 `config` 的其余字段整体换成 YAML 里的值（这里近乎全默认），并往 `os.environ`
    灌 env——都在收尾时还原，避免漏给同一次会话里后面的用例。
    """
    yaml_path = tmp_path / "nekro-agent.yaml"
    yaml_path.write_text(
        yaml.safe_dump({"MODEL_GROUPS": {"default": {"API_KEY": MODEL_GROUP_API_KEY_PLACEHOLDER}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(CoreConfig, "_config_file_path_class", yaml_path)
    monkeypatch.setattr(config, "_config_file_path", yaml_path)
    monkeypatch.setattr(
        config,
        "MODEL_GROUPS",
        {"default": ModelConfigGroup(API_KEY=SENTINEL_KEY, CHAT_MODEL="gpt-4o")},
    )

    registered_before = ConfigManager.get_config("system")
    fields_before: Dict[str, Any] = {name: getattr(config, name) for name in CoreConfig.model_fields}
    environ_before = dict(os.environ)
    ConfigManager.register_config("system", config)
    try:
        yield yaml_path
    finally:
        for name, value in fields_before.items():
            if name != "MODEL_GROUPS":
                setattr(config, name, value)
        os.environ.clear()
        os.environ.update(environ_before)
        if registered_before is not None:
            ConfigManager.register_config("system", registered_before)


def _assert_still_redacted() -> None:
    """重载之后：注册表里仍是活对象、凭据集合含明文、两条脱敏出口真的遮住"""
    assert ConfigManager.get_config("system") is config, "重载把注册表里的 system 换成了另一个实例"
    assert SENTINEL_KEY in collect_platform_secrets(), "重载之后 collect_platform_secrets() 收不到平台 Key"

    secrets = collect_platform_secrets()
    text = redact_sandbox_text(f"上游报错: key={SENTINEL_KEY} 已失效", secrets)
    assert SENTINEL_KEY not in text and REDACTED_PLACEHOLDER in text
    payload = redact_sandbox_payload({"echo": [SENTINEL_KEY]}, secrets)
    assert SENTINEL_KEY not in repr(payload) and REDACTED_PLACEHOLDER in repr(payload)


class TestReloadKeepsPlatformSecretsRedacted:
    def test_positive_control_secret_is_collected_before_reload(self, reload_env: Path) -> None:
        """没有这一条，下面两组断言在「收集器本来就收不到」时也会绿"""
        assert ConfigManager.get_config("system") is config
        assert SENTINEL_KEY in collect_platform_secrets()

    def test_chat_command_reload_path(self, reload_env: Path) -> None:
        """`/reload` 命令：`core.config.reload_config()`"""
        reload_config()
        _assert_still_redacted()
        # 运行时视图没被占位符顶掉（#61 既有约束，顺手钉住）
        assert config.MODEL_GROUPS["default"].API_KEY == SENTINEL_KEY

    def test_console_reload_path(self, reload_env: Path) -> None:
        """`POST /config/reload/system`：`UnifiedConfigService.reload_config("system")`"""
        ok, error = UnifiedConfigService.reload_config("system")
        assert ok, error
        _assert_still_redacted()
        assert config.MODEL_GROUPS["default"].API_KEY == SENTINEL_KEY

    def test_console_reload_applies_the_yaml_to_the_live_object(self, reload_env: Path) -> None:
        """正向对照：重载确实读了 YAML 并改到了活对象上，而不是整条路径被短路成空操作"""
        reload_env.write_text(
            yaml.safe_dump(
                {
                    "PLUGIN_ISOLATION_COOLDOWN_SECONDS": 123.0,
                    "MODEL_GROUPS": {"default": {"API_KEY": MODEL_GROUP_API_KEY_PLACEHOLDER}},
                },
            ),
            encoding="utf-8",
        )
        ok, error = UnifiedConfigService.reload_config("system")
        assert ok, error
        assert config.PLUGIN_ISOLATION_COOLDOWN_SECONDS == 123.0
        assert ConfigManager.get_config("system") is config

    def test_console_reload_refuses_an_explicit_path_for_the_core_config(self, reload_env: Path, tmp_path: Path) -> None:
        """显式路径不能让 system 退回「换实例」的通用路径"""
        other = tmp_path / "other.yaml"
        other.write_text("{}", encoding="utf-8")
        ok, error = UnifiedConfigService.reload_config("system", other)
        assert not ok and error
        _assert_still_redacted()

    def test_non_system_configs_still_swap_in_the_new_instance(self, tmp_path: Path) -> None:
        """只有 CoreConfig 走专用路径；其他配置（插件 / 适配器）的重载行为不变"""
        from nekro_agent.core.core_utils import ConfigBase

        class _PluginLike(ConfigBase):
            VALUE: str = "default"

        path = tmp_path / "plugin.yaml"
        path.write_text(yaml.safe_dump({"VALUE": "from-disk"}), encoding="utf-8")
        old = _PluginLike()
        old.set_instance_config_file_path(path)
        ConfigManager.register_config("reload_test_plugin_like", old)
        try:
            ok, error = UnifiedConfigService.reload_config("reload_test_plugin_like")
            assert ok, error
            reloaded = ConfigManager.get_config("reload_test_plugin_like")
            assert reloaded is not old and getattr(reloaded, "VALUE") == "from-disk"
        finally:
            ConfigManager.unregister_config("reload_test_plugin_like")


class TestReloadInvalidatesTheEffectiveConfigCache:
    """重载之后必须 bump 有效配置版本号（round-27 第二轮复核 P2-5）

    `DBChatChannel._effective_config` 连同 `config_resolver.current_version()` 一起缓存，取用时版本号
    不一致才重新解析。`core.config.reload_config()` 是聊天命令 `conf_reload` 与
    `UnifiedConfigService.reload_config("system")` 的共同路径：只让后者 bump，命令重载之后频道
    读到的仍是重载前合并出来的旧配置——改了 YAML、`conf_reload` 显示成功，实际不生效。
    所以 bump 放在共同路径本身，而不是各调用方各写一遍。
    """

    def test_core_reload_bumps_the_version(self, reload_env: Path) -> None:
        from nekro_agent.services.config_resolver import config_resolver

        before = config_resolver.current_version()

        reload_config()

        assert config_resolver.current_version() > before

    async def test_conf_reload_command_bumps_the_version(self, reload_env: Path) -> None:
        """聊天命令 `conf_reload` 走的就是 `core.config.reload_config()`"""
        from nekro_agent.services.command.built_in.config_cmd import ConfReloadCommand
        from nekro_agent.services.config_resolver import config_resolver

        before = config_resolver.current_version()

        response = await ConfReloadCommand().execute(None)  # type: ignore[arg-type]

        assert "成功" in str(response.message) or "success" in str(response.message).lower()
        assert config_resolver.current_version() > before

    def test_console_reload_still_bumps(self, reload_env: Path) -> None:
        from nekro_agent.services.config_resolver import config_resolver

        before = config_resolver.current_version()

        ok, error = UnifiedConfigService.reload_config("system")

        assert ok, error
        assert config_resolver.current_version() > before

    def test_a_failed_reload_does_not_bump(self, reload_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """反向对照：读盘失败时配置没有变，版本号不该白白自增"""
        from nekro_agent.services.config_resolver import config_resolver

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise ValueError("yaml 坏了")

        monkeypatch.setattr(CoreConfig, "load_config", boom)
        before = config_resolver.current_version()

        with pytest.raises(ValueError):
            reload_config()

        assert config_resolver.current_version() == before
