"""插件的作用域可见性与实例级配置覆盖

## 与「进程级加载状态」的区别（重要）

`NekroPlugin.is_enabled` / `enable()` / `disable()` 表示的是**进程级加载状态**：
插件对象在本进程内是否活着——生命周期回调是否已触发、自定义路由是否已挂载。
本模块引入的是另一个正交概念——**作用域可见性**：某个实例 / 某个会话在本轮对话里
是否看得见这个插件，只影响提示词与沙盒方法的暴露范围。

两者的关系：

- 只要**任意一个**作用域还需要某插件，它的进程级状态就必须保持启用，否则
  「实例里单独启用一个全局关闭的插件」会拿到一个回调没跑过、路由没挂上的半成品。
  这一约束由 `is_enabled_in_any_scope()` 在插件加载与全局禁用时兜住。
- `resolve_active_plugins()` 的结果永远是「进程内活着的插件」的子集。

## 三层优先级

    会话(chat_key) > 实例(instance_key) > 全局(CoreConfig.PLUGIN_ENABLED)

每层只存**显式覆盖**项，未覆盖即回落上一层。单实例部署下 instance_key 与
chat_key 覆盖均为空，解析结果恒等于全局开关，行为与改造前完全一致。

## 落盘布局

- 启停覆盖：`{DATA_DIR}/plugin_data/_overrides/instances.yaml`
  与 `{DATA_DIR}/plugin_data/_overrides/sessions.yaml`，结构均为
  `{scope_key: {plugin_key: bool}}`。

  之所以按层级聚合成单文件而不是每个 scope_key 一个文件：chat_key 是各适配器
  自行拼出的不透明字符串，直接当文件名有路径穿越风险；聚合文件同时把「是否还有
  作用域需要这个插件」退化成一次内存查询。

- 实例级插件配置：`{DATA_DIR}/plugin_data/{plugin_key}/instances/{instance_key}/config.yaml`，
  只写入与全局配置**取值不同**的字段，其余字段永远跟随全局。
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Literal, Optional, Tuple, Type

import yaml
from pydantic import BaseModel

from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import OsEnv

if TYPE_CHECKING:
    from nekro_agent.adapters.onebot_v11.core.instance import InstanceContext
    from nekro_agent.services.plugin.base import NekroPlugin

logger = get_sub_logger("plugin_system")

PLUGIN_DATA_DIR: Path = Path(OsEnv.DATA_DIR) / "plugin_data"
# 插件键形如 `author.module_name`，两段都受 `_validate_name` 约束，永远不含点以外的分隔符，
# 因此以下划线开头的目录名不可能与任何插件的数据目录重名。
SCOPE_OVERRIDE_DIR: Path = PLUGIN_DATA_DIR / "_overrides"

# 用作路径片段的键必须先过这一关，避免适配器传入的标识穿越出数据目录
_SAFE_PATH_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

VisibilitySource = Literal["global", "instance", "session"]


class OverrideScope(str, Enum):
    """插件启停覆盖的作用域层级"""

    INSTANCE = "instance"
    SESSION = "session"


class PluginScopeState(BaseModel):
    """插件在某个作用域下的生效状态与来源层级"""

    plugin_key: str
    enabled: bool
    source: VisibilitySource
    global_enabled: bool
    instance_override: Optional[bool] = None
    session_override: Optional[bool] = None


# ============================================================
# 当前实例上下文
# ============================================================

# 实例上下文由适配器在入站时写入 contextvar，插件服务层不应在 import 期硬依赖某个适配器，
# 因此这里延迟探测一次并缓存函数引用（模块导入本身有 sys.modules 缓存，探测只做一次）。
_instance_context_getter: Optional[Callable[[], Optional["InstanceContext"]]] = None
_instance_context_probed: bool = False

# 不依赖任何适配器的实例上下文：沙盒回调、定时器等不经过适配器入站的链路由调用方显式设置
_scoped_instance_key: ContextVar[Optional[str]] = ContextVar("plugin_scope_instance_key", default=None)


@contextmanager
def instance_scope(instance_key: str) -> Iterator[None]:
    """在这段代码里把当前实例固定为 `instance_key`（空串表示单实例 / 无实例，读全局值）

    在这段代码里用 `asyncio.create_task` 拉起的任务会继承这个实例上下文（contextvar 在创建任务时拷贝）；
    全局常驻服务若在这里惰性启动，应自己包一层 `instance_scope("")`，避免长期停在某个实例上。
    """
    token = _scoped_instance_key.set(instance_key or "")
    try:
        yield
    finally:
        _scoped_instance_key.reset(token)


def current_instance_key() -> str:
    """解析当前处理链路所属的实例标识

    Returns:
        实例标识；都没有上下文时返回空串。

    优先返回 `instance_scope()` 显式设置的实例（空串也是显式设置，表示单实例 / 无实例）；
    其次才是适配器入站写入的上下文。都没有时**必须**优雅回落到全局值而不是抛异常：
    插件在任意链路里读配置都是正常用法，不能因为拿不到实例而失败。
    """
    global _instance_context_getter, _instance_context_probed  # noqa: PLW0603

    scoped = _scoped_instance_key.get()
    if scoped is not None:
        return scoped

    if not _instance_context_probed:
        _instance_context_probed = True
        try:
            from nekro_agent.adapters.onebot_v11.core.instance import get_current_instance
        except Exception:  # pragma: no cover - 适配器缺失时静默降级为单实例行为
            _instance_context_getter = None
        else:
            _instance_context_getter = get_current_instance

    if _instance_context_getter is None:
        return ""
    try:
        context = _instance_context_getter()
    except Exception:
        logger.debug("解析当前实例上下文失败，按无实例上下文处理")
        return ""
    return context.instance_key if context else ""


# ============================================================
# 启停覆盖存储
# ============================================================

_scope_override_cache: Dict[OverrideScope, Dict[str, Dict[str, bool]]] = {}


def _scope_override_path(scope: OverrideScope) -> Path:
    return SCOPE_OVERRIDE_DIR / ("instances.yaml" if scope is OverrideScope.INSTANCE else "sessions.yaml")


def _load_scope_overrides(scope: OverrideScope) -> Dict[str, Dict[str, bool]]:
    """读取某一层级的全部启停覆盖，进程内缓存，写入时失效"""
    cached = _scope_override_cache.get(scope)
    if cached is not None:
        return cached

    path = _scope_override_path(scope)
    raw: Any = {}
    if path.exists():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.exception(f"读取插件作用域覆盖失败，本次按无覆盖处理: {path}")
            raw = {}

    data: Dict[str, Dict[str, bool]] = {}
    if isinstance(raw, dict):
        for scope_key, entries in raw.items():
            if not isinstance(scope_key, str) or not isinstance(entries, dict):
                continue
            data[scope_key] = {
                plugin_key: value for plugin_key, value in entries.items() if isinstance(plugin_key, str) and isinstance(value, bool)
            }
    _scope_override_cache[scope] = data
    return data


def get_scope_overrides(scope: OverrideScope, scope_key: str) -> Dict[str, bool]:
    """获取指定作用域键下的显式启停覆盖，未设置任何覆盖时返回空字典"""
    if not scope_key:
        return {}
    return dict(_load_scope_overrides(scope).get(scope_key, {}))


def set_scope_override(scope: OverrideScope, scope_key: str, plugin_key: str, enabled: Optional[bool]) -> None:
    """写入或清除一条启停覆盖

    Args:
        scope: 覆盖层级
        scope_key: 实例标识或会话标识
        plugin_key: 插件键
        enabled: `True`/`False` 为显式覆盖，`None` 表示清除覆盖、回落上一层
    """
    if not scope_key:
        raise ValueError("作用域标识不能为空")

    data = _load_scope_overrides(scope)
    entries = data.setdefault(scope_key, {})
    if enabled is None:
        entries.pop(plugin_key, None)
    else:
        entries[plugin_key] = enabled
    if not entries:
        data.pop(scope_key, None)

    path = _scope_override_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    _scope_override_cache[scope] = data


def is_enabled_in_any_scope(plugin_key: str) -> bool:
    """是否存在任一作用域显式启用了该插件

    插件的进程级加载状态由「全局开关」与本函数共同决定：全局关掉但某个实例还要用时，
    插件必须继续活着，否则该实例拿到的是没跑过生命周期回调的半成品。
    """
    return any(
        entries.get(plugin_key) is True for scope in OverrideScope for entries in _load_scope_overrides(scope).values()
    )


# ============================================================
# 三层解析
# ============================================================


def resolve_plugin_scope_state(
    plugin_key: str,
    global_enabled: bool,
    instance_overrides: Dict[str, bool],
    session_overrides: Dict[str, bool],
) -> PluginScopeState:
    """按 会话 > 实例 > 全局 的优先级解析单个插件的生效状态与来源层级"""
    instance_override = instance_overrides.get(plugin_key)
    session_override = session_overrides.get(plugin_key)

    if session_override is not None:
        enabled, source = session_override, "session"
    elif instance_override is not None:
        enabled, source = instance_override, "instance"
    else:
        enabled, source = global_enabled, "global"

    return PluginScopeState(
        plugin_key=plugin_key,
        enabled=enabled,
        source=source,
        global_enabled=global_enabled,
        instance_override=instance_override,
        session_override=session_override,
    )


def is_plugin_active(plugin_key: str, instance_key: str = "", chat_key: str = "") -> bool:
    """某个作用域下该插件是否生效。只按配置判，不要求插件已加载，核心代码用它不必 import 插件"""
    from nekro_agent.core.config import config

    return resolve_plugin_scope_state(
        plugin_key,
        plugin_key in config.PLUGIN_ENABLED,
        get_scope_overrides(OverrideScope.INSTANCE, instance_key),
        get_scope_overrides(OverrideScope.SESSION, chat_key),
    ).enabled


def resolve_scope_states(instance_key: str = "", chat_key: str = "") -> List[PluginScopeState]:
    """解析全部已加载插件在给定作用域下的生效状态"""
    from nekro_agent.core.config import config
    from nekro_agent.services.plugin.collector import plugin_collector

    instance_overrides = get_scope_overrides(OverrideScope.INSTANCE, instance_key)
    session_overrides = get_scope_overrides(OverrideScope.SESSION, chat_key)
    return [
        resolve_plugin_scope_state(plugin.key, plugin.key in config.PLUGIN_ENABLED, instance_overrides, session_overrides)
        for plugin in plugin_collector.get_all_plugins()
    ]


async def resolve_active_plugins(instance_key: str = "", chat_key: str = "") -> List["NekroPlugin"]:
    """按 会话 > 实例 > 全局 的优先级解析当前生效的插件集合

    Args:
        instance_key: 适配器实例标识，单实例适配器传空串
        chat_key: 会话标识，无会话上下文时传空串

    Returns:
        当前作用域下生效的插件列表。

    结果必然是「进程内活着的插件」的子集：进程级未加载的插件即便被某作用域显式启用
    也不会返回，避免把没跑过生命周期回调的插件暴露给 AI。
    """
    from nekro_agent.core.config import config
    from nekro_agent.services.plugin.collector import plugin_collector

    instance_overrides = get_scope_overrides(OverrideScope.INSTANCE, instance_key)
    session_overrides = get_scope_overrides(OverrideScope.SESSION, chat_key)

    active: List["NekroPlugin"] = []
    for plugin in plugin_collector.get_all_plugins():
        if not plugin.is_enabled:
            continue
        state = resolve_plugin_scope_state(
            plugin.key,
            plugin.key in config.PLUGIN_ENABLED,
            instance_overrides,
            session_overrides,
        )
        if state.enabled:
            active.append(plugin)
    return active


# ============================================================
# 实例级插件配置
# ============================================================

# 已解析的实例级配置缓存；值为 None 表示该实例无有效覆盖，直接用全局实例即可
_scoped_config_cache: Dict[Tuple[str, str], Optional[ConfigBase]] = {}


def instance_config_path(plugin_key: str, instance_key: str) -> Path:
    """实例级插件配置文件路径

    Raises:
        ValueError: 插件键或实例标识含有不能安全用作路径片段的字符
    """
    if not _SAFE_PATH_KEY_PATTERN.match(plugin_key):
        raise ValueError(f"非法的插件键: {plugin_key}")
    if not _SAFE_PATH_KEY_PATTERN.match(instance_key):
        raise ValueError(f"非法的实例标识: {instance_key}")
    return PLUGIN_DATA_DIR / plugin_key / "instances" / instance_key / "config.yaml"


def load_instance_config_overrides(plugin_key: str, instance_key: str) -> Dict[str, Any]:
    """读取实例级配置覆盖，只含被显式覆盖的字段"""
    try:
        path = instance_config_path(plugin_key, instance_key)
    except ValueError:
        logger.warning(f"跳过实例级插件配置读取: {plugin_key} / {instance_key} 标识不合法")
        return {}
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.exception(f"读取实例级插件配置失败，本次按无覆盖处理: {path}")
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str)}


def save_instance_config_overrides(plugin_key: str, instance_key: str, overrides: Dict[str, Any]) -> None:
    """写入实例级配置覆盖并使解析缓存失效

    只应传入与全局配置取值不同的字段；覆盖为空时直接删除文件，让该实例完全跟随全局。
    """
    path = instance_config_path(plugin_key, instance_key)
    if overrides:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.dump(overrides, default_flow_style=False, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    elif path.exists():
        path.unlink()
    invalidate_scoped_config(plugin_key, instance_key)


def invalidate_scoped_config(plugin_key: str, instance_key: Optional[str] = None) -> None:
    """使实例级配置解析缓存失效

    Args:
        plugin_key: 插件键
        instance_key: 只失效指定实例；为 None 时失效该插件的全部实例
            （全局配置变更会改变合并结果，必须整体失效）
    """
    if instance_key is not None:
        _scoped_config_cache.pop((plugin_key, instance_key), None)
        return
    for cache_key in [key for key in _scoped_config_cache if key[0] == plugin_key]:
        _scoped_config_cache.pop(cache_key, None)


def resolve_scoped_config(
    plugin_key: str,
    config_cls: Type[ConfigBase],
    global_config: ConfigBase,
    instance_key: str,
) -> ConfigBase:
    """解析某实例下生效的插件配置实例

    无覆盖（或覆盖非法）时直接返回传入的全局实例，调用方无需区分两种情形。
    合并结果按 `(plugin_key, instance_key)` 缓存，全局配置写入或实例覆盖写入时失效。
    """
    cache_key = (plugin_key, instance_key)
    if cache_key in _scoped_config_cache:
        cached = _scoped_config_cache[cache_key]
        return cached if cached is not None else global_config

    overrides = load_instance_config_overrides(plugin_key, instance_key)
    merged: Optional[ConfigBase] = None
    if overrides:
        try:
            merged = config_cls.model_validate({**global_config.model_dump(), **overrides})
            # 指向实例文件，避免误调用 dump_config() 时把实例值写回全局配置
            merged.set_instance_config_file_path(instance_config_path(plugin_key, instance_key))
        except Exception:
            logger.exception(f"实例级插件配置校验失败，回落全局配置: {plugin_key} / {instance_key}")
            merged = None

    _scoped_config_cache[cache_key] = merged
    return merged if merged is not None else global_config
