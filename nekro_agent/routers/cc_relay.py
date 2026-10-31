"""CC 工作区的模型 API 中继

工作区容器里的 `claude` CLI 把 `ANTHROPIC_BASE_URL` 指到这里，`ANTHROPIC_AUTH_TOKEN`
是一把只对**它自己那个工作区**有效的中继令牌。本模块校验令牌、换上平台方的真实凭据、
把请求原样转给真实上游，再把响应逐块流回去。真实 Key 因此一个字节都不进容器。

设计判据见 `services/workspace/cc_credential.py` 的模块 docstring。

## 按「敌对调用方」对待

调用方是模型生成的代码，与 `/ext/rpc_exec` 同一威胁类，所以：

1. **身份**只认中继令牌（`Authorization: Bearer` 或 `x-api-key`），不认用户会话；
   令牌唯一确定工作区，不接受调用方传入的工作区标识；
2. **路径白名单**（`ALLOWED_UPSTREAM_PATHS`）：只放行 `claude` CLI 真正会打的那两条。
   否则这把令牌就是一个通往上游中转站全部接口的开放代理；
3. **出口脱敏**：非 2xx 的响应体会被读出来过一遍凭据替换再回传——中转站的报错文案里
   回显 Authorization 头是真实发生过的形态（PROJECT_STATE §5「异常原文回显凭据」）。
   2xx 的流式响应不做替换：按块替换会在块边界上漏掉，而且会把首字延迟变成缓冲整包。

## 为什么响应必须流式

CC 的打字机效果、以及 NA 侧 `comm/stream` 的实时推送，全靠上游的 SSE 逐块到达。
这里用 `aiter_raw()` 原样转发未解码的字节，并把 `content-encoding` 一起带上，
所以压缩与分块语义都不变；`content-length` 必须丢掉（转发后是分块传输）。
"""

from typing import AsyncGenerator, Dict, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.errors import NotFoundError, UnauthorizedError
from nekro_agent.services.sandbox.credential_guard import redact_sandbox_text
from nekro_agent.services.workspace.cc_credential import (
    ALLOWED_UPSTREAM_PATHS,
    RELAY_ROUTE_PREFIX,
    RelayGrant,
    resolve_relay_grant,
)

logger = get_sub_logger("cc_relay")
router = APIRouter(prefix=RELAY_ROUTE_PREFIX, tags=["CC Relay"])

# 逐跳首部与由本层重新决定的首部，一律不透传
_DROPPED_REQUEST_HEADERS = frozenset(
    {
        "host",
        "authorization",
        "x-api-key",
        "proxy-authorization",
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "content-length",
    },
)
_DROPPED_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "content-length",
    },
)

# 非 2xx 响应体的读取上限：只是为了把上游报错原文带回去，不需要无限大
_MAX_ERROR_BODY_BYTES = 64 * 1024

# 进程生命期共用的上游客户端。每请求新建一个的代价是每次 LLM 调用都要重做 TCP + TLS 握手
# ——`services/openai_utils`/`anthropic.py` 已经因为同一个毛病改过一次（PROJECT_STATE §3 #9），
# 这里从一开始就用共享池。**不要套 `async with`**：那会连池一起关掉。
# 超时按预设逐请求给（`build_request(timeout=...)`），所以共享一个客户端不会把超时钉死。
_upstream_client: Optional[httpx.AsyncClient] = None


def _get_upstream_client() -> httpx.AsyncClient:
    global _upstream_client
    if _upstream_client is None or _upstream_client.is_closed:
        _upstream_client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0))
    return _upstream_client


def _extract_relay_token(request: Request) -> str:
    """从请求里取出中继令牌

    `ANTHROPIC_AUTH_TOKEN` 走 `Authorization: Bearer`，`ANTHROPIC_API_KEY` 走 `x-api-key`。
    两种都收，容器里换成哪种配置都不会静默失效。
    """
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _build_upstream_headers(request: Request, grant: RelayGrant) -> Dict[str, str]:
    """换上平台方凭据后的转发首部

    容器带来的 `authorization` / `x-api-key` 已经在 `_DROPPED_REQUEST_HEADERS` 里被丢掉，
    所以这里**不设**这个头就等于「一条凭据都不往上游带」，中继令牌不会因此外泄。

    凭据先 `strip()` 再拼，且**为空时整条头都不发**：
    HTTP 的 field-value 不允许首尾空白，`f"Bearer {''}"` 得到的 `"Bearer "` 会被 h11 在
    上线前直接拒掉，异常经 `relay_to_upstream` 的 `except` 翻译成
    `502 upstream unreachable: Illegal header value b'Bearer '` —— 一句把「预设里没填
    auth_token」说成「上游连不上」的报错。而出厂预设的 `auth_token` 正是空串，
    所以任何还没填 Key 就起工作区的部署都必然撞上它（测试部署实测复现）。
    同样的形态还包括从后台粘贴时带上的尾随换行。

    不发这个头之后，空凭据的结果与 `direct` 模式一致：由上游自己回它的 401 原文
    （非 2xx 会过一遍脱敏再回传），运维看到的是真正的原因；而 base_url 指向本就不要求
    鉴权的自建上游时，这条路径现在真的能用——此前它被那句 `Illegal header value` 堵死。
    """
    headers = {
        key: value for key, value in request.headers.items() if key.lower() not in _DROPPED_REQUEST_HEADERS
    }
    upstream_token = grant.upstream_token.strip()
    if upstream_token:
        headers["authorization"] = f"Bearer {upstream_token}"
    return headers


@router.post("/{upstream_path:path}", summary="CC 工作区的模型 API 中继")
async def relay_to_upstream(upstream_path: str, request: Request) -> Response:
    """把工作区容器发来的 Anthropic 兼容请求转给真实上游

    Raises:
        UnauthorizedError: 令牌缺失、格式不符、对不上工作区，或该工作区没有生效的模型预设
        NotFoundError: 请求路径不在白名单内
    """
    token = _extract_relay_token(request)
    if not token:
        raise UnauthorizedError

    grant = await resolve_relay_grant(token)
    if grant is None:
        logger.warning("CC 中继收到无法解析的令牌，已拒绝")
        raise UnauthorizedError

    normalized_path = upstream_path.strip("/")
    if normalized_path not in ALLOWED_UPSTREAM_PATHS:
        logger.warning(f"CC 中继拒绝未授权路径: {normalized_path!r} (workspace={grant.workspace_id})")
        raise NotFoundError(resource="上游接口")

    target = f"{grant.upstream_base_url}/{normalized_path}"
    client = _get_upstream_client()

    # `build_request()` 也必须在 try 之内：`base_url` 是后台表单里的自由文本，畸形取值会让
    # httpx 在**建请求时**就抛（实测 `https://x/claude\n` -> `InvalidURL`）。放在 try 外面的
    # 后果是这个「按敌对调用方对待」的端点直接吐 500 加一条堆栈，既绕开了下面那条脱敏，
    # 也把「预设填错了」变成一次服务端错误。
    try:
        upstream_request = client.build_request(
            "POST",
            target,
            headers=_build_upstream_headers(request, grant),
            content=request.stream(),
            params=dict(request.query_params),
            timeout=httpx.Timeout(connect=15.0, read=grant.timeout_seconds, write=60.0, pool=15.0),
        )
        response = await client.send(upstream_request, stream=True)
    except Exception as e:
        logger.warning(f"CC 中继转发失败: workspace={grant.workspace_id} error={e}")
        return Response(
            content=redact_sandbox_text(
                f'{{"type":"error","error":{{"type":"api_error","message":"upstream unreachable: {e}"}}}}',
                _relay_secret_values(grant),
            ),
            status_code=502,
            media_type="application/json",
        )

    passthrough_headers = {
        key: value for key, value in response.headers.items() if key.lower() not in _DROPPED_RESPONSE_HEADERS
    }

    if response.status_code >= 400:
        # 报错原文可能回显 Authorization 头或上游地址，读出来脱敏后再回传。
        # 逐块累加而不是 `aread()`：上限要真的是上限，`aread()` 会把整个响应体先收进内存，
        # 之后再切片就只是「看起来有界」——上游一个畸形的巨大报错体足以把 NA 撑爆。
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) >= _MAX_ERROR_BODY_BYTES:
                    break
        finally:
            await response.aclose()
        text = redact_sandbox_text(
            bytes(body[:_MAX_ERROR_BODY_BYTES]).decode("utf-8", "replace"),
            _relay_secret_values(grant),
        )
        # 内容已被改写，长度与编码信息一并作废
        passthrough_headers.pop("content-encoding", None)
        return Response(
            content=text,
            status_code=response.status_code,
            headers=passthrough_headers,
            media_type=response.headers.get("content-type", "application/json"),
        )

    async def _pump() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(
        _pump(),
        status_code=response.status_code,
        headers=passthrough_headers,
        media_type=response.headers.get("content-type"),
    )


def _relay_secret_values(grant: RelayGrant) -> Optional[frozenset]:
    """报错脱敏时要额外遮掉的字面量

    CC 模型预设存在 `configs/cc-model-presets.json` 这个独立文件里，不在 `ConfigManager`
    的配置树上，所以 `collect_platform_secrets()` 收不到它——必须显式带上。

    **原值与 `strip()` 后的取值都要收。** `redact_sandbox_text` 做的是精确字面量替换，
    而 `_build_upstream_headers` 上线的是 `strip()` 之后那个：预设里存着一把带尾随换行的 Key
    （从后台文本框粘贴时最常见的形态）时，上游报错原文里回显的是 `Bearer sk-xxx`，
    脱敏集合里却只有 `sk-xxx\\n`，`in` 判定不成立——真 Key 就这样原样穿过脱敏、落进容器。
    这条路是被「空白 token 也照常转发」打通的：在那之前带空白的取值会先被 h11 拒掉，
    根本到不了上游，所以两处必须一起改，只改发送侧等于把这个洞打开。
    """
    from nekro_agent.services.sandbox.credential_guard import collect_platform_secrets

    values = set(collect_platform_secrets())
    for token in (grant.upstream_token, grant.upstream_token.strip()):
        if token:
            values.add(token)
    return frozenset(values)
