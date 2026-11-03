"""`download_file` 手动跟随跳转的回归测试

QQ 个性 / 超级表情（`gxh.vip.qq.com/.../raw300.gif`）返回 302，过去 `follow_redirects=False` 让它们全部下载失败。
改成手动跟随之后要守住的契约：

- 每一跳的目标在发请求之前都过 `assert_download_url_safe`：跳转到内网 / 回环 / 非 http(s) 一律拒绝，
  而且那一跳**一次请求都不发**；
- 最多跟 3 次跳转，超过就失败；
- 相对 `Location` 相对当前这一跳解析；
- 跳转之后的正文仍受大小上限约束，跳转响应本身的正文不读；
- 这些确定性失败不重试。

传输层用 `httpx.MockTransport`，主机全用 IP 字面量（`getaddrinfo` 不查 DNS），测试不联网。
"""

import asyncio
import gzip
import inspect
import socket
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Union

import httpx
import pytest

from nekro_agent.core.os_env import OsEnv
from nekro_agent.tools import common_util
from nekro_agent.tools.common_util import (
    DEFAULT_EXTRA_ALLOWED_NETWORKS,
    MAX_DOWNLOAD_REDIRECTS,
    UnsafeDownloadUrlError,
    download_file,
)

GIF = b"GIF89a-fake-image"
FIRST = "http://8.8.8.8/club/item/parcel/item/88/raw300.gif"
CDN = "https://1.1.1.1/cdn/raw300.gif"

Route = Union[httpx.Response, Callable[[httpx.Request], Any]]


def _redirect(location: str, status: int = 302, **kwargs: Any) -> httpx.Response:
    return httpx.Response(status, headers={"location": location}, **kwargs)


class FakeWeb:
    """按完整 URL 回响应，记下每个真正发出去的请求；没登记的 URL 一律 404"""

    def __init__(self, routes: Dict[str, Route]) -> None:
        self.routes = routes
        self.requested: List[str] = []
        self.client_kwargs: List[Dict[str, Any]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        route = self.routes.get(url)
        if route is None:
            return httpx.Response(404)
        response = route(request) if callable(route) else route
        if inspect.isawaitable(response):
            response = await response
        try:
            body = response.content
        except httpx.ResponseNotRead:
            return response  # 本来就是流式的
        # `Response(content=...)` 构造时就读完了，`aiter_raw` 读不到；每次请求换一份没读过的流式响应，和真实网络一样
        return httpx.Response(response.status_code, headers=response.headers, stream=httpx.ByteStream(body))


InstallWeb = Callable[[Dict[str, Route]], FakeWeb]


@pytest.fixture(autouse=True)
def _strict_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
    monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", DEFAULT_EXTRA_ALLOWED_NETWORKS)


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> InstallWeb:
    """把 `download_file` 建的客户端换成走假传输层的，其余参数（含 `follow_redirects`）原样保留"""
    real_client = httpx.AsyncClient

    def install(routes: Dict[str, Route]) -> FakeWeb:
        fake = FakeWeb(routes)

        def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            fake.client_kwargs.append(dict(kwargs))
            return real_client(*args, transport=httpx.MockTransport(fake.handle), **kwargs)

        monkeypatch.setattr(common_util.httpx, "AsyncClient", client_factory)
        return fake

    return install


def _fake_dns(monkeypatch: pytest.MonkeyPatch, zone: Dict[str, str]) -> List[str]:
    """让当前事件循环的 getaddrinfo 按 zone 回答（只认 ASCII 主机名）；不在 zone 里的交给真实实现，用例里只会是 IP 字面量"""
    looked_up: List[str] = []
    loop = asyncio.get_running_loop()
    real = loop.getaddrinfo

    async def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        name = host.decode("ascii") if isinstance(host, bytes) else host
        looked_up.append(name)
        if name in zone:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (zone[name], port))]
        return await real(host, port, *args, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", fake)
    return looked_up


async def _download(tmp_path: Path, url: str = FIRST, retry_count: int = 3) -> Path:
    dest = tmp_path / "out.gif"
    await download_file(url, file_path=str(dest), use_suffix=".gif", retry_count=retry_count)
    return dest


class TestFollowsRedirects:
    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    async def test_a_redirect_to_a_public_host_is_downloaded(self, tmp_path: Path, web: InstallWeb,status: int) -> None:
        fake = web({FIRST: _redirect(CDN, status), CDN: httpx.Response(200, content=GIF)})
        dest = await _download(tmp_path)
        assert dest.read_bytes() == GIF
        assert fake.requested == [FIRST, CDN]
        # 跳转必须由我们自己跟：交给 httpx 跟，跳转目标就不再过校验
        assert fake.client_kwargs and all(kwargs.get("follow_redirects") is False for kwargs in fake.client_kwargs)

    async def test_every_hop_is_checked_before_it_is_requested(self, tmp_path: Path, web: InstallWeb,monkeypatch: pytest.MonkeyPatch) -> None:
        hop2 = "http://8.8.8.8/hop2/b.gif"
        fake = web({FIRST: _redirect(CDN), CDN: _redirect(hop2), hop2: httpx.Response(200, content=GIF)})
        checked: List[str] = []
        real_guard = common_util.assert_download_url_safe

        async def spy(url: str) -> None:
            checked.append(url)
            await real_guard(url)

        monkeypatch.setattr(common_util, "assert_download_url_safe", spy)
        await _download(tmp_path)
        # 校验的字符串就是随后请求的字符串，一一对应、同样的顺序
        assert checked == fake.requested == [FIRST, CDN, hop2]

    async def test_the_body_of_a_redirect_response_is_not_read(self, tmp_path: Path, web: InstallWeb,monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(common_util, "MAX_DOWNLOAD_SIZE_BYTES", 32)
        fake = web({FIRST: _redirect(CDN, content=b"x" * 4096), CDN: httpx.Response(200, content=GIF)})
        dest = await _download(tmp_path)
        assert dest.read_bytes() == GIF
        assert fake.requested == [FIRST, CDN]

    async def test_cookies_set_by_one_host_are_not_sent_to_another(self, tmp_path: Path, web: InstallWeb) -> None:
        seen: List[str] = []

        def cdn(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("cookie", ""))
            return httpx.Response(200, content=GIF)

        web({FIRST: httpx.Response(302, headers={"location": CDN, "set-cookie": "sid=from-first-hop; Path=/"}), CDN: cdn})
        await _download(tmp_path)
        assert seen == [""]

    async def test_a_failed_retry_walks_the_chain_again_from_the_start(self, tmp_path: Path, web: InstallWeb) -> None:
        fake = web({FIRST: _redirect(CDN), CDN: httpx.Response(503)})
        with pytest.raises(httpx.HTTPStatusError):
            await _download(tmp_path, retry_count=1)
        assert fake.requested == [FIRST, CDN, FIRST, CDN]


class TestRedirectIntoTheInternalNetwork:
    """最重要的一组：首跳是公网，302 到内网。目标那一跳不能发出任何请求，也不重试"""

    @pytest.mark.parametrize(
        ("location", "why"),
        [
            ("http://127.0.0.1:8021/api/config", "回环：NA 自己的内部接口"),
            ("http://[::1]:8021/api", "IPv6 回环"),
            ("http://10.0.0.5/admin", "私网 A 段"),
            ("http://172.16.0.5/admin", "私网 B 段"),
            ("http://192.168.1.1/", "私网 C 段"),
            ("http://169.254.169.254/latest/meta-data/", "云元数据"),
            ("http://0.0.0.0:8021/", "未指定地址"),
            ("http://100.64.0.1/", "CGNAT"),
            ("http://[fd00::1]/x", "IPv6 唯一本地"),
            ("http://[::ffff:127.0.0.1]/x", "IPv4 映射的回环"),
            ("http://[64:ff9b::7f00:1]/x", "NAT64 嵌着 127.0.0.1"),
            ("//127.0.0.1:8021/api/config", "协议相对：沿用当前协议，主机换成回环"),
            ("http://8.8.8.8@127.0.0.1/x", "userinfo 里放公网地址，真正的主机是回环"),
            ("http://127.0.0.1#@8.8.8.8/x", "fragment 里放公网地址，真正的主机是回环"),
        ],
    )
    async def test_is_refused_without_touching_the_target(self, tmp_path: Path, web: InstallWeb,location: str, why: str) -> None:
        fake = web({FIRST: _redirect(location)})
        with pytest.raises(UnsafeDownloadUrlError):
            await _download(tmp_path)
        assert fake.requested == [FIRST]
        assert not (tmp_path / "out.gif").exists()

    @pytest.mark.parametrize("label", ["ß", "ς"])
    async def test_a_host_spelled_differently_by_idna_2003_and_2008_is_checked_as_httpx_spells_it(
        self, tmp_path: Path, web: InstallWeb, monkeypatch: pytest.MonkeyPatch, label: str
    ) -> None:
        """`ß` 按 IDNA 2003 是 `ss`、按 IDNA 2008 是 `xn--zca`：校验必须解析 httpx 真正要连的那个"""
        public_spelling = label.encode("idna").decode("ascii")  # Python 自带编码（IDNA 2003）
        connect_spelling = httpx.URL(f"http://{label}.attacker.example/").raw_host.decode("ascii")
        assert public_spelling != connect_spelling
        looked_up = _fake_dns(
            monkeypatch,
            {f"{public_spelling}.attacker.example": "8.8.8.8", connect_spelling: "127.0.0.1"},
        )
        location = f"http://{label}.attacker.example/latest/meta-data/".encode("utf-8")
        fake = web({FIRST: httpx.Response(302, headers=[(b"location", location)])})
        with pytest.raises(UnsafeDownloadUrlError):
            await _download(tmp_path)
        assert fake.requested == [FIRST]
        assert connect_spelling in looked_up
        assert f"{public_spelling}.attacker.example" not in looked_up

    async def test_is_refused_at_a_later_hop_too(self, tmp_path: Path, web: InstallWeb) -> None:
        fake = web({FIRST: _redirect(CDN), CDN: _redirect("http://169.254.169.254/latest/meta-data/")})
        with pytest.raises(UnsafeDownloadUrlError):
            await _download(tmp_path)
        assert fake.requested == [FIRST, CDN]

    async def test_the_escape_hatch_applies_to_redirects_like_the_first_hop(self, tmp_path: Path, web: InstallWeb,monkeypatch: pytest.MonkeyPatch) -> None:
        """部署方显式放行内网时，跳转目标与首跳同一判据（不额外收紧，也不额外放宽）"""
        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)
        internal = "http://10.0.0.5:6099/image.gif"
        fake = web({FIRST: _redirect(internal), internal: httpx.Response(200, content=GIF)})
        dest = await _download(tmp_path)
        assert dest.read_bytes() == GIF
        assert fake.requested == [FIRST, internal]


class TestRedirectLimit:
    @staticmethod
    def _chain(length: int) -> Dict[str, Route]:
        """/0 → /1 → … → /length，最后一个回 200"""
        routes: Dict[str, Route] = {f"http://8.8.8.8/{i}": _redirect(f"http://8.8.8.8/{i + 1}") for i in range(length)}
        routes[f"http://8.8.8.8/{length}"] = httpx.Response(200, content=GIF)
        return routes

    def test_the_limit_is_three(self) -> None:
        assert MAX_DOWNLOAD_REDIRECTS == 3

    async def test_three_redirects_are_followed(self, tmp_path: Path, web: InstallWeb) -> None:
        fake = web(self._chain(3))
        dest = await _download(tmp_path, "http://8.8.8.8/0")
        assert dest.read_bytes() == GIF
        assert len(fake.requested) == 4

    async def test_a_fourth_redirect_fails_without_retrying(self, tmp_path: Path, web: InstallWeb) -> None:
        fake = web(self._chain(4))
        with pytest.raises(UnsafeDownloadUrlError, match="跳转超过 3 次"):
            await _download(tmp_path, "http://8.8.8.8/0")
        assert fake.requested == [f"http://8.8.8.8/{i}" for i in range(4)]
        assert not (tmp_path / "out.gif").exists()

    async def test_a_redirect_loop_fails(self, tmp_path: Path, web: InstallWeb) -> None:
        fake = web({FIRST: _redirect(FIRST)})
        with pytest.raises(UnsafeDownloadUrlError, match="跳转超过 3 次"):
            await _download(tmp_path)
        assert fake.requested == [FIRST] * 4


class TestRelativeLocation:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            ("/cdn/a.gif", "http://8.8.8.8/cdn/a.gif"),
            ("b.gif", "http://8.8.8.8/club/item/parcel/item/88/b.gif"),
            ("../89/c.gif", "http://8.8.8.8/club/item/parcel/item/89/c.gif"),
            ("?size=300", "http://8.8.8.8/club/item/parcel/item/88/raw300.gif?size=300"),
            ("//1.1.1.1/d.gif", "http://1.1.1.1/d.gif"),
        ],
    )
    async def test_is_resolved_against_the_current_url(self, tmp_path: Path, web: InstallWeb,location: str, expected: str) -> None:
        fake = web({FIRST: _redirect(location), expected: httpx.Response(200, content=GIF)})
        dest = await _download(tmp_path)
        assert dest.read_bytes() == GIF
        assert fake.requested == [FIRST, expected]

    async def test_is_resolved_against_the_hop_that_sent_it_not_the_first_url(self, tmp_path: Path, web: InstallWeb) -> None:
        final = "https://1.1.1.1/cdn/e.gif"
        fake = web({FIRST: _redirect(CDN), CDN: _redirect("e.gif"), final: httpx.Response(200, content=GIF)})
        dest = await _download(tmp_path)
        assert dest.read_bytes() == GIF
        assert fake.requested == [FIRST, CDN, final]


class TestNonHttpRedirect:
    @pytest.mark.parametrize(
        "location",
        [
            "file:///etc/passwd",
            "file://localhost/c:/windows/win.ini",
            "ftp://8.8.8.8/x",
            "gopher://127.0.0.1:6379/_INFO",
            "dict://127.0.0.1:6379/info",
            "javascript:alert(1)",
            "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
        ],
    )
    async def test_is_refused(self, tmp_path: Path, web: InstallWeb,location: str) -> None:
        fake = web({FIRST: _redirect(location)})
        with pytest.raises(UnsafeDownloadUrlError):
            await _download(tmp_path)
        assert fake.requested == [FIRST]

    @pytest.mark.parametrize("location", ["http://[::1", "http://127.0.0.1:99999/x"])
    async def test_an_unparsable_location_is_never_followed(self, tmp_path: Path, web: InstallWeb, location: str) -> None:
        fake = web({FIRST: _redirect(location)})
        with pytest.raises((httpx.HTTPError, UnsafeDownloadUrlError, ValueError)):
            await _download(tmp_path, retry_count=0)
        assert fake.requested == [FIRST]

    async def test_is_refused_even_with_the_escape_hatch(self, tmp_path: Path, web: InstallWeb,monkeypatch: pytest.MonkeyPatch) -> None:
        """逃生口只放宽地址范围，不放宽协议白名单"""
        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)
        fake = web({FIRST: _redirect("file:///etc/passwd")})
        with pytest.raises(UnsafeDownloadUrlError):
            await _download(tmp_path)
        assert fake.requested == [FIRST]


class TestSizeLimitAfterRedirect:
    async def test_declared_length_over_the_limit_is_refused(self, tmp_path: Path, web: InstallWeb,monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(common_util, "MAX_DOWNLOAD_SIZE_BYTES", 16)
        fake = web({FIRST: _redirect(CDN), CDN: httpx.Response(200, content=b"x" * 17)})
        with pytest.raises(UnsafeDownloadUrlError, match="大小上限"):
            await _download(tmp_path)
        assert fake.requested == [FIRST, CDN]
        assert not (tmp_path / "out.gif").exists()

    async def test_streamed_body_over_the_limit_is_refused(self, tmp_path: Path, web: InstallWeb,monkeypatch: pytest.MonkeyPatch) -> None:
        """不带 Content-Length 的分块正文，靠边读边计数拦住"""
        monkeypatch.setattr(common_util, "MAX_DOWNLOAD_SIZE_BYTES", 16)

        async def body() -> AsyncIterator[bytes]:
            for _ in range(4):
                yield b"x" * 8

        def chunked(_request: httpx.Request) -> httpx.Response:
            response = httpx.Response(200, content=body())
            assert "content-length" not in response.headers
            return response

        fake = web({FIRST: _redirect(CDN), CDN: chunked})
        with pytest.raises(UnsafeDownloadUrlError, match="大小上限"):
            await _download(tmp_path)
        assert fake.requested == [FIRST, CDN]
        assert not (tmp_path / "out.gif").exists()

    async def test_a_body_at_the_limit_is_accepted(self, tmp_path: Path, web: InstallWeb,monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(common_util, "MAX_DOWNLOAD_SIZE_BYTES", 16)
        web({FIRST: _redirect(CDN), CDN: httpx.Response(200, content=b"x" * 16)})
        dest = await _download(tmp_path)
        assert dest.read_bytes() == b"x" * 16


class TestCompressedBody:
    """httpx 解压不限输出长度，压缩正文能在一次读里展开到计数拦不住的大小，所以只收 identity"""

    async def test_identity_is_requested_on_every_hop(self, tmp_path: Path, web: InstallWeb) -> None:
        seen: List[str] = []

        def recorded(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request.headers.get("accept-encoding", ""))
                return response

            return handler

        web({FIRST: recorded(_redirect(CDN)), CDN: recorded(httpx.Response(200, content=GIF))})
        await _download(tmp_path)
        assert seen == ["identity", "identity"]

    @pytest.mark.parametrize("encoding", ["gzip", "gzip, gzip", "x-gzip", "deflate", "br", "zstd"])
    async def test_a_compressed_body_is_refused_without_retrying(self, tmp_path: Path, web: InstallWeb, encoding: str) -> None:
        bomb = gzip.compress(gzip.compress(b"\0" * 4_000_000))

        def compressed(_request: httpx.Request) -> httpx.Response:
            # stream=：不让 httpx 在构造响应时就按 Content-Encoding 解压
            headers = {"content-encoding": encoding, "content-length": str(len(bomb))}
            return httpx.Response(200, headers=headers, stream=httpx.ByteStream(bomb))

        fake = web({FIRST: _redirect(CDN), CDN: compressed})
        with pytest.raises(UnsafeDownloadUrlError, match="压缩编码"):
            await _download(tmp_path)
        assert fake.requested == [FIRST, CDN]
        assert not (tmp_path / "out.gif").exists()

    @pytest.mark.parametrize("encoding", ["identity", "Identity "])
    async def test_identity_encoding_is_accepted(self, tmp_path: Path, web: InstallWeb, encoding: str) -> None:
        web({FIRST: httpx.Response(200, content=GIF, headers={"content-encoding": encoding})})
        dest = await _download(tmp_path)
        assert dest.read_bytes() == GIF


class TestChainDeadline:
    """从首跳发出到最终响应头到达，整条链共用一个时限；读正文不算在里面"""

    async def test_slow_hops_share_one_deadline(self, tmp_path: Path, web: InstallWeb, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(common_util, "DOWNLOAD_TIMEOUT_SECONDS", 1.0)

        def slow(response: httpx.Response) -> Callable[[httpx.Request], Any]:
            async def handler(_request: httpx.Request) -> httpx.Response:
                await asyncio.sleep(0.4)  # 单跳远小于时限，三跳就超了
                return response

            return handler

        routes: Dict[str, Route] = {f"http://8.8.8.8/{i}": slow(_redirect(f"http://8.8.8.8/{i + 1}")) for i in range(3)}
        routes["http://8.8.8.8/3"] = httpx.Response(200, content=GIF)
        fake = web(routes)
        with pytest.raises(TimeoutError):
            await _download(tmp_path, "http://8.8.8.8/0", retry_count=0)
        assert len(fake.requested) < 4
        assert not (tmp_path / "out.gif").exists()

    async def test_reading_the_body_is_not_bound_by_it(self, tmp_path: Path, web: InstallWeb, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(common_util, "DOWNLOAD_TIMEOUT_SECONDS", 1.0)

        async def body() -> AsyncIterator[bytes]:
            for _ in range(3):
                await asyncio.sleep(0.5)
                yield b"x"

        web({FIRST: _redirect(CDN), CDN: lambda _request: httpx.Response(200, content=body())})
        dest = await _download(tmp_path, retry_count=0)
        assert dest.read_bytes() == b"xxx"


class TestRedirectsThatAreNotFollowed:
    async def test_a_redirect_without_location_fails_without_retrying(self, tmp_path: Path, web: InstallWeb) -> None:
        fake = web({FIRST: httpx.Response(302)})
        with pytest.raises(UnsafeDownloadUrlError, match="没有目标地址"):
            await _download(tmp_path)
        assert fake.requested == [FIRST]

    @pytest.mark.parametrize("status", [300, 304])
    async def test_other_3xx_are_failures(self, tmp_path: Path, web: InstallWeb,status: int) -> None:
        fake = web({FIRST: _redirect(CDN, status), CDN: httpx.Response(200, content=GIF)})
        with pytest.raises(httpx.HTTPStatusError):
            await _download(tmp_path, retry_count=0)
        assert fake.requested == [FIRST]

    async def test_the_error_text_does_not_carry_the_signed_url(self, tmp_path: Path, web: InstallWeb) -> None:
        """报错会进日志（聊天链路按 warning 打印异常文本），查询串里的签名不能跟着出去"""
        signed = "http://8.8.8.8/0?rkey=SECRET-TOKEN"
        routes: Dict[str, Route] = {signed: _redirect("http://8.8.8.8/1?rkey=SECRET-TOKEN")}
        routes.update({f"http://8.8.8.8/{i}?rkey=SECRET-TOKEN": _redirect(f"http://8.8.8.8/{i + 1}?rkey=SECRET-TOKEN") for i in range(1, 5)})
        web(routes)
        with pytest.raises(UnsafeDownloadUrlError) as raised:
            await _download(tmp_path, signed)
        assert "SECRET-TOKEN" not in str(raised.value)
