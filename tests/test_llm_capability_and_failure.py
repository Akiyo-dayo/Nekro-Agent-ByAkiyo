"""LLM 请求失败的可修复性与可辨识性回归测试

由来是一次真实事故：一个模型组的 `ENABLE_VISION` 被 WebUI 的默认值
带成了 true，而它接的 `deepseek-v4-pro` 不吃图片。于是——

1. 历史图片取自**整个上下文窗口**，群里只要 30 分钟内有过图，连纯文字消息也会被拖下水；
2. 上游回 400「当前模型不支持该能力：vision」，重试链路对它零分类，原样再发两遍；
3. `FALLBACK_MODEL_GROUP` 恰好配成了主模型组自己，所谓"备用"等于把同一个必然失败的请求
   发了三遍；
4. 三次都挂之后每一轮往真实群里发一条「与 LLM 通信出错啦」，三个小时刷了十几条。

同期还有第二类：上游 2xx 但正文为空。其中一支（原生思考模型把整段输出连同代码块一起走了
`reasoning_content`）**是我们这边的缺陷**，答案完全可解析却被判空丢弃；另一支
（`completion_tokens=0` 而 `reasoning_tokens=1116`）是供应商行为，那就必须留下可辨识的日志
与用户可见的回执，而不是静默装死。

这里保护的契约因此是四条：
- 能力声明（用的是上游既有的 `ENABLE_VISION`）在**每一次尝试**上都生效，而不只在主模型组上；
- 「能力不匹配」这一类错误能被认出来，且认出来之后请求形态**必须改变**（换模型或剥图），
  不能原样重发；
- 正文为空的两支各自可辨识，能被救回来的救回来；
- 每一条失败路径都有用户可见的回执，且回执本身有冷却、不会变成新的洪水。

全部确定性：不起真网络、不靠时间流逝（TTL 与冷却都注入时间戳）、不做随机 fuzz。
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Set

import pytest

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.services.agent import openai as openai_module
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.creator import ContentSegment, OpenAIChatMessage
from nekro_agent.services.agent.failure_feedback import (
    NOTICE_LLM_EMPTY,
    NOTICE_LLM_FAILED,
    ChatNoticeThrottle,
    describe_llm_failure,
    failure_notice_throttle,
)
from nekro_agent.services.agent.model_capability import (
    IMAGE_PLACEHOLDER_TEXT,
    VISION_MARK_TTL_SECONDS,
    LLMFailureKind,
    classify_llm_failure,
    declares_vision,
    has_image_segments,
    is_capability_marked,
    looks_like_capability_rejection,
    note_capability_ok,
    note_capability_unsupported,
    reset_capability_marks,
    strip_image_segments,
    supports_vision,
)
from nekro_agent.services.agent.openai import (
    EmptyLLMResponseError,
    OpenAIErrResponse,
    OpenAIResponse,
    _salvage_reasoning_only_output,
    gen_openai_chat_response,
)
from nekro_agent.services.agent.resolver import parse_chat_response
from nekro_agent.services.agent.run_agent import (
    AgentRoundUnfinishedError,
    AllLLMRequestsFailedError,
    build_attempt_plan,
    pick_vision_repair_group,
    resolve_fallback_model_group,
    resolve_primary_model_group,
    send_agent_request,
)
from nekro_agent.services.agent.templates import history as history_module

# ============================================================
# 现场逐字取回来的上游报错文案（app.log / prompts_error）
# ============================================================

UPSTREAM_VISION_400 = (
    "Error code: 400 - {'error': {'message': '当前模型不支持该能力：vision', "
    "'type': 'invalid_request_error', 'code': 'model_not_support'}}"
)
UPSTREAM_OPENAI_VISION_400 = (
    "Error code: 400 - {'error': {'message': 'Invalid content type. image_url is only supported by certain models.', "
    "'type': 'invalid_request_error'}}"
)
UPSTREAM_PLAIN_VISION_400 = "Error code: 400 - This model does not support image input"


class _FakeStatusError(Exception):
    """模拟 openai SDK 的 `APIStatusError`：状态码挂在异常属性上"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _group(
    name: str,
    model: str,
    *,
    vision: bool = False,
    base_url: str = "https://upstream.example/v1",
) -> ModelConfigGroup:
    return ModelConfigGroup(
        GROUP_NAME=name,
        CHAT_MODEL=model,
        BASE_URL=base_url,
        API_KEY="sk-test",
        ENABLE_VISION=vision,
    )


def _text_message(text: str = "hello") -> OpenAIChatMessage:
    return OpenAIChatMessage.from_text("user", text)


def _image_message(count: int = 1) -> OpenAIChatMessage:
    msg = OpenAIChatMessage.create_empty("user")
    for index in range(count):
        msg.add(ContentSegment.text_content(f"<code | image path=\"pic{index}.jpg\">"))
        msg.add(ContentSegment.image_content(f"data:image/jpeg;base64,AAAA{index}"))
        msg.add(ContentSegment.text_content("</code | image>"))
    return msg


@pytest.fixture(autouse=True)
def _clean_process_state():
    """负判据与回执冷却都是进程内状态，用例之间必须互不影响"""
    reset_capability_marks()
    failure_notice_throttle.reset()
    yield
    reset_capability_marks()
    failure_notice_throttle.reset()


# ============================================================
# 1. 失败分类
# ============================================================


class TestFailureClassification:
    """把「能力不匹配」从其余失败里分出来

    判据是三个条件的**合取**：本次请求真的带了图 + 客户端错误码 + 文案同时含模态词与拒绝词。
    少任何一条都判 OTHER，因为误判的代价是把一个本来能看图的模型剥成瞎子。
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("当前模型不支持该能力：vision", True),
            ("Invalid content type. image_url is only supported by certain models.", True),
            ("This model does not support image input", True),
            ("该模型不支持多模态输入", True),
            ("模型无法处理图片内容", True),
            # 只有模态词没有拒绝词：图挂了不是模型不支持
            ("Invalid image url: connection reset", False),
            ("failed to download image", False),
            # 只有拒绝词没有模态词
            ("this parameter is not supported: top_k", False),
            ("不支持该 API 版本", False),
            ("Connection error.", False),
            ("", False),
        ],
    )
    def test_rejection_text_matcher(self, text: str, expected: bool) -> None:
        assert looks_like_capability_rejection(text) is expected

    @pytest.mark.parametrize(
        ("exc", "has_image", "expected"),
        [
            # 现场那三种文案，带图 + 400 → 能力不匹配
            (_FakeStatusError(UPSTREAM_VISION_400, 400), True, LLMFailureKind.VISION_UNSUPPORTED),
            (_FakeStatusError(UPSTREAM_OPENAI_VISION_400, 400), True, LLMFailureKind.VISION_UNSUPPORTED),
            (_FakeStatusError(UPSTREAM_PLAIN_VISION_400, 400), True, LLMFailureKind.VISION_UNSUPPORTED),
            # 状态码没挂在异常上时，从文案里兜底取一次
            (Exception(UPSTREAM_VISION_400), True, LLMFailureKind.VISION_UNSUPPORTED),
            # 415 / 422 同属「请求内容不合法」
            (_FakeStatusError(UPSTREAM_PLAIN_VISION_400, 415), True, LLMFailureKind.VISION_UNSUPPORTED),
            (_FakeStatusError(UPSTREAM_PLAIN_VISION_400, 422), True, LLMFailureKind.VISION_UNSUPPORTED),
            # 剥过图的请求不可能再因为图被拒 —— 这条前置条件是误判面的主要约束
            (_FakeStatusError(UPSTREAM_VISION_400, 400), False, LLMFailureKind.OTHER),
            # 凭据与限流：剥图既不会让它变好，也不该污染能力判据
            (_FakeStatusError(UPSTREAM_VISION_400, 401), True, LLMFailureKind.OTHER),
            (_FakeStatusError(UPSTREAM_VISION_400, 403), True, LLMFailureKind.OTHER),
            (_FakeStatusError("rate limit", 429), True, LLMFailureKind.OTHER),
            (_FakeStatusError("internal error", 500), True, LLMFailureKind.OTHER),
            # 400 但不是能力问题
            (_FakeStatusError("Error code: 400 - invalid api key", 400), True, LLMFailureKind.OTHER),
            (Exception("Connection error."), True, LLMFailureKind.OTHER),
            # 空响应自成一类
            (EmptyLLMResponseError("非流式响应内容为空"), False, LLMFailureKind.EMPTY_RESPONSE),
            (EmptyLLMResponseError("非流式响应内容为空"), True, LLMFailureKind.EMPTY_RESPONSE),
        ],
    )
    def test_classify(self, exc: Exception, has_image: bool, expected: LLMFailureKind) -> None:
        assert classify_llm_failure(exc, request_has_image=has_image) is expected


# ============================================================
# 2. 能力声明 + 运行时观测
# ============================================================


class TestVisionCapabilityResolution:
    """声明为准，被上游打过脸则在 TTL 内暂时降级，且必须能自愈"""

    def test_undeclared_is_treated_as_unsupported(self) -> None:
        """未声明当作不支持：两种错法的代价不对称——判错成"看不到"只少一路信息，
        判错成"能看"会让整轮对话连同纯文字消息一起失败"""
        assert declares_vision(_group("g", "m", vision=False)) is False
        assert supports_vision(_group("g", "m", vision=False)) is False
        assert supports_vision(_group("g", "m", vision=True)) is True

    def test_mark_downgrades_then_expires(self) -> None:
        group = _group("g", "m", vision=True)
        assert supports_vision(group, now=1000.0) is True

        note_capability_unsupported(group, now=1000.0)
        assert is_capability_marked(group, now=1000.0) is True
        assert supports_vision(group, now=1000.0) is False
        assert supports_vision(group, now=1000.0 + VISION_MARK_TTL_SECONDS - 1) is False

        # TTL 到点自动恢复：运维改完配置/换完模型不需要重启进程
        assert supports_vision(group, now=1000.0 + VISION_MARK_TTL_SECONDS + 1) is True

    def test_success_clears_the_mark(self) -> None:
        """观测必须双向，否则「学到的东西」只会积累成偏见"""
        group = _group("g", "m", vision=True)
        note_capability_unsupported(group, now=1000.0)
        assert supports_vision(group, now=1001.0) is False
        note_capability_ok(group, now=1001.0)
        assert supports_vision(group, now=1001.0) is True

    def test_mark_is_scoped_to_base_url_and_model(self) -> None:
        """同名模型挂在不同上游是两回事；同一上游的另一个模型也不该被连坐"""
        a = _group("a", "same-model", vision=True, base_url="https://one.example/v1")
        b = _group("b", "same-model", vision=True, base_url="https://two.example/v1")
        c = _group("c", "other-model", vision=True, base_url="https://one.example/v1")
        note_capability_unsupported(a, now=1000.0)
        assert supports_vision(a, now=1000.0) is False
        assert supports_vision(b, now=1000.0) is True
        assert supports_vision(c, now=1000.0) is True

    def test_declaration_off_wins_over_a_cleared_mark(self) -> None:
        group = _group("g", "m", vision=False)
        note_capability_ok(group, now=1000.0)
        assert supports_vision(group, now=1000.0) is False


# ============================================================
# 3. 按能力裁剪
# ============================================================


class TestImageTrimming:
    def test_detects_images(self) -> None:
        assert has_image_segments([_text_message()]) is False
        assert has_image_segments([_text_message(), _image_message(2)]) is True

    def test_images_become_placeholders_not_silence(self) -> None:
        """换成占位而不是删掉：模型至少要知道上下文里确实有图、而自己这次看不到"""
        messages = [_text_message("sys"), _image_message(3)]
        trimmed, removed = strip_image_segments(messages)

        assert removed == 3
        assert has_image_segments(trimmed) is False
        rendered = json.dumps([m.to_dict() for m in trimmed], ensure_ascii=False)
        assert rendered.count(IMAGE_PLACEHOLDER_TEXT) == 3
        assert "no vision capability" in IMAGE_PLACEHOLDER_TEXT

    def test_original_messages_are_not_mutated(self) -> None:
        """同一轮里 `run_agent` 还要拿原始 messages 继续拼迭代上下文，
        而下一次尝试用哪个模型组、要不要剥图是那时候才决定的"""
        original = _image_message(2)
        messages = [original]
        strip_image_segments(messages)
        assert has_image_segments([original]) is True
        assert has_image_segments(messages) is True

    def test_untouched_messages_are_passed_through_by_identity(self) -> None:
        """不含图的消息原样透传，不为几 MB 的 base64 做无谓复制"""
        plain = _text_message("plain")
        trimmed, removed = strip_image_segments([plain, _image_message(1)])
        assert removed == 1
        assert trimmed[0] is plain


# ============================================================
# 4. 尝试序列与 fallback 选择
# ============================================================


class TestAttemptPlanning:
    @pytest.mark.parametrize("max_retries", [0, 1, 2, 3, 5])
    def test_plan_matches_the_pre_change_sequence(self, max_retries: int) -> None:
        """改造前是 `use = model_group if i < N-1 else fallback`。
        没有能力不匹配发生时，尝试序列必须与那个公式逐字等价"""
        primary = _group("p", "primary")
        fallback = _group("f", "fallback")
        expected = [primary if i < max_retries - 1 else fallback for i in range(max_retries)]
        assert build_attempt_plan(primary, fallback, max_retries) == expected

    def test_missing_fallback_group_falls_back_instead_of_crashing(self) -> None:
        """改造前是 `config.MODEL_GROUPS[name]`：名字对不上就 KeyError，
        整轮对话连一条日志都没有就没了。运维改名/删组之后这是常见状态"""
        primary = _group("p", "primary")
        config = SimpleNamespace(FALLBACK_MODEL_GROUP="gone", MODEL_GROUPS={"p": primary})
        assert resolve_fallback_model_group(config, primary) is primary

        config_empty = SimpleNamespace(FALLBACK_MODEL_GROUP="", MODEL_GROUPS={"p": primary})
        assert resolve_fallback_model_group(config_empty, primary) is primary

        other = _group("o", "other")
        config_ok = SimpleNamespace(FALLBACK_MODEL_GROUP="o", MODEL_GROUPS={"p": primary, "o": other})
        assert resolve_fallback_model_group(config_ok, primary) is other

    def test_repair_group_filters_out_the_same_model(self) -> None:
        """现场的配置：FALLBACK_MODEL_GROUP 就是主模型组自己，换过去等于白换"""
        failed = _group("p", "deepseek-v4-pro", vision=True)
        assert pick_vision_repair_group(failed, failed, failed) is failed

    def test_repair_group_filters_out_models_without_the_capability(self) -> None:
        failed = _group("p", "deepseek-v4-pro", vision=True)
        blind_fallback = _group("f", "some-text-model", vision=False)
        assert pick_vision_repair_group(failed, failed, blind_fallback) is failed

    def test_repair_group_picks_a_declared_capable_fallback(self) -> None:
        failed = _group("p", "deepseek-v4-pro", vision=True)
        capable = _group("f", "gemini-2.5-flash", vision=True)
        assert pick_vision_repair_group(failed, failed, capable) is capable

    def test_repair_group_skips_a_fallback_that_was_just_rejected_too(self) -> None:
        """备用组声明了视觉能力、但刚刚也被上游拒过：观测优先于声明"""
        failed = _group("p", "model-a", vision=True)
        capable = _group("f", "model-b", vision=True)
        note_capability_unsupported(capable)
        assert pick_vision_repair_group(failed, failed, capable) is failed


class TestPrimaryModelGroupResolution:
    """`DEBUG_MIGRATION_MODEL_GROUP` 是 `FALLBACK_MODEL_GROUP` 那条 KeyError 的另一半

    它的默认值是 `"default"`（不是空串），所以「主模型组改了个名 / 名为 default 的组被删了」
    这种极常见的状态下，第一轮请求照常成功，而**每一次迭代**（沙盒报错、Agent 方法回喂、
    多模态回喂）都在这一行抛裸 `KeyError: 'default'` —— 同形、同因、同样不可诊断，
    只是发生得更晚，因此更难对上号。
    """

    def test_non_iteration_requests_always_use_the_primary_group(self) -> None:
        primary = _group("p", "primary")
        debug = _group("d", "debug")
        config = SimpleNamespace(
            USE_MODEL_GROUP="p",
            DEBUG_MIGRATION_MODEL_GROUP="d",
            MODEL_GROUPS={"p": primary, "d": debug},
        )
        assert resolve_primary_model_group(config, is_debug_iteration=False) is primary

    def test_iteration_requests_use_the_debug_group(self) -> None:
        """这条是改造前的行为，必须逐字保住"""
        primary = _group("p", "primary")
        debug = _group("d", "debug")
        config = SimpleNamespace(
            USE_MODEL_GROUP="p",
            DEBUG_MIGRATION_MODEL_GROUP="d",
            MODEL_GROUPS={"p": primary, "d": debug},
        )
        assert resolve_primary_model_group(config, is_debug_iteration=True) is debug

    def test_empty_debug_group_falls_back_to_primary(self) -> None:
        primary = _group("p", "primary")
        config = SimpleNamespace(
            USE_MODEL_GROUP="p",
            DEBUG_MIGRATION_MODEL_GROUP="",
            MODEL_GROUPS={"p": primary},
        )
        assert resolve_primary_model_group(config, is_debug_iteration=True) is primary

    def test_missing_debug_group_falls_back_instead_of_crashing(self) -> None:
        """改造前：`config.MODEL_GROUPS["default"]` 直接 KeyError，整条迭代链路死在这里"""
        primary = _group("p", "primary")
        config = SimpleNamespace(
            USE_MODEL_GROUP="p",
            DEBUG_MIGRATION_MODEL_GROUP="default",  # 该字段的出厂默认值
            MODEL_GROUPS={"p": primary},
        )
        assert resolve_primary_model_group(config, is_debug_iteration=True) is primary

    def test_missing_primary_group_still_raises(self) -> None:
        """`USE_MODEL_GROUP` 那一支不降级：它是「这个部署压根没配好」，第一轮就该炸"""
        config = SimpleNamespace(USE_MODEL_GROUP="gone", DEBUG_MIGRATION_MODEL_GROUP="", MODEL_GROUPS={})
        with pytest.raises(KeyError):
            resolve_primary_model_group(config, is_debug_iteration=False)

    async def test_iteration_with_a_missing_debug_group_still_sends_the_request(self, monkeypatch, tmp_path) -> None:
        """端到端：接线断了的话这条会红，而上面几条纯函数用例照样全绿"""
        monkeypatch.setattr(run_agent_module, "PROMPT_LOG_DIR", str(tmp_path / "prompts"))
        monkeypatch.setattr(run_agent_module, "PROMPT_ERROR_LOG_DIR", str(tmp_path / "prompts_error"))
        primary = _group("p", "primary-model")
        recorder = _Recorder(lambda model, has_image: _ok_response(model))
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary})
        config.DEBUG_MIGRATION_MODEL_GROUP = "default"  # 组已被删/改名

        _response, used, _errors = await send_agent_request(
            [_text_message()],
            config,  # type: ignore[arg-type]
            is_debug_iteration=True,
        )
        assert recorder.models == ["primary-model"]
        assert used is primary


# ============================================================
# 5. send_agent_request 的端到端行为
# ============================================================


def _ok_response(model: str, content: str = "```python\nsend_msg_text(_ck, 'hi')\n```") -> OpenAIResponse:
    return OpenAIResponse(
        response_content=content,
        thought_chain="",
        messages=[],
        message_cnt=0,
        token_consumption=0,
        token_input=0,
        token_output=0,
        use_model=model,
        speed_tokens_per_second=0.0,
        first_token_cost_ms=0,
        generation_time_ms=0,
        stream_mode=False,
    )


class _Recorder:
    """替身上游：记录每一次真正发出去的请求体"""

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> OpenAIResponse:
        model = kwargs["model"]
        messages = kwargs["messages"]
        record = {"model": model, "has_image": has_image_segments(messages), "messages": messages}
        self.calls.append(record)
        return self.behaviour(model, record["has_image"])

    @property
    def models(self) -> List[str]:
        return [call["model"] for call in self.calls]

    @property
    def image_flags(self) -> List[bool]:
        return [call["has_image"] for call in self.calls]


def _make_config(
    primary: ModelConfigGroup,
    fallback_name: str,
    groups: Dict[str, ModelConfigGroup],
    *,
    max_retries: int = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        MODEL_GROUPS=groups,
        USE_MODEL_GROUP=next(name for name, group in groups.items() if group is primary),
        DEBUG_MIGRATION_MODEL_GROUP="",
        FALLBACK_MODEL_GROUP=fallback_name,
        SAVE_PROMPTS_LOG=False,
        AI_CHAT_LLM_API_MAX_RETRIES=max_retries,
        AI_REQUEST_STREAM_MODE=False,
        AI_GENERATE_TIMEOUT=180,
        AI_STREAM_FIRST_TOKEN_TIMEOUT=30,
    )


def _saving_config() -> SimpleNamespace:
    """`SAVE_PROMPTS_LOG` 打开的配置——直接调 `_build_err_log_path` 的用例需要它

    默认值是 `False`（`core/config.py`），所以「关着」才是绝大多数部署的常态；
    要断言路径长什么样，就得先显式把它打开。
    """
    group = _group("p", "m")
    config = _make_config(group, "p", {"p": group})
    config.SAVE_PROMPTS_LOG = True
    return config


@pytest.fixture()
def _isolated_log_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(run_agent_module, "PROMPT_LOG_DIR", str(tmp_path / "prompts"))
    monkeypatch.setattr(run_agent_module, "PROMPT_ERROR_LOG_DIR", str(tmp_path / "prompts_error"))


@pytest.mark.usefixtures("_isolated_log_dirs")
class TestSendAgentRequestRepairsCapabilityMismatch:
    async def test_declared_blind_model_never_sees_the_images(self, monkeypatch) -> None:
        """装配时就按能力裁剪：声明不支持视觉的模型，图片一次都不会发出去"""
        primary = _group("p", "text-only", vision=False)
        recorder = _Recorder(lambda model, has_image: _ok_response(model))
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary})
        response, used, errors = await send_agent_request([_text_message(), _image_message(2)], config)  # type: ignore[arg-type]

        assert len(recorder.calls) == 1
        assert recorder.image_flags == [False]
        assert errors == []
        assert used is primary
        assert response.use_model == "text-only"
        rendered = json.dumps([m.to_dict() for m in recorder.calls[0]["messages"]], ensure_ascii=False)
        assert IMAGE_PLACEHOLDER_TEXT in rendered

    async def test_same_failing_request_is_not_sent_three_times(self, monkeypatch) -> None:
        """现场形态：FALLBACK_MODEL_GROUP == 主模型组，三次重试全打同一个必然失败的请求。
        修复后第一次就把「这个模型不吃图」学到手，第二次改成剥图后的形态并成功"""
        primary = _group("p", "deepseek-v4-pro", vision=True)

        def behaviour(model: str, has_image: bool) -> OpenAIResponse:
            if has_image:
                raise _FakeStatusError(UPSTREAM_VISION_400, 400)
            return _ok_response(model)

        recorder = _Recorder(behaviour)
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary})
        _response, used, errors = await send_agent_request([_text_message(), _image_message(5)], config)  # type: ignore[arg-type]

        assert recorder.image_flags == [True, False], "第二次尝试必须换一种请求形态，而不是原样重发"
        assert len(recorder.calls) == 2
        assert len(errors) == 1
        assert used is primary
        assert is_capability_marked(primary) is True, "学到的判据没有留下，下一轮还会再炸一次"

    async def test_next_round_skips_the_images_entirely(self, monkeypatch) -> None:
        """负判据的意义：同一段对话的后续回合不必每轮都先炸一次再修"""
        primary = _group("p", "deepseek-v4-pro", vision=True)

        def behaviour(model: str, has_image: bool) -> OpenAIResponse:
            if has_image:
                raise _FakeStatusError(UPSTREAM_VISION_400, 400)
            return _ok_response(model)

        recorder = _Recorder(behaviour)
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)
        config = _make_config(primary, "p", {"p": primary})

        await send_agent_request([_image_message(1)], config)  # type: ignore[arg-type]
        first_round_calls = len(recorder.calls)
        await send_agent_request([_image_message(1)], config)  # type: ignore[arg-type]

        assert first_round_calls == 2
        assert len(recorder.calls) == 3, "第二轮应当一次就成，不该再吃一个 400"
        assert recorder.image_flags == [True, False, False]

    async def test_switches_to_a_declared_capable_fallback_and_keeps_the_images(self, monkeypatch) -> None:
        """有真正声明支持视觉的备用组时，先换模型（保住图片这一路信息），而不是直接剥图"""
        primary = _group("p", "deepseek-v4-pro", vision=True)
        capable = _group("f", "gemini-2.5-flash", vision=True)

        def behaviour(model: str, has_image: bool) -> OpenAIResponse:
            if model == "deepseek-v4-pro" and has_image:
                raise _FakeStatusError(UPSTREAM_VISION_400, 400)
            return _ok_response(model)

        recorder = _Recorder(behaviour)
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "f", {"p": primary, "f": capable})
        _response, used, _errors = await send_agent_request([_image_message(2)], config)  # type: ignore[arg-type]

        assert recorder.models == ["deepseek-v4-pro", "gemini-2.5-flash"]
        assert recorder.image_flags == [True, True]
        assert used is capable
        assert is_capability_marked(primary) is True
        assert is_capability_marked(capable) is False

    async def test_capability_mismatch_does_not_eat_the_retry_budget(self, monkeypatch) -> None:
        """能力不匹配发生在**最后一次**尝试上时，修复后的那一次也要真的跑得到

        重试预算只有 1 次：如果把「能力不匹配」当成普通重试计进去，那一次失败之后就没有
        名额留给修复过的请求了，而修复过的请求本来是能成的。
        """
        blind_primary = _group("p", "text-only", vision=False)
        declared_fallback = _group("f", "claims-vision", vision=True)

        def behaviour(model: str, has_image: bool) -> OpenAIResponse:
            if has_image:
                raise _FakeStatusError(UPSTREAM_VISION_400, 400)
            return _ok_response(model)

        recorder = _Recorder(behaviour)
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(blind_primary, "f", {"p": blind_primary, "f": declared_fallback}, max_retries=1)
        _response, used, errors = await send_agent_request([_image_message(1)], config)  # type: ignore[arg-type]

        assert recorder.models == ["claims-vision", "claims-vision"]
        assert recorder.image_flags == [True, False], "补上的那一次必须是剥过图的新形态"
        assert used is declared_fallback
        assert len(errors) == 1

    async def test_repair_prefers_a_capable_candidate_even_on_the_last_slot(self, monkeypatch) -> None:
        """最后一次尝试撞上能力不匹配、而主模型组声明了视觉能力时，先换模型保住图片"""
        capable_primary = _group("p", "gemini-2.5-flash", vision=True)
        blind_fallback = _group("f", "text-only", vision=True)

        def behaviour(model: str, has_image: bool) -> OpenAIResponse:
            if model == "text-only" and has_image:
                raise _FakeStatusError(UPSTREAM_VISION_400, 400)
            return _ok_response(model)

        recorder = _Recorder(behaviour)
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(capable_primary, "f", {"p": capable_primary, "f": blind_fallback}, max_retries=1)
        _response, used, _errors = await send_agent_request([_image_message(1)], config)  # type: ignore[arg-type]

        assert recorder.models == ["text-only", "gemini-2.5-flash"]
        assert recorder.image_flags == [True, True]
        assert used is capable_primary

    async def test_progress_callbacks_report_the_extended_total(self, monkeypatch) -> None:
        """补进去的那一次修复尝试让本轮真实尝试数变成 N+1

        回调的第二个参数就是为此存在的。丢掉它的话后台会显示「重试 3/2」这种
        比分母还大的进度 —— 这正是运维在故障现场第一眼会看到的东西。
        """
        primary = _group("p", "deepseek-v4-pro", vision=True)

        def behaviour(model: str, has_image: bool) -> OpenAIResponse:
            if has_image:
                raise _FakeStatusError(UPSTREAM_VISION_400, 400)
            return _ok_response(model)

        recorder = _Recorder(behaviour)
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)
        seen: List[tuple] = []

        async def _on_attempt(index: int, total: int, model: str) -> None:
            seen.append((index, total))

        config = _make_config(primary, "p", {"p": primary}, max_retries=1)
        await send_agent_request([_image_message(1)], config, on_llm_attempt=_on_attempt)  # type: ignore[arg-type]

        assert seen == [(1, 1), (2, 2)], "补了一次修复尝试之后，报出去的总数必须跟着变"
        assert all(index <= total for index, total in seen)

    async def test_repair_budget_is_bounded(self, monkeypatch) -> None:
        """上游文案抖动时不能把重试次数变成不设上限：修复只补一次"""
        primary = _group("p", "always-rejects", vision=True)

        recorder = _Recorder(lambda model, has_image: (_ for _ in ()).throw(_FakeStatusError(UPSTREAM_VISION_400, 400)))
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary}, max_retries=3)
        with pytest.raises(AllLLMRequestsFailedError):
            await send_agent_request([_image_message(1)], config)  # type: ignore[arg-type]

        assert len(recorder.calls) == 3, "同一个模型 + 同一个错误，尝试次数不该被无限延长"


@pytest.mark.usefixtures("_isolated_log_dirs")
class TestSendAgentRequestUnchangedPaths:
    """没有能力不匹配发生时，重试与 fallback 的行为必须与改造前逐字一致"""

    async def test_plain_failure_still_uses_the_configured_retry_budget(self, monkeypatch) -> None:
        primary = _group("p", "primary-model")
        fallback = _group("f", "fallback-model")

        recorder = _Recorder(lambda model, has_image: (_ for _ in ()).throw(_FakeStatusError("Connection error.", 500)))
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "f", {"p": primary, "f": fallback}, max_retries=3)
        with pytest.raises(AllLLMRequestsFailedError) as excinfo:
            await send_agent_request([_text_message()], config)  # type: ignore[arg-type]

        assert recorder.models == ["primary-model", "primary-model", "fallback-model"]
        assert str(excinfo.value) == "所有 LLM 请求失败"
        assert excinfo.value.kinds == [LLMFailureKind.OTHER] * 3

    async def test_first_success_wins(self, monkeypatch) -> None:
        primary = _group("p", "primary-model")
        fallback = _group("f", "fallback-model")
        recorder = _Recorder(lambda model, has_image: _ok_response(model))
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "f", {"p": primary, "f": fallback})
        _response, used, errors = await send_agent_request([_text_message()], config)  # type: ignore[arg-type]

        assert recorder.models == ["primary-model"]
        assert used is primary
        assert errors == []

    async def test_vision_capable_model_keeps_its_images(self, monkeypatch) -> None:
        primary = _group("p", "gemini-2.5-flash", vision=True)
        recorder = _Recorder(lambda model, has_image: _ok_response(model))
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary})
        await send_agent_request([_image_message(3)], config)  # type: ignore[arg-type]

        assert recorder.image_flags == [True]


@pytest.mark.usefixtures("_isolated_log_dirs")
class TestEmptyResponseIsAFailureNotSilence:
    async def test_empty_content_is_classified_and_reported(self, monkeypatch) -> None:
        """现场那支 `completion_tokens=0 / reasoning_tokens=1116` 的响应是供应商行为，
        我们能做的是让它可辨识：不能一路走成「沙盒执行空脚本 → NORMAL 退出 → Bot 一言不发」"""
        primary = _group("p", "gemini-3-flash")

        recorder = _Recorder(lambda model, has_image: _ok_response(model, content=""))
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary})
        with pytest.raises(AllLLMRequestsFailedError) as excinfo:
            await send_agent_request([_text_message()], config)  # type: ignore[arg-type]

        assert excinfo.value.kinds == [LLMFailureKind.EMPTY_RESPONSE] * 3
        assert describe_llm_failure(excinfo.value.kinds) == NOTICE_LLM_EMPTY

    async def test_whitespace_only_content_counts_as_empty(self, monkeypatch) -> None:
        primary = _group("p", "some-model")
        recorder = _Recorder(lambda model, has_image: _ok_response(model, content="   \n  "))
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary}, max_retries=1)
        with pytest.raises(AllLLMRequestsFailedError) as excinfo:
            await send_agent_request([_text_message()], config)  # type: ignore[arg-type]
        assert excinfo.value.kinds == [LLMFailureKind.EMPTY_RESPONSE]


class TestReasoningOnlyResponseIsSalvaged:
    """原生思考模型把整段输出走了 `reasoning_content`，`content` 留空

    这一支**是我们这边的缺陷**而不是供应商行为：`finish_reason='stop'`、
    `completion_tokens` 五百多，答案完全可解析可执行，只是被判空丢弃了。
    """

    # 从 app.log 08-11 17:36:15 那条响应里取回来的形状
    FIELD_SAMPLE = (
        "<think>\n让我仔细分析一下当前的情况。\n</think>\n\n"
        '```python\nsend_msg_text(_ck, "[@id:2000000001@] 前辈叫了我两次呢……我在的 是有什么事吗？")\n```'
    )

    @pytest.mark.parametrize(
        ("content", "reasoning", "expected"),
        [
            ("answer", "thinking", "answer"),  # 正常形态：正文非空，一个字都不动
            ("", "thinking", "thinking"),  # 正文为空但思考通道有货 → 取回来
            ("   ", "thinking", "thinking"),  # 只有空白同理
            ("", "", ""),  # 两边都空 → 交给上层判成空响应
            ("", "   ", ""),
            ("answer", "", "answer"),
        ],
    )
    def test_salvage_table(self, content: str, reasoning: str, expected: str) -> None:
        assert _salvage_reasoning_only_output(content, reasoning, model="m", field_name="reasoning_content") == expected

    def test_salvaged_output_actually_parses_into_runnable_code(self) -> None:
        """救回来的内容要真的能用 —— 否则只是把「空响应」换成了「空脚本」"""
        salvaged = _salvage_reasoning_only_output("", self.FIELD_SAMPLE, model="deepseek-v4-pro", field_name="reasoning_content")
        parsed = parse_chat_response(salvaged)
        assert parsed.code_content.startswith("send_msg_text(_ck,")
        assert "前辈叫了我两次呢" in parsed.code_content
        assert "让我仔细分析一下当前的情况" in parsed.thought_chain


class _FakeDelta:
    def __init__(self, content=None, reasoning_content=None) -> None:
        self.content = content
        self.reasoning_content = reasoning_content


class _FakeChoice:
    def __init__(self, delta=None, message=None) -> None:
        self.delta = delta
        self.message = message


class _FakeChunk:
    def __init__(self, *, content=None, reasoning=None, usage=None) -> None:
        self.choices = [_FakeChoice(delta=_FakeDelta(content, reasoning))]
        self.usage = usage


class _FakeUsage:
    def __init__(self, total=10, prompt=5, completion=5) -> None:
        self.total_tokens = total
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _FakeStream:
    """替身流：`gen_openai_chat_response` 用 `async with` 托管它并 `async for` 消费

    **迭代器必须只有一个**：产品代码先用 `__aiter__().__anext__()` 取首块（为了给首字超时
    单独计时），再用 `async for` 接着消费剩下的。每次 `__aiter__` 都新建生成器的话，
    首块会被消费两次 —— 这正是 §2.9 那条「fake 与被测代码的真实约定脱靶」的同款陷阱。
    """

    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)
        self._iter = None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True
        return False

    def __aiter__(self):
        if self._iter is None:

            async def _gen():
                for chunk in self._chunks:
                    yield chunk

            self._iter = _gen()
        return self._iter


class _FakeCompletion:
    def __init__(self, *, content, reasoning) -> None:
        message = SimpleNamespace(content=content, reasoning_content=reasoning)
        self.choices = [_FakeChoice(message=message)]
        self.usage = _FakeUsage()


def _install_fake_openai(monkeypatch, *, result) -> None:
    """把 openai SDK 客户端与共享连接池一起换掉，只留下本模块自己的判据逻辑"""

    class _Completions:
        async def create(self, **kwargs):
            if kwargs.get("stream"):
                return _FakeStream(result)
            return result

    class _FakeClient:
        def __init__(self, **_kwargs) -> None:
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeClient)
    monkeypatch.setattr(openai_module, "_get_http_client", lambda **_kwargs: object())


class TestGenChatResponseEmptyAndSalvage:
    """`gen_openai_chat_response` 自己那两条判据的行为级用例

    纯函数 `_salvage_reasoning_only_output` 的表驱动用例证明不了「它被接在了请求路径上」：
    实测把两处调用点分别改回旧写法，整份测试**一条都不会红**（§2.7 同款）。
    这一组直接驱动 `gen_openai_chat_response`，替身只替换 openai SDK 客户端本身。
    """

    REASONING_ONLY = (
        "<think>\n先看看上下文。\n</think>\n\n" '```python\nsend_msg_text(_ck, "在的")\n```'
    )

    async def test_stream_reasoning_only_is_salvaged(self, monkeypatch) -> None:
        """现场那支：原生思考模型把整段输出（含代码块）走了 reasoning 通道"""
        _install_fake_openai(
            monkeypatch,
            result=[
                _FakeChunk(reasoning="<think>\n先看看上下文。\n</think>\n\n"),
                _FakeChunk(reasoning='```python\nsend_msg_text(_ck, "在的")\n```', usage=_FakeUsage()),
            ],
        )
        response = await gen_openai_chat_response(model="deepseek-v4-pro", messages=[], stream_mode=True)
        assert response.response_content == self.REASONING_ONLY
        assert parse_chat_response(response.response_content).code_content.startswith("send_msg_text(_ck,")

    async def test_stream_with_chunks_but_no_text_is_an_empty_response(self, monkeypatch) -> None:
        """旧判据是「没收到过数据块」，而收到过块、正文与思考通道都空的流上它**恒不成立**：
        `response_content=""` 会一路走到沙盒执行空脚本、NORMAL 退出、Bot 一言不发且零 ERROR"""
        _install_fake_openai(
            monkeypatch,
            result=[_FakeChunk(content=""), _FakeChunk(content=None, usage=_FakeUsage())],
        )
        with pytest.raises(EmptyLLMResponseError):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=True)

    async def test_stream_that_never_yields_a_chunk_is_the_same_class(self, monkeypatch) -> None:
        """「一个块都没有」与「有块但没正文」是同一件事，必须归同一类，
        否则同一个供应商行为会按它是直接关流还是先发空块给出两种用户回执"""
        _install_fake_openai(monkeypatch, result=[])
        with pytest.raises(EmptyLLMResponseError):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=True)

    async def test_stream_normal_content_is_untouched(self, monkeypatch) -> None:
        _install_fake_openai(
            monkeypatch,
            result=[_FakeChunk(content="he"), _FakeChunk(content="llo", usage=_FakeUsage())],
        )
        response = await gen_openai_chat_response(model="m", messages=[], stream_mode=True)
        assert response.response_content == "hello"

    async def test_non_stream_reasoning_only_is_salvaged(self, monkeypatch) -> None:
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content="", reasoning=self.REASONING_ONLY))
        response = await gen_openai_chat_response(model="m", messages=[], stream_mode=False)
        assert response.response_content == self.REASONING_ONLY

    async def test_non_stream_truly_empty_raises(self, monkeypatch) -> None:
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content=None, reasoning=None))
        with pytest.raises(EmptyLLMResponseError):
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)

    async def test_non_stream_normal_content_is_untouched(self, monkeypatch) -> None:
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content="hello", reasoning="thinking"))
        response = await gen_openai_chat_response(model="m", messages=[], stream_mode=False)
        assert response.response_content == "hello"
        assert response.thought_chain == "thinking"

    async def test_sub_millisecond_response_does_not_crash_on_speed_stats(self, monkeypatch) -> None:
        """`_generation_time_ms` 取整到毫秒，够快的上游会让它恰好为 0

        那一行除法在 `except Exception` **之后**，所以 ZeroDivisionError 既不会被翻译成
        可辨识的失败、也不会落盘错误日志：一次成功的请求以一条与 LLM 毫无关系的异常收场。
        """
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content="hello", reasoning=""))
        monkeypatch.setattr(openai_module.time, "time", lambda: 1000.0)
        response = await gen_openai_chat_response(model="m", messages=[], stream_mode=False)
        assert response.response_content == "hello"
        assert response.generation_time_ms == 0
        assert response.speed_tokens_per_second == 0.0

    async def test_speed_stats_are_tokens_per_second(self, monkeypatch) -> None:
        """顺带钉住这个量的口径，免得「修掉除零」顺手把单位改了"""
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content="hello", reasoning=""))
        clock = iter([1000.0, 1002.0])
        monkeypatch.setattr(openai_module.time, "time", lambda: next(clock))
        response = await gen_openai_chat_response(model="m", messages=[], stream_mode=False)
        # _FakeUsage: completion_tokens=5，耗时 2s
        assert response.generation_time_ms == 2000
        assert response.speed_tokens_per_second == pytest.approx(2.5)


class TestErrorLogCarriesTheReason:
    """`prompts_error/*.json` 里必须能看到失败原因

    改造前那些文件只有 `timestamp/model/stream_mode/metrics/request/response` 六个键，
    1.4MB 里绝大部分是图片 base64，**一个字的失败原因都没有**——真正的原因只在 app.log 里，
    而两处按不同规则命名，对不上号。
    """

    async def test_error_log_has_an_error_section(self, tmp_path) -> None:
        response = OpenAIErrResponse.create_from_exception(
            _FakeStatusError(UPSTREAM_VISION_400, 400),
            use_model="deepseek-v4-pro",
            stream_mode=False,
            messages=[{"role": "user", "content": "hi"}],
        )
        path = tmp_path / "err.json"
        await response.save_log(log_path=path, messages=[{"role": "user", "content": "hi"}], message_cnt=1)

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["error"]["type"] == "_FakeStatusError"
        assert "当前模型不支持该能力：vision" in payload["error"]["message"]
        assert payload["model"] == "deepseek-v4-pro"

    async def test_success_log_has_no_error_section(self, tmp_path) -> None:
        path = tmp_path / "ok.json"
        await _ok_response("m").save_log(log_path=path, messages=[], message_cnt=0)
        assert "error" not in json.loads(path.read_text(encoding="utf-8"))

    def test_error_log_filename_uses_the_model_actually_used(self) -> None:
        """改造前三次重试共写同一个文件，且文件名固定取**主**模型组的模型名：
        切到 fallback 时文件名指向的是另一个模型"""
        config = _saving_config()
        first = run_agent_module._build_err_log_path(config, "deepseek-v4-pro", 1)
        second = run_agent_module._build_err_log_path(config, "gemini-2.5-flash", 2)
        assert first is not None and second is not None
        assert "deepseek-v4-pro" in first and "try1" in first
        assert "gemini-2.5-flash" in second and "try2" in second
        assert first != second

    async def test_only_real_files_are_registered_as_recent_error_logs(self, tmp_path, monkeypatch) -> None:
        """`/debug err-logs` 与运维 Bot 会按这些路径去读文件，登记了不存在的路径就是给自己埋雷"""
        monkeypatch.setattr(run_agent_module, "PROMPT_LOG_DIR", str(tmp_path / "prompts"))
        monkeypatch.setattr(run_agent_module, "PROMPT_ERROR_LOG_DIR", str(tmp_path / "prompts_error"))
        run_agent_module.RECENT_ERR_LOGS.clear()

        primary = _group("p", "some-model")
        # 替身上游不落盘，等价于「失败发生在 gen_openai_chat_response 之外」
        recorder = _Recorder(lambda model, has_image: _ok_response(model, content=""))
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary}, max_retries=1)
        with pytest.raises(AllLLMRequestsFailedError):
            await send_agent_request([_text_message()], config)  # type: ignore[arg-type]

        assert list(run_agent_module.RECENT_ERR_LOGS) == []

    def test_slashed_model_names_do_not_create_directories(self) -> None:
        """`openai/gpt-4o` 这类模型名会让 save_log 顺手 mkdir 出一层子目录，把错误日志散到各处"""
        path = run_agent_module._build_err_log_path(_saving_config(), "openai/gpt-4o", 1)
        assert path is not None
        assert "openai_gpt-4o" in Path(path).name
        assert "/" not in Path(path).name and "\\" not in Path(path).name


@pytest.mark.usefixtures("_isolated_log_dirs")
class TestSavePromptsLogGovernsBothDirections:
    """`SAVE_PROMPTS_LOG` 此前只管住了成功那条路

    失败路径（逐次尝试的 `prompts_error/chat_err_*.json`、全败兜底的
    `prompts/chat_err_log_*.log`）**无条件落盘**。测试部署上实测：开关是 `false`，
    `logs/prompts/` 里 267 个文件全是 `chat_err_log_*`，`logs/prompts_error/` 707 MB，
    24 小时新增 261 MB。而这两个目录装的是「全部租户的完整 LLM 提示词原文」
    （`routers/console/space_cleanup.py`）——运维关掉了留存，代码还在留存。

    失败**原因**不受这个开关影响：它由 `openai.py` 的 `logger.error` 打进 `logs/app/`，
    上面 `TestErrorLogCarriesTheReason` 钉的是那一份。
    """

    def test_switch_off_yields_no_error_log_path(self) -> None:
        config = _saving_config()
        config.SAVE_PROMPTS_LOG = False
        assert run_agent_module._build_err_log_path(config, "m", 1) is None

    def test_switch_on_yields_a_path(self) -> None:
        assert run_agent_module._build_err_log_path(_saving_config(), "m", 1) is not None

    async def test_all_attempts_failed_writes_nothing_when_switch_is_off(self, tmp_path, monkeypatch) -> None:
        """全败兜底那一份也受管。它写的是 `messages` 全文，与逐次转储是同一类数据"""
        prompts = tmp_path / "prompts"
        monkeypatch.setattr(run_agent_module, "PROMPT_LOG_DIR", str(prompts))
        primary = _group("p", "some-model")
        monkeypatch.setattr(
            run_agent_module,
            "gen_openai_chat_response",
            _Recorder(lambda model, has_image: _ok_response(model, content="")),
        )

        config = _make_config(primary, "p", {"p": primary}, max_retries=1)
        with pytest.raises(AllLLMRequestsFailedError):
            await send_agent_request([_text_message()], config)  # type: ignore[arg-type]

        assert not prompts.exists() or not list(prompts.glob("chat_err_log_*.log"))

    async def test_all_attempts_failed_still_writes_when_switch_is_on(self, tmp_path, monkeypatch) -> None:
        """开着的时候必须照写——否则这次修复就成了「顺手把功能删了」"""
        prompts = tmp_path / "prompts"
        monkeypatch.setattr(run_agent_module, "PROMPT_LOG_DIR", str(prompts))
        primary = _group("p", "some-model")
        monkeypatch.setattr(
            run_agent_module,
            "gen_openai_chat_response",
            _Recorder(lambda model, has_image: _ok_response(model, content="")),
        )

        config = _make_config(primary, "p", {"p": primary}, max_retries=1)
        config.SAVE_PROMPTS_LOG = True
        with pytest.raises(AllLLMRequestsFailedError):
            await send_agent_request([_text_message()], config)  # type: ignore[arg-type]

        assert list(prompts.glob("chat_err_log_*.log"))


# ============================================================
# 6. 用户可见回执与刷屏节流
# ============================================================


class TestFailureNoticeThrottle:
    """回执要发，但不能变成新的洪水

    08-11 那天真实群里被刷了十几条「与 LLM 通信出错啦」——每一轮失败都发一条，
    而故障持续了三个小时。
    """

    def test_first_notice_goes_out_then_cools_down(self) -> None:
        throttle = ChatNoticeThrottle()
        chat = "onebot_v11-ob1-group_1"
        assert throttle.should_send(chat, 180.0, now=1000.0) is True
        assert throttle.should_send(chat, 180.0, now=1001.0) is False
        assert throttle.should_send(chat, 180.0, now=1179.0) is False
        assert throttle.should_send(chat, 180.0, now=1180.0) is True

    def test_suppressed_rounds_are_counted_not_lost(self) -> None:
        """冷却窗口内仍然计数、仍然打日志，减的只是刷屏"""
        throttle = ChatNoticeThrottle()
        chat = "onebot_v11-ob1-group_1"
        throttle.should_send(chat, 180.0, now=1000.0)
        for offset in (1.0, 2.0, 3.0):
            throttle.should_send(chat, 180.0, now=1000.0 + offset)
        assert throttle.suppressed_count(chat) == 3
        assert throttle.should_send(chat, 180.0, now=1200.0) is True
        assert throttle.suppressed_count(chat) == 0

    def test_sessions_are_independent(self) -> None:
        """粒度是会话不是实例：A 群炸了不该让 B 群也收不到提示"""
        throttle = ChatNoticeThrottle()
        assert throttle.should_send("chat-a", 180.0, now=1000.0) is True
        assert throttle.should_send("chat-b", 180.0, now=1000.0) is True
        assert throttle.should_send("chat-a", 180.0, now=1010.0) is False

    def test_zero_cooldown_notifies_every_time(self) -> None:
        throttle = ChatNoticeThrottle()
        assert throttle.should_send("chat-a", 0.0, now=1000.0) is True
        assert throttle.should_send("chat-a", 0.0, now=1000.0) is True

    def test_bookkeeping_does_not_grow_without_bound(self) -> None:
        """键是 `chat_key` —— 频道数是不设上限的增长维度（几十个实例 × 几十个群 + 私聊），
        与 `model_capability` 那份「键来自模型组配置」的记账完全不是一回事"""
        throttle = ChatNoticeThrottle()
        for index in range(500):
            throttle.should_send(f"chat-{index}", 180.0, now=1000.0 + index)
        # 500 个会话里只有最后 180 秒内发过的还需要记账
        assert throttle.tracked_count() <= 181
        assert throttle.tracked_count() > 0

    def test_pruning_does_not_lose_the_suppressed_count(self) -> None:
        """时间戳可以丢，「这个会话被压下去过几条」不能跟着丢 —— 那是唯一的可诊断线索"""
        throttle = ChatNoticeThrottle()
        assert throttle.should_send("chat-a", 180.0, now=1000.0) is True
        for offset in (1.0, 2.0):
            throttle.should_send("chat-a", 180.0, now=1000.0 + offset)
        assert throttle.suppressed_count("chat-a") == 2
        # 另一个会话在冷却窗口之后触发一次记账清理
        throttle.should_send("chat-b", 180.0, now=2000.0)
        assert throttle.suppressed_count("chat-a") == 2
        assert throttle.should_send("chat-a", 180.0, now=2001.0) is True
        assert throttle.suppressed_count("chat-a") == 0

    def test_cooldown_still_holds_across_pruning(self) -> None:
        """清理只能丢「已经过期」的那些，正在冷却中的一条都不能丢"""
        throttle = ChatNoticeThrottle()
        assert throttle.should_send("chat-a", 180.0, now=1000.0) is True
        for index in range(300):
            throttle.should_send(f"other-{index}", 180.0, now=1000.0 + index * 0.1)
        assert throttle.should_send("chat-a", 180.0, now=1030.0) is False

    @pytest.mark.parametrize(
        ("kinds", "expected"),
        [
            ([LLMFailureKind.EMPTY_RESPONSE] * 3, NOTICE_LLM_EMPTY),
            ([LLMFailureKind.EMPTY_RESPONSE], NOTICE_LLM_EMPTY),
            # 混了别的失败就不能说「一个字都没返回」，那会把用户往错的方向引
            ([LLMFailureKind.EMPTY_RESPONSE, LLMFailureKind.OTHER], NOTICE_LLM_FAILED),
            ([LLMFailureKind.OTHER] * 3, NOTICE_LLM_FAILED),
            ([LLMFailureKind.VISION_UNSUPPORTED], NOTICE_LLM_FAILED),
            ([], NOTICE_LLM_FAILED),
        ],
    )
    def test_notice_text_is_from_a_fixed_set(self, kinds, expected: str) -> None:
        assert describe_llm_failure(kinds) == expected

    def test_notices_never_carry_upstream_text(self) -> None:
        """回执取自固定集合、不带任何上游原文，因此不构成凭据/配置的旁路"""
        for notice in (NOTICE_LLM_EMPTY, NOTICE_LLM_FAILED):
            assert "http" not in notice
            assert "Error code" not in notice


# ============================================================
# 7. 这些零件真的**被接上了**
# ============================================================


def _calls_made_inside(source: str, function_name: str) -> Set[str]:
    """收集某个函数体内所有被调用的名字（含点号形式与嵌套函数）"""
    target = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    names: Set[str] = set()
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _names_raised_inside(source: str, function_name: str) -> Set[str]:
    target = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    raised: Set[str] = set()
    for node in ast.walk(target):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        if isinstance(exc, ast.Name):
            raised.add(exc.id)
    return raised


def _source_of(source: str, function_name: str) -> str:
    tree = ast.parse(source)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    return ast.get_source_segment(source, target) or ""


def _call_arg_names(source: str, container: str, callee: str) -> Set[str]:
    """`container` 内所有 `callee(...)` 调用的实参名字（含关键字实参的值）

    比「文本里出现过这个常量」严格得多：`_run_chat_agent_task` 开头那句局部 import 本身就把
    `NOTICE_ROUND_TIMEOUT` 写进了函数源码，于是按文本断言时，**把那条分支整个删掉**测试照样全绿
    （实测：变异 M17/M18 存活）。
    """
    target = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == container
    )
    names: Set[str] = set()
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != callee:
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            if isinstance(arg, ast.Name):
                names.add(arg.id)
            elif isinstance(arg, ast.Call):
                inner = arg.func
                names.add(inner.id if isinstance(inner, ast.Name) else getattr(inner, "attr", ""))
    return names


def _names_read_inside(source: str, function_name: str) -> Set[str]:
    """函数体内被**读取**过的名字

    形参只是 `ast.arg`，不算读取。所以「收下这个参数但从不使用」会被这条判据认出来，
    而按源码文本找名字则认不出来（形参名本身就在源码里）。
    """
    target = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def _guard_names_of_branch(source: str, container: str, marker: str) -> Set[str]:
    """找到函数体里「分支体含 marker 文本」的那个 if/elif，返回它的判据里用到的名字

    只断言分支体的文本（占位串还在源码里）挡不住「把判据改成恒假」这一类变异：
    分支还在、字面量还在，只是永远走不到（实测：变异 M02 存活）。
    """
    target = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == container
    )
    for node in ast.walk(target):
        if not isinstance(node, ast.If):
            continue
        body_src = "\n".join(ast.get_source_segment(source, stmt) or "" for stmt in node.body)
        if marker not in body_src:
            continue
        return {sub.id for sub in ast.walk(node.test) if isinstance(sub, ast.Name)}
    return set()


class TestTheFixesAreActuallyWiredIn:
    """每个零件都真的被调用了

    上面几组全部在直接调用被测函数，因此它们全绿只证明零件本身是对的。把调用点删掉
    —— 装配时不再判能力、失败后不再分类、回执不再过闸门 —— 那些测试一条都不会红，
    而整套修复会静默失效。本仓库已经为此立过同款契约（§2.7）。
    """

    RUN_AGENT_SOURCE = Path(run_agent_module.__file__).read_text(encoding="utf-8")
    HISTORY_SOURCE = Path(history_module.__file__).read_text(encoding="utf-8")

    def test_history_assembly_reads_the_capability(self) -> None:
        """不判的话，「装配时按能力裁剪」这一整条就只剩注释"""
        calls = _calls_made_inside(self.HISTORY_SOURCE, "render_history_data")
        assert "supports_vision" in calls

    def test_history_assembly_leaves_a_placeholder(self) -> None:
        """静默丢弃会让模型把「群里刚发了张图」当成没发生过"""
        body = _source_of(self.HISTORY_SOURCE, "render_history_data")
        assert "omitted=" in body
        assert "no vision capability" in body

    def test_the_placeholder_branch_is_actually_reachable(self) -> None:
        """占位文本还躺在源码里 ≠ 那条分支还走得到：判据必须真的读能力与图片两件事"""
        guards = _guard_names_of_branch(self.HISTORY_SOURCE, "render_history_data", "no vision capability")
        assert "vision_enabled" in guards, "占位分支的判据不再读能力，等于它永远不会触发"
        assert "image_segments" in guards

    def test_every_attempt_is_trimmed_by_capability(self) -> None:
        """裁剪必须在**每一次尝试**上做：改造前只对主模型组判过一次，
        fallback 的能力声明从头到尾没人读"""
        calls = _calls_made_inside(self.RUN_AGENT_SOURCE, "send_agent_request")
        assert "_prepare_attempt_payload" in calls
        payload_calls = _calls_made_inside(self.RUN_AGENT_SOURCE, "_prepare_attempt_payload")
        assert "supports_vision" in payload_calls
        assert "strip_image_segments" in payload_calls
        assert "has_image_segments" in payload_calls

    def test_failures_are_classified_and_acted_on(self) -> None:
        calls = _calls_made_inside(self.RUN_AGENT_SOURCE, "send_agent_request")
        assert "classify_llm_failure" in calls, "不分类的话 400 与网络错误还是一视同仁地重试"
        assert "note_capability_unsupported" in calls, "分类出来却不记下来，下一轮还会再炸一次"
        assert "pick_vision_repair_group" in calls, "不换候选的话 fallback 还是失败模型自己"
        assert "note_capability_ok" in calls, "只记坏不记好，一次误判就把模型永久钉成瞎子"
        assert "build_attempt_plan" in calls
        assert "resolve_fallback_model_group" in calls
        assert "resolve_primary_model_group" in calls, "迭代请求的模型组解析回到裸下标就是一条 KeyError"
        assert "_build_err_log_path" in calls, "循环外算一次就退回「三次重试共写一个文件、名字取错模型」"

    def test_run_agent_forwards_the_real_attempt_total(self) -> None:
        """回调把真实尝试总数送到了，`run_agent` 得真的用它 —— 否则后台显示「重试 3/2」

        改造前四个进度回调里有两个把 `retry_total` 收下就丢（`_retry_total`），
        另两个连收都不收，一律用外层那个 `AI_CHAT_LLM_API_MAX_RETRIES`。
        """
        # `run_agent` 的正文在 `_run_agent_in_instance_scope` 里（外层只设实例上下文）
        forwarded = _call_arg_names(self.RUN_AGENT_SOURCE, "_run_agent_in_instance_scope", "publish_runtime_state")
        assert "retry_total" in forwarded, "回调收到的真实总数没有往下传，进度条的分母就是错的"
        body = _source_of(self.RUN_AGENT_SOURCE, "_run_agent_in_instance_scope")
        assert body.count("llm_retry_attempts=retry_total") == 4, "四个进度回调都要把真实总数透传下去"
        # 「收下了」不等于「用了」：形参在源码里恒存在，按文本断言杀不掉「收下就丢」这种变异
        assert "llm_retry_attempts" in _names_read_inside(self.RUN_AGENT_SOURCE, "publish_runtime_state"), (
            "广播函数收下了真实总数却没读它，等于四个调用点白传"
        )

    def test_agent_round_exhaustion_is_no_longer_silent(self) -> None:
        """改造前迭代跑满后函数自然返回 None：不抛、不记 ERROR、聊天里也没有回执"""
        assert "AgentRoundUnfinishedError" in _names_raised_inside(self.RUN_AGENT_SOURCE, "_run_agent_in_instance_scope")

    def test_openai_layer_salvages_and_refuses_empty(self) -> None:
        openai_source = Path(
            Path(run_agent_module.__file__).resolve().with_name("openai.py"),
        ).read_text(encoding="utf-8")
        calls = _calls_made_inside(openai_source, "gen_openai_chat_response")
        assert "_salvage_reasoning_only_output" in calls
        raised = _names_raised_inside(openai_source, "gen_openai_chat_response")
        assert "EmptyLLMResponseError" in raised, "空响应不抛的话会一路走到沙盒执行空脚本"

    def test_message_service_gives_a_receipt_on_every_failure_path(self) -> None:
        from nekro_agent.services import message_service  # noqa: PLC0415  局部导入，避免整份测试拉起消息层

        source = Path(message_service.__file__).read_text(encoding="utf-8")
        calls = _calls_made_inside(source, "_run_chat_agent_task")
        assert "_notify_round_failure" in calls
        assert "should_send" in calls, "回执不过闸门的话，故障持续时它自己就是新的洪水"
        assert "describe_llm_failure" in calls

        # 按**实参**断言而不是按函数源码的文本：局部 import 已经把这三个常量写进了函数源码，
        # 于是文本断言下「把分支整个删掉」照样全绿（实测变异 M17/M18 存活）
        notified = _call_arg_names(source, "_run_chat_agent_task", "_notify_round_failure")
        assert "NOTICE_ROUND_TIMEOUT" in notified, "兜底超时那条分支改造前只有一行 ERROR"
        assert "NOTICE_ROUND_UNFINISHED" in notified, "迭代耗尽改造前彻底静默"
        assert "NOTICE_ROUND_ERROR" in notified, "非 LLM 类异常改造前一律静默"
        assert "describe_llm_failure" in notified

    def test_the_receipt_is_actually_sent(self) -> None:
        """回执算出来了却不发，与改造前的装死等价——而上面那些断言一条都不会红
        （实测变异 M15：把 `_notify_round_failure` 里的发送换成一行 debug 日志，全绿）"""
        from nekro_agent.services import message_service  # noqa: PLC0415

        source = Path(message_service.__file__).read_text(encoding="utf-8")
        assert "send_operation_message" in _calls_made_inside(source, "_notify_round_failure")


class TestExceptionContract:
    def test_all_llm_failed_message_is_unchanged(self) -> None:
        """既有调用方按 `str(e)` 认这条消息，改动不能顺手把它换掉"""
        assert str(AllLLMRequestsFailedError()) == "所有 LLM 请求失败"
        assert AllLLMRequestsFailedError().kinds == []

    def test_round_unfinished_is_not_a_value_error(self) -> None:
        """与 `AllLLMRequestsFailedError` 分开，才能给出不同的回执文案"""
        assert not isinstance(AgentRoundUnfinishedError("x"), AllLLMRequestsFailedError)

    def test_empty_response_error_stays_a_value_error(self) -> None:
        """上游 `openai.py` 原本抛的就是 ValueError，既有 `except ValueError` 不能被绕过"""
        assert isinstance(EmptyLLMResponseError("x"), ValueError)
