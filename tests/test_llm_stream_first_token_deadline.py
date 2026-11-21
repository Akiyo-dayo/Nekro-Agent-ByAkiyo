"""流式首 Token 超时从发出请求算起，而不是从收到响应头算起

`AI_STREAM_FIRST_TOKEN_TIMEOUT` 的说明是：指定时间内没收到首个流式数据块，就判本次请求失败、
进入后续重试，用来缩短上游没反应时的干等。原实现只把「等第一个数据块」包进了这个时限，
而在那之前的 `client.chat.completions.create(stream=True)` 要等上游回了**响应头**才返回，
这一段只受 `AI_GENERATE_TIMEOUT`（httpx 读超时，默认 180 秒）约束。上游排队、或中转站要等
它的上游回头之后才回头时，首 Token 超时整个不起作用，一次尝试照样等满读超时再重试。

这里用一个只监听 127.0.0.1 的最小 HTTP 服务：`create(stream=True)` 在响应头到达之前不返回，
是 openai SDK 与 httpx 的行为，替换掉 SDK 客户端的替身测不到这一段。

四条用例：
- 响应头一直不来：按首 Token 时限失败（修复前要等到读超时，抛的是另一类异常）；
- 响应头来了、数据块一直不来：照旧按首 Token 时限失败；
- 响应头来得晚、首块更晚：两段**共用同一个截止时刻**，不是各给一整份时限；
- 首块已经到了之后的间隔：不受首 Token 时限约束，只受读超时约束（正常的慢速流不能被误杀）。

## 判据只看结果，不看耗时

这几条要在机器很忙时（同时跑着几套 pytest）也稳，所以不断言「过了几秒」：

- 修复前，响应头不来时失败的是读超时，抛 `openai.APITimeoutError`；修复后抛带「首个数据块」的
  `TimeoutError`。看异常就分得出来。
- 「共用一个截止时刻」按上游的出牌顺序判：时限 2 秒，响应头在 1.2 秒发、首块在 2.4 秒才发。共用截止
  时刻必然失败（首块晚于截止）；两段各给一整份时限、或修复前只给首块计时，截止落在 3.2 秒以后，
  能等到首块、拿到完整回复。
- 假上游与被测代码跑在**同一个事件循环**里：上游的「几秒后发」和被测代码的截止时刻都是这个循环里
  的定时器，按到点先后触发。机器忙只会让两边一起变慢，不会让首块赶在截止之前到，所以修复后的判定
  与负载无关。
"""

import asyncio
import json
from typing import Optional

import pytest

from nekro_agent.services.agent import openai as openai_module
from nekro_agent.services.agent.openai import gen_openai_chat_response, reset_http_clients

FIRST_TOKEN_TIMEOUT = 1
# 读超时远大于首 Token 时限：首块之后的停顿只受它约束，留足机器忙时的余量
READ_TIMEOUT = 30
# 单条用例的外层兜底，防止实现退化成「永远等下去」时把整套测试挂住；比读超时长，
# 修复前那条才会以读超时（而不是这道兜底）失败
CASE_GUARD = 90

SSE_HEAD = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/event-stream\r\n"
    b"Cache-Control: no-cache\r\n"
    b"Transfer-Encoding: chunked\r\n\r\n"
)


def _http_chunk(payload: bytes) -> bytes:
    return f"{len(payload):x}\r\n".encode() + payload + b"\r\n"


def _sse_event(content: Optional[str], *, finish_reason: Optional[str] = None) -> bytes:
    delta = {} if content is None else {"content": content}
    body = {
        "id": "chatcmpl-probe",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "probe-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return _http_chunk(b"data: " + json.dumps(body).encode() + b"\n\n")


SSE_TAIL = _sse_event(None, finish_reason="stop") + _http_chunk(b"data: [DONE]\n\n") + b"0\r\n\r\n"


class LoopbackUpstream:
    """按剧本回应的最小上游；剧本是一个拿到 writer 的协程"""

    def __init__(self, script) -> None:
        self._script = script
        self._server: Optional[asyncio.AbstractServer] = None
        self._release = asyncio.Event()
        self.base_url = ""

    async def __aenter__(self) -> "LoopbackUpstream":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}/v1"
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._release.set()
        await reset_http_clients()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def hold(self) -> None:
        """一直不发东西，直到用例收尾"""
        await self._release.wait()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            body_length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    body_length = int(value)
            if body_length:
                await reader.readexactly(body_length)
            await self._script(self, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.LimitOverrunError, OSError):
            pass
        finally:
            writer.close()


@pytest.fixture(autouse=True)
async def _isolate(monkeypatch):
    # 用量记账换成空操作：真记账会起后台写库任务，这几条用例既不建库也不等它
    monkeypatch.setattr(openai_module, "record_llm_usage", lambda *_args, **_kwargs: None)
    await reset_http_clients()
    yield
    await reset_http_clients()


async def _call(upstream: LoopbackUpstream, first_token_timeout: int = FIRST_TOKEN_TIMEOUT):
    return await asyncio.wait_for(
        gen_openai_chat_response(
            model="probe-model",
            messages=[{"role": "user", "content": "hi"}],
            api_key="sk-probe",
            base_url=upstream.base_url,
            stream_mode=True,
            max_wait_time=READ_TIMEOUT,
            first_token_timeout=first_token_timeout,
        ),
        timeout=CASE_GUARD,
    )


async def test_headers_never_arrive_fails_at_first_token_timeout() -> None:
    """上游收了请求但一直不回响应头：按首 Token 时限失败，而不是等到读超时

    修复前这里抛的是读超时的 `openai.APITimeoutError`，不是下面这个 `TimeoutError`。
    """

    async def script(server: LoopbackUpstream, _writer: asyncio.StreamWriter) -> None:
        await server.hold()

    async with LoopbackUpstream(script) as upstream:
        with pytest.raises(TimeoutError, match="首个数据块"):
            await _call(upstream)


async def test_headers_arrive_but_no_chunk_fails_at_first_token_timeout() -> None:
    """响应头立刻回、数据块一直不来：原本就生效的这一段不能被改坏"""

    async def script(server: LoopbackUpstream, writer: asyncio.StreamWriter) -> None:
        writer.write(SSE_HEAD)
        await writer.drain()
        await server.hold()

    async with LoopbackUpstream(script) as upstream:
        with pytest.raises(TimeoutError, match="首个数据块"):
            await _call(upstream)


async def test_late_headers_share_one_deadline_with_the_first_chunk() -> None:
    """响应头来得晚、首块更晚：两段共用一个截止时刻

    时限 2 秒，响应头 1.2 秒发、首块 2.4 秒发。共用截止时刻时首块晚于截止，必然失败；
    若两段各给一整份时限（或只给首块计时），截止在 3.2 秒以后，会等到首块、正常返回。
    """
    timeout = 2
    headers_at = 1.2
    first_chunk_at = 2.4

    async def script(_server: LoopbackUpstream, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(headers_at)
        writer.write(SSE_HEAD)
        await writer.drain()
        await asyncio.sleep(first_chunk_at - headers_at)
        writer.write(_sse_event("late") + SSE_TAIL)
        await writer.drain()

    async with LoopbackUpstream(script) as upstream:
        with pytest.raises(TimeoutError, match="首个数据块"):
            await _call(upstream, first_token_timeout=timeout)


async def test_gap_after_first_chunk_is_not_cut_by_first_token_timeout() -> None:
    """首块之后的间隔只受读超时约束：首块后停顿超过首 Token 时限，回复照样完整"""

    async def script(_server: LoopbackUpstream, writer: asyncio.StreamWriter) -> None:
        writer.write(SSE_HEAD + _sse_event("hel"))
        await writer.drain()
        await asyncio.sleep(FIRST_TOKEN_TIMEOUT + 0.8)
        writer.write(_sse_event("lo") + SSE_TAIL)
        await writer.drain()

    async with LoopbackUpstream(script) as upstream:
        response = await _call(upstream)

    assert response.response_content == "hello"
