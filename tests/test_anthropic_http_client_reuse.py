"""`anthropic.py` 出站请求的两条性质：连接复用，以及鉴权首部的取值

## 一、必须走共享连接池，而不是每次请求新建一个客户端

`anthropic.py` 早先用 `async with _create_http_client(...)`：客户端连同连接池在本次请求
结束时就被关掉，keep-alive 完全失效，每一次模型组连通性测试都要重做一遍 TCP + TLS 握手。
实测 5 次调用建了 5 条连接；改走 `openai._get_http_client` 后同样 5 次调用只建 1 条。

这里用一个只监听 127.0.0.1 的最小 HTTP 服务，逐条数 TCP 连接：**连接数**是这条修复唯一
能被外部观察到的性质，光断言"没抛异常"或"客户端还没关"都盖不住它。用例不依赖任何计时，
连接数是确定值。

## 二、凭据为空时不能拼出 `"Bearer "`

同一台服务真正收到的首部同时也是 `build_anthropic_auth_headers` 的唯一外部可观察结果。
用真 socket 而不是 `MockTransport`：非法 field-value 是 **h11 在写线前**校验出来的，
Mock 传输根本不过 h11，把这一整类缺陷测没了（`Bearer ` 在 MockTransport 下畅通无阻）。
"""

import asyncio
import json
from typing import List, Tuple

import pytest

# 走模块属性调用：`test_anthropic_messages` 直接导入会被 pytest 当成一个用例去收集
from nekro_agent.services.agent import anthropic as anthropic_service
from nekro_agent.services.agent.openai import _get_http_client, reset_http_clients

RESPONSE_BODY = json.dumps(
    {
        "model": "claude-probe",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "data": [{"id": "claude-probe"}, {"id": "claude-probe-fast"}],
    },
).encode()


class LoopbackCounter:
    """记录 TCP 连接数与 HTTP 请求数的最小 HTTP/1.1 服务（保持 keep-alive）"""

    def __init__(self) -> None:
        self.connections = 0
        self.requests = 0
        # 逐请求记下服务端**实际收到**的首部（小写名 -> 值），鉴权首部的用例据此判定
        self.received_headers: List[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self.base_url = ""

    async def __aenter__(self) -> "LoopbackCounter":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await reset_http_clients()  # 关掉池中客户端，让服务端的连接处理协程退出
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                body_length = 0
                headers: dict = {}
                for line in head.split(b"\r\n")[1:]:
                    if b":" not in line:
                        continue
                    name, _, value = line.partition(b":")
                    headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
                    if name.lower() == b"content-length":
                        body_length = int(value)
                if body_length:
                    await reader.readexactly(body_length)
                self.requests += 1
                self.received_headers.append(headers)
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(RESPONSE_BODY)).encode() + b"\r\n"
                    b"Connection: keep-alive\r\n\r\n" + RESPONSE_BODY,
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.LimitOverrunError, OSError):
            pass
        finally:
            writer.close()


@pytest.fixture(autouse=True)
async def clean_http_pool():
    await reset_http_clients()
    yield
    await reset_http_clients()


async def test_repeated_calls_reuse_one_connection() -> None:
    """连续 5 次连通性测试只应建立 1 条 TCP 连接"""
    async with LoopbackCounter() as server:
        for _ in range(5):
            result = await anthropic_service.test_anthropic_messages(
                base_url=server.base_url,
                auth_token="probe-token",
                model="claude-probe",
                api_timeout_ms="30000",
            )
            assert result.response_text == "ok"

        assert server.requests == 5
        assert server.connections == 1


async def test_messages_and_models_share_the_same_connection() -> None:
    """同一个 base_url 下，发消息与拉模型列表共用同一条连接

    两条路径的超时参数一致，因此落在连接池的同一个键上；各自新建客户端会把连接数顶到 2。
    """
    async with LoopbackCounter() as server:
        await anthropic_service.test_anthropic_messages(
            base_url=server.base_url,
            auth_token="probe-token",
            model="claude-probe",
            api_timeout_ms="30000",
        )
        models = await anthropic_service.list_anthropic_models(base_url=server.base_url, auth_token="probe-token")

        assert models == ["claude-probe", "claude-probe-fast"]
        assert server.requests == 2
        assert server.connections == 1


async def test_pooled_client_survives_the_call() -> None:
    """客户端归连接池所有，调用结束后不得被关闭

    这条单独钉住：一旦有人把 `async with` 加回去，下一次调用拿到的是一个已关闭的客户端，
    连接池等于不存在。
    """
    async with LoopbackCounter() as server:
        await anthropic_service.test_anthropic_messages(
            base_url=server.base_url,
            auth_token="probe-token",
            model="claude-probe",
            api_timeout_ms="30000",
        )
        client = _get_http_client(
            base_url=server.base_url,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=10,
            pool_timeout=10,
        )
        assert not client.is_closed


class TestAuthHeadersOnTheWire:
    """`x-api-key` / `Authorization` 在线上的形状

    改动前两条都是无条件拼：`auth_token` 为空串时得到 `"Bearer "`，而 HTTP 的 field-value
    不允许首尾空白，h11 在写线之前抛 `LocalProtocolError: Illegal header value b'Bearer '`。
    调用方里 `/cc-model-presets/test` 与 `/test-inline` 会先挡空值，
    **`/cc-model-presets/fetch-models` 不会**——于是「预设里还没填 Key」（出厂预设正是空串）
    被翻译成 `OperationFailedError: 拉取 Anthropic 模型列表失败: Illegal header value b'Bearer '`，
    一句指着上游喊冤的报错。`routers/cc_relay.py` 是同一个缺陷的另一处，本轮一并修。

    判据钉在**服务端实际收到的那一份首部**上，而不是函数返回值：返回值对这条改动是不变的，
    只断言「调用没抛异常」等于把这一整类缺陷放过去。
    """

    @pytest.mark.parametrize(
        ("auth_token", "expected"),
        [
            ("sk-anthropic-real", "sk-anthropic-real"),
            # 从后台文本框粘贴时带上的空白：削掉之后仍是合法取值
            ("  sk-anthropic-real", "sk-anthropic-real"),
            ("sk-anthropic-real\n", "sk-anthropic-real"),
            # 空 / 纯空白：两条鉴权首部一条都不发（而不是发一个畸形的）
            ("", None),
            ("   ", None),
            ("\n", None),
            ("\t", None),
        ],
    )
    async def test_messages_call_sends_wellformed_or_no_credential(
        self,
        auth_token: str,
        expected: str | None,
    ) -> None:
        async with LoopbackCounter() as server:
            await anthropic_service.test_anthropic_messages(
                base_url=server.base_url,
                auth_token=auth_token,
                model="claude-probe",
                api_timeout_ms="30000",
            )
            assert len(server.received_headers) == 1
            headers = server.received_headers[0]
            assert headers.get("x-api-key") == expected
            assert headers.get("authorization") == (None if expected is None else f"Bearer {expected}")
            # anthropic-version 与凭据无关，必须照发——否则「删掉整个 headers」也能让上面全绿
            assert headers.get("anthropic-version") == "2023-06-01"

    @pytest.mark.parametrize(
        ("auth_token", "expected"),
        [
            ("sk-anthropic-real", "sk-anthropic-real"),
            ("sk-anthropic-real\n", "sk-anthropic-real"),
            ("", None),
            ("   ", None),
        ],
    )
    async def test_models_call_sends_wellformed_or_no_credential(
        self,
        auth_token: str,
        expected: str | None,
    ) -> None:
        """第二个调用点单独钉住：`fetch-models` 正是那条没有空值守卫的路径"""
        async with LoopbackCounter() as server:
            models = await anthropic_service.list_anthropic_models(
                base_url=server.base_url,
                auth_token=auth_token,
            )
            assert models == ["claude-probe", "claude-probe-fast"]
            assert len(server.received_headers) == 1
            headers = server.received_headers[0]
            assert headers.get("x-api-key") == expected
            assert headers.get("authorization") == (None if expected is None else f"Bearer {expected}")
            assert headers.get("anthropic-version") == "2023-06-01"


async def test_distinct_base_urls_do_not_share_connections() -> None:
    """不同上游各自一条连接，不会因为共享连接池而串到一起"""
    async with LoopbackCounter() as first, LoopbackCounter() as second:
        calls: List[Tuple[LoopbackCounter, int]] = [(first, 2), (second, 3)]
        for server, times in calls:
            for _ in range(times):
                await anthropic_service.test_anthropic_messages(
                    base_url=server.base_url,
                    auth_token="probe-token",
                    model="claude-probe",
                    api_timeout_ms="30000",
                )

        assert (first.requests, first.connections) == (2, 1)
        assert (second.requests, second.connections) == (3, 1)
