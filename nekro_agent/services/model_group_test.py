"""模型组连通性测试的唯一实现（含凭据隔离与防滥用）

## 为什么这条链路要单独成模块

租户**可以**对平台方的模型组发起连通性测试——"通不通我得自己看得见"是产品需求；
但这条链路上任何一处把上游的原始文本、地址或响应体透传回去，都等于把平台凭据送出去。
本项目真实踩过的坑见 `plugins/builtin/draw/handlers.py`：key 拼在 query string 里，
httpx 的 `raise_for_status()` 异常文案带完整 URL，一次转发就整段泄露。

因此"发起一次测试"收敛成本模块的 `run_model_group_test()`，三个入口
（`/console/model-groups/{name}/test`、`/config/model-groups/actions/test`、
`/config/model-groups/actions/test-inline`）全部走它，不允许各自写一套脱敏。

## 泄露面与对应堵法

| 泄露面 | 堵法 |
| --- | --- |
| 响应体直接回显凭据 | 出参里根本没有凭据字段，见 `ModelGroupTestOutcome` |
| 异常文案带 URL/Header | 凭据不可见时 `message` 只取自 `NORMALIZED_MESSAGES`，与上游文本无关 |
| 上游 4xx 响应体回显 key | 同上；上游文本只在凭据可见时进 `detail`，且仍过 `redact_text` |
| 重定向 / `WWW-Authenticate` | 只归档异常类型与状态码，响应头一律不读出 |
| BASE_URL 本身敏感 | 凭据不可见时不回地址，出站复检也不做（理由见 `assert_test_target_allowed`） |
| 旁路推断 | 分类只按异常类型与 HTTP 状态码，同一类失败恒为同一句话 |

## 两种出参形态

`credential_visible` 就是 `model_group_service.can_see_credential()` 的结果，
不要在调用点另算一遍（归属判定分叉两处就是一次泄露）：

- **False（租户看平台组）**：只回归一化结论，`detail` / `response_text` 恒为空。
- **True（凭据本来就是调用方自己的）**：额外回上游文本，方便定位问题。

## 防滥用

测试会真实打上游、消耗平台方额度，因此租户侧限流。项目里没有现成的限流设施
（`services/quota_service.py` 管的是频道回复配额，语义不同），这里就地实现进程内滑动窗口。
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
from collections import deque
from enum import Enum
from typing import Any, ClassVar, Deque, Dict, Iterator, Optional, Tuple

import httpx
from pydantic import BaseModel, Field

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.schemas.errors import AppError
from nekro_agent.schemas.i18n import I18nDict, i18n_text
from nekro_agent.schemas.tenant import CurrentIdentity
from nekro_agent.services.model_group_service import assert_outbound_urls_allowed
from nekro_agent.services.model_test import (
    build_model_test_messages,
    build_openai_embedding_test_params,
    build_openai_model_test_params,
)
from nekro_agent.tools.redaction import redact_text

logger = get_sub_logger("model_group_test")

# 聊天测试的最长等待时间（秒）。与既有实现保持一致，避免换实现后表现漂移
CHAT_TEST_MAX_WAIT_SECONDS = 45

# 每个租户在窗口内可发起的测试次数。测试花的是平台方的钱，必须有上限
MODEL_TEST_RATE_WINDOW_SECONDS = 60.0
MODEL_TEST_RATE_MAX_CALLS = 10

# 异常链的回溯深度上限。openai SDK 会把 httpx 异常包在 __cause__ 里，socket/ssl 再包一层
_EXCEPTION_CHAIN_MAX_DEPTH = 8


class ModelTestErrorCode(str, Enum):
    """连通性测试的归一化结论

    这是对外唯一的失败分类维度：前端据此做 i18n 与图标，租户据此判断"是我配错了还是上游挂了"。
    刻意做成粗粒度——每多一个可被租户观测的分支，就多一条反推平台配置的旁路。
    """

    NONE = "none"  # 成功
    UNAUTHORIZED = "unauthorized"  # 401
    FORBIDDEN = "forbidden"  # 403
    MODEL_NOT_FOUND = "model_not_found"  # 404
    RATE_LIMITED = "rate_limited"  # 429（上游限流，或本地防滥用限流）
    UPSTREAM_REJECTED = "upstream_rejected"  # 其余 4xx
    UPSTREAM_ERROR = "upstream_error"  # 5xx
    TIMEOUT = "timeout"
    DNS_FAILURE = "dns_failure"
    TLS_ERROR = "tls_error"
    PROXY_ERROR = "proxy_error"
    CONNECTION_FAILED = "connection_failed"
    INVALID_RESPONSE = "invalid_response"  # 连上了但返回内容不可用
    NOT_CONFIGURED = "not_configured"  # 地址或凭据为空，压根没发请求
    UNSUPPORTED_MODEL_TYPE = "unsupported_model_type"
    GROUP_NOT_FOUND = "group_not_found"  # 模型组不存在或当前身份不可见
    INTERNAL_ERROR = "internal_error"


# 对外文案的**有限集合**。响应里的 message 只能取自这里，不允许是上游原文的透传或截断。
NORMALIZED_MESSAGES: Dict[ModelTestErrorCode, str] = {
    ModelTestErrorCode.NONE: "连接正常",
    ModelTestErrorCode.UNAUTHORIZED: "认证失败：上游拒绝了该模型组的凭据",
    ModelTestErrorCode.FORBIDDEN: "无访问权限：上游拒绝了本次调用",
    ModelTestErrorCode.MODEL_NOT_FOUND: "上游未找到该模型或接口路径",
    ModelTestErrorCode.RATE_LIMITED: "请求过于频繁，请稍后再试",
    ModelTestErrorCode.UPSTREAM_REJECTED: "上游拒绝了请求：模型名或参数不被接受",
    ModelTestErrorCode.UPSTREAM_ERROR: "上游服务异常（5xx）",
    ModelTestErrorCode.TIMEOUT: "连接超时",
    ModelTestErrorCode.DNS_FAILURE: "域名解析失败",
    ModelTestErrorCode.TLS_ERROR: "TLS 握手失败",
    ModelTestErrorCode.PROXY_ERROR: "代理不可用",
    ModelTestErrorCode.CONNECTION_FAILED: "无法建立连接",
    ModelTestErrorCode.INVALID_RESPONSE: "上游返回内容无效",
    ModelTestErrorCode.NOT_CONFIGURED: "该模型组尚未配置完整的接口地址与凭据",
    ModelTestErrorCode.UNSUPPORTED_MODEL_TYPE: "仅支持测试聊天模型组和嵌入模型组",
    ModelTestErrorCode.GROUP_NOT_FOUND: "模型组不存在或当前身份不可见",
    ModelTestErrorCode.INTERNAL_ERROR: "测试失败：服务端内部错误",
}


def normalized_message(code: ModelTestErrorCode) -> str:
    """取归一化文案；未登记的取值退回内部错误，绝不回落成上游原文"""
    return NORMALIZED_MESSAGES.get(code, NORMALIZED_MESSAGES[ModelTestErrorCode.INTERNAL_ERROR])


class ModelGroupTestOutcome(BaseModel):
    """一次连通性测试的归一化结论

    出参里没有任何凭据字段，也没有地址字段。`message` 恒取自 `NORMALIZED_MESSAGES`；
    `detail` / `response_text` 只在调用方本就能看到该组凭据时才有值。
    """

    ok: bool = Field(default=False, description="是否连通")
    error_code: ModelTestErrorCode = Field(default=ModelTestErrorCode.NONE, description="归一化结论")
    status_code: Optional[int] = Field(default=None, description="上游 HTTP 状态码；非 HTTP 失败为 None")
    latency_ms: int = Field(default=0, description="耗时")
    message: str = Field(default="", description="有限集合内的人类可读文案")
    detail: str = Field(default="", description="上游原文（已脱敏）；凭据不可见时恒为空")
    model_name: str = Field(default="", description="被测模型名")
    response_text: str = Field(default="", description="上游回复；凭据不可见时恒为空")
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)


class ModelTestRateLimitedError(AppError):
    """连通性测试触发防滥用限流

    按约定业务错误应集中在 `schemas/errors.py`，这里就地定义是为了不与并行开发的其他模块
    争抢同一个共享文件（`services/user/tenant_perm.TenantSuspendedError` 同理）；
    全局异常处理器按 `AppError` 捕获，落在哪个模块不影响响应结构。
    """

    http_status: ClassVar[int] = 429
    i18n_message: ClassVar[I18nDict] = i18n_text(
        zh_CN="连通性测试过于频繁，请 {retry_after} 秒后再试",
        en_US="Too many connectivity tests, please retry in {retry_after} seconds",
    )

    def __init__(self, retry_after: int, **kwargs: Any):
        super().__init__(retry_after=retry_after, **kwargs)


class ModelTestRateLimiter:
    """按键分桶的滑动窗口限流器

    进程内实现，重启即清零；多副本部署下每个副本各自限流。对"别让租户拿平台额度跑量"
    这个目标够用，不引入 Redis 依赖。
    """

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._hits: Dict[str, Deque[float]] = {}

    def try_acquire(self, key: str, now: Optional[float] = None) -> Optional[float]:
        """尝试占用一次配额

        Args:
            key: 限流键
            now: 单调时钟读数；仅测试注入，生产恒为 None

        Returns:
            None 表示放行；否则为还需等待的秒数（可能为 0，表示下一刻即可重试）。
        """
        moment = time.monotonic() if now is None else now
        hits = self._hits.setdefault(key, deque())
        while hits and moment - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._max_calls:
            return max(0.0, self._window - (moment - hits[0]))
        hits.append(moment)
        return None

    def reset(self) -> None:
        """清空全部计数（测试夹具用）"""
        self._hits.clear()


model_test_rate_limiter = ModelTestRateLimiter(MODEL_TEST_RATE_MAX_CALLS, MODEL_TEST_RATE_WINDOW_SECONDS)


def assert_model_test_quota(identity: CurrentIdentity) -> None:
    """租户侧的测试频率限制

    平台方不限流：花的是自己的额度，且平台方本就能直接操作宿主。
    同一租户下的全部成员共用一个桶——限的是"这个客户"，不是"这个账号"，
    否则多建几个成员账号就能绕过。

    Raises:
        ModelTestRateLimitedError: 超出窗口内的次数上限
    """
    if identity.tenant_id is None:
        return
    retry_after = model_test_rate_limiter.try_acquire(f"tenant:{identity.tenant_id}")
    if retry_after is None:
        return
    logger.warning(f"租户 {identity.tenant_id} 的模型组连通性测试触发限流")
    raise ModelTestRateLimitedError(retry_after=int(retry_after) + 1)


async def assert_test_target_allowed(
    identity: CurrentIdentity,
    group: ModelConfigGroup,
    *,
    credential_visible: bool,
) -> None:
    """测试前的出站地址复检

    只对**调用方自己填的**地址复检。租户自建组的 `base_url` 等于"让 NA 服务器替我打这个地址"，
    存量行可能写在出站校验落地之前，所以每次测试都重新校验一次。

    平台组对租户**不复检**，这不是漏掉了，而是必须如此：
    - 该地址由平台方设定，租户无法控制目标，测试不给租户任何新的出站能力；
    - `UnsafeDownloadUrlError` 的文案里带主机名与解析出的 IP，对租户抛出去
      等于把平台方的内网拓扑（以及被当作敏感信息的 BASE_URL）直接念给他听；
    - 平台方用 `http://127.0.0.1:11434/v1` 接本地推理是主流用法，本就豁免该校验。
    """
    if not credential_visible:
        return
    await assert_outbound_urls_allowed(identity, group.BASE_URL, group.CHAT_PROXY)


async def prepare_model_group_test(
    identity: CurrentIdentity,
    group: ModelConfigGroup,
    *,
    credential_visible: bool,
) -> None:
    """测试前的统一准入：防滥用限流 + 出站地址复检

    三个入口都必须先过这里再调 `run_model_group_test()`。

    Raises:
        ModelTestRateLimitedError: 触发限流
        ValidationError: 调用方自己填的出站地址不被允许
    """
    assert_model_test_quota(identity)
    await assert_test_target_allowed(identity, group, credential_visible=credential_visible)


def _iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """回溯异常链（`__cause__` 优先，其次 `__context__`）

    openai SDK 把 httpx 的异常包成 `APIConnectionError`，httpx 又把 `socket.gaierror` /
    `ssl.SSLError` 包在里面，只看最外层会把"域名解析失败"误判成"无法建立连接"。
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    depth = 0
    while current is not None and depth < _EXCEPTION_CHAIN_MAX_DEPTH and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__
        depth += 1


def _extract_status_code(exc: BaseException) -> Optional[int]:
    """取异常携带的 HTTP 状态码

    `openai.APIStatusError` 挂在 `status_code` 上，`httpx.HTTPStatusError` 挂在
    `response.status_code` 上。这里按属性取值而不是 import openai 的异常类型，
    避免为了一次分类把整个 SDK 拉进导入链。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _classify_status(status: int) -> ModelTestErrorCode:
    """把 HTTP 状态码归档到有限的结论集合"""
    if status == 401:
        return ModelTestErrorCode.UNAUTHORIZED
    if status == 403:
        return ModelTestErrorCode.FORBIDDEN
    if status == 404:
        return ModelTestErrorCode.MODEL_NOT_FOUND
    if status == 408:
        return ModelTestErrorCode.TIMEOUT
    if status == 429:
        return ModelTestErrorCode.RATE_LIMITED
    if 400 <= status < 500:
        return ModelTestErrorCode.UPSTREAM_REJECTED
    if status >= 500:
        return ModelTestErrorCode.UPSTREAM_ERROR
    return ModelTestErrorCode.INVALID_RESPONSE


def classify_upstream_error(exc: BaseException) -> Tuple[ModelTestErrorCode, Optional[int]]:
    """把上游异常归档成 (结论, 状态码)

    只看**异常类型与状态码**，不看文案内容：文案是上游可控的，按内容分叉既不可靠，
    也会让租户能用报错差异反推平台方的配置。
    """
    chain = list(_iter_exception_chain(exc))

    status = next((code for code in (_extract_status_code(item) for item in chain) if code is not None), None)
    if status is not None:
        return _classify_status(status), status

    # gaierror / SSLError 都是 OSError 的子类，必须排在通用连接失败之前
    if any(isinstance(item, socket.gaierror) for item in chain):
        return ModelTestErrorCode.DNS_FAILURE, None
    if any(isinstance(item, ssl.SSLError) for item in chain):
        return ModelTestErrorCode.TLS_ERROR, None
    if any(isinstance(item, (httpx.TimeoutException, asyncio.TimeoutError)) for item in chain):
        return ModelTestErrorCode.TIMEOUT, None
    if any(isinstance(item, httpx.ProxyError) for item in chain):
        return ModelTestErrorCode.PROXY_ERROR, None
    if any(isinstance(item, (httpx.TransportError, ConnectionError, OSError)) for item in chain):
        return ModelTestErrorCode.CONNECTION_FAILED, None
    if any(isinstance(item, (ValueError, KeyError, IndexError, TypeError)) for item in chain):
        return ModelTestErrorCode.INVALID_RESPONSE, None
    return ModelTestErrorCode.INTERNAL_ERROR, None


def build_failure_outcome(
    code: ModelTestErrorCode,
    *,
    model_name: str = "",
    status_code: Optional[int] = None,
    latency_ms: int = 0,
) -> ModelGroupTestOutcome:
    """构造一个不含任何上游文本的失败结论

    供"还没发出请求"的失败复用（模型组不可见、类型不支持、触发限流等）。
    """
    return ModelGroupTestOutcome(
        ok=False,
        error_code=code,
        status_code=status_code,
        latency_ms=latency_ms,
        message=normalized_message(code),
        model_name=model_name,
    )


async def run_model_group_test(
    group: ModelConfigGroup,
    *,
    credential_visible: bool,
    group_name: str = "",
    tenant_id: Optional[int] = None,
) -> ModelGroupTestOutcome:
    """发起一次连通性测试并返回归一化结论

    本函数**不抛**上游异常：任何失败都被归档成 `ModelGroupTestOutcome`，
    这样调用方不可能"不小心"把异常原文透传出去。

    Args:
        group: 被测模型组的完整配置（含真实凭据，只在服务端使用）
        credential_visible: 调用方本就能看到该组凭据吗（`can_see_credential()` 的结果）。
            False 时出参里不会出现任何上游文本。
        group_name: 模型组的**身份键**（`DBModelGroup.name` / `config.MODEL_GROUPS` 的键）。
            账上的 `model_group_name` 只认这一个命名空间。留空时退回 `GROUP_NAME`，
            但那是**显示名**（`DBModelGroup.group_name`）：可以重复、可以为空、与身份键
            通常不相等。用显示名落账有两个后果——同一个组在用量页上裂成两行，
            而且 `visible_model_group_names` 按 `DBModelGroup.name` 反查不到它，
            于是租户自己的组在自己的账单里被掩成「(未公开的模型组)」。
        tenant_id: 点这次测试的人所属的租户（平台方为 `None`）。这一跳没有会话也没有实例，
            不声明的话账落成 `tenant_id IS NULL`，租户在自己的用量页上**看不到自己刚烧的钱**。
    """
    from nekro_agent.services.agent.openai import (
        gen_openai_chat_response,
        gen_openai_embeddings,
    )
    from nekro_agent.services.usage import LLMUsageContext, LLMUsageScene, usage_scope

    if group.MODEL_TYPE not in ("chat", "embedding"):
        return build_failure_outcome(ModelTestErrorCode.UNSUPPORTED_MODEL_TYPE, model_name=group.CHAT_MODEL)
    if not group.BASE_URL.strip() or not group.API_KEY.strip():
        return build_failure_outcome(ModelTestErrorCode.NOT_CONFIGURED, model_name=group.CHAT_MODEL)

    started = time.perf_counter()
    try:
        if group.MODEL_TYPE == "embedding":
            # 与下面 chat 那一支同一条理由：连通性测试真的打了上游、真的计费，
            # 而这是租户唯一能自己点出来的非对话花销。**两支都要声明** ——
            # 只给 chat 那一支的话，嵌入组的测试会整条落进 `unattributed`，
            # 租户在自己的用量页上看不到自己刚烧的钱（chat 那一支修过同样的问题）
            with usage_scope(
                LLMUsageContext(scene=LLMUsageScene.EMBEDDING, tenant_id=tenant_id).with_model_group(
                    group_name or group.GROUP_NAME,
                    group,
                ),
            ):
                embedding = await gen_openai_embeddings(**build_openai_embedding_test_params(group))
            if not embedding:
                raise ValueError("嵌入接口未返回向量")
            return ModelGroupTestOutcome(
                ok=True,
                error_code=ModelTestErrorCode.NONE,
                latency_ms=int((time.perf_counter() - started) * 1000),
                message=normalized_message(ModelTestErrorCode.NONE),
                model_name=group.CHAT_MODEL,
                response_text=f"向量维度: {len(embedding)}" if credential_visible else "",
            )

        # 连通性测试也真的打了上游、也真的计费。归属到「哪个租户点的 + 哪个组」这一层
        # （没有会话也没有实例）——租户反复点"测试"就是一笔实实在在的支出，而且要出现在
        # **他自己**的账单上，见上面 `group_name` / `tenant_id` 两个参数的说明。
        with usage_scope(
            LLMUsageContext(scene=LLMUsageScene.MODEL_TEST, tenant_id=tenant_id).with_model_group(
                group_name or group.GROUP_NAME,
                group,
            ),
        ):
            result = await gen_openai_chat_response(
                messages=build_model_test_messages(use_system=False),
                **build_openai_model_test_params(group, False),
                max_wait_time=CHAT_TEST_MAX_WAIT_SECONDS,
            )
        if not result.response_content:
            raise ValueError("模型未返回有效内容")
        return ModelGroupTestOutcome(
            ok=True,
            error_code=ModelTestErrorCode.NONE,
            latency_ms=int((time.perf_counter() - started) * 1000),
            message=normalized_message(ModelTestErrorCode.NONE),
            model_name=group.CHAT_MODEL,
            response_text=result.response_content if credential_visible else "",
            input_tokens=result.token_input if credential_visible else 0,
            output_tokens=result.token_output if credential_visible else 0,
        )
    except Exception as e:  # noqa: BLE001 — 归档而非抛出，见 docstring
        code, status = classify_upstream_error(e)
        # 上游报错常把 `Authorization: Bearer sk-xxx` 或整段请求 URL 回显出来，
        # 因此进日志之前就得脱敏——日志同样是凭据的出口之一。
        detail = redact_text(str(e), literal_secrets=(group.API_KEY, group.CHAT_PROXY))
        logger.warning(f"模型组连通性测试失败: model={group.CHAT_MODEL} code={code.value} status={status} -> {detail}")
        return ModelGroupTestOutcome(
            ok=False,
            error_code=code,
            status_code=status,
            latency_ms=int((time.perf_counter() - started) * 1000),
            message=normalized_message(code),
            detail=detail if credential_visible else "",
            model_name=group.CHAT_MODEL,
        )
