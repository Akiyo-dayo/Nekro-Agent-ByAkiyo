"""不可信外部内容的清洗与提示词边界工具

合并转发、JSON 卡片、网页抓取结果、邮件正文等内容由外部提供，其中的每一个字节都可能
由攻击者完全控制。这类内容进入提示词前必须满足三条约束：

1. **剥离控制字符**：尤其是换行与双向文本控制符。聊天历史的渲染格式为
   ``[时间 id:xxx] "昵称" 说: 内容``（见 :meth:`DBChatMessage.parse_chat_history_prompt`），
   内容中一旦带上换行，攻击者就能凭空伪造出一条格式完全合法、来自任意 id 的历史消息。
2. **限制长度与规模**：避免单条消息挤占上下文，也避免解析阶段的 CPU/网络/磁盘放大。
3. **用带一次性会话令牌的标签包裹**：``nekro_agent/services/agent/templates/j2/runtime_contract.j2``
   中的安全契约规定，模型只信任携带会话令牌的结构化标签。裸拼的外部内容与真实对话
   完全同构，模型无从分辨。

新增任何外部内容来源时，都应当复用本模块，而不是各自实现一遍清洗逻辑。
"""

import re
from dataclasses import dataclass, field
from typing import Dict

# 控制字符 + 零宽字符 + Unicode 行分隔符 + 双向文本覆盖符
# 这些字符要么能破坏提示词的行结构，要么能在人工审阅时造成视觉欺骗
_DANGEROUS_CHAR_PATTERN = re.compile(
    "[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f\\u200b-\\u200f\\u2028\\u2029\\u202a-\\u202e\\u2066-\\u2069]",
)
_ANY_WHITESPACE_PATTERN = re.compile(r"\s+")
_EXCESSIVE_NEWLINE_PATTERN = re.compile(r"\n{3,}")

TRUNCATION_MARK = "…"

# 沙盒方法返回 `UntrustedToolResult` 时，回给沙盒的只有这句话。真正的内容由服务端交给模型
UNTRUSTED_RESULT_PLACEHOLDER = "(the result is attached below by the host, not passed through the sandbox)"


def sanitize_inline_text(text: str, max_length: int) -> str:
    """清洗单行不可信文本

    折叠包括换行在内的所有空白为单个空格，用于昵称、标题这类必须保持单行的字段。

    Args:
        text: 原始文本
        max_length: 最大保留长度，超出部分以省略号替代

    Returns:
        清洗后的单行文本
    """
    cleaned = _ANY_WHITESPACE_PATTERN.sub(" ", _DANGEROUS_CHAR_PATTERN.sub("", text)).strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length] + TRUNCATION_MARK
    return cleaned


def sanitize_block_text(text: str, max_length: int) -> str:
    """清洗多行不可信文本

    保留换行语义（正文本身可能是多段），但剥离其他控制字符并压缩连续空行。
    注意：保留换行意味着调用方必须自行保证该文本被包裹在带令牌的边界标签内，
    否则仍可伪造历史消息行。

    Args:
        text: 原始文本
        max_length: 最大保留长度，超出部分以省略号替代

    Returns:
        清洗后的多行文本
    """
    cleaned = _DANGEROUS_CHAR_PATTERN.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    cleaned = _EXCESSIVE_NEWLINE_PATTERN.sub("\n\n", cleaned).strip()
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length] + TRUNCATION_MARK
    return cleaned


def wrap_untrusted_block(one_time_code: str, kind: str, body: str, **attrs: object) -> str:
    """用带一次性会话令牌的标签包裹不可信内容

    生成形如 ``<{code} | Forward nodes:3 trust:untrusted>...<{code} | /Forward>`` 的结构。
    攻击者无法预知令牌，因此无法伪造边界，模型据此判定标签内的内容不可信。

    Args:
        one_time_code: 本次会话的一次性令牌
        kind: 内容类型标识，如 ``Forward``
        body: 已清洗的内容主体
        **attrs: 附加到开标签上的元信息（来源、节点数等）

    Returns:
        包裹后的提示词片段
    """
    attr_str = "".join(f" {key}:{value}" for key, value in attrs.items())
    return f"<{one_time_code} | {kind}{attr_str} trust:untrusted>\n{body}\n<{one_time_code} | /{kind}>"


@dataclass(frozen=True)
class UntrustedToolResult:
    """沙盒方法交回来的不可信内容

    `AGENT` 方法的普通返回值会落成 SYSTEM 消息并回传沙盒，而提示词要求模型信任 SYSTEM 消息，
    所以外部内容（模型看过视频之后写的话、抓来的网页）不能走那条路：用本轮令牌包好也没用，
    下一轮令牌换了，那段文字就只剩一条「SYSTEM 说的话」。返回这个类型时：

    - 落库不用 SYSTEM 身份、不带令牌，渲染历史时用当时的令牌重新包裹；
    - 回给沙盒的只有 :data:`UNTRUSTED_RESULT_PLACEHOLDER`，令牌与正文都不进容器；
    - 本轮的完整内容由服务端直接拼进模型上下文（`session_token.attach_untrusted_result`）。

    Attributes:
        kind: 标签名，只能是字母，由插件写死
        body: 已清洗的不可信正文
        summary: 插件自己算出来的可信说明，不得含外部内容。它会进 `content_text`，
            被记忆整理、好感评估读到，所以正文不放这里
        attrs: 开标签上的数字属性
    """

    kind: str
    body: str
    summary: str = ""
    attrs: Dict[str, int] = field(default_factory=dict)


def render_untrusted_result(one_time_code: str, result: UntrustedToolResult) -> str:
    """用给定令牌渲染结果：可信说明在标签外，正文在标签内"""
    wrapped = wrap_untrusted_block(one_time_code, result.kind, result.body, **result.attrs)
    return f"{result.summary}\n{wrapped}" if result.summary else wrapped
