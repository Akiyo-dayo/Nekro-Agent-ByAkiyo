"""三组改动之间**接线**的回归测试。

本轮的修复分三处落地，各自都有表驱动用例：

- `resolver.py`：围栏提取 + 「没有可执行代码」判定（`tests/test_agent_response_parsing.py`）
- `openai.py` / `model_capability.py` / `failure_feedback.py`：安全拦截可辨识
  （`tests/test_agent_llm_content_block.py`）
- `run_agent.py` / `message_service.py`：跳过沙盒 + 无进展提前收尾
  （`tests/test_agent_round_control.py`）

**但那三份用例都在跨组的接缝上用了替身**，接线缺陷恰好就藏在替身后面：

- `test_agent_round_control.py` 把 `parse_chat_response` 整个 monkeypatch 成返回 `_Parsed`
  替身的脚本，于是「真解析器输出的字段名」与「`run_agent` 用 `getattr` 读的字段名」
  从来没有在同一次执行里碰过面。两边差一个字母是**静默**的：`getattr(..., default=False)`
  会让所有回合退回改造前的行为，两组的用例仍然全绿。
- `test_agent_llm_content_block.py` 分别直接调用 `classify_llm_failure(exc)` 与
  `describe_llm_failure(kinds)`，中间那一跳——`send_agent_request` 把 kind 收进
  `failure_kinds` 再塞进 `AllLLMRequestsFailedError(kinds=...)`——两端都被跳过了。
- `test_agent_round_control.py` 对 `message_service` 的分派做的是**源码文本**断言
  （`_branch_body` 里出现了常量名），它认不出运行期的 isinstance 顺序问题。

所以这里只钉跨文件的接缝，且刻意**不替换契约的任何一端**：解析器用真的、异常类用真的、
迭代循环跑真代码。组内判据不重复验证。

全部确定性：无网络、无 sleep、无随机。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, List, Tuple, cast

import pytest

# 下面两行复用兄弟用例里已经搭好的可注入环境，而不是再抄一份一百行的替身。
# `tests/` 下没有 `__init__.py`，pytest 的 prepend 导入模式会把该目录放进 sys.path。
from test_agent_round_control import _Harness, _SandboxResult
from test_llm_capability_and_failure import _group, _make_config, _Recorder, _text_message

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.failure_feedback import (
    NOTICE_LLM_BLOCKED,
    NOTICE_LLM_EMPTY,
    NOTICE_LLM_FAILED,
    NOTICE_ROUND_NO_CODE,
    NOTICE_ROUND_STALLED,
    NOTICE_ROUND_UNFINISHED,
    describe_llm_failure,
)
from nekro_agent.services.agent.model_capability import LLMFailureKind
from nekro_agent.services.agent.openai import (
    EmptyLLMResponseError,
    LLMContentBlockedError,
    LLMMalformedToolCallError,
    OpenAIResponse,
)
from nekro_agent.services.agent.resolver import ParsedCodeRunData, parse_chat_response
from nekro_agent.services.agent.run_agent import (
    AgentNoExecutableCodeError,
    AgentRoundStalledError,
    AgentRoundUnfinishedError,
    AllLLMRequestsFailedError,
    is_code_missing,
    send_agent_request,
)

# --------------------------------------------------------------------------------------
# 线上真实响应形态（取自测试部署 prompts_error 日志与 exec_code 表，不要改写措辞）
# --------------------------------------------------------------------------------------

REFUSAL_ZH = "抱歉，我无法协助处理这个请求。这类内容涉及安全风险，我们换个话题聊聊吧。"
REFUSAL_EN = "I'm sorry, but I can't help with that request."
PROSE_EXPLANATION = (
    "好的，我明白你的意思了。\n\n"
    "你想让我把群公告改一下，对吧？不过我需要先确认几件事：\n"
    "1. 新的公告内容是什么\n"
    "2. 需要不需要 @全体成员\n"
)
NORMAL_CODE_RESPONSE = "```python\nsend_msg_text(_ck, 'hi')\n```"
NORMAL_CODE_EXTRACTED = "send_msg_text(_ck, 'hi')"


@pytest.fixture()
def _isolated_log_dirs(tmp_path, monkeypatch):
    """`send_agent_request` 失败时会落盘 prompt 日志，别写进仓库目录"""
    monkeypatch.setattr(run_agent_module, "PROMPT_LOG_DIR", str(tmp_path / "prompts"))
    monkeypatch.setattr(run_agent_module, "PROMPT_ERROR_LOG_DIR", str(tmp_path / "prompts_error"))


# ======================================================================================
# 接缝 1：解析器写的字段名 == 迭代循环读的字段名
# ======================================================================================


class TestParserToLoopFieldContract:
    """`parse_chat_response` 的输出直接喂给 `run_agent.is_code_missing`，中间不放替身

    这是本轮唯一一处「两边差一个字母就静默失效」的接缝：`is_code_missing` 用
    `getattr(parsed, "code_missing", False)` 读，字段名对不上时它恒返回 False，
    于是拒答照旧进沙盒，而 B 组和 C 组的用例**全都还是绿的**。
    """

    @pytest.mark.parametrize(
        ("label", "raw_content", "expected_missing"),
        [
            ("中文安全拒答", REFUSAL_ZH, True),
            ("英文安全拒答", REFUSAL_EN, True),
            ("纯说明文字（含编号列表）", PROSE_EXPLANATION, True),
            ("think 之后仍然是拒答", f"<think>用户想要的东西我不能做</think>\n{REFUSAL_ZH}", True),
            ("空代码块", "```python\n```", True),
            ("正常代码块", NORMAL_CODE_RESPONSE, False),
            ("代码块 + 尾随说明（B2 形态）", f"{NORMAL_CODE_RESPONSE}\n以上就是我的回复", False),
            ("裸代码（无围栏）", "send_msg_text(_ck, 'hi')", False),
        ],
    )
    def test_real_parse_result_is_read_by_the_loop_predicate(
        self,
        label: str,
        raw_content: str,
        expected_missing: bool,
    ) -> None:
        parsed = parse_chat_response(raw_content)
        # 两条一起断言：只断言 is_code_missing 的话，字段名写错时它恒 False，
        # 「有代码」那几行照样绿，只有「没代码」那几行会红——但那正是最容易被
        # 当成 flaky 而放过的一半。加上 code_content 才能指认是哪一端错了。
        assert is_code_missing(parsed) is expected_missing, f"{label}: 循环读到的判定与解析器的结论不一致"
        assert (parsed.code_content == "") is expected_missing, f"{label}: code_content 与 code_missing 不同步"

    def test_predicate_reads_the_field_that_the_model_actually_declares(self) -> None:
        """字段名的正向锚点：真 `ParsedCodeRunData`（不是替身）显式置位时必须被读到"""
        assert "code_missing" in ParsedCodeRunData.model_fields
        parsed = ParsedCodeRunData(raw_content="x", code_content="", thought_chain="", code_missing=True)
        assert is_code_missing(parsed) is True

    def test_predicate_default_is_false_for_legacy_construction(self) -> None:
        """`command/built_in/debug.py` 直接构造这个模型且不传新字段，那条路必须照旧进沙盒"""
        parsed = ParsedCodeRunData(raw_content="print(1)", code_content="print(1)", thought_chain="")
        assert is_code_missing(parsed) is False


# ======================================================================================
# 接缝 2：真解析器驱动真迭代循环 —— 拒答一次都不许进沙盒
# ======================================================================================


def _install_real_parser_harness(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    responses: List[str],
) -> List[str]:
    """装好 `_Harness`，然后把 `parse_chat_response` 换回**真的**，并按脚本喂 LLM 响应

    `_Harness.install` 里那两处 monkeypatch 在此被后装的覆盖（同一属性后设者生效）。
    其余外部依赖（沙盒、广播、模板、DB）沿用 `_Harness` 的替身。
    """
    harness.install(monkeypatch)

    served: List[str] = []

    async def _send(**kwargs: Any) -> Tuple[Any, Any, List[str]]:
        index = len(served)
        if index >= len(responses):
            raise AssertionError(f"LLM 脚本只有 {len(responses)} 条，却被取用了第 {index + 1} 次")
        content = responses[index]
        served.append(content)
        harness.llm_calls += 1
        return (
            SimpleNamespace(response_content=content, use_model="gemini-3.6-flash", thought_chain=""),
            SimpleNamespace(CHAT_MODEL="gemini-3.6-flash"),
            [],
        )

    monkeypatch.setattr(run_agent_module, "send_agent_request", _send)
    monkeypatch.setattr(run_agent_module, "parse_chat_response", parse_chat_response)
    return served


class TestRefusalNeverReachesTheSandbox:
    """用户报的那条故障（「拒答被当成 Python 执行」）的端到端复现

    `_Harness` 的沙盒脚本留空：一旦有任何一次进了沙盒，替身会抛 `_ScriptExhausted`，
    用例以「沙盒被调用」而不是「断言不成立」的形态响亮地红。
    """

    async def test_chinese_refusal_skips_sandbox_and_stops_early(self, monkeypatch) -> None:
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_real_parser_harness(harness, monkeypatch, [REFUSAL_ZH, REFUSAL_ZH])

        with pytest.raises(AgentNoExecutableCodeError) as excinfo:
            await harness.run()

        assert harness.sandbox_calls == 0, "拒答文本一次都不该被送进容器执行"
        assert excinfo.value.attempts == 2
        # 提前收尾必须真的省下迭代：max_retry=3 却只请求了 2 次 LLM
        assert harness.llm_calls == 2

    async def test_prose_explanation_skips_sandbox(self, monkeypatch) -> None:
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_real_parser_harness(harness, monkeypatch, [PROSE_EXPLANATION, REFUSAL_EN])

        with pytest.raises(AgentNoExecutableCodeError):
            await harness.run()

        assert harness.sandbox_calls == 0

    async def test_model_recovers_on_second_try(self, monkeypatch) -> None:
        """第一轮拒答、第二轮给出代码：必须恢复正常，而不是被早停一刀切掉"""
        harness = _Harness(
            max_retry=3,
            parses=[],
            sandbox_results=[_SandboxResult(output="", stop_type=ExecStopType.NORMAL)],
        )
        _install_real_parser_harness(harness, monkeypatch, [REFUSAL_ZH, NORMAL_CODE_RESPONSE])

        await harness.run()

        assert harness.sandbox_codes == [NORMAL_CODE_EXTRACTED]

    async def test_normal_code_still_reaches_the_sandbox(self, monkeypatch) -> None:
        """反向对照：没有这一条，上面几条也可能因为「循环压根没跑起来」而蒙混通过"""
        harness = _Harness(
            max_retry=3,
            parses=[],
            sandbox_results=[_SandboxResult(output="", stop_type=ExecStopType.NORMAL)],
        )
        _install_real_parser_harness(harness, monkeypatch, [NORMAL_CODE_RESPONSE])

        await harness.run()

        assert harness.sandbox_codes == [NORMAL_CODE_EXTRACTED]
        assert harness.sandbox_calls == 1

    async def test_trailing_prose_after_fence_still_executes_only_the_code(self, monkeypatch) -> None:
        """B2 线上形态：闭合围栏后面还有一句话。修复前整个正则不匹配 → 含围栏的整段进沙盒"""
        harness = _Harness(
            max_retry=3,
            parses=[],
            sandbox_results=[_SandboxResult(output="", stop_type=ExecStopType.NORMAL)],
        )
        _install_real_parser_harness(
            harness,
            monkeypatch,
            [f"{NORMAL_CODE_RESPONSE}\n以上就是我的回复，请查收~"],
        )

        await harness.run()

        assert harness.sandbox_codes == [NORMAL_CODE_EXTRACTED]
        assert "```" not in harness.sandbox_codes[0]
        assert "以上就是我的回复" not in harness.sandbox_codes[0]


# ======================================================================================
# 接缝 3：拦截异常 → send_agent_request 收集的 kinds → 用户回执
# ======================================================================================


def _sequence_behaviour(exceptions: List[BaseException]) -> Callable[[str, bool], OpenAIResponse]:
    """按调用次序抛出指定异常（用完即报错，防止静默多调）"""
    state = {"index": 0}

    def _behaviour(_model: str, _has_image: bool) -> OpenAIResponse:
        index = state["index"]
        if index >= len(exceptions):
            raise AssertionError(f"上游脚本只有 {len(exceptions)} 条，却被取用了第 {index + 1} 次")
        state["index"] = index + 1
        raise exceptions[index]

    return _behaviour


@pytest.mark.usefixtures("_isolated_log_dirs")
class TestBlockedResponseReachesTheUserNotice:
    """`LLMContentBlockedError` → `classify_llm_failure` → `kinds` → `describe_llm_failure`

    中间那一跳（`failure_kinds.append(kind)` 与 `AllLLMRequestsFailedError(kinds=...)`）
    此前只在 `EMPTY_RESPONSE` 上被覆盖过，`CONTENT_BLOCKED` 走的是同一段代码但从未被跑过。
    """

    async def test_blocked_every_attempt_yields_the_blocked_notice(self, monkeypatch) -> None:
        primary = _group("p", "gemini-3.6-flash")
        recorder = _Recorder(
            _sequence_behaviour([LLMContentBlockedError("prohibited_content", stream_mode=False)] * 3),
        )
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary})
        with pytest.raises(AllLLMRequestsFailedError) as excinfo:
            await send_agent_request([_text_message()], cast(Any, config))

        assert excinfo.value.kinds == [LLMFailureKind.CONTENT_BLOCKED] * 3
        assert describe_llm_failure(excinfo.value.kinds) == NOTICE_LLM_BLOCKED
        # 回执里不许出现上游的任何原文/理由字符串
        assert "prohibited_content" not in describe_llm_failure(excinfo.value.kinds)

    async def test_malformed_tool_call_stays_on_the_empty_notice(self, monkeypatch) -> None:
        """写坏的工具调用是可重试的采样事故，不该借 `EmptyLLMResponseError` 的子类身份
        被误升级成「换个说法」——那会让用户白改措辞"""
        primary = _group("p", "gemini-3.6-flash")
        recorder = _Recorder(_sequence_behaviour([LLMMalformedToolCallError(stream_mode=True)] * 3))
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary})
        with pytest.raises(AllLLMRequestsFailedError) as excinfo:
            await send_agent_request([_text_message()], cast(Any, config))

        assert excinfo.value.kinds == [LLMFailureKind.EMPTY_RESPONSE] * 3
        assert describe_llm_failure(excinfo.value.kinds) == NOTICE_LLM_EMPTY

    async def test_mixed_failures_fall_back_to_the_generic_notice(self, monkeypatch) -> None:
        """一次被拦 + 两次网络错：说「换个说法」会把用户往错的方向引"""
        primary = _group("p", "gemini-3.6-flash")
        recorder = _Recorder(
            _sequence_behaviour(
                [
                    LLMContentBlockedError("safety", stream_mode=False),
                    ConnectionError("upstream unreachable"),
                    ConnectionError("upstream unreachable"),
                ],
            ),
        )
        monkeypatch.setattr(run_agent_module, "gen_openai_chat_response", recorder)

        config = _make_config(primary, "p", {"p": primary})
        with pytest.raises(AllLLMRequestsFailedError) as excinfo:
            await send_agent_request([_text_message()], cast(Any, config))

        assert excinfo.value.kinds == [
            LLMFailureKind.CONTENT_BLOCKED,
            LLMFailureKind.OTHER,
            LLMFailureKind.OTHER,
        ]
        assert describe_llm_failure(excinfo.value.kinds) == NOTICE_LLM_FAILED


# ======================================================================================
# 接缝 4：message_service 的 isinstance 分派
# ======================================================================================


class TestRoundFailureDispatchIsUnambiguous:
    """`message_service._run_chat_agent_task` 用一条 isinstance elif 链分派四类回合失败。

    它的注释声称「三条异常互不继承，所以顺序不影响判定结果」。那句话是**代码正确性的前提**
    （现在的链把 `AgentRoundUnfinishedError` 放在最后），但没有任何用例钉住它：
    哪天有人让某一类去继承另一类，源码文本断言照样绿，而运行期分派会开始被先命中的分支吃掉。
    """

    _ROUND_ERRORS = (
        AllLLMRequestsFailedError,
        AgentNoExecutableCodeError,
        AgentRoundStalledError,
        AgentRoundUnfinishedError,
    )

    @pytest.mark.parametrize("outer", _ROUND_ERRORS, ids=lambda cls: cls.__name__)
    @pytest.mark.parametrize("inner", _ROUND_ERRORS, ids=lambda cls: cls.__name__)
    def test_round_error_classes_do_not_inherit_from_each_other(self, outer, inner) -> None:
        if outer is inner:
            return
        assert not issubclass(outer, inner), (
            f"{outer.__name__} 继承了 {inner.__name__}：isinstance 链的顺序从此决定回执文案，"
            f"而 message_service 的注释明确假设顺序无关"
        )

    def test_each_round_failure_has_its_own_notice(self) -> None:
        """四条回执两两不同：复制粘贴错常量时，源码文本断言（常量名出现过）认不出来"""
        notices = [NOTICE_ROUND_NO_CODE, NOTICE_ROUND_STALLED, NOTICE_ROUND_UNFINISHED, NOTICE_LLM_BLOCKED]
        assert len(set(notices)) == len(notices)

    def test_blocked_error_remains_a_subclass_of_empty_response(self) -> None:
        """反过来，这一条继承关系是**必需**的：既有的 `except EmptyLLMResponseError`
        调用点一行都没改，断掉继承就是把它们全部漏接"""
        assert issubclass(LLMContentBlockedError, EmptyLLMResponseError)
        assert issubclass(LLMMalformedToolCallError, EmptyLLMResponseError)


# ======================================================================================
# 接缝 5：run_agent 三条收尾路径的异常类型互不串台
# ======================================================================================


class TestEarlyStopPathsRaiseDistinctTypes:
    """C 组的两条早停路径与原有的「烧满迭代」必须能被 `message_service` 区分开"""

    async def test_stalled_round_raises_stalled_not_unfinished(self, monkeypatch) -> None:
        from test_agent_round_control import _error_result

        harness = _Harness(
            max_retry=5,
            parses=[],
            sandbox_results=[
                _error_result("Error sending text message to chat: make sure the chat key is valid"),
                _error_result("Error sending text message to chat: make sure the chat key is valid"),
            ],
        )
        _install_real_parser_harness(
            harness,
            monkeypatch,
            [NORMAL_CODE_RESPONSE, NORMAL_CODE_RESPONSE],
        )

        with pytest.raises(AgentRoundStalledError) as excinfo:
            await harness.run()

        assert excinfo.value.repeats == 2
        # max_retry=5，实际只烧了 2 轮
        assert harness.sandbox_calls == 2

    async def test_distinct_errors_do_not_trigger_the_stall_path(self, monkeypatch) -> None:
        """两次**不同**的错误说明模型确实在改东西，必须让它把迭代跑完"""
        from test_agent_round_control import _error_result

        harness = _Harness(
            max_retry=2,
            parses=[],
            sandbox_results=[
                _error_result("KeyError: 'avatar_url'"),
                _error_result("Error sending text message to chat: make sure the chat key is valid"),
            ],
        )
        # 迭代循环在**每一轮末尾**都会再请求一次 LLM，所以 max_retry=2 要喂 1+2=3 条
        _install_real_parser_harness(
            harness,
            monkeypatch,
            [NORMAL_CODE_RESPONSE] * 3,
        )

        with pytest.raises(AgentRoundUnfinishedError):
            await harness.run()

        assert harness.sandbox_calls == 2
