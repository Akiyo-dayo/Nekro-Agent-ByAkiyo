"""沙盒 -> 平台凭据的隔离边界回归测试

保护的契约：**沙盒里跑的是模型生成的代码，租户能左右模型，所以沙盒必须被当作敌对方**。
平台方运营共用版 Bot 的同时向客户交付定制版实例，客户永远不能拿到平台方的
`MODEL_GROUPS[*].API_KEY`、数据库口令、JWT/RPC 密钥。

沙盒能触达服务端的通道只有两条，这里逐条钉死：

1. `/ext/rpc_exec` 回调 —— 身份必须来自本轮签发的许可，方法必须在本轮下发的白名单内，
   返回值与异常文本出口统一脱敏；
2. 沙盒互通目录的路径映射 —— `/app/shared/../../configs/nekro-agent.yaml` 这类写法
   在修复前会落到 NekroAgent 自己的配置文件上，必须拒绝。

特别地，`test_grant_method_names_match_generated_proxies` 锁的是「白名单从下发动作派生」
这一结构性性质：后人新增沙盒方法时不需要记得维护任何名单，也不可能因为漏改而开一个口子。
"""

import pickle
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nekro_agent.core.config import CoreConfig, ModelConfigGroup, config
from nekro_agent.core.core_utils import ConfigManager
from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.core.os_env import OsEnv
from nekro_agent.routers.rpc import router as rpc_router
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.plugin.collector import plugin_collector
from nekro_agent.services.plugin.schema import SandboxMethod, SandboxMethodType
from nekro_agent.services.sandbox import credential_guard as guard
from nekro_agent.services.sandbox.credential_guard import (
    REDACTED_PLACEHOLDER,
    collect_platform_secrets,
    issue_sandbox_call_grant,
    redact_sandbox_payload,
    redact_sandbox_text,
    resolve_sandbox_call_grant,
    revoke_sandbox_call_grant,
)
from nekro_agent.services.sandbox.ext_caller import get_api_caller_code
from nekro_agent.tools.file_utils import FileSystem
from nekro_agent.tools.path_convertor import convert_to_host_path
from tests.rpc_sandbox_client import request_body

PLATFORM_API_KEY = "sk-platform-tenant-must-never-see-this-0123456789"
PLATFORM_BASE_URL = "https://api.platform-internal.example/v1"

CHAT_KEY = "onebot_v11-ob10001-group_123456"
CONTAINER_KEY = "sandbox_onebot_v11-ob10001-group_123456"
OTHER_CHAT_KEY = "onebot_v11-ob10001-group_999999"


@pytest.fixture
def platform_model_group():
    """把一个带真实形态 API_KEY 的模型组挂进核心配置，并确保配置已注册到 ConfigManager"""
    group_key = "__test_platform_group__"
    config.MODEL_GROUPS[group_key] = ModelConfigGroup(
        GROUP_NAME=group_key,
        CHAT_MODEL="gpt-4o",
        BASE_URL=PLATFORM_BASE_URL,
        API_KEY=PLATFORM_API_KEY,
    )
    ConfigManager.register_config(CoreConfig.get_config_key(), config)
    yield
    config.MODEL_GROUPS.pop(group_key, None)


async def _leaky_upstream_error(_ctx: AgentCtx, prompt: str) -> str:
    """复刻 `plugins/builtin/draw/handlers.py` 谷歌原生模式的泄漏形态：key 拼在 URL 里，
    httpx 的 HTTPStatusError 文案带完整 URL，再被 `raise Exception(f"...: {e}")` 转发出来。
    """
    url = f"{PLATFORM_BASE_URL}/models/x:generateContent?key={PLATFORM_API_KEY}"
    raise RuntimeError(f"多图绘制失败: Client error '400 Bad Request' for url '{url}'")


async def _leaky_config_dump(_ctx: AgentCtx) -> Dict[str, Any]:
    """复刻「插件把配置整块当返回值送回沙盒」的泄漏形态"""
    return {"model_groups": {key: group.model_dump() for key, group in config.MODEL_GROUPS.items()}}


async def _benign_echo(_ctx: AgentCtx, text: str) -> str:
    """正常方法：用于确认防护没有把正常能力一起堵死"""
    return f"echo: {text}"


def _parse_proxy_names(api_caller_code: str) -> List[str]:
    """从生成的 `api_caller.py` 里取出被 `@__extension_method_proxy` 装饰的函数名

    只认代理函数，`ext_caller_code.py` 自带的 `dynamic_importer` / `_parse_pip_error`
    等本地函数不算数——它们不经 RPC。
    """
    lines = api_caller_code.splitlines()
    return [
        lines[index + 1].split("def ")[1].split("(")[0]
        for index, line in enumerate(lines[:-1])
        if line.strip() == "@__extension_method_proxy" and lines[index + 1].startswith("def ")
    ]


class _StubPlugin:
    """最小可用的插件替身，只提供 collector 与 rpc 路由实际会用到的属性"""

    key = "test.sandbox_isolation"
    is_enabled = True

    def __init__(self, methods: List[SandboxMethod]) -> None:
        self.sandbox_methods = methods

    async def collect_available_methods(self, ctx: Optional[AgentCtx]) -> List[SandboxMethod]:
        return self.sandbox_methods


@pytest.fixture
def stub_plugin(monkeypatch: pytest.MonkeyPatch) -> _StubPlugin:
    methods: List[SandboxMethod] = []
    for func, method_type in (
        (_leaky_upstream_error, SandboxMethodType.TOOL),
        (_leaky_config_dump, SandboxMethodType.TOOL),
        (_benign_echo, SandboxMethodType.TOOL),
    ):
        func._method_type = method_type  # noqa: SLF001  # mount_sandbox_method 的等价副作用
        methods.append(SandboxMethod(method_type, func.__name__, "", func))

    plugin = _StubPlugin(methods)
    monkeypatch.setitem(plugin_collector.loaded_plugins, plugin.key, plugin)  # type: ignore[arg-type]
    return plugin


@pytest.fixture
def sandbox_client(stub_plugin: _StubPlugin, monkeypatch: pytest.MonkeyPatch):
    """把 RPC 路由挂成独立应用，并按沙盒侧的真实姿势发请求"""

    async def _fake_ctx(chat_key: str, container_key: str = "") -> AgentCtx:
        return AgentCtx(from_chat_key=chat_key, container_key=container_key)

    monkeypatch.setattr(AgentCtx, "create_by_chat_key", staticmethod(_fake_ctx))

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(rpc_router)
    client = TestClient(app, raise_server_exceptions=False)

    token = issue_sandbox_call_grant(
        container_key=CONTAINER_KEY,
        from_chat_key=CHAT_KEY,
        method_names=[method.func.__name__ for method in stub_plugin.sandbox_methods],
    )

    def call(
        method: str,
        *args: Any,
        token_override: Optional[str] = None,
        chat_key: str = CHAT_KEY,
        container_key: str = CONTAINER_KEY,
    ):
        body = request_body(method, *args)  # 沙盒侧真实的编码器（JSON），见 `tests/rpc_sandbox_client.py`
        return client.post(
            f"/ext/rpc_exec?container_key={container_key}&from_chat_key={chat_key}",
            content=body,
            headers={"Content-Type": "application/json", "X-RPC-Token": token_override or token},
        )

    yield call
    revoke_sandbox_call_grant(CONTAINER_KEY)


class TestSecretCollection:
    """凭据的识别范围：嵌套模型里未标注的 API_KEY 也必须被认出来"""

    def test_collects_nested_model_group_api_key(self, platform_model_group: None) -> None:
        # ModelConfigGroup.API_KEY 没有 is_secret 标记，只能靠字段名兜住；
        # 一旦这条断言挂掉，说明所有模型组密钥都不再受出口脱敏保护
        assert PLATFORM_API_KEY in collect_platform_secrets()

    def test_collects_process_level_secrets(self) -> None:
        secrets = collect_platform_secrets()
        assert OsEnv.RPC_SECRET_KEY in secrets
        assert OsEnv.JWT_SECRET_KEY in secrets

    def test_ignores_short_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 配置里随手填的短值不能进凭据集合，否则正常文本会被打成马赛克
        monkeypatch.setitem(
            config.MODEL_GROUPS,
            "__test_short__",
            ModelConfigGroup(GROUP_NAME="__test_short__", API_KEY="abc"),
        )
        assert "abc" not in collect_platform_secrets()

    def test_collects_the_credential_cipher_master_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模型组凭据的静态加密主密钥同样不能回到沙盒

        它比这张表里任何一项都值钱：拿到它 + 一份库 dump = 全部租户与平台方的 API Key。
        改造前它没被登记进 `_OS_ENV_SECRET_ATTRS`，而**没有任何用例会因此变红**
        （脱敏少遮一个值不改变任何既有断言）—— 这条就是那个信号。
        """
        master_key = "credential-cipher-master-SENTINEL-4b71"
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", master_key)
        assert master_key in collect_platform_secrets()

    def test_an_empty_master_key_does_not_poison_the_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """反向对照：没配这一项时（默认档）不能往集合里塞一个空串

        空串进了集合 = 任何字符串都命中替换，出口脱敏当场变成全文打码。
        """
        monkeypatch.setattr(OsEnv, "CREDENTIAL_SECRET_KEY", "")
        assert "" not in collect_platform_secrets()


class TestEveryOsEnvSecretIsRegistered:
    """`_OS_ENV_SECRET_ATTRS` 是**手工清单**，这里是它的看门人

    `collect_platform_secrets()` 对配置树是自动下钻的（新增插件配置、新增模型组自动纳入），
    但 `OsEnv` 上的进程级凭据必须逐个登记。漏登记没有任何运行时信号：脱敏只是少遮一个值，
    既有断言一条都不会动。新增 `CREDENTIAL_SECRET_KEY` 时就漏过一次。

    判据用的是本模块自己那条凭据命名正则（`_SECRET_FIELD_NAME_PATTERN`，与
    `nekro_log_hygiene.SECRET_ENV_KEY_PATTERN` 同源），只看**字符串**字段：
    `ACCESS_TOKEN_EXPIRE_DAYS` 名字里带 TOKEN 但是个 int，`_collect_string` 本来就跳过它。
    """

    def test_no_string_secret_field_is_missing(self) -> None:
        missing = sorted(
            name
            for name, value in vars(OsEnv).items()
            if not name.startswith("_")
            and isinstance(value, str)
            and guard._SECRET_FIELD_NAME_PATTERN.search(name)  # noqa: SLF001
            and name not in guard._OS_ENV_SECRET_ATTRS  # noqa: SLF001
        )
        assert missing == [], (
            f"`OsEnv` 上这些凭据字段没有登记进 credential_guard._OS_ENV_SECRET_ATTRS：{missing}。"
            "它们的取值会原样随沙盒返回值/异常文本回到模型生成的代码里。"
        )

    def test_the_watchdog_can_actually_see_a_missing_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正向对照：把一个凭据字段从清单里摘掉，上一条必须变红

        没有这条的话，一个正则写错（永不命中）或清单读错的看门人也会"通过"。
        """
        monkeypatch.setattr(
            guard,
            "_OS_ENV_SECRET_ATTRS",
            tuple(name for name in guard._OS_ENV_SECRET_ATTRS if name != "JWT_SECRET_KEY"),  # noqa: SLF001
        )
        with pytest.raises(AssertionError):
            self.test_no_string_secret_field_is_missing()


class TestOutboundRedaction:
    """出口脱敏：凭据不得随返回值或异常文本回到沙盒"""

    def test_redacts_key_inside_upstream_error_text(self, platform_model_group: None) -> None:
        text = f"多图绘制失败: 400 for url '{PLATFORM_BASE_URL}/x?key={PLATFORM_API_KEY}'"
        redacted = redact_sandbox_text(text)
        assert PLATFORM_API_KEY not in redacted
        assert REDACTED_PLACEHOLDER in redacted

    def test_redacts_nested_containers(self, platform_model_group: None) -> None:
        payload = {"groups": [{"API_KEY": PLATFORM_API_KEY}], "tuple": (PLATFORM_API_KEY,)}
        redacted = redact_sandbox_payload(payload)
        assert PLATFORM_API_KEY not in repr(redacted)
        assert redacted["groups"][0]["API_KEY"] == REDACTED_PLACEHOLDER
        assert isinstance(redacted["tuple"], tuple)

    def test_redacts_custom_object_by_repr(self, platform_model_group: None) -> None:
        class _Carrier:
            def __repr__(self) -> str:
                return f"<Carrier key={PLATFORM_API_KEY}>"

        assert redact_sandbox_payload(_Carrier()) == REDACTED_PLACEHOLDER

    def test_keeps_benign_payload_intact(self, platform_model_group: None) -> None:
        payload = {"text": "hello", "items": [1, 2, 3], "ok": True, "none": None}
        assert redact_sandbox_payload(payload) == payload


class TestCallGrant:
    """调用许可：令牌、会话、方法白名单三者必须同时对上"""

    def test_rejects_global_rpc_secret_key(self) -> None:
        issue_sandbox_call_grant(CONTAINER_KEY, CHAT_KEY, ["_benign_echo"])
        try:
            # 令牌下发进容器，模型生成的代码必然能读到；因此它绝不能是全局密钥
            assert resolve_sandbox_call_grant(CONTAINER_KEY, OsEnv.RPC_SECRET_KEY, CHAT_KEY) is None
        finally:
            revoke_sandbox_call_grant(CONTAINER_KEY)

    def test_rejects_chat_key_substitution(self) -> None:
        token = issue_sandbox_call_grant(CONTAINER_KEY, CHAT_KEY, ["_benign_echo"])
        try:
            assert resolve_sandbox_call_grant(CONTAINER_KEY, token, OTHER_CHAT_KEY) is None
            assert resolve_sandbox_call_grant(CONTAINER_KEY, token, CHAT_KEY) is not None
        finally:
            revoke_sandbox_call_grant(CONTAINER_KEY)

    def test_grant_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 容器最长活到沙盒运行超时，许可不能比容器活得久：否则上一轮泄漏出去的令牌
        # 可以在容器早已销毁之后继续调用
        token = issue_sandbox_call_grant(CONTAINER_KEY, CHAT_KEY, ["_benign_echo"])
        try:
            real_time = time.time
            monkeypatch.setattr(guard.time, "time", lambda: real_time() + 365 * 24 * 3600)
            assert resolve_sandbox_call_grant(CONTAINER_KEY, token, CHAT_KEY) is None
        finally:
            revoke_sandbox_call_grant(CONTAINER_KEY)

    async def test_grant_method_names_match_generated_proxies(self, stub_plugin: _StubPlugin) -> None:
        """白名单必须恰好等于写进容器的代理函数集合

        这是整条防线的结构性保证：白名单由「下发」这一动作派生，
        因此新增沙盒方法既不会漏掉白名单，也不会凭空扩大白名单。
        """
        code = await get_api_caller_code(container_key=CONTAINER_KEY, from_chat_key=CHAT_KEY, ctx=None)
        try:
            grant = resolve_sandbox_call_grant(
                CONTAINER_KEY,
                code.split('RPC_SECRET_KEY = "')[1].split('"')[0],
                CHAT_KEY,
            )
            assert grant is not None
            assert grant.method_names == frozenset(_parse_proxy_names(code))
            assert "_benign_echo" in grant.method_names
            # 该方法在容器内本地实现，不经 RPC，因此既不生成代理也不该被授权
            assert "dynamic_importer" not in grant.method_names
        finally:
            revoke_sandbox_call_grant(CONTAINER_KEY)


class TestRpcBoundary:
    """RPC 入口的端到端行为：完全按沙盒侧的请求姿势发起"""

    def test_benign_call_still_works(self, sandbox_client, platform_model_group: None) -> None:
        resp = sandbox_client("_benign_echo", "hello")
        assert resp.status_code == 200
        assert resp.headers["Run-Error"] == "False"
        assert pickle.loads(resp.content) == "echo: hello"

    def test_upstream_error_text_is_redacted(self, sandbox_client, platform_model_group: None) -> None:
        resp = sandbox_client("_leaky_upstream_error", "draw me")
        assert resp.status_code == 200
        assert resp.headers["Run-Error"] == "True"
        assert PLATFORM_API_KEY not in resp.text
        assert REDACTED_PLACEHOLDER in resp.text

    def test_config_dump_result_is_redacted(self, sandbox_client, platform_model_group: None) -> None:
        resp = sandbox_client("_leaky_config_dump")
        assert resp.status_code == 200
        assert PLATFORM_API_KEY not in repr(pickle.loads(resp.content))

    def test_rejects_method_outside_allowlist(self, sandbox_client) -> None:
        # 方法确实存在于其他插件时同样要挡住，否则「未启用插件」等于形同虚设
        assert sandbox_client("get_email_accounts").status_code == 401

    def test_rejects_foreign_chat_key(self, sandbox_client) -> None:
        assert sandbox_client("_benign_echo", "hi", chat_key=OTHER_CHAT_KEY).status_code == 401

    def test_rejects_global_secret_as_token(self, sandbox_client) -> None:
        assert sandbox_client("_benign_echo", "hi", token_override=OsEnv.RPC_SECRET_KEY).status_code == 401


class TestSandboxPathContainment:
    """互通目录的路径映射：`..` 与软链都不得逃出目录"""

    @pytest.mark.parametrize(
        ("sandbox_path", "why"),
        [
            ("/app/shared/../../configs/nekro-agent.yaml", "直指 NekroAgent 配置文件，内含全部模型组 API_KEY"),
            ("/app/uploads/../../configs/nekro-agent.yaml", "上传目录同样可穿越"),
            ("/app/shared/../../../../etc/passwd", "穿越到 DATA_DIR 之外"),
        ],
    )
    def test_convert_to_host_path_rejects_traversal(self, tmp_path: Path, sandbox_path: str, why: str) -> None:
        with pytest.raises(ValueError):
            convert_to_host_path(
                Path(sandbox_path),
                CHAT_KEY,
                CONTAINER_KEY,
                uploads_dir=tmp_path / "uploads",
                shared_dir=tmp_path / "sandboxes",
            )

    def test_convert_to_host_path_keeps_normal_paths(self, tmp_path: Path) -> None:
        host_path = convert_to_host_path(
            Path("/app/shared/sub/dir/pic.png"),
            CHAT_KEY,
            CONTAINER_KEY,
            uploads_dir=tmp_path / "uploads",
            shared_dir=tmp_path / "sandboxes",
        )
        assert host_path == tmp_path / "sandboxes" / CONTAINER_KEY / "sub" / "dir" / "pic.png"

    def test_fs_get_file_rejects_traversal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(FileSystem, "shared_path", property(lambda _self: tmp_path / "shared"))
        monkeypatch.setattr(FileSystem, "upload_path", property(lambda _self: tmp_path / "uploads"))
        fs = FileSystem(CHAT_KEY, CONTAINER_KEY)
        with pytest.raises(ValueError):
            fs.get_file("/app/shared/../../configs/nekro-agent.yaml")
        with pytest.raises(ValueError):
            fs.get_file("/app/uploads/../../configs/nekro-agent.yaml")
        assert fs.get_file("/app/shared/pic.png") == tmp_path / "shared" / "pic.png"
