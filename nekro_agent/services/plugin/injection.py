"""插件提示注入的落点与包裹

注入方法返回 `str` 时行为与以前逐字相同：文字放进历史消息开头的插件上下文里。返回
:class:`~nekro_agent.services.plugin.schema.PromptInjection` 时，由这里决定怎么包、放在哪：

- 不可信的正文先清洗（去不可见与控制字符、拆掉形如 `<令牌 | …>` 的伪造边界），再用本轮令牌包成
  不可信块。令牌是核心在这一轮里生成的，插件拿到的只是渲染好的结果，伪造不了宿主的标签。
- 拿不到本轮令牌时，不可信的正文一律不注入：没包裹的外部文字不能进提示词。
- 上一轮被内容审核拦下时（与世界书不可信条目的抑制同一个判据），这一轮不可信的注入一律不注入。
- 带深度的块交给历史渲染，按世界书「深度」的同一套下标规则插进聊天记录。
- `preface` 是放在包裹之外的宿主说明，只能是插件写死的常量；这里照样清洗、拆边界、截断，
  防的是插件写错，不是给外部文字留口子。

另有一个**不用令牌**的就地版本（:func:`render_injection_inline`），给插件激活这类会把结果送回
沙盒、落成系统消息的入口用：本轮令牌绝不能出现在那里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from nekro_agent.core.logger import get_sub_logger
from nekro_agent.tools.untrusted_content import (
    defang_boundary_tags,
    sanitize_block_text,
    wrap_untrusted_block,
)

from .schema import PromptInjection

logger = get_sub_logger("plugin.injection")

DEFAULT_KIND = "PluginContext"
# 不可信正文的上限；宿主说明另有上限。超出部分由清洗函数截断并加省略标记。
MAX_UNTRUSTED_CHARS = 20_000
MAX_PREFACE_CHARS = 1_000
# 与世界书条目的深度上限一致。
MAX_DEPTH = 100

_KIND_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]{0,31}")
_ATTR_UNSAFE = re.compile(r"[^\w.-]", re.ASCII)


@dataclass(frozen=True)
class RenderedInjection:
    """一个插件这一轮注入的结果：渲染好的文字，以及放在哪（`None` 为历史消息开头）"""

    text: str
    depth: Optional[int] = None


def _kind_of(value: Any) -> str:
    if isinstance(value, str) and _KIND_PATTERN.fullmatch(value):
        return value
    return DEFAULT_KIND


def _depth_of(value: Any) -> Optional[int]:
    # `bool` 是 `int` 的子类，`depth=True` 不能当成深度 1。
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, min(MAX_DEPTH, value))


def _preface_of(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return defang_boundary_tags(sanitize_block_text(value, MAX_PREFACE_CHARS))


def _structured(raw: Any, module_name: str) -> Optional[PromptInjection]:
    """结构化返回且正文是字符串时返回它；其它类型记一条日志、返回 `None`"""
    if not isinstance(raw, PromptInjection):
        if raw is not None:
            logger.warning(f"插件 {module_name} 的提示注入返回了不支持的类型 {type(raw).__name__}，本轮不注入")
        return None
    if not isinstance(raw.text, str):
        logger.warning(f"插件 {module_name} 的提示注入正文不是字符串，本轮不注入")
        return None
    return raw


def render_injection(
    raw: Any,
    *,
    one_time_code: str,
    module_name: str,
    suppress_untrusted: bool = False,
) -> RenderedInjection:
    """把注入方法的返回值变成这一轮要放进提示词的文字

    - `str`：原样返回、放在历史消息开头（与以前逐字相同）；
    - :class:`PromptInjection`：按上面模块说明包裹；`suppress_untrusted` 为真（上一轮被拦）时
      不可信的整段不注入；
    - 其它类型：当成没有注入。
    """
    if isinstance(raw, str):
        return RenderedInjection(raw)
    injection = _structured(raw, module_name)
    if injection is None:
        return RenderedInjection("")

    depth = _depth_of(injection.depth)
    kind = _kind_of(injection.kind)
    preface = _preface_of(injection.preface)
    if injection.untrusted:
        if suppress_untrusted:
            return RenderedInjection("", depth)
        if not one_time_code:
            logger.debug(f"插件 {module_name} 的不可信注入拿不到本轮令牌，本轮不注入")
            return RenderedInjection("", depth)
        body = defang_boundary_tags(sanitize_block_text(injection.text, MAX_UNTRUSTED_CHARS))
        if not body:
            return RenderedInjection("", depth)
        plugin_attr = _ATTR_UNSAFE.sub("_", module_name) or "unknown"
        block = wrap_untrusted_block(one_time_code, kind, body, source="plugin", plugin=plugin_attr)
    else:
        if not injection.text.strip():
            return RenderedInjection("", depth)
        if depth is None or not one_time_code:
            block = injection.text
        else:
            # 插进聊天记录的宿主文字也带令牌标签，模型据此分得清「这一段不是哪位群友说的」。
            block = f"<{one_time_code} | {kind} source:plugin>\n{injection.text}\n<{one_time_code} | /{kind}>"
    text = f"{preface}\n{block}" if preface else block
    return RenderedInjection(text, depth)


def render_injection_inline(raw: Any, *, module_name: str) -> str:
    """不用令牌的就地版本：只有 `untrusted=False` 且不指定深度的结构化返回才给出文字

    给会把结果送回沙盒、落成系统消息的入口用（插件激活）：那里绝不能出现本轮令牌，也不能把
    聊天里来的文字变成「系统说的话」。不可信的、要插到深度的一律给空串。
    """
    if isinstance(raw, str):
        return raw
    injection = _structured(raw, module_name)
    if injection is None or injection.untrusted or _depth_of(injection.depth) is not None:
        return ""
    if not injection.text.strip():
        return ""
    preface = _preface_of(injection.preface)
    return f"{preface}\n{injection.text}" if preface else injection.text


__all__ = [
    "DEFAULT_KIND",
    "MAX_DEPTH",
    "MAX_PREFACE_CHARS",
    "MAX_UNTRUSTED_CHARS",
    "RenderedInjection",
    "render_injection",
    "render_injection_inline",
]
