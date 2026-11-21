"""实例名下的「设置」在生命周期里的统一入口：插件设置 + 核心配置的实例层 / 频道层覆盖

两样都是按实例标识 / 会话标识存的文件，都不在数据库里，口径也一样（与 SSH 凭据、实例自有邮箱
同一条取舍：上一任主人填的、能拿去用的东西默认不跟过去）：

| 操作 | 处理 |
| --- | --- |
| 彻底删除实例 | 一律删：改库之前删一次（删不掉整次失败），提交之后再兜一次（失败只记警告） |
| 划转给别的租户 | 默认删、给勾选框；取消勾选跟着实例走 |
| 跨租户的实例迁移 / 并入 | 默认删、给勾选框（同一归属的迁移总是跟过去） |
| 换会话标识、同归属的实例迁移 | 跟着走，新标识下已有的保留 |
| 新建实例（含认领为新实例、协议端自动登记） | 建之前把这个标识上的残留清掉，记 WARNING |
| 协议端自动登记时没清掉 | 照样登记、带补扫标记；认领 / 并入 / 划转之前补扫成功才放行 |

两侧的细节各在自己的模块：插件 `plugin/scope_lifecycle.py`，核心配置 `config_override_lifecycle.py`。
这里只负责「两样一起做」与做完之后让插件的进程级加载状态跟上（删掉一个实例上单独开着的插件，
全局又关着时它要下线）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Iterable, List, Tuple

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services import config_override_lifecycle
from nekro_agent.services.plugin import scope_lifecycle

if TYPE_CHECKING:
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

logger = get_sub_logger("instance_settings")


@dataclass(frozen=True)
class InstanceSettingsCounts:
    """预览用的两个数：有设置的插件个数、有生效覆盖的配置文件个数"""

    plugin_settings: int
    config_overrides: int


async def instance_chat_keys(adapter_key: str, instance_key: str) -> List[str]:
    """频道表里这个实例名下的会话标识（彻底删除要在删频道行之前取）"""
    return await scope_lifecycle.instance_chat_keys(adapter_key, instance_key)


async def describe_counts(adapter_key: str, instance_key: str, chat_keys: List[str]) -> InstanceSettingsCounts:
    """这个实例名下有设置的插件数与有生效覆盖的配置文件数（`chat_keys` 由调用方先查好）"""
    foreign = await foreign_chat_keys(adapter_key, instance_key)
    plugins = scope_lifecycle.describe_instance_plugin_settings(
        adapter_key,
        instance_key,
        chat_keys,
        exclude_chat_keys=foreign,
    ).plugins
    overrides = config_override_lifecycle.describe_instance_config_overrides(
        adapter_key,
        instance_key,
        chat_keys,
        exclude_chat_keys=foreign,
    )
    return InstanceSettingsCounts(plugin_settings=len(plugins), config_overrides=overrides.active)


async def count_instance_settings(adapter_key: str, instance_key: str) -> InstanceSettingsCounts:
    """预览用：这个实例名下有设置的插件数与有生效覆盖的配置文件数"""
    return await describe_counts(adapter_key, instance_key, await instance_chat_keys(adapter_key, instance_key))


async def resync_plugin_load_states(plugin_keys: Iterable[str]) -> None:
    """删、搬之后让涉及插件的进程级加载状态跟上；失败只记警告（设置已经改了）"""
    keys = sorted(set(plugin_keys))
    if not keys:
        return
    from nekro_agent.services.plugin.manager import sync_plugin_load_states

    try:
        failures = await sync_plugin_load_states(keys)
    except Exception as e:  # noqa: BLE001 - 加载状态同步失败不该让已经完成的删、搬报错
        logger.warning(f"同步插件进程级加载状态失败: {type(e).__name__}: {e}")
        return
    for plugin_key, error in failures.items():
        logger.warning(f"同步插件进程级加载状态失败: {plugin_key} | {error}")


async def foreign_chat_keys(adapter_key: str, instance_key: str) -> List[str]:
    """带这个实例前缀、但在频道表里属于**别的**（适配器, 实例）的会话标识

    前缀 `{适配器}-{实例}-` 只对把实例段编进会话标识的适配器成立（OneBot）；频道标识由客户端给的
    适配器（SSE 等）上，一个活着的频道完全可能恰好以 `<适配器>-<某个实例名>-` 开头。按前缀删、搬、
    清扫时都要排除这些，否则建一个同名实例就把别人频道的开关与覆盖删了。
    """
    from nekro_agent.models.db_chat_channel import DBChatChannel

    prefix = scope_lifecycle.instance_chat_key_prefix(adapter_key, instance_key)
    if not prefix:
        return []
    rows = await DBChatChannel.filter(chat_key__startswith=prefix).values_list("chat_key", "adapter_key", "instance_key")
    return sorted(
        {str(chat_key) for chat_key, row_adapter, row_instance in rows if (row_adapter, row_instance) != (adapter_key, instance_key)},
    )


async def drop_instance_settings(
    adapter_key: str,
    instance_key: str,
    chat_keys: List[str],
    *,
    plugin_settings: bool = True,
    config_overrides: bool = True,
) -> Dict[str, int]:
    """删掉这个实例名下的设置（两样各自可选），返回各自删掉的数量

    出错直接抛出：彻底删除、划转、跨租户迁移都在改库之前调它，删不掉就让整次操作失败。
    插件这一侧先删（里面有 API Key），删不动时核心配置那一侧还没动。
    """
    foreign = await foreign_chat_keys(adapter_key, instance_key)
    counts: Dict[str, int] = {}
    plugins: Tuple[str, ...] = ()
    if plugin_settings:
        plugins = scope_lifecycle.describe_instance_plugin_settings(
            adapter_key,
            instance_key,
            chat_keys,
            exclude_chat_keys=foreign,
        ).plugins
        counts.update(
            scope_lifecycle.drop_instance_plugin_settings(adapter_key, instance_key, chat_keys, exclude_chat_keys=foreign),
        )
    if config_overrides:
        counts.update(
            config_override_lifecycle.drop_instance_config_overrides(
                adapter_key,
                instance_key,
                chat_keys,
                exclude_chat_keys=foreign,
            ),
        )
    await resync_plugin_load_states(plugins)
    return counts


async def drop_instance_settings_quietly(
    adapter_key: str,
    instance_key: str,
    chat_keys: List[str],
    *,
    plugin_settings: bool = True,
    config_overrides: bool = True,
    reason: str = "彻底删除实例提交之后",
) -> Dict[str, int]:
    """同上，但失败只记警告（库已经改完之后的那一遍兜底用：不能再让整次操作报错）"""
    try:
        return await drop_instance_settings(
            adapter_key,
            instance_key,
            chat_keys,
            plugin_settings=plugin_settings,
            config_overrides=config_overrides,
        )
    except Exception as e:  # noqa: BLE001 - 兜底清扫失败只记警告，建实例前还会再扫一次
        logger.warning(f"{reason}兜底清扫 {adapter_key}/{instance_key} 的设置失败: {type(e).__name__}: {e}")
        return {}


async def sweep_before_create(adapter_key: str, instance_key: str) -> Dict[str, int]:
    """新建实例之前清掉这个标识上残留的设置，有残留就记 WARNING，返回清掉的数量

    同一个实例标识在别的适配器下还有活着的行时，插件的实例级设置（只按标识存）是那一行的，不动；
    频道级与核心配置的两层都按（适配器, 实例）区分，这个实例对还不存在，带它前缀的是残留——
    频道表里属于别的实例的那些除外（见 `foreign_chat_keys`）。平台方邮箱共享名单里点名这个实例对的项
    也摘掉：不摘的话，同名的新实例一建出来就拿到那份发信授权。
    出错直接抛出：建实例的调用方决定拒绝还是只记日志（协议端自动登记不能因此把入站消息挡掉）。
    """
    from nekro_agent.models.db_adapter_instance import TOMBSTONE_STATUS, DBAdapterInstance

    shared_elsewhere = (
        await DBAdapterInstance.filter(instance_key=instance_key)
        .exclude(adapter_key=adapter_key)
        .exclude(status=TOMBSTONE_STATUS)
        .exists()
    )
    foreign = await foreign_chat_keys(adapter_key, instance_key)
    plugin_counts, plugins = scope_lifecycle.sweep_stale_plugin_settings(
        adapter_key,
        instance_key,
        include_instance_level=not shared_elsewhere,
        exclude_chat_keys=foreign,
    )
    counts = {
        **plugin_counts,
        **config_override_lifecycle.sweep_stale_config_overrides(adapter_key, instance_key, exclude_chat_keys=foreign),
        "platform_mail_share": _revoke_platform_mail_share(adapter_key, instance_key),
    }
    if any(counts.values()):
        logger.warning(
            f"新建实例 {adapter_key}/{instance_key} 之前清掉了这个标识上残留的设置（不该有，"
            f"可能是删除与提交之间被写回、或旧版本留下的）: {counts}",
        )
    await resync_plugin_load_states(plugins)
    return counts


# 协议端自动登记时兜底清扫失败（入站链路不能因此挡消息，照样登记）留在 `metadata_json` 里的标记：
# 这个标识上可能还躺着前一任主人的设置，认领 / 并入 / 划转之前必须先补扫成功
SWEEP_PENDING_KEY = "settings_sweep_pending"


def sweep_pending_metadata() -> str:
    """带补扫标记的 `metadata_json`（自动登记的新行用）"""
    return json.dumps({SWEEP_PENDING_KEY: True}, ensure_ascii=False)


def is_sweep_pending(instance: DBAdapterInstance) -> bool:
    """这一行是不是带着补扫标记；元数据不是合法 JSON 时看里面有没有这个键名（宁可多扫一遍）"""
    raw = (instance.metadata_json or "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except ValueError:
        return SWEEP_PENDING_KEY in raw
    return bool(parsed.get(SWEEP_PENDING_KEY)) if isinstance(parsed, dict) else False


async def resweep_if_pending(instance: DBAdapterInstance) -> bool:
    """带补扫标记的行：再扫一遍这个标识上残留的设置，成功后摘掉标记，返回是否扫过

    出错直接抛出（调用方拒绝认领 / 并入 / 划转）：扫不掉就意味着前一任主人的设置可能还在，
    这时把实例交出去就是那份设置跟着交出去。扫的是整个标识——登记之后到现在平台方写上去的也一并清掉，
    分不清哪些是残留时只能这样（见 `sweep_before_create`）。
    """
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance as InstanceModel

    if not is_sweep_pending(instance):
        return False
    await sweep_before_create(instance.adapter_key, instance.instance_key)
    row = await InstanceModel.get(id=instance.id)
    try:
        metadata = json.loads(row.metadata_json or "{}")
    except ValueError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.pop(SWEEP_PENDING_KEY, None)
    row.metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else ""
    await row.save(update_fields=["metadata_json", "update_time"])
    instance.metadata_json = row.metadata_json
    logger.warning(f"实例 {instance.adapter_key}/{instance.instance_key} 登记时没扫成的残留设置已补扫，标记已摘掉")
    return True


def _revoke_platform_mail_share(adapter_key: str, instance_key: str) -> int:
    """平台方邮箱 `SEND_SHARE_INSTANCES` 里点名这个实例对的项摘掉，返回改动的账户数

    读不到邮箱配置（email 适配器没注册之类）只记警告：这一步是兜底，彻底删除与划转时已经摘过。
    """
    from nekro_agent.services.email_accounts import rewrite_platform_share_instances

    try:
        return rewrite_platform_share_instances(f"{adapter_key}:{instance_key}", None)
    except Exception as e:  # noqa: BLE001 - 兜底，不挡建实例
        logger.warning(f"新建实例前清理平台方邮箱共享名单失败: {adapter_key}/{instance_key} | {type(e).__name__}")
        return 0


async def transfer_session_settings(
    old_chat_key: str,
    new_chat_key: str,
    *,
    drop_plugin_settings: bool = False,
    drop_config_overrides: bool = False,
) -> Dict[str, int]:
    """换会话标识时频道级插件开关与频道层配置覆盖的去向：默认跟到新标识上（新标识下已有的保留）

    `drop_*` 为真（跨租户迁移选了删除）时那一样不搬，**删掉旧标识上的**：迁移开头已经删过一次，
    但开头与搬到这个频道之间，源租户还能往旧标识上写；搬过去就等于交给目标方。
    """
    plugins = scope_lifecycle.plugins_on_session_key(old_chat_key)
    counts: Dict[str, int] = {}
    if drop_plugin_settings:
        counts["plugin_session_toggle"] = 0
        counts["plugin_session_toggle_deleted"] = scope_lifecycle.drop_session_plugin_settings(old_chat_key)
    else:
        counts["plugin_session_toggle"] = scope_lifecycle.move_session_plugin_settings(old_chat_key, new_chat_key)
    if drop_config_overrides:
        counts["config_channel_override"] = 0
        counts["config_channel_override_deleted"] = config_override_lifecycle.drop_channel_config_override(old_chat_key)
    else:
        counts["config_channel_override"] = config_override_lifecycle.move_channel_config_override(
            old_chat_key,
            new_chat_key,
        )
    await resync_plugin_load_states(plugins)
    return counts


async def move_session_settings(old_chat_key: str, new_chat_key: str) -> Dict[str, int]:
    """换会话标识：频道级插件开关与频道层配置覆盖跟到新标识上（新标识下已有的保留）"""
    return await transfer_session_settings(old_chat_key, new_chat_key)


async def move_instance_settings(
    adapter_key: str,
    source_instance_key: str,
    target_instance_key: str,
    *,
    exclude_chat_keys: Iterable[str] = (),
    instance_level: bool = True,
    plugin_settings: bool = True,
    config_overrides: bool = True,
) -> Dict[str, int]:
    """实例迁移：两样都改到目标实例（见两侧模块的同名函数），返回各自的数量

    `exclude_chat_keys`（还留在源实例名下的频道）之外，频道表里属于别的实例的前缀键也不搬。
    `plugin_settings` / `config_overrides` 为假的那一样不搬（跨租户迁移选了删除：由调用方在搬完之后删）。
    """
    excluded = [*exclude_chat_keys, *await foreign_chat_keys(adapter_key, source_instance_key)]
    counts: Dict[str, int] = {}
    plugins: List[str] = []
    if plugin_settings:
        plugins = scope_lifecycle.instance_level_plugins(source_instance_key) if instance_level else []
        counts.update(
            scope_lifecycle.move_instance_plugin_settings(
                adapter_key,
                source_instance_key,
                target_instance_key,
                exclude_chat_keys=excluded,
                instance_level=instance_level,
            ),
        )
    if config_overrides:
        counts.update(
            config_override_lifecycle.move_instance_config_overrides(
                adapter_key,
                source_instance_key,
                target_instance_key,
                exclude_chat_keys=excluded,
                instance_level=instance_level,
            ),
        )
    await resync_plugin_load_states(plugins)
    return counts


async def shared_instance_level_conflict(adapter_key: str, instance_key: str, moving_row_ids: Iterable[int]) -> bool:
    """划转时：这个标识在别的适配器下还有一行、它不跟着改归属，而这个标识上有插件的实例级设置

    插件的实例级设置只按实例标识存，两行共用一份：删了就连那一行的一起删，留着又等于交给新主人。
    这种情况由调用方拒绝（与彻底删除遇到跨适配器同名时同一个做法），让平台方先处理同名。
    """
    from nekro_agent.models.db_adapter_instance import DBAdapterInstance

    if not scope_lifecycle.instance_level_plugins(instance_key):
        return False
    query = DBAdapterInstance.filter(instance_key=instance_key).exclude(adapter_key=adapter_key)
    moving = [int(row_id) for row_id in moving_row_ids]
    if moving:
        query = query.exclude(id__in=moving)
    return await query.exists()
