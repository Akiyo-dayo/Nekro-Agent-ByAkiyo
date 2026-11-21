import datetime
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from nekro_agent.core.config import CoreConfig, ModelConfigGroup
from nekro_agent.core.logger import get_sub_logger
from nekro_agent.core.os_env import PROMPT_ERROR_LOG_DIR, PROMPT_LOG_DIR
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.services.affection.evaluator import schedule_affection_evaluation
from nekro_agent.services.affection.prompt import render_affection_prompt
from nekro_agent.services.affection.suppression import lift_narrative_suppression, suppress_narrative
from nekro_agent.services.agent.provider_refusal import (
    detect_provider_refusal,
    normalize_for_match,
)
from nekro_agent.services.agent.session_token import attach_untrusted_result, register_session_token
from nekro_agent.services.memory.round_recall import commit_round_recall, discard_round_recall
from nekro_agent.services.model_group_service import enforce_channel_model_group_scope
from nekro_agent.services.plugin.call_priority import build_plugin_call_priority_rules
from nekro_agent.services.plugin.prompt_activation import build_plugin_activation_rules
from nekro_agent.services.plugin.scope import instance_scope, resolve_active_plugins
from nekro_agent.services.sandbox.runner import build_container_key, limited_run_code
from nekro_agent.services.usage import (
    LLMUsageContext,
    LLMUsageScene,
    resolve_model_group_name,
    usage_scope,
)

from .creator import OpenAIChatMessage
from .model_capability import (
    LLMFailureKind,
    classify_llm_failure,
    has_image_segments,
    note_capability_ok,
    note_capability_unsupported,
    strip_image_segments,
    supports_vision,
)
from .openai import EmptyLLMResponseError, OpenAIResponse, gen_openai_chat_response
from .resolver import ParsedCodeRunData, parse_chat_response
from .templates.compiler import PromptCompiler
from .templates.history import render_history_data
from .templates.plugin import render_plugins_prompt
from .upstream_redaction import collect_llm_secret_values, redact_upstream_text

# 使用deque保存最近100条错误日志路径

logger = get_sub_logger("agent_runtime")
RECENT_ERR_LOGS = deque(maxlen=100)

# 模型名会被拿去当文件名（错误落盘日志），而 `openai/gpt-4o` 这类带斜杠的模型名会让
# `save_log` 顺手 mkdir 出一层子目录，把错误日志散到各处
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _summarize_runtime_text(text: str, limit: int = 160) -> str:
    compact = " ".join(text.strip().split())
    if not compact:
        return ""
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


class AllLLMRequestsFailedError(ValueError):
    """All LLM API retries are exhausted for a single agent request.

    `kinds` 带上每一次尝试的归类，让调用方能给出**可辨识的**用户回执：
    「一个字都没返回」和「网络打不通」对用户而言不是同一件事。
    """

    def __init__(self, message: str = "所有 LLM 请求失败", *, kinds: Optional[List[LLMFailureKind]] = None):
        super().__init__(message)
        self.kinds: List[LLMFailureKind] = list(kinds or [])


class AgentRoundUnfinishedError(RuntimeError):
    """迭代次数耗尽，这一轮始终没有以 NORMAL 收尾

    改造前这条路径是**彻底静默**的：`run_agent` 的 for 循环自然结束、函数返回 None，
    既不抛异常、也不打 ERROR、聊天里也没有任何回执。表现就是「机器人偶尔就是不理人」。
    """


class AgentNoExecutableCodeError(RuntimeError):
    """连续多次都没能从模型回复里取出可执行代码，提前收尾

    与 `AgentRoundUnfinishedError` **必须分开**，因为处置完全不同：这条是「模型压根没在写代码」
    （安全拒答、纯散文回复、围栏坏掉），再迭代几次也只是把同一段散文重说一遍；
    而且这条路径**没有进过沙盒**，用户那边不存在「做到一半」的副作用。

    不继承 `AgentRoundUnfinishedError`：`message_service` 的分派是 isinstance 链，
    继承会让先命中的分支吃掉后面的分支，等于白分类。
    """

    def __init__(self, message: str = "模型回复中没有可执行代码", *, attempts: int = 0):
        super().__init__(message)
        self.attempts: int = attempts


class AgentProviderRefusalError(RuntimeError):
    """上游/渠道把「拒绝受理」当成**普通正文**返回，本轮零重试收尾

    与 `AgentNoExecutableCodeError` **必须分开**，尽管两条路径都是「回复里没有可执行代码」：

    - `AgentNoExecutableCodeError` 是**模型**没在写代码（格式打滑、模型自己的拒答话术）。
      格式打滑重出一次通常就好，所以那条给了 `CODE_MISSING_MAX_ATTEMPTS` 次机会。
    - 这一条是**渠道/供应商**根本没把请求交给模型。触发安全策略的是**上下文里那条消息**，
      它不会因为再问一次而消失：实测同一上下文连续拒答直到烧满迭代，每一轮逐字相同。
      那次「再给一次机会」的重试是 100% 注定失败的，只是白烧一次 token 和一次往返。

    `marker` 记的是**我们自己表里**命中的那条标志串（不是上游原文），
    用来在日志里回答「是靠哪一条判出来的」，同时不把供应商的正文带进日志。
    """

    def __init__(self, message: str = "上游拒绝受理本次请求", *, marker: str = ""):
        super().__init__(message)
        self.marker: str = marker


class AgentRoundStalledError(RuntimeError):
    """同一个错误签名连续复现，判定「重试不会有进展」，提前收尾

    线上真实分布里占大头的失败都是**确定性**的（QQ 风控禁言、embedding 模型组 API Key 是空串、
    插件没配地址、模型调用签名写错），它们每一次迭代都返回逐字相同的错误，
    却照样把迭代次数烧满——最长的一次烧了 8 轮。
    """

    def __init__(self, message: str = "Agent 迭代无进展", *, signature: str = "", repeats: int = 0):
        super().__init__(message)
        self.signature: str = signature
        self.repeats: int = repeats


# 「回复里没有可执行代码」最多给几次机会（含第一次）。格式打滑重出一次通常就好；
# 安全拒答重出多少次都是同一份拒答，所以这个上限**不跟着** `AI_SCRIPT_MAX_RETRY_TIMES` 走。
#
# 这两个常量现在只是**默认值**：真正生效的取值由 `SESSION_CODE_MISSING_MAX_ATTEMPTS` /
# `SESSION_STALL_REPEAT_THRESHOLD` 经会话覆盖层解析出来（见 `resolve_code_missing_limit`
# / `resolve_stall_threshold`）。默认值与配置项默认值必须一致，`tests/` 里有一条用例把
# 两处钉在一起——它们是「同一个东西的两份清单」，本仓已经因为这类事故栽过。
CODE_MISSING_MAX_ATTEMPTS = 2

# 同一个归一化错误签名连续出现几次判定为「无进展」。
# 2 = 「第一次报错给一次修的机会，修完还是一模一样的错就别再烧了」。
# 下限就是 2：取 1 等于第一次报错就放弃，把迭代重试机制整个废掉。
STALL_REPEAT_THRESHOLD = 2


def _normalize_early_exit_limit(value: object, *, default: int) -> int:
    """把配置里的原始取值折成「实际生效的上限」

    两条提前收尾判据共用同一套语义，所以归一化只写一份：

    - `<= 0` → **0，表示关掉这条判据**。这是运维的止血开关：判据误伤线上时不必改代码重部署，
      关掉就退回「烧满 `AI_SCRIPT_MAX_RETRY_TIMES` 次」的老行为。
    - `1` → 抬到 2。取 1 等于「第一次就放弃」，把重试机制整个废掉；而 0 已经有了明确语义
      （关闭），所以 1 只能是误配，静默抬到下限比让它生效安全。
    - 其余原样。

    非整数取值（覆盖层里存的是 JSON，历史文件里可能是字符串）折不动就当成默认值，
    而不是让整轮对话因为一条配置炸掉——与 `resolve_fallback_model_group` 是同一条教训。

    **`default` 必须由调用方显式给出，不许在这里写死某一条判据的常量。** 两条判据的默认值
    今天**恰好都是 2**，所以写死 `STALL_REPEAT_THRESHOLD` 在当下与正确实现完全等价、
    任何用例都分辨不出来（复核时的变异 N4 因此存活）；可 `CODE_MISSING_MAX_ATTEMPTS`
    的注释里白纸黑字写着它「**不跟着** `AI_SCRIPT_MAX_RETRY_TIMES` 走」，两者本来就是
    各自独立演化的量。一旦谁先动了自己那份默认值，「无可执行代码」这条判据就会在遇到
    脏配置时静默回落到**另一条判据**的默认值上——正是本仓反复栽的那种「两份清单只改一份」。
    """
    try:
        limit = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if limit <= 0:
        return 0
    return max(2, limit)


def resolve_stall_threshold(config: CoreConfig) -> int:
    """本轮真正生效的「无进展」阈值（0 = 运维已关闭该判据）"""
    return _normalize_early_exit_limit(config.SESSION_STALL_REPEAT_THRESHOLD, default=STALL_REPEAT_THRESHOLD)


def resolve_code_missing_limit(config: CoreConfig) -> int:
    """本轮真正生效的「连续没有可执行代码」上限（0 = 运维已关闭该判据）

    关掉的只是**提前收尾**这一步。「没有可执行代码就不进沙盒」是安全闸门不是启发式，
    任何取值下都照常生效——否则关掉阈值就等于把整段自然语言重新送进容器执行。
    """
    return _normalize_early_exit_limit(config.SESSION_CODE_MISSING_MAX_ATTEMPTS, default=CODE_MISSING_MAX_ATTEMPTS)

# 签名截断长度。签名会进日志，太长的 traceback 尾巴对判等没有增量信息
_SIGNATURE_MAX_LEN = 200

# 只有「失败形态」的收尾才计入无进展判据。
# `AGENT` / `MULTIMODAL_AGENT` 是**有意**的续跑点：插件方法把结果回喂给模型，模型接着往下写。
# 健康流程里它们完全可能连着返回同一段文本（`kb_search` 两次不同的查询都返回「未找到相关内容」、
# `memory_query` 连着两次空结果），把它们计入等于把正常对话拦腰砍断。
# 本轮证据里烧满迭代的那几类（发消息被风控、embedding Key 为空、插件没配、调用签名错）
# 全部是 ERROR 形态，排除续跑点不损失任何检出。
#
# `MANUAL`（沙盒 exit code 9）**同属续跑点，因此同样排除**：它不是故障，而是运行时契约里
# 白纸黑字给模型的调试手段——`templates/j2/runtime_contract.j2:200`
# 「Use print(something_you_want_to_inspect) + exit(9) for debugging if needed」。
# 模型 print 一段状态再 exit(9)，本就指望下一轮拿着回喂的输出继续写，与 `AGENT` 一模一样。
# 把它计入的代价是真实的：调试输出的**最后一行**很容易两轮相同（固定的分隔线、固定的表头、
# 同一个对象的 repr），而签名只取最后一行（`extract_error_head`），于是一次正常的
# 「先看看再动手」会在第二轮被判成无进展、直接掐断。
# 收益则是零：本轮 7 天的证据里烧满迭代的样本**没有一条**是 MANUAL。
#
# ## `SECURITY` 与零输出 `TIMEOUT` 的签名是**结构性退化**的，这是有意保留的
#
# `SECURITY` 分支根本不进沙盒（`one_time_code in code_content` 即就地判定），`sandbox_output`
# 恒为空串，于是签名**永远**是 `SECURITY|<empty>`：触发提前收尾的不是「同一个错重复」，
# 而是**任意两次连续的 one-time-code 泄漏**；容器一个字都没打就被超时杀掉时 `TIMEOUT` 同理。
# 复核时评估过给这两档更有区分度的签名，结论是不改：
#
# 1. **「带上被拦下的原因」拿不到区分度**：`SECURITY` 只有一个触发原因（泄漏了一次性令牌），
#    把它写进签名等于给所有 SECURITY 换一个更长的同名标签。
# 2. **真正的区分度只能来自模型那段代码，而那段代码里就有 one-time-code 本身**。签名会进
#    `logger.error`、进 `AgentRoundStalledError.signature`，也就是进 `app.log` / 内存日志环 /
#    `/logs` API。为了省一次重试，把本判据存在的意义（保护这枚令牌）反手泄进运维日志，
#    方向是反的。不可逆摘要（哈希）能绕开泄漏，但见第 3 条。
# 3. **区分开之后的行为也不对**：模型换一段代码继续泄漏，失败形态没有变（它没听那条反馈），
#    照样是「重试不会有进展」。按代码指纹区分几乎等于把 `SECURITY` 移出本集合，
#    把 #60 前半段刚省下来的迭代又烧回去。
# 4. **代价有界，而且现在有开关了**：默认 `AI_SCRIPT_MAX_RETRY_TIMES=3` + 阈值 2，最坏只少一次
#    重试；真被误伤的部署可以直接调高 `SESSION_STALL_REPEAT_THRESHOLD` 或取 0 关掉，
#    不必再改代码重部署——这条正是「不改签名」的最后一块拼图。
STALL_TRACKED_STOP_TYPES = frozenset(
    {
        ExecStopType.ERROR,
        ExecStopType.TIMEOUT,
        ExecStopType.SECURITY,
    },
)

# 模型没给代码块时的针对性反馈。与通用的「保持格式重试」不同：这条要明确告诉模型
# **什么都没执行**，并且拒答也必须写进代码块里发出去，否则用户那边仍然是一片静默。
CODE_MISSING_FEEDBACK = (
    "[System Automatic Detection] No executable python code block was found in your last reply, "
    "so NOTHING was executed and the user received NOTHING. "
    "You MUST reply with exactly one ```python ... ``` code block and put every action inside it. "
    "Do NOT reply with plain prose, an apology, or a refusal notice outside the code block. "
    "If you really cannot do what was asked, say so **inside** the code block by calling the "
    "message sending method."
)

# 归一化用的易变片段。顺序有意义：UUID / 时间戳要在裸数字之前吃掉，否则会被拆成一堆 <N>
_SIG_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_SIG_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_SIG_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b")
_SIG_HEX_ADDR_RE = re.compile(r"\b0[xX][0-9a-fA-F]+\b")
# 至少两段才算路径，免得把 `and/or` 这类普通文本当成路径吃掉
_SIG_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[/\\][\w.+-]+){2,}")
_SIG_NUMBER_RE = re.compile(r"\d+")

# traceback 的帧行：文件名与行号每次都可能变，而它们对「是不是同一个错」没有贡献
_SIG_FRAME_LINE_RE = re.compile(r'^\s*File "[^"]*", line \d+(?:, in .+)?$')
# 形如 `Exception: xxx` / `SyntaxError: xxx` 的异常尾行
_SIG_EXCEPTION_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*\s*:\s*\S")


def extract_error_head(sandbox_output: str) -> str:
    """从沙盒输出里取出「这次到底是什么错」的那一行

    沙盒输出是容器 stdout+stderr 的拼接：前面可能有模型自己 print 的任意内容，
    后面才是 traceback。判据是「从后往前找第一条像异常尾行的」，找不到就退回最后一条非空行
    （插件直接把错误文案打出来、或超时兜底那行注释都是这种形态）。

    帧行**先整条剔掉**再找：`File "/app/run_script.py", line 14, in <module>` 里的行号会随
    模型每次重写代码而变，留着它签名永远不可能重复，无进展检测就永远不触发。这道过滤对
    「有完整异常尾行」的输出是多余的，真正吃紧的是**退回最后一行**那条兜底路径——超时收尾会
    `container.kill()`，日志可能就断在 traceback 中间，最后一行正好是帧行。
    """
    lines = [
        line.rstrip()
        for line in sandbox_output.splitlines()
        if line.strip() and not _SIG_FRAME_LINE_RE.match(line)
    ]
    if not lines:
        return ""
    for line in reversed(lines):
        stripped = line.strip()
        if _SIG_EXCEPTION_LINE_RE.match(stripped):
            return stripped
    return lines[-1].strip()


def normalize_error_signature(stop_type: ExecStopType, sandbox_output: str) -> str:
    """把一次非 NORMAL 收尾折成一个可比较的错误签名

    归一化掉的是**每次必然不同、却与「是不是同一个错」无关**的东西：行号、路径、时间戳、
    对象地址、以及裸数字（线上样本 `生成嵌入向量失败，已重试 3 次: Illegal header value b'Bearer '`
    里的重试次数每轮都在涨，不吃掉它两轮就永远不同签名）。

    裸数字一律吃掉是有代价的：`KeyError: 3` 与 `KeyError: 5` 会被判成同一个签名。
    这个代价是可接受的——签名只用来回答「再重试一次有没有机会」，而此刻这一轮**已经在失败中**，
    最坏后果是少烧一次注定失败的迭代。反过来放宽（保留数字）的后果要严重得多：
    线上占比最大的那几类确定性故障会全部漏判，回到烧满迭代的老样子。

    Args:
        stop_type: 本次沙盒收尾类型
        sandbox_output: 沙盒输出原文

    Returns:
        形如 `ERROR|Exception: Error sending text message to chat: ...` 的签名字符串
    """
    head = extract_error_head(sandbox_output)
    head = _SIG_UUID_RE.sub("<UUID>", head)
    head = _SIG_TIMESTAMP_RE.sub("<TS>", head)
    head = _SIG_CLOCK_RE.sub("<TS>", head)
    head = _SIG_HEX_ADDR_RE.sub("<ADDR>", head)
    head = _SIG_PATH_RE.sub("<PATH>", head)
    head = _SIG_NUMBER_RE.sub("<N>", head)
    head = " ".join(head.split())
    if len(head) > _SIGNATURE_MAX_LEN:
        head = head[:_SIGNATURE_MAX_LEN]
    return f"{stop_type.name}|{head or '<empty>'}"


class RoundStallDetector:
    """「这一轮是不是在原地打转」的判据

    单独成类而不是内联进循环体，是为了让判据本身可被表驱动地钉住：
    内联的话「连续」被改成「累计」、阈值被改成 1 这类变异，只靠跑 `run_agent` 很难稳定复现。

    只认**连续**相同：中间夹了一个不同的错说明模型确实改了点什么，计数从头开始。
    """

    def __init__(self, threshold: int = STALL_REPEAT_THRESHOLD) -> None:
        # 归一化与配置解析共用同一份语义（`<=0` 关闭、1 抬到 2），不在这里另写一套判定
        normalized = _normalize_early_exit_limit(threshold, default=STALL_REPEAT_THRESHOLD)
        # 关闭态仍然照常记账（`signature` / `repeats` 是诊断信息），只是永远不判 stalled
        self.enabled: bool = normalized > 0
        self.threshold: int = normalized
        self.signature: str = ""
        self.repeats: int = 0
        self._seen: bool = False

    def note(self, signature: str) -> int:
        """记一次非 NORMAL 收尾，返回该签名当前的**连续**出现次数"""
        if self._seen and signature == self.signature:
            self.repeats += 1
        else:
            self._seen = True
            self.signature = signature
            self.repeats = 1
        return self.repeats

    @property
    def stalled(self) -> bool:
        return self.enabled and self._seen and self.repeats >= self.threshold


def is_code_missing(parsed: ParsedCodeRunData) -> bool:
    """本轮解析结果是不是「没能提取出可执行代码」

    `code_missing` 由解析器一组在同一轮里加到 `ParsedCodeRunData` 上。落地期间这里用过
    `getattr(parsed, "code_missing", False)`，好让两组的合并顺序互不阻塞；两边都合并完成后
    该默认值就**永远取不到**了（字段是 `ParsedCodeRunData` 上带默认值的声明字段），
    留着它只会让「默认值被改成 True」这类改动无法被任何用例察觉——变异测试实测：
    把默认值改成 `True`，定向用例 632 条全绿。所以改回直接取属性，把这条死分支去掉。
    """
    return bool(parsed.code_missing)


# 上游拒答的**第二种形态**：渠道不返回空正文 + `finish_reason='prohibited_content'`
# （那一种由 `LLMContentBlockedError` / `LLMFailureKind.CONTENT_BLOCKED` 接住），
# 而是把一整页「拒绝受理」的说明**当成普通正文塞进 `content`**。正文非空，于是它绕过了
# 所有空正文闸门，一路走到解析器；`ast.parse` 闸门能挡住它「被执行」，却挡不住它
# ①白烧一次重试 ②作为 assistant 消息进入下一轮上下文。
#
# 判据必须**窄**：一旦吹宽，模型自己的中文拒答（「抱歉，我无法协助处理这个请求。」）
# 会被误判成供应商拒答，而这两类的正确处置**完全相反**——模型拒答重出一次常常就好
# （`CODE_MISSING_MAX_ATTEMPTS` 给的正是那次机会），供应商拒答重出多少次都是同一页。
#
# **判定本身已经不在这里了**。这张表曾经既是判据、又是日志标签，两件事合在一起的代价被
# 对抗式复核实测出来了：把它当判据跑仓库里 11473 条真实字符串字面量 + 56 条定向反例，
# 误杀 14 + 8 条，全部是**「模型引用了这一页」**那一类——
#
#     "唔…刚才上游提示 The prompt could not be submitted，前辈换个说法再问我一次好不好？"
#
# 拒答原文正是被回喂进上下文的东西，模型下一轮引用它是**可预期**的，不是假想形态。
# 单条整句标志串的「或」判不出「谁在说这句话」，而这是个角色扮演 Bot：判错的表现是
# 整轮被静默掐掉，比原 bug 更难被发现。
#
# 判定现已整体交给 `services/agent/provider_refusal.py`：那边是多重合取
# （域名/整句锚点 + 短语组 + 占满整段），并且带三层只会造成漏杀的放行闸门
# （代码特征 / at 标记 / 口语人设标记）。同一份语料上误杀 0。
#
# 这张表**只剩一个用途**：给日志一个「是靠哪一条判出来的」的人类可读标签。
# 它不再决定任何行为，往里加一条不会放宽判据（`match_provider_refusal` 先过判定再取标签）。
PROVIDER_REFUSAL_MARKER_GROUPS: Tuple[Tuple[str, ...], ...] = (
    # 以下四条取自用户 gemini-3.7-flash-high 部署的**逐字原文**（本机复现一致）：
    # "The prompt could not be submitted. The prompt contains sensitive words that violate
    #  Google's [Generative AI Prohibited Use policy](https://policies.google.com/terms/...)"
    ("the prompt could not be submitted",),
    ("the prompt contains sensitive words that violate",),
    ("generative ai prohibited use policy",),
    ("policies.google.com/terms/generative-ai",),
    # 以下两条是**同构的兄弟形态**（其他渠道把拒绝页当正文返回），非本次现场取证。
    # 收进来的理由是后果完全一致，且这两句都是供应商的固定boilerplate、
    # 不可能出现在正常的机器人回复里；判断依据仍然是「整句」而不是关键词。
    ("your request was rejected as a result of our safety system",),
    ("azure openai", "content management policy"),
)


def _normalize_for_marker_match(text: str) -> str:
    """折成「小写 + 空白归一」的单行，再去比标志串

    换行归一是必需的：渠道返回的说明常常按宽度折行，一条标志串很容易被
    `\\n` 从中间劈开，逐字 `in` 就再也匹配不上。

    与 `provider_refusal.normalize_for_match` 是同一件事，直接转调过去——两份同功能实现
    各自演化正是本次复核发现的问题的来源，这里不再留第二份。
    """
    return normalize_for_match(text)


def match_provider_refusal(text: str) -> str:
    """命中哪一条供应商拒答标志？没有则返回空串

    **判定**由 `provider_refusal.detect_provider_refusal` 做（多重合取 + 三层放行闸门）；
    本函数只负责把结论翻译成一个可以进日志的标签。

    返回**我们自己表里**的那条标志串而不是上游原文：调用方要把它写进日志回答
    「是靠哪一条判出来的」，而上游正文既可能很长、也可能带供应商内部信息，
    不该因为一次拒答就整段落进运维日志。
    """
    match = detect_provider_refusal(text)
    if match is None:
        return ""
    normalized = _normalize_for_marker_match(text)
    for group in PROVIDER_REFUSAL_MARKER_GROUPS:
        if all(marker in normalized for marker in group):
            return " + ".join(group)
    # 判定命中了、但标签表里没有对应条目（例如中文本地化的拒绝页）。退回判定方自己给的
    # 短语——那些同样来自我们的表，不是上游正文的切片，进日志是安全的。
    return " + ".join(match.matched_phrases) or match.provider


def is_provider_refusal(text: str) -> bool:
    """这段正文是不是渠道/供应商的「拒绝受理」说明（而不是模型写的东西）"""
    return bool(match_provider_refusal(text))


async def run_agent(
    chat_key: str,
    chat_message: Optional[ChatMessage] = None,
    ctx: Optional[AgentCtx] = None,
):
    # 获取当前聊天频道的有效配置
    # 一次性会话令牌用于标记提示词中的可信边界，外部内容无法伪造。
    # 4 字节仅 32 bit，对可批量试探的场景偏弱，提升到 8 字节。
    one_time_code = os.urandom(8).hex()
    db_chat_channel: DBChatChannel
    logger.debug(f"[run_agent] {chat_key} | 开始准备上下文")
    if ctx:
        if ctx.db_chat_channel:
            db_chat_channel = ctx.db_chat_channel
        else:
            db_chat_channel = await DBChatChannel.get(chat_key=chat_key)
            ctx = AgentCtx.create_by_db_chat_channel(db_chat_channel=db_chat_channel)
    else:
        db_chat_channel = await DBChatChannel.get(chat_key=chat_key)
        ctx = AgentCtx.create_by_db_chat_channel(db_chat_channel=db_chat_channel)

    with instance_scope(db_chat_channel.instance_key):
        return await _run_agent_with_affection_suppression(
            chat_key=chat_key,
            chat_message=chat_message,
            ctx=ctx,
            db_chat_channel=db_chat_channel,
            one_time_code=one_time_code,
        )


def _round_was_blocked(error: BaseException) -> bool:
    """这一轮是不是以「内容审核拦截」或「供应商拒答」收尾

    内容审核拦截按尝试计：主模型组被拦、备用组又因为别的原因失败，被拦的那份上下文照样
    有嫌疑（好感描述就在系统提示词里），所以任一次尝试被判成 `CONTENT_BLOCKED` 就算。
    抑制只是暂时不注入两段描述，代价很小。

    这里的判据与 `failure_feedback.describe_llm_failure` 的「全部尝试都是 `CONTENT_BLOCKED`」
    不一样，是故意的：这里是为了不让同一段上下文再次触发拦截，那边是给用户看的失败说明。
    """
    if isinstance(error, AgentProviderRefusalError):
        return True
    return isinstance(error, AllLLMRequestsFailedError) and LLMFailureKind.CONTENT_BLOCKED in error.kinds


async def _run_agent_with_affection_suppression(
    chat_key: str,
    chat_message: Optional[ChatMessage],
    ctx: AgentCtx,
    db_chat_channel: DBChatChannel,
    one_time_code: str,
) -> None:
    """跑一轮，并按结果开关这个频道的好感描述抑制与记忆强化

    一轮以内容审核拦截 / 供应商拒答收尾：给频道记标记，抑制期内好感块只注入昵称与阶段，
    不注入两段描述——如果是描述里混进了会被拦的内容，下一轮就不会再被同一段描述锁住；
    同理，本轮注入的记忆不强化，下一轮也不注入记忆块。一轮正常收尾：清掉标记，并把本轮
    注入过的记忆一次性补记强化。其余失败（超时、无进展、没写代码……）与描述无关，不动标记，
    但同样不强化——只有正常收尾才算「这些记忆被用上了」。
    标记与召回记录都只在进程内，重启即失，见 `services/affection/suppression.py` 与
    `services/memory/round_recall.py`。
    """
    try:
        await _run_agent_in_instance_scope(
            chat_key=chat_key,
            chat_message=chat_message,
            ctx=ctx,
            db_chat_channel=db_chat_channel,
            one_time_code=one_time_code,
        )
    except (AllLLMRequestsFailedError, AgentProviderRefusalError) as error:
        if _round_was_blocked(error):
            suppress_narrative(chat_key)
            logger.info(
                f"[run_agent] {chat_key} | 本轮被内容审核拦下或被供应商拒答，好感描述暂不注入这个频道；"
                "本轮注入的记忆不强化，下一轮也不注入记忆块",
            )
        raise
    else:
        lift_narrative_suppression(chat_key)
        await commit_round_recall(one_time_code)
    finally:
        # 成功路径上已被 `commit` 弹出，这里是空操作；任何失败路径都在这里丢掉本轮记录。
        discard_round_recall(one_time_code)


async def _run_agent_in_instance_scope(
    chat_key: str,
    chat_message: Optional[ChatMessage],
    ctx: AgentCtx,
    db_chat_channel: DBChatChannel,
    one_time_code: str,
) -> None:
    # 登记本轮令牌，供插件的沙盒方法用 `ctx.one_time_code` 包裹外部内容。
    # 沙盒方法经 RPC 回调进来时会新建 AgentCtx，拿不到这里的局部变量，
    # 只能靠服务端按 container_key 存查——令牌绝不下发进容器，否则模型生成的代码能读到并伪造可信标签。
    register_session_token(build_container_key(chat_key), one_time_code)

    config = await db_chat_channel.get_effective_config()
    # 模型组归属的运行时兜底。接口层（`routers/config.py:_assert_tenant_may_write`）只管住
    # "从今往后"，而**存量覆盖文件里可能已经写着**越权的组名——补接口层挡不住已落盘的，
    # 而这一行下面立刻就要拿组名去取真实的 API Key。越界引用会被就地回落并打 WARNING，
    # 不抛异常：让 Bot 因为一条历史配置直接哑掉，对租户是不可诊断的故障。
    # 平台方实例与未启用多租户的部署在函数入口即返回，行为逐字不变。
    await enforce_channel_model_group_scope(
        config,
        instance_key=db_chat_channel.instance_key,
        adapter_key=db_chat_channel.adapter_key,
        chat_key=chat_key,
    )
    preset = await db_chat_channel.get_preset()
    adapter_dialog_examples = await ctx.adapter.set_dialog_example()
    adapter_jinja_env = await ctx.adapter.get_jinja_env()
    self_info = await ctx.adapter.get_self_info(chat_key)
    logger.debug(f"[run_agent] {chat_key} | 适配器信息就绪，开始渲染插件 prompt")

    # 获取当前使用的模型组
    used_model_group: ModelConfigGroup = config.MODEL_GROUPS[config.USE_MODEL_GROUP]
    # 按 会话 > 实例 > 全局 解析本轮真正可见的插件集合；单实例部署下等价于全局开关
    active_plugins = await resolve_active_plugins(instance_key=db_chat_channel.instance_key, chat_key=chat_key)
    # 激活策略控制器本身也受作用域约束：某个实例把它关掉时，本轮就该退回全量披露模式
    activation_enabled = any(plugin.module_name == "plugin_activation" for plugin in active_plugins)

    rendered_plugins = await render_plugins_prompt(
        active_plugins,
        ctx,
        activation_enabled=activation_enabled,
    )
    adapter_runtime_prompt = await ctx.adapter.render_runtime_prompt()
    runtime_prompts = [
        prompt for prompt in (adapter_runtime_prompt, rendered_plugins.runtime_prompt) if prompt.strip()
    ]
    runtime_prompt = "\n\n".join(runtime_prompts)
    logger.debug(f"[run_agent] {chat_key} | 插件 prompt 渲染完成，开始构建 system prompt")

    from nekro_agent.services.system_broadcast import AgentRuntimeStatusEvent, publish_system_event

    started_at = int(time.time() * 1000)
    iteration_total = config.AI_SCRIPT_MAX_RETRY_TIMES + 1
    llm_retry_total = config.AI_CHAT_LLM_API_MAX_RETRIES

    async def publish_runtime_state(
        *,
        phase: str,
        iteration_index: int,
        llm_retry_index: int = 1,
        llm_retry_attempts: Optional[int] = None,
        model_name: Optional[str] = None,
        sandbox_stop_type: Optional[int] = None,
        error_summary: Optional[str] = None,
    ) -> None:
        # 「能力不匹配」的修复会往尝试计划里补一次，本轮真实的尝试总数因此可能比
        # `AI_CHAT_LLM_API_MAX_RETRIES` 多 1。回调已经把真实总数传进来了，
        # 不用它的话后台会显示「重试 4/3」。
        effective_retry_total = llm_retry_attempts or llm_retry_total
        try:
            await publish_system_event(
                AgentRuntimeStatusEvent(
                    chat_key=chat_key,
                    active=True,
                    channel_name=db_chat_channel.channel_name,
                    chat_type=db_chat_channel.channel_type,
                    preset_id=getattr(preset, "id", None),
                    preset_name=getattr(preset, "name", None),
                    started_at=started_at,
                    updated_at=int(time.time() * 1000),
                    phase=phase,
                    iteration_index=iteration_index,
                    iteration_total=iteration_total,
                    llm_retry_index=llm_retry_index,
                    llm_retry_total=effective_retry_total,
                    sandbox_stop_type=sandbox_stop_type,
                    model_name=model_name,
                    error_summary=error_summary,
                )
            )
        except Exception as e:
            logger.debug(f"[run_agent] {chat_key} | 运行阶段广播失败: {e}")

    # 好感度：读路径 fail-open（失败返回空串），写路径只登记后台任务后立刻返回。
    # 两者都不会让主对话因为好感度而变慢或失败，详见 services/affection/。
    affection_prompt = await render_affection_prompt(
        db_chat_channel=db_chat_channel,
        one_time_code=one_time_code,
        config=config,
    )
    schedule_affection_evaluation(db_chat_channel, config)

    prompt_compiler = PromptCompiler(
        affection_prompt=affection_prompt,
        platform_name=self_info.platform_name,
        bot_platform_id=self_info.user_id,
        chat_preset=preset.content,
        plugins_prompt=rendered_plugins.system_prompt,
        plugins_runtime_prompt=runtime_prompt,
        plugin_activation_rules=build_plugin_activation_rules() if activation_enabled else "",
        plugin_call_priority_rules=build_plugin_call_priority_rules(),
        enable_cot=used_model_group.ENABLE_COT,
        chat_key_rules="\n".join(f"- {r}" for r in db_chat_channel.adapter.chat_key_rules),
        enable_at=db_chat_channel.adapter.config.SESSION_ENABLE_AT,
    )

    messages = [prompt_compiler.render_system_message()]
    messages.extend(prompt_compiler.render_practice_messages(adapter_dialog_examples, adapter_jinja_env))
    messages.append(
        await prompt_compiler.render_history_message(
            chat_key=chat_key,
            db_chat_channel=db_chat_channel,
            one_time_code=one_time_code,
            config=config,
            model_group=used_model_group,
        ),
    )

    logger.debug(f"[run_agent] {chat_key} | 历史记录渲染完成，发送 LLM 请求 (model={used_model_group.CHAT_MODEL})")
    history_render_until_time = time.time()
    llm_retry_errors: list[str] = []
    try:
        llm_response, used_model_group, llm_retry_errors = await send_agent_request(
            messages=messages,
            config=config,
            chat_key=chat_key,
            on_llm_retry=lambda retry_index, retry_total, model_name, error_summary: publish_runtime_state(
                phase="llm_retrying",
                iteration_index=1,
                llm_retry_index=retry_index,
                llm_retry_attempts=retry_total,
                model_name=model_name,
                error_summary=error_summary,
            ),
            on_llm_attempt=lambda retry_index, retry_total, model_name: publish_runtime_state(
                phase="llm_generating",
                iteration_index=1,
                llm_retry_index=retry_index,
                llm_retry_attempts=retry_total,
                model_name=model_name,
            ),
        )
    except AllLLMRequestsFailedError as e:
        await publish_runtime_state(
            phase="failed",
            iteration_index=1,
            llm_retry_index=llm_retry_total,
            error_summary=_summarize_runtime_text(str(e)),
        )
        raise
    async def raise_if_provider_refusal(
        parsed: ParsedCodeRunData,
        response: OpenAIResponse,
        *,
        iteration_index: int,
    ) -> None:
        """渠道把「拒绝受理」当正文返回 → 零重试、当场收尾

        ## 为什么挂在**解析之后**而不是循环体里

        改造前这段判定写在循环体的 `if is_code_missing(...)` 分支内。那个位置漏掉了
        **最后一次迭代取回来的那条响应**：循环体每一趟消费的是上一趟末尾请求回来的
        `parsed_code_data`，而最后一趟末尾那次请求的结果解析完就直接掉出循环，
        再也没有人看它一眼。实测（`max_retry=2`，脚本第 3 条喂逐字拒绝页）：
        `llm_calls == 3`、`AgentRoundUnfinishedError`——用户看到的是
        「已经试到最大迭代次数啦」，而真正发生的事是上游拒绝受理。
        那正是本轮要消灭的那句回执，从另一个入口又长了回来。

        `parse_chat_response` 在本函数里有**两处**调用（循环外的第一次请求、循环内的迭代
        请求），判定挂在每一次解析之后，两处就都被覆盖，也不会再有「第三处解析忘了接」的
        余地——新增解析点时不接这个判定，`code_missing` 的既有用例会立刻变红。

        ## 顺序上的两条硬约束（都没变）

        1. 在 `code_missing_streak += 1` **之前**：那次「再问一次」的机会 100% 注定失败，
           触发安全策略的是上下文里那条消息，它不会因为再问一次而消失。
        2. 在 `addition_prompt_message.append(... "assistant" ...)` **之前**：那句 append 是
           所有收尾形态共用的通用路径，在这里抛出，拒答原文一次都进不了下一轮上下文。
           挂到解析之后比原先更早，这两条只会更牢。
        """
        if not is_code_missing(parsed):
            return
        refusal_marker = match_provider_refusal(response.response_content)
        if not refusal_marker:
            return
        await publish_runtime_state(
            phase="failed",
            iteration_index=iteration_index,
            model_name=response.use_model,
            # 只报我们自己的结论，不把上游正文塞进后台事件
            error_summary="上游拒绝受理本次请求，零重试提前收尾",
        )
        logger.error(
            f"[run_agent] {chat_key} | 上游/渠道把拒绝受理当成正文返回，判定为供应商拒答，"
            # `max(0, ...)` 现在**够不到**：#58 让循环在最后一趟开头就 `break`，于是本函数的两个
            # 调用点传进来的 `iteration_index` 分别是 1（循环外那次）与 `i + 2 <= max_retry`
            # （循环内那次，`i + 1 < max_retry` 才走得到那一行），差值恒 >= 0。
            # 改造前不是这样：末轮那次无人消费的请求会带着 `max_retry + 1` 进来，剪不住就打出
            # 「剩余 -1 次」——剪裁是为那条路径加的，那条路径已经不存在了。
            # 保留它是因为本函数是个被**两处**手算下标喂的闭包，本仓在「第二个调用点」上栽过十次；
            # 它现在是等价变异（拿掉不会有任何用例变红），所以真正受测的是它成立的**前提**：
            # `tests/test_agent_refusal_tail_iteration.py::TestEveryParseSiteIsGuarded` 逐个位置
            # 断言日志里的剩余次数**恰好**等于 `max_retry - iteration_index`，任一调用点的下标
            # 算错都会红。别把那条断言退回成「不含负号」——那样它就什么都不测了。
            f"零重试提前收尾（剩余 {max(0, config.AI_SCRIPT_MAX_RETRY_TIMES - iteration_index)} 次迭代未使用）"
            f" ｜ 命中标志: {refusal_marker}",
        )
        raise AgentProviderRefusalError(
            "上游拒绝受理本次请求（渠道把拒绝说明当成正文返回）",
            marker=refusal_marker,
        )

    logger.debug(f"[run_agent] {chat_key} | LLM 请求完成，开始解析响应")
    parsed_code_data: ParsedCodeRunData = parse_chat_response(llm_response.response_content)
    await raise_if_provider_refusal(parsed_code_data, llm_response, iteration_index=1)

    # 本轮的收尾控制状态。两条判据都只在**连续**成立时才提前收尾。
    # 两个阈值走会话覆盖层（会话 > 实例 > 全局），取 0 即为运维把该判据整条关掉；
    # 默认取值与改造前的模块常量逐字相同，未配置的部署行为不变。
    stall_detector = RoundStallDetector(threshold=resolve_stall_threshold(config))
    code_missing_limit = resolve_code_missing_limit(config)
    code_missing_streak = 0

    for i in range(config.AI_SCRIPT_MAX_RETRY_TIMES):
        addition_prompt_message: List[OpenAIChatMessage] = []
        sandbox_output = ""
        raw_output = ""
        current_iteration = i + 1
        # None = 这一轮压根没进沙盒（回复里没有可执行代码）
        stop_type: Optional[ExecStopType] = None

        if is_code_missing(parsed_code_data):
            # 没能提取出可执行代码时**绝不进沙盒**：改造前这条路径会把整段自然语言
            # （典型是模型的安全拒答）原样送进容器执行，用户看到的是一条 SyntaxError。
            #
            # 供应商拒答的判定**不在这里**：它挂在每一次 `parse_chat_response` 之后
            # （见 `raise_if_provider_refusal`）。放在这里会漏掉最后一次迭代取回的那条响应
            # ——那条解析完就掉出循环，永远走不到本分支。
            code_missing_streak += 1
            if code_missing_limit and code_missing_streak >= code_missing_limit:
                await publish_runtime_state(
                    phase="failed",
                    iteration_index=current_iteration,
                    model_name=llm_response.use_model,
                    error_summary=f"连续 {code_missing_streak} 次回复中没有可执行代码块，提前放弃",
                )
                logger.error(
                    f"[run_agent] {chat_key} | 连续 {code_missing_streak} 次回复中都没有可执行代码块，"
                    f"未进入沙盒，提前放弃本轮（剩余 {config.AI_SCRIPT_MAX_RETRY_TIMES - current_iteration} 次迭代未使用）",
                )
                raise AgentNoExecutableCodeError(
                    f"模型连续 {code_missing_streak} 次未返回可执行代码块",
                    attempts=code_missing_streak,
                )
            logger.warning(
                f"[run_agent] {chat_key} | 第 {current_iteration} 次回复中没有可执行代码块，"
                f"跳过沙盒并要求模型重新输出（{code_missing_streak}/{code_missing_limit or '判据已关闭'}）",
            )
        else:
            code_missing_streak = 0
            if one_time_code in parsed_code_data.code_content:
                stop_type = ExecStopType.SECURITY
            else:
                await publish_runtime_state(
                    phase="sandbox_running",
                    iteration_index=current_iteration,
                    model_name=llm_response.use_model,
                )
                sandbox_output, raw_output, stop_type_value = await limited_run_code(
                    code_run_data=parsed_code_data,
                    from_chat_key=chat_key,
                    chat_message=chat_message,
                    llm_response=llm_response,
                    ctx=ctx,
                    llm_retry_errors=llm_retry_errors,
                )
                stop_type = ExecStopType(stop_type_value)

            if stop_type == ExecStopType.NORMAL:
                await publish_runtime_state(
                    phase="completed",
                    iteration_index=current_iteration,
                    model_name=llm_response.use_model,
                )
                return

            await publish_runtime_state(
                phase="sandbox_stopped",
                iteration_index=current_iteration,
                model_name=llm_response.use_model,
                sandbox_stop_type=stop_type.value,
                error_summary=_summarize_runtime_text(sandbox_output),
            )

            # 无进展检测：同一个归一化签名连续复现 = 再迭代也是同一份错误，别烧完剩下的次数。
            # 续跑型收尾（AGENT / MULTIMODAL_AGENT）整块跳过，既不记也不重置，
            # 理由见 `STALL_TRACKED_STOP_TYPES`。
            if stop_type in STALL_TRACKED_STOP_TYPES:
                signature = normalize_error_signature(stop_type, sandbox_output)
                repeats = stall_detector.note(signature)
                if stall_detector.stalled:
                    await publish_runtime_state(
                        phase="failed",
                        iteration_index=current_iteration,
                        model_name=llm_response.use_model,
                        sandbox_stop_type=stop_type.value,
                        error_summary=f"错误签名连续重复 {repeats} 次，提前放弃",
                    )
                    # 可诊断性不能因为提前收尾而减少：签名本身、重复次数、以及「还剩几次没用」
                    # 都要留在日志里，否则排查时无法区分「提前放弃」与「本来就只跑了两轮」
                    logger.error(
                        f"[run_agent] {chat_key} | 错误签名相同、已连续重复 {repeats} 次，判定重试无进展，"
                        f"提前放弃本轮（剩余 {config.AI_SCRIPT_MAX_RETRY_TIMES - current_iteration} 次迭代未使用）"
                        f" ｜ 签名: {signature}",
                    )
                    raise AgentRoundStalledError(
                        f"Agent 迭代无进展：同一错误签名连续出现 {repeats} 次",
                        signature=signature,
                        repeats=repeats,
                    )

        # 这一趟已经是最后一趟：**不要再为下一趟发请求了**
        #
        # 循环体每一趟消费的是**上一趟末尾**请求回来的 `parsed_code_data`。最后一趟末尾那次
        # 请求发完、解析完，循环就结束了，它的结果**无人消费**——改造前照样发，含全量上下文，
        # 实测单次请求体可达数百 KB，于是每个跑满 `AI_SCRIPT_MAX_RETRY_TIMES` 的回合都固定白花
        # 一次完整请求。这一行之后的所有装配（assistant 原文回填、沙盒输出回喂、
        # `render_history_data`）**只为喂那次请求而存在**，一并跳过。
        #
        # ⚠️ **`render_history_data` 远不止「一趟 DB 查询 + 模板渲染」**，它自己就带外部副作用，
        # 所以「跳过它」这件事必须单独交代清楚。它一路展开是 `_inject_memory_context`
        # → `_build_enhanced_memory_recall_plan` → **又一次 `gen_openai_chat_response`**
        # （`LLMUsageScene.HISTORY_PLAN`，走计费与用量记账）→ `retrieve_memories_batch`
        # （每条 query 一次 embedding + 一次 Qdrant 查询）→ `stage_round_recall`（只记录，
        # 不写库）。也就是说开了增强检索的部署，一个烧满的回合改造前浪费的是**两次** LLM
        # 请求（迭代请求 + 规划请求），不是一次。
        #
        # 跳过它们**不是回归而是修正**：那次渲染的结果从来没有进沙盒、没有被消费过，现在它
        # 也不再强化——没被消费的渲染本来就不该按「这条记忆被用上了」记账。跳过它省下的正是
        # 规划请求与检索这两段开销。
        # 反过来说，这也意味着 `break` 的**位置**是有含义的：往后挪一行就把这两次请求与那批
        # 检索全请回来，而请求计数一次都不会变。用例因此**单独数 `render_history_data`
        # 的调用次数**（`TestNoUnconsumedTailRequest::test_the_skipped_assembly_is_actually_skipped`），
        # 只数 `llm_calls` 的话这个变异会活着。
        #
        # 依赖关系逐条核对过，没有一条落在被跳过的那次请求上：
        # - **`llm_retry_errors` 落库**：唯一的落库点是下面 `limited_run_code(llm_retry_errors=...)`，
        #   用的是**产出本趟代码**的那次请求的错误列表（`schemas/sandbox.py` → `DBExecCode.extra_data`）。
        #   被跳过的这次请求本来就没有下一趟沙盒去消费它，改造前那份错误列表同样一个字都没落库。
        # - **`publish_runtime_state` 事件序列**：本趟的 `sandbox_running` / `sandbox_stopped` /
        #   早停 `failed` 全在这一行之前，一条不少。只少了「为下一趟发请求」的 `iterating` 与
        #   `llm_generating`——而下一趟根本不存在，前端进度条仍由循环外那条 `failed` 收尾。
        # - **`llm_response`**：保持为产出本趟代码的那次响应，循环外 `failed` 事件里的 `model_name`
        #   因此指向**真正执行过**的那个模型（改造前指向的是结果被丢弃那次的模型）。
        # - **供应商拒答判定**：`raise_if_provider_refusal` 挂在每一次 `parse_chat_response` 之后，
        #   而改造后每一次请求的响应都会被消费，不再有「解析完就掉出循环」的盲区。
        if current_iteration >= config.AI_SCRIPT_MAX_RETRY_TIMES:
            break

        # 添加 AI 回复的原始内容到上下文
        addition_prompt_message.append(OpenAIChatMessage.from_text("assistant", llm_response.response_content))

        msg: OpenAIChatMessage = OpenAIChatMessage.create_empty("user")  # 待添加到迭代上下文的用户消息

        # 没有可执行代码：给的是**针对格式**的反馈，而不是沙盒错误回喂
        if stop_type is None:
            msg = msg.extend(OpenAIChatMessage.from_text("user", CODE_MISSING_FEEDBACK))

        # Agent 类型的迭代对话
        if stop_type == ExecStopType.AGENT:
            msg = msg.extend(
                OpenAIChatMessage.from_text(
                    "user",
                    f"[Agent Method Response] {attach_untrusted_result(sandbox_output, build_container_key(chat_key), one_time_code)}\nPlease continue based on this agent response. Attention: the code after the agent method is NOT EXECUTED!",
                ),
            )

        # 多模态类型的迭代对话
        elif stop_type == ExecStopType.MULTIMODAL_AGENT:
            multimodal_agent_result = json.loads(raw_output.split("<AGENT_RESULT>")[1].split("</AGENT_RESULT>")[0])
            if isinstance(multimodal_agent_result, list):
                msg = msg.extend(OpenAIChatMessage("user", multimodal_agent_result))
            elif isinstance(multimodal_agent_result, str):
                msg = msg.extend(OpenAIChatMessage.from_text("user", multimodal_agent_result))
            elif isinstance(multimodal_agent_result, dict):
                msg = msg.extend(OpenAIChatMessage(**multimodal_agent_result))
            else:
                raise ValueError(f"Multimodal agent result is not a list or string: {multimodal_agent_result}")
            msg = msg.extend(OpenAIChatMessage.from_text("user", "Attention: the code AFTER THE AGENT METHOD is NOT EXECUTED!"))

        # 异常类型的迭代对话
        exception_reason_map: Dict[ExecStopType, str] = {
            ExecStopType.TIMEOUT: "Sandbox exited due to timeout",
            ExecStopType.ERROR: "Sandbox exited due to error occurred",
            ExecStopType.MANUAL: "Sandbox exited due to manual stop by you",
            ExecStopType.AGENT: "Sandbox exited due to agent method",
            ExecStopType.MULTIMODAL_AGENT: "Sandbox exited due to multimodal agent method",
        }

        # 异常处理建议
        exception_suggestion_map: Dict[str, str] = {
            "SyntaxError": "You are prohibited from adding anything other than the content of the code that might break the syntax of the code. Please ensure your output specification and try again",
        }

        new_message_notification = "During the generation and execution, the following messages were sent (You **CANT NOT** send any messages which you have sent before!):"

        if stop_type in exception_reason_map:
            for suggestion_key in exception_suggestion_map:
                if suggestion_key in sandbox_output:
                    suggestion_text = f"\nResolve Suggestion: {exception_suggestion_map[suggestion_key]}"
                    break
            else:
                suggestion_text = ""
            msg = msg.extend(
                OpenAIChatMessage.from_text(
                    "user",
                    f"[Sandbox Output] {sandbox_output}\n---\n{exception_reason_map[stop_type]}\n{suggestion_text}. {new_message_notification}",
                ),
            )

        # 安全类型的迭代对话
        if stop_type == ExecStopType.SECURITY:
            msg = msg.extend(
                OpenAIChatMessage.from_text(
                    "user",
                    f"\n\n[System Automatic Detection] Invalid response detected. You should not reveal the one-time code in your reply. This is just a tag to help you mark trustworthy information. please DO NOT give any extra explanation or apology and keep the response format for retry. {new_message_notification}",
                ),
            )

        # 为所有迭代对话添加新记录背景
        msg = msg.extend(
            await render_history_data(
                chat_key=chat_key,
                db_chat_channel=db_chat_channel,
                one_time_code=one_time_code,
                plugin_injected_prompt=runtime_prompt,
                record_sta_timestamp=history_render_until_time,
                model_group=used_model_group,
                config=config,
            ),
        )

        # 这条消息随**这次请求**发出，而这次请求的产物会在**下一趟**（第 `next_iteration` 趟）
        # 进沙盒执行。计数与「最后一次机会」因此都按 `next_iteration` 算：
        #
        # 改造前两者都按 `i + 1`（=刚刚失败的那一趟）算，于是「This is the last retry.
        # Describe the reason if you can't finish the task.」恰好落在**结果被丢弃的那次请求**上
        # ——模型写的那段说明一次都没被执行过，用户永远看不到。#58 把那次请求去掉之后，
        # 这句提示如果还按老口径算就彻底不会出现了，所以一并挪到真正会被执行的那一趟上。
        # 计数一起挪是为了不自相矛盾：`AI_SCRIPT_MAX_RETRY_TIMES=2` 时老口径会打出
        # 「This is the last retry ... (Iteration times: 1/2)」。
        next_iteration = current_iteration + 1
        msg = msg.extend(
            OpenAIChatMessage.from_text(
                "user",
                "\nplease DO NOT give any extra explanation or apology and keep the response format for retry."
                + (
                    f" This is the last retry. Describe the reason if you can't finish the task. (Iteration times: {next_iteration}/{config.AI_SCRIPT_MAX_RETRY_TIMES})"
                    if next_iteration >= config.AI_SCRIPT_MAX_RETRY_TIMES
                    else f"(Iteration times: {next_iteration}/{config.AI_SCRIPT_MAX_RETRY_TIMES})"
                ),
            ),
        )

        # 将迭代对话添加到上下文
        addition_prompt_message.append(msg.tidy())
        messages.extend(addition_prompt_message)

        await publish_runtime_state(
            phase="iterating",
            iteration_index=i + 2,
            model_name=llm_response.use_model,
            sandbox_stop_type=stop_type.value if stop_type is not None else None,
            error_summary=_summarize_runtime_text(sandbox_output),
        )

        history_render_until_time = time.time()
        try:
            llm_response, used_model_group, llm_retry_errors = await send_agent_request(
                messages=messages,
                config=config,
                is_debug_iteration=True,
                chat_key=chat_key,
                on_llm_retry=lambda retry_index, retry_total, model_name, error_summary, iteration_index=i + 2: publish_runtime_state(
                    phase="llm_retrying",
                    iteration_index=iteration_index,
                    llm_retry_index=retry_index,
                    llm_retry_attempts=retry_total,
                    model_name=model_name,
                    error_summary=error_summary,
                ),
                on_llm_attempt=lambda retry_index, retry_total, model_name, iteration_index=i + 2: publish_runtime_state(
                    phase="llm_generating",
                    iteration_index=iteration_index,
                    llm_retry_index=retry_index,
                    llm_retry_attempts=retry_total,
                    model_name=model_name,
                ),
            )
        except AllLLMRequestsFailedError as e:
            await publish_runtime_state(
                phase="failed",
                iteration_index=i + 2,
                llm_retry_index=llm_retry_total,
                error_summary=_summarize_runtime_text(str(e)),
            )
            raise
        parsed_code_data = parse_chat_response(llm_response.response_content)
        # 末轮取回的这条响应，循环体不会再消费它——判定必须在这里就做掉，
        # 否则「最后一次迭代撞上供应商拒答」会被误报成 `AgentRoundUnfinishedError`
        await raise_if_provider_refusal(parsed_code_data, llm_response, iteration_index=i + 2)

    # 迭代次数耗尽仍未以 NORMAL 收尾。改造前这里是函数自然返回 None —— 既不抛异常、
    # 也不打 ERROR、聊天里也没有任何回执，是 §3 登记的四条静默分支里最难发现的一条：
    # 现象只有「机器人偶尔就是不理人」，日志里连一行都对不上。
    await publish_runtime_state(
        phase="failed",
        iteration_index=iteration_total,
        model_name=llm_response.use_model,
        error_summary=f"迭代 {config.AI_SCRIPT_MAX_RETRY_TIMES} 次仍未正常收尾",
    )
    logger.error(
        f"[run_agent] {chat_key} | 迭代 {config.AI_SCRIPT_MAX_RETRY_TIMES} 次后仍未以 NORMAL 收尾，本轮放弃",
    )
    raise AgentRoundUnfinishedError(
        f"Agent 迭代 {config.AI_SCRIPT_MAX_RETRY_TIMES} 次后仍未正常收尾",
    )


def resolve_primary_model_group(config: CoreConfig, *, is_debug_iteration: bool) -> ModelConfigGroup:
    """解析本次尝试的主模型组（迭代对话走「调试/Agent 迁移模型组」）

    与 `resolve_fallback_model_group` 是同一条教训的另一半：`DEBUG_MIGRATION_MODEL_GROUP`
    的默认值就是 `"default"`（**不是空串**），所以「运维把主模型组改名/删掉了名为 default
    的那个组」这种极常见的状态下，第一轮请求照常成功，而**每一次迭代**（沙盒报错、Agent
    方法回喂、多模态回喂）都会在这一行抛一个裸 `KeyError: 'default'`。
    它与 fallback 那条同形、同因、同样不可诊断，只是发生得更晚一点。

    `USE_MODEL_GROUP` 那一支保持原样抛 KeyError：它是「这个部署压根没配好」，
    第一轮就会炸，不存在「跑着跑着才发现」的诊断问题。
    """
    if is_debug_iteration and config.DEBUG_MIGRATION_MODEL_GROUP:
        group = config.MODEL_GROUPS.get(config.DEBUG_MIGRATION_MODEL_GROUP)
        if group is not None:
            return group
        logger.warning(
            f"调试/Agent 迁移模型组 {config.DEBUG_MIGRATION_MODEL_GROUP} 不在模型组列表中，"
            f"本次迭代回退到主模型组 {config.USE_MODEL_GROUP}",
        )
    return config.MODEL_GROUPS[config.USE_MODEL_GROUP]


def resolve_fallback_model_group(config: CoreConfig, model_group: ModelConfigGroup) -> ModelConfigGroup:
    """解析备用模型组

    改造前这里是 `config.MODEL_GROUPS[config.FALLBACK_MODEL_GROUP]`：配置里写了一个不存在的
    组名时直接 KeyError，整轮对话连一条日志都没有就没了。名字对不上是运维改名/删组之后的
    常见状态，不该是不可诊断的崩溃。
    """
    name = config.FALLBACK_MODEL_GROUP
    if not name:
        return model_group
    group = config.MODEL_GROUPS.get(name)
    if group is None:
        logger.warning(f"备用模型组 {name} 不在模型组列表中，本轮回退到主模型组 {model_group.CHAT_MODEL}")
        return model_group
    return group


def build_attempt_plan(
    model_group: ModelConfigGroup,
    fallback_model_group: ModelConfigGroup,
    max_retries: int,
) -> List[ModelConfigGroup]:
    """本轮的尝试序列：与改造前逐字等价（前 N-1 次主模型组，最后一次备用模型组）

    单独抽出来是因为「能力不匹配」的修复会在运行中改写这个序列，而"改写前它长什么样"
    是一条必须能被单测钉住的契约。
    """
    if max_retries <= 0:
        return []
    if max_retries == 1:
        return [fallback_model_group]
    return [model_group] * (max_retries - 1) + [fallback_model_group]


def pick_vision_repair_group(
    failed: ModelConfigGroup,
    model_group: ModelConfigGroup,
    fallback_model_group: ModelConfigGroup,
) -> ModelConfigGroup:
    """能力不匹配之后，下一次该拿哪个模型组再试

    候选只在**本轮本来就会用到的那两个模型组**（主 + 备用）里挑，并且过滤掉「同一个模型」
    与「同样没声明视觉能力」的那些——现场那次故障里 `FALLBACK_MODEL_GROUP` 就配成了主模型组
    自己，三次重试等于把同一个必然失败的请求原样发了三遍。

    不去 `config.MODEL_GROUPS` 里翻别的组：那些组的归属、额度与人设适配都不由这条链路决定，
    随手换一个等于替运维做了一个他没做过的决定，多租户下还可能把平台方的组用到租户会话上。

    没有可用候选时返回失败的那个组本身——这不是「白试一次」：它已经被打上负判据，
    下一次尝试会自动剥图，请求形态与刚才那次完全不同。
    """
    failed_key = (failed.BASE_URL, failed.CHAT_MODEL)
    for candidate in (fallback_model_group, model_group):
        if (candidate.BASE_URL, candidate.CHAT_MODEL) == failed_key:
            continue
        if supports_vision(candidate):
            return candidate
    return failed


def _build_err_log_path(config: CoreConfig, model_name: str, attempt_index: int) -> Optional[str]:
    """每次尝试各自一个错误日志文件，且按**这次实际用的**模型命名

    改造前三次重试共写同一个文件、且文件名固定取主模型组的模型名：切到 fallback 时
    文件名指向的是另一个模型，排查时对不上号（现场两个组同名才没暴露）。

    ## 为什么这里也要看 `SAVE_PROMPTS_LOG`

    这个开关此前**只管住了成功那条路**（`_prepare_attempt_payload` 里的 `log_path`），
    失败路径无条件落盘。于是把它关成 `false` 的部署照样在写：测试部署上
    `logs/prompts/` 里 267 个文件**全部**是错误转储，`logs/prompts_error/` 707 MB /
    924 个文件，24 小时新增 261 MB —— 开关是 `false`。

    而这两个目录装的是「**全部租户的完整 LLM 提示词原文**」
    （`routers/console/space_cleanup.py` 的模块 docstring 逐字这么写）。所以这不只是
    磁盘浪费，是**运维已经明确关掉了留存、代码却还在留存**。

    **失败原因不受影响。** 它在 `services/agent/openai.py` 里由 `logger.error` 打进
    `logs/app/`（脱敏后的异常 + 堆栈），不在这个文件里；转储文件装的是 prompt 正文本身
    （实测单文件 765 KB，绝大部分是图片 base64）。要连正文一起留，把开关打开即可。

    `config` 走参数而不是取模块全局：本模块**没有**全局 `config`，
    它一路由 `send_agent_request(messages, config, ...)` 显式传进来。
    """
    if not config.SAVE_PROMPTS_LOG:
        return None
    safe_model = _UNSAFE_FILENAME_CHARS.sub("_", model_name) or "unknown"
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{PROMPT_ERROR_LOG_DIR}/chat_err_{safe_model}_{stamp}_try{attempt_index}.json"


def _prepare_attempt_payload(
    messages: List[OpenAIChatMessage],
    use_model_group: ModelConfigGroup,
    chat_key: str,
) -> Tuple[List[OpenAIChatMessage], bool]:
    """按**这次要用的**模型组的能力裁剪请求体，返回 (请求体, 请求体里是否真的带了图)

    这是全链路唯一的裁剪收口：历史图片、插件返回的多模态结果、以及将来任何新的注图点，
    只要它出现在最终的 messages 里就会被这里管住。`render_history_data` 那一层的判据保留，
    但它管的是「不要白白去读盘、压缩、转 base64」，不是最后一道闸门。
    """
    if not has_image_segments(messages):
        return list(messages), False
    if supports_vision(use_model_group):
        return list(messages), True
    trimmed, removed = strip_image_segments(messages)
    logger.info(
        f"[send_agent_request] {chat_key} | 模型 {use_model_group.CHAT_MODEL} 未声明视觉能力，"
        f"本次请求的 {removed} 处图片已替换为占位文本",
    )
    return trimmed, False


async def send_agent_request(
    messages: List[OpenAIChatMessage],
    config: CoreConfig,
    is_debug_iteration: bool = False,
    chat_key: str = "",
    on_llm_attempt: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
    on_llm_retry: Optional[Callable[[int, int, str, str], Awaitable[None]]] = None,
) -> Tuple[OpenAIResponse, ModelConfigGroup, list[str]]:
    model_group: ModelConfigGroup = resolve_primary_model_group(config, is_debug_iteration=is_debug_iteration)
    fallback_model_group: ModelConfigGroup = resolve_fallback_model_group(config, model_group)

    if config.SAVE_PROMPTS_LOG:
        log_path = f"{PROMPT_LOG_DIR}/chat_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    else:
        log_path = None

    attempt_plan: List[ModelConfigGroup] = build_attempt_plan(
        model_group,
        fallback_model_group,
        config.AI_CHAT_LLM_API_MAX_RETRIES,
    )
    used_model_group: ModelConfigGroup = model_group  # 记录实际使用的模型组
    retry_errors: list[str] = []
    failure_kinds: list[LLMFailureKind] = []
    # 「能力不匹配」不计入可重试：修复后的那一次额外补进计划，而不是占用原有重试预算。
    # 只补一次，避免上游文案抖动时把重试次数变成不设上限
    repair_budget = 1
    attempt_index = 0

    while attempt_index < len(attempt_plan):
        use_model_group: ModelConfigGroup = attempt_plan[attempt_index]
        retry_index = attempt_index + 1
        attempt_messages, payload_has_image = _prepare_attempt_payload(messages, use_model_group, chat_key)
        # 「这次用的是不是备用组」在下面的失败分支里本来就要算一次，提到这里来是因为
        # 记账也要它，而两处各算一遍就是同一判据的第二处实现
        is_fallback = fallback_model_group is not model_group and use_model_group is fallback_model_group
        # 归属声明：一次尝试一份。倍率随**这次真正用的**那个组走，切到 fallback 之后
        # 落账的就是 fallback 的倍率——两个组的单价往往差一个数量级，套用主组的等于算错账。
        attempt_usage_context = (
            LLMUsageContext(scene=LLMUsageScene.CHAT, chat_key=chat_key)
            .with_model_group(resolve_model_group_name(config.MODEL_GROUPS, use_model_group), use_model_group)
            .for_attempt(index=retry_index, total=len(attempt_plan), is_fallback=is_fallback)
        )
        if on_llm_attempt is not None:
            await on_llm_attempt(retry_index, len(attempt_plan), use_model_group.CHAT_MODEL)

        logger.info(
            f"[send_agent_request] {chat_key} | 发送 LLM 请求 model={use_model_group.CHAT_MODEL} "
            f"retry={attempt_index}/{len(attempt_plan)} images={'on' if payload_has_image else 'off'}"
        )
        err_log_path = _build_err_log_path(config, use_model_group.CHAT_MODEL, retry_index)
        try:
            with usage_scope(attempt_usage_context):
                llm_response: OpenAIResponse = await gen_openai_chat_response(
                    model=use_model_group.CHAT_MODEL,
                    messages=attempt_messages,
                    temperature=use_model_group.TEMPERATURE,
                    top_p=use_model_group.TOP_P,
                    top_k=use_model_group.TOP_K,
                    frequency_penalty=use_model_group.FREQUENCY_PENALTY,
                    presence_penalty=use_model_group.PRESENCE_PENALTY,
                    extra_body=use_model_group.EXTRA_BODY,
                    base_url=use_model_group.BASE_URL,
                    api_key=use_model_group.API_KEY,
                    stream_mode=config.AI_REQUEST_STREAM_MODE,
                    proxy_url=use_model_group.CHAT_PROXY,
                    max_wait_time=config.AI_GENERATE_TIMEOUT,
                    first_token_timeout=config.AI_STREAM_FIRST_TOKEN_TIMEOUT,
                    log_path=log_path,
                    error_log_path=err_log_path,
                )
            if not llm_response.response_content.strip():
                # 兜底闸门：`gen_openai_chat_response` 两条分支都已经自己判过空，这里挡的是
                # 「将来某条分支忘了判」——空正文一路走下去的表现是沙盒执行一段空脚本、
                # 以 NORMAL 收尾，Bot 一言不发且日志里没有任何 ERROR
                raise EmptyLLMResponseError("LLM 返回的正文为空，无可执行内容")  # noqa: TRY301
        except Exception as e:
            # 分类必须吃**原文**：判据里有「模态词 + 拒绝词」的合取，而脱敏会把命中段落换成占位符。
            # 顺序在这里是有意义的，别把下面那句 redact 的结果传进来。
            kind = classify_llm_failure(e, request_has_image=payload_has_image)
            failure_kinds.append(kind)
            # 这一份文本有三个去向，其中一个是**租户可见**的：
            #   - `error_summary` -> `AgentRuntimeStatusEvent` -> `/events` SSE（`Role.Admin`，租户订阅得到）
            #   - `retry_errors`  -> `DBExecCode.extra_data.llm_retry_errors`（落库，进数据库备份）
            #   - `logger.error`  -> `app.log` + 内存日志环 + `/logs` API + 运维 Bot 的错误摘要推送
            # 租户的会话跑在**平台方的**模型组上，上游一旦回显平台 Key，第一条就是一次
            # 绕过所有 HTTP 层掩码的直投。三个去向共用同一份已脱敏文本，不按出口分叉判据。
            safe_error = redact_upstream_text(
                str(e),
                collect_llm_secret_values(use_model_group.API_KEY, use_model_group.CHAT_PROXY),
            )
            error_summary = _summarize_runtime_text(safe_error)
            retry_errors.append(safe_error)
            logger.error(
                f"LLM 请求失败[{kind.value}]: {safe_error} ｜ 使用模型: {use_model_group.CHAT_MODEL} {'(fallback)' if is_fallback else ''}",
            )

            if kind is LLMFailureKind.VISION_UNSUPPORTED:
                note_capability_unsupported(use_model_group)
                repair_group = pick_vision_repair_group(use_model_group, model_group, fallback_model_group)
                if repair_group is use_model_group:
                    logger.warning(
                        f"[send_agent_request] {chat_key} | 模型 {use_model_group.CHAT_MODEL} 明确拒绝图片输入，"
                        f"且没有声明支持视觉的备选模型组，改为剥离图片后重试同一模型",
                    )
                else:
                    logger.warning(
                        f"[send_agent_request] {chat_key} | 模型 {use_model_group.CHAT_MODEL} 明确拒绝图片输入，"
                        f"切换到声明支持视觉的模型 {repair_group.CHAT_MODEL} 重试",
                    )
                next_group = attempt_plan[attempt_index + 1] if attempt_index + 1 < len(attempt_plan) else None
                if repair_budget > 0 and next_group is not repair_group:
                    attempt_plan.insert(attempt_index + 1, repair_group)
                    repair_budget -= 1

            if on_llm_retry is not None:
                await on_llm_retry(retry_index, len(attempt_plan), use_model_group.CHAT_MODEL, error_summary)
            # 只登记真的落了盘的那些：`/debug err-logs` 与运维 Bot 会按这些路径去读文件，
            # 而失败并不总是发生在 `gen_openai_chat_response` 内部（例如上面那道空正文闸门）。
            # `err_log_path` 为 None 表示 `SAVE_PROMPTS_LOG` 关着、这一趟根本没打算落盘，
            # 直接跳过——`Path(None)` 会 TypeError，把一次上游失败变成一次本地崩溃
            if err_log_path is not None:
                err_log_path_obj = Path(err_log_path)
                if err_log_path_obj.exists() and not any(
                    str(recorded.absolute()) == str(err_log_path_obj.absolute()) for recorded in RECENT_ERR_LOGS
                ):
                    RECENT_ERR_LOGS.append(err_log_path_obj)
            attempt_index += 1
            continue
        else:
            used_model_group = use_model_group  # 记录成功使用的模型组
            if payload_has_image:
                # 带图请求成功 → 立刻作废可能存在的负判据，避免一次误判把模型永久钉成瞎子
                note_capability_ok(use_model_group)
            break
    else:
        # 全部尝试都失败时的整份 prompt 转储。同样受 `SAVE_PROMPTS_LOG` 约束——
        # 它写的是 prompt 正文（`messages` 全文，含图片 base64），与逐次尝试的错误转储
        # 是同一类数据，只是落在 `PROMPT_LOG_DIR` 而不是 `PROMPT_ERROR_LOG_DIR`。
        # 测试部署上 `logs/prompts/` 里 267 个文件全部由这一行产生，而开关是 `false`。
        # 失败本身不会被吞掉：下面那个异常照抛，逐次失败的原因也已经在 `logs/app/` 里
        if config.SAVE_PROMPTS_LOG:
            err_log = Path(f"{PROMPT_LOG_DIR}/chat_err_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.log")
            err_log.parent.mkdir(parents=True, exist_ok=True)
            err_log.write_text(
                json.dumps([message.to_dict() for message in messages], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        raise AllLLMRequestsFailedError("所有 LLM 请求失败", kinds=failure_kinds)

    return llm_response, used_model_group, retry_errors
