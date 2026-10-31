"""上游拒答**第二种形态**的回合收尾：零重试 + 上下文净化 + 独立回执

## 保护的是什么

上一棒只覆盖了拒答的第一种形态：`content` 为空 + `finish_reason='prohibited_content'`
（测试部署 gemini-3.6 的形态，由 `LLMContentBlockedError` / `LLMFailureKind.CONTENT_BLOCKED` 接住）。

用户另一台部署跑 gemini-3.7-flash-high，渠道把**拒绝页当成普通文本塞进 `content`**：
正文非空，于是它绕过了所有「空正文」闸门。`REAL_PROVIDER_REFUSAL` 是那段原文的逐字副本
（本机 `ast.parse` 复现的报错与用户截图逐字吻合：`SyntaxError: unterminated string literal`，
触发字符是 `Google's` 的撇号）。

`ast.parse` 闸门（上一棒）已经能挡住它**被执行**，但改造前它仍然：

1. 走 `code_missing` 那条路，白花掉 `CODE_MISSING_MAX_ATTEMPTS` 给的那次「再问一次」的机会
   —— 而触发安全策略的是**上下文里那条消息**，它不会因为再问一次而消失（实测同一上下文
   连续拒答直到烧满迭代，每轮逐字相同）；
2. 作为 assistant 消息**原样进入下一轮上下文**，既污染上下文、又白烧 token，
   次要风险是后续轮次可能把供应商内部信息复述出去；
3. 最终落到 `NOTICE_ROUND_NO_CODE`（潜台词「模型没写代码」），而用户真正需要知道的是
   「这次的内容被安全策略挡下了、换个说法可能就行」。

改造前的实测基线（本文件的三条主断言正是冲着它去的）：
`llm_calls == 2`、拒答原文出现在第 2 次请求的上下文里、回执是 `NOTICE_ROUND_NO_CODE`。

## 断言的基本要求

「抛了异常」在这一组里是**空断言**——烧满迭代同样抛异常，改造前那条路径也抛异常
（只不过是 `AgentNoExecutableCodeError`）。所以每条用例都必须落到可数的量上：
**LLM 被调用了几次**、**沙盒被调用了几次**、**上下文里有没有那段原文**。

## 反向对照

判据一旦吹宽，代价是具体的：模型**自己**的拒答（「抱歉，我无法协助处理这个请求。」）
会被误判成供应商拒答，从而失去那次本该给的重试机会。所以本文件对称地钉住了
「模型拒答仍然走旧语义（2 次 LLM）」与「普通沙盒 SyntaxError 仍然重试、原文仍然进上下文」。

全部确定性：无网络、无 sleep、无随机。
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any, List, Tuple

import pytest

# 复用兄弟用例里已经搭好的可注入环境，不再抄一份替身。
# `tests/` 下没有 `__init__.py`，pytest 的 prepend 导入模式会把该目录放进 sys.path。
from test_agent_mutation_survivors import _MessageServiceHarness
from test_agent_round_control import _Harness, _SandboxResult, make_traceback

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.failure_feedback import (
    NOTICE_LLM_BLOCKED,
    NOTICE_LLM_EMPTY,
    NOTICE_LLM_FAILED,
    NOTICE_ROUND_ERROR,
    NOTICE_ROUND_NO_CODE,
    NOTICE_ROUND_PROVIDER_REFUSAL,
    NOTICE_ROUND_STALLED,
    NOTICE_ROUND_TIMEOUT,
    NOTICE_ROUND_UNFINISHED,
    failure_notice_throttle,
)
from nekro_agent.services.agent.resolver import parse_chat_response
from nekro_agent.services.agent.run_agent import (
    PROVIDER_REFUSAL_MARKER_GROUPS,
    AgentNoExecutableCodeError,
    AgentProviderRefusalError,
    AgentRoundStalledError,
    AgentRoundUnfinishedError,
    AllLLMRequestsFailedError,
    is_code_missing,
    is_provider_refusal,
    match_provider_refusal,
)

# ======================================================================================
# 线上真实文本（逐字，来自用户截图 + 本机复现，改写它等于把这组用例废掉）
# ======================================================================================

REAL_PROVIDER_REFUSAL = (
    "The prompt could not be submitted. The prompt contains sensitive words that violate "
    "Google's [Generative AI Prohibited Use policy]"
    "(https://policies.google.com/terms/generative-ai/use-policy). Try rephrasing the prompt. "
    "If you think this was an error, [send feedback]"
    "(https://ai.google.dev/gemini-api/docs/troubleshooting)."
)

# 同一段说明被渠道按宽度折行后的形态。折行是真实存在的：标志串一旦被 `\n` 从中间劈开，
# 不做空白归一的逐字 `in` 就再也匹配不上，而故障现象完全一样。
#
# 换行位置是**刻意挑的**：表里的每一条标志串都被劈开了，一条不剩。
# 只劈开其中一条是不够的——剩下那条会照常命中，于是「删掉空白归一」这种变异照样全绿
# （变异测试实测：只劈开第一条时该变异存活）。`test_every_marker_group_is_split` 钉住这一点。
REAL_PROVIDER_REFUSAL_WRAPPED = (
    "The prompt could not\n"
    "be submitted. The prompt contains sensitive words\n"
    "that violate Google's [Generative AI Prohibited Use\n"
    "policy](https://policies.google.com/terms/\n"
    "generative-ai/use-policy). Try rephrasing the prompt."
)

# 模型**自己**写的拒答。它与上面那段的正确处置完全相反：这一类重出一次常常就好，
# 所以它必须继续走 `AgentNoExecutableCodeError`（两次 LLM），一次都不能被本轮判据吃掉。
MODEL_REFUSAL_ZH = "抱歉，我无法协助处理这个请求。这类内容涉及安全风险，我们换个话题聊聊吧。"
MODEL_REFUSAL_EN = "I'm sorry, but I can't help with that request."

NORMAL_CODE_RESPONSE = "```python\nsend_msg_text(_ck, 'hi')\n```"
NORMAL_CODE_EXTRACTED = "send_msg_text(_ck, 'hi')"

# 报告里那条真实的沙盒报错：改造前拒答原文被当代码执行的产物。
# 它是**沙盒输出**而不是模型正文，一次都不该触发本轮的拒答判据。
SANDBOX_SYNTAX_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "/app/run_script.py", line 1\n'
    "    The prompt could not be submitted.\n"
    "        ^\n"
    "SyntaxError: unterminated string literal (detected at line 1)"
)


@pytest.fixture(autouse=True)
def _clean_throttle():
    """回执冷却闸门是进程内单例：不清干净的话用例之间会互相压制

    `test_agent_mutation_survivors` 里那条同名 autouse 固件只在它自己那份模块内生效，
    这里必须自带一份。
    """
    failure_notice_throttle.reset()
    yield
    failure_notice_throttle.reset()


def _message_blob(messages: List[OpenAIChatMessage]) -> str:
    """把一次请求的整个上下文折成一段可搜索的文本

    用 `to_dict()` 而不是读私有字段：断言的是「**真的会被发出去**的那份 payload 里有没有
    那段原文」，中间任何一层裁剪/改写都必须体现在这里。
    """
    return json.dumps([message.to_dict() for message in messages], ensure_ascii=False)


def _install_scripted_llm(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    responses: List[str],
) -> List[str]:
    """装好 `_Harness`，换回**真**解析器，并按脚本喂 LLM 正文；返回每次请求的上下文快照

    与 `test_agent_failure_wiring._install_real_parser_harness` 的差别只有一处：
    这里留存的是**整个 messages 列表**而不是最后一条用户消息——本组要证明的正是
    「拒答原文没有作为 assistant 消息混进上下文」，只看最后一条看不见它。
    """
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
    return contexts


# ======================================================================================
# 1. 判据本身：只认供应商的拒绝页，不认模型自己的话
# ======================================================================================


class TestProviderRefusalPredicateIsNarrow:
    """判据吹宽的代价是具体的：模型自己的拒答会被误判，从而失去本该给的那次重试。

    所以这张表的**下半张**（全部 False）比上半张更重要。
    """

    @pytest.mark.parametrize(
        ("label", "text", "expected"),
        [
            ("线上逐字原文", REAL_PROVIDER_REFUSAL, True),
            ("原文被折行", REAL_PROVIDER_REFUSAL_WRAPPED, True),
            ("原文全大写（渠道改过排版）", REAL_PROVIDER_REFUSAL.upper(), True),
            ("只剩第一句", "The prompt could not be submitted.", True),
            # 这一条**由 True 改成 False**，是对抗式复核的结论，不是让用例迁就实现：
            #
            # 政策文件名是小樱**可以合法说出口**的东西——拒答原文会被回喂进上下文，她下一轮
            # 向用户解释这次故障时引用它是可预期的（「那句话的意思是内容碰到了 Generative AI
            # Prohibited Use policy 这份政策啦」）。整句 boilerplate 不一样：没有哪个角色会
            # 凭空说出「The prompt could not be submitted」并且**只说这一句**。
            #
            # 判定统一到 `provider_refusal.detect_provider_refusal` 之后，这条落在
            # `tests/test_agent_provider_refusal_text.py::BOUNDARY_SAMPLES` 的
            # 「只有政策文件名没有域名 → 放行」上。两份用例原先在这一类输入上结论相反，
            # 保守的那一份赢：漏杀只是退回本模块不存在时的行为（烧满迭代，也就是今天的现状），
            # 误杀是整轮被静默掐掉。
            ("只剩策略名（无域名、无整句 boilerplate）", "Blocked by the Generative AI Prohibited Use policy.", False),
            ("模型自己的中文拒答", MODEL_REFUSAL_ZH, False),
            ("模型自己的英文拒答", MODEL_REFUSAL_EN, False),
            ("正常代码块", NORMAL_CODE_RESPONSE, False),
            ("普通中文闲聊", "好呀，我这就去帮你查一下天气~", False),
            ("空字符串", "", False),
            ("只有空白", "   \n\t ", False),
            # 下面两条是判据「不靠单个情绪词」的锚点：它们各自含有 policy / prompt /
            # sensitive / blocked 这些词，但都不是供应商的拒绝页。
            ("聊到了 policy 一词", "我们群的 policy 是不许刷屏哦，prompt 也别太长啦。", False),
            ("聊到了敏感词三个字", "这条消息可能有敏感词，我换个说法：今天天气不错。", False),
            # 下面两条钉住「同一组内的标志串必须**全部**命中」。把 `all` 写成 `any`
            # （一个字的改动）会让这两句普通聊天被判成供应商拒答、整轮被掐掉。
            # 变异测试实测：没有这两行时该变异存活。
            ("只提到了渠道名", "我这台机器人用的是 Azure OpenAI 的模型组哦~", False),
            ("只提到了策略名", "content management policy 这个词该怎么翻译比较好？", False),
        ],
    )
    def test_predicate_table(self, label: str, text: str, expected: bool) -> None:
        assert is_provider_refusal(text) is expected, f"{label}: 供应商拒答判据结论不符"

    def test_match_returns_our_own_marker_not_the_upstream_text(self) -> None:
        """命中结果要能进日志，所以它必须来自**我们自己的表**，不能是上游正文的切片"""
        marker = match_provider_refusal(REAL_PROVIDER_REFUSAL)

        assert marker, "线上逐字原文必须命中"
        flattened = {" + ".join(group) for group in PROVIDER_REFUSAL_MARKER_GROUPS}
        assert marker in flattened, "返回的标志串不在标志表里，说明它是从上游正文里切出来的"
        # 上游原文里的链接/域名一律不许借由这条返回值溜进日志
        assert "policies.google.com/terms/generative-ai/use-policy" not in marker
        assert "ai.google.dev" not in marker

    def test_every_marker_group_is_split_by_the_wrapped_fixture(self) -> None:
        """折行夹具的自检：每一条标志串都必须真的被换行劈开

        否则「删掉空白归一」的变异会被漏掉——没被劈开的那条照常命中，
        `test_wrapped_refusal_is_caught_too` 依然全绿。变异测试实测过这个漏洞。
        """
        raw = REAL_PROVIDER_REFUSAL_WRAPPED.lower()
        normalized = " ".join(raw.split())
        for group in PROVIDER_REFUSAL_MARKER_GROUPS:
            for marker in group:
                if marker in normalized:
                    assert marker not in raw, f"标志串 {marker!r} 没有被换行劈开，折行用例形同虚设"

    def test_no_marker_means_no_match(self) -> None:
        assert match_provider_refusal(MODEL_REFUSAL_ZH) == ""
        assert match_provider_refusal(NORMAL_CODE_RESPONSE) == ""

    def test_the_real_refusal_is_still_unexecutable(self) -> None:
        """与上一棒的 `ast.parse` 闸门的接缝：真解析器必须继续把这段判成「没有可执行代码」

        本轮的收尾判据挂在 `code_missing` 那条分支里。这条闸门要是哪天松了，
        收尾判据会连同它一起失效，而下面所有用例都还是绿的（它们喂的是同一段文本）。
        """
        parsed = parse_chat_response(REAL_PROVIDER_REFUSAL)
        assert is_code_missing(parsed) is True
        assert parsed.code_content == ""


# ======================================================================================
# 2. 零重试：判定为供应商拒答 → 当轮立刻收尾
# ======================================================================================


class TestZeroRetryOnProviderRefusal:
    """改造前的实测基线是 `llm_calls == 2`（`CODE_MISSING_MAX_ATTEMPTS` 给的那次机会）。

    「抛了异常」不算断言 —— 改造前也抛异常。真正的契约是**那一次重试没有发生**。
    """

    async def test_refusal_costs_exactly_one_llm_call(self, monkeypatch) -> None:
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_scripted_llm(harness, monkeypatch, [REAL_PROVIDER_REFUSAL])

        with pytest.raises(AgentProviderRefusalError) as excinfo:
            await harness.run()

        assert harness.llm_calls == 1, "供应商拒答仍然重试了一次：触发安全策略的是上下文里那条消息，重试必然同样被拒"
        assert harness.sandbox_calls == 0, "拒答文本一次都不该被送进容器"
        assert excinfo.value.marker, "异常要带上命中的标志串，否则日志里无法回答『靠哪一条判出来的』"

    async def test_refusal_does_not_burn_the_iteration_budget(self, monkeypatch) -> None:
        """频道把 `AI_SCRIPT_MAX_RETRY_TIMES` 覆盖成 8 时，仍然只花 1 次"""
        harness = _Harness(max_retry=8, parses=[], sandbox_results=[])
        _install_scripted_llm(harness, monkeypatch, [REAL_PROVIDER_REFUSAL])

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        assert harness.llm_calls == 1
        assert harness.llm_calls < harness.max_retry

    async def test_wrapped_refusal_is_caught_too(self, monkeypatch) -> None:
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_scripted_llm(harness, monkeypatch, [REAL_PROVIDER_REFUSAL_WRAPPED])

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        assert harness.llm_calls == 1

    async def test_model_own_refusal_keeps_the_old_two_attempt_semantics(self, monkeypatch) -> None:
        """反向对照（最重要的一条）：模型自己的拒答**不是**供应商拒答

        没有这一条，把判据放宽成「只要没有代码就零重试」也能让上面几条全绿，
        而代价是每一次格式打滑都失去那次本该给的重试。
        """
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_scripted_llm(harness, monkeypatch, [MODEL_REFUSAL_ZH, MODEL_REFUSAL_ZH])

        with pytest.raises(AgentNoExecutableCodeError) as excinfo:
            await harness.run()

        assert harness.llm_calls == 2, "模型自己的拒答被误判成供应商拒答，那次重试机会被吃掉了"
        assert excinfo.value.attempts == 2
        assert harness.sandbox_calls == 0

    async def test_refusal_is_diagnosable_in_the_error_log(self, monkeypatch) -> None:
        """提前收尾不能以损失可诊断性为代价；但也不能把上游正文整段倒进运维日志"""
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        _install_scripted_llm(harness, monkeypatch, [REAL_PROVIDER_REFUSAL])

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        errors = "\n".join(harness.logger.texts("ERROR"))
        assert "供应商拒答" in errors, "日志里看不出这一轮是因为供应商拒答提前收尾的"
        assert "零重试" in errors
        assert "命中标志" in errors
        # 上游正文里的链接不许落进日志
        assert "policies.google.com" not in errors
        assert "ai.google.dev" not in errors


# ======================================================================================
# 3. 上下文净化：拒答原文一次都不许作为 assistant 消息进入下一轮
# ======================================================================================


# 上游拒绝页的**特征短语**。扫描上下文时不能用「原文里出现过的单词」那种机械切分：
# 迭代上下文里本来就有大量英文系统提示（`CODE_MISSING_FEEDBACK`、SyntaxError 的处理建议
# 里就带着 prohibited / prompt / error 这些词），那样的扫描器会对**未改动的既有行为**报红。
# 所以取的是整句与整域名——它们只可能来自上游那一页。
_UPSTREAM_SIGNATURE_PHRASES = (
    "the prompt could not be submitted",
    "contains sensitive words that violate",
    "generative ai prohibited use policy",
    "policies.google.com",
    "ai.google.dev",
    "try rephrasing the prompt",
)


def _assert_no_upstream_text(blob: str, *, where: str) -> None:
    lowered = " ".join(blob.lower().split())
    leaked = [phrase for phrase in _UPSTREAM_SIGNATURE_PHRASES if phrase in lowered]
    assert not leaked, f"{where}: 上游拒答原文的片段漏进去了 -> {leaked}"


class TestRefusalNeverEntersTheContext:
    """改造前的实测基线：拒答原文**确实**出现在第 2 次请求的上下文里。

    净化不是靠在 append 那一行加过滤实现的，而是靠「在通用 append 之前就 raise」——
    所以这一组同时钉住了那条收尾的**位置**：判据一旦被挪到 append 之后，这里立刻红。
    """

    def test_the_scanner_itself_would_catch_a_leak(self) -> None:
        """扫描器自身的锚点

        `_assert_no_upstream_text` 是本组的主力断言；短语表要是被改空、或者短语根本不在
        逐字原文里，下面每一条断言都会变成恒真的空断言。这里两头都钉住：
        每条短语确实出自原文，且把原文喂进扫描器时它真的会红。
        """
        normalized_original = " ".join(REAL_PROVIDER_REFUSAL.lower().split())
        assert _UPSTREAM_SIGNATURE_PHRASES, "短语表是空的，扫描器已经废了"
        for phrase in _UPSTREAM_SIGNATURE_PHRASES:
            assert phrase in normalized_original, f"短语 {phrase!r} 并不在逐字原文里，扫描器抓不到真实泄漏"

        with pytest.raises(AssertionError):
            _assert_no_upstream_text(REAL_PROVIDER_REFUSAL, where="自检")

    async def test_first_round_refusal_never_reaches_a_second_request(self, monkeypatch) -> None:
        harness = _Harness(max_retry=3, parses=[], sandbox_results=[])
        contexts = _install_scripted_llm(harness, monkeypatch, [REAL_PROVIDER_REFUSAL])

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        assert len(contexts) == 1, "根本不该有第二次请求"
        _assert_no_upstream_text(contexts[0], where="首轮上下文")

    async def test_mid_round_refusal_is_stripped_while_normal_feedback_survives(self, monkeypatch) -> None:
        """拒答出现在**第二轮**：前一轮的正常反馈必须照旧进上下文，只有拒答那条被挡住

        这一条是「别把正常的迭代反馈也一起砍了」的正面锚点：把通用 append 整行删掉
        （一种很自然的『净化』写法）会让 `NORMAL_CODE_EXTRACTED` 这条断言立刻红。
        """
        harness = _Harness(
            max_retry=3,
            parses=[],
            # 沙盒输出刻意**不含**拒答原句：否则第 2 次请求的上下文里本来就该有它
            # （沙盒报错回喂是既有语义），这条用例会变成在测自己的夹具。
            sandbox_results=[
                _SandboxResult(
                    output=make_traceback(line=9, call="send_msg_text()", message="KeyError: 'avatar_url'"),
                    stop_type=ExecStopType.ERROR,
                ),
            ],
        )
        contexts = _install_scripted_llm(
            harness,
            monkeypatch,
            [NORMAL_CODE_RESPONSE, REAL_PROVIDER_REFUSAL],
        )

        with pytest.raises(AgentProviderRefusalError):
            await harness.run()

        assert harness.llm_calls == 2, "第一轮是正常代码，那一轮的迭代必须照常发生"
        assert harness.sandbox_calls == 1
        assert len(contexts) == 2
        # 正常路径没被误伤：上一轮模型的原文仍然作为 assistant 消息进了第 2 次请求
        assert NORMAL_CODE_EXTRACTED in contexts[1], "通用的 assistant 回喂被误砍了"
        # 而拒答本身没有机会进入任何一次请求
        for index, blob in enumerate(contexts):
            _assert_no_upstream_text(blob, where=f"第 {index + 1} 次请求的上下文")


# ======================================================================================
# 4. 用户回执：独立文案，且一个上游片段都不带
# ======================================================================================


class TestProviderRefusalNotice:
    """`AgentProviderRefusalError` → `message_service` 的 isinstance 分派 → 真发出去的那条文案

    这里跑的是**真的** `_run_chat_agent_task`：分派链、`_notify_round_failure`、
    冷却闸门、文案常量全部是真实实现。源码文本断言认不出「复制粘贴错了常量」。
    """

    async def test_refusal_sends_the_dedicated_notice(self, monkeypatch) -> None:
        harness = _MessageServiceHarness()
        harness.install(monkeypatch, raises=AgentProviderRefusalError("上游拒绝受理", marker="x"))

        await harness.run()

        assert harness.sent == [(harness.chat_key, NOTICE_ROUND_PROVIDER_REFUSAL)]
        # 改造前这条路径落在 `NOTICE_ROUND_NO_CODE`（潜台词是「模型没写代码」）
        assert harness.sent[0][1] != NOTICE_ROUND_NO_CODE

    def test_notice_carries_no_upstream_text(self) -> None:
        """既有约束：文案取自固定集合，不带上游任何原文（不构成凭据/配置旁路）

        回执是一条纯中文短句，所以这里可以用比上下文扫描**严格得多**的判据：
        逐字原文里的**任何**一个 ASCII 词元都不许出现，一个都不行。
        """
        _assert_no_upstream_text(NOTICE_ROUND_PROVIDER_REFUSAL, where="用户回执")
        assert "policies.google.com" not in NOTICE_ROUND_PROVIDER_REFUSAL
        assert "finish_reason" not in NOTICE_ROUND_PROVIDER_REFUSAL
        assert "prohibited_content" not in NOTICE_ROUND_PROVIDER_REFUSAL

        tokens = {token.lower() for token in re.findall(r"[A-Za-z][A-Za-z.\-]{3,}", REAL_PROVIDER_REFUSAL)}
        assert len(tokens) >= 15, "词元表抽空了，下面那条断言会变成恒真"
        lowered_notice = NOTICE_ROUND_PROVIDER_REFUSAL.lower()
        leaked = sorted(token for token in tokens if token in lowered_notice)
        assert not leaked, f"回执里带上了上游原文的词元 -> {leaked}"

    def test_notice_tells_the_user_to_rephrase(self) -> None:
        """文案的语义契约：用户得知道「换个说法」，而不是「再说一次原话」

        没有这一条，把常量改成一句「稍后再试」也能让上面两条全绿，
        而那正是把用户往错方向引的那种错。
        """
        assert "换个说法" in NOTICE_ROUND_PROVIDER_REFUSAL
        assert NOTICE_ROUND_PROVIDER_REFUSAL.endswith("QwQ"), "与现有 5 条同语气"

    async def test_notice_goes_through_the_cooldown_gate(self, monkeypatch) -> None:
        """必须复用既有的 `failure_notice_throttle`，不能自己直发绕过冷却

        08-11 那天真实群里被刷了十几条回执，冷却闸门就是为此加的。
        """
        harness = _MessageServiceHarness(cooldown_seconds=600.0)
        harness.install(monkeypatch, raises=AgentProviderRefusalError("上游拒绝受理", marker="x"))

        await harness.run()
        await harness.run()

        assert harness.run_agent_calls == 2, "冷却只该管回执，不该把整轮对话跳过"
        assert harness.sent == [(harness.chat_key, NOTICE_ROUND_PROVIDER_REFUSAL)], "第二条回执绕过了冷却闸门"

    async def test_feedback_switch_still_applies(self, monkeypatch) -> None:
        """运维关掉回执总开关时，新路径也必须闭嘴"""
        harness = _MessageServiceHarness(enable_feedback=False)
        harness.install(monkeypatch, raises=AgentProviderRefusalError("上游拒绝受理", marker="x"))

        await harness.run()

        assert harness.sent == []


# ======================================================================================
# 5. 反向对照：普通沙盒 SyntaxError 的既有语义逐字不变
# ======================================================================================


class TestOrdinarySandboxErrorsKeepTheOldSemantics:
    """本轮判据读的是**模型正文**，与沙盒输出无关。

    这一组证明判据没吹宽到「只要哪儿出现了 SyntaxError / 出现了那段字符串就早停」。
    """

    async def test_sandbox_syntax_error_still_retries_and_feeds_the_original_back(self, monkeypatch) -> None:
        """普通语法错误：仍然重试，模型原文仍然作为 assistant 消息进入下一轮"""
        harness = _Harness(
            max_retry=2,
            parses=[],
            sandbox_results=[
                _SandboxResult(
                    output=make_traceback(line=9, call="x(", message="invalid syntax", exc="SyntaxError"),
                    stop_type=ExecStopType.ERROR,
                ),
                _SandboxResult(
                    output=make_traceback(line=12, call="y[", message="unexpected EOF", exc="SyntaxError"),
                    stop_type=ExecStopType.ERROR,
                ),
            ],
        )
        contexts = _install_scripted_llm(harness, monkeypatch, [NORMAL_CODE_RESPONSE] * 2)

        # 两次错误签名不同 → 无进展判据不该触发，迭代跑满后按既有语义收尾
        with pytest.raises(AgentRoundUnfinishedError):
            await harness.run()

        assert harness.sandbox_calls == 2, "普通沙盒错误的重试语义被改动了"
        # §3 #58：`max_retry=2` 的回合发 2 次请求（改造前 3 次，最后那次结果无人消费）
        assert harness.llm_calls == 2
        assert NORMAL_CODE_EXTRACTED in contexts[1], "模型原文没有像既有语义那样回喂进上下文"
        assert "invalid syntax" in contexts[1], "沙盒报错原文没有回喂进上下文"

    async def test_the_reported_syntax_error_in_sandbox_output_is_not_a_refusal(self, monkeypatch) -> None:
        """报告里那条真实报错（`unterminated string literal`，且沙盒输出里**含有**拒答原句）
        出现在**沙盒输出**里时，一次都不该触发供应商拒答收尾——判据只看模型正文。
        """
        harness = _Harness(
            max_retry=2,
            parses=[],
            sandbox_results=[
                _SandboxResult(output=SANDBOX_SYNTAX_ERROR, stop_type=ExecStopType.ERROR),
                _SandboxResult(
                    output=make_traceback(line=3, call="z(", message="invalid syntax", exc="SyntaxError"),
                    stop_type=ExecStopType.ERROR,
                ),
            ],
        )
        _install_scripted_llm(harness, monkeypatch, [NORMAL_CODE_RESPONSE] * 3)

        with pytest.raises(AgentRoundUnfinishedError):
            await harness.run()

        assert harness.sandbox_calls == 2

    async def test_code_quoting_the_marker_still_runs(self, monkeypatch) -> None:
        """模型在**可执行代码**里引用了那句话（例如向用户解释这次故障）：必须照常执行

        判据是「没有可执行代码」与「正文像供应商拒绝页」的**合取**，
        砍掉前一半就会让这条用例红——而那正是最容易误伤真实用户的一种放宽。
        """
        quoting_code = (
            "```python\n"
            "send_msg_text(_ck, '上游刚才提示 The prompt could not be submitted，换个说法试试吧')\n"
            "```"
        )
        harness = _Harness(
            max_retry=3,
            parses=[],
            sandbox_results=[_SandboxResult(output="", stop_type=ExecStopType.NORMAL)],
        )
        _install_scripted_llm(harness, monkeypatch, [quoting_code])

        await harness.run()

        assert harness.sandbox_calls == 1, "引用了标志串的正常代码被误判成供应商拒答"
        assert "The prompt could not be submitted" in harness.sandbox_codes[0]


# ======================================================================================
# 6. 新异常与既有四类互不串台
# ======================================================================================


class TestNewErrorTypeStaysDistinguishable:
    """`message_service` 靠一条 isinstance elif 链分派回执。

    新类一旦与既有某一类有继承关系，先命中的分支会把它吃掉，而源码文本断言照样绿。
    """

    _ROUND_ERRORS = (
        AllLLMRequestsFailedError,
        AgentNoExecutableCodeError,
        AgentProviderRefusalError,
        AgentRoundStalledError,
        AgentRoundUnfinishedError,
    )

    @pytest.mark.parametrize("outer", _ROUND_ERRORS, ids=lambda cls: cls.__name__)
    @pytest.mark.parametrize("inner", _ROUND_ERRORS, ids=lambda cls: cls.__name__)
    def test_round_error_classes_do_not_inherit_from_each_other(self, outer, inner) -> None:
        if outer is inner:
            return
        assert not issubclass(outer, inner), (
            f"{outer.__name__} 继承了 {inner.__name__}：isinstance 链的顺序从此决定回执文案"
        )

    def test_every_notice_is_distinct(self) -> None:
        """七条回执两两不同：复制粘贴错常量时，源码文本断言（常量名出现过）认不出来"""
        notices = [
            NOTICE_LLM_FAILED,
            NOTICE_LLM_EMPTY,
            NOTICE_LLM_BLOCKED,
            NOTICE_ROUND_UNFINISHED,
            NOTICE_ROUND_TIMEOUT,
            NOTICE_ROUND_ERROR,
            NOTICE_ROUND_NO_CODE,
            NOTICE_ROUND_STALLED,
            NOTICE_ROUND_PROVIDER_REFUSAL,
        ]
        assert len(set(notices)) == len(notices)
