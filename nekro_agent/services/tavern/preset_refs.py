"""角色扮演预设被哪些配置层引用（删除前提醒）

删除预设不会改写引用它的配置：`PROMPT_RP_PRESET_ID` 只是一个整数列里的 id。删掉以后，
运行时会打一条去重 WARNING 然后按「没选预设」渲染（`services/tavern/runtime.py`），
既不断流也不报错。删除本身**不拦**——这里只回答「删掉它会影响哪些配置」，供删除确认框
提醒与「引用数」展示，由操作者决定是否先改配置。

## 扫哪些层

`PROMPT_RP_PRESET_ID` 是可覆盖字段（`core/config.py`），所以引用可能落在四层上：

| 层        | 来源                                                     | label                         |
| --------- | -------------------------------------------------------- | ----------------------------- |
| `system`  | 全局配置 `config`                                        | 固定 `system`                 |
| `adapter` | `configs/<adapter>/overrides.yaml`                       | 适配器标识                    |
| `instance`| `configs/<adapter>/instances/<instance>/overrides.yaml`  | 实例显示名，没有则实例标识    |
| `channel` | `configs/channels/<adapter>/<chat_key>.yaml`             | 频道名，没有则 chat_key       |

覆盖层只认 `enable_PROMPT_RP_PRESET_ID` 为真且取值等于目标 id 的项，与 `config_resolver`
同一口径：开关关着的值不参与解析，算它「引用」只会制造假警报。取值按整数比，手写成
`"7"` 的字符串也能认出来。

## 怎么枚举

实例与频道**先查库**（`DBAdapterInstance` / `DBChatChannel`），再按落盘口径补扫：

- 实例层目录 `configs/<adapter>/instances/` 下还有目录、但实例行已经没了的同样要扫——
  实例删了、文件残留时它照样会在配置页里出现；
- 频道层用生命周期里现成的 `describe_instance_config_overrides`（实例自己的频道 ∪ 以实例
  前缀开头、且不属于别的实例的残留文件），不另写一套命名规则。

读**不新建文件**：只在文件确实存在时才走 `UnifiedConfigService._get_config_instance`
（它加载后会顺手把文件写一遍），已加载过的直接用 `ConfigManager` 里那份（它比文件新）。

## 作用域

`only_instance_key` 非空时只扫这个实例的实例层与它名下频道的频道层（`GET` 接口把作用域
传进来，租户看不到平台层与别的实例）。平台方的全局视图（空串）扫全部四层。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, Field

from nekro_agent.adapters import ADAPTER_REGISTRY
from nekro_agent.core.config import config as system_config
from nekro_agent.core.core_utils import ConfigBase, ConfigManager
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.services import config_service
from nekro_agent.services.config_override_lifecycle import describe_instance_config_overrides
from nekro_agent.services.config_service import (
    ADAPTER_OVERRIDE_KEY_PREFIX,
    CHANNEL_OVERRIDE_KEY_PREFIX,
    UnifiedConfigService,
    instance_override_config_key,
)
from nekro_agent.services.instance_settings import foreign_chat_keys, instance_chat_keys

if TYPE_CHECKING:
    from nekro_agent.services.config_override_lifecycle import InstanceConfigOverrides

logger = get_sub_logger("tavern.preset_refs")

# 引用所在的配置层，与四层覆盖的名字一致
RP_PRESET_REF_LAYERS = ("system", "adapter", "instance", "channel")

_REF_FIELD = "PROMPT_RP_PRESET_ID"

# 一个预设最多返回多少条引用：确认框只需要让人看清「有没有、大概在哪」；`total` 如实给出总数
MAX_REFERENCES_PER_PRESET = 50


class RpPresetReference(BaseModel):
    """一处引用"""

    layer: str  # "system" / "adapter" / "instance" / "channel"
    adapter_key: str = ""
    instance_key: str = ""
    chat_key: str = ""
    # system 固定 "system"；adapter = adapter_key；instance = 显示名（没有则标识）；
    # channel = 频道名（没有则 chat_key）
    label: str


class RpPresetReferencesResponse(BaseModel):
    """一个预设的引用情况；`items` 最多 `MAX_REFERENCES_PER_PRESET` 条，`total` 为总数"""

    total: int
    items: List[RpPresetReference] = Field(default_factory=list)


def _as_int(value: Any) -> Optional[int]:
    """读成整数；读不出来（手填的怪值、None）返回 None"""
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return None


def _load_layer(config_key: str, path: Optional[Path]) -> Optional[ConfigBase]:
    """取一层的覆盖配置；文件不存在时返回 None，**不**让读侧建文件"""
    cached = ConfigManager.get_config(config_key)
    if cached is not None:
        return cached
    if path is None or not path.is_file():
        return None
    return UnifiedConfigService._get_config_instance(config_key)  # noqa: SLF001


def _override_value(layer: Optional[ConfigBase]) -> Optional[int]:
    """这一层里「启用且非 0」的 `PROMPT_RP_PRESET_ID`；没启用或取值不可读时返回 None"""
    if layer is None or not hasattr(layer, f"enable_{_REF_FIELD}"):
        return None
    if not bool(getattr(layer, f"enable_{_REF_FIELD}", False)):
        return None
    value = _as_int(getattr(layer, _REF_FIELD, 0))
    return value if value and value > 0 else None


def _adapter_keys() -> List[str]:
    return sorted(ADAPTER_REGISTRY)


def _instance_refs_on_disk(adapter_key: str) -> List[str]:
    """这个适配器下落盘上还有实例层目录的实例标识（实例行已经没了的也在这里）"""
    root = config_service.instance_override_root_dir(adapter_key)
    if not root.is_dir():
        return []
    try:
        return sorted(child.name for child in root.iterdir() if child.is_dir())
    except OSError as exc:  # noqa: BLE001 - 一个目录读不出来不该让整次查询失败
        logger.warning(f"读取实例覆盖目录失败，跳过该适配器的残留扫描: {adapter_key} | {type(exc).__name__}")
        return []


async def _instance_refs(only_instance_key: str, only_adapter_key: str = "") -> List[Tuple[str, str]]:
    """要扫的 (adapter_key, instance_key)

    作用域为空（平台方全局视图）：先查库，再补上落盘上还有实例层目录的残留实例——实例行
    已经删了、文件还留着时，它照样会出现在配置页里。作用域非空时只认这个实例的库行：
    `instance_key` 只在适配器内唯一，按名字扫盘会把别的适配器下同名实例的覆盖报出来。
    """
    query = DBAdapterInstance.all()
    if only_instance_key:
        query = query.filter(instance_key=only_instance_key)
        # 同一个实例标识在不同适配器下可以是两行、分属两个租户：调用方给了适配器就必须
        # 一起筛，否则会把别人那个同名实例的显示名与频道名报给当前租户。
        if only_adapter_key:
            query = query.filter(adapter_key=only_adapter_key)
    rows = await query.values_list("adapter_key", "instance_key")
    refs = {(str(adapter_key), str(instance_key)) for adapter_key, instance_key in rows if adapter_key and instance_key}
    if only_instance_key:
        return sorted(refs)

    for adapter_key in _adapter_keys():
        for instance_key in _instance_refs_on_disk(adapter_key):
            refs.add((adapter_key, instance_key))
    return sorted(refs)


async def _instance_labels(refs: Iterable[Tuple[str, str]]) -> Dict[Tuple[str, str], str]:
    """实例显示名；没有显示名的用实例标识"""
    labels: Dict[Tuple[str, str], str] = {}
    for adapter_key, instance_key in refs:
        row = await DBAdapterInstance.filter(adapter_key=adapter_key, instance_key=instance_key).first()
        name = (row.display_name or "").strip() if row is not None else ""
        labels[(adapter_key, instance_key)] = name or instance_key
    return labels


async def _channel_labels(chat_keys: Iterable[str]) -> Dict[str, str]:
    """频道名；没有频道名（或频道行已没了）的用 chat_key"""
    wanted = sorted(set(chat_keys))
    if not wanted:
        return {}
    rows = await DBChatChannel.filter(chat_key__in=wanted).values_list("chat_key", "channel_name")
    labels = {str(chat_key): (str(name or "").strip() or str(chat_key)) for chat_key, name in rows}
    for chat_key in wanted:
        labels.setdefault(chat_key, chat_key)
    return labels


async def _instance_overrides(adapter_key: str, instance_key: str) -> "InstanceConfigOverrides":
    """这个实例名下的覆盖文件（实例层 + 它的频道层），用生命周期里现成的口径"""
    chat_keys = await instance_chat_keys(adapter_key, instance_key)
    foreign = await foreign_chat_keys(adapter_key, instance_key)
    return describe_instance_config_overrides(
        adapter_key,
        instance_key,
        chat_keys,
        exclude_chat_keys=foreign,
    )


async def find_rp_preset_references(
    preset_id: int,
    *,
    only_instance_key: str = "",
    only_adapter_key: str = "",
) -> List[RpPresetReference]:
    """列出哪些配置层引用了这个角色扮演预设

    Args:
        preset_id: 预设 id；`<= 0` 直接返回空（0 是「不用」）
        only_instance_key: 非空时只看这个实例的实例层与它名下频道的频道层
        only_adapter_key: 与 `only_instance_key` 一起给，指明是哪个适配器下的那个实例；
            实例标识只在适配器内唯一，租户视角必须带上

    Returns:
        按 全局 → 适配器 → 实例 → 频道 的顺序，同一层内按标识排序
    """
    if int(preset_id) <= 0:
        return []

    wanted = int(preset_id)
    found: List[RpPresetReference] = []

    if not only_instance_key:
        if _as_int(getattr(system_config, _REF_FIELD, 0)) == wanted:
            found.append(RpPresetReference(layer="system", label="system"))

        for adapter_key in _adapter_keys():
            path = config_service.adapter_override_config_path(adapter_key)
            layer = _load_layer(f"{ADAPTER_OVERRIDE_KEY_PREFIX}{adapter_key}", path)
            if _override_value(layer) == wanted:
                found.append(RpPresetReference(layer="adapter", adapter_key=adapter_key, label=adapter_key))

    refs = await _instance_refs(only_instance_key, only_adapter_key)
    instance_labels = await _instance_labels(refs)
    channel_refs: List[Tuple[Tuple[str, str], str, Path]] = []
    channel_keys: List[str] = []

    for adapter_key, instance_key in refs:
        overrides = await _instance_overrides(adapter_key, instance_key)
        if overrides.instance_file is not None:
            layer = _load_layer(instance_override_config_key(adapter_key, instance_key), overrides.instance_file)
            if _override_value(layer) == wanted:
                found.append(
                    RpPresetReference(
                        layer="instance",
                        adapter_key=adapter_key,
                        instance_key=instance_key,
                        label=instance_labels[(adapter_key, instance_key)],
                    ),
                )
        for chat_key, path in overrides.channel_files:
            channel_refs.append(((adapter_key, instance_key), chat_key, path))
            channel_keys.append(chat_key)

    labels = await _channel_labels(channel_keys)
    for (adapter_key, instance_key), chat_key, path in channel_refs:
        layer = _load_layer(f"{CHANNEL_OVERRIDE_KEY_PREFIX}{chat_key}", path)
        if _override_value(layer) == wanted:
            found.append(
                RpPresetReference(
                    layer="channel",
                    adapter_key=adapter_key,
                    instance_key=instance_key,
                    chat_key=chat_key,
                    label=labels[chat_key],
                ),
            )

    return found


__all__ = [
    "MAX_REFERENCES_PER_PRESET",
    "RP_PRESET_REF_LAYERS",
    "RpPresetReference",
    "RpPresetReferencesResponse",
    "find_rp_preset_references",
]