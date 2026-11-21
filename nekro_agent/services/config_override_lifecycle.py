"""核心配置的实例层 / 频道层覆盖的生命周期

覆盖文件有两层按实例 / 频道存（`config_service`）：

- 实例层：`configs/<适配器>/instances/<实例>/overrides.yaml`，配置键 `instance_override_<适配器>__<实例>`；
- 频道层：`configs/channels/<适配器>/<会话标识>.yaml`，配置键 `channel_config_<会话标识>`。

里面是 `CoreConfig` 里标了 `overridable` 的那些字段。逐个核实过：凭据类只有
`INSTANCE_ALERT_WEBHOOK_URL`（`is_secret`，告警 Webhook，地址里常带令牌）；另有六个 `ref_model_groups`
字段（`USE_MODEL_GROUP` 等）按名字指向模型组——指向的是前一个租户自己建的模型组时，等于新主人在用
前一个租户的上游 Key；提示词三段与输出过滤词表是租户自己写的内容。所以与插件设置同一套口径
（`plugin/scope_lifecycle.py`）：彻底删除一律删，划转默认删、给勾选框，换会话标识与实例迁移跟着走，
跨租户迁移默认删，新建实例前兜底清扫。

文件之外还有两处缓存：`ConfigManager` 里按配置键缓存的覆盖对象，与 `config_resolver` 按版本号缓存的
合并结果。删、搬之后两处都要失效，否则删掉的覆盖在重启之前照样生效。

读侧（`UnifiedConfigService._get_config_instance`）对覆盖类配置键会顺手 `dump_config()` 落一个空文件，
所以「文件存在」不等于「有覆盖」：计数只算里面真有生效覆盖项（`enable_X: true`）的文件，删则一律删。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from nekro_agent.core.core_utils import ConfigManager, atomic_write_text
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.services import config_service
from nekro_agent.tools.fs_links import is_link, unlink_link

logger = get_sub_logger("config_override_lifecycle")

_CHANNEL_SUFFIX = ".yaml"


class ConfigOverrideLinkError(OSError):
    """覆盖文件所在的目录是链接，按路径删、搬都不安全"""


@dataclass(frozen=True)
class InstanceConfigOverrides:
    """一个实例名下现有的覆盖文件"""

    instance_file: Optional[Path]
    channel_files: Tuple[Tuple[str, Path], ...]
    active: int


def _present(path: Path) -> bool:
    return is_link(path) or path.is_file()


def instance_override_file(adapter_key: str, instance_key: str) -> Optional[Path]:
    """实例层覆盖文件的路径；标识不合法时为 None（写入侧同样拒绝这种标识，不可能有文件）"""
    if not adapter_key or not instance_key:
        return None
    try:
        return config_service.instance_override_config_path(adapter_key, instance_key)
    except ValueError:
        return None


def channel_override_file(chat_key: str) -> Optional[Path]:
    """频道层覆盖文件的路径；会话标识不能用作文件名时为 None"""
    try:
        return config_service.channel_override_config_path(chat_key)
    except ValueError:
        return None


def _channel_files_with_prefix(adapter_key: str, prefix: str) -> List[Tuple[str, Path]]:
    """`configs/channels/<适配器>/` 下文件名以 `prefix` 开头的覆盖文件：`[(会话标识, 路径)]`"""
    if not prefix:
        return []
    try:
        directory = config_service.channel_override_dir(adapter_key)
    except ValueError:
        return []
    if not directory.is_dir():
        return []
    found: List[Tuple[str, Path]] = []
    for path in sorted(directory.iterdir()):
        if path.name.endswith(_CHANNEL_SUFFIX) and path.name.startswith(prefix):
            found.append((path.name[: -len(_CHANNEL_SUFFIX)], path))
    return found


def _load(path: Path) -> Dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 - 坏文件按「没有生效覆盖」处理，删、搬照样进行
        logger.warning(f"读取覆盖文件失败，按没有生效覆盖处理: {path.name} | {type(e).__name__}")
        return {}
    return raw if isinstance(raw, dict) else {}


def _active_pairs(data: Dict[str, Any]) -> Dict[str, Any]:
    """只留生效的覆盖项（`enable_X` 为真的 `enable_X` / `X` 对），与 `OverridableConfigBase.dump_config` 同一口径"""
    active: Dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str) or key.startswith("enable_"):
            continue
        if data.get(f"enable_{key}") is True:
            active[f"enable_{key}"] = True
            active[key] = value
    return active


def _has_active(path: Path) -> bool:
    return bool(_active_pairs(_load(path)))


def describe_instance_config_overrides(
    adapter_key: str,
    instance_key: str,
    chat_keys: Iterable[str],
    *,
    exclude_chat_keys: Iterable[str] = (),
) -> InstanceConfigOverrides:
    """这个实例名下现有的覆盖文件（只读）：实例层一个，频道层 = 它的频道 ∪ 以实例前缀开头的文件

    `exclude_chat_keys`：带实例前缀、但在频道表里属于**别的**实例的会话标识。前缀只对把实例段编进
    会话标识的适配器成立；频道标识由客户端给的适配器上，一个活着的频道完全可能恰好以
    `<适配器>-<这个实例名>-` 开头，它的覆盖不是这个实例的。
    """
    from nekro_agent.services.plugin.scope_lifecycle import instance_chat_key_prefix

    instance_file = instance_override_file(adapter_key, instance_key)
    if instance_file is not None and not _present(instance_file):
        instance_file = None

    excluded = set(exclude_chat_keys)
    channels: Dict[str, Path] = {}
    for chat_key in chat_keys:
        path = channel_override_file(chat_key)
        if path is not None and _present(path):
            channels[chat_key] = path
    for chat_key, path in _channel_files_with_prefix(adapter_key, instance_chat_key_prefix(adapter_key, instance_key)):
        if chat_key not in excluded:
            channels.setdefault(chat_key, path)

    files = [instance_file, *channels.values()] if instance_file is not None else list(channels.values())
    return InstanceConfigOverrides(
        instance_file=instance_file,
        channel_files=tuple(sorted(channels.items())),
        active=sum(1 for path in files if is_link(path) or _has_active(path)),
    )


def _refuse_linked_dir(path: Path) -> None:
    """文件所在的目录（以及上一级）是链接时拒绝：那两级是同一适配器下所有实例 / 频道共用的"""
    if is_link(path.parent) or is_link(path.parent.parent):
        raise ConfigOverrideLinkError(f"覆盖文件所在的目录是链接，不按路径删除或搬动，请人工处理: {path.parent.name}")


def _remove(path: Path) -> None:
    if is_link(path):
        unlink_link(path)
    elif path.exists():
        path.unlink()


def _invalidate(config_keys: Iterable[str]) -> None:
    """注销 `ConfigManager` 里缓存的覆盖对象，并让合并结果缓存整体失效"""
    for config_key in config_keys:
        ConfigManager.unregister_config(config_key)
    from nekro_agent.services.config_resolver import config_resolver

    config_resolver.bump_version()


def _channel_config_key(chat_key: str) -> str:
    return f"{config_service.CHANNEL_OVERRIDE_KEY_PREFIX}{chat_key}"


def _instance_config_key(adapter_key: str, instance_key: str) -> Optional[str]:
    try:
        return config_service.instance_override_config_key(adapter_key, instance_key)
    except ValueError:
        return None


def _drop_instance_level(adapter_key: str, instance_key: str) -> int:
    """删实例层覆盖，返回删没删（0 / 1）

    `instances/<实例>/` 这一级是这个实例自己的：它本身是链接时只删链接——**不管里面有没有
    `overrides.yaml`**（断链、或者链过去的目录里还没有文件，读侧照样会顺着链接建文件）。
    上两级（`instances/`、`<适配器>/`）是链接时报错。
    """
    path = instance_override_file(adapter_key, instance_key)
    if path is None:
        return 0
    instance_dir = path.parent
    if not is_link(instance_dir) and not _present(path):
        return 0
    if is_link(instance_dir.parent) or is_link(instance_dir.parent.parent):
        raise ConfigOverrideLinkError(f"实例覆盖所在的目录是链接，不按路径删除，请人工处理: {adapter_key}")
    if is_link(instance_dir):
        unlink_link(instance_dir)
        return 1
    _remove(path)
    if instance_dir.is_dir() and not any(instance_dir.iterdir()):
        instance_dir.rmdir()
    return 1


def _cached_channel_keys_with_prefix(prefix: str) -> List[str]:
    """`ConfigManager` 里缓存着的、会话标识以 `prefix` 开头的频道层覆盖配置键（文件可能早就没了）"""
    if not prefix:
        return []
    head = _channel_config_key(prefix)
    return [key for key in ConfigManager.get_all_config_keys() if key.startswith(head)]


def drop_instance_config_overrides(
    adapter_key: str,
    instance_key: str,
    chat_keys: Iterable[str],
    *,
    exclude_chat_keys: Iterable[str] = (),
) -> Dict[str, int]:
    """删掉这个实例名下的覆盖文件（实例层、它的频道、实例前缀的残留频道），返回删掉的个数

    出错直接抛出（调用方决定是中止还是记警告）。链接只删链接本身；所在目录是链接时报错。
    `active` 不影响删不删：空文件也删（读侧落的空壳），否则目录里会留下一堆没主的文件。

    缓存一律注销，不看文件在不在：传进来的会话标识、以实例前缀开头的缓存键（排除属于别的实例的）、
    实例层的键。文件早被删了而缓存里的对象还在时，那份覆盖照样生效到重启。
    """
    from nekro_agent.services.plugin.scope_lifecycle import instance_chat_key_prefix

    chat_key_list = list(chat_keys)
    excluded = set(exclude_chat_keys)
    overrides = describe_instance_config_overrides(
        adapter_key,
        instance_key,
        chat_key_list,
        exclude_chat_keys=excluded,
    )
    removed_instance = _drop_instance_level(adapter_key, instance_key)
    for _chat_key, path in overrides.channel_files:
        _refuse_linked_dir(path)
        _remove(path)
    config_keys = {_channel_config_key(chat_key) for chat_key in chat_key_list}
    config_keys.update(_channel_config_key(chat_key) for chat_key, _ in overrides.channel_files)
    config_keys.update(
        key
        for key in _cached_channel_keys_with_prefix(instance_chat_key_prefix(adapter_key, instance_key))
        if key[len(config_service.CHANNEL_OVERRIDE_KEY_PREFIX) :] not in excluded
    )
    instance_config_key = _instance_config_key(adapter_key, instance_key)
    if instance_config_key:
        config_keys.add(instance_config_key)
    _invalidate(sorted(config_keys))
    counts = {"config_instance_override": removed_instance, "config_channel_overrides": len(overrides.channel_files)}
    if any(counts.values()):
        logger.warning(
            f"删除实例的配置覆盖: adapter_key={adapter_key}, instance_key={instance_key}, counts={counts}",
        )
    return counts


def _merge_into(source: Path, target: Path) -> None:
    """把 `source` 的生效覆盖并进 `target`：目标已生效的项保留，只补目标没开的那些；之后删掉源"""
    target_data = _active_pairs(_load(target))
    for key, value in _active_pairs(_load(source)).items():
        if key.startswith("enable_"):
            continue
        if target_data.get(f"enable_{key}") is True:
            continue
        target_data[f"enable_{key}"] = True
        target_data[key] = value
    atomic_write_text(
        target,
        yaml.dump(target_data, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _remove(source)


def _move_file(source: Path, target: Path) -> bool:
    """把一个覆盖文件搬到新路径（目标已有则合并），返回是否动过"""
    if not _present(source):
        return False
    _refuse_linked_dir(source)
    _refuse_linked_dir(target)
    if not _present(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)  # 源是链接时改名的是链接本身
    elif is_link(source):
        unlink_link(source)  # 目标已有：以目标为准，源那个链接只删链接
    else:
        _merge_into(source, target)
    return True


def move_channel_config_override(old_chat_key: str, new_chat_key: str) -> int:
    """换会话标识：频道层覆盖跟到新标识上（新标识下已生效的项保留），返回搬了几个文件"""
    if not old_chat_key or not new_chat_key or old_chat_key == new_chat_key:
        return 0
    source = channel_override_file(old_chat_key)
    target = channel_override_file(new_chat_key)
    if source is None or target is None:
        return 0
    moved = _move_file(source, target)
    if moved:
        _invalidate([_channel_config_key(old_chat_key), _channel_config_key(new_chat_key)])
    return int(moved)


def move_instance_config_overrides(
    adapter_key: str,
    source_instance_key: str,
    target_instance_key: str,
    *,
    exclude_chat_keys: Iterable[str] = (),
    instance_level: bool = True,
) -> Dict[str, int]:
    """实例迁移：实例层覆盖改到目标实例，没有频道行的频道层残留按前缀改名

    有频道行的那些在逐个频道迁移时已经跟着新标识搬了（`move_channel_config_override`）；
    `exclude_chat_keys` 是还留在源实例名下的频道（迁移失败的），它们不是残留，不搬。
    `instance_level=False`（有频道迁移失败时）实例层留在源实例上，重跑补齐那一次再搬。
    """
    from nekro_agent.services.plugin.scope_lifecycle import instance_chat_key_prefix

    counts = {"config_overrides": 0}
    if not source_instance_key or not target_instance_key or source_instance_key == target_instance_key:
        return counts
    config_keys: List[str] = []
    if instance_level:
        source = instance_override_file(adapter_key, source_instance_key)
        target = instance_override_file(adapter_key, target_instance_key)
        if source is not None and target is not None and _move_file(source, target):
            counts["config_overrides"] += 1
            if source.parent.is_dir() and not is_link(source.parent) and not any(source.parent.iterdir()):
                source.parent.rmdir()
            config_keys += [
                key
                for key in (
                    _instance_config_key(adapter_key, source_instance_key),
                    _instance_config_key(adapter_key, target_instance_key),
                )
                if key
            ]

    source_prefix = instance_chat_key_prefix(adapter_key, source_instance_key)
    target_prefix = instance_chat_key_prefix(adapter_key, target_instance_key)
    still_at_source = set(exclude_chat_keys)
    for chat_key, path in _channel_files_with_prefix(adapter_key, source_prefix):
        if chat_key in still_at_source:
            continue
        new_chat_key = f"{target_prefix}{chat_key[len(source_prefix) :]}"
        target_path = channel_override_file(new_chat_key)
        if target_path is not None and _move_file(path, target_path):
            counts["config_overrides"] += 1
            config_keys += [_channel_config_key(chat_key), _channel_config_key(new_chat_key)]
    if config_keys:
        _invalidate(config_keys)
    return counts


def sweep_stale_config_overrides(
    adapter_key: str,
    instance_key: str,
    *,
    exclude_chat_keys: Iterable[str] = (),
) -> Dict[str, int]:
    """新建实例之前清掉这个实例对上残留的覆盖文件（实例层、以实例前缀开头的频道层）

    实例层文件按（适配器, 实例）存，不像插件的实例级设置那样跨适配器共用：这个实例对还不存在，
    它名下的文件一律是残留。前缀命中、但频道表里属于别的实例的会话（`exclude_chat_keys`）除外。
    """
    return drop_instance_config_overrides(adapter_key, instance_key, (), exclude_chat_keys=exclude_chat_keys)


def drop_channel_config_override(chat_key: str) -> int:
    """删一个会话标识上的频道层覆盖（跨租户迁移选了删除时，逐个频道搬运的那一步用），返回删没删"""
    path = channel_override_file(chat_key)
    if path is None:
        return 0
    removed = 0
    if _present(path):
        _refuse_linked_dir(path)
        _remove(path)
        removed = 1
    _invalidate([_channel_config_key(chat_key)])
    return removed


def count_active_overrides(adapter_key: str, instance_key: str) -> int:
    """实例层是否还有生效的覆盖（迁移残留统计用，0 或 1）"""
    path = instance_override_file(adapter_key, instance_key)
    return int(path is not None and _present(path) and (is_link(path) or _has_active(path)))
