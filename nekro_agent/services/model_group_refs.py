"""模型组被哪些配置项引用（删除前提醒）

删除模型组不会改写引用它的配置：`USE_MODEL_GROUP` 这类字段只是一个组名字符串。组没了以后，
主模型组那一支在下一轮对话上抛 KeyError，备用 / 调试迁移那两支回落到主模型组并打 WARNING
（见 `services/agent/run_agent.py`），插件里的引用各自报错。删除本身**不拦**——这里只回答
"删掉它会影响哪些配置"，供删除确认框提醒，由操作者决定是否先改配置。

## 扫哪些地方

字段一律由 `ref_model_groups` 标记枚举（`model_group_service.iter_model_group_ref_fields`），
不维护字段名单，与写入闸门、运行时兜底是同一个判据。

| 层                | 来源                                                     | 归属             |
| ----------------- | -------------------------------------------------------- | ---------------- |
| `system`          | 全局配置 `config`                                        | 平台方           |
| `adapter`         | `configs/<adapter>/overrides.yaml`                       | 平台方           |
| `instance`        | `configs/<adapter>/instances/<instance>/overrides.yaml`  | 实例所属租户     |
| `channel`         | `configs/channels/<adapter>/<chat_key>.yaml`             | 频道所属实例的租户 |
| `plugin`          | 插件全局配置                                             | 平台方           |
| `plugin_instance` | `plugin_data/<plugin>/instances/<instance>/config.yaml`  | 实例所属租户     |

覆盖层只认 `enable_X` 为真的项，与 `config_resolver` 同一口径：开关关着的值不参与解析，
算它"引用"只会制造假警报。"为真"按 pydantic 的布尔转换判（`"true"`、`1` 也算），
与运行时加载这份文件时的结论一致，手改文件写成字符串也不会漏报。
覆盖层**只读盘、不写盘**：不走 `UnifiedConfigService._get_config_instance`，
那条路径加载时会顺手 `dump_config()` 把文件写一遍。

## 作用域

- 平台方看全部六层。
- 租户只看自己实例下的三层（`instance` / `channel` / `plugin_instance`）：平台层的配置是平台方的
  情报，而平台方若用了租户自建的组，那是平台方自己要处理的事——界面因此对租户说"在你能管理的
  配置里没有发现引用"，不说"没有引用"。
- 实例归属按 `(adapter_key, instance_key)` 判，不能只看 `instance_key`：它只在适配器内唯一
  （`DBAdapterInstance` 的唯一约束），两个适配器下同名的实例可能分属两个租户，只看名字会把
  别的租户的实例覆盖和频道（连同 chat_key）报给当前租户。插件实例配置例外：它的落盘路径里
  本来就只有 `instance_key`（`services/plugin/scope.py`），同名实例共用那一份文件，这里照实按名字匹配。
- 只回答当前身份**可编辑**的组（`get_editable_group`），其余组名一律不出现在结果里，
  不给"这个组名存在、且被某处引用"这类探针。
"""

import asyncio
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple, Type

import yaml
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from nekro_agent.core.config import CHANNEL_CONFIG_DIR, CONFIG_DIR, CoreConfig, config
from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.overridable_config import OverridableConfig
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.errors import ModelGroupNotFoundError, PermissionDeniedError
from nekro_agent.schemas.tenant import CurrentIdentity
from nekro_agent.services.model_group_service import (
    get_editable_group,
    is_platform_owner,
    iter_model_group_ref_fields,
)
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.scope import PLUGIN_DATA_DIR, load_instance_config_overrides

logger = get_sub_logger("model_group")

# 每个组最多返回多少条引用。确认框只需要让人看清"有没有、大概在哪"，
# 一个组被几百个频道引用时全量回传没有意义，`total` 如实给出总数
MAX_REFERENCES_PER_GROUP = 50

_BOOL = TypeAdapter(bool)


class ModelGroupRefScope(str, Enum):
    """引用所在的配置层"""

    SYSTEM = "system"
    ADAPTER = "adapter"
    INSTANCE = "instance"
    CHANNEL = "channel"
    PLUGIN = "plugin"
    PLUGIN_INSTANCE = "plugin_instance"


class ModelGroupReference(NamedTuple):
    """一处引用"""

    scope: ModelGroupRefScope
    # system 为空；adapter 是适配器标识；instance 是 `<adapter>/<instance>`；channel 是 chat_key；
    # plugin / plugin_instance 是插件键
    target: str
    # 配置字段名与标题（标题用于界面展示；`i18n_title` 有值时界面按语言取）
    field: str
    title: str
    # 仅 plugin_instance 有值：插件在哪个实例上的配置
    instance_key: str = ""
    i18n_title: Optional[Dict[str, str]] = None


class ModelGroupReferenceReport(NamedTuple):
    """一个组的引用情况；`references` 最多 `MAX_REFERENCES_PER_GROUP` 条，`total` 为总数"""

    name: str
    total: int
    references: List[ModelGroupReference]


class _RefField(NamedTuple):
    name: str
    title: str
    i18n_title: Optional[Dict[str, str]]


def _ref_fields(config_cls: Type[ConfigBase]) -> List[_RefField]:
    fields: List[_RefField] = []
    for ref in iter_model_group_ref_fields(config_cls):
        extra = config_cls.model_fields[ref.name].json_schema_extra
        i18n_title = extra.get("i18n_title") if isinstance(extra, dict) else None
        fields.append(
            _RefField(
                name=ref.name,
                title=config_cls.get_field_title(ref.name) or ref.name,
                i18n_title=i18n_title if isinstance(i18n_title, dict) else None,
            ),
        )
    return fields


def _reference(scope: ModelGroupRefScope, target: str, field: _RefField, instance_key: str = "") -> ModelGroupReference:
    return ModelGroupReference(scope, target, field.name, field.title, instance_key, field.i18n_title)


def _is_enabled(value: Any) -> bool:
    """覆盖开关是否打开，按 pydantic 的布尔转换判（与运行时加载这份文件的结论一致）"""
    try:
        return _BOOL.validate_python(value)
    except PydanticValidationError:
        return False


def _read_yaml_mapping(path: Path) -> Dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - 一个坏文件不该让整个提醒失败
        logger.warning(f"读取覆盖配置失败，跳过该文件的引用检查: {path}")
        return {}
    return raw if isinstance(raw, dict) else {}


class _TenantScope(NamedTuple):
    """租户可见的范围；平台方不受限，用 None 表示"""

    instances: Set[Tuple[str, str]]  # (adapter_key, instance_key)
    chat_keys: Set[str]

    @property
    def instance_keys(self) -> Set[str]:
        return {instance_key for _, instance_key in self.instances}


async def _tenant_scope(tenant_id: Optional[int]) -> _TenantScope:
    instances = set(
        await DBAdapterInstance.filter(tenant_id=tenant_id).values_list("adapter_key", "instance_key"),
    )
    chat_keys: Set[str] = set()
    if instances:
        rows = await DBChatChannel.filter(
            instance_key__in=sorted({instance_key for _, instance_key in instances}),
        ).values_list("adapter_key", "instance_key", "chat_key")
        chat_keys = {
            chat_key for adapter_key, instance_key, chat_key in rows if (adapter_key, instance_key) in instances
        }
    return _TenantScope(instances=instances, chat_keys=chat_keys)


def _scan_disk(
    wanted: Set[str],
    override_fields: List[_RefField],
    plugin_fields: Dict[str, List[_RefField]],
    scope: Optional[_TenantScope],
) -> List[Tuple[str, ModelGroupReference]]:
    """扫覆盖层文件（同步，放在线程里跑）；`scope` 为 None 表示平台方、不受限"""
    hits: List[Tuple[str, ModelGroupReference]] = []

    def scan_override(path: Path, layer: ModelGroupRefScope, target: str) -> None:
        raw = _read_yaml_mapping(path)
        for field in override_fields:
            value = raw.get(field.name)
            if isinstance(value, str) and value in wanted and _is_enabled(raw.get(f"enable_{field.name}")):
                hits.append((value, _reference(layer, target, field)))

    if scope is None:
        for path in sorted(CONFIG_DIR.glob("*/overrides.yaml")):
            scan_override(path, ModelGroupRefScope.ADAPTER, path.parent.name)

    # configs/<adapter>/instances/<instance>/overrides.yaml
    for path in sorted(CONFIG_DIR.glob("*/instances/*/overrides.yaml")):
        adapter_key, instance_key = path.parents[2].name, path.parent.name
        if scope is None or (adapter_key, instance_key) in scope.instances:
            scan_override(path, ModelGroupRefScope.INSTANCE, f"{adapter_key}/{instance_key}")

    for path in sorted(CHANNEL_CONFIG_DIR.glob("*/*.yaml")):
        chat_key = path.stem
        if scope is None or chat_key in scope.chat_keys:
            scan_override(path, ModelGroupRefScope.CHANNEL, chat_key)

    instance_keys = None if scope is None else scope.instance_keys
    for plugin_key, fields in plugin_fields.items():
        for path in sorted((PLUGIN_DATA_DIR / plugin_key / "instances").glob("*/config.yaml")):
            instance_key = path.parent.name
            if instance_keys is not None and instance_key not in instance_keys:
                continue
            overrides = load_instance_config_overrides(plugin_key, instance_key)
            for field in fields:
                value = overrides.get(field.name)
                if isinstance(value, str) and value in wanted:
                    hits.append(
                        (value, _reference(ModelGroupRefScope.PLUGIN_INSTANCE, plugin_key, field, instance_key))
                    )
    return hits


async def find_model_group_references(
    names: Sequence[str],
    identity: CurrentIdentity,
) -> List[ModelGroupReferenceReport]:
    """列出这些模型组被哪些配置项引用

    只对当前身份可编辑的组作答，不可编辑 / 不可见 / 不存在的组名直接略过（理由见模块文档）。
    返回顺序与入参一致（去重后）。
    """
    editable: List[str] = []
    for name in dict.fromkeys(names):
        try:
            row = await get_editable_group(name, identity)
        except (ModelGroupNotFoundError, PermissionDeniedError):
            continue
        editable.append(row.name)
    if not editable:
        return []

    wanted = set(editable)
    found: Dict[str, List[ModelGroupReference]] = {name: [] for name in editable}

    platform = is_platform_owner(identity)
    scope = None if platform else await _tenant_scope(identity.tenant_id)

    if platform:
        for field in _ref_fields(CoreConfig):
            value = getattr(config, field.name, None)
            if isinstance(value, str) and value in wanted:
                found[value].append(_reference(ModelGroupRefScope.SYSTEM, "", field))

    plugin_fields: Dict[str, List[_RefField]] = {}
    for plugin in plugin_collector.get_all_plugins():
        if plugin.config_class is ConfigBase:
            continue
        fields = _ref_fields(plugin.config_class)
        if not fields:
            continue
        plugin_fields[plugin.key] = fields
        if not platform:
            continue
        try:
            global_config = plugin.get_global_config()
        except Exception:  # noqa: BLE001 - 一个插件配置读不出来不该让整个提醒失败
            logger.warning(f"读取插件 {plugin.key} 的全局配置失败，跳过它的引用检查")
            continue
        for field in fields:
            value = getattr(global_config, field.name, None)
            if isinstance(value, str) and value in wanted:
                found[value].append(_reference(ModelGroupRefScope.PLUGIN, plugin.key, field))

    # 频道覆盖文件可能有上千个，读盘放到线程里，不卡事件循环
    disk_hits = await asyncio.to_thread(_scan_disk, wanted, _ref_fields(OverridableConfig), plugin_fields, scope)
    for name, reference in disk_hits:
        found[name].append(reference)

    return [
        ModelGroupReferenceReport(name=name, total=len(refs), references=refs[:MAX_REFERENCES_PER_GROUP])
        for name, refs in found.items()
    ]
