"""服务端 MCP 校验的出站防护与 stdio 不回退

判据沿用模型组 `base_url` 那一套：租户身份强制公网（不认 `ALLOW_PRIVATE_NETWORK_DOWNLOAD`），
平台方豁免。这里钉住的是「租户点测试时不会从 NA 主机发起一次打内网的请求」，以及
「stdio 指定了工作区就只在它自己的容器里跑」。
"""

from typing import Any, List

import pytest

from nekro_agent.services.mcp import validator
from nekro_agent.services.mcp.schemas import McpServerConfig
from nekro_agent.services.mcp.validator import validate_server
from nekro_agent.tools.common_util import UnsafeDownloadUrlError, assert_tenant_connected_peer_safe


def _http_server(url: str) -> McpServerConfig:
    return McpServerConfig(name="probe", type="http", url=url)


@pytest.fixture
def no_real_connection(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """任何真的发起连接的尝试都被记下来并判失败——被拦的用例必须一次都没连"""
    attempts: List[str] = []

    def fake_client(*_args: Any, **_kwargs: Any) -> Any:
        attempts.append("client_created")
        raise AssertionError("被出站防护拦下的目标不该建立连接")

    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", fake_client)
    monkeypatch.setattr("mcp.client.sse.sse_client", fake_client)
    return attempts


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8021/api/mcp/other",
        "http://10.0.0.1:8080/mcp",
        "http://192.168.1.10/mcp",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]:8021/mcp",
        "http://[::ffff:127.0.0.1]/mcp",
        "http://0.0.0.0/mcp",
    ],
)
async def test_tenant_targets_inside_the_network_are_refused_before_connecting(
    url: str,
    no_real_connection: List[str],
) -> None:
    result = await validate_server(None, _http_server(url), restrict_to_public_network=True)

    assert result.ok is False
    assert result.error_kind == "url_not_allowed"
    assert no_real_connection == []


async def test_domain_resolving_inside_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    no_real_connection: List[str],
) -> None:
    """域名本身合法，解析结果落在内网——必须在连接前拦"""
    import socket

    async def fake_getaddrinfo(*_args: Any, **_kwargs: Any) -> List[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 80))]

    monkeypatch.setattr("nekro_agent.tools.common_util._system_getaddrinfo", fake_getaddrinfo)

    result = await validate_server(None, _http_server("http://internal.example.com/mcp"), restrict_to_public_network=True)

    assert result.ok is False
    assert result.error_kind == "url_not_allowed"
    assert no_real_connection == []


async def test_platform_owner_is_not_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台方把 MCP 服务放在内网是正常用法；不设限时校验会真的去连（这里用一个假握手证明它走下去了）"""
    reached: List[str] = []

    class FakeStream:
        pass

    def fake_client(url: str, **_kwargs: Any) -> Any:
        reached.append(url)

        class _Ctx:
            async def __aenter__(self) -> Any:
                return (FakeStream(), FakeStream(), lambda: None)

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        return _Ctx()

    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", fake_client)
    monkeypatch.setattr(validator, "_ok", lambda *_args: validator._fail("transport_error", "stopped", 0.0))

    result = await validate_server(
        None,
        _http_server("http://127.0.0.1:18080/mcp"),
        restrict_to_public_network=False,
    )

    assert reached == ["http://127.0.0.1:18080/mcp"]
    assert result.error_kind != "url_not_allowed"


def test_default_is_restricted() -> None:
    """默认值必须落在安全一侧：少传一个关键字不能变成「谁都放行」"""
    import inspect

    signature = inspect.signature(validate_server)

    assert signature.parameters["restrict_to_public_network"].default is True


def test_peer_guard_rejects_private_peer() -> None:
    with pytest.raises(UnsafeDownloadUrlError):
        assert_tenant_connected_peer_safe("10.0.0.5")


def test_peer_guard_rejects_missing_peer() -> None:
    with pytest.raises(UnsafeDownloadUrlError):
        assert_tenant_connected_peer_safe(None)


def test_peer_guard_accepts_public_peer() -> None:
    assert_tenant_connected_peer_safe("93.184.216.34")


def test_peer_guard_normalizes_ipv4_mapped_ipv6() -> None:
    with pytest.raises(UnsafeDownloadUrlError):
        assert_tenant_connected_peer_safe("::ffff:192.168.0.1")


def test_peer_guard_ignores_private_download_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ALLOW_PRIVATE_NETWORK_DOWNLOAD` 是部署方对自己下载链路的授权，顺延给租户就是提权"""
    monkeypatch.setattr("nekro_agent.tools.common_util.OsEnv.ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)

    with pytest.raises(UnsafeDownloadUrlError):
        assert_tenant_connected_peer_safe("10.0.0.5")


def test_client_factory_disables_redirects() -> None:
    factory = validator._public_only_client_factory(5.0)

    client = factory()

    assert client.follow_redirects is False
    assert client.trust_env is False


def test_client_factory_accepts_the_sdk_keyword_names() -> None:
    """SDK 按关键字调工厂；形参改名会直接 `TypeError`，测试里不会暴露，线上才会"""
    import httpx

    factory = validator._public_only_client_factory(5.0)

    client = factory(headers={"X-A": "b"}, timeout=httpx.Timeout(7.0), auth=None)

    assert client.headers["X-A"] == "b"
    assert client.timeout.connect == 7.0


async def test_client_factory_guards_each_request() -> None:
    """SSE 握手后服务端会下发一个 POST 地址；那个地址同样要过校验"""
    factory = validator._public_only_client_factory(5.0)
    client = factory()

    with pytest.raises(UnsafeDownloadUrlError):
        await client.send(client.build_request("POST", "http://127.0.0.1:1234/messages"))

    await client.aclose()


async def test_request_guard_blocks_before_the_socket() -> None:
    """钩子在发送前抛，连接不会建立：用一个必然失败的地址也能得到「被拦」而不是网络错误"""
    factory = validator._public_only_client_factory(1.0)
    client = factory()

    with pytest.raises(UnsafeDownloadUrlError):
        await client.get("http://169.254.169.254/latest/meta-data/")

    await client.aclose()


def test_unsafe_url_error_is_found_through_exception_group() -> None:
    """mcp SDK / anyio 会把传输层异常包几层；被拦必须仍归到 url_not_allowed"""
    inner = UnsafeDownloadUrlError("指向非公网地址")
    try:
        try:
            raise inner
        except UnsafeDownloadUrlError as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError as outer:
        assert validator._find_unsafe_url_error(outer) is inner


def test_no_unsafe_error_in_unrelated_exception() -> None:
    assert validator._find_unsafe_url_error(RuntimeError("boom")) is None


async def test_stdio_without_container_does_not_borrow_another_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式指定了工作区、但它没起容器：直接 container_not_running，不许去别的工作区 exec"""
    borrowed: List[str] = []

    async def fake_pick() -> Any:
        borrowed.append("picked")
        return None

    monkeypatch.setattr(validator, "_pick_running_workspace", fake_pick)

    class FakeWorkspace:
        id = 7
        container_name = ""

    server = McpServerConfig(name="probe", type="stdio", command="npx", args=["-y", "some-pkg"])

    result = await validate_server(FakeWorkspace(), server)

    assert result.ok is False
    assert result.error_kind == "container_not_running"
    assert borrowed == []


async def test_global_stdio_validation_may_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台方的全局测试（不传工作区）保留兜底，这是既有行为"""

    async def fake_pick() -> Any:
        return None

    monkeypatch.setattr(validator, "_pick_running_workspace", fake_pick)

    server = McpServerConfig(name="probe", type="stdio", command="npx", args=["-y", "some-pkg"])

    result = await validate_server(None, server)

    assert result.ok is False
    assert result.error_kind == "container_not_running"


async def test_web_chat_entry_is_validated_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """内置条目不走网络：平台方测试它时只需确认适配器在、工具列得出来"""
    from nekro_agent.services.mcp import web_chat_auth

    def no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("内置网页聊天条目不该发起网络连接")

    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", no_network)

    server = McpServerConfig(
        id=web_chat_auth.WEB_CHAT_MCP_SERVER_ID,
        name=web_chat_auth.WEB_CHAT_MCP_SERVER_NAME,
        type="http",
        url=web_chat_auth.WEB_CHAT_MCP_URL,
    )

    result = await validate_server(None, server, restrict_to_public_network=False)

    if result.ok:
        assert result.server_info is not None
        assert result.tools
    else:
        # 适配器未注册/未加载时也算「校验失败但没联网」，这正是进程内校验的语义
        assert result.error_kind == "transport_error"


@pytest.mark.parametrize(
    "server",
    [
        McpServerConfig(name="NekroWebChatTest", type="http", url="http://host.docker.internal:8021/api/mcp/web-chat/mcp"),
        McpServerConfig(name="probe", type="http", url="http://10.0.0.1/api/mcp/web-chat/mcp"),
        McpServerConfig(id="nekro-web-chat", name="probe", type="http", url="http://127.0.0.1:8021/x"),
    ],
)
async def test_tenant_cannot_use_the_in_process_shortcut(
    server: McpServerConfig,
    monkeypatch: pytest.MonkeyPatch,
    no_real_connection: List[str],
) -> None:
    """租户提交一条名字 / id / URL 对得上内置条目的配置：不走进程内自检，按普通 http 过出站校验

    否则租户能借此读到网页聊天是否启用、有哪些工具，而网页聊天整块是平台方专属。
    """

    async def in_process_must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("受限身份不该走进程内自检")

    monkeypatch.setattr(validator, "_validate_web_chat_in_process", in_process_must_not_run)

    async def resolve_inside(host: str, port: int, **_kwargs: Any) -> List[Any]:
        return [(2, 1, 6, "", ("172.17.0.1", port))]

    monkeypatch.setattr("nekro_agent.tools.common_util._system_getaddrinfo", resolve_inside)

    result = await validate_server(None, server, restrict_to_public_network=True)

    assert result.ok is False
    assert result.error_kind == "url_not_allowed"
    assert result.tools is None
    assert no_real_connection == []
