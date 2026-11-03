"""Agent 回合的**收尾控制**回归测试（迭代循环 C 组）

## 保护的是什么

线上真实分布里，把迭代次数烧满的失败**绝大多数是确定性的**：

- 86× `send_msg_text` → `Error sending text message to chat: ...`（QQ 风控/禁言，重试必然再失败）
- 38× `生成嵌入向量失败，已重试 N 次: Illegal header value b'Bearer '`（模型组 API_KEY 是空串）
- 25× `请先在插件配置中填写绘图 API 地址。`（插件没配）
- 17× 模型调用签名写错

它们每一轮都返回**逐字相同**的错误，改造前却照样一轮一轮地重试到上限，最后抛
`AgentRoundUnfinishedError`，用户看到「这轮我没能把事情做完（已经试到最大迭代次数啦）」。
实测最长的一次烧了 8 轮（`onebot_v11-inst4-group_2000000011` 把 `AI_SCRIPT_MAX_RETRY_TIMES`
覆盖成了 8）。

另一条是「模型压根没在写代码」：安全拒答/纯散文回复经解析器判为 `code_missing`，
这类内容**一次都不该进沙盒**，而且重出多少次都是同一份拒答。

## 断言的基本要求

这一组里「抛了异常」本身是**空断言**——烧满迭代同样会抛异常。所以每条早停用例都必须
断言**实际进沙盒的次数严格小于上限**，并配一条「签名每次都不同 → 跑满上限」的反向对照，
证明判据没有被吹宽到「只要出错就早停」。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple, cast

import pytest

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent import run_agent as run_agent_module
from nekro_agent.services.agent.creator import OpenAIChatMessage
from nekro_agent.services.agent.resolver import ParsedCodeRunData
from nekro_agent.services.agent.run_agent import (
    CODE_MISSING_FEEDBACK,
    CODE_MISSING_MAX_ATTEMPTS,
    STALL_REPEAT_THRESHOLD,
    AgentNoExecutableCodeError,
    AgentRoundStalledError,
    AgentRoundUnfinishedError,
    RoundStallDetector,
    extract_error_head,
    is_code_missing,
    normalize_error_signature,
)

# --------------------------------------------------------------------------------------
# 线上真实错误文本（取自测试部署 exec_code 表与 prompts_error 日志，不要改写）
# --------------------------------------------------------------------------------------

SEND_TEXT_FAILURE = (
    "Error sending text message to chat: make sure the chat key is valid, "
    "you have permission to speak, and the message is not too long."
)
EMBEDDING_FAILURE_TPL = "生成嵌入向量失败，已重试 {n} 次: Illegal header value b'Bearer '"
DRAW_PLUGIN_FAILURE = "请先在插件配置中填写绘图 API 地址。"
STICKER_SIGNATURE_FAILURE = "send_matching_sticker() got multiple values for argument 'intent'"

CHAT_KEY = "onebot_v11-inst4-group_2000000011"


def make_traceback(*, line: int, call: str, message: str, exc: str = "Exception") -> str:
    """拼一段与沙盒里长得一样的 traceback

    行号与调用实参每一轮都会变（模型每次重写代码），这正是归一化必须吃掉的部分。
    """
    return (
        "Traceback (most recent call last):\n"
        f'  File "/app/run_script.py", line {line}, in <module>\n'
        f"    {call}\n"
        '  File "/app/ext_caller.py", line 88, in _call\n'
        '    raise RuntimeError(result["error"])\n'
        f"{exc}: {message}"
    )


# --------------------------------------------------------------------------------------
# 归一化：把「每次必然不同、却与是不是同一个错无关」的部分吃掉
# --------------------------------------------------------------------------------------


class TestExtractErrorHead:
    """错误头部提取：帧行不算，最后那条异常行才算"""

    def test_traceback_tail_is_the_exception_line(self) -> None:
        head = extract_error_head(
            make_traceback(line=14, call='send_msg_text("chat", "早上好呀")', message=SEND_TEXT_FAILURE),
        )
        assert head == f"Exception: {SEND_TEXT_FAILURE}"
        assert "run_script.py" not in head, "帧行进了头部的话，行号一变签名就永远不重复"

    def test_syntax_error_form_keeps_the_error_line(self) -> None:
        """B1 围栏泄漏在线上的真实形态（exec_code id=531）：报错行在 caret 行之后"""
        output = (
            "Traceback (most recent call last):\n"
            '  File "/app/run_script.py", line 9\n'
            "    ```\n"
            "    ^\n"
            "SyntaxError: invalid syntax"
        )
        assert extract_error_head(output) == "SyntaxError: invalid syntax"

    def test_stdout_noise_before_traceback_is_skipped(self) -> None:
        output = "开始处理\n处理中...\n" + make_traceback(line=3, call="x()", message=DRAW_PLUGIN_FAILURE)
        assert extract_error_head(output) == f"Exception: {DRAW_PLUGIN_FAILURE}"

    def test_bare_message_without_traceback_falls_back_to_last_line(self) -> None:
        """插件直接把错误打出来、以及超时兜底那行注释，都是没有 `X: ` 前缀的裸文本"""
        assert extract_error_head(f"一些输出\n{DRAW_PLUGIN_FAILURE}") == DRAW_PLUGIN_FAILURE
        assert (
            extract_error_head("# This container has been killed because it exceeded the 120 seconds limit.")
            == "# This container has been killed because it exceeded the 120 seconds limit."
        )

    def test_truncated_traceback_never_falls_back_to_a_frame_line(self) -> None:
        """超时收尾会 `container.kill()`，日志可能就断在 traceback 中间

        这时没有异常尾行，走的是「退回最后一行」的兜底。兜底里如果不剔掉帧行，头部就变成
        `File "/app/run_script.py", line 14, in <module>` —— 行号每轮都变，无进展检测永远不触发。
        """
        cut_at_frame = (
            "Traceback (most recent call last):\n"
            "    payload = build()\n"
            '  File "/app/run_script.py", line 14, in <module>'
        )
        head = extract_error_head(cut_at_frame)
        assert head == "payload = build()"
        assert "line 14" not in head

    def test_truncated_tracebacks_with_different_line_numbers_share_a_signature(self) -> None:
        left = 'x = go()\n  File "/app/run_script.py", line 14, in <module>'
        right = 'x = go()\n  File "/app/run_script.py", line 203, in <module>'
        assert normalize_error_signature(ExecStopType.TIMEOUT, left) == normalize_error_signature(
            ExecStopType.TIMEOUT,
            right,
        )

    def test_empty_output_is_empty_head(self) -> None:
        assert extract_error_head("") == ""
        assert extract_error_head("   \n\n  ") == ""
        assert extract_error_head('  File "/app/run_script.py", line 14, in <module>') == ""


class TestNormalizeErrorSignature:
    """签名归一化的表驱动契约：该合的合、该分的分"""

    # (用例名, 输出 A, 输出 B) —— 期望同签名
    SAME_CASES: List[Tuple[str, str, str]] = [
        (
            "行号与调用实参不同、错误相同",
            make_traceback(line=14, call='send_msg_text("chat", "早上好呀")', message=SEND_TEXT_FAILURE),
            make_traceback(line=27, call='send_msg_text("chat", "晚安啦")', message=SEND_TEXT_FAILURE),
        ),
        (
            "嵌入重试次数每轮递增",
            EMBEDDING_FAILURE_TPL.format(n=3),
            EMBEDDING_FAILURE_TPL.format(n=7),
        ),
        (
            "临时文件路径不同",
            "FileNotFoundError: [Errno 2] No such file or directory: '/app/shared/a1b2c3.png'",
            "FileNotFoundError: [Errno 2] No such file or directory: '/app/shared/zz9911.png'",
        ),
        (
            "时间戳不同",
            "RuntimeError: upstream timeout at 2026-08-17 03:11:09",
            "RuntimeError: upstream timeout at 2026-08-18 22:40:51",
        ),
        (
            "对象地址不同",
            "TypeError: cannot pickle <object at 0x7f9c1a2b3c40>",
            "TypeError: cannot pickle <object at 0x7fa0deadbeef>",
        ),
    ]

    # 期望**不同**签名的成对样本（反向对照：证明归一化没有把一切都抹平）
    DIFFERENT_OUTPUTS: List[Tuple[str, str]] = [
        ("发消息被风控", f"Exception: {SEND_TEXT_FAILURE}"),
        ("嵌入 Key 为空", EMBEDDING_FAILURE_TPL.format(n=3)),
        ("绘图插件没配", f"Exception: {DRAW_PLUGIN_FAILURE}"),
        ("贴纸调用签名错", f"TypeError: {STICKER_SIGNATURE_FAILURE}"),
        ("围栏泄漏", "SyntaxError: invalid character '：' (U+FF1A)"),
    ]

    @pytest.mark.parametrize(("name", "left", "right"), SAME_CASES, ids=[c[0] for c in SAME_CASES])
    def test_volatile_parts_are_normalized_away(self, name: str, left: str, right: str) -> None:
        assert normalize_error_signature(ExecStopType.ERROR, left) == normalize_error_signature(
            ExecStopType.ERROR,
            right,
        ), f"{name}：同一个错误却算出了两个签名，无进展检测永远不会触发"

    def test_signature_keeps_the_discriminating_text(self) -> None:
        """归一化不能把内容抹光：`ERROR|<empty>` 这种退化签名会把所有错误合成一个"""
        signature = normalize_error_signature(
            ExecStopType.ERROR,
            make_traceback(line=14, call="send_msg_text()", message=SEND_TEXT_FAILURE),
        )
        assert signature.startswith("ERROR|")
        assert "Error sending text message to chat" in signature
        assert "<empty>" not in signature

    def test_embedding_signature_keeps_the_cause(self) -> None:
        signature = normalize_error_signature(ExecStopType.ERROR, EMBEDDING_FAILURE_TPL.format(n=3))
        assert "Illegal header value" in signature
        assert "生成嵌入向量失败" in signature

    def test_distinct_errors_stay_distinct(self) -> None:
        signatures = {
            name: normalize_error_signature(ExecStopType.ERROR, output) for name, output in self.DIFFERENT_OUTPUTS
        }
        assert len(set(signatures.values())) == len(self.DIFFERENT_OUTPUTS), (
            f"判据被吹宽了，不同故障折成了同一个签名: {signatures}"
        )

    def test_stop_type_is_part_of_the_signature(self) -> None:
        """同样一段输出，超时收尾与错误收尾不是同一件事"""
        output = f"Exception: {SEND_TEXT_FAILURE}"
        assert normalize_error_signature(ExecStopType.ERROR, output) != normalize_error_signature(
            ExecStopType.TIMEOUT,
            output,
        )

    def test_empty_output_still_yields_a_comparable_signature(self) -> None:
        """SECURITY 收尾没有沙盒输出，但它照样需要一个能自比的签名"""
        assert normalize_error_signature(ExecStopType.SECURITY, "") == "SECURITY|<empty>"
        assert normalize_error_signature(ExecStopType.SECURITY, "") != normalize_error_signature(
            ExecStopType.ERROR,
            "",
        )

    def test_signature_is_length_bounded(self) -> None:
        """签名要进日志，不能把一整段超长 traceback 尾巴带上"""
        signature = normalize_error_signature(ExecStopType.ERROR, "ValueError: " + "x" * 5000)
        assert len(signature) < 400


# --------------------------------------------------------------------------------------
# 无进展判据本体
# --------------------------------------------------------------------------------------


class TestRoundStallDetector:
    def test_fresh_detector_is_not_stalled(self) -> None:
        detector = RoundStallDetector()
        assert detector.stalled is False
        assert detector.repeats == 0

    def test_same_signature_twice_stalls(self) -> None:
        detector = RoundStallDetector()
        assert detector.note("ERROR|a") == 1
        assert detector.stalled is False, "第一次报错就放弃等于把迭代重试机制废掉"
        assert detector.note("ERROR|a") == 2
        assert detector.stalled is True

    def test_interleaved_signature_resets_the_streak(self) -> None:
        """判据是「**连续**相同」而不是「累计出现两次」：中间换了个错说明模型确实改了东西"""
        detector = RoundStallDetector()
        for signature in ("ERROR|a", "ERROR|b", "ERROR|a"):
            detector.note(signature)
            assert detector.stalled is False, f"{signature} 之后不该判定无进展"
        assert detector.repeats == 1

    def test_new_signature_after_a_stall_resets(self) -> None:
        detector = RoundStallDetector()
        detector.note("ERROR|a")
        detector.note("ERROR|a")
        assert detector.stalled is True
        assert detector.note("ERROR|b") == 1
        assert detector.stalled is False

    def test_threshold_floor_is_two(self) -> None:
        """阈值 1 = 第一次报错就放弃，必须被挡住"""
        detector = RoundStallDetector(threshold=1)
        assert detector.threshold == 2
        detector.note("ERROR|a")
        assert detector.stalled is False

    def test_higher_threshold_is_honored(self) -> None:
        detector = RoundStallDetector(threshold=3)
        detector.note("ERROR|a")
        detector.note("ERROR|a")
        assert detector.stalled is False
        detector.note("ERROR|a")
        assert detector.stalled is True


class TestIsCodeMissing:
    """与解析器组（B）之间的字段契约"""

    def test_true_when_flag_set(self) -> None:
        assert is_code_missing(cast(ParsedCodeRunData, _Parsed(code_missing=True))) is True

    def test_false_when_flag_absent_or_off(self) -> None:
        assert is_code_missing(cast(ParsedCodeRunData, _Parsed(code_missing=False))) is False
        legacy = ParsedCodeRunData(raw_content="x", code_content="print(1)", thought_chain="")
        assert is_code_missing(legacy) is False, "字段还没合进来时必须等价于旧行为，不能反过来把所有回合都拦掉"


# --------------------------------------------------------------------------------------
# `run_agent` 迭代循环的集成用例
# --------------------------------------------------------------------------------------


@dataclass
class _Parsed:
    """`ParsedCodeRunData` 的替身

    解析器组（B）正在同一轮里给真模型加 `code_missing` 字段；这里用替身是为了让两组的
    落地顺序互不阻塞。`run_agent` 只读 `.code_content` 与 `.code_missing`，其余原样透传给
    （本用例中被替换掉的）沙盒。
    """

    code_content: str = "print(1)"
    code_missing: bool = False
    raw_content: str = "raw"
    thought_chain: str = ""


@dataclass
class _SandboxResult:
    output: str
    stop_type: ExecStopType


@dataclass
class _LogRecord:
    level: str
    text: str


class _RecordingLogger:
    """替掉 loguru：日志内容本身是本组要保护的契约（提前收尾必须可诊断）"""

    def __init__(self) -> None:
        self.records: List[_LogRecord] = []

    def _add(self, level: str, message: Any) -> None:
        self.records.append(_LogRecord(level=level, text=str(message)))

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._add("DEBUG", message)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._add("INFO", message)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._add("WARNING", message)

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._add("ERROR", message)

    def exception(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._add("ERROR", message)

    def texts(self, level: str) -> List[str]:
        return [record.text for record in self.records if record.level == level]


class _ScriptExhausted(AssertionError):
    """脚本被消费得比预期多 —— 说明循环跑的轮数与用例的期望对不上，必须响亮地红"""


@dataclass
class _Harness:
    """驱动真实 `run_agent` 的最小可注入环境

    只替换外部依赖（LLM、沙盒、DB、广播、模板），迭代循环本体跑的是**真代码**。
    只测判据函数不测循环，等于放过「判据算出来了但循环没用它」这一类变异。
    """

    max_retry: int
    parses: List[_Parsed]
    sandbox_results: List[_SandboxResult] = field(default_factory=list)
    # 两条提前收尾判据的阈值现在走会话覆盖层（`SESSION_*`）。默认取模块常量而不是字面量 2：
    # 「配置项默认值」与「模块常量」是同一个东西的两份清单，harness 抄第三份就白测了。
    stall_threshold: int = STALL_REPEAT_THRESHOLD
    code_missing_limit: int = CODE_MISSING_MAX_ATTEMPTS

    parse_calls: int = 0
    llm_calls: int = 0
    # `render_history_data` 不是纯装配：它内部还会再发一次 LLM 规划请求
    # （`LLMUsageScene.HISTORY_PLAN`）、跑一批向量检索，并对命中的记忆做**读改写落库**
    # （`record_access` → `reinforce()`）。#58 的 `break` 位置决定它在末轮跑不跑，
    # 而挪动 `break` 不会改变 `llm_calls`——只数请求数的话那个变异会活着，所以单独计数。
    history_renders: int = 0
    sandbox_codes: List[str] = field(default_factory=list)
    llm_last_user_text: List[str] = field(default_factory=list)
    events: List[Any] = field(default_factory=list)
    logger: _RecordingLogger = field(default_factory=_RecordingLogger)
    ctx: Any = None

    @property
    def sandbox_calls(self) -> int:
        return len(self.sandbox_codes)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import nekro_agent.services.system_broadcast as broadcast_module

        model_group = SimpleNamespace(
            CHAT_MODEL="gemini-3.6-flash",
            ENABLE_COT=False,
            BASE_URL="https://example.invalid/v1",
            API_KEY="",
            CHAT_PROXY="",
        )
        config = SimpleNamespace(
            MODEL_GROUPS={"default": model_group},
            USE_MODEL_GROUP="default",
            AI_SCRIPT_MAX_RETRY_TIMES=self.max_retry,
            AI_CHAT_LLM_API_MAX_RETRIES=3,
            SAVE_PROMPTS_LOG=False,
            AI_CHAT_CONTEXT_EXPIRE_SECONDS=3600,
            SESSION_STALL_REPEAT_THRESHOLD=self.stall_threshold,
            SESSION_CODE_MISSING_MAX_ATTEMPTS=self.code_missing_limit,
        )
        adapter = SimpleNamespace(
            chat_key_rules=["group_<id>"],
            config=SimpleNamespace(SESSION_ENABLE_AT=False),
            set_dialog_example=_async_value([]),
            get_jinja_env=_async_value(None),
            get_self_info=_async_value(SimpleNamespace(platform_name="qq", user_id="10001")),
            render_runtime_prompt=_async_value(""),
        )
        preset = SimpleNamespace(id=1, name="默认人设", content="人设正文")
        channel = SimpleNamespace(
            chat_key=CHAT_KEY,
            channel_name="测试群",
            channel_type="group",
            instance_key="onebot_v11-inst4",
            adapter_key="onebot_v11",
            adapter=adapter,
            get_effective_config=_async_value(config),
            get_preset=_async_value(preset),
        )
        self.ctx = SimpleNamespace(db_chat_channel=channel, adapter=adapter)

        async def _publish(event: Any) -> None:
            self.events.append(event)

        async def _send_agent_request(**kwargs: Any) -> Tuple[Any, Any, List[str]]:
            self.llm_calls += 1
            messages: List[OpenAIChatMessage] = kwargs["messages"]
            self.llm_last_user_text.append(_message_text(messages[-1]))
            return (
                SimpleNamespace(response_content="<llm response>", use_model="gemini-3.6-flash", thought_chain=""),
                model_group,
                [],
            )

        def _parse(_content: str) -> _Parsed:
            if self.parse_calls >= len(self.parses):
                raise _ScriptExhausted(f"解析脚本只有 {len(self.parses)} 条，却被取用了第 {self.parse_calls + 1} 次")
            parsed = self.parses[self.parse_calls]
            self.parse_calls += 1
            return parsed

        async def _limited_run_code(**kwargs: Any) -> Tuple[str, str, int]:
            index = len(self.sandbox_codes)
            if index >= len(self.sandbox_results):
                raise _ScriptExhausted(f"沙盒脚本只有 {len(self.sandbox_results)} 条，却被取用了第 {index + 1} 次")
            self.sandbox_codes.append(kwargs["code_run_data"].code_content)
            result = self.sandbox_results[index]
            return result.output, result.output, result.stop_type.value

        async def _render_history_data(**_kwargs: Any) -> OpenAIChatMessage:
            self.history_renders += 1
            return OpenAIChatMessage.from_text("user", "[history]")

        class _FakePromptCompiler:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            def render_system_message(self) -> OpenAIChatMessage:
                return OpenAIChatMessage.from_text("system", "[system]")

            def render_practice_messages(self, *_args: Any, **_kwargs: Any) -> List[OpenAIChatMessage]:
                return []

            async def render_history_message(self, **_kwargs: Any) -> OpenAIChatMessage:
                return OpenAIChatMessage.from_text("user", "[history]")

        monkeypatch.setattr(broadcast_module, "publish_system_event", _publish)
        monkeypatch.setattr(run_agent_module, "logger", self.logger)
        monkeypatch.setattr(run_agent_module, "send_agent_request", _send_agent_request)
        monkeypatch.setattr(run_agent_module, "parse_chat_response", _parse)
        monkeypatch.setattr(run_agent_module, "limited_run_code", _limited_run_code)
        monkeypatch.setattr(run_agent_module, "render_history_data", _render_history_data)
        monkeypatch.setattr(run_agent_module, "PromptCompiler", _FakePromptCompiler)
        monkeypatch.setattr(run_agent_module, "enforce_channel_model_group_scope", _async_noop)
        monkeypatch.setattr(run_agent_module, "resolve_active_plugins", _async_value([]))
        monkeypatch.setattr(
            run_agent_module,
            "render_plugins_prompt",
            _async_value(SimpleNamespace(system_prompt="", runtime_prompt="")),
        )
        monkeypatch.setattr(run_agent_module, "render_affection_prompt", _async_value(""))
        monkeypatch.setattr(run_agent_module, "schedule_affection_evaluation", lambda *_a, **_k: None)

    async def run(self) -> None:
        await run_agent_module.run_agent(chat_key=CHAT_KEY, ctx=cast(Any, self.ctx))

    def phases(self) -> List[str]:
        return [getattr(event, "phase", "") for event in self.events]


def _async_noop(*_args: Any, **_kwargs: Any) -> Any:
    async def _inner() -> None:
        return None

    return _inner()


def _async_value(value: Any) -> Any:
    async def _inner(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _inner


def _message_text(message: OpenAIChatMessage) -> str:
    payload: Dict[str, Any] = message.to_dict()
    content = payload.get("content")
    if isinstance(content, str):
        return content
    return "".join(segment.get("text", "") for segment in content or [] if isinstance(segment, dict))


def _error_result(message: str, *, line: int = 14, call: str = "send_msg_text()") -> _SandboxResult:
    return _SandboxResult(
        output=make_traceback(line=line, call=call, message=message),
        stop_type=ExecStopType.ERROR,
    )


class TestStallEarlyExit:
    """同一错误签名连续复现 → 提前收尾（核心：实际迭代次数必须 < 上限）"""

    def test_repeated_identical_error_stops_before_the_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(
            max_retry=3,
            parses=[_Parsed(), _Parsed()],
            sandbox_results=[
                _error_result(SEND_TEXT_FAILURE, line=14, call='send_msg_text(chat, "早上好呀")'),
                _error_result(SEND_TEXT_FAILURE, line=27, call='send_msg_text(chat, "在的在的")'),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError) as excinfo:
            asyncio.run(harness.run())

        # 「抛了异常」不算断言 —— 烧满迭代也会抛。真正的契约是**没有跑满**
        assert harness.sandbox_calls == STALL_REPEAT_THRESHOLD == 2
        assert harness.sandbox_calls < harness.max_retry
        assert excinfo.value.repeats == 2
        assert "Error sending text message to chat" in excinfo.value.signature

    def test_worst_case_channel_override_stops_at_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """线上最长那次：该频道把 `AI_SCRIPT_MAX_RETRY_TIMES` 覆盖成 8，烧满了 8 轮"""
        harness = _Harness(
            max_retry=8,
            parses=[_Parsed(), _Parsed()],
            sandbox_results=[
                _SandboxResult(output=EMBEDDING_FAILURE_TPL.format(n=3), stop_type=ExecStopType.ERROR),
                _SandboxResult(output=EMBEDDING_FAILURE_TPL.format(n=5), stop_type=ExecStopType.ERROR),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 2, "覆盖层把上限调大时，早停判据必须仍然只用两轮"
        assert harness.llm_calls == 2

    def test_stall_is_diagnosable_in_logs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """提前收尾不能以损失可诊断性为代价：签名、重复次数都要落进 ERROR 日志"""
        harness = _Harness(
            max_retry=4,
            parses=[_Parsed(), _Parsed()],
            sandbox_results=[
                _error_result(DRAW_PLUGIN_FAILURE),
                _error_result(DRAW_PLUGIN_FAILURE, line=31),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError) as excinfo:
            asyncio.run(harness.run())

        errors = harness.logger.texts("ERROR")
        assert errors, "提前放弃却一行 ERROR 都不打，排查时与「本来就只跑了两轮」无法区分"
        stall_logs = [text for text in errors if excinfo.value.signature in text]
        assert stall_logs, f"ERROR 日志里没有签名本身: {errors}"
        assert any("2" in text and "提前放弃" in text for text in stall_logs)

    def test_runtime_state_semantics_are_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既有的 `publish_runtime_state` 语义不能被早停打断"""
        harness = _Harness(
            max_retry=3,
            parses=[_Parsed(), _Parsed()],
            sandbox_results=[
                _error_result(SEND_TEXT_FAILURE),
                _error_result(SEND_TEXT_FAILURE, line=99),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError):
            asyncio.run(harness.run())

        phases = harness.phases()
        assert phases.count("sandbox_running") == 2
        assert phases.count("sandbox_stopped") == 2
        assert phases[-1] == "failed", f"早停这条路径没有收尾事件，前端进度条会一直挂着: {phases}"
        assert all(event.iteration_total == harness.max_retry + 1 for event in harness.events)


class TestNoStallWhenErrorsDiffer:
    """反向对照：判据不能吹宽到「只要出错就早停」"""

    def test_distinct_errors_burn_every_iteration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(
            max_retry=3,
            parses=[_Parsed(), _Parsed(), _Parsed()],
            sandbox_results=[
                _error_result(SEND_TEXT_FAILURE),
                _error_result(DRAW_PLUGIN_FAILURE),
                _SandboxResult(output=f"TypeError: {STICKER_SIGNATURE_FAILURE}", stop_type=ExecStopType.ERROR),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == harness.max_retry == 3
        # 跑满迭代的回合恰好发 `max_retry` 次请求。改造前是 `max_retry + 1` ——
        # 最后一趟末尾还会再发一次含全量上下文的请求，而它的结果无人消费。
        assert harness.llm_calls == harness.max_retry
        assert harness.parse_calls == harness.llm_calls, "有响应被解析了却没有对应的请求（或反过来）"

    def test_interleaved_repeat_does_not_early_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A、B、A：同一个错出现了两次但不连续，说明模型确实在变，不该早停"""
        harness = _Harness(
            max_retry=3,
            parses=[_Parsed(), _Parsed(), _Parsed(), _Parsed()],
            sandbox_results=[
                _error_result(SEND_TEXT_FAILURE),
                _error_result(DRAW_PLUGIN_FAILURE),
                _error_result(SEND_TEXT_FAILURE, line=52),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 3

    def test_agent_continuation_rounds_are_never_treated_as_a_stall(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`AGENT` 是**有意**的续跑点，不是失败

        健康流程里它完全可能连着返回同一段文本（`kb_search` 两次不同查询都返回「未找到相关内容」、
        `memory_query` 连着两次空结果）。把它计入无进展判据 = 把正常对话拦腰砍断。
        """
        empty_hit = "未找到相关内容"
        harness = _Harness(
            max_retry=3,
            parses=[_Parsed(), _Parsed(), _Parsed(), _Parsed()],
            sandbox_results=[
                _SandboxResult(output=empty_hit, stop_type=ExecStopType.AGENT),
                _SandboxResult(output=empty_hit, stop_type=ExecStopType.AGENT),
                _SandboxResult(output=empty_hit, stop_type=ExecStopType.AGENT),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 3, "连续三次相同的 AGENT 返回被误判成了无进展"

    def test_stall_detection_still_fires_right_after_an_agent_round(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """排除续跑点不能顺手把失败检测也关掉：AGENT 之后的两次相同 ERROR 照样早停"""
        harness = _Harness(
            max_retry=4,
            parses=[_Parsed(), _Parsed(), _Parsed()],
            sandbox_results=[
                _SandboxResult(output="查到了 3 条", stop_type=ExecStopType.AGENT),
                _error_result(SEND_TEXT_FAILURE),
                _error_result(SEND_TEXT_FAILURE, line=44),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 3
        assert harness.sandbox_calls < harness.max_retry

    def test_normal_exit_still_returns_without_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """成功路径不能被收尾控制带坏"""
        harness = _Harness(
            max_retry=3,
            parses=[_Parsed()],
            sandbox_results=[_SandboxResult(output="done", stop_type=ExecStopType.NORMAL)],
        )
        harness.install(monkeypatch)

        asyncio.run(harness.run())

        assert harness.sandbox_calls == 1
        assert harness.phases()[-1] == "completed"


class TestCodeMissingHandling:
    """`code_missing` 一次都不该进沙盒，且最多只给一次重出机会"""

    def test_first_miss_retries_with_targeted_feedback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = _Harness(
            max_retry=3,
            parses=[_Parsed(code_content="", code_missing=True), _Parsed(code_content="print('ok')")],
            sandbox_results=[_SandboxResult(output="done", stop_type=ExecStopType.NORMAL)],
        )
        harness.install(monkeypatch)

        asyncio.run(harness.run())

        # 第一轮跳过沙盒、第二轮才真的执行，且执行的是**重出后**的代码
        assert harness.sandbox_codes == ["print('ok')"]
        assert harness.llm_calls == 2
        feedback = harness.llm_last_user_text[1]
        assert CODE_MISSING_FEEDBACK in feedback, "重出请求里没有针对性反馈，模型不知道自己错在哪"
        assert "[Sandbox Output]" not in feedback, "根本没进沙盒，不能伪造一条沙盒错误回喂给模型"
        assert harness.phases().count("sandbox_running") == 1

    def test_second_consecutive_miss_stops_without_touching_the_sandbox(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        harness = _Harness(
            max_retry=3,
            parses=[
                _Parsed(code_content="", code_missing=True),
                _Parsed(code_content="", code_missing=True),
            ],
            sandbox_results=[],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentNoExecutableCodeError) as excinfo:
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 0, "安全拒答/散文回复一次都不该被当成 Python 送进容器"
        assert harness.llm_calls == CODE_MISSING_MAX_ATTEMPTS == 2
        assert harness.llm_calls < harness.max_retry + 1
        assert excinfo.value.attempts == 2
        assert "sandbox_running" not in harness.phases()
        assert harness.phases()[-1] == "failed"

    def test_miss_streak_resets_after_a_successful_extraction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """打滑 → 出代码 → 又打滑：不是「连续两次」，不该在第三轮就放弃"""
        harness = _Harness(
            max_retry=4,
            parses=[
                _Parsed(code_content="", code_missing=True),
                _Parsed(code_content="print('a')"),
                _Parsed(code_content="", code_missing=True),
                _Parsed(code_content="print('b')"),
            ],
            sandbox_results=[
                _error_result(SEND_TEXT_FAILURE),
                _SandboxResult(output="done", stop_type=ExecStopType.NORMAL),
            ],
        )
        harness.install(monkeypatch)

        asyncio.run(harness.run())

        assert harness.sandbox_codes == ["print('a')", "print('b')"]

    def test_code_missing_round_is_not_counted_as_a_stall_signature(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """没进沙盒的那一轮不该往签名计数里塞东西，否则两条判据会互相污染"""
        harness = _Harness(
            max_retry=4,
            parses=[
                _Parsed(code_content="", code_missing=True),
                _Parsed(code_content="print('a')"),
                _Parsed(code_content="print('b')"),
                _Parsed(code_content="print('c')"),
                _Parsed(code_content="print('d')"),
            ],
            sandbox_results=[
                _error_result(SEND_TEXT_FAILURE),
                _error_result(DRAW_PLUGIN_FAILURE),
                _error_result(STICKER_SIGNATURE_FAILURE),
            ],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundUnfinishedError):
            asyncio.run(harness.run())

        assert harness.sandbox_calls == 3


class TestExceptionContract:
    """三条收尾异常必须互相可辨识 —— `message_service` 靠 isinstance 分派回执文案"""

    def test_new_errors_are_not_subclasses_of_the_old_one(self) -> None:
        stalled = AgentRoundStalledError("x", signature="ERROR|a", repeats=2)
        no_code = AgentNoExecutableCodeError("x", attempts=2)
        assert not isinstance(stalled, AgentRoundUnfinishedError)
        assert not isinstance(no_code, AgentRoundUnfinishedError)
        assert not isinstance(stalled, AgentNoExecutableCodeError)
        assert not isinstance(no_code, AgentRoundStalledError)

    def test_diagnostic_payload_is_carried_on_the_exception(self) -> None:
        stalled = AgentRoundStalledError("x", signature="ERROR|boom", repeats=3)
        assert stalled.signature == "ERROR|boom"
        assert stalled.repeats == 3
        assert AgentNoExecutableCodeError("x", attempts=2).attempts == 2

    def test_each_round_failure_maps_to_its_own_notice(self) -> None:
        """回执文案必须两两不同：三条路径对用户意味着不同的下一步"""
        from nekro_agent.services.agent.failure_feedback import (
            NOTICE_ROUND_NO_CODE,
            NOTICE_ROUND_STALLED,
            NOTICE_ROUND_UNFINISHED,
        )

        notices = [NOTICE_ROUND_NO_CODE, NOTICE_ROUND_STALLED, NOTICE_ROUND_UNFINISHED]
        assert all(notice.strip() for notice in notices)
        assert len(set(notices)) == 3


class TestMessageServiceDispatch:
    """回执分派：新异常各自有文案，且**没有绕过**冷却闸门"""

    @staticmethod
    def _source() -> str:
        from nekro_agent.services import message_service  # noqa: PLC0415  局部导入，避免整份测试拉起消息层

        return Path(message_service.__file__).read_text(encoding="utf-8")

    @staticmethod
    def _branch_body(source: str, exception_name: str) -> str:
        """取 `isinstance(e, <异常>)` 那一行**紧接着的**分支体

        不用「命中行之后的 N 个字符」：那个窗口会滑进下一条 elif，于是把两条分支的文案
        对调（M：NO_CODE 用 STALLED 的文案）照样全绿。按行取正下方一行才杀得掉这种变异。
        """
        lines = source.splitlines()
        marker = f"isinstance(e, {exception_name}):"
        for index, line in enumerate(lines):
            if marker in line:
                body = [candidate for candidate in lines[index + 1 :] if candidate.strip()]
                assert body, f"{exception_name} 分支下面是空的"
                return body[0]
        raise AssertionError(f"message_service 里找不到 {exception_name} 的分派分支")

    def test_new_exceptions_get_their_own_notice(self) -> None:
        source = self._source()
        assert "AgentNoExecutableCodeError" in source
        assert "AgentRoundStalledError" in source
        assert "NOTICE_ROUND_NO_CODE" in self._branch_body(source, "AgentNoExecutableCodeError")
        assert "NOTICE_ROUND_STALLED" in self._branch_body(source, "AgentRoundStalledError")

    def test_new_notices_still_go_through_the_throttle(self) -> None:
        """两条新分支都走 `_notify_round_failure`，冷却闸门在它里面 —— 直接调发送就是绕过"""
        source = self._source()
        for exception_name in ("AgentNoExecutableCodeError", "AgentRoundStalledError"):
            body = self._branch_body(source, exception_name)
            assert "_notify_round_failure(" in body, f"{exception_name} 分支绕过了 failure_notice_throttle"
            assert "send_operation_message" not in body

    def test_unfinished_branch_survives(self) -> None:
        """真的烧满迭代时仍然抛 `AgentRoundUnfinishedError` 并给原文案"""
        source = self._source()
        assert "NOTICE_ROUND_UNFINISHED" in self._branch_body(source, "AgentRoundUnfinishedError")
