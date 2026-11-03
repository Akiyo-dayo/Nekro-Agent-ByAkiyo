"""供应商拒答判定的**覆盖面**：每一次解析都要判，包括末轮取回的那一条

## 本文件补的是哪一半

`test_agent_refusal_round_exit.py`（上一棒）证明的是「判出来之后处置对不对」：零重试、
上下文净化、独立回执。它的每一条用例都让拒答落在**第一次请求**上。

漏掉的是「什么时候会被判」。改造前判定写在循环体的 `if is_code_missing(...)` 分支里，
而 `run_agent` 的结构是：

    parse_chat_response(第一次请求的响应)      <- 循环外
    for i in range(AI_SCRIPT_MAX_RETRY_TIMES):
        ...消费 parsed_code_data...
        parse_chat_response(本趟末尾请求的响应)  <- 循环内

循环体第 i 趟消费的是第 i-1 趟末尾解析出来的东西。于是**最后一趟末尾那次请求**的响应
解析完就直接掉出循环，再没有人看它一眼——判定对它完全失效。

实测基线（`max_retry=2`，脚本第 3 条是逐字拒绝页）：`llm_calls == 3`、
抛 `AgentRoundUnfinishedError`、用户收到 `NOTICE_ROUND_UNFINISHED`
（「已经试到最大迭代次数啦」）。那正是整个工作流要消灭的那句话，从另一个入口长了回来。

修复把判定挂到**每一次 `parse_chat_response` 之后**（`run_agent.raise_if_provider_refusal`），
两处解析点因此都被覆盖。

## 回合不再多发最后一次请求之后，「末轮」指的是哪一次请求（本文件的口径已随之改过）

那次「结果无人消费」的请求现在**根本不发了**（`run_agent` 在最后一趟开头就 `break`，
理由见该处注释）。于是回合总共只发 `AI_SCRIPT_MAX_RETRY_TIMES` 次请求，
**每一次的响应都会被消费**——

    请求 #1        -> 第 1 趟消费
    请求 #2        -> 第 2 趟消费
    ...
    请求 #M(=预算) -> 第 M 趟（最后一趟）消费

本文件的「末轮」因此重新指向**最后一次真正发出的请求**，也就是第 M 次。它仍然是最容易
漏掉判定的那一处，而且漏掉的后果一字不差：那条响应会被第 M 趟当成 `code_missing` 处理
（`code_missing_streak` 记 1、发一条格式反馈），紧接着撞上最后一趟的 `break`，
于是仍然以 `AgentRoundUnfinishedError` 收尾、用户仍然收到「已经试到最大迭代次数啦」。
所以这一组的每条用例都只把脚本位置往前挪一格，断言与理由逐字不变。

## 断言的基本要求

「抛了异常」在这里是空断言——改造前也抛异常（只是抛错了那一种）。所以每条用例都落在
可辨识的量上：**异常的类型**、**LLM 被调用了几次**、**用户实际收到哪一句回执**。

全部确定性：无网络、无 sleep、无随机。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

# 复用兄弟用例已经搭好的可注入环境，不再抄一份替身（项目 §0.5）
from test_agent_mutation_survivors import _MessageServiceHarness
from test_agent_refusal_round_exit import (
    MODEL_REFUSAL_ZH,
    NORMAL_CODE_RESPONSE,
    REAL_PROVIDER_REFUSAL,
)
from test_agent_round_control import _Harness, _SandboxResult, make_traceback

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.failure_feedback import (
    NOTICE_ROUND_NO_CODE,
    NOTICE_ROUND_PROVIDER_REFUSAL,
    NOTICE_ROUND_UNFINISHED,
    failure_notice_throttle,
)
from nekro_agent.services.agent.resolver import parse_chat_response
from nekro_agent.services.agent.run_agent import (
    AgentNoExecutableCodeError,
    AgentProviderRefusalError,
    AgentRoundUnfinishedError,
)

# 沙盒报错脚本：让每一趟迭代都以 ERROR 收尾，好把循环推到最后一趟。
# 每条的异常类型/行号都不同，免得撞上无进展检测（那条会提前收尾，用例就测不到末轮了）。
_SANDBOX_ERRORS = [
    _SandboxResult(
        output=make_traceback(line=idx + 1, call=f"step_{idx}()", message=f"boom {idx}", exc=exc),
        stop_type=ExecStopType.ERROR,
    )
    for idx, exc in enumerate(["ValueError", "TypeError", "KeyError", "IndexError", "AttributeError"])
]

# 每一趟都能跑但都以 ERROR 收尾的正常代码块，各不相同（同样是为了避开无进展检测）
_ERROR_CODE = [f"```python\nstep_{idx}()\n```" for idx in range(6)]


@pytest.fixture(autouse=True)
def _clean_throttle():
    """回执冷却闸门是进程内单例，用例之间不清会互相压制"""
    failure_notice_throttle.reset()
    yield
    failure_notice_throttle.reset()


def _message_blob(messages: List[OpenAIChatMessage]) -> str:
    return json.dumps([message.to_dict() for message in messages], ensure_ascii=False)


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    responses: List[str],
    *,
    max_retry: int,
    sandbox_results: List[_SandboxResult],
) -> Tuple[_Harness, List[str]]:
    """按脚本喂 LLM 正文驱动**真** `run_agent`，返回 (harness, 每次请求的上下文快照)

    解析器换回真的：判定挂在解析之后，用替身解析器会把被测的接缝整个绕开。
    """
    harness = _Harness(max_retry=max_retry, parses=[], sandbox_results=sandbox_results)
    harness.install(monkeypatch)
    contexts: List[str] = []

    async def _send(**kwargs: Any) -> Tuple[Any, Any, List[str]]:
        index = harness.llm_calls
        if index >= len(responses):
            raise AssertionError(f"LLM 脚本只有 {len(responses)} 条，却被取用了第 {index + 1} 次")
        contexts.append(_message_blob(kwargs["messages"]))
        harness.llm_calls += 1
        return (
            SimpleNamespace(
                response_content=responses[index],
                use_model="gemini-3.7-flash-high",
                thought_chain="",
            ),
            SimpleNamespace(CHAT_MODEL="gemini-3.7-flash-high"),
            [],
        )

    monkeypatch.setattr(run_agent_module, "send_agent_request", _send)
    monkeypatch.setattr(run_agent_module, "parse_chat_response", parse_chat_response)
    return harness, contexts


# ======================================================================================
# 1. 末轮盲区：拒答落在最后一次迭代取回的响应上
# ======================================================================================


class TestRefusalOnTheLastIterationResponse:
    """改造前这条路径的实测基线：`AgentRoundUnfinishedError` + `llm_calls == 3`。

    这一组是本文件的主题——判定挂在循环体里时，它**必然**全红。
    """

    async def test_tail_refusal_is_classified_as_provider_refusal(self, monkeypatch) -> None:
        """max_retry=2：第 2 次请求（本回合最后一次请求）拿回拒绝页，必须判成供应商拒答"""
        harness, _ = _drive(
            monkeypatch,
            [_ERROR_CODE[0], REAL_PROVIDER_REFUSAL],
            max_retry=2,
            sandbox_results=_SANDBOX_ERRORS[:1],
        )

        with pytest.raises(AgentProviderRefusalError) as excinfo:
            await harness.run()

        assert harness.llm_calls == 2, "脚本没有被跑到末轮，用例没测到目标路径"
        assert excinfo.value.marker, "异常要带上命中的标志串，否则日志里无法回答『靠哪一条判出来的』"

    async def test_tail_refusal_is_not_reported_as_round_unfinished(self, monkeypatch) -> None:
        """反向锚点：判定漏掉末轮时，这里落到的正是 `AgentRoundUnfinishedError`

        单独写一条是因为上一条用 `pytest.raises(AgentProviderRefusalError)`，
        而 `AgentRoundUnfinishedError` 不是它的子类、也不是父类——上一条红的时候
        这一条要能直接指出「退化成了哪一种」，而不是只报一个类型不符。
        """
        harness, _ = _drive(
            monkeypatch,
            [_ERROR_CODE[0], REAL_PROVIDER_REFUSAL],
            max_retry=2,
            sandbox_results=_SANDBOX_ERRORS[:1],
        )

        with pytest.raises(BaseException) as excinfo:  # noqa: PT011  - 要的就是「到底抛了哪一种」
            await harness.run()

        assert not isinstance(excinfo.value, AgentRoundUnfinishedError), (
            "末轮撞上供应商拒答仍被报成「迭代次数耗尽」：用户会收到「已经试到最大迭代次数啦」，"
            "而真正发生的是上游拒绝受理"
        )
        assert isinstance(excinfo.value, AgentProviderRefusalError)

    @pytest.mark.parametrize("max_retry", [1, 2, 3, 4])
    async def test_refusal_at_the_tail_for_every_budget(self, monkeypatch, max_retry: int) -> None:
        """把「拒答恰好落在末轮」这件事在每一种迭代预算下各钉一遍

        `AI_SCRIPT_MAX_RETRY_TIMES` 是频道可覆盖的配置项，线上真实取值分布在 1~8。
        只测一个取值挡不住「off-by-one：判定晚一轮才生效」这类变异。

        不再多发最后一次请求之后，回合总共只发 `max_retry` 次请求，所以拒绝页放在第 `max_retry` 条，
        前面 `max_retry - 1` 条正常代码各自消费一趟沙盒。
        """
        responses = _ERROR_CODE[: max_retry - 1] + [REAL_PROVIDER_REFUSAL]
        harness, _ = _drive(
            monkeypatch,
            responses,
            max_retry=max_retry,
            sandbox_results=_SANDBOX_ERRORS[: max_retry - 1],
        )

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        assert harness.llm_calls == max_retry, "末轮那次请求没被发出去，或者循环提前收尾了"
        assert harness.sandbox_calls == max_retry - 1, "前面每一趟都该正常进沙盒，只有末轮那条不进"

    async def test_tail_refusal_never_reaches_the_sandbox(self, monkeypatch) -> None:
        """末轮那条拒绝页一次都不该被送进容器

        沙盒脚本只给 1 条：拒答文本要是被当代码执行，`_ScriptExhausted` 会响亮地红。
        """
        harness, _ = _drive(
            monkeypatch,
            [_ERROR_CODE[0], REAL_PROVIDER_REFUSAL],
            max_retry=2,
            sandbox_results=_SANDBOX_ERRORS[:1],
        )

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        assert harness.sandbox_calls == 1
        for code in harness.sandbox_codes:
            assert "could not be submitted" not in code.lower(), "拒答原文被当成代码送进了沙盒"


# ======================================================================================
# 2. 覆盖面本身：判定挂在**每一次**解析之后
# ======================================================================================


class TestEveryParseSiteIsGuarded:
    """本项目已经五次栽在「只修了一半」。这一组把「有几处解析、判定接了几处」变成断言。"""

    @pytest.mark.parametrize("refusal_at", [1, 2, 3])
    async def test_refusal_at_any_position_terminates_the_round(self, monkeypatch, refusal_at: int) -> None:
        """拒答无论落在第几次请求上，都必须以 `AgentProviderRefusalError` 收尾且立刻停手

        `refusal_at=1` 是循环外那次请求（上一棒已覆盖，这里连带回归）；
        `refusal_at=3` 在 `max_retry=3` 下正是末轮那条——不再多发最后一次请求之后，回合最多只发
        `max_retry` 次请求，所以取值到 3 为止（原先的 `refusal_at=4` 对应的是那次
        「结果无人消费」的请求，它已经不存在了）。
        """
        max_retry = 3
        responses = _ERROR_CODE[: refusal_at - 1] + [REAL_PROVIDER_REFUSAL]
        harness, _ = _drive(
            monkeypatch,
            responses,
            max_retry=max_retry,
            sandbox_results=_SANDBOX_ERRORS[: refusal_at - 1],
        )

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        assert harness.llm_calls == refusal_at, (
            f"拒答出现在第 {refusal_at} 次请求上，却发生了 {harness.llm_calls} 次请求："
            "要么判定漏了这个位置，要么判完还继续重试"
        )
        # `raise_if_provider_refusal` 是被**两处**手算下标喂的闭包（循环外传 1、循环内传 `i + 2`），
        # 而那个下标只在这一行日志里露过面。逐字断言剩余次数 = `max_retry - iteration_index`，
        # 任一调用点算错一格都会红——原先这里只断言「日志里没有负号」，而不再多发最后一次请求之后
        # `iteration_index` 再也不可能超预算，那条断言已经恒真、什么都不测了。
        errors = "\n".join(harness.logger.texts("ERROR"))
        assert f"剩余 {max_retry - refusal_at} 次迭代未使用" in errors, (
            f"第 {refusal_at} 次请求上的拒答，日志里报的剩余迭代次数不是 {max_retry - refusal_at}："
            f"该调用点传给 `raise_if_provider_refusal` 的 `iteration_index` 算错了 ｜ {errors}"
        )

    async def test_guard_never_fires_when_code_was_extracted(self, monkeypatch) -> None:
        """`code_missing` 那个合取项本身要能被单独证明

        变异测试实测：把 `if not is_code_missing(parsed): return` 删掉，上面所有用例照样全绿。
        原因是 `provider_refusal.detect_provider_refusal` 自己也挡代码（``` 围栏 / 函数调用 /
        可 `ast.parse`），于是「带代码的响应」永远走不到判定里——两道墙叠在一起，
        拆掉其中一道从外部看不出来。

        所以这里**把文本判据整个换成恒真的替身**，只留下 `code_missing` 这一道墙受测。
        它保护的是将来：哪天 `has_code_features` 被放宽（正是上一棒改过的那类判据），
        这一条合取就是最后一道墙，而没有本用例的话它会静悄悄地失效。
        """
        harness, _ = _drive(
            monkeypatch,
            [NORMAL_CODE_RESPONSE],
            max_retry=3,
            sandbox_results=[_SandboxResult(output="", stop_type=ExecStopType.NORMAL)],
        )
        monkeypatch.setattr(run_agent_module, "match_provider_refusal", lambda _text: "恒真替身标志")

        await harness.run()

        assert harness.sandbox_calls == 1, (
            "响应里明明取出了可执行代码，却仍被判成供应商拒答并掐断了本轮："
            "`code_missing` 合取项没有生效"
        )
        assert harness.llm_calls == 1

    def test_parse_sites_and_guard_sites_are_balanced(self) -> None:
        """结构断言：`run_agent` 里有几处 `parse_chat_response(`，就有几处判定紧随其后

        这一条防的是**将来**：有人加了第三个解析点却忘了接判定时，行为用例未必覆盖得到那条
        新路径（它可能要很特殊的脚本才能走到），而这条会立刻红。

        它不替代上面的行为用例——只靠数调用点无法证明判定真的生效，所以两者都要有。
        """
        source = Path(run_agent_module.__file__).read_text(encoding="utf-8")
        body = source.split("async def run_agent(", 1)[1].split("\ndef resolve_primary_model_group", 1)[0]

        parse_sites = len(re.findall(r"^\s*\w[\w:\[\] ]*=\s*parse_chat_response\(", body, re.MULTILINE))
        guard_sites = len(re.findall(r"^\s*await raise_if_provider_refusal\(", body, re.MULTILINE))

        assert parse_sites >= 2, f"`run_agent` 里的解析点少于 2 处（数到 {parse_sites}），本用例的前提失效了"
        assert guard_sites == parse_sites, (
            f"{parse_sites} 处 `parse_chat_response`，却只有 {guard_sites} 处供应商拒答判定："
            "有解析点没有接上判定，末轮盲区那一类缺陷会重新出现"
        )


# ======================================================================================
# 3. 用户实际收到哪一句：末轮拒答不能退回「已经试到最大迭代次数啦」
# ======================================================================================


class TestUserFacingNoticeForTailRefusal:
    """文案是这条链路的**唯一**用户可见产物，也是改造前最直接的现象。"""

    async def test_tail_refusal_yields_the_provider_refusal_notice(self, monkeypatch) -> None:
        """两段接起来跑：真 `run_agent` 抛出的**那个异常对象**再喂给真的分派链

        不自己 new 一个异常：那样只测到分派链，测不到「末轮拒答到底抛的是哪一种」——
        而后者正是本文件要证明的东西。
        """
        harness, _ = _drive(
            monkeypatch,
            [_ERROR_CODE[0], REAL_PROVIDER_REFUSAL],
            max_retry=2,
            sandbox_results=_SANDBOX_ERRORS[:1],
        )

        with pytest.raises(BaseException) as excinfo:  # noqa: PT011
            await harness.run()
        raised = excinfo.value

        monkeypatch.undo()

        service_harness = _MessageServiceHarness()
        service_harness.install(monkeypatch, raises=raised)
        await service_harness.run()

        assert [text for _chat, text in service_harness.sent] == [NOTICE_ROUND_PROVIDER_REFUSAL]
        assert NOTICE_ROUND_UNFINISHED not in [text for _chat, text in service_harness.sent], (
            "末轮拒答仍然给出「已经试到最大迭代次数啦」——这句话的潜台词是「再说一次也许就好」，"
            "而供应商拒答重说原话必然还是同一页"
        )


# ======================================================================================
# 4. 回归：把判定挪到解析之后，不能改坏既有的三条收尾路径
# ======================================================================================


class TestExistingExitPathsAreUnchanged:
    """判定换了挂载点，相邻语义必须逐字不变。这一组全部是**反向**用例。"""

    async def test_plain_iteration_exhaustion_still_reports_unfinished(self, monkeypatch) -> None:
        """每一趟都能执行、都以 ERROR 收尾（签名各不相同）→ 仍然是「迭代次数耗尽」"""
        harness, _ = _drive(
            monkeypatch,
            _ERROR_CODE[:3],
            max_retry=3,
            sandbox_results=_SANDBOX_ERRORS[:3],
        )

        with pytest.raises(AgentRoundUnfinishedError):
            await harness.run()

        # 不再多发最后一次请求之后：三趟迭代只发三次请求（改造前是 4 次，最后那次的结果无人消费）
        assert harness.llm_calls == 3
        assert harness.sandbox_calls == 3

    async def test_model_own_refusal_still_gets_its_retry(self, monkeypatch) -> None:
        """模型自己的拒答（不是供应商拒绝页）必须继续走 `CODE_MISSING_MAX_ATTEMPTS`

        这是判据被吹宽时最先塌的一条：把「没有代码」直接当成供应商拒答，
        模型就再也拿不到那次本该给的重出机会。
        """
        harness, _ = _drive(
            monkeypatch,
            [MODEL_REFUSAL_ZH, MODEL_REFUSAL_ZH],
            max_retry=5,
            sandbox_results=[],
        )

        with pytest.raises(AgentNoExecutableCodeError):
            await harness.run()

        assert harness.llm_calls == 2, "模型自己的拒答被当成供应商拒答，那次重试被吃掉了"
        assert harness.sandbox_calls == 0

    async def test_model_own_refusal_notice_is_still_no_code(self, monkeypatch) -> None:
        """并且用户收到的仍然是「没给出能执行的内容」，不是供应商拒答那一句"""
        harness, _ = _drive(
            monkeypatch,
            [MODEL_REFUSAL_ZH, MODEL_REFUSAL_ZH],
            max_retry=5,
            sandbox_results=[],
        )
        with pytest.raises(BaseException) as excinfo:  # noqa: PT011
            await harness.run()
        raised = excinfo.value

        monkeypatch.undo()
        service_harness = _MessageServiceHarness()
        service_harness.install(monkeypatch, raises=raised)
        await service_harness.run()

        assert [text for _chat, text in service_harness.sent] == [NOTICE_ROUND_NO_CODE]

    async def test_normal_round_still_completes(self, monkeypatch) -> None:
        """判定挂在解析之后，正常回合一次都不该被它碰到"""
        harness, _ = _drive(
            monkeypatch,
            [NORMAL_CODE_RESPONSE],
            max_retry=3,
            sandbox_results=[_SandboxResult(output="", stop_type=ExecStopType.NORMAL)],
        )

        await harness.run()

        assert harness.llm_calls == 1
        assert harness.sandbox_calls == 1


# ======================================================================================
# 5. 净化：末轮这条路径同样不许把上游原文漏进上下文或租户可见的事件
# ======================================================================================


class TestTailRefusalDoesNotLeakUpstreamText:
    """`error_summary` 会经 SSE 到达租户可见的 `/events`，是与上下文并列的第二个出口。"""

    async def test_refusal_text_never_enters_any_request_context(self, monkeypatch) -> None:
        harness, contexts = _drive(
            monkeypatch,
            [_ERROR_CODE[0], REAL_PROVIDER_REFUSAL],
            max_retry=2,
            sandbox_results=_SANDBOX_ERRORS[:1],
        )

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        # 末轮拒答之后本来就不会再发请求，所以这条同时也在证明「没有第 3 次请求」
        assert len(contexts) == 2
        for index, blob in enumerate(contexts):
            lowered = blob.lower()
            assert "could not be submitted" not in lowered, f"第 {index + 1} 次请求的上下文里带上了拒答原文"
            assert "policies.google.com" not in lowered, f"第 {index + 1} 次请求的上下文里带上了供应商链接"

    async def test_refusal_text_never_enters_runtime_events(self, monkeypatch) -> None:
        """后台事件只许报我们自己的结论"""
        harness, _ = _drive(
            monkeypatch,
            [_ERROR_CODE[0], REAL_PROVIDER_REFUSAL],
            max_retry=2,
            sandbox_results=_SANDBOX_ERRORS[:1],
        )

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        summaries = [str(getattr(event, "error_summary", "") or "") for event in harness.events]
        joined = " ".join(summaries).lower()
        assert "could not be submitted" not in joined
        assert "policies.google.com" not in joined
        assert "ai.google.dev" not in joined
        assert any("上游拒绝受理本次请求" in summary for summary in summaries), (
            "末轮拒答没有产生任何 failed 事件：前端/运维那边看不到这一轮为什么停了"
        )

    async def test_error_log_reports_our_marker_not_upstream_text(self, monkeypatch) -> None:
        harness, _ = _drive(
            monkeypatch,
            [_ERROR_CODE[0], REAL_PROVIDER_REFUSAL],
            max_retry=2,
            sandbox_results=_SANDBOX_ERRORS[:1],
        )

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        errors = "\n".join(harness.logger.texts("ERROR"))
        assert "零重试" in errors
        assert "命中标志" in errors
        assert "policies.google.com" not in errors, "供应商链接被整段抄进了运维日志"
        # `max_retry=2` 且拒答落在第 2 次（=本回合最后一次）请求上 → 剩余恰好 0 次。
        # 这里不再写「日志里没有负号」：不再多发最后一次请求之后 `iteration_index` 恒 <= 预算，那条断言恒真。
        assert "剩余 0 次迭代未使用" in errors, f"末轮拒答报的剩余迭代次数不是 0 ｜ {errors}"
