"""Agent 回合的**请求预算**与两条提前收尾判据的**开关**

## 本文件钉住的三件事

### 1. #58：跑满迭代的回合不再固定浪费最后一次 LLM 请求

`run_agent` 的迭代循环在**每一趟末尾**发一次请求供**下一趟**消费：

    请求 #1                      -> 第 1 趟消费
    第 1 趟末尾发请求 #2         -> 第 2 趟消费
    ...
    第 M 趟末尾发请求 #(M+1)     -> **没有第 M+1 趟，无人消费**

于是每个跑满 `AI_SCRIPT_MAX_RETRY_TIMES` 的回合都固定多花一次完整请求，含全量上下文
（实测单次请求体可达数百 KB）。改造后最后一趟直接 `break`，回合恰好发 `M` 次请求，
**每一次的响应都会被消费**。

这一组的断言全部落在可数的量上：`llm_calls` / `sandbox_calls` / `parse_calls` 三者的关系、
以及 `publish_runtime_state` 的事件序列——省一次请求不能以弄坏事件流为代价，
那一条正是改动前被点名的风险。

### 2. #60 的另一半：两个阈值现在有配置项与会话覆盖层

`STALL_REPEAT_THRESHOLD` / `CODE_MISSING_MAX_ATTEMPTS` 改造前是模块常量，
所有部署（含单实例、平台方）一律生效，线上误伤时唯一止血手段是改代码重部署。
现在两者分别由 `SESSION_STALL_REPEAT_THRESHOLD` / `SESSION_CODE_MISSING_MAX_ATTEMPTS`
经会话覆盖层解析，取 0 即整条关掉。**默认值保持 2、行为逐字不变**，本文件有用例钉死。

**接线单独测**：每条判据都有一组「把配置调成非默认值，行为必须跟着变」的用例。
把 `resolve_*` 的读取删掉、改回读模块常量，这些用例立刻红；
只测 `resolve_*` 纯函数而不驱动 `run_agent`，则杀不掉「算出来了但循环没用它」这类变异，
所以两者都写。

### 3. #60 的第三条：`SECURITY` 档的退化签名是**有意保留**的

`SECURITY` 根本不进沙盒、`sandbox_output` 恒为空，签名永远是 `SECURITY|<empty>`，
于是触发提前收尾的是**任意两次连续的 one-time-code 泄漏**而不是「同一个错重复」。
评估结论是不改（理由见 `run_agent.STALL_TRACKED_STOP_TYPES` 上方那段注释），
所以这里把**现状**与**不改的两个前提**一起钉住：令牌一个字都不许进签名/日志/后台事件，
以及被误伤的部署能靠调阈值自救。

全部确定性：无网络、无 sleep、无随机（一次性令牌也被钉成定值）。
"""

from __future__ import annotations

import asyncio
import os as _os
from typing import Any, cast

import pytest

# 复用兄弟用例已经搭好的可注入环境，不再抄一份替身（项目 §0.5）
from test_agent_round_control import (
    SEND_TEXT_FAILURE,
    _Harness,
    _Parsed,
    _SandboxResult,
    make_traceback,
)

from nekro_agent.core.config import CoreConfig
from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.run_agent import (
    CODE_MISSING_MAX_ATTEMPTS,
    STALL_REPEAT_THRESHOLD,
    AgentNoExecutableCodeError,
    AgentRoundStalledError,
    AgentRoundUnfinishedError,
    RoundStallDetector,
    _normalize_early_exit_limit,  # noqa: PLC2701  被测对象本身
    resolve_code_missing_limit,
    resolve_stall_threshold,
)

# 每一趟的异常**类型**都不同 —— 归一化会把裸数字吃掉（`boom 1` 与 `boom 2` 同签名），
# 想让签名各不相同只能换类型名，否则无进展判据会抢先收尾，用例就测不到「跑满」那条路径
_DISTINCT_EXCEPTIONS = [
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "AttributeError",
    "ZeroDivisionError",
    "LookupError",
    "RuntimeError",
]


def _distinct_error(index: int) -> _SandboxResult:
    """第 index 趟的沙盒报错，签名与其它趟必然不同"""
    return _SandboxResult(
        output=make_traceback(
            line=index + 1,
            call=f"step_{index}()",
            message="boom",
            exc=_DISTINCT_EXCEPTIONS[index % len(_DISTINCT_EXCEPTIONS)],
        ),
        stop_type=ExecStopType.ERROR,
    )


def _burn_every_iteration(max_retry: int, **kwargs: Any) -> _Harness:
    """每一趟都能进沙盒、都以**互不相同**的 ERROR 收尾 → 一路烧到迭代上限

    解析脚本刚好给 `max_retry` 条：多发一次请求就会多解析一次，`_ScriptExhausted` 会响亮地红。
    """
    return _Harness(
        max_retry=max_retry,
        parses=[_Parsed(code_content=f"step_{index}()") for index in range(max_retry)],
        sandbox_results=[_distinct_error(index) for index in range(max_retry)],
        **kwargs,
    )


def _same_error(index: int) -> _SandboxResult:
    """逐字相同的故障（行号/实参不同，归一化后同签名）——线上占大头的那一类"""
    return _SandboxResult(
        output=make_traceback(
            line=10 + index,
            call=f'send_msg_text(chat, "第{index}次")',
            message=SEND_TEXT_FAILURE,
        ),
        stop_type=ExecStopType.ERROR,
    )


class _FixedUrandomOs:
    """把 `os.urandom` 钉成定值，其余属性原样转发给真的 `os`

    `run_agent` 用 `os.urandom(8).hex()` 生成一次性令牌。要驱动 `SECURITY` 分支
    （判据是 `one_time_code in code_content`），用例必须先知道那个令牌是什么。
    转发其余属性是为了不把「将来 run_agent 里多用了一处 os.xxx」变成本文件的哑炮。
    """

    def __init__(self, value: bytes) -> None:
        self._value = value

    def urandom(self, size: int) -> bytes:
        return (self._value * (size // len(self._value) + 1))[:size]

    def __getattr__(self, name: str) -> Any:
        return getattr(_os, name)


_FROZEN_TOKEN_BYTES = bytes(range(0xA0, 0xA8))
FROZEN_ONE_TIME_CODE = _FROZEN_TOKEN_BYTES.hex()  # "a0a1a2a3a4a5a6a7"


def _freeze_one_time_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_agent_module, "os", _FixedUrandomOs(_FROZEN_TOKEN_BYTES))


def _leaking_code(tag: str) -> str:
    """一段**泄漏了一次性令牌**的代码；`tag` 让两趟的代码正文不同"""
    return f"send_msg_text(_ck, '{tag} {FROZEN_ONE_TIME_CODE}')"


def _all_log_text(harness: _Harness) -> str:
    return "\n".join(record.text for record in harness.logger.records)


# ======================================================================================
# 1. #58：没有任何一次请求的结果是无人消费的
# ======================================================================================


class TestNoUnconsumedTailRequest:
    """回合发出的请求数 == 真正被消费的响应数。改造前恒等于「迭代上限 + 1」。"""

    @pytest.mark.parametrize("max_retry", [1, 2, 3, 4, 8])
    def test_burned_round_spends_exactly_one_request_per_iteration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        max_retry: int,
    ) -> None:
        """线上真实取值分布在 1~8，逐个钉一遍：off-by-one 只在某些预算下才现形"""
        harness = _burn_every_iteration(max_retry)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == max_retry, "迭代次数被改动了，本用例测的已经不是原来那条路径"
        assert harness.llm_calls == max_retry, (
            f"跑满 {max_retry} 趟迭代却发了 {harness.llm_calls} 次请求："
            "最后一趟末尾那次请求的结果无人消费，改造前正是白烧的那一次"
        )
        assert harness.parse_calls == harness.llm_calls, "有响应被解析了却没有对应的请求（或反过来）"
        assert harness.llm_calls == harness.sandbox_calls, "有一次请求的产物没有任何一趟消费它"

    def test_the_saved_request_is_exactly_one_per_burned_round(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """省下来的正好是一次，不是两次也不是零次（改造前 = max_retry + 1）"""
        max_retry = 3
        harness = _burn_every_iteration(max_retry)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert (max_retry + 1) - harness.llm_calls == 1

    def test_runtime_event_sequence_survives_the_saved_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """省一次请求不能把事件流弄坏——这条是改动前被点名的风险，单独钉

        少的只该是「为下一趟发请求」的 `iterating`（下一趟根本不存在），
        沙盒相关事件与收尾事件一条不许少。
        """
        max_retry = 3
        harness = _burn_every_iteration(max_retry)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        phases = harness.phases()
        assert phases.count("sandbox_running") == max_retry, "本趟自己的沙盒事件被顺手删掉了"
        assert phases.count("sandbox_stopped") == max_retry
        assert phases.count("iterating") == max_retry - 1, (
            f"`iterating` 应当与「真正发出去的迭代请求」一一对应（{max_retry - 1} 条），实得 {phases.count('iterating')}"
        )
        assert phases[-1] == "failed", "收尾事件没了，前端进度条会一直挂着"
        assert all(event.iteration_total == max_retry + 1 for event in harness.events), (
            "进度条分母被改动了：`iteration_total` 是既有语义，不该因为省一次请求而变"
        )

    @pytest.mark.parametrize("max_retry", [1, 2, 3, 8])
    def test_the_skipped_assembly_is_actually_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        max_retry: int,
    ) -> None:
        """`break` 的**位置**要钉住，不能只钉「省了一次请求」

        `render_history_data` 不是纯装配。它一路展开是 `_inject_memory_context`
        → `_build_enhanced_memory_recall_plan` → **又一次 `gen_openai_chat_response`**
        （`LLMUsageScene.HISTORY_PLAN`，走计费与用量记账）→ `retrieve_memories_batch`
        （每条 query 一次 embedding + 一次 Qdrant 查询）→ `stage_round_recall`（只记录，
        不写库）。

        也就是说：把 `break` 往后挪到这次调用之后，`llm_calls` **一次都不会变**
        （被跳过的仍然是那次迭代请求），但每个烧满的回合会重新多花一次规划请求与
        一批向量检索。被跳过的那次渲染本来就不会强化，只数请求数杀不掉这类变异。
        """
        harness = _burn_every_iteration(max_retry)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.history_renders == max_retry - 1, (
            f"`render_history_data` 跑了 {harness.history_renders} 趟，应当只跑 {max_retry - 1} 趟"
            "（末轮那趟只为喂一次无人消费的请求而存在）：`break` 被挪到它之后了"
        )
        # 正向对照：它确实是**跟着迭代请求走**的，而不是被整个删掉了
        assert harness.history_renders == harness.llm_calls - 1

    def test_iterating_events_never_announce_an_iteration_that_never_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """改造前最后一条 `iterating` 报的是第 `max_retry + 1` 趟——那一趟从来不存在"""
        max_retry = 4
        harness = _burn_every_iteration(max_retry)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        iterating_indexes = [event.iteration_index for event in harness.events if event.phase == "iterating"]
        assert iterating_indexes == list(range(2, max_retry + 1))

    def test_final_failed_event_still_names_a_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """循环外那条 `failed` 用的是 `llm_response.use_model`

        跳过末次请求后 `llm_response` 保持为**产出本趟代码**的那次响应，
        这条断言防的是「变量没了/变成 None，收尾事件从此不带模型名」。
        """
        harness = _burn_every_iteration(3)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        failed = [event for event in harness.events if event.phase == "failed"]
        assert failed, "没有收尾事件"
        assert failed[-1].model_name, "收尾事件丢了模型名，排查时对不上是哪个模型在跑"
        # 收尾事件必须报「本轮最后一格」，否则前端进度条会在失败的一刻**倒退**。
        # 分母被上面那条用例单独钉着，这里只钉「分子追平分母」这条关系——复核实测：
        # 把这里的 `iteration_index` 改成 1，定向 286 条全绿，这一格原先没人看。
        assert failed[-1].iteration_index == failed[-1].iteration_total, (
            f"收尾事件报的是第 {failed[-1].iteration_index}/{failed[-1].iteration_total} 格，进度条会往回跳"
        )

    def test_code_missing_on_the_last_iteration_does_not_spend_another_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """最后一趟落在 `code_missing` 分支上时同样不再多发一次

        这一条走的是循环体的另一半（不进沙盒那条），改造前它也会在末尾白发一次请求。
        `code_missing_streak` 只到 1，够不上提前收尾，所以仍以「迭代耗尽」收场。
        """
        harness = _Harness(
            max_retry=2,
            parses=[
                _Parsed(code_content="step_0()"),
                _Parsed(code_content="", code_missing=True),
            ],
            sandbox_results=[_distinct_error(0)],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.llm_calls == 2
        assert harness.sandbox_calls == 1, "没有可执行代码的那一趟不该进沙盒"

    def test_successful_round_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """成功路径一次请求都不该多、也不该少"""
        harness = _Harness(
            max_retry=3,
            parses=[_Parsed()],
            sandbox_results=[_SandboxResult(output="done", stop_type=ExecStopType.NORMAL)],
        )
        harness.install(monkeypatch)

        asyncio.run(harness.run())

        assert harness.llm_calls == 1
        assert harness.phases()[-1] == "completed"

    def test_early_exit_paths_still_stop_even_earlier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """提前收尾的回合不受影响：请求数仍由判据决定，而不是由迭代上限决定"""
        harness = _Harness(
            max_retry=8,
            parses=[_Parsed(), _Parsed()],
            sandbox_results=[_same_error(0), _same_error(1)],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError):
            asyncio.run(harness.run())

        assert harness.llm_calls == 2
        assert harness.sandbox_calls == 2


# ======================================================================================
# 2. #58 的连带：「这是最后一次机会」要落在真正会被执行的那一趟上
# ======================================================================================


class TestLastRetryHintRidesAnExecutedRequest:
    """改造前这句提示恰好挂在**结果被丢弃**的那次请求上，一次都没生效过。

    它的用途是让模型在最后一次机会里把「为什么做不完」写进代码块发出去；挂错位置 =
    模型写的那段说明永远不会被执行，用户只能收到通用回执。#58 去掉那次请求之后，
    这句提示如果还按老口径算就彻底不会出现，所以一并挪到最后一次**会被消费**的请求上。
    """

    @pytest.mark.parametrize("max_retry", [2, 3, 4])
    def test_hint_rides_the_request_whose_code_actually_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        max_retry: int,
    ) -> None:
        harness = _burn_every_iteration(max_retry)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        prompts = harness.llm_last_user_text
        assert len(prompts) == max_retry
        assert sum("This is the last retry" in text for text in prompts) == 1, (
            f"「最后一次机会」出现了 {sum('This is the last retry' in t for t in prompts)} 次，应当恰好一次"
        )
        assert "This is the last retry" in prompts[-1], (
            "提示没有落在最后一次请求上：那次请求的产物是本回合最后一段会被执行的代码"
        )
        assert f"(Iteration times: {max_retry}/{max_retry})" in prompts[-1], (
            "计数与提示不同步，模型会读到「这是最后一次机会」却配着一个没到头的计数"
        )

    @pytest.mark.parametrize("max_retry", [2, 3, 4])
    def test_no_request_announces_an_iteration_beyond_the_budget(
        self,
        monkeypatch: pytest.MonkeyPatch,
        max_retry: int,
    ) -> None:
        harness = _burn_every_iteration(max_retry)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        for text in harness.llm_last_user_text:
            assert f"(Iteration times: {max_retry + 1}/" not in text, "计数报到了预算之外的一趟"

    def test_single_iteration_budget_sends_no_iteration_request_at_all(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`max_retry=1`：循环外那一次请求就是全部，循环里一次迭代请求都不发"""
        harness = _burn_every_iteration(1)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.llm_calls == 1
        assert all("Iteration times" not in text for text in harness.llm_last_user_text)


# ======================================================================================
# 3. #60：无进展阈值的配置项与开关
# ======================================================================================


class TestStallThresholdIsConfigurable:
    """接线：`SESSION_STALL_REPEAT_THRESHOLD` 必须真的决定循环的行为"""

    def test_default_behaviour_is_verbatim_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """没配过的部署：仍然是「第二次同签名就收尾」，一个字都没变"""
        harness = _Harness(
            max_retry=8,
            parses=[_Parsed(), _Parsed()],
            sandbox_results=[_same_error(0), _same_error(1)],
        )
        assert harness.stall_threshold == STALL_REPEAT_THRESHOLD == 2, "harness 的默认值已经不是模块默认值了"
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError) as excinfo:
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 2
        assert excinfo.value.repeats == 2

    @pytest.mark.parametrize("threshold", [2, 3, 4, 5])
    def test_threshold_is_read_from_the_session_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
        threshold: int,
    ) -> None:
        """调高阈值 → 同一个故障要连续出现更多次才收尾

        这是**接线**用例：把 `resolve_stall_threshold` 的读取删掉、改回读模块常量，
        `threshold in (3, 4, 5)` 三条立刻红（它们会在第 2 趟就收尾）。
        """
        harness = _Harness(
            max_retry=8,
            stall_threshold=threshold,
            parses=[_Parsed() for _ in range(threshold)],
            sandbox_results=[_same_error(index) for index in range(threshold)],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError) as excinfo:
            asyncio.run(harness.run())

        assert harness.sandbox_calls == threshold, "阈值没有被读进循环"
        assert excinfo.value.repeats == threshold

    @pytest.mark.parametrize("threshold", [0, -1])
    def test_zero_turns_the_check_off_entirely(self, monkeypatch: pytest.MonkeyPatch, threshold: int) -> None:
        """止血开关：判据误伤线上时不必改代码重部署，取 0 即退回烧满迭代的旧行为"""
        max_retry = 4
        harness = _Harness(
            max_retry=max_retry,
            stall_threshold=threshold,
            parses=[_Parsed() for _ in range(max_retry)],
            sandbox_results=[_same_error(index) for index in range(max_retry)],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == max_retry, "判据关掉了却仍然提前收尾"
        assert harness.llm_calls == max_retry

    def test_one_is_raised_to_the_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """取 1 = 第一次报错就放弃，等于把迭代重试机制整个废掉；静默抬到 2

        0 已经有明确语义（关闭），所以 1 只可能是误配。
        """
        harness = _Harness(
            max_retry=6,
            stall_threshold=1,
            parses=[_Parsed(), _Parsed()],
            sandbox_results=[_same_error(0), _same_error(1)],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 2, "阈值 1 生效了：第一次报错就放弃，重试机制被废掉"

    def test_raising_the_threshold_does_not_disable_the_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """反向对照：调高不等于关掉，够次数照样收尾且**没有**跑满预算"""
        harness = _Harness(
            max_retry=8,
            stall_threshold=3,
            parses=[_Parsed(), _Parsed(), _Parsed()],
            sandbox_results=[_same_error(index) for index in range(3)],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 3 < harness.max_retry


# ======================================================================================
# 4. #60：无可执行代码上限的配置项与开关
# ======================================================================================


class TestCodeMissingLimitIsConfigurable:
    """接线：`SESSION_CODE_MISSING_MAX_ATTEMPTS` 必须真的决定循环的行为"""

    def test_default_behaviour_is_verbatim_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(
            max_retry=5,
            parses=[_Parsed(code_content="", code_missing=True) for _ in range(2)],
            sandbox_results=[],
        )
        assert harness.code_missing_limit == CODE_MISSING_MAX_ATTEMPTS == 2
        harness.install(monkeypatch)

        with pytest.raises(AgentNoExecutableCodeError) as excinfo:
            asyncio.run(harness.run())

        assert harness.llm_calls == 2
        assert excinfo.value.attempts == 2
        assert harness.sandbox_calls == 0

    @pytest.mark.parametrize("limit", [2, 3, 4])
    def test_limit_is_read_from_the_session_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
        limit: int,
    ) -> None:
        """接线用例：把 `resolve_code_missing_limit` 的读取删掉，`limit in (3, 4)` 立刻红"""
        harness = _Harness(
            max_retry=8,
            code_missing_limit=limit,
            parses=[_Parsed(code_content="", code_missing=True) for _ in range(limit)],
            sandbox_results=[],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentNoExecutableCodeError) as excinfo:
            asyncio.run(harness.run())

        assert harness.llm_calls == limit, "上限没有被读进循环"
        assert excinfo.value.attempts == limit
        assert harness.sandbox_calls == 0

    @pytest.mark.parametrize("limit", [0, -3])
    def test_zero_turns_off_the_early_exit_but_not_the_sandbox_gate(
        self,
        monkeypatch: pytest.MonkeyPatch,
        limit: int,
    ) -> None:
        """关掉的只是**提前收尾**

        「没有可执行代码就不进沙盒」是安全闸门不是启发式：把它一起关掉，
        整段自然语言（典型是模型的安全拒答）会重新被当成 Python 送进容器执行。
        """
        max_retry = 3
        harness = _Harness(
            max_retry=max_retry,
            code_missing_limit=limit,
            parses=[_Parsed(code_content="", code_missing=True) for _ in range(max_retry)],
            sandbox_results=[],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 0, "关掉阈值把「不进沙盒」这道安全闸门也一起关掉了"
        assert harness.llm_calls == max_retry
        assert "sandbox_running" not in harness.phases()

    def test_one_is_raised_to_the_floor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """取 1 = 第一次没给代码就放弃，那次「格式打滑重出一次」的机会没了"""
        harness = _Harness(
            max_retry=5,
            code_missing_limit=1,
            parses=[
                _Parsed(code_content="", code_missing=True),
                _Parsed(code_content="print('ok')"),
            ],
            sandbox_results=[_SandboxResult(output="done", stop_type=ExecStopType.NORMAL)],
        )
        harness.install(monkeypatch)

        asyncio.run(harness.run())

        assert harness.llm_calls == 2, "上限 1 生效了：模型拿不到那次重出机会"
        assert harness.sandbox_codes == ["print('ok')"]


# ======================================================================================
# 5. 归一化本体：两条判据共用同一份取值语义
# ======================================================================================


class TestEarlyExitLimitNormalization:
    """纯函数表驱动。它不替代上面的接线用例——只证明算得对，证明不了循环真的用了它。"""

    # (原始取值, 期望生效值)
    CASES = [
        ("负数当成关闭", -5, 0),
        ("零就是关闭", 0, 0),
        ("一抬到下限", 1, 2),
        ("默认值原样", 2, 2),
        ("调高原样", 3, 3),
        ("调很高原样", 10, 10),
        ("覆盖层里的字符串", "4", 4),
        ("覆盖层里的字符串零", "0", 0),
    ]

    @pytest.mark.parametrize(("name", "raw", "expected"), CASES, ids=[case[0] for case in CASES])
    def test_table(self, name: str, raw: Any, expected: int) -> None:
        # `default` 给一个谁都不是的哨兵：这张表里的每一格都该由取值本身决定，
        # 一旦哪一格开始回落到默认，断言会直接打出 4242 而不是悄悄等于 2
        assert _normalize_early_exit_limit(raw, default=4242) == expected, name

    @pytest.mark.parametrize("garbage", [None, "abc", object()])
    def test_unparsable_values_fall_back_to_the_default(self, garbage: Any) -> None:
        """折不动就按默认值走，而不是让整轮对话因为一条历史配置炸掉

        用一个**谁的默认值都不是**的哨兵取值：两条判据今天的默认值恰好都是 2，
        拿 `STALL_REPEAT_THRESHOLD` 去断言等于什么都没断言（把 `default` 参数整个忽略、
        写死任一常量都能绿）。
        """
        sentinel = 4242
        assert _normalize_early_exit_limit(garbage, default=sentinel) == sentinel

    def test_resolvers_read_their_own_field(self) -> None:
        """两个 resolver 不能读串——读串了，调其中一个会把另一条判据也改掉"""
        config = cast(
            CoreConfig,
            type("_Cfg", (), {"SESSION_STALL_REPEAT_THRESHOLD": 7, "SESSION_CODE_MISSING_MAX_ATTEMPTS": 4})(),
        )
        assert resolve_stall_threshold(config) == 7
        assert resolve_code_missing_limit(config) == 4

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0, 0), (-4, 0), (1, 2), (2, 2), (6, 6), ("3", 3)],
        ids=["关闭", "负数也是关闭", "抬到下限", "默认", "调高", "字符串"],
    )
    def test_resolvers_normalize_and_do_not_hand_back_the_raw_value(self, raw: Any, expected: int) -> None:
        """两个 resolver **自己**就要归一化，不能指望下游替它补

        `RoundStallDetector` 恰好也会再归一化一次，于是「`resolve_stall_threshold` 直接返回
        原始配置值」这个变异在只驱动 `run_agent` 的用例下是活的（复核变异 N3）。可 resolver
        是公开函数、文档上写着「0 = 运维已关闭该判据」，下一个调用点（例如后台显示"当前生效值"）
        未必还有那层兜底。这里直接对着 resolver 断言，把那层依赖切断。
        """
        config = cast(
            CoreConfig,
            type("_Cfg", (), {"SESSION_STALL_REPEAT_THRESHOLD": raw, "SESSION_CODE_MISSING_MAX_ATTEMPTS": raw})(),
        )
        assert resolve_stall_threshold(config) == expected
        assert resolve_code_missing_limit(config) == expected

    def test_each_resolver_falls_back_to_its_own_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """脏配置的回落值必须是**这条判据自己的**默认值

        两条判据的默认值今天都是 2，于是「`resolve_code_missing_limit` 回落到
        `STALL_REPEAT_THRESHOLD`」与正确实现在行为上完全等价 —— 直接断言「等于 2」也好、
        断言「等于 `CODE_MISSING_MAX_ATTEMPTS`」也好，两边算出来都是 2，**一条都分不出来**
        （复核变异 N4 第一次就是这么活下来的：连拿 spy 记下「传进去的是哪个 default」也没用，
        记下来的还是两个相等的 2）。

        唯一能分开的办法是**先把两个常量撑开成不同的取值**再看谁跟着谁走。
        两个 resolver 都在调用时才从模块全局取这两个常量，所以 monkeypatch 拿得住。

        这不是钻牛角尖：`CODE_MISSING_MAX_ATTEMPTS` 的注释里白纸黑字写着它
        「**不跟着** `AI_SCRIPT_MAX_RETRY_TIMES` 走」，两者本来就是各自独立演化的量。
        谁先动了自己那份默认值，另一条就会在遇到脏配置时静默回落到错误的常量上，
        而在那之前**没有任何用例会红**。
        """
        stall_sentinel, missing_sentinel = 31, 47  # 互不相等，且都不是 2
        monkeypatch.setattr(run_agent_module, "STALL_REPEAT_THRESHOLD", stall_sentinel)
        monkeypatch.setattr(run_agent_module, "CODE_MISSING_MAX_ATTEMPTS", missing_sentinel)
        config = cast(
            CoreConfig,
            type("_Cfg", (), {"SESSION_STALL_REPEAT_THRESHOLD": "脏", "SESSION_CODE_MISSING_MAX_ATTEMPTS": "脏"})(),
        )

        assert resolve_stall_threshold(config) == stall_sentinel, "无进展阈值回落到了别的判据的默认值"
        assert resolve_code_missing_limit(config) == missing_sentinel, (
            "「无可执行代码」上限回落到了 `STALL_REPEAT_THRESHOLD`——两条判据的默认值今天恰好都是 2，"
            "所以这个错在真实取值下一点声音都不会出"
        )

    @pytest.mark.parametrize(("threshold", "enabled"), [(0, False), (-2, False), (1, True), (2, True), (9, True)])
    def test_detector_shares_the_same_semantics(self, threshold: int, enabled: bool) -> None:
        """`RoundStallDetector` 不许另写一套判定——两份实现各自演化正是本仓的老病"""
        detector = RoundStallDetector(threshold=threshold)
        assert detector.enabled is enabled
        assert detector.threshold == _normalize_early_exit_limit(threshold, default=STALL_REPEAT_THRESHOLD)

    def test_disabled_detector_never_stalls_no_matter_how_many_repeats(self) -> None:
        detector = RoundStallDetector(threshold=0)
        for _ in range(20):
            detector.note("ERROR|同一个错")
        assert detector.repeats == 20, "关闭态仍要记账，否则日志里连「重复了多少次」都答不上来"
        assert detector.stalled is False


# ======================================================================================
# 6. #60 第三条：`SECURITY` 的退化签名是有意保留的
# ======================================================================================


class TestSecuritySignatureStaysCoarse:
    """评估结论是**不改**，所以这里钉的是「现状」+「不改的两个前提」

    前提一：真正有区分度的签名只能来自模型那段代码，而那段代码里就有一次性令牌本身，
            签名会进日志/异常/后台事件 —— 为省一次重试反手泄漏本判据要保护的东西，方向是反的。
    前提二：被误伤的部署能靠 `SESSION_STALL_REPEAT_THRESHOLD` 自救，不必改代码重部署。
    """

    def test_two_consecutive_leaks_end_the_round_early(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**两段不同的代码**、各自泄漏一次 → 照样判成无进展

        这正是「签名结构性退化」的现象本身：判的不是「同一个错重复」而是
        「任意两次连续的泄漏」。登记在案、有意保留。
        """
        harness = _Harness(
            max_retry=5,
            parses=[
                _Parsed(code_content=_leaking_code("第一段")),
                _Parsed(code_content=_leaking_code("完全不同的第二段")),
            ],
            sandbox_results=[],
        )
        _freeze_one_time_code(monkeypatch)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError) as excinfo:
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 0, "`SECURITY` 分支根本不该进沙盒"
        assert excinfo.value.signature == "SECURITY|<empty>"
        assert excinfo.value.repeats == 2
        assert harness.llm_calls == 2

    def test_the_one_time_code_never_reaches_signature_logs_or_events(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """给这一档「更有区分度的签名」的唯一材料是模型代码，而它带着令牌

        这条断言就是那个方案的否决理由，也是将来有人真去改签名时第一个会红的地方。
        签名的三个去向全查：异常对象、运维日志、租户可见的运行状态事件。
        """
        harness = _Harness(
            max_retry=5,
            parses=[
                _Parsed(code_content=_leaking_code("第一段")),
                _Parsed(code_content=_leaking_code("第二段")),
            ],
            sandbox_results=[],
        )
        _freeze_one_time_code(monkeypatch)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError) as excinfo:
            asyncio.run(harness.run())

        assert FROZEN_ONE_TIME_CODE not in excinfo.value.signature, "一次性令牌进了错误签名"
        assert FROZEN_ONE_TIME_CODE not in str(excinfo.value), "一次性令牌进了异常消息"
        assert FROZEN_ONE_TIME_CODE not in _all_log_text(harness), "一次性令牌被抄进了运维日志"
        summaries = " ".join(str(getattr(event, "error_summary", "") or "") for event in harness.events)
        assert FROZEN_ONE_TIME_CODE not in summaries, "一次性令牌进了租户可见的运行状态事件"

    def test_raising_the_threshold_is_the_escape_hatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """被这条退化签名误伤的部署，调高阈值即可——这是「不改签名」的最后一块拼图"""
        harness = _Harness(
            max_retry=5,
            stall_threshold=4,
            parses=[_Parsed(code_content=_leaking_code(f"第{index}段")) for index in range(4)],
            sandbox_results=[],
        )
        _freeze_one_time_code(monkeypatch)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError) as excinfo:
            asyncio.run(harness.run())

        assert harness.llm_calls == 4, "调高阈值之后 SECURITY 仍然在第 2 次就被收尾了"
        assert excinfo.value.repeats == 4

    def test_turning_the_check_off_lets_security_rounds_run_to_the_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        max_retry = 3
        harness = _Harness(
            max_retry=max_retry,
            stall_threshold=0,
            parses=[_Parsed(code_content=_leaking_code(f"第{index}段")) for index in range(max_retry)],
            sandbox_results=[],
        )
        _freeze_one_time_code(monkeypatch)
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.llm_calls == max_retry
        assert harness.sandbox_calls == 0


# ======================================================================================
# 7. 两份清单：配置项默认值与模块常量
# ======================================================================================


class TestConfigAndModuleDefaultsAgree:
    """「一个东西登记进两份清单时两份都要测」——本仓已经因为这类事故栽过"""

    def test_config_defaults_match_the_module_constants(self) -> None:
        fields = CoreConfig.model_fields
        assert fields["SESSION_STALL_REPEAT_THRESHOLD"].default == STALL_REPEAT_THRESHOLD == 2
        assert fields["SESSION_CODE_MISSING_MAX_ATTEMPTS"].default == CODE_MISSING_MAX_ATTEMPTS == 2

    @pytest.mark.parametrize(
        "field_name",
        ["SESSION_STALL_REPEAT_THRESHOLD", "SESSION_CODE_MISSING_MAX_ATTEMPTS"],
    )
    def test_both_limits_are_overridable(self, field_name: str) -> None:
        """会话/实例覆盖层是这两项存在的全部意义：不 overridable 就只剩一个全局开关"""
        from nekro_agent.core.overridable_config import OverridableConfig  # noqa: PLC0415

        assert field_name in OverridableConfig.model_fields, f"{field_name} 没进覆盖层，租户/频道改不了"
        assert f"enable_{field_name}" in OverridableConfig.model_fields

    @pytest.mark.parametrize(
        "field_name",
        ["SESSION_STALL_REPEAT_THRESHOLD", "SESSION_CODE_MISSING_MAX_ATTEMPTS"],
    )
    def test_zero_is_documented_as_the_off_switch(self, field_name: str) -> None:
        """取 0 关闭这件事必须写在说明里：运维在后台看到的只有这段文字

        **说明有三份，而后台显示的不是 `description` 那一份。** `ConfigTable.tsx` 渲染的是
        `getLocalizedText(config.i18n_description, config.description, i18n.language)`——
        两个 i18n 键都填齐时，`description=` 只是永远取不到的兜底。原先这条只读
        `.description`，于是「把 `i18n_description` 里的『0 表示关闭』删掉」这个变异**存活**
        （复核实测：定向 286 条全绿），而它删掉的正是运维唯一看得见的那份。
        三份一起钉——又一次「同一份清单的第二个副本」。
        """
        field = CoreConfig.model_fields[field_name]
        extra = field.json_schema_extra
        assert isinstance(extra, dict)
        i18n = extra.get("i18n_description") or {}

        copies = {
            "description": (field.description or "", "关闭"),
            "i18n_description[zh-CN]": (i18n.get("zh-CN", ""), "关闭"),
            "i18n_description[en-US]": (i18n.get("en-US", ""), "disable"),
        }
        for where, (text, keyword) in copies.items():
            assert text, f"{field_name} 的 {where} 是空的——后台会显示不出说明"
            assert "0" in text and keyword.lower() in text.lower(), (
                f"{field_name} 的 {where} 里没写「0 表示关闭」；运维在后台读到的就是这一份"
            )
