
"""插件管理服务

提供插件的管理相关功能，如获取插件列表、配置管理等。
"""

from typing import Any, Dict, List, Optional, Tuple

from nekro_agent.core.config import CONFIG_PATH, config
from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.config_service import ConfigService
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.isolation import PluginQuarantine, get_quarantine
from nekro_agent.services.plugin.prompt_activation import (
    PluginActivationStrategy,
    get_plugin_activation_strategy,
    is_sleep_effective,
    plugin_strategy_can_change,
    plugin_strategy_is_protected,
    plugin_supports_sleep,
    set_plugin_activation_strategy,
)
from nekro_agent.services.plugin.scope import (
    OverrideScope,
    PluginScopeState,
    is_enabled_in_any_scope,
    load_instance_config_overrides,
    resolve_scope_states,
    resolve_scoped_config,
    save_instance_config_overrides,
    set_scope_override,
)

logger = get_sub_logger("plugin_system")


def _get_activation_controller_meta() -> dict:
    controller = plugin_collector.get_plugin_by_module_name("plugin_activation")
    return {
        "enabled": bool(controller and controller.is_enabled),
        "pluginId": controller.key if controller else "KroMiose.plugin_activation",
        "moduleName": "plugin_activation",
    }


def _build_activation_strategy_meta(plugin) -> dict:
    configured = get_plugin_activation_strategy(plugin.module_name)
    return {
        "configured": configured,
        "effective": "sleep" if is_sleep_effective(plugin) else "always_loaded",
        "pluginDefaultAllowsSleep": plugin.allow_sleep,
        "canEnableSleep": plugin_supports_sleep(plugin) and not plugin_strategy_is_protected(plugin),
        "canChangeStrategy": plugin_strategy_can_change(plugin),
        "isProtected": plugin_strategy_is_protected(plugin),
        "sleepBrief": plugin.sleep_brief,
        "controller": _get_activation_controller_meta(),
    }


def build_quarantine_meta(quarantine: Optional[PluginQuarantine]) -> Optional[dict]:
    """自动隔离状态的对外形状

    隔离必须是用户看得见的事件，否则表现出来就是"某个功能悄悄消失了"。
    这里把它挂到插件列表/详情上，前端在插件卡片上直接标出来。
    """
    if quarantine is None:
        return None
    return {
        "stage": quarantine.stage,
        "reason": quarantine.reason,
        "detail": quarantine.detail,
        "failures": quarantine.failures,
        "since": quarantine.since,
        "until": quarantine.until,
    }


async def _notify_commands_changed_after_plugin_toggle(plugin_key: str, plugin_name: str) -> None:
    """插件启停后同步命令清单，失败只记录日志。

    plugin_key 是命令 source 字段所用的插件标识符，plugin_name 是供人阅读的显示名称。
    两者均记录在日志中，便于排查时关联具体命令来源。
    """
    from nekro_agent.services.command.manager import command_manager

    try:
        await command_manager.notify_commands_changed()
    except Exception as e:
        logger.exception(f"插件 {plugin_name} (key={plugin_key}) 状态已更新，但命令同步失败: {e}")


async def get_all_ext_meta_data() -> List[dict]:
    """获取所有已注册插件的元数据（包括加载成功和失败的插件）"""
    plugins = plugin_collector.get_all_plugins()
    failed_plugins = plugin_collector.get_all_failed_plugins()

    # 成功加载的插件
    plugin_list = [
        {
            "id": plugin.key,
            "name": plugin.name,
            "description": plugin.description,
            "version": plugin.version,
            "author": plugin.author,
            "enabled": plugin.is_enabled,
            "hasConfig": hasattr(plugin, "_Configs") and plugin._Configs != ConfigBase,  # noqa: SLF001
            "url": plugin.url or "",
            "isBuiltin": plugin.is_builtin,
            "isPackage": plugin.is_package,
            "i18n_name": plugin.i18n_name,
            "i18n_description": plugin.i18n_description,
            "activationStrategy": _build_activation_strategy_meta(plugin),
            "loadFailed": False,  # 标记加载状态
            "quarantine": build_quarantine_meta(get_quarantine(plugin.key)),  # 非 None 表示已被自动隔离
        }
        for plugin in plugins
    ]

    # 加载失败的插件
    for failed_plugin in failed_plugins:
        plugin_list.append(
            {
                "id": failed_plugin.module_name,
                "name": failed_plugin.module_name,
                "description": failed_plugin.error_message,  # 只显示错误信息，不添加前缀
                "version": "N/A",
                "author": "N/A",
                "enabled": False,
                "hasConfig": False,
                "url": "N/A",
                "isBuiltin": failed_plugin.is_builtin,
                "isPackage": failed_plugin.is_package,
                "i18n_name": None,
                "i18n_description": None,
                "activationStrategy": None,
                "quarantine": None,  # 加载失败走 loadFailed，与自动隔离是两回事
                "loadFailed": True,  # 标记加载失败，前端根据此字段隐藏开关并显示"加载失败"
                "errorMessage": failed_plugin.error_message,  # 完整错误信息
                "errorType": failed_plugin.error_type,  # 错误类型
                "filePath": failed_plugin.file_path,  # 文件路径
                "stackTrace": failed_plugin.stack_trace,  # 堆栈信息
            },
        )

    return plugin_list


async def get_plugin_detail(plugin_id: str) -> Optional[dict]:
    """获取指定插件的详细信息"""
    plugin = plugin_collector.get_plugin(plugin_id)

    # 如果插件不存在，检查是否在失败列表中
    if not plugin:
        # 提取模块名称（用于查询失败的插件）
        module_name = plugin_id.split(".")[-1]
        failed_plugin = plugin_collector.get_failed_plugin_by_module_name(module_name)
        if failed_plugin:
            return {
                "name": failed_plugin.module_name,
                "moduleName": failed_plugin.module_name,
                "id": failed_plugin.module_name,
                "version": "N/A",
                "description": failed_plugin.error_message,  # 只显示错误信息，不添加前缀
                "author": "N/A",
                "url": "N/A",
                "enabled": False,
                "hasConfig": False,
                "methods": [],
                "webhooks": [],
                "router": None,
                "isBuiltin": failed_plugin.is_builtin,
                "isPackage": failed_plugin.is_package,
                "activationStrategy": None,
                "quarantine": None,  # 加载失败走 loadFailed，与自动隔离是两回事
                "loadFailed": True,  # 前端根据此字段隐藏开关并显示"加载失败"
                "errorMessage": failed_plugin.error_message,  # 完整错误信息
                "errorType": failed_plugin.error_type,  # 错误类型
                "filePath": failed_plugin.file_path,  # 文件路径
                "stackTrace": failed_plugin.stack_trace,  # 堆栈信息
            }
        return None

    # 获取方法信息
    methods = [
        {
            "name": method.func.__name__,
            "title": method.name,
            "type": method.method_type.value,
            "description": method.description or "",
        }
        for method in plugin.sandbox_methods
    ]

    # 获取 webhook 信息
    webhooks = [
        {
            "endpoint": endpoint,
            "name": method.name,
            "description": method.description or "",
        }
        for endpoint, method in plugin.webhook_methods.items()
    ]

    # 获取路由信息
    router_info = None
    if plugin.get_plugin_router():
        router_data = plugin_collector.get_plugin_router_info().get(plugin_id)
        if router_data:
            router_info = {
                "mount_path": router_data["mount_path"],
                "routes_count": router_data["routes_count"],
                "routes": router_data["routes"],
            }

    # 构建插件详情
    return {
        "name": plugin.name,
        "moduleName": plugin.module_name,
        "id": plugin.key,
        "version": plugin.version,
        "description": plugin.description,
        "author": plugin.author,
        "url": plugin.url or "",
        "enabled": plugin.is_enabled,
        "hasConfig": hasattr(plugin, "_Configs") and plugin._Configs != ConfigBase,  # noqa: SLF001
        "methods": methods,
        "webhooks": webhooks,
        "router": router_info,  # 新增路由信息
        "isBuiltin": plugin.is_builtin,
        "isPackage": plugin.is_package,
        "quarantine": build_quarantine_meta(get_quarantine(plugin.key)),
        "activationStrategy": _build_activation_strategy_meta(plugin),
        "loadFailed": False,
    }


async def update_plugin_activation_strategy(
    plugin_id: str,
    strategy: PluginActivationStrategy,
) -> tuple[bool, Optional[str]]:
    plugin = plugin_collector.get_plugin(plugin_id)
    if not plugin:
        return False, f"插件 {plugin_id} 不存在"

    if plugin_strategy_is_protected(plugin):
        if strategy == "auto":
            set_plugin_activation_strategy(plugin.module_name, "auto")
            return True, None
        return False, f"插件 {plugin.module_name} 的激活策略受保护，不允许修改"

    if strategy == "allow_sleep" and not plugin_supports_sleep(plugin):
        return False, f"插件 {plugin.module_name} 未提供休眠提示词，无法开启休眠"

    set_plugin_activation_strategy(plugin.module_name, strategy)
    return True, None


async def get_all_plugin_router_info() -> Dict[str, Any]:
    """获取所有插件的路由信息

    Returns:
        Dict[str, Any]: 插件路由信息汇总
    """
    try:
        from nekro_agent.services.plugin.router_manager import plugin_router_manager

        # 使用新的路由管理器获取信息
        return plugin_router_manager.get_plugins_router_info()

    except Exception as e:
        logger.error(f"获取插件路由信息失败: {e}")
        return {
            "total_plugins": 0,
            "plugins_with_router": 0,
            "router_summary": [],
            "detailed_routes": {},
            "error": str(e),
        }


async def enable_plugin(plugin_id: str) -> bool:
    """全局启用插件（支持热重载）"""
    plugin = plugin_collector.get_plugin(plugin_id)
    if not plugin:
        return False

    if plugin.is_enabled and plugin.key in config.PLUGIN_ENABLED:
        return True  # 已经启用，直接返回成功

    try:
        # 启用插件 - enable() 方法内部会自动触发回调
        await plugin.enable()

        if plugin.key not in config.PLUGIN_ENABLED:
            config.PLUGIN_ENABLED.append(plugin.key)
            ConfigService.save_config(config, CONFIG_PATH)

        # 热挂载插件路由
        try:
            from nekro_agent.services.plugin.router_manager import plugin_router_manager

            if plugin_router_manager.mount_plugin_router(plugin):
                logger.info(f"插件 {plugin.name} 启用并热挂载路由成功")
            else:
                logger.debug(f"插件 {plugin.name} 启用成功，但没有路由需要挂载")
        except Exception as router_error:
            logger.exception(f"插件 {plugin.name} 启用成功，但路由挂载失败: {router_error}")
            # 路由挂载失败不影响插件启用

        await _notify_commands_changed_after_plugin_toggle(plugin.key, plugin.name)

    except Exception as e:
        logger.error(f"启用插件失败: {plugin_id}, 错误: {e}")
        return False
    else:
        return True


async def disable_plugin(plugin_id: str) -> bool:
    """全局禁用插件（支持热重载）

    若仍有实例 / 会话显式启用了它，插件的**进程级加载状态**会被保留（路由不卸载、
    不触发 disabled 回调），只是全局开关变为关闭，未做覆盖的作用域从此看不见它。
    """
    plugin = plugin_collector.get_plugin(plugin_id)
    if not plugin:
        return False

    if not plugin.is_enabled and plugin.key not in config.PLUGIN_ENABLED:
        return True  # 已经禁用，直接返回成功

    try:
        if not is_enabled_in_any_scope(plugin.key):
            # 热卸载插件路由
            try:
                from nekro_agent.services.plugin.router_manager import plugin_router_manager

                if plugin_router_manager.unmount_plugin_router(plugin.key):
                    logger.info(f"插件 {plugin.name} 路由已热卸载")
                else:
                    logger.debug(f"插件 {plugin.name} 没有路由需要卸载")
            except Exception as router_error:
                logger.exception(f"插件 {plugin.name} 路由卸载失败: {router_error}")
                # 路由卸载失败不影响插件禁用

            # 禁用插件 - disable() 方法内部会自动触发回调
            await plugin.disable()
        else:
            logger.info(f"插件 {plugin.name} 已全局关闭，但仍有作用域显式启用，进程内保持加载")

        if plugin.key in config.PLUGIN_ENABLED:
            config.PLUGIN_ENABLED.remove(plugin.key)
            ConfigService.save_config(config, CONFIG_PATH)

        await _notify_commands_changed_after_plugin_toggle(plugin.key, plugin.name)

    except Exception as e:
        logger.error(f"禁用插件失败: {plugin_id}, 错误: {e}")
        return False
    else:
        return True


async def get_plugin_config(plugin_id: str) -> Optional[List[Dict[str, Any]]]:
    """获取插件全局配置列表"""
    plugin = plugin_collector.get_plugin(plugin_id)
    if not plugin or plugin.config_class == ConfigBase:
        return None

    try:
        # WebUI 编辑的是全局配置，必须拿真实实例而不是按实例上下文解析的代理
        plugin_config = plugin.get_global_config()
        if not plugin_config:
            return None

        # 使用配置服务获取配置列表
        return ConfigService.get_config_list(plugin_config)
    except Exception as e:
        logger.error(f"获取插件配置失败: {plugin_id}, 错误: {e}")
        return None


async def save_plugin_config(plugin_id: str, configs: Dict[str, str]) -> Tuple[bool, Optional[str]]:
    """保存插件全局配置

    Args:
        plugin_id: 插件ID
        configs: 配置项字典，键为配置项名称，值为字符串形式的配置值

    Returns:
        (成功状态, 错误信息)
    """
    plugin = plugin_collector.get_plugin(plugin_id)
    if not plugin or plugin.config_class == ConfigBase:
        return False, f"插件 {plugin_id} 不存在或无配置"

    try:
        plugin_config = plugin.get_global_config()
        if not plugin_config:
            return False, f"插件 {plugin_id} 配置获取失败"

        # 使用配置服务批量更新配置
        success, error_msg = ConfigService.batch_update_config(plugin_config, configs)
        if not success:
            return False, error_msg

        # 保存配置（save_config 内部会让实例级合并结果失效）
        plugin.save_config(plugin_config)

    except Exception as e:
        logger.error(f"保存插件配置失败: {plugin_id}, 错误: {e}")
        return False, f"保存失败: {e!s}"
    else:
        return True, None


async def get_plugin_scope_states(instance_key: str, chat_key: str = "") -> List[PluginScopeState]:
    """获取各插件在指定作用域下的生效状态与来源层级

    Args:
        instance_key: 适配器实例标识
        chat_key: 会话标识，传空表示只看到实例层为止
    """
    return resolve_scope_states(instance_key=instance_key, chat_key=chat_key)


async def set_plugin_scope_override(
    scope: OverrideScope,
    scope_key: str,
    plugin_id: str,
    enabled: Optional[bool],
) -> Tuple[bool, Optional[str]]:
    """设置或清除一条插件启停的作用域覆盖

    Args:
        scope: 覆盖层级（实例 / 会话）
        scope_key: 实例标识或会话标识
        plugin_id: 插件键
        enabled: `True`/`False` 为显式覆盖，`None` 表示清除覆盖、回落上一层

    覆盖写入后会同步插件的**进程级加载状态**：某作用域把一个全局关闭的插件打开时，
    必须把它在进程内拉起来（触发 enabled 回调、挂载路由），否则该作用域拿到的是半成品；
    反之当最后一个显式启用被撤销且全局也是关闭时，再把它下线。
    """
    plugin = plugin_collector.get_plugin(plugin_id)
    if not plugin:
        return False, f"插件 {plugin_id} 不存在"

    try:
        set_scope_override(scope, scope_key, plugin_id, enabled)
    except ValueError as e:
        return False, str(e)

    should_be_loaded = plugin.key in config.PLUGIN_ENABLED or is_enabled_in_any_scope(plugin.key)
    if should_be_loaded == plugin.is_enabled:
        return True, None

    from nekro_agent.services.plugin.router_manager import plugin_router_manager

    try:
        if should_be_loaded:
            await plugin.enable()
            plugin_router_manager.mount_plugin_router(plugin)
        else:
            plugin_router_manager.unmount_plugin_router(plugin.key)
            await plugin.disable()
        await _notify_commands_changed_after_plugin_toggle(plugin.key, plugin.name)
    except Exception as e:
        logger.exception(f"同步插件 {plugin.name} 进程级加载状态失败: {e}")
        return False, f"覆盖已写入，但插件加载状态同步失败: {e!s}"

    return True, None


async def get_plugin_instance_config(plugin_id: str, instance_key: str) -> Optional[List[Dict[str, Any]]]:
    """获取插件在指定实例下**生效**的配置列表

    未被覆盖的字段返回的是全局值，配合 `overridden` 标记即可在前端区分「跟随全局」与
    「实例已覆盖」两种状态。
    """
    plugin = plugin_collector.get_plugin(plugin_id)
    if not plugin or plugin.config_class == ConfigBase:
        return None

    try:
        global_config = plugin.get_global_config()
        effective = resolve_scoped_config(plugin.key, plugin.config_class, global_config, instance_key)
        overrides = load_instance_config_overrides(plugin.key, instance_key)
        config_list = ConfigService.get_config_list(effective)
    except Exception as e:
        logger.error(f"获取实例级插件配置失败: {plugin_id} / {instance_key}, 错误: {e}")
        return None

    for item in config_list:
        item["overridden"] = item["key"] in overrides
    return config_list


async def save_plugin_instance_config(
    plugin_id: str,
    instance_key: str,
    configs: Dict[str, str],
) -> Tuple[bool, Optional[str]]:
    """写入插件的实例级配置覆盖

    只有与全局取值**不同**的字段才会落盘，取值被改回与全局一致即视为撤销覆盖、
    此后继续跟随全局变化。

    Args:
        plugin_id: 插件键
        instance_key: 适配器实例标识
        configs: 配置项字典，键为配置项名称，值为字符串形式的配置值

    Returns:
        (成功状态, 错误信息)
    """
    plugin = plugin_collector.get_plugin(plugin_id)
    if not plugin or plugin.config_class == ConfigBase:
        return False, f"插件 {plugin_id} 不存在或无配置"

    try:
        global_config = plugin.get_global_config()
        # 在「全局值 + 已有覆盖」的副本上套用本次改动，复用配置服务的字符串解析与校验
        draft = plugin.config_class.model_validate(
            {**global_config.model_dump(), **load_instance_config_overrides(plugin.key, instance_key)},
        )
        success, error_msg = ConfigService.batch_update_config(draft, configs)
        if not success:
            return False, error_msg

        global_values = global_config.model_dump()
        overrides = {key: value for key, value in draft.model_dump().items() if value != global_values.get(key)}
        save_instance_config_overrides(plugin.key, instance_key, overrides)
    except ValueError as e:
        return False, str(e)
    except Exception as e:
        logger.error(f"保存实例级插件配置失败: {plugin_id} / {instance_key}, 错误: {e}")
        return False, f"保存失败: {e!s}"
    else:
        return True, None
