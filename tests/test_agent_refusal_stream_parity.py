"""供应商拒答收尾的两个**变异存活点**：流式分支与异常自身携带的原文

## 由来（两条都是本棒变异测试跑出来的存活变异，不是凭空补的用例）

兄弟用例 `test_agent_refusal_round_exit.py` / `test_agent_refusal_tail_iteration.py`
已经把「判出来之后处置对不对」「每一次解析都判没判」钉死了。本棒对接线做了 20 个变异，
其中 18 个当场被现有用例杀掉，**活下来两个**：

- **M8「流式分支不判」**：在 `raise_if_provider_refusal` 开头插一句
  `if getattr(response, "stream_mode", False): return`，796 条用例**全绿**。
  原因是现有用例的 LLM 替身返回的是 `SimpleNamespace(response_content=..., ...)`，
  身上压根没有 `stream_mode` 字段，于是「流式」这一半从来没被驱动过。
  而线上同一个模型组既可能开流式也可能不开（`STREAM_MODE` 是配置项），
  真出现这个缺陷时受影响的正好是开了流式的那一半部署——本项目已经**五次**栽在
  「同一个判据的第二处实现/第二条分支没跟上」，`test_agent_llm_content_block.py`
  开头那段由来记的就是同一件事。

- **M5c「上游原文塞进异常的 `marker`」**：把 `marker=refusal_marker` 改成
  `marker=response.response_content`，796 条用例**全绿**。既有用例只断言
  `excinfo.value.marker` 为真（「异常要带上标志串」），从没断言过它**是什么**。
  而 `AgentProviderRefusalError` 的 docstring 写的是「记的是我们自己表里命中的那条标志串
  （不是上游原文）」——那条约束当时没有任何断言在保护。它不是纯理论风险：
  `message_service` 拿到这个异常后会 `logger.exception(f"执行失败: {e}")`，
  异常上带的东西就是运维日志里看得见的东西。

所以本文件补的是两件事：**两条分支都要驱动**、**异常这个出口也要扫**。

## 断言的基本要求

沿用兄弟用例的口径：「抛了异常」不算断言。每条都落在可数、可辨识的量上——
LLM 被调用了几次、沙盒被调用了几次、异常是哪一类、marker 的**内容**出自哪张表、
运维日志里到底出现了什么字符串。

流式那一组刻意**不**自己造 `OpenAIResponse`：造一个假的等于把「流式真的会产出这段正文」
这个前提假设掉。这里走真的 `gen_openai_chat_response`（只替换 openai SDK 客户端），
让被测对象就是产品代码在流式下会构造出来的**那一个**对象。

全部确定性：无网络、无 sleep、无随机。
"""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

# 复用兄弟用例已经搭好的替身，不再抄一份（项目 §0.5）。
# `tests/` 下没有 `__init__.py`，pytest 的 prepend 导入模式会把该目录放进 sys.path。
from test_agent_llm_content_block import _FakeChunk, _FakeCompletion, _FakeUsage, _install_fake_openai
from test_agent_mutation_survivors import _MessageServiceHarness
from test_agent_refusal_round_exit import (
    _UPSTREAM_SIGNATURE_PHRASES,
    NORMAL_CODE_RESPONSE,
    REAL_PROVIDER_REFUSAL,
    _assert_no_upstream_text,
)
from test_agent_round_control import _Harness, _SandboxResult, make_traceback

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.failure_feedback import (
    NOTICE_ROUND_PROVIDER_REFUSAL,
    failure_notice_throttle,
)
from nekro_agent.services.agent.openai import OpenAIResponse, gen_openai_chat_response
from nekro_agent.services.agent.provider_refusal import (
    PROVIDER_BOILERPLATE,
    PROVIDER_REFUSAL_SIGNATURES,
    detect_provider_refusal,
)
from nekro_agent.services.agent.resolver import parse_chat_response
from nekro_agent.services.agent.run_agent import (
    PROVIDER_REFUSAL_MARKER_GROUPS,
    AgentProviderRefusalError,
    is_provider_refusal,
)

# 每一趟都能跑但都以 ERROR 收尾的正常代码块，各不相同（避开无进展检测）
_ERROR_CODE = [f"```python\nstep_{idx}()\n```" for idx in range(6)]

_SANDBOX_ERRORS = [
    _SandboxResult(
        output=make_traceback(line=idx + 1, call=f"step_{idx}()", message=f"boom {idx}", exc=exc),
        stop_type=ExecStopType.ERROR,
    )
    for idx, exc in enumerate(["ValueError", "TypeError", "KeyError"])
]


@pytest.fixture(autouse=True)
def _clean_throttle():
    """回执冷却闸门是进程内单例，用例之间不清会互相压制"""
    failure_notice_throttle.reset()
    yield
    failure_notice_throttle.reset()


# ======================================================================================
# 夹具：让真的 `gen_openai_chat_response` 在两条分支上各造一个 `OpenAIResponse`
# ======================================================================================

# 把逐字原文切成若干块喂进流式分支。切点是**刻意挑的**：每一条标志串都被切在块边界上，
# 于是「谁要是改成逐块判定」会立刻失效。`test_every_marker_is_split_across_chunks` 钉住这一点。
_STREAM_CHUNKS: Tuple[str, ...] = (
    "The prompt could not be sub",
    "mitted. The prompt contains sensitive words that vio",
    "late Google's [Generative AI Prohib",
    "ited Use policy](https://policies.goo",
    "gle.com/terms/generative-ai/use-policy). Try rephrasing the prompt. ",
    "If you think this was an error, [send feedback]",
    "(https://ai.google.dev/gemini-api/docs/troubleshooting).",
)


async def _upstream_response(monkeypatch: pytest.MonkeyPatch, text: str, *, stream_mode: bool) -> OpenAIResponse:
    """走**真** `gen_openai_chat_response` 造出一个 `OpenAIResponse`，只替换 openai SDK 客户端

    这是本组的地基：流式那一半的正文由产品代码自己的分块拼装逻辑产出，
    不是测试端拼好再塞进去的。
    """
    if stream_mode:
        chunks: List[Any] = [_FakeChunk(content=piece) for piece in _split_like_a_stream(text)]
        chunks.append(_FakeChunk(content=None, finish_reason="stop", usage=_FakeUsage(), with_delta=False))
        _install_fake_openai(monkeypatch, result=chunks)
    else:
        _install_fake_openai(monkeypatch, result=_FakeCompletion(content=text, finish_reason="stop"))
    return await gen_openai_chat_response(
        model="gemini-3.7-flash-high",
        messages=[],
        stream_mode=stream_mode,
    )


def _split_like_a_stream(text: str) -> List[str]:
    """逐字原文用预设切点，其它文本按固定长度切——两种都保证「不止一块」"""
    if text == REAL_PROVIDER_REFUSAL:
        return list(_STREAM_CHUNKS)
    size = max(1, len(text) // 3)
    return [text[i : i + size] for i in range(0, len(text), size)] or [text]


def _message_blob(messages: List[OpenAIChatMessage]) -> str:
    return json.dumps([message.to_dict() for message in messages], ensure_ascii=False)


def _install_scripted_llm(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    responses: List[Any],
) -> List[str]:
    """按脚本喂**响应对象**（不是字符串）驱动真 `run_agent`

    与兄弟用例里的同名助手差一处：那边喂的是正文字符串、由助手包成 `SimpleNamespace`，
    这里必须能喂**真的 `OpenAIResponse`**，否则 `stream_mode` 这个字段根本进不了被测代码。
    """
    harness.install(monkeypatch)
    contexts: List[str] = []

    async def _send(**kwargs: Any) -> Tuple[Any, Any, List[str]]:
        index = harness.llm_calls
        if index >= len(responses):
            raise AssertionError(f"LLM 脚本只有 {len(responses)} 条，却被取用了第 {index + 1} 次")
        contexts.append(_message_blob(kwargs["messages"]))
        harness.llm_calls += 1
        return (responses[index], SimpleNamespace(CHAT_MODEL="gemini-3.7-flash-high"), [])

    monkeypatch.setattr(run_agent_module, "send_agent_request", _send)
    monkeypatch.setattr(run_agent_module, "parse_chat_response", parse_chat_response)
    return contexts


def _plain_response(text: str, *, stream_mode: bool) -> SimpleNamespace:
    """轻量替身：只在不需要真 SDK 的地方用（正常代码块之类）"""
    return SimpleNamespace(
        response_content=text,
        use_model="gemini-3.7-flash-high",
        thought_chain="",
        stream_mode=stream_mode,
    )


# ======================================================================================
# 1. 夹具自检：流式那一半确实是「分块拼出来的」，且标志串真的被切开了
# ======================================================================================


class TestStreamFixtureIsHonest:
    """没有这一组，下面所有「流式也被判到了」的用例都可能是在测一个假的流。"""

    def test_the_chunks_reassemble_into_the_verbatim_text(self) -> None:
        assert "".join(_STREAM_CHUNKS) == REAL_PROVIDER_REFUSAL, "分块夹具拼不回逐字原文，用例测的不是线上那段文本"
        assert len(_STREAM_CHUNKS) >= 5, "块太少，切点覆盖不到所有标志串"

    def test_every_marker_is_split_across_chunks(self) -> None:
        """每一条标志串都必须被切在块边界上

        只切开其中一条是不够的：剩下那条会在单个块内完整出现，
        于是「改成逐块判定」这种变异照样能命中它、照样全绿。
        兄弟用例 `test_every_marker_group_is_split_by_the_wrapped_fixture` 是同一个道理的折行版。
        """
        normalized = " ".join(REAL_PROVIDER_REFUSAL.lower().split())
        checked = 0
        for group in PROVIDER_REFUSAL_MARKER_GROUPS:
            for marker in group:
                if marker not in normalized:
                    continue
                checked += 1
                assert not any(marker in chunk.lower() for chunk in _STREAM_CHUNKS), (
                    f"标志串 {marker!r} 完整地落在某一个块里，流式分块用例形同虚设"
                )
        assert checked >= 3, f"只检查到 {checked} 条标志串，切点没覆盖到该覆盖的东西"

    async def test_stream_branch_really_goes_through_the_chunk_assembler(self, monkeypatch) -> None:
        """流式那一路产出的 `OpenAIResponse` 是产品代码拼出来的，且 `stream_mode` 为真"""
        response = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=True)

        assert isinstance(response, OpenAIResponse)
        assert response.stream_mode is True, "流式分支产出的响应没有把 stream_mode 标上，本组的前提失效"
        assert response.response_content == REAL_PROVIDER_REFUSAL


# ======================================================================================
# 2. 两条分支产出的正文逐字相同，判定结论也必须相同
# ======================================================================================


class TestBothBranchesYieldTheSameText:
    """「流式与非流式各判一次」是 `openai.py` 里已经写死的教训（见该文件开头那段由来）。

    本轮的判定挂在**两条分支汇流之后**的解析点上，所以这一组要证明的是「汇流点确实是同一个」。
    """

    @pytest.mark.parametrize("stream_mode", [False, True], ids=["非流式", "流式"])
    async def test_verbatim_refusal_survives_both_branches(self, monkeypatch, stream_mode: bool) -> None:
        response = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=stream_mode)

        assert response.response_content == REAL_PROVIDER_REFUSAL
        assert response.stream_mode is stream_mode

    @pytest.mark.parametrize("stream_mode", [False, True], ids=["非流式", "流式"])
    async def test_the_predicate_agrees_on_both_branches(self, monkeypatch, stream_mode: bool) -> None:
        response = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=stream_mode)

        assert detect_provider_refusal(response.response_content) is not None
        assert is_provider_refusal(response.response_content) is True

    async def test_two_branches_produce_identical_content(self, monkeypatch) -> None:
        """一条断言把两边直接对齐：将来谁在某一条分支上加了裁剪/改写，这里立刻红"""
        streamed = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=True)
        monkeypatch.undo()
        plain = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=False)

        assert streamed.response_content == plain.response_content
        assert streamed.stream_mode != plain.stream_mode, "两个响应的 stream_mode 一样，这条对比没有意义"


# ======================================================================================
# 3. 接线：流式响应同样零重试收尾（杀 M8）
# ======================================================================================


class TestWiringCoversTheStreamBranch:
    """变异 M8（`if response.stream_mode: return`）活下来的地方就在这里。

    每条都用**真** `OpenAIResponse`（`stream_mode=True`）驱动真 `run_agent`。
    """

    @pytest.mark.parametrize("stream_mode", [False, True], ids=["非流式", "流式"])
    async def test_first_round_refusal_exits_regardless_of_stream_mode(self, monkeypatch, stream_mode: bool) -> None:
        response = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=stream_mode)
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_scripted_llm(harness, monkeypatch, [response])

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        assert harness.llm_calls == 1, f"stream_mode={stream_mode} 时供应商拒答仍然重试了"
        assert harness.sandbox_calls == 0, "拒答文本一次都不该被送进容器"

    @pytest.mark.parametrize("stream_mode", [False, True], ids=["非流式", "流式"])
    async def test_tail_refusal_exits_regardless_of_stream_mode(self, monkeypatch, stream_mode: bool) -> None:
        """末轮盲区那条路径在流式下同样成立（两个缺陷叠在一起时最难发现）"""
        response = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=stream_mode)
        harness = _Harness(max_retry=2, parses=[], sandbox_results=_SANDBOX_ERRORS[:1])
        _install_scripted_llm(
            harness,
            monkeypatch,
            [
                _plain_response(_ERROR_CODE[0], stream_mode=stream_mode),
                response,
            ],
        )

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        # 回合不再多发最后一次请求之后，「末轮」= 本回合最后一次真正发出的请求，`max_retry=2` 时就是第 2 次
        assert harness.llm_calls == 2, "脚本没有被跑到末轮，用例没测到目标路径"
        assert harness.sandbox_calls == 1

    @pytest.mark.parametrize("stream_mode", [False, True], ids=["非流式", "流式"])
    async def test_normal_stream_response_still_runs(self, monkeypatch, stream_mode: bool) -> None:
        """反向对照：正常代码块在流式下照常进沙盒

        没有这一条，把判定改成「流式一律收尾」也能让上面两条全绿。
        """
        harness = _Harness(
            max_retry=3,
            parses=[],
            sandbox_results=[_SandboxResult(output="", stop_type=ExecStopType.NORMAL)],
        )
        _install_scripted_llm(harness, monkeypatch, [_plain_response(NORMAL_CODE_RESPONSE, stream_mode=stream_mode)])

        await harness.run()

        assert harness.sandbox_calls == 1
        assert harness.llm_calls == 1

    @pytest.mark.parametrize("stream_mode", [False, True], ids=["非流式", "流式"])
    async def test_user_notice_is_the_same_on_both_branches(self, monkeypatch, stream_mode: bool) -> None:
        """真 `run_agent` 抛出的那个异常对象再喂给真的分派链：两条分支的用户回执必须一致"""
        response = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=stream_mode)
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_scripted_llm(harness, monkeypatch, [response])

        with pytest.raises(BaseException) as excinfo:  # noqa: PT011  - 要的就是「到底抛了哪一种」
            await harness.run()
        raised = excinfo.value

        monkeypatch.undo()
        service_harness = _MessageServiceHarness()
        service_harness.install(monkeypatch, raises=raised)
        await service_harness.run()

        assert [text for _chat, text in service_harness.sent] == [NOTICE_ROUND_PROVIDER_REFUSAL]


# ======================================================================================
# 4. 异常自身是第三个出口：`marker` 与异常消息都不许带上游原文（杀 M5c）
# ======================================================================================


def _all_legal_markers() -> set:
    """`match_provider_refusal` **可能**返回的全部字符串，逐条枚举

    这是一张**穷举白名单**，不是「像不像」的启发式。它的三个来源与产品代码一一对应：

    1. `PROVIDER_REFUSAL_MARKER_GROUPS` 拼出来的整组标签（首选返回值）；
    2. 标签表里没有对应条目时的兜底：判定方给出的 `matched_phrases`——
       域名档是「每组各中一条」的笛卡尔积，boilerplate 档是整组；
    3. 再兜底的 `provider` 名。

    三处**全部是我们写在代码里的字面量**，一个字符都不来自上游正文。
    用穷举白名单而不是「不含某些片段」的扫描，是因为标志串本身就是从拒绝页上逐字抄下来的
    （它们必须逐字才认得出），所以「不含上游片段」那种口径在这里天然是自相矛盾的：
    真正的契约不是「长得不像上游正文」，而是「**只能**是这张有限表里的某一条」。
    """
    labels = {" + ".join(group) for group in PROVIDER_REFUSAL_MARKER_GROUPS}
    for signature in PROVIDER_REFUSAL_SIGNATURES:
        labels.add(signature.provider)
        labels.update(" + ".join(combo) for combo in itertools.product(*signature.phrase_groups))
    for entry in PROVIDER_BOILERPLATE:
        labels.add(entry.provider)
        labels.update(" + ".join(group) for group in entry.groups)
    return labels


class TestTheExceptionCarriesNoUpstreamText:
    """变异 M5c（`marker=response.response_content`）活下来的地方就在这里。

    `AgentProviderRefusalError` 的 docstring 写着「`marker` 记的是我们自己表里命中的那条
    标志串（不是上游原文）」。改造前**没有任何断言**在保护这句话，而
    `message_service` 会 `logger.exception(f"执行失败: {e}")` —— 异常上带的东西
    就是运维日志里看得见的东西。
    """

    def test_the_whitelist_itself_is_meaningful(self) -> None:
        """自检：白名单非空、有限，而且**装不下**整页拒绝说明

        没有这一条，白名单被写成「什么都收」时下面几条会变成恒真的空断言。
        """
        labels = _all_legal_markers()
        assert labels, "白名单是空的，下面的成员断言会恒假/恒真，两边都没有意义"
        assert REAL_PROVIDER_REFUSAL not in labels, "整页拒绝说明落在白名单里，M5c 那类泄漏就查不出来了"
        assert max(len(label) for label in labels) < len(REAL_PROVIDER_REFUSAL), (
            "白名单里最长的一条比整页还长，长度这一侧的保护形同虚设"
        )
        # 扫描器自检：`_UPSTREAM_SIGNATURE_PHRASES` 下面还要用来扫异常消息与运维日志
        assert _UPSTREAM_SIGNATURE_PHRASES
        with pytest.raises(AssertionError):
            _assert_no_upstream_text(REAL_PROVIDER_REFUSAL, where="自检")

    @pytest.mark.parametrize("stream_mode", [False, True], ids=["非流式", "流式"])
    async def test_marker_is_exactly_one_of_our_own_labels(self, monkeypatch, stream_mode: bool) -> None:
        """穷举白名单的成员断言——变异 M5c 在这里必红"""
        response = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=stream_mode)
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_scripted_llm(harness, monkeypatch, [response])

        with pytest.raises(AgentProviderRefusalError) as excinfo:
            await harness.run()

        marker = excinfo.value.marker
        assert marker, "异常要带上命中的标志串，否则日志里无法回答『靠哪一条判出来的』"
        assert marker in _all_legal_markers(), (
            f"异常带出去的标志串 {marker!r} 不在我们自己的表里，说明它是上游正文的切片："
            "`message_service` 会把这个异常打进运维日志"
        )

    @pytest.mark.parametrize("stream_mode", [False, True], ids=["非流式", "流式"])
    async def test_no_link_or_domain_escapes_through_the_exception(self, monkeypatch, stream_mode: bool) -> None:
        """标志串是逐字抄来的整句，但链接与域名**从来不是**标志串的一部分

        它是与白名单正交的第二道断言：白名单哪天被人放宽，这一条仍然拦得住最敏感的那部分。
        """
        response = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=stream_mode)
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_scripted_llm(harness, monkeypatch, [response])

        with pytest.raises(AgentProviderRefusalError) as excinfo:
            await harness.run()

        for forbidden in ("policies.google.com", "ai.google.dev", "https://", "[send feedback]"):
            assert forbidden not in excinfo.value.marker, f"异常的 marker 带出了 {forbidden}"
            assert forbidden not in str(excinfo.value), f"异常消息带出了 {forbidden}"
        # 异常消息是固定文案，可以用比 marker 严得多的口径：一个上游片段都不许有
        _assert_no_upstream_text(str(excinfo.value), where="异常消息")

    async def test_tail_refusal_exception_is_clean_too(self, monkeypatch) -> None:
        """末轮那条路径共用同一个闭包，但它的 `iteration_index` 不同——单独钉一遍"""
        response = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=False)
        harness = _Harness(max_retry=2, parses=[], sandbox_results=_SANDBOX_ERRORS[:1])
        _install_scripted_llm(
            harness,
            monkeypatch,
            [_plain_response(_ERROR_CODE[0], stream_mode=False), response],
        )

        with pytest.raises(AgentProviderRefusalError) as excinfo:
            await harness.run()

        assert harness.llm_calls == 2
        assert excinfo.value.marker in _all_legal_markers(), "末轮那条路径把上游正文塞进了异常的 marker"
        _assert_no_upstream_text(str(excinfo.value), where="末轮异常消息")

    async def test_message_service_log_never_prints_the_upstream_page(self, monkeypatch) -> None:
        """接线终点：`message_service` 对这个异常打的那行日志里不许出现上游原文

        它打的是 `logger.exception(f"执行失败: {e}")`——异常消息与 marker 只要有一个带了
        原文，运维日志就会把整页拒绝说明抄进去。这条从**日志内容**这一侧再钉一遍，
        免得将来有人把 marker 拼进异常消息。
        """
        response = await _upstream_response(monkeypatch, REAL_PROVIDER_REFUSAL, stream_mode=False)
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_scripted_llm(harness, monkeypatch, [response])

        with pytest.raises(BaseException) as excinfo:  # noqa: PT011
            await harness.run()
        raised = excinfo.value

        monkeypatch.undo()

        from nekro_agent.services import message_service as msgsvc_module

        recorder = _Harness(max_retry=1, parses=[], sandbox_results=[]).logger
        service_harness = _MessageServiceHarness()
        service_harness.install(monkeypatch, raises=raised)
        monkeypatch.setattr(msgsvc_module, "logger", recorder)

        await service_harness.run()

        blob = "\n".join(recorder.texts("ERROR") + recorder.texts("WARNING") + recorder.texts("INFO"))
        assert "执行失败" in blob, "分派链那行日志没打出来，本条断言会变成恒真"
        _assert_no_upstream_text(blob, where="message_service 的运维日志")
