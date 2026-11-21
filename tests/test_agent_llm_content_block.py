"""模型拒答（安全/策略拦截）必须与「一个字都没返回」分开

## 由来

现场 `data/nekro_agent/logs/prompts_error/` 733 份错误文件里，`EmptyLLMResponseError` 占 511 条，
其中 `finish_reason` 的分布是 `NO_CHOICES` 462、**`prohibited_content` 26**、
**`malformed_function_call` 13**、`tool_calls` 2，模型几乎全是 `gemini-3.6-flash` 系。

也就是说：Gemini 的安全拦截回来的是 `content=None` + `finish_reason='prohibited_content'`，
在改造前它与「上游一个字都没返回」是**同一条分支、同一个异常、同一句用户回执**。后果有两层：

1. 用户收到的是「模型这次一个字都没返回，再跟我说一次好吗」——照原话再说一次必然再被拦，
   于是重试链路把同一个必然失败的请求发满，最后以「这轮我没能把事情做完」收场；
2. 运维在 733 份文件里也分不出这 26 条，只能看到一片空响应。

## 这份测试保护的契约

- 拦截类 `finish_reason` 在**流式与非流式两条分支上各自**都被认出来（本项目已经五次栽在
  「同一个判据的第二处实现没跟上」上，所以两条分支分别驱动、分别断言）；
- 判据认出来之后一路能走到用户回执：`classify_llm_failure` → `describe_llm_failure`
  三个模块串起来验一遍，杜绝「分类函数认得一个没人抛的异常类」；
- `LLMContentBlockedError` 是 `EmptyLLMResponseError` 的子类（既有 `except` 不用改），
  但 `classify_llm_failure` **必须先判子类**——顺序写反就整个吞回 `EMPTY_RESPONSE`，
  正是那种「修了一半」的形态；
- `malformed_function_call` 与安全拦截**不是一回事**：前者重试有意义，后者重试必然再被拦；
- 反向对照：`finish_reason='stop'`/`'length'`/`'tool_calls'` 且正文为空时仍然是普通空响应，
  证明判据没有被吹宽成「只要有 finish_reason 就算拦截」。

全部确定性：不起真网络（只替换 openai SDK 客户端与连接池，用量记账换成空操作）、不靠时间流逝、不做随机 fuzz。
"""

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Set

import pytest

from nekro_agent.services.agent import openai as openai_module
from nekro_agent.services.agent.failure_feedback import (
    NOTICE_LLM_BLOCKED,
    NOTICE_LLM_EMPTY,
    NOTICE_LLM_FAILED,
    describe_llm_failure,
)
from nekro_agent.services.agent.model_capability import (
    LLMFailureKind,
    classify_llm_failure,
)
from nekro_agent.services.agent.openai import (
    CONTENT_BLOCKED_FINISH_REASONS,
    MALFORMED_FUNCTION_CALL_FINISH_REASON,
    EmptyLLMResponseError,
    LLMContentBlockedError,
    LLMMalformedToolCallError,
    gen_openai_chat_response,
    normalize_finish_reason,
)

# ============================================================
# 替身：只替换 openai SDK 客户端本身，判据逻辑全部走真实代码
# ============================================================


class _FakeDelta:
    def __init__(self, content=None, reasoning_content=None) -> None:
        self.content = content
        self.reasoning_content = reasoning_content


class _FakeChoice:
    def __init__(self, *, delta=None, message=None, finish_reason=None) -> None:
        self.delta = delta
        self.message = message
        self.finish_reason = finish_reason


class _FakeUsage:
    def __init__(self, total=10, prompt=5, completion=5) -> None:
        self.total_tokens = total
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _FakeChunk:
    """一个流式数据块

    `with_delta=False` 复刻现场那种「收尾块只有 finish_reason、连 delta 字段都没有」的形态：
    产品代码里空 delta 会被 `_extract_valid_delta` 提前丢掉，所以 finish_reason 必须在那之前记下来。
    """

    def __init__(self, *, content=None, reasoning=None, usage=None, finish_reason=None, with_delta=True) -> None:
        delta = _FakeDelta(content, reasoning) if with_delta else None
        self.choices = [_FakeChoice(delta=delta, finish_reason=finish_reason)]
        self.usage = usage


class _FakeCompletion:
    def __init__(self, *, content=None, reasoning=None, finish_reason=None, has_finish_reason_field=True) -> None:
        message = SimpleNamespace(content=content, reasoning_content=reasoning)
        choice = (
            _FakeChoice(message=message, finish_reason=finish_reason)
            if has_finish_reason_field
            # 部分中转站的响应体里根本没有这个字段：取不到不该变成一条 AttributeError
            else SimpleNamespace(message=message)
        )
        self.choices = [choice]
        self.usage = _FakeUsage()


class _FakeStream:
    """替身流：产品代码先 `__aiter__().__anext__()` 取首块，再 `async for` 消费剩下的

    迭代器只能有一个，否则首块会被消费两次。
    """

    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)
        self._iter = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def __aiter__(self):
        if self._iter is None:

            async def _gen():
                for chunk in self._chunks:
                    yield chunk

            self._iter = _gen()
        return self._iter


def _install_fake_openai(monkeypatch, *, result) -> None:
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
    # 用量记账换成空操作（记账本身由 `test_llm_usage_metering.py` 测）。真记账会起一个后台写库任务，
    # 这里既不建库也不等它：用例一结束事件循环就关，写到一半的那条 INSERT 回不了结果，
    # 会把前面用例留下的 sqlite 连接的工作线程一并弄死，后面查这条连接的用例永远等不到结果。
    monkeypatch.setattr(openai_module, "record_llm_usage", lambda *_args, **_kwargs: None)


# 现场逐字取回来的拒答形态：Gemini 安全拦截时 content 为 None，理由只在 finish_reason 上
GEMINI_BLOCK_REASON = "prohibited_content"
# 同一个原因的其它写法（原生 REST 大写、某些中转站写成连字符），归一化之后必须落到同一类
GEMINI_BLOCK_REASON_UPPER = "PROHIBITED_CONTENT"
OPENAI_FILTER_REASON_HYPHEN = "Content-Filter"


# ============================================================
# 1. 非流式分支
# ============================================================


class TestNonStreamContentBlocked:
    """非流式：`res.choices[0].finish_reason` 属于拦截集合时抛 `LLMContentBlockedError`"""

    @pytest.mark.parametrize("reason", sorted(CONTENT_BLOCKED_FINISH_REASONS))
    async def test_every_blocked_reason_is_recognised(self, monkeypatch, reason: str) -> None:
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content=None, finish_reason=reason))
        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)
        assert excinfo.value.finish_reason == reason
        assert reason in str(excinfo.value), "错误消息里必须带上 finish_reason，否则 733 份错误文件还是分不出这一类"

    @pytest.mark.parametrize(
        ("raw", "normalized"),
        [
            (GEMINI_BLOCK_REASON_UPPER, "prohibited_content"),
            (OPENAI_FILTER_REASON_HYPHEN, "content_filter"),
            ("  SAFETY  ", "safety"),
        ],
    )
    async def test_reason_is_normalised_before_matching(self, monkeypatch, raw: str, normalized: str) -> None:
        """大小写/连字符/空白都要先归一：漏判一种写法就等于这一路完全没修"""
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content=None, finish_reason=raw))
        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)
        assert excinfo.value.finish_reason == normalized

    async def test_blocked_wins_over_a_non_empty_body(self, monkeypatch) -> None:
        """判空**之前**先判拦截

        `content_filter` 可能在模型已经吐了半截之后才触发，那半截必然是被截断的：
        送进 `parse_chat_response` 得到的是半个代码块。把拦截判据放到 `if not output` 之后，
        这一条就会安静地返回那半句残文——本条用例正是钉死这个顺序的。
        """
        _install_fake_openai(
            monkeypatch,
            result=_FakeCompletion(content="```python\nsend_msg_text(_ck, \"我不能", finish_reason="content_filter"),
        )
        with pytest.raises(LLMContentBlockedError):
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)


class TestNonStreamMalformedToolCall:
    async def test_empty_body_with_malformed_call_is_its_own_class(self, monkeypatch) -> None:
        _install_fake_openai(
            monkeypatch,
            result=_FakeCompletion(content=None, finish_reason=MALFORMED_FUNCTION_CALL_FINISH_REASON),
        )
        with pytest.raises(LLMMalformedToolCallError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)
        assert not isinstance(excinfo.value, LLMContentBlockedError), "写坏的工具调用不是内容被拦，重试策略与文案都不同"
        assert MALFORMED_FUNCTION_CALL_FINISH_REASON in str(excinfo.value)

    async def test_usable_body_survives_a_malformed_call(self, monkeypatch) -> None:
        """本项目不消费原生工具调用：模型顺带写坏一个 function call 但正文可用时，正文照常拿去解析

        这也是它与拦截判据不同形状的地方——拦截是无条件的，这一支只在正文确实为空时成立。
        """
        _install_fake_openai(
            monkeypatch,
            result=_FakeCompletion(
                content='```python\nsend_msg_text(_ck, "在的")\n```',
                finish_reason=MALFORMED_FUNCTION_CALL_FINISH_REASON,
            ),
        )
        response = await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)
        assert response.response_content == '```python\nsend_msg_text(_ck, "在的")\n```'


# ============================================================
# 2. 流式分支（同一套判据的第二处实现）
# ============================================================


class TestStreamContentBlocked:
    """流式：finish_reason 挂在收尾块上，产品代码必须自己把它攒下来

    这一组与上面那组是**同一个判据的两处实现**。任一处漏判，都只是「一半的请求被修好」——
    现场同一个模型组既有开流式的会话也有不开的。
    """

    @pytest.mark.parametrize("reason", sorted(CONTENT_BLOCKED_FINISH_REASONS))
    async def test_every_blocked_reason_is_recognised(self, monkeypatch, reason: str) -> None:
        _install_fake_openai(
            monkeypatch,
            result=[_FakeChunk(content=None, finish_reason=reason, usage=_FakeUsage())],
        )
        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=True)
        assert excinfo.value.finish_reason == reason

    async def test_finish_reason_on_a_chunk_without_delta(self, monkeypatch) -> None:
        """收尾块没有 delta 时也要认出来

        现场那种 `{"choices":[{"index":0,"finish_reason":"prohibited_content"}]}` 的收尾块会被
        `_extract_valid_delta` 当成空块提前丢掉。finish_reason 若记在那次早退**之后**，
        整条流式路径就永远看不到拦截理由——这条用例专门钉住记录点的位置。
        """
        _install_fake_openai(
            monkeypatch,
            result=[
                _FakeChunk(content=""),
                _FakeChunk(finish_reason=GEMINI_BLOCK_REASON, with_delta=False, usage=_FakeUsage()),
            ],
        )
        with pytest.raises(LLMContentBlockedError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=True)
        assert excinfo.value.finish_reason == GEMINI_BLOCK_REASON

    async def test_blocked_wins_over_already_streamed_text(self, monkeypatch) -> None:
        """流到一半被拦：已经吐出来的半句是被截断的，不能当成模型的回答返回"""
        _install_fake_openai(
            monkeypatch,
            result=[
                _FakeChunk(content="```python\nsend_msg_text(_ck, \"我不能"),
                _FakeChunk(finish_reason="content_filter", usage=_FakeUsage()),
            ],
        )
        with pytest.raises(LLMContentBlockedError):
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=True)

    async def test_stream_malformed_tool_call_is_its_own_class(self, monkeypatch) -> None:
        _install_fake_openai(
            monkeypatch,
            result=[
                _FakeChunk(content=None),
                _FakeChunk(finish_reason=MALFORMED_FUNCTION_CALL_FINISH_REASON, usage=_FakeUsage()),
            ],
        )
        with pytest.raises(LLMMalformedToolCallError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=True)
        assert not isinstance(excinfo.value, LLMContentBlockedError)


# ============================================================
# 3. 反向对照：判据没有被吹宽
# ============================================================


class TestJudgementIsNotWidened:
    """普通的空响应必须**还是**普通的空响应

    没有这一组的话，「把所有 finish_reason 都当成拦截」也能让上面全部变绿，
    而代价是 462 条 `NO_CHOICES` 与 2 条 `tool_calls` 全被误报成「内容被挡下来了」。
    """

    @pytest.mark.parametrize("reason", ["stop", "length", "tool_calls", "function_call", None, ""])
    async def test_non_stream_other_reasons_stay_empty_response(self, monkeypatch, reason: Optional[str]) -> None:
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content=None, finish_reason=reason))
        with pytest.raises(EmptyLLMResponseError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)
        assert type(excinfo.value) is EmptyLLMResponseError, "普通空响应不该被升级成拦截或工具调用损坏"
        assert classify_llm_failure(excinfo.value, request_has_image=False) is LLMFailureKind.EMPTY_RESPONSE

    @pytest.mark.parametrize("reason", ["stop", "length", "tool_calls", None])
    async def test_stream_other_reasons_stay_empty_response(self, monkeypatch, reason: Optional[str]) -> None:
        _install_fake_openai(
            monkeypatch,
            result=[_FakeChunk(content=None, finish_reason=reason, usage=_FakeUsage())],
        )
        with pytest.raises(EmptyLLMResponseError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=True)
        assert type(excinfo.value) is EmptyLLMResponseError

    @pytest.mark.parametrize("reason", ["safety_ratings", "unsafe", "content_filter_off", "no_safety"])
    async def test_reason_matching_is_exact_not_substring(self, monkeypatch, reason: str) -> None:
        """按集合精确匹配，不按子串：`safety_ratings` 这类字段名不是拦截理由"""
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content=None, finish_reason=reason))
        with pytest.raises(EmptyLLMResponseError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)
        assert type(excinfo.value) is EmptyLLMResponseError

    async def test_normal_reply_with_stop_is_untouched(self, monkeypatch) -> None:
        """正常回复一条都不能被这次改动拦下来"""
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content="hello", finish_reason="stop"))
        response = await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)
        assert response.response_content == "hello"

    async def test_missing_finish_reason_field_does_not_crash(self, monkeypatch) -> None:
        """字段整个缺席（部分中转站）时按「没有理由」处理，不是抛 AttributeError"""
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content=None, has_finish_reason_field=False))
        with pytest.raises(EmptyLLMResponseError) as excinfo:
            await gen_openai_chat_response(model="m", messages=[], stream_mode=False)
        assert type(excinfo.value) is EmptyLLMResponseError

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("PROHIBITED_CONTENT", "prohibited_content"),
            ("content-filter", "content_filter"),
            ("  stop\n", "stop"),
            ("", ""),
            (None, ""),
            (123, ""),
        ],
    )
    def test_normalize_table(self, raw, expected: str) -> None:
        assert normalize_finish_reason(raw) == expected


# ============================================================
# 4. 分类：防「另一半没修」
# ============================================================


class TestClassification:
    """`LLMContentBlockedError` 是 `EmptyLLMResponseError` 的子类，所以判序就是全部

    继承是刻意的（既有 `except EmptyLLMResponseError` 一行都不用改就能接住新异常），
    代价是 `classify_llm_failure` 里两个 isinstance 的先后顺序变成了正确性的一部分。
    """

    def test_blocked_is_its_own_kind_not_empty_response(self) -> None:
        exc = LLMContentBlockedError(GEMINI_BLOCK_REASON, stream_mode=False)
        kind = classify_llm_failure(exc, request_has_image=False)
        assert kind is LLMFailureKind.CONTENT_BLOCKED
        assert kind is not LLMFailureKind.EMPTY_RESPONSE, "父类判在前面的话新异常会被整个吞掉，用户看到的还是旧文案"

    def test_blocked_classification_ignores_the_image_flag(self) -> None:
        """带不带图都是同一类：拦截的是内容，与请求里有没有图片无关"""
        exc = LLMContentBlockedError("safety", stream_mode=True)
        assert classify_llm_failure(exc, request_has_image=True) is LLMFailureKind.CONTENT_BLOCKED
        assert classify_llm_failure(exc, request_has_image=False) is LLMFailureKind.CONTENT_BLOCKED

    def test_malformed_tool_call_is_classified_as_empty_response(self) -> None:
        """写坏的工具调用重试有意义，和空响应的下一步动作一致，所以**故意**不另立一类"""
        exc = LLMMalformedToolCallError(stream_mode=False)
        kind = classify_llm_failure(exc, request_has_image=False)
        assert kind is LLMFailureKind.EMPTY_RESPONSE
        assert kind is not LLMFailureKind.CONTENT_BLOCKED, "它不是内容被拦，别让用户白改措辞"

    def test_plain_empty_response_is_unchanged(self) -> None:
        exc = EmptyLLMResponseError("非流式响应内容为空")
        assert classify_llm_failure(exc, request_has_image=False) is LLMFailureKind.EMPTY_RESPONSE

    def test_blocked_error_still_satisfies_the_old_except_clauses(self) -> None:
        """继承关系本身就是契约：调用方那些 `except EmptyLLMResponseError` / `except ValueError`
        不改一行也必须接得住，否则这次改动会把一类失败从「有回执」变成「整个回合崩掉」"""
        exc = LLMContentBlockedError(GEMINI_BLOCK_REASON, stream_mode=False)
        assert isinstance(exc, EmptyLLMResponseError)
        assert isinstance(exc, ValueError)
        assert isinstance(LLMMalformedToolCallError(stream_mode=True), EmptyLLMResponseError)


# ============================================================
# 5. 用户回执
# ============================================================


class TestNoticeFolding:
    @pytest.mark.parametrize(
        ("kinds", "expected"),
        [
            # 全部是拦截 → 新文案
            ([LLMFailureKind.CONTENT_BLOCKED], NOTICE_LLM_BLOCKED),
            ([LLMFailureKind.CONTENT_BLOCKED] * 3, NOTICE_LLM_BLOCKED),
            # 混合了别的 → 退回通用文案（与既有 EMPTY_RESPONSE 的处理同构）
            ([LLMFailureKind.CONTENT_BLOCKED, LLMFailureKind.EMPTY_RESPONSE], NOTICE_LLM_FAILED),
            ([LLMFailureKind.CONTENT_BLOCKED, LLMFailureKind.OTHER], NOTICE_LLM_FAILED),
            ([LLMFailureKind.CONTENT_BLOCKED, LLMFailureKind.VISION_UNSUPPORTED], NOTICE_LLM_FAILED),
            ([LLMFailureKind.EMPTY_RESPONSE, LLMFailureKind.CONTENT_BLOCKED], NOTICE_LLM_FAILED),
            # 既有行为不能被这次改动带偏
            ([LLMFailureKind.EMPTY_RESPONSE] * 3, NOTICE_LLM_EMPTY),
            ([LLMFailureKind.OTHER], NOTICE_LLM_FAILED),
            ([], NOTICE_LLM_FAILED),
        ],
    )
    def test_fold_table(self, kinds: List[LLMFailureKind], expected: str) -> None:
        assert describe_llm_failure(kinds) == expected

    def test_blocked_notice_is_distinguishable_and_actionable(self) -> None:
        """这条文案要让用户知道「换个说法」，而不是「再等等」或「再说一遍原话」

        复用 `NOTICE_LLM_EMPTY` 的话上面那张表照样能过（两个常量指向同一个字符串时），
        所以这里显式钉死它是**另一句**、并且真的讲了下一步动作。
        """
        assert NOTICE_LLM_BLOCKED != NOTICE_LLM_EMPTY
        assert NOTICE_LLM_BLOCKED != NOTICE_LLM_FAILED
        assert "换个说法" in NOTICE_LLM_BLOCKED
        assert NOTICE_LLM_BLOCKED.endswith("QwQ")

    def test_blocked_notice_carries_no_upstream_text(self) -> None:
        """既有约束：回执取自固定集合，不带上游原文——连 finish_reason 这种枚举值也不带"""
        for reason in CONTENT_BLOCKED_FINISH_REASONS:
            assert reason not in NOTICE_LLM_BLOCKED
        assert MALFORMED_FUNCTION_CALL_FINISH_REASON not in NOTICE_LLM_BLOCKED


# ============================================================
# 6. 三层串起来
# ============================================================


class TestEndToEndWiring:
    """LLM 层抛出的**那个**异常，要真的能一路走成用户看到的那句话

    分开测的话，「分类函数认得一个没人抛的异常类」这种半成品是全绿的。
    """

    async def test_non_stream_block_reaches_the_blocked_notice(self, monkeypatch) -> None:
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content=None, finish_reason=GEMINI_BLOCK_REASON))
        with pytest.raises(EmptyLLMResponseError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)
        kinds = [classify_llm_failure(excinfo.value, request_has_image=False)] * 3
        assert describe_llm_failure(kinds) == NOTICE_LLM_BLOCKED

    async def test_stream_block_reaches_the_blocked_notice(self, monkeypatch) -> None:
        _install_fake_openai(
            monkeypatch,
            result=[_FakeChunk(content=None, finish_reason=GEMINI_BLOCK_REASON, usage=_FakeUsage())],
        )
        with pytest.raises(EmptyLLMResponseError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=True)
        kinds = [classify_llm_failure(excinfo.value, request_has_image=False)] * 3
        assert describe_llm_failure(kinds) == NOTICE_LLM_BLOCKED

    async def test_ordinary_empty_still_reaches_the_empty_notice(self, monkeypatch) -> None:
        """462 条 `NO_CHOICES` 那一类的回执不能被这次改动改掉"""
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content=None, finish_reason="stop"))
        with pytest.raises(EmptyLLMResponseError) as excinfo:
            await gen_openai_chat_response(model="gemini-3.6-flash", messages=[], stream_mode=False)
        kinds = [classify_llm_failure(excinfo.value, request_has_image=False)] * 3
        assert describe_llm_failure(kinds) == NOTICE_LLM_EMPTY


# ============================================================
# 7. 结构断言：两条分支都判了
# ============================================================


def _raised_names(statements) -> Set[str]:
    names: Set[str] = set()
    for statement in statements:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(exc, ast.Name):
                names.add(exc.id)
    return names


class TestBothBranchesAreGuarded:
    def test_stream_and_non_stream_branches_both_raise_the_blocked_error(self) -> None:
        """`gen_openai_chat_response` 里 `if stream_mode:` 的**两侧**都要有拦截判据

        行为用例已经分别覆盖了两条分支，这条是它们的结构化复述：本项目反复栽在
        「同一个判据只改了第一处」上，所以额外留一条能一眼看出漏了哪一侧的断言。
        """
        source = Path(openai_module.__file__).read_text(encoding="utf-8")
        function = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "gen_openai_chat_response"
        )
        branch = next(
            node
            for node in ast.walk(function)
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "stream_mode"
        )
        assert "LLMContentBlockedError" in _raised_names(branch.body), "流式分支没判拦截"
        assert "LLMContentBlockedError" in _raised_names(branch.orelse), "非流式分支没判拦截"
        assert "LLMMalformedToolCallError" in _raised_names(branch.body), "流式分支没判工具调用损坏"
        assert "LLMMalformedToolCallError" in _raised_names(branch.orelse), "非流式分支没判工具调用损坏"

    def test_gemini_stream_and_non_stream_branches_both_raise_the_blocked_error(self) -> None:
        """Gemini 原生分支的 `if stream_mode:` 两侧同样要有拦截判据

        OpenAI 兼容分支与 Gemini 原生分支各有一对自己/非流式语句；只给其中一条分支
        补判据，另一条分支上的拒答就会重新变成「一个字都没返回」。
        """
        source = Path(openai_module.__file__).read_text(encoding="utf-8")
        function = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "gen_openai_chat_response"
        )
        stream_branches = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "stream_mode"
        ]
        assert len(stream_branches) >= 2, "应当同时有 OpenAI 分支与 Gemini 分支的 stream_mode 判断"
        for index, branch in enumerate(stream_branches):
            assert "LLMContentBlockedError" in _raised_names(branch.body), f"第 {index} 条流式分支没判拦截"
            assert "LLMContentBlockedError" in _raised_names(branch.orelse), f"第 {index} 条非流式分支没判拦截"
