"""上游拒答的**第二种形态**：渠道把拒绝页当成普通正文塞进 `content`

## 为什么已有的闸门全都拦不住它

上一棒覆盖的是「`content` 为空 + `finish_reason='prohibited_content'`」——测试部署
gemini-3.6 的形态，由 `openai.LLMContentBlockedError` 在出口处判掉。用户另一台部署跑
**gemini-3.7-flash-high**，渠道把**同一件事**渲染成一段普通文本回来（线上原文逐字）：

    The prompt could not be submitted. The prompt contains sensitive words that violate
    Google's [Generative AI Prohibited Use policy](https://policies.google.com/terms/
    generative-ai/use-policy). Try rephrasing the prompt. If you think this was an error,
    [send feedback](https://ai.google.dev/gemini-api/docs/troubleshooting).

正文非空、`finish_reason` 正常、HTTP 200，于是**每一道判空闸门都原样放行**。后果（用户
截图 + 本机复现逐字吻合）：

1. 整段没有 ``` 围栏 → 旧的 prose 兜底把它整段当代码 → 沙盒 `SyntaxError: unterminated
   string literal`（触发字符是 `Google's` 的撇号，它开了一个不闭合的字符串字面量）；
2. 错误连同拒答原文一起塞回下一轮 → 上下文里触发安全策略的东西还在 → 再拒答 → 同一个错；
3. 烧满迭代 → `AgentRoundUnfinishedError` → 用户看到「已经试到最大迭代次数啦」。

`resolver` 的 `ast.parse` 闸门（上一棒）已经能挡住第 1 步的「被执行」——这段文本编译不过，
`code_missing` 会是 True。但它只回答「这不是代码」，回答不了「这压根不是模型的输出」，
于是第 2、3 步照旧：拒答原文仍然被当成模型的话回喂进上下文，循环仍然烧满。

本模块只负责**认出它**（G1）。拿这个判定去改上下文回喂与收尾文案是后续的事，
这里刻意不 import 任何调用方，保持可单测。

## 判据：四个合取，任何一个不满足就当正常输出

见 `detect_provider_refusal` 的 docstring。核心取舍是**误杀比漏杀贵得多**：

- 漏杀的代价：退回本模块不存在时的行为——`ast.parse` 闸门照旧挡住执行，最坏是烧满迭代，
  也就是今天的现状，不会更糟。
- 误杀的代价：小樱**正常的剧情对白**被当成上游拒答丢掉/改写。这是角色扮演 Bot，
  线上 `exec_code` 全表实测：25 条命中「抱歉/不能/违反」这类拒答关键词的正文，
  **全部是剧情对白**，一条上游拒答都没有。单关键词判据在这份数据上准确率是 0。

所以判据里**没有任何一条是单关键词**。

## 没有实据的供应商一律不加

只登记了 Google/Gemini 这一条——它有线上原文、有用户截图、有本机逐字复现。
OpenAI / Anthropic / 各家中转站的拒绝页形态**没有实据**，凭印象写正则等于凭空扩大误杀面。

TODO(实据到手再加)：拿到其它供应商拒绝页的**逐字原文**后，往
`PROVIDER_REFUSAL_SIGNATURES` 里加一条 `ProviderRefusalSignature`，并在
`tests/test_agent_provider_refusal_text.py` 的正例表里加上同一份逐字原文。
不要改判据形状，也不要为了「兼容」把某个组的短语放宽成单个通用词。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

# 供应商拒绝页是一段**固定短文案**：已知的 Gemini 那条是 322 字符。上限取 1200（约 3.7 倍
# 余量）不是为了性能，而是又一道保守闸门——真要出现「一大段剧情对白里恰好嵌了政策链接和
# 政策措辞」的倒霉组合，长度这一条就先把它放行了。代价是「渠道给拒绝页加了长前言」这类
# 形态会漏判，而漏判恰好是便宜的那一侧。
MAX_REFUSAL_TEXT_CHARS = 1200

# 「整句 boilerplate 锚点」那一档（`PROVIDER_BOILERPLATE`）额外要求：把已知的 boilerplate 片段、
# 链接、标点、空白全部剔掉之后，**剩不下几个字**。理由见 `_boilerplate_dominates`：
# 那一档没有域名做第二道墙，唯一能把「上游真的只回了这一页」与「模型引用了这一页」分开的，
# 就是这一页有没有被别的话包着。24 是按「一个短前缀（`[gemini-3.7-flash-high] `）能过、
# 一个中文小句（`上一轮的错误是`+`我再换个说法`）过不去」定的。
MAX_RESIDUAL_CHARS = 24

_FENCE = "```"

# 先摘掉 URL 再找代码特征：政策链接里天然带 `/`、`?key=`、`-` 这些标点，
# 不摘的话 `?key=value` 会被赋值正则当成代码，把正例自己判掉。
# 只吃 ASCII 可打印字符，**不吃**中日韩字。`\S+` 那种写法会把紧贴在链接末尾的中文一起吞掉，
# 而中文紧跟链接不加空格在中文语境里是常态（「详见 https://example.com/x哦」）——
# 被吞掉的那个「哦」正是判「这是小樱在说话」的依据，吞了就等于把闸门在这一格上关掉。
# 对残余量计算而言，这个改动只会让残余变大（更保守），方向也是对的。
_URL_PATTERN = re.compile(r"https?://[\x21-\x7e]+", re.IGNORECASE)

# 标识符**紧贴**左括号才算函数调用。`\s*` 绝对不能加：英文散文里 `policy (see below)`
# 这种「词 + 空格 + 括号」到处都是，允许空格等于把整类英文说明判成代码。
# 已知正例里的两处括号都是 markdown 链接的 `](`，左边是 `]` 不是词字符，不会命中。
_CALL_PATTERN = re.compile(r"[A-Za-z_]\w*\(")

# 下标取值 `msg[0]` / `data['k']`。同样要求紧贴：markdown 的 `Google's [Generative...`
# 与 `error, [send feedback]` 左边都是空格或标点。
_SUBSCRIPT_PATTERN = re.compile(r"[A-Za-z_]\w*\[")

# 赋值。末尾 `[^=]` 排掉 `==`；`!=` / `<=` / `>=` 因为 `=` 左边是标点而非 `\w`，本来就匹配不上。
_ASSIGN_PATTERN = re.compile(r"[A-Za-z_]\w*[ \t]*(?://|\*\*|[-+*/|&^%@])?=[^=]")

_COMMENT_LINE_PATTERN = re.compile(r"^[ \t]*#", re.MULTILINE)

# 只放**歧义最小**的 Python 关键字。刻意不收 if/else/for/while/with/try：它们同时是常用英文词，
# 而供应商拒绝页就是英文散文，收进来会让正例被自己的措辞判成代码（漏杀虽便宜，但白写一个模块）。
_KEYWORD_LINE_PATTERN = re.compile(
    r"^[ \t]*(?:import|from|def|class|async|await|return|pass|raise|del|global|nonlocal|lambda|yield|elif|except|finally)\b",
    re.MULTILINE,
)

_TRIPLE_QUOTE_PATTERN = re.compile(r"'''|\"\"\"")

# NekroAgent 自己的输出标记。出现即证明这段文本是**模型按我们的协议写的**，
# 不可能是供应商拒绝页。
_AGENT_MARKUP_PATTERNS = (
    re.compile(r"\[@[^\]\n]*@\]"),  # at 标记 `[@id:1@]`
    re.compile(r"</?think>", re.IGNORECASE),  # 思维链标签
)

_CJK_PATTERN = re.compile(r"[㐀-䶿一-鿿]")

# 口语/人设标记：出现即判定「这是小樱在说话」，整段放行。
#
# 这一层是**纯粹的放行层**，它只会制造漏杀、永远不会制造误杀——所以它可以（也应该）写得
# 比判定层松。它防的是本棒实测到的那类形态：拒答原文被回喂进上下文后，模型下一轮**引用**它
# （「唔…刚才上游提示 The prompt could not be submitted，前辈换个说法再问我一次好不好？」）。
# 这条路径没有代码特征、没有 at 标记、长度也在闸门内，光靠签名合取只剩「域名」一道墙。
#
# 收词的标准：**已知的供应商拒绝页里一个都不会出现**。所以只收句末语气词、称呼与颜文字，
# 刻意不收「请 / 您 / 你 / 重试 / 修改 / 尝试」——中文拒绝页（`V4` 形态）恰恰全是这些词，
# 收进来会把真正的中文拒绝页一起放掉。
_CONVERSATIONAL_MARKERS = (
    "前辈",
    "呀",
    "啦",
    "嘛",
    "呢",
    "哦",
    "喔",
    "咯",
    "唔",
    "诶",
    "欸",
    "哟",
    "呐",
    "吧",
    "嗯",
    "哈哈",
    "嘿嘿",
    "嘻嘻",
    "qwq",
    "orz",
    "~",
    "～",
)

# 「模型自己在角色内拒绝」的词。**只用于观测与日志分类，永远不用来拦截**——
# 见 `ResponseRefusalKind.IN_CHARACTER_REFUSAL` 的说明。
_IN_CHARACTER_REFUSAL_TOKENS = (
    "抱歉",
    "对不起",
    "不能",
    "不可以",
    "无法",
    "做不到",
    "不方便",
    "违反",
    "拒绝",
    "sorry",
    "i can't",
    "i cannot",
    "i can not",
    "i'm unable",
    "i am unable",
    "can't help",
    "cannot help",
)


@dataclass(frozen=True)
class ProviderRefusalSignature:
    """一家供应商拒绝页的特征：域名 + 若干「每组至少中一个」的短语组

    判定是**合取**：`hosts` 中至少一个出现，且 `phrase_groups` 的**每一组**都至少中一个。
    组内是或、组间是且——这样每加一组就是收紧一次，不会因为某组写得宽而整体放宽。

    `fragments` 与 `ProviderBoilerplate.fragments` 同义：算残余量时要剔掉的**全部**已知
    页面片段。域名档原先没有这一项，于是缺了 boilerplate 档那两道**防引用**的墙——
    实测「我查了下，错误是 The prompt could not be submitted，详见 <政策链接> 这个
    use policy 里写的 violate 条款。」会被判成上游拒答（域名、政策名、违规动词全中）。
    触发条件一点都不假想：**拒答原文本来就会被贴进群里**，模型下一轮引用它即可。
    误杀的代价是模型的正常回复被丢弃、回合零重试掐断，而用户收到「被安全策略挡下了」——
    实际什么都没被挡。少列一条 fragment 只会把真拒绝页的残余量算大、从而漏判，
    依旧是便宜的那一侧。
    """

    provider: str
    hosts: Tuple[str, ...]
    phrase_groups: Tuple[Tuple[str, ...], ...]
    fragments: Tuple[str, ...] = ()
    verbatim_markers: Tuple[str, ...] = ()
    """能认出**这一版页面**的独有整句。是 `fragments` 的子集，但**不含**政策文件名一类的通用词。

    残余量只有在「认得出这一版」时才算得准，而认版必须靠独有句式：政策文件名
    （`generative ai prohibited use policy`）在**变体页**里同样出现，拿它认版会把
    `Your prompt was blocked because it violates …` 这类真拒绝页误判成引用。
    """


@dataclass(frozen=True)
class ProviderRefusalMatch:
    """命中详情，供日志与后续棒次（回喂裁剪、收尾文案）取用"""

    provider: str
    matched_host: str
    matched_phrases: Tuple[str, ...]


class ResponseRefusalKind(str, Enum):
    """一段 LLM 正文的拒答归类

    三个取值里**只有 `PROVIDER_REFUSAL` 可以据此改变行为**，另外两个都必须原样放行。
    分成三档而不是一个 bool，是为了让「命中了拒答关键词但我们故意没拦」这件事在日志里
    看得见——否则误杀与漏杀在观测上长得一模一样，出了事只能靠猜。
    """

    NORMAL = "normal"
    """既没有拒答词、也不是拒绝页的普通输出。"""

    IN_CHARACTER_REFUSAL = "in_character_refusal"
    """模型**在角色内**说的拒绝（「抱歉呀前辈，我没有这个权限呢」）。

    这是**正常输出**，是角色扮演 Bot 的日常内容，绝对不许拦、不许改写、不许当失败。
    这个取值只用于日志与统计（「这一轮里有多少条像拒答但其实是台词」）。
    """

    PROVIDER_REFUSAL = "provider_refusal"
    """供应商的拒绝页被当成正文送了回来——不是模型的输出，可据此改变行为。"""


PROVIDER_REFUSAL_SIGNATURES: Tuple[ProviderRefusalSignature, ...] = (
    ProviderRefusalSignature(
        provider="google_gemini",
        # 政策页与排障文档的域名。两者都只出现在 Google 自己渲染的拒绝页里，
        # 模型正常输出中不会平白引用它们。
        hosts=("policies.google.com", "ai.google.dev"),
        phrase_groups=(
            # 第一组：必须指名**一份政策文件**。单独一个 "policy" 不算——它太常见，
            # 而且已知拒绝页给的是完整名字 "Generative AI Prohibited Use policy"。
            (
                "prohibited use policy",
                "prohibited use policies",
                "use policy",
                "usage policy",
                "usage policies",
                # 中文本地化的同一页（渠道会按 `Accept-Language` 渲染中文）。仍然要求域名，
                # 所以这几条不是「单个中文词」的判据——「政策」两个字自己什么都判不出来。
                "使用政策",
                "使用限制政策",
                "使用限制",
                "禁止使用政策",
            ),
            # 第二组：必须有**违规/拦截**的动词或断言。刻意不收 "prohibited"——它是第一组
            # 短语的子串，收进来这一组就成了摆设，合取会退化成单条件。
            (
                "violate",
                "violates",
                "violating",
                "violation",
                "could not be submitted",
                "was blocked",
                "blocked by",
                # 中文本地化形态的违规/拦截断言。刻意都是**多字短语**：单独的「违反」被
                # `tests/..._provider_refusal_text.py::_BANNED_STANDALONE_PHRASES` 明令禁止，
                # 而那条禁令是对的——小樱的日常对白里「违反」出现得很频繁（群规、约定、剧情）。
                "无法提交",
                "包含违反",
                "违反了",
                "已被拦截",
                "已被阻止",
            ),
        ),
        # 逐字原文的全部句式（用户 2026-08-18 提供的截图）。只用于算「这一页之外还说了什么」，
        # 不参与命中判定——所以往里加句式只会让判定更保守，不会放宽。
        fragments=(
            "the prompt could not be submitted",
            "the prompt contains sensitive words that violate",
            "google's",
            "googles",
            "generative ai prohibited use policy",
            "try rephrasing the prompt",
            "if you think this was an error",
            "send feedback",
        ),
        # 只挑这一版**独有**的整句。刻意不含 "generative ai prohibited use policy"（政策名，
        # 变体页照样有）与 "google's" / "send feedback"（太通用）。
        verbatim_markers=(
            "the prompt could not be submitted",
            "the prompt contains sensitive words that violate",
            "try rephrasing the prompt",
            "if you think this was an error",
        ),
    ),
    # TODO(需要逐字原文): 其它供应商/中转站的拒绝页形态无实据，不猜测着加（见模块 docstring）。
)


@dataclass(frozen=True)
class ProviderBoilerplate:
    """没有政策域名可锚的那一类拒绝页：靠**整句 boilerplate + 占满整段**来定性

    有些渠道（OpenAI 自家、Azure 网关）的拒绝说明里压根没有链接，`ProviderRefusalSignature`
    的第一道墙（域名）无从谈起。这一档用两条**互相独立**的墙替代它：

    1. `groups` —— 组内标志串**全部**命中才算一次匹配。这些是供应商的固定 boilerplate 整句，
       不是关键词；`("azure openai", "content management policy")` 这种两个弱标志的组合，
       任何一半单独出现都判不出东西。
    2. `_boilerplate_dominates` —— 已知 boilerplate 片段之外**剩不下几个字**。
       这条才是真正顶住误杀的那面墙：模型引用这一页时总要用自己的话包一层
       （「上一轮报错说 …，我再换个说法」），包了就过不了残余量闸门。

    `fragments` 是算残余量时要剔掉的**全部**已知 boilerplate 片段（`groups` 里的标志串是它的
    子集）。少列一条的后果只是把真拒绝页的残余量算大、从而漏判——依旧是便宜的那一侧。
    """

    provider: str
    groups: Tuple[Tuple[str, ...], ...]
    fragments: Tuple[str, ...]


PROVIDER_BOILERPLATE: Tuple[ProviderBoilerplate, ...] = (
    ProviderBoilerplate(
        provider="google_gemini",
        # 逐字原文的第一句。渠道把这一页截断到只剩第一句是真实可能的形态，
        # 而这一句自己就已经是「上游在说话」的完整证据：它谈的是 *the prompt*，
        # 不是对话内容——小樱没有理由凭空说出这一句并且**只说这一句**。
        groups=(("the prompt could not be submitted",),),
        fragments=(
            "the prompt could not be submitted",
            "the prompt contains sensitive words that violate",
            "generative ai prohibited use policy",
            "try rephrasing the prompt",
            "if you think this was an error",
            "send feedback",
            "google's",
        ),
    ),
    ProviderBoilerplate(
        provider="openai",
        # 同构的兄弟形态，**非本次现场取证**（本棒没有 OpenAI 的线上截图）。
        # 收它的理由与收 Google 那条一致：整句 boilerplate，且后果完全一样。
        groups=(("your request was rejected as a result of our safety system",),),
        fragments=(
            "your request was rejected as a result of our safety system",
            "your prompt may contain text that is not allowed by our safety system",
        ),
    ),
    ProviderBoilerplate(
        provider="azure_openai",
        # 同上，非本次现场取证。两个弱标志的合取：单说渠道名（「我这台用的是 Azure OpenAI」）
        # 或单说政策名（「content management policy 怎么翻译」）都判不出东西。
        groups=(("azure openai", "content management policy"),),
        fragments=(
            "azure openai",
            "content management policy",
            "the response was filtered due to the prompt triggering",
            "please modify your prompt and retry",
        ),
    ),
)


def has_code_features(text: str) -> bool:
    """整段文本里有没有**任何**代码特征

    判据故意偏向「宁可多认成代码」：认成代码 → 不判拒绝页 → 漏杀，而漏杀是便宜的那一侧。
    覆盖的特征：``` 围栏、`name(` 函数调用、`name[` 下标、赋值号、`#` 注释行、
    行首 Python 关键字、三引号，以及「整段能被 `ast.parse` 编译」。

    最后那条是为了兜住**没有明显标点的裸代码**：`# 违反规则的做法\\npass` 这种整段
    只有一条注释加一个 `pass` 的样本，靠标点特征只中 `#`，而 `ast.parse` 能直接盖章。
    它也顺带把「一个孤零零的词」判成代码（`ast.parse("policy")` 是合法的 `Name` 表达式）——
    这与 `resolver._is_meaningless_single_expression` 的口径一致：那种文本在本管线里本来
    就会被当成裸代码送进沙盒，叫它「像代码」不算冤枉。

    空串与纯空白返回 False：`ast.parse("  ")` 是合法的空模块，但把「什么都没有」说成
    「有代码特征」只会让调用方读不懂日志。
    """
    if not text or not text.strip():
        return False
    if _FENCE in text:
        return True
    stripped_urls = _URL_PATTERN.sub(" ", text)
    for pattern in (
        _CALL_PATTERN,
        _SUBSCRIPT_PATTERN,
        _ASSIGN_PATTERN,
        _COMMENT_LINE_PATTERN,
        _KEYWORD_LINE_PATTERN,
        _TRIPLE_QUOTE_PATTERN,
    ):
        if pattern.search(stripped_urls):
            return True
    try:
        ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return False
    return True


def has_agent_markup(text: str) -> bool:
    """文本里有没有 NekroAgent 自己的输出标记（at 标记、`<think>`）

    命中即证明这是模型按我们的协议写出来的，不可能是供应商的拒绝页。
    """
    return any(pattern.search(text) for pattern in _AGENT_MARKUP_PATTERNS)


def normalize_for_match(text: str) -> str:
    """折成「小写 + 空白归一」的单行，专供域名/短语的子串比对

    换行归一是必需的：渠道返回的拒绝页常常按宽度折行，`prohibited use policy` 这种多词短语
    很容易被 `\\n` 从中间劈开，逐字 `in` 就再也匹配不上——判据在真实形态上直接失效。

    只用于比对，**不用于**代码特征探测：`has_code_features` 的「`#` 注释行」「行首关键字」
    两条判据靠的正是行首位置，折成一行就全废了。
    """
    return " ".join(text.lower().split())


def has_conversational_markers(text: str) -> bool:
    """文本里有没有**口语/人设**标记（句末语气词、称呼、颜文字）

    命中即判定「这是小樱在说话」。它只制造漏杀、不制造误杀，所以放在最前面无条件放行。

    防的是本棒实测到的形态：拒答原文被回喂进上下文之后，模型下一轮**引用**它——
    这条路径既没有代码特征也没有 at 标记，长度还在闸门内，光靠签名合取只剩域名一道墙。

    先摘 URL 再扫：`~` 在这张表里，而它在 URL 路径（`/~user`）里是合法字符。
    """
    if not text:
        return False
    scannable = _URL_PATTERN.sub(" ", text).lower()
    return any(marker in scannable for marker in _CONVERSATIONAL_MARKERS)


def _strip_known_boilerplate(lowered: str) -> str:
    """把**所有**已知供应商 boilerplate 片段、链接、标点、空白从归一化文本里剔掉

    剩下的就是「这一页之外还说了什么」。跨供应商一起剔是有意的：残余量算得越小越容易判成
    拒绝页，而这里刻意反着来——多剔一家的片段只会让残余变小……所以恰恰要小心。
    实际取舍见 `_boilerplate_dominates`：那里用的是**本次命中那一档的**片段表，不是全表。
    """
    residual = _URL_PATTERN.sub(" ", lowered)
    return "".join(ch for ch in residual if ch.isalnum())


def _boilerplate_dominates(lowered: str, entry: ProviderBoilerplate) -> bool:
    """这一页 boilerplate 是不是**占满了整段**，而不是被别的话引用着

    这是 `PROVIDER_BOILERPLATE` 那一档唯一顶得住误杀的墙（它没有域名可锚）。剔掉该供应商的
    全部已知片段、链接与标点之后，剩下的字数必须 <= `MAX_RESIDUAL_CHARS`。

    - 渠道加的短前缀（`[gemini-3.7-flash-high] `、`上游返回错误：`）过得去；
    - 模型的引用（`上一轮报错说 …，我再换个说法哦`）过不去。
    """
    residual = lowered
    for fragment in entry.fragments:
        residual = residual.replace(fragment, " ")
    return len(_strip_known_boilerplate(residual)) <= MAX_RESIDUAL_CHARS


def _match_boilerplate(lowered: str, raw: str) -> Optional[ProviderRefusalMatch]:
    """整句 boilerplate 那一档：组内全中 + 占满整段 + 不是被中文包着的英文引文"""
    for entry in PROVIDER_BOILERPLATE:
        for group in entry.groups:
            if not all(marker in lowered for marker in group):
                continue
            if not _boilerplate_dominates(lowered, entry):
                continue
            # 语种错位 = 有人在引用。标志串整组是纯 ASCII，而正文里却有中日韩字，
            # 说明这段英文 boilerplate 是被中文包着的——那是模型在复述，不是上游在说话。
            # （中文本地化的拒绝页整页都是中文，走的是上面的域名档，不受这条影响。）
            if all(marker.isascii() for marker in group) and _CJK_PATTERN.search(raw):
                continue
            return ProviderRefusalMatch(
                provider=entry.provider,
                matched_host="",
                matched_phrases=tuple(group),
            )
    return None


def _signature_is_quoted(
    lowered: str,
    raw: str,
    signature: ProviderRefusalSignature,
    matched_phrases: Tuple[str, ...],
) -> bool:
    """域名档的**防引用**墙：这一页是被别的话引用着，而不是它自己在说话

    与 `_match_boilerplate` 里那两道墙同构，缺了它域名档就只剩「域名 + 两个短语」——
    而这三样在**引用**里出现得和在原页里一样齐全。

    两道墙都只在**匹配到的短语整组是纯 ASCII**（= 命中的是英文页）时生效：中文本地化的
    拒绝页整页都是中文，`matched_phrases` 里会有「使用政策」这类中文短语，那时两道墙一律
    不适用——我们**没有中文页的逐字原文**，不能拿英文页的句式表去给它算残余量，
    否则会把一整类真拒绝页判成引用（那是误判方向相反、但同样真实的错）。

    **占比**（有 `fragments` 时的主判据）：剔掉这一页的已知句式、域名、链接与标点后，
    残余必须 <= `MAX_RESIDUAL_CHARS`。它同时管住中英两种引用，因为中日韩字也算进残余：

    - 渠道加的短前缀（`[gemini-3.7-flash-high] `、`上游返回错误：`）残余只有几个字，过得去；
    - 模型自己的话（`我查了下，错误是 …，详见 … 这个 use policy 里写的 violate 条款`）过不去。

    **刻意不用「正文里有没有中日韩字」当判据**：那条在 `_match_boilerplate` 里是够用的，
    但放到这里会把**中文渠道前缀 + 英文拒绝页**这一真实形态判成引用（实测
    `上游返回错误：<英文原页>` 被误放行）。残余量是同一件事的定量版本，不会犯这个错。

    **残余量只在「认得出这一版页面」时才算得准**。`fragments` 抄的是某一版页面的逐字句式；
    换了措辞的**变体**页（`Your prompt was blocked because it violates …`）句式一条都不命中，
    残余量必然超标 —— 那时用残余量判定等于把真拒绝页全判成引用（实测打红 6 条召回用例）。
    所以先看**认不认得**：命中任意一条 `verbatim_markers` 才算认得出这一版，才有资格量
    "这一页之外还说了什么"；认不出就退回语种错位那条弱墙。
    认版**必须**用 `verbatim_markers` 而不是 `fragments`：后者含政策文件名，而政策名在变体页里
    照样出现，拿它认版等于又把变体页判成引用（这一步实测也打红过同样那 6 条）。
    弱墙也比**没有墙**强，域名档裸奔正是本次要修掉的缺陷。

    由此留下一个**有意的漏杀**：用英文引用一个我们没抄过的变体页 —— 既没有中日韩字给弱墙用，
    也没有 fragment 给残余量用。它比修复前的状况严格更好（修复前**所有**引用都误杀），
    收窄它的办法是把那一版页面的逐字句式抄进 `fragments`。
    """
    if not all(phrase.isascii() for phrase in matched_phrases):
        return False
    if not any(marker in lowered for marker in signature.verbatim_markers):
        return bool(_CJK_PATTERN.search(raw))
    residual = lowered
    for fragment in signature.fragments:
        residual = residual.replace(fragment, " ")
    # **不要**在这里单独剔域名**：`_strip_known_boilerplate` 是靠 `_URL_PATTERN` 整条链接一起剔的，
    # 先把域名换成空格会把 `https://policies.google.com/terms/...` 打断成 `https:// /terms/...`，
    # 于是链接不再匹配、整条路径逐字留在残余里。实测这个顺序错误会让真拒绝页的残余从 6 涨到 70，
    # 把「中文渠道前缀 + 英文原页」判成引用而放行。域名本来就长在链接里，剔链接即已覆盖。
    return len(_strip_known_boilerplate(residual)) > MAX_RESIDUAL_CHARS


def _match_signature(lowered: str, raw: str, signature: ProviderRefusalSignature) -> Optional[ProviderRefusalMatch]:
    matched_host = next((host for host in signature.hosts if host in lowered), None)
    if matched_host is None:
        return None
    matched_phrases: List[str] = []
    for group in signature.phrase_groups:
        hit = next((phrase for phrase in group if phrase in lowered), None)
        if hit is None:
            return None
        matched_phrases.append(hit)
    if _signature_is_quoted(lowered, raw, signature, tuple(matched_phrases)):
        return None
    return ProviderRefusalMatch(
        provider=signature.provider,
        matched_host=matched_host,
        matched_phrases=tuple(matched_phrases),
    )


def detect_provider_refusal(text: str) -> Optional[ProviderRefusalMatch]:
    """判断这段正文是不是**供应商的拒绝页**，是则返回命中详情，否则 None

    ## 判据（四个合取，缺一即放行）

    1. **非空**：strip 后为空直接放行。
    2. **短**：长度 <= `MAX_REFUSAL_TEXT_CHARS`（已知拒绝页 322 字符）。
    3. **整段无代码特征、无 NekroAgent 输出标记、无口语/人设标记**（`has_code_features` /
       `has_agent_markup` / `has_conversational_markers`）。前两条判在**原文**上，
       因为它们依赖行首位置。
    4. 命中**两档判定之一**（都判在 `normalize_for_match` 折过的单行上，好让渠道的折行
       不至于把短语劈开）：

       - **域名档**（`ProviderRefusalSignature`）：该供应商的**政策域名**出现，**且**
         「政策文件名」组与「违规/拦截动词」组各中至少一个，**且**不是被引用着的
         （`_signature_is_quoted`：命中英文页时，语种错位或残余量超标即判为引用）；
       - **整句 boilerplate 档**（`ProviderBoilerplate`）：没有域名可锚的形态
         （OpenAI / Azure 网关），要求整句标志串组内全中、**且**这一页占满整段
         （`_boilerplate_dominates`）、**且**不是被中文包着的英文引文。

    第 4 条的两档都是**合取**，没有任何一档是单关键词。

    ## 已知的漏杀（都是有意的）

    - **没有任何供应商身份锚点的短拒绝**（「抱歉，您的请求已被拦截：内容违反了生成式 AI
      使用政策，请修改后重试。」）：既没有域名、也不是某家的整句 boilerplate。它与小樱自己
      转述故障时可能说的话在**原理上**分不开，所以放行。
    - **被截断到只剩政策文件名**（「Generative AI Prohibited Use policy」）：政策名是小樱
      可以合法说出口的东西（她会向用户解释这次故障），整句 boilerplate 不是。

    两条的代价都只是退回本模块不存在时的行为，不会更糟。

    ## 为什么不会误伤剧情对白

    小樱在剧情里大量说「抱歉」「我不能」「违反」——线上 `exec_code` 全表里 25 条命中拒答
    关键词的正文**全是台词**。这些台词过不了本判据的原因是**多重的**，不依赖任何一条：

    - 台词里不会出现 `policies.google.com` / `ai.google.dev`（第 4 条的域名要求）；
    - 台词里的「违反」是中文，而签名要的是英文政策文件名 + 违规动词的**同时**出现；
    - 剧情输出几乎总是包在 `send_msg_text(...)` 这类调用里，或带 `[@id:1@]` at 标记，
      第 3 条先一步放行。

    换句话说，任务给的四条反例每一条都**同时**踩中两条以上的放行理由，不是靠某一条勉强
    挡住的。这正是「保守」的操作定义：判定要经得起任意一条判据被削弱。

    ## 与「模型在角色内拒绝」的分界

    本函数只认**上游渲染的拒绝页**。模型自己在角色内说的拒绝是正常输出，永远返回 None；
    需要把两者分开观测时用 `classify_response_refusal`。
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_REFUSAL_TEXT_CHARS:
        return None
    if has_code_features(stripped) or has_agent_markup(stripped) or has_conversational_markers(stripped):
        return None
    lowered = normalize_for_match(stripped)
    for signature in PROVIDER_REFUSAL_SIGNATURES:
        match = _match_signature(lowered, stripped, signature)
        if match is not None:
            return match
    return _match_boilerplate(lowered, stripped)


def is_provider_refusal_text(text: str) -> bool:
    """这段正文是不是供应商拒绝页——**唯一**可以拿去改变行为的判据"""
    return detect_provider_refusal(text) is not None


def classify_response_refusal(text: str) -> ResponseRefusalKind:
    """把一段正文归到三档拒答分类（见 `ResponseRefusalKind`）

    `IN_CHARACTER_REFUSAL` 是**观测用**的：它告诉日志「这条命中了拒答关键词，但我们判定它
    是模型自己的输出、已经放行」。调用方要拦截时只许看 `PROVIDER_REFUSAL`，
    把它和 `IN_CHARACTER_REFUSAL` 一起拦等于回到「单关键词判据」，在线上数据里会 100% 误杀。
    """
    if detect_provider_refusal(text) is not None:
        return ResponseRefusalKind.PROVIDER_REFUSAL
    if not text:
        return ResponseRefusalKind.NORMAL
    lowered = text.lower()
    if any(token in lowered for token in _IN_CHARACTER_REFUSAL_TOKENS):
        return ResponseRefusalKind.IN_CHARACTER_REFUSAL
    return ResponseRefusalKind.NORMAL


__all__ = [
    "MAX_REFUSAL_TEXT_CHARS",
    "MAX_RESIDUAL_CHARS",
    "PROVIDER_BOILERPLATE",
    "PROVIDER_REFUSAL_SIGNATURES",
    "ProviderBoilerplate",
    "ProviderRefusalMatch",
    "ProviderRefusalSignature",
    "ResponseRefusalKind",
    "classify_response_refusal",
    "detect_provider_refusal",
    "has_agent_markup",
    "has_code_features",
    "has_conversational_markers",
    "is_provider_refusal_text",
    "normalize_for_match",
]
