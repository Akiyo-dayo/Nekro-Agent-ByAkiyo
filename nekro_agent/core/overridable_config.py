import copy
from pathlib import Path
from typing import Any, Optional, Type

import yaml
from pydantic import Field, create_model, model_validator

from nekro_agent.core.config import CoreConfig
from nekro_agent.core.core_utils import ConfigBase, atomic_write_text


class OverridableConfigBase(ConfigBase):
    """覆盖层配置的共同行为：文件里只保留真正生效的覆盖项

    历史实现直接沿用 `ConfigBase.dump_config`，创建覆盖文件时会把**全部**可覆盖字段
    连同当时的默认值一起写盘。这份快照不参与任何解析（`enable_` 为 false 时
    `config_resolver` 不采用它），却会随源码默认值变更而变成陈旧数据：实测有频道
    override 文件存着 `AFFECTION_ENABLE_SYSTEM: false`，而源码默认值早已是 true。
    作用域配置页把这份快照当作"当前值"显示，用户一勾启用覆盖就把旧值钉死了。

    这里从两侧把快照消掉：

    - **写**：只输出 `enable_X` 为 true 的那些 `enable_X` / `X` 对，其余字段完全不落盘；
    - **读**：`enable_X` 为 false 的字段一律回落到字段默认值，不信任文件里存着的取值。

    读侧的回落让**存量文件**不需要任何迁移就立刻停止生效——真正启用的覆盖项一个都不受影响，
    被丢弃的只有从来没被采用过的冻结值。文件本身会在下一次保存时自然瘦身；
    缺字段的旧文件也能照常加载，pydantic 会用字段默认值补齐。
    """

    @model_validator(mode="after")
    def _drop_inactive_override_values(self) -> "OverridableConfigBase":
        fields = type(self).model_fields
        for name, field in fields.items():
            toggle = f"enable_{name}"
            if toggle not in fields or getattr(self, toggle, False):
                continue
            default = field.get_default(call_default_factory=True)
            if getattr(self, name, None) != default:
                # 走 object 而不是普通赋值：这里身处 mode="after" 校验器内，
                # 普通赋值一旦遇上 validate_assignment 就会递归回本校验器
                object.__setattr__(self, name, default)
        return self

    def dump_config(self, file_path: Optional[Path] = None) -> None:
        target_path = file_path or self.get_config_file_path()
        if target_path is None:
            raise ValueError(f"配置文件路径未设置，请调用 {type(self).__name__}.set_config_file_path() 或提供 file_path 参数")
        if target_path.suffix not in (".yaml", ".yml"):
            # 覆盖配置一律是 yaml；出现别的后缀说明调用方用错了路径，交回基类报错
            super().dump_config(file_path)
            return

        fields = type(self).model_fields
        dumped = self.model_dump()
        active: dict[str, Any] = {}
        for name in fields:
            if name.startswith("enable_"):
                continue
            toggle = f"enable_{name}"
            if toggle in fields and dumped.get(toggle):
                active[toggle] = dumped[toggle]
                active[name] = dumped[name]

        target_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            target_path,
            yaml.dump(data=active, default_flow_style=False, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


def create_overridable_config_model(model_name: str, source_model: Type[ConfigBase]) -> Type[ConfigBase]:
    """
    Dynamically creates a Pydantic model class containing all overridable fields
    from a source model.

    For each overridable field `field_name: T` in the source model, two fields are created:
    - `enable_field_name: bool`: A toggle to enable/disable the override.
    - `field_name: T`: The field for the override value, inheriting properties from the source.
    """
    new_fields = {}
    for name, field in source_model.model_fields.items():
        is_overridable = isinstance(field.json_schema_extra, dict) and field.json_schema_extra.get("overridable")

        if is_overridable:
            # Create a copy of the field to avoid modifying the original
            value_field = copy.deepcopy(field)

            # The title for the enable toggle
            enable_title = f"覆盖: {field.title or name}"
            new_fields[f"enable_{name}"] = (
                bool,
                Field(default=False, title=enable_title),
            )

            # The value field itself
            # Update description to clarify it's an override value
            value_field.description = f"覆盖值: {field.description}" if field.description else "此配置项的覆盖值"
            # Add a reference to the enable toggle for the frontend
            extra = value_field.json_schema_extra.copy() if isinstance(value_field.json_schema_extra, dict) else {}
            extra["enable_toggle"] = f"enable_{name}"
            value_field.json_schema_extra = extra
            new_fields[name] = (field.annotation, value_field)

    # Create the new Pydantic model
    model = create_model(model_name, **new_fields, __base__=OverridableConfigBase)

    # Set a default config key for this new model type, although it will be overridden
    model.set_config_key("overrides")

    return model


# Create the single OverridableConfig class to be used by adapters and channels
OverridableConfig = create_overridable_config_model("OverridableConfig", CoreConfig)
