"""WebUI 视频预览：签名、Range、Content-Type、撤回、跨租户

上游用真 HTTP 服务（`FakeUpstream`），接口走真实路由与真实异常处理器，协议端是替身。
覆盖要求里的几条边界：

1. 跨频道、跨实例、跨租户一律 404，不泄露频道和消息是否存在；
2. 签名绑定频道、消息与用户，过期可以重新取；
3. Range 要真的能拖动，上游不认 Range 时也一样；
4. 只放行视频类型：上游回 HTML 时不能以同源内容转给浏览器；
5. 链接不进响应、不进日志、不落盘；
6. 不依赖视频插件开关；别的适配器不受影响。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import unquote

import httpx
import pytest
from fastapi import FastAPI
from loguru import logger as loguru_logger
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from nekro_agent.adapters.onebot_v11.core.bot import BotInstanceOfflineError
from nekro_agent.adapters.onebot_v11.tools import video as video_module
from nekro_agent.adapters.onebot_v11.tools.video import ByteRange, build_video_segment, parse_range_header
from nekro_agent.core.config import config as global_config
from nekro_agent.core.exception_handlers import register_exception_handlers
from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_adapter_instance import DBAdapterInstance
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_user import DBUser
from nekro_agent.routers import chat_channel as chat_channel_router
from nekro_agent.schemas.tenant import TenantRole
from nekro_agent.services.user.auth import expected_pwd_gen_claim
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.role import Role
from nekro_agent.tools import path_convertor
from nekro_agent.tools import video_access as video_access_module
from nekro_agent.tools.video_access import (
    VIDEO_ACCESS_WINDOW_SECONDS,
    issue_video_access_token,
    verify_video_access_token,
)
from tests.test_video_analysis_plugin import (  # noqa: F401 - fixtures are used by name
    ACME_TENANT,
    ADAPTER,
    CHAT_KEY,
    INSTANCE,
    OTHER_CHAT_KEY,
    OTHER_INSTANCE,
    RIVAL_TENANT,
    URL_SECRET,
    FakeUpstream,
    db,
)

MESSAGE_ID = 777
USER_ID = "2000000001"
MP4_HEAD = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8
BODY = MP4_HEAD + bytes(range(256)) * 8
PAGE = b"<html><script>alert(1)</script></html>" * 4
NOW = 1_800_000_000
TRICKLE_CHUNKS = 100
TRICKLE_CHUNK = 64


def _ranged_response(request: Request, body: bytes, media_type: str) -> Response:
    match = re.match(r"bytes=(\d*)-(\d*)$", request.headers.get("range") or "")
    if match is None:
        return Response(body, media_type=media_type, headers={"accept-ranges": "bytes"})
    start = int(match.group(1) or 0)
    if start >= len(body):
        return Response(status_code=416, headers={"content-range": f"bytes */{len(body)}"})
    end = min(int(match.group(2)) if match.group(2) else len(body) - 1, len(body) - 1)
    return Response(
        body[start : end + 1],
        status_code=206,
        media_type=media_type,
        headers={"content-range": f"bytes {start}-{end}/{len(body)}", "accept-ranges": "bytes"},
    )


class RangeUpstream(FakeUpstream):
    """QQ 媒体服务的替身：认 Range、不认 Range、乱回类型"""

    def _routes(self) -> List[Route]:
        return [
            *super()._routes(),
            Route("/ranged.mp4", self._ranged, methods=["GET"]),
            Route("/plain.mp4", self._plain, methods=["GET"]),
            Route("/page.html", self._page, methods=["GET"]),
            Route("/opaque", self._opaque, methods=["GET"]),
            Route("/opaque-ranged", self._opaque_ranged, methods=["GET"]),
            Route("/opaque-page", self._opaque_page, methods=["GET"]),
            Route("/opaque-page-ranged", self._opaque_page_ranged, methods=["GET"]),
            Route("/gone", self._gone, methods=["GET"]),
            Route("/stall", self._stall, methods=["GET"]),
            Route("/trickle", self._trickle, methods=["GET"]),
            Route("/wrong-range", self._wrong_range, methods=["GET"]),
            Route("/chunked", self._chunked, methods=["GET"]),
        ]

    async def _chunked(self, request: Request) -> Response:
        """分块传输、不给长度：浏览器只能靠结束帧判断正文到没到头"""
        self.get_paths.append(request.url.path)

        async def body() -> Any:
            for start in range(0, len(BODY), 512):
                yield BODY[start : start + 512]

        return StreamingResponse(body(), media_type="video/mp4")

    async def _wrong_range(self, request: Request) -> Response:
        """问开头，答的却是中间一段"""
        self.get_paths.append(request.url.path)
        return Response(BODY[5:37], status_code=206, media_type="video/mp4", headers={"content-range": f"bytes 5-36/{len(BODY)}"})

    async def _ranged(self, request: Request) -> Response:
        self.get_paths.append(request.url.path)
        self.last_range = request.headers.get("range")
        return _ranged_response(request, BODY, "video/mp4")

    async def _opaque_ranged(self, request: Request) -> Response:
        """对象存储的常见形态：标成二进制流，但正确地认 Range"""
        self.get_paths.append(request.url.path)
        self.last_range = request.headers.get("range")
        return _ranged_response(request, BODY, "application/octet-stream")

    async def _opaque_page_ranged(self, request: Request) -> Response:
        self.get_paths.append(request.url.path)
        self.last_range = request.headers.get("range")
        return _ranged_response(request, PAGE, "application/octet-stream")

    async def _stall(self, request: Request) -> Response:
        """响应头立刻回来，正文迟迟不来"""
        self.get_paths.append(request.url.path)

        async def body() -> Any:
            await asyncio.sleep(1.0)
            yield BODY

        return StreamingResponse(body(), media_type="application/octet-stream")

    async def _trickle(self, request: Request) -> Response:
        """一直在给数据，只是很慢：总共要几秒"""
        self.get_paths.append(request.url.path)

        async def body() -> Any:
            yield MP4_HEAD
            for _ in range(TRICKLE_CHUNKS):
                await asyncio.sleep(0.05)
                yield b"t" * TRICKLE_CHUNK

        return StreamingResponse(body(), media_type="video/mp4")

    async def _plain(self, request: Request) -> Response:
        self.get_paths.append(request.url.path)
        return Response(BODY, media_type="video/mp4")

    async def _page(self, request: Request) -> Response:
        self.get_paths.append(request.url.path)
        return Response(f"<script>alert(1)</script>{URL_SECRET}", media_type="text/html")

    async def _opaque(self, request: Request) -> Response:
        self.get_paths.append(request.url.path)
        return Response(BODY, media_type="application/octet-stream")

    async def _opaque_page(self, request: Request) -> Response:
        self.get_paths.append(request.url.path)
        return Response(b"<html><script>alert(1)</script></html>", media_type="application/octet-stream")

    async def _gone(self, request: Request) -> Response:
        self.get_paths.append(request.url.path)
        return Response(status_code=403)


@pytest.fixture
def media() -> Iterator[RangeUpstream]:
    server = RangeUpstream()
    server.start()
    try:
        yield server
    finally:
        server.stop()


class FakeBot:
    def __init__(self, url: Optional[str], get_file: Any = None, *, group_id: int = 1001) -> None:
        self.url = url
        self.get_file = get_file
        self.group_id = group_id
        self.calls: List[str] = []

    async def call_api(self, api: str, **_data: Any) -> Any:
        self.calls.append(api)
        if api == "get_msg":
            data: Dict[str, Any] = {"file": "clip.mp4", "file_size": "1024"}
            if self.url:
                data["url"] = self.url
            return {"message_type": "group", "group_id": self.group_id, "user_id": int(USER_ID), "message": [{"type": "video", "data": data}]}
        if self.get_file is None:
            raise RuntimeError("retcode 100")
        return self.get_file


async def seed(
    chat_key: str = CHAT_KEY, *, recalled: bool = False, message_id: int = MESSAGE_ID, adapter_key: str = ADAPTER, instance_key: str = INSTANCE,
) -> None:
    segment = build_video_segment(str(message_id), {"file": "clip.mp4", "file_size": "1024"})
    await DBChatMessage.create(
        message_id=str(message_id), sender_id=USER_ID, sender_name="t", sender_nickname="t", adapter_key=adapter_key, instance_key=instance_key,
        platform_userid=USER_ID, is_tome=0, is_recalled=recalled, chat_key=chat_key, chat_type="group", content_text="x",
        content_data=json.dumps([segment.model_dump()]), raw_cq_code="", ext_data="{}", send_timestamp=int(time.time()),
    )  # fmt: skip


async def make_user(name: str, tenant_id: Optional[int], role: TenantRole, level: Role) -> DBUser:
    return await DBUser.create(
        username=name, password="", adapter_key="webui", platform_userid=name, perm_level=level,
        login_time=datetime.datetime.now(datetime.timezone.utc), tenant_id=tenant_id, tenant_role=role.value,
    )  # fmt: skip


async def make_builtin(instance_key: str = INSTANCE) -> None:
    """这个实例的协议端是 NA 自己在本机起的 NapCat 容器"""
    await DBAdapterInstance.filter(instance_key=instance_key).update(provider="builtin_napcat")


def napcat_root(data_dir: Path, instance_key: str = INSTANCE) -> Path:
    return data_dir / "napcat_data" / instance_key / "QQ"


def can_symlink(tmp_path: Path) -> bool:
    probe = tmp_path / "symlink-probe"
    try:
        os.symlink(tmp_path, probe, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


class RawClient:
    """直接按 ASGI 调接口：收了响应头就不再读，或者在第一个字节之前走掉

    `httpx.ASGITransport` 会把整个响应读完才返回，模拟不了“浏览器暂停后不读了”。
    """

    def __init__(self, app: Any, url: str, *, stall_on: str, spec_version: str = "2.4", disconnect: bool = False) -> None:
        path, _, query = url.partition("?")
        self.scope: Dict[str, Any] = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": unquote(path), "raw_path": path.encode(), "root_path": "",
            "query_string": query.encode(), "headers": [(b"host", b"test")], "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }  # fmt: skip
        self.app = app
        self.stall_on = stall_on
        self.disconnect = disconnect
        self.messages: List[Dict[str, Any]] = []
        self.stalled = asyncio.Event()
        # 设上之后停读的浏览器“恢复读取”：被卡住的那次 send 返回，之后照常收
        self.resume = asyncio.Event()
        self._stalled_once = False
        self._requested = False
        self.task: Optional[asyncio.Task[Any]] = None

    async def _receive(self) -> Dict[str, Any]:
        if not self._requested:
            self._requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        if self.disconnect:
            return {"type": "http.disconnect"}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def _send(self, message: Dict[str, Any]) -> None:
        self.messages.append(message)
        stall_here = message["type"] == self.stall_on and (message["type"] == "http.response.start" or message.get("body"))
        if stall_here and not self._stalled_once:
            self._stalled_once = True
            self.stalled.set()
            await self.resume.wait()

    def start(self) -> "RawClient":
        self.task = asyncio.create_task(self.app(self.scope, self._receive, self._send))
        return self

    @property
    def status(self) -> Optional[int]:
        starts = [message for message in self.messages if message["type"] == "http.response.start"]
        return starts[0]["status"] if starts else None

    @property
    def completed(self) -> bool:
        """服务器有没有发结束帧：发了就是在告诉浏览器“正文到此为止，是完整的”"""
        return any(message["type"] == "http.response.body" and not message.get("more_body", False) for message in self.messages)

    @property
    def body(self) -> bytes:
        return b"".join(message.get("body", b"") for message in self.messages if message["type"] == "http.response.body")


class Web:
    def __init__(self, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
        self.app = app
        self.monkeypatch = monkeypatch
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def login(self, user: DBUser) -> None:
        self.app.dependency_overrides[get_current_active_user] = lambda: user

    def bot(self, bot: Any) -> Any:
        async def locate(_chat_key: str) -> Any:
            if isinstance(bot, Exception):
                raise bot
            return bot

        self.monkeypatch.setattr(chat_channel_router, "get_bot_by_chat_key", locate)
        return bot

    async def access(self, chat_key: str = CHAT_KEY, message_id: Any = MESSAGE_ID) -> httpx.Response:
        return await self.client.get(f"/api/chat-channel/{chat_key}/messages/{message_id}/video-access")

    async def play_url(self) -> str:
        body = (await self.access()).json()
        assert body["status"] == "ok", body
        return body["url"]

    async def stream(self, url: str, range_header: Optional[str] = None) -> httpx.Response:
        return await self.client.get(url, headers={"Range": range_header} if range_header else None)

    def user(self) -> DBUser:
        return self.app.dependency_overrides[get_current_active_user]()

    def signed_url(self, chat_key: str = CHAT_KEY, message_id: int = MESSAGE_ID, *, now: Optional[int] = None) -> str:
        """绕过探测直接签一个地址：探测失败的情况下也要看转发接口怎么回"""
        user = self.user()
        token = issue_video_access_token(chat_key, str(message_id), int(user.id), generation=expected_pwd_gen_claim(user), now=now)
        return f"/api/chat-channel/{chat_key}/messages/{message_id}/video?sig={token}"


@pytest.fixture
async def web(db: None, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(chat_channel_router.router, prefix="/api")
    # 用 ALLOW 开关让本地假上游过出站校验；专门测 SSRF 的用例会关掉它
    monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", True)
    monkeypatch.setattr(global_config, "PLUGIN_ENABLED", [])
    # 名额是进程级的，每条用例一份新的，一条用例漏还名额不会拖累下一条
    monkeypatch.setattr(video_module, "video_stream_slots", video_module.StreamSlots())
    helper = Web(app, monkeypatch)
    await seed()
    helper.login(await make_user("acme", ACME_TENANT, TenantRole.TENANT_ADMIN, Role.Admin))
    async with helper.client:
        yield helper


def assert_safe_headers(response: httpx.Response) -> None:
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in response.headers["cache-control"]
    assert "sandbox" in response.headers["content-security-policy"]


FUTURE = str(NOW + VIDEO_ACCESS_WINDOW_SECONDS)
HUGE_USER = "9" * 40


class TestSignature:
    def test_it_binds_the_channel_the_message_and_the_user(self) -> None:
        token = issue_video_access_token(CHAT_KEY, "777", 7, generation=0, now=NOW)
        assert verify_video_access_token(token, CHAT_KEY, "777", now=NOW) == 7
        assert verify_video_access_token(token, OTHER_CHAT_KEY, "777", now=NOW) is None
        assert verify_video_access_token(token, CHAT_KEY, "778", now=NOW) is None

    def test_the_user_cannot_be_swapped(self) -> None:
        token = issue_video_access_token(CHAT_KEY, "777", 7, generation=0, now=NOW)
        assert verify_video_access_token(token.replace(".7.", ".8."), CHAT_KEY, "777", now=NOW) is None

    @pytest.mark.parametrize("later", [1, "0", "1.fingerprint", "0.fingerprint"])
    def test_it_is_bound_to_the_password_generation(self, later: Any) -> None:
        """与登录令牌的 `pwd_gen` 同一个来源：改密、重置、轮换 admin 口令之后旧地址验不过"""
        token = issue_video_access_token(CHAT_KEY, "777", 7, generation=0, now=NOW)
        assert video_access_module.video_token_matches_generation(token, CHAT_KEY, "777", generation=0, now=NOW)
        assert not video_access_module.video_token_matches_generation(token, CHAT_KEY, "777", generation=later, now=NOW)

    def test_the_generation_signature_is_bound_to_the_rest_too(self) -> None:
        token = issue_video_access_token(CHAT_KEY, "777", 7, generation=0, now=NOW)
        assert not video_access_module.video_token_matches_generation(token, OTHER_CHAT_KEY, "777", generation=0, now=NOW)
        assert not video_access_module.video_token_matches_generation(token, CHAT_KEY, "778", generation=0, now=NOW)

    def test_the_generation_is_not_readable_from_the_url(self) -> None:
        """admin 的世代里带口令指纹，不能进 URL"""
        token = issue_video_access_token(CHAT_KEY, "777", 7, generation="3.SENTINEL-fingerprint", now=NOW)
        assert "SENTINEL" not in token and ".3." not in token
        assert video_access_module.video_token_matches_generation(token, CHAT_KEY, "777", generation="3.SENTINEL-fingerprint", now=NOW)

    def test_it_expires(self) -> None:
        token = issue_video_access_token(CHAT_KEY, "777", 7, generation=0, now=NOW)
        assert verify_video_access_token(token, CHAT_KEY, "777", now=NOW + 2 * VIDEO_ACCESS_WINDOW_SECONDS) is None

    def test_a_token_lives_at_least_one_window(self) -> None:
        late = NOW - NOW % VIDEO_ACCESS_WINDOW_SECONDS + VIDEO_ACCESS_WINDOW_SECONDS - 1
        token = issue_video_access_token(CHAT_KEY, "777", 7, generation=0, now=late)
        assert verify_video_access_token(token, CHAT_KEY, "777", now=late + VIDEO_ACCESS_WINDOW_SECONDS) == 7

    def test_the_url_is_stable_inside_a_window(self) -> None:
        """`<video>` 拖动时用同一个地址，换地址浏览器就从头加载"""
        start = NOW - NOW % VIDEO_ACCESS_WINDOW_SECONDS
        first = issue_video_access_token(CHAT_KEY, "777", 7, generation=0, now=start)
        assert first == issue_video_access_token(CHAT_KEY, "777", 7, generation=0, now=start + 5)

    def test_it_does_not_validate_as_an_upload_token(self) -> None:
        from nekro_agent.tools.upload_access import verify_upload_access_token

        assert verify_upload_access_token(issue_video_access_token(CHAT_KEY, "777", 7, generation=0), CHAT_KEY) is False

    @pytest.mark.parametrize("user_id", [2**31 - 1, 1])
    def test_the_largest_user_id_the_column_holds_still_works(self, user_id: int) -> None:
        token = issue_video_access_token(CHAT_KEY, "777", user_id, generation=0, now=NOW)
        assert verify_video_access_token(token, CHAT_KEY, "777", now=NOW) == user_id

    @pytest.mark.parametrize("user_id", [2**31, 10**40, 0])
    def test_a_user_id_the_column_cannot_hold_is_refused_even_when_signed(self, user_id: int) -> None:
        """就算签得出来也不交给数据库：装不进 `DBUser.id` 的数查库时会抛错变成 500"""
        token = issue_video_access_token(CHAT_KEY, "777", user_id, generation=0, now=NOW)
        assert verify_video_access_token(token, CHAT_KEY, "777", now=NOW) is None

    @pytest.mark.parametrize(
        "token",
        [
            "", "v1", "v1.1.2", "v1.a.b.c", "v1.².1." + "0" * 64, "v2.9999999999.1." + "0" * 64, "x" * 400, "视频",
            f"v1.{FUTURE}.{HUGE_USER}.{'0' * 64}", f"v1.{FUTURE}.{HUGE_USER}.{'0' * 64}.{'0' * 64}",
            f"v1.{FUTURE}.{2**31}.{'0' * 64}.{'0' * 64}", f"v1.{FUTURE}.0.{'0' * 64}.{'0' * 64}", f"v1.{'9' * 40}.7.{'0' * 64}.{'0' * 64}",
        ],
    )  # fmt: skip
    def test_garbage_is_a_refusal_not_a_500(self, token: str) -> None:
        assert verify_video_access_token(token, CHAT_KEY, "777", now=NOW) is None
        assert not video_access_module.video_token_matches_generation(token, CHAT_KEY, "777", generation=0, now=NOW)


class TestRange:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("bytes=0-99", ByteRange(0, 99)),
            ("bytes=100-", ByteRange(100, None)),
            ("bytes=-50", ByteRange(None, 50)),
            ("bytes=5-1", None),
            ("bytes=0-1,5-9", None),
            ("items=0-1", None),
            ("bytes=-", None),
            ("", None),
            (None, None),
        ],
    )
    def test_parsing(self, raw: Optional[str], expected: Optional[ByteRange]) -> None:
        assert parse_range_header(raw) == expected

    async def test_a_first_request_gets_the_whole_file(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4?rkey={URL_SECRET}"))
        response = await web.stream(await web.play_url())
        assert response.status_code == 200
        assert response.content == BODY
        assert response.headers["content-type"].startswith("video/mp4")
        assert response.headers["accept-ranges"] == "bytes"
        assert_safe_headers(response)

    async def test_seeking_is_forwarded(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        response = await web.stream(await web.play_url(), "bytes=100-199")
        assert response.status_code == 206
        assert response.content == BODY[100:200]
        assert response.headers["content-range"] == f"bytes 100-199/{len(BODY)}"
        assert media.last_range == "bytes=100-199"
        assert_safe_headers(response)

    async def test_an_upstream_without_range_support_can_still_be_dragged(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/plain.mp4"))
        response = await web.stream(await web.play_url(), "bytes=100-199")
        assert response.status_code == 206
        assert response.content == BODY[100:200]
        assert response.headers["content-range"] == f"bytes 100-199/{len(BODY)}"
        assert response.headers["content-length"] == "100"

    async def test_a_suffix_range_against_such_an_upstream(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/plain.mp4"))
        response = await web.stream(await web.play_url(), "bytes=-10")
        assert response.status_code == 206
        assert response.content == BODY[-10:]

    @pytest.mark.parametrize("path", ["/ranged.mp4", "/plain.mp4"])
    async def test_past_the_end_is_416(self, web: Web, media: RangeUpstream, path: str) -> None:
        web.bot(FakeBot(f"{media.base_url}{path}"))
        response = await web.stream(await web.play_url(), f"bytes={len(BODY) + 1}-")
        assert response.status_code == 416
        assert response.headers["content-range"] == f"bytes */{len(BODY)}"

    async def test_a_malformed_range_is_ignored_not_forwarded(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        media.last_range = "not-touched"
        response = await web.stream(url, "bytes=0-1,5-9\r\nX-Evil: 1")
        assert response.status_code == 200
        assert media.last_range is None

    async def test_the_builtin_napcats_local_file_supports_range(self, web: Web, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        await make_builtin()
        data_dir = tmp_path / "data"
        host_file = napcat_root(data_dir) / "Video" / "a.mp4"
        host_file.parent.mkdir(parents=True)
        host_file.write_bytes(BODY)
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(data_dir))
        web.bot(FakeBot("/app/.config/QQ/Video/a.mp4"))
        url = await web.play_url()
        response = await web.stream(url, "bytes=4-11")
        assert response.status_code == 206
        assert response.content == BODY[4:12]
        assert response.headers["content-range"] == f"bytes 4-11/{len(BODY)}"
        assert response.headers["content-type"].startswith("video/mp4")
        assert_safe_headers(response)
        whole = await web.stream(url)
        assert whole.status_code == 200 and whole.content == BODY
        past = await web.stream(url, f"bytes={len(BODY)}-")
        assert past.status_code == 416 and past.headers["content-range"] == f"bytes */{len(BODY)}"


class TestLocalFiles:
    """协议端只给本机路径时的后备：只有 NA 自己起的内置 NapCat 能走，且只读那个实例自己的目录"""

    @pytest.fixture
    def data_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        directory = tmp_path / "data"
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(directory))
        return directory

    @staticmethod
    def _place(path: Path, content: bytes = BODY) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    async def test_a_remote_protocol_end_cannot_make_na_read_a_local_file(self, web: Web, data_dir: Path) -> None:
        """远程协议端（多租户下可能归租户控制）报一个容器路径，NA 不能因此去读本机同名位置"""
        self._place(napcat_root(data_dir) / "Video" / "a.mp4")
        web.bot(FakeBot("/app/.config/QQ/Video/a.mp4"))
        assert (await web.access()).json()["status"] == "expired"
        response = await web.stream(web.signed_url())
        assert response.status_code == 410 and BODY not in response.content

    async def test_with_an_instance_key_the_legacy_directory_is_not_a_fallback(self, web: Web, data_dir: Path) -> None:
        await make_builtin()
        napcat_root(data_dir).mkdir(parents=True)
        self._place(data_dir / "napcat_data" / "QQ" / "Video" / "a.mp4")
        web.bot(FakeBot("/app/.config/QQ/Video/a.mp4"))
        assert (await web.access()).json()["status"] == "expired"

    async def test_a_legacy_chat_key_without_an_instance_gets_no_local_fallback(self, web: Web, data_dir: Path) -> None:
        legacy = "onebot_v11-group_1009"
        await DBChatChannel.create(adapter_key="onebot_v11", instance_key="", channel_id="group_1009", chat_key=legacy, channel_type="group", data="{}")
        await seed(legacy, instance_key="")
        self._place(data_dir / "napcat_data" / "QQ" / "Video" / "a.mp4")
        web.bot(FakeBot("/app/.config/QQ/Video/a.mp4", group_id=1009))
        web.login(await make_user("owner", None, TenantRole.PLATFORM_OWNER, Role.Super))
        assert (await web.access(legacy)).json()["status"] == "expired"

    async def test_a_real_file_outside_the_instance_directory_is_not_read(self, web: Web, data_dir: Path) -> None:
        """`..` 拼出来的位置上真有一个文件，挡住它的只能是归属校验

        实例的 QQ 目录要真的存在：不存在的话根目录在 `resolve(strict=True)` 那一步就失败，用例空转。
        """
        await make_builtin()
        napcat_root(data_dir).mkdir(parents=True)
        self._place(data_dir / "napcat_data" / "secret.mp4")
        web.bot(FakeBot("/app/.config/QQ/../../secret.mp4"))
        assert (await web.access()).json()["status"] == "expired"
        response = await web.stream(web.signed_url())
        assert response.status_code == 410 and BODY not in response.content
        # 对照：同一个布局下目录里的文件是能读的，说明上面挡住它的是归属校验，不是别的
        self._place(napcat_root(data_dir) / "Video" / "a.mp4")
        web.bot(FakeBot("/app/.config/QQ/Video/a.mp4"))
        assert (await web.access()).json()["status"] == "ok"

    async def test_an_instance_directory_that_is_itself_a_link_is_not_trusted(self, web: Web, data_dir: Path, tmp_path: Path) -> None:
        """内置 NapCat 的容器能写 `napcat_data/<实例>`：把 `QQ` 换成链接，界线就跟着走了"""
        if not can_symlink(tmp_path):
            pytest.skip("这台机器建不了符号链接")
        await make_builtin()
        self._place(tmp_path / "elsewhere" / "Video" / "a.mp4")
        napcat_root(data_dir).parent.mkdir(parents=True)
        os.symlink(tmp_path / "elsewhere", napcat_root(data_dir), target_is_directory=True)
        web.bot(FakeBot("/app/.config/QQ/Video/a.mp4"))
        assert (await web.access()).json()["status"] == "expired"

    async def test_a_deleted_instance_is_not_trusted(self, web: Web, data_dir: Path) -> None:
        """软删的实例行还留着 `provider=builtin_napcat`，不能凭它读本机文件"""
        await DBAdapterInstance.filter(instance_key=INSTANCE).update(provider="builtin_napcat", status="deleted")
        self._place(napcat_root(data_dir) / "Video" / "a.mp4")
        web.bot(FakeBot("/app/.config/QQ/Video/a.mp4"))
        web.login(await make_user("owner", None, TenantRole.PLATFORM_OWNER, Role.Super))
        assert (await web.access()).json()["status"] == "expired"

    async def test_a_local_file_over_the_size_cap_is_refused(self, web: Web, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        await make_builtin()
        self._place(napcat_root(data_dir) / "Video" / "a.mp4")
        monkeypatch.setattr(video_module, "MAX_PROXY_BYTES", 16)
        web.bot(FakeBot("/app/.config/QQ/Video/a.mp4"))
        assert (await web.access()).json()["status"] == "unavailable"

    @pytest.mark.parametrize(
        "reported",
        # `Z:x.mp4` 没有反斜杠：只有冒号能挡住它（另一块盘上的相对路径）
        ["/app/.config/QQ/nt_qq/\\\\nonexistent.invalid\\share\\x.mp4", "/app/.config/QQ/C:\\Windows\\x.mp4", "/app/.config/QQ/Z:x.mp4"],
    )
    def test_a_drive_or_a_share_in_the_path_is_refused_before_any_lookup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reported: str) -> None:
        """Windows 上这一段会换掉整个前缀，`resolve()` 会先去访问那台主机或那块盘，包含校验再拒就晚了"""
        root = tmp_path / "QQ"
        root.mkdir()
        root = root.resolve()
        looked_up: List[str] = []
        real_resolve = Path.resolve

        def spy(self: Path, strict: bool = False) -> Path:
            looked_up.append(str(self))
            if not str(self).startswith(str(root)):
                raise OSError("测试里不真的去访问别的主机或盘")
            return real_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", spy)
        assert video_module._map_protocol_path(root, reported) is None  # noqa: SLF001
        assert all(path.startswith(str(root)) for path in looked_up), looked_up

    async def test_a_non_video_file_is_not_read(self, web: Web, data_dir: Path) -> None:
        await make_builtin()
        self._place(napcat_root(data_dir) / "nt_qq" / "nt_db" / "nt_msg.db", MP4_HEAD)
        web.bot(FakeBot("/app/.config/QQ/nt_qq/nt_db/nt_msg.db"))
        assert (await web.access()).json()["status"] == "expired"

    def test_the_mapping_alone_refuses_what_resolves_outside_or_is_not_a_video(self, tmp_path: Path) -> None:
        """不依赖打开时的第二道检查：换算这一步自己就要挡住"""
        root = tmp_path / "QQ"
        inside = self._place(root / "Video" / "a.mp4")
        root = root.resolve()
        self._place(tmp_path / "secret.mp4")
        self._place(root / "nt_db" / "nt_msg.db", MP4_HEAD)
        mapping = video_module._map_protocol_path  # noqa: SLF001
        assert mapping(root, "/app/.config/QQ/Video/a.mp4") == inside.resolve()
        assert mapping(root, "file:///app/.config/QQ/Video/a.mp4") == inside.resolve()
        assert mapping(None, "/app/.config/QQ/Video/a.mp4") is None, "不是内置实例"
        assert mapping(root, "/app/.config/QQ/../secret.mp4") is None
        assert mapping(root, "/app/.config/QQ/nt_db/nt_msg.db") is None
        assert mapping(root, "/app/.config/QQ/Video/missing.mp4") is None
        assert mapping(root, "/app/.config/Other/Video/a.mp4") is None
        if can_symlink(tmp_path):
            os.symlink(root / "nt_db" / "nt_msg.db", root / "Video" / "db.mp4")
            assert mapping(root, "/app/.config/QQ/Video/db.mp4") is None, "链接名是 .mp4，指向的不是视频"
            os.symlink(tmp_path / "secret.mp4", root / "Video" / "out.mp4")
            assert mapping(root, "/app/.config/QQ/Video/out.mp4") is None
            os.symlink(inside, root / "Video" / "alias.mp4")
            assert mapping(root, "/app/.config/QQ/Video/alias.mp4") == inside.resolve(), "目录里面互相指没有越界"

    def test_opening_refuses_a_file_outside_the_root(self, tmp_path: Path) -> None:
        root = tmp_path / "QQ"
        root.mkdir()
        outside = self._place(tmp_path / "secret.mp4")
        with pytest.raises(video_module.StaleVideoLocation):
            video_module._open_within(outside.resolve(), root.resolve())  # noqa: SLF001

    def test_opening_refuses_a_file_swapped_for_a_link_after_the_check(self, tmp_path: Path) -> None:
        """换算之后、打开之前，协议端把文件换成指向外面的链接（Windows 没有 O_NOFOLLOW，靠按句柄取真实路径）"""
        if not can_symlink(tmp_path):
            pytest.skip("这台机器建不了符号链接")
        root = tmp_path / "QQ"
        target = self._place(root / "Video" / "a.mp4")
        outside = self._place(tmp_path / "secret.mp4", b"secret-bytes")
        root = root.resolve()
        mapped = video_module._map_protocol_path(root, "/app/.config/QQ/Video/a.mp4")  # noqa: SLF001
        assert mapped is not None
        target.unlink()
        os.symlink(outside, target)
        with pytest.raises(video_module.StaleVideoLocation):
            video_module._open_within(mapped, root)  # noqa: SLF001

    def test_the_real_path_comes_from_the_opened_file_itself(self, tmp_path: Path) -> None:
        """Linux 读 /proc/self/fd，Windows 问文件句柄（GetFinalPathNameByHandleW）：打开的是链接时给的是它指向的文件"""
        if not can_symlink(tmp_path):
            pytest.skip("这台机器建不了符号链接")
        target = self._place(tmp_path / "real" / "a.mp4")
        link = tmp_path / "link.mp4"
        os.symlink(target, link)
        fd = os.open(link, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            real = path_convertor.opened_real_path(fd)
        finally:
            os.close(fd)
        if real is None and not (sys.platform == "win32" or Path("/proc/self/fd").exists()):
            pytest.skip("这个平台取不到已打开文件的真实路径，走 (dev, ino) 核对")
        assert real == target.resolve()

    def test_opening_refuses_when_the_opened_file_is_not_the_one_at_the_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """取不到真实路径的平台：打开的 fd 与 `resolve()` 出来的那个文件必须是同一个（模拟打开前后被调包）"""
        root = tmp_path / "QQ"
        target = self._place(root / "Video" / "a.mp4")
        swapped_in = self._place(root / "Video" / "b.mp4")
        real_stat = os.stat
        monkeypatch.setattr(path_convertor, "opened_real_path", lambda _fd: None)
        monkeypatch.setattr(path_convertor.os, "fstat", lambda _fd: real_stat(swapped_in))
        with pytest.raises(video_module.StaleVideoLocation):
            video_module._open_within(target.resolve(), root.resolve())  # noqa: SLF001

    def test_opening_refuses_something_that_is_not_a_regular_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """打开前被换成目录或 FIFO"""
        root = tmp_path / "QQ"
        target = self._place(root / "Video" / "a.mp4")
        real_stat = os.stat
        monkeypatch.setattr(path_convertor, "opened_real_path", lambda _fd: target.resolve())
        monkeypatch.setattr(path_convertor.os, "fstat", lambda _fd: real_stat(root))
        with pytest.raises(video_module.StaleVideoLocation):
            video_module._open_within(target.resolve(), root.resolve())  # noqa: SLF001

    def test_opening_a_file_inside_the_root_works(self, tmp_path: Path) -> None:
        root = tmp_path / "QQ"
        target = self._place(root / "Video" / "a.mp4")
        fd, size = video_module._open_within(target.resolve(), root.resolve())  # noqa: SLF001
        try:
            assert size == len(BODY)
        finally:
            os.close(fd)

    def test_the_fallback_also_works_on_a_regular_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照：取不到真实路径时，正常文件按 (dev, ino) 核对能通过"""
        root = tmp_path / "QQ"
        target = self._place(root / "Video" / "a.mp4")
        monkeypatch.setattr(path_convertor, "opened_real_path", lambda _fd: None)
        fd, _ = video_module._open_within(target.resolve(), root.resolve())  # noqa: SLF001
        os.close(fd)

    @staticmethod
    def _plant_fake_proc(
        monkeypatch: pytest.MonkeyPatch, fake_root: Path, plant: Callable[[Path], None], forged: Optional[Path] = None,
    ) -> None:
        """模拟有人在盘根目录建了 `proc\\self\\fd\\N`：把对 `/proc/self/fd/` 的访问转到临时目录，不在 C:\\ 下真建

        `forged` 给了时，读那个链接得到的就是它（攻击者想让 NA 以为打开的是哪个文件）。
        """
        real_open, real_exists, real_readlink = os.open, os.path.exists, os.readlink
        prefix = "/proc/self/fd/"

        def redirect(path: Any) -> Any:
            text = os.fspath(path)
            return fake_root / text[len(prefix) :] if isinstance(text, str) and text.startswith(prefix) else path

        def opening(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            plant(fake_root / str(fd))
            return fd

        def reading(path: Any, *args: Any, **kwargs: Any) -> Any:
            target = redirect(path)
            if forged is not None and target is not path:
                return str(forged)
            return real_readlink(target, *args, **kwargs)

        monkeypatch.setattr(path_convertor.os, "open", opening)
        monkeypatch.setattr(path_convertor.os.path, "exists", lambda path: real_exists(redirect(path)))
        monkeypatch.setattr(path_convertor.os, "readlink", reading)

    def test_a_planted_proc_file_does_not_break_opening(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """盘根目录下放了一个普通文件 `proc\\self\\fd\\N`：读链接出错不能变成 500"""
        root = tmp_path / "QQ"
        target = self._place(root / "Video" / "a.mp4")
        fake = tmp_path / "fakeproc"
        fake.mkdir()
        self._plant_fake_proc(monkeypatch, fake, lambda entry: entry.write_bytes(b"planted"))
        fd, size = path_convertor.open_regular_file_within(target.resolve(), (root.resolve(),), resolve_roots=False)
        os.close(fd)
        assert size == len(BODY)

    @pytest.mark.skipif(sys.platform != "win32", reason="Linux 上 /proc 是内核给的，伪造不了；这里测的是 Windows 不该去读它")
    def test_a_planted_proc_link_cannot_fake_the_real_path_on_windows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """盘根目录下放一个链接 `proc\\self\\fd\\N`，读出来是目录里的文件，冒充“打开的就是它”"""
        root = tmp_path / "QQ"
        decoy = self._place(root / "Video" / "a.mp4")
        outside = self._place(tmp_path / "secret.mp4", b"secret-bytes")
        fake = tmp_path / "fakeproc"
        fake.mkdir()
        self._plant_fake_proc(monkeypatch, fake, lambda entry: entry.write_bytes(b"planted"), forged=decoy.resolve())
        with pytest.raises(path_convertor.LocalFileRefused):
            path_convertor.open_regular_file_within(outside.resolve(), (root.resolve(),), resolve_roots=False)

    def test_the_fallback_does_not_follow_a_link_swapped_in_after_resolving(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """取不到真实路径时：`resolve()` 之后那个位置被换成指向外面的链接，(dev, ino) 不能跟着链接取"""
        if not can_symlink(tmp_path):
            pytest.skip("这台机器建不了符号链接")
        root = tmp_path / "QQ"
        root.mkdir()
        outside = self._place(tmp_path / "secret.mp4", b"secret-bytes")
        link = root / "a.mp4"
        os.symlink(outside.resolve(), link)
        monkeypatch.setattr(path_convertor, "opened_real_path", lambda _fd: None)
        # resolve() 那一刻它还是目录里的普通文件
        monkeypatch.setattr(path_convertor.Path, "resolve", lambda self, strict=False: self)
        with pytest.raises(path_convertor.LocalFileRefused):
            path_convertor.open_regular_file_within(link, (root,), resolve_roots=False)

    @pytest.mark.parametrize("kind", ["file", "directory"])
    async def test_a_symlink_out_of_the_instance_directory_is_not_followed(self, web: Web, data_dir: Path, tmp_path: Path, kind: str) -> None:
        if not can_symlink(tmp_path):
            pytest.skip("这台机器建不了符号链接")
        await make_builtin()
        outside = self._place(tmp_path / "outside" / "Video" / "secret.mp4")
        root = napcat_root(data_dir)
        if kind == "file":
            (root / "Video").mkdir(parents=True)
            os.symlink(outside, root / "Video" / "a.mp4")
            reported = "/app/.config/QQ/Video/a.mp4"
        else:
            root.mkdir(parents=True)
            os.symlink(outside.parent, root / "Video", target_is_directory=True)
            reported = "/app/.config/QQ/Video/secret.mp4"
        web.bot(FakeBot(reported))
        assert (await web.access()).json()["status"] == "expired"
        response = await web.stream(web.signed_url())
        assert response.status_code == 410 and BODY not in response.content


class TestContentType:
    async def test_html_never_reaches_the_browser(self, web: Web, media: RangeUpstream) -> None:
        """这条接口跟 WebUI 同源，转过去就是存储型 XSS"""
        web.bot(FakeBot(f"{media.base_url}/page.html"))
        assert (await web.access()).json()["status"] == "expired"
        response = await web.stream(web.signed_url())
        assert response.status_code == 410
        assert b"<script>" not in response.content
        assert URL_SECRET.encode() not in response.content

    async def test_opaque_bytes_that_are_a_video_play(self, web: Web, media: RangeUpstream) -> None:
        """对象存储常把视频标成二进制流，一律拦掉就放不了真视频"""
        web.bot(FakeBot(f"{media.base_url}/opaque"))
        response = await web.stream(await web.play_url())
        assert response.status_code == 200
        assert response.content == BODY
        assert response.headers["content-type"].startswith("video/mp4")

    async def test_opaque_bytes_that_are_a_page_do_not(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/opaque-page"))
        assert (await web.access()).json()["status"] == "expired"

    async def test_opaque_bytes_with_real_range_support_probe_and_seek(self, web: Web, media: RangeUpstream) -> None:
        """对象存储标成二进制流、又正确地认 Range：探测要读够判文件头的字节，不能只要 1 个"""
        web.bot(FakeBot(f"{media.base_url}/opaque-ranged"))
        access = (await web.access()).json()
        assert access["status"] == "ok", access
        whole = await web.stream(access["url"])
        assert whole.status_code == 200 and whole.content == BODY
        seek = await web.stream(access["url"], "bytes=100-199")
        assert seek.status_code == 206 and seek.content == BODY[100:200]
        assert seek.headers["content-type"].startswith("video/mp4")

    async def test_a_tiny_range_at_the_start_is_judged_on_a_wider_read(self, web: Web, media: RangeUpstream) -> None:
        """Safari 先发 `bytes=0-1` 试探：上游多读一点判文件头，回给浏览器的仍是它要的两个字节"""
        web.bot(FakeBot(f"{media.base_url}/opaque-ranged"))
        url = await web.play_url()
        response = await web.stream(url, "bytes=0-1")
        assert response.status_code == 206
        assert response.content == BODY[:2]
        assert response.headers["content-range"] == f"bytes 0-1/{len(BODY)}"
        assert response.headers["content-length"] == "2"
        assert media.last_range == "bytes=0-31"

    async def test_an_upstream_answering_another_range_is_not_trimmed_into_a_lie(self, web: Web, media: RangeUpstream) -> None:
        """为判文件头放宽了区间、再按 0 起裁回去：上游答的若不是从 0 开始，裁出来的就是假的 `bytes 0-1`"""
        web.bot(FakeBot(f"{media.base_url}/wrong-range"))
        response = await web.stream(web.signed_url(), "bytes=0-1")
        assert response.status_code == 410
        assert "content-range" not in response.headers

    async def test_a_page_behind_range_support_is_still_refused(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/opaque-page-ranged"))
        assert (await web.access()).json()["status"] == "expired"
        response = await web.stream(web.signed_url(), "bytes=0-1")
        assert response.status_code == 410 and b"<html" not in response.content


class TestFailures:
    async def test_recalled_is_reported_and_not_played(self, web: Web) -> None:
        await DBChatMessage.filter(chat_key=CHAT_KEY).update(is_recalled=True)
        bot = web.bot(FakeBot("https://a.example.com/x"))
        assert (await web.access()).json() == {"status": "recalled", "url": "", "expires_at": 0, "file_name": "", "file_size": 0}
        assert bot.calls == [], "撤回了就不该再向协议端要链接"

    async def test_a_token_issued_before_the_recall_stops_working(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        await DBChatMessage.filter(chat_key=CHAT_KEY).update(is_recalled=True)
        assert (await web.stream(url)).status_code == 410

    async def test_an_offline_protocol_end_is_readable(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        web.bot(BotInstanceOfflineError(INSTANCE))
        assert (await web.access()).json()["status"] == "offline"
        assert (await web.stream(url)).status_code == 503

    async def test_an_expired_link_uses_get_file(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/gone", get_file={"url": f"{media.base_url}/ranged.mp4", "file_name": "clip.mp4", "file_size": "1024"}))
        assert (await web.stream(await web.play_url())).content == BODY

    @pytest.mark.parametrize(
        "reply",
        [
            {"file_name": "other.mp4", "file_size": "1024"},
            {"file_name": "clip.mp4", "file_size": "999"},
            {"file_name": "clip.mp4"},
            {"file_size": "1024"},
            {},
        ],
    )
    async def test_get_file_must_hand_back_the_same_file(self, web: Web, media: RangeUpstream, reply: Dict[str, Any]) -> None:
        """NapCat 的 `get_file` 按文件名取件：缓存键会被同名文件覆盖，未命中时还会全局按名搜索"""
        web.bot(FakeBot(f"{media.base_url}/gone", get_file={"url": f"{media.base_url}/ranged.mp4", **reply}))
        assert (await web.access()).json()["status"] == "expired"
        assert "/ranged.mp4" not in media.get_paths

    async def test_dns_rebinding_is_caught_at_the_connected_peer(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        """解析时是公网、连上的是内网：只有按实际对端再判一次才挡得住"""

        async def resolved_public(_url: str) -> None:
            return None

        monkeypatch.setattr(video_module, "assert_download_url_safe", resolved_public)
        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
        monkeypatch.setattr(OsEnv, "DOWNLOAD_EXTRA_ALLOWED_NETWORKS", "")
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        assert (await web.access()).json()["status"] == "expired"
        response = await web.stream(web.signed_url())
        assert response.status_code == 410
        assert BODY not in response.content

    @pytest.mark.parametrize("link", ["http://[::1", "http://\x00a.example.com/x"])
    async def test_a_malformed_link_is_a_status_not_a_500(self, web: Web, link: str) -> None:
        web.bot(FakeBot(link))
        response = await web.access()
        assert response.status_code == 200
        assert response.json()["status"] == "expired"
        assert (await web.stream(web.signed_url())).status_code == 410

    async def test_a_stalled_first_read_moves_on_to_the_next_address(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(video_module, "READ_TIMEOUT_SECONDS", 0.2)
        reply = {"url": f"{media.base_url}/ranged.mp4", "file_name": "clip.mp4", "file_size": "1024"}
        web.bot(FakeBot(f"{media.base_url}/stall", get_file=reply))
        response = await web.access()
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert media.get_paths[:2] == ["/stall", "/ranged.mp4"]

    async def test_a_dead_link_with_no_fallback_says_expired(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/gone"))
        assert (await web.access()).json()["status"] == "expired"

    async def test_too_large_is_refused_before_forwarding(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(video_module, "MAX_PROXY_BYTES", 16)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        assert (await web.access()).json()["status"] == "unavailable"

    async def test_the_outbound_guard_applies(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        """链接来自协议端，同样要过出站校验：协议端被攻破时不能拿它探内网"""
        monkeypatch.setattr(OsEnv, "ALLOW_PRIVATE_NETWORK_DOWNLOAD", False)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        assert (await web.access()).json()["status"] == "expired"
        assert media.get_paths == []

    async def test_a_redirect_to_a_private_address_is_stopped_per_hop(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        checked: List[str] = []
        real = video_module.assert_download_url_safe

        async def spy(url: str) -> None:
            checked.append(url)
            await real(url)

        monkeypatch.setattr(video_module, "assert_download_url_safe", spy)
        web.bot(FakeBot(f"{media.base_url}/redirect?to={media.base_url}/ranged.mp4"))
        assert (await web.access()).json()["status"] == "ok"
        assert len(checked) == 2, "每一跳都要重新校验"


class TestResources:
    """持有效签名的人能让 NA 开流、调协议端、替它下载：每一项都要有上限"""

    @pytest.fixture
    def closes(self, monkeypatch: pytest.MonkeyPatch) -> List[bool]:
        closed: List[bool] = []
        real = video_module._close_upstream  # noqa: SLF001

        async def spy(client: Any, response: Any) -> None:
            closed.append(True)
            await real(client, response)

        monkeypatch.setattr(video_module, "_close_upstream", spy)
        return closed

    async def _stall(self, web: Web, url: str) -> RawClient:
        client = RawClient(web.app, url, stall_on="http.response.body").start()
        await asyncio.wait_for(client.stalled.wait(), timeout=5)
        return client

    @staticmethod
    async def _finish(client: RawClient) -> None:
        assert client.task is not None
        client.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await client.task

    async def test_streams_per_user_are_capped(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(video_module, "MAX_STREAMS_PER_USER", 1, raising=False)
        bot = web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        assert (await web.stream(url)).status_code == 200, "一条结束之后名额要还回来"
        held = await self._stall(web, url)
        bot.calls.clear()
        busy = await web.stream(url)
        assert busy.status_code == 429
        assert bot.calls == [], "超限要在调协议端之前拒绝"
        assert (await web.access()).json()["status"] == "unavailable"
        await self._finish(held)
        assert (await web.stream(url)).status_code == 200, "断开的那一条也要把名额还回来"

    async def test_streams_in_total_are_capped(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(video_module, "MAX_STREAMS_TOTAL", 1, raising=False)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        held = await self._stall(web, await web.play_url())
        web.login(await make_user("owner", None, TenantRole.PLATFORM_OWNER, Role.Super))
        assert (await web.stream(web.signed_url())).status_code == 429
        await self._finish(held)
        assert (await web.stream(web.signed_url())).status_code == 200

    async def test_a_client_that_stops_reading_is_cut_off(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch, closes: List[bool]) -> None:
        """暂停播放、浏览器不再读：连接挂在发送上，时长上限查不到它，只能靠发送超时"""
        monkeypatch.setattr(video_module, "SEND_TIMEOUT_SECONDS", 0.2, raising=False)
        monkeypatch.setattr(video_module, "MAX_STREAMS_PER_USER", 1, raising=False)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        closes.clear()
        held = await self._stall(web, url)
        assert held.task is not None
        await asyncio.wait_for(held.task, timeout=5)
        assert closes, "上游连接要关掉"
        assert (await web.stream(url)).status_code == 200, "名额要还回来"

    @pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
    async def test_a_client_gone_before_the_first_byte_still_closes_the_upstream(
        self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch, closes: List[bool], spec_version: str,
    ) -> None:
        """生成器一次都没被迭代过，它的 finally 不会跑；收尾不能只靠它"""
        monkeypatch.setattr(video_module, "MAX_STREAMS_PER_USER", 1, raising=False)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        closes.clear()
        client = RawClient(web.app, url, stall_on="http.response.start", spec_version=spec_version, disconnect=True)
        if spec_version == "2.4":
            # 2.4 起服务器不再推断开事件，而是让 send 抛 OSError
            async def gone(message: Dict[str, Any]) -> None:
                client.messages.append(message)
                raise OSError("client went away")

            client._send = gone  # type: ignore[method-assign]  # noqa: SLF001
        client.start()
        assert client.task is not None
        await asyncio.wait_for(asyncio.gather(client.task, return_exceptions=True), timeout=5)
        assert closes, "没开始读也要关上游"
        assert (await web.stream(url)).status_code == 200, "名额要还回来"

    @pytest.fixture
    def production_app(self, web: Web) -> FastAPI:
        """生产的样子：`mount_middlewares` 挂上的整套中间件；配合 RawClient 的 spec 2.3（uvicorn 0.40 报的版本）"""
        from nekro_agent.routers import mount_middlewares

        app = FastAPI()
        app.include_router(chat_channel_router.router, prefix="/api")
        mount_middlewares(app)
        return app

    async def test_a_stream_is_cut_at_the_time_limit(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch, production_app: FastAPI) -> None:
        """到点切断，而且不能发结束帧：分块传输下那等于告诉浏览器“视频到此为止”"""
        monkeypatch.setattr(video_module, "MAX_PROXY_SECONDS", 0.3)
        monkeypatch.setattr(video_module, "CHUNK_BYTES", TRICKLE_CHUNK)
        web.bot(FakeBot(f"{media.base_url}/trickle"))
        client = RawClient(production_app, web.signed_url(), stall_on="never", spec_version="2.3").start()
        assert client.task is not None
        started = time.monotonic()
        await asyncio.wait_for(asyncio.gather(client.task, return_exceptions=True), timeout=5)
        assert time.monotonic() - started < 3
        assert client.status == 200
        assert client.body.startswith(MP4_HEAD)
        assert len(client.body) < len(MP4_HEAD) + TRICKLE_CHUNKS * TRICKLE_CHUNK
        assert not client.completed, "被切断的流不能以结束帧收尾"

    async def test_a_cut_stream_never_looks_complete_through_the_production_middlewares(
        self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch, production_app: FastAPI,
    ) -> None:
        """浏览器暂停、发送超时切断，之后浏览器又开始读：生产中间件不能替内层把结束帧补上

        以前计时中间件是 `BaseHTTPMiddleware`：内层超时返回后，外层把剩下的转完就发 `more_body: False`，
        上游没给长度（分块传输）时浏览器把截短的视频当成完整的。
        """
        monkeypatch.setattr(video_module, "SEND_TIMEOUT_SECONDS", 0.2, raising=False)
        monkeypatch.setattr(video_module, "CHUNK_BYTES", TRICKLE_CHUNK)
        web.bot(FakeBot(f"{media.base_url}/trickle"))
        client = RawClient(production_app, web.signed_url(), stall_on="http.response.body", spec_version="2.3").start()
        assert client.task is not None
        await asyncio.wait_for(client.stalled.wait(), timeout=5)
        await asyncio.sleep(0.6)
        client.resume.set()
        await asyncio.wait_for(asyncio.gather(client.task, return_exceptions=True), timeout=5)
        assert client.status == 200
        assert not client.completed, "截短的分块响应被补上了结束帧"

    async def test_the_production_stack_has_no_base_http_middleware(self, production_app: FastAPI) -> None:
        """`BaseHTTPMiddleware` 会替内层补结束帧（见上一条）；计时中间件改成纯 ASGI 后响应头照旧"""
        from starlette.middleware.base import BaseHTTPMiddleware

        assert all(not (isinstance(item.cls, type) and issubclass(item.cls, BaseHTTPMiddleware)) for item in production_app.user_middleware)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=production_app), base_url="http://test") as client:
            response = await client.get(f"/api/chat-channel/{CHAT_KEY}/messages/{MESSAGE_ID}/video")
        assert response.status_code == 404
        assert response.headers["x-process-time"].endswith("ms")

    async def test_a_client_that_never_takes_the_headers_is_cut_off(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch, closes: List[bool]) -> None:
        """连响应头都不收：发送超时同样要管到 `http.response.start`"""
        monkeypatch.setattr(video_module, "SEND_TIMEOUT_SECONDS", 0.2, raising=False)
        monkeypatch.setattr(video_module, "MAX_STREAMS_PER_USER", 1, raising=False)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        closes.clear()
        client = RawClient(web.app, url, stall_on="http.response.start").start()
        assert client.task is not None
        await asyncio.wait_for(asyncio.gather(client.task, return_exceptions=True), timeout=5)
        assert closes, "上游连接要关掉"
        assert (await web.stream(url)).status_code == 200, "名额要还回来"

    @pytest.mark.parametrize(("cap", "complete"), [(212, False), (256, False), (len(BODY), True), (len(BODY) + 1000, True)])
    async def test_a_chunked_stream_cut_at_the_size_cap_never_looks_complete(
        self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch, production_app: FastAPI, cap: int, complete: bool,
    ) -> None:
        """上游不给长度时，大小上限只是上限：到了上限后面还有数据，就是切断，不能以结束帧收尾"""
        monkeypatch.setattr(video_module, "MAX_PROXY_BYTES", cap)
        monkeypatch.setattr(video_module, "CHUNK_BYTES", 64)
        web.bot(FakeBot(f"{media.base_url}/chunked"))
        client = RawClient(production_app, web.signed_url(), stall_on="never", spec_version="2.3").start()
        assert client.task is not None
        outcome = await asyncio.wait_for(asyncio.gather(client.task, return_exceptions=True), timeout=5)
        assert outcome == [None], "切断是预期内的结束方式，不该变成应用异常（服务器会记一条带堆栈的错误）"
        assert client.status == 200
        assert client.body == BODY[: min(cap, len(BODY))]
        assert client.completed is complete

    def test_tenants_together_cannot_crowd_out_the_others(self) -> None:
        """几个租户同时撑满，其余租户仍有保底、平台方仍有名额"""
        slots = video_module.StreamSlots()

        def fill(tenant: Optional[str], users: int) -> int:
            taken = 0
            for index in range(users):
                while slots.try_acquire(f"{tenant}-{index}", tenant) is not None:
                    taken += 1
            return taken

        for tenant in ("a", "b", "c"):
            assert fill(tenant, 3) <= video_module.MAX_STREAMS_PER_TENANT
        busy = slots.in_use()
        late = fill("late", 3)
        assert late >= 2, "后来的租户至少能看一个视频（播放加拖动时重叠的那一次请求）"
        platform = fill(None, 10)
        assert platform >= 1, "平台方不能被挤到 429"
        # 具体的分配：三个租户合计不超过全局减保留，后来的租户拿到保底，平台方拿到剩下的全部
        assert busy <= video_module.MAX_STREAMS_TOTAL - video_module.RESERVED_STREAMS
        assert late == video_module.TENANT_GUARANTEED_STREAMS
        assert platform == video_module.MAX_STREAMS_TOTAL - busy - late

    async def test_streams_per_tenant_are_capped(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        """一个租户的几个管理员撑满全局，别的租户就只能看 429：按租户再分一层"""
        monkeypatch.setattr(video_module, "MAX_STREAMS_PER_TENANT", 1, raising=False)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        held = await self._stall(web, await web.play_url())
        web.login(await make_user("acme2", ACME_TENANT, TenantRole.TENANT_ADMIN, Role.Admin))
        assert (await web.stream(web.signed_url())).status_code == 429, "同租户的另一个管理员"
        web.login(await make_user("owner", None, TenantRole.PLATFORM_OWNER, Role.Super))
        assert (await web.stream(web.signed_url())).status_code == 200, "平台方不受租户上限约束"
        await seed(OTHER_CHAT_KEY, instance_key=OTHER_INSTANCE)
        web.login(await make_user("rival", RIVAL_TENANT, TenantRole.TENANT_ADMIN, Role.Admin))
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4", group_id=1002))
        assert (await web.stream(web.signed_url(OTHER_CHAT_KEY))).status_code == 200, "别的租户不受影响"
        await self._finish(held)
        web.login(await make_user("acme3", ACME_TENANT, TenantRole.TENANT_ADMIN, Role.Admin))
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        assert (await web.stream(web.signed_url())).status_code == 200, "名额要还回来"

    async def test_a_far_seek_against_an_upstream_without_range_is_416_before_reading(
        self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """上游不认 Range 时要先读完前面的字节再丢：起点超过上限直接 416，不让 NA 先替人下载"""
        monkeypatch.setattr(video_module, "MAX_SKIP_BYTES", 1024, raising=False)
        web.bot(FakeBot(f"{media.base_url}/plain.mp4"))
        url = await web.play_url()
        far = await web.stream(url, "bytes=1500-")
        assert far.status_code == 416
        assert far.headers["content-range"] == f"bytes */{len(BODY)}"
        assert far.content == b""
        tail = await web.stream(url, "bytes=-10")
        assert tail.status_code == 416, "后缀区间的起点同样要读完前面"
        near = await web.stream(url, "bytes=1000-1099")
        assert near.status_code == 206 and near.content == BODY[1000:1100]

    async def test_a_stream_that_ends_without_a_body_gives_its_slot_back(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        """失败（410）与 416 都没有正文，不经过流式响应，名额要在接口里还"""
        monkeypatch.setattr(video_module, "MAX_STREAMS_PER_USER", 1, raising=False)
        web.bot(FakeBot(f"{media.base_url}/plain.mp4"))
        url = await web.play_url()
        for _ in range(2):
            assert (await web.stream(url, f"bytes={len(BODY) + 5}-")).status_code == 416
        web.bot(FakeBot(f"{media.base_url}/gone"))
        for _ in range(2):
            assert (await web.stream(url)).status_code == 410
        web.bot(BotInstanceOfflineError(INSTANCE))
        for _ in range(2):
            assert (await web.stream(url)).status_code == 503
        assert video_module.video_stream_slots.in_use() == 0

        async def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("数据库挂了")

        monkeypatch.setattr(chat_channel_router, "find_video_record", broken)
        with pytest.raises(RuntimeError):
            await web.stream(url)
        assert video_module.video_stream_slots.in_use() == 0, "意料之外的异常也要还名额"

    async def test_an_upstream_with_range_support_can_seek_anywhere(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(video_module, "MAX_SKIP_BYTES", 1024, raising=False)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        response = await web.stream(await web.play_url(), "bytes=1500-")
        assert response.status_code == 206 and response.content == BODY[1500:]


class TestPermissions:
    async def test_another_tenant_gets_404_at_issue(self, web: Web) -> None:
        web.bot(FakeBot("https://a.example.com/x"))
        web.login(await make_user("rival", RIVAL_TENANT, TenantRole.TENANT_ADMIN, Role.Admin))
        assert (await web.access()).status_code == 404

    async def test_a_message_of_another_channel_is_404(self, web: Web) -> None:
        web.bot(FakeBot("https://a.example.com/x"))
        await seed(OTHER_CHAT_KEY, message_id=888)
        response = await web.access(CHAT_KEY, 888)
        assert response.status_code == 404

    async def test_an_unknown_message_is_404(self, web: Web) -> None:
        web.bot(FakeBot("https://a.example.com/x"))
        assert (await web.access(CHAT_KEY, 999)).status_code == 404

    @pytest.mark.parametrize("message_id", ["abc", "-1", "１２３"])
    async def test_a_malformed_id_is_404(self, web: Web, message_id: str) -> None:
        web.bot(FakeBot("https://a.example.com/x"))
        assert (await web.access(CHAT_KEY, message_id)).status_code == 404

    @pytest.mark.parametrize("shape", ["four-parts", "five-parts"])
    async def test_an_absurd_user_id_is_a_404_not_a_500(self, web: Web, media: RangeUpstream, shape: str) -> None:
        """匿名就能发的请求：40 位用户号拿去查库，SQLite 抛 OverflowError、PostgreSQL 抛 DataError，都是 500"""
        bot = web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        tail = f".{'0' * 64}" if shape == "five-parts" else ""
        response = await web.client.get(
            f"/api/chat-channel/{CHAT_KEY}/messages/{MESSAGE_ID}/video?sig=v1.{int(time.time()) + 600}.{HUGE_USER}.{'0' * 64}{tail}",
        )
        assert response.status_code == 404
        assert bot.calls == []

    async def test_a_forged_token_never_reaches_the_database(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        """格式对、签名错的令牌：不带登录态的接口不该为它查一次库"""
        lookups: List[Any] = []
        real = DBUser.get_or_none

        async def spy(*args: Any, **kwargs: Any) -> Any:
            lookups.append(kwargs)
            return await real(*args, **kwargs)

        monkeypatch.setattr(DBUser, "get_or_none", spy)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        good = web.signed_url()
        token = good.split("sig=", 1)[1]
        prefix, expiry, user_id, _digest, generation_digest = token.split(".")
        forged = ".".join([prefix, expiry, user_id, "0" * 64, generation_digest])
        assert (await web.stream(good.replace(token, forged))).status_code == 404
        assert lookups == []
        assert (await web.stream(good)).status_code == 200, "对照：真令牌照常查库、照常放行"
        assert lookups

    async def test_the_stream_without_a_signature_is_404(self, web: Web, media: RangeUpstream) -> None:
        bot = web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        response = await web.client.get(f"/api/chat-channel/{CHAT_KEY}/messages/{MESSAGE_ID}/video")
        assert response.status_code == 404
        assert bot.calls == [], "签名没过之前不能碰协议端"

    async def test_an_expired_signature_is_404(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        response = await web.stream(web.signed_url(now=int(time.time()) - 3 * VIDEO_ACCESS_WINDOW_SECONDS))
        assert response.status_code == 404

    async def test_after_expiry_a_new_address_can_be_fetched(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        assert (await web.stream(await web.play_url())).status_code == 200

    async def test_a_signature_for_one_message_does_not_open_another(self, web: Web, media: RangeUpstream) -> None:
        await seed(message_id=778)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        assert (await web.stream(url.replace(f"/{MESSAGE_ID}/", "/778/"))).status_code == 404

    async def test_a_signature_cannot_be_moved_to_another_channel(self, web: Web, media: RangeUpstream) -> None:
        await seed(OTHER_CHAT_KEY)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        assert (await web.stream(url.replace(CHAT_KEY, OTHER_CHAT_KEY))).status_code == 404

    async def test_a_user_moved_out_of_the_tenant_loses_access_at_once(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        user = web.app.dependency_overrides[get_current_active_user]()
        url = await web.play_url()
        await DBUser.filter(id=user.id).update(tenant_id=RIVAL_TENANT)
        assert (await web.stream(url)).status_code == 404

    async def test_a_deleted_user_loses_access(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        user = web.app.dependency_overrides[get_current_active_user]()
        url = await web.play_url()
        await DBUser.filter(id=user.id).delete()
        assert (await web.stream(url)).status_code == 404

    async def test_a_demoted_user_loses_access_at_once(self, web: Web, media: RangeUpstream) -> None:
        """签发要 Admin，转发也要：降级之后还能用 20 分钟就是越权"""
        bot = web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        await DBUser.filter(id=web.user().id).update(perm_level=Role.User)
        bot.calls.clear()
        assert (await web.stream(url)).status_code == 404
        assert bot.calls == []

    async def test_a_password_change_revokes_the_address(self, web: Web, media: RangeUpstream) -> None:
        """与登录令牌同一套口令世代：自助改密或管理员重置之后，签出去的地址一起作废"""
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        user = web.user()
        await DBUser.filter(id=user.id).update(password_generation=user.password_generation + 1)
        assert (await web.stream(url)).status_code == 404

    async def test_rotating_the_admin_password_revokes_the_address(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        """admin 的口令在环境变量里，世代里带它的指纹"""
        monkeypatch.setattr(OsEnv, "ADMIN_PASSWORD", "before-rotation")
        web.login(await make_user("admin", None, TenantRole.PLATFORM_OWNER, Role.Super))
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        assert (await web.stream(url)).status_code == 200
        monkeypatch.setattr(OsEnv, "ADMIN_PASSWORD", "after-rotation")
        assert (await web.stream(url)).status_code == 404

    async def test_a_banned_user_loses_access(self, web: Web, media: RangeUpstream) -> None:
        bot = web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        await DBUser.filter(id=web.user().id).update(ban_until=until)
        bot.calls.clear()
        assert (await web.stream(url)).status_code == 404
        assert bot.calls == []

    async def test_an_unchanged_user_keeps_access(self, web: Web, media: RangeUpstream) -> None:
        """上面几条的对照：同一个地址，什么都没变时能放"""
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        url = await web.play_url()
        assert (await web.stream(url)).status_code == 200
        assert (await web.stream(url)).status_code == 200

    async def test_a_member_below_admin_cannot_get_an_address(self, web: Web) -> None:
        web.bot(FakeBot("https://a.example.com/x"))
        web.login(await make_user("member", ACME_TENANT, TenantRole.TENANT_USER, Role.User))
        assert (await web.access()).status_code == 403

    async def test_the_platform_owner_can_watch(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        web.login(await make_user("owner", None, TenantRole.PLATFORM_OWNER, Role.Super))
        assert (await web.stream(await web.play_url())).status_code == 200

    async def test_another_adapter_is_not_served(self, web: Web) -> None:
        """Telegram 之类的视频是落盘文件，走上传目录，这条接口不认识"""
        chat_key = "telegram-group_55"
        await DBChatChannel.create(adapter_key="telegram", instance_key="", channel_id="group_55", chat_key=chat_key, channel_type="group", data="{}")
        web.bot(FakeBot("https://a.example.com/x"))
        web.login(await make_user("owner", None, TenantRole.PLATFORM_OWNER, Role.Super))
        assert (await web.access(chat_key)).status_code == 404

    async def test_another_adapters_message_never_reaches_a_onebot_account(self, web: Web, media: RangeUpstream) -> None:
        """别的适配器的频道里就算有一条长得像占位的视频段，也不能拿它的编号去问 OneBot 账号

        `telegram-group_55` 解析不出 OneBot 实例段，`get_bot_by_chat_key` 会退回“当前在线的那个 OneBot 账号”：
        接口只要放行了别的适配器，就会用 Telegram 的消息编号向 QQ 账号 `get_msg`，播出来的是另一条消息。
        签发与转发两条路都要在碰协议端之前 404。
        """
        chat_key = "telegram-group_55"
        await DBChatChannel.create(adapter_key="telegram", instance_key="", channel_id="group_55", chat_key=chat_key, channel_type="group", data="{}")
        await seed(chat_key, adapter_key="telegram", instance_key="")
        bot = web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        owner = await make_user("owner", None, TenantRole.PLATFORM_OWNER, Role.Super)
        web.login(owner)

        assert (await web.access(chat_key)).status_code == 404
        response = await web.stream(web.signed_url(chat_key))
        assert response.status_code == 404
        assert bot.calls == [], "别的适配器的频道不能碰 OneBot 协议端"
        assert media.get_paths == []

        # 对照：同一条记录放在 OneBot 频道里是能播的，说明 404 来自适配器判据而不是记录本身
        assert (await web.stream(await web.play_url())).status_code == 200


class TestRequestTiming:
    """计时中间件：所有 HTTP 请求都经过它，匿名请求也一样"""

    @pytest.fixture
    def production_app(self) -> FastAPI:
        from nekro_agent.routers import mount_middlewares

        app = FastAPI()
        app.include_router(chat_channel_router.router, prefix="/api")
        mount_middlewares(app)
        return app

    @pytest.mark.parametrize("path", ["/x%3C/y%3E", "/a%3Cred%3Eb", "/%3C/%3E"])
    async def test_markup_in_the_path_is_logged_not_parsed(self, production_app: FastAPI, path: str) -> None:
        """有 DEBUG 级别的输出时，路径里的 `</y>` 不能被当成颜色标签解析（以前抛 ValueError，匿名请求打出 500）"""
        captured: List[str] = []
        sink = loguru_logger.add(lambda message: captured.append(str(message)), level="DEBUG", colorize=True)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=production_app), base_url="http://test") as client:
                response = await client.get(path)
        finally:
            loguru_logger.remove(sink)
        assert response.status_code == 404
        assert response.headers["x-process-time"].endswith("ms")
        decoded = unquote(path)
        assert any(decoded in line for line in captured), captured[-3:]

    async def test_a_response_start_without_headers_is_fine(self) -> None:
        """ASGI 允许 `http.response.start` 不带 `headers`"""
        from nekro_agent.routers import RequestTimingMiddleware

        async def bare(_scope: Any, _receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 204})
            await send({"type": "http.response.body", "body": b""})

        sent: List[Dict[str, Any]] = []

        async def receive() -> Dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Dict[str, Any]) -> None:
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("127.0.0.1", 1)}
        await RequestTimingMiddleware(bare)(scope, receive, send)
        assert sent[0]["status"] == 204
        assert any(name.lower() == b"x-process-time" for name, _ in sent[0]["headers"])


class TestIndependence:
    async def test_it_works_with_the_plugin_off(self, web: Web, media: RangeUpstream) -> None:
        assert global_config.PLUGIN_ENABLED == []
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        assert (await web.stream(await web.play_url())).status_code == 200

    def test_the_core_does_not_import_the_plugin(self) -> None:
        for module in (video_module, chat_channel_router):
            source = Path(module.__file__ or "").read_text(encoding="utf-8")
            assert "plugins.builtin" not in source and "from plugins" not in source

    def test_the_plugin_uses_the_shared_service(self) -> None:
        source = (Path(__file__).parent.parent / "plugins" / "builtin" / "video_analysis" / "source.py").read_text(encoding="utf-8")
        assert "adapters.onebot_v11.tools.video" in source
        assert 'call_api("get_msg"' not in source


class TestNothingLeaks:
    async def test_the_link_stays_out_of_the_response_and_the_logs(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/gone?rkey={URL_SECRET}"))
        captured: List[str] = []
        sink = loguru_logger.add(lambda message: captured.append(str(message)), level="DEBUG")
        try:
            body = await web.access()
        finally:
            loguru_logger.remove(sink)
        assert URL_SECRET not in body.text
        assert URL_SECRET not in "".join(captured)

    async def test_the_address_handed_to_the_browser_carries_no_upstream_link(self, web: Web, media: RangeUpstream) -> None:
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4?rkey={URL_SECRET}"))
        url = await web.play_url()
        assert URL_SECRET not in url and "ranged" not in url
        assert url.startswith(f"/api/chat-channel/{CHAT_KEY}/messages/{MESSAGE_ID}/video?sig=")

    async def test_the_probe_closes_the_upstream(self, web: Web, media: RangeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
        closed: List[bool] = []
        real = video_module._close_upstream  # noqa: SLF001

        async def spy(client: Any, response: Any) -> None:
            closed.append(True)
            await real(client, response)

        monkeypatch.setattr(video_module, "_close_upstream", spy)
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        await web.access()
        assert closed, "探测只要一个字节，没读 body 也要把连接关掉"

    async def test_nothing_is_written_to_disk(self, web: Web, media: RangeUpstream, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(OsEnv, "DATA_DIR", str(tmp_path))
        web.bot(FakeBot(f"{media.base_url}/ranged.mp4"))
        await web.stream(await web.play_url())
        assert list(tmp_path.rglob("*")) == []


class TestFrontendSource:
    ROOT = Path(__file__).parent.parent / "frontend" / "src"

    def _read(self, relative: str) -> str:
        return (self.ROOT / relative).read_text(encoding="utf-8")

    def test_a_placeholder_video_renders_the_player_not_the_raw_text(self) -> None:
        history = self._read("pages/chat-channel/components/detail-tabs/MessageHistory.tsx")
        assert "seg.type === 'video' && !seg.file_name" in history
        assert "<MessageVideoPlayer" in history
        assert history.index("<MessageVideoPlayer") < history.index("<FileCard\n"), "占位视频要先于文件卡片判断"

    def test_a_video_with_a_file_name_stays_a_file_card(self) -> None:
        """别的适配器的落盘文件不受影响"""
        history = self._read("pages/chat-channel/components/detail-tabs/MessageHistory.tsx")
        assert "seg.type === 'file' || seg.type === 'voice' || seg.type === 'video'" in history

    def test_the_player_is_a_real_video_element_loaded_on_demand(self) -> None:
        player = self._read("pages/chat-channel/components/detail-tabs/MessageVideoPlayer.tsx")
        assert 'component="video"' in player and "controls" in player
        assert "getMessageVideoAccess" in player
        assert "phase: 'idle'" in player, "点开才加载：滚动列表不该给协议端和 QQ 发请求"
        assert "onError={handleVideoError}" in player

    def test_the_raw_placeholder_is_not_shown(self) -> None:
        player = self._read("pages/chat-channel/components/detail-tabs/MessageVideoPlayer.tsx")
        assert "{segment.text}" not in player

    @pytest.mark.parametrize("lang", ["zh-CN", "en-US"])
    def test_every_status_has_readable_text(self, lang: str) -> None:
        errors = json.loads(self._read(f"locales/{lang}/chat-channel.json"))["messageHistory"]["video"]["error"]
        assert set(errors) == {"recalled", "offline", "expired", "unavailable", "not_found", "playback"}
        assert all(errors.values())
