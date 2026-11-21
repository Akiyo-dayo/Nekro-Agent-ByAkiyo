"""租户能填的出站点都走 `tools/tenant_egress.py`；平台方的照旧

## 这条测试钉的是什么

每个租户能填、NA 会去连的出站点（模型组的连接池、拉模型列表、绘图、视频转写、实例告警 webhook、
MCP 服务器校验、邮箱）：

- 域名解析到内网 → 拒绝，不发起连接（透明代理的 Fake-IP 段与下载同一份放行网段，默认放行）；
- 连的就是校验过的那个地址（DNS 之后改口也不跟）；
- TLS 的 SNI 与 `Host` 头仍是主机名；
- 平台方的组 / 平台方填的地址照旧（不用租户出站的客户端）。

另有一条静态用例：本仓库里每一处调用模型组出站函数的地方都显式说明了归属（`tenant_egress`），
漏写的新调用处会让这里红，而不是悄悄按平台方处理。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, List

import httpcore
import httpx
import pytest

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.tools import tenant_egress
from nekro_agent.tools.tenant_egress import TenantHTTPTransport
from tests.test_tenant_egress import OK_RESPONSE, PRIVATE_IP, PUBLIC_IP, FakeBackend, FakeDNS


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> FakeDNS:
    fake = FakeDNS()
    fake.install(monkeypatch)
    return fake


@pytest.fixture(autouse=True)
def no_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

REPO_ROOT = Path(__file__).resolve().parents[1]
TENANT_GROUP = ModelConfigGroup(GROUP_NAME="t", CHAT_MODEL="m", BASE_URL="https://api.example.test/v1", API_KEY="k-tenant")
PLATFORM_GROUP = ModelConfigGroup(GROUP_NAME="p", CHAT_MODEL="m", BASE_URL="https://api.example.test/v1", API_KEY="k-platform")


def json_response(payload: Any) -> bytes:
    body = json.dumps(payload).encode()
    return b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    """把租户出站真正建连接的那一层换成假的（`TenantNetworkBackend` 建的时候取 `httpcore.AnyIOBackend()`）"""
    fake = FakeBackend()
    monkeypatch.setattr(httpcore, "AnyIOBackend", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def tenant_groups() -> Any:
    # 判不出来按租户：平台方的组要登记过才按平台方
    tenant_egress.reset_model_group_registry()
    tenant_egress.register_model_groups(tenant=[("tenant-g", TENANT_GROUP)], platform=[("platform-g", PLATFORM_GROUP)])
    yield
    tenant_egress.reset_model_group_registry()


@pytest.fixture(autouse=True)
async def fresh_http_clients() -> Any:
    from nekro_agent.services.agent import openai as openai_module

    await openai_module.reset_http_clients()
    yield
    await openai_module.reset_http_clients()


# ---------------------------------------------------------------------------
# 静态：调用处都说明了归属
# ---------------------------------------------------------------------------

_MODEL_GROUP_EGRESS = {
    "gen_openai_chat_response",
    "gen_openai_chat_stream",
    "gen_openai_embeddings",
    "list_openai_models",
    "list_gemini_models",
    "run_model_group_test",
}
# `**` 展开的参数字典由这几个函数造，它们自己带 `tenant_egress`（下一条用例核对）
_PARAM_BUILDERS = {"build_openai_model_test_params", "build_openai_embedding_test_params", "_build_chat_params", "_build_model_params"}


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_every_model_group_egress_call_states_its_ownership() -> None:
    missing: List[str] = []
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
                if not isinstance(node, ast.Call) or _callee(node) not in _MODEL_GROUP_EGRESS:
                    continue
                if any(keyword.arg == "tenant_egress" for keyword in node.keywords):
                    continue
                spread = [keyword.value for keyword in node.keywords if keyword.arg is None]
                if any(
                    (isinstance(value, ast.Call) and _callee(value) in _PARAM_BUILDERS)
                    or (isinstance(value, ast.Name) and value.id in builder_vars)
                    for value in spread
                ):
                    continue
                missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {_callee(node)}")
    assert missing == [], "这些调用没有说明模型组归属（tenant_egress）"


# 自己建 HTTP 客户端的写法：`httpx.AsyncClient(...)`、`httpx.post(...)` 这类一次性调用、`AsyncOpenAI(...)`、aiohttp 会话
_CLIENT_MODULES = {"httpx", "aiohttp", "openai"}
_CLIENT_CONSTRUCTORS = {"AsyncClient", "Client", "ClientSession", "AsyncOpenAI", "OpenAI"}
_HTTPX_ONE_SHOT = {"get", "post", "put", "patch", "delete", "request", "stream"}
_BARE_CLIENT_CONSTRUCTORS = {"AsyncClient", "AsyncOpenAI", "ClientSession"}

# 同一个函数里既读模型组的 `BASE_URL`、又自己建客户端，而且已经按归属分流过的（租户的组走 `tenant_http_client`，
# 平台方的组照旧）。新出现的这种写法要么改成走 `tenant_egress`，要么核对过之后写进这里并说明理由；
# 这里的条目不再命中时用例同样会红，免得名单越积越旧
_BASE_URL_CLIENT_ALLOWLIST = {
    "plugins/builtin/video_analysis/inference.py::transcribe_audio": "租户的组走 tenant_http_client，平台方的组照旧",
}


def _builds_client(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in _CLIENT_MODULES:
        return func.attr in _CLIENT_CONSTRUCTORS or (func.value.id == "httpx" and func.attr in _HTTPX_ONE_SHOT)
    return isinstance(func, ast.Name) and func.id in _BARE_CLIENT_CONSTRUCTORS


def test_code_that_reads_a_model_group_base_url_does_not_build_its_own_client() -> None:
    """插件或服务拿模型组的 `BASE_URL` 自己建 httpx / openai / aiohttp 客户端，就绕过了租户出站

    判据是「同一个函数里既读 `.BASE_URL`、又自己建客户端」：抓的是最直接的写法，换个辅助函数建客户端就看不见了，
    所以它是一道提醒，不是证明。正路是 `tenant_egress.tenant_http_client`（按 `is_tenant_owned_group` 分流），
    或经 `services/agent/openai.py` 的出站函数并写明 `tenant_egress`（上面那条静态用例）。
    """
    found = set()
    for base in ("nekro_agent", "plugins"):
        for path in (REPO_ROOT / base).rglob("*.py"):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative in {"nekro_agent/tools/tenant_egress.py", "nekro_agent/services/agent/openai.py"}:
                continue  # 出站本身的实现
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                nodes = list(ast.walk(function))
                reads_base_url = any(isinstance(node, ast.Attribute) and node.attr == "BASE_URL" for node in nodes)
                if reads_base_url and any(isinstance(node, ast.Call) and _builds_client(node) for node in nodes):
                    found.add(f"{relative}::{function.name}")
    unexpected = sorted(found - set(_BASE_URL_CLIENT_ALLOWLIST))
    stale = sorted(set(_BASE_URL_CLIENT_ALLOWLIST) - found)
    assert unexpected == [], "这些函数拿模型组的 BASE_URL 自己建客户端，没有经过租户出站"
    assert stale == [], "名单里的这些条目已经不再命中，请删掉"


def test_param_builders_carry_the_ownership() -> None:
    from nekro_agent.services.command.built_in.debug import _build_chat_params
    from nekro_agent.services.model_test import build_openai_embedding_test_params, build_openai_model_test_params

    assert build_openai_model_test_params(TENANT_GROUP)["tenant_egress"] is True
    assert build_openai_model_test_params(PLATFORM_GROUP)["tenant_egress"] is False
    assert build_openai_model_test_params(PLATFORM_GROUP, tenant_egress=True)["tenant_egress"] is True
    assert build_openai_embedding_test_params(TENANT_GROUP)["tenant_egress"] is True
    assert _build_chat_params(TENANT_GROUP, False)["tenant_egress"] is True
    assert _build_chat_params(PLATFORM_GROUP, False)["tenant_egress"] is False


async def test_run_model_group_test_infers_the_ownership_when_not_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """仓库里的入口都显式给（上面那条静态用例）；不给时按组判，租户的组照样按租户出站"""
    from nekro_agent.services import model_group_test

    seen: List[Any] = []

    async def _fake_chat(**kwargs: Any) -> Any:
        seen.append(kwargs["tenant_egress"])
        raise RuntimeError("stop")

    monkeypatch.setattr("nekro_agent.services.agent.openai.gen_openai_chat_response", _fake_chat)
    await model_group_test.run_model_group_test(TENANT_GROUP, credential_visible=True, group_name="tenant-g")
    await model_group_test.run_model_group_test(PLATFORM_GROUP, credential_visible=True, group_name="platform-g")
    await model_group_test.run_model_group_test(PLATFORM_GROUP, credential_visible=True, tenant_egress=True)
    # 组名是平台方的，连接参数却是租户的（或反过来）：任一判出是租户就按租户
    await model_group_test.run_model_group_test(TENANT_GROUP, credential_visible=True, group_name="platform-g")
    await model_group_test.run_model_group_test(PLATFORM_GROUP, credential_visible=True, group_name="tenant-g")
    assert seen == [True, False, True, True, True]


# ---------------------------------------------------------------------------
# 模型组：连接池、拉模型列表
# ---------------------------------------------------------------------------


class TestModelGroupClient:
    async def test_tenant_and_platform_clients_are_separate(self) -> None:
        from nekro_agent.services.agent import openai as openai_module

        tenant_client = openai_module._get_http_client(base_url="https://api.example.test/v1", tenant_egress=True)  # noqa: SLF001
        platform_client = openai_module._get_http_client(base_url="https://api.example.test/v1", tenant_egress=False)  # noqa: SLF001

        assert tenant_client is not platform_client
        assert isinstance(tenant_client._transport, TenantHTTPTransport)  # noqa: SLF001
        assert not isinstance(platform_client._transport, TenantHTTPTransport), "平台方的组照旧"  # noqa: SLF001

    async def test_platform_client_keeps_its_proxy_mounts(self) -> None:
        from nekro_agent.services.agent import openai as openai_module

        client = openai_module._create_http_client(proxy_url="http://platform-proxy.example.test:7890")  # noqa: SLF001
        try:
            assert not isinstance(client._transport, TenantHTTPTransport)  # noqa: SLF001
            assert client._mounts, "平台方的组按原来的写法挂代理"  # noqa: SLF001
        finally:
            await client.aclose()

    async def test_list_models_for_a_tenant_connects_to_the_checked_address(self, dns: FakeDNS, backend: FakeBackend) -> None:
        from nekro_agent.services.agent.openai import list_openai_models

        backend.responses = [[json_response({"data": [{"id": "m1"}]})]]
        models = await list_openai_models(base_url="https://api.example.test/v1", api_key="k", tenant_egress=True)

        assert models == ["m1"]
        assert backend.dials == [(PUBLIC_IP, 443)]
        assert backend.tls_hostnames == ["api.example.test"]
        assert "Host: api.example.test\r\n" in backend.request_text()

    async def test_list_models_for_a_tenant_refuses_a_private_address(self, dns: FakeDNS, backend: FakeBackend) -> None:
        from nekro_agent.services.agent.openai import list_openai_models

        dns.answer("api.example.test", [PRIVATE_IP])
        with pytest.raises(httpx.ConnectError):
            await list_openai_models(base_url="https://api.example.test/v1", api_key="k", tenant_egress=True)
        assert backend.dials == []

    async def test_list_models_for_a_tenant_follows_the_extra_networks_for_fake_ip(
        self, dns: FakeDNS, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fake-IP 段与下载同一份放行网段：默认放行并连校验过的那个地址，删掉就拒"""
        from nekro_agent.core.os_env import OsEnv
        from nekro_agent.services.agent.openai import list_openai_models
        from nekro_agent.tools.common_util import DEFAULT_EXTRA_ALLOWED_NETWORKS

        fake_ip = ".".join(("198", "18", "0", "9"))
        dns.answer("api.example.test", [fake_ip])
        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)
        backend.responses = [[json_response({"data": [{"id": "m1"}]})]]
        assert await list_openai_models(base_url="https://api.example.test/v1", api_key="k", tenant_egress=True) == ["m1"]
        assert backend.dials == [(fake_ip, 443)]

        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", "")
        with pytest.raises(httpx.ConnectError):
            await list_openai_models(base_url="https://api.example.test/v1", api_key="k", tenant_egress=True)
        assert backend.dials == [(fake_ip, 443)], "删掉之后一次都不连"

    async def test_tenant_group_proxy_is_treated_as_a_tenant_address(self, dns: FakeDNS, backend: FakeBackend) -> None:
        from nekro_agent.services.agent import openai as openai_module

        dns.answer("proxy.example.test", [PRIVATE_IP])
        client = openai_module._get_http_client(  # noqa: SLF001
            base_url="https://api.example.test/v1",
            proxy_url="http://proxy.example.test:3128",
            tenant_egress=True,
        )
        with pytest.raises(httpx.ConnectError):
            await client.get("https://api.example.test/v1/models")
        assert backend.dials == []

    async def test_runtime_view_registers_tenant_groups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from nekro_agent.services import model_group_service

        rows = [
            SimpleNamespace(name="tenant-x", is_platform_owned=False, to_config_group=lambda: TENANT_GROUP),
            SimpleNamespace(name="platform-x", is_platform_owned=True, to_config_group=lambda: PLATFORM_GROUP),
        ]

        class _Query:
            def order_by(self, *_args: Any) -> Any:
                async def _rows() -> List[Any]:
                    return rows

                return _rows()

        monkeypatch.setattr(model_group_service.DBModelGroup, "all", classmethod(lambda cls: _Query()))
        monkeypatch.setattr(model_group_service.config, "MODEL_GROUPS", {})
        await model_group_service.refresh_runtime_view(persist=False)

        assert tenant_egress.is_tenant_owned_group("tenant-x")
        assert not tenant_egress.is_tenant_owned_group("platform-x")


# ---------------------------------------------------------------------------
# 一轮中途改组 / 删组：后续迭代仍走租户出站（登记由 `refresh_runtime_view` 做，调用处按副本判归属）
# ---------------------------------------------------------------------------


class TestMidRoundChanges:
    @staticmethod
    async def refresh(monkeypatch: pytest.MonkeyPatch, rows: List[Any]) -> None:
        from nekro_agent.services import model_group_service

        class _Query:
            def order_by(self, *_args: Any) -> Any:
                async def _rows() -> List[Any]:
                    return list(rows)

                return _rows()

        monkeypatch.setattr(model_group_service.DBModelGroup, "all", classmethod(lambda cls: _Query()))
        await model_group_service.refresh_runtime_view(persist=False)

    @staticmethod
    def row(name: str, platform: bool, group: ModelConfigGroup) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(name=name, is_platform_owned=platform, to_config_group=lambda: group)

    @pytest.fixture(autouse=True)
    def empty_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services import model_group_service

        tenant_egress.reset_model_group_registry()
        monkeypatch.setattr(model_group_service.config, "MODEL_GROUPS", {})

    async def _later_iteration_goes_through_tenant_egress(self, stale: ModelConfigGroup, dns: FakeDNS, backend: FakeBackend) -> None:
        """这一轮后面的迭代：调用处拿着旧副本判归属、建客户端；地址指向内网时在建连接那一步被拒"""
        from plugins.builtin.draw.handlers import _draw_client

        assert tenant_egress.is_tenant_owned_group(stale)
        dns.answer("mid.example.test", [PRIVATE_IP])
        async with _draw_client(stale) as client:
            assert isinstance(client._transport, TenantHTTPTransport)  # noqa: SLF001
            with pytest.raises(httpx.ConnectError):
                await client.get("https://mid.example.test/v1/images/generations")
        assert backend.dials == []

    async def test_the_tenant_changes_the_key_mid_round(self, monkeypatch: pytest.MonkeyPatch, dns: FakeDNS, backend: FakeBackend) -> None:
        original = ModelConfigGroup(GROUP_NAME="mid", CHAT_MODEL="m", BASE_URL="https://mid.example.test/v1", API_KEY="k-before")
        await self.refresh(monkeypatch, [self.row("mid-g", False, original), self.row("platform-g", True, PLATFORM_GROUP)])
        stale = original.model_copy()  # 这一轮开始时拿到的副本

        rotated = original.model_copy(update={"API_KEY": "k-after"})
        await self.refresh(monkeypatch, [self.row("mid-g", False, rotated), self.row("platform-g", True, PLATFORM_GROUP)])

        await self._later_iteration_goes_through_tenant_egress(stale, dns, backend)
        assert not tenant_egress.is_tenant_owned_group(PLATFORM_GROUP), "平台方的组照旧"

    async def test_the_tenant_deletes_the_group_mid_round(self, monkeypatch: pytest.MonkeyPatch, dns: FakeDNS, backend: FakeBackend) -> None:
        original = ModelConfigGroup(GROUP_NAME="mid", CHAT_MODEL="m", BASE_URL="https://mid.example.test/v1", API_KEY="k-before")
        await self.refresh(monkeypatch, [self.row("mid-g", False, original), self.row("platform-g", True, PLATFORM_GROUP)])
        stale = original.model_copy()

        await self.refresh(monkeypatch, [self.row("platform-g", True, PLATFORM_GROUP)])

        await self._later_iteration_goes_through_tenant_egress(stale, dns, backend)
        assert tenant_egress.is_tenant_owned_group("mid-g")

    async def test_the_model_client_for_the_stale_copy_is_the_tenant_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模型调用那条路：调用处写 `tenant_egress=is_tenant_owned_group(副本)`，拿到的连接池是租户出站的那一个"""
        from nekro_agent.services.agent import openai as openai_module

        original = ModelConfigGroup(GROUP_NAME="mid", CHAT_MODEL="m", BASE_URL="https://mid.example.test/v1", API_KEY="k-before")
        await self.refresh(monkeypatch, [self.row("mid-g", False, original)])
        stale = original.model_copy()
        await self.refresh(monkeypatch, [self.row("mid-g", False, original.model_copy(update={"API_KEY": "k-after"}))])

        client = openai_module._get_http_client(  # noqa: SLF001
            base_url=stale.BASE_URL,
            tenant_egress=tenant_egress.is_tenant_owned_group(stale),
        )
        assert isinstance(client._transport, TenantHTTPTransport)  # noqa: SLF001


# ---------------------------------------------------------------------------
# 模型组连接池的容量
# ---------------------------------------------------------------------------


class TestHttpClientCache:
    """按最近使用淘汰；被挤出去的客户端等在途请求结束（流式的是流被关掉）、再宽限一段才关"""

    @pytest.fixture(autouse=True)
    def short_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.agent import openai as openai_module

        monkeypatch.setattr(openai_module, "_RETIRE_GRACE_SECONDS", 0.05)

    @pytest.fixture
    def mock_clients(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """`_create_http_client` 造的客户端换成走假传输层的（类型照旧是调用方要的那个）"""
        from nekro_agent.services.agent import openai as openai_module

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "m1"}]})
            # stream=：真正的流式响应（content= 构造时就读完了，算不上在途）
            return httpx.Response(200, stream=httpx.ByteStream(b"chunk-1 chunk-2"))

        created: List[Any] = []

        def fake_create(*, client_class: type = httpx.AsyncClient, **_kwargs: Any) -> Any:
            client = client_class(transport=httpx.MockTransport(handler))
            created.append(client)
            return client

        monkeypatch.setattr(openai_module, "_create_http_client", fake_create)
        return created

    @staticmethod
    async def settle() -> None:
        import asyncio

        for _ in range(5):
            await asyncio.sleep(0)

    @classmethod
    async def past_grace(cls) -> None:
        import asyncio

        await asyncio.sleep(0.15)
        await cls.settle()

    async def test_least_recently_used_idle_clients_are_evicted_and_closed(self, monkeypatch: pytest.MonkeyPatch, mock_clients: Any) -> None:
        from nekro_agent.services.agent import openai as openai_module

        monkeypatch.setattr(openai_module, "_HTTP_CLIENT_CACHE_MAX", 2)
        a = openai_module._get_http_client(base_url="https://a.example.test/v1", tenant_egress=True)  # noqa: SLF001
        b = openai_module._get_http_client(base_url="https://b.example.test/v1", tenant_egress=True)  # noqa: SLF001
        assert openai_module._get_http_client(base_url="https://a.example.test/v1", tenant_egress=True) is a  # noqa: SLF001
        c = openai_module._get_http_client(base_url="https://c.example.test/v1", tenant_egress=True)  # noqa: SLF001
        await self.settle()

        assert len(openai_module._http_clients) == 2  # noqa: SLF001
        assert openai_module._get_http_client(base_url="https://a.example.test/v1", tenant_egress=True) is a, "最近用过的留着"  # noqa: SLF001
        assert openai_module._get_http_client(base_url="https://c.example.test/v1", tenant_egress=True) is c  # noqa: SLF001
        assert not b.is_closed, "没有在途请求也先宽限一段：可能刚交出去、还没发请求"
        await self.past_grace()
        assert b.is_closed, "宽限期过了、仍没有在途请求：关"
        assert not a.is_closed and not c.is_closed

    async def test_a_client_handed_out_just_before_eviction_can_still_send(self, monkeypatch: pytest.MonkeyPatch, mock_clients: Any) -> None:
        """`_get_http_client` 交出客户端、调用方还没发请求时，来了一批新键把它挤出缓存：请求照常发得出去"""
        from nekro_agent.services.agent import openai as openai_module

        monkeypatch.setattr(openai_module, "_HTTP_CLIENT_CACHE_MAX", 1)
        handed_out = openai_module._get_http_client(base_url="https://a.example.test/v1")  # noqa: SLF001
        openai_module._get_http_client(base_url="https://b.example.test/v1")  # noqa: SLF001
        await self.settle()

        response = await handed_out.get("https://a.example.test/v1/models")
        assert response.status_code == 200
        assert not handed_out.is_closed, "请求刚结束，还在宽限期里"
        await self.past_grace()
        assert handed_out.is_closed
        assert id(handed_out) not in openai_module._retiring_clients  # noqa: SLF001

    async def test_a_request_started_during_the_grace_period_keeps_the_client_open(
        self, monkeypatch: pytest.MonkeyPatch, mock_clients: Any
    ) -> None:
        from nekro_agent.services.agent import openai as openai_module

        monkeypatch.setattr(openai_module, "_HTTP_CLIENT_CACHE_MAX", 1)
        first = openai_module._get_http_client(base_url="https://a.example.test/v1")  # noqa: SLF001
        openai_module._get_http_client(base_url="https://b.example.test/v1")  # noqa: SLF001
        response = await first.send(first.build_request("GET", "https://a.example.test/v1/x"), stream=True)
        await self.past_grace()
        assert not first.is_closed, "宽限期到点时还有在途请求：不关"
        await response.aclose()
        await self.past_grace()
        assert first.is_closed

    async def test_an_evicted_client_with_a_stream_in_flight_is_closed_when_the_stream_ends(
        self, monkeypatch: pytest.MonkeyPatch, mock_clients: Any
    ) -> None:
        """流式回复在首个 token 之后没有总时长上限：不能按时间估，要等它真的结束"""
        from nekro_agent.services.agent import openai as openai_module

        monkeypatch.setattr(openai_module, "_HTTP_CLIENT_CACHE_MAX", 1)
        first = openai_module._get_http_client(base_url="https://a.example.test/v1")  # noqa: SLF001
        response = await first.send(first.build_request("POST", "https://a.example.test/v1/chat/completions"), stream=True)
        openai_module._get_http_client(base_url="https://b.example.test/v1")  # noqa: SLF001
        await self.settle()

        assert not first.is_closed, "流还没结束，不能关"
        assert id(first) in openai_module._retiring_clients  # noqa: SLF001
        assert [chunk async for chunk in response.aiter_bytes()] == [b"chunk-1 chunk-2"]
        await response.aclose()
        await self.past_grace()
        assert first.is_closed
        assert id(first) not in openai_module._retiring_clients  # noqa: SLF001

    async def test_a_failed_request_does_not_keep_the_client_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.agent import openai as openai_module

        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client = openai_module._TrackedAsyncClient(transport=httpx.MockTransport(boom))  # noqa: SLF001
        with pytest.raises(httpx.ConnectError):
            await client.get("https://a.example.test/v1/models")
        assert client.na_in_flight == 0
        await client.aclose()

    async def test_reset_closes_clients_still_waiting_for_their_streams(self, monkeypatch: pytest.MonkeyPatch, mock_clients: Any) -> None:
        from nekro_agent.services.agent import openai as openai_module

        monkeypatch.setattr(openai_module, "_HTTP_CLIENT_CACHE_MAX", 1)
        first = openai_module._get_http_client(base_url="https://a.example.test/v1")  # noqa: SLF001
        response = await first.send(first.build_request("GET", "https://a.example.test/v1/x"), stream=True)
        openai_module._get_http_client(base_url="https://b.example.test/v1")  # noqa: SLF001
        await openai_module.reset_http_clients()
        assert first.is_closed
        assert openai_module._retiring_clients == {}  # noqa: SLF001
        await response.aclose()

    async def test_listing_models_does_not_touch_the_cache(self, mock_clients: Any) -> None:
        """地址直接来自请求体：按地址缓存的话，换着填几十个主机名就能把平台方的客户端挤出去"""
        from nekro_agent.services.agent import openai as openai_module

        models = await openai_module.list_openai_models(base_url="https://list.example.test/v1", api_key="k", tenant_egress=True)
        assert models == ["m1"]
        assert openai_module._http_clients == {}  # noqa: SLF001
        assert len(mock_clients) == 1 and mock_clients[0].is_closed, "一次性客户端，用完就关"

    async def test_testing_a_model_group_does_not_touch_the_cache(self, mock_clients: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """内联测试的地址直接来自请求体：与拉模型列表一样用一次性客户端"""
        from nekro_agent.services import model_group_test
        from nekro_agent.services.agent import openai as openai_module

        seen: List[Any] = []
        real_chat = openai_module.gen_openai_chat_response

        async def spy(**kwargs: Any) -> Any:
            seen.append(kwargs.get("pooled_client"))
            return await real_chat(**kwargs)

        monkeypatch.setattr(openai_module, "gen_openai_chat_response", spy)
        monkeypatch.setattr(openai_module, "record_llm_usage", lambda *_args, **_kwargs: None)
        await model_group_test.run_model_group_test(TENANT_GROUP, credential_visible=True, tenant_egress=True)

        assert seen == [False]
        assert openai_module._http_clients == {}  # noqa: SLF001
        assert mock_clients and all(client.is_closed for client in mock_clients), "一次性客户端，用完就关"


class TestDefaultOwnership:
    """底层出站函数没说明归属时按连接参数判，判不出来按租户（第三方插件拿租户选的组直接调这些函数）"""

    def test_unknown_parameters_are_tenant_and_registered_platform_ones_are_not(self) -> None:
        from nekro_agent.services.agent import openai as openai_module

        resolve = openai_module._resolve_tenant_egress  # noqa: SLF001
        assert resolve(None, "https://unknown.example.test/v1", "k", "") is True
        assert resolve(None, TENANT_GROUP.BASE_URL, TENANT_GROUP.API_KEY, TENANT_GROUP.CHAT_PROXY) is True
        assert resolve(None, PLATFORM_GROUP.BASE_URL + "/", PLATFORM_GROUP.API_KEY, PLATFORM_GROUP.CHAT_PROXY) is False
        assert resolve(False, "https://unknown.example.test/v1", "k", "") is False, "显式给的照用"
        assert resolve(True, PLATFORM_GROUP.BASE_URL, PLATFORM_GROUP.API_KEY, "") is True

    async def test_chat_without_ownership_uses_the_tenant_client_for_unknown_parameters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.agent import openai as openai_module

        seen: List[Any] = []
        real = openai_module._get_http_client  # noqa: SLF001

        def spy(**kwargs: Any) -> Any:
            seen.append(kwargs["tenant_egress"])
            raise RuntimeError("stop")

        monkeypatch.setattr(openai_module, "_get_http_client", spy)
        # 调用失败也会记一行用量，落库在后台任务里；这条只看归属，不留一个用例结束时还在半途的后台写库
        # （调这两个函数的其他用例也都这么做）
        monkeypatch.setattr(openai_module, "record_llm_usage", lambda *_args, **_kwargs: None)
        for call in (
            openai_module.gen_openai_chat_response(model="m", messages=[], base_url="https://unknown.example.test/v1", api_key="k"),
            openai_module.gen_openai_embeddings(model="m", input="x", base_url="https://unknown.example.test/v1", api_key="k"),
        ):
            with pytest.raises(Exception):  # noqa: B017 - 只关心传给连接池的归属
                await call
        assert seen == [True, True]
        assert real is not spy


# ---------------------------------------------------------------------------
# 绘图、视频转写
# ---------------------------------------------------------------------------


class TestPlugins:
    async def test_draw_client_for_a_tenant_group(self, dns: FakeDNS, backend: FakeBackend) -> None:
        from plugins.builtin.draw.handlers import _draw_client

        async with _draw_client(TENANT_GROUP) as client:
            assert isinstance(client._transport, TenantHTTPTransport)  # noqa: SLF001
            await client.get("https://api.example.test/v1/images/generations")
        assert backend.dials == [(PUBLIC_IP, 443)]
        assert backend.tls_hostnames == ["api.example.test"]

        dns.answer("api.example.test", [PRIVATE_IP])
        async with _draw_client(TENANT_GROUP) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://api.example.test/v1/images/generations")

    async def test_draw_client_for_a_platform_group_is_unchanged(self) -> None:
        from plugins.builtin.draw.handlers import _draw_client

        async with _draw_client(PLATFORM_GROUP) as client:
            assert not isinstance(client._transport, TenantHTTPTransport)  # noqa: SLF001

    async def test_transcribe_with_a_tenant_group(self, dns: FakeDNS, backend: FakeBackend, tmp_path: Path) -> None:
        from plugins.builtin.video_analysis.inference import ResolvedGroup, transcribe_audio

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"\x00" * 16)
        backend.responses = [[json_response({"text": "你好"})]]
        text = await transcribe_audio(ResolvedGroup(name="tenant-g", group=TENANT_GROUP), audio, chat_key="c")

        assert text == "你好"
        assert backend.dials == [(PUBLIC_IP, 443)]
        assert backend.tls_hostnames == ["api.example.test"]
        assert "Host: api.example.test\r\n" in backend.request_text()

    async def test_transcribe_with_a_tenant_group_refuses_a_private_address(self, dns: FakeDNS, backend: FakeBackend, tmp_path: Path) -> None:
        from plugins.builtin.video_analysis.inference import ResolvedGroup, transcribe_audio

        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"\x00" * 16)
        dns.answer("api.example.test", [PRIVATE_IP])
        with pytest.raises(Exception):  # noqa: B017 - 转写把失败包成插件自己的错误
            await transcribe_audio(ResolvedGroup(name="tenant-g", group=TENANT_GROUP), audio, chat_key="c")
        assert backend.dials == []


# ---------------------------------------------------------------------------
# 视频下载（插件 `analyze_video` 的链接、OneBot 视频预览的上游）：与 `download_file` 同一套建连接办法
# ---------------------------------------------------------------------------


VIDEO_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Type: video/mp4\r\n\r\nvideo"


class TestVideoDownloads:
    @pytest.fixture(autouse=True)
    def strict_download(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.core.os_env import OsEnv

        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", "")

    async def test_plugin_download_connects_to_the_checked_address(self, dns: FakeDNS, backend: FakeBackend, tmp_path: Path) -> None:
        from plugins.builtin.video_analysis import source

        dns.answer("vid.example.test", [PUBLIC_IP])
        backend.responses = [[VIDEO_RESPONSE]]
        await source.download_video("https://vid.example.test/a.mp4", tmp_path / "out", max_bytes=1024, max_redirects=0)

        assert (tmp_path / "out").read_bytes() == b"video"
        assert backend.dials == [(PUBLIC_IP, 443)]
        assert backend.tls_hostnames == ["vid.example.test"]

    async def test_plugin_download_refuses_a_rebound_answer_without_connecting(self, dns: FakeDNS, backend: FakeBackend, tmp_path: Path) -> None:
        """预检时是公网、建连接时改口成内网：一次都不连（原来会连上内网的 host:port、把 GET 发出去）"""
        from nekro_agent.tools.common_util import tenant_target_unavailable_message
        from plugins.builtin.video_analysis import source
        from plugins.builtin.video_analysis.media import VideoAnalysisError

        dns.answer("vid.example.test", [PUBLIC_IP], [PRIVATE_IP])
        with pytest.raises(VideoAnalysisError) as raised:
            await source.download_video("https://vid.example.test/a.mp4", tmp_path / "out", max_bytes=1024, max_redirects=0)

        assert str(raised.value) == tenant_target_unavailable_message("视频链接")
        assert backend.dials == []
        assert not (tmp_path / "out").exists()

    async def test_plugin_download_says_the_same_for_unresolvable_and_private(self, dns: FakeDNS, backend: FakeBackend, tmp_path: Path) -> None:
        """「解析不到」与「指向内网」同一句：否则任何群友都能拿它查 NA 所在网络的 DNS"""
        from plugins.builtin.video_analysis import source
        from plugins.builtin.video_analysis.media import VideoAnalysisError

        dns.answer("internal.example.test", [PRIVATE_IP])
        messages = []
        for host in ("internal.example.test", "missing.example.test"):
            with pytest.raises(VideoAnalysisError) as raised:
                await source.download_video(f"https://{host}/a.mp4", tmp_path / "out", max_bytes=1024, max_redirects=0)
            messages.append(str(raised.value))
        assert messages[0] == messages[1]
        assert backend.dials == []

    async def test_onebot_preview_upstream_connects_to_the_checked_address(self, dns: FakeDNS, backend: FakeBackend) -> None:
        from nekro_agent.adapters.onebot_v11.tools import video

        dns.answer("vid.example.test", [PUBLIC_IP])
        backend.responses = [[VIDEO_RESPONSE]]
        client, response = await video._open_upstream("https://vid.example.test/a.mp4", None)  # noqa: SLF001
        try:
            assert response.status_code == 200
        finally:
            await response.aclose()
            await client.aclose()
        assert backend.dials == [(PUBLIC_IP, 443)]
        assert backend.tls_hostnames == ["vid.example.test"]

    async def test_onebot_preview_upstream_refuses_a_rebound_answer_without_connecting(self, dns: FakeDNS, backend: FakeBackend) -> None:
        from nekro_agent.adapters.onebot_v11.tools import video

        dns.answer("vid.example.test", [PUBLIC_IP], [PRIVATE_IP])
        with pytest.raises(video.StaleVideoLocation):
            await video._open_upstream("https://vid.example.test/a.mp4", None)  # noqa: SLF001
        assert backend.dials == []


# ---------------------------------------------------------------------------
# 实例告警 webhook
# ---------------------------------------------------------------------------


class TestWebhook:
    async def test_tenant_webhook_connects_to_the_checked_address(self, dns: FakeDNS, backend: FakeBackend) -> None:
        from nekro_agent.services.notification.channels import WebhookChannel

        dns.answer("hook.example.test", [PUBLIC_IP])
        assert await WebhookChannel("https://hook.example.test/n", tenant_writable=True).send("t", "b", "warning")

        assert backend.dials == [(PUBLIC_IP, 443)]
        assert backend.tls_hostnames == ["hook.example.test"]
        assert "Host: hook.example.test\r\n" in backend.request_text()

    async def test_tenant_webhook_rebound_after_the_check_is_not_followed(self, dns: FakeDNS, backend: FakeBackend) -> None:
        """发送前那次校验看到公网，连接时 DNS 改口成内网：不连"""
        from nekro_agent.services.notification.channels import WebhookChannel

        dns.answer("hook.example.test", [PUBLIC_IP], [PRIVATE_IP])
        assert not await WebhookChannel("https://hook.example.test/n", tenant_writable=True).send("t", "b", "warning")
        assert backend.dials == []

    async def test_platform_webhook_is_unchanged(self, dns: FakeDNS, monkeypatch: pytest.MonkeyPatch) -> None:
        from nekro_agent.services.notification import channels

        def _boom(**_kwargs: Any) -> Any:
            raise AssertionError("平台方的地址不该走租户出站")

        monkeypatch.setattr(tenant_egress, "tenant_http_client", _boom)
        sent: List[str] = []

        class _Client:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            async def post(self, url: str, json: Any) -> Any:
                sent.append(url)
                return httpx.Response(200)

        monkeypatch.setattr(channels.httpx, "AsyncClient", _Client)
        dns.answer("hook.example.test", [PUBLIC_IP])
        assert await channels.WebhookChannel("https://hook.example.test/n").send("t", "b", "warning")
        assert sent == ["https://hook.example.test/n"]


# ---------------------------------------------------------------------------
# MCP 服务器校验
# ---------------------------------------------------------------------------


class TestMcpValidator:
    async def test_validator_client_connects_to_the_checked_address(self, dns: FakeDNS, backend: FakeBackend) -> None:
        from nekro_agent.services.mcp.validator import _public_only_client_factory

        dns.answer("mcp.example.test", [PUBLIC_IP])
        client = _public_only_client_factory(5)(headers=None, timeout=None, auth=None)
        async with client:
            assert isinstance(client._transport, TenantHTTPTransport)  # noqa: SLF001
            await client.get("https://mcp.example.test/mcp")

        assert backend.dials == [(PUBLIC_IP, 443)]
        assert backend.tls_hostnames == ["mcp.example.test"]

    async def test_validator_client_refuses_a_rebound_address(self, dns: FakeDNS, backend: FakeBackend) -> None:
        """请求钩子那次校验看到公网，建连接时 DNS 改口：建连接那一步拒绝"""
        from nekro_agent.services.mcp.validator import _public_only_client_factory

        dns.answer("mcp.example.test", [PUBLIC_IP], [PRIVATE_IP])
        client = _public_only_client_factory(5)(headers=None, timeout=None, auth=None)
        async with client:
            with pytest.raises(httpx.ConnectError):
                await client.get("https://mcp.example.test/mcp")
        assert backend.dials == []


# ---------------------------------------------------------------------------
# 邮箱
# ---------------------------------------------------------------------------


def test_email_uses_the_shared_socket_helpers() -> None:
    from nekro_agent.adapters.email.clients import imap_smtp_password

    assert imap_smtp_password._connect_pinned is tenant_egress.connect_pinned  # noqa: SLF001
    assert imap_smtp_password._connect_via_http_proxy is tenant_egress.connect_via_http_proxy  # noqa: SLF001


def test_ok_response_constant_is_shared() -> None:
    assert OK_RESPONSE.startswith(b"HTTP/1.1 200")
