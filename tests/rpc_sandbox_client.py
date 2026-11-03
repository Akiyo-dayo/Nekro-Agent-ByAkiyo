"""测试里的“沙盒”：用 `ext_caller_code.py` 里真正下发进容器的那段编码器拼请求体，经真实的 `rpc_exec` 入口发出

请求体不在测试里手写：用的是模板里 `RPC CODEC` 标记之间的源码（与 `get_api_caller_code` 一样替换上限占位符），
所以“沙盒怎么编、宿主怎么解”两边是对着测的。
"""

import base64
import datetime
import io
import json
import os
import pickle
import threading
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from nekro_agent.tools.rpc_codec import MAX_BODY_BYTES, sandbox_limits

STUB = Path(__file__).resolve().parent.parent / "nekro_agent" / "services" / "sandbox" / "ext_caller_code.py"


def load_stub_block(begin: str, end: str, limits: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """执行模板里两个标记之间的源码片段，返回它的命名空间（片段用到的模块在模板顶部导入，这里按同样的别名给它）

    `limits` 覆盖下发的上限：放宽之后编出“刚好超限”的请求体，用来证明宿主在同一个边界上拒绝
    """
    text = STUB.read_text(encoding="utf-8")
    source = text[text.index(begin) : text.index(end)].replace("{RPC_LIMITS_JSON}", json.dumps(limits or sandbox_limits()))
    namespace: Dict[str, Any] = {
        "_base64": base64,
        "_datetime": datetime,
        "_io": io,
        "_json": json,
        "_os": os,
        "_pickle": pickle,
        "_threading": threading,
        "_uuid": uuid,
    }
    exec(compile(source, str(STUB), "exec"), namespace)  # noqa: S102 - 执行的是仓库里我们自己的源码片段
    return namespace


SANDBOX_CODEC = load_stub_block("# --- BEGIN RPC CODEC", "# --- END RPC CODEC")


def load_stub_module(post: Any, method_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行整份 `api_caller.py` 模板（占位符按宿主的方式替换），`requests.post` 换成 `post`

    沙盒镜像里的 `matplotlib` / `packaging` 在宿主的测试环境里不一定有，用空模块顶上；只在执行期间放进 `sys.modules`
    """
    import sys
    import types

    text = (
        STUB.read_text(encoding="utf-8")
        .replace("{CHAT_API}", "http://na.invalid")
        .replace("{CONTAINER_KEY}", "stub-container")
        .replace("{FROM_CHAT_KEY}", "stub-chat")
        .replace("{RPC_SECRET_KEY}", "stub-token")
        .replace("{RPC_READ_TIMEOUT_SECONDS}", "10")
        .replace("{RPC_LIMITS_JSON}", json.dumps(sandbox_limits()))
    )
    pyplot = types.ModuleType("matplotlib.pyplot")
    pyplot.rcParams = {}  # type: ignore[attr-defined]
    matplotlib = types.ModuleType("matplotlib")
    matplotlib.pyplot = pyplot  # type: ignore[attr-defined]

    class _ConnectionError(Exception):
        pass

    class _Timeout(Exception):
        pass

    requests = types.ModuleType("requests")
    requests.post = post  # type: ignore[attr-defined]
    requests.exceptions = types.SimpleNamespace(ConnectionError=_ConnectionError, Timeout=_Timeout)  # type: ignore[attr-defined]
    fakes = {"matplotlib": matplotlib, "matplotlib.pyplot": pyplot, "requests": requests}
    saved = {name: sys.modules.get(name) for name in fakes}
    sys.modules.update(fakes)
    try:
        namespace: Dict[str, Any] = {"__name__": "api_caller"}
        exec(compile(text, "api_caller.py", "exec"), namespace)  # noqa: S102 - 执行的是仓库里我们自己的模板
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    namespace["_RPC_METHOD_PARAMS"].update(method_params or {})
    return namespace


def request_body(method: str, *args: Any, **kwargs: Any) -> bytes:
    """沙盒里调用 `method(*args, **kwargs)` 时发给宿主的请求体"""
    return SANDBOX_CODEC["_rpc_encode_request"](method, args, kwargs)


class FakeRpcRequest:
    """`starlette.requests.Request` 在 `rpc_exec` 里用到的部分：`headers` 与 `stream()`

    记下实际被读走多少字节，用来证明超限的请求体没有被整个读进内存
    """

    def __init__(
        self,
        payload: bytes,
        *,
        chunk_size: int = 64 * 1024,
        content_length: Optional[str] = "auto",
        endless: bool = False,
    ) -> None:
        self.payload = payload
        self.chunk_size = chunk_size
        self.endless = endless
        self.consumed = 0
        self.headers: Dict[str, str] = {}
        if content_length == "auto":
            self.headers["content-length"] = str(len(payload))
        elif content_length is not None:
            self.headers["content-length"] = content_length

    async def stream(self) -> AsyncIterator[bytes]:
        # “无尽”的流也有个兜底（上限的 16 倍）：被测代码坏掉、不截断时，用例以“读多了”失败，而不是把内存吃光
        while self.consumed < 16 * MAX_BODY_BYTES:
            for start in range(0, len(self.payload), self.chunk_size):
                chunk = self.payload[start : start + self.chunk_size]
                self.consumed += len(chunk)
                yield chunk
            if not self.endless:
                return

    async def body(self) -> bytes:
        raise AssertionError("rpc_exec 必须经 stream() 限长读取，不能整体 body()")
