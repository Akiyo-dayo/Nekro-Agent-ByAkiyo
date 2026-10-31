"""对抗式复核（empty-assert 专项）补的**覆盖缺口**。

这一份不重复 `test_agent_response_parsing.py` / `test_agent_round_control.py` /
`test_agent_llm_content_block.py` / `test_agent_failure_wiring.py` 已经钉住的判据。
它只装那些经**变异测试实测**、「把修复删掉四份用例照样全绿」的地方 —— 也就是当前
一条断言都没有的产品行为。每个小节都标注了它杀掉的那个变异，以及实测结论。

变异清单（在仓库的**隔离副本**里跑：并行会话在同一棵树上做各自的变异测试，
互相覆盖会把真红读成假绿）：

| 变异 | 内容 | 补之前 | 补之后 |
|------|------|--------|--------|
| P1 | 去掉 `_is_meaningless_single_expression` 闸门 | 全绿（存活） | 红 ×7 |
| P2 | `_pick_longest_code_block` 退回纯按长度取块 | 全绿（存活） | 红 |
| P3 | 围栏块编译不过时**照旧送进沙盒**（不置 `code_missing`） | 全绿（存活） | 红 ×3 |
| P4 | `_pick_longest_legacy_span` 恒返回空串 | 全绿（存活） | 红 ×2 |
| P5 | 回退候选取自整段而不是 `<think>` 之后 | 全绿（存活） | 红 |
| P7/P8 | 从 `STALL_TRACKED_STOP_TYPES` 里删掉 TIMEOUT / SECURITY | 全绿（存活） | 红 |
| P9 | 把 `MANUAL` 加回 `STALL_TRACKED_STOP_TYPES` | 全绿（存活） | 红 ×2 |
| M5 | `_finalize_code` 收尾不再 `.strip()` | 全绿（存活） | 红 |
| N6 | `extract_error_head` 从**前**往后扫 | 全绿（存活） | 红 ×2 |
| M24 | 无进展日志不再写「连续重复 N 次」 | 全绿（存活） | 红 |

## 与 `test_agent_fenced_prose_gate.py` / `test_agent_regression_guard.py` 的重叠

那两份是**同一轮里另一路复核**在本文件写完前后落地的，它们与这里有交集。
落地后重跑同一批变异，得到的归属是：

* **只有本文件杀得掉**：P5（回退候选的取值范围）、N6（错误头部的扫描方向）、
  M24（无进展日志里的重复次数）、M5（`from plugins` 剃除后的前导空白）、
  以及 TIMEOUT / SECURITY **走真迭代循环**的早停（那两份钉的是集合成员，不驱动循环）；
* **两边都杀得掉**：P1（裸名字/字面量）、P2（选块偏好）、P3（围栏内散文）、P4（旧跨度回退）。

重叠的那几组保留而不删：它们各自用的是不同的样本，且删测试比留冗余风险大。
真要收敛的话，删本文件里的「一」「二」两节即可，「三」往后是独有的。

全部确定性：显式输入、精确期望输出，无网络、无 sleep、无随机。
"""

from __future__ import annotations

import ast
import asyncio
from typing import List, Tuple

import pytest

# 复用兄弟用例已经搭好的可注入环境（`tests/` 下没有 `__init__.py`，pytest 的 prepend
# 导入模式会把该目录放进 sys.path），不再抄一份一百行的替身。
from test_agent_round_control import (
    SEND_TEXT_FAILURE,
    _Harness,
    _Parsed,
    _SandboxResult,
    make_traceback,
)

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent.resolver import parse_chat_response
from nekro_agent.services.agent.run_agent import (
    STALL_TRACKED_STOP_TYPES,
    AgentRoundStalledError,
    extract_error_head,
    normalize_error_signature,
)


def _compiles(code: str) -> bool:
    try:
        compile(code, "<sandbox>", "exec")
    except SyntaxError:
        return False
    return True


def _parses_as_python(text: str) -> bool:
    """样本自检用：这段文本在**语法上**是不是 Python

    `ast.parse` 那道闸门只回答这个问题。下面「裸文本」那一组的每条样本都必须在这里返回
    True —— 否则它们只是「恰好因为有个全角标点而编译不过」的简单样本，
    把闸门整个删掉也照样绿，断言就没有指认任何东西。
    """
    try:
        ast.parse(text)
    except SyntaxError:
        return False
    return True


# ======================================================================================
# 一、语法上恰好是 Python 的裸文本（杀 P1）
# ======================================================================================


class TestBareTextThatHappensToParse:
    """中日韩文字是合法的标识符首字符，于是**不带标点的短句**能原样通过 `ast.parse`

    `tests/test_agent_response_parsing.py::PROSE_CASES` 里每一条散文都含 `，` `。` `'`
    这类字符，因而全部在语法层就被挡下了 —— 那组用例证明的是「编译不过的文本不会被执行」，
    而不是「散文不会被执行」。真正危险的是编译**得过**的散文：`抱歉我无法协助` 是一个合法的
    Python 表达式语句（一个裸名字），旧判据会把它当裸代码送进容器，回来一条 `NameError`。
    """

    # (用例名, 模型回复)
    BARE_TEXT_CASES: List[Tuple[str, str]] = [
        ("中文拒答-无标点", "抱歉我无法协助"),
        ("中文应答-单词", "好的"),
        ("中文应答-两个字", "收到"),
        ("英文拒答-单词", "Sorry"),
        ("英文应答-单词", "Understood"),
        # 裸字面量比裸名字更阴险：它在沙盒里是个**静默 no-op**，
        # 容器以 NORMAL 退出，Bot 一言不发，日志里连一条 ERROR 都没有
        ("裸字符串字面量", '"好的，我知道了"'),
        ("裸数字", "42"),
    ]

    @pytest.mark.parametrize(
        "raw",
        [pytest.param(raw, id=name) for name, raw in BARE_TEXT_CASES],
    )
    def test_sample_really_passes_the_syntax_gate(self, raw: str) -> None:
        """样本有效性自检：这些样本必须**真的**能通过 `ast.parse`

        没有这一条的话，上面那组断言可能只是「样本本来就编译不过」而全绿，
        `_is_meaningless_single_expression` 被整个删掉也照样过。
        """
        assert _parses_as_python(raw), f"{raw!r} 在语法层就被挡下了，这条样本证明不了闸门有用"

    @pytest.mark.parametrize(
        "raw",
        [pytest.param(raw, id=name) for name, raw in BARE_TEXT_CASES],
    )
    def test_bare_text_is_not_executable_code(self, raw: str) -> None:
        parsed = parse_chat_response(raw)

        # 两条缺一不可：只断 `code_missing` 的话，字段默认值 False 说明不了散文没被当代码；
        # 只断 `code_content == ""` 的话，调用方据以跳过沙盒的那个字段没有被验证
        assert parsed.code_content == "", f"{raw!r} 被当成代码送进沙盒了"
        assert parsed.code_missing is True

    # 反向对照：单条表达式语句**不是**天生就该被否掉，一次真正的方法调用照旧要放行
    SINGLE_CALL_CASES: List[Tuple[str, str]] = [
        ("单条方法调用", 'send_msg_text(_ck, "在的，前辈")'),
        ("单条带关键字参数的调用", 'set_note("待办", "查天气", duration=3600)'),
        ("单条赋值", 'msg = "早上好"'),
        ("裸属性访问后再调用", 'ctx.send(_ck, "hi")'),
    ]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [pytest.param(raw, raw, id=name) for name, raw in SINGLE_CALL_CASES],
    )
    def test_single_statement_code_is_still_accepted(self, raw: str, expected: str) -> None:
        """判据被吹宽成「单条语句一律不算代码」的话，这一组会红"""
        parsed = parse_chat_response(raw)

        assert parsed.code_content == expected
        assert parsed.code_missing is False
        assert _compiles(parsed.code_content)


# ======================================================================================
# 二、围栏里装的不是代码（杀 P2 / P3）
# ======================================================================================


class TestFencedContentThatIsNotCode:
    """「不把散文当代码」这道闸门必须在**有围栏**时也成立

    本轮新增的 `CODE_MISSING_FEEDBACK` 明确要求模型「把拒绝也写进代码块里」，
    等于把「围栏里装着一段中文」这个形态的出现概率主动推高了。
    """

    def test_prose_wrapped_in_a_fence_is_not_executed(self) -> None:
        """模型照着反馈把拒答塞进了代码块 —— 编译不过，一次都不该进容器"""
        parsed = parse_chat_response("```\n抱歉，我不能帮你做这个。\n```")

        assert parsed.code_content == ""
        assert parsed.code_missing is True

    def test_prose_wrapped_in_a_python_fence_is_not_executed(self) -> None:
        parsed = parse_chat_response("```python\n抱歉，我不能帮你做这个。\n```")

        assert parsed.code_content == ""
        assert parsed.code_missing is True

    def test_a_stray_fence_inside_prose_does_not_turn_prose_into_code(self) -> None:
        """散文里恰好出现一个孤立的 ```：逐块正则的 `\\Z` 分支会把它后面的半句话当成一个块"""
        raw = "我不能帮你写 ``` 这种东西，换个话题吧"
        parsed = parse_chat_response(raw)

        assert parsed.code_content == ""
        assert parsed.code_missing is True
        assert "换个话题吧" not in parsed.code_content

    def test_a_longer_prose_block_never_beats_the_real_code_block(self) -> None:
        """模型在代码块之后再开一个块写给自己看的说明，而说明比代码长

        「取最长块」是既有语义，但单凭长度会挑中说明块 —— B1（说明文字被当代码执行）
        换个壳又回来了。判据必须先看**编译得过**，再比长度。
        """
        raw = (
            "```python\n"
            'send_msg_text(_ck, "查到啦")\n'
            "```\n"
            "检查一遍：\n"
            "```text\n"
            "1. 已经用 _ck 作为会话标识\n"
            "2. 没有多余的顶层输出\n"
            "3. 也没有引入任何插件模块，符合规范要求\n"
            "```"
        )
        parsed = parse_chat_response(raw)

        assert parsed.code_content == 'send_msg_text(_ck, "查到啦")'
        assert parsed.code_missing is False
        assert "检查一遍" not in parsed.code_content
        assert "会话标识" not in parsed.code_content
        assert _compiles(parsed.code_content)

    def test_leading_blank_left_by_the_plugins_import_stripper_is_removed(self) -> None:
        """`fix_code_content` 剃掉 `from plugins ...` 之后可能留下一段**前导空白**

        剩下的是 `"   \\nsend_msg_text(...)"`，直接送进沙盒是一条 `IndentationError`
        （与本轮在修的 SyntaxError 同一类：容器白启动一次、错误日志多一条）。
        收尾那个 `.strip()` 就是干这个的，但补之前把它删掉四份用例 235 passed 全绿。
        """
        raw = '```python\nfrom plugins.weather import query\n   \nsend_msg_text(_ck, "查到啦")\n```'
        parsed = parse_chat_response(raw)

        assert parsed.code_content == 'send_msg_text(_ck, "查到啦")'
        assert not parsed.code_content.startswith((" ", "\t", "\n"))
        assert parsed.code_missing is False
        assert _compiles(parsed.code_content)

    def test_the_longest_block_still_wins_among_compilable_blocks(self) -> None:
        """反向对照：几个块都编译得过时，「取最长」这条既有语义不变"""
        raw = (
            "```python\nx = 1\n```\n"
            "再来：\n"
            '```python\nsend_msg_text(_ck, "第一句")\nsend_msg_text(_ck, "第二句")\n```'
        )
        parsed = parse_chat_response(raw)

        assert parsed.code_content == 'send_msg_text(_ck, "第一句")\nsend_msg_text(_ck, "第二句")'
        assert parsed.code_missing is False


# ======================================================================================
# 三、代码正文里本身出现 ```（杀 P4 / P5）
# ======================================================================================


class TestCodeContainingAnInnerFence:
    """逐块匹配会在**代码字符串里的** ``` 处提前截断，把半截语句当代码

    这是逐块正则相对旧实现的**回归面**：旧的「整段跨度」正则一路吃到最后一个围栏，
    对这类输入恰好是对的。所以回退候选必须留着，判据是「逐块结果编译不过、而整段跨度编译得过」。
    """

    def test_inner_fence_inside_a_string_is_preserved(self) -> None:
        raw = '```python\nsend_msg_text(_ck, "反引号是这样的：```")\n```'
        parsed = parse_chat_response(raw)

        assert parsed.code_content == 'send_msg_text(_ck, "反引号是这样的：```")'
        assert parsed.code_missing is False
        # 截断的话得到的是 `send_msg_text(_ck, "反引号是这样的：`，编译不过
        assert _compiles(parsed.code_content)

    def test_fallback_candidate_comes_from_after_the_think_block(self) -> None:
        """`<think>` 里模型常常先草拟一段带围栏的代码

        回退候选必须与主候选取自**同一段**文本（思维链之后），否则整段跨度会从思维链里的
        那个围栏开始，把草稿和思考文字一起吃进来。
        """
        raw = (
            "<think>\n"
            "我先草拟一下：\n"
            "```python\n"
            "draft = 1\n"
            "```\n"
            "这样应该可以。\n"
            "</think>\n"
            '```python\nsend_msg_text(_ck, "反引号是这样的：```")\n```'
        )
        parsed = parse_chat_response(raw)

        assert parsed.code_content == 'send_msg_text(_ck, "反引号是这样的：```")'
        assert parsed.code_missing is False
        assert "draft" not in parsed.code_content, "回退候选把思维链里的草稿吃进来了"
        assert "这样应该可以" not in parsed.code_content
        assert parsed.thought_chain.startswith("我先草拟一下：")


# ======================================================================================
# 四、错误头部取的是**最外层**那条异常（杀 N6）
# ======================================================================================


class TestErrorHeadPicksTheOutermostException:
    """链式异常里有两条形如 `X: y` 的行，扫描方向决定取到哪一条

    `test_agent_round_control.py::TestExtractErrorHead::test_traceback_tail_is_the_exception_line`
    的名字声称「最后那条才算」，但它的样本里只有一条候选行 —— 把 `reversed(lines)` 改成
    `lines`（从前往后扫）时那条用例照样绿。链式异常是线上常态：插件里 `except ... raise`
    出来的错误，第一条是**内层原因**，最后一条才是这一轮真正失败的那个。
    """

    CHAINED_TRACEBACK = (
        "Traceback (most recent call last):\n"
        '  File "/app/run_script.py", line 3, in <module>\n'
        "    payload = json.loads(raw)\n"
        "ValueError: Expecting value: line 1 column 1 (char 0)\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "\n"
        "Traceback (most recent call last):\n"
        '  File "/app/run_script.py", line 7, in <module>\n'
        "    send_msg_text(_ck, summary)\n"
        f"Exception: {SEND_TEXT_FAILURE}"
    )

    def test_head_is_the_last_exception_line_not_the_first(self) -> None:
        head = extract_error_head(self.CHAINED_TRACEBACK)

        assert head == f"Exception: {SEND_TEXT_FAILURE}"
        # 样本自检：内层那条确实也长得像一条异常尾行，方向写反就会取到它
        assert "ValueError: Expecting value" in self.CHAINED_TRACEBACK
        assert not head.startswith("ValueError")

    def test_inner_cause_alone_is_a_different_signature(self) -> None:
        """反向对照：真的只有内层那条错时，签名与上面那条不同 —— 证明取哪条是有后果的"""
        inner_only = (
            "Traceback (most recent call last):\n"
            '  File "/app/run_script.py", line 3, in <module>\n'
            "    payload = json.loads(raw)\n"
            "ValueError: Expecting value: line 1 column 1 (char 0)"
        )
        assert normalize_error_signature(ExecStopType.ERROR, inner_only) != normalize_error_signature(
            ExecStopType.ERROR,
            self.CHAINED_TRACEBACK,
        )


# ======================================================================================
# 五、无进展早停：日志与被跟踪的收尾类型（杀 M24 / P7 / P8）
# ======================================================================================


def _same_error(stop_type: ExecStopType, *, line: int) -> _SandboxResult:
    """同一个错误、不同的行号（模型每轮重写代码，行号必然变）"""
    return _SandboxResult(
        output=make_traceback(line=line, call="send_msg_text()", message=SEND_TEXT_FAILURE),
        stop_type=stop_type,
    )


class TestStallEarlyExitCoverageGaps:
    def test_stall_log_states_the_repeat_count_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """日志里要**明写**连续重复了几次

        `max_retry=5` 是刻意选的：此时「剩余未用次数」是 3、「重复次数」是 2，两个数字不同。
        兄弟用例用的是 `max_retry=4`，两个数字恰好都是 2，于是它那条
        `any("2" in text and "提前放弃" in text)` 在「日志不再写重复次数」的变异下**照样绿**
        （实测：改动后 202 passed）。
        """
        harness = _Harness(
            max_retry=5,
            parses=[_Parsed(), _Parsed()],
            sandbox_results=[_same_error(ExecStopType.ERROR, line=14), _same_error(ExecStopType.ERROR, line=27)],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError) as excinfo:
            asyncio.run(harness.run())

        assert excinfo.value.repeats == 2
        errors = harness.logger.texts("ERROR")
        assert any(f"连续重复 {excinfo.value.repeats} 次" in text for text in errors), (
            f"ERROR 日志里没有写明重复次数，排查时无法区分「提前放弃」与「本来就只跑了两轮」: {errors}"
        )
        # 「剩余几次没用」也要在，且它与重复次数是两个不同的数字
        assert any(f"剩余 {harness.max_retry - 2} 次" in text for text in errors), errors

    # 被跟踪的收尾类型逐个跑一遍真循环。只钉 `STALL_TRACKED_STOP_TYPES` 的成员是不够的：
    # 那样只是把常量抄一遍，循环里没用它也照样绿。
    TRACKED_CASES: List[ExecStopType] = [
        ExecStopType.ERROR,
        ExecStopType.TIMEOUT,
        ExecStopType.SECURITY,
    ]

    @pytest.mark.parametrize(
        "stop_type",
        [pytest.param(stop_type, id=stop_type.name) for stop_type in TRACKED_CASES],
    )
    def test_every_tracked_stop_type_can_trigger_the_early_exit(
        self,
        stop_type: ExecStopType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """确定性故障不止 ERROR 一种：插件每轮都卡到超时（TIMEOUT）同样是「重试没用」

        补之前只有 ERROR 被真的驱动过，把 TIMEOUT / MANUAL / SECURITY 从
        `STALL_TRACKED_STOP_TYPES` 里删掉，四份用例 202 passed 全绿。
        """
        harness = _Harness(
            max_retry=5,
            parses=[_Parsed(), _Parsed()],
            sandbox_results=[_same_error(stop_type, line=14), _same_error(stop_type, line=27)],
        )
        harness.install(monkeypatch)

        with pytest.raises(AgentRoundStalledError) as excinfo:
            asyncio.run(harness.run())

        # 「抛了异常」不算断言 —— 烧满迭代也会抛。契约是**没有跑满**
        assert harness.sandbox_calls == 2
        assert harness.sandbox_calls < harness.max_retry
        assert harness.llm_calls == 2
        assert excinfo.value.signature.startswith(f"{stop_type.name}|")

    def test_manual_stop_keeps_iterating_because_it_is_a_debugging_affordance(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`MANUAL`（`exit(9)`）是运行时契约白纸黑字给模型的调试手段，不是故障

        `templates/j2/runtime_contract.j2` 让模型「print 一下再 exit(9)」，它本就指望下一轮
        拿着回喂的输出继续写。调试输出的最后一行两轮相同太常见（固定分隔线、同一个对象的
        repr），把它计入无进展就会把一次正常的「先看看再动手」在第二轮掐断。

        这条是上面那组的**反向对照**：判据不能被吹宽成「只要不是 NORMAL 就算失败」。
        """
        harness = _Harness(
            max_retry=3,
            parses=[_Parsed(), _Parsed(), _Parsed(), _Parsed()],
            sandbox_results=[
                _SandboxResult(output="=== 调试 ===\nstate = {}", stop_type=ExecStopType.MANUAL),
                _SandboxResult(output="=== 调试 ===\nstate = {}", stop_type=ExecStopType.MANUAL),
                _SandboxResult(output="done", stop_type=ExecStopType.NORMAL),
            ],
        )
        harness.install(monkeypatch)

        asyncio.run(harness.run())

        # 连着两次逐字相同的 MANUAL 输出之后，第三轮照样跑到了并正常收尾
        assert harness.sandbox_calls == 3
        assert harness.phases()[-1] == "completed"

    def test_tracked_set_excludes_the_continuation_stop_types(self) -> None:
        """成员关系的负向锚点：续跑点被算进来的话，正常对话会被拦腰砍断

        （正向由上面那组真循环用例覆盖，这里只补几条**不该**在集合里的。）
        """
        assert ExecStopType.AGENT not in STALL_TRACKED_STOP_TYPES
        assert ExecStopType.MULTIMODAL_AGENT not in STALL_TRACKED_STOP_TYPES
        assert ExecStopType.NORMAL not in STALL_TRACKED_STOP_TYPES
        assert ExecStopType.MANUAL not in STALL_TRACKED_STOP_TYPES
