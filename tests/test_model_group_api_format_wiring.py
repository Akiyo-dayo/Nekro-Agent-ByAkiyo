"""模型组「接口格式」的接线、取值范围与往返

## 这份测试钉的是什么

| 失效形态 | 表现 | 钉在哪 |
|---|---|---|
| 新加的聊天调用处忘了传接口格式 | 选了 Gemini 原生的组在这一处照样按 OpenAI 格式发，上游 404 / 400 | `test_every_chat_call_states_its_api_format` |
| 参数构造器漏了这个键 | 测试连接、重放、插件生成器按错的格式发 | `TestParamBuilders` |
| 写入时什么值都收 | 库里存进一个两条分支都不认的值，读回时静默回落成 openai | `TestOnlyTwoValuesAreAccepted` |
| 某条路径漏拷这个字段 | 经过 YAML 快照、导入、复制之后，Gemini 原生的组悄悄变回 OpenAI 兼容 | `TestRoundTrips` |
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest
import yaml
from pydantic import ValidationError
from tortoise import Tortoise

from nekro_agent.core.config import CoreConfig, ModelConfigGroup, config
from nekro_agent.models.db_model_group import DBModelGroup

REPO_ROOT = Path(__file__).resolve().parents[1]
GEMINI_GROUP = ModelConfigGroup(
    GROUP_NAME="g",
    CHAT_MODEL="gemini-test-model",
    BASE_URL="https://gemini.example.test/v1beta",
    API_KEY="",
    API_FORMAT="gemini",
)


# ---------------------------------------------------------------------------
# 静态：每一处聊天调用都说明了接口格式
# ---------------------------------------------------------------------------

_CHAT_ENTRIES = {"gen_openai_chat_response", "gen_openai_chat_stream"}
# `**` 展开的参数字典由这几个函数造，它们自己带 `api_format`（`TestParamBuilders` 核对）
_PARAM_BUILDERS = {"build_openai_model_test_params", "_build_chat_params", "_build_model_params"}


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_every_chat_call_states_its_api_format() -> None:
    missing: List[str] = []
    seen = 0
    for base in ("nekro_agent", "plugins"):
        for path in (REPO_ROOT / base).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            builder_vars = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _callee(node.value) in _PARAM_BUILDERS:
                    builder_vars.update(target.id for target in node.targets if isinstance(target, ast.Name))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _callee(node) not in _CHAT_ENTRIES:
                    continue
                seen += 1
                if any(keyword.arg == "api_format" for keyword in node.keywords):
                    continue
                spread = [keyword.value for keyword in node.keywords if keyword.arg is None]
                if any(
                    (isinstance(value, ast.Call) and _callee(value) in _PARAM_BUILDERS)
                    or (isinstance(value, ast.Name) and value.id in builder_vars)
                    for value in spread
                ):
                    continue
                missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {_callee(node)}")
    # 扫描本身没扫到调用处时这条用例就失去意义了
    assert seen >= 10
    assert missing == [], "这些聊天调用没有说明接口格式（api_format）"


class TestParamBuilders:
    def test_model_test_params(self) -> None:
        from nekro_agent.services.model_test import build_openai_model_test_params

        assert build_openai_model_test_params(GEMINI_GROUP)["api_format"] == "gemini"
        assert build_openai_model_test_params(ModelConfigGroup())["api_format"] == "openai"

    def test_debug_replay_params(self) -> None:
        from nekro_agent.services.command.built_in.debug import _build_chat_params

        assert _build_chat_params(GEMINI_GROUP, False)["api_format"] == "gemini"

    def test_plugin_generator_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.plugin.generator import _build_model_params

        monkeypatch.setattr(config, "MODEL_GROUPS", {"g": GEMINI_GROUP})
        assert _build_model_params("g")["api_format"] == "gemini"


# ---------------------------------------------------------------------------
# 只收两个值
# ---------------------------------------------------------------------------


class TestOnlyTwoValuesAreAccepted:
    @pytest.mark.parametrize("value", ["anthropic", "Gemini", "", "openai-compatible"])
    def test_config_model_rejects_other_values(self, value: str) -> None:
        with pytest.raises(ValidationError):
            ModelConfigGroup(API_FORMAT=value)

    @pytest.mark.parametrize("value", ["anthropic", "Gemini", ""])
    def test_console_write_payload_rejects_other_values(self, value: str) -> None:
        from nekro_agent.routers.console.model_groups import ModelGroupPayload

        with pytest.raises(ValidationError) as excinfo:
            ModelGroupPayload(api_format=value)
        assert "api_format" in str(excinfo.value)
        assert ModelGroupPayload(api_format="gemini").api_format == "gemini"
        assert ModelGroupPayload().api_format is None  # 不提交 = 保持原值

    def test_inline_test_and_fetch_models_reject_other_values(self) -> None:
        from nekro_agent.routers.config import FetchModelsRequest, ModelGroupInlineTestRequest

        with pytest.raises(ValidationError):
            FetchModelsRequest(base_url="https://gemini.example.test/v1beta", api_key="", api_format="anthropic")
        with pytest.raises(ValidationError):
            ModelGroupInlineTestRequest(
                group_name="g",
                chat_model="m",
                base_url="https://gemini.example.test/v1beta",
                api_key="",
                model_type="chat",
                api_format="anthropic",
            )
        assert FetchModelsRequest(base_url="https://gemini.example.test/v1beta", api_key="").api_format == "openai"


# ---------------------------------------------------------------------------
# 往返
# ---------------------------------------------------------------------------


@pytest.fixture
async def model_group_db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


class TestRoundTrips:
    def test_db_row_and_runtime_view(self) -> None:
        row = DBModelGroup(name="g", ownership="platform", visibility="private")
        row.apply_config_group(GEMINI_GROUP)
        assert row.api_format == "gemini"
        assert row.to_config_group().API_FORMAT == "gemini"

    def test_yaml_snapshot_keeps_the_field(self) -> None:
        """运行时视图落成 YAML 快照、再按快照读回来，字段不丢"""
        snapshot = CoreConfig()
        snapshot.MODEL_GROUPS = {"g": GEMINI_GROUP}
        text = yaml.safe_dump(snapshot._dump_data(), allow_unicode=True)  # noqa: SLF001
        entry = yaml.safe_load(text)["MODEL_GROUPS"]["g"]

        assert entry["API_FORMAT"] == "gemini"
        assert ModelConfigGroup.model_validate(entry).API_FORMAT == "gemini"

    def test_old_snapshot_without_the_field_reads_as_openai(self) -> None:
        entry = GEMINI_GROUP.model_dump()
        entry.pop("API_FORMAT")
        assert ModelConfigGroup.model_validate(entry).API_FORMAT == "openai"

    async def test_import_from_config_file_keeps_the_field(self, model_group_db: None) -> None:
        """配置文件里的组导入数据表（首次部署 / 用快照恢复）时带上接口格式"""
        created = await DBModelGroup.import_config_group("g", GEMINI_GROUP)
        assert created is not None

        stored = await DBModelGroup.get(name="g")
        assert stored.api_format == "gemini"
        assert stored.to_config_group().API_FORMAT == "gemini"

    async def test_unknown_stored_value_reads_back_as_openai(self, model_group_db: None) -> None:
        await DBModelGroup.import_config_group("g", GEMINI_GROUP)
        await DBModelGroup.filter(name="g").update(api_format="anthropic")

        stored = await DBModelGroup.get(name="g")
        assert stored.to_config_group().API_FORMAT == "openai"

    def test_copy_and_edit_dialog_carry_the_field(self) -> None:
        """复制 / 编辑对话框从源组回填这个字段，提交时带上：复制出来的组不会变回默认格式"""
        source = (REPO_ROOT / "frontend/src/components/model-group/ModelGroupFormDialog.tsx").read_text(encoding="utf-8")

        assert "API_FORMAT: initialConfig.API_FORMAT ?? 'openai'" in source
        assert "api_format: config.API_FORMAT ?? 'openai'" in source


# ---------------------------------------------------------------------------
# 不知道这个参数的调用方（插件市场里的插件直接调聊天入口）
# ---------------------------------------------------------------------------


class _Sentinel(Exception):
    pass


class TestCallersThatDoNotPassTheFormat:
    @pytest.fixture
    def gemini_view(self, monkeypatch: pytest.MonkeyPatch) -> None:
        groups = {
            "g": ModelConfigGroup(
                GROUP_NAME="g",
                CHAT_MODEL="gemini-test-model",
                BASE_URL="https://gemini.example.test/v1beta",
                API_KEY="test-gemini-key-do-not-leak",
                API_FORMAT="gemini",
            ),
            "o": ModelConfigGroup(
                GROUP_NAME="o",
                CHAT_MODEL="openai-test-model",
                BASE_URL="https://openai.example.test/v1",
                API_KEY="test-openai-key",
            ),
            "e": ModelConfigGroup(
                GROUP_NAME="e",
                CHAT_MODEL="embed-test-model",
                BASE_URL="https://embed.example.test/v1beta",
                API_KEY="test-embed-key",
                MODEL_TYPE="embedding",
                API_FORMAT="gemini",
            ),
        }
        monkeypatch.setattr(config, "MODEL_GROUPS", groups)

    def test_an_explicit_value_always_wins(self, gemini_view: None) -> None:
        from nekro_agent.services.agent.openai import _resolve_is_gemini

        same = ("https://gemini.example.test/v1beta", "gemini-test-model", "test-gemini-key-do-not-leak")
        assert _resolve_is_gemini("openai", *same) is False
        assert _resolve_is_gemini("anything-else", *same) is False
        assert _resolve_is_gemini(" Gemini ", "https://other.example.test", "m", "k") is True

    def test_missing_value_follows_a_matching_gemini_chat_group(self, gemini_view: None) -> None:
        from nekro_agent.services.agent.openai import _resolve_is_gemini

        assert _resolve_is_gemini(None, "https://gemini.example.test/v1beta ", "gemini-test-model", " test-gemini-key-do-not-leak")
        # 三项里任何一项对不上都按 OpenAI 兼容
        assert not _resolve_is_gemini(None, "https://gemini.example.test/v1beta", "gemini-test-model", "another-key")
        assert not _resolve_is_gemini(None, "https://gemini.example.test/v1", "gemini-test-model", "test-gemini-key-do-not-leak")
        assert not _resolve_is_gemini(None, "https://gemini.example.test/v1beta", "other-model", "test-gemini-key-do-not-leak")
        # 没切格式的组、声明了 gemini 的非聊天组都不算
        assert not _resolve_is_gemini(None, "https://openai.example.test/v1", "openai-test-model", "test-openai-key")
        assert not _resolve_is_gemini(None, "https://embed.example.test/v1beta", "embed-test-model", "test-embed-key")

    async def test_a_plugin_call_without_the_parameter_goes_native(
        self,
        gemini_view: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """插件照老写法只传地址、模型、Key：选了 Gemini 原生的组照样走原生，不会按 OpenAI 格式打过去"""
        from nekro_agent.services.agent import openai as openai_module

        seen: dict[str, Any] = {}

        class _NativeClient:
            async def post(self, url: str, **kwargs: Any) -> Any:
                seen["url"] = url
                seen["headers"] = kwargs.get("headers")
                raise _Sentinel

        def _no_sdk(**_kwargs: Any) -> Any:
            raise AssertionError("不该建 OpenAI SDK 客户端")

        monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: _NativeClient())
        monkeypatch.setattr(openai_module, "AsyncOpenAI", _no_sdk)
        monkeypatch.setattr(openai_module, "record_llm_usage", lambda **_kwargs: None)

        with pytest.raises(_Sentinel):
            await openai_module.gen_openai_chat_response(
                model="gemini-test-model",
                messages=[{"role": "user", "content": "hi"}],
                base_url="https://gemini.example.test/v1beta",
                api_key="test-gemini-key-do-not-leak",
                tenant_egress=False,
            )

        assert seen["url"].endswith("/models/gemini-test-model:generateContent")
        assert "key=" not in seen["url"]
        assert seen["headers"]["x-goog-api-key"] == "test-gemini-key-do-not-leak"
