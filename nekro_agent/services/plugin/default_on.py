"""新内置插件的「默认启用」

## 为什么不能只改 `PLUGIN_ENABLED` 的默认值

全局启停存在 `CoreConfig.PLUGIN_ENABLED` 里，而配置文件是整份落盘的：已经跑起来的部署，
文件里早就写着自己的那份列表，改字段默认值只对全新安装有效。所以要「默认启用」的新内置插件，
在启动时由这里加进 `PLUGIN_ENABLED`，**只加一次**。

## 为什么要记「已经加过」

加过的插件记进 `PLUGIN_DEFAULT_ON_APPLIED`。平台方之后在插件管理里把它关掉，它就从
`PLUGIN_ENABLED` 里移除了，但仍在已应用的记录里，下次启动不会被又打开——默认值只决定
「第一次见到时的状态」，不能盖过平台方的选择。租户在自己实例上关掉是实例级覆盖，与这里无关。

## 这里只改内存，从不自己落盘

调用点在 `nekro_agent/__init__.py` 的启动流程里、`bootstrap_model_groups()` **之前**（与
`reconcile_sandbox_chat_api_url` 同一个做法）：改动由 `bootstrap_model_groups()` 那次常规落盘
一起写进配置文件。

不自己落盘是有意的：配置落盘会把模型组 `API_KEY` 掩码成占位符，而 `bootstrap_model_groups()`
在两种情形下会**故意不落盘**，保住 YAML 里可能是唯一一份的明文 Key——库里有当前密钥解不开的行；
或 YAML 里手填的 Key 与库里不一致、而手填值的备份写失败了（后一种的明文不在落盘钩子的原样写回范围里）。
在它之后再落一次盘，就把这层保护绕过去了。

它不落盘的那次启动，这里的改动只活在内存里：本次运行插件照样按默认启用；下次启动重新加载配置后
再加一次（幂等）。平台方在这期间关掉插件、或保存任何别的配置，都会触发正常落盘，「已经加过」的记录
随之写进文件。
"""

from typing import Any, List, Tuple

from nekro_agent.core.logger import get_sub_logger

logger = get_sub_logger("plugin_system")

# 要「默认启用」的内置插件键（`作者.module_name`）。只往里加新插件；从这里删掉一项不会关掉已启用的插件
DEFAULT_ON_BUILTIN_PLUGINS: Tuple[str, ...] = ("Akiyo_dayo.message_recall", "Akiyo_dayo.affection_tools")


def apply_default_on_plugins(cfg: Any, plugin_keys: Tuple[str, ...] = DEFAULT_ON_BUILTIN_PLUGINS) -> List[str]:
    """把还没应用过的默认启用插件加进 `PLUGIN_ENABLED`（只改内存）

    Args:
        cfg: 全局配置对象（读写 `PLUGIN_ENABLED` 与 `PLUGIN_DEFAULT_ON_APPLIED` 两个列表）
        plugin_keys: 要默认启用的插件键

    Returns:
        这次新加进 `PLUGIN_ENABLED` 的插件键
    """
    newly_enabled: List[str] = []
    for key in plugin_keys:
        if key in cfg.PLUGIN_DEFAULT_ON_APPLIED:
            continue
        cfg.PLUGIN_DEFAULT_ON_APPLIED.append(key)
        if key not in cfg.PLUGIN_ENABLED:
            cfg.PLUGIN_ENABLED.append(key)
            newly_enabled.append(key)
    return newly_enabled


def apply_default_on_plugins_at_startup() -> None:
    """启动时调用：对全局配置应用默认启用（只改内存）；任何失败都只记日志，不影响启动"""
    from nekro_agent.core.config import config

    try:
        newly_enabled = apply_default_on_plugins(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"应用插件默认启用失败（已忽略）: {exc}")
        return
    if newly_enabled:
        logger.info(f"新内置插件按默认启用: {', '.join(newly_enabled)}（随启动时的配置落盘写入配置文件）")
