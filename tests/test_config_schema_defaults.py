"""配置页结构（`field_schema`）里的默认值必须能 JSON 化

`/api/config/list/{key}` 把嵌套模型（列表、字典里的对象）的字段结构原样放进响应。pydantic v2 里
没有默认值的字段——必填的、用 `default_factory` 的——`field.default` 都是 `PydanticUndefined`，
它曾原样进了结构，整页序列化失败成 500：邮件适配器（`EmailAccount.SEND_SHARE_INSTANCES`）与
表情包插件（`PersonaSetRule.SET_IDS`）的配置页都打不开。

要钉住的事：

1. 核心、每个适配器、每个内置插件的配置模型，连同它们嵌套的模型，`_get_model_schema` 的结果都能被
   标准 `json` 序列化；每个配置的配置列表都能按接口的响应模型序列化；
2. 默认值的取法：必填为 None；内置空容器工厂给一个新的空容器；别的工厂不调用（可能有副作用）、给 None；
   普通默认值转成 JSON 能表示的形态，转不了的给 None；
3. 前端「列表里新增一项」按 `default ?? 按类型的空值` 预填（`ConfigTable.tsx` 的
   `getDefaultValueForType`），这样拼出来的新对象嵌套模型要收——保存不会因为预填值被拒。
"""

from __future__ import annotations

import enum
import importlib
import inspect
import json
import pkgutil
from typing import Any, Dict, Iterator, List, Optional, Set, Type, get_args, get_origin

import pytest
from pydantic import BaseModel, Field

from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.routers.config import ConfigItem
from nekro_agent.services.config_service import ConfigService, _get_model_schema

# 与 `frontend/src/components/common/ConfigTable.tsx` 的 `getDefaultValueForType` 一致
FRONTEND_TYPE_DEFAULTS: Dict[str, Any] = {
    "bool": False,
    "boolean": False,
    "int": 0,
    "float": 0,
    "number": 0,
    "list": [],
    "dict": {},
}


def _subclasses(cls: type) -> Set[type]:
    seen: Set[type] = set()
    stack = [cls]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub not in seen:
                seen.add(sub)
                stack.append(sub)
    return seen


def _load_every_config_class() -> List[Type[ConfigBase]]:
    """导入核心、全部适配器的配置类与全部内置插件，收齐这个仓库里定义的配置类

    导入内置插件会把它们的默认配置落盘一次，测试会话的数据目录已经改到临时目录（`tests/conftest.py`）。
    """
    import plugins.builtin as builtin
    from nekro_agent.adapters import ADAPTER_REGISTRY, _import_string
    from nekro_agent.core.config import CoreConfig  # noqa: F401
    from nekro_agent.core.overridable_config import OverridableConfig  # noqa: F401

    for spec in ADAPTER_REGISTRY.values():
        _import_string(spec.config_path)
    for info in pkgutil.iter_modules(builtin.__path__):
        importlib.import_module(f"{builtin.__name__}.{info.name}")
    return sorted(
        (cls for cls in _subclasses(ConfigBase) if cls.__module__.startswith(("nekro_agent.", "plugins."))),
        key=lambda cls: (cls.__module__, cls.__qualname__),
    )


def _model_types(annotation: Any) -> Iterator[Type[BaseModel]]:
    """注解里出现的 pydantic 模型（含 `List[...]`、`Dict[str, ...]`、`Optional[...]` 里的）"""
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in get_args(annotation):
        yield from _model_types(arg)


def _with_nested(classes: List[Type[ConfigBase]]) -> List[Type[BaseModel]]:
    seen: Dict[Type[BaseModel], None] = {}

    def walk(model: Type[BaseModel]) -> None:
        if model in seen:
            return
        seen[model] = None
        for field in model.model_fields.values():
            for nested in _model_types(field.annotation):
                walk(nested)

    for cls in classes:
        walk(cls)
    return list(seen)


def _name(model: type) -> str:
    return f"{model.__module__}.{model.__qualname__}"


@pytest.fixture(scope="module")
def config_classes() -> List[Type[ConfigBase]]:
    return _load_every_config_class()


class TestEveryConfigModel:
    def test_the_scan_reaches_core_adapters_and_plugins(self, config_classes: List[Type[ConfigBase]]) -> None:
        """收齐的范围不能悄悄变小：核心两份、每个适配器的配置类、带配置的内置插件都在"""
        from nekro_agent.adapters import ADAPTER_REGISTRY, _import_string

        names = {_name(cls) for cls in config_classes}
        assert "nekro_agent.core.config.CoreConfig" in names
        assert "nekro_agent.core.overridable_config.OverridableConfig" in names
        for key, spec in ADAPTER_REGISTRY.items():
            assert _name(_import_string(spec.config_path)) in names, key
        assert "plugins.builtin.emotion.EmotionConfig" in names
        assert sum(name.startswith("plugins.builtin.") for name in names) >= 10

    def test_every_schema_is_json_serializable(self, config_classes: List[Type[ConfigBase]]) -> None:
        models = _with_nested(config_classes)
        assert len(models) > len(config_classes), "前提：嵌套的模型也收进来了"
        broken: List[str] = []
        for model in models:
            try:
                json.dumps(_get_model_schema(model))
            except (TypeError, ValueError) as exc:
                broken.append(f"{_name(model)}: {exc}")
        assert broken == []

    def test_every_config_list_serializes_like_the_api(self, config_classes: List[Type[ConfigBase]]) -> None:
        """按 `/config/list/{key}` 的响应模型逐项序列化（失败时接口就是 500）"""
        broken: List[str] = []
        for cls in config_classes:
            try:
                items = ConfigService.get_config_list(cls())
                for item in items:
                    ConfigItem(**item).model_dump_json()
            except Exception as exc:  # noqa: BLE001 - 收齐所有失败的配置类一起报
                broken.append(f"{_name(cls)}: {type(exc).__name__}: {str(exc)[:160]}")
        assert broken == []

    def test_a_new_list_item_filled_like_the_frontend_is_accepted(
        self,
        config_classes: List[Type[ConfigBase]],
    ) -> None:
        """配置页上能编辑的「对象列表」，新增一项按前端的预填拼出来，嵌套模型要收"""
        checked: List[str] = []
        rejected: List[str] = []
        for cls in config_classes:
            for key, field in cls.model_fields.items():
                extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
                if extra.get("is_hidden") or get_origin(field.annotation) is not list:
                    continue
                element = (get_args(field.annotation) or (None,))[0]
                if not (inspect.isclass(element) and issubclass(element, BaseModel)):
                    continue
                schema = _get_model_schema(element)
                item = {
                    name: info["default"]
                    if info["default"] is not None
                    else FRONTEND_TYPE_DEFAULTS.get(info["type"], "")
                    for name, info in schema.items()
                }
                checked.append(f"{_name(cls)}.{key}")
                try:
                    element.model_validate(item)
                except Exception as exc:  # noqa: BLE001
                    rejected.append(f"{_name(cls)}.{key}: {exc}")
        assert checked, "前提：至少有一个能在配置页上编辑的对象列表"
        assert rejected == []


class _Color(enum.Enum):
    RED = "red"


class _Opaque:
    """一个不能 JSON 化的默认值"""


class TestDefaults:
    def test_fields_without_a_plain_default(self) -> None:
        calls: List[int] = []

        def token() -> str:
            calls.append(1)
            return "generated"

        class Inner(BaseModel):
            name: str
            explicit: str = Field(...)
            tags: List[str] = Field(default_factory=list)
            mapping: Dict[str, int] = Field(default_factory=dict)
            secret: str = Field(default_factory=token)

        schema = _get_model_schema(Inner)
        assert (schema["name"]["default"], schema["name"]["required"]) == (None, True)
        assert (schema["explicit"]["default"], schema["explicit"]["required"]) == (None, True)
        assert (schema["tags"]["default"], schema["tags"]["type"], schema["tags"]["required"]) == ([], "list", False)
        assert (schema["mapping"]["default"], schema["mapping"]["type"]) == ({}, "dict")
        assert schema["secret"]["default"] is None
        assert calls == [], "别的工厂不调用：可能有副作用"
        json.dumps(schema)

    def test_each_call_gets_a_fresh_empty_container(self) -> None:
        class Inner(BaseModel):
            tags: List[str] = Field(default_factory=list)

        first = _get_model_schema(Inner)["tags"]["default"]
        first.append("x")
        assert _get_model_schema(Inner)["tags"]["default"] == []

    def test_plain_defaults_become_json(self) -> None:
        class Inner(BaseModel):
            color: _Color = _Color.RED
            numbers: Set[int] = {3}
            note: Optional[str] = None
            count: int = 5
            opaque: Any = _Opaque()

        schema = _get_model_schema(Inner)
        assert schema["color"]["default"] == "red"
        assert schema["numbers"]["default"] == [3]
        assert schema["note"]["default"] is None
        assert schema["count"]["default"] == 5
        assert schema["opaque"]["default"] is None
        json.dumps(schema)

    def test_the_email_adapter_page_serializes(self) -> None:
        """原来 500 的那一页：邮箱账户里用 `default_factory=list` 的字段"""
        from nekro_agent.adapters.email.config import EmailConfig

        items = {item["key"]: item for item in ConfigService.get_config_list(EmailConfig())}
        accounts = items["RECEIVE_ACCOUNTS"]
        assert accounts["field_schema"]["SEND_SHARE_INSTANCES"]["default"] == []
        for item in items.values():
            ConfigItem(**item).model_dump_json()
