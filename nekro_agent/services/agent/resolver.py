import ast
import re
from typing import List

from pydantic import BaseModel

from nekro_agent.tools.at_markup import normalize_malformed_at_markup

# 逐个围栏块匹配：一个块的终止符是**它自己的**闭合围栏，而不是整段响应的结尾。
#
# 旧写法 ```` ```(?:python)?\s*(.*?)```(?:\s*$|(?=\s*```\s*$)) ```` 把终止条件锚在了 `$` 上，
# 造成两类线上事故：
#   1. 响应里有多个代码块时，非贪婪会一路吃到**最后一个**围栏，中间的 ``` 与自然语言说明
#      原样进入 code_content 被当代码执行（线上 exec_code id=4372/583/531 的 SyntaxError）；
#   2. 闭合围栏后面还有一句「以上就是我的回复」这类尾随文字时，**整个正则都不匹配**，
#      于是掉进散文兜底、把含围栏的整段当代码执行。
#
# 末尾的 `\Z` 分支只服务于「响应被截断、开围栏没有闭合」的形态：此时该块一直取到串尾，
# 与旧实现（兜底 + 去掉 ```python 前缀）的结果保持一致，不至于把半截代码判成「没有代码」。
CODE_BLOCK_PATTERN = re.compile(r"```(?:python)?[ \t]*\r?\n?(.*?)(?:```|\Z)", re.DOTALL)

# 旧实现的「整段跨度」正则，**逐字保留**，只作为次选候选使用。
#
# 它把闭合条件锚在串尾，所以会从第一个开围栏一路吃到最后一个闭围栏 —— 这正是 B1/B2 的根因，
# 因此不能再当主判据。但同一个「吃到最后」的行为，对**代码正文里本身出现 ``` ** 的形态
# 恰好是对的：内层围栏被整个包在跨度里。逐块匹配在这类输入上会在内层围栏处提前截断，
# 把 `send_msg_text(_ck, "` 这种半截语句当代码送进沙盒（复核实测：旧版输出完整可编译，
# 新版输出 `SyntaxError: unterminated string literal`）。
#
# 回退判据是 `ast.parse`，不是「谁更长」：只有逐块结果**不可编译**、而整段跨度**可编译**时
# 才改用它。这道闸门保证 B1/B2 不会被放回来 —— 那两类的正确块本来就可编译，主判据直接胜出；
# 而它们的整段跨度里夹着裸 ``` 与中文说明，永远编译不过，选不上。
LEGACY_SPAN_PATTERN = re.compile(r"```(?:python)?\s*(.*?)```(?:\s*$|(?=\s*```\s*$))", re.DOTALL)

THINK_BLOCK_PATTERN = re.compile(r"<think>(.*?)</think>", re.DOTALL)


class ParsedCodeRunData(BaseModel):
    raw_content: str
    code_content: str
    thought_chain: str
    code_missing: bool = False
    """没能从响应里提取出可执行代码（纯拒答/纯说明文字/空代码块）。

    调用方据此**跳过沙盒**：把自然语言原样送进沙盒执行只会得到一条 SyntaxError，
    既污染错误日志又白烧一轮迭代。
    """


def _extract_code_blocks(text: str) -> List[str]:
    """按围栏逐块提取代码正文（各自 strip，可能含空串）"""
    return [match.group(1).strip() for match in CODE_BLOCK_PATTERN.finditer(text)]


def _pick_longest_code_block(text: str) -> str:
    """多个围栏块时优先取**能编译的**最长块；一个都编译不过时退回最长块

    「取最长」是既有语义，但单凭长度选块在线上真实形态上会选错：模型经常在代码块之后
    再开一个块写给自己看的说明（`检查语法与规则：...`），而说明文字完全可能比代码更长。
    比长度就会挑中说明，于是整段说明被当代码执行 —— B1 换了个壳又回来了。

    一个块都编译不过时**原样返回最长块**而不是空串：调用方还要拿它再试一次旧的整段跨度
    （见 `LEGACY_SPAN_PATTERN`），把「代码正文里本身出现 ``` 」那一类救回来。
    """
    blocks = _extract_code_blocks(text)
    if not blocks:
        return ""
    parsable: List[str] = []
    for block in blocks:
        finalized = _finalize_code(block)
        if finalized and _is_executable_code(finalized):
            parsable.append(block)
    return max(parsable or blocks, key=len)


def _pick_longest_legacy_span(text: str) -> str:
    """用旧的「整段跨度」正则取最长候选（见 `LEGACY_SPAN_PATTERN`），没有则返回空串"""
    spans = [match.group(1).strip() for match in LEGACY_SPAN_PATTERN.finditer(text)]
    return max(spans, key=len) if spans else ""


def _is_parsable_python(text: str) -> bool:
    """整段内容是否真的是一段能编译的 Python 源码

    判据用的是**宿主机**的 Python 语法，而沙盒镜像固定在 `python:3.11-slim-bullseye`
    （`sandbox/dockerfile:2`），宿主机自身被 `pyproject.toml` 限死在 `>=3.11,<3.13`。
    两端同为 3.11 时判据与沙盒逐字一致；宿主机是 3.12 时判据只会**更宽松**（新语法能过闸门、
    到沙盒里照旧报错），也就是最坏退回改造前的行为，不会把本来能跑的代码挡在门外。

    **不要把 `ast.parse` 换成 `compile(..., "exec")`**：后者多跑一遍符号表检查，会把顶层
    `await` / `return` / `yield` 判成 SyntaxError（实测 `ast.parse("await f()")` 通过、
    `compile("await f()", "<s>", "exec")` 报 `'await' outside function`）。模型写顶层 `await`
    是常见形态，换成 `compile` 会让这一整类回复被误判成「没有代码」而直接不进沙盒——
    那才是真正的回归。宽一点没有代价：多出来的那点语法到沙盒里照旧原样报错。
    """
    try:
        ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return False
    return True


def _is_meaningless_single_expression(text: str) -> bool:
    """整段内容是不是「一个孤零零的名字/字面量」

    `ast.parse` 只回答「语法上是不是 Python」，而中文、日文这些字符是合法的标识符首字符
    （`XID_Start`），于是**不带标点的短句拒答**（`你好` / `抱歉` / `Sorry`）能原样通过语法闸门，
    被当成裸代码送进沙盒——正是本轮要根除的那条路径，只是换了个更短的样本。

    这里只掐掉最窄的一种形态：整个模块体恰好是一条表达式语句，且这条表达式是一个裸名字
    或一个字面量。这种脚本在沙盒里**永远**没有意义——裸名字必然 `NameError`，
    裸字面量是个静默 no-op（比报错更糟：Bot 一言不发、沙盒以 NORMAL 退出，
    日志里连一条 ERROR 都没有）。

    刻意**不**收窄到「模块体为空」（纯注释）：`# 注释\\npass` 这类线上真实成功样本
    的模块体是 `[Pass]`，纯注释的模块体是 `[]`，两者都保留原样放行。
    """
    try:
        module = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return False
    if len(module.body) != 1:
        return False
    stmt = module.body[0]
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Name, ast.Constant))


def _is_executable_code(text: str) -> bool:
    """整段内容是不是**值得送进沙盒**的代码

    「可执行」= 语法上是 Python（`_is_parsable_python`）**且**不是一个孤零零的名字/字面量
    （`_is_meaningless_single_expression`）。

    这两个判据必须一起用，而且只能有这一处定义。原先只有「没有围栏」那条支路同时用了两个，
    围栏支路只判语法——于是 `'抱歉，我不能帮你'` 这种**合法的表达式语句**只要被包进
    ```` ```python ```` 里就能绕过守卫进沙盒，以 NORMAL 退出，Bot 一言不发、日志里
    连一条 ERROR 都没有（正是 `_is_meaningless_single_expression` docstring 里点名
    「比报错更糟」的那个形态）。而本轮新增的 `CODE_MISSING_FEEDBACK` 恰恰在要求模型
    「把拒绝写进代码块里」，等于把这条形态的概率推高了。
    """
    return _is_parsable_python(text) and not _is_meaningless_single_expression(text)


def _finalize_code(code_content: str) -> str:
    """把一个候选块收敛成最终要送进沙盒的代码正文

    抽成函数是为了让「候选是否可编译」的判定发生在**最终形态**上：残留的 ```python 前缀
    与尾随 ``` 会被先剃掉，否则闸门判的是一份和真正执行的不一样的文本。
    """
    stripped = code_content.strip()
    if stripped.startswith("```python"):
        stripped = stripped[len("```python") :].strip()
    if stripped.endswith("```"):
        stripped = stripped[: -len("```")].strip()
    return fix_code_content(stripped).strip()


def parse_chat_response(raw_content: str) -> ParsedCodeRunData:
    """
    解析聊天响应内容，提取思维链和代码内容。

    Args:
        raw_content: 原始响应内容

    Returns:
        ParsedCodeRunData对象, 即代码执行数据
    """
    # 清理和准备内容
    cleaned_content = raw_content.strip()
    thought_chain = ""
    code_content = ""

    # 检查是否同时存在<think>和</think>标签
    has_think_tags = "<think>" in cleaned_content and "</think>" in cleaned_content

    # 主判据取自哪一段文本，回退判据就得取自同一段，否则两个候选比的不是同一个东西
    search_scope = cleaned_content

    if has_think_tags:
        # 如果存在思维链，先提取思维链
        think_match = THINK_BLOCK_PATTERN.search(cleaned_content)

        if think_match:
            # 提取思维链内容并清理可能存在的嵌套标签
            thought_chain = think_match.group(1).strip()
            thought_chain = re.sub(r"</?think>", "", thought_chain)

            # 在思维链之后查找代码块
            after_think = cleaned_content[think_match.end() :].strip()
            code_content = _pick_longest_code_block(after_think)
            if code_content:
                search_scope = after_think

    # 如果通过思维链匹配没有提取到代码内容，尝试直接匹配代码块
    if not code_content:
        code_content = _pick_longest_code_block(cleaned_content)
        search_scope = cleaned_content

    fixed_code_content = _finalize_code(code_content)

    if fixed_code_content and not _is_executable_code(fixed_code_content):
        # 逐块结果编译不过：可能是「代码正文里本身出现 ``` 」被内层围栏截断了。
        # 只有旧的整段跨度**能**编译时才回退过去（判据见 `LEGACY_SPAN_PATTERN` 的说明）。
        #
        # 整段跨度只在作用域里出现**第三个** ``` 时才可能与逐块结果不同：恰好一对围栏时
        # 两者必然是同一段文本，跨度扫描纯属白跑。这道前置把回退分支的作用面**证明性地**
        # 限制在「正文里带 ``` 」这一类上——单围栏形态永远走不到 `legacy_code_content` 这一支。
        # 它只挡回退，**不挡**下面那条 else：围栏里装的是散文时同样只有两个围栏，
        # 把 else 一起挡掉等于把「拒答被包进代码块」这条形态又放回沙盒。
        legacy_code_content = ""
        if search_scope.count("```") >= 3:
            legacy_code_content = _finalize_code(_pick_longest_legacy_span(search_scope))
        if legacy_code_content and _is_executable_code(legacy_code_content):
            fixed_code_content = legacy_code_content
        else:
            # 两个候选都编译不过 —— 围栏里装的根本不是代码。
            #
            # 「不把散文当代码」这道闸门原先**只**装在下面那条「没有围栏」的支路上，
            # 于是只要响应里出现哪怕一个 ``` ，整段自然语言就能绕过它被送进沙盒：
            #   * 模型把拒答话术包进 ```...``` 里（本轮新增的 `CODE_MISSING_FEEDBACK`
            #     恰恰在要求模型「把拒绝写进代码块里」，等于把这条形态的概率推高）；
            #   * 散文里恰好出现一个孤立的 ``` （`我不能帮你写 ``` 这种东西`），
            #     逐块正则的 `\Z` 分支会把它后面的半句话整个当成一个块。
            # 两者的终点都一样：容器里跑一段中文，回来一条必然的 SyntaxError。
            #
            # 判定为「没能提取出代码」：不进沙盒，直接要求模型重出。
            # 代价是模型**真的**把语法写错时也走这条路（原先能拿到 SyntaxError 原文回喂），
            # 但那一类本来就会被无进展检测在第二次相同签名时掐掉，重试预算并没有变少；
            # 换回来的是省掉一次 100% 注定失败的容器启动。
            fixed_code_content = ""

    if not code_content:
        # 没有任何围栏块：只有整段内容**真的是一段可编译的 Python**（模型不带围栏直接
        # 输出裸代码的既有形态）才接受；否则判定为「没能提取出代码」，绝不把拒答话术、
        # 纯说明文字当成脚本送进沙盒。
        content_without_think = re.sub(r"<think>.*?</think>", "", cleaned_content, flags=re.DOTALL).strip()
        if (
            content_without_think
            and _is_executable_code(content_without_think)
        ):
            fixed_code_content = _finalize_code(content_without_think)

    return ParsedCodeRunData(
        raw_content=raw_content,
        code_content=fixed_code_content,
        thought_chain=thought_chain,
        code_missing=not fixed_code_content,
    )


def fix_code_content(code_content: str) -> str:
    """修复代码内容"""
    # 修正代码块去掉所有 from plugins ... import ... 开头的行
    code_content = re.sub(r"^from plugins.*\n", "", code_content, flags=re.MULTILINE)
    code_content = re.sub(r"^from predefined_methods .*\n", "", code_content, flags=re.MULTILINE)
    code_content = re.sub(r"^from predefined_plugins .*\n", "", code_content, flags=re.MULTILINE)
    code_content = re.sub(r"^from core\.api.*\n", "", code_content, flags=re.MULTILINE)
    # 修正代码块去掉所有 from plugin_manager ... import ... 开头的行
    return re.sub(r"^from plugin_manager.*\n", "", code_content, flags=re.MULTILINE)


def fix_raw_response(raw_response: str) -> str:
    """修复原始响应"""
    # logger.debug(f"Raw response: {raw_response}")
    raw_response = normalize_malformed_at_markup(raw_response)

    # 处理类似 `<1952b262 | message separator>` 模型幻觉续写的情况，截断其后的所有内容
    reg = r"<\w{8} \| message separator>"
    match = re.search(reg, raw_response)
    if match:
        raw_response = raw_response[: match.start()]

    # 检查是否存在多个 <think> 标签
    tags = ["<think>", "</think>"]
    for tag in tags:
        if raw_response.count(tag) > 1 and raw_response.endswith(tag):
            raw_response = raw_response[: -len(tag)]

    # logger.debug(f"Fixed raw response: {raw_response}")
    return raw_response.strip()
