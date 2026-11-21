"""模型组连通性测试的凭据隔离回归测试

保护的契约：**租户可以对平台方的模型组发起连通性测试，但整条链路不得泄露平台方的
API_KEY 与 BASE_URL**。请求由服务端发起，租户只拿到一个归一化结论。

这里逐条钉死已知的泄露面（`services/model_group_test.py` 的模块文档列了同一张表）：

1. 响应体直接回显凭据；
2. 异常文案带完整 URL（本项目在 `plugins/builtin/draw/handlers.py` 真实踩过）；
3. 上游 4xx 响应体把收到的 key 回显出来；
4. 重定向 `Location` / `WWW-Authenticate` 响应头带凭据；
5. `BASE_URL` 本身也是敏感信息——包括出站校验失败时报错里的主机名与解析出的 IP；
6. 旁路推断：同一类失败必须给出完全一致的结论，租户无法用报错差异反推 key。

特别地，`test_inline_refuses_credential_fallback_for_platform_group` 锁的是一条**完整的
外带通道**：租户提交「平台组名 + 掩码 api_key + 自己的 base_url」，服务端若按"保持不变"
哨兵回落成平台方的真实 Key，就会把它填进 Authorization 头发到租户指定的地址；掩码、脱敏、
出站公网校验没有任何一条拦得住，因为那次请求本身完全"合法"。

上游 HTTP 一律用本地桩替换，用例不打外网。
"""

import socket
import ssl
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx
import openai
import pytest
from pytest import MonkeyPatch

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.models.db_model_group import DBModelGroup, ModelGroupVisibility
from nekro_agent.routers import config as config_router
from nekro_agent.schemas.errors import (
    ModelGroupNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from nekro_agent.schemas.tenant import (
    CREDENTIAL_MASK,
    CurrentIdentity,
    ModelGroupOwnership,
    TenantRole,
)
from nekro_agent.services import model_group_test as model_group_test_module
from nekro_agent.services.model_group_test import (
    NORMALIZED_MESSAGES,
    ModelGroupTestOutcome,
    ModelTestErrorCode,
    ModelTestRateLimitedError,
    ModelTestRateLimiter,
    assert_model_test_quota,
    assert_test_target_allowed,
    classify_upstream_error,
    run_model_group_test,
)
from nekro_agent.services.user.role import Role

PLATFORM_API_KEY = "sk-platform-tenant-must-never-see-this-0123456789"
PLATFORM_BASE_URL = "https://api.platform-internal.example/v1"
PLATFORM_PROXY = "http://proxy-user:proxy-pass-must-stay-secret@proxy.internal.example:8080"

TENANT_API_KEY = "sk-tenant-owns-this-key-9876543210"
TENANT_BASE_URL = "https://api.tenant-own.example/v1"

# 平台方的凭据类秘密。BASE_URL 单独断言：凭据可见的分支里回显地址是允许的
PLATFORM_CREDENTIALS = (PLATFORM_API_KEY, PLATFORM_PROXY, "proxy-pass-must-stay-secret")

PLATFORM_OWNER = CurrentIdentity(user_id=1, username="owner", role=TenantRole.PLATFORM_OWNER, tenant_id=None)
TENANT_ADMIN = CurrentIdentity(user_id=2, username="cust-admin", role=TenantRole.TENANT_ADMIN, tenant_id=7)
TENANT_MEMBER = CurrentIdentity(user_id=3, username="cust-user", role=TenantRole.TENANT_ADMIN, tenant_id=7)


@pytest.fixture(autouse=True)
def reset_rate_limiter() -> Iterator[None]:
    """限流器是模块级单例，用例之间必须互不影响"""
    model_group_test_module.model_test_rate_limiter.reset()
    yield
    model_group_test_module.model_test_rate_limiter.reset()


@pytest.fixture
def skip_outbound_check(monkeypatch: MonkeyPatch) -> List[Tuple[str, str]]:
    """跳过出站地址复检并记录调用

    复检会做真实 DNS 解析，`*.example` 域名的解析结果取决于运行环境，直接放进用例
    会让断言依赖网络。真正校验该复检行为的用例见
    `test_outbound_recheck_is_skipped_for_platform_group`，那里用 127.0.0.1 本地解析。
    """
    calls: List[Tuple[str, str]] = []

    async def fake_assert(_identity: CurrentIdentity, base_url: str, chat_proxy: str = "") -> None:
        calls.append((base_url, chat_proxy))

    monkeypatch.setattr(model_group_test_module, "assert_outbound_urls_allowed", fake_assert)
    return calls


# ============================================================
# 夹具数据
# ============================================================


def platform_group(model_type: str = "chat") -> ModelConfigGroup:
    """平台方的模型组配置（带真实形态的凭据）"""
    return ModelConfigGroup(
        GROUP_NAME="platform-group",
        CHAT_MODEL="gpt-4o",
        BASE_URL=PLATFORM_BASE_URL,
        API_KEY=PLATFORM_API_KEY,
        CHAT_PROXY=PLATFORM_PROXY,
        MODEL_TYPE=model_type,  # pyright: ignore[reportArgumentType]
    )


def platform_row() -> DBModelGroup:
    """平台方的模型组行；未落库，只用于归属判定"""
    return DBModelGroup(
        name="platform-group",
        ownership=ModelGroupOwnership.PLATFORM.value,
        owner_tenant_id=None,
        visibility=ModelGroupVisibility.SHARED.value,
        chat_model="gpt-4o",
        base_url=PLATFORM_BASE_URL,
        api_key=PLATFORM_API_KEY,
        chat_proxy=PLATFORM_PROXY,
        model_type="chat",
    )


def tenant_row(tenant_id: int = 7) -> DBModelGroup:
    """租户自建的模型组行"""
    return DBModelGroup(
        name="tenant-group",
        ownership=ModelGroupOwnership.TENANT.value,
        owner_tenant_id=tenant_id,
        visibility=ModelGroupVisibility.PRIVATE.value,
        chat_model="gpt-4o-mini",
        base_url=TENANT_BASE_URL,
        api_key=TENANT_API_KEY,
        chat_proxy="",
        model_type="chat",
    )


def stub_upstream(
    monkeypatch: MonkeyPatch,
    *,
    raises: Optional[BaseException] = None,
    content: str = "Test",
    embedding: Optional[List[float]] = None,
) -> None:
    """把上游调用换成本地桩：用例绝不真的打外网"""
    import nekro_agent.services.agent.openai as openai_service

    async def fake_chat(**_kwargs: Any) -> Any:
        if raises is not None:
            raise raises
        return SimpleNamespace(response_content=content, use_model="stub-model", token_input=11, token_output=7)

    async def fake_embeddings(**_kwargs: Any) -> List[float]:
        if raises is not None:
            raise raises
        return embedding if embedding is not None else [0.1, 0.2, 0.3]

    monkeypatch.setattr(openai_service, "gen_openai_chat_response", fake_chat)
    monkeypatch.setattr(openai_service, "gen_openai_embeddings", fake_embeddings)


def _request() -> httpx.Request:
    return httpx.Request("POST", f"{PLATFORM_BASE_URL}/chat/completions")


def leaky_upstream_errors() -> List[Tuple[str, BaseException]]:
    """复刻各供应商真实出现过的凭据回显形态

    每一条都对应模块文档里的一个泄露面；新增供应商踩到新形态时往这里加一行即可。
    """
    request = _request()
    return [
        (
            "异常文案带 query string 里的 key",
            httpx.HTTPStatusError(
                f"Client error '400 Bad Request' for url "
                f"'{PLATFORM_BASE_URL}/models/x:generateContent?key={PLATFORM_API_KEY}'",
                request=request,
                response=httpx.Response(400, request=request),
            ),
        ),
        (
            "上游 4xx 响应体回显收到的 key",
            openai.AuthenticationError(
                message=f"Error code: 401 - {{'error': {{'message': 'Incorrect API key provided: {PLATFORM_API_KEY}'}}}}",
                response=httpx.Response(401, request=request),
                body=None,
            ),
        ),
        (
            "WWW-Authenticate 响应头带凭据",
            openai.AuthenticationError(
                message=f"Error code: 401 - headers: {{'www-authenticate': 'Bearer {PLATFORM_API_KEY}'}}",
                response=httpx.Response(
                    401,
                    request=request,
                    headers={"www-authenticate": f"Bearer {PLATFORM_API_KEY}"},
                ),
                body=None,
            ),
        ),
        (
            "重定向 Location 带凭据",
            httpx.HTTPStatusError(
                f"Redirect response '307' for url '{PLATFORM_BASE_URL}' -> "
                f"'https://relay.example/v1?api_key={PLATFORM_API_KEY}'",
                request=request,
                response=httpx.Response(
                    307,
                    request=request,
                    headers={"location": f"https://relay.example/v1?api_key={PLATFORM_API_KEY}"},
                ),
            ),
        ),
        (
            "代理凭据出现在连接错误里",
            httpx.ProxyError(f"proxy {PLATFORM_PROXY} refused connection"),
        ),
        (
            "整段请求头被回显",
            RuntimeError(f"upstream echoed request: Authorization: Bearer {PLATFORM_API_KEY}"),
        ),
    ]


def iter_payload_strings(value: Any) -> Iterator[str]:
    """递归取出结构体里的全部字符串，供泄露扫描使用"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]
            yield str(key)
            yield from iter_payload_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:  # pyright: ignore[reportUnknownVariableType]
            yield from iter_payload_strings(item)


def assert_no_secret_leak(outcome: ModelGroupTestOutcome, secrets: Tuple[str, ...]) -> None:
    """断言真实凭据的明文不出现在出参的任何字段里

    同时扫描 `model_dump()` 的每个字符串与序列化后的整段 JSON：前者保证没有漏看的嵌套字段，
    后者保证没有绕过 dump 的字段。
    """
    dumped: Dict[str, Any] = outcome.model_dump()
    serialized = outcome.model_dump_json()
    for secret in secrets:
        for text in iter_payload_strings(dumped):
            assert secret not in text, f"凭据明文出现在响应字段中: {text!r}"
        assert secret not in serialized, "凭据明文出现在序列化后的响应里"


# ============================================================
# 上游异常的归一化分类
# ============================================================


def test_classify_upstream_error_归档到有限结论() -> None:
    """分类只看异常类型与状态码，不看文案内容"""
    request = _request()

    def status_error(status: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError(
            f"error {status}",
            request=request,
            response=httpx.Response(status, request=request),
        )

    def wrapped(outer: BaseException, cause: BaseException) -> BaseException:
        """构造 openai/httpx 那样的多层包装，验证异常链回溯"""
        outer.__cause__ = cause
        return outer

    cases: List[Tuple[str, BaseException, ModelTestErrorCode, Optional[int]]] = [
        ("401", status_error(401), ModelTestErrorCode.UNAUTHORIZED, 401),
        ("403", status_error(403), ModelTestErrorCode.FORBIDDEN, 403),
        ("404", status_error(404), ModelTestErrorCode.MODEL_NOT_FOUND, 404),
        ("408", status_error(408), ModelTestErrorCode.TIMEOUT, 408),
        ("429", status_error(429), ModelTestErrorCode.RATE_LIMITED, 429),
        ("400", status_error(400), ModelTestErrorCode.UPSTREAM_REJECTED, 400),
        ("422", status_error(422), ModelTestErrorCode.UPSTREAM_REJECTED, 422),
        ("500", status_error(500), ModelTestErrorCode.UPSTREAM_ERROR, 500),
        ("503", status_error(503), ModelTestErrorCode.UPSTREAM_ERROR, 503),
        (
            "openai 状态异常",
            openai.AuthenticationError(
                message="Error code: 401",
                response=httpx.Response(401, request=request),
                body=None,
            ),
            ModelTestErrorCode.UNAUTHORIZED,
            401,
        ),
        (
            "openai 包装的 DNS 失败",
            wrapped(
                openai.APIConnectionError(request=request),
                wrapped(httpx.ConnectError("[Errno -2] Name or service not known"), socket.gaierror(-2, "no host")),
            ),
            ModelTestErrorCode.DNS_FAILURE,
            None,
        ),
        (
            "TLS 握手失败",
            wrapped(httpx.ConnectError("ssl handshake failed"), ssl.SSLError("CERTIFICATE_VERIFY_FAILED")),
            ModelTestErrorCode.TLS_ERROR,
            None,
        ),
        ("httpx 连接超时", httpx.ConnectTimeout("timed out"), ModelTestErrorCode.TIMEOUT, None),
        (
            "openai 包装的超时",
            wrapped(openai.APITimeoutError(request=request), httpx.ReadTimeout("read timed out")),
            ModelTestErrorCode.TIMEOUT,
            None,
        ),
        ("代理不可用", httpx.ProxyError("proxy refused"), ModelTestErrorCode.PROXY_ERROR, None),
        ("连接被拒", httpx.ConnectError("connection refused"), ModelTestErrorCode.CONNECTION_FAILED, None),
        ("返回内容无效", ValueError("模型未返回有效内容"), ModelTestErrorCode.INVALID_RESPONSE, None),
        ("兜底内部错误", RuntimeError("boom"), ModelTestErrorCode.INTERNAL_ERROR, None),
    ]

    for label, exc, expected_code, expected_status in cases:
        code, status = classify_upstream_error(exc)
        assert (code, status) == (expected_code, expected_status), f"{label} 分类错误: {code} / {status}"


def test_归一化文案覆盖全部结论码() -> None:
    """每个结论码都必须有预定义文案，否则响应会退化成兜底文案"""
    for code in ModelTestErrorCode:
        assert code in NORMALIZED_MESSAGES, f"{code.value} 缺少归一化文案"
        assert NORMALIZED_MESSAGES[code].strip(), f"{code.value} 的文案为空"


# ============================================================
# 泄露面：租户视角
# ============================================================


@pytest.mark.parametrize(("label", "error"), leaky_upstream_errors(), ids=lambda v: v if isinstance(v, str) else "")
async def test_租户视角不回显平台凭据(label: str, error: BaseException, monkeypatch: MonkeyPatch) -> None:
    """凭据不可见时，出参里既没有凭据也没有任何上游原文"""
    stub_upstream(monkeypatch, raises=error)

    outcome = await run_model_group_test(platform_group(), credential_visible=False)

    assert outcome.ok is False, label
    assert_no_secret_leak(outcome, PLATFORM_CREDENTIALS + (PLATFORM_BASE_URL,))
    assert outcome.message in set(NORMALIZED_MESSAGES.values()), "文案必须来自有限集合"
    assert outcome.detail == "", "凭据不可见时不得回显上游原文"
    assert outcome.response_text == "", "凭据不可见时不得回显上游内容"


@pytest.mark.parametrize(("label", "error"), leaky_upstream_errors(), ids=lambda v: v if isinstance(v, str) else "")
async def test_凭据可见时仍然抹掉密钥(label: str, error: BaseException, monkeypatch: MonkeyPatch) -> None:
    """凭据可见的分支回显上游原文，但密钥与代理口令仍要被脱敏器抹掉"""
    stub_upstream(monkeypatch, raises=error)

    outcome = await run_model_group_test(platform_group(), credential_visible=True)

    assert outcome.ok is False, label
    assert_no_secret_leak(outcome, PLATFORM_CREDENTIALS)
    assert outcome.detail, "凭据可见时应给出脱敏后的上游原文，便于排障"
    assert outcome.message in set(NORMALIZED_MESSAGES.values())


async def test_同类失败对租户完全不可区分(monkeypatch: MonkeyPatch) -> None:
    """旁路推断：两个文案迥异但同为 401 的失败，对租户必须给出完全一致的结论"""
    request = _request()
    first = openai.AuthenticationError(
        message=f"Incorrect API key provided: {PLATFORM_API_KEY}",
        response=httpx.Response(401, request=request),
        body=None,
    )
    second = openai.AuthenticationError(
        message="Your account is not authorized for this endpoint",
        response=httpx.Response(401, request=request),
        body=None,
    )

    stub_upstream(monkeypatch, raises=first)
    first_outcome = await run_model_group_test(platform_group(), credential_visible=False)
    stub_upstream(monkeypatch, raises=second)
    second_outcome = await run_model_group_test(platform_group(), credential_visible=False)

    assert first_outcome.model_dump(exclude={"latency_ms"}) == second_outcome.model_dump(exclude={"latency_ms"})


async def test_成功时租户只拿到结论而非模型输出(monkeypatch: MonkeyPatch) -> None:
    """上游回复可能被诱导复述凭据，凭据不可见时一律不回显"""
    stub_upstream(monkeypatch, content=f"这是我的 key: {PLATFORM_API_KEY}")

    tenant_view = await run_model_group_test(platform_group(), credential_visible=False)
    assert tenant_view.ok is True
    assert tenant_view.error_code is ModelTestErrorCode.NONE
    assert tenant_view.message == NORMALIZED_MESSAGES[ModelTestErrorCode.NONE]
    assert tenant_view.response_text == ""
    assert (tenant_view.input_tokens, tenant_view.output_tokens) == (0, 0)
    assert_no_secret_leak(tenant_view, PLATFORM_CREDENTIALS + (PLATFORM_BASE_URL,))

    owner_view = await run_model_group_test(platform_group(), credential_visible=True)
    assert owner_view.response_text.startswith("这是我的 key")
    assert (owner_view.input_tokens, owner_view.output_tokens) == (11, 7)


async def test_嵌入模型组的成功结论同样受控(monkeypatch: MonkeyPatch) -> None:
    """嵌入分支与聊天分支共用同一套出参规则"""
    stub_upstream(monkeypatch, embedding=[0.0] * 1536)

    tenant_view = await run_model_group_test(platform_group("embedding"), credential_visible=False)
    assert (tenant_view.ok, tenant_view.response_text) == (True, "")

    owner_view = await run_model_group_test(platform_group("embedding"), credential_visible=True)
    assert owner_view.response_text == "向量维度: 1536"


async def test_未配置与不支持的类型不发起请求(monkeypatch: MonkeyPatch) -> None:
    """没配全或类型不支持时直接给结论，不消耗平台方额度"""

    async def explode(**_kwargs: Any) -> Any:
        raise AssertionError("不应发起上游请求")

    import nekro_agent.services.agent.openai as openai_service

    monkeypatch.setattr(openai_service, "gen_openai_chat_response", explode)
    monkeypatch.setattr(openai_service, "gen_openai_embeddings", explode)

    draw_group = platform_group("draw")
    unsupported = await run_model_group_test(draw_group, credential_visible=True)
    assert unsupported.error_code is ModelTestErrorCode.UNSUPPORTED_MODEL_TYPE

    empty_group = platform_group()
    empty_group.API_KEY = ""
    not_configured = await run_model_group_test(empty_group, credential_visible=True)
    assert not_configured.error_code is ModelTestErrorCode.NOT_CONFIGURED


# ============================================================
# 泄露面：出站地址复检
# ============================================================


async def test_outbound_recheck_is_skipped_for_platform_group() -> None:
    """平台组对租户不做出站复检

    平台方用本地推理服务是主流用法，对租户复检会被拒，而「被拒」本身就告诉了租户平台方的组指向内网
    （复检失败的文案已不带主机名与解析出的地址，统一成一句，但拒与不拒仍分得出来）；租户又控制不了
    这个地址，跳过不增加任何出站能力。
    """
    internal_group = platform_group()
    internal_group.BASE_URL = "http://127.0.0.1:11434/v1"
    internal_group.CHAT_PROXY = ""

    await assert_test_target_allowed(TENANT_ADMIN, internal_group, credential_visible=False)

    with pytest.raises(ValidationError) as exc_info:
        await assert_test_target_allowed(TENANT_ADMIN, internal_group, credential_visible=True)
    assert "127.0.0.1" not in str(exc_info.value), "租户看到的报错不带地址"


async def test_平台方自身不受出站校验限制() -> None:
    """平台方接本地推理服务是正常用法，不该被自己的租户防线拦住"""
    internal_group = platform_group()
    internal_group.BASE_URL = "http://127.0.0.1:11434/v1"
    internal_group.CHAT_PROXY = ""

    await assert_test_target_allowed(PLATFORM_OWNER, internal_group, credential_visible=True)


# ============================================================
# 防滥用限流
# ============================================================


def test_rate_limiter_sliding_window() -> None:
    """滑动窗口：窗口内超额即拒，窗口滑过后恢复"""
    limiter = ModelTestRateLimiter(max_calls=3, window_seconds=60.0)

    assert [limiter.try_acquire("t:1", now=0.0) for _ in range(3)] == [None, None, None]

    denied = limiter.try_acquire("t:1", now=10.0)
    assert denied is not None and denied == pytest.approx(50.0)

    # 另一个键互不影响
    assert limiter.try_acquire("t:2", now=10.0) is None

    # 最早一次的 60 秒窗口滑过后放行
    assert limiter.try_acquire("t:1", now=60.0) is None


def test_平台方不限流() -> None:
    """花的是平台方自己的额度，不设上限"""
    for _ in range(model_group_test_module.MODEL_TEST_RATE_MAX_CALLS * 3):
        assert_model_test_quota(PLATFORM_OWNER)


def test_同租户成员共用一个限流桶() -> None:
    """限的是"这个客户"而不是"这个账号"，否则多建几个成员账号就能绕过"""
    identities = [TENANT_ADMIN, TENANT_MEMBER]
    for index in range(model_group_test_module.MODEL_TEST_RATE_MAX_CALLS):
        assert_model_test_quota(identities[index % 2])

    with pytest.raises(ModelTestRateLimitedError) as exc_info:
        assert_model_test_quota(TENANT_MEMBER)
    assert exc_info.value.http_status == 429


# ============================================================
# 端点：inline 测试的凭据外带通道
# ============================================================


class _FakeGroupQuery:
    """替身：只提供端点用得到的 `get_or_none`，避免用例依赖数据库"""

    def __init__(self, row: Optional[DBModelGroup]) -> None:
        self._row = row

    async def get_or_none(self, **_kwargs: Any) -> Optional[DBModelGroup]:
        return self._row


def _tenant_user() -> SimpleNamespace:
    return SimpleNamespace(
        id=TENANT_ADMIN.user_id,
        username=TENANT_ADMIN.username,
        tenant_id=TENANT_ADMIN.tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        perm_level=Role.Admin,
    )


def _spy_on_test_runner(monkeypatch: MonkeyPatch) -> List[Tuple[ModelConfigGroup, bool]]:
    """记录真正发出去的模型组配置与凭据可见性"""
    calls: List[Tuple[ModelConfigGroup, bool]] = []

    async def fake_run(
        group: ModelConfigGroup,
        *,
        credential_visible: bool,
        **_usage_attribution: Any,
    ) -> ModelGroupTestOutcome:
        # `**_usage_attribution` 吃掉 `group_name` / `tenant_id`：那两个是**用量归属**的
        # 入参（后来加的，见 `services/model_group_test.run_model_group_test`），
        # 与本文件要测的"凭据可见性"是两件事。替身若不接受它们，本文件会在调用点
        # 直接 TypeError —— 而那正是真实调用点确实传了它们的证据。
        # 它们**传对了没有**由 `tests/test_llm_usage_metering.py` 的
        # `TestEveryModelTestCallSiteDeclaresAttribution` 逐个调用点钉，不在这里重复一遍。
        calls.append((group, credential_visible))
        return ModelGroupTestOutcome(ok=True, message="连接正常", model_name=group.CHAT_MODEL)

    monkeypatch.setattr(config_router, "run_model_group_test", fake_run)
    return calls


def _inline_body(group_name: str, *, api_key: str, base_url: str) -> Any:
    return config_router.ModelGroupInlineTestRequest(
        group_name=group_name,
        chat_model="gpt-4o",
        base_url=base_url,
        api_key=api_key,
        model_type="chat",
    )


async def test_inline_refuses_credential_fallback_for_platform_group(
    monkeypatch: MonkeyPatch,
    skip_outbound_check: List[Tuple[str, str]],
) -> None:
    """租户不能借"保持不变"哨兵把平台方的 Key 发到自己的地址上

    这是一条完整的外带通道：掩码 + 自己的 base_url，服务端一回落就把真实 Key
    填进 Authorization 头发到租户指定的服务器。必须在回落这一步拒绝。
    """
    monkeypatch.setattr(config_router, "DBModelGroup", _FakeGroupQuery(platform_row()))
    calls = _spy_on_test_runner(monkeypatch)

    with pytest.raises(PermissionDeniedError):
        await config_router.test_model_group_inline(
            _inline_body("platform-group", api_key=CREDENTIAL_MASK, base_url="https://exfil.tenant.example/v1"),
            _current_user=_tenant_user(),
        )

    assert calls == [], "拒绝之后不得发起任何上游请求"
    assert skip_outbound_check == [], "拒绝发生在出站校验之前"


async def test_inline_substitutes_mask_only_for_own_group(
    monkeypatch: MonkeyPatch,
    skip_outbound_check: List[Tuple[str, str]],
) -> None:
    """租户自建组的凭据归他自己，掩码回落照常生效"""
    monkeypatch.setattr(config_router, "DBModelGroup", _FakeGroupQuery(tenant_row()))
    calls = _spy_on_test_runner(monkeypatch)

    await config_router.test_model_group_inline(
        _inline_body("tenant-group", api_key=CREDENTIAL_MASK, base_url=TENANT_BASE_URL),
        _current_user=_tenant_user(),
    )

    assert len(calls) == 1
    sent_group, credential_visible = calls[0]
    assert sent_group.API_KEY == TENANT_API_KEY
    assert credential_visible is True
    assert skip_outbound_check == [(TENANT_BASE_URL, "")], "租户自己填的地址必须过出站校验"


async def test_inline_未保存的新组不做任何回落(
    monkeypatch: MonkeyPatch,
    skip_outbound_check: List[Tuple[str, str]],
) -> None:
    """新建流程里组名还不存在，提交什么就用什么"""
    monkeypatch.setattr(config_router, "DBModelGroup", _FakeGroupQuery(None))
    calls = _spy_on_test_runner(monkeypatch)

    await config_router.test_model_group_inline(
        _inline_body("brand-new", api_key=TENANT_API_KEY, base_url=TENANT_BASE_URL),
        _current_user=_tenant_user(),
    )

    assert len(calls) == 1
    assert calls[0][0].API_KEY == TENANT_API_KEY
    assert skip_outbound_check == [(TENANT_BASE_URL, "")]


# ============================================================
# 端点：批量测试的可见性判定
# ============================================================


def _stub_visible_group(monkeypatch: MonkeyPatch, rows: Dict[str, DBModelGroup]) -> None:
    """按名字返回可见的模型组行；缺失即按不存在处理，与真实实现的语义一致"""

    async def fake_get_visible_group(name: str, _identity: CurrentIdentity) -> DBModelGroup:
        if name not in rows:
            raise ModelGroupNotFoundError(name=name)
        return rows[name]

    monkeypatch.setattr(config_router, "get_visible_group", fake_get_visible_group)


async def test_批量测试按行归属决定凭据可见性(
    monkeypatch: MonkeyPatch,
    skip_outbound_check: List[Tuple[str, str]],
) -> None:
    """平台组只回归一化结论，租户自建组才回显上游原文"""
    _stub_visible_group(monkeypatch, {"platform-group": platform_row(), "tenant-group": tenant_row()})
    calls = _spy_on_test_runner(monkeypatch)

    response = await config_router.test_model_groups(
        config_router.ModelGroupTestRequest(group_names=["platform-group", "tenant-group"]),
        _current_user=_tenant_user(),
    )

    assert len(response.items) == 2
    visibility_by_key = {group.API_KEY: visible for group, visible in calls}
    assert visibility_by_key == {PLATFORM_API_KEY: False, TENANT_API_KEY: True}


async def test_批量测试对不可见的组按不存在处理(monkeypatch: MonkeyPatch) -> None:
    """用不存在掩盖无权访问，避免把别的租户的组名变成可枚举的"""
    _stub_visible_group(monkeypatch, {})
    calls = _spy_on_test_runner(monkeypatch)

    response = await config_router.test_model_groups(
        config_router.ModelGroupTestRequest(group_names=["someone-else-group"]),
        _current_user=_tenant_user(),
    )

    assert len(response.items) == 1
    item = response.items[0]
    assert item.success is False
    assert item.error_code is ModelTestErrorCode.GROUP_NOT_FOUND
    assert item.error_message == NORMALIZED_MESSAGES[ModelTestErrorCode.GROUP_NOT_FOUND]
    assert calls == [], "不可见的组不得发起上游请求"


async def test_批量测试超额时返回限流错误(
    monkeypatch: MonkeyPatch,
    skip_outbound_check: List[Tuple[str, str]],
) -> None:
    """连通性测试花的是平台方的钱，租户侧必须有次数上限"""
    _stub_visible_group(monkeypatch, {"platform-group": platform_row()})
    _spy_on_test_runner(monkeypatch)

    request = config_router.ModelGroupTestRequest(group_names=["platform-group"])
    for _ in range(model_group_test_module.MODEL_TEST_RATE_MAX_CALLS):
        await config_router.test_model_groups(request, _current_user=_tenant_user())

    with pytest.raises(ModelTestRateLimitedError):
        await config_router.test_model_groups(request, _current_user=_tenant_user())
