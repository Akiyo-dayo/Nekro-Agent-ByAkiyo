"""开 `ENABLE_OPENAPI_DOCS` 时 `/api/openapi.json` 必须能生成（裸机部署验收发现，round-27 第二轮）

`ExtraField(required=True)` 经 `json_schema_extra` 被原样写进**属性级** JSON Schema，而 JSON Schema 里
`required` 是对象级的字符串数组：属性上出现 `required: true / false` 让 FastAPI 的 OpenAPI 校验
（`fastapi.openapi.models.OpenAPI`）直接拒绝，`/api/openapi.json` 返回 400，`/api/docs` 打不开。
`ModelConfigGroup.API_KEY` 因被接口作为响应模型引用，是第一个撞上的。

修法在源头（`ExtraField.model_dump`）：落成 `json_schema_extra` 字典时把 `required` 换名为 `x-field-required`
扩展键（全仓 180 多处 `ExtraField(...).model_dump()` 都经过它，含适配器 / 插件配置和邮箱账户这类嵌套的普通 BaseModel）。
读取方 `config_service._extract_extra_fields` 再换回来，WebUI 看到的配置项里仍是 `required`。

本文件三层把关：
1. 真路径：在真实路由表上开文档、请求 `/api/openapi.json`，要 200；
2. 独立于 FastAPI 的校验：每个 components.schemas 条目过 JSON Schema 2020-12 元模式；
3. 单元：全部 BaseModel 子类的字段扩展里不再有属性级的非数组 `required`，且 WebUI 要的必填信息没丢。
"""

from typing import Any, Dict, Iterator, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from nekro_agent.core.config import CoreConfig, ModelConfigGroup
from nekro_agent.core.os_env import OsEnv


@pytest.fixture(scope="module")
def docs_app() -> Iterator[FastAPI]:
    """用真实路由表、开着文档挂载一份 FastAPI（挂载一次，模块内复用）"""
    from nekro_agent.routers import mount_api_routes

    previous = OsEnv.ENABLE_OPENAPI_DOCS
    OsEnv.ENABLE_OPENAPI_DOCS = True  # type: ignore[misc]
    try:
        app = FastAPI()
        mount_api_routes(app)
        yield app
    finally:
        OsEnv.ENABLE_OPENAPI_DOCS = previous  # type: ignore[misc]


def _walk_schemas(node: Any, path: str = "") -> Iterator[Tuple[str, Dict[str, Any]]]:
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk_schemas(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_schemas(value, f"{path}/{index}")


def _bad_property_level_required(schema: Dict[str, Any]) -> List[str]:
    """属性级（`properties.<名字>` 下）出现的非数组 `required`"""
    offenders: List[str] = []
    for path, node in _walk_schemas(schema):
        properties = node.get("properties")
        if not isinstance(properties, dict):
            continue
        for name, prop in properties.items():
            if isinstance(prop, dict) and "required" in prop and not isinstance(prop["required"], list):
                offenders.append(f"{path}/properties/{name}: required={prop['required']!r}")
    return offenders


class TestOpenApiDocumentIsServed:
    def test_openapi_json_returns_200_with_docs_enabled(self, docs_app: FastAPI) -> None:
        response = TestClient(docs_app).get("/api/openapi.json")

        assert response.status_code == 200, response.text[:800]
        document = response.json()
        assert document["openapi"].startswith("3.")
        assert document["paths"], "文档里一条路径都没有"

    def test_every_component_schema_is_a_valid_json_schema(self, docs_app: FastAPI) -> None:
        """不依赖 FastAPI 自己的校验：每个组件 schema 都要通过 JSON Schema 2020-12 的元模式"""
        document = TestClient(docs_app).get("/api/openapi.json").json()
        schemas: Dict[str, Any] = document["components"]["schemas"]
        assert "ModelConfigGroup" in schemas, "前置对照：撞上这个问题的模型确实在文档里"

        invalid: List[str] = []
        for name, schema in schemas.items():
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as error:  # noqa: BLE001 - 收集全部再一起报
                invalid.append(f"{name}: {str(error).splitlines()[0]}")
        assert not invalid, invalid

    def test_the_documented_model_group_still_carries_the_field_metadata(self, docs_app: FastAPI) -> None:
        """修法不是把元数据整个删掉：API_KEY 仍带着 is_secret 等扩展，必填标记改记在扩展键下"""
        document = TestClient(docs_app).get("/api/openapi.json").json()
        api_key = document["components"]["schemas"]["ModelConfigGroup"]["properties"]["API_KEY"]

        assert api_key.get("is_secret") is True
        assert "required" not in api_key or isinstance(api_key["required"], list)
        assert api_key.get("x-field-required") is False

    def test_docs_are_not_mounted_by_default(self) -> None:
        """反向对照：不开文档时不挂 /api/openapi.json（默认关闭的行为不变）"""
        from nekro_agent.routers import mount_api_routes

        previous = OsEnv.ENABLE_OPENAPI_DOCS
        OsEnv.ENABLE_OPENAPI_DOCS = False  # type: ignore[misc]
        try:
            app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
            mount_api_routes(app)
            assert TestClient(app).get("/api/openapi.json").status_code == 404
        finally:
            OsEnv.ENABLE_OPENAPI_DOCS = previous  # type: ignore[misc]


class TestExtraFieldNeverEmitsAPropertyLevelRequiredKeyword:
    def test_dump_renames_the_colliding_key(self) -> None:
        from nekro_agent.core.core_utils import ExtraField

        dumped = ExtraField(required=True).model_dump()

        assert dumped["x-field-required"] is True
        assert "required" not in dumped

    def test_default_dump_carries_the_flag_too(self) -> None:
        """默认值 False 也会被 dump 出来——历史上撞上的恰恰是几百个 `required: false`"""
        from nekro_agent.core.core_utils import ExtraField

        assert ExtraField().model_dump()["x-field-required"] is False

    def test_other_keys_are_untouched(self) -> None:
        from nekro_agent.core.core_utils import ExtraField

        dumped = ExtraField(is_secret=True, placeholder="p").model_dump()

        assert dumped["is_secret"] is True and dumped["placeholder"] == "p"
        assert set(dumped) == (set(ExtraField.model_fields) - {"required"}) | {"x-field-required"}

    def test_no_pydantic_model_in_the_codebase_leaves_a_non_list_required_in_its_field_extras(self) -> None:
        """全仓扫描：所有 BaseModel 子类（含嵌套的、不是 ConfigBase 的）字段上的 json_schema_extra"""
        from pydantic import BaseModel

        import nekro_agent.adapters  # noqa: F401 - 让适配器配置类被导入
        import nekro_agent.routers  # noqa: F401

        seen = set()
        stack = list(BaseModel.__subclasses__())
        offenders: List[str] = []
        scanned_with_extras = 0
        while stack:
            cls = stack.pop()
            if cls in seen:
                continue
            seen.add(cls)
            stack.extend(cls.__subclasses__())
            for name, field in getattr(cls, "model_fields", {}).items():
                extra = field.json_schema_extra
                if isinstance(extra, dict):
                    scanned_with_extras += 1
                    if "required" in extra and not isinstance(extra["required"], list):
                        offenders.append(f"{cls.__module__}.{cls.__name__}.{name}")
        assert scanned_with_extras > 100, "扫描范围不对：带扩展元数据的字段太少"
        assert not offenders, offenders[:10]

    def test_config_json_schemas_have_no_property_level_required(self) -> None:
        assert _bad_property_level_required(CoreConfig.model_json_schema()) == []
        assert _bad_property_level_required(ModelConfigGroup.model_json_schema()) == []

    def test_nested_models_are_clean_too(self) -> None:
        """邮箱账户是嵌套的普通 BaseModel（不是 ConfigBase）：只改配置基类的做法漏过它，源头换名天然覆盖"""
        from nekro_agent.adapters.email.config import EmailConfig

        assert _bad_property_level_required(EmailConfig.model_json_schema()) == []

    def test_the_webui_still_sees_required_under_its_original_name(self) -> None:
        """WebUI 的必填校验读的是配置项里的 `required`：`_extract_extra_fields` 换回原名"""
        from nekro_agent.services.config_service import _extract_extra_fields

        extra = CoreConfig.model_fields["USE_MODEL_GROUP"].json_schema_extra
        assert isinstance(extra, dict)
        assert _extract_extra_fields(extra)["required"] is True
        assert "x-field-required" not in _extract_extra_fields(extra)

    def test_the_config_item_api_shape_is_unchanged(self) -> None:
        """端到端到配置项列表：必填的项仍带 `required: True`，不必填的是 False，换名后的键不外泄"""
        from nekro_agent.services.config_service import UnifiedConfigService

        items = {item["key"]: item for item in UnifiedConfigService.get_config_list("system")}
        assert items["USE_MODEL_GROUP"].get("required") is True
        assert items["MODEL_GROUPS"].get("required") is False
        assert not any("x-field-required" in item for item in items.values())

    def test_object_level_required_lists_are_untouched(self) -> None:
        """对象级的 `required: [...]`（JSON Schema 本来的含义）一个字都不能动"""
        schema = ModelConfigGroup.model_json_schema()
        assert isinstance(schema.get("required", []), list)


class TestLiteralRequiredKeyIsNotDropped:
    """字面量 `"required": True` 不能被丢弃（round-27 第三轮复核 P2-4）

    换名之后 `_extract_extra_fields` 只认 `x-field-required`：第三方插件（以及内置的 `email_utils`）手写的
    `json_schema_extra={"required": True}` 被丢掉，前端不再做必填校验；同时这种字面量同样会把
    属性级 `required: true` 写进 JSON Schema，与 `ExtraField` 要避开的是同一个问题。
    修法两处：内置插件改用 `ExtraField`；提取回退读原键（兼容第三方插件，WebUI 照常看到 `required`）。
    """

    def test_extract_falls_back_to_the_original_key(self) -> None:
        from nekro_agent.services.config_service import _extract_extra_fields

        assert _extract_extra_fields({"required": True, "ref_model_groups": True})["required"] is True
        assert _extract_extra_fields({"required": False})["required"] is False

    def test_the_renamed_key_wins_when_both_are_present(self) -> None:
        from nekro_agent.services.config_service import _extract_extra_fields

        assert _extract_extra_fields({"x-field-required": True, "required": False})["required"] is True

    def test_nothing_is_invented_when_neither_key_is_present(self) -> None:
        from nekro_agent.services.config_service import _extract_extra_fields

        assert "required" not in _extract_extra_fields({"placeholder": "p"})

    def test_email_utils_default_model_group_is_still_required_in_the_webui_shape(self) -> None:
        from nekro_agent.services.config_service import _extract_extra_fields
        from plugins.builtin.email_utils import EmailUtilsConfig

        extra = EmailUtilsConfig.model_fields["DEFAULT_SUMMARY_MODEL_GROUP"].json_schema_extra
        assert isinstance(extra, dict)
        assert "required" not in extra, "内置插件仍在手写字面量 required，会泄进属性级 JSON Schema"
        extracted = _extract_extra_fields(extra)
        assert extracted["required"] is True
        assert extracted["ref_model_groups"] is True and extracted["model_type"] == "chat"

    def test_no_source_file_hand_writes_a_literal_required_in_a_dict(self) -> None:
        """源码扫描：全仓（`nekro_agent/` 与 `plugins/`）不再有手写的 `{"required": <bool>}` 字典项"""
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders: List[str] = []
        for base in ("nekro_agent", "plugins"):
            for path in (root / base).rglob("*.py"):
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (SyntaxError, UnicodeDecodeError):
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Dict):
                        continue
                    for key, value in zip(node.keys, node.values):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "required"
                            and isinstance(value, ast.Constant)
                            and isinstance(value.value, bool)
                        ):
                            offenders.append(f"{path.relative_to(root)}:{node.lineno}")
        assert not offenders, offenders