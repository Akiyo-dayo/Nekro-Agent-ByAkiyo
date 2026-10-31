"""模型能力声明的解析、观测与「按能力裁剪请求」

## 为什么没有新增字段

能力声明这条链路**上游已经有了**，缺的只是接线：

| 环节 | 现状 |
| --- | --- |
| 配置项 | `ModelConfigGroup.ENABLE_VISION`（`core/config.py`） |
| 存储 | `model_group.enable_vision`（`models/db_model_group.py`，双向映射齐全） |
| API | `routers/console/model_groups.py` 的 `enable_vision` |
| WebUI | 模型组表单里的「视觉」开关 |
| 已有用例 | `plugins/builtin/view_image.py` 就是拿它当能力判据的 |

真正缺的三处接线是：① 装配历史图片时只对**主**模型组判了一次，fallback 模型组从头到尾
没人读它；② 插件返回的多模态结果原样塞回上下文，完全不判；③ 请求失败后重试与 fallback
对它零感知。所以本模块只做接线与判据收敛，不另造一套能力声明。

## 声明之外为什么还要观测

声明是人填的，会填错——本次事故的直接诱因就是 WebUI 把 `ENABLE_VISION` 默认成 true
（后端与 DB 默认都是 False），新建模型组时带着这个默认值落了盘，于是每一轮对话都先发一次
几 MB 的图、再吃一个 400、再原样重试三次。只读声明修不掉这种情形。

所以除了读声明，还从上游那句「当前模型不支持该能力：vision」里学一条**负判据**。这条
观测必须是 **有 TTL、可自愈** 的：换模型、上游修好、或者只是一次误判，都不能把一个
本来能看图的模型永久钉成瞎子。这与 `services/sandbox/network_guard.py` 那条教训同源
（进程内缓存的「我知道」必须能被现实推翻），只不过这里的现实来自上游的报错而不是内核。

## 判据的方向性

分类只在「本次请求真的带了图」时才可能得出 `VISION_UNSUPPORTED`：没带图的请求不可能
因为图被拒。这条前置条件把误判面收得很窄，剩下的靠状态码 + 文案双条件。
"""

import re
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from nekro_agent.core.config import ModelConfigGroup
from nekro_agent.core.logger import get_sub_logger

from .creator import ContentSegment, OpenAIChatMessage
from .openai import EmptyLLMResponseError, LLMContentBlockedError

logger = get_sub_logger("agent_runtime")

# 模型看不到图时替换上去的占位文本。特意说明「为什么没有图」而不是静默删掉：
# 模型至少还知道这条消息里本来有一张图，也知道可以按上下文里的路径去处理它。
IMAGE_PLACEHOLDER_TEXT = (
    "[image omitted: the current model group has no vision capability declared. "
    "Refer to the image by the path reference in the surrounding text if you need to act on it.]"
)

# 负判据的存活时间。取 10 分钟：足够覆盖一整段连续对话（不必每轮都先炸一次），
# 又短到运维改完配置/换完模型后不需要重启进程就能自己恢复。
VISION_MARK_TTL_SECONDS = 600.0

# 负判据条目上限。键来自模型组配置（上游地址 + 模型名），天然被配置规模限制在很小的范围内，
# 这个上限只是防止异常配置把字典撑爆。
_MAX_MARKS = 256

# (base_url, model, capability) -> 过期时间戳
_capability_marks: Dict[Tuple[str, str, str], float] = {}

VISION_CAPABILITY = "vision"


class LLMFailureKind(str, Enum):
    """一次 LLM 请求失败的归类

    只分到「能改变下一步动作」的粒度，不做更细的分类：
    - `VISION_UNSUPPORTED`：请求里的图片是失败原因，换模型或剥图才有意义，原样重试必然再失败
    - `CONTENT_BLOCKED`：上游内容策略把这条回复挡下来了，原样重试必然再被挡，
      用户要的是「换个说法」；文案也不能说成「模型一个字都没返回」
    - `EMPTY_RESPONSE`：上游 2xx 但正文为空，重试有意义（可能是偶发）
    - `OTHER`：其余一律按原有语义重试
    """

    VISION_UNSUPPORTED = "vision_unsupported"
    CONTENT_BLOCKED = "content_blocked"
    EMPTY_RESPONSE = "empty_response"
    OTHER = "other"


# 只要文案里同时出现「模态词」与「拒绝词」，且请求确实带了图、状态码是客户端错误，
# 才判为能力不匹配。单独一个词都不足以定性：例如「invalid image url」是图挂了不是模型不支持。
_MODALITY_TOKENS = (
    "vision",
    "image_url",
    "image",
    "multimodal",
    "multi-modal",
    "图片",
    "图像",
    "视觉",
    "多模态",
)
_REJECTION_TOKENS = (
    "not support",
    "not supported",
    "does not support",
    "doesn't support",
    "do not support",
    "don't support",
    "unsupported",
    "no support",
    "only supported by",
    "invalid content type",
    "cannot process",
    "can not process",
    "不支持",
    "不支援",
    "无法处理",
    "不可用于",
)

# 上游 SDK 没有把状态码挂到异常上时（例如中转站抛的是通用异常），从文案里兜底取一次
_STATUS_IN_TEXT = re.compile(r"error code:\s*(\d{3})", re.IGNORECASE)

# 能被「请求内容不合法」解释的客户端错误码。401/403/429 明确排除：那是凭据与限流，
# 剥图既不会让它变好，也不该污染能力判据。
_CONTENT_REJECT_STATUS = frozenset({400, 413, 415, 422})


def _group_key(model_group: ModelConfigGroup, capability: str) -> Tuple[str, str, str]:
    return ((model_group.BASE_URL or "").strip().rstrip("/"), (model_group.CHAT_MODEL or "").strip(), capability)


def _prune_marks(now: float) -> None:
    expired = [key for key, expires_at in _capability_marks.items() if expires_at <= now]
    for key in expired:
        _capability_marks.pop(key, None)
    while len(_capability_marks) > _MAX_MARKS:
        oldest = min(_capability_marks, key=lambda key: _capability_marks[key])
        _capability_marks.pop(oldest, None)


def note_capability_unsupported(
    model_group: ModelConfigGroup,
    capability: str = VISION_CAPABILITY,
    *,
    now: Optional[float] = None,
) -> None:
    """记下「这个模型刚刚明确拒绝了这项能力」"""
    current = time.time() if now is None else now
    _prune_marks(current)
    _capability_marks[_group_key(model_group, capability)] = current + VISION_MARK_TTL_SECONDS


def note_capability_ok(
    model_group: ModelConfigGroup,
    capability: str = VISION_CAPABILITY,
    *,
    now: Optional[float] = None,
) -> None:
    """带着该能力的请求成功了：立刻作废负判据

    观测必须双向，否则「学到的东西」就变成了只会积累的偏见。
    """
    current = time.time() if now is None else now
    _prune_marks(current)
    _capability_marks.pop(_group_key(model_group, capability), None)


def is_capability_marked(
    model_group: ModelConfigGroup,
    capability: str = VISION_CAPABILITY,
    *,
    now: Optional[float] = None,
) -> bool:
    """该模型是否处于「刚被上游拒绝过这项能力」的窗口内"""
    current = time.time() if now is None else now
    expires_at = _capability_marks.get(_group_key(model_group, capability))
    if expires_at is None:
        return False
    if expires_at <= current:
        _capability_marks.pop(_group_key(model_group, capability), None)
        return False
    return True


def reset_capability_marks() -> None:
    """清空全部负判据（供测试与「配置刚改完，别等 TTL」的场景使用）"""
    _capability_marks.clear()


def declares_vision(model_group: ModelConfigGroup) -> bool:
    """模型组是否**声明**了视觉能力

    未声明一律当作不支持。理由：两种错法的代价不对称——把能看图的模型判成看不到，
    代价是这一轮少了图片这一路信息，回合照常完成；把看不到图的模型判成能看，
    代价是整个回合连同用户那条消息一起失败，而且因为图片取自整个上下文窗口，
    连纯文字消息也会被拖下水。所以判据向「少给一点信息」这一侧倒。
    """
    return bool(model_group.ENABLE_VISION)


def supports_vision(model_group: ModelConfigGroup, *, now: Optional[float] = None) -> bool:
    """本次装配时该模型组能不能收图：声明为准，被上游打过脸则暂时降级"""
    return declares_vision(model_group) and not is_capability_marked(model_group, VISION_CAPABILITY, now=now)


def _extract_status_code(exc: Exception) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    match = _STATUS_IN_TEXT.search(str(exc))
    if match:
        return int(match.group(1))
    return None


def looks_like_capability_rejection(text: str) -> bool:
    """文案是否在说「这个模型不支持图片这类输入」"""
    lowered = text.lower()
    if not any(token in lowered for token in _MODALITY_TOKENS):
        return False
    return any(token in lowered for token in _REJECTION_TOKENS)


def classify_llm_failure(exc: Exception, *, request_has_image: bool) -> LLMFailureKind:
    """把一次 LLM 请求失败归类，决定下一步是重试、换模型还是剥图

    Args:
        exc: `gen_openai_chat_response` 抛出的异常
        request_has_image: **本次实际发出去的**请求体里是否含图片片段。剥过图的请求恒为 False，
            因此不可能再被判成能力不匹配——这条前置条件是误判面的主要约束
    """
    # 顺序不能反：`LLMContentBlockedError` 是 `EmptyLLMResponseError` 的**子类**
    # （故意设计成这样，好让既有的 `except EmptyLLMResponseError` 一行都不用改）。
    # 先判父类的话，安全拦截会被整个吞成 EMPTY_RESPONSE，用户拿到的还是那句
    # 「模型一个字都没返回」——「另一半没修」在这里就是一行 if 的先后。
    if isinstance(exc, LLMContentBlockedError):
        return LLMFailureKind.CONTENT_BLOCKED
    # `LLMMalformedToolCallError` 同为子类，但**故意**落到这里：它的下一步动作
    # （重试有意义、文案是「一个字都没返回，再说一次」）与空响应完全一致，
    # 详见 `openai.LLMMalformedToolCallError` 的说明
    if isinstance(exc, EmptyLLMResponseError):
        return LLMFailureKind.EMPTY_RESPONSE
    if not request_has_image:
        return LLMFailureKind.OTHER
    status = _extract_status_code(exc)
    if status not in _CONTENT_REJECT_STATUS:
        return LLMFailureKind.OTHER
    if not looks_like_capability_rejection(str(exc)):
        return LLMFailureKind.OTHER
    return LLMFailureKind.VISION_UNSUPPORTED


def _message_content_segments(message: OpenAIChatMessage) -> Optional[List[Dict[str, Any]]]:
    content = message.content
    if isinstance(content, list):
        return content
    return None


def has_image_segments(messages: Sequence[OpenAIChatMessage]) -> bool:
    """消息列表里是否含有图片片段"""
    for message in messages:
        segments = _message_content_segments(message)
        if segments and any(isinstance(seg, dict) and seg.get("type") == "image_url" for seg in segments):
            return True
    return False


def strip_image_segments(
    messages: Sequence[OpenAIChatMessage],
    *,
    placeholder: str = IMAGE_PLACEHOLDER_TEXT,
) -> Tuple[List[OpenAIChatMessage], int]:
    """把图片片段换成占位文本，返回 (新消息列表, 被替换的图片数)

    不修改传入的消息对象：同一轮里 `run_agent` 还要拿原始 `messages` 继续往后拼迭代上下文，
    而下一次尝试用的是哪个模型组、要不要剥图，是那时候才决定的。
    未含图片的消息原样透传（连同其 content 的 dict 一起共享引用），避免为几 MB 的 base64 做无谓复制。
    """
    new_messages: List[OpenAIChatMessage] = []
    removed = 0
    for message in messages:
        segments = _message_content_segments(message)
        if not segments or not any(isinstance(seg, dict) and seg.get("type") == "image_url" for seg in segments):
            new_messages.append(message)
            continue
        new_content: List[Dict[str, Any]] = []
        for seg in segments:
            if isinstance(seg, dict) and seg.get("type") == "image_url":
                removed += 1
                new_content.append(ContentSegment.text_content(placeholder))
            else:
                new_content.append(seg)
        new_messages.append(OpenAIChatMessage(message.role, new_content).tidy())
    return new_messages, removed
