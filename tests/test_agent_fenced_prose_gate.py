"""围栏**里面**装的不是代码时，同样不许进沙盒。

## 这组用例守的是什么（对抗式复核补的「另一半」）

本轮的设计契约是「`parse_chat_response` 不再把散文当代码」。落地时这道 `ast.parse` 闸门
**只装在「响应里没有任何围栏」那一条支路上**（`resolver.parse_chat_response` 末尾那段
`if not code_content:`）。于是只要响应里出现哪怕一个 ``` ，整段自然语言就能绕过闸门：

* 模型把拒答话术包进 ```` ```...``` ```` 里 —— 而本轮新增的 `CODE_MISSING_FEEDBACK` 恰恰在
  要求模型「实在做不到就把话写进代码块里」，等于把这条形态的概率往上推；
* 散文里恰好出现一个孤立的 ``` （`我不能帮你写 ``` 这种东西`）—— 逐块正则的 `\\Z` 分支
  会把它后面的半句话整个当成一个块；
* 两个块、其中说明文字那块更长 —— 「取最长块」按长度选中了说明文字（B1 换了个壳）。

三者的终点都一样：一段中文进容器，回来一条必然的 `SyntaxError`，白烧一轮迭代。

## 断言口径

* 拒答组断言 `code_content == ""` **且** `code_missing is True`。只断言后者不够：
  `code_missing` 有默认值 `False`，而「散文进了 code_content」时它同样是 `False`，
  两条一起才能把「散文没被当代码」钉死。
* 每条拒答样本都过 `test_sample_really_contains_a_fence`：样本里必须真的有 ``` ，
  否则它走的是既有的无围栏闸门，这组用例就变成了重复覆盖、证明不了本条修复。
* `test_prefix_selection_without_the_gate_would_have_executed_prose` 用**修复前的选块逻辑**
  （逐块取最长 + 不判可编译）原样跑一遍，断言它选出的正是编译不过的散文 ——
  没有这一条，上面的断言可能只是「样本本来就不难」。
* 防回归组断言精确的 `code_content`：真代码、顶层 `await`、纯注释块、代码正文里自带 ``` 的
  旧跨度回退，一条都不能被这道闸门误伤。
"""

from __future__ import annotations

import ast
import re
from typing import List, Tuple

import pytest

from nekro_agent.services.agent.resolver import (
    CODE_BLOCK_PATTERN,
    fix_code_content,
    parse_chat_response,
)


def _compiles(code: str) -> bool:
    try:
        ast.parse(code)
    except SyntaxError:
        return False
    return True


def _pre_fix_pick(text: str) -> str:
    """修复**之前**的选块逻辑：逐块提取，纯按长度取最长，不判可编译

    逐字对应改动前的 `_pick_longest_code_block` + 尾部的围栏剃除，
    只用来证明样本确实踩在这个坑上。
    """
    blocks = [match.group(1).strip() for match in CODE_BLOCK_PATTERN.finditer(text)]
    if not blocks:
        return ""
    picked = max(blocks, key=len)
    if picked.startswith("```python"):
        picked = picked[len("```python") :].strip()
    if picked.endswith("```"):
        picked = picked[: -len("```")].strip()
    return fix_code_content(picked).strip()


# --------------------------------------------------------------------------------------
# 一、围栏里装的是散文 —— 一律判「没能提取出代码」
# --------------------------------------------------------------------------------------

# (用例名, 原始响应, 期望 thought_chain, 一个字都不许出现在 code_content 里的片段)
FENCED_PROSE_CASES: List[Tuple[str, str, str, Tuple[str, ...]]] = [
    (
        "围栏内中文拒答-无语言标记",
        "```\n抱歉，我不能协助这个请求。\n```",
        "",
        ("抱歉", "我不能协助这个请求。"),
    ),
    (
        "围栏内中文拒答-带python标记",
        "```python\n很抱歉，我无法生成这类内容。\n```",
        "",
        ("很抱歉", "我无法生成这类内容。"),
    ),
    (
        "围栏内英文拒答",
        "```\nI'm sorry, but I can't help with that.\n```",
        "",
        ("I'm sorry", "I can't help with that."),
    ),
    (
        "围栏前后都有散文",
        "我看了一下。\n```\n对不起，这个我做不到。\n```\n就这样。",
        "",
        ("我看了一下。", "对不起", "这个我做不到。", "就这样。"),
    ),
    (
        # CODE_MISSING_FEEDBACK 要求模型「把拒绝写进代码块里」，模型照做但写的是话不是代码
        "被要求写进代码块后仍然只写了话",
        "```python\n我不能执行这个操作，请换个说法。\n```",
        "",
        ("我不能执行这个操作", "请换个说法。"),
    ),
    (
        "think 之后是围栏内拒答",
        "<think>\n这个请求不该做，应当拒绝。\n</think>\n```\n抱歉，我不能这么做。\n```",
        "这个请求不该做，应当拒绝。",
        ("抱歉", "我不能这么做。", "<think>"),
    ),
    (
        # 逐块正则的 \Z 分支：孤立围栏之后的半句话被整个当成一个块
        "散文中间夹一个孤立围栏",
        "我不能帮你写 ``` 这种东西，请换个说法。",
        "",
        ("这种东西，请换个说法。",),
    ),
    (
        "孤立围栏在开头",
        "``` 抱歉，我不能这么做。",
        "",
        ("抱歉", "我不能这么做。"),
    ),
    (
        "开围栏未闭合、正文是拒答",
        "```python\n抱歉，我不能执行这个操作。",
        "",
        ("抱歉", "我不能执行这个操作。"),
    ),
    (
        # 「取最长块」的坑：说明文字那块比代码块长，纯比长度会选中它
        "两个块-说明文字块更长",
        "```\n这里是一段特别特别长的中文说明文字，专门用来把它撑成最长的那个块。\n```\n```\n另一段同样不是代码的中文补充说明，也很长很长很长很长很长。\n```",
        "",
        ("这里是一段特别特别长的中文说明文字", "另一段同样不是代码的中文补充说明"),
    ),
]


@pytest.mark.parametrize(
    ("raw", "expected_think"),
    [pytest.param(raw, think, id=name) for name, raw, think, _frags in FENCED_PROSE_CASES],
)
def test_fenced_prose_is_never_executed_as_code(raw: str, expected_think: str) -> None:
    """修复前这些输入会让围栏里的自然语言原样进 code_content 并被送进沙盒执行"""
    parsed = parse_chat_response(raw)

    # 两条缺一不可：只断言 code_missing 的话，「散文进了 code_content」时它同样是 False
    assert parsed.code_content == ""
    assert parsed.code_missing is True
    # 思维链的既有语义不受这道闸门影响
    assert parsed.thought_chain == expected_think


@pytest.mark.parametrize(
    "raw",
    [pytest.param(raw, id=name) for name, raw, _think, _frags in FENCED_PROSE_CASES],
)
def test_sample_really_contains_a_fence(raw: str) -> None:
    """样本有效性自检：必须真的带围栏

    不带围栏的样本走的是既有的「无围栏 + ast.parse」闸门，
    放在这一组里等于自证成功，本条修复被整个删掉也照样绿。
    """
    assert "```" in raw


@pytest.mark.parametrize(
    ("raw", "fragments"),
    [pytest.param(raw, frags, id=name) for name, raw, _think, frags in FENCED_PROSE_CASES],
)
def test_no_fragment_of_the_fenced_prose_leaks_into_code(raw: str, fragments: Tuple[str, ...]) -> None:
    parsed = parse_chat_response(raw)

    assert fragments, "这条用例必须真的点名了要检查的文本，否则断言是空的"
    for fragment in fragments:
        assert fragment in raw, f"样本自检失败：{fragment!r} 并不在原始响应里"
        assert fragment not in parsed.code_content


@pytest.mark.parametrize(
    "raw",
    [pytest.param(raw, id=name) for name, raw, _think, _frags in FENCED_PROSE_CASES],
)
def test_prefix_selection_without_the_gate_would_have_executed_prose(raw: str) -> None:
    """样本有效性自检：用修复前的选块逻辑跑一遍，确认它确实会选出编译不过的散文"""
    legacy_pick = _pre_fix_pick(raw)

    assert legacy_pick, f"修复前的逻辑什么都没选出来，这条样本证明不了问题: {raw!r}"
    assert not _compiles(legacy_pick), f"修复前选出的内容居然能编译，样本无效: {legacy_pick!r}"


# --------------------------------------------------------------------------------------
# 二、防回归：这道闸门不能误伤任何一种真的在写代码的形态
# --------------------------------------------------------------------------------------

# (用例名, 原始响应, 期望 code_content, 期望 thought_chain)
STILL_ACCEPTED_CASES: List[Tuple[str, str, str, str]] = [
    (
        "普通代码块",
        '```python\nsend_msg_text(_ck, "在的，前辈")\n```',
        'send_msg_text(_ck, "在的，前辈")',
        "",
    ),
    (
        # `ast.parse` 放行顶层 await，`compile(..., "exec")` 不放行。闸门必须用前者，
        # 否则模型写顶层 await 的整类回复会被误判成「没有代码」
        "顶层 await",
        "```python\nawait send_msg_text(_ck, 'hi')\n```",
        "await send_msg_text(_ck, 'hi')",
        "",
    ),
    (
        # 线上真实的成功样本形态：模型判断无事可做，只回一行注释
        "纯注释块",
        "```python\n# 无需其他补充操作\n```",
        "# 无需其他补充操作",
        "",
    ),
    (
        # 代码正文里自带 ``` ：逐块匹配会在内层围栏处截断，旧的整段跨度回退把它救回来。
        # 这条同时钉住「回退分支没有被这道闸门顺手削掉」
        "代码正文里含反引号围栏（走旧跨度回退）",
        '```python\nsend_msg_text(_ck, "用 ``` 包起来")\n```',
        'send_msg_text(_ck, "用 ``` 包起来")',
        "",
    ),
    (
        # 代码块 + 更长的说明块：应当选中代码块，而不是「长度更长的那个」
        "代码块与更长的说明块并存-选代码块",
        '```python\nsend_msg_text(_ck, "好")\n```\n```\n下面是一段特别特别长的中文说明，专门用来抢最长块的位置。\n```',
        'send_msg_text(_ck, "好")',
        "",
    ),
    (
        "三个块-只有中间那块是代码",
        "```\n说明一：这是前言。\n```\n```python\nsend_msg_text(_ck, 'x')\n```\n```\n说明二：这是一段更长更长更长的后记文字。\n```",
        "send_msg_text(_ck, 'x')",
        "",
    ),
    (
        "think + 代码块 + 更长的说明块",
        '<think>\n想好了。\n</think>\n```python\nsend_msg_text(_ck, "好")\n```\n```\n下面是一段特别特别长的中文说明，专门用来抢最长块的位置。\n```',
        'send_msg_text(_ck, "好")',
        "想好了。",
    ),
    (
        "B1 线上 4372 形态",
        '```python\nsend_msg_text(_ck, "好的")\nset_note("待办", "查天气", duration=3600)\n```\n'
        "检查语法与规则：\n1. 使用了 _ck\n"
        "```python\n# 无需其他补充操作\n```",
        'send_msg_text(_ck, "好的")\nset_note("待办", "查天气", duration=3600)',
        "",
    ),
    (
        "B2 闭合围栏后有尾随文字",
        '```python\nsend_msg_text(_ck, "好的")\n```\n以上就是我的回复，前辈还有别的吩咐吗？',
        'send_msg_text(_ck, "好的")',
        "",
    ),
]


@pytest.mark.parametrize(
    ("raw", "expected_code", "expected_think"),
    [pytest.param(raw, code, think, id=name) for name, raw, code, think in STILL_ACCEPTED_CASES],
)
def test_real_code_is_still_accepted(raw: str, expected_code: str, expected_think: str) -> None:
    parsed = parse_chat_response(raw)

    assert parsed.code_content == expected_code
    assert parsed.code_missing is False
    assert parsed.thought_chain == expected_think
    assert _compiles(parsed.code_content)


# --------------------------------------------------------------------------------------
# 三、跨这一整组样本的不变式
# --------------------------------------------------------------------------------------

ALL_SAMPLES: List[Tuple[str, str]] = [(name, raw) for name, raw, _t, _f in FENCED_PROSE_CASES] + [
    (name, raw) for name, raw, _c, _t in STILL_ACCEPTED_CASES
]


@pytest.mark.parametrize("raw", [pytest.param(raw, id=name) for name, raw in ALL_SAMPLES])
def test_code_missing_is_exactly_the_empty_code_predicate(raw: str) -> None:
    """`run_agent` 的沙盒闸门只读 `code_missing`，而沙盒真正执行的是 `code_content`。

    两者一旦脱钩，就会出现「判据说有代码、送进去的却是空脚本」——那条路径的表现是
    沙盒以 NORMAL 收尾、Bot 一言不发、日志里一条 ERROR 都没有。
    """
    parsed = parse_chat_response(raw)

    assert parsed.code_missing is (parsed.code_content == "")


@pytest.mark.parametrize("raw", [pytest.param(raw, id=name) for name, raw in ALL_SAMPLES])
def test_whatever_reaches_the_sandbox_always_compiles(raw: str) -> None:
    """凡是判定成「有代码」的，送进沙盒的正文必须真的能编译。

    这条是整轮修复的收口断言：容器里跑一段编译不过的文本，唯一的产物就是一条
    `SyntaxError` —— 线上 7 天 15 次 SyntaxError 正是这么来的。
    """
    parsed = parse_chat_response(raw)

    if not parsed.code_missing:
        assert _compiles(parsed.code_content), f"送进沙盒的不是合法 Python: {parsed.code_content!r}"
        # 这里曾经还有一条 `assert "```" not in code or _compiles(code)`：上一行已经断言过
        # `_compiles(...)` 为真，所以那条析取式恒成立，任何变异都杀不掉它（对抗式复核实测）。
        # 「正文里不许有 ```」这条更强的判据在这一组里**本来就不成立** ——
        # `代码正文里含反引号围栏（走旧跨度回退）` 那条样本的正文里就有一对合法的 ``` 。
        # 所以正确的做法是把恒真的那条删掉，而不是补一条会误伤的。


def test_gate_uses_ast_parse_not_compile() -> None:
    """闸门必须用 `ast.parse`：`compile(..., "exec")` 会把顶层 await 判成语法错

    这条是上面那个「顶层 await」用例的判据说明，单独钉一次，
    免得将来有人把 `ast.parse` 顺手换成 `compile` 时只看到一条用例红而不知道为什么。
    """
    source = "await send_msg_text(_ck, 'hi')"

    ast.parse(source)  # 不抛
    with pytest.raises(SyntaxError):
        compile(source, "<sandbox>", "exec")


# --------------------------------------------------------------------------------------
# 四、无围栏支路上的姊妹闸门：整段是「一个孤零零的名字/字面量」
#
# 这道守卫（`resolver._is_meaningless_single_expression`）此前**一条用例都没有**：
# 复核时把它整个删掉，全量定向用例仍然 403 passed。它挡的是 `ast.parse` 闸门的漏网之鱼——
# 中文是合法的标识符首字符，所以不带标点的短句拒答（`你好` / `抱歉` / `Sorry`）
# 在语法上就是一个裸名字，能原样通过闸门被当裸代码送进沙盒。
# --------------------------------------------------------------------------------------

# (用例名, 原始响应, 期望 code_missing, 期望 code_content)
SINGLE_EXPRESSION_CASES: List[Tuple[str, str, bool, str]] = [
    ("不带标点的中文短句拒答", "你好", True, ""),
    ("不带标点的中文拒答", "抱歉", True, ""),
    ("不带标点的英文拒答", "Sorry", True, ""),
    ("整段是一个字符串字面量", "'我不能这么做'", True, ""),
    ("整段是一个数字字面量", "123", True, ""),
    ("整段是一个内置常量名", "None", True, ""),
    # 下面两条是**必须放行**的既有成功形态，用来证明这道守卫收得够窄
    ("纯注释（线上真实成功样本）", "# 只有注释", False, "# 只有注释"),
    ("单独一个 pass", "pass", False, "pass"),
    ("正常的单行调用", 'send_msg_text(_ck, "hi")', False, 'send_msg_text(_ck, "hi")'),
]


@pytest.mark.parametrize(
    ("raw", "expected_missing", "expected_code"),
    [pytest.param(raw, missing, code, id=name) for name, raw, missing, code in SINGLE_EXPRESSION_CASES],
)
def test_bare_name_or_literal_is_not_treated_as_code(raw: str, expected_missing: bool, expected_code: str) -> None:
    """裸名字必然 `NameError`，裸字面量是个静默 no-op（沙盒以 NORMAL 收尾、Bot 一言不发）。

    两种结局都不该由「把一句中文当代码跑一遍」得到。
    """
    parsed = parse_chat_response(raw)

    assert parsed.code_missing is expected_missing
    assert parsed.code_content == expected_code


@pytest.mark.parametrize(
    "raw",
    [pytest.param(raw, id=name) for name, raw, missing, _code in SINGLE_EXPRESSION_CASES if missing],
)
def test_single_expression_samples_really_pass_the_syntax_gate(raw: str) -> None:
    """样本有效性自检：这些样本**语法上确实是合法 Python**

    否则它们会被上一道 `ast.parse` 闸门挡下，这一组就证明不了这道守卫的存在价值。
    """
    assert _compiles(raw), f"样本无效：{raw!r} 本来就过不了语法闸门"


@pytest.mark.parametrize(
    ("raw", "expected_missing"),
    [pytest.param(raw, missing, id=name) for name, raw, missing, _code in SINGLE_EXPRESSION_CASES],
)
def test_the_same_single_expression_wrapped_in_a_fence_gets_the_same_verdict(
    raw: str,
    expected_missing: bool,
) -> None:
    """把上表逐条包进围栏，结论必须与不带围栏时**逐字一致**

    这道守卫原先只装在「没有围栏」那条支路上，围栏支路只判语法。而 `'我不能这么做'`
    这类**合法的表达式语句**过得了语法闸门，于是只要模型把话包进代码块——本轮
    `CODE_MISSING_FEEDBACK` 恰恰在要求它这么做——整段中文就能进沙盒，以 NORMAL 退出：
    Bot 一言不发，日志里连一条 ERROR 都没有。

    反向的一半同样重要：`pass` / 纯注释 / 正常调用被包进围栏后**仍然要能执行**，
    否则这道守卫就从「挡住无意义的」变成了「把正常代码也挡了」。
    """
    fence = "`" * 3
    parsed = parse_chat_response(f"{fence}python\n{raw}\n{fence}")

    assert parsed.code_missing is expected_missing
    if not expected_missing:
        assert parsed.code_content.strip(), "既有的成功形态被围栏支路误挡了"


def test_pre_fix_pick_helper_matches_the_current_block_regex() -> None:
    """自检：本文件里那个「修复前的选块逻辑」用的确实是产品代码当前的逐块正则

    否则它证明的是另一个正则的行为，`test_prefix_selection_without_the_gate...` 就成了空断言。
    """
    assert isinstance(CODE_BLOCK_PATTERN, re.Pattern)
    assert _pre_fix_pick("```python\nx = 1\n```") == "x = 1"


def test_a_longer_meaningless_block_never_beats_the_real_code_block() -> None:
    """选块阶段就要把「无意义的单表达式」排除掉，否则真代码会被丢掉

    `_pick_longest_code_block` 是按长度挑的。若这一步只判语法，
    一段**更长的**中文字面量（合法的表达式语句）会胜出，随后被下游闸门拒掉，
    于是同一条响应里那个**短一些的真代码块**跟着一起没了 —— 模型明明照做了，
    Bot 却回「没能提取出代码」。三处判据必须是同一个。
    """
    fence = "`" * 3
    raw = (
        f"{fence}python\n"
        "'我不能帮你做这个，这段话故意写得比下面那行代码长很多很多很多'"
        f"\n{fence}\n\n{fence}python\nsend_msg_text(_ck, \"hi\")\n{fence}"
    )

    parsed = parse_chat_response(raw)

    assert parsed.code_missing is False
    assert parsed.code_content == 'send_msg_text(_ck, "hi")'


def test_a_legacy_span_that_is_just_a_string_literal_is_not_code() -> None:
    """旧跨度回退分支同样要判「无意义的单表达式」

    逐块候选编译不过时会退到旧的整段跨度。模型把一段带 ``` 的话整个用引号括起来时，
    那个跨度**是一个合法的字符串字面量** —— 只判语法就会放行，沙盒以 NORMAL 退出，
    Bot 一言不发、日志里连一条 ERROR 都没有。
    """
    fence = "`" * 3
    quote = chr(34) * 3
    # 跨行的**单**引号串本来就编译不过，够不到这条分支；三引号串才是个合法的单一字面量。
    raw = f"{fence}python\n{quote}开头 {fence}\nprint(1)\n{fence} 结尾{quote}\n{fence}"

    parsed = parse_chat_response(raw)

    assert parsed.code_missing is True
    assert parsed.code_content == ""

