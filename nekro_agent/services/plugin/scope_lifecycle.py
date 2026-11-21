"""插件按实例 / 按频道设置的生命周期

插件有三样东西按实例或频道存（落盘布局见 `scope.py`）：

- 实例级启停：`plugin_data/_overrides/instances.yaml`，键是 `instance_key`；
- 频道级启停：`plugin_data/_overrides/sessions.yaml`，键是 `chat_key`；
- 实例级配置：`plugin_data/<插件>/instances/<instance_key>/`（插件的 API Key 之类就在这里）。

它们都不在数据库里，彻底删除实例、划转、换会话标识、实例迁移这几条路径上以前一样都没管：
`instance_key` 是可以被重新认领的 slug，同名的新实例（可能属于另一个租户）一建出来就直接
读到上一任主人填的插件配置。处理口径与 SSH 凭据、实例自有邮箱一致：

| 操作 | 实例级启停 | 频道级启停 | 实例级配置目录 |
| --- | --- | --- | --- |
| 彻底删除实例 | 删 | 删（这个实例的频道） | 删 |
| 划转给别的租户（默认） | 删 | 删（这个实例的频道） | 删 |
| 划转时取消勾选 | 跟着实例走 | 跟着实例走 | 跟着实例走 |
| 换会话标识 | — | 键改成新标识 | — |
| 实例迁移（源 → 目标） | 键改成目标 | 随频道改；没有频道行的按前缀改 | 目录改名 |

「这个实例的频道」= 频道表里 `(adapter_key, instance_key)` 名下的 `chat_key`，加上以
`{adapter_key}-{instance_key}-` 开头的键（频道行已经不在、开关还留着的那些）。这个前缀与
`purge_instance` 删执行记录、插件数据用的是同一个。

实例级的两样只按 `instance_key` 存：存量里跨适配器同名的实例共用同一份（启动自检会告警，
见 `instance_manager.warn_cross_adapter_instance_key_collisions`），删 / 搬时那一份照样一起删 / 搬。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services.plugin import scope as scope_store
from nekro_agent.services.plugin.scope import OverrideScope

logger = get_sub_logger("plugin_system")


def instance_chat_key_prefix(adapter_key: str, instance_key: str) -> str:
    """这个实例的频道标识前缀；单实例（实例标识为空）没有前缀，只认频道表里的行"""
    if not adapter_key or not instance_key:
        return ""
    return f"{adapter_key}-{instance_key}-"


@dataclass(frozen=True)
class InstancePluginSettings:
    """一个实例名下有哪些插件设置"""

    instance_toggle_plugins: Tuple[str, ...]
    session_keys: Tuple[str, ...]
    session_toggle_plugins: Tuple[str, ...]
    config_plugins: Tuple[str, ...]

    @property
    def plugins(self) -> Tuple[str, ...]:
        """涉及的插件（三样的并集）"""
        return tuple(
            sorted({*self.instance_toggle_plugins, *self.session_toggle_plugins, *self.config_plugins}),
        )


async def instance_chat_keys(adapter_key: str, instance_key: str) -> List[str]:
    """频道表里这个实例名下的会话标识"""
    from nekro_agent.models.db_chat_channel import DBChatChannel

    rows = await DBChatChannel.filter(adapter_key=adapter_key, instance_key=instance_key).values_list(
        "chat_key",
        flat=True,
    )
    return sorted({str(chat_key) for chat_key in rows if chat_key})


def _prefix_keys_excluding(prefix_matched: List[str], explicit: Iterable[str], excluded: Iterable[str]) -> List[str]:
    """前缀命中的键里去掉属于别的实例的那些；调用方显式点名的键（这个实例自己的频道）不受影响"""
    keep = set(explicit)
    drop = set(excluded) - keep
    return [key for key in prefix_matched if key not in drop]


def describe_instance_plugin_settings(
    adapter_key: str,
    instance_key: str,
    chat_keys: List[str],
    *,
    exclude_chat_keys: Iterable[str] = (),
) -> InstancePluginSettings:
    """这个实例名下的插件设置（只读）；`chat_keys` 由调用方先查好（彻底删除要在删频道行之前取）

    `exclude_chat_keys`：带实例前缀、但在频道表里属于**别的**实例的会话标识。前缀只对把实例段编进
    会话标识的适配器成立；频道标识由客户端给的适配器上，一个活着的频道完全可能恰好以
    `<适配器>-<这个实例名>-` 开头，它的开关不是这个实例的。
    """
    session_keys = _prefix_keys_excluding(
        scope_store.scope_keys_matching(
            OverrideScope.SESSION,
            chat_keys,
            [instance_chat_key_prefix(adapter_key, instance_key)],
        ),
        chat_keys,
        exclude_chat_keys,
    )
    return InstancePluginSettings(
        instance_toggle_plugins=tuple(sorted(scope_store.get_scope_overrides(OverrideScope.INSTANCE, instance_key))),
        session_keys=tuple(session_keys),
        session_toggle_plugins=tuple(sorted(scope_store.plugins_with_overrides(OverrideScope.SESSION, session_keys))),
        config_plugins=tuple(plugin_key for plugin_key, _ in scope_store.instance_config_dirs(instance_key)),
    )


async def count_instance_plugin_settings(adapter_key: str, instance_key: str) -> int:
    """预览用：这个实例名下有设置的插件个数（实例级启停、频道级启停、实例级配置的并集）"""
    chat_keys = await instance_chat_keys(adapter_key, instance_key)
    return len(describe_instance_plugin_settings(adapter_key, instance_key, chat_keys).plugins)


def drop_instance_plugin_settings(
    adapter_key: str,
    instance_key: str,
    chat_keys: List[str],
    *,
    exclude_chat_keys: Iterable[str] = (),
) -> Dict[str, int]:
    """删掉这个实例名下的全部插件设置，返回各自删掉的数量

    出错直接抛出：彻底删除与划转都在改库之前调它，删不掉就让整次操作失败、库里什么都不改
    （留着这份设置再把实例名放出去，正是这里要堵的事）。配置目录先删：它最敏感、也最可能删不动
    （文件被占用），删不动时两份启停文件还是原样。`exclude_chat_keys` 见 `describe_instance_plugin_settings`。
    """
    settings = describe_instance_plugin_settings(
        adapter_key,
        instance_key,
        chat_keys,
        exclude_chat_keys=exclude_chat_keys,
    )
    config_count = len(scope_store.drop_instance_config_dirs(instance_key))
    counts = {
        "plugin_instance_toggles": (
            scope_store.drop_scope_keys(OverrideScope.INSTANCE, [instance_key]) if instance_key else 0
        ),
        "plugin_session_toggles": scope_store.drop_scope_keys(OverrideScope.SESSION, settings.session_keys),
        "plugin_instance_configs": config_count,
    }
    if any(counts.values()):
        logger.warning(
            f"删除实例的插件设置: adapter_key={adapter_key}, instance_key={instance_key}, "
            f"plugins={list(settings.plugins)}, counts={counts}",
        )
    return counts


def plugins_on_session_key(chat_key: str) -> Tuple[str, ...]:
    """某个会话标识上有频道级开关的插件"""
    return tuple(sorted(scope_store.get_scope_overrides(OverrideScope.SESSION, chat_key)))


def drop_session_plugin_settings(chat_key: str) -> int:
    """删掉一个会话标识上的频道级开关，返回删掉的条目数"""
    return scope_store.drop_scope_keys(OverrideScope.SESSION, [chat_key])


def move_session_plugin_settings(old_chat_key: str, new_chat_key: str) -> int:
    """换会话标识：频道级启停跟到新标识上，返回搬过去的条目数（新标识下已有的条目保留）"""
    return scope_store.move_scope_key(OverrideScope.SESSION, old_chat_key, new_chat_key)


def instance_level_plugins(instance_key: str) -> List[str]:
    """实例级启停与实例级配置涉及的插件（实例迁移的预览与残留统计用）"""
    if not instance_key:
        return []
    toggles = set(scope_store.get_scope_overrides(OverrideScope.INSTANCE, instance_key))
    configs = {plugin_key for plugin_key, _ in scope_store.instance_config_dirs(instance_key)}
    return sorted(toggles | configs)


def move_instance_plugin_settings(
    adapter_key: str,
    source_instance_key: str,
    target_instance_key: str,
    *,
    exclude_chat_keys: Iterable[str] = (),
    instance_level: bool = True,
) -> Dict[str, int]:
    """实例迁移：实例级启停改到目标实例、实例级配置目录改名，没有频道行的频道级启停按前缀改

    有频道行的频道级启停不在这里改：逐个频道迁移时 `migrate_chat_key_references` 已经把它们
    跟着新标识搬过去了。这里只接住频道行早已不在、键还带着源实例前缀的那些，按
    `build_instance_chat_key` 同一套格式把实例段换掉。

    - `exclude_chat_keys`：此刻**还在源实例名下**的频道（迁移失败留下的）。它们的键同样带着源实例
      前缀，但不是孤儿：搬走的话，留在源实例的频道就丢了自己的开关。
    - `instance_level=False`：有频道迁移失败时由调用方传入。源实例名下还有频道，实例级的设置
      还在给它们用，留在源实例上，重跑补齐那一次再搬。

    返回 `plugin_settings`（迁移前源实例名下有实例级设置的插件数；`instance_level=False` 时为 0）与
    `plugin_session_toggles_orphan`（按前缀改的条目数）。
    """
    empty = {"plugin_settings": 0, "plugin_session_toggles_orphan": 0}
    if not source_instance_key or not target_instance_key or source_instance_key == target_instance_key:
        return empty
    plugins = instance_level_plugins(source_instance_key) if instance_level else []
    if instance_level:
        scope_store.move_scope_key(OverrideScope.INSTANCE, source_instance_key, target_instance_key)
        scope_store.move_instance_config_dirs(source_instance_key, target_instance_key)

    source_prefix = instance_chat_key_prefix(adapter_key, source_instance_key)
    target_prefix = instance_chat_key_prefix(adapter_key, target_instance_key)
    still_at_source = set(exclude_chat_keys)
    orphan_moved = 0
    for chat_key in scope_store.scope_keys_matching(OverrideScope.SESSION, prefixes=[source_prefix]):
        if chat_key in still_at_source:
            continue
        orphan_moved += scope_store.move_scope_key(
            OverrideScope.SESSION,
            chat_key,
            f"{target_prefix}{chat_key[len(source_prefix) :]}",
        )
    return {"plugin_settings": len(plugins), "plugin_session_toggles_orphan": orphan_moved}


def sweep_stale_plugin_settings(
    adapter_key: str,
    instance_key: str,
    *,
    include_instance_level: bool,
    exclude_chat_keys: Iterable[str] = (),
) -> Tuple[Dict[str, int], Tuple[str, ...]]:
    """新建实例之前清掉这个标识上残留的插件设置，返回 `(各自删掉的数量, 涉及的插件)`

    防「同名新实例继承旧设置」不能只靠删除那一刻：删除与提交之间有人写回、平台方对已删的标识
    写过配置、旧版本留下的、迁移失败留下的，都会让新实例一建出来就读到别人的东西。所以每次新建都
    兜一遍底。

    - 以 `{适配器}-{实例}-` 开头的频道级开关：这个实例对还不存在，带它前缀的键是残留——
      在频道表里属于别的实例的（`exclude_chat_keys`，频道标识由客户端给的适配器上会撞上）除外；
    - `include_instance_level`：同一个实例标识在别的适配器下**没有**活着的行时才清实例级的两样——
      它们只按实例标识存，有活行时是那一行的设置（存量的跨适配器同名，启动自检会告警）。
    """
    session_keys = _prefix_keys_excluding(
        scope_store.scope_keys_matching(
            OverrideScope.SESSION,
            prefixes=[instance_chat_key_prefix(adapter_key, instance_key)],
        ),
        (),
        exclude_chat_keys,
    )
    plugins = set(scope_store.plugins_with_overrides(OverrideScope.SESSION, session_keys))
    counts = {"plugin_session_toggles": scope_store.drop_scope_keys(OverrideScope.SESSION, session_keys)}
    if include_instance_level and instance_key:
        plugins.update(instance_level_plugins(instance_key))
        counts["plugin_instance_configs"] = len(scope_store.drop_instance_config_dirs(instance_key))
        counts["plugin_instance_toggles"] = scope_store.drop_scope_keys(OverrideScope.INSTANCE, [instance_key])
        scope_store.invalidate_instance_scoped_configs(instance_key)
    return counts, tuple(sorted(plugins))
