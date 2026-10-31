"""对抗式复核：钉住本轮改造**自己引入**的回归。

本文件不重复验证本轮的正向修复（那是 `test_agent_response_parsing.py` /
`test_agent_round_control.py` / `test_agent_failure_wiring.py` 的职责），只钉三处
复核中实测到的、修复反过来打断既有正常形态的地方：

1. **围栏泄漏的修法把「代码正文里本身出现 ``` 」的形态截断了**（`TestFenceInsideCode`）。
   逐块匹配在内层围栏处提前收尾，`send_msg_text(_ck, "` 这种半截语句被判成
   `code_missing=False` 送进沙盒，得到 `SyntaxError: unterminated string literal`——
   正是本轮要消灭的那类失败，只是换了个触发形态。旧实现在这两个样本上是**对的**。

2. **`ast.parse` 闸门的边界**（`TestAstGateBoundaries`）。顶层 `await` 必须继续放行：
   `ast.parse` 不做符号表检查所以能过，而 `compile(..., "exec")` 会报
   `'await' outside function`。谁要是把闸门"顺手收紧"成 `compile`，模型写顶层 `await`
   的整类回复就会被误判成「没有代码」而不进沙盒。

3. **无进展检测把 `MANUAL` 也算成了故障**（`TestManualStopIsNotAStall`）。
   `exit(9)` 是运行时契约里写给模型的调试手段
   （`templates/j2/runtime_contract.j2:200`），与 `AGENT` 同属**有意的续跑点**，
   不是失败形态。

每条断言都按「修复不存在时会不会红」挑选：`TestFenceInsideCode` 的期望值是**旧实现的
逐字输出**，去掉回退分支立刻红；`TestAstGateBoundaries` 换成 `compile` 立刻红；
`TestManualStopIsNotAStall` 把 `MANUAL` 放回集合立刻红（`_ScriptExhausted` 除外，
它证明的是轮数确实跑满而不是提前收尾）。
"""

from __future__ import annotations

import ast
from typing import Any, List, Tuple, cast

import pytest

# ruff: noqa: E402
from test_agent_round_control import _Harness, _Parsed, _SandboxResult

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent.resolver import (
    _pick_longest_code_block,  # noqa: PLC2701
    parse_chat_response,
)
from nekro_agent.services.agent.run_agent import (
    STALL_TRACKED_STOP_TYPES,
    AgentRoundStalledError,
    AgentRoundUnfinishedError,
)

F = "```"


# ---------------------------------------------------------------------------
# 1. 代码正文里本身出现 ``` 的形态
# ---------------------------------------------------------------------------

# (用例名, 原始响应, 期望的 code_content)
# 期望值取自 **HEAD 版解析器**在同一输入上的逐字输出（复核时用对拍脚本核对过）：
# 这类响应改造前就是成功执行的，修围栏泄漏不能把它们打断。
NESTED_FENCE_CASES: List[Tuple[str, str, str]] = [
    (
        # 把一段 markdown 代码块作为**消息正文**发出去（单行字符串里带 ```）
        "fence_inside_single_line_string",
        f'{F}python\nsend_msg_text(_ck, "{F}\\nprint(1)\\n{F}")\n{F}',
        'send_msg_text(_ck, "```\\nprint(1)\\n```")',
    ),
    (
        # 同样的意图，但用三引号跨行写——内层围栏此时**位于行首**，
        # 逐块匹配会把它当成闭合围栏
        "fence_inside_triple_quoted_string",
        f'{F}python\nmsg = """\n{F}\nprint(1)\n{F}\n"""\nsend_msg_text(_ck, msg)\n{F}',
        'msg = """\n```\nprint(1)\n```\n"""\nsend_msg_text(_ck, msg)',
    ),
    (
        # 与上一条同形，只多一行 `from plugins` 导入。
        #
        # 它盯的是**回退分支上的 `_finalize_code`**：逐块候选编译不过时会退到旧的整段跨度，
        # 而那一支此前一条用例都没驱动过 —— 把 `_finalize_code(...)` 从回退分支去掉，
        # 全套测试照样全绿，但 `from plugins...` 会被原样送进沙盒（容器里没有这个包，
        # 必然 ImportError）。剃除行为在逐块主路径上有用例钉着，这一条补的是第二条分支。
        "fence_inside_string_with_plugins_import",
        f'{F}python\nfrom plugins.weather import query\nmsg = \"\"\"\n{F}\nprint(1)\n{F}\n\"\"\"\nsend_msg_text(_ck, msg)\n{F}',
        'msg = \"\"\"\n```\nprint(1)\n```\n\"\"\"\nsend_msg_text(_ck, msg)',
    ),
]


class TestFenceInsideCode:
    """代码正文里的 ``` 不能把代码截断"""

    @pytest.mark.parametrize(
        ("raw", "expected_code"),
        [pytest.param(raw, code, id=name) for name, raw, code in NESTED_FENCE_CASES],
    )
    def test_code_content_is_the_whole_block(self, raw: str, expected_code: str) -> None:
        parsed = parse_chat_response(raw)
        assert parsed.code_content == expected_code
        assert parsed.code_missing is False

    @pytest.mark.parametrize(
        ("raw",),
        [pytest.param(raw, id=name) for name, raw, _code in NESTED_FENCE_CASES],
    )
    def test_extracted_code_actually_compiles(self, raw: str) -> None:
        """截断的结果是编译不过的半截语句，所以这条断言能直接抓住回归

        先断非空再断可编译：`ast.parse("")` 是**通过**的（空模块），少了这一行，
        回退分支退化成「判成没有代码、正文置空」时这条会静悄悄地绿。
        """
        parsed = parse_chat_response(raw)
        assert parsed.code_content.strip()
        ast.parse(parsed.code_content)  # 回归时抛 SyntaxError: unterminated string literal

    @pytest.mark.parametrize(
        ("raw", "expected_code"),
        [pytest.param(raw, code, id=name) for name, raw, code in NESTED_FENCE_CASES],
    )
    def test_per_block_primary_alone_would_truncate(self, raw: str, expected_code: str) -> None:
        """反向对照：证明上面两条不是「本来就对」

        逐块匹配（主判据）在这两个输入上给出的**就是**被截断的半截语句。
        没有 `ast.parse` 兜底回退时，`parse_chat_response` 返回的正是这个值。
        """
        primary = _pick_longest_code_block(raw)
        assert primary != expected_code
        with pytest.raises(SyntaxError):
            ast.parse(primary)


# ---------------------------------------------------------------------------
# 2. ast.parse 闸门的边界
# ---------------------------------------------------------------------------

# (用例名, 无围栏的整段响应, 是否应判定为「没有代码」)
BARE_CONTENT_CASES: List[Tuple[str, str, bool]] = [
    # —— 必须继续当成代码放行（线上真实成功形态）——
    ("top_level_await", "await send_msg_text(_ck, 'hi')", False),
    ("top_level_return_in_snippet", "send_msg_text(_ck, 'hi')", False),
    ("comment_then_pass", "# 什么都不用做\npass", False),
    ("comment_only", "# 什么都不用做", False),
    ("pass_only", "pass", False),
    ("single_call_expression", "send_msg_text(_ck, '在的')", False),
    ("multi_statement", "a = 1\nb = 2\nsend_msg_text(_ck, str(a + b))", False),
    # —— 必须判成「没有代码」——
    ("chinese_refusal_with_punctuation", "抱歉，我不能协助处理这个请求。", True),
    ("english_refusal", "I'm sorry, but I can't help with that request.", True),
    ("prose_report", "好的，我已经把消息发送出去了！", True),
    # 中日文字符是合法标识符首字符，不带标点的短句拒答能通过 ast.parse，
    # 必须靠「孤立的裸名字/字面量」这道额外闸门掐掉
    ("bare_chinese_identifier", "你好", True),
    ("bare_english_identifier", "Sorry", True),
    ("bare_string_literal", "'抱歉，我做不到'", True),
]


class TestAstGateBoundaries:
    @pytest.mark.parametrize(
        ("raw", "expect_missing"),
        [pytest.param(raw, miss, id=name) for name, raw, miss in BARE_CONTENT_CASES],
    )
    def test_bare_content_classification(self, raw: str, expect_missing: bool) -> None:
        parsed = parse_chat_response(raw)
        assert parsed.code_missing is expect_missing
        # 判成「没有代码」时正文必须是空串，否则会被下游当代码送进沙盒
        assert (parsed.code_content == "") is expect_missing

    def test_gate_must_not_be_tightened_to_compile(self) -> None:
        """把闸门换成 `compile(..., "exec")` 就是一个真回归，这条钉住理由本身

        `ast.parse` 只做语法分析，`compile` 还会跑符号表检查——顶层 `await` 在前者通过、
        在后者报 `'await' outside function`。模型写顶层 `await` 是常见形态。
        """
        src = "await send_msg_text(_ck, 'hi')"
        ast.parse(src)  # 不抛
        with pytest.raises(SyntaxError, match="'await' outside function"):
            compile(src, "<probe>", "exec")
        assert parse_chat_response(src).code_missing is False


# ---------------------------------------------------------------------------
# 3. MANUAL（exit 9）不是失败形态
# ---------------------------------------------------------------------------

# 每一个 ExecStopType 成员是否计入无进展判据。
# 全量列举而不是只列 MANUAL：将来新增枚举成员时这条会红，逼着做一次显式决策。
STOP_TYPE_TRACKING: List[Tuple[ExecStopType, bool]] = [
    (ExecStopType.NORMAL, False),  # 直接 return，压根到不了判据
    (ExecStopType.ERROR, True),
    (ExecStopType.TIMEOUT, True),
    (ExecStopType.AGENT, False),  # 有意的续跑点
    (ExecStopType.MANUAL, False),  # exit(9)：运行时契约里给模型的调试手段，同属续跑点
    (ExecStopType.SECURITY, True),
    (ExecStopType.MULTIMODAL_AGENT, False),  # 有意的续跑点
]


class TestManualStopIsNotAStall:
    def test_every_stop_type_has_an_explicit_verdict(self) -> None:
        assert {stop for stop, _ in STOP_TYPE_TRACKING} == set(ExecStopType)
        for stop_type, tracked in STOP_TYPE_TRACKING:
            assert (stop_type in STALL_TRACKED_STOP_TYPES) is tracked, stop_type.name

    @pytest.mark.asyncio
    async def test_repeated_manual_debug_stops_use_all_iterations(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """连着两轮 `exit(9)` 且调试输出逐字相同时，**不能**被判成无进展

        取「输出逐字相同」这个最苛刻的形态：模型 print 的最后一行是固定分隔线时就是这样，
        而签名只取最后一行。把 `MANUAL` 放回 `STALL_TRACKED_STOP_TYPES`，
        这一轮会在第 2 次收尾时抛 `AgentRoundStalledError` 而不是跑满 3 轮。
        """
        debug_output = "state = {'a': 1}\n---- end of dump ----"
        harness = _Harness(
            max_retry=3,
            # 循环外 1 次 + 每轮末尾 1 次 = 4 次；给足这么多、且一条不多，
            # 多跑一轮就会撞上 `_ScriptExhausted` 而不是静悄悄通过
            parses=[_stub_parsed() for _ in range(4)],
            sandbox_results=[
                _SandboxResult(output=debug_output, stop_type=ExecStopType.MANUAL),
                _SandboxResult(output=debug_output, stop_type=ExecStopType.MANUAL),
                _SandboxResult(output=debug_output, stop_type=ExecStopType.MANUAL),
            ],
        )
        harness.install(monkeypatch)

        # MANUAL 被放回判据集合时，这里抛的会是 `AgentRoundStalledError`
        with pytest.raises(AgentRoundUnfinishedError):
            await harness.run()

        # 跑满 3 轮才收尾，而不是第 2 轮就被无进展检测掐掉
        assert harness.sandbox_calls == 3

    @pytest.mark.asyncio
    async def test_error_stop_still_stalls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """反向对照：同一个 harness 形状下 ERROR 仍然照常提前收尾

        没有这条，上面那条无法区分「MANUAL 被正确放行」与「无进展检测整个失效」。
        """
        err = "Exception: Error sending text message to chat: make sure the chat key is valid"
        harness = _Harness(
            max_retry=3,
            # 第 2 轮就该提前收尾，所以只给 2 条：真跑满 3 轮会撞 `_ScriptExhausted`
            parses=[_stub_parsed() for _ in range(2)],
            sandbox_results=[
                _SandboxResult(output=err, stop_type=ExecStopType.ERROR),
                _SandboxResult(output=err, stop_type=ExecStopType.ERROR),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError) as excinfo:
            await harness.run()

        assert excinfo.value.repeats == 2
        assert harness.sandbox_calls == 2


def _stub_parsed() -> Any:
    return cast(Any, _Parsed(code_content="send_msg_text(_ck, 'hi')"))
