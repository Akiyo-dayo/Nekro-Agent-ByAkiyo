from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from nekro_agent.core.config import CoreConfig
from nekro_agent.core.config import config as system_config
from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.services.config_service import (
    ADAPTER_OVERRIDE_KEY_PREFIX,
    CHANNEL_OVERRIDE_KEY_PREFIX,
    INSTANCE_OVERRIDE_KEY_PREFIX,
    INSTANCE_OVERRIDE_KEY_SEP,
    UnifiedConfigService,
    instance_override_config_key,
    instance_override_root_dir,
)

logger = get_sub_logger("config_system")


class ConfigLayer(str, Enum):
    """配置层级，取值同时作为溯源接口对外暴露的层标识"""

    SYSTEM = "system"
    ADAPTER = "adapter"
    INSTANCE = "instance"
    CHANNEL = "channel"


@dataclass
class ResolvedConfigLayers:
    """一次解析所需的全部覆盖层

    合并与溯源共用同一份结果，避免为了拿层信息重复查库、重复读盘。
    """

    chat_key: str
    adapter_key: str
    instance_key: str
    # 生效的覆盖层，按优先级从高到低排列（频道 > 实例 > 适配器）
    overrides: List[Tuple[ConfigLayer, ConfigBase]] = field(default_factory=list)


class EffectiveConfigFieldTrace(BaseModel):
    """单个配置字段的有效值与来源"""

    key: str
    title: str
    value: Any = None
    source: ConfigLayer
    overridable: bool
    # 仅在该字段确实被某一层覆盖时给出，包含全局值与每个设置了该字段的覆盖层的值
    layer_values: Optional[Dict[str, Any]] = None


class EffectiveConfigTrace(BaseModel):
    """频道有效配置溯源结果"""

    chat_key: str
    adapter_key: str
    instance_key: str
    version: int
    # 本次参与合并的覆盖层，优先级从高到低
    active_layers: List[ConfigLayer]
    items: List[EffectiveConfigFieldTrace]


class ConfigResolver:
    """配置解析器

    按 频道 > 实例 > 适配器 > 全局 的优先级解析最终生效的配置。
    实例层用于同一适配器下多账号部署时按账号区分配置，单实例部署不会进入该层。
    """

    def __init__(self) -> None:
        self._version: int = 0
        # 适配器是否存在实例层的判定结果缓存，随版本号一起失效
        self._instance_layer_present: Dict[str, bool] = {}
        self._instance_layer_scanned_version: int = -1

    def current_version(self) -> int:
        """当前配置版本号

        任意一层覆盖配置（含全局配置）被写入后版本号都会自增。缓存合并结果的调用方
        （如 `DBChatChannel._effective_config`）应连同版本号一并缓存，
        取用时版本号不一致即丢弃缓存重新解析，从而避免"改了配置不生效"。
        """
        return self._version

    def bump_version(self) -> int:
        """使所有已缓存的有效配置失效

        Returns:
            int: 自增后的版本号
        """
        self._version += 1
        return self._version

    async def get_effective_config(self, chat_key: str, instance_key: Optional[str] = None) -> CoreConfig:
        """获取指定频道的最终有效配置

        Args:
            chat_key: 频道标识
            instance_key: 频道所属的适配器实例标识。调用方已持有频道记录时应直接传入
                （单实例部署传空字符串），可完全省去反查实例标识的数据库查询；
                传 None 表示未知，由解析器按需反查。

        Returns:
            CoreConfig: 一个包含已解析配置的新 CoreConfig 实例
        """
        layers = await self._resolve_layers(chat_key, instance_key)
        return CoreConfig(**self._merge(layers.overrides))

    def get_instance_effective_config(self, adapter_key: str, instance_key: str) -> CoreConfig:
        """获取某个**实例**上生效的配置（实例 > 适配器 > 全局，不含频道层）

        与 `get_effective_config` 的差别只在入口：那一条从 `chat_key` 出发，必须先有一个频道；
        而掉线告警这类"实例级但与任何频道无关"的判定手上只有 `(adapter_key, instance_key)`——
        实例掉线时它连一条频道都不需要存在。

        合并规则与 `get_effective_config` 共用 `_merge()`，不另写一套：两份实现会各自漂移，
        而漂移出来的那一半（比如"实例层的静默时段不生效"）没有任何运行时信号。

        Args:
            adapter_key: 适配器标识
            instance_key: 实例标识

        Returns:
            CoreConfig: 该实例上生效的配置副本
        """
        return CoreConfig(**self._merge(self._instance_layers(adapter_key, instance_key)))

    def instance_field_source(self, adapter_key: str, instance_key: str, field_name: str) -> Optional[ConfigLayer]:
        """某个字段在实例视角下由**哪一层**覆盖决定；没有覆盖层命中时返回 None（= 全局层）

        调用方拿到最终值之后往往还要知道「这一份是谁填的」，因为写入权限是按层分的：
        `system` / `adapter_*` 只有平台方写得进，实例 / 频道层租户写得进。
        拿最终值与全局值做字符串比较来推断来源是不成立的 —— 两者相等时它会把一次真实的
        覆盖判成"来自全局"，而这类判定的下游通常是一条安全判据（例如告警 Webhook 的
        出网校验该用哪一档）。

        与 `get_instance_effective_config` 共用 `_instance_layers()` 与同一条优先级顺序，
        因此"取值来自哪一层"与"取值是多少"不可能分叉。

        Args:
            adapter_key: 适配器标识
            instance_key: 实例标识
            field_name: `CoreConfig` 上的字段名

        Returns:
            Optional[ConfigLayer]: 命中的覆盖层；无覆盖返回 None
        """
        for layer, overrides in self._instance_layers(adapter_key, instance_key):
            if getattr(overrides, f"enable_{field_name}", False):
                return layer
        return None

    def _merge(self, overrides: List[Tuple[ConfigLayer, ConfigBase]]) -> Dict[str, Any]:
        """全局配置为基础，按优先级从高到低套用各层覆盖"""
        effective_config_data = system_config.model_dump()
        for field_name in effective_config_data:
            for _layer, layer_overrides in overrides:
                if getattr(layer_overrides, f"enable_{field_name}", False):
                    effective_config_data[field_name] = getattr(layer_overrides, field_name)
                    break
        return effective_config_data

    def _instance_layers(self, adapter_key: str, instance_key: str) -> List[Tuple[ConfigLayer, ConfigBase]]:
        """实例视角命中的覆盖层，按优先级从高到低（实例 > 适配器）"""
        overrides: List[Tuple[ConfigLayer, ConfigBase]] = []

        if instance_key and self._has_instance_layer(adapter_key):
            try:
                instance_config_key = instance_override_config_key(adapter_key, instance_key)
            except ValueError as e:
                # 标识非法说明数据不可信，跳过实例层而不是让整个解析失败（与 `_resolve_layers` 同款）
                logger.warning(f"实例层配置键构造失败，跳过实例层: {adapter_key}/{instance_key}, 错误: {e}")
            else:
                instance_overrides = UnifiedConfigService._get_config_instance(instance_config_key)  # noqa: SLF001
                if isinstance(instance_overrides, OverridableConfig):
                    overrides.append((ConfigLayer.INSTANCE, instance_overrides))

        adapter_overrides = UnifiedConfigService._get_config_instance(f"{ADAPTER_OVERRIDE_KEY_PREFIX}{adapter_key}")  # noqa: SLF001
        if isinstance(adapter_overrides, OverridableConfig):
            overrides.append((ConfigLayer.ADAPTER, adapter_overrides))

        return overrides

    async def explain_effective_config(self, chat_key: str, instance_key: Optional[str] = None) -> EffectiveConfigTrace:
        """解析频道有效配置并给出每个字段的来源

        四层叠加后单看最终值无法判断问题出在哪一层，本方法按字段给出最终值、
        命中的层，以及该字段在每一层上的取值。

        Args:
            chat_key: 频道标识
            instance_key: 频道所属的适配器实例标识，语义同 `get_effective_config`

        Returns:
            EffectiveConfigTrace: 溯源结果
        """
        layers = await self._resolve_layers(chat_key, instance_key)

        # 统一用 JSON 模式导出，保证嵌套模型（如模型组）在接口上是可序列化的原生结构
        system_values: Dict[str, Any] = system_config.model_dump(mode="json")
        layer_dumps: List[Tuple[ConfigLayer, ConfigBase, Dict[str, Any]]] = [
            (layer, overrides, overrides.model_dump(mode="json")) for layer, overrides in layers.overrides
        ]

        items: List[EffectiveConfigFieldTrace] = []
        for field_name, field_info in CoreConfig.model_fields.items():
            extra = field_info.json_schema_extra if isinstance(field_info.json_schema_extra, dict) else {}
            source = ConfigLayer.SYSTEM
            value: Any = system_values.get(field_name)
            layer_values: Dict[str, Any] = {}

            for layer, overrides, dumped in layer_dumps:
                if not getattr(overrides, f"enable_{field_name}", False):
                    continue
                layer_values[layer.value] = dumped.get(field_name)
                if source is ConfigLayer.SYSTEM:  # 层已按优先级排序，首个命中即最终来源
                    source = layer
                    value = dumped.get(field_name)

            if layer_values:
                layer_values[ConfigLayer.SYSTEM.value] = system_values.get(field_name)

            items.append(
                EffectiveConfigFieldTrace(
                    key=field_name,
                    title=CoreConfig.get_field_title(field_name) or field_name,
                    value=value,
                    source=source,
                    overridable=bool(extra.get("overridable")),
                    layer_values=layer_values or None,
                ),
            )

        return EffectiveConfigTrace(
            chat_key=layers.chat_key,
            adapter_key=layers.adapter_key,
            instance_key=layers.instance_key,
            version=self._version,
            active_layers=[layer for layer, _ in layers.overrides],
            items=items,
        )

    async def inherited_values(self, config_key: str) -> Optional[Dict[str, Any]]:
        """某个覆盖层**之上**（优先级更低的各层）此刻真正生效的取值

        覆盖开关关着的时候，该层文件里存的那份值不参与任何解析，把它当成"当前值"回给界面
        会误导用户——历史上创建覆盖文件会把当时的默认值全量冻结进去，源码默认值改过之后
        这份快照就是错的。作用域配置页改为显示本方法给出的继承值，用户勾上开关时钉住的
        就是他看到的那个值。

        Args:
            config_key: 覆盖层的配置键（频道 / 实例 / 适配器三种前缀之一）

        Returns:
            按字段名给出的继承值；`config_key` 不是覆盖层配置时返回 None
        """
        overrides: List[Tuple[ConfigLayer, ConfigBase]] = []

        if config_key.startswith(CHANNEL_OVERRIDE_KEY_PREFIX):
            layers = await self._resolve_layers(config_key[len(CHANNEL_OVERRIDE_KEY_PREFIX) :], None)
            overrides = [item for item in layers.overrides if item[0] is not ConfigLayer.CHANNEL]
        elif config_key.startswith(INSTANCE_OVERRIDE_KEY_PREFIX):
            adapter_key = config_key[len(INSTANCE_OVERRIDE_KEY_PREFIX) :].partition(INSTANCE_OVERRIDE_KEY_SEP)[0]
            adapter_overrides = UnifiedConfigService._get_config_instance(f"{ADAPTER_OVERRIDE_KEY_PREFIX}{adapter_key}")  # noqa: SLF001
            if isinstance(adapter_overrides, OverridableConfig):
                overrides = [(ConfigLayer.ADAPTER, adapter_overrides)]
        elif not config_key.startswith(ADAPTER_OVERRIDE_KEY_PREFIX):
            return None
        # 适配器层之上只剩全局配置，overrides 保持为空

        values: Dict[str, Any] = {name: getattr(system_config, name) for name in CoreConfig.model_fields}
        for name in values:
            for _layer, layer_overrides in overrides:
                if getattr(layer_overrides, f"enable_{name}", False):
                    values[name] = getattr(layer_overrides, name)
                    break
        return values

    async def _resolve_layers(self, chat_key: str, instance_key: Optional[str]) -> ResolvedConfigLayers:
        """加载该频道命中的全部覆盖层，按优先级从高到低排列"""
        # 通过别名感知的解析获取 adapter_key（部分适配器的 chat_key 短前缀 != adapter_key）
        from nekro_agent.adapters import resolve_adapter_key_from_chat_key

        adapter_key = resolve_adapter_key_from_chat_key(chat_key)

        resolved_instance_key = ""
        if self._has_instance_layer(adapter_key):
            resolved_instance_key = instance_key if instance_key is not None else await self._query_instance_key(chat_key)

        result = ResolvedConfigLayers(chat_key=chat_key, adapter_key=adapter_key, instance_key=resolved_instance_key)

        # 使用 UnifiedConfigService 加载各层覆盖配置，这会利用缓存或从文件动态加载
        channel_overrides = UnifiedConfigService._get_config_instance(f"{CHANNEL_OVERRIDE_KEY_PREFIX}{chat_key}")  # noqa: SLF001
        if isinstance(channel_overrides, OverridableConfig):
            result.overrides.append((ConfigLayer.CHANNEL, channel_overrides))

        # 实例层与适配器层与「实例视角」共用同一条装配（`_instance_layers`）：
        # 两处各写一份的话，只在其中一条路上生效的覆盖没有任何运行时信号
        result.overrides.extend(self._instance_layers(adapter_key, resolved_instance_key))

        return result

    def _has_instance_layer(self, adapter_key: str) -> bool:
        """该适配器下是否配置过实例级覆盖

        实例目录不存在即说明没有任何实例级覆盖，此时整条实例层链路（含反查实例标识的
        数据库查询）都可以跳过，单实例部署因而与引入实例层之前完全等价。
        判定结果随配置版本号缓存，写入实例配置后自然失效。
        """
        if self._instance_layer_scanned_version != self._version:
            self._instance_layer_present.clear()
            self._instance_layer_scanned_version = self._version

        present = self._instance_layer_present.get(adapter_key)
        if present is None:
            present = instance_override_root_dir(adapter_key).is_dir()
            self._instance_layer_present[adapter_key] = present
        return present

    async def _query_instance_key(self, chat_key: str) -> str:
        """反查频道所属的适配器实例标识

        实例标识直接取自频道记录，不解析 chat_key 字符串：各适配器的 chat_key 组成方式不同，
        查库是唯一可靠的来源。每次解析最多查询一次，结果由 `ResolvedConfigLayers` 承载，
        合并与溯源共用，不会退化成按字段查询。
        """
        from nekro_agent.models.db_chat_channel import DBChatChannel

        try:
            channel_query = DBChatChannel.filter(chat_key=chat_key).limit(1)
            instance_keys: List[Any] = await channel_query.values_list("instance_key", flat=True)
        except Exception as e:
            # 数据库不可用时退化为不启用实例层，而不是让整个配置解析失败
            logger.warning(f"反查频道实例标识失败，跳过实例层: {chat_key}, 错误: {e}")
            return ""

        return str(instance_keys[0]) if instance_keys else ""


# 创建配置解析器单例
config_resolver = ConfigResolver()
