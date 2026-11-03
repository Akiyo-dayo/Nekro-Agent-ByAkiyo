"""插件实例上下文：沙盒回调与 Agent 运行都按频道所属实例解析配置

沙盒方法是独立的 HTTP 回调，Agent 也可能由定时器或其他适配器触发，两条链路都没有
OneBot 入站时写入的实例 contextvar。这里守住「调用方显式设置实例」这条路径：
插件里 `plugin.get_config().FIELD` 必须拿到该频道所属实例的覆盖值。
"""

from __future__ import annotations

import asyncio
import pickle
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, List, cast

import pytest
from pydantic import Field
from tortoise import Tortoise

from nekro_agent.core.core_utils import ConfigBase
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.agent.session_token import clear_session_token
from nekro_agent.services.plugin import scope as plugin_scope
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.schema import SandboxMethodType
from nekro_agent.services.plugin.scope import current_instance_key, instance_scope
from nekro_agent.services.sandbox.credential_guard import REDACTED_PLACEHOLDER
from nekro_agent.services.sandbox.runner import build_container_key

ADAPTER = "onebot_v11"
INSTANCE = "inst1"
OTHER_INSTANCE = "inst2"
CHAT_KEY = f"{ADAPTER}-{INSTANCE}-group_10001"
OTHER_CHAT_KEY = f"{ADAPTER}-{OTHER_INSTANCE}-group_10002"
RPC_CONTAINER = "plugin-scope-rpc-container"
OTHER_RPC_CONTAINER = "plugin-scope-rpc-container-other"
GLOBAL_SECRET = "global-secret-AAAAAAAA"
INSTANCE_SECRET = "instance-secret-BBBBBBBB"


class ProbeConfig(ConfigBase):
    FIELD: str = "global"


class _ProbePlugin(NekroPlugin):
    _Configs = ProbeConfig


probe_plugin = _ProbePlugin(
    name="实例配置探针",
    module_name="instance_scope_probe",
    description="测试用插件",
    version="0.0.1",
    author="test_scope",
    url="",
)


@probe_plugin.mount_sandbox_method(SandboxMethodType.TOOL, "read_probe_field", "读取探针插件配置字段")
async def read_probe_field(_ctx: AgentCtx) -> str:
    return probe_plugin.get_config(ProbeConfig).FIELD


class SecretProbeConfig(ConfigBase):
    API_KEY: str = Field(default=GLOBAL_SECRET, json_schema_extra={"is_secret": True})


class _SecretProbePlugin(NekroPlugin):
    _Configs = SecretProbeConfig


secret_probe_plugin = _SecretProbePlugin(
    name="实例密钥探针",
    module_name="instance_secret_probe",
    description="测试用插件",
    version="0.0.1",
    author="test_scope",
    url="",
)


@secret_probe_plugin.mount_sandbox_method(SandboxMethodType.TOOL, "read_secret_probe", "读取探针插件密钥")
async def read_secret_probe(_ctx: AgentCtx) -> str:
    return secret_probe_plugin.get_config(SecretProbeConfig).API_KEY


@secret_probe_plugin.mount_sandbox_method(SandboxMethodType.TOOL, "leak_secret_probe", "在异常里带出探针插件密钥")
async def leak_secret_probe(_ctx: AgentCtx) -> str:
    cfg = secret_probe_plugin.get_config(SecretProbeConfig)
    raise ValueError(f"bad url ?key={cfg.API_KEY}")


def _async_value(value: Any) -> Any:
    async def _inner(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _inner


@pytest.fixture
def onebot_instance() -> Iterator[Any]:
    """隔离 OneBot 的实例 contextvar：用例结束后还原，避免把上下文留给下一条用例"""
    from nekro_agent.adapters.onebot_v11.core import instance as ob_instance

    token = ob_instance._current_instance.set(None)  # noqa: SLF001
    try:
        yield ob_instance
    finally:
        ob_instance._current_instance.reset(token)  # noqa: SLF001


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[NekroPlugin]:
    """把探针插件的全局配置与数据目录都收进 tmp_path，并写好实例 inst1 的覆盖"""
    from nekro_agent.services.plugin.collector import plugin_collector

    global_config = ProbeConfig()
    monkeypatch.setattr(_ProbePlugin, "get_global_config", lambda _self: global_config)
    monkeypatch.setattr(plugin_scope, "PLUGIN_DATA_DIR", tmp_path / "plugin_data")
    plugin_scope.invalidate_scoped_config(probe_plugin.key)
    probe_plugin.get_config(ProbeConfig)
    plugin_scope.save_instance_config_overrides(probe_plugin.key, INSTANCE, {"FIELD": "inst1-value"})
    monkeypatch.setitem(plugin_collector.loaded_plugins, probe_plugin.key, probe_plugin)
    try:
        yield probe_plugin
    finally:
        plugin_scope.invalidate_scoped_config(probe_plugin.key)


@pytest.fixture
def secret_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[NekroPlugin]:
    """全局密钥与实例 inst1 的覆盖值不同，用于验证出口脱敏覆盖实例级配置"""
    from nekro_agent.services.plugin.collector import plugin_collector

    global_config = SecretProbeConfig()
    monkeypatch.setattr(_SecretProbePlugin, "get_global_config", lambda _self: global_config)
    monkeypatch.setattr(plugin_scope, "PLUGIN_DATA_DIR", tmp_path / "plugin_data")
    plugin_scope.invalidate_scoped_config(secret_probe_plugin.key)
    secret_probe_plugin.get_config(SecretProbeConfig)
    plugin_scope.save_instance_config_overrides(secret_probe_plugin.key, INSTANCE, {"API_KEY": INSTANCE_SECRET})
    monkeypatch.setitem(plugin_collector.loaded_plugins, secret_probe_plugin.key, secret_probe_plugin)
    try:
        yield secret_probe_plugin
    finally:
        plugin_scope.invalidate_scoped_config(secret_probe_plugin.key)


@pytest.fixture
async def db() -> Any:
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["nekro_agent.models"]})
    await Tortoise.generate_schemas()
    try:
        for instance_key, chat_key in ((INSTANCE, CHAT_KEY), (OTHER_INSTANCE, OTHER_CHAT_KEY)):
            await DBChatChannel.create(
                adapter_key=ADAPTER,
                instance_key=instance_key,
                channel_id=chat_key.rsplit("-", maxsplit=1)[-1],
                chat_key=chat_key,
                channel_type="group",
                data="{}",
            )
        yield
    finally:
        await Tortoise.close_connections()


async def _call_rpc(chat_key: str, container_key: str, method_name: str) -> Any:
    """经真实的 `rpc_exec` 入口调用探针的沙盒方法"""
    from nekro_agent.routers.rpc import rpc_exec
    from nekro_agent.services.sandbox.credential_guard import issue_sandbox_call_grant, revoke_sandbox_call_grant
    from tests.rpc_sandbox_client import FakeRpcRequest, request_body

    token = issue_sandbox_call_grant(
        container_key=container_key,
        from_chat_key=chat_key,
        method_names=[method_name],
    )
    try:
        request = FakeRpcRequest(request_body(method_name))
        return await rpc_exec(container_key, chat_key, request, x_rpc_token=token)  # type: ignore[arg-type]
    finally:
        revoke_sandbox_call_grant(container_key)


async def _call_probe_rpc(chat_key: str, container_key: str) -> Any:
    response = await _call_rpc(chat_key, container_key, "read_probe_field")
    assert response.headers["Run-Error"] == "False"
    return pickle.loads(response.body)


class TestCurrentInstanceKey:
    def test_no_context_reads_as_empty(self, onebot_instance: Any) -> None:
        assert current_instance_key() == ""

    def test_a_scope_sets_and_restores_the_key(self, onebot_instance: Any) -> None:
        with instance_scope("inst1"):
            assert current_instance_key() == "inst1"
        assert current_instance_key() == ""

    def test_nested_scopes_restore_the_outer_one(self, onebot_instance: Any) -> None:
        with instance_scope("a"):
            assert current_instance_key() == "a"
            with instance_scope("b"):
                assert current_instance_key() == "b"
            assert current_instance_key() == "a"
        assert current_instance_key() == ""

    def test_an_exception_still_restores_the_previous_value(self, onebot_instance: Any) -> None:
        with pytest.raises(RuntimeError):
            with instance_scope("inst1"):
                assert current_instance_key() == "inst1"
                raise RuntimeError("boom")
        assert current_instance_key() == ""

    def test_an_explicit_scope_wins_over_the_adapter_context(self, onebot_instance: Any) -> None:
        onebot_instance.set_current_instance("ob", "10001")
        assert current_instance_key() == "ob"
        with instance_scope("x"):
            assert current_instance_key() == "x"
        assert current_instance_key() == "ob"
        with instance_scope(""):
            assert current_instance_key() == "", "显式空串表示单实例 / 无实例，也要优先于适配器上下文"
        assert current_instance_key() == "ob"

    async def test_concurrent_tasks_keep_their_own_scope(self, onebot_instance: Any) -> None:
        async def worker(key: str) -> str:
            with instance_scope(key):
                await asyncio.sleep(0)
                first = current_instance_key()
                await asyncio.sleep(0)
                return f"{first}/{current_instance_key()}"

        first, second = await asyncio.gather(worker("a"), worker("b"))
        assert (first, second) == ("a/a", "b/b")


class TestTheRealRpcEntryUsesTheChannelInstance:
    async def test_an_override_reaches_the_sandbox_method(self, db: Any, probe: NekroPlugin, onebot_instance: Any) -> None:
        assert await _call_probe_rpc(CHAT_KEY, RPC_CONTAINER) == "inst1-value"
        assert current_instance_key() == ""

    async def test_a_channel_without_an_override_reads_the_global_value(self, db: Any, probe: NekroPlugin, onebot_instance: Any) -> None:
        assert await _call_probe_rpc(OTHER_CHAT_KEY, OTHER_RPC_CONTAINER) == "global"
        assert current_instance_key() == ""


class TestInstanceSecretsAreRedacted:
    """实例覆盖里的凭据必须与全局凭据一样在 RPC 出口被替换掉"""

    async def test_exception_text_hides_the_instance_override(
        self, db: Any, secret_probe: NekroPlugin, onebot_instance: Any,
    ) -> None:
        response = await _call_rpc(CHAT_KEY, RPC_CONTAINER, "leak_secret_probe")

        assert response.headers["Run-Error"] == "True"
        body = response.body.decode()
        assert INSTANCE_SECRET not in body
        assert REDACTED_PLACEHOLDER in body

    async def test_exception_text_hides_the_global_value_without_an_override(
        self, db: Any, secret_probe: NekroPlugin, onebot_instance: Any,
    ) -> None:
        response = await _call_rpc(OTHER_CHAT_KEY, OTHER_RPC_CONTAINER, "leak_secret_probe")

        assert response.headers["Run-Error"] == "True"
        body = response.body.decode()
        assert GLOBAL_SECRET not in body
        assert REDACTED_PLACEHOLDER in body

    async def test_a_returned_instance_override_is_redacted(
        self, db: Any, secret_probe: NekroPlugin, onebot_instance: Any,
    ) -> None:
        response = await _call_rpc(CHAT_KEY, RPC_CONTAINER, "read_secret_probe")

        assert response.headers["Run-Error"] == "False"
        assert pickle.loads(response.body) == REDACTED_PLACEHOLDER


def test_platform_secrets_include_already_resolved_instance_configs(secret_probe: NekroPlugin) -> None:
    """兜底：没有显式传实例的出口，进程里已经解析过的实例配置也要算进凭据集合"""
    from nekro_agent.services.sandbox.credential_guard import collect_platform_secrets

    resolved = plugin_scope.resolve_scoped_config(
        secret_probe.key,
        SecretProbeConfig,
        secret_probe.get_global_config(),
        INSTANCE,
    )
    assert resolved.API_KEY == INSTANCE_SECRET
    assert INSTANCE_SECRET in collect_platform_secrets()


class _StopAgent(RuntimeError):
    pass


async def test_run_agent_sets_the_channel_instance_scope(onebot_instance: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from nekro_agent.services.agent import run_agent as run_agent_module

    seen: List[str] = []

    async def _spy_resolve_active_plugins(*, instance_key: str = "", chat_key: str = "") -> Any:
        seen.append(current_instance_key())
        raise _StopAgent

    model_group = SimpleNamespace(CHAT_MODEL="probe-model", ENABLE_COT=False)

    async def _no_op(*_args: Any, **_kwargs: Any) -> None:
        return None

    config = SimpleNamespace(MODEL_GROUPS={"default": model_group}, USE_MODEL_GROUP="default")
    adapter = SimpleNamespace(
        chat_key_rules=["group_<id>"],
        config=SimpleNamespace(SESSION_ENABLE_AT=False),
        set_dialog_example=_async_value([]),
        get_jinja_env=_async_value(None),
        get_self_info=_async_value(SimpleNamespace(platform_name="qq", user_id="10001")),
    )
    channel = SimpleNamespace(
        chat_key=CHAT_KEY,
        channel_name="探针群",
        channel_type="group",
        adapter_key=ADAPTER,
        instance_key=INSTANCE,
        get_effective_config=_async_value(config),
        get_preset=_async_value(SimpleNamespace(id=1, name="默认人设", content="人设正文")),
    )
    ctx = SimpleNamespace(db_chat_channel=channel, adapter=adapter)

    monkeypatch.setattr(run_agent_module, "resolve_active_plugins", _spy_resolve_active_plugins)
    monkeypatch.setattr(run_agent_module, "enforce_channel_model_group_scope", _no_op)

    try:
        with pytest.raises(_StopAgent):
            await run_agent_module.run_agent(chat_key=CHAT_KEY, ctx=cast(Any, ctx))
    finally:
        clear_session_token(build_container_key(CHAT_KEY))

    assert seen == [INSTANCE]
    assert current_instance_key() == ""
