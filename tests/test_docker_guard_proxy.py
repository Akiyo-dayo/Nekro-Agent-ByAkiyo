"""docker-guard 代理层的归属门单测（docs/docker-guard.md）。

`policy.py` 的表驱动判据只回答「NA 能对 Docker 做哪个动作」，回答不了「对哪个容器做」。
按 ID 指向具体容器的端点（exec/logs/inspect/start/stop/…）若不加归属校验，NA 一旦被攻破就能
`exec` 进宿主上别人的容器（挂了 docker.sock 的 → 宿主 root）、或 `inspect` 兄弟 postgres
读走 `POSTGRES_PASSWORD`。这道门在 `proxy.py` 的 `handle()` 里，本文件把它钉住。

**为什么要有代理层的 handle() 测试而不是只测纯函数**：见 §2.7——「零件全对、调用点没了，
测试会全绿」。归属判据本身（`labels_indicate_ownership` / `container_ownership_target`）在
`test_docker_guard_policy.py` 里已表驱动覆盖；这里专门盯**它被接进了转发路径**这件事：
foreign 容器的 exec 请求必须在 `handle()` 里被挡下、`_forward` 根本不该被调用到。
"""

import asyncio
import json
from typing import Any, Dict, List, Tuple
from unittest import mock

import aiohttp
from aiohttp import streams, web
from aiohttp.test_utils import make_mocked_request
from yarl import URL

from nekro_agent.services.docker_guard.policy import PolicyContext
from nekro_agent.services.docker_guard.proxy import _MAX_OWNED_IDS, DockerGuardProxy
from tests.docker_guard_fakefs import data_dir_fs

DATA_DIR = "/srv/nekro_agent"
FULL_ID = "a" * 64  # docker create 返回的完整 64 位容器 ID


def _make_proxy() -> DockerGuardProxy:
    return DockerGuardProxy(
        "/nonexistent.sock",
        PolicyContext(allowed_bind_roots=(DATA_DIR,), fs=data_dir_fs(DATA_DIR)),
    )


def _request(method: str, path: str, body: bytes = b"") -> web.Request:
    if body:
        loop = asyncio.get_event_loop()
        protocol = mock.Mock()
        payload = streams.StreamReader(protocol, 2**16, loop=loop)
        payload.feed_data(body)
        payload.feed_eof()
        return make_mocked_request(method, path, payload=payload)
    return make_mocked_request(method, path)


# ---------------------------------------------------------------------------
# 快表：亲手建的容器 ID 记忆（完整/短 ID/名称）
# ---------------------------------------------------------------------------


def test_owned_id_full_short_and_forget():
    proxy = _make_proxy()
    assert proxy._is_owned_id(FULL_ID) is False
    proxy._remember_owned(FULL_ID)
    assert proxy._is_owned_id(FULL_ID) is True          # 完整 ID 精确命中
    assert proxy._is_owned_id(FULL_ID[:12]) is True     # 短 ID（>=12 位）前缀命中
    assert proxy._is_owned_id("a" * 8) is False         # 太短，不做前缀匹配（避免误判）
    assert proxy._is_owned_id("b" * 64) is False        # 别的容器
    proxy._forget_owned(FULL_ID)
    assert proxy._is_owned_id(FULL_ID) is False


def test_owned_id_set_is_bounded():
    proxy = _make_proxy()
    for i in range(_MAX_OWNED_IDS + 50):
        proxy._remember_owned(f"{i:064x}")
    assert len(proxy._owned_ids) <= _MAX_OWNED_IDS       # 有上限，不会无界增长
    assert proxy._is_owned_id(f"{_MAX_OWNED_IDS + 49:064x}") is True  # 最新的还在


# ---------------------------------------------------------------------------
# create 响应解析出新容器 ID
# ---------------------------------------------------------------------------

NOTE_CREATED_CASES: List[Tuple[int, bytes, bool]] = [
    (201, json.dumps({"Id": FULL_ID, "Warnings": []}).encode(), True),   # 正常 create
    (200, json.dumps({"Id": FULL_ID}).encode(), True),
    (500, json.dumps({"Id": FULL_ID}).encode(), False),                  # 失败响应不登记
    (201, b"not json", False),                                           # 解析失败不登记
    (201, json.dumps({"Warnings": []}).encode(), False),                 # 无 Id 不登记
]


def test_note_created_response():
    for status, raw, expected in NOTE_CREATED_CASES:
        proxy = _make_proxy()
        proxy._note_created_response(status, raw)
        assert proxy._is_owned_id(FULL_ID) is expected, f"status={status} raw={raw!r}"


# ---------------------------------------------------------------------------
# 归属判定：快表未命中时按 Labels 兜底（长命容器跨代理重启）
# ---------------------------------------------------------------------------


async def test_is_owned_container_uses_fast_path_without_inspect():
    """亲手建的容器走快表，绝不触发 inspect（inspect 一被调用就让它爆，证明没走）。"""
    proxy = _make_proxy()
    proxy._remember_owned(FULL_ID)
    proxy._inspect_labels = mock.AsyncMock(side_effect=AssertionError("不该 inspect"))  # type: ignore[method-assign]
    assert await proxy._is_owned_container(FULL_ID) is True


async def test_is_owned_container_label_fallback():
    proxy = _make_proxy()
    # 带 nekro.* 标签的长命容器（如 CC 工作区）→ 判为本部署，并回填快表
    proxy._inspect_labels = mock.AsyncMock(return_value={"nekro.owner.scope": "myfork-"})  # type: ignore[method-assign]
    assert await proxy._is_owned_container("b" * 64) is True
    assert proxy._is_owned_id("b" * 64) is True  # 回填后下次走快表

    # 兄弟 postgres / 第三方容器：无 nekro.* 标签 → 判为非本部署
    proxy2 = _make_proxy()
    proxy2._inspect_labels = mock.AsyncMock(return_value={"maintainer": "postgres"})  # type: ignore[method-assign]
    assert await proxy2._is_owned_container("c" * 64) is False

    # inspect 失败（容器不存在）→ 归属无法确证，按非本部署处理
    proxy3 = _make_proxy()
    proxy3._inspect_labels = mock.AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await proxy3._is_owned_container("d" * 64) is False


async def test_inspect_labels_rejects_unsafe_ref_without_touching_upstream():
    """请求体里来的容器引用会被拼进一条 inspect URL —— 含 `/`/`..` 时 yarl 会把它改写成
    对**另一个**（攻击者名下的）容器的 inspect，从而骗过归属判定。

    这是判据函数 `is_safe_container_ref` 的**调用点**断言（§2.7：零件对不等于接上了）：
    引用不合法时连上游会话都不该去取。
    """
    proxy = _make_proxy()
    proxy._get_session = mock.AsyncMock(side_effect=AssertionError("不该去连上游"))  # type: ignore[method-assign]

    assert await proxy._inspect_labels("victim/../../containers/mine") is None
    assert await proxy._is_owned_container("victim/../../containers/mine") is False


# ---------------------------------------------------------------------------
# handle() 归属门：接线证据（foreign 容器的按 ID 操作必须在此被挡下）
# ---------------------------------------------------------------------------


async def _forward_should_not_run(*_a: Any, **_k: Any) -> web.StreamResponse:
    raise AssertionError("越权请求不该被转发到宿主 docker.sock")


async def test_handle_blocks_exec_into_foreign_container():
    """exec 进一个不归属本部署的容器 → 拒绝，且绝不转发。"""
    proxy = _make_proxy()
    proxy._is_owned_container = mock.AsyncMock(return_value=False)  # type: ignore[method-assign]
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]

    resp = await proxy.handle(_request("POST", "/containers/nekro_dev_postgres/exec", body=b'{"Cmd":["id"]}'))
    assert resp.status == 404
    proxy._is_owned_container.assert_awaited_once()


async def test_handle_blocks_inspect_foreign_container():
    """inspect 兄弟 postgres 读环境变量（含 POSTGRES_PASSWORD）→ 拒绝。"""
    proxy = _make_proxy()
    proxy._is_owned_container = mock.AsyncMock(return_value=False)  # type: ignore[method-assign]
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]

    resp = await proxy.handle(_request("GET", "/containers/nekro_dev_postgres/json"))
    assert resp.status == 404


async def test_ownership_denial_is_docker_shaped_404_not_403():
    """归属门拒绝必须长成 Docker 的「没有这个容器」，否则 NA 自己的 404 分支全部失效。

    `_is_owned_container` 对「不是本部署的」和「压根不存在」返回同一个 False。回 403 的话，
    `console/container_manager.py:_inspect_container`（`if e.status == 404: return None`，
    NapCat **首次创建**必经）、`_ensure_image`（404 才去 pull）、
    `adapters/onebot_v11/routers.py` 的候选名遍历（404 才 continue）这些分支就永远走不到，
    硬化部署下第一次装 NapCat 直接失败。这条断言同时钉住状态码与文案形状。
    """
    proxy = _make_proxy()
    proxy._is_owned_container = mock.AsyncMock(return_value=False)  # type: ignore[method-assign]
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]

    resp = await proxy.handle(_request("GET", "/containers/not_created_yet/json"))
    assert resp.status == 404
    # aiodocker 把响应体里的 message 直接带进 DockerError；沿用 Docker 原文案
    assert json.loads(resp.body)["message"] == "No such container: not_created_yet"  # type: ignore[arg-type]


async def test_handle_blocks_network_connect_to_foreign_container():
    """归属门的第二个出口：`/networks/{id}/connect` 的目标容器在**请求体**里。

    URL 上没有容器 ID，只按 URL 判归属的话这条完全漏过去——攻击者能把宿主上别人的容器
    接进 NA 的沙盒/工作区网络。
    """
    proxy = _make_proxy()
    owned = mock.AsyncMock(return_value=False)
    proxy._is_owned_container = owned  # type: ignore[method-assign]
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]

    resp = await proxy.handle(
        _request("POST", "/networks/nekro_sandbox_net/connect", body=b'{"Container":"nekro_dev_postgres"}'),
    )
    assert resp.status == 404
    owned.assert_awaited_once_with("nekro_dev_postgres")


async def test_handle_allows_network_connect_to_owned_container():
    proxy = _make_proxy()
    proxy._is_owned_container = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
    forward = mock.AsyncMock(return_value=web.Response(status=200))
    proxy._forward = forward  # type: ignore[method-assign]

    resp = await proxy.handle(
        _request("POST", "/networks/nekro_sandbox_net/connect", body=b'{"Container":"' + FULL_ID.encode() + b'"}'),
    )
    assert resp.status == 200
    forward.assert_awaited_once()


async def test_handle_denies_traversal_before_ownership_and_forward():
    """`/images/x/../../containers/<兄弟容器>/json`：转发时 yarl 会把它改写成对任意容器的 inspect。

    判据在 handle() 里就该把它挡掉——归属门对这条路径**算不出目标**（URL 长得像镜像 inspect），
    所以这里不能指望归属门兜底。
    """
    proxy = _make_proxy()
    owned = mock.AsyncMock(return_value=True)  # 就算归属判定说「是自己的」也不该走到转发
    proxy._is_owned_container = owned  # type: ignore[method-assign]
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]

    resp = await proxy.handle(_request("GET", "/images/x/../../containers/nekro_dev_postgres/json"))
    assert resp.status == 403
    assert "相对段" in json.loads(resp.body)["message"]  # type: ignore[arg-type]
    owned.assert_not_awaited()


async def test_handle_denies_hijacked_exec_start():
    """Detach=false 的 exec 启动是劫持流，guard 转不了 —— 明确拒绝而不是让它挂住。"""
    proxy = _make_proxy()
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]

    resp = await proxy.handle(_request("POST", "/exec/deadbeef/start", body=b'{"Detach":false,"Tty":true}'))
    assert resp.status == 403
    assert "劫持" in json.loads(resp.body)["message"]  # type: ignore[arg-type]


async def test_handle_denies_privileged_exec_create():
    """exec 的 Privileged 与 create 的同名同义，必须一起判。"""
    proxy = _make_proxy()
    proxy._is_owned_container = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]

    resp = await proxy.handle(
        _request("POST", f"/containers/{FULL_ID}/exec", body=b'{"Cmd":["sh"],"Privileged":true}'),
    )
    assert resp.status == 403


async def test_handle_allows_ops_on_owned_container():
    """归属本部署的容器（如自己的 CC 工作区）→ 正常转发。"""
    proxy = _make_proxy()
    proxy._is_owned_container = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
    sentinel = web.Response(status=200, text="ok")
    forward = mock.AsyncMock(return_value=sentinel)
    proxy._forward = forward  # type: ignore[method-assign]

    resp = await proxy.handle(_request("GET", f"/containers/{FULL_ID}/json"))
    assert resp is sentinel
    forward.assert_awaited_once()


async def test_handle_non_container_endpoint_skips_ownership():
    """不指向具体容器的端点（version/create/list）不触发归属判定。"""
    proxy = _make_proxy()
    owned = mock.AsyncMock(return_value=True)
    proxy._is_owned_container = owned  # type: ignore[method-assign]
    proxy._forward = mock.AsyncMock(return_value=web.Response(status=200))  # type: ignore[method-assign]

    await proxy.handle(_request("GET", "/version"))
    await proxy.handle(_request("GET", "/containers/json"))
    owned.assert_not_awaited()  # 这些端点根本不该问归属


async def test_handle_create_routes_with_capture_flag():
    """create 走缓冲分支（capture_created=True），以便记住新容器 ID。"""
    proxy = _make_proxy()
    captured: Dict[str, Any] = {}

    async def fake_forward(request: web.Request, body_bytes: bytes, capture_created: bool = False) -> web.StreamResponse:
        captured["capture"] = capture_created
        # 顺带模拟上游 201，验证 handle 之后新 ID 进快表（经真实解析路径）
        proxy._note_created_response(201, json.dumps({"Id": FULL_ID}).encode())
        return web.Response(status=201)

    proxy._forward = fake_forward  # type: ignore[method-assign]
    body = json.dumps({"Image": "x", "Cmd": ["true"]}).encode()
    await proxy.handle(_request("POST", "/containers/create", body=body))
    assert captured["capture"] is True
    assert proxy._is_owned_id(FULL_ID) is True  # 建完即可对它做后续操作，无需 inspect


async def test_handle_delete_forgets_owned_id():
    """删除成功后从快表摘掉（避免 ID 复用误判 + 控制集合规模）。"""
    proxy = _make_proxy()
    proxy._remember_owned(FULL_ID)
    proxy._is_owned_container = mock.AsyncMock(return_value=True)  # type: ignore[method-assign]
    proxy._forward = mock.AsyncMock(return_value=web.Response(status=204))  # type: ignore[method-assign]

    await proxy.handle(_request("DELETE", f"/containers/{FULL_ID}?force=true"))
    assert proxy._is_owned_id(FULL_ID) is False


# ---------------------------------------------------------------------------
# _forward 必须按**原样字节**转发：判到的路径与发出去的路径不能是两条
# ---------------------------------------------------------------------------


class _RecordingSession:
    """只记录 `request()` 的调用参数，不产生任何网络行为。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        raise aiohttp.ClientError("测试到此为止，只看请求怎么发出去")  # 转发后的响应处理不在本条断言范围


async def test_forward_sends_path_verbatim_and_never_follows_redirects():
    """yarl 传字符串 URL 时会自己消解 `..`，把已判过的路径换成另一条。

    `URL("http://docker/images/x/../../containers/victim/json").path == "/containers/victim/json"`
    —— 判据看到 images.inspect（不查归属），Docker 收到的却是对任意容器的 inspect。
    所以 `_forward` 必须 `encoded=True`（原样字节）且 `allow_redirects=False`
    （一次 30x 同样能把路径换掉）。
    """
    proxy = _make_proxy()
    session = _RecordingSession()
    proxy._get_session = mock.AsyncMock(return_value=session)  # type: ignore[method-assign]

    raw = "/images/x/../../containers/victim/json"
    resp = await proxy._forward(_request("GET", raw), b"")
    assert resp.status == 502  # 上游连接失败分支，说明确实走到了发请求这一步

    assert len(session.calls) == 1
    sent: Any = session.calls[0]["url"]
    assert isinstance(sent, URL)
    assert sent.raw_path == raw, f"转发路径被改写了: {sent.raw_path!r}"
    assert session.calls[0]["allow_redirects"] is False


async def test_forward_keeps_query_string():
    """原样转发不能把查询串弄丢（logs/wait/delete 全靠它传参）。"""
    proxy = _make_proxy()
    session = _RecordingSession()
    proxy._get_session = mock.AsyncMock(return_value=session)  # type: ignore[method-assign]

    await proxy._forward(_request("GET", "/containers/abcd/logs?stdout=true&follow=true"), b"")
    assert session.calls[0]["url"].raw_path_qs == "/containers/abcd/logs?stdout=true&follow=true"


async def test_handle_still_denies_privileged_create_before_ownership():
    """归属门不该削弱既有的请求体判据：特权 create 仍在更早一步被 403。"""
    proxy = _make_proxy()
    proxy._forward = _forward_should_not_run  # type: ignore[method-assign]
    body = json.dumps({"Image": "x", "HostConfig": {"Privileged": True}}).encode()
    resp = await proxy.handle(_request("POST", "/containers/create", body=body))
    assert resp.status == 403
    assert "Privileged" in json.loads(resp.body)["message"]  # type: ignore[arg-type]
