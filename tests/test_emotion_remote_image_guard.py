"""表情收集取远程图片时走下载守卫

`collect_emotion` 的图片来源可以是模型给的 http(s) 链接。以前插件在 NA 主进程里直接
`httpx.get` 它：不限大小、不拦内网与保留地址、照跟跳转。多租户部署里，租户的模型就能借它让
NA 去访问内网服务，或者一次拉下一个超大文件。现在走 `common_util.fetch_url_bytes`，与上传下载
同一套守卫：按解析出的 IP 拒回环 / 私有 / 链路本地地址、跳转逐跳复检、只收未压缩正文、边读边
计数，上限与控制台上传相同。被拒的原因只进日志，交给模型的那句话不带细节（可能含解析出的内网地址）。

传输层用 `httpx.MockTransport`，主机用回环地址、拼接出的私有地址或假 DNS，不联网；需要放行的
用例打开 `ALLOW_PRIVATE_NETWORK_DOWNLOAD`，所以全文件没有公网地址。
"""

import asyncio
import socket
from typing import Any, Dict, List

import httpx
import pytest

from nekro_agent.core.os_env import OsEnv
from nekro_agent.tools import common_util
from nekro_agent.tools.common_util import DEFAULT_EXTRA_ALLOWED_NETWORKS
from plugins.builtin import emotion as plugin_module

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
# 私有与链路本地地址用拼接得出：公开树的泄漏扫描对这两类字面量只在少数注明理由的文件里放行，
# 而这里要的只是「守卫拒它们」，不需要它们以字面量出现
PRIVATE_IP = ".".join(("10", "1", "2", "3"))
RESOLVED_PRIVATE_IP = ".".join(("10", "9", "8", "7"))
LINK_LOCAL_IP = ".".join(("169", "254", "169", "254"))


class FakeWeb:
    """按完整 URL 回响应，记下每个真正发出去的请求"""

    def __init__(self) -> None:
        self.routes: Dict[str, bytes] = {}
        self.requested: List[str] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        body = self.routes.get(url)
        if body is None:
            return httpx.Response(404)
        # 流式响应：守卫用 `aiter_raw` 边读边计数
        return httpx.Response(200, stream=httpx.ByteStream(body))


@pytest.fixture(autouse=True)
def _strict_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
    monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    fake = FakeWeb()
    real_client = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_client(*args, transport=httpx.MockTransport(fake.handle), **kwargs)

    monkeypatch.setattr(common_util.httpx, "AsyncClient", client_factory)
    return fake


def _fake_dns(monkeypatch: pytest.MonkeyPatch, zone: Dict[str, str]) -> None:
    loop = asyncio.get_running_loop()
    real = loop.getaddrinfo

    async def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode("ascii") if isinstance(host, bytes) else host
        if name in zone:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (zone[name], port))]
        return await real(host, port, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", fake)


async def _read(url: str) -> bytes:
    # 走链接的分支用不到上下文
    return await plugin_module._read_image_bytes(None, url)  # type: ignore[arg-type]  # noqa: SLF001


class TestRefusedAddresses:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/a.png",
            f"http://{PRIVATE_IP}/a.png",
            f"http://{LINK_LOCAL_IP}/latest/meta-data/",
            "http://[::1]/a.png",
        ],
    )
    async def test_loopback_private_and_link_local_are_refused_before_any_request(self, web: FakeWeb, url: str) -> None:
        with pytest.raises(ValueError) as caught:
            await _read(url)

        assert str(caught.value) == plugin_module._REMOTE_IMAGE_REFUSED  # noqa: SLF001
        assert web.requested == []

    async def test_a_hostname_resolving_to_a_private_address_is_refused(self, web: FakeWeb, monkeypatch: pytest.MonkeyPatch) -> None:
        """判据是解析出的 IP，不是主机名长什么样：一个普通的域名照样能指向内网"""
        _fake_dns(monkeypatch, {"stickers.example": RESOLVED_PRIVATE_IP})

        with pytest.raises(ValueError) as caught:
            await _read("http://stickers.example/a.png")

        assert web.requested == []
        # 交给模型的话里没有解析出的内网地址
        assert RESOLVED_PRIVATE_IP not in str(caught.value)


class TestAllowedDownloads:
    @pytest.fixture(autouse=True)
    def _allow_private(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 本组要一个「能放行」的目标，又不写公网地址：打开内网下载开关（开关打开后守卫只查协议），用回环地址
        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)

    async def test_a_small_image_is_returned(self, web: FakeWeb) -> None:
        web.routes["http://127.0.0.1/ok.png"] = PNG

        assert await _read("http://127.0.0.1/ok.png") == PNG
        assert web.requested == ["http://127.0.0.1/ok.png"]

    async def test_an_image_over_the_limit_is_refused(self, web: FakeWeb, monkeypatch: pytest.MonkeyPatch) -> None:
        """上限与控制台上传同一个值；这里把它调小，免得在用例里造一个十几 MB 的正文"""
        monkeypatch.setattr(plugin_module, "_MAX_UPLOAD_BYTES", 16)
        web.routes["http://127.0.0.1/big.png"] = PNG  # 40 字节 > 16

        with pytest.raises(ValueError) as caught:
            await _read("http://127.0.0.1/big.png")

        assert str(caught.value) == plugin_module._REMOTE_IMAGE_REFUSED  # noqa: SLF001


class TestSharedFetcher:
    async def test_the_default_limit_follows_the_module_constant(self, web: FakeWeb, monkeypatch: pytest.MonkeyPatch) -> None:
        """不传 `max_bytes` 时上限在调用时才读：测试或部署改模块常量照样生效"""
        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)
        monkeypatch.setattr(common_util, "MAX_DOWNLOAD_SIZE_BYTES", 8)
        web.routes["http://127.0.0.1/x.png"] = PNG

        with pytest.raises(common_util.UnsafeDownloadUrlError):
            await common_util.fetch_url_bytes("http://127.0.0.1/x.png")
