"""`services/agent/resolver.py::parse_chat_response` 的围栏提取与「没有代码」判定。

## 这组用例守的是什么

线上（测试部署 `exec_code` 表 5388 行、`prompts_error/` 733 个错误文件）暴露出三个缺陷，
根因都在旧正则 ```` ```(?:python)?\\s*(.*?)```(?:\\s*$|(?=\\s*```\\s*$)) ```` 把一个代码块的
**终止条件锚在了整段响应的结尾**（`$`）上：

* **B1 围栏泄漏**：响应里两个代码块中间夹了说明文字时，非贪婪会一路吃到最后一个围栏，
  于是 ``` 和中文说明原样进入 `code_content` 被当代码执行。
  真实样本 `exec_code` id=4372（`SyntaxError: invalid character '：' (U+FF1A)`）、id=583、
  id=531（``File "/app/run_script.py", line 9  ```  SyntaxError: invalid syntax``）。
* **B2 尾随文字**：闭合围栏后还有一句「以上就是我的回复」时整个正则**不匹配**，
  于是掉进散文兜底，把**含围栏的整段**当代码执行。
* **B3 散文兜底**：没有任何代码块时旧实现无条件 `code_content = 整段自然语言`，
  模型的拒答话术就这样被送进沙盒当 Python 执行。

线上 7 天内 15 次 `SyntaxError` 全部由 B1/B2 造成。

## 断言口径

每条断言都要求「修复不存在时会红」：
* B1/B2 断言的是**精确的 code_content 字符串**外加 `compile()` 能过、``` 与全角冒号不在其中
  —— 旧实现在这些样本上产出的正是含 ``` 的串（见 `test_online_sample_really_triggers_the_old_anchor_bug`，
  它用历史正则原样跑一遍，证明样本确实踩的是这个坑，而不是一条恰好也能过的用例）;
* B3 断言 `code_content == ""` **且** `code_missing is True` —— 只断言后者的话，
  旧实现「散文进 code_content 但 code_missing 字段压根不存在」也会因为默认值而蒙混过关；
* 裸代码、`<think>` 两组是**防回归**断言：它们在修复前后都应为绿，用来钉住
  「不带围栏直接输出代码」这个既有可用形态没有被 B3 的修复误伤。
"""

from __future__ import annotations

import re
from typing import List, Tuple

import pytest

from nekro_agent.services.agent.resolver import ParsedCodeRunData, parse_chat_response

# --------------------------------------------------------------------------------------
# 线上真实样本（exec_code id=4372 的形态）：正文代码 + 闭合围栏 + 「检查语法与规则：」+ 第二个块
# --------------------------------------------------------------------------------------

ONLINE_SAMPLE_4372 = (
    "```python\n"
    'send_msg_text(_ck, "[@id:2000000001@] 好的，我这就去看看~")\n'
    'set_note("待办", "帮前辈查天气", duration=3600)\n'
    "```\n"
    "检查语法与规则：\n"
    "1. 使用了 _ck 作为会话标识\n"
    "2. 没有多余的顶层输出\n"
    "```python\n"
    "# 无需其他补充操作\n"
    "```"
)

ONLINE_SAMPLE_4372_EXPECTED_CODE = 'send_msg_text(_ck, "[@id:2000000001@] 好的，我这就去看看~")\nset_note("待办", "帮前辈查天气", duration=3600)'

# 历史实现的正则，原样保留在测试里，只用于证明上面的样本确实是 B1 的触发形态
LEGACY_CODE_BLOCK_PATTERN = re.compile(r"```(?:python)?\s*(.*?)```(?:\s*$|(?=\s*```\s*$))", re.DOTALL)


def _compiles(code: str) -> bool:
    try:
        compile(code, "<sandbox>", "exec")
    except SyntaxError:
        return False
    return True


# --------------------------------------------------------------------------------------
# 一、围栏块提取：精确的输入 → 精确的 code_content / thought_chain
# --------------------------------------------------------------------------------------

# (用例名, 原始响应, 期望 code_content, 期望 thought_chain)
FENCE_CASES: List[Tuple[str, str, str, str]] = [
    (
        "单围栏-带python标记",
        '```python\nsend_msg_text(_ck, "在的，前辈")\n```',
        'send_msg_text(_ck, "在的，前辈")',
        "",
    ),
    (
        "单围栏-无语言标记",
        '```\nsend_msg_text(_ck, "在的，前辈")\n```',
        'send_msg_text(_ck, "在的，前辈")',
        "",
    ),
    (
        # B1：两个块中间夹说明文字。修复前 code_content 会是
        # 'x = 1\n```\n下面是真正要执行的：\n```python\nsend_msg_text(...)'
        "B1-多围栏夹说明文字-取最长块",
        "```python\nx = 1\n```\n"
        "下面是真正要执行的：\n"
        '```python\nsend_msg_text(_ck, "第一句")\nsend_msg_text(_ck, "第二句")\n```',
        'send_msg_text(_ck, "第一句")\nsend_msg_text(_ck, "第二句")',
        "",
    ),
    (
        # B1 的另一种排布：最长块在前，说明文字与短块在后（线上 id=4372 的形态）
        "B1-最长块在前-说明与短块在后",
        ONLINE_SAMPLE_4372,
        ONLINE_SAMPLE_4372_EXPECTED_CODE,
        "",
    ),
    (
        # B2：闭合围栏后有尾随文字。修复前整个正则不匹配 → 散文兜底 →
        # code_content = 'send_msg_text(_ck, "好的")\n```\n以上就是我的回复，前辈还有别的吩咐吗？'
        "B2-闭合围栏后有中文尾随文字",
        '```python\nsend_msg_text(_ck, "好的")\n```\n以上就是我的回复，前辈还有别的吩咐吗？',
        'send_msg_text(_ck, "好的")',
        "",
    ),
    (
        "B2-闭合围栏后有英文尾随文字",
        '```python\nsend_msg_text(_ck, "ok")\n```\nThat is my reply, let me know if anything else.',
        'send_msg_text(_ck, "ok")',
        "",
    ),
    (
        # B1 + B2 同时出现：多个块 + 最后一个块后面还有尾随文字
        "B1B2-多围栏且末尾还有尾随文字",
        "```python\nx = 1\n```\n"
        "说明：\n"
        '```python\nsend_msg_text(_ck, "这是真正要发的消息")\n```\n'
        "以上。",
        'send_msg_text(_ck, "这是真正要发的消息")',
        "",
    ),
    (
        "think-单围栏（既有形态，防回归）",
        '<think>\n先看看上下文，前辈叫了我两次。\n</think>\n\n```python\nsend_msg_text(_ck, "在的")\n```',
        'send_msg_text(_ck, "在的")',
        "先看看上下文，前辈叫了我两次。",
    ),
    (
        # think + B1：思维链之后仍有两个块夹说明文字
        "think-多围栏夹说明文字",
        "<think>\n想清楚再动手。\n</think>\n"
        "```python\nx = 1\n```\n"
        "检查一遍：\n"
        '```python\nsend_msg_text(_ck, "已经处理好啦")\n```',
        'send_msg_text(_ck, "已经处理好啦")',
        "想清楚再动手。",
    ),
    (
        # think + B2：闭合围栏后有尾随文字
        "think-闭合围栏后有尾随文字",
        '<think>\n简单回一句。\n</think>\n```python\nsend_msg_text(_ck, "收到")\n```\n就这样。',
        'send_msg_text(_ck, "收到")',
        "简单回一句。",
    ),
    (
        # 围栏后跟一个孤立的 ```（奇数个围栏），不能把说明文字带进来
        "孤立尾随围栏",
        '```python\nsend_msg_text(_ck, "好")\n```\n补充说明\n```',
        'send_msg_text(_ck, "好")',
        "",
    ),
    (
        # 响应被截断、开围栏没有闭合：与旧实现一致地取到串尾，不判成「没有代码」
        "未闭合围栏-截断响应（既有形态，防回归）",
        '```python\nsend_msg_text(_ck, "写到一半就断了")',
        'send_msg_text(_ck, "写到一半就断了")',
        "",
    ),
    (
        # fix_code_content 的既有行为不受影响
        "围栏内含plugins导入行-仍被剔除（防回归）",
        '```python\nfrom plugins.weather import query\nsend_msg_text(_ck, "查到啦")\n```',
        'send_msg_text(_ck, "查到啦")',
        "",
    ),
]


@pytest.mark.parametrize(
    ("raw", "expected_code", "expected_think"),
    [pytest.param(raw, code, think, id=name) for name, raw, code, think in FENCE_CASES],
)
def test_fenced_block_extraction(raw: str, expected_code: str, expected_think: str) -> None:
    parsed = parse_chat_response(raw)

    assert parsed.code_content == expected_code
    assert parsed.thought_chain == expected_think
    assert parsed.code_missing is False
    assert parsed.raw_content == raw


@pytest.mark.parametrize(
    "raw",
    [pytest.param(raw, id=name) for name, raw, _code, _think in FENCE_CASES],
)
def test_extracted_code_never_carries_fences_or_prose(raw: str) -> None:
    """围栏与说明文字**一个字都不能**进 code_content —— 线上 15 次 SyntaxError 的直接来源"""
    parsed = parse_chat_response(raw)

    assert "```" not in parsed.code_content
    assert "<think>" not in parsed.code_content
    assert _compiles(parsed.code_content), f"提取出的内容不是合法 Python: {parsed.code_content!r}"


class TestOnlineSample4372:
    """线上 `exec_code` id=4372：`SyntaxError: invalid character '：' (U+FF1A)`"""

    def test_extracted_code_compiles_and_drops_the_prose(self) -> None:
        parsed = parse_chat_response(ONLINE_SAMPLE_4372)

        assert parsed.code_content == ONLINE_SAMPLE_4372_EXPECTED_CODE
        assert parsed.code_missing is False
        # 沙盒里报错的那个全角冒号来自「检查语法与规则：」，它属于说明文字而不是代码
        assert "：" not in parsed.code_content
        assert "检查语法与规则" not in parsed.code_content
        assert "```" not in parsed.code_content
        assert _compiles(parsed.code_content)

    def test_online_sample_really_triggers_the_old_anchor_bug(self) -> None:
        """样本有效性自检：用历史正则跑一遍，确认它确实会泄漏围栏并编译失败。

        没有这一条的话，上面那组断言可能只是「样本本来就不难」而全绿，
        修复被整个删掉也照样过。
        """
        match = LEGACY_CODE_BLOCK_PATTERN.search(ONLINE_SAMPLE_4372)
        assert match is not None
        legacy_code = match.group(1).strip()

        assert "```" in legacy_code
        assert "：" in legacy_code
        assert not _compiles(legacy_code)


# --------------------------------------------------------------------------------------
# 二、B3：没有围栏且整段不是可编译的 Python 时，绝不把自然语言当代码
# --------------------------------------------------------------------------------------

# (用例名, 原始响应, 期望 thought_chain, 一个字都不许出现在 code_content 里的文本)
PROSE_CASES: List[Tuple[str, str, str, Tuple[str, ...]]] = [
    (
        "中文拒答-单句",
        "抱歉，我无法协助这个请求。",
        "",
        ("抱歉", "我无法协助这个请求。"),
    ),
    (
        "中文拒答-多句带换行",
        "很抱歉，我不能生成这类内容。\n如果你有其他问题，我很乐意帮忙。",
        "",
        ("很抱歉", "我不能生成这类内容。", "如果你有其他问题，我很乐意帮忙。"),
    ),
    (
        "中文拒答-作为AI助手话术",
        "对不起，作为一个 AI 助手，我不能参与这样的话题。",
        "",
        ("对不起", "作为一个 AI 助手", "我不能参与这样的话题。"),
    ),
    (
        "英文拒答-带缩写",
        "I'm sorry, but I can't help with that request.",
        "",
        ("I'm sorry", "I can't help with that request."),
    ),
    (
        "英文拒答-多句",
        "Sorry, I am unable to assist with this.\nPlease let me know if there is anything else.",
        "",
        ("Sorry", "I am unable to assist with this.", "Please let me know if there is anything else."),
    ),
    (
        "纯说明文字-没有任何代码",
        "我已经把消息发出去了：如果还需要别的，随时叫我。",
        "",
        ("我已经把消息发出去了", "随时叫我。"),
    ),
    (
        "think之后是拒答话术",
        "<think>\n这个请求涉及不该做的事，我应当拒绝。\n</think>\n抱歉，这个我没办法帮你。",
        "这个请求涉及不该做的事，我应当拒绝。",
        ("抱歉", "这个我没办法帮你。", "我应当拒绝", "<think>"),
    ),
    (
        "markdown说明而非代码",
        "接下来我打算这么做：\n- 先查天气\n- 再回复前辈",
        "",
        ("接下来我打算这么做", "- 先查天气", "- 再回复前辈"),
    ),
    (
        # 没有围栏、又不是合法 Python 的截断输出：同样不进沙盒
        "无围栏的截断代码",
        'send_msg_text(_ck, "写到一半就断',
        "",
        ('send_msg_text(_ck, "写到一半就断',),
    ),
    (
        # 空代码块：提取得到空串，等价于「没有可执行代码」。
        # 它本来就没有任何散文，泄漏检查对它无话可说，所以只进下面的精确相等那一组。
        "空代码块",
        "```python\n\n```",
        "",
        (),
    ),
]


@pytest.mark.parametrize(
    ("raw", "expected_think"),
    [pytest.param(raw, think, id=name) for name, raw, think, _frags in PROSE_CASES],
)
def test_prose_is_never_executed_as_code(raw: str, expected_think: str) -> None:
    """修复前这些输入会让整段自然语言原样进 code_content 并被送进沙盒执行"""
    parsed = parse_chat_response(raw)

    # 两条缺一不可：只断言 code_missing 的话，字段默认值 False 也说明不了散文没被当代码
    assert parsed.code_content == ""
    assert parsed.code_missing is True
    # 思维链的既有语义不受影响
    assert parsed.thought_chain == expected_think


@pytest.mark.parametrize(
    ("raw", "fragments"),
    [pytest.param(raw, frags, id=name) for name, raw, _think, frags in PROSE_CASES if frags],
)
def test_no_fragment_of_the_prose_leaks_into_code(raw: str, fragments: Tuple[str, ...]) -> None:
    """逐句核对：拒答话术里被点名的每一句都不应出现在 code_content 中"""
    parsed = parse_chat_response(raw)

    assert fragments, "这条用例必须真的点名了要检查的文本，否则断言是空的"
    for fragment in fragments:
        assert fragment in raw, f"样本自检失败：{fragment!r} 并不在原始响应里"
        assert fragment not in parsed.code_content


# --------------------------------------------------------------------------------------
# 三、防回归：模型不带围栏直接输出裸代码，是既有的、能正常工作的形态
# --------------------------------------------------------------------------------------

# (用例名, 原始响应, 期望 code_content, 期望 thought_chain)
BARE_CODE_CASES: List[Tuple[str, str, str, str]] = [
    (
        "裸代码-单行调用",
        'send_msg_text(_ck, "在的，前辈")',
        'send_msg_text(_ck, "在的，前辈")',
        "",
    ),
    (
        "裸代码-多行",
        'msg = "早上好呀"\nsend_msg_text(_ck, msg)',
        'msg = "早上好呀"\nsend_msg_text(_ck, msg)',
        "",
    ),
    (
        "裸代码-带注释与空行",
        '# 先打个招呼\n\nsend_msg_text(_ck, "嗨")\n',
        '# 先打个招呼\n\nsend_msg_text(_ck, "嗨")',
        "",
    ),
    (
        "裸代码-带控制流",
        'for name in ["A", "B"]:\n    send_msg_text(_ck, f"你好 {name}")',
        'for name in ["A", "B"]:\n    send_msg_text(_ck, f"你好 {name}")',
        "",
    ),
    (
        "think之后是裸代码",
        '<think>\n直接回一句就好。\n</think>\nsend_msg_text(_ck, "收到啦")',
        'send_msg_text(_ck, "收到啦")',
        "直接回一句就好。",
    ),
    (
        "裸代码-含plugins导入行仍被剔除",
        'from plugins.weather import query\nsend_msg_text(_ck, "查到啦")',
        'send_msg_text(_ck, "查到啦")',
        "",
    ),
]


@pytest.mark.parametrize(
    ("raw", "expected_code", "expected_think"),
    [pytest.param(raw, code, think, id=name) for name, raw, code, think in BARE_CODE_CASES],
)
def test_bare_code_without_fences_is_still_accepted(raw: str, expected_code: str, expected_think: str) -> None:
    parsed = parse_chat_response(raw)

    assert parsed.code_content == expected_code
    assert parsed.code_missing is False
    assert parsed.thought_chain == expected_think
    assert _compiles(parsed.code_content)


# --------------------------------------------------------------------------------------
# 四、模型字段本身
# --------------------------------------------------------------------------------------


def test_code_missing_defaults_to_false_for_direct_construction() -> None:
    """`services/command/built_in/debug.py` 直接构造这个模型执行用户手写的代码，
    不传 `code_missing` 时必须仍然是「有代码」，否则调试指令会被当成空响应跳过沙盒。"""
    data = ParsedCodeRunData(raw_content="send_msg_text(_ck, 'hi')", code_content="send_msg_text(_ck, 'hi')", thought_chain="")

    assert data.code_missing is False
