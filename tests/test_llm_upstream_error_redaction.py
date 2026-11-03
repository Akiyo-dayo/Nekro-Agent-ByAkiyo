"""LLM 上游报错原文的脱敏回归测试

## 保护的契约

上游 400/401/5xx 的报错体**内容由上游决定**，供应商回显凭据是常见形态。此前这段文本
一路原样流到四个出口，其中一个是**租户可见**的：

| 出口 | 谁能看到 |
| --- | --- |
| `logger.exception` -> `app.log` / 内存日志环 / `/logs` / 运维 Bot 推送 | 平台方 + 备份 |
| `prompts_error/*.json` 的 `error.message` | 平台方 + `/deployment/backup` 打包 |
| `AgentRuntimeStatusEvent.error_summary` -> `/events` SSE | **租户**（该端点是 `Role.Admin`） |
| `DBExecCode.extra_data.llm_retry_errors` | 平台方 + 数据库备份 |

租户的会话跑在平台方的模型组上，所以第三条是一次绕过所有 HTTP 层掩码的直投。

## 两个方向都要断言

只断言「凭据 0 命中」是不够的——把整段报错换成一个常量同样满足它，而那等于把排障废掉。
每一条用例都同时断言**非敏感部分逐字保留**（状态码、异常类名、供应商文案、模型名）。

## 三个哨兵，对应两条判据各自的盲区

- `PLATFORM_SK_KEY`：在配置树里 **且** 是 `sk-` 形态 —— 两条判据都能抓；
- `PLATFORM_OPAQUE_KEY`：在配置树里，但**没有任何可识别形态**（自建网关的 32 位 hex）
  —— 只有「按值」那条抓得到，证明形态正则不可单独使用；
- `RELAY_THIRD_PARTY_KEY`：`sk-` 形态但**不属于本部署**（中转站把它自己上游的 Key 抄回来了）
  —— 只有「按形态」那条抓得到，证明字面量替换同样不可单独使用。

三个哨兵全部是本文件自己编的（§2.13：样例凭据必须是自己编的，
上一轮真的在测试文件里发现过测试部署的真口令）。

全部确定性：不起真网络、不靠时间流逝、不做随机 fuzz。
"""

from __future__ import annotations

import ast
import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

from nekro_agent.core import logger
from nekro_agent.core.config import CoreConfig, ModelConfigGroup, config
from nekro_agent.core.core_utils import ConfigManager
from nekro_agent.services.agent import openai as openai_module
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.model_capability import (
    LLMFailureKind,
    classify_llm_failure,
    reset_capability_marks,
)
from nekro_agent.services.agent.openai import (
    OpenAIErrResponse,
    gen_openai_chat_response,
)
from nekro_agent.services.agent.run_agent import (
    AllLLMRequestsFailedError,
    send_agent_request,
)
from nekro_agent.services.agent.upstream_redaction import (
    collect_llm_secret_values,
    format_redacted_traceback,
    redact_upstream_text,
)
from nekro_agent.services.memory.recall_contract import MemoryRecallPlan
from nekro_agent.tools.redaction import REDACTED_CREDENTIAL

# ============================================================
# 哨兵（本文件自造，不得取自任何真实部署）
# ============================================================

PLATFORM_SK_KEY = "sk-plat39platform-DO-NOT-LEAK-4a7c1e93b6d2f085"
PLATFORM_OPAQUE_KEY = "9f8e7d6c5b4a392817065544332211aabbccddee"
RELAY_THIRD_PARTY_KEY = "sk-relaystationupstream-1122334455667788990a"
PLATFORM_BASE_URL = "https://api.internal.example/v1"
PLATFORM_PROXY = "http://proxyuser:proxysecret9988@proxy.example:7890"

#: 上游 401 把整条 Authorization 头抄回来——现场最常见的一种回显
UPSTREAM_401_ECHOES_HEADER = (
    "Error code: 401 - {'error': {'message': 'Incorrect API key provided: "
    f"{PLATFORM_SK_KEY}"
    ". You can find your API key at https://platform.example/account/api-keys', "
    "'type': 'invalid_request_error', 'code': 'invalid_api_key'}} | "
    f"request headers: Authorization: Bearer {PLATFORM_SK_KEY}"
)

#: 上游 400 把 key 拼进 URL 回显（`plugins/builtin/draw` 踩过的同款形态）
UPSTREAM_400_ECHOES_URL = (
    "Client error '400 Bad Request' for url "
    f"'{PLATFORM_BASE_URL}/models/x:generateContent?key={PLATFORM_OPAQUE_KEY}'"
)

#: 中转站 502：回显的是**它自己上游**的凭据，我们的配置树里根本没有这一条
UPSTREAM_502_RELAY_KEY = (
    "Error code: 502 - {'error': {'message': 'upstream rejected: "
    f"Authorization: Bearer {RELAY_THIRD_PARTY_KEY}"
    "', 'type': 'relay_error'}}"
)

#: 能力不匹配那一类的原文，外加一段被回显的凭据。
#: 用来钉住「脱敏不能把分类判据吃掉」
UPSTREAM_VISION_400_WITH_KEY = (
    "Error code: 400 - {'error': {'message': '当前模型不支持该能力：vision', "
    "'type': 'invalid_request_error', 'code': 'model_not_support'}} "
    f"(sent with Authorization: Bearer {PLATFORM_SK_KEY})"
)

ALL_SENTINELS = (PLATFORM_SK_KEY, PLATFORM_OPAQUE_KEY, RELAY_THIRD_PARTY_KEY)

_AGENT_DIR = Path(run_agent_module.__file__).parent


class _FakeStatusError(Exception):
    """模拟 openai SDK 的 `APIStatusError`：状态码挂在异常属性上"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _hits(text: str, *sentinels: str) -> int:
    """text 里出现了多少次真实凭据"""
    return sum(text.count(sentinel) for sentinel in (sentinels or ALL_SENTINELS))


@pytest.fixture()
def platform_config():
    """把带哨兵凭据的模型组挂进核心配置，并注册到 `ConfigManager`

    `collect_platform_secrets()` 取的是**活配置树**，不注册就等于这条判据没有输入，
    「0 命中」会变成「我压根没去看」（§2.12 的审计范围陷阱同款）。
    """
    group_key = "__test_upstream_redaction__"
    opaque_key = "__test_upstream_redaction_opaque__"
    config.MODEL_GROUPS[group_key] = ModelConfigGroup(
        GROUP_NAME=group_key,
        CHAT_MODEL="gpt-4o",
        BASE_URL=PLATFORM_BASE_URL,
        API_KEY=PLATFORM_SK_KEY,
    )
    config.MODEL_GROUPS[opaque_key] = ModelConfigGroup(
        GROUP_NAME=opaque_key,
        CHAT_MODEL="self-hosted-llm",
        BASE_URL=PLATFORM_BASE_URL,
        API_KEY=PLATFORM_OPAQUE_KEY,
    )
    ConfigManager.register_config(CoreConfig.get_config_key(), config)
    yield
    config.MODEL_GROUPS.pop(group_key, None)
    config.MODEL_GROUPS.pop(opaque_key, None)


@pytest.fixture()
def log_sink():
    """抓住 loguru 真正写出去的内容（含它自己渲染的 traceback）

    断言只看 `logger.error(...)` 的 f-string 是**测不出这条缺陷的**：
    `logger.exception` 另外渲染一份带局部变量的诊断 traceback，那份不经过 f-string。
    """
    captured: List[Any] = []
    handler_id = logger.add(captured.append, level="DEBUG", format="{message}", colorize=False)
    try:
        yield captured
    finally:
        logger.remove(handler_id)


def _sink_text(captured: List[Any]) -> str:
    return "".join(str(message) for message in captured)


@pytest.fixture(autouse=True)
def _clean_process_state():
    reset_capability_marks()
    yield
    reset_capability_marks()


# ============================================================
# 1. 判据本身：按值 + 按形态，缺一条就有盲区
# ============================================================


@pytest.mark.usefixtures("platform_config")
class TestBothJudgementsAreRequired:
    """#37 脱的是「已知配置项的取值」，这里脱的是「任意上游返回的文本」

    前者按值就够（键名可枚举），后者不行：回显的凭据可能根本不属于本部署。
    反过来只靠形态也不行：自建网关的 Key 没有任何可识别前缀。
    """

    def test_value_judgement_catches_a_shapeless_platform_key(self) -> None:
        """`PLATFORM_OPAQUE_KEY` 是 40 位 hex，三条形态正则**一条都不命中**"""
        from nekro_agent.tools.redaction import redact_credentials

        shape_only = redact_credentials(UPSTREAM_400_ECHOES_URL)
        assert PLATFORM_OPAQUE_KEY in shape_only, "前提失效：这个哨兵本该躲过形态正则"

        redacted = redact_upstream_text(UPSTREAM_400_ECHOES_URL)
        assert _hits(redacted) == 0
        assert REDACTED_CREDENTIAL in redacted

    def test_shape_judgement_catches_a_key_that_is_not_ours(self) -> None:
        """中转站回显它自己上游的 Key：配置树里没有，字面量替换抓不到"""
        values = collect_llm_secret_values()
        assert RELAY_THIRD_PARTY_KEY not in values, "前提失效：这个哨兵不该出现在配置树里"

        redacted = redact_upstream_text(UPSTREAM_502_RELAY_KEY, values)
        assert _hits(redacted) == 0

    def test_both_judgements_on_the_same_text(self) -> None:
        redacted = redact_upstream_text(UPSTREAM_401_ECHOES_HEADER)
        assert _hits(redacted) == 0

    @pytest.mark.parametrize(
        ("raw", "preserved"),
        [
            (
                UPSTREAM_401_ECHOES_HEADER,
                ("Error code: 401", "invalid_request_error", "invalid_api_key", "Incorrect API key provided"),
            ),
            (UPSTREAM_400_ECHOES_URL, ("400 Bad Request", "generateContent")),
            (UPSTREAM_502_RELAY_KEY, ("Error code: 502", "relay_error", "upstream rejected")),
            (
                UPSTREAM_VISION_400_WITH_KEY,
                ("Error code: 400", "当前模型不支持该能力：vision", "model_not_support"),
            ),
        ],
    )
    def test_the_diagnostic_part_survives_verbatim(self, raw: str, preserved: tuple) -> None:
        """另一个方向：把整段报错吞掉同样能让「0 命中」成立，那是把排障废掉"""
        redacted = redact_upstream_text(raw)
        assert _hits(redacted) == 0
        for fragment in preserved:
            assert fragment in redacted, f"诊断信息被吃掉了: {fragment!r}"

    def test_benign_text_is_left_alone(self) -> None:
        """不含凭据的报错逐字不变——避免「反正都遮掉」式的实现混过测试"""
        benign = "Error code: 429 - {'error': {'message': 'Rate limit reached for gpt-4o', 'type': 'rate_limit'}}"
        assert redact_upstream_text(benign) == benign

    def test_max_tokens_is_not_mistaken_for_a_token(self) -> None:
        """`_KV_SECRET_RE` 的 `token` 分支不能吃掉生成参数——`max_tokens` 里没有词边界"""
        text = "Error code: 400 - max_tokens must be <= 4096, got 999999"
        assert redact_upstream_text(text) == text


@pytest.mark.usefixtures("platform_config")
class TestSecretValueCollection:
    def test_collects_the_live_config_tree(self) -> None:
        values = collect_llm_secret_values()
        assert PLATFORM_SK_KEY in values
        assert PLATFORM_OPAQUE_KEY in values

    def test_extra_secrets_cover_the_override_layers(self) -> None:
        """本次请求真正用的模型组可能来自会话/实例覆盖层，全局配置树里没有它"""
        override_key = "sk-plat39-channel-override-77665544332211"
        assert override_key not in collect_llm_secret_values()
        assert override_key in collect_llm_secret_values(override_key)

    def test_empty_extras_are_ignored(self) -> None:
        """`API_KEY` 为空是自建上游的常见状态；空串进了集合会让替换把文本打成乱码"""
        values = collect_llm_secret_values("", None)
        assert "" not in values
        assert None not in values


# ============================================================
# 2. traceback：loguru 的 diagnose 会把局部变量打出来
# ============================================================


class TestTracebackRendering:
    """`logger.exception` 渲染的不只是 f-string

    loguru 的 `diagnose` 默认开着，会把每一行源码里出现的名字连同取值一起打出来。
    实测测试部署 `app.log` 里因此存着 50 处 `ConnectionParameters(user=…, password=…)`
    —— 那是 tortoise/asyncpg 连库失败时被诊断行打出来的数据库口令。
    """

    @staticmethod
    def _upstream_call(api_key: str, payload: Dict[str, str]) -> None:
        raise _FakeStatusError(f"Error code: 401 - rejected {payload}", 401)

    def _raise_with_locals(self) -> None:
        # 复刻 `gen_openai_chat_response` 的形状：凭据以**裸名字**出现在调用行上。
        # loguru 的 diagnose 标注的正是「源码行里出现的名字」，把凭据藏进 f-string 字面量
        # 反而复现不出来——对照组必须与被测代码同形，否则它证明不了任何事（§2.15 同款）。
        api_key = PLATFORM_SK_KEY
        payload = {"authorization": f"Bearer {api_key}"}
        self._upstream_call(api_key, payload)

    def test_logger_exception_really_leaks_locals(self, log_sink) -> None:
        """对照组：证明这份测试测得出差异（§2.11）。这条**不是**产品行为，是反例"""
        try:
            self._raise_with_locals()
        except _FakeStatusError as exc:
            logger.opt(exception=exc).error("对照组：未脱敏的 logger.exception")

        text = _sink_text(log_sink)
        assert _hits(text, PLATFORM_SK_KEY) >= 2, "对照组没能复现泄漏，后面的 0 命中就没有意义"

    def test_format_redacted_traceback_has_no_secret_and_keeps_the_frames(self) -> None:
        try:
            self._raise_with_locals()
        except _FakeStatusError as exc:
            rendered = format_redacted_traceback(exc)

        assert _hits(rendered) == 0
        # 非敏感部分：异常类名、抛出位置、状态码全在
        assert "_FakeStatusError" in rendered
        assert "_raise_with_locals" in rendered
        assert "_upstream_call" in rendered
        assert "Error code: 401" in rendered
        assert "Traceback (most recent call last)" in rendered

    def test_stdlib_traceback_carries_no_local_variables(self) -> None:
        """选 stdlib 渲染的理由：它压根不打局部变量，脱敏之外还少一整类泄漏面"""
        try:
            self._raise_with_locals()
        except _FakeStatusError as exc:
            rendered = format_redacted_traceback(exc)
        # loguru 诊断行用 `└` / `│` 画树来标注取值；stdlib 的 traceback 里这两个字符不会出现
        assert "└" not in rendered
        assert "│" not in rendered

    def test_exception_chain_is_included_and_redacted(self) -> None:
        try:
            try:
                raise ValueError(f"inner: {PLATFORM_SK_KEY}")
            except ValueError as inner:
                raise _FakeStatusError(f"Error code: 502 - {RELAY_THIRD_PARTY_KEY}", 502) from inner
        except _FakeStatusError as exc:
            rendered = format_redacted_traceback(exc)

        assert _hits(rendered) == 0
        assert "ValueError" in rendered and "_FakeStatusError" in rendered


# ============================================================
# 3. 落盘：prompts_error/*.json 与文本日志
# ============================================================


@pytest.mark.usefixtures("platform_config")
class TestErrorLogOnDisk:
    """`prompts_error/*.json` 会被 `/deployment/backup` 打包带走，也会被 `log_chat_test` 读回"""

    async def test_json_error_log_has_no_credential_but_keeps_the_reason(self, tmp_path) -> None:
        response = OpenAIErrResponse.create_from_exception(
            _FakeStatusError(UPSTREAM_401_ECHOES_HEADER, 401),
            use_model="gpt-4o",
            stream_mode=False,
            messages=[{"role": "user", "content": "hi"}],
        )
        path = tmp_path / "err.json"
        await response.save_log(log_path=path, messages=[{"role": "user", "content": "hi"}], message_cnt=1)

        raw_bytes = path.read_text(encoding="utf-8")
        assert _hits(raw_bytes) == 0

        payload = json.loads(raw_bytes)
        assert payload["error"]["type"] == "_FakeStatusError"
        assert "Error code: 401" in payload["error"]["message"]
        assert "invalid_api_key" in payload["error"]["message"]
        assert payload["model"] == "gpt-4o"

    async def test_text_error_log_has_no_credential(self, tmp_path) -> None:
        """`save_log` 按后缀选形态，`.log` 走文本分支——两条分支都要钉"""
        response = OpenAIErrResponse.create_from_exception(
            _FakeStatusError(UPSTREAM_400_ECHOES_URL, 400),
            use_model="self-hosted-llm",
            stream_mode=True,
            messages=[{"role": "user", "content": "hi"}],
        )
        path = tmp_path / "err.log"
        await response.save_log(log_path=path, messages=[{"role": "user", "content": "hi"}], message_cnt=1)

        text = path.read_text(encoding="utf-8")
        assert _hits(text) == 0
        assert "400 Bad Request" in text
        assert "失败原因" in text

    async def test_without_the_redaction_the_file_really_leaks(self, tmp_path) -> None:
        """对照组：直接构造一个未脱敏的实例落盘，证明这条断言测得出差异"""
        leaky = OpenAIErrResponse(
            error_type="_FakeStatusError",
            error_msg=UPSTREAM_401_ECHOES_HEADER,  # 绕过 create_from_exception
            response_content="",
            thought_chain="",
            messages=[],
            message_cnt=0,
            token_consumption=0,
            token_input=0,
            token_output=0,
            use_model="gpt-4o",
            speed_tokens_per_second=0,
            first_token_cost_ms=0,
            generation_time_ms=0,
            stream_mode=False,
        )
        path = tmp_path / "leaky.json"
        await leaky.save_log(log_path=path, messages=[], message_cnt=0)
        assert _hits(path.read_text(encoding="utf-8")) > 0

    async def test_extra_secrets_reach_the_disk_form(self, tmp_path) -> None:
        """覆盖层里的 Key 不在配置树里，只能由调用方显式传进来

        这里刻意用**无形态**的取值：换成 `sk-…` 的话形态正则会顺手把它遮掉，
        于是「调用方传没传 extras」这件事就断言不出来了。
        """
        override_key = "0ffe1a2b3c4d5e6f70819293a4b5c6d7"
        assert redact_upstream_text(override_key, frozenset()) == override_key, "前提失效：这个哨兵不该被形态正则命中"
        response = OpenAIErrResponse.create_from_exception(
            _FakeStatusError(f"Error code: 401 - key={override_key}", 401),
            use_model="m",
            stream_mode=False,
            messages=[],
            secret_values=collect_llm_secret_values(override_key),
        )
        path = tmp_path / "err.json"
        await response.save_log(log_path=path, messages=[], message_cnt=0)
        assert override_key not in path.read_text(encoding="utf-8")
        assert "Error code: 401" in json.loads(path.read_text(encoding="utf-8"))["error"]["message"]


# ============================================================
# 4. 全链路：gen_openai_chat_response 真的接上了
# ============================================================


def _install_failing_openai(monkeypatch, error: Exception) -> None:
    """只替换 openai SDK 客户端与共享连接池，产品自己的错误处理原样跑"""

    class _Completions:
        async def create(self, **_kwargs):
            raise error

    class _FakeClient:
        def __init__(self, **_kwargs) -> None:
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeClient)
    monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: object())


@pytest.mark.usefixtures("platform_config")
class TestGenChatResponseWiring:
    """纯函数用例证明不了「脱敏被接在了请求路径上」（§2.7）"""

    async def test_failure_writes_a_clean_file_and_a_clean_log(self, monkeypatch, tmp_path, log_sink) -> None:
        _install_failing_openai(monkeypatch, _FakeStatusError(UPSTREAM_401_ECHOES_HEADER, 401))
        err_path = tmp_path / "chat_err.json"

        with pytest.raises(_FakeStatusError):
            await gen_openai_chat_response(
                model="gpt-4o",
                messages=[{"role": "user", "content": "hi"}],
                api_key=PLATFORM_SK_KEY,
                base_url=PLATFORM_BASE_URL,
                error_log_path=err_path,
            )

        assert err_path.is_file()
        assert _hits(err_path.read_text(encoding="utf-8")) == 0

        text = _sink_text(log_sink)
        assert _hits(text) == 0
        # 非敏感部分：异常类名与上游文案都还在，否则这条日志没有排障价值
        assert "_FakeStatusError" in text
        assert "Error code: 401" in text
        assert "invalid_api_key" in text

    async def test_the_log_record_no_longer_carries_an_exception_object(self, monkeypatch, log_sink) -> None:
        """`logger.exception` 会把异常对象挂在 record 上，而 `core/logger.format_log_entry`
        会把它的 `str()` 与完整 traceback 单独抄进内存日志环 -> `/logs` API -> 运维 Bot 推送。
        那三处都不经过我们的 f-string，只能靠「不再产生 exception record」堵住。
        """
        _install_failing_openai(monkeypatch, _FakeStatusError(UPSTREAM_502_RELAY_KEY, 502))

        with pytest.raises(_FakeStatusError):
            await gen_openai_chat_response(model="m", messages=[], api_key=PLATFORM_SK_KEY)

        agent_records = [
            message.record
            for message in log_sink
            if "OpenAI请求失败" in str(message.record["message"])
        ]
        assert agent_records, "没抓到那条错误日志，用例本身失效了"
        for record in agent_records:
            assert record["exception"] is None

    async def test_api_key_passed_only_at_call_time_is_still_redacted(self, monkeypatch, tmp_path, log_sink) -> None:
        """调用方直接传进来的 Key（连通性测试、CC 预设、覆盖层）不在配置树里

        哨兵刻意**无形态**：`sk-…` 的话形态正则会顺手遮掉它，这条就断言不出
        「错误分支到底有没有把本次的 `api_key` 收进凭据集合」。
        """
        call_time_key = "b7d1c9e35f0a48261d3e5f7a9b0c2d4e"
        assert redact_upstream_text(call_time_key, frozenset()) == call_time_key, "前提失效：这个哨兵不该被形态正则命中"

        _install_failing_openai(
            monkeypatch,
            _FakeStatusError(f"Error code: 401 - Incorrect API key provided: {call_time_key}", 401),
        )
        err_path = tmp_path / "err.json"
        with pytest.raises(_FakeStatusError):
            await gen_openai_chat_response(
                model="m", messages=[], api_key=call_time_key, error_log_path=err_path
            )
        assert call_time_key not in err_path.read_text(encoding="utf-8")
        assert call_time_key not in _sink_text(log_sink)
        assert "Error code: 401" in err_path.read_text(encoding="utf-8")

    async def test_simplified_stream_generator_is_also_covered(self, monkeypatch, log_sink) -> None:
        """`gen_openai_chat_stream`（WebUI 实时代码生成那条）是**第二个**直连上游的出口

        它与 `gen_openai_chat_response` 各有一个 `except`，改一个漏一个就是判据分叉。
        变异测试里这条一开始是存活的——因为整份测试没有一条驱动过这个生成器（§2.7）。
        """
        _install_failing_openai(monkeypatch, _FakeStatusError(UPSTREAM_401_ECHOES_HEADER, 401))

        with pytest.raises(_FakeStatusError):
            async for _chunk in openai_module.gen_openai_chat_stream(
                model="gpt-4o",
                messages=[],
                api_key=PLATFORM_SK_KEY,
                base_url=PLATFORM_BASE_URL,
            ):
                pass

        text = _sink_text(log_sink)
        assert "流式生成过程中出错" in text, "没抓到那条错误日志，用例本身失效了"
        assert _hits(text) == 0
        assert "Error code: 401" in text

    async def test_success_path_is_untouched(self, monkeypatch, tmp_path) -> None:
        """成功响应逐字不变：脱敏只挂在失败分支上，不该影响正常回合"""

        class _Completions:
            async def create(self, **_kwargs):
                message = SimpleNamespace(content="hello", reasoning_content="")
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=message)],
                    usage=SimpleNamespace(total_tokens=10, prompt_tokens=5, completion_tokens=5),
                )

        class _FakeClient:
            def __init__(self, **_kwargs) -> None:
                self.chat = SimpleNamespace(completions=_Completions())

        monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeClient)
        monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: object())

        log_path = tmp_path / "ok.json"
        response = await gen_openai_chat_response(
            model="m", messages=[], api_key=PLATFORM_SK_KEY, log_path=log_path
        )
        assert response.response_content == "hello"
        assert "error" not in json.loads(log_path.read_text(encoding="utf-8"))


# ============================================================
# 5. send_agent_request 的三个出口（其中一个租户可见）
# ============================================================


def _group(name: str, model: str, api_key: str, *, proxy: str = "") -> ModelConfigGroup:
    return ModelConfigGroup(
        GROUP_NAME=name,
        CHAT_MODEL=model,
        BASE_URL=PLATFORM_BASE_URL,
        API_KEY=api_key,
        CHAT_PROXY=proxy,
    )


def _make_config(group: ModelConfigGroup, *, max_retries: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        MODEL_GROUPS={"p": group},
        USE_MODEL_GROUP="p",
        DEBUG_MIGRATION_MODEL_GROUP="",
        FALLBACK_MODEL_GROUP="p",
        SAVE_PROMPTS_LOG=False,
        AI_CHAT_LLM_API_MAX_RETRIES=max_retries,
        AI_REQUEST_STREAM_MODE=False,
        AI_GENERATE_TIMEOUT=180,
        AI_STREAM_FIRST_TOKEN_TIMEOUT=30,
    )


@pytest.fixture()
def _isolated_log_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(run_agent_module, "PROMPT_LOG_DIR", str(tmp_path / "prompts"))
    monkeypatch.setattr(run_agent_module, "PROMPT_ERROR_LOG_DIR", str(tmp_path / "prompts_error"))


@pytest.mark.usefixtures("_isolated_log_dirs", "platform_config")
class TestSendAgentRequestExits:
    async def test_retry_errors_and_runtime_summary_are_clean(self, monkeypatch, log_sink) -> None:
        """`retry_errors` 落库进 `DBExecCode.extra_data`，`error_summary` 走 SSE 推给**租户**"""
        summaries: List[str] = []

        async def _boom(**_kwargs):
            raise _FakeStatusError(UPSTREAM_401_ECHOES_HEADER, 401)

        async def _on_retry(_index: int, _total: int, _model: str, error_summary: str) -> None:
            summaries.append(error_summary)

        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", _boom)
        group = _group("p", "gpt-4o", PLATFORM_SK_KEY, proxy=PLATFORM_PROXY)

        with pytest.raises(AllLLMRequestsFailedError):
            await send_agent_request(
                [OpenAIChatMessage.from_text("user", "hi")],
                _make_config(group),  # type: ignore[arg-type]
                on_llm_retry=_on_retry,
            )

        assert summaries, "没触发重试回调，用例失效"
        assert _hits("".join(summaries)) == 0
        assert "Error code: 401" in summaries[0]
        assert _hits(_sink_text(log_sink)) == 0
        assert "LLM 请求失败" in _sink_text(log_sink)

    async def test_proxy_credentials_are_redacted_too(self, monkeypatch) -> None:
        """代理串里的口令同样是凭据；它由 `CHAT_PROXY` 承载，配置树按字段名收得到"""
        summaries: List[str] = []

        async def _boom(**_kwargs):
            raise _FakeStatusError(f"ProxyError: failed to connect via {PLATFORM_PROXY}", 502)

        async def _on_retry(_i: int, _t: int, _m: str, error_summary: str) -> None:
            summaries.append(error_summary)

        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", _boom)
        group = _group("p", "gpt-4o", PLATFORM_SK_KEY, proxy=PLATFORM_PROXY)

        with pytest.raises(AllLLMRequestsFailedError):
            await send_agent_request(
                [OpenAIChatMessage.from_text("user", "hi")],
                _make_config(group),  # type: ignore[arg-type]
                on_llm_retry=_on_retry,
            )
        assert "proxysecret9988" not in "".join(summaries)
        assert "ProxyError" in summaries[0]

    async def test_classification_still_sees_the_raw_text(self, monkeypatch) -> None:
        """脱敏不能吃掉分类判据：能力不匹配那一类必须照样被认出来

        判据是「模态词 + 拒绝词」的合取，而这段原文里恰好也带着一段被回显的凭据。
        把 `classify_llm_failure` 的入参换成脱敏后的字符串，这条会红。
        """
        error = _FakeStatusError(UPSTREAM_VISION_400_WITH_KEY, 400)
        assert classify_llm_failure(error, request_has_image=True) is LLMFailureKind.VISION_UNSUPPORTED

        redacted = redact_upstream_text(str(error))
        assert _hits(redacted) == 0
        assert "当前模型不支持该能力：vision" in redacted


# ============================================================
# 6. 接线契约（AST）：把调用点改回旧写法必须红
# ============================================================


def _module_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_node(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"未找到函数 {name}")


class TestTheRedactionIsActuallyWiredIn:
    """§2.7：零件全对、调用点没了，测试会全绿

    这一组按**实参**判，不按「函数名出现在源码里」判——后者只要文件里有一次 import
    就恒成立（以前就踩过这个坑）。
    """

    def test_gen_chat_response_does_not_use_logger_exception(self) -> None:
        """按 AST 的 Call 节点判，不按源码文本判

        这个文件的 docstring 里就写着 `logger.exception` 四个字（在讲这条缺陷的由来），
        按文本判会恒不成立——以前的一次检查正是这样被一句局部 import 骗过去的。
        """
        tree = ast.parse(_module_source(_AGENT_DIR / "openai.py"))
        offenders = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "exception"
            and getattr(node.func.value, "id", "") == "logger"
        ]
        assert offenders == [], (
            "loguru 的 diagnose 会把局部变量（含 messages 整份 prompt）渲染进日志，"
            "而那份文本不经过任何 f-string"
        )

    def test_create_from_exception_redacts_the_error_message(self) -> None:
        tree = ast.parse(_module_source(_AGENT_DIR / "openai.py"))
        node = _function_node(tree, "create_from_exception")
        call = next(
            keyword.value
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            for keyword in child.keywords
            if keyword.arg == "error_msg"
        )
        assert isinstance(call, ast.Call), "error_msg 必须是一次脱敏调用，不能是裸 str(e)"
        assert getattr(call.func, "id", "") == "redact_upstream_text"

    def test_openai_error_branch_passes_the_call_time_credentials(self) -> None:
        """只收集配置树等于漏掉「调用方直接传 Key」的那条路"""
        tree = ast.parse(_module_source(_AGENT_DIR / "openai.py"))
        node = _function_node(tree, "gen_openai_chat_response")
        collect_calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and getattr(child.func, "id", "") == "collect_llm_secret_values"
        ]
        assert collect_calls, "错误分支没有收集本次请求的凭据"
        arg_names = {getattr(arg, "id", "") for call in collect_calls for arg in call.args}
        assert "api_key" in arg_names

    def test_run_agent_stores_the_redacted_text_not_the_raw_one(self) -> None:
        tree = ast.parse(_module_source(_AGENT_DIR / "run_agent.py"))
        node = _function_node(tree, "send_agent_request")
        appends = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "append"
            and getattr(child.func.value, "id", "") == "retry_errors"
        ]
        assert len(appends) == 1
        assert getattr(appends[0].args[0], "id", "") == "safe_error"

    def test_run_agent_summary_is_built_from_the_redacted_text(self) -> None:
        tree = ast.parse(_module_source(_AGENT_DIR / "run_agent.py"))
        node = _function_node(tree, "send_agent_request")
        summarize = next(
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and getattr(child.func, "id", "") == "_summarize_runtime_text"
        )
        assert getattr(summarize.args[0], "id", "") == "safe_error"

    def test_classification_input_is_the_exception_itself(self) -> None:
        """反向契约：分类必须吃原始异常，喂脱敏字符串会让「模态词 + 拒绝词」判据失灵"""
        tree = ast.parse(_module_source(_AGENT_DIR / "run_agent.py"))
        node = _function_node(tree, "send_agent_request")
        classify = next(
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and getattr(child.func, "id", "") == "classify_llm_failure"
        )
        assert getattr(classify.args[0], "id", "") == "e"

    def test_debug_command_uses_the_shared_helper(self) -> None:
        """`log_chat_test` 的输出直接发回 IM 频道；旧写法只认本组的 Key，
        且 `API_KEY` 为空时 `str.replace("", ...)` 会把报错打成乱码"""
        path = Path(run_agent_module.__file__).parents[1] / "command" / "built_in" / "debug.py"
        source = _module_source(path)
        assert "redact_upstream_text(" in source
        assert '.replace(model_group.API_KEY' not in source


@pytest.mark.usefixtures("platform_config")
class TestEnhancedMemoryRecallExit:
    """第五个出口：`templates/history.py` 的增强记忆检索规划

    它与 `run_agent` 一样直连上游（`gen_openai_chat_response` 打完自己那条已脱敏的日志之后
    `raise` 的是**原始**异常），而它用的正是平台方的模型组
    （`MEMORY_ENHANCED_RETRIEVAL_MODEL_GROUP` 缺省回落到 `USE_MODEL_GROUP`）。
    它还挂在主对话链路的正前方 —— 每一条消息都会走一次，一把坏 Key 就是持续写入。

    复核时实测过：修复前这条用例里哨兵在 sink 中命中 1 次。
    """

    @pytest.fixture()
    def _enhanced_recall_on(self, monkeypatch):
        from nekro_agent.services.agent.templates import history as history_module

        monkeypatch.setattr(config, "MEMORY_ENABLE_ENHANCED_RETRIEVAL", True)
        monkeypatch.setattr(config, "MEMORY_ENHANCED_RETRIEVAL_MODEL_GROUP", "__test_upstream_redaction__")
        monkeypatch.setattr(config, "MEMORY_ENHANCED_RETRIEVAL_TIMEOUT_SECONDS", 0)
        return history_module

    @staticmethod
    def _messages():
        from nekro_agent.models.db_chat_message import DBChatMessage

        return [
            DBChatMessage(
                content_text="上周我们说好的那件事后来怎么样了",
                sender_nickname="u",
                sender_name="u",
                sender_id="10001",
            ),
        ]

    async def test_upstream_failure_does_not_leak_into_the_warning(
        self, monkeypatch, log_sink, _enhanced_recall_on
    ) -> None:
        history_module = _enhanced_recall_on

        async def _boom(**_kwargs):
            raise _FakeStatusError(UPSTREAM_401_ECHOES_HEADER, 401)

        monkeypatch.setattr(openai_module, "gen_openai_chat_response", _boom)

        plan = await history_module._build_enhanced_memory_recall_plan(self._messages())

        assert plan is None, "降级行为必须逐字不变：拿不到规划就回退规则检索"
        text = _sink_text(log_sink)
        assert "增强记忆检索规划失败" in text, "没抓到那条告警，用例本身失效了"
        assert _hits(text) == 0
        # 另一个方向：排障信息还在
        assert "Error code: 401" in text
        assert "invalid_api_key" in text

    async def test_relay_key_of_unknown_shape_source_is_also_covered(
        self, monkeypatch, log_sink, _enhanced_recall_on
    ) -> None:
        """中转站回显的第三方 Key 不在我们的配置树里，只有形态那条判据抓得到"""
        history_module = _enhanced_recall_on

        async def _boom(**_kwargs):
            raise _FakeStatusError(UPSTREAM_502_RELAY_KEY, 502)

        monkeypatch.setattr(openai_module, "gen_openai_chat_response", _boom)

        assert await history_module._build_enhanced_memory_recall_plan(self._messages()) is None
        text = _sink_text(log_sink)
        assert "增强记忆检索规划失败" in text
        assert _hits(text) == 0
        assert "relay_error" in text

    async def test_validation_error_branch_is_redacted_too(
        self, monkeypatch, log_sink, _enhanced_recall_on
    ) -> None:
        """解析失败是**另一个** `except`，它的文案里带着模型返回的原文

        pydantic 的 `ValidationError` 会把 `input_value=...` 抄进消息。上游网关用 200 回一段
        错误体（而不是 4xx）时，那段原文就是这条日志的内容——两个 `except` 改一个漏一个
        就是判据分叉（§2.10）。

        取值刻意用**裸哨兵**：pydantic 对超过约 50 字符的 `input_value` 会**掐掉中间一段**，
        加个前缀就再也拼不出完整哨兵，于是「0 命中」在没有修复时同样成立 —— 那是空断言。
        下面第一行就是钉住这件事的前提断言（§2.11）。
        """
        history_module = _enhanced_recall_on

        try:
            MemoryRecallPlan.model_validate({"queries": PLATFORM_SK_KEY})
        except ValidationError as probe:
            assert _hits(str(probe), PLATFORM_SK_KEY) == 1, "前提失效：pydantic 把哨兵截断了，这条用例测不出差异"
        else:
            pytest.fail("前提失效：这份输入本该触发 ValidationError")

        async def _bad_json(**_kwargs):
            return SimpleNamespace(response_content=json.dumps({"queries": PLATFORM_SK_KEY}))

        monkeypatch.setattr(openai_module, "gen_openai_chat_response", _bad_json)

        assert await history_module._build_enhanced_memory_recall_plan(self._messages()) is None
        text = _sink_text(log_sink)
        assert "增强记忆检索规划解析失败" in text, "没走到解析失败那一档，用例本身失效了"
        assert _hits(text) == 0
        assert "queries" in text, "字段名要留着，否则不知道模型哪里返错了"

    async def test_missing_model_group_still_degrades_cleanly(
        self, monkeypatch, log_sink, _enhanced_recall_on
    ) -> None:
        """异常也可能发生在**拿到模型组之前**（配置里没有这个组）

        脱敏助手此时收不到本次的 `API_KEY`，但不能因此抛 `UnboundLocalError` 把
        「降级」变成「炸掉主链路」。
        """
        history_module = _enhanced_recall_on
        monkeypatch.setattr(config, "MEMORY_ENHANCED_RETRIEVAL_MODEL_GROUP", "__no_such_group__")

        assert await history_module._build_enhanced_memory_recall_plan(self._messages()) is None
        text = _sink_text(log_sink)
        assert "增强记忆检索规划失败" in text
        assert "UnboundLocalError" not in text
        assert "__no_such_group__" in text, "模型组名不是凭据，得留着才知道是哪个组配错了"


@pytest.mark.usefixtures("platform_config")
class TestExtraBodyParseFailure:
    """`EXTRA_BODY` 是模型组的自由文本字段，解析失败时原文会被打进日志

    某些上游要求把凭据写进请求体（自建网关的签名串、`{"api_key": ...}`），
    所以这条日志的两个插值都要过脱敏。
    """

    def test_malformed_body_is_logged_without_the_credential(self, log_sink) -> None:
        # 刻意选**无形态**的取值 + 不在关键词表里的键名：这样只有「按值」那条判据能抓到它，
        # 断言才真的在测「这里到底接没接脱敏」，而不是在测形态正则
        broken = f'{{"custom_auth_blob": "{PLATFORM_OPAQUE_KEY}", "vendor_flag": true'
        from nekro_agent.tools.redaction import redact_credentials

        assert PLATFORM_OPAQUE_KEY in redact_credentials(broken), "前提失效：这段文本本该躲过形态正则"

        assert openai_module.parse_extra_body(broken, source_hint="group=__test__") is None

        text = _sink_text(log_sink)
        assert "解析 EXTRA_BODY 失败" in text, "没抓到那条日志，用例本身失效了"
        assert _hits(text, PLATFORM_OPAQUE_KEY) == 0
        # 非敏感部分：来源提示与「哪个字段没闭合」的线索都还在
        assert "group=__test__" in text
        assert "vendor_flag" in text

    def test_valid_body_is_parsed_unchanged(self) -> None:
        assert openai_module.parse_extra_body('{"a": 1}') == {"a": 1}
        assert openai_module.parse_extra_body(None) is None
        assert openai_module.parse_extra_body("") is None


@pytest.mark.usefixtures("platform_config")
class TestStackTraceSurvivesTheSwitch:
    """换掉 `logger.exception` 的代价必须**只有**诊断行

    否则「脱敏」就变成了「顺手把堆栈也吞了」——那是把排障废掉，
    而「凭据 0 命中」对这种实现同样成立（§2.11 的空断言形态）。
    """

    async def test_the_error_log_still_carries_the_stack(self, monkeypatch, log_sink) -> None:
        _install_failing_openai(monkeypatch, _FakeStatusError(UPSTREAM_401_ECHOES_HEADER, 401))

        with pytest.raises(_FakeStatusError):
            await gen_openai_chat_response(model="m", messages=[], api_key=PLATFORM_SK_KEY)

        text = _sink_text(log_sink)
        assert "Traceback (most recent call last)" in text, "堆栈没了：这次改动把排障能力换掉了"
        assert "gen_openai_chat_response" in text, "抛错所在的帧必须还在"
        assert _hits(text) == 0


@pytest.mark.usefixtures("platform_config")
class TestEmbeddingUpstreamExit:
    """第二个直连上游的**通道**：embedding

    `services/memory/embedding_service.py` 的重试日志里有个 `detail=`，抄的就是
    **上游响应体原文**（`e.response.text[:300]`）—— 而「4xx 体里回显 Key」正是 #39 的前提。
    chat 那条链路修完、embedding 这条不修，就是「同一类资源的第二个出口」漏在原地。

    知识库索引与记忆沉淀都会成批打 embedding，一把坏 Key = 每条文本写一次日志。
    """

    @staticmethod
    def _http_status_error(body: str, status: int) -> Exception:
        import httpx

        request = httpx.Request("POST", "https://api.internal.example/v1/embeddings")
        response = httpx.Response(status, text=body, request=request)
        return httpx.HTTPStatusError("Client error", request=request, response=response)

    async def test_upstream_body_is_redacted_before_it_reaches_the_log(self, monkeypatch, log_sink) -> None:
        embedding_module = import_module("nekro_agent.services.memory.embedding_service")

        async def _boom(**_kwargs):
            raise self._http_status_error(UPSTREAM_401_ECHOES_HEADER, 401)

        monkeypatch.setattr(embedding_module, "gen_openai_embeddings", _boom)
        service = embedding_module.EmbeddingService(model_group="__test_upstream_redaction__", dimension=8)

        with pytest.raises(Exception, match="Client error"):
            await service.embed_text("hello")

        text = _sink_text(log_sink)
        assert "Embedding 请求失败" in text, "没抓到那条日志，用例本身失效了"
        assert _hits(text) == 0
        # 另一个方向：状态码、模型名、上游文案都还在
        assert "status=401" in text
        assert "invalid_api_key" in text

    async def test_non_http_failure_is_redacted_too(self, monkeypatch, log_sink) -> None:
        """非 HTTPStatusError 走的是**另一个** `except`——两档都要脱，改一档漏一档

        `MAX_RETRIES` 改成 1：这一档没有「4xx 立刻放弃」的短路，默认 3 次会真的 sleep，
        而验收明令禁止 sleep 计时的用例。
        """
        embedding_module = import_module("nekro_agent.services.memory.embedding_service")

        monkeypatch.setattr(embedding_module, "MAX_RETRIES", 1)

        async def _boom(**_kwargs):
            raise RuntimeError(f"ProxyError: upstream handshake rejected key {PLATFORM_SK_KEY}")

        monkeypatch.setattr(embedding_module, "gen_openai_embeddings", _boom)
        service = embedding_module.EmbeddingService(model_group="__test_upstream_redaction__", dimension=8)

        with pytest.raises(RuntimeError):
            await service.embed_text("hello")

        text = _sink_text(log_sink)
        assert "Embedding 请求失败" in text
        assert _hits(text) == 0
        assert "ProxyError" in text


class TestEmptyApiKeyDoesNotCorruptTheMessage:
    """自建上游不需要鉴权时 `API_KEY` 是空串，这是 `str.replace("", x)` 的经典陷阱

    `"abc".replace("", "[K]")` == `"[K]a[K]b[K]c[K]"`：报错会变成一串占位符。
    共享判据有最短长度门槛，不会这样。
    """

    def test_blank_key_leaves_the_text_verbatim(self) -> None:
        text = "Error code: 500 - upstream internal error"
        assert redact_upstream_text(text, collect_llm_secret_values("", None)) == text

    def test_the_old_implementation_would_have_corrupted_it(self) -> None:
        """把旧写法钉在测试里，免得后人「简化」回去"""
        assert "Error code: 500".replace("", "[API_KEY]") != "Error code: 500"
